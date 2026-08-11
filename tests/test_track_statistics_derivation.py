"""Statistiques reprises des sources en copie stricte (sans mesurer la sortie)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.matroska.editors.statistics import MatroskaTrackStatisticsEditor
from core.matroska.reader import MatroskaReader
from core.workflows.common.track_statistics import (
    derive_output_statistics,
    parse_statistics_duration_ns,
    source_statistics_by_position,
)
from core.workflows.remux_mapping import MappedTrack
from core.workflows.remux_models import TrackEntry
from core.workflows.remux_plan import passthrough_source_refs

from tests.test_matroska_statistics_editor import _write_mkv as _write_tracks


def _write_mkv(path: Path) -> Path:
    """MKV à deux pistes : 2 frames vidéo, 3 éléments de sous-titres."""
    _write_tracks(path)
    return path


def _track(track_type: str = "audio", **kwargs) -> TrackEntry:
    values = dict(
        mkv_tid=1, track_type=track_type, codec="EAC3", display_info="5.1",
        language="fra", title="", file_id="src0", orig_codec="EAC3",
    )
    values.update(kwargs)
    return TrackEntry(**values)


def _mapped(track: TrackEntry, source: Path) -> MappedTrack:
    return MappedTrack(
        source_input_idx=0, source_file_index=0, source_path=source,
        stream_index=track.mkv_tid, track=track, out_type_index=0,
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("00:00:00.000000000", 0),
        ("01:52:10.667000000", 6730667000000),
        ("00:00:01.5", 1_500_000_000),
        ("00:01:02", 62_000_000_000),
        ("", None),
        ("bogus", None),
    ],
)
def test_statistics_duration_parsing(text: str, expected: int | None) -> None:
    assert parse_statistics_duration_ns(text) == expected


def test_source_statistics_are_read_from_published_tags(tmp_path: Path) -> None:
    source = _write_mkv(tmp_path / "src.mkv")
    MatroskaTrackStatisticsEditor().apply(source, writing_app="source-tool")

    statistics = source_statistics_by_position(source)

    assert set(statistics) == {0, 1}
    assert statistics[0][0] == 2   # frames vidéo
    assert statistics[1][0] == 3   # éléments de sous-titres


def test_derivation_maps_output_positions_to_their_sources(tmp_path: Path) -> None:
    first = _write_mkv(tmp_path / "first.mkv")
    second = _write_mkv(tmp_path / "second.mkv")
    for path in (first, second):
        MatroskaTrackStatisticsEditor().apply(path, writing_app="source-tool")

    derived = derive_output_statistics([(second, 1), (first, 0), (second, 0)])

    assert derived is not None
    assert derived[0] == source_statistics_by_position(second)[1]
    assert derived[1] == source_statistics_by_position(first)[0]
    assert derived[2] == source_statistics_by_position(second)[0]


def test_derivation_refuses_sources_without_statistics(tmp_path: Path) -> None:
    """Sans tags publiés, aucune valeur n'est inventée : la sortie sera mesurée."""
    source = _write_mkv(tmp_path / "bare.mkv")

    assert source_statistics_by_position(source) == {}
    assert derive_output_statistics([(source, 0)]) is None
    assert derive_output_statistics([]) is None


