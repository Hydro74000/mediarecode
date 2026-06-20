"""NVEncC backend implementation for the encode workflow."""

from __future__ import annotations

from typing import cast

from core.bluray import is_bluray_playlist
from core.runner import TaskSignals
from core.workflows.encode.backends.ffmpeg_backend import FfmpegEncodeBackend
from core.workflows.encode.backends.models import (
    BackendCapabilities,
    BackendContext,
    EncodeBackend,
    ProgressEvent,
)
from core.workflows.encode.backends.progress import parse_nvencc_progress
from core.workflows.encode.catalog import CQ_CAPABLE_VIDEO_CODECS
from core.workflows.encode.models import EncodeConfig, QualityMode, VideoEncodeSettings
from core.workflows.encode.planning.plan_models import EncodePlan
from core.workflows.encode.runtime.nvencc import (
    is_nvencc_codec,
    nvencc_requires_ffmpeg_filter_pipe,
    nvencc_supports_dynamic_hdr,
    nvencc_supports_manual_static_hdr,
    sanitize_nvencc_extra_params,
)


class NvenccEncodeBackend(EncodeBackend):
    backend_id = "nvencc"

    def __init__(self) -> None:
        self._ffmpeg_backend = FfmpegEncodeBackend()

    def capabilities(
        self,
        codec: str,
        config_ctx: BackendContext | None = None,
    ) -> BackendCapabilities:
        _ = config_ctx
        modes: list[QualityMode] = [QualityMode.CRF]
        if codec in CQ_CAPABLE_VIDEO_CODECS:
            modes.append(QualityMode.CQ)
        modes.append(QualityMode.BITRATE)
        return BackendCapabilities(
            backend_id=self.backend_id,
            quality_modes=tuple(modes),
            supports_dynamic_hdr=nvencc_supports_dynamic_hdr(codec),
            supports_manual_static_hdr=nvencc_supports_manual_static_hdr(codec),
            supports_tonemap=True,
            supports_multi_video=False,
            supports_main_filters=True,
            extra_params_backend="nvencc",
            progress_kind="nvencc",
        )

    def validate(
        self,
        config: EncodeConfig,
        *,
        plan: object | None,
        ctx: BackendContext,
    ) -> list[str]:
        all_video_tracks = ctx.workflow._video_tracks(config)
        videos = [video for video in all_video_tracks if video.codec != "copy"]
        if not any(is_nvencc_codec(video.codec) for video in videos):
            return []

        errors: list[str] = []
        if len(all_video_tracks) != 1:
            errors.append("NVEncC ne supporte pas le mode multi-pistes vidéo dans cette version.")
            return errors
        if len(videos) != 1 or not is_nvencc_codec(videos[0].codec):
            errors.append("NVEncC ne supporte qu'une seule piste vidéo encodée dans cette version.")
            return errors

        video = videos[0]
        if not ctx.workflow._nvencc_bin:
            errors.append("NVEncC est sélectionné mais le binaire n'est pas configuré.")
        if video.quality_mode == QualityMode.SIZE:
            errors.append("NVEncC ne supporte pas le mode taille cible (2 passes) dans cette version.")
        if video.inject_hdr_meta and video.codec == "nvencc_h264":
            errors.append("NVEncC H.264 ne supporte pas les métadonnées HDR statiques.")
        if (video.copy_dv or video.copy_hdr10plus) and is_bluray_playlist(video.source_path or config.source):
            errors.append(
                "NVEncC ne peut pas copier DoVi/HDR10+ dynamiques depuis une playlist Blu-ray ; "
                "utilisez le backend FFmpeg pour cette source."
            )
        if (video.copy_dv or video.copy_hdr10plus) and not nvencc_supports_dynamic_hdr(video.codec):
            errors.append("Le codec NVEncC sélectionné ne supporte pas DoVi/HDR10+.")
        if (video.copy_dv or video.copy_hdr10plus) and nvencc_requires_ffmpeg_filter_pipe(video):
            errors.append(
                "NVEncC avec préfiltrage FFmpeg (deblock/chroma_smooth/crop %/resize %) "
                "est incompatible avec la copie DoVi/HDR10+ dynamique dans cette version."
            )
        if video.copy_dv or video.copy_hdr10plus:
            try:
                ctx.workflow._resolve_nvencc_input_routing(config)
            except Exception as exc:
                errors.append(str(exc))
        _ = plan
        return errors

    def build_preview(
        self,
        config: EncodeConfig,
        *,
        ctx: BackendContext,
    ) -> list[list[str]]:
        preview = ctx.workflow._build_nvencc_pipeline_commands(config)
        if preview is not None:
            return [list(cmd) for cmd in preview]
        return self._ffmpeg_backend.build_preview(config, ctx=ctx)

    def build_single_preview(
        self,
        config: EncodeConfig,
        *,
        ctx: BackendContext,
    ) -> list[str]:
        preview = self.build_preview(config, ctx=ctx)
        return list(preview[0]) if preview else []

    def run(
        self,
        config: EncodeConfig,
        cleanup_paths: list,
        *,
        ctx: BackendContext,
        prep_signals: TaskSignals | None = None,
    ) -> TaskSignals:
        return ctx.workflow._run_nvencc_direct_output(
            config,
            cleanup_paths,
            prep_signals=prep_signals,
            plan=cast(EncodePlan | None, ctx.plan),
        )

    def normalize_extra_params(self, video: VideoEncodeSettings) -> str:
        return " ".join(sanitize_nvencc_extra_params(video.extra_params)).strip()

    def parse_progress(self, line: str) -> ProgressEvent | None:
        return parse_nvencc_progress(line)
