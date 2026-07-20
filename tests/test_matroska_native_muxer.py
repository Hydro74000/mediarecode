"""Tests du muxer Matroska natif Python."""

from __future__ import annotations

import json
import shutil
import subprocess
from unittest.mock import patch

import pytest

from core.matroska.editors.dovi import DolbyVisionConfigRecord
from core.matroska.ids import (
    BLOCK_GROUP_ID,
    CLUSTER_ID,
    EBML_HEADER_ID,
    SEGMENT_ID,
    SIMPLE_BLOCK_ID,
    TIMESTAMP_ID,
    TRACKS_ID,
)
from core.matroska.native_muxer import MatroskaNativeMuxer
from core.matroska.reader import MatroskaReader, payload_children


# ---------------------------------------------------------------------------
# Helpers : construction d'un HEVC annexB factice
# ---------------------------------------------------------------------------


def _nal(nal_type: int, *, first_slice: bool = False, extra: bytes = b"") -> bytes:
    header_byte_0 = (nal_type & 0x3F) << 1
    header_byte_1 = 0x01
    rbsp_first = 0x80 if first_slice else 0x00
    return bytes([header_byte_0, header_byte_1, rbsp_first]) + extra


def _build_fake_hevc(*, frame_count: int = 5, vps_sps_pps: bool = True) -> bytes:
    """
    Construit un flux HEVC factice : 1 IDR + (frame_count-1) TRAIL_R.
    Chaque AU précédé éventuellement de VPS/SPS/PPS.
    """
    out = b""

    def _put(nal: bytes) -> None:
        nonlocal out
        out += b"\x00\x00\x00\x01" + nal

    if vps_sps_pps:
        _put(_nal(32, extra=b"\x40\x01"))   # VPS
        _put(_nal(33, extra=b"\x42\x01"))   # SPS
        _put(_nal(34, extra=b"\x44\x01"))   # PPS

    # 1ère frame : IDR_W_RADL (type 19)
    _put(_nal(19, first_slice=True, extra=b"\x00" * 32))
    for i in range(1, frame_count):
        # TRAIL_R (type 1)
        _put(_nal(1, first_slice=True, extra=bytes([i & 0xFF]) * 16))
    return out


