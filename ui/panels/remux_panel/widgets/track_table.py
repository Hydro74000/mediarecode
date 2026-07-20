"""Tableau des pistes pour RemuxPanel."""

from __future__ import annotations

from PySide6.QtCore import QSize, QTimer, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QMenu,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from core.i18n import translate_text
from core.lang_tags import Rfc5646LanguageTags
from core.workflows.common.sync_rewrite import (
    sync_rewrite_forced_offset,
    ui_sync_rewrite_can_toggle,
    ui_sync_rewrite_label_for_track,
    ui_sync_rewrite_preview_for_track,
)
from core.workflows.remux_models import TrackEntry
from ui.panels.remux_panel.models import (
    _TRACK_INFO_OFFSET_NEG_COLOR,
    _TRACK_INFO_OFFSET_POS_COLOR,
    _TRACK_INFO_OFFSET_VALUE_ROLE,
    _TRACK_INFO_SYNC_LABEL_ROLE,
)
from ui.panels.remux_panel.theme import _C, _pencil_icon, _refresh_icon, _warning_icon, _x_icon
from ui.panels.track_edit_dialog import TrackEditDialog

class _TrackInfoDelegate(QStyledItemDelegate):
    _SYNC_LABEL_COLOR = QColor(_C.ACCENT)

    @staticmethod
    def _offset_color(offset_value: str) -> QColor:
        return (
            _TRACK_INFO_OFFSET_NEG_COLOR
            if str(offset_value or "").strip().startswith("-")
            else _TRACK_INFO_OFFSET_POS_COLOR
        )

    @staticmethod
    def _find_next_marker(text: str, cursor: int, markers: dict[str, str]) -> tuple[int, str, str] | None:
        best: tuple[int, str, str] | None = None
        for kind, marker in markers.items():
            if not marker:
                continue
            position = text.find(marker, cursor)
            if position < 0:
                continue
            if best is None or position < best[0]:
                best = (position, kind, marker)
        return best

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        value = index.data(_TRACK_INFO_OFFSET_VALUE_ROLE)
        offset_value = str(value).strip() if value is not None else ""
        sync_value = index.data(_TRACK_INFO_SYNC_LABEL_ROLE)
        sync_label = str(sync_value).strip() if sync_value is not None else ""
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        markers = {
            "offset": offset_value if offset_value and offset_value in text else "",
            "sync": sync_label if sync_label and sync_label in text else "",
        }
        if not text or not any(markers.values()):
            super().paint(painter, option, index)
            return

        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        widget = opt.widget

        def get_style(obj):
            return obj.style() if isinstance(obj, QWidget) else None

        style = get_style(widget) or get_style(self.parent())

        if style is None:
            super().paint(painter, option, index)
            return

        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        text_rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, widget)
        if text_rect.width() <= 0 or text_rect.height() <= 0:
            return

        fg_value = index.data(Qt.ItemDataRole.ForegroundRole)
        if isinstance(fg_value, QBrush):
            normal = fg_value.color()
        elif isinstance(fg_value, QColor):
            normal = fg_value
        else:
            normal = opt.palette.color(QPalette.ColorRole.Text)
        if opt.state & QStyle.StateFlag.State_Selected:
            normal = opt.palette.color(QPalette.ColorRole.HighlightedText)

        painter.save()
        painter.setClipRect(text_rect)
        metrics = QFontMetrics(opt.font)
        baseline = text_rect.y() + (text_rect.height() + metrics.ascent() - metrics.descent()) // 2

        x = text_rect.x()
        cursor = 0
        while cursor < len(text):
            marker = self._find_next_marker(text, cursor, markers)
            if marker is None:
                chunk = text[cursor:]
                painter.setFont(opt.font)
                painter.setPen(normal)
                painter.drawText(x, baseline, chunk)
                x += metrics.horizontalAdvance(chunk)
                break

            position, kind, marker_text = marker
            if position > cursor:
                chunk = text[cursor:position]
                painter.setFont(opt.font)
                painter.setPen(normal)
                painter.drawText(x, baseline, chunk)
                x += metrics.horizontalAdvance(chunk)

            if kind == "offset":
                painter.setFont(opt.font)
                painter.setPen(self._offset_color(marker_text))
            else:
                sync_font = QFont(opt.font)
                sync_font.setUnderline(True)
                painter.setFont(sync_font)
                painter.setPen(self._SYNC_LABEL_COLOR)
            painter.drawText(x, baseline, marker_text)
            x += metrics.horizontalAdvance(marker_text)
            cursor = position + len(marker_text)

        painter.restore()

