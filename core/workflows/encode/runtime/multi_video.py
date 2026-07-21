from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, cast

from core.bluray import append_ffmpeg_input_args
from core.runner import TaskCancelledError, TaskSignals
from core.workdir import remove_path
from core.workflows.encode.domain import (
    needs_static_hdr_bitstream_patch,
    should_reinject_static_hdr_metadata,
)
from core.workflows.encode.models import EncodeConfig, EncodeError, QualityMode, VideoEncodeSettings
from core.workflows.encode.planning.plan_models import EncodePlan
from core.workflows.encode.planning.track_assembly import build_track_input_paths, resolve_track_assembly
from core.workflows.hevc_static_hdr_metadata import inject_static_hdr_sei_file
from core.matroska.editors.dovi import DolbyVisionConfigRecord
from core.matroska.hevc.access_units import HevcStreamCancelled
from core.matroska.hevc.payload_rewriter import MatroskaHevcPayloadRewriter
from core.matroska.hevc.timing_skeleton import write_timing_skeleton
from core.matroska.writer import MatroskaWriteCancelled
from core.workflows.encode.runtime_helpers import (
    VideoTrackPreparationOrchestrator,
    VideoTrackPrepSpec,
    VideoTrackPrepTask,
)
from core.workflows.remux_timeline_sync import LiveSyncSession


@dataclass(frozen=True)
class PreparedVideoInput:
    input_args: list[str]
    path: Path | str
    map_arg: str


@dataclass(frozen=True)
class MultiVideoPipelineRunnerCallbacks:
    ffmpeg_bin: str
    bins: dict[str, str]
    max_parallel_video_encodes: int
    check_cancelled: Callable[[TaskSignals | None], None]
    build_encode_plan: Callable[[EncodeConfig], EncodePlan]
    video_tracks: Callable[[EncodeConfig], list[VideoEncodeSettings]]
    video_source_from_settings: Callable[[EncodeConfig, VideoEncodeSettings], Path]
    video_stream_from_settings: Callable[[VideoEncodeSettings], int]
    track_offset_ms: Callable[..., int]
    offset_input_args: Callable[[int], list[str]]
    parallel_video_worker_thread_count: Callable[..., int | None]
    video_encode_resource_key: Callable[[VideoEncodeSettings], str]
    parallel_video_min_available_ram_bytes: Callable[[], int]
    video_prep_estimated_ram_bytes: Callable[[VideoTrackPrepSpec], int]
    format_bytes: Callable[[int], str]
    available_ram_bytes: Callable[[], int]
    source_input_index_map: Callable[[list[Path], int], dict[Path, int]]
    prepare_multisource_sync: Callable[..., tuple[dict[tuple[Path, int, str], tuple[int, int]], list[Path | str], LiveSyncSession | None, bool]]
    sync_cleanup_paths: Callable[[list[Path | str]], list[Path]]
    append_sync_inputs: Callable[[list[str], list[Path | str]], None]
    prepare_container_metadata_inputs: Callable[..., tuple[int, int | None, int | None]]
    ffmpeg_thread_args: Callable[[], list[str]]
    append_offset_aux_inputs: Callable[..., tuple[int, dict[tuple[Path, int, str], tuple[int, int]]]]
    build_offset_specs: Callable[..., object]
    append_stream_maps_and_attachments: Callable[..., None]
    append_strict_interleave_mux_flags: Callable[[list[str]], None]
    append_container_metadata_args: Callable[..., None]
    ffmpeg_progress_args: Callable[[], list[str]]
    run_cmd: Callable[[list[str], Path | None, str, Callable[[str], None], TaskSignals], str]
    finalize_ffmpeg: Callable[..., str]
    log_step: Callable[[int, str], None]
    log_info: Callable[[str], None]
    ui_encode_progress_message: Callable[..., str]
    cleanup_two_pass_logs_for_prefix: Callable[[Path], None]
    build_video_only_mkv_commands: Callable[..., list[list[str]]]
    build_dovi_record_from_rpu: Callable[..., DolbyVisionConfigRecord | None]
    build_multi_video_track_encode_commands: Callable[..., list[list[str]]]
    two_pass_log_prefix: Callable[[Path, str], Path]
    #: Assemblage final Matroska natif (lot 2). None → reconstruction FFmpeg.
    native_assemble: Callable[..., None] | None = None


