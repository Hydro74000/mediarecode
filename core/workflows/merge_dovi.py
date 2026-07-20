"""
core/workflows/merge_dovi.py — Workflow d'injection DoVi RPU + HDR10+.

Logique métier pilotée depuis l'interface Qt via ToolRunner.

Classes publiques :
    FrameCountResult   — résultat de la comparaison des frame counts
    DoviProfile        — profil Dolby Vision cible (8.0 / 8.1)
    WorkflowStep       — énumération des étapes du workflow
    StepResult         — résultat d'une étape
    MergeDoviWorkflow  — orchestrateur du workflow

Signaux :
    MergeDoviWorkflow.step_started(step: WorkflowStep)
    MergeDoviWorkflow.step_progress(step: WorkflowStep, message: str)
    MergeDoviWorkflow.step_finished(step: WorkflowStep, result: StepResult)
    MergeDoviWorkflow.workflow_finished(output_path: str)
    MergeDoviWorkflow.workflow_failed(step: WorkflowStep, error: str)

Conventions :
    - Jamais shell=True
    - pathlib.Path pour tous les chemins
    - ThreadPoolExecutor pour les extractions parallèles
    - Signaux Qt thread-safe (QueuedConnection depuis les workers)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Signal
from core.dovi_profile_detector import DoviProfileDetector, DoviSubProfile
from core.subprocess_utils import subprocess_text_kwargs
from core.subtitle_codec import plan_subtitle_codec
from core.workdir import prepare_process_work_dir
from core.workflows.encode.runtime.dovi_p7_router import DoviP7Router, P7RoutingDecision
from core.workflows.encode.runtime.frame_count_guard import (
    FrameCountAuditError,
    FrameCountGuard,
)
from core.workflows.hevc_static_hdr_metadata import inject_static_hdr_sei_file
from core.matroska.editors.dovi import (
    DolbyVisionConfigRecord,
    MatroskaDoviBlockAdditionEditor,
)
from core.matroska.editors.video_timecodes import MatroskaVideoTimecodePatcher

# Outils dont la barre de progression XX% n'est émise qu'en TTY.
_PTY_PROGRESS_TOOLS: frozenset[str] = frozenset({"dovi_tool", "hdr10plus_tool"})
# Pourcentage à l'intérieur d'une ligne de progression (ex : "Extracting RPU... 73%")
_PERCENT_RE = re.compile(r"(\d{1,3})\s*%")
# Séquences ANSI à supprimer pour garder un log lisible.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_FALLBACK_HEVC_FRAME_RATE = "24000/1001"

#: Extensions de streams HEVC bruts — pas besoin d'extraction ffmpeg.
_RAW_HEVC_EXTENSIONS: frozenset[str] = frozenset({
    ".hevc", ".h265", ".265", ".x265",
})

#: Extensions de conteneurs acceptés en entrée merge_dovi.
#: dovi_tool et hdr10plus_tool ne gèrent nativement que MKV + stream HEVC brut ;
#: pour tout autre conteneur (MP4/MOV/TS/M2TS/VOB/...), on extrait d'abord en
#: HEVC annexB via ffmpeg avec le BSF hevc_mp4toannexb (obligatoire pour MP4).
_MERGE_DOVI_CONTAINERS: frozenset[str] = frozenset({
    ".mkv", ".mk3d", ".mks",
    ".mp4", ".m4v",
    ".mov",
    ".ts", ".m2ts", ".mts",
    ".mpg", ".mpeg", ".m2v", ".mpv", ".evo", ".evob", ".vob",
    ".avi",
    ".webm",
    ".flv", ".f4v",
})

_MERGE_DOVI_ACCEPTED: frozenset[str] = _MERGE_DOVI_CONTAINERS | _RAW_HEVC_EXTENSIONS


def _is_raw_hevc(path: Path) -> bool:
    """Teste si le fichier est un stream HEVC brut (annexB)."""
    return path.suffix.lower() in _RAW_HEVC_EXTENSIONS


def _needs_hevc_extraction(path: Path) -> bool:
    """Teste si le fichier doit être extrait en HEVC annexB avant manipulation."""
    return not _is_raw_hevc(path)


# Mapping des primaires colorimétriques mediainfo → coordonnées CIE 1931 en
# unités 0.00002 (échelle utilisée par master_display côté x265/HEVC SEI 137).
# Format SEI : G(x,y)B(x,y)R(x,y)WP(x,y) avec L(max_lum, min_lum) en 0.0001 cd/m².
_PRIMARIES_DISPLAYS: dict[str, tuple[tuple[int, int], tuple[int, int], tuple[int, int], tuple[int, int]]] = {
    # BT.2020 / Rec.2020 — primaires standards UHD HDR
    "bt.2020": ((8500, 39850), (6550, 2300), (35400, 14600), (15635, 16450)),
    "rec.2020": ((8500, 39850), (6550, 2300), (35400, 14600), (15635, 16450)),
    # Display P3 — souvent utilisé sur masters Apple/Disney
    "display p3": ((13250, 34500), (7500, 3000), (34000, 16000), (15635, 16450)),
    "p3": ((13250, 34500), (7500, 3000), (34000, 16000), (15635, 16450)),
}

_MASTERING_LUM_RE = re.compile(
    r"min:\s*([\d.]+)\s*cd/m\^?2.*?max:\s*([\d.]+)\s*cd/m\^?2",
    re.IGNORECASE | re.DOTALL,
)


def _format_master_display_from_mediainfo(track: dict) -> str:
    """Construit le ``master_display`` x265 depuis un track Video mediainfo.

    mediainfo expose ``MasteringDisplay_ColorPrimaries`` (ex "BT.2020") et
    ``MasteringDisplay_Luminance`` (ex "min: 0.0050 cd/m^2, max: 1000 cd/m^2").
    Renvoie "" si les deux ne sont pas extractibles."""
    primaries_raw = str(track.get("MasteringDisplay_ColorPrimaries") or "").strip().lower()
    lum_raw = str(track.get("MasteringDisplay_Luminance") or "").strip()
    if not primaries_raw or not lum_raw:
        return ""

    coords = None
    for key, value in _PRIMARIES_DISPLAYS.items():
        if key in primaries_raw:
            coords = value
            break
    if coords is None:
        return ""

    m = _MASTERING_LUM_RE.search(lum_raw)
    if not m:
        return ""
    try:
        lmin = int(round(float(m.group(1)) * 10000))
        lmax = int(round(float(m.group(2)) * 10000))
    except ValueError:
        return ""

    g, b, r, wp = coords
    return (
        f"G({g[0]},{g[1]})"
        f"B({b[0]},{b[1]})"
        f"R({r[0]},{r[1]})"
        f"WP({wp[0]},{wp[1]})"
        f"L({lmax},{lmin})"
    )


def _format_max_cll_from_mediainfo(track: dict) -> str:
    """Construit ``"MaxCLL,MaxFALL"`` depuis ``MaxCLL`` / ``MaxFALL`` mediainfo
    (formats : "1000 cd/m2" ou "1000"). Renvoie "" si manquant."""
    raw_cll = str(track.get("MaxCLL") or "").strip()
    raw_fall = str(track.get("MaxFALL") or "").strip()
    if not raw_cll or not raw_fall:
        return ""
    m_cll = re.search(r"(\d+)", raw_cll)
    m_fall = re.search(r"(\d+)", raw_fall)
    if not m_cll or not m_fall:
        return ""
    return f"{m_cll.group(1)},{m_fall.group(1)}"


# =============================================================================
# Types de données
# =============================================================================

class DoviProfile(Enum):
    """
    Profil Dolby Vision cible pour l'injection RPU.

    La valeur de l'enum est le flag `-m` passé en argument GLOBAL à dovi_tool
    (avant la sous-commande inject-rpu).

    Modes :
        DISABLED = "disabled" → n'injecte pas Dolby Vision. Le workflow peut
                       quand même injecter HDR10+ ou convertir un Film 1 SDR
                       vers HDR10 si Film 2 sert de référence HDR10.
        P8_1 = "2"  → normalise le RPU en Profile 8.1, supprime le mapping FEL.
                       Standard pour les remux UHD Blu-ray. Recommandé.
        P8_0 = "0"  → copie le RPU sans modification (rewrite untouched).
                       Préserve le profil source tel quel.
    """
    DISABLED = "disabled"  # Pas d'injection Dolby Vision, HDR10+ reste possible.
    P8_1 = "2"   # -m 2 : conversion Profile 8.1 (standard remux UHD)
    P8_0 = "0"   # -m 0 : copie brute sans conversion


class WorkflowStep(Enum):
    """Étapes du workflow dans l'ordre d'exécution."""
    VALIDATION        = auto()   # Vérifications préliminaires
    DETECT_DOVI       = auto()   # Détection du sous-profil DoVi de Film 2 (P5/P7/P8.x)
    FRAME_COUNT       = auto()   # Comparaison des frame counts
    EXTRACT_PARALLEL  = auto()   # Extractions parallèles (HEVC + RPU + HDR10+)
    SDR_TO_HDR10      = auto()   # Conversion Film 1 SDR → HDR10 assistée par Film 2
    CONVERT_DOVI      = auto()   # Conversion P7/P5 → P8.1 si nécessaire
    INJECT_DOVI       = auto()   # Injection RPU DoVi
    INJECT_HDR10PLUS  = auto()   # Injection HDR10+
    INJECT_STATIC_HDR = auto()   # Injection SEI HDR10 statiques (master_display / max_cll)
    VERIFY            = auto()   # Vérification intégrité RPU frames + alignement frame count
    REMUX             = auto()   # Remuxage final MKV
    CLEANUP           = auto()   # Nettoyage des fichiers intermédiaires


@dataclass
class FrameCountResult:
    """Résultat de la comparaison des frame counts des deux fichiers."""
    fc1: int | None
    fc2: int | None
    diff: int | None

    @property
    def compatible(self) -> bool:
        if self.diff is None:
            return False
        return self.diff <= 4

    @property
    def warning(self) -> bool:
        """True si l'écart est tolérable mais non nul."""
        return self.diff is not None and 0 < self.diff <= 4

    @property
    def status_text(self) -> str:
        if self.fc1 is None or self.fc2 is None:
            return "Frame count illisible"
        if self.diff == 0:
            return f"{self.fc1} frames — identiques ✓"
        if self.warning:
            return f"{self.fc1} / {self.fc2} frames — écart {self.diff} (tolérable)"
        return f"{self.fc1} / {self.fc2} frames — écart {self.diff} (incompatible)"


