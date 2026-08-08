"""Deterministic multi-track Matroska writer."""

from __future__ import annotations

import heapq
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Callable

from .ebml import (
    binary_element, element, encode_unknown_size_marker, encode_vint_size,
    encode_vint_size_minimal,
    float_element, sint_element, string_element, uint_element,
)
from .ids import (
    ATTACHMENTS_ID, ATTACHED_FILE_ID,
    BLOCK_ADDITIONS_ID, BLOCK_DURATION_ID, BLOCK_GROUP_ID, BLOCK_ID,
    CLUSTER_ID, CODEC_STATE_ID, CUES_ID, DISCARD_PADDING_ID,
    FLAG_COMMENTARY_ID, FLAG_DEFAULT_ID, FLAG_ENABLED_ID, FLAG_FORCED_ID,
    FLAG_HEARING_IMPAIRED_ID, FLAG_ORIGINAL_ID, FLAG_VISUAL_IMPAIRED_ID,
    CRC32_ID, DURATION_ID, INFO_ID, LANGUAGE_BCP47_ID, MUXING_APP_ID,
    LANGUAGE_ID, NAME_ID, REFERENCE_BLOCK_ID, SEGMENT_ID, SIMPLE_BLOCK_ID,
    CHAPTERS_ID, CHAPTER_ATOM_ID, CHAPTER_DISPLAY_ID, CHAPTER_TIME_START_ID,
    CHAPTER_TIME_END_ID, CHAPTER_UID_ID, CHAP_LANGUAGE_ID, CHAP_STRING_ID, EDITION_ENTRY_ID,
    FILE_DATA_ID, FILE_DESCRIPTION_ID, FILE_MEDIA_TYPE_ID, FILE_NAME_ID, FILE_UID_ID,
    SIMPLE_TAG_ID, TAGS_ID, TAG_ID, TAG_NAME_ID, TAG_STRING_ID, TARGETS_ID,
    TAG_ATTACHMENT_UID_ID, TAG_CHAPTER_UID_ID, TAG_EDITION_UID_ID, TAG_TRACK_UID_ID,
    SEGMENT_UID_ID, TIMESTAMP_ID, TIMESTAMP_SCALE_ID, TRACK_ENTRY_ID,
    TRACK_NUMBER_ID, TRACK_UID_ID, TRACKS_ID, TITLE_ID, WRITING_APP_ID,
)
from .mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack, deterministic_uid
from .native_muxer import (
    _ClusterRecord, _build_cues, _build_ebml_header, _build_seek_head,
)
from .reader import read_element
from .reader import MatroskaAttachment
from .validation import MatroskaPacketValidation


#: Charge utile maximale d'un Cluster (octets). Borne le pic mémoire du
#: writer (groupe courant + copies transitoires) indépendamment du flux.
_MAX_CLUSTER_PAYLOAD_BYTES = 4 * 1024 * 1024

_PATCHED_TRACK_FLAG_FIELDS = {
    FLAG_ENABLED_ID, FLAG_DEFAULT_ID, FLAG_FORCED_ID,
    FLAG_HEARING_IMPAIRED_ID, FLAG_VISUAL_IMPAIRED_ID, FLAG_ORIGINAL_ID,
    FLAG_COMMENTARY_ID,
}
_PATCHED_TRACK_LANGUAGE_FIELDS = {LANGUAGE_ID, LANGUAGE_BCP47_ID}
_TRACK_STATISTICS_TAGS = frozenset({
    "BPS", "DURATION", "NUMBER_OF_FRAMES", "NUMBER_OF_BYTES",
    "_STATISTICS_WRITING_APP", "_STATISTICS_WRITING_DATE_UTC",
    "_STATISTICS_TAGS",
})
_STATISTICS_TAG_NAMES = (
    "BPS", "DURATION", "NUMBER_OF_FRAMES", "NUMBER_OF_BYTES",
)