def _make_packets_json(pts_list_s: list[float]) -> str:
    return json.dumps({
        "packets": [{"pts_time": f"{t:.6f}"} for t in pts_list_s]
    })


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMatroskaNativeMuxerSmoke:
    def test_produces_valid_ebml_header_and_segment(self, tmp_path):
        hevc = tmp_path / "in.hevc"
        hevc.write_bytes(_build_fake_hevc(frame_count=3))
        out = tmp_path / "out.mkv"

        with patch(
            "core.matroska.timestamps.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=_make_packets_json([0.0, 0.041666, 0.083333]),
                stderr="",
            ),
        ):
            result = MatroskaNativeMuxer().mux(
                hevc_input=hevc,
                source_for_timestamps=tmp_path / "fake-source.mkv",
                output=out,
                pixel_width=1920,
                pixel_height=1080,
            )

        assert result.frames_written == 3
        assert result.cluster_count >= 1
        assert out.stat().st_size > 100

        data = out.read_bytes()
        # EBML header en tête
        assert data.startswith(EBML_HEADER_ID)
        # Segment présent
        assert SEGMENT_ID in data
        # Tracks présent
        assert TRACKS_ID in data
        # Au moins 1 cluster + SimpleBlock
        assert CLUSTER_ID in data
        assert SIMPLE_BLOCK_ID in data
        # Cluster.Timestamp présent
        assert TIMESTAMP_ID in data

    def test_dovi_block_addition_mapping_present_when_record_provided(self, tmp_path):
        hevc = tmp_path / "in.hevc"
        hevc.write_bytes(_build_fake_hevc(frame_count=2))
        out = tmp_path / "out.mkv"

        record = DolbyVisionConfigRecord(
            profile=8, level=6,
            rpu_present=True, el_present=False, bl_present=True,
            bl_signal_compat_id=1,
        )
        with patch(
            "core.matroska.timestamps.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=_make_packets_json([0.0, 0.041666]),
                stderr="",
            ),
        ):
            MatroskaNativeMuxer().mux(
                hevc_input=hevc,
                source_for_timestamps=tmp_path / "src.mkv",
                output=out,
                pixel_width=3840,
                pixel_height=2160,
                dovi_record=record,
            )

        data = out.read_bytes()
        # FourCC dvcC = 0x64766343 doit apparaître
        assert b"\x64\x76\x63\x43" in data
        # BlockAdditionMapping ID
        assert b"\x41\xe4" in data

    def test_blocks_are_hvcc_length_prefixed_simpleblocks(self, tmp_path):
        """MKV-S4 : NAL length-prefixed (toutes conservées), SimpleBlocks,
        vrai bit keyframe — plus de BlockGroups tous vus keyframes."""
        hevc = tmp_path / "in.hevc"
        hevc.write_bytes(_build_fake_hevc(frame_count=3))
        out = tmp_path / "out.mkv"

        with patch(
            "core.matroska.timestamps.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=_make_packets_json([0.0, 0.041666, 0.083333]),
                stderr="",
            ),
        ):
            MatroskaNativeMuxer().mux(
                hevc_input=hevc,
                source_for_timestamps=tmp_path / "src.mkv",
                output=out,
                pixel_width=1920,
                pixel_height=1080,
            )

        reader = MatroskaReader(out)
        track = reader.tracks()[0]
        # lengthSizeMinusOne du hvcC = 3 → longueurs NAL sur 4 octets.
        length_size = (track.codec_private[21] & 0x03) + 1
        assert length_size == 4

        blocks = list(reader.blocks())
        # Seule la frame IDR porte le bit keyframe.
        assert [bool(block.flags & 0x80) for block in blocks] == [True, False, False]
        # AU 1 : VPS/SPS/PPS + IDR conservés dans l'ordre, préfixés longueur.
        first_au_nals = [
            _nal(32, extra=b"\x40\x01"),
            _nal(33, extra=b"\x42\x01"),
            _nal(34, extra=b"\x44\x01"),
            _nal(19, first_slice=True, extra=b"\x00" * 32),
        ]
        assert blocks[0].payload == b"".join(
            len(nal).to_bytes(length_size, "big") + nal for nal in first_au_nals
        )
        trail = _nal(1, first_slice=True, extra=bytes([1]) * 16)
        assert blocks[1].payload == len(trail).to_bytes(length_size, "big") + trail
        # SimpleBlocks uniquement : aucun BlockGroup dans les Clusters.
        for item in reader.top_level():
            if item.element_id == CLUSTER_ID:
                child_ids = [child_id for child_id, _ in payload_children(reader.payload(item))]
                assert BLOCK_GROUP_ID not in child_ids
                assert SIMPLE_BLOCK_ID in child_ids

    def test_raises_on_frame_count_misalignment(self, tmp_path):
        # 3 frames HEVC mais seulement 2 PTS source → désaligné.
        hevc = tmp_path / "in.hevc"
        hevc.write_bytes(_build_fake_hevc(frame_count=3))
        out = tmp_path / "out.mkv"

        with patch(
            "core.matroska.timestamps.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=_make_packets_json([0.0, 0.041666]),
                stderr="",
            ),
        ):
            with pytest.raises(RuntimeError, match="Désalignement"):
                MatroskaNativeMuxer().mux(
                    hevc_input=hevc,
                    source_for_timestamps=tmp_path / "src.mkv",
                    output=out,
                    pixel_width=1920,
                    pixel_height=1080,
                )

    def test_vfr_timestamps_preserved_in_clusters(self, tmp_path):
        # Source VFR : intervalles non uniformes.
        hevc = tmp_path / "in.hevc"
        hevc.write_bytes(_build_fake_hevc(frame_count=4))
        out = tmp_path / "out.mkv"

        # PTS : 0, 40 ms, 100 ms (gap VFR), 140 ms
        with patch(
            "core.matroska.timestamps.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=_make_packets_json([0.0, 0.040, 0.100, 0.140]),
                stderr="",
            ),
        ):
            result = MatroskaNativeMuxer().mux(
                hevc_input=hevc,
                source_for_timestamps=tmp_path / "src.mkv",
                output=out,
                pixel_width=1920,
                pixel_height=1080,
            )

        # Durée totale = pts[-1] + duration[-1]. Pour 4 frames avec intervalles
        # 40, 60, 40, et duration[-1]=40 (dupliqué), total = 140 + 40 = 180 ms.
        assert result.duration_ms == 180


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe non disponible")
class TestMatroskaNativeMuxerFfprobeRoundTrip:
    def test_ffprobe_can_parse_output(self, tmp_path):
        hevc = tmp_path / "in.hevc"
        hevc.write_bytes(_build_fake_hevc(frame_count=5))
        out = tmp_path / "out.mkv"

        with patch(
            "core.matroska.timestamps.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=_make_packets_json([i * 0.041666 for i in range(5)]),
                stderr="",
            ),
        ):
            MatroskaNativeMuxer().mux(
                hevc_input=hevc,
                source_for_timestamps=tmp_path / "src.mkv",
                output=out,
                pixel_width=1920,
                pixel_height=1080,
            )

        # ffprobe doit pouvoir lire le conteneur (codec inconnu OK, on
        # vérifie juste la structure EBML).
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=format_name", "-of", "default=noprint_wrappers=1:nokey=1",
             str(out)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "matroska" in result.stdout.lower()
