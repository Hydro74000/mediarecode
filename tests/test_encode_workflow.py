"""
tests/test_encode_workflow.py — Tests unitaires pour core/workflows/encode.py

Plan de couverture :

    Helpers RAM — cross-platform :
        _total_ram_bytes :
            - Linux  : lecture MemTotal /proc/meminfo
            - macOS  : parse sysctl hw.memsize
            - Windows: MEMORYSTATUSEX.ullTotalPhys via ctypes
            - Retourne 0 sur erreur

        _available_ram_bytes :
            - Linux  : lecture MemAvailable /proc/meminfo
            - macOS  : parse vm_stat (free+inactive+speculative+purgeable)
            - Windows: MEMORYSTATUSEX.ullAvailPhys via ctypes
            - Retourne 0 sur erreur

        _macos_available_ram :
            - Parse correctement vm_stat avec différentes tailles de page
            - Retourne 0 si vm_stat échoue

        _ram_buffer_dir :
            - Linux/macOS : /dev/shm si writable
            - Linux/macOS : None si /dev/shm absent ou non writable
            - Windows     : None systématiquement

    _shm_path (méthode d'instance) — formule de seuil :
        Formule : available - file_size ≥ total × threshold_pct / 100
        - Retourne RAM si formule vérifiée
        - Retourne disque si RAM insuffisante
        - Retourne disque si ram_buffer_enabled=False (config)
        - Retourne disque si _ram_buffer_dir() = None
        - Retourne disque si total ou available = 0
        - Frontière exacte du seuil

    Gestion des fichiers dans _run_with_metadata_inject :
        Tracking ext_files :
            - Un fichier RAM est enregistré dans ext_files
            - Un fichier disque N'est PAS dans ext_files
            - _free() retire le fichier de ext_files
            - _free() silencieux si fichier déjà absent

        Pas de src.hevc :
            - aucune extraction ffmpeg vers src.hevc n'est créée

        Cleanup des intermédiaires HEVC :
            - enc.hevc supprimé dans les snapshots suivant la création de enc_hdr10p.hevc
            - enc_hdr10p.hevc supprimé dans les snapshots suivant enc_dv.hevc

        Comptage fichiers simultanés :
            - Jamais > 2 fichiers .hevc/.mkv simultanément (DV / HDR10+ / les deux)

        Cleanup ext_files sur annulation / exception :
            - Fichiers ext_files supprimés si RuntimeError
            - Fichiers ext_files supprimés si TaskCancelledError
            - Itération sur copie de ext_files (pas de mutation pendant le finally)

        Désactivation du buffer RAM :
            - ram_buffer_enabled=False → tous les HEVC vont sur disque (ext_files vide)

    Codec COPY — passthrough métadonnées (TestCopyCodecMetadataPassthrough) :
        build_command single pass :
            - codec=copy → -map_metadata 0 présent
            - codec=copy → -map_metadata:s:v:0 0:s:v:0 présent
            - codec=copy → -map_metadata avant la sortie
            - codec≠copy (libx265, libx264, nvenc, amf…) → pas de -map_metadata
        build_command two pass (SIZE) :
            - codec=copy → forcé en single-pass (pas de -pass)
            - codec≠copy → aucune passe ne contient -map_metadata
        Interaction : audio, subtitles → -map_metadata toujours présent pour copy

    run() bypass inject (TestRunCopyBypassesInject) :
        - codec=copy + copy_dv=True + profil source → _run_with_metadata_inject NON appelé
        - codec=copy + copy_dv=True + dovi_profile=2 → _run_with_metadata_inject APPELÉ
        - codec=copy + copy_hdr10plus=True → idem
        - codec=copy + les deux → idem
        - codec=libx265 + copy_dv=True → _run_with_metadata_inject APPELÉ (régression)
        - codec=copy + copy_dv → log INFO "passthrough" émis
        - codec=copy sans flags → runner standard utilisé

    Méthodes build_command (régression) :
        - CRF → list[str] ; SIZE → list[list[str]]
        - codec copy → pas de -crf ; audio copy → pas de -b:a ; AAC → bitrate présent
        - TrueHD core → BSF truehd_core

    validate :
        - Source manquante, source==output, SIZE sans durée, master_display invalide, max_cll invalide

    ProfileManager :
        - save/load round-trip, delete, names(), overwrite

Exécution :
    cd Muxiveo && pytest tests/test_encode_workflow.py -v
"""

from __future__ import annotations

import json
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call
import tempfile
import os
from typing import Any, cast

import pytest
from PySide6.QtCore import QCoreApplication, Qt
import core.workflows.encode.workflow as encode_workflow_mod
from core.workflows.encode.runtime import ram_buffer as _ram_buffer_mod
from core.workflows.encode.domain.codecs import hdr_meta_args as _hdr_meta_args

_app: QCoreApplication | None = None


def _get_app() -> QCoreApplication:
    global _app
    if _app is None:
        _app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    return _app


@pytest.fixture(autouse=True)
def qt_app():
    return _get_app()


from core.workflows.encode import (
    AUDIO_CODECS,
    AudioTrackSettings,
    EncodeConfig,
    EncodeError,
    EncodePreviewRequest,
    EncodePreset,
    EncodeWorkflow,
    ProfileManager,
    QualityMode,
    TrackMetaEdit,
    TrackTimeOffset,
    VideoCropSettings,
    VideoEncodeSettings,
    VideoFilterSettings,
    VideoResizeSettings,
)
from core.inspector import ChapterEntry
from core.runner import TaskSignals
from core.workflows.common.sync_rewrite import SyncRewritePreparedInput
from core.workflows.encode.runtime.hdr_metadata import HdrMetadataProbeService
from core.workflows.encode.runtime.dovi_p7_router import P7RoutingDecision
from core.workflows.encode.runtime.metadata_inject import (
    _friendly_dovi_conversion_error,
)
from core.workflows.encode.runtime.static_hdr_estimator import StaticHdrEstimate
from core.dovi_profile_detector import DoviSubProfile


# ---------------------------------------------------------------------------
# Fixtures communes
# ---------------------------------------------------------------------------

def _make_video_settings(**kw) -> VideoEncodeSettings:
    defaults = dict(
        codec="libx265", quality_mode=QualityMode.CRF, crf=18,
        bitrate_kbps=5000, target_size_mb=4000, preset="slow",
        extra_params="", inject_hdr_meta=False, master_display="",
        max_cll="", tonemap_to_sdr=False, tonemap_algorithm="hable",
    )
    defaults.update(kw)
    return VideoEncodeSettings(**cast(Any, defaults))


def _make_config(source: Path, output: Path, **kw) -> EncodeConfig:
    defaults = dict(
        source=source, output=output, video=_make_video_settings(),
        audio_tracks=[], copy_subtitles=False, duration_s=3600.0,
        copy_dv=False, copy_hdr10plus=False, dovi_profile="0", work_dir=None,
    )
    defaults.update(kw)
    return EncodeConfig(**cast(Any, defaults))


def _as_single_command(cmd: list[str] | list[list[str]]) -> list[str]:
    assert cmd and isinstance(cmd[0], str)
    return cast(list[str], cmd)


def _make_workflow(
    enabled=True,
    threshold=15,
    ffmpeg_threads=None,
    max_parallel_video_encodes=1,
) -> EncodeWorkflow:
    return EncodeWorkflow(
        ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool",
        hdr10plus_bin="hdr10plus_tool",
        ram_buffer_enabled=enabled,
        ram_buffer_threshold_pct=threshold,
        ffmpeg_threads=ffmpeg_threads,
        max_parallel_video_encodes=max_parallel_video_encodes,
    )


