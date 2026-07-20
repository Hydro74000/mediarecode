"""
core/matroska/hevc/access_units.py

Découpage d'un flux HEVC annexB en access units (= frames Matroska).

Un access unit HEVC contient typiquement :
  - éventuels NAL units delimiter / VPS / SPS / PPS (en début de stream, et
    potentiellement avant chaque keyframe selon la sortie ffmpeg)
  - éventuels SEI prefix (notamment HDR10+ : NAL type 39)
  - les NAL units de slice (types 0..31), avec exactement UN d'entre eux
    ayant ``first_slice_segment_in_pic_flag = 1`` qui marque le début de
    l'image
  - éventuels suffixes après les slices : SEI suffix, et le RPU Dolby
    Vision (NAL type 62, "unspecified" ITU-T, écrit par dovi_tool après
    les slices de la frame à laquelle il s'applique).

Algorithme :
  1. Découper le flux en NAL units (séparateurs 0x000001 ou 0x00000001).
  2. Décoder le header NAL (2 octets pour HEVC).
  3. Marquer un nouvel access unit à chaque NAL slice (types 0..31) qui a
     ``first_slice_segment_in_pic_flag = 1`` après une exception (le 1er AU
     du flux), ou à chaque NAL non-slice si l'AU précédent contenait au
     moins un slice (ce qui détecte la frontière naturelle).

Contrainte du muxer : chaque access unit doit former un seul SimpleBlock
Matroska. Tous les NAL d'un même AU restent collés dans le payload du Block.
La détection des keyframes se fait via le NAL type :
  - 16..21 = IRAP (BLA, IDR, CRA) → keyframe.
  - sinon → non-keyframe.

Référence : ITU-T H.265 §7.3.1.2 (NAL unit syntax), §7.4.2.4.4 (detection
of first VCL NAL unit of an access unit).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable


# Séparateurs annexB
_START_CODE_4 = b"\x00\x00\x00\x01"
_START_CODE_3 = b"\x00\x00\x01"

#: Taille de lecture par défaut du parcours streaming (octets).
DEFAULT_HEVC_CHUNK_SIZE = 4 * 1024 * 1024


class HevcStreamCancelled(Exception):
    """Annulation coopérative demandée pendant le parcours du flux HEVC."""


# Types NAL HEVC pertinents
_NAL_TYPE_TRAIL_N = 0
_NAL_TYPE_TRAIL_R = 1
_NAL_TYPE_BLA_W_LP = 16
_NAL_TYPE_BLA_W_RADL = 17
_NAL_TYPE_BLA_N_LP = 18
_NAL_TYPE_IDR_W_RADL = 19
_NAL_TYPE_IDR_N_LP = 20
_NAL_TYPE_CRA_NUT = 21
_NAL_TYPE_VPS = 32
_NAL_TYPE_SPS = 33
_NAL_TYPE_PPS = 34
_NAL_TYPE_AUD = 35
_NAL_TYPE_EOS = 36
_NAL_TYPE_EOB = 37
_NAL_TYPE_FD = 38
_NAL_TYPE_PREFIX_SEI = 39
_NAL_TYPE_SUFFIX_SEI = 40

_VCL_RANGE = range(0, 32)
_IRAP_TYPES = frozenset({
    _NAL_TYPE_BLA_W_LP, _NAL_TYPE_BLA_W_RADL, _NAL_TYPE_BLA_N_LP,
    _NAL_TYPE_IDR_W_RADL, _NAL_TYPE_IDR_N_LP, _NAL_TYPE_CRA_NUT,
})


@dataclass
class HevcNalUnit:
    """Un NAL unit HEVC + ses métadonnées principales."""
    payload: bytes        # Les octets du NAL (sans le start code annexB).
    nal_type: int
    first_slice_in_pic: bool


@dataclass
class HevcAccessUnit:
    """Un access unit = ensemble de NAL formant 1 frame présentée."""
    payload: bytes = b""
    is_keyframe: bool = False
    nal_units: list[HevcNalUnit] = field(default_factory=list)


def _iter_nal_units(stream: bytes) -> Iterator[HevcNalUnit]:
    """
    Itère sur les NAL units d'un buffer annexB. Tolère les start codes
    courts (3 octets) et longs (4 octets), tels qu'émis par ffmpeg
    ``-f hevc``.
    """
    n = len(stream)
    if n == 0:
        return

    # Localise tous les start codes.
    positions: list[tuple[int, int]] = []  # (start_code_offset, payload_offset)
    i = 0
    while i < n - 2:
        if stream[i] == 0 and stream[i + 1] == 0:
            if i + 2 < n and stream[i + 2] == 1:
                positions.append((i, i + 3))
                i += 3
                continue
            if i + 3 < n and stream[i + 2] == 0 and stream[i + 3] == 1:
                positions.append((i, i + 4))
                i += 4
                continue
        i += 1

    if not positions:
        return

    for idx, (_, payload_off) in enumerate(positions):
        next_off = positions[idx + 1][0] if idx + 1 < len(positions) else n
        nal_bytes = stream[payload_off:next_off]
        if len(nal_bytes) < 2:
            continue
        # Header NAL HEVC (2 octets) :
        #   forbidden_zero_bit (1) + nal_unit_type (6) + nuh_layer_id (6) +
        #   nuh_temporal_id_plus1 (3)
        header_byte_0 = nal_bytes[0]
        nal_type = (header_byte_0 >> 1) & 0x3F

        first_slice = False
        if nal_type in _VCL_RANGE and len(nal_bytes) >= 3:
            # Le 3ème octet contient (entre autres) ``first_slice_segment_in_pic_flag``
            # comme premier bit du RBSP slice_segment_header().
            first_slice = bool(nal_bytes[2] & 0x80)

        yield HevcNalUnit(
            payload=nal_bytes,
            nal_type=nal_type,
            first_slice_in_pic=first_slice,
        )


def _group_access_units(nal_units: Iterator[HevcNalUnit]) -> Iterator[HevcAccessUnit]:
    """Regroupe un flux de NAL units en access units (générateur partagé).

    Logique de séparation : un AU se termine quand on rencontre soit
    a) un NAL slice avec ``first_slice_segment_in_pic_flag = 1`` (= début
    d'une nouvelle frame), précédé d'au moins un slice dans l'AU courant ;
    soit b) un NAL non-slice "préfixe" (VPS/SPS/PPS/AUD/SEI prefix) qui
    suit un slice — il appartient au prochain AU.

    Les NAL 62/63 (RPU Dolby Vision et réservé) sont des **suffixes** : le
    RPU d'une frame est écrit après ses slices (dovi_tool, remux DV) et doit
    rester dans l'AU courant — sinon chaque RPU glisse d'une frame et le
    dernier forme un AU fantôme.
    """
    current = HevcAccessUnit()
    has_slice = False

    def _sealed(unit: HevcAccessUnit) -> HevcAccessUnit:
        unit.payload = b"".join(_emit_nal(n) for n in unit.nal_units)
        return unit

    for nal in nal_units:
        is_slice = nal.nal_type in _VCL_RANGE
        # Frontière A : nouveau slice "first in pic" alors qu'on en avait déjà un.
        boundary = is_slice and nal.first_slice_in_pic and has_slice
        # Frontière B : NAL "prefix" (VPS/SPS/PPS/AUD/SEI prefix) qui suit
        # au moins un slice → appartient au prochain AU. Les suffixes (SEI
        # suffix, RPU DoVi 62, 63) restent dans l'AU courant.
        if not boundary and not is_slice and has_slice and nal.nal_type in {
            _NAL_TYPE_VPS, _NAL_TYPE_SPS, _NAL_TYPE_PPS,
            _NAL_TYPE_AUD, _NAL_TYPE_PREFIX_SEI,
        }:
            boundary = True
        if boundary and current.nal_units:
            yield _sealed(current)
            current = HevcAccessUnit()
            has_slice = False

        current.nal_units.append(nal)
        if is_slice:
            has_slice = True
            if nal.nal_type in _IRAP_TYPES:
                current.is_keyframe = True

    if current.nal_units:
        yield _sealed(current)


def split_into_access_units(stream: bytes) -> list[HevcAccessUnit]:
    """
    Découpe ``stream`` (HEVC annexB) en access units. Chaque AU correspond
    à exactement une frame présentée et porte ses NAL non-slice précédents
    (VPS, SPS, PPS, SEI prefix dont HDR10+) ainsi que ses suffixes (RPU
    Dolby Vision NAL 62).
    """
    return list(_group_access_units(_iter_nal_units(stream)))


def iter_hevc_nal_units(
    source: Path | BinaryIO,
    chunk_size: int = DEFAULT_HEVC_CHUNK_SIZE,
    *,
    cancel_cb: Callable[[], bool] | None = None,
    progress_cb: Callable[[int], None] | None = None,
) -> Iterator[HevcNalUnit]:
    """Itère les NAL units d'un flux annexB en mémoire bornée.

    Lecture par blocs de ``chunk_size`` octets ; les start codes traversant
    deux chunks sont gérés par un buffer frontière. La mémoire est limitée
    au NAL courant plus un chunk. ``cancel_cb`` est vérifié à chaque chunk
    (lève :class:`HevcStreamCancelled`) ; ``progress_cb`` reçoit le cumul
    d'octets lus.
    """
    own_handle = isinstance(source, Path)
    handle: BinaryIO = source.open("rb") if isinstance(source, Path) else source
    try:
        buffer = bytearray()
        started = False
        search = 0
        total_read = 0
        eof = False
        while True:
            if cancel_cb is not None and cancel_cb():
                raise HevcStreamCancelled("Parcours HEVC annulé.")
            # Consomme tous les start codes présents dans le buffer.
            while True:
                found = buffer.find(_START_CODE_3, search)
                if found < 0:
                    break
                if not started:
                    # Premier start code : tout ce qui précède est ignoré.
                    del buffer[:found + 3]
                    started = True
                    search = 0
                    continue
                # Un start code long (00 00 00 01) absorbe l'octet zéro qui
                # précède immédiatement le motif court.
                code_start = found - 1 if found > 0 and buffer[found - 1] == 0 else found
                nal_bytes = bytes(buffer[:code_start])
                del buffer[:found + 3]
                search = 0
                if len(nal_bytes) >= 2:
                    yield _decode_nal(nal_bytes)
            if eof:
                break
            if started:
                # Un motif partiel peut chevaucher la frontière de chunk.
                search = max(0, len(buffer) - 3)
            elif len(buffer) > 3:
                del buffer[:len(buffer) - 3]
            chunk = handle.read(chunk_size)
            if not chunk:
                eof = True
                continue
            total_read += len(chunk)
            if progress_cb is not None:
                progress_cb(total_read)
            buffer.extend(chunk)
        if started and len(buffer) >= 2:
            yield _decode_nal(bytes(buffer))
    finally:
        if own_handle:
            handle.close()


def iter_hevc_access_units(
    source: Path | BinaryIO,
    chunk_size: int = DEFAULT_HEVC_CHUNK_SIZE,
    *,
    cancel_cb: Callable[[], bool] | None = None,
    progress_cb: Callable[[int], None] | None = None,
) -> Iterator[HevcAccessUnit]:
    """Itère les access units d'un flux annexB en mémoire bornée.

    La mémoire reste limitée à l'access unit courant et au buffer frontière —
    aucun ``Path.read_bytes()`` sur le bitstream complet.
    """
    return _group_access_units(iter_hevc_nal_units(
        source, chunk_size, cancel_cb=cancel_cb, progress_cb=progress_cb,
    ))


def _decode_nal(nal_bytes: bytes) -> HevcNalUnit:
    """Décode le header NAL HEVC (2 octets) d'un payload sans start code."""
    nal_type = (nal_bytes[0] >> 1) & 0x3F
    first_slice = False
    if nal_type in _VCL_RANGE and len(nal_bytes) >= 3:
        first_slice = bool(nal_bytes[2] & 0x80)
    return HevcNalUnit(
        payload=nal_bytes,
        nal_type=nal_type,
        first_slice_in_pic=first_slice,
    )


def _emit_nal(nal: HevcNalUnit) -> bytes:
    """Réémet un NAL avec un start code annexB long (4 octets)."""
    return _START_CODE_4 + nal.payload


__all__ = [
    "DEFAULT_HEVC_CHUNK_SIZE",
    "HevcAccessUnit",
    "HevcNalUnit",
    "HevcStreamCancelled",
    "iter_hevc_access_units",
    "iter_hevc_nal_units",
    "split_into_access_units",
]
