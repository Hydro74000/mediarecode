from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.bluray import append_ffmpeg_input_args
from core.runner import TaskSignals
from core.workflows.common.timeline_sync import sync_cleanup_paths as _common_sync_cleanup_paths
from core.workflows.encode.domain import (
    EncodeCodecDomainCallbacks as _EncodeCodecDomainCallbacks,
    build_encoder_vf as _build_encoder_vf_domain,
    hardware_input_args as _hardware_input_args_domain,
)
from core.workflows.encode.models import EncodeConfig, VideoEncodeSettings
from core.workflows.encode.planning.offsets import build_offset_specs as _build_offset_specs_plan
from core.workflows.encode.planning.plan_models import (
    EncodePlan as _EncodePlan,
    MaterializedContainerMetadataPlan as _MaterializedContainerMetadataPlan,
    ResolvedTrackAssembly as _ResolvedTrackAssembly,
)
from core.workflows.encode.planning.track_assembly import build_track_input_paths as _build_track_input_paths_plan
from core.workflows.remux_timeline_sync import LiveSyncSession


@dataclass(frozen=True)
class EncodeCommandBuilderCallbacks:
    ffmpeg_bin: str
    ffmpeg_progress_args: Callable[[], list[str]]
    ffmpeg_thread_args: Callable[[int | None], list[str]]
    primary_video_settings: Callable[[EncodeConfig], VideoEncodeSettings]
    build_encode_plan: Callable[[EncodeConfig], _EncodePlan]
    size_to_bitrate_kbps: Callable[[EncodeConfig], int]
    codec_domain_callbacks: Callable[[], _EncodeCodecDomainCallbacks]
    materialize_container_metadata_inputs: Callable[..., _MaterializedContainerMetadataPlan]
    resolve_track_assembly_and_offset_remap: Callable[..., tuple[_ResolvedTrackAssembly, dict[tuple[Path, int, str], tuple[int, int]]]]
    append_primary_video_map_and_codec: Callable[..., None]
    append_common_streams_and_metadata: Callable[..., None]
    prepare_multisource_sync: Callable[..., tuple[dict[tuple[Path, int, str], tuple[int, int]], list[Path | str], LiveSyncSession | None, bool]]
    append_sync_inputs: Callable[[list[str], list[Path | str]], None]
    append_offset_aux_inputs: Callable[..., tuple[int, dict[tuple[Path, int, str], tuple[int, int]]]]
    video_track_mapping: Callable[..., tuple[tuple[Path, int, str], Path | str, int]]


def build_single_pass(
    callbacks: EncodeCommandBuilderCallbacks,
    config: EncodeConfig,
    *,
    chapter_materialize_dir: Path | None = None,
    plan: _EncodePlan | None = None,
) -> list[str]:
    plan = plan or callbacks.build_encode_plan(config)
    video = callbacks.primary_video_settings(config)
    all_sources = list(plan.all_sources)
    source_idx = dict(plan.source_idx)

    cmd: list[str] = [callbacks.ffmpeg_bin, "-hide_banner", "-y"]
    cmd.extend(callbacks.ffmpeg_progress_args())
    cmd.extend(_hardware_input_args_domain(video, callbacks=callbacks.codec_domain_callbacks()))
    for src in all_sources:
        append_ffmpeg_input_args(cmd, src)
    metadata_inputs = callbacks.materialize_container_metadata_inputs(
        config,
        source_idx=source_idx,
        next_input_index=len(all_sources),
        plan=plan,
        chapter_materialize_dir=chapter_materialize_dir,
        chapter_probe_source=config.source,
    )
    cmd.extend(metadata_inputs.input_args)

    vf = _build_encoder_vf_domain(video, callbacks=callbacks.codec_domain_callbacks())
    if vf:
        cmd.extend(["-vf", vf])

    cmd.extend(callbacks.ffmpeg_thread_args(None))
    track_assembly, offset_remap = callbacks.resolve_track_assembly_and_offset_remap(
        cmd=cmd,
        config=config,
        plan=plan,
        source_idx=source_idx,
        track_input_paths=_build_track_input_paths_plan(all_sources=all_sources),
        start_input_index=metadata_inputs.next_input_index,
    )

    callbacks.append_primary_video_map_and_codec(
        cmd,
        plan=plan,
        video_map=track_assembly.video_map,
        offset_remap=offset_remap,
        video=video,
    )
    callbacks.append_common_streams_and_metadata(
        cmd,
        config=config,
        source_idx=source_idx,
        all_sources_count=len(all_sources),
        plan=plan,
        metadata_inputs=metadata_inputs,
        offset_remap=offset_remap,
    )
    cmd.append(str(config.output))
    return cmd