class TestEncodePreviewWorkflow:

    def test_preview_source_segment_command_uses_scene_and_duration(self, tmp_path):
        wf = _make_workflow()
        src = tmp_path / "source.mkv"
        out = tmp_path / "preview.mkv"

        cmd = wf._build_preview_source_segment_cmd(
            src,
            out,
            stream_index=2,
            start_s=12.3456,
            duration_s=8.0,
        )

        assert cmd[:4] == ["ffmpeg", "-hide_banner", "-y", "-ss"]
        assert cmd[cmd.index("-ss") + 1] == "12.346"
        assert cmd[cmd.index("-t") + 1] == "8.000"
        assert cmd[cmd.index("-map") + 1] == "0:2"
        assert cmd[-1] == str(out)

    def test_preview_encode_config_keeps_video_only_and_converts_size_mode(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.touch()
        segment = tmp_path / "segment.mkv"
        segment.touch()
        output = tmp_path / "preview.mkv"
        video = _make_video_settings(quality_mode=QualityMode.SIZE, target_size_mb=1000)
        cfg = _make_config(
            src,
            tmp_path / "out.mkv",
            video=video,
            audio_tracks=[AudioTrackSettings(stream_index=1, codec="aac", bitrate_kbps=384)],
            copy_subtitles=True,
            duration_s=1000.0,
        )
        wf = _make_workflow()

        preview_cfg = wf._preview_encode_config(
            cfg,
            source_segment=segment,
            output=output,
            clip_duration_s=10.0,
        )

        assert preview_cfg.source == segment
        assert preview_cfg.output == output
        assert preview_cfg.audio_tracks == []
        assert preview_cfg.copy_subtitles is False
        assert preview_cfg.keep_chapters is False
        assert preview_cfg.video is not None
        assert preview_cfg.video.source_path == segment
        assert preview_cfg.video.stream_index == 0
        assert preview_cfg.video.quality_mode == QualityMode.BITRATE
        assert preview_cfg.video.bitrate_kbps == wf._size_to_bitrate_kbps(cfg)

    def test_cleanup_preview_dir_removes_all_entries(self, tmp_path):
        from core.workflows.encode import EncodeWorkflow

        preview_dir = tmp_path / "previews"
        preview_dir.mkdir()
        (preview_dir / "a.png").write_bytes(b"a")
        (preview_dir / "b.mkv").write_bytes(b"b")
        sub = preview_dir / "sub"
        sub.mkdir()
        (sub / "c.png").write_bytes(b"c")

        deleted = EncodeWorkflow.cleanup_preview_dir(tmp_path)

        assert deleted == 3
        assert not preview_dir.exists()

    def test_cleanup_preview_dir_noop_when_missing(self, tmp_path):
        from core.workflows.encode import EncodeWorkflow

        assert EncodeWorkflow.cleanup_preview_dir(tmp_path) == 0
        assert EncodeWorkflow.cleanup_preview_dir(None) == 0

    def test_preview_request_duration_bounds(self):
        assert EncodePreviewRequest(mode="video", duration_s=3).normalized_duration_s() == 5.0
        assert EncodePreviewRequest(mode="video", duration_s=99).normalized_duration_s() == 30.0
        assert EncodePreviewRequest(mode="image", duration_s=99).normalized_duration_s() == 5.0

    def test_run_preview_sync_image_mode_generates_seven_captures(self, tmp_path, monkeypatch):
        from core.workflows.encode import PREVIEW_IMAGE_CAPTURE_COUNT
        from core.workflows.encode.runtime.hdr_metadata import DynamicHdrPreviewSceneSelection

        src = tmp_path / "source.mkv"
        src.touch()
        wf = _make_workflow()
        cfg = _make_config(
            src,
            tmp_path / "out.mkv",
            work_dir=tmp_path,
            duration_s=600.0,
        )
        created_images: list[Path] = []

        def fake_run_cmd(cmd, *, cwd=None, env=None, label="", progress_cb=None, progress_pct_cb=None, signals=None):
            _ = (cwd, env, progress_cb, progress_pct_cb, signals)
            output = Path(cmd[-1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"\x89PNG\r\n\x1a\n")
            if label.startswith("ffmpeg-preview-image-"):
                created_images.append(output)
            return ""

        fake_scenes = tuple(
            DynamicHdrPreviewSceneSelection(
                requested_time_s=float(i) * 60.0,
                scene_time_s=float(i) * 60.0 + 1.0,
                reason="I-frame DoVi metadata",
            )
            for i in range(PREVIEW_IMAGE_CAPTURE_COUNT)
        )

        monkeypatch.setattr(wf._runner, "_run_cmd", fake_run_cmd)
        monkeypatch.setattr(
            wf._hdr_metadata_service,
            "select_preview_scenes_random",
            lambda *args, **kwargs: fake_scenes,
        )
        monkeypatch.setattr(
            wf._hdr_metadata_service,
            "source_hdr_transfer",
            lambda *args, **kwargs: "pq",
        )

        result = wf._run_preview_sync(
            cfg,
            EncodePreviewRequest(mode="image"),
            TaskSignals(),
        )

        assert result.mode == "image"
        assert result.video_path is None
        assert len(result.captures) == PREVIEW_IMAGE_CAPTURE_COUNT
        assert len(created_images) == PREVIEW_IMAGE_CAPTURE_COUNT
        assert all(capture.image_path.exists() for capture in result.captures)

    def test_run_preview_sync_video_mode_generates_thumbnails(self, tmp_path, monkeypatch):
        from core.workflows.encode import PREVIEW_VIDEO_THUMBNAIL_COUNT
        from core.workflows.encode.runtime.hdr_metadata import DynamicHdrPreviewSceneSelection

        src = tmp_path / "source.mkv"
        src.touch()
        wf = _make_workflow()
        cfg = _make_config(
            src,
            tmp_path / "out.mkv",
            work_dir=tmp_path,
            duration_s=600.0,
        )
        created_thumbs: list[Path] = []
        created_segments: list[Path] = []

        def fake_run_cmd(cmd, *, cwd=None, env=None, label="", progress_cb=None, progress_pct_cb=None, signals=None):
            _ = (cwd, env, progress_cb, progress_pct_cb, signals)
            output = Path(cmd[-1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"data")
            if label == "ffmpeg-preview-source":
                created_segments.append(output)
            elif label.startswith("ffmpeg-preview-thumb-"):
                created_thumbs.append(output)
            return ""

        def fake_run_with_preparation(config, *, validate, prep_signals=None):
            _ = validate
            config.output.write_bytes(b"encoded")
            if prep_signals is not None:
                prep_signals.finished.emit("ok")
                return prep_signals
            signals = TaskSignals()
            signals.finished.emit("ok")
            return signals

        monkeypatch.setattr(wf._runner, "_run_cmd", fake_run_cmd)
        monkeypatch.setattr(wf, "_run_with_preparation", fake_run_with_preparation)
        monkeypatch.setattr(
            wf._hdr_metadata_service,
            "select_preview_scene",
            lambda *args, **kwargs: DynamicHdrPreviewSceneSelection(
                requested_time_s=0.0,
                scene_time_s=42.0,
                reason="I-frame DoVi metadata",
            ),
        )
        monkeypatch.setattr(
            wf._hdr_metadata_service,
            "source_hdr_transfer",
            lambda *args, **kwargs: "",
        )

        result = wf._run_preview_sync(
            cfg,
            EncodePreviewRequest(mode="video", timecode_s=0.0, duration_s=10.0),
            TaskSignals(),
        )

        assert result.mode == "video"
        assert result.video_path is not None and result.video_path.exists()
        assert len(result.captures) == PREVIEW_VIDEO_THUMBNAIL_COUNT
        assert len(created_thumbs) == PREVIEW_VIDEO_THUMBNAIL_COUNT
        assert created_segments and not created_segments[0].exists()


class TestDynamicHdrPreviewSceneProbe:

    @staticmethod
    def _service() -> HdrMetadataProbeService:
        return HdrMetadataProbeService(
            ffmpeg_bin=lambda: "ffmpeg",
            tool_bin=lambda name: name,
        )

    def test_select_preview_scene_snaps_to_nearest_keyframe_for_hdr(self, tmp_path, monkeypatch):
        source = tmp_path / "hdr10p.mkv"
        source.touch()
        service = self._service()
        monkeypatch.setattr(
            service,
            "preview_keyframe_times",
            lambda *_args, **_kwargs: (0.0, 2.0, 10.0, 21.0, 40.0, 60.0),
        )

        selection = service.select_preview_scene(
            source,
            requested_time_s=22.0,
            source_duration_s=120.0,
            prefer_hdr10plus=True,
        )

        assert selection.scene_time_s == 21.0
        assert selection.reason == "I-frame HDR10+"
        assert selection.snapped is True

    def test_select_preview_scenes_random_skips_probe_for_sdr(self, tmp_path, monkeypatch):
        source = tmp_path / "sdr.mkv"
        source.touch()
        service = self._service()
        probe_calls: list[int] = []
        monkeypatch.setattr(
            service,
            "preview_keyframe_times",
            lambda *_args, **_kwargs: probe_calls.append(1) or (),
        )

        scenes = service.select_preview_scenes_random(
            source,
            count=7,
            source_duration_s=600.0,
            prefer_dovi=False,
            prefer_hdr10plus=False,
        )

        assert len(scenes) == 7
        assert probe_calls == []
        assert all(1.0 <= s.scene_time_s <= 595.0 for s in scenes)

    def test_select_preview_scenes_random_uses_keyframes_for_hdr(self, tmp_path, monkeypatch):
        source = tmp_path / "dv.mkv"
        source.touch()
        service = self._service()
        keyframes = tuple(float(i) * 2.0 for i in range(300))  # 0, 2, 4, ..., 598
        monkeypatch.setattr(
            service,
            "preview_keyframe_times",
            lambda *_args, **_kwargs: keyframes,
        )

        scenes = service.select_preview_scenes_random(
            source,
            count=7,
            source_duration_s=600.0,
            prefer_dovi=True,
        )

        assert len(scenes) == 7
        for s in scenes:
            assert s.scene_time_s in keyframes
            assert "I-frame" in s.reason


# ===========================================================================
# FFmpeg threads
# ===========================================================================

class TestFfmpegThreads:

    def test_default_threads_use_cpu_count_times_0_75_rounded_up(self):
        with patch("core.workflows.encode.workflow.os.cpu_count", return_value=8):
            wf = _make_workflow()
        assert wf._ffmpeg_threads == 6

    def test_negative_threads_fallback_to_default(self):
        with patch("core.workflows.encode.workflow.os.cpu_count", return_value=4):
            wf = _make_workflow(ffmpeg_threads=-3)
        assert wf._ffmpeg_threads == 3

    def test_single_pass_build_includes_threads(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.touch()
        wf = _make_workflow(ffmpeg_threads=10)
        cmd = wf.build_command_single(_make_config(src, tmp_path / "out.mkv"))

        assert "-threads" in cmd
        assert cmd[cmd.index("-threads") + 1] == "10"

    def test_two_pass_build_includes_threads_on_both_passes(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.touch()
        video = _make_video_settings(quality_mode=QualityMode.SIZE)
        wf = _make_workflow(ffmpeg_threads=14)
        cmds = wf.build_command(_make_config(src, tmp_path / "out.mkv", video=video))

        assert cmds[0][cmds[0].index("-threads") + 1] == "14"
        assert cmds[1][cmds[1].index("-threads") + 1] == "14"


class TestMaxParallelVideoEncodes:

    def test_default_is_one(self):
        wf = _make_workflow()
        assert wf._max_parallel_video_encodes == 1

    def test_setter_normalizes_to_minimum_one(self):
        wf = _make_workflow()
        wf.set_max_parallel_video_encodes(4)
        assert wf._max_parallel_video_encodes == 4
        wf.set_max_parallel_video_encodes(0)
        assert wf._max_parallel_video_encodes == 1


class TestVideoTrackPreparationOrchestrator:

    def test_ram_guard_serializes_disjoint_resources_when_budget_is_tight(self):
        available_ram = [10]
        first_started = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()
        active = 0
        max_active = 0
        gate = threading.Lock()
        results: list[tuple[int, dict[str, object], list[Path]]] = []

        def _task(order: int):
            def _run():
                nonlocal active, max_active
                with gate:
                    active += 1
                    max_active = max(max_active, active)
                try:
                    if order == 0:
                        first_started.set()
                        available_ram[0] = 5
                        assert release_first.wait(1.0)
                        available_ram[0] = 10
                    else:
                        second_started.set()
                    prepared: dict[str, object] = {"path": Path(f"/tmp/video_{order}.mkv")}
                    cleanup: list[Path] = []
                    return prepared, cleanup
                finally:
                    with gate:
                        active -= 1
            return _run

        orchestrator = encode_workflow_mod._VideoTrackPreparationOrchestrator(
            max_parallel=2,
            cancel_cb=lambda: None,
            min_available_ram_bytes=4,
            available_ram_cb=lambda: available_ram[0],
        )
        tasks = [
            encode_workflow_mod._VideoTrackPrepTask(
                order=0,
                resource_key="cpu",
                estimated_ram_bytes=3,
                run=_task(0),
            ),
            encode_workflow_mod._VideoTrackPrepTask(
                order=1,
                resource_key="gpu:nvenc",
                estimated_ram_bytes=3,
                run=_task(1),
            ),
        ]

        worker = threading.Thread(target=lambda: results.extend(orchestrator.execute(tasks)))
        worker.start()
        assert first_started.wait(1.0)
        time.sleep(0.1)
        assert not second_started.is_set()
        release_first.set()
        worker.join(1.0)

        assert not worker.is_alive()
        assert second_started.is_set()
        assert max_active == 1
        assert [order for order, _prepared, _cleanup in sorted(results)] == [0, 1]

    def test_ram_guard_allows_overlap_when_budget_is_sufficient(self):
        available_ram = [20]
        both_started = threading.Event()
        release_tasks = threading.Event()
        active = 0
        max_active = 0
        gate = threading.Lock()
        results: list[tuple[int, dict[str, object], list[Path]]] = []

        def _task(order: int):
            def _run():
                nonlocal active, max_active
                with gate:
                    active += 1
                    max_active = max(max_active, active)
                    if active >= 2:
                        both_started.set()
                try:
                    assert release_tasks.wait(1.0)
                    prepared: dict[str, object] = {"path": Path(f"/tmp/video_{order}.mkv")}
                    cleanup: list[Path] = []
                    return prepared, cleanup
                finally:
                    with gate:
                        active -= 1
            return _run

        orchestrator = encode_workflow_mod._VideoTrackPreparationOrchestrator(
            max_parallel=2,
            cancel_cb=lambda: None,
            min_available_ram_bytes=4,
            available_ram_cb=lambda: available_ram[0],
        )
        tasks = [
            encode_workflow_mod._VideoTrackPrepTask(
                order=0,
                resource_key="cpu",
                estimated_ram_bytes=3,
                run=_task(0),
            ),
            encode_workflow_mod._VideoTrackPrepTask(
                order=1,
                resource_key="gpu:nvenc",
                estimated_ram_bytes=3,
                run=_task(1),
            ),
        ]

        worker = threading.Thread(target=lambda: results.extend(orchestrator.execute(tasks)))
        worker.start()
        assert both_started.wait(1.0)
        release_tasks.set()
        worker.join(1.0)

        assert not worker.is_alive()
        assert max_active >= 2
        assert [order for order, _prepared, _cleanup in sorted(results)] == [0, 1]


class TestFfmpegProgressArgs:

    def test_single_pass_build_includes_machine_progress_flags(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.touch()
        wf = _make_workflow()

        cmd = wf.build_command_single(_make_config(src, tmp_path / "out.mkv"))

        assert "-progress" in cmd
        assert cmd[cmd.index("-progress") + 1] == "pipe:1"
        assert "-nostats" in cmd
        assert cmd.index("-nostats") < cmd.index("-i")

    def test_two_pass_build_uses_machine_progress_and_platform_null_sink(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.touch()
        wf = _make_workflow()
        video = _make_video_settings(quality_mode=QualityMode.SIZE)

        cmds = wf.build_command(_make_config(src, tmp_path / "out.mkv", video=video, duration_s=3600.0))

        for cmd in cmds:
            assert "-progress" in cmd
            assert cmd[cmd.index("-progress") + 1] == "pipe:1"
            assert "-nostats" in cmd
        assert cmds[0][-1] == os.devnull

    def test_video_only_build_includes_machine_progress_flags(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.touch()
        wf = _make_workflow()
        output_hevc = tmp_path / "enc.hevc"

        cmd = wf._build_video_only_cmd(_make_config(src, tmp_path / "out.mkv"), output_hevc)

        assert "-progress" in cmd
        assert cmd[cmd.index("-progress") + 1] == "pipe:1"
        assert "-nostats" in cmd


class TestTwoPassPerTrackPasslogfile:

    def test_multi_video_track_two_pass_uses_track_specific_passlogfile(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.touch()
        out = tmp_path / "video_1.mkv"
        passlog_prefix = tmp_path / "ffmpeg2pass-video_1"
        wf = _make_workflow()
        video = _make_video_settings(codec="libx265", quality_mode=QualityMode.SIZE)
        cfg = _make_config(src, tmp_path / "out.mkv", video=video, duration_s=3600.0)

        cmds = wf._build_multi_video_track_encode_commands(
            cfg,
            video,
            src,
            out,
            passlog_prefix=passlog_prefix,
        )

        assert len(cmds) == 2
        assert "-passlogfile" in cmds[0]
        assert cmds[0][cmds[0].index("-passlogfile") + 1] == str(passlog_prefix)
        assert "-passlogfile" in cmds[1]
        assert cmds[1][cmds[1].index("-passlogfile") + 1] == str(passlog_prefix)

    def test_video_only_two_pass_for_track_uses_track_specific_passlogfile(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.touch()
        out = tmp_path / "video_1.hevc"
        passlog_prefix = tmp_path / "ffmpeg2pass-video_1"
        wf = _make_workflow()
        video = _make_video_settings(codec="libx265", quality_mode=QualityMode.SIZE)
        cfg = _make_config(src, tmp_path / "out.mkv", video=video, duration_s=3600.0)

        cmds = wf._build_video_only_two_pass_for_track(
            cfg,
            video,
            src,
            out,
            passlog_prefix=passlog_prefix,
        )

        assert len(cmds) == 2
        assert "-passlogfile" in cmds[0]
        assert cmds[0][cmds[0].index("-passlogfile") + 1] == str(passlog_prefix)
        assert "-passlogfile" in cmds[1]
        assert cmds[1][cmds[1].index("-passlogfile") + 1] == str(passlog_prefix)

# ===========================================================================
# _total_ram_bytes — cross-platform
# ===========================================================================

class TestTotalRamBytes:

    def test_linux_reads_memtotal(self):
        """Linux : MemTotal depuis /proc/meminfo."""
        fake = "MemTotal:       16384000 kB\nMemFree: 4096000 kB\n"
        with patch("sys.platform", "linux"), \
             patch.object(Path, "read_text", return_value=fake):
            assert _ram_buffer_mod.total_ram_bytes() == 16_384_000 * 1024

    def test_linux_returns_zero_if_memtotal_absent(self):
        fake = "MemFree: 4096000 kB\n"
        with patch("sys.platform", "linux"), \
             patch.object(Path, "read_text", return_value=fake):
            assert _ram_buffer_mod.total_ram_bytes() == 0

    def test_linux_returns_zero_on_ioerror(self):
        with patch("sys.platform", "linux"), \
             patch.object(Path, "read_text", side_effect=OSError):
            assert _ram_buffer_mod.total_ram_bytes() == 0

    def test_macos_parses_sysctl(self):
        """macOS : sysctl hw.memsize retourne RAM totale en octets."""
        with patch("sys.platform", "darwin"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="17179869184\n")
            assert _ram_buffer_mod.total_ram_bytes() == 17_179_869_184

    def test_macos_returns_zero_on_sysctl_failure(self):
        with patch("sys.platform", "darwin"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert _ram_buffer_mod.total_ram_bytes() == 0

    def test_windows_uses_ctypes(self):
        """Windows : lit ullTotalPhys via GlobalMemoryStatusEx."""
        fake_stat = MagicMock()
        fake_stat.ullTotalPhys = 17_179_869_184
        with patch("sys.platform", "win32"), \
             patch.object(_ram_buffer_mod, "_win_mem_status", return_value=fake_stat):
            assert _ram_buffer_mod.total_ram_bytes() == 17_179_869_184

    def test_windows_returns_zero_if_ctypes_unavailable(self):
        """Windows : fallback à 0 si ctypes/_ctypes est indisponible."""
        with patch("sys.platform", "win32"), \
             patch.object(_ram_buffer_mod, "_win_mem_status", side_effect=ImportError):
            assert _ram_buffer_mod.total_ram_bytes() == 0

    def test_unknown_platform_returns_zero(self):
        with patch("sys.platform", "freebsd"):
            assert _ram_buffer_mod.total_ram_bytes() == 0


# ===========================================================================
# _available_ram_bytes — cross-platform
# ===========================================================================

class TestAvailableRamBytes:

    def test_linux_reads_memavailable(self):
        """Linux : MemAvailable depuis /proc/meminfo."""
        fake = "MemTotal: 16384000 kB\nMemAvailable: 8192000 kB\n"
        with patch("sys.platform", "linux"), \
             patch.object(Path, "read_text", return_value=fake):
            assert _ram_buffer_mod.available_ram_bytes() == 8_192_000 * 1024

    def test_linux_returns_zero_when_memavailable_absent(self):
        fake = "MemTotal: 16384000 kB\nMemFree: 4096000 kB\n"
        with patch("sys.platform", "linux"), \
             patch.object(Path, "read_text", return_value=fake):
            assert _ram_buffer_mod.available_ram_bytes() == 0

    def test_linux_returns_zero_on_ioerror(self):
        with patch("sys.platform", "linux"), \
             patch.object(Path, "read_text", side_effect=OSError):
            assert _ram_buffer_mod.available_ram_bytes() == 0

    def test_linux_parses_large_values(self):
        fake = "MemAvailable:   67108864 kB\n"
        with patch("sys.platform", "linux"), \
             patch.object(Path, "read_text", return_value=fake):
            assert _ram_buffer_mod.available_ram_bytes() == 67_108_864 * 1024

    def test_macos_delegates_to_macos_available_ram(self):
        """macOS : délègue à _macos_available_ram."""
        with patch("sys.platform", "darwin"), \
             patch.object(_ram_buffer_mod, "macos_available_ram", return_value=4_000_000_000):
            assert _ram_buffer_mod.available_ram_bytes() == 4_000_000_000

    def test_windows_reads_ullavailphys(self):
        fake_stat = MagicMock()
        fake_stat.ullAvailPhys = 8_589_934_592
        with patch("sys.platform", "win32"), \
             patch.object(_ram_buffer_mod, "_win_mem_status", return_value=fake_stat):
            assert _ram_buffer_mod.available_ram_bytes() == 8_589_934_592

    def test_windows_available_returns_zero_if_ctypes_unavailable(self):
        with patch("sys.platform", "win32"), \
             patch.object(_ram_buffer_mod, "_win_mem_status", side_effect=ImportError):
            assert _ram_buffer_mod.available_ram_bytes() == 0

    def test_unknown_platform_returns_zero(self):
        with patch("sys.platform", "freebsd"):
            assert _ram_buffer_mod.available_ram_bytes() == 0


# ===========================================================================
# _macos_available_ram
# ===========================================================================

class TestMacosAvailableRam:

    _VM_STAT_SAMPLE = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                               1234.\n"
        "Pages active:                             5678.\n"
        "Pages inactive:                           2000.\n"
        "Pages speculative:                         500.\n"
        "Pages throttled:                             0.\n"
        "Pages wired down:                         3000.\n"
        "Pages purgeable:                           300.\n"
    )

    def test_parses_free_inactive_speculative_purgeable(self):
        """Additionne free+inactive+speculative+purgeable avec la bonne taille de page."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=self._VM_STAT_SAMPLE)
            result = _ram_buffer_mod.macos_available_ram()
        expected = (1234 + 2000 + 500 + 300) * 16384
        assert result == expected

    def test_returns_zero_on_subprocess_failure(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert _ram_buffer_mod.macos_available_ram() == 0

    def test_uses_default_page_size_4096_when_not_parseable(self):
        """Taille de page par défaut = 4096 si non parseable."""
        vm_stat_no_pagesize = (
            "Pages free:     1000.\n"
            "Pages inactive:  500.\n"
            "Pages speculative: 200.\n"
            "Pages purgeable:   100.\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=vm_stat_no_pagesize)
            result = _ram_buffer_mod.macos_available_ram()
        assert result == (1000 + 500 + 200 + 100) * 4096


# ===========================================================================
# _ram_buffer_dir
# ===========================================================================

class TestRamBufferDir:

    def test_linux_returns_dev_shm_when_writable(self):
        with patch("sys.platform", "linux"), \
             patch.object(Path, "is_dir", return_value=True), \
             patch("os.access", return_value=True):
            assert _ram_buffer_mod.ram_buffer_dir() == Path("/dev/shm")

    def test_linux_returns_none_when_shm_not_dir(self):
        with patch("sys.platform", "linux"), \
             patch.object(Path, "is_dir", return_value=False):
            assert _ram_buffer_mod.ram_buffer_dir() is None

    def test_linux_returns_none_when_shm_not_writable(self):
        with patch("sys.platform", "linux"), \
             patch.object(Path, "is_dir", return_value=True), \
             patch("os.access", return_value=False):
            assert _ram_buffer_mod.ram_buffer_dir() is None

    def test_macos_returns_dev_shm_when_available(self):
        with patch("sys.platform", "darwin"), \
             patch.object(Path, "is_dir", return_value=True), \
             patch("os.access", return_value=True):
            assert _ram_buffer_mod.ram_buffer_dir() == Path("/dev/shm")

    def test_windows_always_returns_none(self):
        """Windows n'a pas de répertoire RAM standard."""
        with patch("sys.platform", "win32"):
            assert _ram_buffer_mod.ram_buffer_dir() is None

    def test_unknown_platform_returns_none(self):
        with patch("sys.platform", "freebsd11"):
            assert _ram_buffer_mod.ram_buffer_dir() is None


# ===========================================================================
# _shm_path — formule de seuil  (available - file_size ≥ total × pct / 100)
# ===========================================================================

class TestShmPath:
    """
    Formule : available_before - file_size >= total_ram * threshold_pct / 100
    Valeurs par défaut du workflow : enabled=True, threshold=15 %
    """

    def _wf(self, enabled=True, threshold=15) -> EncodeWorkflow:
        return _make_workflow(enabled=enabled, threshold=threshold)

    def _patch_ram(self, available: int, total: int) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(patch.object(_ram_buffer_mod, "available_ram_bytes", return_value=available))
        stack.enter_context(patch.object(_ram_buffer_mod, "total_ram_bytes",     return_value=total))
        stack.enter_context(patch.object(_ram_buffer_mod, "ram_buffer_dir",      return_value=Path("/dev/shm")))
        return stack

    # ── formule correcte ─────────────────────────────────────────────────────

    def test_uses_shm_when_formula_satisfied(self, tmp_path):
        """
        total=10 Go, file=1 Go, available=3 Go
        3 - 1 = 2 Go  ≥  10 × 15% = 1.5 Go  → RAM
        """
        total     = 10 * 2**30
        file_size =  1 * 2**30
        available =  3 * 2**30
        wf = self._wf()
        with self._patch_ram(available, total):
            result = wf._shm_path(tmp_path, "test.hevc", file_size)
        assert result == Path("/dev/shm/test.hevc")

    def test_uses_disk_when_formula_not_satisfied(self, tmp_path):
        """
        total=10 Go, file=1 Go, available=2.4 Go
        2.4 - 1 = 1.4 Go  <  10 × 15% = 1.5 Go  → disque
        """
        total     = 10 * 2**30
        file_size =  1 * 2**30
        available = int(2.4 * 2**30)
        wf = self._wf()
        with self._patch_ram(available, total):
            result = wf._shm_path(tmp_path, "test.hevc", file_size)
        assert result == tmp_path / "test.hevc"

    def test_boundary_exactly_at_threshold_uses_shm(self, tmp_path):
        """
        Exactement à la limite : available - file = total × pct / 100
        → condition satisfaite (>=) → RAM
        """
        total     = 10 * 2**30
        threshold = 15
        file_size =  1 * 2**30
        available = file_size + int(total * threshold / 100)   # exactement égal
        wf = self._wf(threshold=threshold)
        with self._patch_ram(available, total):
            result = wf._shm_path(tmp_path, "test.hevc", file_size)
        assert result == Path("/dev/shm/test.hevc")

    def test_boundary_one_byte_above_threshold_uses_shm(self, tmp_path):
        """Un octet au-dessus de la limite exacte → RAM."""
        total     = 10 * 2**30
        threshold = 15
        file_size =  1 * 2**30
        available = file_size + int(total * threshold / 100) + 1
        wf = self._wf(threshold=threshold)
        with self._patch_ram(available, total):
            result = wf._shm_path(tmp_path, "test.hevc", file_size)
        assert result == Path("/dev/shm/test.hevc")

    # ── config disabled ───────────────────────────────────────────────────────

    def test_disabled_config_always_returns_disk(self, tmp_path):
        """ram_buffer_enabled=False → disque même avec RAM abondante."""
        wf = self._wf(enabled=False)
        with self._patch_ram(10 * 2**30, 10 * 2**30):
            result = wf._shm_path(tmp_path, "test.hevc", 1_000_000)
        assert result == tmp_path / "test.hevc"

    # ── ram_buffer_dir absent ─────────────────────────────────────────────────

    def test_no_ram_dir_returns_disk(self, tmp_path):
        """Pas de répertoire RAM (Windows) → disque même avec RAM suffisante."""
        wf = self._wf()
        with patch.object(_ram_buffer_mod, "ram_buffer_dir", return_value=None), \
             patch.object(_ram_buffer_mod, "available_ram_bytes", return_value=10 * 2**30), \
             patch.object(_ram_buffer_mod, "total_ram_bytes",     return_value=10 * 2**30):
            result = wf._shm_path(tmp_path, "test.hevc", 1_000_000)
        assert result == tmp_path / "test.hevc"

    # ── RAM indisponible / inconnue ───────────────────────────────────────────

    def test_zero_total_returns_disk(self, tmp_path):
        """total=0 → impossible d'évaluer le seuil → disque."""
        wf = self._wf()
        with patch.object(_ram_buffer_mod, "ram_buffer_dir", return_value=Path("/dev/shm")), \
             patch.object(_ram_buffer_mod, "available_ram_bytes", return_value=8 * 2**30), \
             patch.object(_ram_buffer_mod, "total_ram_bytes",     return_value=0):
            result = wf._shm_path(tmp_path, "test.hevc", 1_000_000)
        assert result == tmp_path / "test.hevc"

    def test_zero_available_returns_disk(self, tmp_path):
        """available=0 → disque."""
        wf = self._wf()
        with patch.object(_ram_buffer_mod, "ram_buffer_dir", return_value=Path("/dev/shm")), \
             patch.object(_ram_buffer_mod, "available_ram_bytes", return_value=0), \
             patch.object(_ram_buffer_mod, "total_ram_bytes",     return_value=10 * 2**30):
            result = wf._shm_path(tmp_path, "test.hevc", 1_000_000)
        assert result == tmp_path / "test.hevc"

    # ── seuil configurable ────────────────────────────────────────────────────

    def test_custom_threshold_5pct(self, tmp_path):
        """
        Seuil 5% : total=10 Go, file=1 Go, available=1.5 Go
        1.5 - 1 = 0.5 Go  ≥  10 × 5% = 0.5 Go  → exactement égal → RAM (>=)
        """
        total     = 10 * 2**30
        file_size =  1 * 2**30
        available = file_size + int(total * 5 / 100)
        wf = self._wf(threshold=5)
        with self._patch_ram(available, total):
            result = wf._shm_path(tmp_path, "test.hevc", file_size)
        assert result == Path("/dev/shm/test.hevc")   # exactement égal → RAM

    def test_threshold_clamped_to_90(self, tmp_path):
        """threshold > 90 est ramené à 90 par le constructeur."""
        wf = _make_workflow(threshold=200)
        assert wf._ram_buffer_threshold_pct == 90

    def test_threshold_clamped_to_0(self, tmp_path):
        """threshold < 0 est ramené à 0 (toujours utiliser RAM si disponible)."""
        wf = _make_workflow(threshold=-5)
        assert wf._ram_buffer_threshold_pct == 0


# ===========================================================================
# _run_with_metadata_inject — gestion fichiers (mock _run_cmd)
# ===========================================================================

def _collect_signals(signals, timeout=10.0):
    app = _get_app()
    done = [False]
    signals.finished.connect(lambda _: done.__setitem__(0, True),
                              Qt.ConnectionType.QueuedConnection)
    signals.failed.connect(lambda *_: done.__setitem__(0, True),
                            Qt.ConnectionType.QueuedConnection)
    signals.cancelled.connect(lambda: done.__setitem__(0, True),
                               Qt.ConnectionType.QueuedConnection)
    deadline = time.monotonic() + timeout
    while not done[0] and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


class TestMetadataInjectFileManagement:

    def _run_inject(self, tmp_path: Path, copy_dv: bool, copy_hdr10plus: bool,
                    ram_enabled: bool = False) -> list[list[Path]]:
        """
        Lance le workflow avec des mocks.
        ram_enabled=False : force les HEVC sur disque pour tester les snapshots disque.
        Retourne la liste des snapshots (fichiers présents après chaque _run_cmd).
        """
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        out = tmp_path / "output.mkv"
        config = _make_config(source=src, output=out, copy_dv=copy_dv,
                              copy_hdr10plus=copy_hdr10plus,
                              work_dir=tmp_path / "work")
        wf = _make_workflow(enabled=ram_enabled)
        snapshots: list[list[Path]] = []

        def _fake_run_cmd(cmd, signals=None, cwd=None, progress_cb=None):
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 100_000)
                    break
            work = tmp_path / "work"
            snap: list[Path] = []
            if work.exists():
                snap = [p for p in work.rglob("*")
                        if p.is_file() and p.suffix in (".hevc", ".mkv")]
            snapshots.append(snap[:])
            return ""

        # Force disque pour tests de comptage (pas de /dev/shm)
        with patch.object(EncodeWorkflow, "_shm_path",
                          side_effect=lambda tmp_dir, name, _size: tmp_dir / name):
            with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run_cmd):
                sigs = wf._run_with_metadata_inject(config)
                _collect_signals(sigs)
        return snapshots

    # ── pas de src.hevc ──────────────────────────────────────────────────────

    @pytest.mark.parametrize("copy_dv,copy_hdr10plus", [
        (True, False), (False, True), (True, True)
    ])
    def test_src_hevc_is_never_created(self, tmp_path, copy_dv, copy_hdr10plus):
        """Le workflow n'alloue jamais de src.hevc intermédiaire."""
        snapshots = self._run_inject(tmp_path, copy_dv, copy_hdr10plus)
        assert all(
            not any("src.hevc" in str(path) for path in snap)
            for snap in snapshots
        )

    # ── cleanup des intermédiaires ────────────────────────────────────────────

    def test_enc_hevc_deleted_after_hdr10plus_injection(self, tmp_path):
        """enc.hevc absent dans tous les snapshots après la création de enc_hdr10p.hevc."""
        snapshots = self._run_inject(tmp_path, copy_dv=False, copy_hdr10plus=True)
        first_hdr10p = next(
            (i for i, snap in enumerate(snapshots)
             if any("enc_hdr10p.hevc" in str(p) for p in snap)), None
        )
        assert first_hdr10p is not None, "enc_hdr10p.hevc jamais créé"
        for snap in snapshots[first_hdr10p + 1:]:
            names = [p.name for p in snap]
            assert "enc.hevc" not in names, \
                f"enc.hevc encore présent après injection HDR10+ : {names}"

    def test_enc_hdr10p_deleted_after_dv_injection(self, tmp_path):
        """enc_hdr10p.hevc absent dans tous les snapshots après création de enc_dv.hevc."""
        snapshots = self._run_inject(tmp_path, copy_dv=True, copy_hdr10plus=True)
        first_dv = next(
            (i for i, snap in enumerate(snapshots)
             if any("enc_dv.hevc" in str(p) for p in snap)), None
        )
        assert first_dv is not None, "enc_dv.hevc jamais créé"
        for snap in snapshots[first_dv + 1:]:
            names = [p.name for p in snap]
            assert "enc_hdr10p.hevc" not in names, \
                f"enc_hdr10p.hevc encore présent après injection DV : {names}"

    # ── comptage max 2 fichiers ───────────────────────────────────────────────

    @pytest.mark.parametrize("copy_dv,copy_hdr10plus", [
        (True, False), (False, True), (True, True)
    ])
    def test_max_two_large_files_at_any_time(self, tmp_path, copy_dv, copy_hdr10plus):
        """Jamais plus de 2 gros fichiers simultanés sur disque."""
        snapshots = self._run_inject(tmp_path, copy_dv, copy_hdr10plus)
        for i, snap in enumerate(snapshots):
            count = len([p for p in snap if p.exists()])
            assert count <= 2, (
                f"Snapshot {i} : {count} fichiers > 2 : {[p.name for p in snap]}"
            )

    # ── buffer RAM désactivé ──────────────────────────────────────────────────

    def test_disabled_ram_buffer_no_ext_files(self, tmp_path):
        """
        ram_buffer_enabled=False → _shm_path retourne toujours disque
        → ext_files reste vide tout au long du workflow.
        """
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        config = _make_config(source=src, output=tmp_path / "out.mkv",
                              copy_dv=True, copy_hdr10plus=True,
                              work_dir=tmp_path / "work")
        wf = _make_workflow(enabled=False)

        shm_calls: list[Path] = []

        def _fake_shm_path(tmp_dir, name, _size):
            # Avec enabled=False, le vrai _shm_path retourne toujours disque
            p = tmp_dir / name
            if p.parent == Path("/dev/shm"):
                shm_calls.append(p)
            return p

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 50_000)
                    break
            return ""

        with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
            sigs = wf._run_with_metadata_inject(config)
            _collect_signals(sigs)

        assert shm_calls == [], "Des fichiers /dev/shm créés malgré enabled=False"


class TestMetadataInjectDoviProfileRouting:

    @staticmethod
    def _run_inject(tmp_path: Path, *, dovi_profile: str) -> list[list[str]]:
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        config = _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="libx265"),
            copy_dv=True,
            dovi_profile=dovi_profile,
            work_dir=tmp_path / "work",
        )
        wf = _make_workflow(enabled=False)
        cmds_run: list[list[str]] = []

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            cmds_run.append(list(cmd))
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json", ".ffmetadata")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 100_000)
                    break
            return ""

        with patch.object(EncodeWorkflow, "_shm_path", side_effect=lambda tmp_dir, name, _size: tmp_dir / name):
            with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
                sigs = wf._run_with_metadata_inject(config)
                _collect_signals(sigs)
        return cmds_run

    def test_p7_source_auto_converts_even_when_user_profile_is_0(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        config = _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="libx265"),
            copy_dv=True,
            dovi_profile="0",
            work_dir=tmp_path / "work",
        )
        wf = _make_workflow(enabled=False)
        cmds_run: list[list[str]] = []
        mi_p7_fel = {
            "HDR_Format": "Dolby Vision, Version 1.0, dvhe.07.06, BL+EL+RPU",
            "HDR_Format_Profile": "dvhe.07 / 06",
            "HDR_Format_Settings": "BL+EL+RPU",
        }

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            cmds_run.append(list(cmd))
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json", ".ffmetadata")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 100_000)
                    break
            return ""

        with patch.object(EncodeWorkflow, "_shm_path", side_effect=lambda tmp_dir, name, _size: tmp_dir / name):
            with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
                with patch(
                    "core.workflows.encode.runtime.metadata_inject._load_mediainfo_video",
                    return_value=mi_p7_fel,
                ):
                    sigs = wf._run_with_metadata_inject(config)
                    _collect_signals(sigs)

        convert_cmds = [
            cmd for cmd in cmds_run
            if len(cmd) >= 4 and cmd[:4] == ["dovi_tool", "-m", "2", "convert"]
        ]
        assert len(convert_cmds) == 1
        assert "--discard" in convert_cmds[0]

        extract_cmds = [cmd for cmd in cmds_run if cmd[:2] == ["dovi_tool", "extract-rpu"]]
        assert len(extract_cmds) == 1
        assert extract_cmds[0][extract_cmds[0].index("-i") + 1].endswith("source_p8.hevc")

        inject_cmds = [cmd for cmd in cmds_run if "inject-rpu" in cmd]
        assert len(inject_cmds) == 1
        assert inject_cmds[0][:3] == ["dovi_tool", "-m", "2"]

    def test_normalize_p8_1_converts_p7_source_before_extract_and_encode(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        config = _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="libx265"),
            copy_dv=True,
            dovi_profile="2",
            work_dir=tmp_path / "work",
        )
        wf = _make_workflow(enabled=False)
        cmds_run: list[list[str]] = []
        mi_p7_fel = {
            "HDR_Format": "Dolby Vision, Version 1.0, dvhe.07.06, BL+EL+RPU",
            "HDR_Format_Profile": "dvhe.07 / 06",
            "HDR_Format_Settings": "BL+EL+RPU",
        }

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            cmds_run.append(list(cmd))
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json", ".ffmetadata")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 100_000)
                    break
            return ""

        with patch.object(EncodeWorkflow, "_shm_path", side_effect=lambda tmp_dir, name, _size: tmp_dir / name):
            with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
                with patch(
                    "core.workflows.encode.runtime.metadata_inject._load_mediainfo_video",
                    return_value=mi_p7_fel,
                ):
                    sigs = wf._run_with_metadata_inject(config)
                    _collect_signals(sigs)

        convert_cmds = [
            cmd for cmd in cmds_run
            if len(cmd) >= 4 and cmd[:4] == ["dovi_tool", "-m", "2", "convert"]
        ]
        assert len(convert_cmds) == 1
        assert "--discard" in convert_cmds[0]

        extract_cmds = [cmd for cmd in cmds_run if cmd[:2] == ["dovi_tool", "extract-rpu"]]
        assert len(extract_cmds) == 1
        assert extract_cmds[0][extract_cmds[0].index("-i") + 1].endswith("source_p8.hevc")

        inject_cmds = [cmd for cmd in cmds_run if "inject-rpu" in cmd]
        assert len(inject_cmds) == 1
        assert inject_cmds[0][:3] == ["dovi_tool", "-m", "2"]

    def test_dynamic_hdr_copy_reinjects_static_hdr_even_on_libx265(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        config = _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(
                codec="libx265",
                master_display="G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)",
                max_cll="1000,400",
            ),
            copy_dv=True,
            work_dir=tmp_path / "work",
        )
        wf = _make_workflow(enabled=False)
        calls: list[tuple[Path, Path, str, str]] = []

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json", ".ffmetadata")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 100_000)
                    break
            return ""

        def _fake_static_patch(src_path, dst_path, *, master_display, max_cll):
            calls.append((src_path, dst_path, master_display, max_cll))
            dst_path.write_bytes(b"patched")
            from core.workflows.hevc_static_hdr_metadata import StaticHdrSeiInjectionResult
            return StaticHdrSeiInjectionResult(
                access_units=10,
                targeted_access_units=2,
                injected_access_units=2,
                preserved_access_units=0,
            )

        with patch.object(EncodeWorkflow, "_shm_path", side_effect=lambda tmp_dir, name, _size: tmp_dir / name):
            with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
                with patch(
                    "core.workflows.encode.runtime.metadata_inject.inject_static_hdr_sei_file",
                    side_effect=_fake_static_patch,
                ):
                    sigs = wf._run_with_metadata_inject(config)
                    _collect_signals(sigs)

        assert len(calls) == 1
        src_path, dst_path, master_display, max_cll = calls[0]
        assert src_path.name == "enc_dv.hevc"
        assert dst_path.name == "enc_hdr_static.hevc"
        assert config.video is not None
        assert master_display == config.video.master_display
        assert max_cll == config.video.max_cll


class TestRuntimeCleanup:
    def test_run_two_pass_cleans_passlogs(self, tmp_path):
        wf = _make_workflow()

        def _fake_run(cmd, cwd=None, label=None, progress_cb=None, signals=None):
            assert cwd == tmp_path
            (tmp_path / "ffmpeg2pass-0.log").write_text("log", encoding="utf-8")
            (tmp_path / "ffmpeg2pass-0.log.mbtree").write_text("tree", encoding="utf-8")
            return ""

        with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
            sigs = wf._run_two_pass([["ffmpeg", "-pass", "1"], ["ffmpeg", "-pass", "2"]], cwd=tmp_path)
            _collect_signals(sigs)

        assert not (tmp_path / "ffmpeg2pass-0.log").exists()
        assert not (tmp_path / "ffmpeg2pass-0.log.mbtree").exists()

    def test_run_cleans_relocated_tmdb_covers(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1000)
        work_dir = tmp_path / "work"
        tmdb_root = work_dir / "tmdb_covers"
        guid_dir = tmdb_root / "deadbeef"
        guid_dir.mkdir(parents=True)
        cover = guid_dir / "cover.jpg"
        cover.write_bytes(b"jpeg")
        out = tmp_path / "output.mkv"
        cfg = _make_config(
            source=src,
            output=out,
            video=_make_video_settings(codec="copy"),
            extra_attachments=[cover],
            work_dir=work_dir,
        )
        wf = _make_workflow()

        with patch.object(wf._runner, "run") as mock_run:
            signals = TaskSignals()
            mock_run.return_value = signals
            returned = wf.run(cfg)
            process_dir = work_dir / "output"
            attachments_dir = process_dir / "attachments"
            assert (attachments_dir / "cover.jpg").exists()
            assert not guid_dir.exists()
            returned.finished.emit("done")
            _get_app().processEvents()

        assert not attachments_dir.exists()
        assert not process_dir.exists()


# ===========================================================================
# Cleanup ext_files sur annulation / exception
# ===========================================================================

class TestExtFilesCleanupOnFailure:

    def _run_and_fail(self, tmp_path: Path, fail_after: int, exc_class) -> list[Path]:
        """Simule une erreur après N commandes, retourne les fichiers /dev/shm créés."""
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        config = _make_config(source=src, output=tmp_path / "out.mkv",
                              copy_dv=True, copy_hdr10plus=True,
                              work_dir=tmp_path / "work")
        wf = _make_workflow(enabled=True)

        call_n = [0]
        created_ext: list[Path] = []
        shm = Path("/dev/shm")

        def _fake_shm_path(tmp_dir, name, _size):
            if name.endswith(".hevc"):
                p = shm / f"_test_cleanup_{id(wf)}_{name}"
                created_ext.append(p)
                return p
            return tmp_dir / name

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            call_n[0] += 1
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 50_000)
                    break
            if call_n[0] >= fail_after:
                raise exc_class("Simulated")
            return ""

        with patch.object(EncodeWorkflow, "_shm_path", side_effect=_fake_shm_path), \
             patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
            sigs = wf._run_with_metadata_inject(config)
            _collect_signals(sigs)

        return created_ext

    def test_ext_files_cleaned_on_runtime_error(self, tmp_path):
        """Fichiers ext (shm) supprimés si RuntimeError."""
        created = self._run_and_fail(tmp_path, fail_after=4, exc_class=RuntimeError)
        for p in created:
            assert not p.exists(), f"Fichier ext non nettoyé : {p}"

    def test_ext_files_cleaned_on_cancel(self, tmp_path):
        """Fichiers ext (shm) supprimés si TaskCancelledError."""
        from core.runner import TaskCancelledError
        created = self._run_and_fail(tmp_path, fail_after=4, exc_class=TaskCancelledError)
        for p in created:
            assert not p.exists(), f"Fichier ext non nettoyé après cancel : {p}"

    def test_finally_iterates_copy_of_ext_files(self, tmp_path):
        """
        Le finally itère sur une copie de ext_files (list(ext_files)).
        Pas de RuntimeError si ext_files est muté pendant l'itération.
        Ce test vérifie qu'aucune exception ne remonte du finally.
        """
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        config = _make_config(source=src, output=tmp_path / "out.mkv",
                              copy_dv=True, work_dir=tmp_path / "work")
        wf = _make_workflow(enabled=True)
        connected = threading.Event()

        def _fast_fail(cmd, signals=None, cwd=None, progress_cb=None):
            connected.wait(timeout=1.0)
            raise RuntimeError("Fast fail")

        result: list = []
        # Les connexions doivent être établies avant le lancement du thread
        # pour ne pas manquer un signal émis avant processEvents().
        with patch.object(EncodeWorkflow, "_shm_path",
                          side_effect=lambda t, n, _: t / n), \
             patch.object(wf._runner, "_run_cmd", side_effect=_fast_fail):
            sigs = wf._run_with_metadata_inject(config)
            sigs.failed.connect(lambda msg, _: result.append(msg),
                                Qt.ConnectionType.QueuedConnection)
            sigs.finished.connect(lambda _: result.append("ok"),
                                  Qt.ConnectionType.QueuedConnection)
            connected.set()
            _collect_signals(sigs)
        # Le workflow se termine proprement (pas de crash dans le finally)
        assert len(result) == 1


# ===========================================================================
# build_command (régression)
# ===========================================================================

class TestBuildCommand:

    def setup_method(self):
        self.wf = _make_workflow()

    def test_crf_mode_returns_single_command(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        config = _make_config(src, tmp_path / "out.mkv",
                              video=_make_video_settings(quality_mode=QualityMode.CRF))
        result = self.wf.build_command(config)
        assert isinstance(result, list) and isinstance(result[0], str)

    def test_size_mode_returns_two_commands(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        config = _make_config(src, tmp_path / "out.mkv",
                              video=_make_video_settings(quality_mode=QualityMode.SIZE),
                              duration_s=3600.0)
        result = self.wf.build_command(config)
        assert isinstance(result, list) and isinstance(result[0], list)
        assert len(result) == 2

    def test_single_pass_contains_source_and_output(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        out = tmp_path / "out.mkv"
        cmd = _as_single_command(self.wf.build_command(_make_config(src, out)))
        cmd_str = " ".join(cmd)
        assert str(src) in cmd_str and str(out) in cmd_str

    def test_copy_codec_no_crf_flag(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        config = _make_config(src, tmp_path / "out.mkv",
                              video=_make_video_settings(codec="copy"))
        assert "-crf" not in self.wf.build_command(config)

    def test_audio_copy_codec_no_bitrate(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        track = AudioTrackSettings(stream_index=1, codec="copy", bitrate_kbps=384)
        cmd = self.wf.build_command(_make_config(src, tmp_path / "out.mkv",
                                                 audio_tracks=[track]))
        assert "-b:a:0" not in cmd

    def test_audio_aac_has_bitrate(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        track = AudioTrackSettings(stream_index=1, codec="aac", bitrate_kbps=256)
        cmd = self.wf.build_command(_make_config(src, tmp_path / "out.mkv",
                                                 audio_tracks=[track]))
        assert "-b:a:0" in cmd and "256k" in cmd

    def test_audio_ac3_has_bitrate(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        track = AudioTrackSettings(stream_index=1, codec="ac3", bitrate_kbps=448)
        cmd = _as_single_command(
            self.wf.build_command(_make_config(src, tmp_path / "out.mkv", audio_tracks=[track]))
        )
        assert "-c:a:0" in cmd and cmd[cmd.index("-c:a:0") + 1] == "ac3"
        assert "-b:a:0" in cmd and "448k" in cmd

    def test_audio_ac3_off_grid_bitrate_is_normalized(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        track = AudioTrackSettings(stream_index=1, codec="ac3", bitrate_kbps=444)
        cmd = self.wf.build_command(_make_config(src, tmp_path / "out.mkv",
                                                 audio_tracks=[track]))
        assert "-b:a:0" in cmd and "448k" in cmd

    def test_audio_eac3_bitrate_is_clamped_by_output_channels(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        track = AudioTrackSettings(
            stream_index=1,
            codec="eac3",
            bitrate_kbps=4096,
            input_channels=2,
            input_channel_layout="stereo",
        )
        cmd = self.wf.build_command(_make_config(src, tmp_path / "out.mkv",
                                                 audio_tracks=[track]))
        assert "-b:a:0" in cmd and "2048k" in cmd

    def test_audio_eac3_7_1_forces_5_1_downmix(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        track = AudioTrackSettings(
            stream_index=1,
            codec="eac3",
            bitrate_kbps=640,
            input_channels=8,
            input_channel_layout="7.1",
        )
        cmd = _as_single_command(
            self.wf.build_command(_make_config(src, tmp_path / "out.mkv", audio_tracks=[track]))
        )
        assert "-ac:a:0" in cmd and cmd[cmd.index("-ac:a:0") + 1] == "6"
        assert "-channel_layout:a:0" in cmd and cmd[cmd.index("-channel_layout:a:0") + 1] == "5.1"

    def test_audio_eac3_5_1_keeps_native_channels(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        track = AudioTrackSettings(
            stream_index=1,
            codec="eac3",
            bitrate_kbps=640,
            input_channels=6,
            input_channel_layout="5.1",
        )
        cmd = self.wf.build_command(_make_config(src, tmp_path / "out.mkv", audio_tracks=[track]))
        assert "-ac:a:0" not in cmd
        assert "-channel_layout:a:0" not in cmd

    def test_truehd_core_bsf_present(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        track = AudioTrackSettings(stream_index=2, codec="copy",
                                   bitrate_kbps=384, extract_truehd_core=True)
        cmd = _as_single_command(
            self.wf.build_command(_make_config(src, tmp_path / "out.mkv", audio_tracks=[track]))
        )
        assert "truehd_core" in " ".join(cmd)

    def test_truehd_core_bsf_ignored_for_transcoded_audio(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        track = AudioTrackSettings(stream_index=2, codec="eac3",
                                   bitrate_kbps=640, extract_truehd_core=True)
        cmd = _as_single_command(
            self.wf.build_command(_make_config(src, tmp_path / "out.mkv", audio_tracks=[track]))
        )
        assert "truehd_core" not in cmd
        assert "-bsf:a:0" not in cmd
        assert "-c:a:0" in cmd and cmd[cmd.index("-c:a:0") + 1] == "eac3"

    def test_audio_track_order_follows_config_across_sources(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        alt = tmp_path / "alt.mkv"; alt.touch()
        tracks = [
            AudioTrackSettings(stream_index=5, codec="aac", bitrate_kbps=192, source_path=alt),
            AudioTrackSettings(stream_index=1, codec="copy", source_path=src),
        ]
        cmd = _as_single_command(self.wf.build_command(_make_config(src, tmp_path / "out.mkv", audio_tracks=tracks)))

        map_values = [cmd[i + 1] for i, arg in enumerate(cmd[:-1]) if arg == "-map"]
        assert map_values[:3] == ["0:v:0", "1:5", "0:1"]
        assert "-c:a:0" in cmd and cmd[cmd.index("-c:a:0") + 1] == "aac"
        assert "-b:a:0" in cmd and cmd[cmd.index("-b:a:0") + 1] == "192k"
        assert "-c:a:1" in cmd and cmd[cmd.index("-c:a:1") + 1] == "copy"

    def test_subtitle_track_order_follows_config_across_sources(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        alt = tmp_path / "alt.mkv"; alt.touch()
        cmd = _as_single_command(self.wf.build_command(_make_config(
            src,
            tmp_path / "out.mkv",
            copy_subtitles=False,
            subtitle_tracks=[(alt, 7), (src, 4)],
        )))

        map_values = [cmd[i + 1] for i, arg in enumerate(cmd[:-1]) if arg == "-map"]
        assert map_values[:3] == ["0:v:0", "1:7", "0:4"]
        assert "-c:s" in cmd and cmd[cmd.index("-c:s") + 1] == "copy"

    def test_video_track_mapping_uses_selected_stream_index(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        cmd = _as_single_command(self.wf.build_command(_make_config(
            src,
            tmp_path / "out.mkv",
            video=_make_video_settings(codec="copy", stream_index=4, source_path=src),
        )))

        map_values = [cmd[i + 1] for i, arg in enumerate(cmd[:-1]) if arg == "-map"]
        assert map_values[0] == "0:4"

    def test_video_track_mapping_uses_selected_source(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        alt = tmp_path / "alt.mkv"; alt.touch()
        cmd = _as_single_command(self.wf.build_command(_make_config(
            src,
            tmp_path / "out.mkv",
            video=_make_video_settings(codec="copy", stream_index=3, source_path=alt),
        )))

        first_input = cmd.index("-i")
        second_input = cmd.index("-i", first_input + 1)
        map_values = [cmd[i + 1] for i, arg in enumerate(cmd[:-1]) if arg == "-map"]
        assert cmd[first_input + 1] == str(src)
        assert cmd[second_input + 1] == str(alt)
        assert map_values[0] == "1:3"

    def test_vaapi_single_pass_adds_device_and_hwaccel_without_vf(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        with patch.object(EncodeWorkflow, "_vaapi_device", return_value="/dev/dri/renderD128"):
            cmd = self.wf.build_command_single(
                _make_config(src, tmp_path / "out.mkv",
                             video=_make_video_settings(codec="hevc_vaapi"))
            )

        assert "-vaapi_device" in cmd
        assert cmd[cmd.index("-vaapi_device") + 1] == "/dev/dri/renderD128"
        assert cmd.index("-vaapi_device") < cmd.index("-i")
        assert "-hwaccel" in cmd and cmd[cmd.index("-hwaccel") + 1] == "vaapi"
        assert "-hwaccel_output_format" in cmd
        assert cmd[cmd.index("-hwaccel_output_format") + 1] == "vaapi"
        assert "-vf" not in cmd

    def test_vaapi_tonemap_adds_hwupload_vf(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        with patch.object(EncodeWorkflow, "_vaapi_device", return_value="/dev/dri/renderD128"):
            cmd = self.wf.build_command_single(
                _make_config(src, tmp_path / "out.mkv",
                             video=_make_video_settings(codec="hevc_vaapi", tonemap_to_sdr=True))
            )

        assert "-vaapi_device" in cmd
        assert "-vf" in cmd
        assert cmd[cmd.index("-vf") + 1].endswith("format=nv12,hwupload")

    def test_vaapi_two_pass_adds_device_on_both_passes(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        with patch.object(EncodeWorkflow, "_vaapi_device", return_value="/dev/dri/renderD128"):
            cmds = self.wf.build_command(
                _make_config(
                    src,
                    tmp_path / "out.mkv",
                    video=_make_video_settings(codec="h264_vaapi", quality_mode=QualityMode.SIZE),
                    duration_s=3600.0,
                )
            )

        for pass_cmd in cmds:
            assert "-vaapi_device" in pass_cmd
            assert pass_cmd[pass_cmd.index("-vaapi_device") + 1] == "/dev/dri/renderD128"
            assert "-hwaccel" in pass_cmd and pass_cmd[pass_cmd.index("-hwaccel") + 1] == "vaapi"
            assert "-vf" not in pass_cmd

    def test_h264_vaapi_force_8bit_disables_hw_surface_decode_and_adds_upload(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        with patch.object(EncodeWorkflow, "_vaapi_device", return_value="/dev/dri/renderD128"):
            cmd = self.wf.build_command_single(
                _make_config(
                    src,
                    tmp_path / "out.mkv",
                    video=_make_video_settings(codec="h264_vaapi", force_8bit=True),
                )
            )

        assert "-vaapi_device" in cmd
        assert "-hwaccel_output_format" not in cmd
        assert "-vf" in cmd
        assert cmd[cmd.index("-vf") + 1].endswith("format=nv12,hwupload")
        assert "-pix_fmt" in cmd
        assert cmd[cmd.index("-pix_fmt") + 1] == "nv12"

    def test_h264_nvenc_force_8bit_disables_hw_decode_and_sets_nv12(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        cmd = self.wf.build_command_single(
            _make_config(
                src,
                tmp_path / "out.mkv",
                video=_make_video_settings(codec="h264_nvenc", force_8bit=True),
            )
        )

        assert "-hwaccel_output_format" not in cmd
        assert "-pix_fmt" in cmd
        assert cmd[cmd.index("-pix_fmt") + 1] == "nv12"

    def test_qsv_single_pass_adds_explicit_device_on_linux(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        with patch.object(EncodeWorkflow, "_qsv_device", return_value="/dev/dri/renderD129"):
            cmd = self.wf.build_command_single(
                _make_config(
                    src,
                    tmp_path / "out.mkv",
                    video=_make_video_settings(codec="hevc_qsv"),
                )
            )

        assert "-qsv_device" in cmd
        assert cmd[cmd.index("-qsv_device") + 1] == "/dev/dri/renderD129"
        assert cmd.index("-qsv_device") < cmd.index("-i")
        assert "-hwaccel" in cmd and cmd[cmd.index("-hwaccel") + 1] == "qsv"
        assert "-hwaccel_output_format" in cmd
        assert cmd[cmd.index("-hwaccel_output_format") + 1] == "qsv"

    def test_qsv_without_resolved_device_keeps_legacy_hwaccel_flags(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        with patch.object(EncodeWorkflow, "_qsv_device", return_value=None):
            cmd = self.wf.build_command_single(
                _make_config(
                    src,
                    tmp_path / "out.mkv",
                    video=_make_video_settings(codec="h264_qsv"),
                )
            )

        assert "-qsv_device" not in cmd
        assert "-hwaccel" in cmd and cmd[cmd.index("-hwaccel") + 1] == "qsv"

    def test_qsv_windows_single_pass_adds_explicit_adapter_index(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        with patch("core.workflows.encode.workflow.sys.platform", "win32"), \
             patch.object(EncodeWorkflow, "_qsv_device", return_value="1"):
            cmd = self.wf.build_command_single(
                _make_config(
                    src,
                    tmp_path / "out.mkv",
                    video=_make_video_settings(codec="hevc_qsv"),
                )
            )

        assert "-qsv_device" in cmd
        assert cmd[cmd.index("-qsv_device") + 1] == "1"
        assert "-hwaccel" in cmd and cmd[cmd.index("-hwaccel") + 1] == "qsv"

    def test_amf_windows_single_pass_adds_named_d3d11_device(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        with patch("core.workflows.encode.workflow.sys.platform", "win32"), \
             patch.object(EncodeWorkflow, "_amf_device", return_value="2"):
            cmd = self.wf.build_command_single(
                _make_config(
                    src,
                    tmp_path / "out.mkv",
                    video=_make_video_settings(codec="h264_amf"),
                )
            )

        assert "-init_hw_device" in cmd
        assert cmd[cmd.index("-init_hw_device") + 1] == "d3d11va=mre_amf:2"
        assert "-filter_hw_device" in cmd
        assert cmd[cmd.index("-filter_hw_device") + 1] == "mre_amf"
        assert "-hwaccel_device" in cmd
        assert cmd[cmd.index("-hwaccel_device") + 1] == "mre_amf"

    def test_amf_windows_tonemap_adds_hwupload_to_selected_device(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        with patch("core.workflows.encode.workflow.sys.platform", "win32"), \
             patch.object(EncodeWorkflow, "_amf_device", return_value="2"):
            cmd = self.wf.build_command_single(
                _make_config(
                    src,
                    tmp_path / "out.mkv",
                    video=_make_video_settings(codec="hevc_amf", tonemap_to_sdr=True),
                )
            )

        assert "-init_hw_device" in cmd
        assert "-vf" in cmd
        assert cmd[cmd.index("-vf") + 1].endswith("format=nv12,hwupload")
        assert "-hwaccel_output_format" not in cmd

    def test_nvenc_windows_single_pass_adds_explicit_gpu_index(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        with patch("core.workflows.encode.workflow.sys.platform", "win32"), \
             patch.object(EncodeWorkflow, "_nvenc_device", return_value="1"):
            cmd = self.wf.build_command_single(
                _make_config(
                    src,
                    tmp_path / "out.mkv",
                    video=_make_video_settings(codec="h264_nvenc"),
                )
            )

        assert "-hwaccel_device" in cmd
        assert cmd[cmd.index("-hwaccel_device") + 1] == "1"
        assert "-gpu" in cmd
        assert cmd[cmd.index("-gpu") + 1] == "1"

    def test_libx264_force_8bit_sets_yuv420p(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        cmd = self.wf.build_command_single(
            _make_config(
                src,
                tmp_path / "out.mkv",
                video=_make_video_settings(codec="libx264", force_8bit=True),
            )
        )

        assert "-pix_fmt" in cmd
        assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"

    def test_non_vaapi_codec_does_not_receive_vaapi_args(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        with patch.object(EncodeWorkflow, "_vaapi_device", return_value="/dev/dri/renderD128"):
            cmd = self.wf.build_command_single(
                _make_config(
                    src,
                    tmp_path / "out.mkv",
                    video=_make_video_settings(codec="libx265", tonemap_to_sdr=True),
                )
            )

        assert "-vaapi_device" not in cmd
        assert "-vf" in cmd
        assert "hwupload" not in cmd[cmd.index("-vf") + 1]

    def test_ffmpeg_vf_orders_geometry_filters_resize_and_tonemap(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        cmd = self.wf.build_command_single(
            _make_config(
                src,
                tmp_path / "out.mkv",
                video=_make_video_settings(
                    filters=VideoFilterSettings(
                        yadif_enabled=True,
                        deblock_enabled=True,
                        nlmeans_enabled=True,
                        chroma_smooth_enabled=True,
                    ),
                    crop=VideoCropSettings(enabled=True, left=8, right=8, top=4, bottom=4),
                    resize=VideoResizeSettings(
                        enabled=True,
                        mode="preset",
                        preset="720p",
                        allow_upscale=False,
                    ),
                    tonemap_to_sdr=True,
                ),
            )
        )

        vf = cmd[cmd.index("-vf") + 1]
        expected_order = [
            "yadif=",
            "crop=",
            "deblock=",
            "nlmeans=",
            "chromanr=",
            "scale=",
            "zscale=transfer=linear",
        ]
        positions = [vf.index(token) for token in expected_order]
        assert positions == sorted(positions)
        # Virgules échappées : ffmpeg sépare les filtres sur ',' dans le filtergraph.
        assert "min(1280\\,iw):min(720\\,ih)" in vf

    def test_ffmpeg_percent_resize_clamps_when_upscale_is_disabled(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        cmd = self.wf.build_command_single(
            _make_config(
                src,
                tmp_path / "out.mkv",
                video=_make_video_settings(
                    resize=VideoResizeSettings(
                        enabled=True,
                        mode="percent",
                        percent=150,
                        allow_upscale=False,
                    ),
                ),
            )
        )

        vf = cmd[cmd.index("-vf") + 1]
        assert "iw*100/100" in vf
        assert "iw*150/100" not in vf


# ===========================================================================
# validate
# ===========================================================================

class TestValidate:

    def setup_method(self):
        self.wf = _make_workflow()

    def test_missing_source(self, tmp_path):
        config = _make_config(tmp_path / "absent.mkv", tmp_path / "out.mkv")
        errors = self.wf.validate(config)
        assert any("introuvable" in e.lower() or "source" in e.lower() for e in errors)

    def test_source_equals_output(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        errors = self.wf.validate(_make_config(src, src))
        assert len(errors) >= 1

    def test_output_dir_not_writable(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        config = _make_config(src, tmp_path / "out.mkv")
        with patch(
            "core.workflows.encode.workflow.tempfile.NamedTemporaryFile",
            side_effect=OSError("blocked"),
        ):
            errors = self.wf.validate(config)
        assert any("inscriptible" in e.lower() for e in errors)

    def test_size_mode_without_duration(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        config = _make_config(src, tmp_path / "out.mkv",
                              video=_make_video_settings(quality_mode=QualityMode.SIZE),
                              duration_s=None)
        errors = self.wf.validate(config)
        assert any("durée" in e.lower() or "taille" in e.lower() for e in errors)

    def test_copy_size_mode_without_duration_is_allowed(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        config = _make_config(
            src,
            tmp_path / "out.mkv",
            video=_make_video_settings(codec="copy", quality_mode=QualityMode.SIZE),
            duration_s=None,
        )
        errors = self.wf.validate(config)
        assert not any("durée" in e.lower() or "taille" in e.lower() for e in errors)

    def test_copy_with_source_hdr_passthrough_is_allowed(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        config = _make_config(
            src,
            tmp_path / "out.mkv",
            video=_make_video_settings(
                codec="copy",
                inject_hdr_meta=True,
                master_display="G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)",
                max_cll="1000,400",
            ),
        )
        errors = self.wf.validate(config)
        assert not any("codec copy incompatible" in e.lower() for e in errors)

    def test_valid_config_no_errors(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        assert self.wf.validate(_make_config(src, tmp_path / "out.mkv")) == []

    def test_copy_with_tonemap_is_rejected(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        config = _make_config(
            src,
            tmp_path / "out.mkv",
            video=_make_video_settings(codec="copy", tonemap_to_sdr=True),
        )
        errors = self.wf.validate(config)
        assert any("codec copy incompatible avec les transformations vidéo" in e.lower() for e in errors)

    def test_copy_with_geometry_or_filters_is_rejected(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        config = _make_config(
            src,
            tmp_path / "out.mkv",
            video=_make_video_settings(
                codec="copy",
                resize=VideoResizeSettings(enabled=True, mode="preset", preset="720p"),
                filters=VideoFilterSettings(deblock_enabled=True),
            ),
        )
        errors = self.wf.validate(config)
        assert any("codec copy incompatible avec les transformations vidéo" in e.lower() for e in errors)

    def test_invalid_master_display(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        config = _make_config(src, tmp_path / "out.mkv",
                              video=_make_video_settings(inject_hdr_meta=True,
                                                         master_display="INVALID"))
        assert any("master_display" in e.lower() or "format" in e.lower()
                   for e in self.wf.validate(config))

    def test_invalid_max_cll(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        config = _make_config(src, tmp_path / "out.mkv",
                              video=_make_video_settings(inject_hdr_meta=True,
                                                         max_cll="INVALID"))
        assert any("cll" in e.lower() for e in self.wf.validate(config))

    def test_validate_rejects_negative_video_track_offset(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        config = _make_config(
            src,
            tmp_path / "out.mkv",
            track_time_offsets=[
                TrackTimeOffset(track_type="video", source_path=src, stream_index=0, offset_ms=-50)
            ],
        )
        errors = self.wf.validate(config)
        assert any("vidéo" in e.lower() and "négatif" in e.lower() for e in errors)


# ===========================================================================
# ProfileManager
# ===========================================================================

class TestProfileManager:

    def test_save_and_load_roundtrip(self, tmp_path):
        pm = ProfileManager(tmp_path)
        pm.save(EncodePreset(name="Test CRF18", codec="libx265", crf=18))
        loaded = pm.load_all()
        assert len(loaded) == 1
        assert loaded[0].name == "Test CRF18" and loaded[0].crf == 18

    def test_delete_removes_profile(self, tmp_path):
        pm = ProfileManager(tmp_path)
        pm.save(EncodePreset(name="ToDelete"))
        pm.delete("ToDelete")
        assert pm.load_all() == []

    def test_names_returns_all_names(self, tmp_path):
        pm = ProfileManager(tmp_path)
        pm.save(EncodePreset(name="Alpha"))
        pm.save(EncodePreset(name="Beta"))
        assert set(pm.names()) == {"Alpha", "Beta"}

    def test_load_all_empty_initially(self, tmp_path):
        assert ProfileManager(tmp_path).load_all() == []

    def test_save_overwrites_existing(self, tmp_path):
        pm = ProfileManager(tmp_path)
        pm.save(EncodePreset(name="P", crf=18))
        pm.save(EncodePreset(name="P", crf=28))
        loaded = pm.load_all()
        assert len(loaded) == 1 and loaded[0].crf == 28


# ===========================================================================
# _hdr_meta_args — comportement par codec
# ===========================================================================

class TestHdrMetaArgs:
    """Vérifie que _hdr_meta_args génère les bons flags selon le codec."""

    def setup_method(self):
        self.wf = _make_workflow()

    def _vs(self, codec: str, master: str = "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,50)",
            max_cll: str = "343,203") -> VideoEncodeSettings:
        return _make_video_settings(codec=codec, inject_hdr_meta=True,
                                    master_display=master, max_cll=max_cll)

    def test_libx265_color_flags_only_no_standalone_master_display(self):
        """libx265 : couleur seulement — master_display/max_cll via -x265-params."""
        args = _hdr_meta_args(self._vs("libx265"))
        assert "-color_primaries" in args
        assert "-master_display" not in args
        assert "-max_cll" not in args

    def test_libx265_x265_params_contains_master_display(self, tmp_path):
        """libx265 : -x265-params fusionné avec master-display et max-cll."""
        src = tmp_path / "s.mkv"; src.touch()
        vs = self._vs("libx265")
        config = _make_config(src, tmp_path / "o.mkv", video=vs)
        cmd = self.wf.build_command_single(config)
        idx = cmd.index("-x265-params")
        params = cmd[idx + 1]
        assert "master-display=" in params
        assert "max-cll=" in params

    def test_libx265_x265_params_merges_extra_params(self, tmp_path):
        """libx265 : extra_params + HDR meta fusionnés dans un seul -x265-params."""
        src = tmp_path / "s.mkv"; src.touch()
        vs = _make_video_settings(codec="libx265", inject_hdr_meta=True,
                                   master_display="G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,50)",
                                   max_cll="343,203", extra_params="hdr-opt=1:repeat-headers=1")
        config = _make_config(src, tmp_path / "o.mkv", video=vs)
        cmd = self.wf.build_command_single(config)
        # Un seul -x265-params dans la commande
        assert cmd.count("-x265-params") == 1
        params = cmd[cmd.index("-x265-params") + 1]
        assert "hdr-opt=1" in params
        assert "master-display=" in params
        assert "max-cll=" in params

    def test_hevc_nvenc_color_flags_only(self):
        """hevc_nvenc : couleur/VUI seulement, pas de faux flags master_display/max_cll."""
        args = _hdr_meta_args(self._vs("hevc_nvenc"))
        assert "-color_primaries" in args
        assert "-master_display" not in args
        assert "-max_cll" not in args

    def test_hevc_nvenc_no_master_display_when_empty(self):
        """hevc_nvenc : meme avec des champs vides, on garde seulement les flags couleur."""
        vs = _make_video_settings(codec="hevc_nvenc", inject_hdr_meta=True,
                                   master_display="", max_cll="")
        args = _hdr_meta_args(vs)
        assert "-master_display" not in args
        assert "-max_cll" not in args
        assert "-color_primaries" in args

    def test_hevc_amf_color_flags_only(self):
        """hevc_amf : couleur seulement, pas de master_display/max_cll."""
        args = _hdr_meta_args(self._vs("hevc_amf"))
        assert "-color_primaries" in args
        assert "-master_display" not in args
        assert "-max_cll" not in args

    def test_hevc_qsv_color_flags_only(self):
        """hevc_qsv : couleur seulement."""
        args = _hdr_meta_args(self._vs("hevc_qsv"))
        assert "-color_primaries" in args
        assert "-master_display" not in args

    def test_libsvtav1_color_flags_only(self):
        """libsvtav1 : couleur seulement, pas de master_display/max_cll."""
        args = _hdr_meta_args(self._vs("libsvtav1"))
        assert "-color_primaries" in args
        assert "-master_display" not in args

    def test_copy_no_color_flags(self):
        """copy : aucun flag de couleur — pas pertinent pour un stream copié."""
        args = _hdr_meta_args(self._vs("copy"))
        assert args == []

    def test_h264_codecs_no_flags(self):
        """h264_* et libx264 : aucun flag HDR — H.264 est SDR."""
        for codec in ("libx264", "h264_nvenc", "h264_amf", "h264_qsv"):
            args = _hdr_meta_args(self._vs(codec))
            assert args == [], f"{codec} devrait retourner [] mais retourne {args}"


class TestNvencSafePreset:
    def test_hevc_nvenc_safe_preset_is_dormant_but_maps_to_valid_native_preset(self, tmp_path):
        src = tmp_path / "s.mkv"
        src.touch()
        wf = _make_workflow()
        vs = _make_video_settings(codec="hevc_nvenc", preset="safe", force_10bit=True)
        cmd = wf.build_command_single(_make_config(src, tmp_path / "o.mkv", video=vs))

        assert "-preset:v" in cmd
        assert cmd[cmd.index("-preset:v") + 1] == "p5"
        assert "-b_ref_mode" not in cmd
        assert "-nonref_p" not in cmd
        assert "-rc-lookahead" not in cmd
        assert "-spatial-aq" not in cmd
        assert "-temporal-aq" not in cmd
        assert "-strict_gop" not in cmd
        assert "-g" not in cmd
        assert "-forced-idr" not in cmd

    def test_hevc_nvenc_safe_preset_keeps_user_extra_params(self, tmp_path):
        src = tmp_path / "s.mkv"
        src.touch()
        wf = _make_workflow()
        vs = _make_video_settings(
            codec="hevc_nvenc",
            preset="safe",
            force_10bit=True,
            extra_params="-rc-lookahead 20 -spatial-aq 1",
        )
        cmd = wf.build_command_single(_make_config(src, tmp_path / "o.mkv", video=vs))

        rc_positions = [i for i, token in enumerate(cmd) if token == "-rc-lookahead"]
        assert len(rc_positions) == 1
        assert cmd[rc_positions[0] + 1] == "20"
        aq_positions = [i for i, token in enumerate(cmd) if token == "-spatial-aq"]
        assert len(aq_positions) == 1
        assert cmd[aq_positions[0] + 1] == "1"


# ===========================================================================
# _run_with_metadata_inject — optimisation codec copy
# ===========================================================================

class TestMetadataInjectCopyCodec:
    """
    Vérifie que _run_with_metadata_inject fonctionne correctement quand il est
    appelé directement avec codec=copy (cas rare — en usage normal, run() dévie
    vers le chemin standard avant d'appeler cette méthode).

    Depuis la refactorisation copy :
      - aucune extraction source.hevc n'est créée
      - enc.hevc est produit directement par l'étape vidéo
      - test_copy_inject_completes_and_produces_output reste valide
    """

    def _run_inject_copy(self, tmp_path: Path, copy_dv: bool, copy_hdr10plus: bool):
        """Lance le workflow copy+inject et retourne (cmds_run, snapshots)."""
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        config = _make_config(
            source=src, output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="copy"),
            copy_dv=copy_dv, copy_hdr10plus=copy_hdr10plus,
            work_dir=tmp_path / "work",
        )
        wf = _make_workflow(enabled=False)
        cmds_run: list[list[str]] = []
        snapshots: list[list[str]] = []

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            cmds_run.append(list(cmd))
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 100_000)
                    break
            work = tmp_path / "work"
            if work.exists():
                names = [p.name for p in work.rglob("*")
                         if p.is_file() and p.suffix in (".hevc", ".mkv")]
                snapshots.append(names[:])
            return ""

        with patch.object(EncodeWorkflow, "_shm_path",
                          side_effect=lambda tmp_dir, name, _size: tmp_dir / name):
            with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
                sigs = wf._run_with_metadata_inject(config)
                _collect_signals(sigs)

        return cmds_run, snapshots

    def test_no_src_hevc_temp_created_for_copy(self, tmp_path):
        """Aucun src.hevc temporaire n'est créé dans le chemin codec=copy."""
        _, snapshots = self._run_inject_copy(tmp_path, copy_dv=True, copy_hdr10plus=False)
        assert all("src.hevc" not in snap for snap in snapshots)

    def test_enc_hevc_created_by_direct_encode_not_extraction(self, tmp_path):
        """
        enc.hevc est créé DIRECTEMENT par la commande d'encodage vidéo (-f hevc),
        pas par extraction depuis un encoded.mkv (qui n'existe plus).
        La commande 'ffmpeg -i encoded.mkv ... enc.hevc' ne doit pas apparaître.
        """
        cmds, _ = self._run_inject_copy(tmp_path, copy_dv=True, copy_hdr10plus=False)
        # Aucune commande ffmpeg avec encoded.mkv en entrée
        extraction_from_mkv = [
            c for c in cmds
            if "encoded.mkv" in " ".join(c)
            and any("encoded.mkv" in c[i+1] for i, a in enumerate(c[:-1]) if a == "-i")
        ]
        assert len(extraction_from_mkv) == 0, \
            f"Commande d'extraction depuis encoded.mkv trouvée (ne devrait pas exister) : {extraction_from_mkv}"
        # enc.hevc est créé par une commande ffmpeg avec -f hevc en sortie
        direct_encode_cmds = [
            c for c in cmds
            if "-f" in c and "hevc" in c and "enc.hevc" in " ".join(c)
            and not any("encoded.mkv" in c[i+1] for i, a in enumerate(c[:-1]) if a == "-i")
        ]
        assert len(direct_encode_cmds) >= 1, \
            "enc.hevc non créé par encodage direct (-f hevc)"

    def test_dv_extract_reads_source_mkv_directly(self, tmp_path):
        """L'extraction RPU lit directement source.mkv, sans src.hevc."""
        cmds, _ = self._run_inject_copy(tmp_path, copy_dv=True, copy_hdr10plus=False)
        extract_cmds = [c for c in cmds if c[:2] == ["dovi_tool", "extract-rpu"]]
        assert len(extract_cmds) == 1
        cmd = extract_cmds[0]
        idx = cmd.index("-i")
        assert cmd[idx + 1].endswith("source.mkv")

    def test_hdr10plus_extract_reads_source_mkv_directly(self, tmp_path):
        """L'extraction HDR10+ lit directement source.mkv, sans src.hevc."""
        cmds, _ = self._run_inject_copy(tmp_path, copy_dv=False, copy_hdr10plus=True)
        extract_cmds = [c for c in cmds if c[:2] == ["hdr10plus_tool", "extract"]]
        assert len(extract_cmds) == 1
        cmd = extract_cmds[0]
        assert cmd[2].endswith("source.mkv")

    @pytest.mark.parametrize("copy_dv,copy_hdr10plus", [
        (True, False), (False, True), (True, True)
    ])
    def test_copy_inject_completes_and_produces_output(self, tmp_path,
                                                        copy_dv, copy_hdr10plus):
        """Le workflow se termine et la commande ffmpeg de reconstitution finale est émise."""
        cmds, _ = self._run_inject_copy(tmp_path, copy_dv, copy_hdr10plus)
        # La reconstitution finale est une commande ffmpeg avec 2 inputs et output.mkv
        recon_cmds = [
            c for c in cmds
            if c[0] == "ffmpeg" and "output.mkv" in " ".join(c)
        ]
        assert len(recon_cmds) == 1, \
            f"Commande de reconstitution ffmpeg absente ou dupliquée. Cmds : {[c[0] for c in cmds]}"
        assert "output.mkv" in " ".join(recon_cmds[0])

    def test_copy_dv_injects_rpu_into_enc_hevc(self, tmp_path):
        """
        Depuis la refactorisation, dovi_tool inject-rpu opère sur enc.hevc
        (ou enc_hdr10p.hevc si HDR10+ a déjà été injecté).
        """
        cmds, _ = self._run_inject_copy(tmp_path, copy_dv=True, copy_hdr10plus=False)
        dv_injects = [c for c in cmds if "inject-rpu" in c]
        assert len(dv_injects) == 1
        # Le fichier d'entrée de inject-rpu doit être enc.hevc (ou enc_hdr10p.hevc)
        inj_cmd = dv_injects[0]
        i_flag = inj_cmd.index("-i")
        input_file = inj_cmd[i_flag + 1]
        assert "enc.hevc" in input_file or "enc_hdr10p.hevc" in input_file, \
            f"inject-rpu n'opère pas sur enc.hevc : {input_file}"

    def test_copy_dv_p8_normalization_reinjects_estimated_static_hdr(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        config = _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(
                codec="copy",
                copy_dv=True,
                dovi_profile="2",
                inject_hdr_meta=True,
                master_display="G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)",
                max_cll="900,300",
                static_hdr_metadata_source="estimated_p5_to_p8",
                static_hdr_metadata_confidence="medium",
                static_hdr_metadata_analysis_mode="precise",
            ),
            work_dir=tmp_path / "work",
        )
        wf = _make_workflow(enabled=False)
        calls: list[tuple[Path, Path, str, str]] = []
        logs: list[tuple[str, str]] = []
        wf.log_message.connect(lambda level, message: logs.append((str(level), str(message))))

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 100_000)
                    break
            return ""

        def _fake_static_patch(src_path, dst_path, *, master_display, max_cll):
            calls.append((src_path, dst_path, master_display, max_cll))
            dst_path.write_bytes(b"patched")
            from core.workflows.hevc_static_hdr_metadata import StaticHdrSeiInjectionResult
            return StaticHdrSeiInjectionResult(
                access_units=10,
                targeted_access_units=2,
                injected_access_units=2,
                preserved_access_units=0,
            )

        with patch.object(EncodeWorkflow, "_shm_path", side_effect=lambda tmp_dir, name, _size: tmp_dir / name):
            with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
                with patch(
                    "core.workflows.encode.runtime.metadata_inject.inject_static_hdr_sei_file",
                    side_effect=_fake_static_patch,
                ):
                    sigs = wf._run_with_metadata_inject(config)
                    _collect_signals(sigs)

        assert len(calls) == 1
        assert calls[0][2] == config.video.master_display
        assert calls[0][3] == "900,300"
        assert any(
            level == "WARN" and "estimée" in message and "mode precise" in message
            for level, message in logs
        )

    def test_quota_error_is_reported_with_actionable_workdir_message(self, tmp_path):
        message = _friendly_dovi_conversion_error(
            RuntimeError("Error: Quota exceeded (os error 122)"),
            tmp_path,
        )

        assert "quota ou espace temporaire insuffisant" in message
        assert str(tmp_path) in message
        assert "Libérez de l’espace" in message

    def test_p5_to_p8_reencodes_bl_with_libplacebo_and_analyzes_final_bl(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        video = _make_video_settings(
            codec="copy",
            copy_dv=True,
            dovi_profile="2",
            inject_hdr_meta=True,
            track_entry_id="video-p5",
            static_hdr_metadata_analysis_request="precise",
        )
        config = _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=video,
            video_tracks=[video],
            work_dir=tmp_path / "work",
        )
        wf = _make_workflow(enabled=False)
        commands: list[list[str]] = []
        ready: list[tuple[str, object]] = []
        wf.static_hdr_estimate_ready.connect(
            lambda entry_id, estimate: ready.append((entry_id, estimate))
        )
        estimate = StaticHdrEstimate(
            master_display="G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)",
            max_cll="900,300",
            confidence="medium",
            sample_count=80,
            coverage=0.8,
            mode="precise",
            analysis_method="linear_grayf32le",
            active_sample_count=80,
        )

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            commands.append([str(part) for part in cmd])
            for arg in reversed(cmd):
                path = Path(str(arg))
                if path.suffix.lower() in {".hevc", ".mkv", ".bin", ".json"}:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"\x00" * 100_000)
                    break
            return ""

        def _fake_static_patch(src_path, dst_path, *, master_display, max_cll):
            dst_path.write_bytes(b"patched")
            from core.workflows.hevc_static_hdr_metadata import StaticHdrSeiInjectionResult
            return StaticHdrSeiInjectionResult(
                access_units=10,
                targeted_access_units=2,
                injected_access_units=2,
                preserved_access_units=0,
            )

        decision = P7RoutingDecision(
            conversion_needed=True,
            sub_profile=DoviSubProfile.P5,
            convert_mode="3",
            reason="Source P5 détectée.",
        )
        with patch(
            "core.workflows.encode.runtime.metadata_inject.DoviP7Router.analyze",
            return_value=decision,
        ):
            with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
                with patch.object(
                    wf._static_hdr_estimator,
                    "estimate_converted_static_hdr",
                    return_value=estimate,
                ) as estimate_mock:
                    with patch(
                        "core.workflows.encode.runtime.metadata_inject.inject_static_hdr_sei_file",
                        side_effect=_fake_static_patch,
                    ):
                        signals = wf._run_with_metadata_inject(config)
                        _collect_signals(signals)

        convert_commands = [cmd for cmd in commands if "convert" in cmd]
        encode_commands = [cmd for cmd in commands if "libplacebo=" in " ".join(cmd)]
        assert len(convert_commands) == 1
        assert len(encode_commands) == 1
        encode_cmd = encode_commands[0]
        assert ["-init_hw_device", "vulkan=mre_dovi"] == encode_cmd[
            encode_cmd.index("-init_hw_device"):encode_cmd.index("-init_hw_device") + 2
        ]
        assert "libx265" in encode_cmd
        assert "apply_dolbyvision=true" in encode_cmd[encode_cmd.index("-vf") + 1]
        assert Path(estimate_mock.call_args.args[0]).name == "enc.hevc"
        assert ready == [("video-p5", estimate)]


# ===========================================================================
# Codec COPY — passthrough métadonnées dans les commandes FFmpeg
# ===========================================================================

class TestCopyCodecMetadataPassthrough:
    """
    Vérifie que -map_metadata 0 et -map_metadata:s:v:0 0:s:v:0 sont injectés
    dans les commandes FFmpeg quand et seulement quand codec=copy.

    Justification :
      -map_metadata 0            : préserve titre, chapitres, attachments container.
      -map_metadata:s:v:0 0:s:v:0 : préserve color primaries, mastering display,
                                    content light level, etc. au niveau du stream.
      Avec -c:v copy les NAL SEI (RPU DoVi, HDR10+) sont déjà dans le bitstream.
    """

    def setup_method(self):
        self.wf = _make_workflow()

    def _cmd_copy(self, tmp_path, **video_kw) -> list[str]:
        src = tmp_path / "src.mkv"; src.touch()
        vs = _make_video_settings(codec="copy", **video_kw)
        return self.wf.build_command_single(
            _make_config(src, tmp_path / "out.mkv", video=vs)
        )

    def _cmd_x265(self, tmp_path) -> list[str]:
        src = tmp_path / "src.mkv"; src.touch()
        return self.wf.build_command_single(
            _make_config(src, tmp_path / "out.mkv",
                         video=_make_video_settings(codec="libx265"))
        )

    # ── single pass ──────────────────────────────────────────────────────────

    def test_copy_single_pass_has_map_metadata_global(self, tmp_path):
        """-map_metadata 0 présent dans la commande single pass (copy)."""
        cmd = self._cmd_copy(tmp_path)
        assert "-map_metadata" in cmd
        idx = cmd.index("-map_metadata")
        assert cmd[idx + 1] == "0"

    def test_copy_single_pass_has_map_metadata_stream(self, tmp_path):
        """-map_metadata:s:v:0 0:s:v:0 présent dans la commande single pass (copy)."""
        cmd = self._cmd_copy(tmp_path)
        assert "-map_metadata:s:v:0" in cmd
        idx = cmd.index("-map_metadata:s:v:0")
        assert cmd[idx + 1] == "0:s:v:0"

    def test_copy_single_pass_metadata_before_output(self, tmp_path):
        """-map_metadata apparaît avant le chemin de sortie."""
        src = tmp_path / "src.mkv"; src.touch()
        out = tmp_path / "out.mkv"
        vs = _make_video_settings(codec="copy")
        cmd = self.wf.build_command_single(
            _make_config(src, out, video=vs)
        )
        assert cmd.index("-map_metadata") < cmd.index(str(out))

    def test_non_copy_single_pass_no_map_metadata(self, tmp_path):
        """-map_metadata absent pour les codecs de réencodage (libx265)."""
        cmd = self._cmd_x265(tmp_path)
        assert "-map_metadata" not in cmd

    @pytest.mark.parametrize("codec", ["libx264", "libsvtav1", "hevc_nvenc", "hevc_amf"])
    def test_non_copy_codecs_no_map_metadata(self, tmp_path, codec):
        """-map_metadata absent pour tout codec de réencodage."""
        src = tmp_path / "src.mkv"; src.touch()
        cmd = self.wf.build_command_single(
            _make_config(src, tmp_path / "out.mkv",
                         video=_make_video_settings(codec=codec))
        )
        assert "-map_metadata" not in cmd

    # ── mode SIZE ─────────────────────────────────────────────────────────────

    def test_copy_size_mode_forces_single_pass_with_map_metadata(self, tmp_path):
        """codec=copy en mode SIZE reste en single-pass (pas de 2-pass)."""
        src = tmp_path / "src.mkv"; src.touch()
        vs = _make_video_settings(codec="copy", quality_mode=QualityMode.SIZE)
        cmd = _as_single_command(
            self.wf.build_command(_make_config(src, tmp_path / "out.mkv", video=vs, duration_s=3600.0))
        )
        assert isinstance(cmd[0], str), "codec=copy doit retourner list[str] (single-pass)"
        assert "-pass" not in cmd
        assert "-map_metadata" in cmd
        idx = cmd.index("-map_metadata")
        assert cmd[idx + 1] == "0"

    def test_non_copy_two_pass_no_map_metadata(self, tmp_path):
        """Passe 2 sans -map_metadata pour codec de réencodage en mode SIZE."""
        src = tmp_path / "src.mkv"; src.touch()
        vs = _make_video_settings(codec="libx265", quality_mode=QualityMode.SIZE)
        cmds = self.wf.build_command(
            _make_config(src, tmp_path / "out.mkv", video=vs, duration_s=3600.0)
        )
        for pass_cmd in cmds:
            assert "-map_metadata" not in pass_cmd

    # ── interaction avec d'autres flags ──────────────────────────────────────

    def test_copy_with_audio_tracks_metadata_still_present(self, tmp_path):
        """-map_metadata présent même avec des pistes audio configurées."""
        src = tmp_path / "src.mkv"; src.touch()
        audio = [AudioTrackSettings(stream_index=1, codec="copy")]
        vs = _make_video_settings(codec="copy")
        cmd = self.wf.build_command_single(
            _make_config(src, tmp_path / "out.mkv", video=vs, audio_tracks=audio)
        )
        assert "-map_metadata" in cmd

    def test_copy_with_subtitles_metadata_still_present(self, tmp_path):
        """-map_metadata présent même avec copy_subtitles=True."""
        src = tmp_path / "src.mkv"; src.touch()
        vs = _make_video_settings(codec="copy")
        cmd = self.wf.build_command_single(
            _make_config(src, tmp_path / "out.mkv", video=vs, copy_subtitles=True)
        )
        assert "-map_metadata" in cmd


# ===========================================================================
# run() — codec COPY dévie vers chemin standard (pas _run_with_metadata_inject)
# ===========================================================================

class TestRunCopyBypassesInject:
    """
    Vérifie que run() n'appelle PAS _run_with_metadata_inject quand codec=copy,
    sauf si une normalisation DoVi P8.1 est explicitement demandée.

    Comportement attendu :
      - codec=copy + copy_dv=True + profil source → chemin standard FFmpeg (passthrough)
      - codec=copy + copy_dv=True + dovi_profile=2 → _run_with_metadata_inject
      - codec=copy + copy_hdr10plus=True → idem
      - codec=libx265 + copy_dv=True → _run_with_metadata_inject (inchangé)
      - log_message("INFO", "...passthrough...") émis quand copy est court-circuité
    """

    def _make_copy_config(
        self,
        tmp_path,
        copy_dv=False,
        copy_hdr10plus=False,
        dovi_profile="0",
    ) -> EncodeConfig:
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1000)
        return _make_config(
            source=src, output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="copy"),
            copy_dv=copy_dv, copy_hdr10plus=copy_hdr10plus,
            dovi_profile=dovi_profile,
        )

    @pytest.mark.parametrize("copy_dv,copy_hdr10plus", [
        (True, False), (False, True), (True, True)
    ])
    def test_copy_codec_does_not_call_metadata_inject(self, tmp_path, copy_dv, copy_hdr10plus):
        """run() avec codec=copy + profil source ne doit pas appeler _run_with_metadata_inject."""
        config = self._make_copy_config(tmp_path, copy_dv=copy_dv,
                                        copy_hdr10plus=copy_hdr10plus)
        wf = _make_workflow()
        inject_called = [False]

        original = wf._run_with_metadata_inject

        def _spy(cfg, **_kwargs):
            inject_called[0] = True
            return original(cfg)

        with patch.object(wf, "_run_with_metadata_inject", side_effect=_spy), \
             patch.object(wf._runner, "run") as mock_run:
            mock_run.return_value = MagicMock()
            wf.run(config)

        assert not inject_called[0], \
            "codec=copy + profil source ne doit pas passer par _run_with_metadata_inject"

    def test_copy_codec_skips_dynamic_hdr_detection(self, tmp_path):
        """run() avec codec=copy ne doit pas lancer la détection ffprobe/mediainfo."""
        config = self._make_copy_config(tmp_path, copy_dv=True, copy_hdr10plus=True)
        wf = _make_workflow()
        detection_called = {"value": False}

        def _fake_detect(_source):
            detection_called["value"] = True
            return (True, True)

        with patch.object(wf, "_detect_source_dynamic_hdr_presence", side_effect=_fake_detect), \
             patch.object(wf._runner, "run") as mock_run:
            mock_run.return_value = MagicMock()
            wf.run(config)

        assert detection_called["value"] is False, \
            "La détection HDR dynamique ne doit pas être appelée en codec=copy."

    def test_copy_codec_with_dovi_normalization_calls_metadata_inject(self, tmp_path):
        """copy + normalisation DoVi P8.1 doit passer par le pipeline d'injection."""
        config = self._make_copy_config(tmp_path, copy_dv=True, dovi_profile="2")
        wf = _make_workflow()

        with patch.object(wf, "_detect_source_dynamic_hdr_presence", return_value=(True, False)) as detect_mock, \
             patch.object(wf, "_run_with_metadata_inject", return_value=MagicMock()) as inject_mock, \
             patch.object(wf._runner, "run", return_value=MagicMock()) as direct_mock:
            wf.run(config)

        assert detect_mock.called
        assert inject_mock.called, "copy + dovi_profile=2 doit lancer l'injection metadata"
        assert not direct_mock.called

    def test_scheduled_analysis_defers_generic_static_hdr_fallback(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1000)
        video = _make_video_settings(
            codec="copy",
            copy_dv=True,
            dovi_profile="2",
            inject_hdr_meta=True,
            static_hdr_metadata_analysis_request="precise",
        )
        config = _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=video,
            video_tracks=[video],
        )
        wf = _make_workflow()

        with patch.object(wf, "_detect_source_dynamic_hdr_presence", return_value=(True, False)), \
             patch.object(wf, "_extract_static_hdr_metadata", return_value=("SOURCE_MD", "1000,400")), \
             patch.object(wf, "_extract_static_hdr_via_ffprobe", return_value=("FF_MD", "900,300")):
            normalized = wf._normalize_dynamic_hdr_config(config)

        assert normalized.video is not None
        assert normalized.video.master_display == ""
        assert normalized.video.max_cll == ""
        assert normalized.video.static_hdr_metadata_analysis_request == "precise"

    def test_scheduled_analysis_rejects_external_nvencc_pipeline(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1000)
        video = _make_video_settings(
            codec="nvencc_hevc",
            copy_dv=True,
            dovi_profile="2",
            static_hdr_metadata_analysis_request="precise",
        )
        config = _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=video,
            video_tracks=[video],
        )

        errors = _make_workflow().validate(config)

        assert any("pipeline vidéo unique FFmpeg/copy" in error for error in errors)

    def test_copy_codec_with_dovi_normalization_but_no_dv_uses_standard_runner(self, tmp_path):
        """Si la source ne contient pas de DoVi, la normalisation demandée est ignorée."""
        config = self._make_copy_config(tmp_path, copy_dv=True, dovi_profile="2")
        wf = _make_workflow()

        with patch.object(wf, "_detect_source_dynamic_hdr_presence", return_value=(False, False)), \
             patch.object(wf, "_run_with_metadata_inject", return_value=MagicMock()) as inject_mock, \
             patch.object(wf._runner, "run", return_value=MagicMock()) as direct_mock:
            wf.run(config)

        assert not inject_mock.called
        assert direct_mock.called

    def test_non_copy_with_copy_dv_calls_metadata_inject(self, tmp_path):
        """run() avec codec=libx265 + copy_dv=True DOIT appeler _run_with_metadata_inject."""
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1000)
        config = _make_config(
            source=src, output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="libx265"),
            copy_dv=True,
        )
        wf = _make_workflow()
        inject_called = [False]

        def _spy(cfg, **_kwargs):
            inject_called[0] = True
            return MagicMock()

        with patch.object(wf, "_detect_source_dynamic_hdr_presence", return_value=(True, False)), \
             patch.object(wf, "_run_with_metadata_inject", side_effect=_spy):
            wf.run(config)

        assert inject_called[0], \
            "codec≠copy + copy_dv doit passer par _run_with_metadata_inject"

    def test_non_copy_with_copy_dv_but_no_hdr_presence_uses_standard_runner(self, tmp_path):
        """Si aucun DV/HDR10+ détecté, codec≠copy reste sur le chemin encode standard."""
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1000)
        config = _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="libx265"),
            copy_dv=True,
        )
        wf = _make_workflow()
        inject_called = [False]

        def _spy(_cfg, **_kwargs):
            inject_called[0] = True
            return MagicMock()

        with patch.object(wf, "_detect_source_dynamic_hdr_presence", return_value=(False, False)), \
             patch.object(wf, "_run_with_metadata_inject", side_effect=_spy), \
             patch.object(wf._runner, "run") as mock_run:
            mock_run.return_value = MagicMock()
            wf.run(config)

        assert not inject_called[0], "Sans DV/HDR10+ source, l'injection ne doit pas être lancée."
        assert mock_run.called, "Le chemin encode standard doit rester utilisé."

    def test_hevc_nvenc_with_explicit_hdr10_stays_on_standard_path_when_patch_is_disabled(self, tmp_path):
        """Le patch HDR10 statique NVENC est conservé mais désactivé dans le workflow actif."""
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1000)
        config = _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(
                codec="hevc_nvenc",
                inject_hdr_meta=True,
                master_display="G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)",
                max_cll="1000,400",
            ),
        )
        wf = _make_workflow()

        with patch.object(wf, "_run_with_metadata_inject", return_value=MagicMock()) as inject_mock, \
             patch.object(wf._runner, "run") as mock_run:
            mock_run.return_value = MagicMock()
            wf.run(config)

        assert not inject_mock.called, "Le fallback bitstream HDR10 NVENC ne doit plus être actif."
        assert mock_run.called, "Le chemin encode standard doit rester utilisé."

    def test_libx265_with_explicit_hdr10_stays_on_native_codec_path(self, tmp_path):
        """libx265 injecte le HDR statique nativement via x265-params."""
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1000)
        config = _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(
                codec="libx265",
                inject_hdr_meta=True,
                master_display="G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)",
                max_cll="1000,400",
            ),
        )
        wf = _make_workflow()

        with patch.object(wf, "_run_with_metadata_inject", return_value=MagicMock()) as inject_mock, \
             patch.object(wf._runner, "run") as mock_run:
            mock_run.return_value = MagicMock()
            wf.run(config)

        assert not inject_mock.called, "libx265 ne doit pas passer par le fallback bitstream."
        assert mock_run.called, "libx265 doit rester sur le chemin encode standard."

    def test_hevc_vaapi_with_explicit_hdr10_stays_on_native_codec_path(self, tmp_path):
        """hevc_vaapi garde sa voie native via -sei +hdr."""
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1000)
        config = _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(
                codec="hevc_vaapi",
                inject_hdr_meta=True,
                master_display="G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)",
                max_cll="1000,400",
            ),
        )
        wf = _make_workflow()

        with patch.object(wf, "_run_with_metadata_inject", return_value=MagicMock()) as inject_mock, \
             patch.object(wf._runner, "run") as mock_run:
            mock_run.return_value = MagicMock()
            wf.run(config)

        assert not inject_mock.called, "hevc_vaapi ne doit pas passer par le fallback bitstream."
        assert mock_run.called, "hevc_vaapi doit rester sur le chemin encode standard."

    @pytest.mark.parametrize("copy_dv,copy_hdr10plus", [
        (True, False), (False, True), (True, True)
    ])
    def test_copy_codec_emits_passthrough_log(self, tmp_path, copy_dv, copy_hdr10plus):
        """run() émet un log INFO 'passthrough' quand copy_dv/hdr10+ est ignoré."""
        config = self._make_copy_config(tmp_path, copy_dv=copy_dv,
                                        copy_hdr10plus=copy_hdr10plus)
        wf = _make_workflow()
        log_msgs: list[tuple[str, str]] = []
        wf.log_message.connect(lambda lvl, msg: log_msgs.append((lvl, msg)))

        with patch.object(wf._runner, "run") as mock_run:
            mock_run.return_value = MagicMock()
            wf.run(config)

        passthrough_logs = [
            (lvl, msg) for lvl, msg in log_msgs
            if lvl == "INFO" and "passthrough" in msg.lower()
        ]
        assert len(passthrough_logs) >= 1, \
            f"Aucun log INFO 'passthrough' émis. Logs reçus : {log_msgs}"

    def test_copy_no_flags_uses_standard_ffmpeg_runner(self, tmp_path):
        """run() avec codec=copy sans copy_dv/hdr10+ utilise le runner standard."""
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1000)
        config = _make_config(
            source=src, output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="copy"),
        )
        wf = _make_workflow()
        with patch.object(wf._runner, "run") as mock_run:
            mock_run.return_value = MagicMock()
            wf.run(config)

    def test_validate_false_returns_before_preparation_finishes(self, tmp_path):
        """Le chemin UI encode ne bloque pas pendant la préparation workflow."""
        config = self._make_copy_config(tmp_path)
        wf = _make_workflow()
        release = threading.Event()
        inner = TaskSignals()

        def _slow_preparation(_cfg, *, validate):
            assert validate is False
            release.wait(timeout=0.25)
            return inner

        with patch.object(wf, "_run_with_preparation", side_effect=_slow_preparation):
            started = time.monotonic()
            outer = wf.run(config, validate=False)
            elapsed = time.monotonic() - started
            release.set()

        assert outer is not inner
        assert elapsed < 0.10

    def test_cancel_during_async_preparation_prevents_ffmpeg_launch(self, tmp_path):
        """Annuler pendant la jauge de préparation ne doit pas lancer ffmpeg ensuite."""
        config = self._make_copy_config(tmp_path)
        wf = _make_workflow()
        entered = threading.Event()
        release = threading.Event()
        launched: list[list[str]] = []
        cancelled: list[bool] = []

        def _slow_prepare(config_arg, *, validate, prep_signals=None):
            entered.set()
            release.wait(timeout=1.0)
            wf._check_cancelled(prep_signals)
            launched.append(["ffmpeg"])
            return TaskSignals()

        with patch.object(wf, "_run_with_preparation", side_effect=_slow_prepare):
            outer = wf.run(config, validate=False)
            outer.cancelled.connect(lambda: cancelled.append(True), Qt.ConnectionType.QueuedConnection)
            assert entered.wait(timeout=1.0)
            outer.cancel()
            release.set()
            _collect_signals(outer)

        assert launched == []
        assert cancelled == [True]

    def test_async_preparation_does_not_self_connect_outer_signals(self, tmp_path):
        """Si la préparation utilise déjà le signal extérieur, ne pas le relayer à lui-même."""
        config = self._make_copy_config(tmp_path)
        wf = _make_workflow()
        prepared = threading.Event()
        progress_messages: list[str] = []
        failures: list[str] = []

        def _prepare_with_outer_signal(_cfg, *, validate, prep_signals=None):
            assert prep_signals is not None
            prepared.set()
            prep_signals.progress.emit("prep-progress")
            return prep_signals

        with patch.object(wf, "_run_with_preparation", side_effect=_prepare_with_outer_signal):
            outer = wf.run(config, validate=False)
            outer.progress.connect(lambda msg: progress_messages.append(msg), Qt.ConnectionType.QueuedConnection)
            outer.failed.connect(lambda msg, _exc: failures.append(msg), Qt.ConnectionType.QueuedConnection)
            assert prepared.wait(timeout=1.0)
            _get_app().processEvents()

        assert progress_messages in ([], ["prep-progress"])
        assert failures == []


class TestRunIntegratedMetadata:
    def _base_config(self, tmp_path: Path) -> EncodeConfig:
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1000)
        return _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="copy"),
        )

    def test_chapter_overrides_stays_on_single_pass_runner(self, tmp_path):
        cfg = self._base_config(tmp_path)
        cfg.chapter_overrides = []
        wf = _make_workflow()

        with patch.object(wf._runner, "run") as mock_run:
            mock_run.return_value = MagicMock()
            wf.run(cfg)

        assert mock_run.called

    def test_writing_application_stays_on_single_pass_runner(self, tmp_path):
        cfg = self._base_config(tmp_path)
        wf = EncodeWorkflow(
            ffmpeg_bin="ffmpeg",
            dovi_tool_bin="dovi_tool",
            hdr10plus_bin="hdr10plus_tool",
            writing_application="MuxiveoTest",
        )

        with patch.object(wf._runner, "run") as mock_run:
            mock_run.return_value = MagicMock()
            wf.run(cfg)

        assert mock_run.called


class TestInjectStorageChecks:
    def _make_inject_config(self, tmp_path: Path) -> EncodeConfig:
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        return _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="libx265"),
            copy_dv=True,
            work_dir=tmp_path / "work",
        )

    def test_inject_path_checks_storage_before_launch(self, tmp_path):
        cfg = self._make_inject_config(tmp_path)
        wf = _make_workflow()
        logs: list[tuple[str, str]] = []
        wf.log_message.connect(lambda lvl, msg: logs.append((lvl, msg)))

        with patch.object(wf, "_detect_source_dynamic_hdr_presence", return_value=(True, False)), \
             patch.object(wf, "_estimate_inject_storage_requirements", return_value=(100, 200)) as mock_est, \
             patch("core.workflows.encode.workflow.shutil.disk_usage") as mock_du, \
             patch.object(wf, "_run_with_metadata_inject") as mock_inject:
            mock_du.return_value = SimpleNamespace(total=10_000_000, used=1, free=9_000_000)
            mock_inject.return_value = MagicMock()
            wf.run(cfg)

        assert mock_est.called
        assert mock_inject.called
        assert any("Estimation espace injection" in msg for _lvl, msg in logs), \
            f"Log d'estimation absent. Logs: {logs}"

    def test_inject_path_raises_when_storage_is_insufficient(self, tmp_path):
        cfg = self._make_inject_config(tmp_path)
        wf = _make_workflow()

        with patch.object(wf, "_detect_source_dynamic_hdr_presence", return_value=(True, False)), \
             patch.object(wf, "_estimate_inject_storage_requirements", return_value=(10_000, 20_000)), \
             patch("core.workflows.encode.workflow.shutil.disk_usage") as mock_du, \
             patch.object(wf, "_run_with_metadata_inject") as mock_inject:
            mock_du.return_value = SimpleNamespace(total=100_000, used=99_900, free=100)
            with pytest.raises(EncodeError, match="Espace disque insuffisant"):
                wf.run(cfg)

        assert not mock_inject.called


class TestDynamicHdrDetection:
    def test_detect_source_dynamic_hdr_presence_falls_back_to_mediainfo(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1000)
        wf = _make_workflow()

        ffprobe_payload = json.dumps({
            "streams": [
                {
                    "codec_type": "video",
                    "side_data_list": [],
                }
            ]
        })

        def _fake_subprocess_run(cmd, *args, **kwargs):
            prog = Path(cmd[0]).name
            if prog.startswith("ffprobe"):
                return MagicMock(returncode=0, stdout=ffprobe_payload, stderr="")
            if prog == "mediainfo":
                if cmd[1] == "--Inform=Video;%HDR_Format%":
                    return MagicMock(returncode=0, stdout="Dolby Vision", stderr="")
                if cmd[1] == "--Inform=Video;%HDR_Format_Compatibility%":
                    return MagicMock(returncode=0, stdout="HDR10+", stderr="")
            raise AssertionError(f"Commande subprocess inattendue: {cmd}")

        with patch("subprocess.run", side_effect=_fake_subprocess_run):
            assert wf._detect_source_dynamic_hdr_presence(src) == (True, True)

    def test_detect_source_dynamic_hdr_presence_falls_back_to_ffprobe_frames(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1000)
        wf = _make_workflow()

        with patch.object(
            wf,
            "_ffprobe_streams_payload",
            return_value={"streams": [{"codec_type": "video", "side_data_list": []}]},
        ), patch.object(
            wf,
            "_mediainfo_hdr_flags",
            return_value=None,
        ), patch.object(
            wf,
            "_ffprobe_frame_dynamic_hdr_flags",
            return_value=(True, True),
        ):
            assert wf._detect_source_dynamic_hdr_presence(src) == (True, True)


class TestIntegratedMetadataCommand:
    def test_tag_overrides_disable_metadata_copy(self, tmp_path):
        src = tmp_path / "source.mkv"; src.write_bytes(b"\x00")
        out = tmp_path / "output.mkv"
        cfg = _make_config(
            src,
            out,
            video=_make_video_settings(codec="copy"),
            tag_overrides={"GENRE": "Drama"},
            file_title="Titre",
        )
        wf = _make_workflow()
        cmd = wf.build_command_single(cfg)

        idx = cmd.index("-map_metadata")
        # Avec tag_overrides, les tags sources sont ignorés mais les chapitres
        # restent préservés via chapter_map (fallback = input 0). On vérifie
        # que la source des tags globaux == source des chapitres.
        chap_idx = cmd.index("-map_chapters")
        assert cmd[idx + 1] == cmd[chap_idx + 1]
        assert "GENRE=Drama" in cmd
        assert "title=Titre" in cmd

    def test_chapter_overrides_with_tag_overrides_map_metadata_from_chapter_input(self, tmp_path):
        src = tmp_path / "source.mkv"; src.write_bytes(b"\x00")
        out = tmp_path / "output.mkv"
        cfg = _make_config(
            src,
            out,
            video=_make_video_settings(codec="copy"),
            tag_overrides={"GENRE": "Drama"},
            chapter_overrides=[
                ChapterEntry(timecode_s=0.0, name="Intro"),
                ChapterEntry(timecode_s=30.0, name="Outro"),
            ],
        )
        wf = _make_workflow()

        cmd = wf.build_command_single(cfg)

        idx = cmd.index("-map_metadata")
        assert cmd[idx + 1] == "1"
        chap_idx = cmd.index("-map_chapters")
        assert cmd[chap_idx + 1] == "1"

    def test_tag_sources_copy_from_last_source(self, tmp_path):
        src = tmp_path / "source.mkv"; src.write_bytes(b"\x00")
        tag1 = tmp_path / "tag1.mkv"; tag1.write_bytes(b"\x00")
        tag2 = tmp_path / "tag2.mkv"; tag2.write_bytes(b"\x00")
        out = tmp_path / "output.mkv"
        cfg = _make_config(
            src,
            out,
            video=_make_video_settings(codec="copy"),
            tag_sources=[tag1, tag2],
        )
        wf = _make_workflow()
        cmd = wf.build_command_single(cfg)

        assert str(tag2) in cmd
        idx = cmd.index("-map_metadata")
        assert cmd[idx + 1] == "1"

    def test_empty_chapter_overrides_remove_chapters(self, tmp_path):
        src = tmp_path / "source.mkv"; src.write_bytes(b"\x00")
        out = tmp_path / "output.mkv"
        cfg = _make_config(
            src,
            out,
            video=_make_video_settings(codec="copy"),
            chapter_overrides=[],
        )
        wf = _make_workflow()
        cmd = wf.build_command_single(cfg)

        idx = cmd.index("-map_chapters")
        assert cmd[idx + 1] == "-1"

    def test_main_command_does_not_force_muxing_application_tag(self, tmp_path):
        src = tmp_path / "source.mkv"; src.write_bytes(b"\x00")
        out = tmp_path / "output.mkv"
        cfg = _make_config(src, out, video=_make_video_settings(codec="copy"))
        wf = EncodeWorkflow(
            ffmpeg_bin="ffmpeg",
            dovi_tool_bin="dovi_tool",
            hdr10plus_bin="hdr10plus_tool",
            writing_application="MuxiveoMuxApp",
        )
        cmd = wf.build_command_single(cfg)
        assert not any("muxing_application=" in str(arg) for arg in cmd)

    def test_main_command_includes_threads_argument(self, tmp_path):
        src = tmp_path / "source.mkv"; src.write_bytes(b"\x00")
        out = tmp_path / "output.mkv"
        cfg = _make_config(
            src,
            out,
            video=_make_video_settings(codec="copy"),
            tag_overrides={},
        )
        wf = _make_workflow(ffmpeg_threads=11)

        cmd = wf.build_command_single(cfg)

        assert "-threads" in cmd
        assert cmd[cmd.index("-threads") + 1] == "11"


class TestSelectedAttachedPicHandling:

    def test_run_converts_selected_cover_to_attach(self, tmp_path):
        """
        Un attachment sélectionné reporté par ffprobe comme attached_pic ne doit
        plus être remappé via -map 0:stream, mais extrait puis réattaché avec
        -attach pour conserver son statut d'attachment logique.
        """
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1000)
        out = tmp_path / "output.mkv"
        config = _make_config(
            source=src,
            output=out,
            video=_make_video_settings(codec="copy"),
            audio_tracks=[AudioTrackSettings(stream_index=1, codec="ac3", bitrate_kbps=384)],
            attachment_streams=[(src, 9)],
        )
        wf = _make_workflow()
        captured: list[list[str]] = []

        ffprobe_payload = json.dumps({
            "streams": [
                {
                    "index": 9,
                    "disposition": {"attached_pic": 1},
                    "tags": {"filename": "cover.jpg", "mimetype": "image/jpeg"},
                }
            ]
        })

        def _fake_subprocess_run(cmd, *args, **kwargs):
            prog = Path(cmd[0]).name
            if prog.startswith("ffprobe"):
                return MagicMock(returncode=0, stdout=ffprobe_payload, stderr="")
            if prog == "ffmpeg" and "-map" in cmd and "0:9" in cmd:
                Path(cmd[-1]).write_bytes(b"jpeg-data")
                return MagicMock(returncode=0, stdout="", stderr="")
            raise AssertionError(f"Commande subprocess inattendue: {cmd}")

        original_run_cmd = wf._runner._run_cmd

        def _fake_run_cmd(cmd, *args, **kwargs):
            if "-map" in cmd and "0:9" in cmd:
                Path(cmd[-1]).write_bytes(b"jpeg-data")
                return ""
            return original_run_cmd(cmd, *args, **kwargs)

        with patch("subprocess.run", side_effect=_fake_subprocess_run), \
             patch.object(wf._runner, "_run_cmd", side_effect=_fake_run_cmd), \
             patch.object(wf._runner, "run") as mock_run:
            signals = TaskSignals()
            mock_run.side_effect = lambda cmd, **_kw: captured.append(list(cmd)) or signals
            returned = wf.run(config)
            returned.finished.emit("done")

        assert returned is signals
        assert len(captured) == 1
        cmd = captured[0]
        assert "-attach" in cmd
        attach_path = Path(cmd[cmd.index("-attach") + 1])
        assert attach_path.name == "cover.jpg"
        assert "0:9" not in cmd, f"Le stream cover ne doit plus être remappé: {cmd}"
        assert "-metadata:s:t:0" in cmd
        idx = cmd.index("-metadata:s:t:0")
        assert cmd[idx + 1] == "mimetype=image/jpeg"
        assert mock_run.called


# ===========================================================================
# _run_with_metadata_inject — pistes audio dans la reconstitution finale
# ===========================================================================

class TestMetadataInjectAudio:
    """
    Vérifie que la commande de reconstitution finale dans _run_with_metadata_inject
    contient les bons arguments audio (map, codec, bitrate, BSF).

    Contexte :
      - La reconstitution ffmpeg utilise deux inputs :
          input 0 = current_hevc (flux vidéo injecté)
          input 1 = source (audio + subs)
      - L'audio est donc mappé via "-map 1:{stream_index}".
      - Les arguments codec sont appliqués par output stream index (0-based).

    Scénarios testés :
      - Aucune piste audio → aucun -map audio ni -c:a dans la reconstitution
      - Audio copy → -map 1:N, -c:a:0 copy, pas de -b:a:0
      - Audio AAC → -map 1:N, -c:a:0 aac, -b:a:0 NNNk
      - Audio EAC3 → -map 1:N, -c:a:0 eac3, -b:a:0 NNNk
      - Audio FLAC → -map 1:N, -c:a:0 flac, pas de -b:a:0
      - Plusieurs pistes → -map 1:N1, -c:a:0, -map 1:N2, -c:a:1
      - TrueHD core → -bsf:a:0 truehd_core avant -c:a:0 copy
      - copy_subtitles=True → -map 1:s? -c:s copy dans la reconstitution
    """

    def _run_inject_with_audio(
        self,
        tmp_path: Path,
        audio_tracks: list,
        copy_subtitles: bool = False,
    ) -> list[list[str]]:
        """
        Lance _run_with_metadata_inject (libx265 + copy_dv) avec les pistes audio données.
        Retourne la liste de toutes les commandes exécutées.
        """
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        config = _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="libx265"),
            audio_tracks=audio_tracks,
            copy_subtitles=copy_subtitles,
            copy_dv=True,
            work_dir=tmp_path / "work",
        )
        wf = _make_workflow(enabled=False)
        cmds_run: list[list[str]] = []

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            cmds_run.append(list(cmd))
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 100_000)
                    break
            return ""

        with patch.object(EncodeWorkflow, "_shm_path",
                          side_effect=lambda tmp_dir, name, _size: tmp_dir / name):
            with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
                sigs = wf._run_with_metadata_inject(config)
                _collect_signals(sigs)

        return cmds_run

    def _get_recon_cmd(self, cmds: list[list[str]]) -> list[str]:
        """Extrait la commande de reconstitution finale (ffmpeg avec output.mkv)."""
        recon = [c for c in cmds if c[0] == "ffmpeg" and "output.mkv" in " ".join(c)]
        assert len(recon) == 1, f"Commande de reconstitution introuvable. Cmds: {[c[0] for c in cmds]}"
        return recon[0]

    def test_no_audio_tracks_no_audio_args(self, tmp_path):
        """Sans pistes audio, la reconstitution ne contient ni -map audio ni -c:a."""
        cmds = self._run_inject_with_audio(tmp_path, audio_tracks=[])
        recon = self._get_recon_cmd(cmds)
        assert "-c:a:0" not in recon
        assert not any(arg.startswith("-c:a") for arg in recon)

    def test_audio_copy_maps_from_source_input(self, tmp_path):
        """Audio copy : mappé depuis l'input 1 (source), pas l'input 0 (HEVC)."""
        track = AudioTrackSettings(stream_index=2, codec="copy")
        cmds = self._run_inject_with_audio(tmp_path, [track])
        recon = self._get_recon_cmd(cmds)
        assert "-map" in recon
        assert "1:2" in recon, f"Attendu -map 1:2 (source input 1). Reconstitution: {recon}"
        # Pas de -map 0:2 (l'HEVC n'a pas d'audio)
        cmd_str = " ".join(recon)
        assert "0:2" not in cmd_str, "Audio ne doit pas être mappé depuis le HEVC (input 0)"

    def test_audio_copy_no_bitrate(self, tmp_path):
        """Audio copy : pas de -b:a dans la reconstitution."""
        track = AudioTrackSettings(stream_index=1, codec="copy", bitrate_kbps=384)
        cmds = self._run_inject_with_audio(tmp_path, [track])
        recon = self._get_recon_cmd(cmds)
        assert "-b:a:0" not in recon
        assert "-c:a:0" in recon
        assert "copy" in recon

    def test_audio_aac_has_codec_and_bitrate(self, tmp_path):
        """Audio AAC : -c:a:0 aac et -b:a:0 NNNk présents dans la reconstitution."""
        track = AudioTrackSettings(stream_index=1, codec="aac", bitrate_kbps=192)
        cmds = self._run_inject_with_audio(tmp_path, [track])
        recon = self._get_recon_cmd(cmds)
        assert "-c:a:0" in recon
        assert "aac" in recon
        assert "-b:a:0" in recon
        assert "192k" in recon

    def test_audio_eac3_has_codec_and_bitrate(self, tmp_path):
        """Audio EAC3 : -c:a:0 eac3 et -b:a:0 NNNk présents dans la reconstitution."""
        track = AudioTrackSettings(stream_index=1, codec="eac3", bitrate_kbps=640)
        cmds = self._run_inject_with_audio(tmp_path, [track])
        recon = self._get_recon_cmd(cmds)
        assert "eac3" in recon
        assert "640k" in recon

    def test_audio_eac3_7_1_is_downmixed_to_5_1_in_reconstitution(self, tmp_path):
        track = AudioTrackSettings(
            stream_index=1,
            codec="eac3",
            bitrate_kbps=640,
            input_channels=8,
            input_channel_layout="7.1(side)",
        )
        cmds = self._run_inject_with_audio(tmp_path, [track])
        recon = self._get_recon_cmd(cmds)
        assert "-ac:a:0" in recon and recon[recon.index("-ac:a:0") + 1] == "6"
        assert "-channel_layout:a:0" in recon and recon[recon.index("-channel_layout:a:0") + 1] == "5.1"

    def test_audio_flac_no_bitrate(self, tmp_path):
        """Audio FLAC : -c:a:0 flac sans -b:a (codec sans débit)."""
        track = AudioTrackSettings(stream_index=1, codec="flac")
        cmds = self._run_inject_with_audio(tmp_path, [track])
        recon = self._get_recon_cmd(cmds)
        assert "flac" in recon
        assert "-b:a:0" not in recon

    def test_multiple_audio_tracks(self, tmp_path):
        """Plusieurs pistes : chaque track mappée depuis source avec le bon index de sortie."""
        tracks = [
            AudioTrackSettings(stream_index=1, codec="copy"),
            AudioTrackSettings(stream_index=3, codec="aac", bitrate_kbps=256),
        ]
        cmds = self._run_inject_with_audio(tmp_path, tracks)
        recon = self._get_recon_cmd(cmds)
        # Piste 0 : copy depuis stream 1 de la source
        assert "1:1" in recon
        assert "-c:a:0" in recon
        # Piste 1 : aac depuis stream 3 de la source
        assert "1:3" in recon
        assert "-c:a:1" in recon
        assert "256k" in recon

    def test_reordered_audio_tracks_across_sources_preserved_in_reconstitution(self, tmp_path):
        alt = tmp_path / "alt.mkv"
        tracks = [
            AudioTrackSettings(stream_index=5, codec="aac", bitrate_kbps=192, source_path=alt),
            AudioTrackSettings(stream_index=1, codec="copy"),
        ]
        cmds = self._run_inject_with_audio(tmp_path, tracks)
        recon = self._get_recon_cmd(cmds)

        map_values = [recon[i + 1] for i, arg in enumerate(recon[:-1]) if arg == "-map"]
        assert map_values[:3] == ["0:v:0", "2:5", "1:1"]
        assert "-c:a:0" in recon and recon[recon.index("-c:a:0") + 1] == "aac"
        assert "-b:a:0" in recon and recon[recon.index("-b:a:0") + 1] == "192k"
        assert "-c:a:1" in recon and recon[recon.index("-c:a:1") + 1] == "copy"

    def test_truehd_core_bsf_in_reconstitution(self, tmp_path):
        """extract_truehd_core=True : -bsf:a:0 truehd_core dans la reconstitution."""
        track = AudioTrackSettings(stream_index=1, codec="copy", extract_truehd_core=True)
        cmds = self._run_inject_with_audio(tmp_path, [track])
        recon = self._get_recon_cmd(cmds)
        cmd_str = " ".join(recon)
        assert "truehd_core" in cmd_str, \
            f"BSF truehd_core absent de la reconstitution. Cmd: {recon}"

    def test_copy_subtitles_mapped_from_source(self, tmp_path):
        """copy_subtitles=True : -map 1:s? -c:s copy dans la reconstitution."""
        cmds = self._run_inject_with_audio(tmp_path, audio_tracks=[], copy_subtitles=True)
        recon = self._get_recon_cmd(cmds)
        cmd_str = " ".join(recon)
        assert "1:s?" in cmd_str, \
            f"Subs non mappés depuis source (attendu '1:s?'). Cmd: {recon}"
        assert "-c:s" in recon and "copy" in recon


# ===========================================================================
# file_title — balise Title du segment dans les commandes FFmpeg
# ===========================================================================

class TestEncodeFileTitleCommand:
    """
    Vérifie que -metadata title=<value> est injecté dans chaque variante
    de build_command (single-pass, two-pass pass2) avec la valeur exacte.
    """

    def setup_method(self):
        self.wf = _make_workflow()

    def _src(self, tmp_path: Path) -> Path:
        src = tmp_path / "src.mkv"
        src.touch()
        return src

    # ── single pass ──────────────────────────────────────────────────────────

    def test_single_pass_title_flag_present(self, tmp_path):
        """-metadata title=... présent dans le single pass."""
        src = self._src(tmp_path)
        cmd = self.wf.build_command_single(
            _make_config(src, tmp_path / "out.mkv", file_title="Mon Film")
        )
        assert "-metadata" in cmd
        idx = cmd.index("-metadata")
        assert cmd[idx + 1] == "title=Mon Film"

    def test_single_pass_title_empty(self, tmp_path):
        """-metadata title= (vide) présent si file_title=''."""
        src = self._src(tmp_path)
        cmd = self.wf.build_command_single(
            _make_config(src, tmp_path / "out.mkv", file_title="")
        )
        assert "-metadata" in cmd
        idx = cmd.index("-metadata")
        assert cmd[idx + 1] == "title="

    def test_single_pass_title_before_output(self, tmp_path):
        """-metadata title= précède le chemin de sortie."""
        src = self._src(tmp_path)
        out = tmp_path / "out.mkv"
        cmd = self.wf.build_command_single(
            _make_config(src, out, file_title="Film")
        )
        assert cmd.index("-metadata") < cmd.index(str(out))

    # ── two pass (mode SIZE) ─────────────────────────────────────────────────

    def test_two_pass_pass2_title_present(self, tmp_path):
        """-metadata title=... présent dans la passe 2 (mode SIZE)."""
        src = self._src(tmp_path)
        vs = _make_video_settings(quality_mode=QualityMode.SIZE)
        cmds = self.wf.build_command(
            _make_config(src, tmp_path / "out.mkv", video=vs,
                         duration_s=3600.0, file_title="Film Encode")
        )
        pass2 = cmds[1]
        assert "-metadata" in pass2
        idx = pass2.index("-metadata")
        assert pass2[idx + 1] == "title=Film Encode"

    def test_two_pass_pass1_no_title(self, tmp_path):
        """-metadata absent de la passe 1 (analyse seule)."""
        src = self._src(tmp_path)
        vs = _make_video_settings(quality_mode=QualityMode.SIZE)
        cmds = self.wf.build_command(
            _make_config(src, tmp_path / "out.mkv", video=vs,
                         duration_s=3600.0, file_title="Film Encode")
        )
        pass1 = cmds[0]
        assert "-metadata" not in pass1

    # ── reconstitution finale (_run_with_metadata_inject) ────────────────────

    def _run_inject(self, tmp_path: Path, file_title: str) -> list[str]:
        """Lance _run_with_metadata_inject et retourne la commande de reconstitution."""
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        config = _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="libx265"),
            copy_dv=True,
            work_dir=tmp_path / "work",
            file_title=file_title,
        )
        wf = _make_workflow(enabled=False)
        cmds_run: list[list[str]] = []

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            cmds_run.append(list(cmd))
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 100_000)
                    break
            return ""

        with patch.object(EncodeWorkflow, "_shm_path",
                          side_effect=lambda tmp_dir, name, _size: tmp_dir / name):
            with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
                sigs = wf._run_with_metadata_inject(config)
                _collect_signals(sigs)

        recon = [c for c in cmds_run if c[0] == "ffmpeg" and "output.mkv" in " ".join(c)]
        assert len(recon) == 1, f"Commande de reconstitution introuvable. Cmds: {cmds_run}"
        return recon[0]

    def test_inject_path_title_present(self, tmp_path):
        """-metadata title=... présent dans la reconstitution finale (DV/HDR10+)."""
        recon = self._run_inject(tmp_path, "Mon Film DV")
        assert "-metadata" in recon
        idx = recon.index("-metadata")
        assert recon[idx + 1] == "title=Mon Film DV"

    def test_inject_path_title_empty(self, tmp_path):
        """-metadata title= (vide) présent si file_title='' dans le chemin DV/HDR10+."""
        recon = self._run_inject(tmp_path, "")
        assert "-metadata" in recon
        idx = recon.index("-metadata")
        assert recon[idx + 1] == "title="

    def test_inject_path_title_before_output(self, tmp_path):
        """-metadata title= précède le chemin de sortie dans la reconstitution finale."""
        recon = self._run_inject(tmp_path, "Film DV")
        meta_idx = recon.index("-metadata")
        # output.mkv est le dernier argument de la commande de reconstitution
        assert meta_idx < len(recon) - 1


class TestInjectPathIntegratedPostproc:
    def _run_inject(self, tmp_path: Path, **config_overrides) -> list[list[str]]:
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        default_cfg = dict(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="libx265"),
            copy_dv=True,
            work_dir=tmp_path / "work",
            file_title="Film DV",
        )
        default_cfg.update(config_overrides)
        config = _make_config(**cast(Any, default_cfg))
        wf = EncodeWorkflow(
            ffmpeg_bin="ffmpeg",
            dovi_tool_bin="dovi_tool",
            hdr10plus_bin="hdr10plus_tool",
            writing_application="MuxiveoMuxApp",
            ram_buffer_enabled=False,
        )

        cmds_run: list[list[str]] = []

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            cmds_run.append(list(cmd))
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json", ".ffmetadata")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 100_000)
                    break
            return ""

        with patch.object(EncodeWorkflow, "_shm_path", side_effect=lambda tmp_dir, name, _size: tmp_dir / name):
            with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
                sigs = wf._run_with_metadata_inject(config)
                _collect_signals(sigs)
        return cmds_run

    def _get_recon_cmd(self, cmds: list[list[str]]) -> list[str]:
        recon = [c for c in cmds if c[0] == "ffmpeg" and "output.mkv" in " ".join(c)]
        assert len(recon) == 1, f"Attendu une seule commande ffmpeg de sortie, obtenu {len(recon)}"
        return recon[0]

    def test_recon_applies_postproc_features_in_single_pass(self, tmp_path):
        cfg = dict(
            tag_overrides={"GENRE": "Drama"},
            chapter_overrides=[],
            track_meta_edits=[TrackMetaEdit(track_order=1, language="fr-FR", title="Main Video")],
        )
        cmds = self._run_inject(tmp_path, **cfg)
        recon = self._get_recon_cmd(cmds)

        assert "-map_metadata" in recon
        assert recon[recon.index("-map_metadata") + 1] == "-1"
        assert "-map_chapters" in recon
        assert recon[recon.index("-map_chapters") + 1] == "-1"
        assert "GENRE=Drama" in recon
        assert not any("muxing_application=" in str(arg) for arg in recon)
        assert "-metadata:s:v:0" in recon
        assert "language=fr-FR" in recon
        assert "language-ietf=" in recon
        assert "title=Main Video" in recon

    def test_inject_path_calls_matroska_header_patch_hook(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        config = _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="libx265"),
            copy_dv=True,
            work_dir=tmp_path / "work",
        )
        wf = EncodeWorkflow(
            ffmpeg_bin="ffmpeg",
            dovi_tool_bin="dovi_tool",
            hdr10plus_bin="hdr10plus_tool",
            writing_application="MuxiveoMuxApp",
            ram_buffer_enabled=False,
        )

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json", ".ffmetadata")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 64_000)
                    break
            return ""

        with patch.object(EncodeWorkflow, "_shm_path", side_effect=lambda tmp_dir, name, _size: tmp_dir / name):
            with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
                with patch.object(wf._muxing_post_action, "apply_if_mkv") as patch_hook:
                    sigs = wf._run_with_metadata_inject(config)
                    _collect_signals(sigs)

        assert patch_hook.called

    def test_recon_maps_metadata_from_last_tag_source(self, tmp_path):
        tag1 = tmp_path / "tag1.mkv"
        tag2 = tmp_path / "tag2.mkv"
        tag1.write_bytes(b"\x00")
        tag2.write_bytes(b"\x00")

        cmds = self._run_inject(
            tmp_path,
            tag_sources=[tag1, tag2],
            tag_overrides=None,
            chapter_overrides=None,
        )
        recon = self._get_recon_cmd(cmds)

        assert str(tag2) in recon
        assert "-map_metadata" in recon
        idx = recon.index("-map_metadata")
        assert recon[idx + 1] == "2"

    def test_recon_uses_chapter_input_metadata_when_tags_are_overridden(self, tmp_path):
        cmds = self._run_inject(
            tmp_path,
            tag_overrides={"GENRE": "Drama"},
            chapter_overrides=[
                ChapterEntry(timecode_s=0.0, name="Intro"),
                ChapterEntry(timecode_s=30.0, name="Outro"),
            ],
        )
        recon = self._get_recon_cmd(cmds)

        idx = recon.index("-map_metadata")
        assert recon[idx + 1] == "2"
        chap_idx = recon.index("-map_chapters")
        assert recon[chap_idx + 1] == "2"

    def test_recon_wraps_injected_hevc_before_final_mux(self, tmp_path):
        cmds = self._run_inject(tmp_path)
        wrap_cmds = [c for c in cmds if c[0] == "ffmpeg" and str(c[-1]).endswith("enc_wrapped.mkv")]
        assert len(wrap_cmds) == 1, f"Commande d'encapsulation introuvable. Cmds: {cmds}"
        wrap = wrap_cmds[0]
        assert "-f" in wrap
        assert wrap[wrap.index("-f") + 1] == "hevc"
        assert "-framerate" in wrap
        assert "-bsf:v" in wrap
        assert wrap[wrap.index("-bsf:v") + 1].startswith("setts=pts=N/(")

        recon = self._get_recon_cmd(cmds)
        first_input = recon[recon.index("-i") + 1]
        assert first_input.endswith("enc_wrapped.mkv")
        assert not first_input.endswith(".hevc")


# ===========================================================================
# extra_attachments — pièces jointes manuelles dans les commandes FFmpeg
# ===========================================================================

class TestEncodeExtraAttachments:
    """
    Vérifie que -attach / -metadata:s:t:N sont injectés pour chaque fichier
    dans EncodeConfig.extra_attachments, dans les trois variantes :
      - single pass (build_command_single)
      - two-pass pass2 (build_command avec mode SIZE)
      - reconstitution finale (_run_with_metadata_inject)

    Conventions ffmpeg :
      -attach <path>                       — attache le fichier
      -metadata:s:t:N mimetype=<mime>      — type MIME de l'attachement N
      -metadata:s:t:N filename=<name>      — nom dans le MKV (cover → "cover")
    """

    def setup_method(self):
        self.wf = _make_workflow()

    def _src(self, tmp_path: Path) -> Path:
        src = tmp_path / "src.mkv"
        src.touch()
        return src

    # ── helpers ─────────────────────────────────────────────────────────────

    def _single(self, tmp_path: Path, extras: list[Path], **kw) -> list[str]:
        src = self._src(tmp_path)
        return self.wf.build_command_single(
            _make_config(src, tmp_path / "out.mkv",
                         extra_attachments=extras, **kw)
        )

    def _pass2(self, tmp_path: Path, extras: list[Path]) -> list[str]:
        src = self._src(tmp_path)
        vs = _make_video_settings(quality_mode=QualityMode.SIZE)
        cmds = self.wf.build_command(
            _make_config(src, tmp_path / "out.mkv", video=vs,
                         duration_s=3600.0, extra_attachments=extras)
        )
        assert cmds and isinstance(cmds[0], list)
        return cast(list[list[str]], cmds)[1]   # pass2

    # ── single pass — présence et valeurs ────────────────────────────────────

    def test_single_attach_flag_present(self, tmp_path):
        """-attach présent pour un attachement manuel (single pass)."""
        extras = [tmp_path / "poster.jpg"]
        cmd = self._single(tmp_path, extras)
        assert "-attach" in cmd

    def test_single_attach_path_correct(self, tmp_path):
        """Le chemin suivant -attach est le chemin absolu du fichier."""
        poster = tmp_path / "poster.jpg"
        cmd = self._single(tmp_path, [poster])
        idx = cmd.index("-attach")
        assert cmd[idx + 1] == str(poster)

    def test_single_attach_mimetype_jpeg(self, tmp_path):
        """-metadata:s:t:0 mimetype=image/jpeg pour un .jpg."""
        cmd = self._single(tmp_path, [tmp_path / "poster.jpg"])
        assert "-metadata:s:t:0" in cmd
        idx = cmd.index("-metadata:s:t:0")
        assert cmd[idx + 1] == "mimetype=image/jpeg"

    def test_single_attach_mimetype_png(self, tmp_path):
        """-metadata:s:t:0 mimetype=image/png pour un .png."""
        cmd = self._single(tmp_path, [tmp_path / "cover.png"])
        assert "-metadata:s:t:0" in cmd
        idx = cmd.index("-metadata:s:t:0")
        assert cmd[idx + 1] == "mimetype=image/png"

    def test_single_attach_mimetype_unknown(self, tmp_path):
        """-metadata:s:t:0 mimetype=application/octet-stream pour extension inconnue."""
        cmd = self._single(tmp_path, [tmp_path / "data.bin"])
        assert "-metadata:s:t:0" in cmd
        idx = cmd.index("-metadata:s:t:0")
        assert cmd[idx + 1] == "mimetype=application/octet-stream"

    def test_single_attach_filename_non_cover(self, tmp_path):
        """-metadata:s:t:0 filename=<basename> pour un fichier non-cover."""
        cmd = self._single(tmp_path, [tmp_path / "poster.jpg"])
        # Il y a deux -metadata:s:t:0 : mimetype puis filename — prendre le second
        indices = [i for i, a in enumerate(cmd) if a == "-metadata:s:t:0"]
        assert len(indices) == 2
        filename_val = cmd[indices[1] + 1]
        assert filename_val == "filename=poster.jpg"

    def test_single_attach_filename_cover(self, tmp_path):
        """-metadata:s:t:0 filename=cover pour un fichier nommé cover.*."""
        cmd = self._single(tmp_path, [tmp_path / "cover.jpg"])
        indices = [i for i, a in enumerate(cmd) if a == "-metadata:s:t:0"]
        assert len(indices) == 2
        filename_val = cmd[indices[1] + 1]
        assert filename_val == "filename=cover"

    def test_single_cover_case_insensitive(self, tmp_path):
        """COVER.JPG → filename=cover (insensible à la casse)."""
        cmd = self._single(tmp_path, [tmp_path / "COVER.JPG"])
        indices = [i for i, a in enumerate(cmd) if a == "-metadata:s:t:0"]
        filename_val = cmd[indices[1] + 1]
        assert filename_val == "filename=cover"

    def test_single_no_extra_no_attach(self, tmp_path):
        """extra_attachments=[] → aucun -attach dans la commande."""
        cmd = self._single(tmp_path, [])
        assert "-attach" not in cmd

    # ── single pass — indices multiples ──────────────────────────────────────

    def test_single_two_attachments_indices(self, tmp_path):
        """Deux attachements → indices 0 et 1 dans les metadata:s:t:N."""
        extras = [tmp_path / "cover.jpg", tmp_path / "notes.txt"]
        cmd = self._single(tmp_path, extras)
        assert "-metadata:s:t:0" in cmd
        assert "-metadata:s:t:1" in cmd

    def test_single_two_attach_flags(self, tmp_path):
        """Deux attachements → deux occurrences de -attach."""
        extras = [tmp_path / "cover.jpg", tmp_path / "notes.txt"]
        cmd = self._single(tmp_path, extras)
        assert sum(1 for a in cmd if a == "-attach") == 2

    def test_single_attachment_streams_offset(self, tmp_path):
        """attachment_streams existants → extra_attachments commencent à l'index N."""
        src = self._src(tmp_path)
        att_streams = [(src, 5)]   # 1 stream existant → extra débute à l'index 1
        extras = [tmp_path / "cover.jpg"]
        with patch.object(
            self.wf,
            "_describe_attachment_stream",
            return_value={
                "is_attached_pic": False,
                "filename": "DejaVuSans.ttf",
                "mimetype": "application/x-truetype-font",
            },
        ):
            cmd = self.wf.build_command_single(
                _make_config(src, tmp_path / "out.mkv",
                             attachment_streams=att_streams,
                             extra_attachments=extras)
            )
        assert "filename=DejaVuSans.ttf" in cmd
        assert "-metadata:s:t:1" in cmd
        indices = [i for i, a in enumerate(cmd) if a == "-metadata:s:t:1"]
        assert any(cmd[i + 1] == "filename=cover" for i in indices)

    def test_single_attachment_streams_write_filename_and_mimetype(self, tmp_path):
        src = self._src(tmp_path)
        cfg = _make_config(
            src,
            tmp_path / "out.mkv",
            attachment_streams=[(src, 5)],
        )
        with patch.object(
            self.wf,
            "_describe_attachment_stream",
            return_value={
                "is_attached_pic": False,
                "filename": "DejaVuSans.ttf",
                "mimetype": "application/x-truetype-font",
            },
        ):
            cmd = self.wf.build_command_single(cfg)

        assert "mimetype=application/x-truetype-font" in cmd
        assert "filename=DejaVuSans.ttf" in cmd

    # ── two-pass pass2 ───────────────────────────────────────────────────────

    def test_pass2_attach_flag_present(self, tmp_path):
        """-attach présent dans la passe 2 (mode SIZE)."""
        cmd = self._pass2(tmp_path, [tmp_path / "poster.jpg"])
        assert "-attach" in cmd

    def test_pass2_attach_mimetype(self, tmp_path):
        """-metadata:s:t:0 mimetype=image/jpeg dans la passe 2."""
        cmd = self._pass2(tmp_path, [tmp_path / "cover.jpg"])
        assert "-metadata:s:t:0" in cmd
        idx = cmd.index("-metadata:s:t:0")
        assert cmd[idx + 1] == "mimetype=image/jpeg"

    def test_pass2_cover_filename(self, tmp_path):
        """-metadata:s:t:0 filename=cover dans la passe 2 pour cover.*."""
        cmd = self._pass2(tmp_path, [tmp_path / "cover.jpg"])
        indices = [i for i, a in enumerate(cmd) if a == "-metadata:s:t:0"]
        filename_val = cmd[indices[1] + 1]
        assert filename_val == "filename=cover"

    def test_pass1_no_attach(self, tmp_path):
        """-attach absent de la passe 1 (analyse seule, pas de sortie)."""
        src = self._src(tmp_path)
        vs = _make_video_settings(quality_mode=QualityMode.SIZE)
        cmds = self.wf.build_command(
            _make_config(src, tmp_path / "out.mkv", video=vs,
                         duration_s=3600.0, extra_attachments=[tmp_path / "cover.jpg"])
        )
        pass1 = cmds[0]
        assert "-attach" not in pass1

    # ── reconstitution finale (_run_with_metadata_inject) ────────────────────

    def _run_inject_with_extras(
        self, tmp_path: Path, extras: list[Path], ffmpeg_threads: int | None = None
    ) -> list[list[str]]:
        """Lance _run_with_metadata_inject (libx265 + copy_dv) avec extra_attachments."""
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        config = _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="libx265"),
            copy_dv=True,
            work_dir=tmp_path / "work",
            extra_attachments=extras,
        )
        wf = _make_workflow(enabled=False, ffmpeg_threads=ffmpeg_threads)
        cmds_run: list[list[str]] = []

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            cmds_run.append(list(cmd))
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 100_000)
                    break
            return ""

        with patch.object(EncodeWorkflow, "_shm_path",
                          side_effect=lambda tmp_dir, name, _size: tmp_dir / name):
            with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
                sigs = wf._run_with_metadata_inject(config)
                _collect_signals(sigs)

        return cmds_run

    def _get_recon_cmd(self, cmds: list[list[str]]) -> list[str]:
        recon = [c for c in cmds if c[0] == "ffmpeg" and "output.mkv" in " ".join(c)]
        assert len(recon) == 1
        return recon[0]

    def test_inject_attach_flag_present(self, tmp_path):
        """-attach présent dans la reconstitution finale (chemin DV/HDR10+)."""
        extras = [tmp_path / "cover.jpg"]
        cmds = self._run_inject_with_extras(tmp_path, extras)
        recon = self._get_recon_cmd(cmds)
        assert "-attach" in recon

    def test_inject_reconstruction_includes_threads(self, tmp_path):
        cmds = self._run_inject_with_extras(tmp_path, [], ffmpeg_threads=9)
        recon = self._get_recon_cmd(cmds)
        assert recon[recon.index("-threads") + 1] == "9"

    def test_inject_cover_filename(self, tmp_path):
        """filename=cover dans la reconstitution finale pour cover.*."""
        extras = [tmp_path / "cover.jpg"]
        cmds = self._run_inject_with_extras(tmp_path, extras)
        recon = self._get_recon_cmd(cmds)
        indices = [i for i, a in enumerate(recon) if a == "-metadata:s:t:0"]
        assert any(recon[i + 1] == "filename=cover" for i in indices)

    def test_inject_no_attach_when_empty(self, tmp_path):
        """extra_attachments=[] → aucun -attach dans la reconstitution."""
        cmds = self._run_inject_with_extras(tmp_path, [])
        recon = self._get_recon_cmd(cmds)
        assert "-attach" not in recon


# ===========================================================================
# Sync timeline multi-source (encode runtime)
# ===========================================================================

class TestEncodeRuntimeMultiSourceSync:

    def test_runtime_single_pass_uses_sync_remap_and_strict_flags(self, tmp_path):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        sync_audio = tmp_path / "sync_audio.mka"
        src_main.touch()
        src_alt.touch()
        sync_audio.touch()

        wf = _make_workflow()
        cfg = _make_config(
            src_main,
            out,
            video=_make_video_settings(codec="libx265"),
            audio_tracks=[
                AudioTrackSettings(stream_index=1, codec="copy", source_path=src_alt),
            ],
            subtitle_tracks=[(src_main, 3)],
        )

        with patch.object(
            wf,
            "_prepare_multisource_sync",
            return_value=(
                {(src_alt, 1, "audio"): (2, 0)},
                [sync_audio],
                None,
                True,
            ),
        ):
            cmd, live, cleanup = wf._build_runtime_single_pass_with_sync(cfg)

        assert live is None
        assert cleanup == [sync_audio]
        assert str(sync_audio) in cmd
        sync_i = cmd.index(str(sync_audio))
        assert cmd[sync_i - 1] == "-i"
        assert cmd[sync_i - 2] == "matroska"
        assert cmd[sync_i - 3] == "-f"
        assert "-max_interleave_delta" in cmd
        assert "-max_muxing_queue_size" in cmd
        map_values = [cmd[i + 1] for i, tok in enumerate(cmd[:-1]) if tok == "-map"]
        assert "2:0" in map_values

    def test_runtime_single_pass_sync_rewrite_consumes_audio_offset(self, tmp_path, monkeypatch):
        src = tmp_path / "main.mkv"
        out = tmp_path / "out.mkv"
        rewritten = tmp_path / "rewritten.mka"
        src.touch()

        wf = _make_workflow()
        wf.set_sync_rewrite_enabled(True)
        cfg = _make_config(
            src,
            out,
            video=_make_video_settings(codec="libx265"),
            audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy", source_path=src)],
            copy_subtitles=False,
            track_time_offsets=[
                TrackTimeOffset(track_type="audio", source_path=src, stream_index=1, offset_ms=125),
            ],
        )
        captured: dict[str, object] = {}

        class FakeSyncRewriteService:
            def __init__(self, **_kwargs):
                pass

            def maybe_materialize(self, **kwargs):
                rewritten.write_bytes(b"audio")
                captured["rewrite_kwargs"] = kwargs
                return SyncRewritePreparedInput(
                    path=rewritten,
                    input_idx=int(kwargs["input_idx"]),
                    track_type="audio",
                    codec="eac3",
                    mode_label="Sync réelle · audio réencodé",
                    bitrate_kbps=640,
                )

        monkeypatch.setattr(
            "core.workflows.encode.workflow.SyncRewriteService",
            FakeSyncRewriteService,
        )

        with patch.object(wf, "_prepare_multisource_sync", return_value=({}, [], None, False)), \
             patch.object(wf, "_stream_codec_of", return_value="eac3"):
            cmd, live, cleanup = wf._build_runtime_single_pass_with_sync(cfg)

        assert live is None
        assert cleanup == []
        assert "-itsoffset" not in cmd
        assert str(rewritten) in cmd
        map_values = [cmd[i + 1] for i, tok in enumerate(cmd[:-1]) if tok == "-map"]
        assert "1:0" in map_values
        rewrite_kwargs = cast(dict, captured["rewrite_kwargs"])
        assert rewrite_kwargs["preserve_source_audio_params"] is True

    def test_runtime_single_pass_sync_rewrite_respects_forced_standard_offset(self, tmp_path, monkeypatch):
        src = tmp_path / "main.mkv"
        out = tmp_path / "out.mkv"
        src.touch()

        wf = _make_workflow()
        wf.set_sync_rewrite_enabled(True)
        cfg = _make_config(
            src,
            out,
            video=_make_video_settings(codec="libx265"),
            audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy", source_path=src)],
            copy_subtitles=False,
            track_time_offsets=[
                TrackTimeOffset(
                    track_type="audio",
                    source_path=src,
                    stream_index=1,
                    offset_ms=125,
                    sync_rewrite_mode="offset",
                ),
            ],
        )

        class FakeSyncRewriteService:
            def __init__(self, **_kwargs):
                pass

            def maybe_materialize(self, **_kwargs):
                raise AssertionError("forced standard offset must not use sync rewrite")

        monkeypatch.setattr(
            "core.workflows.encode.workflow.SyncRewriteService",
            FakeSyncRewriteService,
        )

        with patch.object(wf, "_prepare_multisource_sync", return_value=({}, [], None, False)):
            cmd, live, cleanup = wf._build_runtime_single_pass_with_sync(cfg)

        assert live is None
        assert cleanup == []
        assert "-itsoffset" in cmd
        assert cmd[cmd.index("-itsoffset") + 1] == "0.125"

    def test_runtime_two_pass_disables_live_and_applies_sync_to_pass2_only(self, tmp_path):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        sync_audio = tmp_path / "sync_audio.mka"
        src_main.touch()
        src_alt.touch()
        sync_audio.touch()

        wf = _make_workflow()
        cfg = _make_config(
            src_main,
            out,
            video=_make_video_settings(codec="libx265", quality_mode=QualityMode.SIZE),
            duration_s=3600.0,
            audio_tracks=[
                AudioTrackSettings(stream_index=1, codec="copy", source_path=src_alt),
            ],
            subtitle_tracks=[(src_main, 3)],
        )

        recorded_allow_live: list[bool] = []

        def _fake_prepare(**kwargs):
            recorded_allow_live.append(bool(kwargs.get("allow_live")))
            return (
                {(src_alt, 1, "audio"): (2, 0)},
                [sync_audio],
                None,
                True,
            )

        with patch.object(wf, "_prepare_multisource_sync", side_effect=_fake_prepare):
            cmds, live, cleanup = wf._build_runtime_two_pass_with_sync(cfg)

        assert live is None
        assert cleanup == [sync_audio]
        assert recorded_allow_live == [False]

        pass1, pass2 = cmds
        assert str(sync_audio) not in pass1
        assert str(sync_audio) in pass2
        assert "-max_interleave_delta" not in pass1
        assert "-max_interleave_delta" in pass2
        map_values_pass2 = [pass2[i + 1] for i, tok in enumerate(pass2[:-1]) if tok == "-map"]
        assert "2:0" in map_values_pass2

    def test_metadata_inject_reconstruction_uses_sync_inputs(self, tmp_path):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        sync_audio = tmp_path / "sync_audio.mka"
        src_main.write_bytes(b"\x00" * 200_000)
        src_alt.write_bytes(b"\x00" * 100_000)
        sync_audio.touch()

        wf = _make_workflow(enabled=False)
        cfg = _make_config(
            source=src_main,
            output=out,
            video=_make_video_settings(codec="libx265"),
            copy_dv=True,
            work_dir=tmp_path / "work",
            audio_tracks=[
                AudioTrackSettings(stream_index=1, codec="copy", source_path=src_alt),
            ],
            subtitle_tracks=[(src_main, 3)],
        )

        ran_cmds: list[list[str]] = []

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            ran_cmds.append(list(cmd))
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 100_000)
                    break
            return ""

        with patch.object(
            wf,
            "_prepare_multisource_sync",
            return_value=(
                {(src_alt, 1, "audio"): (3, 0)},
                [sync_audio],
                None,
                True,
            ),
        ):
            with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
                sigs = wf._run_with_metadata_inject(cfg)
                _collect_signals(sigs)

        recon = [c for c in ran_cmds if str(out) in [str(x) for x in c]]
        assert len(recon) == 1
        cmd = recon[0]
        assert str(sync_audio) in cmd
        sync_i = cmd.index(str(sync_audio))
        assert cmd[sync_i - 1] == "-i"
        assert cmd[sync_i - 2] == "matroska"
        assert cmd[sync_i - 3] == "-f"
        assert "-max_interleave_delta" in cmd
        assert "-max_muxing_queue_size" in cmd
        map_values = [cmd[i + 1] for i, tok in enumerate(cmd[:-1]) if tok == "-map"]
        assert "3:0" in map_values

    def test_metadata_inject_reconstruction_applies_offsets_after_sync_remap(self, tmp_path):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        sync_audio = tmp_path / "sync_audio.mka"
        src_main.write_bytes(b"\x00" * 200_000)
        src_alt.write_bytes(b"\x00" * 100_000)
        sync_audio.touch()

        wf = _make_workflow(enabled=False)
        cfg = _make_config(
            source=src_main,
            output=out,
            video=_make_video_settings(codec="libx265"),
            copy_dv=True,
            work_dir=tmp_path / "work",
            audio_tracks=[
                AudioTrackSettings(stream_index=1, codec="copy", source_path=src_alt),
            ],
            subtitle_tracks=[(src_main, 3)],
            track_time_offsets=[
                TrackTimeOffset(track_type="audio", source_path=src_alt, stream_index=1, offset_ms=120),
            ],
        )

        ran_cmds: list[list[str]] = []

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            ran_cmds.append(list(cmd))
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 100_000)
                    break
            return ""

        with patch.object(
            wf,
            "_prepare_multisource_sync",
            return_value=(
                {(src_alt, 1, "audio"): (3, 0)},
                [sync_audio],
                None,
                True,
            ),
        ):
            with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
                sigs = wf._run_with_metadata_inject(cfg)
                _collect_signals(sigs)

        recon = [c for c in ran_cmds if str(out) in [str(x) for x in c]]
        assert len(recon) == 1
        cmd = recon[0]
        assert "-itsoffset" in cmd
        assert cmd[cmd.index("-itsoffset") + 1] == "0.120"
        map_values = [cmd[i + 1] for i, tok in enumerate(cmd[:-1]) if tok == "-map"]
        assert "4:0" in map_values

    def test_prepare_multisource_sync_prefers_live_when_available(self, tmp_path, monkeypatch):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        sync_audio = tmp_path / "sync_audio.mka"
        src_main.touch()
        src_alt.touch()
        sync_audio.touch()

        wf = _make_workflow()
        cfg = _make_config(
            src_main,
            out,
            video=_make_video_settings(codec="copy"),
            audio_tracks=[
                AudioTrackSettings(stream_index=1, codec="copy", source_path=src_alt),
            ],
            subtitle_tracks=[(src_main, 3)],
        )

        calls = {"live": 0, "file": 0}

        class _FakeSyncer:
            def __init__(self, **_kwargs):
                pass

            def start_live_demux_session(self, **_kwargs):
                calls["live"] += 1
                return SimpleNamespace(inputs=[SimpleNamespace(key=(1, 1, "audio"), path=sync_audio, input_idx=2)])

            def prepare_from_mapped_tracks(self, **_kwargs):
                calls["file"] += 1
                return [SimpleNamespace(key=(1, 1, "audio"), path=sync_audio, input_idx=2)]

            def prepare_from_mapped_tracks_mmap(self, **_kwargs):
                calls["file"] += 1
                return [SimpleNamespace(key=(1, 1, "audio"), path=sync_audio, input_idx=2)]

        # The syncer is fully stubbed here; patching os.name would leak into pathlib
        # on Windows if the assertion fails, which breaks pytest error reporting.
        monkeypatch.setattr("core.workflows.encode.workflow.FfmpegTimelineSync", _FakeSyncer)

        remap, sync_inputs, live, strict = wf._prepare_multisource_sync(
            config=cfg,
            all_sources=[src_main, src_alt],
            sync_base_input_idx=2,
            work_dir=tmp_path,
            allow_live=True,
        )

        assert calls["live"] == 1
        assert calls["file"] == 0
        assert live is not None
        assert strict is True
        assert sync_inputs == [sync_audio]
        assert remap[(src_alt, 1, "audio")] == (2, 0)

    def test_prepare_multisource_sync_fallback_prefers_ram_before_disk(self, tmp_path, monkeypatch):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        ram_dir = tmp_path / "ram"
        ram_dir.mkdir()
        sync_audio = ram_dir / "sync_audio.mka"
        src_main.touch()
        src_alt.touch()

        wf = _make_workflow()
        cfg = _make_config(
            src_main,
            out,
            video=_make_video_settings(codec="copy"),
            audio_tracks=[
                AudioTrackSettings(stream_index=1, codec="copy", source_path=src_alt),
            ],
            subtitle_tracks=[(src_main, 3)],
        )

        calls: list[Path] = []

        class _FakeSyncer:
            def __init__(self, **_kwargs):
                pass

            def start_live_demux_session(self, **_kwargs):
                raise RuntimeError("live unavailable")

            def prepare_from_mapped_tracks_mmap(self, **_kwargs):
                tmp_dir = Path(_kwargs["tmp_dir"])
                calls.append(tmp_dir)
                return [SimpleNamespace(key=(1, 1, "audio"), path=sync_audio, input_idx=2)]

            def prepare_from_mapped_tracks(self, **_kwargs):
                pytest.fail("file fallback should not be used when RAM mmap works")

        monkeypatch.setattr("core.workflows.encode.workflow.FfmpegTimelineSync", _FakeSyncer)
        monkeypatch.setattr(EncodeWorkflow, "_ram_buffer_dir", staticmethod(lambda: ram_dir))

        remap, sync_inputs, live, strict = wf._prepare_multisource_sync(
            config=cfg,
            all_sources=[src_main, src_alt],
            sync_base_input_idx=2,
            work_dir=tmp_path,
            allow_live=True,
        )

        assert calls == [ram_dir]
        assert live is None
        assert strict is True
        assert sync_inputs == [sync_audio]
        assert remap[(src_alt, 1, "audio")] == (2, 0)

    def test_runtime_single_pass_copy_subtitles_uses_sync_remap_when_resolved(self, tmp_path):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        sync_sub = tmp_path / "sync_sub.mks"
        src_main.touch()
        src_alt.touch()
        sync_sub.touch()

        wf = _make_workflow()
        cfg = _make_config(
            src_main,
            out,
            video=_make_video_settings(codec="copy"),
            copy_subtitles=True,
            subtitle_tracks=[],
        )

        with patch.object(
            wf,
            "_prepare_multisource_sync",
            return_value=(
                {(src_alt, 4, "subtitle"): (2, 0)},
                [sync_sub],
                None,
                True,
            ),
        ), patch.object(
            wf,
            "_resolved_subtitle_tracks_for_encode",
            return_value=([(src_alt, 4)], True),
        ):
            cmd, _live, _cleanup = wf._build_runtime_single_pass_with_sync(cfg)

        map_values = [cmd[i + 1] for i, tok in enumerate(cmd[:-1]) if tok == "-map"]
        assert "2:0" in map_values
        assert all(":s?" not in mv for mv in map_values)
        assert "-c:s" in cmd and cmd[cmd.index("-c:s") + 1] == "copy"

    def test_runtime_single_pass_copy_subtitles_falls_back_to_wildcard_when_unresolved(self, tmp_path):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        src_main.touch()
        src_alt.touch()

        wf = _make_workflow()
        cfg = _make_config(
            src_main,
            out,
            video=_make_video_settings(codec="copy"),
            copy_subtitles=True,
            subtitle_tracks=[],
            audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy", source_path=src_alt)],
        )

        with patch.object(
            wf,
            "_prepare_multisource_sync",
            return_value=({}, [], None, False),
        ), patch.object(
            wf,
            "_resolved_subtitle_tracks_for_encode",
            return_value=([], False),
        ):
            cmd, _live, _cleanup = wf._build_runtime_single_pass_with_sync(cfg)

        map_values = [cmd[i + 1] for i, tok in enumerate(cmd[:-1]) if tok == "-map"]
        assert "0:s?" in map_values
        assert "1:s?" in map_values
        assert "-c:s" in cmd and cmd[cmd.index("-c:s") + 1] == "copy"

    def test_build_single_pass_applies_video_audio_subtitle_offsets(self, tmp_path):
        src = tmp_path / "src.mkv"
        out = tmp_path / "out.mkv"
        src.touch()

        wf = _make_workflow()
        cfg = _make_config(
            src,
            out,
            video=_make_video_settings(codec="libx265"),
            audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy", source_path=src)],
            subtitle_tracks=[(src, 3)],
            copy_subtitles=False,
            track_time_offsets=[
                TrackTimeOffset(track_type="video", source_path=src, stream_index=0, offset_ms=40),
                TrackTimeOffset(track_type="audio", source_path=src, stream_index=1, offset_ms=125),
                TrackTimeOffset(track_type="subtitle", source_path=src, stream_index=3, offset_ms=-80),
            ],
        )

        cmd = wf.build_command_single(cfg)

        assert "-itsoffset" in cmd
        assert "0.040" in cmd
        assert "0.125" in cmd
        assert "-ss" in cmd
        assert "0.080" in cmd
        map_values = [cmd[i + 1] for i, tok in enumerate(cmd[:-1]) if tok == "-map"]
        assert "1:0" in map_values
        assert "2:1" in map_values
        assert "3:3" in map_values

    def test_runtime_two_pass_applies_offsets_after_sync_remap(self, tmp_path):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        sync_audio = tmp_path / "sync_audio.mka"
        src_main.touch()
        src_alt.touch()
        sync_audio.touch()

        wf = _make_workflow()
        cfg = _make_config(
            src_main,
            out,
            video=_make_video_settings(codec="libx265", quality_mode=QualityMode.SIZE),
            duration_s=3600.0,
            audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy", source_path=src_alt)],
            subtitle_tracks=[(src_main, 3)],
            track_time_offsets=[
                TrackTimeOffset(track_type="audio", source_path=src_alt, stream_index=1, offset_ms=150),
            ],
        )

        with patch.object(
            wf,
            "_prepare_multisource_sync",
            return_value=(
                {(src_alt, 1, "audio"): (2, 0)},
                [sync_audio],
                None,
                True,
            ),
        ):
            cmds, _live, _cleanup = wf._build_runtime_two_pass_with_sync(cfg)

        pass2 = cmds[1]
        assert "-itsoffset" in pass2
        assert pass2[pass2.index("-itsoffset") + 1] == "0.150"
        map_values = [pass2[i + 1] for i, tok in enumerate(pass2[:-1]) if tok == "-map"]
        assert "3:0" in map_values

    def test_run_multi_video_pipeline_applies_audio_and_subtitle_offsets(self, tmp_path):
        src = tmp_path / "src.mkv"
        out = tmp_path / "out.mkv"
        src.touch()

        video_a = _make_video_settings(codec="copy", source_path=src, stream_index=0)
        video_b = _make_video_settings(codec="copy", source_path=src, stream_index=1)
        cfg = _make_config(
            src,
            out,
            video=video_a,
            video_tracks=[video_a, video_b],
            audio_tracks=[AudioTrackSettings(stream_index=2, codec="copy", source_path=src)],
            subtitle_tracks=[(src, 3)],
            copy_subtitles=False,
            track_time_offsets=[
                TrackTimeOffset(track_type="audio", source_path=src, stream_index=2, offset_ms=120),
                TrackTimeOffset(track_type="subtitle", source_path=src, stream_index=3, offset_ms=-80),
            ],
        )

        wf = _make_workflow()
        captured: list[list[str]] = []

        def _fake_run_cmd(cmd, **_kwargs):
            captured.append(list(cmd))
            return "ok"

        with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run_cmd), patch.object(
            wf,
            "_prepare_multisource_sync",
            return_value=({}, [], None, False),
        ):
            wf._run_multi_video_pipeline(cfg, cleanup_paths=[], prep_signals=TaskSignals())

        final_cmd = captured[-1]
        assert "-itsoffset" in final_cmd
        assert final_cmd[final_cmd.index("-itsoffset") + 1] == "0.120"
        assert "-ss" in final_cmd
        assert final_cmd[final_cmd.index("-ss") + 1] == "0.080"
        map_values = [final_cmd[i + 1] for i, tok in enumerate(final_cmd[:-1]) if tok == "-map"]
        assert "3:2" in map_values
        assert "4:3" in map_values

    def test_run_multi_video_pipeline_keeps_video_offsets_stream_specific(self, tmp_path):
        src = tmp_path / "src.mkv"
        out = tmp_path / "out.mkv"
        src.touch()

        video_a = _make_video_settings(codec="copy", source_path=src, stream_index=0)
        video_b = _make_video_settings(codec="copy", source_path=src, stream_index=1)
        cfg = _make_config(
            src,
            out,
            video=video_a,
            video_tracks=[video_a, video_b],
            audio_tracks=[],
            subtitle_tracks=[],
            copy_subtitles=False,
            track_time_offsets=[
                TrackTimeOffset(track_type="video", source_path=src, stream_index=1, offset_ms=1000),
            ],
        )

        wf = _make_workflow()
        captured: list[list[str]] = []

        def _fake_run_cmd(cmd, **_kwargs):
            captured.append(list(cmd))
            return "ok"

        with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run_cmd), patch.object(
            wf,
            "_prepare_multisource_sync",
            return_value=({}, [], None, False),
        ):
            wf._run_multi_video_pipeline(cfg, cleanup_paths=[], prep_signals=TaskSignals())

        final_cmd = captured[-1]
        assert final_cmd.count("-itsoffset") == 1
        assert final_cmd[final_cmd.index("-itsoffset") + 1] == "1.000"
        map_values = [final_cmd[i + 1] for i, tok in enumerate(final_cmd[:-1]) if tok == "-map"]
        assert "0:0" in map_values
        assert "1:1" in map_values

    def test_run_with_preparation_multi_video_binds_post_actions(self, tmp_path):
        src = tmp_path / "src.mkv"
        out = tmp_path / "out.mkv"
        src.touch()
        work_dir = tmp_path / "work"
        work_dir.mkdir()

        video_a = _make_video_settings(codec="copy", source_path=src, stream_index=0)
        video_b = _make_video_settings(codec="copy", source_path=src, stream_index=1)
        cfg = _make_config(
            src,
            out,
            video=video_a,
            video_tracks=[video_a, video_b],
            audio_tracks=[],
            copy_subtitles=False,
        )

        wf = _make_workflow()
        prep_signals = TaskSignals()
        inner_signals = TaskSignals()
        logs: list[tuple[str, str]] = []
        wf.log_message.connect(lambda level, message: logs.append((str(level), str(message))))

        with patch(
            "core.workflows.encode.workflow.prepare_process_work_dir",
            return_value=work_dir,
        ), patch(
            "core.workflows.encode.workflow.relocate_tmdb_covers_to_process_dir",
            return_value=[],
        ), patch.object(
            wf,
            "_prepare_attachment_config",
            return_value=(cfg, None),
        ), patch.object(
            wf,
            "_run_multi_video_pipeline",
            return_value=inner_signals,
        ) as run_multi, patch.object(
            wf,
            "_bind_temp_cleanup",
        ) as bind_cleanup, patch.object(
            wf,
            "_bind_matroska_segment_muxing_patch",
        ) as bind_mux, patch.object(
            wf,
            "_bind_nfo_write",
        ) as bind_nfo:
            result = wf._run_with_preparation(cfg, validate=False, prep_signals=prep_signals)

        assert result is inner_signals
        run_multi.assert_called_once()
        assert bind_cleanup.call_count == 2
        bind_cleanup.assert_any_call(prep_signals, [work_dir])
        bind_cleanup.assert_any_call(inner_signals, [work_dir])
        assert bind_mux.call_count == 2
        bind_mux.assert_any_call(prep_signals, out)
        bind_mux.assert_any_call(inner_signals, out)
        assert bind_nfo.call_count == 2
        bind_nfo.assert_any_call(prep_signals, out)
        bind_nfo.assert_any_call(inner_signals, out)
        assert any(
            level == "INFO"
            and message == "STEP 4 - Routage du workflow (pipeline multi-pistes vidéo)"
            for level, message in logs
        )

    def test_run_with_preparation_prebinds_hooks_before_sync_multi_video_pipeline(self, tmp_path):
        src = tmp_path / "src.mkv"
        out = tmp_path / "out.mkv"
        src.touch()
        work_dir = tmp_path / "work"
        work_dir.mkdir()

        video_a = _make_video_settings(codec="copy", source_path=src, stream_index=0)
        video_b = _make_video_settings(codec="copy", source_path=src, stream_index=1)
        cfg = _make_config(
            src,
            out,
            video=video_a,
            video_tracks=[video_a, video_b],
            audio_tracks=[],
            copy_subtitles=False,
        )

        wf = _make_workflow()
        prep_signals = TaskSignals()
        call_order: list[str] = []

        def _fake_bind_cleanup(signals, _paths):
            assert signals is prep_signals
            call_order.append("bind_cleanup")

        def _fake_bind_mux(signals, _out):
            assert signals is prep_signals
            call_order.append("bind_mux")

        def _fake_bind_nfo(signals, _out):
            assert signals is prep_signals
            call_order.append("bind_nfo")

        def _fake_run_multi(_cfg, _cleanup_paths, *, prep_signals=None, plan=None):
            assert prep_signals is prep_signals_ref
            assert plan is not None
            call_order.append("run_multi")
            assert call_order[:3] == ["bind_cleanup", "bind_mux", "bind_nfo"]
            prep_signals_ref.finished.emit("done")
            return prep_signals_ref

        prep_signals_ref = prep_signals

        with patch(
            "core.workflows.encode.workflow.prepare_process_work_dir",
            return_value=work_dir,
        ), patch(
            "core.workflows.encode.workflow.relocate_tmdb_covers_to_process_dir",
            return_value=[],
        ), patch.object(
            wf,
            "_prepare_attachment_config",
            return_value=(cfg, None),
        ), patch.object(
            wf,
            "_run_multi_video_pipeline",
            side_effect=_fake_run_multi,
        ), patch.object(
            wf,
            "_bind_temp_cleanup",
            side_effect=_fake_bind_cleanup,
        ), patch.object(
            wf,
            "_bind_matroska_segment_muxing_patch",
            side_effect=_fake_bind_mux,
        ), patch.object(
            wf,
            "_bind_nfo_write",
            side_effect=_fake_bind_nfo,
        ):
            result = wf._run_with_preparation(cfg, validate=False, prep_signals=prep_signals)

        assert result is prep_signals
        assert call_order[:4] == ["bind_cleanup", "bind_mux", "bind_nfo", "run_multi"]

    def test_run_with_preparation_prebinds_hooks_before_sync_direct_output(self, tmp_path):
        src = tmp_path / "src.mkv"
        out = tmp_path / "out.mkv"
        src.touch()
        work_dir = tmp_path / "work"
        work_dir.mkdir()

        video = _make_video_settings(codec="libx265", source_path=src, stream_index=0)
        cfg = _make_config(
            src,
            out,
            video=video,
            video_tracks=[video],
            audio_tracks=[],
            copy_subtitles=False,
        )

        wf = _make_workflow()
        prep_signals = TaskSignals()
        call_order: list[str] = []

        def _fake_bind_cleanup(signals, _paths):
            assert signals is prep_signals
            call_order.append("bind_cleanup")

        def _fake_bind_mux(signals, _out):
            assert signals is prep_signals
            call_order.append("bind_mux")

        def _fake_bind_nfo(signals, _out):
            assert signals is prep_signals
            call_order.append("bind_nfo")

        def _fake_run_direct(_cfg, _cleanup_paths, *, prep_signals=None, plan=None):
            assert prep_signals is prep_signals_ref
            assert plan is not None
            call_order.append("run_direct")
            assert call_order[:3] == ["bind_cleanup", "bind_mux", "bind_nfo"]
            prep_signals_ref.finished.emit("done")
            return prep_signals_ref

        prep_signals_ref = prep_signals

        with patch(
            "core.workflows.encode.workflow.prepare_process_work_dir",
            return_value=work_dir,
        ), patch(
            "core.workflows.encode.workflow.relocate_tmdb_covers_to_process_dir",
            return_value=[],
        ), patch.object(
            wf,
            "_prepare_attachment_config",
            return_value=(cfg, None),
        ), patch.object(
            wf,
            "_run_direct_output",
            side_effect=_fake_run_direct,
        ), patch.object(
            wf,
            "_bind_temp_cleanup",
            side_effect=_fake_bind_cleanup,
        ), patch.object(
            wf,
            "_bind_matroska_segment_muxing_patch",
            side_effect=_fake_bind_mux,
        ), patch.object(
            wf,
            "_bind_nfo_write",
            side_effect=_fake_bind_nfo,
        ):
            result = wf._run_with_preparation(cfg, validate=False, prep_signals=prep_signals)

        assert result is prep_signals
        assert call_order[:4] == ["bind_cleanup", "bind_mux", "bind_nfo", "run_direct"]

    def test_run_multi_video_pipeline_serializes_same_resource(self, tmp_path):
        src = tmp_path / "src.mkv"
        out = tmp_path / "out.mkv"
        src.touch()

        video_a = _make_video_settings(codec="libx264", source_path=src, stream_index=0)
        video_b = _make_video_settings(codec="libx265", source_path=src, stream_index=1)
        cfg = _make_config(
            src,
            out,
            video=video_a,
            video_tracks=[video_a, video_b],
            audio_tracks=[],
            subtitle_tracks=[],
            copy_subtitles=False,
        )

        wf = _make_workflow(max_parallel_video_encodes=2)
        active = 0
        max_active = 0
        gate = threading.Lock()

        def _fake_run_cmd(_cmd, **kwargs):
            nonlocal active, max_active
            label = str(kwargs.get("label", ""))
            if label.startswith("ffmpeg-video-"):
                with gate:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.06)
                with gate:
                    active -= 1
            return "ok"

        with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run_cmd), patch.object(
            wf,
            "_prepare_multisource_sync",
            return_value=({}, [], None, False),
        ):
            wf._run_multi_video_pipeline(cfg, cleanup_paths=[], prep_signals=TaskSignals())

        assert max_active == 1

    def test_run_multi_video_pipeline_parallelizes_disjoint_resources(self, tmp_path):
        src = tmp_path / "src.mkv"
        out = tmp_path / "out.mkv"
        src.touch()

        video_a = _make_video_settings(codec="libx264", source_path=src, stream_index=0)
        video_b = _make_video_settings(codec="h264_nvenc", source_path=src, stream_index=1)
        cfg = _make_config(
            src,
            out,
            video=video_a,
            video_tracks=[video_a, video_b],
            audio_tracks=[],
            subtitle_tracks=[],
            copy_subtitles=False,
        )

        wf = _make_workflow(max_parallel_video_encodes=2)
        active = 0
        max_active = 0
        gate = threading.Lock()

        def _fake_run_cmd(_cmd, **kwargs):
            nonlocal active, max_active
            label = str(kwargs.get("label", ""))
            if label.startswith("ffmpeg-video-"):
                with gate:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.06)
                with gate:
                    active -= 1
            return "ok"

        with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run_cmd), patch.object(
            wf,
            "_prepare_multisource_sync",
            return_value=({}, [], None, False),
        ):
            wf._run_multi_video_pipeline(cfg, cleanup_paths=[], prep_signals=TaskSignals())

        assert max_active >= 2

    def test_run_multi_video_pipeline_scales_threads_for_disjoint_resources(self, tmp_path):
        src = tmp_path / "src.mkv"
        out = tmp_path / "out.mkv"
        src.touch()

        video_a = _make_video_settings(codec="libx264", source_path=src, stream_index=0)
        video_b = _make_video_settings(codec="h264_nvenc", source_path=src, stream_index=1)
        cfg = _make_config(
            src,
            out,
            video=video_a,
            video_tracks=[video_a, video_b],
            audio_tracks=[],
            subtitle_tracks=[],
            copy_subtitles=False,
        )

        wf = _make_workflow(ffmpeg_threads=12, max_parallel_video_encodes=2)
        commands_by_label: dict[str, list[str]] = {}

        def _fake_run_cmd(cmd, **kwargs):
            label = str(kwargs.get("label", ""))
            commands_by_label[label] = list(cmd)
            return "ok"

        with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run_cmd), patch.object(
            wf,
            "_prepare_multisource_sync",
            return_value=({}, [], None, False),
        ):
            wf._run_multi_video_pipeline(cfg, cleanup_paths=[], prep_signals=TaskSignals())

        assert commands_by_label["ffmpeg-video-1"][commands_by_label["ffmpeg-video-1"].index("-threads") + 1] == "6"
        assert commands_by_label["ffmpeg-video-2"][commands_by_label["ffmpeg-video-2"].index("-threads") + 1] == "6"
        assert commands_by_label["ffmpeg-multi-video"][commands_by_label["ffmpeg-multi-video"].index("-threads") + 1] == "12"

    def test_run_multi_video_pipeline_keeps_full_threads_for_serialized_resource(self, tmp_path):
        src = tmp_path / "src.mkv"
        out = tmp_path / "out.mkv"
        src.touch()

        video_a = _make_video_settings(codec="libx264", source_path=src, stream_index=0)
        video_b = _make_video_settings(codec="libx265", source_path=src, stream_index=1)
        cfg = _make_config(
            src,
            out,
            video=video_a,
            video_tracks=[video_a, video_b],
            audio_tracks=[],
            subtitle_tracks=[],
            copy_subtitles=False,
        )

        wf = _make_workflow(ffmpeg_threads=12, max_parallel_video_encodes=2)
        commands_by_label: dict[str, list[str]] = {}

        def _fake_run_cmd(cmd, **kwargs):
            label = str(kwargs.get("label", ""))
            commands_by_label[label] = list(cmd)
            return "ok"

        with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run_cmd), patch.object(
            wf,
            "_prepare_multisource_sync",
            return_value=({}, [], None, False),
        ):
            wf._run_multi_video_pipeline(cfg, cleanup_paths=[], prep_signals=TaskSignals())

        assert commands_by_label["ffmpeg-video-1"][commands_by_label["ffmpeg-video-1"].index("-threads") + 1] == "12"
        assert commands_by_label["ffmpeg-video-2"][commands_by_label["ffmpeg-video-2"].index("-threads") + 1] == "12"

    def test_prepare_multisource_sync_disables_live_when_foreign_offset(self, tmp_path, monkeypatch):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        src_main.touch()
        src_alt.touch()

        wf = _make_workflow()
        cfg = _make_config(
            src_main,
            out,
            video=_make_video_settings(codec="copy"),
            audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy", source_path=src_alt)],
            subtitle_tracks=[(src_main, 3)],
            track_time_offsets=[
                TrackTimeOffset(track_type="audio", source_path=src_alt, stream_index=1, offset_ms=120),
            ],
        )

        recorded_allow_live: list[bool] = []

        class _FakeHelper:
            def __init__(self, **_kwargs):
                pass

            def prepare(self, **kwargs):
                recorded_allow_live.append(bool(kwargs.get("allow_live")))
                return SimpleNamespace(prepared_inputs=[], live_session=None)

        monkeypatch.setattr("core.workflows.encode.workflow.TimelineSyncFallbackHelper", _FakeHelper)
        monkeypatch.setattr(wf._postprocess_service, "decide_strict_interleave_with_prescan", lambda _cfg, *, log_cb: True)

        remap, sync_inputs, live, strict = wf._prepare_multisource_sync(
            config=cfg,
            all_sources=[src_main, src_alt],
            sync_base_input_idx=2,
            work_dir=tmp_path,
            allow_live=True,
        )

        assert strict is True
        assert remap == {}
        assert sync_inputs == []
        assert live is None
        assert recorded_allow_live == [False]

    def test_prepare_multisource_sync_forces_sync_when_foreign_offset_without_subtitle_risk(
        self,
        tmp_path,
        monkeypatch,
    ):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        src_main.touch()
        src_alt.touch()

        wf = _make_workflow()
        cfg = _make_config(
            src_main,
            out,
            video=_make_video_settings(codec="copy"),
            copy_subtitles=False,
            subtitle_tracks=[],
            audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy", source_path=src_alt)],
            track_time_offsets=[
                TrackTimeOffset(track_type="audio", source_path=src_alt, stream_index=1, offset_ms=120),
            ],
        )

        recorded_allow_live: list[bool] = []

        class _FakeHelper:
            def __init__(self, **_kwargs):
                pass

            def prepare(self, **kwargs):
                recorded_allow_live.append(bool(kwargs.get("allow_live")))
                return SimpleNamespace(prepared_inputs=[], live_session=None)

        monkeypatch.setattr("core.workflows.encode.workflow.TimelineSyncFallbackHelper", _FakeHelper)
        monkeypatch.setattr(wf._postprocess_service, "decide_strict_interleave_with_prescan", lambda _cfg, *, log_cb: False)

        remap, sync_inputs, live, strict = wf._prepare_multisource_sync(
            config=cfg,
            all_sources=[src_main, src_alt],
            sync_base_input_idx=2,
            work_dir=tmp_path,
            allow_live=True,
        )

        assert strict is True
        assert remap == {}
        assert sync_inputs == []
        assert live is None
        assert recorded_allow_live == [False]

    def test_prepare_multisource_sync_skips_subtitle_prescan_when_foreign_offset_forces_sync(
        self,
        tmp_path,
        monkeypatch,
    ):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        src_main.touch()
        src_alt.touch()

        wf = _make_workflow()
        cfg = _make_config(
            src_main,
            out,
            video=_make_video_settings(codec="copy"),
            copy_subtitles=False,
            subtitle_tracks=[(src_main, 3)],
            audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy", source_path=src_alt)],
            track_time_offsets=[
                TrackTimeOffset(track_type="audio", source_path=src_alt, stream_index=1, offset_ms=120),
            ],
        )

        recorded_allow_live: list[bool] = []

        class _FakeHelper:
            def __init__(self, **_kwargs):
                pass

            def prepare(self, **kwargs):
                recorded_allow_live.append(bool(kwargs.get("allow_live")))
                return SimpleNamespace(prepared_inputs=[], live_session=None)

        monkeypatch.setattr("core.workflows.encode.workflow.TimelineSyncFallbackHelper", _FakeHelper)
        monkeypatch.setattr(
            wf._postprocess_service,
            "decide_strict_interleave_with_prescan",
            lambda _cfg, *, log_cb: pytest.fail("subtitle prescan must be skipped when foreign offset already forces sync"),
        )

        remap, sync_inputs, live, strict = wf._prepare_multisource_sync(
            config=cfg,
            all_sources=[src_main, src_alt],
            sync_base_input_idx=2,
            work_dir=tmp_path,
            allow_live=True,
        )

        assert strict is True
        assert remap == {}
        assert sync_inputs == []
        assert live is None
        assert recorded_allow_live == [False]


