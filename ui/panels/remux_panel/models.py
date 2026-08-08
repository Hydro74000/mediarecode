"""ui/panels/remux_panel/models.py — modèle de données et helpers purs."""

from __future__ import annotations

import colorsys
import re
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from core.inspector import FileInfo, fmt_timecode_display
from core.media_info_fetcher import clean_text_for_search, extract_year_from_text

# Hauteurs fixes pour les éléments de la liste de fichiers
_FILE_ROW_H = 52
_FILE_BAR_H = 36
_FILE_PH_H = 100

_TRACK_INFO_OFFSET_VALUE_ROLE = int(Qt.ItemDataRole.UserRole) + 40
_TRACK_INFO_SYNC_LABEL_ROLE = int(Qt.ItemDataRole.UserRole) + 41
_TRACK_INFO_DISABLED_LABEL_ROLE = int(Qt.ItemDataRole.UserRole) + 42
_TRACK_INFO_OFFSET_NEG_COLOR = QColor("#d92f2f")
_TRACK_INFO_OFFSET_POS_COLOR = QColor("#1f9d55")
_TRACK_INFO_OFFSET_COLOR = _TRACK_INFO_OFFSET_NEG_COLOR

_TC_RE = re.compile(r"^(\d+):(\d{2}):(\d{2})(?:[.,](\d+))?$")


def _pick_file_color(index: int) -> str:
    hue = (index * 137.508) % 360
    r, g, b = colorsys.hls_to_rgb(hue / 360, 0.62, 0.70)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


def _parse_timecode(tc: str) -> float | None:
    m = _TC_RE.match(tc.strip())
    if not m:
        return None
    h = int(m.group(1))
    mn = int(m.group(2))
    sc = int(m.group(3))
    frac_str = m.group(4) or ""
    frac = float("0." + frac_str) if frac_str else 0.0
    return h * 3600 + mn * 60 + sc + frac


def _format_timecode(seconds: float) -> str:
    return fmt_timecode_display(seconds)


def _normalize_tmdb_manual_title_suggestion(title: str) -> str:
    raw = (title or "").strip()
    if not raw:
        return ""

    cleaned = clean_text_for_search(raw)
    if not cleaned:
        return raw

    year = extract_year_from_text(raw)
    if year and not re.search(r"\b" + re.escape(year) + r"\b", cleaned):
        cleaned = f"{cleaned} {year}"
    return cleaned.strip()


@dataclass
class SourceFile:
    id: str
    path: Path
    color: str = ""
    info: FileInfo | None = None
    tracks: list = field(default_factory=list)


__all__ = [
    "SourceFile",
    "_FILE_BAR_H",
    "_FILE_PH_H",
    "_FILE_ROW_H",
    "_TRACK_INFO_DISABLED_LABEL_ROLE",
    "_TRACK_INFO_OFFSET_COLOR",
    "_TRACK_INFO_OFFSET_NEG_COLOR",
    "_TRACK_INFO_OFFSET_POS_COLOR",
    "_TRACK_INFO_OFFSET_VALUE_ROLE",
    "_TRACK_INFO_SYNC_LABEL_ROLE",
    "_format_timecode",
    "_normalize_tmdb_manual_title_suggestion",
    "_parse_timecode",
    "_pick_file_color",
]
