"""
ui/panels/encode_panel/widgets.py — Reusable sub-widgets for the encode panel.

Public:
    _has_atmos       — detects Atmos layer in TrueHD track
    _FileZone        — drag-drop file source selector
    _AudioSourceDialog — popup for adding custom audio track
    _AudioTable      — editable audio tracks table
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog,
    QFrame, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from core.bluray import discover_titles
from core.file_types import VIDEO_CONTAINER_EXTENSIONS, build_qt_filter
from core.inspector import AudioTrack, FileInfo
from core.i18n import apply_translations, translate_text
from core.lang_tags import Rfc5646LanguageTags
from core.workflows.encode.catalog import AUDIO_CODECS
from core.workflows.encode.models import (
    DEFAULT_AUDIO_KBPS_PER_CHANNEL,
    audio_bitrate_choices_kbps,
    default_audio_bitrate_kbps,
    normalize_audio_bitrate_kbps,
    AudioTrackSettings,
)
from core.workflows.remux_models import TrackEntry
from ui.panels.encode_panel.theme import (
    _C, _combo_style, _input_style, _primary_button, _secondary_button, _separator,
)
from ui.design_system import font_px as _font_px, scale as _scale

if TYPE_CHECKING:
    from core.config import AppConfig


# =============================================================================
# Helpers
# =============================================================================

def _has_atmos(track: AudioTrack) -> bool:
    """True si la piste est TrueHD avec couche Atmos (utilisé pour extract_truehd_core)."""
    return track.codec.lower() == "truehd" and track.atmos_flag


def _channel_count(track: AudioTrack) -> int:
    """Retourne le nombre de canaux de la piste, avec fallback raisonnable."""
    if track.channels and track.channels > 0:
        return track.channels
    layout = (track.channel_layout or "").lower()
    if "7.1" in layout:
        return 8
    if "5.1" in layout:
        return 6
    if "stereo" in layout:
        return 2
    if "mono" in layout:
        return 1
    return 2


def _sample_rate_hz(track: AudioTrack) -> int | None:
    if track.sample_rate and track.sample_rate > 0:
        return track.sample_rate
    return None


def _clamp_codec_bitrate_kbps(codec: str, track: AudioTrack, bitrate_kbps: int, config: "AppConfig | None" = None) -> int:
    """Borne un débit selon le codec cible."""
    return normalize_audio_bitrate_kbps(
        codec,
        bitrate_kbps,
        _channel_count(track),
        _sample_rate_hz(track),
        track.channel_layout,
    )


def _default_lossy_bitrate_kbps(track: AudioTrack, config: "AppConfig | None" = None, codec: str = "aac") -> int:
    """Débit par défaut pour les codecs lossy, par canal configurable pour AAC et EAC-3."""
    kbps_per_channel: int | None = None

    def _safe_per_channel(value: object) -> int:
        try:
            parsed = int(str(value))
            return parsed if parsed > 0 else DEFAULT_AUDIO_KBPS_PER_CHANNEL
        except (TypeError, ValueError):
            return DEFAULT_AUDIO_KBPS_PER_CHANNEL

    if config is not None:
        if codec == "aac":
            kbps_per_channel = _safe_per_channel(
                getattr(config, "aac_bitrate_per_channel_kbps", DEFAULT_AUDIO_KBPS_PER_CHANNEL)
            )
        elif codec == "eac3":
            kbps_per_channel = _safe_per_channel(
                getattr(config, "eac3_bitrate_per_channel_kbps", DEFAULT_AUDIO_KBPS_PER_CHANNEL)
            )
    return default_audio_bitrate_kbps(
        codec,
        _channel_count(track),
        _sample_rate_hz(track),
        track.channel_layout,
        kbps_per_channel,
    )


def _raw_source_bitrate_kbps(track: AudioTrack) -> int | None:
    """Débit source exact en kbps, ou None s'il n'est pas connu."""
    if track.bit_rate and track.bit_rate > 0:
        return max(1, round(track.bit_rate / 1000))
    return None


def _default_lossy_selected_bitrate_kbps(track: AudioTrack, config: "AppConfig | None" = None, codec: str = "aac") -> int:
    """Valeur préselectionnée pour les codecs lossy, bornée par le bitrate source si plus faible."""
    default_bitrate = _default_lossy_bitrate_kbps(track, config, codec)
    source_bitrate = _raw_source_bitrate_kbps(track)
    if source_bitrate is not None and source_bitrate < default_bitrate:
        return _rounded_choice_not_above_source(codec, track, source_bitrate)
    return default_bitrate


def _source_bitrate_kbps(track: AudioTrack, config: "AppConfig | None" = None) -> int:
    """Débit source en kbps, ou fallback sur le débit par défaut."""
    source_bitrate = _raw_source_bitrate_kbps(track)
    if source_bitrate is not None:
        return source_bitrate
    return _default_lossy_bitrate_kbps(track, config)


def _lossy_combo_bitrate_choices(track: AudioTrack, config: "AppConfig | None" = None, codec: str = "aac") -> list[int]:
    """Plage de débits adaptée au codec cible."""
    _ = config
    return audio_bitrate_choices_kbps(
        codec,
        _channel_count(track),
        _sample_rate_hz(track),
        track.channel_layout,
    )


def _rounded_choice_not_above_source(codec: str, track: AudioTrack, bitrate_kbps: int) -> int:
    """Retourne le meilleur choix fixe sans dépasser le bitrate source."""
    choices = _lossy_combo_bitrate_choices(track, None, codec)
    if not choices:
        return bitrate_kbps
    eligible = [choice for choice in choices if choice <= bitrate_kbps]
    return eligible[-1] if eligible else choices[0]


def _preferred_bitrate_for_codec_switch(
    track: AudioTrack,
    config: "AppConfig | None",
    new_codec: str,
    previous_codec: str,
    current_value: int,
) -> int | None:
    """Bitrate préféré lors d'un changement de codec depuis l'UI."""
    if new_codec == previous_codec:
        return current_value
    if new_codec in {"aac", "ac3", "eac3"}:
        return _default_lossy_bitrate_kbps(track, config, new_codec)
    if new_codec in {"copy", "flac"}:
        return None
    return current_value


def _closest_choice(value: int, choices: list[int]) -> int:
    """Retourne l'option la plus proche dans une liste de choix."""
    if not choices:
        return value
    return min(choices, key=lambda choice: abs(choice - value))


