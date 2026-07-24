"""
tests/test_remux.py — Tests du workflow de remuxage multi-source et de l'UI associée.

Plan de couverture :
    _pick_file_color :
        - Retourne une chaîne hex valide (#rrggbb, 7 chars)
        - Déterministe : même index → même couleur
        - Indices différents → couleurs différentes
        - Ni proche du noir ni proche du blanc (luminosité 0.1–0.9)
        - Angle doré : couleurs consécutives bien séparées en teinte (>90°)

    SourceFile :
        - Champ color par défaut ""
        - Champ color accepte une valeur hex

    TrackEntry :
        - Propriétés type_label / type_long pour chaque type de piste
        - Valeurs par défaut (file_id vide, enabled True)

    tracks_from_file_info :
        - file_id propagé à toutes les pistes générées
        - Ordre de sortie : vidéo → audio → sous-titres
        - display_info vidéo (résolution + HDR + fps décimal et fractionnaire)
        - display_info audio (canaux + kHz + kbps)
        - display_info sous-titre (flags forcé / défaut)
        - FileInfo sans pistes → liste vide

    RemuxWorkflow.build_command :
        - Source unique, toutes pistes activées → pas de flags de filtrage
        - Source unique, aucune vidéo activée → --no-video
        - Source unique, audio partiel → --audio-tracks avec TIDs
        - Source unique, aucun sous-titre → --no-subtitles partiel → --subtitle-tracks
        - Source sans vidéo du tout → --no-video N'EST PAS émis (bug-guard)
        - --no-chapters / --no-attachments respectés
        - --track-name émis si title modifié, absent si inchangé
        - --language émis si modifié, absent si inchangé
        - Deux sources : --track-order cross-source (fi:tid,...)
        - Deux sources : flags per-source indépendants
        - track_order vide → pas de --track-order dans la commande
        - --track-order précède les chemins de source dans la commande

    RemuxWorkflow.preview_command :
        - Sortie multi-ligne avec continuation \\
        - Chaque flag/valeur sur sa propre ligne

    RemuxWorkflow.validate :
        - sources vide → erreur immédiate (une seule)
        - Fichier source introuvable → erreur
        - source == output → erreur
        - Dossier de sortie inexistant → erreur
        - track_order vide → erreur "Aucune piste"
        - Configuration valide (fichiers réels) → liste vide
        - Plusieurs erreurs → toutes retournées

    _TrackTable — colonnes :
        - COL_LANG == 4, COL_INFO == 5 (Langue avant Info)
        - En-têtes : "Langue" en col 4, "Info" en col 5

    _TrackTable — tri par type (V → A → S) :
        - Pistes vidéo insérées avant audio (table vide)
        - Pistes audio insérées après vidéo, avant sous-titres
        - Pistes sous-titre insérées en dernier
        - Multi-source : V-file1, V-file2, A-file1, A-file2 (groupement par type)
        - _find_insert_position("video") → 0 sur table vide
        - _find_insert_position("audio") → après toutes les vidéos
        - _find_insert_position("subtitle") → après tous les audios

    _TrackTable — colonne source (carré coloré) :
        - Texte de la cellule est "█"
        - Couleur de premier plan correspond à source_color fournie
        - Couleur stockée dans UserRole (pour reconstruction après drag-drop)

    _TrackTable — filtre "sélectionnées seulement" :
        - set_filter_selected(False) → toutes les lignes visibles
        - set_filter_selected(True) → lignes décochées masquées
        - refresh_filter() reapplique le filtre après changement de case
        - Désactiver le filtre réaffiche les lignes masquées

    _TrackTable — ajustement de hauteur :
        - Table vide → hauteur de base (header + placeholder)
        - N ≤ 15 lignes → N * row_h + header + 4
        - N > 15 lignes → 15 * row_h + header + 4 (plafond)

    _TrackTable (widget Qt) — autres :
        - append_tracks ajoute les bonnes lignes
        - append_tracks ne supprime pas les lignes existantes
        - remove_tracks_by_file_id supprime uniquement les lignes concernées
        - clear_all vide complètement le tableau
        - current_tracks retourne les pistes dans l'ordre du tableau
        - current_tracks synchronise l'état enabled depuis la case à cocher
        - current_tracks synchronise language et title éditables
        - set_all_enabled active / désactive toutes les pistes

    _FileListWidget — taille automatique :
        - Hauteur initiale (vide) = _FILE_PH_H + _FILE_BAR_H
        - Hauteur avec N fichiers = N * _FILE_ROW_H + _FILE_BAR_H
        - file_count() retourne le bon nombre

    _TrackTable.update_audio_meta :
        - Met à jour language et title dans la cellule et dans l'objet TrackEntry
        - Met à jour codec et bitrate affichés quand l'encode panel prévoit un réencodage
        - N'émet pas de signal itemChanged (blockSignals)
        - Cible uniquement la ligne correspondant à (file_id, mkv_tid)
        - Cible la piste NEW par entry_id sans modifier la source
        - Laisse les autres lignes intactes
        - Sans effet si (file_id, mkv_tid) introuvable

    RemuxPanel — pistes NEW issues de l'encode panel :
        - Si la piste source est désélectionnée, la piste NEW reste disponible
        - Si la piste NEW est supprimée, elle quitte le panel remux et le workflow
        - Les éditions de nom restent indépendantes entre source et piste NEW
        - Un changement d'ordre réémet les pistes audio vers EncodePanel avec les entry_id

    _AttachmentItemWidget — balises cochées par défaut :
        - is_tag=False → case cochée
        - is_tag=True → case cochée (nouveau comportement)

Exécution :
    pytest tests/test_remux.py -v
"""

from __future__ import annotations

import colorsys
import json
from threading import Event
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QDialog, QPushButton

from core.config import AppConfig
from core.i18n import current_language, set_current_language, translate_text
from core.inspector import (
    AttachmentInfo, AudioTrack, ChapterEntry, ChapterInfo, FileInfo, HDRType, SubtitleTrack, VideoTrack,
    build_chapter_xml,
)
from core.matroska.reader import MatroskaReader
from core.media_info_fetcher import MediaDetails
from core.profiles.decision import remux_config_to_decision_profile
from core.workflows.remux import RemuxWorkflow
from core.workflows.remux_mapping import resolved_global_tags
from core.workflows.remux_models import (
    RemuxConfig, SourceInput, TrackEntry, clone_track_entry, tracks_from_file_info,
)
from ui.panels.remux_panel import (
    RemuxPanel, SourceFile, _FILE_BAR_H, _FILE_PH_H, _FILE_ROW_H,
    _AttachmentItemWidget, _AttachmentPanel, _FileListWidget, _TrackInfoDelegate, _TrackTable,
    _TRACK_INFO_OFFSET_COLOR, _TRACK_INFO_OFFSET_NEG_COLOR, _TRACK_INFO_OFFSET_POS_COLOR,
    _TRACK_INFO_OFFSET_VALUE_ROLE, _TRACK_INFO_SYNC_LABEL_ROLE,
    _normalize_tmdb_manual_title_suggestion, _pick_file_color,
)
from ui.panels.remux_panel.functions import inspection as inspection_functions
from ui.panels.remux_panel.widgets.attachments import _AttachmentNameButton, _pretty_text_attachment_content
from ui.panels.remux_panel.widgets.file_list import _ACCEPTED_EXT
from ui.panels.track_edit_dialog import TrackEditDialog
from ui.panels.tmdb_search_modal import extract_season_episode


# ===========================================================================
# Helpers / fabriques
# ===========================================================================

def _video(
    index: int = 0,
    codec: str = "hevc",
    width: int = 1920,
    height: int = 1080,
    hdr_type: HDRType = HDRType.NONE,
    frame_rate: str | None = "23.976",
    bit_rate: int | None = None,
    language: str | None = None,
    title: str | None = None,
) -> VideoTrack:
    return VideoTrack(
        index=index, codec=codec, codec_long=codec,
        width=width, height=height,
        frame_rate=frame_rate,
        bit_depth=10, color_space=None,
        color_primaries=None, color_transfer=None, color_matrix=None,
        hdr_type=hdr_type, bit_rate=bit_rate, language=language, title=title,
    )


def _audio(
    index: int = 1,
    codec: str = "eac3",
    channels: int = 6,
    sample_rate: int = 48000,
    bit_rate: int | None = 640_000,
    language: str | None = "fra",
    title: str | None = None,
) -> AudioTrack:
    return AudioTrack(
        index=index, codec=codec, codec_long=codec,
        channels=channels, channel_layout=None,
        sample_rate=sample_rate, bit_rate=bit_rate,
        language=language, title=title,
    )


def _subtitle(
    index: int = 2,
    codec: str = "subrip",
    language: str | None = "fra",
    forced: bool = False,
    default: bool = False,
    element_count: int | None = None,
) -> SubtitleTrack:
    return SubtitleTrack(
        index=index, codec=codec, language=language,
        title=None, forced=forced, default=default, element_count=element_count,
    )


def _file_info(
    path: Path = Path("/tmp/film.mkv"),
    videos: list[VideoTrack] | None = None,
    audios: list[AudioTrack] | None = None,
    subs: list[SubtitleTrack] | None = None,
) -> FileInfo:
    return FileInfo(
        path=path,
        format="matroska",
        duration_s=7200.0,
        size_bytes=20_000_000_000,
        bit_rate=22_000_000,
        video_tracks=videos or [],
        audio_tracks=audios or [],
        subtitle_tracks=subs or [],
    )


def _encode_ebml_size(value: int) -> bytes:
    for length in range(1, 9):
        max_known = (1 << (7 * length)) - 2
        if value <= max_known:
            raw = value.to_bytes(length, "big")
            marker = 1 << (8 - length)
            return bytes([raw[0] | marker]) + raw[1:]
    raise ValueError("Valeur EBML trop grande")


def _ebml_element(element_id: bytes, payload: bytes) -> bytes:
    return element_id + _encode_ebml_size(len(payload)) + payload


def _make_mkv_with_attachments(
    path: Path,
    attachments: list[tuple[str, bytes]],
) -> None:
    attached_files = []
    for name, data in attachments:
        attached_payload = b"".join([
            _ebml_element(b"\x46\x6e", name.encode("utf-8")),
            _ebml_element(b"\x46\x60", b"application/octet-stream"),
            _ebml_element(b"\x46\x5c", data),
        ])
        attached_files.append(_ebml_element(b"\x61\xa7", attached_payload))
    segment_payload = _ebml_element(b"\x19\x41\xa4\x69", b"".join(attached_files))
    path.write_bytes(_ebml_element(b"\x18\x53\x80\x67", segment_payload))


def _track(
    tid: int,
    track_type: str = "audio",
    codec: str = "EAC3",
    file_id: str = "abc",
    enabled: bool = True,
    title: str = "",
    language: str = "fra",
    orig_title: str = "",
    orig_language: str = "fra",
) -> TrackEntry:
    return TrackEntry(
        mkv_tid=tid, track_type=track_type, codec=codec,
        display_info="5.1  48 kHz",
        language=language, title=title, enabled=enabled,
        file_id=file_id,
        orig_language=orig_language, orig_title=orig_title,
    )


def _source(
    path: Path,
    file_index: int,
    tracks: list[TrackEntry],
) -> SourceInput:
    return SourceInput(path=path, file_index=file_index, tracks=tracks)


def _workflow() -> RemuxWorkflow:
    """RemuxWorkflow instancié avec un binaire factice (tests sans exécution)."""
    return RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")


# ===========================================================================
# Helpers TMDB (saison/épisode)
# ===========================================================================

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("s01 e01", (1, 1)),
        (".s01.e01.", (1, 1)),
        (".s01e01.", (1, 1)),
        (".s1e1.", (1, 1)),
        (".01x01.", (1, 1)),
        (
            "Daredevil.Born.Again.S02E01.Le.Northern.Star.2160p.DSNP.WEB.DV.HDR.MULTi.AD.VFF.DDP.5.1.ATMOS.H265-HYDROMUX-Muxiveo",
            (2, 1),
        ),
        ("Movie.Without.Episode.Tag", None),
    ],
)
def test_extract_season_episode_supported_patterns(text: str, expected):
    assert extract_season_episode(text) == expected


def test_normalize_tmdb_manual_title_suggestion_cleans_release_noise():
    raw = "Fallout.S01E01.The.End.2160p.UHD.BluRay.HDR10.10Bit.AC-3.TrueHD7.1Atmos"
    assert _normalize_tmdb_manual_title_suggestion(raw) == "Fallout"


def test_normalize_tmdb_manual_title_suggestion_keeps_year():
    assert _normalize_tmdb_manual_title_suggestion("Inception (2010)") == "Inception 2010"


# ===========================================================================
# TrackEntry — propriétés
# ===========================================================================

class TestTrackEntryProperties:

    def test_type_label_video(self):
        t = _track(0, track_type="video")
        assert t.type_label == "V"

    def test_type_label_audio(self):
        t = _track(1, track_type="audio")
        assert t.type_label == "A"

    def test_type_label_subtitle(self):
        t = _track(2, track_type="subtitle")
        assert t.type_label == "S"

    def test_type_label_unknown(self):
        t = _track(3, track_type="attachment")
        assert t.type_label == "?"

    def test_type_long_video(self):
        assert _track(0, "video").type_long == "Vidéo"

    def test_type_long_audio(self):
        assert _track(1, "audio").type_long == "Audio"

    def test_type_long_subtitle(self):
        assert _track(2, "subtitle").type_long == "Sous-titre"

    def test_file_id_default_empty(self):
        t = TrackEntry(
            mkv_tid=0, track_type="video", codec="HEVC",
            display_info="1080p", language="", title="",
        )
        assert t.file_id == ""

    def test_enabled_default_true(self):
        t = TrackEntry(
            mkv_tid=0, track_type="video", codec="HEVC",
            display_info="", language="", title="",
        )
        assert t.enabled is True

    def test_original_codec_and_display_info_default_to_initial_values(self):
        t = _track(1, track_type="audio", codec="EAC3")
        assert t.orig_codec == "EAC3"
        assert t.orig_display_info == "5.1  48 kHz"

    def test_time_shift_value_label_empty_when_zero(self):
        t = _track(1, track_type="audio")
        t.time_shift_ms = 0
        assert t.time_shift_value_label == ""

    def test_time_shift_value_label_with_signed_ms(self):
        t = _track(1, track_type="audio")
        t.time_shift_ms = -80
        assert t.time_shift_value_label == "-80 ms"

    def test_time_shift_label_uses_delta_prefix(self):
        t = _track(1, track_type="audio")
        t.time_shift_ms = 125
        assert t.time_shift_label == "Δt +125 ms"

    def test_full_info_label_includes_offset_when_non_zero(self):
        t = _track(1, track_type="audio")
        t.display_info = "5.1"
        t.time_shift_ms = 125
        assert "Δt +125 ms" in t.full_info_label

    def test_full_info_label_prefixes_new_for_cloned_track(self):
        t = clone_track_entry(_track(1, track_type="audio"))
        assert t.is_new is True
        assert t.full_info_label.startswith("NEW")


# ===========================================================================
# tracks_from_file_info
# ===========================================================================

