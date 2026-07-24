"""
tests/test_matroska_mux_unification.py — Cahier de test du lot 2 (muxage unifié).

Couverture :
- lecture, migration et sauvegarde du setting unique [matroska] mux_backend ;
- défaut FFmpeg sans configuration, valeur invalide avec avertissement ;
- priorité job > setting global > FFmpeg ;
- propagation remux → encode (merge_remux_into_encode_config) ;
- distinction encodeur vidéo (NVEncC/FFmpeg) et muxeur Matroska ;
- routage auto/native/ffmpeg pour chaque pipeline (diagnostics inclus) ;
- assemblage final natif encode via le contrat partagé (parity métadonnées) ;
- post-patchs prévus uniquement sur FFmpeg (preview) ;
- backend natif strict signalé avant l'encodage lourd (validate).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from core.workflows.common.track_types import TrackMetaPatch, TrackOffset
from core.matroska.ebml import ascii_element, element, uint_element
from core.workflows.encode.models import (
    AudioTrackSettings,
    EncodeConfig,
    EncodeError,
    QualityMode,
    VideoEncodeSettings,
)
from core.workflows.encode.mux_backend import (
    PIPELINE_FFMPEG_DIRECT,
    PIPELINE_METADATA_INJECT,
    PIPELINE_MULTI_VIDEO,
    PIPELINE_NVENCC_DIRECT,
    encode_native_mux_blockers,
    select_encode_mux_backend,
)
from core.workflows.encode.remux_bridge import merge_remux_into_encode_config
from core.workflows.encode.runtime.native_mux import (
    NativeVideoArtifactRef,
    assemble_encode_output_native,
    prepare_native_encode_inputs,
)
from core.runner import TaskCancelledError, TaskSignals
from core.workflows.common.matroska_finalize import MatroskaOutputTransaction
from core.matroska.contract import (
    ExpectedMatroskaAttachment,
    ExpectedMatroskaTrack,
    MatroskaOutputContract,
)
from core.matroska.assembly import (
    MatroskaAssemblyPlan,
    MatroskaAssemblyTrack,
    assembly_output_contract,
)
from core.matroska.ids import (
    CHAPTERS_ID, CLUSTER_ID, CODEC_ID_ID, EBML_HEADER_ID, SEGMENT_ID,
    SIMPLE_BLOCK_ID, TAGS_ID, TIMESTAMP_ID, TRACKS_ID, TRACK_ENTRY_ID,
    TRACK_NUMBER_ID, TRACK_TYPE_ID, TRACK_UID_ID,
)
from core.matroska.reader import MatroskaAttachment, MatroskaReader
from core.matroska.validation import validate_matroska_output
from core.matroska.writer import build_attachments_element
from core.workflows.common.attachments import mime_for_path
from core.workflows.encode.output_contract import build_encode_output_contract
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry, normalize_mux_backend
from core.workflows.remux_plan import plan_remux


@pytest.fixture(autouse=True)
def _qt_application(qt_app):
    """Partage la QApplication de session avec les autres tests Qt."""
    return qt_app


@pytest.fixture(autouse=True)
def _isolate_ini_path(tmp_path, monkeypatch):
    """Évite toute pollution par un config.ini utilisateur réel."""
    import core.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "_INI_PATH", tmp_path / "config.ini")


# =============================================================================
# Helpers
# =============================================================================

def _app_config(tmp_path: Path, *, ini_text: str = "", settings_values: dict[str, str] | None = None):
    """AppConfig isolé : config.ini contrôlé + QSettings simulé."""
    import core.config as cfg_mod

    ini_path = tmp_path / "config.ini"
    if ini_text:
        ini_path.write_text(ini_text, encoding="utf-8")
    values = settings_values or {}
    with patch("core.config.QSettings") as mock_qs:
        inst = MagicMock()
        inst.value.side_effect = lambda key, default=None: values.get(key, default)
        mock_qs.return_value = inst
        with patch.object(cfg_mod, "_INI_PATH", ini_path), \
             patch("core.config._app_data_dir", return_value=tmp_path), \
             patch.dict(os.environ, {}, clear=False):
            config = cfg_mod.AppConfig()
    return config, inst


def _entry_bytes(number: int, track_type: int, codec_id: str) -> bytes:
    return element(TRACK_ENTRY_ID, b"".join((
        uint_element(TRACK_NUMBER_ID, number),
        uint_element(TRACK_UID_ID, number),
        uint_element(TRACK_TYPE_ID, track_type),
        ascii_element(CODEC_ID_ID, codec_id),
    )))


def _simple_cluster(track_number: int, payload: bytes = b"\x00" * 16) -> bytes:
    block = bytes([0x80 | track_number]) + b"\x00\x00\x80" + payload
    return element(CLUSTER_ID, uint_element(TIMESTAMP_ID, 0) + element(SIMPLE_BLOCK_ID, block))


def _write_mkv(path: Path, entries: list[bytes], *, clusters: bytes = b"", extra_top_level: bytes = b"") -> Path:
    path.write_bytes(
        element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff"
        + element(TRACKS_ID, b"".join(entries)) + extra_top_level + clusters
    )
    return path


def _transaction_contract() -> MatroskaOutputContract:
    return MatroskaOutputContract(
        track_types=("video",),
        expected_tracks=(ExpectedMatroskaTrack("video", require_packets=True),),
        duration_coherent=False,
    )


class TestMatroskaOutputTransaction:

    def _transaction(self, tmp_path: Path, *, run_command, post_actions=(), write_nfo=None, warnings=None):
        return MatroskaOutputTransaction(
            output=tmp_path / "out.mkv",
            contract=_transaction_contract(),
            ffprobe_bin="ffprobe",
            run_command=run_command,
            post_actions=tuple(post_actions),
            write_nfo=write_nfo,
            warn=(warnings.append if warnings is not None else None),
        )

    @staticmethod
    def _valid_candidate(path: Path) -> None:
        _write_mkv(
            path,
            [_entry_bytes(1, 1, "V_MPEG4/ISO/AVC")],
            clusters=_simple_cluster(1),
        )

    @pytest.mark.parametrize("failure_stage", ["ffmpeg", "post", "validation", "ffprobe"])
    def test_failure_preserves_old_output_and_removes_candidate(
        self, tmp_path: Path, failure_stage: str,
    ) -> None:
        output = tmp_path / "out.mkv"
        output.write_bytes(b"OLD")

        def run(command, _cwd, label, _progress, _signals):
            if label == "ffmpeg-final":
                if failure_stage == "ffmpeg":
                    raise RuntimeError("ffmpeg KO")
                if failure_stage == "validation":
                    Path(command[-1]).write_bytes(b"corrupt")
                else:
                    self._valid_candidate(Path(command[-1]))
            if label == "ffprobe-validation" and failure_stage == "ffprobe":
                raise RuntimeError("ffprobe KO")
            return "ok"

        def post(_candidate: Path) -> None:
            if failure_stage == "post":
                raise RuntimeError("post KO")

        transaction = self._transaction(tmp_path, run_command=run, post_actions=(post,))
        with pytest.raises(RuntimeError):
            transaction.execute(
                ["ffmpeg", "-y", str(output)],
                cwd=tmp_path,
                label="ffmpeg-final",
                signals=TaskSignals(),
            )
        assert output.read_bytes() == b"OLD"
        assert not transaction.candidate.exists()

    def test_success_orders_actions_commits_then_warns_on_nfo(self, tmp_path: Path) -> None:
        output = tmp_path / "out.mkv"
        output.write_bytes(b"OLD")
        order: list[str] = []
        warnings: list[str] = []

        def run(command, _cwd, label, _progress, _signals):
            if label == "ffmpeg-final":
                assert command[-3:-1] == ["-f", "matroska"]
                self._valid_candidate(Path(command[-1]))
                order.append("ffmpeg")
            elif label == "ffprobe-validation":
                order.append("ffprobe")
            return "ok"

        def action(name: str):
            return lambda candidate: order.append(name) or candidate

        def fail_nfo(path: Path) -> None:
            assert path == output and path.is_file()
            order.append("nfo")
            raise RuntimeError("nfo KO")

        transaction = self._transaction(
            tmp_path,
            run_command=run,
            post_actions=(action("muxing"), action("language")),
            write_nfo=fail_nfo,
            warnings=warnings,
        )
        result = transaction.execute(
            ["ffmpeg", "-y", str(output)],
            cwd=tmp_path,
            label="ffmpeg-final",
            signals=TaskSignals(),
            extra_post_actions=(action("dovi"),),
        )
        assert result == "ok"
        assert order == ["ffmpeg", "muxing", "language", "dovi", "ffprobe", "nfo"]
        assert output.read_bytes() != b"OLD"
        assert not transaction.candidate.exists()
        assert warnings and "NFO" in warnings[0]

    def test_cancellation_before_commit_preserves_old_output(self, tmp_path: Path) -> None:
        output = tmp_path / "out.mkv"
        output.write_bytes(b"OLD")
        signals = TaskSignals()

        def run(command, _cwd, label, _progress, _signals):
            if label == "ffmpeg-final":
                self._valid_candidate(Path(command[-1]))
            return "ok"

        def cancel(_candidate: Path) -> None:
            signals.cancel()

        transaction = self._transaction(tmp_path, run_command=run, post_actions=(cancel,))
        with pytest.raises(TaskCancelledError):
            transaction.execute(
                ["ffmpeg", "-y", str(output)],
                cwd=tmp_path,
                label="ffmpeg-final",
                signals=signals,
            )
        assert output.read_bytes() == b"OLD"
        assert not transaction.candidate.exists()


def _video_settings(**kw: Any) -> VideoEncodeSettings:
    defaults: dict[str, Any] = dict(codec="libx265", quality_mode=QualityMode.CRF, crf=18)
    defaults.update(kw)
    return VideoEncodeSettings(**cast(Any, defaults))


def _encode_config(tmp_path: Path, **kw: Any) -> EncodeConfig:
    source = kw.pop("source", None)
    if source is None:
        source = tmp_path / "src.mkv"
        if not source.exists():
            source.write_bytes(b"src")
    defaults: dict[str, Any] = dict(
        source=source,
        output=tmp_path / "out.mkv",
        video=_video_settings(),
        audio_tracks=[],
        copy_subtitles=False,
        keep_chapters=False,
        duration_s=60.0,
    )
    defaults.update(kw)
    return EncodeConfig(**cast(Any, defaults))


# =============================================================================
# 2.1 — Setting global [matroska] mux_backend
# =============================================================================

class TestMatroskaMuxSetting:

    def test_default_is_ffmpeg_without_configuration(self, tmp_path: Path) -> None:
        config, _settings = _app_config(tmp_path)
        assert config.matroska_mux_backend == "ffmpeg"
        assert config.remux_mux_backend == "ffmpeg"  # alias de compatibilité
        assert config.load_warnings == []

    def test_reads_new_ini_section(self, tmp_path: Path) -> None:
        config, _settings = _app_config(tmp_path, ini_text="[matroska]\nmux_backend = native\n")
        assert config.matroska_mux_backend == "native"

    def test_migrates_explicit_legacy_ini_key(self, tmp_path: Path) -> None:
        config, _settings = _app_config(tmp_path, ini_text="[remux]\nmux_backend = auto\n")
        assert config.matroska_mux_backend == "auto"

    def test_migrates_explicit_legacy_qsettings_key(self, tmp_path: Path) -> None:
        config, _settings = _app_config(
            tmp_path, settings_values={"remux/mux_backend": "native"},
        )
        assert config.matroska_mux_backend == "native"

    def test_new_key_wins_over_legacy(self, tmp_path: Path) -> None:
        config, _settings = _app_config(
            tmp_path,
            ini_text="[matroska]\nmux_backend = ffmpeg\n\n[remux]\nmux_backend = auto\n",
        )
        assert config.matroska_mux_backend == "ffmpeg"

    def test_invalid_value_falls_back_to_ffmpeg_with_warning(self, tmp_path: Path) -> None:
        config, _settings = _app_config(tmp_path, ini_text="[matroska]\nmux_backend = turbo\n")
        assert config.matroska_mux_backend == "ffmpeg"
        assert any("mux_backend invalide" in warning for warning in config.load_warnings)

    def test_save_writes_new_key_only(self, tmp_path: Path) -> None:
        config, settings = _app_config(tmp_path, ini_text="[matroska]\nmux_backend = auto\n")
        config.save()
        saved_keys = [call.args[0] for call in settings.setValue.call_args_list]
        assert "matroska/mux_backend" in saved_keys
        assert "remux/mux_backend" not in saved_keys

    def test_to_dict_exposes_matroska_section(self, tmp_path: Path) -> None:
        config, _settings = _app_config(tmp_path)
        assert config.to_dict()["matroska"] == {"mux_backend": "ffmpeg"}

    def test_ini_field_groups_expose_muxage_matroska_section(self) -> None:
        from core.config import INI_FIELD_GROUPS

        group = next(group for group in INI_FIELD_GROUPS if group["section"] == "matroska")
        assert group["title"] == "Muxage Matroska"
        field = group["fields"][0]
        assert field["attr"] == "matroska_mux_backend"
        assert field["options"][0][0] == "ffmpeg"


# =============================================================================
# 2.1 — Priorité job > setting global > FFmpeg
# =============================================================================

class TestJobPriority:

    def test_normalize_defaults_to_ffmpeg(self) -> None:
        assert normalize_mux_backend(None) == "ffmpeg"
        assert normalize_mux_backend("") == "ffmpeg"

    def test_dataclass_defaults_are_ffmpeg(self, tmp_path: Path) -> None:
        source = tmp_path / "in.mkv"
        source.touch()
        remux = RemuxConfig(
            sources=[SourceInput(source, 0, [])],
            output=tmp_path / "out.mkv",
            track_order=[],
        )
        assert remux.mux_backend == "ffmpeg"
        assert _encode_config(tmp_path).mux_backend == "ffmpeg"

    def test_job_template_defaults_to_ffmpeg(self) -> None:
        from cli.remux_config import config_to_template

        template = config_to_template({})
        assert template["mux_backend"] == "ffmpeg"
        explicit = config_to_template({"mux_backend": "native"})
        assert explicit["mux_backend"] == "native"


# =============================================================================
# 2.1 — Propagation remux → encode
# =============================================================================

class TestBridgePropagation:

    def _remux_config(self, tmp_path: Path, *, backend: str, keep_chapters: bool = True) -> RemuxConfig:
        source = tmp_path / "in.mkv"
        source.touch()
        track = TrackEntry(0, "video", "COPY", "", "und", "", file_id="s")
        return RemuxConfig(
            sources=[SourceInput(source, 0, [track])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            keep_chapters=keep_chapters,
            mux_backend=backend,
        )

    def test_propagates_on_passthrough_path(self, tmp_path: Path) -> None:
        encode = _encode_config(tmp_path, keep_chapters=True)
        merged = merge_remux_into_encode_config(encode, self._remux_config(tmp_path, backend="native"))
        assert merged.mux_backend == "native"

    def test_propagates_on_full_rebuild_path(self, tmp_path: Path) -> None:
        encode = _encode_config(tmp_path, keep_chapters=False)
        merged = merge_remux_into_encode_config(
            encode, self._remux_config(tmp_path, backend="auto", keep_chapters=True),
        )
        assert merged.mux_backend == "auto"


# =============================================================================
# 2.3 — Routage auto/native/ffmpeg par pipeline
# =============================================================================

class TestEncodeMuxSelector:

    def test_requested_ffmpeg_short_circuits(self, tmp_path: Path) -> None:
        config = _encode_config(tmp_path, mux_backend="ffmpeg")
        decision = select_encode_mux_backend(config, pipeline=PIPELINE_NVENCC_DIRECT)
        assert decision.selected == "ffmpeg"
        assert decision.diagnostics == ()

    def test_inject_pipeline_native_eligible_in_auto(self, tmp_path: Path) -> None:
        # Lot 3 : l'artefact réécrit porte timestamps et signalisation DoVi —
        # les chemins d'injection deviennent éligibles au natif en auto.
        config = _encode_config(tmp_path, mux_backend="auto")
        decision = select_encode_mux_backend(config, pipeline=PIPELINE_METADATA_INJECT)
        assert decision.selected == "native"
        assert not decision.uses_fallback

    def test_auto_selects_native_for_eligible_nvencc(self, tmp_path: Path) -> None:
        config = _encode_config(
            tmp_path, mux_backend="auto",
            audio_tracks=[AudioTrackSettings(stream_index=1, codec="aac")],
            copy_subtitles=True,
        )
        decision = select_encode_mux_backend(config, pipeline=PIPELINE_NVENCC_DIRECT)
        assert decision.selected == "native"

    def test_auto_keeps_ffmpeg_for_direct_single_pass(self, tmp_path: Path) -> None:
        config = _encode_config(tmp_path, mux_backend="auto")
        decision = select_encode_mux_backend(config, pipeline=PIPELINE_FFMPEG_DIRECT)
        assert decision.selected == "ffmpeg"
        assert not decision.uses_fallback  # choix de coût, pas un repli sur blocage
        assert "monopasse" in decision.reason

    def test_requested_native_uses_native_for_direct_pipeline(self, tmp_path: Path) -> None:
        config = _encode_config(tmp_path, mux_backend="native")
        decision = select_encode_mux_backend(config, pipeline=PIPELINE_FFMPEG_DIRECT)
        assert decision.selected == "native"
        assert not decision.diagnostics

    def test_dynamic_hdr_blocks_native_nvencc_but_not_multi_video(self, tmp_path: Path) -> None:
        video = _video_settings()
        video.copy_dv = True
        config = _encode_config(tmp_path, mux_backend="auto", video=video, video_tracks=[video])
        nvencc = select_encode_mux_backend(config, pipeline=PIPELINE_NVENCC_DIRECT)
        assert nvencc.selected == "ffmpeg"
        assert any("NVEncC" in reason for reason in nvencc.diagnostics)
        # Multi-vidéo : le rewriter du lot 3 embarque la signalisation DoVi.
        multi = select_encode_mux_backend(config, pipeline=PIPELINE_MULTI_VIDEO)
        assert multi.selected == "native"

    def test_native_encode_supports_offsets_flags_tags_and_materialized_inputs(self, tmp_path: Path) -> None:
        mp4 = tmp_path / "in.mp4"
        mp4.touch()
        config = _encode_config(
            tmp_path, mux_backend="auto",
            audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy", source_path=mp4)],
            track_time_offsets=[TrackOffset("audio", mp4, 1, offset_ms=40)],
            track_meta_edits=[TrackMetaPatch(track_order=1, flag_default=True)],
            attachment_streams=[(mp4, 3)],
            tag_sources=[mp4],
            output=tmp_path / "out.mp4",
        )
        blockers = encode_native_mux_blockers(config, pipeline=PIPELINE_NVENCC_DIRECT)
        text = " | ".join(blockers)
        assert "sorties .mkv" in text
        assert "décalages temporels" not in text
        assert "non Matroska" not in text
        assert "flags de piste" not in text
        assert "attachments par stream" not in text
        assert "tags sources" not in text

    def test_strict_native_accepts_track_offsets_without_fallback(self, tmp_path: Path) -> None:
        source = tmp_path / "src.mkv"
        config = _encode_config(
            tmp_path, mux_backend="native",
            track_time_offsets=[TrackOffset("audio", source, 1, offset_ms=40)],
        )
        decision = select_encode_mux_backend(config, pipeline=PIPELINE_NVENCC_DIRECT)
        assert decision.selected == "native"
        assert not decision.diagnostics
        assert not decision.uses_fallback


# =============================================================================
# 2.4 — Préflight remux : cache de reader et chemin FFmpeg
# =============================================================================

class TestRemuxPreviewPlanningCost:

    @staticmethod
    def _config(tmp_path: Path, *, backend: str) -> RemuxConfig:
        source = _write_mkv(
            tmp_path / "source.mkv",
            [_entry_bytes(1, 1, "V_MPEG4/ISO/AVC")],
            clusters=_simple_cluster(1),
        )
        track = TrackEntry(
            mkv_tid=0,
            track_type="video",
            codec="H264",
            display_info="",
            language="und",
            title="",
            file_id="source-0",
        )
        return RemuxConfig(
            sources=[SourceInput(path=source, file_index=0, tracks=[track])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0, track.entry_id)],
            keep_chapters=False,
            mux_backend=backend,
        )

    def test_native_plan_scans_track_headers_once_per_source(self, tmp_path: Path, monkeypatch) -> None:
        """Le préflight et le contrat partagent le reader d'une compilation."""
        calls = 0
        original = MatroskaReader.top_level

        def _counted_top_level(reader):
            nonlocal calls
            calls += 1
            yield from original(reader)

        monkeypatch.setattr(MatroskaReader, "top_level", _counted_top_level)

        plan = plan_remux(self._config(tmp_path, backend="native"))

        assert plan.selected_backend == "native"
        assert calls == 1

    def test_forced_ffmpeg_skips_native_capability_preflight(self, tmp_path: Path, monkeypatch) -> None:
        def _unexpected_preflight(*_args, **_kwargs):
            raise AssertionError("le préflight de capacité natif ne doit pas être exécuté")

        monkeypatch.setattr(
            "core.workflows.remux_plan.native_capability_reasons",
            _unexpected_preflight,
        )

        plan = plan_remux(self._config(tmp_path, backend="ffmpeg"))

        assert plan.selected_backend == "ffmpeg"


