from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol


class TrackType(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"
    SUBTITLE = "subtitle"
    ATTACHMENT = "attachment"


@dataclass(frozen=True)
class TrackRef:
    track_type: str
    source_path: Path
    stream_index: int
    track_entry_id: str | None = None


@dataclass
class TrackOffset:
    """Décalage temporel appliqué à une piste d'entrée (en millisecondes)."""

    track_type: str
    source_path: Path
    stream_index: int
    offset_ms: int = 0
    sync_rewrite_mode: str = ""


@dataclass
class TrackMetaPatch:
    """Édition de métadonnées/dispositions d'une piste de sortie."""

    track_order: int
    language: str = ""
    title: str | None = None
    #: FlagEnabled Matroska — aucune disposition FFmpeg ne lui correspond,
    #: il est appliqué par patch EBML après le mux.
    flag_enabled: bool | None = None
    flag_default: bool | None = None
    flag_forced: bool | None = None
    flag_hearing_impaired: bool | None = None
    flag_visual_impaired: bool | None = None
    flag_original: bool | None = None
    flag_commentary: bool | None = None


TrackTimeOffset = TrackOffset
TrackMetaEdit = TrackMetaPatch


class TrackTypeCarrier(Protocol):
    @property
    def track_type(self) -> str:
        ...


class TimelineMappedTrack(Protocol):
    @property
    def source_file_index(self) -> int:
        ...

    @property
    def track(self) -> TrackTypeCarrier:
        ...
