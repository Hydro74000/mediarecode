"""Codec domain helpers extracted from EncodeWorkflow."""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from core.workflows.encode.catalog import (
    AMF_VIDEO_CODECS,
    NVENC_VIDEO_CODECS,
    NVENCC_VIDEO_CODECS,
    QSV_VIDEO_CODECS,
    VAAPI_VIDEO_CODECS,
    is_h264_video_codec,
    needs_static_hdr_bitstream_patch_codec,
    supports_10bit,
)
from core.workflows.encode.models import (
    AudioTrackSettings,
    QualityMode,
    VideoEncodeSettings,
    normalize_audio_bitrate_kbps,
)

# Experimental NVENC-specific static HDR bitstream patch kept in-tree for
# reference, but disabled in the active workflow.
ENABLE_EXPERIMENTAL_NVENC_STATIC_HDR_PATCH = False


@dataclass(frozen=True)
class EncodeCodecDomainCallbacks:
    platform: str
    vaapi_device: str | None = None
    qsv_device: str | None = None
    amf_device: str | None = None
    nvenc_device: str | None = None


def force_h264_8bit(video: VideoEncodeSettings) -> bool:
    return bool(getattr(video, "force_8bit", False)) and is_h264_video_codec(video.codec)


def h264_8bit_pix_fmt_args(video: VideoEncodeSettings) -> list[str]:
    if not force_h264_8bit(video):
        return []
    if video.codec == "libx264":
        return ["-pix_fmt", "yuv420p"]
    return ["-pix_fmt", "nv12"]


def force_10bit_active(video: VideoEncodeSettings) -> bool:
    """Vrai quand l'utilisateur a activé 10-bit pour un codec compatible.

    force_8bit (H.264 + source >8-bit) prend priorité et désactive 10-bit.
    """
    if force_h264_8bit(video):
        return False
    if not bool(getattr(video, "force_10bit", False)):
        return False
    return supports_10bit(video.codec)


def has_video_transform(video: VideoEncodeSettings) -> bool:
    return bool(getattr(video, "has_video_transform", lambda: False)())


def has_cpu_video_filter(video: VideoEncodeSettings) -> bool:
    """True when FFmpeg must keep frames in system memory for filtering."""
    if video.codec == "copy":
        return False
    return bool(
        getattr(video, "p5_to_hdr10", False)
        or
        video.resize.is_active()
        or video.crop.is_active()
        or video.filters.is_active()
        or video.tonemap_to_sdr
    )


def ten_bit_args(video: VideoEncodeSettings) -> list[str]:
    """Tokens ffmpeg pour forcer une sortie 10-bit (profile + pix_fmt).

    Les codecs VAAPI gèrent leur pix_fmt via build_encoder_vf (hwupload p010).
    """
    if not force_10bit_active(video):
        return []
    codec = video.codec
    if codec == "libx265":
        return ["-pix_fmt", "yuv420p10le"]
    if codec == "libx264":
        return ["-pix_fmt", "yuv420p10le", "-profile:v", "high10"]
    if codec == "libsvtav1":
        return ["-pix_fmt", "yuv420p10le"]
    if codec in ("hevc_nvenc", "hevc_amf", "hevc_qsv"):
        return ["-pix_fmt", "p010le", "-profile:v", "main10"]
    if codec in ("av1_nvenc", "av1_amf", "av1_qsv"):
        return ["-pix_fmt", "p010le"]
    if codec in VAAPI_VIDEO_CODECS:
        # pix_fmt géré dans build_encoder_vf via hwupload ; on ajoute le profile.
        if codec == "hevc_vaapi":
            return ["-profile:v", "main10"]
        return []
    return []


def hw_extra_args(video: VideoEncodeSettings) -> list[str]:
    """Tokens ffmpeg additionnels pour encodeurs HW (NVENC/AMF/QSV).

    Le champ extra_params est passé tel quel à shlex.split — l'utilisateur saisit
    une suite de flags ffmpeg (ex: ``-spatial-aq 1 -temporal-aq 1 -rc-lookahead 32``).
    Les codecs software (libx265, libsvtav1) consomment extra_params via leur
    propre syntaxe et n'utilisent PAS cette fonction.
    """
    raw = (video.extra_params or "").strip()
    if not raw:
        return []
    try:
        return shlex.split(raw)
    except ValueError:
        return []


def _is_hevc_nvenc_safe_preset(video: VideoEncodeSettings) -> bool:
    return video.codec == "hevc_nvenc" and str(video.preset or "").strip().lower() == "safe"


def nvenc_effective_preset(video: VideoEncodeSettings) -> str:
    if _is_hevc_nvenc_safe_preset(video):
        # Backward-compat only: old saved profiles may still contain preset=safe.
        # The experimental workflow patch is disabled, but we still map the
        # dormant logical preset to a valid native NVENC preset.
        return "p5"
    return video.preset


