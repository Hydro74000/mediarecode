from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.runner import TaskSignals
from core.workdir import remove_path
from core.workflows.remux_timeline_sync import LiveSyncSession


@dataclass(frozen=True)
class SignalBindingServiceCallbacks:
    write_nfo: Callable[[Path], None]
    remove_path: Callable[[Path], None] = remove_path


class SignalBindingService:
    """Centralise les bindings de nettoyage et de génération NFO."""

    def __init__(self, callbacks: SignalBindingServiceCallbacks) -> None:
        self._callbacks = callbacks

    def bind_live_sync_cleanup(
        self,
        signals: TaskSignals,
        session: LiveSyncSession | None,
    ) -> None:
        if session is None:
            return

        done = {"closed": False}
        for proc in session.processes:
            signals._register_proc(proc)

        def _cleanup(*_args) -> None:
            if done["closed"]:
                return
            done["closed"] = True
            for proc in session.processes:
                signals._unregister_proc(proc)
            session.close()

        cleanup = signals.retain_callback(_cleanup)
        signals.finished.connect(cleanup)
        signals.failed.connect(cleanup)
        signals.cancelled.connect(cleanup)

    def bind_temp_cleanup(self, signals: TaskSignals, cleanup_paths: list[Path]) -> None:
        if not cleanup_paths:
            return

        done = {"cleaned": False}

        def _cleanup(*_args) -> None:
            if done["cleaned"]:
                return
            done["cleaned"] = True
            for path in cleanup_paths:
                try:
                    self._callbacks.remove_path(path)
                except OSError:
                    pass

        cleanup = signals.retain_callback(_cleanup)
        signals.finished.connect(cleanup)
        signals.failed.connect(cleanup)
        signals.cancelled.connect(cleanup)

    def bind_nfo_write(self, signals: TaskSignals, output: Path) -> None:
        def _write(*_args) -> None:
            self._callbacks.write_nfo(output)

        signals.finished.connect(signals.retain_callback(_write))

    def bind_output_hooks(
        self,
        signals: TaskSignals,
        *,
        output: Path,
        cleanup_paths: list[Path] | None = None,
        include_temp_cleanup: bool = True,
        include_nfo: bool = True,
    ) -> None:
        if include_temp_cleanup and cleanup_paths is not None:
            self.bind_temp_cleanup(signals, cleanup_paths)
        if include_nfo:
            self.bind_nfo_write(signals, output)
