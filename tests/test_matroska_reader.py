from pathlib import Path

from core.workflows.ebml_writer import element
from core.workflows.matroska_element_ids import EBML_HEADER_ID, INFO_ID, SEGMENT_ID
from core.workflows.matroska_reader import MatroskaReader


def test_reader_enumerates_segment_children(tmp_path: Path) -> None:
    path = tmp_path / "sample.mkv"
    # Minimal EBML prefix is sufficient for the boundary reader.
    path.write_bytes(element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff" + element(INFO_ID, b""))

    reader = MatroskaReader(path)
    assert reader.segment().element_id == SEGMENT_ID
    children = list(reader.top_level())
    assert [child.element_id for child in children] == [INFO_ID]


def test_reader_extracts_track_entry_fields(tmp_path: Path) -> None:
    tracks = bytes.fromhex("1654ae6b")
    entry = bytes.fromhex("ae")
    number, uid, kind = bytes.fromhex("d7"), bytes.fromhex("73c5"), bytes.fromhex("83")
    codec, language, name = bytes.fromhex("86"), bytes.fromhex("22b59c"), bytes.fromhex("536e")
    track = element(entry, b"".join((
        element(number, b"\x01"), element(uid, b"\x02"), element(kind, b"\x01"),
        element(codec, b"V_MPEGH/ISO/HEVC"), element(language, b"fra"), element(name, "Vidéo".encode()),
    )))
    path = tmp_path / "tracks.mkv"
    path.write_bytes(element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff" + element(tracks, track))
    result = MatroskaReader(path).tracks()
    assert len(result) == 1
    assert (result[0].number, result[0].uid, result[0].codec_id, result[0].language, result[0].name) == (1, 2, "V_MPEGH/ISO/HEVC", "fra", "Vidéo")
