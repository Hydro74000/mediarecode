"""Validation commune du remux, partagée par les backends natif et FFmpeg."""

from __future__ import annotations

import shutil as _shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Callable

from core.bluray import validate_bluray_source
from core.workflows.common.metadata import STREAM_SPEC_BY_TRACK_TYPE
from core.workflows.remux_models import RemuxConfig, TrackEntry
from core.workflows.remux_plan import MuxExecutionPlan, select_mux_backend


TrackOrderItem = tuple[int, int] | tuple[int, int, str]
TrackOrderParts = Callable[[TrackOrderItem], tuple[int, int, str | None]]
DirWritable = Callable[[Path], bool]


def _same_filesystem_target(source: Path, output: Path) -> bool:
    """Détecte source == sortie, y compris liens symboliques et hardlinks."""
    if source == output:
        return True
    try:
        if source.resolve() == output.resolve():
            return True
    except OSError:
        pass
    try:
        return source.exists() and output.exists() and source.samefile(output)
    except OSError:
        return False


def _tool_available(binary: str) -> bool:
    candidate = Path(binary)
    if candidate.is_file():
        return True
    return _shutil.which(binary) is not None


def validate_remux_config(
    config: RemuxConfig,
    *,
    track_order_parts: TrackOrderParts,
    dir_writable: DirWritable,
    plan: MuxExecutionPlan | None = None,
    tool_paths: Mapping[str, str] | None = None,
) -> list[str]:
    """Validation obligatoire avant tout dispatch de backend.

    ``plan`` permet de consommer le :class:`MuxExecutionPlan` déjà compilé
    (mêmes diagnostics que la preview et l'exécution). ``tool_paths`` mappe
    les rôles d'outils du plan vers les binaires configurés.
    """
    errors: list[str] = []

    if plan is not None:
        requested = plan.requested_backend
        native_reasons: tuple[str, ...] = plan.native_diagnostics
        errors.extend(plan.mapping_errors)
    else:
        try:
            decision = select_mux_backend(config)
        except ValueError as exc:
            return [str(exc)]
        requested = decision.requested
        native_reasons = decision.native_reasons
    if requested == "native" and native_reasons:
        errors.extend(f"Backend natif indisponible : {reason}." for reason in native_reasons)

    if not config.sources:
        errors.append("Aucun fichier source.")
        return errors

    if config.output.suffix.lower() != ".mkv":
        errors.append("La sortie remux doit être un fichier .mkv.")

    file_indexes = [src.file_index for src in config.sources]
    if len(set(file_indexes)) != len(file_indexes):
        errors.append("Indices de source dupliqués dans la configuration (file_index).")

    for src in config.sources:
        if not src.path.is_file():
            errors.append(f"Fichier source introuvable : {src.path}")
        else:
            try:
                with src.path.open("rb"):
                    pass
            except OSError as exc:
                errors.append(f"Fichier source illisible : {src.path} ({exc})")
        errors.extend(validate_bluray_source(src.path))
        if _same_filesystem_target(src.path, config.output):
            errors.append(f"Le fichier de sortie doit être différent de la source : {src.path.name}")

        seen_attachment_indexes: set[int] = set()
        seen_attachment_local_indexes: set[int] = set()
        for att in src.selected_attachments:
            if att.index < 0:
                errors.append(
                    "Pièce jointe source invalide : "
                    f"index négatif ({att.index}) dans {src.path.name}"
                )
            if att.local_index < 0:
                errors.append(
                    "Pièce jointe source invalide : "
                    f"local_index négatif ({att.local_index}) dans {src.path.name}"
                )
            if att.index in seen_attachment_indexes:
                errors.append(
                    "Pièce jointe source dupliquée : "
                    f"stream {att.index} dans {src.path.name}"
                )
            if att.local_index in seen_attachment_local_indexes:
                errors.append(
                    "Pièce jointe source dupliquée : "
                    f"local_index {att.local_index} dans {src.path.name}"
                )
            seen_attachment_indexes.add(att.index)
            seen_attachment_local_indexes.add(att.local_index)

    output_dir = config.output.parent
    if not output_dir.exists():
        if not bool(getattr(config, "allow_missing_output_dir", False)):
            errors.append(f"Dossier de sortie inexistant : {output_dir}")
    elif not dir_writable(output_dir):
        errors.append(
            "Dossier de sortie non inscriptible : "
            f"{output_dir} (vérifiez les protections Windows sur les dossiers Bibliothèques)."
        )

    if not config.track_order:
        errors.append("Aucune piste sélectionnée.")

    track_map_by_id = {
        (src.file_index, t.entry_id): t
        for src in config.sources
        for t in src.tracks
    }
    track_map_by_pair: dict[tuple[int, int], list[TrackEntry]] = {}
    for src in config.sources:
        for source_track in src.tracks:
            track_map_by_pair.setdefault((src.file_index, source_track.mkv_tid), []).append(source_track)
    valid_file_indexes = {src.file_index for src in config.sources}

    for order_item in config.track_order:
        file_index, mkv_tid, entry_id = track_order_parts(order_item)
        if file_index not in valid_file_indexes:
            errors.append(f"track_order référence une source inconnue : file_index={file_index}")
            continue
        selected_track: TrackEntry | None = (
            track_map_by_id.get((file_index, entry_id))
            if entry_id
            else next(iter(track_map_by_pair.get((file_index, mkv_tid), [])), None)
        )
        if selected_track is None:
            errors.append(
                "track_order référence une piste introuvable : "
                f"file_index={file_index}, stream={mkv_tid}"
            )
            continue
        if selected_track.track_type not in STREAM_SPEC_BY_TRACK_TYPE:
            errors.append(
                "Type de piste non supporté par le remux : "
                f"{selected_track.track_type} (file_index={file_index}, stream={mkv_tid})"
            )
            continue
        if selected_track.track_type == "video" and int(selected_track.time_shift_ms) < 0:
            errors.append(
                "Décalage vidéo négatif interdit : "
                f"file_index={file_index}, stream={mkv_tid}, offset={selected_track.time_shift_ms} ms"
            )

    for extra in config.extra_attachments:
        if not extra.is_file():
            errors.append(f"Pièce jointe manuelle introuvable : {extra}")

    if config.chapter_overrides is not None:
        for idx, chapter in enumerate(config.chapter_overrides):
            try:
                tc = float(getattr(chapter, "timecode_s", 0.0))
            except (TypeError, ValueError):
                errors.append(f"Chapitre #{idx + 1} invalide : timecode non numérique.")
                continue
            if tc < 0:
                errors.append(f"Chapitre #{idx + 1} invalide : timecode négatif ({tc}).")

    if plan is not None:
        resolved_tools = dict(tool_paths or {})
        for tool_role in plan.required_tools:
            binary = resolved_tools.get(tool_role) or tool_role
            if not _tool_available(binary):
                errors.append(f"Outil requis indisponible : {tool_role} ({binary})")

    return errors
