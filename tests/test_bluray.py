from __future__ import annotations

import json
import subprocess
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.batch import discover_direct_batch_jobs
from cli.constants import EXIT_VALIDATION
from cli.errors import CliError
from cli.inspection import inspect_sources
from cli.logging import Logger
from cli.options import CommonOptions
from cli.schema import build_cli_json_schema
from core.bluray import (
    BluRayStreamInfo,
    append_ffmpeg_input_args,
    discover_titles,
    ffmpeg_input_args,
    ffprobe_input_args,
    find_disc_root,
    is_bluray_playlist,
    playlist_id_from_path,
    playlist_path_for,
    title_for_playlist,
    validate_bluray_source,
)
from core.inspector import FileInspector
from core.workflows.common.chapters import probe_media_duration_seconds
from core.workflows.encode.backends.models import BackendContext
from core.workflows.encode.backends.nvencc_backend import NvenccEncodeBackend
from core.workflows.encode.domain import EncodeCodecDomainCallbacks
from core.workflows.encode.models import EncodeConfig, VideoEncodeSettings
from core.workflows.encode.runtime.video_preparation import (
    VideoOnlyCommandBuilder,
    VideoOnlyCommandBuilderCallbacks,
)
from core.workflows.remux_command import build_remux_command
from core.workflows.remux_mapping import OffsetInputSpec, append_offset_inputs
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry


def _stn_entry(pid: int) -> bytes:
    return bytes([9, 1, (pid >> 8) & 0xFF, pid & 0xFF, 0, 0, 0, 0, 0, 0])


def _stn_attr(kind: str, language: str, coding_type: int) -> bytes:
    language_bytes = language.encode("ascii")[:3].ljust(3, b"\0")
    if kind == "audio":
        return bytes([5, coding_type, 0x61, *language_bytes])
    if kind == "subtitle":
        return bytes([5, coding_type, *language_bytes, 0])
    return bytes([5, coding_type, 0x81, 0x12, 0, 0])


def _stn_table(streams: list[tuple[str, int, str, int]]) -> bytes:
    kinds = ("video", "audio", "subtitle")
    ordered = [stream for kind in kinds for stream in streams if stream[0] == kind]
    counts = [sum(1 for stream in streams if stream[0] == kind) for kind in kinds]
    payload = bytearray()
    payload.extend(b"\0\0")  # length placeholder
    payload.extend(b"\0\0")
    payload.extend(bytes([counts[0], counts[1], counts[2], 0, 0, 0, 0]))
    payload.extend(b"\0\0\0\0\0")
    for kind, pid, language, coding_type in ordered:
        payload.extend(_stn_entry(pid))
        payload.extend(_stn_attr(kind, language, coding_type))
    payload[:2] = (len(payload) - 4).to_bytes(2, "big")
    return bytes(payload)


def _mpls_bytes(
    items: list[tuple[str, float]],
    *,
    streams: list[tuple[str, int, str, int]] | None = None,
) -> bytes:
    playlist_start = 58
    data = bytearray(playlist_start)
    data[:4] = b"MPLS"
    data[4:8] = b"0200"
    data[8:12] = playlist_start.to_bytes(4, "big")

    section = bytearray()
    section.extend(b"\0\0\0\0")  # playlist section length, unused by parser
    section.extend(b"\0\0")
    section.extend(len(items).to_bytes(2, "big"))
    section.extend(b"\0\0")  # subpath count

    for clip_id, duration_s in items:
        item = bytearray(34 if streams else 22)
        item[2:7] = clip_id.encode("ascii")
        item[7:11] = b"M2TS"
        item[14:18] = (0).to_bytes(4, "big")
        item[18:22] = int(duration_s * 45_000).to_bytes(4, "big")
        if streams:
            item.extend(_stn_table(streams))
        item[0:2] = (len(item) - 2).to_bytes(2, "big")
        section.extend(item)

    section[:4] = (len(section) - 4).to_bytes(4, "big")
    data.extend(section)
    return bytes(data)


