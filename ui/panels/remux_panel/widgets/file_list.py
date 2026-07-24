"""Widgets de sélection de fichiers pour RemuxPanel."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.file_types import ACCEPTED_EXTENSIONS, build_qt_filter
from core.i18n import translate_text
from core.inspector import FileInfo
from ui.panels.remux_panel.models import _FILE_BAR_H, _FILE_PH_H, _FILE_ROW_H, SourceFile
from ui.panels.remux_panel.theme import _C
from ui.design_system import font_px as _font_px, scale as _scale


_ACCEPTED_EXT = ACCEPTED_EXTENSIONS


class _FileRow(QWidget):
    remove_clicked = Signal(str)

    def __init__(self, file_id: str, path: Path, color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._file_id = file_id
        self.setFixedHeight(_scale(_FILE_ROW_H))
        self._build_ui(path, color)

    def _build_ui(self, path: Path, color: str) -> None:
        self.setStyleSheet(f"""
            _FileRow {{
                background: {_C.BG_CARD};
                border-bottom: 1px solid {_C.BORDER};
            }}
        """)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(_scale(12), _scale(8), _scale(8), _scale(8))
        lay.setSpacing(_scale(10))

        color_square = QLabel()
        color_square.setFixedSize(_scale(12), _scale(12))
        color_square.setStyleSheet(
            f"background: {color}; border-radius: {_scale(3)}px; border: none;"
        )
        lay.addWidget(color_square)

        text_col = QVBoxLayout()
        text_col.setSpacing(_scale(2))

        self._name_lbl = QLabel(path.name)
        self._name_lbl.setStyleSheet(f"""
            color: {_C.TEXT_PRI};
            font-size: {_font_px(12)}px;
            font-weight: 600;
            background: transparent;
            border: none;
        """)

        self._info_lbl = QLabel("Inspection en cours…")
        self._info_lbl.setStyleSheet(f"""
            color: {_C.TEXT_DIM};
            font-size: {_font_px(10)}px;
            font-family: 'JetBrains Mono', monospace;
            background: transparent;
            border: none;
        """)

        text_col.addWidget(self._name_lbl)
        text_col.addWidget(self._info_lbl)
        lay.addLayout(text_col, stretch=1)

        remove_btn = QPushButton("✕")
        remove_btn.setFixedSize(_scale(22), _scale(22))
        remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_btn.setToolTip("Retirer ce fichier")
        remove_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {_C.TEXT_DIM};
                border: 1px solid {_C.BORDER};
                border-radius: {_scale(4)}px;
                font-size: {_font_px(10)}px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                color: {_C.ERROR};
                border-color: {_C.ERROR};
                background: #1f0e0e;
            }}
            QPushButton:pressed {{ background: #2a0f0f; }}
        """)
        remove_btn.clicked.connect(lambda: self.remove_clicked.emit(self._file_id))
        lay.addWidget(remove_btn)

    def set_info(self, info: FileInfo) -> None:
        parts = [info.size_human, info.duration_human, info.format]
        if info.primary_video:
            parts.append(info.primary_video.resolution)
            if info.primary_video.bit_rate:
                parts.append(f"{info.primary_video.bit_rate // 1000} kbps")
            hdr_lbl = info.primary_video.hdr_label
            if hdr_lbl != "SDR":
                parts.append(hdr_lbl)
        self._info_lbl.setText("   ·   ".join(p for p in parts if p and p != "?"))
        self._info_lbl.setStyleSheet(f"""
            color: {_C.TEXT_SEC};
            font-size: {_font_px(10)}px;
            font-family: 'JetBrains Mono', monospace;
            background: transparent;
            border: none;
        """)

    def set_error(self, message: str) -> None:
        self._info_lbl.setText(translate_text("Erreur : {message}", message=message))
        self._info_lbl.setStyleSheet(f"""
            color: {_C.ERROR};
            font-size: {_font_px(10)}px;
            background: transparent;
            border: none;
        """)


