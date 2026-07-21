"""Réécriture des payloads HEVC d'un MKV encodé avec les access units injectés.

Pipeline lot 3 : le flux vidéo encodé existe d'abord sous forme d'un MKV
mono-piste horodaté (timestamps écrits par l'encodeur, jamais réassociés
depuis la source d'origine). Après extraction annexB et injection
HDR10+/Dolby Vision, ce module consomme **en lockstep** :

- les blocs vidéo du MKV encodé (ordre de décodage, PTS, durées, keyframes
  et références conservés tels quels) ;
- les access units HEVC injectés (streaming, mémoire bornée) ;
- les métadonnées Dolby Vision (record ``BlockAdditionMapping``).

Il produit un itérateur de :class:`MatroskaMuxPacket` consommé par le writer
générique, qui écrit un MKV mono-piste candidat (commit atomique). Une
différence de nombre entre blocs encodés et access units injectés provoque
un échec strict avant commit (le candidat ``.partial`` est supprimé).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from ..editors.dovi import DolbyVisionConfigRecord
from ..ids import (
    BLOCK_ADDITION_MAPPING_ID,
    CODEC_PRIVATE_ID,
)
from ..ebml import binary_element, element
from .access_units import (
    DEFAULT_HEVC_CHUNK_SIZE,
    HevcAccessUnit,
    iter_hevc_access_units,
)
from ..mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack
from ..native_muxer import (
    _build_dovi_block_addition_mapping,
    _build_hvcc,
    _extract_hvcc_components,
    _HvccComponents,
)
from ..reader import MatroskaReader, payload_children
from ..writer import MatroskaWriteProgress, MatroskaWriter


#: Types NAL des parameter sets stockés dans le CodecPrivate hvcC.
_PARAMETER_SET_TYPES = frozenset({32, 33, 34})  # VPS, SPS, PPS

_TRACK_TYPE_VIDEO = 1


class HevcPayloadAlignmentError(RuntimeError):
    """Blocs encodés et access units injectés désalignés (échec strict)."""


@dataclass(frozen=True)
class HevcPayloadRewriteResult:
    """Résultat de la réécriture (artefact MKV mono-piste horodaté)."""

    output: Path
    frames_rewritten: int
    codec_private_updated: bool
    dovi_mapping_written: bool


def _hvcc_length_size(codec_private: bytes) -> int | None:
    """Taille des préfixes de longueur NAL du hvcC (1/2/4), ou None si non-hvcC."""
    if len(codec_private) < 23 or codec_private[0] != 1:
        return None
    return (codec_private[21] & 0x03) + 1


def _updated_hvcc(original: bytes, components: _HvccComponents) -> bytes:
    """Reconstruit le hvcC : header d'origine conservé, arrays VPS/SPS/PPS remplacés.

    Le header de 23 octets écrit par l'encodeur (profil, niveau, chroma…)
    est plus précis que le header minimal ; seuls les NAL arrays sont mis à
    jour depuis le flux injecté. Repli sur le hvcC minimal si l'original
    n'est pas un hvcC valide.
    """
    if not (components.vps and components.sps and components.pps):
        return original
    if len(original) < 23 or original[0] != 1:
        return _build_hvcc(components)
    out = bytearray(original[:22])
    arrays: list[tuple[int, list[bytes]]] = [
        (32, components.vps), (33, components.sps), (34, components.pps),
    ]
    out.append(len(arrays))
    for nal_type, nals in arrays:
        out.append(0x80 | (nal_type & 0x3F))
        out.extend(len(nals).to_bytes(2, "big"))
        for nal in nals:
            out.extend(len(nal).to_bytes(2, "big"))
            out.extend(nal)
    return bytes(out)


def _au_to_block_payload(au: HevcAccessUnit, length_size: int | None) -> bytes:
    """Convertit un access unit injecté vers le framing des blocs du MKV.

    hvcC (``length_size`` connu) : NAL préfixés par leur longueur, parameter
    sets exclus (ils vivent dans le CodecPrivate). Sans hvcC : payload annexB
    inchangé (parité avec les artefacts du muxer natif).
    """
    if length_size is None:
        return au.payload
    parts: list[bytes] = []
    for nal in au.nal_units:
        if nal.nal_type in _PARAMETER_SET_TYPES:
            continue
        parts.append(len(nal.payload).to_bytes(length_size, "big"))
        parts.append(nal.payload)
    return b"".join(parts)


def _first_access_unit(injected_hevc: Path, chunk_size: int) -> HevcAccessUnit | None:
    """Pré-scan borné : premier access unit du flux injecté (parameter sets)."""
    for access_unit in iter_hevc_access_units(injected_hevc, chunk_size):
        return access_unit
    return None


def _patched_raw_entry(
    raw_entry: bytes,
    *,
    codec_private: bytes | None,
    dovi_mapping: bytes | None,
) -> bytes:
    """Reconstruit le payload TrackEntry : CodecPrivate remplacé, mapping DoVi ajouté."""
    children: list[bytes] = []
    for element_id, payload in payload_children(raw_entry):
        if element_id == CODEC_PRIVATE_ID and codec_private is not None:
            children.append(binary_element(CODEC_PRIVATE_ID, codec_private))
            continue
        if element_id == BLOCK_ADDITION_MAPPING_ID and dovi_mapping is not None:
            # Remplacé par le mapping reconstruit (ajouté en fin d'entrée).
            continue
        children.append(element(element_id, payload))
    if dovi_mapping is not None:
        children.append(dovi_mapping)
    return b"".join(children)


class MatroskaHevcPayloadRewriter:
    """Remplace les payloads vidéo d'un MKV encodé par les AUs injectés."""

    def __init__(self, *, chunk_size: int = DEFAULT_HEVC_CHUNK_SIZE) -> None:
        self._chunk_size = max(64 * 1024, int(chunk_size))

    def rewrite(
        self,
        *,
        encoded_mkv: Path,
        injected_hevc: Path,
        output: Path,
        dovi_record: DolbyVisionConfigRecord | None = None,
        cancel_cb: Callable[[], bool] | None = None,
        progress_cb: Callable[[MatroskaWriteProgress], None] | None = None,
        external_validator: Callable[[Path], None] | None = None,
    ) -> HevcPayloadRewriteResult:
        """Écrit ``output`` : blocs du MKV encodé, payloads du flux injecté.

        L'ordre de décodage, les PTS, durées, keyframes et références des
        blocs encodés sont conservés à l'identique ; seul le payload change.
        """
        reader = MatroskaReader(encoded_mkv)
        reader.segment()
        tracks = reader.tracks()
        video_tracks = [track for track in tracks if track.track_type == _TRACK_TYPE_VIDEO]
        if len(video_tracks) != 1 or len(tracks) != 1:
            raise HevcPayloadAlignmentError(
                "MKV encodé mono-piste vidéo attendu : "
                f"{len(video_tracks)} piste(s) vidéo / {len(tracks)} piste(s) au total."
            )
        video = video_tracks[0]

        # Pré-scan borné du 1er AU injecté : parameter sets pour le CodecPrivate.
        first_au = _first_access_unit(injected_hevc, self._chunk_size)
        if first_au is None:
            raise HevcPayloadAlignmentError(
                f"Aucun access unit HEVC dans le flux injecté {injected_hevc.name}."
            )
        components = _extract_hvcc_components(first_au)
        length_size = _hvcc_length_size(video.codec_private)
        updated_codec_private: bytes | None = None
        if video.codec_private and components.sps:
            updated_codec_private = _updated_hvcc(video.codec_private, components)
            if updated_codec_private == video.codec_private:
                updated_codec_private = None

        dovi_mapping = (
            _build_dovi_block_addition_mapping(dovi_record)
            if dovi_record is not None
            else None
        )
        new_raw_entry = _patched_raw_entry(
            video.raw_entry,
            codec_private=updated_codec_private,
            dovi_mapping=dovi_mapping,
        )
        rewritten_track = replace(
            video,
            raw_entry=new_raw_entry,
            codec_private=updated_codec_private or video.codec_private,
        )
        mux_track = MatroskaMuxTrack(
            source=encoded_mkv,
            source_track=rewritten_track,
            output_number=1,
            output_uid=video.uid or 1,
            patch_language=False,
            patch_name=False,
            patch_flags=False,
        )

        frames = {"count": 0}

        def _packets() -> Iterator[MatroskaMuxPacket]:
            """Lockstep blocs encodés ↔ AUs injectés (mémoire bornée)."""
            au_iter = iter_hevc_access_units(
                injected_hevc, self._chunk_size, cancel_cb=cancel_cb,
            )
            sequence = 0
            for block in reader.blocks():
                if block.track_number != video.number:
                    continue
                if block.lace_count > 1 or block.lacing_mode:
                    raise HevcPayloadAlignmentError(
                        "Blocs lacés non supportés par la réécriture lockstep "
                        f"(bloc #{sequence + 1})."
                    )
                access_unit = next(au_iter, None)
                if access_unit is None:
                    raise HevcPayloadAlignmentError(
                        "Désalignement strict : plus de blocs encodés que "
                        f"d'access units injectés (bloc #{sequence + 1})."
                    )
                rewritten_payload = _au_to_block_payload(access_unit, length_size)
                if not rewritten_payload:
                    # Un bloc vide dans la sortie livrée violerait le contrat
                    # Matroska attendu des lecteurs : échec strict avant commit.
                    raise HevcPayloadAlignmentError(
                        "Access unit injecté sans payload utilisable "
                        f"(bloc #{sequence + 1})."
                    )
                rewritten_block = block.__class__(**{
                    **block.__dict__,
                    "payload": rewritten_payload,
                })
                frames["count"] += 1
                yield MatroskaMuxPacket(1, rewritten_block, sequence)
                sequence += 1
            if next(au_iter, None) is not None:
                raise HevcPayloadAlignmentError(
                    "Désalignement strict : plus d'access units injectés que "
                    f"de blocs encodés ({sequence} bloc(s))."
                )

        plan = MatroskaMuxPlan(
            output=output,
            tracks=(mux_track,),
            packets=_packets(),
            timestamp_scale_ns=reader.timestamp_scale_ns(),
            muxing_app="Muxiveo payload rewriter",
            writing_app="Muxiveo",
        )
        MatroskaWriter().write(
            plan,
            external_validator=external_validator,
            cancel_cb=cancel_cb,
            progress_cb=progress_cb,
        )
        return HevcPayloadRewriteResult(
            output=output,
            frames_rewritten=frames["count"],
            codec_private_updated=updated_codec_private is not None,
            dovi_mapping_written=dovi_mapping is not None,
        )


__all__ = [
    "HevcPayloadAlignmentError",
    "HevcPayloadRewriteResult",
    "MatroskaHevcPayloadRewriter",
]
