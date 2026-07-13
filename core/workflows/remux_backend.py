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
import re
from math import gcd
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Protocol

from core.runner import TaskSignals
from core.bluray import append_ffmpeg_input_args
from core.subtitle_codec import plan_subtitle_codec
from core.workflows.matroska_mux_plan import (
    MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack,
    deterministic_source_identity, deterministic_uid,
)
from core.workflows.matroska_element_ids import CHAPTERS_ID, TAGS_ID
from core.workflows.matroska_language_editor import matroska_legacy_language
from core.workflows.matroska_reader import MatroskaAttachment, MatroskaReader
from core.workflows.matroska_writer import (
    MatroskaWriter, build_attachments_element, build_chapters_element,
    build_tags_element, rewrite_tag_target_uids,
)
from core.workflows.remux_mapping import resolve_mapped_tracks
from core.workflows.remux_mapping import normalized_language_value, resolved_global_tags
from core.workflows.remux_models import RemuxConfig, SourceInput, normalize_mux_backend
from core.workflows.common.sync_rewrite import (
    audio_bitrate_kbps_from_display_info,
    normalized_rewrite_codec,
)
from core.subprocess_utils import subprocess_windows_no_window_kwargs
from core.version import APP_VERSION_LABEL
from core.workdir import download_tmdb_cover


MATROSKA_EXTENSIONS = frozenset({".mkv", ".webm", ".mka", ".mks", ".mk3d"})


@dataclass(frozen=True)
class MuxBackendDecision:
    requested: str
    selected: str
    native_reasons: tuple[str, ...] = ()

    @property
    def uses_fallback(self) -> bool:
        return self.requested == "auto" and self.selected == "ffmpeg"


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
            finalize=self.finalize,
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


def native_capability_reasons(config: RemuxConfig) -> tuple[str, ...]:
    """Return blockers without silently weakening a native exact-job.

    Keeping this check central means writer increments only remove blockers;
    they never silently change v1 semantics.
    """
    reasons: list[str] = []
    if config.output.suffix.lower() != ".mkv":
        reasons.append("le backend natif écrit uniquement des sorties .mkv")
    for source in config.sources:
        for track in source.tracks:
            if track.track_type == "subtitle":
                try:
                    plan_subtitle_codec(track.codec)
                except ValueError as exc:
                    reasons.append(f"{source.path.name}: {exc}")
            if track.sync_rewrite_label and track.time_shift_ms:
                reasons.append(
                    f"{source.path.name}: réécriture de synchronisation avancée à matérialiser par FFmpeg"
                )
        if source.path.suffix.lower() not in MATROSKA_EXTENSIONS:
            continue
        if not source.path.is_file():
            continue
        try:
            reader = MatroskaReader(source.path)
            reader.segment()
            if not reader.tracks():
                reasons.append(f"{source.path.name}: aucune piste Matroska lisible")
                continue
            _compression, encryption = reader.content_encoding_capabilities()
            if encryption:
                reasons.append(f"{source.path.name}: piste Matroska chiffrée non transposable")
        except (OSError, ValueError) as exc:
            reasons.append(f"{source.path.name}: structure Matroska illisible ({exc})")
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


def native_preparation_commands(config: RemuxConfig, ffmpeg_bin: str) -> list[list[str]]:
    commands: list[list[str]] = []
    for source in config.sources:
        if source.path.suffix.lower() in MATROSKA_EXTENSIONS:
            continue
        commands.append(build_canonicalization_command(
            source, Path(f"<temporary>/source_{source.file_index}.mkv"), ffmpeg_bin,
        ))
    return commands


def build_canonicalization_command(source: SourceInput, target: Path, ffmpeg_bin: str) -> list[str]:
    command = [ffmpeg_bin, "-y", "-nostdin", "-hide_banner", "-loglevel", "error"]
    raw_video_suffixes = {
        ".264", ".avc", ".h264", ".x264", ".265", ".hevc", ".h265", ".x265",
        ".av1", ".obu", ".ivf", ".vc1", ".m1v", ".m2v", ".mpv",
    }
    if source.path.suffix.lower() in raw_video_suffixes:
        display = next((track.display_info for track in source.tracks if track.track_type == "video"), "")
        fps_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:fps|FPS)", display)
        if fps_match:
            command.extend(["-r", fps_match.group(1).replace(",", ".")])
    append_ffmpeg_input_args(command, source.path)
    command.extend(["-map", "0", "-c", "copy"])
    subtitle_index = 0
    for track in source.tracks:
        if track.track_type != "subtitle":
            continue
        codec_arg, _warning = plan_subtitle_codec(track.codec)
        if codec_arg != "copy":
            command.extend([f"-c:s:{subtitle_index}", codec_arg])
        subtitle_index += 1
    command.append(str(target))
    return command