def _raw_children(payload: bytes) -> list[tuple[bytes, bytes]]:
    """Enfants bruts d'un master en cours de réassemblage.

    Les CRC-32 source sont écartés : le payload parent étant modifié, un CRC
    recopié deviendrait invalide (l'élément est optionnel selon la spec EBML).
    """
    out: list[tuple[bytes, bytes]] = []
    stream = BytesIO(payload)
    while stream.tell() < len(payload):
        item = read_element(stream, limit=len(payload))
        if item is None or item.size is None:
            raise ValueError("TrackEntry EBML invalide")
        stream.seek(item.offset)
        raw = stream.read(item.header_size + item.size)
        if item.element_id != CRC32_ID:
            out.append((item.element_id, raw))
        stream.seek(item.payload_offset + item.size)
    return out


def _element_payload(raw: bytes) -> bytes:
    stream = BytesIO(raw)
    item = read_element(stream, limit=len(raw))
    if item is None or item.size is None or item.end != len(raw):
        raise ValueError("Élément EBML brut invalide")
    return raw[item.payload_offset:item.end]


def _simple_tag_name(raw_simple_tag: bytes) -> str:
    """Return a direct SimpleTag name without altering nested tag payloads."""
    for child_id, child_raw in _raw_children(_element_payload(raw_simple_tag)):
        if child_id == TAG_NAME_ID:
            return _element_payload(child_raw).decode("utf-8", "replace").rstrip("\0")
    return ""


def rewrite_tag_target_uids(
    raw_tags: bytes,
    *,
    track_uids: dict[int, int],
    attachment_uids: dict[int, int],
    drop_chapter_targets: bool = False,
    drop_track_statistics: bool = False,
) -> bytes:
    """Remap copied Tag targets and discard tags whose target was not selected.

    Fresh per-track statistics are generated from packets by the native
    assembler.  When requested, stale technical statistics attached to a
    selected source track are dropped before those fresh values are appended.
    """
    rebuilt_tags: list[bytes] = []
    for child_id, child_raw in _raw_children(_element_payload(raw_tags)):
        if child_id != TAG_ID:
            rebuilt_tags.append(child_raw)
            continue
        rebuilt_tag: list[bytes] = []
        keep_tag = True
        remapped_track_target = False
        has_tag_value = False
        for tag_child_id, tag_child_raw in _raw_children(_element_payload(child_raw)):
            if tag_child_id == SIMPLE_TAG_ID:
                if (
                    drop_track_statistics
                    and remapped_track_target
                    and _simple_tag_name(tag_child_raw).upper() in _TRACK_STATISTICS_TAGS
                ):
                    continue
                rebuilt_tag.append(tag_child_raw)
                has_tag_value = True
                continue
            if tag_child_id != TARGETS_ID:
                rebuilt_tag.append(tag_child_raw)
                has_tag_value = True
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
                if target_id == TAG_TRACK_UID_ID and old_uid in track_uids:
                    remapped_track_target = True
                rebuilt_targets.append(uint_element(target_id, uid_map.get(old_uid, old_uid)))
            if not keep_tag:
                break
            rebuilt_tag.append(element(TARGETS_ID, b"".join(rebuilt_targets)))
        if keep_tag and has_tag_value:
            rebuilt_tags.append(element(TAG_ID, b"".join(rebuilt_tag)))
    return element(TAGS_ID, b"".join(rebuilt_tags))


def _track_entry(track: MatroskaMuxTrack) -> bytes:
    patched_ids = {TRACK_NUMBER_ID, TRACK_UID_ID}
    if track.patch_flags:
        patched_ids |= _PATCHED_TRACK_FLAG_FIELDS
    if track.patch_language:
        patched_ids |= _PATCHED_TRACK_LANGUAGE_FIELDS
    if track.patch_name:
        patched_ids.add(NAME_ID)
    kept = b"".join(raw for element_id, raw in _raw_children(track.source_track.raw_entry) if element_id not in patched_ids)
    parts: list[bytes] = [
        uint_element(TRACK_NUMBER_ID, track.output_number),
        uint_element(TRACK_UID_ID, track.output_uid),
    ]
    if track.patch_flags:
        parts.extend((
            uint_element(FLAG_ENABLED_ID, int(track.flag_enabled)),
            uint_element(FLAG_DEFAULT_ID, int(track.flag_default)),
            uint_element(FLAG_FORCED_ID, int(track.flag_forced)),
            uint_element(FLAG_HEARING_IMPAIRED_ID, int(track.flag_hearing_impaired)),
            uint_element(FLAG_VISUAL_IMPAIRED_ID, int(track.flag_visual_impaired)),
            uint_element(FLAG_ORIGINAL_ID, int(track.flag_original)),
            uint_element(FLAG_COMMENTARY_ID, int(track.flag_commentary)),
        ))
    if track.patch_language:
        parts.append(string_element(LANGUAGE_ID, track.language or "und"))
        if track.language_bcp47:
            parts.append(string_element(LANGUAGE_BCP47_ID, track.language_bcp47))
    if track.patch_name and track.name:
        parts.append(string_element(NAME_ID, track.name))
    return element(TRACK_ENTRY_ID, b"".join(parts) + kept)


