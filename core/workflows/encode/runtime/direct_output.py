from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.runner import TaskCancelledError, TaskSignals
from core.workflows.encode.models import EncodeConfig
from core.workflows.encode.planning.plan_models import EncodePlan
from core.workflows.remux_timeline_sync import LiveSyncSession


@dataclass(frozen=True)
class DirectOutputRunnerCallbacks:
    check_cancelled: Callable[[TaskSignals | None], None]
    log_step: Callable[[int, str], None]
    log_info: Callable[[str], None]
    uses_two_pass: Callable[[EncodeConfig], bool]
    build_encode_plan: Callable[[EncodeConfig], EncodePlan]
    build_runtime_two_pass_with_sync: Callable[
        ...,
        tuple[list[list[str]], LiveSyncSession | None, list[Path]],
    ]
    build_runtime_single_pass_with_sync: Callable[
        ...,
        tuple[list[str], LiveSyncSession | None, list[Path]],
    ]
    run_two_pass: Callable[[list[list[str]], Path | None, TaskSignals | None], TaskSignals]
    run_cmd: Callable[[list[str], Path | None, str, Callable[[str], None], TaskSignals], str]
    run_tool: Callable[[list[str], Path | None, str], TaskSignals]
    finalize_ffmpeg: Callable[..., str]
    bind_live_sync_cleanup: Callable[[TaskSignals, LiveSyncSession | None], None]
    cleanup_two_pass_logs: Callable[[Path | None], None]
    before_async_start: Callable[[TaskSignals], None] = lambda _signals: None


