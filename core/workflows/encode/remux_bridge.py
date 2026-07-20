"""Bridge helpers between remux and encode workflow configurations."""

from __future__ import annotations

from dataclasses import replace as dataclasses_replace
from pathlib import Path

from core.workflows.common.track_types import TrackMetaPatch, TrackOffset, TrackType
from core.workflows.encode.models import EncodeConfig
from core.workflows.remux_models import RemuxConfig, TrackEntry


def merge_remux_into_encode_config(
    encode_cfg: EncodeConfig,
    remux_cfg: RemuxConfig,
) -> EncodeConfig:
    """Enrichit l'encode config avec les informations issues du remux panel."""
    sub_tracks: list[tuple[Path, int]] = []
    attachment_streams: list[tuple[Path, int]] = []
    tag_sources: list[Path] = []

    source_by_index = {src.file_index: src for src in remux_cfg.sources}
    remux_track_map: dict[tuple[Path, int], TrackEntry] = {}
    remux_track_map_by_id: dict[str, TrackEntry] = {}

    for src in remux_cfg.sources:
        for track in src.tracks:
            remux_track_map[(src.path, track.mkv_tid)] = track
            remux_track_map_by_id[track.entry_id] = track
        for att in src.selected_attachments:
            attachment_streams.append((src.path, att.index))
        if src.copy_tags:
            tag_sources.append(src.path)

    ordered_tracks: list[tuple[Path, TrackEntry]] = []
    for item in remux_cfg.track_order:
        file_index = int(item[0])
        mkv_tid = int(item[1])
        entry_id = str(item[2]).strip() if len(item) > 2 else ""
        ordered_source = source_by_index.get(file_index)
        if ordered_source is None:
            continue
        ordered_track = (
            remux_track_map_by_id.get(entry_id)
            if entry_id
            else remux_track_map.get((ordered_source.path, mkv_tid))
        )
        if ordered_track is None:
            continue
        ordered_tracks.append((ordered_source.path, ordered_track))

    sub_tracks = [
        (src_path, track.mkv_tid)
        for src_path, track in ordered_tracks
        if track.track_type == "subtitle"
    ]

    tag_overrides = remux_cfg.tag_overrides
    track_meta_edits: list[TrackMetaPatch] = []
    track_time_offsets: list[TrackOffset] = []
    video_tracks = encode_cfg.video_tracks or ([encode_cfg.video] if encode_cfg.video is not None else [])

    def _make_edit(track_order: int, track: TrackEntry) -> TrackMetaPatch | None:
        lang = (track.language or "").strip()
        orig_lang = (track.orig_language or "").strip()
        title = (track.title or "").strip()
        if not lang and orig_lang and lang != orig_lang:
            lang = "und"
        has_flag_state = any((
            track.flag_default,
            track.flag_forced,
            track.flag_hearing_impaired,
            track.flag_visual_impaired,
            track.flag_original,
            track.flag_commentary,
        ))
        has_flag_change = any((
            track.flag_default != track.orig_flag_default,
            track.flag_forced != track.orig_flag_forced,
            track.flag_hearing_impaired != track.orig_flag_hearing_impaired,
            track.flag_visual_impaired != track.orig_flag_visual_impaired,
            track.flag_original != track.orig_flag_original,
            track.flag_commentary != track.orig_flag_commentary,
        ))
        if not lang and not title and not has_flag_state and not has_flag_change:
            return None
        return TrackMetaPatch(
            track_order=track_order,
            language=lang,
            title=title if title else None,
            flag_default=track.flag_default,
            flag_forced=track.flag_forced,
            flag_hearing_impaired=track.flag_hearing_impaired,
            flag_visual_impaired=track.flag_visual_impaired,
            flag_original=track.flag_original,
            flag_commentary=track.flag_commentary,
        )

    def _append_track_offset(track_type: str, src_path: Path, stream_index: int, track: TrackEntry | None) -> None:
        if track is None:
            return
        offset_ms = int(getattr(track, "time_shift_ms", 0) or 0)
        if offset_ms == 0:
            return
        track_time_offsets.append(TrackOffset(
            track_type=track_type,
            source_path=src_path,
            stream_index=int(stream_index),
            offset_ms=offset_ms,
            sync_rewrite_mode=str(getattr(track, "sync_rewrite_mode", "") or ""),
        ))

    def _find_track(src_path: Path, stream_index: int, track_type: str) -> TrackEntry | None:
        track = remux_track_map.get((src_path, stream_index))
        if track is not None:
            return track
        for entry in remux_track_map.values():
            if entry.mkv_tid == stream_index and entry.track_type == track_type:
                return entry
        return None

    for video_order, video_settings in enumerate(video_tracks, start=1):
        video_src = Path(video_settings.source_path or encode_cfg.source)
        if video_settings.track_entry_id:
            video_entry = remux_track_map_by_id.get(video_settings.track_entry_id)
        else:
            video_entry = _find_track(video_src, int(video_settings.stream_index), "video")
        if video_entry is None:
            continue
        edit = _make_edit(video_order, video_entry)
        if edit:
            track_meta_edits.append(edit)
        _append_track_offset(TrackType.VIDEO.value, video_src, int(video_settings.stream_index), video_entry)

    audio_offset = len(video_tracks) + 1
    for audio_order, audio_settings in enumerate(encode_cfg.audio_tracks):
        src_path = audio_settings.source_path or encode_cfg.source
        if audio_settings.track_entry_id:
            audio_entry = remux_track_map_by_id.get(audio_settings.track_entry_id)
        else:
            audio_entry = _find_track(src_path, audio_settings.stream_index, "audio")
        if audio_entry is None:
            continue
        edit = _make_edit(audio_offset + audio_order, audio_entry)
        if edit:
            track_meta_edits.append(edit)
        _append_track_offset(TrackType.AUDIO.value, src_path, audio_settings.stream_index, audio_entry)

    sub_offset = audio_offset + len(encode_cfg.audio_tracks)
    used_sub_tracks = sub_tracks or encode_cfg.subtitle_tracks
    for sub_order, (sub_path, sub_sid) in enumerate(used_sub_tracks):
        subtitle_entry = remux_track_map.get((sub_path, sub_sid))
        if subtitle_entry is None:
            continue
        edit = _make_edit(sub_offset + sub_order, subtitle_entry)
        if edit:
            track_meta_edits.append(edit)
        _append_track_offset(TrackType.SUBTITLE.value, sub_path, sub_sid, subtitle_entry)

    chapter_overrides = remux_cfg.chapter_overrides
    if (
        not sub_tracks
        and not attachment_streams
        and not tag_sources
        and tag_overrides is None
        and not track_meta_edits
        and not track_time_offsets
        and encode_cfg.keep_chapters == remux_cfg.keep_chapters
        and remux_cfg.chapter_overrides is None
    ):
        # Le choix de backend de muxage du job conteneur reste propagé, sans
        # reconstruire l'objet quand rien ne change.
        if encode_cfg.mux_backend == remux_cfg.mux_backend:
            return encode_cfg
        return dataclasses_replace(encode_cfg, mux_backend=remux_cfg.mux_backend)

    return EncodeConfig(
        source=encode_cfg.source,
        output=encode_cfg.output,
        video=encode_cfg.video,
        video_tracks=video_tracks,
        audio_tracks=encode_cfg.audio_tracks,
        copy_subtitles=encode_cfg.copy_subtitles if not sub_tracks else False,
        subtitle_tracks=sub_tracks or encode_cfg.subtitle_tracks,
        keep_chapters=remux_cfg.keep_chapters,
        chapter_overrides=chapter_overrides,
        attachment_streams=attachment_streams,
        tag_sources=[] if tag_overrides is not None else tag_sources,
        tag_overrides=tag_overrides,
        track_meta_edits=track_meta_edits,
        track_time_offsets=track_time_offsets,
        duration_s=encode_cfg.duration_s,
        copy_dv=encode_cfg.copy_dv,
        copy_hdr10plus=encode_cfg.copy_hdr10plus,
        dovi_profile=encode_cfg.dovi_profile,
        work_dir=encode_cfg.work_dir,
        file_title=encode_cfg.file_title,
        extra_attachments=encode_cfg.extra_attachments,
        tmdb_cover=encode_cfg.tmdb_cover,
        mux_backend=remux_cfg.mux_backend,
    )
