"""Backend selection for Matroska remuxing.

The public contract deliberately lives here instead of in the FFmpeg runner:
an exact-job can request a backend without coupling its JSON shape to an
implementation.  The native backend is capability-gated; ``auto`` remains
backwards compatible by selecting FFmpeg when a plan needs a feature that has
not yet been materialised by the native writer.

La planification (préflight scopé, canonicalisation sélective, variantes
audio, rapport structuré) vit dans :mod:`core.workflows.remux_plan` ; ce
module conserve les backends et le runner natif.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Protocol

from core.runner import TaskCancelledError, TaskSignals
from core.matroska.assembly import (
    MatroskaAssemblyAttachment,
    MatroskaAssemblyPlan,
    MatroskaAssemblyTrack,
    MatroskaTrackFlags,
    compile_assembly_plan,
)
from core.matroska.mux_plan import (
    MatroskaMuxPlan,
    deterministic_source_identity,
)
from core.matroska.assembly import canonical_attachment_output_name
from core.matroska.contract import without_expected_attachment
from core.matroska.validation import MatroskaPacketValidation, validate_matroska_output
from core.matroska.writer import (
    MatroskaWriteCancelled, MatroskaWriteProgress, MatroskaWriter,
)
from core.workflows.remux_mapping import resolve_mapped_tracks, track_order_parts
from core.workflows.remux_mapping import normalized_language_value, resolved_global_tags
from core.workflows.remux_models import RemuxConfig, SourceInput
from core.workflows.remux_plan import (
    MATROSKA_EXTENSIONS,
    MuxBackendDecision,
    MuxExecutionPlan,
    build_audio_variant_command,
    build_canonicalization_command,
    mux_backend_report,
    native_capability_reasons,
    native_preparation_commands,
    participating_source_indexes,
    plan_remux,
    select_mux_backend,
)
from core.subprocess_utils import subprocess_text_kwargs
from core.workdir import (
    download_tmdb_cover,
    normalized_tmdb_cover_filename,
    prepare_process_work_dir,
    relocate_tmdb_covers_to_process_dir,
    remove_path,
)


class RemuxBackend(Protocol):
    """Execution contract shared by native and external remux backends."""

    name: str

    def validate(self, config: RemuxConfig) -> tuple[str, ...]: ...
    def preview(self, config: RemuxConfig) -> dict[str, object]: ...
    def execute(self, config: RemuxConfig) -> TaskSignals: ...


@dataclass
class NativeMatroskaBackend:
    log: Callable[[str, str], None]
    log_step: Callable[[int, str], None]
    ffmpeg_bin: str
    ffprobe_bin: str = "ffprobe"
    finalize: Callable[[Path], None] = lambda _path: None
    plan: MuxExecutionPlan | None = None
    name: str = "native"

    def validate(self, config: RemuxConfig) -> tuple[str, ...]:
        return native_capability_reasons(config)

    def preview(self, config: RemuxConfig) -> dict[str, object]:
        return {
            "backend": self.name,
            "plan_version": 1,
            "action": "internal_matroska_write",
            "preparation_commands": native_preparation_commands(config, self.ffmpeg_bin),
        }

    def execute(self, config: RemuxConfig) -> TaskSignals:
        return run_native_remux(
            config, log=self.log, log_step=self.log_step,
            ffmpeg_bin=self.ffmpeg_bin, ffprobe_bin=self.ffprobe_bin,
            finalize=self.finalize, plan=self.plan,
        )


@dataclass
class FfmpegRemuxBackend:
    execute_callback: Callable[[RemuxConfig], TaskSignals]
    preview_callback: Callable[[RemuxConfig], str]
    command_callback: Callable[[RemuxConfig], list[str]]
    name: str = "ffmpeg"

    def validate(self, config: RemuxConfig) -> tuple[str, ...]:
        return ()

    def preview(self, config: RemuxConfig) -> dict[str, object]:
        return {
            "backend": self.name,
            "plan_version": 1,
            "action": "external_ffmpeg",
            "command": self.command_callback(config),
            "command_text": self.preview_callback(config),
            "preparation_commands": [],
        }

    def execute(self, config: RemuxConfig) -> TaskSignals:
        return self.execute_callback(config)


def remux_assembly_plan(config: RemuxConfig) -> MatroskaAssemblyPlan:
    """Compile la configuration remux vers le contrat d'assemblage partagé."""
    mapped = resolve_mapped_tracks(config)
    participating = participating_source_indexes(config)
    source_by_index = {source.file_index: source for source in config.sources}
    identities = {
        source.file_index: source.origin_identity or deterministic_source_identity(source.path)
        for source in config.sources
        if source.file_index in participating
    }

    ordered_tracks: list[MatroskaAssemblyTrack] = []
    for item in mapped:
        track = item.track
        ordered_tracks.append(MatroskaAssemblyTrack(
            artifact=Path(item.source_path),
            artifact_track_index=item.stream_index,
            source_identity=identities[item.source_file_index],
            language_value=normalized_language_value(track),
            name=track.title,
            flags=MatroskaTrackFlags(
                enabled=track.flag_enabled,
                default=track.flag_default,
                forced=track.flag_forced,
                hearing_impaired=track.flag_hearing_impaired,
                visual_impaired=track.flag_visual_impaired,
                original=track.flag_original,
                commentary=track.flag_commentary,
            ),
            time_shift_ms=int(track.time_shift_ms or 0),
        ))

    attachments = tuple(
        MatroskaAssemblyAttachment(
            artifact=source.path,
            local_index=selected.local_index,
            source_identity=identities[source.file_index],
        )
        for source in config.sources
        for selected in source.selected_attachments
    )

    chapter_entries = tuple(config.chapter_overrides) if config.chapter_overrides is not None else None
    chapter_source: Path | None = None
    if chapter_entries is None and config.keep_chapters:
        chapter_index = config.chapter_source_index
        if chapter_index is None or chapter_index not in participating:
            chapter_index = next(
                (source.file_index for source in config.sources if source.has_chapters),
                config.sources[0].file_index,
            )
        if chapter_index not in participating:
            chapter_index = config.sources[0].file_index
        chapter_source = source_by_index[chapter_index].path

    return MatroskaAssemblyPlan(
        output=config.output,
        ordered_tracks=tuple(ordered_tracks),
        attachments=attachments,
        extra_attachment_files=tuple(Path(path) for path in config.extra_attachments),
        chapter_entries=chapter_entries,
        chapter_source=chapter_source,
        tag_overrides=resolved_global_tags(config) if config.tag_overrides is not None else None,
        tag_copy_sources=tuple(source.path for source in config.sources if source.copy_tags),
        segment_title=(
            config.file_title
            if config.file_title.strip() or config.tag_overrides is not None
            else None
        ),
        title_tag_value=config.file_title,
        artifact_order=tuple(
            source.path for source in config.sources if source.file_index in participating
        ),
        segment_info_source=config.sources[0].path,
    )


