"""
ui/main_window.py — Fenêtre principale de Muxiveo.

Architecture :
    ┌────────────────────────────────────────────────────────────┐
    │  MainWindow (QMainWindow)                               │
    │  ┌──────────┬────────────────────────────────────────┐  │
    │  │ Sidebar  │  QScrollArea                           │  │
    │  │          │   └─ QStackedWidget (pages)            │  │
    │  │          │      ─ DashboardPage      (index 0)    │  │
    │  │          │      ─ MergeDoviPanel     (index 1)    │  │
    │  │          │      ─ EncodePanel        (index 2)    │  │
    │  │          │      ─ RemuxPanel         (index 3)    │  │
    │  │          │      ─ SettingsPanel      (index 4)    │  │
    │  └──────────┴────────────────────────────────────────┘  │
    │  ┌────────────────────────────────────────────────────┐  │
    │  │ Action bar globale : état, progression, exécuter, │  │
    │  │ annuler                                            │  │
    │  └────────────────────────────────────────────────────┘  │
    │  ┌────────────────────────────────────────────────────┐  │
    │  │ LogPanel global (INFO / OK / WARN / ERROR)        │  │
    │  └────────────────────────────────────────────────────┘  │
    └────────────────────────────────────────────────────────────┘

Liaisons principales :
    - la sidebar pilote l'index du QStackedWidget
    - MergeDoviPanel / EncodePanel / RemuxPanel poussent leurs logs vers LogPanel
    - RemuxPanel partage ses pistes et métadonnées de sortie avec EncodePanel
    - SettingsPanel notifie MainWindow quand la configuration est sauvegardée

Signal exposé :
    MainWindow.log_requested(level: str, message: str)
        → point d'entrée global pour envoyer un log vers l'interface
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable, TYPE_CHECKING, cast

from PySide6.QtCore import QEvent, QObject, Qt, Signal, QTimer
from PySide6.QtGui import (
    QColor, QFont, QTextCharFormat, QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QSplitter, QStackedWidget, QTextEdit,
    QVBoxLayout, QWidget,
)

from core.config import AppConfig
from core.bluray import discover_titles, find_disc_root
from core.file_types import is_accepted
from core.i18n import apply_translations, set_current_language, translate_text
from core.logging import LogLevel, VerboseFileLogger, parse_log_level
from core.runner import TaskSignals, _PCT_SENTINEL
from core.subprocess_utils import subprocess_text_kwargs
from core.version import APP_VERSION_LABEL, WRITING_APPLICATION_TAG
from core.workflows.encode.backends import backend_id_for_codec
from core.workflows.encode import EncodeError
from core.workflows.common.sync_rewrite import SYNC_REWRITE_STAGE_PREFIX
from core.workflows.remux_models import RemuxError
from ui.panels.encode_panel import EncodePanel
from ui.panels.encode_panel.theme import (
    _FPS_RE,
    _FRAME_RE,
    EtaTracker,
    _fmt_eta,
    ffmpeg_progress_seconds,
)
from ui.panels.merge_dovi_panel import MergeDoviPanel
from ui.panels.remux_panel import RemuxPanel
from ui.panels.settings_panel import SettingsPanel
from ui.design_system import DesignSystem, colors as _Colors, font_px as _font_px, scale as _scale

if TYPE_CHECKING:
    from core.workflows.encode.models import EncodeConfig
    from core.workflows.remux_models import RemuxConfig


# ---------------------------------------------------------------------------
# Niveaux de log
# ---------------------------------------------------------------------------


def _level_color(level: LogLevel) -> str:
    if level == LogLevel.OK:
        return _Colors.LOG_OK
    if level == LogLevel.WARN:
        return _Colors.LOG_WARN
    if level == LogLevel.ERROR:
        return _Colors.LOG_ERROR
    return _Colors.LOG_INFO

_LEVEL_LABELS: dict[LogLevel, str] = {
    LogLevel.INFO:  " INFO ",
    LogLevel.OK:    "  OK  ",
    LogLevel.WARN:  " WARN ",
    LogLevel.ERROR: " ERR  ",
}

_ENCODE_STAGE_PREFIXES: tuple[str, ...] = (
    "Extraction HEVC source",
    "Extraction HEVC annexB",
    "Extraction RPU Dolby Vision",
    "Extraction métadonnées HDR10+",
    "Conversion P5 → P8",
    "Conversion P7 FEL → P8.1",
    "Conversion P7 MEL → P8.1",
    "Encodage vidéo",
    "Encodage NVEncC",
    "Injection métadonnées HDR10+",
    "Injection RPU Dolby Vision",
    "Encapsulation vidéo injectée",
    "Reconstitution finale",
    "Injection balises MKV",
    "Écriture balises MKV",
)

# Étapes pilotées par des outils sans progression ffmpeg parsable
# (dovi_tool, hdr10plus_tool, patch EBML…). Pendant ces étapes, on
# anime la barre en mode indéterminé pour montrer que ça travaille.
_ENCODE_INDETERMINATE_STAGE_PREFIXES: tuple[str, ...] = (
    "Extraction RPU Dolby Vision",
    "Extraction métadonnées HDR10+",
    "Conversion P5 → P8",
    "Conversion P7 FEL → P8.1",
    "Conversion P7 MEL → P8.1",
    "Injection métadonnées HDR10+",
    "Injection RPU Dolby Vision",
    "Injection balises MKV",
    "Écriture balises MKV",
)


def _is_encode_indeterminate_stage(line: str) -> bool:
    return any(line.startswith(prefix) for prefix in _ENCODE_INDETERMINATE_STAGE_PREFIXES)

_ENCODE_PROGRESS_NOISE_PREFIXES: tuple[str, ...] = (
    "frame=",
    "fps=",
    "stream_",
    "bitrate=",
    "total_size=",
    "out_time=",
    "out_time_ms=",
    "out_time_us=",
    "dup_frames=",
    "drop_frames=",
    "speed=",
    "progress=",
)

# Lignes informatives bruyantes ffmpeg (banner, listing streams, chapitres,
# metadata side-data) qu'on n'affiche PAS dans le LogPanel UI mais qu'on
# laisse passer au verbose file logger via _capture_verbose_progress_line.
# Sans ce filtre, charger un MKV avec 45 sous-titres pollue l'UI de
# centaines de lignes "Stream #0:N(xxx): Subtitle: ..." à chaque appel
# ffmpeg (extract annexB, encode, remux final, etc.).
_FFMPEG_INFO_PREFIXES: tuple[str, ...] = (
    "ffmpeg version ",
    "  built with ",
    "  configuration: ",
    "  libavutil",
    "  libavcodec",
    "  libavformat",
    "  libavdevice",
    "  libavfilter",
    "  libswscale",
    "  libswresample",
    "  libpostproc",
    "Input #",
    "Output #",
    "Stream mapping:",
    "Press [q] to stop",
    "  Stream #",
    "  Duration:",
    "  Metadata:",
    "  Chapters:",
    "    Chapter #",
    "    Stream #",
    "    Metadata:",
    "      Metadata:",
    "      Side data:",
    "        ",
    "      title           :",
    "      encoder         :",
    "      creation_time   :",
    "      Source          :",
    "[matroska,",
    "[mov,",
    "[mp4 @",
    "[hevc @",
    "[h264 @",
    "Consider increasing the value for the 'analyzeduration'",
)


def _is_ffmpeg_info_noise(line: str) -> bool:
    """Vrai pour les lignes ffmpeg purement informatives (banner, streams,
    metadata side-data) qu'on ne veut pas afficher dans le LogPanel UI.

    Ne filtre PAS les warnings/errors réels (lignes ne matchant aucun préfixe
    sont laissées passer au LogPanel pour visibilité utilisateur).
    """
    return any(line.startswith(prefix) for prefix in _FFMPEG_INFO_PREFIXES)
_STEP_PROGRESS_RE = re.compile(r"^STEP\s+\d+\s*-\s*(.+)$")
_ENCODE_INTERNAL_PROGRESS_PREFIX = "__MRE_PROGRESS__ "
_MULTI_ENCODE_LABEL_RE = re.compile(r"^ffmpeg-video-(\d+)(?:-pass(\d+))?$")


def _is_encode_stage_message(line: str) -> bool:
    return any(line.startswith(prefix) for prefix in _ENCODE_STAGE_PREFIXES)


def _is_encode_progress_noise(line: str) -> bool:
    return any(line.startswith(prefix) for prefix in _ENCODE_PROGRESS_NOISE_PREFIXES)


def _fmt_progress_percent(percent: float | int) -> str:
    value = float(percent)
    if abs(value - round(value)) < 0.05:
        return f"{int(round(value))}%"
    return f"{value:.1f}%"


def _parse_encode_internal_progress(line: str) -> dict[str, object] | None:
    if not line.startswith(_ENCODE_INTERNAL_PROGRESS_PREFIX):
        return None
    payload = line[len(_ENCODE_INTERNAL_PROGRESS_PREFIX):].strip()
    if not payload:
        return None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _parse_sync_rewrite_stage_progress(line: str) -> tuple[str, str] | None:
    if not line.startswith(SYNC_REWRITE_STAGE_PREFIX):
        return None
    payload = line[len(SYNC_REWRITE_STAGE_PREFIX):].strip()
    if not payload:
        return None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    track_type = str(obj.get("track_type") or "").strip().lower()
    name = " ".join(str(obj.get("name") or "").split())
    if track_type not in {"audio", "subtitle"} or not name:
        return None
    return track_type, name


def _sync_rewrite_stage_label(track_type: str, name: str) -> str:
    if track_type == "audio":
        return translate_text("Synchro piste audio {name}", name=name)
    return translate_text("Synchro sous-titre {name}", name=name)


def _parse_multi_encode_label(label: str) -> tuple[int, int | None, int] | None:
    match = _MULTI_ENCODE_LABEL_RE.match(str(label).strip())
    if match is None:
        return None
    order = int(match.group(1))
    pass_index_raw = match.group(2)
    if pass_index_raw is None:
        return order, None, 1
    pass_index = int(pass_index_raw)
    return order, pass_index, 2


def _state_float(state: dict[str, object], key: str, default: float = 0.0) -> float:
    value = state.get(key, default)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def _config_file_logging_enabled(config: object) -> bool:
    if hasattr(config, "enable_file_logging"):
        return bool(getattr(config, "enable_file_logging", False))
    return bool(getattr(config, "verbose_file_logging", False))


def _config_file_logging_level(config: object) -> str:
    if hasattr(config, "file_logging_level"):
        raw = str(getattr(config, "file_logging_level", "")).strip().lower()
        if raw in {"standard", "verbose"}:
            return raw
        return "standard"
    return "verbose" if bool(getattr(config, "verbose_file_logging", False)) else "standard"


def _config_file_logging_is_verbose(config: object) -> bool:
    return _config_file_logging_enabled(config) and _config_file_logging_level(config) == "verbose"


def _ensure_verbose_file_logger(window: object) -> VerboseFileLogger:
    logger = getattr(window, "_verbose_file_logger", None)
    if isinstance(logger, VerboseFileLogger):
        logger.configure(
            app_data_dir=Path(getattr(window, "_config").app_data_dir),
            verbose_log_dir=getattr(getattr(window, "_config"), "verbose_log_dir", None),
            enabled=_config_file_logging_enabled(getattr(window, "_config")),
        )
        return logger

    def _on_write_error() -> None:
        log_panel = getattr(window, "_log_panel", None)
        if log_panel is not None:
            log_panel.log(
                "Impossible d'écrire le fichier de logging.",
                LogLevel.WARN,
            )

    logger = VerboseFileLogger(
        app_data_dir=Path(getattr(window, "_config").app_data_dir),
        verbose_log_dir=getattr(getattr(window, "_config"), "verbose_log_dir", None),
        enabled=_config_file_logging_enabled(getattr(window, "_config")),
        on_write_error=_on_write_error,
    )
    setattr(window, "_verbose_file_logger", logger)
    return logger


def _multi_encode_remaining_seconds(state: dict[str, object], now: float) -> float | None:
    """
    ETA d'un encode multi-stream basé sur la vitesse lissée (EWMA), pas sur
    la moyenne depuis le début. Évite l'ETA gonflé pendant l'init de ffmpeg.
    """
    tracker = state.get("eta_tracker")
    if not isinstance(tracker, EtaTracker):
        tracker = EtaTracker(warmup_progress=1.0)
        state["eta_tracker"] = tracker

    duration_s = state.get("duration_s")
    elapsed_video = state.get("elapsed_video")
    if (
        isinstance(duration_s, (int, float))
        and duration_s > 0
        and isinstance(elapsed_video, (int, float))
        and elapsed_video >= 0
    ):
        tracker.update(float(elapsed_video), now)
        return tracker.eta(float(duration_s), float(elapsed_video))

    total_frames = state.get("total_frames")
    frame = state.get("frame")
    fps_value = state.get("fps")
    if (
        isinstance(total_frames, int)
        and total_frames > 0
        and isinstance(frame, int)
        and frame > 0
        and total_frames > frame
    ):
        # Si ffmpeg a déjà fourni un fps, on l'utilise tel quel — c'est le
        # même chiffre que l'utilisateur voit dans le label, donc cohérent.
        if isinstance(fps_value, (int, float)) and fps_value > 0:
            return tracker.eta_from_speed(float(total_frames), float(frame), float(fps_value))
        tracker.update(float(frame), now)
        return tracker.eta(float(total_frames), float(frame))
    return None


def _select_multi_encode_label(
    states: dict[str, dict[str, object]],
    active_label: str | None,
    now: float,
) -> str | None:
    candidates = [
        (label, state)
        for label, state in states.items()
        if not bool(state.get("done", False))
    ]
    if not candidates:
        return None

    scored: list[tuple[int, float, float, int, str]] = []
    for label, state in candidates:
        remaining_s = _multi_encode_remaining_seconds(state, now)
        known_remaining = 1 if remaining_s is not None else 0
        scored.append(
            (
                known_remaining,
                float(remaining_s if remaining_s is not None else -1.0),
                _state_float(state, "last_update", 0.0),
                1 if label == active_label else 0,
                label,
            )
        )
    scored.sort(reverse=True)
    return scored[0][4]

# ---------------------------------------------------------------------------
# LogPanel
# ---------------------------------------------------------------------------

class _LogHeader(QWidget):
    """Barre d'en-tête cliquable du panneau de logs."""
    clicked = Signal()

    def mousePressEvent(self, event) -> None:
        self.clicked.emit()
        super().mousePressEvent(event)


