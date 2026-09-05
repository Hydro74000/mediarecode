"""Runtime runner for the FFmpeg remux workflow."""

from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from core.runner import TaskCancelledError, TaskSignals
from core.workdir import (
    download_tmdb_cover,
    normalized_tmdb_cover_filename,
    prepare_process_work_dir,
    relocate_tmdb_covers_to_process_dir,
    remove_path,
)
from core.workflows.common.chapters import probe_media_duration_seconds, write_ffmetadata_chapters
from core.workflows.common.ffmpeg_runtime import (
    cli_path as _cli_path,
    ffmpeg_progress_args as _ffmpeg_progress_args,
)
from core.workflows.common.sync_rewrite import (
    SyncRewriteService,
    audio_bitrate_kbps_from_display_info,
    normalized_rewrite_codec,
    sync_rewrite_forced_offset,
)
from core.matroska.contract import without_expected_attachment
from core.matroska.validation import MatroskaPacketValidation, validate_matroska_output
from core.workflows.common.track_statistics import derive_output_statistics
from core.workflows.remux_plan import passthrough_source_refs
from core.workflows.common.attachments import canonical_attachment_output_name
from core.workflows.remux_attachments import extract_attached_pics as _extract_attached_pics_helper
from core.workflows.remux_mapping import (
    MappedTrack,
    requires_file_sync_fallback_for_offsets as _requires_file_sync_fallback_for_offsets_helper,
)
from core.workflows.remux_models import RemuxConfig, RemuxError
from core.workflows.remux_plan import MuxExecutionPlan
from core.workflows.remux_sync import (
    decide_strict_interleave_with_prescan as _decide_strict_interleave_with_prescan_helper,
    prepare_timeline_sync_inputs as _prepare_timeline_sync_inputs_helper,
)
from core.workflows.remux_timeline_sync import (
    FfmpegTimelineSync,
    LiveSyncSession,
    SyncPreparedInput,
    TimelineSyncFallbackHelper,
)


@dataclass(frozen=True)
class RemuxRuntimeRunnerCallbacks:
    ffmpeg_bin: str
    ffprobe_bin: str
    ffmpeg_thread_args: Callable[[], list[str]]
    build_command: Callable[..., list[str]]
    log_workflow_type: Callable[[str], None]
    log_step: Callable[[int, str], None]
    log: Callable[[str, str], None]
    bind_temp_cleanup: Callable[[TaskSignals, list[Path]], None]
    run_cmd: Callable[[list[str], Path | None, str, Callable[[str], None], TaskSignals], str]
    apply_muxing_post_action: Callable[[Path], object]
    apply_language_post_action: Callable[[Path], object]
    write_nfo: Callable[[Path], None]
    apply_statistics_post_action: Callable[..., object] = lambda _path, **_kwargs: None
    apply_track_enabled_post_action: Callable[[Path, object], object] = (
        lambda _path, _contract: None
    )
    sync_rewrite_enabled: Callable[[], bool] = lambda: False
    sync_advanced_audio_rewrite_enabled: Callable[[], bool] = lambda: False
    sync_rewrite_audio_bitrates: Callable[[], dict[str, int]] = lambda: {}


