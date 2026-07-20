"""Attachment and NFO helpers for the remux workflow."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from core.inspector import AttachmentInfo
from core.runner import TaskCancelledError, TaskSignals
from core.subprocess_utils import subprocess_text_kwargs
from core.workflows.common.attachments import ATTACHMENT_EXT_BY_MIME, sanitize_filename
from core.workflows.remux_models import RemuxConfig, RemuxError


def write_mediainfo_nfo(
    output_path: Path,
    log_cb: Callable[[str, str], None],
    mediainfo_bin: str = "mediainfo",
    *,
    run_cmd: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    output_path = Path(output_path).expanduser()
    if not output_path.is_absolute():
        output_path = output_path.resolve()
    nfo_path = output_path.with_suffix(".nfo")
    try:
        result = run_cmd(
            [mediainfo_bin, str(output_path)],
            capture_output=True,
            **subprocess_text_kwargs(),
        )
        cleaned_lines: list[str] = []
        changed = False
        for line in result.stdout.splitlines(keepends=True):
            line_body = line.rstrip("\r\n")
            newline = line[len(line_body):]
            if ":" not in line_body:
                cleaned_lines.append(line)
                continue

            key, value = line_body.split(":", 1)
            current = value.strip()
            if (
                key.strip().casefold() == "complete name"
                and current != output_path.name
                and ("/" in current or "\\" in current)
            ):
                suffix_start = len(value) - len(value.lstrip())
                cleaned_lines.append(f"{key}:{value[:suffix_start]}{output_path.name}{newline}")
                changed = True
            else:
                cleaned_lines.append(line)

        nfo_text = "".join(cleaned_lines) if changed else result.stdout
        nfo_path.write_text(nfo_text, encoding="utf-8")
        log_cb("OK", f"NFO généré : {nfo_path.name}")
    except Exception as exc:
        log_cb("WARN", f"Impossible de générer le NFO : {exc}")


def build_attachment_mapping(config: RemuxConfig) -> list[str]:
    args: list[str] = []
    for input_idx, source in enumerate(config.sources):
        for attachment in sorted(source.selected_attachments, key=lambda item: item.local_index):
            if attachment.is_attached_pic:
                continue
            args.extend(["-map", f"{input_idx}:{attachment.index}"])
    if args:
        args.extend(["-c:t", "copy"])
        args.extend(["-map_metadata:s:t", "-1"])
    return args


def attachment_names(attachment: AttachmentInfo) -> tuple[str, str]:
    raw_name = sanitize_filename(attachment.filename, f"attachment_{attachment.index}")
    if not Path(raw_name).suffix:
        mime = (attachment.mimetype or "").strip().lower()
        ext = ATTACHMENT_EXT_BY_MIME.get(mime, ".bin")
        raw_name = f"{raw_name}{ext}"
    return raw_name, raw_name


def extract_attached_pics(
    config: RemuxConfig,
    tmp_dir: Path,
    signals: TaskSignals,
    *,
    ffmpeg_bin: str,
    cli_path: Callable[[Path | str], str],
    log_cb: Callable[[str, str], None],
) -> list[Path]:
    paths: list[Path] = []
    # Dédoublonnage déterministe, insensible à la casse : identique sur tout
    # OS (Path.exists() seul divergerait entre Linux et Windows/macOS) et
    # répliqué à l'identique par le contrat de validation.
    used_names: set[str] = set()
    for source in config.sources:
        for attachment in sorted(source.selected_attachments, key=lambda item: item.local_index):
            if not attachment.is_attached_pic:
                continue
            if signals._cancel_event.is_set():
                raise TaskCancelledError()

            raw_name = sanitize_filename(attachment.filename, f"attachment_{attachment.index}")
            suffix = Path(raw_name).suffix.lower()
            if not suffix:
                suffix = ATTACHMENT_EXT_BY_MIME.get((attachment.mimetype or "").strip().lower(), ".jpg")
            stem = Path(raw_name).stem or f"attachment_{attachment.index}"
            out_name = f"{stem}{suffix}"
            counter = 1
            while out_name.casefold() in used_names or (tmp_dir / out_name).exists():
                out_name = f"{stem}_{counter}{suffix}"
                counter += 1
            used_names.add(out_name.casefold())
            out_path = tmp_dir / out_name

            cmd = [
                ffmpeg_bin, "-hide_banner", "-y",
                "-i", cli_path(source.path),
                "-map", f"0:{attachment.index}",
                "-threads", "1",
                "-frames:v", "1",
                cli_path(out_path),
            ]
            log_cb("INFO", "$ " + " ".join(str(token) for token in cmd))
            result = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                timeout=60,
                **subprocess_text_kwargs(),
            )
            if result.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
                stderr = (result.stderr or "").strip()
                raise RemuxError(
                    f"Extraction attached_pic échouée (source={source.path.name}, stream={attachment.index}): {stderr}"
                )
            paths.append(out_path)
    return paths


__all__ = [
    "attachment_names",
    "build_attachment_mapping",
    "extract_attached_pics",
    "write_mediainfo_nfo",
]