def build_two_pass(
    callbacks: EncodeCommandBuilderCallbacks,
    config: EncodeConfig,
    *,
    chapter_materialize_dir: Path | None = None,
    plan: _EncodePlan | None = None,
) -> list[list[str]]:
    video = callbacks.primary_video_settings(config)
    bitrate = callbacks.size_to_bitrate_kbps(config)
    vf = _build_encoder_vf_domain(video, callbacks=callbacks.codec_domain_callbacks())
    plan = plan or callbacks.build_encode_plan(config)
    all_sources = list(plan.all_sources)
    source_idx = dict(plan.source_idx)

    def _base() -> list[str]:
        c = [callbacks.ffmpeg_bin, "-hide_banner", "-y"]
        c.extend(callbacks.ffmpeg_progress_args())
        c.extend(_hardware_input_args_domain(video, callbacks=callbacks.codec_domain_callbacks()))
        for src in all_sources:
            append_ffmpeg_input_args(c, src)
        if vf:
            c.extend(["-vf", vf])
        c.extend(callbacks.ffmpeg_thread_args(None))
        return c

    pass1 = _base()
    _next1, pass1_offset_remap = callbacks.append_offset_aux_inputs(
        pass1,
        _build_offset_specs_plan(
            config,
            track_mappings=[callbacks.video_track_mapping(config, all_sources[plan.video_input_idx])],
            offset_lookup=dict(plan.offset_lookup),
        ),
        start_input_index=len(all_sources),
    )
    _ = _next1
    callbacks.append_primary_video_map_and_codec(
        pass1,
        plan=plan,
        video_map=plan.video_default_map,
        offset_remap=pass1_offset_remap,
        video=video,
        bitrate_kbps=bitrate,
        include_hdr_meta=False,
    )
    pass1.extend(["-pass", "1", "-an", "-f", "null", os.devnull])

    pass2 = _base()
    metadata_inputs = callbacks.materialize_container_metadata_inputs(
        config,
        source_idx=source_idx,
        next_input_index=len(all_sources),
        plan=plan,
        chapter_materialize_dir=chapter_materialize_dir,
        chapter_probe_source=config.source,
    )
    pass2.extend(metadata_inputs.input_args)

    track_assembly, pass2_offset_remap = callbacks.resolve_track_assembly_and_offset_remap(
        cmd=pass2,
        config=config,
        plan=plan,
        source_idx=source_idx,
        track_input_paths=_build_track_input_paths_plan(all_sources=all_sources),
        start_input_index=metadata_inputs.next_input_index,
    )

    callbacks.append_primary_video_map_and_codec(
        pass2,
        plan=plan,
        video_map=track_assembly.video_map,
        offset_remap=pass2_offset_remap,
        video=video,
        bitrate_kbps=bitrate,
    )
    pass2.extend(["-pass", "2"])
    callbacks.append_common_streams_and_metadata(
        pass2,
        config=config,
        source_idx=source_idx,
        all_sources_count=len(all_sources),
        plan=plan,
        metadata_inputs=metadata_inputs,
        offset_remap=pass2_offset_remap,
    )
    pass2.append(str(config.output))
    return [pass1, pass2]


def build_runtime_single_pass_with_sync(
    callbacks: EncodeCommandBuilderCallbacks,
    config: EncodeConfig,
    *,
    chapter_materialize_dir: Path | None = None,
    signals: TaskSignals | None = None,
    plan: _EncodePlan | None = None,
) -> tuple[list[str], LiveSyncSession | None, list[Path]]:
    plan = plan or callbacks.build_encode_plan(config)
    video = callbacks.primary_video_settings(config)
    all_sources = list(plan.all_sources)
    source_idx = dict(plan.source_idx)
    work_dir = config.work_dir or config.source.parent

    sync_remap, sync_inputs, live_session, strict_interleave = callbacks.prepare_multisource_sync(
        config=config,
        all_sources=all_sources,
        sync_base_input_idx=len(all_sources),
        work_dir=work_dir,
        signals=signals,
        allow_live=True,
        plan=plan,
    )

    cmd: list[str] = [callbacks.ffmpeg_bin, "-hide_banner", "-y"]
    cmd.extend(callbacks.ffmpeg_progress_args())
    cmd.extend(_hardware_input_args_domain(video, callbacks=callbacks.codec_domain_callbacks()))
    for src in all_sources:
        append_ffmpeg_input_args(cmd, src)
    callbacks.append_sync_inputs(cmd, sync_inputs)

    metadata_inputs = callbacks.materialize_container_metadata_inputs(
        config,
        source_idx=source_idx,
        next_input_index=len(all_sources) + len(sync_inputs),
        plan=plan,
        chapter_materialize_dir=chapter_materialize_dir,
        chapter_probe_source=config.source,
    )
    cmd.extend(metadata_inputs.input_args)

    vf = _build_encoder_vf_domain(video, callbacks=callbacks.codec_domain_callbacks())
    if vf:
        cmd.extend(["-vf", vf])

    cmd.extend(callbacks.ffmpeg_thread_args(None))
    track_assembly, offset_remap = callbacks.resolve_track_assembly_and_offset_remap(
        cmd=cmd,
        config=config,
        plan=plan,
        source_idx=source_idx,
        track_input_paths=_build_track_input_paths_plan(
            all_sources=all_sources,
            sync_inputs=sync_inputs,
        ),
        start_input_index=metadata_inputs.next_input_index,
        sync_remap=sync_remap,
        video_fallback_input=plan.video_source,
        allow_sync_rewrite=True,
        sync_rewrite_work_dir=work_dir,
        signals=signals,
    )
    callbacks.append_primary_video_map_and_codec(
        cmd,
        plan=plan,
        video_map=track_assembly.video_map,
        offset_remap=offset_remap,
        video=video,
    )
    callbacks.append_common_streams_and_metadata(
        cmd,
        config=config,
        source_idx=source_idx,
        all_sources_count=len(all_sources),
        plan=plan,
        metadata_inputs=metadata_inputs,
        offset_remap=offset_remap,
        sync_remap=sync_remap,
        strict_interleave=strict_interleave,
    )
    cmd.append(str(config.output))
    return cmd, live_session, _common_sync_cleanup_paths(sync_inputs)


