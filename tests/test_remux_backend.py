from pathlib import Path

from core.workflows.remux_backend import select_mux_backend
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry
from core.workflows.ebml_writer import ascii_element, element, uint_element
from core.workflows.matroska_element_ids import (
    EBML_HEADER_ID, SEGMENT_ID, TRACKS_ID, TRACK_ENTRY_ID, TRACK_NUMBER_ID,
    TRACK_UID_ID, TRACK_TYPE_ID, CODEC_ID_ID,
)


def track(index: int, kind: str = "video") -> TrackEntry:
    return TrackEntry(index, kind, "COPY", "", "und", "", file_id="src0")


def config(tmp_path: Path, *, backend: str = "auto", suffix: str = ".mkv") -> RemuxConfig:
    source = tmp_path / f"source{suffix}"
    if suffix == ".mkv":
        entry = element(TRACK_ENTRY_ID, b"".join((
            uint_element(TRACK_NUMBER_ID, 1), uint_element(TRACK_UID_ID, 1),
            uint_element(TRACK_TYPE_ID, 1), ascii_element(CODEC_ID_ID, "V_MPEG4/ISO/AVC"),
        )))
        source.write_bytes(element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff" + element(TRACKS_ID, entry))
    else:
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


def test_auto_falls_back_for_unreadable_matroska(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    cfg.sources[0].path.write_bytes(b"not-ebml")
    decision = select_mux_backend(cfg)
    assert decision.selected == "ffmpeg"
    assert "illisible" in decision.native_reasons[0]
