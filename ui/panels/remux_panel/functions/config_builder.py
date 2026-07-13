"""Construction de RemuxConfig depuis l'état UI."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from core.workflows.remux_models import RemuxConfig, SourceInput

if TYPE_CHECKING:
    from ui.panels.remux_panel.panel import RemuxPanel


def current_config(panel: "RemuxPanel") -> RemuxConfig | None:
    if not panel._has_ready_files():
        return None

    output_str = panel._output_edit.text().strip()
    if not output_str:
        return None

    all_tracks = panel._track_table.current_tracks()
    if not all_tracks:
        return None

    id_to_index = {sf.id: i for i, sf in enumerate(panel._source_files)}

    extras = panel._attachment_panel.get_extras_per_file()
    merged_tag_overrides = panel._attachment_panel.get_global_tag_overrides()

    sources: list[SourceInput] = []
    for i, sf in enumerate(panel._source_files):
        if sf.info is None:
            continue
        src_tracks = [t for t in all_tracks if t.file_id == sf.id]
        if not src_tracks:
            src_tracks = sf.tracks
        file_extras = extras.get(sf.id, {})
        has_tags = file_extras.get("has_tags", False)
        has_chapters = bool(
            sf.info and sf.info.chapters and sf.info.chapters.entries
        )
        sources.append(SourceInput(
            path=sf.path,
            file_index=i,
            tracks=src_tracks,
            selected_attachments=file_extras.get("selected_attachments", []),
            attachment_count=len(sf.info.attachments) if sf.info else 0,
            copy_tags=has_tags,
            has_chapters=has_chapters,
        ))

    if not sources:
        return None

    track_order: list[tuple[int, int] | tuple[int, int, str]] = [
        (id_to_index[t.file_id], t.mkv_tid, t.entry_id)
        for t in all_tracks
        if t.enabled and t.file_id in id_to_index
    ]

    keep_ch = panel._chapter_panel.keep_chapters()
    ch_overrides = panel.current_chapter_overrides()
    ch_source_idx = (
        panel._chapter_panel.selected_source_index() if keep_ch else None
    )

    return RemuxConfig(
        sources=sources,
        output=Path(output_str),
        track_order=track_order,
        keep_chapters=keep_ch,
        chapter_overrides=ch_overrides,
        chapter_source_index=ch_source_idx,
        extra_attachments=panel._attachment_panel.get_extra_attachments(),
        work_dir=panel._config.work_dir,
        file_title=panel._file_title_edit.text().strip(),
        tag_overrides=merged_tag_overrides,
        tmdb_cover=panel._attachment_panel.get_pending_tmdb_cover(),
        mux_backend=panel._config.remux_mux_backend,
    )


__all__ = ["current_config"]
