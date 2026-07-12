"""Command construction helpers for the remux workflow."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Callable

from core.bluray import append_ffmpeg_input_args
from core.subtitle_codec import plan_subtitle_codec
from core.workflows.common.sync_rewrite import (
    audio_bitrate_kbps_from_display_info,
    normalized_rewrite_codec,
)
from core.workflows.common.attachments import mime_for_path
from core.workflows.common.track_types import TimelineMappedTrack
from core.workflows.common.metadata import STREAM_SPEC_BY_TRACK_TYPE as STREAM_SPEC_BY_TYPE
from core.workflows.remux_attachments import attachment_names, build_attachment_mapping
from core.workflows.remux_mapping import (
    MappedTrack,
    append_offset_inputs,
    disposition_value_for_track,
    metadata_context,
    normalized_language_value,
    offset_input_specs_from_mapped_tracks,
    resolve_mapped_tracks,
)
from core.workflows.remux_models import RemuxConfig, RemuxError


# Codec audio cible (label éditeur) → encodeur ffmpeg. Limité aux codecs lossy/FLAC
# que le panneau d'encodage expose ; les autres restent en copie.
_AUDIO_REENCODE_ENCODERS = {"aac": "aac", "ac3": "ac3", "eac3": "eac3", "flac": "flac"}


def _audio_channel_count(display_info: str) -> int:
    match = re.search(r"\b(\d)\.(\d)\b", str(display_info or ""))
    if match:
        return int(match.group(1)) + int(match.group(2))
    text = str(display_info or "").lower()
    if "stereo" in text:
        return 2
    if "mono" in text:
        return 1
    return 0


def _audio_variant_codec_args(mapped_track: MappedTrack) -> list[str]:
    """Args ffmpeg de réencodage pour une variante audio dont le codec cible
    diffère de la source (sinon liste vide → copie via le `-c copy` global)."""
    track = mapped_track.track
    if track.track_type != "audio":
        return []
    target = normalized_rewrite_codec(track.codec)
    source = normalized_rewrite_codec(track.orig_codec or track.codec)
    if not target or target == source or target not in _AUDIO_REENCODE_ENCODERS:
        return []
    out_idx = mapped_track.out_type_index
    args = [f"-c:a:{out_idx}", _AUDIO_REENCODE_ENCODERS[target]]
    if target != "flac":
        bitrate = audio_bitrate_kbps_from_display_info(track.display_info)
        if bitrate:
            args.extend([f"-b:a:{out_idx}", f"{int(bitrate)}k"])
    if target == "ac3" and _audio_channel_count(track.display_info) > 6:
        args.extend([f"-ac:a:{out_idx}", "6", f"-channel_layout:a:{out_idx}", "5.1"])
    return args


def build_remux_command(
    config: RemuxConfig,
    *,
    ffmpeg_bin: str,
    ffmpeg_progress_args: list[str],
    ffmpeg_thread_args: list[str],
    cli_path: Callable[[Path | str], str],
    sync_inputs: list[Path | str] | None = None,
    sync_input_formats: list[str] | None = None,
    extra_inputs: list[Path | str] | None = None,
    chapter_input_index: int | None = None,
    strict_interleave_override: bool | None = None,
    mapped_tracks_override: list[MappedTrack] | None = None,
    resolve_mapped_tracks_fn: Callable[[RemuxConfig], list[MappedTrack]] = resolve_mapped_tracks,
    needs_strict_interleave_fn: Callable[[Sequence[TimelineMappedTrack]], bool] | None = None,
) -> list[str]:
    mapped_tracks = mapped_tracks_override if mapped_tracks_override is not None else resolve_mapped_tracks_fn(config)
    needs_strict_interleave_impl = needs_strict_interleave_fn or (lambda tracks: False)
    needs_strict_interleave = (
        needs_strict_interleave_impl(mapped_tracks)
        if strict_interleave_override is None
        else strict_interleave_override
    )

    cmd: list[str] = [ffmpeg_bin, "-hide_banner", "-y"]
    cmd.extend(ffmpeg_progress_args)
    cmd.extend(ffmpeg_thread_args)

    for source in config.sources:
        append_ffmpeg_input_args(cmd, source.path, cli_path=cli_path)
    for index, path in enumerate(sync_inputs or []):
        fmt = (sync_input_formats or [])[index] if sync_input_formats and index < len(sync_input_formats) else "matroska"
        cmd.extend(["-f", fmt, "-i", cli_path(path)])
    for path in (extra_inputs or []):
        cmd.extend(["-i", cli_path(path)])

    sync_count = len(sync_inputs or [])
    extra_count = len(extra_inputs or [])
    offset_specs = offset_input_specs_from_mapped_tracks(mapped_tracks)
    _, offset_remap = append_offset_inputs(
        cmd,
        offset_specs,
        start_input_index=len(config.sources) + sync_count + extra_count,
        cli_path=cli_path,
    )

    for mapped_track in mapped_tracks:
        map_key = (
            int(mapped_track.source_file_index),
            int(mapped_track.stream_index),
            str(mapped_track.track.track_type),
            int(mapped_track.out_type_index),
        )
        remapped = offset_remap.get(map_key)
        if remapped is None:
            cmd.extend(["-map", f"{mapped_track.source_input_idx}:{mapped_track.stream_index}"])
        else:
            cmd.extend(["-map", f"{remapped[0]}:{remapped[1]}"])

    cmd.extend(["-c", "copy", "-default_mode", "passthrough"])

    for mapped_track in mapped_tracks:
        if mapped_track.track.track_type != "subtitle":
            continue
        try:
            codec_arg, _ = plan_subtitle_codec(mapped_track.track.codec)
        except ValueError as exc:
            raise RemuxError(str(exc)) from exc
        if codec_arg != "copy":
            cmd.extend([f"-c:s:{mapped_track.out_type_index}", codec_arg])

    for mapped_track in mapped_tracks:
        cmd.extend(_audio_variant_codec_args(mapped_track))

    if needs_strict_interleave:
        cmd.extend(["-max_interleave_delta", "0"])
        cmd.extend(["-max_muxing_queue_size", "9999"])

    meta = metadata_context(config, chapter_input_index)
    cmd.extend(["-map_metadata", meta.metadata_map])
    cmd.extend(["-map_chapters", meta.chapter_map])
    cmd.extend(["-metadata", "encoder=", "-metadata", "creation_time="])

    for key, value in meta.global_tags.items():
        cmd.extend(["-metadata", f"{key}={value}"])

    for mapped_track in mapped_tracks:
        stream_spec = STREAM_SPEC_BY_TYPE[mapped_track.track.track_type]
        out_idx = mapped_track.out_type_index
        lang_value = normalized_language_value(mapped_track.track)
        cmd.extend([f"-metadata:s:{stream_spec}:{out_idx}", f"language={lang_value}"])
        cmd.extend([f"-metadata:s:{stream_spec}:{out_idx}", "language-ietf="])
        cmd.extend([f"-metadata:s:{stream_spec}:{out_idx}", f"title={mapped_track.track.title or ''}"])
        cmd.extend([f"-disposition:{stream_spec}:{out_idx}", disposition_value_for_track(mapped_track.track)])

    cmd.extend(build_attachment_mapping(config))
    att_t_idx = 0
    for source in config.sources:
        for attachment in sorted(source.selected_attachments, key=lambda item: item.local_index):
            if attachment.is_attached_pic:
                continue
            meta_name = attachment_names(attachment)[0]
            mimetype = (attachment.mimetype or "").strip() or mime_for_path(Path(meta_name))
            cmd.extend([f"-metadata:s:t:{att_t_idx}", f"mimetype={mimetype}"])
            cmd.extend([f"-metadata:s:t:{att_t_idx}", f"filename={meta_name}"])
            att_t_idx += 1

    for attachment_path in config.extra_attachments:
        cmd.extend(["-attach", cli_path(attachment_path)])
        cmd.extend([f"-metadata:s:t:{att_t_idx}", f"mimetype={mime_for_path(attachment_path)}"])
        cmd.extend([f"-metadata:s:t:{att_t_idx}", f"filename={attachment_path.name}"])
        att_t_idx += 1

    cmd.append(cli_path(config.output))
    return cmd


def preview_remux_command(
    config: RemuxConfig,
    *,
    build_command: Callable[..., list[str]],
) -> str:
    extra_inputs: list[Path | str] = []
    chapter_input_index: int | None = None
    if config.chapter_overrides:
        extra_inputs.append(Path("<chapitres.ffmetadata>"))
        chapter_input_index = len(config.sources)

    parts = build_command(
        config,
        extra_inputs=extra_inputs,
        chapter_input_index=chapter_input_index,
    )
    if not parts:
        return ""

    lines: list[str] = [parts[0]]
    index = 1
    while index < len(parts):
        token = parts[index]
        if token.startswith("-") and index + 1 < len(parts) and not parts[index + 1].startswith("-"):
            lines.append(f"    {token} {parts[index + 1]}")
            index += 2
        else:
            lines.append(f"    {token}")
            index += 1

    return " \\\n".join(lines)


__all__ = ["build_remux_command", "preview_remux_command"]
