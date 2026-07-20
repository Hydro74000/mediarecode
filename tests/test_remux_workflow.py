"""
tests/test_remux_workflow.py — Cahier de test backend remux FFmpeg (RemuxWorkflow).

Couverture visée :
1. Construction de commande
   - sortie MKV obligatoire
   - mapping des pistes selon track_order
   - écriture langue BCP-47 sur `language` (+ purge `language-ietf`)
   - mapping chapitres (copie source vs overrides ffmetadata)
   - tags globaux choisis
   - attachements manuels et issus des sources

2. Exécution réelle (intégration légère)
   - génération d'un MKV de test
   - remux complet via RemuxWorkflow.run()
   - vérification ffprobe des tags, chapitres, langues et attachements

3. Cas sensible attachements source
   - extraction/re-attachement des streams "attachment" et "attached_pic"
   - validation du résultat final (mimetype/filename présents)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtCore import QCoreApplication, Qt

from core.inspector import AttachmentInfo, ChapterEntry
from core.runner import TaskSignals
from core.version import APP_VERSION_LABEL
from core.matroska.editors.segment_info import MatroskaSegmentInfoHeaderEditor
from core.workflows.remux_mapping import (
    requires_file_sync_fallback_for_offsets,
    resolve_mapped_tracks,
)
from core.workflows.remux_models import RemuxConfig, RemuxError, SourceInput, TrackEntry
from core.workflows.remux import RemuxWorkflow
from core.workflows.remux_runtime import RemuxRuntimeRunner, RemuxRuntimeRunnerCallbacks
from core.workflows.remux_plan import plan_remux
from core.matroska.contract import ExpectedMatroskaTrack, MatroskaOutputContract
from core.workflows.remux_sync import (
    decide_strict_interleave_with_prescan,
    prepare_timeline_sync_inputs,
)
from core.workflows.remux_timeline_sync import (
    LiveSyncNotSupportedError,
    SyncPreparedInput,
    TimelineSyncFallbackHelper,
)
from core.workflows.common.sync_rewrite import SyncRewritePreparedInput
from core.matroska.ebml import ascii_element, element, uint_element
from core.matroska.ids import (
    EBML_HEADER_ID, SEGMENT_ID, TRACKS_ID, TRACK_ENTRY_ID, TRACK_NUMBER_ID,
    TRACK_UID_ID, TRACK_TYPE_ID, CODEC_ID_ID,
)


def _fake_run_cmd_writing_candidate(cmd, _cwd, label, _progress_cb, _signals) -> str:
    """Faux run_cmd : matérialise un candidat Matroska minimal pour le commit atomique."""
    if label == "ffmpeg-remux":
        entry = element(TRACK_ENTRY_ID, b"".join((
            uint_element(TRACK_NUMBER_ID, 1), uint_element(TRACK_UID_ID, 1),
            uint_element(TRACK_TYPE_ID, 1), ascii_element(CODEC_ID_ID, "V_MPEG4/ISO/AVC"),
        )))
        Path(cmd[-1]).write_bytes(
            element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff" + element(TRACKS_ID, entry)
        )
    return "ok"


def _track(
    tid: int,
    track_type: str,
    *,
    language: str = "",
    title: str = "",
    codec: str = "COPY",
    time_shift_ms: int = 0,
) -> TrackEntry:
    return TrackEntry(
        mkv_tid=tid,
        track_type=track_type,
        codec=codec,
        display_info="",
        language=language,
        title=title,
        enabled=True,
        file_id="src-0",
        time_shift_ms=time_shift_ms,
        orig_language=language,
        orig_title=title,
    )


@pytest.fixture(autouse=True)
def _qt_app(qt_app):
    """Assure qu'une QApplication existe avant les tests de ce module."""
    return qt_app


def _wait_task(signals, timeout: float = 20.0) -> dict[str, object]:
    app = QCoreApplication.instance()
    assert app is not None, "Q(Core)Application non initialisée"
    state: dict[str, object] = {
        "finished": None,
        "failed": None,
        "cancelled": False,
        "progress": [],
    }
    done = {"value": False}

    signals.progress.connect(
        lambda msg: cast(list[str], state["progress"]).append(msg),
        Qt.ConnectionType.QueuedConnection,
    )

    def on_finished(res: object) -> None:
        state["finished"] = res
        done["value"] = True

    def on_failed(msg: str, exc: object) -> None:
        state["failed"] = (msg, exc)
        done["value"] = True

    def on_cancelled() -> None:
        state["cancelled"] = True
        done["value"] = True

    signals.finished.connect(on_finished, Qt.ConnectionType.QueuedConnection)
    signals.failed.connect(on_failed, Qt.ConnectionType.QueuedConnection)
    signals.cancelled.connect(on_cancelled, Qt.ConnectionType.QueuedConnection)

    deadline = time.monotonic() + timeout
    while not done["value"] and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    return state