class MultiVideoPipelineRunner:
    """Runner dédié au pipeline multi-pistes vidéo."""

    def __init__(self, callbacks: MultiVideoPipelineRunnerCallbacks) -> None:
        self._callbacks = callbacks

    def prepare_multi_video_track(
        self,
        *,
        config: EncodeConfig,
        spec: VideoTrackPrepSpec,
        work_dir: Path,
        total_tracks: int,
        thread_count: int | None,
        signals: TaskSignals,
        run_cmd: Callable[[list[str], str], str],
    ) -> tuple[PreparedVideoInput, list[Path]]:
        cb = self._callbacks
        order = spec.order
        video = spec.video
        source = spec.source
        offset_ms = spec.offset_ms
        index = order + 1
        local_cleanup: list[Path] = []

        cb.check_cancelled(signals)
        cb.log_info(f"Préparation vidéo {index}/{total_tracks}…")

        if video.copy_dv or video.copy_hdr10plus or needs_static_hdr_bitstream_patch(video):
            rpu_bin = work_dir / f"video_{index}.rpu.bin"
            hdr10p_json = work_dir / f"video_{index}.hdr10plus.json"
            enc_video_mkv = work_dir / f"video_{index}.enc.mkv"
            current_hevc = work_dir / f"video_{index}.enc.hevc"
            # dovi_tool / hdr10plus_tool n'acceptent que MKV ou HEVC annexB :
            # pré-extraction obligatoire pour MP4/MOV/TS/... (BSF hevc_mp4toannexb).
            _RAW_HEVC_EXT = {".hevc", ".h265", ".265", ".x265"}
            src_ext = source.suffix.lower()
            if src_ext not in _RAW_HEVC_EXT and src_ext != ".mkv":
                annexb_src = work_dir / f"video_{index}.source.hevc"
                annexb_cmd = [cb.ffmpeg_bin, "-nostdin", "-y"]
                append_ffmpeg_input_args(annexb_cmd, source)
                annexb_cmd.extend([
                    "-map", "0:v:0", "-c", "copy",
                    "-bsf:v", "hevc_mp4toannexb",
                    "-f", "hevc", str(annexb_src),
                ])
                run_cmd(annexb_cmd, f"annexb-extract-{index}")
                local_cleanup.append(annexb_src)
                meta_input = annexb_src
            else:
                meta_input = source
            if video.copy_dv:
                run_cmd([
                    cb.bins["dovi_tool"], "extract-rpu",
                    "-i", str(meta_input), "-o", str(rpu_bin),
                ], f"dovi-extract-{index}")
                local_cleanup.append(rpu_bin)
            if video.copy_hdr10plus:
                run_cmd([
                    cb.bins["hdr10plus_tool"], "extract",
                    str(meta_input), "-o", str(hdr10p_json),
                ], f"hdr10plus-extract-{index}")
                local_cleanup.append(hdr10p_json)

            record_for_rewriter = None
            if video.copy_dv and rpu_bin.exists():
                record_for_rewriter = cb.build_dovi_record_from_rpu(
                    rpu_bin=rpu_bin,
                    dovi_tool_bin=cb.bins["dovi_tool"],
                )
                if cb.native_assemble is not None and record_for_rewriter is None:
                    raise EncodeError(
                        f"Piste vidéo {index}: Dolby Vision natif requis mais le "
                        "DolbyVisionConfigRecord n'a pas pu être construit."
                    )

            # Encode vidéo-seule vers un MKV mono-piste horodaté (lot 3) :
            # les timestamps de la source ne sont plus réassociés au bitstream.
            passlog_prefix = (
                cb.two_pass_log_prefix(work_dir, f"video_{index}")
                if video.quality_mode == QualityMode.SIZE
                else None
            )
            try:
                for pass_index, cmd in enumerate(
                    cb.build_video_only_mkv_commands(
                        config,
                        video,
                        source,
                        enc_video_mkv,
                        offset_ms=offset_ms,
                        passlog_prefix=passlog_prefix,
                        thread_count=thread_count,
                    ),
                    start=1,
                ):
                    run_cmd(cmd, f"ffmpeg-video-{index}-pass{pass_index}" if passlog_prefix else f"ffmpeg-video-{index}")
            finally:
                if passlog_prefix is not None:
                    cb.cleanup_two_pass_logs_for_prefix(passlog_prefix)
            local_cleanup.append(enc_video_mkv)
            # Squelette de timing : mêmes TrackEntry/timestamps que le MKV
            # encodé, payloads vides. Le MKV encodé complet est libéré dès
            # l'extraction annexB — pic disque 2× vidéo au lieu de 3×.
            timing_mkv = work_dir / f"video_{index}.timing.mkv"
            local_cleanup.append(timing_mkv)
            try:
                write_timing_skeleton(
                    enc_video_mkv, timing_mkv,
                    cancel_cb=signals._cancel_event.is_set,
                )
            except MatroskaWriteCancelled as exc:
                raise TaskCancelledError() from exc
            run_cmd([
                cb.ffmpeg_bin, "-nostdin", "-y",
                "-i", str(enc_video_mkv),
                "-map", "0:v:0", "-c", "copy",
                "-bsf:v", "hevc_mp4toannexb",
                "-f", "hevc", str(current_hevc),
            ], f"annexb-from-encoded-{index}")
            local_cleanup.append(current_hevc)
            remove_path(enc_video_mkv)

            if video.copy_hdr10plus and hdr10p_json.exists():
                hdr10_out = work_dir / f"video_{index}.hdr10plus.hevc"
                run_cmd([
                    cb.bins["hdr10plus_tool"], "inject",
                    "-i", str(current_hevc),
                    "-j", str(hdr10p_json),
                    "-o", str(hdr10_out),
                ], f"hdr10plus-inject-{index}")
                local_cleanup.append(hdr10_out)
                remove_path(current_hevc)
                current_hevc = hdr10_out
            if video.copy_dv and rpu_bin.exists():
                dovi_out = work_dir / f"video_{index}.dovi.hevc"
                run_cmd([
                    cb.bins["dovi_tool"],
                    "-m", video.dovi_profile,
                    "inject-rpu",
                    "-i", str(current_hevc),
                    "-r", str(rpu_bin),
                    "-o", str(dovi_out),
                ], f"dovi-inject-{index}")
                local_cleanup.append(dovi_out)
                remove_path(current_hevc)
                current_hevc = dovi_out

            if should_reinject_static_hdr_metadata(video):
                static_hdr_out = work_dir / f"video_{index}.hdr_static.hevc"
                if str(getattr(video, "static_hdr_metadata_source", "") or "") == "estimated_p5_to_p8":
                    confidence = str(getattr(video, "static_hdr_metadata_confidence", "") or "?")
                    mode = str(getattr(video, "static_hdr_metadata_analysis_mode", "") or "?")
                    cb.log_info(
                        f"Piste video {index}: metadata HDR10 statiques estimees "
                        f"P5->P8.1 (confiance {confidence}, mode {mode})."
                    )
                cb.log_info(f"Piste video {index}: injection metadonnees HDR statiques…")
                static_hdr_result = inject_static_hdr_sei_file(
                    current_hevc,
                    static_hdr_out,
                    master_display=video.master_display,
                    max_cll=video.max_cll,
                )
                if static_hdr_result.applied:
                    try:
                        current_hevc.unlink(missing_ok=True)
                    except OSError:
                        pass
                    local_cleanup.append(static_hdr_out)
                    current_hevc = static_hdr_out
                    cb.log_info(
                        f"Piste video {index}: SEI HDR statiques injectes sur "
                        f"{static_hdr_result.injected_access_units} access unit(s)."
                    )
                else:
                    static_hdr_out.unlink(missing_ok=True)

            # Réécriture des payloads dans le squelette de timing : timestamps
            # de l'encodeur conservés, signalisation DoVi portée par le
            # TrackEntry. Aucun repli après le démarrage : un échec est fatal.
            wrapped = work_dir / f"video_{index}.wrapped.mkv"
            try:
                rewrite_result = MatroskaHevcPayloadRewriter().rewrite(
                    encoded_mkv=timing_mkv,
                    injected_hevc=current_hevc,
                    output=wrapped,
                    dovi_record=record_for_rewriter,
                    cancel_cb=signals._cancel_event.is_set,
                )
            except (HevcStreamCancelled, MatroskaWriteCancelled) as exc:
                raise TaskCancelledError() from exc
            cb.log_info(
                f"Piste video {index}: payloads réécrits "
                f"({rewrite_result.frames_rewritten} frame(s), timestamps encodeur)."
            )
            # Le squelette et le bitstream injecté ne sont plus consultés
            # après le retour du rewriter. Les libérer ici borne le pic disque
            # quand plusieurs pistes HDR/DoVi sont préparées en parallèle.
            remove_path(timing_mkv)
            remove_path(current_hevc)
            local_cleanup.append(wrapped)
            return PreparedVideoInput(
                input_args=[],
                path=wrapped,
                map_arg=f"{order}:v:0",
            ), local_cleanup

        output_path = work_dir / f"video_{index}.mkv"
        multi_passlog_prefix = (
            cb.two_pass_log_prefix(work_dir, f"video_{index}")
            if video.quality_mode == QualityMode.SIZE
            else None
        )
        commands = cb.build_multi_video_track_encode_commands(
            config,
            video,
            source,
            output_path,
            offset_ms=offset_ms,
            passlog_prefix=multi_passlog_prefix,
            thread_count=thread_count,
        )
        try:
            for pass_index, cmd in enumerate(commands, start=1):
                label = (
                    f"ffmpeg-video-{index}"
                    if len(commands) == 1
                    else f"ffmpeg-video-{index}-pass{pass_index}"
                )
                run_cmd(cmd, label)
        finally:
            if multi_passlog_prefix is not None:
                cb.cleanup_two_pass_logs_for_prefix(multi_passlog_prefix)
        local_cleanup.append(output_path)
        return PreparedVideoInput(
            input_args=[],
            path=output_path,
            map_arg=f"{order}:v:0",
        ), local_cleanup

    def run(
        self,
        config: EncodeConfig,
        cleanup_paths: list[Path],
        *,
        prep_signals: TaskSignals | None = None,
        plan: EncodePlan | None = None,
    ) -> TaskSignals:
        cb = self._callbacks
        signals = prep_signals or TaskSignals()
        executor = None if prep_signals is not None else ThreadPoolExecutor(max_workers=1)

        def _run_pipeline() -> None:
            work_dir = config.work_dir or config.source.parent
            encode_plan = plan or cb.build_encode_plan(config)
            chapter_dir: Path | None = None
            live_sync_session: LiveSyncSession | None = None
            sync_cleanup_paths: list[Path] = []

            def _run(cmd: list[str], *, cwd: Path | None = work_dir, label: str = "ffmpeg") -> str:
                is_multi_video_ffmpeg = label.startswith("ffmpeg-video-")

                def _progress(line: str) -> None:
                    if is_multi_video_ffmpeg and not line.startswith("$ "):
                        signals.progress.emit(
                            cb.ui_encode_progress_message(label=label, event="line", line=line)
                        )
                        return
                    signals.progress.emit(line)

                output = cb.run_cmd(
                    cmd,
                    cwd,
                    label,
                    _progress,
                    signals,
                )
                if is_multi_video_ffmpeg:
                    signals.progress.emit(
                        cb.ui_encode_progress_message(label=label, event="done")
                    )
                return output

            try:
                if config.chapter_overrides:
                    chapter_dir = Path(
                        tempfile.mkdtemp(
                            prefix="enc_multi_video_chapters_",
                            dir=str(config.work_dir) if config.work_dir else None,
                        )
                    )
                    cleanup_paths.append(chapter_dir)

                video_tracks = cb.video_tracks(config)
                if not video_tracks:
                    raise EncodeError("Aucune piste vidéo configurée pour le pipeline multi-pistes.")
                prepared_inputs: list[PreparedVideoInput | None] = [None] * len(video_tracks)

                track_specs: list[VideoTrackPrepSpec] = []
                for order, video in enumerate(video_tracks):
                    source = cb.video_source_from_settings(config, video)
                    stream_index = cb.video_stream_from_settings(video)
                    offset_ms = cb.track_offset_ms(
                        dict(encode_plan.offset_lookup),
                        track_type="video",
                        source_path=source,
                        stream_index=stream_index,
                        allow_single_video_source_fallback=False,
                    )
                    track_specs.append(
                        VideoTrackPrepSpec(
                            order=order,
                            video=video,
                            source=source,
                            stream_index=stream_index,
                            offset_ms=offset_ms,
                        )
                    )

                transcode_specs: list[VideoTrackPrepSpec] = []
                for spec in track_specs:
                    if spec.video.codec == "copy":
                        order = spec.order
                        prepared_inputs[order] = PreparedVideoInput(
                            input_args=cb.offset_input_args(spec.offset_ms),
                            path=spec.source,
                            map_arg=f"{order}:{spec.stream_index}",
                        )
                        continue
                    transcode_specs.append(spec)

                max_parallel = max(1, int(cb.max_parallel_video_encodes))
                prep_thread_count = cb.parallel_video_worker_thread_count(
                    resource_keys=[
                        cb.video_encode_resource_key(spec.video)
                        for spec in transcode_specs
                    ],
                    max_parallel=max_parallel,
                )
                if transcode_specs and max_parallel > 1:
                    cb.log_info(
                        f"Préparation vidéo parallèle activée ({min(max_parallel, len(transcode_specs))} piste(s) max)."
                    )
                if prep_thread_count is not None:
                    cb.log_info(
                        "Répartition threads FFmpeg sur la préparation vidéo parallèle: "
                        f"{prep_thread_count} thread(s) par worker."
                    )

                if transcode_specs:
                    min_available_ram = cb.parallel_video_min_available_ram_bytes()
                    if max_parallel > 1 and min_available_ram > 0:
                        cb.log_info(
                            "Garde-fou RAM parallèle actif: réserve minimale "
                            f"{cb.format_bytes(min_available_ram)}."
                        )

                    ram_wait_notified: set[int] = set()

                    def _on_ram_wait(order: int, required: int, available: int) -> None:
                        if order in ram_wait_notified:
                            return
                        ram_wait_notified.add(order)
                        cb.log_info(
                            "Préparation vidéo différée par le garde-fou RAM "
                            f"(piste #{order + 1}, libre={cb.format_bytes(available)}, "
                            f"requis={cb.format_bytes(required)})."
                        )

                    orchestrator = VideoTrackPreparationOrchestrator(
                        max_parallel=max_parallel,
                        cancel_cb=lambda: cb.check_cancelled(signals),
                        on_worker_failure=signals.cancel,
                        min_available_ram_bytes=min_available_ram,
                        available_ram_cb=cb.available_ram_bytes,
                        on_ram_wait=_on_ram_wait,
                    )

                    tasks = [
                        VideoTrackPrepTask(
                            order=spec.order,
                            resource_key=cb.video_encode_resource_key(spec.video),
                            estimated_ram_bytes=cb.video_prep_estimated_ram_bytes(spec),
                            run=cast(
                                Callable[[], tuple[dict[str, object], list[Path]]],
                                lambda spec=spec: self.prepare_multi_video_track(
                                    config=config,
                                    spec=spec,
                                    work_dir=work_dir,
                                    total_tracks=len(video_tracks),
                                    thread_count=prep_thread_count,
                                    signals=signals,
                                    run_cmd=lambda cmd, label: _run(cmd, label=label),
                            ),
                            ),
                        )
                        for spec in transcode_specs
                    ]

                    for order, prepared, local_cleanup in orchestrator.execute(tasks):
                        prepared_inputs[order] = cast(PreparedVideoInput, prepared)
                        cleanup_paths.extend(local_cleanup)

                if any(spec is None for spec in prepared_inputs):
                    raise EncodeError("Préparation vidéo incomplète: au moins une piste n'a pas été préparée.")
                prepared_inputs_ready: list[PreparedVideoInput] = [
                    cast(PreparedVideoInput, prepared_item)
                    for prepared_item in prepared_inputs
                    if prepared_item is not None
                ]

                if cb.native_assemble is not None:
                    cb.log_step(5, "Assemblage final Matroska natif multi-pistes")
                    cb.native_assemble(
                        config,
                        prepared_inputs=prepared_inputs_ready,
                        track_specs=track_specs,
                        signals=signals,
                        plan=encode_plan,
                        work_dir=work_dir,
                    )
                    signals.finished.emit(str(config.output))
                    return

                cb.log_step(5, "Reconstruction finale multi-pistes vidéo")
                all_sources = list(encode_plan.all_sources)
                source_idx = cb.source_input_index_map(all_sources, len(prepared_inputs_ready))
                sync_remap: dict[tuple[Path, int, str], tuple[int, int]] = {}
                sync_inputs: list[Path | str] = []
                strict_interleave = False

                final_cmd: list[str] = [cb.ffmpeg_bin, "-hide_banner", "-y"]
                final_cmd.extend(cb.ffmpeg_progress_args())
                for prepared_input in prepared_inputs_ready:
                    final_cmd.extend(prepared_input.input_args)
                    append_ffmpeg_input_args(final_cmd, prepared_input.path)
                for src in all_sources:
                    append_ffmpeg_input_args(final_cmd, src)

                sync_remap, sync_inputs, live_sync_session, strict_interleave = cb.prepare_multisource_sync(
                    config=config,
                    all_sources=all_sources,
                    sync_base_input_idx=len(prepared_inputs_ready) + len(all_sources),
                    work_dir=work_dir,
                    signals=signals,
                    allow_live=True,
                    plan=encode_plan,
                )
                sync_cleanup_paths = cb.sync_cleanup_paths(sync_inputs)
                cb.append_sync_inputs(final_cmd, sync_inputs)
                if live_sync_session is not None:
                    for proc in live_sync_session.processes:
                        signals._register_proc(proc)

                next_input_index, chapter_input_index, tag_input_index = cb.prepare_container_metadata_inputs(
                    final_cmd,
                    config,
                    source_idx=source_idx,
                    next_input_index=len(prepared_inputs_ready) + len(all_sources) + len(sync_inputs),
                    plan=encode_plan,
                    chapter_materialize_dir=chapter_dir,
                    chapter_probe_source=config.source,
                )
                final_cmd.extend(cb.ffmpeg_thread_args())
                resolved_subtitle_tracks = list(encode_plan.resolved_subtitle_tracks)
                track_assembly = resolve_track_assembly(
                    config,
                    encode_plan,
                    source_idx=source_idx,
                    track_input_paths=build_track_input_paths(
                        leading_inputs=[prepared_input.path for prepared_input in prepared_inputs_ready],
                        all_sources=all_sources,
                        sync_inputs=sync_inputs,
                    ),
                    sync_remap=sync_remap,
                    include_video=False,
                )

                next_input_index, offset_remap = cb.append_offset_aux_inputs(
                    final_cmd,
                    cb.build_offset_specs(
                        config,
                        track_mappings=list(track_assembly.track_mappings),
                        offset_lookup=dict(encode_plan.offset_lookup),
                    ),
                    start_input_index=next_input_index,
                )
                _ = next_input_index

                for out_idx, prepared_input in enumerate(prepared_inputs_ready):
                    final_cmd.extend(["-map", str(prepared_input.map_arg)])
                    final_cmd.extend([f"-c:v:{out_idx}", "copy"])

                cb.append_stream_maps_and_attachments(
                    final_cmd,
                    config,
                    source_idx=source_idx,
                    subtitle_copy_input_indices=list(source_idx.values()),
                    sync_remap=sync_remap,
                    offset_remap=offset_remap,
                    subtitle_tracks_override=resolved_subtitle_tracks,
                    force_copy_subtitles_wildcard=(config.copy_subtitles and not encode_plan.subtitles_resolved),
                )
                if strict_interleave:
                    cb.append_strict_interleave_mux_flags(final_cmd)

                default_source_index = source_idx.get(config.source, len(prepared_inputs_ready))
                cb.append_container_metadata_args(
                    final_cmd,
                    config,
                    default_metadata_input_index=default_source_index,
                    default_chapter_input_index=default_source_index,
                    chapter_input_index=chapter_input_index,
                    tag_input_index=tag_input_index,
                    include_copy_video_stream_passthrough=False,
                    plan=encode_plan,
                )
                final_cmd.append(str(config.output))
                output = cb.finalize_ffmpeg(
                    config,
                    final_cmd,
                    work_dir,
                    "ffmpeg-multi-video",
                    signals,
                    plan=encode_plan,
                )
                signals.finished.emit(output)
            except TaskCancelledError:
                signals.cancelled.emit()
            except Exception as exc:
                signals.failed.emit(str(exc), exc)
            finally:
                if live_sync_session is not None:
                    for proc in live_sync_session.processes:
                        signals._unregister_proc(proc)
                    live_sync_session.close()
                for path in sync_cleanup_paths:
                    try:
                        remove_path(path)
                    except OSError:
                        pass

        if prep_signals is not None:
            _run_pipeline()
            return signals

        assert executor is not None
        executor.submit(_run_pipeline)
        executor.shutdown(wait=False)
        return signals