class DirectOutputRunner:
    """Runner dédié au pipeline encode en sortie directe."""

    def __init__(self, callbacks: DirectOutputRunnerCallbacks) -> None:
        self._callbacks = callbacks

    def run(
        self,
        *,
        config: EncodeConfig,
        cleanup_paths: list[Path],
        cwd: Path,
        prep_signals: TaskSignals | None = None,
        plan: EncodePlan | None = None,
    ) -> TaskSignals:
        cb = self._callbacks
        cb.check_cancelled(prep_signals)
        cb.log_step(5, "Construction de la commande ffmpeg (sortie directe)")
        encode_plan = plan or cb.build_encode_plan(config)

        if len(encode_plan.all_sources) > 1 or prep_signals is None:
            return self.run_multisource_async(
                config=config,
                cleanup_paths=cleanup_paths,
                cwd=cwd,
                prep_signals=prep_signals,
                plan=encode_plan,
            )

        chapter_dir: Path | None = None
        if config.chapter_overrides:
            chapter_dir = Path(
                tempfile.mkdtemp(
                    prefix="enc_chapters_",
                    dir=str(config.work_dir) if config.work_dir else None,
                )
            )
            cleanup_paths.append(chapter_dir)

        cb.check_cancelled(prep_signals)
        if cb.uses_two_pass(config):
            cb.log_step(6, "Préparation sync/remap + commandes ffmpeg (2 passes)")
            cmds: list[list[str]]
            live_sync_session: LiveSyncSession | None = None
            sync_cleanup_paths: list[Path] = []
            try:
                cmds, live_sync_session, sync_cleanup_paths = cb.build_runtime_two_pass_with_sync(
                    config,
                    chapter_materialize_dir=chapter_dir,
                    signals=prep_signals,
                    plan=encode_plan,
                )
                cleanup_paths.extend(sync_cleanup_paths)
                cb.check_cancelled(prep_signals)
                cb.log_step(7, "Exécution ffmpeg en 2 passes (sortie directe)")
                cb.log_info("Passe 1/2 (analyse)…")
                cb.run_cmd(
                    cmds[0], cwd, "ffmpeg-pass1",
                    lambda line: prep_signals.progress.emit(line), prep_signals,
                )
                cb.check_cancelled(prep_signals)
                cb.log_info("Passe 2/2 (encodage)…")
                output = cb.finalize_ffmpeg(
                    config, cmds[1], cwd, "ffmpeg-pass2", prep_signals,
                    plan=encode_plan,
                )
                prep_signals.finished.emit(output)
                signals = prep_signals
            except Exception:
                if live_sync_session is not None:
                    live_sync_session.close()
                raise
            cb.bind_live_sync_cleanup(signals, live_sync_session)
            return signals

        cb.log_step(6, "Préparation sync/remap + commande ffmpeg (single pass)")
        cmd: list[str]
        live_sync_session = None
        single_sync_cleanup_paths: list[Path] = []
        try:
            cmd, live_sync_session, single_sync_cleanup_paths = cb.build_runtime_single_pass_with_sync(
                config,
                chapter_materialize_dir=chapter_dir,
                signals=prep_signals,
                plan=encode_plan,
            )
            cleanup_paths.extend(single_sync_cleanup_paths)
            cb.check_cancelled(prep_signals)
            cb.log_step(7, "Exécution ffmpeg en single pass (sortie directe)")
            assert prep_signals is not None
            output = cb.finalize_ffmpeg(
                config, cmd, cwd, "ffmpeg", prep_signals, plan=encode_plan,
            )
            prep_signals.finished.emit(output)
            signals = prep_signals
        except Exception:
            if live_sync_session is not None:
                live_sync_session.close()
            raise
        cb.bind_live_sync_cleanup(signals, live_sync_session)
        return signals

    def run_multisource_async(
        self,
        *,
        config: EncodeConfig,
        cleanup_paths: list[Path],
        cwd: Path,
        prep_signals: TaskSignals | None = None,
        plan: EncodePlan | None = None,
    ) -> TaskSignals:
        cb = self._callbacks
        signals = prep_signals or TaskSignals()
        if prep_signals is None:
            cb.before_async_start(signals)
        executor = ThreadPoolExecutor(max_workers=1)

        def _task() -> None:
            chapter_dir: Path | None = None
            live_sync_session: LiveSyncSession | None = None
            sync_cleanup_paths: list[Path] = []
            is_two_pass = cb.uses_two_pass(config)
            encode_plan = plan or cb.build_encode_plan(config)

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

                if is_two_pass:
                    cb.log_step(6, "Préparation sync/remap + commandes ffmpeg (2 passes)")
                    cmds, live_sync_session, sync_cleanup_paths = cb.build_runtime_two_pass_with_sync(
                        config,
                        chapter_materialize_dir=chapter_dir,
                        signals=signals,
                        plan=encode_plan,
                    )
                    cleanup_paths.extend(sync_cleanup_paths)
                    if live_sync_session is not None:
                        for proc in live_sync_session.processes:
                            signals._register_proc(proc)
                    cb.check_cancelled(signals)
                    cb.log_step(7, "Exécution ffmpeg en 2 passes (sortie directe)")

                    cb.log_info("Passe 1/2 (analyse)…")
                    cb.run_cmd(
                        cmds[0],
                        cwd,
                        "ffmpeg-pass1",
                        lambda line: signals.progress.emit(line),
                        signals,
                    )
                    cb.check_cancelled(signals)
                    cb.log_info("Passe 2/2 (encodage)…")
                    output = cb.finalize_ffmpeg(
                        config, cmds[1], cwd, "ffmpeg-pass2", signals,
                        plan=encode_plan,
                    )
                    signals.finished.emit(output)
                else:
                    cb.log_step(6, "Préparation sync/remap + commande ffmpeg (single pass)")
                    cmd, live_sync_session, sync_cleanup_paths = cb.build_runtime_single_pass_with_sync(
                        config,
                        chapter_materialize_dir=chapter_dir,
                        signals=signals,
                        plan=encode_plan,
                    )
                    cleanup_paths.extend(sync_cleanup_paths)
                    if live_sync_session is not None:
                        for proc in live_sync_session.processes:
                            signals._register_proc(proc)
                    cb.check_cancelled(signals)
                    cb.log_step(7, "Exécution ffmpeg en single pass (sortie directe)")
                    output = cb.finalize_ffmpeg(
                        config, cmd, cwd, "ffmpeg", signals,
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
                if is_two_pass:
                    cb.cleanup_two_pass_logs(cwd)

        executor.submit(_task)
        executor.shutdown(wait=False)
        return signals
