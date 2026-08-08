"""Résolution unique des métadonnées de pistes d'une sortie encode.

La commande FFmpeg, le contrat de validation et l'assembleur natif consomment
exactement ce plan. Une piste Matroska lisible fournit les valeurs implicites ;
les ``TrackMetaEdit`` positionnels les surchargent ensuite.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Iterable

from core.matroska.reader import MatroskaReader, MatroskaTrack
from core.workflows.encode.models import EncodeConfig

from .plan_models import PlannedTrackFlags, PlannedTrackMetadata


_TRACK_TYPE_LABELS = {1: "video", 2: "audio", 17: "subtitle"}
_MATROSKA_EXTENSIONS = {".mkv", ".webm", ".mka", ".mks", ".mk3d"}
_EBML_HEADER = b"\x1a\x45\xdf\xa3"


def looks_like_matroska(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) == _EBML_HEADER
    except OSError:
        return False


def source_matroska_track(path: Path, stream_index: int) -> MatroskaTrack | None:
    """Retourne la piste quand l'index ffprobe est positionnel dans un MKV."""
    path = Path(path)
    if (
        path.suffix.lower() not in _MATROSKA_EXTENSIONS
        or not path.is_file()
        or not looks_like_matroska(path)
    ):
        return None
    try:
        tracks = MatroskaReader(path).tracks()
    except (OSError, ValueError):
        return None
    index = int(stream_index)
    return tracks[index] if 0 <= index < len(tracks) else None


def _metadata_for_ref(track_type: str, source: Path, stream_index: int) -> PlannedTrackMetadata:
    observed = source_matroska_track(source, stream_index)
    if observed is None or _TRACK_TYPE_LABELS.get(observed.track_type) != track_type:
        return PlannedTrackMetadata(track_type, Path(source), int(stream_index))
    return PlannedTrackMetadata(
        track_type=track_type,
        source=Path(source),
        stream_index=int(stream_index),
        name=observed.name,
        language=observed.language_bcp47 or observed.language,
        flags=PlannedTrackFlags(
            enabled=observed.flag_enabled,
            default=observed.flag_default,
            forced=observed.flag_forced,
            hearing_impaired=observed.flag_hearing_impaired,
            visual_impaired=observed.flag_visual_impaired,
            original=observed.flag_original,
            commentary=observed.flag_commentary,
        ),
    )


def resolve_track_metadata(
    config: EncodeConfig,
    *,
    video_refs: Iterable[tuple[Path, int]],
    subtitle_refs: Iterable[tuple[Path, int]],
) -> tuple[PlannedTrackMetadata, ...]:
    """Résout les valeurs source puis applique les éditions positionnelles."""
    resolved: list[PlannedTrackMetadata] = []
    for source, stream_index in video_refs:
        resolved.append(_metadata_for_ref("video", Path(source), int(stream_index)))
    for audio in config.audio_tracks:
        resolved.append(_metadata_for_ref(
            "audio",
            Path(audio.source_path or config.source),
            int(audio.stream_index),
        ))
    for source, stream_index in subtitle_refs:
        resolved.append(_metadata_for_ref("subtitle", Path(source), int(stream_index)))

    for patch in config.track_meta_edits:
        position = int(patch.track_order) - 1
        if not 0 <= position < len(resolved):
            continue
        current = resolved[position]
        flags = current.flags or PlannedTrackFlags()
        patched_flags = replace(
            flags,
            enabled=patch.flag_enabled if patch.flag_enabled is not None else flags.enabled,
            default=patch.flag_default if patch.flag_default is not None else flags.default,
            forced=patch.flag_forced if patch.flag_forced is not None else flags.forced,
            hearing_impaired=(
                patch.flag_hearing_impaired
                if patch.flag_hearing_impaired is not None
                else flags.hearing_impaired
            ),
            visual_impaired=(
                patch.flag_visual_impaired
                if patch.flag_visual_impaired is not None
                else flags.visual_impaired
            ),
            original=patch.flag_original if patch.flag_original is not None else flags.original,
            commentary=(
                patch.flag_commentary
                if patch.flag_commentary is not None
                else flags.commentary
            ),
        )
        has_known_flags = any(
            value is not None
            for value in (
                patched_flags.enabled,
                patched_flags.default,
                patched_flags.forced,
                patched_flags.hearing_impaired,
                patched_flags.visual_impaired,
                patched_flags.original,
                patched_flags.commentary,
            )
        )
        language = (patch.language or "").strip()
        resolved[position] = replace(
            current,
            name=patch.title if patch.title is not None else current.name,
            language=language if language else current.language,
            flags=patched_flags if has_known_flags else None,
        )
    return tuple(resolved)


__all__ = [
    "looks_like_matroska",
    "resolve_track_metadata",
    "source_matroska_track",
]