def _choices_with_selected(selected: int, choices: list[int]) -> list[int]:
    """Ajoute la valeur sélectionnée à la liste si elle n'est pas déjà proposée."""
    if selected <= 0 or selected in choices:
        return choices
    return sorted([*choices, selected])


class _AudioBitrateEditor(QWidget):
    """Éditeur de débit qui change de contrôle selon le codec choisi."""

    value_changed = Signal()

    def __init__(
        self,
        track: AudioTrack,
        config: "AppConfig | None" = None,
        codec: str = "copy",
        bitrate_kbps: int | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._track = track
        self._config = config
        self._codec = codec

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._combo = QComboBox()
        self._combo.setStyleSheet(_combo_style())
        self._combo.setFixedWidth(_scale(84))
        self._combo.hide()
        self._combo.currentIndexChanged.connect(lambda _=0: self.value_changed.emit())
        layout.addWidget(self._combo)

        self._edit = QLineEdit()
        self._edit.setStyleSheet(_input_style())
        self._edit.setFixedWidth(_scale(84))
        self._edit.textChanged.connect(lambda _="": self.value_changed.emit())
        layout.addWidget(self._edit)

        self.set_codec(codec, bitrate_kbps)

    def set_track(self, track: AudioTrack) -> None:
        self._track = track
        self.set_codec(self._codec, None)

    def set_codec(self, codec: str, bitrate_kbps: int | None = None) -> None:
        self._codec = codec

        if codec in {"aac", "ac3", "eac3"}:
            selected = bitrate_kbps if bitrate_kbps is not None else _default_lossy_selected_bitrate_kbps(self._track, self._config, codec)
            selected = _clamp_codec_bitrate_kbps(codec, self._track, selected, self._config)
            source_bitrate = _raw_source_bitrate_kbps(self._track)
            choices = _choices_with_selected(
                selected,
                _lossy_combo_bitrate_choices(self._track, self._config, codec),
            )
            selected = _closest_choice(selected, choices)
            self._combo.blockSignals(True)
            self._combo.clear()
            for index, choice in enumerate(choices):
                self._combo.addItem(str(choice), choice)
                if source_bitrate is not None and choice == source_bitrate:
                    font = self._combo.font()
                    font.setBold(True)
                    self._combo.setItemData(index, font, Qt.ItemDataRole.FontRole)
            idx = next((i for i in range(self._combo.count()) if self._combo.itemData(i) == selected), 0)
            self._combo.setCurrentIndex(idx)
            self._combo.blockSignals(False)
            self._combo.setEnabled(True)
            self._combo.show()
            self._edit.hide()
            return

        if codec == "flac":
            value = bitrate_kbps if bitrate_kbps is not None else _source_bitrate_kbps(self._track, self._config)
            self._edit.setText(str(value))
            self._edit.setEnabled(True)
            self._edit.show()
            self._combo.hide()
            return

        # En mode copy, afficher la référence source plutôt qu'un défaut AAC implicite.
        value = bitrate_kbps if bitrate_kbps is not None else _source_bitrate_kbps(self._track, self._config)
        self._edit.setText(str(value))
        self._edit.setEnabled(codec != "copy")
        self._edit.show()
        self._combo.hide()

    def value(self) -> int:
        if self._codec in {"aac", "ac3", "eac3"}:
            data = self._combo.currentData()
            if isinstance(data, int):
                return data
            try:
                return int(self._combo.currentText())
            except ValueError:
                return _default_lossy_bitrate_kbps(self._track, self._config, self._codec)
        try:
            return int(self._edit.text())
        except ValueError:
            if self._codec == "flac":
                return _source_bitrate_kbps(self._track, self._config)
            return _default_lossy_bitrate_kbps(self._track, self._config)


# =============================================================================
# Zone de dépôt du fichier source
# =============================================================================

class _FileZone(QFrame):
    file_selected = Signal(str)
    _ACCEPTED = VIDEO_CONTAINER_EXTENSIONS

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._build_ui()
        apply_translations(self)

    def _build_ui(self) -> None:
        self.setStyleSheet(f"QFrame{{background:{_C.BG_CARD};"
                           f"border:1px dashed {_C.BORDER_LT};border-radius:{_scale(8)}px;}}")
        self.setMinimumHeight(_scale(72))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(_scale(16), _scale(12), _scale(16), _scale(12))
        layout.setSpacing(_scale(12))

        self._icon = QLabel("⊞")
        self._icon.setStyleSheet(f"font-size:{_font_px(24)}px;color:{_C.TEXT_DIM};"
                                 f"background:transparent;border:none;")
        layout.addWidget(self._icon)

        text_col = QVBoxLayout()
        text_col.setSpacing(_scale(3))
        self._main_lbl = QLabel("Déposer un fichier vidéo ici")
        self._main_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:{_font_px(12)}px;"
                                     f"font-weight:500;background:transparent;border:none;")
        self._info_lbl = QLabel("")
        self._info_lbl.setStyleSheet(f"color:{_C.TEXT_DIM};font-size:{_font_px(10)}px;"
                                     f"font-family:'JetBrains Mono',monospace;"
                                     f"background:transparent;border:none;")
        text_col.addWidget(self._main_lbl)
        text_col.addWidget(self._info_lbl)
        layout.addLayout(text_col, stretch=1)

        btn = _secondary_button("Parcourir…")
        btn.clicked.connect(self._browse)
        layout.addWidget(btn)

    def set_file_info(self, info: FileInfo) -> None:
        self._main_lbl.setText(info.path.name)
        self._main_lbl.setStyleSheet(f"color:{_C.TEXT_PRI};font-size:{_font_px(12)}px;"
                                     f"font-weight:600;background:transparent;border:none;")
        parts = [info.size_human, info.duration_human, info.format]
        if info.primary_video:
            parts.append(info.primary_video.resolution)
            hdr_lbl = info.primary_video.hdr_label
            if hdr_lbl != "SDR":
                parts.append(hdr_lbl)
        self._info_lbl.setText("   ".join(p for p in parts if p != "?"))
        self.setStyleSheet(f"QFrame{{background:{_C.BG_CARD};"
                           f"border:1px solid {_C.BORDER_LT};border-radius:{_scale(8)}px;}}")

    def reset(self) -> None:
        self._main_lbl.setText(translate_text("Déposer un fichier vidéo ici"))
        self._main_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:{_font_px(12)}px;"
                                     f"font-weight:500;background:transparent;border:none;")
        self._info_lbl.setText("")
        self.setStyleSheet(f"QFrame{{background:{_C.BG_CARD};"
                           f"border:1px dashed {_C.BORDER_LT};border-radius:{_scale(8)}px;}}")

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                path = Path(url.toLocalFile())
                if path.suffix.lower() in self._ACCEPTED or (path.is_dir() and discover_titles(path, min_duration_s=60.0)):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.suffix.lower() in self._ACCEPTED and path.is_file():
                self.file_selected.emit(str(path))
                event.acceptProposedAction()
                return
            if path.is_dir():
                titles = discover_titles(path, min_duration_s=60.0)
                if titles:
                    self.file_selected.emit(str(titles[0].playlist_path))
                    event.acceptProposedAction()
                    return
        event.ignore()

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            translate_text("Sélectionner un fichier vidéo"),
            "",
            build_qt_filter(video_only=True),
        )
        if path:
            self.file_selected.emit(path)


