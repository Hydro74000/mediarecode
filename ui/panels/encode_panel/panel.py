"""
ui/panels/encode_panel/panel.py — Main EncodePanel widget.

Public:
    EncodePanel
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QSize, Qt, Signal, QUrl
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QFont, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel,
    QLayout,
    QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox,
    QPlainTextEdit, QProgressBar, QScrollArea, QSizePolicy, QSlider, QSpinBox, QStackedWidget, QTabWidget,
    QVBoxLayout, QWidget,
)

from core.bluray import ffprobe_input_args
from core.config import AppConfig
from core.inspector import FileInfo, HDRType, VideoTrack
from core.i18n import apply_translations, set_current_language, translate_text
from core.workflows.remux_models import TrackEntry
from core.runner import TaskSignals
from core.workflows.encode import (
    HARDWARE_VIDEO_CODECS, SOFTWARE_VIDEO_CODECS,
    TONEMAP_ALGORITHMS, EncodeConfig,
    EncodePreviewRequest,
    EncodePreset, EncodeWorkflow, HardwareEncoderDetector,
    ProfileManager, QualityMode, VideoCropSettings, VideoEncodeSettings, VideoFilterSettings,
    VideoResizeSettings, VideoTrackEncodePlan, presets_for_codec,
)
from core.workflows.encode.catalog import (
    VIDEO_ENCODER_BADGES,
    VIDEO_HDR_BADGE_ORDER,
    encoder_badge,
    is_h264_video_codec,
    supports_10bit,
)
from core.workflows.encode.backends import (
    backend_capabilities_for_codec,
)
from core.workflows.encode.runtime.static_hdr_estimator import (
    StaticHdrEstimate,
    StaticHdrEstimateService,
)
from ui.panels.encode_panel.theme import (
    _C, _card, _checkbox_style, _combo_style,
    _input_style, _primary_button, _secondary_button,
    _section_label, _separator,
)
from ui.dialogs.extra_params_dialog import edit_extra_params
from ui.panels.encode_panel.widgets import _AudioSourceDialog, _AudioTable


class EncodePanel(QWidget):
    """
    Panneau d'encodage vidéo/audio.

    Signaux :
        log_message(level: str, message: str)
        ready_changed(bool) — True quand une source vidéo est sélectionnée
    """

    log_message              = Signal(str, str)
    ready_changed            = Signal(bool)     # émis quand la source vidéo change
    audio_track_meta_changed = Signal(int, object, str, str, object)  # (stream_index, source_path, lang, title, entry_id)
    audio_track_encoding_changed = Signal(object, str, int)  # (entry_id, codec, bitrate_kbps)
    audio_track_add_requested = Signal(object, str, str, int)  # (template TrackEntry, entry_id, codec, bitrate_kbps)
    audio_track_remove_requested = Signal(object)  # (entry_id)
    video_tracks_encoding_changed = Signal(object)
    _hw_detected             = Signal(object, object, object)   # (hw: set[str], sw: set[str], hw_ffmpeg: str)
    _hdr_meta_frame_probe_ready = Signal(int, str, str)
    _VIDEO_ENCODER_BADGES = VIDEO_ENCODER_BADGES
    _VIDEO_HDR_BADGE_ORDER = VIDEO_HDR_BADGE_ORDER
    _MAX_VISIBLE_VIDEO_SOURCE_ROWS = 10
    _VIDEO_SOURCE_ROW_H = 34

    def __init__(
        self,
        config: AppConfig,
        parent: QWidget | None = None,
        *,
        writing_application: str = "",
    ) -> None:
        super().__init__(parent)
        self._config    = config
        set_current_language(getattr(config, "language", None))
        self._workflow  = EncodeWorkflow(
            ffmpeg_bin=config.tool_ffmpeg,
            dovi_tool_bin=config.tool_dovi_tool,
            hdr10plus_bin=config.tool_hdr10plus,
            mediainfo_bin=config.tool_mediainfo,
            ram_buffer_enabled=config.ram_buffer_enabled,
            ram_buffer_threshold_pct=config.ram_buffer_threshold_pct,
            ffmpeg_threads=config.ffmpeg_threads,
            max_parallel_video_encodes=config.max_parallel_video_encodes,
            parent=self,
            writing_application=writing_application,
            generate_nfo=config.generate_nfo,
            nvencc_bin=getattr(config, "tool_nvencc", None) or None,
            sync_rewrite_enabled=config.sync_rewrite_enabled,
            aac_bitrate_per_channel_kbps=config.aac_bitrate_per_channel_kbps,
            eac3_bitrate_per_channel_kbps=config.eac3_bitrate_per_channel_kbps,
            regenerate_statistics=getattr(config, "matroska_regenerate_statistics", True),
        )
        self._profiles  = ProfileManager(config.app_data_dir / "encode_profiles")
        self._executor  = ThreadPoolExecutor(max_workers=1)
        # Le fallback HDR peut attendre un stockage lent. Il ne doit ni
        # retarder la détection matérielle, ni rendre la fermeture de la GUI
        # bloquante.
        self._hdr_meta_executor = ThreadPoolExecutor(max_workers=1)
        self._file_info: FileInfo | None = None
        self._video_tracks: list[tuple[FileInfo, TrackEntry, str]] = []
        self._video_settings_by_entry_id: dict[str, dict[str, object]] = {}
        self._video_force_8bit_by_entry_id: dict[str, bool] = {}
        self._current_video_entry_id: str | None = None
        self._loading_video_settings = False
        self._syncing_video_selectors = False
        self._video_selector_combos: list[QComboBox] = []
        self._video_apply_all = False
        self._audio_tracks_data: list[tuple] = []   # list[tuple[AudioTrack, str, Path, TrackEntry]]
        self._duration_s: float | None = None
        self._hw_encoders: set[str] = set()
        # Callable fourni par MainWindow pour récupérer le chemin de sortie depuis RemuxPanel.
        self._output_provider: Callable[[], Path | None] = lambda: None
        # Callable fourni par MainWindow pour récupérer le titre de fichier depuis RemuxPanel.
        self._file_title_provider: Callable[[], str] = lambda: ""
        # Callable fourni par MainWindow pour récupérer les pièces jointes manuelles depuis RemuxPanel.
        self._extra_attachments_provider: Callable[[], list] = lambda: []
        # Callable fourni par MainWindow pour récupérer la cover TMDB en attente depuis RemuxPanel.
        self._tmdb_cover_provider: "Callable[[], tuple[str, str] | None]" = lambda: None
        # Callable fourni par MainWindow pour récupérer les tag_overrides depuis RemuxPanel.
        self._tag_overrides_provider: Callable[[], "dict | None"] = lambda: None
        # Callable fourni par MainWindow pour récupérer les chapter_overrides depuis RemuxPanel.
        self._chapters_provider: Callable[[], "list | None"] = lambda: None
        # Callable fourni par MainWindow pour le backend de muxage final du job.
        self._mux_backend_provider: Callable[[], str] = lambda: str(
            self._config.matroska_mux_backend
        )
        self._preview_signals: TaskSignals | None = None
        self._preview_random_scene = False
        self._preview_video_path: Path | None = None
        self._preview_captures: list[dict] = []
        self._preview_current_index: int = 0
        self._preview_zoom_percent: int = 100
        self._preview_current_pixmap: QPixmap | None = None
        self._static_hdr_estimate_prompted: set[str] = set()
        self._hdr_meta_probe_generation = 0
        self._hdr_meta_probe_expected: tuple[int, str, str] | None = None
        self._closing = False

        self._sw_encoders: set[str] = {codec_id for codec_id, _ in SOFTWARE_VIDEO_CODECS}
        self._workflow.log_message.connect(self.log_message, Qt.ConnectionType.QueuedConnection)
        self._hw_detected.connect(self._on_hw_detected, Qt.ConnectionType.QueuedConnection)
        self._hdr_meta_frame_probe_ready.connect(
            self._on_hdr_meta_frame_probe_ready,
            Qt.ConnectionType.QueuedConnection,
        )
        self._workflow.static_hdr_estimate_ready.connect(
            self._on_static_hdr_estimate_ready,
            Qt.ConnectionType.QueuedConnection,
        )
        self._workflow.static_hdr_estimate_failed.connect(
            self._on_static_hdr_estimate_failed,
            Qt.ConnectionType.QueuedConnection,
        )

        self._build_ui()
        apply_translations(self)
        self._executor.submit(self._detect_hw_encoders)
        try:
            EncodeWorkflow.cleanup_preview_dir(self._config.work_dir)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Construction de l'interface
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.setStyleSheet(f"background:{_C.BG_DEEP};")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(f"QScrollArea{{background:{_C.BG_DEEP};border:none;}}"
                             f"QScrollBar:vertical{{background:{_C.BG_DEEP};width:6px;border:none;}}"
                             f"QScrollBar::handle:vertical{{background:{_C.BORDER_LT};"
                             f"border-radius:3px;min-height:24px;}}"
                             f"QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}")

        content = QWidget()
        content.setStyleSheet(f"background:{_C.BG_DEEP};")
        cl = QVBoxLayout(content)
        cl.setContentsMargins(28, 24, 28, 24)
        cl.setSpacing(20)
        cl.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)

        # --- Titre ---
        title = QLabel("Encodage Vidéo / Audio")
        title.setStyleSheet(f"font-size:20px;font-weight:800;color:{_C.TEXT_PRI};"
                            f"background:transparent;letter-spacing:-0.3px;")
        subtitle = QLabel("x265 · x264 · SVT-AV1 · NVENC/AMF/QSV — HDR10 · Tone mapping · Audio multicanal")
        subtitle.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:12px;background:transparent;")
        cl.addWidget(title)
        cl.addWidget(subtitle)
        cl.addWidget(_separator())

        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        self._tabs.setUsesScrollButtons(False)
        self._tabs.setStyleSheet(
            f"QTabWidget::pane{{border:0;background:{_C.BG_DEEP};margin-top:8px;}}"
            f"QTabBar::tab{{background:transparent;color:{_C.TEXT_SEC};padding:7px 11px;"
            f"margin-right:4px;border:0;border-bottom:2px solid transparent;}}"
            f"QTabBar::tab:selected{{color:{_C.TEXT_PRI};background:{_C.BG_CARD};"
            f"border-bottom:2px solid {_C.ACCENT};border-radius:4px;}}"
            f"QTabBar::tab:hover{{color:{_C.TEXT_PRI};background:{_C.BG_HOVER};"
            f"border-radius:4px;}}"
        )
        self._tabs.addTab(self._build_sources_audio_tab(), "Sources & Audio")
        self._tabs.addTab(self._build_video_tab(), "Video")
        self._tabs.addTab(self._build_geometry_filters_tab(), "Géométrie / Filtres")
        self._tabs.addTab(self._build_preview_tab(), "Preview / Commande")
        cl.addWidget(self._tabs)
        cl.addStretch()

        scroll.setWidget(content)
        root.addWidget(scroll, stretch=1)

    def _new_tab_page(self) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        page.setStyleSheet(f"background:{_C.BG_DEEP};")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)
        return page, layout

    def _build_sources_audio_tab(self) -> QWidget:
        page, layout = self._new_tab_page()
        layout.addWidget(_section_label("PISTE VIDÉO SOURCE"))
        layout.addWidget(self._build_video_source_card())
        layout.addWidget(_separator())
        layout.addWidget(_section_label("PISTES AUDIO"))
        self._audio_table = _AudioTable(self._config)
        self._audio_table.set_changed_callback(self._rebuild_preview)
        self._audio_table.track_meta_changed.connect(self.audio_track_meta_changed)
        self._audio_table.track_encoding_changed.connect(self.audio_track_encoding_changed)
        self._audio_table.track_removed.connect(self.audio_track_remove_requested)
        layout.addWidget(self._audio_table)

        add_track_row = QHBoxLayout()
        add_track_row.setSpacing(0)
        self._add_audio_btn = _secondary_button("＋  Ajouter une piste…")
        self._add_audio_btn.setEnabled(False)
        self._add_audio_btn.clicked.connect(self._on_add_audio_track)
        add_track_row.addWidget(self._add_audio_btn)
        add_track_row.addStretch()
        layout.addLayout(add_track_row)
        layout.addStretch()
        return page

    def _build_video_tab(self) -> QWidget:
        page, layout = self._new_tab_page()
        layout.addWidget(self._build_video_selector_row())
        layout.addWidget(_section_label("ENCODAGE VIDÉO"))
        layout.addWidget(self._build_video_card())
        layout.addWidget(_separator())
        layout.addWidget(_section_label("HDR"))
        layout.addWidget(self._build_hdr_card())
        layout.addStretch()
        return page

    def _build_geometry_filters_tab(self) -> QWidget:
        page, layout = self._new_tab_page()
        layout.addWidget(self._build_video_selector_row())
        self._geometry_copy_msg = self._build_transform_message_label()
        self._filters_copy_msg = self._geometry_copy_msg
        layout.addWidget(self._geometry_copy_msg)
        layout.addWidget(self._build_geometry_card())
        layout.addWidget(self._build_filters_card())
        layout.addStretch()
        return page

    def _build_preview_tab(self) -> QWidget:
        page, layout = self._new_tab_page()
        layout.addWidget(self._build_video_selector_row())

        layout.addWidget(_section_label("PREVIEW RÉELLE"))
        preview_card = _card()
        preview_layout = QVBoxLayout(preview_card)
        preview_layout.setContentsMargins(14, 12, 14, 12)
        preview_layout.setSpacing(10)

        controls = QGridLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setHorizontalSpacing(10)
        controls.setVerticalSpacing(8)

        mode_label = QLabel("Mode")
        mode_label.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        self._preview_mode_combo = QComboBox()
        self._preview_mode_combo.setObjectName("PreviewModeCombo")
        self._preview_mode_combo.setStyleSheet(_combo_style())
        self._preview_mode_combo.addItem("Image", "image")
        self._preview_mode_combo.addItem("Vidéo", "video")
        self._preview_mode_combo.currentIndexChanged.connect(self._on_preview_mode_changed)
        controls.addWidget(mode_label, 0, 0)
        controls.addWidget(self._preview_mode_combo, 1, 0)

        time_label = QLabel("Scène")
        time_label.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        time_row = QHBoxLayout()
        time_row.setContentsMargins(0, 0, 0, 0)
        time_row.setSpacing(6)
        self._preview_time_edit = QLineEdit("00:00:00.000")
        self._preview_time_edit.setObjectName("PreviewTimeEdit")
        self._preview_time_edit.setStyleSheet(_input_style())
        self._preview_time_edit.setPlaceholderText("HH:MM:SS.mmm")
        self._preview_time_edit.textEdited.connect(self._on_preview_time_edited)
        time_row.addWidget(self._preview_time_edit, 1)
        self._preview_random_btn = _secondary_button("Hasard")
        self._preview_random_btn.setObjectName("PreviewRandomButton")
        self._preview_random_btn.clicked.connect(self._on_random_preview_scene)
        time_row.addWidget(self._preview_random_btn)
        time_widget = QWidget()
        time_widget.setStyleSheet("background:transparent;")
        time_widget.setLayout(time_row)
        controls.addWidget(time_label, 0, 1)
        controls.addWidget(time_widget, 1, 1)

        duration_label = QLabel("Durée vidéo")
        duration_label.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        self._preview_duration_spin = QSpinBox()
        self._preview_duration_spin.setObjectName("PreviewDurationSpin")
        self._preview_duration_spin.setRange(5, 30)
        self._preview_duration_spin.setValue(10)
        self._preview_duration_spin.setSuffix(" s")
        self._preview_duration_spin.setStyleSheet(_input_style())
        controls.addWidget(duration_label, 0, 2)
        controls.addWidget(self._preview_duration_spin, 1, 2)

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(8)
        self._preview_generate_btn = _primary_button("Générer")
        self._preview_generate_btn.setObjectName("PreviewGenerateButton")
        self._preview_generate_btn.clicked.connect(self._on_generate_preview)
        self._preview_cancel_btn = _secondary_button("Annuler")
        self._preview_cancel_btn.setObjectName("PreviewCancelButton")
        self._preview_cancel_btn.clicked.connect(self._on_cancel_preview)
        self._preview_cancel_btn.setEnabled(False)
        action_row.addWidget(self._preview_generate_btn)
        action_row.addWidget(self._preview_cancel_btn)
        action_row.addStretch()
        action_widget = QWidget()
        action_widget.setStyleSheet("background:transparent;")
        action_widget.setLayout(action_row)
        controls.addWidget(action_widget, 1, 3)
        controls.setColumnStretch(1, 1)
        preview_layout.addLayout(controls)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(10)
        self._preview_status = QLabel("Prêt.")
        self._preview_status.setObjectName("PreviewStatusLabel")
        self._preview_status.setWordWrap(True)
        self._preview_status.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        self._preview_progress = QProgressBar()
        self._preview_progress.setObjectName("PreviewProgressBar")
        self._preview_progress.setRange(0, 100)
        self._preview_progress.setValue(0)
        self._preview_progress.setFixedWidth(180)
        self._preview_progress.setFixedHeight(14)
        self._preview_progress.setTextVisible(True)
        self._preview_progress.setFormat("%p %")
        self._preview_progress.setVisible(False)
        self._preview_progress.setStyleSheet(
            f"QProgressBar{{background:{_C.BG_CARD};border:1px solid {_C.BORDER};"
            f"border-radius:5px;color:{_C.TEXT_PRI};font-size:10px;text-align:center;}}"
            f"QProgressBar::chunk{{background:{_C.ACCENT};border-radius:4px;}}"
        )
        status_row.addWidget(self._preview_status, 1)
        status_row.addWidget(self._preview_progress)
        preview_layout.addLayout(status_row)

        self._preview_scene_status = QLabel("")
        self._preview_scene_status.setObjectName("PreviewSceneStatusLabel")
        self._preview_scene_status.setWordWrap(True)
        self._preview_scene_status.setStyleSheet(f"color:{_C.TEXT_DIM};font-size:11px;background:transparent;")
        preview_layout.addWidget(self._preview_scene_status)

        self._preview_image = QLabel("Aucune image générée")
        self._preview_image.setObjectName("PreviewImageLabel")
        self._preview_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_image.setMinimumSize(0, 0)
        self._preview_image.setStyleSheet(
            f"QLabel{{background:{_C.BG_DEEP};color:{_C.TEXT_DIM};padding:10px;}}"
        )
        self._preview_scroll = QScrollArea()
        self._preview_scroll.setObjectName("PreviewImageScroll")
        self._preview_scroll.setWidget(self._preview_image)
        self._preview_scroll.setWidgetResizable(True)
        self._preview_scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_scroll.setMinimumHeight(299)
        self._preview_scroll.setStyleSheet(
            f"QScrollArea{{background:{_C.BG_DEEP};border:1px solid {_C.BORDER};border-radius:6px;}}"
            f"QScrollBar:vertical,QScrollBar:horizontal{{background:{_C.BG_DEEP};width:8px;height:8px;border:none;}}"
            f"QScrollBar::handle{{background:{_C.BORDER_LT};border-radius:4px;min-height:24px;min-width:24px;}}"
            f"QScrollBar::add-line,QScrollBar::sub-line{{height:0;width:0;}}"
        )
        preview_layout.addWidget(self._preview_scroll, 1)

        nav_row = QHBoxLayout()
        nav_row.setContentsMargins(0, 0, 0, 0)
        nav_row.setSpacing(8)
        self._preview_prev_btn = _secondary_button("◀")
        self._preview_prev_btn.setObjectName("PreviewPrevButton")
        self._preview_prev_btn.setFixedWidth(46)
        self._preview_prev_btn.clicked.connect(self._on_preview_prev)
        self._preview_prev_btn.setEnabled(False)
        self._preview_next_btn = _secondary_button("▶")
        self._preview_next_btn.setObjectName("PreviewNextButton")
        self._preview_next_btn.setFixedWidth(46)
        self._preview_next_btn.clicked.connect(self._on_preview_next)
        self._preview_next_btn.setEnabled(False)
        self._preview_index_label = QLabel("— / —")
        self._preview_index_label.setObjectName("PreviewIndexLabel")
        self._preview_index_label.setStyleSheet(
            f"color:{_C.TEXT_PRI};font-size:11px;font-family:'JetBrains Mono',monospace;"
            f"background:transparent;min-width:60px;"
        )
        self._preview_index_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_index_label.setFixedWidth(72)
        zoom_lbl = QLabel("Zoom")
        zoom_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        self._preview_zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self._preview_zoom_slider.setObjectName("PreviewZoomSlider")
        self._preview_zoom_slider.setRange(10, 400)
        self._preview_zoom_slider.setSingleStep(10)
        self._preview_zoom_slider.setPageStep(25)
        self._preview_zoom_slider.setValue(100)
        self._preview_zoom_slider.setMinimumWidth(180)
        self._preview_zoom_slider.valueChanged.connect(self._on_preview_zoom_changed)
        self._preview_zoom_value_lbl = QLabel("100 %")
        self._preview_zoom_value_lbl.setObjectName("PreviewZoomValueLabel")
        self._preview_zoom_value_lbl.setStyleSheet(
            f"color:{_C.TEXT_PRI};font-size:11px;font-family:'JetBrains Mono',monospace;"
            f"background:transparent;min-width:54px;"
        )
        self._preview_zoom_value_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_zoom_fit_btn = _secondary_button("Ajuster")
        self._preview_zoom_fit_btn.setObjectName("PreviewZoomFitButton")
        self._preview_zoom_fit_btn.clicked.connect(self._on_preview_zoom_fit)
        nav_row.addWidget(self._preview_prev_btn)
        nav_row.addWidget(self._preview_index_label)
        nav_row.addWidget(self._preview_next_btn)
        nav_row.addStretch()
        nav_row.addWidget(zoom_lbl)
        nav_row.addWidget(self._preview_zoom_slider, 1)
        nav_row.addWidget(self._preview_zoom_value_lbl)
        nav_row.addWidget(self._preview_zoom_fit_btn)
        preview_layout.addLayout(nav_row)

        video_result_row = QHBoxLayout()
        video_result_row.setContentsMargins(0, 0, 0, 0)
        video_result_row.setSpacing(8)
        video_lbl = QLabel("Vidéo :")
        video_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        self._preview_video_path_label = QLabel("")
        self._preview_video_path_label.setObjectName("PreviewVideoPathLabel")
        self._preview_video_path_label.setWordWrap(True)
        self._preview_video_path_label.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        self._preview_open_video_btn = _secondary_button("Ouvrir")
        self._preview_open_video_btn.setObjectName("PreviewOpenVideoButton")
        self._preview_open_video_btn.clicked.connect(self._open_preview_video)
        self._preview_open_video_btn.setEnabled(False)
        video_result_row.addWidget(video_lbl)
        video_result_row.addWidget(self._preview_video_path_label, 1)
        video_result_row.addWidget(self._preview_open_video_btn)
        video_result = QWidget()
        video_result.setObjectName("PreviewVideoResultRow")
        video_result.setStyleSheet("background:transparent;")
        video_result.setLayout(video_result_row)
        preview_layout.addWidget(video_result)
        self._preview_video_result_row = video_result

        layout.addWidget(preview_card)
        layout.addWidget(_separator())

        cmd_row = QHBoxLayout()
        cmd_row.addWidget(_section_label("APERÇU COMMANDE"))
        cmd_row.addStretch()
        copy_btn = _secondary_button("Copier")
        copy_btn.clicked.connect(self._copy_command)
        cmd_row.addWidget(copy_btn)
        layout.addLayout(cmd_row)

        self._cmd_preview = QPlainTextEdit()
        self._cmd_preview.setReadOnly(True)
        self._cmd_preview.setFixedHeight(220)
        mono = QFont("JetBrains Mono", 9)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._cmd_preview.setFont(mono)
        self._cmd_preview.setStyleSheet(
            f"QPlainTextEdit{{background:{_C.BG_DEEP};color:{_C.TEXT_SEC};"
            f"border:1px solid {_C.BORDER};border-radius:6px;padding:8px 12px;}}"
        )
        self._cmd_preview.setPlaceholderText(
            "Sélectionnez un fichier source et configurez l'encodage…"
        )
        layout.addWidget(self._cmd_preview)
        self._on_preview_mode_changed()
        layout.addStretch()
        return page

    def _build_video_source_card(self) -> QWidget:
        """Sélecteur de piste vidéo alimenté par l'onglet Conteneur."""
        card = _card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)

        self._video_list = QListWidget()
        self._video_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._video_list.setUniformItemSizes(True)
        self._video_list.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self._video_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._video_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._video_list.setStyleSheet(
            f"QListWidget{{background:{_C.BG_CARD};border:none;border-radius:6px;"
            f"color:{_C.TEXT_PRI};font-size:11px;font-family:'JetBrains Mono',monospace;}}"
            f"QListWidget::item{{padding:8px 12px;border-bottom:1px solid {_C.BORDER};}}"
            f"QListWidget::item:selected{{background:{_C.ACCENT_DIM};}}"
            f"QListWidget::item:hover{{background:{_C.BG_HOVER};}}"
            f"QScrollBar:vertical{{background:{_C.BG_CARD};width:6px;border:none;}}"
            f"QScrollBar::handle:vertical{{background:{_C.BORDER_LT};"
            f"border-radius:3px;min-height:24px;}}"
            f"QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}"
        )
        self._video_list.currentRowChanged.connect(self._on_video_row_changed)
        cl.addWidget(self._video_list)

        self._video_placeholder = QLabel(
            "Aucune piste vidéo — sélectionnez des fichiers dans l'onglet Conteneur"
        )
        self._video_placeholder.setStyleSheet(
            f"color:{_C.TEXT_DIM};font-size:11px;padding:14px;"
            f"background:transparent;"
        )
        self._video_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(self._video_placeholder)

        self._video_list.setVisible(False)
        self._video_placeholder.setVisible(True)
        return card

    def _build_video_selector_row(self) -> QWidget:
        row = QWidget()
        row.setStyleSheet("background:transparent;")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        label = QLabel("Piste vidéo")
        label.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        layout.addWidget(label)
        combo = QComboBox()
        combo.setStyleSheet(_combo_style())
        combo.setMinimumWidth(360)
        combo.currentIndexChanged.connect(lambda idx, c=combo: self._on_video_selector_changed(c, idx))
        self._video_selector_combos.append(combo)
        layout.addWidget(combo, 1)
        layout.addStretch()
        return row

    def _on_video_selector_changed(self, combo: QComboBox, index: int) -> None:
        if self._syncing_video_selectors or index < 0:
            return
        data = combo.itemData(index)
        try:
            row = int(data)
        except (TypeError, ValueError):
            return
        if hasattr(self, "_video_list") and 0 <= row < self._video_list.count():
            self._video_list.setCurrentRow(row)

    def _sync_video_selector_items(self) -> None:
        if not self._video_selector_combos:
            return
        self._syncing_video_selectors = True
        try:
            current = self._video_list.currentRow() if hasattr(self, "_video_list") else -1
            for combo in self._video_selector_combos:
                combo.blockSignals(True)
                combo.clear()
                for row, (file_info, track, _color) in enumerate(self._video_tracks):
                    codec = (track.orig_codec or track.codec or "").upper()
                    combo.addItem(f"{file_info.path.name} · #{track.mkv_tid} · {codec}", row)
                combo.setEnabled(bool(self._video_tracks))
                if 0 <= current < combo.count():
                    combo.setCurrentIndex(current)
                combo.blockSignals(False)
        finally:
            self._syncing_video_selectors = False

    def _set_video_selector_row(self, row: int) -> None:
        if not self._video_selector_combos:
            return
        self._syncing_video_selectors = True
        try:
            for combo in self._video_selector_combos:
                if 0 <= row < combo.count():
                    combo.blockSignals(True)
                    combo.setCurrentIndex(row)
                    combo.blockSignals(False)
        finally:
            self._syncing_video_selectors = False

    # ------------------------------------------------------------------
    # API publique — appelée par MainWindow depuis RemuxPanel
    # ------------------------------------------------------------------

    def set_video_tracks(self, tracks: list[tuple]) -> None:
        """Met à jour la liste des pistes vidéo depuis l'onglet Conteneur."""
        self._save_current_video_state()
        selected_entry_id = self._current_video_entry_id
        self._video_tracks = tracks
        self._video_list.blockSignals(True)
        self._video_list.clear()

        if not tracks:
            self._video_list.setVisible(False)
            self._video_placeholder.setVisible(True)
            self._adjust_video_list_height()
            self._sync_video_selector_items()
            self._file_info = None
            self.ready_changed.emit(False)
            self._video_list.blockSignals(False)
            self._current_video_entry_id = None
            self._video_force_8bit_by_entry_id.clear()
            self.video_tracks_encoding_changed.emit([])
            self._rebuild_preview()
            return

        self._video_placeholder.setVisible(False)
        self._video_list.setVisible(True)

        for file_info, track, color in tracks:
            state = self._video_settings_by_entry_id.get(self._video_entry_id(track))
            text = self._video_source_row_text(file_info, track, state=state)
            item = QListWidgetItem(text)
            item.setSizeHint(QSize(0, self._VIDEO_SOURCE_ROW_H))
            item.setData(Qt.ItemDataRole.UserRole, (file_info, track))
            item.setForeground(QBrush(QColor(color)))
            self._apply_video_source_item_style(item, state)
            self._video_list.addItem(item)

        active_ids = {self._video_entry_id(track) for _info, track, _color in tracks}
        self._video_settings_by_entry_id = {
            entry_id: settings
            for entry_id, settings in self._video_settings_by_entry_id.items()
            if entry_id in active_ids
        }
        self._video_force_8bit_by_entry_id = {
            entry_id: forced
            for entry_id, forced in self._video_force_8bit_by_entry_id.items()
            if entry_id in active_ids
        }
        target_row = 0
        if selected_entry_id is not None:
            for row, (_info, track, _color) in enumerate(tracks):
                if self._video_entry_id(track) == selected_entry_id:
                    target_row = row
                    break

        self._video_list.setCurrentRow(target_row)
        self._video_list.blockSignals(False)
        self._adjust_video_list_height()
        self._sync_video_selector_items()
        self._on_video_row_changed(target_row)
        self._ensure_video_states_for_active_tracks()

    def _adjust_video_list_height(self) -> None:
        """Ajuste la hauteur de la liste vidéo, avec scrollbar au-delà de 10 lignes."""
        n = self._video_list.count()
        if n == 0:
            self._video_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._video_list.setFixedHeight(0)
            return
        row_h = self._VIDEO_SOURCE_ROW_H
        visible = min(n, self._MAX_VISIBLE_VIDEO_SOURCE_ROWS)
        frame_h = self._video_list.frameWidth() * 2
        self._video_list.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
            if n > self._MAX_VISIBLE_VIDEO_SOURCE_ROWS
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._video_list.setFixedHeight(visible * row_h + frame_h + 2)

    def _on_video_row_changed(self, row: int) -> None:
        if row < 0 or row >= len(self._video_tracks):
            return
        self._save_current_video_state()
        file_info, track, _color = self._video_tracks[row]
        self._current_video_entry_id = self._video_entry_id(track)
        self._set_video_selector_row(row)
        self._apply_file_info(file_info, track)

    def _apply_file_info(self, info: FileInfo, track: TrackEntry | None = None) -> None:
        """Applique les infos d'un FileInfo sélectionné comme source d'encodage."""
        # Un éventuel fallback ffprobe de la piste précédemment sélectionnée
        # ne doit jamais mettre à jour les champs HDR de la nouvelle piste.
        self._invalidate_hdr_meta_frame_probe()
        self._file_info  = info
        self._duration_s = info.duration_s

        selected_video = (
            self._video_track_for_entry(info, track)
            if track is not None
            else info.primary_video
        )
        settings = (
            self._video_settings_by_entry_id.get(self._current_video_entry_id)
            if self._current_video_entry_id is not None
            else None
        )
        if settings is None and selected_video:
            self._prefill_hdr_meta(
                selected_video.raw,
                info.path,
                info.mediainfo_json,
                probe_frames=selected_video.hdr_type != HDRType.NONE,
            )

        pass  # Fichier de sortie géré par RemuxPanel

        self.ready_changed.emit(True)
        if settings is not None:
            self._apply_video_state(settings)
            self._update_passthrough_controls(auto_check=False)
        else:
            self._update_passthrough_controls(auto_check=True)
            self._save_current_video_state()
            if self._video_apply_all:
                self._propagate_current_video_state_to_all(force_current=False)
        hdr_lbl = (
            selected_video.hdr_label if selected_video is not None
            else (info.primary_video.hdr_label if info.primary_video else info.hdr_type.label())
        )
        self.log_message.emit(
            "OK",
            f"{info.path.name} — "
            f"{len(info.video_tracks)}V  {len(info.audio_tracks)}A  "
            f"{len(info.subtitle_tracks)}S  {hdr_lbl}",
        )
        self._rebuild_preview()

    def set_audio_tracks(self, tracks: list[tuple]) -> None:
        """Met à jour les pistes audio depuis les pistes activées dans l'onglet Conteneur.
        tracks : list[tuple[AudioTrack, str, Path, TrackEntry]]
        """
        self._audio_tracks_data = tracks
        self._add_audio_btn.setEnabled(any(self._is_original_audio_source(t) for t in tracks))

        default_codec   = "copy"
        default_bitrate = None
        profile_name = self._profile_combo.currentText()
        if profile_name:
            for p in self._profiles.load_all():
                if p.name == profile_name:
                    default_codec   = p.default_audio_codec
                    default_bitrate = p.default_audio_bitrate_kbps
                    break

        self._audio_table.load_tracks(tracks, default_codec, default_bitrate)
        self._audio_table.emit_encoding_plans()
        self._rebuild_preview()

    # ------------------------------------------------------------------
    # Carte encodage vidéo
    # ------------------------------------------------------------------

    def _build_video_card(self) -> QWidget:
        card = _card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(12)

        # Ligne codec (toujours visible)
        r1 = QHBoxLayout()
        r1.setSpacing(12)
        codec_lbl = QLabel("Codec")
        codec_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        r1.addWidget(codec_lbl)
        self._codec_combo = QComboBox()
        self._codec_combo.setStyleSheet(_combo_style())
        self._codec_combo.setMinimumWidth(220)
        self._populate_codec_combo()
        self._codec_combo.currentIndexChanged.connect(self._on_codec_changed)
        r1.addWidget(self._codec_combo)
        self._apply_all_video_cb = QCheckBox("Appliquer à toutes les pistes")
        self._apply_all_video_cb.setChecked(False)
        self._apply_all_video_cb.setStyleSheet(_checkbox_style())
        self._apply_all_video_cb.toggled.connect(self._on_apply_all_video_toggled)
        r1.addWidget(self._apply_all_video_cb)
        r1.addStretch()
        cl.addLayout(r1)

        # Contrôles d'encodage (masqués quand codec = copy)
        self._video_encode_controls = QWidget()
        self._video_encode_controls.setStyleSheet("background:transparent;")
        enc_cl = QVBoxLayout(self._video_encode_controls)
        enc_cl.setContentsMargins(0, 0, 0, 0)
        enc_cl.setSpacing(12)

        rp = QHBoxLayout()
        rp.setSpacing(12)
        self._ten_bit_cb = QCheckBox("10-Bits")
        self._ten_bit_cb.setStyleSheet(_checkbox_style())
        self._ten_bit_cb.setToolTip(
            "Forcer une sortie 10-bit (profil main10/high10 + pix_fmt p010le/yuv420p10le).\n"
            "Désactivé pour le codec Copy ou les codecs sans support 10-bit."
        )
        self._ten_bit_cb.toggled.connect(self._on_ten_bit_toggled)
        rp.addWidget(self._ten_bit_cb)
        preset_lbl = QLabel("Preset")
        preset_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        rp.addWidget(preset_lbl)
        self._preset_combo = QComboBox()
        self._preset_combo.setStyleSheet(_combo_style())
        self._preset_combo.setMinimumWidth(120)
        rp.addWidget(self._preset_combo)
        rp.addStretch()
        enc_cl.addLayout(rp)

        # Ligne mode qualité
        r2 = QHBoxLayout()
        r2.setSpacing(12)
        mode_lbl = QLabel("Mode")
        mode_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        self._mode_combo = QComboBox()
        self._mode_combo.setStyleSheet(_combo_style())
        # Peuplé dynamiquement par _refresh_mode_combo() — le mode CQ
        # n'apparaît que pour les encodeurs HW.
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        r2.addWidget(mode_lbl)
        r2.addWidget(self._mode_combo)
        r2.addSpacing(16)

        # Valeur qualité (stack : CRF slider+spin / bitrate edit / size edit)
        self._quality_stack = QStackedWidget()

        # Page 0 : CRF
        crf_w = QWidget()
        crf_w.setStyleSheet("background:transparent;")
        crf_l = QHBoxLayout(crf_w)
        crf_l.setContentsMargins(0, 0, 0, 0)
        crf_l.setSpacing(8)
        self._crf_slider = QSlider(Qt.Orientation.Horizontal)
        self._crf_slider.setRange(0, 51)
        self._crf_slider.setValue(18)
        self._crf_slider.setFixedWidth(160)
        self._crf_slider.setStyleSheet(
            f"QSlider::groove:horizontal{{height:4px;background:{_C.BG_ACTIVE};"
            f"border-radius:2px;}}"
            f"QSlider::handle:horizontal{{width:14px;height:14px;margin:-5px 0;"
            f"background:{_C.ACCENT};border-radius:7px;}}"
            f"QSlider::sub-page:horizontal{{background:{_C.ACCENT};border-radius:2px;}}"
        )
        self._crf_spin = QSpinBox()
        self._crf_spin.setRange(0, 51)
        self._crf_spin.setValue(18)
        self._crf_spin.setFixedWidth(52)
        self._crf_spin.setStyleSheet(
            f"QSpinBox{{background:{_C.BG_CARD};color:{_C.TEXT_PRI};"
            f"border:1px solid {_C.BORDER};border-radius:4px;padding:2px 4px;}}"
        )
        self._crf_slider.valueChanged.connect(self._crf_spin.setValue)
        self._crf_spin.valueChanged.connect(self._crf_slider.setValue)
        self._crf_slider.valueChanged.connect(lambda _: self._rebuild_preview())
        crf_l.addWidget(self._crf_slider)
        crf_l.addWidget(self._crf_spin)
        self._quality_stack.addWidget(crf_w)

        # Page 1 : CQ (HW only — qualité constante côté NVENC/AMF/QSV/VAAPI)
        cq_w = QWidget()
        cq_w.setStyleSheet("background:transparent;")
        cq_l = QHBoxLayout(cq_w)
        cq_l.setContentsMargins(0, 0, 0, 0)
        cq_l.setSpacing(8)
        self._cq_slider = QSlider(Qt.Orientation.Horizontal)
        self._cq_slider.setRange(0, 51)
        self._cq_slider.setValue(26)
        self._cq_slider.setFixedWidth(160)
        self._cq_slider.setStyleSheet(
            f"QSlider::groove:horizontal{{height:4px;background:{_C.BG_ACTIVE};"
            f"border-radius:2px;}}"
            f"QSlider::handle:horizontal{{width:14px;height:14px;margin:-5px 0;"
            f"background:{_C.ACCENT};border-radius:7px;}}"
            f"QSlider::sub-page:horizontal{{background:{_C.ACCENT};border-radius:2px;}}"
        )
        self._cq_spin = QSpinBox()
        self._cq_spin.setRange(0, 51)
        self._cq_spin.setValue(26)
        self._cq_spin.setFixedWidth(52)
        self._cq_spin.setStyleSheet(
            f"QSpinBox{{background:{_C.BG_CARD};color:{_C.TEXT_PRI};"
            f"border:1px solid {_C.BORDER};border-radius:4px;padding:2px 4px;}}"
        )
        self._cq_slider.valueChanged.connect(self._cq_spin.setValue)
        self._cq_spin.valueChanged.connect(self._cq_slider.setValue)
        self._cq_slider.valueChanged.connect(lambda _: self._rebuild_preview())
        cq_l.addWidget(self._cq_slider)
        cq_l.addWidget(self._cq_spin)
        self._quality_stack.addWidget(cq_w)

        # Page 2 : Bitrate
        br_w = QWidget()
        br_w.setStyleSheet("background:transparent;")
        br_l = QHBoxLayout(br_w)
        br_l.setContentsMargins(0, 0, 0, 0)
        br_l.setSpacing(6)
        self._bitrate_edit = QLineEdit("5000")
        self._bitrate_edit.setStyleSheet(_input_style())
        self._bitrate_edit.setFixedWidth(100)
        self._bitrate_edit.textChanged.connect(lambda _: self._rebuild_preview())
        br_lbl = QLabel("kbps")
        br_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        br_l.addWidget(self._bitrate_edit)
        br_l.addWidget(br_lbl)
        self._quality_stack.addWidget(br_w)

        # Page 3 : Taille cible
        sz_w = QWidget()
        sz_w.setStyleSheet("background:transparent;")
        sz_l = QHBoxLayout(sz_w)
        sz_l.setContentsMargins(0, 0, 0, 0)
        sz_l.setSpacing(6)
        self._size_edit = QLineEdit("4000")
        self._size_edit.setStyleSheet(_input_style())
        self._size_edit.setFixedWidth(100)
        self._size_edit.textChanged.connect(lambda _: self._rebuild_preview())
        sz_lbl = QLabel("Mo")
        sz_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        sz_l.addWidget(self._size_edit)
        sz_l.addWidget(sz_lbl)
        self._quality_stack.addWidget(sz_w)

        r2.addWidget(self._quality_stack)
        r2.addStretch()
        enc_cl.addLayout(r2)

        # Params avancés
        adv_lbl = QLabel("Params avancés  (x265-params / svtav1-params / flags HW)")
        adv_lbl.setStyleSheet(f"color:{_C.TEXT_DIM};font-size:10px;background:transparent;")
        enc_cl.addWidget(adv_lbl)
        adv_row = QHBoxLayout()
        adv_row.setContentsMargins(0, 0, 0, 0)
        adv_row.setSpacing(8)
        self._extra_params = QLineEdit()
        self._extra_params.setPlaceholderText("ex. no-open-gop=1:hdr10=1:hdr10-opt=1  ·  -spatial-aq 1 -rc-lookahead 32")
        self._extra_params.setStyleSheet(_input_style())
        self._extra_params.textChanged.connect(lambda _: self._rebuild_preview())
        adv_row.addWidget(self._extra_params, 1)
        self._edit_extra_btn = _secondary_button("Éditer…")
        self._edit_extra_btn.setToolTip("Ouvrir l'éditeur visuel de paramètres pour le codec sélectionné")
        self._edit_extra_btn.clicked.connect(self._open_extra_params_dialog)
        adv_row.addWidget(self._edit_extra_btn)
        enc_cl.addLayout(adv_row)

        # Profils — section repliée à l'intérieur des contrôles d'encodage
        profiles_lbl = QLabel("Profils d'encodage")
        profiles_lbl.setStyleSheet(f"color:{_C.TEXT_DIM};font-size:10px;background:transparent;")
        enc_cl.addWidget(profiles_lbl)
        enc_cl.addWidget(self._build_profiles_row())

        cl.addWidget(self._video_encode_controls)

        self._on_codec_changed()   # initialise preset combo + visibility
        return card

    def _build_hdr_card(self) -> QWidget:
        card = _card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(10)

        # 1. Injection métadonnées HDR10 statiques
        self._inject_hdr_cb = QCheckBox("Injecter les métadonnées HDR10 statiques (ST 2086 / MaxCLL)")
        self._inject_hdr_cb.setStyleSheet(_checkbox_style())
        self._inject_hdr_cb.stateChanged.connect(self._on_hdr_toggle)
        cl.addWidget(self._inject_hdr_cb)

        self._hdr_meta_widget = QWidget()
        self._hdr_meta_widget.setStyleSheet("background:transparent;")
        hm_l = QVBoxLayout(self._hdr_meta_widget)
        hm_l.setContentsMargins(20, 4, 0, 4)
        hm_l.setSpacing(6)

        r_md = QHBoxLayout()
        r_md.setSpacing(8)
        md_lbl = QLabel("Master Display")
        md_lbl.setFixedWidth(110)
        md_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        self._master_display = QLineEdit()
        self._master_display.setPlaceholderText(
            "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(40000000,50)"
        )
        self._master_display.setStyleSheet(_input_style())
        self._master_display.textChanged.connect(lambda _: self._rebuild_preview())
        r_md.addWidget(md_lbl)
        r_md.addWidget(self._master_display)
        hm_l.addLayout(r_md)

        r_cll = QHBoxLayout()
        r_cll.setSpacing(8)
        cll_lbl = QLabel("MaxCLL / MaxFALL")
        cll_lbl.setFixedWidth(110)
        cll_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        self._max_cll = QLineEdit()
        self._max_cll.setPlaceholderText("1000,400")
        self._max_cll.setFixedWidth(160)
        self._max_cll.setStyleSheet(_input_style())
        self._max_cll.textChanged.connect(lambda _: self._rebuild_preview())
        r_cll.addWidget(cll_lbl)
        r_cll.addWidget(self._max_cll)
        r_cll.addStretch()
        hm_l.addLayout(r_cll)

        self._hdr_meta_widget.setVisible(False)
        cl.addWidget(self._hdr_meta_widget)

        cl.addWidget(_separator())

        # 2. Passthrough Dolby Vision RPU
        self._copy_dv_cb = QCheckBox("Copier le RPU Dolby Vision depuis la source")
        self._copy_dv_cb.setStyleSheet(_checkbox_style())
        self._copy_dv_cb.setEnabled(False)
        self._copy_dv_cb.stateChanged.connect(self._on_dv_toggle)
        cl.addWidget(self._copy_dv_cb)

        self._dovi_profile_widget = QWidget()
        self._dovi_profile_widget.setStyleSheet("background:transparent;")
        dp_l = QHBoxLayout(self._dovi_profile_widget)
        dp_l.setContentsMargins(20, 0, 0, 0)
        dp_l.setSpacing(8)
        dp_lbl = QLabel("Profil dovi_tool")
        dp_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        self._dovi_profile_combo = QComboBox()
        self._dovi_profile_combo.setStyleSheet(_combo_style())
        self._dovi_profile_combo.addItem("Conserver le profil source (par défaut)", "0")
        self._dovi_profile_combo.addItem("Normaliser en P8.1 (supprimer FEL·MEL)", "2")
        self._dovi_profile_combo.currentIndexChanged.connect(self._on_dovi_profile_changed)
        dp_l.addWidget(dp_lbl)
        dp_l.addWidget(self._dovi_profile_combo)
        dp_l.addStretch()
        self._dovi_profile_widget.setVisible(False)
        cl.addWidget(self._dovi_profile_widget)

        # 3. Passthrough HDR10+ SEI
        self._copy_hdr10plus_cb = QCheckBox("Copier les métadonnées HDR10+ depuis la source")
        self._copy_hdr10plus_cb.setStyleSheet(_checkbox_style())
        self._copy_hdr10plus_cb.setEnabled(False)
        self._copy_hdr10plus_cb.stateChanged.connect(lambda _: self._rebuild_preview())
        cl.addWidget(self._copy_hdr10plus_cb)

        cl.addWidget(_separator())

        # 4. Tone mapping HDR→SDR
        self._tonemap_cb = QCheckBox("Tone-mapping HDR → SDR  (zscale + tonemap)")
        self._tonemap_cb.setStyleSheet(_checkbox_style())
        self._tonemap_cb.stateChanged.connect(self._on_tonemap_toggle)
        cl.addWidget(self._tonemap_cb)

        self._tonemap_algo_widget = QWidget()
        self._tonemap_algo_widget.setStyleSheet("background:transparent;")
        ta_l = QHBoxLayout(self._tonemap_algo_widget)
        ta_l.setContentsMargins(20, 0, 0, 0)
        ta_l.setSpacing(8)
        algo_lbl = QLabel("Algorithme")
        algo_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        self._tonemap_algo = QComboBox()
        self._tonemap_algo.setStyleSheet(_combo_style())
        for algo in TONEMAP_ALGORITHMS:
            self._tonemap_algo.addItem(algo, algo)
        self._tonemap_algo.currentIndexChanged.connect(lambda _: self._rebuild_preview())
        ta_l.addWidget(algo_lbl)
        ta_l.addWidget(self._tonemap_algo)
        ta_l.addStretch()
        self._tonemap_algo_widget.setVisible(False)
        cl.addWidget(self._tonemap_algo_widget)

        return card

    @staticmethod
    def _copy_transform_message() -> str:
        return (
            "Options indisponibles en mode Copy. Choisissez un codec d'encodage "
            "dans l'onglet Video pour activer la géométrie et les filtres."
        )

    def _build_transform_message_label(self) -> QLabel:
        label = QLabel(self._copy_transform_message())
        label.setWordWrap(True)
        label.setStyleSheet(
            f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;"
            f"border:none;padding:0 0 2px 0;"
        )
        return label

    def _build_transform_surface(self, title: str) -> tuple[QWidget, QVBoxLayout]:
        surface = QWidget()
        surface.setObjectName("TransformSurface")
        surface.setStyleSheet(
            f"QWidget#TransformSurface{{background:{_C.BG_CARD};border:none;border-radius:6px;}}"
        )
        layout = QVBoxLayout(surface)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(14)

        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"color:{_C.TEXT_PRI};font-size:12px;font-weight:700;"
            f"letter-spacing:0;background:transparent;"
        )
        layout.addWidget(title_label)
        return surface, layout

    def _build_geometry_card(self) -> QWidget:
        card, cl = self._build_transform_surface("GÉOMÉTRIE")

        self._geometry_controls = QWidget()
        self._geometry_controls.setStyleSheet("background:transparent;")
        gl = QVBoxLayout(self._geometry_controls)
        gl.setContentsMargins(0, 0, 0, 0)
        gl.setSpacing(14)

        self._resize_enabled_cb = QCheckBox("Redimensionner")
        self._resize_enabled_cb.setStyleSheet(_checkbox_style())
        self._resize_enabled_cb.toggled.connect(lambda _: self._rebuild_preview())

        resize_head = QHBoxLayout()
        resize_head.setSpacing(10)
        resize_head.addWidget(self._resize_enabled_cb)
        resize_head.addStretch()
        resize_mode_lbl = QLabel("Mode")
        resize_mode_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        resize_head.addWidget(resize_mode_lbl)
        self._resize_mode_combo = QComboBox()
        self._resize_mode_combo.setStyleSheet(_combo_style())
        self._resize_mode_combo.setToolTip("Choisit le type de redimensionnement à afficher et appliquer.")
        self._resize_mode_combo.addItem("Preset", "preset")
        self._resize_mode_combo.addItem("%", "percent")
        self._resize_mode_combo.addItem("WxH", "size")
        self._resize_mode_combo.currentIndexChanged.connect(self._on_resize_mode_changed)
        self._resize_mode_combo.setFixedWidth(120)
        resize_head.addWidget(self._resize_mode_combo)
        resize_algo_lbl = QLabel("Algorithme")
        resize_algo_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        resize_head.addWidget(resize_algo_lbl)
        self._resize_algo_combo = QComboBox()
        self._resize_algo_combo.setStyleSheet(_combo_style())
        self._resize_algo_combo.setToolTip("Filtre de mise à l'échelle utilisé par FFmpeg ou NVEncC.")
        for algo in ("lanczos", "bicubic", "bilinear", "spline"):
            self._resize_algo_combo.addItem(algo, algo)
        self._resize_algo_combo.currentIndexChanged.connect(lambda _: self._rebuild_preview())
        self._resize_algo_combo.setFixedWidth(130)
        resize_head.addWidget(self._resize_algo_combo)
        gl.addLayout(resize_head)

        self._resize_value_stack = QStackedWidget()
        self._resize_value_stack.setStyleSheet("background:transparent;")
        resize_page_style = (
            f"background:transparent;"
            f"QLabel{{color:{_C.TEXT_SEC};font-size:11px;background:transparent;}}"
        )

        resize_preset_page = QWidget()
        resize_preset_page.setStyleSheet(resize_page_style)
        preset_l = QHBoxLayout(resize_preset_page)
        preset_l.setContentsMargins(0, 0, 0, 0)
        preset_l.setSpacing(8)
        preset_l.addWidget(QLabel("Preset"))
        self._resize_preset_combo = QComboBox()
        self._resize_preset_combo.setStyleSheet(_combo_style())
        self._resize_preset_combo.setToolTip("Applique une résolution courante et prépare les valeurs WxH correspondantes.")
        for label in ("720p", "1080p", "1440p", "2160p"):
            self._resize_preset_combo.addItem(label, label)
        self._resize_preset_combo.currentIndexChanged.connect(self._on_resize_preset_changed)
        preset_l.addWidget(self._resize_preset_combo)
        self._resize_preset_hint = QLabel("1280 x 720")
        self._resize_preset_hint.setStyleSheet(f"color:{_C.TEXT_DIM};font-size:11px;background:transparent;")
        preset_l.addWidget(self._resize_preset_hint)
        preset_l.addStretch()
        self._resize_value_stack.addWidget(resize_preset_page)

        resize_percent_page = QWidget()
        resize_percent_page.setStyleSheet(resize_page_style)
        percent_l = QHBoxLayout(resize_percent_page)
        percent_l.setContentsMargins(0, 0, 0, 0)
        percent_l.setSpacing(8)
        percent_l.addWidget(QLabel("Pourcentage"))
        self._resize_percent_spin = QSpinBox()
        self._resize_percent_spin.setRange(1, 400)
        self._resize_percent_spin.setValue(100)
        self._resize_percent_spin.setSuffix(" %")
        self._resize_percent_spin.setToolTip("100 % conserve la résolution source. L'upscale est plafonné si l'option est décochée.")
        self._resize_percent_spin.setStyleSheet(_input_style())
        self._resize_percent_spin.valueChanged.connect(lambda _: self._rebuild_preview())
        percent_l.addWidget(self._resize_percent_spin)
        percent_l.addStretch()
        self._resize_value_stack.addWidget(resize_percent_page)

        resize_size_page = QWidget()
        resize_size_page.setStyleSheet(resize_page_style)
        size_l = QHBoxLayout(resize_size_page)
        size_l.setContentsMargins(0, 0, 0, 0)
        size_l.setSpacing(8)
        size_l.addWidget(QLabel("Largeur"))
        self._resize_width_spin = QSpinBox()
        self._resize_width_spin.setRange(2, 16384)
        self._resize_width_spin.setValue(1280)
        self._resize_width_spin.setToolTip("Largeur cible en pixels.")
        self._resize_width_spin.setStyleSheet(_input_style())
        self._resize_width_spin.valueChanged.connect(lambda _: self._rebuild_preview())
        size_l.addWidget(self._resize_width_spin)
        size_l.addWidget(QLabel("Hauteur"))
        self._resize_height_spin = QSpinBox()
        self._resize_height_spin.setRange(2, 16384)
        self._resize_height_spin.setValue(720)
        self._resize_height_spin.setToolTip("Hauteur cible en pixels.")
        self._resize_height_spin.setStyleSheet(_input_style())
        self._resize_height_spin.valueChanged.connect(lambda _: self._rebuild_preview())
        size_l.addWidget(self._resize_height_spin)
        size_l.addStretch()
        self._resize_value_stack.addWidget(resize_size_page)

        resize_flags = QHBoxLayout()
        resize_flags.setSpacing(12)
        resize_flags.addWidget(self._resize_value_stack, 1)
        self._resize_keep_aspect_cb = QCheckBox("Conserver le ratio")
        self._resize_keep_aspect_cb.setChecked(True)
        self._resize_keep_aspect_cb.setStyleSheet(_checkbox_style())
        self._resize_keep_aspect_cb.setToolTip("Évite la déformation en conservant le ratio source dans la résolution cible.")
        self._resize_keep_aspect_cb.toggled.connect(lambda _: self._rebuild_preview())
        resize_flags.addWidget(self._resize_keep_aspect_cb)
        self._resize_allow_upscale_cb = QCheckBox("Autoriser upscale")
        self._resize_allow_upscale_cb.setStyleSheet(_checkbox_style())
        self._resize_allow_upscale_cb.setToolTip("Si décoché, la sortie ne dépasse pas la résolution de la source.")
        self._resize_allow_upscale_cb.toggled.connect(lambda _: self._rebuild_preview())
        resize_flags.addWidget(self._resize_allow_upscale_cb)
        gl.addLayout(resize_flags)

        gl.addWidget(_separator())
        self._crop_enabled_cb = QCheckBox("Recadrer")
        self._crop_enabled_cb.setStyleSheet(_checkbox_style())
        self._crop_enabled_cb.toggled.connect(lambda _: self._rebuild_preview())
        crop_head = QHBoxLayout()
        crop_head.setSpacing(10)
        crop_head.addWidget(self._crop_enabled_cb)
        crop_head.addStretch()
        self._crop_auto_cb = QCheckBox("Auto-crop  (suppression bandes noires)")
        self._crop_auto_cb.setStyleSheet(_checkbox_style())
        self._crop_auto_cb.setToolTip("L'autocrop sera détecté au lancement puis appliqué à la piste active.")
        self._crop_auto_cb.toggled.connect(lambda _: self._rebuild_preview())
        crop_head.addWidget(self._crop_auto_cb)
        gl.addLayout(crop_head)

        self._crop_unit_combo = QComboBox()
        self._crop_unit_combo.setStyleSheet(_combo_style())
        self._crop_unit_combo.setToolTip("Unité du recadrage manuel : pixels ou pourcentage de la source.")
        self._crop_unit_combo.addItem("px", "px")
        self._crop_unit_combo.addItem("%", "percent")
        self._crop_unit_combo.currentIndexChanged.connect(lambda _: self._rebuild_preview())
        self._crop_unit_combo.setFixedWidth(84)
        self._crop_top_spin = self._make_crop_spin("Haut")
        self._crop_bottom_spin = self._make_crop_spin("Bas")
        self._crop_left_spin = self._make_crop_spin("Gauche")
        self._crop_right_spin = self._make_crop_spin("Droite")
        gl.addWidget(self._build_crop_cross_controls())

        cl.addWidget(self._geometry_controls)
        self._sync_transform_controls_enabled()
        return card

    def _build_crop_cross_controls(self) -> QWidget:
        wrap = QWidget()
        wrap.setStyleSheet("background:transparent;")
        layout = QHBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        layout.addWidget(self._crop_unit_combo, alignment=Qt.AlignmentFlag.AlignTop)

        cross = QWidget()
        cross.setStyleSheet("background:transparent;")
        grid = QGridLayout(cross)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.addWidget(self._crop_top_spin, 0, 1, alignment=Qt.AlignmentFlag.AlignHCenter)
        grid.addWidget(self._crop_left_spin, 1, 0, alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(self._build_crop_preview_frame(), 1, 1, alignment=Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(self._crop_right_spin, 1, 2, alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(self._crop_bottom_spin, 2, 1, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(cross)
        layout.addStretch()
        return wrap

    def _build_crop_preview_frame(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("CropPreviewFrame")
        frame.setFixedSize(174, 98)
        frame.setStyleSheet(
            f"QFrame#CropPreviewFrame{{background:{_C.BG_DEEP};"
            f"border:1px solid {_C.BORDER_LT};border-radius:5px;}}"
        )

        grid = QGridLayout(frame)
        grid.setContentsMargins(8, 7, 8, 7)
        grid.setSpacing(0)

        top = QFrame()
        bottom = QFrame()
        left = QFrame()
        right = QFrame()
        image = QFrame()
        for bar in (top, bottom, left, right):
            bar.setStyleSheet("background:rgba(0,0,0,105);border:none;")
        image.setStyleSheet(
            f"background:qlineargradient(x1:0,y1:0,x2:1,y2:1,"
            f"stop:0 {_C.BG_HOVER},stop:0.5 {_C.ACCENT_DIM},stop:1 {_C.BG_ACTIVE});"
            f"border:1px solid {_C.ACCENT};border-radius:3px;"
        )

        top.setFixedHeight(14)
        bottom.setFixedHeight(14)
        left.setFixedWidth(20)
        right.setFixedWidth(20)
        grid.addWidget(top, 0, 0, 1, 3)
        grid.addWidget(left, 1, 0)
        grid.addWidget(image, 1, 1)
        grid.addWidget(right, 1, 2)
        grid.addWidget(bottom, 2, 0, 1, 3)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(1, 1)
        return frame

    def _make_crop_spin(self, prefix: str) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(0, 4096)
        spin.setPrefix(prefix + " ")
        spin.setToolTip(f"Recadrage {prefix.lower()} de la piste vidéo active.")
        spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        spin.setStyleSheet(
            f"QSpinBox{{background:{_C.BG_CARD};color:{_C.TEXT_PRI};"
            f"border:1px solid {_C.BORDER};border-radius:5px;"
            f"padding:4px 8px;font-size:11px;}}"
            f"QSpinBox:focus{{border-color:{_C.ACCENT};}}"
        )
        spin.setFixedWidth(118)
        spin.valueChanged.connect(lambda _: self._rebuild_preview())
        return spin

    @staticmethod
    def _resize_preset_dimensions(preset: str) -> tuple[int, int]:
        return {
            "720p": (1280, 720),
            "1080p": (1920, 1080),
            "1440p": (2560, 1440),
            "2160p": (3840, 2160),
        }.get(str(preset or "720p"), (1280, 720))

    def _sync_resize_mode_ui(self) -> None:
        if not hasattr(self, "_resize_value_stack"):
            return
        mode = str(self._resize_mode_combo.currentData() or "preset")
        page = {"preset": 0, "percent": 1, "size": 2}.get(mode, 0)
        self._resize_value_stack.setCurrentIndex(page)
        width, height = self._resize_preset_dimensions(str(self._resize_preset_combo.currentData() or "720p"))
        self._resize_preset_hint.setText(f"{width} x {height}")

    def _on_resize_mode_changed(self, _index: int = 0) -> None:
        mode = str(self._resize_mode_combo.currentData() or "preset")
        if mode == "size":
            width, height = self._resize_preset_dimensions(str(self._resize_preset_combo.currentData() or "720p"))
            if self._resize_width_spin.value() == 1280 and self._resize_height_spin.value() == 720:
                self._resize_width_spin.setValue(width)
                self._resize_height_spin.setValue(height)
        self._sync_resize_mode_ui()
        self._rebuild_preview()

    def _on_resize_preset_changed(self, _index: int = 0) -> None:
        width, height = self._resize_preset_dimensions(str(self._resize_preset_combo.currentData() or "720p"))
        self._resize_width_spin.setValue(width)
        self._resize_height_spin.setValue(height)
        self._sync_resize_mode_ui()
        self._rebuild_preview()

    def _build_filters_card(self) -> QWidget:
        card, cl = self._build_transform_surface("FILTRES")

        self._filters_controls = QWidget()
        self._filters_controls.setStyleSheet("background:transparent;")
        fl = QVBoxLayout(self._filters_controls)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(12)

        self._yadif_cb = QCheckBox("Désentrelacement")
        self._yadif_cb.setStyleSheet(_checkbox_style())
        self._yadif_cb.setToolTip("Désentrelacement FFmpeg yadif, appliqué avant crop et resize.")
        self._yadif_cb.toggled.connect(lambda _: self._rebuild_preview())
        self._yadif_filter_combo = QComboBox()
        self._yadif_filter_combo.setStyleSheet(_combo_style())
        self._yadif_filter_combo.setToolTip("Filtre de désentrelacement utilisé.")
        self._yadif_filter_combo.addItem("Yadif", "yadif")
        self._yadif_filter_combo.setEnabled(False)
        self._yadif_filter_combo.setVisible(False)
        self._yadif_mode_combo = QComboBox()
        self._yadif_mode_combo.setStyleSheet(_combo_style())
        self._yadif_mode_combo.setToolTip("Frame conserve la cadence, Bob double la cadence.")
        for label, value in (("Frame", "send_frame"), ("Bob", "send_field")):
            self._yadif_mode_combo.addItem(label, value)
        self._yadif_mode_combo.currentIndexChanged.connect(lambda _: self._rebuild_preview())
        self._yadif_parity_combo = QComboBox()
        self._yadif_parity_combo.setStyleSheet(_combo_style())
        self._yadif_parity_combo.setToolTip("Parité du champ source ; auto convient à la plupart des fichiers.")
        for value in ("auto", "tff", "bff"):
            self._yadif_parity_combo.addItem(value, value)
        self._yadif_parity_combo.currentIndexChanged.connect(lambda _: self._rebuild_preview())
        fl.addWidget(self._build_filter_row(
            self._yadif_cb,
            self._filter_tech_label(self._yadif_filter_combo.currentText()),
            self._yadif_mode_combo,
            self._yadif_parity_combo,
        ))

        self._deblock_cb = QCheckBox("Deblock")
        self._deblock_cb.setStyleSheet(_checkbox_style())
        self._deblock_cb.setToolTip("Réduit les blocs de compression via le filtre FFmpeg deblock.")
        self._deblock_cb.toggled.connect(lambda _: self._rebuild_preview())
        self._deblock_filter_combo = QComboBox()
        self._deblock_filter_combo.setStyleSheet(_combo_style())
        self._deblock_filter_combo.setToolTip("Filtre de deblocking utilisé.")
        self._deblock_filter_combo.addItem("deblock", "deblock")
        self._deblock_filter_combo.setEnabled(False)
        self._deblock_filter_combo.setVisible(False)
        self._deblock_strength_combo = self._preset_combo_widget(("ultralight", "light", "medium", "strong", "stronger", "verystrong"))
        self._deblock_strength_combo.setToolTip("Force du deblocking. Commencer léger si le grain doit rester visible.")
        self._deblock_block_combo = self._preset_combo_widget(("4", "8", "16"))
        self._deblock_block_combo.setToolTip("Taille de bloc analysée par le filtre deblock.")
        fl.addWidget(self._build_filter_row(
            self._deblock_cb,
            self._filter_tech_label(self._deblock_filter_combo.currentText()),
            self._deblock_strength_combo,
            self._deblock_block_combo,
        ))

        self._nlmeans_cb = QCheckBox("Débruitage")
        self._nlmeans_cb.setStyleSheet(_checkbox_style())
        self._nlmeans_cb.setToolTip("Débruitage spatial/temporal. Plus lent mais utile sur sources bruitées.")
        self._nlmeans_cb.toggled.connect(lambda _: self._rebuild_preview())
        self._nlmeans_filter_combo = QComboBox()
        self._nlmeans_filter_combo.setStyleSheet(_combo_style())
        self._nlmeans_filter_combo.setToolTip("Filtre de débruitage utilisé.")
        self._nlmeans_filter_combo.addItem("NLMeans", "nlmeans")
        self._nlmeans_filter_combo.setEnabled(False)
        self._nlmeans_filter_combo.setVisible(False)
        self._nlmeans_strength_combo = self._preset_combo_widget(("ultralight", "light", "medium", "strong"))
        self._nlmeans_strength_combo.setToolTip("Intensité du débruitage NLMeans.")
        self._nlmeans_profile_combo = self._preset_combo_widget(("standard", "grain", "animation", "high motion", "sprite"))
        self._nlmeans_profile_combo.setToolTip("Profil de contenu qui ajuste légèrement les paramètres NLMeans.")
        fl.addWidget(self._build_filter_row(
            self._nlmeans_cb,
            self._filter_tech_label(self._nlmeans_filter_combo.currentText()),
            self._nlmeans_strength_combo,
            self._nlmeans_profile_combo,
        ))

        self._chroma_cb = QCheckBox("Color Smooth")
        self._chroma_cb.setStyleSheet(_checkbox_style())
        self._chroma_cb.setToolTip("Lisse le bruit couleur via chromanr, sans dépendance externe.")
        self._chroma_cb.toggled.connect(lambda _: self._rebuild_preview())
        self._chroma_filter_combo = QComboBox()
        self._chroma_filter_combo.setStyleSheet(_combo_style())
        self._chroma_filter_combo.setToolTip("Filtre de lissage couleur utilisé.")
        self._chroma_filter_combo.addItem("chromanr", "chromanr")
        self._chroma_filter_combo.setEnabled(False)
        self._chroma_filter_combo.setVisible(False)
        self._chroma_strength_combo = self._preset_combo_widget(("ultralight", "light", "medium", "strong", "stronger", "verystrong"))
        self._chroma_strength_combo.setToolTip("Force du lissage chroma FFmpeg chromanr.")
        fl.addWidget(self._build_filter_row(
            self._chroma_cb,
            self._filter_tech_label(self._chroma_filter_combo.currentText()),
            self._chroma_strength_combo,
        ))

        cl.addWidget(self._filters_controls)
        self._sync_transform_controls_enabled()
        return card

    def _build_filter_row(self, toggle: QCheckBox, *widgets: QWidget) -> QWidget:
        row = QWidget()
        row.setStyleSheet("background:transparent;")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        toggle.setMinimumWidth(150)
        layout.addWidget(toggle)
        for widget in widgets:
            layout.addWidget(widget)
        layout.addStretch()
        return row

    def _filter_tech_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setMinimumWidth(72)
        label.setStyleSheet(
            f"color:{_C.TEXT_DIM};font-size:10px;font-weight:700;"
            f"background:transparent;border:none;"
        )
        return label

    def _preset_combo_widget(self, values: tuple[str, ...]) -> QComboBox:
        combo = QComboBox()
        combo.setStyleSheet(_combo_style())
        for value in values:
            combo.addItem(value, value)
        combo.currentIndexChanged.connect(lambda _: self._rebuild_preview())
        return combo

    def _build_profiles_row(self) -> QWidget:
        wrap = QWidget()
        wrap.setStyleSheet("background:transparent;")
        cl = QHBoxLayout(wrap)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(8)

        self._profile_combo = QComboBox()
        self._profile_combo.setStyleSheet(_combo_style())
        self._profile_combo.setMinimumWidth(180)
        self._refresh_profiles()
        cl.addWidget(self._profile_combo)

        load_btn = _secondary_button("Charger")
        load_btn.clicked.connect(self._load_profile)
        del_btn = _secondary_button("Supprimer")
        del_btn.clicked.connect(self._delete_profile)
        cl.addWidget(load_btn)
        cl.addWidget(del_btn)
        cl.addStretch()

        self._profile_name = QLineEdit()
        self._profile_name.setPlaceholderText("Nom du profil…")
        self._profile_name.setStyleSheet(_input_style())
        self._profile_name.setFixedWidth(160)
        self._profile_name.returnPressed.connect(self._save_profile)
        save_btn = _secondary_button("Enregistrer")
        save_btn.clicked.connect(self._save_profile)
        cl.addWidget(self._profile_name)
        cl.addWidget(save_btn)

        return wrap

    def _prefill_hdr_meta(
        self,
        raw: dict,
        source_path: Path | None = None,
        mediainfo_json: dict | None = None,
        *,
        probe_frames: bool = False,
    ) -> None:
        """Pré-remplit master_display et max_cll sans bloquer le thread Qt."""
        master_display, max_cll = self._extract_hdr_meta_fields(
            raw,
            source_path,
            mediainfo_json=mediainfo_json,
            include_frame_probe=False,
        )
        self._master_display.setText(master_display)
        self._max_cll.setText(max_cll)
        if (
            not probe_frames
            or source_path is None
            or not source_path.is_file()
            or (master_display and max_cll)
        ):
            return

        generation = self._hdr_meta_probe_generation
        self._hdr_meta_probe_expected = (generation, master_display, max_cll)
        self._hdr_meta_executor.submit(self._probe_hdr_meta_from_frames, generation, source_path)

    def _invalidate_hdr_meta_frame_probe(self) -> None:
        self._hdr_meta_probe_generation += 1
        self._hdr_meta_probe_expected = None

    def _probe_hdr_meta_from_frames(self, generation: int, source_path: Path) -> None:
        try:
            master_display, max_cll = self._extract_hdr_meta_from_ffprobe_frames(source_path)
        except Exception:
            master_display, max_cll = "", ""
        self._hdr_meta_frame_probe_ready.emit(generation, master_display, max_cll)

    def _on_hdr_meta_frame_probe_ready(
        self,
        generation: int,
        master_display: str,
        max_cll: str,
    ) -> None:
        if self._closing:
            return
        expected = self._hdr_meta_probe_expected
        if expected is None or generation != expected[0]:
            return
        self._hdr_meta_probe_expected = None
        expected_master_display, expected_max_cll = expected[1:]
        # Ne pas écraser une valeur saisie par l'utilisateur pendant le probe.
        if master_display and self._master_display.text() == expected_master_display:
            self._master_display.setText(master_display)
        if max_cll and self._max_cll.text() == expected_max_cll:
            self._max_cll.setText(max_cll)

    def _extract_hdr_meta_fields(
        self,
        raw: dict,
        source_path: Path | None = None,
        *,
        mediainfo_json: dict | None = None,
        include_frame_probe: bool = True,
    ) -> tuple[str, str]:
        # 1. Priorité mediainfo (parse de tous les SEI HEVC d'un coup).
        master_display, max_cll = "", ""
        if mediainfo_json is not None:
            master_display, max_cll = self._extract_hdr_meta_from_mediainfo_json(mediainfo_json)
        elif source_path is not None:
            master_display, max_cll = self._extract_hdr_meta_from_mediainfo(source_path)
        if master_display and max_cll:
            return master_display, max_cll

        # 2. Fallback ffprobe via side_data_list **stream-level** (rare —
        #    seuls quelques fichiers exposent MDCV/CLL au niveau stream).
        ff_md, ff_cll = self._extract_hdr_meta_from_ffprobe_raw(raw)
        master_display = master_display or ff_md
        max_cll = max_cll or ff_cll
        if master_display and max_cll:
            return master_display, max_cll

        # 3. Fallback ffprobe -show_frames : lit le SEI MDCV/CLL directement
        #    dans les frames HEVC (cas typique : mediainfo absent et stream-
        #    level n'expose pas les side_data). Indispensable quand
        #    `WARN Outils manquants : mediainfo` au démarrage.
        if include_frame_probe and source_path is not None:
            ff2_md, ff2_cll = self._extract_hdr_meta_from_ffprobe_frames(source_path)
            master_display = master_display or ff2_md
            max_cll = max_cll or ff2_cll
        return master_display, max_cll

    def _extract_hdr_meta_from_ffprobe_frames(
        self, source_path: Path,
    ) -> tuple[str, str]:
        """
        Fallback alternatif quand mediainfo est absent : lit MDCV/CLL via
        ``ffprobe -show_frames -read_intervals "%+#1"``.

        ffprobe expose les chromaticités au format ``num/50000`` et la
        luminance au format ``num/10000`` — exactement les unités ffmpeg/x265.
        Retourne ``("", "")`` si ffprobe absent ou si le SEI n'est pas présent.
        """
        cmd = [
            self._config.tool_ffprobe,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_frames",
            "-read_intervals", "%+#1",
            "-print_format", "json",
        ]
        cmd.extend(ffprobe_input_args(source_path))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True, check=False, timeout=20, text=True,
            )
        except (FileNotFoundError, OSError):
            return "", ""
        if result.returncode != 0:
            return "", ""
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return "", ""
        frames = data.get("frames") or []
        if not frames:
            return "", ""
        side_data_list = frames[0].get("side_data_list") or []

        def _num(rat: object) -> int:
            try:
                return int(str(rat).split("/", 1)[0])
            except (ValueError, AttributeError):
                return 0

        master_display = ""
        max_cll = ""
        for sd in side_data_list:
            stype = sd.get("side_data_type") or ""
            if stype == "Mastering display metadata":
                gx, gy = _num(sd.get("green_x")), _num(sd.get("green_y"))
                bx, by = _num(sd.get("blue_x")), _num(sd.get("blue_y"))
                rx, ry = _num(sd.get("red_x")), _num(sd.get("red_y"))
                wx, wy = _num(sd.get("white_point_x")), _num(sd.get("white_point_y"))
                lmin = _num(sd.get("min_luminance"))
                lmax = _num(sd.get("max_luminance"))
                if lmax > 0 and (rx > 0 or gx > 0 or bx > 0):
                    master_display = (
                        f"G({gx},{gy})B({bx},{by})R({rx},{ry})"
                        f"WP({wx},{wy})L({lmax},{lmin})"
                    )
            elif stype == "Content light level metadata":
                try:
                    mc = int(sd.get("max_content") or 0)
                    ma = int(sd.get("max_average") or 0)
                except (TypeError, ValueError):
                    mc = ma = 0
                if mc > 0:
                    max_cll = f"{mc},{ma}"
        return master_display, max_cll

    def _extract_hdr_meta_from_mediainfo(
        self, source_path: Path,
    ) -> tuple[str, str]:
        """Renvoie (master_display, max_cll) depuis le JSON mediainfo,
        ou ('', '') si mediainfo est absent ou sans champs HDR statiques.

        Le format master_display est celui attendu par x265/ffmpeg
        (chromaticité ×50000, luminance ×10000).
        Mediainfo n'expose pas les chromaticités du Mastering Display ;
        on dérive donc depuis ``MasteringDisplay_ColorPrimaries`` quand
        c'est un primaire connu (BT.2020, Display P3, BT.709).
        """
        try:
            result = subprocess.run(
                [self._config.tool_mediainfo, "--Output=JSON", str(source_path)],
                capture_output=True, check=False, text=True,
            )
        except (FileNotFoundError, OSError):
            return "", ""
        if result.returncode != 0:
            return "", ""
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return "", ""
        return self._extract_hdr_meta_from_mediainfo_json(data)

    def _extract_hdr_meta_from_mediainfo_json(
        self,
        data: dict,
    ) -> tuple[str, str]:
        """Renvoie (master_display, max_cll) depuis un JSON MediaInfo déjà chargé."""
        media = data.get("media") or {}
        mi_video = next(
            (t for t in (media.get("track") or []) if isinstance(t, dict) and t.get("@type") == "Video"),
            None,
        )
        if mi_video is None:
            return "", ""

        master_display = ""
        primaries_label = str(mi_video.get("MasteringDisplay_ColorPrimaries") or "").lower()
        primaries = self._MASTER_DISPLAY_PRIMARIES.get(primaries_label.strip())
        try:
            lmin = float(mi_video.get("MasteringDisplay_Luminance_Min") or 0)
            lmax = float(mi_video.get("MasteringDisplay_Luminance_Max") or 0)
        except (TypeError, ValueError):
            lmin = lmax = 0.0
        if primaries and lmax > 0:
            (gx, gy), (bx, by), (rx, ry), (wx, wy) = primaries

            def c(value: float) -> int:
                return int(round(value * 50000))

            def l_(value: float) -> int:
                return int(round(value * 10000))

            master_display = (
                f"G({c(gx)},{c(gy)})"
                f"B({c(bx)},{c(by)})"
                f"R({c(rx)},{c(ry)})"
                f"WP({c(wx)},{c(wy)})"
                f"L({l_(lmax)},{l_(lmin)})"
            )

        max_cll = ""
        def _first_int(value: object) -> int:
            match = re.search(r"\d+(?:[ \u00a0\u202f_]\d{3})*", str(value or ""))
            if match is None:
                return 0
            return int(
                match.group(0)
                .replace(" ", "")
                .replace("\u00a0", "")
                .replace("\u202f", "")
                .replace("_", "")
            )

        try:
            max_content = _first_int(mi_video.get("MaxCLL"))
            max_average = _first_int(mi_video.get("MaxFALL"))
        except (TypeError, ValueError):
            max_content = max_average = 0
        if max_content > 0:
            max_cll = f"{max_content},{max_average}"
        return master_display, max_cll

    # Chromaticités CIE 1931 (x, y) standard pour les primaires courants
    # exposés par mediainfo via MasteringDisplay_ColorPrimaries.
    _MASTER_DISPLAY_PRIMARIES: dict[str, tuple[tuple[float, float], ...]] = {
        # G, B, R, WP (D65)
        "bt.2020":    ((0.170, 0.797), (0.131, 0.046), (0.708, 0.292), (0.3127, 0.3290)),
        "display p3": ((0.265, 0.690), (0.150, 0.060), (0.680, 0.320), (0.3127, 0.3290)),
        "bt.709":     ((0.300, 0.600), (0.150, 0.060), (0.640, 0.330), (0.3127, 0.3290)),
        "p3-d65":     ((0.265, 0.690), (0.150, 0.060), (0.680, 0.320), (0.3127, 0.3290)),
    }

    @staticmethod
    def _extract_hdr_meta_from_ffprobe_raw(raw: dict) -> tuple[str, str]:
        master_display = ""
        max_cll = ""

        def _rat(v) -> float:
            s = str(v)
            if "/" in s:
                a, b = s.split("/", 1)
                return float(a) / float(b)
            return float(s)

        for sd in raw.get("side_data_list", []):
            if sd.get("side_data_type") == "Mastering display metadata":
                try:
                    rx = _rat(sd.get("red_x", 0))
                    ry = _rat(sd.get("red_y", 0))
                    gx = _rat(sd.get("green_x", 0))
                    gy = _rat(sd.get("green_y", 0))
                    bx = _rat(sd.get("blue_x", 0))
                    by = _rat(sd.get("blue_y", 0))
                    wx = _rat(sd.get("white_point_x", 0))
                    wy = _rat(sd.get("white_point_y", 0))
                    lmax = _rat(sd.get("max_luminance", 0))
                    lmin = _rat(sd.get("min_luminance", 0))

                    def c(value: float) -> int:
                        return int(round(value * 50000))

                    def l_(value: float) -> int:
                        return int(round(value * 10000))

                    master_display = (
                        f"G({c(gx)},{c(gy)})"
                        f"B({c(bx)},{c(by)})"
                        f"R({c(rx)},{c(ry)})"
                        f"WP({c(wx)},{c(wy)})"
                        f"L({l_(lmax)},{l_(lmin)})"
                    )
                except Exception:
                    pass
            elif sd.get("side_data_type") == "Content light level metadata":
                try:
                    max_content = int(sd.get("max_content", 0))
                    max_average = int(sd.get("max_average", 0))
                    max_cll = f"{max_content},{max_average}"
                except Exception:
                    pass
        return master_display, max_cll

    # ------------------------------------------------------------------
    # Détection encodeurs matériels
    # ------------------------------------------------------------------

    def _detect_hw_encoders(self) -> None:
        detector = HardwareEncoderDetector()
        ffmpeg = self._config.tool_ffmpeg
        nvencc = getattr(self._config, "tool_nvencc", None) or None
        hw, hw_ffmpeg = detector.detect(ffmpeg, nvencc_bin=nvencc)
        sw = detector.detect_software(ffmpeg)
        # hw_ffmpeg peut être le ffmpeg système si le ffmpeg embarqué manque de HW codecs.
        # On l'envoie avec le signal pour que le workflow l'utilise lors de l'encodage HW.
        self._hw_detected.emit(hw, sw, hw_ffmpeg)

    def _on_hw_detected(self, hw: set[str], sw: set[str], hw_ffmpeg: str) -> None:
        self._hw_encoders = hw
        # Ne met à jour les codecs SW que si la détection a retourné au moins un résultat.
        # Un set vide signifie une erreur de détection (ffmpeg absent), pas "aucun codec".
        if sw:
            self._sw_encoders = sw
        # Si le ffmpeg HW est différent du ffmpeg embarqué (AppImage : système vs bundled),
        # mettre à jour le workflow pour que l'encodage HW utilise le bon binaire.
        if hw and hw_ffmpeg != self._config.tool_ffmpeg:
            self._workflow.set_ffmpeg(hw_ffmpeg)
        current = self._codec_combo.currentData()
        self._populate_codec_combo()
        # Restaure la sélection précédente si toujours disponible
        for i in range(self._codec_combo.count()):
            if self._codec_combo.itemData(i) == current:
                self._codec_combo.setCurrentIndex(i)
                break
        all_detected = hw | sw
        if all_detected:
            self.log_message.emit(
                "OK",
                translate_text(
                    "Encodeurs détectés : {items}",
                    items=", ".join(sorted(all_detected)),
                ),
            )

    def _populate_codec_combo(self) -> None:
        self._codec_combo.blockSignals(True)
        self._codec_combo.clear()
        self._codec_combo.addItem("Copy — remux (sans conversion)", "copy")
        for codec_id, label in SOFTWARE_VIDEO_CODECS:
            if codec_id in self._sw_encoders:
                self._codec_combo.addItem(label, codec_id)
        for codec_id, label in HARDWARE_VIDEO_CODECS:
            if codec_id in self._hw_encoders:
                self._codec_combo.addItem(f"⚡ {label}", codec_id)
        self._codec_combo.blockSignals(False)

    # ------------------------------------------------------------------
    # Changements UI → rebuild preview
    # ------------------------------------------------------------------

    def _on_codec_changed(self, _idx: int = 0) -> None:
        codec = self._codec_combo.currentData() or "libx265"
        if hasattr(self, "_video_encode_controls"):
            self._video_encode_controls.setVisible(codec != "copy")
        self._refresh_mode_combo(codec)
        presets = presets_for_codec(codec)
        self._preset_combo.blockSignals(True)
        self._preset_combo.clear()
        for p in presets:
            self._preset_combo.addItem(p, p)
        # Sélectionne "slow" par défaut pour x265/x264, "6" pour SVT-AV1
        default = "slow" if codec not in ("libsvtav1",) else "6"
        idx = next((i for i in range(self._preset_combo.count())
                    if self._preset_combo.itemData(i) == default), 0)
        self._preset_combo.setCurrentIndex(idx)
        self._preset_combo.setEnabled(bool(presets))
        self._preset_combo.blockSignals(False)
        self._update_ten_bit_control(codec)
        self._sync_hdr_metadata_field_editability(str(codec))
        self._update_passthrough_controls()
        self._sync_transform_controls_enabled()
        self._rebuild_preview()

    def _sync_transform_controls_enabled(self) -> None:
        codec = str(self._codec_combo.currentData() or "copy").strip().lower() if hasattr(self, "_codec_combo") else "copy"
        enabled = codec != "copy"
        for controls_name, msg_name in (
            ("_geometry_controls", "_geometry_copy_msg"),
            ("_filters_controls", "_filters_copy_msg"),
        ):
            controls = getattr(self, controls_name, None)
            if controls is not None:
                controls.setEnabled(enabled)
            msg = getattr(self, msg_name, None)
            if msg is not None:
                msg.setVisible(not enabled)

    def _on_ten_bit_toggled(self, _checked: bool) -> None:
        if self._loading_video_settings:
            return
        self._save_current_video_state()
        self._rebuild_preview()

    def _sync_hdr_metadata_field_editability(self, codec: str) -> None:
        if not hasattr(self, "_master_display") or not hasattr(self, "_max_cll"):
            return
        codec_id = str(codec or "").strip().lower()
        editable = (
            self._backend_capabilities(codec_id).supports_manual_static_hdr
            or self._current_state_allows_copy_static_hdr_edit(codec_id)
        )
        if not editable:
            prev = (
                self._video_settings_by_entry_id.get(self._current_video_entry_id)
                if self._current_video_entry_id is not None
                else None
            ) or {}
            default_md = str(prev.get("default_master_display") or "")
            default_cll = str(prev.get("default_max_cll") or "")
            self._master_display.blockSignals(True)
            self._max_cll.blockSignals(True)
            self._master_display.setText(default_md)
            self._max_cll.setText(default_cll)
            self._master_display.blockSignals(False)
            self._max_cll.blockSignals(False)
        self._master_display.setReadOnly(not editable)
        self._max_cll.setReadOnly(not editable)
        tooltip = (
            self._static_hdr_metadata_tooltip()
            if editable
            else (
                "Ce codec conserve les métadonnées HDR statiques déjà présentes "
                "dans la source, mais ne permet pas leur édition manuelle."
            )
        )
        self._master_display.setToolTip(tooltip)
        self._max_cll.setToolTip(tooltip)

    def _current_state_allows_copy_static_hdr_edit(self, codec_id: str) -> bool:
        if codec_id != "copy":
            return False
        if not hasattr(self, "_copy_dv_cb") or not hasattr(self, "_dovi_profile_combo"):
            return False
        return bool(
            self._copy_dv_cb.isChecked()
            and str(self._dovi_profile_combo.currentData() or "0").strip() == "2"
            and self._inject_hdr_cb.isChecked()
        )

    def _static_hdr_metadata_tooltip(self) -> str:
        state = (
            self._video_settings_by_entry_id.get(self._current_video_entry_id)
            if self._current_video_entry_id is not None
            else None
        ) or {}
        request_mode = str(state.get("static_hdr_metadata_analysis_request") or "")
        if request_mode:
            mode = self._static_hdr_estimate_mode_label(request_mode)
            return (
                f"Analyse HDR10 P5→P8.1 {mode} programmée ; "
                "elle démarrera avec le workflow."
            )
        if str(state.get("static_hdr_metadata_source") or "") == StaticHdrEstimateService.SOURCE_LABEL:
            confidence = str(state.get("static_hdr_metadata_confidence") or "?")
            mode = self._static_hdr_estimate_mode_label(
                str(state.get("static_hdr_metadata_analysis_mode") or "")
            )
            return f"HDR10 estimé depuis analyse P5→P8.1 {mode} (confiance {confidence})."
        return ""

    def _update_ten_bit_control(self, codec: str) -> None:
        """Active/désactive la checkbox 10-Bits selon le codec et le force_8bit."""
        if not hasattr(self, "_ten_bit_cb"):
            return
        target_codec = (codec or "").strip().lower()
        codec_ok = target_codec != "copy" and supports_10bit(target_codec)
        # force_8bit (h264 + source >8bit) prend priorité — désactive 10-bit.
        force_8bit = self._current_force_8bit_active(target_codec)
        enabled = codec_ok and not force_8bit
        self._ten_bit_cb.blockSignals(True)
        self._ten_bit_cb.setEnabled(enabled)
        if not enabled:
            self._ten_bit_cb.setChecked(False)
        elif not self._ten_bit_cb.isChecked() and self._current_default_force_10bit():
            # Source ≥10-bit / DV / HDR10+ → coche par défaut quand le codec
            # cible (re)devient compatible 10-bit (ex. passage copy → hevc_nvenc).
            self._ten_bit_cb.setChecked(True)
        self._ten_bit_cb.blockSignals(False)

    def _current_default_force_10bit(self) -> bool:
        """Default 10-bit pour la piste vidéo courante (source ≥10-bit / DV / HDR10+)."""
        row = self._video_list.currentRow() if hasattr(self, "_video_list") else -1
        if not (0 <= row < len(self._video_tracks)):
            return False
        file_info, track, _color = self._video_tracks[row]
        bit_depth = self._video_source_bit_depth(file_info, track)
        source_hdr = self._hdr_type_for_entry(file_info, track)
        return bit_depth >= 10 or self._source_has_dv(source_hdr) or self._source_has_hdr10plus(source_hdr)

    def _current_force_8bit_active(self, codec: str) -> bool:
        """True si le précheck force_8bit s'applique pour la piste sélectionnée."""
        row = self._video_list.currentRow() if hasattr(self, "_video_list") else -1
        if 0 <= row < len(self._video_tracks):
            file_info, track, _color = self._video_tracks[row]
            return self._video_force_8bit_for_codec(file_info, track, codec)
        return False

    def _refresh_mode_combo(self, codec: str) -> None:
        """Reconstruit la liste des modes de qualité selon le codec sélectionné.

        Le mode CQ n'est exposé que pour les encodeurs matériels (NVENC/AMF/QSV/VAAPI)
        où il a un équivalent natif (-cq:v / cqp / global_quality).
        """
        previous = self._mode_combo.currentData() if self._mode_combo.count() else QualityMode.CRF
        caps = self._backend_capabilities(codec)
        modes = list(caps.quality_modes)

        self._mode_combo.blockSignals(True)
        self._mode_combo.clear()
        for mode in modes:
            self._mode_combo.addItem(mode.label(), mode)
        target = previous if previous in modes else QualityMode.CRF
        idx = next((i for i in range(self._mode_combo.count())
                    if self._mode_combo.itemData(i) == target), 0)
        self._mode_combo.setCurrentIndex(idx)
        self._mode_combo.blockSignals(False)
        self._on_mode_changed()

    def _on_apply_all_video_toggled(self, checked: bool) -> None:
        self._video_apply_all = bool(checked)
        if checked:
            self._propagate_current_video_state_to_all()
        else:
            self._save_current_video_state()
        self._rebuild_preview()

    def _on_dv_toggle(self, _state: int) -> None:
        self._dovi_profile_widget.setVisible(self._copy_dv_cb.isChecked())
        self._sync_hdr_metadata_field_editability(self._codec_combo.currentData() or "copy")
        if not self._loading_video_settings:
            self._save_current_video_state()
            self._maybe_offer_static_hdr_estimate_for_current()
        self._rebuild_preview()

    def _on_dovi_profile_changed(self, _idx: int = 0) -> None:
        self._sync_hdr_metadata_field_editability(self._codec_combo.currentData() or "copy")
        if not self._loading_video_settings:
            self._save_current_video_state()
            self._maybe_offer_static_hdr_estimate_for_current()
        self._rebuild_preview()

    def _maybe_offer_static_hdr_estimate_for_current(self) -> None:
        candidate = self._static_hdr_estimate_candidate_current()
        if candidate is None:
            return
        entry_id, _info, _track, state = candidate
        prompt_key = f"{entry_id}:p5_to_p8"
        if prompt_key in self._static_hdr_estimate_prompted:
            return
        self._static_hdr_estimate_prompted.add(prompt_key)
        mode = self._ask_static_hdr_estimate_mode()
        if mode:
            self._schedule_static_hdr_estimate(entry_id, state, mode=mode)

    def _ask_static_hdr_estimate_mode(self) -> str:
        box = QMessageBox(self)
        box.setWindowTitle("Analyse HDR10 estimée")
        box.setText(
            "La conversion P5→P8.1 va réencoder le base layer IPT en HDR10 "
            "BT.2020/PQ avec libplacebo, puis convertir et réinjecter le RPU "
            "Dolby Vision.\n\nChoisissez l'analyse HDR10 statique à appliquer "
            "sur le nouveau base layer."
        )
        precise_btn = box.addButton("Analyse précise", QMessageBox.ButtonRole.AcceptRole)
        fast_btn = box.addButton("Analyse rapide", QMessageBox.ButtonRole.ActionRole)
        ignore_btn = box.addButton("Ignorer", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(precise_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is precise_btn:
            return StaticHdrEstimateService.PRECISE_MODE
        if clicked is fast_btn:
            return StaticHdrEstimateService.FAST_MODE
        if clicked is ignore_btn:
            return ""
        return ""

    @staticmethod
    def _static_hdr_estimate_mode_label(mode: str) -> str:
        value = str(mode or "").strip().lower()
        if value == StaticHdrEstimateService.PRECISE_MODE:
            return "précise"
        if value == StaticHdrEstimateService.FAST_MODE:
            return "rapide"
        return "estimée"

    def _static_hdr_estimate_candidate_current(
        self,
    ) -> tuple[str, FileInfo, TrackEntry, dict[str, object]] | None:
        if self._loading_video_settings or self._current_video_entry_id is None:
            return None
        if len(self._video_tracks) != 1:
            return None
        row = self._video_list.currentRow() if hasattr(self, "_video_list") else -1
        if not (0 <= row < len(self._video_tracks)):
            return None
        info, track, _color = self._video_tracks[row]
        entry_id = self._video_entry_id(track)
        if entry_id != self._current_video_entry_id:
            return None
        state = self._video_settings_by_entry_id.get(entry_id)
        if state is None:
            state = self._current_video_state()
        if not self._state_needs_p5_static_hdr_estimate(info, track, state):
            return None
        return entry_id, info, track, state

    def _state_needs_p5_static_hdr_estimate(
        self,
        info: FileInfo,
        track: TrackEntry,
        state: dict[str, object],
    ) -> bool:
        source_video = self._video_track_for_entry(info, track)
        if source_video is None or int(source_video.dovi_profile or 0) != 5:
            return False
        if not bool(state.get("copy_dv")):
            return False
        if str(state.get("dovi_profile") or "0").strip() != "2":
            return False
        if str(state.get("codec") or "").strip().lower().startswith("nvencc_"):
            return False
        return self._state_allows_static_hdr_estimate(state)

    def _normalize_pending_static_hdr_analysis(
        self,
        entry_id: str,
        state: dict[str, object],
    ) -> tuple[dict[str, object], bool]:
        request_mode = str(state.get("static_hdr_metadata_analysis_request") or "").strip()
        if not request_mode:
            return state, False

        matching_track = next(
            (
                (info, track)
                for info, track, _color in self._video_tracks
                if self._video_entry_id(track) == entry_id
            ),
            None,
        )
        source_video = (
            self._video_track_for_entry(*matching_track)
            if matching_track is not None
            else None
        )
        request_is_valid = bool(
            len(self._video_tracks) == 1
            and source_video is not None
            and int(source_video.dovi_profile or 0) == 5
            and bool(state.get("inject_hdr_meta"))
            and bool(state.get("copy_dv"))
            and str(state.get("dovi_profile") or "0").strip() == "2"
            and not str(state.get("codec") or "").strip().lower().startswith("nvencc_")
            and not str(state.get("master_display") or "").strip()
            and not str(state.get("max_cll") or "").strip()
        )
        if request_is_valid:
            return state, False

        normalized = self._copy_video_state(state)
        normalized.update(
            {
                "static_hdr_metadata_analysis_request": "",
                "static_hdr_metadata_analysis_mode": "",
                "static_hdr_metadata_source": "",
                "static_hdr_metadata_confidence": "",
            }
        )
        self._static_hdr_estimate_prompted.discard(f"{entry_id}:p5_to_p8")
        return normalized, True

    @staticmethod
    def _state_allows_static_hdr_estimate(state: dict[str, object]) -> bool:
        if str(state.get("static_hdr_metadata_analysis_request") or "").strip():
            return False
        source = str(state.get("static_hdr_metadata_source") or "")
        if source == StaticHdrEstimateService.SOURCE_LABEL:
            return True
        fields = (
            "master_display",
            "max_cll",
            "default_master_display",
            "default_max_cll",
        )
        return not any(str(state.get(key) or "").strip() for key in fields)

    def _schedule_static_hdr_estimate(
        self,
        entry_id: str,
        state: dict[str, object],
        *,
        mode: str = StaticHdrEstimateService.PRECISE_MODE,
    ) -> None:
        mode = (
            StaticHdrEstimateService.FAST_MODE
            if str(mode).strip().lower() == StaticHdrEstimateService.FAST_MODE
            else StaticHdrEstimateService.PRECISE_MODE
        )
        scheduled = self._copy_video_state(state)
        scheduled.update(
            {
                "inject_hdr_meta": True,
                "static_hdr_metadata_analysis_request": mode,
                "static_hdr_metadata_analysis_mode": "",
                "static_hdr_metadata_source": "",
                "static_hdr_metadata_confidence": "",
            }
        )
        self._video_settings_by_entry_id[entry_id] = scheduled
        if entry_id == self._current_video_entry_id:
            self._apply_video_state(scheduled)
        self._emit_video_encoding_plans()
        mode_label = self._static_hdr_estimate_mode_label(mode)
        self.log_message.emit(
            "INFO",
            (
                f"Analyse HDR10 estimée P5→P8.1 {mode_label} programmée. "
                "Elle sera exécutée et suivie dans le workflow d’encodage."
            ),
        )

    def _on_static_hdr_estimate_ready(
        self,
        entry_id: str,
        estimate_obj: object,
    ) -> None:
        if not isinstance(estimate_obj, StaticHdrEstimate):
            return
        state = self._video_settings_by_entry_id.get(entry_id)
        if state is None:
            return
        if not str(state.get("static_hdr_metadata_analysis_request") or "").strip():
            return
        source = str(state.get("static_hdr_metadata_source") or "")
        if source != StaticHdrEstimateService.SOURCE_LABEL and any(
            str(state.get(key) or "").strip()
            for key in ("master_display", "max_cll")
        ):
            self.log_message.emit(
                "WARN",
                "Résultat de l’analyse HDR10 ignoré : des valeurs manuelles sont désormais présentes.",
            )
            return
        state = self._copy_video_state(state)
        state.update(
            {
                "inject_hdr_meta": True,
                "master_display": estimate_obj.master_display,
                "max_cll": estimate_obj.max_cll,
                "default_master_display": estimate_obj.master_display,
                "default_max_cll": estimate_obj.max_cll,
                "static_hdr_metadata_source": estimate_obj.source,
                "static_hdr_metadata_confidence": estimate_obj.confidence,
                "static_hdr_metadata_analysis_mode": estimate_obj.mode,
                "static_hdr_metadata_analysis_request": "",
            }
        )
        self._video_settings_by_entry_id[entry_id] = state
        if entry_id == self._current_video_entry_id:
            self._apply_video_state(state)
        self._emit_video_encoding_plans()
        warning_suffix = f" ({'; '.join(estimate_obj.warnings)})" if estimate_obj.warnings else ""
        mode_label = self._static_hdr_estimate_mode_label(estimate_obj.mode)
        detail_parts = [
            f"confiance {estimate_obj.confidence}",
            f"{estimate_obj.active_sample_count or estimate_obj.sample_count} actifs",
        ]
        if estimate_obj.ignored_sample_count:
            detail_parts.append(f"{estimate_obj.ignored_sample_count} ignorés")
        if estimate_obj.active_crop:
            detail_parts.append(f"crop {estimate_obj.active_crop}")
        self.log_message.emit(
            "WARN",
            (
                "HDR10 statique estimé depuis analyse P5→P8.1 "
                f"{mode_label} ({', '.join(detail_parts)}): "
                f"{estimate_obj.max_cll}.{warning_suffix}"
            ),
        )
        self._rebuild_preview()

    def _on_static_hdr_estimate_failed(self, entry_id: str, message: str) -> None:
        if entry_id and entry_id not in self._video_settings_by_entry_id:
            return
        self.log_message.emit("ERROR", message)
        QMessageBox.critical(
            self,
            "Analyse HDR10 impossible",
            message,
        )

    def _update_passthrough_controls(self, *, auto_check: bool = False) -> None:
        """Active/désactive les contrôles DV/HDR10+ selon la source et le codec."""
        if not hasattr(self, "_copy_dv_cb"):
            return   # appelé pendant l'init avant que le card HDR soit construit
        if self._file_info is None:
            self._copy_dv_cb.setEnabled(False)
            self._copy_hdr10plus_cb.setEnabled(False)
            return

        codec = self._codec_combo.currentData() or "libx265"
        supports_hdr_passthrough = self._backend_capabilities(codec).supports_dynamic_hdr
        hdr = self._selected_video_hdr_type()

        has_dv       = hdr in (HDRType.DOLBY_VISION, HDRType.DOLBY_VISION_HDR10PLUS)
        has_hdr10plus = hdr in (HDRType.HDR10PLUS, HDRType.DOLBY_VISION_HDR10PLUS)

        dv_ok       = has_dv and supports_hdr_passthrough
        hdr10plus_ok = has_hdr10plus and supports_hdr_passthrough

        self._copy_dv_cb.setEnabled(dv_ok)
        self._copy_hdr10plus_cb.setEnabled(hdr10plus_ok)

        if auto_check:
            self._copy_dv_cb.setChecked(dv_ok)
            self._copy_hdr10plus_cb.setChecked(hdr10plus_ok)
            # Cocher la case HDR statique dès qu'on a du HDR sous n'importe
            # quelle forme (HDR10/HDR10+/DV) — pas seulement si master_display
            # est rempli. Sans MDCV/CLL dans le BL encodé, un fichier DoVi
            # P8.1 affiche fade et désynchronise le décodeur côté TV.
            is_hdr_source = hdr is not None and hdr != HDRType.NONE
            self._inject_hdr_cb.setChecked(is_hdr_source)

        if not dv_ok and not self._video_apply_all:
            self._copy_dv_cb.setChecked(False)
        if not hdr10plus_ok and not self._video_apply_all:
            self._copy_hdr10plus_cb.setChecked(False)

    def _on_mode_changed(self, _idx: int = 0) -> None:
        mode = self._mode_combo.currentData()
        page = {
            QualityMode.CRF: 0,
            QualityMode.CQ: 1,
            QualityMode.BITRATE: 2,
            QualityMode.SIZE: 3,
        }.get(mode, 0)
        self._quality_stack.setCurrentIndex(page)
        self._rebuild_preview()

    def _on_hdr_toggle(self, _state: int) -> None:
        checked = self._inject_hdr_cb.isChecked()
        self._hdr_meta_widget.setVisible(checked)
        # Cohérence DV / HDR10+ : sans MDCV/CLL dans le BL, un passthrough
        # DV ou HDR10+ produit un fichier que la TV affiche en fade. On
        # désactive donc les passthrough quand l'utilisateur retire la case
        # HDR statique, et on les réactive quand il la recoche (selon ce
        # que la source autorise).
        if checked:
            self._tonemap_cb.setChecked(False)
            # Restaurer master_display/max_cll depuis le default figé à la
            # création du state. Recocher inject_hdr_meta = remettre les
            # valeurs source (override des éventuelles valeurs custom).
            # Si l'utilisateur veut conserver ses valeurs custom, il ne
            # décoche pas la case.
            state = (
                self._video_settings_by_entry_id.get(self._current_video_entry_id)
                if self._current_video_entry_id is not None
                else None
            ) or {}
            default_md = str(state.get("default_master_display") or "")
            default_cll = str(state.get("default_max_cll") or "")
            if default_md:
                self._master_display.setText(default_md)
            if default_cll:
                self._max_cll.setText(default_cll)
            # Réactiver les contrôles passthrough selon la source / le codec.
            self._update_passthrough_controls(auto_check=False)
            hdr = self._selected_video_hdr_type()
            codec = self._codec_combo.currentData() or "libx265"
            if self._backend_capabilities(codec).supports_dynamic_hdr:
                if hdr in (HDRType.DOLBY_VISION, HDRType.DOLBY_VISION_HDR10PLUS):
                    self._copy_dv_cb.setChecked(True)
                if hdr in (HDRType.HDR10PLUS, HDRType.DOLBY_VISION_HDR10PLUS):
                    self._copy_hdr10plus_cb.setChecked(True)
        else:
            # Décocher + désactiver DV/HDR10+ — sans MDCV/CLL le BL n'est
            # plus un HDR10 valide, donc le passthrough dynamique casserait.
            self._copy_dv_cb.setChecked(False)
            self._copy_dv_cb.setEnabled(False)
            self._copy_hdr10plus_cb.setChecked(False)
            self._copy_hdr10plus_cb.setEnabled(False)
            # Cocher tone-mapping si la source est HDR (transformation cohérente
            # vers SDR plutôt qu'un HDR cassé).
            hdr = self._selected_video_hdr_type()
            if hdr is not None and hdr != HDRType.NONE:
                self._tonemap_cb.setChecked(True)
        self._rebuild_preview()

    def _on_tonemap_toggle(self, _state: int) -> None:
        visible = self._tonemap_cb.isChecked()
        self._tonemap_algo_widget.setVisible(visible)
        if visible:
            self._inject_hdr_cb.setChecked(False)
        self._rebuild_preview()

    # ------------------------------------------------------------------
    # Éditeur visuel des params avancés
    # ------------------------------------------------------------------

    def _open_extra_params_dialog(self) -> None:
        codec = self._codec_combo.currentData() or "libx265"
        new_value = edit_extra_params(codec, self._extra_params.text(), self)
        if new_value is not None:
            self._extra_params.setText(new_value)

    # ------------------------------------------------------------------
    # Aperçu commande
    # ------------------------------------------------------------------

    def _rebuild_preview(self) -> None:
        if not hasattr(self, "_cmd_preview"):
            return   # appelé pendant l'init avant que le widget existe
        self._save_current_video_state()
        config = self._current_config()
        if config is None:
            self._cmd_preview.setPlainText("")
            return
        try:
            text = self._workflow.preview_command(config)
            self._cmd_preview.setPlainText(text)
        except Exception:
            self._cmd_preview.setPlainText(translate_text("(erreur de construction de la commande)"))

    def _on_preview_mode_changed(self) -> None:
        if not hasattr(self, "_preview_mode_combo"):
            return
        is_video = self._preview_mode_combo.currentData() == "video"
        self._preview_duration_spin.setEnabled(is_video)
        self._preview_time_edit.setEnabled(is_video)
        self._preview_random_btn.setEnabled(is_video)
        self._preview_video_result_row.setVisible(is_video)

    def _on_preview_time_edited(self, _text: str) -> None:
        self._preview_random_scene = False

    def _on_random_preview_scene(self) -> None:
        duration = max(0.0, float(self._duration_s or 0.0))
        preview_duration = (
            float(self._preview_duration_spin.value())
            if self._preview_mode_combo.currentData() == "video"
            else 2.0
        )
        max_start = max(0.0, duration - preview_duration) if duration > 0 else 0.0
        import random
        seconds = random.uniform(0.0, max_start) if max_start > 0 else 0.0
        self._preview_random_scene = True
        self._preview_time_edit.blockSignals(True)
        self._preview_time_edit.setText(self._format_preview_timecode(seconds))
        self._preview_time_edit.blockSignals(False)
        self._preview_scene_status.setText("Scène aléatoire prête. Le recalage HDR sera appliqué à la génération.")

    def _on_generate_preview(self) -> None:
        config = self._current_preview_config()
        if config is None:
            self._preview_status.setText("Sélectionnez une piste vidéo source pour générer une preview.")
            return
        try:
            timecode_s = self._parse_preview_timecode(self._preview_time_edit.text())
        except ValueError as exc:
            self._preview_status.setText(str(exc))
            return
        mode = str(self._preview_mode_combo.currentData() or "image")
        duration_s = float(self._preview_duration_spin.value()) if mode == "video" else 2.0
        request = EncodePreviewRequest(
            mode=mode,
            timecode_s=timecode_s,
            duration_s=duration_s,
            random_scene=self._preview_random_scene,
        )
        self._preview_random_scene = False
        self._set_preview_running(True)
        self._preview_status.setText("Génération de la preview…")
        self._preview_scene_status.setText("")
        self._preview_captures = []
        self._preview_current_index = 0
        self._preview_current_pixmap = None
        self._preview_image.setText("Génération…")
        self._preview_image.setPixmap(QPixmap())
        self._update_preview_nav_state()
        self._preview_video_path = None
        self._preview_video_path_label.setText("")
        self._preview_open_video_btn.setEnabled(False)
        self._preview_progress.setValue(0)
        self._preview_progress.setVisible(True)
        signals = self._workflow.run_preview(config, request)
        self._preview_signals = signals
        signals.progress.connect(self._on_preview_progress, Qt.ConnectionType.QueuedConnection)
        signals.progress_pct.connect(self._on_preview_progress_pct, Qt.ConnectionType.QueuedConnection)
        signals.finished.connect(self._on_preview_finished, Qt.ConnectionType.QueuedConnection)
        signals.failed.connect(self._on_preview_failed, Qt.ConnectionType.QueuedConnection)
        signals.cancelled.connect(self._on_preview_cancelled, Qt.ConnectionType.QueuedConnection)

    def _on_cancel_preview(self) -> None:
        if self._preview_signals is not None:
            self._preview_status.setText("Annulation de la preview…")
            self._preview_signals.cancel()

    _PREVIEW_SCAFFOLD_PREFIXES = (
        "Analyse des keyframes",
        "Probe HDR",
        "Scène preview",
        "Capture ",
        "Vignette ",
        "Préparation du segment",
        "Encodage du segment",
        "Extraction de l'image",
    )

    def _on_preview_progress(self, line: str) -> None:
        text = str(line or "").strip()
        if not text:
            return
        if text.startswith("$ "):
            return
        if text.startswith("Scène preview :"):
            self._preview_scene_status.setText(text.removeprefix("Scène preview :").strip())
            self.log_message.emit("INFO", text)
            return
        self._preview_status.setText(text[:300])
        if any(text.startswith(p) for p in self._PREVIEW_SCAFFOLD_PREFIXES):
            self.log_message.emit("INFO", text)

    def _on_preview_progress_pct(self, pct: int) -> None:
        value = max(0, min(100, int(pct)))
        if not self._preview_progress.isVisible():
            self._preview_progress.setVisible(True)
        self._preview_progress.setValue(value)

    def _on_preview_finished(self, result_text: str) -> None:
        try:
            payload = json.loads(result_text or "{}")
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict) or "mode" not in payload:
            return

        mode = str(payload.get("mode") or "")
        warning = str(payload.get("warning") or "").strip()
        raw_captures = payload.get("captures") or []
        captures: list[dict] = []
        for entry in raw_captures:
            if not isinstance(entry, dict):
                continue
            path_str = str(entry.get("image_path") or "")
            if not path_str:
                continue
            captures.append({
                "image_path": path_str,
                "scene_time_s": float(entry.get("scene_time_s") or 0.0),
                "label": str(entry.get("label") or ""),
            })
        self._preview_captures = captures
        self._preview_current_index = 0
        self._preview_current_pixmap = None

        if mode == "video":
            video_path = Path(str(payload.get("video_path") or ""))
            self._preview_video_path = video_path
            self._preview_video_path_label.setText(str(video_path))
            self._preview_open_video_btn.setEnabled(video_path.exists())
        else:
            self._preview_video_path = None
            self._preview_video_path_label.setText("")
            self._preview_open_video_btn.setEnabled(False)

        if captures:
            self._show_preview_capture(0)
            count = len(captures)
            label = "image" if mode == "image" else "vignette"
            plural = "s" if count > 1 else ""
            scene_text = f"{count} {label}{plural} disponible{plural}"
            if warning:
                scene_text += f" · {warning}"
            self._preview_scene_status.setText(scene_text)
            self._preview_status.setText(
                f"Preview {'image' if mode == 'image' else 'vidéo'} prête : {count} {label}{plural}."
            )
        else:
            self._preview_image.setText("Aucune image générée")
            self._preview_image.setPixmap(QPixmap())
            self._preview_current_pixmap = None
            self._preview_scene_status.setText(warning or "")
            self._preview_status.setText("Preview terminée sans image exploitable.")

        self._set_preview_running(False)
        self._update_preview_nav_state()

    def _show_preview_capture(self, index: int) -> None:
        if not self._preview_captures:
            self._preview_current_pixmap = None
            self._preview_image.setPixmap(QPixmap())
            self._preview_image.setText("Aucune image générée")
            self._update_preview_nav_state()
            return
        index = max(0, min(len(self._preview_captures) - 1, int(index)))
        self._preview_current_index = index
        capture = self._preview_captures[index]
        path = capture["image_path"]
        pix = QPixmap(path)
        if pix.isNull():
            self._preview_current_pixmap = None
            self._preview_image.setText(f"Image illisible : {path}")
        else:
            self._preview_current_pixmap = pix
            self._apply_preview_zoom()
        self._update_preview_nav_state()

    def _update_preview_nav_state(self) -> None:
        total = len(self._preview_captures)
        if total <= 0:
            self._preview_index_label.setText("— / —")
            self._preview_prev_btn.setEnabled(False)
            self._preview_next_btn.setEnabled(False)
            return
        idx = self._preview_current_index
        capture = self._preview_captures[idx]
        label_suffix = capture.get("label") or ""
        text = f"{idx + 1} / {total}"
        if label_suffix:
            text += f"  {label_suffix}"
        self._preview_index_label.setText(text)
        self._preview_prev_btn.setEnabled(total > 1)
        self._preview_next_btn.setEnabled(total > 1)

    def _on_preview_prev(self) -> None:
        if not self._preview_captures:
            return
        total = len(self._preview_captures)
        self._show_preview_capture((self._preview_current_index - 1) % total)

    def _on_preview_next(self) -> None:
        if not self._preview_captures:
            return
        total = len(self._preview_captures)
        self._show_preview_capture((self._preview_current_index + 1) % total)

    def _on_preview_zoom_changed(self, value: int) -> None:
        self._preview_zoom_percent = max(10, min(400, int(value)))
        self._preview_zoom_value_lbl.setText(f"{self._preview_zoom_percent} %")
        self._apply_preview_zoom()

    def _on_preview_zoom_fit(self) -> None:
        if self._preview_current_pixmap is None or self._preview_current_pixmap.isNull():
            return
        viewport = self._preview_scroll.viewport().size()
        pix = self._preview_current_pixmap
        if pix.width() <= 0 or pix.height() <= 0:
            return
        ratio_w = (viewport.width() - 8) / pix.width()
        ratio_h = (viewport.height() - 8) / pix.height()
        ratio = max(0.01, min(ratio_w, ratio_h))
        pct = max(10, min(400, int(round(ratio * 100))))
        if pct != self._preview_zoom_slider.value():
            self._preview_zoom_slider.blockSignals(True)
            self._preview_zoom_slider.setValue(pct)
            self._preview_zoom_slider.blockSignals(False)
        self._preview_zoom_percent = pct
        self._preview_zoom_value_lbl.setText(f"{pct} %")
        self._apply_preview_zoom()

    def _apply_preview_zoom(self) -> None:
        if self._preview_current_pixmap is None or self._preview_current_pixmap.isNull():
            return
        pix = self._preview_current_pixmap
        target_w = max(1, int(pix.width() * self._preview_zoom_percent / 100))
        target_h = max(1, int(pix.height() * self._preview_zoom_percent / 100))
        scaled = pix.scaled(
            target_w,
            target_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._preview_image.setPixmap(scaled)
        self._preview_image.resize(scaled.size())

    def _on_preview_failed(self, message: str, _exc: object) -> None:
        self._preview_status.setText(f"Preview échouée : {message}")
        self._set_preview_running(False)

    def _on_preview_cancelled(self) -> None:
        self._preview_status.setText("Preview annulée.")
        self._set_preview_running(False)

    def _set_preview_running(self, running: bool) -> None:
        self._preview_generate_btn.setEnabled(not running)
        self._preview_cancel_btn.setEnabled(running)
        self._preview_mode_combo.setEnabled(not running)
        is_video = self._preview_mode_combo.currentData() == "video"
        active = (not running) and is_video
        self._preview_time_edit.setEnabled(active)
        self._preview_random_btn.setEnabled(active)
        self._preview_duration_spin.setEnabled(active)
        if not running:
            self._preview_signals = None
            self._preview_progress.setVisible(False)
            self._preview_progress.setValue(0)

    def _open_preview_video(self) -> None:
        if self._preview_video_path is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._preview_video_path)))

    def _current_preview_config(self) -> EncodeConfig | None:
        if self._file_info is None:
            return None
        self._save_current_video_state()
        video = self._current_video_settings()
        source = Path(video.source_path or self._file_info.path)
        output = self._output_provider()
        if output is None:
            output = source.with_name(f"{source.stem}.preview.mkv")
        return EncodeConfig(
            source=source,
            output=output,
            video=video,
            video_tracks=[video],
            audio_tracks=[],
            copy_subtitles=False,
            duration_s=self._duration_s,
            copy_dv=video.copy_dv,
            copy_hdr10plus=video.copy_hdr10plus,
            dovi_profile=video.dovi_profile,
            work_dir=self._config.work_dir,
            file_title="",
            extra_attachments=[],
            tmdb_cover=None,
            tag_overrides={},
            chapter_overrides=[],
        )

    @staticmethod
    def _parse_preview_timecode(text: str) -> float:
        raw = str(text or "").strip()
        if not raw:
            return 0.0
        if re.fullmatch(r"\d+(?:[.,]\d+)?", raw):
            return max(0.0, float(raw.replace(",", ".")))
        match = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:[.,](\d{1,3}))?", raw)
        if not match:
            raise ValueError("Timecode invalide. Format attendu : HH:MM:SS.mmm")
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        seconds = int(match.group(3) or 0)
        millis_raw = match.group(4) or "0"
        millis = int(millis_raw.ljust(3, "0")[:3])
        if minutes >= 60 or seconds >= 60:
            raise ValueError("Timecode invalide. Minutes et secondes doivent être inférieures à 60.")
        return hours * 3600 + minutes * 60 + seconds + millis / 1000.0

    @staticmethod
    def _format_preview_timecode(seconds: float) -> str:
        seconds = max(0.0, float(seconds or 0.0))
        whole = int(seconds)
        millis = int(round((seconds - whole) * 1000))
        if millis >= 1000:
            whole += 1
            millis -= 1000
        hours, rem = divmod(whole, 3600)
        minutes, secs = divmod(rem, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"

    # ------------------------------------------------------------------
    # Profils
    # ------------------------------------------------------------------

    def _refresh_profiles(self, *, select: str | None = None) -> None:
        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        names = self._profiles.names()
        if not names:
            self._profile_combo.addItem(translate_text("(aucun profil enregistré)"), None)
            self._profile_combo.setEnabled(False)
        else:
            self._profile_combo.setEnabled(True)
            for name in names:
                self._profile_combo.addItem(name, name)
            if select is not None:
                idx = next((i for i in range(self._profile_combo.count())
                            if self._profile_combo.itemData(i) == select), 0)
                self._profile_combo.setCurrentIndex(idx)
        self._profile_combo.blockSignals(False)

    def _load_profile(self) -> None:
        name = self._profile_combo.currentData()
        if not name:
            return
        presets = {p.name: p for p in self._profiles.load_all()}
        if name not in presets:
            self.log_message.emit("WARN", translate_text("Profil introuvable : {name}", name=name))
            self._refresh_profiles()
            return
        preset = presets[name]
        vs = preset.to_video_settings()
        # Codec — déclenche _on_codec_changed → reconstruit le mode_combo
        for i in range(self._codec_combo.count()):
            if self._codec_combo.itemData(i) == vs.codec:
                self._codec_combo.setCurrentIndex(i)
                break
        # Mode qualité (après refresh par _on_codec_changed)
        for i in range(self._mode_combo.count()):
            if self._mode_combo.itemData(i) == QualityMode(preset.quality_mode):
                self._mode_combo.setCurrentIndex(i)
                break
        # Preset codec
        for i in range(self._preset_combo.count()):
            if self._preset_combo.itemData(i) == preset.preset:
                self._preset_combo.setCurrentIndex(i)
                break
        self._crf_slider.setValue(vs.crf)
        self._cq_slider.setValue(vs.cq)
        self._bitrate_edit.setText(str(vs.bitrate_kbps))
        self._size_edit.setText(str(vs.target_size_mb))
        self._extra_params.setText(vs.extra_params)
        self._apply_resize_settings(vs.resize)
        self._apply_crop_settings(vs.crop)
        self._apply_filter_settings(vs.filters)
        # Les options HDR (inject_hdr_meta / master_display / max_cll /
        # tonemap_to_sdr) dépendent de la source, pas du profil — un même
        # profil "x265 CRF18 slow" doit s'appliquer à une source SDR ou HDR
        # sans imposer/écraser ce qui a été détecté côté source.
        idx_algo = next((i for i in range(self._tonemap_algo.count())
                         if self._tonemap_algo.itemData(i) == vs.tonemap_algorithm), 0)
        self._tonemap_algo.setCurrentIndex(idx_algo)
        self._save_current_video_state()
        self._rebuild_preview()
        self.log_message.emit("OK", translate_text("Profil chargé : {name}", name=name))

    def _save_profile(self) -> None:
        name = self._profile_name.text().strip()
        if not name:
            name, ok = QInputDialog.getText(
                self,
                translate_text("Enregistrer le profil"),
                translate_text("Nom du profil :"),
            )
            if not ok or not name.strip():
                return
            name = name.strip()

        if name in self._profiles.names():
            confirm = QMessageBox.question(
                self,
                translate_text("Écraser le profil ?"),
                translate_text("Un profil « {name} » existe déjà. L'écraser ?", name=name),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return

        vs = self._current_video_settings()
        # Les options HDR (inject_hdr_meta / master_display / max_cll /
        # tonemap_to_sdr) dépendent de la source — on ne les sauve PAS dans
        # le profil. tonemap_algorithm est un préréglage cohérent à garder
        # (algorithme préféré quand le tone-mapping est activé).
        preset = EncodePreset(
            name=name,
            codec=vs.codec,
            quality_mode=vs.quality_mode.value if isinstance(vs.quality_mode, QualityMode) else str(vs.quality_mode),
            crf=vs.crf,
            cq=vs.cq,
            bitrate_kbps=vs.bitrate_kbps,
            target_size_mb=vs.target_size_mb,
            preset=vs.preset,
            extra_params=vs.extra_params,
            resize=vs.resize,
            crop=vs.crop,
            filters=vs.filters,
            inject_hdr_meta=False,
            master_display="",
            max_cll="",
            tonemap_to_sdr=False,
            tonemap_algorithm=vs.tonemap_algorithm,
        )
        try:
            self._profiles.save(preset)
        except OSError as exc:
            self.log_message.emit("ERROR", translate_text("Échec sauvegarde profil : {err}", err=str(exc)))
            return
        self._refresh_profiles(select=name)
        self._profile_name.clear()
        self.log_message.emit("OK", translate_text("Profil enregistré : {name}", name=name))

    def _delete_profile(self) -> None:
        name = self._profile_combo.currentData()
        if not name:
            return
        confirm = QMessageBox.question(
            self,
            translate_text("Supprimer le profil ?"),
            translate_text("Supprimer définitivement le profil « {name} » ?", name=name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._profiles.delete(name)
        self._refresh_profiles()
        self.log_message.emit("INFO", translate_text("Profil supprimé : {name}", name=name))

    # ------------------------------------------------------------------
    # API publique — exécution (déléguée à MainWindow)
    # ------------------------------------------------------------------

    def collect_config(self) -> "EncodeConfig | None":
        """Retourne la configuration d'encodage courante, ou None si incomplète."""
        return self._current_config()

    def get_duration_s(self) -> "float | None":
        """Durée de la source sélectionnée (pour le calcul de progression dans MainWindow)."""
        return self._duration_s

    def get_total_frames(self) -> "int | None":
        """Nombre total de frames de la source (fallback progression quand out_time=N/A)."""
        info = self._file_info
        if info is None:
            return None
        frames = getattr(info, "frame_count", None)
        if isinstance(frames, int) and frames > 0:
            return frames
        return None

    def get_video_progress_targets(self, config: "EncodeConfig") -> dict[int, dict[str, object]]:
        """
        Retourne les métriques de progression par piste vidéo routée.

        Clé : index 1-based de la piste dans `config.video_tracks`.
        Valeurs : `duration_s`, `total_frames`, `source_name`.
        """
        targets: dict[int, dict[str, object]] = {}
        video_tracks = self._routing_video_tracks(config)
        if not video_tracks:
            return targets

        entry_map: dict[str, tuple[FileInfo, TrackEntry]] = {}
        source_map: dict[tuple[Path, int], tuple[FileInfo, TrackEntry]] = {}
        for info, track, _color in self._video_tracks:
            entry_map[self._video_entry_id(track)] = (info, track)
            source_map[(Path(info.path), int(track.mkv_tid))] = (info, track)

        for index, video in enumerate(video_tracks, start=1):
            current_info: FileInfo | None = None
            current_track: TrackEntry | None = None
            entry_id = str(getattr(video, "track_entry_id", "") or "").strip()
            source_path = Path(getattr(video, "source_path", None) or config.source)
            stream_index = int(getattr(video, "stream_index", 0) or 0)

            if entry_id:
                resolved = entry_map.get(entry_id)
                if resolved is not None:
                    current_info, current_track = resolved
            if current_info is None:
                resolved = source_map.get((source_path, stream_index))
                if resolved is not None:
                    current_info, current_track = resolved
            if current_info is None and self._file_info is not None and Path(self._file_info.path) == source_path:
                current_info = self._file_info

            duration_s: float | None = None
            total_frames: int | None = None
            if current_info is not None:
                resolved_track = self._video_track_for_entry(current_info, current_track)
                duration_s = (
                    getattr(resolved_track, "duration_s", None)
                    or getattr(current_info, "duration_s", None)
                    or config.duration_s
                )
                frames_obj = getattr(current_info, "frame_count", None)
                if isinstance(frames_obj, int) and frames_obj > 0:
                    total_frames = frames_obj
            else:
                duration_s = config.duration_s

            targets[index] = {
                "duration_s": duration_s if isinstance(duration_s, (int, float)) and duration_s > 0 else None,
                "total_frames": total_frames,
                "source_name": source_path.name,
            }
        return targets

    def run_operation(self, config: "EncodeConfig") -> "TaskSignals":
        """Lance l'encodage et retourne les signaux de progression."""
        # MainWindow valide juste avant l'appel ; éviter un second passage I/O
        # dans le thread UI au moment de démarrer l'encodage.
        return self._workflow.run(config, validate=False)

    def validate_config(self, config: "EncodeConfig") -> list[str]:
        """Retourne la liste des erreurs de validation (vide = OK)."""
        return self._workflow.validate(config)

    def parse_progress_line(self, config: "EncodeConfig", line: str):
        """Normalise une ligne de progression via le backend actif."""
        return self._workflow.parse_progress(config, line)

    def is_pure_copy(self, config: "EncodeConfig") -> bool:
        """True si tout est en copie et qu'aucune transformation vidéo n'est demandée."""
        video_tracks = self._routing_video_tracks(config)
        if not video_tracks:
            return False
        return (
            all(
                str(getattr(video, "codec", "") or "").strip().lower() == "copy"
                and not bool(getattr(video, "inject_hdr_meta", False))
                and not bool(getattr(video, "tonemap_to_sdr", False))
                and not self._video_requires_dovi_profile_normalization(video)
                for video in video_tracks
            )
            and all(
                str(getattr(audio, "codec", "") or "").strip().lower() == "copy"
                for audio in getattr(config, "audio_tracks", []) or []
            )
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _video_entry_id(self, track: TrackEntry) -> str:
        if track.entry_id:
            return track.entry_id
        if track.source_entry_id:
            return track.source_entry_id
        return f"{track.file_id}:{track.track_type}:{track.mkv_tid}"

    @classmethod
    def _is_h264_codec(cls, codec: str) -> bool:
        return is_h264_video_codec(codec)

    @classmethod
    def _is_dynamic_hdr_codec(cls, codec: str) -> bool:
        return cls._backend_capabilities(codec).supports_dynamic_hdr

    @staticmethod
    def _backend_capabilities(codec: str):
        return backend_capabilities_for_codec(codec)

    @staticmethod
    def _effective_static_hdr_fields(
        state: dict[str, object],
        *,
        codec: str,
    ) -> tuple[str, str]:
        current_md = str(state.get("master_display") or "").strip()
        current_cll = str(state.get("max_cll") or "").strip()
        default_md = str(state.get("default_master_display") or "").strip()
        default_cll = str(state.get("default_max_cll") or "").strip()
        if (
            backend_capabilities_for_codec(codec).supports_manual_static_hdr
            or EncodePanel._state_allows_copy_static_hdr_edit(state, codec)
        ):
            return current_md or default_md, current_cll or default_cll
        return default_md, default_cll

    @staticmethod
    def _state_allows_copy_static_hdr_edit(state: dict[str, object], codec: str) -> bool:
        return bool(
            str(codec or "").strip().lower() == "copy"
            and bool(state.get("inject_hdr_meta"))
            and bool(state.get("copy_dv"))
            and str(state.get("dovi_profile") or "0").strip() == "2"
        )

    @staticmethod
    def _source_has_dv(source_hdr: HDRType) -> bool:
        return source_hdr in (HDRType.DOLBY_VISION, HDRType.DOLBY_VISION_HDR10PLUS)

    @staticmethod
    def _source_has_hdr10plus(source_hdr: HDRType) -> bool:
        return source_hdr in (HDRType.HDR10PLUS, HDRType.DOLBY_VISION_HDR10PLUS)

    def _effective_dynamic_hdr_flags(
        self,
        state: dict[str, object],
        *,
        source_hdr: HDRType,
        target_codec: str | None = None,
    ) -> tuple[bool, bool]:
        codec = (
            str(target_codec or "").strip().lower()
            if target_codec is not None
            else self._video_state_target_codec(state)
        )
        supports_dynamic_hdr = self._is_dynamic_hdr_codec(codec)
        copy_dv = bool(state.get("copy_dv")) and supports_dynamic_hdr and self._source_has_dv(source_hdr)
        copy_hdr10plus = bool(state.get("copy_hdr10plus")) and supports_dynamic_hdr and self._source_has_hdr10plus(source_hdr)
        return copy_dv, copy_hdr10plus

    def _normalized_video_state_for_track(
        self,
        *,
        info: FileInfo,
        track: TrackEntry,
        state: dict[str, object],
    ) -> dict[str, object]:
        normalized = self._copy_video_state(state)
        source_hdr = self._hdr_type_for_entry(info, track)
        target_codec = self._video_state_target_codec(normalized)
        copy_dv, copy_hdr10plus = self._effective_dynamic_hdr_flags(
            normalized,
            source_hdr=source_hdr,
            target_codec=target_codec,
        )
        normalized["copy_dv"] = copy_dv
        normalized["copy_hdr10plus"] = copy_hdr10plus
        md, cll = self._effective_static_hdr_fields(normalized, codec=target_codec)
        normalized["master_display"] = md
        normalized["max_cll"] = cll
        return normalized

    def _default_video_state_for_track(
        self,
        *,
        info: FileInfo,
        track: TrackEntry,
    ) -> dict[str, object]:
        source_hdr = self._hdr_type_for_entry(info, track)
        source_video = self._video_track_for_entry(info, track)
        raw = source_video.raw if source_video is not None else {}
        master_display, max_cll = self._extract_hdr_meta_fields(
            raw,
            info.path,
            mediainfo_json=info.mediainfo_json,
        )
        bit_depth = self._video_source_bit_depth(info, track)
        default_10bit = bit_depth >= 10 or self._source_has_dv(source_hdr) or self._source_has_hdr10plus(source_hdr)
        # Source HDR (HDR10/HDR10+/DV) → injection HDR statique activée par
        # défaut. Sans master_display/max-cll dans le HEVC encodé, les TV
        # et players appliquent un tone-mapping générique → image fade.
        is_hdr_source = source_hdr is not None and source_hdr != HDRType.NONE
        state: dict[str, object] = {
            "codec": "copy",
            "quality_mode": QualityMode.CRF,
            "preset": "slow",
            "crf": 18,
            "cq": 26,
            "bitrate_kbps": "5000",
            "target_size_mb": "4000",
            "extra_params": "",
            "force_10bit": default_10bit,
            "resize": VideoResizeSettings(),
            "crop": VideoCropSettings(),
            "filters": VideoFilterSettings(),
            "inject_hdr_meta": is_hdr_source,
            "master_display": master_display,
            "max_cll": max_cll,
            # Valeurs source figées (extraites mediainfo/ffprobe au 1er chargement) :
            # servent de mémoire pour reset après décoche/recoche, ou si l'utilisateur
            # vide les champs par erreur. Ne sont jamais écrasées au cours de la session.
            "default_master_display": master_display,
            "default_max_cll": max_cll,
            "static_hdr_metadata_source": "",
            "static_hdr_metadata_confidence": "",
            "static_hdr_metadata_analysis_mode": "",
            "static_hdr_metadata_analysis_request": "",
            "copy_dv": self._source_has_dv(source_hdr),
            "copy_hdr10plus": self._source_has_hdr10plus(source_hdr),
            "dovi_profile": "0",
            "tonemap_to_sdr": False,
            "tonemap_algorithm": "hable",
        }
        return self._normalized_video_state_for_track(
            info=info,
            track=track,
            state=state,
        )

    def _should_propagate_global_state(self, state: dict[str, object] | None) -> bool:
        if not self._video_apply_all or state is None:
            return False
        return self._video_state_target_codec(state) != "copy"

    def _video_source_bit_depth(self, info: FileInfo, track: TrackEntry) -> int:
        video_track = self._video_track_for_entry(info, track)
        if video_track is None:
            return 8
        try:
            bit_depth = int(getattr(video_track, "bit_depth", 8) or 8)
        except (TypeError, ValueError):
            bit_depth = 8
        return bit_depth if bit_depth > 0 else 8

    def _video_force_8bit_for_codec(
        self,
        info: FileInfo,
        track: TrackEntry,
        codec: str,
    ) -> bool:
        return self._is_h264_codec(codec) and self._video_source_bit_depth(info, track) > 8

    def _effective_force_10bit(
        self,
        *,
        info: FileInfo | None,
        track: TrackEntry | None,
        codec: str,
        state_force_10bit: bool,
    ) -> bool:
        target = (codec or "").strip().lower()
        if target == "copy" or not supports_10bit(target):
            return False
        if info is not None and track is not None and self._video_force_8bit_for_codec(info, track, target):
            return False
        return bool(state_force_10bit)

    def _video_source_row_text(
        self,
        info: FileInfo,
        track: TrackEntry,
        *,
        state: dict[str, object] | None,
    ) -> str:
        source_codec = (track.orig_codec or track.codec).upper()
        text = f"█  {info.path.name}    {source_codec}  {track.display_info}"
        if state is None:
            return text

        plan = self._video_plan_from_state(
            entry_id=self._video_entry_id(track),
            state=state,
            source_video=self._video_track_for_entry(info, track),
        )
        badges: list[str] = []
        target_codec = str(plan.target_codec or "copy").strip().lower()
        if target_codec != "copy":
            badges.append(encoder_badge(target_codec))
        if self._video_force_8bit_for_codec(info, track, target_codec):
            badges.append("8-bit")
        badges.extend(self._sorted_video_hdr_badges(plan.hdr_badges))
        badges.extend(plan.filter_badges)
        if badges:
            text += "    " + " ".join(f"[{badge}]" for badge in badges)
        return text

    def _sorted_video_hdr_badges(self, badges: tuple[str, ...]) -> list[str]:
        order = {badge: idx for idx, badge in enumerate(self._VIDEO_HDR_BADGE_ORDER)}
        unique = list(dict.fromkeys(str(badge or "").strip().upper() for badge in badges if str(badge or "").strip()))
        return sorted(unique, key=lambda badge: (order.get(badge, len(order)), badge))

    def _refresh_video_source_rows(self) -> None:
        if not hasattr(self, "_video_list"):
            return
        for row, (file_info, track, _color) in enumerate(self._video_tracks):
            item = self._video_list.item(row)
            if item is None:
                continue
            entry_id = self._video_entry_id(track)
            state = self._video_settings_by_entry_id.get(entry_id)
            item.setText(self._video_source_row_text(file_info, track, state=state))
            self._apply_video_source_item_style(item, state)

    @staticmethod
    def _video_state_target_codec(state: dict[str, object] | None) -> str:
        if state is None:
            return "copy"
        return str(state.get("codec") or "copy").strip().lower()

    def _apply_video_source_item_style(
        self,
        item: QListWidgetItem,
        state: dict[str, object] | None,
    ) -> None:
        font = item.font()
        font.setBold(self._video_state_target_codec(state) != "copy")
        item.setFont(font)

    def _video_track_for_entry(
        self,
        info: FileInfo,
        track: TrackEntry | None,
    ) -> VideoTrack | None:
        """Retourne la piste vidéo réelle associée au TrackEntry remux."""
        if track is not None:
            for video in info.video_tracks:
                if video.index == track.mkv_tid:
                    return video
        return info.primary_video

    def _hdr_type_for_entry(self, info: FileInfo, track: TrackEntry | None) -> HDRType:
        video = self._video_track_for_entry(info, track)
        return video.hdr_type if video is not None else info.hdr_type

    def _selected_video_hdr_type(self) -> HDRType:
        row = self._video_list.currentRow() if hasattr(self, "_video_list") else -1
        if 0 <= row < len(self._video_tracks):
            file_info, track, _color = self._video_tracks[row]
            return self._hdr_type_for_entry(file_info, track)
        if self._file_info is not None:
            return self._hdr_type_for_entry(self._file_info, None)
        return HDRType.NONE

    def _combo_data(self, combo: QComboBox) -> object:
        return combo.currentData()

    def _set_combo_data(self, combo: QComboBox, value: object) -> None:
        for idx in range(combo.count()):
            if combo.itemData(idx) == value:
                combo.setCurrentIndex(idx)
                return

    def _active_video_entry_ids(self) -> list[str]:
        return [self._video_entry_id(track) for _info, track, _color in self._video_tracks]

    @staticmethod
    def _routing_video_tracks(config: object) -> list[object]:
        tracks = list(getattr(config, "video_tracks", []) or [])
        if tracks:
            return tracks
        primary = getattr(config, "video", None)
        return [primary] if primary is not None else []

    @staticmethod
    def _video_requires_dovi_profile_normalization(video: object) -> bool:
        return bool(
            getattr(video, "copy_dv", False)
            and str(getattr(video, "dovi_profile", "0") or "0").strip() == "2"
        )

    def _ensure_video_states_for_active_tracks(self) -> None:
        """Garantit un état UI pour chaque piste vidéo active du remux."""
        missing_tracks = [
            (info, track)
            for info, track, _color in self._video_tracks
            if self._video_entry_id(track) not in self._video_settings_by_entry_id
        ]
        if not missing_tracks:
            return

        template_state = (
            self._video_settings_by_entry_id.get(self._current_video_entry_id)
            if self._current_video_entry_id is not None
            else None
        )
        if template_state is None and self._video_settings_by_entry_id:
            template_state = next(iter(self._video_settings_by_entry_id.values()))
        if template_state is None and hasattr(self, "_codec_combo") and hasattr(self, "_copy_dv_cb"):
            template_state = self._current_video_state()
        if template_state is None:
            return

        propagate_global = self._should_propagate_global_state(template_state)
        for info, track in missing_tracks:
            entry_id = self._video_entry_id(track)
            if propagate_global:
                state = self._normalized_video_state_for_track(
                    info=info,
                    track=track,
                    state=template_state,
                )
            else:
                state = self._default_video_state_for_track(info=info, track=track)
            self._video_settings_by_entry_id[entry_id] = state
        self._emit_video_encoding_plans()

    def _current_resize_settings(self) -> VideoResizeSettings:
        if not hasattr(self, "_resize_enabled_cb"):
            return VideoResizeSettings()
        return VideoResizeSettings(
            enabled=self._resize_enabled_cb.isChecked(),
            mode=str(self._resize_mode_combo.currentData() or "preset"),
            preset=str(self._resize_preset_combo.currentData() or "720p"),
            percent=int(self._resize_percent_spin.value()),
            width=int(self._resize_width_spin.value()),
            height=int(self._resize_height_spin.value()),
            keep_aspect=self._resize_keep_aspect_cb.isChecked(),
            allow_upscale=self._resize_allow_upscale_cb.isChecked(),
            algorithm=str(self._resize_algo_combo.currentData() or "lanczos"),
        )

    def _current_crop_settings(self) -> VideoCropSettings:
        if not hasattr(self, "_crop_enabled_cb"):
            return VideoCropSettings()
        return VideoCropSettings(
            enabled=self._crop_enabled_cb.isChecked(),
            unit=str(self._crop_unit_combo.currentData() or "px"),
            top=int(self._crop_top_spin.value()),
            bottom=int(self._crop_bottom_spin.value()),
            left=int(self._crop_left_spin.value()),
            right=int(self._crop_right_spin.value()),
            auto=self._crop_auto_cb.isChecked(),
        )

    def _current_filter_settings(self) -> VideoFilterSettings:
        if not hasattr(self, "_yadif_cb"):
            return VideoFilterSettings()
        return VideoFilterSettings(
            yadif_enabled=self._yadif_cb.isChecked(),
            yadif_mode=str(self._yadif_mode_combo.currentData() or "send_frame"),
            yadif_parity=str(self._yadif_parity_combo.currentData() or "auto"),
            yadif_deint="all",
            deblock_enabled=self._deblock_cb.isChecked(),
            deblock_strength=str(self._deblock_strength_combo.currentData() or "medium"),
            deblock_block=int(str(self._deblock_block_combo.currentData() or "8")),
            nlmeans_enabled=self._nlmeans_cb.isChecked(),
            nlmeans_strength=str(self._nlmeans_strength_combo.currentData() or "light"),
            nlmeans_profile=str(self._nlmeans_profile_combo.currentData() or "standard"),
            chroma_smooth_enabled=self._chroma_cb.isChecked(),
            chroma_smooth_strength=str(self._chroma_strength_combo.currentData() or "medium"),
        )

    def _current_video_state(self) -> dict[str, object]:
        # default_master_display / default_max_cll sont figés à la création
        # du state initial (cf. _default_video_state_for_track) — on les
        # préserve depuis le state existant pour qu'ils survivent aux
        # rebuilds successifs et restent disponibles pour reset UI.
        prev = (
            self._video_settings_by_entry_id.get(self._current_video_entry_id)
            if self._current_video_entry_id is not None
            else None
        ) or {}
        return {
            "codec": self._combo_data(self._codec_combo),
            "quality_mode": self._combo_data(self._mode_combo),
            "preset": self._combo_data(self._preset_combo),
            "crf": self._crf_spin.value(),
            "cq": self._cq_spin.value(),
            "bitrate_kbps": self._bitrate_edit.text(),
            "target_size_mb": self._size_edit.text(),
            "extra_params": self._extra_params.text(),
            "force_10bit": bool(self._ten_bit_cb.isChecked()) if hasattr(self, "_ten_bit_cb") else False,
            "resize": self._current_resize_settings(),
            "crop": self._current_crop_settings(),
            "filters": self._current_filter_settings(),
            "inject_hdr_meta": self._inject_hdr_cb.isChecked(),
            "master_display": self._master_display.text(),
            "max_cll": self._max_cll.text(),
            "default_master_display": str(prev.get("default_master_display") or ""),
            "default_max_cll": str(prev.get("default_max_cll") or ""),
            "static_hdr_metadata_source": str(prev.get("static_hdr_metadata_source") or ""),
            "static_hdr_metadata_confidence": str(prev.get("static_hdr_metadata_confidence") or ""),
            "static_hdr_metadata_analysis_mode": str(prev.get("static_hdr_metadata_analysis_mode") or ""),
            "static_hdr_metadata_analysis_request": str(prev.get("static_hdr_metadata_analysis_request") or ""),
            "copy_dv": self._copy_dv_cb.isChecked(),
            "copy_hdr10plus": self._copy_hdr10plus_cb.isChecked(),
            "dovi_profile": self._combo_data(self._dovi_profile_combo),
            "tonemap_to_sdr": self._tonemap_cb.isChecked(),
            "tonemap_algorithm": self._combo_data(self._tonemap_algo),
        }

    def _copy_video_state(self, state: dict[str, object]) -> dict[str, object]:
        return dict(state)

    def _propagate_current_video_state_to_all(self, *, force_current: bool = True) -> None:
        if not self._video_tracks:
            return
        if force_current:
            self._save_current_video_state()
        source_id = self._current_video_entry_id or self._active_video_entry_ids()[0]
        state = self._video_settings_by_entry_id.get(source_id)
        if state is None:
            state = self._current_video_state()
        for entry_id in self._active_video_entry_ids():
            self._video_settings_by_entry_id[entry_id] = self._copy_video_state(state)
        self._emit_video_encoding_plans()

    @staticmethod
    def _quality_value_summary(mode: QualityMode, state: dict[str, object]) -> str:
        if mode == QualityMode.CRF:
            return str(state.get("crf") or 18)
        if mode == QualityMode.CQ:
            return str(state.get("cq") or 26)
        if mode == QualityMode.BITRATE:
            return f"{state.get('bitrate_kbps') or '5000'} kbps"
        return f"{state.get('target_size_mb') or '4000'} Mo"

    @staticmethod
    def _summary_mode_label(mode: QualityMode) -> str:
        return {
            QualityMode.CRF: "CRF",
            QualityMode.CQ: "CQ",
            QualityMode.BITRATE: "Débit",
            QualityMode.SIZE: "Taille",
        }.get(mode, "CRF")

    def _video_hdr_badges_from_state(
        self,
        state: dict[str, object],
        *,
        source_video: VideoTrack | None,
    ) -> tuple[str, ...]:
        source_hdr = source_video.hdr_type if source_video is not None else HDRType.NONE
        target_codec = self._video_state_target_codec(state)
        copy_dv, copy_hdr10plus = self._effective_dynamic_hdr_flags(
            state,
            source_hdr=source_hdr,
            target_codec=target_codec,
        )
        if bool(state.get("tonemap_to_sdr")):
            return ("SDR",)

        badges: list[str] = []
        if bool(state.get("inject_hdr_meta")):
            badges.append("HDR")
        if copy_dv:
            badges.append("DV")
        if copy_hdr10plus:
            badges.append("10+")

        if badges:
            return tuple(badges)

        if target_codec != "copy":
            return ()

        if source_hdr == HDRType.HDR10:
            return ("HDR",)
        if source_hdr == HDRType.HDR10PLUS:
            return ("10+",)
        if source_hdr == HDRType.HLG:
            return ("HLG",)
        if source_hdr == HDRType.DOLBY_VISION:
            compat_label = source_video.dovi_compat_label if source_video is not None else None
            if compat_label == "HDR10":
                return ("DV", "HDR")
            if compat_label == "HLG":
                return ("DV", "HLG")
            # P8.0 (compat_id=0) ou P8.2 (SDR) → pas de badge fallback HDR
            return ("DV",)
        if source_hdr == HDRType.DOLBY_VISION_HDR10PLUS:
            return ("DV", "10+")
        return ()

    def _video_filter_badges_from_state(self, state: dict[str, object]) -> tuple[str, ...]:
        badges: list[str] = []
        resize = VideoResizeSettings.from_value(state.get("resize"))
        crop = VideoCropSettings.from_value(state.get("crop"))
        filters = VideoFilterSettings.from_value(state.get("filters"))
        if resize.is_active():
            if resize.mode == "preset":
                badges.append(str(resize.preset or "Resize"))
            elif resize.mode == "percent":
                badges.append(f"{int(resize.percent or 100)}%")
            else:
                badges.append(f"{int(resize.width or 0)}x{int(resize.height or 0)}")
        if crop.is_active():
            badges.append("AutoCrop" if crop.auto else "Crop")
        if filters.yadif_enabled:
            badges.append("Yadif")
        if filters.deblock_enabled:
            badges.append("Deblock")
        if filters.nlmeans_enabled:
            badges.append("NLMeans")
        if filters.chroma_smooth_enabled:
            badges.append("Chroma")
        return tuple(badges)

    def _video_plan_from_state(
        self,
        *,
        entry_id: str,
        state: dict[str, object],
        source_video: VideoTrack | None,
    ) -> VideoTrackEncodePlan:
        source_hdr = source_video.hdr_type if source_video is not None else HDRType.NONE
        codec = str(state.get("codec") or "copy")
        target_codec = str(codec or "copy").strip().lower()
        copy_dv, copy_hdr10plus = self._effective_dynamic_hdr_flags(
            state,
            source_hdr=source_hdr,
            target_codec=target_codec,
        )
        mode = state.get("quality_mode") or QualityMode.CRF
        if not isinstance(mode, QualityMode):
            mode = QualityMode(str(mode))
        if codec == "copy":
            summary = "Copy"
        else:
            summary = " - ".join([
                codec,
                str(state.get("preset") or "slow"),
                self._summary_mode_label(mode),
                f"({self._quality_value_summary(mode, state)})",
            ])

        is_modified = bool(
            codec != "copy"
            or bool(VideoResizeSettings.from_value(state.get("resize")).is_active())
            or bool(VideoCropSettings.from_value(state.get("crop")).is_active())
            or bool(VideoFilterSettings.from_value(state.get("filters")).is_active())
            or bool(state.get("inject_hdr_meta"))
            or bool(state.get("tonemap_to_sdr"))
            or bool(state.get("extra_params"))
            or (codec != "copy" and copy_dv)
            or (codec != "copy" and copy_hdr10plus)
            or (copy_dv and str(state.get("dovi_profile") or "0").strip() == "2")
        )
        return VideoTrackEncodePlan(
            track_entry_id=entry_id,
            codec_summary=summary,
            target_codec=codec,
            hdr_badges=self._video_hdr_badges_from_state(state, source_video=source_video),
            filter_badges=self._video_filter_badges_from_state(state),
            is_modified=is_modified,
        )

    def _emit_video_encoding_plans(self) -> None:
        plans: list[VideoTrackEncodePlan] = []
        next_force_8bit: dict[str, bool] = {}
        for index, (info, track, _color) in enumerate(self._video_tracks, start=1):
            entry_id = self._video_entry_id(track)
            state = self._video_settings_by_entry_id.get(entry_id)
            if state is None:
                continue
            target_codec = self._video_state_target_codec(state)
            force_8bit = self._video_force_8bit_for_codec(info, track, target_codec)
            previous_force_8bit = self._video_force_8bit_by_entry_id.get(entry_id)
            if force_8bit != previous_force_8bit:
                if force_8bit:
                    self.log_message.emit(
                        "WARN",
                        translate_text(
                            "Précheck piste vidéo #{index} : source {depth}-bit + codec {codec} → bascule auto en 8-bit.",
                            index=index,
                            depth=self._video_source_bit_depth(info, track),
                            codec=target_codec.upper(),
                        ),
                    )
                elif previous_force_8bit:
                    self.log_message.emit(
                        "INFO",
                        translate_text(
                            "Précheck piste vidéo #{index} : retour au mode source (8-bit auto désactivé).",
                            index=index,
                        ),
                    )
            next_force_8bit[entry_id] = force_8bit
            plans.append(
                self._video_plan_from_state(
                    entry_id=entry_id,
                    state=state,
                    source_video=self._video_track_for_entry(info, track),
                )
            )
        self._video_force_8bit_by_entry_id = next_force_8bit
        self._refresh_video_source_rows()
        if hasattr(self, "_codec_combo"):
            self._update_ten_bit_control(self._codec_combo.currentData() or "libx265")
        self.video_tracks_encoding_changed.emit(plans)

    def _save_current_video_state(self) -> None:
        if self._loading_video_settings or self._current_video_entry_id is None:
            return
        if not hasattr(self, "_codec_combo") or not hasattr(self, "_copy_dv_cb"):
            return
        state = self._current_video_state()
        state, analysis_cancelled = self._normalize_pending_static_hdr_analysis(
            self._current_video_entry_id,
            state,
        )
        self._video_settings_by_entry_id[self._current_video_entry_id] = state
        if self._video_apply_all:
            for entry_id in self._active_video_entry_ids():
                self._video_settings_by_entry_id[entry_id] = self._copy_video_state(state)
        if analysis_cancelled:
            self.log_message.emit(
                "INFO",
                (
                    "Analyse HDR10 estimée déprogrammée : les options P5→P8.1 "
                    "ont changé ou des valeurs HDR10 manuelles ont été saisies."
                ),
            )
        self._emit_video_encoding_plans()

    def _apply_video_state(self, state: dict[str, object]) -> None:
        self._loading_video_settings = True
        try:
            self._set_combo_data(self._codec_combo, state.get("codec"))
            self._set_combo_data(self._mode_combo, state.get("quality_mode"))
            self._set_combo_data(self._preset_combo, state.get("preset"))
            self._crf_spin.setValue(self._state_int(state, "crf", self._crf_spin.value()))
            self._cq_spin.setValue(self._state_int(state, "cq", self._cq_spin.value()))
            self._bitrate_edit.setText(str(state.get("bitrate_kbps") or "5000"))
            self._size_edit.setText(str(state.get("target_size_mb") or "4000"))
            self._extra_params.setText(str(state.get("extra_params") or ""))
            if hasattr(self, "_ten_bit_cb"):
                self._ten_bit_cb.setChecked(bool(state.get("force_10bit")))
            self._apply_resize_settings(VideoResizeSettings.from_value(state.get("resize")))
            self._apply_crop_settings(VideoCropSettings.from_value(state.get("crop")))
            self._apply_filter_settings(VideoFilterSettings.from_value(state.get("filters")))
            self._inject_hdr_cb.setChecked(bool(state.get("inject_hdr_meta")))
            target_codec = str(state.get("codec") or "copy").strip().lower()
            md_state, cll_state = self._effective_static_hdr_fields(
                state,
                codec=target_codec,
            )
            self._master_display.setText(md_state)
            self._max_cll.setText(cll_state)
            self._copy_dv_cb.setChecked(bool(state.get("copy_dv")))
            self._copy_hdr10plus_cb.setChecked(bool(state.get("copy_hdr10plus")))
            self._set_combo_data(self._dovi_profile_combo, state.get("dovi_profile"))
            self._tonemap_cb.setChecked(bool(state.get("tonemap_to_sdr")))
            self._set_combo_data(self._tonemap_algo, state.get("tonemap_algorithm"))
        finally:
            self._loading_video_settings = False

        self._video_encode_controls.setVisible((self._codec_combo.currentData() or "libx265") != "copy")
        self._hdr_meta_widget.setVisible(self._inject_hdr_cb.isChecked())
        self._dovi_profile_widget.setVisible(self._copy_dv_cb.isChecked())
        self._tonemap_algo_widget.setVisible(self._tonemap_cb.isChecked())
        self._update_ten_bit_control(self._codec_combo.currentData() or "libx265")
        self._sync_hdr_metadata_field_editability(self._codec_combo.currentData() or "libx265")
        self._sync_transform_controls_enabled()

    def _apply_resize_settings(self, resize: VideoResizeSettings) -> None:
        if not hasattr(self, "_resize_enabled_cb"):
            return
        self._resize_enabled_cb.setChecked(bool(resize.enabled))
        self._set_combo_data(self._resize_mode_combo, resize.mode)
        self._set_combo_data(self._resize_preset_combo, resize.preset)
        self._resize_percent_spin.setValue(int(resize.percent or 100))
        self._resize_width_spin.setValue(int(resize.width or 1280))
        self._resize_height_spin.setValue(int(resize.height or 720))
        self._resize_keep_aspect_cb.setChecked(bool(resize.keep_aspect))
        self._resize_allow_upscale_cb.setChecked(bool(resize.allow_upscale))
        self._set_combo_data(self._resize_algo_combo, resize.algorithm)
        if str(resize.mode or "preset") == "preset":
            width, height = self._resize_preset_dimensions(resize.preset)
            self._resize_width_spin.setValue(width)
            self._resize_height_spin.setValue(height)
        self._sync_resize_mode_ui()

    def _apply_crop_settings(self, crop: VideoCropSettings) -> None:
        if not hasattr(self, "_crop_enabled_cb"):
            return
        self._crop_enabled_cb.setChecked(bool(crop.enabled))
        self._set_combo_data(self._crop_unit_combo, crop.unit)
        self._crop_top_spin.setValue(int(crop.top or 0))
        self._crop_bottom_spin.setValue(int(crop.bottom or 0))
        self._crop_left_spin.setValue(int(crop.left or 0))
        self._crop_right_spin.setValue(int(crop.right or 0))
        self._crop_auto_cb.setChecked(bool(crop.auto))

    def _apply_filter_settings(self, filters: VideoFilterSettings) -> None:
        if not hasattr(self, "_yadif_cb"):
            return
        self._yadif_cb.setChecked(bool(filters.yadif_enabled))
        self._set_combo_data(self._yadif_mode_combo, filters.yadif_mode)
        self._set_combo_data(self._yadif_parity_combo, filters.yadif_parity)
        self._deblock_cb.setChecked(bool(filters.deblock_enabled))
        self._set_combo_data(self._deblock_strength_combo, filters.deblock_strength)
        self._set_combo_data(self._deblock_block_combo, str(filters.deblock_block))
        self._nlmeans_cb.setChecked(bool(filters.nlmeans_enabled))
        self._set_combo_data(self._nlmeans_strength_combo, filters.nlmeans_strength)
        self._set_combo_data(self._nlmeans_profile_combo, filters.nlmeans_profile)
        self._chroma_cb.setChecked(bool(filters.chroma_smooth_enabled))
        self._set_combo_data(self._chroma_strength_combo, filters.chroma_smooth_strength)

    def _current_video_settings(self) -> VideoEncodeSettings:
        video_source = self._file_info.path if self._file_info is not None else None
        stream_index = 0
        track_entry_id = self._current_video_entry_id
        selected_file_info: FileInfo | None = None
        selected_track: TrackEntry | None = None
        row = self._video_list.currentRow() if hasattr(self, "_video_list") else -1
        if 0 <= row < len(self._video_tracks):
            file_info, track, _color = self._video_tracks[row]
            video_source = file_info.path
            stream_index = int(track.mkv_tid)
            track_entry_id = self._video_entry_id(track)
            selected_file_info = file_info
            selected_track = track
        codec = self._codec_combo.currentData() or "libx265"
        mode  = self._mode_combo.currentData() or QualityMode.CRF
        if not isinstance(mode, QualityMode):
            mode = QualityMode(str(mode))
        preset = self._preset_combo.currentData() or "slow"
        try:
            bitrate = int(self._bitrate_edit.text())
        except ValueError:
            bitrate = 5000
        try:
            size = int(self._size_edit.text())
        except ValueError:
            size = 4000
        force_8bit = bool(
            selected_file_info is not None
            and selected_track is not None
            and self._video_force_8bit_for_codec(selected_file_info, selected_track, str(codec))
        )
        force_10bit = self._effective_force_10bit(
            info=selected_file_info,
            track=selected_track,
            codec=str(codec),
            state_force_10bit=bool(self._ten_bit_cb.isChecked()) if hasattr(self, "_ten_bit_cb") else False,
        )
        source_hdr = (
            self._hdr_type_for_entry(selected_file_info, selected_track)
            if selected_file_info is not None and selected_track is not None
            else HDRType.NONE
        )
        current_state = self._current_video_state()
        copy_dv, copy_hdr10plus = self._effective_dynamic_hdr_flags(
            current_state,
            source_hdr=source_hdr,
            target_codec=str(codec),
        )
        master_display, max_cll = self._effective_static_hdr_fields(
            current_state,
            codec=str(codec),
        )
        return VideoEncodeSettings(
            stream_index=stream_index,
            source_path=video_source,
            track_entry_id=track_entry_id,
            codec=codec,
            quality_mode=mode,
            crf=self._crf_slider.value(),
            cq=self._cq_slider.value(),
            bitrate_kbps=bitrate,
            target_size_mb=size,
            preset=preset,
            extra_params=self._extra_params.text().strip(),
            force_8bit=force_8bit,
            force_10bit=force_10bit,
            resize=self._current_resize_settings(),
            crop=self._current_crop_settings(),
            filters=self._current_filter_settings(),
            inject_hdr_meta=self._inject_hdr_cb.isChecked(),
            master_display=master_display,
            max_cll=max_cll,
            static_hdr_metadata_source=str(current_state.get("static_hdr_metadata_source") or ""),
            static_hdr_metadata_confidence=str(current_state.get("static_hdr_metadata_confidence") or ""),
            static_hdr_metadata_analysis_mode=str(current_state.get("static_hdr_metadata_analysis_mode") or ""),
            static_hdr_metadata_analysis_request=str(current_state.get("static_hdr_metadata_analysis_request") or ""),
            copy_dv=copy_dv,
            copy_hdr10plus=copy_hdr10plus,
            dovi_profile=self._dovi_profile_combo.currentData() or "0",
            tonemap_to_sdr=self._tonemap_cb.isChecked(),
            tonemap_algorithm=self._tonemap_algo.currentData() or "hable",
        )

    def _video_settings_from_state(
        self,
        *,
        file_info: FileInfo,
        track: TrackEntry,
        state: dict[str, object],
    ) -> VideoEncodeSettings:
        mode = state.get("quality_mode") or QualityMode.CRF
        if not isinstance(mode, QualityMode):
            mode = QualityMode(str(mode))
        bitrate = self._state_int(state, "bitrate_kbps", 5000)
        size = self._state_int(state, "target_size_mb", 4000)
        codec = str(state.get("codec") or "libx265")
        source_hdr = self._hdr_type_for_entry(file_info, track)
        copy_dv, copy_hdr10plus = self._effective_dynamic_hdr_flags(
            state,
            source_hdr=source_hdr,
            target_codec=codec,
        )
        master_display, max_cll = self._effective_static_hdr_fields(
            state,
            codec=codec,
        )
        return VideoEncodeSettings(
            stream_index=int(track.mkv_tid),
            source_path=file_info.path,
            track_entry_id=self._video_entry_id(track),
            codec=codec,
            quality_mode=mode,
            crf=self._state_int(state, "crf", 18),
            cq=self._state_int(state, "cq", 26),
            bitrate_kbps=bitrate,
            target_size_mb=size,
            preset=str(state.get("preset") or "slow"),
            extra_params=str(state.get("extra_params") or "").strip(),
            force_8bit=self._video_force_8bit_for_codec(file_info, track, codec),
            force_10bit=self._effective_force_10bit(
                info=file_info,
                track=track,
                codec=codec,
                state_force_10bit=bool(state.get("force_10bit")),
            ),
            resize=VideoResizeSettings.from_value(state.get("resize")),
            crop=VideoCropSettings.from_value(state.get("crop")),
            filters=VideoFilterSettings.from_value(state.get("filters")),
            inject_hdr_meta=bool(state.get("inject_hdr_meta")),
            master_display=master_display,
            max_cll=max_cll,
            static_hdr_metadata_source=str(state.get("static_hdr_metadata_source") or ""),
            static_hdr_metadata_confidence=str(state.get("static_hdr_metadata_confidence") or ""),
            static_hdr_metadata_analysis_mode=str(state.get("static_hdr_metadata_analysis_mode") or ""),
            static_hdr_metadata_analysis_request=str(state.get("static_hdr_metadata_analysis_request") or ""),
            copy_dv=copy_dv,
            copy_hdr10plus=copy_hdr10plus,
            dovi_profile=str(state.get("dovi_profile") or "0"),
            tonemap_to_sdr=bool(state.get("tonemap_to_sdr")),
            tonemap_algorithm=str(state.get("tonemap_algorithm") or "hable"),
        )

    @staticmethod
    def _state_int(state: dict[str, object], key: str, default: int) -> int:
        value = state.get(key, default)
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                return default
        return default

    def _current_video_settings_list(self) -> list[VideoEncodeSettings]:
        self._save_current_video_state()
        settings: list[VideoEncodeSettings] = []
        template_state = (
            self._video_settings_by_entry_id.get(self._current_video_entry_id)
            if self._current_video_entry_id is not None
            else None
        )
        if template_state is None and self._video_settings_by_entry_id:
            template_state = next(iter(self._video_settings_by_entry_id.values()))
        if template_state is None and hasattr(self, "_codec_combo") and hasattr(self, "_copy_dv_cb"):
            template_state = self._current_video_state()

        for file_info, track, _color in self._video_tracks:
            entry_id = self._video_entry_id(track)
            state = self._video_settings_by_entry_id.get(entry_id)
            if state is None:
                if entry_id == self._current_video_entry_id:
                    settings.append(self._current_video_settings())
                    continue
                if self._should_propagate_global_state(template_state):
                    state = self._normalized_video_state_for_track(
                        info=file_info,
                        track=track,
                        state=template_state or {},
                    )
                else:
                    state = self._default_video_state_for_track(
                        info=file_info,
                        track=track,
                    )
                self._video_settings_by_entry_id[entry_id] = self._copy_video_state(state)
            settings.append(
                self._video_settings_from_state(
                    file_info=file_info,
                    track=track,
                    state=state,
                )
            )
        return settings

    def set_output_provider(self, provider: Callable[[], "Path | None"]) -> None:
        """
        Fournit un callable qui retourne le chemin de sortie courant (depuis RemuxPanel).
        Appelé par MainWindow après création des panneaux.
        """
        self._output_provider = provider

    def set_file_title_provider(self, provider: Callable[[], str]) -> None:
        """
        Fournit un callable qui retourne le titre de fichier courant (depuis RemuxPanel).
        Appelé par MainWindow après création des panneaux.
        """
        self._file_title_provider = provider

    def set_extra_attachments_provider(self, provider: Callable[[], list]) -> None:
        """
        Fournit un callable qui retourne les pièces jointes manuelles (depuis RemuxPanel).
        Appelé par MainWindow après création des panneaux.
        """
        self._extra_attachments_provider = provider

    def set_tmdb_cover_provider(self, provider: "Callable[[], tuple[str, str] | None]") -> None:
        """
        Fournit un callable qui retourne (url, filename) de la cover TMDB en attente,
        ou None si aucune. Appelé par MainWindow après création des panneaux.
        """
        self._tmdb_cover_provider = provider

    def set_tag_overrides_provider(self, provider: "Callable[[], dict | None]") -> None:
        """
        Fournit un callable qui retourne les balises MKV éditées (depuis RemuxPanel).
        Appelé par MainWindow après création des panneaux.
        """
        self._tag_overrides_provider = provider

    def set_chapters_provider(self, provider: "Callable[[], list | None]") -> None:
        """
        Fournit un callable qui retourne les chapter_overrides (depuis RemuxPanel).
        Appelé par MainWindow après création des panneaux.
        """
        self._chapters_provider = provider

    def set_mux_backend_provider(self, provider: Callable[[], str]) -> None:
        """Lie la preview encode au choix courant du panneau Remux."""
        self._mux_backend_provider = provider
        self._rebuild_preview()

    def refresh_command_preview(self) -> None:
        """Reconstruit publiquement la preview après un changement partagé."""
        self._rebuild_preview()

    def _current_config(self) -> EncodeConfig | None:
        if self._file_info is None:
            return None
        output = self._output_provider()
        if output is None:
            return None
        video_tracks = self._current_video_settings_list()
        if not video_tracks:
            return None
        primary_source = video_tracks[0].source_path or self._file_info.path
        return EncodeConfig(
            source=primary_source,
            output=output,
            video=video_tracks[0],
            video_tracks=video_tracks,
            audio_tracks=self._audio_table.current_audio_settings(),
            copy_subtitles=True,
            duration_s=self._duration_s,
            copy_dv=video_tracks[0].copy_dv,
            copy_hdr10plus=video_tracks[0].copy_hdr10plus,
            dovi_profile=video_tracks[0].dovi_profile,
            work_dir=self._config.work_dir,
            file_title=self._file_title_provider(),
            extra_attachments=self._extra_attachments_provider(),
            tmdb_cover=self._tmdb_cover_provider(),
            tag_overrides=self._tag_overrides_provider(),
            chapter_overrides=self._chapters_provider(),
            mux_backend=str(
                self._mux_backend_provider() or self._config.matroska_mux_backend
            ),
        )

    def _on_add_audio_track(self) -> None:
        """Ouvre le popup de sélection pour ajouter une piste audio custom."""
        source_tracks = [t for t in self._audio_tracks_data if self._is_original_audio_source(t)]
        if not source_tracks:
            return
        dlg = _AudioSourceDialog(source_tracks, config=self._config, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        track = dlg.selected_track()
        if track is None:
            return
        template_entry = dlg.selected_track_entry()
        if template_entry is None:
            return
        new_entry_id = uuid4().hex
        self._audio_table.add_custom_row(
            track,
            dlg.selected_color(),
            dlg.selected_codec(),
            dlg.selected_bitrate(),
            dlg.selected_source_path(),
            track_entry_id=new_entry_id,
        )
        self.audio_track_add_requested.emit(
            template_entry,
            new_entry_id,
            dlg.selected_codec(),
            dlg.selected_bitrate(),
        )
        self.log_message.emit(
            "INFO",
            translate_text(
                "Piste audio ajoutée : #{index} {codec} {channels} → {target}",
                index=track.index,
                codec=track.codec.upper(),
                channels=track.channels_label,
                target=dlg.selected_codec(),
            ),
        )

    @staticmethod
    def _is_original_audio_source(track_tuple: tuple) -> bool:
        entry = track_tuple[3] if len(track_tuple) > 3 else None
        return not bool(getattr(entry, "is_new", False))

    def refresh_runtime_settings(self) -> None:
        self._audio_table.refresh_runtime_settings()
        self._workflow.set_ffmpeg_threads(self._config.ffmpeg_threads)
        self._workflow.set_max_parallel_video_encodes(self._config.max_parallel_video_encodes)
        self._workflow.set_mediainfo_bin(self._config.tool_mediainfo)
        self._workflow.set_generate_nfo(self._config.generate_nfo)
        self._workflow.set_regenerate_statistics(getattr(self._config, "matroska_regenerate_statistics", True))
        self._workflow.set_sync_rewrite_enabled(self._config.sync_rewrite_enabled)
        self._workflow.set_sync_rewrite_audio_bitrates(
            aac_bitrate_per_channel_kbps=self._config.aac_bitrate_per_channel_kbps,
            eac3_bitrate_per_channel_kbps=self._config.eac3_bitrate_per_channel_kbps,
        )
        self._rebuild_preview()

    def _copy_command(self) -> None:
        from PySide6.QtWidgets import QApplication
        text = self._cmd_preview.toPlainText()
        if text:
            QApplication.clipboard().setText(text)

    def closeEvent(self, event) -> None:
        self._closing = True
        if self._preview_signals is not None:
            self._preview_signals.cancel()
        self._hdr_meta_executor.shutdown(wait=False, cancel_futures=True)
        self._executor.shutdown(wait=True)
        try:
            EncodeWorkflow.cleanup_preview_dir(self._config.work_dir)
        except Exception:
            pass
        super().closeEvent(event)