# =============================================================================
# 2.5 — Assemblage final natif encode (contrat partagé)
# =============================================================================

class TestNativeEncodeAssembly:

    def test_extra_cover_contract_uses_writer_canonical_name(self, tmp_path: Path) -> None:
        """Le contrat natif doit attendre le nom réellement écrit."""
        artifact = _write_mkv(
            tmp_path / "video_artifact.mkv",
            [_entry_bytes(1, 1, "V_MPEGH/ISO/HEVC")],
            clusters=_simple_cluster(1),
        )
        cover = tmp_path / "COVER.JPG"
        cover.write_bytes(b"jpeg")
        plan = MatroskaAssemblyPlan(
            output=tmp_path / "out.mkv",
            ordered_tracks=(MatroskaAssemblyTrack(
                artifact=artifact,
                artifact_track_index=0,
                source_identity="video-source",
            ),),
            extra_attachment_files=(cover,),
        )

        contract = assembly_output_contract(plan)

        assert contract.attachment_names == ("cover.jpg",)
        assert contract.expected_attachments[0].name == "cover.jpg"

    def _fake_run_cmd(self, recorded: list[list[str]]):
        def _run(cmd: list[str], label: str) -> str:
            recorded.append(list(cmd))
            if label.startswith("ffmpeg-native-audio"):
                _write_mkv(
                    Path(cmd[-1]),
                    [_entry_bytes(1, 2, "A_AAC")],
                    clusters=_simple_cluster(1),
                )
            return "ok"
        return _run

    def _sources(self, tmp_path: Path) -> tuple[Path, Path]:
        source = _write_mkv(
            tmp_path / "src.mkv",
            [_entry_bytes(1, 2, "A_EAC3"), _entry_bytes(2, 17, "S_TEXT/UTF8")],
            clusters=_simple_cluster(1) + _simple_cluster(2, payload=b"Bonjour"),
            extra_top_level=element(CHAPTERS_ID, b""),
        )
        artifact = _write_mkv(
            tmp_path / "video_artifact.mkv",
            [_entry_bytes(1, 1, "V_MPEGH/ISO/HEVC")],
            clusters=_simple_cluster(1),
        )
        return source, artifact

    def test_assembles_copy_tracks_with_metadata_parity(self, tmp_path: Path) -> None:
        source, artifact = self._sources(tmp_path)
        config = _encode_config(
            tmp_path,
            source=source,
            audio_tracks=[AudioTrackSettings(stream_index=0, codec="copy")],
            copy_subtitles=True,
            keep_chapters=True,
            tag_overrides={"title": "Tag titre"},
            file_title="Mon titre",
            track_meta_edits=[TrackMetaPatch(track_order=2, language="fra", title="Audio FR")],
            work_dir=tmp_path,
        )
        recorded: list[list[str]] = []
        output = assemble_encode_output_native(
            config,
            video_artifacts=[NativeVideoArtifactRef(artifact, 0, 0)],
            work_dir=tmp_path,
            signals=None,
            run_cmd=self._fake_run_cmd(recorded),
            log=lambda _level, _message: None,
            ffmpeg_bin="ffmpeg",
            ffprobe_bin="ffprobe",
        )
        assert output.is_file()
        assert not list(tmp_path.rglob("*.partial"))
        reader = MatroskaReader(output)
        tracks = reader.tracks()
        assert [track.track_type for track in tracks] == [1, 2, 17]
        audio = tracks[1]
        assert audio.name == "Audio FR"
        assert audio.language not in ("", "und")
        # Chapitres copiés, tags écrits, titre de segment appliqué.
        assert reader.raw_top_level(CHAPTERS_ID)
        assert reader.raw_top_level(TAGS_ID)
        assert reader.segment_title() == "Mon titre"
        muxing_app, _writing_app = reader.segment_info_apps()
        assert muxing_app.startswith("Muxiveo")
        # Second validateur ffprobe passé par le run_cmd injecté.
        assert any(cmd[0] == "ffprobe" for cmd in recorded)

    def test_native_assembly_writes_all_track_flags(self, tmp_path: Path) -> None:
        source, artifact = self._sources(tmp_path)
        config = _encode_config(
            tmp_path,
            source=source,
            audio_tracks=[AudioTrackSettings(stream_index=0, codec="copy")],
            copy_subtitles=False,
            keep_chapters=False,
            track_meta_edits=[TrackMetaPatch(
                track_order=2,
                flag_default=False,
                flag_forced=True,
                flag_hearing_impaired=True,
                flag_visual_impaired=True,
                flag_original=True,
                flag_commentary=True,
            )],
            work_dir=tmp_path,
        )
        output = assemble_encode_output_native(
            config,
            video_artifacts=[NativeVideoArtifactRef(artifact)],
            work_dir=tmp_path,
            signals=None,
            run_cmd=self._fake_run_cmd([]),
            log=lambda _level, _message: None,
        )
        audio = MatroskaReader(output).tracks()[1]
        assert not audio.flag_default
        assert audio.flag_forced
        assert audio.flag_hearing_impaired
        assert audio.flag_visual_impaired
        assert audio.flag_original
        assert audio.flag_commentary

    def test_native_assembly_materializes_non_matroska_copied_audio(self, tmp_path: Path) -> None:
        foreign = tmp_path / "audio.mp4"
        foreign.write_bytes(b"mp4")
        artifact = _write_mkv(
            tmp_path / "video_artifact.mkv",
            [_entry_bytes(1, 1, "V_MPEGH/ISO/HEVC")],
            clusters=_simple_cluster(1),
        )
        config = _encode_config(
            tmp_path,
            source=foreign,
            audio_tracks=[AudioTrackSettings(stream_index=0, codec="copy", source_path=foreign)],
            copy_subtitles=False,
            keep_chapters=False,
            work_dir=tmp_path,
        )
        recorded: list[list[str]] = []

        def _run(cmd: list[str], label: str) -> str:
            recorded.append(cmd)
            if label.startswith("ffmpeg-native-canonical-track"):
                _write_mkv(Path(cmd[-1]), [_entry_bytes(1, 2, "A_AAC")], clusters=_simple_cluster(1))
            return "ok"

        output = assemble_encode_output_native(
            config,
            video_artifacts=[NativeVideoArtifactRef(artifact)],
            work_dir=tmp_path,
            signals=None,
            run_cmd=_run,
            log=lambda _level, _message: None,
        )
        assert [track.track_type for track in MatroskaReader(output).tracks()] == [1, 2]
        canonical = next(cmd for cmd in recorded if "native_track_0.mkv" in str(cmd[-1]))
        assert "-c" in canonical and "copy" in canonical
        assert not (tmp_path / "native_track_0.mkv").exists()

    def test_native_input_preparation_cleans_partial_artifacts_on_failure(self, tmp_path: Path) -> None:
        first = tmp_path / "first.mp4"
        second = tmp_path / "second.mp4"
        first.write_bytes(b"mp4")
        second.write_bytes(b"mp4")
        config = _encode_config(
            tmp_path,
            source=first,
            tag_sources=[first, second],
            tag_overrides=None,
        )

        def _run(command: list[str], _label: str) -> str:
            target = Path(command[-1])
            if "native_container_0" in target.name:
                target.write_bytes(b"partial")
                return "ok"
            target.write_bytes(b"partial")
            raise RuntimeError("ffmpeg failed")

        with pytest.raises(RuntimeError, match="ffmpeg failed"):
            prepare_native_encode_inputs(
                config,
                work_dir=tmp_path,
                ffmpeg_bin="ffmpeg",
                run_cmd=_run,
            )

        assert not list(tmp_path.glob("native_container_*.mkv"))

    def test_keep_chapters_on_chapterless_source_succeeds(self, tmp_path: Path) -> None:
        """keep_chapters=True sur une source SANS chapitres : le contrat ne
        doit pas exiger de chapitres (sinon toute source sans chapitres rend
        l'assemblage natif inatteignable — cas corpus réel)."""
        source = _write_mkv(
            tmp_path / "src_no_chapters.mkv",
            [_entry_bytes(1, 2, "A_EAC3")],
            clusters=_simple_cluster(1),
        )
        artifact = _write_mkv(
            tmp_path / "video_artifact.mkv",
            [_entry_bytes(1, 1, "V_MPEGH/ISO/HEVC")],
            clusters=_simple_cluster(1),
        )
        config = _encode_config(
            tmp_path,
            source=source,
            audio_tracks=[AudioTrackSettings(stream_index=0, codec="copy")],
            copy_subtitles=False,
            keep_chapters=True,
            work_dir=tmp_path,
        )
        output = assemble_encode_output_native(
            config,
            video_artifacts=[NativeVideoArtifactRef(artifact, 0, 0)],
            work_dir=tmp_path,
            signals=None,
            run_cmd=self._fake_run_cmd([]),
            log=lambda _level, _message: None,
            ffmpeg_bin="ffmpeg",
            ffprobe_bin="ffprobe",
        )
        assert output.is_file()
        assert not MatroskaReader(output).raw_top_level(CHAPTERS_ID)

    def test_materializes_encoded_audio_artifacts(self, tmp_path: Path) -> None:
        source, artifact = self._sources(tmp_path)
        config = _encode_config(
            tmp_path,
            source=source,
            audio_tracks=[AudioTrackSettings(
                stream_index=0, codec="aac", bitrate_kbps=128, input_channels=6,
            )],
            copy_subtitles=False,
            keep_chapters=False,
            work_dir=tmp_path,
        )
        recorded: list[list[str]] = []
        output = assemble_encode_output_native(
            config,
            video_artifacts=[NativeVideoArtifactRef(artifact, 0, 0)],
            work_dir=tmp_path,
            signals=None,
            run_cmd=self._fake_run_cmd(recorded),
            log=lambda _level, _message: None,
            ffmpeg_bin="ffmpeg",
            ffprobe_bin="ffprobe",
        )
        audio_cmd = next(cmd for cmd in recorded if "native_audio_0.mkv" in str(cmd[-1]))
        assert "-c:a:0" in audio_cmd and "aac" in audio_cmd
        assert any(arg.startswith("-b:a") for arg in audio_cmd)
        assert not (tmp_path / "native_audio_0.mkv").exists()
        tracks = MatroskaReader(output).tracks()
        assert [track.track_type for track in tracks] == [1, 2]

    def test_cleans_partial_audio_artifact_when_materialization_fails(self, tmp_path: Path) -> None:
        source, artifact = self._sources(tmp_path)
        config = _encode_config(
            tmp_path,
            source=source,
            audio_tracks=[AudioTrackSettings(stream_index=0, codec="aac")],
            copy_subtitles=False,
            keep_chapters=False,
            work_dir=None,
        )

        def _failing_run(cmd: list[str], label: str) -> str:
            if label.startswith("ffmpeg-native-audio"):
                Path(cmd[-1]).write_bytes(b"partial")
                raise EncodeError("audio failed")
            return "ok"

        with pytest.raises(EncodeError, match="audio failed"):
            assemble_encode_output_native(
                config,
                video_artifacts=[NativeVideoArtifactRef(artifact, 0, 0)],
                work_dir=tmp_path,
                signals=None,
                run_cmd=_failing_run,
                log=lambda _level, _message: None,
            )

        assert not (tmp_path / "native_audio_0.mkv").exists()


