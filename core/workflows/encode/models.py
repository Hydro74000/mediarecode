"""
core/workflows/encode/models.py — Data models, enums and constants for encoding.

Public:
    QualityMode
    SOFTWARE_VIDEO_CODECS, HARDWARE_VIDEO_CODECS, AUDIO_CODECS
    X265_PRESETS, X264_PRESETS, SVTAV1_PRESETS, NVENC_PRESETS
    TONEMAP_ALGORITHMS
    presets_for_codec()
    VideoEncodeSettings, AudioTrackSettings, EncodeConfig, EncodePreset
    EncodeError
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

from core.workflows.encode.catalog import (
    AMF_PRESETS,
    AUDIO_CODECS,
    HARDWARE_VIDEO_CODECS,
    NVENC_PRESETS,
    QSV_PRESETS,
    SOFTWARE_VIDEO_CODECS,
    SVTAV1_PRESETS,
    TONEMAP_ALGORITHMS,
    VAAPI_PRESETS,
    X264_PRESETS,
    X265_PRESETS,
    presets_for_codec,
)
from core.workflows.common.track_types import TrackMetaEdit, TrackMetaPatch, TrackTimeOffset, TrackOffset


class ChapterEntryLike(Protocol):
    timecode_s: float
    name: str


SubtitleTrackRef = tuple[Path, int]
AttachmentStreamRef = tuple[Path, int]


# =============================================================================
# Enums et constantes
# =============================================================================

class QualityMode(str, Enum):
    CRF     = "crf"
    CQ      = "cq"
    BITRATE = "bitrate"
    SIZE    = "size"

    def label(self) -> str:
        return {
            "crf": "CRF",
            "cq": "CQ (qualité HW)",
            "bitrate": "Débit (kbps)",
            "size": "Taille cible (Mo)",
        }[self.value]


class EncodePreviewMode(str, Enum):
    IMAGE = "image"
    VIDEO = "video"


PREVIEW_IMAGE_CAPTURE_COUNT = 7
PREVIEW_VIDEO_THUMBNAIL_COUNT = 5
PREVIEW_FRAME_MIN_OFFSET_S = 1.0
PREVIEW_FRAME_TAIL_OFFSET_S = 5.0

AC3_STANDARD_BITRATES_KBPS: list[int] = [
    32, 40, 48, 56, 64, 80, 96, 112,
    128, 160, 192, 224, 256, 320, 384, 448, 512, 576, 640,
]
AUDIO_BITRATES_PER_CHANNEL_KBPS: list[int] = AC3_STANDARD_BITRATES_KBPS.copy()
DEFAULT_AUDIO_KBPS_PER_CHANNEL = 96
AAC_MAX_BITS_PER_CHANNEL_FRAME = 6144
AAC_FRAME_SAMPLES = 1024
EAC3_MAX_BITRATE_KBPS = 6144
EAC3_MAX_KBPS_PER_CHANNEL = EAC3_MAX_BITRATE_KBPS // 6
_DEFAULT_AUDIO_SAMPLE_RATE_HZ = 48_000


def audio_output_channel_count(codec: str, channels: int | None, channel_layout: str | None = None) -> int:
    """Nombre de canaux de sortie utilisé pour les calculs de débit."""
    count = channels if channels and channels > 0 else 0
    layout = (channel_layout or "").lower()
    if count <= 0:
        if "7.1" in layout:
            count = 8
        elif "5.1" in layout:
            count = 6
        elif "stereo" in layout:
            count = 2
        elif "mono" in layout:
            count = 1
        else:
            count = 2
    if codec in {"ac3", "eac3"} and count > 6:
        return 6
    return count


def audio_codec_max_bitrate_kbps(
    codec: str,
    channels: int | None = None,
    sample_rate: int | None = None,
    channel_layout: str | None = None,
) -> int:
    """Plafond par piste après calcul par canal quand le codec l'exige."""
    output_channels = audio_output_channel_count(codec, channels, channel_layout)
    if codec == "ac3":
        return AC3_STANDARD_BITRATES_KBPS[-1]
    if codec == "eac3":
        return min(EAC3_MAX_BITRATE_KBPS, EAC3_MAX_KBPS_PER_CHANNEL * output_channels)
    if codec == "aac":
        rate = sample_rate if sample_rate and sample_rate > 0 else _DEFAULT_AUDIO_SAMPLE_RATE_HZ
        per_channel = int(rate * AAC_MAX_BITS_PER_CHANNEL_FRAME / AAC_FRAME_SAMPLES / 1000)
        return max(1, per_channel * output_channels)
    return max(1, output_channels)


