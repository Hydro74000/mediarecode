"""Tests pour core/workflows/encode/runtime/nvencc.py — détection NVEncC + pipeline."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from core.workflows.encode.models import (
    QualityMode,
    VideoCropSettings,
    VideoEncodeSettings,
    VideoFilterSettings,
    VideoResizeSettings,
)
from core.workflows.encode.runtime.nvencc import (
    NVENCC_CODEC_FLAG,
    NVENCC_OUTPUT_EXT,
    NVENCC_VIDEO_CODECS,
    _parse_supported_codecs,
    build_decode_pipe_cmd,
    build_nvencc_command,
    build_nvencc_pipeline,
    build_remux_cmd,
    detect_nvencc_available,
    is_nvencc_codec,
    is_expected_nvencc_pipe_producer_exit,
    map_nvencc_video_transform_args,
    nvencc_binary_name,
    nvencc_intermediate_path,
    nvencc_requires_ffmpeg_filter_pipe,
)


class TestIsNvenccCodec:
    @pytest.mark.parametrize("codec", ["nvencc_hevc", "nvencc_h264", "nvencc_av1"])
    def test_known_codecs(self, codec):
        assert is_nvencc_codec(codec) is True

    @pytest.mark.parametrize("codec", ["hevc_nvenc", "libx265", "", None])
    def test_other_codecs(self, codec):
        assert is_nvencc_codec(codec) is False

    def test_case_insensitive(self):
        assert is_nvencc_codec("NVENCC_HEVC") is True


class TestNvenccPipeProducerExit:
    @pytest.mark.parametrize("returncode", [0, -13])
    def test_accepts_normal_and_sigpipe_exits(self, returncode):
        assert is_expected_nvencc_pipe_producer_exit(returncode, "") is True

    def test_accepts_ffmpeg_broken_pipe_after_consumer_success(self):
        assert is_expected_nvencc_pipe_producer_exit(
            1,
            "av_interleaved_write_frame(): Broken pipe",
        ) is True

    @pytest.mark.parametrize(
        ("returncode", "stderr"),
        [(1, "Invalid data found"), (2, "Broken pipe")],
    )
    def test_rejects_other_decode_failures(self, returncode, stderr):
        assert is_expected_nvencc_pipe_producer_exit(returncode, stderr) is False


class TestNvenccBinaryName:
    def test_linux(self):
        # Sur Linux, le binaire posé par les paquets .deb/.rpm rigaya est
        # ``nvencc`` (lowercase). Cf. /usr/bin/nvencc dans le RPM Fedora.
        with patch("core.workflows.encode.runtime.nvencc.sys.platform", "linux"):
            assert nvencc_binary_name() == "nvencc"

    def test_windows(self):
        with patch("core.workflows.encode.runtime.nvencc.sys.platform", "win32"):
            assert nvencc_binary_name() == "NVEncC64.exe"


class TestCodecMappings:
    def test_codec_flag_covers_all_codecs(self):
        assert set(NVENCC_CODEC_FLAG) == NVENCC_VIDEO_CODECS

    def test_codec_flag_values(self):
        assert NVENCC_CODEC_FLAG["nvencc_hevc"] == "hevc"
        assert NVENCC_CODEC_FLAG["nvencc_h264"] == "h264"
        assert NVENCC_CODEC_FLAG["nvencc_av1"] == "av1"

    def test_output_ext_covers_all_codecs(self):
        assert set(NVENCC_OUTPUT_EXT) == NVENCC_VIDEO_CODECS

    def test_output_ext_values(self):
        # Conteneur MP4 (timestamps propagés via libavformat de NVEncC).
        assert NVENCC_OUTPUT_EXT["nvencc_hevc"] == ".mkv"
        assert NVENCC_OUTPUT_EXT["nvencc_h264"] == ".mkv"
        assert NVENCC_OUTPUT_EXT["nvencc_av1"] == ".mkv"


class TestParseSupportedCodecs:
    def test_extracts_hevc_h264_av1(self):
        sample = """
        NVEncC --check-features
        Codec: H.264/AVC
            ...
        Codec: H.265/HEVC
            ...
        Codec: AV1
            ...
        """
        codecs = _parse_supported_codecs(sample)
        assert codecs == {"nvencc_hevc", "nvencc_h264", "nvencc_av1"}

    def test_only_hevc_and_h264_on_pre_ada_gpu(self):
        # GPU pré-Ada Lovelace : pas d'AV1.
        sample = """
        Codec: H.264/AVC OK
        Codec: H.265/HEVC OK
        """
        codecs = _parse_supported_codecs(sample)
        assert codecs == {"nvencc_hevc", "nvencc_h264"}
        assert "nvencc_av1" not in codecs

    def test_empty_output(self):
        assert _parse_supported_codecs("") == set()

    def test_recognizes_alternate_tokens(self):
        # Format alternatif sans le slash.
        sample = "HEVC: supported\nH.264: supported"
        codecs = _parse_supported_codecs(sample)
        assert "nvencc_hevc" in codecs
        assert "nvencc_h264" in codecs


class TestDetectNvenccAvailable:
    def test_no_binary_returns_unavailable(self):
        available, codecs = detect_nvencc_available(None)
        assert available is False
        assert codecs == set()

        available, codecs = detect_nvencc_available("")
        assert available is False
        assert codecs == set()

    def test_binary_not_found_returns_unavailable(self):
        with patch(
            "core.workflows.encode.runtime.nvencc.subprocess.run",
            side_effect=FileNotFoundError(),
        ):
            available, codecs = detect_nvencc_available("/nonexistent/NVEncC")
        assert available is False
        assert codecs == set()

    def test_timeout_returns_unavailable(self):
        with patch(
            "core.workflows.encode.runtime.nvencc.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="NVEncC", timeout=10),
        ):
            available, codecs = detect_nvencc_available("/usr/bin/NVEncC")
        assert available is False
        assert codecs == set()

    def test_full_codec_support(self):
        fake = subprocess.CompletedProcess(
            args=["NVEncC"],
            returncode=0,
            stdout="Codec: H.264/AVC\nCodec: H.265/HEVC\nCodec: AV1\n",
            stderr="",
        )
        with patch(
            "core.workflows.encode.runtime.nvencc.subprocess.run",
            return_value=fake,
        ):
            available, codecs = detect_nvencc_available("/usr/bin/NVEncC")
        assert available is True
        assert codecs == {"nvencc_hevc", "nvencc_h264", "nvencc_av1"}

    def test_partial_codec_support(self):
        # GPU Turing/Ampere : pas d'AV1.
        fake = subprocess.CompletedProcess(
            args=["NVEncC"],
            returncode=0,
            stdout="Codec: H.264/AVC\nCodec: H.265/HEVC\n",
            stderr="",
        )
        with patch(
            "core.workflows.encode.runtime.nvencc.subprocess.run",
            return_value=fake,
        ):
            available, codecs = detect_nvencc_available("/usr/bin/NVEncC")
        assert available is True
        assert codecs == {"nvencc_hevc", "nvencc_h264"}

    def test_empty_output_returns_unavailable(self):
        # Cas hypothétique : binaire répond mais sans codec listé.
        fake = subprocess.CompletedProcess(
            args=["NVEncC"], returncode=0, stdout="", stderr="",
        )
        with patch(
            "core.workflows.encode.runtime.nvencc.subprocess.run",
            return_value=fake,
        ):
            available, codecs = detect_nvencc_available("/usr/bin/NVEncC")
        assert available is False
        assert codecs == set()


class TestHardwareDetectorIntegration:
    """Vérifie le branchement nvencc dans HardwareEncoderDetector.detect()."""

    def test_no_nvencc_bin_yields_no_nvencc_codecs(self):
        """Sans nvencc_bin, aucun codec NVEncC n'est exposé même si NVENC est dispo."""
        from core.workflows.encode.hardware import HardwareEncoderDetector

        detector = HardwareEncoderDetector()
        with patch.object(detector, "_compiled_hw", return_value=("ffmpeg", {"hevc_nvenc", "h264_nvenc"})), \
             patch.object(detector, "_detect_nvenc", return_value={"hevc_nvenc", "h264_nvenc"}), \
             patch.object(detector, "_probe_codecs", return_value=set()):
            available, _ff = detector.detect("ffmpeg")
        assert "nvencc_hevc" not in available
        assert "nvencc_h264" not in available
        assert "nvencc_av1" not in available

    def test_nvencc_added_when_nvenc_available(self):
        from core.workflows.encode.hardware import HardwareEncoderDetector

        detector = HardwareEncoderDetector()
        with patch.object(detector, "_compiled_hw", return_value=("ffmpeg", {"hevc_nvenc"})), \
             patch.object(detector, "_detect_nvenc", return_value={"hevc_nvenc"}), \
             patch.object(detector, "_probe_codecs", return_value=set()), \
             patch(
                "core.workflows.encode.hardware.detect_nvencc_available",
                return_value=(True, {"nvencc_hevc", "nvencc_h264"}),
             ):
            available, _ff = detector.detect("ffmpeg", nvencc_bin="/usr/bin/NVEncC")
        assert "nvencc_hevc" in available
        assert "nvencc_h264" in available
        assert "nvencc_av1" not in available

    def test_nvencc_skipped_when_nvenc_unavailable(self):
        """Garde-fou : NVEncC ne s'expose jamais sans NVENC ffmpeg détecté."""
        from core.workflows.encode.hardware import HardwareEncoderDetector

        detector = HardwareEncoderDetector()
        # NVENC compilé mais probe échoue → nvenc_available reste vide.
        with patch.object(detector, "_compiled_hw", return_value=("ffmpeg", {"hevc_nvenc"})), \
             patch.object(detector, "_detect_nvenc", return_value=set()), \
             patch.object(detector, "_probe_codecs", return_value=set()), \
             patch(
                "core.workflows.encode.hardware.detect_nvencc_available",
                return_value=(True, {"nvencc_hevc"}),
             ) as mocked_detect:
            available, _ff = detector.detect("ffmpeg", nvencc_bin="/usr/bin/NVEncC")
        assert "nvencc_hevc" not in available
        # Important : detect_nvencc_available NE DOIT PAS être appelée car NVENC absent.
        mocked_detect.assert_not_called()


