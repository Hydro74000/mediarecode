"""Remux workflow runtime helpers for the CLI."""

from __future__ import annotations

import threading

from PySide6.QtCore import QEventLoop

from core.config import AppConfig
from core.workflows.remux import RemuxWorkflow
from core.workflows.remux_models import RemuxConfig

from cli.constants import EXIT_EXISTS, EXIT_OK, EXIT_VALIDATION, EXIT_WORKFLOW
from cli.errors import CliError
from cli.logging import Logger
from cli.options import CommonOptions


def workflow(config: AppConfig, options: CommonOptions, logger: Logger) -> RemuxWorkflow:
    return RemuxWorkflow(
        ffmpeg_bin=str(options.ffmpeg or config.tool_ffmpeg),
        ffprobe_bin=str(options.ffprobe or config.tool_ffprobe),
        ffmpeg_threads=options.threads if options.threads is not None else config.ffmpeg_threads,
        writing_application=str(options.writing_application or ""),
        generate_nfo=config.generate_nfo if options.nfo is None else bool(options.nfo),
        mediainfo_bin=str(options.mediainfo or config.tool_mediainfo),
    )


def preview_remux_config(config: AppConfig, options: CommonOptions, logger: Logger, remux_config: RemuxConfig) -> int:
    errors = workflow(config, options, logger).validate(remux_config)
    if errors:
        for error in errors:
            logger.emit("error", error)
        return EXIT_VALIDATION
    print(workflow(config, options, logger).preview_command(remux_config))
    return EXIT_OK


def run_remux_config(
    config: AppConfig,
    options: CommonOptions,
    logger: Logger,
    remux_config: RemuxConfig,
    *,
    force: bool = False,
) -> int:
    if remux_config.output.exists() and not force:
        raise CliError(f"Sortie déjà existante : {remux_config.output} (utiliser --force)", EXIT_EXISTS)
    wf = workflow(config, options, logger)
    wf.log_message.connect(logger.workflow_log)
    wf.backend_event.connect(
        lambda report: logger.emit(
            "info", "Sélection du backend de remuxage.",
            event="mux_backend", **dict(report),
        )
    )
    signals = wf.run(remux_config)
    loop = QEventLoop()
    state_exit = {"value": EXIT_OK}

    def done(message: str = "") -> None:
        if message:
            logger.emit("info", message)
        state_exit["value"] = EXIT_OK
        loop.quit()

    def failed(message: str, exc: object) -> None:
        logger.emit("error", message, exception=repr(exc))
        state_exit["value"] = EXIT_WORKFLOW
        loop.quit()

    def cancelled() -> None:
        logger.emit("error", "Opération annulée.")
        state_exit["value"] = EXIT_WORKFLOW
        loop.quit()

    if options.verbose:
        signals.progress.connect(lambda line: logger.emit("info", line))
    signals.finished.connect(done)
    signals.failed.connect(failed)
    signals.cancelled.connect(cancelled)

    stop = threading.Event()

    def _watch_stdin() -> None:
        # Reserved for future cancellation while keeping the CLI non-interactive.
        stop.wait()

    watcher = threading.Thread(target=_watch_stdin, daemon=True)
    watcher.start()
    try:
        loop.exec()
    finally:
        stop.set()
    return state_exit["value"]
