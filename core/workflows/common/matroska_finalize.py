"""Finalisation commune des muxages Matroska produits par FFmpeg."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from core.matroska.contract import MatroskaOutputContract
from core.matroska.editors.language import (
    MatroskaLanguageEditor,
    MatroskaLanguagePatchResult,
)
from core.matroska.editors.segment_info import (
    MatroskaSegmentInfoHeaderEditor,
    MatroskaSegmentInfoHeaderEditorOptions,
    MatroskaSegmentInfoPatchResult,
)
from core.matroska.validation import validate_matroska_output
from core.runner import TaskCancelledError, TaskSignals


PostAction = Callable[[Path], object]
RunCommand = Callable[[list[str], Path | None, str, Callable[[str], None], TaskSignals], str]


class MatroskaLanguagePostAction:
    """Workflow adapter applying Matroska language normalization."""

    def __init__(
        self,
        *,
        editor: MatroskaLanguageEditor | None = None,
        log_cb: Callable[[str, str], None] | None = None,
    ) -> None:
        self._editor = editor or MatroskaLanguageEditor()
        self._log_cb = log_cb

    def apply_if_mkv(
        self,
        output_path: Path,
        *,
        log_cb: Callable[[str, str], None] | None = None,
    ) -> MatroskaLanguagePatchResult | None:
        cb = log_cb or self._log_cb
        name = output_path.name.lower()
        if output_path.suffix.lower() != ".mkv" and not name.endswith(".mkv.partial"):
            return None
        if not output_path.is_file():
            return None

        result = self._editor.apply(output_path)
        if cb is not None:
            if result.applied:
                details = ", ".join(
                    f"'{fix.language_before}'→'{fix.language_after}' / "
                    f"BCP47='{fix.language_bcp47_after}'"
                    for fix in result.fixes
                )
                cb(
                    "INFO",
                    f"Langues Matroska normalisées ({len(result.fixes)} piste(s)): {details}",
                )
            elif result.skipped and result.reason:
                cb("WARN", f"Post-action langues ignorée: {result.reason}")
            elif result.reason:
                cb("INFO", f"Post-action langues: {result.reason}")
        return result


class MatroskaMuxingAppPostAction:
    """Workflow adapter applying the FFmpeg MuxingApp patch."""

    def __init__(
        self,
        *,
        editor: MatroskaSegmentInfoHeaderEditor | None = None,
        app_prefix: str | None = None,
        log_cb: Callable[[str, str], None] | None = None,
    ) -> None:
        self._editor = editor or MatroskaSegmentInfoHeaderEditor(
            options=MatroskaSegmentInfoHeaderEditorOptions(
                edit_muxing_app=True,
                edit_writing_app=False,
                rebuild_on_overflow=True,
                fallback_mode="skip",
            )
        )
        self._app_prefix = app_prefix
        self._log_cb = log_cb

    @staticmethod
    def default_prefix(version_label: str) -> str:
        normalized_version = version_label.removeprefix("v")
        return f"Muxiveo {normalized_version}"

    def apply_if_mkv(
        self,
        output_path: Path,
        *,
        app_prefix: str | None = None,
        log_cb: Callable[[str, str], None] | None = None,
    ) -> MatroskaSegmentInfoPatchResult | None:
        prefix = app_prefix or self._app_prefix
        if prefix is None:
            raise ValueError("app_prefix requis (paramètre ou valeur d'init)")
        cb = log_cb or self._log_cb
        name = output_path.name.lower()
        if output_path.suffix.lower() != ".mkv" and not name.endswith(".mkv.partial"):
            return None
        if not output_path.is_file():
            return None

        result = self._editor.apply_muxing_app_replace_with_header_rebuild(
            output_path,
            app_prefix=prefix,
        )
        if cb is not None:
            if result.applied:
                cb(
                    "INFO",
                    "Segment Info Matroska patché en post-action "
                    f"(MuxingApp: '{result.muxing_app_before}' -> "
                    f"'{result.muxing_app_after}').",
                )
            elif result.skipped:
                cb("WARN", f"Post-action MuxingApp ignorée: {result.reason}")
            elif result.reason:
                cb("INFO", f"Post-action MuxingApp: {result.reason}")
        return result


@dataclass
class MatroskaOutputTransaction:
    output: Path
    contract: MatroskaOutputContract
    ffprobe_bin: str
    run_command: RunCommand
    post_actions: tuple[PostAction, ...] = ()
    write_nfo: Callable[[Path], None] | None = None
    warn: Callable[[str], None] | None = None

    @property
    def candidate(self) -> Path:
        return self.output.with_suffix(self.output.suffix + ".partial")

    def candidate_command(self, command: list[str]) -> list[str]:
        if not command or Path(str(command[-1])) != self.output:
            raise ValueError(
                "Commande de muxage final sans sortie utilisateur en dernière position."
            )
        return [*command[:-1], "-f", "matroska", str(self.candidate)]

    @staticmethod
    def _check_cancelled(signals: TaskSignals) -> None:
        if signals._cancel_event.is_set():
            raise TaskCancelledError()

    def execute(
        self,
        command: list[str],
        *,
        cwd: Path | None,
        label: str,
        signals: TaskSignals,
        extra_post_actions: Iterable[PostAction] = (),
    ) -> str:
        """Écrit, patche, valide puis commit le candidat ou le supprime."""
        candidate = self.candidate
        candidate.unlink(missing_ok=True)
        try:
            self._check_cancelled(signals)
            output = self.run_command(
                self.candidate_command(command),
                cwd,
                label,
                lambda line: signals.progress.emit(line),
                signals,
            )
            self._check_cancelled(signals)
            for action in (*self.post_actions, *tuple(extra_post_actions)):
                action(candidate)
                self._check_cancelled(signals)

            errors = validate_matroska_output(candidate, self.contract)
            if errors:
                raise RuntimeError(
                    "Validation sémantique de la sortie candidate échouée : "
                    + " ; ".join(errors)
                )
            self.run_command(
                [
                    self.ffprobe_bin,
                    "-v", "error",
                    "-show_entries", "format=format_name",
                    "-of", "json",
                    str(candidate),
                ],
                cwd,
                "ffprobe-validation",
                lambda line: signals.progress.emit(line),
                signals,
            )
            self._check_cancelled(signals)
            candidate.replace(self.output)
            if self.write_nfo is not None:
                try:
                    self.write_nfo(self.output)
                except Exception as exc:
                    if self.warn is not None:
                        self.warn(f"Génération NFO échouée après commit : {exc}")
            return output
        except BaseException:
            candidate.unlink(missing_ok=True)
            raise


__all__ = [
    "MatroskaLanguagePostAction",
    "MatroskaMuxingAppPostAction",
    "MatroskaOutputTransaction",
    "PostAction",
    "RunCommand",
]