def _write_playlist(
    root: Path,
    *,
    playlist_id: int,
    items: list[tuple[str, float]],
    streams: list[tuple[str, int, str, int]] | None = None,
) -> Path:
    playlist = root / "BDMV" / "PLAYLIST" / f"{playlist_id:05d}.mpls"
    playlist.write_bytes(_mpls_bytes(items, streams=streams))
    return playlist


def _make_bluray_tree(
    tmp_path: Path,
    *,
    playlist_id: int = 1,
    items: list[tuple[str, float]] | None = None,
    streams: list[tuple[str, int, str, int]] | None = None,
) -> tuple[Path, Path]:
    root = tmp_path / "disc"
    playlist_dir = root / "BDMV" / "PLAYLIST"
    stream_dir = root / "BDMV" / "STREAM"
    playlist_dir.mkdir(parents=True)
    stream_dir.mkdir(parents=True)
    (root / "BDMV" / "index.bdmv").write_bytes(b"index")
    items = items or [("00001", 10.0), ("00002", 5.5)]
    for index, (clip_id, _duration) in enumerate(items, start=1):
        (stream_dir / f"{clip_id}.m2ts").write_bytes(bytes(index * 7))
    playlist = _write_playlist(root, playlist_id=playlist_id, items=items, streams=streams)
    return root, playlist


def _track(tid: int, track_type: str = "video") -> TrackEntry:
    return TrackEntry(
        mkv_tid=tid,
        track_type=track_type,
        codec="HEVC" if track_type == "video" else "COPY",
        display_info="",
        language="",
        title="",
        file_id="src0",
    )


def test_bluray_path_detection_disc_root_and_playlist_resolution(tmp_path: Path) -> None:
    root, playlist = _make_bluray_tree(tmp_path, playlist_id=12)
    bdmv = root / "BDMV"

    assert playlist_id_from_path(playlist) == 12
    assert playlist_id_from_path(tmp_path / "abc.mpls") is None
    assert is_bluray_playlist(playlist)
    assert not is_bluray_playlist(root / "00012.mpls")
    assert find_disc_root(root) == root
    assert find_disc_root(bdmv) == root
    assert find_disc_root(playlist) == root
    assert playlist_path_for(root, 12) == playlist
    assert playlist_path_for(bdmv, 12) == playlist
    assert playlist_path_for(playlist) == playlist
    assert playlist_path_for(root, 99) is None


def test_bluray_title_parser_segments_size_streams_and_ffmpeg_args(tmp_path: Path) -> None:
    root, playlist = _make_bluray_tree(tmp_path, playlist_id=12)

    title = title_for_playlist(playlist)

    assert title is not None
    assert title.playlist_id == 12
    assert title.duration_s == pytest.approx(15.5)
    assert [segment.path.name for segment in title.segments] == ["00001.m2ts", "00002.m2ts"]
    assert title.size_bytes == 21
    assert discover_titles(root)[0].playlist_path == playlist
    assert ffmpeg_input_args(playlist) == ["-playlist", "12", "-i", f"bluray:{root}"]
    assert ffprobe_input_args(playlist) == ["-playlist", "12", "-i", f"bluray:{root}"]

    normal = tmp_path / "normal.mkv"
    normal.touch()
    assert ffprobe_input_args(normal) == [str(normal)]

    cmd = ["ffmpeg"]
    append_ffmpeg_input_args(cmd, playlist)
    assert cmd == ["ffmpeg", "-playlist", "12", "-i", f"bluray:{root}"]


def test_bluray_title_parser_ignores_invalid_mpls_payload(tmp_path: Path) -> None:
    root, playlist = _make_bluray_tree(tmp_path, playlist_id=2)
    playlist.write_bytes(b"not a real playlist")

    title = title_for_playlist(playlist)

    assert title is not None
    assert title.duration_s == 0
    assert title.segments == ()
    assert title.streams == ()
    assert discover_titles(root) == []


