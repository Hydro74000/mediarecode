from __future__ import annotations

import json
import subprocess
from pathlib import Path

from core.bluray import ffprobe_input_args, title_for_playlist, is_bluray_playlist
from core.subprocess_utils import subprocess_text_kwargs


def ffmeta_escape(value: str) -> str:
    text = str(value).replace("\\", "\\\\")
    text = text.replace("\n", " ")
    text = text.replace(";", "\\;").replace("#", "\\#").replace("=", "\\=")
    return text


def probe_media_duration_seconds(ffprobe_bin: str, source: Path) -> float | None:
    if is_bluray_playlist(source):
        title = title_for_playlist(source)
        if title is not None and title.duration_s > 0:
            return title.duration_s

    cmd = [
        ffprobe_bin,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
    ]
    cmd.extend(ffprobe_input_args(source))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=20,
            **subprocess_text_kwargs(),
        )
    except FileNotFoundError:
        return None

    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "{}")
        raw = (payload.get("format") or {}).get("duration")
        if raw is None:
            return None
        value = float(raw)
        return value if value > 0 else None
    except Exception:
        pass
    return None


def write_ffmetadata_chapters(
    entries: list,
    out_dir: Path,
    duration_s: float | None,
) -> Path:
    sorted_entries = sorted(entries, key=lambda e: float(getattr(e, "timecode_s", 0.0)))

    if duration_s is None:
        duration_s = max((float(getattr(e, "timecode_s", 0.0)) for e in sorted_entries), default=0.0) + 1.0
    total_ms = max(1, int(round(duration_s * 1000.0)))

    lines: list[str] = [";FFMETADATA1"]
    for idx, chapter in enumerate(sorted_entries):
        start_ms = max(0, int(round(float(getattr(chapter, "timecode_s", 0.0)) * 1000.0)))
        if idx + 1 < len(sorted_entries):
            end_ms = max(start_ms + 1, int(round(float(getattr(sorted_entries[idx + 1], "timecode_s", 0.0)) * 1000.0)))
        else:
            end_ms = max(start_ms + 1, total_ms)

        lines.extend([
            "",
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={start_ms}",
            f"END={end_ms}",
            f"title={ffmeta_escape(str(getattr(chapter, 'name', '') or ''))}",
        ])

    ffmeta_path = out_dir / "chapters.ffmetadata"
    ffmeta_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ffmeta_path
