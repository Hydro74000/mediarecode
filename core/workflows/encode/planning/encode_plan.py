from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from core.workflows.encode.models import EncodeConfig, VideoEncodeSettings

from .metadata_plan import build_container_metadata_plan
from .offsets import track_time_offset_lookup
from .plan_models import EncodePlan, PlannedVideoTrack
from .sources import resolve_source_layout
from .subtitles import resolve_subtitle_tracks_for_encode
from .sync_plan import build_sync_analysis_plan


def build_planned_video_tracks(
    config: EncodeConfig,
    *,
    video_tracks,
    video_source_from_settings,
) -> tuple[PlannedVideoTrack, ...]:
    planned: list[PlannedVideoTrack] = []
    for video in video_tracks(config):
        settings = video if isinstance(video, VideoEncodeSettings) else VideoEncodeSettings()
        planned.append(
            PlannedVideoTrack(
                source=Path(video_source_from_settings(config, settings)),
                stream_index=int(getattr(settings, "stream_index", 0) or 0),
                codec=str(settings.codec),
                quality_mode=settings.quality_mode,
                inject_hdr_meta=bool(settings.inject_hdr_meta),
                has_transform=bool(settings.has_video_transform()),
                tonemap_to_sdr=bool(settings.tonemap_to_sdr),
                copy_dv=bool(settings.copy_dv),
                copy_hdr10plus=bool(settings.copy_hdr10plus),
                master_display=str(settings.master_display or ""),
                max_cll=str(settings.max_cll or ""),
                static_hdr_analysis_request=str(
                    settings.static_hdr_metadata_analysis_request or ""
                ),
            )
        )
    return tuple(planned)


def build_encode_plan(
    config: EncodeConfig,
    *,
    probe_subtitle_indices: Callable[[Path, str], list[int] | None] | None = None,
    resolve_subtitle_tracks=None,
    resolve_global_tags,
    video_tracks,
    video_source_from_settings,
    video_source_path,
    video_stream_index,
    video_map_key,
) -> EncodePlan:
    source_layout = resolve_source_layout(config)
    all_sources = list(source_layout.sources)
    source_idx = dict(source_layout.source_idx)
    offset_lookup = track_time_offset_lookup(config)
    if resolve_subtitle_tracks is not None:
        subtitle_tracks, subtitles_resolved = resolve_subtitle_tracks(config, all_sources)
        resolved_subtitles = tuple((Path(source), int(stream_index)) for source, stream_index in subtitle_tracks)
    else:
        if probe_subtitle_indices is None:
            raise ValueError("probe_subtitle_indices callback is required")
        resolved = resolve_subtitle_tracks_for_encode(
            config,
            all_sources,
            probe_indices=probe_subtitle_indices,
        )
        resolved_subtitles = tuple(resolved.tracks)
        subtitles_resolved = bool(resolved.complete)
    source = Path(video_source_path(config))
    stream_index = int(video_stream_index(config))
    input_idx = source_idx.get(source, source_idx.get(config.source, 0))
    container_metadata = build_container_metadata_plan(
        config,
        resolve_global_tags=resolve_global_tags,
    )
    planned_video_tracks = build_planned_video_tracks(
        config,
        video_tracks=video_tracks,
        video_source_from_settings=video_source_from_settings,
    )
    sync_analysis = build_sync_analysis_plan(
        config,
        all_sources,
        source_idx,
        list(resolved_subtitles),
        subtitles_resolved=bool(subtitles_resolved),
        offset_lookup=offset_lookup,
    )

    return EncodePlan(
        all_sources=tuple(all_sources),
        source_idx=EncodePlan.freeze_mapping(source_idx),
        offset_lookup=EncodePlan.freeze_mapping(offset_lookup),
        resolved_subtitle_tracks=tuple(resolved_subtitles),
        subtitles_resolved=bool(subtitles_resolved),
        video_source=source,
        video_stream=stream_index,
        video_key=video_map_key(config),
        video_input_idx=int(input_idx),
        video_default_map=(int(input_idx), int(stream_index)),
        video_tracks=planned_video_tracks,
        sync_analysis=sync_analysis,
        container_metadata=container_metadata,
    )