class TestTracksFromFileInfo:

    def test_file_id_propagated_to_all_tracks(self):
        info = _file_info(
            videos=[_video(0)], audios=[_audio(1)], subs=[_subtitle(2)]
        )
        entries = tracks_from_file_info(info, file_id="uuid-xyz")
        assert all(e.file_id == "uuid-xyz" for e in entries)

    def test_order_video_then_audio_then_subtitle(self):
        info = _file_info(
            videos=[_video(0)], audios=[_audio(1)], subs=[_subtitle(2)]
        )
        entries = tracks_from_file_info(info)
        types = [e.track_type for e in entries]
        assert types == ["video", "audio", "subtitle"]

    def test_video_display_info_includes_resolution(self):
        info = _file_info(videos=[_video(0, width=3840, height=2160)])
        entries = tracks_from_file_info(info)
        assert "3840×2160" in entries[0].display_info

    def test_video_display_info_includes_hdr_label(self):
        info = _file_info(videos=[_video(0, hdr_type=HDRType.HDR10)])
        entries = tracks_from_file_info(info)
        assert "HDR10" in entries[0].display_info

    def test_video_display_info_sdr_has_no_hdr_label(self):
        info = _file_info(videos=[_video(0, hdr_type=HDRType.NONE)])
        entries = tracks_from_file_info(info)
        assert "SDR" not in entries[0].display_info
        assert "HDR" not in entries[0].display_info

    def test_video_fps_decimal_formatted(self):
        info = _file_info(videos=[_video(0, frame_rate="25")])
        entries = tracks_from_file_info(info)
        assert "25 fps" in entries[0].display_info

    def test_video_fps_fractional_converted(self):
        info = _file_info(videos=[_video(0, frame_rate="24000/1001")])
        entries = tracks_from_file_info(info)
        # 24000/1001 ≈ 23.976
        assert "23.976 fps" in entries[0].display_info

    def test_video_display_info_excludes_bitrate(self):
        info = _file_info(videos=[_video(0, bit_rate=22_000_000)])
        entries = tracks_from_file_info(info)
        assert "kbps" not in entries[0].display_info

    def test_video_fps_zero_denominator_safe(self):
        info = _file_info(videos=[_video(0, frame_rate="24000/0")])
        # Ne doit pas lever ZeroDivisionError
        entries = tracks_from_file_info(info)
        assert entries[0].track_type == "video"

    def test_audio_display_info_includes_channels(self):
        info = _file_info(audios=[_audio(1, channels=6)])
        entries = tracks_from_file_info(info)
        assert "5.1" in entries[0].display_info

    def test_audio_display_info_excludes_sample_rate(self):
        # Le sample rate n'est plus inclus dans display_info (supprimé)
        info = _file_info(audios=[_audio(1, sample_rate=48000)])
        entries = tracks_from_file_info(info)
        assert "kHz" not in entries[0].display_info

    def test_audio_display_info_includes_bitrate(self):
        info = _file_info(audios=[_audio(1, bit_rate=640_000)])
        entries = tracks_from_file_info(info)
        assert "640 kbps" in entries[0].display_info

    def test_subtitle_forced_flag_in_full_info_label(self):
        # Les flags sont exposés via full_info_label (flags_label), pas display_info
        info = _file_info(subs=[_subtitle(2, forced=True)])
        entries = tracks_from_file_info(info)
        assert "forcé" in entries[0].full_info_label

    def test_subtitle_default_flag_in_full_info_label(self):
        info = _file_info(subs=[_subtitle(2, default=True)])
        entries = tracks_from_file_info(info)
        assert "défaut" in entries[0].full_info_label

    def test_subtitle_neither_forced_nor_default(self):
        info = _file_info(subs=[_subtitle(2, forced=False, default=False)])
        entries = tracks_from_file_info(info)
        assert entries[0].display_info == ""

    def test_subtitle_display_info_includes_element_count(self):
        info = _file_info(subs=[_subtitle(2, element_count=1292)])
        entries = tracks_from_file_info(info)
        assert entries[0].display_info == "1292 éléments"

    def test_empty_file_info_returns_empty_list(self):
        info = _file_info()
        assert tracks_from_file_info(info) == []

    def test_language_preserved_in_track_entry(self):
        info = _file_info(audios=[_audio(1, language="jpn")])
        entries = tracks_from_file_info(info)
        assert entries[0].language == "jpn"
        assert entries[0].orig_language == "jpn"

    def test_none_language_becomes_empty_string(self):
        info = _file_info(audios=[_audio(1, language=None)])
        entries = tracks_from_file_info(info)
        assert entries[0].language == ""


# ===========================================================================
# RemuxWorkflow.validate
# ===========================================================================

class TestValidate:

    def setup_method(self):
        self.wf = _workflow()

    def test_empty_sources_returns_one_error(self):
        cfg = RemuxConfig(
            sources=[], output=Path("/out.mkv"), track_order=[],
        )
        errors = self.wf.validate(cfg)
        assert len(errors) == 1
        assert "source" in errors[0].lower()

    def test_source_file_not_found(self, tmp_path):
        missing = tmp_path / "absent.mkv"
        tracks = [_track(0, "video", file_id="id0")]
        src = _source(missing, 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
        )
        errors = self.wf.validate(cfg)
        assert any("introuvable" in e for e in errors)

    def test_source_equals_output_error(self, tmp_path):
        f = tmp_path / "film.mkv"
        f.touch()
        tracks = [_track(0, "video", file_id="id0")]
        src = _source(f, 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=f,   # même chemin !
            track_order=[(0, 0)],
        )
        errors = self.wf.validate(cfg)
        assert any("différent" in e for e in errors)

    def test_output_dir_not_exists(self, tmp_path):
        f = tmp_path / "film.mkv"
        f.touch()
        tracks = [_track(0, "video", file_id="id0")]
        src = _source(f, 0, tracks)
        cfg = RemuxConfig(
            sources=[src],
            output=tmp_path / "inexistant_subdir" / "out.mkv",
            track_order=[(0, 0)],
        )
        errors = self.wf.validate(cfg)
        assert any("inexistant" in e or "sortie" in e.lower() for e in errors)

    def test_output_dir_not_writable(self, tmp_path):
        f = tmp_path / "film.mkv"
        f.touch()
        tracks = [_track(0, "video", file_id="id0")]
        src = _source(f, 0, tracks)
        cfg = RemuxConfig(
            sources=[src],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
        )
        with patch(
            "core.workflows.remux_mapping.tempfile.NamedTemporaryFile",
            side_effect=OSError("blocked"),
        ):
            errors = self.wf.validate(cfg)
        assert any("inscriptible" in e.lower() for e in errors)

    def test_empty_track_order_error(self, tmp_path):
        f = tmp_path / "film.mkv"
        f.touch()
        tracks = [_track(0, "video", file_id="id0")]
        src = _source(f, 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=tmp_path / "out.mkv",
            track_order=[],
        )
        errors = self.wf.validate(cfg)
        assert any("piste" in e.lower() for e in errors)

    def test_validate_rejects_negative_video_offset(self, tmp_path):
        f = tmp_path / "film.mkv"
        f.touch()
        v = _track(0, "video", file_id="id0")
        v.time_shift_ms = -40
        src = _source(f, 0, [v])
        cfg = RemuxConfig(
            sources=[src],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
        )
        errors = self.wf.validate(cfg)
        assert any("vidéo" in e.lower() and "négatif" in e.lower() for e in errors)

    def test_valid_config_returns_empty_list(self, tmp_path):
        f = tmp_path / "film.mkv"
        f.touch()
        tracks = [_track(0, "video", file_id="id0")]
        src = _source(f, 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
        )
        errors = self.wf.validate(cfg)
        assert errors == []

    def test_multiple_errors_all_returned(self, tmp_path):
        """Fichier manquant + track_order vide → au moins 2 erreurs."""
        missing = tmp_path / "absent.mkv"
        tracks = [_track(0, "video", file_id="id0")]
        src = _source(missing, 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=tmp_path / "out.mkv",
            track_order=[],
        )
        errors = self.wf.validate(cfg)
        assert len(errors) >= 2


# ===========================================================================
# _TrackTable (widget Qt — nécessite QApplication)
# ===========================================================================

@pytest.fixture
def table(qt_app):
    """_TrackTable instanciée pour les tests (sans parent)."""
    t = _TrackTable()
    yield t
    t.close()


_COLOR_A = "#4f6ef7"   # couleur de test pour file_id "fid" / "A"
_COLOR_B = "#e05050"   # couleur de test pour file_id "B"


def _fill_table(
    table: _TrackTable,
    n: int = 3,
    file_id: str = "fid",
    track_type: str = "audio",
    color: str = _COLOR_A,
) -> list[TrackEntry]:
    """Ajoute n pistes du type donné dans la table et retourne les TrackEntry."""
    tracks = [
        _track(i, track_type, file_id=file_id, language="fra", title=f"Piste {i}")
        for i in range(n)
    ]
    table.append_tracks(color, tracks)
    return tracks


def _action_buttons(table: _TrackTable, row: int) -> list[QPushButton]:
    widget = table.cellWidget(row, _TrackTable.COL_EDIT)
    assert widget is not None
    return widget.findChildren(QPushButton)


class TestTrackTable:

    def test_append_tracks_adds_correct_row_count(self, table):
        _fill_table(table, 3)
        assert table.rowCount() == 3

    def test_append_tracks_preserves_existing_rows(self, table):
        _fill_table(table, 2, file_id="A")
        _fill_table(table, 3, file_id="B")
        assert table.rowCount() == 5

    def test_drop_moves_only_the_selected_row(self, table, monkeypatch):
        tracks = _fill_table(table, 3)
        second_row_actions = table.cellWidget(1, _TrackTable.COL_EDIT)
        assert second_row_actions is not None
        table.selectRow(0)

        class _DropEvent:
            accepted = False

            @staticmethod
            def source():
                return table

            @staticmethod
            def position():
                return QPointF(0, 0)

            def setDropAction(self, _action):
                pass

            def accept(self):
                self.accepted = True

            def ignore(self):
                raise AssertionError("Le drop interne ne doit pas être ignoré")

        monkeypatch.setattr(table, "_drop_target_row", lambda _event: 3)
        event = _DropEvent()
        table.dropEvent(event)

        assert [entry.entry_id for entry in table.current_tracks()] == [
            tracks[1].entry_id,
            tracks[2].entry_id,
            tracks[0].entry_id,
        ]
        assert table.cellWidget(0, _TrackTable.COL_EDIT) is second_row_actions
        assert table.cellWidget(2, _TrackTable.COL_EDIT) is not None
        assert table.consume_order_changed_track_types() == frozenset({"audio"})
        assert event.accepted is True

    def test_drop_across_track_types_skips_unchanged_encode_projections(self, table, monkeypatch):
        video = _track(0, "video")
        audio = _track(1, "audio")
        table.append_tracks(_COLOR_A, [video, audio])
        table.selectRow(1)

        class _DropEvent:
            @staticmethod
            def source():
                return table

            @staticmethod
            def position():
                return QPointF(0, 0)

            @staticmethod
            def setDropAction(_action):
                pass

            @staticmethod
            def accept():
                pass

            @staticmethod
            def ignore():
                raise AssertionError("Le drop interne ne doit pas être ignoré")

        monkeypatch.setattr(table, "_drop_target_row", lambda _event: 0)
        table.dropEvent(_DropEvent())

        assert [entry.entry_id for entry in table.current_tracks()] == [
            audio.entry_id,
            video.entry_id,
        ]
        assert table.consume_order_changed_track_types() == frozenset()

    def test_remove_tracks_by_file_id_removes_only_target(self, table):
        _fill_table(table, 2, file_id="A")
        _fill_table(table, 3, file_id="B")
        table.remove_tracks_by_file_id("A")
        assert table.rowCount() == 3

    def test_remove_tracks_by_file_id_leaves_other_ids(self, table):
        _fill_table(table, 2, file_id="A")
        _fill_table(table, 1, file_id="B")
        table.remove_tracks_by_file_id("A")
        remaining = table.current_tracks()
        assert all(t.file_id == "B" for t in remaining)

    def test_remove_tracks_by_unknown_file_id_is_noop(self, table):
        _fill_table(table, 3, file_id="A")
        table.remove_tracks_by_file_id("nonexistent")
        assert table.rowCount() == 3

    def test_clear_all_empties_table(self, table):
        _fill_table(table, 4)
        table.clear_all()
        assert table.rowCount() == 0

    def test_current_tracks_returns_all_tracks(self, table):
        _fill_table(table, 3)
        tracks = table.current_tracks()
        assert len(tracks) == 3

    def test_current_tracks_preserves_file_id(self, table):
        _fill_table(table, 2, file_id="uuid-test")
        tracks = table.current_tracks()
        assert all(t.file_id == "uuid-test" for t in tracks)

    def test_current_tracks_syncs_enabled_state(self, table):
        """Décocher une case → enabled False dans current_tracks()."""
        _fill_table(table, 1)
        # Décoche la case à cocher (col COL_CHECK = 1)
        item = table.item(0, _TrackTable.COL_CHECK)
        item.setCheckState(Qt.CheckState.Unchecked)
        tracks = table.current_tracks()
        assert tracks[0].enabled is False

    def test_current_tracks_syncs_language(self, table):
        """Modifier la cellule Langue → language mis à jour dans current_tracks()."""
        _fill_table(table, 1)
        lang_item = table.item(0, _TrackTable.COL_LANG)
        lang_item.setText("zh-CN")
        tracks = table.current_tracks()
        assert tracks[0].language == "zh-CN"

    def test_current_tracks_syncs_title(self, table):
        _fill_table(table, 1)
        title_item = table.item(0, _TrackTable.COL_TITLE)
        title_item.setText("Nouveau titre")
        tracks = table.current_tracks()
        assert tracks[0].title == "Nouveau titre"

    def test_set_all_enabled_checks_all(self, table):
        """set_all_enabled(True) → toutes les cases cochées."""
        _fill_table(table, 3)
        # Décoche quelques cases d'abord
        table.item(0, _TrackTable.COL_CHECK).setCheckState(Qt.CheckState.Unchecked)
        table.item(2, _TrackTable.COL_CHECK).setCheckState(Qt.CheckState.Unchecked)

        table.set_all_enabled(True)
        result = table.current_tracks()
        assert all(t.enabled for t in result)

    def test_set_all_enabled_unchecks_all(self, table):
        """set_all_enabled(False) → toutes les cases décochées."""
        _fill_table(table, 3)
        table.set_all_enabled(False)
        result = table.current_tracks()
        assert all(not t.enabled for t in result)

    def test_source_column_shows_block_char(self, table):
        """La colonne Source affiche le caractère bloc '█' (carré coloré)."""
        tracks = [_track(0, "audio", file_id="fid")]
        table.append_tracks(_COLOR_A, tracks)
        src_item = table.item(0, _TrackTable.COL_SOURCE)
        assert src_item is not None
        assert src_item.text() == "█"

    def test_source_column_stores_color_in_user_role(self, table):
        """La couleur doit être stockée dans UserRole pour reconstruction drag-drop."""
        tracks = [_track(0, "audio", file_id="fid")]
        table.append_tracks(_COLOR_A, tracks)
        src_item = table.item(0, _TrackTable.COL_SOURCE)
        assert src_item.data(Qt.ItemDataRole.UserRole) == _COLOR_A

    def test_source_column_foreground_matches_color(self, table):
        """La couleur de premier plan de la cellule correspond à source_color."""
        tracks = [_track(0, "audio", file_id="fid")]
        table.append_tracks(_COLOR_A, tracks)
        src_item = table.item(0, _TrackTable.COL_SOURCE)
        assert src_item.foreground().color() == QColor(_COLOR_A)

    def test_type_column_shows_correct_label(self, table):
        tracks = [_track(0, "video", file_id="fid")]
        table.append_tracks(_COLOR_A, tracks)
        type_item = table.item(0, _TrackTable.COL_TYPE)
        assert type_item.text() == "V"

    def test_append_tracks_default_enabled(self, table):
        """Les pistes ajoutées sont cochées par défaut."""
        _fill_table(table, 1)
        item = table.item(0, _TrackTable.COL_CHECK)
        assert item.checkState() == Qt.CheckState.Checked

    def test_info_column_stores_offset_value_role(self, table):
        t = _track(1, "audio", file_id="fid")
        t.time_shift_ms = 125
        table.append_tracks(_COLOR_A, [t])
        info_item = table.item(0, _TrackTable.COL_INFO)
        assert info_item is not None
        assert info_item.text().endswith("Δt +125 ms")
        assert info_item.data(_TRACK_INFO_OFFSET_VALUE_ROLE) == "+125 ms"

    def test_info_column_hides_zero_offset(self, table):
        t = _track(1, "audio", file_id="fid")
        t.time_shift_ms = 0
        table.append_tracks(_COLOR_A, [t])
        info_item = table.item(0, _TrackTable.COL_INFO)
        assert info_item is not None
        assert "Δt" not in info_item.text()
        assert info_item.data(_TRACK_INFO_OFFSET_VALUE_ROLE) == ""

    def test_update_time_shift_updates_info_column(self, table):
        t = _track(1, "audio", file_id="fid")
        table.append_tracks(_COLOR_A, [t])

        assert table.update_time_shift(t.entry_id, -320) is True

        info_item = table.item(0, _TrackTable.COL_INFO)
        assert t.time_shift_ms == -320
        assert info_item.text().endswith("Δt -320 ms")
        assert info_item.data(_TRACK_INFO_OFFSET_VALUE_ROLE) == "-320 ms"

    def test_auto_synced_audio_track_shows_cancel_button(self, table):
        t = _track(1, "audio", file_id="fid")
        table.append_tracks(_COLOR_A, [t])
        table.set_audio_sync_available(True)
        assert "Synchronisation améliorée de cette piste audio" in [
            button.toolTip() for button in _action_buttons(table, 0)
        ]

        table.set_auto_sync_cancelable_entries({t.entry_id})
        table.update_time_shift(t.entry_id, -320)

        cancel_tooltip = translate_text("Annuler la synchro")
        assert cancel_tooltip in [
            button.toolTip() for button in _action_buttons(table, 0)
        ]

    def test_auto_sync_cancel_button_emits_only_row_entry(self, table):
        first, second = _fill_table(table, 2, file_id="fid")
        table.set_audio_sync_available(True)
        table.set_auto_sync_cancelable_entries({first.entry_id})
        table.update_time_shift(first.entry_id, -320)
        table.update_time_shift(second.entry_id, -320)
        emitted: list[TrackEntry] = []
        table.auto_sync_cancel_requested.connect(emitted.append)

        cancel_tooltip = translate_text("Annuler la synchro")
        cancel_button = next(
            button
            for button in _action_buttons(table, 0)
            if button.toolTip() == cancel_tooltip
        )
        cancel_button.click()

        assert emitted == [first]
        assert "Synchronisation améliorée de cette piste audio" in [
            button.toolTip() for button in _action_buttons(table, 1)
        ]

    def test_auto_synced_subtitle_track_shows_cancel_button(self, table):
        t = _track(2, "subtitle", file_id="fid", codec="SRT")
        table.append_tracks(_COLOR_A, [t])
        table.set_auto_sync_cancelable_entries({t.entry_id})
        table.update_time_shift(t.entry_id, -320)

        cancel_tooltip = translate_text("Annuler la synchro")
        assert cancel_tooltip in [
            button.toolTip() for button in _action_buttons(table, 0)
        ]

    def test_sync_rewrite_label_click_emits_toggle_for_eligible_track(self, table):
        table.set_sync_rewrite_enabled(True)
        t = _track(1, "audio", file_id="fid", codec="EAC3")
        t.time_shift_ms = 125
        table.append_tracks(_COLOR_A, [t])
        info_item = table.item(0, _TrackTable.COL_INFO)
        assert info_item is not None
        assert info_item.data(_TRACK_INFO_SYNC_LABEL_ROLE) == "Sync réelle · audio réencodé"
        emitted: list[TrackEntry] = []
        table.sync_rewrite_toggle_requested.connect(emitted.append)

        table._on_cell_clicked(0, _TrackTable.COL_INFO)

        assert emitted == [t]

    def test_advanced_sync_track_shows_warning_icon_tooltip(self, table):
        table.set_sync_rewrite_enabled(True, advanced_audio_enabled=True)
        t = _track(1, "audio", file_id="fid", codec="TRUEHD")
        t.time_shift_ms = 125
        table.append_tracks(_COLOR_A, [t])

        info_item = table.item(0, _TrackTable.COL_INFO)
        assert info_item is not None
        assert info_item.data(_TRACK_INFO_SYNC_LABEL_ROLE) == "Sync réelle · audio avancé"
        tooltips = [button.toolTip() for button in _action_buttons(table, 0)]
        warning_tip = next(tip for tip in tooltips if "Sync réelle audio avancée" in tip)
        assert "TrueHD" in warning_tip
        assert "Δt +125 ms" in warning_tip
        assert "non bit-perfect" in warning_tip

    def test_advanced_sync_warning_hides_when_forced_offset(self, table):
        table.set_sync_rewrite_enabled(True, advanced_audio_enabled=True)
        t = _track(1, "audio", file_id="fid", codec="TRUEHD")
        t.time_shift_ms = 125
        table.append_tracks(_COLOR_A, [t])

        t.sync_rewrite_mode = "offset"
        assert table.refresh_entry_info(t.entry_id) is True

        info_item = table.item(0, _TrackTable.COL_INFO)
        assert info_item is not None
        assert info_item.data(_TRACK_INFO_SYNC_LABEL_ROLE) == "Sync offset"
        assert all("Sync réelle audio avancée" not in button.toolTip() for button in _action_buttons(table, 0))

    def test_info_column_uses_custom_delegate(self, table):
        delegate = table.itemDelegateForColumn(_TrackTable.COL_INFO)
        assert delegate is not None
        assert delegate.__class__.__name__ == "_TrackInfoDelegate"

    def test_offset_negative_color_constant_is_fixed_red(self):
        assert _TRACK_INFO_OFFSET_NEG_COLOR.name().lower() == "#d92f2f"
        assert _TRACK_INFO_OFFSET_COLOR.name().lower() == "#d92f2f"

    def test_offset_positive_color_constant_is_fixed_green(self):
        assert _TRACK_INFO_OFFSET_POS_COLOR.name().lower() == "#1f9d55"

    def test_offset_delegate_color_depends_on_sign(self):
        assert _TrackInfoDelegate._offset_color("-80 ms").name().lower() == "#d92f2f"
        assert _TrackInfoDelegate._offset_color("+125 ms").name().lower() == "#1f9d55"


