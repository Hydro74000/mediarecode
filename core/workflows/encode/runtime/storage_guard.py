from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from core.workflows.encode.models import EncodeConfig, EncodeError, QualityMode, normalize_audio_bitrate_kbps


def format_bytes(value: int) -> str:
    size = float(max(0, value))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    idx = 0
    while size >= 1024.0 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    return f"{size:.1f} {units[idx]}"


def estimate_duration_seconds(
    config: EncodeConfig,
    *,
    probe_duration_seconds: Callable[[Path], float | None],
) -> float:
    duration_s = config.duration_s
    if duration_s is not None and duration_s > 0:
        return float(duration_s)
    probed = probe_duration_seconds(config.source)
    if probed is not None and probed > 0:
        return float(probed)
    return 3600.0


def estimate_inject_video_bytes(
    config: EncodeConfig,
    *,
    duration_s: float,
    source_size: int,
    size_to_bitrate_kbps: Callable[[EncodeConfig], int],
) -> int:
    video = config.video
    if video is None:
        raise EncodeError("Configuration vidéo absente pour l'estimation d'injection")
    if video.quality_mode == QualityMode.SIZE:
        video_kbps = size_to_bitrate_kbps(config)
        return int((video_kbps * 1000 / 8) * duration_s)
    if video.quality_mode == QualityMode.BITRATE:
        video_kbps = max(1, int(video.bitrate_kbps or 1))
        return int((video_kbps * 1000 / 8) * duration_s)
    return max(source_size, int(source_size * 3 / 4))


def estimate_inject_storage_requirements(
    config: EncodeConfig,
    *,
    probe_duration_seconds: Callable[[Path], float | None],
    size_to_bitrate_kbps: Callable[[EncodeConfig], int],
) -> tuple[int, int]:
    """
    Retourne (work_required_bytes, output_required_bytes) pour le chemin injection.
    """
    source_size = max(0, config.source.stat().st_size if config.source.exists() else 0)
    duration_s = estimate_duration_seconds(
        config,
        probe_duration_seconds=probe_duration_seconds,
    )
    video_bytes = max(
        128 * 1024 * 1024,
        estimate_inject_video_bytes(
            config,
            duration_s=duration_s,
            source_size=source_size,
            size_to_bitrate_kbps=size_to_bitrate_kbps,
        ),
    )

    video = config.video
    if video is None:
        raise EncodeError("Configuration vidéo absente pour l'estimation de stockage")

    sidecars = 32 * 1024 * 1024
    if video.copy_dv:
        sidecars += 64 * 1024 * 1024
    if video.copy_hdr10plus:
        sidecars += 64 * 1024 * 1024
    if config.chapter_overrides:
        sidecars += 16 * 1024 * 1024

    # Pic du pipeline lot 3 avec squelette de timing : au plus deux copies
    # vidéo coexistent (MKV encodé + annexB à l'extraction, puis annexB
    # courant + injecté, puis injecté + MKV réécrit) ; le squelette (payloads
    # vides) est couvert par un forfait fixe.
    skeleton_allowance = 64 * 1024 * 1024
    work_required = (2 * video_bytes) + skeleton_allowance + sidecars

    encoded_audio_bytes = 0
    for audio in config.audio_tracks:
        if audio.codec == "copy":
            continue
        if audio.codec == "flac":
            kbps = max(1000, int(audio.bitrate_kbps or 0))
        else:
            kbps = normalize_audio_bitrate_kbps(
                audio.codec,
                audio.bitrate_kbps,
                audio.input_channels,
                None,
                audio.input_channel_layout,
            )
        encoded_audio_bytes += int((kbps * 1000 / 8) * duration_s)

    extra_attachments_bytes = sum(
        max(0, Path(p).stat().st_size)
        for p in config.extra_attachments
        if Path(p).exists()
    )
    output_required = max(
        source_size,
        video_bytes + encoded_audio_bytes + extra_attachments_bytes,
    ) + (64 * 1024 * 1024)
    return work_required, output_required


def ensure_inject_storage_available(
    config: EncodeConfig,
    *,
    estimate_requirements: Callable[[EncodeConfig], tuple[int, int]],
    log_info: Callable[[str], None],
    ram_buffer_enabled: bool,
    ram_buffer_dir: Callable[[], Path | None],
    disk_usage: Callable[[str | bytes | os.PathLike[str] | os.PathLike[bytes]], shutil._ntuple_diskusage] = shutil.disk_usage,
    stat: Callable[[str | bytes | os.PathLike[str] | os.PathLike[bytes]], os.stat_result] = os.stat,
    temp_dir: Callable[[], str] = tempfile.gettempdir,
    format_bytes_fn: Callable[[int], str] = format_bytes,
) -> None:
    """Vérifie l'espace libre avant le chemin d'injection metadata vidéo."""
    work_required, output_required = estimate_requirements(config)

    work_root = config.work_dir or Path(temp_dir())
    work_root.mkdir(parents=True, exist_ok=True)
    output_root = config.output.parent
    output_root.mkdir(parents=True, exist_ok=True)

    work_free = disk_usage(work_root).free
    output_free = disk_usage(output_root).free

    log_info(
        "Estimation espace injection: "
        f"temp≈{format_bytes_fn(work_required)} sur {work_root} ; "
        f"sortie≈{format_bytes_fn(output_required)} sur {output_root}."
    )

    if ram_buffer_enabled and ram_buffer_dir() is not None:
        log_info("Buffer RAM actif: estimation disque conservative (fallback disque).")

    same_fs = False
    try:
        same_fs = stat(work_root).st_dev == stat(output_root).st_dev
    except OSError:
        same_fs = False

    if same_fs:
        required = work_required + output_required
        free = min(work_free, output_free)
        if free < required:
            raise EncodeError(
                "Espace disque insuffisant pour l'injection metadata vidéo "
                f"(requis≈{format_bytes_fn(required)}, libre≈{format_bytes_fn(free)} "
                f"sur {output_root})."
            )
        return

    if work_free < work_required:
        raise EncodeError(
            "Espace disque insuffisant pour les temporaires d'injection metadata "
            f"(requis≈{format_bytes_fn(work_required)}, "
            f"libre≈{format_bytes_fn(work_free)} sur {work_root})."
        )
    if output_free < output_required:
        raise EncodeError(
            "Espace disque insuffisant pour le fichier de sortie "
            f"(requis≈{format_bytes_fn(output_required)}, "
            f"libre≈{format_bytes_fn(output_free)} sur {output_root})."
        )
