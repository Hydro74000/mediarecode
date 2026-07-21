from __future__ import annotations

from pathlib import Path

import pytest

from core.workflows.encode.models import (
    AudioTrackSettings,
    EncodeConfig,
    EncodeError,
    VideoEncodeSettings,
)
from core.workflows.encode.planning.encode_plan import build_encode_plan
from core.workflows.encode.planning.command_plan import build_encode_command_selection
from core.workflows.encode.planning.metadata_plan import (
    append_container_metadata_args,
    build_container_metadata_plan,
    container_chapter_map_value,
    container_metadata_map_value,
    materialize_container_metadata_inputs,
    prepare_container_metadata_inputs,
)
from core.workflows.encode.planning.offsets import (
    build_offset_specs,
    track_time_offset_lookup,
    video_map_arg,
)
from core.workflows.encode.planning.track_assembly import (
    build_track_input_paths,
    resolve_track_assembly,
)
from core.workflows.encode.planning.sources import resolve_source_layout
from core.workflows.encode.planning.subtitles import resolve_subtitle_tracks_for_encode
from core.workflows.encode.planning.sync_plan import (
    build_sync_analysis_plan,
    build_probe_remux_config,
    build_sync_mapped_tracks,
    needs_strict_interleave_for_encode,
    requires_file_sync_fallback_for_offsets,
)
from core.workflows.encode.planning.preview import format_preview_selection
from core.workflows.encode.runtime_helpers import EncodeSyncMappedTrack, EncodeSyncTrack
from core.workflows.common.track_types import TrackTimeOffset
from core.inspector import ChapterEntry


def _make_config(source: Path, output: Path, **kwargs) -> EncodeConfig:
    cfg = EncodeConfig(
        source=source,
        output=output,
        video=kwargs.pop("video", VideoEncodeSettings(codec="copy")),
    )
    for key, value in kwargs.items():
        setattr(cfg, key, value)
    return cfg


def test_resolve_source_layout_dedupes_all_encode_inputs(tmp_path):
    src = tmp_path / "src.mkv"
    alt = tmp_path / "alt.mkv"
    src.touch()
    alt.touch()

    cfg = _make_config(
        src,
        tmp_path / "out.mkv",
        video=VideoEncodeSettings(codec="copy", source_path=alt),
        audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy", source_path=alt)],
        subtitle_tracks=[(src, 3), (alt, 4)],
        attachment_streams=[(alt, 5)],
    )

    layout = resolve_source_layout(cfg)

    assert layout.sources == (src, alt)
    assert layout.source_idx == {src: 0, alt: 1}


def test_build_encode_plan_aggregates_static_encode_resolution(tmp_path):
    src = tmp_path / "src.mkv"
    alt = tmp_path / "alt.mkv"
    src.touch()
    alt.touch()
    cfg = _make_config(
        src,
        tmp_path / "out.mkv",
        video=VideoEncodeSettings(codec="copy", source_path=alt, stream_index=2),
        audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy", source_path=src)],
        subtitle_tracks=[(alt, 4)],
        track_time_offsets=[
            TrackTimeOffset(track_type="audio", source_path=src, stream_index=1, offset_ms=120),
        ],
    )

    plan = build_encode_plan(
        cfg,
        probe_subtitle_indices=lambda *_args: pytest.fail("explicit subtitles should skip probe"),
        resolve_global_tags=lambda _cfg: {"title": "Titre", "GENRE": "Drama"},
        video_tracks=lambda config: list(config.video_tracks) if config.video_tracks else [config.video],
        video_source_from_settings=lambda config, video: video.source_path or config.source,
        video_source_path=lambda config: config.video.source_path or config.source,
        video_stream_index=lambda config: config.video.stream_index,
        video_map_key=lambda config: (Path(config.video.source_path or config.source), int(config.video.stream_index), "video"),
    )

    assert plan.all_sources == (src, alt)
    assert dict(plan.source_idx) == {src: 0, alt: 1}
    assert plan.resolved_subtitle_tracks == ((alt, 4),)
    assert plan.subtitles_resolved is True
    assert plan.video_source == alt
    assert plan.video_stream == 2
    assert plan.video_default_map == (1, 2)
    assert len(plan.video_tracks) == 1
    assert plan.video_tracks[0].source == alt
    assert plan.video_tracks[0].stream_index == 2
    assert plan.video_tracks[0].codec == "copy"
    assert plan.sync_analysis.enabled is True
    assert plan.sync_analysis.needs_subtitle_prescan is True
    assert plan.sync_analysis.probe_remux_config is not None
    assert dict(plan.offset_lookup) == {("audio", src, 1): 120}
    assert plan.container_metadata.tag_source is None
    assert dict(plan.container_metadata.global_tags) == {"title": "Titre", "GENRE": "Drama"}


