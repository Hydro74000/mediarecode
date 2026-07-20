"""
tests/test_matroska_hevc_streaming.py — Streaming HEVC et gate mémoire (lot 3).

Couverture :
- parité itérateurs streaming vs découpage en mémoire (start codes 3/4 octets,
  frontières de chunks, zéros de bourrage) ;
- annulation coopérative et progression en octets lus ;
- gate mémoire : pic borné indépendant de la taille du flux (3.4) ;
- réécriture de payloads : timestamps dupliqués préservés, échec strict sur
  désalignement, annulation en milieu de flux sans fichier partiel.
"""

from __future__ import annotations

import tracemalloc
from pathlib import Path

import pytest

from core.matroska.ebml import ascii_element, uint_element
from core.matroska.ids import (
    CODEC_ID_ID, TRACK_NUMBER_ID, TRACK_TYPE_ID, TRACK_UID_ID,
)
from core.matroska.hevc.access_units import (
    HevcStreamCancelled,
    _iter_nal_units,
    iter_hevc_access_units,
    iter_hevc_nal_units,
    split_into_access_units,
)
from core.matroska.hevc.payload_rewriter import (
    HevcPayloadAlignmentError,
    MatroskaHevcPayloadRewriter,
)
from core.matroska.mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack
from core.matroska.reader import MatroskaBlock, MatroskaReader, MatroskaTrack
from core.matroska.writer import MatroskaWriter


GIB_STREAM_AU_PAYLOAD = 1024 * 1024  # 1 Mio par AU pour la gate mémoire


def _aud_nal() -> bytes:
    return bytes([35 << 1, 0x01, 0x50])


def _idr_nal(payload_size: int = 24) -> bytes:
    return bytes([19 << 1, 0x01, 0x80]) + bytes(payload_size)


def _rpu_nal(payload_size: int = 8) -> bytes:
    """NAL 62 (RPU Dolby Vision) — suffixe écrit après les slices de sa frame."""
    return bytes([62 << 1, 0x01]) + bytes(payload_size)


def _annexb_stream(au_count: int, payload_size: int = 24, *, long_codes: bool = True) -> bytes:
    code = b"\x00\x00\x00\x01" if long_codes else b"\x00\x00\x01"
    parts: list[bytes] = []
    for _ in range(au_count):
        parts.append(code + _aud_nal())
        parts.append(code + _idr_nal(payload_size))
    return b"".join(parts)


def _nal_signature(units) -> list[tuple[int, bytes]]:
    return [(nal.nal_type, nal.payload) for nal in units]


# =============================================================================
# 3.2 — Parité streaming vs découpage mémoire
# =============================================================================

class TestStreamingParity:

    @pytest.mark.parametrize("chunk_size", [1, 3, 7, 64, 4096])
    def test_nal_iterator_matches_in_memory_scanner(self, tmp_path: Path, chunk_size: int) -> None:
        stream = (
            _annexb_stream(3, payload_size=17)
            + b"\x00\x00\x01" + _idr_nal(5)          # start code court
            + b"\x00\x00\x00\x00\x00\x01" + _aud_nal()  # zéros de bourrage
        )
        path = tmp_path / "stream.hevc"
        path.write_bytes(stream)
        streamed = _nal_signature(iter_hevc_nal_units(path, chunk_size))
        in_memory = _nal_signature(_iter_nal_units(stream))
        assert streamed == in_memory
        assert streamed  # le flux de test n'est pas vide

    @pytest.mark.parametrize("chunk_size", [5, 64, 4096])
    def test_access_units_match_split(self, tmp_path: Path, chunk_size: int) -> None:
        stream = _annexb_stream(7, payload_size=33)
        path = tmp_path / "stream.hevc"
        path.write_bytes(stream)
        streamed = list(iter_hevc_access_units(path, chunk_size))
        reference = split_into_access_units(stream)
        assert len(streamed) == len(reference) == 7
        assert [au.payload for au in streamed] == [au.payload for au in reference]
        assert all(au.is_keyframe for au in streamed)

    @pytest.mark.parametrize("with_aud", [True, False])
    def test_suffix_rpu_stays_in_its_access_unit(self, tmp_path: Path, with_aud: bool) -> None:
        """Le RPU DoVi (NAL 62) suit les slices de sa frame : il doit rester
        dans l'AU courant — pas glisser dans le suivant ni créer un AU final
        fantôme (motif réel produit par dovi_tool inject-rpu)."""
        code = b"\x00\x00\x00\x01"
        parts: list[bytes] = []
        for _ in range(5):
            if with_aud:
                parts.append(code + _aud_nal())
            parts.append(code + _idr_nal(24))
            parts.append(code + _rpu_nal())
        stream = b"".join(parts)
        path = tmp_path / "rpu.hevc"
        path.write_bytes(stream)

        for access_units in (
            split_into_access_units(stream),
            list(iter_hevc_access_units(path, 16)),
        ):
            assert len(access_units) == 5
            expected = [35, 19, 62] if with_aud else [19, 62]
            for access_unit in access_units:
                assert [nal.nal_type for nal in access_unit.nal_units] == expected

    def test_cancellation_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "stream.hevc"
        path.write_bytes(_annexb_stream(4))
        with pytest.raises(HevcStreamCancelled):
            list(iter_hevc_access_units(path, 8, cancel_cb=lambda: True))

    def test_progress_reports_cumulative_bytes(self, tmp_path: Path) -> None:
        stream = _annexb_stream(4, payload_size=100)
        path = tmp_path / "stream.hevc"
        path.write_bytes(stream)
        seen: list[int] = []
        list(iter_hevc_access_units(path, 64, progress_cb=seen.append))
        assert seen
        assert seen == sorted(seen)
        assert seen[-1] == len(stream)


