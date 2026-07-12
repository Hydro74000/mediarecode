"""
tests/test_inspector.py — Tests unitaires pour core/inspector.py

Couverture :
    HDRType :
        - label() retourne les chaînes attendues
        - ordre de priorité (DOLBY_VISION_HDR10PLUS > DOLBY_VISION > …)

    Helpers :
        - _float_or_none / _int_or_none : cas normaux et dégénérés

    VideoTrack / AudioTrack / SubtitleTrack :
        - propriétés calculées (resolution, channels_label, is_hdr)
        - match/case channels_label

    FileInfo :
        - size_human : Go / Mo / Ko / octets
        - duration_human : formatage HH:MM:SS
        - primary_video : premier track ou None

    FileInspector (avec mocks subprocess) :
        - _parse_ffprobe : parsing JSON complet → FileInfo correct
        - _parse_video   : extraction des champs, bit_depth depuis pix_fmt
        - _parse_audio   : extraction channels_label
        - _parse_subtitle: forced/default depuis disposition
        - get_frame_count : retourne int ou None selon la sortie mediainfo
        - detect_hdr_type : les 5 cas (NONE, HDR10, HDR10+, DoVi, DoVi+HDR10+)
        - inspect()       : fichier introuvable → InspectionError
        - _run_ffprobe    : ffprobe absent → InspectionError
        - _run_ffprobe    : returncode != 0 → InspectionError

Exécution :
    pytest tests/test_inspector.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from core.inspector import (
    AudioTrack,
    FileInfo,
    FileInspector,
    HDRType,
    InspectionError,
    SubtitleTrack,
    VideoTrack,
    _float_or_none,
    _int_or_none,
)


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
def inspector() -> FileInspector:
    return FileInspector(ffprobe_bin="ffprobe", mediainfo_bin="mediainfo")


@pytest.fixture
def fake_path(tmp_path: Path) -> Path:
    """Fichier vide mais existant."""
    p = tmp_path / "test.mkv"
    p.touch()
    return p


def _make_ffprobe_output(
    *,
    video_streams: list[dict] | None = None,
    audio_streams: list[dict] | None = None,
    sub_streams:   list[dict] | None = None,
    chapters:      list[dict] | None = None,
    format_info:   dict | None = None,
) -> dict:
    """Construit un dict ffprobe minimal valide."""
    streams = []
    if video_streams:
        streams.extend(video_streams)
    if audio_streams:
        streams.extend(audio_streams)
    if sub_streams:
        streams.extend(sub_streams)
    return {
        "streams": streams,
        "chapters": chapters or [],
        "format": format_info or {
            "format_name": "matroska,webm",
            "duration": "7200.0",
            "size": "10737418240",
            "bit_rate": "11943000",
        },
    }


def _video_stream(**kwargs) -> dict:
    """Stream vidéo ffprobe minimal."""
    base = {
        "index": 0,
        "codec_type": "video",
        "codec_name": "hevc",
        "codec_long_name": "H.265 / HEVC",
        "width": 3840,
        "height": 2160,
        "avg_frame_rate": "24000/1001",
        "pix_fmt": "yuv420p10le",
        "color_primaries": "bt2020",
        "color_transfer": "smpte2084",
        "color_space": "bt2020nc",
        "tags": {},
    }
    base.update(kwargs)
    return base


def _audio_stream(**kwargs) -> dict:
    base = {
        "index": 1,
        "codec_type": "audio",
        "codec_name": "truehd",
        "codec_long_name": "TrueHD",
        "channels": 8,
        "channel_layout": "7.1",
        "sample_rate": "48000",
        "bit_rate": "4000000",
        "tags": {"language": "eng", "title": "Atmos"},
    }
    base.update(kwargs)
    return base


def _sub_stream(**kwargs) -> dict:
    base = {
        "index": 2,
        "codec_type": "subtitle",
        "codec_name": "hdmv_pgs_subtitle",
        "tags": {"language": "fre"},
        "disposition": {"forced": 0, "default": 1},
    }
    base.update(kwargs)
    return base


# ===========================================================================
# HDRType
# ===========================================================================

class TestHDRType:

    def test_labels_are_non_empty(self):
        for hdr in HDRType:
            assert hdr.label()

    def test_label_none_is_sdr(self):
        assert HDRType.NONE.label() == "SDR"

    def test_label_hdr10(self):
        assert HDRType.HDR10.label() == "HDR10"

    def test_label_hdr10plus(self):
        assert "HDR10+" in HDRType.HDR10PLUS.label()

    def test_label_dovi(self):
        assert "Dolby" in HDRType.DOLBY_VISION.label()

    def test_label_dovi_hdr10plus(self):
        label = HDRType.DOLBY_VISION_HDR10PLUS.label()
        assert "Dolby" in label
        assert "HDR10+" in label

    def test_priority_order(self):
        """DOLBY_VISION_HDR10PLUS a la valeur d'enum la plus élevée."""
        values = [h.value for h in HDRType]
        assert HDRType.DOLBY_VISION_HDR10PLUS.value == max(values)

    def test_none_is_lowest(self):
        values = [h.value for h in HDRType]
        assert HDRType.NONE.value == min(values)