def test_bluray_title_parser_extracts_stn_languages(tmp_path: Path) -> None:
    _root, playlist = _make_bluray_tree(
        tmp_path,
        playlist_id=4,
        streams=[
            ("video", 0x1011, "", 0x24),
            ("audio", 0x1100, "eng", 0x86),
            ("audio", 0x1101, "fra", 0x81),
            ("subtitle", 0x12A0, "eng", 0x90),
            ("subtitle", 0x12A1, "deu", 0x90),
        ],
    )

    title = title_for_playlist(playlist)

    assert title is not None
    by_pid = title.stream_by_pid
    assert by_pid[0x1100].language == "eng"
    assert by_pid[0x1101].language == "fra"
    assert by_pid[0x12A0].kind == "subtitle"
    assert by_pid[0x12A0].language == "eng"
    assert by_pid[0x12A0].ordinal == 0
    assert by_pid[0x12A0].coding_type == 0x90
    assert isinstance(by_pid[0x12A0], BluRayStreamInfo)


def test_bluray_discovery_sorts_by_duration_size_and_filters_titles(tmp_path: Path) -> None:
    root, _playlist = _make_bluray_tree(
        tmp_path,
        playlist_id=1,
        items=[("00001", 10.0)],
    )
    _write_playlist(root, playlist_id=2, items=[("00001", 10.0), ("00002", 5.0)])
    _write_playlist(root, playlist_id=3, items=[("00001", 15.0)])
    (root / "BDMV" / "PLAYLIST" / "bad.mpls").write_bytes(b"ignored")

    all_titles = discover_titles(root)
    filtered_titles = discover_titles(root, min_duration_s=12.0)

    assert [title.playlist_id for title in all_titles] == [2, 3, 1]
    assert [title.playlist_id for title in filtered_titles] == [2, 3]


def test_bluray_validation_reports_missing_segments(tmp_path: Path) -> None:
    _root, playlist = _make_bluray_tree(tmp_path, playlist_id=6, items=[("00031", 10.0)])
    segment = playlist.parents[1] / "STREAM" / "00031.m2ts"
    segment.unlink()

    errors = validate_bluray_source(playlist)

    assert errors == [f"Segments Blu-ray introuvables pour {playlist.name} : 00031.m2ts"]
    assert validate_bluray_source(tmp_path / "normal.mkv") == []


def test_remux_command_reads_bluray_playlist_via_libbluray(tmp_path: Path) -> None:
    root, playlist = _make_bluray_tree(tmp_path, playlist_id=1)
    cfg = RemuxConfig(
        sources=[SourceInput(path=playlist, file_index=0, tracks=[_track(0)])],
        output=tmp_path / "out.mkv",
        track_order=[(0, 0)],
        keep_chapters=False,
    )

    cmd = build_remux_command(
        cfg,
        ffmpeg_bin="ffmpeg",
        ffmpeg_progress_args=[],
        ffmpeg_thread_args=[],
        cli_path=lambda value: str(value),
    )

    assert cmd[cmd.index("-playlist") + 1] == "1"
    assert cmd[cmd.index("-i") + 1] == f"bluray:{root}"
    assert "-map" in cmd
    assert "0:0" in [cmd[index + 1] for index, part in enumerate(cmd[:-1]) if part == "-map"]


def test_remux_offset_inputs_read_bluray_playlist_via_libbluray(tmp_path: Path) -> None:
    root, playlist = _make_bluray_tree(tmp_path, playlist_id=5)
    cmd: list[str] = []

    next_index, remap = append_offset_inputs(
        cmd,
        [
            OffsetInputSpec(
                map_key=(0, 1, "audio", 0),
                input_path=playlist,
                input_stream_index=1,
                offset_ms=250,
            ),
            OffsetInputSpec(
                map_key=(0, 2, "subtitle", 0),
                input_path=playlist,
                input_stream_index=2,
                offset_ms=-80,
            ),
        ],
        start_input_index=1,
        cli_path=lambda value: str(value),
    )

    assert next_index == 3
    assert remap == {
        (0, 1, "audio", 0): (1, 1),
        (0, 2, "subtitle", 0): (2, 2),
    }
    assert cmd == [
        "-itsoffset", "0.250", "-playlist", "5", "-i", f"bluray:{root}",
        "-ss", "0.080", "-playlist", "5", "-i", f"bluray:{root}",
    ]


