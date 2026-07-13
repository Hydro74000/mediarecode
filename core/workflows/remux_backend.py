"""Backend selection for Matroska remuxing.

The public contract deliberately lives here instead of in the FFmpeg runner:
an exact-job can request a backend without coupling its JSON shape to an
implementation.  The native backend is capability-gated; ``auto`` remains
backwards compatible by selecting FFmpeg when a plan needs a feature that has
not yet been materialised by the native writer.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from core.runner import TaskSignals
from core.subprocess_utils import subprocess_windows_no_window_kwargs
from core.workflows.matroska_native_muxer import MatroskaNativeMuxer

from core.workflows.remux_models import RemuxConfig, normalize_mux_backend


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

    The existing native writer is intentionally limited to one elementary
    HEVC video stream.  Keeping this check central means later multi-track
    writer increments only remove blockers; they never change v1 semantics.
    """
    reasons: list[str] = []
    if config.output.suffix.lower() != ".mkv":
        reasons.append("le backend natif écrit uniquement des sorties .mkv")
    selected = []
    source_by_index = {source.file_index: source for source in config.sources}
    for item in config.track_order:
        source_index, stream_index = int(item[0]), int(item[1])
        source = source_by_index.get(source_index)
        track = next((t for t in (source.tracks if source else []) if t.mkv_tid == stream_index), None)
        if track is not None:
            selected.append(track)
    if len(selected) != 1 or selected[0].track_type != "video":
        reasons.append("le muxeur natif courant exige une unique piste vidéo")
    elif "hevc" not in selected[0].codec.lower() and "h.265" not in selected[0].codec.lower():
        reasons.append("le muxeur natif courant prend en charge HEVC uniquement")
    if any(int(track.time_shift_ms or 0) for track in selected):
        reasons.append("les décalages de timeline ne sont pas encore matérialisés nativement")
    if config.extra_attachments or any(source.selected_attachments for source in config.sources):
        reasons.append("les pièces jointes multi-sources ne sont pas encore écrites nativement")
    if config.chapter_overrides is not None or config.keep_chapters:
        reasons.append("les chapitres ne sont pas encore écrits nativement")
    if config.tag_overrides is not None or config.file_title or any(source.copy_tags for source in config.sources):
        reasons.append("les tags Matroska ne sont pas encore écrits nativement")
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


def run_native_single_hevc(
    config: RemuxConfig,
    *,
    ffmpeg_bin: str,
    ffprobe_bin: str,
    log: callable,
    log_step: callable,
) -> TaskSignals:
    """Execute the currently supported native final-write path.

    FFmpeg only extracts Annex-B HEVC; the final Matroska document is written
    by :class:`MatroskaNativeMuxer`.  This deliberately keeps the native
    boundary honest while the generic multi-track writer is introduced.
    """
    source_by_index = {source.file_index: source for source in config.sources}
    source_index, stream_index = int(config.track_order[0][0]), int(config.track_order[0][1])
    source = source_by_index[source_index]
    track = next(track for track in source.tracks if track.mkv_tid == stream_index)
    signals = TaskSignals()
    work_root = config.work_dir or Path(tempfile.gettempdir())
    work_root.mkdir(parents=True, exist_ok=True)

    def task() -> None:
        tmp_dir = Path(tempfile.mkdtemp(prefix="Muxiveo_native_", dir=work_root))
        raw_hevc = tmp_dir / "video.hevc"
        partial = config.output.with_suffix(config.output.suffix + ".partial")
        try:
            log("INFO", "Backend Matroska natif sélectionné (HEVC mono-piste).")
            log_step(2, "Extraction du flux HEVC pour muxage natif")
            cmd = [ffmpeg_bin, "-y", "-nostdin", "-hide_banner", "-i", str(source.path), "-map", f"0:{stream_index}", "-c:v", "copy", "-f", "hevc", str(raw_hevc)]
            result = subprocess.run(cmd, capture_output=True, text=True, **subprocess_windows_no_window_kwargs())
            if result.returncode:
                raise RuntimeError(result.stderr or "Extraction HEVC impossible.")
            log_step(3, "Lecture des dimensions vidéo")
            probe = subprocess.run(
                [ffprobe_bin, "-v", "error", "-select_streams", f"v:{stream_index}", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(source.path)],
                capture_output=True, text=True, **subprocess_windows_no_window_kwargs(),
            )
            if probe.returncode:
                raise RuntimeError(probe.stderr or "Inspection vidéo impossible.")
            match = re.search(r"(\d+)\s*,\s*(\d+)", probe.stdout or "")
            if not match:
                raise RuntimeError("Dimensions vidéo introuvables.")
            log_step(4, "Écriture Matroska native")
            partial.unlink(missing_ok=True)
            MatroskaNativeMuxer(ffprobe_bin=ffprobe_bin).mux(
                hevc_input=raw_hevc,
                source_for_timestamps=source.path,
                output=partial,
                pixel_width=int(match.group(1)),
                pixel_height=int(match.group(2)),
                language=track.language or "und",
                timestamp_order="packet",
            )
            partial.replace(config.output)
            log_step(5, "Validation sortie native terminée")
            signals.finished.emit(str(config.output))
        except Exception as exc:
            partial.unlink(missing_ok=True)
            signals.failed.emit(str(exc), exc)
        finally:
            for path in (raw_hevc, tmp_dir):
                try:
                    if path.is_dir():
                        path.rmdir()
                    else:
                        path.unlink(missing_ok=True)
                except OSError:
                    pass

    executor = ThreadPoolExecutor(max_workers=1)
    executor.submit(task)
    executor.shutdown(wait=False)
    return signals


__all__ = ["MuxBackendDecision", "native_capability_reasons", "run_native_single_hevc", "select_mux_backend"]