def audio_bitrate_choices_kbps(
    codec: str,
    channels: int | None = None,
    sample_rate: int | None = None,
    channel_layout: str | None = None,
) -> list[int]:
    """Liste de débits proposée par piste pour un codec et un nombre de canaux."""
    if codec == "ac3":
        return AC3_STANDARD_BITRATES_KBPS.copy()

    output_channels = audio_output_channel_count(codec, channels, channel_layout)
    maximum = audio_codec_max_bitrate_kbps(codec, channels, sample_rate, channel_layout)
    choices = [
        per_channel * output_channels
        for per_channel in AUDIO_BITRATES_PER_CHANNEL_KBPS
        if per_channel * output_channels <= maximum
    ]
    if not choices:
        choices = [maximum]
    elif choices[-1] != maximum:
        choices.append(maximum)
    return sorted(set(choices))


def default_audio_bitrate_kbps(
    codec: str,
    channels: int | None = None,
    sample_rate: int | None = None,
    channel_layout: str | None = None,
    kbps_per_channel: int | None = None,
) -> int:
    """Débit par défaut par canal (AAC/EAC-3 : configurable, autres : 96 kbps), arrondi au choix valide."""
    output_channels = audio_output_channel_count(codec, channels, channel_layout)
    per_channel = kbps_per_channel if kbps_per_channel is not None else DEFAULT_AUDIO_KBPS_PER_CHANNEL
    target = per_channel * output_channels
    return normalize_audio_bitrate_kbps(codec, target, channels, sample_rate, channel_layout)


def normalize_audio_bitrate_kbps(
    codec: str,
    bitrate_kbps: int | None,
    channels: int | None = None,
    sample_rate: int | None = None,
    channel_layout: str | None = None,
) -> int:
    """Normalise un débit pour éviter les valeurs impossibles envoyées à FFmpeg."""
    try:
        bitrate = int(bitrate_kbps or 0)
    except (TypeError, ValueError):
        bitrate = 0
    if codec == "ac3":
        if bitrate <= AC3_STANDARD_BITRATES_KBPS[0]:
            return AC3_STANDARD_BITRATES_KBPS[0]
        if bitrate >= AC3_STANDARD_BITRATES_KBPS[-1]:
            return AC3_STANDARD_BITRATES_KBPS[-1]
        return min(AC3_STANDARD_BITRATES_KBPS, key=lambda choice: abs(choice - bitrate))
    choices = audio_bitrate_choices_kbps(codec, channels, sample_rate, channel_layout)
    if bitrate <= choices[0]:
        return choices[0]
    if bitrate >= choices[-1]:
        return choices[-1]
    return min(choices, key=lambda choice: abs(choice - bitrate))

# =============================================================================
# Dataclasses
# =============================================================================

def _dataclass_from_value(cls, value):
    if isinstance(value, cls):
        return value
    if isinstance(value, dict):
        allowed = cls.__dataclass_fields__
        return cls(**{k: v for k, v in value.items() if k in allowed})
    return cls()


@dataclass
class VideoResizeSettings:
    """Réglages de redimensionnement vidéo."""
    enabled: bool = False
    mode: str = "preset"          # preset | percent | size
    preset: str = "720p"
    percent: int = 100
    width: int = 1280
    height: int = 720
    keep_aspect: bool = True
    allow_upscale: bool = False
    algorithm: str = "lanczos"

    def is_active(self) -> bool:
        return bool(self.enabled)

    @classmethod
    def from_value(cls, value: object) -> "VideoResizeSettings":
        return _dataclass_from_value(cls, value)


@dataclass
class VideoCropSettings:
    """Réglages de recadrage vidéo."""
    enabled: bool = False
    unit: str = "px"              # px | percent
    top: int = 0
    bottom: int = 0
    left: int = 0
    right: int = 0
    auto: bool = False

    def is_active(self) -> bool:
        return bool(self.enabled and (self.auto or any((self.top, self.bottom, self.left, self.right))))

    @classmethod
    def from_value(cls, value: object) -> "VideoCropSettings":
        return _dataclass_from_value(cls, value)


