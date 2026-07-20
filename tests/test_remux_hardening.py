"""
tests/test_remux_hardening.py — Cahier de test du durcissement remux (lot 1).

Couverture :
- validation appelée par run() pour les deux backends (aucun thread en erreur) ;
- protection source = sortie (liens symboliques) ;
- préflight natif scopé : pistes incompatibles/chiffrées non sélectionnées ;
- correspondance des indices après canonicalisation sélective ;
- égalité entre preview et liste réelle des préparations (variantes incluses) ;
- annulation pendant canonicalisation, variante audio et écriture native ;
- conservation d'une ancienne sortie en cas d'échec FFmpeg/post-patch/validation ;
- nettoyage des fichiers partiels ;
- NFO en avertissement après commit (deux backends) ;
- post-patchs exécutés sur FFmpeg et absents du natif.
"""

from __future__ import annotations

import time
import threading
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtCore import QCoreApplication, Qt

from core.matroska.ebml import ascii_element, element, string_element, uint_element
from core.matroska.ids import (
    CLUSTER_ID, CODEC_ID_ID, EBML_HEADER_ID, INFO_ID, SEGMENT_ID, SIMPLE_BLOCK_ID,
    TIMESTAMP_ID, TRACKS_ID, TRACK_ENTRY_ID, TRACK_NUMBER_ID, TRACK_TYPE_ID,
    TRACK_UID_ID, TITLE_ID,
)
from core.matroska.mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack
from core.matroska.contract import ExpectedMatroskaTrack, MatroskaOutputContract
from core.matroska.validation import validate_matroska_output
from core.matroska.reader import MatroskaBlock, MatroskaReader, MatroskaTrack
from core.matroska.writer import (
    MatroskaWriteCancelled, MatroskaWriteProgress, MatroskaWriter,
)
from core.workflows.remux import RemuxWorkflow
from core.workflows.remux_backend import run_native_remux
from core.workflows.remux_models import RemuxConfig, RemuxError, SourceInput, TrackEntry
from core.workflows.remux_plan import (
    build_canonicalization_command,
    canonicalization_stream_selection,
    mux_backend_report_from_plan,
    native_preparation_commands,
    plan_native_audio_variants,
    plan_remux,
    select_mux_backend,
)
from core.workflows.remux_runtime import RemuxRuntimeRunner, RemuxRuntimeRunnerCallbacks


# =============================================================================
# Helpers de construction
# =============================================================================

def _track(
    tid: int,
    kind: str = "video",
    *,
    codec: str = "COPY",
    time_shift_ms: int = 0,
    is_new: bool = False,
    orig_codec: str = "",
    display_info: str = "",
) -> TrackEntry:
    entry = TrackEntry(
        mkv_tid=tid, track_type=kind, codec=codec, display_info=display_info,
        language="und", title="", file_id="src0", time_shift_ms=time_shift_ms,
    )
    entry.is_new = is_new
    if orig_codec:
        entry.orig_codec = orig_codec
    return entry


def _entry_bytes(number: int, track_type: int, codec_id: str, extra: bytes = b"") -> bytes:
    return element(TRACK_ENTRY_ID, b"".join((
        uint_element(TRACK_NUMBER_ID, number),
        uint_element(TRACK_UID_ID, number),
        uint_element(TRACK_TYPE_ID, track_type),
        ascii_element(CODEC_ID_ID, codec_id),
        extra,
    )))


def _encrypted_extra() -> bytes:
    return element(bytes.fromhex("6d80"), element(
        bytes.fromhex("6240"), element(bytes.fromhex("5035"), b"\x01"),
    ))


def _simple_cluster(track_number: int = 1, payload: bytes = b"\x00" * 16) -> bytes:
    block = bytes([0x80 | track_number]) + b"\x00\x00\x80" + payload
    return element(CLUSTER_ID, uint_element(TIMESTAMP_ID, 0) + element(SIMPLE_BLOCK_ID, block))


def _write_mkv(path: Path, entries: list[bytes], *, clusters: bytes = b"") -> Path:
    path.write_bytes(
        element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff"
        + element(TRACKS_ID, b"".join(entries)) + clusters
    )
    return path