# ===========================================================================
# Helpers
# ===========================================================================

class TestHelpers:

    def test_float_or_none_valid(self):
        assert _float_or_none("3.14") == pytest.approx(3.14)

    def test_float_or_none_int_string(self):
        assert _float_or_none("42") == 42.0

    def test_float_or_none_none_input(self):
        assert _float_or_none(None) is None

    def test_float_or_none_invalid_string(self):
        assert _float_or_none("abc") is None

    def test_int_or_none_valid(self):
        assert _int_or_none("1024") == 1024

    def test_int_or_none_none_input(self):
        assert _int_or_none(None) is None

    def test_int_or_none_float_string(self):
        # int("3.14") lève ValueError → None
        assert _int_or_none("3.14") is None

    def test_int_or_none_negative(self):
        assert _int_or_none("-5") == -5


# ===========================================================================
# VideoTrack
# ===========================================================================

class TestVideoTrack:

    def _make(self, **kwargs) -> VideoTrack:
        defaults = dict(
            index=0, codec="hevc", codec_long="H.265",
            width=3840, height=2160, frame_rate="24000/1001",
            bit_depth=10, color_space="yuv420p10le",
            color_primaries="bt2020", color_transfer="smpte2084",
            color_matrix="bt2020nc",
        )
        defaults.update(kwargs)
        return VideoTrack(**cast(Any, defaults))

    def test_resolution(self):
        t = self._make(width=1920, height=1080)
        assert t.resolution == "1920×1080"

    def test_resolution_unknown(self):
        t = self._make(width=None, height=None)
        assert t.resolution == "?"

    def test_is_hdr_true(self):
        t = self._make(hdr_type=HDRType.HDR10)
        assert t.is_hdr is True

    def test_is_hdr_false(self):
        t = self._make(hdr_type=HDRType.NONE)
        assert t.is_hdr is False

    def test_default_hdr_is_none(self):
        t = self._make()
        assert t.hdr_type == HDRType.NONE


# ===========================================================================
# AudioTrack
# ===========================================================================

class TestAudioTrack:

    def _make(self, **kwargs) -> AudioTrack:
        defaults = dict(
            index=1, codec="truehd", codec_long="TrueHD",
            channels=8, channel_layout="7.1",
            sample_rate=48000, bit_rate=4000000,
            language="eng", title="Atmos",
        )
        defaults.update(kwargs)
        return AudioTrack(**cast(Any, defaults))

    def test_channels_label_from_layout(self):
        t = self._make(channel_layout="7.1")
        assert t.channels_label == "7.1"

    def test_channels_label_fallback_8(self):
        t = self._make(channels=8, channel_layout=None)
        assert t.channels_label == "7.1"

    def test_channels_label_fallback_6(self):
        t = self._make(channels=6, channel_layout=None)
        assert t.channels_label == "5.1"

    def test_channels_label_fallback_2(self):
        t = self._make(channels=2, channel_layout=None)
        assert t.channels_label == "Stereo"

    def test_channels_label_fallback_1(self):
        t = self._make(channels=1, channel_layout=None)
        assert t.channels_label == "Mono"

    def test_channels_label_fallback_none(self):
        t = self._make(channels=None, channel_layout=None)
        assert t.channels_label == "?"


# ===========================================================================
# FileInfo
# ===========================================================================

class TestFileInfo:

    def _make(self, **kwargs) -> FileInfo:
        defaults = dict(
            path=Path("/tmp/test.mkv"),
            format="matroska,webm",
            duration_s=7200.0,
            size_bytes=10 * (1 << 30),
            bit_rate=11943000,
        )
        defaults.update(kwargs)
        return FileInfo(**cast(Any, defaults))

    def test_size_human_go(self):
        info = self._make(size_bytes=10 * (1 << 30))
        assert "Go" in info.size_human

    def test_size_human_mo(self):
        info = self._make(size_bytes=500 * (1 << 20))
        assert "Mo" in info.size_human

    def test_size_human_ko(self):
        info = self._make(size_bytes=800 * (1 << 10))
        assert "Ko" in info.size_human

    def test_size_human_none(self):
        info = self._make(size_bytes=None)
        assert info.size_human == "?"

    def test_duration_human_format(self):
        info = self._make(duration_s=3661.0)  # 1h 1m 1s
        assert info.duration_human == "01:01:01"

    def test_duration_human_none(self):
        info = self._make(duration_s=None)
        assert info.duration_human == "?"

    def test_primary_video_none_when_empty(self):
        info = self._make()
        assert info.primary_video is None

    def test_primary_video_returns_first(self):
        info = self._make()
        t = VideoTrack(
            index=0, codec="hevc", codec_long="H.265",
            width=3840, height=2160, frame_rate=None,
            bit_depth=10, color_space=None, color_primaries=None,
            color_transfer=None, color_matrix=None,
        )
        info.video_tracks.append(t)
        assert info.primary_video is t