def _block_header(track_number: int, timestamp_offset: int, flags: int) -> bytes:
    if not -32768 <= timestamp_offset <= 32767:
        raise ValueError("Offset Block hors int16")
    return encode_vint_size_minimal(track_number) + timestamp_offset.to_bytes(2, "big", signed=True) + bytes([flags])


def _timestamp_ns(packet: MatroskaMuxPacket) -> int:
    block = packet.block
    return block.timestamp_ns if block.timestamp_ns is not None else block.timestamp_ms * 1_000_000


def _is_keyframe(packet: MatroskaMuxPacket) -> bool:
    value = packet.block.is_keyframe
    return bool(packet.block.flags & 0x80) if value is None else value


def _explicit_duration_ns(packet: MatroskaMuxPacket) -> int | None:
    block = packet.block
    if block.duration_ns is not None:
        return block.duration_ns
    if block.duration_ms is not None:
        return block.duration_ms * 1_000_000
    return None


def _effective_frame_payload(packet: MatroskaMuxPacket) -> bytes:
    """Payload réellement écrit : payload lacé complet pour un block lacé."""
    block = packet.block
    if block.lace_count > 1 and block.encoded_frames_payload:
        return block.encoded_frames_payload
    return block.payload


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
    frame_payload = _effective_frame_payload(packet)
    # Bits communs SimpleBlock/Block : invisible (0x08) + lacing (0x06).
    shared_flags = block.flags & 0x0E
    if not (
        block.duration_ms is not None or block.duration_ns is not None
        or block.references or block.references_ns or block.discard_padding_ns
        or block.codec_state or block.block_additions
    ):
        # SimpleBlock : le bit keyframe est recalculé — une keyframe issue
        # d'un BlockGroup source (signalée par l'absence de ReferenceBlock)
        # doit rester signalée ici par le bit 0x80.
        simple_flags = shared_flags | (block.flags & 0x01)
        if _is_keyframe(packet):
            simple_flags |= 0x80
        raw = _block_header(packet.output_track_number, packet_time - cluster_time, simple_flags) + frame_payload
        return element(SIMPLE_BLOCK_ID, raw)
    # Block (BlockGroup) : pas de bit keyframe ni discardable — la keyframe
    # est signalée par l'absence de ReferenceBlock.
    raw = _block_header(packet.output_track_number, packet_time - cluster_time, shared_flags) + frame_payload
    children = [element(BLOCK_ID, raw)]
    duration_ns = block.duration_ns if block.duration_ns is not None else ((block.duration_ms or 0) * 1_000_000 if block.duration_ms is not None else None)
    if duration_ns is not None:
        children.append(uint_element(BLOCK_DURATION_ID, _exact_ticks(duration_ns, timestamp_scale_ns, label="BlockDuration")))
    reference_ns = block.references_ns or tuple(value * 1_000_000 for value in block.references)
    children.extend(
        sint_element(REFERENCE_BLOCK_ID, _exact_ticks(value, timestamp_scale_ns, label="ReferenceBlock"))
        for value in reference_ns
    )
    if block.discard_padding_ns:
        children.append(sint_element(DISCARD_PADDING_ID, block.discard_padding_ns))
    if block.codec_state:
        children.append(element(CODEC_STATE_ID, block.codec_state))
    if block.block_additions:
        children.append(element(BLOCK_ADDITIONS_ID, block.block_additions))
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


