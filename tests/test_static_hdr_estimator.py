from __future__ import annotations

import struct
from pathlib import Path

import pytest

from core.workflows.encode.runtime.static_hdr_estimator import (
    _FrameLuminance,
    StaticHdrEstimateService,
)


def test_pq_code_value_to_nits_maps_peak_close_to_10000():
    assert StaticHdrEstimateService.pq_code_value_to_nits(940) == pytest.approx(10000, rel=0.01)


def test_signalstats_output_estimates_maxcll_and_master_bucket():
    output = "\n".join(
        [
            "lavfi.signalstats.YMAX=758",
            "lavfi.signalstats.YAVG=580",
        ]
        * 30
    )

    estimate = StaticHdrEstimateService.estimate_from_signalstats_output(output)

    max_cll, max_fall = [int(part) for part in estimate.max_cll.split(",", 1)]
    assert max_cll >= max_fall
    assert estimate.confidence == "medium"
    assert estimate.sample_count == 30
    assert estimate.source == "estimated_p5_to_p8"
    assert estimate.mode == "fast"
    assert "WP(15635,16450)" in estimate.master_display


def test_signalstats_output_ignores_near_black_samples():
    lines: list[str] = []
    for index in range(60):
        is_dark_sample = index < 30
        lines.extend([
            f"frame:{index} pts:{index}",
            "lavfi.signalstats.YBITDEPTH=10",
            f"lavfi.signalstats.YMAX={64 if is_dark_sample else 700}",
            f"lavfi.signalstats.YAVG={64 if is_dark_sample else 500}",
        ])
    output = "\n".join(lines)

    estimate = StaticHdrEstimateService.estimate_from_signalstats_output(output)

    assert estimate.sample_count == 30
    assert estimate.confidence == "medium"
    assert any("quasi noir" in warning for warning in estimate.warnings)


def test_master_display_uses_expected_luminance_buckets():
    assert "L(10000000,1)" in StaticHdrEstimateService.master_display_for_peak(900)
    assert "L(40000000,1)" in StaticHdrEstimateService.master_display_for_peak(2500)
    assert "L(100000000,1)" in StaticHdrEstimateService.master_display_for_peak(6000)


