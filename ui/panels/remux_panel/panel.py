"""Panneau principal RemuxPanel."""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QDropEvent, QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.config import AppConfig
from core.bluray import discover_titles, find_disc_root
from core.extractor import TrackExtractor
from core.file_types import is_accepted
from core.i18n import apply_translations, translate_text
from core.matroska.reader import MatroskaReader
from core.inspector import AttachmentInfo, ChapterEntry, FileInfo
from core.profiles.decision import DecisionProfileManager, apply_decision_profile as apply_decision_profile_v1
from core.profiles.selectors import remux_config_to_exact_job
from core.runner import TaskSignals, ToolRunner
from core.workflows.remux import RemuxWorkflow
from core.workflows.audio_sync import AudioSyncTrack, AudioSyncWorkflow
from core.workflows.common.ffmpeg_runtime import ffmpeg_progress_args
from core.workflows.common.sync_rewrite import (
    SYNC_REWRITE_MODE_OFFSET,
    normalized_sync_rewrite_mode,
    ui_sync_rewrite_can_toggle,
)
from core.workflows.remux_models import (
    RemuxConfig,
    SourceInput,
    TrackEntry,
    clone_track_entry,
)
from ui.panels.remux_panel.functions import chapters as chapter_functions
from ui.panels.remux_panel.functions import config_builder, inspection, signals, tmdb
from ui.panels.remux_panel.models import SourceFile
from ui.panels.remux_panel.theme import (
    _C,
    _card,
    _input_style,
    _secondary_button,
    _section_label,
    _separator,
    _x_icon,
)
from ui.design_system import font_px as _font_px, scale as _scale
from ui.panels.remux_panel.widgets.attachments import _AttachmentPanel
from ui.panels.remux_panel.widgets.chapters import _ChapterPanel
from ui.panels.remux_panel.widgets.file_list import _FileListWidget
from ui.panels.remux_panel.widgets.track_table import _TrackTable
from ui.panels.remux_panel.profile_editor import DecisionProfileEditorDialog