@dataclass
class VideoFilterSettings:
    """Filtres vidéo typés appliqués avant encodage."""
    yadif_enabled: bool = False
    yadif_mode: str = "send_frame"
    yadif_parity: str = "auto"
    yadif_deint: str = "all"
    deblock_enabled: bool = False
    deblock_strength: str = "medium"
    deblock_block: int = 8
    nlmeans_enabled: bool = False
    nlmeans_strength: str = "light"
    nlmeans_profile: str = "standard"
    chroma_smooth_enabled: bool = False
    chroma_smooth_strength: str = "medium"

    def is_active(self) -> bool:
        return bool(
            self.yadif_enabled
            or self.deblock_enabled
            or self.nlmeans_enabled
            or self.chroma_smooth_enabled
        )

    @classmethod
    def from_value(cls, value: object) -> "VideoFilterSettings":
        return _dataclass_from_value(cls, value)


@dataclass
class VideoEncodeSettings:
    """Paramètres d'encodage vidéo."""
    stream_index:     int          = 0      # index global ffprobe de la piste vidéo source
    source_path:      Path | None  = None   # None = même fichier que EncodeConfig.source
    track_entry_id:   str | None   = None   # GUID TrackEntry synchronisé avec RemuxPanel
    codec:            str          = "libx265"
    quality_mode:     QualityMode  = QualityMode.CRF
    crf:              int          = 18
    cq:               int          = 26   # Quality target pour mode CQ (HW only)
    bitrate_kbps:     int          = 5000
    target_size_mb:   int          = 4000
    preset:           str          = "slow"
    extra_params:     str          = ""    # x265-params / svtav1-params passthrough
    # Précheck UI: forcer une sortie 8-bit pour les encodeurs H.264
    # quand la source est > 8-bit (appliqué piste par piste).
    force_8bit:       bool         = False
    # Sortie 10-bit explicite (profile main10/high10 + pix_fmt p010le/yuv420p10le).
    # Mutuellement exclusif avec force_8bit (qui prend priorité).
    force_10bit:      bool         = False
    # Transformations vidéo
    resize:           VideoResizeSettings = field(default_factory=VideoResizeSettings)
    crop:             VideoCropSettings = field(default_factory=VideoCropSettings)
    filters:          VideoFilterSettings = field(default_factory=VideoFilterSettings)
    # HDR statique
    inject_hdr_meta:  bool         = False
    master_display:   str          = ""   # ex. "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(40000000,50)"
    max_cll:          str          = ""   # ex. "1000,400"
    static_hdr_metadata_source: str = ""
    static_hdr_metadata_confidence: str = ""
    static_hdr_metadata_analysis_mode: str = ""
    static_hdr_metadata_analysis_request: str = ""
    # HDR dynamique
    copy_dv:          bool         = False
    copy_hdr10plus:   bool         = False
    dovi_profile:     str          = "0"
    # Transformation interne P5 IPT -> base layer HDR10 BT.2020/PQ via
    # libplacebo. Activée par le workflow, jamais directement par le panel.
    p5_to_hdr10:      bool         = False
    # Normalisation expérimentale du bitstream HEVC après injection
    # HDR dynamique : retire les SEI pic_timing pour rapprocher la
    # structure SEI des encodes fonctionnels observés.
    # Le code existe toujours mais le hook workflow est actuellement désactivé.
    strip_pic_timing_sei: bool     = False
    # Tone mapping
    tonemap_to_sdr:   bool         = False
    tonemap_algorithm: str         = "hable"

    def __post_init__(self) -> None:
        self.resize = VideoResizeSettings.from_value(self.resize)
        self.crop = VideoCropSettings.from_value(self.crop)
        self.filters = VideoFilterSettings.from_value(self.filters)

    def has_video_transform(self) -> bool:
        return bool(
            self.resize.is_active()
            or self.crop.is_active()
            or self.filters.is_active()
            or self.tonemap_to_sdr
            or self.p5_to_hdr10
        )


