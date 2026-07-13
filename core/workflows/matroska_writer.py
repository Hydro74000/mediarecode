"""Deterministic multi-track Matroska writer."""

from __future__ import annotations

import heapq
from io import BytesIO
from pathlib import Path
from typing import Callable

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
    CHAPTER_TIME_END_ID, CHAPTER_UID_ID, CHAP_LANGUAGE_ID, CHAP_STRING_ID, EDITION_ENTRY_ID,
    FILE_DATA_ID, FILE_DESCRIPTION_ID, FILE_MEDIA_TYPE_ID, FILE_NAME_ID, FILE_UID_ID,
    SIMPLE_TAG_ID, TAGS_ID, TAG_ID, TAG_NAME_ID, TAG_STRING_ID, TARGETS_ID,
    TAG_ATTACHMENT_UID_ID, TAG_CHAPTER_UID_ID, TAG_EDITION_UID_ID, TAG_TRACK_UID_ID,
    SEGMENT_UID_ID, TIMESTAMP_ID, TIMESTAMP_SCALE_ID, TRACK_ENTRY_ID,
    TRACK_NUMBER_ID, TRACK_UID_ID, TRACKS_ID, TITLE_ID, WRITING_APP_ID,
)
from core.workflows.matroska_mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack, deterministic_uid
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


def _element_payload(raw: bytes) -> bytes:
    stream = BytesIO(raw)
    item = read_element(stream, limit=len(raw))
    if item is None or item.size is None or item.end != len(raw):
        raise ValueError("Élément EBML brut invalide")
    return raw[item.payload_offset:item.end]


def rewrite_tag_target_uids(
    raw_tags: bytes,
    *,
    track_uids: dict[int, int],
    attachment_uids: dict[int, int],
    drop_chapter_targets: bool = False,
) -> bytes:
    """Remap copied Tag targets and discard tags whose target was not selected."""
    rebuilt_tags: list[bytes] = []
    for child_id, child_raw in _raw_children(_element_payload(raw_tags)):
        if child_id != TAG_ID:
            rebuilt_tags.append(child_raw)
            continue
        rebuilt_tag: list[bytes] = []
        keep_tag = True
        for tag_child_id, tag_child_raw in _raw_children(_element_payload(child_raw)):
            if tag_child_id != TARGETS_ID:
                rebuilt_tag.append(tag_child_raw)
                continue
            rebuilt_targets: list[bytes] = []
            for target_id, target_raw in _raw_children(_element_payload(tag_child_raw)):
                if target_id in {TAG_CHAPTER_UID_ID, TAG_EDITION_UID_ID} and drop_chapter_targets:
                    keep_tag = False
                    break
                if target_id not in {TAG_TRACK_UID_ID, TAG_ATTACHMENT_UID_ID}:
                    rebuilt_targets.append(target_raw)
                    continue
                old_uid = int.from_bytes(_element_payload(target_raw), "big")
                uid_map = track_uids if target_id == TAG_TRACK_UID_ID else attachment_uids
                if old_uid and old_uid not in uid_map:
                    keep_tag = False
                    break
                rebuilt_targets.append(uint_element(target_id, uid_map.get(old_uid, old_uid)))
            if not keep_tag:
                break
            rebuilt_tag.append(element(TARGETS_ID, b"".join(rebuilt_targets)))
        if keep_tag:
            rebuilt_tags.append(element(TAG_ID, b"".join(rebuilt_tag)))
    return element(TAGS_ID, b"".join(rebuilt_tags))


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
    return encode_vint_size_minimal(track_number) + timestamp_offset.to_bytes(2, "big", signed=True) + bytes([flags])


def _timestamp_ns(packet: MatroskaMuxPacket) -> int:
    block = packet.block
    return block.timestamp_ns if block.timestamp_ns is not None else block.timestamp_ms * 1_000_000