def nvenc_safe_extra_args(video: VideoEncodeSettings) -> list[str]:
    # Experimental NVENC "safe" patch kept in-tree for reference only.
    # The workflow no longer injects these flags automatically.
    _ = video
    return []


def x265_params(video: VideoEncodeSettings) -> str:
    parts: list[str] = []
    if video.extra_params:
        parts.append(video.extra_params.strip(":"))
    if video.inject_hdr_meta and not video.tonemap_to_sdr:
        if video.master_display:
            parts.append(f"master-display={video.master_display}")
        if video.max_cll:
            parts.append(f"max-cll={video.max_cll}")
    return ":".join(part for part in parts if part)


def requests_hdr_metadata(video: VideoEncodeSettings) -> bool:
    if video.tonemap_to_sdr:
        return False
    return bool(video.inject_hdr_meta or video.copy_dv or video.copy_hdr10plus)


def needs_static_hdr_bitstream_patch(video: VideoEncodeSettings) -> bool:
    if not ENABLE_EXPERIMENTAL_NVENC_STATIC_HDR_PATCH:
        return False
    if not requests_hdr_metadata(video):
        return False
    if not (video.master_display or video.max_cll):
        return False
    return needs_static_hdr_bitstream_patch_codec(video.codec)


def should_reinject_static_hdr_metadata(video: VideoEncodeSettings) -> bool:
    """Vrai si le pipeline d'injection doit reposer des SEI HDR statiques.

    Ce chemin est utile dès qu'on passe par la pipeline de réinjection
    DoVi/HDR10+ : une conversion P5/P7→P8 ou certaines chaînes HEVC
    hardware peuvent perdre les SEI MDCV/CLL même si la source initiale
    les exposait correctement. L'injection est idempotente et ne duplique
    pas les SEI déjà présents.
    """
    if not requests_hdr_metadata(video):
        return False
    if video.codec == "copy":
        return bool(
            video.inject_hdr_meta
            and video.copy_dv
            and str(video.dovi_profile or "0").strip() == "2"
            and (video.master_display or video.max_cll)
        )
    return bool(video.master_display or video.max_cll)


def video_codec_args(video: VideoEncodeSettings, bitrate_kbps: int, *, callbacks: EncodeCodecDomainCallbacks) -> list[str]:
    # NVEncC est un binaire externe (pas un encodeur ffmpeg) : la construction
    # de la commande passe par core/workflows/encode/runtime/nvencc.py. Côté
    # ffmpeg-only, on retourne une liste vide pour que le caller (workflow.py)
    # ait détecté NVEncC en amont et basculé sur le pipeline 3-process.
    if video.codec in NVENCC_VIDEO_CODECS:
        return []
    if video.quality_mode == QualityMode.CRF:
        return video_codec_args_crf(video, callbacks=callbacks)
    if video.quality_mode == QualityMode.CQ:
        return video_codec_args_cq(video, callbacks=callbacks)
    return video_codec_args_bitrate(video, bitrate_kbps, callbacks=callbacks)


