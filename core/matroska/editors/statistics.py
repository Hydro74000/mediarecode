"""
core/matroska/editors/statistics.py

Post-action EBML qui régénère les statistiques de pistes (BPS, DURATION,
NUMBER_OF_FRAMES, NUMBER_OF_BYTES) d'un MKV produit par ffmpeg.

FFmpeg n'écrit qu'un tag ``DURATION`` par piste ; les autres statistiques ne
subsistent que là où ``-map_metadata:s`` a recopié celles de la source —
valeurs alors obsolètes (sélection de pistes, remap, décalage). MediaInfo ne
promeut ``NUMBER_OF_FRAMES`` en « Count of elements » que lorsque les tags
compagnons ``_STATISTICS_*`` accompagnent la valeur : sans eux, le compte
d'éléments des sous-titres disparaît de l'analyse (MediaInfo comme Muxiveo).

Ce module, après mux ffmpeg :
- mesure les paquets réellement écrits (un seul parcours du fichier),
- retire les statistiques héritées des pistes mesurées,
- réécrit l'élément Tags via
  ``MatroskaSegmentInfoHeaderEditor.replace_level1_element``.

Le parcours produit aussi le résumé de paquets consommé par
``validate_matroska_output`` : la validation qui suit n'a plus à relire le
fichier.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..ids import TAGS_ID
from ..reader import MatroskaReader
from ..validation import MatroskaPacketValidation
from ..writer import merge_track_statistics_tags
from .segment_info import (
    MatroskaSegmentInfoHeaderEditor,
    MatroskaSegmentInfoHeaderEditorOptions,
)


@dataclass
class _TrackAccumulator:
    """Compteurs d'une piste pendant le parcours des paquets."""

    frame_count: int = 0
    payload_bytes: int = 0
    duration_ns: int = 0
    last_timestamp_ns: int = -1
    last_delta_ns: int = 0


@dataclass(frozen=True)
class MatroskaStatisticsPatchResult:
    applied: bool
    skipped: bool
    reason: str = ""
    track_count: int = 0
    bytes_delta: int = 0
    #: Résumé des paquets observés, réutilisable par la validation sémantique.
    packet_validation: MatroskaPacketValidation | None = None