def test_resolve_subtitle_tracks_for_encode_dedupes_explicit_tracks(tmp_path):
    src = tmp_path / "src.mkv"
    src.touch()
    cfg = _make_config(
        src,
        tmp_path / "out.mkv",
        subtitle_tracks=[(src, 3), (src, 3), (src, 4)],
        copy_subtitles=True,
    )

    resolved = resolve_subtitle_tracks_for_encode(
        cfg,
        [src],
        probe_indices=lambda *_args: pytest.fail("probe should not be used"),
    )

    assert resolved.complete is True
    assert resolved.tracks == ((src, 3), (src, 4))


def test_resolve_subtitle_tracks_for_encode_marks_incomplete_when_probe_fails(tmp_path):
    src = tmp_path / "src.mkv"
    src.touch()
    cfg = _make_config(src, tmp_path / "out.mkv", subtitle_tracks=[], copy_subtitles=True)

    resolved = resolve_subtitle_tracks_for_encode(
        cfg,
        [src],
        probe_indices=lambda *_args: None,
    )

    assert resolved.complete is False
    assert resolved.tracks == ()


def test_build_offset_specs_rejects_negative_video_offsets(tmp_path):
    src = tmp_path / "src.mkv"
    src.touch()
    cfg = _make_config(
        src,
        tmp_path / "out.mkv",
        track_time_offsets=[
            TrackTimeOffset(track_type="video", source_path=src, stream_index=0, offset_ms=-100),
        ],
    )

    with pytest.raises(EncodeError):
        build_offset_specs(
            cfg,
            track_mappings=[((src, 0, "video"), src, 0)],
        )


def test_build_offset_specs_and_video_map_arg_use_lookup(tmp_path):
    src = tmp_path / "src.mkv"
    src.touch()
    cfg = _make_config(
        src,
        tmp_path / "out.mkv",
        track_time_offsets=[
            TrackTimeOffset(track_type="audio", source_path=src, stream_index=1, offset_ms=125),
        ],
    )

    lookup = track_time_offset_lookup(cfg)
    specs = build_offset_specs(
        cfg,
        track_mappings=[((src, 1, "audio"), src, 1)],
        offset_lookup=lookup,
    )

    assert len(specs) == 1
    assert specs[0].offset_ms == 125
    assert video_map_arg((0, 0), offset_remap={}, map_key=(src, 0, "video")) == "0:v:0"
    assert video_map_arg((0, 0), offset_remap={(src, 0, "video"): (2, 0)}, map_key=(src, 0, "video")) == "2:0"