class _TrackTable(QTableWidget):
    order_changed = Signal()
    extract_requested = Signal(object)  # TrackEntry
    audio_sync_requested = Signal(object)  # TrackEntry
    auto_sync_cancel_requested = Signal(object)  # TrackEntry
    sync_rewrite_toggle_requested = Signal(object)  # TrackEntry

    _TYPE_ORDER: dict[str, int] = {"video": 0, "audio": 1, "subtitle": 2}
    _MAX_VISIBLE_ROWS = 15
    _ROW_H_DEFAULT = 28
    _NEW_TRACK_COLOR = QColor(_C.ERROR)
    _VIDEO_ENCODE_COLOR = QColor(_C.ACCENT)
    _VIDEO_ENCODE_CODEC_COLOR = QColor(_C.ERROR)
    _VIDEO_CODEC_SHORT_LABELS: dict[str, str] = {
        "libx265": "HEVC",
        "hevc_nvenc": "HEVC",
        "hevc_amf": "HEVC",
        "hevc_qsv": "HEVC",
        "hevc_vaapi": "HEVC",
        "nvencc_hevc": "HEVC",
        "libx264": "H264",
        "h264_nvenc": "H264",
        "h264_amf": "H264",
        "h264_qsv": "H264",
        "h264_vaapi": "H264",
        "nvencc_h264": "H264",
        "libsvtav1": "AV1",
        "av1_nvenc": "AV1",
        "av1_amf": "AV1",
        "av1_qsv": "AV1",
        "av1_vaapi": "AV1",
        "nvencc_av1": "AV1",
    }

    COL_SOURCE = 0
    COL_CHECK = 1
    COL_TYPE = 2
    COL_CODEC = 3
    COL_LANG = 4
    COL_TITLE = 5
    COL_INFO = 6
    COL_EDIT = 7

    _HEADERS = ["", "", "Type", "Codec", "Langue", "Titre", "Info", ""]

    _FLAG_RO = (
        Qt.ItemFlag.ItemIsEnabled
        | Qt.ItemFlag.ItemIsSelectable
        | Qt.ItemFlag.ItemIsDragEnabled
    )
    _FLAG_RW = _FLAG_RO | Qt.ItemFlag.ItemIsEditable

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, len(self._HEADERS), parent)
        self._filter_selected = False
        self._audio_sync_available = False
        self._auto_sync_cancelable_entry_ids: set[str] = set()
        self._sync_rewrite_enabled = False
        self._sync_rewrite_advanced_audio_enabled = False
        self._prev_lang: dict[int, str] = {}
        self._setup_ui()
        self._adjust_height()
        self.itemChanged.connect(self._on_item_changed)
        self.cellClicked.connect(self._on_cell_clicked)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

    def _setup_ui(self) -> None:
        self.setHorizontalHeaderLabels(self._HEADERS)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropOverwriteMode(False)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

        self.setShowGrid(False)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
        )

        hh = self.horizontalHeader()
        hh.setSectionResizeMode(self.COL_SOURCE, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_CHECK, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_TYPE, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_CODEC, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(self.COL_LANG, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_INFO, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(self.COL_TITLE, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(self.COL_EDIT, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(self.COL_SOURCE, 20)
        self.setColumnWidth(self.COL_CHECK, 32)
        self.setColumnWidth(self.COL_TYPE, 48)
        self.setColumnWidth(self.COL_LANG, 70)
        self.setColumnWidth(self.COL_EDIT, 82)

        self.setItemDelegateForColumn(self.COL_INFO, _TrackInfoDelegate(self))

        mono = QFont("JetBrains Mono", 10)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(mono)

        self.setStyleSheet(f"""
            QTableWidget {{
                background: {_C.BG_CARD};
                alternate-background-color: {_C.BG_DEEP};
                color: {_C.TEXT_PRI};
                border: 1px solid {_C.BORDER};
                border-radius: 6px;
                gridline-color: transparent;
            }}
            QTableWidget::item {{
                padding: 4px 6px;
                border: none;
            }}
            QTableWidget::item:selected {{
                background: {_C.ACCENT_DIM};
                color: {_C.TEXT_PRI};
            }}
            QHeaderView::section {{
                background: {_C.BG_PANEL};
                color: {_C.TEXT_DIM};
                font-size: 9px;
                font-weight: 700;
                letter-spacing: 1px;
                border: none;
                border-bottom: 1px solid {_C.BORDER};
                padding: 4px 6px;
            }}
            QScrollBar:vertical {{
                background: {_C.BG_DEEP};
                width: 6px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {_C.BORDER_LT};
                border-radius: 3px;
                min-height: 20px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        """)

    def append_tracks(self, source_color: str, tracks: list[TrackEntry]) -> None:
        self.blockSignals(True)
        for entry in tracks:
            if self.has_entry_id(entry.entry_id):
                continue
            order = {"video": 0, "audio": 1, "subtitle": 2}.get(entry.track_type, 2)
            pos = self._find_insert_position(order)
            self.insertRow(pos)
            self._fill_row(pos, entry, source_color)
        self.blockSignals(False)
        self._adjust_height()

    def has_entry_id(self, entry_id: str) -> bool:
        for row in range(self.rowCount()):
            item = self.item(row, self.COL_CHECK)
            if item is None:
                continue
            entry = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(entry, TrackEntry) and entry.entry_id == entry_id:
                return True
        return False

    @staticmethod
    def _row_type_order(data) -> int:
        if isinstance(data, TrackEntry):
            return {"video": 0, "audio": 1, "subtitle": 2}.get(data.track_type, 2)
        return 3

    def _find_insert_position(self, order: int) -> int:
        for row in range(self.rowCount()):
            item = self.item(row, self.COL_CHECK)
            if item is None:
                continue
            data = item.data(Qt.ItemDataRole.UserRole)
            if data is None:
                continue
            if self._row_type_order(data) > order:
                return row
        return self.rowCount()

    def _adjust_height(self) -> None:
        n = self.rowCount()
        row_h = self.rowHeight(0) if n > 0 else self._ROW_H_DEFAULT
        header_h = self.horizontalHeader().height()
        visible = min(n, self._MAX_VISIBLE_ROWS)
        h = visible * row_h + header_h + 4 if n > 0 else 80 + header_h
        self.setFixedHeight(h)

    def remove_tracks_by_file_id(self, file_id: str) -> None:
        self.blockSignals(True)
        row = self.rowCount() - 1
        while row >= 0:
            item = self.item(row, self.COL_CHECK)
            if item is not None:
                data = item.data(Qt.ItemDataRole.UserRole)
                if data is not None and getattr(data, "file_id", None) == file_id:
                    entry_id = str(getattr(data, "entry_id", "") or "")
                    if entry_id:
                        self._auto_sync_cancelable_entry_ids.discard(entry_id)
                    self.removeRow(row)
            row -= 1
        self.blockSignals(False)
        self._rebuild_prev_lang()
        self._adjust_height()

    def remove_track_by_entry_id(self, entry_id: str) -> bool:
        if not entry_id:
            return False
        self.blockSignals(True)
        try:
            for row in range(self.rowCount() - 1, -1, -1):
                item = self.item(row, self.COL_CHECK)
                if item is None:
                    continue
                entry = item.data(Qt.ItemDataRole.UserRole)
                if isinstance(entry, TrackEntry) and entry.entry_id == entry_id:
                    self._auto_sync_cancelable_entry_ids.discard(entry.entry_id)
                    self.removeRow(row)
                    return True
        finally:
            self.blockSignals(False)
            self._rebuild_prev_lang()
            self._adjust_height()
        return False

    def clear_all(self) -> None:
        self.setRowCount(0)
        self._prev_lang.clear()
        self._auto_sync_cancelable_entry_ids.clear()
        self._adjust_height()

    def _rebuild_prev_lang(self) -> None:
        self._prev_lang.clear()
        for row in range(self.rowCount()):
            lang_item = self.item(row, self.COL_LANG)
            if lang_item is not None:
                self._prev_lang[row] = lang_item.text()

    def _fill_row(self, row: int, entry: TrackEntry, source_color: str) -> None:
        self._update_entry_sync_rewrite_label(entry)
        src_item = QTableWidgetItem("█")
        src_item.setFlags(self._FLAG_RO & ~Qt.ItemFlag.ItemIsDragEnabled)
        src_item.setForeground(QColor(source_color))
        src_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        src_item.setFont(QFont("Arial", 11))
        src_item.setData(Qt.ItemDataRole.UserRole, source_color)
        self.setItem(row, self.COL_SOURCE, src_item)

        chk = QTableWidgetItem()
        chk.setData(Qt.ItemDataRole.UserRole, entry)
        chk.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsDragEnabled
        )
        chk.setCheckState(Qt.CheckState.Checked if entry.enabled else Qt.CheckState.Unchecked)
        chk.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setItem(row, self.COL_CHECK, chk)

        type_item = QTableWidgetItem(entry.type_label)
        type_item.setFlags(self._FLAG_RO)
        type_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        match entry.track_type:
            case "video":
                type_item.setForeground(QColor(_C.TRACK_VIDEO))
            case "audio":
                type_item.setForeground(QColor(_C.TRACK_AUDIO))
            case "subtitle":
                type_item.setForeground(QColor(_C.TRACK_SUBTITLE))
        self.setItem(row, self.COL_TYPE, type_item)

        codec_item = QTableWidgetItem(entry.codec)
        codec_item.setFlags(self._FLAG_RO)
        self.setItem(row, self.COL_CODEC, codec_item)

        lang_item = QTableWidgetItem(entry.language)
        lang_item.setFlags(self._FLAG_RW)
        self._prev_lang[row] = entry.language
        self.setItem(row, self.COL_LANG, lang_item)

        info_item = QTableWidgetItem(entry.full_info_label)
        info_item.setFlags(self._FLAG_RO)
        info_item.setForeground(QColor(_C.TEXT_SEC))
        info_item.setData(_TRACK_INFO_OFFSET_VALUE_ROLE, entry.time_shift_value_label)
        info_item.setData(
            _TRACK_INFO_SYNC_LABEL_ROLE,
            entry.sync_rewrite_label if self._can_toggle_sync_rewrite(entry) else "",
        )
        self.setItem(row, self.COL_INFO, info_item)
        self._update_info_tooltip(row, entry)

        title_item = QTableWidgetItem(entry.title)
        title_item.setFlags(self._FLAG_RW)
        self.setItem(row, self.COL_TITLE, title_item)

        if entry.is_new:
            self._apply_new_track_style(row)
        self._apply_video_encode_style(row, entry)

        self._set_action_cell(row, entry)

    @staticmethod
    def _small_action_button_style() -> str:
        return f"""
            QPushButton {{
                background: transparent;
                border: 1px solid {_C.BORDER};
                border-radius: 4px;
                padding: 0;
            }}
            QPushButton:hover {{
                border-color: {_C.ACCENT};
                background: {_C.ACCENT_DIM};
            }}
            QPushButton:pressed {{
                background: {_C.BG_ACTIVE};
            }}
        """

    def _make_action_button(self, *, tooltip: str, icon) -> QPushButton:
        btn = QPushButton()
        btn.setIcon(icon)
        btn.setIconSize(QSize(13, 13))
        btn.setFixedSize(22, 22)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(tooltip)
        btn.setStyleSheet(self._small_action_button_style())
        return btn

    def _set_action_cell(self, row: int, entry: TrackEntry) -> None:
        container = QWidget()
        container.setAutoFillBackground(False)
        container.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        container.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        layout.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        preview = self._sync_rewrite_preview(entry)
        if preview.is_advanced and preview.warning_tooltip and not sync_rewrite_forced_offset(entry):
            warning_btn = self._make_action_button(
                tooltip=translate_text(preview.warning_tooltip),
                icon=_warning_icon("#f0b429", 13),
            )
            warning_btn.setCursor(Qt.CursorShape.ArrowCursor)
            warning_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            layout.addWidget(warning_btn)

        if self._has_cancelable_auto_sync(entry):
            cancel_btn = self._make_action_button(
                tooltip=translate_text("Annuler la synchro"),
                icon=_x_icon(_C.ERROR, 13),
            )
            cancel_btn.clicked.connect(
                lambda _=None, e=entry: self.auto_sync_cancel_requested.emit(e)
            )
            layout.addWidget(cancel_btn)
        elif entry.track_type == "audio" and self._audio_sync_available:
            sync_btn = self._make_action_button(
                tooltip="Synchronisation améliorée de cette piste audio",
                icon=_refresh_icon(_C.TEXT_SEC, 13),
            )
            sync_btn.clicked.connect(lambda _=None, e=entry: self.audio_sync_requested.emit(e))
            layout.addWidget(sync_btn)

        edit_btn = self._make_action_button(
            tooltip="Éditer les métadonnées de cette piste",
            icon=_pencil_icon(_C.TEXT_SEC, 13),
        )
        edit_btn.clicked.connect(lambda _=None, e=entry: self._open_edit_dialog(e))
        layout.addWidget(edit_btn)
        self.setCellWidget(row, self.COL_EDIT, container)

    def _has_cancelable_auto_sync(self, entry: TrackEntry) -> bool:
        if entry.track_type not in {"audio", "subtitle"}:
            return False
        if entry.entry_id not in self._auto_sync_cancelable_entry_ids:
            return False
        return int(entry.time_shift_ms or 0) != 0

    def _sync_rewrite_preview(self, entry: TrackEntry):
        return ui_sync_rewrite_preview_for_track(
            entry,
            enabled=self._sync_rewrite_enabled,
            advanced_audio_enabled=self._sync_rewrite_advanced_audio_enabled,
        )

    def _can_toggle_sync_rewrite(self, entry: TrackEntry) -> bool:
        return ui_sync_rewrite_can_toggle(
            entry,
            enabled=self._sync_rewrite_enabled,
            advanced_audio_enabled=self._sync_rewrite_advanced_audio_enabled,
        )

    def _update_entry_sync_rewrite_label(self, entry: TrackEntry) -> None:
        entry.sync_rewrite_label = ui_sync_rewrite_label_for_track(
            entry,
            enabled=self._sync_rewrite_enabled,
            advanced_audio_enabled=self._sync_rewrite_advanced_audio_enabled,
        )

    def _update_info_tooltip(self, row: int, entry: TrackEntry) -> None:
        info_item = self.item(row, self.COL_INFO)
        if info_item is None:
            return
        info_item.setToolTip(
            translate_text("Cliquer pour basculer entre sync réelle et sync offset")
            if self._can_toggle_sync_rewrite(entry)
            else ""
        )

    def _refresh_info_cell(self, row: int, entry: TrackEntry) -> None:
        self._update_entry_sync_rewrite_label(entry)
        info_item = self.item(row, self.COL_INFO)
        if info_item:
            info_item.setText(entry.full_info_label)
            info_item.setData(_TRACK_INFO_OFFSET_VALUE_ROLE, entry.time_shift_value_label)
            info_item.setData(
                _TRACK_INFO_SYNC_LABEL_ROLE,
                entry.sync_rewrite_label if self._can_toggle_sync_rewrite(entry) else "",
            )
        self._update_info_tooltip(row, entry)

    def _apply_new_track_style(self, row: int) -> None:
        for col in (self.COL_CODEC, self.COL_LANG, self.COL_TITLE, self.COL_INFO):
            item = self.item(row, col)
            if item is None:
                continue
            item.setForeground(self._NEW_TRACK_COLOR)
            font = item.font()
            font.setBold(True)
            item.setFont(font)

    def _apply_video_encode_style(self, row: int, entry: TrackEntry) -> None:
        codec_item = self.item(row, self.COL_CODEC)
        if entry.track_type != "video":
            if codec_item is not None:
                codec_item.setText(entry.codec)
            return

        override_codec = str(entry.encode_plan_codec or "").strip().lower()
        has_encode_override = bool(override_codec and override_codec != "copy")
        display_codec = entry.orig_codec or entry.codec
        if has_encode_override:
            display_codec = self._VIDEO_CODEC_SHORT_LABELS.get(
                override_codec,
                override_codec.upper(),
            )
        if codec_item is not None:
            codec_item.setText(display_codec)

        highlight = has_encode_override
        color = self._VIDEO_ENCODE_COLOR if highlight else None
        columns = (self.COL_TYPE, self.COL_CODEC, self.COL_LANG, self.COL_TITLE, self.COL_INFO)
        for col in columns:
            item = self.item(row, col)
            if item is None:
                continue
            font = item.font()
            font.setBold(highlight)
            item.setFont(font)
            if color is None:
                if col == self.COL_TYPE:
                    item.setForeground(QColor(_C.TRACK_VIDEO))
                elif col == self.COL_INFO:
                    item.setForeground(QColor(_C.TEXT_SEC))
                else:
                    item.setForeground(QColor(_C.TEXT_PRI))
            else:
                # Ligne encodée mise en avant en bleu, mais codec explicite en rouge.
                if col == self.COL_CODEC:
                    item.setForeground(self._VIDEO_ENCODE_CODEC_COLOR)
                else:
                    item.setForeground(color)

    def current_tracks(self) -> list[TrackEntry]:
        tracks: list[TrackEntry] = []
        for row in range(self.rowCount()):
            item0 = self.item(row, self.COL_CHECK)
            if item0 is None:
                continue
            entry = item0.data(Qt.ItemDataRole.UserRole)
            if not isinstance(entry, TrackEntry):
                continue

            entry.enabled = item0.checkState() == Qt.CheckState.Checked
            lang_item = self.item(row, self.COL_LANG)
            if lang_item:
                entry.language = lang_item.text().strip()
            title_item = self.item(row, self.COL_TITLE)
            if title_item:
                entry.title = title_item.text().strip()
            tracks.append(entry)
        return tracks

    def _open_edit_dialog(self, entry: TrackEntry) -> None:
        dlg = TrackEditDialog(entry, parent=self)
        if dlg.exec() == TrackEditDialog.DialogCode.Accepted:
            row = self._find_row_for_entry(entry)
            if row is not None:
                self.blockSignals(True)
                lang_item = self.item(row, self.COL_LANG)
                if lang_item:
                    lang_item.setText(entry.language)
                title_item = self.item(row, self.COL_TITLE)
                if title_item:
                    title_item.setText(entry.title)
                self._refresh_info_cell(row, entry)
                self._set_action_cell(row, entry)
                self.blockSignals(False)
                if lang_item is not None:
                    self.itemChanged.emit(lang_item)

    def update_audio_meta(
        self,
        file_id: str,
        mkv_tid: int,
        lang: str,
        title: str,
        *,
        entry_id: str | None = None,
    ) -> None:
        self.blockSignals(True)
        try:
            for row in range(self.rowCount()):
                item0 = self.item(row, self.COL_CHECK)
                if item0 is None:
                    continue
                entry = item0.data(Qt.ItemDataRole.UserRole)
                if not isinstance(entry, TrackEntry):
                    continue
                if entry_id:
                    if entry.entry_id != entry_id:
                        continue
                elif entry.file_id != file_id or entry.mkv_tid != mkv_tid:
                    continue
                if entry.file_id == file_id and entry.mkv_tid == mkv_tid:
                    lang_item = self.item(row, self.COL_LANG)
                    if lang_item:
                        lang_item.setText(lang)
                        self._prev_lang[row] = lang
                    title_item = self.item(row, self.COL_TITLE)
                    if title_item:
                        title_item.setText(title)
                    entry.language = lang
                    entry.title = title
                    self._refresh_info_cell(row, entry)
                    self._set_action_cell(row, entry)
                    break
        finally:
            self.blockSignals(False)

    def update_audio_encoding(
        self,
        entry_id: str,
        codec: str,
        display_info: str,
    ) -> bool:
        if not entry_id:
            return False
        self.blockSignals(True)
        try:
            for row in range(self.rowCount()):
                item0 = self.item(row, self.COL_CHECK)
                if item0 is None:
                    continue
                entry = item0.data(Qt.ItemDataRole.UserRole)
                if not isinstance(entry, TrackEntry) or entry.entry_id != entry_id:
                    continue

                entry.codec = codec
                entry.display_info = display_info
                codec_item = self.item(row, self.COL_CODEC)
                if codec_item:
                    codec_item.setText(codec)
                self._refresh_info_cell(row, entry)
                self._set_action_cell(row, entry)
                if entry.is_new:
                    self._apply_new_track_style(row)
                return True
        finally:
            self.blockSignals(False)
        return False

    def update_time_shift(self, entry_id: str, offset_ms: int) -> bool:
        if not entry_id:
            return False
        self.blockSignals(True)
        try:
            for row in range(self.rowCount()):
                item0 = self.item(row, self.COL_CHECK)
                if item0 is None:
                    continue
                entry = item0.data(Qt.ItemDataRole.UserRole)
                if not isinstance(entry, TrackEntry) or entry.entry_id != entry_id:
                    continue

                entry.time_shift_ms = int(offset_ms)
                self._refresh_info_cell(row, entry)
                if entry.track_type in {"audio", "subtitle"}:
                    self._set_action_cell(row, entry)
                return True
        finally:
            self.blockSignals(False)
        return False

    def set_sync_rewrite_enabled(self, enabled: bool, *, advanced_audio_enabled: bool | None = None) -> None:
        self._sync_rewrite_enabled = bool(enabled)
        if advanced_audio_enabled is not None:
            self._sync_rewrite_advanced_audio_enabled = bool(advanced_audio_enabled)
        self.blockSignals(True)
        try:
            for row in range(self.rowCount()):
                item0 = self.item(row, self.COL_CHECK)
                if item0 is None:
                    continue
                entry = item0.data(Qt.ItemDataRole.UserRole)
                if not isinstance(entry, TrackEntry):
                    continue
                self._refresh_info_cell(row, entry)
                self._set_action_cell(row, entry)
        finally:
            self.blockSignals(False)

    def set_sync_rewrite_advanced_audio_enabled(self, enabled: bool) -> None:
        self.set_sync_rewrite_enabled(
            self._sync_rewrite_enabled,
            advanced_audio_enabled=enabled,
        )

    def refresh_entry_info(self, entry_id: str) -> bool:
        if not entry_id:
            return False
        self.blockSignals(True)
        try:
            for row in range(self.rowCount()):
                item0 = self.item(row, self.COL_CHECK)
                if item0 is None:
                    continue
                entry = item0.data(Qt.ItemDataRole.UserRole)
                if not isinstance(entry, TrackEntry) or entry.entry_id != entry_id:
                    continue
                self._refresh_info_cell(row, entry)
                self._set_action_cell(row, entry)
                return True
        finally:
            self.blockSignals(False)
        return False

    def set_audio_sync_available(self, available: bool) -> None:
        available = bool(available)
        if self._audio_sync_available == available:
            return
        self._audio_sync_available = available
        for row in range(self.rowCount()):
            item0 = self.item(row, self.COL_CHECK)
            if item0 is None:
                continue
            entry = item0.data(Qt.ItemDataRole.UserRole)
            if isinstance(entry, TrackEntry):
                self._set_action_cell(row, entry)

    def set_auto_sync_cancelable_entries(
        self,
        entry_ids: set[str] | list[str] | tuple[str, ...],
    ) -> None:
        normalized = {str(entry_id) for entry_id in entry_ids if str(entry_id or "").strip()}
        if self._auto_sync_cancelable_entry_ids == normalized:
            return
        self._auto_sync_cancelable_entry_ids = normalized
        for row in range(self.rowCount()):
            item0 = self.item(row, self.COL_CHECK)
            if item0 is None:
                continue
            entry = item0.data(Qt.ItemDataRole.UserRole)
            if isinstance(entry, TrackEntry):
                self._set_action_cell(row, entry)

    def update_video_encoding_plans(
        self,
        plans: dict[str, str],
        *,
        clear_missing: bool = False,
    ) -> bool:
        changed = False
        self.blockSignals(True)
        try:
            for row in range(self.rowCount()):
                item0 = self.item(row, self.COL_CHECK)
                if item0 is None:
                    continue
                entry = item0.data(Qt.ItemDataRole.UserRole)
                if not isinstance(entry, TrackEntry) or entry.track_type != "video":
                    continue
                target_codec = str(plans.get(entry.entry_id, "") or "").strip().lower()
                if not target_codec and not clear_missing:
                    continue
                modified = bool(target_codec and target_codec != "copy")
                if entry.encode_plan_codec == target_codec and entry.encode_plan_modified == modified:
                    continue
                entry.encode_plan_codec = target_codec
                entry.encode_plan_summary = ""
                entry.encode_plan_hdr_badges = ()
                entry.encode_plan_modified = modified
                self._apply_video_encode_style(row, entry)
                changed = True
        finally:
            self.blockSignals(False)
        return changed

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != self.COL_LANG:
            return
        if not Rfc5646LanguageTags.validate_item(item, self._prev_lang):
            prev = self._prev_lang.get(item.row(), "")
            self.blockSignals(True)
            item.setText(prev)
            self.blockSignals(False)
            QTimer.singleShot(
                0,
                lambda: QMessageBox.warning(
                    self,
                    translate_text("Erreur"),
                    translate_text("Erreur : code langue non reconnu"),
                ),
            )

    def _on_cell_clicked(self, row: int, column: int) -> None:
        if column != self.COL_INFO:
            return
        item0 = self.item(row, self.COL_CHECK)
        if item0 is None:
            return
        entry = item0.data(Qt.ItemDataRole.UserRole)
        if isinstance(entry, TrackEntry) and self._can_toggle_sync_rewrite(entry):
            self.sync_rewrite_toggle_requested.emit(entry)

    def _on_context_menu(self, pos) -> None:
        index = self.indexAt(pos)
        if not index.isValid():
            return
        chk = self.item(index.row(), self.COL_CHECK)
        if chk is None:
            return
        entry = chk.data(Qt.ItemDataRole.UserRole)
        if not isinstance(entry, TrackEntry) or entry.track_type != "subtitle":
            return

        menu = QMenu(self)
        action = menu.addAction(translate_text("Extraire…"))
        chosen = menu.exec(self.viewport().mapToGlobal(pos))
        if chosen is action:
            self.extract_requested.emit(entry)

    def _find_row_for_entry(self, entry: TrackEntry) -> int | None:
        for row in range(self.rowCount()):
            item = self.item(row, self.COL_CHECK)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) is entry:
                return row
        return None

    def set_all_enabled(self, enabled: bool) -> None:
        self.blockSignals(True)
        state = Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked
        for row in range(self.rowCount()):
            item = self.item(row, self.COL_CHECK)
            if item:
                item.setCheckState(state)
        self.blockSignals(False)

    def set_filter_selected(self, enabled: bool) -> None:
        self._filter_selected = enabled
        self.refresh_filter()

    def refresh_filter(self) -> None:
        for row in range(self.rowCount()):
            item = self.item(row, self.COL_CHECK)
            if item is None:
                self.setRowHidden(row, False)
                continue
            hidden = self._filter_selected and item.checkState() != Qt.CheckState.Checked
            self.setRowHidden(row, hidden)

    def dropEvent(self, event) -> None:
        if event.source() is not self:
            event.ignore()
            return

        src_rows = sorted(set(idx.row() for idx in self.selectedIndexes()))
        if not src_rows:
            event.ignore()
            return

        all_entries = self.current_tracks()
        drop_row = self._drop_target_row(event)

        moving = [all_entries[r] for r in src_rows]
        remaining = [e for i, e in enumerate(all_entries) if i not in src_rows]

        adjusted = drop_row
        for r in src_rows:
            if r < drop_row:
                adjusted -= 1
        adjusted = max(0, min(adjusted, len(remaining)))

        for i, entry in enumerate(moving):
            remaining.insert(adjusted + i, entry)

        color_by_file_id: dict[str, str] = {}
        for r in range(self.rowCount()):
            item_chk = self.item(r, self.COL_CHECK)
            item_src = self.item(r, self.COL_SOURCE)
            if item_chk and item_src:
                e = item_chk.data(Qt.ItemDataRole.UserRole)
                if isinstance(e, TrackEntry):
                    color_by_file_id[e.file_id] = item_src.data(Qt.ItemDataRole.UserRole) or _C.BORDER

        self.blockSignals(True)
        self.setRowCount(0)
        for entry in remaining:
            row = self.rowCount()
            self.insertRow(row)
            src_color = color_by_file_id.get(entry.file_id, _C.BORDER)
            self._fill_row(row, entry, src_color)
        self.blockSignals(False)

        self.selectRow(adjusted)
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()
        self._adjust_height()
        self.order_changed.emit()

    def _drop_target_row(self, event) -> int:
        index = self.indexAt(event.position().toPoint())
        if not index.isValid():
            return self.rowCount()
        row = index.row()
        rect = self.visualRect(index)
        if event.position().toPoint().y() > rect.top() + rect.height() // 2:
            return row + 1
        return row


__all__ = ["_TrackInfoDelegate", "_TrackTable"]
