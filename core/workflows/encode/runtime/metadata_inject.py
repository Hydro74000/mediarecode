from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.bluray import append_ffmpeg_input_args
from core.subprocess_utils import subprocess_text_kwargs

from core.runner import TaskCancelledError, TaskSignals
from core.workdir import remove_path
from core.workflows.encode.domain import should_reinject_static_hdr_metadata
from core.workflows.encode.models import EncodeConfig, EncodeError, QualityMode
from core.workflows.encode.planning.track_assembly import build_track_input_paths, resolve_track_assembly
from core.workflows.encode.planning.plan_models import EncodePlan
from core.workflows.encode.runtime.frame_count_guard import (
    FrameCountAuditError,
    FrameCountGuard,
)
from core.workflows.hevc_static_hdr_metadata import inject_static_hdr_sei_file
from core.dovi_profile_detector import DoviSubProfile
from core.workflows.encode.runtime.dovi_p7_router import DoviP7Router
from core.workflows.encode.runtime.hevc_sei_normalizer import (
    strip_pic_timing_from_annexb_file,
)
from core.workflows.encode.runtime.static_hdr_estimator import StaticHdrEstimate
from core.matroska.editors.dovi import (
    DolbyVisionConfigRecord,
    MatroskaDoviBlockAdditionEditor,
)
from core.matroska.hevc.access_units import HevcStreamCancelled
from core.matroska.hevc.payload_rewriter import MatroskaHevcPayloadRewriter
from core.matroska.hevc.timing_skeleton import write_timing_skeleton
from core.matroska.writer import MatroskaWriteCancelled
from core.workflows.remux_timeline_sync import LiveSyncSession

import dataclasses


# Experimental NVENC-specific post-encode normalization kept in-tree for
# reference, but disabled in the active workflow.
ENABLE_EXPERIMENTAL_NVENC_SEI_NORMALIZATION = False


def _format_bytes(value: int) -> str:
    size = float(max(0, value))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    index = 0
    while size >= 1024.0 and index < len(units) - 1:
        size /= 1024.0
        index += 1
    return f"{size:.1f} {units[index]}"


def _friendly_dovi_conversion_error(exc: Exception, work_dir: Path) -> str:
    raw = str(exc)
    lowered = raw.lower()
    if (
        "quota exceeded" not in lowered
        and "no space left on device" not in lowered
        and "os error 122" not in lowered
        and "os error 28" not in lowered
    ):
        return f"Conversion Dolby Vision P5→P8.1 impossible : {raw}"
    try:
        free = shutil.disk_usage(work_dir).free
        free_label = _format_bytes(free)
    except OSError:
        free_label = "inconnu"
    return (
        "Conversion Dolby Vision P5→P8.1 impossible : quota ou espace temporaire "
        f"insuffisant dans {work_dir} (libre : {free_label}). "
        "Libérez de l’espace ou choisissez un dossier de travail disposant d’un quota suffisant."
    )


@dataclass(frozen=True)
class MetadataInjectRunnerCallbacks:
    ffmpeg_bin: str
    bins: dict[str, str]
    log_step: Callable[[int, str], None]
    log_info: Callable[[str], None]
    log_warn: Callable[[str], None]
    check_cancelled: Callable[[TaskSignals | None], None]
    video_source_path: Callable[[EncodeConfig], Path]
    video_stream_index: Callable[[EncodeConfig], int]
    build_video_track_encode_commands: Callable[..., list[list[str]]]
    two_pass_log_prefix: Callable[[Path, str], Path]
    cleanup_two_pass_logs_for_prefix: Callable[[Path], None]
    build_encode_plan: Callable[[EncodeConfig], EncodePlan]
    source_input_index_map: Callable[[list[Path]], dict[Path, int]]
    prepare_multisource_sync: Callable[..., tuple[dict[tuple[Path, int, str], tuple[int, int]], list[Path | str], LiveSyncSession | None, bool]]
    sync_cleanup_paths: Callable[[list[Path | str]], list[Path]]
    append_sync_inputs: Callable[[list[str], list[Path | str]], None]
    prepare_container_metadata_inputs: Callable[..., tuple[int, int | None, int | None]]
    ffmpeg_thread_args: Callable[[], list[str]]
    ffmpeg_progress_args: Callable[[], list[str]]
    video_map_key: Callable[[EncodeConfig], tuple[Path, int, str]]
    append_offset_aux_inputs: Callable[..., tuple[int, dict[tuple[Path, int, str], tuple[int, int]]]]
    build_offset_specs: Callable[..., object]
    video_map_arg: Callable[..., str]
    append_stream_maps_and_attachments: Callable[..., None]
    append_strict_interleave_mux_flags: Callable[[list[str]], None]
    append_container_metadata_args: Callable[..., None]
    run_cmd: Callable[[list[str], TaskSignals, Path | None, Callable[[str], None] | None], str]
    finalize_ffmpeg: Callable[..., str]
    estimate_static_hdr: Callable[..., object]
    report_static_hdr_estimate: Callable[[str, object], None]
    report_static_hdr_failure: Callable[[str, str], None]
    #: Assemblage final Matroska natif (lot 3). None → reconstruction FFmpeg.
    native_assemble: Callable[..., None] | None = None