@dataclass
class AudioTrackSettings:
    """Paramètres d'encodage pour une piste audio."""
    stream_index:        int         # index global ffprobe dans le fichier source
    codec:               str = "copy"
    bitrate_kbps:        int = 384
    extract_truehd_core: bool = False   # strip Atmos via BSF truehd_core
    input_channels:      int | None = None   # nb de canaux de la piste source (ffprobe)
    input_channel_layout: str | None = None  # layout source (ex: "7.1", "5.1(side)")
    source_path:         Path | None = None  # None = même fichier que la vidéo (config.source)
    track_entry_id:      str | None = None   # GUID de l'objet TrackEntry synchronisé entre panels


@dataclass(frozen=True)
class EncodePreviewRequest:
    """Demande de génération de preview réelle depuis l'UI."""
    mode: str = EncodePreviewMode.IMAGE.value
    timecode_s: float = 0.0
    duration_s: float = 5.0
    random_scene: bool = False

    def normalized_mode(self) -> EncodePreviewMode:
        try:
            return EncodePreviewMode(str(self.mode).strip().lower())
        except ValueError:
            return EncodePreviewMode.IMAGE

    def normalized_duration_s(self) -> float:
        if self.normalized_mode() == EncodePreviewMode.VIDEO:
            return max(5.0, min(30.0, float(self.duration_s or 5.0)))
        return max(0.5, min(5.0, float(self.duration_s or 2.0)))


@dataclass(frozen=True)
class EncodePreviewCapture:
    """Une capture image (chemin + scène + label)."""
    image_path: Path
    scene_time_s: float
    label: str = ""


@dataclass(frozen=True)
class EncodePreviewResult:
    """Résultat sérialisable émis par TaskSignals.finished."""
    mode: str
    captures: tuple[EncodePreviewCapture, ...] = ()
    video_path: Path | None = None
    warning: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "mode": self.mode,
                "captures": [
                    {
                        "image_path": str(c.image_path),
                        "scene_time_s": c.scene_time_s,
                        "label": c.label,
                    }
                    for c in self.captures
                ],
                "video_path": str(self.video_path) if self.video_path is not None else None,
                "warning": self.warning,
            },
            ensure_ascii=False,
        )


@dataclass(frozen=True)
class VideoTrackEncodePlan:
    """Résumé UI d'un plan d'encodage vidéo pour une piste remux."""
    track_entry_id: str
    codec_summary: str
    target_codec: str = "copy"
    hdr_badges: tuple[str, ...] = ()
    filter_badges: tuple[str, ...] = ()
    is_modified: bool = False


@dataclass
class EncodeConfig:
    """Configuration complète d'un encodage."""
    source:           Path
    output:           Path
    video:            VideoEncodeSettings | None = None
    video_tracks:     list[VideoEncodeSettings] = field(default_factory=list)
    audio_tracks:     list[AudioTrackSettings] = field(default_factory=list)
    copy_subtitles:   bool         = True
    # Pistes de sous-titres multi-sources : (chemin_source, stream_index_ffprobe)
    # Si non vide, remplace le copy_subtitles générique.
    subtitle_tracks:  list[SubtitleTrackRef] = field(default_factory=list)
    keep_chapters:    bool         = True
    #: Chapitres personnalisés à appliquer en post-traitement FFmpeg.
    #: None  → comportement keep_chapters (copie depuis la source ou rien).
    #: list  → écrase les chapitres existants avec ces entrées.
    chapter_overrides: list[ChapterEntryLike] | None = None
    # Flux d'attachements à copier : (chemin_source, stream_index_ffprobe)
    # Sélection individuelle — remplace l'ancien attachment_sources global.
    attachment_streams: list[AttachmentStreamRef] = field(default_factory=list)
    # Fichiers externes à attacher (ajout manuel, via -attach ffmpeg).
    extra_attachments:  list[Path] = field(default_factory=list)
    # Sources dont on copie les tags globaux via post-traitement FFmpeg.
    tag_sources:      list[Path] = field(default_factory=list)
    #: Balises MKV globales à écrire directement (prioritaire sur tag_sources).
    #: None  → utiliser tag_sources si présents.
    #: dict  → écrire ces balises et ignorer tag_sources.
    #: {}    → supprimer toutes les balises existantes.
    tag_overrides:    dict | None = None                    # dict[str, str] | None
    # Éditions de métadonnées de pistes (langue, titre) appliquées via FFmpeg.
    track_meta_edits: list[TrackMetaEdit] = field(default_factory=list)
    # Décalages temporels par piste (ms), appliqués directement au runtime encode.
    track_time_offsets: list[TrackTimeOffset] = field(default_factory=list)
    file_title:       str          = ""     # balise Title du segment de sortie
    duration_s:       float | None = None   # requis pour le mode taille cible
    # Passthrough métadonnées dynamiques (HEVC uniquement)
    copy_dv:          bool         = False  # compat legacy : miroir de la vidéo primaire
    copy_hdr10plus:   bool         = False  # compat legacy : miroir de la vidéo primaire
    dovi_profile:     str          = "0"    # compat legacy : miroir de la vidéo primaire
    work_dir:         Path | None  = None   # dossier de travail (passlog, fichiers temp)
    #: Cover TMDB à télécharger juste avant l'encodage : (url, filename).
    #: None → pas de cover TMDB en attente.
    tmdb_cover:       tuple[str, str] | None = None
    #: Autorise une preview CLI à construire la commande même si le dossier de
    #: sortie n'existe pas encore. Ne doit pas être utilisé pour une exécution.
    allow_missing_output_dir: bool = False

    def __post_init__(self) -> None:
        if not self.video_tracks and self.video is not None:
            primary = self.video
            if not primary.copy_dv and self.copy_dv:
                primary.copy_dv = self.copy_dv
            if not primary.copy_hdr10plus and self.copy_hdr10plus:
                primary.copy_hdr10plus = self.copy_hdr10plus
            if primary.dovi_profile == "0" and self.dovi_profile != "0":
                primary.dovi_profile = self.dovi_profile
            self.video_tracks = [primary]
        elif self.video_tracks and self.video is None:
            self.video = self.video_tracks[0]

        if self.video is None:
            raise ValueError("EncodeConfig nécessite au moins une piste vidéo.")

        # La première piste reste l'accesseur de compatibilité pour l'ancien code.
        self.video = self.video_tracks[0]
        self.copy_dv = bool(self.video.copy_dv)
        self.copy_hdr10plus = bool(self.video.copy_hdr10plus)
        self.dovi_profile = str(self.video.dovi_profile or "0")


