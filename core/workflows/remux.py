"""Thin remux facade; runtime, mapping, sync and validation live in sibling modules."""

from __future__ import annotations

from typing import Callable, cast
import subprocess
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from core.runner import TaskSignals, ToolRunner
from core.version import APP_VERSION_LABEL
from core.workflows.common.matroska_finalize import MatroskaMuxingAppPostAction
from core.workflows.common.matroska_finalize import MatroskaLanguagePostAction
from core.workflows.common.chapters import (
    probe_media_duration_seconds,
    write_ffmetadata_chapters,
)
from core.workflows.common.ffmpeg_runtime import (
    cli_path as _cli_path,
    ffmpeg_progress_args,
    ffmpeg_thread_args as _common_ffmpeg_thread_args,
    normalize_ffmpeg_thread_count as _normalize_ffmpeg_thread_count,
)
from core.workflows.common.timeline_sync import needs_strict_interleave as _common_needs_strict_interleave
from core.workflows.remux_attachments import (
    write_mediainfo_nfo as _write_mediainfo_nfo_helper,
)
from core.workflows.remux_command import (
    build_remux_command as _build_remux_command_helper,
    preview_remux_command as _preview_remux_command_helper,
)
from core.workflows.remux_mapping import (
    is_dir_writable as _is_dir_writable_helper,
    MappedTrack as _MappedTrack,
    resolve_mapped_tracks as _resolve_mapped_tracks_helper,
    track_order_parts as _track_order_parts_helper,
)
from core.workflows.remux_models import (
    RemuxConfig,
    RemuxError,
)
from core.workflows.remux_backend import (
    FfmpegRemuxBackend, NativeMatroskaBackend,
)
from core.workflows.remux_plan import (
    MuxExecutionPlan,
    mux_backend_report_from_plan,
    plan_remux as _plan_remux,
)
from core.workflows.remux_sync import (
    bind_temp_cleanup as _bind_temp_cleanup_helper,
    decide_strict_interleave_with_prescan as _decide_strict_interleave_with_prescan_helper,
)
from core.workflows.remux_validation import validate_remux_config as _validate_remux_config
from core.workflows.remux_runtime import (
    RemuxRuntimeRunner,
    RemuxRuntimeRunnerCallbacks,
)

def write_mediainfo_nfo(
    output_path: Path,
    log_cb: Callable[[str, str], None],
    mediainfo_bin: str = "mediainfo",
) -> None:
    """Génère un fichier .nfo (même nom que le MKV) avec la sortie brute de mediainfo."""
    _write_mediainfo_nfo_helper(
        output_path,
        log_cb=log_cb,
        mediainfo_bin=mediainfo_bin,
        run_cmd=subprocess.run,
    )