def test_prepare_container_metadata_inputs_materializes_chapters_and_extra_tag_input(tmp_path):
    src = tmp_path / "src.mkv"
    tag = tmp_path / "tag.mkv"
    src.touch()
    tag.touch()
    cfg = _make_config(
        src,
        tmp_path / "out.mkv",
        chapter_overrides=[ChapterEntry(timecode_s=0.0, name="Intro")],
        tag_sources=[tag],
    )
    cmd: list[str] = []
    expected_chapter_file = tmp_path / "chapters_1_123.ffmeta"

    planned = prepare_container_metadata_inputs(
        cmd,
        cfg,
        source_idx={src: 0},
        next_input_index=1,
        chapter_materialize_dir=tmp_path,
        probe_duration_seconds=lambda path: 123.0 if path == src else None,
        write_ffmetadata_chapters=lambda entries, out_dir, duration_s: out_dir / f"chapters_{len(entries)}_{int(duration_s or 0)}.ffmeta",
    )

    assert planned.chapter_input_index == 1
    assert planned.tag_input_index == 2
    assert planned.next_input_index == 3
    assert cmd == ["-i", str(expected_chapter_file), "-i", str(tag)]


def test_materialize_container_metadata_inputs_returns_args_and_indices(tmp_path):
    src = tmp_path / "src.mkv"
    tag = tmp_path / "tag.mkv"
    src.touch()
    tag.touch()
    cfg = _make_config(
        src,
        tmp_path / "out.mkv",
        chapter_overrides=[ChapterEntry(timecode_s=0.0, name="Intro")],
        tag_sources=[tag],
    )

    materialized = materialize_container_metadata_inputs(
        cfg,
        source_idx={src: 0},
        next_input_index=1,
        container_metadata_plan=build_container_metadata_plan(
            cfg,
            resolve_global_tags=lambda _cfg: {"title": "Titre"},
        ),
        chapter_materialize_dir=tmp_path,
        probe_duration_seconds=lambda _path: 123.0,
        write_ffmetadata_chapters=lambda entries, out_dir, duration_s: out_dir / f"chapters_{len(entries)}_{int(duration_s or 0)}.ffmeta",
    )

    assert materialized.chapter_input_index == 1
    assert materialized.tag_input_index == 2
    assert materialized.next_input_index == 3
    assert materialized.input_args == (
        "-i",
        str(tmp_path / "chapters_1_123.ffmeta"),
        "-i",
        str(tag),
    )


def test_append_container_metadata_args_applies_maps_and_tags(tmp_path):
    src = tmp_path / "src.mkv"
    src.touch()
    cfg = _make_config(
        src,
        tmp_path / "out.mkv",
        tag_overrides={"GENRE": "Drama"},
        file_title="Titre",
    )
    cmd: list[str] = []

    append_container_metadata_args(
        cmd,
        cfg,
        default_metadata_input_index=0,
        default_chapter_input_index=0,
        chapter_input_index=None,
        tag_input_index=None,
        include_copy_video_stream_passthrough=True,
        is_video_passthrough=lambda _cfg: True,
        resolve_global_tags=lambda _cfg: {"title": "Titre", "GENRE": "Drama"},
        build_track_meta_args=lambda _cfg, _plan: ["-metadata:s:a:0", "language=fre"],
    )

    assert cmd[:4] == ["-map_metadata", "0", "-map_metadata:s:v:0", "0:s:v:0"]
    assert "-map_chapters" in cmd and cmd[cmd.index("-map_chapters") + 1] == "0"
    assert "title=Titre" in cmd
    assert "GENRE=Drama" in cmd
    assert cmd[-2:] == ["-metadata:s:a:0", "language=fre"]


def test_container_metadata_helpers_keep_existing_behavior(tmp_path):
    src = tmp_path / "src.mkv"
    src.touch()
    cfg = _make_config(
        src,
        tmp_path / "out.mkv",
        tag_overrides={},
        chapter_overrides=[],
    )

    chapter_map = container_chapter_map_value(
        cfg,
        default_chapter_input_index=0,
        chapter_input_index=None,
    )
    metadata_map = container_metadata_map_value(
        cfg,
        default_metadata_input_index=0,
        chapter_input_index=None,
        tag_input_index=None,
        include_copy_video_stream_passthrough=False,
        is_video_passthrough=lambda _cfg: False,
        chapter_map=chapter_map,
    )

    assert chapter_map == "-1"
    assert metadata_map == "-1"


