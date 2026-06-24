"""
core/workflows/matroska_timestamp_reader.py

Lecteur de timestamps de présentation (PTS) d'une piste vidéo source via
ffprobe. Utilisé par le muxer Matroska natif (``MatroskaNativeMuxer``)
pour réinjecter les PTS source sur un HEVC ré-encodé sans dépendre de
mkvmerge.

Pour les sources VFR, c'est la seule manière d'obtenir un MKV avec des
PTS fidèles à l'original ; pour les sources CFR, le muxer natif s'en
sert aussi (c'est plus simple et plus robuste que de générer des PTS
synthétiques au framerate moyen, qui peut dériver de quelques
millisecondes sur 2 h).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from core.bluray import ffprobe_input_args
from core.subprocess_utils import subprocess_text_kwargs


@dataclass(frozen=True)
class TimestampSequence:
    """
    Séquence ordonnée de PTS et durations en millisecondes.

    ``pts_ms`` : timestamps de présentation, tels qu'ils étaient dans la
                 source. Peuvent être non-uniformes (VFR).
    ``durations_ms`` : durée de chaque frame, calculée à partir des deltas
                       PTS successifs (la dernière frame réutilise la durée
                       précédente faute de mieux).
    """
    pts_ms: tuple[int, ...]
    durations_ms: tuple[int, ...]

    def __len__(self) -> int:
        return len(self.pts_ms)

    @property
    def total_duration_ms(self) -> int:
        if not self.pts_ms:
            return 0
        return max(
            pts + max(0, duration)
            for pts, duration in zip(self.pts_ms, self.durations_ms)
        )


class MatroskaTimestampReader:
    """
    Lit les PTS d'une piste vidéo via ``ffprobe -show_packets``.

    Choix du flux : la 1ère piste vidéo (``-select_streams v:0``).
    Unité interne : millisecondes (= unité Matroska standard avec
    TimestampScale = 1_000_000 ns).
    """

    def __init__(self, *, ffprobe_bin: str = "ffprobe") -> None:
        self._ffprobe = ffprobe_bin

    def read(self, source: Path, *, sort_by_pts: bool = True) -> TimestampSequence:
        """
        Renvoie la séquence des PTS de la piste vidéo de ``source``.

        Lève RuntimeError si ffprobe est indisponible ou si aucun packet
        vidéo n'a été trouvé.

        ``sort_by_pts`` garde le comportement historique : ordre de
        présentation croissant. Pour remuxer un HEVC brut déjà encodé en
        ordre de décodage (B-frames), passer ``False`` afin de conserver
        l'ordre packet et d'associer chaque AU au PTS source correspondant.
        """
        cmd = [
            self._ffprobe, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "packet=pts_time,duration_time",
            "-of", "json",
        ]
        cmd.extend(ffprobe_input_args(source))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError) as exc:
            raise RuntimeError(f"ffprobe indisponible : {exc}") from exc
        if result.returncode != 0:
            raise RuntimeError(
                f"ffprobe a échoué (rc={result.returncode}) : {result.stderr or ''}"
            )

        try:
            payload = json.loads(result.stdout or "{}")
        except ValueError as exc:
            raise RuntimeError(f"Sortie ffprobe non-JSON : {exc}") from exc

        packets = payload.get("packets") or []
        if not packets:
            raise RuntimeError("Aucun packet vidéo trouvé par ffprobe.")

        pts_ms_list: list[int] = []
        for pkt in packets:
            raw = pkt.get("pts_time")
            if raw is None or raw == "N/A":
                continue
            try:
                pts_s = float(raw)
            except (TypeError, ValueError):
                continue
            pts_ms_list.append(int(round(pts_s * 1000.0)))

        if not pts_ms_list:
            raise RuntimeError("Aucun pts_time exploitable dans les packets ffprobe.")

        ordered_pts = sorted(pts_ms_list) if sort_by_pts else list(pts_ms_list)
        presentation_pts = sorted(pts_ms_list)
        durations_ms_list: list[int] = []
        for i in range(len(presentation_pts) - 1):
            durations_ms_list.append(presentation_pts[i + 1] - presentation_pts[i])
        if durations_ms_list:
            durations_ms_list.append(durations_ms_list[-1])
        else:
            durations_ms_list.append(0)

        if sort_by_pts:
            ordered_durations = durations_ms_list
        else:
            # Réassocie une durée à chaque PTS dans l'ordre packet. En cas de
            # PTS dupliqués, on consomme les durées dans l'ordre d'apparition
            # en présentation.
            by_pts: dict[int, list[int]] = {}
            for pts, duration in zip(presentation_pts, durations_ms_list):
                by_pts.setdefault(pts, []).append(duration)
            default_duration = durations_ms_list[-1] if durations_ms_list else 0
            ordered_durations = []
            for pts in ordered_pts:
                bucket = by_pts.get(pts)
                ordered_durations.append(
                    bucket.pop(0) if bucket else default_duration
                )

        return TimestampSequence(
            pts_ms=tuple(ordered_pts),
            durations_ms=tuple(ordered_durations),
        )


__all__ = [
    "MatroskaTimestampReader",
    "TimestampSequence",
]
