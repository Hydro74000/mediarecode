from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from core.bluray import append_ffmpeg_input_args, ffprobe_input_args
from core.runner import TaskCancelledError, TaskSignals
from core.subprocess_utils import subprocess_text_kwargs
from core.workflows.common.attachments import attachment_filename_from_meta
from core.workflows.encode.models import EncodeConfig, EncodeError


def probe_attachment_stream(
    source: Path,
    stream_idx: int,
    *,
    ffprobe_bin: str,
    subprocess_run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    text_kwargs_factory: Callable[[], dict[str, object]] = subprocess_text_kwargs,
) -> dict[str, object]:
    """Retourne les métadonnées minimales d'un stream potentiellement attachment."""
    cmd = [
        ffprobe_bin,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
    ]
    cmd.extend(ffprobe_input_args(source))
    try:
        result = subprocess_run(
            cmd,
            capture_output=True,
            check=False,
            timeout=30,
            **text_kwargs_factory(),
        )
    except FileNotFoundError:
        return {
            "is_attached_pic": False,
            "filename": f"attachment_{stream_idx}.bin",
            "mimetype": "application/octet-stream",
        }

    if result.returncode != 0:
        return {
            "is_attached_pic": False,
            "filename": f"attachment_{stream_idx}.bin",
            "mimetype": "application/octet-stream",
        }

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        payload = {}

    for stream in payload.get("streams", []):
        if int(stream.get("index", -1)) != int(stream_idx):
            continue
        tags = stream.get("tags", {}) or {}
        disposition = stream.get("disposition", {}) or {}
        return {
            "is_attached_pic": bool(disposition.get("attached_pic", 0)),
            "filename": tags.get("filename") or f"attachment_{stream_idx}.bin",
            "mimetype": tags.get("mimetype") or "application/octet-stream",
        }

    return {
        "is_attached_pic": False,
        "filename": f"attachment_{stream_idx}.bin",
        "mimetype": "application/octet-stream",
    }


def unique_attachment_path(tmp_dir: Path, filename: str) -> Path:
    """Retourne un chemin unique dans ``tmp_dir`` pour éviter les collisions."""
    candidate = tmp_dir / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for idx in range(1, 1000):
        alt = tmp_dir / f"{stem}_{idx}{suffix}"
        if not alt.exists():
            return alt
    return tmp_dir / f"{stem}_{os.getpid()}{suffix}"


def extract_attached_pic(
    source: Path,
    stream_idx: int,
    dest: Path,
    *,
    ffmpeg_bin: str,
    ffmpeg_thread_args: Callable[[], list[str]],
    check_cancelled: Callable[[TaskSignals | None], None],
    log_info: Callable[[str], None],
    run_cmd: Callable[[list[str], str, TaskSignals | None], object],
    signals: TaskSignals | None = None,
) -> None:
    """Extrait un ``attached_pic`` vers un vrai fichier image."""
    check_cancelled(signals)
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-y",
    ]
    append_ffmpeg_input_args(cmd, source)
    cmd.extend([
        "-map",
        f"0:{stream_idx}",
        *ffmpeg_thread_args(),
        "-c",
        "copy",
        "-frames:v",
        "1",
        str(dest),
    ])
    log_info("$ " + " ".join(cmd))
    try:
        run_cmd(cmd, "extract-attached-pic", signals)
    except TaskCancelledError:
        dest.unlink(missing_ok=True)
        raise
    except Exception as exc:
        dest.unlink(missing_ok=True)
        stderr = str(exc).strip()
        raise EncodeError(
            f"Extraction attachment échouée pour le stream {stream_idx} de {source.name}: {stderr}"
        ) from exc
    check_cancelled(signals)
    if not dest.exists():
        raise EncodeError(
            f"Extraction attachment échouée pour le stream {stream_idx} de {source.name}: fichier absent"
        )