def _ffprobe_json(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            str(path),
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    return json.loads(result.stdout or "{}")


def _tag_value(tags: dict | None, key: str) -> str | None:
    if not tags:
        return None
    for k, v in tags.items():
        if k.lower() == key.lower():
            return str(v)
    return None


def _segment_info_apps(path: Path) -> dict[str, str]:
    data = path.read_bytes()
    fields = MatroskaSegmentInfoHeaderEditor().locate_info_application_fields(data)
    out: dict[str, str] = {}
    for field_id, label in (
        (b"\x4d\x80", "muxing_app"),
        (b"\x57\x41", "writing_app"),
    ):
        hit = fields.get(field_id)
        if hit is None:
            continue
        value_offset, value_size = hit
        raw = data[value_offset:value_offset + value_size]
        out[label] = raw.decode("utf-8", errors="replace").rstrip(" \x00")
    return out


class TestRemuxWorkflowBuildCommand:

    def test_validate_rejects_non_mkv_output(self, tmp_path):
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src = tmp_path / "in.mkv"
        src.touch()
        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video")])],
            output=tmp_path / "out.mp4",
            track_order=[(0, 0)],
        )

        errors = wf.validate(cfg)
        assert any(".mkv" in e for e in errors)

    def test_validate_rejects_negative_video_offset(self, tmp_path):
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src = tmp_path / "in.mkv"
        src.touch()
        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video", time_shift_ms=-25)])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
        )

        errors = wf.validate(cfg)
        assert any("vidéo" in e.lower() and "négatif" in e.lower() for e in errors)

    def test_build_command_positive_audio_offset_uses_itsoffset(self, tmp_path):
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src = tmp_path / "in.mkv"
        src.touch()
        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video"), _track(1, "audio", time_shift_ms=125)])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (0, 1)],
            keep_chapters=False,
        )

        cmd = wf.build_command(cfg)
        assert "-itsoffset" in cmd
        assert cmd[cmd.index("-itsoffset") + 1] == "0.125"
        map_values = [cmd[i + 1] for i, tok in enumerate(cmd[:-1]) if tok == "-map"]
        assert "2:1" not in map_values  # un seul offset input attendu ici
        assert "1:1" in map_values

    def test_build_command_negative_subtitle_offset_uses_ss(self, tmp_path):
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src = tmp_path / "in.mkv"
        src.touch()
        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video"), _track(2, "subtitle", time_shift_ms=-80)])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (0, 2)],
            keep_chapters=False,
        )

        cmd = wf.build_command(cfg)
        assert "-ss" in cmd
        assert cmd[cmd.index("-ss") + 1] == "0.080"
        map_values = [cmd[i + 1] for i, tok in enumerate(cmd[:-1]) if tok == "-map"]
        assert "1:2" in map_values

    def test_build_command_external_srt_subtitle_is_mapped_and_offset_applied(self, tmp_path):
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src_video = tmp_path / "in.mkv"
        src_sub = tmp_path / "subtitles.srt"
        src_video.touch()
        src_sub.write_text("1\n00:00:01,000 --> 00:00:02,000\nBonjour\n", encoding="utf-8")

        cfg = RemuxConfig(
            sources=[
                SourceInput(path=src_video, file_index=0, tracks=[_track(0, "video")]),
                SourceInput(path=src_sub, file_index=1, tracks=[_track(0, "subtitle", codec="SUBRIP", time_shift_ms=250)]),
            ],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (1, 0)],
            keep_chapters=False,
        )

        cmd = wf.build_command(cfg)
        assert "-itsoffset" in cmd
        assert cmd[cmd.index("-itsoffset") + 1] == "0.250"
        map_values = [cmd[i + 1] for i, tok in enumerate(cmd[:-1]) if tok == "-map"]
        assert "0:0" in map_values
        assert "2:0" in map_values

    def test_runtime_sync_rewrite_consumes_audio_offset(self, tmp_path, monkeypatch):
        src = tmp_path / "in.mkv"
        src.touch()
        cfg = RemuxConfig(
            sources=[
                SourceInput(
                    path=src,
                    file_index=0,
                    tracks=[
                        _track(0, "video"),
                        _track(1, "audio", codec="EAC3", time_shift_ms=250),
                    ],
                )
            ],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (0, 1)],
            keep_chapters=False,
            work_dir=tmp_path,
        )
        captured: dict[str, object] = {}

        class FakeSyncRewriteService:
            def __init__(self, **kwargs):
                captured["service_kwargs"] = kwargs

            def maybe_materialize(self, **kwargs):
                path = tmp_path / "rewritten.mka"
                path.write_bytes(b"audio")
                captured["rewrite_kwargs"] = kwargs
                return SyncRewritePreparedInput(
                    path=path,
                    input_idx=int(kwargs["input_idx"]),
                    track_type="audio",
                    codec="eac3",
                    mode_label="Sync réelle · audio réencodé",
                    bitrate_kbps=640,
                )

        monkeypatch.setattr(
            "core.workflows.remux_runtime.SyncRewriteService",
            FakeSyncRewriteService,
        )

        def build_command(_config, **kwargs):
            captured["sync_inputs"] = list(kwargs.get("sync_inputs") or [])
            captured["mapped_tracks"] = list(kwargs.get("mapped_tracks_override") or [])
            return ["ffmpeg", "-hide_banner", "-y", str(cfg.output)]

        runner = RemuxRuntimeRunner(
            RemuxRuntimeRunnerCallbacks(
                ffmpeg_bin="ffmpeg",
                ffprobe_bin="ffprobe",
                ffmpeg_thread_args=lambda: [],
                build_command=build_command,
                log_workflow_type=lambda _kind: None,
                log_step=lambda _idx, _name: None,
                log=lambda _level, _message: None,
                bind_temp_cleanup=lambda _signals, _paths: None,
                run_cmd=_fake_run_cmd_writing_candidate,
                apply_muxing_post_action=lambda _path: None,
                apply_language_post_action=lambda _path: None,
                write_nfo=lambda _path: None,
                sync_rewrite_enabled=lambda: True,
                sync_advanced_audio_rewrite_enabled=lambda: True,
                sync_rewrite_audio_bitrates=lambda: {"eac3": 96},
            )
        )

        execution_plan = replace(
            plan_remux(cfg),
            output_contract=MatroskaOutputContract(
                track_types=("video",),
                expected_tracks=(ExpectedMatroskaTrack("video"),),
                duration_coherent=False,
            ),
        )
        state = _wait_task(runner.run(cfg, execution_plan))

        assert state["failed"] is None
        assert captured["sync_inputs"]
        mapped_tracks = cast(list, captured["mapped_tracks"])
        rewritten_audio = next(mt for mt in mapped_tracks if mt.track.track_type == "audio")
        assert rewritten_audio.track.time_shift_ms == 0
        assert rewritten_audio.stream_index == 0
        assert rewritten_audio.track.sync_rewrite_label == "Sync réelle · audio réencodé"
        rewrite_kwargs = cast(dict, captured["rewrite_kwargs"])
        assert rewrite_kwargs["preserve_source_audio_params"] is True
        service_kwargs = cast(dict, captured["service_kwargs"])
        assert service_kwargs["advanced_audio_enabled"] is True

    def test_runtime_sync_rewrite_respects_forced_standard_offset(self, tmp_path, monkeypatch):
        src = tmp_path / "in.mkv"
        src.touch()
        audio = _track(1, "audio", codec="EAC3", time_shift_ms=250)
        audio.sync_rewrite_mode = "offset"
        cfg = RemuxConfig(
            sources=[
                SourceInput(
                    path=src,
                    file_index=0,
                    tracks=[_track(0, "video"), audio],
                )
            ],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (0, 1)],
            keep_chapters=False,
            work_dir=tmp_path,
        )

        class FakeSyncRewriteService:
            def __init__(self, **_kwargs):
                pass

            def maybe_materialize(self, **_kwargs):
                raise AssertionError("forced standard offset must not use sync rewrite")

        monkeypatch.setattr(
            "core.workflows.remux_runtime.SyncRewriteService",
            FakeSyncRewriteService,
        )
        captured: dict[str, object] = {}

        def build_command(_config, **kwargs):
            mapped_tracks = list(kwargs.get("mapped_tracks_override") or [])
            captured["mapped_tracks"] = mapped_tracks
            return ["ffmpeg", "-hide_banner", "-y", str(cfg.output)]

        runner = RemuxRuntimeRunner(
            RemuxRuntimeRunnerCallbacks(
                ffmpeg_bin="ffmpeg",
                ffprobe_bin="ffprobe",
                ffmpeg_thread_args=lambda: [],
                build_command=build_command,
                log_workflow_type=lambda _kind: None,
                log_step=lambda _idx, _name: None,
                log=lambda _level, _message: None,
                bind_temp_cleanup=lambda _signals, _paths: None,
                run_cmd=_fake_run_cmd_writing_candidate,
                apply_muxing_post_action=lambda _path: None,
                apply_language_post_action=lambda _path: None,
                write_nfo=lambda _path: None,
                sync_rewrite_enabled=lambda: True,
                sync_rewrite_audio_bitrates=lambda: {"eac3": 96},
            )
        )

        execution_plan = replace(
            plan_remux(cfg),
            output_contract=MatroskaOutputContract(
                track_types=("video",),
                expected_tracks=(ExpectedMatroskaTrack("video"),),
                duration_coherent=False,
            ),
        )
        state = _wait_task(runner.run(cfg, execution_plan))

        assert state["failed"] is None
        mapped_tracks = cast(list, captured["mapped_tracks"])
        mapped_audio = next(mt for mt in mapped_tracks if mt.track.track_type == "audio")
        assert mapped_audio.track.time_shift_ms == 250

    def test_requires_file_sync_fallback_for_offsets_detects_foreign_offset(self, tmp_path):
        src_a = tmp_path / "a.mkv"
        src_b = tmp_path / "b.mkv"
        src_a.touch()
        src_b.touch()

        cfg = RemuxConfig(
            sources=[
                SourceInput(path=src_a, file_index=0, tracks=[_track(0, "video"), _track(2, "subtitle")]),
                SourceInput(path=src_b, file_index=1, tracks=[_track(1, "audio", time_shift_ms=90)]),
            ],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (1, 1), (0, 2)],
            keep_chapters=False,
        )

        mapped = resolve_mapped_tracks(cfg)
        assert requires_file_sync_fallback_for_offsets(mapped) is True

    def test_requires_file_sync_fallback_for_offsets_ignores_zero_offsets(self, tmp_path):
        src_a = tmp_path / "a.mkv"
        src_b = tmp_path / "b.mkv"
        src_a.touch()
        src_b.touch()

        cfg = RemuxConfig(
            sources=[
                SourceInput(path=src_a, file_index=0, tracks=[_track(0, "video"), _track(2, "subtitle")]),
                SourceInput(path=src_b, file_index=1, tracks=[_track(1, "audio", time_shift_ms=0)]),
            ],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (1, 1), (0, 2)],
            keep_chapters=False,
        )

        mapped = resolve_mapped_tracks(cfg)
        assert requires_file_sync_fallback_for_offsets(mapped) is False

    def test_decide_strict_interleave_with_prescan_forces_sync_on_foreign_offset(self, tmp_path):
        src_a = tmp_path / "a.mkv"
        src_b = tmp_path / "b.mkv"
        src_a.touch()
        src_b.touch()

        cfg = RemuxConfig(
            sources=[
                SourceInput(path=src_a, file_index=0, tracks=[_track(0, "video")]),
                SourceInput(path=src_b, file_index=1, tracks=[_track(1, "audio", time_shift_ms=120)]),
            ],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (1, 1)],
            keep_chapters=False,
        )

        assert decide_strict_interleave_with_prescan(
            cfg,
            resolve_mapped_tracks=resolve_mapped_tracks,
            log_cb=lambda *_args: None,
        ) is True

    def test_build_command_does_not_force_muxing_application_metadata(self, tmp_path):
        wf = RemuxWorkflow(
            ffmpeg_bin="ffmpeg",
            ffprobe_bin="ffprobe",
            writing_application="Muxiveo Test/1.0",
        )
        src = tmp_path / "in.mkv"
        src.touch()
        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video")])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            keep_chapters=False,
        )
        cmd = wf.build_command(cfg)
        flat = " ".join(cmd)
        assert "muxing_application=" not in flat

    def test_build_command_includes_threads_argument(self, tmp_path):
        wf = RemuxWorkflow(
            ffmpeg_bin="ffmpeg",
            ffprobe_bin="ffprobe",
            ffmpeg_threads=12,
        )
        src = tmp_path / "in.mkv"
        src.touch()
        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video")])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            keep_chapters=False,
        )

        cmd = wf.build_command(cfg)
        assert "-threads" in cmd
        assert cmd[cmd.index("-threads") + 1] == "12"

    def test_build_command_emits_language_and_clears_legacy_ietf_tag(self, tmp_path):
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src = tmp_path / "in.mkv"
        src.touch()

        tracks = [_track(0, "video"), _track(1, "audio", language="fr", title="VF")]
        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=tracks)],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (0, 1)],
            keep_chapters=False,
            tag_overrides={"TMDB_ID": "42"},
        )

        cmd = wf.build_command(cfg)
        flat = " ".join(cmd)

        assert "-metadata:s:a:0 language=fr-FR" in flat
        assert "-metadata:s:a:0 language-ietf=" in flat
        assert "-map_chapters -1" in flat
        assert "-metadata TMDB_ID=42" in flat

    def test_build_command_with_chapter_overrides_maps_metadata_input(self, tmp_path):
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src = tmp_path / "in.mkv"
        src.touch()

        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video")])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            chapter_overrides=[ChapterEntry(timecode_s=0.0, name="Intro")],
        )

        cmd = wf.build_command(
            cfg,
            extra_inputs=[Path("<chapitres.ffmetadata>")],
            chapter_input_index=1,
        )

        assert "-i" in cmd
        assert "<chapitres.ffmetadata>" in cmd
        assert cmd[cmd.index("-map_chapters") + 1] == "1"

    def test_build_command_with_tag_and_chapter_overrides_maps_metadata_from_chapter_input(self, tmp_path):
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src = tmp_path / "in.mkv"
        src.touch()

        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video")])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            chapter_overrides=[ChapterEntry(timecode_s=0.0, name="Intro")],
            tag_overrides={"TMDB_ID": "42"},
        )

        cmd = wf.build_command(
            cfg,
            extra_inputs=[Path("<chapitres.ffmetadata>")],
            chapter_input_index=1,
        )

        assert cmd[cmd.index("-map_metadata") + 1] == "1"
        assert cmd[cmd.index("-map_chapters") + 1] == "1"

    def test_build_command_keeps_chapters_targets_first_source_with_chapters(self, tmp_path):
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src_mp4 = tmp_path / "video.mp4"
        src_mkv = tmp_path / "with_chapters.mkv"
        src_extra = tmp_path / "audio.mkv"
        src_mp4.touch()
        src_mkv.touch()
        src_extra.touch()

        cfg = RemuxConfig(
            sources=[
                SourceInput(path=src_mp4, file_index=0, tracks=[_track(0, "video")], has_chapters=False),
                SourceInput(path=src_mkv, file_index=1, tracks=[_track(1, "audio")], has_chapters=True),
                SourceInput(path=src_extra, file_index=2, tracks=[_track(2, "subtitle")], has_chapters=False),
            ],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (1, 1), (2, 2)],
            keep_chapters=True,
        )

        cmd = wf.build_command(cfg)
        assert cmd[cmd.index("-map_chapters") + 1] == "1"

    def test_build_command_uses_explicit_chapter_source_index(self, tmp_path):
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src_a = tmp_path / "a.mkv"
        src_b = tmp_path / "b.mkv"
        src_c = tmp_path / "c.mkv"
        src_a.touch()
        src_b.touch()
        src_c.touch()

        # 3 sources avec chapitres : on cible explicitement la 3e (index 2).
        cfg = RemuxConfig(
            sources=[
                SourceInput(path=src_a, file_index=0, tracks=[_track(0, "video")], has_chapters=True),
                SourceInput(path=src_b, file_index=1, tracks=[_track(1, "audio")], has_chapters=True),
                SourceInput(path=src_c, file_index=2, tracks=[_track(2, "subtitle")], has_chapters=True),
            ],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (1, 1), (2, 2)],
            keep_chapters=True,
            chapter_source_index=2,
        )

        cmd = wf.build_command(cfg)
        assert cmd[cmd.index("-map_chapters") + 1] == "2"

    def test_build_command_explicit_chapter_source_index_invalid_falls_back(self, tmp_path):
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src_a = tmp_path / "a.mkv"
        src_b = tmp_path / "b.mkv"
        src_a.touch()
        src_b.touch()

        # chapter_source_index=0 désigne une source sans chapitres → fallback sur la 1re porteuse.
        cfg = RemuxConfig(
            sources=[
                SourceInput(path=src_a, file_index=0, tracks=[_track(0, "video")], has_chapters=False),
                SourceInput(path=src_b, file_index=1, tracks=[_track(1, "audio")], has_chapters=True),
            ],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (1, 1)],
            keep_chapters=True,
            chapter_source_index=0,
        )

        cmd = wf.build_command(cfg)
        assert cmd[cmd.index("-map_chapters") + 1] == "1"

    def test_build_command_keeps_chapters_falls_back_to_input_zero(self, tmp_path):
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src = tmp_path / "in.mkv"
        src.touch()

        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video")])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            keep_chapters=True,
        )

        cmd = wf.build_command(cfg)
        assert cmd[cmd.index("-map_chapters") + 1] == "0"

    def test_build_command_forces_matroska_demuxer_for_sync_inputs(self, tmp_path):
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src = tmp_path / "in.mkv"
        sync = tmp_path / "sync_audio.mka"
        chapters = tmp_path / "chapters.ffmetadata"
        src.touch()
        sync.touch()
        chapters.touch()

        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video")])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            chapter_overrides=[ChapterEntry(timecode_s=0.0, name="Intro")],
        )

        cmd = wf.build_command(
            cfg,
            sync_inputs=[sync],
            extra_inputs=[chapters],
            chapter_input_index=2,
        )

        sync_idx = cmd.index(sync.as_posix())
        assert cmd[sync_idx - 1] == "-i"
        assert cmd[sync_idx - 2] == "matroska"
        assert cmd[sync_idx - 3] == "-f"
        assert cmd[cmd.index("-map_chapters") + 1] == "2"

    def test_build_command_maps_source_metadata_when_copy_tags_is_enabled(self, tmp_path):
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src = tmp_path / "in.mkv"
        src.touch()

        cfg = RemuxConfig(
            sources=[SourceInput(
                path=src,
                file_index=0,
                tracks=[_track(0, "video")],
                copy_tags=True,
            )],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            keep_chapters=False,
            tag_overrides=None,
        )
        cmd = wf.build_command(cfg)
        assert cmd[cmd.index("-map_metadata") + 1] == "0"

    def test_build_command_drops_source_metadata_when_copy_tags_is_disabled(self, tmp_path):
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src = tmp_path / "in.mkv"
        src.touch()

        cfg = RemuxConfig(
            sources=[SourceInput(
                path=src,
                file_index=0,
                tracks=[_track(0, "video")],
                copy_tags=False,
            )],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            keep_chapters=False,
            tag_overrides=None,
        )
        cmd = wf.build_command(cfg)
        assert cmd[cmd.index("-map_metadata") + 1] == "-1"

    def test_build_command_multi_source_with_subtitles_enables_strict_interleave(self, tmp_path):
        """Le mux multi-source avec sous-titres a besoin d'un entrelacement strict
        pour éviter qu'une piste audio importée soit écrite très loin de la vidéo."""
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src_a = tmp_path / "a.mkv"
        src_b = tmp_path / "b.mkv"
        src_a.touch()
        src_b.touch()

        cfg = RemuxConfig(
            sources=[
                SourceInput(path=src_a, file_index=0, tracks=[_track(0, "video"), _track(2, "subtitle")]),
                SourceInput(path=src_b, file_index=1, tracks=[_track(1, "audio")]),
            ],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (1, 1), (0, 2)],
            keep_chapters=False,
        )

        cmd = wf.build_command(cfg)
        assert cmd[cmd.index("-max_interleave_delta") + 1] == "0"
        assert cmd[cmd.index("-max_muxing_queue_size") + 1] == "9999"
        assert "-fflags" not in cmd
        assert "-copytb" not in cmd
        assert "-avoid_negative_ts" not in cmd

    def test_build_command_strict_interleave_override_false_disables_flags(self, tmp_path):
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src_a = tmp_path / "a.mkv"
        src_b = tmp_path / "b.mkv"
        src_a.touch()
        src_b.touch()

        cfg = RemuxConfig(
            sources=[
                SourceInput(path=src_a, file_index=0, tracks=[_track(0, "video"), _track(2, "subtitle")]),
                SourceInput(path=src_b, file_index=1, tracks=[_track(1, "audio")]),
            ],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (1, 1), (0, 2)],
            keep_chapters=False,
        )

        cmd = wf.build_command(cfg, strict_interleave_override=False)
        assert "-max_muxing_queue_size" not in cmd
        assert "-max_interleave_delta" not in cmd

    def test_build_command_strict_interleave_override_true_enables_flags(self, tmp_path):
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src = tmp_path / "in.mkv"
        src.touch()

        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video")])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            keep_chapters=False,
        )

        cmd = wf.build_command(cfg, strict_interleave_override=True)
        assert cmd[cmd.index("-max_interleave_delta") + 1] == "0"
        assert cmd[cmd.index("-max_muxing_queue_size") + 1] == "9999"

    def test_build_command_multi_source_without_subtitles_keeps_default_interleave(self, tmp_path):
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src_a = tmp_path / "a.mkv"
        src_b = tmp_path / "b.mkv"
        src_a.touch()
        src_b.touch()

        cfg = RemuxConfig(
            sources=[
                SourceInput(path=src_a, file_index=0, tracks=[_track(0, "video")]),
                SourceInput(path=src_b, file_index=1, tracks=[_track(1, "audio")]),
            ],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (1, 1)],
            keep_chapters=False,
        )

        cmd = wf.build_command(cfg)
        assert "-max_muxing_queue_size" not in cmd
        assert "-max_interleave_delta" not in cmd

    def test_build_command_multi_source_with_foreign_subtitle_enables_strict_interleave(self, tmp_path):
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src_a = tmp_path / "a.mkv"
        src_b = tmp_path / "b.mkv"
        src_a.touch()
        src_b.touch()

        cfg = RemuxConfig(
            sources=[
                SourceInput(path=src_a, file_index=0, tracks=[_track(0, "video"), _track(1, "audio")]),
                SourceInput(path=src_b, file_index=1, tracks=[_track(2, "subtitle")]),
            ],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (0, 1), (1, 2)],
            keep_chapters=False,
        )

        cmd = wf.build_command(cfg)
        assert cmd[cmd.index("-max_interleave_delta") + 1] == "0"
        assert cmd[cmd.index("-max_muxing_queue_size") + 1] == "9999"

    def test_prepare_sync_inputs_windows_prefers_mmap_fallback(self, tmp_path, monkeypatch):
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src_a = tmp_path / "a.mkv"
        src_b = tmp_path / "b.mkv"
        src_a.touch()
        src_b.touch()

        cfg = RemuxConfig(
            sources=[
                SourceInput(path=src_a, file_index=0, tracks=[_track(0, "video"), _track(2, "subtitle")]),
                SourceInput(path=src_b, file_index=1, tracks=[_track(1, "audio")]),
            ],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (1, 1), (0, 2)],
            keep_chapters=False,
        )
        mapped = resolve_mapped_tracks(cfg)

        class _FakeSyncer:
            def __init__(self, **_kwargs):
                pass

            def start_live_demux_session(self, **_kwargs):
                raise LiveSyncNotSupportedError("no live")

            def prepare_from_mapped_tracks_mmap(self, **_kwargs):
                return [SyncPreparedInput(key=(1, 1, "audio"), path=tmp_path / "mmap.mka", input_idx=2)]

            def prepare_from_mapped_tracks(self, **_kwargs):
                pytest.fail("temp fallback should not be used when mmap works")

        remapped, extra_inputs, live = prepare_timeline_sync_inputs(
            cfg,
            mapped,
            tmp_path,
            TaskSignals(),
            allow_live=True,
            ffmpeg_bin="ffmpeg",
            ffmpeg_thread_args=wf._ffmpeg_thread_args(),
            log_cb=lambda *_args: None,
            syncer_factory=_FakeSyncer,
            fallback_helper_factory=TimelineSyncFallbackHelper,
        )
        assert live is None
        assert [item.path for item in extra_inputs] == [tmp_path / "mmap.mka"]
        audio = next(mt for mt in remapped if mt.track.track_type == "audio")
        assert audio.source_input_idx == 2
        assert audio.stream_index == 0

    def test_prepare_sync_inputs_windows_falls_back_to_temp_when_mmap_fails(self, tmp_path, monkeypatch):
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src_a = tmp_path / "a.mkv"
        src_b = tmp_path / "b.mkv"
        src_a.touch()
        src_b.touch()

        cfg = RemuxConfig(
            sources=[
                SourceInput(path=src_a, file_index=0, tracks=[_track(0, "video"), _track(2, "subtitle")]),
                SourceInput(path=src_b, file_index=1, tracks=[_track(1, "audio")]),
            ],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (1, 1), (0, 2)],
            keep_chapters=False,
        )
        mapped = resolve_mapped_tracks(cfg)

        class _FakeSyncer:
            def __init__(self, **_kwargs):
                pass

            def start_live_demux_session(self, **_kwargs):
                raise LiveSyncNotSupportedError("no live")

            def prepare_from_mapped_tracks_mmap(self, **_kwargs):
                raise RemuxError("mmap failed")

            def prepare_from_mapped_tracks(self, **_kwargs):
                return [SyncPreparedInput(key=(1, 1, "audio"), path=tmp_path / "temp.mka", input_idx=2)]

        remapped, extra_inputs, live = prepare_timeline_sync_inputs(
            cfg,
            mapped,
            tmp_path,
            TaskSignals(),
            allow_live=True,
            ffmpeg_bin="ffmpeg",
            ffmpeg_thread_args=wf._ffmpeg_thread_args(),
            log_cb=lambda *_args: None,
            syncer_factory=_FakeSyncer,
            fallback_helper_factory=TimelineSyncFallbackHelper,
        )
        assert live is None
        assert [item.path for item in extra_inputs] == [tmp_path / "temp.mka"]
        audio = next(mt for mt in remapped if mt.track.track_type == "audio")
        assert audio.source_input_idx == 2
        assert audio.stream_index == 0

    def test_validate_rejects_invalid_selected_attachments(self, tmp_path):
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src = tmp_path / "in.mkv"
        src.touch()

        bad = AttachmentInfo(
            index=-1,
            local_index=-2,
            filename="font.ttf",
            mimetype="application/x-truetype-font",
            is_attached_pic=False,
        )
        cfg = RemuxConfig(
            sources=[SourceInput(
                path=src,
                file_index=0,
                tracks=[_track(0, "video")],
                selected_attachments=[bad, bad],
            )],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
        )
        errors = wf.validate(cfg)
        assert any("index négatif" in e for e in errors)
        assert any("local_index négatif" in e for e in errors)
        assert any("dupliquée" in e for e in errors)