# =============================================================================
# Fixtures rewriter : MKV encodé synthétique (annexB dans les blocs, sans hvcC)
# =============================================================================

def _hevc_track(number: int = 1) -> MatroskaTrack:
    raw_entry = b"".join((
        uint_element(TRACK_NUMBER_ID, number),
        uint_element(TRACK_UID_ID, number),
        uint_element(TRACK_TYPE_ID, 1),
        ascii_element(CODEC_ID_ID, "V_MPEGH/ISO/HEVC"),
    ))
    return MatroskaTrack(
        number=number, uid=number, track_type=1, codec_id="V_MPEGH/ISO/HEVC",
        codec_private=b"", language_bcp47="", language="und", name="",
        raw_entry=raw_entry,
    )


def _write_encoded_mkv(
    path: Path,
    payloads: list[bytes],
    *,
    timestamps_ms: list[int] | None = None,
) -> Path:
    track = _hevc_track()
    stamps = timestamps_ms if timestamps_ms is not None else [i * 40 for i in range(len(payloads))]
    packets = tuple(
        MatroskaMuxPacket(1, MatroskaBlock(
            track_number=1, timestamp_ms=stamp, flags=0x80, payload=payload,
        ), index)
        for index, (payload, stamp) in enumerate(zip(payloads, stamps))
    )
    MatroskaWriter().write(MatroskaMuxPlan(
        output=path,
        tracks=(MatroskaMuxTrack(
            source=path, source_track=track, output_number=1, output_uid=1,
        ),),
        packets=packets,
    ))
    return path


# =============================================================================
# 3.1 — Rewriter : timestamps dupliqués, désalignement strict, annulation
# =============================================================================

class TestPayloadRewriter:

    def test_duplicate_timestamps_are_preserved(self, tmp_path: Path) -> None:
        stream = _annexb_stream(3, payload_size=9)
        injected = tmp_path / "injected.hevc"
        injected.write_bytes(stream)
        access_units = split_into_access_units(stream)
        encoded = _write_encoded_mkv(
            tmp_path / "encoded.mkv",
            [au.payload for au in access_units],
            timestamps_ms=[0, 40, 40],  # timestamps dupliqués volontaires
        )
        output = tmp_path / "rewritten.mkv"
        result = MatroskaHevcPayloadRewriter(chunk_size=64 * 1024).rewrite(
            encoded_mkv=encoded, injected_hevc=injected, output=output,
        )
        assert result.frames_rewritten == 3
        stamps = [block.timestamp_ms for block in MatroskaReader(output).blocks()]
        assert stamps == [0, 40, 40]

    def test_fewer_injected_access_units_fails_strictly(self, tmp_path: Path) -> None:
        full = split_into_access_units(_annexb_stream(4, payload_size=9))
        encoded = _write_encoded_mkv(tmp_path / "encoded.mkv", [au.payload for au in full])
        injected = tmp_path / "short.hevc"
        injected.write_bytes(_annexb_stream(3, payload_size=9))
        output = tmp_path / "rewritten.mkv"
        with pytest.raises(HevcPayloadAlignmentError, match="plus de blocs encodés"):
            MatroskaHevcPayloadRewriter(chunk_size=64 * 1024).rewrite(
                encoded_mkv=encoded, injected_hevc=injected, output=output,
            )
        assert not output.exists()
        assert not list(tmp_path.rglob("*.partial"))

    def test_extra_injected_access_units_fail_strictly(self, tmp_path: Path) -> None:
        short = split_into_access_units(_annexb_stream(2, payload_size=9))
        encoded = _write_encoded_mkv(tmp_path / "encoded.mkv", [au.payload for au in short])
        injected = tmp_path / "long.hevc"
        injected.write_bytes(_annexb_stream(5, payload_size=9))
        output = tmp_path / "rewritten.mkv"
        with pytest.raises(HevcPayloadAlignmentError, match="plus d'access units"):
            MatroskaHevcPayloadRewriter(chunk_size=64 * 1024).rewrite(
                encoded_mkv=encoded, injected_hevc=injected, output=output,
            )
        assert not output.exists()
        assert not list(tmp_path.rglob("*.partial"))

    def test_cancellation_mid_stream_leaves_no_partial(self, tmp_path: Path) -> None:
        stream = _annexb_stream(64, payload_size=64 * 1024)
        injected = tmp_path / "injected.hevc"
        injected.write_bytes(stream)
        access_units = split_into_access_units(stream)
        encoded = _write_encoded_mkv(tmp_path / "encoded.mkv", [au.payload for au in access_units])
        output = tmp_path / "rewritten.mkv"
        calls = {"count": 0}

        def _cancel_soon() -> bool:
            calls["count"] += 1
            return calls["count"] > 4

        with pytest.raises(Exception) as excinfo:
            MatroskaHevcPayloadRewriter(chunk_size=64 * 1024).rewrite(
                encoded_mkv=encoded, injected_hevc=injected, output=output,
                cancel_cb=_cancel_soon,
            )
        assert "annul" in str(excinfo.value).lower()
        assert not output.exists()
        assert not list(tmp_path.rglob("*.partial"))


