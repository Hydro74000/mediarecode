"""
core/matroska/editors/track_flags.py

Post-action EBML qui applique le ``FlagEnabled`` (0xB9) des TrackEntry d'un
MKV produit par ffmpeg.

FFmpeg n'expose aucune disposition correspondant à ``FlagEnabled`` (voir
``ffmpeg -dispositions``) et son muxer Matroska n'émet jamais cet élément :
une piste désactivée demandée par l'utilisateur — ou déjà désactivée en
source — ressortirait systématiquement activée. Ce patch rétablit la valeur
voulue après le mux, sans toucher au reste du conteneur.

Les autres flags (default, forced, hearing/visual impaired, original,
commentary) sont écrits par ffmpeg via ``-disposition`` et ne sont donc pas
concernés.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..ids import FLAG_ENABLED_ID, TRACKS_ID, TRACK_ENTRY_ID
from .segment_info import (
    MatroskaSegmentInfoHeaderEditor,
    MatroskaSegmentInfoHeaderEditorOptions,
)


@dataclass(frozen=True)
class TrackEnabledFix:
    track_position: int
    enabled_before: bool
    enabled_after: bool


@dataclass(frozen=True)
class MatroskaTrackEnabledPatchResult:
    applied: bool
    skipped: bool
    reason: str = ""
    fixes: tuple[TrackEnabledFix, ...] = ()
    bytes_delta: int = 0


class MatroskaTrackEnabledEditor:
    """Force le FlagEnabled des TrackEntry aux valeurs demandées."""

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
        enabled_by_position: dict[int, bool],
    ) -> MatroskaTrackEnabledPatchResult:
        """``enabled_by_position`` : FlagEnabled voulu, indexé sur l'ordre des pistes."""
        try:
            return self._apply_impl(path, enabled_by_position)
        except Exception as exc:
            return MatroskaTrackEnabledPatchResult(
                applied=False,
                skipped=True,
                reason=f"Patch FlagEnabled ignoré: {exc}",
            )

    # ------------------------------------------------------------------
    # Implementation
    # ------------------------------------------------------------------

    def _apply_impl(
        self,
        path: Path,
        enabled_by_position: dict[int, bool],
    ) -> MatroskaTrackEnabledPatchResult:
        if not path.is_file():
            raise ValueError(f"Fichier introuvable: {path}")
        wanted = {int(position): bool(value) for position, value in enabled_by_position.items()}
        if not wanted:
            return MatroskaTrackEnabledPatchResult(
                applied=False, skipped=False, reason="Aucun FlagEnabled à appliquer.",
            )

        ed = self._editor
        with path.open("rb") as handle:
            state = ed._analyze_file(handle, parse_fast=ed.options.parse_fast)
            tracks = next(
                (
                    item for item in state.data
                    if item.element_id == TRACKS_ID and not item.unknown_size
                ),
                None,
            )
            if tracks is None:
                return MatroskaTrackEnabledPatchResult(
                    applied=False, skipped=True, reason="Élément Tracks absent.",
                )
            old_payload = ed._read_exact(handle, tracks.payload_offset, tracks.size)

        new_payload, fixes = self._rebuild_tracks_payload(old_payload, wanted)
        if not fixes:
            return MatroskaTrackEnabledPatchResult(
                applied=False,
                skipped=False,
                reason="FlagEnabled déjà conforme.",
            )

        new_payload = ed._refresh_crc32_in_payload(new_payload)
        new_size = ed._encode_ebml_size_prefer_length(
            len(new_payload), preferred_length=tracks.size_len,
        )
        delta = ed.replace_level1_element(
            path,
            element_id=TRACKS_ID,
            new_element_bytes=tracks.element_id + new_size + new_payload,
        )
        return MatroskaTrackEnabledPatchResult(
            applied=True,
            skipped=False,
            reason="FlagEnabled appliqué aux TrackEntry.",
            fixes=tuple(fixes),
            bytes_delta=delta,
        )

    # ------------------------------------------------------------------
    # Tracks payload rebuilder
    # ------------------------------------------------------------------

    def _rebuild_tracks_payload(
        self,
        payload: bytes,
        wanted: dict[int, bool],
    ) -> tuple[bytes, list[TrackEnabledFix]]:
        ed = self._editor
        out = bytearray()
        fixes: list[TrackEnabledFix] = []
        position = 0

        cursor = 0
        while cursor < len(payload):
            child = ed._read_ebml_element_from_bytes(payload, cursor)
            if child.unknown_size or child.end > len(payload):
                out.extend(payload[cursor:])
                break
            if child.element_id != TRACK_ENTRY_ID:
                out.extend(payload[child.offset:child.end])
                cursor = child.end
                continue

            entry_bytes = payload[child.offset:child.end]
            target = wanted.get(position)
            position += 1
            if target is None:
                out.extend(entry_bytes)
                cursor = child.end
                continue

            new_entry, fix = self._rewrite_track_entry(entry_bytes, target, position - 1)
            out.extend(new_entry)
            if fix is not None:
                fixes.append(fix)
            cursor = child.end

        return bytes(out), fixes

    def _rewrite_track_entry(
        self,
        entry_bytes: bytes,
        enabled: bool,
        track_position: int,
    ) -> tuple[bytes, TrackEnabledFix | None]:
        """Réécrit un TrackEntry pour porter exactement ``enabled``.

        L'élément est remplacé quand il existe, sinon inséré en tête du
        payload (l'ordre des enfants d'un TrackEntry est libre en EBML).
        """
        ed = self._editor
        entry = ed._read_ebml_element_from_bytes(entry_bytes, 0)
        payload_start, payload_end = entry.payload_offset, entry.end

        observed = True
        existing: tuple[int, int] | None = None
        cursor = payload_start
        while cursor < payload_end:
            child = ed._read_ebml_element_from_bytes(entry_bytes, cursor)
            if child.unknown_size or child.end > payload_end:
                break
            if child.element_id == FLAG_ENABLED_ID:
                existing = (child.offset, child.end)
                raw_value = entry_bytes[child.payload_offset:child.end]
                observed = bool(int.from_bytes(raw_value, "big")) if raw_value else True
            cursor = child.end

        if observed == enabled:
            return entry_bytes, None

        new_flag = FLAG_ENABLED_ID + ed._encode_ebml_size_prefer_length(
            1, preferred_length=1,
        ) + bytes([int(enabled)])
        if existing is None:
            new_payload = new_flag + entry_bytes[payload_start:payload_end]
        else:
            start, end = existing
            new_payload = (
                entry_bytes[payload_start:start] + new_flag + entry_bytes[end:payload_end]
            )
        new_size = ed._encode_ebml_size_prefer_length(
            len(new_payload), preferred_length=entry.size_len,
        )
        return (
            entry.element_id + new_size + new_payload,
            TrackEnabledFix(track_position, observed, enabled),
        )


__all__ = [
    "MatroskaTrackEnabledEditor",
    "MatroskaTrackEnabledPatchResult",
    "TrackEnabledFix",
]
