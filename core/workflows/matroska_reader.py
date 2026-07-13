"""Streaming-safe primitives for inspecting Matroska/EBML documents.

This module is intentionally codec agnostic: it exposes element boundaries and
raw payloads so the native remux planner can preserve packet bytes verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
import struct
from typing import BinaryIO, Iterator


_CONTENT_ENCODINGS_ID = bytes.fromhex("6d80")
_CONTENT_ENCODING_ID = bytes.fromhex("6240")
_CONTENT_COMPRESSION_ID = bytes.fromhex("5034")
_CONTENT_ENCRYPTION_ID = bytes.fromhex("5035")


@dataclass(frozen=True)
class EbmlElement:
    element_id: bytes
    offset: int
    payload_offset: int
    size: int | None
    header_size: int

    @property
    def end(self) -> int | None:
        return None if self.size is None else self.payload_offset + self.size


def _vint_length(first: int) -> int:
    for length in range(1, 9):
        if first & (0x80 >> (length - 1)):
            return length
    raise ValueError("VINT EBML invalide")


def _read_exact(fh: BinaryIO, count: int) -> bytes:
    value = fh.read(count)
    if len(value) != count:
        raise ValueError("EBML tronqué")
    return value


def _read_vint_value(data: bytes, offset: int = 0) -> tuple[int, int]:
    if offset >= len(data):
        raise ValueError("VINT absent")
    length = _vint_length(data[offset])
    if offset + length > len(data):
        raise ValueError("VINT tronqué")
    value = data[offset] & (0xFF >> length)
    for byte in data[offset + 1:offset + length]:
        value = (value << 8) | byte
    return value, length


def _split_laces(payload: bytes, flags: int) -> tuple[bytes, ...]:
    mode = (flags >> 1) & 0x03
    if mode == 0:
        return (payload,)
    if not payload:
        raise ValueError("En-tête de lacing absent")
    count = payload[0] + 1
    cursor = 1
    sizes: list[int] = []
    if mode == 1:  # Xiph
        for _ in range(count - 1):
            size = 0
            while True:
                if cursor >= len(payload):
                    raise ValueError("Lacing Xiph tronqué")
                byte = payload[cursor]
                cursor += 1
                size += byte
                if byte != 255:
                    break
            sizes.append(size)
    elif mode == 2:  # fixed
        remaining = len(payload) - cursor
        if remaining % count:
            raise ValueError("Lacing fixed de taille non divisible")
        sizes = [remaining // count] * (count - 1)
    else:  # EBML
        first, length = _read_vint_value(payload, cursor)
        cursor += length
        sizes.append(first)
        for _ in range(count - 2):
            encoded, length = _read_vint_value(payload, cursor)
            cursor += length
            bias = (1 << (7 * length - 1)) - 1
            sizes.append(sizes[-1] + encoded - bias)
            if sizes[-1] < 0:
                raise ValueError("Lacing EBML avec taille négative")
    last = len(payload) - cursor - sum(sizes)
    if last < 0:
        raise ValueError("Tailles de lacing hors payload")
    sizes.append(last)
    frames: list[bytes] = []
    for size in sizes:
        frames.append(payload[cursor:cursor + size])
        cursor += size
    if cursor != len(payload):
        raise ValueError("Payload de lacing incohérent")
    return tuple(frames)


def read_element(fh: BinaryIO, *, limit: int | None = None) -> EbmlElement | None:
    offset = fh.tell()
    if limit is not None and offset >= limit:
        return None
    first = fh.read(1)
    if not first:
        return None
    id_len = _vint_length(first[0])
    element_id = first + _read_exact(fh, id_len - 1)
    first_size = _read_exact(fh, 1)[0]
    size_len = _vint_length(first_size)
    raw_size = bytes([first_size]) + _read_exact(fh, size_len - 1)
    value = raw_size[0] & (0xFF >> size_len)
    for byte in raw_size[1:]:
        value = (value << 8) | byte
    unknown = value == (1 << (7 * size_len)) - 1
    payload_offset = fh.tell()
    size = None if unknown else value
    if size is not None and limit is not None and payload_offset + size > limit:
        raise ValueError("Élément EBML hors limites")
    return EbmlElement(element_id, offset, payload_offset, size, id_len + size_len)


def iter_children(fh: BinaryIO, parent: EbmlElement, *, file_size: int) -> Iterator[EbmlElement]:
    end = parent.end if parent.end is not None else file_size
    while fh.tell() < end:
        child = read_element(fh, limit=end)
        if child is None:
            return
        yield child
        child_end = child.end
        if child_end is None:
            # Unknown-size children are containers extending to their parent.
            return
        fh.seek(child_end)


def payload_children(payload: bytes) -> Iterator[tuple[bytes, bytes]]:
    stream = BytesIO(payload)
    while stream.tell() < len(payload):
        child = read_element(stream, limit=len(payload))
        if child is None or child.size is None:
            raise ValueError("Conteneur EBML invalide")
        stream.seek(child.payload_offset)
        yield child.element_id, _read_exact(stream, child.size)
        stream.seek(child.end)


class MatroskaReader:
    """Read top-level Matroska elements without loading media payloads."""

    SEGMENT_ID = bytes.fromhex("18538067")
    TRACKS_ID = bytes.fromhex("1654ae6b")
    TRACK_ENTRY_ID = bytes.fromhex("ae")
    TRACK_NUMBER_ID = bytes.fromhex("d7")
    TRACK_UID_ID = bytes.fromhex("73c5")
    TRACK_TYPE_ID = bytes.fromhex("83")
    CODEC_ID = bytes.fromhex("86")
    CODEC_PRIVATE = bytes.fromhex("63a2")
    LANGUAGE = bytes.fromhex("22b59c")
    LANGUAGE_BCP47 = bytes.fromhex("22b59d")
    NAME = bytes.fromhex("536e")
    VIDEO_ID = bytes.fromhex("e0")
    AUDIO_ID = bytes.fromhex("e1")
    COLOUR_ID = bytes.fromhex("55b0")
    MASTERING_METADATA_ID = bytes.fromhex("55d0")
    BLOCK_ADDITION_MAPPING_ID = bytes.fromhex("41e4")
    CLUSTER_ID = bytes.fromhex("1f43b675")
    TIMESTAMP_ID = bytes.fromhex("e7")
    SIMPLE_BLOCK_ID = bytes.fromhex("a3")
    BLOCK_GROUP_ID = bytes.fromhex("a0")
    BLOCK_ID = bytes.fromhex("a1")
    BLOCK_DURATION_ID = bytes.fromhex("9b")
    REFERENCE_BLOCK_ID = bytes.fromhex("fb")
    DISCARD_PADDING_ID = bytes.fromhex("75a2")
    CODEC_STATE_ID = bytes.fromhex("a4")
    BLOCK_ADDITIONS_ID = bytes.fromhex("75a1")
    INFO_ID = bytes.fromhex("1549a966")
    MUXING_APP_ID = bytes.fromhex("4d80")
    WRITING_APP_ID = bytes.fromhex("5741")
    TITLE_ID = bytes.fromhex("7ba9")
    TIMESTAMP_SCALE_ID = bytes.fromhex("2ad7b1")
    ATTACHMENTS_ID = bytes.fromhex("1941a469")
    ATTACHED_FILE_ID = bytes.fromhex("61a7")
    FILE_DESCRIPTION_ID = bytes.fromhex("467e")
    FILE_NAME_ID = bytes.fromhex("466e")
    FILE_MEDIA_TYPE_ID = bytes.fromhex("4660")
    FILE_DATA_ID = bytes.fromhex("465c")
    FILE_UID_ID = bytes.fromhex("46ae")
    CHAPTERS_ID = bytes.fromhex("1043a770")
    EDITION_ENTRY_ID = bytes.fromhex("45b9")
    EDITION_UID_ID = bytes.fromhex("45bc")
    CHAPTER_ATOM_ID = bytes.fromhex("b6")
    CHAPTER_UID_ID = bytes.fromhex("73c4")
    CHAPTER_TIME_START_ID = bytes.fromhex("91")
    CHAPTER_TIME_END_ID = bytes.fromhex("92")
    CHAPTER_DISPLAY_ID = bytes.fromhex("80")
    CHAP_STRING_ID = bytes.fromhex("85")
    CHAP_LANGUAGE_ID = bytes.fromhex("437c")
    TAGS_ID = bytes.fromhex("1254c367")
    TAG_ID = bytes.fromhex("7373")
    TARGETS_ID = bytes.fromhex("63c0")
    SIMPLE_TAG_ID = bytes.fromhex("67c8")
    TAG_NAME_ID = bytes.fromhex("45a3")
    TAG_STRING_ID = bytes.fromhex("4487")
    LEVEL1_IDS = frozenset({
        bytes.fromhex(value) for value in (
            "114d9b74", "1549a966", "1654ae6b", "1f43b675",
            "1c53bb6b", "1941a469", "1043a770", "1254c367",
        )
    })

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def segment(self) -> EbmlElement:
        with self.path.open("rb") as fh:
            size = self.path.stat().st_size
            while element := read_element(fh, limit=size):
                if element.element_id == self.SEGMENT_ID:
                    return element
                if element.end is None:
                    break
                fh.seek(element.end)
        raise ValueError("Segment Matroska introuvable")

    def top_level(self) -> Iterator[EbmlElement]:
        size = self.path.stat().st_size
        with self.path.open("rb") as fh:
            segment = self.segment()
            fh.seek(segment.payload_offset)
            segment_end = segment.end if segment.end is not None else size
            while fh.tell() < segment_end:
                item = read_element(fh, limit=segment_end)
                if item is None:
                    return
                if item.size is not None:
                    yield item
                    fh.seek(item.end)
                    continue
                if item.element_id != self.CLUSTER_ID:
                    yield item
                    return
                # Unknown-size Clusters end at the next level-1 element.
                cursor = item.payload_offset
                boundary = segment_end
                fh.seek(cursor)
                while fh.tell() < segment_end:
                    child_offset = fh.tell()
                    child = read_element(fh, limit=segment_end)
                    if child is None:
                        break
                    if child.element_id in self.LEVEL1_IDS:
                        boundary = child_offset
                        break
                    if child.end is None:
                        boundary = segment_end
                        break
                    fh.seek(child.end)
                yield EbmlElement(
                    item.element_id, item.offset, item.payload_offset,
                    boundary - item.payload_offset, item.header_size,
                )
                fh.seek(boundary)

    def payload(self, element: EbmlElement) -> bytes:
        if element.size is None:
            raise ValueError("Un élément de taille inconnue ne peut pas être lu brut")
        with self.path.open("rb") as fh:
            fh.seek(element.payload_offset)
            return _read_exact(fh, element.size)

    def raw_element(self, element: EbmlElement) -> bytes:
        if element.size is None:
            raise ValueError("Copie brute impossible pour une taille inconnue")
        with self.path.open("rb") as fh:
            fh.seek(element.offset)
            return _read_exact(fh, element.header_size + element.size)

    def raw_top_level(self, element_id: bytes) -> tuple[bytes, ...]:
        return tuple(self.raw_element(item) for item in self.top_level() if item.element_id == element_id)

    def attachments(self) -> list["MatroskaAttachment"]:
        root = next((item for item in self.top_level() if item.element_id == self.ATTACHMENTS_ID), None)
        if root is None:
            return []
        result: list[MatroskaAttachment] = []
        size = self.path.stat().st_size
        with self.path.open("rb") as fh:
            fh.seek(root.payload_offset)
            for attached in iter_children(fh, root, file_size=size):
                if attached.element_id != self.ATTACHED_FILE_ID:
                    continue
                values: dict[bytes, bytes] = {}
                fh.seek(attached.payload_offset)
                for child in iter_children(fh, attached, file_size=size):
                    if child.size is not None:
                        values[child.element_id] = self.payload(child)
                text = lambda key: values.get(key, b"").decode("utf-8", "replace").rstrip("\0")
                result.append(MatroskaAttachment(
                    uid=int.from_bytes(values.get(self.FILE_UID_ID, b"\0"), "big"),
                    name=text(self.FILE_NAME_ID), media_type=text(self.FILE_MEDIA_TYPE_ID),
                    description=text(self.FILE_DESCRIPTION_ID), data=values.get(self.FILE_DATA_ID, b""),
                ))
        return result

    def chapter_editions(self) -> tuple["MatroskaEdition", ...]:
        editions: list[MatroskaEdition] = []

        def parse_atom(payload: bytes) -> MatroskaChapter:
            uid = start = 0
            end: int | None = None
            displays: list[tuple[str, str]] = []
            children: list[MatroskaChapter] = []
            for element_id, value in payload_children(payload):
                if element_id == self.CHAPTER_UID_ID:
                    uid = int.from_bytes(value, "big")
                elif element_id == self.CHAPTER_TIME_START_ID:
                    start = int.from_bytes(value, "big")
                elif element_id == self.CHAPTER_TIME_END_ID:
                    end = int.from_bytes(value, "big")
                elif element_id == self.CHAPTER_DISPLAY_ID:
                    name = ""
                    language = "und"
                    for display_id, display_value in payload_children(value):
                        if display_id == self.CHAP_STRING_ID:
                            name = display_value.decode("utf-8", "replace").rstrip("\0")
                        elif display_id == self.CHAP_LANGUAGE_ID:
                            language = display_value.decode("ascii", "replace").rstrip("\0")
                    displays.append((name, language))
                elif element_id == self.CHAPTER_ATOM_ID:
                    children.append(parse_atom(value))
            return MatroskaChapter(uid, start, end, tuple(displays), tuple(children))

        for raw in self.raw_top_level(self.CHAPTERS_ID):
            for element_id, value in payload_children(self._raw_payload(raw)):
                if element_id != self.EDITION_ENTRY_ID:
                    continue
                uid = 0
                chapters: list[MatroskaChapter] = []
                for edition_id, edition_value in payload_children(value):
                    if edition_id == self.EDITION_UID_ID:
                        uid = int.from_bytes(edition_value, "big")
                    elif edition_id == self.CHAPTER_ATOM_ID:
                        chapters.append(parse_atom(edition_value))
                editions.append(MatroskaEdition(uid, tuple(chapters)))
        return tuple(editions)

    @staticmethod
    def _raw_payload(raw: bytes) -> bytes:
        stream = BytesIO(raw)
        element = read_element(stream, limit=len(raw))
        if element is None or element.size is None:
            raise ValueError("Élément EBML brut invalide")
        return raw[element.payload_offset:element.end]

    def tags(self) -> tuple["MatroskaTag", ...]:
        tags: list[MatroskaTag] = []
        for raw in self.raw_top_level(self.TAGS_ID):
            for element_id, tag_payload in payload_children(self._raw_payload(raw)):
                if element_id != self.TAG_ID:
                    continue
                targets: dict[str, int] = {}
                values: list[tuple[str, str]] = []
                for tag_id, value in payload_children(tag_payload):
                    if tag_id == self.TARGETS_ID:
                        targets.update({child_id.hex(): int.from_bytes(child_value, "big") for child_id, child_value in payload_children(value)})
                    elif tag_id == self.SIMPLE_TAG_ID:
                        name = text = ""
                        for simple_id, simple_value in payload_children(value):
                            if simple_id == self.TAG_NAME_ID:
                                name = simple_value.decode("utf-8", "replace").rstrip("\0")
                            elif simple_id == self.TAG_STRING_ID:
                                text = simple_value.decode("utf-8", "replace").rstrip("\0")
                        values.append((name, text))
                tags.append(MatroskaTag(targets, tuple(values)))
        return tuple(tags)

    def tracks(self) -> list["MatroskaTrack"]:
        """Return core TrackEntry metadata while retaining its raw EBML body."""
        tracks_element = next((item for item in self.top_level() if item.element_id == self.TRACKS_ID), None)
        if tracks_element is None:
            return []
        out: list[MatroskaTrack] = []
        size = self.path.stat().st_size
        with self.path.open("rb") as fh:
            fh.seek(tracks_element.payload_offset)
            for entry in iter_children(fh, tracks_element, file_size=size):
                if entry.element_id != self.TRACK_ENTRY_ID or entry.size is None:
                    continue
                fields: dict[bytes, bytes] = {}
                fh.seek(entry.payload_offset)
                for child in iter_children(fh, entry, file_size=size):
                    if child.size is not None and child.element_id in {
                        self.TRACK_NUMBER_ID, self.TRACK_UID_ID, self.TRACK_TYPE_ID,
                        self.CODEC_ID, self.CODEC_PRIVATE, self.LANGUAGE,
                        self.LANGUAGE_BCP47, self.NAME,
                    }:
                        fields[child.element_id] = self.payload(child)
                def uint(key: bytes, default: int = 0) -> int:
                    return int.from_bytes(fields.get(key, b""), "big") if fields.get(key) else default
                def text(key: bytes) -> str:
                    return fields.get(key, b"").decode("utf-8", "replace").rstrip("\0")
                nested = {child_id: value for child_id, value in payload_children(self.payload(entry))}
                video: dict[str, int | float] = {}
                audio: dict[str, int | float] = {}
                block_addition_mappings: list[dict[str, int | str | bytes]] = []

                def uint_values(payload: bytes, names: dict[bytes, str]) -> dict[str, int]:
                    return {
                        names[element_id]: int.from_bytes(value, "big")
                        for element_id, value in payload_children(payload)
                        if element_id in names
                    }

                def float_value(value: bytes) -> float:
                    if len(value) == 4:
                        return float(struct.unpack(">f", value)[0])
                    if len(value) == 8:
                        return float(struct.unpack(">d", value)[0])
                    raise ValueError("Float EBML de taille invalide")

                video_names = {
                    bytes.fromhex(key): name for key, name in {
                        "b0": "pixel_width", "ba": "pixel_height",
                        "54b0": "display_width", "54ba": "display_height",
                        "54b2": "display_unit", "54b3": "aspect_ratio_type",
                        "9a": "flag_interlaced", "9d": "field_order",
                        "53b8": "stereo_mode", "53c0": "alpha_mode",
                        "54aa": "pixel_crop_bottom", "54bb": "pixel_crop_top",
                        "54cc": "pixel_crop_left", "54dd": "pixel_crop_right",
                    }.items()
                }
                colour_names = {
                    bytes.fromhex(key): name for key, name in {
                        "55b1": "matrix_coefficients", "55b2": "bits_per_channel",
                        "55b3": "chroma_subsampling_horz", "55b4": "chroma_subsampling_vert",
                        "55b5": "cb_subsampling_horz", "55b6": "cb_subsampling_vert",
                        "55b7": "chroma_siting_horz", "55b8": "chroma_siting_vert",
                        "55b9": "range", "55ba": "transfer_characteristics",
                        "55bb": "primaries", "55bc": "max_cll", "55bd": "max_fall",
                    }.items()
                }
                mastering_names = {
                    bytes.fromhex(key): name for key, name in {
                        "55d1": "primary_r_x", "55d2": "primary_r_y",
                        "55d3": "primary_g_x", "55d4": "primary_g_y",
                        "55d5": "primary_b_x", "55d6": "primary_b_y",
                        "55d7": "white_point_x", "55d8": "white_point_y",
                        "55d9": "luminance_max", "55da": "luminance_min",
                    }.items()
                }
                if self.VIDEO_ID in nested:
                    video.update(uint_values(nested[self.VIDEO_ID], video_names))
                    video_children = dict(payload_children(nested[self.VIDEO_ID]))
                    colour_payload = video_children.get(self.COLOUR_ID)
                    if colour_payload is not None:
                        video.update(uint_values(colour_payload, colour_names))
                        colour_children = dict(payload_children(colour_payload))
                        mastering = colour_children.get(self.MASTERING_METADATA_ID)
                        if mastering is not None:
                            for element_id, value in payload_children(mastering):
                                if element_id in mastering_names:
                                    video[mastering_names[element_id]] = float_value(value)
                if self.AUDIO_ID in nested:
                    audio_names = {
                        bytes.fromhex("9f"): "channels", bytes.fromhex("6264"): "bit_depth",
                    }
                    audio.update(uint_values(nested[self.AUDIO_ID], audio_names))
                    for element_id, value in payload_children(nested[self.AUDIO_ID]):
                        if element_id == bytes.fromhex("b5"):
                            audio["sampling_frequency"] = float_value(value)
                        elif element_id == bytes.fromhex("78b5"):
                            audio["output_sampling_frequency"] = float_value(value)
                for element_id, value in payload_children(self.payload(entry)):
                    if element_id != self.BLOCK_ADDITION_MAPPING_ID:
                        continue
                    mapping: dict[str, int | str | bytes] = {}
                    for mapping_id, mapping_value in payload_children(value):
                        if mapping_id == bytes.fromhex("41f0"):
                            mapping["value"] = int.from_bytes(mapping_value, "big")
                        elif mapping_id == bytes.fromhex("41a4"):
                            mapping["name"] = mapping_value.decode("utf-8", "replace").rstrip("\0")
                        elif mapping_id == bytes.fromhex("41e7"):
                            mapping["type"] = int.from_bytes(mapping_value, "big")
                        elif mapping_id == bytes.fromhex("41ed"):
                            mapping["extra_data"] = mapping_value
                    block_addition_mappings.append(mapping)
                out.append(MatroskaTrack(
                    number=uint(self.TRACK_NUMBER_ID), uid=uint(self.TRACK_UID_ID),
                    track_type=uint(self.TRACK_TYPE_ID), codec_id=text(self.CODEC_ID),
                    codec_private=fields.get(self.CODEC_PRIVATE, b""),
                    language_bcp47=text(self.LANGUAGE_BCP47), language=text(self.LANGUAGE) or "und",
                    name=text(self.NAME), raw_entry=self.payload(entry),
                    video=video, audio=audio,
                    block_addition_mappings=tuple(block_addition_mappings),
                ))
        return out

    def content_encoding_capabilities(self) -> tuple[bool, bool]:
        """Return ``(uses_compression, uses_encryption)`` for all tracks."""
        compression = False
        encryption = False
        container_ids = {_CONTENT_ENCODINGS_ID, _CONTENT_ENCODING_ID}

        def inspect(payload: bytes) -> None:
            nonlocal compression, encryption
            from io import BytesIO

            stream = BytesIO(payload)
            while stream.tell() < len(payload):
                child = read_element(stream, limit=len(payload))
                if child is None or child.size is None:
                    raise ValueError("ContentEncodings EBML invalide")
                stream.seek(child.payload_offset)
                child_payload = _read_exact(stream, child.size)
                if child.element_id == _CONTENT_COMPRESSION_ID:
                    compression = True
                elif child.element_id == _CONTENT_ENCRYPTION_ID:
                    encryption = True
                elif child.element_id in container_ids:
                    inspect(child_payload)
                stream.seek(child.end)

        for track in self.tracks():
            stream_payload = track.raw_entry
            from io import BytesIO

            stream = BytesIO(stream_payload)
            while stream.tell() < len(stream_payload):
                child = read_element(stream, limit=len(stream_payload))
                if child is None or child.size is None:
                    raise ValueError("TrackEntry EBML invalide")
                if child.element_id == _CONTENT_ENCODINGS_ID:
                    stream.seek(child.payload_offset)
                    inspect(_read_exact(stream, child.size))
                stream.seek(child.end)
        return compression, encryption

    def segment_info_apps(self) -> tuple[str, str]:
        info = next((item for item in self.top_level() if item.element_id == self.INFO_ID), None)
        if info is None:
            return "", ""
        values: dict[bytes, str] = {}
        size = self.path.stat().st_size
        with self.path.open("rb") as fh:
            fh.seek(info.payload_offset)
            for child in iter_children(fh, info, file_size=size):
                if child.element_id in {self.MUXING_APP_ID, self.WRITING_APP_ID} and child.size is not None:
                    values[child.element_id] = self.payload(child).decode("utf-8", "replace").rstrip("\0")
        return values.get(self.MUXING_APP_ID, ""), values.get(self.WRITING_APP_ID, "")

    def segment_title(self) -> str:
        info = next((item for item in self.top_level() if item.element_id == self.INFO_ID), None)
        if info is None:
            return ""
        size = self.path.stat().st_size
        with self.path.open("rb") as fh:
            fh.seek(info.payload_offset)
            for child in iter_children(fh, info, file_size=size):
                if child.element_id == self.TITLE_ID and child.size is not None:
                    return self.payload(child).decode("utf-8", "replace").rstrip("\0")
        return ""

    def timestamp_scale_ns(self) -> int:
        info = next((item for item in self.top_level() if item.element_id == self.INFO_ID), None)
        if info is None:
            return 1_000_000
        size = self.path.stat().st_size
        with self.path.open("rb") as fh:
            fh.seek(info.payload_offset)
            for child in iter_children(fh, info, file_size=size):
                if child.element_id == self.TIMESTAMP_SCALE_ID and child.size is not None:
                    return int.from_bytes(self.payload(child), "big") or 1_000_000
        return 1_000_000

    @staticmethod
    def _decode_block(raw: bytes, cluster_timestamp: int, **metadata: object) -> tuple["MatroskaBlock", ...]:
        track_no, length = _read_vint_value(raw)
        if len(raw) < length + 3:
            raise ValueError("Block Matroska tronqué")
        relative = int.from_bytes(raw[length:length + 2], "big", signed=True)
        flags = raw[length + 2]
        encoded_frames_payload = raw[length + 3:]
        frames = _split_laces(encoded_frames_payload, flags)
        return tuple(MatroskaBlock(
            track_number=track_no, timestamp_ms=cluster_timestamp + relative,
            flags=flags, payload=frame, lace_index=index, lace_count=len(frames),
            duration_ms=metadata.get("duration_ms"),
            references=tuple(metadata.get("references", ())),
            discard_padding_ns=int(metadata.get("discard_padding_ns", 0)),
            codec_state=bytes(metadata.get("codec_state", b"")),
            block_additions=bytes(metadata.get("block_additions", b"")),
            duration_ns=metadata.get("duration_ns"),
            references_ns=tuple(metadata.get("references_ns", ())),
            lacing_mode=(flags >> 1) & 0x03,
            encoded_frames_payload=encoded_frames_payload,
        ) for index, frame in enumerate(frames))

    def blocks(self) -> Iterator["MatroskaBlock"]:
        """Yield SimpleBlock and BlockGroup frames, including all lacing modes."""
        size = self.path.stat().st_size
        scale_ns = self.timestamp_scale_ns()
        with self.path.open("rb") as fh:
            for cluster in self.top_level():
                if cluster.element_id != self.CLUSTER_ID:
                    continue
                timestamp = 0
                fh.seek(cluster.payload_offset)
                for child in iter_children(fh, cluster, file_size=size):
                    if child.element_id == self.TIMESTAMP_ID and child.size is not None:
                        timestamp = int.from_bytes(self.payload(child), "big")
                    elif child.element_id == self.SIMPLE_BLOCK_ID and child.size is not None:
                        decoded = self._decode_block(self.payload(child), timestamp)
                        for block in decoded:
                            yield block.__class__(**{
                                **block.__dict__,
                                "timestamp_ms": round(block.timestamp_ms * scale_ns / 1_000_000),
                                "timestamp_ns": block.timestamp_ms * scale_ns,
                            })
                    elif child.element_id == self.BLOCK_GROUP_ID and child.size is not None:
                        values: dict[bytes, list[bytes]] = {}
                        fh.seek(child.payload_offset)
                        for part in iter_children(fh, child, file_size=size):
                            if part.size is not None:
                                values.setdefault(part.element_id, []).append(self.payload(part))
                        raw_blocks = values.get(self.BLOCK_ID, [])
                        if not raw_blocks:
                            raise ValueError("BlockGroup sans Block")
                        uint = lambda key: int.from_bytes(values.get(key, [b""])[0], "big") if values.get(key) else 0
                        sint_values = lambda key: tuple(int.from_bytes(item, "big", signed=True) for item in values.get(key, []))
                        decoded = self._decode_block(
                            raw_blocks[0], timestamp,
                            duration_ms=(round(uint(self.BLOCK_DURATION_ID) * scale_ns / 1_000_000) if uint(self.BLOCK_DURATION_ID) else None),
                            duration_ns=(uint(self.BLOCK_DURATION_ID) * scale_ns if uint(self.BLOCK_DURATION_ID) else None),
                            references=tuple(round(value * scale_ns / 1_000_000) for value in sint_values(self.REFERENCE_BLOCK_ID)),
                            references_ns=tuple(value * scale_ns for value in sint_values(self.REFERENCE_BLOCK_ID)),
                            discard_padding_ns=(sint_values(self.DISCARD_PADDING_ID) or (0,))[0],
                            codec_state=(values.get(self.CODEC_STATE_ID) or [b""])[0],
                            block_additions=(values.get(self.BLOCK_ADDITIONS_ID) or [b""])[0],
                        )
                        for block in decoded:
                            yield block.__class__(**{
                                **block.__dict__,
                                "timestamp_ms": round(block.timestamp_ms * scale_ns / 1_000_000),
                                "timestamp_ns": block.timestamp_ms * scale_ns,
                            })

    def simple_blocks(self) -> Iterator["MatroskaBlock"]:
        """Compatibility alias for callers predating BlockGroup support."""
        yield from self.blocks()


@dataclass(frozen=True)
class MatroskaTrack:
    number: int
    uid: int
    track_type: int
    codec_id: str
    codec_private: bytes
    language_bcp47: str
    language: str
    name: str
    raw_entry: bytes
    video: dict[str, int | float] = field(default_factory=dict)
    audio: dict[str, int | float] = field(default_factory=dict)
    block_addition_mappings: tuple[dict[str, int | str | bytes], ...] = ()

@dataclass(frozen=True)
class MatroskaBlock:
    track_number: int
    timestamp_ms: int
    flags: int
    payload: bytes
    lace_index: int = 0
    lace_count: int = 1
    duration_ms: int | None = None
    references: tuple[int, ...] = ()
    discard_padding_ns: int = 0
    codec_state: bytes = b""
    block_additions: bytes = b""
    timestamp_ns: int | None = None
    duration_ns: int | None = None
    references_ns: tuple[int, ...] = ()
    lacing_mode: int = 0
    encoded_frames_payload: bytes = b""


@dataclass(frozen=True)
class MatroskaAttachment:
    uid: int
    name: str
    media_type: str
    description: str
    data: bytes


@dataclass(frozen=True)
class MatroskaChapter:
    uid: int
    start_ns: int
    end_ns: int | None
    displays: tuple[tuple[str, str], ...]
    children: tuple["MatroskaChapter", ...] = ()


@dataclass(frozen=True)
class MatroskaEdition:
    uid: int
    chapters: tuple[MatroskaChapter, ...]


@dataclass(frozen=True)
class MatroskaTag:
    targets: dict[str, int]
    values: tuple[tuple[str, str], ...]


__all__ = [
    "EbmlElement", "MatroskaAttachment", "MatroskaBlock", "MatroskaChapter",
    "MatroskaEdition", "MatroskaReader", "MatroskaTag", "MatroskaTrack",
    "iter_children", "payload_children", "read_element",
]
