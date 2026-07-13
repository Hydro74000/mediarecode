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