# =============================================================================
# 2.5 — Preview : encodeur vidéo vs muxeur Matroska, post-patchs FFmpeg only
# =============================================================================

class TestExecutionPreviewAndValidate:

    def _workflow(self):
        from core.workflows.encode.workflow import EncodeWorkflow

        return EncodeWorkflow(
            ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool",
            hdr10plus_bin="hdr10plus_tool",
        )

    def test_direct_preview_separates_encoder_and_muxer(self, tmp_path: Path) -> None:
        wf = self._workflow()
        config = _encode_config(tmp_path, mux_backend="auto")
        report = wf.execution_preview(config)
        assert report["video_encode_backend"] == "ffmpeg"
        assert report["requested_mux_backend"] == "auto"
        assert report["selected_mux_backend"] == "ffmpeg"
        assert report["pipeline"] == PIPELINE_FFMPEG_DIRECT
        assert report["post_patches"] == ["muxing_app", "language"]

    def test_multi_video_native_preview_has_no_post_patches(self, tmp_path: Path) -> None:
        wf = self._workflow()
        source = _write_mkv(
            tmp_path / "src.mkv",
            [_entry_bytes(1, 1, "V_MPEGH/ISO/HEVC"), _entry_bytes(2, 1, "V_MPEGH/ISO/HEVC")],
        )
        video_a = _video_settings(stream_index=0)
        video_b = _video_settings(stream_index=1)
        config = _encode_config(
            tmp_path, source=source, mux_backend="auto",
            video=video_a, video_tracks=[video_a, video_b],
        )
        report = wf.execution_preview(config)
        assert report["pipeline"] == PIPELINE_MULTI_VIDEO
        assert report["selected_mux_backend"] == "native"
        assert report["post_patches"] == []
        assert any("video_0.mkv" in artifact for artifact in cast(list, report["temporary_artifacts"]))
        command_preview = wf.preview_command(config)
        assert "# Muxage final Matroska : native (demandé : auto)" in command_preview
        assert "Assemblage final interne" in command_preview

    def test_ffmpeg_attachment_contract_uses_command_mime(self, tmp_path: Path) -> None:
        wf = self._workflow()
        font = tmp_path / "subtitle.ttf"
        font.write_bytes(b"font-data")
        config = _encode_config(tmp_path, extra_attachments=[font])

        contract = build_encode_output_contract(
            config,
            wf._build_encode_plan(config),
        )

        assert contract.expected_attachments[0].media_type == mime_for_path(font)

    def test_webm_source_track_is_inspected_strictly(self, tmp_path: Path) -> None:
        from core.workflows.encode.output_contract import _source_track

        source = _write_mkv(
            tmp_path / "source.webm",
            [_entry_bytes(1, 1, "V_VP9")],
            clusters=_simple_cluster(1),
        )

        assert _source_track(source, 0) is not None

    def test_duplicate_attachment_names_match_distinct_headers(self, tmp_path: Path) -> None:
        attachments = build_attachments_element([
            MatroskaAttachment(1, "cover.jpg", "image/jpeg", "", b"a"),
            MatroskaAttachment(2, "cover.jpg", "image/jpeg", "", b"bb"),
        ])
        output = _write_mkv(
            tmp_path / "duplicates.mkv",
            [_entry_bytes(1, 1, "V_MPEG4/ISO/AVC")],
            clusters=_simple_cluster(1),
            extra_top_level=attachments,
        )
        contract = MatroskaOutputContract(
            track_types=("video",),
            expected_tracks=(ExpectedMatroskaTrack("video", require_packets=True),),
            attachment_names=("cover.jpg", "cover.jpg"),
            expected_attachments=(
                ExpectedMatroskaAttachment("cover.jpg", "image/jpeg", size=1),
                ExpectedMatroskaAttachment("cover.jpg", "image/jpeg", size=2),
            ),
            strict_attachment_names=True,
            duration_coherent=False,
        )

        assert validate_matroska_output(output, contract) == []

    def test_validate_accepts_strict_native_track_offsets(self, tmp_path: Path) -> None:
        wf = self._workflow()
        source = tmp_path / "src.mkv"
        source.write_bytes(b"src")
        config = _encode_config(
            tmp_path, source=source, mux_backend="native",
            track_time_offsets=[TrackOffset("audio", source, 1, offset_ms=80)],
        )
        errors = wf.validate(config)
        assert not any("Backend natif indisponible" in error for error in errors)


