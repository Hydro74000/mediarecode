"""Widgets pièces jointes/balises pour RemuxPanel."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Sequence
from xml.dom import minidom

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QCursor, QFont, QMouseEvent, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.config import AppConfig
from core.i18n import apply_translations, translate_text
from core.inspector import AttachmentInfo, STANDARD_MKV_TAGS
from core.media_info_fetcher import MediaDetails
from ui.panels.remux_panel.theme import _C
from ui.panels.tmdb_search_modal import TmdbSearchModal
from ui.design_system import font_px as _font_px, scale as _scale

class _TagEditDialog(QDialog):
    """
    Dialogue d'édition des balises MKV globales d'un fichier source.

    Affiche les balises existantes (tag name figé + value éditable) et permet
    d'en ajouter via un bouton « + Ajouter ».
    """

    def __init__(self, tags: dict[str, str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Éditer les balises")
        self.setMinimumWidth(_scale(580))
        self.setMinimumHeight(_scale(320))
        self.setStyleSheet(f"""
            QDialog {{ background: {_C.BG_DEEP}; color: {_C.TEXT_PRI}; }}
            QLabel  {{ color: {_C.TEXT_PRI}; background: transparent; border: none; font-size: {_font_px(11)}px; }}
        """)
        # Liste mutable (name_widget, value_edit, row_widget)
        self._rows: list[tuple[QComboBox | QLabel, QLineEdit, QWidget]] = []
        self._build_ui(tags)
        apply_translations(self)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_ui(self, tags: dict[str, str]) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(_scale(14), _scale(14), _scale(14), _scale(14))
        root.setSpacing(_scale(8))

        # Scroll pour les lignes de tags
        self._rows_widget = QWidget()
        self._rows_widget.setStyleSheet("background: transparent;")
        self._rows_layout = QVBoxLayout(self._rows_widget)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(_scale(4))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(
            f"QScrollArea {{ background: {_C.BG_CARD}; border: 1px solid {_C.BORDER}; border-radius: {_scale(4)}px; }}"
        )
        scroll.setWidget(self._rows_widget)
        root.addWidget(scroll, stretch=1)

        # Tags existants
        for name, value in tags.items():
            self._add_existing_row(name, value)

        # Bouton Ajouter
        add_btn = QPushButton("+ Ajouter un tag")
        add_btn.setFixedHeight(_scale(26))
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {_C.ACCENT};
                border: 1px solid {_C.ACCENT_DIM};
                border-radius: {_scale(4)}px;
                font-size: {_font_px(10)}px;
                font-weight: 600;
                padding: 0 {_scale(12)}px;
            }}
            QPushButton:hover {{ background: {_C.ACCENT_DIM}; color: #fff; }}
        """)
        add_btn.clicked.connect(self._add_new_row)
        root.addWidget(add_btn)

        # OK / Annuler
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        btns.setStyleSheet(f"""
            QPushButton {{
                background: {_C.BG_PANEL};
                color: {_C.TEXT_PRI};
                border: 1px solid {_C.BORDER_LT};
                border-radius: {_scale(4)}px;
                font-size: {_font_px(11)}px;
                padding: {_scale(4)}px {_scale(16)}px;
            }}
            QPushButton:hover {{ background: {_C.BG_CARD}; }}
        """)
        root.addWidget(btns)

    def _row_stylesheet(self) -> str:
        return (
            f"QLineEdit {{ background: {_C.BG_DEEP}; color: {_C.TEXT_PRI}; "
            f"border: 1px solid {_C.BORDER}; border-radius: {_scale(3)}px; font-size: {_font_px(11)}px; padding: {_scale(2)}px {_scale(6)}px; }}"
            f"QLineEdit:focus {{ border-color: {_C.ACCENT}; }}"
        )

    def _add_existing_row(self, name: str, value: str) -> None:
        """Ajoute une ligne avec tag name en label fixe + value éditable."""
        row = QWidget()
        row.setStyleSheet("background: transparent;")
        h = QHBoxLayout(row)
        h.setContentsMargins(_scale(4), _scale(2), _scale(4), _scale(2))
        h.setSpacing(_scale(8))

        name_lbl = QLabel(name)
        name_lbl.setFixedWidth(_scale(160))
        name_lbl.setStyleSheet(
            f"color: {_C.TEXT_DIM}; font-size: {_font_px(10)}px; font-weight: 600; "
            "background: transparent; border: none;"
        )
        h.addWidget(name_lbl)

        val_edit = QLineEdit(value)
        val_edit.setStyleSheet(self._row_stylesheet())
        h.addWidget(val_edit, stretch=1)

        rm_btn = self._make_remove_btn(row, name_lbl, val_edit)
        h.addWidget(rm_btn)

        self._rows_layout.addWidget(row)
        self._rows.append((name_lbl, val_edit, row))

    def _add_new_row(self) -> None:
        """Ajoute une ligne avec combobox (noms standards) + value éditable."""
        row = QWidget()
        row.setStyleSheet("background: transparent;")
        h = QHBoxLayout(row)
        h.setContentsMargins(_scale(4), _scale(2), _scale(4), _scale(2))
        h.setSpacing(_scale(8))

        name_combo = QComboBox()
        name_combo.setEditable(True)
        name_combo.setFixedWidth(_scale(160))
        sorted_tags = sorted((STANDARD_MKV_TAGS | {"COMMENTS"}) - {"TITLE"})
        name_combo.addItems(sorted_tags)
        name_combo.setCurrentText("")
        name_combo.setStyleSheet(f"""
            QComboBox {{
                background: {_C.BG_DEEP};
                color: {_C.TEXT_PRI};
                border: 1px solid {_C.BORDER};
                border-radius: {_scale(3)}px;
                font-size: {_font_px(11)}px;
                padding: {_scale(2)}px {_scale(6)}px;
            }}
            QComboBox:focus {{ border-color: {_C.ACCENT}; }}
            QComboBox QAbstractItemView {{
                background: {_C.BG_PANEL};
                color: {_C.TEXT_PRI};
                selection-background-color: {_C.ACCENT_DIM};
            }}
        """)
        h.addWidget(name_combo)

        val_edit = QLineEdit()
        val_edit.setPlaceholderText("valeur…")
        val_edit.setStyleSheet(self._row_stylesheet())
        h.addWidget(val_edit, stretch=1)

        rm_btn = self._make_remove_btn(row, name_combo, val_edit)
        h.addWidget(rm_btn)

        self._rows_layout.addWidget(row)
        self._rows.append((name_combo, val_edit, row))

    def _make_remove_btn(
        self, row: QWidget, name_w: QWidget, val_edit: QLineEdit
    ) -> QPushButton:
        btn = QPushButton("✕")
        btn.setFixedSize(_scale(20), _scale(20))
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {_C.TEXT_DIM};
                border: 1px solid {_C.BORDER}; border-radius: {_scale(3)}px;
                font-size: {_font_px(9)}px; font-weight: 700;
            }}
            QPushButton:hover {{ color: {_C.ERROR}; border-color: {_C.ERROR}; background: #1f0e0e; }}
        """)
        btn.clicked.connect(lambda: self._remove_row(row, name_w, val_edit))
        return btn

    def _remove_row(self, row: QWidget, name_w: QWidget, val_edit: QLineEdit) -> None:
        self._rows = [(n, v, r) for n, v, r in self._rows if r is not row]
        self._rows_layout.removeWidget(row)
        row.deleteLater()

    # ------------------------------------------------------------------
    # Résultat
    # ------------------------------------------------------------------

    def result_tags(self) -> dict[str, str]:
        """Retourne le dict tag_name → valeur après confirmation."""
        result: dict[str, str] = {}
        for name_w, val_edit, _row in self._rows:
            if isinstance(name_w, QComboBox):
                name = name_w.currentText().strip().upper()
            elif isinstance(name_w, QLabel):
                name = name_w.text().strip().upper()
            else:
                continue
            value = val_edit.text().strip()
            if name and value:
                result[name] = value
        return result


# =============================================================================

_IMAGE_ATTACHMENT_EXTENSIONS = {".jpg", ".jpeg", ".png"}
_TEXT_ATTACHMENT_EXTENSIONS = {".txt", ".xml", ".nfo"}

_TMDB_COVER_CACHE: dict[str, bytes] = {}


def _fetch_tmdb_cover_bytes(url: str) -> bytes | None:
    """Télécharge (et met en cache) les octets d'une cover TMDB depuis son URL."""
    if not url:
        return None
    cached = _TMDB_COVER_CACHE.get(url)
    if cached is not None:
        return cached
    import ssl
    import urllib.error
    import urllib.request

    from core.version import APP_USER_AGENT

    req = urllib.request.Request(url, headers={"User-Agent": APP_USER_AGENT, "Accept": "image/*,*/*;q=0.8"})
    try:
        try:
            resp_cm = urllib.request.urlopen(req, timeout=10)
        except urllib.error.URLError as exc:
            cur: BaseException | None = exc
            is_ssl = False
            while cur is not None:
                if isinstance(cur, ssl.SSLError):
                    is_ssl = True
                    break
                if isinstance(getattr(cur, "reason", None), ssl.SSLError):
                    is_ssl = True
                    break
                cur = cur.__cause__ or cur.__context__
            if not is_ssl:
                return None
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            resp_cm = urllib.request.urlopen(req, timeout=10, context=ctx)
        with resp_cm as resp:
            data = resp.read()
    except (urllib.error.URLError, OSError):
        return None
    if not data:
        return None
    _TMDB_COVER_CACHE[url] = data
    return data


def _resolved_manual_attachment_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    try:
        return path if path.is_file() else None
    except OSError:
        return None


def _is_image_attachment_path(path: Path | None) -> bool:
    if path is None:
        return False
    suffix = path.suffix.lower()
    if suffix in _IMAGE_ATTACHMENT_EXTENSIONS:
        return True
    return path.stem.lower() == "cover" and suffix in _IMAGE_ATTACHMENT_EXTENSIONS


def _is_text_attachment_path(path: Path | None) -> bool:
    if path is None:
        return False
    return path.suffix.lower() in _TEXT_ATTACHMENT_EXTENSIONS


def _is_image_attachment_name(name: str, *, is_attached_pic: bool = False) -> bool:
    if is_attached_pic:
        return True
    path = Path(name)
    suffix = path.suffix.lower()
    if suffix in _IMAGE_ATTACHMENT_EXTENSIONS:
        return True
    return path.stem.lower() == "cover" and suffix in _IMAGE_ATTACHMENT_EXTENSIONS


def _is_text_attachment_name(name: str) -> bool:
    return Path(name).suffix.lower() in _TEXT_ATTACHMENT_EXTENSIONS


def _scaled_pixmap(pixmap: QPixmap, bounds: QSize) -> QPixmap:
    if pixmap.isNull():
        return pixmap
    if pixmap.width() <= bounds.width() and pixmap.height() <= bounds.height():
        return pixmap
    return pixmap.scaled(
        bounds,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _pretty_text_attachment_content(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() not in {".xml", ".nfo"}:
        return raw
    try:
        formatted = minidom.parseString(raw.encode("utf-8")).toprettyxml(indent="  ")
    except Exception:
        return raw
    return "\n".join(line for line in formatted.splitlines() if line.strip())


def _pretty_text_attachment_bytes(data: bytes, filename: str) -> str:
    raw = data.decode("utf-8", errors="replace")
    if Path(filename).suffix.lower() not in {".xml", ".nfo"}:
        return raw
    try:
        formatted = minidom.parseString(raw.encode("utf-8")).toprettyxml(indent="  ")
    except Exception:
        return raw
    return "\n".join(line for line in formatted.splitlines() if line.strip())


class _AttachmentNameButton(QPushButton):
    hover_moved = Signal(QPoint)
    hover_left = Signal()

    def __init__(self, text: str, color: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setFlat(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)
        self.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {color};
                border: none;
                padding: 0;
                text-align: left;
                font-size: {_font_px(11)}px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                color: {_C.ACCENT};
                text-decoration: underline;
            }}
            QPushButton:pressed {{
                color: {_C.ACCENT};
            }}
        """)

    def enterEvent(self, event) -> None:
        self.hover_moved.emit(QCursor.pos())
        super().enterEvent(event)

    def mouseMoveEvent(self, event) -> None:
        self.hover_moved.emit(event.globalPosition().toPoint())
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self.hover_left.emit()
        super().leaveEvent(event)


class _ImageHoverPreview(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.ToolTip)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setStyleSheet(f"""
            QWidget {{
                background: {_C.BG_PANEL};
                border: 1px solid {_C.BORDER_LT};
                border-radius: {_scale(6)}px;
            }}
            QLabel {{
                background: transparent;
                border: none;
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(_scale(8), _scale(8), _scale(8), _scale(8))
        self._label = QLabel()
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._label)

    def show_pixmap(self, pixmap: QPixmap, global_pos: QPoint) -> None:
        if pixmap.isNull():
            self.hide()
            return
        bounds = QSize(320, 200) if pixmap.width() >= pixmap.height() else QSize(200, 320)
        scaled = _scaled_pixmap(pixmap, bounds)
        self._label.setPixmap(scaled)
        self._label.resize(scaled.size())
        self.adjustSize()
        self.move(global_pos + QPoint(_scale(18), _scale(22)))
        self.show()
        self.raise_()


class _ClickableImageLabel(QLabel):
    """QLabel qui émet un signal au clic gauche."""
    clicked = Signal()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class _ImageAttachmentDialog(QDialog):
    """
    Modale d'aperçu d'image.

    - Dimensions de la modale ajustées à l'image (plafonnée à 720p de hauteur/1280 de largeur).
    - Si l'image source est ≥ 720p : clic sur l'image bascule entre dézoom 720p et zoom 100%,
      le curseur devient une loupe sur l'image.
    - Si l'image source est < 720p : affichage 100% taille d'origine, pas de zoom possible.
    """

    _FIT_BOUNDS = QSize(1280, 720)

    def __init__(
        self,
        display_name: str,
        *,
        path: Path | None = None,
        image_bytes: bytes | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._path = path
        self._pixmap = QPixmap()
        if image_bytes is not None:
            self._pixmap.loadFromData(image_bytes)
        elif path is not None:
            self._pixmap = QPixmap(str(path))
        self.setWindowTitle(display_name)
        self.setStyleSheet(f"""
            QDialog {{
                background: {_C.BG_DEEP};
                color: {_C.TEXT_PRI};
            }}
            QLabel {{
                background: transparent;
                border: none;
            }}
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(_scale(14), _scale(14), _scale(14), _scale(14))
        root.setSpacing(_scale(10))

        title = QLabel(display_name)
        title.setStyleSheet(
            f"color: {_C.TEXT_PRI}; font-size: {_font_px(13)}px; font-weight: 700;"
        )
        root.addWidget(title)

        # Image source ≥ 720p → zoom disponible (clic bascule 100% / dézoom 720p).
        self._zoom_available = (
            not self._pixmap.isNull()
            and (self._pixmap.width() > self._FIT_BOUNDS.width()
                 or self._pixmap.height() > self._FIT_BOUNDS.height())
        )
        self._zoomed = False  # état courant : False = dézoom 720p, True = 100%

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(False)
        self._scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background: {_C.BG_CARD}; border: 1px solid {_C.BORDER}; border-radius: {_scale(6)}px; }}"
        )
        self._image_label = _ClickableImageLabel()
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setStyleSheet("background: transparent;")
        if self._zoom_available:
            self._image_label.setCursor(Qt.CursorShape.WhatsThisCursor)
            self._image_label.clicked.connect(self._toggle_zoom)
        self._scroll.setWidget(self._image_label)
        root.addWidget(self._scroll, stretch=1)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.reject)
        btns.accepted.connect(self.accept)
        btns.setStyleSheet(f"""
            QPushButton {{
                background: {_C.BG_PANEL};
                color: {_C.TEXT_PRI};
                border: 1px solid {_C.BORDER_LT};
                border-radius: {_scale(4)}px;
                font-size: {_font_px(11)}px;
                padding: {_scale(4)}px {_scale(14)}px;
            }}
            QPushButton:hover {{
                background: {_C.BG_CARD};
            }}
        """)
        root.addWidget(btns)

        self._refresh_pixmap()
        self._resize_to_current_pixmap()

    def _displayed_pixmap(self) -> QPixmap:
        if self._pixmap.isNull():
            return self._pixmap
        if not self._zoom_available or not self._zoomed:
            return _scaled_pixmap(self._pixmap, self._FIT_BOUNDS)
        return self._pixmap

    def _refresh_pixmap(self) -> None:
        if self._pixmap.isNull():
            self._image_label.setText(translate_text("Image introuvable ou illisible."))
            self._image_label.setPixmap(QPixmap())
            return
        pixmap = self._displayed_pixmap()
        self._image_label.setPixmap(pixmap)
        self._image_label.resize(pixmap.size())

    def _resize_to_current_pixmap(self) -> None:
        """Ajuste la taille du dialogue aux dimensions de l'image affichée."""
        pixmap = self._displayed_pixmap()
        if pixmap.isNull():
            self.resize(_scale(640), _scale(360))
            return
        layout = self.layout()
        if layout is not None:
            margins = layout.contentsMargins()
            extra_w = margins.left() + margins.right() + _scale(4)
            extra_h = margins.top() + margins.bottom() + _scale(70)  # titre + boutons
        else:
            extra_w = _scale(4)
            extra_h = _scale(70)
        self.resize(pixmap.width() + extra_w, pixmap.height() + extra_h)

    def _toggle_zoom(self) -> None:
        if not self._zoom_available:
            return
        self._zoomed = not self._zoomed
        self._refresh_pixmap()


class _TextAttachmentDialog(QDialog):
    def __init__(
        self,
        display_name: str,
        *,
        path: Path | None = None,
        text_bytes: bytes | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(display_name)
        self.resize(_scale(920), _scale(640))
        self.setStyleSheet(f"""
            QDialog {{
                background: {_C.BG_DEEP};
                color: {_C.TEXT_PRI};
            }}
            QLabel {{
                background: transparent;
                border: none;
                color: {_C.TEXT_PRI};
            }}
            QPlainTextEdit {{
                background: {_C.BG_CARD};
                color: {_C.TEXT_PRI};
                border: 1px solid {_C.BORDER};
                border-radius: {_scale(6)}px;
                font-size: {_font_px(11)}px;
                padding: {_scale(10)}px;
            }}
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(_scale(14), _scale(14), _scale(14), _scale(14))
        root.setSpacing(_scale(10))

        title = QLabel(display_name)
        title.setStyleSheet(
            f"color: {_C.TEXT_PRI}; font-size: {_font_px(13)}px; font-weight: 700;"
        )
        root.addWidget(title)

        self._editor = QPlainTextEdit()
        self._editor.setReadOnly(True)
        self._editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFont("monospace")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSizeF(max(9.0, float(_font_px(10))))
        self._editor.setFont(font)
        if text_bytes is not None:
            self._editor.setPlainText(_pretty_text_attachment_bytes(text_bytes, display_name))
        elif path is not None:
            self._editor.setPlainText(_pretty_text_attachment_content(path))
        else:
            self._editor.setPlainText("")
        root.addWidget(self._editor, stretch=1)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.reject)
        btns.accepted.connect(self.accept)
        btns.setStyleSheet(f"""
            QPushButton {{
                background: {_C.BG_PANEL};
                color: {_C.TEXT_PRI};
                border: 1px solid {_C.BORDER_LT};
                border-radius: {_scale(4)}px;
                font-size: {_font_px(11)}px;
                padding: {_scale(4)}px {_scale(14)}px;
            }}
            QPushButton:hover {{
                background: {_C.BG_CARD};
            }}
        """)
        root.addWidget(btns)


# =============================================================================

class _AttachmentItemWidget(QWidget):
    """
    Ligne dans le panneau des pièces jointes.

    Quatre variantes :
    - Attachement source  (file_id, att, source_color)        → case + nom, sans ✕
    - Balises source      (file_id, is_tag=True, tag_count)   → case + "X balises", sans ✕
    - Ajout manuel        (is_manual=True, manual_path)        → case + nom + ✕
    - Cover TMDB pending  (is_tmdb_pending=True, tmdb_cover_url, tmdb_cover_filename)
                                                               → case + "nom — Depuis TMDB" + ✕
    """

    remove_clicked = Signal(object)   # self
    changed        = Signal()

    def __init__(
        self,
        file_id:            str,
        source_color:       str                 = "",
        att:                AttachmentInfo | None = None,
        tags:               dict[str, str]    | None = None,   # balises MKV globales
        is_tag:             bool              = False,
        is_manual:          bool              = False,
        manual_path:        Path | None       = None,
        is_tmdb_pending:    bool              = False,   # cover TMDB non encore téléchargée
        tmdb_cover_url:     str               = "",
        tmdb_cover_filename: str              = "",
        embedded_attachment_loader: Callable[[str, AttachmentInfo], bytes | None] | None = None,
        parent:             QWidget | None    = None,
    ) -> None:
        super().__init__(parent)
        self.file_id             = file_id
        self.att                 = att
        self.is_tag              = is_tag
        self.is_manual           = is_manual or is_tmdb_pending
        self.manual_path         = manual_path
        self.is_tmdb_pending     = is_tmdb_pending
        self.tmdb_cover_url      = tmdb_cover_url
        self.tmdb_cover_filename = tmdb_cover_filename
        self._embedded_attachment_loader = embedded_attachment_loader
        self._orig_tags:   dict[str, str] = tags or {}
        self._hover_preview: _ImageHoverPreview | None = None
        self._name_widget: QWidget | None = None
        self._embedded_attachment_bytes: bytes | None = None
        self._embedded_attachment_bytes_loaded = False
        self.setFixedHeight(_scale(28))
        self._build_ui(source_color)

    @property
    def tag_count(self) -> int:
        return len(self._orig_tags)

    @property
    def tags(self) -> dict[str, str]:
        return self._orig_tags

    @property
    def enabled(self) -> bool:
        return self._cb.isChecked()

    def _local_attachment_path(self) -> Path | None:
        return _resolved_manual_attachment_path(self.manual_path if self.is_manual else None)

    def _supports_image_interaction(self) -> bool:
        local_path = self._local_attachment_path()
        if _is_image_attachment_path(local_path):
            return True
        if self.is_tmdb_pending and self.tmdb_cover_url:
            return True
        if self.att is None or self._embedded_attachment_loader is None:
            return False
        return _is_image_attachment_name(
            self.att.filename,
            is_attached_pic=self.att.is_attached_pic,
        )

    def _supports_text_interaction(self) -> bool:
        local_path = self._local_attachment_path()
        if _is_text_attachment_path(local_path):
            return True
        if self.att is None or self._embedded_attachment_loader is None:
            return False
        return _is_text_attachment_name(self.att.filename)

    def _supports_interaction(self) -> bool:
        return self._supports_image_interaction() or self._supports_text_interaction()

    def _build_ui(self, source_color: str) -> None:
        lay = QHBoxLayout(self)
        lay.setContentsMargins(_scale(12), 0, _scale(8), 0)
        lay.setSpacing(_scale(6))

        # Carré coloré source
        if source_color and not self.is_manual:
            sq = QLabel("█")
            sq.setFixedWidth(_scale(14))
            sq.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sq.setStyleSheet(
                f"color: {source_color}; background: transparent; border: none; font-size: {_font_px(11)}px;"
            )
            lay.addWidget(sq)
        else:
            sp = QWidget()
            sp.setFixedWidth(_scale(14))
            lay.addWidget(sp)

        # Case à cocher
        self._cb = QCheckBox()
        self._cb.setChecked(True)
        self._cb.setStyleSheet(f"""
            QCheckBox::indicator {{
                width: {_scale(13)}px;
                height: {_scale(13)}px;
                border-radius: {_scale(3)}px;
                border: 1px solid {_C.BORDER_LT};
                background: {_C.BG_DEEP};
            }}
            QCheckBox::indicator:checked {{
                background: {_C.ACCENT};
                border-color: {_C.ACCENT};
            }}
        """)
        self._cb.stateChanged.connect(self.changed)
        lay.addWidget(self._cb)

        # Libellé
        if self.is_tag:
            n = self.tag_count
            names = ", ".join(self._orig_tags.keys())
            prefix = f"{n} Tag{'s' if n > 1 else ''}"
            text   = f"{prefix} : {names}" if names else prefix
            color  = _C.TRACK_TAGS
        elif self.is_tmdb_pending:
            fname = self.tmdb_cover_filename or "cover.jpg"
            text  = f"{fname}  —  {translate_text('Depuis TMDB')}"
            color = _C.ACCENT
        elif self.is_manual:
            text  = self.manual_path.name if self.manual_path else ""
            color = _C.TEXT_PRI
        else:
            text  = self.att.filename if self.att else ""
            color = _C.TRACK_ATTACHMENT

        lbl = QLabel(text)
        if self.is_tmdb_pending and self.tmdb_cover_url:
            lbl.setToolTip(self.tmdb_cover_url)
        elif self.is_manual and self.manual_path:
            lbl.setToolTip(str(self.manual_path))
        if self._supports_interaction():
            name_btn = _AttachmentNameButton(text, color, self)
            if self.is_manual and self.manual_path:
                name_btn.setToolTip(str(self.manual_path))
            name_btn.clicked.connect(self._open_attachment_viewer)
            if self._supports_image_interaction():
                name_btn.hover_moved.connect(self._show_hover_preview)
                name_btn.hover_left.connect(self._hide_hover_preview)
            self._name_widget = name_btn
            lay.addWidget(name_btn, stretch=1)
        else:
            lbl.setStyleSheet(
                f"color: {color}; background: transparent; border: none; font-size: {_font_px(11)}px;"
            )
            self._name_widget = lbl
            lay.addWidget(lbl, stretch=1)

        # Bouton ✕ (uniquement pour les ajouts manuels)
        if self.is_manual:
            rm_btn = QPushButton("✕")
            rm_btn.setFixedSize(_scale(18), _scale(18))
            rm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            rm_btn.setToolTip("Retirer cet attachement")
            rm_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    color: {_C.TEXT_DIM};
                    border: 1px solid {_C.BORDER};
                    border-radius: {_scale(3)}px;
                    font-size: {_font_px(9)}px;
                    font-weight: 700;
                }}
                QPushButton:hover {{
                    color: {_C.ERROR};
                    border-color: {_C.ERROR};
                    background: #1f0e0e;
                }}
            """)
            rm_btn.clicked.connect(lambda: self.remove_clicked.emit(self))
            lay.addWidget(rm_btn)
        elif not self.is_tag:
            sp2 = QWidget()
            sp2.setFixedWidth(_scale(18))
            lay.addWidget(sp2)

    def _show_hover_preview(self, global_pos: QPoint) -> None:
        if not self._supports_image_interaction():
            return
        pixmap = self._image_pixmap()
        if pixmap is None or pixmap.isNull():
            self._hide_hover_preview()
            return
        if self._hover_preview is None:
            self._hover_preview = _ImageHoverPreview(self.window())
        self._hover_preview.show_pixmap(pixmap, global_pos)

    def _hide_hover_preview(self) -> None:
        if self._hover_preview is not None:
            self._hover_preview.hide()

    def _open_attachment_viewer(self) -> None:
        self._hide_hover_preview()
        path = self._local_attachment_path()
        if self._supports_image_interaction():
            image_bytes = None if path is not None else self._load_embedded_attachment_bytes()
            if path is None and image_bytes is None:
                return
            _ImageAttachmentDialog(
                self._display_name(),
                path=path,
                image_bytes=image_bytes,
                parent=self,
            ).exec()
            return
        if self._supports_text_interaction():
            text_bytes = None if path is not None else self._load_embedded_attachment_bytes()
            if path is None and text_bytes is None:
                return
            _TextAttachmentDialog(
                self._display_name(),
                path=path,
                text_bytes=text_bytes,
                parent=self,
            ).exec()
            return
        QMessageBox.information(
            self,
            translate_text("Attachement"),
            translate_text("Aucun aperçu n'est disponible pour ce type de fichier."),
        )

    def _display_name(self) -> str:
        if self.is_manual and self.manual_path is not None:
            return self.manual_path.name
        if self.att is not None and self.att.filename:
            return self.att.filename
        return self.tmdb_cover_filename or ""

    def _load_embedded_attachment_bytes(self) -> bytes | None:
        if self._embedded_attachment_bytes_loaded:
            return self._embedded_attachment_bytes
        self._embedded_attachment_bytes_loaded = True
        if self.is_tmdb_pending and self.tmdb_cover_url:
            self._embedded_attachment_bytes = _fetch_tmdb_cover_bytes(self.tmdb_cover_url)
            if self._embedded_attachment_bytes is None:
                QMessageBox.warning(
                    self,
                    translate_text("Attachement"),
                    translate_text("Impossible de télécharger la cover TMDB."),
                )
            return self._embedded_attachment_bytes
        if self.att is None or self._embedded_attachment_loader is None:
            return None
        try:
            self._embedded_attachment_bytes = self._embedded_attachment_loader(
                self.file_id,
                self.att,
            )
        except Exception as exc:
            QMessageBox.warning(
                self,
                translate_text("Attachement"),
                translate_text("Impossible d'ouvrir cet attachement embarqué : {exc}", exc=exc),
            )
            self._embedded_attachment_bytes = None
            return None
        if self._embedded_attachment_bytes is None:
            QMessageBox.warning(
                self,
                translate_text("Attachement"),
                translate_text("Impossible d'extraire l'attachement embarqué."),
            )
        return self._embedded_attachment_bytes

    def _image_pixmap(self) -> QPixmap | None:
        path = self._local_attachment_path()
        if path is not None:
            pixmap = QPixmap(str(path))
            return None if pixmap.isNull() else pixmap
        image_bytes = self._load_embedded_attachment_bytes()
        if image_bytes is None:
            return None
        pixmap = QPixmap()
        pixmap.loadFromData(image_bytes)
        return None if pixmap.isNull() else pixmap

    def closeEvent(self, event) -> None:
        self._hide_hover_preview()
        super().closeEvent(event)


class _AttachmentPanel(QFrame):
    """
    Panneau dédié aux pièces jointes et balises MKV.

    Affiche :
    - Par fichier source : une ligne par attachement individuel (cochée par défaut)
    - Par fichier source : une ligne "X balises" (décochée par défaut)
    - Attachements manuels ajoutés via « Ajouter… » (cochés, avec bouton ✕)

    Signal :
        changed()  — émis à chaque modification de sélection ou ajout/retrait
    """

    changed = Signal()
    tmdb_details_selected = Signal(object)  # MediaDetails

    def __init__(self, config: "AppConfig", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config  = config
        self._items: list[_AttachmentItemWidget] = []
        self._source_attachment_loader: Callable[[str, AttachmentInfo], bytes | None] | None = None
        self._panel_tag_overrides: dict[str, str] | None = None  # None = utiliser tags source
        self._suggested_title: str = ""
        self._suggested_season: int = 0
        self._suggested_episode: int = 0
        self._auto_tmdb_cover_url: str = ""        # URL de la cover TMDB en attente
        self._auto_tmdb_cover_filename: str = ""   # nom de fichier correspondant
        self._build_ui()

    def set_suggested_title(self, title: str, season: int = 0, episode: int = 0) -> None:
        """
        Mémorise les suggestions de recherche TMDB.

        title : texte pré-rempli dans la recherche.
        season/episode : pré-remplissage optionnel des champs de série.
        """
        self._suggested_title = title
        self._suggested_season = season if season > 0 else 0
        self._suggested_episode = episode if episode > 0 else 0

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.setStyleSheet(f"""
            QFrame {{
                background: {_C.BG_CARD};
                border: 1px solid {_C.BORDER};
                border-radius: {_scale(6)}px;
            }}
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # En-tête
        header = QWidget()
        header.setStyleSheet(f"""
            QWidget {{
                background: {_C.BG_PANEL};
                border-bottom: 1px solid {_C.BORDER};
                border-top-left-radius: {_scale(6)}px;
                border-top-right-radius: {_scale(6)}px;
            }}
        """)
        header.setFixedHeight(_scale(32))
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(_scale(12), 0, _scale(8), 0)
        h_lay.setSpacing(_scale(8))

        title_lbl = QLabel("PIÈCES JOINTES  &  BALISES")
        title_lbl.setStyleSheet(f"""
            color: {_C.TEXT_DIM};
            font-size: {_font_px(9)}px;
            font-weight: 700;
            letter-spacing: {_scale(2)}px;
            background: transparent;
            border: none;
        """)
        h_lay.addWidget(title_lbl)
        h_lay.addStretch()

        self._imdb_btn = QPushButton("IMDb / TMDB")
        self._imdb_btn.setFixedHeight(_scale(22))
        self._imdb_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._imdb_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {_C.ACCENT};
                border: 1px solid {_C.ACCENT_DIM};
                border-radius: {_scale(4)}px;
                font-size: {_font_px(10)}px;
                font-weight: 600;
                padding: 0 {_scale(10)}px;
            }}
            QPushButton:hover {{
                background: {_C.ACCENT_DIM};
                color: #ffffff;
            }}
        """)
        self._imdb_btn.clicked.connect(self._open_media_search)
        h_lay.addWidget(self._imdb_btn)

        self._edit_tags_btn = QPushButton("Éditer les tags")
        self._edit_tags_btn.setFixedHeight(_scale(22))
        self._edit_tags_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._edit_tags_btn.setEnabled(False)
        self._edit_tags_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {_C.TRACK_TAGS};
                border: 1px solid {_C.TRACK_TAGS};
                border-radius: {_scale(4)}px;
                font-size: {_font_px(10)}px;
                font-weight: 600;
                padding: 0 {_scale(10)}px;
            }}
            QPushButton:hover {{
                background: rgba(245,160,48,0.15);
            }}
            QPushButton:disabled {{
                color: {_C.TEXT_DIM};
                border-color: {_C.BORDER};
            }}
        """)
        self._edit_tags_btn.clicked.connect(self._open_global_tag_dialog)
        h_lay.addWidget(self._edit_tags_btn)

        add_btn = QPushButton("+ Ajouter…")
        add_btn.setFixedHeight(_scale(22))
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {_C.ACCENT};
                border: 1px solid {_C.ACCENT_DIM};
                border-radius: {_scale(4)}px;
                font-size: {_font_px(10)}px;
                font-weight: 600;
                padding: 0 {_scale(10)}px;
            }}
            QPushButton:hover {{
                background: {_C.ACCENT_DIM};
                color: #ffffff;
            }}
        """)
        add_btn.clicked.connect(self._browse_add)
        h_lay.addWidget(add_btn)
        root.addWidget(header)

        # Placeholder
        self._placeholder = QLabel("Aucune pièce jointe ni balise dans les fichiers sources")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setContentsMargins(0, _scale(16), 0, _scale(16))
        self._placeholder.setStyleSheet(
            f"color: {_C.TEXT_DIM}; font-size: {_font_px(11)}px; background: transparent; border: none;"
        )
        root.addWidget(self._placeholder)

        # Conteneur des items
        self._items_widget = QWidget()
        self._items_widget.setStyleSheet("background: transparent;")
        self._items_layout = QVBoxLayout(self._items_widget)
        self._items_layout.setContentsMargins(0, _scale(4), 0, _scale(4))
        self._items_layout.setSpacing(0)
        root.addWidget(self._items_widget)

        self._update_state()

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def set_embedded_attachment_loader(
        self,
        loader: Callable[[str, AttachmentInfo], bytes | None] | None,
    ) -> None:
        self._source_attachment_loader = loader

    def add_source_attachments(
        self, file_id: str, source_color: str, attachments: list[AttachmentInfo]
    ) -> None:
        """Ajoute les attachements d'un fichier source."""
        for att in attachments:
            self._add_item(_AttachmentItemWidget(
                file_id=file_id,
                source_color=source_color,
                att=att,
                embedded_attachment_loader=self._source_attachment_loader,
            ))
        # Si une cover TMDB est en attente, les covers source doivent être
        # décochées pour laisser la priorité à la cover TMDB.
        if self._auto_tmdb_cover_url and self._has_existing_cover_attachment():
            self._deselect_existing_cover_items()

    def add_source_tags(self, file_id: str, source_color: str, tags: dict[str, str]) -> None:
        """Ajoute la ligne de balises d'un fichier source (si tags non vide)."""
        if tags:
            self._add_item(_AttachmentItemWidget(
                file_id=file_id, source_color=source_color,
                tags=tags, is_tag=True,
            ))

    def remove_by_file_id(self, file_id: str) -> None:
        """Retire tous les items (attachements + balises) d'un fichier source."""
        to_remove = [i for i in self._items if i.file_id == file_id]
        for item in to_remove:
            self._items.remove(item)
            self._items_layout.removeWidget(item)
            item.deleteLater()
        if to_remove:
            # Réinitialise les tags édités car la fusion peut avoir changé
            self._panel_tag_overrides = None
            self._update_state()
            self.changed.emit()

    def clear_all(self) -> None:
        """Vide complètement le panneau, y compris les ajouts manuels."""
        self._clear_pending_tmdb_cover()
        for item in self._items[:]:
            self._items_layout.removeWidget(item)
            item.deleteLater()
        self._items.clear()
        self._panel_tag_overrides = None
        self._update_state()

    def get_global_tag_overrides(self) -> "dict[str, str] | None":
        """
        Retourne les balises à appliquer globalement sur le fichier de sortie.

        - Si l'utilisateur a édité via le dialogue global → retourne les tags édités.
        - Sinon → fusionne les tags sources de toutes les lignes cochées
          (priorité au premier fichier importé pour les clés en commun).
        - Retourne None si aucune ligne de balises n'est cochée.
        """
        if self._panel_tag_overrides is not None:
            return self._panel_tag_overrides
        # Merge first-wins depuis les items de tags activés
        merged: dict[str, str] | None = None
        for item in self._items:
            if item.is_tag and item.enabled:
                if merged is None:
                    merged = {}
                for k, v in item.tags.items():
                    merged.setdefault(k, v)   # premier fichier garde la priorité
        return merged

    def get_extras_per_file(self) -> dict:
        """
        Retourne les sélections par fichier source.

        Retourne : dict[file_id, {
            "selected_attachments": list[AttachmentInfo],
            "has_tags": bool,   — True si la ligne de balises de ce fichier est cochée
        }]
        """
        result: dict = {}
        for item in self._items:
            if item.is_manual:
                continue
            entry = result.setdefault(
                item.file_id, {"selected_attachments": [], "has_tags": False}
            )
            if item.is_tag:
                if item.enabled:
                    entry["has_tags"] = True
            elif item.att is not None and item.enabled:
                entry["selected_attachments"].append(item.att)
        return result

    def get_extra_attachments(self) -> list[Path]:
        """Retourne les pièces jointes manuelles cochées."""
        return [
            item.manual_path
            for item in self._items
            if item.is_manual and item.enabled and item.manual_path is not None
        ]

    def add_manual_paths(self, paths: Sequence[str | Path]) -> None:
        """Ajoute des pièces jointes manuelles depuis une liste de chemins."""
        added = False
        existing_manual_paths = {
            item.manual_path
            for item in self._items
            if item.is_manual and item.manual_path is not None
        }
        for raw_path in paths:
            path = Path(raw_path)
            if path in existing_manual_paths:
                continue
            self._add_item(_AttachmentItemWidget(
                file_id="", is_manual=True, manual_path=path,
            ))
            existing_manual_paths.add(path)
            added = True
        if added:
            self.changed.emit()

    # ------------------------------------------------------------------
    # Interne
    # ------------------------------------------------------------------

    def _add_item(self, item: _AttachmentItemWidget) -> None:
        self._items.append(item)
        self._items_layout.addWidget(item)
        item.changed.connect(self.changed)
        item.remove_clicked.connect(self._on_remove_item)
        self._update_state()

    def _on_remove_item(self, item: _AttachmentItemWidget) -> None:
        self._items.remove(item)
        self._items_layout.removeWidget(item)
        if item.is_tmdb_pending:
            self._clear_pending_tmdb_cover()
        item.deleteLater()
        self._update_state()
        self.changed.emit()

    def _merged_source_tags(self) -> dict[str, str]:
        """Fusionne les tags de toutes les lignes de balises (premier fichier prioritaire)."""
        merged: dict[str, str] = {}
        for item in self._items:
            if item.is_tag and item.enabled:
                for k, v in item.tags.items():
                    merged.setdefault(k, v)
        return merged

    def _tmdb_comment_value(self) -> str:
        return translate_text("Informations media récupérée depuis TMDB.")

    def _clear_pending_tmdb_cover(self) -> None:
        """Réinitialise l'état de la cover TMDB en attente (URL + item widget)."""
        self._auto_tmdb_cover_url = ""
        self._auto_tmdb_cover_filename = ""

    def _clear_auto_tmdb_cover_item(self) -> None:
        """Retire l'item de cover TMDB en attente du panneau (s'il existe)."""
        removed = False
        for item in self._items[:]:
            if item.is_tmdb_pending:
                self._items.remove(item)
                self._items_layout.removeWidget(item)
                item.deleteLater()
                removed = True
                break
        self._clear_pending_tmdb_cover()
        if removed:
            self._update_state()

    def _item_has_cover(self, item: _AttachmentItemWidget) -> bool:
        """
        Retourne True si l'item est une cover active (fichier source ou manuel réel).
        Les covers TMDB en attente (is_tmdb_pending) ne comptent jamais comme
        « cover existante » pour éviter les boucles de déselection.
        """
        if not item.enabled:
            return False
        if item.is_tag:
            return False
        if item.is_tmdb_pending:
            return False
        if item.is_manual:
            if item.manual_path is None:
                return False
            return item.manual_path.stem.lower() == "cover"
        if item.att is None:
            return False
        if item.att.is_attached_pic:
            return True
        return Path(item.att.filename).stem.lower() == "cover"

    def _has_existing_cover_attachment(self) -> bool:
        """Retourne True si au moins une cover source/manuelle réelle est cochée."""
        return any(self._item_has_cover(item) for item in self._items)

    def _deselect_existing_cover_items(self) -> None:
        """
        Décoche toutes les covers source/manuelles réelles (non TMDB pending).
        Appelé quand une cover TMDB en attente prend la priorité.
        """
        for item in self._items:
            if not item.is_tmdb_pending and self._item_has_cover(item):
                item._cb.setChecked(False)

    def _install_tmdb_cover(self, details: MediaDetails) -> None:
        """
        Enregistre la cover TMDB en mode « téléchargement différé ».

        L'URL est mémorisée ; le fichier n'est créé qu'au lancement du workflow.
        Si des covers source sont déjà présentes, elles sont décochées.
        """
        self._clear_auto_tmdb_cover_item()
        if not details.cover_url:
            return

        filename = (details.cover_filename or "cover.jpg").strip() or "cover.jpg"
        self._auto_tmdb_cover_url = details.cover_url
        self._auto_tmdb_cover_filename = filename

        # Décocher les covers existantes pour laisser la priorité à la cover TMDB
        self._deselect_existing_cover_items()

        self._add_item(_AttachmentItemWidget(
            file_id="",
            is_tmdb_pending=True,
            tmdb_cover_url=details.cover_url,
            tmdb_cover_filename=filename,
        ))

    def get_pending_tmdb_cover(self) -> "tuple[str, str] | None":
        """
        Retourne (url, filename) de la cover TMDB en attente si elle est cochée,
        None sinon.
        """
        for item in self._items:
            if item.is_tmdb_pending and item.enabled and item.tmdb_cover_url:
                return item.tmdb_cover_url, item.tmdb_cover_filename or "cover.jpg"
        return None

    def _apply_tmdb_details(self, details: MediaDetails, *, open_editor: bool = True) -> None:
        self.tmdb_details_selected.emit(details)

        new_tags = details.to_mkv_tags()
        new_tags["COMMENTS"] = self._tmdb_comment_value()
        current = (
            self._panel_tag_overrides
            if self._panel_tag_overrides is not None
            else self._merged_source_tags()
        )
        # Les données TMDB prennent la priorité sur les balises sources
        merged = {**current, **new_tags}
        self._panel_tag_overrides = merged
        self._install_tmdb_cover(details)

        if open_editor:
            # Ouvrir le dialogue d'édition pour relecture/corrections
            edit_dlg = _TagEditDialog(merged, parent=self)
            if edit_dlg.exec() == QDialog.DialogCode.Accepted:
                self._panel_tag_overrides = edit_dlg.result_tags()

        n = len(self._panel_tag_overrides or {})
        label = translate_text("Tags édités ({count})", count=n) if n else translate_text("Tags supprimés")
        self._edit_tags_btn.setText(label)
        self._edit_tags_btn.setEnabled(True)
        self.changed.emit()

    def _open_media_search(self) -> None:
        """
        Ouvre la modale de recherche TMDB et injecte les métadonnées en balises MKV.

        Le titre suggéré est celui mémorisé par set_suggested_title() ; il peut être
        issu du champ Titre saisi manuellement ou du nom de fichier nettoyé.
        Les balises récupérées sont fusionnées avec les balises existantes
        (les données TMDB ont la priorité), puis ouvertes dans le dialogue
        d'édition pour une révision éventuelle avant confirmation.
        """
        dlg = TmdbSearchModal(
            self._config,
            suggested_title=self._suggested_title,
            suggested_season=self._suggested_season,
            suggested_episode=self._suggested_episode,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        details = dlg.fetched_details
        if details is None:
            return
        self._apply_tmdb_details(details)

    def _open_global_tag_dialog(self) -> None:
        """Ouvre le dialogue d'édition global des balises (toutes sources fusionnées)."""
        current = self._panel_tag_overrides if self._panel_tag_overrides is not None \
            else self._merged_source_tags()
        dlg = _TagEditDialog(current, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._panel_tag_overrides = dlg.result_tags()
            # Met à jour le libellé du bouton pour indiquer qu'il y a des modifications
            n = len(self._panel_tag_overrides)
            label = translate_text("Tags édités ({count})", count=n) if n else translate_text("Tags supprimés")
            self._edit_tags_btn.setText(label)
            self.changed.emit()

    def _update_state(self) -> None:
        has = bool(self._items)
        self._placeholder.setVisible(not has)
        self._items_widget.setVisible(has)
        self._edit_tags_btn.setEnabled(True)
        self._edit_tags_btn.setText(translate_text("Éditer les tags"))

    def closeEvent(self, event) -> None:
        self._clear_pending_tmdb_cover()
        super().closeEvent(event)

    def _browse_add(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            translate_text("Ajouter des pièces jointes"),
            "",
            translate_text("Tous les fichiers (*)"),
        )
        self.add_manual_paths(paths)

__all__ = ["_AttachmentItemWidget", "_AttachmentPanel", "_TagEditDialog"]
