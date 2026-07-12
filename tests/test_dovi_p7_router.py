"""Tests du routeur P7 / P5 → P8.1."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.dovi_profile_detector import DoviSubProfile
from core.workflows.encode.runtime.dovi_p7_router import (
    DoviP7Router,
    P7RoutingDecision,
)


# Mediainfo dicts factices ----------------------------------------------------


def _mi_p7_fel() -> dict:
    return {
        "HDR_Format": "Dolby Vision, Version 1.0, dvhe.07.06, BL+EL+RPU",
        "HDR_Format_Profile": "dvhe.07 / 06",
        "HDR_Format_Settings": "BL+EL+RPU",
    }


def _mi_p8_1() -> dict:
    return {
        "HDR_Format": "Dolby Vision, Version 1.0, dvhe.08.06, BL+RPU, HDR10 compatible",
        "HDR_Format_Profile": "dvhe.08 / 06",
        "HDR_Format_Settings": "BL+RPU",
        "HDR_Format_Compatibility": "HDR10",
    }


def _mi_p5() -> dict:
    return {
        "HDR_Format": "Dolby Vision, Version 1.0, dvhe.05.06",
        "HDR_Format_Profile": "dvhe.05 / 06",
    }


def _mi_hdr10_only() -> dict:
    return {"HDR_Format": "HDR10"}


# Tests décision -------------------------------------------------------------


class TestDoviP7RouterAnalyze:
    def test_p7_fel_triggers_conversion_with_mode_2(self, tmp_path):
        router = DoviP7Router()
        decision = router.analyze(
            source=tmp_path / "src.hevc",
            mi_video=_mi_p7_fel(),
            fallback_to_dovi_tool=False,
        )
        assert decision.conversion_needed is True
        assert decision.sub_profile == DoviSubProfile.P7_FEL
        assert decision.convert_mode == "2"
        assert "P7 FEL" in decision.reason

    def test_p8_1_skips_conversion(self, tmp_path):
        router = DoviP7Router()
        decision = router.analyze(
            source=tmp_path / "src.hevc",
            mi_video=_mi_p8_1(),
            fallback_to_dovi_tool=False,
        )
        assert decision.conversion_needed is False
        assert decision.sub_profile == DoviSubProfile.P8_1
        assert decision.convert_mode is None

    def test_p5_triggers_conversion_with_mode_3(self, tmp_path):
        router = DoviP7Router()
        decision = router.analyze(
            source=tmp_path / "src.hevc",
            mi_video=_mi_p5(),
            fallback_to_dovi_tool=False,
        )
        assert decision.conversion_needed is True
        assert decision.sub_profile == DoviSubProfile.P5
        assert decision.convert_mode == "3"

    def test_hdr10_only_skips_conversion(self, tmp_path):
        router = DoviP7Router()
        decision = router.analyze(
            source=tmp_path / "src.hevc",
            mi_video=_mi_hdr10_only(),
            fallback_to_dovi_tool=False,
        )
        assert decision.conversion_needed is False
        assert decision.sub_profile == DoviSubProfile.UNKNOWN

    def test_no_mediainfo_unknown_no_fallback(self, tmp_path):
        router = DoviP7Router()
        decision = router.analyze(
            source=tmp_path / "src.hevc",
            mi_video=None,
            fallback_to_dovi_tool=False,
        )
        assert decision.conversion_needed is False
        assert decision.sub_profile == DoviSubProfile.UNKNOWN


# Tests exécution -----------------------------------------------------------


class TestDoviP7RouterExecuteConversion:
    def test_p7_fel_command_contains_discard(self, tmp_path):
        router = DoviP7Router()
        decision = P7RoutingDecision(
            conversion_needed=True,
            sub_profile=DoviSubProfile.P7_FEL,
            convert_mode="2",
            reason="test",
        )
        captured: list[list[str]] = []

        def _fake_run(cmd):
            captured.append(list(cmd))
            return ""

        out_path = router.execute_conversion(
            source=tmp_path / "src.hevc",
            output_dir=tmp_path,
            run_cmd=_fake_run,
            dovi_tool_bin="/usr/bin/dovi_tool",
            decision=decision,
        )

        assert out_path == tmp_path / "source_p8.hevc"
        assert len(captured) == 1
        cmd = captured[0]
        assert cmd[:5] == ["/usr/bin/dovi_tool", "-m", "2", "convert", "--discard"]
        assert "-i" in cmd and str(tmp_path / "src.hevc") in cmd
        assert "-o" in cmd and str(out_path) in cmd

    def test_p7_mel_also_uses_discard(self, tmp_path):
        router = DoviP7Router()
        decision = P7RoutingDecision(
            conversion_needed=True,
            sub_profile=DoviSubProfile.P7_MEL,
            convert_mode="2",
            reason="test",
        )
        captured: list[list[str]] = []

        router.execute_conversion(
            source=tmp_path / "src.hevc",
            output_dir=tmp_path,
            run_cmd=lambda cmd: captured.append(list(cmd)),
            dovi_tool_bin="dovi_tool",
            decision=decision,
        )
        assert "--discard" in captured[0]

    def test_p5_command_converts_rpu_without_discard(self, tmp_path):
        router = DoviP7Router()
        decision = P7RoutingDecision(
            conversion_needed=True,
            sub_profile=DoviSubProfile.P5,
            convert_mode="3",
            reason="test",
        )
        captured: list[list[str]] = []
        router.execute_conversion(
            source=tmp_path / "src.hevc",
            output_dir=tmp_path,
            run_cmd=lambda cmd: captured.append(list(cmd)),
            dovi_tool_bin="dovi_tool",
            decision=decision,
        )
        assert captured[0][:4] == ["dovi_tool", "-m", "3", "convert"]
        assert "--discard" not in captured[0]

    def test_raises_when_conversion_not_needed(self, tmp_path):
        router = DoviP7Router()
        decision = P7RoutingDecision(
            conversion_needed=False,
            sub_profile=DoviSubProfile.P8_1,
            convert_mode=None,
            reason="ok",
        )
        with pytest.raises(ValueError, match="conversion_needed=False"):
            router.execute_conversion(
                source=tmp_path / "src.hevc",
                output_dir=tmp_path,
                run_cmd=lambda cmd: None,
                dovi_tool_bin="dovi_tool",
                decision=decision,
            )

    def test_raises_when_convert_mode_missing(self, tmp_path):
        router = DoviP7Router()
        decision = P7RoutingDecision(
            conversion_needed=True,
            sub_profile=DoviSubProfile.P7_FEL,
            convert_mode=None,  # incohérent
            reason="bug",
        )
        with pytest.raises(ValueError, match="convert_mode"):
            router.execute_conversion(
                source=tmp_path / "src.hevc",
                output_dir=tmp_path,
                run_cmd=lambda cmd: None,
                dovi_tool_bin="dovi_tool",
                decision=decision,
            )
