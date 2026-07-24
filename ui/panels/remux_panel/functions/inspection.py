"""Logique d'inspection/gestion des sources pour RemuxPanel."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from core.i18n import translate_text
from core.inspector import FileInfo, FileInspector, InspectionError
from core.workflows.remux_models import tracks_from_file_info
from ui.panels.remux_panel.models import SourceFile, _pick_file_color
from ui.panels.remux_panel.theme import _C

if TYPE_CHECKING:
    from ui.panels.remux_panel.panel import RemuxPanel


def find_source(panel: "RemuxPanel", file_id: str) -> SourceFile | None:
    return next((sf for sf in panel._source_files if sf.id == file_id), None)


def has_ready_files(panel: "RemuxPanel") -> bool:
    return any(sf.info is not None for sf in panel._source_files)


def on_add_files(panel: "RemuxPanel", paths: list[str]) -> None:
    for path_str in paths:
        path = Path(path_str)
        if any(sf.path == path for sf in panel._source_files):
            panel.log_message.emit("WARN", translate_text("{name} est déjà dans la liste.", name=path.name))
            continue

        color = _pick_file_color(panel._color_index)
        panel._color_index += 1
        sf = SourceFile(id=str(uuid.uuid4()), path=path, color=color)
        panel._source_files.append(sf)

        name = path.name
        short = name[:18] + "…" if len(name) > 20 else name
        panel._source_names[sf.id] = short
        panel._source_colors[sf.id] = color

        panel._file_list.add_file(sf)
        panel.log_message.emit(
            "INFO", translate_text("Inspection de {name} (ffprobe + mediainfo)…", name=path.name)
        )
        # L'inspection est indépendante des calculs de synchronisation audio.
        # Un calcul long ne doit pas mettre les nouveaux drops en attente.
        panel._inspection_executor.submit(panel._inspect_file, sf.id, path)

    panel._sync_tmdb_suggested_title()


def inspect_file(panel: "RemuxPanel", file_id: str, path: Path) -> None:
    try:
        inspector = FileInspector(
            ffprobe_bin=panel._config.tool_ffprobe,
            mediainfo_bin=panel._config.tool_mediainfo,
            verbose_output=lambda line: panel.tool_output.emit("inspector", line),
        )
        info = inspector.inspect(path)
        panel._inspection_done.emit(file_id, info)
    except InspectionError as exc:
        panel.log_message.emit("ERROR", str(exc))
        panel._inspection_error.emit(file_id, translate_text("Erreur d'inspection."))
    except Exception as exc:
        panel.log_message.emit("ERROR", translate_text("Erreur inattendue : {exc}", exc=exc))
        panel._inspection_error.emit(file_id, translate_text("Erreur d'inspection."))


def apply_inspection(panel: "RemuxPanel", file_id: str, info: FileInfo) -> None:
    sf = find_source(panel, file_id)
    if sf is None:
        return

    sf.info = info
    sf.tracks = tracks_from_file_info(info, file_id=file_id)

    panel._file_list.update_file(sf)

    source_color = panel._source_colors.get(file_id, _C.BORDER)
    panel._track_table.append_tracks(source_color, sf.tracks)
    panel._refresh_audio_sync_buttons()
    panel._attachment_panel.add_source_attachments(file_id, source_color, info.attachments)
    panel._attachment_panel.add_source_tags(file_id, source_color, info.global_tags)

    att_str = f"  {len(info.attachments)}PJ" if info.attachments else ""
    tag_str = f"  {info.tag_count}Tags" if info.tag_count else ""
    chap_str = f"  {info.chapters.count}Chap" if info.chapters else ""
    panel.log_message.emit(
        "OK",
        translate_text(
            "{name} chargé — {video}V  {audio}A  {subtitle}S{att}{tag}{chap}",
            name=info.path.name,
            video=len(info.video_tracks),
            audio=len(info.audio_tracks),
            subtitle=len(info.subtitle_tracks),
            att=att_str,
            tag=tag_str,
            chap=chap_str,
        ),
    )

    panel._update_chapters_from_sources()

    if panel._source_files[0].id == file_id and not panel._output_edit.text().strip():
        default_out = panel._config.output_dir / f"{info.path.stem}-MVO.mkv"
        panel._output_edit.setText(str(default_out))
        if not panel._file_title_edit.text().strip():
            panel._file_title_edit.setText(info.title)

    panel._sync_tmdb_suggested_title()
    panel.ready_changed.emit(panel._has_ready_files())
    panel._rebuild_preview()
    panel._emit_signals()


def on_inspection_error(panel: "RemuxPanel", file_id: str, message: str) -> None:
    panel._file_list.set_file_error(file_id, message)


def on_remove_file(panel: "RemuxPanel", file_id: str) -> None:
    sf = find_source(panel, file_id)
    if sf is None:
        return

    panel._source_files.remove(sf)
    panel._source_names.pop(file_id, None)
    panel._source_colors.pop(file_id, None)
    panel._source_sync_offsets_ms.pop(file_id, None)
    panel._chapter_sync_cancelled_source_ids.discard(file_id)
    panel._auto_sync_entry_ids.difference_update(
        track.entry_id
        for track in sf.tracks
        if getattr(track, "track_type", "") in {"audio", "subtitle"}
    )
    panel._file_list.remove_file(file_id)
    panel._track_table.remove_tracks_by_file_id(file_id)
    panel._refresh_audio_sync_buttons()
    panel._attachment_panel.remove_by_file_id(file_id)

    if panel._source_files:
        panel._update_chapters_from_sources()
    else:
        panel._reset_empty_state()

    panel._sync_tmdb_suggested_title()
    panel.ready_changed.emit(panel._has_ready_files())
    panel._rebuild_preview()
    panel._emit_signals()

    panel.log_message.emit("INFO", translate_text("{name} retiré de la liste.", name=sf.path.name))


__all__ = [
    "apply_inspection",
    "find_source",
    "has_ready_files",
    "inspect_file",
    "on_add_files",
    "on_inspection_error",
    "on_remove_file",
]
