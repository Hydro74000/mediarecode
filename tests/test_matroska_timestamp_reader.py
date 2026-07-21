"""Tests du lecteur de timestamps source via ffprobe."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from core.matroska.timestamps import (
    MatroskaTimestampReader,
)


def _completed(stdout: str, returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class TestMatroskaTimestampReader:
    def test_reads_cfr_packets(self, tmp_path):
        reader = MatroskaTimestampReader()
        packets_payload = json.dumps({
            "packets": [
                {"pts_time": "0.000000", "duration_time": "0.041666"},
                {"pts_time": "0.041666", "duration_time": "0.041666"},
                {"pts_time": "0.083333", "duration_time": "0.041666"},
            ]
        })
        with patch(
            "core.matroska.timestamps.subprocess.run",
            return_value=_completed(packets_payload),
        ):
            seq = reader.read(tmp_path / "src.mkv")
        assert seq.pts_ms == (0, 42, 83)
        # Durées calculées depuis deltas + duplication de la dernière.
        assert seq.durations_ms == (42, 41, 41)

    def test_reads_vfr_packets(self, tmp_path):
        reader = MatroskaTimestampReader()
        packets_payload = json.dumps({
            "packets": [
                {"pts_time": "0.000000"},
                {"pts_time": "0.040000"},
                {"pts_time": "0.100000"},  # gap VFR
                {"pts_time": "0.140000"},
            ]
        })
        with patch(
            "core.matroska.timestamps.subprocess.run",
            return_value=_completed(packets_payload),
        ):
            seq = reader.read(tmp_path / "src.mkv")
        assert seq.pts_ms == (0, 40, 100, 140)
        assert seq.durations_ms == (40, 60, 40, 40)

    def test_total_duration(self, tmp_path):
        reader = MatroskaTimestampReader()
        packets_payload = json.dumps({
            "packets": [{"pts_time": "0.0"}, {"pts_time": "1.0"}, {"pts_time": "2.0"}]
        })
        with patch(
            "core.matroska.timestamps.subprocess.run",
            return_value=_completed(packets_payload),
        ):
            seq = reader.read(tmp_path / "src.mkv")
        # 3 frames à 1 s d'intervalle → durée totale = 2000 + duration[-1]=1000 = 3000
        assert seq.total_duration_ms == 3000

    def test_sorts_packets_by_pts(self, tmp_path):
        reader = MatroskaTimestampReader()
        # B-frames : ordre DTS != ordre PTS.
        packets_payload = json.dumps({
            "packets": [
                {"pts_time": "0.0"},
                {"pts_time": "0.083"},
                {"pts_time": "0.041"},
            ]
        })
        with patch(
            "core.matroska.timestamps.subprocess.run",
            return_value=_completed(packets_payload),
        ):
            seq = reader.read(tmp_path / "src.mkv")
        assert list(seq.pts_ms) == sorted(seq.pts_ms)

    def test_can_preserve_packet_order_for_b_frames(self, tmp_path):
        reader = MatroskaTimestampReader()
        packets_payload = json.dumps({
            "packets": [
                {"pts_time": "0.000"},
                {"pts_time": "0.083"},
                {"pts_time": "0.041"},
            ]
        })
        with patch(
            "core.matroska.timestamps.subprocess.run",
            return_value=_completed(packets_payload),
        ):
            seq = reader.read(tmp_path / "src.mkv", sort_by_pts=False)

        assert seq.pts_ms == (0, 83, 41)
        assert seq.total_duration_ms == 125

    def test_skips_na_pts(self, tmp_path):
        reader = MatroskaTimestampReader()
        packets_payload = json.dumps({
            "packets": [
                {"pts_time": "N/A"},
                {"pts_time": "0.0"},
                {"pts_time": "0.041"},
            ]
        })
        with patch(
            "core.matroska.timestamps.subprocess.run",
            return_value=_completed(packets_payload),
        ):
            seq = reader.read(tmp_path / "src.mkv")
        assert seq.pts_ms == (0, 41)

    def test_raises_when_no_packets(self, tmp_path):
        reader = MatroskaTimestampReader()
        with patch(
            "core.matroska.timestamps.subprocess.run",
            return_value=_completed(json.dumps({"packets": []})),
        ):
            with pytest.raises(RuntimeError, match="Aucun packet"):
                reader.read(tmp_path / "src.mkv")

    def test_raises_when_ffprobe_missing(self, tmp_path):
        reader = MatroskaTimestampReader()
        with patch(
            "core.matroska.timestamps.subprocess.run",
            side_effect=FileNotFoundError("ffprobe"),
        ):
            with pytest.raises(RuntimeError, match="indisponible"):
                reader.read(tmp_path / "src.mkv")
