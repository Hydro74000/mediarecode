"""Backend selection for Matroska remuxing.

The public contract deliberately lives here instead of in the FFmpeg runner:
an exact-job can request a backend without coupling its JSON shape to an
implementation.  The native backend is capability-gated; ``auto`` remains
backwards compatible by selecting FFmpeg when a plan needs a feature that has
not yet been materialised by the native writer.
"""

from __future__ import annotations

from dataclasses import dataclass

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


__all__ = ["MuxBackendDecision", "native_capability_reasons", "select_mux_backend"]
