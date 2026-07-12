"""Media inspection helpers used by CLI commands and config building."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.bluray import discover_titles, playlist_path_for
from core.config import AppConfig
from core.inspector import AttachmentInfo, FileInfo, FileInspector
from core.workflows.remux_models import SourceInput, TrackEntry, tracks_from_file_info

from cli.constants import EXIT_ARGS, EXIT_VALIDATION
from cli.errors import CliError
from cli.logging import Logger
from cli.options import CommonOptions


def source_path_items(job: dict[str, Any], cli_inputs: list[str] | None = None) -> list[dict[str, Any]]:
    if cli_inputs:
        return [{"path": value} for value in cli_inputs]
    raw_sources = job.get("sources")
    if raw_sources is None and job.get("input"):
        raw_sources = [job["input"]]
    if isinstance(raw_sources, (str, Path)):
        raw_sources = [raw_sources]
    if not isinstance(raw_sources, list) or not raw_sources:
        raise CliError("Au moins une source est requise.", EXIT_ARGS)
    items: list[dict[str, Any]] = []
    for item in raw_sources:
        if isinstance(item, dict):
            if not item.get("path"):
                raise CliError("Chaque source JSON doit contenir `path`.", EXIT_ARGS)
            items.append(item)
        else:
            items.append({"path": item})
    return items


def inspect_sources(
    job: dict[str, Any],
    config: AppConfig,
    options: CommonOptions,
    logger: Logger,
    *,
    cli_inputs: list[str] | None = None,
) -> tuple[list[SourceInput], list[FileInfo], list[TrackEntry]]:
    ffprobe = options.ffprobe or job.get("ffprobe") or config.tool_ffprobe
    mediainfo = options.mediainfo or job.get("mediainfo") or config.tool_mediainfo
    inspector = FileInspector(
        ffprobe_bin=str(ffprobe),
        mediainfo_bin=str(mediainfo),
        verbose_output=(lambda line: logger.emit("debug", line)) if options.verbose else None,
    )
    source_items = source_path_items(job, cli_inputs)
    sources: list[SourceInput] = []
    infos: list[FileInfo] = []
    all_tracks: list[TrackEntry] = []

    for source_index, item in enumerate(source_items):
        path = Path(str(item["path"])).expanduser()
        playlist_value = item.get("playlist")
        if playlist_value is not None:
            try:
                playlist_id = int(playlist_value)
            except (TypeError, ValueError) as exc:
                raise CliError(f"Playlist Blu-ray invalide : {playlist_value}", EXIT_VALIDATION) from exc
            playlist_path = playlist_path_for(path, playlist_id)
            if playlist_path is None:
                raise CliError(
                    f"Playlist Blu-ray introuvable : {playlist_id:05d}.mpls dans {path}",
                    EXIT_VALIDATION,
                )
            path = playlist_path
        elif path.is_dir():
            titles = discover_titles(path, min_duration_s=60.0)
            if titles:
                path = titles[0].playlist_path
        if not path.exists():
            raise CliError(f"Source introuvable : {path}", EXIT_VALIDATION)
        info = inspector.inspect(path)
        file_id = f"src{source_index}"
        tracks = tracks_from_file_info(info, file_id=file_id)
        infos.append(info)
        attachment_selection = selected_attachments(info, item)
        source = SourceInput(
            path=path,
            file_index=source_index,
            tracks=tracks,
            selected_attachments=attachment_selection,
            attachment_count=len(info.attachments),
            copy_tags=bool(item.get("copy_tags", False)),
            has_chapters=bool(info.chapters and info.chapters.entries),
        )
        sources.append(source)
        all_tracks.extend(tracks)
    return sources, infos, all_tracks


def selected_attachments(info: FileInfo, item: dict[str, Any]) -> list[AttachmentInfo]:
    selection = item.get("attachments", "none")
    if selection is True or selection == "all":
        return list(info.attachments)
    if selection in (False, None, "none"):
        return []
    names: set[str] = set()
    indices: set[int] = set()
    if isinstance(selection, list):
        for entry in selection:
            if isinstance(entry, int):
                indices.add(entry)
            else:
                names.add(str(entry))
    return [
        att
        for att in info.attachments
        if att.local_index in indices or att.index in indices or att.filename in names
    ]


def config_template_from_info(info: FileInfo, *, output: str = "") -> dict[str, Any]:
    return {
        "version": 1,
        "kind": "exact-job",
        "sources": [{"path": str(info.path), "attachments": "none", "copy_tags": False}],
        "output": output or str(info.path.with_suffix(".remux.mkv")),
        "chapters": {"source_index": 0},
    }