def video_codec_args_cq(video: VideoEncodeSettings, *, callbacks: EncodeCodecDomainCallbacks) -> list[str]:
    """Mode CQ — Constant Quality côté encodeurs HW.

    Sur les codecs software (x264/x265/svt-av1), CQ n'a pas d'équivalent natif :
    on retombe sur le mode CRF (qui est déjà la qualité constante de ces encodeurs).
    """
    cq = int(video.cq)
    match video.codec:
        case "copy":
            return ["-c:v", "copy"]
        case "hevc_nvenc":
            return [
                "-c:v", "hevc_nvenc", "-rc:v", "vbr", "-b:v", "0", "-cq:v", str(cq), "-preset:v", nvenc_effective_preset(video),
                *nvenc_device_args(callbacks),
                *ten_bit_args(video),
                *nvenc_safe_extra_args(video),
                *hw_extra_args(video),
            ]
        case "h264_nvenc":
            return [
                "-c:v", "h264_nvenc", "-rc:v", "vbr", "-b:v", "0", "-cq:v", str(cq), "-preset:v", nvenc_effective_preset(video),
                *nvenc_device_args(callbacks),
                *h264_8bit_pix_fmt_args(video),
                *hw_extra_args(video),
            ]
        case "av1_nvenc":
            return [
                "-c:v", "av1_nvenc", "-rc:v", "vbr", "-b:v", "0", "-cq:v", str(cq), "-preset:v", nvenc_effective_preset(video),
                *nvenc_device_args(callbacks),
                *ten_bit_args(video),
                *hw_extra_args(video),
            ]
        case "hevc_amf":
            args = ["-c:v", "hevc_amf", "-rc", "cqp", "-qp_i", str(cq), "-qp_p", str(cq), "-qp_b", str(cq)]
            if video.preset:
                args.extend(["-quality", video.preset])
            args.extend(ten_bit_args(video))
            args.extend(hw_extra_args(video))
            return args
        case "h264_amf":
            args = ["-c:v", "h264_amf", "-rc", "cqp", "-qp_i", str(cq), "-qp_p", str(cq), "-qp_b", str(cq)]
            if video.preset:
                args.extend(["-quality", video.preset])
            args.extend(h264_8bit_pix_fmt_args(video))
            args.extend(hw_extra_args(video))
            return args
        case "av1_amf":
            args = ["-c:v", "av1_amf", "-rc", "cqp", "-qp_i", str(cq), "-qp_p", str(cq)]
            if video.preset:
                args.extend(["-quality", video.preset])
            args.extend(ten_bit_args(video))
            args.extend(hw_extra_args(video))
            return args
        case "hevc_qsv":
            args = ["-c:v", "hevc_qsv", "-global_quality", str(cq), "-look_ahead", "0", "-async_depth", "4"]
            if video.preset:
                args.extend(["-preset", video.preset])
            args.extend(ten_bit_args(video))
            args.extend(hw_extra_args(video))
            return args
        case "h264_qsv":
            args = ["-c:v", "h264_qsv", "-global_quality", str(cq), "-look_ahead", "0", "-async_depth", "4"]
            if video.preset:
                args.extend(["-preset", video.preset])
            args.extend(h264_8bit_pix_fmt_args(video))
            args.extend(hw_extra_args(video))
            return args
        case "av1_qsv":
            args = ["-c:v", "av1_qsv", "-global_quality", str(cq), "-async_depth", "4"]
            if video.preset:
                args.extend(["-preset", video.preset])
            args.extend(ten_bit_args(video))
            args.extend(hw_extra_args(video))
            return args
        case "hevc_vaapi":
            return [
                "-c:v", "hevc_vaapi", "-rc_mode", "CQP", "-qp", str(cq),
                "-compression_level", (video.preset or "4"), "-async_depth", "4",
                *ten_bit_args(video),
                *hw_extra_args(video),
            ]
        case "h264_vaapi":
            return [
                "-c:v", "h264_vaapi", "-rc_mode", "CQP", "-qp", str(cq),
                "-compression_level", (video.preset or "4"), "-async_depth", "4",
                *h264_8bit_pix_fmt_args(video),
                *hw_extra_args(video),
            ]
        case "av1_vaapi":
            return [
                "-c:v", "av1_vaapi", "-rc_mode", "CQP", "-qp", str(cq),
                "-compression_level", (video.preset or "4"), "-async_depth", "4",
                *ten_bit_args(video),
                *hw_extra_args(video),
            ]
        case _:
            # Software : pas d'équivalent natif → fallback sur CRF avec la valeur CQ.
            override = VideoEncodeSettings(**{**video.__dict__, "crf": cq, "quality_mode": QualityMode.CRF})
            return video_codec_args_crf(override, callbacks=callbacks)