def extract_attachment_data(
    source: Path,
    stream_idx: int,
    dest: Path,
    *,
    ffmpeg_bin: str,
    ffmpeg_thread_args: Callable[[], list[str]],
    check_cancelled: Callable[[TaskSignals | None], None],
    log_info: Callable[[str], None],
    run_cmd: Callable[[list[str], str, TaskSignals | None], object],
    signals: TaskSignals | None = None,
) -> None:
    """Extract a Matroska attachment stream as its original byte payload."""
    check_cancelled(signals)
    cmd = [ffmpeg_bin, "-hide_banner", "-y"]
    append_ffmpeg_input_args(cmd, source)
    cmd.extend([
        "-map", f"0:{stream_idx}", *ffmpeg_thread_args(),
        "-c", "copy", "-f", "data", str(dest),
    ])
    log_info("$ " + " ".join(cmd))
    try:
        run_cmd(cmd, "extract-attachment", signals)
    except TaskCancelledError:
        dest.unlink(missing_ok=True)
        raise
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise EncodeError(
            f"Extraction attachment échouée pour le stream {stream_idx} de {source.name}: {str(exc).strip()}"
        ) from exc
    check_cancelled(signals)
    if not dest.is_file():
        raise EncodeError(
            f"Extraction attachment échouée pour le stream {stream_idx} de {source.name}: fichier absent"
        )


@dataclass(frozen=True)
class AttachmentPreparationServiceCallbacks:
    check_cancelled: Callable[[TaskSignals | None], None]
    describe_attachment_stream: Callable[[Path, int], dict[str, object]]
    attachment_filename: Callable[[dict[str, object], int], str]
    unique_attachment_path: Callable[[Path, str], Path]
    extract_attached_pic: Callable[[Path, int, Path, TaskSignals | None], None]
    extract_attachment_data: Callable[[Path, int, Path, TaskSignals | None], None] | None = None


class AttachmentPreparationService:
    """Prépare les attachments avant le runtime encode."""

    def __init__(self, callbacks: AttachmentPreparationServiceCallbacks) -> None:
        self._callbacks = callbacks

    def prepare(
        self,
        config: EncodeConfig,
        *,
        work_dir: Path,
        signals: TaskSignals | None = None,
    ) -> tuple[EncodeConfig, Path | None]:
        cb = self._callbacks
        if not config.attachment_streams:
            return config, None

        tmp_dir = Path(tempfile.mkdtemp(prefix="enc_attachments_", dir=str(work_dir)))
        direct_streams: list = []
        extracted_files: list[Path] = []
        created_any = False

        try:
            for selection in config.attachment_streams:
                cb.check_cancelled(signals)
                src_path, stream_idx = selection[:2]
                meta = cb.describe_attachment_stream(src_path, stream_idx)
                cb.check_cancelled(signals)
                # Le muxeur interne reçoit des fichiers d'attachement, pas
                # des streams FFmpeg. En mode natif on matérialise donc aussi
                # les fonts et pièces jointes ordinaires.
                # Toujours matérialiser : le même résultat sert au muxeur
                # interne comme au chemin FFmpeg et évite qu'un choix ``auto``
                # soit arrêté avant la sélection réelle du backend.

                created_any = True
                filename = cb.attachment_filename(meta, stream_idx)
                dest = cb.unique_attachment_path(tmp_dir, filename)
                if meta["is_attached_pic"]:
                    cb.extract_attached_pic(src_path, stream_idx, dest, signals)
                else:
                    if cb.extract_attachment_data is None:
                        raise EncodeError("Extraction d'attachment natif non configurée")
                    cb.extract_attachment_data(src_path, stream_idx, dest, signals)
                cb.check_cancelled(signals)
                extracted_files.append(dest)

            if not created_any:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return config, None

            prepared = replace(
                config,
                attachment_streams=direct_streams,
                extra_attachments=[*extracted_files, *config.extra_attachments],
            )
            return prepared, tmp_dir
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise


def default_attachment_filename(meta: dict[str, object], stream_idx: int) -> str:
    return attachment_filename_from_meta(meta, stream_idx)