@dataclass
class EncodePreset:
    """Profil d'encodage sauvegardable en JSON."""
    name:                       str  = "Nouveau profil"
    description:                str  = ""
    codec:                      str  = "libx265"
    quality_mode:               str  = QualityMode.CRF.value
    crf:                        int  = 18
    cq:                         int  = 26
    bitrate_kbps:               int  = 5000
    target_size_mb:             int  = 4000
    preset:                     str  = "slow"
    extra_params:               str  = ""
    force_10bit:                bool = False
    resize:                     VideoResizeSettings = field(default_factory=VideoResizeSettings)
    crop:                       VideoCropSettings = field(default_factory=VideoCropSettings)
    filters:                    VideoFilterSettings = field(default_factory=VideoFilterSettings)
    inject_hdr_meta:            bool = False
    master_display:             str  = ""
    max_cll:                    str  = ""
    tonemap_to_sdr:             bool = False
    tonemap_algorithm:          str  = "hable"
    default_audio_codec:        str  = "copy"
    default_audio_bitrate_kbps: int  = 384

    def __post_init__(self) -> None:
        self.resize = VideoResizeSettings.from_value(self.resize)
        self.crop = VideoCropSettings.from_value(self.crop)
        self.filters = VideoFilterSettings.from_value(self.filters)

    def to_video_settings(self) -> VideoEncodeSettings:
        return VideoEncodeSettings(
            codec=self.codec,
            quality_mode=QualityMode(self.quality_mode),
            crf=self.crf,
            cq=self.cq,
            bitrate_kbps=self.bitrate_kbps,
            target_size_mb=self.target_size_mb,
            preset=self.preset,
            extra_params=self.extra_params,
            force_10bit=self.force_10bit,
            resize=self.resize,
            crop=self.crop,
            filters=self.filters,
            inject_hdr_meta=self.inject_hdr_meta,
            master_display=self.master_display,
            max_cll=self.max_cll,
            tonemap_to_sdr=self.tonemap_to_sdr,
            tonemap_algorithm=self.tonemap_algorithm,
        )

    def to_json_dict(self) -> dict:
        return asdict(self)


# =============================================================================
# Exception
# =============================================================================

class EncodeError(RuntimeError):
    """Erreur levée lors de la validation ou de l'exécution d'un encodage."""