def video_codec_args_crf(video: VideoEncodeSettings, *, callbacks: EncodeCodecDomainCallbacks) -> list[str]:
    match video.codec:
        case "copy":
            return ["-c:v", "copy"]
        case "libx265":
            args = ["-c:v", "libx265", "-crf", str(video.crf), "-preset", video.preset]
            args.extend(ten_bit_args(video))
            x265 = x265_params(video)
            if x265:
                args.extend(["-x265-params", x265])
            return args
        case "libx264":
            return [
                "-c:v", "libx264", "-crf", str(video.crf), "-preset", video.preset,
                *h264_8bit_pix_fmt_args(video),
                *ten_bit_args(video),
            ]
        case "libsvtav1":
            args = ["-c:v", "libsvtav1", "-crf", str(video.crf), "-preset", video.preset]
            args.extend(ten_bit_args(video))
            if video.extra_params:
                args.extend(["-svtav1-params", video.extra_params])
            return args
        case "hevc_nvenc":
            return [
                "-c:v", "hevc_nvenc", "-rc:v", "vbr", "-cq:v", str(video.crf), "-preset:v", nvenc_effective_preset(video),
                *nvenc_device_args(callbacks),
                *ten_bit_args(video),
                *nvenc_safe_extra_args(video),
                *hw_extra_args(video),
            ]
        case "hevc_amf":
            args = ["-c:v", "hevc_amf", "-rc", "cqp", "-qp_p", str(video.crf), "-qp_i", str(video.crf)]
            if video.preset:
                args.extend(["-quality", video.preset])
            args.extend(ten_bit_args(video))
            args.extend(hw_extra_args(video))
            return args
        case "hevc_qsv":
            args = ["-c:v", "hevc_qsv", "-global_quality", str(video.crf), "-look_ahead", "1", "-async_depth", "4"]
            if video.preset:
                args.extend(["-preset", video.preset])
            args.extend(ten_bit_args(video))
            args.extend(hw_extra_args(video))
            return args
        case "hevc_vaapi":
            return [
                "-c:v", "hevc_vaapi", "-rc_mode", "CQP", "-qp", str(video.crf),
                "-compression_level", (video.preset or "4"),
                "-async_depth", "4",
                *ten_bit_args(video),
                *hw_extra_args(video),
            ]
        case "h264_nvenc":
            return [
                "-c:v", "h264_nvenc", "-rc:v", "vbr", "-cq:v", str(video.crf), "-preset:v", nvenc_effective_preset(video),
                *nvenc_device_args(callbacks),
                *h264_8bit_pix_fmt_args(video),
                *hw_extra_args(video),
            ]
        case "h264_amf":
            args = ["-c:v", "h264_amf", "-rc", "cqp", "-qp_p", str(video.crf), "-qp_i", str(video.crf)]
            if video.preset:
                args.extend(["-quality", video.preset])
            args.extend(h264_8bit_pix_fmt_args(video))
            args.extend(hw_extra_args(video))
            return args
        case "h264_qsv":
            args = ["-c:v", "h264_qsv", "-global_quality", str(video.crf), "-async_depth", "4"]
            if video.preset:
                args.extend(["-preset", video.preset])
            args.extend(h264_8bit_pix_fmt_args(video))
            args.extend(hw_extra_args(video))
            return args
        case "h264_vaapi":
            return [
                "-c:v", "h264_vaapi", "-rc_mode", "CQP", "-qp", str(video.crf),
                "-compression_level", (video.preset or "4"),
                "-async_depth", "4",
                *h264_8bit_pix_fmt_args(video),
                *hw_extra_args(video),
            ]
        case "av1_nvenc":
            return [
                "-c:v", "av1_nvenc", "-rc:v", "vbr", "-cq:v", str(video.crf), "-preset:v", nvenc_effective_preset(video),
                *nvenc_device_args(callbacks),
                *ten_bit_args(video),
                *hw_extra_args(video),
            ]
        case "av1_amf":
            args = ["-c:v", "av1_amf", "-rc", "cqp", "-qp_p", str(video.crf), "-qp_i", str(video.crf)]
            if video.preset:
                args.extend(["-quality", video.preset])
            args.extend(ten_bit_args(video))
            args.extend(hw_extra_args(video))
            return args
        case "av1_qsv":
            args = ["-c:v", "av1_qsv", "-global_quality", str(video.crf), "-async_depth", "4"]
            if video.preset:
                args.extend(["-preset", video.preset])
            args.extend(ten_bit_args(video))
            args.extend(hw_extra_args(video))
            return args
        case "av1_vaapi":
            return [
                "-c:v", "av1_vaapi", "-rc_mode", "CQP", "-qp", str(video.crf),
                "-compression_level", (video.preset or "4"),
                "-async_depth", "4",
                *ten_bit_args(video),
                *hw_extra_args(video),
            ]
        case _:
            return ["-c:v", video.codec, "-crf", str(video.crf)]