# ===========================================================================
# _build_track_meta_args — options metadata FFmpeg
# ===========================================================================

class TestTrackMetaArgs:
    def setup_method(self):
        self.wf = _make_workflow()

    def _build_args(self, tmp_path, edits: list, *, audio_count: int = 1) -> list[str]:
        source = tmp_path / "src.mkv"
        output = tmp_path / "out.mkv"
        source.touch()
        output.touch()
        audio_tracks = [AudioTrackSettings(stream_index=i) for i in range(audio_count)]
        config = _make_config(
            source,
            output,
            audio_tracks=audio_tracks,
            track_meta_edits=edits,
        )
        return self.wf._build_track_meta_args(config)

    def test_fr_FR_language_tags(self, tmp_path):
        from core.workflows.encode.models import TrackMetaEdit
        args = self._build_args(tmp_path, [TrackMetaEdit(track_order=1, language="fr-FR")], audio_count=0)
        assert "language=fr-FR" in args
        assert "language-ietf=" in args
        assert args.index("language=fr-FR") < args.index("language-ietf=")

    def test_track_order_maps_video_audio_subtitle(self, tmp_path):
        from core.workflows.encode.models import TrackMetaEdit
        edits = [
            TrackMetaEdit(track_order=1, language="fr"),
            TrackMetaEdit(track_order=2, language="en"),
            TrackMetaEdit(track_order=3, language="es"),
        ]
        args = self._build_args(tmp_path, edits, audio_count=1)
        assert "-metadata:s:v:0" in args
        assert "-metadata:s:a:0" in args
        assert "-metadata:s:s:0" in args

    def test_iso639_2_and_und_inputs_are_kept(self, tmp_path):
        from core.workflows.encode.models import TrackMetaEdit
        args = self._build_args(
            tmp_path,
            [
                TrackMetaEdit(track_order=2, language="eng"),
                TrackMetaEdit(track_order=3, language="und"),
            ],
            audio_count=2,
        )
        assert "language=en-US" in args
        assert "language=und" in args
        assert args.count("language-ietf=") == 2

    def test_title_only_sets_stream_title(self, tmp_path):
        from core.workflows.encode.models import TrackMetaEdit
        args = self._build_args(tmp_path, [TrackMetaEdit(track_order=1, title="Commentaires")], audio_count=0)
        assert "title=Commentaires" in args
        assert not any(str(a).startswith("language=") for a in args)

    def test_disposition_flags_are_emitted_when_provided(self, tmp_path):
        from core.workflows.encode.models import TrackMetaEdit
        args = self._build_args(
            tmp_path,
            [
                TrackMetaEdit(
                    track_order=2,
                    flag_default=False,
                    flag_forced=True,
                    flag_hearing_impaired=True,
                    flag_visual_impaired=False,
                    flag_original=False,
                    flag_commentary=True,
                )
            ],
            audio_count=2,
        )
        assert "-disposition:a:0" in args
        assert "forced+hearing_impaired+comment" in args

    # ── merge remux extras : fr-FR transmis tel quel dans TrackMetaEdit ───────

    def test_merge_fr_FR_in_track_meta_edit(self, tmp_path):
        """
        _merge_remux_extras avec une piste language='fr-FR' doit produire
        un TrackMetaEdit avec language='fr-FR' (conservé tel quel).
        """
        from core.workflows.encode.remux_bridge import merge_remux_into_encode_config
        from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry
        from core.workflows.encode.models import EncodeConfig, VideoEncodeSettings

        src = tmp_path / "src.mkv"
        src.touch()
        out = tmp_path / "out.mkv"

        track = TrackEntry(
            mkv_tid=0, track_type="video", codec="H264",
            display_info="", language="fr-FR", title="",
            orig_language="fr-FR", orig_title="",
        )
        source = SourceInput(
            path=src, file_index=0, tracks=[track],
            selected_attachments=[], attachment_count=0, copy_tags=True,
        )
        rmx = RemuxConfig(
            sources=[source], output=out,
            track_order=[(0, 0)], keep_chapters=True,
        )
        enc = EncodeConfig(
            source=src, output=out,
            video=VideoEncodeSettings(),
            audio_tracks=[],
        )

        result = merge_remux_into_encode_config(enc, rmx)

        assert result.track_meta_edits, "Aucun TrackMetaEdit généré"
        edit = result.track_meta_edits[0]
        assert edit.language == "fr-FR", \
            f"Langue dans TrackMetaEdit : attendu 'fr-FR', obtenu {edit.language!r}"


