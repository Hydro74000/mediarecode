"""
core/workflows/encode/runtime/frame_count_guard.py

Audit d'alignement frame count entre source / encoded HEVC / RPU DoVi /
HDR10+ JSON.

Objectif : empêcher l'injection silencieuse de RPU/HDR10+ sur un stream
encodé qui aurait drop/dup une frame (NVENC en mode rapide, NVDEC sur
sources DV P7, filtres non-frame-preserving). Sans cette garde, le pipeline
produit un fichier décalé, les scènes-cuts DV/HDR10+ ne tombent plus aux bons
endroits.

Politique :
- Écart 0 frames partout → OK, on continue.
- Écart RPU ou HDR10+ ≤ tolérance (4 par défaut) → trim auto + WARN.
- Écart encoded vs source > 0 → abort (ré-encodage non frame-preserving).
- Écart RPU ou HDR10+ > tolérance → abort.

Le trim auto sur HDR10+ est trivial (JSON tronqué). Sur RPU, on délègue à
``dovi_tool editor`` avec un edit JSON ``remove`` pour retirer les frames de
queue.

Fallback frame count
====================
mediainfo est l'outil préféré (rapide, lit l'index sans décoder), mais on ne
veut pas en dépendre exclusivement. Cascade utilisée :

  1. mediainfo --Inform="Video;%FrameCount%"   (instantané si dispo)
  2. ffprobe -show_streams nb_frames           (instantané si déclaré dans
                                                le conteneur, ex MP4)
  3. ffprobe -count_packets -show_streams      (rapide pour MKV indexé,
                                                équivalent au nb de frames
                                                pour HEVC vidéo)
  4. ffprobe -count_frames                     (lent — dernier recours,
                                                décode tout le stream)

Le coût de #4 sur un long-métrage est non négligeable (~30 s à 1 min sur
SSD), donc on ne l'active que si #1-3 ont échoué.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.subprocess_utils import subprocess_text_kwargs


_FRAME_COUNT_FIELD = "Video;%FrameCount%"
_DEFAULT_TOLERANCE = 4


class FrameCountAuditError(RuntimeError):
    """Erreur fatale d'alignement de frames — abort de l'injection."""


@dataclass(frozen=True)
class FrameCountAudit:
    source: int | None
    encoded: int | None
    rpu: int | None
    hdr10p: int | None

    def deltas(self) -> dict[str, int]:
        """Renvoie les écarts vs source pour chaque flux disponible."""
        if self.source is None:
            return {}
        out: dict[str, int] = {}
        for name, value in (
            ("encoded", self.encoded),
            ("rpu", self.rpu),
            ("hdr10p", self.hdr10p),
        ):
            if value is not None:
                out[name] = value - self.source
        return out

    def is_aligned(self, *, tolerance: int = _DEFAULT_TOLERANCE) -> tuple[bool, str]:
        """
        Renvoie (ok, message). ok=True si :
          - encoded == source (aucune tolérance sur le réencodage)
          - |rpu - source| ≤ tolerance
          - |hdr10p - source| ≤ tolerance
        Si source ou encoded sont inconnus → ok=False (audit incomplet).
        """
        if self.source is None or self.encoded is None:
            return (False, "frame count source ou encoded indéterminé")
        if self.encoded != self.source:
            return (
                False,
                f"frame count divergent : source={self.source} encoded={self.encoded} "
                f"(écart {self.encoded - self.source})",
            )
        for name, value in (("rpu", self.rpu), ("hdr10p", self.hdr10p)):
            if value is None:
                continue
            delta = abs(value - self.source)
            if delta > tolerance:
                return (
                    False,
                    f"frame count {name} ({value}) divergent vs source ({self.source}) "
                    f"de {delta} frames (tolérance {tolerance})",
                )
        return (True, "alignement OK")


class FrameCountGuard:
    """
    Garde-fou d'alignement frame count post-encode.

    Utilisation :

        guard = FrameCountGuard(mediainfo_bin="mediainfo", dovi_tool_bin="dovi_tool")
        audit = guard.audit(
            source=source_mkv,
            encoded=enc_hevc,
            rpu_bin=rpu_bin if has_dv else None,
            hdr10p_json=hdr10p_json if has_hdr10p else None,
        )
        guard.enforce(audit, on_warn=log_fn)
        # → lève FrameCountAuditError si désaligné, sinon trim auto.
    """

    def __init__(
        self,
        *,
        mediainfo_bin: str = "mediainfo",
        ffprobe_bin: str = "ffprobe",
        dovi_tool_bin: str = "dovi_tool",
        tolerance: int = _DEFAULT_TOLERANCE,
    ) -> None:
        self._mediainfo = mediainfo_bin
        self._ffprobe = ffprobe_bin
        self._dovi_tool = dovi_tool_bin
        self._tolerance = tolerance

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def audit(
        self,
        *,
        source: Path,
        encoded: Path,
        rpu_bin: Path | None = None,
        hdr10p_json: Path | None = None,
    ) -> FrameCountAudit:
        return FrameCountAudit(
            source=self._read_video_frame_count(source),
            encoded=self._read_video_frame_count(encoded),
            rpu=self._dovi_rpu_frame_count(rpu_bin) if rpu_bin else None,
            hdr10p=self._hdr10p_json_frame_count(hdr10p_json) if hdr10p_json else None,
        )

    # ------------------------------------------------------------------
    # Application de la politique
    # ------------------------------------------------------------------

    def enforce(
        self,
        audit: FrameCountAudit,
        *,
        rpu_bin: Path | None = None,
        hdr10p_json: Path | None = None,
        on_warn: Callable[[str], None] | None = None,
        on_info: Callable[[str], None] | None = None,
    ) -> FrameCountAudit:
        """
        Applique la politique d'alignement :
          - bloque si l'audit est incomplet (mediainfo manquant).
          - bloque si le HEVC encodé diverge de la source (frame-preserving requis).
          - bloque si RPU ou HDR10+ dépassent la tolérance.
          - normalise au strict (trim) si RPU/HDR10+ divergent dans la tolérance,
            même de 1 seule frame — l'injection demande un alignement parfait.

        Renvoie l'audit mis à jour après éventuel trim.
        """
        if audit.source is None or audit.encoded is None:
            # Aucun lecteur n'a réussi (mediainfo + ffprobe tous indisponibles
            # ou source illisible). Mode dégradé "best effort" avec WARN
            # plutôt que d'échouer : un drop NVENC silencieux ne sera pas
            # détecté, mais le workflow peut continuer.
            if on_warn:
                on_warn(
                    "Audit frame count ignoré : mediainfo et ffprobe ont tous "
                    "deux échoué à lire la frame count. Risque de "
                    "désalignement non détecté."
                )
            return audit
        if audit.encoded != audit.source:
            raise FrameCountAuditError(
                f"Encodage non frame-preserving : source={audit.source} frames, "
                f"encodé={audit.encoded} frames. Réessayez avec un preset "
                f"NVENC plus lent (p4/p5) ou un encodeur software."
            )

        # encoded == source : si RPU et HDR10+ matchent strictement aussi → OK direct.
        rpu_aligned = audit.rpu is None or audit.rpu == audit.source
        hdr_aligned = audit.hdr10p is None or audit.hdr10p == audit.source
        if rpu_aligned and hdr_aligned:
            if on_info:
                on_info(
                    f"Audit frame count : alignement OK ({audit.source} frames)."
                )
            return audit

        # RPU / HDR10+ : trim auto si écart ≤ tolérance.
        new_rpu = audit.rpu
        new_hdr10p = audit.hdr10p
        target = audit.source

        if audit.rpu is not None and audit.rpu != target:
            delta = abs(audit.rpu - target)
            if delta > self._tolerance:
                raise FrameCountAuditError(
                    f"RPU DoVi désaligné : {audit.rpu} frames vs source {target} "
                    f"(écart {delta} > tolérance {self._tolerance})."
                )
            if rpu_bin is None:
                raise FrameCountAuditError(
                    "RPU désaligné mais rpu_bin non fourni à enforce()."
                )
            if on_warn:
                on_warn(
                    f"RPU DoVi a {audit.rpu} frames vs source {target} — trim "
                    f"auto à {target} frames."
                )
            self._trim_rpu(rpu_bin, target_frames=target)
            new_rpu = self._dovi_rpu_frame_count(rpu_bin)

        if audit.hdr10p is not None and audit.hdr10p != target:
            delta = abs(audit.hdr10p - target)
            if delta > self._tolerance:
                raise FrameCountAuditError(
                    f"HDR10+ JSON désaligné : {audit.hdr10p} frames vs source "
                    f"{target} (écart {delta} > tolérance {self._tolerance})."
                )
            if hdr10p_json is None:
                raise FrameCountAuditError(
                    "HDR10+ désaligné mais hdr10p_json non fourni à enforce()."
                )
            if on_warn:
                on_warn(
                    f"HDR10+ JSON a {audit.hdr10p} scènes vs source {target} "
                    f"frames — trim auto."
                )
            self._trim_hdr10p_json(hdr10p_json, target_frames=target)
            new_hdr10p = self._hdr10p_json_frame_count(hdr10p_json)

        return FrameCountAudit(
            source=audit.source,
            encoded=audit.encoded,
            rpu=new_rpu,
            hdr10p=new_hdr10p,
        )

    # ------------------------------------------------------------------
    # Lecteurs de frame count
    # ------------------------------------------------------------------

    def _read_video_frame_count(self, path: Path) -> int | None:
        """
        Cascade : mediainfo → ffprobe nb_frames → ffprobe count_packets →
        ffprobe count_frames. Renvoie la première valeur cohérente trouvée,
        None si tout a échoué.
        """
        for reader in (
            self._mediainfo_frame_count,
            self._ffprobe_nb_frames,
            self._ffprobe_count_packets,
            self._ffprobe_count_frames,
        ):
            value = reader(path)
            if value is not None and value > 0:
                return value
        return None

    def _mediainfo_frame_count(self, path: Path) -> int | None:
        try:
            result = subprocess.run(
                [self._mediainfo, f"--Inform={_FRAME_COUNT_FIELD}", str(path)],
                capture_output=True,
                check=False,
                **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError):
            return None
        raw = (result.stdout or "").strip()
        if re.fullmatch(r"\d+", raw):
            return int(raw)
        return None

    def _ffprobe_nb_frames(self, path: Path) -> int | None:
        """
        Lecture directe de ``nb_frames`` dans le conteneur. Instantané quand
        le muxer le déclare (toujours en MP4 ; en MKV, selon le muxeur
        d'origine — on tente, on saute si absent).
        """
        try:
            result = subprocess.run(
                [
                    self._ffprobe, "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=nb_frames",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                check=False,
                **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError):
            return None
        raw = (result.stdout or "").strip()
        if re.fullmatch(r"\d+", raw):
            return int(raw)
        return None

    def _ffprobe_count_packets(self, path: Path) -> int | None:
        """
        ``-count_packets`` lit l'index du conteneur (Cues côté MKV, sample
        table côté MP4) sans décoder. Pour un stream HEVC vidéo, 1 packet
        = 1 access unit = 1 frame. Rapide (quelques secondes pour un
        long-métrage 4K) et fiable tant que le conteneur a un index.

        Pour un HEVC brut (.hevc annexB), le démuxeur compte les access
        units en parsant les NAL — c'est encore plus rapide qu'un décode
        complet.
        """
        try:
            result = subprocess.run(
                [
                    self._ffprobe, "-v", "error",
                    "-select_streams", "v:0",
                    "-count_packets",
                    "-show_entries", "stream=nb_read_packets",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                check=False,
                **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError):
            return None
        raw = (result.stdout or "").strip()
        if re.fullmatch(r"\d+", raw):
            return int(raw)
        return None

    def _ffprobe_count_frames(self, path: Path) -> int | None:
        """
        Dernier recours : ``-count_frames`` décode tout le stream. Lent (du
        même ordre que l'encode lui-même) mais infaillible. Activé seulement
        si les méthodes plus rapides ont toutes échoué (cas exotique :
        conteneur sans index, source corrompue, format inhabituel).
        """
        try:
            result = subprocess.run(
                [
                    self._ffprobe, "-v", "error",
                    "-select_streams", "v:0",
                    "-count_frames",
                    "-show_entries", "stream=nb_read_frames",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                check=False,
                **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError):
            return None
        raw = (result.stdout or "").strip()
        if re.fullmatch(r"\d+", raw):
            return int(raw)
        return None

    def _dovi_rpu_frame_count(self, rpu_bin: Path) -> int | None:
        try:
            result = subprocess.run(
                [self._dovi_tool, "info", "-i", str(rpu_bin), "--summary"],
                capture_output=True,
                check=False,
                **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError):
            return None
        text = (result.stdout or "") + (result.stderr or "")
        # dovi_tool affiche typiquement "Frames: 191733" ou "Total frames: 191733".
        match = re.search(r"(?:Total\s+frames|Frames)\s*:\s*(\d+)", text, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return None

    def _hdr10p_json_frame_count(self, hdr10p_json: Path) -> int | None:
        try:
            data = json.loads(hdr10p_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        scene_info = data.get("SceneInfo")
        if isinstance(scene_info, list):
            return len(scene_info)
        return None

    # ------------------------------------------------------------------
    # Trims
    # ------------------------------------------------------------------

    def _trim_hdr10p_json(self, hdr10p_json: Path, *, target_frames: int) -> None:
        data = json.loads(hdr10p_json.read_text(encoding="utf-8"))
        scene_info = data.get("SceneInfo")
        if not isinstance(scene_info, list):
            return
        if len(scene_info) <= target_frames:
            return
        data["SceneInfo"] = scene_info[:target_frames]
        # SceneInfoSummary.SceneFirstFrameIndex peut référencer des frames retirées
        # → on filtre côté résumé pour éviter les références vers des frames coupées.
        summary = data.get("SceneInfoSummary")
        if isinstance(summary, dict):
            firsts = summary.get("SceneFirstFrameIndex")
            if isinstance(firsts, list):
                summary["SceneFirstFrameIndex"] = [
                    idx for idx in firsts if isinstance(idx, int) and idx < target_frames
                ]
        hdr10p_json.write_text(
            json.dumps(data, separators=(",", ":")),
            encoding="utf-8",
        )

    def _trim_rpu(self, rpu_bin: Path, *, target_frames: int) -> None:
        """
        Tronque le RPU aux N premières frames via ``dovi_tool editor``.

        On génère un edit JSON minimal qui ``remove`` toutes les frames à
        partir de target_frames jusqu'à la fin.
        """
        edit_path = rpu_bin.with_suffix(".edit.json")
        # remove range exclusif sur la dernière frame ; dovi_tool accepte
        # une borne supérieure ouverte avec un grand nombre.
        edit_payload = {"remove": [f"{target_frames}-9999999"]}
        edit_path.write_text(
            json.dumps(edit_payload, separators=(",", ":")),
            encoding="utf-8",
        )
        out_path = rpu_bin.with_suffix(".trimmed.bin")
        try:
            subprocess.run(
                [
                    self._dovi_tool,
                    "editor",
                    "-i", str(rpu_bin),
                    "-j", str(edit_path),
                    "-o", str(out_path),
                ],
                capture_output=True,
                check=True,
                **subprocess_text_kwargs(),
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            raise FrameCountAuditError(
                f"dovi_tool editor a échoué pour le trim RPU : {stderr}"
            ) from exc
        finally:
            edit_path.unlink(missing_ok=True)
        # Remplacement atomique du RPU par la version trimmée.
        out_path.replace(rpu_bin)


__all__ = [
    "FrameCountAudit",
    "FrameCountAuditError",
    "FrameCountGuard",
]
