from pathlib import Path

from core.workflows.remux_backend import select_mux_backend
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry


def track(index: int, kind: str = "video") -> TrackEntry:
    return TrackEntry(index, kind, "COPY", "", "und", "", file_id="src0")


def config(tmp_path: Path, *, backend: str = "auto", suffix: str = ".mkv") -> RemuxConfig:
    source = tmp_path / f"source{suffix}"
    source.write_bytes(b"fixture")
    return RemuxConfig(
        sources=[SourceInput(source, 0, [track(0)])], output=tmp_path / "out.mkv",
        track_order=[(0, 0)], keep_chapters=False, mux_backend=backend,
    )


def test_auto_selects_native_for_mkv_and_non_mkv_inputs(tmp_path: Path) -> None:
    assert select_mux_backend(config(tmp_path)).selected == "native"
    assert select_mux_backend(config(tmp_path, suffix=".mp4")).selected == "native"


def test_ffmpeg_override_remains_strictly_backward_compatible(tmp_path: Path) -> None:
    decision = select_mux_backend(config(tmp_path, backend="ffmpeg"))
    assert decision.requested == decision.selected == "ffmpeg"


def test_native_rejects_non_mkv_output(tmp_path: Path) -> None:
    cfg = config(tmp_path, backend="native")
    cfg.output = tmp_path / "out.mp4"
    decision = select_mux_backend(cfg)
    assert decision.native_reasons
