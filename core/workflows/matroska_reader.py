"""Streaming-safe primitives for inspecting Matroska/EBML documents.

This module is intentionally codec agnostic: it exposes element boundaries and
raw payloads so the native remux planner can preserve packet bytes verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator


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
            yield from iter_children(fh, segment, file_size=size)

    def payload(self, element: EbmlElement) -> bytes:
        if element.size is None:
            raise ValueError("Un élément de taille inconnue ne peut pas être lu brut")
        with self.path.open("rb") as fh:
            fh.seek(element.payload_offset)
            return _read_exact(fh, element.size)

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
                out.append(MatroskaTrack(
                    number=uint(self.TRACK_NUMBER_ID), uid=uint(self.TRACK_UID_ID),
                    track_type=uint(self.TRACK_TYPE_ID), codec_id=text(self.CODEC_ID),
                    codec_private=fields.get(self.CODEC_PRIVATE, b""),
                    language_bcp47=text(self.LANGUAGE_BCP47), language=text(self.LANGUAGE) or "und",
                    name=text(self.NAME), raw_entry=self.payload(entry),
                ))
        return out

    @staticmethod
    def _decode_block(raw: bytes, cluster_timestamp: int, **metadata: object) -> tuple["MatroskaBlock", ...]:
        track_no, length = _read_vint_value(raw)
        if len(raw) < length + 3:
            raise ValueError("Block Matroska tronqué")
        relative = int.from_bytes(raw[length:length + 2], "big", signed=True)
        flags = raw[length + 2]
        frames = _split_laces(raw[length + 3:], flags)
        return tuple(MatroskaBlock(
            track_number=track_no, timestamp_ms=cluster_timestamp + relative,
            flags=flags, payload=frame, lace_index=index, lace_count=len(frames),
            duration_ms=metadata.get("duration_ms"),
            references=tuple(metadata.get("references", ())),
            discard_padding_ns=int(metadata.get("discard_padding_ns", 0)),
            codec_state=bytes(metadata.get("codec_state", b"")),
            block_additions=bytes(metadata.get("block_additions", b"")),
        ) for index, frame in enumerate(frames))

    def blocks(self) -> Iterator["MatroskaBlock"]:
        """Yield SimpleBlock and BlockGroup frames, including all lacing modes."""
        size = self.path.stat().st_size
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
                        yield from self._decode_block(self.payload(child), timestamp)
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
                        yield from self._decode_block(
                            raw_blocks[0], timestamp,
                            duration_ms=uint(self.BLOCK_DURATION_ID) or None,
                            references=sint_values(self.REFERENCE_BLOCK_ID),
                            discard_padding_ns=(sint_values(self.DISCARD_PADDING_ID) or (0,))[0],
                            codec_state=(values.get(self.CODEC_STATE_ID) or [b""])[0],
                            block_additions=(values.get(self.BLOCK_ADDITIONS_ID) or [b""])[0],
                        )

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


__all__ = ["EbmlElement", "MatroskaBlock", "MatroskaReader", "MatroskaTrack", "iter_children", "read_element"]