# ===========================================================================
# TrackEditDialog
# ===========================================================================

class TestTrackEditDialog:

    def test_accept_persists_signed_offset(self, qt_app):
        entry = _track(1, "audio", file_id="fid")
        dlg = TrackEditDialog(entry)
        dlg._offset_edit.setText("+125")
        dlg.accept()
        assert entry.time_shift_ms == 125
        dlg.close()

    def test_accept_empty_offset_normalizes_to_zero(self, qt_app):
        entry = _track(1, "audio", file_id="fid")
        entry.time_shift_ms = 250
        dlg = TrackEditDialog(entry)
        dlg._offset_edit.setText("")
        dlg.accept()
        assert entry.time_shift_ms == 0
        dlg.close()

    def test_accept_rejects_invalid_offset(self, qt_app):
        entry = _track(1, "audio", file_id="fid")
        entry.time_shift_ms = 42
        dlg = TrackEditDialog(entry)
        dlg._offset_edit.setText("12.5")

        with patch("ui.panels.track_edit_dialog.QMessageBox.warning") as warn:
            dlg.accept()

        warn.assert_called_once()
        assert entry.time_shift_ms == 42
        dlg.close()


# ===========================================================================
# _pick_file_color
# ===========================================================================

class TestPickFileColor:

    def test_returns_hex_string(self):
        c = _pick_file_color(0)
        assert c.startswith("#")
        assert len(c) == 7

    def test_deterministic(self):
        assert _pick_file_color(3) == _pick_file_color(3)

    def test_different_indices_different_colors(self):
        colors = [_pick_file_color(i) for i in range(10)]
        assert len(set(colors)) == 10

    def test_not_near_black(self):
        """Luminosité > 0.1 pour tous les indices 0–19."""
        for i in range(20):
            c = _pick_file_color(i)
            r = int(c[1:3], 16) / 255
            g = int(c[3:5], 16) / 255
            b = int(c[5:7], 16) / 255
            _, lightness, _ = colorsys.rgb_to_hls(r, g, b)
            assert lightness > 0.1, (
                f"index {i}: couleur {c} trop sombre (l={lightness:.2f})"
            )

    def test_not_near_white(self):
        """Luminosité < 0.9 pour tous les indices 0–19."""
        for i in range(20):
            c = _pick_file_color(i)
            r = int(c[1:3], 16) / 255
            g = int(c[3:5], 16) / 255
            b = int(c[5:7], 16) / 255
            _, lightness, _ = colorsys.rgb_to_hls(r, g, b)
            assert lightness < 0.9, (
                f"index {i}: couleur {c} trop claire (l={lightness:.2f})"
            )

    def test_golden_angle_spread(self):
        """Les 8 premières couleurs couvrent le cercle chromatique (plage > 200°)."""
        hues = []
        for i in range(8):
            c = _pick_file_color(i)
            r = int(c[1:3], 16) / 255
            g = int(c[3:5], 16) / 255
            b = int(c[5:7], 16) / 255
            h, _, _ = colorsys.rgb_to_hls(r, g, b)
            hues.append(h * 360)
        span = max(hues) - min(hues)
        assert span > 200, f"plage de teinte insuffisante : {span:.1f}°"


# ===========================================================================
# SourceFile.color
# ===========================================================================

class TestSourceFileColor:

    def test_color_default_empty(self):
        sf = SourceFile(id="x", path=Path("/tmp/film.mkv"))
        assert sf.color == ""

    def test_color_can_be_set(self):
        sf = SourceFile(id="x", path=Path("/tmp/film.mkv"), color="#aabbcc")
        assert sf.color == "#aabbcc"


# ===========================================================================
# _TrackTable — ordre des colonnes
# ===========================================================================

class TestTrackTableColumnOrder:

    def test_col_lang_is_4(self):
        assert _TrackTable.COL_LANG == 4

    def test_col_title_is_5(self):
        assert _TrackTable.COL_TITLE == 5

    def test_col_info_is_6(self):
        assert _TrackTable.COL_INFO == 6

    def test_header_langue_at_index_4(self):
        assert _TrackTable._HEADERS[4] == "Langue"

    def test_header_titre_at_index_5(self):
        assert _TrackTable._HEADERS[5] == "Titre"

    def test_header_info_at_index_6(self):
        assert _TrackTable._HEADERS[6] == "Info"


# ===========================================================================
# _TrackTable — tri par type (V → A → S)
# ===========================================================================

class TestTrackTableSort:

    def test_video_inserted_before_audio(self, table):
        audio = _track(1, "audio", file_id="f1")
        video = _track(0, "video", file_id="f1")
        table.append_tracks(_COLOR_A, [audio])
        table.append_tracks(_COLOR_A, [video])
        tracks = table.current_tracks()
        types = [t.track_type for t in tracks]
        assert types == ["video", "audio"]

    def test_audio_inserted_before_subtitle(self, table):
        sub = _track(2, "subtitle", file_id="f1")
        audio = _track(1, "audio", file_id="f1")
        table.append_tracks(_COLOR_A, [sub])
        table.append_tracks(_COLOR_A, [audio])
        tracks = table.current_tracks()
        types = [t.track_type for t in tracks]
        assert types == ["audio", "subtitle"]

    def test_full_order_v_a_s(self, table):
        entries = [
            _track(2, "subtitle", file_id="f1"),
            _track(1, "audio", file_id="f1"),
            _track(0, "video", file_id="f1"),
        ]
        table.append_tracks(_COLOR_A, entries)
        types = [t.track_type for t in table.current_tracks()]
        assert types == ["video", "audio", "subtitle"]

    def test_multifile_grouped_by_type(self, table):
        """V-f1, V-f2, A-f1, A-f2 : les vidéos précèdent tous les audios."""
        v1 = _track(0, "video", file_id="f1")
        a1 = _track(1, "audio", file_id="f1")
        v2 = _track(0, "video", file_id="f2")
        a2 = _track(1, "audio", file_id="f2")
        table.append_tracks(_COLOR_A, [v1, a1])
        table.append_tracks(_COLOR_B, [v2, a2])
        types = [t.track_type for t in table.current_tracks()]
        assert types == ["video", "video", "audio", "audio"]

    def test_find_insert_position_video_empty_table(self, table):
        assert table._find_insert_position(0) == 0   # order 0 = video

    def test_find_insert_position_audio_after_video(self, table):
        _fill_table(table, 2, track_type="video")
        assert table._find_insert_position(1) == 2   # order 1 = audio

    def test_find_insert_position_subtitle_after_audio(self, table):
        _fill_table(table, 1, track_type="video")
        _fill_table(table, 2, track_type="audio")
        assert table._find_insert_position(2) == 3   # order 2 = subtitle


# ===========================================================================
# _TrackTable — filtre "sélectionnées seulement"
# ===========================================================================

class TestTrackTableFilter:

    def test_filter_off_all_rows_visible(self, table):
        _fill_table(table, 3)
        table.set_filter_selected(False)
        hidden = [table.isRowHidden(r) for r in range(table.rowCount())]
        assert not any(hidden)

    def test_filter_on_hides_unchecked_rows(self, table):
        _fill_table(table, 3)
        table.item(1, _TrackTable.COL_CHECK).setCheckState(Qt.CheckState.Unchecked)
        table.set_filter_selected(True)
        assert table.isRowHidden(1)
        assert not table.isRowHidden(0)
        assert not table.isRowHidden(2)

    def test_filter_on_checked_rows_remain_visible(self, table):
        _fill_table(table, 3)
        table.set_filter_selected(True)
        hidden = [table.isRowHidden(r) for r in range(table.rowCount())]
        assert not any(hidden)

    def test_disable_filter_restores_hidden_rows(self, table):
        _fill_table(table, 3)
        table.item(0, _TrackTable.COL_CHECK).setCheckState(Qt.CheckState.Unchecked)
        table.set_filter_selected(True)
        assert table.isRowHidden(0)
        table.set_filter_selected(False)
        assert not table.isRowHidden(0)

    def test_refresh_filter_hides_newly_unchecked(self, table):
        _fill_table(table, 2)
        table.set_filter_selected(True)
        table.item(0, _TrackTable.COL_CHECK).setCheckState(Qt.CheckState.Unchecked)
        table.refresh_filter()
        assert table.isRowHidden(0)
        assert not table.isRowHidden(1)


# ===========================================================================
# _TrackTable — ajustement de hauteur (_adjust_height)
# ===========================================================================

class TestTrackTableHeight:

    def test_empty_table_has_positive_height(self, table):
        assert table.height() > 0

    def test_adding_rows_increases_height(self, table):
        h0 = table.height()
        _fill_table(table, 5)
        assert table.height() > h0

    def test_height_capped_at_15_rows(self, table):
        _fill_table(table, 15)
        h15 = table.height()
        _fill_table(table, 5)   # 20 rows total
        h20 = table.height()
        assert h15 == h20, "La hauteur doit être plafonnée à 15 lignes visibles"

    def test_removing_rows_reduces_height(self, table):
        _fill_table(table, 5, file_id="A")
        h5 = table.height()
        table.remove_tracks_by_file_id("A")
        assert table.height() < h5

    def test_clear_all_resets_to_base_height(self, table):
        h0 = table.height()
        _fill_table(table, 5)
        table.clear_all()
        assert table.height() == h0


# ===========================================================================
# _FileListWidget — taille automatique
# ===========================================================================

@pytest.fixture
def file_list(qt_app):
    w = _FileListWidget()
    yield w
    w.close()


def _make_sf(idx: int = 0, color: str = "#ff0000") -> SourceFile:
    return SourceFile(
        id=f"id-{idx}",
        path=Path(f"/tmp/film{idx}.mkv"),
        color=color,
    )


