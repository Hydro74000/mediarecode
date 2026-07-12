from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from core.bluray import append_ffmpeg_input_args
from core.workflows.encode.models import ChapterEntryLike, EncodeConfig

from .plan_models import (
    ContainerMetadataPlan,
    MaterializedContainerMetadataPlan,
    PreparedContainerMetadataInputs,
)


def build_container_metadata_plan(
    config: EncodeConfig,
    *,
    resolve_global_tags: Callable[[EncodeConfig], dict[str, str]],
) -> ContainerMetadataPlan:
    tag_source: Path | None = None
    if config.tag_overrides is None and config.tag_sources:
        tag_source = Path(config.tag_sources[-1])
    return ContainerMetadataPlan(
        tag_source=tag_source,
        tag_overrides_defined=config.tag_overrides is not None,
        chapter_overrides_defined=config.chapter_overrides is not None,
        chapter_overrides_nonempty=bool(config.chapter_overrides),
        has_track_meta_edits=bool(config.track_meta_edits),
        global_tags=resolve_global_tags(config),
    )


def prepare_container_metadata_inputs(
    cmd: list[str],
    config: EncodeConfig,
    *,
    source_idx: dict[Path, int],
    next_input_index: int,
    container_metadata_plan: ContainerMetadataPlan | None = None,
    chapter_materialize_dir: Path | None = None,
    chapter_probe_source: Path | None = None,
    probe_duration_seconds: Callable[[Path], float | None],
    write_ffmetadata_chapters: Callable[[list, Path, float | None], Path],
) -> PreparedContainerMetadataInputs:
    materialized = materialize_container_metadata_inputs(
        config,
        source_idx=source_idx,
        next_input_index=next_input_index,
        container_metadata_plan=container_metadata_plan,
        chapter_materialize_dir=chapter_materialize_dir,
        chapter_probe_source=chapter_probe_source,
        probe_duration_seconds=probe_duration_seconds,
        write_ffmetadata_chapters=write_ffmetadata_chapters,
    )
    cmd.extend(materialized.input_args)
    return PreparedContainerMetadataInputs(
        next_input_index=materialized.next_input_index,
        chapter_input_index=materialized.chapter_input_index,
        tag_input_index=materialized.tag_input_index,
    )


def materialize_container_metadata_inputs(
    config: EncodeConfig,
    *,
    source_idx: dict[Path, int],
    next_input_index: int,
    container_metadata_plan: ContainerMetadataPlan | None = None,
    chapter_materialize_dir: Path | None = None,
    chapter_probe_source: Path | None = None,
    probe_duration_seconds: Callable[[Path], float | None],
    write_ffmetadata_chapters: Callable[[list[ChapterEntryLike], Path, float | None], Path],
) -> MaterializedContainerMetadataPlan:
    metadata_plan = container_metadata_plan or build_container_metadata_plan(
        config,
        resolve_global_tags=lambda current: {},
    )
    input_args: list[str] = []
    chapter_input_index: int | None = None
    if metadata_plan.chapter_overrides_nonempty:
        if chapter_materialize_dir is not None:
            duration_s = probe_duration_seconds(chapter_probe_source or config.source)
            assert config.chapter_overrides is not None
            chapter_file = write_ffmetadata_chapters(
                config.chapter_overrides,
                chapter_materialize_dir,
                duration_s,
            )
            chapter_ref = str(chapter_file)
        else:
            chapter_ref = "<chapitres.ffmetadata>"
        chapter_input_index = next_input_index
        next_input_index += 1
        input_args.extend(["-i", chapter_ref])

    tag_input_index: int | None = None
    if metadata_plan.tag_source is not None:
        mapped_idx = source_idx.get(metadata_plan.tag_source)
        if mapped_idx is not None:
            tag_input_index = mapped_idx
        else:
            tag_input_index = next_input_index
            next_input_index += 1
            append_ffmpeg_input_args(input_args, metadata_plan.tag_source)
    return MaterializedContainerMetadataPlan(
        next_input_index=next_input_index,
        chapter_input_index=chapter_input_index,
        tag_input_index=tag_input_index,
        input_args=tuple(input_args),
    )


