"""Régénération post-mux des statistiques de pistes (Count of elements)."""

from __future__ import annotations

from pathlib import Path

from core.matroska.ebml import ascii_element, element, string_element, uint_element
from core.matroska.editors.statistics import MatroskaTrackStatisticsEditor
from core.matroska.ids import (
    SIMPLE_TAG_ID, TAGS_ID, TAG_ID, TAG_NAME_ID, TAG_STRING_ID,
    TAG_TRACK_UID_ID, TARGETS_ID,
    CODEC_ID_ID, TRACK_NUMBER_ID, TRACK_TYPE_ID, TRACK_UID_ID,
)
from core.matroska.mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack
from core.matroska.reader import MatroskaBlock, MatroskaReader, MatroskaTrack
from core.matroska.writer import MatroskaWriter


def _source_track(number: int, uid: int, codec: str, kind: int) -> MatroskaTrack:
    raw = b"".join((
        uint_element(TRACK_NUMBER_ID, number),
        uint_element(TRACK_UID_ID, uid),
        uint_element(TRACK_TYPE_ID, kind),
        ascii_element(CODEC_ID_ID, codec),
    ))
    return MatroskaTrack(number, uid, kind, codec, b"", "", "und", "", raw)


def _simple_tag(name: str, value: str) -> bytes:
    return element(
        SIMPLE_TAG_ID,
        string_element(TAG_NAME_ID, name) + string_element(TAG_STRING_ID, value),
    )


def _tags_element(*, track_uid: int, stale_frames: str) -> bytes:
    """Tags façon sortie ffmpeg : globaux + statistiques héritées d'une source."""
    global_tag = element(
        TAG_ID,
        element(TARGETS_ID, b"")
        + _simple_tag("DIRECTOR", "Louis Leterrier")
        + _simple_tag("TITLE", "Le Dernier Refuge"),
    )
    track_tag = element(
        TAG_ID,
        element(TARGETS_ID, uint_element(TAG_TRACK_UID_ID, track_uid))
        + _simple_tag("NUMBER_OF_FRAMES", stale_frames)
        + _simple_tag("BPS", "42")
        + _simple_tag("_STATISTICS_WRITING_APP", "outil externe")
        + _simple_tag("LANGUAGE_CUSTOM", "conservé"),
    )
    return element(TAGS_ID, global_tag + track_tag)


def _write_mkv(path: Path, *, tags: bytes = b"") -> tuple[int, int]:
    """Écrit un MKV à deux pistes et retourne leurs UID de sortie."""
    video_uid, subtitle_uid = 900_001, 900_002
    tracks = (
        MatroskaMuxTrack(
            Path("v.mkv"), _source_track(7, 70, "V_MPEG4/ISO/AVC", 1), 1, video_uid,
            language="und", name="Video",
        ),
        MatroskaMuxTrack(
            Path("s.mkv"), _source_track(8, 71, "S_TEXT/UTF8", 17), 2, subtitle_uid,
            language="fre", name="French",
        ),
    )
    packets = (
        MatroskaMuxPacket(1, MatroskaBlock(7, 0, 0x80, b"video-0")),
        MatroskaMuxPacket(1, MatroskaBlock(7, 40, 0x80, b"video-1")),
        MatroskaMuxPacket(2, MatroskaBlock(8, 10, 0x80, b"sub-0", duration_ms=20)),
        MatroskaMuxPacket(2, MatroskaBlock(8, 50, 0x80, b"sub-1", duration_ms=20)),
        MatroskaMuxPacket(2, MatroskaBlock(8, 90, 0x80, b"sub-2", duration_ms=20)),
    )
    MatroskaWriter().write(
        MatroskaMuxPlan(
            path, tracks, packets, duration_ms=120,
            opaque_top_level=(tags,) if tags else (),
        ),
    )
    return video_uid, subtitle_uid


def _track_tags(path: Path) -> dict[int, dict[str, str]]:
    """Statistiques lues par UID de piste depuis l'élément Tags du fichier."""
    from core.matroska.writer import _element_payload, _raw_children, _simple_tag_name

    out: dict[int, dict[str, str]] = {}
    for raw_tags in MatroskaReader(path).raw_top_level(TAGS_ID):
        for child_id, child_raw in _raw_children(_element_payload(raw_tags)):
            if child_id != TAG_ID:
                continue
            uid = 0
            values: dict[str, str] = {}
            for tag_child_id, tag_child_raw in _raw_children(_element_payload(child_raw)):
                if tag_child_id == TARGETS_ID:
                    for target_id, target_raw in _raw_children(_element_payload(tag_child_raw)):
                        if target_id == TAG_TRACK_UID_ID:
                            uid = int.from_bytes(_element_payload(target_raw), "big")
                elif tag_child_id == SIMPLE_TAG_ID:
                    for name_id, name_raw in _raw_children(_element_payload(tag_child_raw)):
                        if name_id == TAG_STRING_ID:
                            values[_simple_tag_name(tag_child_raw)] = (
                                _element_payload(name_raw).decode("utf-8").rstrip("\0")
                            )
            out.setdefault(uid, {}).update(values)
    return out