def build_native_plan(
    config: RemuxConfig,
    *,
    output_contract=None,
) -> MatroskaMuxPlan:
    """Plan bas niveau du writer, compilé via le contrat d'assemblage partagé."""
    assembly = remux_assembly_plan(config)
    if output_contract is None:
        from core.matroska.assembly import assembly_output_contract
        output_contract = assembly_output_contract(assembly)
    assembly = replace(assembly, expected_output_contract=output_contract)
    return compile_assembly_plan(assembly)


def run_native_remux(
    config: RemuxConfig,
    *,
    log: Callable[[str, str], None],
    log_step: Callable[[int, str], None],
    ffmpeg_bin: str,
    ffprobe_bin: str = "ffprobe",
    finalize: Callable[[Path], None] = lambda _path: None,
    plan: MuxExecutionPlan | None = None,
) -> TaskSignals:
    """Runner natif : mêmes signaux ``TaskSignals`` que le runner FFmpeg.

    Toutes les commandes externes sont enregistrées dans les signaux (donc
    annulables), les fichiers temporaires sont nettoyés, et l'écriture native
    transmet une progression en compteurs (paquets/octets) sans pourcentage
    artificiel.
    """
    work_root = config.work_dir or Path(tempfile.gettempdir())
    process_work_dir = prepare_process_work_dir(
        work_root,
        output_path=config.output,
        fallback_name="remux_job",
    )
    relocated_attachments = relocate_tmdb_covers_to_process_dir(
        [Path(path) for path in config.extra_attachments],
        work_root=work_root,
        process_dir=process_work_dir,
    )
    runtime_config = replace(config, extra_attachments=relocated_attachments)
    signals = TaskSignals()

    def task() -> None:
        canonical_root: Path | None = None
        partial = config.output.with_suffix(config.output.suffix + ".partial")

        def _check_cancel() -> None:
            if signals._cancel_event.is_set():
                raise TaskCancelledError()

        def _run_external(cmd: list[str], label: str) -> None:
            """Commande externe enregistrée dans TaskSignals, annulable proprement."""
            _check_cancel()
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                **subprocess_text_kwargs(),
            )
            signals._register_proc(proc)
            try:
                output, _ = proc.communicate()
            finally:
                signals._unregister_proc(proc)
            _check_cancel()
            if proc.returncode:
                raise RuntimeError(f"{label}: {str(output or '').strip()}")

        def _ensure_canonical_root() -> Path:
            nonlocal canonical_root
            if canonical_root is None:
                canonical_root = Path(tempfile.mkdtemp(
                    prefix="Muxiveo_native_", dir=process_work_dir,
                ))
            return canonical_root

        try:
            execution_plan = plan or plan_remux(
                runtime_config, ffmpeg_bin=ffmpeg_bin, ffprobe_bin=ffprobe_bin,
            )
            log("INFO", "Backend Matroska natif multi-pistes sélectionné (plan v1).")
            run_config = replace(runtime_config, sources=[
                replace(
                    source,
                    origin_identity=source.origin_identity or deterministic_source_identity(source.path),
                )
                for source in config.sources
            ])
            output_contract = execution_plan.output_contract
            if config.tmdb_cover is not None:
                cover_url, cover_name = config.tmdb_cover
                cover_name = normalized_tmdb_cover_filename(cover_name)
                try:
                    cover = download_tmdb_cover(cover_url, cover_name, _ensure_canonical_root() / "attachments")
                    run_config = replace(run_config, extra_attachments=[*run_config.extra_attachments, cover], tmdb_cover=None)
                except Exception as exc:
                    # Parité avec le backend FFmpeg : la cover est optionnelle —
                    # warning, poursuite sans cover et attente retirée du contrat
                    # (clé = nom canonique, identique à l'attente).
                    log("WARN", f"Impossible de télécharger la cover TMDB : {exc}")
                    run_config = replace(run_config, tmdb_cover=None)
                    output_contract = without_expected_attachment(
                        output_contract,
                        canonical_attachment_output_name(Path(cover_name)),
                    )
            _check_cancel()

            # Attachments de type attached_pic provenant d'un conteneur non-MKV :
            # le plan les matérialise en fichiers, puis l'assembleur les traite
            # comme attachments externes (aucun local_index canonicalisé).
            attachment_actions = [
                action for action in execution_plan.preparation_actions
                if action.kind == "extract_attachment"
            ]
            if attachment_actions:
                root = _ensure_canonical_root() / "attachments"
                root.mkdir(parents=True, exist_ok=True)
                extracted: list[Path] = []
                extracted_sources = {
                    action.source_file_index for action in attachment_actions
                    if action.source_file_index is not None
                }
                for action in attachment_actions:
                    target = root / action.target_name
                    command = list(action.command)
                    command[-1] = str(target)
                    _run_external(command, action.description)
                    extracted.append(target)
                run_config = replace(
                    run_config,
                    extra_attachments=[*run_config.extra_attachments, *extracted],
                    sources=[
                        replace(
                            source,
                            selected_attachments=[
                                attachment for attachment in source.selected_attachments
                                if not (
                                    source.file_index in extracted_sources
                                    and bool(getattr(attachment, "is_attached_pic", False))
                                )
                            ],
                        )
                        for source in run_config.sources
                    ],
                )
                _check_cancel()

            canonical_source_indexes = {
                action.source_file_index
                for action in execution_plan.preparation_actions
                if action.kind == "canonicalize_source" and action.source_file_index is not None
            }
            non_matroska = [
                source for source in run_config.sources
                if source.file_index in canonical_source_indexes
            ]
            if non_matroska:
                log_step(2, "Canonicalisation Matroska des sources non-MKV")
                root = _ensure_canonical_root()
                replacements: dict[int, Path] = {}
                for source in non_matroska:
                    index_map = execution_plan.canonical_index_map(source.file_index)
                    target = root / f"source_{source.file_index}.mkv"
                    cmd = build_canonicalization_command(
                        source, target, ffmpeg_bin,
                        selected_streams=sorted(index_map),
                    )
                    _run_external(cmd, f"Canonicalisation impossible pour {source.path.name}")
                    replacements[source.file_index] = target
                # Correspondance explicite index source original → index canonique
                new_sources: list[SourceInput] = []
                for source in run_config.sources:
                    if source.file_index not in replacements:
                        new_sources.append(source)
                        continue
                    index_map = execution_plan.canonical_index_map(source.file_index)
                    kept_tracks = [
                        replace(track, mkv_tid=index_map[track.mkv_tid])
                        for track in source.tracks
                        if track.mkv_tid in index_map
                    ]
                    new_sources.append(replace(
                        source,
                        path=replacements[source.file_index],
                        tracks=kept_tracks or source.tracks,
                    ))
                new_order: list[tuple[int, int] | tuple[int, int, str]] = []
                for order_item in run_config.track_order:
                    file_index, mkv_tid, entry_id = track_order_parts(order_item)
                    if file_index in replacements:
                        mkv_tid = execution_plan.canonical_index_map(file_index).get(mkv_tid, mkv_tid)
                    new_order.append((file_index, mkv_tid, entry_id) if entry_id else (file_index, mkv_tid))
                run_config = replace(run_config, sources=new_sources, track_order=new_order)
                _check_cancel()

            # Matérialisation des variantes audio planifiées (preview == exécution) ;
            # le writer natif garde la main sur le document multi-pistes final.
            if execution_plan.audio_variants:
                root = _ensure_canonical_root()
                source_by_index = {source.file_index: source for source in run_config.sources}
                next_source_index = max(source_by_index, default=-1) + 1
                new_sources = list(run_config.sources)
                variant_order: list[tuple[int, int] | tuple[int, int, str]] = []
                variants_by_index = {variant.order_index: variant for variant in execution_plan.audio_variants}
                for order_index, order_item in enumerate(run_config.track_order):
                    variant = variants_by_index.get(order_index)
                    if variant is None:
                        variant_order.append(order_item)
                        continue
                    file_index, mkv_tid, entry_id = track_order_parts(order_item)
                    source = source_by_index[file_index]
                    candidates = [track for track in source.tracks if track.mkv_tid == mkv_tid]
                    track = next(
                        (item for item in candidates if not entry_id or item.entry_id == entry_id),
                        None,
                    )
                    if track is None:
                        raise ValueError(
                            f"Piste de variante introuvable : source={file_index}, stream={mkv_tid}"
                        )
                    target_path = root / variant.target_name
                    cmd = build_audio_variant_command(
                        variant, source.path, mkv_tid, target_path, ffmpeg_bin,
                    )
                    _run_external(cmd, f"Variante audio {variant.target_codec} impossible")
                    materialized_track = replace(track, mkv_tid=0, orig_codec=track.codec, is_new=False)
                    new_source = SourceInput(
                        target_path, next_source_index, [materialized_track],
                        origin_identity=f"{source.origin_identity}:audio:{variant.stream_index}:{variant.target_codec}",
                    )
                    new_sources.append(new_source)
                    variant_order.append((next_source_index, 0, materialized_track.entry_id))
                    next_source_index += 1
                run_config = replace(run_config, sources=new_sources, track_order=variant_order)
                _check_cancel()

            log_step(3, "Construction du plan Matroska natif")
            plan_matroska = build_native_plan(
                run_config, output_contract=output_contract,
            )
            log_step(4, "Écriture Matroska native multi-pistes")

            def validate_partial(path: Path, packet_validation: MatroskaPacketValidation) -> None:
                errors = validate_matroska_output(
                    path, output_contract, packet_validation=packet_validation,
                )
                if errors:
                    raise RuntimeError(
                        "Validation sémantique de la sortie native échouée : "
                        + " ; ".join(errors)
                    )
                _run_external(
                    [
                        ffprobe_bin, "-v", "error", "-show_entries",
                        "format=format_name", "-of", "json", str(path),
                    ],
                    "Validation ffprobe de la sortie native impossible",
                )

            progress_state = {"packets": 0, "bytes": 0}

            def on_write_progress(progress: MatroskaWriteProgress) -> None:
                # Étape indéterminée avec compteurs : pas de pourcentage artificiel.
                if progress.stage != "clusters":
                    signals.progress.emit(
                        f"Écriture Matroska ({progress.stage}) : "
                        f"{progress.packets_written} paquets, "
                        f"{progress.bytes_written / (1024 * 1024):.1f} Mio"
                    )
                    return
                if (
                    progress.packets_written - progress_state["packets"] >= 2000
                    or progress.bytes_written - progress_state["bytes"] >= 64 * 1024 * 1024
                ):
                    progress_state["packets"] = progress.packets_written
                    progress_state["bytes"] = progress.bytes_written
                    signals.progress.emit(
                        f"Écriture Matroska : {progress.packets_written} paquets, "
                        f"{progress.bytes_written / (1024 * 1024):.1f} Mio"
                    )

            MatroskaWriter().write(
                plan_matroska,
                external_validator=validate_partial,
                cancel_cb=signals._cancel_event.is_set,
                progress_cb=on_write_progress,
            )
            log_step(5, "Validation structure native terminée")
            # Un échec NFO après commit ne transforme plus un média valide en
            # workflow échoué.
            try:
                finalize(runtime_config.output)
            except Exception as exc:
                log("WARN", f"Post-traitement (NFO) échoué après commit : {exc}")
            signals.finished.emit(str(runtime_config.output))
        except (TaskCancelledError, MatroskaWriteCancelled):
            partial.unlink(missing_ok=True)
            signals.cancelled.emit()
        except Exception as exc:
            partial.unlink(missing_ok=True)
            signals.failed.emit(str(exc), exc)
        finally:
            if canonical_root is not None:
                shutil.rmtree(canonical_root, ignore_errors=True)
            remove_path(process_work_dir)

    executor = ThreadPoolExecutor(max_workers=1)
    executor.submit(task)
    executor.shutdown(wait=False)
    return signals


__all__ = [
    "FfmpegRemuxBackend", "MuxBackendDecision", "MuxExecutionPlan",
    "NativeMatroskaBackend",
    "RemuxBackend", "build_native_plan", "mux_backend_report",
    "build_canonicalization_command",
    "MATROSKA_EXTENSIONS",
    "native_capability_reasons", "native_preparation_commands",
    "plan_remux",
    "run_native_remux", "select_mux_backend",
]
