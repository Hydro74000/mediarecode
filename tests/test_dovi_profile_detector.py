"""Tests du détecteur de sous-profil Dolby Vision."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch


from core.dovi_profile_detector import (
    DoviProfileDetector,
    DoviSubProfile,
)


class TestDoviSubProfileEnum:
    def test_p7_fel_needs_p8_conversion(self):
        assert DoviSubProfile.P7_FEL.needs_p8_conversion is True
        assert DoviSubProfile.P7_FEL.convert_mode == "2"

    def test_p7_mel_needs_p8_conversion(self):
        assert DoviSubProfile.P7_MEL.needs_p8_conversion is True
        assert DoviSubProfile.P7_MEL.convert_mode == "2"

    def test_p5_needs_p8_conversion_via_mode_3(self):
        assert DoviSubProfile.P5.needs_p8_conversion is True
        assert DoviSubProfile.P5.convert_mode == "3"

    def test_p8_1_does_not_need_conversion(self):
        assert DoviSubProfile.P8_1.needs_p8_conversion is False
        assert DoviSubProfile.P8_1.convert_mode is None

    def test_unknown_does_not_need_conversion(self):
        assert DoviSubProfile.UNKNOWN.needs_p8_conversion is False

    def test_labels_are_human_readable(self):
        assert DoviSubProfile.P7_FEL.label == "P7 FEL"
        assert DoviSubProfile.P8_1.label == "P8.1"


class TestDetectFromMediainfo:
    def test_p7_6_fel_with_bl_el_rpu_settings(self):
        mi = {
            "HDR_Format": "Dolby Vision, Version 1.0, dvhe.07.06, BL+EL+RPU",
            "HDR_Format_Profile": "dvhe.07 / 06",
            "HDR_Format_Settings": "BL+EL+RPU",
            "HDR_Format_Compatibility": "",
        }
        result = DoviProfileDetector().detect_from_mediainfo(mi)
        assert result.sub_profile == DoviSubProfile.P7_FEL
        assert result.profile == 7
        assert result.level == 6
        assert result.raw_source == "mediainfo"

    def test_p8_1_hdr10_compatible(self):
        mi = {
            "HDR_Format": "Dolby Vision, Version 1.0, dvhe.08.06, BL+RPU, HDR10 compatible",
            "HDR_Format_Profile": "dvhe.08 / 06",
            "HDR_Format_Settings": "BL+RPU",
            "HDR_Format_Compatibility": "HDR10",
        }
        result = DoviProfileDetector().detect_from_mediainfo(mi)
        assert result.sub_profile == DoviSubProfile.P8_1
        assert result.profile == 8
        assert result.level == 6
        assert result.bl_signal_compat_id == 1

    def test_p8_0(self):
        mi = {
            "HDR_Format": "Dolby Vision, Version 1.0, dvhe.08.06, BL+RPU",
            "HDR_Format_Profile": "dvhe.08 / 06",
            "HDR_Format_Settings": "BL+RPU",
            "HDR_Format_Compatibility": "",
        }
        result = DoviProfileDetector().detect_from_mediainfo(mi)
        assert result.sub_profile == DoviSubProfile.P8_0

    def test_p8_4_hlg_compatible(self):
        mi = {
            "HDR_Format": "Dolby Vision, Version 1.0, dvhe.08.06",
            "HDR_Format_Profile": "dvhe.08 / 06",
            "HDR_Format_Compatibility": "HLG",
        }
        result = DoviProfileDetector().detect_from_mediainfo(mi)
        assert result.sub_profile == DoviSubProfile.P8_4
        assert result.bl_signal_compat_id == 4

    def test_p5(self):
        mi = {
            "HDR_Format": "Dolby Vision, Version 1.0, dvhe.05.06",
            "HDR_Format_Profile": "dvhe.05 / 06",
        }
        result = DoviProfileDetector().detect_from_mediainfo(mi)
        assert result.sub_profile == DoviSubProfile.P5

    def test_no_dolby_vision_returns_unknown(self):
        mi = {"HDR_Format": "HDR10"}
        result = DoviProfileDetector().detect_from_mediainfo(mi)
        assert result.sub_profile == DoviSubProfile.UNKNOWN

    def test_empty_mi_video_returns_unknown(self):
        result = DoviProfileDetector().detect_from_mediainfo({})
        assert result.sub_profile == DoviSubProfile.UNKNOWN
        assert result.raw_source == "mediainfo"

    def test_none_mi_video_returns_unknown(self):
        result = DoviProfileDetector().detect_from_mediainfo(None)
        assert result.sub_profile == DoviSubProfile.UNKNOWN
        assert result.raw_source == "none"


class TestParseDoviToolOutput:
    def test_p8_1_with_compat_id(self):
        text = """
