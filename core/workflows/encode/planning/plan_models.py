from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, TypeVar

from core.workflows.encode.models import QualityMode
from core.workflows.remux_models import RemuxConfig

_TKey = TypeVar("_TKey")
_TValue = TypeVar("_TValue")


MapKey = tuple[Path, int, str]
TrackMapping = tuple[MapKey, Path | str, int]


@dataclass(frozen=True)
class SourceLayout:
    sources: tuple[Path, ...]
    source_idx: dict[Path, int]


@dataclass(frozen=True)
class ResolvedSubtitleTracks:
    tracks: tuple[tuple[Path, int], ...]
    complete: bool


@dataclass(frozen=True)
class PreparedContainerMetadataInputs:
    next_input_index: int
    chapter_input_index: int | None
    tag_input_index: int | None


@dataclass(frozen=True)
class MaterializedContainerMetadataPlan:
    next_input_index: int
    chapter_input_index: int | None
    tag_input_index: int | None
    input_args: tuple[str, ...]


@dataclass(frozen=True)
class ContainerMetadataPlan:
    tag_source: Path | None
    tag_overrides_defined: bool
    chapter_overrides_defined: bool
    chapter_overrides_nonempty: bool
    has_track_meta_edits: bool
    global_tags: Mapping[str, str]


@dataclass(frozen=True)
class PlannedVideoTrack:
    source: Path
    stream_index: int
    codec: str
    quality_mode: QualityMode
    inject_hdr_meta: bool
    has_transform: bool
    tonemap_to_sdr: bool
    copy_dv: bool
    copy_hdr10plus: bool
    master_display: str
    max_cll: str
    static_hdr_analysis_request: str = ""


@dataclass(frozen=True)
class SyncMappedTrackPlan:
    source_file_index: int
    stream_index: int
    track_type: str

    @property
    def track(self) -> "SyncMappedTrackPlan":
        return self


@dataclass(frozen=True)
class SyncAnalysisPlan:
    enabled: bool
    mapped_tracks: tuple[SyncMappedTrackPlan, ...]
    offset_requires_file_fallback: bool
    needs_subtitle_prescan: bool
    strict_interleave_without_prescan: bool
    allow_live_sync: bool
    probe_remux_config: RemuxConfig | None


@dataclass(frozen=True)
class ResolvedTrackAssembly:
    track_mappings: tuple[TrackMapping, ...]
    video_map: tuple[int, int]


@dataclass(frozen=True)
class EncodeCommandSelection:
    commands: tuple[tuple[str, ...], ...]
    preview_index: int
    is_multi_video: bool
    is_two_pass: bool

    @property
    def preview_command(self) -> tuple[str, ...]:
        if not self.commands:
            return ()
        index = min(max(0, int(self.preview_index)), len(self.commands) - 1)
        return self.commands[index]


@dataclass(frozen=True)
class EncodePlan:
    all_sources: tuple[Path, ...]
    source_idx: Mapping[Path, int]
    offset_lookup: Mapping[tuple[str, Path, int], int]
    resolved_subtitle_tracks: tuple[tuple[Path, int], ...]
    subtitles_resolved: bool
    video_source: Path
    video_stream: int
    video_key: MapKey
    video_input_idx: int
    video_default_map: tuple[int, int]
    video_tracks: tuple[PlannedVideoTrack, ...]
    sync_analysis: SyncAnalysisPlan
    container_metadata: ContainerMetadataPlan

    @staticmethod
    def freeze_mapping(mapping: dict[_TKey, _TValue]) -> Mapping[_TKey, _TValue]:
        return MappingProxyType(dict(mapping))