def test_statistics_are_measured_from_written_packets(tmp_path: Path) -> None:
    output = tmp_path / "out.mkv"
    _video_uid, subtitle_uid = _write_mkv(output)

    result = MatroskaTrackStatisticsEditor().apply(output, writing_app="Muxiveo 9.9.9")

    assert result.applied and not result.skipped
    assert result.track_count == 2
    subtitle_tags = _track_tags(output)[subtitle_uid]
    # Trois éléments de sous-titres écrits → MediaInfo affiche « Count of
    # elements » uniquement si les tags compagnons accompagnent la valeur.
    assert subtitle_tags["NUMBER_OF_FRAMES"] == "3"
    assert subtitle_tags["NUMBER_OF_BYTES"] == str(len(b"sub-0") * 3)
    assert subtitle_tags["_STATISTICS_WRITING_APP"] == "Muxiveo 9.9.9"
    assert subtitle_tags["_STATISTICS_TAGS"] == "BPS DURATION NUMBER_OF_FRAMES NUMBER_OF_BYTES"
    assert subtitle_tags["DURATION"] == "00:00:00.110000000"


def test_inherited_statistics_are_replaced_and_other_tags_kept(tmp_path: Path) -> None:
    output = tmp_path / "out.mkv"
    video_uid, _subtitle_uid = _write_mkv(output)
    # Statistiques héritées d'une source, fausses après sélection de pistes.
    output_with_tags = tmp_path / "tagged.mkv"
    _write_mkv(output_with_tags, tags=_tags_element(track_uid=video_uid, stale_frames="161536"))

    result = MatroskaTrackStatisticsEditor().apply(output_with_tags)

    assert result.applied
    tags = _track_tags(output_with_tags)
    assert tags[video_uid]["NUMBER_OF_FRAMES"] == "2"
    assert tags[video_uid]["_STATISTICS_WRITING_APP"] == "Muxiveo"
    # Tags non statistiques conservés, globaux comme par piste.
    assert tags[video_uid]["LANGUAGE_CUSTOM"] == "conservé"
    assert tags[0]["DIRECTOR"] == "Louis Leterrier"


def test_patch_is_idempotent_and_keeps_file_readable(tmp_path: Path) -> None:
    output = tmp_path / "out.mkv"
    video_uid, _subtitle_uid = _write_mkv(output)
    editor = MatroskaTrackStatisticsEditor()

    first = editor.apply(output)
    tags_after_first = _track_tags(output)
    second = editor.apply(output)

    assert first.applied and second.applied
    assert _track_tags(output) == tags_after_first
    assert len(MatroskaReader(output).raw_top_level(TAGS_ID)) == 1
    assert [track.number for track in MatroskaReader(output).tracks()] == [1, 2]
    assert tags_after_first[video_uid]["NUMBER_OF_FRAMES"] == "2"


def test_packet_summary_matches_a_full_validation_scan(tmp_path: Path) -> None:
    """Le résumé remonté évite à la validation de relire la sortie entière."""
    output = tmp_path / "out.mkv"
    _write_mkv(output)

    summary = MatroskaTrackStatisticsEditor().apply(output).packet_validation

    reader = MatroskaReader(output)
    expected_tracks: set[int] = set()
    expected_max = 0
    for block in reader.blocks():
        expected_tracks.add(block.track_number)
        timestamp_ns = (
            block.timestamp_ns if block.timestamp_ns is not None
            else block.timestamp_ms * 1_000_000
        )
        duration_ns = block.duration_ns or ((block.duration_ms or 0) * 1_000_000)
        expected_max = max(expected_max, timestamp_ns + duration_ns)
    assert summary is not None
    assert set(summary.track_numbers) == expected_tracks
    assert summary.max_packet_timestamp_ns == expected_max


def test_missing_file_is_reported_as_skipped(tmp_path: Path) -> None:
    result = MatroskaTrackStatisticsEditor().apply(tmp_path / "absent.mkv")

    assert not result.applied and result.skipped
    assert "introuvable" in result.reason


def test_validation_probe_detects_an_empty_media_track(tmp_path: Path) -> None:
    """L'arrêt anticipé ne masque jamais une piste sans paquet."""
    from core.matroska.contract import ExpectedMatroskaTrack, MatroskaOutputContract
    from core.matroska.validation import validate_matroska_output

    output = tmp_path / "out.mkv"
    _write_mkv(output)  # piste 1 : vidéo, piste 2 : sous-titres
    contract = MatroskaOutputContract(
        track_types=("video", "audio"),
        expected_tracks=(
            ExpectedMatroskaTrack(track_type="video", require_packets=True),
            # La seconde piste du fichier est un sous-titre : aucun paquet
            # « audio » n'existe, l'erreur doit être signalée.
            ExpectedMatroskaTrack(track_type="audio", require_packets=True),
        ),
    )

    errors = validate_matroska_output(output, contract)

    assert any("Aucun paquet écrit" in error for error in errors) or any(
        "Pistes de sortie inattendues" in error for error in errors
    )


def test_validation_probe_reads_the_last_packet_timestamp(tmp_path: Path) -> None:
    """La durée est validée depuis les Clusters de fin, sans tout relire."""
    from core.matroska.contract import ExpectedMatroskaTrack, MatroskaOutputContract
    from core.matroska.validation import validate_matroska_output

    output = tmp_path / "out.mkv"
    _write_mkv(output)
    contract = MatroskaOutputContract(
        track_types=("video", "subtitle"),
        expected_tracks=(
            ExpectedMatroskaTrack(track_type="video", require_packets=True),
            ExpectedMatroskaTrack(track_type="subtitle", require_packets=False),
        ),
    )

    assert validate_matroska_output(output, contract) == []
