"""NVEncC runtime execution services for encode workflows."""

from __future__ import annotations

import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from core.bluray import append_ffmpeg_input_args, is_bluray_playlist
from core.runner import TaskCancelledError, TaskSignals
from core.subprocess_utils import (
    decode_subprocess_output,
    subprocess_windows_no_window_kwargs,
)
from core.workflows.encode.models import EncodeConfig, EncodeError, QualityMode, VideoEncodeSettings
from core.workflows.encode.planning.offsets import track_offset_ms as _track_offset_ms_plan
from core.workflows.encode.planning.offsets import build_offset_specs as _build_offset_specs_plan
from core.workflows.encode.planning.plan_models import EncodePlan, MaterializedContainerMetadataPlan
from core.workflows.encode.planning.track_assembly import (
    build_track_input_paths as _build_track_input_paths_plan,
    resolve_track_assembly as _resolve_track_assembly_plan,
)
from core.workflows.encode.runtime.nvencc import (
    build_decode_pipe_cmd as _build_decode_pipe_cmd_runtime,
    build_nvencc_command as _build_nvencc_command_runtime,
    is_nvencc_codec as _is_nvencc_codec_runtime,
    nvencc_ffmpeg_filter_vf as _nvencc_ffmpeg_filter_vf_runtime,
    nvencc_intermediate_path as _nvencc_intermediate_path_runtime,
    nvencc_pipe_encode_video as _nvencc_pipe_encode_video_runtime,
    nvencc_requires_ffmpeg_filter_pipe as _nvencc_requires_ffmpeg_filter_pipe_runtime,
)
from core.workflows.encode.runtime.nvencc_routing import NvenccInputRouting
from core.workflows.encode.runtime.dovi_p7_router import DoviP7Router
from core.workflows.common.timeline_sync import sync_cleanup_paths as _common_sync_cleanup_paths
from core.workflows.remux_timeline_sync import LiveSyncSession


@dataclass(frozen=True)
class NvenccAssetPreparationCallbacks:
    ffmpeg_bin: str
    bins: dict[str, str]
    log: Callable[[str, str], None]
    primary_video_settings: Callable[[EncodeConfig], VideoEncodeSettings]
    video_source_path: Callable[[EncodeConfig], Path]
    video_stream_index: Callable[[EncodeConfig], int]
    source_is_vfr: Callable[[Path], bool]
    load_mediainfo_video_track: Callable[[Path], dict | None]