def test_chapter_duration_probe_prefers_mpls_duration_without_ffprobe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, playlist = _make_bluray_tree(tmp_path, playlist_id=2, items=[("00041", 10.0), ("00042", 5.0)])

    def fail_run(*_args, **_kwargs):
        raise AssertionError("ffprobe should not be called for a valid Blu-ray playlist duration")

    monkeypatch.setattr("core.workflows.common.chapters.subprocess.run", fail_run)

    assert probe_media_duration_seconds("ffprobe", playlist) == pytest.approx(15.0)


def test_inspector_uses_bluray_duration_segments_and_probe_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, playlist = _make_bluray_tree(tmp_path, playlist_id=7, items=[("00011", 12.0)])
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        commands.append([str(part) for part in cmd])
        executable = Path(str(cmd[0])).name
        if executable == "ffprobe":
            if "-show_frames" in cmd:
                stdout = json.dumps({"frames": []})
            else:
                stdout = json.dumps(
                    {
                        "format": {
                            "format_name": "mpegts",
                            "duration": "3.0",
                            "size": "3",
                            "bit_rate": "1000",
                        },
                        "streams": [
                            {
                                "index": 0,
                                "codec_type": "video",
                                "codec_name": "hevc",
                                "codec_long_name": "H.265 / HEVC",
                                "width": 3840,
                                "height": 2160,
                                "pix_fmt": "yuv420p10le",
                                "r_frame_rate": "24000/1001",
                                "avg_frame_rate": "24000/1001",
                            }
                        ],
                        "chapters": [],
                    }
                )
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        if executable == "mediainfo":
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"media": {"track": []}}), stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected command")

    monkeypatch.setattr("core.inspector.subprocess.run", fake_run)

    info = FileInspector(ffprobe_bin="ffprobe", mediainfo_bin="mediainfo").inspect(playlist)

    assert info.source_kind == "bluray"
    assert info.source_label == "00007.mpls"
    assert info.bluray_playlist_id == 7
    assert info.bluray_segments == ["00011.m2ts"]
    assert info.duration_s == pytest.approx(12.0)
    assert info.size_bytes == 7
    assert any(
        ["-playlist", "7", "-i", f"bluray:{root}"] == command[-4:]
        for command in commands
        if command and command[0] == "ffprobe"
    )
    assert any(command[-1].endswith("00011.m2ts") for command in commands if command and command[0] == "mediainfo")


def test_inspector_enriches_bluray_track_languages_from_mpls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, playlist = _make_bluray_tree(
        tmp_path,
        playlist_id=8,
        items=[("00012", 12.0)],
        streams=[
            ("audio", 0x1100, "eng", 0x86),
            ("audio", 0x1101, "fra", 0x81),
            ("subtitle", 0x12A0, "eng", 0x90),
        ],
    )

    def fake_run(cmd: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        executable = Path(str(cmd[0])).name
        if executable == "ffprobe":
            stdout = json.dumps(
                {
                    "format": {"format_name": "mpegts", "duration": "3.0"},
                    "streams": [
                        {
                            "index": 0,
                            "id": "0x1011",
                            "codec_type": "video",
                            "codec_name": "hevc",
                            "codec_long_name": "H.265 / HEVC",
                        },
                        {
                            "index": 1,
                            "id": "0x1100",
                            "codec_type": "audio",
                            "codec_name": "dts",
                            "codec_long_name": "DTS-HD MA",
                        },
                        {
                            "index": 2,
                            "id": "0x1101",
                            "codec_type": "audio",
                            "codec_name": "ac3",
                            "codec_long_name": "AC-3",
                        },
                        {
                            "index": 3,
                            "id": "0x12a0",
                            "codec_type": "subtitle",
                            "codec_name": "hdmv_pgs_subtitle",
                        },
                        {
                            "index": 4,
                            "id": "0x12a1",
                            "codec_type": "subtitle",
                            "codec_name": "hdmv_pgs_subtitle",
                        },
                    ],
                    "chapters": [],
                }
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        if executable == "mediainfo":
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"media": {"track": []}}), stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected command")

    monkeypatch.setattr("core.inspector.subprocess.run", fake_run)

    info = FileInspector(ffprobe_bin="ffprobe", mediainfo_bin="mediainfo").inspect(playlist)

    assert [track.language for track in info.audio_tracks] == ["en-US", "fr-FR"]
    assert info.subtitle_tracks[0].language == "en-US"
    assert info.subtitle_tracks[1].language is None


