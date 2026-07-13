from pathlib import Path

import pytest

from core.workflows.ebml_writer import ascii_element, element, string_element, uint_element
from core.workflows.matroska_element_ids import CODEC_ID_ID, TRACK_NUMBER_ID, TRACK_TYPE_ID, TRACK_UID_ID
from core.workflows.matroska_element_ids import CUES_ID, INFO_ID, SEEK_HEAD_ID, TRACKS_ID
from core.workflows.matroska_element_ids import (
    SIMPLE_TAG_ID, TAGS_ID, TAG_ID, TAG_NAME_ID, TAG_STRING_ID,
    TAG_TRACK_UID_ID, TARGETS_ID,
)
from core.workflows.matroska_mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack, deterministic_uid
from core.workflows.matroska_reader import MatroskaBlock, MatroskaReader, MatroskaTrack
from core.workflows.matroska_writer import MatroskaWriter, rewrite_tag_target_uids


def source_track(number: int, uid: int, codec: str, kind: int) -> MatroskaTrack:
    raw = b"".join((uint_element(TRACK_NUMBER_ID, number), uint_element(TRACK_UID_ID, uid), uint_element(TRACK_TYPE_ID, kind), ascii_element(CODEC_ID_ID, codec)))
    return MatroskaTrack(number, uid, kind, codec, b"", "", "und", "", raw)


def test_writer_roundtrips_multiple_tracks_and_packets(tmp_path: Path) -> None:
    video = source_track(7, 70, "V_MPEG4/ISO/AVC", 1)
    audio = source_track(7, 71, "A_AAC", 2)
    tracks = (
        MatroskaMuxTrack(Path("v.mkv"), video, 1, deterministic_uid("v"), language="und", name="Video"),
        MatroskaMuxTrack(Path("a.mkv"), audio, 2, deterministic_uid("a"), language="fra", name="Audio"),
    )
    packets = (
        MatroskaMuxPacket(1, MatroskaBlock(7, 0, 0x80, b"video")),
        MatroskaMuxPacket(2, MatroskaBlock(7, 5, 0x80, b"audio", duration_ms=20, references=(-1,))),
    )
    output = tmp_path / "out.mkv"
    MatroskaWriter().write(MatroskaMuxPlan(output, tracks, packets, duration_ms=25))
    reader = MatroskaReader(output)
    assert [(track.number, track.codec_id, track.language, track.name) for track in reader.tracks()] == [(1, "V_MPEG4/ISO/AVC", "und", "Video"), (2, "A_AAC", "fra", "Audio")]
    assert [(block.track_number, block.timestamp_ms, block.payload) for block in reader.blocks()] == [(1, 0, b"video"), (2, 5, b"audio")]
    assert not output.with_suffix(".mkv.partial").exists()
    top_level_ids = [item.element_id for item in reader.top_level()]
    assert top_level_ids[0] == SEEK_HEAD_ID
    assert INFO_ID in top_level_ids and TRACKS_ID in top_level_ids and CUES_ID in top_level_ids


def test_deterministic_uid_is_stable_and_nonzero() -> None:
    assert deterministic_uid("source", 1) == deterministic_uid("source", 1)
    assert deterministic_uid("source", 1) != deterministic_uid("source", 2)
    assert deterministic_uid("source", 1) > 0


def test_writer_emits_segment_title(tmp_path: Path) -> None:
    source = source_track(1, 10, "V_MPEG4/ISO/AVC", 1)
    output = tmp_path / "title.mkv"
    plan = MatroskaMuxPlan(
        output,
        (MatroskaMuxTrack(Path("v.mkv"), source, 1, 20),),
        (MatroskaMuxPacket(1, MatroskaBlock(1, 0, 0x80, b"video")),),
        title="Titre exact",
    )
    MatroskaWriter().write(plan)
    assert MatroskaReader(output).segment_title() == "Titre exact"


def test_copied_track_tag_uid_is_remapped() -> None:
    simple = element(
        SIMPLE_TAG_ID,
        string_element(TAG_NAME_ID, "TITLE") + string_element(TAG_STRING_ID, "Commentaire"),
    )
    targeted = element(
        TAGS_ID,
        element(TAG_ID, element(TARGETS_ID, uint_element(TAG_TRACK_UID_ID, 10)) + simple),
    )
    rewritten = rewrite_tag_target_uids(targeted, track_uids={10: 900}, attachment_uids={})
    assert uint_element(TAG_TRACK_UID_ID, 900) in rewritten
    assert uint_element(TAG_TRACK_UID_ID, 10) not in rewritten


def test_tag_for_unselected_track_is_discarded() -> None:
    simple = element(
        SIMPLE_TAG_ID,
        string_element(TAG_NAME_ID, "TITLE") + string_element(TAG_STRING_ID, "Retiré"),
    )
    targeted = element(
        TAGS_ID,
        element(TAG_ID, element(TARGETS_ID, uint_element(TAG_TRACK_UID_ID, 10)) + simple),
    )
    rewritten = rewrite_tag_target_uids(targeted, track_uids={}, attachment_uids={})
    assert "Retiré".encode() not in rewritten


def test_sub_millisecond_timestamp_roundtrips_without_precision_loss(tmp_path: Path) -> None:
    source = source_track(1, 10, "A_AAC", 2)
    output = tmp_path / "precision.mkv"
    block = MatroskaBlock(1, 2, 0x80, b"audio", timestamp_ns=1_500_000)
    MatroskaWriter().write(MatroskaMuxPlan(
        output,
        (MatroskaMuxTrack(Path("a.mkv"), source, 1, 20),),
        (MatroskaMuxPacket(1, block),),
        timestamp_scale_ns=1_000,
        duration_ns=1_500_000,
    ))
    decoded = list(MatroskaReader(output).blocks())
    assert decoded[0].timestamp_ns == 1_500_000


def test_validation_failure_removes_partial_and_preserves_existing_output(tmp_path: Path) -> None:
    source = source_track(1, 10, "A_AAC", 2)
    output = tmp_path / "atomic.mkv"
    output.write_bytes(b"previous")
    plan = MatroskaMuxPlan(
        output,
        (MatroskaMuxTrack(Path("a.mkv"), source, 1, 20),),
        (MatroskaMuxPacket(1, MatroskaBlock(1, 0, 0x80, b"audio")),),
    )

    def reject(_path: Path) -> None:
        raise RuntimeError("invalid")

    with pytest.raises(RuntimeError, match="invalid"):
        MatroskaWriter().write(plan, external_validator=reject)
    assert output.read_bytes() == b"previous"
    assert not output.with_suffix(".mkv.partial").exists()


def test_writer_preserves_xiph_lacing_as_one_block(tmp_path: Path) -> None:
    source = source_track(1, 10, "A_AAC", 2)
    # Deux frames Xiph: count-1=1, taille première=1, puis a + bb.
    block = MatroskaBlock(
        1, 0, 0x82, b"a", lace_index=0, lace_count=2,
        lacing_mode=1, encoded_frames_payload=b"\x01\x01abb",
    )
    output = tmp_path / "laced.mkv"
    MatroskaWriter().write(MatroskaMuxPlan(
        output,
        (MatroskaMuxTrack(Path("a.mkv"), source, 1, 20),),
        (MatroskaMuxPacket(1, block),),
    ))
    decoded = list(MatroskaReader(output).blocks())
    assert [item.payload for item in decoded] == [b"a", b"bb"]
    assert {item.lacing_mode for item in decoded} == {1}