class NvenccAssetPreparationService:
    def __init__(self, callbacks: NvenccAssetPreparationCallbacks) -> None:
        self._cb = callbacks

    def prepare(
        self,
        config: EncodeConfig,
        *,
        work_dir: Path,
        signals: TaskSignals,
        run_cmd: Callable[[list[str], str], str],
        cleanup_paths: list[Path] | None = None,
    ) -> tuple[Path, int, Path | None, Path | None, bool, list[Path]]:
        _ = signals
        cb = self._cb
        video = cb.primary_video_settings(config)
        local_cleanup_paths: list[Path] = []
        effective_source = cb.video_source_path(config)
        effective_stream_index = cb.video_stream_index(config)
        source_is_vfr = cb.source_is_vfr(effective_source)
        converted_source: Path | None = None
        hdr10plus_json: Path | None = None
        dovi_rpu: Path | None = None
        dovi_converted_to_p8 = False

        def _track(path: Path) -> None:
            local_cleanup_paths.append(path)
            if cleanup_paths is not None:
                cleanup_paths.append(path)

        if video.copy_dv:
            p7_router = DoviP7Router()
            mi_video = cb.load_mediainfo_video_track(effective_source)
            decision = p7_router.analyze(
                source=effective_source,
                mi_video=mi_video,
                fallback_to_dovi_tool=True,
            )
            cb.log("INFO", f"Routage DV : {decision.reason}")
            if decision.conversion_needed:
                annexb_for_convert = work_dir / "source_annexb.hevc"
                annexb_cmd = [cb.ffmpeg_bin, "-nostdin", "-y"]
                append_ffmpeg_input_args(annexb_cmd, effective_source)
                annexb_cmd.extend([
                    "-map", f"0:{int(effective_stream_index)}",
                    "-c", "copy",
                    "-bsf:v", "hevc_mp4toannexb",
                    "-f", "hevc", str(annexb_for_convert),
                ])
                run_cmd(annexb_cmd, "ffmpeg-dv-annexb")
                _track(annexb_for_convert)
                converted = p7_router.execute_conversion(
                    source=annexb_for_convert,
                    output_dir=work_dir,
                    run_cmd=lambda cmd: run_cmd(cmd, "dovi-convert"),
                    dovi_tool_bin=cb.bins["dovi_tool"],
                    decision=decision,
                )
                _track(converted)
                converted_source = converted
                dovi_converted_to_p8 = True
                if not source_is_vfr:
                    effective_source = converted
                    effective_stream_index = 0

        if not (video.copy_dv or video.copy_hdr10plus):
            return (
                effective_source,
                effective_stream_index,
                hdr10plus_json,
                dovi_rpu,
                dovi_converted_to_p8,
                local_cleanup_paths,
            )

        raw_hevc_ext = {".hevc", ".h265", ".265", ".x265"}
        meta_input = effective_source
        if effective_source.suffix.lower() not in raw_hevc_ext and effective_source.suffix.lower() != ".mkv":
            meta_input = work_dir / "source_meta.hevc"
            meta_cmd = [cb.ffmpeg_bin, "-nostdin", "-y"]
            append_ffmpeg_input_args(meta_cmd, effective_source)
            meta_cmd.extend([
                "-map", f"0:{int(effective_stream_index)}",
                "-c", "copy",
                "-bsf:v", "hevc_mp4toannexb",
                "-f", "hevc", str(meta_input),
            ])
            run_cmd(meta_cmd, "ffmpeg-hdr-annexb")
            _track(meta_input)

        if video.copy_dv and dovi_converted_to_p8 and source_is_vfr and converted_source is not None:
            meta_input = converted_source

        if video.copy_dv:
            dovi_rpu = work_dir / "rpu.bin"
            run_cmd([
                cb.bins["dovi_tool"], "extract-rpu",
                "-i", str(meta_input),
                "-o", str(dovi_rpu),
            ], "dovi_tool")
            _track(dovi_rpu)

        if video.copy_hdr10plus:
            hdr10plus_json = work_dir / "hdr10p.json"
            run_cmd([
                cb.bins["hdr10plus_tool"], "extract",
                str(meta_input),
                "-o", str(hdr10plus_json),
            ], "hdr10plus_tool")
            _track(hdr10plus_json)

        return (
            effective_source,
            effective_stream_index,
            hdr10plus_json,
            dovi_rpu,
            dovi_converted_to_p8,
            local_cleanup_paths,
        )


class NvenccPipeExecutor:
    def run(
        self,
        *,
        decode_cmd: list[str],
        encode_cmd: list[str],
        cwd: Path,
        signals: TaskSignals,
    ) -> str:
        def _reader(stream, label: str, sink: list[str]) -> None:
            if stream is None:
                return
            while True:
                raw = stream.readline()
                if not raw:
                    break
                line = decode_subprocess_output(raw).rstrip()
                if not line:
                    continue
                sink.append(line)
                signals.progress.emit(f"[{label}] {line}")

        decode_lines: list[str] = []
        encode_lines: list[str] = []

        decode_proc = subprocess.Popen(
            decode_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            **subprocess_windows_no_window_kwargs(),
        )
        signals._register_proc(decode_proc)
        try:
            encode_proc = subprocess.Popen(
                encode_cmd,
                stdin=decode_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(cwd),
                **subprocess_windows_no_window_kwargs(),
            )
        except Exception:
            signals._unregister_proc(decode_proc)
            try:
                decode_proc.kill()
            except OSError:
                pass
            raise
        signals._register_proc(encode_proc)
        if decode_proc.stdout is not None:
            decode_proc.stdout.close()

        decode_reader = ThreadPoolExecutor(max_workers=2)
        decode_reader.submit(_reader, decode_proc.stderr, "ffmpeg-decode", decode_lines)
        decode_reader.submit(_reader, encode_proc.stdout, "nvencc", encode_lines)
        try:
            encode_rc = encode_proc.wait()
            decode_rc = decode_proc.wait()
            decode_reader.shutdown(wait=True)
            if signals._cancel_event.is_set():
                raise TaskCancelledError()
            if encode_rc != 0:
                tail = "\n".join(encode_lines[-40:])
                raise EncodeError(f"NVEncC a échoué.\n{tail}")
            if decode_rc not in (0, -13):
                tail = "\n".join(decode_lines[-40:])
                raise EncodeError(f"FFmpeg decode a échoué.\n{tail}")
            return "\n".join(encode_lines[-400:])
        finally:
            signals._unregister_proc(encode_proc)
            signals._unregister_proc(decode_proc)


