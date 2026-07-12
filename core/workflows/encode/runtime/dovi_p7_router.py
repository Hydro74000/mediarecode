"""
core/workflows/encode/runtime/dovi_p7_router.py

Routeur P7 / P5 pour le pipeline d'encode DoVi.

Quand la source est en Dolby Vision Profile 7 (FEL/MEL), ``dovi_tool
convert`` peut normaliser le flux en P8.1 avant le reste du pipeline :

  - P7 FEL/MEL : ``dovi_tool -m 2 convert --discard``

Pour P5, le mode 3 ne convertit que le RPU. Le base layer IPT doit être
réencodé vers BT.2020/PQ par un traitement Dolby Vision conscient avant
de pouvoir constituer un vrai fallback HDR10 P8.1.

Cette classe encapsule toute la logique de routage pour rester testable
et garder ``metadata_inject.py`` propre.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.dovi_profile_detector import (
    DoviDetectionResult,
    DoviProfileDetector,
    DoviSubProfile,
)


@dataclass(frozen=True)
class P7RoutingDecision:
    """
    Résultat de l'analyse de routage pour une source donnée.

    ``conversion_needed`` indique si on doit lancer ``dovi_tool convert``
    avant le reste du pipeline. Si False, le pipeline utilise la source
    telle quelle.
    """
    conversion_needed: bool
    sub_profile: DoviSubProfile
    convert_mode: str | None    # "-m 2", "-m 3" ou None
    reason: str                 # message humainement lisible pour les logs


class DoviP7Router:
    """
    Décide si une source DV nécessite une conversion P→P8.1 et exécute
    la commande ``dovi_tool convert`` au besoin.

    Utilisation typique dans STEP 4-bis de ``metadata_inject.py`` :

        router = DoviP7Router(detector=DoviProfileDetector(...))
        decision = router.analyze(source=config.source, mi_video=...)
        if decision.conversion_needed:
            converted = router.execute_conversion(
                source=config.source,
                output_dir=tmp,
                run_cmd=_run,
                dovi_tool_bin=cb.bins["dovi_tool"],
            )
            video_source_path_override = converted
    """

    def __init__(self, *, detector: DoviProfileDetector | None = None) -> None:
        self._detector = detector or DoviProfileDetector()

    # ------------------------------------------------------------------
    # Décision
    # ------------------------------------------------------------------

    def analyze(
        self,
        *,
        source: Path,
        mi_video: dict | None = None,
        fallback_to_dovi_tool: bool = True,
    ) -> P7RoutingDecision:
        """
        Analyse la source et renvoie la décision de routage.

        Cherche d'abord via mediainfo (rapide, déjà parsé en amont).
        Tombe sur ``dovi_tool info`` si mediainfo n'a pas pu déterminer
        le sous-profil (et que ``fallback_to_dovi_tool`` est True).
        """
        result = self._detector.detect_from_mediainfo(mi_video)
        if result.sub_profile == DoviSubProfile.UNKNOWN and fallback_to_dovi_tool:
            result = self._detector.detect_from_dovi_tool(source)
        return self._decision_from_result(result)

    @staticmethod
    def _decision_from_result(result: DoviDetectionResult) -> P7RoutingDecision:
        sp = result.sub_profile
        if sp == DoviSubProfile.UNKNOWN:
            return P7RoutingDecision(
                conversion_needed=False,
                sub_profile=sp,
                convert_mode=None,
                reason="Profil DoVi non détecté — pipeline standard appliqué.",
            )
        if not sp.needs_p8_conversion:
            return P7RoutingDecision(
                conversion_needed=False,
                sub_profile=sp,
                convert_mode=None,
                reason=f"Source déjà compatible ({sp.label}) — pas de conversion.",
            )
        return P7RoutingDecision(
            conversion_needed=True,
            sub_profile=sp,
            convert_mode=sp.convert_mode,
            reason=(
                f"Source {sp.label} détectée → conversion vers P8.1 via "
                f"`dovi_tool -m {sp.convert_mode} convert`."
            ),
        )

    # ------------------------------------------------------------------
    # Exécution
    # ------------------------------------------------------------------

    def execute_conversion(
        self,
        *,
        source: Path,
        output_dir: Path,
        run_cmd: Callable[[list[str]], object],
        dovi_tool_bin: str,
        decision: P7RoutingDecision,
    ) -> Path:
        """
        Lance ``dovi_tool convert`` et renvoie le chemin du HEVC P8.1
        produit. ``run_cmd`` est l'helper d'exécution du pipeline (qui
        gère la cancellation, le logging et les signaux Qt).

        ``source`` doit être un HEVC annexB (``.hevc``/``.h265``/``.265``).
        ``dovi_tool convert`` ne lit pas les conteneurs MKV/MP4 :
        l'appelant est responsable de l'extraction annexB préalable
        (voir ``metadata_inject._ensure_hevc_annexb``).

        Le HEVC produit est nommé ``source_p8.hevc`` dans ``output_dir``.
        """
        if not decision.conversion_needed:
            raise ValueError(
                "execute_conversion() appelé alors que conversion_needed=False"
            )
        if decision.convert_mode is None:
            raise ValueError("convert_mode manquant dans la décision.")
        if source.suffix.lower() not in {".hevc", ".h265", ".265", ".x265"}:
            raise ValueError(
                "execute_conversion() exige un HEVC annexB en entrée "
                f"(reçu : {source.suffix})."
            )

        out_path = output_dir / "source_p8.hevc"
        cmd = [
            dovi_tool_bin,
            "-m", decision.convert_mode,
            "convert",
        ]
        # Le flag --discard est requis pour P7 FEL/MEL (jette la EL après
        # conversion). Pour P5, le résultat sert uniquement à extraire le
        # RPU P8.1 ; le base layer HDR10 est produit séparément par libplacebo.
        if decision.sub_profile in {DoviSubProfile.P7_FEL, DoviSubProfile.P7_MEL}:
            cmd.append("--discard")
        cmd.extend(["-i", str(source), "-o", str(out_path)])
        run_cmd(cmd)
        return out_path


__all__ = [
    "DoviP7Router",
    "P7RoutingDecision",
]
