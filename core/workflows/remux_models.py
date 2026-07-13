"""
core/workflows/remux_models.py — Modèles de données pour le remuxage.

Classes publiques :
    TrackEntry           — piste avec état d'inclusion et métadonnées éditables
    SourceInput          — un fichier source avec ses pistes associées
    RemuxConfig          — configuration complète d'un remuxage (multi-source)
    RemuxError           — exception levée par le workflow
    tracks_from_file_info — fabrique une liste de TrackEntry depuis un FileInfo
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import uuid4

from core.inspector import AttachmentInfo, FileInfo, HDRType


MUX_BACKENDS = frozenset({"auto", "native", "ffmpeg"})


def normalize_mux_backend(value: str | None) -> str:
    """Return a stable public backend name, rejecting unknown contracts."""
    backend = str(value or "auto").strip().lower()
    if backend not in MUX_BACKENDS:
        raise ValueError(
            "mux_backend invalide : attendu 'auto', 'native' ou 'ffmpeg'."
        )
    return backend


# =============================================================================
# Modèle de piste
# =============================================================================

@dataclass
class TrackEntry:
    """
    Représente une piste avec son état d'inclusion et ses métadonnées éditables.

    mkv_tid est l'identifiant de piste (= index ffprobe pour les fichiers MKV).
    file_id est l'identifiant UUID du SourceFile parent (géré par l'UI).
    orig_language / orig_title conservent les valeurs d'origine pour détecter
    les modifications et n'émettre des flags que si nécessaire.
    """

    mkv_tid:      int
    track_type:   str           # "video" | "audio" | "subtitle"
    codec:        str           # libellé court ("HEVC", "TRUEHD", ...)
    display_info: str           # résolution, canaux, flags... — lecture seule
    language:     str           # tag BCP-47 éditable ("fra", "eng", ...)
    title:        str           # titre de piste éditable
    enabled:      bool = True
    file_id:      str  = ""    # UUID du SourceFile parent (usage UI uniquement)
    time_shift_ms: int = 0      # décalage signé appliqué à la piste (ms)
    entry_id:      str = field(default_factory=lambda: uuid4().hex)
    source_entry_id: str = field(default="", repr=False)
    is_new:        bool = field(default=False, repr=False)

    orig_language: str = field(default="", repr=False)
    orig_title:    str = field(default="", repr=False)
    orig_codec:    str = field(default="", repr=False)
    orig_display_info: str = field(default="", repr=False)
    encode_plan_codec: str = field(default="", repr=False)
    encode_plan_summary: str = field(default="", repr=False)
    encode_plan_hdr_badges: tuple[str, ...] = field(default_factory=tuple, repr=False)
    encode_plan_modified: bool = field(default=False, repr=False)
    sync_rewrite_label: str = field(default="", repr=False)
    sync_rewrite_mode: str = field(default="", repr=False)  # "" = auto, "offset" = sync standard forcée

    # Flags MKV éditables (transmis à FFmpeg si modifiés)
    flag_enabled:          bool = field(default=True,  repr=False)  # --track-enabled-flag
    flag_default:          bool = field(default=False, repr=False)  # --default-track-flag
    flag_forced:           bool = field(default=False, repr=False)  # --forced-track
    flag_hearing_impaired: bool = field(default=False, repr=False)  # --hearing-impaired-flag
    flag_visual_impaired:  bool = field(default=False, repr=False)  # --visual-impaired-flag
    flag_original:         bool = field(default=False, repr=False)  # --original-flag
    flag_commentary:       bool = field(default=False, repr=False)  # --commentary-flag

    orig_flag_enabled:          bool = field(default=True,  repr=False)
    orig_flag_default:          bool = field(default=False, repr=False)
    orig_flag_forced:           bool = field(default=False, repr=False)
    orig_flag_hearing_impaired: bool = field(default=False, repr=False)
    orig_flag_visual_impaired:  bool = field(default=False, repr=False)
    orig_flag_original:         bool = field(default=False, repr=False)
    orig_flag_commentary:       bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if not self.orig_codec:
            self.orig_codec = self.codec
        if not self.orig_display_info:
            self.orig_display_info = self.display_info

    @property
    def flags_label(self) -> str:
        """Résumé court des flags actifs (pour la colonne Info du tableau)."""
        parts: list[str] = []
        if not self.flag_enabled:
            parts.append("désact.")
        if self.flag_default:
            parts.append("défaut")
        if self.flag_forced:
            parts.append("forcé")
        if self.flag_hearing_impaired:
            parts.append("malent.")
        if self.flag_visual_impaired:
            parts.append("malvoy.")
        if self.flag_original:
            parts.append("orig.")
        if self.flag_commentary:
            parts.append("comm.")
        return "  ·  ".join(parts)

    @property
    def full_info_label(self) -> str:
        """Info technique + flags actifs (affichage colonne Info)."""
        parts = [
            p
            for p in (
                "NEW" if self.is_new else "",
                self.display_info,
                self.flags_label,
                self.time_shift_label,
                self.sync_rewrite_label,
            )
            if p
        ]
        return "  ·  ".join(parts)

    @property
    def time_shift_value_label(self) -> str:
        """Valeur courte du décalage, formatée pour l'UI (ex: +125 ms)."""
        if self.time_shift_ms == 0:
            return ""
        return f"{self.time_shift_ms:+d} ms"

    @property
    def time_shift_label(self) -> str:
        """Libellé court du décalage affiché dans la colonne Info."""
        value = self.time_shift_value_label
        if not value:
            return ""
        return f"Δt {value}"

    @property
    def type_label(self) -> str:
        """Lettre courte pour l'affichage dans le tableau."""
        match self.track_type:
            case "video":
                return "V"
            case "audio":
                return "A"
            case "subtitle":
                return "S"
            case _:
                return "?"

    @property
    def type_long(self) -> str:
        """Libellé long du type de piste."""
        match self.track_type:
            case "video":
                return "Vidéo"
            case "audio":
                return "Audio"
            case "subtitle":
                return "Sous-titre"
            case _:
                return self.track_type


