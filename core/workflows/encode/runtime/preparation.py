"""Encode workflow preparation/orchestration runner."""

from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from core.runner import TaskCancelledError, TaskSignals
from core.workflows.encode.models import EncodeConfig, EncodeError
from core.workflows.encode.mux_backend import EncodeMuxDecision
from core.workflows.encode.planning.plan_models import EncodePlan


@dataclass(frozen=True)
class EncodePreparationRunnerCallbacks:
    run_with_preparation: Callable[..., TaskSignals]
    validate_config: Callable[[EncodeConfig], list[str]]
    check_cancelled: Callable[[TaskSignals | None], None]
    log_workflow_type: Callable[[str], None]
    log_step: Callable[[int, str], None]
    log: Callable[[str, str], None]
    prepare_attachment_config: Callable[..., tuple[EncodeConfig, Path | None]]
    prepare_process_work_dir: Callable[..., Path]
    relocate_tmdb_covers_to_process_dir: Callable[..., list[Path]]
    download_tmdb_cover: Callable[..., Path]
    is_multi_video: Callable[[EncodeConfig], bool]
    normalize_dynamic_hdr_multi: Callable[[EncodeConfig], EncodeConfig]
    normalize_dynamic_hdr_config: Callable[[EncodeConfig], EncodeConfig]
    is_video_passthrough: Callable[[EncodeConfig], bool]
    wants_dynamic_hdr_copy: Callable[[EncodeConfig], bool]
    wants_dovi_profile_normalization: Callable[[EncodeConfig], bool]
    needs_metadata_inject: Callable[[EncodeConfig], bool]
    ensure_inject_storage_available: Callable[[EncodeConfig], None]
    build_encode_plan: Callable[[EncodeConfig], EncodePlan]
    bind_output_hooks: Callable[..., None]
    run_multi_video_pipeline: Callable[..., TaskSignals]
    run_with_metadata_inject: Callable[..., TaskSignals]
    run_direct_output: Callable[..., TaskSignals]
    #: Sélection du backend de muxage final (lot 2). None → FFmpeg historique.
    select_mux_backend: Callable[[EncodeConfig], EncodeMuxDecision] | None = None


