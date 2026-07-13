from pathlib import Path

from core.workflows.ebml_writer import element, float_element
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


def test_reader_extracts_simple_block_timestamp(tmp_path: Path) -> None:
    cluster, timestamp, block = bytes.fromhex("1f43b675"), bytes.fromhex("e7"), bytes.fromhex("a3")
    payload = element(timestamp, b"\x64") + element(block, b"\x81\x00\x05\x80payload")
    path = tmp_path / "blocks.mkv"
    path.write_bytes(element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff" + element(cluster, payload))
    blocks = list(MatroskaReader(path).simple_blocks())
    assert len(blocks) == 1
    assert (blocks[0].track_number, blocks[0].timestamp_ms, blocks[0].payload) == (1, 105, b"payload")


def _blocks_file(tmp_path: Path, block_payload: bytes, *, grouped: bool = False) -> Path:
    cluster, timestamp = bytes.fromhex("1f43b675"), bytes.fromhex("e7")
    if grouped:
        body = element(bytes.fromhex("a1"), block_payload)
        body += element(bytes.fromhex("9b"), b"\x28")
        body += element(bytes.fromhex("fb"), b"\xff")
        packet = element(bytes.fromhex("a0"), body)
    else:
        packet = element(bytes.fromhex("a3"), block_payload)
    path = tmp_path / ("group.mkv" if grouped else "lace.mkv")
    path.write_bytes(element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff" + element(cluster, element(timestamp, b"\x00") + packet))
    return path


def test_reader_decodes_fixed_and_xiph_lacing(tmp_path: Path) -> None:
    fixed = _blocks_file(tmp_path, b"\x81\x00\x00\x84\x01aabb")  # 2 frames fixed
    assert [b.payload for b in MatroskaReader(fixed).blocks()] == [b"aa", b"bb"]
    xiph = _blocks_file(tmp_path, b"\x81\x00\x00\x82\x01\x01abb")
    assert [b.payload for b in MatroskaReader(xiph).blocks()] == [b"a", b"bb"]


def test_reader_decodes_ebml_lacing_and_block_group(tmp_path: Path) -> None:
    ebml = _blocks_file(tmp_path, b"\x81\x00\x00\x86\x01\x81abb")
    assert [b.payload for b in MatroskaReader(ebml).blocks()] == [b"a", b"bb"]
    grouped = list(MatroskaReader(_blocks_file(tmp_path, b"\x81\x00\x05\x00frame", grouped=True)).blocks())
    assert grouped[0].timestamp_ms == 5
    assert grouped[0].duration_ms == 40
    assert grouped[0].references == (-1,)


def test_reader_resumes_after_unknown_size_clusters(tmp_path: Path) -> None:
    cluster, timestamp, block = bytes.fromhex("1f43b675"), bytes.fromhex("e7"), bytes.fromhex("a3")
    first = cluster + b"\xff" + element(timestamp, b"\x00") + element(block, b"\x81\x00\x00\x80a")
    second = cluster + b"\xff" + element(timestamp, b"\x0a") + element(block, b"\x81\x00\x00\x80b")
    path = tmp_path / "unknown-clusters.mkv"
    path.write_bytes(element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff" + first + second)
    reader = MatroskaReader(path)
    assert [item.element_id for item in reader.top_level()] == [cluster, cluster]
    assert [(item.timestamp_ms, item.payload) for item in reader.blocks()] == [(0, b"a"), (10, b"b")]


def test_reader_models_nested_video_audio_colour_and_dovi_mapping(tmp_path: Path) -> None:
    track_entry = element(bytes.fromhex("ae"), b"".join((
        element(bytes.fromhex("d7"), b"\x01"),
        element(bytes.fromhex("73c5"), b"\x02"),
        element(bytes.fromhex("83"), b"\x01"),
        element(bytes.fromhex("86"), b"V_MPEGH/ISO/HEVC"),
        element(bytes.fromhex("e0"), b"".join((
            element(bytes.fromhex("b0"), b"\x07\x80"),
            element(bytes.fromhex("ba"), b"\x04\x38"),
            element(bytes.fromhex("55b0"), b"".join((
                element(bytes.fromhex("55bb"), b"\x09"),
                element(bytes.fromhex("55ba"), b"\x10"),
                element(bytes.fromhex("55bc"), b"\x03\xe8"),
                element(bytes.fromhex("55d0"), float_element(bytes.fromhex("55d9"), 1000.0)),
            ))),
        ))),
        element(bytes.fromhex("41e4"), b"".join((
            element(bytes.fromhex("41f0"), b"\x01"),
            element(bytes.fromhex("41a4"), b"Dolby Vision configuration"),
            element(bytes.fromhex("41e7"), b"\x07"),
            element(bytes.fromhex("41ed"), b"\x01\x08\x06"),
        ))),
    )))
    path = tmp_path / "metadata.mkv"
    path.write_bytes(
        element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff"
        + element(bytes.fromhex("1654ae6b"), track_entry)
    )

    track = MatroskaReader(path).tracks()[0]

    assert track.video["pixel_width"] == 1920
    assert track.video["pixel_height"] == 1080
    assert track.video["primaries"] == 9
    assert track.video["transfer_characteristics"] == 16
    assert track.video["max_cll"] == 1000
    assert track.video["luminance_max"] == 1000.0
    assert track.block_addition_mappings[0]["name"] == "Dolby Vision configuration"
    assert track.block_addition_mappings[0]["extra_data"] == b"\x01\x08\x06"


def test_reader_reports_content_encryption_as_native_blocker(tmp_path: Path) -> None:
    encoded = element(bytes.fromhex("6d80"), element(
        bytes.fromhex("6240"), element(bytes.fromhex("5035"), b"\x01"),
    ))
    track = element(bytes.fromhex("ae"), b"".join((
        element(bytes.fromhex("d7"), b"\x01"), element(bytes.fromhex("73c5"), b"\x02"),
        element(bytes.fromhex("83"), b"\x02"), element(bytes.fromhex("86"), b"A_AAC"), encoded,
    )))
    path = tmp_path / "encrypted.mka"
    path.write_bytes(
        element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff"
        + element(bytes.fromhex("1654ae6b"), track)
    )
    assert MatroskaReader(path).content_encoding_capabilities() == (False, True)