# =============================================================================
# 3.4 — Gate mémoire : pic borné, indépendant de la taille du flux
# =============================================================================

class TestMemoryGate:

    def test_access_unit_iterator_memory_is_bounded(self, tmp_path: Path) -> None:
        au_count = 48  # ~48 Mio de flux
        path = tmp_path / "big.hevc"
        with path.open("wb") as handle:
            for _ in range(au_count):
                handle.write(b"\x00\x00\x00\x01" + _aud_nal())
                handle.write(b"\x00\x00\x00\x01" + _idr_nal(GIB_STREAM_AU_PAYLOAD))
        stream_size = path.stat().st_size
        assert stream_size > 40 * 1024 * 1024

        tracemalloc.start()
        count = 0
        for _access_unit in iter_hevc_access_units(path, 1024 * 1024):
            count += 1
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert count == au_count
        # Pic borné par l'AU courant + chunk + marge fixe — jamais par le flux.
        assert peak < 12 * 1024 * 1024, f"pic mémoire {peak} octets"

    def _rewrite_peak_bytes(self, tmp_path: Path, au_count: int) -> int:
        """Pic tracemalloc d'une réécriture complète de ``au_count`` Mio."""
        workdir = tmp_path / f"run_{au_count}"
        workdir.mkdir()
        injected = workdir / "big.hevc"
        payloads: list[bytes] = []
        with injected.open("wb") as handle:
            for _ in range(au_count):
                au_bytes = (
                    b"\x00\x00\x00\x01" + _aud_nal()
                    + b"\x00\x00\x00\x01" + _idr_nal(GIB_STREAM_AU_PAYLOAD)
                )
                handle.write(au_bytes)
                payloads.append(au_bytes)
        encoded = _write_encoded_mkv(workdir / "encoded.mkv", payloads)
        del payloads
        output = workdir / "rewritten.mkv"

        tracemalloc.start()
        result = MatroskaHevcPayloadRewriter(chunk_size=1024 * 1024).rewrite(
            encoded_mkv=encoded, injected_hevc=injected, output=output,
        )
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert result.frames_rewritten == au_count
        assert output.stat().st_size > au_count * GIB_STREAM_AU_PAYLOAD
        return peak

    def test_rewriter_memory_does_not_grow_with_stream_size(self, tmp_path: Path) -> None:
        # Le pic ne doit pas croître proportionnellement à la taille du HEVC :
        # un flux 4× plus grand doit rester dans une marge fixe du petit flux.
        small_peak = self._rewrite_peak_bytes(tmp_path, 16)
        large_peak = self._rewrite_peak_bytes(tmp_path, 64)
        assert large_peak < small_peak + 8 * 1024 * 1024, (
            f"pic {large_peak} octets pour 64 Mio vs {small_peak} octets pour 16 Mio"
        )
        # Garde-fou absolu : jamais le flux entier en mémoire.
        assert large_peak < 48 * 1024 * 1024, f"pic mémoire {large_peak} octets"