class TestFileListWidgetSize:
    """
    Vérifie la contrainte de hauteur fixe posée par setFixedHeight.

    On utilise maximumHeight() (mis à jour immédiatement par setFixedHeight)
    plutôt que height() (qui n'est recalculé qu'après un cycle de layout).
    """

    def test_initial_height_is_placeholder_plus_bar(self, file_list):
        expected = _FILE_PH_H + _FILE_BAR_H
        assert file_list.maximumHeight() == expected

    def test_one_file_height(self, file_list):
        file_list.add_file(_make_sf(0))
        expected = 1 * _FILE_ROW_H + _FILE_BAR_H
        assert file_list.maximumHeight() == expected

    def test_three_files_height(self, file_list):
        for i in range(3):
            file_list.add_file(_make_sf(i))
        expected = 3 * _FILE_ROW_H + _FILE_BAR_H
        assert file_list.maximumHeight() == expected

    def test_remove_reduces_height(self, file_list):
        sf0 = _make_sf(0)
        sf1 = _make_sf(1)
        file_list.add_file(sf0)
        file_list.add_file(sf1)
        file_list.remove_file(sf0.id)
        expected = 1 * _FILE_ROW_H + _FILE_BAR_H
        assert file_list.maximumHeight() == expected

    def test_drop_on_visible_scroll_viewport_is_forwarded(self, file_list, tmp_path):
        """Le viewport recouvre la zone après la première source."""
        file_list.add_file(_make_sf(0))
        source = tmp_path / "second.mkv"
        source.touch()
        received: list[list[str]] = []
        file_list.add_requested.connect(received.append)

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(source))])
        viewport = file_list._scroll.viewport()
        enter = QDragEnterEvent(
            QPoint(4, 4),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(viewport, enter)
        drop = QDropEvent(
            QPoint(4, 4),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(viewport, drop)

        assert enter.isAccepted()
        assert drop.isAccepted()
        assert received == [[str(source)]]

    def test_remove_last_restores_placeholder_height(self, file_list):
        sf = _make_sf(0)
        file_list.add_file(sf)
        file_list.remove_file(sf.id)
        expected = _FILE_PH_H + _FILE_BAR_H
        assert file_list.maximumHeight() == expected

    def test_file_count_tracks_additions(self, file_list):
        assert file_list.file_count() == 0
        file_list.add_file(_make_sf(0))
        file_list.add_file(_make_sf(1))
        assert file_list.file_count() == 2

    def test_file_count_tracks_removals(self, file_list):
        sf = _make_sf(0)
        file_list.add_file(sf)
        file_list.remove_file(sf.id)
        assert file_list.file_count() == 0

    def test_accepts_external_srt_sources(self, file_list):
        assert ".srt" in _ACCEPTED_EXT


# ===========================================================================
# _TrackTable.update_audio_meta (synchronisation depuis EncodePanel)
# ===========================================================================

class TestTrackTableUpdateAudioMeta:

    def test_updates_language_cell(self, table):
        """update_audio_meta met à jour la cellule Langue."""
        tracks = [_track(5, "audio", file_id="fid", language="fra")]
        table.append_tracks(_COLOR_A, tracks)
        table.update_audio_meta("fid", 5, "jpn", "")
        lang_item = table.item(0, _TrackTable.COL_LANG)
        assert lang_item.text() == "jpn"

    def test_updates_title_cell(self, table):
        """update_audio_meta met à jour la cellule Titre."""
        tracks = [_track(5, "audio", file_id="fid", title="Original")]
        table.append_tracks(_COLOR_A, tracks)
        table.update_audio_meta("fid", 5, "fra", "Nouveau titre")
        title_item = table.item(0, _TrackTable.COL_TITLE)
        assert title_item.text() == "Nouveau titre"

    def test_updates_track_entry_language(self, table):
        """L'objet TrackEntry est aussi mis à jour (pas seulement la cellule UI)."""
        tracks = [_track(5, "audio", file_id="fid", language="fra")]
        table.append_tracks(_COLOR_A, tracks)
        table.update_audio_meta("fid", 5, "eng", "")
        result = table.current_tracks()
        assert result[0].language == "eng"

    def test_updates_track_entry_title(self, table):
        tracks = [_track(5, "audio", file_id="fid", title="Avant")]
        table.append_tracks(_COLOR_A, tracks)
        table.update_audio_meta("fid", 5, "fra", "Après")
        result = table.current_tracks()
        assert result[0].title == "Après"

    def test_no_item_changed_signal_emitted(self, table):
        """update_audio_meta doit bloquer les signaux pour éviter une boucle de sync."""
        tracks = [_track(5, "audio", file_id="fid")]
        table.append_tracks(_COLOR_A, tracks)
        emitted: list = []
        table.itemChanged.connect(lambda _: emitted.append(True))
        table.update_audio_meta("fid", 5, "jpn", "X")
        assert emitted == []

    def test_does_not_affect_other_rows(self, table):
        """Seule la ligne ciblée est modifiée, les autres restent intactes."""
        t0 = _track(1, "audio", file_id="fid", language="fra", title="Piste 1")
        t1 = _track(2, "audio", file_id="fid", language="eng", title="Piste 2")
        table.append_tracks(_COLOR_A, [t0, t1])
        table.update_audio_meta("fid", 1, "jpn", "Modifiée")
        result = table.current_tracks()
        other = next(t for t in result if t.mkv_tid == 2)
        assert other.language == "eng"
        assert other.title == "Piste 2"

    def test_unknown_file_id_is_noop(self, table):
        """file_id inconnu → aucune modification."""
        tracks = [_track(1, "audio", file_id="fid", language="fra")]
        table.append_tracks(_COLOR_A, tracks)
        table.update_audio_meta("unknown", 1, "jpn", "X")
        result = table.current_tracks()
        assert result[0].language == "fra"

    def test_unknown_mkv_tid_is_noop(self, table):
        """mkv_tid inconnu → aucune modification."""
        tracks = [_track(1, "audio", file_id="fid", language="fra")]
        table.append_tracks(_COLOR_A, tracks)
        table.update_audio_meta("fid", 99, "jpn", "X")
        result = table.current_tracks()
        assert result[0].language == "fra"

    def test_entry_id_targets_cloned_track_without_touching_source(self, table):
        source = _track(1, "audio", file_id="fid", language="fra", title="Source")
        clone = clone_track_entry(source)
        table.append_tracks(_COLOR_A, [source, clone])

        table.update_audio_meta("fid", 1, "jpn", "Clone", entry_id=clone.entry_id)

        result = table.current_tracks()
        original = next(t for t in result if t.entry_id == source.entry_id)
        updated = next(t for t in result if t.entry_id == clone.entry_id)
        assert original.language == "fra"
        assert original.title == "Source"
        assert updated.language == "jpn"
        assert updated.title == "Clone"

    def test_update_audio_encoding_updates_codec_and_info_cells(self, table):
        track = _track(1, "audio", file_id="fid", codec="EAC3")
        table.append_tracks(_COLOR_A, [track])

        changed = table.update_audio_encoding(track.entry_id, "AAC", "5.1  384 kbps")

        assert changed is True
        assert table.item(0, _TrackTable.COL_CODEC).text() == "AAC"
        assert table.item(0, _TrackTable.COL_INFO).text() == "5.1  384 kbps"
        assert track.codec == "AAC"
        assert track.display_info == "5.1  384 kbps"

    def test_update_video_encoding_plan_updates_codec_cell_and_state(self, table):
        track = _track(0, "video", file_id="fid", codec="HEVC")
        table.append_tracks(_COLOR_A, [track])

        changed = table.update_video_encoding_plans({
            track.entry_id: "libx265",
        })

        assert changed is True
        assert table.item(0, _TrackTable.COL_CODEC).text() == "HEVC"
        assert track.encode_plan_codec == "libx265"
        assert track.encode_plan_modified is True

    def test_update_video_encoding_plan_is_per_track(self, table):
        first = _track(0, "video", file_id="fid", codec="HEVC")
        second = _track(1, "video", file_id="fid", codec="H264")
        table.append_tracks(_COLOR_A, [first, second])

        changed = table.update_video_encoding_plans({
            first.entry_id: "libx265",
            second.entry_id: "copy",
        })

        assert changed is True
        assert table.item(0, _TrackTable.COL_CODEC).text() == "HEVC"
        assert table.item(1, _TrackTable.COL_CODEC).text() == "H264"
        assert first.encode_plan_codec == "libx265"
        assert second.encode_plan_codec == "copy"

    def test_update_video_encoding_plan_clear_missing_resets_codec_cell_and_state(self, table):
        track = _track(0, "video", file_id="fid", codec="HEVC")
        table.append_tracks(_COLOR_A, [track])
        table.update_video_encoding_plans({
            track.entry_id: "copy",
        })

        changed = table.update_video_encoding_plans({}, clear_missing=True)

        assert changed is True
        assert table.item(0, _TrackTable.COL_CODEC).text() == "HEVC"
        assert track.encode_plan_codec == ""
        assert track.encode_plan_summary == ""
        assert track.encode_plan_hdr_badges == ()
        assert track.encode_plan_modified is False

    def test_video_codec_cell_stays_normal_when_copy_untouched(self, table):
        track = _track(0, "video", file_id="fid", codec="HEVC")
        table.append_tracks(_COLOR_A, [track])

        codec_item = table.item(0, _TrackTable.COL_CODEC)
        assert codec_item is not None
        assert codec_item.foreground().color().name().lower() != table._VIDEO_ENCODE_COLOR.name().lower()
        assert codec_item.font().bold() is False

    def test_video_row_turns_blue_and_bold_when_encode_codec_is_set(self, table):
        track = _track(0, "video", file_id="fid", codec="HEVC")
        table.append_tracks(_COLOR_A, [track])

        table.update_video_encoding_plans({track.entry_id: "libx265"})

        for col in (
            _TrackTable.COL_TYPE,
            _TrackTable.COL_LANG,
            _TrackTable.COL_TITLE,
            _TrackTable.COL_INFO,
        ):
            item = table.item(0, col)
            assert item is not None
            assert item.foreground().color().name().lower() == table._VIDEO_ENCODE_COLOR.name().lower()
            assert item.font().bold() is True
        codec_item = table.item(0, _TrackTable.COL_CODEC)
        assert codec_item is not None
        assert codec_item.foreground().color().name().lower() == table._VIDEO_ENCODE_CODEC_COLOR.name().lower()
        assert codec_item.font().bold() is True

    def test_video_row_rolls_back_when_codec_returns_to_copy(self, table):
        track = _track(0, "video", file_id="fid", codec="HEVC")
        table.append_tracks(_COLOR_A, [track])
        table.update_video_encoding_plans({track.entry_id: "libx265"})

        table.update_video_encoding_plans({track.entry_id: "copy"})

        codec_item = table.item(0, _TrackTable.COL_CODEC)
        assert codec_item is not None
        assert codec_item.text() == "HEVC"
        assert codec_item.foreground().color().name().lower() != table._VIDEO_ENCODE_COLOR.name().lower()
        assert codec_item.font().bold() is False

    def test_remux_panel_update_video_track_encoding_empty_list_clears_state(self, qt_app, tmp_path):
        panel = RemuxPanel(AppConfig())
        src = tmp_path / "source.mkv"
        src.touch()

        video = _track(0, "video", file_id="fid", codec="HEVC")
        video.encode_plan_codec = "libx265"
        video.encode_plan_modified = True

        info = _file_info(path=src, videos=[_video(index=0)])
        source = SourceFile(id="fid", path=src, color=_COLOR_A, info=info, tracks=[video])
        panel._source_files = [source]
        panel._source_colors = {"fid": _COLOR_A}
        panel._source_names = {"fid": "source.mkv"}
        panel._track_table.append_tracks(_COLOR_A, [video])

        with patch.object(panel, "_rebuild_preview"), patch.object(panel, "_emit_video_tracks"):
            panel.update_video_track_encoding([])

        assert video.encode_plan_codec == ""
        assert video.encode_plan_summary == ""
        assert video.encode_plan_hdr_badges == ()
        assert video.encode_plan_modified is False
        codec_item = panel._track_table.item(0, _TrackTable.COL_CODEC)
        assert codec_item is not None
        assert codec_item.text() == "HEVC"
        panel.close()

    def test_audio_sync_done_logs_preformatted_i18n_values(self, qt_app, tmp_path):
        prev_lang = current_language()
        panel = None
        try:
            set_current_language("fra")
            panel = RemuxPanel(AppConfig())
            track = _track(1, "audio", file_id="fid")
            panel._track_table.append_tracks(_COLOR_A, [track])
            emitted: list[tuple[str, str]] = []
            panel.log_message.connect(lambda level, message: emitted.append((level, message)))

            panel._on_audio_sync_done(track.entry_id, "", -320, 0.876)

            assert emitted == [
                (
                    "OK",
                    "Synchronisation audio améliorée appliquée : -320 ms (confiance 0.88).",
                )
            ]
        finally:
            if panel is not None:
                panel.close()
            set_current_language(prev_lang)

    def test_audio_sync_done_clears_reference_offset(self, qt_app, tmp_path):
        panel = RemuxPanel(AppConfig())
        target = _track(1, "audio", file_id="target")
        reference = _track(1, "audio", file_id="reference")
        reference.time_shift_ms = 250
        panel._track_table.append_tracks(_COLOR_A, [target])
        panel._track_table.append_tracks(_COLOR_B, [reference])

        panel._on_audio_sync_done(target.entry_id, reference.entry_id, -320, 0.876)

        assert target.time_shift_ms == -320
        assert reference.time_shift_ms == 0
        panel.close()

    def test_audio_sync_done_applies_delta_to_source_tracks_and_chapters(self, qt_app, tmp_path):
        panel = RemuxPanel(AppConfig())
        target_src = tmp_path / "target.mkv"
        reference_src = tmp_path / "reference.mkv"
        target_src.touch()
        reference_src.touch()

        target_video = _track(0, "video", file_id="target")
        target_audio = _track(1, "audio", file_id="target")
        target_sub = _track(2, "subtitle", codec="SRT", file_id="target")
        reference_audio = _track(1, "audio", file_id="reference")
        target_info = _file_info(
            path=target_src,
            videos=[_video(index=0)],
            audios=[_audio(index=1)],
            subs=[_subtitle(index=2)],
        )
        target_info.chapters = ChapterInfo([
            ChapterEntry(0.2, "Intro"),
            ChapterEntry(10.0, "Main"),
        ])
        reference_info = _file_info(path=reference_src, audios=[_audio(index=1)])
        panel._source_files = [
            SourceFile(id="target", path=target_src, color=_COLOR_A, info=target_info, tracks=[target_video, target_audio, target_sub]),
            SourceFile(id="reference", path=reference_src, color=_COLOR_B, info=reference_info, tracks=[reference_audio]),
        ]
        panel._source_colors = {"target": _COLOR_A, "reference": _COLOR_B}
        panel._track_table.append_tracks(_COLOR_A, [target_video, target_audio, target_sub])
        panel._track_table.append_tracks(_COLOR_B, [reference_audio])
        panel._update_chapters_from_sources()

        panel._on_audio_sync_done(target_audio.entry_id, reference_audio.entry_id, -320, 0.876)

        assert target_video.time_shift_ms == 0
        assert target_audio.time_shift_ms == -320
        assert target_sub.time_shift_ms == -320
        assert reference_audio.time_shift_ms == 0
        chapters = panel.current_chapter_overrides()
        assert chapters is not None
        assert [round(entry.timecode_s, 2) for entry in chapters] == [0.0, 9.68]
        panel.close()

    def test_audio_sync_cancel_resets_only_requested_track(self, qt_app, tmp_path):
        panel = RemuxPanel(AppConfig())
        target_src = tmp_path / "target.mkv"
        target_src.touch()

        target_audio = _track(1, "audio", file_id="target")
        second_audio = _track(2, "audio", file_id="target")
        target_sub = _track(3, "subtitle", codec="SRT", file_id="target")
        target_info = _file_info(
            path=target_src,
            audios=[_audio(index=1), _audio(index=2)],
            subs=[_subtitle(index=3)],
        )
        target_info.chapters = ChapterInfo([
            ChapterEntry(0.2, "Intro"),
            ChapterEntry(10.0, "Main"),
        ])
        panel._source_files = [
            SourceFile(
                id="target",
                path=target_src,
                color=_COLOR_A,
                info=target_info,
                tracks=[target_audio, second_audio, target_sub],
            ),
        ]
        panel._source_colors = {"target": _COLOR_A}
        panel._track_table.append_tracks(_COLOR_A, [target_audio, second_audio, target_sub])
        panel._update_chapters_from_sources()

        panel._on_audio_sync_done(target_audio.entry_id, "", -320, 0.876)
        assert {
            target_audio.entry_id,
            second_audio.entry_id,
            target_sub.entry_id,
        }.issubset(panel._auto_sync_entry_ids)
        assert [round(entry.timecode_s, 2) for entry in panel._chapter_panel.get_chapters()] == [0.0, 9.68]
        assert not panel._chapter_panel._sync_cancel_btn.isHidden()
        assert not panel._global_sync_cancel_btn.isHidden()
        tooltip = panel._global_sync_cancel_btn.toolTip()
        assert "V</span>=0" in tooltip
        assert "A</span>=2" in tooltip
        assert "S</span>=1" in tooltip
        assert "C</span>=Yes" in tooltip

        panel._on_auto_sync_cancel_requested(target_audio)

        assert target_audio.time_shift_ms == 0
        assert second_audio.time_shift_ms == -320
        assert target_sub.time_shift_ms == -320
        assert target_audio.entry_id not in panel._auto_sync_entry_ids
        assert second_audio.entry_id in panel._auto_sync_entry_ids
        assert target_sub.entry_id in panel._auto_sync_entry_ids
        panel._on_auto_sync_cancel_requested(target_sub)
        assert second_audio.time_shift_ms == -320
        assert target_sub.time_shift_ms == 0
        assert [round(entry.timecode_s, 2) for entry in panel._chapter_panel.get_chapters()] == [0.0, 9.68]
        panel._on_auto_sync_cancel_requested(second_audio)
        assert second_audio.time_shift_ms == 0
        assert [round(entry.timecode_s, 2) for entry in panel._chapter_panel.get_chapters()] == [0.2, 10.0]
        assert panel._chapter_panel._sync_cancel_btn.isHidden()
        assert panel._global_sync_cancel_btn.isHidden()
        panel.close()

    def test_chapter_sync_cancel_only_resets_chapters(self, qt_app, tmp_path):
        panel = RemuxPanel(AppConfig())
        target_src = tmp_path / "target.mkv"
        target_src.touch()

        target_audio = _track(1, "audio", file_id="target")
        target_sub = _track(2, "subtitle", codec="SRT", file_id="target")
        target_info = _file_info(
            path=target_src,
            audios=[_audio(index=1)],
            subs=[_subtitle(index=2)],
        )
        target_info.chapters = ChapterInfo([
            ChapterEntry(0.2, "Intro"),
            ChapterEntry(10.0, "Main"),
        ])
        panel._source_files = [
            SourceFile(
                id="target",
                path=target_src,
                color=_COLOR_A,
                info=target_info,
                tracks=[target_audio, target_sub],
            ),
        ]
        panel._source_colors = {"target": _COLOR_A}
        panel._track_table.append_tracks(_COLOR_A, [target_audio, target_sub])
        panel._update_chapters_from_sources()

        panel._on_audio_sync_done(target_audio.entry_id, "", -320, 0.876)
        panel._chapter_panel._sync_cancel_btn.click()

        assert target_audio.time_shift_ms == -320
        assert target_sub.time_shift_ms == -320
        assert [round(entry.timecode_s, 2) for entry in panel._chapter_panel.get_chapters()] == [0.2, 10.0]
        assert panel.current_chapter_overrides() is None
        assert panel._chapter_panel._sync_cancel_btn.isHidden()
        assert not panel._global_sync_cancel_btn.isHidden()
        assert "C</span>=No" in panel._global_sync_cancel_btn.toolTip()
        panel.close()

    def test_global_sync_cancel_resets_tracks_and_chapters(self, qt_app, tmp_path):
        panel = RemuxPanel(AppConfig())
        target_src = tmp_path / "target.mkv"
        target_src.touch()

        target_audio = _track(1, "audio", file_id="target")
        target_sub = _track(2, "subtitle", codec="SRT", file_id="target")
        target_info = _file_info(
            path=target_src,
            audios=[_audio(index=1)],
            subs=[_subtitle(index=2)],
        )
        target_info.chapters = ChapterInfo([
            ChapterEntry(0.2, "Intro"),
            ChapterEntry(10.0, "Main"),
        ])
        panel._source_files = [
            SourceFile(
                id="target",
                path=target_src,
                color=_COLOR_A,
                info=target_info,
                tracks=[target_audio, target_sub],
            ),
        ]
        panel._source_colors = {"target": _COLOR_A}
        panel._track_table.append_tracks(_COLOR_A, [target_audio, target_sub])
        panel._update_chapters_from_sources()

        panel._on_audio_sync_done(target_audio.entry_id, "", -320, 0.876)
        panel._global_sync_cancel_btn.click()

        assert target_audio.time_shift_ms == 0
        assert target_sub.time_shift_ms == 0
        assert panel._auto_sync_entry_ids == set()
        assert panel._source_sync_offsets_ms == {}
        assert [round(entry.timecode_s, 2) for entry in panel._chapter_panel.get_chapters()] == [0.2, 10.0]
        assert panel._chapter_panel._sync_cancel_btn.isHidden()
        assert panel._global_sync_cancel_btn.isHidden()
        panel.close()

    def test_collect_config_exports_synced_chapters_after_source_switch(self, qt_app, tmp_path):
        panel = RemuxPanel(AppConfig())
        reference_src = tmp_path / "reference.mkv"
        target_src = tmp_path / "target.mkv"
        reference_src.touch()
        target_src.touch()

        reference_audio = _track(1, "audio", file_id="reference")
        target_audio = _track(1, "audio", file_id="target")
        reference_info = _file_info(path=reference_src, audios=[_audio(index=1)])
        reference_info.chapters = ChapterInfo([
            ChapterEntry(0.0, "Ref"),
            ChapterEntry(12.0, "Ref main"),
        ])
        target_info = _file_info(path=target_src, audios=[_audio(index=1)])
        target_info.chapters = ChapterInfo([
            ChapterEntry(0.2, "Intro"),
            ChapterEntry(10.0, "Main"),
        ])
        panel._source_files = [
            SourceFile(id="reference", path=reference_src, color=_COLOR_A, info=reference_info, tracks=[reference_audio]),
            SourceFile(id="target", path=target_src, color=_COLOR_B, info=target_info, tracks=[target_audio]),
        ]
        panel._source_colors = {"reference": _COLOR_A, "target": _COLOR_B}
        panel._track_table.append_tracks(_COLOR_A, [reference_audio])
        panel._track_table.append_tracks(_COLOR_B, [target_audio])
        panel._output_edit.setText(str(tmp_path / "out.mkv"))
        panel._update_chapters_from_sources()

        panel._on_audio_sync_done(target_audio.entry_id, reference_audio.entry_id, -320, 0.876)
        panel._chapter_panel._on_source_changed(1)

        config = panel.collect_config()

        assert config is not None
        assert config.chapter_overrides is not None
        assert [round(entry.timecode_s, 2) for entry in config.chapter_overrides] == [0.0, 9.68]
        panel.close()

    def test_sync_rewrite_toggle_switches_track_between_real_and_offset(self, qt_app):
        config = AppConfig()
        config.sync_rewrite_enabled = True
        panel = RemuxPanel(config)
        track = _track(1, "audio", file_id="target", codec="EAC3")
        track.time_shift_ms = 125
        panel._track_table.append_tracks(_COLOR_A, [track])

        panel._on_sync_rewrite_toggle_requested(track)

        info_item = panel._track_table.item(0, _TrackTable.COL_INFO)
        assert track.sync_rewrite_mode == "offset"
        assert info_item is not None
        assert "Sync offset" in info_item.text()

        panel._on_sync_rewrite_toggle_requested(track)

        assert track.sync_rewrite_mode == ""
        assert "Sync réelle" in info_item.text()
        panel.close()

    def test_audio_sync_done_resets_reference_source_tracks_and_chapters(self, qt_app, tmp_path):
        panel = RemuxPanel(AppConfig())
        reference_src = tmp_path / "reference.mkv"
        target_src = tmp_path / "target.mkv"
        reference_src.touch()
        target_src.touch()

        reference_audio = _track(1, "audio", file_id="reference")
        reference_sub = _track(2, "subtitle", codec="SRT", file_id="reference")
        target_audio = _track(1, "audio", file_id="target")
        target_sub = _track(2, "subtitle", codec="SRT", file_id="target")
        reference_info = _file_info(
            path=reference_src,
            audios=[_audio(index=1)],
            subs=[_subtitle(index=2)],
        )
        reference_info.chapters = ChapterInfo([
            ChapterEntry(0.0, "Intro"),
            ChapterEntry(10.0, "Main"),
        ])
        target_info = _file_info(path=target_src, audios=[_audio(index=1)], subs=[_subtitle(index=2)])
        panel._source_files = [
            SourceFile(id="reference", path=reference_src, color=_COLOR_A, info=reference_info, tracks=[reference_audio, reference_sub]),
            SourceFile(id="target", path=target_src, color=_COLOR_B, info=target_info, tracks=[target_audio, target_sub]),
        ]
        panel._source_colors = {"reference": _COLOR_A, "target": _COLOR_B}
        panel._track_table.append_tracks(_COLOR_A, [reference_audio, reference_sub])
        panel._track_table.append_tracks(_COLOR_B, [target_audio, target_sub])
        panel._update_chapters_from_sources()

        panel._on_audio_sync_done(reference_audio.entry_id, "", 250, 0.9)
        assert reference_audio.time_shift_ms == 250
        assert reference_sub.time_shift_ms == 250
        assert [round(entry.timecode_s, 2) for entry in panel._chapter_panel.get_chapters()] == [0.25, 10.25]

        panel._on_audio_sync_done(target_audio.entry_id, reference_audio.entry_id, -320, 0.876)

        assert reference_audio.time_shift_ms == 0
        assert reference_sub.time_shift_ms == 0
        assert target_audio.time_shift_ms == -320
        assert target_sub.time_shift_ms == -320
        assert [round(entry.timecode_s, 2) for entry in panel._chapter_panel.get_chapters()] == [0.0, 10.0]
        panel.close()


# ===========================================================================
# RemuxPanel — pistes NEW synchronisées avec EncodePanel
# ===========================================================================

class TestRemuxPanelNewAudioTracks:

    @staticmethod
    def _panel_with_audio_tracks(
        qt_app,
        tmp_path,
        tracks: list[TrackEntry],
    ) -> RemuxPanel:
        cfg = AppConfig()
        panel = RemuxPanel(cfg)
        src = tmp_path / "source.mkv"
        src.touch()
        info = _file_info(path=src, audios=[_audio(index=1, title="Source")])
        sf = SourceFile(id="fid", path=src, color=_COLOR_A, info=info, tracks=tracks)
        panel._source_files = [sf]
        panel._source_colors = {"fid": _COLOR_A}
        panel._source_names = {"fid": "source.mkv"}
        panel._track_table.append_tracks(_COLOR_A, tracks)
        panel._output_edit.setText(str(tmp_path / "out.mkv"))
        return panel

    @staticmethod
    def _row_for_entry(panel: RemuxPanel, entry: TrackEntry) -> int:
        for row in range(panel._track_table.rowCount()):
            item = panel._track_table.item(row, _TrackTable.COL_CHECK)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) is entry:
                return row
        raise AssertionError(f"entry not found: {entry.entry_id}")

    def test_new_track_remains_available_when_source_track_is_unselected(self, qt_app, tmp_path):
        source = _track(1, "audio", file_id="fid", language="fra", title="Source")
        new_track = clone_track_entry(source)
        new_track.title = "Clone"
        panel = self._panel_with_audio_tracks(qt_app, tmp_path, [source, new_track])
        emitted: list = []
        panel.audio_tracks_changed.connect(lambda tracks: emitted.append(tracks))

        source_row = self._row_for_entry(panel, source)
        source_item = panel._track_table.item(source_row, _TrackTable.COL_CHECK)
        assert source_item is not None
        source_item.setCheckState(Qt.CheckState.Unchecked)
        panel._emit_audio_tracks()

        assert emitted
        entries = [item[3] for item in emitted[-1]]
        assert entries == [new_track]
        panel.close()

    def test_removed_new_track_leaves_remux_and_workflow_config(self, qt_app, tmp_path):
        source = _track(1, "audio", file_id="fid", language="fra", title="Source")
        new_track = clone_track_entry(source)
        panel = self._panel_with_audio_tracks(qt_app, tmp_path, [source, new_track])

        panel.remove_audio_track_variant(new_track.entry_id)
        config = panel.collect_config()

        assert all(track.entry_id != new_track.entry_id for track in panel._source_files[0].tracks)
        assert all(
            item[2] != new_track.entry_id
            for item in (config.track_order if config is not None else [])
            if len(item) > 2
        )
        assert panel._track_table.has_entry_id(new_track.entry_id) is False
        panel.close()

    def test_track_order_change_reemits_audio_tracks_with_entry_ids(self, qt_app, tmp_path):
        source = _track(1, "audio", file_id="fid", language="fra", title="Source")
        new_track = clone_track_entry(source)
        panel = self._panel_with_audio_tracks(qt_app, tmp_path, [source, new_track])
        emitted: list = []
        panel.audio_tracks_changed.connect(lambda tracks: emitted.append(tracks))

        panel._track_table.order_changed.emit()

        assert emitted
        assert [item[3].entry_id for item in emitted[-1]] == [
            source.entry_id,
            new_track.entry_id,
        ]
        panel.close()


class TestRemuxPanelTrackOrderProjections:

    def test_cross_type_reorder_keeps_encode_projections_untouched(self, qt_app, tmp_path, monkeypatch):
        cfg = AppConfig()
        panel = RemuxPanel(cfg)
        source_path = tmp_path / "source.mkv"
        source_path.touch()
        video = _track(0, "video", file_id="fid", codec="HEVC")
        audio = _track(1, "audio", file_id="fid")
        info = _file_info(
            path=source_path,
            videos=[_video(index=0)],
            audios=[_audio(index=1)],
        )
        panel._source_files = [
            SourceFile(id="fid", path=source_path, color=_COLOR_A, info=info, tracks=[video, audio])
        ]
        panel._source_colors = {"fid": _COLOR_A}
        panel._source_names = {"fid": source_path.name}
        panel._track_table.append_tracks(_COLOR_A, [video, audio])
        video_emissions: list = []
        audio_emissions: list = []
        panel.video_tracks_changed.connect(video_emissions.append)
        panel.audio_tracks_changed.connect(audio_emissions.append)
        panel._track_table.selectRow(1)

        class _DropEvent:
            @staticmethod
            def source():
                return panel._track_table

            @staticmethod
            def position():
                return QPointF(0, 0)

            @staticmethod
            def setDropAction(_action):
                pass

            @staticmethod
            def accept():
                pass

            @staticmethod
            def ignore():
                raise AssertionError("Le drop interne ne doit pas être ignoré")

        monkeypatch.setattr(panel._track_table, "_drop_target_row", lambda _event: 0)
        panel._track_table.dropEvent(_DropEvent())

        assert [entry.entry_id for entry in panel._track_table.current_tracks()] == [
            audio.entry_id,
            video.entry_id,
        ]
        assert video_emissions == []
        assert audio_emissions == []
        panel.close()


class TestRemuxPanelDecisionProfiles:

    @staticmethod
    def _panel_with_tracks(qt_app, tmp_path, tracks: list[TrackEntry], *, file_id: str = "fid") -> RemuxPanel:
        cfg = AppConfig()
        cfg.profiles_dir = tmp_path / "profiles"
        panel = RemuxPanel(cfg)
        src = tmp_path / f"{file_id}.mkv"
        src.touch()
        info = _file_info(path=src, videos=[_video(index=0)], audios=[_audio(index=1, title="Source")])
        sf = SourceFile(id=file_id, path=src, color=_COLOR_A, info=info, tracks=tracks)
        panel._source_files = [sf]
        panel._source_colors = {file_id: _COLOR_A}
        panel._source_names = {file_id: src.name}
        panel._track_table.append_tracks(_COLOR_A, tracks)
        return panel

    def test_save_profile_writes_decision_profile_without_paths(self, qt_app, tmp_path, monkeypatch):
        video = _track(0, "video", file_id="fid", codec="HEVC", language="und", orig_language="und")
        audio = _track(1, "audio", file_id="fid", language="fra", title="VF")
        panel = self._panel_with_tracks(qt_app, tmp_path, [video, audio])

        class FakeDialog:
            def __init__(self, *, manager=None, current_config=None, current_tracks=None, source_index_by_file_id=None, parent=None, profile=None):
                assert current_config is not None
                self._manager = manager
                self._profile = remux_config_to_decision_profile(current_config, name="Auto VF")

            def exec(self):
                assert self._manager is not None
                self._manager.save(self._profile)
                return QDialog.DialogCode.Accepted

            def profile(self):
                return self._profile

        monkeypatch.setattr("ui.panels.remux_panel.panel.DecisionProfileEditorDialog", FakeDialog)

        panel._save_decision_profile()
        payload = json.loads((tmp_path / "profiles" / "decision" / "Auto_VF.json").read_text(encoding="utf-8"))

        assert payload["kind"] == "decision-profile"
        assert payload["version"] == 1
        assert "sources" not in payload
        assert "output" not in payload
        assert str(tmp_path) not in json.dumps(payload)
        panel.close()

    def test_decision_profile_applies_to_another_gui_source(self, qt_app, tmp_path):
        source_audio = _track(1, "audio", file_id="fid", language="fra", title="VF")
        source_audio.title = "VF EAC3 5.1"
        source_audio.flag_default = True
        panel_a = self._panel_with_tracks(qt_app, tmp_path, [
            _track(0, "video", file_id="fid", codec="HEVC", language="und", orig_language="und"),
            source_audio,
        ])
        profile_config = panel_a._current_profile_config()
        assert profile_config is not None
        profile = remux_config_to_decision_profile(profile_config, name="Auto VF")

        target_audio = _track(11, "audio", file_id="other", language="fr-FR", orig_language="fr-FR", title="French")
        target_extra = _track(12, "audio", file_id="other", language="eng", orig_language="eng", title="English")
        panel_b = self._panel_with_tracks(qt_app, tmp_path, [
            target_extra,
            _track(7, "video", file_id="other", codec="HEVC", language="und", orig_language="und"),
            target_audio,
        ], file_id="other")

        panel_b._apply_decision_profile(profile)

        applied_tracks = panel_b._track_table.current_tracks()
        applied_audio = next(track for track in applied_tracks if track.mkv_tid == 11)
        applied_extra = next(track for track in applied_tracks if track.mkv_tid == 12)
        assert applied_audio.title == "VF EAC3 5.1"
        assert applied_audio.flag_default is True
        assert applied_extra.enabled is False
        assert all("profile-preview" not in str(source.path) for source in panel_b._source_files)
        panel_a.close()
        panel_b.close()

    def test_decision_profile_order_priority_updates_config_track_order_by_entry_id(self, qt_app, tmp_path):
        video = _track(0, "video", file_id="fid", codec="HEVC", language="und", orig_language="und")
        audio_fr = _track(1, "audio", file_id="fid", language="fr-FR", orig_language="fr-FR", title="French")
        audio_en = _track(2, "audio", file_id="fid", language="en-US", orig_language="en-US", title="English")
        panel = self._panel_with_tracks(qt_app, tmp_path, [video, audio_fr, audio_en])
        panel._output_edit.setText(str(tmp_path / "out.mkv"))
        profile = {
            "version": 1,
            "kind": "decision-profile",
            "name": "Order",
            "rules": [
                {
                    "id": "audio",
                    "scope": "all",
                    "match": {"all": [{"field": "type", "op": "is", "value": "audio"}]},
                    "actions": [{"type": "set_order_priority", "value": 1}],
                },
                {
                    "id": "video",
                    "scope": "all",
                    "match": {"all": [{"field": "type", "op": "is", "value": "video"}]},
                    "actions": [{"type": "set_order_priority", "value": 2}],
                },
            ],
        }

        panel._apply_decision_profile(profile)
        config = panel.collect_config()

        assert config is not None
        assert [item[2] for item in config.track_order if len(item) == 3] == [audio_fr.entry_id, audio_en.entry_id, video.entry_id]
        panel.close()

    def test_profile_editor_video_criteria_build_resolution_and_hdr_flags(self, qt_app, tmp_path):
        from core.profiles.decision import DecisionProfileManager
        from ui.panels.remux_panel.profile_editor import DecisionProfileEditorDialog

        dialog = DecisionProfileEditorDialog(manager=DecisionProfileManager(tmp_path / "profiles"))
        dialog._add_template_rule("video")
        dialog._match_width.setValue(3840)
        dialog._match_height.setValue(2160)
        dialog._video_hdr.setChecked(True)
        dialog._video_hdr10plus.setChecked(True)
        dialog._video_dolby_vision.setChecked(True)
        dialog._match_codec.setText("{codec_atmos}")
        dialog._match_codec_required.setChecked(True)
        dialog._match_keywords.setText("{flag_visual_impaired}")

        profile = dialog.profile()
        conditions = profile["rules"][0]["match"]["all"]
        by_field = {item["field"]: item for item in conditions}

        assert by_field["width"]["value"] == 3840
        assert by_field["height"]["value"] == 2160
        video_flags = int(by_field["video_flags_hex"]["value"], 16)
        assert video_flags & 0x00000008
        assert video_flags & 0x00000010
        assert video_flags & 0x00000040
        assert video_flags & 0x00000080
        assert by_field["flag_visual_impaired"]["required"] is True
        assert by_field["codec_atmos"]["required"] is True
        dialog.close()

    def test_profile_editor_can_make_codec_required_and_atmos_preferred(self, qt_app, tmp_path):
        from core.profiles.decision import DecisionProfileManager
        from ui.panels.remux_panel.profile_editor import DecisionProfileEditorDialog

        dialog = DecisionProfileEditorDialog(manager=DecisionProfileManager(tmp_path / "profiles"))
        dialog._add_template_rule("language")
        dialog._match_language.setText("fr-FR")
        dialog._match_codec.setText("EAC3")
        dialog._match_codec_required.setChecked(True)
        dialog._match_preferred_keywords.setText("{atmos}")

        profile = dialog.profile()
        conditions = profile["rules"][0]["match"]["all"]
        by_field = {item["field"]: item for item in conditions}

        assert by_field["language"]["required"] is True
        assert by_field["codec"]["required"] is True
        assert by_field["codec_atmos"]["required"] is False
        dialog.close()

    def test_profile_editor_no_commentary_template_prefills_visible_criteria(self, qt_app, tmp_path):
        from core.profiles.decision import DecisionProfileManager
        from ui.panels.remux_panel.profile_editor import DecisionProfileEditorDialog

        dialog = DecisionProfileEditorDialog(manager=DecisionProfileManager(tmp_path / "profiles"))
        dialog._add_template_rule("no_commentary")

        profile = dialog.profile()

        assert dialog._match_title.text() == "comment | commentary"
        assert [item["value"] for item in profile["rules"][0]["match"]["all"][0]["any"]] == ["comment", "commentary"]
        assert profile["rules"][0]["actions"] == [{"type": "set_enabled", "value": False}]
        dialog.close()

    def test_profile_editor_title_contains_filters_all_matching_subtitles(self, qt_app, tmp_path):
        from core.profiles.decision import DecisionProfileManager, apply_decision_profile
        from ui.panels.remux_panel.profile_editor import DecisionProfileEditorDialog

        dialog = DecisionProfileEditorDialog(manager=DecisionProfileManager(tmp_path / "profiles"))
        dialog._add_template_rule("language")
        dialog._set_combo_data(dialog._match_type, "subtitle")
        dialog._match_title.setText("VFQ")
        dialog._action_language.setText("fr-CA")

        profile = dialog.profile()
        rule = profile["rules"][0]
        conditions = rule["match"]["all"]
        title_condition = next(item for item in conditions if item["field"] == "source_title")
        tracks = [
            _track(5, "subtitle", codec="SUBRIP", title="VFF", orig_title="VFF"),
            _track(6, "subtitle", codec="SUBRIP", title="VFQ Forced", orig_title="VFQ Forced"),
            _track(7, "subtitle", codec="SUBRIP", title="VFQ", orig_title="VFQ"),
        ]

        result = apply_decision_profile(profile, tracks)

        assert rule["scope"] == "all"
        assert title_condition["required"] is True
        assert result.report["applied_rules"] == 2
        assert [track.language for track in tracks] == ["fra", "fr-CA", "fr-CA"]
        dialog.close()

    def test_profile_editor_criteria_expressions_build_nested_match(self, qt_app, tmp_path):
        from core.profiles.decision import DecisionProfileManager
        from ui.panels.remux_panel.profile_editor import DecisionProfileEditorDialog

        dialog = DecisionProfileEditorDialog(manager=DecisionProfileManager(tmp_path / "profiles"))
        dialog._add_template_rule("language")
        dialog._match_title.setText("(VFQ | VFF) & Forced")
        dialog._match_codec.setText("EAC3 | AC3")
        dialog._match_codec_required.setChecked(True)
        dialog._match_keywords.setText("{atmos} | {dtsx}")

        profile = dialog.profile()
        conditions = profile["rules"][0]["match"]["all"]
        title_condition = next(item for item in conditions if "all" in item and any("source_title" in str(child) for child in item["all"]))
        codec_condition = next(item for item in conditions if "any" in item and any(child.get("field") == "codec" for child in item["any"]))
        keyword_condition = next(item for item in conditions if "any" in item and any(child.get("field") == "codec_atmos" for child in item["any"]))

        assert title_condition["all"][0]["any"][0]["value"] == "VFQ"
        assert title_condition["all"][0]["any"][1]["value"] == "VFF"
        assert title_condition["all"][1]["value"] == "Forced"
        assert [item["value"] for item in codec_condition["any"]] == ["EAC3", "AC3"]
        assert all(item["required"] is True for item in codec_condition["any"])
        assert {item["field"] for item in keyword_condition["any"]} == {"codec_atmos", "codec_dtsx"}
        dialog.close()

    def test_profile_editor_title_contains_expression_can_or_values(self, qt_app, tmp_path):
        from core.profiles.decision import DecisionProfileManager
        from ui.panels.remux_panel.profile_editor import DecisionProfileEditorDialog

        dialog = DecisionProfileEditorDialog(manager=DecisionProfileManager(tmp_path / "profiles"))
        dialog._add_template_rule("language")
        dialog._match_title.setText("VFQ | VFF")

        profile = dialog.profile()
        title_condition = next(item for item in profile["rules"][0]["match"]["all"] if "any" in item)

        assert [item["value"] for item in title_condition["any"]] == ["VFQ", "VFF"]
        assert all(item["field"] == "source_title" for item in title_condition["any"])
        dialog.close()

    def test_profile_editor_title_expression_supports_literal_operator_escapes(self, qt_app, tmp_path):
        from core.profiles.decision import DecisionProfileManager
        from ui.panels.remux_panel.profile_editor import DecisionProfileEditorDialog

        dialog = DecisionProfileEditorDialog(manager=DecisionProfileManager(tmp_path / "profiles"))
        dialog._add_template_rule("language")
        dialog._match_title.setText(r'"A | B" | Dolby \& DTS')

        profile = dialog.profile()
        title_condition = next(item for item in profile["rules"][0]["match"]["all"] if "any" in item)

        assert [item["value"] for item in title_condition["any"]] == ["A | B", "Dolby & DTS"]
        dialog.close()

    def test_profile_editor_invalid_expression_blocks_profile_and_preview(self, qt_app, tmp_path):
        from core.profiles.decision import DecisionProfileManager
        from ui.panels.remux_panel.profile_editor import DecisionProfileEditorDialog

        prev_lang = current_language()
        set_current_language("fra")
        dialog = None
        try:
            dialog = DecisionProfileEditorDialog(manager=DecisionProfileManager(tmp_path / "profiles"))
            dialog._add_template_rule("language")
            dialog._match_title.setText("VFQ |")

            with pytest.raises(ValueError):
                dialog.profile()
            dialog._refresh_preview()

            assert "Expression critères invalide" in dialog._preview.toPlainText()
        finally:
            if dialog is not None:
                dialog.close()
            set_current_language(prev_lang)

    def test_profile_editor_can_store_generic_alias_variables(self, qt_app, tmp_path):
        from core.profiles.decision import DecisionProfileManager
        from ui.panels.remux_panel.profile_editor import DecisionProfileEditorDialog

        dialog = DecisionProfileEditorDialog(manager=DecisionProfileManager(tmp_path / "profiles"))
        dialog._aliases = {
            "*": {"EAC3": "DDP"},
            "lang_name": {"French": "Français"},
        }
        dialog._refresh_alias_status()

        profile = dialog.profile()

        assert profile["variables"]["aliases"] == {
            "*": {"EAC3": "DDP"},
            "lang_name": {"French": "Français"},
        }
        assert "codec_names" not in profile["variables"]
        status = dialog._aliases_status.text()
        assert "2" in status
        assert "alias" in status
        dialog.close()

    def test_profile_editor_alias_parser_supports_global_scoped_and_legacy_codec_names(self, qt_app, tmp_path):
        from core.profiles.decision import DecisionProfileManager
        from ui.panels.remux_panel.profile_editor import DecisionProfileEditorDialog

        parsed = DecisionProfileEditorDialog._parse_aliases("EAC3=DDP\nlang_name:French=Français\nDTS:X=DTSX")
        formatted = DecisionProfileEditorDialog._format_aliases(parsed)
        dialog = DecisionProfileEditorDialog(
            manager=DecisionProfileManager(tmp_path / "profiles"),
            profile={
                "version": 1,
                "kind": "decision-profile",
                "name": "Legacy",
                "variables": {"codec_names": {"AC3": "Dolby Digital"}},
                "rules": [],
            },
        )

        assert parsed["*"]["EAC3"] == "DDP"
        assert parsed["*"]["DTS:X"] == "DTSX"
        assert parsed["lang_name"]["French"] == "Français"
        assert "lang_name:French=Français" in formatted
        assert dialog._aliases["codec"]["AC3"] == "Dolby Digital"
        assert dialog._aliases["codec_name"]["AC3"] == "Dolby Digital"
        dialog.close()

    def test_profile_editor_can_load_and_delete_existing_profile(self, qt_app, tmp_path, monkeypatch):
        from PySide6.QtWidgets import QMessageBox
        from core.profiles.decision import DecisionProfileManager
        from ui.panels.remux_panel.profile_editor import DecisionProfileEditorDialog

        manager = DecisionProfileManager(tmp_path / "profiles")
        manager.save(
            {
                "version": 1,
                "kind": "decision-profile",
                "name": "Existing",
                "rules": [
                    {
                        "id": "r1",
                        "match": {"all": [{"field": "type", "op": "is", "value": "audio"}]},
                        "actions": [{"type": "set_title", "value": "Loaded"}],
                    }
                ],
            }
        )
        dialog = DecisionProfileEditorDialog(manager=manager)

        index = dialog._profile_selector.findData("Existing")
        assert index >= 0
        dialog._profile_selector.setCurrentIndex(index)
        dialog._load_selected_profile()

        assert dialog._name_edit.text() == "Existing"
        assert dialog._rules()[0]["id"] == "r1"

        monkeypatch.setattr(
            "ui.panels.remux_panel.profile_editor.QMessageBox.question",
            lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
        )
        dialog._delete_selected_profile()

        assert manager.load("Existing") is None
        assert dialog._profile_selector.findData("Existing") == -1
        assert dialog._name_edit.text() == "Nouveau profil"
        dialog.close()

    def test_profile_editor_video_criteria_can_use_width_without_height(self, qt_app, tmp_path):
        from core.profiles.decision import DecisionProfileManager
        from ui.panels.remux_panel.profile_editor import DecisionProfileEditorDialog

        dialog = DecisionProfileEditorDialog(manager=DecisionProfileManager(tmp_path / "profiles"))
        dialog._add_template_rule("video")
        dialog._match_width.setValue(3840)
        dialog._match_height.setValue(0)

        profile = dialog.profile()
        conditions = profile["rules"][0]["match"]["all"]
        fields = [item["field"] for item in conditions]

        assert "width" in fields
        assert "height" not in fields
        assert "resolution" not in fields
        dialog.close()

    def test_profile_editor_serializes_not_none_flags_tags_and_order(self, qt_app, tmp_path):
        from core.profiles.decision import DecisionProfileManager
        from ui.panels.remux_panel.profile_editor import DecisionProfileEditorDialog

        dialog = DecisionProfileEditorDialog(manager=DecisionProfileManager(tmp_path / "profiles"))
        dialog._add_template_rule("language")
        dialog._match_title.setText("!(Commentary | Descriptive)")
        dialog._match_flags.setText("{none}")
        dialog._action_flags.setText("default, !forced")
        dialog._action_tag.setText("vf_main, !old_tag")
        dialog._action_order.setValue(800)

        profile = dialog.profile()
        rule = profile["rules"][0]
        title_condition = next(item for item in rule["match"]["all"] if "not" in item)
        flags_condition = next(item for item in rule["match"]["all"] if item.get("field") == "flags")
        actions = rule["actions"]

        assert title_condition["not"]["any"][0]["value"] == "Commentary"
        assert flags_condition["op"] == "missing"
        assert {"type": "set_flags", "value": {"default": True, "forced": False}} in actions
        assert {"type": "remove_track_tags", "value": ["old_tag"]} in actions
        assert {"type": "add_track_tags", "value": ["vf_main"]} in actions
        assert {"type": "set_order_priority", "value": 800} in actions
        dialog.close()

    def test_profile_editor_loads_not_flags_tags_and_order(self, qt_app, tmp_path):
        from core.profiles.decision import DecisionProfileManager
        from ui.panels.remux_panel.profile_editor import DecisionProfileEditorDialog

        dialog = DecisionProfileEditorDialog(
            manager=DecisionProfileManager(tmp_path / "profiles"),
            profile={
                "version": 1,
                "kind": "decision-profile",
                "name": "Load",
                "rules": [
                    {
                        "id": "r1",
                        "match": {
                            "all": [
                                {"not": {"any": [
                                    {"field": "source_title", "op": "contains", "value": "Commentary", "required": True},
                                    {"field": "source_title", "op": "contains", "value": "Descriptive", "required": True},
                                ]}},
                                {"field": "flags", "op": "missing", "required": False},
                            ]
                        },
                        "actions": [
                            {"type": "set_flags", "value": {"default": True, "forced": False}},
                            {"type": "remove_track_tags", "value": ["old_tag"]},
                            {"type": "add_track_tags", "value": ["vf_main"]},
                            {"type": "set_order_priority", "value": 800},
                        ],
                    }
                ],
            },
        )
        dialog._rule_list.setCurrentRow(0)

        assert dialog._match_title.text() == "!(Commentary | Descriptive)"
        assert dialog._match_flags.text() == "{none}"
        assert dialog._action_flags.text() == "default, !forced"
        assert dialog._action_tag.text() == "vf_main, !old_tag"
        assert dialog._action_order.value() == 800
        dialog.close()

    def test_profile_editor_keywords_are_grouped_in_menu(self, qt_app, tmp_path):
        from PySide6.QtWidgets import QMenu
        from core.profiles.decision import DecisionProfileManager
        from ui.panels.remux_panel.profile_editor import DecisionProfileEditorDialog, KeywordLineEdit

        dialog = DecisionProfileEditorDialog(manager=DecisionProfileManager(tmp_path / "profiles"))
        menu = dialog._keyword_button.menu()

        assert menu is not None
        assert len(menu.actions()) == 7
        video_menu = next(
            submenu
            for submenu in (action.menu() for action in menu.actions())
            if isinstance(submenu, QMenu) and "{video_dolby_vision}" in [item.text() for item in submenu.actions()]
        )
        misc_menu = next(
            submenu
            for submenu in (action.menu() for action in menu.actions())
            if isinstance(submenu, QMenu) and "{none}" in [item.text() for item in submenu.actions()]
        )
        assert video_menu is not None
        assert misc_menu is not None
        assert isinstance(dialog._title_pattern, KeywordLineEdit)
        dialog.close()

    def test_keyword_line_edit_exposes_context_keyword_menu_and_badge_segments(self, qt_app):
        from PySide6.QtWidgets import QMenu
        from ui.panels.remux_panel.profile_editor import KeywordLineEdit

        edit = KeywordLineEdit()
        edit.setText("VF {codec} {channels}")
        menu = QMenu()
        edit._populate_keyword_menu(menu)

        assert KeywordLineEdit._segments(edit.text()) == [
            (False, "VF"),
            (True, "codec"),
            (True, "channels"),
        ]
        assert len(menu.actions()) == 7
        audio_menu = next(
            submenu
            for submenu in (action.menu() for action in menu.actions())
            if isinstance(submenu, QMenu) and "{codec_name}" in [item.text() for item in submenu.actions()]
        )
        assert audio_menu is not None
        edit.close()

    def test_keyword_line_edit_exposes_context_operands_menu(self, qt_app):
        from PySide6.QtWidgets import QMenu
        from ui.panels.remux_panel.profile_editor import KeywordLineEdit

        edit = KeywordLineEdit()
        menu = QMenu()
        edit._populate_operands_menu(menu)

        assert [action.text() for action in menu.actions()] == ["AND", "OR", "GROUP ()", "NOT()"]

        edit.setText("VFQ VFF")
        edit.setSelection(0, 3)
        menu.actions()[2].trigger()
        assert edit.text() == "(VFQ) VFF"

        edit.clear()
        edit.setCursorPosition(0)
        menu.actions()[2].trigger()
        assert edit.text() == "()"
        assert edit.cursorPosition() == 1

        edit.clear()
        edit.setText("VFQ VFF")
        edit.setSelection(0, 3)
        menu.actions()[3].trigger()
        assert edit.text() == "NOT(VFQ) VFF"

        edit.clear()
        menu.actions()[0].trigger()
        menu.actions()[1].trigger()
        assert edit.text() == " &  | "
        edit.close()

    def test_keyword_line_edit_keyboard_shortcuts_insert_operands(self, qt_app):
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent
        from ui.panels.remux_panel.profile_editor import KeywordLineEdit

        edit = KeywordLineEdit()

        edit.keyPressEvent(
            QKeyEvent(
                QEvent.Type.KeyPress,
                Qt.Key.Key_Plus,
                Qt.KeyboardModifier.ControlModifier,
                "+",
            )
        )
        assert edit.text() == " & "

        edit.clear()
        edit.keyPressEvent(
            QKeyEvent(
                QEvent.Type.KeyPress,
                Qt.Key.Key_Colon,
                Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
                ":",
            )
        )
        assert edit.text() == " | "

        edit.clear()
        edit.keyPressEvent(
            QKeyEvent(
                QEvent.Type.KeyPress,
                Qt.Key.Key_Slash,
                Qt.KeyboardModifier.ControlModifier,
                "/",
            )
        )
        assert edit.text() == " | "
        edit.close()


class TestRemuxPanelVideoTrackSignals:

    @staticmethod
    def _panel_with_video_tracks(
        qt_app,
        tmp_path,
        tracks: list[TrackEntry],
    ) -> RemuxPanel:
        cfg = AppConfig()
        panel = RemuxPanel(cfg)
        src = tmp_path / "source.mkv"
        src.touch()
        info = _file_info(
            path=src,
            videos=[
                _video(index=0, width=1920, height=1080),
                _video(index=1, width=3840, height=2160, hdr_type=HDRType.DOLBY_VISION),
            ],
        )
        sf = SourceFile(id="fid", path=src, color=_COLOR_A, info=info, tracks=tracks)
        panel._source_files = [sf]
        panel._source_colors = {"fid": _COLOR_A}
        panel._source_names = {"fid": "source.mkv"}
        panel._track_table.append_tracks(_COLOR_A, tracks)
        panel._output_edit.setText(str(tmp_path / "out.mkv"))
        return panel

    @staticmethod
    def _row_for_entry(panel: RemuxPanel, entry: TrackEntry) -> int:
        for row in range(panel._track_table.rowCount()):
            item = panel._track_table.item(row, _TrackTable.COL_CHECK)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) is entry:
                return row
        raise AssertionError(f"entry not found: {entry.entry_id}")

    def test_table_checkbox_change_reemits_video_tracks(self, qt_app, tmp_path):
        video_a = _track(0, "video", file_id="fid", codec="HEVC")
        video_b = _track(1, "video", file_id="fid", codec="HEVC")
        panel = self._panel_with_video_tracks(qt_app, tmp_path, [video_a, video_b])
        emitted: list = []
        panel.video_tracks_changed.connect(lambda tracks: emitted.append(tracks))

        row = self._row_for_entry(panel, video_b)
        row_item = panel._track_table.item(row, _TrackTable.COL_CHECK)
        assert row_item is not None
        row_item.setCheckState(Qt.CheckState.Unchecked)
        panel._on_table_changed()

        assert emitted
        assert [entry.entry_id for _info, entry, _color in emitted[-1]] == [video_a.entry_id]
        panel.close()

    def test_video_checkbox_change_does_not_rebuild_audio_projection(self, qt_app, tmp_path):
        video = _track(0, "video", file_id="fid", codec="HEVC")
        panel = self._panel_with_video_tracks(qt_app, tmp_path, [video])
        video_emitted: list = []
        audio_emitted: list = []
        panel.video_tracks_changed.connect(video_emitted.append)
        panel.audio_tracks_changed.connect(audio_emitted.append)

        row = self._row_for_entry(panel, video)
        row_item = panel._track_table.item(row, _TrackTable.COL_CHECK)
        assert row_item is not None
        row_item.setCheckState(Qt.CheckState.Unchecked)

        assert video_emitted == [[]]
        assert audio_emitted == []
        panel.close()

    def test_reemitted_video_tracks_are_detached_objects(self, qt_app, tmp_path):
        video = _track(0, "video", file_id="fid", codec="HEVC")
        panel = self._panel_with_video_tracks(qt_app, tmp_path, [video])
        emitted: list = []
        panel.video_tracks_changed.connect(lambda tracks: emitted.append(tracks))

        panel._emit_video_tracks()

        assert emitted
        emitted_entry = emitted[-1][0][1]
        assert emitted_entry is not video
        emitted_entry.encode_plan_codec = "libx264"
        assert video.encode_plan_codec == ""
        panel.close()