def _wait(signals, timeout: float = 20.0) -> dict[str, object]:
    app = QCoreApplication.instance()
    assert app is not None, "Q(Core)Application non initialisée"
    state: dict[str, object] = {"finished": None, "failed": None, "cancelled": False, "progress": []}
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


def _no_partial_left(directory: Path) -> bool:
    return not list(directory.rglob("*.partial"))


@pytest.fixture(autouse=True)
def _qt_app(qt_app):
    """QApplication requise pour les signaux Qt des runners."""
    return qt_app


# =============================================================================
# 1.2 — Validation appelée par run() pour les deux backends
# =============================================================================

class TestRunValidatesBeforeDispatch:

    def _invalid_config(self, tmp_path: Path, backend: str) -> RemuxConfig:
        return RemuxConfig(
            sources=[SourceInput(tmp_path / "missing.mkv", 0, [_track(0)])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            keep_chapters=False,
            mux_backend=backend,
        )

    @pytest.mark.parametrize("backend", ["ffmpeg", "native", "auto"])
    def test_run_raises_without_thread_or_partial(self, tmp_path: Path, backend: str) -> None:
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", generate_nfo=False)
        with pytest.raises(RemuxError, match="introuvable"):
            wf.run(self._invalid_config(tmp_path, backend))
        assert not (tmp_path / "out.mkv").exists()
        assert _no_partial_left(tmp_path)

    def test_native_strict_fails_at_preflight(self, tmp_path: Path) -> None:
        source = _write_mkv(tmp_path / "in.mkv", [_entry_bytes(1, 1, "V_MPEG4/ISO/AVC")])
        cfg = RemuxConfig(
            sources=[SourceInput(source, 0, [_track(0)])],
            output=tmp_path / "out.mp4",
            track_order=[(0, 0)],
            keep_chapters=False,
            mux_backend="native",
        )
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", generate_nfo=False)
        with pytest.raises(RemuxError, match="Backend natif indisponible"):
            wf.run(cfg)

    def test_symlinked_output_equals_source_is_rejected(self, tmp_path: Path) -> None:
        source = _write_mkv(tmp_path / "in.mkv", [_entry_bytes(1, 1, "V_MPEG4/ISO/AVC")])
        output = tmp_path / "out.mkv"
        output.symlink_to(source)
        cfg = RemuxConfig(
            sources=[SourceInput(source, 0, [_track(0)])],
            output=output,
            track_order=[(0, 0)],
            keep_chapters=False,
        )
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", generate_nfo=False)
        errors = wf.validate(cfg)
        assert any("différent de la source" in error for error in errors)


# =============================================================================
# 1.3 — Préflight natif scopé aux pistes sélectionnées
# =============================================================================

class TestScopedNativePreflight:

    def _config(self, tmp_path: Path, tracks: list[TrackEntry], order: list[tuple[int, int]],
                entries: list[bytes]) -> RemuxConfig:
        source = _write_mkv(tmp_path / "in.mkv", entries)
        return RemuxConfig(
            sources=[SourceInput(source, 0, tracks)],
            output=tmp_path / "out.mkv",
            track_order=order,
            keep_chapters=False,
            mux_backend="auto",
        )

    def test_unselected_incompatible_subtitle_keeps_native(self, tmp_path: Path) -> None:
        cfg = self._config(
            tmp_path,
            [_track(0, "video"), _track(1, "subtitle", codec="DVB_TELETEXT")],
            [(0, 0)],
            [_entry_bytes(1, 1, "V_MPEG4/ISO/AVC"), _entry_bytes(2, 17, "S_DVBSUB")],
        )
        decision = select_mux_backend(cfg)
        assert decision.selected == "native"
        assert decision.native_reasons == ()

    def test_selected_incompatible_subtitle_falls_back(self, tmp_path: Path) -> None:
        cfg = self._config(
            tmp_path,
            [_track(0, "video"), _track(1, "subtitle", codec="DVB_TELETEXT")],
            [(0, 0), (0, 1)],
            [_entry_bytes(1, 1, "V_MPEG4/ISO/AVC"), _entry_bytes(2, 17, "S_DVBSUB")],
        )
        decision = select_mux_backend(cfg)
        assert decision.selected == "ffmpeg"
        assert decision.native_reasons

    def test_unselected_encrypted_track_keeps_native(self, tmp_path: Path) -> None:
        entries = [
            _entry_bytes(1, 1, "V_MPEG4/ISO/AVC"),
            _entry_bytes(2, 2, "A_AAC", _encrypted_extra()),
        ]
        cfg = self._config(tmp_path, [_track(0, "video"), _track(1, "audio", codec="AAC")], [(0, 0)], entries)
        decision = select_mux_backend(cfg)
        assert decision.selected == "native"
        assert decision.native_reasons == ()

    def test_selected_encrypted_track_blocks_native(self, tmp_path: Path) -> None:
        entries = [
            _entry_bytes(1, 1, "V_MPEG4/ISO/AVC"),
            _entry_bytes(2, 2, "A_AAC", _encrypted_extra()),
        ]
        cfg = self._config(
            tmp_path,
            [_track(0, "video"), _track(1, "audio", codec="AAC")],
            [(0, 0), (0, 1)],
            entries,
        )
        decision = select_mux_backend(cfg)
        assert decision.selected == "ffmpeg"
        assert any("chiffrée" in reason for reason in decision.native_reasons)

    def test_content_encodings_exposed_per_track(self, tmp_path: Path) -> None:
        source = _write_mkv(tmp_path / "enc.mkv", [
            _entry_bytes(1, 1, "V_MPEG4/ISO/AVC"),
            _entry_bytes(2, 2, "A_AAC", _encrypted_extra()),
        ])
        assert MatroskaReader(source).content_encodings_by_track() == [
            (False, False), (False, True),
        ]

    def test_unreadable_non_participating_source_keeps_native(self, tmp_path: Path) -> None:
        good = _write_mkv(tmp_path / "good.mkv", [_entry_bytes(1, 1, "V_MPEG4/ISO/AVC")])
        bad = tmp_path / "bad.mkv"
        bad.write_bytes(b"not-ebml")
        cfg = RemuxConfig(
            sources=[
                SourceInput(good, 0, [_track(0)]),
                SourceInput(bad, 1, [_track(0, "audio", codec="AAC")]),
            ],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            keep_chapters=False,
            mux_backend="auto",
        )
        decision = select_mux_backend(cfg)
        assert decision.selected == "native"
        assert decision.native_reasons == ()


# =============================================================================
# 1.3 — Correspondance des indices après canonicalisation sélective
# =============================================================================

class TestCanonicalizationIndexMapping:

    def _mp4_source(self, tmp_path: Path) -> SourceInput:
        path = tmp_path / "in.mp4"
        path.write_bytes(b"fixture")
        return SourceInput(path, 0, [
            _track(0, "video"),
            _track(2, "audio", codec="AAC"),
            _track(5, "subtitle", codec="MOV_TEXT"),
        ])

    def test_stream_selection_maps_original_to_canonical(self, tmp_path: Path) -> None:
        source = self._mp4_source(tmp_path)
        streams, index_map = canonicalization_stream_selection(
            source, [source.tracks[0], source.tracks[2]],
        )
        assert streams == [0, 5]
        assert index_map == {0: 0, 5: 1}

    def test_command_maps_only_selected_streams(self, tmp_path: Path) -> None:
        source = self._mp4_source(tmp_path)
        cmd = build_canonicalization_command(
            source, tmp_path / "canon.mkv", "ffmpeg", selected_streams=[0, 5],
        )
        joined = " ".join(cmd)
        assert "-map 0:0" in joined
        assert "-map 0:5" in joined
        assert "-map 0 " not in joined + " "
        # mov_text sélectionné → transcodé srt à l'index sous-titre canonique 0
        assert "-c:s:0 srt" in joined

    def test_plan_records_explicit_index_map(self, tmp_path: Path) -> None:
        source = self._mp4_source(tmp_path)
        cfg = RemuxConfig(
            sources=[source],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (0, 5)],
            keep_chapters=False,
            mux_backend="auto",
        )
        plan = plan_remux(cfg)
        assert plan.selected_backend == "native"
        assert plan.canonical_index_map(0) == {0: 0, 5: 1}


