"""
ui/panels/track_edit_dialog.py — Boîte de dialogue d'édition des métadonnées d'une piste.

Utilisée par RemuxPanel (_TrackTable) pour modifier en place un TrackEntry.

Usage :
    from ui.panels.track_edit_dialog import TrackEditDialog

    dlg = TrackEditDialog(entry, parent=self)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        ...  # entry a été mis à jour en place

Champs proposés selon le type de piste :
    video    → Langue uniquement
    audio    → Nom, Langue, Flags (sans Forcé)
    subtitle → Nom, Langue, Flags complets (avec Forcé, sans Malvoyant)
"""

from __future__ import annotations

import re

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFrame, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit,
    QMessageBox,
    QVBoxLayout, QWidget,
)

from core.i18n import apply_translations, translate_text
from core.lang_tags import Rfc5646LanguageTags
from core.workflows.remux_models import TrackEntry
from ui.design_system import colors as _C, font_px as _font_px, scale as _scale


_SIGNED_INT_RE = re.compile(r"^[+-]?\d+$")


def _separator() -> QFrame:
    sep = QFrame()
    sep.setFrameShape(QFrame.Shape.HLine)
    sep.setFixedHeight(_scale(1))
    sep.setStyleSheet(f"background: {_C.BORDER}; border: none;")
    return sep


# =============================================================================
# Boîte de dialogue
# =============================================================================

