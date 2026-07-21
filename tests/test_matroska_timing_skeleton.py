"""Squelette de timing Matroska (pic disque 2× du pipeline injection HDR).

Couverture :
- parité des métadonnées de blocs (PTS, durées, keyframes, références,
  discard padding) avec payloads vidés et TrackEntry/CodecPrivate préservés ;
- réécriture depuis le squelette strictement identique (octet à octet) à la
  réécriture depuis le MKV encodé complet ;
- refus strict : multi-pistes, blocs lacés, access unit sans payload utile ;
- annulation coopérative sans fichier partiel ;
- taille du squelette négligeable devant le flux.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from core.matroska.ebml import ascii_element, binary_element, uint_element
from core.matroska.hevc.access_units import split_into_access_units
from core.matroska.hevc.payload_rewriter import (
    HevcPayloadAlignmentError,
    MatroskaHevcPayloadRewriter,
)
from core.matroska.hevc.timing_skeleton import (
    TimingSkeletonError,
    write_timing_skeleton,
)
from core.matroska.ids import (
    CODEC_ID_ID, CODEC_PRIVATE_ID, TRACK_NUMBER_ID, TRACK_TYPE_ID, TRACK_UID_ID,
)
from core.matroska.mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack
from core.matroska.reader import MatroskaBlock, MatroskaReader, MatroskaTrack
from core.matroska.writer import MatroskaWriteCancelled, MatroskaWriter


def _aud_nal() -> bytes:
    return bytes([35 << 1, 0x01, 0x50])


def _idr_nal(payload_size: int = 24) -> bytes:
    return bytes([19 << 1, 0x01, 0x80]) + bytes(payload_size)


def _annexb_stream(au_count: int, payload_size: int = 24) -> bytes:
    code = b"\x00\x00\x00\x01"
    parts: list[bytes] = []
    for _ in range(au_count):
        parts.append(code + _aud_nal())
        parts.append(code + _idr_nal(payload_size))
    return b"".join(parts)


def _hevc_track(number: int = 1, *, codec_private: bytes = b"") -> MatroskaTrack:
    raw_entry = b"".join((
        uint_element(TRACK_NUMBER_ID, number),
        uint_element(TRACK_UID_ID, number),
        uint_element(TRACK_TYPE_ID, 1),
        ascii_element(CODEC_ID_ID, "V_MPEGH/ISO/HEVC"),
        binary_element(CODEC_PRIVATE_ID, codec_private) if codec_private else b"",
    ))
    return MatroskaTrack(
        number=number, uid=number, track_type=1, codec_id="V_MPEGH/ISO/HEVC",
        codec_private=codec_private, language_bcp47="", language="und", name="",
        raw_entry=raw_entry,
    )


def _write_encoded_mkv(
    path: Path,
    blocks: list[MatroskaBlock],
    *,
    track: MatroskaTrack | None = None,
) -> Path:
    source_track = track if track is not None else _hevc_track()
    packets = tuple(
        MatroskaMuxPacket(source_track.number, block, index)
        for index, block in enumerate(blocks)
    )
    MatroskaWriter().write(MatroskaMuxPlan(
        output=path,
        tracks=(MatroskaMuxTrack(
            source=path, source_track=source_track,
            output_number=source_track.number, output_uid=source_track.uid,
        ),),
        packets=packets,
    ))
    return path


def _varied_blocks(payloads: list[bytes]) -> list[MatroskaBlock]:
    """Blocs variés : SimpleBlock keyframe, BlockGroup avec durée/référence."""
    blocks: list[MatroskaBlock] = []
    for index, payload in enumerate(payloads):
        if index % 2 == 0:
            blocks.append(MatroskaBlock(
                track_number=1, timestamp_ms=index * 40, flags=0x80,
                payload=payload,
            ))
        else:
            blocks.append(MatroskaBlock(
                track_number=1, timestamp_ms=index * 40, flags=0x00,
                payload=payload, duration_ms=40, references=(-40,),
                is_keyframe=False,
            ))
    return blocks


def _block_signature(block: MatroskaBlock) -> tuple[object, ...]:
    return (
        block.track_number, block.timestamp_ms, block.timestamp_ns,
        block.duration_ms, block.duration_ns, block.references,
        block.references_ns, block.discard_padding_ns, block.is_keyframe,
        block.flags,
    )


# =============================================================================
# Parité des métadonnées et du TrackEntry
# =============================================================================

class TestTimingSkeletonParity:

    def test_preserves_block_metadata_and_empties_payloads(self, tmp_path: Path) -> None:
        payloads = [bytes([index]) * 5_000 for index in range(6)]
        encoded = _write_encoded_mkv(tmp_path / "encoded.mkv", _varied_blocks(payloads))
        skeleton = tmp_path / "timing.mkv"
        result = write_timing_skeleton(encoded, skeleton)
        assert result.blocks_written == 6

        original_blocks = list(MatroskaReader(encoded).blocks())
        skeleton_blocks = list(MatroskaReader(skeleton).blocks())
        assert len(skeleton_blocks) == len(original_blocks) == 6
        assert [_block_signature(b) for b in skeleton_blocks] == [
            _block_signature(b) for b in original_blocks
        ]
        assert all(block.payload == b"" for block in skeleton_blocks)
        assert any(block.payload for block in original_blocks)

    def test_preserves_track_entry_and_timestamp_scale(self, tmp_path: Path) -> None:
        codec_private = bytes([1]) + bytes(20) + bytes([0x03]) + bytes([0])
        track = _hevc_track(codec_private=codec_private)
        encoded = _write_encoded_mkv(
            tmp_path / "encoded.mkv",
            _varied_blocks([b"\x01" * 256, b"\x02" * 256]),
            track=track,
        )
        skeleton = tmp_path / "timing.mkv"
        write_timing_skeleton(encoded, skeleton)

        original_track = MatroskaReader(encoded).tracks()[0]
        skeleton_track = MatroskaReader(skeleton).tracks()[0]
        assert skeleton_track.number == original_track.number
        assert skeleton_track.uid == original_track.uid
        assert skeleton_track.track_type == original_track.track_type
        assert skeleton_track.codec_id == original_track.codec_id
        assert skeleton_track.codec_private == codec_private
        assert (
            MatroskaReader(skeleton).timestamp_scale_ns()
            == MatroskaReader(encoded).timestamp_scale_ns()
        )

    def test_skeleton_is_negligible_next_to_stream(self, tmp_path: Path) -> None:
        payloads = [bytes(1024 * 256) for _ in range(24)]
        encoded = _write_encoded_mkv(
            tmp_path / "encoded.mkv",
            _varied_blocks(payloads),
        )
        skeleton = tmp_path / "timing.mkv"
        write_timing_skeleton(encoded, skeleton)
        assert skeleton.stat().st_size < encoded.stat().st_size / 20


# =============================================================================
# Réécriture depuis le squelette == réécriture depuis le MKV complet
# =============================================================================

class TestRewriteFromSkeleton:

    def test_rewritten_output_is_byte_identical(self, tmp_path: Path) -> None:
        stream = _annexb_stream(5, payload_size=333)
        injected = tmp_path / "injected.hevc"
        injected.write_bytes(stream)
        access_units = split_into_access_units(stream)
        encoded = _write_encoded_mkv(
            tmp_path / "encoded.mkv",
            _varied_blocks([au.payload for au in access_units]),
        )
        skeleton = tmp_path / "timing.mkv"
        write_timing_skeleton(encoded, skeleton)

        from_full = tmp_path / "from_full.mkv"
        from_skeleton = tmp_path / "from_skeleton.mkv"
        result_full = MatroskaHevcPayloadRewriter(chunk_size=64 * 1024).rewrite(
            encoded_mkv=encoded, injected_hevc=injected, output=from_full,
        )
        result_skeleton = MatroskaHevcPayloadRewriter(chunk_size=64 * 1024).rewrite(
            encoded_mkv=skeleton, injected_hevc=injected, output=from_skeleton,
        )
        assert result_full.frames_rewritten == result_skeleton.frames_rewritten == 5
        assert from_full.read_bytes() == from_skeleton.read_bytes()


# =============================================================================
# Refus stricts et annulation
# =============================================================================

class TestTimingSkeletonStrictness:

    def test_rejects_multi_track_input(self, tmp_path: Path) -> None:
        video = _hevc_track(1)
        audio = MatroskaTrack(
            number=2, uid=2, track_type=2, codec_id="A_AAC",
            codec_private=b"", language_bcp47="", language="und", name="",
            raw_entry=b"".join((
                uint_element(TRACK_NUMBER_ID, 2),
                uint_element(TRACK_UID_ID, 2),
                uint_element(TRACK_TYPE_ID, 2),
                ascii_element(CODEC_ID_ID, "A_AAC"),
            )),
        )
        path = tmp_path / "multi.mkv"
        MatroskaWriter().write(MatroskaMuxPlan(
            output=path,
            tracks=(
                MatroskaMuxTrack(source=path, source_track=video, output_number=1, output_uid=1),
                MatroskaMuxTrack(source=path, source_track=audio, output_number=2, output_uid=2),
            ),
            packets=(
                MatroskaMuxPacket(1, MatroskaBlock(
                    track_number=1, timestamp_ms=0, flags=0x80, payload=b"\x00" * 16,
                ), 0),
                MatroskaMuxPacket(2, MatroskaBlock(
                    track_number=2, timestamp_ms=0, flags=0x80, payload=b"\x00" * 16,
                ), 1),
            ),
        ))
        with pytest.raises(TimingSkeletonError, match="mono-piste"):
            write_timing_skeleton(path, tmp_path / "timing.mkv")

    def test_rejects_laced_blocks(self, tmp_path: Path, monkeypatch) -> None:
        encoded = _write_encoded_mkv(
            tmp_path / "encoded.mkv",
            _varied_blocks([b"\x01" * 64, b"\x02" * 64]),
        )
        original_blocks = MatroskaReader.blocks

        def _laced_blocks(self):
            for block in original_blocks(self):
                yield dataclasses.replace(block, lace_count=2)

        monkeypatch.setattr(MatroskaReader, "blocks", _laced_blocks)
        with pytest.raises(TimingSkeletonError, match="lacés"):
            write_timing_skeleton(encoded, tmp_path / "timing.mkv")
        assert not list(tmp_path.rglob("*.partial"))

    def test_cancellation_leaves_no_partial(self, tmp_path: Path) -> None:
        encoded = _write_encoded_mkv(
            tmp_path / "encoded.mkv",
            _varied_blocks([bytes(4096) for _ in range(16)]),
        )
        with pytest.raises(MatroskaWriteCancelled):
            write_timing_skeleton(
                encoded, tmp_path / "timing.mkv", cancel_cb=lambda: True,
            )
        assert not (tmp_path / "timing.mkv").exists()
        assert not list(tmp_path.rglob("*.partial"))


class TestRewriterStrictGuards:

    def test_laced_encoded_block_fails_strictly(self, tmp_path: Path, monkeypatch) -> None:
        stream = _annexb_stream(2, payload_size=16)
        injected = tmp_path / "injected.hevc"
        injected.write_bytes(stream)
        access_units = split_into_access_units(stream)
        encoded = _write_encoded_mkv(
            tmp_path / "encoded.mkv",
            _varied_blocks([au.payload for au in access_units]),
        )
        original_blocks = MatroskaReader.blocks

        def _laced_blocks(self):
            for block in original_blocks(self):
                yield dataclasses.replace(block, lace_count=2)

        monkeypatch.setattr(MatroskaReader, "blocks", _laced_blocks)
        with pytest.raises(HevcPayloadAlignmentError, match="lacés"):
            MatroskaHevcPayloadRewriter(chunk_size=64 * 1024).rewrite(
                encoded_mkv=encoded, injected_hevc=injected,
                output=tmp_path / "rewritten.mkv",
            )
        assert not list(tmp_path.rglob("*.partial"))

    def test_parameter_set_only_access_unit_fails_strictly(self, tmp_path: Path) -> None:
        # hvcC (length_size=4) : les parameter sets sont exclus du payload de
        # bloc — un AU qui n'en contient que produirait un bloc vide interdit.
        codec_private = bytes([1]) + bytes(20) + bytes([0x03]) + bytes([0])
        track = _hevc_track(codec_private=codec_private)
        encoded = _write_encoded_mkv(
            tmp_path / "encoded.mkv",
            [MatroskaBlock(track_number=1, timestamp_ms=0, flags=0x80, payload=b"\x00" * 32)],
            track=track,
        )
        vps = bytes([32 << 1, 0x01]) + bytes(6)
        sps = bytes([33 << 1, 0x01]) + bytes(6)
        pps = bytes([34 << 1, 0x01]) + bytes(6)
        injected = tmp_path / "paramsets.hevc"
        injected.write_bytes(b"".join(
            b"\x00\x00\x00\x01" + nal for nal in (vps, sps, pps)
        ))
        with pytest.raises(HevcPayloadAlignmentError, match="payload utilisable"):
            MatroskaHevcPayloadRewriter(chunk_size=64 * 1024).rewrite(
                encoded_mkv=encoded, injected_hevc=injected,
                output=tmp_path / "rewritten.mkv",
            )
        assert not list(tmp_path.rglob("*.partial"))