Header:
Profile: 8.1
DV Level: 6
RPU Header:
compatibility id: 1
Frames: 191733
"""
        result = DoviProfileDetector().parse_dovi_tool_output(text)
        assert result.sub_profile == DoviSubProfile.P8_1
        assert result.profile == 8
        assert result.level == 6
        assert result.bl_signal_compat_id == 1

    def test_p7_with_el_flag_classified_as_fel_by_default(self):
        text = """
Profile: 7
DV Level: 6
el flag: 1
bl flag: 1
"""
        result = DoviProfileDetector().parse_dovi_tool_output(text)
        # P7 + EL flag → on classifie FEL par défaut (cas standard remux UHD).
        assert result.sub_profile == DoviSubProfile.P7_FEL

    def test_p7_explicit_mel_marker(self):
        text = """
Profile: 7
DV Level: 6
Subprofile: MEL
el flag: 1
"""
        result = DoviProfileDetector().parse_dovi_tool_output(text)
        assert result.sub_profile == DoviSubProfile.P7_MEL

    def test_p7_explicit_fel_marker(self):
        text = """
Profile: 7
DV Level: 6
Subprofile: FEL
"""
        result = DoviProfileDetector().parse_dovi_tool_output(text)
        assert result.sub_profile == DoviSubProfile.P7_FEL

    def test_p5(self):
        text = "Profile: 5\nDV Level: 4\n"
        result = DoviProfileDetector().parse_dovi_tool_output(text)
        assert result.sub_profile == DoviSubProfile.P5

    def test_p8_compat_id_from_minor_when_no_explicit_compat(self):
        text = "Profile: 8.1\nDV Level: 6\n"
        result = DoviProfileDetector().parse_dovi_tool_output(text)
        assert result.sub_profile == DoviSubProfile.P8_1
        assert result.bl_signal_compat_id == 1

    def test_empty_output_returns_unknown(self):
        result = DoviProfileDetector().parse_dovi_tool_output("")
        assert result.sub_profile == DoviSubProfile.UNKNOWN

    def test_garbage_output_returns_unknown(self):
        result = DoviProfileDetector().parse_dovi_tool_output("hello world")
        assert result.sub_profile == DoviSubProfile.UNKNOWN


class TestDetectFromDoviTool:
    def test_subprocess_called_with_correct_args(self, tmp_path):
        # ``dovi_tool info`` n'accepte qu'un fichier RPU binaire : pour un
        # MKV/HEVC source, on enchaîne extract-rpu (qui crée rpu.bin) puis
        # info -i rpu.bin --summary.
        src = tmp_path / "src.mkv"
        src.touch()
        detector = DoviProfileDetector(dovi_tool_bin="/usr/bin/dovi_tool")
        rpu_bytes = b"FAKE"

        def fake_run(cmd, *args, **kwargs):
            if "extract-rpu" in cmd:
                # Simule la création du fichier RPU.
                out_idx = cmd.index("-o") + 1
                Path(cmd[out_idx]).write_bytes(rpu_bytes)
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            if "info" in cmd:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0,
                    stdout="Profile: 8.1\nDV Level: 6\ncompatibility id: 1\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        with patch("core.dovi_profile_detector.subprocess.run", side_effect=fake_run) as run:
            result = detector.detect_from_dovi_tool(src)

        # Au moins 2 appels : extract-rpu puis info.
        assert run.call_count >= 2
        cmds = [c.args[0] for c in run.call_args_list]
        extract_cmd = next(c for c in cmds if "extract-rpu" in c)
        info_cmd = next(c for c in cmds if "info" in c and "extract-rpu" not in c)
        assert extract_cmd[0] == "/usr/bin/dovi_tool"
        assert "-i" in extract_cmd and str(src) in extract_cmd
        assert info_cmd[0] == "/usr/bin/dovi_tool"
        assert "info" in info_cmd
        assert "--summary" in info_cmd
        assert result.sub_profile == DoviSubProfile.P8_1

    def test_returns_unknown_when_dovi_tool_missing(self, tmp_path):
        detector = DoviProfileDetector()
        with patch("core.dovi_profile_detector.subprocess.run", side_effect=FileNotFoundError):
            result = detector.detect_from_dovi_tool(tmp_path / "src.mkv")
        assert result.sub_profile == DoviSubProfile.UNKNOWN
        assert result.raw_source == "none"