def test_build_sync_mapped_tracks_keeps_source_order(tmp_path):
    src = tmp_path / "src.mkv"
    alt = tmp_path / "alt.mkv"
    src.touch()
    alt.touch()
    cfg = _make_config(
        src,
        tmp_path / "out.mkv",
        audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy", source_path=alt)],
    )

    mapped = build_sync_mapped_tracks(
        cfg,
        {src: 0, alt: 1},
        [(src, 3)],
    )

    assert [(track.source_file_index, track.stream_index, track.track.track_type) for track in mapped] == [
        (0, 0, "video"),
        (1, 1, "audio"),
        (0, 3, "subtitle"),
    ]


def test_build_probe_remux_config_reuses_resolved_sync_tracks(tmp_path):
    src = tmp_path / "src.mkv"
    alt = tmp_path / "alt.mkv"
    src.touch()
    alt.touch()
    cfg = _make_config(
        src,
        tmp_path / "out.mkv",
        audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy", source_path=alt)],
        keep_chapters=False,
    )

    remux_cfg = build_probe_remux_config(
        cfg,
        [src, alt],
        {src: 0, alt: 1},
        [(src, 3)],
    )

    assert [source.path for source in remux_cfg.sources] == [src, alt]
    assert [(track.track_type, track.mkv_tid) for track in remux_cfg.sources[0].tracks] == [("video", 0), ("subtitle", 3)]
    assert [(track.track_type, track.mkv_tid) for track in remux_cfg.sources[1].tracks] == [("audio", 1)]
    assert remux_cfg.keep_chapters is False


def test_requires_file_sync_fallback_for_offsets_detects_foreign_audio_offset(tmp_path):
    src = tmp_path / "src.mkv"
    alt = tmp_path / "alt.mkv"
    src.touch()
    alt.touch()
    cfg = _make_config(src, tmp_path / "out.mkv")
    mapped_tracks = [
        EncodeSyncMappedTrack(source_file_index=0, stream_index=0, track=EncodeSyncTrack("video")),
        EncodeSyncMappedTrack(source_file_index=1, stream_index=2, track=EncodeSyncTrack("audio")),
    ]

    requires_fallback = requires_file_sync_fallback_for_offsets(
        cfg,
        mapped_tracks,
        {0: src, 1: alt},
        track_offset_ms=lambda lookup, **kwargs: lookup.get((kwargs["track_type"], kwargs["source_path"], kwargs["stream_index"]), 0),
        offset_lookup={("audio", alt, 2): 120},
    )

    assert requires_fallback is True


def test_needs_strict_interleave_for_encode_tracks_foreign_audio():
    mapped_tracks = [
        EncodeSyncMappedTrack(source_file_index=0, stream_index=0, track=EncodeSyncTrack("video")),
        EncodeSyncMappedTrack(source_file_index=1, stream_index=1, track=EncodeSyncTrack("audio")),
        EncodeSyncMappedTrack(source_file_index=0, stream_index=3, track=EncodeSyncTrack("subtitle")),
    ]

    assert needs_strict_interleave_for_encode(mapped_tracks) is True


def test_build_sync_analysis_plan_marks_foreign_offset_as_file_fallback(tmp_path):
    src = tmp_path / "src.mkv"
    alt = tmp_path / "alt.mkv"
    src.touch()
    alt.touch()
    cfg = _make_config(
        src,
        tmp_path / "out.mkv",
        audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy", source_path=alt)],
        subtitle_tracks=[(src, 3)],
        track_time_offsets=[
            TrackTimeOffset(track_type="audio", source_path=alt, stream_index=1, offset_ms=125),
        ],
    )

    analysis = build_sync_analysis_plan(
        cfg,
        [src, alt],
        {src: 0, alt: 1},
        [(src, 3)],
        subtitles_resolved=True,
        offset_lookup=track_time_offset_lookup(cfg),
    )

    assert analysis.enabled is True
    assert analysis.offset_requires_file_fallback is True
    assert analysis.strict_interleave_without_prescan is True
    assert analysis.allow_live_sync is False
    assert analysis.probe_remux_config is None