class RemuxRuntimeRunner:
    def __init__(self, callbacks: RemuxRuntimeRunnerCallbacks) -> None:
        self._cb = callbacks

    @staticmethod
    def _audio_rewrite_preserve_source(track) -> bool:
        source_codec = normalized_rewrite_codec(str(getattr(track, "orig_codec", "") or getattr(track, "codec", "") or ""))
        current_codec = normalized_rewrite_codec(str(getattr(track, "codec", "") or ""))
        if current_codec and source_codec and current_codec != source_codec:
            return False
        source_bitrate = audio_bitrate_kbps_from_display_info(
            str(getattr(track, "orig_display_info", "") or "")
        )
        current_bitrate = audio_bitrate_kbps_from_display_info(
            str(getattr(track, "display_info", "") or "")
        )
        if source_bitrate is not None and current_bitrate is not None and source_bitrate != current_bitrate:
            return False
        source_display = str(getattr(track, "orig_display_info", "") or "")
        current_display = str(getattr(track, "display_info", "") or "")
        if source_display != current_display and (source_bitrate is None) != (current_bitrate is None):
            return False
        return True

    def run(self, config: RemuxConfig, plan: MuxExecutionPlan) -> TaskSignals:
        cb = self._cb
        cb.log_workflow_type("REMUX")
        cb.log_step(1, "Plan remux validé")
        cb.log("INFO", f"Remuxage (FFmpeg) → {config.output.name}")
        cb.log_step(2, "Préparation workspace et attachments")
        work_root = config.work_dir or Path(tempfile.gettempdir())
        process_work_dir = prepare_process_work_dir(
            work_root,
            output_path=config.output,
            fallback_name="remux_job",
        )
        relocated_attachments = relocate_tmdb_covers_to_process_dir(
            [Path(p) for p in config.extra_attachments],
            work_root=work_root,
            process_dir=process_work_dir,
        )

        output_contract = plan.output_contract
        run_config = replace(config, extra_attachments=relocated_attachments)
        cwd = process_work_dir

        signals = TaskSignals()
        cb.bind_temp_cleanup(signals, [process_work_dir])
        executor = ThreadPoolExecutor(max_workers=1)

        def _task() -> None:
            nonlocal output_contract, run_config
            tmp_dir = process_work_dir
            chapter_meta_file: Path | None = None
            live_sync_session: LiveSyncSession | None = None
            sync_prepared: list[SyncPreparedInput] = []
            sync_cleanup_paths: list[Path] = []
            candidate: Path | None = None
            try:
                if signals._cancel_event.is_set():
                    raise TaskCancelledError()
                if config.tmdb_cover is not None:
                    tmdb_url, raw_tmdb_filename = config.tmdb_cover
                    tmdb_filename = normalized_tmdb_cover_filename(raw_tmdb_filename)
                    try:
                        cb.log("INFO", f"Téléchargement cover TMDB : {tmdb_filename}")
                        cover_path = download_tmdb_cover(
                            tmdb_url,
                            tmdb_filename,
                            process_work_dir / "attachments",
                        )
                        if signals._cancel_event.is_set():
                            raise TaskCancelledError()
                        run_config = replace(
                            run_config,
                            extra_attachments=[*run_config.extra_attachments, cover_path],
                            tmdb_cover=None,
                        )
                    except TaskCancelledError:
                        raise
                    except Exception as exc:
                        cb.log("WARN", f"Impossible de télécharger la cover TMDB : {exc}")
                        run_config = replace(run_config, tmdb_cover=None)
                        # Le workflow continue sans cover : le contrat de validation
                        # ne doit plus l'exiger — sinon la sortie, pourtant correcte,
                        # serait rejetée tardivement. Clé de retrait = nom canonique,
                        # identique à celui attendu par le contrat.
                        output_contract = without_expected_attachment(
                            output_contract,
                            canonical_attachment_output_name(Path(tmdb_filename)),
                        )

                extra_inputs: list[Path | str] = []
                cb.log_step(3, "Analyse du mapping pistes + pré-scan de risque")
                mapped_tracks: list[MappedTrack] = list(plan.mapped_tracks)
                strict_interleave = _decide_strict_interleave_with_prescan_helper(
                    run_config,
                    resolve_mapped_tracks=lambda _config: list(plan.mapped_tracks),
                    log_cb=cb.log,
                )

                extracted_pics = _extract_attached_pics_helper(
                    run_config,
                    tmp_dir,
                    signals,
                    ffmpeg_bin=cb.ffmpeg_bin,
                    cli_path=_cli_path,
                    log_cb=cb.log,
                )
                if extracted_pics:
                    run_config = replace(run_config, extra_attachments=[*run_config.extra_attachments, *extracted_pics])

                if strict_interleave:
                    cb.log_step(4, "Synchronisation timeline multi-source (live/fallback)")
                    allow_live_sync = True
                    if _requires_file_sync_fallback_for_offsets_helper(mapped_tracks):
                        allow_live_sync = False
                        cb.log(
                            "INFO",
                            "Décalage sur piste étrangère détecté : sync live désactivé, fallback fichier forcé.",
                        )
                    mapped_tracks, sync_prepared, live_sync_session = _prepare_timeline_sync_inputs_helper(
                        run_config,
                        mapped_tracks,
                        tmp_dir,
                        signals,
                        allow_live=allow_live_sync,
                        ffmpeg_bin=cb.ffmpeg_bin,
                        ffmpeg_thread_args=cb.ffmpeg_thread_args(),
                        log_cb=cb.log,
                        syncer_factory=FfmpegTimelineSync,
                        fallback_helper_factory=TimelineSyncFallbackHelper,
                    )
                    sync_cleanup_paths = [p for p in (item.path for item in sync_prepared) if isinstance(p, Path)]
                else:
                    cb.log_step(4, "Synchronisation timeline multi-source (non requise)")

                if cb.sync_rewrite_enabled():
                    cb.log_step(5, "Réécriture réelle des décalages audio/sous-titres")
                    rewrite_service = SyncRewriteService(
                        ffmpeg_bin=cb.ffmpeg_bin,
                        ffprobe_bin=cb.ffprobe_bin,
                        ffmpeg_progress_args=_ffmpeg_progress_args(),
                        ffmpeg_thread_args=cb.ffmpeg_thread_args(),
                        audio_bitrate_per_channel=cb.sync_rewrite_audio_bitrates(),
                        advanced_audio_enabled=cb.sync_advanced_audio_rewrite_enabled(),
                        log_cb=lambda msg: cb.log("INFO", msg),
                        progress_cb=signals.progress.emit,
                    )
                    rewritten_tracks: list[MappedTrack] = []
                    rewritten_inputs: list[SyncPreparedInput] = []
                    for mapped_track in mapped_tracks:
                        offset_ms = int(getattr(mapped_track.track, "time_shift_ms", 0) or 0)
                        if offset_ms == 0 or sync_rewrite_forced_offset(mapped_track.track):
                            rewritten_tracks.append(mapped_track)
                            continue
                        prepared = rewrite_service.maybe_materialize(
                            source_path=mapped_track.source_path,
                            stream_index=int(mapped_track.stream_index),
                            track_type=mapped_track.track.track_type,
                            codec=mapped_track.track.orig_codec or mapped_track.track.codec,
                            title=mapped_track.track.title,
                            display_info=mapped_track.track.orig_display_info or mapped_track.track.display_info,
                            offset_ms=offset_ms,
                            tmp_dir=tmp_dir,
                            input_idx=len(run_config.sources) + len(sync_prepared) + len(rewritten_inputs),
                            token=(
                                f"remux_f{mapped_track.source_file_index}_"
                                f"s{mapped_track.stream_index}_{mapped_track.out_type_index}_"
                                f"{mapped_track.track.track_type}"
                            ),
                            preserve_source_audio_params=self._audio_rewrite_preserve_source(mapped_track.track),
                            audio_target_codec=mapped_track.track.codec,
                            audio_target_bitrate_kbps=audio_bitrate_kbps_from_display_info(
                                mapped_track.track.display_info
                            ),
                            cancel_cb=signals._cancel_event.is_set,
                        )
                        if prepared is None:
                            rewritten_tracks.append(mapped_track)
                            continue
                        consumed_track = replace(
                            mapped_track.track,
                            time_shift_ms=0,
                            sync_rewrite_label=prepared.mode_label,
                        )
                        rewritten_tracks.append(replace(
                            mapped_track,
                            source_input_idx=prepared.input_idx,
                            source_path=prepared.path,
                            stream_index=0,
                            track=consumed_track,
                        ))
                        rewritten_inputs.append(SyncPreparedInput(
                            key=(
                                mapped_track.source_file_index,
                                mapped_track.stream_index,
                                mapped_track.track.track_type,
                            ),
                            path=prepared.path,
                            input_idx=prepared.input_idx,
                            container_format="matroska",
                        ))
                    if rewritten_inputs:
                        mapped_tracks = rewritten_tracks
                        sync_prepared.extend(rewritten_inputs)
                else:
                    cb.log_step(5, "Réécriture réelle des décalages désactivée")

                sync_cleanup_paths = [p for p in (item.path for item in sync_prepared) if isinstance(p, Path)]
                sync_inputs: list[Path | str] = [item.path for item in sync_prepared]
                sync_input_formats: list[str] = [item.container_format for item in sync_prepared]

                chapter_input_index: int | None = None
                if run_config.chapter_overrides:
                    cb.log_step(6, "Matérialisation des chapitres FFMetadata")
                    duration_s = probe_media_duration_seconds(cb.ffprobe_bin, run_config.sources[0].path)
                    chapter_meta_file = write_ffmetadata_chapters(
                        entries=run_config.chapter_overrides,
                        out_dir=tmp_dir,
                        duration_s=duration_s,
                    )
                    extra_inputs.append(chapter_meta_file)
                    chapter_input_index = len(run_config.sources) + len(sync_inputs) + len(extra_inputs) - 1
                else:
                    cb.log_step(6, "Chapitres: copie source ou désactivé (pas d'override)")

                cb.log_step(7, "Construction de la commande ffmpeg remux")
                cmd = cb.build_command(
                    run_config,
                    sync_inputs=sync_inputs,
                    sync_input_formats=sync_input_formats,
                    extra_inputs=extra_inputs,
                    chapter_input_index=chapter_input_index,
                    strict_interleave_override=strict_interleave,
                    mapped_tracks_override=mapped_tracks,
                )
                # Écriture atomique : FFmpeg produit un candidat « .mkv.partial »
                # (format Matroska explicite) ; la sortie finale n'est remplacée
                # qu'après post-patchs et validation.
                expected_output = {str(run_config.output), _cli_path(run_config.output)}
                if not cmd or str(cmd[-1]) not in expected_output:
                    raise RemuxError("Commande remux sans chemin de sortie attendu en dernière position.")
                candidate = plan.candidate_output
                candidate.unlink(missing_ok=True)
                cmd = [*cmd[:-1], "-f", "matroska", str(candidate)]
                cb.log("INFO", "$ " + " ".join(str(c) for c in cmd))

                cb.log_step(8, "Exécution du remux ffmpeg (candidat atomique)")
                output = cb.run_cmd(
                    cmd,
                    cwd,
                    "ffmpeg-remux",
                    lambda line: signals.progress.emit(line),
                    signals,
                )
                cb.log_step(9, "Post-actions sur le candidat (patchs conteneur)")
                cb.apply_muxing_post_action(candidate)
                cb.apply_language_post_action(candidate)
                # FlagEnabled : aucune disposition FFmpeg ne l'exprime, il est
                # écrit ici d'après le contrat (pistes désactivées demandées).
                cb.apply_track_enabled_post_action(candidate, output_contract)
                # Copie stricte : les compteurs de la sortie sont ceux des
                # pistes sources, déjà publiés par leurs tags. Les mesurer sur
                # la sortie relirait le fichier entier pour rien.
                derived_statistics = None
                if not any(p for p in sync_cleanup_paths if any(m.source_path == p for m in mapped_tracks)):
                    passthrough_refs = passthrough_source_refs(mapped_tracks)
                    if passthrough_refs is not None:
                        derived_statistics = derive_output_statistics(passthrough_refs)
                statistics_result = cb.apply_statistics_post_action(
                    candidate, statistics_by_position=derived_statistics,
                )
                cb.log_step(10, "Validation du candidat puis commit atomique")
                # Le patch de statistiques a déjà parcouru les paquets : son
                # résumé évite une seconde lecture intégrale du candidat.
                packet_validation = getattr(statistics_result, "packet_validation", None)
                validation_errors = validate_matroska_output(
                    candidate,
                    output_contract,
                    packet_validation=(
                        packet_validation
                        if isinstance(packet_validation, MatroskaPacketValidation)
                        else None
                    ),
                )
                if validation_errors:
                    raise RemuxError(
                        "Validation de la sortie remux échouée : " + " ; ".join(validation_errors)
                    )
                cb.run_cmd(
                    [
                        cb.ffprobe_bin, "-v", "error", "-show_entries",
                        "format=format_name", "-of", "json", str(candidate),
                    ],
                    cwd,
                    "ffprobe-validation",
                    lambda line: signals.progress.emit(line),
                    signals,
                )
                candidate.replace(run_config.output)
                # Un échec NFO après commit ne transforme plus un média valide
                # en workflow échoué.
                try:
                    cb.write_nfo(run_config.output)
                except Exception as nfo_exc:
                    cb.log("WARN", f"Génération NFO échouée après commit : {nfo_exc}")
                signals.finished.emit(output)
            except TaskCancelledError:
                if candidate is not None:
                    candidate.unlink(missing_ok=True)
                signals.cancelled.emit()
            except Exception as exc:
                if candidate is not None:
                    candidate.unlink(missing_ok=True)
                signals.failed.emit(str(exc), exc)
            finally:
                if live_sync_session is not None:
                    for proc in live_sync_session.processes:
                        signals._unregister_proc(proc)
                    live_sync_session.close()
                try:
                    if chapter_meta_file is not None:
                        chapter_meta_file.unlink(missing_ok=True)
                except Exception:
                    pass
                for path in sync_cleanup_paths:
                    try:
                        remove_path(path)
                    except OSError:
                        pass

        executor.submit(_task)
        executor.shutdown(wait=False)
        return signals