# ---------------------------------------------------------------------------
# Construction du pipeline (chapitre 3)
# ---------------------------------------------------------------------------

def _video(codec: str = "nvencc_hevc", **overrides: Any) -> VideoEncodeSettings:
    return replace(VideoEncodeSettings(codec=codec), **overrides)


class TestBuildDecodePipeCmd:
    def test_minimal_command(self):
        cmd = build_decode_pipe_cmd("ffmpeg", "/in.mkv")
        assert cmd[0] == "ffmpeg"
        assert "-i" in cmd
        idx = cmd.index("-i")
        assert cmd[idx + 1] == "/in.mkv"
        assert cmd[-1] == "-"
        assert "yuv4mpegpipe" in cmd

    def test_no_vf_filter(self):
        # VPP délégués à NVEncC : la phase 1 ne doit JAMAIS contenir -vf.
        cmd = build_decode_pipe_cmd("ffmpeg", "/in.mkv")
        assert "-vf" not in cmd

    def test_stream_index_mapped(self):
        cmd = build_decode_pipe_cmd("ffmpeg", "/in.mkv", stream_index=2)
        assert "-map" in cmd
        idx = cmd.index("-map")
        assert cmd[idx + 1] == "0:2"


class TestBuildNvenccCommand:
    def test_codec_flag_per_codec(self):
        for codec, flag in NVENCC_CODEC_FLAG.items():
            cmd = build_nvencc_command(
                "/usr/bin/NVEncC", _video(codec=codec), "/tmp/out.bin",
            )
            assert cmd[0] == "/usr/bin/NVEncC"
            i = cmd.index("-c")
            assert cmd[i + 1] == flag
            # stdin pipe attendu
            assert "--y4m" in cmd
            i_in = cmd.index("-i")
            assert cmd[i_in + 1] == "-"

    def test_unknown_codec_raises(self):
        v = _video(codec="hevc_nvenc")  # codec ffmpeg, pas NVEncC
        with pytest.raises(ValueError):
            build_nvencc_command("/usr/bin/NVEncC", v, "/tmp/out.bin")

    def test_crf_mode_emits_cqp_triplet(self):
        v = _video(quality_mode=QualityMode.CRF, crf=20)
        cmd = build_nvencc_command("nvencc", v, "/tmp/out.hevc")
        i = cmd.index("--cqp")
        assert cmd[i + 1] == "20:22:24"

    def test_cq_mode_emits_qvbr(self):
        v = _video(quality_mode=QualityMode.CQ, cq=26)
        cmd = build_nvencc_command("nvencc", v, "/tmp/out.hevc")
        i = cmd.index("--qvbr")
        assert cmd[i + 1] == "26"

    def test_bitrate_mode_emits_vbr(self):
        v = _video(quality_mode=QualityMode.BITRATE, bitrate_kbps=12000)
        cmd = build_nvencc_command("nvencc", v, "/tmp/out.hevc")
        i = cmd.index("--vbr")
        assert cmd[i + 1] == "12000"

    def test_force_10bit_adds_output_depth_10(self):
        v = _video(force_10bit=True)
        cmd = build_nvencc_command("nvencc", v, "/tmp/out.hevc")
        assert "--output-depth" in cmd
        assert cmd[cmd.index("--output-depth") + 1] == "10"

    def test_force_8bit_only_for_h264(self):
        # H.264 + force_8bit → --output-depth 8.
        v_h264 = _video(codec="nvencc_h264", force_8bit=True)
        cmd_h264 = build_nvencc_command("nvencc", v_h264, "/tmp/out.h264")
        assert cmd_h264[cmd_h264.index("--output-depth") + 1] == "8"
        # HEVC + force_8bit → flag ignoré (NVEncC HEVC n'a pas de notion 8bit forcé).
        v_hevc = _video(codec="nvencc_hevc", force_8bit=True)
        cmd_hevc = build_nvencc_command("nvencc", v_hevc, "/tmp/out.hevc")
        assert "--output-depth" not in cmd_hevc

    def test_preset_p_levels(self):
        v = _video(preset="P5")
        cmd = build_nvencc_command("nvencc", v, "/tmp/out.hevc")
        assert "-u" in cmd
        assert cmd[cmd.index("-u") + 1] == "P5"

    def test_preset_x265_style_is_ignored(self):
        # "slow" est un preset libx265, pas NVEncC : ne pas propager.
        v = _video(preset="slow")
        cmd = build_nvencc_command("nvencc", v, "/tmp/out.hevc")
        assert "-u" not in cmd

    def test_hdr_static_metadata(self):
        v = _video(
            inject_hdr_meta=True,
            master_display="G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)",
            max_cll="1000,400",
        )
        cmd = build_nvencc_command("nvencc", v, "/tmp/out.hevc")
        assert "--master-display" in cmd
        assert "G(13250,34500)" in cmd[cmd.index("--master-display") + 1]
        assert cmd[cmd.index("--max-cll") + 1] == "1000,400"

    def test_direct_input_adds_video_track(self):
        cmd = build_nvencc_command(
            "nvencc",
            _video(),
            "/tmp/out.hevc",
            input_path="/in.mkv",
            stream_index=2,
        )
        assert "--y4m" not in cmd
        assert cmd[cmd.index("-i") + 1] == "/in.mkv"
        assert cmd[cmd.index("--video-streamid") + 1] == "2"

    def test_direct_input_with_fps_hint_adds_fps(self):
        cmd = build_nvencc_command(
            "nvencc",
            _video(),
            "/tmp/out.hevc",
            input_path="/in.hevc",
            stream_index=0,
            input_fps="24000/1001",
        )
        assert cmd[cmd.index("-i") + 1] == "/in.hevc"
        assert "--fps" in cmd
        assert cmd[cmd.index("--fps") + 1] == "24000/1001"

    def test_direct_input_with_vfr_avsync_adds_avsync(self):
        cmd = build_nvencc_command(
            "nvencc",
            _video(),
            "/tmp/out.hevc",
            input_path="/in.mkv",
            stream_index=0,
            input_avsync="vfr",
        )
        assert cmd[cmd.index("-i") + 1] == "/in.mkv"
        assert "--avsync" in cmd
        assert cmd[cmd.index("--avsync") + 1] == "vfr"

    def test_direct_input_with_forced_avsw_reader_adds_reader_flag(self):
        cmd = build_nvencc_command(
            "nvencc",
            _video(copy_dv=True),
            "/tmp/out.hevc",
            input_path="/in.hevc",
            stream_index=0,
            input_reader="avsw",
        )
        assert "--avsw" in cmd
        assert "--avhw" not in cmd
        assert cmd[cmd.index("-i") + 1] == "/in.hevc"

    def test_hdr10plus_passthrough(self):
        v = _video(copy_hdr10plus=True)
        cmd = build_nvencc_command("nvencc", v, "/tmp/out.hevc")
        assert cmd[cmd.index("--dhdr10-info") + 1] == "copy"

    def test_hdr10plus_from_file(self):
        v = _video()
        cmd = build_nvencc_command(
            "nvencc", v, "/tmp/out.hevc",
            hdr10plus_json="/work/hdr10plus.json",
        )
        assert cmd[cmd.index("--dhdr10-info") + 1] == "/work/hdr10plus.json"

    def test_dovi_rpu_with_profile_8(self):
        v = _video(dovi_profile="8")
        cmd = build_nvencc_command(
            "nvencc", v, "/tmp/out.hevc",
            dovi_rpu="/work/rpu.bin",
        )
        assert cmd[cmd.index("--dolby-vision-rpu") + 1] == "/work/rpu.bin"
        assert cmd[cmd.index("--dolby-vision-profile") + 1] == "8.1"

    def test_dovi_passthrough(self):
        v = _video(copy_dv=True)
        cmd = build_nvencc_command("nvencc", v, "/tmp/out.hevc")
        assert cmd[cmd.index("--dolby-vision-rpu") + 1] == "copy"
        assert cmd[cmd.index("--dolby-vision-profile") + 1] == "8.1"

    def test_direct_input_dovi_passthrough_forces_profile_8_1(self):
        v = _video(copy_dv=True)
        cmd = build_nvencc_command(
            "nvencc",
            v,
            "/tmp/out.hevc",
            input_path="/in.mkv",
            stream_index=0,
        )
        assert cmd[cmd.index("--dolby-vision-rpu") + 1] == "copy"
        assert cmd[cmd.index("--dolby-vision-profile") + 1] == "8.1"

    def test_external_dovi_rpu_does_not_keep_profile_copy(self):
        v = _video(copy_dv=True)
        cmd = build_nvencc_command(
            "nvencc",
            v,
            "/tmp/out.hevc",
            input_path="/in.mkv",
            stream_index=0,
            dovi_rpu="/work/rpu.bin",
        )
        assert cmd[cmd.index("--dolby-vision-rpu") + 1] == "/work/rpu.bin"
        assert "--dolby-vision-profile" not in cmd

    def test_dovi_copy_crop_injects_rpu_prm(self):
        cmd = build_nvencc_command(
            "nvencc",
            _video(copy_dv=True),
            "/tmp/out.hevc",
            input_path="/in.mkv",
            stream_index=0,
            dovi_rpu_prm="crop=true",
        )
        assert cmd[cmd.index("--dolby-vision-rpu-prm") + 1] == "crop=true"

    def test_dovi_ui_legacy_profile_2_maps_to_8_1(self):
        v = _video(copy_dv=True, dovi_profile="2")
        cmd = build_nvencc_command("nvencc", v, "/tmp/out.hevc")
        assert cmd[cmd.index("--dolby-vision-profile") + 1] == "8.1"

    def test_dovi_decimal_profile_is_kept_for_nvencc(self):
        v = _video(copy_dv=True, dovi_profile="8.1")
        cmd = build_nvencc_command("nvencc", v, "/tmp/out.hevc")
        assert cmd[cmd.index("--dolby-vision-profile") + 1] == "8.1"

    def test_dovi_compact_profile_is_expanded_for_nvencc(self):
        v = _video(copy_dv=True, dovi_profile="81")
        cmd = build_nvencc_command("nvencc", v, "/tmp/out.hevc")
        assert cmd[cmd.index("--dolby-vision-profile") + 1] == "8.1"

    def test_direct_input_dynamic_hdr_copies_static_hdr_from_source(self):
        v = _video(copy_dv=True, copy_hdr10plus=True)
        cmd = build_nvencc_command(
            "nvencc",
            v,
            "/tmp/out.hevc",
            input_path="/in.mkv",
            stream_index=0,
        )
        assert cmd[cmd.index("--master-display") + 1] == "copy"
        assert cmd[cmd.index("--max-cll") + 1] == "copy"
        assert cmd[cmd.index("--colormatrix") + 1] == "auto"
        assert cmd[cmd.index("--colorprim") + 1] == "auto"
        assert cmd[cmd.index("--transfer") + 1] == "auto"
        assert cmd[cmd.index("--chromaloc") + 1] == "auto"

    def test_tonemap_ui_maps_to_nvencc_vpp_colorspace(self):
        v = _video(tonemap_to_sdr=True, tonemap_algorithm="mobius")
        cmd = build_nvencc_command("nvencc", v, "/tmp/out.hevc")
        assert "--vpp-colorspace" in cmd
        assert "hdr2sdr=mobius" in cmd[cmd.index("--vpp-colorspace") + 1]

    def test_tonemap_ui_maps_linear_to_libplacebo(self):
        v = _video(tonemap_to_sdr=True, tonemap_algorithm="linear")
        cmd = build_nvencc_command("nvencc", v, "/tmp/out.hevc")
        assert "--vpp-libplacebo-tonemapping" in cmd
        assert "tonemapping_function=linear" in cmd[cmd.index("--vpp-libplacebo-tonemapping") + 1]

    def test_native_transform_ui_maps_to_nvencc_vpp_args(self):
        v = _video(
            crop=VideoCropSettings(enabled=True, left=8, top=4, right=8, bottom=4),
            resize=VideoResizeSettings(enabled=True, mode="size", width=1920, height=1080),
            filters=VideoFilterSettings(yadif_enabled=True, nlmeans_enabled=True),
        )

        cmd = build_nvencc_command("nvencc", v, "/tmp/out.hevc", input_path="/in.mkv", stream_index=0)

        assert cmd[cmd.index("--crop") + 1] == "8,4,8,4"
        assert cmd[cmd.index("--output-res") + 1] == "1920x1080"
        assert "--vpp-resize" in cmd
        assert "--vpp-yadif" in cmd
        assert "--vpp-nlmeans" in cmd

    def test_deblock_and_chroma_smooth_require_ffmpeg_pipe(self):
        v = _video(filters=VideoFilterSettings(deblock_enabled=True, chroma_smooth_enabled=True))
        args = map_nvencc_video_transform_args(v)

        assert nvencc_requires_ffmpeg_filter_pipe(v) is True
        assert "--vpp-yadif" not in args
        assert "--vpp-nlmeans" not in args

    def test_extra_params_appended(self):
        v = _video(extra_params="--aq --aq-temporal --aq-strength 12 --multipass 2pass-full")
        cmd = build_nvencc_command("nvencc", v, "/tmp/out.hevc")
        assert "--aq" in cmd
        assert "--aq-temporal" in cmd
        assert "12" in cmd
        assert "2pass-full" in cmd

    def test_parallel_all_is_canonicalized_to_auto(self):
        v = _video(extra_params="--parallel all")
        cmd = build_nvencc_command("nvencc", v, "/tmp/out.hevc")
        assert "--parallel" in cmd
        assert cmd[cmd.index("--parallel") + 1] == "auto"

    def test_dynamic_hdr_passthrough_strips_parallel_encode(self):
        v = _video(copy_dv=True, extra_params="--parallel 2 --aq")
        cmd = build_nvencc_command(
            "nvencc",
            v,
            "/tmp/out.hevc",
            input_path="/in.mkv",
            stream_index=0,
        )
        assert "--parallel" not in cmd
        assert "--aq" in cmd

    def test_dynamic_hdr_passthrough_strips_parallel_equals_form(self):
        v = _video(copy_hdr10plus=True, extra_params="--parallel=auto --aq-strength 12")
        cmd = build_nvencc_command(
            "nvencc",
            v,
            "/tmp/out.hevc",
            input_path="/in.mkv",
            stream_index=0,
        )
        assert "--parallel" not in cmd
        assert "--aq-strength" in cmd
        assert "12" in cmd

    def test_dynamic_hdr_passthrough_strips_lowlatency_tune(self):
        v = _video(copy_dv=True, extra_params="--tune lowlatency --aq")
        cmd = build_nvencc_command(
            "nvencc",
            v,
            "/tmp/out.hevc",
            input_path="/in.mkv",
            stream_index=0,
        )
        assert "--tune" not in cmd
        assert "--aq" in cmd

    def test_dynamic_hdr_passthrough_strips_ultralowlatency_equals_tune(self):
        v = _video(copy_hdr10plus=True, extra_params="--tune=ultralowlatency --aq-strength 12")
        cmd = build_nvencc_command(
            "nvencc",
            v,
            "/tmp/out.hevc",
            input_path="/in.mkv",
            stream_index=0,
        )
        assert "--tune" not in cmd
        assert "--aq-strength" in cmd
        assert "12" in cmd

    def test_dynamic_hdr_passthrough_strips_lowlatency_flag(self):
        v = _video(copy_dv=True, extra_params="--lowlatency --aq")
        cmd = build_nvencc_command(
            "nvencc",
            v,
            "/tmp/out.hevc",
            input_path="/in.mkv",
            stream_index=0,
        )
        assert "--lowlatency" not in cmd
        assert "--aq" in cmd

    def test_workflow_owned_hdr_flags_in_extra_params_are_superseded(self):
        v = _video(
            inject_hdr_meta=True,
            master_display="UI_MD",
            max_cll="1111,222",
            copy_dv=True,
            dovi_profile="2",
            copy_hdr10plus=True,
            tonemap_to_sdr=True,
            tonemap_algorithm="mobius",
            extra_params=(
                "--master-display OLD_MD "
                "--max-cll 1,2 "
                "--dhdr10-info old.json "
                "--dolby-vision-profile 5.0 "
                "--dolby-vision-rpu old.bin "
                "--dolby-vision-rpu-prm crop=false "
                "--avhw "
                "--colormatrix bt709 "
                "--colorprim bt709 "
                "--transfer bt709 "
                "--chromaloc 1 "
                "--vpp-colorspace matrix=bt709:bt709,hdr2sdr=hable "
                "--vpp-libplacebo-tonemapping src_csp=hdr10,dst_csp=sdr,tonemapping_function=clip "
                "--vpp-libplacebo-tonemapping-lut /tmp/lut.cube "
                "--aq --aq-strength 12"
            ),
        )
        cmd = build_nvencc_command("nvencc", v, "/tmp/out.hevc", input_path="/in.mkv", stream_index=0)
        joined = " ".join(cmd)
        assert "--aq" in cmd
        assert "--aq-strength" in cmd
        assert "12" in cmd
        assert "OLD_MD" not in joined
        assert "old.json" not in joined
        assert "old.bin" not in joined
        assert "/tmp/lut.cube" not in joined
        assert "--avhw" not in cmd
        assert cmd[cmd.index("--master-display") + 1] == "UI_MD"
        assert cmd[cmd.index("--max-cll") + 1] == "1111,222"
        assert cmd[cmd.index("--dhdr10-info") + 1] == "copy"
        assert cmd[cmd.index("--dolby-vision-rpu") + 1] == "copy"
        assert cmd[cmd.index("--dolby-vision-profile") + 1] == "8.1"
        assert "matrix=bt709:bt709,hdr2sdr=hable" not in joined
        assert "tonemapping_function=clip" not in joined
        assert "--vpp-colorspace" in cmd
        assert "hdr2sdr=mobius" in cmd[cmd.index("--vpp-colorspace") + 1]
        assert "--vpp-libplacebo-tonemapping" not in cmd

    @pytest.mark.parametrize("flag", ["--cqp", "--qp-init", "--qp-min", "--qp-max"])
    def test_qp_triplets_from_extra_params_single_value_are_normalized(self, flag):
        v = _video(extra_params=f"{flag} 18")
        cmd = build_nvencc_command("nvencc", v, "/tmp/out.hevc")
        positions = [idx for idx, token in enumerate(cmd) if token == flag]
        assert positions
        assert cmd[positions[-1] + 1] == "18:18:18"

    def test_output_path(self):
        cmd = build_nvencc_command("nvencc", _video(), "/work/out.hevc")
        assert cmd[-1] == "/work/out.hevc"
        assert cmd[-2] == "-o"


