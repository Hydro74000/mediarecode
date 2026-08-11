"""Statistiques de pistes dérivées des sources, sans relire la sortie.

Un remux en copie stricte recopie les frames telles quelles : les compteurs
d'une piste de sortie sont exactement ceux de la piste source. Quand la
source les publie déjà (tags ``_STATISTICS_*`` écrits par le muxeur qui l'a
produite), ils sont réutilisables immédiatement — le scan complet de la
sortie devient inutile.

Toute condition non remplie (réencodage, décalage, conversion, source non
Matroska, statistiques absentes ou partielles) fait retourner ``None`` :
l'appelant retombe alors sur la mesure de la sortie.
"""

from __future__ import annotations

import re
from pathlib import Path

from core.matroska.reader import MatroskaReader


#: Tags requis pour reconstruire (frames, octets, durée) sans mesurer.
_REQUIRED_TAGS = ("NUMBER_OF_FRAMES", "NUMBER_OF_BYTES", "DURATION")
_TRACK_UID_TARGET = "63c5"
_DURATION_RE = re.compile(r"^(\d+):(\d{2}):(\d{2})(?:\.(\d{1,9}))?$")
_MATROSKA_SUFFIXES = frozenset({".mkv", ".webm", ".mka", ".mks", ".mk3d"})


def parse_statistics_duration_ns(value: str) -> int | None:
    """``HH:MM:SS.nnnnnnnnn`` → nanosecondes."""
    match = _DURATION_RE.match(str(value or "").strip())
    if match is None:
        return None
    hours, minutes, seconds, fraction = match.groups()
    total = (int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1_000_000_000
    return total + int((fraction or "").ljust(9, "0"))


def source_statistics_by_position(path: Path) -> dict[int, tuple[int, int, int]]:
    """``(frames, octets, durée ns)`` par position de piste d'une source."""
    path = Path(path)
    if path.suffix.lower() not in _MATROSKA_SUFFIXES or not path.is_file():
        return {}
    try:
        reader = MatroskaReader(path)
        position_by_uid = {
            track.uid: position for position, track in enumerate(reader.tracks()) if track.uid
        }
        tags = reader.tags()
    except (OSError, ValueError):
        return {}

    statistics: dict[int, tuple[int, int, int]] = {}
    for tag in tags:
        uid = tag.targets.get(_TRACK_UID_TARGET, 0)
        position = position_by_uid.get(uid)
        if position is None:
            continue
        values = {name.upper(): text for name, text in tag.values}
        if not all(name in values for name in _REQUIRED_TAGS):
            continue
        duration_ns = parse_statistics_duration_ns(values["DURATION"])
        if duration_ns is None:
            continue
        try:
            frames = int(values["NUMBER_OF_FRAMES"])
            payload_bytes = int(values["NUMBER_OF_BYTES"])
        except ValueError:
            continue
        if frames > 0 and payload_bytes >= 0:
            statistics[position] = (frames, payload_bytes, duration_ns)
    return statistics


def derive_output_statistics(
    refs: list[tuple[Path, int]],
) -> dict[int, tuple[int, int, int]] | None:
    """Statistiques de sortie par position, ``None`` si une piste manque.

    ``refs`` liste, dans l'ordre de sortie, la source et l'index de piste de
    chaque piste copiée sans transformation.
    """
    if not refs:
        return None
    cache: dict[Path, dict[int, tuple[int, int, int]]] = {}
    derived: dict[int, tuple[int, int, int]] = {}
    for position, (source, stream_index) in enumerate(refs):
        source = Path(source)
        if source not in cache:
            cache[source] = source_statistics_by_position(source)
        values = cache[source].get(int(stream_index))
        if values is None:
            return None
        derived[position] = values
    return derived


__all__ = [
    "derive_output_statistics",
    "parse_statistics_duration_ns",
    "source_statistics_by_position",
]