class MetadataInjectRunner:
    """Runner dédié au pipeline d'injection DoVi/HDR10+."""

    def __init__(self, callbacks: MetadataInjectRunnerCallbacks) -> None:
        self._callbacks = callbacks

    def run(
        self,
        config: EncodeConfig,
        *,
        prep_signals: TaskSignals | None = None,
        plan: EncodePlan | None = None,
    ) -> TaskSignals:
        cb = self._callbacks
        signals = prep_signals or TaskSignals()
        executor = None if prep_signals is not None else ThreadPoolExecutor(max_workers=1)

        # La transaction complète dépasse la limite d'analyse de flux de Pyright.
        def _task() -> None:  # pyright: ignore[reportGeneralTypeIssues]
            work = config.work_dir or Path(tempfile.gettempdir())
            work.mkdir(parents=True, exist_ok=True)
            tmp_dir = tempfile.mkdtemp(
                prefix="Muxiveo_encode_",
                dir=str(work),
            )
            tmp = Path(tmp_dir)
            ext_files: list[Path] = []
            live_sync_session: LiveSyncSession | None = None
            sync_cleanup_paths: list[Path] = []

            def _run(cmd: list[str]) -> str:
                return cb.run_cmd(
                    cmd,
                    signals,
                    tmp,
                    lambda line: signals.progress.emit(line),
                )

            def _check() -> None:
                if signals._cancel_event.is_set():
                    raise TaskCancelledError()

            def _alloc(name: str, ref_size: int) -> Path:
                _ = ref_size
                return tmp / name

            def _free(path: Path) -> None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                try:
                    ext_files.remove(path)
                except ValueError:
                    pass

            try:
                rpu_bin = tmp / "rpu.bin"
                rpu_preextracted = False
                selected_video_source = cb.video_source_path(config)
                selected_video_stream = cb.video_stream_index(config)
                src_size_est = selected_video_source.stat().st_size
                video = config.video
                if video is None:
                    raise ValueError("EncodeConfig.video is required for metadata injection")

                # STEP 4-bis : routage P7/P5 → P8.1 si besoin.
                # P7 peut réutiliser son base layer. P5 nécessite deux chemins :
                # dovi_tool convertit le RPU, tandis que libplacebo transforme
                # réellement les pixels IPT en base layer HDR10 BT.2020/PQ.
                # Pour P8.x → no-op, le config reste tel quel (aucune
                # étape supplémentaire).
                effective_config = config
                p7_router_decision = None
                if video.copy_dv:
                    p7_router = DoviP7Router()
                    mi_video = _load_mediainfo_video(
                        cb.bins.get("mediainfo", "mediainfo"),
                        selected_video_source,
                    )
                    p7_router_decision = p7_router.analyze(
                        source=selected_video_source,
                        mi_video=mi_video,
                        fallback_to_dovi_tool=True,
                    )
                    cb.log_info(
                        f"Routage DV : {p7_router_decision.reason}"
                    )
                    if p7_router_decision.conversion_needed:
                        cb.log_step(
                            4,
                            f"Conversion {p7_router_decision.sub_profile.label} "
                            f"→ P8.1 (dovi_tool convert)",
                        )
                        # `dovi_tool convert` n'accepte que du HEVC annexB
                        # brut. On extrait depuis le MKV/MP4 source en
                        # intermédiaire jetable, qui sera supprimé dès la
                        # conversion terminée pour respecter la contrainte
                        # « max 2 fichiers vidéo en parallèle ».
                        annexb_for_convert = tmp / "source_annexb.hevc"
                        signals.progress.emit("Extraction HEVC annexB pour conversion DoVi…")
                        annexb_cmd = [cb.ffmpeg_bin, "-nostdin", "-y"]
                        append_ffmpeg_input_args(annexb_cmd, selected_video_source)
                        annexb_cmd.extend([
                            "-map", f"0:{int(selected_video_stream)}", "-c", "copy",
                            "-bsf:v", "hevc_mp4toannexb",
                            "-f", "hevc", str(annexb_for_convert),
                        ])
                        _run(annexb_cmd)
                        _check()
                        signals.progress.emit(
                            f"Conversion {p7_router_decision.sub_profile.label} → P8.1…"
                        )
                        try:
                            converted = p7_router.execute_conversion(
                                source=annexb_for_convert,
                                output_dir=tmp,
                                run_cmd=lambda c: _run(c),
                                dovi_tool_bin=cb.bins["dovi_tool"],
                                decision=p7_router_decision,
                            )
                        except Exception as exc:
                            message = _friendly_dovi_conversion_error(exc, tmp)
                            request_mode = str(
                                getattr(video, "static_hdr_metadata_analysis_request", "") or ""
                            ).strip()
                            if request_mode:
                                cb.report_static_hdr_failure(
                                    str(getattr(video, "track_entry_id", "") or ""),
                                    message,
                                )
                            raise EncodeError(message) from exc
                        # Annexb extrait n'est plus utile (consommé par convert).
                        try:
                            annexb_for_convert.unlink(missing_ok=True)
                        except OSError:
                            pass
                        ext_files.append(converted)
                        if p7_router_decision.sub_profile == DoviSubProfile.P5:
                            signals.progress.emit("Extraction du RPU P8.1 converti…")
                            _run([
                                cb.bins["dovi_tool"], "extract-rpu",
                                "-i", str(converted), "-o", str(rpu_bin),
                            ])
                            _check()
                            rpu_preextracted = True
                            _free(converted)

                            runtime_codec = str(video.codec or "copy").strip().lower()
                            if runtime_codec == "copy":
                                runtime_codec = "libx265"
                                cb.log_warn(
                                    "P5→P8.1 exige un réencodage du base layer : "
                                    "remux remplacé par libx265 10-bit CRF 16."
                                )
                            video = dataclasses.replace(
                                video,
                                codec=runtime_codec,
                                quality_mode=(
                                    QualityMode.CRF
                                    if str(video.codec or "").strip().lower() == "copy"
                                    else video.quality_mode
                                ),
                                crf=(
                                    16
                                    if str(video.codec or "").strip().lower() == "copy"
                                    else video.crf
                                ),
                                preset=(
                                    "slow"
                                    if str(video.codec or "").strip().lower() == "copy"
                                    else video.preset
                                ),
                                force_8bit=False,
                                force_10bit=True,
                                p5_to_hdr10=True,
                            )
                            runtime_tracks = []
                            for track_index, track in enumerate(config.video_tracks):
                                same_track = bool(
                                    video.track_entry_id
                                    and track.track_entry_id == video.track_entry_id
                                )
                                runtime_tracks.append(
                                    video
                                    if same_track or (not video.track_entry_id and track_index == 0)
                                    else track
                                )
                            effective_config = dataclasses.replace(
                                config,
                                video=video,
                                video_tracks=runtime_tracks or [video],
                            )
                            cb.log_info(
                                "Conversion colorimétrique P5 IPT→HDR10 BT.2020/PQ "
                                "activée via libplacebo."
                            )
                            continue_rebound = False
                        else:
                            continue_rebound = True
                        # On clone EncodeConfig en redirigeant `source` ET
                        # `video_tracks[*].source_path` vers le HEVC P8.1.
                        # `_video_source_path` lit `video.source_path` en
                        # priorité (config.source n'est utilisé qu'en
                        # fallback) — sans rebind du source_path, le STEP 5
                        # ré-extrait le RPU depuis la MKV P7 d'origine et
                        # injecte un RPU P7 dans un BL P8 → DV cassé en
                        # lecture (Plex/TV).
                        # `config` original reste intact pour STEP 8/9
                        # (timestamps source, audio, subs, chapitres).
                        if continue_rebound:
                            rebound_tracks = [
                                dataclasses.replace(track, source_path=converted, stream_index=0)
                                for track in config.video_tracks
                            ]
                            rebound_video = (
                                dataclasses.replace(config.video, source_path=converted, stream_index=0)
                                if config.video is not None
                                else None
                            )
                            effective_config = dataclasses.replace(
                                config,
                                source=converted,
                                video=rebound_video,
                                video_tracks=rebound_tracks,
                            )
                        _check()

                if video.copy_dv or video.copy_hdr10plus:
                    cb.log_step(5, "Extraction des métadonnées dynamiques (DoVi/HDR10+)")
                else:
                    cb.log_step(5, "Préparation de l'injection HDR statique")
                # dovi_tool / hdr10plus_tool n'acceptent nativement que MKV ou
                # HEVC annexB brut. Pour MP4/MOV/TS/..., il faut extraire d'abord
                # en HEVC annexB via ffmpeg + bsf hevc_mp4toannexb, sinon le
                # parser HEVC panique sur le VPS.
                _RAW_HEVC_EXT = {".hevc", ".h265", ".265", ".x265"}
                meta_src = cb.video_source_path(effective_config)
                meta_src_ext = meta_src.suffix.lower()
                needs_annexb = meta_src_ext not in _RAW_HEVC_EXT and meta_src_ext != ".mkv"
                if needs_annexb and (video.copy_dv or video.copy_hdr10plus):
                    annexb_src = _alloc("source.hevc", src_size_est)
                    signals.progress.emit("Extraction HEVC annexB pour outillage DoVi/HDR10+…")
                    annexb_cmd = [cb.ffmpeg_bin, "-nostdin", "-y"]
                    append_ffmpeg_input_args(annexb_cmd, meta_src)
                    annexb_cmd.extend([
                        "-map", "0:v:0", "-c", "copy",
                        "-bsf:v", "hevc_mp4toannexb",
                        "-f", "hevc", str(annexb_src),
                    ])
                    _run(annexb_cmd)
                    _check()
                    meta_input = annexb_src
                else:
                    meta_input = meta_src

                if video.copy_dv and not rpu_preextracted:
                    signals.progress.emit("Extraction RPU Dolby Vision…")
                    _run([
                        cb.bins["dovi_tool"], "extract-rpu",
                        "-i", str(meta_input), "-o", str(rpu_bin),
                    ])
                    _check()

                hdr10p_json = tmp / "hdr10p.json"
                if video.copy_hdr10plus:
                    signals.progress.emit("Extraction métadonnées HDR10+…")
                    _run([
                        cb.bins["hdr10plus_tool"], "extract",
                        str(meta_input), "-o", str(hdr10p_json),
                    ])
                    _check()

                if needs_annexb and (video.copy_dv or video.copy_hdr10plus):
                    _free(meta_input)

                native_dovi_record = None
                if cb.native_assemble is not None and video.copy_dv:
                    if not rpu_bin.is_file():
                        raise EncodeError(
                            "Dolby Vision natif requis mais aucun RPU n'a été extrait."
                        )
                    native_dovi_record = _build_dovi_record_from_rpu(
                        rpu_bin=rpu_bin,
                        dovi_tool_bin=cb.bins["dovi_tool"],
                        forced_compat_id=_resolve_dovi_compat_id(
                            p7_router_decision=p7_router_decision,
                            user_dovi_profile=str(video.dovi_profile or "0"),
                        ),
                    )
                    if native_dovi_record is None:
                        raise EncodeError(
                            "Dolby Vision natif requis mais le DolbyVisionConfigRecord "
                            "n'a pas pu être construit."
                        )

                cb.log_step(6, "Encodage vidéo vers MKV mono-piste horodaté")
                enc_video_mkv = _alloc("enc_video.mkv", src_size_est)
                signals.progress.emit("Encodage vidéo…")
                passlog_prefix = (
                    cb.two_pass_log_prefix(tmp, "inject_video")
                    if video.quality_mode == QualityMode.SIZE
                    else None
                )
                encode_cmds = cb.build_video_track_encode_commands(
                    effective_config,
                    video,
                    cb.video_source_path(effective_config),
                    enc_video_mkv,
                    passlog_prefix=passlog_prefix,
                )
                try:
                    for pass_index, encode_cmd in enumerate(encode_cmds, start=1):
                        if len(encode_cmds) > 1:
                            cb.log_info(
                                f"Passe {pass_index}/{len(encode_cmds)} "
                                f"({'analyse' if pass_index == 1 else 'encodage'})…"
                            )
                        _run(encode_cmd)
                        _check()
                finally:
                    if passlog_prefix is not None:
                        cb.cleanup_two_pass_logs_for_prefix(passlog_prefix)
                # Squelette de timing : mêmes TrackEntry/timestamps que le MKV
                # encodé, payloads vides (quelques Mo). Il portera seul la
                # vérité temporelle jusqu'à la réécriture des payloads.
                timing_mkv = _alloc("enc_timing.mkv", 64 * 1024 * 1024)
                signals.progress.emit("Écriture du squelette de timing…")
                try:
                    skeleton_result = write_timing_skeleton(
                        enc_video_mkv, timing_mkv,
                        cancel_cb=signals._cancel_event.is_set,
                    )
                except MatroskaWriteCancelled as exc:
                    raise TaskCancelledError() from exc
                signals.progress.emit(
                    f"Squelette de timing écrit : {skeleton_result.blocks_written} bloc(s)."
                )
                # Extraction annexB depuis l'artefact encodé : les timestamps
                # de la source d'origine ne sont plus jamais réassociés au
                # bitstream. Le MKV encodé complet est libéré aussitôt — le
                # squelette suffit à la réécriture (pic disque 2× vidéo).
                enc_hevc = _alloc("enc.hevc", src_size_est)
                signals.progress.emit("Extraction annexB depuis l'artefact encodé…")
                _run([
                    cb.ffmpeg_bin, "-nostdin", "-y",
                    "-i", str(enc_video_mkv),
                    "-map", "0:v:0", "-c", "copy",
                    "-bsf:v", "hevc_mp4toannexb",
                    "-f", "hevc", str(enc_hevc),
                ])
                _check()
                _free(enc_video_mkv)
                current_hevc = enc_hevc

                request_mode = str(
                    getattr(video, "static_hdr_metadata_analysis_request", "") or ""
                ).strip()
                if request_mode:
                    cb.log_info(
                        "Analyse HDR10 statique sur le base layer BT.2020/PQ encodé."
                    )
                    signals.progress.emit(
                        f"Analyse HDR10 {request_mode} sur le base layer final…"
                    )
                    try:
                        estimate_obj = cb.estimate_static_hdr(
                            current_hevc,
                            duration_s=config.duration_s,
                            mode=request_mode,
                            progress_cb=signals.progress.emit,
                            cancel_cb=lambda: signals._cancel_event.is_set(),
                        )
                    except TaskCancelledError:
                        raise
                    except Exception as exc:
                        if signals._cancel_event.is_set():
                            raise TaskCancelledError() from exc
                        message = f"Analyse HDR10 P5→P8.1 impossible : {exc}"
                        cb.report_static_hdr_failure(
                            str(getattr(video, "track_entry_id", "") or ""),
                            message,
                        )
                        raise EncodeError(message) from exc
                    if not isinstance(estimate_obj, StaticHdrEstimate):
                        message = "Analyse HDR10 P5→P8.1 impossible : résultat interne invalide."
                        cb.report_static_hdr_failure(
                            str(getattr(video, "track_entry_id", "") or ""),
                            message,
                        )
                        raise EncodeError(message)

                    video = dataclasses.replace(
                        video,
                        inject_hdr_meta=True,
                        master_display=estimate_obj.master_display,
                        max_cll=estimate_obj.max_cll,
                        static_hdr_metadata_source=estimate_obj.source,
                        static_hdr_metadata_confidence=estimate_obj.confidence,
                        static_hdr_metadata_analysis_mode=estimate_obj.mode,
                        static_hdr_metadata_analysis_request="",
                    )
                    updated_tracks = []
                    for track_index, track in enumerate(effective_config.video_tracks):
                        same_track = bool(
                            video.track_entry_id
                            and track.track_entry_id == video.track_entry_id
                        )
                        if same_track or (not video.track_entry_id and track_index == 0):
                            track = dataclasses.replace(
                                track,
                                inject_hdr_meta=True,
                                master_display=estimate_obj.master_display,
                                max_cll=estimate_obj.max_cll,
                                static_hdr_metadata_source=estimate_obj.source,
                                static_hdr_metadata_confidence=estimate_obj.confidence,
                                static_hdr_metadata_analysis_mode=estimate_obj.mode,
                                static_hdr_metadata_analysis_request="",
                            )
                        updated_tracks.append(track)
                    effective_config = dataclasses.replace(
                        effective_config,
                        video=video,
                        video_tracks=updated_tracks or [video],
                    )
                    cb.report_static_hdr_estimate(
                        str(getattr(video, "track_entry_id", "") or ""),
                        estimate_obj,
                    )
                    cb.log_warn(
                        "HDR10 statique estimé dans le workflow depuis le base layer "
                        f"final (mode {estimate_obj.mode}, confiance "
                        f"{estimate_obj.confidence}) : {estimate_obj.max_cll}."
                    )

                # Audit frame count : empêche l'injection sur un stream qui
                # aurait dérapé en frame count (NVENC drop, NVDEC dup, filtre
                # non frame-preserving). Sans ça, l'injection RPU/HDR10+ se
                # désaligne silencieusement et on produit un fichier où les
                # scènes-cuts DV ne tombent plus aux bons endroits.
                if video.copy_dv or video.copy_hdr10plus:
                    cb.log_info("Audit alignement frame count (source/encoded/RPU/HDR10+)…")
                    guard = FrameCountGuard(
                        mediainfo_bin=cb.bins.get("mediainfo", "mediainfo"),
                        ffprobe_bin=cb.bins.get("ffprobe", "ffprobe"),
                        dovi_tool_bin=cb.bins["dovi_tool"],
                    )
                    audit = guard.audit(
                        source=cb.video_source_path(effective_config),
                        encoded=current_hevc,
                        rpu_bin=rpu_bin if (video.copy_dv and rpu_bin.exists()) else None,
                        hdr10p_json=hdr10p_json if (video.copy_hdr10plus and hdr10p_json.exists()) else None,
                    )
                    try:
                        guard.enforce(
                            audit,
                            rpu_bin=rpu_bin if (video.copy_dv and rpu_bin.exists()) else None,
                            hdr10p_json=hdr10p_json if (video.copy_hdr10plus and hdr10p_json.exists()) else None,
                            on_warn=lambda msg: signals.progress.emit(f"[WARN] {msg}"),
                            on_info=lambda msg: signals.progress.emit(msg),
                        )
                    except FrameCountAuditError as exc:
                        raise RuntimeError(f"Audit frame count : {exc}") from exc
                    _check()

                # Si on avait converti P7/P5 → P8.1 en amont, le HEVC
                # converti a fini son rôle (il a servi d'input à l'extract
                # RPU/HDR10+ et à l'encode, et l'audit frame count est
                # passé). On le supprime pour respecter la contrainte
                # « max 2 fichiers vidéo intermédiaires en parallèle ».
                if (
                    p7_router_decision is not None
                    and p7_router_decision.conversion_needed
                    and effective_config.source != config.source
                ):
                    _free(effective_config.source)
                    effective_config = config

                cb.log_step(7, "Injection des métadonnées vidéo HDR")
                if video.copy_hdr10plus and hdr10p_json.exists():
                    cur_size = current_hevc.stat().st_size
                    out_hdr10p = _alloc("enc_hdr10p.hevc", cur_size)
                    signals.progress.emit("Injection métadonnées HDR10+…")
                    _run([
                        cb.bins["hdr10plus_tool"], "inject",
                        "-i", str(current_hevc),
                        "-j", str(hdr10p_json),
                        "-o", str(out_hdr10p),
                    ])
                    _free(current_hevc)
                    current_hevc = out_hdr10p
                    _check()

                if video.copy_dv and rpu_bin.exists():
                    cur_size = current_hevc.stat().st_size
                    out_dv = _alloc("enc_dv.hevc", cur_size)
                    signals.progress.emit("Injection RPU Dolby Vision…")
                    # Si une normalisation P7/P5→P8.1 a effectivement eu
                    # lieu en amont, on force -m 2 pour que le RPU réinjecté
                    # soit explicitement tagué P8.1. `inject-rpu` n'accepte
                    # pas --compat-id : le mode est porté par `-m`.
                    inject_mode = video.dovi_profile
                    if (
                        p7_router_decision is not None
                        and p7_router_decision.conversion_needed
                        and inject_mode == "0"
                    ):
                        inject_mode = "2"
                    inject_cmd = [
                        cb.bins["dovi_tool"],
                        "-m", inject_mode,
                        "inject-rpu",
                        "-i", str(current_hevc),
                        "-r", str(rpu_bin),
                        "-o", str(out_dv),
                    ]
                    _run(inject_cmd)
                    _free(current_hevc)
                    current_hevc = out_dv
                    _check()

                if should_reinject_static_hdr_metadata(video):
                    cur_size = current_hevc.stat().st_size
                    out_static_hdr = _alloc("enc_hdr_static.hevc", cur_size)
                    if str(getattr(video, "static_hdr_metadata_source", "") or "") == "estimated_p5_to_p8":
                        confidence = str(getattr(video, "static_hdr_metadata_confidence", "") or "?")
                        mode = str(getattr(video, "static_hdr_metadata_analysis_mode", "") or "?")
                        cb.log_warn(
                            "Injection HDR10 statique estimée depuis analyse P5→P8.1 "
                            f"(confiance {confidence}, mode {mode}) — fallback approximatif, "
                            "pas metadata source."
                        )
                    signals.progress.emit("Injection metadonnees HDR statiques…")
                    static_hdr_result = inject_static_hdr_sei_file(
                        current_hevc,
                        out_static_hdr,
                        master_display=video.master_display,
                        max_cll=video.max_cll,
                    )
                    if static_hdr_result.applied:
                        _free(current_hevc)
                        current_hevc = out_static_hdr
                        signals.progress.emit(
                            "SEI HDR statiques injectes "
                            f"sur {static_hdr_result.injected_access_units} access unit(s)."
                        )
                    else:
                        _free(out_static_hdr)
                    _check()

                if (
                    ENABLE_EXPERIMENTAL_NVENC_SEI_NORMALIZATION
                    and bool(getattr(video, "strip_pic_timing_sei", False))
                ):
                    cur_size = current_hevc.stat().st_size
                    out_sei_norm = _alloc("enc_sei_norm.hevc", cur_size)
                    signals.progress.emit("Normalisation SEI HEVC (suppression pic_timing)…")
                    cb.log_info("Normalisation expérimentale du flux HEVC : retrait des SEI pic_timing.")
                    sei_stats = strip_pic_timing_from_annexb_file(current_hevc, out_sei_norm)
                    if sei_stats.pic_timing_messages_removed > 0:
                        _free(current_hevc)
                        current_hevc = out_sei_norm
                        signals.progress.emit(
                            "SEI pic_timing retires : "
                            f"{sei_stats.pic_timing_messages_removed} message(s), "
                            f"{sei_stats.sei_nals_rewritten} NAL re-ecrit(s), "
                            f"{sei_stats.sei_nals_dropped} NAL supprime(s)."
                        )
                    else:
                        _free(out_sei_norm)
                        signals.progress.emit("Aucun SEI pic_timing detecte dans le flux HEVC injecte.")
                    _check()

                cb.log_step(8, "Réécriture des payloads dans le squelette de timing")
                wrapped_video = _alloc("enc_wrapped.mkv", current_hevc.stat().st_size)
                # Aucun repli après le démarrage : la sélection du backend a
                # eu lieu au préflight ; un échec ici est fatal (pas de
                # fallback FFmpeg silencieux).
                record_for_muxer = native_dovi_record
                if record_for_muxer is None and video.copy_dv and rpu_bin.exists():
                    record_for_muxer = _build_dovi_record_from_rpu(
                        rpu_bin=rpu_bin,
                        dovi_tool_bin=cb.bins["dovi_tool"],
                        forced_compat_id=_resolve_dovi_compat_id(
                            p7_router_decision=p7_router_decision,
                            user_dovi_profile=str(video.dovi_profile or "0"),
                        ),
                    )
                signals.progress.emit(
                    "Réécriture des payloads vidéo (timestamps de l'encodeur conservés)…"
                )
                try:
                    rewrite_result = MatroskaHevcPayloadRewriter().rewrite(
                        encoded_mkv=timing_mkv,
                        injected_hevc=current_hevc,
                        output=wrapped_video,
                        dovi_record=record_for_muxer,
                        cancel_cb=signals._cancel_event.is_set,
                    )
                except (HevcStreamCancelled, MatroskaWriteCancelled) as exc:
                    raise TaskCancelledError() from exc
                signals.progress.emit(
                    f"Payloads réécrits : {rewrite_result.frames_rewritten} frame(s) — "
                    "CodecPrivate et signalisation Dolby Vision portés par le TrackEntry."
                )
                _free(current_hevc)
                _free(timing_mkv)
                current_video_input = wrapped_video
                _check()

                if cb.native_assemble is not None:
                    cb.log_step(9, "Assemblage final Matroska natif")
                    cb.native_assemble(
                        config,
                        intermediate=current_video_input,
                        video_offset_ms=0,
                        signals=signals,
                        plan=plan or cb.build_encode_plan(config),
                        work_dir=tmp,
                    )
                    signals.finished.emit(str(config.output))
                    return

                cb.log_step(9, "Reconstruction finale du conteneur MKV")
                signals.progress.emit("Reconstitution finale…")
                encode_plan = plan or cb.build_encode_plan(config)
                all_sources = list(encode_plan.all_sources)
                extra_sources = all_sources[1:]
                recon_source_idx = cb.source_input_index_map(all_sources)
                sync_remap: dict[tuple[Path, int, str], tuple[int, int]] = {}
                sync_inputs: list[Path | str] = []
                strict_interleave = False

                recon_cmd = [
                    cb.ffmpeg_bin,
                    "-hide_banner",
                    "-y",
                    *cb.ffmpeg_progress_args(),
                    "-i",
                    str(current_video_input),
                ]
                append_ffmpeg_input_args(recon_cmd, config.source)
                for sp in extra_sources:
                    append_ffmpeg_input_args(recon_cmd, sp)

                sync_remap, sync_inputs, live_sync_session, strict_interleave = cb.prepare_multisource_sync(
                    config=config,
                    all_sources=all_sources,
                    sync_base_input_idx=2 + len(extra_sources),
                    work_dir=tmp,
                    signals=signals,
                    allow_live=True,
                    plan=encode_plan,
                )
                sync_cleanup_paths = cb.sync_cleanup_paths(sync_inputs)
                cb.append_sync_inputs(recon_cmd, sync_inputs)
                if live_sync_session is not None:
                    for proc in live_sync_session.processes:
                        signals._register_proc(proc)

                next_input_index, chapter_input_index, tag_input_index = cb.prepare_container_metadata_inputs(
                    recon_cmd,
                    config,
                    source_idx=recon_source_idx,
                    next_input_index=2 + len(extra_sources) + len(sync_inputs),
                    plan=encode_plan,
                    chapter_materialize_dir=tmp,
                    chapter_probe_source=config.source,
                )

                recon_cmd.extend(cb.ffmpeg_thread_args())
                resolved_subtitle_tracks = list(encode_plan.resolved_subtitle_tracks)
                video_key = cb.video_map_key(config)
                video_default_map = (0, 0)
                track_assembly = resolve_track_assembly(
                    config,
                    encode_plan,
                    source_idx=recon_source_idx,
                    track_input_paths=build_track_input_paths(
                        leading_inputs=[current_video_input],
                        all_sources=all_sources,
                        sync_inputs=sync_inputs,
                    ),
                    sync_remap=sync_remap,
                    video_default_map=video_default_map,
                    video_fallback_input=current_video_input,
                )

                next_input_index, offset_remap = cb.append_offset_aux_inputs(
                    recon_cmd,
                    cb.build_offset_specs(
                        config,
                        track_mappings=list(track_assembly.track_mappings),
                        offset_lookup=dict(encode_plan.offset_lookup),
                    ),
                    start_input_index=next_input_index,
                )
                _ = next_input_index

                recon_cmd.extend([
                    "-map",
                    cb.video_map_arg(
                        track_assembly.video_map,
                        offset_remap=offset_remap,
                        map_key=video_key,
                    ),
                    "-c:v",
                    "copy",
                ])
                cb.append_stream_maps_and_attachments(
                    recon_cmd,
                    config,
                    source_idx=recon_source_idx,
                    subtitle_copy_input_indices=list(range(1, 2 + len(extra_sources))),
                    sync_remap=sync_remap,
                    offset_remap=offset_remap,
                    subtitle_tracks_override=resolved_subtitle_tracks,
                    force_copy_subtitles_wildcard=(config.copy_subtitles and not encode_plan.subtitles_resolved),
                )
                if strict_interleave:
                    cb.append_strict_interleave_mux_flags(recon_cmd)

                cb.append_container_metadata_args(
                    recon_cmd,
                    config,
                    default_metadata_input_index=0,
                    default_chapter_input_index=1,
                    chapter_input_index=chapter_input_index,
                    tag_input_index=tag_input_index,
                    plan=encode_plan,
                )
                recon_cmd.append(str(config.output))

                dovi_post_actions: tuple[Callable[[Path], object], ...] = ()
                if video.copy_dv and rpu_bin.exists():
                    cb.log_info("Injection signal Dolby Vision au niveau Matroska…")
                    forced_compat_id = _resolve_dovi_compat_id(
                        p7_router_decision=p7_router_decision,
                        user_dovi_profile=str(video.dovi_profile or "0"),
                    )
                    def _patch_dovi(candidate: Path) -> object:
                        record = _build_dovi_record_from_rpu(
                            rpu_bin=rpu_bin,
                            dovi_tool_bin=cb.bins["dovi_tool"],
                            forced_compat_id=forced_compat_id,
                        )
                        if record is None:
                            signals.progress.emit(
                                "[WARN] Impossible d'extraire le profil DOVI du RPU "
                                "pour le signal Matroska."
                            )
                            return None
                        try:
                            patch_result = MatroskaDoviBlockAdditionEditor().patch(
                                candidate, record=record,
                            )
                            if patch_result.applied:
                                signals.progress.emit(
                                    f"BlockAdditionMapping DOVI ajouté "
                                    f"(track #{patch_result.patched_track_number}, "
                                    f"Δ {patch_result.bytes_delta:+d} octets)."
                                )
                            elif patch_result.skipped:
                                signals.progress.emit(
                                    f"Signal DOVI Matroska non modifié : {patch_result.reason}"
                                )
                        except Exception as patch_exc:
                            # On loggue mais on n'échoue pas le workflow : le
                            # fichier reste lisible, juste sans signal DV.
                            signals.progress.emit(
                                f"[WARN] Patch BlockAdditionMapping DOVI échoué : {patch_exc}"
                            )
                            return None
                        return patch_result

                    dovi_post_actions = (_patch_dovi,)

                output = cb.finalize_ffmpeg(
                    config,
                    recon_cmd,
                    tmp,
                    "ffmpeg-reconstruct",
                    signals,
                    plan=encode_plan,
                    extra_post_actions=dovi_post_actions,
                    # Politique historique FFmpeg : le patch DoVi reste non
                    # bloquant ; seul le backend natif rend le mapping strict.
                    require_dovi_video_indexes=set(),
                )

                signals.finished.emit(output)

            except TaskCancelledError:
                signals.cancelled.emit()
            except Exception as exc:
                signals.failed.emit(str(exc), exc)
            finally:
                if live_sync_session is not None:
                    for proc in live_sync_session.processes:
                        signals._unregister_proc(proc)
                    live_sync_session.close()
                for path in sync_cleanup_paths:
                    try:
                        remove_path(path)
                    except OSError:
                        pass
                for p in list(ext_files):
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        pass
                shutil.rmtree(tmp_dir, ignore_errors=True)

        if prep_signals is None:
            assert executor is not None
            executor.submit(_task)
            executor.shutdown(wait=False)
        else:
            _task()
        return signals