def _interleave_packets(packets: tuple[MatroskaMuxPacket, ...]) -> list[MatroskaMuxPacket]:
    """Interleave tracks without ever changing one track's decode order."""
    per_track: dict[int, list[MatroskaMuxPacket]] = {}
    track_order: list[int] = []
    for packet in packets:
        if packet.output_track_number not in per_track:
            per_track[packet.output_track_number] = []
            track_order.append(packet.output_track_number)
        per_track[packet.output_track_number].append(packet)
    for values in per_track.values():
        values.sort(key=lambda item: item.source_sequence)
    positions = {track_number: 0 for track_number in track_order}
    heap: list[tuple[int, int, int, MatroskaMuxPacket]] = []
    for stable_order, track_number in enumerate(track_order):
        packet = per_track[track_number][0]
        heapq.heappush(heap, (_timestamp_ns(packet), stable_order, track_number, packet))
    output: list[MatroskaMuxPacket] = []
    while heap:
        _timestamp, stable_order, track_number, packet = heapq.heappop(heap)
        output.append(packet)
        positions[track_number] += 1
        position = positions[track_number]
        if position < len(per_track[track_number]):
            next_packet = per_track[track_number][position]
            heapq.heappush(
                heap, (_timestamp_ns(next_packet), stable_order, track_number, next_packet),
            )
    return output


def _exact_ticks(value_ns: int, scale_ns: int, *, label: str) -> int:
    ticks, remainder = divmod(value_ns, scale_ns)
    if remainder:
        raise ValueError(f"{label} non représentable avec TimestampScale={scale_ns} ns")
    return ticks


def _packet_element(packet: MatroskaMuxPacket, cluster_time: int, timestamp_scale_ns: int) -> bytes:
    block = packet.block
    packet_time = _exact_ticks(_timestamp_ns(packet), timestamp_scale_ns, label="Timestamp de block")
    frame_payload = (
        block.encoded_frames_payload
        if block.lace_count > 1 and block.encoded_frames_payload
        else block.payload
    )
    raw = _block_header(packet.output_track_number, packet_time - cluster_time, block.flags) + frame_payload
    if not (
        block.duration_ms is not None or block.duration_ns is not None
        or block.references or block.references_ns or block.discard_padding_ns
        or block.codec_state or block.block_additions
    ):
        return element(SIMPLE_BLOCK_ID, raw)
    children = [element(BLOCK_ID, raw)]
    duration_ns = block.duration_ns if block.duration_ns is not None else ((block.duration_ms or 0) * 1_000_000 if block.duration_ms is not None else None)
    if duration_ns is not None:
        children.append(uint_element(BLOCK_DURATION_ID, _exact_ticks(duration_ns, timestamp_scale_ns, label="BlockDuration")))
    reference_ns = block.references_ns or tuple(value * 1_000_000 for value in block.references)
    children.extend(
        sint_element(REFERENCE_BLOCK_ID, _exact_ticks(value, timestamp_scale_ns, label="ReferenceBlock"))
        for value in reference_ns
    )
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
    def chapter_atom(chapter: object, position: tuple[int, ...]) -> bytes:
        seconds = float(getattr(chapter, "timecode_s", 0.0))
        name = str(getattr(chapter, "name", "") or "")
        language = str(getattr(chapter, "language", "und") or "und")
        uid = int(getattr(chapter, "uid", 0) or deterministic_uid("chapter", position, seconds, name))
        displays = getattr(chapter, "displays", None) or [(name, language)]
        display_elements = b"".join(
            element(
                CHAPTER_DISPLAY_ID,
                string_element(CHAP_STRING_ID, str(display_name))
                + string_element(CHAP_LANGUAGE_ID, str(display_language or "und")),
            )
            for display_name, display_language in displays
        )
        atom = (
            uint_element(CHAPTER_UID_ID, uid)
            + uint_element(CHAPTER_TIME_START_ID, round(seconds * 1_000_000_000))
        )
        end_value = getattr(chapter, "end_s", None)
        if end_value is not None:
            atom += uint_element(CHAPTER_TIME_END_ID, round(float(end_value) * 1_000_000_000))
        atom += display_elements
        atom += b"".join(
            chapter_atom(child, (*position, child_index))
            for child_index, child in enumerate(getattr(chapter, "children", ()) or (), start=1)
        )
        return element(CHAPTER_ATOM_ID, atom)

    atoms = [chapter_atom(chapter, (index,)) for index, chapter in enumerate(entries, start=1)]
    return element(CHAPTERS_ID, element(EDITION_ENTRY_ID, b"".join(atoms))) if atoms else b""


def build_tags_element(tags: dict[str, str]) -> bytes:
    simple: list[bytes] = []
    for name, value in sorted(tags.items()):
        body = string_element(TAG_NAME_ID, str(name).upper()) + string_element(TAG_STRING_ID, str(value))
        simple.append(element(SIMPLE_TAG_ID, body))
    return element(TAGS_ID, element(TAG_ID, element(TARGETS_ID, b"") + b"".join(simple))) if simple else b""