class TestBuildRemuxCmd:
    def test_basic(self):
        cmd = build_remux_cmd("ffmpeg", "/work/encoded.hevc", "/in.mkv", "/out.mkv")
        assert cmd[0] == "ffmpeg"
        assert cmd[-1] == "/out.mkv"
        # Input 0 = encoded, input 1 = source
        assert cmd[cmd.index("-i") + 1] == "/work/encoded.hevc"
        # Map vidéo phase 2 + audio/subs/chapters source
        assert "0:v:0" in cmd
        assert "1:a?" in cmd
        assert "1:s?" in cmd
        # Stream copy de tout
        assert cmd[cmd.index("-c") + 1] == "copy"

    def test_disable_subtitles(self):
        cmd = build_remux_cmd(
            "ffmpeg", "/e.hevc", "/in.mkv", "/out.mkv",
            map_subtitles=False,
        )
        assert "1:s?" not in cmd

    def test_extra_audio_args(self):
        cmd = build_remux_cmd(
            "ffmpeg", "/e.hevc", "/in.mkv", "/out.mkv",
            extra_args=["-c:a", "eac3", "-b:a", "640k"],
        )
        assert "-c:a" in cmd
        assert cmd[cmd.index("-c:a") + 1] == "eac3"


class TestBuildNvenccPipeline:
    def test_returns_three_commands(self):
        seq = build_nvencc_pipeline(
            ffmpeg_bin="ffmpeg",
            nvencc_bin="nvencc",
            video=_video(),
            source="/in.mkv",
            output="/out.mkv",
            intermediate="/work/encoded.hevc",
        )
        assert len(seq) == 3
        decode, encode, remux = seq
        # Phase 1 : ffmpeg → stdout
        assert decode[0] == "ffmpeg"
        assert decode[-1] == "-"
        # Phase 2 : NVEncC reading "-"
        assert encode[0] == "nvencc"
        assert "-i" in encode
        assert encode[encode.index("-i") + 1] == "-"
        assert encode[-2] == "-o"
        assert encode[-1] == "/work/encoded.hevc"
        # Phase 3 : ffmpeg remux
        assert remux[0] == "ffmpeg"
        assert remux[-1] == "/out.mkv"

    def test_ffmpeg_prefilter_pipe_carries_deblock_chroma_and_strips_native_vpp(self):
        seq = build_nvencc_pipeline(
            ffmpeg_bin="ffmpeg",
            nvencc_bin="nvencc",
            video=_video(
                resize=VideoResizeSettings(enabled=True, mode="percent", percent=75),
                filters=VideoFilterSettings(deblock_enabled=True, chroma_smooth_enabled=True),
            ),
            source="/in.mkv",
            output="/out.mkv",
            intermediate="/work/encoded.hevc",
        )

        decode, encode, _remux = seq
        vf = decode[decode.index("-vf") + 1]
        assert "deblock=" in vf
        assert "chromanr=" in vf
        assert "scale=trunc(iw*75/100/2)*2" in vf
        assert "--y4m" in encode
        assert encode[encode.index("-i") + 1] == "-"
        assert "--output-res" not in encode
        assert "--vpp-resize" not in encode