def test_inspector_preserves_existing_ffprobe_language_over_mpls_language(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, playlist = _make_bluray_tree(
        tmp_path,
        playlist_id=9,
        items=[("00013", 12.0)],
        streams=[("audio", 0x1100, "eng", 0x86)],
    )

    def fake_run(cmd: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        executable = Path(str(cmd[0])).name
        if executable == "ffprobe":
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps(
                    {
                        "format": {"format_name": "mpegts"},
                        "streams": [
                            {
                                "index": 1,
                                "id": "0x1100",
                                "codec_type": "audio",
                                "codec_name": "dts",
                                "codec_long_name": "DTS-HD MA",
                                "tags": {"language": "fra"},
                            }
                        ],
                        "chapters": [],
                    }
                ),
                stderr="",
            )
        if executable == "mediainfo":
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"media": {"track": []}}), stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected command")

    monkeypatch.setattr("core.inspector.subprocess.run", fake_run)

    info = FileInspector(ffprobe_bin="ffprobe", mediainfo_bin="mediainfo").inspect(playlist)

    assert [track.language for track in info.audio_tracks] == ["fr-FR"]


def test_cli_inspection_accepts_explicit_bluray_playlist_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, playlist = _make_bluray_tree(tmp_path, playlist_id=14)
    inspected: list[Path] = []

    class FakeInspector:
        def __init__(self, **_kwargs) -> None:
            pass

        def inspect(self, path: Path):
            inspected.append(path)
            return SimpleNamespace(
                path=path,
                attachments=[],
                chapters=None,
                video_tracks=[],
                audio_tracks=[],
                subtitle_tracks=[],
            )

    monkeypatch.setattr("cli.inspection.FileInspector", FakeInspector)

    sources, infos, tracks = inspect_sources(
        {"sources": [{"path": str(root), "playlist": 14}]},
        SimpleNamespace(tool_ffprobe="ffprobe", tool_mediainfo="mediainfo"),
        CommonOptions(),
        Logger(stream=StringIO()),
    )

    assert inspected == [playlist]
    assert sources[0].path == playlist
    assert infos[0].path == playlist
    assert tracks == []


def test_cli_inspection_auto_selects_longest_bluray_title_for_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _playlist = _make_bluray_tree(tmp_path, playlist_id=1, items=[("00001", 10.0)])
    long_playlist = _write_playlist(root, playlist_id=2, items=[("00001", 120.0)])
    inspected: list[Path] = []

    class FakeInspector:
        def __init__(self, **_kwargs) -> None:
            pass

        def inspect(self, path: Path):
            inspected.append(path)
            return SimpleNamespace(
                path=path,
                attachments=[],
                chapters=None,
                video_tracks=[],
                audio_tracks=[],
                subtitle_tracks=[],
            )

    monkeypatch.setattr("cli.inspection.FileInspector", FakeInspector)

    sources, _infos, _tracks = inspect_sources(
        {"sources": [str(root)]},
        SimpleNamespace(tool_ffprobe="ffprobe", tool_mediainfo="mediainfo"),
        CommonOptions(),
        Logger(stream=StringIO()),
    )

    assert inspected == [long_playlist]
    assert sources[0].path == long_playlist


def test_cli_inspection_rejects_missing_bluray_playlist_id(tmp_path: Path) -> None:
    root, _playlist = _make_bluray_tree(tmp_path, playlist_id=1)

    with pytest.raises(CliError) as exc:
        inspect_sources(
            {"sources": [{"path": str(root), "playlist": 99}]},
            SimpleNamespace(tool_ffprobe="ffprobe", tool_mediainfo="mediainfo"),
            CommonOptions(),
            Logger(stream=StringIO()),
        )

    assert exc.value.exit_code == EXIT_VALIDATION
    assert "00099.mpls" in str(exc.value)


