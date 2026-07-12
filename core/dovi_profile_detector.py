"""
core/dovi_profile_detector.py

Détection fine du sous-profil Dolby Vision pour le routage du pipeline
d'encode (Fix #3 du plan_p7.md).

Pourquoi
========
``VideoTrack.dovi_profile`` (int) ne distingue pas P7 FEL (Full Enhancement
Layer, le BL contient déjà la version HDR pleine) de P7 MEL (Minimum
Enhancement Layer, le BL est SDR-tonemapped et la HDR est dans la EL).
Pour réencoder proprement une source P7 sans dépendre du hasard, le
pipeline a besoin de cette distinction afin de :

- Pour P7 FEL : encoder uniquement le BL (suffisant), comme aujourd'hui.
- Pour P7 MEL : convertir P7→P8.1 via ``dovi_tool -m 2 convert --discard``
  AVANT l'encode, sinon le BL seul produit un visuel SDR.
- Pour P5 : signaler qu'une conversion du base layer IPT vers HDR10 est
  nécessaire. ``dovi_tool -m 3`` ne convertit que le RPU et ne suffit pas.
- Pour P8.x : pipeline tel quel.

Sources d'info
==============
1. Mediainfo ``HDR_Format`` / ``HDR_Format_Profile`` / ``HDR_Format_Settings`` :
   c'est le plus fiable et déjà disponible quand mediainfo est installé.
   Format typique :
     HDR_Format          : "Dolby Vision"
     HDR_Format_Profile  : "dvhe.07 / 06"  ← profile 7, level 6
     HDR_Format_Settings : "BL+EL+RPU" (FEL)  ou  "BL+RPU" (MEL ou P8)
2. ``dovi_tool info -i <stream>`` : fallback quand mediainfo n'expose pas
   les détails. Sortie typique pour un P7 FEL :
     "Profile: 7"
     "DV Level: 6"
     "Subprofile: FEL"  (parfois "MEL" ou "Cross-compatibility ID: 6")

Le détecteur tente d'abord mediainfo (rapide, déjà parsé en amont), puis
dovi_tool si le détail manque.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from core.subprocess_utils import subprocess_text_kwargs


class DoviSubProfile(Enum):
    """Sous-profil Dolby Vision pour le routage encode."""

    UNKNOWN = "unknown"
    """Pas de DV ou détection impossible."""

    P5 = "p5"
    """Profile 5 : base layer IPT, non compatible HDR10 sans réencodage couleur."""
    """Profile 5 — mono-layer non-HDR10-compat (IPTPQc2). Convert → P8 requis."""

    P7_FEL = "p7_fel"
    """Profile 7 Full Enhancement Layer — BL contient déjà la HDR."""

    P7_MEL = "p7_mel"
    """Profile 7 Minimum Enhancement Layer — BL = SDR, EL = HDR delta. Convert → P8 requis."""

    P8_0 = "p8_0"
    """Profile 8.0 — DoVi sans fallback HDR10 explicite."""

    P8_1 = "p8_1"
    """Profile 8.1 — DoVi avec compat_id=1 (HDR10 fallback). Standard remux UHD."""

    P8_2 = "p8_2"
    """Profile 8.2 — DoVi avec compat_id=2 (SDR fallback)."""

    P8_4 = "p8_4"
    """Profile 8.4 — DoVi avec compat_id=4 (HLG fallback)."""

    @property
    def needs_p8_conversion(self) -> bool:
        """True si une préparation vers P8.1 est nécessaire.

        Pour P5, cela inclut obligatoirement une conversion colorimétrique du
        base layer ; le seul traitement RPU de ``dovi_tool`` n'est pas suffisant.
        """
        return self in {DoviSubProfile.P5, DoviSubProfile.P7_FEL, DoviSubProfile.P7_MEL}

    @property
    def convert_mode(self) -> str | None:
        """Retourne le ``-m`` à passer à ``dovi_tool convert``, ou None si inutile."""
        if self in {DoviSubProfile.P7_FEL, DoviSubProfile.P7_MEL}:
            return "2"  # P7 → P8.1
        if self == DoviSubProfile.P5:
            return "3"  # RPU P5 → P8.1 seulement ; BL à convertir séparément.
        return None

    @property
    def label(self) -> str:
        return {
            DoviSubProfile.UNKNOWN: "Inconnu",
            DoviSubProfile.P5: "P5",
            DoviSubProfile.P7_FEL: "P7 FEL",
            DoviSubProfile.P7_MEL: "P7 MEL",
            DoviSubProfile.P8_0: "P8.0",
            DoviSubProfile.P8_1: "P8.1",
            DoviSubProfile.P8_2: "P8.2",
            DoviSubProfile.P8_4: "P8.4",
        }[self]


@dataclass(frozen=True)
class DoviDetectionResult:
    sub_profile: DoviSubProfile
    profile: int | None
    level: int | None
    bl_signal_compat_id: int | None
    raw_source: str        # "mediainfo" | "dovi_tool" | "none"


class DoviProfileDetector:
    """
    Détecte le sous-profil Dolby Vision d'un fichier source ou d'un dict
    mediainfo déjà chargé.

    Utilisation typique depuis ``core.inspector`` ou
    ``metadata_inject.py`` :

        detector = DoviProfileDetector()
        result = detector.detect_from_mediainfo(mi_video_dict)
        if result.sub_profile == DoviSubProfile.UNKNOWN:
            result = detector.detect_from_dovi_tool(source_path)
    """

    def __init__(self, *, dovi_tool_bin: str = "dovi_tool") -> None:
        self._dovi_tool = dovi_tool_bin

    # ------------------------------------------------------------------
    # Mediainfo (préféré : déjà parsé en amont)
    # ------------------------------------------------------------------

    def detect_from_mediainfo(self, mi_video: dict | None) -> DoviDetectionResult:
        """Construit un DoviDetectionResult depuis le track Video du JSON mediainfo."""
        if mi_video is None:
            return DoviDetectionResult(
                sub_profile=DoviSubProfile.UNKNOWN,
                profile=None, level=None, bl_signal_compat_id=None,
                raw_source="none",
            )

        hdr_format = str(mi_video.get("HDR_Format") or "")
        if "dolby vision" not in hdr_format.lower():
            return DoviDetectionResult(
                sub_profile=DoviSubProfile.UNKNOWN,
                profile=None, level=None, bl_signal_compat_id=None,
                raw_source="mediainfo",
            )

        # Profile + level depuis HDR_Format_Profile (ex "dvhe.07 / 06").
        hfp = str(mi_video.get("HDR_Format_Profile") or "").lower()
        m = re.search(r"dv(?:he|av)\.?(\d+)\s*/\s*(\d+)", hfp)
        profile = int(m.group(1)) if m else None
        level = int(m.group(2)) if m else None
        if profile is None:
            # Fallback : juste "dvhe.07" sans le " / 06"
            m2 = re.search(r"dv(?:he|av)\.?(\d+)", hfp)
            profile = int(m2.group(1)) if m2 else None

        # Compat_id depuis HDR_Format_Compatibility.
        compat = str(mi_video.get("HDR_Format_Compatibility") or "").lower()
        if "hdr10" in compat:
            compat_id: int | None = 1
        elif "sdr" in compat:
            compat_id = 2
        elif "hlg" in compat:
            compat_id = 4
        else:
            compat_id = None

        # Settings : "BL+EL+RPU" pour FEL ; "BL+RPU" pour MEL ou P8 mono-layer.
        settings = str(mi_video.get("HDR_Format_Settings") or "").upper()
        has_el = "BL+EL+RPU" in settings or "EL+RPU" in settings

        sub_profile = self._classify(
            profile=profile,
            compat_id=compat_id,
            has_enhancement_layer=has_el,
        )

        return DoviDetectionResult(
            sub_profile=sub_profile,
            profile=profile,
            level=level,
            bl_signal_compat_id=compat_id,
            raw_source="mediainfo",
        )

    # ------------------------------------------------------------------
    # dovi_tool (fallback)
    # ------------------------------------------------------------------

    def detect_from_dovi_tool(self, source: Path) -> DoviDetectionResult:
        """
        Lance ``dovi_tool info -i <RPU>`` après extraction du RPU et parse
        la sortie. ``dovi_tool info`` n'accepte qu'un fichier RPU binaire :
        pour un MKV/MP4/HEVC, on extrait d'abord le RPU via
        ``dovi_tool extract-rpu`` (qui sait lire MKV directement et HEVC
        annexB ; pour MP4 on passe par un pipe ffmpeg + bsf hevc_mp4toannexb).
        """
        import tempfile

        ext = source.suffix.lower()
        rpu_dir = Path(tempfile.mkdtemp(prefix="dovi_detect_"))
        rpu_bin = rpu_dir / "rpu.bin"
        try:
            if ext in {".mkv", ".hevc", ".h265", ".265", ".x265"}:
                extract = subprocess.run(
                    [self._dovi_tool, "extract-rpu", "-i", str(source), "-o", str(rpu_bin)],
                    capture_output=True, check=False, **subprocess_text_kwargs(),
                )
                if extract.returncode != 0 or not rpu_bin.exists():
                    return DoviDetectionResult(
                        sub_profile=DoviSubProfile.UNKNOWN,
                        profile=None, level=None, bl_signal_compat_id=None,
                        raw_source="none",
                    )
            else:
                # MP4/MOV/TS : pipe ffmpeg → dovi_tool extract-rpu via stdin.
                ff = subprocess.Popen(
                    ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(source),
                     "-map", "0:v:0", "-c", "copy", "-bsf:v", "hevc_mp4toannexb",
                     "-f", "hevc", "-"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                )
                dt = subprocess.run(
                    [self._dovi_tool, "extract-rpu", "-", "-o", str(rpu_bin)],
                    stdin=ff.stdout, capture_output=True, check=False,
                    **subprocess_text_kwargs(),
                )
                if ff.stdout is not None:
                    ff.stdout.close()
                ff.wait()
                if dt.returncode != 0 or not rpu_bin.exists():
                    return DoviDetectionResult(
                        sub_profile=DoviSubProfile.UNKNOWN,
                        profile=None, level=None, bl_signal_compat_id=None,
                        raw_source="none",
                    )

            try:
                result = subprocess.run(
                    [self._dovi_tool, "info", "-i", str(rpu_bin), "--summary"],
                    capture_output=True, check=False, **subprocess_text_kwargs(),
                )
            except (FileNotFoundError, OSError):
                return DoviDetectionResult(
                    sub_profile=DoviSubProfile.UNKNOWN,
                    profile=None, level=None, bl_signal_compat_id=None,
                    raw_source="none",
                )
            text = (result.stdout or "") + (result.stderr or "")
            return self.parse_dovi_tool_output(text)
        except (FileNotFoundError, OSError):
            return DoviDetectionResult(
                sub_profile=DoviSubProfile.UNKNOWN,
                profile=None, level=None, bl_signal_compat_id=None,
                raw_source="none",
            )
        finally:
            try:
                rpu_bin.unlink(missing_ok=True)
                rpu_dir.rmdir()
            except OSError:
                pass

    def parse_dovi_tool_output(self, text: str) -> DoviDetectionResult:
        """Parse une sortie texte ``dovi_tool info`` (testable sans subprocess)."""
        if not text.strip():
            return DoviDetectionResult(
                sub_profile=DoviSubProfile.UNKNOWN,
                profile=None, level=None, bl_signal_compat_id=None,
                raw_source="dovi_tool",
            )

        # Profile (parfois "Profile: 8.1", parfois "Profile: 7")
        m = re.search(r"Profile\s*:\s*(\d+)(?:\.(\d+))?", text)
        profile = int(m.group(1)) if m else None
        sub_profile_minor = int(m.group(2)) if (m and m.group(2)) else None

        # Level
        m2 = re.search(r"DV\s+Level\s*:\s*(\d+)", text, re.IGNORECASE)
        level = int(m2.group(1)) if m2 else None

        # Compatibility id (préféré quand dispo)
        m3 = re.search(r"compatibility\s*id\s*:\s*(\d+)", text, re.IGNORECASE)
        compat_id = int(m3.group(1)) if m3 else None

        # Subprofile (FEL/MEL) : dovi_tool affiche parfois "Subprofile: FEL"
        # ou "EL Type: FEL" selon la version.
        has_el_marker = bool(re.search(r"\b(FEL|MEL)\b", text))
        is_fel = bool(re.search(r"\bFEL\b", text))
        is_mel = bool(re.search(r"\bMEL\b", text))

        # Si profile=7 et pas de marqueur explicite, on déduit de "el flag".
        if profile == 7 and not has_el_marker:
            # "el flag: 1" → EL présent. FEL si compatibility id == 6 (cas
            # standard P7.6), MEL sinon.
            el_flag_match = re.search(r"el\s*flag\s*:\s*1", text, re.IGNORECASE)
            if el_flag_match:
                has_el_marker = True
                # P7.6 standard = FEL ; autres minor = ambigu, on tranche FEL
                # par défaut (cas le plus fréquent en remux UHD).
                is_fel = True

        # Si compat_id n'est pas dans la sortie texte, on déduit depuis le minor.
        if compat_id is None and profile == 8 and sub_profile_minor is not None:
            compat_id = sub_profile_minor

        sub_profile = self._classify(
            profile=profile,
            compat_id=compat_id,
            has_enhancement_layer=has_el_marker,
            is_fel=is_fel,
            is_mel=is_mel,
        )
        return DoviDetectionResult(
            sub_profile=sub_profile,
            profile=profile,
            level=level,
            bl_signal_compat_id=compat_id,
            raw_source="dovi_tool",
        )

    # ------------------------------------------------------------------
    # Classification commune
    # ------------------------------------------------------------------

    @staticmethod
    def _classify(
        *,
        profile: int | None,
        compat_id: int | None,
        has_enhancement_layer: bool,
        is_fel: bool = False,
        is_mel: bool = False,
    ) -> DoviSubProfile:
        if profile == 5:
            return DoviSubProfile.P5
        if profile == 7:
            if is_mel:
                return DoviSubProfile.P7_MEL
            if is_fel:
                return DoviSubProfile.P7_FEL
            # Fallback heuristique : pas de marqueur explicite. Sur les
            # remux UHD Blu-ray standards, P7.6 + EL est en pratique
            # quasi-toujours FEL. On classe FEL par défaut faute de mieux —
            # le frame count guard ratrappera un éventuel désalignement.
            _ = has_enhancement_layer
            return DoviSubProfile.P7_FEL
        if profile == 8:
            if compat_id == 1:
                return DoviSubProfile.P8_1
            if compat_id == 2:
                return DoviSubProfile.P8_2
            if compat_id == 4:
                return DoviSubProfile.P8_4
            return DoviSubProfile.P8_0
        return DoviSubProfile.UNKNOWN


__all__ = [
    "DoviDetectionResult",
    "DoviProfileDetector",
    "DoviSubProfile",
]