def video_codec_args_bitrate(
    video: VideoEncodeSettings,
    bitrate_kbps: int,
    *,
    callbacks: EncodeCodecDomainCallbacks,
) -> list[str]:
    match video.codec:
        case "copy":
            return ["-c:v", "copy"]
        case "libx265":
            args = ["-c:v", "libx265", "-b:v", f"{bitrate_kbps}k", "-preset", video.preset]
            args.extend(ten_bit_args(video))
            x265 = x265_params(video)
            if x265:
                args.extend(["-x265-params", x265])
            return args
        case "libx264":
            return [
                "-c:v", "libx264", "-b:v", f"{bitrate_kbps}k", "-preset", video.preset,
                *h264_8bit_pix_fmt_args(video),
                *ten_bit_args(video),
            ]
        case "libsvtav1":
            args = ["-c:v", "libsvtav1", "-b:v", f"{bitrate_kbps}k", "-preset", video.preset]
            args.extend(ten_bit_args(video))
            if video.extra_params:
                args.extend(["-svtav1-params", video.extra_params])
            return args
        case "hevc_nvenc":
            return [
                "-c:v", "hevc_nvenc", "-b:v", f"{bitrate_kbps}k", "-preset:v", nvenc_effective_preset(video),
                *nvenc_device_args(callbacks),
                *ten_bit_args(video),
                *nvenc_safe_extra_args(video),
                *hw_extra_args(video),
            ]
        case "hevc_amf":
            args = ["-c:v", "hevc_amf", "-b:v", f"{bitrate_kbps}k"]
            if video.preset:
                args.extend(["-quality", video.preset])
            args.extend(ten_bit_args(video))
            args.extend(hw_extra_args(video))
            return args
        case "hevc_qsv":
            args = ["-c:v", "hevc_qsv", "-b:v", f"{bitrate_kbps}k", "-async_depth", "4"]
            if video.preset:
                args.extend(["-preset", video.preset])
            args.extend(ten_bit_args(video))
            args.extend(hw_extra_args(video))
            return args
        case "hevc_vaapi":
            return [
                "-c:v", "hevc_vaapi", "-rc_mode", "VBR", "-b:v", f"{bitrate_kbps}k",
                "-compression_level", (video.preset or "4"),
                "-async_depth", "4",
                *ten_bit_args(video),
                *hw_extra_args(video),
            ]
        case "h264_nvenc":
            return [
                "-c:v", "h264_nvenc", "-b:v", f"{bitrate_kbps}k", "-preset:v", nvenc_effective_preset(video),
                *nvenc_device_args(callbacks),
                *h264_8bit_pix_fmt_args(video),
                *hw_extra_args(video),
            ]
        case "h264_amf":
            args = ["-c:v", "h264_amf", "-b:v", f"{bitrate_kbps}k"]
            if video.preset:
                args.extend(["-quality", video.preset])
            args.extend(h264_8bit_pix_fmt_args(video))
            args.extend(hw_extra_args(video))
            return args
        case "h264_qsv":
            args = ["-c:v", "h264_qsv", "-b:v", f"{bitrate_kbps}k", "-async_depth", "4"]
            if video.preset:
                args.extend(["-preset", video.preset])
            args.extend(h264_8bit_pix_fmt_args(video))
            args.extend(hw_extra_args(video))
            return args
        case "h264_vaapi":
            return [
                "-c:v", "h264_vaapi", "-rc_mode", "VBR", "-b:v", f"{bitrate_kbps}k",
                "-compression_level", (video.preset or "4"),
                "-async_depth", "4",
                *h264_8bit_pix_fmt_args(video),
                *hw_extra_args(video),
            ]
        case "av1_nvenc":
            return [
                "-c:v", "av1_nvenc", "-b:v", f"{bitrate_kbps}k", "-preset:v", nvenc_effective_preset(video),
                *nvenc_device_args(callbacks),
                *ten_bit_args(video),
                *hw_extra_args(video),
            ]
        case "av1_amf":
            args = ["-c:v", "av1_amf", "-b:v", f"{bitrate_kbps}k"]
            if video.preset:
                args.extend(["-quality", video.preset])
            args.extend(ten_bit_args(video))
            args.extend(hw_extra_args(video))
            return args
        case "av1_qsv":
            args = ["-c:v", "av1_qsv", "-b:v", f"{bitrate_kbps}k", "-async_depth", "4"]
            if video.preset:
                args.extend(["-preset", video.preset])
            args.extend(ten_bit_args(video))
            args.extend(hw_extra_args(video))
            return args
        case "av1_vaapi":
            return [
                "-c:v", "av1_vaapi", "-rc_mode", "VBR", "-b:v", f"{bitrate_kbps}k",
                "-compression_level", (video.preset or "4"),
                "-async_depth", "4",
                *ten_bit_args(video),
                *hw_extra_args(video),
            ]
        case _:
            return ["-c:v", video.codec, "-b:v", f"{bitrate_kbps}k"]


_RESIZE_PRESETS: dict[str, tuple[int, int, str]] = {
    "720p": (1280, 720, "720p"),
    "1080p": (1920, 1080, "1080p"),
    "1440p": (2560, 1440, "1440p"),
    "2160p": (3840, 2160, "2160p"),
}

_DEBLOCK_PRESETS: dict[str, tuple[str, float, float, float, float]] = {
    "ultralight": ("weak", 0.04, 0.03, 0.02, 0.02),
    "light": ("weak", 0.06, 0.04, 0.03, 0.03),
    "medium": ("strong", 0.08, 0.05, 0.04, 0.04),
    "strong": ("strong", 0.10, 0.06, 0.05, 0.05),
    "stronger": ("strong", 0.12, 0.07, 0.06, 0.05),
    "verystrong": ("strong", 0.14, 0.08, 0.07, 0.06),
}

_NLMEANS_PRESETS: dict[str, tuple[float, int, int]] = {
    "ultralight": (1.0, 3, 7),
    "light": (2.0, 5, 9),
    "medium": (3.0, 7, 11),
    "strong": (5.0, 7, 15),
}

_CHROMA_PRESETS: dict[str, tuple[int, int]] = {
    "ultralight": (12, 3),
    "light": (18, 5),
    "medium": (24, 5),
    "strong": (30, 7),
    "stronger": (36, 7),
    "verystrong": (42, 9),
}