class TestMatroskaSegmentInfoPatch:

    def test_patch_rebuilds_info_and_updates_only_muxing_app(self, tmp_path):
        old_mux = b"Lavf"
        old_writing = b"Lavf"
        children = (
            b"\x4d\x80" + bytes([0x80 | len(old_mux)]) + old_mux +
            b"\x57\x41" + bytes([0x80 | len(old_writing)]) + old_writing
        )
        info = b"\x15\x49\xa9\x66" + bytes([0x80 | len(children)]) + children
        segment_payload = info + b"\xec\x81\x00"
        segment = b"\x18\x53\x80\x67" + bytes([0x80 | len(segment_payload)]) + segment_payload
        path = tmp_path / "mini.mkv"
        path.write_bytes(b"\x00" * 16 + segment + b"\x00" * 16)

        editor = MatroskaSegmentInfoHeaderEditor()
        result = editor.apply_muxing_app_replace_with_header_rebuild(
            path,
            app_prefix=f"Muxiveo {APP_VERSION_LABEL.removeprefix('v')}",
        )

        assert result.applied is True
        assert not list(tmp_path.glob("*.hdrpatch.*"))
        apps = _segment_info_apps(path)
        assert apps["muxing_app"] == f"Muxiveo {APP_VERSION_LABEL.removeprefix('v')}"
        assert apps["writing_app"] == "Lavf"

    def test_patch_works_with_known_size_segment_lavf61_style(self, tmp_path):
        # Régression : Lavf 61+ écrit une taille Segment connue (non indéterminée).
        # Le payload du Segment couvre tout le reste du fichier, donc bien au-delà
        # de la fenêtre de scan de 8 Mo. _locate_segment ne doit pas lever
        # "Segment Matroska introuvable" dans ce cas.
        old_mux = b"Lavf61.7.100"
        old_writing = b"Lavf61.7.100"
        children = (
            b"\x4d\x80" + bytes([0x80 | len(old_mux)]) + old_mux +
            b"\x57\x41" + bytes([0x80 | len(old_writing)]) + old_writing
        )
        info = b"\x15\x49\xa9\x66" + bytes([0x80 | len(children)]) + children
        dummy_cluster = b"\xec\x81\x00"
        segment_payload = info + dummy_cluster
        # Encode la taille Segment comme valeur connue (pas de bits tous à 1 = unknown).
        segment_size = len(segment_payload)
        # Encode en VINT 8 octets (suffisamment grand pour un vrai fichier).
        segment_size_vint = (0x01 << 56 | segment_size).to_bytes(8, "big")
        segment = b"\x18\x53\x80\x67" + segment_size_vint + segment_payload
        path = tmp_path / "lavf61.mkv"
        path.write_bytes(b"\x00" * 16 + segment + b"\x00" * 16)

        editor = MatroskaSegmentInfoHeaderEditor()
        result = editor.apply_muxing_app_replace_with_header_rebuild(
            path,
            app_prefix=f"Muxiveo {APP_VERSION_LABEL.removeprefix('v')}",
        )

        assert result.applied is True
        apps = _segment_info_apps(path)
        assert apps["muxing_app"] == f"Muxiveo {APP_VERSION_LABEL.removeprefix('v')}"
        assert apps["writing_app"] == "Lavf61.7.100"

    def test_patch_failure_is_skipped_without_file_mutation(self, tmp_path):
        path = tmp_path / "invalid.mkv"
        path.write_bytes(b"not a matroska header")
        before = path.read_bytes()

        editor = MatroskaSegmentInfoHeaderEditor()
        result = editor.apply_muxing_app_replace_with_header_rebuild(
            path,
            app_prefix=f"Muxiveo {APP_VERSION_LABEL.removeprefix('v')}",
        )

        assert result.applied is False
        assert result.skipped is True
        assert path.read_bytes() == before


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe requis pour les tests d'intégration",
)
class TestRemuxWorkflowIntegration:

    def _make_av_source(self, path: Path) -> None:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25",
                "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
                "-t", "1",
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-shortest",
                str(path),
            ],
            check=True,
        )

    def test_run_applies_language_chapters_tags_and_cover(self, tmp_path):
        src = tmp_path / "src.mkv"
        out = tmp_path / "out.mkv"
        cover = tmp_path / "cover.jpg"

        self._make_av_source(src)
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=blue:s=48x48:d=0.1",
                "-frames:v", "1",
                str(cover),
            ],
            check=True,
        )

        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        tracks = [_track(0, "video"), _track(1, "audio", language="fr", title="Français")]
        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=tracks)],
            output=out,
            track_order=[(0, 0), (0, 1)],
            chapter_overrides=[
                ChapterEntry(timecode_s=0.0, name="Intro"),
                ChapterEntry(timecode_s=0.5, name="Main"),
            ],
            extra_attachments=[cover],
            tag_overrides={"TMDB_ID": "777", "SOURCE": "pytest"},
            keep_chapters=True,
        )

        state = _wait_task(wf.run(cfg), timeout=30.0)
        assert state["failed"] is None, f"Remux failed: {state['failed']}"
        assert state["cancelled"] is False
        assert out.exists()

        probe = _ffprobe_json(out)

        assert _tag_value(probe.get("format", {}).get("tags", {}), "TMDB_ID") == "777"
        assert _tag_value(probe.get("format", {}).get("tags", {}), "SOURCE") == "pytest"
        assert len(probe.get("chapters", [])) == 2
        assert [_tag_value(ch.get("tags", {}), "title") for ch in probe.get("chapters", [])] == ["Intro", "Main"]

        audio_stream = next(s for s in probe.get("streams", []) if s.get("codec_type") == "audio")
        # Post-action langues : Language normalisé en ISO 639-2/B, BCP-47 préservé
        # dans LanguageBCP47 (non remonté par ffprobe, vérifié via EBML ailleurs).
        assert _tag_value(audio_stream.get("tags", {}), "language") == "fre"

        attached_cover = next(
            s for s in probe.get("streams", [])
            if s.get("codec_type") in {"attachment", "video"}
            and int((s.get("disposition") or {}).get("attached_pic", 0)) == 1
        )
        assert _tag_value(attached_cover.get("tags", {}), "mimetype") == "image/jpeg"

    def test_run_rehydrates_selected_source_attachments(self, tmp_path):
        base = tmp_path / "base.mkv"
        src = tmp_path / "src_with_att.mkv"
        out = tmp_path / "out_att.mkv"
        font = tmp_path / "test_font.ttf"
        cover = tmp_path / "cover.jpg"

        self._make_av_source(base)
        font.write_text("dummy-font", encoding="utf-8")
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=red:s=32x32:d=0.1",
                "-frames:v", "1",
                str(cover),
            ],
            check=True,
        )

        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner", "-loglevel", "error",
                "-i", str(base),
                "-map", "0",
                "-c", "copy",
                "-attach", str(font),
                "-metadata:s:t:0", "mimetype=application/x-truetype-font",
                "-metadata:s:t:0", "filename=test_font.ttf",
                "-attach", str(cover),
                "-metadata:s:t:1", "mimetype=image/jpeg",
                "-metadata:s:t:1", "filename=cover.jpg",
                str(src),
            ],
            check=True,
        )

        probe_src = _ffprobe_json(src)
        att_streams = [
            s for s in probe_src.get("streams", [])
            if s.get("codec_type") == "attachment"
            or int((s.get("disposition") or {}).get("attached_pic", 0)) == 1
        ]
        selected: list[AttachmentInfo] = []
        local_index = 0
        for s in att_streams:
            tags = s.get("tags") or {}
            selected.append(AttachmentInfo(
                index=int(s.get("index", -1)),
                local_index=local_index,
                filename=_tag_value(tags, "filename") or f"att_{local_index}",
                mimetype=_tag_value(tags, "mimetype") or "application/octet-stream",
                is_attached_pic=bool(int((s.get("disposition") or {}).get("attached_pic", 0))),
            ))
            local_index += 1

        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video")], selected_attachments=selected)],
            output=out,
            track_order=[(0, 0)],
            keep_chapters=False,
            tag_overrides={},
        )

        state = _wait_task(wf.run(cfg), timeout=30.0)
        assert state["failed"] is None, f"Remux failed: {state['failed']}"
        assert out.exists()

        probe_out = _ffprobe_json(out)
        out_attachments = [
            s for s in probe_out.get("streams", [])
            if s.get("codec_type") == "attachment"
            or int((s.get("disposition") or {}).get("attached_pic", 0)) == 1
        ]
        assert len(out_attachments) >= 2

        mimetypes = {
            _tag_value(s.get("tags", {}), "mimetype")
            for s in out_attachments
        }
        assert "application/x-truetype-font" in mimetypes
        assert "image/jpeg" in mimetypes

    def test_run_can_copy_source_global_tags_when_requested(self, tmp_path):
        src_plain = tmp_path / "src_plain.mkv"
        src_tagged = tmp_path / "src_tagged.mkv"
        out = tmp_path / "out_tag_copy.mkv"

        self._make_av_source(src_plain)
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner", "-loglevel", "error",
                "-i", str(src_plain),
                "-map", "0",
                "-c", "copy",
                "-metadata", "SOURCE=from_source",
                str(src_tagged),
            ],
            check=True,
        )

        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        cfg = RemuxConfig(
            sources=[SourceInput(
                path=src_tagged,
                file_index=0,
                tracks=[_track(0, "video"), _track(1, "audio", language="eng")],
                copy_tags=True,
            )],
            output=out,
            track_order=[(0, 0), (0, 1)],
            keep_chapters=False,
            tag_overrides=None,
        )
        state = _wait_task(wf.run(cfg), timeout=30.0)
        assert state["failed"] is None, f"Remux failed: {state['failed']}"
        assert out.exists()

        probe = _ffprobe_json(out)
        assert _tag_value(probe.get("format", {}).get("tags", {}), "SOURCE") == "from_source"

    def test_run_applies_custom_muxing_application_without_mkvpropedit(self, tmp_path):
        src = tmp_path / "src.mkv"
        out = tmp_path / "out_muxing_app.mkv"
        self._make_av_source(src)

        wf = RemuxWorkflow(
            ffmpeg_bin="ffmpeg",
            ffprobe_bin="ffprobe",
            writing_application="Muxiveo v1.2.1 - test",
        )
        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video"), _track(1, "audio", language="eng")])],
            output=out,
            track_order=[(0, 0), (0, 1)],
            keep_chapters=False,
            tag_overrides=None,
        )
        state = _wait_task(wf.run(cfg), timeout=30.0)
        assert state["failed"] is None, f"Remux failed: {state['failed']}"
        probe = _ffprobe_json(out)
        assert _tag_value(probe.get("format", {}).get("tags", {}), "MUXING_APPLICATION") is None

        apps = _segment_info_apps(out)
        muxing_app = apps.get("muxing_app", "")
        assert muxing_app == f"Muxiveo {APP_VERSION_LABEL.removeprefix('v')}"
        src_apps = _segment_info_apps(src)
        assert apps.get("writing_app") == src_apps.get("writing_app")