def test_inspect_file_routes_verbose_inspector_output_to_panel_signal(tmp_path):
    path = tmp_path / "movie.mkv"
    path.touch()
    info = FileInfo(path=path, format="matroska,webm", duration_s=None, size_bytes=None, bit_rate=None)
    verbose_lines: list[tuple[str, str]] = []

    panel = SimpleNamespace(
        _config=SimpleNamespace(tool_ffprobe="ffprobe", tool_mediainfo="mediainfo"),
        tool_output=SimpleNamespace(emit=lambda label, line: verbose_lines.append((str(label), str(line)))),
        _inspection_done=SimpleNamespace(emit=MagicMock()),
        _inspection_error=SimpleNamespace(emit=MagicMock()),
        log_message=SimpleNamespace(emit=MagicMock()),
    )

    callback_holder: dict[str, object] = {}
    inspector_instance = MagicMock()

    def fake_ctor(*args, **kwargs):
        callback_holder["verbose_output"] = kwargs["verbose_output"]
        return inspector_instance

    def fake_inspect(_path):
        callback = callback_holder["verbose_output"]
        assert callable(callback)
        callback("Inspection démarrée : /tmp/movie.mkv")
        return info

    inspector_instance.inspect.side_effect = fake_inspect

    with patch.object(inspection_functions, "FileInspector", side_effect=fake_ctor):
        inspection_functions.inspect_file(cast(Any, panel), "fid-1", path)

    assert verbose_lines == [("inspector", "Inspection démarrée : /tmp/movie.mkv")]
    panel._inspection_done.emit.assert_called_once_with("fid-1", info)
    panel._inspection_error.emit.assert_not_called()