class LogPanel(QWidget):
    """
    Panneau de logs à niveaux colorés.

    Usage :
        panel.log("Vérification des dépendances...", LogLevel.INFO)
        panel.log("Toutes les dépendances sont présentes.", LogLevel.OK)
        panel.log("Différence de 2 frames tolérée.", LogLevel.WARN)
        panel.log("Fichier introuvable.", LogLevel.ERROR)
    """

    collapse_toggled = Signal(bool)   # True = collapsed

    def __init__(self, max_lines: int = 2000, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._max_lines = max_lines
        self._collapsed = False
        self._build_ui()
        # Buffer batching : ffmpeg/nvenc peut émettre 200+ lignes/s. Insérer
        # chaque ligne dans le QTextEdit bloque l'event loop. On accumule et
        # on flushe via QTimer pour garder l'UI réactive.
        self._pending: list[tuple[str, LogLevel, str]] = []
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(50)
        self._flush_timer.timeout.connect(self._flush_pending)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # En-tête cliquable
        header = _LogHeader()
        header.setFixedHeight(_scale(32))
        header.setCursor(Qt.CursorShape.PointingHandCursor)
        header.setStyleSheet(f"""
            _LogHeader {{
                background: {_Colors.BG_PANEL};
                border-top: 1px solid {_Colors.BORDER};
                border-bottom: 1px solid {_Colors.BORDER};
            }}
        """)
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(_scale(12), 0, _scale(8), 0)
        h_layout.setSpacing(_scale(8))

        title = QLabel("LOGS")
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        title.setStyleSheet(f"""
            color: {_Colors.TEXT_DIM};
            font-size: {_font_px(10)}px;
            font-weight: 700;
            letter-spacing: {_scale(2)}px;
            background: transparent;
            border: none;
        """)
        h_layout.addWidget(title)
        h_layout.addStretch()

        # Bouton collapse (▲/▼)
        self._collapse_btn = QPushButton("▲")
        self._collapse_btn.setFixedSize(_scale(20), _scale(20))
        self._collapse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._collapse_btn.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._collapse_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {_Colors.TEXT_DIM};
                border: none;
                font-size: {_font_px(10)}px;
                padding: 0;
            }}
        """)
        h_layout.addWidget(self._collapse_btn)

        # Bouton clear
        clear_btn = QPushButton("Effacer")
        clear_btn.setFixedHeight(_scale(20))
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {_Colors.TEXT_DIM};
                border: 1px solid {_Colors.BORDER};
                border-radius: 3px;
                font-size: {_font_px(10)}px;
                padding: 0 {_scale(8)}px;
            }}
            QPushButton:hover {{
                color: {_Colors.TEXT_SEC};
                border-color: {_Colors.BORDER_LT};
            }}
        """)
        clear_btn.clicked.connect(self.clear)
        h_layout.addWidget(clear_btn)

        header.clicked.connect(self._toggle)
        layout.addWidget(header)

        # Zone de texte
        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._text.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        mono = QFont("JetBrains Mono", _font_px(9))
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._text.setFont(mono)
        self._text.setStyleSheet(f"""
            QTextEdit {{
                background: {_Colors.LOG_BG};
                color: {_Colors.TEXT_PRI};
                border: none;
                padding: {_scale(8)}px {_scale(12)}px;
                selection-background-color: {_Colors.ACCENT_DIM};
            }}
            QScrollBar:vertical {{
                background: {_Colors.BG_DEEP};
                width: {_scale(8)}px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {_Colors.BORDER_LT};
                border-radius: {_scale(4)}px;
                min-height: {_scale(20)}px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
        """)
        self._text.setMinimumHeight(0)
        layout.addWidget(self._text)

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def log(self, message: str, level: LogLevel = LogLevel.INFO) -> None:
        """Met une ligne de log en file, flush via QTimer pour ne pas bloquer l'UI."""
        ts = datetime.now().strftime("%H:%M:%S")
        self._pending.append((ts, level, translate_text(message)))
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _flush_pending(self) -> None:
        if not self._pending:
            return
        batch = self._pending
        self._pending = []

        cursor = self._text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        ts_fmt = QTextCharFormat()
        ts_fmt.setForeground(QColor(_Colors.LOG_TS))
        label_fmt = QTextCharFormat()
        label_fmt.setFontWeight(QFont.Weight.Bold)
        msg_fmt = QTextCharFormat()
        msg_fmt.setFontWeight(QFont.Weight.Normal)

        # setUpdatesEnabled(False) évite des repaint intermédiaires sur le batch.
        self._text.setUpdatesEnabled(False)
        try:
            for ts, level, message in batch:
                color = _level_color(level)
                label = _LEVEL_LABELS[level]
                cursor.insertText(f"{ts}  ", ts_fmt)
                label_fmt.setForeground(QColor(color))
                cursor.insertText(label, label_fmt)
                msg_fmt.setForeground(
                    QColor(color if level != LogLevel.INFO else _Colors.TEXT_PRI)
                )
                cursor.insertText(f"  {message}\n", msg_fmt)

            self._text.setTextCursor(cursor)

            doc = self._text.document()
            excess = doc.blockCount() - self._max_lines
            if excess > 0:
                cur = QTextCursor(doc)
                cur.movePosition(QTextCursor.MoveOperation.Start)
                for _ in range(excess):
                    cur.select(QTextCursor.SelectionType.BlockUnderCursor)
                    cur.removeSelectedText()
                    cur.deleteChar()
        finally:
            self._text.setUpdatesEnabled(True)
        self._text.ensureCursorVisible()

    def info(self, message: str)  -> None: self.log(message, LogLevel.INFO)
    def ok(self, message: str)    -> None: self.log(message, LogLevel.OK)
    def warn(self, message: str)  -> None: self.log(message, LogLevel.WARN)
    def error(self, message: str) -> None: self.log(message, LogLevel.ERROR)

    def clear(self) -> None:
        self._pending.clear()
        self._flush_timer.stop()
        self._text.clear()

    def is_collapsed(self) -> bool:
        return self._collapsed

    def set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = bool(collapsed)
        self._text.setVisible(not self._collapsed)
        self._collapse_btn.setText("▼" if self._collapsed else "▲")

    def _toggle(self) -> None:
        self.set_collapsed(not self._collapsed)
        self.collapse_toggled.emit(self._collapsed)


# ---------------------------------------------------------------------------
# Page placeholder (remplacée en Phase 2 par des pages réelles)
# ---------------------------------------------------------------------------