class EncodePreparationRunner:
    """Coordinates workspace preparation before delegating to encode runtimes."""

    def __init__(self, callbacks: EncodePreparationRunnerCallbacks) -> None:
        self._cb = callbacks

    def run_async_preparation(self, config: EncodeConfig) -> TaskSignals:
        signals = TaskSignals()
        executor = ThreadPoolExecutor(max_workers=1)
        active_inner: dict[str, TaskSignals | None] = {"signals": None}
        original_cancel = signals.cancel

        def _cancel() -> None:
            original_cancel()
            inner = active_inner.get("signals")
            if inner is not None and inner is not signals:
                inner.cancel()

        signals.cancel = _cancel  # type: ignore[method-assign]

        def _task() -> None:
            try:
                if signals._cancel_event.is_set():
                    raise TaskCancelledError()

                inner = self._cb.run_with_preparation(
                    config,
                    validate=False,
                    prep_signals=signals,
                )
                active_inner["signals"] = inner

                if inner is not signals:
                    inner.progress.connect(signals.progress.emit)
                    inner.finished.connect(signals.finished.emit)
                    inner.failed.connect(signals.failed.emit)
                    inner.cancelled.connect(signals.cancelled.emit)

                if signals._cancel_event.is_set() and inner is not signals:
                    inner.cancel()
            except TaskCancelledError:
                signals.cancelled.emit()
            except Exception as exc:
                signals.failed.emit(str(exc), exc)

        executor.submit(_task)
        executor.shutdown(wait=False)
        return signals

    def run_with_preparation(
        self,
        config: EncodeConfig,
        *,
        validate: bool,
        prep_signals: TaskSignals | None = None,
    ) -> TaskSignals:
        cb = self._cb
        cb.check_cancelled(prep_signals)
        if validate:
            errors = cb.validate_config(config)
            if errors:
                raise EncodeError("\n".join(errors))
        cb.check_cancelled(prep_signals)

        cb.log_workflow_type("ENCODE")
        cb.log_step(1, "Validation configuration")
        cb.log("INFO", f"Encodage → {config.output.name}")

        cb.log_step(2, "Préparation workspace et attachments")
        cb.check_cancelled(prep_signals)
        work_root = config.work_dir or Path(tempfile.gettempdir())
        process_work_dir = cb.prepare_process_work_dir(
            work_root,
            output_path=config.output,
            fallback_name="encode_job",
        )
        cb.check_cancelled(prep_signals)
        relocated_attachments = cb.relocate_tmdb_covers_to_process_dir(
            [Path(p) for p in config.extra_attachments],
            work_root=work_root,
            process_dir=process_work_dir,
        )
        cb.check_cancelled(prep_signals)

        if config.tmdb_cover is not None:
            tmdb_url, tmdb_filename = config.tmdb_cover
            try:
                cb.check_cancelled(prep_signals)
                cb.log("INFO", f"Téléchargement cover TMDB : {tmdb_filename}")
                cover_path = cb.download_tmdb_cover(
                    tmdb_url,
                    tmdb_filename,
                    process_work_dir / "attachments",
                )
                relocated_attachments = [*relocated_attachments, cover_path]
            except Exception as exc:
                cb.log("WARN", f"Impossible de télécharger la cover TMDB : {exc}")
        cb.check_cancelled(prep_signals)

        prepared_config = replace(
            config,
            work_dir=process_work_dir,
            extra_attachments=relocated_attachments,
        )
        prepared_config, cleanup_dir = cb.prepare_attachment_config(
            prepared_config,
            work_dir=process_work_dir,
            signals=prep_signals,
        )
        cb.check_cancelled(prep_signals)

        cleanup_paths: list[Path] = []
        if cleanup_dir is not None:
            cleanup_paths.append(cleanup_dir)
        relocated_attachment_dir = process_work_dir / "attachments"
        if relocated_attachment_dir.exists():
            cleanup_paths.append(relocated_attachment_dir)
        cleanup_paths.append(process_work_dir)

        cb.log_step(3, "Normalisation des options HDR dynamiques")
        cb.check_cancelled(prep_signals)
        if cb.is_multi_video(prepared_config):
            prepared_config = cb.normalize_dynamic_hdr_multi(prepared_config)
        elif (
            not cb.is_video_passthrough(prepared_config)
            or cb.wants_dovi_profile_normalization(prepared_config)
        ):
            prepared_config = cb.normalize_dynamic_hdr_config(prepared_config)
        elif cb.wants_dynamic_hdr_copy(prepared_config):
            cb.log(
                "INFO",
                "Codec COPY : injection DoVi/HDR10+ ignorée — "
                "métadonnées préservées par passthrough ffmpeg.",
            )

        # Décision de backend de muxage final : prise au préflight, journalisée
        # avant toute écriture. Aucun repli après le démarrage effectif.
        cb.check_cancelled(prep_signals)
        mux_decision = (
            cb.select_mux_backend(prepared_config)
            if cb.select_mux_backend is not None
            else None
        )
        native_mux = mux_decision is not None and mux_decision.selected == "native"
        if mux_decision is not None:
            cb.log(
                "INFO",
                f"MUX_BACKEND requested={mux_decision.requested} "
                f"selected={mux_decision.selected} plan_version=1 "
                f"pipeline={mux_decision.pipeline}",
            )
            diagnostics = mux_decision.diagnostics
            if native_mux and diagnostics:
                # Mode strict : incompatibilité signalée avant l'encodage lourd.
                raise EncodeError("\n".join(
                    f"Backend natif indisponible : {reason}." for reason in diagnostics
                ))
            if mux_decision.uses_fallback:
                cb.log(
                    "WARN",
                    "Backend natif non applicable ; repli FFmpeg : " + "; ".join(diagnostics),
                )

        cb.check_cancelled(prep_signals)
        if cb.is_multi_video(prepared_config):
            cb.log_step(4, "Routage du workflow (pipeline multi-pistes vidéo)")
            plan = cb.build_encode_plan(prepared_config)
            if prep_signals is not None:
                cb.bind_output_hooks(
                    prep_signals,
                    output=prepared_config.output,
                    cleanup_paths=cleanup_paths,
                    include_nfo=native_mux,
                )
            signals = cb.run_multi_video_pipeline(
                prepared_config,
                cleanup_paths,
                prep_signals=prep_signals,
                plan=plan,
                mux_decision=mux_decision,
            )
            if prep_signals is None or signals is not prep_signals:
                cb.bind_output_hooks(
                    signals,
                    output=prepared_config.output,
                    cleanup_paths=cleanup_paths,
                    include_nfo=native_mux,
                )
            return signals

        cb.check_cancelled(prep_signals)
        needs_inject = cb.needs_metadata_inject(prepared_config)

        if (
            native_mux
            and not needs_inject
            and getattr(mux_decision, "pipeline", "") == "ffmpeg_direct"
        ):
            # Encode direct explicitement découpé en encode puis assemblage :
            # le pipeline multi-pistes produit les MKV vidéo intermédiaires,
            # l'assembleur natif matérialise l'audio et écrit la sortie.
            cb.log_step(4, "Routage du workflow (encode découpé puis assemblage natif)")
            plan = cb.build_encode_plan(prepared_config)
            if prep_signals is not None:
                cb.bind_output_hooks(
                    prep_signals,
                    output=prepared_config.output,
                    cleanup_paths=cleanup_paths,
                    include_nfo=True,
                )
            signals = cb.run_multi_video_pipeline(
                prepared_config,
                cleanup_paths,
                prep_signals=prep_signals,
                plan=plan,
                mux_decision=mux_decision,
            )
            if prep_signals is None or signals is not prep_signals:
                cb.bind_output_hooks(
                    signals,
                    output=prepared_config.output,
                    cleanup_paths=cleanup_paths,
                    include_nfo=True,
                )
            return signals

        cb.log_step(
            4,
            "Routage du workflow (sortie directe ou injection metadata)"
            + (" -> injection" if needs_inject else " -> sortie directe"),
        )
        cb.check_cancelled(prep_signals)
        if needs_inject:
            cb.log(
                "INFO",
                (
                    "Injection DoVi/HDR10+: pipeline fichier (pas de pipe direct outillage)."
                    if cb.wants_dynamic_hdr_copy(prepared_config)
                    else "Injection HDR statique: pipeline fichier (codec sans support natif fiable)."
                ),
            )
            cb.ensure_inject_storage_available(prepared_config)
        cb.check_cancelled(prep_signals)

        if prep_signals is not None:
            cb.bind_output_hooks(
                prep_signals,
                output=prepared_config.output,
                cleanup_paths=cleanup_paths,
                include_nfo=native_mux,
            )

        plan = cb.build_encode_plan(prepared_config)
        signals = (
            cb.run_with_metadata_inject(
                prepared_config,
                prep_signals=prep_signals,
                plan=plan,
                mux_decision=mux_decision,
            )
            if needs_inject
            else cb.run_direct_output(
                prepared_config,
                cleanup_paths,
                prep_signals=prep_signals,
                plan=plan,
                mux_decision=mux_decision,
            )
        )
        if prep_signals is None or signals is not prep_signals:
            cb.bind_output_hooks(
                signals,
                output=prepared_config.output,
                cleanup_paths=cleanup_paths,
                include_nfo=native_mux,
            )
        return signals