@dataclass(frozen=True)
class NvenccRuntimeRemuxBuilderCallbacks:
    ffmpeg_bin: str
    ffmpeg_progress_args: Callable[[], list[str]]
    ffmpeg_thread_args: Callable[[int | None], list[str]]
    offset_input_args: Callable[[int], list[str]]
    build_encode_plan: Callable[[EncodeConfig], EncodePlan]
    prepare_multisource_sync: Callable[..., tuple[dict[tuple[Path, int, str], tuple[int, int]], list[Path | str], LiveSyncSession | None, bool]]
    append_sync_inputs: Callable[[list[str], list[Path | str]], None]
    materialize_container_metadata_inputs: Callable[..., MaterializedContainerMetadataPlan]
    append_offset_aux_inputs: Callable[..., tuple[int, dict[tuple[Path, int, str], tuple[int, int]]]]
    append_stream_maps_and_attachments: Callable[..., None]
    append_strict_interleave_mux_flags: Callable[[list[str]], None]
    append_container_metadata_args: Callable[..., None]


class NvenccRuntimeRemuxBuilder:
    def __init__(self, callbacks: NvenccRuntimeRemuxBuilderCallbacks) -> None:
        self._cb = callbacks

    def build(
        self,
        config: EncodeConfig,
        encoded_video: Path,
        *,
        video_offset_ms: int = 0,
        chapter_materialize_dir: Path | None = None,
        signals: TaskSignals | None = None,
        plan: EncodePlan | None = None,
    ) -> tuple[list[str], LiveSyncSession | None, list[Path]]:
        cb = self._cb
        plan = plan or cb.build_encode_plan(config)
        all_sources = list(plan.all_sources)
        source_idx_shifted = {source: index + 1 for source, index in dict(plan.source_idx).items()}
        work_dir = config.work_dir or config.source.parent

        sync_remap, sync_inputs, live_session, strict_interleave = cb.prepare_multisource_sync(
            config=config,
            all_sources=all_sources,
            sync_base_input_idx=1 + len(all_sources),
            work_dir=work_dir,
            signals=signals,
            allow_live=True,
            plan=plan,
        )

        cmd: list[str] = [cb.ffmpeg_bin, "-hide_banner", "-y"]
        cmd.extend(cb.ffmpeg_progress_args())
        cmd.extend(cb.offset_input_args(video_offset_ms))
        cmd.extend(["-i", str(encoded_video)])
        for src in all_sources:
            append_ffmpeg_input_args(cmd, src)
        cb.append_sync_inputs(cmd, sync_inputs)

        metadata_inputs = cb.materialize_container_metadata_inputs(
            config,
            source_idx=source_idx_shifted,
            next_input_index=1 + len(all_sources) + len(sync_inputs),
            plan=plan,
            chapter_materialize_dir=chapter_materialize_dir,
            chapter_probe_source=config.source,
        )
        cmd.extend(metadata_inputs.input_args)
        cmd.extend(cb.ffmpeg_thread_args(None))

        track_input_paths = _build_track_input_paths_plan(
            leading_inputs=(encoded_video,),
            all_sources=all_sources,
            sync_inputs=sync_inputs,
        )
        track_assembly = _resolve_track_assembly_plan(
            config,
            plan,
            source_idx=source_idx_shifted,
            track_input_paths=track_input_paths,
            sync_remap=sync_remap,
            include_video=False,
        )
        _next_input_index, offset_remap = cb.append_offset_aux_inputs(
            cmd,
            _build_offset_specs_plan(
                config,
                track_mappings=list(track_assembly.track_mappings),
                offset_lookup=dict(plan.offset_lookup),
            ),
            start_input_index=metadata_inputs.next_input_index,
        )
        _ = _next_input_index

        cmd.extend(["-map", "0:v:0"])
        cmd.extend(["-c:v", "copy"])
        cb.append_stream_maps_and_attachments(
            cmd,
            config,
            source_idx=source_idx_shifted,
            subtitle_copy_input_indices=[index + 1 for index in range(len(all_sources))],
            sync_remap=sync_remap,
            offset_remap=offset_remap,
            subtitle_tracks_override=list(plan.resolved_subtitle_tracks),
            force_copy_subtitles_wildcard=(config.copy_subtitles and not plan.subtitles_resolved),
        )
        if strict_interleave:
            cb.append_strict_interleave_mux_flags(cmd)

        default_source_input_index = 1 + int(plan.video_input_idx)
        cb.append_container_metadata_args(
            cmd,
            config,
            default_metadata_input_index=default_source_input_index,
            default_chapter_input_index=default_source_input_index,
            chapter_input_index=metadata_inputs.chapter_input_index,
            tag_input_index=metadata_inputs.tag_input_index,
            include_copy_video_stream_passthrough=True,
            plan=plan,
        )
        cmd.append(str(config.output))
        return cmd, live_session, _common_sync_cleanup_paths(sync_inputs)