def test_add_files_uses_dedicated_inspection_executor(tmp_path):
    first = tmp_path / "first.mkv"
    second = tmp_path / "second.mkv"
    inspection_executor = MagicMock()
    audio_sync_executor = MagicMock()
    panel = SimpleNamespace(
        _source_files=[],
        _source_names={},
        _source_colors={},
        _color_index=0,
        _file_list=MagicMock(),
        _inspection_executor=inspection_executor,
        _executor=audio_sync_executor,
        _inspect_file=MagicMock(),
        _sync_tmdb_suggested_title=MagicMock(),
        log_message=SimpleNamespace(emit=MagicMock()),
    )

    inspection_functions.on_add_files(cast(Any, panel), [str(first), str(second)])

    assert inspection_executor.submit.call_count == 2
    assert audio_sync_executor.submit.call_count == 0
    assert [call.args[2] for call in inspection_executor.submit.call_args_list] == [first, second]
    assert len(panel._source_files) == 2


# ===========================================================================
# _AttachmentItemWidget — case cochée par défaut (tags et attachements)
# ===========================================================================

class TestAttachmentItemWidgetDefaultChecked:

    def test_attachment_checked_by_default(self, qt_app):
        """Un attachement normal (is_tag=False) est coché par défaut."""
        w = _AttachmentItemWidget(file_id="fid")
        assert w.enabled is True
        w.close()

    def test_tag_checked_by_default(self, qt_app):
        """Un widget de balises (is_tag=True) est coché par défaut (nouveau comportement)."""
        tags = {"COLLECTION": "MyShow", "SEASON": "1", "EPISODE": "2"}
        w = _AttachmentItemWidget(file_id="fid", is_tag=True, tags=tags)
        assert w.enabled is True
        assert w.tag_count == 3
        w.close()

    def test_manual_image_attachment_is_interactive(self, qt_app, tmp_path):
        image = tmp_path / "cover.jpg"
        image.write_bytes(b"\xff\xd8\xff\xd9")

        w = _AttachmentItemWidget(file_id="", is_manual=True, manual_path=image)

        assert w._supports_image_interaction() is True
        assert isinstance(w._name_widget, _AttachmentNameButton)
        w.close()

    def test_manual_text_attachment_is_interactive(self, qt_app, tmp_path):
        text_path = tmp_path / "info.xml"
        text_path.write_text("<root><title>Demo</title></root>", encoding="utf-8")

        w = _AttachmentItemWidget(file_id="", is_manual=True, manual_path=text_path)

        assert w._supports_text_interaction() is True
        assert isinstance(w._name_widget, _AttachmentNameButton)
        w.close()

    def test_source_attachment_without_local_path_is_not_interactive(self, qt_app):
        w = _AttachmentItemWidget(
            file_id="fid",
            att=AttachmentInfo(
                index=3,
                local_index=0,
                filename="cover.jpg",
                mimetype="image/jpeg",
                is_attached_pic=True,
            ),
        )

        assert w._supports_interaction() is False
        assert not isinstance(w._name_widget, _AttachmentNameButton)
        w.close()

    def test_source_attachment_with_loader_is_interactive_and_cached(self, qt_app):
        payload = b"embedded-cover"
        calls = {"count": 0}

        def _loader(file_id: str, att: AttachmentInfo) -> bytes | None:
            calls["count"] += 1
            assert file_id == "fid"
            assert att.local_index == 0
            return payload

        w = _AttachmentItemWidget(
            file_id="fid",
            att=AttachmentInfo(
                index=3,
                local_index=0,
                filename="cover.jpg",
                mimetype="image/jpeg",
                is_attached_pic=False,
            ),
            embedded_attachment_loader=_loader,
        )

        assert w._supports_image_interaction() is True
        assert isinstance(w._name_widget, _AttachmentNameButton)
        assert w._load_embedded_attachment_bytes() == payload
        assert w._load_embedded_attachment_bytes() == payload
        assert calls["count"] == 1
        w.close()