def _plan_info(plan: MatroskaMuxPlan, duration_ns: int) -> bytes:
    return element(INFO_ID, b"".join((
        uint_element(TIMESTAMP_SCALE_ID, plan.timestamp_scale_ns),
        float_element(DURATION_ID, float(duration_ns) / plan.timestamp_scale_ns),
        binary_element(SEGMENT_UID_ID, plan.segment_uid.to_bytes(16, "big")),
        string_element(TITLE_ID, plan.title) if plan.title else b"",
        string_element(MUXING_APP_ID, plan.muxing_app),
        string_element(WRITING_APP_ID, plan.writing_app),
    )))


class MatroskaWriter:
    def write(
        self,
        plan: MatroskaMuxPlan,
        *,
        external_validator: Callable[[Path], None] | None = None,
    ) -> Path:
        destination = Path(plan.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".partial")
        partial.unlink(missing_ok=True)
        packets = _interleave_packets(plan.packets)
        duration_ns = plan.duration_ns or (plan.duration_ms * 1_000_000) or max((
            _timestamp_ns(item)
            + (item.block.duration_ns if item.block.duration_ns is not None else (item.block.duration_ms or 0) * 1_000_000)
            for item in packets
        ), default=0)
        info = _plan_info(plan, duration_ns)
        tracks = element(TRACKS_ID, b"".join(_track_entry(track) for track in plan.tracks))
        try:
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
                    group: list[MatroskaMuxPacket] = []
                    group_min_ns = group_max_ns = _timestamp_ns(packets[index])
                    max_cluster_ns = min(
                        30_000_000_000,
                        32767 * plan.timestamp_scale_ns,
                    )
                    while index < len(packets):
                        packet_ns = _timestamp_ns(packets[index])
                        candidate_min = min(group_min_ns, packet_ns)
                        candidate_max = max(group_max_ns, packet_ns)
                        if group and candidate_max - candidate_min > max_cluster_ns:
                            break
                        group.append(packets[index])
                        group_min_ns, group_max_ns = candidate_min, candidate_max
                        index += 1
                    cluster_time = _exact_ticks(group_min_ns, plan.timestamp_scale_ns, label="Cluster.Timestamp")
                    timestamp_element = uint_element(TIMESTAMP_ID, max(0, cluster_time))
                    packet_elements = [
                        _packet_element(packet, cluster_time, plan.timestamp_scale_ns)
                        for packet in group
                    ]
                    payload = timestamp_element + b"".join(packet_elements)
                    cluster_element = element(CLUSTER_ID, payload)
                    cluster_offset = fh.tell() - payload_start
                    fh.write(cluster_element)
                    cluster_header_size = len(cluster_element) - len(payload)
                    relative_position = cluster_header_size + len(timestamp_element)
                    cue_points: list[tuple[int, int]] = []
                    for packet, packet_raw in zip(group, packet_elements):
                        if packet.output_track_number == video_track and packet.block.flags & 0x80:
                            key_time = _exact_ticks(_timestamp_ns(packet), plan.timestamp_scale_ns, label="CueTime")
                            cue_points.append((key_time, relative_position))
                        relative_position += len(packet_raw)
                    if not cue_points and not any(track.source_track.track_type == 1 for track in plan.tracks):
                        first_packet = next((item for item in group if item.output_track_number == video_track), None)
                        if first_packet is not None:
                            cue_points.append((
                                _exact_ticks(_timestamp_ns(first_packet), plan.timestamp_scale_ns, label="CueTime"),
                                cluster_header_size + len(timestamp_element),
                            ))
                    if cue_points:
                        cluster_records.append(_ClusterRecord(cluster_offset, cluster_time, cue_points))
                cues_offset = fh.tell() - payload_start
                fh.write(_build_cues(cluster_records, video_track))
                seek = _build_seek_head(
                    [(INFO_ID, info_offset), (TRACKS_ID, tracks_offset), (CUES_ID, cues_offset)],
                    total_size=seek_reserved,
                )
                fh.seek(payload_start)
                fh.write(seek)
            from core.workflows.matroska_reader import MatroskaReader

            validation_reader = MatroskaReader(partial)
            validation_reader.segment()
            if len(validation_reader.tracks()) != len(plan.tracks):
                raise ValueError("Validation native : nombre de pistes incohérent")
            if external_validator is not None:
                external_validator(partial)
            partial.replace(destination)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        return destination


__all__ = [
    "MatroskaWriter", "build_attachments_element", "build_chapters_element",
    "build_tags_element", "rewrite_tag_target_uids",
]
