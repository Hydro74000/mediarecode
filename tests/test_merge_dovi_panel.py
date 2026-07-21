from __future__ import annotations

from pathlib import Path
from typing import Any, cast


class _DummySignal:
    def connect(self, *args, **kwargs) -> None:
        _ = args, kwargs


class _FakeWorkflow:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.step_started = _DummySignal()
        self.step_progress = _DummySignal()
        self.step_progress_pct = _DummySignal()
        self.step_finished = _DummySignal()
        self.workflow_finished = _DummySignal()
        self.workflow_failed = _DummySignal()


class _DummyConfig:
    def __init__(self, tmp_path: Path) -> None:
        self.tool_mediainfo = "mi"
        self.tool_ffmpeg = "ffm"
        self.tool_ffprobe = "ffp"
        self.tool_dovi_tool = "dovi"
        self.tool_hdr10plus = "hdr10p"
        self.work_dir = tmp_path / "work"
        self.output_dir = tmp_path / "out"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)


def test_merge_dovi_panel_inits_workflow_with_ffmpeg_bins(tmp_path, qt_app, monkeypatch):
    import ui.panels.merge_dovi_panel as panel_mod
    from core.workflows.merge_dovi import DoviProfile

    captured: dict[str, object] = {}

    def _factory(**kwargs):
        captured.update(kwargs)
        return _FakeWorkflow(**kwargs)

    monkeypatch.setattr(panel_mod, "MergeDoviWorkflow", _factory)

    cfg = _DummyConfig(tmp_path)
    panel = panel_mod.MergeDoviPanel(cast(Any, cfg))

    assert captured["mediainfo_bin"] == "mi"
    assert captured["ffmpeg_bin"] == "ffm"
    assert captured["ffprobe_bin"] == "ffp"
    assert captured["dovi_tool_bin"] == "dovi"
    assert captured["hdr10plus_bin"] == "hdr10p"
    assert "mkvextract_bin" not in captured
    assert panel._config_section.dovi_profile == DoviProfile.P8_1

    combo = panel._config_section._profile_combo
    values = [combo.itemData(i) for i in range(combo.count())]
    assert values == [DoviProfile.DISABLED, DoviProfile.P8_1, DoviProfile.P8_0]