def test_resolve_track_assembly_reuses_sync_and_video_maps(tmp_path):
    src = tmp_path / "src.mkv"
    alt = tmp_path / "alt.mkv"
    sync_audio = tmp_path / "sync_audio.mka"
    src.touch()
    alt.touch()
    sync_audio.touch()
    cfg = _make_config(
        src,
        tmp_path / "out.mkv",
        video=VideoEncodeSettings(codec="copy", source_path=alt, stream_index=2),
        audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy", source_path=alt)],
        subtitle_tracks=[(src, 3)],
    )
    plan = build_encode_plan(
        cfg,
        probe_subtitle_indices=lambda *_args: pytest.fail("explicit subtitles should skip probe"),
        resolve_global_tags=lambda _cfg: {},
        video_tracks=lambda config: list(config.video_tracks) if config.video_tracks else [config.video],
        video_source_from_settings=lambda config, video: video.source_path or config.source,
        video_source_path=lambda config: config.video.source_path or config.source,
        video_stream_index=lambda config: config.video.stream_index,
        video_map_key=lambda config: (Path(config.video.source_path or config.source), int(config.video.stream_index), "video"),
    )

    assembly = resolve_track_assembly(
        cfg,
        plan,
        source_idx={src: 0, alt: 1},
        track_input_paths=build_track_input_paths(all_sources=[src, alt], sync_inputs=[sync_audio]),
        sync_remap={(alt, 1, "audio"): (2, 0)},
    )

    assert assembly.video_map == (1, 2)
    assert ((alt, 2, "video"), alt, 2) in assembly.track_mappings
    assert ((alt, 1, "audio"), sync_audio, 0) in assembly.track_mappings
    assert ((src, 3, "subtitle"), src, 3) in assembly.track_mappings


def test_build_encode_command_selection_picks_pass2_for_preview(tmp_path):
    src = tmp_path / "src.mkv"
    src.touch()
    cfg = _make_config(src, tmp_path / "out.mkv")
    plan = build_encode_plan(
        cfg,
        probe_subtitle_indices=lambda *_args: [],
        resolve_global_tags=lambda _cfg: {},
        video_tracks=lambda config: list(config.video_tracks) if config.video_tracks else [config.video],
        video_source_from_settings=lambda config, video: video.source_path or config.source,
        video_source_path=lambda config: config.video.source_path or config.source,
        video_stream_index=lambda config: config.video.stream_index,
        video_map_key=lambda config: (Path(config.video.source_path or config.source), int(config.video.stream_index), "video"),
    )

    selection = build_encode_command_selection(
        cfg,
        plan=plan,
        is_multi_video=lambda _cfg: False,
        uses_two_pass=lambda _cfg: True,
        build_multi_video_preview=lambda *_args, **_kwargs: pytest.fail("multi-video path should not be used"),
        build_two_pass=lambda *_args, **_kwargs: [["ffmpeg", "-pass", "1"], ["ffmpeg", "-pass", "2", "out.mkv"]],
        build_single_pass=lambda *_args, **_kwargs: pytest.fail("single-pass path should not be used"),
    )

    assert selection.is_two_pass is True
    assert selection.is_multi_video is False
    assert selection.preview_command == ("ffmpeg", "-pass", "2", "out.mkv")


def test_format_preview_selection_adds_two_pass_prefix():
    from core.workflows.encode.planning.plan_models import EncodeCommandSelection

    selection = EncodeCommandSelection(
        commands=(("ffmpeg", "-pass", "1"), ("ffmpeg", "-pass", "2", "out.mkv")),
        preview_index=1,
        is_multi_video=False,
        is_two_pass=True,
    )

    preview = format_preview_selection(selection)

    assert preview.startswith("# Mode taille cible")
    assert "-pass 2" in preview
