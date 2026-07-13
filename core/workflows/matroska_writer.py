"""Deterministic multi-track Matroska writer."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from core.workflows.ebml_writer import (
    element, encode_unknown_size_marker, encode_vint_size_minimal,
    sint_element, string_element, uint_element,
)
from core.workflows.matroska_element_ids import (
    ATTACHMENTS_ID, ATTACHED_FILE_ID,
    BLOCK_ADDITIONS_ID, BLOCK_DURATION_ID, BLOCK_GROUP_ID, BLOCK_ID,
    CLUSTER_ID, CODEC_STATE_ID, CUES_ID, DISCARD_PADDING_ID,
    FLAG_DEFAULT_ID, FLAG_ENABLED_ID, INFO_ID, LANGUAGE_BCP47_ID,
    LANGUAGE_ID, NAME_ID, REFERENCE_BLOCK_ID, SEGMENT_ID, SIMPLE_BLOCK_ID,
    CHAPTERS_ID, CHAPTER_ATOM_ID, CHAPTER_DISPLAY_ID, CHAPTER_TIME_START_ID,
    CHAPTER_UID_ID, CHAP_LANGUAGE_ID, CHAP_STRING_ID, EDITION_ENTRY_ID,
    FILE_DATA_ID, FILE_DESCRIPTION_ID, FILE_MEDIA_TYPE_ID, FILE_NAME_ID, FILE_UID_ID,
    SIMPLE_TAG_ID, TAGS_ID, TAG_ID, TAG_NAME_ID, TAG_STRING_ID, TARGETS_ID,
    TIMESTAMP_ID, TRACK_ENTRY_ID, TRACK_NUMBER_ID, TRACK_UID_ID, TRACKS_ID,
)
from core.workflows.matroska_mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack
from core.workflows.matroska_native_muxer import _build_ebml_header, _build_info
from core.workflows.matroska_reader import read_element
from core.workflows.matroska_reader import MatroskaAttachment


_PATCHED_TRACK_FIELDS = {
    TRACK_NUMBER_ID, TRACK_UID_ID, LANGUAGE_ID, LANGUAGE_BCP47_ID,
    NAME_ID, FLAG_ENABLED_ID, FLAG_DEFAULT_ID,
}


def _raw_children(payload: bytes) -> list[tuple[bytes, bytes]]:
    out: list[tuple[bytes, bytes]] = []
    stream = BytesIO(payload)
    while stream.tell() < len(payload):
        item = read_element(stream, limit=len(payload))
        if item is None or item.size is None:
            raise ValueError("TrackEntry EBML invalide")
        stream.seek(item.offset)
        raw = stream.read(item.header_size + item.size)
        out.append((item.element_id, raw))
        stream.seek(item.payload_offset + item.size)
    return out


def _track_entry(track: MatroskaMuxTrack) -> bytes:
    kept = b"".join(raw for element_id, raw in _raw_children(track.source_track.raw_entry) if element_id not in _PATCHED_TRACK_FIELDS)
    fields = b"".join((
        uint_element(TRACK_NUMBER_ID, track.output_number),
        uint_element(TRACK_UID_ID, track.output_uid),
        uint_element(FLAG_ENABLED_ID, int(track.flag_enabled)),
        uint_element(FLAG_DEFAULT_ID, int(track.flag_default)),
        string_element(LANGUAGE_ID, track.language or "und"),
        string_element(LANGUAGE_BCP47_ID, track.language_bcp47) if track.language_bcp47 else b"",
        string_element(NAME_ID, track.name) if track.name else b"",
    ))
    return element(TRACK_ENTRY_ID, fields + kept)


def _block_header(track_number: int, timestamp_offset: int, flags: int) -> bytes:
    if not -32768 <= timestamp_offset <= 32767:
        raise ValueError("Offset Block hors int16")
    return encode_vint_size_minimal(track_number) + timestamp_offset.to_bytes(2, "big", signed=True) + bytes([flags & ~0x06])


def _packet_element(packet: MatroskaMuxPacket, cluster_time: int) -> bytes:
    block = packet.block
    raw = _block_header(packet.output_track_number, block.timestamp_ms - cluster_time, block.flags) + block.payload
    if not (block.duration_ms is not None or block.references or block.discard_padding_ns or block.codec_state or block.block_additions):
        return element(SIMPLE_BLOCK_ID, raw)
    children = [element(BLOCK_ID, raw)]
    if block.duration_ms is not None: children.append(uint_element(BLOCK_DURATION_ID, block.duration_ms))
    children.extend(sint_element(REFERENCE_BLOCK_ID, value) for value in block.references)
    if block.discard_padding_ns: children.append(sint_element(DISCARD_PADDING_ID, block.discard_padding_ns))
    if block.codec_state: children.append(element(CODEC_STATE_ID, block.codec_state))
    if block.block_additions: children.append(element(BLOCK_ADDITIONS_ID, block.block_additions))
    return element(BLOCK_GROUP_ID, b"".join(children))


def build_attachments_element(attachments: list[MatroskaAttachment]) -> bytes:
    files: list[bytes] = []
    for attachment in attachments:
        body = b"".join((
            string_element(FILE_DESCRIPTION_ID, attachment.description) if attachment.description else b"",
            string_element(FILE_NAME_ID, attachment.name),
            string_element(FILE_MEDIA_TYPE_ID, attachment.media_type or "application/octet-stream"),
            element(FILE_DATA_ID, attachment.data),
            uint_element(FILE_UID_ID, attachment.uid),
        ))
        files.append(element(ATTACHED_FILE_ID, body))
    return element(ATTACHMENTS_ID, b"".join(files)) if files else b""


def build_chapters_element(entries: list[object]) -> bytes:
    atoms: list[bytes] = []
    for index, chapter in enumerate(entries, start=1):
        seconds = float(getattr(chapter, "timecode_s", 0.0))
        name = str(getattr(chapter, "name", "") or "")
        display = element(CHAPTER_DISPLAY_ID, string_element(CHAP_STRING_ID, name) + string_element(CHAP_LANGUAGE_ID, "und"))
        atom = uint_element(CHAPTER_UID_ID, index) + uint_element(CHAPTER_TIME_START_ID, round(seconds * 1_000_000_000)) + display
        atoms.append(element(CHAPTER_ATOM_ID, atom))
    return element(CHAPTERS_ID, element(EDITION_ENTRY_ID, b"".join(atoms))) if atoms else b""


def build_tags_element(tags: dict[str, str]) -> bytes:
    simple: list[bytes] = []
    for name, value in sorted(tags.items()):
        body = string_element(TAG_NAME_ID, str(name).upper()) + string_element(TAG_STRING_ID, str(value))
        simple.append(element(SIMPLE_TAG_ID, body))
    return element(TAGS_ID, element(TAG_ID, element(TARGETS_ID, b"") + b"".join(simple))) if simple else b""


class MatroskaWriter:
    def write(self, plan: MatroskaMuxPlan) -> Path:
        destination = Path(plan.output)
        partial = destination.with_suffix(destination.suffix + ".partial")
        partial.unlink(missing_ok=True)
        packets = sorted(plan.packets, key=lambda item: (item.block.timestamp_ms, item.output_track_number, item.block.lace_index))
        duration = plan.duration_ms or max((item.block.timestamp_ms + (item.block.duration_ms or 0) for item in packets), default=0)
        info = _build_info(duration_ms=float(duration), muxing_app=plan.muxing_app, writing_app=plan.writing_app)
        tracks = element(TRACKS_ID, b"".join(_track_entry(track) for track in plan.tracks))
        with partial.open("wb") as fh:
            fh.write(_build_ebml_header())
            fh.write(SEGMENT_ID + encode_unknown_size_marker(length=8))
            fh.write(info)
            fh.write(tracks)
            # Metadata level-1 before the first Cluster is required by readers
            # that stop their metadata scan once media payload begins.
            for raw in plan.opaque_top_level:
                fh.write(raw)
            index = 0
            while index < len(packets):
                cluster_time = packets[index].block.timestamp_ms
                group: list[MatroskaMuxPacket] = []
                while index < len(packets) and packets[index].block.timestamp_ms - cluster_time <= 30_000:
                    group.append(packets[index]); index += 1
                payload = uint_element(TIMESTAMP_ID, max(0, cluster_time))
                payload += b"".join(_packet_element(packet, cluster_time) for packet in group)
                fh.write(element(CLUSTER_ID, payload))
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial.replace(destination)
        return destination


__all__ = ["MatroskaWriter", "build_attachments_element", "build_chapters_element", "build_tags_element"]
