from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from core.workflows.encode.models import EncodeConfig
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry

from .offsets import track_offset_ms as default_track_offset_ms
from .plan_models import SyncAnalysisPlan, SyncMappedTrackPlan


class _SyncMappedTrackLike(Protocol):
    @property
    def source_file_index(self) -> int:
        ...

    @property
    def stream_index(self) -> int:
        ...


def _track_type_of(mapped_track) -> str:
    direct = getattr(mapped_track, "track_type", None)
    if direct is not None:
        return str(direct)
    return str(getattr(getattr(mapped_track, "track", None), "track_type", ""))


def build_probe_remux_config(
    config: EncodeConfig,
    all_sources: list[Path],
    source_idx_local: dict[Path, int],
    resolved_subtitle_tracks: list[tuple[Path, int]],
) -> RemuxConfig:
    tracks_by_source: dict[int, list[TrackEntry]] = {i: [] for i in range(len(all_sources))}

    def _push_track(src_idx: int, stream_idx: int, track_type: str) -> None:
        bucket = tracks_by_source.setdefault(src_idx, [])
        if any(track.mkv_tid == stream_idx and track.track_type == track_type for track in bucket):
            return
        bucket.append(
            TrackEntry(
                mkv_tid=stream_idx,
                track_type=track_type,
                codec="COPY",
                display_info="",
                language="",
                title="",
            )
        )

    _push_track(0, 0, "video")
    for audio in config.audio_tracks:
        source = Path(audio.source_path or config.source)
        audio_src_idx = source_idx_local.get(source, 0)
        _push_track(audio_src_idx, int(audio.stream_index), "audio")
    for source, stream_idx in resolved_subtitle_tracks:
        subtitle_src_idx = source_idx_local.get(source)
        if subtitle_src_idx is None:
            continue
        _push_track(subtitle_src_idx, int(stream_idx), "subtitle")

    sources: list[SourceInput] = []
    track_order: list[tuple[int, int] | tuple[int, int, str]] = []
    for index, source in enumerate(all_sources):
        source_tracks = tracks_by_source.get(index, [])
        sources.append(SourceInput(path=source, file_index=index, tracks=source_tracks))
        for track in source_tracks:
            track_order.append((index, int(track.mkv_tid)))

    return RemuxConfig(
        sources=sources,
        output=config.output,
        track_order=track_order,
        keep_chapters=config.keep_chapters,
    )


def build_sync_mapped_tracks(
    config: EncodeConfig,
    source_idx_local: dict[Path, int],
    resolved_subtitle_tracks: list[tuple[Path, int]],
) -> list[SyncMappedTrackPlan]:
    mapped: list[SyncMappedTrackPlan] = [
        SyncMappedTrackPlan(
            source_file_index=0,
            stream_index=0,
            track_type="video",
        )
    ]
    for audio in config.audio_tracks:
        source = Path(audio.source_path or config.source)
        audio_src_idx = source_idx_local.get(source, 0)
        mapped.append(
            SyncMappedTrackPlan(
                source_file_index=audio_src_idx,
                stream_index=int(audio.stream_index),
                track_type="audio",
            )
        )
    for source, stream_idx in resolved_subtitle_tracks:
        subtitle_src_idx = source_idx_local.get(source)
        if subtitle_src_idx is None:
            continue
        mapped.append(
            SyncMappedTrackPlan(
                source_file_index=subtitle_src_idx,
                stream_index=int(stream_idx),
                track_type="subtitle",
            )
        )
    return mapped


def needs_strict_interleave_for_encode(
    mapped_tracks: Sequence[_SyncMappedTrackLike],
) -> bool:
    if len({int(track.source_file_index) for track in mapped_tracks}) < 2:
        return False
    if not any(_track_type_of(track) == "subtitle" for track in mapped_tracks):
        return False
    primary_video = next((track for track in mapped_tracks if _track_type_of(track) == "video"), None)
    if primary_video is None:
        return False
    primary_source = int(primary_video.source_file_index)
    return any(
        _track_type_of(track) == "audio"
        and int(track.source_file_index) != primary_source
        for track in mapped_tracks
    )


def requires_file_sync_fallback_for_offsets(
    config: EncodeConfig,
    mapped_tracks: Sequence[_SyncMappedTrackLike],
    source_by_index: dict[int, Path],
    *,
    track_offset_ms=None,
    offset_lookup: dict[tuple[str, Path, int], int],
) -> bool:
    primary_video = next((track for track in mapped_tracks if _track_type_of(track) == "video"), None)
    if primary_video is None:
        return False

    for mapped_track in mapped_tracks:
        track_type = _track_type_of(mapped_track)
        if track_type not in {"audio", "subtitle"}:
            continue
        if mapped_track.source_file_index == primary_video.source_file_index:
            continue
        source_path = source_by_index.get(mapped_track.source_file_index)
        if source_path is None:
            continue
        resolver = track_offset_ms or default_track_offset_ms
        offset_ms = resolver(
            offset_lookup,
            track_type=track_type,
            source_path=source_path,
            stream_index=mapped_track.stream_index,
        )
        if offset_ms != 0:
            return True
    return False


def build_sync_analysis_plan(
    config: EncodeConfig,
    all_sources: list[Path],
    source_idx_local: dict[Path, int],
    resolved_subtitle_tracks: list[tuple[Path, int]],
    *,
    subtitles_resolved: bool,
    offset_lookup: dict[tuple[str, Path, int], int],
) -> SyncAnalysisPlan:
    if len(source_idx_local) < 2:
        return SyncAnalysisPlan(
            enabled=False,
            mapped_tracks=(),
            offset_requires_file_fallback=False,
            needs_subtitle_prescan=False,
            strict_interleave_without_prescan=False,
            allow_live_sync=True,
            probe_remux_config=None,
        )

    mapped_tracks = tuple(
        build_sync_mapped_tracks(
            config,
            source_idx_local,
            resolved_subtitle_tracks,
        )
    )
    path_by_source_idx = {idx: path for path, idx in source_idx_local.items()}
    offset_requires_sync = requires_file_sync_fallback_for_offsets(
        config,
        mapped_tracks,
        path_by_source_idx,
        offset_lookup=offset_lookup,
    )
    if offset_requires_sync:
        return SyncAnalysisPlan(
            enabled=True,
            mapped_tracks=mapped_tracks,
            offset_requires_file_fallback=True,
            needs_subtitle_prescan=False,
            strict_interleave_without_prescan=True,
            allow_live_sync=False,
            probe_remux_config=None,
        )

    if subtitles_resolved:
        return SyncAnalysisPlan(
            enabled=True,
            mapped_tracks=mapped_tracks,
            offset_requires_file_fallback=False,
            needs_subtitle_prescan=True,
            strict_interleave_without_prescan=False,
            allow_live_sync=True,
            probe_remux_config=build_probe_remux_config(
                config,
                all_sources,
                source_idx_local,
                resolved_subtitle_tracks,
            ),
        )

    strict_without_prescan = bool(config.copy_subtitles and needs_strict_interleave_for_encode(mapped_tracks))
    return SyncAnalysisPlan(
        enabled=True,
        mapped_tracks=mapped_tracks,
        offset_requires_file_fallback=False,
        needs_subtitle_prescan=False,
        strict_interleave_without_prescan=strict_without_prescan,
        allow_live_sync=True,
        probe_remux_config=None,
    )