# =============================================================================
# Popup de sélection source pour piste audio custom
# =============================================================================

class _AudioSourceDialog(QDialog):
    """
    Fenêtre popup pour ajouter une piste audio custom.
    Permet de choisir la piste source, l'encodage et le débit cible.

    tracks : pistes d'origine uniquement, sous forme
             list[tuple[AudioTrack, str]] ou list[tuple[AudioTrack, str, Path, TrackEntry]]
    """

    def __init__(
        self,
        tracks: list[tuple],   # (track, color) ou (track, color, source_path)
        config: "AppConfig | None" = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._tracks = tracks
        self._config = config
        self._result_track:       AudioTrack | None = None
        self._result_color:       str = "#ffffff"
        self._result_source_path = None   # Path | None
        self._result_track_entry: TrackEntry | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setWindowTitle("Ajouter une piste audio")
        self.setModal(True)
        self.setMinimumWidth(_scale(500))
        self.setStyleSheet(
            f"QDialog{{background:{_C.BG_PANEL};}}"
            f"QLabel{{background:transparent;color:{_C.TEXT_PRI};}}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(_scale(24), _scale(20), _scale(24), _scale(20))
        layout.setSpacing(_scale(16))

        # Titre
        title = QLabel("Sélectionner la piste source")
        title.setStyleSheet(
            f"font-size:{_font_px(14)}px;font-weight:700;color:{_C.TEXT_PRI};"
        )
        layout.addWidget(title)

        sub = QLabel(
            "La piste sera ajoutée en tant qu'encodage supplémentaire de la source choisie."
        )
        sub.setStyleSheet(f"font-size:{_font_px(11)}px;color:{_C.TEXT_SEC};")
        sub.setWordWrap(True)
        layout.addWidget(sub)

        # Liste des pistes source
        self._track_list = QListWidget()
        self._track_list.setStyleSheet(
            f"QListWidget{{background:{_C.BG_CARD};border:1px solid {_C.BORDER};"
            f"border-radius:{_scale(6)}px;color:{_C.TEXT_PRI};font-size:{_font_px(11)}px;"
            f"font-family:'JetBrains Mono',monospace;}}"
            f"QListWidget::item{{padding:{_scale(10)}px {_scale(12)}px;"
            f"border-bottom:1px solid {_C.BORDER};}}"
            f"QListWidget::item:selected{{background:{_C.ACCENT_DIM};}}"
            f"QListWidget::item:hover{{background:{_C.BG_HOVER};}}"
        )
        for entry in self._tracks:
            track, color = entry[0], entry[1]
            source_path = entry[2] if len(entry) > 2 else None
            track_entry = entry[3] if len(entry) > 3 else None
            ch = track.channels_label
            lang = track.language or "—"
            title_part = f"  {track.title}" if track.title else ""
            if track.atmos_flag:
                fmt_tag = "  Atmos"
            elif track.dtsx_flag:
                fmt_tag = "  DTS:X"
            else:
                fmt_tag = ""
            text = f"█  #{track.index}  {track.codec.upper()} {ch}{fmt_tag}  [{lang}]{title_part}"
            item = QListWidgetItem(text)
            item.setForeground(QBrush(QColor(color)))
            item.setData(Qt.ItemDataRole.UserRole, (track, color, source_path, track_entry))
            self._track_list.addItem(item)
        if self._track_list.count():
            self._track_list.setCurrentRow(0)
        n = min(self._track_list.count(), 6)
        self._track_list.setFixedHeight(n * _scale(40) + _scale(4))
        layout.addWidget(self._track_list)

        layout.addWidget(_separator())

        # Encodage + débit
        enc_row = QHBoxLayout()
        enc_row.setSpacing(_scale(12))
        enc_lbl = QLabel("Encodage")
        enc_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:{_font_px(11)}px;")
        enc_lbl.setFixedWidth(_scale(70))
        enc_row.addWidget(enc_lbl)

        self._codec_combo = QComboBox()
        self._codec_combo.setStyleSheet(_combo_style())
        self._codec_combo.setMinimumWidth(_scale(200))
        for codec_id, codec_label in AUDIO_CODECS:
            self._codec_combo.addItem(codec_label, codec_id)
        self._codec_combo.currentIndexChanged.connect(self._on_codec_changed)
        enc_row.addWidget(self._codec_combo)
        enc_row.addStretch()
        layout.addLayout(enc_row)

        br_row = QHBoxLayout()
        br_row.setSpacing(_scale(12))
        br_lbl = QLabel("Débit cible")
        br_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:{_font_px(11)}px;")
        br_lbl.setFixedWidth(_scale(70))
        br_row.addWidget(br_lbl)
        self._bitrate_edit = _AudioBitrateEditor(self._tracks[0][0], self._config, "copy")
        self._bitrate_edit.setFixedWidth(_scale(90))
        br_row.addWidget(self._bitrate_edit)
        br_kbps = QLabel("kbps")
        br_kbps.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:{_font_px(11)}px;")
        br_row.addWidget(br_kbps)
        br_row.addStretch()
        layout.addLayout(br_row)

        layout.addSpacing(_scale(4))

        # Boutons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = _secondary_button("Annuler")
        cancel_btn.clicked.connect(self.reject)
        add_btn = _primary_button("Ajouter la piste")
        add_btn.setFixedWidth(160)
        add_btn.clicked.connect(self._on_accept)
        btn_row.addWidget(cancel_btn)
        btn_row.addSpacing(8)
        btn_row.addWidget(add_btn)
        layout.addLayout(btn_row)
        self._track_list.currentItemChanged.connect(self._on_track_changed)
        apply_translations(self)

    def _on_codec_changed(self, _idx: int = 0) -> None:
        codec = self._codec_combo.currentData()
        previous_codec = getattr(self._bitrate_edit, "_codec", "copy")
        active_track = getattr(self._bitrate_edit, "_track", self._tracks[0][0])
        preferred = _preferred_bitrate_for_codec_switch(
            active_track,
            self._config,
            codec or "copy",
            previous_codec,
            self._bitrate_edit.value(),
        )
        self._bitrate_edit.set_codec(codec or "copy", preferred)

    def _on_track_changed(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is None:
            return
        data = current.data(Qt.ItemDataRole.UserRole)
        self._bitrate_edit.set_track(data[0])

    def _on_accept(self) -> None:
        item = self._track_list.currentItem()
        if item is None:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        self._result_track       = data[0]
        self._result_color       = data[1]
        self._result_source_path = data[2] if len(data) > 2 else None
        self._result_track_entry = data[3] if len(data) > 3 and isinstance(data[3], TrackEntry) else None
        self.accept()

    def selected_track(self) -> AudioTrack | None:
        return self._result_track

    def selected_track_entry(self) -> TrackEntry | None:
        return self._result_track_entry

    def selected_color(self) -> str:
        return self._result_color

    def selected_source_path(self):   # -> Path | None
        return self._result_source_path

    def selected_codec(self) -> str:
        return self._codec_combo.currentData() or "copy"

    def selected_bitrate(self) -> int:
        return self._bitrate_edit.value()


# =============================================================================
# Tableau des pistes audio
# =============================================================================

class _AudioTable(QTableWidget):
    """
    Tableau listant les pistes audio avec sélecteur codec + débit par ligne.
    Chaque ligne dispose d'un bouton de suppression, désactivé si c'est la
    dernière entrée pour cette piste source.

    Colonnes : src  |  #  |  Format  |  Bitrate src  |  Lang  |  Nom  |  Encodage  |  Débit  |  Del
    """

    # Émis quand l'utilisateur modifie lang ou titre :
    # (stream_index, source_path, lang, title, track_entry_id)
    track_meta_changed = Signal(int, object, str, str, object)
    track_encoding_changed = Signal(object, str, int)
    track_removed = Signal(object)

    COL_SOURCE  = 0
    COL_IDX     = 1
    COL_FORMAT  = 2
    COL_SRC_BR  = 3
    COL_LANG    = 4
    COL_TITLE   = 5
    COL_CODEC   = 6
    COL_BITRATE = 7
    COL_DEL     = 8
    HEADERS = ["", "#", "Format", "Bitrate src", "Lang", "Nom", "Encodage", "Débit", ""]

    def __init__(self, config: "AppConfig | None" = None, parent: QWidget | None = None) -> None:
        super().__init__(0, len(self.HEADERS), parent)
        self._config = config
        self._row_data: list[dict] = []   # {combo, bitrate, has_atmos, track, color, source_path, del_btn}
        self._changed_cb = None
        self._prev_lang: dict[int, str] = {}
        self._setup_table()
        self.itemChanged.connect(self._on_item_changed)
        apply_translations(self)

    def set_changed_callback(self, cb) -> None:
        self._changed_cb = cb

    def _setup_table(self) -> None:
        self.setHorizontalHeaderLabels(self.HEADERS)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self.verticalHeader().setVisible(False)
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(self.COL_SOURCE,  QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_IDX,     QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_FORMAT,  QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_SRC_BR,  QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_LANG,    QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_TITLE,   QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(self.COL_CODEC,   QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_BITRATE, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_DEL,     QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(self.COL_SOURCE,  20)
        self.setColumnWidth(self.COL_IDX,     32)
        self.setColumnWidth(self.COL_FORMAT, 170)
        self.setColumnWidth(self.COL_SRC_BR, 86)
        self.setColumnWidth(self.COL_LANG,    56)
        self.setColumnWidth(self.COL_CODEC,  210)
        self.setColumnWidth(self.COL_BITRATE, 96)
        self.setColumnWidth(self.COL_DEL,     36)
        self.setStyleSheet(f"""
            QTableWidget{{background:{_C.BG_CARD};color:{_C.TEXT_PRI};
                          border:1px solid {_C.BORDER};border-radius:6px;
                          gridline-color:transparent;font-size:11px;}}
            QHeaderView::section{{background:{_C.BG_ACTIVE};color:{_C.TEXT_DIM};
                                   border:none;padding:4px 8px;font-size:9px;
                                   font-weight:700;letter-spacing:1px;}}
            QTableWidget::item{{padding:4px 8px;border:none;}}
            QTableWidget::item:selected{{background:{_C.ACCENT_DIM};}}
        """)

    _MAX_VISIBLE_ROWS = 10
    _ROW_H = 36

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def load_tracks(
        self,
        tracks: list[tuple],   # list[tuple[AudioTrack, str]] ou [AudioTrack, str, Path, TrackEntry]
        default_codec: str = "copy",
        default_bitrate: int | None = None,
    ) -> None:
        previous_settings: dict[object, list[tuple[str, int]]] = {}
        for data in self._row_data:
            key = data.get("track_entry_id") or (data.get("source_path"), data["track"].index)
            codec = data["combo"].currentData() or default_codec
            bitrate = data["bitrate"].value()
            previous_settings.setdefault(key, []).append((codec, bitrate))

        self.blockSignals(True)
        self._row_data = []
        self._prev_lang = {}
        self.setRowCount(0)
        for entry in tracks:
            track, color = entry[0], entry[1]
            source_path = entry[2] if len(entry) > 2 else None
            track_entry = entry[3] if len(entry) > 3 else None
            track_entry_id = track_entry.entry_id if isinstance(track_entry, TrackEntry) else None
            is_new = bool(getattr(track_entry, "is_new", False))
            key = track_entry_id or (source_path, track.index)
            codec = default_codec
            bitrate = default_bitrate
            saved_settings = previous_settings.get(key)
            if saved_settings:
                codec, bitrate = saved_settings.pop(0)
            self._append_row(
                track,
                color,
                codec,
                bitrate,
                source_path,
                track_entry_id=track_entry_id,
                is_new=is_new,
            )
        self.blockSignals(False)
        self._refresh_delete_buttons()
        self._adjust_height()

    def add_custom_row(
        self, track: AudioTrack, color: str, codec: str = "copy", bitrate: int | None = None,
        source_path=None,   # Path | None
        track_entry_id: str | None = None,
    ) -> None:
        self._append_row(
            track,
            color,
            codec,
            bitrate,
            source_path,
            track_entry_id=track_entry_id,
            is_new=bool(track_entry_id),
        )
        self._refresh_delete_buttons()
        self._adjust_height()
        if self._changed_cb:
            self._changed_cb()

    def current_audio_settings(self) -> list[AudioTrackSettings]:
        result: list[AudioTrackSettings] = []
        for d in self._row_data:
            codec = d["combo"].currentData() or "copy"
            bitrate = d["bitrate"].value()
            result.append(AudioTrackSettings(
                stream_index=d["track"].index,
                codec=codec,
                bitrate_kbps=bitrate,
                extract_truehd_core=d["has_atmos"] and codec == "copy",
                input_channels=d["track"].channels,
                input_channel_layout=d["track"].channel_layout,
                source_path=d.get("source_path"),
                track_entry_id=d.get("track_entry_id"),
            ))
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _adjust_height(self) -> None:
        n = self.rowCount()
        header_h = self.horizontalHeader().height()
        if n == 0:
            self.setFixedHeight(header_h + 40)
            return
        visible = min(n, self._MAX_VISIBLE_ROWS)
        self.setFixedHeight(visible * self._ROW_H + header_h + 4)

    def _append_row(
        self, track: AudioTrack, color: str, codec: str, bitrate: int | None,
        source_path=None,   # Path | None
        *,
        track_entry_id: str | None = None,
        is_new: bool = False,
    ) -> None:
        row = self.rowCount()
        self.insertRow(row)
        self.setRowHeight(row, self._ROW_H)

        def _item(text: str) -> QTableWidgetItem:
            it = QTableWidgetItem(text)
            it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            return it

        src_item = QTableWidgetItem("█")
        src_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        src_item.setForeground(QBrush(QColor(color)))
        src_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setItem(row, self.COL_SOURCE, src_item)

        fmt_parts = [f"{track.codec.upper()} {track.channels_label}"]
        if track.atmos_flag:
            fmt_parts.append("Atmos")
        elif track.dtsx_flag:
            fmt_parts.append("DTS:X")

        def _item_rw(text: str) -> QTableWidgetItem:
            it = QTableWidgetItem(text)
            it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable)
            return it

        self.setItem(row, self.COL_IDX,    _item(str(track.index)))
        self.setItem(row, self.COL_FORMAT, _item("  ".join(fmt_parts)))
        source_bitrate = _raw_source_bitrate_kbps(track)
        self.setItem(row, self.COL_SRC_BR, _item(str(source_bitrate) if source_bitrate is not None else "—"))
        self.setItem(row, self.COL_TITLE,  _item_rw(track.title or ""))
        self._prev_lang[row] = track.language or ""
        self.setItem(row, self.COL_LANG,   _item_rw(track.language or ""))

        # Sélecteur codec
        combo = QComboBox()
        for codec_id, codec_label in AUDIO_CODECS:
            combo.addItem(codec_label, codec_id)
        sel_idx = next((i for i, (cid, _) in enumerate(AUDIO_CODECS) if cid == codec), 0)
        combo.setCurrentIndex(sel_idx)
        combo.setStyleSheet(_combo_style())
        combo.currentIndexChanged.connect(self._make_codec_handler(combo))
        callback = self._changed_cb
        if callback is not None:
            combo.currentIndexChanged.connect(callback)
        self.setCellWidget(row, self.COL_CODEC, combo)

        # Débit
        bitrate_edit = _AudioBitrateEditor(track, self._config, codec, bitrate)
        if self._changed_cb:
            bitrate_edit.value_changed.connect(self._changed_cb)
        bitrate_edit.value_changed.connect(
            lambda editor=bitrate_edit: self._emit_encoding_changed_for_bitrate_editor(editor)
        )
        self.setCellWidget(row, self.COL_BITRATE, bitrate_edit)

        # Bouton suppression
        del_btn = QPushButton("✕")
        del_btn.setFixedSize(22, 22)
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.setStyleSheet(f"""
            QPushButton{{background:transparent;color:{_C.ERROR};
                         border:1px solid {_C.ERROR};border-radius:4px;
                         font-size:10px;font-weight:700;padding:0;}}
            QPushButton:hover{{background:#2a1010;}}
            QPushButton:pressed{{background:#1a0808;}}
            QPushButton:disabled{{color:{_C.TEXT_DIM};border-color:{_C.TEXT_DIM};}}
        """)
        del_btn.clicked.connect(self._make_delete_handler(del_btn))
        del_w = QWidget()
        del_w.setStyleSheet("background:transparent;")
        dl = QHBoxLayout(del_w)
        dl.setContentsMargins(4, 0, 4, 0)
        dl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dl.addWidget(del_btn)
        self.setCellWidget(row, self.COL_DEL, del_w)

        self._row_data.append({
            "combo":       combo,
            "bitrate":     bitrate_edit,
            "has_atmos":   _has_atmos(track),
            "track":       track,
            "color":       color,
            "source_path": source_path,
            "track_entry_id": track_entry_id,
            "is_new":     is_new,
            "del_btn":     del_btn,
        })

    def refresh_runtime_settings(self) -> None:
        for data in self._row_data:
            combo = data["combo"]
            bitrate_edit = data["bitrate"]
            codec = combo.currentData() or "copy"
            bitrate_edit.set_codec(codec, bitrate_edit.value())

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        row = item.row()
        col = item.column()
        if col not in (self.COL_TITLE, self.COL_LANG):
            return
        if row >= len(self._row_data):
            return
        if col == self.COL_LANG:
            if not Rfc5646LanguageTags.validate_item(item, self._prev_lang):
                prev = self._prev_lang.get(row, "")
                self.blockSignals(True)
                item.setText(prev)
                self.blockSignals(False)
                QTimer.singleShot(0, lambda: QMessageBox.warning(
                    self,
                    translate_text("Erreur"),
                    translate_text("Erreur : code langue non reconnu"),
                ))
                return
        d = self._row_data[row]
        lang_item  = self.item(row, self.COL_LANG)
        title_item = self.item(row, self.COL_TITLE)
        lang  = lang_item.text()  if lang_item  else ""
        title = title_item.text() if title_item else ""
        self.track_meta_changed.emit(
            d["track"].index,
            d.get("source_path"),
            lang,
            title,
            d.get("track_entry_id"),
        )
        if self._changed_cb:
            self._changed_cb()

    def _make_codec_handler(self, combo: QComboBox):
        def _handler(_idx: int = 0) -> None:
            for d in self._row_data:
                if d["combo"] is combo:
                    codec = combo.currentData()
                    previous_codec = getattr(d["bitrate"], "_codec", "copy")
                    preferred = _preferred_bitrate_for_codec_switch(
                        d["track"],
                        self._config,
                        codec or "copy",
                        previous_codec,
                        d["bitrate"].value(),
                    )
                    d["bitrate"].set_codec(codec or "copy", preferred)
                    self._emit_encoding_changed(d)
                    break
        return _handler

    def _emit_encoding_changed_for_bitrate_editor(self, editor: _AudioBitrateEditor) -> None:
        for data in self._row_data:
            if data["bitrate"] is editor:
                self._emit_encoding_changed(data)
                return

    def _emit_encoding_changed(self, data: dict) -> None:
        track_entry_id = data.get("track_entry_id")
        if not track_entry_id:
            return
        codec = data["combo"].currentData() or "copy"
        self.track_encoding_changed.emit(track_entry_id, codec, int(data["bitrate"].value()))

    def emit_encoding_plans(self) -> None:
        for data in self._row_data:
            self._emit_encoding_changed(data)

    def _make_delete_handler(self, del_btn: QPushButton):
        def _handler() -> None:
            for row, d in enumerate(self._row_data):
                if d["del_btn"] is del_btn:
                    self._delete_row(row)
                    break
        return _handler

    def _delete_row(self, row: int) -> None:
        if not self._can_delete(row):
            return
        track_entry_id = self._row_data[row].get("track_entry_id")
        is_new = bool(self._row_data[row].get("is_new"))
        self.removeRow(row)
        self._row_data.pop(row)
        self._refresh_delete_buttons()
        self._adjust_height()
        if is_new and track_entry_id:
            self.track_removed.emit(track_entry_id)
        if self._changed_cb:
            self._changed_cb()

    def _can_delete(self, row: int) -> bool:
        if bool(self._row_data[row].get("is_new")):
            return True
        track_idx = self._row_data[row]["track"].index
        source_path = self._row_data[row].get("source_path")
        return (
            sum(
                1
                for d in self._row_data
                if d["track"].index == track_idx and d.get("source_path") == source_path
            )
            > 1
        )

    def _refresh_delete_buttons(self) -> None:
        for row, d in enumerate(self._row_data):
            d["del_btn"].setEnabled(self._can_delete(row))