class TestAttachmentPreviewFormatting:

    def test_pretty_text_attachment_content_formats_xml(self, tmp_path):
        xml_path = tmp_path / "info.xml"
        xml_path.write_text("<root><title>Demo</title></root>", encoding="utf-8")

        content = _pretty_text_attachment_content(xml_path)

        assert "<root>" in content
        assert "  <title>Demo</title>" in content

    def test_pretty_text_attachment_content_keeps_plain_text(self, tmp_path):
        txt_path = tmp_path / "notes.txt"
        txt_path.write_text("line 1\nline 2", encoding="utf-8")

        content = _pretty_text_attachment_content(txt_path)

        assert content == "line 1\nline 2"


class TestMatroskaAttachmentExtractor:

    def test_attachment_data_returns_expected_payload(self, tmp_path):
        src = tmp_path / "sample.mkv"
        _make_mkv_with_attachments(
            src,
            [
                ("cover.jpg", b"image-bytes"),
                ("info.xml", b"<root><demo>ok</demo></root>"),
            ],
        )

        payload = MatroskaReader(src).attachment_data(1)

        assert payload == b"<root><demo>ok</demo></root>"


class TestAttachmentPanelTmdb:

    def test_apply_tmdb_details_adds_i18n_comments_and_pending_cover(self, qt_app, tmp_path):
        """
        Depuis le téléchargement différé, la cover TMDB n'est plus sauvegardée sur
        disque lors de _apply_tmdb_details() ; get_pending_tmdb_cover() retourne l'URL.
        """
        prev_lang = current_language()
        panel = None
        try:
            set_current_language("eng")
            cfg = AppConfig()
            cfg.work_dir = tmp_path
            panel = _AttachmentPanel(cfg)

            details = MediaDetails(
                title="My Show",
                synopsis="Episode overview",
                cover_url="https://img.tmdb.org/poster.jpg",
                cover_filename="cover.jpg",
            )
            panel._apply_tmdb_details(details, open_editor=False)

            tags = panel.get_global_tag_overrides()
            assert tags is not None
            assert tags["COMMENTS"] == "Media information retrieved from TMDB."

            # La cover est en mode « en attente » : pas de fichier sur disque.
            assert panel.get_extra_attachments() == []
            pending = panel.get_pending_tmdb_cover()
            assert pending is not None
            assert pending[0] == "https://img.tmdb.org/poster.jpg"
            assert pending[1] == "cover.jpg"
        finally:
            if panel is not None:
                panel.close()
            set_current_language(prev_lang)

    def test_apply_tmdb_details_deselects_source_cover_and_installs_tmdb_cover(self, qt_app, tmp_path):
        """
        Quand une cover source existe déjà, la cover TMDB est tout de même
        installée (en attente) et la cover source est décochée.
        """
        cfg = AppConfig()
        cfg.work_dir = tmp_path
        panel = _AttachmentPanel(cfg)
        panel.add_source_attachments(
            "fid",
            "#ffffff",
            [AttachmentInfo(
                index=5,
                local_index=0,
                filename="cover.jpg",
                mimetype="image/jpeg",
                is_attached_pic=True,
            )],
        )

        details = MediaDetails(
            title="My Show",
            cover_url="https://img.tmdb.org/poster.jpg",
            cover_filename="cover.jpg",
        )
        panel._apply_tmdb_details(details, open_editor=False)

        # La cover TMDB est en attente (cochée), la cover source est décochée.
        pending = panel.get_pending_tmdb_cover()
        assert pending is not None
        assert pending[0] == "https://img.tmdb.org/poster.jpg"
        # get_extra_attachments ne retourne pas les covers pending TMDB.
        assert panel.get_extra_attachments() == []
        panel.close()

    def test_source_cover_added_after_tmdb_cover_is_deselected(self, qt_app, tmp_path):
        """
        Quand une cover TMDB est en attente et qu'une cover source est ajoutée
        ensuite, la cover source est automatiquement décochée.
        La cover TMDB reste en attente.
        """
        cfg = AppConfig()
        cfg.work_dir = tmp_path
        panel = _AttachmentPanel(cfg)

        details = MediaDetails(
            title="My Show",
            cover_url="https://img.tmdb.org/poster.jpg",
            cover_filename="cover.jpg",
        )
        panel._apply_tmdb_details(details, open_editor=False)
        assert panel.get_pending_tmdb_cover() is not None

        panel.add_source_attachments(
            "fid",
            "#ffffff",
            [AttachmentInfo(
                index=5,
                local_index=0,
                filename="cover.jpg",
                mimetype="image/jpeg",
                is_attached_pic=True,
            )],
        )

        # La cover TMDB est toujours en attente.
        assert panel.get_pending_tmdb_cover() is not None
        # La cover source est décochée → get_extra_attachments vide.
        assert panel.get_extra_attachments() == []
        panel.close()

    def test_clear_auto_tmdb_cover_removes_pending_item(self, qt_app, tmp_path):
        """Après _clear_auto_tmdb_cover_item(), get_pending_tmdb_cover() retourne None."""
        cfg = AppConfig()
        cfg.work_dir = tmp_path
        panel = _AttachmentPanel(cfg)

        details = MediaDetails(
            title="My Show",
            cover_url="https://img.tmdb.org/poster.jpg",
            cover_filename="cover.jpg",
        )
        panel._apply_tmdb_details(details, open_editor=False)
        assert panel.get_pending_tmdb_cover() is not None

        panel._clear_auto_tmdb_cover_item()

        assert panel.get_pending_tmdb_cover() is None
        assert panel.get_extra_attachments() == []
        panel.close()