class TestAbsolutePathNormalization:
    """P1-b : le workflow résout les chemins relatifs de TOUT producteur
    (UI, profils, appels directs) avant plan/validation/exécution — les
    runners exécutent FFmpeg depuis le workspace temporaire tandis que les
    post-actions résolvent le candidat depuis le cwd du processus."""

    def test_absolute_paths_config_resolves_everything(self, tmp_path, monkeypatch) -> None:
        from core.workflows.remux import RemuxWorkflow
        from core.workflows.remux_models import RemuxConfig, SourceInput

        monkeypatch.chdir(tmp_path)
        (tmp_path / "in.mkv").write_bytes(b"x")
        config = RemuxConfig(
            sources=[SourceInput(Path("in.mkv"), 0, [])],
            output=Path("sub/out.mkv"),
            track_order=[],
            work_dir=Path("wip"),
            extra_attachments=[Path("cover.jpg")],
        )
        normalized = RemuxWorkflow._absolute_paths_config(config)
        assert normalized.sources[0].path == tmp_path / "in.mkv"
        assert normalized.output == tmp_path / "sub" / "out.mkv"
        assert normalized.work_dir == tmp_path / "wip"
        assert normalized.extra_attachments == [tmp_path / "cover.jpg"]
        assert all(path.is_absolute() for path in (
            normalized.sources[0].path, normalized.output, normalized.work_dir,
            *normalized.extra_attachments,
        ))