# ===========================================================================
# FileInspector._parse_ffprobe
# ===========================================================================

class TestParseFFprobe:

    @pytest.fixture(autouse=True)
    def setup(self, inspector):
        self.insp = inspector
        self.path = Path("/tmp/fake.mkv")

    def test_parse_basic_counts(self):
        raw = _make_ffprobe_output(
            video_streams=[_video_stream()],
            audio_streams=[_audio_stream()],
            sub_streams=[_sub_stream()],
        )
        info = self.insp._parse_ffprobe(self.path, raw)
        assert len(info.video_tracks)    == 1
        assert len(info.audio_tracks)    == 1
        assert len(info.subtitle_tracks) == 1

    def test_parse_format_fields(self):
        raw = _make_ffprobe_output(format_info={
            "format_name": "matroska,webm",
            "duration": "3600.5",
            "size": "5368709120",
            "bit_rate": "12000000",
        })
        info = self.insp._parse_ffprobe(self.path, raw)
        assert info.format == "matroska,webm"
        assert info.duration_s == pytest.approx(3600.5)
        assert info.size_bytes == 5368709120
        assert info.bit_rate   == 12000000

    def test_parse_video_codec(self):
        raw = _make_ffprobe_output(video_streams=[_video_stream(codec_name="av1")])
        info = self.insp._parse_ffprobe(self.path, raw)
        assert info.video_tracks[0].codec == "av1"

    def test_parse_video_resolution(self):
        raw = _make_ffprobe_output(video_streams=[_video_stream(width=1920, height=1080)])
        info = self.insp._parse_ffprobe(self.path, raw)
        assert info.video_tracks[0].resolution == "1920×1080"

    def test_parse_video_bitdepth_from_pixfmt(self):
        """yuv420p10le → bit_depth=10"""
        raw = _make_ffprobe_output(video_streams=[_video_stream(pix_fmt="yuv420p10le")])
        info = self.insp._parse_ffprobe(self.path, raw)
        assert info.video_tracks[0].bit_depth == 10

    def test_parse_video_bitdepth_8(self):
        raw = _make_ffprobe_output(video_streams=[_video_stream(pix_fmt="yuv420p")])
        info = self.insp._parse_ffprobe(self.path, raw)
        # "yuv420p" → pas de suffixe numérique valide → None
        assert info.video_tracks[0].bit_depth is None

    def test_parse_audio_channels(self):
        raw = _make_ffprobe_output(audio_streams=[_audio_stream(channels=6)])
        info = self.insp._parse_ffprobe(self.path, raw)
        assert info.audio_tracks[0].channels == 6

    def test_parse_audio_language(self):
        raw = _make_ffprobe_output(audio_streams=[
            _audio_stream(tags={"language": "fra", "title": "VF"})
        ])
        info = self.insp._parse_ffprobe(self.path, raw)
        assert info.audio_tracks[0].language == "fra"
        assert info.audio_tracks[0].title    == "VF"

    def test_parse_subtitle_forced(self):
        raw = _make_ffprobe_output(sub_streams=[
            _sub_stream(disposition={"forced": 1, "default": 0})
        ])
        info = self.insp._parse_ffprobe(self.path, raw)
        assert info.subtitle_tracks[0].forced  is True
        assert info.subtitle_tracks[0].default is False

    def test_parse_subtitle_element_count_from_ffprobe_frames(self):
        raw = _make_ffprobe_output(sub_streams=[
            _sub_stream(nb_frames="1292")
        ])
        info = self.insp._parse_ffprobe(self.path, raw)
        assert info.subtitle_tracks[0].element_count == 1292

    def test_parse_subtitle_element_count_from_ffprobe_tags(self):
        raw = _make_ffprobe_output(sub_streams=[
            _sub_stream(tags={"language": "fre", "NUMBER_OF_FRAMES": "1 292"})
        ])
        info = self.insp._parse_ffprobe(self.path, raw)
        assert info.subtitle_tracks[0].element_count == 1292

    def test_parse_chapters(self):
        raw = _make_ffprobe_output(chapters=[
            {"start_time": "0.000000", "tags": {"title": "Chapitre 1"}},
            {"start_time": "300.000000", "tags": {"title": "Chapitre 2"}},
        ])
        info = self.insp._parse_ffprobe(self.path, raw)
        assert info.chapters is not None
        assert info.chapters.count == 2
        names = [e.name for e in info.chapters.entries]
        assert "Chapitre 1" in names
        assert "Chapitre 2" in names

    def test_parse_chapters_timecodes(self):
        """Les timecodes des chapitres sont correctement parsés en secondes."""
        raw = _make_ffprobe_output(chapters=[
            {"start_time": "0.000000",   "tags": {"title": "Intro"}},
            {"start_time": "3661.500000", "tags": {"title": "Acte 2"}},
        ])
        info = self.insp._parse_ffprobe(self.path, raw)
        assert info.chapters is not None
        tcs = [e.timecode_s for e in info.chapters.entries]
        assert tcs[0] == pytest.approx(0.0)
        assert tcs[1] == pytest.approx(3661.5)

    def test_parse_no_chapters(self):
        raw = _make_ffprobe_output()
        info = self.insp._parse_ffprobe(self.path, raw)
        assert info.chapters is None

    def test_parse_fps_fraction(self):
        raw = _make_ffprobe_output(video_streams=[
            _video_stream(avg_frame_rate="24000/1001")
        ])
        info = self.insp._parse_ffprobe(self.path, raw)
        assert info.video_tracks[0].frame_rate == "24000/1001"

    def test_parse_fps_zero_becomes_none(self):
        raw = _make_ffprobe_output(video_streams=[
            _video_stream(avg_frame_rate="0/0", r_frame_rate="0/0")
        ])
        info = self.insp._parse_ffprobe(self.path, raw)
        assert info.video_tracks[0].frame_rate is None

    def test_parse_multiple_audio_tracks(self):
        raw = _make_ffprobe_output(audio_streams=[
            _audio_stream(index=1),
            _audio_stream(index=2, tags={"language": "fre"}),
        ])
        info = self.insp._parse_ffprobe(self.path, raw)
        assert len(info.audio_tracks) == 2


