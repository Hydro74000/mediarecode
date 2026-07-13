"""Backend selection for Matroska remuxing.

The public contract deliberately lives here instead of in the FFmpeg runner:
an exact-job can request a backend without coupling its JSON shape to an
implementation.  The native backend is capability-gated; ``auto`` remains
backwards compatible by selecting FFmpeg when a plan needs a feature that has
not yet been materialised by the native writer.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import mimetypes
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from core.runner import TaskSignals
from core.workflows.matroska_mux_plan import (
    MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack, deterministic_uid,
)
from core.workflows.matroska_element_ids import CHAPTERS_ID, TAGS_ID
from core.workflows.matroska_language_editor import matroska_legacy_language
from core.workflows.matroska_reader import MatroskaAttachment, MatroskaReader
from core.workflows.matroska_writer import MatroskaWriter, build_attachments_element, build_chapters_element, build_tags_element
from core.workflows.remux_mapping import resolve_mapped_tracks
from core.workflows.remux_mapping import normalized_language_value, resolved_global_tags
from core.workflows.remux_models import RemuxConfig, normalize_mux_backend
from core.version import APP_VERSION_LABEL
from core.workdir import download_tmdb_cover


@dataclass(frozen=True)
class MuxBackendDecision:
    requested: str
    selected: str
    native_reasons: tuple[str, ...] = ()

    @property
    def uses_fallback(self) -> bool:
        return self.requested == "auto" and self.selected == "ffmpeg"


def native_capability_reasons(config: RemuxConfig) -> tuple[str, ...]:
    """Return blockers without silently weakening a native exact-job.

    Keeping this check central means writer increments only remove blockers;
    they never silently change v1 semantics.
    """
    reasons: list[str] = []
    if config.output.suffix.lower() != ".mkv":
        reasons.append("le backend natif écrit uniquement des sorties .mkv")
    return tuple(reasons)


def select_mux_backend(config: RemuxConfig) -> MuxBackendDecision:
    requested = normalize_mux_backend(config.mux_backend)
    reasons = native_capability_reasons(config)
    if requested == "ffmpeg":
        return MuxBackendDecision(requested=requested, selected="ffmpeg")
    if not reasons:
        return MuxBackendDecision(requested=requested, selected="native")
    if requested == "native":
        return MuxBackendDecision(requested=requested, selected="native", native_reasons=reasons)
    return MuxBackendDecision(requested=requested, selected="ffmpeg", native_reasons=reasons)


def build_native_plan(config: RemuxConfig) -> MatroskaMuxPlan:
    mapped = resolve_mapped_tracks(config)
    readers = {source.file_index: MatroskaReader(source.path) for source in config.sources}
    native_tracks = {index: reader.tracks() for index, reader in readers.items()}
    output_tracks: list[MatroskaMuxTrack] = []
    output_packets: list[MatroskaMuxPacket] = []
    block_cache = {index: list(reader.blocks()) for index, reader in readers.items()}
    for output_index, item in enumerate(mapped, start=1):
        tracks = native_tracks[item.source_file_index]
        if not 0 <= item.stream_index < len(tracks):
            raise ValueError(
                f"Piste native introuvable : source={item.source_file_index}, stream={item.stream_index}"
            )
        source_track = tracks[item.stream_index]
        uid = deterministic_uid(item.source_path, source_track.uid, output_index, item.track.entry_id)
        output_tracks.append(MatroskaMuxTrack(
            source=Path(item.source_path), source_track=source_track,
            output_number=output_index, output_uid=uid,
            language=matroska_legacy_language(normalized_language_value(item.track)),
            language_bcp47=(
                source_track.language_bcp47
                if (item.track.language or "und").lower() == (source_track.language_bcp47 or "").lower()
                else ""
            ),
            name=item.track.title,
            flag_enabled=item.track.flag_enabled,
            flag_default=item.track.flag_default,
            flag_forced=item.track.flag_forced,
            flag_hearing_impaired=item.track.flag_hearing_impaired,
            flag_visual_impaired=item.track.flag_visual_impaired,
            flag_original=item.track.flag_original,
            flag_commentary=item.track.flag_commentary,
        ))
        offset = int(item.track.time_shift_ms or 0)
        for block in block_cache[item.source_file_index]:
            if block.track_number == source_track.number:
                shifted = block.__class__(**{**block.__dict__, "timestamp_ms": block.timestamp_ms + offset})
                output_packets.append(MatroskaMuxPacket(output_index, shifted))
    duration = max((packet.block.timestamp_ms + (packet.block.duration_ms or 0) for packet in output_packets), default=0)
    opaque: list[bytes] = []

    attachments: list[MatroskaAttachment] = []
    source_by_index = {source.file_index: source for source in config.sources}
    for source_index, source in source_by_index.items():
        available = readers[source_index].attachments()
        for selected in source.selected_attachments:
            if not 0 <= selected.local_index < len(available):
                raise ValueError(f"Attachment natif introuvable : {source.path} #{selected.local_index}")
            item = available[selected.local_index]
            attachments.append(MatroskaAttachment(
                uid=deterministic_uid(source.path, item.uid, item.name), name=item.name,
                media_type=item.media_type, description=item.description, data=item.data,
            ))
    for path in config.extra_attachments:
        attachment_path = Path(path)
        attachments.append(MatroskaAttachment(
            uid=deterministic_uid(attachment_path, attachment_path.stat().st_size),
            name=attachment_path.name,
            media_type=mimetypes.guess_type(attachment_path.name)[0] or "application/octet-stream",
            description="", data=attachment_path.read_bytes(),
        ))
    attachment_element = build_attachments_element(attachments)
    if attachment_element:
        opaque.append(attachment_element)

    if config.chapter_overrides is not None:
        chapter_element = build_chapters_element(list(config.chapter_overrides))
        if chapter_element:
            opaque.append(chapter_element)
    elif config.keep_chapters:
        chapter_source = config.chapter_source_index
        if chapter_source is None or chapter_source not in readers:
            chapter_source = next((source.file_index for source in config.sources if source.has_chapters), config.sources[0].file_index)
        opaque.extend(readers[chapter_source].raw_top_level(CHAPTERS_ID))

    if config.tag_overrides is not None or config.file_title:
        tag_element = build_tags_element(resolved_global_tags(config))
        if tag_element:
            opaque.append(tag_element)
    else:
        for source in config.sources:
            if source.copy_tags:
                opaque.extend(readers[source.file_index].raw_top_level(TAGS_ID))
    first_reader = readers[config.sources[0].file_index]
    _, source_writing_app = first_reader.segment_info_apps()
    return MatroskaMuxPlan(
        config.output, tuple(output_tracks), tuple(output_packets), duration_ms=duration,
        muxing_app=f"Muxiveo {APP_VERSION_LABEL.removeprefix('v')}",
        writing_app=source_writing_app or "Muxiveo",
        opaque_top_level=tuple(opaque),
    )


def run_native_remux(
    config: RemuxConfig,
    *,
    log: Callable[[str, str], None],
    log_step: Callable[[int, str], None],
    ffmpeg_bin: str,
) -> TaskSignals:
    signals = TaskSignals()

    def task() -> None:
        canonical_root: Path | None = None
        try:
            log("INFO", "Backend Matroska natif multi-pistes sélectionné (plan v1).")
            run_config = config
            if config.tmdb_cover is not None:
                work_root = config.work_dir or Path(tempfile.gettempdir())
                canonical_root = Path(tempfile.mkdtemp(prefix="Muxiveo_native_", dir=work_root))
                cover_url, cover_name = config.tmdb_cover
                cover = download_tmdb_cover(cover_url, cover_name, canonical_root / "attachments")
                run_config = replace(run_config, extra_attachments=[*run_config.extra_attachments, cover], tmdb_cover=None)
            non_matroska = [source for source in config.sources if source.path.suffix.lower() not in {".mkv", ".webm"}]
            if non_matroska:
                log_step(2, "Canonicalisation Matroska des sources non-MKV")
                work_root = config.work_dir or Path(tempfile.gettempdir())
                work_root.mkdir(parents=True, exist_ok=True)
                if canonical_root is None:
                    canonical_root = Path(tempfile.mkdtemp(prefix="Muxiveo_canonical_", dir=work_root))
                replacements: dict[int, Path] = {}
                for source in non_matroska:
                    target = canonical_root / f"source_{source.file_index}.mkv"
                    cmd = [
                        ffmpeg_bin, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
                        "-i", str(source.path), "-map", "0", "-c", "copy", "-c:s", "srt", str(target),
                    ]
                    result = subprocess.run(cmd, capture_output=True, text=True)
                    if result.returncode:
                        raise RuntimeError(
                            f"Canonicalisation impossible pour {source.path.name}: {result.stderr or result.stdout}"
                        )
                    replacements[source.file_index] = target
                run_config = replace(config, sources=[
                    replace(source, path=replacements.get(source.file_index, source.path))
                    for source in config.sources
                ])
            log_step(3, "Construction du plan Matroska natif")
            plan = build_native_plan(run_config)
            log_step(4, "Écriture Matroska native multi-pistes")
            MatroskaWriter().write(plan)
            log_step(5, "Validation structure native terminée")
            MatroskaReader(config.output).segment()
            signals.finished.emit(str(config.output))
        except Exception as exc:
            config.output.with_suffix(config.output.suffix + ".partial").unlink(missing_ok=True)
            signals.failed.emit(str(exc), exc)
        finally:
            if canonical_root is not None:
                shutil.rmtree(canonical_root, ignore_errors=True)

    executor = ThreadPoolExecutor(max_workers=1)
    executor.submit(task)
    executor.shutdown(wait=False)
    return signals


__all__ = ["MuxBackendDecision", "build_native_plan", "native_capability_reasons", "run_native_remux", "select_mux_backend"]
