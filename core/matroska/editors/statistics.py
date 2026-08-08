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
    ) -> None:
        self._editor = editor or MatroskaSegmentInfoHeaderEditor(
            options=MatroskaSegmentInfoHeaderEditorOptions(fallback_mode="skip")
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def apply(
        self,
        path: Path,
        *,
        writing_app: str = "Muxiveo",
    ) -> MatroskaStatisticsPatchResult:
        try:
            return self._apply_impl(path, writing_app=writing_app)
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

        accumulators, packet_validation = self._measure(
            reader, default_duration_ns=default_duration_ns,
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

    @staticmethod
    def _measure(
        reader: MatroskaReader,
        *,
        default_duration_ns: dict[int, int],
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
        for block in reader.blocks():
            item = accumulators.setdefault(block.track_number, _TrackAccumulator())
            timestamp_ns = (
                block.timestamp_ns
                if block.timestamp_ns is not None
                else block.timestamp_ms * 1_000_000
            )
            explicit_duration_ns = (
                block.duration_ns
                if block.duration_ns is not None
                else ((block.duration_ms or 0) * 1_000_000 if block.duration_ms is not None else None)
            )
            item.frame_count += 1
            item.payload_bytes += len(block.payload)
            if timestamp_ns > item.last_timestamp_ns >= 0:
                item.last_delta_ns = timestamp_ns - item.last_timestamp_ns
                last_delta_by_track[block.track_number] = item.last_delta_ns
            item.last_timestamp_ns = timestamp_ns

            duration_ns = explicit_duration_ns
            if duration_ns is None:
                track_default_ns = default_duration_ns.get(block.track_number, 0)
                if track_default_ns:
                    duration_ns = track_default_ns * max(1, block.lace_count)
            if duration_ns is None:
                duration_ns = item.last_delta_ns
            item.duration_ns = max(item.duration_ns, timestamp_ns + duration_ns)

            packet_end = timestamp_ns + (
                block.duration_ns or ((block.duration_ms or 0) * 1_000_000)
            )
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