class TestAttachmentPanelManualPaths:

    def test_add_manual_paths_deduplicates(self, qt_app, tmp_path):
        cfg = AppConfig()
        panel = _AttachmentPanel(cfg)
        sample = tmp_path / "poster.jpg"
        sample.write_bytes(b"x")

        panel.add_manual_paths([str(sample), str(sample)])

        extras = panel.get_extra_attachments()
        assert extras == [sample]
        panel.close()


class TestRemuxPanelGlobalDropRouting:

    def test_route_dropped_paths_sends_media_to_sources_and_others_to_attachments(self, qt_app, tmp_path):
        cfg = AppConfig()
        panel = RemuxPanel(cfg)
        src = tmp_path / "movie.mkv"
        att = tmp_path / "poster.jpg"
        src.write_bytes(b"src")
        att.write_bytes(b"att")

        with patch.object(panel, "_on_add_files") as mock_add_sources, \
             patch.object(panel._attachment_panel, "add_manual_paths") as mock_add_attachments:
            panel._route_dropped_paths([str(src), str(att)])

        mock_add_sources.assert_called_once_with([str(src)])
        mock_add_attachments.assert_called_once_with([str(att)])
        panel.close()

    def test_route_dropped_folder_keeps_only_sources_and_jpg_attachments(self, qt_app, tmp_path):
        cfg = AppConfig()
        panel = RemuxPanel(cfg)
        folder = tmp_path / "drop_folder"
        folder.mkdir()
        src = folder / "movie.mkv"
        cover = folder / "cover.jpg"
        ignored = folder / "notes.txt"
        nested = folder / "nested"
        nested.mkdir()
        nested_src = nested / "bonus.mp4"
        nested_cover = nested / "poster.JPG"
        nested_ignored = nested / "cover.png"
        src.write_bytes(b"src")
        cover.write_bytes(b"jpg")
        ignored.write_text("ignore")
        nested_src.write_bytes(b"nested")
        nested_cover.write_bytes(b"jpg")
        nested_ignored.write_bytes(b"png")

        with patch.object(panel, "_on_add_files") as mock_add_sources, \
             patch.object(panel._attachment_panel, "add_manual_paths") as mock_add_attachments:
            panel._route_dropped_paths([str(folder)])

        mock_add_sources.assert_called_once_with([str(src), str(nested_src)])
        mock_add_attachments.assert_called_once_with([str(cover), str(nested_cover)])
        panel.close()

    def test_route_dropped_multiple_folders_merges_and_deduplicates_paths(self, qt_app, tmp_path):
        cfg = AppConfig()
        panel = RemuxPanel(cfg)
        folder_a = tmp_path / "folder_a"
        folder_b = tmp_path / "folder_b"
        folder_a.mkdir()
        folder_b.mkdir()
        src_a = folder_a / "episode1.mkv"
        src_b = folder_b / "episode2.mkv"
        cover_a = folder_a / "cover.jpg"
        cover_b = folder_b / "cover.jpg"
        ignored_b = folder_b / "readme.nfo"
        src_a.write_bytes(b"a")
        src_b.write_bytes(b"b")
        cover_a.write_bytes(b"jpg-a")
        cover_b.write_bytes(b"jpg-b")
        ignored_b.write_text("ignore")

        with patch.object(panel, "_on_add_files") as mock_add_sources, \
             patch.object(panel._attachment_panel, "add_manual_paths") as mock_add_attachments:
            panel._route_dropped_paths([str(folder_a), str(folder_b), str(folder_a)])

        mock_add_sources.assert_called_once_with([str(src_a), str(src_b)])
        mock_add_attachments.assert_called_once_with([str(cover_a), str(cover_b)])
        panel.close()


class TestRemuxRunCleanup:

    def test_run_cleans_process_subdir(self, qt_app, tmp_path):
        """Le dossier process temporaire est supprimé après le remuxage."""
        src = tmp_path / "source.mkv"
        src.write_bytes(b"src")
        out = tmp_path / "output.mkv"
        work_dir = tmp_path / "work"

        cfg = RemuxConfig(
            sources=[_source(src, 0, [_track(0, "video", language="", orig_language="")])],
            output=out,
            track_order=[(0, 0)],
            keep_chapters=False,
            work_dir=work_dir,
            tag_overrides={},
        )
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")

        def _fake_run_cmd(cmd, **_kwargs):
            out.write_bytes(b"mkv")
            return ""

        with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run_cmd):
            wf.run(cfg)
            process_dir = work_dir / "output"
            cleanup_deadline = time.monotonic() + 2.0
            while process_dir.exists() and time.monotonic() < cleanup_deadline:
                qt_app.processEvents()
                time.sleep(0.01)

        assert out.exists()
        assert not process_dir.exists()


# ===========================================================================
# ChapterEntry / build_chapter_xml
# ===========================================================================

class TestChapterEntry:

    def test_chapter_entry_fields(self):
        e = ChapterEntry(timecode_s=3661.5, name="Acte 2")
        assert e.timecode_s == pytest.approx(3661.5)
        assert e.name == "Acte 2"

    def test_chapter_info_count_property(self):
        ci = ChapterInfo(entries=[
            ChapterEntry(0.0, "Intro"),
            ChapterEntry(300.0, "Part 1"),
        ])
        assert ci.count == 2

    def test_chapter_info_empty(self):
        ci = ChapterInfo()
        assert ci.count == 0
        assert ci.entries == []


class TestBuildChapterXml:

    def test_xml_contains_chapter_name(self):
        entries = [ChapterEntry(0.0, "Intro"), ChapterEntry(300.0, "Acte 1")]
        xml = build_chapter_xml(entries)
        assert "Intro" in xml
        assert "Acte 1" in xml

    def test_xml_timecode_format(self):
        entries = [ChapterEntry(3661.5, "Test")]
        xml = build_chapter_xml(entries)
        # 3661.5 s = 01:01:01.500000000
        assert "01:01:01." in xml

    def test_xml_sorted_by_timecode(self):
        entries = [
            ChapterEntry(300.0, "Second"),
            ChapterEntry(0.0,   "First"),
        ]
        xml = build_chapter_xml(entries)
        assert xml.index("First") < xml.index("Second")

    def test_xml_special_chars_escaped(self):
        entries = [ChapterEntry(0.0, "Chapitre <1> & 'special'")]
        xml = build_chapter_xml(entries)
        assert "<1>" not in xml
        assert "&lt;" in xml or "&#" in xml or "&amp;" in xml

    def test_xml_empty_name(self):
        entries = [ChapterEntry(0.0, "")]
        xml = build_chapter_xml(entries)
        assert "<ChapterString></ChapterString>" in xml


class TestRemuxWorkflowPostMetadata:

    def _cfg(self, output: Path, *, tags: dict[str, str] | None = None) -> RemuxConfig:
        t = _track(0, "video", file_id="id0")
        src = SourceInput(path=Path("/a.mkv"), file_index=0, tracks=[t])
        return RemuxConfig(
            sources=[src],
            output=output,
            track_order=[(0, 0)],
            tag_overrides=tags,
        )

    def test_resolved_global_tags_keeps_only_user_tags(self, tmp_path):
        output = tmp_path / "out.mkv"
        cfg = self._cfg(output, tags={"GENRE": "Drama", "EMPTY": "   "})
        tags = resolved_global_tags(cfg)

        assert tags["GENRE"] == "Drama"
        assert "EMPTY" not in tags

    def test_run_invokes_matroska_header_patch_post_action(self, qt_app, tmp_path):
        src = tmp_path / "source.mkv"
        src.write_bytes(b"src")
        out = tmp_path / "output.mkv"
        cfg = RemuxConfig(
            sources=[_source(src, 0, [_track(0, "video", file_id="id0")])],
            output=out,
            track_order=[(0, 0)],
            keep_chapters=False,
            work_dir=tmp_path / "work",
            tag_overrides=None,
        )
        wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")

        def _fake_run_cmd(cmd, **_kwargs):
            out.write_bytes(b"mkv")
            return ""

        with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run_cmd):
            with patch.object(wf._muxing_post_action, "apply_if_mkv") as patch_hook:
                wf.run(cfg)
                deadline = time.monotonic() + 2.0
                while not patch_hook.called and time.monotonic() < deadline:
                    qt_app.processEvents()
                    time.sleep(0.01)

        assert patch_hook.called


def test_remux_panel_backend_selector_is_initialized_but_job_local(qt_app):
    config = AppConfig()
    config.matroska_mux_backend = "native"
    panel = RemuxPanel(config)

    assert panel._mux_backend_combo.currentData() == "native"
    panel._mux_backend_combo.setCurrentIndex(panel._mux_backend_combo.findData("ffmpeg"))

    assert panel._mux_backend_combo.currentData() == "ffmpeg"
    assert config.matroska_mux_backend == "native"


def test_remux_panel_refreshes_backend_after_settings_save(qt_app):
    config = AppConfig()
    config.matroska_mux_backend = "native"
    panel = RemuxPanel(config)

    config.matroska_mux_backend = "ffmpeg"
    panel.refresh_runtime_settings()

    assert panel._mux_backend_combo.currentData() == "ffmpeg"


def test_remux_panel_debounces_preview_and_compiles_it_off_the_ui_thread(qt_app):
    """Une rafale tableau ne doit ni compiler deux fois ni figer Qt."""
    panel = RemuxPanel(AppConfig())
    started = Event()
    release = Event()
    sentinel_config = object()

    def _slow_preview(_config):
        started.set()
        assert release.wait(timeout=1)
        return "preview à jour"

    try:
        with patch.object(panel, "_current_config", return_value=sentinel_config), \
             patch.object(panel._workflow, "preview_command", side_effect=_slow_preview) as preview:
            panel._rebuild_preview()
            panel._rebuild_preview()

            # Avant l'expiration du debounce, aucun préflight n'est déclenché.
            assert preview.call_count == 0
            deadline = time.monotonic() + 1.0
            while not started.is_set() and time.monotonic() < deadline:
                qt_app.processEvents()
                time.sleep(0.01)
            assert started.is_set()
            assert preview.call_count == 1

            # Le worker reste bloqué, mais la boucle Qt continue de traiter les
            # événements ; le texte n'est appliqué qu'une fois le calcul fini.
            qt_app.processEvents()
            assert panel._cmd_preview.toPlainText() != "preview à jour"
            release.set()
            deadline = time.monotonic() + 1.0
            while panel._cmd_preview.toPlainText() != "preview à jour" and time.monotonic() < deadline:
                qt_app.processEvents()
                time.sleep(0.01)

        assert panel._cmd_preview.toPlainText() == "preview à jour"
    finally:
        release.set()
        panel.close()