# =============================================================================
# Source d'entrée
# =============================================================================

@dataclass
class SourceInput:
    """
    Un fichier source avec toutes ses pistes (activées ou non).

    file_index est l'indice 0-based de ce fichier dans RemuxConfig.sources ;
    il est utilisé dans --track-order pour référencer les pistes.
    """

    path:                    Path
    file_index:              int                    # 0-based, --track-order
    tracks:                  list[TrackEntry]       # toutes les pistes (enabled ou non)
    # Attachements sélectionnés (vide = aucun).
    # AttachmentInfo.local_index + 1 = numéro d'attachement 1-based.
    # AttachmentInfo.index = stream index ffprobe pour ffmpeg -map.
    selected_attachments:    list[AttachmentInfo]   = field(default_factory=list)
    attachment_count:        int                    = 0   # total dans le fichier source
    copy_tags:               bool                   = False
    #: Indique que ce conteneur expose des chapitres exploitables (>0). Permet
    #: de cibler -map_chapters sur la bonne source quand keep_chapters=True et
    #: que la première source n'en contient pas.
    has_chapters:            bool                   = False
    #: Identité stable de la source d'origine, conservée lors des matérialisations temporaires.
    origin_identity:         str                    = field(default="", repr=False)


# =============================================================================
# Configuration de remuxage
# =============================================================================

@dataclass
class RemuxConfig:
    """
    Configuration complète d'un remuxage multi-source.

    sources           : liste ordonnée des fichiers source (chacun avec ses pistes).
    track_order       : liste de (file_index, mkv_tid[, entry_id]) dans l'ordre désiré
                        en sortie. ``entry_id`` permet de distinguer deux lignes UI
                        basées sur la même piste source.
                        Seules les pistes présentes dans track_order sont incluses.
    extra_attachments : fichiers externes à attacher en plus (--attach-file).
    """

    sources:             list[SourceInput]
    output:              Path
    track_order:         Sequence[tuple[int, int] | tuple[int, int, str]]
    keep_chapters:       bool          = True
    #: None  → FFmpeg recopie les chapitres des sources (comportement par défaut).
    #: list  → un fichier ffmetadata temporaire est généré depuis ces entrées ;
    #:         les chapitres sources sont ignorés (-map_chapters -1).
    chapter_overrides:   list | None   = None  # list[ChapterEntry] | None
    #: Index 0-based de la source à utiliser pour `-map_chapters` quand
    #: `keep_chapters=True` et qu'aucun override n'est défini. None → fallback
    #: automatique sur la première source dont `has_chapters=True`.
    chapter_source_index: int | None   = None
    extra_attachments:   list          = field(default_factory=list)  # list[Path]
    work_dir:            Path | None   = None
    file_title:          str           = ""      # balise Title du segment de sortie
    #: Balises MKV globales à écrire dans le fichier de sortie via post-traitement FFmpeg.
    #: None  → comportement par défaut (FFmpeg recopie les balises des sources).
    #: dict  → les balises sources sont ignorées (-map_metadata -1) et ce dict est écrit.
    #: {}    → supprime toutes les balises (-map_metadata -1, rien n'est écrit).
    tag_overrides:       dict[str, str] | None = None
    #: Cover TMDB à télécharger juste avant le remuxage : (url, filename).
    #: None → pas de cover TMDB en attente.
    tmdb_cover:          tuple[str, str] | None = None
    #: Autorise une preview CLI à construire la commande même si le dossier de
    #: sortie n'existe pas encore. Ne doit pas être utilisé pour une exécution.
    allow_missing_output_dir: bool = False
    #: Backend public du remux. ``auto`` préserve les exact-job v1 existants.
    mux_backend: str = "auto"


