"""Backend-neutral native Matroska mux plan."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from core.workflows.matroska_reader import MatroskaBlock, MatroskaTrack


def deterministic_uid(*parts: object) -> int:
    raw = "\0".join(str(part) for part in parts).encode("utf-8")
    value = int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")
    return value or 1


def deterministic_uid128(*parts: object) -> int:
    raw = "\0".join(str(part) for part in parts).encode("utf-8")
    value = int.from_bytes(hashlib.sha256(raw).digest()[:16], "big")
    return value or 1


def deterministic_source_identity(path: Path) -> str:
    """Stable, bounded-cost identity independent from volatile job entry IDs."""
    source = Path(path)
    digest = hashlib.sha256()
    stat = source.stat()
    digest.update(str(stat.st_size).encode("ascii"))
    with source.open("rb") as stream:
        digest.update(stream.read(1024 * 1024))
        if stat.st_size > 1024 * 1024:
            stream.seek(max(0, stat.st_size - 1024 * 1024))
            digest.update(stream.read(1024 * 1024))
    return digest.hexdigest()


@dataclass(frozen=True)
class MatroskaMuxTrack:
    source: Path
    source_track: MatroskaTrack
    output_number: int
    output_uid: int
    language: str = "und"
    language_bcp47: str = ""
    name: str = ""
    flag_enabled: bool = True
    flag_default: bool = False
    flag_forced: bool = False
    flag_hearing_impaired: bool = False
    flag_visual_impaired: bool = False
    flag_original: bool = False
    flag_commentary: bool = False


@dataclass(frozen=True)
class MatroskaMuxPacket:
    output_track_number: int
    block: MatroskaBlock


@dataclass(frozen=True)
class MatroskaMuxPlan:
    output: Path
    tracks: tuple[MatroskaMuxTrack, ...]
    packets: tuple[MatroskaMuxPacket, ...]
    timestamp_scale_ns: int = 1_000_000
    duration_ms: int = 0
    duration_ns: int = 0
    segment_uid: int = 0
    opaque_top_level: tuple[bytes, ...] = field(default_factory=tuple)
    muxing_app: str = "Muxiveo"
    writing_app: str = "Muxiveo"
    title: str = ""

    def __post_init__(self) -> None:
        if not self.tracks:
            raise ValueError("Plan Matroska sans piste")
        numbers = [track.output_number for track in self.tracks]
        if len(numbers) != len(set(numbers)) or any(number <= 0 for number in numbers):
            raise ValueError("Numéros de piste de sortie invalides ou dupliqués")
        if self.segment_uid == 0:
            track_identity = tuple((track.output_uid, track.output_number) for track in self.tracks)
            object.__setattr__(self, "segment_uid", deterministic_uid128("segment", track_identity, self.title))


__all__ = [
    "MatroskaMuxPacket", "MatroskaMuxPlan", "MatroskaMuxTrack",
    "deterministic_source_identity", "deterministic_uid", "deterministic_uid128",
]
