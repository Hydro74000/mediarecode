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
    segment_uid: int = 0
    opaque_top_level: tuple[bytes, ...] = field(default_factory=tuple)
    muxing_app: str = "Muxiveo"
    writing_app: str = "Muxiveo"

    def __post_init__(self) -> None:
        if not self.tracks:
            raise ValueError("Plan Matroska sans piste")
        numbers = [track.output_number for track in self.tracks]
        if len(numbers) != len(set(numbers)) or any(number <= 0 for number in numbers):
            raise ValueError("Numéros de piste de sortie invalides ou dupliqués")
        if self.segment_uid == 0:
            object.__setattr__(self, "segment_uid", deterministic_uid(self.output, *numbers))


__all__ = ["MatroskaMuxPacket", "MatroskaMuxPlan", "MatroskaMuxTrack", "deterministic_uid"]