class _FileListWidget(QFrame):
    add_requested = Signal(list)
    remove_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._rows: dict[str, _FileRow] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        self.setStyleSheet(f"""
            QFrame {{
                background: {_C.BG_CARD};
                border: 1px dashed {_C.BORDER_LT};
                border-radius: {_scale(8)}px;
            }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self._scroll.setVisible(False)

        self._rows_container = QWidget()
        self._rows_container.setStyleSheet("background: transparent;")
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)
        self._rows_layout.addStretch()

        self._scroll.setWidget(self._rows_container)
        # Dès qu'une source est présente, le viewport du QScrollArea recouvre
        # la zone de dépôt. Sans relais explicite, il accepte le drag-enter
        # mais perd le drop, ce qui empêche d'ajouter une seconde source tant
        # que la première est affichée dans la liste.
        for target in (self._scroll, self._scroll.viewport(), self._rows_container):
            target.setAcceptDrops(True)
            target.installEventFilter(self)
        root.addWidget(self._scroll, stretch=1)

        self._placeholder = QWidget()
        self._placeholder.setStyleSheet("background: transparent;")
        ph_lay = QVBoxLayout(self._placeholder)
        ph_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ph_lay.setSpacing(_scale(6))

        ph_icon = QLabel("⊞")
        ph_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ph_icon.setStyleSheet(
            f"font-size: {_font_px(28)}px; color: {_C.TEXT_DIM}; background: transparent; border: none;"
        )
        ph_lay.addWidget(ph_icon)

        ph_text = QLabel("Déposer des fichiers vidéo / sous-titres ici")
        ph_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ph_text.setStyleSheet(
            f"color: {_C.TEXT_SEC}; font-size: {_font_px(12)}px; font-weight: 500; background: transparent; border: none;"
        )
        ph_lay.addWidget(ph_text)

        ph_sub = QLabel("ou cliquer sur « Ajouter des fichiers »")
        ph_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ph_sub.setStyleSheet(
            f"color: {_C.TEXT_DIM}; font-size: {_font_px(10)}px; background: transparent; border: none;"
        )
        ph_lay.addWidget(ph_sub)

        root.addWidget(self._placeholder, stretch=1)

        add_bar = QWidget()
        add_bar.setStyleSheet(f"""
            QWidget {{
                background: {_C.BG_DEEP};
                border-top: 1px solid {_C.BORDER};
                border-bottom-left-radius: {_scale(8)}px;
                border-bottom-right-radius: {_scale(8)}px;
            }}
        """)
        add_bar.setFixedHeight(_scale(36))
        add_bar_lay = QHBoxLayout(add_bar)
        add_bar_lay.setContentsMargins(_scale(12), 0, _scale(12), 0)
        add_bar_lay.setSpacing(0)

        add_btn = QPushButton("+ Ajouter des fichiers…")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {_C.ACCENT};
                border: none;
                font-size: {_font_px(11)}px;
                font-weight: 600;
            }}
            QPushButton:hover {{ color: #8090ff; }}
        """)
        add_btn.clicked.connect(self._browse)
        add_bar_lay.addWidget(add_btn)
        add_bar_lay.addStretch()

        root.addWidget(add_bar)
        self._update_visibility()

    def add_file(self, sf: SourceFile) -> None:
        row = _FileRow(sf.id, sf.path, sf.color)
        row.remove_clicked.connect(self.remove_requested)
        self._rows[sf.id] = row

        count = self._rows_layout.count()
        self._rows_layout.insertWidget(count - 1, row)
        self._update_visibility()

    def update_file(self, sf: SourceFile) -> None:
        row = self._rows.get(sf.id)
        if row and sf.info:
            row.set_info(sf.info)

    def set_file_error(self, file_id: str, message: str) -> None:
        row = self._rows.get(file_id)
        if row:
            row.set_error(message)

    def remove_file(self, file_id: str) -> None:
        row = self._rows.pop(file_id, None)
        if row:
            self._rows_layout.removeWidget(row)
            row.deleteLater()
        self._update_visibility()

    def file_count(self) -> int:
        return len(self._rows)

    def _update_visibility(self) -> None:
        has_files = bool(self._rows)
        self._scroll.setVisible(has_files)
        self._placeholder.setVisible(not has_files)
        n = len(self._rows)
        h = (_scale(_FILE_ROW_H) * n + _scale(_FILE_BAR_H)) if has_files else (_scale(_FILE_PH_H) + _scale(_FILE_BAR_H))
        self.setFixedHeight(h)

    def _browse(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            translate_text("Sélectionner des fichiers source"),
            "",
            build_qt_filter(),
        )
        if paths:
            self.add_requested.emit(paths)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        self._accept_local_drop(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self._emit_local_drop(event)

    @staticmethod
    def _local_drop_paths(event) -> list[str]:
        mime = getattr(event, "mimeData", lambda: None)()
        if mime is None or not mime.hasUrls():
            return []
        return [
            url.toLocalFile()
            for url in mime.urls()
            if url.isLocalFile() and Path(url.toLocalFile()).exists()
        ]

    def _accept_local_drop(self, event) -> None:
        if self._local_drop_paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def _emit_local_drop(self, event) -> None:
        paths = self._local_drop_paths(event)
        if not paths:
            event.ignore()
            return
        # La validation détaillée (médias, pièces jointes et dossiers) relève
        # du RemuxPanel. Ici, ne jamais lancer discover_titles() : cette sonde
        # peut parcourir un disque Blu-ray et bloquait le glisser-déposer.
        self.add_requested.emit(paths)
        event.acceptProposedAction()

    def eventFilter(self, watched, event) -> bool:
        if watched in (self._scroll, self._scroll.viewport(), self._rows_container):
            if event.type() in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
                self._accept_local_drop(event)
                return True
            if event.type() == QEvent.Type.Drop:
                self._emit_local_drop(event)
                return True
        return super().eventFilter(watched, event)


__all__ = ["_FileListWidget"]