# =============================================================================
# Exception
# =============================================================================

class RemuxError(RuntimeError):
    """Erreur levée lors de la validation ou de l'exécution du remuxage."""


# =============================================================================
# Fabrique depuis FileInfo
# =============================================================================

def tracks_from_file_info(info: FileInfo, file_id: str = "") -> list[TrackEntry]:
    """
    Construit la liste des TrackEntry depuis un FileInfo inspecté.

    L'ordre est : pistes vidéo, puis audio, puis sous-titres.
    file_id permet d'associer chaque piste à un SourceFile de l'UI.
    """
    entries: list[TrackEntry] = []

    def _flags_from_disp(raw: dict) -> dict:
        disp = raw.get("disposition", {})
        return dict(
            flag_default          = bool(disp.get("default",          0)),
            flag_forced           = bool(disp.get("forced",           0)),
            flag_hearing_impaired = bool(disp.get("hearing_impaired", 0)),
            flag_visual_impaired  = bool(disp.get("visual_impaired",  0)),
            flag_original         = bool(disp.get("original",         0)),
            flag_commentary       = bool(disp.get("comment",          0)),
            orig_flag_default          = bool(disp.get("default",          0)),
            orig_flag_forced           = bool(disp.get("forced",           0)),
            orig_flag_hearing_impaired = bool(disp.get("hearing_impaired", 0)),
            orig_flag_visual_impaired  = bool(disp.get("visual_impaired",  0)),
            orig_flag_original         = bool(disp.get("original",         0)),
            orig_flag_commentary       = bool(disp.get("comment",          0)),
        )

    for v in info.video_tracks:
        parts: list[str] = [v.resolution]
        if v.hdr_type != HDRType.NONE:
            parts.append(v.hdr_label)
        if v.frame_rate:
            fr = v.frame_rate
            if "/" in fr:
                try:
                    num, den = fr.split("/")
                    fps = round(int(num) / int(den), 3)
                    fr = f"{fps} fps"
                except (ValueError, ZeroDivisionError):
                    pass
            else:
                fr = f"{fr} fps"
            parts.append(fr)
        entries.append(TrackEntry(
            mkv_tid=v.index,
            track_type="video",
            codec=v.codec.upper(),
            display_info="  ".join(parts),
            language=v.language or "",
            title=v.title or "",
            orig_language=v.language or "",
            orig_title=v.title or "",
            file_id=file_id,
            **_flags_from_disp(v.raw),
        ))

    for a in info.audio_tracks:
        parts = [a.channels_label]
        if a.bit_rate:
            parts.append(f"{a.bit_rate // 1000} kbps")
        if a.atmos_flag:
            parts.append("Atmos")
        elif a.dtsx_flag:
            parts.append("DTS:X")
        entries.append(TrackEntry(
            mkv_tid=a.index,
            track_type="audio",
            codec=a.codec.upper(),
            display_info="  ".join(parts),
            language=a.language or "",
            title=a.title or "",
            orig_language=a.language or "",
            orig_title=a.title or "",
            file_id=file_id,
            **_flags_from_disp(a.raw),
        ))

    for s in info.subtitle_tracks:
        disp_flags = _flags_from_disp(s.raw)
        # SubtitleTrack.forced / .default are the authoritative source;
        # override whatever raw["disposition"] may (or may not) contain.
        disp_flags["flag_forced"]      = s.forced
        disp_flags["orig_flag_forced"] = s.forced
        disp_flags["flag_default"]      = s.default
        disp_flags["orig_flag_default"] = s.default
        display_info = f"{s.element_count} éléments" if s.element_count else ""
        entries.append(TrackEntry(
            mkv_tid=s.index,
            track_type="subtitle",
            codec=s.codec.upper(),
            display_info=display_info,
            language=s.language or "",
            title=s.title or "",
            orig_language=s.language or "",
            orig_title=s.title or "",
            file_id=file_id,
            **disp_flags,
        ))

    return entries


def clone_track_entry(
    entry: TrackEntry,
    *,
    entry_id: str | None = None,
) -> TrackEntry:
    """Clone une piste pour en faire une entrée UI indépendante."""
    source_entry_id = entry.source_entry_id or entry.entry_id
    return replace(
        entry,
        entry_id=entry_id or uuid4().hex,
        source_entry_id=source_entry_id,
        is_new=True,
        orig_language=entry.language,
        orig_title=entry.title,
        orig_flag_enabled=entry.flag_enabled,
        orig_flag_default=entry.flag_default,
        orig_flag_forced=entry.flag_forced,
        orig_flag_hearing_impaired=entry.flag_hearing_impaired,
        orig_flag_visual_impaired=entry.flag_visual_impaired,
        orig_flag_original=entry.flag_original,
        orig_flag_commentary=entry.flag_commentary,
    )