# ----------------------------------------------------------------------------
# Helpers de bas niveau
# ----------------------------------------------------------------------------


def _load_mediainfo_video(mediainfo_bin: str, path: Path) -> dict | None:
    """Charge le track Video du JSON mediainfo, ou None si indisponible."""
    try:
        result = subprocess.run(
            [mediainfo_bin, "--Output=JSON", str(path)],
            capture_output=True, check=False,
            **subprocess_text_kwargs(),
        )
    except (FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    media = data.get("media") or {}
    for track in media.get("track") or []:
        if isinstance(track, dict) and track.get("@type") == "Video":
            return track
    return None


def _build_dovi_record_from_rpu(
    *,
    rpu_bin: Path,
    dovi_tool_bin: str,
    forced_compat_id: int | None = None,
) -> DolbyVisionConfigRecord | None:
    """
    Interroge ``dovi_tool info -i RPU --summary`` et construit le record DOVI
    correspondant pour le ``BlockAdditionMapping`` Matroska.

    Renvoie None si dovi_tool est indisponible ou si le summary ne contient
    pas les champs attendus.

    ``forced_compat_id`` permet d'imposer le ``bl_signal_compat_id`` quand
    l'appelant connaît la sub-version cible (ex. P7→P8.1 via dovi_tool
    convert -m 2). ``dovi_tool info --summary`` ne distingue PAS P8.0/P8.1
    (la sub-version vit dans le container, pas dans le RPU) — sans override
    on tombait toujours sur compat_id=0 → fichier signalé P8.0 → certaines
    TV refusent le flux ou tombent en fallback HDR10 cassé.
    """
    try:
        result = subprocess.run(
            [dovi_tool_bin, "info", "-i", str(rpu_bin), "--summary"],
            capture_output=True,
            check=False,
            **subprocess_text_kwargs(),
        )
    except (FileNotFoundError, OSError):
        return None
    text = (result.stdout or "") + (result.stderr or "")

    profile_match = re.search(r"Profile\s*:\s*(\d+)(?:\.(\d+))?", text)
    if not profile_match:
        return None
    profile = int(profile_match.group(1))
    sub_profile = int(profile_match.group(2) or 0)

    if forced_compat_id is not None:
        compat_id = int(forced_compat_id)
    else:
        # bl_signal_compat_id : 1 pour P8.1 (HDR10), 0 pour P8.0, 2 pour P8.2
        # (P8.4/HLG). On l'extrait préférentiellement de la ligne dédiée si
        # présente (dovi_tool ≥ 2.x), sinon on déduit du sub_profile.
        compat_match = re.search(r"compatibility\s*id\s*:\s*(\d+)", text, re.IGNORECASE)
        if compat_match:
            compat_id = int(compat_match.group(1))
        elif profile == 8 and sub_profile > 0:
            compat_id = sub_profile
        else:
            # Profile 8 sans sub : par défaut HDR10-compatible (P8.1).
            # P8.0 strict est exotique et casse la lecture sur la majorité
            # des TV/players (pas de fallback HDR10 déclaré).
            compat_id = 1 if profile == 8 else 0

    level_match = re.search(r"DV\s+Level\s*:\s*(\d+)", text, re.IGNORECASE)
    level = int(level_match.group(1)) if level_match else 6

    # En sortie de pipeline metadata_inject, on a forcément un stream
    # mono-layer (le BL est ce que NVENC a encodé) avec RPU réinjecté.
    return DolbyVisionConfigRecord(
        profile=profile,
        level=level,
        rpu_present=True,
        el_present=False,
        bl_present=True,
        bl_signal_compat_id=max(0, min(15, compat_id)),
    )


def _should_normalize_dovi_profile(user_dovi_profile: str) -> bool:
    """Retourne True uniquement pour les modes demandant une normalisation P8.x."""
    return str(user_dovi_profile or "0").strip() == "2"


def _resolve_dovi_compat_id(
    *,
    p7_router_decision: object | None,
    user_dovi_profile: str,
) -> int | None:
    """
    Détermine le ``bl_signal_compat_id`` cible pour le BlockAddition Matroska.

    Priorité :
    1. Si le routeur P7 a converti via -m 2/-m 5 (P7→P8.1) → 1
    2. Si l'utilisateur a forcé "Normaliser en P8.1" (dovi_profile=2) → 1
    3. Sinon None → laisse ``_build_dovi_record_from_rpu`` deviner depuis
       le summary dovi_tool (avec fallback P8.1 si profile=8).
    """
    if p7_router_decision is not None and getattr(p7_router_decision, "conversion_needed", False):
        mode = str(getattr(p7_router_decision, "convert_mode", "") or "")
        # Modes 2, 3, 5 produisent du P8.1 (HDR10-compatible).
        # Mode 4 produit du P8.4 (HLG-compatible) → compat_id=2.
        if mode in {"2", "3", "5"}:
            return 1
        if mode == "4":
            return 2
    if _should_normalize_dovi_profile(user_dovi_profile):
        return 1
    return None