class TestNvenccIntermediatePath:
    @pytest.mark.parametrize("codec", ["nvencc_hevc", "nvencc_h264", "nvencc_av1"])
    def test_extension_is_container(self, codec, tmp_path: Path):
        # Le fichier intermédiaire doit être un container (.mkv) et non un
        # bitstream brut, sinon ffmpeg ne peut pas le muxer (timestamps unset).
        p = nvencc_intermediate_path(tmp_path, codec)
        assert p.suffix == ".mkv"
        assert p.parent == tmp_path


# ---------------------------------------------------------------------------
# Intégration EncodeWorkflow.build_command (court-circuit NVEncC)
# ---------------------------------------------------------------------------

import os  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _make_encode_config(source: Path, output: Path, **video_overrides):
    """EncodeConfig minimal pour tester le court-circuit NVEncC."""
    from core.workflows.encode.models import EncodeConfig

    video = _video(**video_overrides)
    return EncodeConfig(
        source=source, output=output, video=video,
        audio_tracks=[], copy_subtitles=False, duration_s=3600.0,
    )


class TestWorkflowNvenccShortCircuit:
    """Vérifie que EncodeWorkflow bascule sur le pipeline NVEncC quand pertinent."""

    def _make_wf(self, nvencc: str | None = "/usr/bin/NVEncC"):
        from core.workflows.encode.workflow import EncodeWorkflow
        return EncodeWorkflow(
            ffmpeg_bin="ffmpeg",
            dovi_tool_bin="dovi_tool",
            hdr10plus_bin="hdr10plus_tool",
            mediainfo_bin="mediainfo",
            nvencc_bin=nvencc,
        )

    def test_nvencc_codec_returns_two_commands(self, tmp_path: Path):
        wf = self._make_wf()
        config = _make_encode_config(
            tmp_path / "in.mkv", tmp_path / "out.mkv",
            codec="nvencc_hevc", crf=20,
        )
        result = wf.build_command(config)
        # Doit être list[list[str]] de longueur 2 (encode | remux).
        assert isinstance(result, list)
        assert len(result) == 2
        assert all(isinstance(c, list) for c in result)
        encode, remux = result
        assert encode[0] == "/usr/bin/NVEncC"
        assert encode[encode.index("-i") + 1] == str(tmp_path / "in.mkv")
        assert "--video-streamid" in encode
        assert "--cqp" in encode
        assert remux[0] == "ffmpeg"
        assert remux[-1] == str(tmp_path / "out.mkv")

    def test_nvencc_prefilter_returns_ffmpeg_pipe_encode_remux(self, tmp_path: Path):
        wf = self._make_wf()
        config = _make_encode_config(
            tmp_path / "in.mkv",
            tmp_path / "out.mkv",
            codec="nvencc_hevc",
            filters=VideoFilterSettings(deblock_enabled=True),
        )

        result = wf.build_command(config)

        assert isinstance(result, list)
        assert len(result) == 3
        decode, encode, remux = result
        assert decode[0] == "ffmpeg"
        assert "-vf" in decode
        assert "deblock=" in decode[decode.index("-vf") + 1]
        assert encode[0] == "/usr/bin/NVEncC"
        assert encode[encode.index("-i") + 1] == "-"
        assert remux[0] == "ffmpeg"

    def test_no_nvencc_bin_falls_back(self, tmp_path: Path):
        wf = self._make_wf(nvencc=None)
        config = _make_encode_config(
            tmp_path / "in.mkv", tmp_path / "out.mkv",
            codec="nvencc_hevc",
        )
        # Sans bin, le court-circuit retourne None → pipeline ffmpeg normal.
        result = wf.build_command(config)
        joined = " ".join(
            tok
            for sub in result
            for tok in ([sub] if isinstance(sub, str) else sub)
        )
        # NVEncC n'est PAS lancé sans binaire configuré.
        assert "NVEncC" not in joined
        # On doit avoir un ffmpeg seul (1 commande), pas de séquence encode+remux NVEncC.
        assert isinstance(result, list) and (
            isinstance(result[0], str)
            or len(result) != 2
        )

    def test_non_nvencc_codec_unchanged(self, tmp_path: Path):
        wf = self._make_wf()
        config = _make_encode_config(
            tmp_path / "in.mkv", tmp_path / "out.mkv",
            codec="libx265", crf=18,
        )
        result = wf.build_command(config)
        # Pour libx265, on reste sur le pipeline ffmpeg standard (1 commande).
        assert isinstance(result, list)
        # result peut être list[str] (single pass) — pas une liste de 3 listes.
        if result and isinstance(result[0], list):
            # Si list[list[str]], ça vient du multi-track / two-pass, pas de NVEncC.
            assert len(result) != 3 or not any("NVEncC" in tok for sub in result for tok in sub)

    def test_build_command_single_returns_encode_phase(self, tmp_path: Path):
        wf = self._make_wf()
        config = _make_encode_config(
            tmp_path / "in.mkv", tmp_path / "out.mkv",
            codec="nvencc_hevc", crf=20,
        )
        cmd = wf.build_command_single(config)
        assert isinstance(cmd, list)
        assert cmd[0] == "/usr/bin/NVEncC"
        assert "--cqp" in cmd

    def test_validate_rejects_size_mode(self, tmp_path: Path):
        wf = self._make_wf()
        config = _make_encode_config(
            tmp_path / "in.mkv", tmp_path / "out.mkv",
            codec="nvencc_hevc", quality_mode=QualityMode.SIZE,
            target_size_mb=2000,
        )
        errors = wf.validate(config)
        assert any("taille cible" in err.lower() for err in errors)

    def test_validate_rejects_missing_binary(self, tmp_path: Path):
        wf = self._make_wf(nvencc=None)
        config = _make_encode_config(
            tmp_path / "in.mkv", tmp_path / "out.mkv",
            codec="nvencc_hevc",
        )
        errors = wf.validate(config)
        assert any("binaire" in err.lower() for err in errors)

    def test_validate_allows_native_tonemap(self, tmp_path: Path):
        wf = self._make_wf()
        config = _make_encode_config(
            tmp_path / "in.mkv", tmp_path / "out.mkv",
            codec="nvencc_hevc",
            tonemap_to_sdr=True,
        )
        errors = wf.validate(config)
        assert not any("tone-mapping" in err.lower() for err in errors)
        cmd = wf.build_command_single(config)
        assert "--vpp-colorspace" in cmd or "--vpp-libplacebo-tonemapping" in cmd

    def test_validate_rejects_multi_video_even_with_copy_track(self, tmp_path: Path):
        from core.workflows.encode.models import EncodeConfig

        wf = self._make_wf()
        config = EncodeConfig(
            source=tmp_path / "in.mkv",
            output=tmp_path / "out.mkv",
            video_tracks=[
                _video(codec="nvencc_hevc"),
                _video(codec="copy"),
            ],
            audio_tracks=[],
            copy_subtitles=False,
            duration_s=3600.0,
        )
        errors = wf.validate(config)
        assert any("multi-pistes vidéo" in err.lower() for err in errors)

    def test_validate_rejects_dynamic_hdr_on_nvencc_h264(self, tmp_path: Path):
        wf = self._make_wf()
        config = _make_encode_config(
            tmp_path / "in.mkv", tmp_path / "out.mkv",
            codec="nvencc_h264",
            copy_dv=True,
        )
        errors = wf.validate(config)
        assert any("dovi/hdr10+" in err.lower() for err in errors)

    def test_validate_allows_workflow_owned_extra_params(self, tmp_path: Path):
        wf = self._make_wf()
        config = _make_encode_config(
            tmp_path / "in.mkv",
            tmp_path / "out.mkv",
            codec="nvencc_hevc",
            copy_dv=True,
            extra_params="--dolby-vision-profile 8.1 --master-display OLD_MD",
        )
        errors = wf.validate(config)
        assert not any("extra_params" in err.lower() for err in errors)
        cmd = wf.build_command_single(config)
        assert cmd[cmd.index("--dolby-vision-profile") + 1] == "8.1"

    def test_validate_rejects_non_hevc_raw_input_for_dovi_copy(self, tmp_path: Path):
        wf = self._make_wf()
        src = tmp_path / "in.h264"
        src.touch()
        config = _make_encode_config(src, tmp_path / "out.mkv", codec="nvencc_hevc", copy_dv=True)
        errors = wf.validate(config)
        assert any("hevc" in err.lower() and "dolby-vision-rpu copy" in err.lower() for err in errors)

    def test_validate_allows_vfr_when_copy_dv_uses_external_converted_rpu(self, tmp_path: Path):
        wf = self._make_wf()
        src = tmp_path / "in.mkv"
        out = tmp_path / "out.mkv"
        config = _make_encode_config(src, out, codec="nvencc_hevc", copy_dv=True)

        with patch.object(wf, "_source_is_vfr", return_value=True):
            errors = wf.validate(config)

        assert not any("vfr" in err.lower() and "conversion hevc intermédiaire" in err.lower() for err in errors)