@dataclass
class HDRFlags:
    """Résultat de la détection des formats HDR dans Film 2."""
    has_dovi: bool = False
    has_hdr10plus: bool = False

    @property
    def label(self) -> str:
        parts = []
        if self.has_dovi:
            parts.append("Dolby Vision")
        if self.has_hdr10plus:
            parts.append("HDR10+")
        return " + ".join(parts) if parts else "Aucun HDR avancé détecté"


@dataclass
class StaticHdrMetadata:
    """SEI HDR10 statiques (Mastering Display + Content Light Level).

    Lus via mediainfo. ``master_display`` est au format x265
    ``G(x,y)B(x,y)R(x,y)WP(x,y)L(max,min)`` ; ``max_cll`` au format
    ``"MaxCLL,MaxFALL"``. Une chaîne vide signifie « non disponible ».
    """
    master_display: str = ""
    max_cll: str = ""

    @property
    def has_any(self) -> bool:
        return bool(self.master_display or self.max_cll)

    @property
    def is_complete(self) -> bool:
        return bool(self.master_display and self.max_cll)


@dataclass(frozen=True)
class ValidationContext:
    flags: HDRFlags
    static_film1: StaticHdrMetadata
    static_film2: StaticHdrMetadata
    film1_needs_sdr_to_hdr10: bool = False
    film2_has_hdr10_reference: bool = False


@dataclass
class StepResult:
    """Résultat d'une étape du workflow."""
    step:     WorkflowStep
    success:  bool
    message:  str
    duration: float = 0.0
    detail:   str   = ""


# =============================================================================
# Configuration interne du workflow
# =============================================================================

@dataclass
class _WorkflowPaths:
    """Chemins intermédiaires utilisés pendant le workflow."""
    work_dir:         Path
    film1:            Path   # Chemin original Film 1 (MKV ou HEVC brut)
    film1_hevc:       Path   # HEVC extrait de Film 1 (uniquement si Film 1 est MKV)
    film1_hdr10_hevc: Path   # Film 1 SDR transcodé en HEVC HDR10
    film2_hevc:       Path   # HEVC temporaire Film 2 (cas 2 : double extraction)
    film2_hevc_p8:    Path   # HEVC Film 2 converti P7/P5 → P8.1 (sert de source RPU)
    film2_rpu:        Path   # RPU DoVi extrait de Film 2
    film2_hdr10plus:  Path   # Métadonnées HDR10+ extraites de Film 2
    film1_with_dovi:  Path   # Film 1 + RPU DoVi injecté
    film1_final:      Path   # Film 1 + RPU DoVi + HDR10+ (résultat final HEVC)
    film1_with_static_hdr: Path  # Film 1 final + SEI HDR10 statiques injectés
    film1_wrapped_video: Path  # Encapsulation MKV de la vidéo injectée (PTS reconstruit)
    output_mkv:       Path   # Fichier de sortie final

    @classmethod
    def from_config(
        cls,
        work_dir: Path,
        output_dir: Path,
        film1: Path,
        basename: str,
    ) -> "_WorkflowPaths":
        return cls(
            work_dir        = work_dir,
            film1           = film1,
            film1_hevc      = work_dir / "film1.hevc",
            film1_hdr10_hevc = work_dir / "film1_hdr10.hevc",
            film2_hevc      = work_dir / "film2.hevc",
            film2_hevc_p8   = work_dir / "film2_p8.hevc",
            film2_rpu       = work_dir / "film2_rpu.bin",
            film2_hdr10plus = work_dir / "film2_hdr10plus.json",
            film1_with_dovi = work_dir / "film1_with_dovi.hevc",
            film1_final     = work_dir / "film1_final.hevc",
            film1_with_static_hdr = work_dir / "film1_with_static_hdr.hevc",
            film1_wrapped_video = work_dir / "film1_wrapped_video.mkv",
            output_mkv      = output_dir / f"{basename}.mkv",
        )

    @property
    def film1_hevc_input(self) -> Path:
        """
        Chemin HEVC en entrée des outils d'injection.
        Si Film 1 est un conteneur (MKV/MP4/TS/…) → film1_hevc extrait en annexB.
        Si Film 1 est un stream HEVC brut           → film1 directement.
        """
        if self.film1_hdr10_hevc.exists():
            return self.film1_hdr10_hevc
        return self.film1 if _is_raw_hevc(self.film1) else self.film1_hevc

    def injection_chain_final(
        self, flags: HDRFlags, *, static_hdr_applied: bool = False,
    ) -> Path:
        """Fichier HEVC final à muxer selon les opérations effectuées."""
        if static_hdr_applied:
            return self.film1_with_static_hdr
        if flags.has_dovi and flags.has_hdr10plus:
            return self.film1_final
        if flags.has_dovi:
            return self.film1_with_dovi
        if flags.has_hdr10plus:
            return self.film1_final
        if self.film1_hdr10_hevc.exists():
            return self.film1_hdr10_hevc
        return self.film1_hevc_input


# =============================================================================
# Exceptions
# =============================================================================

class WorkflowError(RuntimeError):
    """Erreur bloquante pendant le workflow."""
    def __init__(self, step: WorkflowStep, message: str) -> None:
        self.step    = step
        self.message = message
        super().__init__(f"[{step.name}] {message}")


# =============================================================================
# MergeDoviWorkflow
# =============================================================================

