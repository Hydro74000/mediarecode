"""Deterministic multi-track Matroska writer."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from core.workflows.ebml_writer import (
    binary_element, element, encode_unknown_size_marker, encode_vint_size_minimal,
    float_element, sint_element, string_element, uint_element,
)
from core.workflows.matroska_element_ids import (
    ATTACHMENTS_ID, ATTACHED_FILE_ID,
    BLOCK_ADDITIONS_ID, BLOCK_DURATION_ID, BLOCK_GROUP_ID, BLOCK_ID,
    CLUSTER_ID, CODEC_STATE_ID, CUES_ID, DISCARD_PADDING_ID,
    FLAG_COMMENTARY_ID, FLAG_DEFAULT_ID, FLAG_ENABLED_ID, FLAG_FORCED_ID,
    FLAG_HEARING_IMPAIRED_ID, FLAG_ORIGINAL_ID, FLAG_VISUAL_IMPAIRED_ID,
    DURATION_ID, INFO_ID, LANGUAGE_BCP47_ID, MUXING_APP_ID,
    LANGUAGE_ID, NAME_ID, REFERENCE_BLOCK_ID, SEGMENT_ID, SIMPLE_BLOCK_ID,
    CHAPTERS_ID, CHAPTER_ATOM_ID, CHAPTER_DISPLAY_ID, CHAPTER_TIME_START_ID,
    CHAPTER_UID_ID, CHAP_LANGUAGE_ID, CHAP_STRING_ID, EDITION_ENTRY_ID,
    FILE_DATA_ID, FILE_DESCRIPTION_ID, FILE_MEDIA_TYPE_ID, FILE_NAME_ID, FILE_UID_ID,
    SIMPLE_TAG_ID, TAGS_ID, TAG_ID, TAG_NAME_ID, TAG_STRING_ID, TARGETS_ID,
    SEGMENT_UID_ID, TIMESTAMP_ID, TIMESTAMP_SCALE_ID, TRACK_ENTRY_ID,
    TRACK_NUMBER_ID, TRACK_UID_ID, TRACKS_ID, WRITING_APP_ID,
)
from core.workflows.matroska_mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack
from core.workflows.matroska_native_muxer import (
    _ClusterRecord, _build_cues, _build_ebml_header, _build_seek_head,
)
from core.workflows.matroska_reader import read_element
from core.workflows.matroska_reader import MatroskaAttachment


_PATCHED_TRACK_FIELDS = {
    TRACK_NUMBER_ID, TRACK_UID_ID, LANGUAGE_ID, LANGUAGE_BCP47_ID,
    NAME_ID, FLAG_ENABLED_ID, FLAG_DEFAULT_ID, FLAG_FORCED_ID,
    FLAG_HEARING_IMPAIRED_ID, FLAG_VISUAL_IMPAIRED_ID, FLAG_ORIGINAL_ID,
    FLAG_COMMENTARY_ID,
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
        uint_element(FLAG_FORCED_ID, int(track.flag_forced)),
        uint_element(FLAG_HEARING_IMPAIRED_ID, int(track.flag_hearing_impaired)),
        uint_element(FLAG_VISUAL_IMPAIRED_ID, int(track.flag_visual_impaired)),
        uint_element(FLAG_ORIGINAL_ID, int(track.flag_original)),
        uint_element(FLAG_COMMENTARY_ID, int(track.flag_commentary)),
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


def _plan_info(plan: MatroskaMuxPlan, duration_ms: int) -> bytes:
    return element(INFO_ID, b"".join((
        uint_element(TIMESTAMP_SCALE_ID, plan.timestamp_scale_ns),
        float_element(DURATION_ID, float(duration_ms)),
        binary_element(SEGMENT_UID_ID, plan.segment_uid.to_bytes(16, "big")),
        string_element(MUXING_APP_ID, plan.muxing_app),
        string_element(WRITING_APP_ID, plan.writing_app),
    )))


class MatroskaWriter:
    def write(self, plan: MatroskaMuxPlan) -> Path:
        destination = Path(plan.output)
        partial = destination.with_suffix(destination.suffix + ".partial")
        partial.unlink(missing_ok=True)
        packets = sorted(plan.packets, key=lambda item: (item.block.timestamp_ms, item.output_track_number, item.block.lace_index))
        duration = plan.duration_ms or max((item.block.timestamp_ms + (item.block.duration_ms or 0) for item in packets), default=0)
        info = _plan_info(plan, duration)
        tracks = element(TRACKS_ID, b"".join(_track_entry(track) for track in plan.tracks))
        with partial.open("wb") as fh:
            fh.write(_build_ebml_header())
            fh.write(SEGMENT_ID + encode_unknown_size_marker(length=8))
            payload_start = fh.tell()
            seek_reserved = 512
            fh.write(b"\0" * seek_reserved)
            info_offset = fh.tell() - payload_start
            fh.write(info)
            tracks_offset = fh.tell() - payload_start
            fh.write(tracks)
            # Metadata level-1 before the first Cluster is required by readers
            # that stop their metadata scan once media payload begins.
            for raw in plan.opaque_top_level:
                fh.write(raw)
            index = 0
            cluster_records: list[_ClusterRecord] = []
            video_track = next((track.output_number for track in plan.tracks if track.source_track.track_type == 1), plan.tracks[0].output_number)
            while index < len(packets):
                cluster_time = packets[index].block.timestamp_ms
                group: list[MatroskaMuxPacket] = []
                while index < len(packets) and packets[index].block.timestamp_ms - cluster_time <= 30_000:
                    group.append(packets[index]); index += 1
                payload = uint_element(TIMESTAMP_ID, max(0, cluster_time))
                payload += b"".join(_packet_element(packet, cluster_time) for packet in group)
                cluster_offset = fh.tell() - payload_start
                fh.write(element(CLUSTER_ID, payload))
                key_packet = next((packet for packet in group if packet.output_track_number == video_track and packet.block.flags & 0x80), None)
                if key_packet is not None:
                    cluster_records.append(_ClusterRecord(cluster_offset, cluster_time, [(key_packet.block.timestamp_ms, 0)]))
            cues_offset = fh.tell() - payload_start
            fh.write(_build_cues(cluster_records, video_track))
            seek = _build_seek_head(
                [(INFO_ID, info_offset), (TRACKS_ID, tracks_offset), (CUES_ID, cues_offset)],
                total_size=seek_reserved,
            )
            fh.seek(payload_start)
            fh.write(seek)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial.replace(destination)
        return destination


__all__ = ["MatroskaWriter", "build_attachments_element", "build_chapters_element", "build_tags_element"]