def test_derivation_refuses_a_non_matroska_source(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    assert source_statistics_by_position(source) == {}


class TestPassthroughDetection:
    """Toute transformation invalide la reprise des compteurs sources."""

    def test_plain_copy_is_accepted(self, tmp_path: Path) -> None:
        source = tmp_path / "src.mkv"
        refs = passthrough_source_refs([_mapped(_track(), source)])
        assert refs == [(source, 1)]

    def test_reencoded_audio_is_rejected(self, tmp_path: Path) -> None:
        track = _track(codec="AC3", orig_codec="EAC3")
        assert passthrough_source_refs([_mapped(track, tmp_path / "src.mkv")]) is None

    def test_time_shift_is_rejected(self, tmp_path: Path) -> None:
        track = _track(time_shift_ms=120)
        assert passthrough_source_refs([_mapped(track, tmp_path / "src.mkv")]) is None

    def test_sync_rewrite_is_rejected(self, tmp_path: Path) -> None:
        track = _track(sync_rewrite_mode="offset")
        assert passthrough_source_refs([_mapped(track, tmp_path / "src.mkv")]) is None

    def test_new_track_is_rejected(self, tmp_path: Path) -> None:
        track = _track(is_new=True)
        assert passthrough_source_refs([_mapped(track, tmp_path / "src.mkv")]) is None

    def test_converted_subtitle_is_rejected(self, tmp_path: Path) -> None:
        track = _track(track_type="subtitle", codec="MOV_TEXT", orig_codec="MOV_TEXT")
        assert passthrough_source_refs([_mapped(track, tmp_path / "src.mkv")]) is None

    def test_copied_subtitle_is_accepted(self, tmp_path: Path) -> None:
        track = _track(track_type="subtitle", codec="SUBRIP", orig_codec="SUBRIP")
        assert passthrough_source_refs([_mapped(track, tmp_path / "src.mkv")]) is not None


def test_editor_writes_supplied_statistics_without_scanning(tmp_path: Path) -> None:
    """Les valeurs fournies sont écrites telles quelles, sans mesurer."""
    output = _write_mkv(tmp_path / "out.mkv")
    # Durées cohérentes avec le segment écrit (120 ms) : sinon les valeurs
    # sont rejetées et la sortie mesurée.
    supplied = {0: (111, 2222, 100_000_000), 1: (44, 555, 120_000_000)}

    scanned: list[str] = []
    original = MatroskaReader.block_summaries

    def _guard(self, **kwargs):
        scanned.append(str(self.path))
        return original(self, **kwargs)

    MatroskaReader.block_summaries = _guard
    try:
        result = MatroskaTrackStatisticsEditor().apply(
            output, writing_app="Muxiveo", statistics_by_position=supplied,
        )
    finally:
        MatroskaReader.block_summaries = original

    assert result.applied and result.track_count == 2
    assert scanned == []
    assert result.packet_validation is None
    values = {}
    for tag in MatroskaReader(output).tags():
        uid = tag.targets.get("63c5", 0)
        entries = {name.upper(): text for name, text in tag.values}
        if "NUMBER_OF_FRAMES" in entries:
            values[uid] = entries
    positions = {track.uid: index for index, track in enumerate(MatroskaReader(output).tracks())}
    by_position = {positions[uid]: entry for uid, entry in values.items() if uid in positions}
    assert by_position[0]["NUMBER_OF_FRAMES"] == "111"
    assert by_position[1]["NUMBER_OF_BYTES"] == "555"
    assert by_position[1]["DURATION"] == "00:00:00.120000000"


def test_supplied_statistics_are_rejected_when_they_do_not_fit_the_output(tmp_path: Path) -> None:
    """Une sortie plus courte que les sources est mesurée, jamais recopiée."""
    output = _write_mkv(tmp_path / "out.mkv")
    # Valeurs d'une source d'une heure alors que la sortie dure 120 ms.
    supplied = {0: (90_000, 10_000_000, 3_600_000_000_000), 1: (1_200, 40_000, 3_600_000_000_000)}

    result = MatroskaTrackStatisticsEditor().apply(
        output, writing_app="Muxiveo", statistics_by_position=supplied,
    )

    assert result.applied
    assert result.packet_validation is not None  # la sortie a bien été mesurée
    positions = {track.uid: index for index, track in enumerate(MatroskaReader(output).tracks())}
    frames = {
        positions[tag.targets.get("63c5", 0)]: dict(
            (name.upper(), text) for name, text in tag.values
        )["NUMBER_OF_FRAMES"]
        for tag in MatroskaReader(output).tags()
        if tag.targets.get("63c5", 0) in positions
        and any(name.upper() == "NUMBER_OF_FRAMES" for name, _ in tag.values)
    }
    assert frames == {0: "2", 1: "3"}