# ===========================================================================
# FileInspector.get_frame_count
# ===========================================================================

class TestGetFrameCount:

    @pytest.fixture(autouse=True)
    def setup(self, inspector, fake_path):
        self.insp = inspector
        self.path = fake_path

    def _mock_run(self, stdout: str, returncode: int = 0):
        result = MagicMock()
        result.stdout    = stdout
        result.returncode = returncode
        return result

    def test_returns_int_on_valid_output(self):
        with patch("subprocess.run", return_value=self._mock_run("142857\n")):
            assert self.insp.get_frame_count(self.path) == 142857

    def test_returns_none_on_empty_output(self):
        with patch("subprocess.run", return_value=self._mock_run("")):
            assert self.insp.get_frame_count(self.path) is None

    def test_returns_none_on_non_numeric_output(self):
        with patch("subprocess.run", return_value=self._mock_run("N/A")):
            assert self.insp.get_frame_count(self.path) is None

    def test_returns_none_when_mediainfo_missing(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert self.insp.get_frame_count(self.path) is None

    def test_strips_whitespace(self):
        with patch("subprocess.run", return_value=self._mock_run("  48000  \n")):
            assert self.insp.get_frame_count(self.path) == 48000


# ===========================================================================
# FileInspector.detect_hdr_type
# ===========================================================================

class TestDetectHDRType:

    @pytest.fixture(autouse=True)
    def setup(self, inspector, fake_path):
        self.insp = inspector
        self.path = fake_path

    def _patch_ffprobe(self, raw: dict):
        """Patch _run_ffprobe pour retourner le dict donné."""
        return patch.object(self.insp, "_run_ffprobe", return_value=raw)

    def _patch_mi(self, dovi: bool = False, hdr10plus: bool = False, responded: bool = True):
        return patch.object(
            self.insp, "_mediainfo_hdr_flags", return_value=(dovi, hdr10plus, responded)
        )

    def test_sdr_returns_none(self):
        raw = _make_ffprobe_output(video_streams=[
            _video_stream(color_transfer="bt709", pix_fmt="yuv420p")
        ])
        with self._patch_ffprobe(raw), self._patch_mi():
            assert self.insp.detect_hdr_type(self.path) == HDRType.NONE

    def test_hdr10_detected(self):
        raw = _make_ffprobe_output(video_streams=[_video_stream(
            color_transfer="smpte2084",
            side_data_list=[
                {"side_data_type": "Mastering display metadata"},
                {"side_data_type": "Content light level metadata"},
            ],
        )])
        with self._patch_ffprobe(raw), self._patch_mi():
            assert self.insp.detect_hdr_type(self.path) == HDRType.HDR10

    def test_hdr10plus_detected(self):
        raw = _make_ffprobe_output(video_streams=[_video_stream(
            color_transfer="smpte2084",
            side_data_list=[
                {"side_data_type": "Mastering display metadata"},
                {"side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"},
            ],
        )])
        with self._patch_ffprobe(raw), self._patch_mi():
            assert self.insp.detect_hdr_type(self.path) == HDRType.HDR10PLUS

    def test_dolby_vision_detected_via_side_data(self):
        raw = _make_ffprobe_output(video_streams=[_video_stream(
            color_transfer="smpte2084",
            side_data_list=[
                {"side_data_type": "Mastering display metadata"},
                {"side_data_type": "DOVI configuration record"},
            ],
        )])
        with self._patch_ffprobe(raw), self._patch_mi():
            assert self.insp.detect_hdr_type(self.path) == HDRType.DOLBY_VISION

    def test_dolby_vision_hdr10plus_detected(self):
        raw = _make_ffprobe_output(video_streams=[_video_stream(
            color_transfer="smpte2084",
            side_data_list=[
                {"side_data_type": "Mastering display metadata"},
                {"side_data_type": "DOVI configuration record"},
                {"side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"},
            ],
        )])
        with self._patch_ffprobe(raw), self._patch_mi():
            assert self.insp.detect_hdr_type(self.path) == HDRType.DOLBY_VISION_HDR10PLUS

    def test_dovi_via_mediainfo_fallback(self):
        """DoVi non détecté par ffprobe mais trouvé via mediainfo."""
        raw = _make_ffprobe_output(video_streams=[_video_stream(
            color_transfer="smpte2084",
            side_data_list=[{"side_data_type": "Mastering display metadata"}],
        )])
        with self._patch_ffprobe(raw), self._patch_mi(dovi=True):
            assert self.insp.detect_hdr_type(self.path) == HDRType.DOLBY_VISION

    def test_hdr10plus_via_mediainfo_fallback(self):
        raw = _make_ffprobe_output(video_streams=[_video_stream(
            color_transfer="smpte2084",
            side_data_list=[{"side_data_type": "Mastering display metadata"}],
        )])
        with self._patch_ffprobe(raw), self._patch_mi(hdr10plus=True):
            assert self.insp.detect_hdr_type(self.path) == HDRType.HDR10PLUS

    def test_hdr10plus_via_ffprobe_frame_fallback(self):
        # mediainfo absent / silencieux : seul cas où le frame-level fallback
        # se déclenche encore (économie de 3-6 s sur les fichiers HDR10/DV).
        raw = _make_ffprobe_output(video_streams=[_video_stream(
            color_transfer="smpte2084",
            side_data_list=[{"side_data_type": "DOVI configuration record"}],
        )])
        with self._patch_ffprobe(raw), self._patch_mi(responded=False), patch.object(
            self.insp,
            "_ffprobe_frame_dynamic_hdr_flags",
            return_value=(True, True),
        ):
            assert self.insp.detect_hdr_type(self.path) == HDRType.DOLBY_VISION_HDR10PLUS

    def test_frame_fallback_skipped_when_mediainfo_responded(self):
        # Même cas que ci-dessus mais mediainfo a répondu (négatif pour HDR10+)
        # → le frame-level NE doit PAS être appelé même si on garde un mock.
        raw = _make_ffprobe_output(video_streams=[_video_stream(
            color_transfer="smpte2084",
            side_data_list=[{"side_data_type": "DOVI configuration record"}],
        )])
        frame_mock = MagicMock(return_value=(True, True))
        with self._patch_ffprobe(raw), self._patch_mi(responded=True), patch.object(
            self.insp, "_ffprobe_frame_dynamic_hdr_flags", frame_mock,
        ):
            assert self.insp.detect_hdr_type(self.path) == HDRType.DOLBY_VISION
            frame_mock.assert_not_called()

    def test_hlg_detected_as_hlg(self):
        raw = _make_ffprobe_output(video_streams=[_video_stream(
            color_transfer="arib-std-b67",
            side_data_list=[],
        )])
        with self._patch_ffprobe(raw), self._patch_mi():
            assert self.insp.detect_hdr_type(self.path) == HDRType.HLG

    def test_attached_pic_is_ignored_for_hdr_detection(self):
        raw = _make_ffprobe_output(video_streams=[
            _video_stream(
                index=0,
                codec_name="mjpeg",
                color_transfer="bt709",
                side_data_list=[],
                disposition={"attached_pic": 1},
            ),
            _video_stream(
                index=1,
                color_transfer="smpte2084",
                side_data_list=[
                    {"side_data_type": "DOVI configuration record"},
                    {"side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"},
                ],
            ),
        ])
        with self._patch_ffprobe(raw), self._patch_mi():
            assert self.insp.detect_hdr_type(self.path) == HDRType.DOLBY_VISION_HDR10PLUS

    def test_dynamic_hdr_can_be_detected_from_secondary_video_stream(self):
        raw = _make_ffprobe_output(video_streams=[
            _video_stream(
                index=0,
                color_transfer="smpte2084",
                side_data_list=[{"side_data_type": "Mastering display metadata"}],
            ),
            _video_stream(
                index=1,
                color_transfer="smpte2084",
                side_data_list=[{"side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"}],
            ),
        ])
        with self._patch_ffprobe(raw), self._patch_mi():
            assert self.insp.detect_hdr_type(self.path) == HDRType.HDR10PLUS

    def test_no_video_stream_returns_none(self):
        raw = _make_ffprobe_output(video_streams=[])
        with self._patch_ffprobe(raw), self._patch_mi():
            assert self.insp.detect_hdr_type(self.path) == HDRType.NONE

    def test_ffprobe_failure_returns_none(self):
        with patch.object(self.insp, "_run_ffprobe", side_effect=InspectionError(self.path, "échec")):
            assert self.insp.detect_hdr_type(self.path) == HDRType.NONE


# ===========================================================================
# FileInspector._run_ffprobe
# ===========================================================================

class TestRunFFprobe:

    @pytest.fixture(autouse=True)
    def setup(self, inspector, fake_path):
        self.insp = inspector
        self.path = fake_path

    def test_raises_when_ffprobe_missing(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(InspectionError) as exc:
                self.insp._run_ffprobe(self.path)
            assert "ffprobe" in str(exc.value).lower()

    def test_raises_on_nonzero_returncode(self):
        result = MagicMock()
        result.returncode = 1
        result.stderr     = "no such file"
        result.stdout     = ""
        with patch("subprocess.run", return_value=result):
            with pytest.raises(InspectionError):
                self.insp._run_ffprobe(self.path)

    def test_raises_on_invalid_json(self):
        result = MagicMock()
        result.returncode = 0
        result.stdout     = "ce n'est pas du json {"
        result.stderr     = ""
        with patch("subprocess.run", return_value=result):
            with pytest.raises(InspectionError):
                self.insp._run_ffprobe(self.path)

    def test_returns_dict_on_valid_json(self):
        payload = {"streams": [], "format": {}, "chapters": []}
        result  = MagicMock()
        result.returncode = 0
        result.stdout     = json.dumps(payload)
        result.stderr     = ""
        with patch("subprocess.run", return_value=result):
            out = self.insp._run_ffprobe(self.path)
        assert out == payload

    def test_emits_verbose_lines_for_ffprobe_probe(self, fake_path):
        verbose_lines: list[str] = []
        inspector = FileInspector(
            ffprobe_bin="ffprobe",
            mediainfo_bin="mediainfo",
            verbose_output=verbose_lines.append,
        )
        payload = {"streams": [], "format": {}, "chapters": []}
        result = MagicMock()
        result.returncode = 0
        result.stdout = json.dumps(payload)
        result.stderr = ""

        with patch("subprocess.run", return_value=result):
            out = inspector._run_ffprobe(fake_path)

        assert out == payload
        assert any(line.startswith("$ ffprobe ") for line in verbose_lines)
        assert any("ffprobe rc=0" in line for line in verbose_lines)
        assert any("ffprobe JSON parsé" in line for line in verbose_lines)


# ===========================================================================
# FileInspector.inspect — intégration légère
# ===========================================================================

class TestInspect:

    @pytest.fixture(autouse=True)
    def setup(self, inspector, fake_path):
        self.insp = inspector
        self.path = fake_path

    def test_raises_when_file_not_found(self, tmp_path):
        missing = tmp_path / "missing.mkv"
        with pytest.raises(InspectionError) as exc:
            self.insp.inspect(missing)
        assert "introuvable" in str(exc.value)

    def test_returns_file_info(self):
        raw = _make_ffprobe_output(
            video_streams=[_video_stream()],
            audio_streams=[_audio_stream()],
        )
        payload = MagicMock()
        payload.returncode = 0
        payload.stdout     = json.dumps(raw)
        payload.stderr     = ""

        mi_payload = MagicMock()
        mi_payload.returncode = 0
        mi_payload.stdout     = "142857"

        mi_hdr = MagicMock()
        mi_hdr.returncode = 0
        mi_hdr.stdout     = ""

        def fake_run(cmd, **kwargs):
            if "ffprobe" in cmd[0]:
                return payload
            return mi_payload if "%FrameCount%" in " ".join(cmd) else mi_hdr

        with patch("subprocess.run", side_effect=fake_run):
            info = self.insp.inspect(self.path)

        assert isinstance(info, FileInfo)
        assert len(info.video_tracks) == 1
        assert len(info.audio_tracks) == 1
        assert info.frame_count == 142857

    def test_inspect_prefers_mediainfo_for_video_bitrate_and_subtitle_elements(self):
        info = FileInfo(
            path=self.path,
            format="mov,mp4,m4a,3gp,3g2,mj2",
            duration_s=None,
            size_bytes=None,
            bit_rate=None,
            video_tracks=[
                VideoTrack(
                    index=0,
                    codec="hevc",
                    codec_long="H.265",
                    width=1920,
                    height=1080,
                    frame_rate="24000/1001",
                    bit_depth=10,
                    color_space="yuv420p10le",
                    color_primaries=None,
                    color_transfer=None,
                    color_matrix=None,
                    bit_rate=1_000_000,
                )
            ],
            subtitle_tracks=[
                SubtitleTrack(
                    index=2,
                    codec="subrip",
                    language="fra",
                    title=None,
                )
            ],
        )
        mi_data = {
            "media": {
                "track": [
                    {"@type": "Video", "BitRate": "22 000 000", "FrameCount": "240"},
                    {"@type": "Text", "ElementCount": "1 292"},
                ]
            }
        }

        with (
            patch.object(self.insp, "_run_ffprobe", return_value={}),
            patch.object(self.insp, "_parse_ffprobe", return_value=info),
            patch.object(self.insp, "_run_mediainfo_json", return_value=mi_data),
            patch.object(self.insp, "get_frame_count", side_effect=AssertionError("mediainfo JSON must be reused")),
            patch.object(self.insp, "_detect_hdr_from_raw", return_value=HDRType.NONE),
        ):
            out = self.insp.inspect(self.path)

        assert out.video_tracks[0].bit_rate == 22_000_000
        assert out.subtitle_tracks[0].element_count == 1292
        assert out.frame_count == 240
        assert out.mediainfo_json == mi_data

    def test_inspect_continues_when_mediainfo_fails(self):
        """inspect() ne plante pas si mediainfo est absent."""
        raw = _make_ffprobe_output(video_streams=[_video_stream()])

        ffprobe_result = MagicMock()
        ffprobe_result.returncode = 0
        ffprobe_result.stdout     = json.dumps(raw)
        ffprobe_result.stderr     = ""

        def fake_run(cmd, **kwargs):
            if "ffprobe" in cmd[0]:
                return ffprobe_result
            raise FileNotFoundError("mediainfo not found")

        with patch("subprocess.run", side_effect=fake_run):
            info = self.insp.inspect(self.path)

        assert info.frame_count is None     # mediainfo absent → None
        assert isinstance(info, FileInfo)   # pas d'exception levée

    def test_inspect_emits_verbose_start_and_summary(self):
        verbose_lines: list[str] = []
        inspector = FileInspector(
            ffprobe_bin="ffprobe",
            mediainfo_bin="mediainfo",
            verbose_output=verbose_lines.append,
        )
        info = FileInfo(
            path=self.path,
            format="matroska,webm",
            duration_s=None,
            size_bytes=None,
            bit_rate=None,
            video_tracks=[
                VideoTrack(
                    index=0,
                    codec="hevc",
                    codec_long="H.265",
                    width=1920,
                    height=1080,
                    frame_rate="24000/1001",
                    bit_depth=10,
                    color_space="yuv420p10le",
                    color_primaries="bt2020",
                    color_transfer="smpte2084",
                    color_matrix="bt2020nc",
                )
            ],
            audio_tracks=[
                AudioTrack(
                    index=1,
                    codec="eac3",
                    codec_long="E-AC-3",
                    channels=6,
                    channel_layout="5.1",
                    sample_rate=48000,
                    bit_rate=640000,
                    language="fra",
                    title="VF",
                )
            ],
        )

        with (
            patch.object(inspector, "_run_ffprobe", return_value={}),
            patch.object(inspector, "_parse_ffprobe", return_value=info),
            patch.object(inspector, "get_frame_count", return_value=1200),
            patch.object(inspector, "_extract_mkv_track_data_from_raw", return_value=(0, {})),
            patch.object(inspector, "_detect_hdr_from_raw", return_value=HDRType.HDR10),
        ):
            out = inspector.inspect(self.path)

        assert out.frame_count == 1200
        assert any("Inspection démarrée" in line for line in verbose_lines)
        assert any("Inspection terminée" in line and "HDR=HDR10" in line for line in verbose_lines)

    def _audio_only_info(self, *, fmt: str, language: str, title: str = "") -> FileInfo:
        return FileInfo(
            path=self.path,
            format=fmt,
            duration_s=None,
            size_bytes=None,
            bit_rate=None,
            audio_tracks=[
                AudioTrack(
                    index=1,
                    codec="aac",
                    codec_long="AAC",
                    channels=2,
                    channel_layout="stereo",
                    sample_rate=48000,
                    bit_rate=192000,
                    language=language,
                    title=title,
                )
            ],
        )

    def test_inspect_regionalizes_short_ietf_from_track_enrichment(self):
        """
        MKV + language_ietf court (en) -> variante régionale par défaut (en-US).
        """
        info = self._audio_only_info(fmt="matroska,webm", language="eng")

        with (
            patch.object(self.insp, "_run_ffprobe", return_value={}),
            patch.object(self.insp, "_parse_ffprobe", return_value=info),
            patch.object(self.insp, "get_frame_count", return_value=None),
            patch.object(self.insp, "_extract_mkv_track_data_from_raw", return_value=(0, {1: "en"})),
        ):
            out = self.insp.inspect(self.path)

        assert out.audio_tracks[0].language == "en-US"

    def test_inspect_regionalizes_short_ietf_with_title_hint(self):
        """
        RFC 5646 court (fr) + titre indicatif -> variante régionale inférée.
        """
        info = self._audio_only_info(
            fmt="mov,mp4,m4a,3gp,3g2,mj2",
            language="fr",
            title="Français (Canadien)",
        )

        with (
            patch.object(self.insp, "_run_ffprobe", return_value={}),
            patch.object(self.insp, "_parse_ffprobe", return_value=info),
            patch.object(self.insp, "get_frame_count", return_value=None),
        ):
            out = self.insp.inspect(self.path)

        assert out.audio_tracks[0].language == "fr-CA"

    def test_inspect_defaults_eng_to_en_us_when_region_missing(self):
        """
        ISO 639-2 'eng' sans précision régionale -> en-US par défaut.
        """
        info = self._audio_only_info(
            fmt="mov,mp4,m4a,3gp,3g2,mj2",
            language="eng",
        )

        with (
            patch.object(self.insp, "_run_ffprobe", return_value={}),
            patch.object(self.insp, "_parse_ffprobe", return_value=info),
            patch.object(self.insp, "get_frame_count", return_value=None),
        ):
            out = self.insp.inspect(self.path)

        assert out.audio_tracks[0].language == "en-US"

    def test_inspect_keeps_already_regional_tag(self):
        """
        Un tag déjà régional issu de l'enrichissement piste est conservé tel quel.
        """
        info = self._audio_only_info(fmt="matroska,webm", language="eng")

        with (
            patch.object(self.insp, "_run_ffprobe", return_value={}),
            patch.object(self.insp, "_parse_ffprobe", return_value=info),
            patch.object(self.insp, "get_frame_count", return_value=None),
            patch.object(self.insp, "_extract_mkv_track_data_from_raw", return_value=(0, {1: "en-GB"})),
        ):
            out = self.insp.inspect(self.path)

        assert out.audio_tracks[0].language == "en-GB"

    def test_inspect_maps_und_to_none(self):
        """
        'und' reste traité comme indéfini (None dans le modèle interne).
        """
        info = self._audio_only_info(
            fmt="mov,mp4,m4a,3gp,3g2,mj2",
            language="und",
        )

        with (
            patch.object(self.insp, "_run_ffprobe", return_value={}),
            patch.object(self.insp, "_parse_ffprobe", return_value=info),
            patch.object(self.insp, "get_frame_count", return_value=None),
        ):
            out = self.insp.inspect(self.path)

        assert out.audio_tracks[0].language is None

    def test_inspect_canonicalizes_case_for_known_ietf(self):
        """
        Les balises connues sont normalisées avec la casse canonique.
        """
        info = self._audio_only_info(
            fmt="mov,mp4,m4a,3gp,3g2,mj2",
            language="EN-us",
        )

        with (
            patch.object(self.insp, "_run_ffprobe", return_value={}),
            patch.object(self.insp, "_parse_ffprobe", return_value=info),
            patch.object(self.insp, "get_frame_count", return_value=None),
        ):
            out = self.insp.inspect(self.path)

        assert out.audio_tracks[0].language == "en-US"


class TestMkvTrackDataFromFfprobe:

    def test_prefers_language_ietf_and_counts_non_title_tags(self, inspector, fake_path):
        # La méthode opère désormais sur le ``raw`` ffprobe déjà parsé
        # (zéro appel subprocess). Reproduit le contenu attendu directement.
        raw = {
            "format": {
                "tags": {
                    "TITLE": "Movie",
                    "GENRE": "Action",
                    "DATE_RELEASED": "2024",
                }
            },
            "streams": [
                {"index": 0, "tags": {"language": "eng"}},
                {"index": 1, "tags": {"language-ietf": "fr-CA", "language": "fra"}},
            ],
        }

        tag_count, lang_map = inspector._extract_mkv_track_data_from_raw(raw)

        assert tag_count == 2
        assert lang_map == {0: "eng", 1: "fr-CA"}

    def test_returns_empty_when_raw_is_empty(self, inspector, fake_path):
        tag_count, lang_map = inspector._extract_mkv_track_data_from_raw({})
        assert tag_count == 0
        assert lang_map == {}
