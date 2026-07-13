from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "concat_video.py"
SPEC = importlib.util.spec_from_file_location("concat_video_standalone", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
concat_video = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(concat_video)


def test_explicit_mkvmerge_backend_is_available_only_when_binary_exists(monkeypatch) -> None:
    monkeypatch.setattr(
        concat_video,
        "is_tool_available",
        lambda path: path == concat_video.MKVMERGE,
    )
    assert concat_video.select_mux_backend("mkvmerge") == "mkvmerge"
    with pytest.raises(ValueError, match="muxiveo"):
        concat_video.select_mux_backend("muxiveo")


def test_auto_prefers_muxiveo_then_mkvmerge_then_ffmpeg(monkeypatch) -> None:
    available = {concat_video.MUXIVEO, concat_video.MKVMERGE, concat_video.FFMPEG}
    monkeypatch.setattr(concat_video, "is_tool_available", lambda path: path in available)
    assert concat_video.select_mux_backend("auto") == "muxiveo"
    available.remove(concat_video.MUXIVEO)
    assert concat_video.select_mux_backend("auto") == "mkvmerge"
    available.remove(concat_video.MKVMERGE)
    assert concat_video.select_mux_backend("auto") == "ffmpeg"


def test_mkvmerge_warning_exit_code_is_accepted(monkeypatch) -> None:
    class Result:
        returncode = 1
        stdout = ""
        stderr = "warning"

    monkeypatch.setattr(concat_video.subprocess, "run", lambda *_args, **_kwargs: Result())
    assert concat_video.run_command(["mkvmerge", "-o", "out.mkv", "in.mkv"]).returncode == 1
