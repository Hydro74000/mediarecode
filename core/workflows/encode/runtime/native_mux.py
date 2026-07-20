"""Assemblage final Matroska natif des workflows encode (lot 2).

Les services de préparation matérialisent les pistes vidéo (artefacts MKV
NVEncC/multi-vidéo/encode découpé) et audio (encodage isolé) ; ce module
compile ensuite le contrat partagé :class:`MatroskaAssemblyPlan` et écrit la
sortie via le writer natif — commit atomique, validation sémantique interne
puis ffprobe en second validateur, sans aucun post-patch conteneur.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from core.runner import TaskCancelledError, TaskSignals
from core.workdir import remove_path
from core.workflows.encode.domain.codecs import audio_codec_args
from core.workflows.encode.models import EncodeConfig, EncodeError
from core.matroska.assembly import (
    MatroskaAssemblyPlan,
    MatroskaAssemblyTrack,
    MatroskaTrackFlags,
    assembly_output_contract,
    compile_assembly_plan,
)
from core.matroska.mux_plan import deterministic_source_identity
from core.matroska.validation import validate_matroska_output
from core.matroska.reader import MatroskaReader
from core.matroska.writer import (
    MatroskaWriteCancelled,
    MatroskaWriteProgress,
    MatroskaWriter,
)
from core.workflows.encode.planning.sources import resolve_source_layout
from core.workflows.encode.planning.plan_models import PlannedTrackMetadata
from core.workflows.encode.planning.track_metadata import resolve_track_metadata
from core.workflows.remux_plan import MATROSKA_EXTENSIONS


#: Type Matroska « subtitle » dans les TrackEntry natifs.
_SUBTITLE_TRACK_TYPE = 17


@dataclass(frozen=True)
class NativeVideoArtifactRef:
    """Artefact vidéo Matroska prêt pour l'assemblage final."""

    path: Path
    track_index: int = 0
    offset_ms: int = 0


def _matroska_track_index_for_stream(artifact: Path, stream_index: int) -> int:
    """Index positionnel de piste pour un index de stream ffprobe (MKV)."""
    return int(stream_index)


def resolve_native_subtitle_tracks(config: EncodeConfig) -> list[tuple[Path, int]]:
    """Sous-titres à copier, résolus sur TOUTES les sources du layout.

    Miroir natif de la résolution du plan (``copy_subtitles`` sans sélection
    explicite) : chaque source Matroska du layout est inspectée. Une source
    non Matroska lève — le sélecteur de backend doit avoir bloqué en amont ;
    échouer bruyamment vaut mieux qu'une perte silencieuse de pistes.
    """
    if config.subtitle_tracks:
        return [(Path(path), int(index)) for path, index in config.subtitle_tracks]
    if not config.copy_subtitles:
        return []
    resolved: list[tuple[Path, int]] = []
    for source in resolve_source_layout(config).sources:
        source = Path(source)
        if not source.is_file():
            continue
        if source.suffix.lower() not in MATROSKA_EXTENSIONS:
            raise EncodeError(
                f"Assemblage natif : sous-titres à copier depuis une source non "
                f"Matroska ({source.name}) — chemin FFmpeg requis."
            )
        for position, track in enumerate(MatroskaReader(source).tracks()):
            if track.track_type == _SUBTITLE_TRACK_TYPE:
                resolved.append((source, position))
    return resolved


