"""
core/inspector.py — Inspection et analyse de fichiers vidéo MKV/MP4.

Classes publiques :
    VideoTrack      — dataclass modélisant une piste vidéo
    AudioTrack      — dataclass modélisant une piste audio
    SubtitleTrack   — dataclass modélisant une piste de sous-titres
    ChapterTrack    — dataclass modélisant les chapitres
    FileInfo        — agrégat complet d'un fichier inspecté
    HDRType         — enum des formats HDR détectés
    FileInspector   — moteur d'inspection via ffprobe + mediainfo

Conventions :
    - Jamais shell=True
    - pathlib.Path pour tous les chemins
    - ffprobe pour l'inventaire des pistes (JSON)
    - mediainfo pour le frame count et les métadonnées HDR complémentaires
    - Toutes les méthodes publiques sont thread-safe (pas d'état mutable partagé)
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any

from core.bluray import (
    BluRayTitle,
    ffprobe_input_args,
    is_bluray_playlist,
    title_for_playlist,
)
from core.lang_tags import Rfc5646LanguageTags
from core.subprocess_utils import subprocess_text_kwargs


# =============================================================================
# Tags MKV standard (spec Matroska officielle)
# =============================================================================

#: Noms de balises MKV reconnus par la spec officielle Matroska.
#: Sert à distinguer les tags standard des tags propriétaires (préfixe _, etc.)
#: lors de l'affichage et de l'édition dans l'UI.
STANDARD_MKV_TAGS: frozenset[str] = frozenset({
    "TITLE", "SUBTITLE", "URL", "SYNOPSIS", "DESCRIPTION",
    "KEYWORDS", "SUMMARY", "COMMENT", "COLLECTION",
    "SEASON", "EPISODE", "PART_NUMBER",
    "DATE_RECORDED", "DATE_TAGGED", "DATE_RELEASED",
    "DATE_ENCODED", "DATE_WRITTEN", "DATE_PURCHASED",
    "ENCODER", "ENCODER_SETTINGS", "ORIGINAL",
    "DIRECTOR", "CAST", "GENRE", "MOOD",
    "RATING", "COUNTRY", "LANGUAGE",
    "LAW_RATING", "ICRA",
    "TOTAL_PARTS", "PART_OFFSET",
    "SORT_WITH", "INSTRUMENTS",
    "EMAIL", "PHONE", "FAX", "ADDRESS",
    "MEASURE", "TUNING",
    "REPLAY_GAIN_GAIN", "REPLAY_GAIN_PEAK",
    "POPULARITY_METER", "PLAY_COUNTER", "RATING",
    "SOURCE", "SOURCE_ID", "BPS", "DURATION",
    "NUMBER_OF_FRAMES", "NUMBER_OF_BYTES",
})

#: Balises de la source à ne pas transporter dans les fichiers de sortie.
_EXCLUDED_SOURCE_TAGS: frozenset[str] = frozenset({"TITLE", "ENCODER", "CREATION_TIME"})


# =============================================================================
# Enum HDR
# =============================================================================

class HDRType(Enum):
    """
    Type de métadonnées HDR détecté dans le flux vidéo principal.

    Ordre de priorité (du plus riche au plus pauvre) :
        DOLBY_VISION_HDR10PLUS > DOLBY_VISION > HDR10PLUS > HDR10 > HLG > NONE
    """
    NONE                  = auto()
    HLG                   = auto()
    HDR10                 = auto()
    HDR10PLUS             = auto()
    DOLBY_VISION          = auto()
    DOLBY_VISION_HDR10PLUS = auto()

    def label(self) -> str:
        """Libellé court pour l'affichage dans l'UI."""
        return {
            HDRType.NONE:                   "SDR",
            HDRType.HLG:                    "HLG",
            HDRType.HDR10:                  "HDR10",
            HDRType.HDR10PLUS:              "HDR10+",
            HDRType.DOLBY_VISION:           "Dolby Vision",
            HDRType.DOLBY_VISION_HDR10PLUS: "Dolby Vision + HDR10+",
        }[self]


# =============================================================================
# Dataclasses de pistes
# =============================================================================

@dataclass
class VideoTrack:
    """Piste vidéo extraite depuis ffprobe."""
    index:           int
    codec:           str          # "hevc", "h264", "av1"…
    codec_long:      str          # "H.265 / HEVC (High Efficiency Video Coding)"
    width:           int | None
    height:          int | None
    frame_rate:      str | None   # "23.976025" ou "24000/1001"
    bit_depth:       int | None   # 8, 10, 12
    color_space:     str | None   # "yuv420p10le"…
    color_primaries: str | None   # "bt2020"
    color_transfer:  str | None   # "smpte2084" (PQ), "arib-std-b67" (HLG)
    color_matrix:    str | None   # "bt2020nc"
    hdr_type:        HDRType      = HDRType.NONE
    dovi_profile:    int | None   = None   # ex: 8 pour P8.x
    dovi_compat_id:  int | None   = None   # 0=P8.0, 1=P8.1
    language:        str | None   = None
    title:           str | None   = None
    duration_s:      float | None = None
    bit_rate:        int | None   = None   # bps
    raw:             dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def resolution(self) -> str:
        if self.width and self.height:
            return f"{self.width}×{self.height}"
        return "?"

    @property
    def is_hdr(self) -> bool:
        return self.hdr_type != HDRType.NONE

    @property
    def hdr_label(self) -> str:
        """
        Label d'affichage enrichi : profil DoVi + couche de compatibilité.

        Mappage compat_id pour DoVi Profile 8 :
            P8.0 → DoVi only (pas de fallback)
            P8.1 → HDR10
            P8.2 → SDR (BT.709)
            P8.4 → HLG
        """
        if self.hdr_type == HDRType.NONE:
            return "SDR"

        parts: list[str] = []

        if self.hdr_type in (HDRType.DOLBY_VISION, HDRType.DOLBY_VISION_HDR10PLUS):
            dv = "Dolby Vision"
            if self.dovi_profile is not None:
                compat = self.dovi_compat_id
                if compat is not None:
                    dv += f" P{self.dovi_profile}.{compat}"
                else:
                    dv += f" P{self.dovi_profile}"
            parts.append(dv)

        if self.hdr_type == HDRType.DOLBY_VISION_HDR10PLUS:
            parts.append("HDR10+")
        elif self.hdr_type == HDRType.HDR10PLUS:
            parts.append("HDR10+")
        elif self.hdr_type == HDRType.HDR10:
            parts.append("HDR10")
        elif self.hdr_type == HDRType.HLG:
            parts.append("HLG")
        elif self.hdr_type == HDRType.DOLBY_VISION:
            compat_label = self.dovi_compat_label
            if compat_label:
                parts.append(compat_label)

        return " + ".join(parts) if parts else self.hdr_type.label()

    @property
    def dovi_compat_label(self) -> str | None:
        """Label de la couche de compatibilité DoVi P8.x selon compat_id (None pour P8.0)."""
        if self.dovi_profile != 8 or self.dovi_compat_id is None:
            return None
        return {1: "HDR10", 2: "SDR", 4: "HLG"}.get(self.dovi_compat_id)


@dataclass
class AudioTrack:
    """Piste audio extraite depuis ffprobe."""
    index:        int
    codec:        str          # "truehd", "eac3", "dts", "aac"…
    codec_long:   str
    channels:     int | None   # 2, 6, 8
    channel_layout: str | None # "stereo", "5.1(side)", "7.1"
    sample_rate:  int | None   # Hz
    bit_rate:     int | None   # bps
    language:     str | None
    title:        str | None
    duration_s:   float | None = None
    raw:          dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def channels_label(self) -> str:
        """Libellé court des canaux : '7.1', '5.1', 'Stereo'…"""
        if self.channel_layout:
            return self.channel_layout
        match self.channels:
            case 8:
                return "7.1"
            case 6:
                return "5.1"
            case 2:
                return "Stereo"
            case 1:
                return "Mono"
            case _:
                return str(self.channels) if self.channels else "?"

    @property
    def atmos_flag(self) -> bool:
        """True si la piste contient une couche Atmos (TrueHD Atmos ou E-AC-3 JOC)."""
        profile    = (self.raw.get("profile") or "").lower()
        title      = (self.title       or "").lower()
        codec_long = self.codec_long.lower()
        return (
            "atmos"  in profile    or
            "atmos"  in title      or
            "atmos"  in codec_long or
            "joc"    in profile    or
            "joc"    in codec_long
        )

    @property
    def dtsx_flag(self) -> bool:
        """True si la piste est DTS:X (XLL X)."""
        if self.codec.lower() != "dts":
            return False
        profile    = (self.raw.get("profile") or "").lower()
        title      = (self.title       or "").lower()
        codec_long = self.codec_long.lower()
        return (
            "dts-x"  in profile    or
            "dts:x"  in profile    or
            "dtsx"   in profile    or
            "dts-x"  in title      or
            "dts:x"  in title      or
            "xll x"  in codec_long or
            "dts-x"  in codec_long
        )


@dataclass
class SubtitleTrack:
    """Piste de sous-titres extraite depuis ffprobe."""
    index:    int
    codec:    str        # "subrip", "ass", "hdmv_pgs_subtitle", "dvd_subtitle"…
    language: str | None
    title:    str | None
    forced:   bool = False
    default:  bool = False
    element_count: int | None = None
    raw:      dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class ChapterEntry:
    """Un chapitre avec son code temporel et son nom."""
    timecode_s: float   # secondes depuis le début du fichier
    name:       str     # nom du chapitre (peut être vide)


@dataclass
class ChapterInfo:
    """Informations sur les chapitres du fichier."""
    entries: list[ChapterEntry] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.entries)


@dataclass
class AttachmentInfo:
    """
    Pièce jointe MKV (cover art, police de sous-titres, etc.).

    index       : index global ffprobe (utilisé pour -map dans ffmpeg).
    local_index : position 0-based parmi les attachements du fichier
                  (numéro d'attachement 1-based = local_index + 1).
    filename    : nom du fichier tel que stocké dans le MKV.
    mimetype    : type MIME (ex. "image/jpeg", "application/x-truetype-font").
    size_bytes  : taille en octets (None si non disponible via ffprobe).
    is_attached_pic : True si la pièce jointe provient d'un stream vidéo
                  marqué ``disposition.attached_pic=1``.
    """
    index:       int
    local_index: int
    filename:    str
    mimetype:    str
    size_bytes:  int | None = None
    is_attached_pic: bool = False


@dataclass
class FileInfo:
    """
    Résultat complet de l'inspection d'un fichier.

    Agrège toutes les pistes et métadonnées issues de ffprobe et mediainfo.
    """
    path:       Path
    format:     str          # "matroska,webm", "mov,mp4,m4a,3gp,3g2,mj2"…
    duration_s: float | None
    size_bytes: int | None
    bit_rate:   int | None

    video_tracks:    list[VideoTrack]    = field(default_factory=list)
    audio_tracks:    list[AudioTrack]    = field(default_factory=list)
    subtitle_tracks: list[SubtitleTrack] = field(default_factory=list)
    attachments:     list[AttachmentInfo] = field(default_factory=list)
    chapters:        ChapterInfo | None  = None

    frame_count:  int | None        = None   # via mediainfo
    tag_count:    int               = 0      # nombre de balises globales (via ffprobe format.tags)
    hdr_type:     HDRType           = HDRType.NONE  # du flux vidéo principal
    title:        str               = ""     # titre de segment (balise Title du conteneur)
    source_kind:  str               = "file" # "file" | "bluray"
    source_label: str               = ""     # libellé source synthétique (ex. 00418.mpls)
    bluray_playlist_id: int | None  = None
    bluray_segments: list[str]      = field(default_factory=list)
    mediainfo_json: dict[str, Any] | None = field(default=None, repr=False)
    #: Balises MKV globales du conteneur (clés en MAJUSCULES, hors TITLE).
    #: Inclut les tags standard et propriétaires (ex. _PROGRAM_LABEL).
    #: Peuplé depuis ffprobe format.tags lors de l'inspection.
    global_tags:  dict[str, str]    = field(default_factory=dict)

    @property
    def primary_video(self) -> VideoTrack | None:
        return self.video_tracks[0] if self.video_tracks else None

    @property
    def size_human(self) -> str:
        if self.size_bytes is None:
            return "?"
        for unit, threshold in [("Go", 1 << 30), ("Mo", 1 << 20), ("Ko", 1 << 10)]:
            if self.size_bytes >= threshold:
                return f"{self.size_bytes / threshold:.2f} {unit}"
        return f"{self.size_bytes} o"

    @property
    def duration_human(self) -> str:
        if self.duration_s is None:
            return "?"
        h = int(self.duration_s // 3600)
        m = int((self.duration_s % 3600) // 60)
        s = int(self.duration_s % 60)
        return f"{h:02d}:{m:02d}:{s:02d}"


# =============================================================================
# Exceptions
# =============================================================================

class InspectionError(RuntimeError):
    """Échec de l'inspection d'un fichier."""
    def __init__(self, path: Path, reason: str) -> None:
        self.path   = path
        self.reason = reason
        super().__init__(f"Inspection échouée pour {path.name} : {reason}")


# =============================================================================
# FileInspector
# =============================================================================

class FileInspector:
    """
    Analyse un fichier MKV/MP4 via ffprobe et mediainfo.

    Toutes les méthodes sont synchrones et thread-safe.
    Pour une utilisation asynchrone depuis l'UI Qt, encapsuler dans
    un worker (QThread ou ThreadPoolExecutor).

    Usage :
        inspector = FileInspector()
        info = inspector.inspect(Path("/films/movie.mkv"))
        print(info.primary_video.resolution)
        print(info.hdr_type.label())
        print(info.frame_count)
    """

    # ------------------------------------------------------------------
    # Constructeur
    # ------------------------------------------------------------------

    def __init__(
        self,
        ffprobe_bin:   str = "ffprobe",
        mediainfo_bin: str = "mediainfo",
        verbose_output: Callable[[str], None] | None = None,
    ) -> None:
        self._ffprobe   = ffprobe_bin
        self._mediainfo = mediainfo_bin
        self._verbose_output = verbose_output

    def _emit_verbose(self, line: str) -> None:
        callback = self._verbose_output
        if callback is None:
            return
        rendered = str(line).rstrip()
        if not rendered:
            return
        try:
            callback(rendered)
        except Exception:
            pass

    def _emit_command(self, cmd: list[str]) -> None:
        self._emit_verbose(f"$ {shlex.join([str(part) for part in cmd])}")

    def _emit_process_result(
        self,
        tool_name: str,
        result: subprocess.CompletedProcess[str],
        *,
        preview_stdout: bool = False,
    ) -> None:
        stdout = str(result.stdout or "")
        stderr = str(result.stderr or "")
        self._emit_verbose(
            f"{tool_name} rc={result.returncode} stdout={len(stdout.encode('utf-8'))}o stderr={len(stderr.encode('utf-8'))}o"
        )
        if preview_stdout:
            for line in stdout.strip().splitlines()[:3]:
                self._emit_verbose(f"{tool_name} stdout: {line[:400]}")
        for line in stderr.strip().splitlines()[-3:]:
            self._emit_verbose(f"{tool_name} stderr: {line[:400]}")

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def inspect(self, path: Path) -> FileInfo:
        """
        Inspecte un fichier et retourne un FileInfo complet.

        Lève :
            InspectionError : si le fichier est illisible ou si ffprobe échoue.
        """
        self._emit_verbose(f"Inspection démarrée : {path}")
        if not path.is_file():
            self._emit_verbose(f"Inspection impossible : fichier introuvable ({path})")
            raise InspectionError(path, "fichier introuvable")

        bluray_title = title_for_playlist(path) if is_bluray_playlist(path) else None
        raw = self._run_ffprobe(path)
        info = self._parse_ffprobe(path, raw)
        mediainfo_path = path
        if bluray_title is not None:
            info.source_kind = "bluray"
            info.source_label = bluray_title.label
            info.bluray_playlist_id = bluray_title.playlist_id
            info.bluray_segments = [segment.path.name for segment in bluray_title.segments]
            if bluray_title.duration_s > 0:
                info.duration_s = bluray_title.duration_s
            title_size = bluray_title.size_bytes
            if title_size > 0:
                info.size_bytes = title_size
            if bluray_title.segments:
                mediainfo_path = bluray_title.segments[0].path
            self._apply_bluray_stream_languages(info, bluray_title)

        # ── Enrichissement mediainfo (un seul appel JSON couvrant frame_count,
        #    HDR_Format, HDR_Format_Compatibility, profil DoVi). Remplace les
        #    3 appels --Inform séparés et accélère la phase HDR.
        mi_data = self._run_mediainfo_json(mediainfo_path)
        info.mediainfo_json = mi_data
        mi_video = self._mediainfo_video_track(mi_data) if mi_data else None
        if mi_data is not None:
            self._merge_track_counts_from_mediainfo(info, mi_data)

        # Frame count (issu de mediainfo JSON quand disponible, sinon fallback
        # legacy --Inform=FrameCount pour rester compatible avec très vieux mediainfo).
        if bluray_title is None and mi_video is not None:
            fc = mi_video.get("FrameCount")
            if isinstance(fc, str) and fc.isdigit():
                info.frame_count = int(fc)
        if bluray_title is None and info.frame_count is None:
            try:
                info.frame_count = self.get_frame_count(path)
            except Exception:
                pass

        # Enrichit le profil DoVi depuis mediainfo si ffprobe ne l'a pas fourni
        # (certains builds ffprobe ne remontent pas DOVI configuration record).
        if info.primary_video and mi_video is not None:
            self._merge_dovi_from_mediainfo(info.primary_video, mi_video)

        # Enrichissement MKV : tag count + language_ietf depuis le raw ffprobe
        # déjà parsé (évite un second appel à ffprobe).
        if "matroska" in info.format or "webm" in info.format:
            try:
                tag_count, ietf_langs = self._extract_mkv_track_data_from_raw(raw)
                info.tag_count = tag_count
                # Remplace les codes ISO 639-2 de ffprobe par les balises IETF
                # quand elles sont disponibles (ex : "en-US", "fr-FR").
                all_tracks: list[VideoTrack | AudioTrack | SubtitleTrack] = [
                    *info.video_tracks,
                    *info.audio_tracks,
                    *info.subtitle_tracks,
                ]
                for track in all_tracks:
                    lang = ietf_langs.get(track.index)
                    if lang is not None:
                        track.language = lang if lang != "und" else None
            except Exception:
                pass  # erreur non bloquante

        # Passe de normalisation finale : homogénéise tous les tags langue en IETF
        # régional lorsque possible, pour les entrées ISO 639-2 (xxx) et RFC 5646
        # courtes (xx). Les indices de région dans le titre restent prioritaires.
        all_tracks = [*info.video_tracks, *info.audio_tracks, *info.subtitle_tracks]
        for track in all_tracks:
            lang = (track.language or "").strip()
            if not lang:
                continue
            title = getattr(track, "title", None) or ""
            normalized = Rfc5646LanguageTags.regionalize_track_language(lang, title)
            track.language = normalized if normalized and normalized != "und" else None

        # HDR du flux vidéo principal — réutilise raw ffprobe ET mi_video.
        if info.primary_video:
            info.hdr_type = self._detect_hdr_from_raw(path, raw, mi_video=mi_video)
            info.primary_video.hdr_type = info.hdr_type

        chapter_count = info.chapters.count if info.chapters is not None else 0
        hdr_label = info.hdr_type.label() if info.primary_video else "Aucune piste vidéo"
        frame_count = info.frame_count if info.frame_count is not None else "?"
        self._emit_verbose(
            "Inspection terminée : "
            f"{path.name} V={len(info.video_tracks)} A={len(info.audio_tracks)} "
            f"S={len(info.subtitle_tracks)} PJ={len(info.attachments)} "
            f"Chap={chapter_count} HDR={hdr_label} Frames={frame_count}"
        )
        return info

    @staticmethod
    def _stream_pid_from_raw(raw: dict[str, Any]) -> int | None:
        value = raw.get("id")
        if value in (None, ""):
            return None
        try:
            return int(str(value), 0)
        except (TypeError, ValueError):
            return None

    def _apply_bluray_stream_languages(self, info: FileInfo, title: BluRayTitle) -> None:
        by_pid = title.stream_by_pid
        if not by_pid:
            return

        def apply_language(track: AudioTrack | SubtitleTrack) -> None:
            if (track.language or "").strip():
                return
            pid = self._stream_pid_from_raw(track.raw)
            if pid is None:
                return
            stream = by_pid.get(pid)
            if stream is None or not stream.language:
                return
            track.language = stream.language

        for audio_track in info.audio_tracks:
            apply_language(audio_track)
        for subtitle_track in info.subtitle_tracks:
            apply_language(subtitle_track)

    def get_frame_count(self, path: Path) -> int | None:
        """
        Retourne le frame count via ``mediainfo --Inform=Video;%FrameCount%``.

        Retourne None si mediainfo est absent ou si la valeur est illisible.
        """
        try:
            cmd = [self._mediainfo, "--Inform=Video;%FrameCount%", str(path)]
            self._emit_command(cmd)
            result = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                **subprocess_text_kwargs(),
                # shell=True JAMAIS
            )
            self._emit_process_result("mediainfo", result, preview_stdout=True)
            raw = result.stdout.strip()
            if re.fullmatch(r"\d+", raw):
                self._emit_verbose(f"mediainfo frame_count={raw}")
                return int(raw)
        except FileNotFoundError:
            self._emit_verbose("mediainfo introuvable dans PATH (frame count ignoré).")
        return None

    def detect_hdr_type(self, path: Path) -> HDRType:
        """
        Détecte le type HDR du flux vidéo principal.

        Stratégie de détection (par ordre de priorité) :
            1. Dolby Vision + HDR10+ : color_transfer PQ + DoVi NAL + HDR10+ SEI
            2. Dolby Vision seul      : présence NAL units DoVi (via mediainfo)
            3. HDR10+                 : présence SEI MDCV + MaxSCL (via mediainfo)
            4. HDR10                  : color_transfer=smpte2084 + master_display
            5. NONE / SDR

        Retourne HDRType.NONE si la détection échoue.
        """
        try:
            raw = self._run_ffprobe(path)
        except InspectionError:
            return HDRType.NONE
        return self._detect_hdr_from_raw(path, raw)

    def _detect_hdr_from_raw(
        self,
        path: Path,
        raw: dict[str, Any],
        *,
        mi_video: dict[str, Any] | None = None,
    ) -> HDRType:
        """
        Détecte le type HDR depuis un dict ffprobe déjà parsé.

        Utilisé en interne par inspect() pour éviter un second appel ffprobe.
        Si ``mi_video`` (track Video du JSON mediainfo) est fourni, il sert de
        source HDR enrichie et évite tout sous-appel mediainfo additionnel.
        """
        video_streams = [
            s for s in raw.get("streams", [])
            if s.get("codec_type") == "video"
            and not bool((s.get("disposition") or {}).get("attached_pic", 0))
        ]
        if not video_streams:
            return HDRType.NONE

        vs = video_streams[0]
        transfer = str(vs.get("color_transfer", "") or "")
        side_data_obj = vs.get("side_data_list")
        side_data = side_data_obj if isinstance(side_data_obj, list) else []

        has_pq           = transfer in ("smpte2084", "smpte2084le")
        has_hlg          = transfer == "arib-std-b67"
        has_master_disp  = any(sd.get("side_data_type") == "Mastering display metadata" for sd in side_data)
        has_cll          = any(sd.get("side_data_type") == "Content light level metadata" for sd in side_data)
        has_dovi         = any(sd.get("side_data_type") == "DOVI configuration record" for sd in side_data)
        has_hdr10plus    = any(sd.get("side_data_type") == "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)" for sd in side_data)

        # Certains fichiers reportent les side-data dynamiques sur un autre flux
        # vidéo que le premier (ou pas de façon stable selon ffprobe build).
        if not has_dovi or not has_hdr10plus:
            for stream in video_streams[1:]:
                other_side_obj = stream.get("side_data_list")
                other_side_data = other_side_obj if isinstance(other_side_obj, list) else []
                if not has_dovi and any(sd.get("side_data_type") == "DOVI configuration record" for sd in other_side_data):
                    has_dovi = True
                if not has_hdr10plus and any(
                    sd.get("side_data_type") == "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"
                    for sd in other_side_data
                ):
                    has_hdr10plus = True
                if has_dovi and has_hdr10plus:
                    break

        # Fallback mediainfo pour DoVi/HDR10+ (très rapide quand disponible).
        # mediainfo lit les SEI/NAL units : si présent et qu'il a répondu, on
        # lui fait confiance et on évite le coûteux probe frame-level (~3-6 s).
        mediainfo_responded = False
        if not has_dovi or not has_hdr10plus:
            if mi_video is not None:
                # Source unifiée mediainfo JSON (1 seul appel déjà effectué).
                mi_dovi, mi_hdr10plus, mediainfo_responded = self._hdr_flags_from_mi_video(mi_video)
            else:
                mi_dovi, mi_hdr10plus, mediainfo_responded = self._mediainfo_hdr_flags(path)
            has_dovi      = has_dovi      or mi_dovi
            has_hdr10plus = has_hdr10plus or mi_hdr10plus

        # Fallback ffprobe frame-level : uniquement quand mediainfo est absent
        # ou n'a rien retourné. Sinon le coût (240 frames) ne se justifie pas.
        if (not has_dovi or not has_hdr10plus) and not mediainfo_responded:
            frame_flags = self._ffprobe_frame_dynamic_hdr_flags(path)
            if frame_flags is not None:
                frame_dovi, frame_hdr10plus = frame_flags
                has_dovi      = has_dovi      or frame_dovi
                has_hdr10plus = has_hdr10plus or frame_hdr10plus

        # Priorité décroissante
        if has_dovi and has_hdr10plus:
            return HDRType.DOLBY_VISION_HDR10PLUS
        if has_dovi:
            return HDRType.DOLBY_VISION
        if has_hdr10plus:
            return HDRType.HDR10PLUS
        if has_pq and (has_master_disp or has_cll):
            return HDRType.HDR10
        if has_pq:
            # PQ sans métadonnées statiques : HDR10 incomplet mais présent
            return HDRType.HDR10
        if has_hlg:
            return HDRType.HLG

        return HDRType.NONE

    # ------------------------------------------------------------------
    # Appels externes
    # ------------------------------------------------------------------

    def _run_ffprobe(self, path: Path) -> dict[str, Any]:
        """Lance ffprobe et retourne le JSON parsé."""
        cmd = [
            self._ffprobe,
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
            "-show_chapters",
        ]
        cmd.extend(ffprobe_input_args(path))
        self._emit_command(cmd)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                **subprocess_text_kwargs(),
                # shell=True JAMAIS
            )
        except FileNotFoundError:
            self._emit_verbose("ffprobe introuvable dans PATH.")
            raise InspectionError(path, "ffprobe introuvable dans PATH")

        self._emit_process_result("ffprobe", result)

        if result.returncode != 0:
            stderr = result.stderr.strip()[-500:]
            raise InspectionError(path, f"ffprobe a échoué (code {result.returncode}) : {stderr}")

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise InspectionError(path, f"Sortie ffprobe non parseable : {exc}")
        stream_count = len(payload.get("streams") or [])
        chapter_count = len(payload.get("chapters") or [])
        format_name = str((payload.get("format") or {}).get("format_name") or "?")
        self._emit_verbose(
            f"ffprobe JSON parsé : format={format_name} streams={stream_count} chapters={chapter_count}"
        )
        return payload

    def _ffprobe_frame_dynamic_hdr_flags(
        self,
        path: Path,
        *,
        max_frames: int = 240,
    ) -> tuple[bool, bool] | None:
        """
        Retourne (has_dovi, has_hdr10plus) via ffprobe frame-level.

        Utilisé en fallback quand `-show_streams` ne remonte pas les side-data
        dynamiques (cas fréquent sur certains remux DV/HDR10+).
        """
        cmd = [
            self._ffprobe,
            "-v", "quiet",
            "-print_format", "json",
            "-select_streams", "v:0",
            "-read_intervals", f"%+#{max(1, int(max_frames))}",
            "-show_frames",
            "-show_entries", "frame_side_data=side_data_type",
        ]
        cmd.extend(ffprobe_input_args(path))
        self._emit_command(cmd)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                timeout=30,
                **subprocess_text_kwargs(),
            )
        except FileNotFoundError:
            self._emit_verbose("ffprobe introuvable pour le probe HDR frame-level.")
            return None
        self._emit_process_result("ffprobe", result)
        if result.returncode != 0:
            return None
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return None

        frames_obj = payload.get("frames")
        if not isinstance(frames_obj, list):
            return False, False

        has_dovi = False
        has_hdr10plus = False
        for frame in frames_obj:
            if not isinstance(frame, dict):
                continue
            side_data_obj = frame.get("side_data_list")
            if not isinstance(side_data_obj, list):
                continue
            for side_data in side_data_obj:
                if not isinstance(side_data, dict):
                    continue
                side_type = str(side_data.get("side_data_type", "") or "")
                side_type_lower = side_type.lower()
                if ("dolby vision" in side_type_lower) or (side_type == "DOVI configuration record"):
                    has_dovi = True
                if (
                    "hdr dynamic metadata smpte2094-40" in side_type_lower
                    or "hdr10+" in side_type_lower
                    or "smpte st 2094" in side_type_lower
                    or "smpte2094" in side_type_lower
                ):
                    has_hdr10plus = True
                if has_dovi and has_hdr10plus:
                    self._emit_verbose("ffprobe frame HDR : dovi=True hdr10plus=True")
                    return True, True
        self._emit_verbose(f"ffprobe frame HDR : dovi={has_dovi} hdr10plus={has_hdr10plus}")
        return has_dovi, has_hdr10plus

    def _mediainfo_hdr_flags(self, path: Path) -> tuple[bool, bool, bool]:
        """
        Retourne ``(has_dovi, has_hdr10plus, mediainfo_responded)`` via mediainfo.

        ``mediainfo_responded`` est True si mediainfo a renvoyé du texte non vide
        pour ``HDR_Format`` ou ``HDR_Format_Compatibility``. Cela permet à
        l'appelant de savoir si mediainfo a vraiment statué (et donc d'éviter
        un probe frame-level ffprobe coûteux quand la réponse est négative).

        Combine les deux requêtes en un seul appel mediainfo via un séparateur
        ``|`` pour minimiser la latence.
        """
        try:
            # Un seul appel mediainfo : HDR_Format et HDR_Format_Compatibility
            # concaténés avec un séparateur. Selon les sources, HDR10+ peut
            # apparaître dans l'un ou l'autre.
            cmd = [
                self._mediainfo,
                "--Inform=Video;%HDR_Format%|%HDR_Format_Compatibility%",
                str(path),
            ]
            self._emit_command(cmd)
            r = subprocess.run(
                cmd,
                capture_output=True, check=False, **subprocess_text_kwargs(),
            )
            self._emit_process_result("mediainfo", r, preview_stdout=True)
            stdout = (r.stdout or "").strip()
            mediainfo_responded = bool(stdout.replace("|", "").strip())
            hdr_text = stdout.lower()
            has_dovi = "dolby vision" in hdr_text
            has_hdr10plus = (
                "hdr10+" in hdr_text
                or "smpte st 2094" in hdr_text
                or "smpte2094" in hdr_text
            )
            self._emit_verbose(
                f"mediainfo HDR : dovi={has_dovi} hdr10plus={has_hdr10plus} "
                f"responded={mediainfo_responded}"
            )
            return has_dovi, has_hdr10plus, mediainfo_responded

        except FileNotFoundError:
            self._emit_verbose("mediainfo introuvable dans PATH (détection HDR enrichie ignorée).")
            return False, False, False

    # ------------------------------------------------------------------
    # mediainfo JSON (source unifiée pour HDR, frame_count, profil DoVi)
    # ------------------------------------------------------------------

    def _run_mediainfo_json(self, path: Path) -> dict[str, Any] | None:
        """
        Lance ``mediainfo --Output=JSON`` une seule fois et retourne le dict parsé.

        Retourne ``None`` si mediainfo est absent, en échec ou si la sortie
        n'est pas du JSON valide. Cet appel unique remplace les 3 ``--Inform``
        ciblés (FrameCount, HDR_Format, HDR_Format_Compatibility) et expose en
        prime tous les champs HDR riches (mastering display, MaxCLL/FALL,
        compatibility profile DoVi, etc.).
        """
        cmd = [self._mediainfo, "--Output=JSON", str(path)]
        self._emit_command(cmd)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True, check=False, **subprocess_text_kwargs(),
            )
        except FileNotFoundError:
            self._emit_verbose("mediainfo introuvable dans PATH (JSON ignoré).")
            return None
        self._emit_process_result("mediainfo", result)
        if result.returncode != 0:
            return None
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            self._emit_verbose(f"mediainfo JSON invalide : {exc}")
            return None
        if not isinstance(payload, dict):
            self._emit_verbose("mediainfo JSON invalide : racine non objet.")
            return None
        return payload

    @staticmethod
    def _mediainfo_typed_tracks(mi_data: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
        """
        Retourne ``(track, type)`` en supportant le schéma natif mediainfo
        (``media.track[].@type``, ex. "Video") et un schéma alternatif
        observé chez certains wrappers tiers (``streams[].kind``, ex. "VIDEO").
        """
        media = mi_data.get("media")
        if isinstance(media, dict):
            return [
                (track, str(track.get("@type") or ""))
                for track in media.get("track") or []
                if isinstance(track, dict)
            ]
        streams = mi_data.get("streams")
        if isinstance(streams, list):
            return [
                (track, str(track.get("kind") or "").capitalize())
                for track in streams
                if isinstance(track, dict)
            ]
        return []

    @classmethod
    def _mediainfo_video_track(cls, mi_data: dict[str, Any]) -> dict[str, Any] | None:
        """Retourne le 1er track de type Video du JSON mediainfo, ou None."""
        for track, track_type in cls._mediainfo_typed_tracks(mi_data):
            if track_type == "Video":
                return track
        return None

    @classmethod
    def _mediainfo_tracks(cls, mi_data: dict[str, Any], track_type: str) -> list[dict[str, Any]]:
        return [
            track
            for track, t in cls._mediainfo_typed_tracks(mi_data)
            if t == track_type
        ]

    @staticmethod
    def _mediainfo_int_field(track: dict[str, Any], candidates: set[str]) -> int | None:
        for key, value in track.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized_key not in candidates:
                continue
            value_int = _int_or_none_relaxed(value)
            if value_int is not None and value_int > 0:
                return value_int
        return None

    @classmethod
    def _mediainfo_video_bitrate(cls, track: dict[str, Any]) -> int | None:
        return cls._mediainfo_int_field(
            track,
            {
                "bitrate",
                "bitratenominal",
                "nominalbitrate",
                "encodedbitrate",
            },
        )

    @classmethod
    def _mediainfo_subtitle_element_count(cls, track: dict[str, Any]) -> int | None:
        return cls._mediainfo_int_field(
            track,
            {
                "elementcount",
                "countofelements",
                "numberofelements",
                "framecount",
                "numberofframes",
            },
        )

    def _merge_track_counts_from_mediainfo(self, info: FileInfo, mi_data: dict[str, Any]) -> None:
        video_tracks = self._mediainfo_tracks(mi_data, "Video")
        for video, mi_track in zip(info.video_tracks, video_tracks, strict=False):
            bitrate = self._mediainfo_video_bitrate(mi_track)
            if bitrate is not None:
                video.bit_rate = bitrate

        text_tracks = self._mediainfo_tracks(mi_data, "Text")
        for subtitle, mi_track in zip(info.subtitle_tracks, text_tracks, strict=False):
            element_count = self._mediainfo_subtitle_element_count(mi_track)
            if element_count is not None:
                subtitle.element_count = element_count

    @staticmethod
    def _hdr_flags_from_mi_video(mi_video: dict[str, Any]) -> tuple[bool, bool, bool]:
        """
        Retourne ``(has_dovi, has_hdr10plus, mediainfo_responded)`` à partir de
        la track Video mediainfo. Pure : aucun sous-process, idéal pour éviter
        de relancer mediainfo quand on a déjà le JSON complet.
        """
        hdr_format = str(mi_video.get("HDR_Format") or "")
        hdr_compat = str(mi_video.get("HDR_Format_Compatibility") or "")
        hdr_text = f"{hdr_format}\n{hdr_compat}".lower()
        responded = bool(hdr_format.strip() or hdr_compat.strip())
        has_dovi = "dolby vision" in hdr_text
        has_hdr10plus = (
            "hdr10+" in hdr_text
            or "smpte st 2094" in hdr_text
            or "smpte2094" in hdr_text
        )
        return has_dovi, has_hdr10plus, responded

    @staticmethod
    def _merge_dovi_from_mediainfo(
        video: VideoTrack, mi_video: dict[str, Any]
    ) -> None:
        """
        Complète ``video.dovi_profile`` / ``dovi_compat_id`` depuis mediainfo.

        Mappage :
          - ``HDR_Format_Profile`` ``dvheNN`` ou ``dvavNN`` → profile NN.
          - ``HDR_Format_Compatibility`` (côté droit du ``/`` après le profil) :
              "HDR10" → compat_id 1
              "SDR"   → compat_id 2
              "HLG"   → compat_id 4
              sinon   → 0 (P8.0, fallback DoVi-only).

        N'écrase pas une valeur déjà présente côté ffprobe (qui reste plus
        précise quand ``DOVI configuration record`` est exposé).
        """
        if video.dovi_profile is None:
            hfp = str(mi_video.get("HDR_Format_Profile") or "")
            # "dvhe.08 / " → 8 ; "dvav.05 / " → 5
            m = re.search(r"dv(?:he|av)\.?(\d+)", hfp.lower())
            if m:
                video.dovi_profile = int(m.group(1))

        if video.dovi_compat_id is None and video.dovi_profile is not None:
            compat = str(mi_video.get("HDR_Format_Compatibility") or "").lower()
            # Pour les DoVi, la chaîne contient "HDR10" / "SDR" / "HLG"
            # (parfois avec "Profile B" derrière, qu'on ignore).
            if "hdr10" in compat:
                video.dovi_compat_id = 1
            elif "sdr" in compat:
                video.dovi_compat_id = 2
            elif "hlg" in compat:
                video.dovi_compat_id = 4
            else:
                video.dovi_compat_id = 0

    # ------------------------------------------------------------------
    # Parsing ffprobe
    # ------------------------------------------------------------------

    def _parse_ffprobe(self, path: Path, raw: dict[str, Any]) -> FileInfo:
        """Convertit la sortie brute ffprobe en FileInfo structuré."""
        fmt = raw.get("format", {})

        raw_tags = fmt.get("tags", {})
        # Normalise les clés en MAJUSCULES et exclut TITLE (déjà dans .title)
        # ainsi que les balises techniques ENCODER et CREATION_TIME non pertinentes
        # pour la réutilisation dans les fichiers de sortie.
        global_tags: dict[str, str] = {
            k.upper(): str(v)
            for k, v in raw_tags.items()
            if k.upper() not in _EXCLUDED_SOURCE_TAGS and str(v).strip()
        }

        info = FileInfo(
            path        = path,
            format      = fmt.get("format_name", "?"),
            duration_s  = _float_or_none(fmt.get("duration")),
            size_bytes  = _int_or_none(fmt.get("size")),
            bit_rate    = _int_or_none(fmt.get("bit_rate")),
            title       = raw_tags.get("title", "") or raw_tags.get("TITLE", ""),
            global_tags = global_tags,
        )

        att_local_idx = 0
        for stream in raw.get("streams", []):
            codec_type = stream.get("codec_type", "")
            match codec_type:
                case "video":
                    # Les images de couverture (cover art) sont reportées par ffprobe
                    # avec codec_type="video" mais disposition.attached_pic=1.
                    # On les traite comme des attachements.
                    if stream.get("disposition", {}).get("attached_pic", 0):
                        info.attachments.append(
                            self._parse_attachment(stream, att_local_idx)
                        )
                        att_local_idx += 1
                    else:
                        info.video_tracks.append(self._parse_video(stream))
                case "audio":
                    info.audio_tracks.append(self._parse_audio(stream))
                case "subtitle":
                    info.subtitle_tracks.append(self._parse_subtitle(stream))
                case "attachment":
                    info.attachments.append(
                        self._parse_attachment(stream, att_local_idx)
                    )
                    att_local_idx += 1

        chapters = raw.get("chapters", [])
        if chapters:
            entries: list[ChapterEntry] = []
            for c in chapters:
                start_s = _float_or_none(c.get("start_time")) or 0.0
                title   = c.get("tags", {}).get("title", "")
                entries.append(ChapterEntry(timecode_s=start_s, name=title))
            if entries:
                info.chapters = ChapterInfo(entries=entries)

        return info

    def _parse_video(self, s: dict[str, Any]) -> VideoTrack:
        tags = s.get("tags", {})

        # Bit depth depuis pix_fmt (ex. "yuv420p10le" → 10)
        bit_depth: int | None = None
        pix_fmt = s.get("pix_fmt", "")
        if m := re.search(r"(\d+)(?:le|be)?$", pix_fmt):
            bd = int(m.group(1))
            bit_depth = bd if bd in (8, 10, 12, 16) else None

        # Frame rate : avg_frame_rate ou r_frame_rate
        frame_rate = s.get("avg_frame_rate") or s.get("r_frame_rate")
        if frame_rate in ("0/0", "0", None):
            frame_rate = None

        # Profil DoVi depuis le side_data DOVI configuration record
        dovi_profile: int | None = None
        dovi_compat_id: int | None = None
        for sd in s.get("side_data_list") or []:
            if sd.get("side_data_type") == "DOVI configuration record":
                dovi_profile = _int_or_none(sd.get("dv_profile"))
                dovi_compat_id = _int_or_none(sd.get("dv_bl_signal_compatibility_id"))
                break

        return VideoTrack(
            index           = s.get("index", 0),
            codec           = s.get("codec_name", "?"),
            codec_long      = s.get("codec_long_name", ""),
            width           = _int_or_none(s.get("width")),
            height          = _int_or_none(s.get("height")),
            frame_rate      = frame_rate,
            bit_depth       = bit_depth,
            color_space     = s.get("pix_fmt"),
            color_primaries = s.get("color_primaries"),
            color_transfer  = s.get("color_transfer"),
            color_matrix    = s.get("color_space"),
            dovi_profile    = dovi_profile,
            dovi_compat_id  = dovi_compat_id,
            language        = tags.get("language"),
            title           = tags.get("title"),
            duration_s      = _float_or_none(s.get("duration")),
            bit_rate        = _int_or_none(s.get("bit_rate")),
            raw             = s,
        )

    def _parse_audio(self, s: dict[str, Any]) -> AudioTrack:
        tags = s.get("tags", {})
        return AudioTrack(
            index          = s.get("index", 0),
            codec          = s.get("codec_name", "?"),
            codec_long     = s.get("codec_long_name", ""),
            channels       = _int_or_none(s.get("channels")),
            channel_layout = s.get("channel_layout"),
            sample_rate    = _int_or_none(s.get("sample_rate")),
            bit_rate       = _int_or_none(s.get("bit_rate")),
            language       = tags.get("language"),
            title          = tags.get("title"),
            duration_s     = _float_or_none(s.get("duration")),
            raw            = s,
        )

    def _parse_subtitle(self, s: dict[str, Any]) -> SubtitleTrack:
        tags        = s.get("tags", {})
        disposition = s.get("disposition", {})
        return SubtitleTrack(
            index    = s.get("index", 0),
            codec    = s.get("codec_name", "?"),
            language = tags.get("language"),
            title    = tags.get("title"),
            forced   = bool(disposition.get("forced", 0)),
            default  = bool(disposition.get("default", 0)),
            element_count = _subtitle_element_count_from_stream(s),
            raw      = s,
        )

    def _parse_attachment(self, s: dict[str, Any], local_index: int) -> "AttachmentInfo":
        tags = s.get("tags", {})
        return AttachmentInfo(
            index       = s.get("index", 0),
            local_index = local_index,
            filename    = tags.get("filename", "attachment"),
            mimetype    = tags.get("mimetype", "application/octet-stream"),
            size_bytes  = _int_or_none(s.get("size")),
            is_attached_pic = bool(s.get("disposition", {}).get("attached_pic", 0)),
        )

    @staticmethod
    def _stream_tag_lookup(tags: dict[str, Any], normalized_key: str) -> str | None:
        for key, value in tags.items():
            key_norm = str(key).strip().lower().replace("_", "-")
            if key_norm != normalized_key:
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    def _extract_mkv_track_data_from_raw(
        self, raw: dict[str, Any]
    ) -> tuple[int, dict[int, str]]:
        """
        Extrait depuis le ``raw`` ffprobe déjà parsé :
          - le nombre de balises MKV globales (int)
          - un dict ``{track_id: language_ietf|language}`` pour chaque piste

        ``language-ietf`` est prioritaire, ``language`` en fallback.
        Évite un second appel ffprobe (les données sont déjà dans le JSON
        retourné par ``_run_ffprobe``).
        """
        fmt_tags = (raw.get("format") or {}).get("tags") or {}
        tag_count = sum(
            1
            for key, value in fmt_tags.items()
            if str(value).strip() and str(key).strip().upper() not in _EXCLUDED_SOURCE_TAGS
        )

        lang_map: dict[int, str] = {}
        for track in raw.get("streams", []):
            tid = track.get("index")
            tags = track.get("tags", {}) or {}
            lang = self._stream_tag_lookup(tags, "language-ietf")
            if lang is None:
                lang = self._stream_tag_lookup(tags, "language")
            if tid is not None and lang:
                lang_map[tid] = lang
        self._emit_verbose(f"MKV tags depuis raw : tag_count={tag_count} langues={len(lang_map)}")
        return tag_count, lang_map


# =============================================================================
# Helpers
# =============================================================================

def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _int_or_none_relaxed(value: Any) -> int | None:
    direct = _int_or_none(value)
    if direct is not None:
        return direct
    text = str(value or "").strip()
    if not text:
        return None
    compact = (
        text.replace(" ", "")
        .replace("\u00a0", "")
        .replace("\u202f", "")
        .replace("_", "")
    )
    if compact.isdigit():
        return int(compact)
    return None


def _subtitle_element_count_from_stream(stream: dict[str, Any]) -> int | None:
    for key in ("nb_read_frames", "nb_read_packets", "nb_frames"):
        value = _int_or_none_relaxed(stream.get(key))
        if value is not None and value > 0:
            return value

    tags = stream.get("tags") or {}
    if isinstance(tags, dict):
        for key, value in tags.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized_key not in {
                "numberofframes",
                "framecount",
                "elementcount",
                "countofelements",
                "numberofelements",
            }:
                continue
            value_int = _int_or_none_relaxed(value)
            if value_int is not None and value_int > 0:
                return value_int
    return None


# =============================================================================
# Génération XML de chapitres (format Matroska)
# =============================================================================

def fmt_timecode_display(seconds: float) -> str:
    """
    Formate un nombre de secondes en HH:MM:SS.mmm (affichage UI et édition).
    """
    h  = int(seconds // 3600)
    mn = int((seconds % 3600) // 60)
    sc = seconds % 60
    s  = int(sc)
    ms = int(round((sc - s) * 1000))
    return f"{h:02d}:{mn:02d}:{s:02d}.{ms:03d}"


def _fmt_chapter_time(seconds: float) -> str:
    """
    Formate un nombre de secondes en chaîne HH:MM:SS.nnnnnnnnn (format XML Matroska Chapters).
    """
    h  = int(seconds // 3600)
    mn = int((seconds % 3600) // 60)
    sc = seconds % 60
    s  = int(sc)
    ns = int(round((sc - s) * 1_000_000_000))
    return f"{h:02d}:{mn:02d}:{s:02d}.{ns:09d}"


def build_ffmetadata_chapters(entries: "list[ChapterEntry]", global_title: str = "") -> str:
    """
    Génère le contenu d'un fichier ffmetadata compatible avec ``ffmpeg -i metadata.txt``.

    Chaque chapitre est converti en bloc [CHAPTER] avec TIMEBASE=1/1000.
    La fin de chaque chapitre est définie par le début du chapitre suivant,
    ou par start_ms + 1000 ms pour le dernier.
    """
    lines = [";FFMETADATA1"]
    if global_title:
        lines.append(f"title={global_title}")

    sorted_entries = sorted(entries, key=lambda x: x.timecode_s)
    for i, entry in enumerate(sorted_entries):
        start_ms = int(entry.timecode_s * 1000)
        end_ms = int(sorted_entries[i + 1].timecode_s * 1000) if i + 1 < len(sorted_entries) else start_ms + 1000

        lines.append("\n[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={start_ms}")
        lines.append(f"END={end_ms}")
        lines.append(f"title={entry.name or f'Chapter {i + 1}'}")

    return "\n".join(lines)


def build_chapter_xml(entries: "list[ChapterEntry]") -> str:
    """
    Construit un fichier XML Matroska Chapters depuis une liste de ChapterEntry.

    Le format suit le schéma XML Matroska Chapters.
    Les chapitres sont triés par timecode croissant.
    """
    from xml.sax.saxutils import escape as _xe
    atoms: list[str] = []
    for e in sorted(entries, key=lambda x: x.timecode_s):
        tc   = _fmt_chapter_time(e.timecode_s)
        name = _xe(e.name) if e.name else ""
        atoms.append(
            "    <ChapterAtom>\n"
            f"      <ChapterTimeStart>{tc}</ChapterTimeStart>\n"
            "      <ChapterDisplay>\n"
            f"        <ChapterString>{name}</ChapterString>\n"
            "        <ChapterLanguage>und</ChapterLanguage>\n"
            "      </ChapterDisplay>\n"
            "    </ChapterAtom>"
        )
    body = "\n".join(atoms)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE Chapters SYSTEM "matroskachapters.dtd">\n'
        "<Chapters>\n"
        "  <EditionEntry>\n"
        "    <EditionFlagHidden>0</EditionFlagHidden>\n"
        "    <EditionFlagDefault>1</EditionFlagDefault>\n"
        f"{body}\n"
        "  </EditionEntry>\n"
        "</Chapters>\n"
    )