@dataclass(frozen=True)
class NvenccDirectOutputRunnerCallbacks:
    ffmpeg_bin: str
    nvencc_bin: str | None
    check_cancelled: Callable[[TaskSignals | None], None]
    log_step: Callable[[int, str], None]
    log_info: Callable[[str], None]
    primary_video_settings: Callable[[EncodeConfig], VideoEncodeSettings]
    video_source_path: Callable[[EncodeConfig], Path]
    video_stream_index: Callable[[EncodeConfig], int]
    build_encode_plan: Callable[[EncodeConfig], EncodePlan]
    resolve_input_routing: Callable[[EncodeConfig], NvenccInputRouting]
    build_runtime_remux_cmd: Callable[..., tuple[list[str], LiveSyncSession | None, list[Path]]]
    run_cmd: Callable[[list[str], Path | None, str, Callable[[str], None], TaskSignals], str]


class NvenccDirectOutputRunner:
    def __init__(self, callbacks: NvenccDirectOutputRunnerCallbacks) -> None:
        self._cb = callbacks

    def run(
        self,
        config: EncodeConfig,
        cleanup_paths: list[Path],
        *,
        prep_signals: TaskSignals | None = None,
        plan: EncodePlan | None = None,
    ) -> TaskSignals:
        cb = self._cb
        signals = prep_signals or TaskSignals()
        cwd = config.work_dir or config.source.parent
        plan = plan or cb.build_encode_plan(config)

        def _task() -> None:
            chapter_dir: Path | None = None
            live_sync_session: LiveSyncSession | None = None
            sync_cleanup_paths: list[Path] = []
            try:
                cb.check_cancelled(signals)
                if config.chapter_overrides:
                    chapter_dir = Path(
                        tempfile.mkdtemp(
                            prefix="enc_chapters_",
                            dir=str(config.work_dir) if config.work_dir else None,
                        )
                    )
                    cleanup_paths.append(chapter_dir)

                video = cb.primary_video_settings(config)
                cb.log_step(5, "Préparation de l'encode NVEncC natif")
                intermediate = _nvencc_intermediate_path_runtime(cwd, video.codec)
                cleanup_paths.append(intermediate)
                routing = cb.resolve_input_routing(config)
                runtime_video = routing.video

                if (
                    not runtime_video.inject_hdr_meta
                    and (runtime_video.master_display or runtime_video.max_cll)
                ):
                    runtime_video = replace(runtime_video, inject_hdr_meta=True)

                if routing.rebased_to_source:
                    cb.log_info(
                        "NVEncC HDR dynamique : rebascule sur la source d'origine "
                        "pour préserver les timestamps et la copie native DoVi/HDR10+."
                    )
                if routing.forced_reader == "avsw":
                    cb.log_info(
                        "NVEncC HDR dynamique : --avsw forcé pour une entrée sans "
                        "timestamps compatibles avec le chemin copy natif."
                    )

                video_offset_ms = _track_offset_ms_plan(
                    dict(plan.offset_lookup),
                    track_type="video",
                    source_path=cb.video_source_path(config),
                    stream_index=cb.video_stream_index(config),
                )
                needs_ffmpeg_pipe = (
                    _nvencc_requires_ffmpeg_filter_pipe_runtime(runtime_video)
                    or is_bluray_playlist(routing.input_path)
                )
                encode_cmd = _build_nvencc_command_runtime(
                    cb.nvencc_bin or "",
                    (
                        _nvencc_pipe_encode_video_runtime(runtime_video)
                        if needs_ffmpeg_pipe
                        else runtime_video
                    ),
                    intermediate,
                    input_path=None if needs_ffmpeg_pipe else routing.input_path,
                    stream_index=None if needs_ffmpeg_pipe else routing.stream_index,
                    input_reader=None if needs_ffmpeg_pipe else routing.input_reader,
                    input_fps=None if needs_ffmpeg_pipe else routing.input_fps,
                    input_avsync=None if needs_ffmpeg_pipe else routing.input_avsync,
                    hdr10plus_json=None,
                    dovi_rpu=None,
                    dovi_rpu_prm=None if needs_ffmpeg_pipe else routing.dovi_rpu_prm,
                )
                decode_cmd: list[str] | None = None
                if needs_ffmpeg_pipe:
                    decode_cmd = _build_decode_pipe_cmd_runtime(
                        cb.ffmpeg_bin,
                        routing.input_path,
                        stream_index=routing.stream_index,
                        vf=_nvencc_ffmpeg_filter_vf_runtime(runtime_video),
                    )
                remux_cmd, live_sync_session, sync_cleanup_paths = cb.build_runtime_remux_cmd(
                    config,
                    intermediate,
                    video_offset_ms=video_offset_ms,
                    chapter_materialize_dir=chapter_dir,
                    signals=signals,
                    plan=plan,
                )
                cleanup_paths.extend(sync_cleanup_paths)
                if live_sync_session is not None:
                    for proc in live_sync_session.processes:
                        signals._register_proc(proc)

                cb.check_cancelled(signals)
                cb.log_step(6, "Encodage NVEncC")
                if decode_cmd is not None:
                    NvenccPipeExecutor().run(
                        decode_cmd=decode_cmd,
                        encode_cmd=encode_cmd,
                        cwd=cwd,
                        signals=signals,
                    )
                else:
                    cb.run_cmd(
                        encode_cmd,
                        cwd,
                        "nvencc",
                        lambda line: signals.progress.emit(line),
                        signals,
                    )
                cb.check_cancelled(signals)
                cb.log_step(7, "Remux final ffmpeg")
                output = cb.run_cmd(
                    remux_cmd,
                    cwd,
                    "ffmpeg-remux",
                    lambda line: signals.progress.emit(line),
                    signals,
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

        if prep_signals is not None:
            _task()
            return signals

        executor = ThreadPoolExecutor(max_workers=1)
        executor.submit(_task)
        executor.shutdown(wait=False)
        return signals


def build_nvencc_pipeline_commands(
    config: EncodeConfig,
    *,
    nvencc_bin: str | None,
    ffmpeg_bin: str,
    video_tracks: Callable[[EncodeConfig], list[VideoEncodeSettings]],
    resolve_input_routing: Callable[[EncodeConfig], NvenccInputRouting],
) -> list[list[str]] | None:
    if len(video_tracks(config)) != 1:
        return None
    videos = [v for v in (config.video_tracks or []) if v.codec != "copy"]
    if config.video and config.video.codec != "copy" and not videos:
        videos = [config.video]
    if len(videos) != 1:
        return None
    video = videos[0]
    if not _is_nvencc_codec_runtime(video.codec):
        return None
    if not nvencc_bin:
        return None
    if video.quality_mode == QualityMode.SIZE:
        return None

    work_dir = (config.work_dir or Path(tempfile.gettempdir())).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    intermediate = _nvencc_intermediate_path_runtime(work_dir, video.codec)
    routing = resolve_input_routing(config)
    needs_ffmpeg_pipe = (
        _nvencc_requires_ffmpeg_filter_pipe_runtime(routing.video)
        or is_bluray_playlist(routing.input_path)
    )
    encode = _build_nvencc_command_runtime(
        nvencc_bin,
        (
            _nvencc_pipe_encode_video_runtime(routing.video)
            if needs_ffmpeg_pipe
            else routing.video
        ),
        intermediate,
        input_path=None if needs_ffmpeg_pipe else routing.input_path,
        stream_index=None if needs_ffmpeg_pipe else routing.stream_index,
        input_reader=None if needs_ffmpeg_pipe else routing.input_reader,
        input_fps=None if needs_ffmpeg_pipe else routing.input_fps,
        input_avsync=None if needs_ffmpeg_pipe else routing.input_avsync,
        dovi_rpu_prm=None if needs_ffmpeg_pipe else routing.dovi_rpu_prm,
    )
    if needs_ffmpeg_pipe:
        decode = _build_decode_pipe_cmd_runtime(
            ffmpeg_bin,
            routing.input_path,
            stream_index=routing.stream_index,
            vf=_nvencc_ffmpeg_filter_vf_runtime(routing.video),
        )
    else:
        decode = None
    remux = [
        ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(intermediate),
    ]
    append_ffmpeg_input_args(remux, config.source)
    remux.extend([
        "-map", "0:v:0",
        "-map", "1:a?",
        "-map", "1:s?",
        "-map_chapters", "1",
        "-c", "copy",
        str(config.output),
    ])
    return [decode, encode, remux] if decode is not None else [encode, remux]