class TestNvenccPipelineExecution:
    """Tests d'exécution réelle du pipeline NVEncC (skip si binaire absent).

    Construit un MKV de test via ffmpeg, lance le pipeline complet
    ``ffmpeg | NVEncC → ffmpeg`` via ``subprocess.Popen`` (avec pipe stdout→stdin)
    et vérifie que la sortie est valide.
    """

    @pytest.fixture(scope="class")
    def nvencc_bin(self) -> str:
        import shutil
        bin_path = shutil.which("nvencc") or shutil.which("NVEncC")
        if not bin_path:
            pytest.skip("NVEncC non disponible sur le PATH (test e2e)")
        return bin_path

    @pytest.fixture(scope="class")
    def ffmpeg_bin(self) -> str:
        import shutil
        bin_path = shutil.which("ffmpeg")
        if not bin_path:
            pytest.skip("ffmpeg non disponible (test e2e)")
        return bin_path

    @pytest.fixture
    def test_mkv(self, tmp_path: Path, ffmpeg_bin: str) -> Path:
        """Génère un MKV 320x240 25fps 2s via ffmpeg testsrc2."""
        out = tmp_path / "test_in.mkv"
        cmd = [
            ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-f", "matroska", str(out),
        ]
        rc = subprocess.run(cmd, capture_output=True, timeout=60).returncode
        assert rc == 0, "ffmpeg testsrc2 failed"
        assert out.exists() and out.stat().st_size > 1000
        return out

    def test_real_pipeline_hevc(
        self, tmp_path: Path,
        ffmpeg_bin: str, nvencc_bin: str, test_mkv: Path,
    ):
        """Exécute le pipeline complet ffmpeg|NVEncC→ffmpeg sur un MKV réel.

        Reproduit exactement ce que ``EncodeWorkflow`` fera : 3 process
        chaînés, phases 1↔2 via pipe, phase 3 séquentielle.
        """
        from core.workflows.encode.runtime.nvencc import (
            build_decode_pipe_cmd,
            build_nvencc_command,
            build_remux_cmd,
            nvencc_intermediate_path,
        )

        intermediate = nvencc_intermediate_path(tmp_path, "nvencc_hevc")
        output = tmp_path / "out.mkv"

        video = _video(
            codec="nvencc_hevc",
            quality_mode=QualityMode.CRF,
            crf=28,
            preset="P4",
        )

        decode_cmd = build_decode_pipe_cmd(ffmpeg_bin, test_mkv)
        encode_cmd = build_nvencc_command(nvencc_bin, video, intermediate)
        remux_cmd = build_remux_cmd(ffmpeg_bin, intermediate, test_mkv, output)

        # Phase 1+2 : pipe stdout→stdin (pattern subprocess.Popen).
        p1 = subprocess.Popen(decode_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p2 = subprocess.Popen(
            encode_cmd, stdin=p1.stdout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        # Permet à p1 de recevoir SIGPIPE si p2 crash.
        if p1.stdout is not None:
            p1.stdout.close()

        # Attendre la fin des 2 process (timeout généreux : 60s pour 50 frames).
        p2_stdout, p2_stderr = p2.communicate(timeout=60)
        p1.wait(timeout=10)

        assert p2.returncode == 0, (
            f"NVEncC failed (rc={p2.returncode}):\n"
            f"stderr: {p2_stderr.decode('utf-8', errors='replace')[-500:]}"
        )
        assert p1.returncode == 0, f"ffmpeg phase 1 failed (rc={p1.returncode})"
        assert intermediate.exists(), "Pas de fichier intermédiaire"
        assert intermediate.stat().st_size > 1000, "Bitstream NVEncC trop petit"

        # Phase 3 : remux séquentiel.
        p3 = subprocess.run(remux_cmd, capture_output=True, timeout=30)
        assert p3.returncode == 0, (
            f"Remux failed:\n{p3.stderr.decode('utf-8', errors='replace')[-500:]}"
        )
        assert output.exists() and output.stat().st_size > 1000

        # Validation finale : le MKV doit contenir un flux HEVC à 25 fps.
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,width,height,r_frame_rate",
                "-of", "default=nw=1:nk=1",
                str(output),
            ],
            capture_output=True, timeout=10, text=True,
        )
        assert probe.returncode == 0
        out_lines = probe.stdout.strip().split("\n")
        assert "hevc" in out_lines, f"Codec attendu hevc, got {out_lines}"
        assert "320" in out_lines
        assert "240" in out_lines

    def test_dashboard_detection_with_real_binary(self, nvencc_bin: str):
        """Vérifie qu'une instance réelle de HardwareEncoderDetector trouve NVEncC."""
        from core.workflows.encode.hardware import HardwareEncoderDetector

        detector = HardwareEncoderDetector()
        hw, _ff = detector.detect("ffmpeg", nvencc_bin=nvencc_bin)
        # Sur RTX 40+ on doit avoir au minimum HEVC + H.264 + AV1.
        assert "nvencc_hevc" in hw
        assert "nvencc_h264" in hw
        # AV1 conditionnel : vérifie que la fonction de détection répond.
        # Sur un GPU pré-Ada, av1 manquerait — on vérifie juste la présence dans hw
        # uniquement si --check-features le mentionne.

    def test_config_resolves_real_binary(self, nvencc_bin: str):
        """Vérifie qu'AppConfig.tool_nvencc trouve le binaire installé."""
        from core.config import AppConfig

        cfg = AppConfig()
        # Le résolveur doit retourner le chemin réel (PascalCase ou minuscules).
        assert cfg.tool_nvencc, "tool_nvencc vide"
        # Sur Linux, doit pointer vers un fichier existant.
        from pathlib import Path as _P
        if "/" in cfg.tool_nvencc:
            assert _P(cfg.tool_nvencc).is_file(), (
                f"tool_nvencc={cfg.tool_nvencc} n'existe pas"
            )


class TestDomainCodecsShortCircuit:
    """Vérifie que video_codec_args() retourne [] pour les codecs NVEncC."""

    def test_nvencc_returns_empty(self):
        from core.workflows.encode.domain.codecs import (
            EncodeCodecDomainCallbacks,
            video_codec_args,
        )

        cbs = EncodeCodecDomainCallbacks(platform="linux")
        for codec in NVENCC_VIDEO_CODECS:
            v = _video(codec=codec, quality_mode=QualityMode.CRF, crf=20)
            assert video_codec_args(v, 5000, callbacks=cbs) == []

    def test_libx265_unchanged(self):
        from core.workflows.encode.domain.codecs import (
            EncodeCodecDomainCallbacks,
            video_codec_args,
        )

        cbs = EncodeCodecDomainCallbacks(platform="linux")
        v = _video(codec="libx265", quality_mode=QualityMode.CRF, crf=18, preset="slow")
        result = video_codec_args(v, 5000, callbacks=cbs)
        assert "-c:v" in result
        assert "libx265" in result
