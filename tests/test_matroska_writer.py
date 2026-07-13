from pathlib import Path

from core.workflows.ebml_writer import ascii_element, uint_element
from core.workflows.matroska_element_ids import CODEC_ID_ID, TRACK_NUMBER_ID, TRACK_TYPE_ID, TRACK_UID_ID
from core.workflows.matroska_mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack, deterministic_uid
from core.workflows.matroska_reader import MatroskaBlock, MatroskaReader, MatroskaTrack
from core.workflows.matroska_writer import MatroskaWriter


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


def test_deterministic_uid_is_stable_and_nonzero() -> None:
    assert deterministic_uid("source", 1) == deterministic_uid("source", 1)
    assert deterministic_uid("source", 1) != deterministic_uid("source", 2)
    assert deterministic_uid("source", 1) > 0
