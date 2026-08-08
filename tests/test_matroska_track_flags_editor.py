"""Patch post-mux du FlagEnabled Matroska (pistes désactivées)."""

from __future__ import annotations

from pathlib import Path

from core.matroska.ebml import ascii_element, uint_element
from core.matroska.editors.track_flags import MatroskaTrackEnabledEditor
from core.matroska.ids import (
    CODEC_ID_ID, FLAG_ENABLED_ID, NAME_ID, TRACK_NUMBER_ID, TRACK_TYPE_ID, TRACK_UID_ID,
)
from core.matroska.mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack
from core.matroska.reader import MatroskaBlock, MatroskaReader, MatroskaTrack
from core.matroska.writer import MatroskaWriter


def _source_track(number: int, uid: int, codec: str, kind: int, *, enabled: bool | None = None) -> MatroskaTrack:
    raw = b"".join((
        uint_element(TRACK_NUMBER_ID, number),
        uint_element(TRACK_UID_ID, uid),
        uint_element(TRACK_TYPE_ID, kind),
        ascii_element(CODEC_ID_ID, codec),
        ascii_element(NAME_ID, f"track-{number}"),
        *(() if enabled is None else (uint_element(FLAG_ENABLED_ID, int(enabled)),)),
    ))
    return MatroskaTrack(number, uid, kind, codec, b"", "", "und", f"track-{number}", raw)


def _write_mkv(path: Path, *, written_enabled: bool = True) -> Path:
    """MKV à trois pistes portant toutes le même FlagEnabled écrit."""
    tracks = tuple(
        MatroskaMuxTrack(
            Path(f"{kind}.mkv"),
            _source_track(number, 900_000 + number, codec, kind_id, enabled=written_enabled),
            number,
            900_000 + number,
            language="und",
            name=f"track-{number}",
            flag_enabled=written_enabled,
        )
        for number, (kind, codec, kind_id) in enumerate(
            (("v", "V_MPEG4/ISO/AVC", 1), ("a", "A_AAC", 2), ("s", "S_TEXT/UTF8", 17)),
            start=1,
        )
    )
    packets = (
        MatroskaMuxPacket(1, MatroskaBlock(1, 0, 0x80, b"video")),
        MatroskaMuxPacket(2, MatroskaBlock(2, 0, 0x80, b"audio")),
        MatroskaMuxPacket(3, MatroskaBlock(3, 10, 0x80, b"sub", duration_ms=20)),
    )
    MatroskaWriter().write(MatroskaMuxPlan(path, tracks, packets, duration_ms=40))
    return path


def _flags(path: Path) -> list[bool]:
    return [track.flag_enabled for track in MatroskaReader(path).tracks()]


def test_requested_tracks_are_disabled_in_place(tmp_path: Path) -> None:
    output = _write_mkv(tmp_path / "out.mkv")
    assert _flags(output) == [True, True, True]

    result = MatroskaTrackEnabledEditor().apply(output, {1: False, 2: False})

    assert result.applied and not result.skipped
    assert _flags(output) == [True, False, False]
    assert [(fix.track_position, fix.enabled_before, fix.enabled_after) for fix in result.fixes] == [
        (1, True, False), (2, True, False),
    ]
    # Le reste du TrackEntry est préservé.
    assert [track.name for track in MatroskaReader(output).tracks()] == [
        "track-1", "track-2", "track-3",
    ]


def test_existing_flag_is_replaced_not_duplicated(tmp_path: Path) -> None:
    output = _write_mkv(tmp_path / "out.mkv", written_enabled=False)
    assert _flags(output) == [False, False, False]

    result = MatroskaTrackEnabledEditor().apply(output, {0: True, 1: True, 2: True})

    assert result.applied
    assert _flags(output) == [True, True, True]
    entry = MatroskaReader(output).tracks()[0].raw_entry
    assert entry.count(FLAG_ENABLED_ID) == 1


def test_conforming_file_is_left_untouched(tmp_path: Path) -> None:
    output = _write_mkv(tmp_path / "out.mkv")
    before = output.read_bytes()

    result = MatroskaTrackEnabledEditor().apply(output, {0: True, 1: True, 2: True})

    assert not result.applied and not result.skipped
    assert "conforme" in result.reason
    assert output.read_bytes() == before


def test_patch_is_idempotent_and_keeps_packets(tmp_path: Path) -> None:
    output = _write_mkv(tmp_path / "out.mkv")
    editor = MatroskaTrackEnabledEditor()
    packets_before = [
        (block.track_number, block.timestamp_ms, block.payload)
        for block in MatroskaReader(output).blocks()
    ]

    first = editor.apply(output, {1: False})
    second = editor.apply(output, {1: False})

    assert first.applied and not second.applied
    assert _flags(output) == [True, False, True]
    assert [
        (block.track_number, block.timestamp_ms, block.payload)
        for block in MatroskaReader(output).blocks()
    ] == packets_before


def test_missing_file_is_reported_as_skipped(tmp_path: Path) -> None:
    result = MatroskaTrackEnabledEditor().apply(tmp_path / "absent.mkv", {0: False})

    assert not result.applied and result.skipped
    assert "introuvable" in result.reason