def _sanitize_scale_algorithm(value: str) -> str:
    algo = str(value or "lanczos").strip().lower()
    return algo if algo in {"fast_bilinear", "bilinear", "bicubic", "neighbor", "area", "lanczos", "spline"} else "lanczos"


def _build_crop_filter(video: VideoEncodeSettings) -> str:
    crop = video.crop
    if not crop.is_active():
        return ""
    top = max(0, int(crop.top))
    bottom = max(0, int(crop.bottom))
    left = max(0, int(crop.left))
    right = max(0, int(crop.right))
    if crop.auto:
        # Autocrop detection is session-side; until detection fills numeric
        # offsets, command generation keeps the source untouched.
        return ""
    if crop.unit == "percent":
        return (
            "crop="
            f"iw*(100-{left}-{right})/100:"
            f"ih*(100-{top}-{bottom})/100:"
            f"iw*{left}/100:"
            f"ih*{top}/100"
        )
    return f"crop=iw-{left}-{right}:ih-{top}-{bottom}:{left}:{top}"


def _build_resize_filter(video: VideoEncodeSettings) -> str:
    resize = video.resize
    if not resize.is_active():
        return ""
    algo = _sanitize_scale_algorithm(resize.algorithm)
    mode = str(resize.mode or "preset").strip().lower()
    if mode == "percent":
        pct = max(1, int(resize.percent or 100))
        if not bool(resize.allow_upscale):
            pct = min(pct, 100)
        return f"scale=trunc(iw*{pct}/100/2)*2:trunc(ih*{pct}/100/2)*2:flags={algo}"
    if mode == "size":
        width = max(2, int(resize.width or 2))
        height = max(2, int(resize.height or 2))
    else:
        width, height, _label = _RESIZE_PRESETS.get(str(resize.preset or "720p"), _RESIZE_PRESETS["720p"])
    if not bool(resize.allow_upscale):
        # Virgule échappée : ffmpeg sépare les filtres sur ',' dans le filtergraph -vf.
        target_w = f"min({width}\\,iw)"
        target_h = f"min({height}\\,ih)"
    else:
        target_w = str(width)
        target_h = str(height)
    if resize.keep_aspect:
        return (
            f"scale={target_w}:{target_h}:"
            f"force_original_aspect_ratio=decrease:force_divisible_by=2:flags={algo}"
        )
    return f"scale={target_w}:{target_h}:flags={algo}"


def _build_filters(video: VideoEncodeSettings) -> list[str]:
    filters = video.filters
    chain: list[str] = []
    if filters.yadif_enabled:
        mode = str(filters.yadif_mode or "send_frame").strip()
        parity = str(filters.yadif_parity or "auto").strip()
        deint = str(filters.yadif_deint or "all").strip()
        chain.append(f"yadif={mode}:{parity}:{deint}")
    crop = _build_crop_filter(video)
    if crop:
        chain.append(crop)
    if filters.deblock_enabled:
        kind, alpha, beta, gamma, delta = _DEBLOCK_PRESETS.get(
            str(filters.deblock_strength or "medium").strip().lower(),
            _DEBLOCK_PRESETS["medium"],
        )
        block = max(4, min(512, int(filters.deblock_block or 8)))
        chain.append(
            f"deblock=filter={kind}:block={block}:"
            f"alpha={alpha}:beta={beta}:gamma={gamma}:delta={delta}"
        )
    if filters.nlmeans_enabled:
        strength, patch, radius = _NLMEANS_PRESETS.get(
            str(filters.nlmeans_strength or "light").strip().lower(),
            _NLMEANS_PRESETS["light"],
        )
        if str(filters.nlmeans_profile or "").strip().lower() in {"grain", "animation"}:
            # ffmpeg nlmeans 's' a un minimum dur de 1.0 (ultralight*0.75=0.75 → erreur).
            strength = max(1.0, strength * 0.75)
        elif str(filters.nlmeans_profile or "").strip().lower() in {"high motion", "highmotion", "sprite"}:
            radius = max(5, radius - 2)
        chain.append(f"nlmeans=s={strength}:p={patch}:r={radius}")
    if filters.chroma_smooth_enabled:
        thres, size = _CHROMA_PRESETS.get(
            str(filters.chroma_smooth_strength or "medium").strip().lower(),
            _CHROMA_PRESETS["medium"],
        )
        chain.append(
            f"chromanr=thres={thres}:sizew={size}:sizeh={size}:"
            "stepw=1:steph=1:distance=manhattan"
        )
    resize = _build_resize_filter(video)
    if resize:
        chain.append(resize)
    return chain


