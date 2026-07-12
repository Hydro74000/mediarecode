from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Callable

from core.workflows.common.track_types import TrackTimeOffset
from core.workflows.encode.catalog import supports_dynamic_hdr
from core.workflows.encode.models import EncodeConfig, QualityMode, VideoEncodeSettings
from core.workflows.encode.planning.plan_models import PlannedVideoTrack

_MASTER_DISPLAY_RE = re.compile(r"^G\(\d+,\d+\)B\(\d+,\d+\)R\(\d+,\d+\)WP\(\d+,\d+\)L\(\d+,\d+\)$")
_MAX_CLL_RE = re.compile(r"^\d+,\d+$")


def is_dir_writable(path: Path) -> bool:
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path,
            prefix="muxiveo_write_probe_",
            delete=True,
        ):
            pass
        return True
    except OSError:
        return False


def validate_encode_config(
    config: EncodeConfig,
    *,
    planned_video_tracks: tuple[PlannedVideoTrack, ...] | None = None,
    video_tracks: list[VideoEncodeSettings] | None = None,
    video_source_from_settings: Callable[[EncodeConfig, VideoEncodeSettings], Path] | None = None,
    dir_writable: Callable[[Path], bool] = is_dir_writable,
) -> list[str]:
    errors: list[str] = []
    if not config.source.is_file():
        errors.append(f"Fichier source introuvable : {config.source}")
    if planned_video_tracks is None:
        if video_tracks is None or video_source_from_settings is None:
            raise ValueError("planned_video_tracks or (video_tracks + video_source_from_settings) is required")
        planned_video_tracks = tuple(
            PlannedVideoTrack(
                source=Path(video_source_from_settings(config, video)),
                stream_index=int(getattr(video, "stream_index", 0) or 0),
                codec=str(video.codec),
                quality_mode=video.quality_mode,
                inject_hdr_meta=bool(video.inject_hdr_meta),
                has_transform=bool(video.has_video_transform()),
                tonemap_to_sdr=bool(video.tonemap_to_sdr),
                copy_dv=bool(video.copy_dv),
                copy_hdr10plus=bool(video.copy_hdr10plus),
                master_display=str(video.master_display or ""),
                max_cll=str(video.max_cll or ""),
                static_hdr_analysis_request=str(
                    video.static_hdr_metadata_analysis_request or ""
                ),
            )
            for video in video_tracks
        )
    if not planned_video_tracks:
        errors.append("Aucune piste vidéo sélectionnée.")
        return errors

    for index, video in enumerate(planned_video_tracks, start=1):
        source = video.source
        if not source.is_file():
            errors.append(f"Piste vidéo #{index} — source introuvable : {source}")
        if video.codec == "copy" and video.has_transform:
            errors.append(
                f"Piste vidéo #{index} — codec copy incompatible avec les transformations vidéo."
            )
        if (video.copy_dv or video.copy_hdr10plus) and not supports_dynamic_hdr(video.codec):
            errors.append(
                f"Piste vidéo #{index} — DoVi/HDR10+ exige un codec compatible."
            )
        analysis_request = str(video.static_hdr_analysis_request or "").strip()
        if analysis_request and (
            len(planned_video_tracks) != 1
            or str(video.codec or "").strip().lower().startswith("nvencc_")
        ):
            errors.append(
                f"Piste vidéo #{index} — l’analyse HDR10 P5→P8.1 intégrée "
                "requiert un pipeline vidéo unique FFmpeg/copy."
            )

    output_dir = config.output.parent
    if not output_dir.exists():
        if not bool(getattr(config, "allow_missing_output_dir", False)):
            errors.append(f"Dossier de sortie inexistant : {output_dir}")
    elif not dir_writable(output_dir):
        errors.append(
            "Dossier de sortie non inscriptible : "
            f"{output_dir} (vérifiez les protections Windows sur les dossiers Bibliothèques)."
        )
    if config.source == config.output:
        errors.append("Le fichier de sortie doit être différent du fichier source.")
    if any(video.quality_mode == QualityMode.SIZE and video.codec != "copy" for video in planned_video_tracks) and not (config.duration_s or 0) > 0:
        errors.append("Durée du fichier source inconnue — mode taille cible impossible.")

    for index, video in enumerate(planned_video_tracks, start=1):
        if video.inject_hdr_meta and not video.tonemap_to_sdr:
            if video.master_display and not _MASTER_DISPLAY_RE.match(video.master_display.strip()):
                errors.append(
                    f"Piste vidéo #{index} — format master_display invalide. "
                    "Attendu : G(x,y)B(x,y)R(x,y)WP(x,y)L(max,min)"
                )
            if video.max_cll and not _MAX_CLL_RE.match(video.max_cll.strip()):
                errors.append(
                    f"Piste vidéo #{index} — format MaxCLL invalide. Attendu : MaxCLL,MaxFALL  ex. 1000,400"
                )

    for raw in config.track_time_offsets:
        if not isinstance(raw, TrackTimeOffset):
            continue
        track_type = str(raw.track_type or "").strip().lower()
        if track_type == "video" and int(raw.offset_ms) < 0:
            errors.append(
                "Décalage vidéo négatif interdit : "
                f"source={Path(raw.source_path)}, stream={int(raw.stream_index)}, "
                f"offset={int(raw.offset_ms)} ms"
            )
    return errors