def build_encode_assembly_plan(
    config: EncodeConfig,
    *,
    video_artifacts: list[NativeVideoArtifactRef],
    materialized_audio: dict[int, Path],
    resolved_subtitles: list[tuple[Path, int]] | None = None,
    track_metadata: tuple[PlannedTrackMetadata, ...] | None = None,
) -> MatroskaAssemblyPlan:
    """Compile la configuration encode vers le contrat d'assemblage partagé.

    ``materialized_audio`` mappe l'index de la piste audio de la config vers
    son artefact MKV mono-piste (pistes réencodées) ; les pistes ``copy``
    restent référencées directement dans leur source Matroska.
    ``resolved_subtitles`` transporte la résolution du plan d'encodage
    (toutes sources) ; à ``None``, la résolution native équivalente
    (:func:`resolve_native_subtitle_tracks`) est appliquée.
    """
    identities: dict[Path, str] = {}

    def _identity(path: Path) -> str:
        return identities.setdefault(path, deterministic_source_identity(path))

    ordered: list[MatroskaAssemblyTrack] = []
    for ref in video_artifacts:
        ordered.append(MatroskaAssemblyTrack(
            artifact=ref.path,
            artifact_track_index=ref.track_index,
            source_identity=_identity(ref.path),
            time_shift_ms=int(ref.offset_ms or 0),
        ))

    for audio_index, audio in enumerate(config.audio_tracks):
        source = Path(audio.source_path or config.source)
        artifact = materialized_audio.get(audio_index)
        if artifact is not None:
            ordered.append(MatroskaAssemblyTrack(
                artifact=artifact,
                artifact_track_index=0,
                source_identity=_identity(source),
                provenance=f"audio:{audio.stream_index}:{audio.codec}",
            ))
        else:
            ordered.append(MatroskaAssemblyTrack(
                artifact=source,
                artifact_track_index=_matroska_track_index_for_stream(source, audio.stream_index),
                source_identity=_identity(source),
            ))

    subtitles: list[tuple[Path, int]] = (
        [(Path(path), int(index)) for path, index in resolved_subtitles]
        if resolved_subtitles is not None
        else resolve_native_subtitle_tracks(config)
    )
    for subtitle_path, subtitle_index in subtitles:
        ordered.append(MatroskaAssemblyTrack(
            artifact=subtitle_path,
            artifact_track_index=_matroska_track_index_for_stream(subtitle_path, subtitle_index),
            source_identity=_identity(subtitle_path),
        ))

    if track_metadata is None:
        videos = list(config.video_tracks or ([config.video] if config.video is not None else []))
        track_metadata = resolve_track_metadata(
            config,
            video_refs=(
                (
                    Path(getattr(video, "source_path", None) or config.source),
                    int(getattr(video, "stream_index", 0) or 0),
                )
                for video in videos
            ),
            subtitle_refs=subtitles,
        )

    # Force les valeurs de la source logique sur les artefacts préparés : un
    # HEVC brut remballé ou un audio réencodé ne doit pas devenir la nouvelle
    # source de vérité de la langue, du titre ou des dispositions.
    for position, metadata in enumerate(track_metadata):
        if not 0 <= position < len(ordered):
            continue
        planned_flags = metadata.flags
        flags = None
        if planned_flags is not None and all(
            value is not None
            for value in (
                planned_flags.enabled,
                planned_flags.default,
                planned_flags.forced,
                planned_flags.hearing_impaired,
                planned_flags.visual_impaired,
                planned_flags.original,
                planned_flags.commentary,
            )
        ):
            flags = MatroskaTrackFlags(
                enabled=bool(planned_flags.enabled),
                default=bool(planned_flags.default),
                forced=bool(planned_flags.forced),
                hearing_impaired=bool(planned_flags.hearing_impaired),
                visual_impaired=bool(planned_flags.visual_impaired),
                original=bool(planned_flags.original),
                commentary=bool(planned_flags.commentary),
            )
        ordered[position] = replace(
            ordered[position],
            language_value=metadata.language,
            name=metadata.name,
            flags=flags,
        )

    chapter_entries = tuple(config.chapter_overrides) if config.chapter_overrides is not None else None
    chapter_source: Path | None = None
    if (
        chapter_entries is None
        and config.keep_chapters
        and Path(config.source).suffix.lower() in MATROSKA_EXTENSIONS
    ):
        chapter_source = Path(config.source)

    segment_info_source = (
        Path(config.source)
        if Path(config.source).suffix.lower() in MATROSKA_EXTENSIONS and Path(config.source).is_file()
        else (video_artifacts[0].path if video_artifacts else None)
    )

    return MatroskaAssemblyPlan(
        output=config.output,
        ordered_tracks=tuple(ordered),
        extra_attachment_files=tuple(Path(path) for path in config.extra_attachments),
        chapter_entries=chapter_entries,
        chapter_source=chapter_source,
        tag_overrides=dict(config.tag_overrides) if config.tag_overrides is not None else None,
        segment_title=(
            config.file_title
            if config.file_title.strip() or config.tag_overrides is not None
            else None
        ),
        title_tag_value=config.file_title,
        segment_info_source=segment_info_source,
    )