class TrackEditDialog(QDialog):
    """
    Fenêtre modale d'édition des métadonnées d'une piste.

    Les champs proposés varient selon le type de piste :
        video    → Langue uniquement
        audio    → Nom, Langue, Flags (sans Forcé)
        subtitle → Nom, Langue, Flags complets (avec Forcé, sans Malvoyant)

    Usage :
        dlg = TrackEditDialog(entry, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            ...  # entry a été mis à jour en place
    """

    def __init__(self, entry: TrackEntry, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entry = entry
        tt = entry.track_type

        title = (
            translate_text("Langue de la piste")
            if tt == "video"
            else translate_text("Éditer la piste")
        )
        self.setWindowTitle(f"{title} — {entry.type_long}  ·  {entry.codec}")
        self.setModal(True)
        self.setMinimumWidth(_scale(440))
        self.setStyleSheet(f"""
            QDialog {{
                background: {_C.BG_PANEL};
            }}
            QLabel {{
                color: {_C.TEXT_PRI};
                background: transparent;
            }}
            QLineEdit {{
                background: {_C.BG_DEEP};
                color: {_C.TEXT_PRI};
                border: 1px solid {_C.BORDER_LT};
                border-radius: {_scale(4)}px;
                padding: {_scale(4)}px {_scale(8)}px;
                font-size: {_font_px(12)}px;
            }}
            QLineEdit:focus {{
                border-color: {_C.ACCENT};
            }}
            QComboBox {{
                background: {_C.BG_DEEP};
                color: {_C.TEXT_PRI};
                border: 1px solid {_C.BORDER_LT};
                border-radius: {_scale(4)}px;
                padding: {_scale(4)}px {_scale(8)}px;
                font-size: {_font_px(12)}px;
            }}
            QComboBox::drop-down {{
                border: none;
                padding-right: {_scale(6)}px;
            }}
            QComboBox QAbstractItemView {{
                background: {_C.BG_CARD};
                color: {_C.TEXT_PRI};
                selection-background-color: {_C.ACCENT_DIM};
                border: 1px solid {_C.BORDER_LT};
            }}
            QGroupBox {{
                color: {_C.TEXT_DIM};
                font-size: {_font_px(9)}px;
                font-weight: 700;
                letter-spacing: {_scale(2)}px;
                border: 1px solid {_C.BORDER};
                border-radius: {_scale(6)}px;
                margin-top: {_scale(10)}px;
                padding-top: {_scale(8)}px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: {_scale(10)}px;
                padding: 0 {_scale(4)}px;
                background: {_C.BG_PANEL};
            }}
            QCheckBox {{
                color: {_C.TEXT_PRI};
                font-size: {_font_px(12)}px;
                spacing: {_scale(8)}px;
            }}
            QCheckBox::indicator {{
                width: {_scale(14)}px;
                height: {_scale(14)}px;
                border-radius: {_scale(3)}px;
                border: 1px solid {_C.BORDER_LT};
                background: {_C.BG_DEEP};
            }}
            QCheckBox::indicator:checked {{
                background: {_C.ACCENT};
                border-color: {_C.ACCENT};
            }}
            QDialogButtonBox QPushButton {{
                background: {_C.BG_CARD};
                color: {_C.TEXT_SEC};
                border: 1px solid {_C.BORDER};
                border-radius: {_scale(5)}px;
                font-size: {_font_px(11)}px;
                font-weight: 600;
                padding: {_scale(5)}px {_scale(18)}px;
                min-width: {_scale(70)}px;
            }}
            QDialogButtonBox QPushButton:hover {{
                background: {_C.BG_HOVER};
                color: {_C.TEXT_PRI};
                border-color: {_C.BORDER_LT};
            }}
            QDialogButtonBox QPushButton[text="OK"] {{
                background: {_C.ACCENT};
                color: #ffffff;
                border: none;
            }}
            QDialogButtonBox QPushButton[text="OK"]:hover {{
                background: #6070f0;
            }}
        """)
        self._build_ui()
        self._configure_for_type(tt)
        self._populate(entry)
        apply_translations(self)

    # ------------------------------------------------------------------
    # Construction de l'UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(_scale(14))
        root.setContentsMargins(_scale(20), _scale(20), _scale(20), _scale(20))

        # --- En-tête : type + codec + infos techniques ---
        header_parts = [self._entry.type_long, self._entry.codec]
        display_info = (self._entry.display_info or "").strip()
        if display_info:
            header_parts.append(display_info)
        header_lbl = QLabel("  ·  ".join(header_parts))
        header_lbl.setStyleSheet(f"""
            color: {_C.TEXT_SEC};
            font-size: {_font_px(11)}px;
            font-family: 'JetBrains Mono', monospace;
            background: transparent;
        """)
        root.addWidget(header_lbl)

        root.addWidget(_separator())

        # --- Section Nom (cachée pour vidéo) ---
        self._name_widget = QWidget()
        name_lay = QVBoxLayout(self._name_widget)
        name_lay.setContentsMargins(0, 0, 0, 0)
        name_lay.setSpacing(_scale(4))

        name_lbl = QLabel("NOM DE LA PISTE")
        name_lbl.setStyleSheet(
            f"color: {_C.TEXT_DIM}; font-size: {_font_px(9)}px; font-weight: 700; "
            f"letter-spacing: {_scale(2)}px; background: transparent;"
        )
        name_lay.addWidget(name_lbl)

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Titre de la piste (facultatif)")
        name_lay.addWidget(self._name_edit)
        root.addWidget(self._name_widget)

        # --- Section Langue (toujours visible) ---
        lang_lbl = QLabel("LANGUE  (RFC 5646)")
        lang_lbl.setStyleSheet(
            f"color: {_C.TEXT_DIM}; font-size: {_font_px(9)}px; font-weight: 700; "
            f"letter-spacing: {_scale(2)}px; background: transparent;"
        )
        root.addWidget(lang_lbl)

        lang_row = QHBoxLayout()
        lang_row.setSpacing(_scale(8))

        self._lang_edit = QLineEdit()
        self._lang_edit.setPlaceholderText("ex : fr, en, ja, und…")
        self._lang_edit.setMaximumWidth(_scale(120))
        lang_row.addWidget(self._lang_edit)

        self._lang_combo = QComboBox()
        self._lang_combo.setMaxVisibleItems(20)
        self._lang_combo.addItem("— sélectionner —", None)
        for code, name in Rfc5646LanguageTags.items():
            self._lang_combo.addItem(f"{code}  —  {name}", code)
        lang_row.addWidget(self._lang_combo, stretch=1)
        root.addLayout(lang_row)

        self._lang_warn = QLabel("⚠  Balise non reconnue — vérifiez la valeur RFC 5646")
        self._lang_warn.setStyleSheet(
            f"color: {_C.WARN}; font-size: {_font_px(10)}px; background: transparent;"
        )
        self._lang_warn.setVisible(False)
        root.addWidget(self._lang_warn)

        # --- Section Flags (cachée pour vidéo) ---
        self._flags_group = QGroupBox("FLAGS")
        flags_lay = QVBoxLayout(self._flags_group)
        flags_lay.setSpacing(_scale(6))
        flags_lay.setContentsMargins(_scale(12), _scale(8), _scale(12), _scale(10))

        self._cb_enabled    = QCheckBox("Piste activée")
        self._cb_default    = QCheckBox("Piste par défaut")
        self._cb_forced     = QCheckBox("Forcé  (forced track)")
        self._cb_hearing    = QCheckBox("Malentendant  (hearing impaired)")
        self._cb_visual     = QCheckBox("Malvoyant  (visual impaired)")
        self._cb_original   = QCheckBox("Langue d'origine  (original language)")
        self._cb_commentary = QCheckBox("Commentaires  (commentary)")

        for cb in (
            self._cb_enabled, self._cb_default, self._cb_forced,
            self._cb_hearing, self._cb_visual,
            self._cb_original, self._cb_commentary,
        ):
            flags_lay.addWidget(cb)

        root.addWidget(self._flags_group)

        # --- Ligne basse : décalage + boutons OK/Annuler ---
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(_scale(10))

        offset_lbl = QLabel("DÉCALAGE TEMPOREL (ms)")
        offset_lbl.setStyleSheet(
            f"color: {_C.TEXT_DIM}; font-size: {_font_px(9)}px; font-weight: 700; "
            f"letter-spacing: {_scale(1)}px; background: transparent;"
        )
        bottom_row.addWidget(offset_lbl)

        self._offset_edit = QLineEdit()
        self._offset_edit.setPlaceholderText("ex : +125, -80, 0")
        self._offset_edit.setMaximumWidth(_scale(140))
        bottom_row.addWidget(self._offset_edit)
        bottom_row.addStretch()

        # --- Boutons OK / Annuler ---
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        bottom_row.addWidget(btn_box)
        root.addLayout(bottom_row)

        self._offset_warn = QLabel("⚠  Valeur invalide — entier signé attendu")
        self._offset_warn.setStyleSheet(
            f"color: {_C.WARN}; font-size: {_font_px(10)}px; background: transparent;"
        )
        self._offset_warn.setVisible(False)
        root.addWidget(self._offset_warn)

        # Connexions
        self._lang_combo.currentIndexChanged.connect(self._on_combo_changed)
        self._lang_edit.textChanged.connect(self._on_lang_text_changed)
        self._offset_edit.textChanged.connect(self._on_offset_text_changed)

    def _configure_for_type(self, track_type: str) -> None:
        """
        Adapte la visibilité des sections selon le type de piste.

        video    → Langue uniquement (Nom et Flags masqués)
        audio    → Nom + Langue + Flags sans Forcé (Malvoyant visible)
        subtitle → Nom + Langue + Flags avec Forcé, sans Malvoyant
        """
        is_video    = track_type == "video"
        is_subtitle = track_type == "subtitle"

        self._name_widget.setVisible(not is_video)
        self._flags_group.setVisible(not is_video)

        # Forcé uniquement pour les sous-titres
        self._cb_forced.setVisible(is_subtitle)
        # Malvoyant uniquement pour l'audio (pas les sous-titres)
        self._cb_visual.setVisible(not is_subtitle)

    # ------------------------------------------------------------------
    # Population / synchronisation
    # ------------------------------------------------------------------

    def _populate(self, entry: TrackEntry) -> None:
        self._name_edit.setText(entry.title)
        self._lang_edit.setText(entry.language)
        self._offset_edit.setText(str(int(entry.time_shift_ms)) if int(entry.time_shift_ms) != 0 else "")
        self._cb_enabled.setChecked(entry.flag_enabled)
        self._cb_default.setChecked(entry.flag_default)
        self._cb_forced.setChecked(entry.flag_forced)
        self._cb_hearing.setChecked(entry.flag_hearing_impaired)
        self._cb_visual.setChecked(entry.flag_visual_impaired)
        self._cb_original.setChecked(entry.flag_original)
        self._cb_commentary.setChecked(entry.flag_commentary)
        self._sync_combo_to_lang(entry.language)
        self._update_offset_validation(self._offset_edit.text())

    def _sync_combo_to_lang(self, lang: str) -> None:
        """Sélectionne dans la combobox la valeur correspondant au code langue."""
        for i in range(self._lang_combo.count()):
            if self._lang_combo.itemData(i) == lang:
                self._lang_combo.blockSignals(True)
                self._lang_combo.setCurrentIndex(i)
                self._lang_combo.blockSignals(False)
                return
        self._lang_combo.blockSignals(True)
        self._lang_combo.setCurrentIndex(0)
        self._lang_combo.blockSignals(False)

    def _on_combo_changed(self, idx: int) -> None:
        code = self._lang_combo.itemData(idx)
        if code is not None:
            self._lang_edit.blockSignals(True)
            self._lang_edit.setText(code)
            self._lang_edit.blockSignals(False)
            self._update_lang_validation(code)

    def _on_lang_text_changed(self, text: str) -> None:
        self._sync_combo_to_lang(text.strip())
        self._update_lang_validation(text.strip())

    def _on_offset_text_changed(self, text: str) -> None:
        self._update_offset_validation(text)

    def _update_lang_validation(self, tag: str) -> None:
        valid = Rfc5646LanguageTags.is_valid(tag)
        self._lang_warn.setVisible(bool(tag) and not valid)
        border = _C.BORDER_LT if valid else _C.WARN
        self._lang_edit.setStyleSheet(f"""
            QLineEdit {{
                background: {_C.BG_DEEP};
                color: {_C.TEXT_PRI};
                border: 1px solid {border};
                border-radius: {_scale(4)}px;
                padding: {_scale(4)}px {_scale(8)}px;
                font-size: {_font_px(12)}px;
            }}
        """)

    @staticmethod
    def _parse_offset_ms(text: str) -> int | None:
        raw = text.strip()
        if raw == "":
            return 0
        if not _SIGNED_INT_RE.fullmatch(raw):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _update_offset_validation(self, text: str) -> None:
        valid = self._parse_offset_ms(text) is not None
        self._offset_warn.setVisible(bool(text.strip()) and not valid)
        border = _C.BORDER_LT if valid else _C.WARN
        self._offset_edit.setStyleSheet(f"""
            QLineEdit {{
                background: {_C.BG_DEEP};
                color: {_C.TEXT_PRI};
                border: 1px solid {border};
                border-radius: {_scale(4)}px;
                padding: {_scale(4)}px {_scale(8)}px;
                font-size: {_font_px(12)}px;
            }}
        """)

    # ------------------------------------------------------------------
    # Acceptation
    # ------------------------------------------------------------------

    def accept(self) -> None:
        e = self._entry
        tt = e.track_type
        parsed_offset = self._parse_offset_ms(self._offset_edit.text())
        if parsed_offset is None:
            QMessageBox.warning(
                self,
                translate_text("Erreur"),
                translate_text("Décalage temporel invalide : entier signé attendu."),
            )
            return

        # Langue : toujours appliquée
        e.language = self._lang_edit.text().strip()
        e.time_shift_ms = int(parsed_offset)
        # Nom : audio et subtitle uniquement
        if tt != "video":
            e.title = self._name_edit.text().strip()
        # Flags : audio et subtitle uniquement
        if tt != "video":
            e.flag_enabled            = self._cb_enabled.isChecked()
            e.flag_default            = self._cb_default.isChecked()
            e.flag_hearing_impaired   = self._cb_hearing.isChecked()
            e.flag_original           = self._cb_original.isChecked()
            e.flag_commentary         = self._cb_commentary.isChecked()
            if tt == "subtitle":
                e.flag_forced         = self._cb_forced.isChecked()
            if tt == "audio":
                e.flag_visual_impaired = self._cb_visual.isChecked()
        super().accept()