class _AudioSyncReferenceDialog(QDialog):
    def __init__(
        self,
        choices: list[tuple[str, TrackEntry]],
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(translate_text("Source de référence"))
        self.setModal(True)
        self._combo = QComboBox()
        for choice_label, entry in choices:
            self._combo.addItem(choice_label, entry)

        root = QVBoxLayout(self)
        root.setContentsMargins(_scale(18), _scale(16), _scale(18), _scale(16))
        root.setSpacing(_scale(12))
        self.setStyleSheet(f"""
            QDialog {{ background: {_C.BG_DEEP}; color: {_C.TEXT_PRI}; }}
            QLabel {{ color: {_C.TEXT_SEC}; background: transparent; }}
            QComboBox {{
                background: {_C.BG_CARD};
                color: {_C.TEXT_PRI};
                border: 1px solid {_C.BORDER};
                border-radius: 5px;
                padding: {_scale(6)}px {_scale(8)}px;
                min-width: {_scale(420)}px;
            }}
        """)

        prompt_label = QLabel(translate_text("Choisir la source audio qui servira de référence."))
        root.addWidget(prompt_label)
        root.addWidget(self._combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        root.addWidget(buttons)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

    def selected_entry(self) -> TrackEntry | None:
        data = self._combo.currentData()
        return data if isinstance(data, TrackEntry) else None


class RemuxPanel(QWidget):
    """
    Panneau de remuxage MKV/MP4 — support multi-sources.

    Signaux :
        log_message(level: str, message: str)
        tool_output(label: str, line: str)
        extract_started(TaskSignals, dict) — extraction de piste lancée depuis le menu contextuel
        audio_sync_started(dict) — synchronisation audio lancée depuis le tableau des pistes
        audio_sync_finished(bool, dict) — synchronisation audio terminée
        video_tracks_changed(list)  — pistes vidéo activées (FileInfo, TrackEntry, couleur)
        audio_tracks_changed(list)  — pistes audio activées (AudioTrack, couleur, Path source)
        ready_changed(bool)         — True quand au moins un fichier est inspecté
    """

    log_message = Signal(str, str)
    tool_output = Signal(str, str)
    extract_started = Signal(object, object)
    audio_sync_started = Signal(object)
    audio_sync_finished = Signal(bool, object)

    _inspection_done = Signal(str, object)
    _inspection_error = Signal(str, str)
    _audio_sync_done = Signal(str, str, int, float)
    _audio_sync_error = Signal(str, str)
    _preview_compiled = Signal(int, str)

    video_tracks_changed = Signal(object)
    audio_tracks_changed = Signal(object)
    ready_changed = Signal(bool)
    mux_backend_changed = Signal(str)

    def __init__(
        self,
        config: AppConfig,
        parent: QWidget | None = None,
        *,
        writing_application: str = "",
    ) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._config = config
        self._writing_application = writing_application
        self._workflow: RemuxWorkflow = self._make_workflow()
        self._executor = ThreadPoolExecutor(max_workers=2)
        # Le préflight du backend natif peut lire les sources ; il ne doit pas
        # partager le pool d'inspection ni bloquer la boucle Qt.
        self._preview_executor = ThreadPoolExecutor(max_workers=1)

        self._source_files: list[SourceFile] = []
        self._source_names: dict[str, str] = {}
        self._source_colors: dict[str, str] = {}
        self._source_sync_offsets_ms: dict[str, int] = {}
        self._auto_sync_entry_ids: set[str] = set()
        self._chapter_sync_cancelled_source_ids: set[str] = set()
        self._color_index: int = 0
        self._file_list: _FileListWidget
        self._track_table: _TrackTable
        self._file_title_edit: QLineEdit
        self._attachment_panel: _AttachmentPanel
        self._chapter_panel: _ChapterPanel
        self._output_edit: QLineEdit
        self._mux_backend_combo: QComboBox
        self._cmd_preview: QPlainTextEdit
        self._preview_generation = 0
        self._preview_dirty = False
        self._closing = False
        self._preview_future: Future[None] | None = None

        # Debounce du preview : les cases à cocher émettent itemChanged par
        # cellule et le drag-reorder plusieurs order_changed ; sans coalescence,
        # chaque événement relancerait une compilation de plan et des I/O.
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(180)
        self._preview_timer.timeout.connect(self._apply_rebuild_preview)
        self._preview_compiled.connect(
            self._apply_compiled_preview,
            Qt.ConnectionType.QueuedConnection,
        )

        self._workflow.log_message.connect(
            self.log_message, Qt.ConnectionType.QueuedConnection
        )
        self._inspection_done.connect(
            self._apply_inspection, Qt.ConnectionType.QueuedConnection
        )
        self._inspection_error.connect(
            self._on_inspection_error, Qt.ConnectionType.QueuedConnection
        )
        self._audio_sync_done.connect(
            self._on_audio_sync_done, Qt.ConnectionType.QueuedConnection
        )
        self._audio_sync_error.connect(
            self._on_audio_sync_error, Qt.ConnectionType.QueuedConnection
        )

        self._build_ui()
        self._track_table.set_sync_rewrite_enabled(
            self._config.sync_rewrite_enabled,
            advanced_audio_enabled=self._config.sync_advanced_audio_rewrite_enabled,
        )
        apply_translations(self)

    def _make_workflow(self) -> RemuxWorkflow:
        return RemuxWorkflow(
            ffmpeg_bin=self._config.tool_ffmpeg,
            ffprobe_bin=self._config.tool_ffprobe,
            ffmpeg_threads=self._config.ffmpeg_threads,
            writing_application=self._writing_application,
            generate_nfo=self._config.generate_nfo,
            mediainfo_bin=self._config.tool_mediainfo,
            sync_rewrite_enabled=self._config.sync_rewrite_enabled,
            sync_advanced_audio_rewrite_enabled=self._config.sync_advanced_audio_rewrite_enabled,
            aac_bitrate_per_channel_kbps=self._config.aac_bitrate_per_channel_kbps,
            eac3_bitrate_per_channel_kbps=self._config.eac3_bitrate_per_channel_kbps,
        )

    def _recreate_workflow(self) -> None:
        try:
            self._workflow.log_message.disconnect(self.log_message)
        except Exception:
            pass
        self._workflow = self._make_workflow()
        self._workflow.log_message.connect(
            self.log_message, Qt.ConnectionType.QueuedConnection
        )

    def _build_ui(self) -> None:
        self.setStyleSheet(f"background: {_C.BG_DEEP};")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(f"""
            QScrollArea {{ background: {_C.BG_DEEP}; border: none; }}
            QScrollBar:vertical {{
                background: {_C.BG_DEEP};
                width: {_scale(6)}px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {_C.BORDER_LT};
                border-radius: {_scale(3)}px;
                min-height: {_scale(24)}px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        """)

        content = QWidget()
        content.setStyleSheet(f"background: {_C.BG_DEEP};")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(_scale(28), _scale(24), _scale(28), _scale(24))
        content_layout.setSpacing(_scale(20))
        content_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)

        title = QLabel("Manipulation Conteneur")
        title.setStyleSheet(f"""
            font-size: {_font_px(20)}px;
            font-weight: 800;
            color: {_C.TEXT_PRI};
            background: transparent;
            letter-spacing: -{_scale(1)}px;
        """)
        subtitle = QLabel("Remuxage, fusion et sélection de pistes (vidéo/audio/sous-titres externes) — sans réencodage")
        subtitle.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(12)}px; background: transparent;")
        content_layout.addWidget(title)
        content_layout.addWidget(subtitle)
        content_layout.addWidget(_separator())

        content_layout.addWidget(_section_label("FICHIERS SOURCES"))
        self._file_list = _FileListWidget()
        self._file_list.add_requested.connect(self._route_dropped_paths)
        self._file_list.remove_requested.connect(self._on_remove_file)
        content_layout.addWidget(self._file_list)

        content_layout.addWidget(_separator())

        track_header = QHBoxLayout()
        track_header.setSpacing(_scale(8))
        track_header.addWidget(_section_label("PISTES"))
        track_header.addStretch()

        btn_all = _secondary_button("Tout activer")
        btn_none = _secondary_button("Tout désactiver")
        btn_all.clicked.connect(lambda: self._set_all_tracks(True))
        btn_none.clicked.connect(lambda: self._set_all_tracks(False))
        track_header.addWidget(btn_all)
        track_header.addWidget(btn_none)

        export_profile_btn = _secondary_button("Exporter JSON CLI")
        export_profile_btn.clicked.connect(self._export_exact_json)
        save_profile_btn = _secondary_button("Éditer profil")
        save_profile_btn.clicked.connect(self._save_decision_profile)
        apply_profile_btn = _secondary_button("Appliquer profil")
        apply_profile_btn.clicked.connect(self._apply_decision_profile_dialog)
        track_header.addWidget(export_profile_btn)
        track_header.addWidget(save_profile_btn)
        track_header.addWidget(apply_profile_btn)

        self._filter_btn = QPushButton("Sélectionnées seulement")
        self._filter_btn.setCheckable(True)
        self._filter_btn.setFixedHeight(_scale(28))
        self._filter_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._filter_btn.setStyleSheet(f"""
            QPushButton {{
                background: {_C.BG_CARD};
                color: {_C.TEXT_SEC};
                border: 1px solid {_C.BORDER};
                border-radius: {_scale(5)}px;
                font-size: {_font_px(11)}px;
                font-weight: 500;
                padding: 0 {_scale(12)}px;
            }}
            QPushButton:hover {{
                background: {_C.BG_HOVER};
                color: {_C.TEXT_PRI};
                border-color: {_C.BORDER_LT};
            }}
            QPushButton:checked {{
                background: {_C.ACCENT_DIM};
                color: {_C.ACCENT};
                border-color: {_C.ACCENT};
            }}
        """)
        self._filter_btn.toggled.connect(
            lambda checked: self._track_table.set_filter_selected(checked)
        )
        track_header.addWidget(self._filter_btn)
        content_layout.addLayout(track_header)

        hint_row = QHBoxLayout()
        hint_row.setSpacing(_scale(8))
        hint = QLabel("Glisser-déposer les lignes pour réordonner · Double-clic pour éditer Langue / Titre")
        hint.setStyleSheet(f"color: {_C.TEXT_DIM}; font-size: {_font_px(10)}px; background: transparent;")
        hint_row.addWidget(hint)
        hint_row.addStretch()
        self._global_sync_cancel_btn = _secondary_button(translate_text("Annuler toutes les synchros"))
        self._global_sync_cancel_btn.setIcon(_x_icon(_C.ERROR, 12))
        self._global_sync_cancel_btn.setIconSize(QSize(_scale(12), _scale(12)))
        self._global_sync_cancel_btn.clicked.connect(self._on_global_sync_cancel_requested)
        self._global_sync_cancel_btn.setVisible(False)
        hint_row.addWidget(self._global_sync_cancel_btn)
        content_layout.addLayout(hint_row)

        self._track_table = _TrackTable()
        self._track_table.itemChanged.connect(self._on_table_changed)
        self._track_table.order_changed.connect(self._on_track_order_changed)
        self._track_table.extract_requested.connect(self._on_extract_track)
        self._track_table.audio_sync_requested.connect(self._on_audio_sync_requested)
        self._track_table.auto_sync_cancel_requested.connect(self._on_auto_sync_cancel_requested)
        self._track_table.sync_rewrite_toggle_requested.connect(self._on_sync_rewrite_toggle_requested)
        content_layout.addWidget(self._track_table)

        content_layout.addWidget(_separator())

        title_card = _card()
        title_card_layout = QVBoxLayout(title_card)
        title_card_layout.setContentsMargins(_scale(16), _scale(10), _scale(16), _scale(10))
        title_card_layout.setSpacing(_scale(6))
        title_card_layout.addWidget(_section_label("TITRE DU FICHIER"))

        self._file_title_edit = QLineEdit()
        self._file_title_edit.setPlaceholderText("Titre du conteneur MKV (balise Title)")
        self._file_title_edit.setStyleSheet(_input_style())
        self._file_title_edit.textChanged.connect(self._rebuild_preview)
        self._file_title_edit.textChanged.connect(self._sync_tmdb_suggested_title)
        title_card_layout.addWidget(self._file_title_edit)

        content_layout.addWidget(title_card)

        self._attachment_panel = _AttachmentPanel(self._config)
        self._attachment_panel.set_embedded_attachment_loader(
            self._extract_embedded_attachment_bytes
        )
        self._attachment_panel.changed.connect(self._rebuild_preview)
        self._attachment_panel.tmdb_details_selected.connect(self._on_tmdb_details_selected)
        content_layout.addWidget(self._attachment_panel)
        self._sync_tmdb_suggested_title()

        content_layout.addWidget(_separator())

        self._chapter_panel = _ChapterPanel()
        self._chapter_panel.changed.connect(self._on_chapters_changed)
        self._chapter_panel.sync_cancel_requested.connect(self._on_chapter_sync_cancel_requested)
        content_layout.addWidget(self._chapter_panel)

        content_layout.addWidget(_separator())

        content_layout.addWidget(_section_label("FICHIER DE SORTIE"))
        backend_row = QHBoxLayout()
        backend_row.setSpacing(_scale(8))
        backend_label = QLabel(translate_text("Backend de muxage"))
        backend_label.setStyleSheet(f"color: {_C.TEXT_SEC}; background: transparent;")
        self._mux_backend_combo = QComboBox()
        for value, label in (
            ("ffmpeg", "FFmpeg"), ("native", "Natif Matroska"), ("auto", "Auto"),
        ):
            self._mux_backend_combo.addItem(translate_text(label), value)
        # Override du job, initialisé avec le réglage global [matroska].
        selected_backend = self._mux_backend_combo.findData(self._config.matroska_mux_backend)
        self._mux_backend_combo.setCurrentIndex(max(0, selected_backend))
        self._mux_backend_combo.setStyleSheet(_input_style())
        self._mux_backend_combo.currentIndexChanged.connect(self._rebuild_preview)
        self._mux_backend_combo.currentIndexChanged.connect(
            lambda _index: self.mux_backend_changed.emit(self.current_mux_backend())
        )
        backend_row.addWidget(backend_label)
        backend_row.addWidget(self._mux_backend_combo)
        backend_row.addStretch()
        content_layout.addLayout(backend_row)
        out_row = QHBoxLayout()
        out_row.setSpacing(_scale(8))

        self._output_edit = QLineEdit()
        self._output_edit.setPlaceholderText("/chemin/vers/sortie.mkv")
        self._output_edit.setStyleSheet(_input_style())
        self._output_edit.textChanged.connect(self._rebuild_preview)
        out_row.addWidget(self._output_edit, stretch=1)

        browse_out = _secondary_button("Choisir…")
        browse_out.clicked.connect(self._browse_output)
        out_row.addWidget(browse_out)

        content_layout.addLayout(out_row)
        content_layout.addWidget(_separator())

        cmd_header = QHBoxLayout()
        cmd_header.addWidget(_section_label("APERÇU COMMANDE"))
        cmd_header.addStretch()
        copy_btn = _secondary_button("Copier")
        copy_btn.clicked.connect(self._copy_command)
        cmd_header.addWidget(copy_btn)
        content_layout.addLayout(cmd_header)

        self._cmd_preview = QPlainTextEdit()
        self._cmd_preview.setReadOnly(True)
        self._cmd_preview.setFixedHeight(_scale(120))
        mono = QFont("JetBrains Mono", _font_px(9))
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._cmd_preview.setFont(mono)
        self._cmd_preview.setStyleSheet(f"""
            QPlainTextEdit {{
                background: {_C.BG_DEEP};
                color: {_C.TEXT_SEC};
                border: 1px solid {_C.BORDER};
                border-radius: {_scale(6)}px;
                padding: {_scale(8)}px {_scale(12)}px;
            }}
        """)
        self._cmd_preview.setPlaceholderText(
            "Ajoutez au moins un fichier source et définissez le chemin de sortie…"
        )
        content_layout.addWidget(self._cmd_preview)

        content_layout.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll, stretch=1)
        self._install_global_drop_targets(scroll, content)

    def _on_add_files(self, paths: list[str]) -> None:
        inspection.on_add_files(self, paths)

    def add_sources(self, paths: Sequence[str | Path]) -> None:
        """Ajoute des fichiers source depuis l'extérieur du panneau."""
        normalized = [str(Path(path)) for path in paths if str(path).strip()]
        if not normalized:
            return
        self._on_add_files(normalized)

    def _install_global_drop_targets(self, scroll: QScrollArea, content: QWidget) -> None:
        for target in (self, scroll, scroll.viewport(), content):
            target.setAcceptDrops(True)
            target.installEventFilter(self)

    def _collect_folder_drop_paths(self, folder: Path) -> tuple[list[str], list[str]]:
        """
        Parcourt récursivement un dossier dropé.

        Seuls les types reconnus par file_types vont en sources.
        Seuls les .jpg sont retenus comme pièces jointes (cover).
        """
        source_paths: list[str] = []
        attachment_paths: list[str] = []

        if find_disc_root(folder) is not None:
            return source_paths, attachment_paths

        for child in sorted(folder.rglob("*")):
            if not child.is_file():
                continue
            child_str = str(child)
            if is_accepted(child_str):
                source_paths.append(child_str)
            elif child.suffix.lower() == ".jpg":
                attachment_paths.append(child_str)

        return source_paths, attachment_paths

    def _select_bluray_playlist(self, folder: Path) -> str | None:
        titles = discover_titles(folder, min_duration_s=60.0)
        if not titles:
            return None
        items: list[str] = []
        by_label: dict[str, Path] = {}
        for title in titles[:50]:
            hours = int(title.duration_s // 3600)
            minutes = int((title.duration_s % 3600) // 60)
            seconds = int(title.duration_s % 60)
            label = (
                f"{title.label}  —  {hours:02d}:{minutes:02d}:{seconds:02d}"
                f"  —  {len(title.segments)} segment(s)"
            )
            items.append(label)
            by_label[label] = title.playlist_path
        if len(items) == 1:
            return str(by_label[items[0]])
        selected, ok = QInputDialog.getItem(
            self,
            translate_text("Sélectionner une playlist Blu-ray"),
            translate_text("Playlist :"),
            items,
            0,
            False,
        )
        if not ok:
            return None
        playlist = by_label.get(selected)
        return str(playlist) if playlist is not None else None

    def _route_dropped_paths(self, paths: list[str]) -> None:
        source_paths: list[str] = []
        attachment_paths: list[str] = []
        seen_sources: set[str] = set()
        seen_attachments: set[str] = set()

        for path_str in paths:
            path = Path(path_str)
            if path.is_dir():
                bluray_playlist = self._select_bluray_playlist(path)
                if bluray_playlist:
                    if bluray_playlist not in seen_sources:
                        source_paths.append(bluray_playlist)
                        seen_sources.add(bluray_playlist)
                    continue
                if find_disc_root(path) is not None:
                    continue
                folder_sources, folder_attachments = self._collect_folder_drop_paths(path)
                for folder_source in folder_sources:
                    if folder_source not in seen_sources:
                        source_paths.append(folder_source)
                        seen_sources.add(folder_source)
                for folder_attachment in folder_attachments:
                    if folder_attachment not in seen_attachments:
                        attachment_paths.append(folder_attachment)
                        seen_attachments.add(folder_attachment)
                continue

            if not path.is_file():
                continue

            normalized_path = str(path)
            if is_accepted(normalized_path):
                if normalized_path not in seen_sources:
                    source_paths.append(normalized_path)
                    seen_sources.add(normalized_path)
            elif normalized_path not in seen_attachments:
                attachment_paths.append(normalized_path)
                seen_attachments.add(normalized_path)

        if source_paths:
            self._on_add_files(source_paths)
        if attachment_paths:
            self._attachment_panel.add_manual_paths(attachment_paths)
            self.log_message.emit(
                "INFO",
                translate_text(
                    "{count} fichier(s) ajouté(s) comme pièce(s) jointe(s).",
                    count=len(attachment_paths),
                ),
            )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        event_type = event.type()
        if event_type in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop):
            mime = getattr(event, "mimeData", lambda: None)()
            urls = mime.urls() if mime is not None and mime.hasUrls() else []
            local_paths = [
                url.toLocalFile()
                for url in urls
                if url.isLocalFile() and Path(url.toLocalFile()).exists()
            ]
            if not local_paths:
                return super().eventFilter(watched, event)

            if event_type == QEvent.Type.Drop:
                self._route_dropped_paths(local_paths)
            if isinstance(event, QDropEvent):
                event.acceptProposedAction()
            return True

        return super().eventFilter(watched, event)

    def _inspect_file(self, file_id: str, path: Path) -> None:
        inspection.inspect_file(self, file_id, path)

    def _extract_embedded_attachment_bytes(
        self,
        file_id: str,
        attachment: AttachmentInfo,
    ) -> bytes | None:
        source = self._find_source(file_id)
        if source is None:
            raise RuntimeError(
                translate_text("Source introuvable pour cet attachement embarqué.")
            )
        try:
            return MatroskaReader(source.path).attachment_data(attachment.local_index)
        except Exception as exc:
            raise RuntimeError(
                translate_text("Impossible d'extraire cet attachement embarqué depuis la source.")
            ) from exc

    def _apply_inspection(self, file_id: str, info: FileInfo) -> None:
        inspection.apply_inspection(self, file_id, info)

    def _on_inspection_error(self, file_id: str, message: str) -> None:
        inspection.on_inspection_error(self, file_id, message)

    def _on_remove_file(self, file_id: str) -> None:
        inspection.on_remove_file(self, file_id)

    def _find_source(self, file_id: str) -> SourceFile | None:
        return inspection.find_source(self, file_id)

    def _has_ready_files(self) -> bool:
        return inspection.has_ready_files(self)

    def _default_tmdb_suggested_title(self) -> str:
        return tmdb.default_tmdb_suggested_title(self)

    def _default_tmdb_season_episode(self) -> tuple[int, int]:
        return tmdb.default_tmdb_season_episode(self)

    def _sync_tmdb_suggested_title(self, _text: str = "") -> None:
        tmdb.sync_tmdb_suggested_title(self, _text)

    def _on_tmdb_details_selected(self, details: object) -> None:
        tmdb.on_tmdb_details_selected(self, details)

    def _rebuild_preview(self) -> None:
        if self._closing:
            return
        # Coalesce les rafales d'événements (cases, reorder) en une seule
        # recompilation via le timer single-shot. Invalider immédiatement le
        # résultat en vol évite d'afficher une commande devenue obsolète.
        self._preview_generation += 1
        self._preview_dirty = True
        if self._preview_future is not None:
            self._preview_future.cancel()
        self._preview_timer.start()

    def _apply_rebuild_preview(self) -> None:
        if self._closing:
            return
        generation = self._preview_generation
        config = self._current_config()
        if config is None:
            self._preview_dirty = False
            self._cmd_preview.setPlainText("")
            return

        # ``preview_command`` ne modifie pas le workflow : il ne fait que
        # compiler le plan et lire éventuellement les conteneurs sources.
        # Le déplacer du thread Qt garantit que les interactions du tableau
        # restent réactives, y compris avec un stockage réseau lent.
        # Les TrackEntry du tableau sont mutables ; le worker reçoit donc un
        # instantané indépendant de l'UI, pas les mêmes objets partagés.
        snapshot = copy.deepcopy(config)
        if self._preview_future is not None:
            self._preview_future.cancel()
        self._preview_future = self._preview_executor.submit(
            self._compile_preview,
            generation,
            snapshot,
        )

    def _compile_preview(self, generation: int, config: RemuxConfig) -> None:
        try:
            text = self._workflow.preview_command(config)
        except Exception:
            text = "(erreur de construction de la commande)"
        self._preview_compiled.emit(generation, text)

    def _apply_compiled_preview(self, generation: int, text: str) -> None:
        if self._closing or generation != self._preview_generation:
            return
        self._preview_dirty = False
        self._cmd_preview.setPlainText(text)

    def _refresh_preview_synchronously(self) -> None:
        """Rafraîchit le preview avant une action explicite qui l'utilise."""
        self._preview_timer.stop()
        self._preview_generation += 1
        config = self._current_config()
        if config is None:
            text = ""
        else:
            try:
                text = self._workflow.preview_command(config)
            except Exception:
                text = "(erreur de construction de la commande)"
        self._preview_dirty = False
        self._cmd_preview.setPlainText(text)

    def _on_table_changed(self, _item: QTableWidgetItem | None = None) -> None:
        self._prune_auto_sync_entry_ids()
        self._refresh_audio_sync_buttons()
        self._track_table.refresh_filter()
        self._rebuild_preview()
        if _item is None:
            # Compatibilité des appels programmatiques qui ne fournissent pas
            # la cellule modifiée : les deux projections restent sûres.
            self._emit_signals()
            return
        check_item = self._track_table.item(_item.row(), _TrackTable.COL_CHECK)
        entry = (
            check_item.data(Qt.ItemDataRole.UserRole)
            if check_item is not None
            else None
        )
        # EncodePanel maintient des listes vidéo et audio indépendantes. Ne
        # reconstruire que celle affectée évite une seconde vague de widgets à
        # chaque clic ; l'ordre global reste synchronisé dans le handler DnD.
        if isinstance(entry, TrackEntry) and entry.track_type == "video":
            self._emit_video_tracks()
        elif isinstance(entry, TrackEntry) and entry.track_type == "audio":
            self._emit_audio_tracks()

    def _on_track_order_changed(self) -> None:
        self._prune_auto_sync_entry_ids()
        self._refresh_audio_sync_buttons()
        self._rebuild_preview()
        self._emit_signals()

    def _emit_signals(self) -> None:
        signals.emit_signals(self)

    def _emit_video_tracks(self) -> None:
        signals.emit_video_tracks(self)

    def _emit_audio_tracks(self) -> None:
        signals.emit_audio_tracks(self)

    def _current_config(self) -> RemuxConfig | None:
        return config_builder.current_config(self)

    def collect_config(self) -> RemuxConfig | None:
        return self._current_config()

    def _decision_profile_manager(self) -> DecisionProfileManager:
        return DecisionProfileManager(self._config.profiles_dir / "decision")

    def _current_exact_job(self, *, name: str = "") -> dict | None:
        config = self._current_config()
        if config is None:
            QMessageBox.warning(
                self,
                translate_text("Exporter JSON CLI"),
                translate_text("Aucune configuration complète à exporter."),
            )
            return None
        return remux_config_to_exact_job(config, name=name)

    def _current_profile_config(self) -> RemuxConfig | None:
        if not self._has_ready_files():
            QMessageBox.warning(
                self,
                translate_text("Profils"),
                translate_text("Aucune source prête pour créer un profil."),
            )
            return None
        all_tracks = self._track_table.current_tracks()
        if not all_tracks:
            QMessageBox.warning(
                self,
                translate_text("Profils"),
                translate_text("Aucune piste disponible pour créer un profil."),
            )
            return None
        id_to_index = {sf.id: index for index, sf in enumerate(self._source_files)}
        sources: list[SourceInput] = []
        for index, source_file in enumerate(self._source_files):
            if source_file.info is None:
                continue
            source_tracks = [track for track in all_tracks if track.file_id == source_file.id]
            if not source_tracks:
                source_tracks = source_file.tracks
            sources.append(
                SourceInput(
                    path=source_file.path,
                    file_index=index,
                    tracks=source_tracks,
                    attachment_count=len(source_file.info.attachments),
                    has_chapters=bool(source_file.info.chapters and source_file.info.chapters.entries),
                )
            )
        if not sources:
            return None
        track_order: list[tuple[int, int] | tuple[int, int, str]] = [
            (id_to_index[track.file_id], track.mkv_tid, track.entry_id)
            for track in all_tracks
            if track.enabled and track.file_id in id_to_index
        ]
        return RemuxConfig(
            sources=sources,
            output=Path("profile-preview.mkv"),
            track_order=track_order,
            keep_chapters=True,
            work_dir=self._config.work_dir,
            mux_backend=str(self._mux_backend_combo.currentData() or self._config.matroska_mux_backend),
        )

    def _export_exact_json(self) -> None:
        job = self._current_exact_job()
        if job is None:
            return
        default = self._output_edit.text().strip()
        if default:
            default_path = Path(default).with_suffix(".exact-job.json")
        elif self._source_files:
            default_path = self._source_files[0].path.with_suffix(".exact-job.json")
        else:
            default_path = self._config.config_dir / "template.exact-job.json"
        path, _ = QFileDialog.getSaveFileName(
            self,
            translate_text("Exporter JSON CLI"),
            str(default_path),
            translate_text("JSON (*.json);;Tous les fichiers (*)"),
        )
        if not path:
            return
        try:
            Path(path).write_text(
                json.dumps(job, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            QMessageBox.warning(
                self,
                translate_text("Export impossible"),
                translate_text("Impossible d'écrire le fichier JSON : {err}", err=str(exc)),
            )
            return
        self.log_message.emit("OK", translate_text("Template CLI exporté : {path}", path=path))

    def _save_decision_profile(self) -> None:
        config = self._current_profile_config()
        current_tracks = self._track_table.current_tracks()
        source_index_by_file_id = {sf.id: index for index, sf in enumerate(self._source_files)}
        dialog = DecisionProfileEditorDialog(
            manager=self._decision_profile_manager(),
            current_config=config,
            current_tracks=current_tracks,
            source_index_by_file_id=source_index_by_file_id,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.log_message.emit("OK", translate_text("Profil sauvegardé : {name}", name=dialog.profile().get("name", "")))

    def _apply_decision_profile_dialog(self) -> None:
        manager = self._decision_profile_manager()
        names = manager.names()
        if not names:
            QMessageBox.information(
                self,
                translate_text("Profils"),
                translate_text("Aucun profil enregistré."),
            )
            return
        name, ok = QInputDialog.getItem(
            self,
            translate_text("Appliquer profil"),
            translate_text("Profil :"),
            names,
            0,
            False,
        )
        if not ok or not name:
            return
        profile = manager.load(name)
        if not profile:
            QMessageBox.warning(
                self,
                translate_text("Profil introuvable"),
                translate_text("Impossible de charger le profil sélectionné."),
            )
            return
        try:
            tracks, report = self._build_decision_profile_result(profile)
        except Exception as exc:
            QMessageBox.warning(
                self,
                translate_text("Profil incompatible"),
                str(exc),
            )
            return
        if not self._confirm_decision_profile_application(name, tracks, report):
            return
        self._commit_decision_profile_result(tracks)
        self.log_message.emit("OK", translate_text("Profil appliqué : {name}", name=name))

    def _apply_decision_profile(self, profile: dict) -> None:
        tracks, _report = self._build_decision_profile_result(profile)
        self._commit_decision_profile_result(tracks)

    def _build_decision_profile_result(self, profile: dict) -> tuple[list[TrackEntry], dict]:
        all_tracks = copy.deepcopy(self._track_table.current_tracks())
        if not all_tracks:
            return [], {"valid": True, "applied_rules": 0}
        source_index_by_file_id = {sf.id: index for index, sf in enumerate(self._source_files)}
        result = apply_decision_profile_v1(
            profile,
            all_tracks,
            source_index_by_file_id=source_index_by_file_id,
        )
        return result.tracks, result.report

    def _commit_decision_profile_result(self, tracks: list[TrackEntry]) -> None:
        by_file_id: dict[str, list[TrackEntry]] = {}
        for track in tracks:
            by_file_id.setdefault(track.file_id, []).append(track)
        for source in self._source_files:
            if source.id in by_file_id:
                source.tracks = by_file_id[source.id]
        self._replace_track_table_tracks(tracks)
        self._refresh_audio_sync_buttons()
        self._track_table.refresh_filter()
        self._rebuild_preview()
        self._emit_signals()

    def _confirm_decision_profile_application(
        self,
        name: str,
        tracks: list[TrackEntry],
        report: dict,
    ) -> bool:
        before_tracks = self._track_table.current_tracks()
        before_by_id = {track.entry_id: track for track in before_tracks}
        before_order = [track.entry_id for track in before_tracks]
        after_order = [track.entry_id for track in tracks]
        changed_tracks = 0
        for track in tracks:
            before = before_by_id.get(track.entry_id)
            if before is None:
                changed_tracks += 1
                continue
            if (
                before.enabled != track.enabled
                or before.language != track.language
                or before.title != track.title
                or before.flag_default != track.flag_default
                or before.flag_forced != track.flag_forced
                or before.flag_enabled != track.flag_enabled
                or before.flag_original != track.flag_original
                or before.flag_commentary != track.flag_commentary
                or before.flag_hearing_impaired != track.flag_hearing_impaired
                or before.flag_visual_impaired != track.flag_visual_impaired
                or before.time_shift_ms != track.time_shift_ms
            ):
                changed_tracks += 1

        lines = [
            translate_text("Profil : {name}", name=name),
            translate_text("Pistes modifiées : {count}", count=changed_tracks),
            translate_text("Règles appliquées : {count}", count=int(report.get("applied_rules", 0) or 0)),
        ]
        if before_order != after_order:
            lines.append(translate_text("Ordre des pistes modifié."))
        created_count = len(report.get("created_variants", []) if isinstance(report.get("created_variants"), list) else [])
        if created_count:
            lines.append(translate_text("Pistes audio créées : {count}", count=created_count))
        missing_count = len(report.get("missing_rules", []) if isinstance(report.get("missing_rules"), list) else [])
        if missing_count:
            lines.append(translate_text("Règles sans correspondance : {count}", count=missing_count))
        ambiguous_count = len(report.get("ambiguous_matches", report.get("ambiguous_rules", [])) if isinstance(report.get("ambiguous_matches", report.get("ambiguous_rules", [])), list) else [])
        if ambiguous_count:
            lines.append(translate_text("Règles ambiguës ignorées : {count}", count=ambiguous_count))
        conflict_count = len(report.get("conflicts", []) if isinstance(report.get("conflicts"), list) else [])
        if conflict_count:
            lines.append(translate_text("Conflits non résolus : {count}", count=conflict_count))
        resolved_count = len(report.get("resolved_writes", []) if isinstance(report.get("resolved_writes"), list) else [])
        if resolved_count:
            lines.append(translate_text("Écritures résolues par mode/priorité : {count}", count=resolved_count))
        skipped_count = len(report.get("skipped_writes", []) if isinstance(report.get("skipped_writes"), list) else [])
        if skipped_count:
            lines.append(translate_text("Écritures ignorées par priorité : {count}", count=skipped_count))
        if report.get("legacy_converted"):
            lines.append(translate_text("Ancien profil converti automatiquement. Resauvegardez-le pour stabiliser le format."))
        if changed_tracks == 0 and before_order == after_order and not created_count and not missing_count and not ambiguous_count and not conflict_count and not resolved_count and not skipped_count:
            lines.append(translate_text("Aucun changement détecté."))

        message = "\n".join(lines)
        if conflict_count:
            QMessageBox.warning(
                self,
                translate_text("Aperçu du profil"),
                message + "\n\n" + translate_text("Corrigez les priorités ou les règles avant application."),
            )
            return False
        message += "\n\n" + translate_text("Appliquer ce profil ?")
        reply = QMessageBox.question(
            self,
            translate_text("Aperçu du profil"),
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _replace_track_table_tracks(self, tracks: list[TrackEntry]) -> None:
        self._auto_sync_entry_ids.intersection_update({track.entry_id for track in tracks})
        self._track_table.set_auto_sync_cancelable_entries(self._auto_sync_entry_ids)
        self._track_table.blockSignals(True)
        try:
            self._track_table.setRowCount(0)
            self._track_table._prev_lang.clear()
            for entry in tracks:
                row = self._track_table.rowCount()
                self._track_table.insertRow(row)
                source_color = self._source_colors.get(entry.file_id, _C.BORDER)
                self._track_table._fill_row(row, entry, source_color)
        finally:
            self._track_table.blockSignals(False)
            self._track_table._adjust_height()

    def refresh_runtime_settings(self) -> None:
        self._workflow.set_ffmpeg_bin(self._config.tool_ffmpeg)
        self._workflow.set_ffprobe_bin(self._config.tool_ffprobe)
        self._workflow.set_ffmpeg_threads(self._config.ffmpeg_threads)
        self._workflow.set_generate_nfo(self._config.generate_nfo)
        self._workflow.set_mediainfo_bin(self._config.tool_mediainfo)
        self._workflow.set_sync_rewrite_enabled(self._config.sync_rewrite_enabled)
        self._workflow.set_sync_advanced_audio_rewrite_enabled(
            self._config.sync_advanced_audio_rewrite_enabled
        )
        self._workflow.set_sync_rewrite_audio_bitrates(
            aac_bitrate_per_channel_kbps=self._config.aac_bitrate_per_channel_kbps,
            eac3_bitrate_per_channel_kbps=self._config.eac3_bitrate_per_channel_kbps,
        )
        self._track_table.set_sync_rewrite_enabled(
            self._config.sync_rewrite_enabled,
            advanced_audio_enabled=self._config.sync_advanced_audio_rewrite_enabled,
        )
        backend_index = self._mux_backend_combo.findData(
            self._config.matroska_mux_backend
        )
        if backend_index >= 0 and backend_index != self._mux_backend_combo.currentIndex():
            # Le réglage Matroska est global : sa sauvegarde doit affecter le
            # prochain job sans redémarrage ni recréation du panneau.
            self._mux_backend_combo.setCurrentIndex(backend_index)
        self._rebuild_preview()

    def update_audio_track_meta(
        self,
        stream_index: int,
        source_path,
        lang: str,
        title: str,
        entry_id,
    ) -> None:
        file_id = next(
            (sf.id for sf in self._source_files if sf.info and sf.info.path == source_path),
            None,
        )
        if file_id is None:
            return
        self._track_table.update_audio_meta(
            file_id,
            stream_index,
            lang,
            title,
            entry_id=str(entry_id or "").strip() or None,
        )
        self._rebuild_preview()

    @staticmethod
    def _audio_encode_codec_label(codec: str) -> str:
        normalized = (codec or "copy").strip().lower()
        return {
            "aac": "AAC",
            "ac3": "AC3",
            "eac3": "EAC3",
            "flac": "FLAC",
        }.get(normalized, normalized.upper() if normalized else "COPY")

    @staticmethod
    def _audio_encode_display_info(source_display_info: str, codec: str, bitrate_kbps: int) -> str:
        normalized = (codec or "copy").strip().lower()
        if normalized == "copy":
            return source_display_info
        parts: list[str] = []
        for raw_part in str(source_display_info or "").replace("·", "  ").split("  "):
            part = raw_part.strip()
            if not part or "kbps" in part.lower():
                continue
            parts.append(part)
        if bitrate_kbps > 0:
            parts.append(f"{int(bitrate_kbps)} kbps")
        return "  ".join(parts)

    def _source_track_for_variant(self, entry: TrackEntry) -> TrackEntry:
        source = self._find_source(entry.file_id)
        if source is None:
            return entry
        source_entry_id = entry.source_entry_id or entry.entry_id
        return next((track for track in source.tracks if track.entry_id == source_entry_id), entry)

    def _apply_audio_encoding_to_entry(
        self,
        entry: TrackEntry,
        codec: str,
        bitrate_kbps: int,
    ) -> None:
        source_entry = self._source_track_for_variant(entry)
        normalized = (codec or "copy").strip().lower()
        if normalized == "copy":
            entry.codec = source_entry.orig_codec or source_entry.codec
            entry.display_info = source_entry.orig_display_info or source_entry.display_info
            return
        entry.codec = self._audio_encode_codec_label(normalized)
        entry.display_info = self._audio_encode_display_info(
            source_entry.orig_display_info or source_entry.display_info,
            normalized,
            bitrate_kbps,
        )

    def add_audio_track_variant(
        self,
        template_entry: TrackEntry,
        entry_id: str = "",
        codec: str = "copy",
        bitrate_kbps: int = 0,
    ) -> None:
        if template_entry.track_type != "audio":
            return
        if self._track_table.has_entry_id(entry_id or template_entry.entry_id):
            return

        source = self._find_source(template_entry.file_id)
        if source is None or source.info is None:
            self.log_message.emit("WARN", translate_text("Source introuvable pour cette piste."))
            return

        new_entry = clone_track_entry(template_entry, entry_id=entry_id or None)
        self._apply_audio_encoding_to_entry(new_entry, codec, bitrate_kbps)
        source.tracks.append(new_entry)
        source_color = self._source_colors.get(template_entry.file_id, _C.BORDER)
        self._track_table.append_tracks(source_color, [new_entry])
        self._refresh_audio_sync_buttons()
        self._track_table.refresh_filter()
        self._rebuild_preview()
        self._emit_audio_tracks()

    def remove_audio_track_variant(self, entry_id) -> None:
        entry_id_str = str(entry_id or "").strip()
        if not entry_id_str:
            return
        for source in self._source_files:
            source.tracks = [track for track in source.tracks if track.entry_id != entry_id_str]
        if self._track_table.remove_track_by_entry_id(entry_id_str):
            self._refresh_audio_sync_buttons()
            self._track_table.refresh_filter()
            self._rebuild_preview()
            self._emit_audio_tracks()

    def update_audio_track_encoding(self, entry_id, codec: str, bitrate_kbps: int) -> None:
        entry_id_str = str(entry_id or "").strip()
        if not entry_id_str:
            return
        for source in self._source_files:
            for entry in source.tracks:
                if entry.entry_id != entry_id_str or entry.track_type != "audio":
                    continue
                previous = (entry.codec, entry.display_info)
                self._apply_audio_encoding_to_entry(entry, codec, bitrate_kbps)
                if (entry.codec, entry.display_info) == previous:
                    return
                if self._track_table.update_audio_encoding(
                    entry.entry_id,
                    entry.codec,
                    entry.display_info,
                ):
                    self._rebuild_preview()
                    self._emit_audio_tracks()
                return

    def _audio_entries_by_source(self) -> dict[str, list[TrackEntry]]:
        by_source: dict[str, list[TrackEntry]] = {}
        for entry in self._track_table.current_tracks():
            if entry.track_type != "audio":
                continue
            by_source.setdefault(entry.file_id, []).append(entry)
        return by_source

    def _refresh_audio_sync_buttons(self) -> None:
        by_source = self._audio_entries_by_source()
        self._track_table.set_audio_sync_available(len(by_source) >= 2)
        self._refresh_sync_action_buttons()

    def _track_sync_counts(self) -> dict[str, int]:
        counts = {"video": 0, "audio": 0, "subtitle": 0}
        for entry in self._track_table.current_tracks():
            if entry.track_type in counts and int(entry.time_shift_ms or 0) != 0:
                counts[entry.track_type] += 1
        return counts

    def _source_has_chapters(self, source_index: int) -> bool:
        if source_index < 0 or source_index >= len(self._source_files):
            return False
        source = self._source_files[source_index]
        return bool(source.info and source.info.chapters and source.info.chapters.entries)

    def _chapter_sync_is_active_for_index(self, source_index: int | None) -> bool:
        return (
            source_index is not None
            and self._source_has_chapters(source_index)
            and self._chapter_sync_offset_ms_for_index(source_index) != 0
        )

    def _any_chapter_sync_active(self) -> bool:
        return any(
            self._chapter_sync_is_active_for_index(index)
            for index in range(len(self._source_files))
        )

    def _global_sync_tooltip(self, counts: dict[str, int], chapter_active: bool) -> str:
        return (
            "<html><body style='white-space:nowrap;'>"
            "Synchro : "
            f"<span style='color:{_C.TRACK_VIDEO}; font-weight:700;'>V</span>={counts['video']} "
            f"<span style='color:{_C.TRACK_AUDIO}; font-weight:700;'>A</span>={counts['audio']} "
            f"<span style='color:{_C.TRACK_SUBTITLE}; font-weight:700;'>S</span>={counts['subtitle']} "
            f"<span style='color:{_C.TRACK_TAGS}; font-weight:700;'>C</span>={'Yes' if chapter_active else 'No'}"
            "</body></html>"
        )

    def _refresh_sync_action_buttons(self) -> None:
        if hasattr(self, "_track_table"):
            self._track_table.set_auto_sync_cancelable_entries(self._auto_sync_entry_ids)
        selected_source_index = (
            self._chapter_panel.selected_source_index()
            if hasattr(self, "_chapter_panel")
            else None
        )
        chapter_active_for_selection = self._chapter_sync_is_active_for_index(selected_source_index)
        if hasattr(self, "_chapter_panel"):
            self._chapter_panel.set_sync_cancel_available(chapter_active_for_selection)

        counts = self._track_sync_counts() if hasattr(self, "_track_table") else {"video": 0, "audio": 0, "subtitle": 0}
        chapter_active = self._any_chapter_sync_active()
        active = any(counts.values()) or chapter_active
        if hasattr(self, "_global_sync_cancel_btn"):
            self._global_sync_cancel_btn.setVisible(active)
            self._global_sync_cancel_btn.setToolTip(self._global_sync_tooltip(counts, chapter_active))

    def _on_sync_rewrite_toggle_requested(self, entry: TrackEntry) -> None:
        if not ui_sync_rewrite_can_toggle(
            entry,
            enabled=self._config.sync_rewrite_enabled,
            advanced_audio_enabled=self._config.sync_advanced_audio_rewrite_enabled,
        ):
            return
        current = normalized_sync_rewrite_mode(entry.sync_rewrite_mode)
        entry.sync_rewrite_mode = "" if current == SYNC_REWRITE_MODE_OFFSET else SYNC_REWRITE_MODE_OFFSET
        self._track_table.refresh_entry_info(entry.entry_id)
        self._rebuild_preview()
        self._emit_signals()

    def _prune_auto_sync_entry_ids(self) -> None:
        active = {
            entry.entry_id
            for entry in self._track_table.current_tracks()
            if entry.track_type in {"audio", "subtitle"} and int(entry.time_shift_ms or 0) != 0
        }
        if self._auto_sync_entry_ids.issubset(active):
            return
        self._auto_sync_entry_ids.intersection_update(active)
        self._track_table.set_auto_sync_cancelable_entries(self._auto_sync_entry_ids)

    @staticmethod
    def _audio_sync_family(entry: TrackEntry) -> str | None:
        info = (entry.display_info or "").lower()
        if (
            "5.1" in info or "7.1" in info or
            "6 ch" in info or "8 ch" in info or
            "6 channel" in info or "8 channel" in info
        ):
            return "surround"
        if (
            "stereo" in info or "2.0" in info or
            "2 ch" in info or "2ch" in info or "2 channel" in info
        ):
            return "stereo"
        return None

    def _audio_sync_reference_choices(self, target: TrackEntry) -> list[tuple[str, TrackEntry]]:
        choices: list[tuple[str, TrackEntry]] = []
        target_family = self._audio_sync_family(target)
        if target_family is None:
            return choices
        seen_sources: set[str] = set()
        for sf in self._source_files:
            if sf.id == target.file_id or sf.id in seen_sources:
                continue
            candidates = [
                entry for entry in self._track_table.current_tracks()
                if entry.file_id == sf.id
                and entry.track_type == "audio"
                and self._audio_sync_family(entry) == target_family
            ]
            if not candidates:
                continue
            ref = candidates[0]
            title = f" - {ref.title}" if ref.title else ""
            choices.append((
                f"{sf.path.name} | #{ref.mkv_tid} {ref.codec} {ref.display_info}{title}",
                ref,
            ))
            seen_sources.add(sf.id)
        return choices

    def _audio_sync_track(self, entry: TrackEntry) -> AudioSyncTrack | None:
        source = self._find_source(entry.file_id)
        if source is None:
            return None
        return AudioSyncTrack(source_path=source.path, stream_index=int(entry.mkv_tid))

    def _source_index_for_file_id(self, file_id: str) -> int | None:
        for index, source in enumerate(self._source_files):
            if source.id == file_id:
                return index
        return None

    def _source_sync_offset_ms_for_index(self, source_index: int | None) -> int:
        if source_index is None or source_index < 0 or source_index >= len(self._source_files):
            return 0
        return int(self._source_sync_offsets_ms.get(self._source_files[source_index].id, 0) or 0)

    def _chapter_sync_offset_ms_for_index(self, source_index: int | None) -> int:
        if source_index is None or source_index < 0 or source_index >= len(self._source_files):
            return 0
        source_id = self._source_files[source_index].id
        if source_id in self._chapter_sync_cancelled_source_ids:
            return 0
        return self._source_sync_offset_ms_for_index(source_index)

    def _shift_chapter_entries(
        self,
        entries: list[ChapterEntry],
        offset_ms: int,
    ) -> list[ChapterEntry]:
        delta_s = int(offset_ms or 0) / 1000.0
        if abs(delta_s) < 0.0005:
            return list(entries)
        return [
            ChapterEntry(
                timecode_s=max(0.0, float(entry.timecode_s) + delta_s),
                name=entry.name,
            )
            for entry in entries
        ]

    def _source_chapter_entries_with_offset(
        self,
        source_index: int,
        offset_ms: int,
    ) -> list[ChapterEntry]:
        if source_index < 0 or source_index >= len(self._source_files):
            return []
        source = self._source_files[source_index]
        if not source.info or not source.info.chapters or not source.info.chapters.entries:
            return []
        return self._shift_chapter_entries(
            list(source.info.chapters.entries),
            offset_ms,
        )

    def _chapter_entries_for_source(self, source_index: int) -> list[ChapterEntry]:
        return self._source_chapter_entries_with_offset(
            source_index,
            self._chapter_sync_offset_ms_for_index(source_index),
        )

    @staticmethod
    def _chapter_entries_equal(
        left: list[ChapterEntry],
        right: list[ChapterEntry],
    ) -> bool:
        if len(left) != len(right):
            return False
        return all(
            abs(float(a.timecode_s) - float(b.timecode_s)) < 0.0005
            and str(a.name or "") == str(b.name or "")
            for a, b in zip(left, right, strict=True)
        )

    def _sync_selected_chapters_after_source_offset_change(
        self,
        source_index: int | None,
        selected_source_index: int | None,
        old_offset_ms: int,
        new_offset_ms: int,
    ) -> None:
        if (
            source_index is None
            or selected_source_index != source_index
            or old_offset_ms == new_offset_ms
        ):
            return

        expected_old = self._source_chapter_entries_with_offset(source_index, old_offset_ms)
        expected_new = self._source_chapter_entries_with_offset(source_index, new_offset_ms)
        current = self._chapter_panel.get_chapters()
        if self._chapter_entries_equal(current, expected_old):
            self._chapter_panel.reset_chapters(expected_new)
            return
        self._chapter_panel.shift_timecodes((new_offset_ms - old_offset_ms) / 1000.0)

    def _apply_time_shift_to_source_tracks(self, file_id: str, offset_ms: int) -> bool:
        if not file_id:
            return False
        updated = False
        for entry in self._track_table.current_tracks():
            if entry.file_id != file_id or entry.track_type not in {"audio", "subtitle"}:
                continue
            if self._track_table.update_time_shift(entry.entry_id, int(offset_ms)):
                updated = True
        return updated

    def _sync_entry_ids_for_file(self, file_id: str) -> set[str]:
        if not file_id:
            return set()
        return {
            entry.entry_id
            for entry in self._track_table.current_tracks()
            if entry.file_id == file_id and entry.track_type in {"audio", "subtitle"}
        }

    def _file_has_shifted_timeline_tracks(self, file_id: str) -> bool:
        if not file_id:
            return False
        return any(
            entry.file_id == file_id
            and entry.track_type in {"audio", "subtitle"}
            and int(entry.time_shift_ms or 0) != 0
            for entry in self._track_table.current_tracks()
        )

    def _apply_source_sync_offset(
        self,
        file_id: str,
        offset_ms: int,
    ) -> bool:
        if not file_id:
            return False

        offset_ms = int(offset_ms)
        sync_entry_ids = self._sync_entry_ids_for_file(file_id)
        source_index = self._source_index_for_file_id(file_id)
        selected_source_index = self._chapter_panel.selected_source_index()
        old_chapter_offset_ms = self._chapter_sync_offset_ms_for_index(source_index)
        if offset_ms:
            self._source_sync_offsets_ms[file_id] = offset_ms
            self._chapter_sync_cancelled_source_ids.discard(file_id)
            self._auto_sync_entry_ids.update(sync_entry_ids)
        else:
            self._source_sync_offsets_ms.pop(file_id, None)
            self._chapter_sync_cancelled_source_ids.discard(file_id)
            self._auto_sync_entry_ids.difference_update(sync_entry_ids)

        updated = self._apply_time_shift_to_source_tracks(file_id, offset_ms)
        self._update_chapters_from_sources()
        new_chapter_offset_ms = self._chapter_sync_offset_ms_for_index(source_index)
        self._sync_selected_chapters_after_source_offset_change(
            source_index,
            selected_source_index,
            old_chapter_offset_ms,
            new_chapter_offset_ms,
        )
        self._refresh_sync_action_buttons()
        return updated or source_index is not None

    def _on_auto_sync_cancel_requested(self, entry: TrackEntry) -> None:
        if entry.track_type not in {"audio", "subtitle"}:
            return
        source_index = self._source_index_for_file_id(entry.file_id)
        selected_source_index = self._chapter_panel.selected_source_index()
        old_chapter_offset_ms = self._chapter_sync_offset_ms_for_index(source_index)
        if not self._track_table.update_time_shift(entry.entry_id, 0):
            return

        self._auto_sync_entry_ids.discard(entry.entry_id)
        if not self._file_has_shifted_timeline_tracks(entry.file_id):
            self._source_sync_offsets_ms.pop(entry.file_id, None)
            self._chapter_sync_cancelled_source_ids.discard(entry.file_id)
            self._update_chapters_from_sources()
            new_chapter_offset_ms = self._chapter_sync_offset_ms_for_index(source_index)
            self._sync_selected_chapters_after_source_offset_change(
                source_index,
                selected_source_index,
                old_chapter_offset_ms,
                new_chapter_offset_ms,
            )
        self._refresh_sync_action_buttons()
        self.log_message.emit(
            "INFO",
            translate_text(
                "Synchronisation annulée pour la piste #{idx}.",
                idx=entry.mkv_tid,
            ),
        )
        self._rebuild_preview()
        self._emit_signals()

    def _on_chapter_sync_cancel_requested(self) -> None:
        source_index = self._chapter_panel.selected_source_index()
        if not self._chapter_sync_is_active_for_index(source_index):
            return
        assert source_index is not None
        old_chapter_offset_ms = self._chapter_sync_offset_ms_for_index(source_index)
        source_id = self._source_files[source_index].id
        self._chapter_sync_cancelled_source_ids.add(source_id)
        self._update_chapters_from_sources()
        self._sync_selected_chapters_after_source_offset_change(
            source_index,
            source_index,
            old_chapter_offset_ms,
            0,
        )
        self._refresh_sync_action_buttons()
        self.log_message.emit("INFO", translate_text("Synchronisation des chapitres annulée."))
        self._rebuild_preview()
        self._emit_signals()

    def _on_global_sync_cancel_requested(self) -> None:
        selected_source_index = self._chapter_panel.selected_source_index()
        old_chapter_offset_ms = self._chapter_sync_offset_ms_for_index(selected_source_index)
        for entry in self._track_table.current_tracks():
            if int(entry.time_shift_ms or 0) != 0:
                self._track_table.update_time_shift(entry.entry_id, 0)
        self._source_sync_offsets_ms.clear()
        self._auto_sync_entry_ids.clear()
        self._chapter_sync_cancelled_source_ids.clear()
        self._update_chapters_from_sources()
        self._sync_selected_chapters_after_source_offset_change(
            selected_source_index,
            selected_source_index,
            old_chapter_offset_ms,
            0,
        )
        self._refresh_sync_action_buttons()
        self.log_message.emit("INFO", translate_text("Toutes les synchronisations ont été annulées."))
        self._rebuild_preview()
        self._emit_signals()

    def _on_audio_sync_requested(self, entry: TrackEntry) -> None:
        if entry.track_type != "audio":
            return
        target_family = self._audio_sync_family(entry)
        if target_family is None:
            self.log_message.emit("ERROR", "impossible d'utiliser la synchronisation améliorée")
            return

        choices = self._audio_sync_reference_choices(entry)
        if not choices:
            self.log_message.emit("ERROR", "impossible d'utiliser la synchronisation améliorée")
            return

        dialog = _AudioSyncReferenceDialog(choices, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        reference_entry = dialog.selected_entry()
        if reference_entry is None:
            return
        if target_family == "stereo":
            QMessageBox.warning(
                self,
                translate_text("Synchronisation stéréo"),
                translate_text(
                    "La synchronisation stéréo n'est ni précise ni conseillée. "
                    "Elle cherche de gros marqueurs sonores sur le même côté, "
                    "mais le résultat doit être vérifié."
                ),
            )

        reference = self._audio_sync_track(reference_entry)
        target = self._audio_sync_track(entry)
        if reference is None or target is None:
            self.log_message.emit("ERROR", "impossible d'utiliser la synchronisation améliorée")
            return

        self.log_message.emit(
            "INFO",
            translate_text(
                "Synchronisation audio améliorée : piste #{target} vs référence #{reference}…",
                target=entry.mkv_tid,
                reference=reference_entry.mkv_tid,
            ),
        )
        self.audio_sync_started.emit({
            "label": translate_text(
                "Synchronisation audio améliorée : piste #{target} vs référence #{reference}…",
                target=entry.mkv_tid,
                reference=reference_entry.mkv_tid,
            )
        })

        target_entry_id = entry.entry_id
        reference_entry_id = reference_entry.entry_id

        def _task() -> None:
            try:
                workflow = AudioSyncWorkflow(
                    ffmpeg_bin=self._config.tool_ffmpeg,
                    ffprobe_bin=self._config.tool_ffprobe,
                    log_cb=self.log_message.emit,
                )
                result = workflow.detect_offset(reference, target)
                self._audio_sync_done.emit(
                    target_entry_id,
                    reference_entry_id,
                    result.offset_ms,
                    result.confidence,
                )
            except Exception as exc:
                self._audio_sync_error.emit(target_entry_id, str(exc))

        self._executor.submit(_task)

    def _on_audio_sync_done(
        self,
        entry_id: str,
        reference_entry_id: str,
        offset_ms: int,
        confidence: float,
    ) -> None:
        try:
            tracks_by_id = {
                track.entry_id: track
                for track in self._track_table.current_tracks()
            }
            target_entry = tracks_by_id.get(entry_id)
            reference_entry = tracks_by_id.get(reference_entry_id) if reference_entry_id else None
            target_file_id = target_entry.file_id if target_entry is not None else ""
            reference_file_id = reference_entry.file_id if reference_entry is not None else ""

            if reference_file_id and reference_file_id != target_file_id:
                self._apply_source_sync_offset(reference_file_id, 0)
            if self._apply_source_sync_offset(
                target_file_id,
                offset_ms,
            ):
                offset_label = f"{int(offset_ms):+d}"
                confidence_label = f"{float(confidence):.2f}"
                self.log_message.emit(
                    "OK",
                    translate_text(
                        "Synchronisation audio améliorée appliquée : {offset} ms (confiance {confidence}).",
                        offset=offset_label,
                        confidence=confidence_label,
                    ),
                )
                self._rebuild_preview()
                self._emit_signals()
        finally:
            self.audio_sync_finished.emit(True, {"entry_id": entry_id})

    def _on_audio_sync_error(self, _entry_id: str, detail: str) -> None:
        try:
            self.log_message.emit("ERROR", "impossible d'utiliser la synchronisation améliorée")
            if detail:
                self.log_message.emit("ERROR", detail)
        finally:
            self.audio_sync_finished.emit(False, {"entry_id": _entry_id})

    def update_video_track_encoding(self, plans) -> None:
        plan_map: dict[str, str] = {}
        for plan in plans or []:
            entry_id = str(getattr(plan, "track_entry_id", "") or "").strip()
            if not entry_id:
                continue
            target_codec = str(getattr(plan, "target_codec", "") or "").strip().lower()
            if not target_codec:
                summary = str(getattr(plan, "codec_summary", "") or "").strip()
                if summary.lower() == "copy":
                    target_codec = "copy"
                else:
                    target_codec = summary.split(" - ", 1)[0].strip().lower()
            plan_map[entry_id] = target_codec

        table_changed = self._track_table.update_video_encoding_plans(plan_map, clear_missing=True)
        model_changed = False
        for source in self._source_files:
            for entry in source.tracks:
                if entry.track_type != "video":
                    continue
                target_codec = plan_map.get(entry.entry_id, "")
                modified = bool(target_codec and target_codec != "copy")
                if entry.encode_plan_codec == target_codec and entry.encode_plan_modified == modified:
                    continue
                entry.encode_plan_codec = target_codec
                entry.encode_plan_summary = ""
                entry.encode_plan_hdr_badges = ()
                entry.encode_plan_modified = modified
                model_changed = True

        if table_changed or model_changed:
            self._rebuild_preview()
            self._emit_video_tracks()

    def current_output_path(self) -> Path | None:
        text = self._output_edit.text().strip()
        return Path(text) if text else None

    def current_file_title(self) -> str:
        return self._file_title_edit.text().strip()

    def current_mux_backend(self) -> str:
        return str(
            self._mux_backend_combo.currentData()
            or self._config.matroska_mux_backend
        )

    def current_extra_attachments(self) -> list:
        return self._attachment_panel.get_extra_attachments()

    def current_tmdb_cover(self) -> tuple[str, str] | None:
        return self._attachment_panel.get_pending_tmdb_cover()

    def current_tag_overrides(self) -> dict[str, str] | None:
        return self._attachment_panel.get_global_tag_overrides()

    def current_chapter_overrides(self) -> list | None:
        keep_ch = self._chapter_panel.keep_chapters()
        selected_source_index = self._chapter_panel.selected_source_index()
        source_has_sync_offset = self._chapter_sync_offset_ms_for_index(selected_source_index) != 0
        if keep_ch and (self._chapter_panel.is_modified() or source_has_sync_offset):
            return self._chapter_panel.get_chapters()
        return None

    def _on_chapters_changed(self) -> None:
        chapter_functions.on_chapters_changed(self)

    def _update_chapters_from_sources(self) -> None:
        chapter_functions.update_chapters_from_sources(self)
        self._refresh_sync_action_buttons()

    def _reset_empty_state(self) -> None:
        chapter_functions.reset_empty_state(self)

    def _resolve_base_chapters(self) -> list[ChapterEntry]:
        return chapter_functions.resolve_base_chapters(self)

    def is_ready(self) -> bool:
        return self._has_ready_files()

    def get_duration_s(self) -> float | None:
        """Durée de la première source (pour le calcul de progression dans MainWindow)."""
        for sf in self._source_files:
            if sf.info and sf.info.duration_s:
                return sf.info.duration_s
        return None

    def run_operation(self, config: RemuxConfig) -> TaskSignals:
        return self._workflow.run(config)

    def validate_config(self, config: RemuxConfig) -> list[str]:
        return self._workflow.validate(config)

    def _set_all_tracks(self, enabled: bool) -> None:
        self._track_table.set_all_enabled(enabled)
        self._refresh_audio_sync_buttons()
        self._track_table.refresh_filter()
        self._rebuild_preview()

    def _browse_output(self) -> None:
        default = self._output_edit.text() or str(self._config.output_dir)
        path, _ = QFileDialog.getSaveFileName(
            self,
            translate_text("Fichier de sortie"),
            default,
            translate_text("Matroska (*.mkv);;Tous les fichiers (*)"),
        )
        if path:
            self._output_edit.setText(path)

    def _on_extract_track(self, entry: TrackEntry) -> None:
        source = self._find_source(entry.file_id)
        if source is None or source.info is None:
            self.log_message.emit("WARN", translate_text("Source introuvable pour cette piste."))
            return

        codec = (entry.codec or "").lower()
        try:
            plan = TrackExtractor.plan_subtitle(codec)
        except ValueError as exc:
            QMessageBox.warning(
                self,
                translate_text("Extraction impossible"),
                str(exc),
            )
            return

        default_name = TrackExtractor.default_output_name(
            source.path.stem, entry.language, entry.mkv_tid, plan.extension,
        )
        default_path = source.path.parent / default_name
        out_str, _ = QFileDialog.getSaveFileName(
            self,
            translate_text("Extraire le sous-titre"),
            str(default_path),
            plan.file_filter,
        )
        if not out_str:
            return

        out_path = Path(out_str)
        cmd = TrackExtractor.build_subtitle_command(
            self._config.tool_ffmpeg,
            source.path,
            entry.mkv_tid,
            codec,
            out_path,
            progress_args=ffmpeg_progress_args(),
        )

        self.log_message.emit(
            "INFO",
            translate_text(
                "Extraction du sous-titre #{idx} ({codec}) vers {name}…",
                idx=entry.mkv_tid, codec=plan.format_label, name=out_path.name,
            ),
        )

        runner = ToolRunner()
        self._extract_runner = runner  # conserve la référence pendant l'exécution
        label = f"extract-sub-{entry.mkv_tid}"
        signals = runner.run(cmd, label=label)
        self.extract_started.emit(
            signals,
            {
                "duration_s": source.info.duration_s,
                "label": translate_text("Extraction du sous-titre"),
                "output_name": out_path.name,
            },
        )
        signals.finished.connect(
            lambda _=None, p=out_path: self.log_message.emit(
                "OK", translate_text("Sous-titre extrait : {path}", path=str(p)),
            ),
            Qt.ConnectionType.QueuedConnection,
        )
        signals.failed.connect(
            lambda msg, _exc: self.log_message.emit(
                "ERROR", translate_text("Extraction échouée : {msg}", msg=msg),
            ),
            Qt.ConnectionType.QueuedConnection,
        )

    def _copy_command(self) -> None:
        from PySide6.QtWidgets import QApplication

        # Le debounce peut laisser le texte précédent pendant quelques
        # millisecondes. Pour une copie explicite, la justesse prime : ne pas
        # recopier une commande correspondant à l'ancien ordre des pistes.
        if self._preview_dirty:
            self._refresh_preview_synchronously()
        text = self._cmd_preview.toPlainText()
        if text:
            QApplication.clipboard().setText(text)

    def closeEvent(self, event) -> None:
        self._closing = True
        self._preview_timer.stop()
        if self._preview_future is not None:
            self._preview_future.cancel()
        self._preview_executor.shutdown(wait=True)
        self._executor.shutdown(wait=True)
        super().closeEvent(event)


__all__ = ["RemuxPanel"]