# =============================================================================
# 1.1/1.3 — Égalité preview / exécution des préparations (variantes incluses)
# =============================================================================

class TestPreparationParity:

    def _variant_config(self, tmp_path: Path) -> RemuxConfig:
        source = _write_mkv(tmp_path / "in.mkv", [
            _entry_bytes(1, 1, "V_MPEG4/ISO/AVC"),
            _entry_bytes(2, 2, "A_EAC3"),
        ], clusters=_simple_cluster(1) + _simple_cluster(2))
        variant = _track(
            1, "audio", codec="AAC", is_new=True,
            orig_codec="EAC3", display_info="5.1  640 kbps",
        )
        return RemuxConfig(
            sources=[SourceInput(source, 0, [_track(0), _track(1, "audio", codec="EAC3"), variant])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (0, 1, variant.entry_id)],
            keep_chapters=False,
            mux_backend="auto",
        )

    def test_audio_variants_appear_in_preview_and_plan(self, tmp_path: Path) -> None:
        cfg = self._variant_config(tmp_path)
        plan = plan_remux(cfg)
        assert plan.selected_backend == "native"
        kinds = [action.kind for action in plan.preparation_actions]
        assert kinds == ["audio_variant"]
        variants = plan_native_audio_variants(cfg)
        assert len(variants) == 1
        assert variants[0].target_codec == "aac"
        assert variants[0].bitrate_kbps == 640
        commands = native_preparation_commands(cfg, "ffmpeg")
        assert commands == [list(action.command) for action in plan.preparation_actions]
        assert any("-c:a" in command for command in commands)

    def test_report_projects_plan_fields(self, tmp_path: Path) -> None:
        cfg = self._variant_config(tmp_path)
        plan = plan_remux(cfg)
        report = mux_backend_report_from_plan(plan)
        assert report["output_commit_mode"] == "atomic"
        assert report["required_tools"] == ["ffprobe", "ffmpeg"]
        assert report["preparation_commands"] == [
            list(action.command) for action in plan.preparation_actions if action.command
        ]
        assert str(report["candidate_output"]).endswith(".mkv.partial")
        actions = cast(list, report["preparation_actions"])
        assert actions[0]["kind"] == "audio_variant"