def build_runtime_two_pass_with_sync(
    callbacks: EncodeCommandBuilderCallbacks,
    config: EncodeConfig,
    *,
    chapter_materialize_dir: Path | None = None,
    signals: TaskSignals | None = None,
    plan: _EncodePlan | None = None,
) -> tuple[list[list[str]], LiveSyncSession | None, list[Path]]:
    video = callbacks.primary_video_settings(config)
    bitrate = callbacks.size_to_bitrate_kbps(config)
    vf = _build_encoder_vf_domain(video, callbacks=callbacks.codec_domain_callbacks())
    plan = plan or callbacks.build_encode_plan(config)
    all_sources = list(plan.all_sources)
    source_idx = dict(plan.source_idx)
    work_dir = config.work_dir or config.source.parent

    sync_remap, sync_inputs, live_session, strict_interleave = callbacks.prepare_multisource_sync(
        config=config,
        all_sources=all_sources,
        sync_base_input_idx=len(all_sources),
        work_dir=work_dir,
        signals=signals,
        allow_live=False,
        plan=plan,
    )

    def _base(include_sync_inputs: bool) -> list[str]:
        c = [callbacks.ffmpeg_bin, "-hide_banner", "-y"]
        c.extend(callbacks.ffmpeg_progress_args())
        c.extend(_hardware_input_args_domain(video, callbacks=callbacks.codec_domain_callbacks()))
        for src in all_sources:
            append_ffmpeg_input_args(c, src)
        if include_sync_inputs:
            callbacks.append_sync_inputs(c, sync_inputs)
        if vf:
            c.extend(["-vf", vf])
        c.extend(callbacks.ffmpeg_thread_args(None))
        return c

    video_key = plan.video_key
    video_default_map = plan.video_default_map

    pass1 = _base(False)
    _next1, pass1_offset_remap = callbacks.append_offset_aux_inputs(
        pass1,
        _build_offset_specs_plan(
            config,
            track_mappings=[callbacks.video_track_mapping(config, plan.video_source)],
            offset_lookup=dict(plan.offset_lookup),
        ),
        start_input_index=len(all_sources),
    )
    _ = _next1
    callbacks.append_primary_video_map_and_codec(
        pass1,
        plan=plan,
        video_map=video_default_map,
        offset_remap=pass1_offset_remap,
        video=video,
        bitrate_kbps=bitrate,
        include_hdr_meta=False,
    )
    pass1.extend(["-pass", "1", "-an", "-f", "null", os.devnull])

    pass2 = _base(True)
    metadata_inputs = callbacks.materialize_container_metadata_inputs(
        config,
        source_idx=source_idx,
        next_input_index=len(all_sources) + len(sync_inputs),
        plan=plan,
        chapter_materialize_dir=chapter_materialize_dir,
        chapter_probe_source=config.source,
    )
    pass2.extend(metadata_inputs.input_args)

    track_assembly, pass2_offset_remap = callbacks.resolve_track_assembly_and_offset_remap(
        cmd=pass2,
        config=config,
        plan=plan,
        source_idx=source_idx,
        track_input_paths=_build_track_input_paths_plan(
            all_sources=all_sources,
            sync_inputs=sync_inputs,
        ),
        start_input_index=metadata_inputs.next_input_index,
        sync_remap=sync_remap,
        video_default_map=sync_remap.get(video_key, video_default_map),
        video_fallback_input=plan.video_source,
        allow_sync_rewrite=True,
        sync_rewrite_work_dir=work_dir,
        signals=signals,
    )
    callbacks.append_primary_video_map_and_codec(
        pass2,
        plan=plan,
        video_map=track_assembly.video_map,
        offset_remap=pass2_offset_remap,
        video=video,
        bitrate_kbps=bitrate,
    )
    pass2.extend(["-pass", "2"])
    callbacks.append_common_streams_and_metadata(
        pass2,
        config=config,
        source_idx=source_idx,
        all_sources_count=len(all_sources),
        plan=plan,
        metadata_inputs=metadata_inputs,
        offset_remap=pass2_offset_remap,
        sync_remap=sync_remap,
        strict_interleave=strict_interleave,
    )
    pass2.append(str(config.output))
    return [pass1, pass2], live_session, _common_sync_cleanup_paths(sync_inputs)