def materialize_audio_artifacts(
    config: EncodeConfig,
    *,
    work_dir: Path,
    ffmpeg_bin: str,
    run_cmd: Callable[[list[str], str], str],
    cleanup_paths: list[Path] | None = None,
) -> dict[int, Path]:
    """Matérialise en MKV mono-piste les pistes audio réencodées.

    Les pistes ``copy`` sans BSF restent copiées directement depuis leur
    source Matroska par l'assembleur (aucun artefact nécessaire).
    """
    artifacts: dict[int, Path] = {}
    for audio_index, audio in enumerate(config.audio_tracks):
        codec = str(audio.codec or "copy").strip().lower()
        if codec == "copy" and not audio.extract_truehd_core:
            continue
        source = Path(audio.source_path or config.source)
        target = work_dir / f"native_audio_{audio_index}.mkv"
        if cleanup_paths is not None:
            cleanup_paths.append(target)
        command = [
            ffmpeg_bin, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-map", f"0:{int(audio.stream_index)}",
        ]
        command.extend(audio_codec_args(0, audio))
        command.append(str(target))
        run_cmd(command, f"ffmpeg-native-audio-{audio_index}")
        artifacts[audio_index] = target
    return artifacts


def assemble_encode_output_native(
    config: EncodeConfig,
    *,
    video_artifacts: list[NativeVideoArtifactRef],
    work_dir: Path,
    signals: TaskSignals | None,
    run_cmd: Callable[[list[str], str], str],
    log: Callable[[str, str], None],
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    resolved_subtitles: list[tuple[Path, int]] | None = None,
    track_metadata: tuple[PlannedTrackMetadata, ...] | None = None,
) -> Path:
    """Assemblage final natif : matérialisation audio, contrat, écriture atomique.

    Aucun post-patch MuxingApp/langue/Dolby Vision : la signalisation est
    écrite dans les ``TrackEntry`` lors de l'assemblage.
    ``resolved_subtitles`` : résolution des sous-titres issue du plan
    d'encodage (toutes sources) ; à ``None``, résolution native équivalente.
    """
    if not video_artifacts:
        raise EncodeError("Assemblage natif sans artefact vidéo.")
    log("INFO", "Assemblage final Matroska natif (plan partagé remux/encode).")
    audio_cleanup_paths: list[Path] = []
    try:
        materialized_audio = materialize_audio_artifacts(
            config,
            work_dir=work_dir,
            ffmpeg_bin=ffmpeg_bin,
            run_cmd=run_cmd,
            cleanup_paths=audio_cleanup_paths,
        ) if config.audio_tracks else {}
        assembly = build_encode_assembly_plan(
            config,
            video_artifacts=video_artifacts,
            materialized_audio=materialized_audio,
            resolved_subtitles=resolved_subtitles,
            track_metadata=track_metadata,
        )
        dovi_video_indexes = {
            index
            for index, video in enumerate(config.video_tracks)
            if bool(getattr(video, "copy_dv", False))
        }
        contract = assembly_output_contract(
            assembly,
            require_block_addition_mapping=dovi_video_indexes,
        )
        assembly = replace(assembly, expected_output_contract=contract)
        mux_plan = compile_assembly_plan(assembly)

        def _validate(path: Path) -> None:
            errors = validate_matroska_output(path, contract)
            if errors:
                raise EncodeError(
                    "Validation sémantique de la sortie native échouée : "
                    + " ; ".join(errors)
                )
            run_cmd(
                [
                    ffprobe_bin, "-v", "error", "-show_entries",
                    "format=format_name", "-of", "json", str(path),
                ],
                "ffprobe-native-validation",
            )

        progress_state = {"packets": 0, "bytes": 0}

        def _on_progress(progress: MatroskaWriteProgress) -> None:
            if signals is None:
                return
            if progress.stage != "clusters":
                signals.progress.emit(
                    f"Assemblage Matroska ({progress.stage}) : "
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
                    f"Assemblage Matroska : {progress.packets_written} paquets, "
                    f"{progress.bytes_written / (1024 * 1024):.1f} Mio"
                )

        try:
            MatroskaWriter().write(
                mux_plan,
                external_validator=_validate,
                cancel_cb=(signals._cancel_event.is_set if signals is not None else None),
                progress_cb=_on_progress,
            )
        except MatroskaWriteCancelled as exc:
            # Annulation coopérative : convertie vers le contrat des runners
            # (qui n'interceptent que TaskCancelledError).
            raise TaskCancelledError() from exc
        log("INFO", "Assemblage Matroska natif terminé (aucun post-patch conteneur).")
        return config.output
    finally:
        for path in audio_cleanup_paths:
            remove_path(path)


__all__ = [
    "NativeVideoArtifactRef",
    "assemble_encode_output_native",
    "build_encode_assembly_plan",
    "materialize_audio_artifacts",
    "resolve_native_subtitle_tracks",
]