# =============================================================================
# 1.5 — Annulation et progression du writer natif
# =============================================================================

def _writer_plan(tmp_path: Path, packet_count: int = 3) -> MatroskaMuxPlan:
    raw_entry = b"".join((
        uint_element(TRACK_NUMBER_ID, 1),
        uint_element(TRACK_UID_ID, 1),
        uint_element(TRACK_TYPE_ID, 1),
        ascii_element(CODEC_ID_ID, "V_MPEG4/ISO/AVC"),
    ))
    source_track = MatroskaTrack(
        number=1, uid=1, track_type=1, codec_id="V_MPEG4/ISO/AVC",
        codec_private=b"", language_bcp47="", language="und", name="",
        raw_entry=raw_entry,
    )
    packets = tuple(
        MatroskaMuxPacket(1, MatroskaBlock(
            track_number=1, timestamp_ms=index * 40, flags=0x80, payload=b"\x00" * 32,
        ), index)
        for index in range(packet_count)
    )
    return MatroskaMuxPlan(
        tmp_path / "out.mkv",
        (MatroskaMuxTrack(
            source=tmp_path / "src.mkv", source_track=source_track,
            output_number=1, output_uid=1,
        ),),
        packets,
    )


class TestWriterCancellationAndProgress:

    def test_cancel_leaves_no_partial(self, tmp_path: Path) -> None:
        plan = _writer_plan(tmp_path)
        with pytest.raises(MatroskaWriteCancelled):
            MatroskaWriter().write(plan, cancel_cb=lambda: True)
        assert not (tmp_path / "out.mkv").exists()
        assert _no_partial_left(tmp_path)

    def test_progress_reports_counters_and_stages(self, tmp_path: Path) -> None:
        plan = _writer_plan(tmp_path, packet_count=5)
        events: list[MatroskaWriteProgress] = []
        MatroskaWriter().write(plan, progress_cb=events.append)
        stages = [event.stage for event in events]
        assert "clusters" in stages
        assert stages[-1] == "commit"
        assert events[-1].packets_written == 5
        assert events[-1].bytes_written > 0
        assert events[-1].candidate.name == "out.mkv.partial"
        assert (tmp_path / "out.mkv").is_file()
        assert _no_partial_left(tmp_path)

    def test_media_track_without_packet_fails_validation(self, tmp_path: Path) -> None:
        plan = _writer_plan(tmp_path, packet_count=0)
        with pytest.raises(ValueError, match="aucun paquet écrit"):
            MatroskaWriter().write(plan)
        assert _no_partial_left(tmp_path)

    def test_semantic_validator_checks_contract(self, tmp_path: Path) -> None:
        plan = _writer_plan(tmp_path, packet_count=2)
        MatroskaWriter().write(plan)
        output = tmp_path / "out.mkv"
        assert validate_matroska_output(output, MatroskaOutputContract(track_types=("video",))) == []
        errors = validate_matroska_output(output, MatroskaOutputContract(track_types=("video", "audio")))
        assert any("Pistes de sortie inattendues" in error for error in errors)
        errors = validate_matroska_output(
            output,
            MatroskaOutputContract(track_types=("video",), require_block_addition_mapping=True),
        )
        assert any("BlockAdditionMapping" in error for error in errors)