# =============================================================================
# 2.7 — Correctifs audit externe : multi-source, annulation, gating audio
# =============================================================================

class TestExternalAuditFixes:
    """Non-régression des 4 défauts relevés par l'audit externe.

    - sous-titres copiés depuis TOUTES les sources du layout (plan transmis
      aux adaptateurs natifs + résolution native équivalente) ;
    - annulation de l'assemblage natif convertie en TaskCancelledError ;
    - audio réencodé depuis un conteneur non-Matroska sans blocage natif ;
    - copie implicite de sous-titres : toutes les sources du layout gardées.
    """

    def _run_assembly(self, config: EncodeConfig, artifact: Path, tmp_path: Path, **kw: Any) -> Path:
        return assemble_encode_output_native(
            config,
            video_artifacts=[NativeVideoArtifactRef(artifact, 0, 0)],
            work_dir=tmp_path,
            signals=kw.pop("signals", None),
            run_cmd=lambda cmd, label: "ok",
            log=lambda _level, _message: None,
            ffmpeg_bin="ffmpeg",
            ffprobe_bin="ffprobe",
            **kw,
        )

    def _dual_sources(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        primary = _write_mkv(
            tmp_path / "primary.mkv",
            [_entry_bytes(1, 2, "A_EAC3"), _entry_bytes(2, 17, "S_TEXT/UTF8")],
            clusters=_simple_cluster(1) + _simple_cluster(2, payload=b"Sub A"),
        )
        secondary = _write_mkv(
            tmp_path / "secondary.mkv",
            [_entry_bytes(1, 2, "A_AAC"), _entry_bytes(2, 17, "S_TEXT/ASS")],
            clusters=_simple_cluster(1) + _simple_cluster(2, payload=b"Sub B"),
        )
        artifact = _write_mkv(
            tmp_path / "video_artifact.mkv",
            [_entry_bytes(1, 1, "V_MPEGH/ISO/HEVC")],
            clusters=_simple_cluster(1),
        )
        return primary, secondary, artifact

    def test_native_assembly_copies_subtitles_from_all_sources(self, tmp_path: Path) -> None:
        """copy_subtitles multi-source : les sous-titres de CHAQUE source sont copiés."""
        primary, secondary, artifact = self._dual_sources(tmp_path)
        config = _encode_config(
            tmp_path,
            source=primary,
            audio_tracks=[
                AudioTrackSettings(stream_index=0, codec="copy"),
                AudioTrackSettings(stream_index=0, codec="copy", source_path=str(secondary)),
            ],
            copy_subtitles=True,
            work_dir=tmp_path,
        )
        output = self._run_assembly(config, artifact, tmp_path)
        reader = MatroskaReader(output)
        subtitle_codecs = sorted(
            track.codec_id for track in reader.tracks() if track.track_type == 17
        )
        assert subtitle_codecs == ["S_TEXT/ASS", "S_TEXT/UTF8"]

    def test_plan_resolution_wins_over_native_fallback(self, tmp_path: Path) -> None:
        """Les sous-titres résolus du plan (toutes sources) priment sur le fallback."""
        primary, secondary, artifact = self._dual_sources(tmp_path)
        config = _encode_config(
            tmp_path,
            source=primary,
            audio_tracks=[AudioTrackSettings(stream_index=0, codec="copy")],
            copy_subtitles=True,
            work_dir=tmp_path,
        )
        output = self._run_assembly(
            config, artifact, tmp_path,
            resolved_subtitles=[(secondary, 1)],
        )
        subtitle_codecs = [
            track.codec_id for track in MatroskaReader(output).tracks()
            if track.track_type == 17
        ]
        assert subtitle_codecs == ["S_TEXT/ASS"]

    def test_native_subtitle_resolution_rejects_non_matroska_source(self, tmp_path: Path) -> None:
        """Fallback natif : une source non-Matroska du layout lève (pas de perte muette)."""
        from core.workflows.encode.runtime.native_mux import resolve_native_subtitle_tracks

        primary = _write_mkv(
            tmp_path / "primary.mkv",
            [_entry_bytes(1, 2, "A_EAC3")],
            clusters=_simple_cluster(1),
        )
        foreign = tmp_path / "second.mp4"
        foreign.write_bytes(b"mp4")
        config = _encode_config(
            tmp_path,
            source=primary,
            audio_tracks=[AudioTrackSettings(stream_index=0, codec="copy", source_path=str(foreign))],
            copy_subtitles=True,
        )
        with pytest.raises(EncodeError, match="non\\s+Matroska"):
            resolve_native_subtitle_tracks(config)

    def test_cancelled_native_assembly_raises_task_cancelled(self, tmp_path: Path) -> None:
        """Annulation pendant l'écriture native : TaskCancelledError, pas un échec."""
        primary, _secondary, artifact = self._dual_sources(tmp_path)
        config = _encode_config(
            tmp_path,
            source=primary,
            audio_tracks=[AudioTrackSettings(stream_index=0, codec="copy")],
            copy_subtitles=False,
            work_dir=tmp_path,
        )
        signals = TaskSignals()
        signals._cancel_event.set()
        with pytest.raises(TaskCancelledError):
            self._run_assembly(config, artifact, tmp_path, signals=signals)
        assert not list(tmp_path.rglob("*.partial"))

    def test_reencoded_audio_from_non_mkv_does_not_block_native(self, tmp_path: Path) -> None:
        """Audio réencodé depuis un conteneur non-MKV : matérialisé, aucun blocage."""
        foreign = tmp_path / "audio.mp4"
        foreign.write_bytes(b"mp4")
        config = _encode_config(
            tmp_path,
            audio_tracks=[AudioTrackSettings(stream_index=0, codec="aac", source_path=str(foreign))],
            copy_subtitles=False,
            keep_chapters=False,
        )
        reasons = encode_native_mux_blockers(config, pipeline=PIPELINE_FFMPEG_DIRECT)
        assert not any("audio.mp4" in reason for reason in reasons)

    def test_copied_audio_from_non_mkv_is_materialized_for_native(self, tmp_path: Path) -> None:
        """Audio copié depuis un conteneur non-MKV : artefact MKV préparé."""
        foreign = tmp_path / "audio.mp4"
        foreign.write_bytes(b"mp4")
        config = _encode_config(
            tmp_path,
            audio_tracks=[AudioTrackSettings(stream_index=0, codec="copy", source_path=str(foreign))],
            copy_subtitles=False,
            keep_chapters=False,
        )
        reasons = encode_native_mux_blockers(config, pipeline=PIPELINE_FFMPEG_DIRECT)
        assert not any("audio.mp4" in reason for reason in reasons)

    def test_implicit_subtitle_copy_uses_preparation_for_layout_sources(self, tmp_path: Path) -> None:
        """Les sources secondaires non-MKV sont préparées avant le mux natif."""
        foreign = tmp_path / "second.mp4"
        foreign.write_bytes(b"mp4")
        config = _encode_config(
            tmp_path,
            audio_tracks=[AudioTrackSettings(stream_index=0, codec="aac", source_path=str(foreign))],
            copy_subtitles=True,
            keep_chapters=False,
        )
        reasons = encode_native_mux_blockers(config, pipeline=PIPELINE_FFMPEG_DIRECT)
        assert not any("second.mp4" in reason for reason in reasons)