class MatroskaTrackStatisticsEditor:
    """Régénère les statistiques de pistes d'un MKV depuis ses paquets."""

    def __init__(
        self,
        *,
        editor: MatroskaSegmentInfoHeaderEditor | None = None,
        scan_workers: int | None = None,
    ) -> None:
        self._editor = editor or MatroskaSegmentInfoHeaderEditor(
            options=MatroskaSegmentInfoHeaderEditorOptions(fallback_mode="skip")
        )
        # Le parcours coûte une entrée/sortie par bloc : plusieurs lectures en
        # vol divisent le temps sur un SSD sans changer le résultat (les
        # tranches restent consommées dans l'ordre du fichier).
        self._scan_workers = (
            max(1, min(8, os.cpu_count() or 1)) if scan_workers is None else max(1, scan_workers)
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def apply(
        self,
        path: Path,
        *,
        writing_app: str = "Muxiveo",
        statistics_by_position: dict[int, tuple[int, int, int]] | None = None,
    ) -> MatroskaStatisticsPatchResult:
        """``statistics_by_position`` évite de mesurer : valeurs déjà connues.

        Utilisé quand la sortie recopie les frames de ses sources à
        l'identique — les compteurs sont alors ceux des pistes sources.
        """
        try:
            return self._apply_impl(
                path,
                writing_app=writing_app,
                statistics_by_position=statistics_by_position,
            )
        except Exception as exc:
            return MatroskaStatisticsPatchResult(
                applied=False,
                skipped=True,
                reason=f"Patch statistiques ignoré: {exc}",
            )

    # ------------------------------------------------------------------
    # Implementation
    # ------------------------------------------------------------------

    def _apply_impl(
        self,
        path: Path,
        *,
        writing_app: str,
        statistics_by_position: dict[int, tuple[int, int, int]] | None = None,
    ) -> MatroskaStatisticsPatchResult:
        if not path.is_file():
            raise ValueError(f"Fichier introuvable: {path}")

        reader = MatroskaReader(path)
        reader.segment()
        tracks = reader.tracks()
        if not tracks:
            return MatroskaStatisticsPatchResult(
                applied=False, skipped=True, reason="Aucune piste Matroska.",
            )
        uid_by_number = {track.number: track.uid for track in tracks if track.uid}
        default_duration_ns = {track.number: track.default_duration_ns for track in tracks}

        packet_validation: MatroskaPacketValidation | None = None
        if statistics_by_position is not None and self._supplied_values_fit(
            reader, tracks, statistics_by_position,
        ):
            statistics = {
                track.uid: statistics_by_position[position]
                for position, track in enumerate(tracks)
                if track.uid and statistics_by_position[position][0] > 0
            }
        else:
            accumulators, packet_validation = self._measure(
                reader, default_duration_ns=default_duration_ns, workers=self._scan_workers,
            )
            statistics = {
                uid_by_number[number]: (
                    item.frame_count, item.payload_bytes, item.duration_ns,
                )
                for number, item in accumulators.items()
                if number in uid_by_number and item.frame_count > 0
            }
        if not statistics:
            return MatroskaStatisticsPatchResult(
                applied=False,
                skipped=False,
                reason="Aucun paquet mesuré : statistiques inchangées.",
                packet_validation=packet_validation,
            )

        new_tags_element = merge_track_statistics_tags(
            reader.raw_top_level(TAGS_ID),
            statistics,
            writing_app=writing_app,
        )
        if not new_tags_element:
            return MatroskaStatisticsPatchResult(
                applied=False,
                skipped=True,
                reason="Élément Tags vide après fusion.",
                packet_validation=packet_validation,
            )

        delta = self._editor.replace_level1_element(
            path,
            element_id=TAGS_ID,
            new_element_bytes=new_tags_element,
        )
        return MatroskaStatisticsPatchResult(
            applied=True,
            skipped=False,
            reason="Statistiques de pistes régénérées.",
            track_count=len(statistics),
            bytes_delta=delta,
            packet_validation=packet_validation,
        )

    #: Écart toléré entre la durée annoncée par les valeurs fournies et celle
    #: du segment écrit. Au-delà, la sortie ne correspond plus aux sources
    #: (troncature, paquets perdus) et doit être mesurée.
    _SUPPLIED_DURATION_TOLERANCE_NS = 2_000_000_000

    @classmethod
    def _supplied_values_fit(
        cls,
        reader: MatroskaReader,
        tracks: list,
        statistics_by_position: dict[int, tuple[int, int, int]],
    ) -> bool:
        """Vérifie que des valeurs fournies décrivent bien la sortie écrite."""
        if len(statistics_by_position) != len(tracks):
            return False
        if any(position not in statistics_by_position for position in range(len(tracks))):
            return False
        try:
            segment_duration_ns = reader.segment_duration_ns()
        except (OSError, ValueError):
            return False
        if not segment_duration_ns:
            return False
        longest_ns = max(values[2] for values in statistics_by_position.values())
        return abs(segment_duration_ns - longest_ns) <= cls._SUPPLIED_DURATION_TOLERANCE_NS

    @staticmethod
    def _measure(
        reader: MatroskaReader,
        *,
        default_duration_ns: dict[int, int],
        workers: int = 1,
    ) -> tuple[dict[int, _TrackAccumulator], MatroskaPacketValidation]:
        """Parcourt les paquets une fois : statistiques + résumé de validation.

        Les statistiques suivent la sémantique de l'assembleur natif (durée
        implicite dérivée du DefaultDuration puis du dernier écart) ; le
        résumé de validation garde celle de ``validate_matroska_output``
        (durées explicites uniquement).
        """
        accumulators: dict[int, _TrackAccumulator] = {}
        max_packet_timestamp_ns: int | None = None
        last_delta_by_track: dict[int, int] = {}
        for block in reader.block_summaries(workers=workers):
            item = accumulators.setdefault(block.track_number, _TrackAccumulator())
            timestamp_ns = block.timestamp_ns
            explicit_duration_ns = block.duration_ns
            item.frame_count += block.frame_count
            item.payload_bytes += block.payload_bytes
            if timestamp_ns > item.last_timestamp_ns >= 0:
                item.last_delta_ns = timestamp_ns - item.last_timestamp_ns
                last_delta_by_track[block.track_number] = item.last_delta_ns
            item.last_timestamp_ns = timestamp_ns

            duration_ns = explicit_duration_ns
            if duration_ns is None:
                track_default_ns = default_duration_ns.get(block.track_number, 0)
                if track_default_ns:
                    duration_ns = track_default_ns * max(1, block.frame_count)
            if duration_ns is None:
                duration_ns = item.last_delta_ns
            item.duration_ns = max(item.duration_ns, timestamp_ns + duration_ns)

            packet_end = timestamp_ns + (explicit_duration_ns or 0)
            max_packet_timestamp_ns = max(max_packet_timestamp_ns or packet_end, packet_end)
        return accumulators, MatroskaPacketValidation(
            track_numbers=frozenset(accumulators),
            max_packet_timestamp_ns=max_packet_timestamp_ns,
            last_delta_by_track=last_delta_by_track,
        )


__all__ = [
    "MatroskaStatisticsPatchResult",
    "MatroskaTrackStatisticsEditor",
]