# =============================================================================
# 1.5 — Annulation du runner natif (canonicalisation et variante audio)
# =============================================================================

class _CancellingProcess:
    """Faux Popen : déclenche l'annulation du job pendant son exécution."""

    holder: dict[str, object] = {}

    def __init__(self, cmd, **_kwargs) -> None:
        self.cmd = cmd
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    def communicate(self):
        deadline = time.monotonic() + 5.0
        while "signals" not in self.holder and time.monotonic() < deadline:
            time.sleep(0.01)
        signals = self.holder.get("signals")
        if signals is not None:
            signals.cancel()  # type: ignore[attr-defined]
        return "", None


class TestNativeRunnerCancellation:

    def test_cancel_during_canonicalization(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            "core.workflows.remux_backend.subprocess.Popen", _CancellingProcess,
        )
        _CancellingProcess.holder = {}
        mp4 = tmp_path / "in.mp4"
        mp4.write_bytes(b"fixture")
        cfg = RemuxConfig(
            sources=[SourceInput(mp4, 0, [_track(0)])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            keep_chapters=False,
            work_dir=tmp_path / "work",
            mux_backend="auto",
        )
        signals = run_native_remux(
            cfg, log=lambda _l, _m: None, log_step=lambda _i, _n: None, ffmpeg_bin="ffmpeg",
        )
        _CancellingProcess.holder["signals"] = signals
        state = _wait(signals)
        assert state["cancelled"] is True
        assert state["failed"] is None
        assert not (tmp_path / "out.mkv").exists()
        assert _no_partial_left(tmp_path)
        assert not list((tmp_path / "work").glob("Muxiveo_*"))

    def test_cancel_during_audio_variant(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            "core.workflows.remux_backend.subprocess.Popen", _CancellingProcess,
        )
        _CancellingProcess.holder = {}
        source = _write_mkv(tmp_path / "in.mkv", [
            _entry_bytes(1, 1, "V_MPEG4/ISO/AVC"),
            _entry_bytes(2, 2, "A_EAC3"),
        ], clusters=_simple_cluster(1))
        variant = _track(1, "audio", codec="AAC", is_new=True, orig_codec="EAC3")
        cfg = RemuxConfig(
            sources=[SourceInput(source, 0, [_track(0), variant])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (0, 1, variant.entry_id)],
            keep_chapters=False,
            work_dir=tmp_path / "work",
            mux_backend="auto",
        )
        signals = run_native_remux(
            cfg, log=lambda _l, _m: None, log_step=lambda _i, _n: None, ffmpeg_bin="ffmpeg",
        )
        _CancellingProcess.holder["signals"] = signals
        state = _wait(signals)
        assert state["cancelled"] is True
        assert _no_partial_left(tmp_path)
        assert not list((tmp_path / "work").glob("Muxiveo_*"))


# =============================================================================
# Natif de bout en bout : NFO en avertissement, pas de post-patchs
# =============================================================================

class TestNativeEndToEnd:

    def _config(self, tmp_path: Path) -> RemuxConfig:
        # Piste sous-titre : ffprobe (second validateur) accepte le flux
        # S_TEXT/UTF8 synthétique sans tenter de décodage vidéo.
        source = _write_mkv(
            tmp_path / "in.mkv",
            [_entry_bytes(1, 17, "S_TEXT/UTF8")],
            clusters=_simple_cluster(1, payload=b"Bonjour"),
        )
        return RemuxConfig(
            sources=[SourceInput(source, 0, [_track(0, "subtitle", codec="SRT")])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            keep_chapters=False,
            work_dir=tmp_path / "work",
            mux_backend="auto",
        )

    def test_nfo_failure_after_commit_is_warning(self, tmp_path: Path) -> None:
        logs: list[tuple[str, str]] = []

        def failing_nfo(_path: Path) -> None:
            raise RuntimeError("mediainfo absent")

        signals = run_native_remux(
            self._config(tmp_path),
            log=lambda level, message: logs.append((level, message)),
            log_step=lambda _i, _n: None,
            ffmpeg_bin="ffmpeg",
            finalize=failing_nfo,
        )
        state = _wait(signals)
        assert state["failed"] is None
        assert state["finished"] is not None
        assert (tmp_path / "out.mkv").is_file()
        assert _no_partial_left(tmp_path)
        assert any(level == "WARN" and "NFO" in message for level, message in logs)

    def test_native_run_skips_container_post_patches(self, tmp_path: Path, monkeypatch) -> None:
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", generate_nfo=False)
        patched: list[Path] = []
        monkeypatch.setattr(wf._muxing_post_action, "apply_if_mkv", lambda path, **_kw: patched.append(path))
        monkeypatch.setattr(wf._language_post_action, "apply_if_mkv", lambda path, **_kw: patched.append(path))
        cfg = self._config(tmp_path)
        signals = wf.run(cfg)
        state = _wait(signals)
        assert state["failed"] is None
        assert (tmp_path / "out.mkv").is_file()
        assert patched == []

    def test_native_cleans_process_directory_after_success(self, tmp_path: Path) -> None:
        cfg = self._config(tmp_path)
        process_dir = cfg.work_dir / cfg.output.stem
        process_dir.mkdir(parents=True)
        (process_dir / "stale.bin").write_bytes(b"stale")

        state = _wait(run_native_remux(
            cfg,
            log=lambda _level, _message: None,
            log_step=lambda _index, _name: None,
            ffmpeg_bin="ffmpeg",
        ))

        assert state["failed"] is None
        assert not process_dir.exists()

    def test_explicit_empty_metadata_clears_source_segment_title(self, tmp_path: Path) -> None:
        cfg = self._config(tmp_path)
        source = cfg.sources[0].path
        track_payload = element(
            TRACKS_ID,
            _entry_bytes(1, 17, "S_TEXT/UTF8"),
        )
        source.write_bytes(
            element(EBML_HEADER_ID, b"")
            + SEGMENT_ID
            + b"\xff"
            + element(INFO_ID, string_element(TITLE_ID, "Titre source"))
            + track_payload
            + _simple_cluster(1, payload=b"Bonjour")
        )
        cfg.tag_overrides = {}
        cfg.file_title = ""

        state = _wait(run_native_remux(
            cfg,
            log=lambda _level, _message: None,
            log_step=lambda _index, _name: None,
            ffmpeg_bin="ffmpeg",
        ))

        assert state["failed"] is None
        assert MatroskaReader(cfg.output).segment_title() == ""


# =============================================================================
# 1.4 — Atomicité du backend FFmpeg
# =============================================================================

def _valid_candidate_bytes() -> bytes:
    entry = _entry_bytes(1, 1, "V_MPEG4/ISO/AVC")
    return element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff" + element(TRACKS_ID, entry)


class TestFfmpegRuntimeAtomicity:

    @staticmethod
    def _plan(cfg: RemuxConfig):
        return replace(
            plan_remux(cfg),
            output_contract=MatroskaOutputContract(
                track_types=("video",),
                expected_tracks=(ExpectedMatroskaTrack("video"),),
                duration_coherent=False,
            ),
        )

    def _config(self, tmp_path: Path) -> RemuxConfig:
        source = tmp_path / "in.mkv"
        source.write_bytes(b"src")
        return RemuxConfig(
            sources=[SourceInput(source, 0, [_track(0)])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            keep_chapters=False,
            work_dir=tmp_path / "work",
        )

    def _runner(
        self,
        cfg: RemuxConfig,
        *,
        run_cmd,
        apply_muxing=None,
        apply_language=None,
        write_nfo=None,
        logs: list[tuple[str, str]] | None = None,
    ) -> RemuxRuntimeRunner:
        return RemuxRuntimeRunner(RemuxRuntimeRunnerCallbacks(
            ffmpeg_bin="ffmpeg",
            ffprobe_bin="ffprobe",
            ffmpeg_thread_args=lambda: [],
            build_command=lambda _config, **_kw: ["ffmpeg", "-y", str(cfg.output)],
            log_workflow_type=lambda _kind: None,
            log_step=lambda _idx, _name: None,
            log=(lambda level, message: logs.append((level, message))) if logs is not None else (lambda _l, _m: None),
            bind_temp_cleanup=lambda _signals, _paths: None,
            run_cmd=run_cmd,
            apply_muxing_post_action=apply_muxing or (lambda _path: None),
            apply_language_post_action=apply_language or (lambda _path: None),
            write_nfo=write_nfo or (lambda _path: None),
        ))

    @staticmethod
    def _writing_run_cmd(cmd, _cwd, label, _progress_cb, _signals) -> str:
        if label == "ffmpeg-remux":
            Path(cmd[-1]).write_bytes(_valid_candidate_bytes())
        return "ok"

    def test_ffmpeg_failure_preserves_previous_output(self, tmp_path: Path) -> None:
        cfg = self._config(tmp_path)
        cfg.output.write_bytes(b"OLD")

        def failing_run_cmd(_cmd, _cwd, label, _progress_cb, _signals) -> str:
            if label == "ffmpeg-remux":
                # Laisse le test connecter les signaux (émission trop précoce sinon).
                time.sleep(0.2)
                raise RuntimeError("ffmpeg exit 1")
            return "ok"

        state = _wait(self._runner(cfg, run_cmd=failing_run_cmd).run(cfg, self._plan(cfg)))
        assert state["failed"] is not None
        assert cfg.output.read_bytes() == b"OLD"
        assert _no_partial_left(tmp_path)

    def test_post_patch_failure_preserves_previous_output(self, tmp_path: Path) -> None:
        cfg = self._config(tmp_path)
        cfg.output.write_bytes(b"OLD")

        def failing_patch(_path: Path) -> None:
            raise RuntimeError("patch KO")

        state = _wait(self._runner(
            cfg, run_cmd=self._writing_run_cmd, apply_muxing=failing_patch,
        ).run(cfg, self._plan(cfg)))
        assert state["failed"] is not None
        assert cfg.output.read_bytes() == b"OLD"
        assert _no_partial_left(tmp_path)

    def test_validation_failure_preserves_previous_output(self, tmp_path: Path) -> None:
        cfg = self._config(tmp_path)
        cfg.output.write_bytes(b"OLD")

        def corrupt_run_cmd(cmd, _cwd, label, _progress_cb, _signals) -> str:
            if label == "ffmpeg-remux":
                time.sleep(0.1)
                Path(cmd[-1]).write_bytes(b"not-ebml")
            return "ok"

        state = _wait(self._runner(cfg, run_cmd=corrupt_run_cmd).run(cfg, self._plan(cfg)))
        assert state["failed"] is not None
        failure = cast(tuple, state["failed"])
        assert "Validation de la sortie remux échouée" in str(failure[0])
        assert cfg.output.read_bytes() == b"OLD"
        assert _no_partial_left(tmp_path)

    def test_success_patches_candidate_then_commits_with_nfo_warning(self, tmp_path: Path) -> None:
        cfg = self._config(tmp_path)
        patched: list[str] = []
        logs: list[tuple[str, str]] = []

        def record_patch(path: Path) -> None:
            patched.append(Path(path).name)

        def failing_nfo(_path: Path) -> None:
            raise RuntimeError("nfo KO")

        state = _wait(self._runner(
            cfg,
            run_cmd=self._writing_run_cmd,
            apply_muxing=record_patch,
            apply_language=record_patch,
            write_nfo=failing_nfo,
            logs=logs,
        ).run(cfg, self._plan(cfg)))
        assert state["failed"] is None
        assert state["finished"] is not None
        # Post-patchs exécutés sur le candidat, avant le commit atomique.
        assert patched == ["out.mkv.partial", "out.mkv.partial"]
        assert cfg.output.read_bytes() == _valid_candidate_bytes()
        assert _no_partial_left(tmp_path)
        assert any(level == "WARN" and "NFO" in message for level, message in logs)

    def test_tmdb_download_runs_in_worker_thread(self, tmp_path: Path, monkeypatch) -> None:
        cfg = self._config(tmp_path)
        cfg.tmdb_cover = ("https://image.example/cover.jpg", "")
        caller_thread = threading.current_thread()
        download_threads: list[threading.Thread] = []
        logs: list[tuple[str, str]] = []

        def failing_download(*_args, **_kwargs):
            download_threads.append(threading.current_thread())
            raise RuntimeError("panne réseau simulée")

        def delayed_run_cmd(cmd, cwd, label, progress_cb, signals) -> str:
            if label == "ffmpeg-remux":
                time.sleep(0.1)
            return self._writing_run_cmd(cmd, cwd, label, progress_cb, signals)

        monkeypatch.setattr(
            "core.workflows.remux_runtime.download_tmdb_cover",
            failing_download,
        )
        state = _wait(self._runner(
            cfg,
            run_cmd=delayed_run_cmd,
            logs=logs,
        ).run(cfg, self._plan(cfg)))

        assert state["failed"] is None
        assert download_threads and download_threads[0] is not caller_thread
        assert any(level == "WARN" and "TMDB" in message for level, message in logs)


class TestNativeTmdbCoverOptional:
    """P2 audit externe : l'échec du téléchargement TMDB est un warning sur
    les DEUX backends — parité FFmpeg/natif, attente retirée du contrat."""

    def test_tmdb_download_failure_is_nonfatal_in_native(self, tmp_path: Path, monkeypatch) -> None:
        def _boom(*_args, **_kwargs):
            raise RuntimeError("panne réseau simulée")

        monkeypatch.setattr("core.workflows.remux_backend.download_tmdb_cover", _boom)
        source = _write_mkv(
            tmp_path / "in.mkv",
            [_entry_bytes(1, 17, "S_TEXT/UTF8")],
            clusters=_simple_cluster(1, payload=b"Bonjour"),
        )
        cfg = RemuxConfig(
            sources=[SourceInput(source, 0, [_track(0, "subtitle", codec="SRT")])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            keep_chapters=False,
            work_dir=tmp_path / "work",
            mux_backend="native",
            tmdb_cover=("https://image.example/cover.jpg", "cover.jpg"),
        )
        logs: list[tuple[str, str]] = []
        signals = run_native_remux(
            cfg,
            log=lambda level, message: logs.append((level, message)),
            log_step=lambda _i, _n: None,
            ffmpeg_bin="ffmpeg",
        )
        state = _wait(signals)
        assert state["failed"] is None
        assert state["cancelled"] is False
        assert (tmp_path / "out.mkv").is_file()
        assert any(level == "WARN" and "TMDB" in message for level, message in logs)
        # La sortie ne contient aucune cover et la validation a accepté.
        assert MatroskaReader(tmp_path / "out.mkv").attachments() == []