def _format_statistics_duration(duration_ns: int) -> str:
    seconds, nanos = divmod(max(0, duration_ns), 1_000_000_000)
    hours, seconds = divmod(seconds, 3_600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{nanos:09d}"


def build_track_statistics_tags_element(
    statistics: dict[int, tuple[int, int, int]],
    *,
    writing_app: str = "Muxiveo",
    written_at_utc: datetime | None = None,
) -> bytes:
    """Build per-track statistics recognized by MediaInfo.

    Each value is ``(frame_count, payload_bytes, duration_ns)``.  These are
    ordinary Matroska tags rather than EBML fields.  MediaInfo only promotes
    ``NUMBER_OF_FRAMES`` to the text-subtitle ``ElementCount`` when the
    mkvmerge-compatible ``_STATISTICS_*`` companion tags are present.
    """
    written_at = written_at_utc or datetime.now(timezone.utc)
    if written_at.tzinfo is None:
        raise ValueError("La date des statistiques doit être exprimée en UTC")
    written_at = written_at.astimezone(timezone.utc)
    written_at_text = written_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    tags: list[bytes] = []
    for track_uid, (frame_count, payload_bytes, duration_ns) in sorted(statistics.items()):
        if frame_count <= 0:
            continue
        # Keep statistics at millisecond precision even when the Matroska
        # TimestampScale is finer; this is what common inspection tools
        # expect when presenting BPS and DURATION.
        statistics_duration_ns = ((max(0, duration_ns) + 500_000) // 1_000_000) * 1_000_000
        values = {
            "BPS": str((payload_bytes * 8_000_000_000) // statistics_duration_ns) if statistics_duration_ns else "0",
            "DURATION": _format_statistics_duration(statistics_duration_ns),
            "NUMBER_OF_FRAMES": str(frame_count),
            "NUMBER_OF_BYTES": str(payload_bytes),
            "_STATISTICS_WRITING_APP": writing_app,
            "_STATISTICS_WRITING_DATE_UTC": written_at_text,
            "_STATISTICS_TAGS": " ".join(_STATISTICS_TAG_NAMES),
        }
        simple = b"".join(
            element(
                SIMPLE_TAG_ID,
                string_element(TAG_NAME_ID, name) + string_element(TAG_STRING_ID, value),
            )
            for name, value in values.items()
        )
        tags.append(element(
            TAG_ID,
            element(TARGETS_ID, uint_element(TAG_TRACK_UID_ID, track_uid)) + simple,
        ))
    return element(TAGS_ID, b"".join(tags)) if tags else b""


def _tag_target_track_uids(raw_tag: bytes) -> set[int]:
    """UID de pistes ciblés par un Tag (vide quand la cible est globale)."""
    uids: set[int] = set()
    for child_id, child_raw in _raw_children(_element_payload(raw_tag)):
        if child_id != TARGETS_ID:
            continue
        for target_id, target_raw in _raw_children(_element_payload(child_raw)):
            if target_id == TAG_TRACK_UID_ID:
                uid = int.from_bytes(_element_payload(target_raw), "big")
                if uid:
                    uids.add(uid)
    return uids


def merge_track_statistics_tags(
    existing_tags: Iterable[bytes],
    statistics: dict[int, tuple[int, int, int]],
    *,
    writing_app: str = "Muxiveo",
    written_at_utc: datetime | None = None,
) -> bytes:
    """Fusionne des Tags existants avec des statistiques de pistes fraîches.

    Les statistiques héritées des pistes recalculées sont retirées (valeurs
    obsolètes après sélection, remap ou décalage), les autres tags — globaux
    comme par piste — sont conservés tels quels.  Retourne un unique élément
    ``Tags``, ou ``b""`` quand il ne reste rien à écrire.
    """
    rebuilt: list[bytes] = []
    for raw_tags in existing_tags:
        for child_id, child_raw in _raw_children(_element_payload(raw_tags)):
            if child_id != TAG_ID or not (_tag_target_track_uids(child_raw) & statistics.keys()):
                rebuilt.append(child_raw)
                continue
            kept: list[bytes] = []
            has_value = False
            for tag_child_id, tag_child_raw in _raw_children(_element_payload(child_raw)):
                if (
                    tag_child_id == SIMPLE_TAG_ID
                    and _simple_tag_name(tag_child_raw).upper() in _TRACK_STATISTICS_TAGS
                ):
                    continue
                kept.append(tag_child_raw)
                has_value = has_value or tag_child_id == SIMPLE_TAG_ID
            if has_value:
                rebuilt.append(element(TAG_ID, b"".join(kept)))
    fresh = build_track_statistics_tags_element(
        statistics, writing_app=writing_app, written_at_utc=written_at_utc,
    )
    if fresh:
        rebuilt.extend(raw for _child_id, raw in _raw_children(_element_payload(fresh)))
    return element(TAGS_ID, b"".join(rebuilt)) if rebuilt else b""


def _plan_info(plan: MatroskaMuxPlan, duration_ns: int) -> bytes:
    return element(INFO_ID, b"".join((
        uint_element(TIMESTAMP_SCALE_ID, plan.timestamp_scale_ns),
        float_element(DURATION_ID, float(duration_ns) / plan.timestamp_scale_ns),
        binary_element(SEGMENT_UID_ID, plan.segment_uid.to_bytes(16, "big")),
        string_element(TITLE_ID, plan.title) if plan.title else b"",
        string_element(MUXING_APP_ID, plan.muxing_app),
        string_element(WRITING_APP_ID, plan.writing_app),
    )))


class MatroskaWriteCancelled(Exception):
    """Annulation coopérative demandée pendant l'écriture native."""


@dataclass(frozen=True)
class MatroskaWriteProgress:
    """Progression d'écriture : étape, paquets, octets écrits, candidat."""

    stage: str
    packets_written: int
    bytes_written: int
    candidate: Path


class MatroskaWriter:
    def write(
        self,
        plan: MatroskaMuxPlan,
        *,
        external_validator: Callable[[Path, MatroskaPacketValidation], None] | None = None,
        cancel_cb: Callable[[], bool] | None = None,
        progress_cb: Callable[[MatroskaWriteProgress], None] | None = None,
    ) -> Path:
        destination = Path(plan.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".partial")
        partial.unlink(missing_ok=True)

        packets_written = 0
        packet_counts: dict[int, int] = {}

        def _check_cancel(stage: str) -> None:
            if cancel_cb is not None and cancel_cb():
                raise MatroskaWriteCancelled(f"Écriture Matroska annulée ({stage})")

        def _notify(stage: str, bytes_written: int) -> None:
            if progress_cb is not None:
                progress_cb(MatroskaWriteProgress(
                    stage=stage,
                    packets_written=packets_written,
                    bytes_written=bytes_written,
                    candidate=partial,
                ))
        if isinstance(plan.packets, (tuple, list)):
            packets: Iterable[MatroskaMuxPacket] = _interleave_packets(tuple(plan.packets))
        else:
            # Flux paresseux : l'ordre de mux vient du producteur, consommé
            # une seule fois (mémoire bornée).
            packets = plan.packets
        # Durée du plan si connue ; sinon 0, puis Info réécrite à taille
        # égale après consommation avec la fin réelle observée.
        duration_ns = plan.duration_ns or (plan.duration_ms * 1_000_000)
        info = _plan_info(plan, duration_ns)
        tracks = element(TRACKS_ID, b"".join(_track_entry(track) for track in plan.tracks))
        subtitle_tracks = {
            track.output_number for track in plan.tracks
            if track.source_track.track_type == 17
        }
        default_duration_by_track = {
            track.output_number: track.source_track.default_duration_ns
            for track in plan.tracks
        }
        # Fin réelle par piste, toutes pistes confondues (les muxeurs de
        # référence prolongent la durée jusqu'au dernier sous-titre) : durée
        # explicite du block, sinon DefaultDuration × laces, sinon dernier
        # delta positif observé. Ce dernier repli (pistes sans
        # DefaultDuration, ex. FLAC) majore la fin d'au plus un inter-block
        # quand la dernière frame est plus courte — information codec
        # inaccessible au niveau conteneur.
        last_timestamp_by_track: dict[int, int] = {}
        last_delta_by_track: dict[int, int] = {}
        observed_end_ns = 0
        validation_max_packet_timestamp_ns: int | None = None

        def _note_packet_end(packet: MatroskaMuxPacket) -> None:
            nonlocal observed_end_ns, validation_max_packet_timestamp_ns
            track_number = packet.output_track_number
            timestamp = _timestamp_ns(packet)
            previous = last_timestamp_by_track.get(track_number)
            if previous is not None and timestamp > previous:
                last_delta_by_track[track_number] = timestamp - previous
            last_timestamp_by_track[track_number] = timestamp
            # Même calcul que validate_matroska_output : seules les durées
            # explicites des blocks interviennent dans la borne supérieure.
            validation_duration = _explicit_duration_ns(packet) or 0
            validation_packet_end = timestamp + validation_duration
            validation_max_packet_timestamp_ns = max(
                validation_max_packet_timestamp_ns or validation_packet_end,
                validation_packet_end,
            )
            duration = _explicit_duration_ns(packet)
            if duration is None:
                default_duration = default_duration_by_track.get(track_number, 0)
                if default_duration:
                    duration = default_duration * max(1, packet.block.lace_count)
                else:
                    duration = last_delta_by_track.get(track_number, 0)
            observed_end_ns = max(observed_end_ns, timestamp + duration)

        try:
            with partial.open("wb") as fh:
                fh.write(_build_ebml_header())
                # Taille de Segment : VINT 8 octets « inconnue » réservée,
                # patchée avec la taille réelle en fin d'écriture.
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
                opaque_seek_entries: list[tuple[bytes, int]] = []
                for raw in plan.opaque_top_level:
                    raw_stream = BytesIO(raw)
                    raw_item = read_element(raw_stream, limit=len(raw))
                    if raw_item is not None:
                        opaque_seek_entries.append((raw_item.element_id, fh.tell() - payload_start))
                    fh.write(raw)
                cluster_records: list[_ClusterRecord] = []
                # Sélection des Cues par TrackType : keyframes de toutes les
                # pistes vidéo ; chaque sous-titre avec sa durée ; sans piste
                # vidéo, au plus un point de la piste audio primaire
                # (première piste audio du plan) par Cluster.
                video_tracks = {
                    track.output_number for track in plan.tracks
                    if track.source_track.track_type == 1
                }
                primary_audio_track = None if video_tracks else next(
                    (track.output_number for track in plan.tracks
                     if track.source_track.track_type == 2),
                    None,
                )
                max_cluster_ns = min(
                    30_000_000_000,
                    32767 * plan.timestamp_scale_ns,
                )
                packet_iter = iter(packets)
                pending = next(packet_iter, None)
                while pending is not None:
                    _check_cancel("clusters")
                    group: list[MatroskaMuxPacket] = [pending]
                    group_min_ns = group_max_ns = _timestamp_ns(pending)
                    group_bytes = len(_effective_frame_payload(pending))
                    _note_packet_end(pending)
                    pending = next(packet_iter, None)
                    while pending is not None:
                        packet_ns = _timestamp_ns(pending)
                        candidate_min = min(group_min_ns, packet_ns)
                        candidate_max = max(group_max_ns, packet_ns)
                        if candidate_max - candidate_min > max_cluster_ns:
                            break
                        # Borne d'octets par Cluster : garde le pic mémoire du
                        # writer fixe, indépendant de la taille du flux (lot 3).
                        if group_bytes + len(_effective_frame_payload(pending)) > _MAX_CLUSTER_PAYLOAD_BYTES:
                            break
                        group.append(pending)
                        group_min_ns, group_max_ns = candidate_min, candidate_max
                        group_bytes += len(_effective_frame_payload(pending))
                        _note_packet_end(pending)
                        pending = next(packet_iter, None)
                    # Timestamp Cluster écrit (uint ≥ 0) : les offsets de
                    # blocks sont calculés contre cette même valeur pour que
                    # les timestamps absolus restent exacts.
                    cluster_time = max(0, _exact_ticks(group_min_ns, plan.timestamp_scale_ns, label="Cluster.Timestamp"))
                    timestamp_element = uint_element(TIMESTAMP_ID, cluster_time)
                    packet_elements = [
                        _packet_element(packet, cluster_time, plan.timestamp_scale_ns)
                        for packet in group
                    ]
                    payload = timestamp_element + b"".join(packet_elements)
                    cluster_element = element(CLUSTER_ID, payload)
                    cluster_offset = fh.tell() - payload_start
                    fh.write(cluster_element)
                    # CueRelativePosition (RFC 9559) : relatif au premier
                    # octet du payload du Cluster (0 = premier élément).
                    relative_position = len(timestamp_element)
                    cue_points: list[tuple[int, int, int, int | None]] = []
                    audio_cue: tuple[int, int, int, int | None] | None = None
                    for packet, packet_raw in zip(group, packet_elements):
                        if packet.output_track_number in video_tracks and _is_keyframe(packet):
                            key_time = _exact_ticks(_timestamp_ns(packet), plan.timestamp_scale_ns, label="CueTime")
                            cue_points.append((key_time, packet.output_track_number, relative_position, None))
                        elif packet.output_track_number in subtitle_tracks:
                            # Index sous-titres : chaque entrée, avec durée.
                            entry_time = _exact_ticks(_timestamp_ns(packet), plan.timestamp_scale_ns, label="CueTime")
                            entry_duration = _explicit_duration_ns(packet)
                            cue_points.append((
                                entry_time, packet.output_track_number, relative_position,
                                _exact_ticks(entry_duration, plan.timestamp_scale_ns, label="CueDuration")
                                if entry_duration else None,
                            ))
                        elif packet.output_track_number == primary_audio_track and audio_cue is None:
                            audio_cue = (
                                _exact_ticks(_timestamp_ns(packet), plan.timestamp_scale_ns, label="CueTime"),
                                packet.output_track_number, relative_position, None,
                            )
                        relative_position += len(packet_raw)
                    if audio_cue is not None:
                        cue_points.append(audio_cue)
                    if cue_points:
                        cluster_records.append(_ClusterRecord(cluster_offset, cluster_time, cue_points))
                    packets_written += len(group)
                    for packet in group:
                        packet_counts[packet.output_track_number] = (
                            packet_counts.get(packet.output_track_number, 0) + 1
                        )
                    _notify("clusters", fh.tell())
                _check_cancel("cues")
                cues_offset = fh.tell() - payload_start
                fh.write(_build_cues(cluster_records))
                seek = _build_seek_head(
                    [
                        (INFO_ID, info_offset),
                        (TRACKS_ID, tracks_offset),
                        *opaque_seek_entries,
                        (CUES_ID, cues_offset),
                    ],
                    total_size=seek_reserved,
                )
                fh.seek(payload_start)
                fh.write(seek)
                if not duration_ns and observed_end_ns:
                    # Durée non fournie par le plan : Info réécrite à taille
                    # égale avec la fin réelle du dernier paquet.
                    fh.seek(payload_start + info_offset)
                    fh.write(_plan_info(plan, observed_end_ns))
                # Taille réelle du Segment patchée dans la VINT réservée.
                segment_end = fh.seek(0, 2)
                fh.seek(payload_start - 8)
                fh.write(encode_vint_size(segment_end - payload_start, length=8))
            _check_cancel("validation")
            _notify("validation", partial.stat().st_size)
            from .reader import MatroskaReader

            validation_reader = MatroskaReader(partial)
            validation_reader.segment()
            if len(validation_reader.tracks()) != len(plan.tracks):
                raise ValueError("Validation native : nombre de pistes incohérent")
            missing_media = [
                track.output_number for track in plan.tracks
                if track.source_track.track_type in (1, 2)
                and not packet_counts.get(track.output_number)
            ]
            if missing_media:
                raise ValueError(
                    "Validation native : aucun paquet écrit pour les pistes média "
                    + ", ".join(f"#{number}" for number in missing_media)
                )
            if external_validator is not None:
                external_validator(
                    partial,
                    MatroskaPacketValidation(
                        track_numbers=frozenset(packet_counts),
                        max_packet_timestamp_ns=validation_max_packet_timestamp_ns,
                        last_delta_by_track=dict(last_delta_by_track),
                    ),
                )
            _check_cancel("commit")
            _notify("commit", partial.stat().st_size)
            partial.replace(destination)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        return destination


__all__ = [
    "MatroskaWriteCancelled", "MatroskaWriteProgress", "MatroskaWriter",
    "build_attachments_element", "build_chapters_element",
    "build_tags_element", "build_track_statistics_tags_element",
    "merge_track_statistics_tags", "rewrite_tag_target_uids",
]