class _PlaceholderPage(QWidget):
    def __init__(self, title: str, icon: str, description: str) -> None:
        super().__init__()
        self.setStyleSheet(f"background: {_Colors.BG_DEEP};")
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(_scale(16))

        icon_lbl = QLabel(icon)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setStyleSheet(f"font-size: {_font_px(48)}px; color: {_Colors.TEXT_DIM}; background: transparent;")
        layout.addWidget(icon_lbl)

        title_lbl = QLabel(title)
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl.setStyleSheet(f"""
            font-size: {_font_px(18)}px;
            font-weight: 700;
            color: {_Colors.TEXT_PRI};
            background: transparent;
            letter-spacing: {_scale(1)}px;
        """)
        layout.addWidget(title_lbl)

        desc_lbl = QLabel(description)
        desc_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc_lbl.setWordWrap(True)
        desc_lbl.setMaximumWidth(_scale(400))
        desc_lbl.setStyleSheet(f"color: {_Colors.TEXT_SEC}; font-size: {_font_px(12)}px; background: transparent;")
        layout.addWidget(desc_lbl)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class DashboardPage(QWidget):
    """Page d'accueil — résumé des outils disponibles et raccourcis."""

    _hw_detected = Signal(object)   # set[str] — encodeurs HW disponibles

    # codec_id → (label affiché, badge QLabel) pour mise à jour async
    _HW_VIDEO: list[tuple[str, str]] = [
        ("hevc_nvenc", "NVENC·HEVC"), ("hevc_amf", "AMF·HEVC"), ("hevc_vaapi", "VAAPI·HEVC"), ("hevc_qsv", "QSV·HEVC"),
        ("h264_nvenc", "NVENC·H264"), ("h264_amf", "AMF·H264"), ("h264_vaapi", "VAAPI·H264"), ("h264_qsv", "QSV·H264"),
        ("av1_nvenc",  "NVENC·AV1"),  ("av1_amf",  "AMF·AV1"),  ("av1_vaapi",  "VAAPI·AV1"),  ("av1_qsv",  "QSV·AV1"),
    ]
    # Encodeurs NVEncC (rigaya) — wrapper standalone NVIDIA. Affichés sur une
    # ligne distincte pour bien indiquer qu'ils utilisent un binaire séparé.
    _HW_VIDEO_NVENCC: list[tuple[str, str]] = [
        ("nvencc_hevc", "NVEncC·HEVC"),
        ("nvencc_h264", "NVEncC·H264"),
        ("nvencc_av1",  "NVEncC·AV1"),
    ]
    _SW_VIDEO: list[tuple[str, str]] = [
        ("libx265", "x265"), ("libx264", "x264"), ("libsvtav1", "SVT-AV1"),
    ]
    _AUDIO: list[tuple[str, str]] = [
        ("aac", "AAC"), ("eac3", "EAC-3"), ("flac", "FLAC"), ("ac3", "AC-3"), ("libopus", "Opus"),
    ]

    def __init__(self, config: AppConfig, log: Callable[[str, LogLevel], None]) -> None:
        super().__init__()
        self._config = config
        self._log = log
        self._hw_badges: dict[str, tuple[QLabel, str]] = {}   # codec_id → (badge, label)
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._hw_detected.connect(self._on_hw_detected, Qt.ConnectionType.QueuedConnection)
        self._build_ui()
        self._start_hw_detection()

    def _build_ui(self) -> None:
        self.setStyleSheet(f"background: {_Colors.BG_DEEP};")
        root = QVBoxLayout(self)
        root.setContentsMargins(_scale(32), _scale(32), _scale(32), _scale(32))
        root.setSpacing(_scale(24))
        root.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Titre
        title = QLabel("Muxiveo")
        title.setStyleSheet(f"""
            font-size: {_font_px(26)}px;
            font-weight: 800;
            color: {_Colors.TEXT_PRI};
            background: transparent;
            letter-spacing: -{_scale(1)}px;
        """)
        root.addWidget(title)

        subtitle = QLabel("Manipulation, encodage et injection de métadonnées HDR")
        subtitle.setStyleSheet(f"color: {_Colors.TEXT_SEC}; font-size: {_font_px(13)}px; background: transparent;")
        root.addWidget(subtitle)

        # Séparateur
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {_Colors.BORDER};")
        root.addWidget(sep)

        # Statut des outils
        status_title = QLabel("Outils disponibles")
        status_title.setStyleSheet(f"""
            font-size: {_font_px(11)}px;
            font-weight: 700;
            color: {_Colors.TEXT_DIM};
            letter-spacing: {_scale(2)}px;
            background: transparent;
        """)
        root.addWidget(status_title)

        tools_grid = QWidget()
        tools_grid.setStyleSheet("background: transparent;")
        grid_layout = QHBoxLayout(tools_grid)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setSpacing(_scale(8))
        grid_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)

        availability = self._config.all_tools_available()
        for tool_name, available in availability.items():
            badge = self._make_tool_badge(tool_name, available)
            grid_layout.addWidget(badge)

        root.addWidget(tools_grid)

        # Vérification manuelle
        check_btn = QPushButton("↻  Vérifier les outils")
        check_btn.setFixedWidth(_scale(200))
        check_btn.setFixedHeight(_scale(34))
        check_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        check_btn.setStyleSheet(f"""
            QPushButton {{
                background: {_Colors.BG_CARD};
                color: {_Colors.TEXT_SEC};
                border: 1px solid {_Colors.BORDER};
                border-radius: {_scale(6)}px;
                font-size: {_font_px(12)}px;
                font-weight: 600;
                padding: 0 {_scale(16)}px;
            }}
            QPushButton:hover {{
                background: {_Colors.BG_HOVER};
                color: {_Colors.TEXT_PRI};
                border-color: {_Colors.BORDER_LT};
            }}
            QPushButton:pressed {{
                background: {_Colors.BG_ACTIVE};
            }}
        """)
        check_btn.clicked.connect(self._check_tools)
        root.addWidget(check_btn)

        self._build_encoder_section(root)

        root.addStretch()

        # Infos de chemins
        paths_title = QLabel("Chemins configurés")
        paths_title.setStyleSheet(f"""
            font-size: {_font_px(11)}px;
            font-weight: 700;
            color: {_Colors.TEXT_DIM};
            letter-spacing: {_scale(2)}px;
            background: transparent;
        """)
        root.addWidget(paths_title)

        for label, value in [
            ("Dossier travail", self._config.work_dir),
            ("Dossier sortie",  self._config.output_dir),
            ("App data",        self._config.app_data_dir),
        ]:
            row = QWidget()
            row.setStyleSheet("background: transparent;")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(_scale(12))

            key_lbl = QLabel(label)
            key_lbl.setFixedWidth(_scale(110))
            key_lbl.setStyleSheet(
                f"color: {_Colors.TEXT_SEC}; font-size: {_font_px(11)}px; background: transparent;"
            )

            val_lbl = QLabel(str(value))
            val_lbl.setStyleSheet(f"""
                color: {_Colors.TEXT_DIM};
                font-size: {_font_px(11)}px;
                font-family: 'JetBrains Mono', monospace;
                background: transparent;
            """)
            rl.addWidget(key_lbl)
            rl.addWidget(val_lbl)
            rl.addStretch()
            root.addWidget(row)

    def _make_tool_badge(self, name: str, available: bool) -> QLabel:
        color  = _Colors.LOG_OK  if available else _Colors.LOG_ERROR
        bg     = _Colors.BADGE_OK_BG if available else _Colors.BADGE_ERROR_BG
        border = _Colors.BADGE_OK_BORDER if available else _Colors.BADGE_ERROR_BORDER
        symbol = "●"             if available else "○"
        lbl = QLabel(f" {symbol}  {name} ")
        lbl.setStyleSheet(f"""
            QLabel {{
                background: {bg};
                color: {color};
                border: 1px solid {border};
                border-radius: {_scale(4)}px;
                font-size: {_font_px(11)}px;
                font-family: 'JetBrains Mono', monospace;
                padding: {_scale(3)}px {_scale(8)}px;
            }}
        """)
        return lbl

    # ------------------------------------------------------------------
    # Section encodeurs
    # ------------------------------------------------------------------

    def _build_encoder_section(self, root: QVBoxLayout) -> None:
        """Ajoute la section encodeurs au layout root. Détection SW synchrone, HW asynchrone."""
        self._hw_badges = {}
        ffmpeg_bin = self._config.tool_ffmpeg

        # Détection synchrone des encodeurs logiciels via ffmpeg -encoders
        all_sw_ids = [c for c, _ in self._SW_VIDEO] + [c for c, _ in self._AUDIO]
        sw_avail = self._scan_encoder_availability(ffmpeg_bin, all_sw_ids)

        # Séparateur + titre
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {_Colors.BORDER};")
        root.addWidget(sep)

        section_title = QLabel("ENCODEURS DISPONIBLES")
        section_title.setStyleSheet(f"""
            font-size: {_font_px(11)}px; font-weight: 700;
            color: {_Colors.TEXT_DIM}; letter-spacing: {_scale(2)}px; background: transparent;
        """)
        root.addWidget(section_title)

        def _row(sub_label: str) -> tuple[QWidget, QHBoxLayout]:
            row = QWidget()
            row.setStyleSheet("background: transparent;")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(_scale(8))
            lbl = QLabel(sub_label)
            lbl.setFixedWidth(_scale(130))
            lbl.setStyleSheet(
                f"color:{_Colors.TEXT_SEC};font-size:{_font_px(11)}px;background:transparent;"
            )
            rl.addWidget(lbl)
            return row, rl

        # Vidéo logiciel
        row, rl = _row("Vidéo — logiciel")
        for codec_id, label in self._SW_VIDEO:
            state = "available" if sw_avail.get(codec_id) else "unavailable"
            rl.addWidget(self._make_encoder_badge(label, state))
        rl.addStretch()
        root.addWidget(row)

        # Vidéo matériel — ligne 1 : HEVC + H.264
        row, rl = _row("Vidéo — matériel")
        for codec_id, label in self._HW_VIDEO[:8]:
            badge = self._make_encoder_badge(label, "pending")
            self._hw_badges[codec_id] = (badge, label)
            rl.addWidget(badge)
        rl.addStretch()
        root.addWidget(row)

        # Vidéo matériel — ligne 2 : AV1
        row, rl = _row("  ↳ AV1")
        for codec_id, label in self._HW_VIDEO[8:]:
            badge = self._make_encoder_badge(label, "pending")
            self._hw_badges[codec_id] = (badge, label)
            rl.addWidget(badge)
        rl.addStretch()
        root.addWidget(row)

        # Vidéo matériel — ligne 3 : NVEncC (rigaya)
        row, rl = _row("  ↳ NVEncC")
        for codec_id, label in self._HW_VIDEO_NVENCC:
            badge = self._make_encoder_badge(label, "pending")
            self._hw_badges[codec_id] = (badge, label)
            rl.addWidget(badge)
        rl.addStretch()
        root.addWidget(row)

        # Audio
        row, rl = _row("Audio")
        for codec_id, label in self._AUDIO:
            state = "available" if sw_avail.get(codec_id) else "unavailable"
            rl.addWidget(self._make_encoder_badge(label, state))
        rl.addStretch()
        root.addWidget(row)

    def _make_encoder_badge(self, label: str, state: str) -> QLabel:
        """Crée un badge d'encodeur. state ∈ {"available", "unavailable", "pending"}."""
        badge = QLabel()
        self._apply_encoder_badge_state(badge, label, state)
        return badge

    @staticmethod
    def _scan_encoder_availability(ffmpeg_bin: str, codec_ids: list[str]) -> dict[str, bool]:
        """Retourne l'état de disponibilité des codecs présents dans `ffmpeg -encoders`."""
        import shutil
        resolved = shutil.which(ffmpeg_bin) or ffmpeg_bin
        try:
            result = subprocess.run(
                [resolved, "-hide_banner", "-encoders"],
                capture_output=True,
                check=False,
                **subprocess_text_kwargs(),
            )
            encoders_output = "\n".join(part for part in (result.stdout, result.stderr) if part)
            return {
                codec_id: bool(re.search(rf"\b{re.escape(codec_id)}\b", encoders_output))
                for codec_id in codec_ids
            }
        except FileNotFoundError:
            return {codec_id: False for codec_id in codec_ids}

    def _apply_encoder_badge_state(self, badge: QLabel, label: str, state: str) -> None:
        if state == "available":
            symbol, color, bg, border = "●", _Colors.LOG_OK, _Colors.BADGE_OK_BG, _Colors.BADGE_OK_BORDER
        elif state == "unavailable":
            symbol, color, bg, border = "○", _Colors.LOG_ERROR, _Colors.BADGE_ERROR_BG, _Colors.BADGE_ERROR_BORDER
        else:  # pending
            symbol, color, bg, border = "…", _Colors.TEXT_DIM, _Colors.BADGE_PENDING_BG, _Colors.BADGE_PENDING_BORDER
        badge.setText(f" {symbol}  {label} ")
        badge.setStyleSheet(f"""
            QLabel {{
                background: {bg}; color: {color};
                border: 1px solid {border}; border-radius: {_scale(4)}px;
                font-size: {_font_px(11)}px; font-family: 'JetBrains Mono', monospace;
                padding: {_scale(3)}px {_scale(8)}px;
            }}
        """)

    # ------------------------------------------------------------------
    # Détection asynchrone des encodeurs matériels
    # ------------------------------------------------------------------

    def _start_hw_detection(self) -> None:
        """Remet les badges HW en état "pending" et soumet la détection à l'executor."""
        for codec_id, (badge, label) in self._hw_badges.items():
            self._apply_encoder_badge_state(badge, label, "pending")
        self._executor.submit(self._run_hw_detection)

    def _run_hw_detection(self) -> None:
        """Thread worker : probe runtime de chaque encodeur HW."""
        from core.workflows.encode import HardwareEncoderDetector
        detector = HardwareEncoderDetector()
        ffmpeg = self._config.tool_ffmpeg
        nvencc = getattr(self._config, "tool_nvencc", None) or None
        result = detector.detect(ffmpeg, nvencc_bin=nvencc)
        # Compatibilité : certains tests/mocks retournent encore un set simple.
        if isinstance(result, tuple):
            available = result[0]
        else:
            available = result
        self._hw_detected.emit(available)

    def _on_hw_detected(self, available: set[str]) -> None:
        """Slot Qt (thread principal) : met à jour les badges HW."""
        for codec_id, (badge, label) in self._hw_badges.items():
            state = "available" if codec_id in available else "unavailable"
            self._apply_encoder_badge_state(badge, label, state)

    # ------------------------------------------------------------------
    # Vérification manuelle des outils
    # ------------------------------------------------------------------

    def _check_tools(self) -> None:
        self._log("Vérification des outils...", LogLevel.INFO)
        availability = self._config.all_tools_available()
        all_ok = True
        for name, available in availability.items():
            if available:
                self._log(f"{name} — trouvé", LogLevel.OK)
            else:
                self._log(f"{name} — introuvable", LogLevel.WARN)
                all_ok = False
        if all_ok:
            self._log("Tous les outils sont disponibles.", LogLevel.OK)
        else:
            self._log("Certains outils sont manquants. Vérifiez votre PATH ou les paramètres.", LogLevel.WARN)
        # Rafraîchit l'affichage des badges + relance la détection HW
        self._build_ui()
        apply_translations(self)
        self._start_hw_detection()


# ---------------------------------------------------------------------------
# Bouton de navigation sidebar
# ---------------------------------------------------------------------------

class _NavButton(QWidget):
    """Bouton sidebar avec icône à largeur fixe et label aligné."""

    clicked = Signal()

    def __init__(
        self,
        label: str,
        icon_char: str,
        page_index: int,
        is_sub: bool = False,
        *,
        compact: bool = False,
    ) -> None:
        super().__init__()
        self.page_index = page_index
        self._checked  = False
        self._is_sub   = is_sub
        self._compact  = compact
        self._icon_char = icon_char
        self._full_height = _scale(36 if is_sub else 40)
        self._compact_height = max(self._full_height * 2, _scale(76))
        self.setFixedHeight(self._compact_height if compact else self._full_height)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip(label)
        self.setAccessibleName(label)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._stack = QStackedWidget()
        self._stack.setStyleSheet("background: transparent; border: none;")
        root.addWidget(self._stack)

        full_page = QWidget()
        full_page.setStyleSheet("background: transparent; border: none;")
        full_lay = QHBoxLayout(full_page)
        full_lay.setContentsMargins(_scale(28 if is_sub else 14), 0, _scale(14), 0)
        full_lay.setSpacing(_scale(10))

        self._full_icon_lbl = QLabel(icon_char)
        self._full_icon_lbl.setFixedWidth(_scale(20))
        self._full_icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._full_icon_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self._text_lbl = QLabel(label)
        self._text_lbl.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self._text_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        full_lay.addWidget(self._full_icon_lbl)
        full_lay.addWidget(self._text_lbl)
        full_lay.addStretch()
        self._stack.addWidget(full_page)

        compact_page = QWidget()
        compact_page.setStyleSheet("background: transparent; border: none;")
        compact_lay = QHBoxLayout(compact_page)
        compact_lay.setContentsMargins(0, 0, 0, 0)
        compact_lay.setSpacing(0)
        self._compact_icon_lbl = QLabel(icon_char)
        self._compact_icon_lbl.setFixedWidth(_scale(64))
        self._compact_icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._compact_icon_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        compact_lay.addStretch()
        compact_lay.addWidget(self._compact_icon_lbl)
        compact_lay.addStretch()
        self._stack.addWidget(compact_page)

        self._stack.setCurrentIndex(1 if self._compact else 0)

        self._update_style(False)

    # ------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        self.clicked.emit()
        super().mousePressEvent(event)

    def setChecked(self, checked: bool) -> None:
        self._checked = checked
        self._update_style(checked)

    def set_compact(self, compact: bool) -> None:
        self._compact = compact
        self._stack.setCurrentIndex(1 if compact else 0)
        self.setFixedHeight(self._compact_height if compact else self._full_height)
        self._update_style(self._checked)

    def isCheckable(self) -> bool:
        return True

    def _update_style(self, checked: bool) -> None:
        if checked:
            bg     = _Colors.BG_ACTIVE
            color  = _Colors.TEXT_PRI
            icon_c = _Colors.ACCENT
            border = f"border-left: {_scale(2)}px solid {_Colors.ACCENT};"
            weight = "600"
        else:
            bg     = "transparent"
            color  = _Colors.TEXT_SEC
            icon_c = _Colors.TEXT_DIM
            border = f"border-left: {_scale(2)}px solid transparent;"
            weight = "400"

        self.setStyleSheet(f"""
            _NavButton {{
                background: {bg};
                {border}
                border-right: none;
                border-top: none;
                border-bottom: none;
            }}
            _NavButton:hover {{
                background: {_Colors.BG_HOVER};
            }}
        """)
        icon_style_full = (
            f"color: {icon_c}; font-size: {_font_px(14)}px; background: transparent; border: none;"
        )
        compact_px = _font_px(32 if self._icon_char == "▶" else 56)
        icon_style_compact = (
            f"color: {icon_c}; font-size: {compact_px}px; background: transparent; border: none;"
        )
        self._full_icon_lbl.setStyleSheet(icon_style_full)
        self._compact_icon_lbl.setStyleSheet(icon_style_compact)
        font_size = f"{_font_px(11 if self._is_sub else 12)}px"
        self._text_lbl.setStyleSheet(
            f"color: {color}; font-size: {font_size}; font-weight: {weight};"
            f" background: transparent; border: none;"
        )


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