class RemuxWorkflow(QObject):
    """
    Remux MKV router.

    Business logic belongs to `remux_runtime`, `remux_command`, `remux_mapping`,
    `remux_sync`, `remux_attachments` and `remux_validation`; private methods
    here are compatibility shims for existing tests/runtime call sites.
    """

    log_message = Signal(str, str)
    backend_event = Signal(object)

    def __init__(
        self,
        ffmpeg_bin: str = "ffmpeg",
        ffprobe_bin: str = "ffprobe",
        ffmpeg_threads: int | None = None,
        parent: QObject | None = None,
        *,
        writing_application: str = "",
        generate_nfo: bool = True,
        mediainfo_bin: str = "mediainfo",
        sync_rewrite_enabled: bool = False,
        sync_advanced_audio_rewrite_enabled: bool = False,
        aac_bitrate_per_channel_kbps: int = 96,
        eac3_bitrate_per_channel_kbps: int = 96,
    ) -> None:
        super().__init__(parent)
        self._ffmpeg = ffmpeg_bin
        self._ffprobe = ffprobe_bin
        self._ffmpeg_threads = _normalize_ffmpeg_thread_count(ffmpeg_threads)
        self._generate_nfo = generate_nfo
        self._mediainfo_bin = mediainfo_bin
        self._sync_rewrite_enabled = bool(sync_rewrite_enabled)
        self._sync_advanced_audio_rewrite_enabled = bool(sync_advanced_audio_rewrite_enabled)
        self._sync_rewrite_audio_bitrates = {
            "aac": int(aac_bitrate_per_channel_kbps or 96),
            "ac3": int(eac3_bitrate_per_channel_kbps or 96),
            "eac3": int(eac3_bitrate_per_channel_kbps or 96),
        }
        self._runner = ToolRunner(max_workers=1, parent=self)
        self._writing_application = writing_application.strip()
        self._muxing_post_action = MatroskaMuxingAppPostAction(
            app_prefix=MatroskaMuxingAppPostAction.default_prefix(APP_VERSION_LABEL),
            log_cb=self.log_message.emit,
        )
        self._language_post_action = MatroskaLanguagePostAction(
            log_cb=self.log_message.emit,
        )

    def set_ffmpeg_bin(self, ffmpeg_bin: str) -> None:
        self._ffmpeg = ffmpeg_bin

    def set_ffprobe_bin(self, ffprobe_bin: str) -> None:
        self._ffprobe = ffprobe_bin

    def set_ffmpeg_threads(self, ffmpeg_threads: int | None) -> None:
        self._ffmpeg_threads = _normalize_ffmpeg_thread_count(ffmpeg_threads)

    def set_writing_application(self, writing_application: str) -> None:
        self._writing_application = writing_application.strip()

    def set_generate_nfo(self, generate_nfo: bool) -> None:
        self._generate_nfo = generate_nfo

    def set_mediainfo_bin(self, mediainfo_bin: str) -> None:
        self._mediainfo_bin = mediainfo_bin

    def set_sync_rewrite_enabled(self, enabled: bool) -> None:
        self._sync_rewrite_enabled = bool(enabled)

    def set_sync_advanced_audio_rewrite_enabled(self, enabled: bool) -> None:
        self._sync_advanced_audio_rewrite_enabled = bool(enabled)

    def set_sync_rewrite_audio_bitrates(
        self,
        *,
        aac_bitrate_per_channel_kbps: int,
        eac3_bitrate_per_channel_kbps: int,
    ) -> None:
        self._sync_rewrite_audio_bitrates = {
            "aac": int(aac_bitrate_per_channel_kbps or 96),
            "ac3": int(eac3_bitrate_per_channel_kbps or 96),
            "eac3": int(eac3_bitrate_per_channel_kbps or 96),
        }

    def _ffmpeg_thread_args(self) -> list[str]:
        return _common_ffmpeg_thread_args(self._ffmpeg_threads)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def _absolute_paths_config(config: RemuxConfig) -> RemuxConfig:
        """Config aux chemins résolus en absolu.

        Filet de sécurité pour tous les producteurs (UI, profils, appels
        directs) : les runners exécutent FFmpeg depuis le workspace temporaire
        et les post-actions résolvent le candidat depuis le cwd du processus —
        un chemin relatif y serait créé puis cherché à deux endroits différents.
        """
        from dataclasses import replace

        def _abs(path: Path) -> Path:
            return Path(path).expanduser().resolve()

        return replace(
            config,
            sources=[replace(source, path=_abs(source.path)) for source in config.sources],
            output=_abs(config.output),
            work_dir=_abs(config.work_dir) if config.work_dir else config.work_dir,
            extra_attachments=[_abs(Path(item)) for item in config.extra_attachments],
        )

    def compile_plan(self, config: RemuxConfig) -> MuxExecutionPlan:
        """Compile le plan d'exécution unique consommé par validation/preview/run."""
        config = self._absolute_paths_config(config)
        return _plan_remux(config, ffmpeg_bin=self._ffmpeg, ffprobe_bin=self._ffprobe)

    def validate(self, config: RemuxConfig, *, plan: MuxExecutionPlan | None = None) -> list[str]:
        return _validate_remux_config(
            config,
            track_order_parts=self._track_order_parts,
            dir_writable=self._is_dir_writable,
            plan=plan if plan is not None else self.compile_plan(config),
            tool_paths={"ffmpeg": self._ffmpeg, "ffprobe": self._ffprobe},
        )

    @staticmethod
    def _is_dir_writable(path: Path) -> bool:
        return _is_dir_writable_helper(path)

    # ------------------------------------------------------------------
    # Construction de commande
    # ------------------------------------------------------------------

    def build_command(
        self,
        config: RemuxConfig,
        *,
        sync_inputs: list[Path | str] | None = None,
        sync_input_formats: list[str] | None = None,
        extra_inputs: list[Path | str] | None = None,
        chapter_input_index: int | None = None,
        strict_interleave_override: bool | None = None,
        mapped_tracks_override: list[_MappedTrack] | None = None,
    ) -> list[str]:
        return _build_remux_command_helper(
            config,
            ffmpeg_bin=self._ffmpeg,
            ffmpeg_progress_args=ffmpeg_progress_args(),
            ffmpeg_thread_args=self._ffmpeg_thread_args(),
            cli_path=_cli_path,
            sync_inputs=sync_inputs,
            sync_input_formats=sync_input_formats,
            extra_inputs=extra_inputs,
            chapter_input_index=chapter_input_index,
            strict_interleave_override=strict_interleave_override,
            mapped_tracks_override=mapped_tracks_override,
            resolve_mapped_tracks_fn=_resolve_mapped_tracks_helper,
            needs_strict_interleave_fn=_common_needs_strict_interleave,
        )

    def preview_command(self, config: RemuxConfig) -> str:
        reference = _preview_remux_command_helper(config, build_command=self.build_command)
        report = self.backend_report(config)
        if report["selected_backend"] == "ffmpeg":
            return reference
        preparations = cast(
            list[list[object]],
            report.get("preparation_commands") or [],
        )
        prep_text = "\n".join(" ".join(map(str, command)) for command in preparations)
        sections = [
            f"# Backend: native Matroska (plan v{report['plan_version']})",
            prep_text,
            f"# Écriture interne Matroska -> {config.output}",
            "# Référence FFmpeg compatible v1:",
            reference,
        ]
        return "\n".join(section for section in sections if section)

    def backend_report(self, config: RemuxConfig) -> dict[str, object]:
        return mux_backend_report_from_plan(self.compile_plan(config))

    def execution_preview(self, config: RemuxConfig) -> dict[str, object]:
        report = self.backend_report(config)
        if report["selected_backend"] == "ffmpeg":
            return {
                **report,
                "action": "external_ffmpeg",
                "command": self.build_command(config),
            }
        return {
            **report,
            "action": "internal_matroska_write",
            "output": str(config.output),
        }

    # ------------------------------------------------------------------
    # Exécution
    # ------------------------------------------------------------------

    def _log_workflow_type(self, workflow_kind: str) -> None:
        self.log_message.emit("INFO", f"WORKFLOW TYPE - {workflow_kind}")

    def _log_step(self, step_index: int, step_name: str) -> None:
        self.log_message.emit("INFO", f"STEP {step_index} - {step_name}")

    def run(self, config: RemuxConfig) -> TaskSignals:
        # Chemins absolus AVANT tout : le plan, la validation et le backend
        # doivent consommer exactement les mêmes chemins que l'exécution.
        config = self._absolute_paths_config(config)
        # Plan compilé une seule fois : validation, rapport et exécution
        # consomment le même objet. Toute erreur lève avant thread, fichier
        # partiel ou téléchargement.
        plan = self.compile_plan(config)
        report = mux_backend_report_from_plan(plan)
        self.backend_event.emit(report)
        self.log_message.emit(
            "INFO",
            f"MUX_BACKEND requested={plan.requested_backend} selected={plan.selected_backend} plan_version=1",
        )
        errors = self.validate(config, plan=plan)
        if errors:
            raise RemuxError("\n".join(errors))
        if plan.fallback:
            self.log_message.emit(
                "WARN",
                "Backend natif non applicable ; repli FFmpeg : " + "; ".join(plan.native_diagnostics),
            )
        native_backend = NativeMatroskaBackend(
            log=self.log_message.emit, log_step=self._log_step,
            ffmpeg_bin=self._ffmpeg, ffprobe_bin=self._ffprobe,
            finalize=self._write_nfo, plan=plan,
        )
        ffmpeg_backend = FfmpegRemuxBackend(
            execute_callback=lambda item: self._run_ffmpeg(item, plan),
            preview_callback=lambda item: _preview_remux_command_helper(item, build_command=self.build_command),
            command_callback=self.build_command,
        )
        backend = native_backend if plan.selected_backend == "native" else ffmpeg_backend
        return backend.execute(config)

    def _run_ffmpeg(self, config: RemuxConfig, plan: MuxExecutionPlan) -> TaskSignals:
        return RemuxRuntimeRunner(
            RemuxRuntimeRunnerCallbacks(
                ffmpeg_bin=self._ffmpeg,
                ffprobe_bin=self._ffprobe,
                ffmpeg_thread_args=self._ffmpeg_thread_args,
                build_command=self.build_command,
                log_workflow_type=self._log_workflow_type,
                log_step=self._log_step,
                log=self.log_message.emit,
                bind_temp_cleanup=self._bind_temp_cleanup,
                run_cmd=lambda cmd, cwd, label, progress_cb, signals: self._runner._run_cmd(
                    cmd,
                    cwd=cwd,
                    label=label,
                    progress_cb=progress_cb,
                    signals=signals,
                ),
                apply_muxing_post_action=self._muxing_post_action.apply_if_mkv,
                apply_language_post_action=self._language_post_action.apply_if_mkv,
                write_nfo=self._write_nfo,
                sync_rewrite_enabled=lambda: self._sync_rewrite_enabled,
                sync_advanced_audio_rewrite_enabled=lambda: self._sync_advanced_audio_rewrite_enabled,
                sync_rewrite_audio_bitrates=lambda: dict(self._sync_rewrite_audio_bitrates),
            )
        ).run(config, plan)

    def _write_nfo(self, output_path: Path) -> None:
        if self._generate_nfo:
            write_mediainfo_nfo(output_path, log_cb=self.log_message.emit, mediainfo_bin=self._mediainfo_bin)

    def _bind_temp_cleanup(self, signals: TaskSignals, cleanup_paths: list[Path]) -> None:
        """Supprime les dossiers temporaires du workflow quand le traitement se termine."""
        _bind_temp_cleanup_helper(signals, cleanup_paths)

    @staticmethod
    def _track_order_parts(
        item: tuple[int, int] | tuple[int, int, str],
    ) -> tuple[int, int, str | None]:
        return _track_order_parts_helper(item)

    def _decide_strict_interleave_with_prescan(self, config: RemuxConfig) -> bool:
        return _decide_strict_interleave_with_prescan_helper(
            config,
            resolve_mapped_tracks=_resolve_mapped_tracks_helper,
            log_cb=self.log_message.emit,
        )

    # ------------------------------------------------------------------
    # Helpers: chapitres (contrat interne encode -> remux)
    # ------------------------------------------------------------------

    def _probe_duration_seconds(self, source: Path) -> float | None:
        return probe_media_duration_seconds(self._ffprobe, source)

    def _write_ffmetadata_chapters(
        self,
        entries: list,
        out_dir: Path,
        duration_s: float | None,
    ) -> Path:
        return write_ffmetadata_chapters(entries, out_dir, duration_s)