def test_cli_batch_discovers_bluray_root_as_single_job(tmp_path: Path) -> None:
    root, playlist = _make_bluray_tree(tmp_path, playlist_id=3, items=[("00021", 120.0)])
    output_dir = tmp_path / "out"

    discovery = discover_direct_batch_jobs(input_dirs=[str(root)], output_dir=str(output_dir))

    assert discovery.selected == 1
    assert discovery.jobs[0]["sources"] == [{"path": str(playlist)}]
    assert discovery.jobs[0]["output"] == str(output_dir / f"{root.name}.mkv")


def test_cli_batch_skips_raw_m2ts_inside_bluray_root(tmp_path: Path) -> None:
    root, playlist = _make_bluray_tree(tmp_path, playlist_id=3, items=[("00021", 120.0)])
    extra_mkv = root / "extra.mkv"
    extra_mkv.touch()

    discovery = discover_direct_batch_jobs(input_dirs=[str(root.parent)], recursive=True)

    discovered_sources = [job["sources"][0]["path"] for job in discovery.jobs]
    assert str(playlist) in discovered_sources
    assert str(extra_mkv) in discovered_sources
    assert not any(path.endswith(".m2ts") for path in discovered_sources)


def test_cli_schema_allows_source_playlist_property() -> None:
    schema = build_cli_json_schema()

    source_properties = schema["$defs"]["source"]["oneOf"][1]["properties"]
    assert source_properties["playlist"] == {"type": "integer"}


def test_encode_video_preparation_uses_libbluray_input_args(tmp_path: Path) -> None:
    root, playlist = _make_bluray_tree(tmp_path, playlist_id=16)
    callbacks = VideoOnlyCommandBuilderCallbacks(
        ffmpeg_bin="ffmpeg",
        ffmpeg_progress_args=lambda: [],
        ffmpeg_thread_args=lambda _threads: [],
        offset_input_args=lambda _offset: [],
        codec_domain_callbacks=lambda: EncodeCodecDomainCallbacks(platform="linux"),
        primary_video_settings=lambda config: config.video or VideoEncodeSettings(),
        video_source_path=lambda config: config.source,
        video_stream_from_settings=lambda video: int(video.stream_index),
        size_to_bitrate_kbps=lambda _config: 1000,
        size_to_bitrate_kbps_for_video=lambda _config, _video: 1000,
    )

    cmd = VideoOnlyCommandBuilder(callbacks).build_video_track_base_cmd(
        video=VideoEncodeSettings(codec="libx265"),
        source=playlist,
        stream_index=0,
    )

    assert cmd[:5] == ["ffmpeg", "-hide_banner", "-y", "-playlist", "16"]
    assert cmd[cmd.index("-i") + 1] == f"bluray:{root}"


def test_nvencc_backend_rejects_dynamic_hdr_copy_from_bluray_playlist(tmp_path: Path) -> None:
    _root, playlist = _make_bluray_tree(tmp_path, playlist_id=17)
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_hdr10plus=True)
    config = EncodeConfig(source=playlist, output=tmp_path / "out.mkv", video=video)

    class FakeWorkflow:
        _nvencc_bin = "nvencc"

        def _video_tracks(self, _config):
            return [video]

        def _resolve_nvencc_input_routing(self, _config):
            return object()

    errors = NvenccEncodeBackend().validate(
        config,
        plan=None,
        ctx=BackendContext(workflow=FakeWorkflow()),
    )

    assert any("playlist Blu-ray" in error for error in errors)


def test_main_window_global_drop_collects_bluray_playlist_not_raw_segments(tmp_path: Path) -> None:
    root, playlist = _make_bluray_tree(tmp_path, playlist_id=18, items=[("00071", 120.0)])

    from ui.main_window import MainWindow

    sources = MainWindow._collect_drop_source_paths([root])

    assert sources == [playlist]