def build_encoder_vf(video: VideoEncodeSettings, *, callbacks: EncodeCodecDomainCallbacks) -> str:
    vf = build_vf(video)
    force_8bit = force_h264_8bit(video)
    force_10bit = force_10bit_active(video)
    software_filtering = bool(vf)
    if video.codec not in VAAPI_VIDEO_CODECS:
        if (
            callbacks.platform == "win32"
            and video.codec in AMF_VIDEO_CODECS
            and callbacks.amf_device is not None
            and (software_filtering or force_8bit or force_10bit)
        ):
            if force_10bit and not force_8bit:
                amf_upload = "format=p010le,hwupload"
            else:
                amf_upload = "format=nv12,hwupload"
            if force_8bit:
                if vf and "format=yuv420p" not in {part.strip() for part in vf.split(",")}:
                    vf = f"{vf},format=yuv420p"
                elif not vf:
                    vf = "format=yuv420p"
            return f"{vf},{amf_upload}" if vf else amf_upload
        if force_8bit:
            force_8bit_filter = "format=yuv420p"
            if vf and force_8bit_filter in {part.strip() for part in vf.split(",")}:
                return vf
            return f"{vf},{force_8bit_filter}" if vf else force_8bit_filter
        return vf
    # VAAPI : besoin d'un hwupload depuis le pix_fmt cible.
    if force_10bit and not force_8bit:
        vaapi_upload = "format=p010,hwupload"
        return f"{vf},{vaapi_upload}" if vf else vaapi_upload
    if software_filtering or force_8bit:
        vaapi_upload = "format=nv12,hwupload"
        return f"{vf},{vaapi_upload}" if vf else vaapi_upload
    return vf


def build_vf(video: VideoEncodeSettings) -> str:
    if video.codec == "copy":
        return ""
    chain: list[str] = []
    if bool(getattr(video, "p5_to_hdr10", False)):
        chain.extend([
            "format=yuv420p10le",
            "hwupload",
            (
                "libplacebo=apply_dolbyvision=true:"
                "color_primaries=bt2020:color_trc=smpte2084:"
                "colorspace=bt2020nc:range=tv:"
                "tonemapping=clip:peak_detect=false:gamut_mode=clip"
            ),
            "hwdownload",
            "format=yuv420p10le",
        ])
    chain.extend(_build_filters(video))
    if not video.tonemap_to_sdr:
        return ",".join(chain)
    algo = video.tonemap_algorithm or "hable"
    chain.append(
        "zscale=transfer=linear:npl=100,"
        "format=gbrpf32le,"
        "zscale=primaries=bt709,"
        f"tonemap=tonemap={algo}:desat=0,"
        "zscale=transfer=bt709:matrix=bt709:range=tv,"
        "format=yuv420p"
    )
    return ",".join(chain)


def hardware_input_args(video: VideoEncodeSettings, *, callbacks: EncodeCodecDomainCallbacks) -> list[str]:
    args: list[str] = []
    if bool(getattr(video, "p5_to_hdr10", False)):
        args.extend([
            "-init_hw_device", "vulkan=mre_dovi",
            "-filter_hw_device", "mre_dovi",
        ])
    tonemap = bool(video.tonemap_to_sdr)
    software_filtering = has_cpu_video_filter(video)
    force_8bit = force_h264_8bit(video)
    force_10bit = force_10bit_active(video)

    if video.codec in VAAPI_VIDEO_CODECS:
        if callbacks.vaapi_device:
            args.extend(["-vaapi_device", callbacks.vaapi_device])
            if not software_filtering and not tonemap and not force_8bit and not force_10bit:
                args.extend(["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi"])
        return args

    if video.codec in QSV_VIDEO_CODECS:
        if callbacks.qsv_device:
            args.extend(["-qsv_device", callbacks.qsv_device])
        if software_filtering or tonemap or force_8bit or force_10bit:
            return args
        args.extend(["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"])
        return args

    if video.codec in AMF_VIDEO_CODECS and callbacks.platform == "win32":
        if callbacks.amf_device:
            args.extend([
                "-init_hw_device", f"d3d11va=mre_amf:{callbacks.amf_device}",
                "-filter_hw_device", "mre_amf",
            ])
        if software_filtering or tonemap or force_8bit or force_10bit:
            return args
        if callbacks.amf_device:
            args.extend([
                "-hwaccel", "d3d11va",
                "-hwaccel_device", "mre_amf",
                "-hwaccel_output_format", "d3d11",
            ])
        else:
            args.extend(["-hwaccel", "d3d11va", "-hwaccel_output_format", "d3d11"])
        return args

    if software_filtering or tonemap or force_8bit or force_10bit:
        return args

    if video.codec in NVENC_VIDEO_CODECS:
        if callbacks.platform == "win32" and callbacks.nvenc_device:
            args.extend(["-hwaccel_device", callbacks.nvenc_device])
        args.extend(["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"])

    return args


