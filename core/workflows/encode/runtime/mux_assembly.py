"""Mux assembly helpers for encode workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.bluray import append_ffmpeg_input_args
from core.workflows.common.attachments import canonical_attachment_output_name, mime_for_path
from core.workflows.common.metadata import (
    disposition_value as _common_disposition_value,
    normalize_track_language as _common_normalize_track_language,
)
from core.workflows.encode.domain import audio_codec_args as _audio_codec_args_domain
from core.workflows.encode.models import EncodeConfig, VideoEncodeSettings
from core.workflows.encode.planning.plan_models import EncodePlan
from core.workflows.encode.planning.track_metadata import resolve_track_metadata
from core.workflows.encode.planning.sources import source_input_index_map as _source_input_index_map_plan


@dataclass(frozen=True)
class EncodeStreamMappingCallbacks:
    subtitle_codec_args: Callable[[list[tuple[object, int]]], list[str]]
    describe_attachment_stream: Callable[[Path, int], dict[str, object]]
    default_attachment_filename: Callable[[dict[str, object], int], str]


class EncodeStreamMappingService:
    def __init__(self, callbacks: EncodeStreamMappingCallbacks) -> None:
        self._cb = callbacks

    def append(
        self,
        cmd: list[str],
        config: EncodeConfig,
        *,
        source_idx: dict[Path, int],
        subtitle_copy_input_indices: list[int],
        sync_remap: dict[tuple[Path, int, str], tuple[int, int]] | None = None,
        offset_remap: dict[tuple[Path, int, str], tuple[int, int]] | None = None,
        subtitle_tracks_override: list[tuple[Path, int]] | None = None,
        force_copy_subtitles_wildcard: bool = True,
    ) -> None:
        sync_remap = sync_remap or {}
        offset_remap = offset_remap or {}
        for i, audio in enumerate(config.audio_tracks):
            src_path = audio.source_path or config.source
            key = (Path(src_path), int(audio.stream_index), "audio")
            remapped = offset_remap.get(key)
            if remapped is None:
                remapped = sync_remap.get((src_path, int(audio.stream_index), "audio"))
            if remapped is not None:
                mapped_audio_inp_idx, stream_idx = remapped
            else:
                source_audio_inp_idx = source_idx.get(Path(src_path))
                if source_audio_inp_idx is None:
                    source_audio_inp_idx = source_idx.get(config.source)
                mapped_audio_inp_idx = source_audio_inp_idx if source_audio_inp_idx is not None else 0
                stream_idx = int(audio.stream_index)
            cmd.extend(["-map", f"{mapped_audio_inp_idx}:{stream_idx}"])
            cmd.extend(_audio_codec_args_domain(i, audio))

        subtitle_tracks = (
            subtitle_tracks_override
            if subtitle_tracks_override is not None
            else config.subtitle_tracks
        )
        if subtitle_tracks:
            for src_path, stream_idx in subtitle_tracks:
                key = (Path(src_path), int(stream_idx), "subtitle")
                remapped = offset_remap.get(key)
                if remapped is None:
                    remapped = sync_remap.get((src_path, int(stream_idx), "subtitle"))
                if remapped is not None:
                    mapped_subtitle_inp_idx, mapped_stream_idx = remapped
                    cmd.extend(["-map", f"{mapped_subtitle_inp_idx}:{mapped_stream_idx}"])
                    continue
                source_subtitle_inp_idx = source_idx.get(Path(src_path))
                if source_subtitle_inp_idx is None:
                    continue
                cmd.extend(["-map", f"{source_subtitle_inp_idx}:{stream_idx}"])
            cmd.extend(self._cb.subtitle_codec_args([
                (track[0], int(track[1])) for track in subtitle_tracks
            ]))
        elif config.copy_subtitles and force_copy_subtitles_wildcard:
            for inp_i in subtitle_copy_input_indices:
                cmd.extend(["-map", f"{inp_i}:s?"])
            cmd.extend(["-c:s", "copy"])

        mapped_attachment_meta: list[tuple[int, dict[str, object]]] = []
        if config.attachment_streams:
            for src_path, stream_idx in config.attachment_streams:
                attachment_inp_idx = source_idx.get(Path(src_path))
                if attachment_inp_idx is None:
                    continue
                cmd.extend(["-map", f"{attachment_inp_idx}:{stream_idx}"])
                mapped_attachment_meta.append(
                    (stream_idx, self._cb.describe_attachment_stream(src_path, stream_idx))
                )
            if mapped_attachment_meta:
                cmd.extend(["-c:t", "copy"])
                for out_idx, (stream_idx, meta) in enumerate(mapped_attachment_meta):
                    cmd.extend([
                        f"-metadata:s:t:{out_idx}",
                        f"mimetype={str(meta.get('mimetype') or 'application/octet-stream').strip() or 'application/octet-stream'}",
                    ])
                    cmd.extend([
                        f"-metadata:s:t:{out_idx}",
                        f"filename={self._cb.default_attachment_filename(meta, stream_idx)}",
                    ])

        existing_att = len(mapped_attachment_meta)
        for i, att_path in enumerate(config.extra_attachments):
            att_idx = existing_att + i
            # Nom partagé avec le contrat de validation (cover-art Matroska :
            # extension conservée, stem canonique en minuscules).
            att_name = canonical_attachment_output_name(att_path)
            cmd.extend(["-attach", str(att_path)])
            cmd.extend([f"-metadata:s:t:{att_idx}", f"mimetype={mime_for_path(att_path)}"])
            cmd.extend([f"-metadata:s:t:{att_idx}", f"filename={att_name}"])


def build_injected_hevc_wrap_command(
    *,
    ffmpeg_bin: str,
    source: Path,
    hevc_input: Path,
    mkv_output: Path,
    source_video_fps_expr: Callable[[Path], str],
    ffmpeg_progress_args: Callable[[], list[str]],
    ffmpeg_thread_args: Callable[[], list[str]],
) -> list[str]:
    fps_expr = source_video_fps_expr(source)
    return [
        ffmpeg_bin,
        "-hide_banner",
        "-y",
        *ffmpeg_progress_args(),
        "-f",
        "hevc",
        "-framerate",
        fps_expr,
        "-i",
        str(hevc_input),
        *ffmpeg_thread_args(),
        "-map",
        "0:v:0",
        "-c:v",
        "copy",
        "-bsf:v",
        f"setts=pts=N/({fps_expr}*TB)",
        str(mkv_output),
    ]


@dataclass(frozen=True)
class EncodeFinalMuxBuilderCallbacks:
    ffmpeg_bin: str
    ffmpeg_progress_args: Callable[[], list[str]]
    ffmpeg_thread_args: Callable[[], list[str]]
    video_tracks: Callable[[EncodeConfig], list[VideoEncodeSettings]]
    video_source_from_settings: Callable[[EncodeConfig, VideoEncodeSettings], Path]
    video_stream_from_settings: Callable[[VideoEncodeSettings], int]
    build_encode_plan: Callable[[EncodeConfig], EncodePlan]
    prepare_container_metadata_inputs: Callable[..., tuple[int, int | None, int | None]]
    append_stream_maps_and_attachments: Callable[..., None]
    append_container_metadata_args: Callable[..., None]


class EncodeFinalMuxBuilder:
    def __init__(self, callbacks: EncodeFinalMuxBuilderCallbacks) -> None:
        self._cb = callbacks

    def build(
        self,
        config: EncodeConfig,
        prepared_video_inputs: list[dict[str, object]],
        *,
        chapter_materialize_dir: Path | None = None,
        plan: EncodePlan | None = None,
    ) -> list[str]:
        cb = self._cb
        video_inputs = prepared_video_inputs or [
            {
                "input_args": [],
                "path": (
                    cb.video_source_from_settings(config, video)
                    if video.codec == "copy"
                    else Path(f"<video_{idx}.mkv>")
                ),
                "map_arg": (
                    f"{idx - 1}:{cb.video_stream_from_settings(video)}"
                    if video.codec == "copy"
                    else f"{idx - 1}:v:0"
                ),
            }
            for idx, video in enumerate(cb.video_tracks(config), start=1)
        ]

        cmd: list[str] = [cb.ffmpeg_bin, "-hide_banner", "-y"]
        cmd.extend(cb.ffmpeg_progress_args())
        for spec in video_inputs:
            raw_input_args = spec.get("input_args", ())
            if isinstance(raw_input_args, (list, tuple)):
                input_args = [str(arg) for arg in raw_input_args]
            else:
                input_args = []
            cmd.extend(input_args)
            input_path = spec["path"]
            if not isinstance(input_path, (Path, str)):
                raise TypeError("Chemin d'entrée vidéo invalide")
            append_ffmpeg_input_args(cmd, input_path)

        plan = plan or cb.build_encode_plan(config)
        all_sources = list(plan.all_sources)
        source_idx = _source_input_index_map_plan(all_sources, start_index=len(video_inputs))
        for src in all_sources:
            append_ffmpeg_input_args(cmd, src)

        next_input_index, chapter_input_index, tag_input_index = cb.prepare_container_metadata_inputs(
            cmd,
            config,
            source_idx=source_idx,
            next_input_index=len(video_inputs) + len(all_sources),
            plan=plan,
            chapter_materialize_dir=chapter_materialize_dir,
            chapter_probe_source=config.source,
        )
        _ = next_input_index
        cmd.extend(cb.ffmpeg_thread_args())

        for out_idx, spec in enumerate(video_inputs):
            cmd.extend(["-map", str(spec["map_arg"])])
            cmd.extend([f"-c:v:{out_idx}", "copy"])

        cb.append_stream_maps_and_attachments(
            cmd,
            config,
            source_idx=source_idx,
            subtitle_copy_input_indices=list(source_idx.values()),
            subtitle_tracks_override=list(plan.resolved_subtitle_tracks),
            force_copy_subtitles_wildcard=(config.copy_subtitles and not plan.subtitles_resolved),
        )

        default_source_index = source_idx.get(config.source, len(video_inputs))
        cb.append_container_metadata_args(
            cmd,
            config,
            default_metadata_input_index=default_source_index,
            default_chapter_input_index=default_source_index,
            chapter_input_index=chapter_input_index,
            tag_input_index=tag_input_index,
            include_copy_video_stream_passthrough=False,
            plan=plan,
        )
        cmd.append(str(config.output))
        return cmd


@dataclass(frozen=True)
class TrackMetadataArgsBuilderCallbacks:
    video_tracks: Callable[[EncodeConfig], list[VideoEncodeSettings]]
    log_warn: Callable[[str], None]


class TrackMetadataArgsBuilder:
    def __init__(self, callbacks: TrackMetadataArgsBuilderCallbacks) -> None:
        self._cb = callbacks

    def build(self, config: EncodeConfig, *, plan: EncodePlan | None = None) -> list[str]:
        args: list[str] = []
        if plan is not None:
            metadata_items = plan.track_metadata
        else:
            videos = self._cb.video_tracks(config)
            metadata_items = resolve_track_metadata(
                config,
                video_refs=(
                    (
                        Path(getattr(video, "source_path", None) or config.source),
                        int(getattr(video, "stream_index", 0) or 0),
                    )
                    for video in videos
                ),
                subtitle_refs=((Path(path), int(index)) for path, index in config.subtitle_tracks),
            )

        valid_positions = set(range(1, len(metadata_items) + 1))
        for edit in config.track_meta_edits:
            if int(edit.track_order) not in valid_positions:
                self._cb.log_warn(f"Piste invalide en édition metadata: @{edit.track_order}")
        type_indexes = {"video": 0, "audio": 0, "subtitle": 0}
        for metadata in metadata_items:
            stream_type = {"video": "v", "audio": "a", "subtitle": "s"}[metadata.track_type]
            out_idx = type_indexes[metadata.track_type]
            type_indexes[metadata.track_type] += 1
            stream_spec = f"-metadata:s:{stream_type}:{out_idx}"
            disposition_spec = f"-disposition:{stream_type}:{out_idx}"

            if metadata.language is not None:
                lang_value = normalized_track_language_value(metadata.language, metadata.name)
                if lang_value:
                    args.extend([stream_spec, f"language={lang_value}"])
                    args.extend([stream_spec, "language-ietf="])

            if metadata.name is not None:
                args.extend([stream_spec, f"title={metadata.name}"])

            flags = metadata.flags
            disposition = (
                _common_disposition_value(
                    flag_default=flags.default,
                    flag_forced=flags.forced,
                    flag_hearing_impaired=flags.hearing_impaired,
                    flag_visual_impaired=flags.visual_impaired,
                    flag_original=flags.original,
                    flag_commentary=flags.commentary,
                )
                if flags is not None
                else None
            )
            if disposition is not None:
                args.extend([disposition_spec, disposition])
        return args


def track_spec_for_track_order(track_order: int, video_count: int, audio_count: int) -> tuple[str, int] | None:
    if track_order <= 0:
        return None
    if track_order <= max(1, video_count):
        return ("v", track_order - 1)

    first_audio = max(1, video_count) + 1
    last_audio = first_audio + max(0, audio_count) - 1
    if first_audio <= track_order <= last_audio:
        return ("a", track_order - first_audio)

    first_sub = last_audio + 1
    if track_order >= first_sub:
        return ("s", track_order - first_sub)
    return None


def normalized_track_language_value(language: str, title: str | None = None) -> str | None:
    return _common_normalize_track_language(language, title)


def disposition_value_from_edit(edit) -> str | None:
    return _common_disposition_value(
        flag_default=edit.flag_default,
        flag_forced=edit.flag_forced,
        flag_hearing_impaired=edit.flag_hearing_impaired,
        flag_visual_impaired=edit.flag_visual_impaired,
        flag_original=edit.flag_original,
        flag_commentary=edit.flag_commentary,
    )
