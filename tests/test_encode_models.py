"""
tests/test_encode_models.py — Tests unitaires pour core/workflows/encode/models.py

Couverture :
    QualityMode     — valeurs, label()
    Constants       — SOFTWARE_VIDEO_CODECS, HARDWARE_VIDEO_CODECS, AUDIO_CODECS
    presets_for_codec — dispatch correct selon le codec
    VideoEncodeSettings — valeurs par défaut, champs
    AudioTrackSettings  — valeurs par défaut, champs obligatoires
    EncodeConfig        — valeurs par défaut, champs obligatoires
    EncodePreset        — valeurs par défaut, to_video_settings()
    EncodeError         — est une RuntimeError

Exécution :
    cd Muxiveo && pytest tests/test_encode_models.py -v
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


import pytest

from core.workflows.encode.models import (
    AudioTrackSettings,
    EncodeConfig,
    EncodeError,
    EncodePreset,
    QualityMode,
    TrackTimeOffset,
    VideoCropSettings,
    VideoTrackEncodePlan,
    VideoEncodeSettings,
    VideoFilterSettings,
    VideoResizeSettings,
)
from core.workflows.encode.catalog import (
    AMF_PRESETS,
    AUDIO_CODECS,
    HARDWARE_VIDEO_CODECS,
    HEVC_NVENC_PRESETS,
    NVENC_PRESETS,
    QSV_PRESETS,
    SOFTWARE_VIDEO_CODECS,
    SVTAV1_PRESETS,
    TONEMAP_ALGORITHMS,
    VAAPI_PRESETS,
    X265_PRESETS,
    presets_for_codec,
)


# ===========================================================================
# QualityMode
# ===========================================================================

class TestQualityMode:
    def test_values(self):
        assert QualityMode.CRF.value == "crf"
        assert QualityMode.CQ.value == "cq"
        assert QualityMode.BITRATE.value == "bitrate"
        assert QualityMode.SIZE.value == "size"

    def test_labels(self):
        assert "CRF" in QualityMode.CRF.label()
        assert "CQ" in QualityMode.CQ.label()
        assert "kbps" in QualityMode.BITRATE.label()
        assert "Mo" in QualityMode.SIZE.label()

    def test_str_enum_comparison(self):
        assert QualityMode.CRF == "crf"
        assert QualityMode("bitrate") is QualityMode.BITRATE
        assert QualityMode("cq") is QualityMode.CQ

    def test_all_values_covered(self):
        assert len(list(QualityMode)) == 4


# ===========================================================================
# Constantes
# ===========================================================================

class TestConstants:
    def test_software_codecs_are_tuples(self):
        for item in SOFTWARE_VIDEO_CODECS:
            assert len(item) == 2
            codec_id, label = item
            assert isinstance(codec_id, str)
            assert isinstance(label, str)

    def test_hardware_codecs_include_nvenc_amf_qsv(self):
        ids = [c for c, _ in HARDWARE_VIDEO_CODECS]
        assert "hevc_nvenc" in ids
        assert "hevc_amf"   in ids
        assert "hevc_qsv"   in ids
        assert "h264_nvenc" in ids

    def test_audio_codecs_include_copy_aac_ac3_eac3_flac(self):
        ids = [c for c, _ in AUDIO_CODECS]
        assert "copy" in ids
        assert "aac"  in ids
        assert "ac3"  in ids
        assert "eac3" in ids
        assert "flac" in ids

    def test_x265_presets_order(self):
        assert X265_PRESETS[0] == "ultrafast"
        assert X265_PRESETS[-1] == "placebo"
        assert "slow" in X265_PRESETS

    def test_svtav1_presets_are_numeric_strings(self):
        for p in SVTAV1_PRESETS:
            assert p.isdigit()
        assert len(SVTAV1_PRESETS) == 13   # 0..12

    def test_nvenc_presets_include_p_values(self):
        assert "p1" in NVENC_PRESETS
        assert "p7" in NVENC_PRESETS

    def test_tonemap_algorithms_non_empty(self):
        assert len(TONEMAP_ALGORITHMS) > 0
        assert "hable" in TONEMAP_ALGORITHMS


# ===========================================================================
# presets_for_codec
# ===========================================================================

class TestPresetsForCodec:
    def test_libx265_returns_x265_presets(self):
        result = presets_for_codec("libx265")
        assert result == X265_PRESETS

    def test_libx264_returns_x265_presets(self):
        result = presets_for_codec("libx264")
        assert result == X265_PRESETS   # alias

    def test_svtav1_returns_numeric_presets(self):
        result = presets_for_codec("libsvtav1")
        assert result == SVTAV1_PRESETS

    def test_nvenc_hevc_returns_nvenc_presets(self):
        assert presets_for_codec("hevc_nvenc") == HEVC_NVENC_PRESETS
        assert "safe" not in presets_for_codec("hevc_nvenc")

    def test_nvenc_h264_returns_nvenc_presets(self):
        assert presets_for_codec("h264_nvenc") == NVENC_PRESETS

    def test_amf_returns_amf_presets(self):
        for codec in ("hevc_amf", "h264_amf", "av1_amf"):
            assert presets_for_codec(codec) == AMF_PRESETS

    def test_qsv_returns_qsv_presets(self):
        for codec in ("hevc_qsv", "h264_qsv", "av1_qsv"):
            assert presets_for_codec(codec) == QSV_PRESETS

    def test_vaapi_returns_vaapi_presets(self):
        for codec in ("hevc_vaapi", "h264_vaapi", "av1_vaapi"):
            assert presets_for_codec(codec) == VAAPI_PRESETS

    def test_unknown_codec_returns_x265_presets(self):
        assert presets_for_codec("unknown_codec") == X265_PRESETS


# ===========================================================================
# VideoEncodeSettings
# ===========================================================================

class TestVideoEncodeSettings:
    def test_defaults(self):
        vs = VideoEncodeSettings()
        assert vs.codec == "libx265"
        assert vs.quality_mode == QualityMode.CRF
        assert vs.crf == 18
        assert vs.bitrate_kbps == 5000
        assert vs.target_size_mb == 4000
        assert vs.preset == "slow"
        assert vs.extra_params == ""
        assert vs.force_8bit is False
        assert vs.inject_hdr_meta is False
        assert vs.master_display == ""
        assert vs.max_cll == ""
        assert vs.copy_dv is False
        assert vs.copy_hdr10plus is False
        assert vs.dovi_profile == "0"
        assert vs.tonemap_to_sdr is False
        assert vs.tonemap_algorithm == "hable"

    def test_custom_values(self):
        vs = VideoEncodeSettings(
            codec="libsvtav1",
            quality_mode=QualityMode.BITRATE,
            crf=24,
            bitrate_kbps=8000,
            tonemap_to_sdr=True,
            tonemap_algorithm="mobius",
        )
        assert vs.codec == "libsvtav1"
        assert vs.quality_mode == QualityMode.BITRATE
        assert vs.crf == 24
        assert vs.tonemap_to_sdr is True
        assert vs.tonemap_algorithm == "mobius"

    def test_nested_transform_dicts_are_coerced(self):
        # Coercion dict → dataclass via __post_init__ (from_value) : input dict volontaire.
        vs = VideoEncodeSettings(
            resize={"enabled": True, "mode": "size", "width": 1920, "height": 1080},  # pyright: ignore[reportArgumentType]
            crop={"enabled": True, "top": 8, "bottom": 8},  # pyright: ignore[reportArgumentType]
            filters={"deblock_enabled": True, "chroma_smooth_enabled": True},  # pyright: ignore[reportArgumentType]
        )

        assert isinstance(vs.resize, VideoResizeSettings)
        assert isinstance(vs.crop, VideoCropSettings)
        assert isinstance(vs.filters, VideoFilterSettings)
        assert vs.resize.width == 1920
        assert vs.crop.top == 8
        assert vs.filters.deblock_enabled is True
        assert vs.has_video_transform() is True


# ===========================================================================
# AudioTrackSettings
# ===========================================================================

class TestAudioTrackSettings:
    def test_requires_stream_index(self):
        a = AudioTrackSettings(stream_index=3)
        assert a.stream_index == 3
        assert a.codec == "copy"
        assert a.bitrate_kbps == 384
        assert a.extract_truehd_core is False
        assert a.input_channels is None
        assert a.input_channel_layout is None

    def test_custom_codec(self):
        a = AudioTrackSettings(stream_index=1, codec="aac", bitrate_kbps=192)
        assert a.codec == "aac"
        assert a.bitrate_kbps == 192

    def test_track_entry_id_is_canonical_guid(self):
        a = AudioTrackSettings(stream_index=1, track_entry_id="track-guid")
        assert a.track_entry_id == "track-guid"


# ===========================================================================
# EncodeConfig
# ===========================================================================

class TestEncodeConfig:
    def test_required_fields(self, tmp_path):
        src = tmp_path / "src.mkv"
        src.touch()
        out = tmp_path / "out.mkv"
        vs = VideoEncodeSettings()
        cfg = EncodeConfig(source=src, output=out, video=vs, audio_tracks=[])
        assert cfg.source == src
        assert cfg.output == out
        assert cfg.video_tracks == [vs]
        assert cfg.copy_subtitles is True
        assert cfg.copy_dv is False
        assert cfg.copy_hdr10plus is False
        assert cfg.dovi_profile == "0"
        assert cfg.work_dir is None
        assert cfg.track_time_offsets == []

    def test_duration_default_none(self, tmp_path):
        cfg = EncodeConfig(
            source=tmp_path / "s.mkv",
            output=tmp_path / "o.mkv",
            video=VideoEncodeSettings(),
            audio_tracks=[],
        )
        assert cfg.duration_s is None

    def test_video_tracks_can_define_primary_legacy_flags(self, tmp_path):
        first = VideoEncodeSettings(codec="libx265", copy_dv=True, dovi_profile="2")
        second = VideoEncodeSettings(codec="copy", copy_hdr10plus=True)
        cfg = EncodeConfig(
            source=tmp_path / "s.mkv",
            output=tmp_path / "o.mkv",
            video_tracks=[first, second],
            audio_tracks=[],
        )
        assert cfg.video is first
        assert cfg.copy_dv is True
        assert cfg.copy_hdr10plus is False
        assert cfg.dovi_profile == "2"


# ===========================================================================
# TrackTimeOffset
# ===========================================================================

class TestTrackTimeOffset:
    def test_fields(self, tmp_path):
        src = tmp_path / "src.mkv"
        tto = TrackTimeOffset(track_type="audio", source_path=src, stream_index=3, offset_ms=-80)
        assert tto.track_type == "audio"
        assert tto.source_path == src
        assert tto.stream_index == 3
        assert tto.offset_ms == -80


class TestVideoTrackEncodePlan:
    def test_defaults(self):
        plan = VideoTrackEncodePlan(track_entry_id="video-1", codec_summary="Copy")
        assert plan.track_entry_id == "video-1"
        assert plan.codec_summary == "Copy"
        assert plan.target_codec == "copy"
        assert plan.hdr_badges == ()
        assert plan.is_modified is False


# ===========================================================================
# EncodePreset
# ===========================================================================

class TestEncodePreset:
    def test_defaults(self):
        p = EncodePreset()
        assert p.name == "Nouveau profil"
        assert p.codec == "libx265"
        assert p.quality_mode == "crf"
        assert p.crf == 18
        assert p.default_audio_codec == "copy"

    def test_to_video_settings(self):
        p = EncodePreset(
            codec="libsvtav1",
            quality_mode="bitrate",
            crf=22,
            bitrate_kbps=6000,
            tonemap_to_sdr=True,
            tonemap_algorithm="reinhard",
        )
        vs = p.to_video_settings()
        assert isinstance(vs, VideoEncodeSettings)
        assert vs.codec == "libsvtav1"
        assert vs.quality_mode == QualityMode.BITRATE
        assert vs.crf == 22
        assert vs.bitrate_kbps == 6000
        assert vs.tonemap_to_sdr is True
        assert vs.tonemap_algorithm == "reinhard"

    def test_to_video_settings_hdr_fields(self):
        p = EncodePreset(
            inject_hdr_meta=True,
            master_display="G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(40000000,50)",
            max_cll="1000,400",
        )
        vs = p.to_video_settings()
        assert vs.inject_hdr_meta is True
        assert vs.master_display != ""
        assert vs.max_cll == "1000,400"

    def test_nested_transform_fields_roundtrip_to_video_settings_and_json(self):
        p = EncodePreset(
            name="Filters",
            resize=VideoResizeSettings(enabled=True, mode="preset", preset="1440p"),
            crop=VideoCropSettings(enabled=True, left=12, right=12),
            filters=VideoFilterSettings(yadif_enabled=True, nlmeans_enabled=True),
        )

        vs = p.to_video_settings()
        as_json = p.to_json_dict()

        assert vs.resize.preset == "1440p"
        assert vs.crop.left == 12
        assert vs.filters.yadif_enabled is True
        assert as_json["resize"]["preset"] == "1440p"
        assert as_json["crop"]["right"] == 12
        assert as_json["filters"]["nlmeans_enabled"] is True

    def test_legacy_preset_without_transform_fields_keeps_defaults(self):
        p = EncodePreset(name="Legacy")
        assert p.resize == VideoResizeSettings()
        assert p.crop == VideoCropSettings()
        assert p.filters == VideoFilterSettings()


# ===========================================================================
# EncodeError
# ===========================================================================

class TestEncodeError:
    def test_is_runtime_error(self):
        err = EncodeError("test")
        assert isinstance(err, RuntimeError)
        assert str(err) == "test"

    def test_can_be_raised_and_caught(self):
        with pytest.raises(EncodeError, match="validation failed"):
            raise EncodeError("validation failed")

    def test_can_be_caught_as_runtime_error(self):
        with pytest.raises(RuntimeError):
            raise EncodeError("generic")


# ===========================================================================
# Package __init__ re-exports
# ===========================================================================

class TestPackageReexports:
    """Vérifie que core.workflows.encode ré-exporte tout correctement."""

    def test_all_symbols_importable_from_package(self):
        from core.workflows.encode import (
            EncodeError, EncodeWorkflow, HardwareEncoderDetector,
            ProfileManager, QualityMode, TrackTimeOffset,
        )
        assert EncodeWorkflow is not None
        assert HardwareEncoderDetector is not None
        assert ProfileManager is not None
        assert QualityMode is not None
        assert EncodeError is not None
        assert TrackTimeOffset is not None