class _Sidebar(QWidget):
    page_changed = Signal(int)

    _NAV_ITEMS = [
        ("Tableau de bord", "⌂", 0, False),
        ("Conteneur",       "⊞", 3, False),
        ("Encodage",        "▶", 2, True),    # sous-menu de Conteneur
        ("DoVi / HDR10+",   "◈", 1, False),
        ("Paramètres",      "⚙", 4, False),
    ]
    _FULL_WIDTH = 200
    _COMPACT_WIDTH = 96

    def __init__(self, parent: QWidget | None = None, *, compact: bool = False) -> None:
        super().__init__(parent)
        self._compact = compact
        self.setFixedWidth(_scale(self._COMPACT_WIDTH if compact else self._FULL_WIDTH))
        self.setStyleSheet(f"""
            QWidget {{
                background: {_Colors.BG_SIDEBAR};
                border-right: 1px solid {_Colors.BORDER};
            }}
        """)
        self._buttons: list[_NavButton] = []
        self._build_ui()
        self.set_compact(self._compact)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Logo / App name
        logo_area = QWidget()
        logo_area.setFixedHeight(_scale(56))
        logo_area.setStyleSheet(f"""
            QWidget {{
                background: {_Colors.BG_SIDEBAR};
                border-bottom: 1px solid {_Colors.BORDER};
                border-right: none;
            }}
        """)
        la = QHBoxLayout(logo_area)
        la.setContentsMargins(_scale(12), 0, _scale(8), 0)
        la.setSpacing(_scale(8))

        self._logo_icon = QLabel("▣")
        self._logo_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._logo_icon.setToolTip("Muxiveo")
        self._logo_icon.setStyleSheet(
            f"color: {_Colors.ACCENT}; font-size: {_font_px(18)}px; background: transparent; border: none;"
        )
        la.addWidget(self._logo_icon)
        self._logo_text = QLabel("Muxiveo")
        self._logo_text.setStyleSheet(f"""
            color: {_Colors.TEXT_PRI};
            font-size: {_font_px(13)}px;
            font-weight: 700;
            background: transparent;
            border: none;
            letter-spacing: {_scale(1)}px;
        """)
        la.addWidget(self._logo_text)
        la.addStretch()
        self._toggle_btn = QPushButton("◀")
        self._toggle_btn.setFixedSize(_scale(22), _scale(22))
        self._toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {_Colors.TEXT_DIM};
                border: 1px solid {_Colors.BORDER};
                border-radius: 4px;
                font-size: {_font_px(11)}px;
                padding: 0;
            }}
            QPushButton:hover {{
                color: {_Colors.TEXT_PRI};
                border-color: {_Colors.BORDER_LT};
                background: {_Colors.BG_HOVER};
            }}
        """)
        self._toggle_btn.clicked.connect(self.toggle_compact)
        la.addWidget(self._toggle_btn)
        layout.addWidget(logo_area)

        # Navigation
        self._nav_label = QLabel("NAVIGATION")
        self._nav_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._nav_label.setContentsMargins(_scale(16), _scale(16), 0, _scale(8))
        self._nav_label.setStyleSheet(f"""
            color: {_Colors.TEXT_DIM};
            font-size: {_font_px(9)}px;
            font-weight: 700;
            letter-spacing: {_scale(2)}px;
            background: transparent;
            border: none;
        """)
        layout.addWidget(self._nav_label)

        for label, icon, idx, is_sub in self._NAV_ITEMS:
            btn = _NavButton(label, icon, idx, is_sub, compact=self._compact)
            btn.clicked.connect(lambda b=btn: self._on_nav_click(b))
            self._buttons.append(btn)
            layout.addWidget(btn)

        layout.addStretch()

        # Version
        self._version_lbl = QLabel(APP_VERSION_LABEL)
        self._version_lbl.setToolTip("")
        self._version_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._version_lbl.setContentsMargins(_scale(16), 0, 0, _scale(12))
        self._version_lbl.setStyleSheet(f"""
            color: {_Colors.TEXT_DIM};
            font-size: {_font_px(9)}px;
            background: transparent;
            border: none;
        """)
        layout.addWidget(self._version_lbl)

        # Sélectionner le premier bouton
        if self._buttons:
            self._buttons[0].setChecked(True)

    def _on_nav_click(self, clicked: _NavButton) -> None:
        for btn in self._buttons:
            if btn is not clicked:
                btn.setChecked(False)
        clicked.setChecked(True)
        self.page_changed.emit(clicked.page_index)

    def select_page(self, index: int) -> None:
        for btn in self._buttons:
            btn.setChecked(btn.page_index == index)

    def is_compact(self) -> bool:
        return self._compact

    def toggle_compact(self, _checked: bool = False) -> None:
        self.set_compact(not self._compact)

    def set_compact(self, compact: bool) -> None:
        self._compact = compact
        self.setFixedWidth(_scale(self._COMPACT_WIDTH if compact else self._FULL_WIDTH))
        self._logo_text.setVisible(not compact)
        self._nav_label.setVisible(not compact)
        for btn in self._buttons:
            btn.set_compact(compact)

        if compact:
            self._toggle_btn.setText("▶")
            self._toggle_btn.setToolTip(translate_text("Agrandir le menu"))
            self._version_lbl.setText(APP_VERSION_LABEL.split()[-1])
            self._version_lbl.setToolTip(APP_VERSION_LABEL)
            self._version_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._version_lbl.setContentsMargins(0, 0, 0, _scale(12))
        else:
            self._toggle_btn.setText("◀")
            self._toggle_btn.setToolTip(translate_text("Réduire le menu"))
            self._version_lbl.setText(APP_VERSION_LABEL)
            self._version_lbl.setToolTip("")
            self._version_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self._version_lbl.setContentsMargins(_scale(16), 0, 0, _scale(12))


# ---------------------------------------------------------------------------
# MainWindow
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    """
    Fenêtre principale de l'application.

    Signal :
        log_requested(level: str, message: str)
            Émet un message de log depuis n'importe quel composant.
            Les workers peuvent s'y connecter via Qt.QueuedConnection
            pour poster des logs depuis des threads secondaires.
    """

    log_requested = Signal(str, str)
    WRITING_APPLICATION = WRITING_APPLICATION_TAG
    _PAGE_INDEX_BY_PANEL_KEY = {
        "dashboard": 0,
        "dovi": 1,
        "encoding": 2,
        "container": 3,
        "settings": 4,
    }

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config
        DesignSystem.set_theme(config.theme)
        self._running   = False
        self._signals: TaskSignals | None = None
        self._op_start: float = 0.0
        self._op_mode: str = ""   # "remux", "encode", "extract", "audio_sync" ou "merge_dovi"
        # Trace one-shot du banner ffmpeg : on logge la version la 1re fois
        # vue dans la session (en INFO standard), les suivantes restent
        # silencieuses côté UI (mais visibles dans le verbose file).
        self._ffmpeg_version_logged: bool = False
        self._op_encode_config: EncodeConfig | None = None
        self._op_extract_duration_s: float | None = None
        self._op_encode_fps: float | None = None
        self._op_encode_frame: int | None = None
        # Trackers ETA lissés (EWMA) pour les barres single-stream (remux et
        # encode). Le multi-encode utilise un tracker par label dans le state.
        self._eta_tracker_video = EtaTracker(warmup_progress=1.0)
        self._eta_tracker_frame = EtaTracker(warmup_progress=24.0)
        # Légende de l'étape en cours (« Encodage vidéo… », « Injection RPU
        # Dolby Vision… »…). Mise à jour quand un message d'étape arrive et
        # préfixée aux métriques ffmpeg sur la barre de progression.
        self._op_stage_label: str = ""
        self._op_encode_multi_targets: dict[int, dict[str, object]] = {}
        self._op_encode_multi_state: dict[str, dict[str, object]] = {}
        self._op_encode_multi_active_label: str | None = None
        self._op_encode_multi_reselect_timer = QTimer(self)
        self._op_encode_multi_reselect_timer.setInterval(60_000)
        self._op_encode_multi_reselect_timer.timeout.connect(self._reevaluate_multi_encode_progress)
        self._verbose_file_logger = VerboseFileLogger(
            app_data_dir=Path(self._config.app_data_dir),
            verbose_log_dir=getattr(self._config, "verbose_log_dir", None),
            enabled=_config_file_logging_enabled(self._config),
            on_write_error=lambda: self._log_panel.log(
                "Impossible d'écrire le fichier de logging.",
                LogLevel.WARN,
            ),
        )
        self._prep_progress_active = False
        self._prep_progress_value = 0
        self._prep_progress_direction = 1
        self._prep_progress_timer = QTimer(self)
        self._prep_progress_timer.setInterval(120)
        self._prep_progress_timer.timeout.connect(self._tick_prep_progress)
        self._setup_window()
        self._build_ui()
        self._apply_startup_panel()
        self._restore_geometry()
        self._connect_signals()
        self._apply_locale()
        self._post_init_log()

    # ------------------------------------------------------------------
    # Fenêtre
    # ------------------------------------------------------------------

    def _setup_window(self) -> None:
        self.setWindowTitle("Muxiveo")
        self.setMinimumSize(1024, 680)
        self.resize(1280, 800)

        # Fond global
        self.setStyleSheet(f"""
            QMainWindow {{
                background: {_Colors.BG_DEEP};
            }}
            QSplitter::handle {{
                background: {_Colors.BORDER};
                height: 1px;
            }}
        """)

    # ------------------------------------------------------------------
    # Construction de l'interface
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        writing_application = self.writing_application_tag()

        central = QWidget()
        central.setStyleSheet(f"background: {_Colors.BG_DEEP};")
        self.setCentralWidget(central)

        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── Splitter vertical : (sidebar + stack + action bar) / log panel ──
        self._vsplit = QSplitter(Qt.Orientation.Vertical)
        vsplit = self._vsplit
        vsplit.setHandleWidth(5)
        vsplit.setChildrenCollapsible(False)
        vsplit.setStretchFactor(0, 1)
        vsplit.setStretchFactor(1, 0)

        # Partie haute : sidebar + pages + barre d'action globale
        top_widget = QWidget()
        top_widget.setStyleSheet(f"background: {_Colors.BG_DEEP};")
        top_widget.setMinimumHeight(1)
        top_vbox = QVBoxLayout(top_widget)
        top_vbox.setContentsMargins(0, 0, 0, 0)
        top_vbox.setSpacing(0)

        # Ligne principale : sidebar + stack
        content_widget = QWidget()
        content_widget.setStyleSheet(f"background: {_Colors.BG_DEEP};")
        content_widget.setMinimumHeight(1)
        top_layout = QHBoxLayout(content_widget)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(0)

        # Sidebar
        self._sidebar = _Sidebar(compact=self._config.startup_menu_compact)
        top_layout.addWidget(self._sidebar)

        # Stack de pages
        self._stack = QStackedWidget()
        self._stack.setStyleSheet(f"background: {_Colors.BG_DEEP};")
        self._stack.setMinimumHeight(0)
        self._stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # Page 0 — Dashboard (fonctionnelle)
        self._dashboard = DashboardPage(self._config, self._log_from_page)
        self._stack.addWidget(self._dashboard)

        # Page 1 — DoVi / HDR10+ (fonctionnelle)
        self._dovi_panel = MergeDoviPanel(self._config)
        self._stack.addWidget(self._dovi_panel)

        # Page 2 — Encodage (fonctionnelle)
        self._encode_panel = EncodePanel(
            self._config,
            writing_application=writing_application,
        )
        self._stack.addWidget(self._encode_panel)

        # Page 3 — Manipulation Conteneur (fonctionnelle)
        self._remux_panel = RemuxPanel(
            self._config,
            writing_application=writing_application,
        )
        self._stack.addWidget(self._remux_panel)

        self._settings_panel = SettingsPanel(self._config)
        self._stack.addWidget(self._settings_panel)

        self._install_source_drop_targets(self._dashboard)
        self._install_source_drop_targets(self._encode_panel)

        self._page_area = QScrollArea()
        self._page_area.setWidgetResizable(True)
        self._page_area.setFrameShape(QFrame.Shape.NoFrame)
        self._page_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._page_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._page_area.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self._page_area.setWidget(self._stack)

        top_layout.addWidget(self._page_area, stretch=1)
        top_vbox.addWidget(content_widget, stretch=1)

        # ── Barre d'action globale ───────────────────────────────────────────
        top_vbox.addWidget(self._build_action_bar())

        vsplit.addWidget(top_widget)

        # Partie basse : log panel
        self._log_panel = LogPanel(max_lines=self._config.log_max_lines)
        self._log_panel.setMinimumHeight(_scale(32))
        vsplit.addWidget(self._log_panel)

        self._apply_startup_log_panel_state()

        main_layout.addWidget(vsplit)

    @classmethod
    def writing_application_tag(cls) -> str:
        return cls.WRITING_APPLICATION

    def _build_action_bar(self) -> QWidget:
        """Construit la barre d'action globale avec bouton unique 'Exécuter l'opération'."""
        bar = QWidget()
        bar.setStyleSheet(
            f"QWidget{{background:{_Colors.BG_PANEL};"
            f"border-top:1px solid {_Colors.BORDER};}}"
        )
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(_scale(28), _scale(10), _scale(28), _scale(10))
        layout.setSpacing(_scale(12))

        # Zone progress (barre fine + légende)
        self._prog_widget = QWidget()
        self._prog_widget.setStyleSheet("background:transparent;")
        pv = QVBoxLayout(self._prog_widget)
        pv.setContentsMargins(0, _scale(4), 0, _scale(4))
        pv.setSpacing(_scale(4))

        self._prog_bar = QProgressBar()
        self._prog_bar.setRange(0, 100)
        self._prog_bar.setValue(0)
        self._prog_bar.setFixedHeight(_scale(6))
        self._prog_bar.setTextVisible(False)
        self._prog_bar.setStyleSheet(self._progress_bar_stylesheet(_Colors.ACCENT))
        pv.addWidget(self._prog_bar)

        self._prog_lbl = QLabel("")
        self._prog_lbl.setStyleSheet(
            f"color:{_Colors.TEXT_DIM};font-size:{_font_px(10)}px;"
            f"font-family:'JetBrains Mono',monospace;background:transparent;"
        )
        pv.addWidget(self._prog_lbl)

        self._prog_widget.setVisible(False)
        layout.addWidget(self._prog_widget, stretch=1)

        self._status_lbl = QLabel("")
        self._status_lbl.setMinimumWidth(0)
        self._status_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._status_lbl.setStyleSheet(
            f"color:{_Colors.TEXT_SEC};font-size:{_font_px(11)}px;background:transparent;"
        )
        layout.addWidget(self._status_lbl)
        layout.addSpacing(_scale(4))

        # Bouton principal unique
        self._run_btn = QPushButton("▶  Exécuter l'opération")
        self._run_btn.setFixedHeight(_scale(36))
        self._run_btn.setMinimumWidth(_scale(180))
        self._run_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._run_btn.setEnabled(False)
        self._run_btn.setStyleSheet(f"""
            QPushButton{{
                background:{_Colors.ACCENT};color:#ffffff;
                border:none;border-radius:6px;
                font-size:{_font_px(12)}px;font-weight:700;padding:0 {_scale(20)}px;
            }}
            QPushButton:hover{{background:#6070f8;}}
            QPushButton:pressed{{background:{_Colors.ACCENT_DIM};}}
            QPushButton:disabled{{background:{_Colors.BG_CARD};
                color:{_Colors.TEXT_DIM};border:1px solid {_Colors.BORDER};}}
        """)
        self._run_btn.clicked.connect(self._on_run)
        layout.addWidget(self._run_btn)

        # Bouton annulation
        self._cancel_btn = QPushButton("✕  Annuler")
        self._cancel_btn.setMinimumWidth(_scale(96))
        self._cancel_btn.setFixedHeight(_scale(36))
        self._cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_btn.setStyleSheet(f"""
            QPushButton{{background:{_Colors.BG_CARD};color:#f5c842;
                border:1px solid #f5c842;border-radius:6px;
                font-size:{_font_px(12)}px;font-weight:600;padding:0 {_scale(14)}px;}}
            QPushButton:hover{{background:#2a2010;border-color:#f0b030;color:#f0b030;}}
            QPushButton:pressed{{background:#1a1608;}}
        """)
        self._cancel_btn.setVisible(False)
        self._cancel_btn.clicked.connect(self._on_cancel_op)
        layout.addWidget(self._cancel_btn)

        return bar

    @staticmethod
    def _progress_bar_stylesheet(chunk_color: str) -> str:
        return (
            f"QProgressBar{{background:{_Colors.BG_CARD};border:none;border-radius:{_scale(3)}px;}}"
            f"QProgressBar::chunk{{background:{chunk_color};border-radius:{_scale(3)}px;}}"
        )

    def _format_progress_label(self, *parts: str) -> str:
        """Concatène la légende d'étape et les métriques (pct/fps/ETA).

        Sans cette concaténation, la barre n'affiche que les métriques
        chiffrées et l'utilisateur perd la trace de l'étape en cours
        (extraction, conversion DV, encode, injection RPU…).
        """
        merged = [p for p in parts if p]
        if self._op_stage_label:
            return f"{self._op_stage_label}  ·  " + "  ·  ".join(merged) if merged else self._op_stage_label
        return "  ·  ".join(merged)

    def _start_prep_progress(self) -> None:
        if not self._running:
            return
        if self._prep_progress_active:
            return
        self._prep_progress_active = True
        self._prep_progress_direction = 1
        self._prep_progress_value = max(4, min(30, self._prog_bar.value() or 0))
        self._prog_bar.setRange(0, 100)
        self._prog_bar.setStyleSheet(self._progress_bar_stylesheet(_Colors.WARN))
        if self._prog_bar.value() <= 0:
            self._prog_bar.setValue(self._prep_progress_value)
        self._prep_progress_timer.start()

    def _stop_prep_progress(self) -> None:
        if self._prep_progress_timer.isActive():
            self._prep_progress_timer.stop()
        self._prep_progress_active = False
        self._prog_bar.setStyleSheet(self._progress_bar_stylesheet(_Colors.ACCENT))

    def _tick_prep_progress(self) -> None:
        if not self._prep_progress_active:
            return
        value = self._prep_progress_value + self._prep_progress_direction
        if value >= 32:
            value = 32
            self._prep_progress_direction = -1
        elif value <= 6:
            value = 6
            self._prep_progress_direction = 1
        self._prep_progress_value = value
        self._prog_bar.setValue(value)

    def _sync_prep_progress_from_log(self, message: str) -> None:
        if not self._running:
            return
        match = _STEP_PROGRESS_RE.match(message.strip())
        if match is None:
            return
        step_text = match.group(1).strip()
        if "Exécution ffmpeg" in step_text or "Exécution du remux ffmpeg" in step_text:
            self._stop_prep_progress()
            return
        self._start_prep_progress()
        self._prog_lbl.setText(
            translate_text(
                "{step} - veuillez patienter",
                step=translate_text(step_text),
            )
        )

    @classmethod
    def startup_page_index(cls, panel_key: str | None) -> int:
        if not panel_key:
            return 0
        return cls._PAGE_INDEX_BY_PANEL_KEY.get(panel_key.strip().lower(), 0)

    def _apply_startup_panel(self) -> None:
        startup_panel = getattr(self._config, "startup_panel", "dashboard")
        page_index = self.startup_page_index(startup_panel)
        self._stack.setCurrentIndex(page_index)
        self._sidebar.select_page(page_index)

    def _apply_startup_log_panel_state(self) -> None:
        logs_expanded = bool(getattr(self._config, "startup_logs_expanded", False))
        self._log_panel.set_collapsed(not logs_expanded)
        if logs_expanded:
            self._vsplit.setSizes([620, 180])
            return
        self._vsplit.setSizes([768, 32])

    def _open_container_panel(self) -> None:
        container_index = self._PAGE_INDEX_BY_PANEL_KEY["container"]
        self._stack.setCurrentIndex(container_index)
        self._sidebar.select_page(container_index)

    def open_startup_paths(self, paths: list[Path | str]) -> None:
        """Charge automatiquement des fichiers transmis au lancement de l'app."""
        normalized = []
        for raw in paths:
            path = Path(raw)
            if not path.exists():
                continue
            normalized.append(path)

        if not normalized:
            return

        MainWindow._open_container_panel(self)
        self._remux_panel.add_sources(normalized)

    def _install_source_drop_targets(self, root: QWidget) -> None:
        for target in (root, *root.findChildren(QWidget)):
            target.setAcceptDrops(True)
            target.installEventFilter(self)

    @staticmethod
    def _local_paths_from_drop_event(event: QEvent) -> list[Path]:
        mime = getattr(event, "mimeData", lambda: None)()
        urls = mime.urls() if mime is not None and mime.hasUrls() else []
        paths: list[Path] = []
        for url in urls:
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if path.exists():
                paths.append(path)
        return paths

    @staticmethod
    def _collect_drop_source_paths(paths: list[Path]) -> list[Path]:
        sources: list[Path] = []
        seen: set[str] = set()

        def add_source(path: Path) -> None:
            path_key = str(path)
            if path_key in seen or not is_accepted(path):
                return
            sources.append(path)
            seen.add(path_key)

        for path in paths:
            if path.is_dir():
                titles = discover_titles(path, min_duration_s=60.0)
                if titles:
                    add_source(titles[0].playlist_path)
                    continue
                if find_disc_root(path) is not None:
                    continue
                for child in sorted(path.rglob("*")):
                    if child.is_file():
                        add_source(child)
                continue
            if path.is_file():
                add_source(path)

        return sources

    def _handle_source_drop_paths(self, paths: list[Path]) -> bool:
        sources = MainWindow._collect_drop_source_paths(paths)
        if not sources:
            return False
        self._remux_panel.add_sources(sources)
        MainWindow._open_container_panel(self)
        return True

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        event_type = event.type()
        if event_type in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop):
            local_paths = self._local_paths_from_drop_event(event)
            if not local_paths:
                return super().eventFilter(watched, event)

            has_possible_source = any(
                path.is_dir() or (path.is_file() and is_accepted(path))
                for path in local_paths
            )
            if not has_possible_source:
                return super().eventFilter(watched, event)

            if event_type == QEvent.Type.Drop and not self._handle_source_drop_paths(local_paths):
                return super().eventFilter(watched, event)

            accept = getattr(event, "acceptProposedAction", None)
            if callable(accept):
                accept()
            return True

        return super().eventFilter(watched, event)

    # ------------------------------------------------------------------
    # Signaux
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        self._sidebar.page_changed.connect(self._stack.setCurrentIndex)
        self.log_requested.connect(self._on_log_requested)
        self._log_panel.collapse_toggled.connect(self._on_log_collapsed)
        # MergeDoviPanel → LogPanel global (QueuedConnection : signal émis depuis threads)
        self._dovi_panel.log_message.connect(
            self.log_requested, Qt.ConnectionType.QueuedConnection
        )
        # MergeDoviPanel → bandeau de progression global (barre indéterminée jaune
        # + libellé d'étape, faute de progression chiffrée native sur ce workflow)
        self._dovi_panel.op_state_changed.connect(
            self._on_dovi_op_state, Qt.ConnectionType.QueuedConnection
        )
        # Pourcentage des outils dovi_tool / hdr10plus_tool → barre globale.
        # Reset implicite à 0 émis par le panel à chaque step_started.
        self._dovi_panel.op_progress_pct.connect(
            self._on_dovi_op_progress_pct, Qt.ConnectionType.QueuedConnection
        )
        # EncodePanel → LogPanel global
        self._encode_panel.log_message.connect(
            self.log_requested, Qt.ConnectionType.QueuedConnection
        )
        # RemuxPanel → LogPanel global
        self._remux_panel.log_message.connect(
            self.log_requested, Qt.ConnectionType.QueuedConnection
        )
        self._remux_panel.tool_output.connect(
            self._on_tool_output_requested, Qt.ConnectionType.QueuedConnection
        )
        self._remux_panel.extract_started.connect(self._on_remux_extract_started)
        self._remux_panel.audio_sync_started.connect(self._on_remux_audio_sync_started)
        self._remux_panel.audio_sync_finished.connect(self._on_remux_audio_sync_finished)
        # RemuxPanel → EncodePanel : pistes partagées + chemin de sortie commun
        self._remux_panel.video_tracks_changed.connect(self._encode_panel.set_video_tracks)
        self._remux_panel.audio_tracks_changed.connect(self._encode_panel.set_audio_tracks)
        self._encode_panel.video_tracks_encoding_changed.connect(self._remux_panel.update_video_track_encoding)
        self._encode_panel.audio_track_meta_changed.connect(self._remux_panel.update_audio_track_meta)
        self._encode_panel.audio_track_encoding_changed.connect(self._remux_panel.update_audio_track_encoding)
        self._encode_panel.audio_track_add_requested.connect(self._remux_panel.add_audio_track_variant)
        self._encode_panel.audio_track_remove_requested.connect(self._remux_panel.remove_audio_track_variant)
        self._encode_panel.set_output_provider(self._remux_panel.current_output_path)
        self._encode_panel.set_file_title_provider(self._remux_panel.current_file_title)
        self._encode_panel.set_extra_attachments_provider(self._remux_panel.current_extra_attachments)
        self._encode_panel.set_tmdb_cover_provider(self._remux_panel.current_tmdb_cover)
        self._encode_panel.set_tag_overrides_provider(self._remux_panel.current_tag_overrides)
        self._encode_panel.set_chapters_provider(self._remux_panel.current_chapter_overrides)
        self._encode_panel.set_mux_backend_provider(self._remux_panel.current_mux_backend)
        self._remux_panel.mux_backend_changed.connect(
            lambda _backend: self._encode_panel.refresh_command_preview()
        )
        # État "prêt" → bouton Exécuter
        self._remux_panel.ready_changed.connect(self._on_ready_changed)
        self._encode_panel.ready_changed.connect(self._on_ready_changed)
        self._settings_panel.settings_saved.connect(self._on_settings_saved)

    def _apply_locale(self) -> None:
        set_current_language(self._config.language)
        apply_translations(self)

    # ------------------------------------------------------------------
    # Bouton "Exécuter l'opération" — logique hybride remux / encode
    # ------------------------------------------------------------------

    _NOISE_RE = re.compile(r"libvmaf\s+ERROR|could not read model from path")

    def _on_ready_changed(self, _ready: bool) -> None:
        """Met à jour l'état du bouton selon la disponibilité des deux panneaux."""
        enabled = (self._remux_panel.is_ready() or self._encode_panel.collect_config() is not None)
        self._run_btn.setEnabled(enabled and not self._running)

    def _on_remux_extract_started(self, task_signals: object, metadata: object) -> None:
        if not isinstance(task_signals, TaskSignals):
            return
        if self._running:
            return

        data = metadata if isinstance(metadata, dict) else {}
        duration_raw = data.get("duration_s")
        self._op_extract_duration_s = (
            float(duration_raw)
            if isinstance(duration_raw, (int, float)) and duration_raw > 0
            else None
        )
        label = str(data.get("label") or translate_text("Extraction du sous-titre")).strip()
        output_name = str(data.get("output_name") or "").strip()
        if output_name:
            label = translate_text("{label} vers {name}", label=label, name=output_name)

        self._running = True
        self._op_start = time.monotonic()
        self._op_mode = "extract"
        self._signals = task_signals
        self._ffmpeg_version_logged = False
        self._op_encode_config = None
        self._op_encode_fps = None
        self._op_encode_frame = None
        self._eta_tracker_video.reset()
        self._eta_tracker_frame.reset()
        self._op_stage_label = label
        self._reset_multi_encode_progress_tracking()

        self._run_btn.setEnabled(False)
        self._cancel_btn.setVisible(True)
        self._prog_bar.setRange(0, 100)
        self._prog_bar.setValue(0)
        self._prog_bar.setStyleSheet(self._progress_bar_stylesheet(_Colors.ACCENT))
        self._prog_lbl.setText(label)
        self._prog_widget.setVisible(True)
        self._status_lbl.setText(translate_text("Extraction en cours…"))
        self._start_prep_progress()

        task_signals.progress.connect(self._on_op_progress, Qt.ConnectionType.QueuedConnection)
        task_signals.finished.connect(
            lambda _: self._on_op_finished(success=True),
            Qt.ConnectionType.QueuedConnection,
        )
        task_signals.failed.connect(
            lambda msg, _exc: self._on_op_finished(success=False, error=msg),
            Qt.ConnectionType.QueuedConnection,
        )
        task_signals.cancelled.connect(self._on_op_cancelled, Qt.ConnectionType.QueuedConnection)

    def _on_remux_audio_sync_started(self, metadata: object) -> None:
        if self._running:
            return

        data = metadata if isinstance(metadata, dict) else {}
        label = str(data.get("label") or translate_text("Synchronisation audio")).strip()

        self._running = True
        self._op_start = time.monotonic()
        self._op_mode = "audio_sync"
        self._signals = None
        self._ffmpeg_version_logged = False
        self._op_encode_config = None
        self._op_encode_fps = None
        self._op_encode_frame = None
        self._eta_tracker_video.reset()
        self._eta_tracker_frame.reset()
        self._op_stage_label = label
        self._reset_multi_encode_progress_tracking()

        self._run_btn.setEnabled(False)
        self._cancel_btn.setVisible(False)
        self._prog_bar.setRange(0, 100)
        self._prog_bar.setValue(0)
        self._prog_bar.setStyleSheet(self._progress_bar_stylesheet(_Colors.WARN))
        self._prog_lbl.setText(label)
        self._prog_widget.setVisible(True)
        self._status_lbl.setText(translate_text("Synchronisation audio en cours…"))
        self._start_prep_progress()

    def _on_remux_audio_sync_finished(self, success: bool, _metadata: object) -> None:
        if self._op_mode != "audio_sync":
            return
        self._on_op_finished(success=bool(success))

    def _on_run(self) -> None:
        if self._running:
            return

        # Reset au démarrage : on veut voir la version ffmpeg de cette
        # exécution (utile en diag), puis silence pour les invocations
        # internes suivantes.
        self._ffmpeg_version_logged = False

        remux_cfg  = self._remux_panel.collect_config()
        encode_cfg = self._encode_panel.collect_config()

        # ── Décision du mode ────────────────────────────────────────────────
        # Encode si une source vidéo est sélectionnée ET (codec ≠ copy ou HDR actif)
        if encode_cfg is None:
            use_encode = False
        else:
            use_encode = not self._encode_panel.is_pure_copy(encode_cfg)

        if use_encode:
            assert encode_cfg is not None
            # Enrichir l'encode config avec les sous-titres / chapitres du remux
            if remux_cfg is not None:
                encode_cfg = self._merge_remux_extras(encode_cfg, remux_cfg)
            errors = self._encode_panel.validate_config(encode_cfg)
            if errors:
                for e in errors:
                    self.log_requested.emit("ERROR", e)
                return
            self._op_mode = "encode"
            self._op_encode_config = encode_cfg
            self.log_requested.emit("INFO", f"Encodage → {encode_cfg.output.name}")
            try:
                signals = self._encode_panel.run_operation(encode_cfg)
            except EncodeError as exc:
                self.log_requested.emit("ERROR", str(exc))
                return

        elif remux_cfg is not None:
            self._op_encode_config = None
            errors = self._remux_panel.validate_config(remux_cfg)
            if errors:
                for e in errors:
                    self.log_requested.emit("ERROR", e)
                return
            self._op_mode = "remux"
            self.log_requested.emit("INFO", f"Remuxage → {remux_cfg.output.name}")
            try:
                signals = self._remux_panel.run_operation(remux_cfg)
            except RemuxError as exc:
                self.log_requested.emit("ERROR", str(exc))
                return

        else:
            self.log_requested.emit("WARN", "Aucune opération configurée.")
            return

        # ── Connexion des signaux de progression ────────────────────────────
        self._running  = True
        self._op_start = time.monotonic()
        self._signals  = signals
        self._run_btn.setEnabled(False)
        self._cancel_btn.setVisible(True)
        self._op_encode_fps = None
        self._op_encode_frame = None
        self._eta_tracker_video.reset()
        self._eta_tracker_frame.reset()
        self._op_stage_label = ""
        self._reset_multi_encode_progress_tracking()
        if self._op_mode == "encode" and self._op_encode_config is not None:
            self._op_encode_multi_targets = self._encode_panel.get_video_progress_targets(self._op_encode_config)
        self._prep_progress_active = False
        if self._prep_progress_timer.isActive():
            self._prep_progress_timer.stop()
        self._prog_bar.setRange(0, 100)
        self._prog_bar.setValue(0)
        self._prog_bar.setStyleSheet(self._progress_bar_stylesheet(_Colors.ACCENT))
        self._prog_lbl.setText("")
        self._prog_widget.setVisible(True)
        label = translate_text("Encodage en cours…") if self._op_mode == "encode" else translate_text("Remuxage en cours…")
        self._status_lbl.setText(label)
        self._start_prep_progress()

        signals.progress.connect(self._on_op_progress, Qt.ConnectionType.QueuedConnection)
        signals.finished.connect(
            lambda _: self._on_op_finished(success=True),
            Qt.ConnectionType.QueuedConnection,
        )
        signals.failed.connect(
            lambda msg, _exc: self._on_op_finished(success=False, error=msg),
            Qt.ConnectionType.QueuedConnection,
        )
        signals.cancelled.connect(self._on_op_cancelled, Qt.ConnectionType.QueuedConnection)

    def _merge_remux_extras(
        self,
        encode_cfg: "EncodeConfig",
        remux_cfg: "RemuxConfig",
    ) -> "EncodeConfig":
        from core.workflows.encode.remux_bridge import merge_remux_into_encode_config

        return merge_remux_into_encode_config(encode_cfg, remux_cfg)

    def _reset_multi_encode_progress_tracking(self) -> None:
        if self._op_encode_multi_reselect_timer.isActive():
            self._op_encode_multi_reselect_timer.stop()
        self._op_encode_multi_targets = {}
        self._op_encode_multi_state = {}
        self._op_encode_multi_active_label = None

    def _ensure_multi_encode_state(self, label: str) -> dict[str, object]:
        state = self._op_encode_multi_state.get(label)
        if state is not None:
            return state

        parsed = _parse_multi_encode_label(label)
        order = 0
        pass_index: int | None = None
        pass_count = 1
        if parsed is not None:
            order, pass_index, pass_count = parsed
        target = self._op_encode_multi_targets.get(order, {})
        state = {
            "label": label,
            "order": order,
            "pass_index": pass_index,
            "pass_count": pass_count,
            "duration_s": target.get("duration_s"),
            "total_frames": target.get("total_frames"),
            "source_name": target.get("source_name"),
            "started_at": time.monotonic(),
            "last_update": 0.0,
            "fps": None,
            "frame": None,
            "elapsed_video": None,
            "done": False,
        }
        self._op_encode_multi_state[label] = state
        return state

    def _multi_encode_progress_parts(
        self,
        state: dict[str, object],
        *,
        now: float,
    ) -> tuple[list[str], int | None]:
        order_raw = state.get("order")
        if isinstance(order_raw, int):
            order = order_raw
        elif isinstance(order_raw, float):
            order = int(order_raw)
        elif isinstance(order_raw, str):
            try:
                order = int(order_raw.strip())
            except ValueError:
                order = 0
        else:
            order = 0
        total_tracks = max(self._op_encode_multi_targets) if self._op_encode_multi_targets else 0
        track_label = (
            translate_text("Piste vidéo {current}/{total}", current=order, total=total_tracks)
            if order > 0 and total_tracks > 1
            else translate_text("Piste vidéo")
        )
        parts = [track_label]

        pass_index = state.get("pass_index")
        pass_count_raw = state.get("pass_count")
        if isinstance(pass_count_raw, int):
            pass_count = pass_count_raw
        elif isinstance(pass_count_raw, float):
            pass_count = int(pass_count_raw)
        elif isinstance(pass_count_raw, str):
            try:
                pass_count = int(pass_count_raw.strip())
            except ValueError:
                pass_count = 1
        else:
            pass_count = 1
        if isinstance(pass_index, int) and pass_count > 1:
            parts.append(
                translate_text("passe {current}/{total}", current=pass_index, total=pass_count)
            )

        fps_value = state.get("fps")
        pct: int | None = None
        duration_s = state.get("duration_s")
        elapsed_video = state.get("elapsed_video")
        if isinstance(duration_s, (int, float)) and duration_s > 0 and isinstance(elapsed_video, (int, float)):
            pct = min(99, int(float(elapsed_video) / float(duration_s) * 100))
        else:
            total_frames = state.get("total_frames")
            frame = state.get("frame")
            if isinstance(total_frames, int) and total_frames > 0 and isinstance(frame, int):
                pct = min(99, int(frame / total_frames * 100))

        if pct is not None:
            parts.append(f"{pct}%")
        if isinstance(fps_value, (int, float)) and fps_value > 0:
            parts.append(f"{float(fps_value):.1f} fps")

        remaining_s = _multi_encode_remaining_seconds(state, now)
        if remaining_s is not None:
            parts.append(f"ETA {_fmt_eta(remaining_s)}")
        return parts, pct

    def _reevaluate_multi_encode_progress(self) -> None:
        if not self._running or self._op_mode != "encode" or not self._op_encode_multi_state:
            return
        now = time.monotonic()
        active_label = _select_multi_encode_label(
            self._op_encode_multi_state,
            self._op_encode_multi_active_label,
            now,
        )
        if active_label is None:
            self._op_encode_multi_active_label = None
            return

        self._op_encode_multi_active_label = active_label
        state = self._op_encode_multi_state.get(active_label)
        if state is None:
            return

        parts, pct = self._multi_encode_progress_parts(state, now=now)
        if pct is not None:
            self._stop_prep_progress()
            self._prog_bar.setValue(pct)
        self._prog_lbl.setText(self._format_progress_label(*parts))
        if len(self._op_encode_multi_state) > 1 and not self._op_encode_multi_reselect_timer.isActive():
            self._op_encode_multi_reselect_timer.start()

    def _handle_encode_internal_progress(self, line: str) -> bool:
        payload = _parse_encode_internal_progress(line)
        if payload is None:
            return False
        if str(payload.get("kind") or "") != "encode_ffmpeg":
            return False

        label = str(payload.get("label") or "").strip()
        if not label.startswith("ffmpeg-video-"):
            return True

        event = str(payload.get("event") or "").strip().lower()
        raw_line = str(payload.get("line") or "")
        state = self._ensure_multi_encode_state(label)

        if event == "done":
            state["done"] = True
            state["last_update"] = time.monotonic()
            self._reevaluate_multi_encode_progress()
            return True

        if raw_line.startswith("$ "):
            self.log_requested.emit("INFO", raw_line)
            return True

        fps_m = _FPS_RE.search(raw_line)
        if fps_m:
            try:
                state["fps"] = float(fps_m.group(1))
            except ValueError:
                state["fps"] = None
        frame_m = _FRAME_RE.search(raw_line)
        if frame_m:
            try:
                state["frame"] = int(frame_m.group(1))
            except ValueError:
                state["frame"] = None
        elapsed_video = ffmpeg_progress_seconds(raw_line)
        if elapsed_video is not None:
            state["elapsed_video"] = elapsed_video
        state["last_update"] = time.monotonic()
        self._reevaluate_multi_encode_progress()

        if _is_encode_progress_noise(raw_line):
            return True
        if _is_ffmpeg_info_noise(raw_line):
            return True
        if _is_encode_stage_message(raw_line):
            if _is_encode_indeterminate_stage(raw_line):
                self._start_prep_progress()
            else:
                self._stop_prep_progress()
            self._op_stage_label = raw_line.strip()
            self._prog_lbl.setText(raw_line)
            return True
        self.log_requested.emit("INFO", raw_line)
        return True

    def _on_op_progress(self, line: str) -> None:
        """Gère la progression selon le mode (remux ou encode)."""
        self._capture_verbose_progress_line(line)
        # Banner/listing ffmpeg : ne pas pourrir l'UI mais loguer la version
        # la 1re fois pour traçabilité standard. Le verbose file a déjà la
        # ligne complète via _capture_verbose_progress_line ci-dessus.
        if line.startswith("ffmpeg version "):
            if not self._ffmpeg_version_logged:
                self._ffmpeg_version_logged = True
                # Extraire "ffmpeg version 7.1.3" jusqu'au premier "Copyright".
                short = line.split(" Copyright", 1)[0].strip()
                self.log_requested.emit("INFO", short)
            return
        if _is_ffmpeg_info_noise(line):
            return
        sync_stage = _parse_sync_rewrite_stage_progress(line)
        if sync_stage is not None:
            self._stop_prep_progress()
            self._op_stage_label = _sync_rewrite_stage_label(*sync_stage)
            self._prog_lbl.setText(self._op_stage_label)
            return
        if self._op_mode == "extract":
            if line.startswith("$ "):
                self._stop_prep_progress()
                self.log_requested.emit("INFO", line)
                return
            elapsed_video = ffmpeg_progress_seconds(line)
            if elapsed_video is not None:
                dur = self._op_extract_duration_s or self._remux_panel.get_duration_s()
                if dur and dur > 0:
                    self._stop_prep_progress()
                    pct = min(99, int(elapsed_video / dur * 100))
                    self._prog_bar.setValue(pct)
                    self._eta_tracker_video.update(elapsed_video, time.monotonic())
                    eta_s = self._eta_tracker_video.eta(dur, elapsed_video)
                    eta_str = f"ETA {_fmt_eta(eta_s)}" if eta_s is not None else ""
                    self._prog_lbl.setText(self._format_progress_label(f"{pct}%", eta_str))
                else:
                    self._start_prep_progress()
                return
            if _is_encode_progress_noise(line):
                return
            self.log_requested.emit("INFO", line)
            return
        if self._op_mode == "remux":
            if line.startswith("$ "):
                self._stop_prep_progress()
                self.log_requested.emit("INFO", line)
                return
            elapsed_video = ffmpeg_progress_seconds(line)
            if elapsed_video is not None:
                self._stop_prep_progress()
                dur = self._remux_panel.get_duration_s()
                if dur and dur > 0:
                    pct = min(99, int(elapsed_video / dur * 100))
                    self._prog_bar.setValue(pct)
                    self._eta_tracker_video.update(elapsed_video, time.monotonic())
                    eta_s = self._eta_tracker_video.eta(dur, elapsed_video)
                    eta_str = f"ETA {_fmt_eta(eta_s)}" if eta_s is not None else ""
                    parts = [f"{pct}%", eta_str]
                    self._prog_lbl.setText(self._format_progress_label(*parts))
                return
            if _is_encode_progress_noise(line):
                return
            self.log_requested.emit("INFO", line)
        else:
            if self._handle_encode_internal_progress(line):
                return
            if line.startswith(_PCT_SENTINEL):
                # Progression chiffrée des outils dovi_tool / hdr10plus_tool
                # (lecture pty). Bascule la barre globale en mode déterminé.
                try:
                    pct = int(line[len(_PCT_SENTINEL):].strip())
                except ValueError:
                    return
                self._stop_prep_progress()
                self._prog_bar.setRange(0, 100)
                self._prog_bar.setValue(max(0, min(100, pct)))
                return
            if self._NOISE_RE.search(line):
                return
            if line.startswith("$ "):
                self._stop_prep_progress()
                self._op_encode_fps = None
                self._op_encode_frame = None
                self._eta_tracker_video.reset()
                self._eta_tracker_frame.reset()
                self.log_requested.emit("INFO", line)
                return
            progress_event = (
                self._encode_panel.parse_progress_line(self._op_encode_config, line)
                if self._op_encode_config is not None
                else None
            )
            if progress_event is not None:
                if progress_event.fps is not None:
                    self._op_encode_fps = progress_event.fps
                if progress_event.frame is not None:
                    self._op_encode_frame = progress_event.frame

                pct_value: int | None = None
                pct_label = ""
                if progress_event.percent is not None:
                    pct_value = max(0, min(99, int(progress_event.percent)))
                    pct_label = _fmt_progress_percent(progress_event.percent)
                elif progress_event.elapsed_seconds is not None:
                    dur = self._encode_panel.get_duration_s()
                    if dur and dur > 0:
                        pct_value = min(99, int(progress_event.elapsed_seconds / dur * 100))
                        pct_label = f"{pct_value}%"
                elif progress_event.frame is not None:
                    total_frames = self._encode_panel.get_total_frames()
                    if total_frames and total_frames > 0:
                        pct_value = min(99, int(progress_event.frame / total_frames * 100))
                        pct_label = f"{pct_value}%"

                if pct_value is not None:
                    self._stop_prep_progress()
                    self._prog_bar.setValue(pct_value)

                fps_str = (
                    f"{progress_event.fps:.1f} fps"
                    if progress_event.fps is not None and progress_event.fps > 0
                    else ""
                )
                eta_s = progress_event.eta_seconds
                if (
                    eta_s is None
                    and progress_event.elapsed_seconds is not None
                ):
                    dur = self._encode_panel.get_duration_s()
                    if dur and dur > 0:
                        self._eta_tracker_video.update(progress_event.elapsed_seconds, time.monotonic())
                        eta_s = self._eta_tracker_video.eta(dur, progress_event.elapsed_seconds)
                if (
                    eta_s is None
                    and progress_event.frame is not None
                ):
                    total_frames = self._encode_panel.get_total_frames()
                    if total_frames and total_frames > 0:
                        if progress_event.fps is not None and progress_event.fps > 0 and total_frames > progress_event.frame:
                            eta_s = self._eta_tracker_frame.eta_from_speed(
                                float(total_frames),
                                float(progress_event.frame),
                                float(progress_event.fps),
                            )
                        else:
                            self._eta_tracker_frame.update(float(progress_event.frame), time.monotonic())
                            eta_s = self._eta_tracker_frame.eta(float(total_frames), float(progress_event.frame))
                eta_str = f"ETA {_fmt_eta(eta_s)}" if eta_s is not None else ""
                if pct_label or fps_str or eta_str:
                    self._prog_lbl.setText(self._format_progress_label(pct_label, fps_str, eta_str))
                if progress_event.should_log:
                    self.log_requested.emit("INFO", progress_event.raw_line)
                return
            fps_m = _FPS_RE.search(line)
            if fps_m:
                try:
                    self._op_encode_fps = float(fps_m.group(1))
                except ValueError:
                    self._op_encode_fps = None
            frame_m = _FRAME_RE.search(line)
            if frame_m:
                try:
                    self._op_encode_frame = int(frame_m.group(1))
                except ValueError:
                    self._op_encode_frame = None
            elapsed_video = ffmpeg_progress_seconds(line)
            if elapsed_video is not None:
                self._stop_prep_progress()
                dur = self._encode_panel.get_duration_s()
                if dur and dur > 0:
                    pct = min(99, int(elapsed_video / dur * 100))
                    self._prog_bar.setValue(pct)
                    fps_str = (
                        f"{self._op_encode_fps:.1f} fps"
                        if self._op_encode_fps is not None and self._op_encode_fps > 0
                        else ""
                    )
                    self._eta_tracker_video.update(elapsed_video, time.monotonic())
                    eta_s = self._eta_tracker_video.eta(dur, elapsed_video)
                    eta_str = f"ETA {_fmt_eta(eta_s)}" if eta_s is not None else ""
                    parts = [f"{pct}%", fps_str, eta_str]
                    self._prog_lbl.setText(self._format_progress_label(*parts))
                return
            if frame_m is not None and self._op_encode_frame is not None:
                total_frames = self._encode_panel.get_total_frames()
                if total_frames and total_frames > 0:
                    self._stop_prep_progress()
                    pct = min(99, int(self._op_encode_frame / total_frames * 100))
                    self._prog_bar.setValue(pct)
                    fps_str = (
                        f"{self._op_encode_fps:.1f} fps"
                        if self._op_encode_fps is not None and self._op_encode_fps > 0
                        else ""
                    )
                    encode_eta_s: float | None = None
                    # Si ffmpeg a rapporté un fps instantané, l'utiliser
                    # directement : c'est déjà la moyenne glissante côté ffmpeg
                    # et c'est ce que l'utilisateur voit comme "fps". L'ETA
                    # affiché reste cohérent avec le débit affiché.
                    if (
                        self._op_encode_fps is not None
                        and self._op_encode_fps > 0
                        and total_frames > self._op_encode_frame
                    ):
                        encode_eta_s = self._eta_tracker_frame.eta_from_speed(
                            float(total_frames),
                            float(self._op_encode_frame),
                            float(self._op_encode_fps),
                        )
                    else:
                        # Fallback : EWMA sur la vitesse instantanée mesurée.
                        self._eta_tracker_frame.update(
                            float(self._op_encode_frame), time.monotonic()
                        )
                        encode_eta_s = self._eta_tracker_frame.eta(
                            float(total_frames), float(self._op_encode_frame)
                        )
                    eta_str = f"ETA {_fmt_eta(encode_eta_s)}" if encode_eta_s is not None else ""
                    parts = [f"{pct}%", fps_str, eta_str]
                    self._prog_lbl.setText(self._format_progress_label(*parts))
                return
            if _is_encode_progress_noise(line):
                return
            if _is_encode_stage_message(line):
                self._op_encode_fps = None
                self._op_encode_frame = None
                if _is_encode_indeterminate_stage(line):
                    self._start_prep_progress()
                else:
                    self._stop_prep_progress()
                self._op_stage_label = line.strip()
                self._prog_lbl.setText(line)
            self.log_requested.emit("INFO", line)

    def _on_cancel_op(self) -> None:
        reply = QMessageBox.question(
            self,
            translate_text("Confirmer l'annulation"),
            translate_text("Annuler l'opération en cours ?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes and self._signals is not None:
            self._signals.cancel()

    def _on_dovi_op_state(self, state: str, label: str) -> None:
        """
        Pilote le bandeau de progression global pour le workflow MergeDovi.

        Comme dovi_tool/hdr10plus_tool n'émettent pas de pourcentage exploitable
        (et pas du tout sous Windows faute de pty), on affiche une barre jaune
        indéterminée pendant toute l'opération, avec le libellé de l'étape en
        cours.
        """
        if state == "started":
            self._running = True
            self._op_start = time.monotonic()
            self._op_mode = "merge_dovi"
            self._dovi_step_label = label
            self._prog_widget.setVisible(True)
            self._prog_bar.setRange(0, 100)
            self._prog_bar.setValue(0)
            self._prog_lbl.setText(label)
            self._status_lbl.setText(label)
            self._start_prep_progress()
        elif state == "step":
            self._prog_lbl.setText(label)
            self._dovi_step_label = label
            # Nouvelle étape : repart en prep jaune indéterminée. Si l'outil
            # émet un % (dovi_tool / hdr10plus_tool), la barre bascule en
            # chiffré via _on_dovi_op_progress_pct.
            self._prog_bar.setValue(0)
            self._start_prep_progress()
        elif state == "finished":
            self._stop_prep_progress()
            self._prog_bar.setRange(0, 100)
            self._prog_bar.setValue(100)
            self._prog_lbl.setText(translate_text("100%  ·  terminé"))
            self._status_lbl.setText(label)
            self._running = False
            self._op_mode = ""
        elif state in ("failed", "cancelled"):
            self._stop_prep_progress()
            self._prog_widget.setVisible(False)
            self._prog_bar.setRange(0, 100)
            self._prog_lbl.setText("")
            self._status_lbl.setText(label)
            self._running = False
            self._op_mode = ""

    def _on_dovi_op_progress_pct(self, pct: int) -> None:
        """
        Pourcentage chiffré reporté par dovi_tool / hdr10plus_tool pendant le
        workflow MergeDovi. Bascule la barre globale de la prep jaune
        indéterminée vers une progression chiffrée bleue.
        """
        if self._op_mode != "merge_dovi":
            return
        self._stop_prep_progress()
        self._prog_bar.setRange(0, 100)
        clamped = max(0, min(100, pct))
        self._prog_bar.setValue(clamped)
        step_label = getattr(self, "_dovi_step_label", "") or ""
        prefix = f"{step_label}  ·  " if step_label else ""
        self._prog_lbl.setText(f"{prefix}{clamped}%")

    def _on_op_cancelled(self) -> None:
        self._running = False
        self._signals = None
        self._op_encode_config = None
        self._op_extract_duration_s = None
        self._reset_multi_encode_progress_tracking()
        self._stop_prep_progress()
        self._run_btn.setEnabled(True)
        self._cancel_btn.setVisible(False)
        self._prog_widget.setVisible(False)
        self._prog_bar.setRange(0, 100)
        self._prog_lbl.setText("")
        self._status_lbl.setText(translate_text("Annulé."))
        self.log_requested.emit("WARN", "Opération annulée.")

    def _on_op_finished(self, success: bool, error: str = "") -> None:
        self._running = False
        self._signals = None
        self._op_encode_config = None
        finished_mode = self._op_mode
        self._op_extract_duration_s = None
        self._reset_multi_encode_progress_tracking()
        self._stop_prep_progress()
        self._run_btn.setEnabled(True)
        self._cancel_btn.setVisible(False)
        self._prog_bar.setRange(0, 100)
        if success:
            self._prog_bar.setValue(100)
            self._prog_lbl.setText(translate_text("100%  ·  terminé"))
            self._status_lbl.setText(translate_text("Terminé."))
            label = {
                "encode": "Encodage terminé",
                "extract": "Extraction terminée",
                "audio_sync": "Synchronisation audio terminée",
                "remux": "Remuxage terminé",
            }.get(finished_mode, "Opération terminée")
            self.log_requested.emit("OK", translate_text(label))
        else:
            self._prog_widget.setVisible(False)
            self._prog_lbl.setText("")
            self._status_lbl.setText(translate_text("Échec."))
            if error:
                self.log_requested.emit("ERROR", error)

    def _on_settings_saved(self) -> None:
        previous_theme = DesignSystem.current_theme()
        previous_scale = DesignSystem.current_ui_scale()
        previous_file_logging_enabled = _config_file_logging_enabled(self._config)
        previous_file_logging_level = _config_file_logging_level(self._config)
        self._config.reload()
        new_theme = DesignSystem.set_theme(self._config.theme)
        new_scale = DesignSystem.set_ui_scale(self._config.ui_scale_percent)
        _ensure_verbose_file_logger(self)
        app = cast(QApplication | None, QApplication.instance())
        if app is not None:
            current_font = app.font()
            current_font.setPointSizeF(max(8.0, 10.0 * DesignSystem.scale_factor()))
            app.setFont(current_font)
        DesignSystem.apply_to_application(app)
        self._log_panel._max_lines = self._config.log_max_lines
        self._encode_panel.refresh_runtime_settings()
        self._remux_panel.refresh_runtime_settings()
        self._sidebar.set_compact(self._config.startup_menu_compact)
        self._apply_locale()
        if new_theme != previous_theme:
            self.log_requested.emit(
                "INFO",
                "Nouveau thème chargé. Un redémarrage de l'application est recommandé pour recolorer tous les panneaux ouverts.",
            )
        if new_scale != previous_scale:
            self.log_requested.emit(
                "INFO",
                translate_text(
                    "Nouvelle échelle d'interface chargée ({percent}%). Un redémarrage de l'application est recommandé pour homogénéiser tous les panneaux.",
                    percent=new_scale,
                ),
            )
            self._prompt_restart_for_scale_change(new_scale)
        new_file_logging_enabled = _config_file_logging_enabled(self._config)
        new_file_logging_level = _config_file_logging_level(self._config)
        if new_file_logging_enabled and not previous_file_logging_enabled:
            self.log_requested.emit(
                "INFO",
                translate_text(
                    "Logging fichier activé ({level}) : {path}",
                    level=translate_text("Verbose" if new_file_logging_level == "verbose" else "Standard"),
                    path=str(self._verbose_log_session_path()),
                ),
            )
        elif previous_file_logging_enabled and not new_file_logging_enabled:
            self.log_requested.emit("INFO", "Logging fichier désactivé.")
        elif new_file_logging_enabled and previous_file_logging_level != new_file_logging_level:
            self.log_requested.emit(
                "INFO",
                translate_text(
                    "Niveau de logging fichier : {level}",
                    level=translate_text("Verbose" if new_file_logging_level == "verbose" else "Standard"),
                ),
            )
        self.log_requested.emit("OK", "Configuration appliquée depuis config.ini.")

    def _prompt_restart_for_scale_change(self, percent: int) -> None:
        reply = QMessageBox.question(
            self,
            translate_text("Redémarrage recommandé"),
            translate_text(
                "La nouvelle échelle d'interface ({percent}%) est chargée.\nRedémarrer l'application maintenant ?",
                percent=percent,
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        restarted = self._config.restart_application()
        if not restarted:
            QMessageBox.warning(
                self,
                translate_text("Erreur"),
                translate_text("Impossible de redémarrer automatiquement l'application."),
            )
            return

        app = cast(QApplication | None, QApplication.instance())
        if app is not None:
            app.quit()

    # ------------------------------------------------------------------
    # Collapse du panneau de logs
    # ------------------------------------------------------------------

    def _on_log_collapsed(self, collapsed: bool) -> None:
        total = self._vsplit.height()
        if collapsed:
            self._vsplit.setSizes([total - 32, 32])
        else:
            log_h = max(total // 5, 120)
            self._vsplit.setSizes([total - log_h, log_h])

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _verbose_log_part_path(self, index: int) -> Path:
        return _ensure_verbose_file_logger(self).part_path(index)

    def _verbose_log_session_path(self) -> Path:
        return _ensure_verbose_file_logger(self).session_path()

    def _prepare_verbose_log_target(self, incoming_bytes: int) -> Path:
        return _ensure_verbose_file_logger(self).prepare_target(incoming_bytes)

    def _append_verbose_log_file(self, message: str, level: LogLevel) -> None:
        _ensure_verbose_file_logger(self).append_application_message(message, level)

    def _append_verbose_tool_output(
        self,
        line: str,
        *,
        label: str | None = None,
    ) -> None:
        _ensure_verbose_file_logger(self).append_tool_output(line, label=label)

    def _capture_verbose_progress_line(self, line: str) -> None:
        if not _config_file_logging_is_verbose(self._config):
            return

        payload = _parse_encode_internal_progress(line)
        if payload is not None and str(payload.get("kind") or "") == "encode_ffmpeg":
            label = str(payload.get("label") or "").strip() or None
            event = str(payload.get("event") or "").strip().lower()
            raw_line = str(payload.get("line") or "")
            if event == "done":
                self._append_verbose_tool_output("done", label=label)
                return
            if raw_line and not raw_line.startswith("$ "):
                if _is_encode_progress_noise(raw_line) or _is_encode_stage_message(raw_line):
                    self._append_verbose_tool_output(raw_line, label=label)
            return

        if line.startswith("$ "):
            return

        if self._op_mode == "remux":
            if ffmpeg_progress_seconds(line) is not None or _is_encode_progress_noise(line):
                self._append_verbose_tool_output(line)
            return

        encode_cfg = getattr(self, "_op_encode_config", None)
        encode_codec = str(getattr(getattr(encode_cfg, "video", None), "codec", "") or "").strip().lower()
        if encode_codec and backend_id_for_codec(encode_codec) == "nvencc":
            if line and not line.startswith("$ ") and not line.startswith(_PCT_SENTINEL):
                self._append_verbose_tool_output(line)
            return

        if self._NOISE_RE.search(line):
            self._append_verbose_tool_output(line)
            return
        if ffmpeg_progress_seconds(line) is not None:
            self._append_verbose_tool_output(line)
            return
        if _FRAME_RE.search(line):
            total_frames = self._encode_panel.get_total_frames()
            if total_frames and total_frames > 0:
                self._append_verbose_tool_output(line)
                return
        if _is_encode_progress_noise(line):
            self._append_verbose_tool_output(line)

    def _emit_log_entry(self, message: str, level: LogLevel) -> None:
        self._append_verbose_log_file(message, level)
        self._log_panel.log(message, level)

    def _log_from_page(self, message: str, level: LogLevel) -> None:
        """Callback passé aux pages pour poster des logs."""
        self._emit_log_entry(message, level)

    def _on_log_requested(self, level: str, message: str) -> None:
        """Slot connecté au signal public log_requested(str, str)."""
        self._sync_prep_progress_from_log(message)
        lv = parse_log_level(level)
        self._emit_log_entry(message, lv)

    def _on_tool_output_requested(self, label: str, line: str) -> None:
        if not _config_file_logging_is_verbose(self._config):
            return
        tool_label = str(label).strip() or None
        self._append_verbose_tool_output(line, label=tool_label)

    # Raccourcis directs
    def log_info(self, msg: str)  -> None: self._emit_log_entry(msg, LogLevel.INFO)
    def log_ok(self, msg: str)    -> None: self._emit_log_entry(msg, LogLevel.OK)
    def log_warn(self, msg: str)  -> None: self._emit_log_entry(msg, LogLevel.WARN)
    def log_error(self, msg: str) -> None: self._emit_log_entry(msg, LogLevel.ERROR)

    # ------------------------------------------------------------------
    # Géométrie
    # ------------------------------------------------------------------

    def _restore_geometry(self) -> None:
        if self._config.window_geometry:
            self.restoreGeometry(self._config.window_geometry)

    def closeEvent(self, event) -> None:
        self._config.save_geometry(bytes(self.saveGeometry().data()))
        self._config.save()
        # Arrête proprement tous les ThreadPoolExecutor des pages enfants :
        # sinon des threads survivent à app.exec() et peuvent retenir des
        # FDs/processus, ce qui empêche l'OS de restaurer les flags du tty
        # parent (terminal sans echo après fermeture).
        for attr in ("_dashboard", "_encode_panel", "_remux_panel", "_dovi_panel"):
            page = getattr(self, attr, None)
            executor = getattr(page, "_executor", None) if page is not None else None
            if executor is not None:
                try:
                    executor.shutdown(wait=True)
                except Exception:
                    pass
        verbose_logger = getattr(self, "_verbose_file_logger", None)
        if verbose_logger is not None:
            try:
                verbose_logger.close()
            except Exception:
                pass
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Post-init
    # ------------------------------------------------------------------

    def _post_init_log(self) -> None:
        self.log_info("Muxiveo démarré.")
        availability = self._config.all_tools_available()
        missing = [n for n, ok in availability.items() if not ok]
        if missing:
            self.log_warn(
                f"Outils manquants : {', '.join(missing)}"
            )
        else:
            self.log_ok("Tous les outils externes sont disponibles.")
        if _config_file_logging_enabled(self._config):
            logging_level = _config_file_logging_level(self._config)
            self.log_info(
                translate_text(
                    "Logging fichier activé ({level}) : {path}",
                    level=translate_text("Verbose" if logging_level == "verbose" else "Standard"),
                    path=str(self._verbose_log_session_path()),
                )
            )
        self.log_info(
            f"Dossier de travail : {self._config.work_dir}"
        )
        self.log_info(
            f"Dossier de sortie  : {self._config.output_dir}"
        )