def mux_backend_report(config: RemuxConfig, *, ffmpeg_bin: str = "ffmpeg") -> dict[str, object]:
    decision = select_mux_backend(config)
    return {
        "requested_backend": decision.requested,
        "selected_backend": decision.selected,
        "plan_version": 1,
        "fallback": decision.uses_fallback,
        "fallback_reason": "; ".join(decision.native_reasons) if decision.uses_fallback else "",
        "native_diagnostics": list(decision.native_reasons),
        "preparation_commands": native_preparation_commands(config, ffmpeg_bin) if decision.selected == "native" else [],
    }


def build_native_plan(config: RemuxConfig) -> MatroskaMuxPlan:
    mapped = resolve_mapped_tracks(config)
    readers = {source.file_index: MatroskaReader(source.path) for source in config.sources}
    native_tracks = {index: reader.tracks() for index, reader in readers.items()}
    source_identities = {
        source.file_index: source.origin_identity or deterministic_source_identity(source.path)
        for source in config.sources
    }
    timestamp_scale_ns = 0
    for reader in readers.values():
        timestamp_scale_ns = gcd(timestamp_scale_ns, reader.timestamp_scale_ns())
    for item in mapped:
        timestamp_scale_ns = gcd(timestamp_scale_ns, abs(int(item.track.time_shift_ms or 0)) * 1_000_000)
    timestamp_scale_ns = timestamp_scale_ns or 1_000_000
    output_tracks: list[MatroskaMuxTrack] = []
    output_packets: list[MatroskaMuxPacket] = []
    track_uid_maps: dict[int, dict[int, int]] = {}
    block_cache = {index: list(reader.blocks()) for index, reader in readers.items()}
    for output_index, item in enumerate(mapped, start=1):
        tracks = native_tracks[item.source_file_index]
        if not 0 <= item.stream_index < len(tracks):
            raise ValueError(
                f"Piste native introuvable : source={item.source_file_index}, stream={item.stream_index}"
            )
        source_track = tracks[item.stream_index]
        uid = deterministic_uid(
            source_identities[item.source_file_index], source_track.uid, output_index,
            normalized_language_value(item.track), item.track.title,
            item.track.flag_default, item.track.flag_forced,
        )
        track_uid_maps.setdefault(item.source_file_index, {})[source_track.uid] = uid
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
        for source_sequence, block in enumerate(block_cache[item.source_file_index]):
            if block.track_number == source_track.number:
                if block.lace_count > 1 and block.lace_index > 0:
                    continue
                source_timestamp_ns = block.timestamp_ns if block.timestamp_ns is not None else block.timestamp_ms * 1_000_000
                shifted_timestamp_ns = source_timestamp_ns + offset * 1_000_000
                if shifted_timestamp_ns < 0:
                    continue
                shifted = block.__class__(**{
                    **block.__dict__,
                    "timestamp_ms": round(shifted_timestamp_ns / 1_000_000),
                    "timestamp_ns": shifted_timestamp_ns,
                })
                output_packets.append(MatroskaMuxPacket(output_index, shifted, source_sequence))
    duration_ns = max((
        (packet.block.timestamp_ns if packet.block.timestamp_ns is not None else packet.block.timestamp_ms * 1_000_000)
        + (packet.block.duration_ns if packet.block.duration_ns is not None else (packet.block.duration_ms or 0) * 1_000_000)
        for packet in output_packets
    ), default=0)
    duration = round(duration_ns / 1_000_000)
    opaque: list[bytes] = []

    attachments: list[MatroskaAttachment] = []
    attachment_uid_maps: dict[int, dict[int, int]] = {}
    source_by_index = {source.file_index: source for source in config.sources}
    for source_index, source in source_by_index.items():
        available = readers[source_index].attachments()
        for selected in source.selected_attachments:
            if not 0 <= selected.local_index < len(available):
                raise ValueError(f"Attachment natif introuvable : {source.path} #{selected.local_index}")
            item = available[selected.local_index]
            output_uid = deterministic_uid(source_identities[source_index], item.uid, item.name)
            attachment_uid_maps.setdefault(source_index, {})[item.uid] = output_uid
            attachments.append(MatroskaAttachment(
                uid=output_uid, name=item.name,
                media_type=item.media_type, description=item.description, data=item.data,
            ))
    for path in config.extra_attachments:
        attachment_path = Path(path)
        attachments.append(MatroskaAttachment(
            uid=deterministic_uid(deterministic_source_identity(attachment_path), attachment_path.name),
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

    if config.tag_overrides is not None:
        tag_element = build_tags_element(resolved_global_tags(config))
        if tag_element:
            opaque.append(tag_element)
    else:
        for source in config.sources:
            if source.copy_tags:
                opaque.extend(
                    rewrite_tag_target_uids(
                        raw,
                        track_uids=track_uid_maps.get(source.file_index, {}),
                        attachment_uids=attachment_uid_maps.get(source.file_index, {}),
                        drop_chapter_targets=config.chapter_overrides is not None,
                    )
                    for raw in readers[source.file_index].raw_top_level(TAGS_ID)
                )
        if config.file_title:
            title_tag = build_tags_element({"title": config.file_title.strip()})
            if title_tag:
                opaque.append(title_tag)
    first_reader = readers[config.sources[0].file_index]
    _, source_writing_app = first_reader.segment_info_apps()
    segment_title = config.file_title.strip() or first_reader.segment_title()
    return MatroskaMuxPlan(
        config.output, tuple(output_tracks), tuple(output_packets), duration_ms=duration,
        duration_ns=duration_ns, timestamp_scale_ns=timestamp_scale_ns,
        muxing_app=f"Muxiveo {APP_VERSION_LABEL.removeprefix('v')}",
        writing_app=source_writing_app or "Muxiveo",
        title=segment_title,
        opaque_top_level=tuple(opaque),
    )


def run_native_remux(
    config: RemuxConfig,
    *,
    log: Callable[[str, str], None],
    log_step: Callable[[int, str], None],
    ffmpeg_bin: str,
    ffprobe_bin: str = "ffprobe",
    finalize: Callable[[Path], None] = lambda _path: None,
) -> TaskSignals:
    signals = TaskSignals()

    def task() -> None:
        canonical_root: Path | None = None
        try:
            log("INFO", "Backend Matroska natif multi-pistes sélectionné (plan v1).")
            run_config = replace(config, sources=[
                replace(
                    source,
                    origin_identity=source.origin_identity or deterministic_source_identity(source.path),
                )
                for source in config.sources
            ])
            if config.tmdb_cover is not None:
                work_root = config.work_dir or Path(tempfile.gettempdir())
                work_root.mkdir(parents=True, exist_ok=True)
                canonical_root = Path(tempfile.mkdtemp(prefix="Muxiveo_native_", dir=work_root))
                cover_url, cover_name = config.tmdb_cover
                cover = download_tmdb_cover(cover_url, cover_name, canonical_root / "attachments")
                run_config = replace(run_config, extra_attachments=[*run_config.extra_attachments, cover], tmdb_cover=None)
            non_matroska = [
                source for source in config.sources
                if source.path.suffix.lower() not in MATROSKA_EXTENSIONS
            ]
            if non_matroska:
                log_step(2, "Canonicalisation Matroska des sources non-MKV")
                work_root = config.work_dir or Path(tempfile.gettempdir())
                work_root.mkdir(parents=True, exist_ok=True)
                if canonical_root is None:
                    canonical_root = Path(tempfile.mkdtemp(prefix="Muxiveo_canonical_", dir=work_root))
                replacements: dict[int, Path] = {}
                for source in non_matroska:
                    target = canonical_root / f"source_{source.file_index}.mkv"
                    cmd = build_canonicalization_command(source, target, ffmpeg_bin)
                    result = subprocess.run(
                        cmd, capture_output=True, text=True,
                        **subprocess_windows_no_window_kwargs(),
                    )
                    if result.returncode:
                        raise RuntimeError(
                            f"Canonicalisation impossible pour {source.path.name}: {result.stderr or result.stdout}"
                        )
                    replacements[source.file_index] = target
                run_config = replace(run_config, sources=[
                    replace(source, path=replacements.get(source.file_index, source.path))
                    for source in run_config.sources
                ])
            # Materialise encoded audio variants as isolated Matroska sources;
            # the native writer still owns the final multi-track document.
            source_by_index = {source.file_index: source for source in run_config.sources}
            next_source_index = max(source_by_index, default=-1) + 1
            new_sources = list(run_config.sources)
            new_order: list[tuple[int, int] | tuple[int, int, str]] = []
            codec_encoders = {
                "aac": "aac", "ac3": "ac3", "eac3": "eac3", "flac": "flac",
            }
            for order_index, order_item in enumerate(run_config.track_order):
                source_index, stream_index = int(order_item[0]), int(order_item[1])
                entry_id = str(order_item[2]) if len(order_item) > 2 else ""
                source = source_by_index[source_index]
                candidates = [track for track in source.tracks if track.mkv_tid == stream_index]
                track = next((item for item in candidates if not entry_id or item.entry_id == entry_id), None)
                if track is None:
                    raise ValueError(
                        f"Piste de variante introuvable : source={source_index}, stream={stream_index}"
                    )
                target = normalized_rewrite_codec(track.codec)
                original = normalized_rewrite_codec(track.orig_codec or track.codec)
                needs_audio_encode = (
                    track.track_type == "audio"
                    and target in codec_encoders
                    and (track.is_new or target != original)
                )
                if not needs_audio_encode:
                    new_order.append(order_item)
                    continue
                if canonical_root is None:
                    work_root = config.work_dir or Path(tempfile.gettempdir())
                    canonical_root = Path(tempfile.mkdtemp(prefix="Muxiveo_native_", dir=work_root))
                target_path = canonical_root / f"audio_variant_{order_index}.mkv"
                cmd = [
                    ffmpeg_bin, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-i", str(source.path), "-map", f"0:{stream_index}",
                    "-c:a", codec_encoders[target],
                ]
                bitrate = audio_bitrate_kbps_from_display_info(track.display_info)
                if target != "flac" and bitrate:
                    cmd.extend(["-b:a", f"{int(bitrate)}k"])
                channel_match = re.search(r"\b(\d)\.(\d)\b", str(track.display_info or ""))
                channel_count = sum(map(int, channel_match.groups())) if channel_match else 0
                if target == "ac3" and channel_count > 6:
                    cmd.extend(["-ac:a", "6", "-channel_layout:a", "5.1"])
                cmd.append(str(target_path))
                result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    **subprocess_windows_no_window_kwargs(),
                )
                if result.returncode:
                    raise RuntimeError(f"Variante audio {target} impossible: {result.stderr or result.stdout}")
                materialized_track = replace(track, mkv_tid=0, orig_codec=track.codec, is_new=False)
                new_source = SourceInput(
                    target_path, next_source_index, [materialized_track],
                    origin_identity=f"{source.origin_identity}:audio:{stream_index}:{target}",
                )
                new_sources.append(new_source)
                new_order.append((next_source_index, 0, materialized_track.entry_id))
                next_source_index += 1
            run_config = replace(run_config, sources=new_sources, track_order=new_order)
            log_step(3, "Construction du plan Matroska natif")
            plan = build_native_plan(run_config)
            log_step(4, "Écriture Matroska native multi-pistes")
            def validate_partial(path: Path) -> None:
                result = subprocess.run(
                    [
                        ffprobe_bin, "-v", "error", "-show_entries",
                        "format=format_name", "-of", "json", str(path),
                    ],
                    capture_output=True, text=True,
                    **subprocess_windows_no_window_kwargs(),
                )
                if result.returncode:
                    raise RuntimeError(
                        "Validation ffprobe de la sortie native impossible: "
                        + str(result.stderr or result.stdout)
                    )

            MatroskaWriter().write(plan, external_validator=validate_partial)
            log_step(5, "Validation structure native terminée")
            finalize(config.output)
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


__all__ = [
    "FfmpegRemuxBackend", "MuxBackendDecision", "NativeMatroskaBackend",
    "RemuxBackend", "build_native_plan", "mux_backend_report",
    "build_canonicalization_command",
    "MATROSKA_EXTENSIONS",
    "native_capability_reasons", "native_preparation_commands",
    "run_native_remux", "select_mux_backend",
]