class TestNvenccRuntimeRouting:

    def _make_workflow(self) -> EncodeWorkflow:
        return EncodeWorkflow(
            ffmpeg_bin="ffmpeg",
            dovi_tool_bin="dovi_tool",
            hdr10plus_bin="hdr10plus_tool",
            mediainfo_bin="mediainfo",
            nvencc_bin="/usr/bin/NVEncC",
        )

    def _make_config(self, tmp_path: Path, **video_overrides: Any) -> EncodeConfig:
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1000)
        return EncodeConfig(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="nvencc_hevc", **video_overrides),
            audio_tracks=[],
            copy_subtitles=False,
            duration_s=120.0,
        )

    def test_run_routes_nvencc_dynamic_hdr_to_nvencc_direct_output(self, tmp_path):
        cfg = self._make_config(tmp_path, copy_dv=True)
        wf = self._make_workflow()

        with patch.object(wf, "_detect_source_dynamic_hdr_presence", return_value=(True, False)), \
             patch.object(wf, "_run_with_metadata_inject", side_effect=AssertionError("metadata inject ne doit pas être appelé")), \
             patch.object(wf, "_run_nvencc_direct_output", return_value=TaskSignals()) as nvencc_mock:
            wf.run(cfg)

        assert nvencc_mock.called, "Le chemin runtime NVEncC doit être utilisé pour copy_dv."

    def test_run_nvencc_direct_output_builds_encode_and_ffmpeg_remux(self, tmp_path):
        cfg = self._make_config(
            tmp_path,
            inject_hdr_meta=True,
            master_display="G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)",
            max_cll="1000,400",
            extra_params="--aq --aq-strength 12",
        )
        wf = self._make_workflow()
        cleanup_paths: list[Path] = []
        captured: dict[str, list[str]] = {}
        remux_calls: list[tuple[list[str], str | None]] = []

        def _capture_run_cmd(cmd, *, cwd, label, progress_cb=None, signals=None):
            _ = cwd
            _ = progress_cb
            _ = signals
            remux_calls.append((list(cmd), label))
            if label == "nvencc":
                captured["encode"] = list(cmd)
            return "remux-ok"

        with patch.object(wf, "_prepare_nvencc_dynamic_hdr_assets", side_effect=AssertionError("plus d'extraction HDR externe attendue")), \
             patch.object(wf._runner, "_run_cmd", side_effect=_capture_run_cmd):
            wf._run_nvencc_direct_output(
                cfg,
                cleanup_paths,
                prep_signals=TaskSignals(),
            )

        encode_cmd = captured["encode"]
        assert encode_cmd[0] == "/usr/bin/NVEncC"
        assert encode_cmd[encode_cmd.index("-i") + 1] == str(cfg.source)
        assert encode_cmd[encode_cmd.index("--video-streamid") + 1] == "0"
        assert "--master-display" in encode_cmd
        assert "--max-cll" in encode_cmd
        assert "--aq" in encode_cmd
        assert "--aq-strength" in encode_cmd
        assert "12" in encode_cmd

        assert remux_calls, "Le remux final ffmpeg doit être lancé."
        remux_cmd, label = remux_calls[-1]
        assert label == "ffmpeg-remux"
        assert remux_cmd[0] == "ffmpeg"
        assert remux_cmd[remux_cmd.index("-i") + 1].endswith("nvencc.mkv")
        assert str(cfg.source) in remux_cmd
        assert "0:v:0" in remux_cmd
        assert "-c:v" in remux_cmd
        assert remux_cmd[remux_cmd.index("-c:v") + 1] == "copy"

    def test_run_nvencc_direct_output_applies_video_offset_on_remux_input(self, tmp_path):
        cfg = self._make_config(tmp_path)
        cfg.track_time_offsets = [TrackTimeOffset(
            source_path=cfg.source,
            stream_index=0,
            track_type="video",
            offset_ms=1500,
        )]
        wf = self._make_workflow()
        cleanup_paths: list[Path] = []
        remux_cmds: list[list[str]] = []

        def _capture_run_cmd(cmd, *, cwd, label, progress_cb=None, signals=None):
            _ = cwd
            _ = progress_cb
            _ = signals
            if label == "ffmpeg-remux":
                remux_cmds.append(list(cmd))
            return "ok"

        with patch.object(wf._runner, "_run_cmd", side_effect=_capture_run_cmd):
            wf._run_nvencc_direct_output(
                cfg,
                cleanup_paths,
                prep_signals=TaskSignals(),
            )

        assert remux_cmds, "Le remux final doit être lancé."
        remux_cmd = remux_cmds[-1]
        assert "-itsoffset" in remux_cmd
        assert remux_cmd[remux_cmd.index("-itsoffset") + 1] == "1.500"

    def test_build_runtime_nvencc_remux_cmd_uses_sync_input_for_foreign_audio(self, tmp_path):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        encoded = tmp_path / "nvencc.mkv"
        sync_audio = tmp_path / "sync_audio.mka"
        src_main.touch()
        src_alt.touch()
        encoded.touch()
        sync_audio.touch()

        wf = self._make_workflow()
        cfg = EncodeConfig(
            source=src_main,
            output=out,
            video=_make_video_settings(codec="nvencc_hevc"),
            audio_tracks=[
                AudioTrackSettings(stream_index=1, codec="copy", source_path=src_alt),
            ],
            copy_subtitles=False,
            duration_s=120.0,
        )

        with patch.object(
            wf,
            "_prepare_multisource_sync",
            return_value=(
                {(src_alt, 1, "audio"): (3, 0)},
                [sync_audio],
                None,
                True,
            ),
        ):
            remux_cmd, live, cleanup = wf._build_runtime_nvencc_remux_cmd(cfg, encoded)

        assert live is None
        assert cleanup == [sync_audio]
        assert str(sync_audio) in remux_cmd
        map_values = [remux_cmd[i + 1] for i, tok in enumerate(remux_cmd[:-1]) if tok == "-map"]
        assert "3:0" in map_values
        assert "4:0" not in map_values

    def test_build_runtime_nvencc_remux_cmd_applies_offsets_after_sync_remap(self, tmp_path):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        encoded = tmp_path / "nvencc.mkv"
        sync_audio = tmp_path / "sync_audio.mka"
        src_main.touch()
        src_alt.touch()
        encoded.touch()
        sync_audio.touch()

        wf = self._make_workflow()
        cfg = EncodeConfig(
            source=src_main,
            output=out,
            video=_make_video_settings(codec="nvencc_hevc"),
            audio_tracks=[
                AudioTrackSettings(stream_index=1, codec="copy", source_path=src_alt),
            ],
            copy_subtitles=False,
            duration_s=120.0,
            track_time_offsets=[
                TrackTimeOffset(
                    track_type="audio",
                    source_path=src_alt,
                    stream_index=1,
                    offset_ms=120,
                ),
            ],
        )

        with patch.object(
            wf,
            "_prepare_multisource_sync",
            return_value=(
                {(src_alt, 1, "audio"): (3, 0)},
                [sync_audio],
                None,
                True,
            ),
        ):
            remux_cmd, live, cleanup = wf._build_runtime_nvencc_remux_cmd(cfg, encoded)

        assert live is None
        assert cleanup == [sync_audio]
        assert "-itsoffset" in remux_cmd
        assert remux_cmd[remux_cmd.index("-itsoffset") + 1] == "0.120"
        map_values = [remux_cmd[i + 1] for i, tok in enumerate(remux_cmd[:-1]) if tok == "-map"]
        assert "4:0" in map_values

    def test_run_nvencc_direct_output_uses_native_dynamic_hdr_copy_from_source(self, tmp_path):
        cfg = self._make_config(
            tmp_path,
            copy_dv=True,
            copy_hdr10plus=True,
            master_display="UI_MD",
            max_cll="1111,222",
        )
        wf = self._make_workflow()
        cleanup_paths: list[Path] = []
        captured_encode_cmds: list[list[str]] = []

        def _capture_run_cmd(cmd, *, cwd, label, progress_cb=None, signals=None):
            _ = cwd
            _ = progress_cb
            _ = signals
            if label == "nvencc":
                captured_encode_cmds.append(list(cmd))
            return "ok"

        with patch.object(
            wf,
            "_prepare_nvencc_dynamic_hdr_assets",
            side_effect=AssertionError("le chemin natif NVEncC ne doit plus préparer d'assets HDR externes"),
        ), patch.object(wf._runner, "_run_cmd", side_effect=_capture_run_cmd):
            wf._run_nvencc_direct_output(
                cfg,
                cleanup_paths,
                prep_signals=TaskSignals(),
            )

        assert captured_encode_cmds, "La commande NVEncC runtime doit être lancée."
        runtime_cmd = captured_encode_cmds[-1]
        assert runtime_cmd[runtime_cmd.index("-i") + 1] == str(cfg.source)
        assert runtime_cmd[runtime_cmd.index("--video-streamid") + 1] == "0"
        assert runtime_cmd[runtime_cmd.index("--dhdr10-info") + 1] == "copy"
        assert runtime_cmd[runtime_cmd.index("--dolby-vision-rpu") + 1] == "copy"
        assert runtime_cmd[runtime_cmd.index("--dolby-vision-profile") + 1] == "8.1"
        assert runtime_cmd[runtime_cmd.index("--master-display") + 1] == "UI_MD"
        assert runtime_cmd[runtime_cmd.index("--max-cll") + 1] == "1111,222"
        joined = " ".join(runtime_cmd)
        assert "hdr10p.json" not in joined
        assert "rpu.bin" not in joined

    def test_run_nvencc_direct_output_forces_avsw_for_raw_dynamic_hdr_input(self, tmp_path):
        src = tmp_path / "source.hevc"
        src.touch()
        cfg = EncodeConfig(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="nvencc_hevc", copy_dv=True),
            audio_tracks=[],
            copy_subtitles=False,
            duration_s=120.0,
        )
        wf = self._make_workflow()
        cleanup_paths: list[Path] = []
        captured_encode_cmds: list[list[str]] = []

        def _capture_run_cmd(cmd, *, cwd, label, progress_cb=None, signals=None):
            _ = cwd
            _ = progress_cb
            _ = signals
            if label == "nvencc":
                captured_encode_cmds.append(list(cmd))
            return "ok"

        with patch.object(wf._runner, "_run_cmd", side_effect=_capture_run_cmd):
            wf._run_nvencc_direct_output(
                cfg,
                cleanup_paths,
                prep_signals=TaskSignals(),
            )

        assert captured_encode_cmds, "La commande NVEncC runtime doit être lancée."
        runtime_cmd = captured_encode_cmds[-1]
        assert runtime_cmd[runtime_cmd.index("-i") + 1] == str(src)
        assert "--avsw" in runtime_cmd
        assert "--avhw" not in runtime_cmd
        assert runtime_cmd[runtime_cmd.index("--dolby-vision-rpu") + 1] == "copy"
        assert runtime_cmd[runtime_cmd.index("--dolby-vision-profile") + 1] == "8.1"
        assert "--avsync" not in runtime_cmd
        assert "--fps" not in runtime_cmd

    def test_build_command_single_rebases_raw_override_to_original_container_for_dynamic_hdr_copy(self, tmp_path):
        container = tmp_path / "source.mkv"
        raw = tmp_path / "source.hevc"
        container.touch()
        raw.touch()
        wf = self._make_workflow()
        cfg = EncodeConfig(
            source=container,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="nvencc_hevc", copy_dv=True, source_path=raw),
            audio_tracks=[],
            copy_subtitles=False,
            duration_s=120.0,
        )

        preview_cmd = wf.build_command_single(cfg)

        assert preview_cmd[preview_cmd.index("-i") + 1] == str(container)
        assert "--avsw" not in preview_cmd
        assert "--fps" not in preview_cmd
        assert preview_cmd[preview_cmd.index("--dolby-vision-rpu") + 1] == "copy"
        assert preview_cmd[preview_cmd.index("--dolby-vision-profile") + 1] == "8.1"

    def test_run_nvencc_direct_output_pushes_avsync_vfr_for_container_input(self, tmp_path):
        cfg = self._make_config(tmp_path)
        wf = self._make_workflow()
        cleanup_paths: list[Path] = []
        captured_encode_cmds: list[list[str]] = []

        def _capture_run_cmd(cmd, *, cwd, label, progress_cb=None, signals=None):
            _ = cwd
            _ = progress_cb
            _ = signals
            if label == "nvencc":
                captured_encode_cmds.append(list(cmd))
            return "ok"

        with patch.object(wf, "_source_is_vfr", return_value=True), \
             patch.object(wf._runner, "_run_cmd", side_effect=_capture_run_cmd):
            wf._run_nvencc_direct_output(
                cfg,
                cleanup_paths,
                prep_signals=TaskSignals(),
            )

        assert captured_encode_cmds, "La commande NVEncC runtime doit être lancée."
        runtime_cmd = captured_encode_cmds[-1]
        assert runtime_cmd[runtime_cmd.index("-i") + 1] == str(cfg.source)
        assert "--avsync" in runtime_cmd
        assert runtime_cmd[runtime_cmd.index("--avsync") + 1] == "vfr"
        assert "--fps" not in runtime_cmd

    def test_run_nvencc_direct_output_skips_post_encode_dovi_processing(self, tmp_path):
        cfg = self._make_config(tmp_path, copy_dv=True)
        wf = self._make_workflow()
        cleanup_paths: list[Path] = []
        cmds_by_label: dict[str, list[list[str]]] = {}

        def _capture_run_cmd(cmd, *, cwd, label, progress_cb=None, signals=None):
            _ = cwd
            _ = progress_cb
            _ = signals
            cmds_by_label.setdefault(label, []).append(list(cmd))
            return "ok"

        with patch.object(wf._runner, "_run_cmd", side_effect=_capture_run_cmd):
            wf._run_nvencc_direct_output(
                cfg,
                cleanup_paths,
                prep_signals=TaskSignals(),
            )

        assert "nvencc" in cmds_by_label
        assert "ffmpeg-remux" in cmds_by_label
        assert "dovi_tool-patch-rpu" not in cmds_by_label
        assert "ffmpeg-nvencc-patch-rpu-annexb" not in cmds_by_label

    def test_runtime_and_preview_share_same_extra_param_sanitation(self, tmp_path):
        cfg = self._make_config(
            tmp_path,
            copy_dv=True,
            tonemap_to_sdr=True,
            tonemap_algorithm="linear",
            extra_params=(
                "--dolby-vision-profile 5.0 "
                "--dolby-vision-rpu old.bin "
                "--dolby-vision-rpu-prm crop=false "
                "--vpp-colorspace matrix=bt709:bt709,hdr2sdr=hable "
                "--vpp-libplacebo-tonemapping src_csp=hdr10,dst_csp=sdr,tonemapping_function=clip "
                "--aq --qp-init 18 --crop 0,140,0,140"
            ),
        )
        wf = self._make_workflow()
        cleanup_paths: list[Path] = []
        captured_encode_cmds: list[list[str]] = []

        preview_cmd = wf.build_command_single(cfg)

        def _capture_run_cmd(cmd, *, cwd, label, progress_cb=None, signals=None):
            _ = cwd
            _ = progress_cb
            _ = signals
            if label == "nvencc":
                captured_encode_cmds.append(list(cmd))
            return "ok"

        with patch.object(wf._runner, "_run_cmd", side_effect=_capture_run_cmd):
            wf._run_nvencc_direct_output(
                cfg,
                cleanup_paths,
                prep_signals=TaskSignals(),
            )

        assert captured_encode_cmds, "Le runtime doit lancer la commande NVEncC."
        runtime_cmd = captured_encode_cmds[-1]
        assert preview_cmd[-2] == runtime_cmd[-2] == "-o"
        assert preview_cmd[-1].endswith("nvencc.mkv")
        assert runtime_cmd[-1].endswith("nvencc.mkv")
        assert preview_cmd[preview_cmd.index("--dolby-vision-profile") + 1] == "8.1"
        assert preview_cmd[preview_cmd.index("--dolby-vision-rpu") + 1] == "copy"
        assert preview_cmd[preview_cmd.index("--dolby-vision-rpu-prm") + 1] == "crop=true"
        assert runtime_cmd[runtime_cmd.index("-i") + 1] == str(cfg.source)
        assert runtime_cmd[runtime_cmd.index("--dolby-vision-profile") + 1] == "8.1"
        assert runtime_cmd[runtime_cmd.index("--dolby-vision-rpu") + 1] == "copy"
        assert runtime_cmd[runtime_cmd.index("--dolby-vision-rpu-prm") + 1] == "crop=true"
        assert runtime_cmd[runtime_cmd.index("--qp-init") + 1] == "18:18:18"
        assert "--aq" in runtime_cmd
        assert "--vpp-libplacebo-tonemapping" in runtime_cmd
        assert "tonemapping_function=linear" in runtime_cmd[runtime_cmd.index("--vpp-libplacebo-tonemapping") + 1]
        assert "old.bin" not in " ".join(runtime_cmd)

    def test_build_command_single_pushes_source_fps_for_raw_nvencc_input(self, tmp_path):
        src = tmp_path / "source.hevc"
        src.touch()
        wf = self._make_workflow()
        cfg = EncodeConfig(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="nvencc_hevc"),
            audio_tracks=[],
            copy_subtitles=False,
            duration_s=120.0,
        )

        with patch.object(wf, "_source_video_fps_expr", return_value="24000/1001"):
            preview_cmd = wf.build_command_single(cfg)

        assert preview_cmd[preview_cmd.index("-i") + 1] == str(src)
        assert "--fps" in preview_cmd
        assert preview_cmd[preview_cmd.index("--fps") + 1] == "24000/1001"

    def test_build_command_single_forces_avsw_for_raw_dynamic_hdr_input(self, tmp_path):
        src = tmp_path / "source.hevc"
        src.touch()
        wf = self._make_workflow()
        cfg = EncodeConfig(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="nvencc_hevc", copy_dv=True),
            audio_tracks=[],
            copy_subtitles=False,
            duration_s=120.0,
        )

        preview_cmd = wf.build_command_single(cfg)

        assert preview_cmd[preview_cmd.index("-i") + 1] == str(src)
        assert "--avsw" in preview_cmd
        assert "--fps" not in preview_cmd
        assert preview_cmd[preview_cmd.index("--dolby-vision-rpu") + 1] == "copy"
        assert preview_cmd[preview_cmd.index("--dolby-vision-profile") + 1] == "8.1"

    def test_build_command_single_pushes_avsync_vfr_for_container_input(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.touch()
        wf = self._make_workflow()
        cfg = EncodeConfig(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="nvencc_hevc"),
            audio_tracks=[],
            copy_subtitles=False,
            duration_s=120.0,
        )

        with patch.object(wf, "_source_is_vfr", return_value=True):
            preview_cmd = wf.build_command_single(cfg)

        assert preview_cmd[preview_cmd.index("-i") + 1] == str(src)
        assert "--avsync" in preview_cmd
        assert preview_cmd[preview_cmd.index("--avsync") + 1] == "vfr"
        assert "--fps" not in preview_cmd

    def test_source_is_vfr_uses_mediainfo_fallback_when_ffprobe_is_inconclusive(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.touch()
        wf = self._make_workflow()

        with patch.object(wf, "_ffprobe_streams_payload", return_value=None), \
             patch.object(wf, "_load_mediainfo_video_track", return_value={"FrameRate_Mode": "VFR"}):
            assert wf._source_is_vfr(src) is True

        with patch.object(wf, "_ffprobe_streams_payload", return_value=None), \
             patch.object(wf, "_load_mediainfo_video_track", return_value={"FrameRate_Mode": "CFR"}):
            assert wf._source_is_vfr(src) is False
