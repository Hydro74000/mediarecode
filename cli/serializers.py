"""JSON serializers for CLI command outputs."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from core.inspector import FileInfo, fmt_timecode_display
from core.workflows.remux_models import RemuxConfig, TrackEntry


def serialize_file_info(info: FileInfo) -> dict[str, Any]:
    def track_payload(track: Any, track_type: str) -> dict[str, Any]:
        payload = asdict(track)
        payload.pop("raw", None)
        payload["type"] = track_type
        if hasattr(track, "hdr_type"):
            payload["hdr_type"] = track.hdr_type.label()
        return payload

    return {
        "path": str(info.path),
        "format": info.format,
        "duration_s": info.duration_s,
        "duration": info.duration_human,
        "size_bytes": info.size_bytes,
        "size": info.size_human,
        "bit_rate": info.bit_rate,
        "title": info.title,
        "hdr_type": info.hdr_type.label(),
        "frame_count": info.frame_count,
        "tag_count": info.tag_count,
        "global_tags": info.global_tags,
        "tracks": [
            *(track_payload(track, "video") for track in info.video_tracks),
            *(track_payload(track, "audio") for track in info.audio_tracks),
            *(track_payload(track, "subtitle") for track in info.subtitle_tracks),
        ],
        "attachments": [asdict(att) for att in info.attachments],
        "chapters": [
            {"timecode_s": ch.timecode_s, "timecode": fmt_timecode_display(ch.timecode_s), "name": ch.name}
            for ch in (info.chapters.entries if info.chapters else [])
        ],
    }


def serialize_track_preview(track: TrackEntry) -> dict[str, Any]:
    return {
        "source": int(track.file_id.removeprefix("src")) if track.file_id.startswith("src") else 0,
        "id": track.mkv_tid,
        "type": track.track_type,
        "enabled": track.enabled,
        "language": track.language,
        "title": track.title,
        "codec": track.codec,
        "display_info": track.display_info,
        "sync_rewrite_mode": track.sync_rewrite_mode,
        "flags": {
            "enabled": track.flag_enabled,
            "default": track.flag_default,
            "forced": track.flag_forced,
            "hearing_impaired": track.flag_hearing_impaired,
            "visual_impaired": track.flag_visual_impaired,
            "original": track.flag_original,
            "commentary": track.flag_commentary,
        },
    }


def serialize_remux_config(config: RemuxConfig) -> dict[str, Any]:
    return {
        "output": str(config.output),
        "sources": [
            {
                "index": source.file_index,
                "path": str(source.path),
                "selected_attachments": [attachment.filename for attachment in source.selected_attachments],
                "copy_tags": source.copy_tags,
                "has_chapters": source.has_chapters,
            }
            for source in config.sources
        ],
        "track_order": [
            {
                "source": item[0],
                "id": item[1],
                **({"entry_id": item[2]} if len(item) > 2 else {}),
            }
            for item in config.track_order
        ],
        "keep_chapters": config.keep_chapters,
        "chapter_source_index": config.chapter_source_index,
        "extra_attachments": [str(path) for path in config.extra_attachments],
        "mux_backend": config.mux_backend,
        "file_title": config.file_title,
    }