def nvenc_device_args(callbacks: EncodeCodecDomainCallbacks) -> list[str]:
    if callbacks.platform != "win32" or not callbacks.nvenc_device:
        return []
    return ["-gpu", callbacks.nvenc_device]


def hdr_meta_args(video: VideoEncodeSettings) -> list[str]:
    if video.codec in ("copy", "libx264", "h264_nvenc", "h264_amf", "h264_qsv", "h264_vaapi"):
        return []
    # VUI tagging — placés en options output (après -c:v) pour qu'ffmpeg les
    # attache au flux encodé et non au décodeur d'entrée. Couvre tous les
    # encoders HEVC/AV1 (libx265, libsvtav1, libaom-av1, hevc_nvenc, av1_nvenc,
    # hevc_amf, av1_amf, hevc_qsv, av1_qsv, hevc_vaapi).
    # `bt2020nc` == `bt2020_ncl` (matrix non-constant luminance, requis HDR10/DV).
    # `-color_range tv` : HDR10/HDR10+/DoVi sont toujours limited range (16-235) ;
    # sans ce flag certains players/TV interprètent en full range → couleurs lavées.
    args = [
        "-color_primaries", "bt2020",
        "-color_trc",       "smpte2084",
        "-colorspace",      "bt2020nc",
        "-color_range",     "tv",
    ]
    # SEI HDR explicites côté hevc_vaapi (default `hdr+a53_cc`, on force pour être
    # robuste si l'utilisateur passe des extra_params qui changeraient le défaut).
    if video.codec == "hevc_vaapi":
        args.extend(["-sei", "+hdr"])
    # IMPORTANT — Limitation ffmpeg (≤ 7.1) :
    # ffmpeg n'expose AUCUNE option globale `-master_display` / `-max_cll` côté
    # output. Les seules voies fiables pour obtenir les SEI MDCV/CLL :
    #   - libx265 : via `-x265-params master-display=...:max-cll=...`
    #     (déjà géré par x265_params() ci-dessus, branche écrite par la
    #     génération `video_codec_args`).
    #   - libsvtav1 : via `-svtav1-params mastering-display=...:content-light=...`
    #     (à implémenter si besoin ; format L décimal, pas ×10000).
    #   - hevc_vaapi : `-sei +hdr` (par défaut) sérialise les side_data HDR.
    #   - hevc_amf / hevc_qsv : support natif via side_data AVFrame déjà
    #     présents sur la source, mais pas de voie CLI fiable pour éditer
    #     manuellement master_display / max_cll.
    #   - hevc_nvenc : selon le build FFmpeg/NVENC, la voie native reste
    #     incomplète ; le workflow garde un fallback bitstream dédié en
    #     dernier recours.
    return args


def needs_hdr_vui(video: VideoEncodeSettings) -> bool:
    """Vrai si la sortie nécessite le tagging VUI bt2020/PQ.

    DoVi P8 RPU et HDR10+ s'appuient sur une base layer correctement taggée
    (bt2020 / smpte2084 / bt2020nc) — sans ces VUI les TV appliquent un
    tone-mapping bt709 incorrect même quand le RPU est ré-injecté ensuite.
    """
    return requests_hdr_metadata(video)


def audio_codec_args(out_idx: int, audio: AudioTrackSettings) -> list[str]:
    args: list[str] = []
    needs_downmix = needs_ac3_51_downmix(audio)
    bitrate_kbps = normalize_audio_bitrate_kbps(
        audio.codec,
        audio.bitrate_kbps,
        audio.input_channels,
        None,
        audio.input_channel_layout,
    )
    match audio.codec:
        case "copy":
            args.extend([f"-c:a:{out_idx}", "copy"])
            if audio.extract_truehd_core:
                args.extend([f"-bsf:a:{out_idx}", "truehd_core"])
        case "aac":
            args.extend([f"-c:a:{out_idx}", "aac", f"-b:a:{out_idx}", f"{bitrate_kbps}k"])
        case "ac3":
            args.extend([f"-c:a:{out_idx}", "ac3", f"-b:a:{out_idx}", f"{bitrate_kbps}k"])
        case "eac3":
            args.extend([f"-c:a:{out_idx}", "eac3", f"-b:a:{out_idx}", f"{bitrate_kbps}k"])
        case "flac":
            args.extend([f"-c:a:{out_idx}", "flac"])
        case _:
            args.extend([f"-c:a:{out_idx}", audio.codec])
    if needs_downmix:
        args.extend([f"-ac:a:{out_idx}", "6", f"-channel_layout:a:{out_idx}", "5.1"])
    return args


def needs_ac3_51_downmix(audio: AudioTrackSettings) -> bool:
    if audio.codec not in {"ac3", "eac3"}:
        return False
    if (audio.input_channels or 0) >= 8:
        return True
    layout = (audio.input_channel_layout or "").lower()
    return "7.1" in layout
