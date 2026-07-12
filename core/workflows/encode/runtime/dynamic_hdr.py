"""Dynamic HDR request normalization for encode workflows."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from core.workflows.encode.models import EncodeConfig, VideoEncodeSettings
from core.workflows.encode.runtime.hdr_metadata import HdrMetadataProbeService


@dataclass(frozen=True)
class DynamicHdrNormalizerCallbacks:
    log: Callable[[str, str], None]
    wants_dynamic_hdr_copy: Callable[[EncodeConfig], bool]
    is_video_passthrough: Callable[[EncodeConfig], bool]
    primary_video_settings: Callable[[EncodeConfig], VideoEncodeSettings]
    video_tracks: Callable[[EncodeConfig], list[VideoEncodeSettings]]
    video_source_path: Callable[[EncodeConfig], Path]
    video_source_from_settings: Callable[[EncodeConfig, VideoEncodeSettings], Path]
    detect_source_dynamic_hdr_presence: Callable[[Path], tuple[bool, bool] | None]
    extract_static_hdr_metadata: Callable[[Path], tuple[str, str]]
    extract_static_hdr_via_ffprobe: Callable[[Path], tuple[str, str]]
    color_primaries_label: Callable[[Path], str]
    build_master_display_for_primaries: Callable[[str], str]


class DynamicHdrConfigNormalizer:
    """Normalize DoVi/HDR10+ copy requests without changing workflow choices."""

    def __init__(self, callbacks: DynamicHdrNormalizerCallbacks) -> None:
        self._cb = callbacks

    def normalize_single(self, config: EncodeConfig) -> EncodeConfig:
        if not self._cb.wants_dynamic_hdr_copy(config):
            return config

        source = self._cb.video_source_path(config)
        detected = self._cb.detect_source_dynamic_hdr_presence(source)
        if detected is None:
            self._cb.log(
                "WARN",
                "Détection DoVi/HDR10+ impossible sur la source — workflow demandé conservé.",
            )
            return config

        video = self._cb.primary_video_settings(config)
        normalized_video = self._normalize_video(
            video,
            source=source,
            detected=detected,
            track_label=None,
        )
        normalized_tracks = list(config.video_tracks)
        if normalized_tracks:
            normalized_tracks = [normalized_video, *normalized_tracks[1:]]
        else:
            normalized_tracks = [normalized_video]

        normalized = replace(
            config,
            video=normalized_video,
            video_tracks=normalized_tracks,
            copy_dv=normalized_video.copy_dv,
            copy_hdr10plus=normalized_video.copy_hdr10plus,
            dovi_profile=normalized_video.dovi_profile,
        )
        if not self._cb.wants_dynamic_hdr_copy(normalized) and self._cb.is_video_passthrough(config):
            self._cb.log(
                "INFO",
                "Aucun DoVi/HDR10+ utile à recopier — passthrough vidéo direct.",
            )
        return normalized

    def normalize_multi(self, config: EncodeConfig) -> EncodeConfig:
        videos: list[VideoEncodeSettings] = []
        for index, video in enumerate(self._cb.video_tracks(config), start=1):
            if not (video.copy_dv or video.copy_hdr10plus):
                videos.append(video)
                continue

            source = self._cb.video_source_from_settings(config, video)
            detected = self._cb.detect_source_dynamic_hdr_presence(source)
            track_label = f"Piste #{index}"
            if detected is None:
                self._cb.log(
                    "WARN",
                    f"Détection DoVi/HDR10+ impossible pour la piste vidéo #{index} — demande conservée.",
                )
                videos.append(video)
                continue

            videos.append(
                self._normalize_video(
                    video,
                    source=source,
                    detected=detected,
                    track_label=track_label,
                )
            )

        primary = videos[0]
        return replace(
            config,
            video=primary,
            video_tracks=videos,
            copy_dv=primary.copy_dv,
            copy_hdr10plus=primary.copy_hdr10plus,
            dovi_profile=primary.dovi_profile,
        )

    def _normalize_video(
        self,
        video: VideoEncodeSettings,
        *,
        source: Path,
        detected: tuple[bool, bool],
        track_label: str | None,
    ) -> VideoEncodeSettings:
        has_dv, has_hdr10plus = detected
        copy_dv = video.copy_dv and has_dv
        copy_hdr10plus = video.copy_hdr10plus and has_hdr10plus

        if video.copy_dv and not copy_dv:
            self._cb.log(
                "WARN",
                self._track_message(
                    track_label,
                    "Copy DoVi demandé mais aucune donnée DoVi détectée",
                    "option ignorée.",
                ),
            )
        if video.copy_hdr10plus and not copy_hdr10plus:
            self._cb.log(
                "WARN",
                self._track_message(
                    track_label,
                    "Copy HDR10+ demandé mais aucune donnée HDR10+ détectée",
                    "option ignorée.",
                ),
            )

        auto_md, auto_cll = video.master_display, video.max_cll
        analysis_pending = bool(
            str(getattr(video, "static_hdr_metadata_analysis_request", "") or "").strip()
        )
        if (
            (copy_dv or copy_hdr10plus)
            and (not auto_md or not auto_cll)
            and not analysis_pending
        ):
            auto_md, auto_cll = self._fill_static_hdr_fallbacks(
                source,
                master_display=auto_md,
                max_cll=auto_cll,
                track_label=track_label,
            )
        elif analysis_pending:
            self._cb.log(
                "INFO",
                self._track_message(
                    track_label,
                    "Analyse HDR10 P5→P8.1 programmée",
                    "fallback statique différé au workflow.",
                ),
            )

        return replace(
            video,
            copy_dv=copy_dv,
            copy_hdr10plus=copy_hdr10plus,
            master_display=auto_md,
            max_cll=auto_cll,
        )

    def _fill_static_hdr_fallbacks(
        self,
        source: Path,
        *,
        master_display: str,
        max_cll: str,
        track_label: str | None,
    ) -> tuple[str, str]:
        auto_md, auto_cll = master_display, max_cll

        md_mi, cll_mi = self._cb.extract_static_hdr_metadata(source)
        if not auto_md and md_mi:
            auto_md = md_mi
            self._cb.log("WARN", self._static_hdr_message(track_label, "Master Display", "mediainfo", md_mi))
        if not auto_cll and cll_mi:
            auto_cll = cll_mi
            self._cb.log("WARN", self._static_hdr_message(track_label, "MaxCLL/MaxFALL", "mediainfo", cll_mi))

        if not auto_md or not auto_cll:
            md_ff, cll_ff = self._cb.extract_static_hdr_via_ffprobe(source)
            if not auto_md and md_ff:
                auto_md = md_ff
                self._cb.log("WARN", self._static_hdr_message(track_label, "Master Display", "ffprobe", md_ff))
            if not auto_cll and cll_ff:
                auto_cll = cll_ff
                self._cb.log("WARN", self._static_hdr_message(track_label, "MaxCLL/MaxFALL", "ffprobe", cll_ff))

        if not auto_md:
            primaries = self._cb.color_primaries_label(source)
            synth_md = self._cb.build_master_display_for_primaries(primaries)
            if synth_md:
                auto_md = synth_md
                if track_label:
                    self._cb.log(
                        "WARN",
                        f"{track_label} : Master Display reconstruit depuis "
                        f"color_primaries={primaries or '?'} + luminance par défaut.",
                    )
                else:
                    self._cb.log(
                        "WARN",
                        f"Master Display reconstruit depuis color_primaries={primaries or '?'} "
                        "+ luminance par défaut 1000/0.0001 nits (master UHD BD typique). "
                        "Si la source est gradée >1000 nits, éditez le champ Master Display avant l'encode.",
                    )
        if not auto_cll:
            auto_cll = HdrMetadataProbeService.DEFAULT_MAX_CLL
            if track_label:
                self._cb.log(
                    "WARN",
                    f"{track_label} : MaxCLL/MaxFALL non trouvés — défaut conservatif "
                    f"({HdrMetadataProbeService.DEFAULT_MAX_CLL}).",
                )
            else:
                self._cb.log(
                    "WARN",
                    "MaxCLL/MaxFALL non trouvés — défaut conservatif appliqué "
                    f"({HdrMetadataProbeService.DEFAULT_MAX_CLL}).",
                )
        return auto_md, auto_cll

    @staticmethod
    def _track_message(track_label: str | None, subject: str, suffix: str) -> str:
        if track_label:
            return f"{subject} pour la {track_label.lower().replace('piste', 'piste vidéo', 1)} — {suffix}"
        return f"{subject} — {suffix}"

    @staticmethod
    def _static_hdr_message(
        track_label: str | None,
        field: str,
        source_name: str,
        value: str,
    ) -> str:
        if track_label:
            source_label = f"{source_name} (fallback mediainfo)" if source_name == "ffprobe" else source_name
            return f"{track_label} : {field} extraits via {source_label}."
        if source_name == "mediainfo":
            return f"{field} absents côté UI — auto-extraits via mediainfo ({value})."
        return f"{field} extraits via ffprobe (fallback mediainfo) : {value}."