def container_chapter_map_value(
    config: EncodeConfig,
    *,
    default_chapter_input_index: int,
    chapter_input_index: int | None,
    container_metadata_plan: ContainerMetadataPlan | None = None,
) -> str:
    metadata_plan = container_metadata_plan
    chapter_overrides_defined = (
        metadata_plan.chapter_overrides_defined
        if metadata_plan is not None
        else config.chapter_overrides is not None
    )
    chapter_overrides_nonempty = (
        metadata_plan.chapter_overrides_nonempty
        if metadata_plan is not None
        else bool(config.chapter_overrides)
    )
    if chapter_overrides_defined:
        if chapter_overrides_nonempty and chapter_input_index is not None:
            return str(chapter_input_index)
        return "-1"
    return str(default_chapter_input_index) if config.keep_chapters else "-1"


def container_metadata_map_value(
    config: EncodeConfig,
    *,
    default_metadata_input_index: int,
    chapter_input_index: int | None,
    tag_input_index: int | None,
    include_copy_video_stream_passthrough: bool,
    is_video_passthrough: Callable[[EncodeConfig], bool],
    chapter_map: str | None = None,
    container_metadata_plan: ContainerMetadataPlan | None = None,
) -> str | None:
    metadata_plan = container_metadata_plan
    tag_overrides_defined = (
        metadata_plan.tag_overrides_defined
        if metadata_plan is not None
        else config.tag_overrides is not None
    )
    chapter_overrides_defined = (
        metadata_plan.chapter_overrides_defined
        if metadata_plan is not None
        else config.chapter_overrides is not None
    )
    has_track_meta_edits = (
        metadata_plan.has_track_meta_edits
        if metadata_plan is not None
        else bool(config.track_meta_edits)
    )
    if tag_overrides_defined:
        if chapter_input_index is not None:
            return str(chapter_input_index)
        if chapter_map is not None and chapter_map not in {"-1", ""}:
            return chapter_map
        return "-1"
    if tag_input_index is not None:
        return str(tag_input_index)
    if include_copy_video_stream_passthrough and is_video_passthrough(config):
        return str(default_metadata_input_index)
    if chapter_overrides_defined or has_track_meta_edits:
        return str(default_metadata_input_index)
    return None


def append_container_metadata_args(
    cmd: list[str],
    config: EncodeConfig,
    *,
    default_metadata_input_index: int,
    default_chapter_input_index: int,
    chapter_input_index: int | None,
    tag_input_index: int | None,
    include_copy_video_stream_passthrough: bool,
    is_video_passthrough: Callable[[EncodeConfig], bool],
    resolve_global_tags: Callable[[EncodeConfig], dict[str, str]],
    build_track_meta_args: Callable[[EncodeConfig], list[str]],
    container_metadata_plan: ContainerMetadataPlan | None = None,
) -> None:
    metadata_plan = container_metadata_plan or build_container_metadata_plan(
        config,
        resolve_global_tags=resolve_global_tags,
    )
    chapter_map = container_chapter_map_value(
        config,
        default_chapter_input_index=default_chapter_input_index,
        chapter_input_index=chapter_input_index,
        container_metadata_plan=metadata_plan,
    )
    metadata_map = container_metadata_map_value(
        config,
        default_metadata_input_index=default_metadata_input_index,
        chapter_input_index=chapter_input_index,
        tag_input_index=tag_input_index,
        include_copy_video_stream_passthrough=include_copy_video_stream_passthrough,
        is_video_passthrough=is_video_passthrough,
        chapter_map=chapter_map,
        container_metadata_plan=metadata_plan,
    )
    if metadata_map is not None:
        cmd.extend(["-map_metadata", metadata_map])
        if include_copy_video_stream_passthrough and is_video_passthrough(config):
            cmd.extend([
                "-map_metadata:s:v:0",
                f"{default_metadata_input_index}:s:v:0",
            ])

    cmd.extend(["-map_chapters", chapter_map])

    global_tags = dict(metadata_plan.global_tags)
    title_value = global_tags.pop("title", None)
    if title_value is not None:
        cmd.extend(["-metadata", f"title={title_value}"])

    cmd.extend(["-metadata", "encoder=", "-metadata", "creation_time="])
    for key, value in global_tags.items():
        cmd.extend(["-metadata", f"{key}={value}"])
    cmd.extend(build_track_meta_args(config))
