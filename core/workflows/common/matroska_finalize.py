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
from core.matroska.editors.statistics import (
    MatroskaStatisticsPatchResult,
    MatroskaTrackStatisticsEditor,
)
from core.matroska.editors.track_flags import (
    MatroskaTrackEnabledEditor,
    MatroskaTrackEnabledPatchResult,
)
from core.matroska.reader import MatroskaReader
from core.matroska.validation import MatroskaPacketValidation, validate_matroska_output
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


class MatroskaTrackEnabledPostAction:
    """Workflow adapter applying the Matroska FlagEnabled of a contract.

    FFmpeg ne sait pas écrire ``FlagEnabled`` : les pistes que l'utilisateur
    a désactivées (ou qui l'étaient déjà en source) sortiraient activées sans
    ce patch. Le backend natif, lui, écrit la valeur directement.
    """

    def __init__(
        self,
        *,
        editor: MatroskaTrackEnabledEditor | None = None,
        log_cb: Callable[[str, str], None] | None = None,
    ) -> None:
        self._editor = editor or MatroskaTrackEnabledEditor()
        self._log_cb = log_cb

    @staticmethod
    def expected_flags(contract: MatroskaOutputContract) -> dict[int, bool]:
        """FlagEnabled attendu par position de piste de sortie."""
        return {
            position: bool(track.flags.enabled)
            for position, track in enumerate(contract.expected_tracks)
            if track.flags is not None and track.flags.enabled is not None
        }

    def apply_for_contract(
        self,
        output_path: Path,
        contract: MatroskaOutputContract,
        *,
        log_cb: Callable[[str, str], None] | None = None,
    ) -> MatroskaTrackEnabledPatchResult | None:
        cb = log_cb or self._log_cb
        name = output_path.name.lower()
        if output_path.suffix.lower() != ".mkv" and not name.endswith(".mkv.partial"):
            return None
        if not output_path.is_file():
            return None
        wanted = self.expected_flags(contract)
        if not wanted:
            return None

        try:
            observed_count = len(MatroskaReader(output_path).tracks())
        except (OSError, ValueError) as exc:
            if cb is not None:
                cb("WARN", f"Post-action FlagEnabled ignorée: pistes illisibles ({exc})")
            return None
        if observed_count != len(contract.expected_tracks):
            # Pistes de sortie non prévisibles (copie de sous-titres par
            # mapping optionnel) : l'appariement par position ne serait pas
            # fiable, aucun flag n'est forcé.
            if cb is not None and any(not value for value in wanted.values()):
                cb(
                    "WARN",
                    "Post-action FlagEnabled ignorée : "
                    f"{observed_count} piste(s) écrite(s) pour "
                    f"{len(contract.expected_tracks)} attendue(s).",
                )
            return None

        result = self._editor.apply(output_path, wanted)
        if cb is not None:
            if result.applied:
                details = ", ".join(
                    f"#{fix.track_position + 1}→"
                    + ("activée" if fix.enabled_after else "désactivée")
                    for fix in result.fixes
                )
                cb("INFO", f"FlagEnabled Matroska appliqué ({len(result.fixes)} piste(s)): {details}")
            elif result.skipped and result.reason:
                cb("WARN", f"Post-action FlagEnabled ignorée: {result.reason}")
        return result


class MatroskaTrackStatisticsPostAction:
    """Workflow adapter regenerating Matroska per-track statistics."""

    def __init__(
        self,
        *,
        editor: MatroskaTrackStatisticsEditor | None = None,
        writing_app: str | None = None,
        log_cb: Callable[[str, str], None] | None = None,
    ) -> None:
        self._editor = editor or MatroskaTrackStatisticsEditor()
        self._writing_app = writing_app
        self._log_cb = log_cb

    def apply_if_mkv(
        self,
        output_path: Path,
        *,
        writing_app: str | None = None,
        log_cb: Callable[[str, str], None] | None = None,
        statistics_by_position: dict[int, tuple[int, int, int]] | None = None,
    ) -> MatroskaStatisticsPatchResult | None:
        cb = log_cb or self._log_cb
        name = output_path.name.lower()
        if output_path.suffix.lower() != ".mkv" and not name.endswith(".mkv.partial"):
            return None
        if not output_path.is_file():
            return None

        result = self._editor.apply(
            output_path,
            writing_app=writing_app or self._writing_app or "Muxiveo",
            statistics_by_position=statistics_by_position,
        )
        if cb is not None:
            if result.applied:
                origin = (
                    "reprises des sources (copie stricte)"
                    if statistics_by_position is not None
                    else "mesurées sur la sortie"
                )
                cb(
                    "INFO",
                    "Statistiques Matroska régénérées "
                    f"({result.track_count} piste(s), {origin}) : BPS, DURATION, "
                    "NUMBER_OF_FRAMES, NUMBER_OF_BYTES.",
                )
            elif result.skipped and result.reason:
                cb("WARN", f"Post-action statistiques ignorée: {result.reason}")
            elif result.reason:
                cb("INFO", f"Post-action statistiques: {result.reason}")
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
    #: Applique le FlagEnabled du contrat, que FFmpeg ne sait pas écrire.
    track_enabled_post_action: MatroskaTrackEnabledPostAction | None = None

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
            if self.track_enabled_post_action is not None:
                self.track_enabled_post_action.apply_for_contract(candidate, self.contract)
                self._check_cancelled(signals)
            packet_validation: MatroskaPacketValidation | None = None
            for action in (*self.post_actions, *tuple(extra_post_actions)):
                result = action(candidate)
                # Une post-action qui a déjà parcouru les paquets transmet son
                # résumé : la validation n'a pas à relire la sortie entière.
                summary = getattr(result, "packet_validation", None)
                if isinstance(summary, MatroskaPacketValidation):
                    packet_validation = summary
                self._check_cancelled(signals)

            errors = validate_matroska_output(
                candidate, self.contract, packet_validation=packet_validation,
            )
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
    "MatroskaTrackEnabledPostAction",
    "MatroskaTrackStatisticsPostAction",
    "PostAction",
    "RunCommand",
]