class MergeDoviWorkflow(QObject):
    """
    Orchestrateur du workflow d'injection DoVi RPU + HDR10+.

    QObject émettant des signaux Qt pour chaque étape. Toutes les opérations
    lourdes s'exécutent dans des threads secondaires via ThreadPoolExecutor.

    Usage :
        wf = MergeDoviWorkflow(config)
        wf.step_started.connect(on_step_started)
        wf.step_finished.connect(on_step_finished)
        wf.workflow_finished.connect(on_done)
        wf.workflow_failed.connect(on_error)
        wf.start(film1, film2, dovi_profile=DoviProfile.P8_1)

    Arrêt :
        wf.cancel()   — demande l'arrêt propre après l'étape en cours
    """

    # --- Signaux ---
    step_started    = Signal(object)          # WorkflowStep
    step_progress   = Signal(object, str)     # WorkflowStep, message
    step_progress_pct = Signal(object, int)   # WorkflowStep, pourcentage 0..100
    step_finished   = Signal(object, object)  # WorkflowStep, StepResult
    workflow_finished = Signal(str)           # chemin du fichier de sortie
    workflow_failed   = Signal(object, str)   # WorkflowStep, message d'erreur

    def __init__(
        self,
        mediainfo_bin:    str = "mediainfo",
        ffmpeg_bin:       str = "ffmpeg",
        ffprobe_bin:      str = "ffprobe",
        dovi_tool_bin:    str = "dovi_tool",
        hdr10plus_bin:    str = "hdr10plus_tool",
        max_workers:      int = 4,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._bins = {
            "mediainfo":     mediainfo_bin,
            "ffmpeg":        ffmpeg_bin,
            "ffprobe":       ffprobe_bin,
            "dovi_tool":     dovi_tool_bin,
            "hdr10plus_tool": hdr10plus_bin,
        }
        self._max_workers    = max_workers
        self._cancelled      = False

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def start(
        self,
        film1:        Path,
        film2:        Path,
        work_dir:     Path,
        output_dir:   Path,
        dovi_profile: DoviProfile = DoviProfile.P8_1,
        output_basename: str | None = None,
    ) -> None:
        """Lance le workflow dans un thread secondaire."""
        self._cancelled = False
        basename = output_basename or f"{film1.stem}_DOVI_HDR10PLUS"
        output_path = output_dir / f"{basename}.mkv"
        process_work_dir = prepare_process_work_dir(
            work_dir,
            output_path=output_path,
            fallback_name="dovi_job",
        )
        paths = _WorkflowPaths.from_config(process_work_dir, output_dir, film1, basename)

        outer = ThreadPoolExecutor(max_workers=1)
        outer.submit(self._run, film1, film2, paths, dovi_profile)
        outer.shutdown(wait=False)

    def cancel(self) -> None:
        """
        Demande l'annulation propre du workflow.
        L'étape en cours se termine, puis _check_cancel() lève _CancelledError.
        Les sous-processus déjà lancés ne sont pas interrompus de force.
        """
        self._cancelled = True

    # ------------------------------------------------------------------
    # Orchestration principale (thread secondaire)
    # ------------------------------------------------------------------

    def _run(
        self,
        film1: Path,
        film2: Path,
        paths: _WorkflowPaths,
        profile: DoviProfile,
    ) -> None:
        try:
            paths.work_dir.mkdir(parents=True, exist_ok=True)
            paths.output_mkv.parent.mkdir(parents=True, exist_ok=True)

            # 1 — Validation (HEVC, transfert PQ, outils, HDR Film 2)
            validation = self._step_validate(
                film1, film2, profile,
            )
            flags = validation.flags
            static_hdr_film1 = validation.static_film1
            static_hdr_film2 = validation.static_film2
            self._check_cancel()

            # 2 — Détection sous-profil DV de Film 2 (P5/P7 FEL/MEL/P8.x)
            routing = self._step_detect_dovi(film2, flags)
            self._check_cancel()

            # 2-bis — Auto-bump du profil cible : si conversion P7/P5 imposée
            # et l'utilisateur a choisi P8_0 (untouched), forcer P8_1 — sinon
            # le RPU réinjecté reste tagué P7, ce qui rend le fichier illisible
            # côté Plex/TV malgré la conversion en amont.
            effective_profile = profile
            if (
                routing is not None
                and routing.conversion_needed
                and profile == DoviProfile.P8_0
            ):
                effective_profile = DoviProfile.P8_1
                self.step_progress.emit(
                    WorkflowStep.DETECT_DOVI,
                    f"Profil cible auto-bumpé P8.0 → P8.1 "
                    f"(source {routing.sub_profile.label} convertie).",
                )

            # 3 — Frame count. Pour Film 1 SDR, un gros écart interdit les
            # injections temporelles RPU/HDR10+, mais n'empêche pas la
            # conversion HDR10 statique assistée.
            frame_counts = self._step_framecount(
                film1,
                film2,
                allow_large_delta=validation.film1_needs_sdr_to_hdr10,
            )
            metadata_injection_allowed = not (
                validation.film1_needs_sdr_to_hdr10
                and frame_counts.diff is not None
                and frame_counts.diff > 4
            )
            if not metadata_injection_allowed:
                if flags.has_dovi or flags.has_hdr10plus:
                    self.step_progress.emit(
                        WorkflowStep.FRAME_COUNT,
                        "Écart frame count trop important : injection DoVi/HDR10+ "
                        "désactivée, conversion HDR10 seule.",
                    )
                flags = HDRFlags(has_dovi=False, has_hdr10plus=False)
                routing = None
            self._check_cancel()

            # 4 — Extractions parallèles HEVC (Film 1 et Film 2 si nécessaire)
            self._step_extract_hevc(film1, film2, paths, flags, routing)
            self._check_cancel()

            # 4-bis — Film 1 SDR : conversion HDR10 avant injection DV/HDR10+
            if validation.film1_needs_sdr_to_hdr10:
                self._step_convert_sdr_to_hdr10(paths, static_hdr_film2)
                static_hdr_film1 = static_hdr_film2
                self._check_cancel()

            # 5 — Conversion P7/P5 → P8.1 si Film 2 le demande, AVANT extract-rpu
            if routing is not None and routing.conversion_needed:
                self._step_convert_dovi(film2, paths, routing)
                self._check_cancel()

            # 6 — Extraction métadonnées DV/HDR10+ depuis la source appropriée
            if flags.has_dovi or flags.has_hdr10plus:
                self._step_extract_metadata(film2, paths, flags, routing)
                self._check_cancel()

            # 7 — Injection DoVi
            if flags.has_dovi:
                self._step_inject_dovi(paths, flags, effective_profile)
                self._check_cancel()

            # 8 — Injection HDR10+
            if flags.has_hdr10plus:
                self._step_inject_hdr10plus(paths, flags)
                self._check_cancel()

            # 9 — Injection SEI HDR10 statiques si Film 1 ne les a pas
            static_applied = self._step_inject_static_hdr(
                paths, flags, static_hdr_film1, static_hdr_film2,
            )
            self._check_cancel()

            # 10 — Vérification frame count strict (FrameCountGuard)
            self._step_verify(film1, paths, flags, static_hdr_applied=static_applied)
            self._check_cancel()

            # 11 — Remuxage
            self._step_remux(
                film1,
                paths,
                flags,
                static_hdr_applied=static_applied,
                dovi_profile=effective_profile,
            )
            self._check_cancel()

            # 12 — Nettoyage
            self._step_cleanup(paths)

            self.workflow_finished.emit(str(paths.output_mkv))

        except WorkflowError as exc:
            self.workflow_failed.emit(exc.step, exc.message)
        except _CancelledError:
            self.workflow_failed.emit(WorkflowStep.VALIDATION, "Workflow annulé.")

    def _check_cancel(self) -> None:
        if self._cancelled:
            raise _CancelledError()

    # ------------------------------------------------------------------
    # Étape 1 — Validation préliminaire
    # ------------------------------------------------------------------

    def _step_validate(
        self,
        film1: Path,
        film2: Path,
        profile: DoviProfile,
    ) -> ValidationContext:
        step = WorkflowStep.VALIDATION
        t0   = time.monotonic()
        self.step_started.emit(step)

        # Fichiers présents
        for label, path in [("Film 1", film1), ("Film 2", film2)]:
            if not path.is_file():
                raise WorkflowError(step, f"{label} introuvable : {path}")
            self.step_progress.emit(step, f"{label} trouvé : {path.name}")

        # Extensions supportées : conteneurs vidéo (MKV/MP4/MOV/TS/M2TS/…) + HEVC brut.
        # Les conteneurs non-MKV seront extraits en HEVC annexB via ffmpeg
        # (BSF hevc_mp4toannexb appliqué automatiquement) avant passage aux
        # outils dovi_tool / hdr10plus_tool qui ne gèrent que MKV + HEVC brut.
        for label, path in [("Film 1", film1), ("Film 2", film2)]:
            if path.suffix.lower() not in _MERGE_DOVI_ACCEPTED:
                raise WorkflowError(
                    step,
                    f"{label} : format non supporté '{path.suffix}'.",
                )

        # Outils disponibles
        missing = [n for n, cmd in self._bins.items() if not shutil.which(cmd)]
        if missing:
            raise WorkflowError(step, f"Outils manquants : {', '.join(missing)}")
        self.step_progress.emit(step, "Tous les outils sont disponibles.")

        # Flux HEVC présents
        for label, path in [("Film 1", film1), ("Film 2", film2)]:
            codec = self._mediainfo(path, "Video;%Format%").strip().upper()
            if codec != "HEVC":
                raise WorkflowError(step, f"{label} ne contient pas de flux HEVC ({codec})")
            self.step_progress.emit(step, f"{label} — flux HEVC confirmé.")

        static_film1 = self._read_static_hdr_metadata(film1)
        static_film2 = self._read_static_hdr_metadata(film2)

        # Détection HDR dans Film 2 — match strict pour éviter les faux
        # positifs sur "SMPTE ST 2094-10" (DV legacy) qui ne sont PAS HDR10+.
        hdr_raw = self._mediainfo(film2, "Video;%HDR_Format%").strip()
        hdr_lower = hdr_raw.lower()
        transfer2 = self._mediainfo(film2, "Video;%transfer_characteristics%").strip().lower()
        film2_has_hdr10_reference = static_film2.is_complete or self._is_hdr_transfer(transfer2)
        flags = HDRFlags(
            has_dovi      = "dolby vision" in hdr_lower,
            has_hdr10plus = "smpte st 2094 app 4" in hdr_lower,
        )
        if profile == DoviProfile.DISABLED and flags.has_dovi:
            flags.has_dovi = False
            self.step_progress.emit(step, "Dolby Vision désactivé — RPU ignoré.")

        # Validation transfert Film 1. Si Film 1 est SDR mais Film 2 porte des
        # métadonnées HDR10/HDR10+, on bascule vers le workflow SDR→HDR10.
        transfer1 = self._mediainfo(film1, "Video;%transfer_characteristics%").strip().lower()
        film1_is_hdr = self._is_hdr_transfer(transfer1)
        film1_needs_sdr_to_hdr10 = False
        if transfer1 and not film1_is_hdr:
            if film2_has_hdr10_reference:
                film1_needs_sdr_to_hdr10 = True
                self.step_progress.emit(
                    step,
                    f"Film 1 SDR détecté ({transfer1}) — conversion HDR10 assistée activée.",
                )
            else:
                raise WorkflowError(
                    step,
                    f"Film 1 n'est pas en transfert HDR (PQ/HLG) : '{transfer1}'. "
                    "Une conversion SDR→HDR10 nécessite une source HDR10/HDR10+.",
                )
        if not flags.has_dovi and not flags.has_hdr10plus and not film1_needs_sdr_to_hdr10:
            raise WorkflowError(step, "Film 2 ne contient ni Dolby Vision ni HDR10+ utilisable.")
        if film1_needs_sdr_to_hdr10 and not static_film2.is_complete:
            raise WorkflowError(
                step,
                "Conversion SDR→HDR10 impossible : Film 2 ne fournit pas "
                "Master Display + MaxCLL/MaxFALL complets.",
            )
        if flags.has_dovi or flags.has_hdr10plus:
            self.step_progress.emit(step, f"HDR avancé détecté dans Film 2 : {flags.label}")
        elif film1_needs_sdr_to_hdr10:
            self.step_progress.emit(step, "Film 2 HDR10 statique utilisé comme référence.")
        if transfer1:
            if film1_is_hdr:
                self.step_progress.emit(step, f"Film 1 — transfert HDR confirmé ({transfer1}).")

        # Lecture des SEI HDR10 statiques (Mastering Display + MaxCLL/MaxFALL)
        # sur les deux films. Si Film 1 n'en a pas mais Film 2 oui, on
        # complétera plus tard via inject_static_hdr_sei_file.
        if static_film1.is_complete:
            self.step_progress.emit(step, "Film 1 — SEI HDR10 statiques présents.")
        elif static_film2.has_any:
            self.step_progress.emit(
                step,
                "Film 1 — SEI HDR10 statiques manquants ; "
                "fallback vers Film 2 lors de l'injection.",
            )
        else:
            self.step_progress.emit(
                step,
                "[WARN] Aucune métadonnée HDR10 statique disponible "
                "dans Film 1 ni Film 2 — la sortie ne sera pas conforme HDR10.",
            )

        duration = time.monotonic() - t0
        self.step_finished.emit(step, StepResult(step, True, flags.label, duration))
        return ValidationContext(
            flags=flags,
            static_film1=static_film1,
            static_film2=static_film2,
            film1_needs_sdr_to_hdr10=film1_needs_sdr_to_hdr10,
            film2_has_hdr10_reference=film2_has_hdr10_reference,
        )

    @staticmethod
    def _is_hdr_transfer(value: str) -> bool:
        transfer = str(value or "").lower()
        return bool(
            transfer
            and any(k in transfer for k in ("pq", "2084", "smpte st 2084", "hlg", "arib"))
        )

    # ------------------------------------------------------------------
    # Étape 2 — Détection sous-profil DoVi de Film 2
    # ------------------------------------------------------------------

    def _step_detect_dovi(
        self, film2: Path, flags: HDRFlags,
    ) -> P7RoutingDecision | None:
        """
        Détecte le sous-profil DV (P5/P7 FEL/MEL/P8.x) de Film 2 et décide
        si une conversion P7/P5 → P8.1 est nécessaire avant l'extraction RPU.

        Retourne None si Film 2 n'a pas de DoVi (HDR10+ pur).
        """
        step = WorkflowStep.DETECT_DOVI
        t0   = time.monotonic()
        self.step_started.emit(step)

        if not flags.has_dovi:
            self.step_progress.emit(step, "Film 2 sans Dolby Vision — étape ignorée.")
            duration = time.monotonic() - t0
            self.step_finished.emit(
                step, StepResult(step, True, "Pas de DV à router", duration),
            )
            return None

        detector = DoviProfileDetector(dovi_tool_bin=self._bins["dovi_tool"])
        router = DoviP7Router(detector=detector)
        mi_video = self._load_mediainfo_video(film2)
        decision = router.analyze(
            source=film2, mi_video=mi_video, fallback_to_dovi_tool=True,
        )
        self.step_progress.emit(step, decision.reason)

        duration = time.monotonic() - t0
        self.step_finished.emit(
            step,
            StepResult(step, True, decision.sub_profile.label, duration),
        )
        return decision

    # ------------------------------------------------------------------
    # Étape 3 — Comparaison frame counts
    # ------------------------------------------------------------------

    def _step_framecount(
        self,
        film1: Path,
        film2: Path,
        *,
        allow_large_delta: bool = False,
    ) -> FrameCountResult:
        step = WorkflowStep.FRAME_COUNT
        t0   = time.monotonic()
        self.step_started.emit(step)

        fc1 = self._get_framecount(film1)
        fc2 = self._get_framecount(film2)

        diff = abs(fc2 - fc1) if fc1 is not None and fc2 is not None else None
        result = FrameCountResult(fc1, fc2, diff)

        self.step_progress.emit(step, f"Film 1 : {fc1} frames  |  Film 2 : {fc2} frames")

        if diff is not None and diff > 4 and allow_large_delta:
            self.step_progress.emit(
                step,
                f"[WARN] Écart de {diff} frames trop important pour injecter "
                "DoVi/HDR10+ ; conversion HDR10 seule autorisée.",
            )
        elif diff is not None and diff > 4:
            raise WorkflowError(
                step,
                f"Écart de {diff} frames trop important — "
                "les deux fichiers ne semblent pas être le même contenu.",
            )

        duration = time.monotonic() - t0
        self.step_finished.emit(
            step, StepResult(step, True, result.status_text, duration)
        )
        return result

    # ------------------------------------------------------------------
    # Étape 3 — Extractions parallèles
    # ------------------------------------------------------------------

    def _step_extract_hevc(
        self,
        film1: Path,
        film2: Path,
        paths: _WorkflowPaths,
        flags: HDRFlags,
        routing: P7RoutingDecision | None,
    ) -> None:
        """Extrait les flux HEVC annexB requis avant la conversion / extraction
        des métadonnées. Film 1 est extrait s'il n'est pas déjà raw HEVC.
        Film 2 est extrait s'il faut le convertir P7/P5 → P8.1 (dovi_tool
        convert n'accepte que du HEVC annexB) ou si son conteneur n'est pas
        géré nativement par dovi_tool/hdr10plus_tool (MP4/MOV/TS/…)."""
        step = WorkflowStep.EXTRACT_PARALLEL
        t0   = time.monotonic()
        self.step_started.emit(step)

        film1_needs_extract = _needs_hevc_extraction(film1)
        film2_is_mkv = film2.suffix.lower() == ".mkv"
        film2_is_raw = _is_raw_hevc(film2)
        needs_conversion = bool(routing and routing.conversion_needed)
        # Film 2 doit être extrait en annexB si :
        #   - on doit le convertir (dovi_tool convert exige annexB) ; ou
        #   - son conteneur n'est ni MKV ni raw HEVC (extract-rpu/hdr10plus
        #     n'acceptent pas MP4/MOV/TS).
        film2_needs_extract = (
            needs_conversion and not film2_is_raw
        ) or (not film2_is_mkv and not film2_is_raw)

        errors: list[str] = []

        def _emit(msg: str) -> None:
            self.step_progress.emit(step, msg)

        tasks: dict[str, Callable] = {}
        if film1_needs_extract:
            tasks["HEVC Film 1"] = lambda: self._extract_hevc(film1, paths.film1_hevc, _emit)
        if film2_needs_extract:
            tasks["HEVC Film 2"] = lambda: self._extract_hevc(film2, paths.film2_hevc, _emit)

        if tasks:
            _emit(f"Extraction HEVC parallèle ({', '.join(tasks)})…")
            self._run_pool(tasks, errors)
            if errors:
                raise WorkflowError(step, "Extraction HEVC échouée :\n" + "\n".join(errors))
        else:
            _emit("Aucune extraction HEVC requise.")

        duration = time.monotonic() - t0
        self.step_finished.emit(
            step,
            StepResult(step, True, "Extraction HEVC terminée", duration),
        )

    def _film2_metadata_source(
        self,
        film2: Path,
        paths: _WorkflowPaths,
        routing: P7RoutingDecision | None,
    ) -> Path:
        """Source utilisée pour `extract-rpu` et `hdr10plus_tool extract`.

        Priorité :
          1. HEVC P8.1 converti (si routing.conversion_needed) ;
          2. HEVC annexB extrait (si Film 2 n'est ni MKV ni raw HEVC) ;
          3. Film 2 d'origine (MKV ou raw HEVC).
        """
        if routing is not None and routing.conversion_needed and paths.film2_hevc_p8.exists():
            return paths.film2_hevc_p8
        if paths.film2_hevc.exists():
            return paths.film2_hevc
        return film2

    def _step_extract_metadata(
        self,
        film2: Path,
        paths: _WorkflowPaths,
        flags: HDRFlags,
        routing: P7RoutingDecision | None,
    ) -> None:
        """Extrait RPU DoVi et JSON HDR10+ depuis la bonne source (P8.1
        converti si nécessaire). Lancé après l'éventuelle conversion P7/P5."""
        step = WorkflowStep.EXTRACT_PARALLEL
        t0   = time.monotonic()

        source2 = self._film2_metadata_source(film2, paths, routing)
        errors: list[str] = []

        def _emit(msg: str) -> None:
            self.step_progress.emit(step, msg)

        tasks: dict[str, Callable] = {}
        if flags.has_dovi:
            tasks["RPU DoVi"] = lambda: self._extract_rpu(source2, paths.film2_rpu, _emit)
        if flags.has_hdr10plus:
            tasks["HDR10+"] = lambda: self._extract_hdr10plus(source2, paths.film2_hdr10plus, _emit)

        if not tasks:
            return

        _emit(f"Extraction métadonnées depuis {source2.name} ({', '.join(tasks)})…")
        self._run_pool(tasks, errors)
        if errors:
            raise WorkflowError(step, "Extraction métadonnées échouée :\n" + "\n".join(errors))

        duration = time.monotonic() - t0
        self.step_finished.emit(
            step,
            StepResult(step, True, "Métadonnées HDR extraites", duration),
        )

    # ------------------------------------------------------------------
    # Étape 5 — Conversion P7/P5 → P8.1 (avant extract-rpu)
    # ------------------------------------------------------------------

    def _step_convert_dovi(
        self,
        film2: Path,
        paths: _WorkflowPaths,
        routing: P7RoutingDecision,
    ) -> None:
        """Convertit Film 2 P7 (FEL/MEL) ou P5 vers P8.1 mono-layer via
        ``dovi_tool convert``. Le HEVC P8.1 résultant servira de source pour
        extract-rpu et hdr10plus_tool extract dans l'étape suivante."""
        step = WorkflowStep.CONVERT_DOVI
        t0   = time.monotonic()
        self.step_started.emit(step)

        # Source pour la conversion : HEVC annexB obligatoire (dovi_tool
        # convert ne lit pas MKV/MP4). Film 2 brut HEVC ou film2_hevc extrait.
        source = paths.film2_hevc if paths.film2_hevc.exists() else film2
        if not _is_raw_hevc(source):
            raise WorkflowError(
                step,
                f"Conversion DV impossible : source non-HEVC annexB ({source.name}). "
                "Vérifiez l'étape d'extraction HEVC.",
            )

        self.step_progress.emit(
            step,
            f"Conversion {routing.sub_profile.label} → P8.1 "
            f"(dovi_tool -m {routing.convert_mode} convert)…",
        )

        cmd = [
            self._bins["dovi_tool"],
            "-m", routing.convert_mode or "2",
            "convert",
        ]
        if routing.sub_profile in {DoviSubProfile.P7_FEL, DoviSubProfile.P7_MEL}:
            cmd.append("--discard")
        cmd.extend(["-i", str(source), "-o", str(paths.film2_hevc_p8)])
        self._run_cmd(cmd, step)

        # L'extrait annexB intermédiaire a fini son rôle (consommé par convert).
        if paths.film2_hevc.exists():
            paths.film2_hevc.unlink(missing_ok=True)

        duration = time.monotonic() - t0
        self.step_finished.emit(
            step,
            StepResult(
                step, True,
                f"Conversion {routing.sub_profile.label} → P8.1 réussie",
                duration,
            ),
        )

    # ------------------------------------------------------------------
    # Étape 4-bis — Conversion Film 1 SDR → HDR10
    # ------------------------------------------------------------------

    def _step_convert_sdr_to_hdr10(
        self,
        paths: _WorkflowPaths,
        static_hdr: StaticHdrMetadata,
    ) -> None:
        """Transcode Film 1 SDR en HEVC HDR10 10-bit/PQ avant injection.

        Ce chemin ne prétend pas reconstruire l'information HDR absente ; il
        produit une base layer HDR10 cohérente, guidée par les métadonnées
        statiques de Film 2, afin que l'injection HDR10+/DoVi ne repose pas sur
        un BL SDR invalide.
        """
        step = WorkflowStep.SDR_TO_HDR10
        t0 = time.monotonic()
        self.step_started.emit(step)

        hevc_input = paths.film1_hevc_input
        if not hevc_input.exists():
            raise WorkflowError(
                step,
                f"Fichier HEVC source introuvable pour conversion SDR→HDR10 : {hevc_input.name}",
            )
        if not static_hdr.is_complete:
            raise WorkflowError(step, "Métadonnées HDR10 de référence incomplètes.")

        x265_params = (
            f"repeat-headers=1:"
            f"colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:"
            f"master-display={static_hdr.master_display}:"
            f"max-cll={static_hdr.max_cll}"
        )
        vf = (
            "zscale=transfer=linear:npl=100,"
            "format=gbrpf32le,"
            "zscale=primaries=bt2020,"
            "tonemap=tonemap=mobius:desat=0,"
            "zscale=transfer=smpte2084:matrix=bt2020nc:range=tv,"
            "format=yuv420p10le"
        )

        self.step_progress.emit(
            step,
            "Conversion SDR→HDR10 : HEVC Main10 BT.2020/PQ avec métadonnées Film 2…",
        )
        self._run_cmd([
            self._bins["ffmpeg"],
            "-hide_banner",
            "-y",
            "-f", "hevc",
            "-i", str(hevc_input),
            "-map", "0:v:0",
            "-vf", vf,
            "-c:v", "libx265",
            "-preset", "slow",
            "-crf", "18",
            "-pix_fmt", "yuv420p10le",
            "-color_primaries", "bt2020",
            "-color_trc", "smpte2084",
            "-colorspace", "bt2020nc",
            "-color_range", "tv",
            "-x265-params", x265_params,
            "-an",
            "-sn",
            "-dn",
            "-f", "hevc",
            str(paths.film1_hdr10_hevc),
        ], step)

        duration = time.monotonic() - t0
        self.step_finished.emit(
            step,
            StepResult(step, True, "Film 1 SDR converti en base HDR10", duration),
        )

    # ------------------------------------------------------------------
    # Étape 4 — Injection DoVi RPU
    # ------------------------------------------------------------------

    def _step_inject_dovi(
        self,
        paths: _WorkflowPaths,
        flags: HDRFlags,
        profile: DoviProfile,
    ) -> None:
        step = WorkflowStep.INJECT_DOVI
        t0   = time.monotonic()
        self.step_started.emit(step)
        self.step_progress.emit(step, f"Injection RPU DoVi (dovi_tool -m {profile.value})…")

        # film1_hevc_input résout automatiquement MKV extrait vs HEVC direct
        hevc_input = paths.film1_hevc_input
        if not hevc_input.exists():
            raise WorkflowError(
                step,
                f"Fichier HEVC source introuvable : {hevc_input.name} — "
                "vérifiez que l'étape d'extraction s'est bien déroulée.",
            )

        # -m est un flag GLOBAL de dovi_tool placé avant la sous-commande.
        # La valeur de DoviProfile.value est directement le flag -m à passer.
        # P8_1 → -m 2 (normalise en Profile 8.1, supprime mapping FEL)
        # P8_0 → -m 0 (rewrite untouched, préserve le profil source)
        self._run_cmd([
            self._bins["dovi_tool"],
            "-m", profile.value,
            "inject-rpu",
            "-i", str(hevc_input),
            "-r", str(paths.film2_rpu),
            "-o", str(paths.film1_with_dovi),
        ], step)

        self.step_progress.emit(step, f"RPU injecté → {paths.film1_with_dovi.name}")
        duration = time.monotonic() - t0
        mode_label = "Profile 8.1 (standard remux)" if profile == DoviProfile.P8_1 else "mode 0 (untouched)"
        self.step_finished.emit(
            step, StepResult(step, True, f"DoVi RPU injecté — {mode_label}", duration)
        )

    # ------------------------------------------------------------------
    # Étape 5 — Injection HDR10+
    # ------------------------------------------------------------------

    def _step_inject_hdr10plus(
        self,
        paths: _WorkflowPaths,
        flags: HDRFlags,
    ) -> None:
        step = WorkflowStep.INJECT_HDR10PLUS
        t0   = time.monotonic()
        self.step_started.emit(step)
        self.step_progress.emit(step, "Injection HDR10+…")

        # Si DoVi a déjà été injecté → partir de film1_with_dovi.
        # Sinon (HDR10+ seul) → film1_hevc_input résout MKV extrait vs HEVC direct.
        hevc_input = paths.film1_with_dovi if flags.has_dovi else paths.film1_hevc_input

        self._run_cmd([
            self._bins["hdr10plus_tool"],
            "inject",
            "-i", str(hevc_input),
            "-j", str(paths.film2_hdr10plus),
            "-o", str(paths.film1_final),
        ], step)

        self.step_progress.emit(step, f"HDR10+ injecté → {paths.film1_final.name}")
        duration = time.monotonic() - t0
        self.step_finished.emit(
            step, StepResult(step, True, "HDR10+ injecté", duration)
        )

    # ------------------------------------------------------------------
    # Étape 6 — Injection SEI HDR10 statiques (master_display + max_cll)
    # ------------------------------------------------------------------

    def _step_inject_static_hdr(
        self,
        paths: _WorkflowPaths,
        flags: HDRFlags,
        static_film1: StaticHdrMetadata,
        static_film2: StaticHdrMetadata,
    ) -> bool:
        """Injecte les SEI HDR10 statiques (Mastering Display 137 + CLL 144)
        dans le flux final si Film 1 ne les a pas. Priorité d'origine :
        Film 1 (déjà présents → no-op géré par inject_static_hdr_sei_file)
        sinon Film 2 (fallback). Retourne True si l'injection a produit un
        nouveau fichier ``film1_with_static_hdr.hevc`` à muxer."""
        step = WorkflowStep.INJECT_STATIC_HDR
        t0   = time.monotonic()
        self.step_started.emit(step)

        # Si Film 1 a déjà ses SEI statiques complets, on ne touche à rien
        # (dovi_tool inject-rpu et hdr10plus_tool inject les préservent).
        if static_film1.is_complete:
            self.step_progress.emit(
                step, "Film 1 a déjà ses SEI HDR10 statiques — aucune injection.",
            )
            duration = time.monotonic() - t0
            self.step_finished.emit(
                step, StepResult(step, True, "SEI HDR10 préservés", duration),
            )
            return False

        # Source des valeurs : Film 2 si dispo, sinon Film 1 partiel.
        chosen = static_film2 if static_film2.has_any else static_film1
        if not chosen.has_any:
            self.step_progress.emit(
                step,
                "[WARN] Aucune métadonnée HDR10 statique disponible — étape ignorée.",
            )
            duration = time.monotonic() - t0
            self.step_finished.emit(
                step, StepResult(step, True, "Pas de SEI à injecter", duration),
            )
            return False

        # Source HEVC à patcher = sortie de la chaîne d'injection RPU/HDR10+.
        source_hevc = paths.injection_chain_final(flags, static_hdr_applied=False)
        if not source_hevc.exists():
            raise WorkflowError(
                step,
                f"Source HEVC introuvable pour patch SEI statiques : {source_hevc.name}",
            )

        self.step_progress.emit(
            step,
            f"Injection SEI HDR10 statiques (master_display="
            f"{'oui' if chosen.master_display else 'non'}, "
            f"max_cll={'oui' if chosen.max_cll else 'non'}, "
            f"source : Film {'2' if chosen is static_film2 else '1'})…",
        )

        try:
            result = inject_static_hdr_sei_file(
                source_hevc,
                paths.film1_with_static_hdr,
                master_display=chosen.master_display,
                max_cll=chosen.max_cll,
            )
        except ValueError as exc:
            # Format invalide → on log et on continue sans injection.
            self.step_progress.emit(
                step, f"[WARN] Injection SEI ignorée ({exc}).",
            )
            duration = time.monotonic() - t0
            self.step_finished.emit(
                step, StepResult(step, True, "SEI invalides — ignoré", duration),
            )
            return False

        if result.applied:
            self.step_progress.emit(
                step,
                f"SEI HDR10 statiques injectés sur "
                f"{result.injected_access_units} access unit(s).",
            )
        else:
            # inject_static_hdr_sei_file a fait shutil.copyfile (pas d'AU
            # ciblé). On nettoie le doublon et on retourne False.
            paths.film1_with_static_hdr.unlink(missing_ok=True)
            self.step_progress.emit(
                step, "SEI HDR10 déjà présents dans le flux — aucune modification.",
            )

        duration = time.monotonic() - t0
        self.step_finished.emit(
            step,
            StepResult(
                step, True,
                f"SEI HDR10 statiques : {'injectés' if result.applied else 'préservés'}",
                duration,
            ),
        )
        return result.applied

    # ------------------------------------------------------------------
    # Étape 7 — Vérification alignement frame count (FrameCountGuard)
    # ------------------------------------------------------------------

    def _step_verify(
        self,
        film1: Path,
        paths: _WorkflowPaths,
        flags: HDRFlags,
        *,
        static_hdr_applied: bool = False,
    ) -> None:
        """Audit final via FrameCountGuard : compare frame counts du flux
        final HEVC, du RPU et du JSON HDR10+ vs Film 1. Politique stricte :
        encoded == film1 obligatoire ; RPU/HDR10+ tolérance ≤ 4 frames."""
        step = WorkflowStep.VERIFY
        t0   = time.monotonic()
        self.step_started.emit(step)

        final = paths.injection_chain_final(flags, static_hdr_applied=static_hdr_applied)
        if not final.exists():
            raise WorkflowError(step, f"Flux injecté introuvable : {final.name}")

        guard = FrameCountGuard(
            mediainfo_bin=self._bins["mediainfo"],
            ffprobe_bin=self._bins["ffprobe"],
            dovi_tool_bin=self._bins["dovi_tool"],
        )
        audit = guard.audit(
            source=film1,
            encoded=final,
            rpu_bin=paths.film2_rpu if (flags.has_dovi and paths.film2_rpu.exists()) else None,
            hdr10p_json=paths.film2_hdr10plus if (
                flags.has_hdr10plus and paths.film2_hdr10plus.exists()
            ) else None,
        )

        try:
            guard.enforce(
                audit,
                rpu_bin=paths.film2_rpu if (flags.has_dovi and paths.film2_rpu.exists()) else None,
                hdr10p_json=paths.film2_hdr10plus if (
                    flags.has_hdr10plus and paths.film2_hdr10plus.exists()
                ) else None,
                on_warn=lambda msg: self.step_progress.emit(step, f"[WARN] {msg}"),
                on_info=lambda msg: self.step_progress.emit(step, msg),
            )
        except FrameCountAuditError as exc:
            raise WorkflowError(step, f"Audit frame count : {exc}") from exc

        detail = (
            f"Source : {audit.source if audit.source is not None else '?'}  |  "
            f"Final : {audit.encoded if audit.encoded is not None else '?'}  |  "
            f"RPU : {audit.rpu if audit.rpu is not None else '-'}  |  "
            f"HDR10+ : {audit.hdr10p if audit.hdr10p is not None else '-'}"
        )
        self.step_progress.emit(step, detail)

        duration = time.monotonic() - t0
        self.step_finished.emit(
            step, StepResult(step, True, detail, duration)
        )

    # ------------------------------------------------------------------
    # Étape 7 — Remuxage final
    # ------------------------------------------------------------------

    def _step_remux(
        self,
        film1: Path,
        paths: _WorkflowPaths,
        flags: HDRFlags,
        *,
        static_hdr_applied: bool = False,
        dovi_profile: DoviProfile = DoviProfile.P8_1,
    ) -> None:
        step = WorkflowStep.REMUX
        t0   = time.monotonic()
        self.step_started.emit(step)

        final_hevc = paths.injection_chain_final(flags, static_hdr_applied=static_hdr_applied)
        if not final_hevc.exists():
            raise WorkflowError(step, f"Flux injecté introuvable : {final_hevc.name}")

        fps_expr = self._source_video_fps_expr(film1)
        self.step_progress.emit(
            step,
            f"Encapsulation vidéo injectée (FPS source: {fps_expr}) → {paths.film1_wrapped_video.name}…",
        )

        dovi_record = None
        if flags.has_dovi and paths.film2_rpu.exists():
            dovi_record = self._build_dovi_record_from_rpu(
                paths.film2_rpu,
                forced_compat_id=1 if dovi_profile == DoviProfile.P8_1 else None,
            )

        self._run_cmd([
            self._bins["ffmpeg"],
            "-hide_banner",
            "-y",
            "-f", "hevc",
            "-framerate", fps_expr,
            "-i", str(final_hevc),
            "-map", "0:v:0",
            "-c:v", "copy",
            "-bsf:v", f"setts=pts=N/({fps_expr}*TB)",
            str(paths.film1_wrapped_video),
        ], step)

        self.step_progress.emit(step, f"Reconstruction conteneur final → {paths.output_mkv.name}…")
        # Route les subtitle streams de film1 : copy si MKV l'accepte, srt
        # sinon (mov_text, eia_608, …). Indispensable quand film1 est un MP4.
        subs_codec_args = self._subtitle_codec_args_for(film1)
        self._run_cmd([
            self._bins["ffmpeg"],
            "-hide_banner",
            "-y",
            "-i", str(paths.film1_wrapped_video),
            "-i", str(film1),
            "-map", "0:v:0",
            "-map", "1:a?",
            "-map", "1:s?",
            "-map", "1:t?",
            "-map", "1:d?",
            "-c:v", "copy",
            "-c:a", "copy",
            *subs_codec_args,
            "-c:t", "copy",
            "-c:d", "copy",
            "-map_metadata", "1",
            "-map_chapters", "1",
            str(paths.output_mkv),
        ], step)

        self._patch_video_timecodes(paths.output_mkv, film1, step)

        if flags.has_dovi and paths.film2_rpu.exists():
            self._patch_dovi_block_addition(
                paths,
                dovi_profile=dovi_profile,
                step=step,
                dovi_record=dovi_record,
            )

        size_mb = paths.output_mkv.stat().st_size / (1024 ** 2)
        duration = time.monotonic() - t0
        self.step_finished.emit(
            step,
            StepResult(
                step, True,
                f"{paths.output_mkv.name}  ({size_mb:.0f} Mo)",
                duration,
            ),
        )

    def _patch_video_timecodes(
        self,
        target_mkv: Path,
        source_for_timestamps: Path,
        step: WorkflowStep,
    ) -> None:
        """Réapplique les PTS source packet-order sur les blocs vidéo."""
        self.step_progress.emit(
            step,
            "Correction timecodes vidéo depuis la source (ordre packet)…",
        )
        try:
            result = MatroskaVideoTimecodePatcher(
                ffprobe_bin=self._bins["ffprobe"],
            ).patch(
                target_mkv=target_mkv,
                source_for_timestamps=source_for_timestamps,
            )
        except Exception as exc:
            raise WorkflowError(step, f"Patch timecodes vidéo échoué : {exc}") from exc
        self.step_progress.emit(
            step,
            f"Timecodes vidéo corrigés : {result.patched_blocks} blocs "
            f"(dernier PTS {result.last_pts_ms} ms).",
        )

    def _patch_dovi_block_addition(
        self,
        paths: _WorkflowPaths,
        *,
        dovi_profile: DoviProfile,
        step: WorkflowStep,
        dovi_record: DolbyVisionConfigRecord | None = None,
    ) -> None:
        """Ajoute la signalisation Dolby Vision Matroska sur la piste HEVC."""
        self.step_progress.emit(step, "Injection signal Dolby Vision au niveau Matroska…")
        record = dovi_record or self._build_dovi_record_from_rpu(
            paths.film2_rpu,
            forced_compat_id=1 if dovi_profile == DoviProfile.P8_1 else None,
        )
        if record is None:
            self.step_progress.emit(
                step,
                "[WARN] Impossible d'extraire le profil DOVI du RPU "
                "pour le signal Matroska.",
            )
            return

        try:
            patch_result = MatroskaDoviBlockAdditionEditor().patch(
                paths.output_mkv,
                record=record,
            )
        except Exception as exc:
            self.step_progress.emit(
                step,
                f"[WARN] Patch BlockAdditionMapping DOVI échoué : {exc}",
            )
            return

        if patch_result.applied:
            self.step_progress.emit(
                step,
                f"BlockAdditionMapping DOVI ajouté "
                f"(track #{patch_result.patched_track_number}, "
                f"Δ {patch_result.bytes_delta:+d} octets).",
            )
        elif patch_result.skipped:
            self.step_progress.emit(
                step,
                f"Signal DOVI Matroska non modifié : {patch_result.reason}",
            )

    def _build_dovi_record_from_rpu(
        self,
        rpu_bin: Path,
        *,
        forced_compat_id: int | None = None,
    ) -> DolbyVisionConfigRecord | None:
        """Construit le record ``dvcC`` Matroska depuis le résumé dovi_tool."""
        try:
            result = subprocess.run(
                [
                    self._bins["dovi_tool"],
                    "info",
                    "-i",
                    str(rpu_bin),
                    "--summary",
                ],
                capture_output=True,
                check=False,
                **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError):
            return None
        text = (result.stdout or "") + (result.stderr or "")

        profile_match = re.search(r"Profile\s*:\s*(\d+)(?:\.(\d+))?", text)
        if not profile_match:
            return None
        profile = int(profile_match.group(1))
        sub_profile = int(profile_match.group(2) or 0)

        if forced_compat_id is not None:
            compat_id = int(forced_compat_id)
        else:
            compat_match = re.search(
                r"compatibility\s*id\s*:\s*(\d+)",
                text,
                re.IGNORECASE,
            )
            if compat_match:
                compat_id = int(compat_match.group(1))
            elif profile == 8 and sub_profile > 0:
                compat_id = sub_profile
            else:
                # Profile 8 nu dans dovi_tool == P8.x ; pour une base HDR10,
                # signaler P8.1 évite le fallback DV/HDR10 ambigu côté players.
                compat_id = 1 if profile == 8 else 0

        level_match = re.search(r"DV\s+Level\s*:\s*(\d+)", text, re.IGNORECASE)
        level = int(level_match.group(1)) if level_match else 6

        return DolbyVisionConfigRecord(
            profile=profile,
            level=level,
            rpu_present=True,
            el_present=False,
            bl_present=True,
            bl_signal_compat_id=max(0, min(15, compat_id)),
        )

    # ------------------------------------------------------------------
    # Étape 8 — Nettoyage
    # ------------------------------------------------------------------

    def _step_cleanup(self, paths: _WorkflowPaths) -> None:
        step = WorkflowStep.CLEANUP
        t0   = time.monotonic()
        self.step_started.emit(step)

        for path in [
            paths.film1_hevc,
            paths.film1_hdr10_hevc,
            paths.film2_hevc,
            paths.film2_hevc_p8,
            paths.film2_rpu,
            paths.film2_hdr10plus,
            paths.film1_with_dovi,
            paths.film1_final,
            paths.film1_with_static_hdr,
            paths.film1_wrapped_video,
        ]:
            if path.exists():
                path.unlink()
                self.step_progress.emit(step, f"Supprimé : {path.name}")

        try:
            paths.work_dir.rmdir()
        except OSError:
            pass

        duration = time.monotonic() - t0
        self.step_finished.emit(
            step, StepResult(step, True, "Fichiers intermédiaires supprimés", duration)
        )

    # ------------------------------------------------------------------
    # Tâches d'extraction (utilisées dans le pool)
    # ------------------------------------------------------------------

    def _extract_hevc(
        self, source: Path, dest: Path, emit: Callable[[str], None]
    ) -> str:
        emit(f"Extraction HEVC : {source.name} → {dest.name}…")
        # hevc_mp4toannexb est OBLIGATOIRE pour MP4/MOV/TS/M2TS : convertit
        # le HEVC length-prefixed en annexB (start codes) consommable par
        # dovi_tool et hdr10plus_tool. Inoffensif pour MKV (start codes déjà
        # présents → BSF no-op).
        self._run_raw([
            self._bins["ffmpeg"],
            "-hide_banner",
            "-y",
            "-i", str(source),
            "-map", "0:v:0",
            "-c:v", "copy",
            "-bsf:v", "hevc_mp4toannexb",
            "-an",
            "-sn",
            "-dn",
            "-f", "hevc",
            str(dest),
        ])
        return f"HEVC extrait → {dest.name}"

    def _extract_rpu(
        self, source: Path, dest: Path, emit: Callable[[str], None]
    ) -> str:
        emit(f"Extraction RPU DoVi : {source.name}…")
        self._run_raw([
            self._bins["dovi_tool"], "extract-rpu",
            "-i", str(source), "-o", str(dest),
        ], step=WorkflowStep.EXTRACT_PARALLEL)
        return f"RPU extrait → {dest.name}"

    def _extract_hdr10plus(
        self, source: Path, dest: Path, emit: Callable[[str], None]
    ) -> str:
        emit(f"Extraction HDR10+ : {source.name}…")
        self._run_raw([
            self._bins["hdr10plus_tool"], "extract",
            str(source), "-o", str(dest),
        ], step=WorkflowStep.EXTRACT_PARALLEL)
        return f"HDR10+ extrait → {dest.name}"

    # ------------------------------------------------------------------
    # Pool d'exécution parallèle
    # ------------------------------------------------------------------

    def _run_pool(
        self,
        tasks: dict[str, Callable],
        errors: list[str],
    ) -> None:
        """
        Exécute les callables en parallèle via ThreadPoolExecutor.
        Collecte les erreurs dans `errors`. Vérifie le flag d'annulation
        entre chaque future complétée pour permettre un arrêt propre.
        Ne fait rien si `tasks` est vide (ThreadPoolExecutor(max_workers=0)
        lèverait ValueError).
        """
        if not tasks:
            return
        with ThreadPoolExecutor(max_workers=min(len(tasks), self._max_workers)) as executor:
            futures: dict[Future, str] = {
                executor.submit(fn): label
                for label, fn in tasks.items()
            }
            for future in as_completed(futures):
                label = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    errors.append(f"[{label}] {exc}")
                # Vérifier l'annulation après chaque tâche terminée
                if self._cancelled:
                    break

    # ------------------------------------------------------------------
    # Helpers subprocess
    # ------------------------------------------------------------------

    def _run_cmd(self, cmd: list[str], step: WorkflowStep) -> str:
        """
        Lance une commande, émet des lignes de progression, lève WorkflowError
        si le code de retour est non nul.

        Pour `dovi_tool` / `hdr10plus_tool` sous Linux/macOS, la commande est
        exécutée sous pty pour que l'outil émette sa barre de progression.
        Les lignes XX% alimentent `step_progress_pct` (barre globale) et NE
        sont PAS émises en `step_progress` (le LogPanel reste lisible).
        """
        binary = Path(cmd[0]).name
        use_pty = sys.platform != "win32" and binary in _PTY_PROGRESS_TOOLS
        try:
            if use_pty:
                return self._run_cmd_pty(cmd, step)
            with subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                **subprocess_text_kwargs(),
            ) as proc:
                lines: list[str] = []
                assert proc.stdout is not None
                for line in proc.stdout:
                    stripped = line.rstrip("\n")
                    if stripped:
                        lines.append(stripped)
                        self.step_progress.emit(step, stripped)
                proc.wait()
                output = "\n".join(lines)
                if proc.returncode != 0:
                    raise WorkflowError(
                        step,
                        f"Commande échouée (code {proc.returncode}) : {' '.join(cmd[:2])}\n"
                        + output[-1000:],
                    )
                return output
        except WorkflowError:
            raise
        except FileNotFoundError:
            raise WorkflowError(step, f"Outil introuvable : {cmd[0]}")

    def _run_cmd_pty(self, cmd: list[str], step: WorkflowStep) -> str:
        """
        Variante pty pour dovi_tool / hdr10plus_tool (Linux/macOS).

        Lignes XX% → `step_progress_pct.emit(step, pct)` au changement.
        Autres lignes → `step_progress.emit(step, line)` comme d'habitude.
        """
        import fcntl
        import pty
        import struct
        import termios

        master_fd, slave_fd = pty.openpty()
        # Taille de terminal nécessaire pour que `indicatif` (dovi_tool /
        # hdr10plus_tool) accepte d'afficher la barre de progression.
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 120, 0, 0))
        except OSError:
            pass
        # `indicatif` désactive la barre quand TERM=dumb (cas via .desktop /
        # distrobox-export) ou non défini. Forcer un TERM réel.
        env = os.environ.copy()
        if env.get("TERM", "dumb") in ("", "dumb"):
            env["TERM"] = "xterm-256color"
        proc = subprocess.Popen(
            cmd,
            stdout=slave_fd,
            stderr=slave_fd,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            env=env,
        )
        os.close(slave_fd)

        lines: list[str] = []
        last_pct: int = -1
        buffer = ""
        try:
            while True:
                try:
                    data = os.read(master_fd, 1024)
                except OSError:
                    break
                if not data:
                    break

                buffer += data.decode("utf-8", errors="replace")
                if "\r" not in buffer and "\n" not in buffer:
                    continue

                parts = re.split(r"[\r\n]+", buffer)
                buffer = parts[-1]
                for raw in parts[:-1]:
                    line = _ANSI_RE.sub("", raw).strip()
                    if not line:
                        continue
                    lines.append(line)

                    m = _PERCENT_RE.search(line)
                    if m:
                        try:
                            pct = int(m.group(1))
                        except ValueError:
                            pct = -1
                        if 0 <= pct <= 100 and pct != last_pct:
                            last_pct = pct
                            self.step_progress_pct.emit(step, pct)
                        # Lignes de progression : pas de log
                        continue
                    self.step_progress.emit(step, line)

            if buffer.strip():
                line = _ANSI_RE.sub("", buffer).strip()
                lines.append(line)
                if not _PERCENT_RE.search(line):
                    self.step_progress.emit(step, line)

            proc.wait()
            output = "\n".join(lines)
            if proc.returncode != 0:
                raise WorkflowError(
                    step,
                    f"Commande échouée (code {proc.returncode}) : {' '.join(cmd[:2])}\n"
                    + output[-1000:],
                )
            return output
        finally:
            try:
                os.close(master_fd)
            except OSError:
                pass

    def _run_raw(self, cmd: list[str], step: WorkflowStep | None = None) -> str:
        """
        Lance une commande dans un worker. Si `step` est fourni et le binaire
        est dovi_tool/hdr10plus_tool sous Linux/macOS, exécute sous pty pour
        capturer le pourcentage de progression (alimente `step_progress_pct`).
        """
        binary = Path(cmd[0]).name
        if (
            step is not None
            and sys.platform != "win32"
            and binary in _PTY_PROGRESS_TOOLS
        ):
            return self._run_cmd_pty(cmd, step)

        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            **subprocess_text_kwargs(),
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Commande échouée (code {result.returncode}) : {' '.join(cmd[:2])}\n"
                + (result.stdout + result.stderr)[-500:]
            )
        return result.stdout

    # ------------------------------------------------------------------
    # Helpers mediainfo
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_frame_rate_expr(value: object) -> str | None:
        raw = str(value or "").strip()
        if raw in {"", "0", "0/0", "N/A"}:
            return None
        if re.fullmatch(r"\d+(?:\.\d+)?", raw):
            return raw
        if re.fullmatch(r"\d+/\d+", raw):
            return raw
        return None

    def _source_video_fps_expr(self, source: Path) -> str:
        cmd = [
            self._bins["ffprobe"],
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            str(source),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                **subprocess_text_kwargs(),
            )
        except Exception:
            return _FALLBACK_HEVC_FRAME_RATE
        if result.returncode != 0:
            return _FALLBACK_HEVC_FRAME_RATE
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return _FALLBACK_HEVC_FRAME_RATE
        streams = payload.get("streams")
        if not isinstance(streams, list):
            return _FALLBACK_HEVC_FRAME_RATE
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            if stream.get("codec_type") != "video":
                continue
            for key in ("avg_frame_rate", "r_frame_rate"):
                fps_expr = self._normalize_frame_rate_expr(stream.get(key))
                if fps_expr is not None:
                    return fps_expr
            break
        return _FALLBACK_HEVC_FRAME_RATE

    def _subtitle_codec_args_for(self, source: Path) -> list[str]:
        """Args ``-c:s …`` pour le muxage MKV final selon les subs du source.

        Route chaque piste : copy quand MKV l'accepte, srt sinon (mov_text,
        eia_608, …). Si aucune sub ou probing impossible → ``-c:s copy``.
        """
        cmd = [
            self._bins["ffprobe"],
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-select_streams", "s",
            str(source),
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, check=False,
                **subprocess_text_kwargs(),
            )
        except Exception:
            return ["-c:s", "copy"]
        if result.returncode != 0:
            return ["-c:s", "copy"]
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return ["-c:s", "copy"]

        streams = payload.get("streams") or []
        per_index: list[str] = []
        any_convert = False
        # Les streams renvoyés par -select_streams s sont ordonnés selon
        # leur apparition dans le fichier : c'est l'ordre que ffmpeg utilisera
        # pour -map 1:s? (out index 0, 1, 2, …).
        for out_idx, stream in enumerate(streams):
            if not isinstance(stream, dict):
                continue
            codec = str(stream.get("codec_name", "") or "")
            try:
                codec_arg, _ = plan_subtitle_codec(codec)
            except ValueError:
                # Codec non supporté : on force srt, ffmpeg refusera s'il ne
                # sait pas convertir et l'utilisateur verra l'erreur.
                codec_arg = "srt"
            if codec_arg != "copy":
                any_convert = True
                per_index.extend([f"-c:s:{out_idx}", codec_arg])
        if not any_convert:
            return ["-c:s", "copy"]
        return ["-c:s", "copy", *per_index]

    def _mediainfo(self, path: Path, inform: str) -> str:
        """Lance mediainfo --Inform et retourne la sortie brute."""
        result = subprocess.run(
            [self._bins["mediainfo"], f"--Inform={inform}", str(path)],
            capture_output=True, check=False, **subprocess_text_kwargs(),
        )
        return result.stdout

    def _get_framecount(self, path: Path) -> int | None:
        """Retourne le frame count via mediainfo, ou None si illisible."""
        raw = self._mediainfo(path, "Video;%FrameCount%").strip()
        if re.fullmatch(r"\d+", raw):
            return int(raw)
        return None

    def _load_mediainfo_video(self, path: Path) -> dict | None:
        """Charge le track Video du JSON mediainfo (None si indisponible)."""
        try:
            result = subprocess.run(
                [self._bins["mediainfo"], "--Output=JSON", str(path)],
                capture_output=True, check=False, **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError):
            return None
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return None
        media = data.get("media") or {}
        for track in media.get("track") or []:
            if isinstance(track, dict) and track.get("@type") == "Video":
                return track
        return None

    def _read_static_hdr_metadata(self, path: Path) -> StaticHdrMetadata:
        """Lit MasteringDisplay + MaxCLL/MaxFALL via mediainfo et reformate
        en chaînes attendues par ``inject_static_hdr_sei_file`` :
          - master_display : ``G(x,y)B(x,y)R(x,y)WP(x,y)L(max,min)``
          - max_cll        : ``"MaxCLL,MaxFALL"``

        Toute partie absente / non parsable → chaîne vide (no-op côté
        injection)."""
        track = self._load_mediainfo_video(path)
        if track is None:
            return StaticHdrMetadata()
        return StaticHdrMetadata(
            master_display=_format_master_display_from_mediainfo(track),
            max_cll=_format_max_cll_from_mediainfo(track),
        )

    # ------------------------------------------------------------------
    # Utilitaire statique
    # ------------------------------------------------------------------

    @staticmethod
    def required_tools() -> list[str]:
        return ["mediainfo", "ffmpeg", "ffprobe", "dovi_tool", "hdr10plus_tool"]


class _CancelledError(Exception):
    """Levée en interne pour signaler une annulation."""
    pass
