"""Shared mapping and metadata helpers for the remux workflow."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.bluray import append_ffmpeg_input_args
from core.workflows.common.metadata import (
    STREAM_SPEC_BY_TRACK_TYPE as STREAM_SPEC_BY_TYPE,
    disposition_value,
    normalize_track_language_from_track,
    resolve_global_tags,
)
from core.workflows.common.timeline_sync import needs_strict_interleave as common_needs_strict_interleave
from core.workflows.remux_models import RemuxConfig, RemuxError, TrackEntry


@dataclass(frozen=True)
class MappedTrack:
    source_input_idx: int
    source_file_index: int
    source_path: Path | str
    stream_index: int
    track: TrackEntry
    out_type_index: int


@dataclass(frozen=True)
class OffsetInputSpec:
    map_key: tuple[int, int, str, int]
    input_path: Path | str
    input_stream_index: int
    offset_ms: int


@dataclass(frozen=True)
class RemuxMetadataContext:
    chapter_map: str
    metadata_map: str
    global_tags: dict[str, str]


def is_dir_writable(path: Path) -> bool:
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path,
            prefix="muxiveo_write_probe_",
            delete=True,
        ):
            pass
        return True
    except OSError:
        return False


def offset_seconds(offset_ms: int) -> str:
    return f"{abs(int(offset_ms)) / 1000.0:.3f}"


def track_order_parts(
    item: tuple[int, int] | tuple[int, int, str],
) -> tuple[int, int, str | None]:
    if len(item) >= 3:
        file_index, mkv_tid, entry_id = item[0], item[1], str(item[2] or "").strip()
        return int(file_index), int(mkv_tid), entry_id or None
    return int(item[0]), int(item[1]), None


def resolve_mapped_tracks(config: RemuxConfig) -> list[MappedTrack]:
    file_index_to_input_idx = {
        src.file_index: i
        for i, src in enumerate(config.sources)
    }

    track_map_by_id = {
        (src.file_index, track.entry_id): (src.path, track)
        for src in config.sources
        for track in src.tracks
    }
    track_map_by_pair: dict[tuple[int, int], list[tuple[Path, TrackEntry]]] = {}
    for src in config.sources:
        for track in src.tracks:
            track_map_by_pair.setdefault((src.file_index, track.mkv_tid), []).append((src.path, track))

    type_counters: dict[str, int] = {"video": 0, "audio": 0, "subtitle": 0}
    mapped: list[MappedTrack] = []

    for order_item in config.track_order:
        file_index, mkv_tid, entry_id = track_order_parts(order_item)
        input_idx = file_index_to_input_idx.get(file_index)
        if input_idx is None:
            raise RemuxError(f"Source inconnue dans track_order : file_index={file_index}")

        found = (
            track_map_by_id.get((file_index, entry_id))
            if entry_id
            else next(iter(track_map_by_pair.get((file_index, mkv_tid), [])), None)
        )
        if found is None:
            raise RemuxError(
                "Piste introuvable dans track_order : "
                f"file_index={file_index}, stream={mkv_tid}"
            )
        src_path, track = found
        if track.track_type not in STREAM_SPEC_BY_TYPE:
            raise RemuxError(
                "Type de piste non supporté par le remux : "
                f"{track.track_type} (file_index={file_index}, stream={mkv_tid})"
            )

        out_type_index = type_counters[track.track_type]
        type_counters[track.track_type] += 1
        mapped.append(MappedTrack(
            source_input_idx=input_idx,
            source_file_index=file_index,
            source_path=src_path,
            stream_index=mkv_tid,
            track=track,
            out_type_index=out_type_index,
        ))

    return mapped


def offset_input_specs_from_mapped_tracks(mapped_tracks: list[MappedTrack]) -> list[OffsetInputSpec]:
    specs: list[OffsetInputSpec] = []
    for mapped_track in mapped_tracks:
        offset_ms = int(getattr(mapped_track.track, "time_shift_ms", 0) or 0)
        if offset_ms == 0:
            continue
        if mapped_track.track.track_type == "video" and offset_ms < 0:
            raise RemuxError(
                "Décalage vidéo négatif interdit : "
                f"file_index={mapped_track.source_file_index}, stream={mapped_track.stream_index}, offset={offset_ms} ms"
            )
        specs.append(OffsetInputSpec(
            map_key=(
                int(mapped_track.source_file_index),
                int(mapped_track.stream_index),
                str(mapped_track.track.track_type),
                int(mapped_track.out_type_index),
            ),
            input_path=mapped_track.source_path,
            input_stream_index=int(mapped_track.stream_index),
            offset_ms=offset_ms,
        ))
    return specs


def append_offset_inputs(
    cmd: list[str],
    specs: list[OffsetInputSpec],
    *,
    start_input_index: int,
    cli_path: Callable[[Path | str], str],
) -> tuple[int, dict[tuple[int, int, str, int], tuple[int, int]]]:
    next_input_index = start_input_index
    input_by_key: dict[tuple[str, int, int, str], int] = {}
    remap: dict[tuple[int, int, str, int], tuple[int, int]] = {}

    for spec in specs:
        input_key = (
            cli_path(spec.input_path),
            int(spec.input_stream_index),
            int(spec.offset_ms),
            str(spec.map_key[2]),
        )
        input_idx = input_by_key.get(input_key)
        if input_idx is None:
            if int(spec.offset_ms) > 0:
                cmd.extend(["-itsoffset", offset_seconds(spec.offset_ms)])
                append_ffmpeg_input_args(cmd, spec.input_path, cli_path=cli_path)
            else:
                cmd.extend(["-ss", offset_seconds(spec.offset_ms)])
                append_ffmpeg_input_args(cmd, spec.input_path, cli_path=cli_path)
            input_idx = next_input_index
            input_by_key[input_key] = input_idx
            next_input_index += 1

        remap[spec.map_key] = (int(input_idx), int(spec.input_stream_index))

    return next_input_index, remap


def needs_strict_interleave(mapped_tracks: list[MappedTrack]) -> bool:
    return common_needs_strict_interleave(mapped_tracks)


def requires_file_sync_fallback_for_offsets(mapped_tracks: list[MappedTrack]) -> bool:
    primary_video = next((mapped_track for mapped_track in mapped_tracks if mapped_track.track.track_type == "video"), None)
    if primary_video is None:
        return False

    return any(
        mapped_track.track.track_type in {"audio", "subtitle"}
        and mapped_track.source_file_index != primary_video.source_file_index
        and int(getattr(mapped_track.track, "time_shift_ms", 0) or 0) != 0
        for mapped_track in mapped_tracks
    )


def chapter_map_value(config: RemuxConfig, chapter_input_index: int | None) -> str:
    if config.chapter_overrides is not None:
        if config.chapter_overrides and chapter_input_index is not None:
            return str(chapter_input_index)
        return "-1"
    if not config.keep_chapters:
        return "-1"
    # 1) Sélection explicite (combo UI) si fournie et valide.
    selected = getattr(config, "chapter_source_index", None)
    if selected is not None and 0 <= selected < len(config.sources):
        if getattr(config.sources[selected], "has_chapters", False):
            return str(selected)
    # 2) Fallback : première source qui possède réellement des chapitres.
    #    Sinon ffmpeg pioche dans l'input 0 et perd les chapitres si elle n'en
    #    contient pas (cas typique : MP4 vidéo + MKV remux porteur des chapitres).
    for input_idx, source in enumerate(config.sources):
        if getattr(source, "has_chapters", False):
            return str(input_idx)
    return "0"


def metadata_map_value(
    config: RemuxConfig,
    chapter_input_index: int | None,
    chapter_map: str | None = None,
) -> str:
    # On dérive le défaut de "métadonnées globales" du même input que celui
    # historiquement utilisé pour les chapitres (input 0 quand keep_chapters
    # et pas d'override) ; chapter_map peut désormais cibler un autre input
    # quand seul celui-ci porte des chapitres, mais on ne veut pas pour
    # autant déplacer la copie des tags globaux : on retombe alors sur "0".
    fallback_map = "0" if config.keep_chapters else "-1"
    if config.tag_overrides is not None:
        if config.chapter_overrides and chapter_input_index is not None:
            return str(chapter_input_index)
        if chapter_map is not None and chapter_map not in ("-1", ""):
            return fallback_map
        return "-1"
    for input_idx, source in enumerate(config.sources):
        if source.copy_tags:
            return str(input_idx)
    if chapter_map is not None and chapter_map not in ("-1", ""):
        return fallback_map
    return "-1"


def resolved_global_tags(config: RemuxConfig) -> dict[str, str]:
    return resolve_global_tags(config.tag_overrides, config.file_title)


def normalized_language_value(track: TrackEntry) -> str:
    return normalize_track_language_from_track(track)


def disposition_value_for_track(track: TrackEntry) -> str:
    return disposition_value(
        flag_default=track.flag_default,
        flag_forced=track.flag_forced,
        flag_hearing_impaired=track.flag_hearing_impaired,
        flag_visual_impaired=track.flag_visual_impaired,
        flag_original=track.flag_original,
        flag_commentary=track.flag_commentary,
        allow_partial=True,
    ) or "0"


def metadata_context(config: RemuxConfig, chapter_input_index: int | None) -> RemuxMetadataContext:
    chap_map = chapter_map_value(config, chapter_input_index)
    return RemuxMetadataContext(
        chapter_map=chap_map,
        metadata_map=metadata_map_value(config, chapter_input_index, chap_map),
        global_tags=resolved_global_tags(config),
    )


__all__ = [
    "MappedTrack",
    "OffsetInputSpec",
    "RemuxMetadataContext",
    "append_offset_inputs",
    "chapter_map_value",
    "disposition_value_for_track",
    "is_dir_writable",
    "metadata_context",
    "metadata_map_value",
    "needs_strict_interleave",
    "normalized_language_value",
    "offset_input_specs_from_mapped_tracks",
    "offset_seconds",
    "requires_file_sync_fallback_for_offsets",
    "resolve_mapped_tracks",
    "resolved_global_tags",
    "track_order_parts",
]