def test_analysis_cmd_samples_middle_eighty_percent():
    service = StaticHdrEstimateService(ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool")

    cmd = service._analysis_cmd(Path("converted.hevc"), duration_s=7200.0)

    vf = cmd[cmd.index("-vf") + 1]
    assert "trim=start=720.000:end=6480.000" in vf
    assert "fps=fps=0.01666667" in vf


def test_precise_cmds_include_cropdetect_zscale_and_middle_window():
    service = StaticHdrEstimateService(ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool")

    discovery = service._discovery_cmd(Path("converted.hevc"), duration_s=7200.0)
    precise = service._linear_frames_cmd(
        Path("converted.hevc"),
        duration_s=7200.0,
        target_samples=128,
        active_crop="1920:800:0:100",
    )

    discovery_vf = discovery[discovery.index("-vf") + 1]
    precise_vf = precise[precise.index("-vf") + 1]
    assert "trim=start=720.000:end=6480.000" in discovery_vf
    assert "cropdetect" in discovery_vf
    assert "trim=start=720.000:end=6480.000" in precise_vf
    assert "crop=1920:800:0:100" in precise_vf
    assert "zscale=" in precise_vf
    assert "format=grayf32le" in precise_vf
    assert precise[precise.index("-pix_fmt") + 1] == "grayf32le"


def test_linear_frame_bytes_feed_precise_maxcll_and_maxfall():
    raw = struct.pack("<4f", 0.01, 0.10, 0.25, 0.40)
    frame = StaticHdrEstimateService._linear_frame_luminance_from_bytes(raw)

    estimate = StaticHdrEstimateService._estimate_from_linear_luminance(
        [frame] * 64,
        guardrail_peak_nits=0,
        active_crop="",
    )

    assert estimate.mode == "precise"
    assert estimate.analysis_method == "linear_grayf32le"
    assert estimate.max_cll == "4000,1900"
    assert estimate.active_sample_count == 64


def test_precise_guardrail_keeps_maxcll_above_downscaled_peak():
    estimate = StaticHdrEstimateService._estimate_from_linear_luminance(
        [_FrameLuminance(peak_nits=500, average_nits=100)] * 64,
        guardrail_peak_nits=1350,
        active_crop="",
    )

    max_cll, max_fall = [int(part) for part in estimate.max_cll.split(",", 1)]
    assert max_cll == 1400
    assert max_fall == 100
    assert any("garde-fou" in warning for warning in estimate.warnings)


def test_active_crop_requires_stable_candidate():
    stable = "\n".join(
        [
            "frame:0 pts:0 pts_time:0",
            "lavfi.cropdetect.w=1920",
            "lavfi.cropdetect.h=800",
            "lavfi.cropdetect.x=0",
            "lavfi.cropdetect.y=100",
        ]
        * 8
    )
    unstable = "\n".join(
        f"crop=1920:800:{index}:100"
        for index in range(8)
    )

    assert StaticHdrEstimateService._select_active_crop(stable) == "1920:800:0:100"
    assert StaticHdrEstimateService._select_active_crop(unstable) == ""


def test_cache_key_is_separate_for_fast_and_precise(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"source")
    service = StaticHdrEstimateService(ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool")

    fast = service._cache_key(source, 0, 7200.0, "fast")
    precise = service._cache_key(source, 0, 7200.0, "precise")

    assert fast != precise


def test_precise_falls_back_to_fast_when_support_missing(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"source")

    class FakeEstimator(StaticHdrEstimateService):
        def _precise_analysis_supported(self):  # noqa: ANN201
            return False

        def _run_capture(self, cmd, *, cancel_cb=None):  # noqa: ANN001
            if "signalstats" in " ".join(cmd):
                return "\n".join(
                    [
                        "lavfi.signalstats.YMAX=700",
                        "lavfi.signalstats.YAVG=500",
                    ]
                    * 80
                )
            return ""

    service = FakeEstimator(ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool")

    estimate = service.estimate_p5_to_p8_static_hdr(
        source,
        stream_index=3,
        duration_s=7200.0,
        work_dir=tmp_path,
        mode="precise",
    )

    assert estimate.mode == "fast"
    assert estimate.analysis_method == "signalstats_fallback"
    assert any("mode rapide" in warning for warning in estimate.warnings)


def test_precise_runs_discovery_and_linear_passes(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"source")
    calls: list[list[str]] = []
    linear_calls: list[list[str]] = []

    class FakeEstimator(StaticHdrEstimateService):
        def _precise_analysis_supported(self):  # noqa: ANN201
            return True

        def _run_capture(self, cmd, *, cancel_cb=None):  # noqa: ANN001
            calls.append(list(cmd))
            if "cropdetect" in " ".join(cmd):
                lines: list[str] = []
                for index in range(160):
                    lines.extend([
                        f"frame:{index} pts:{index} pts_time:{720 + index * 36}",
                        "lavfi.signalstats.YBITDEPTH=10",
                        f"lavfi.signalstats.YMAX={700 + (index % 5)}",
                        "lavfi.signalstats.YAVG=500",
                        "lavfi.cropdetect.w=1920",
                        "lavfi.cropdetect.h=800",
                        "lavfi.cropdetect.x=0",
                        "lavfi.cropdetect.y=100",
                    ])
                return "\n".join(lines)
            return ""

        def _analyze_linear_frames(self, cmd, *, width, height, cancel_cb=None):  # noqa: ANN001
            linear_calls.append(list(cmd))
            return [_FrameLuminance(peak_nits=900, average_nits=250)] * 80

    service = FakeEstimator(ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool")

    estimate = service.estimate_p5_to_p8_static_hdr(
        source,
        stream_index=3,
        duration_s=7200.0,
        work_dir=tmp_path,
        mode="precise",
    )

    assert calls[0][0] == "ffmpeg"
    assert calls[0][calls[0].index("-map") + 1] == "0:3"
    assert calls[1][:4] == ["dovi_tool", "-m", "3", "convert"]
    assert "cropdetect" in " ".join(calls[2])
    assert len(linear_calls) == 2
    assert all("zscale" in " ".join(cmd) for cmd in linear_calls)
    assert any("crop=1920:800:0:100" in " ".join(cmd) for cmd in linear_calls)
    assert estimate.mode == "precise"
    assert estimate.confidence == "high"
    assert estimate.active_crop == "1920:800:0:100"
    assert not any(path.name.startswith("static_hdr_p5_") for path in tmp_path.iterdir())


def test_fast_estimate_runs_extract_convert_analyze_and_cleans_temp(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"source")
    calls: list[list[str]] = []

    class FakeEstimator(StaticHdrEstimateService):
        def _run_capture(self, cmd, *, cancel_cb=None):  # noqa: ANN001
            calls.append(list(cmd))
            if "signalstats" in " ".join(cmd):
                return "\n".join(
                    [
                        "lavfi.signalstats.YMAX=700",
                        "lavfi.signalstats.YAVG=500",
                    ]
                    * 80
                )
            return ""

    service = FakeEstimator(ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool")

    estimate = service.estimate_p5_to_p8_static_hdr(
        source,
        stream_index=3,
        duration_s=7200.0,
        work_dir=tmp_path,
        mode="fast",
    )

    assert calls[0][0] == "ffmpeg"
    assert calls[0][calls[0].index("-map") + 1] == "0:3"
    assert calls[1][:4] == ["dovi_tool", "-m", "3", "convert"]
    assert "signalstats" in " ".join(calls[2])
    assert estimate.mode == "fast"
    assert estimate.confidence == "high"
    assert not any(path.name.startswith("static_hdr_p5_") for path in tmp_path.iterdir())
