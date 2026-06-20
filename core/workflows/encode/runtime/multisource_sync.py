"""Multi-source timeline sync helpers for encode workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.bluray import append_ffmpeg_input_args
from core.runner import TaskSignals
from core.workflows.common.timeline_sync import (
    append_strict_interleave_mux_flags as _common_append_strict_interleave_mux_flags,
    append_sync_inputs as _common_append_sync_inputs,
)
from core.workflows.encode.planning.offsets import offset_seconds as _offset_seconds_plan
from core.workflows.encode.planning.plan_models import EncodePlan
from core.workflows.encode.models import EncodeConfig
from core.workflows.encode.runtime_helpers import EncodeOffsetInputSpec
from core.workflows.remux_models import SourceInput
from core.workflows.remux_timeline_sync import (
    FfmpegTimelineSync,
    LiveSyncSession,
    TimelineSyncFallbackHelper,
)


@dataclass(frozen=True)
class EncodeMultisourceSyncCallbacks:
    ffmpeg_bin: str
    ffmpeg_thread_args: Callable[[], list[str]]
    log: Callable[[str, str], None]
    build_encode_plan: Callable[[EncodeConfig], EncodePlan]
    decide_strict_interleave_with_prescan: Callable[..., bool]
    ram_buffer_enabled: bool
    ram_buffer_dir: Callable[[], Path | None]
    syncer_factory: Callable[..., FfmpegTimelineSync] = FfmpegTimelineSync
    fallback_helper_factory: Callable[..., TimelineSyncFallbackHelper] = TimelineSyncFallbackHelper


def append_offset_aux_inputs(
    cmd: list[str],
    specs: list[EncodeOffsetInputSpec],
    *,
    start_input_index: int,
) -> tuple[int, dict[tuple[Path, int, str], tuple[int, int]]]:
    next_input_index = int(start_input_index)
    input_by_key: dict[tuple[str, int, int, str], int] = {}
    remap: dict[tuple[Path, int, str], tuple[int, int]] = {}

    for spec in specs:
        input_key = (
            str(spec.input_path),
            int(spec.input_stream_index),
            int(spec.offset_ms),
            str(spec.map_key[2]),
        )
        input_idx = input_by_key.get(input_key)
        if input_idx is None:
            if int(spec.offset_ms) > 0:
                cmd.extend(["-itsoffset", _offset_seconds_plan(spec.offset_ms)])
                append_ffmpeg_input_args(cmd, spec.input_path)
            else:
                cmd.extend(["-ss", _offset_seconds_plan(spec.offset_ms)])
                append_ffmpeg_input_args(cmd, spec.input_path)
            input_idx = next_input_index
            input_by_key[input_key] = input_idx
            next_input_index += 1

        remap[spec.map_key] = (int(input_idx), int(spec.input_stream_index))

    return next_input_index, remap


class EncodeMultisourceSyncService:
    def __init__(self, callbacks: EncodeMultisourceSyncCallbacks) -> None:
        self._cb = callbacks

    def prepare(
        self,
        *,
        config: EncodeConfig,
        all_sources: list[Path],
        sync_base_input_idx: int,
        work_dir: Path,
        signals: TaskSignals | None = None,
        allow_live: bool = True,
        plan: EncodePlan | None = None,
    ) -> tuple[dict[tuple[Path, int, str], tuple[int, int]], list[Path | str], LiveSyncSession | None, bool]:
        cb = self._cb
        encode_plan = plan
        source_idx_local = {p: i for i, p in enumerate(all_sources)}
        if len(source_idx_local) < 2:
            return {}, [], None, False
        cb.log("INFO", "Analyse sync timeline multi-source : pré-scan/remap en cours…")
        if encode_plan is None:
            encode_plan = cb.build_encode_plan(config)
        sync_analysis = encode_plan.sync_analysis
        if not sync_analysis.enabled:
            return {}, [], None, False

        strict_interleave = False
        if sync_analysis.offset_requires_file_fallback:
            strict_interleave = True
            cb.log("INFO", "Décalage sur piste étrangère détecté : sync timeline activé.")
        elif sync_analysis.needs_subtitle_prescan and sync_analysis.probe_remux_config is not None:
            cb.log("INFO", "Pré-scan ffprobe des sous-titres (décision interleave strict)…")
            strict_interleave = cb.decide_strict_interleave_with_prescan(
                sync_analysis.probe_remux_config,
                log_cb=cb.log,
            )
        elif sync_analysis.strict_interleave_without_prescan:
            strict_interleave = True
            cb.log(
                "WARNING",
                "Pré-scan sous-titres indisponible en mode copy_subtitles ; "
                "activation sync timeline par sécurité.",
            )

        if not strict_interleave:
            return {}, [], None, False

        allow_live_sync = bool(allow_live)
        if allow_live_sync and not sync_analysis.allow_live_sync:
            allow_live_sync = False
            cb.log(
                "INFO",
                "Décalage sur piste étrangère détecté : sync live désactivé, fallback fichier forcé.",
            )

        sync_sources = [SourceInput(path=p, file_index=i, tracks=[]) for i, p in enumerate(all_sources)]
        syncer = cb.syncer_factory(
            ffmpeg_bin=cb.ffmpeg_bin,
            ffmpeg_thread_args=cb.ffmpeg_thread_args(),
            log_cb=lambda msg: cb.log("INFO", msg),
        )

        cancel_cb = signals._cancel_event.is_set if signals is not None else None
        ram_dir: Path | None = None
        if cb.ram_buffer_enabled:
            ram_dir = cb.ram_buffer_dir()

        prepared_result = cb.fallback_helper_factory(
            syncer=syncer,
            work_dir=work_dir,
            ram_dir=ram_dir,
            log_cb=lambda msg: cb.log("INFO", msg),
        ).prepare(
            mapped_tracks=list(sync_analysis.mapped_tracks),
            sources=sync_sources,
            base_input_idx=sync_base_input_idx,
            allow_live=allow_live_sync,
            cancel_cb=cancel_cb,
        )
        prepared = prepared_result.prepared_inputs
        live_session = prepared_result.live_session

        sync_inputs: list[Path | str] = [item.path for item in prepared]
        remap: dict[tuple[Path, int, str], tuple[int, int]] = {}
        path_by_source_idx = {idx: path for path, idx in source_idx_local.items()}
        for item in prepared:
            src_file_idx, src_stream_idx, track_type = item.key
            src_path = path_by_source_idx.get(src_file_idx)
            if src_path is None:
                continue
            remap[(src_path, int(src_stream_idx), track_type)] = (int(item.input_idx), 0)

        return remap, sync_inputs, live_session, True


def append_strict_interleave_mux_flags(cmd: list[str]) -> None:
    _common_append_strict_interleave_mux_flags(cmd)


def append_sync_inputs(cmd: list[str], sync_inputs: list[Path | str]) -> None:
    _common_append_sync_inputs(cmd, sync_inputs)
