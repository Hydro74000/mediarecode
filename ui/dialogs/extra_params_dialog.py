"""
ui/dialogs/extra_params_dialog.py — Modale d'édition des params avancés.

Composant **réutilisable** (pas spécifique au panneau d'encodage). Tout panneau
qui possède un champ "extra params" pour un codec ffmpeg donné peut l'ouvrir
via ``edit_extra_params(codec, current, parent)``.

Public:
    ExtraParamsDialog
    edit_extra_params  (helper one-shot)

Conception :
    - Catalogue déclaratif par codec : groupes = onglets verticaux, chaque param
      a un type (int_range / float_range / enum / bool_flag / free_text).
    - Pour chaque param, une checkbox d'activation : seuls les params cochés
      sont sérialisés en sortie (les autres = défaut ffmpeg, non émis).
    - Sérialisation contextuelle :
        * libx265 / libsvtav1  → "key=val:key=val"  (consommé via -x265-params/-svtav1-params)
        * libx264              → flags ffmpeg "-flag value"  (passthrough -x264-params si besoin)
        * NVENC / AMF / QSV    → flags ffmpeg "-flag value"
    - L'init parse la valeur existante du champ pour pré-cocher les params connus.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QScrollArea, QSpinBox, QStyle, QStyleOptionTab, QStylePainter,
    QTabBar, QTabWidget, QToolTip, QVBoxLayout, QWidget,
)

from core.workflows.encode.backends import backend_id_for_codec
from core.i18n import apply_translations, translate_text
from ui.design_system import colors as _C
from ui.styles import (
    _checkbox_style, _combo_style, _input_style,
    _primary_button, _secondary_button,
)


# =============================================================================
# Schéma déclaratif des paramètres
# =============================================================================

@dataclass(frozen=True)
class ParamSpec:
    """Spécification d'un paramètre avancé.

    key       : nom du flag ffmpeg (ex: "spatial-aq", "rc-lookahead") OU clé
                key=val pour x265-params (ex: "aq-mode", "psy-rd").
    label     : label affiché.
    kind      : "int" | "float" | "enum" | "bool" | "text"
    default   : valeur par défaut affichée (pré-remplie quand on coche).
    options   : pour kind="enum", liste de tuples (value, label).
    minimum/maximum/step : bornes pour int/float.
    suffix    : suffixe affiché (ex: " ms", " kbps").
    tooltip   : aide contextuelle.
    bool_repr : pour kind="bool", paire (off_value, on_value) à émettre. Par
                défaut ("0","1"). Mettre (None,"") pour les flags x265-params
                booléens (la clé seule suffit).
    """
    key: str
    label: str
    kind: str
    default: Any = None
    options: tuple[tuple[str, str], ...] = ()
    minimum: float | int | None = None
    maximum: float | int | None = None
    step: float | int | None = None
    suffix: str = ""
    tooltip: str = ""
    bool_repr: tuple[str | None, str | None] = ("0", "1")


@dataclass(frozen=True)
class ParamGroup:
    """Onglet vertical regroupant plusieurs ParamSpec par scope."""
    title: str
    params: tuple[ParamSpec, ...]


@dataclass(frozen=True)
class CodecSchema:
    """Schéma complet pour un codec : style de sérialisation + groupes."""
    codec: str
    style: str   # "ffmpeg_flags" (NVENC/AMF/QSV/libx264) | "x265_params" | "svtav1_params"
    groups: tuple[ParamGroup, ...]


_NVENCC_IGNORED_PARSE_KEYS: frozenset[str] = frozenset({
    "master-display",
    "max-cll",
    "dhdr10-info",
    "dolby-vision-profile",
    "dolby-vision-rpu",
    "colormatrix",
    "colorprim",
    "transfer",
    "chromaloc",
    "vpp-colorspace",
    "vpp-libplacebo-tonemapping",
    "vpp-libplacebo-tonemapping-lut",
})

_NVENCC_QP_TRIPLET_KEYS: frozenset[str] = frozenset({
    "cqp",
    "qp-init",
    "qp-min",
    "qp-max",
})

_NVENCC_SPLIT_ENC = (
    ("disable", "disable"),
    ("auto", "auto"),
    ("auto_forced", "auto_forced"),
    ("forced_2", "forced_2"),
    ("forced_3", "forced_3"),
    ("forced_4", "forced_4"),
)

def _normalize_nvencc_text_value(key: str, raw: Any) -> str | None:
    """Canonise certains champs texte NVEncC saisis via le dialog."""
    text = str(raw).strip()
    if not text:
        return None
    if key in _NVENCC_QP_TRIPLET_KEYS and text.isdigit():
        return f"{text}:{text}:{text}"
    if key == "parallel" and text.lower() == "all":
        return "auto"
    return text


# -------- NVENC (HEVC) -------------------------------------------------------

_NVENC_PRESETS = tuple((p, p) for p in ("p1", "p2", "p3", "p4", "p5", "p6", "p7"))
_NVENC_TUNES = (("hq", "hq — high quality"), ("ll", "ll — low latency"),
                ("ull", "ull — ultra low latency"), ("lossless", "lossless"))
_NVENC_RC = (("vbr", "vbr"), ("cbr", "cbr"), ("constqp", "constqp"))
_NVENC_MULTIPASS = (("disabled", "disabled"), ("qres", "qres — quart res"),
                    ("fullres", "fullres — full res"))
_NVENC_BREF = (("disabled", "disabled"), ("each", "each"), ("middle", "middle"))
_NVENC_LEVEL_HEVC = tuple((v, v) for v in ("auto", "1", "2", "2.1", "3", "3.1",
                                            "4", "4.1", "5", "5.1", "5.2", "6", "6.1", "6.2"))
_NVENC_PROFILE_HEVC = (("main", "main"), ("main10", "main10 (10-bit)"), ("rext", "rext"))
_NVENC_TIER = (("main", "main"), ("high", "high"))

_NVENC_HEVC = CodecSchema(
    codec="hevc_nvenc",
    style="ffmpeg_flags",
    groups=(
        ParamGroup("Tune", (
            ParamSpec("tune", "Tune", "enum", default="hq", options=_NVENC_TUNES,
                      tooltip="Profil d'usage du moteur NVENC.\n"
                              "• hq : qualité maximale (encodage offline) — recommandé pour le remux.\n"
                              "• ll / ull : faible latence pour le streaming, qualité moindre.\n"
                              "• lossless : sans perte (taille de fichier énorme)."),
            ParamSpec("multipass", "Multipass", "enum", default="fullres", options=_NVENC_MULTIPASS,
                      tooltip="Encodage multi-passes pour mieux répartir le débit.\n"
                              "• disabled : 1 passe (plus rapide).\n"
                              "• qres : 1ʳᵉ passe en quart de résolution (compromis).\n"
                              "• fullres : 1ʳᵉ passe pleine résolution — qualité maximale, ~30% plus lent.\n"
                              "Requiert Turing (RTX 20xx) ou plus récent."),
        )),
        ParamGroup("Rate control", (
            ParamSpec("qmin", "QP min", "int", default=0, minimum=0, maximum=51,
                      tooltip="Valeur QP minimale autorisée par l'encodeur.\n"
                              "Plafonne la qualité maximale d'une frame (utile pour limiter le sur-coût "
                              "bitrate sur des scènes simples). Laisser 0 = pas de limite basse."),
            ParamSpec("qmax", "QP max", "int", default=51, minimum=0, maximum=51,
                      tooltip="Valeur QP maximale autorisée. Plus le QP est haut, plus la qualité chute.\n"
                              "Réduire à ~35-40 pour garantir un seuil de qualité minimal sur les "
                              "scènes complexes."),
            ParamSpec("rc-lookahead", "Lookahead (frames)", "int", default=32, minimum=0, maximum=64,
                      tooltip="Nombre de frames analysées en avance pour optimiser le placement "
                              "des B-frames et la décision de QP.\n"
                              "32 = bon compromis. 0 désactive (perte de qualité notable)."),
            ParamSpec("maxrate", "Max bitrate (kbps)", "int", default=80000, minimum=0, maximum=400000,
                      suffix=" kbps",
                      tooltip="Plafond instantané du débit (mode VBR uniquement).\n"
                              "Doit être ≥ bitrate cible. Typiquement 1.5× à 2× le bitrate moyen "
                              "pour absorber les pics de complexité."),
            ParamSpec("bufsize", "Buffer size (kbps)", "int", default=160000, minimum=0, maximum=800000,
                      suffix=" kbps",
                      tooltip="Taille du buffer VBV (Video Buffering Verifier).\n"
                              "Typiquement 2× le maxrate. Plus grand = plus de souplesse pour les pics, "
                              "mais latence de décodage accrue."),
        )),
        ParamGroup("Adaptive Quant.", (
            ParamSpec("spatial-aq", "Spatial AQ", "bool", default="1",
                      tooltip="Adaptive Quantization spatial : alloue plus de bits aux zones lisses "
                              "(ciels, peau) où la compression est visible, moins aux zones chargées.\n"
                              "Améliore notablement la qualité perçue. Recommandé toujours actif."),
            ParamSpec("aq-strength", "AQ strength", "int", default=8, minimum=1, maximum=15,
                      tooltip="Intensité de l'AQ spatial (1-15). Plus la valeur est haute, plus la "
                              "redistribution de bits est agressive.\n"
                              "8 = défaut équilibré. 12-15 = peut introduire du flou sur les détails fins."),
            ParamSpec("temporal-aq", "Temporal AQ", "bool", default="1",
                      tooltip="AQ temporel : alloue plus de bits aux frames qui servent de référence "
                              "aux frames suivantes (propage la qualité dans le temps).\n"
                              "Requiert Turing (RTX 20xx) ou plus récent. Gain qualité ~5-10%."),
            ParamSpec("nonref_p", "Non-ref P-frames", "bool",
                      tooltip="Marque les P-frames comme non-référencées : économise du bitrate au prix "
                              "d'une légère perte de qualité.\n"
                              "Utile pour le streaming bas débit, déconseillé pour l'archivage."),
        )),
        ParamGroup("GOP / B-frames", (
            ParamSpec("g", "GOP size", "int", default=250, minimum=1, maximum=600,
                      tooltip="Distance maximale entre deux keyframes (I-frames).\n"
                              "Typique : 10× le framerate (250 pour 25 fps, 240 pour 24p).\n"
                              "Plus court = meilleur seek, plus long = compression accrue."),
            ParamSpec("bf", "B-frames", "int", default=4, minimum=0, maximum=8,
                      tooltip="Nombre maximum de B-frames consécutives entre deux frames de référence.\n"
                              "4 = très bon pour HEVC. 0 désactive complètement (qualité réduite)."),
            ParamSpec("b_ref_mode", "B-ref mode", "enum", default="middle", options=_NVENC_BREF,
                      tooltip="Permet aux B-frames de servir de référence à d'autres frames.\n"
                              "• disabled : B-frames non-référencées (HEVC standard).\n"
                              "• each : chaque B peut être référence.\n"
                              "• middle : seule la B du milieu est référence — meilleur compromis qualité.\n"
                              "Requiert Turing (RTX 20xx) ou plus récent."),
            ParamSpec("refs", "Reference frames", "int", default=4, minimum=1, maximum=8,
                      tooltip="Nombre de frames de référence pour la prédiction inter-image.\n"
                              "Plus = meilleure qualité sur scènes complexes, mais encodage plus lent."),
            ParamSpec("strict_gop", "Strict GOP", "bool",
                      tooltip="Force une structure GOP strictement régulière (keyframes à intervalles fixes).\n"
                              "Désactive la détection de scenecut. Utile pour ABR streaming, déconseillé sinon."),
            ParamSpec("no-scenecut", "Désactiver scenecut", "bool", bool_repr=("0", "1"),
                      tooltip="Empêche l'encodeur d'insérer une keyframe sur changement de scène.\n"
                              "Active = GOP régulier, désactive = keyframes adaptatives (qualité ↑)."),
        )),
        ParamGroup("Profil / Level", (
            ParamSpec("profile", "Profile", "enum", default="main10", options=_NVENC_PROFILE_HEVC,
                      tooltip="Profil HEVC :\n"
                              "• main : 8-bit 4:2:0 — compatibilité universelle.\n"
                              "• main10 : 10-bit 4:2:0 — obligatoire pour HDR/DoVi.\n"
                              "• rext : range extensions (4:4:4, 12-bit) — usages pro."),
            ParamSpec("level", "Level", "enum", default="auto", options=_NVENC_LEVEL_HEVC,
                      tooltip="Level HEVC : limite de bitrate/résolution/framerate.\n"
                              "• auto : déduit automatiquement du contenu.\n"
                              "• 5.1 : UHD 4K 60p (Blu-ray UHD).\n"
                              "• 6.1 / 6.2 : 8K, framerates extrêmes."),
            ParamSpec("tier", "Tier", "enum", default="high", options=_NVENC_TIER,
                      tooltip="• main : bitrate plafonné selon le level (consumer).\n"
                              "• high : bitrate étendu — requis pour UHD HDR haut bitrate.\n"
                              "Combiner avec level 5.1 ou 6.x pour les UHD remux."),
        )),
        ParamGroup("Avancé / libre", (
            ParamSpec("weighted_pred", "Weighted prediction", "bool",
                      tooltip="Active la prédiction pondérée — améliore la compression sur les "
                              "scènes avec fondus, transitions de luminosité.\n"
                              "Requiert Pascal (GTX 10xx) ou plus récent. Désactive certaines combinaisons "
                              "B-frames sur GPUs anciens."),
            ParamSpec("init_qpI", "Init QP I", "int", default=20, minimum=0, maximum=51,
                      tooltip="QP initial des I-frames (mode constqp uniquement).\n"
                              "Référence de qualité absolue. Typique : 18-22 pour qualité visuelle élevée."),
            ParamSpec("init_qpP", "Init QP P", "int", default=23, minimum=0, maximum=51,
                      tooltip="QP initial des P-frames (mode constqp). Typiquement init_qpI + 2-3."),
            ParamSpec("init_qpB", "Init QP B", "int", default=25, minimum=0, maximum=51,
                      tooltip="QP initial des B-frames (mode constqp). Typiquement init_qpP + 2."),
            ParamSpec("__free__", "Flags libres (ajoutés tel quel)", "text", default="",
                      tooltip="Tokens ffmpeg additionnels concaténés en fin de commande.\n"
                              "Ex: -bluray-compat 1 -coder cabac"),
        )),
    ),
)

# -------- NVENC (H.264) ------------------------------------------------------

_NVENC_PROFILE_H264 = (("baseline", "baseline"), ("main", "main"), ("high", "high"),
                       ("high444p", "high444p"))

_NVENC_H264 = CodecSchema(
    codec="h264_nvenc",
    style="ffmpeg_flags",
    groups=(
        ParamGroup("Tune", (
            ParamSpec("tune", "Tune", "enum", default="hq", options=_NVENC_TUNES,
                      tooltip="Profil d'usage NVENC.\n"
                              "• hq : qualité maximale — recommandé pour fichier offline.\n"
                              "• ll / ull : streaming faible latence (qualité moindre).\n"
                              "• lossless : sans perte (taille énorme)."),
            ParamSpec("multipass", "Multipass", "enum", default="fullres", options=_NVENC_MULTIPASS,
                      tooltip="Encodage multi-passes pour mieux distribuer le bitrate.\n"
                              "fullres = qualité maximale (~30% plus lent). Requiert Turing+."),
        )),
        ParamGroup("Rate control", (
            ParamSpec("rc-lookahead", "Lookahead", "int", default=32, minimum=0, maximum=64,
                      tooltip="Frames analysées en avance pour optimiser B-frames et QP.\n"
                              "32 = bon compromis. 0 désactive (perte qualité notable)."),
            ParamSpec("maxrate", "Max bitrate (kbps)", "int", default=40000, minimum=0, maximum=200000,
                      suffix=" kbps",
                      tooltip="Plafond instantané du débit (mode VBR).\n"
                              "Typique : 1.5-2× le bitrate cible. H.264 1080p HQ : ~40 Mbps suffit."),
            ParamSpec("bufsize", "Buffer size (kbps)", "int", default=80000, minimum=0, maximum=400000,
                      suffix=" kbps",
                      tooltip="Taille du buffer VBV. Typiquement 2× maxrate.\n"
                              "Plus grand = plus de souplesse pour les pics."),
        )),
        ParamGroup("Adaptive Quant.", (
            ParamSpec("spatial-aq", "Spatial AQ", "bool", default="1",
                      tooltip="AQ spatial : alloue plus de bits aux zones lisses (peau, ciels) où la "
                              "compression est visible.\n"
                              "Recommandé toujours actif — améliore notablement la qualité perçue."),
            ParamSpec("aq-strength", "AQ strength", "int", default=8, minimum=1, maximum=15,
                      tooltip="Intensité de l'AQ spatial (1-15).\n"
                              "8 = défaut équilibré. 12+ peut introduire du flou sur détails fins."),
            ParamSpec("temporal-aq", "Temporal AQ", "bool", default="1",
                      tooltip="AQ temporel : propage la qualité dans le temps en privilégiant les "
                              "frames de référence.\n"
                              "Requiert Turing (RTX 20xx)+. Gain qualité ~5-10%."),
        )),
        ParamGroup("GOP / B-frames", (
            ParamSpec("g", "GOP size", "int", default=250, minimum=1, maximum=600,
                      tooltip="Distance maximale entre keyframes (I-frames).\n"
                              "Typique : 10× framerate. Plus court = meilleur seek, plus long = "
                              "compression accrue."),
            ParamSpec("bf", "B-frames", "int", default=3, minimum=0, maximum=4,
                      tooltip="Nombre maximum de B-frames consécutives.\n"
                              "H.264 limite à 4. 3 = bon défaut. 0 = qualité réduite."),
            ParamSpec("b_ref_mode", "B-ref mode", "enum", default="middle", options=_NVENC_BREF,
                      tooltip="Permet aux B-frames de servir de référence.\n"
                              "• middle : seule la B du milieu est référence — meilleur compromis.\n"
                              "Requiert Turing (RTX 20xx)+."),
            ParamSpec("refs", "Reference frames", "int", default=4, minimum=1, maximum=16,
                      tooltip="Nombre de frames de référence pour la prédiction inter-image.\n"
                              "Plus = meilleure qualité sur scènes complexes, encodage plus lent."),
        )),
        ParamGroup("Profil", (
            ParamSpec("profile", "Profile", "enum", default="high", options=_NVENC_PROFILE_H264,
                      tooltip="Profil H.264 :\n"
                              "• baseline : plus compatible (anciens mobiles), pas de B-frames.\n"
                              "• main : standard.\n"
                              "• high : qualité maximale 8-bit — recommandé pour Blu-ray/streaming.\n"
                              "• high444p : 4:4:4 chroma (cas pro)."),
        )),
        ParamGroup("Avancé / libre", (
            ParamSpec("weighted_pred", "Weighted prediction", "bool",
                      tooltip="Prédiction pondérée : améliore la compression sur fondus, transitions de "
                              "luminosité.\n"
                              "Requiert Pascal (GTX 10xx)+. Peut désactiver certaines combinaisons B-frames."),
            ParamSpec("__free__", "Flags libres", "text", default="",
                      tooltip="Tokens ffmpeg additionnels concaténés en fin de commande.\n"
                              "Ex: -coder cabac -bluray-compat 1"),
        )),
    ),
)

# -------- NVENC (AV1) --------------------------------------------------------

_NVENC_AV1 = CodecSchema(
    codec="av1_nvenc",
    style="ffmpeg_flags",
    groups=(
        ParamGroup("Tune", (
            ParamSpec("tune", "Tune", "enum", default="hq", options=_NVENC_TUNES,
                      tooltip="Profil d'usage NVENC.\n"
                              "• hq : qualité maximale (offline) — recommandé.\n"
                              "• ll / ull : streaming faible latence.\n"
                              "AV1 NVENC requiert Ada Lovelace (RTX 40xx)."),
            ParamSpec("multipass", "Multipass", "enum", default="fullres", options=_NVENC_MULTIPASS,
                      tooltip="Encodage multi-passes pour distribution optimale du bitrate.\n"
                              "fullres = qualité maximale, ~30% plus lent."),
        )),
        ParamGroup("Rate control", (
            ParamSpec("rc-lookahead", "Lookahead", "int", default=32, minimum=0, maximum=64,
                      tooltip="Frames analysées en avance pour optimiser le placement des frames "
                              "et la décision de QP.\n"
                              "32 = bon compromis. 0 désactive."),
        )),
        ParamGroup("GOP", (
            ParamSpec("g", "GOP size", "int", default=240, minimum=1, maximum=600,
                      tooltip="Distance maximale entre keyframes.\n"
                              "AV1 supporte des GOP très longs grâce à sa robustesse aux artefacts. "
                              "240 ≈ 10s à 24p."),
            ParamSpec("bf", "B-frames", "int", default=0, minimum=0, maximum=4,
                      tooltip="Nombre de B-frames. AV1 NVENC les supporte mal — 0 = recommandé.\n"
                              "AV1 utilise plutôt des frames ALTREF (prédiction temporelle avancée)."),
        )),
        ParamGroup("Avancé / libre", (
            ParamSpec("__free__", "Flags libres", "text", default="",
                      tooltip="Tokens ffmpeg additionnels.\n"
                              "Ex: -tile-columns 1 -tile-rows 1"),
        )),
    ),
)

# -------- AMF (HEVC) ---------------------------------------------------------

_AMF_QUALITY = (("speed", "speed"), ("balanced", "balanced"), ("quality", "quality"))
_AMF_RC = (("cqp", "cqp"), ("cbr", "cbr"), ("vbr_peak", "vbr_peak"),
           ("vbr_latency", "vbr_latency"))
_AMF_USAGE = (("transcoding", "transcoding"), ("ultralowlatency", "ultralowlatency"),
              ("lowlatency", "lowlatency"), ("webcam", "webcam"))

_AMF_USAGE_TIP = ("Profil d'usage AMF.\n"
                  "• transcoding : encodage offline qualité — recommandé pour fichiers.\n"
                  "• lowlatency / ultralowlatency : streaming / cloud gaming.\n"
                  "• webcam : capture caméra temps réel.")

_AMF_HEVC = CodecSchema(
    codec="hevc_amf",
    style="ffmpeg_flags",
    groups=(
        ParamGroup("Usage", (
            ParamSpec("usage", "Usage", "enum", default="transcoding", options=_AMF_USAGE,
                      tooltip=_AMF_USAGE_TIP),
        )),
        ParamGroup("Rate control", (
            ParamSpec("min_qp_i", "Min QP I", "int", default=18, minimum=0, maximum=51,
                      tooltip="QP minimum sur les I-frames. Plafonne la qualité maximale.\n"
                              "Augmenter à 22-25 pour limiter le sur-coût bitrate sur scènes simples."),
            ParamSpec("max_qp_i", "Max QP I", "int", default=46, minimum=0, maximum=51,
                      tooltip="QP maximum sur les I-frames. Plafond bas de qualité.\n"
                              "Réduire (~38-42) pour éviter une chute visible sur scènes complexes."),
            ParamSpec("max_au_size", "Max AU size", "int", default=0, minimum=0, maximum=100000000,
                      tooltip="Taille maximale d'une Access Unit (frame compressée) en bytes.\n"
                              "0 = pas de limite. Utile pour streaming HLS/DASH avec limite par segment."),
        )),
        ParamGroup("GOP / B-frames", (
            ParamSpec("gops_per_idr", "GOPs per IDR", "int", default=1, minimum=0, maximum=10,
                      tooltip="Nombre de GOPs entre deux IDR (Instantaneous Decoder Refresh).\n"
                              "1 = chaque keyframe est IDR (compatibilité maximale, recommandé).\n"
                              "Plus grand = compression accrue mais seek dégradé."),
            ParamSpec("header_insertion_mode", "Header insertion", "enum",
                      default="idr", options=(("none", "none"), ("gop", "gop"), ("idr", "idr")),
                      tooltip="Insertion des SPS/PPS/VPS (paramètres de séquence).\n"
                              "• none : début de stream uniquement.\n"
                              "• gop : à chaque GOP.\n"
                              "• idr : à chaque IDR — recommandé pour streaming."),
        )),
        ParamGroup("Profil", (
            ParamSpec("profile", "Profile", "enum", default="main",
                      options=(("main", "main"), ("main10", "main10")),
                      tooltip="• main : 8-bit 4:2:0.\n"
                              "• main10 : 10-bit 4:2:0 — obligatoire pour HDR/DoVi."),
            ParamSpec("level", "Level", "enum", default="auto",
                      options=tuple((v, v) for v in
                                    ("auto", "1", "2", "2.1", "3", "3.1", "4", "4.1", "5", "5.1", "5.2")),
                      tooltip="Level HEVC : limite résolution/framerate/bitrate.\n"
                              "auto recommandé. 5.1 = UHD 4K 60p (Blu-ray UHD)."),
            ParamSpec("tier", "Tier", "enum", default="high",
                      options=(("main", "main"), ("high", "high")),
                      tooltip="• main : bitrate consumer.\n"
                              "• high : bitrate étendu — requis pour UHD HDR haut débit."),
        )),
        ParamGroup("Avancé / libre", (
            ParamSpec("preanalysis", "Pre-analysis", "bool",
                      tooltip="Active une analyse pré-encodage du contenu pour optimiser bitrate et "
                              "placement des frames.\n"
                              "Améliore notablement la qualité au prix de ~10-15% de vitesse."),
            ParamSpec("vbaq", "VBAQ", "bool",
                      tooltip="Variance-Based Adaptive Quantization : équivalent AMF du Spatial AQ.\n"
                              "Alloue plus de bits aux zones perceptuellement sensibles. Recommandé."),
            ParamSpec("enforce_hrd", "Enforce HRD", "bool",
                      tooltip="Force le respect du modèle HRD (Hypothetical Reference Decoder).\n"
                              "Garantit que le stream est décodable en temps réel par tout décodeur "
                              "conforme. Utile pour Blu-ray/diffusion."),
            ParamSpec("__free__", "Flags libres", "text", default="",
                      tooltip="Tokens ffmpeg additionnels.\n"
                              "Ex: -filler_data 1 -coder cabac"),
        )),
    ),
)

# -------- AMF (H.264) --------------------------------------------------------

_AMF_H264 = CodecSchema(
    codec="h264_amf",
    style="ffmpeg_flags",
    groups=(
        ParamGroup("Usage", (
            ParamSpec("usage", "Usage", "enum", default="transcoding", options=_AMF_USAGE,
                      tooltip=_AMF_USAGE_TIP),
        )),
        ParamGroup("Profil", (
            ParamSpec("profile", "Profile", "enum", default="high",
                      options=(("baseline", "baseline"), ("main", "main"), ("high", "high")),
                      tooltip="Profil H.264 :\n"
                              "• baseline : compatibilité maximale (anciens mobiles, sans B-frames).\n"
                              "• main : standard.\n"
                              "• high : qualité maximale 8-bit — recommandé."),
        )),
        ParamGroup("Avancé / libre", (
            ParamSpec("preanalysis", "Pre-analysis", "bool",
                      tooltip="Analyse pré-encodage qui optimise bitrate et placement des frames.\n"
                              "Coût : ~10-15% de vitesse. Gain qualité notable."),
            ParamSpec("vbaq", "VBAQ", "bool",
                      tooltip="Variance-Based Adaptive Quantization (équivalent AMF du Spatial AQ).\n"
                              "Recommandé toujours actif."),
            ParamSpec("__free__", "Flags libres", "text", default="",
                      tooltip="Tokens ffmpeg additionnels."),
        )),
    ),
)

# -------- AMF (AV1) ---------------------------------------------------------

_AMF_AV1 = CodecSchema(
    codec="av1_amf",
    style="ffmpeg_flags",
    groups=(
        ParamGroup("Usage", (
            ParamSpec("usage", "Usage", "enum", default="transcoding", options=_AMF_USAGE,
                      tooltip=_AMF_USAGE_TIP + "\n\nAV1 AMF requiert RX 7000 (RDNA 3) ou plus récent."),
        )),
        ParamGroup("Avancé / libre", (
            ParamSpec("__free__", "Flags libres", "text", default="",
                      tooltip="Tokens ffmpeg additionnels."),
        )),
    ),
)

# -------- QSV (HEVC) ---------------------------------------------------------

_QSV_PRESET = tuple((p, p) for p in ("veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"))

_QSV_LOOKAHEAD_TIP = ("Active la pré-analyse en avance pour optimiser bitrate et placement des frames.\n"
                      "Améliore notablement la qualité. Désactiver uniquement si latence critique.")
_QSV_LOOKAHEAD_DEPTH_TIP = ("Profondeur d'analyse en frames (0-100).\n"
                            "40 = bon défaut. Plus = qualité ↑, mémoire et latence ↑.")
_QSV_ASYNC_DEPTH_TIP = ("Nombre de frames encodées en parallèle dans le pipeline.\n"
                        "4 = bon compromis. Plus = throughput ↑, latence ↑.")

_QSV_HEVC = CodecSchema(
    codec="hevc_qsv",
    style="ffmpeg_flags",
    groups=(
        ParamGroup("Rate control", (
            ParamSpec("look_ahead", "Look-ahead", "bool", default="1",
                      tooltip=_QSV_LOOKAHEAD_TIP),
            ParamSpec("look_ahead_depth", "Lookahead depth", "int", default=40, minimum=0, maximum=100,
                      tooltip=_QSV_LOOKAHEAD_DEPTH_TIP),
            ParamSpec("async_depth", "Async depth", "int", default=4, minimum=1, maximum=8,
                      tooltip=_QSV_ASYNC_DEPTH_TIP),
            ParamSpec("extbrc", "Extended BRC", "bool",
                      tooltip="Bitrate Control étendu : algorithme amélioré pour une meilleure régulation "
                              "du débit, surtout en mode VBR.\n"
                              "Recommandé sur Skylake+ (6e gen Intel ou plus récent)."),
        )),
        ParamGroup("GOP / B-frames", (
            ParamSpec("g", "GOP size", "int", default=248, minimum=1, maximum=600,
                      tooltip="Distance maximale entre keyframes.\n"
                              "Typique : 10× framerate. 248 ≈ 10s à 24p."),
            ParamSpec("bf", "B-frames", "int", default=3, minimum=0, maximum=8,
                      tooltip="Nombre maximum de B-frames consécutives.\n"
                              "3-4 = bon défaut HEVC. 0 = qualité réduite."),
            ParamSpec("b_strategy", "B-strategy", "int", default=1, minimum=0, maximum=1,
                      tooltip="Stratégie de placement des B-frames.\n"
                              "0 = nombre fixe. 1 = adaptatif (recommandé)."),
            ParamSpec("refs", "References", "int", default=4, minimum=1, maximum=16,
                      tooltip="Nombre de frames de référence pour la prédiction inter-image.\n"
                              "Plus = meilleure qualité sur scènes complexes, encodage plus lent."),
        )),
        ParamGroup("Profil / Level", (
            ParamSpec("profile", "Profile", "enum", default="main",
                      options=(("main", "main"), ("main10", "main10")),
                      tooltip="• main : 8-bit 4:2:0.\n"
                              "• main10 : 10-bit 4:2:0 — obligatoire pour HDR/DoVi."),
            ParamSpec("tier", "Tier", "enum", default="high",
                      options=(("main", "main"), ("high", "high")),
                      tooltip="• main : bitrate consumer.\n"
                              "• high : bitrate étendu — requis pour UHD HDR haut débit."),
            ParamSpec("level", "Level", "enum", default="auto",
                      options=tuple((v, v) for v in
                                    ("auto", "1", "2", "2.1", "3", "3.1", "4", "4.1", "5", "5.1", "5.2")),
                      tooltip="Level HEVC : limite résolution/framerate/bitrate.\n"
                              "auto recommandé. 5.1 = UHD 4K 60p (Blu-ray UHD)."),
        )),
        ParamGroup("Avancé / libre", (
            ParamSpec("low_power", "Low power", "bool",
                      tooltip="Active le moteur d'encodage VDENC (basse consommation).\n"
                              "Plus rapide et moins gourmand, qualité légèrement inférieure au pipeline "
                              "complet PAK. Disponible sur Skylake+ pour HEVC."),
            ParamSpec("mbbrc", "MB-level BRC", "bool",
                      tooltip="Macroblock-level Bitrate Control : régule le débit au niveau du MB plutôt "
                              "que de la frame entière.\n"
                              "Améliore la régularité de qualité, coût mineur en vitesse."),
            ParamSpec("__free__", "Flags libres", "text", default="",
                      tooltip="Tokens ffmpeg additionnels."),
        )),
    ),
)

# -------- QSV (H.264) --------------------------------------------------------

_QSV_H264 = CodecSchema(
    codec="h264_qsv",
    style="ffmpeg_flags",
    groups=(
        ParamGroup("Rate control", (
            ParamSpec("look_ahead", "Look-ahead", "bool", default="1",
                      tooltip=_QSV_LOOKAHEAD_TIP),
            ParamSpec("look_ahead_depth", "Lookahead depth", "int", default=40, minimum=0, maximum=100,
                      tooltip=_QSV_LOOKAHEAD_DEPTH_TIP),
            ParamSpec("async_depth", "Async depth", "int", default=4, minimum=1, maximum=8,
                      tooltip=_QSV_ASYNC_DEPTH_TIP),
        )),
        ParamGroup("GOP / B-frames", (
            ParamSpec("g", "GOP size", "int", default=250, minimum=1, maximum=600,
                      tooltip="Distance maximale entre keyframes. Typique : 10× framerate."),
            ParamSpec("bf", "B-frames", "int", default=3, minimum=0, maximum=8,
                      tooltip="Nombre maximum de B-frames consécutives.\n"
                              "H.264 limite à 4 typiquement. 3 = bon défaut."),
            ParamSpec("refs", "References", "int", default=3, minimum=1, maximum=16,
                      tooltip="Nombre de frames de référence pour la prédiction inter-image."),
        )),
        ParamGroup("Profil", (
            ParamSpec("profile", "Profile", "enum", default="high",
                      options=(("baseline", "baseline"), ("main", "main"), ("high", "high")),
                      tooltip="Profil H.264 :\n"
                              "• baseline : compatibilité maximale (sans B-frames).\n"
                              "• main : standard.\n"
                              "• high : qualité maximale 8-bit — recommandé."),
        )),
        ParamGroup("Avancé / libre", (
            ParamSpec("__free__", "Flags libres", "text", default="",
                      tooltip="Tokens ffmpeg additionnels."),
        )),
    ),
)

# -------- QSV (AV1) ----------------------------------------------------------

_QSV_AV1 = CodecSchema(
    codec="av1_qsv",
    style="ffmpeg_flags",
    groups=(
        ParamGroup("Rate control", (
            ParamSpec("async_depth", "Async depth", "int", default=4, minimum=1, maximum=8,
                      tooltip=_QSV_ASYNC_DEPTH_TIP + "\n\nAV1 QSV requiert Arc / Xe-HPG (Alchemist+)."),
        )),
        ParamGroup("Avancé / libre", (
            ParamSpec("__free__", "Flags libres", "text", default="",
                      tooltip="Tokens ffmpeg additionnels."),
        )),
    ),
)

# -------- VAAPI (HEVC) -------------------------------------------------------

_VAAPI_RC_MODE = (
    ("auto", "auto (driver)"),
    ("CQP", "CQP"),
    ("CBR", "CBR"),
    ("VBR", "VBR"),
    ("ICQ", "ICQ"),
    ("QVBR", "QVBR"),
    ("AVBR", "AVBR"),
)

_VAAPI_COMMON_GOP_PARAMS = (
    ParamSpec("g", "GOP size", "int", default=240, minimum=1, maximum=600,
              tooltip="Distance maximale entre keyframes (I-frames).\n"
                      "Ce paramètre pilote le GOP standard ffmpeg (-g), en plus des IDR open-GOP."),
    ParamSpec("bf", "B-frames", "int", default=3, minimum=0, maximum=16,
              tooltip="Nombre maximum de B-frames consécutives autorisées par le wrapper VAAPI.\n"
                      "Plus de B-frames améliore souvent la compression, au prix de latence et de compatibilité."),
    ParamSpec("idr_interval", "IDR interval (frames)", "int",
              default=0, minimum=0, maximum=600,
              tooltip="Distance entre IDR (Instantaneous Decoder Refresh).\n"
                      "0 = laisse le driver décider. Une IDR vide complètement le buffer de référence."),
    ParamSpec("b_depth", "B-frame ref depth", "int", default=1, minimum=1, maximum=4,
              tooltip="Profondeur de la pyramide de références B-frames.\n"
                      "1 = B-frames non-référencées. 2-4 = hiérarchique si supporté par le driver."),
)

_VAAPI_COMMON_RATE_CONTROL_PARAMS = (
    ParamSpec("rc_mode", "Rate control mode", "enum", default="auto", options=_VAAPI_RC_MODE,
              tooltip="Mode de contrôle de débit VAAPI.\n"
                      "• auto : laisse le driver choisir.\n"
                      "• CQP : quantizer fixe.\n"
                      "• CBR/VBR : débit piloté.\n"
                      "• ICQ/QVBR/AVBR : modes qualité avancés, support driver variable."),
    ParamSpec("async_depth", "Async depth", "int", default=2, minimum=1, maximum=64,
              tooltip="Parallélisme interne VAAPI. Augmenter peut améliorer le débit d'encodage sur un flux unique,\n"
                      "mais exige plus de surfaces et ajoute de la latence."),
    ParamSpec("compression_level", "Compression level", "int", default=4, minimum=0, maximum=7,
              tooltip="Compromis vitesse/qualité du wrapper VAAPI.\n"
                      "Valeur plus haute = plus rapide, mais qualité/compression légèrement inférieures."),
    ParamSpec("b", "Bitrate cible", "text", default="",
              tooltip="Bitrate ffmpeg standard (-b). Accepte les suffixes ffmpeg, ex: 18M ou 18000k.\n"
                      "Peut volontairement surcharger le mode SIZE du workflow."),
    ParamSpec("maxrate", "Max bitrate", "text", default="",
              tooltip="Plafond ffmpeg standard (-maxrate). Accepte les suffixes ffmpeg, ex: 24M."),
    ParamSpec("bufsize", "Buffer size", "text", default="",
              tooltip="Taille du buffer VBV ffmpeg (-bufsize). Accepte les suffixes ffmpeg, ex: 36M."),
    ParamSpec("rc_init_occupancy", "RC init occupancy", "text", default="",
              tooltip="Occupation initiale du buffer VBV (-rc_init_occupancy).\n"
                      "Accepte les suffixes ffmpeg, ex: 18M."),
    ParamSpec("qp", "QP", "int", default=22, minimum=0, maximum=52,
              tooltip="Quantizer constant pour les modes CQP.\n"
                      "Sur HEVC/H.264 VAAPI, ce champ correspond au QP des P-frames."),
    ParamSpec("q", "Global quality", "int", default=23, minimum=0, maximum=255,
              tooltip="Option ffmpeg standard (-q / -global_quality).\n"
                      "Plus la valeur est élevée, plus la qualité baisse et la taille diminue."),
    ParamSpec("qmin", "QP min", "int", default=0, minimum=0, maximum=255,
              tooltip="Bornage inférieur du quantizer autorisé."),
    ParamSpec("qmax", "QP max", "int", default=51, minimum=0, maximum=255,
              tooltip="Bornage supérieur du quantizer autorisé."),
    ParamSpec("i_qfactor", "I qfactor", "float", default=-0.8, minimum=-20.0, maximum=20.0, step=0.1,
              tooltip="Facteur relatif appliqué aux I-frames par rapport aux P-frames.\n"
                      "Valeurs négatives favorisent davantage les I-frames."),
    ParamSpec("i_qoffset", "I qoffset", "float", default=0.0, minimum=-20.0, maximum=20.0, step=0.1,
              tooltip="Offset ajouté au quantizer des I-frames."),
    ParamSpec("b_qfactor", "B qfactor", "float", default=1.25, minimum=-20.0, maximum=20.0, step=0.1,
              tooltip="Facteur relatif appliqué aux B-frames par rapport aux P-frames."),
    ParamSpec("b_qoffset", "B qoffset", "float", default=1.25, minimum=-20.0, maximum=20.0, step=0.1,
              tooltip="Offset ajouté au quantizer des B-frames."),
    ParamSpec("blbrc", "Block-level BRC", "bool",
              tooltip="Bitrate Control au niveau bloc plutôt que frame.\n"
                      "Régule plus finement le débit intra-frame. Invalide en mode CQP."),
    ParamSpec("max_frame_size", "Max frame size (bytes)", "int",
              default=0, minimum=0, maximum=100_000_000,
              tooltip="Taille maximale d'une frame compressée en bytes.\n"
                      "0 = pas de limite. Utile pour streaming avec contraintes de buffer."),
    ParamSpec("slices", "Slices", "int", default=0, minimum=0, maximum=256,
              tooltip="Nombre de slices de sortie demandé au wrapper ffmpeg.\n"
                      "0 = laisse l'encodeur choisir."),
)

_VAAPI_COMMON_PLATFORM_PARAMS = (
    ParamSpec("low_power", "Low-power encoding", "bool",
              tooltip="Active le moteur basse consommation quand la plateforme en propose un.\n"
                      "Peut réduire le set de fonctionnalités disponibles."),
)

_VAAPI_HEVC_LEVEL = tuple((v, v) for v in
                          ("auto", "1", "2", "2.1", "3", "3.1", "4", "4.1",
                           "5", "5.1", "5.2", "6", "6.1", "6.2"))
_VAAPI_HEVC_PROFILE = (("main", "main"), ("main10", "main10 (10-bit)"), ("rext", "rext"))
_VAAPI_TIER = (("main", "main"), ("high", "high"))
_VAAPI_HEVC_SEI = (("hdr+a53_cc", "hdr+a53_cc (défaut)"), ("hdr", "hdr"),
                   ("a53_cc", "a53_cc"), ("", "(aucun)"))

_VAAPI_HEVC = CodecSchema(
    codec="hevc_vaapi",
    style="ffmpeg_flags",
    groups=(
        ParamGroup("GOP / B-frames", _VAAPI_COMMON_GOP_PARAMS),
        ParamGroup("Rate control", _VAAPI_COMMON_RATE_CONTROL_PARAMS),
        ParamGroup("Profil / Level", (
            ParamSpec("profile", "Profile", "enum", default="main10", options=_VAAPI_HEVC_PROFILE,
                      tooltip="• main : 8-bit 4:2:0 — compatibilité universelle.\n"
                              "• main10 : 10-bit 4:2:0 — obligatoire pour HDR/DoVi.\n"
                              "• rext : range extensions (4:4:4, 12-bit) — usages pro."),
            ParamSpec("level", "Level", "enum", default="auto", options=_VAAPI_HEVC_LEVEL,
                      tooltip="Level HEVC : limite résolution/framerate/bitrate.\n"
                              "auto recommandé. 5.1 = UHD 4K 60p (Blu-ray UHD)."),
            ParamSpec("tier", "Tier", "enum", default="high", options=_VAAPI_TIER,
                      tooltip="• main : bitrate plafonné selon le level (consumer).\n"
                              "• high : bitrate étendu — requis pour UHD HDR haut bitrate."),
        )),
        ParamGroup("HDR / SEI", (
            ParamSpec("sei", "SEI à inclure", "enum", default="hdr+a53_cc",
                      options=_VAAPI_HEVC_SEI,
                      tooltip="Messages SEI insérés dans le bitstream :\n"
                              "• hdr : mastering display + content light level (HDR10 statique).\n"
                              "• a53_cc : sous-titres ATSC A/53 (CC).\n"
                              "Sans 'hdr', les métadonnées HDR ne seront PAS écrites."),
            ParamSpec("aud", "Inclure AUD", "bool",
                      tooltip="Access Unit Delimiter NAL : marqueur de début de frame.\n"
                              "Recommandé pour streaming MPEG-TS/RTP. Inutile pour fichier MKV/MP4."),
        )),
        ParamGroup("Tiles / Plateforme", (
            ParamSpec("tiles", "Tiles (colsxrows)", "text", default="",
                      tooltip="Découpage en tuiles HEVC pour parallélisation décodage.\n"
                              "Format : 'colsxrows' (ex: 2x1, 4x2). Vide = pas de tuiles.\n"
                              "Utile pour 4K+ : améliore les performances du décodeur côté lecture."),
            *_VAAPI_COMMON_PLATFORM_PARAMS,
        )),
        ParamGroup("Avancé / libre", (
            ParamSpec("__free__", "Flags libres", "text", default="",
                      tooltip="Tokens ffmpeg additionnels."),
        )),
    ),
)

# -------- VAAPI (H.264) ------------------------------------------------------

_VAAPI_H264_PROFILE = (("constrained_baseline", "constrained_baseline"),
                       ("main", "main"), ("high", "high"), ("high10", "high10"))
_VAAPI_H264_LEVEL = tuple((v, v) for v in
                          ("auto", "1", "1.1", "1.2", "1.3", "2", "2.1", "2.2",
                           "3", "3.1", "3.2", "4", "4.1", "4.2",
                           "5", "5.1", "5.2", "6", "6.1", "6.2"))
_VAAPI_H264_SEI = (("identifier+timing+recovery_point+a53_cc", "tout (défaut)"),
                   ("a53_cc", "a53_cc seul"),
                   ("identifier+recovery_point", "identifier+recovery_point"),
                   ("", "(aucun)"))
_VAAPI_H264_CODER = (("cabac", "cabac"), ("cavlc", "cavlc"))

_VAAPI_H264 = CodecSchema(
    codec="h264_vaapi",
    style="ffmpeg_flags",
    groups=(
        ParamGroup("GOP / B-frames", _VAAPI_COMMON_GOP_PARAMS),
        ParamGroup("Rate control", (
            *_VAAPI_COMMON_RATE_CONTROL_PARAMS,
            ParamSpec("quality", "Quality (speed/quality)", "int",
                      default=-1, minimum=-1, maximum=8,
                      tooltip="Compromis vitesse/qualité du moteur d'encodage.\n"
                              "-1 = défaut driver. 0 = qualité maximale (lent). 8 = très rapide.\n"
                              "Spécifique H.264 VAAPI (pas dispo sur HEVC/AV1)."),
        )),
        ParamGroup("Codage", (
            ParamSpec("coder", "Entropy coder", "enum", default="cabac", options=_VAAPI_H264_CODER,
                      tooltip="• cabac : Context-Adaptive Binary Arithmetic Coding — qualité maximale.\n"
                              "• cavlc : variable-length coding — plus rapide à décoder, requis pour le "
                              "profil baseline (anciens mobiles)."),
        )),
        ParamGroup("Profil / Level", (
            ParamSpec("profile", "Profile", "enum", default="high", options=_VAAPI_H264_PROFILE,
                      tooltip="Profil H.264 :\n"
                              "• constrained_baseline : compatibilité maximale (sans B-frames, cabac).\n"
                              "• main : standard.\n"
                              "• high : qualité maximale 8-bit — recommandé.\n"
                              "• high10 : 10-bit (rare, peu de décodeurs hardware)."),
            ParamSpec("level", "Level", "enum", default="auto", options=_VAAPI_H264_LEVEL,
                      tooltip="Level H.264 : limite résolution/framerate/bitrate.\n"
                              "auto recommandé. 4.1 = 1080p60 max. 5.1 = 4K 30p."),
        )),
        ParamGroup("SEI / AUD", (
            ParamSpec("sei", "SEI à inclure", "enum",
                      default="identifier+timing+recovery_point+a53_cc",
                      options=_VAAPI_H264_SEI,
                      tooltip="Messages SEI insérés dans le bitstream :\n"
                              "• identifier : version de l'encodeur.\n"
                              "• timing : buffering_period + pic_timing (décodage temps réel).\n"
                              "• recovery_point : point de reprise après erreur.\n"
                              "• a53_cc : sous-titres ATSC A/53 (CC)."),
            ParamSpec("aud", "Inclure AUD", "bool",
                      tooltip="Access Unit Delimiter : marqueur de début de frame.\n"
                              "Recommandé pour streaming MPEG-TS/RTP. Inutile pour MKV/MP4."),
        )),
        ParamGroup("Plateforme", (
            *_VAAPI_COMMON_PLATFORM_PARAMS,
        )),
        ParamGroup("Avancé / libre", (
            ParamSpec("__free__", "Flags libres", "text", default="",
                      tooltip="Tokens ffmpeg additionnels."),
        )),
    ),
)

# -------- VAAPI (AV1) --------------------------------------------------------

_VAAPI_AV1_PROFILE = (("main", "main"), ("high", "high"), ("professional", "professional"))
_VAAPI_AV1_LEVEL = tuple((v, v) for v in
                         ("auto", "2.0", "2.1", "3.0", "3.1", "4.0", "4.1",
                          "5.0", "5.1", "5.2", "5.3", "6.0", "6.1", "6.2", "6.3"))

_VAAPI_AV1 = CodecSchema(
    codec="av1_vaapi",
    style="ffmpeg_flags",
    groups=(
        ParamGroup("GOP / B-frames", _VAAPI_COMMON_GOP_PARAMS),
        ParamGroup("Rate control", tuple(
            spec for spec in _VAAPI_COMMON_RATE_CONTROL_PARAMS if spec.key != "qp"
        )),
        ParamGroup("Profil / Level", (
            ParamSpec("profile", "Profile", "enum", default="main", options=_VAAPI_AV1_PROFILE,
                      tooltip="Profil AV1 :\n"
                              "• main : 8/10-bit 4:2:0 — usage standard.\n"
                              "• high : 8/10-bit 4:4:4.\n"
                              "• professional : 8/10/12-bit, tous chromas (cas pro)."),
            ParamSpec("level", "Level", "enum", default="auto", options=_VAAPI_AV1_LEVEL,
                      tooltip="Level AV1 : limite résolution/framerate/bitrate.\n"
                              "auto recommandé. 5.0 = 4K 30p, 5.1 = 4K 60p, 6.0+ = 8K."),
            ParamSpec("tier", "Tier", "enum", default="main", options=_VAAPI_TIER,
                      tooltip="• main : bitrate consumer.\n"
                              "• high : bitrate étendu — pour mastering haut débit."),
        )),
        ParamGroup("Tiles", (
            ParamSpec("tiles", "Tiles (colsxrows)", "text", default="",
                      tooltip="Découpage en tuiles AV1 pour parallélisation décodage.\n"
                              "Format : 'colsxrows' (ex: 2x2). Vide = pas de tuiles.\n"
                              "Recommandé pour 4K+ : améliore les performances décodeur logiciel "
                              "(dav1d notamment)."),
            ParamSpec("tile_groups", "Tile groups", "int", default=1, minimum=1, maximum=4096,
                      tooltip="Nombre de groupes de tuiles par frame.\n"
                              "Permet de découper le bitstream pour transport segmenté. 1 = défaut."),
        )),
        ParamGroup("Plateforme", (
            *_VAAPI_COMMON_PLATFORM_PARAMS,
        )),
        ParamGroup("Avancé / libre", (
            ParamSpec("__free__", "Flags libres", "text", default="",
                      tooltip="Tokens ffmpeg additionnels."),
        )),
    ),
)

# -------- libx265 (style key=val:key=val) ------------------------------------

_X265_LIBX265 = CodecSchema(
    codec="libx265",
    style="x265_params",
    groups=(
        ParamGroup("HDR / Color", (
            ParamSpec("hdr10", "HDR10 metadata", "bool", bool_repr=("0", "1"),
                      tooltip="Active la signalisation HDR10 dans le bitstream (SEI mastering display "
                              "+ content light level).\n"
                              "Indispensable pour les sources HDR10 — sans ça, le téléviseur n'active "
                              "pas le mode HDR."),
            ParamSpec("hdr10-opt", "HDR10 optimisations", "bool", bool_repr=("0", "1"),
                      tooltip="Optimisations spécifiques HDR10 : ajuste le pipeline RD pour mieux "
                              "préserver les détails dans les ombres et hautes lumières (PQ).\n"
                              "Recommandé conjointement à hdr10."),
            ParamSpec("repeat-headers", "Repeat headers", "bool", bool_repr=("0", "1"),
                      tooltip="Insère SPS/PPS/VPS à chaque keyframe plutôt qu'une seule fois en début "
                              "de stream.\n"
                              "Recommandé pour streaming/segments (HLS/DASH) ou cut/append. Coût "
                              "bitrate négligeable."),
            ParamSpec("master-display", "Master display", "text", default="",
                      tooltip="Métadonnées SMPTE ST 2086 : primaires/blancpoint/luminance du moniteur "
                              "de mastering.\n"
                              "Format : G(x,y)B(x,y)R(x,y)WP(x,y)L(max,min) — chromaticité ×50000, "
                              "luminance ×10000.\n"
                              "Ex BT.2020 1000 nits :\n"
                              "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)"),
            ParamSpec("max-cll", "Max CLL", "text", default="1000,400",
                      tooltip="Max Content Light Level + Max Frame-Average Light Level (cd/m²).\n"
                              "Format : 'maxCLL,maxFALL'. Typique 4K HDR10 : 1000,400."),
            ParamSpec("colorprim", "Color primaries", "enum", default="bt2020",
                      options=(("bt709", "bt709"), ("bt2020", "bt2020")),
                      tooltip="Primaires de couleur :\n"
                              "• bt709 : SDR HD (Rec.709).\n"
                              "• bt2020 : UHD/HDR (Rec.2020) — obligatoire pour HDR10/DoVi."),
            ParamSpec("transfer", "Transfer", "enum", default="smpte2084",
                      options=(("bt709", "bt709"), ("smpte2084", "smpte2084 (PQ)"),
                               ("arib-std-b67", "arib-std-b67 (HLG)")),
                      tooltip="Fonction de transfert :\n"
                              "• bt709 : SDR (gamma 2.4).\n"
                              "• smpte2084 : PQ (Perceptual Quantizer) — HDR10/DoVi.\n"
                              "• arib-std-b67 : HLG (Hybrid Log-Gamma) — broadcast HDR."),
            ParamSpec("colormatrix", "Color matrix", "enum", default="bt2020nc",
                      options=(("bt709", "bt709"), ("bt2020nc", "bt2020nc"), ("bt2020c", "bt2020c")),
                      tooltip="Matrice de conversion YCbCr↔RGB :\n"
                              "• bt709 : HD SDR.\n"
                              "• bt2020nc : non-constant luminance (UHD/HDR standard).\n"
                              "• bt2020c : constant luminance (rare)."),
        )),
        ParamGroup("Adaptive Quant.", (
            ParamSpec("aq-mode", "AQ mode", "enum", default="3",
                      options=(("0", "0 — off"), ("1", "1 — variance"),
                               ("2", "2 — auto"), ("3", "3 — auto+bias"), ("4", "4 — auto+bias+select")),
                      tooltip="Algorithme d'Adaptive Quantization (allocation différentielle de bits).\n"
                              "• 0 : désactivé.\n"
                              "• 1 : basé sur la variance.\n"
                              "• 2 : auto-variance.\n"
                              "• 3 : auto-variance + bias zones sombres (recommandé).\n"
                              "• 4 : ajoute la sélection adaptative de profondeur de bits."),
            ParamSpec("aq-strength", "AQ strength", "float", default=1.0,
                      minimum=0.0, maximum=3.0, step=0.1,
                      tooltip="Intensité de l'AQ (0.0-3.0).\n"
                              "1.0 = défaut équilibré. 1.5-2.0 = plus agressif (peut introduire du flou)."),
            ParamSpec("psy-rd", "Psy RD", "float", default=2.0, minimum=0.0, maximum=5.0, step=0.1,
                      tooltip="Psy-RD : favorise la préservation visuelle des détails au prix d'une "
                              "métrique PSNR/SSIM moindre.\n"
                              "2.0 = défaut très bon pour film. 0 = désactive (utile pour benchmarking)."),
            ParamSpec("psy-rdoq", "Psy RDOQ", "float", default=1.0, minimum=0.0, maximum=10.0, step=0.1,
                      tooltip="Psy-RDOQ : préservation perceptuelle dans la quantification trellis.\n"
                              "Renforce les détails fins (texture, grain). 1.0-2.0 typique."),
            ParamSpec("rdoq-level", "RDOQ level", "enum", default="2",
                      options=(("0", "0"), ("1", "1"), ("2", "2")),
                      tooltip="Profondeur du Rate-Distortion Optimized Quantization.\n"
                              "• 0 : désactivé. • 1 : niveau modéré. • 2 : complet (qualité maximale, "
                              "lent)."),
        )),
        ParamGroup("GOP / Slices", (
            ParamSpec("keyint", "Keyint", "int", default=250, minimum=1, maximum=600,
                      tooltip="Distance maximale entre keyframes (I-frames).\n"
                              "Typique : 10× framerate. Plus court = meilleur seek, plus long = "
                              "compression accrue."),
            ParamSpec("min-keyint", "Min keyint", "int", default=25, minimum=1, maximum=300,
                      tooltip="Distance minimale entre keyframes.\n"
                              "Typique : framerate (1 keyframe/seconde min). Évite les keyframes trop "
                              "rapprochées sur scenecut multiples."),
            ParamSpec("bframes", "B-frames", "int", default=4, minimum=0, maximum=16,
                      tooltip="Nombre maximum de B-frames consécutives.\n"
                              "4-8 = bon pour HEVC. 0 = qualité réduite. >8 = gain marginal."),
            ParamSpec("b-adapt", "B-adapt", "enum", default="2",
                      options=(("0", "0"), ("1", "1"), ("2", "2")),
                      tooltip="Décision adaptative des B-frames :\n"
                              "• 0 : nombre fixe.\n"
                              "• 1 : rapide (compromis).\n"
                              "• 2 : optimal (lent mais qualité maximale)."),
            ParamSpec("ref", "References", "int", default=4, minimum=1, maximum=16,
                      tooltip="Nombre de frames de référence pour la prédiction inter-image.\n"
                              "Plus = qualité ↑ sur scènes complexes, encodage plus lent.\n"
                              "Limité par le level HEVC choisi."),
            ParamSpec("no-open-gop", "No open GOP", "bool", bool_repr=("0", "1"),
                      tooltip="Force des GOP fermés (closed GOP) : pas de référence à des frames "
                              "antérieures à la dernière keyframe.\n"
                              "Recommandé pour : streaming HLS/DASH, montage/découpe, Blu-ray."),
            ParamSpec("scenecut", "Scenecut", "int", default=40, minimum=0, maximum=100,
                      tooltip="Sensibilité de la détection de changement de scène (0-100).\n"
                              "40 = défaut équilibré. 0 = désactive (GOP régulier strict)."),
        )),
        ParamGroup("Tools", (
            ParamSpec("ctu", "CTU size", "enum", default="64",
                      options=(("16", "16"), ("32", "32"), ("64", "64")),
                      tooltip="Taille du Coding Tree Unit (équivalent macrobloc HEVC).\n"
                              "• 64 : qualité maximale 4K (recommandé).\n"
                              "• 32 : compromis vitesse/qualité.\n"
                              "• 16 : très rapide, qualité dégradée."),
            ParamSpec("rd", "RD level", "int", default=4, minimum=1, maximum=6,
                      tooltip="Profondeur du Rate-Distortion (1-6). Plus haut = meilleure décision "
                              "de partition mais plus lent.\n"
                              "4 = défaut très bon. 6 = maximum (placebo)."),
            ParamSpec("subme", "Subpel ME", "int", default=3, minimum=1, maximum=7,
                      tooltip="Précision de motion estimation sub-pixel (1-7).\n"
                              "3 = défaut. 5+ = qualité maximale, encodage notablement plus lent."),
            ParamSpec("me", "Motion estimation", "enum", default="hex",
                      options=(("dia", "dia"), ("hex", "hex"), ("umh", "umh"),
                               ("star", "star"), ("sea", "sea"), ("full", "full")),
                      tooltip="Algorithme de motion estimation :\n"
                              "• dia : diamond — rapide.\n"
                              "• hex : hexagon — défaut équilibré.\n"
                              "• umh : uneven multi-hex — qualité ↑.\n"
                              "• star/sea/full : recherche exhaustive (très lent, gain marginal)."),
            ParamSpec("merange", "Merange", "int", default=57, minimum=0, maximum=128,
                      tooltip="Rayon de recherche du motion estimation en pixels.\n"
                              "57 = défaut. Augmenter pour scènes très mouvementées (panoramiques rapides)."),
            ParamSpec("rect", "Rectangular partitions", "bool", bool_repr=("0", "1"),
                      tooltip="Active les partitions rectangulaires asymétriques (Nx2N, 2NxN).\n"
                              "Améliore la compression à coût modéré. Désactivé par les presets rapides."),
            ParamSpec("amp", "AMP partitions", "bool", bool_repr=("0", "1"),
                      tooltip="Asymmetric Motion Partitions : partitions encore plus fines "
                              "(2NxnU, 2NxnD, etc.).\n"
                              "Gain qualité notable sur preset slower+. Coût encodage important."),
            ParamSpec("limit-modes", "Limit modes", "bool", bool_repr=("0", "1"),
                      tooltip="Limite intelligemment les modes de prédiction analysés selon le contenu.\n"
                              "Accélère sans perte significative de qualité."),
            ParamSpec("strong-intra-smoothing", "Strong intra smoothing", "bool", bool_repr=("0", "1"),
                      tooltip="Lissage agressif sur grands blocs intra (32×32, 64×64) pour zones "
                              "très uniformes (ciels, dégradés).\n"
                              "Recommandé sur sources propres (UHD), à désactiver sur source bruitée."),
            ParamSpec("sao", "SAO", "bool", bool_repr=("0", "1"),
                      tooltip="Sample Adaptive Offset : filtre in-loop qui réduit les artefacts de "
                              "compression (ringing).\n"
                              "Recommandé toujours actif (gain qualité visuel, coût mineur)."),
        )),
        ParamGroup("Threading / Misc", (
            ParamSpec("pools", "Thread pools", "text", default="*",
                      tooltip="Configuration des pools de threads NUMA.\n"
                              "• '*' : auto (utilise tous les cores).\n"
                              "• '4,4' : 2 pools de 4 threads (NUMA explicite).\n"
                              "• '+' : limite à 1 pool unique."),
            ParamSpec("frame-threads", "Frame threads", "int", default=0, minimum=0, maximum=16,
                      tooltip="Nombre de frames encodées en parallèle.\n"
                              "0 = auto (selon RAM/CPU). Plus = throughput ↑ mais latence et RAM ↑.\n"
                              "Réduire pour économiser la RAM sur 4K."),
            ParamSpec("wpp", "Wavefront parallel proc.", "bool", bool_repr=("0", "1"),
                      tooltip="Wavefront Parallel Processing : parallélise l'encodage par lignes de CTU.\n"
                              "Recommandé toujours actif. Permet une accélération multi-cœurs efficace."),
            ParamSpec("pmode", "Parallel mode decision", "bool", bool_repr=("0", "1"),
                      tooltip="Parallélise la décision de mode de partition.\n"
                              "Accélération CPU sur machines avec beaucoup de cores. Coût mémoire mineur."),
            ParamSpec("__free__", "Tokens libres (ajoutés en fin)", "text", default="",
                      tooltip="Tokens x265-params additionnels (séparés par ':').\n"
                              "Ex: log-level=warning:stats=stats.log:csv=summary.csv"),
        )),
    ),
)

# -------- libsvtav1 (style key=val:key=val) ----------------------------------

_LIBSVTAV1 = CodecSchema(
    codec="libsvtav1",
    style="svtav1_params",
    groups=(
        ParamGroup("Tune", (
            ParamSpec("tune", "Tune", "enum", default="0",
                      options=(("0", "0 — VQ"), ("1", "1 — PSNR"), ("2", "2 — SSIM")),
                      tooltip="Cible d'optimisation de l'encodeur.\n"
                              "• 0 — VQ : qualité visuelle subjective (recommandé pour film/vidéo).\n"
                              "• 1 — PSNR : optimise la métrique PSNR (benchmarking).\n"
                              "• 2 — SSIM : optimise la métrique SSIM."),
            ParamSpec("film-grain", "Film grain", "int", default=0, minimum=0, maximum=50,
                      tooltip="Synthèse de grain de film côté décodeur (0-50).\n"
                              "Le grain est analysé puis re-synthétisé après décodage — économise un "
                              "bitrate considérable sur les sources granuleuses.\n"
                              "0 = désactivé. 8-15 = source 35mm typique. 25+ = grain prononcé."),
            ParamSpec("film-grain-denoise", "Grain denoise", "bool", bool_repr=("0", "1"),
                      tooltip="Applique un débruitage avant analyse du grain pour mieux séparer le "
                              "signal du bruit.\n"
                              "Conseillé conjointement à film-grain ≥ 8 pour les sources analogiques."),
        )),
        ParamGroup("GOP / Latency", (
            ParamSpec("keyint", "Keyint", "int", default=240, minimum=1, maximum=600,
                      tooltip="Distance maximale entre keyframes.\n"
                              "AV1 supporte des GOP très longs grâce à sa robustesse aux artefacts. "
                              "240 ≈ 10s à 24p."),
            ParamSpec("scd", "Scene change detect", "bool", bool_repr=("0", "1"),
                      tooltip="Active la détection de changement de scène pour insérer des keyframes "
                              "adaptatives.\n"
                              "Recommandé toujours actif. Améliore la qualité des cuts."),
            ParamSpec("lookahead", "Lookahead", "int", default=33, minimum=0, maximum=120,
                      tooltip="Frames analysées en avance pour optimiser le placement des frames de "
                              "référence et la décision de QP.\n"
                              "33 = bon défaut. 60+ = qualité ↑ légèrement, RAM et latence ↑."),
            ParamSpec("pred-struct", "Prediction structure", "enum", default="2",
                      options=(("0", "0 — low delay P"), ("1", "1 — low delay B"), ("2", "2 — random")),
                      tooltip="Structure de prédiction inter-image :\n"
                              "• 0 — low-delay P : streaming faible latence (P-frames seulement).\n"
                              "• 1 — low-delay B : streaming + B-frames.\n"
                              "• 2 — random access : pyramide hiérarchique — qualité maximale (recommandé)."),
        )),
        ParamGroup("Rate control", (
            ParamSpec("aq-mode", "AQ mode", "enum", default="2",
                      options=(("0", "0 — off"), ("1", "1 — variance"), ("2", "2 — variance+lambda")),
                      tooltip="Adaptive Quantization :\n"
                              "• 0 : désactivé.\n"
                              "• 1 : basé sur variance des blocs.\n"
                              "• 2 : variance + ajustement du lagrangien (recommandé)."),
            ParamSpec("enable-tf", "Temporal filtering", "bool", bool_repr=("0", "1"),
                      tooltip="Filtre temporel pré-encodage : réduit le bruit en moyennant les frames "
                              "voisines.\n"
                              "Améliore la compression sur sources bruitées. À désactiver sur sources "
                              "déjà débruitées (anime)."),
        )),
        ParamGroup("HDR / Color", (
            ParamSpec("color-primaries", "Color primaries", "enum", default="9",
                      options=(("1", "1 — bt709"), ("9", "9 — bt2020")),
                      tooltip="Primaires de couleur (codes ITU-T H.273) :\n"
                              "• 1 : BT.709 (SDR HD).\n"
                              "• 9 : BT.2020 (UHD/HDR — obligatoire pour HDR10)."),
            ParamSpec("transfer-characteristics", "Transfer", "enum", default="16",
                      options=(("1", "1 — bt709"), ("16", "16 — smpte2084"), ("18", "18 — HLG")),
                      tooltip="Fonction de transfert :\n"
                              "• 1 : BT.709 (SDR).\n"
                              "• 16 : SMPTE ST 2084 (PQ — HDR10).\n"
                              "• 18 : HLG (broadcast HDR)."),
            ParamSpec("matrix-coefficients", "Matrix coefs", "enum", default="9",
                      options=(("1", "1 — bt709"), ("9", "9 — bt2020nc")),
                      tooltip="Matrice YCbCr↔RGB :\n"
                              "• 1 : BT.709.\n"
                              "• 9 : BT.2020 non-constant luminance (UHD/HDR standard)."),
            ParamSpec("mastering-display", "Mastering display", "text", default="",
                      tooltip="Métadonnées SMPTE ST 2086 du moniteur de mastering.\n"
                              "Format svtav1 : 'G(x,y)B(x,y)R(x,y)WP(x,y)L(max,min)' "
                              "(chromaticité ×50000, luminance ×10000)."),
            ParamSpec("content-light", "Content light", "text", default="1000,400",
                      tooltip="Max Content Light Level + Max Frame-Average Light Level (cd/m²).\n"
                              "Format : 'maxCLL,maxFALL'. Typique HDR10 : 1000,400."),
        )),
        ParamGroup("Misc", (
            ParamSpec("tile-rows", "Tile rows (log2)", "int", default=0, minimum=0, maximum=6,
                      tooltip="Nombre de lignes de tuiles, exprimé en log2.\n"
                              "0 = 1 ligne, 1 = 2, 2 = 4, etc.\n"
                              "Utile pour 4K+ : améliore parallélisme décodeur (dav1d)."),
            ParamSpec("tile-columns", "Tile columns (log2)", "int", default=0, minimum=0, maximum=6,
                      tooltip="Nombre de colonnes de tuiles, log2.\n"
                              "Pour 4K HDR : tile-columns=1 ou 2 typique."),
            ParamSpec("__free__", "Tokens libres", "text", default="",
                      tooltip="Tokens svtav1-params additionnels (séparés par ':')."),
        )),
    ),
)

# -------- libx264 (flags ffmpeg + -x264-params) ------------------------------

_LIBX264 = CodecSchema(
    codec="libx264",
    style="ffmpeg_flags",
    groups=(
        ParamGroup("Tune / Profile", (
            ParamSpec("tune", "Tune", "enum", default="film",
                      options=tuple((v, v) for v in
                                    ("film", "animation", "grain", "stillimage", "psnr",
                                     "ssim", "fastdecode", "zerolatency")),
                      tooltip="Profil d'optimisation x264 selon le contenu :\n"
                              "• film : sources caméra/cinéma — recommandé général.\n"
                              "• animation : dessin animé / cell-shaded (plus de B-frames, deblocking ↑).\n"
                              "• grain : préserve le grain de film (psy-rd ↑, dct-decimate off).\n"
                              "• stillimage : diaporama, screencast.\n"
                              "• psnr / ssim : optimise la métrique (benchmarking).\n"
                              "• fastdecode : pour décodeurs faibles (CABAC off).\n"
                              "• zerolatency : streaming temps réel."),
            ParamSpec("profile:v", "Profile", "enum", default="high",
                      options=(("baseline", "baseline"), ("main", "main"),
                               ("high", "high"), ("high10", "high10"), ("high422", "high422"),
                               ("high444", "high444")),
                      tooltip="Profil H.264 :\n"
                              "• baseline : compatibilité maximale (anciens mobiles, sans B-frames).\n"
                              "• main : standard.\n"
                              "• high : qualité maximale 8-bit — recommandé.\n"
                              "• high10 : 10-bit 4:2:0.\n"
                              "• high422 : 10-bit 4:2:2 (cas pro).\n"
                              "• high444 : 10/12-bit 4:4:4 (mastering)."),
            ParamSpec("level:v", "Level", "enum", default="4.1",
                      options=tuple((v, v) for v in
                                    ("3.0", "3.1", "3.2", "4.0", "4.1", "4.2", "5.0", "5.1", "5.2")),
                      tooltip="Level H.264 : limite résolution/framerate/bitrate.\n"
                              "• 4.1 : 1080p30 — Blu-ray standard.\n"
                              "• 4.2 : 1080p60.\n"
                              "• 5.1 : 4K 30p.\n"
                              "• 5.2 : 4K 60p (rare en H.264)."),
        )),
        ParamGroup("GOP / B-frames", (
            ParamSpec("g", "GOP size", "int", default=250, minimum=1, maximum=600,
                      tooltip="Distance maximale entre keyframes.\n"
                              "Typique : 10× framerate. Plus court = meilleur seek, plus long = "
                              "compression accrue."),
            ParamSpec("bf", "B-frames", "int", default=3, minimum=0, maximum=16,
                      tooltip="Nombre maximum de B-frames consécutives.\n"
                              "3 = bon défaut H.264. Limite pratique ~4 (gain marginal au-delà)."),
            ParamSpec("refs", "References", "int", default=3, minimum=1, maximum=16,
                      tooltip="Nombre de frames de référence pour la prédiction inter-image.\n"
                              "Plus = meilleure qualité sur scènes complexes mais encodage plus lent.\n"
                              "Limité par le level (Level 4.1 = 4 refs max à 1080p)."),
        )),
        ParamGroup("x264-params", (
            ParamSpec("__x264_params__", "x264-params (key=val:key=val)", "text", default="",
                      tooltip="Passthrough -x264-params : tokens internes au moteur x264 séparés par ':'.\n"
                              "Ex utiles :\n"
                              "• aq-mode=3 : adaptive quantization avec bias zones sombres.\n"
                              "• psy-rd=1.0,0.15 : préservation perceptuelle.\n"
                              "• rc-lookahead=60 : lookahead profond.\n"
                              "• me=umh : motion estimation qualité."),
        )),
        ParamGroup("Avancé / libre", (
            ParamSpec("__free__", "Flags libres", "text", default="",
                      tooltip="Tokens ffmpeg additionnels concaténés en fin de commande.\n"
                              "Ex: -coder cabac -bf-strategy 2"),
        )),
    ),
)

# =============================================================================
# NVEncC (rigaya) — wrapper standalone NVIDIA NVENC
# =============================================================================
# Style de sérialisation: ``nvencc_flags`` (long-form ``--key value`` au lieu
# du ``-key value`` ffmpeg). Les booléens utilisent :
#   - bool_repr=(None, "")            → flag seul si activé (ex: ``--aq``)
#   - bool_repr=("--no-X", "--X")     → variantes paire ON/OFF (ex: ``--deblock`` / ``--no-deblock``)
# Les paramètres VPP ``--vpp-*`` sont saisis en mode ``text`` (chaîne composée
# ``param=val,param=val``) car leur ergonomie mérite une UI custom future.

# -------- Constantes communes ------------------------------------------------

_NVENCC_PRESETS = (("default", "default"), ("performance", "performance"),
                   ("quality", "quality"),
                   ("P1", "P1"), ("P2", "P2"), ("P3", "P3"), ("P4", "P4 (défaut)"),
                   ("P5", "P5"), ("P6", "P6"), ("P7", "P7"))
_NVENCC_TUNES = (("hq", "hq — high quality"), ("uhq", "uhq — ultra high quality"),
                 ("lowlatency", "lowlatency"), ("ultralowlatency", "ultralowlatency"),
                 ("lossless", "lossless"))
_NVENCC_MULTIPASS = (("none", "none (1 passe)"),
                     ("2pass-quarter", "2pass-quarter (quart de résolution)"),
                     ("2pass-full", "2pass-full (pleine résolution)"))
_NVENCC_BREF = (("auto", "auto"), ("disabled", "disabled"),
                ("each", "each"), ("middle", "middle"))
_NVENCC_MV_PRECISION = (("auto", "auto"), ("Q-pel", "Q-pel"),
                        ("half-pel", "half-pel"), ("full-pel", "full-pel"))
_NVENCC_OUTPUT_DEPTH = (("8", "8-bit"), ("10", "10-bit"))
_NVENCC_OUTPUT_CSP = (("yuv420", "yuv420"), ("yuv422", "yuv422"),
                      ("yuv444", "yuv444"), ("rgb", "rgb"), ("yuva420", "yuva420"))
_NVENCC_COLORMATRIX = tuple((v, v) for v in (
    "undef", "auto", "bt709", "smpte170m", "bt470bg", "smpte240m",
    "YCgCo", "fcc", "GBR", "bt2020nc", "bt2020c"))
_NVENCC_COLORPRIM = tuple((v, v) for v in (
    "undef", "auto", "bt709", "smpte170m", "bt470m", "bt470bg",
    "smpte240m", "film", "bt2020"))
_NVENCC_TRANSFER = tuple((v, v) for v in (
    "undef", "auto", "bt709", "smpte170m", "bt470m", "bt470bg", "smpte240m",
    "linear", "log100", "log316", "iec61966-2-4", "bt1361e", "iec61966-2-1",
    "bt2020-10", "bt2020-12", "smpte2084", "smpte428", "arib-std-b67"))
_NVENCC_COLORRANGE = (("limited", "limited"), ("full", "full"), ("auto", "auto"))
_NVENCC_VIDEOFORMAT = tuple((v, v) for v in
                            ("undef", "ntsc", "component", "pal", "secam", "mac"))
_NVENCC_DOLBY_VISION_PROFILE = tuple((v, v) for v in (
    "unset", "copy", "5.0", "8.1", "8.2", "8.4", "10.0", "10.1", "10.2", "10.4"))
_NVENCC_VPP_DEINTERLACE = (("none", "none"), ("normal", "normal"),
                           ("adaptive", "adaptive"), ("bob", "bob"))
_NVENCC_VPP_RESIZE = tuple((v, v) for v in (
    "lanczos", "spline16", "spline36", "spline64", "nearest", "bilinear"))
_NVENCC_VPP_ROTATE = (("90", "90°"), ("180", "180°"), ("270", "270°"))
_NVENCC_CUDA_SCHEDULE = (("auto", "auto"), ("spin", "spin"),
                         ("yield", "yield"), ("blocking", "blocking"))

# Niveaux par codec
_NVENCC_LEVEL_HEVC = tuple((v, v) for v in (
    "auto", "1", "2", "2.1", "3", "3.1", "4", "4.1", "5", "5.1", "5.2",
    "6", "6.1", "6.2"))
_NVENCC_LEVEL_H264 = tuple((v, v) for v in (
    "auto", "1", "1b", "1.1", "1.2", "1.3", "2", "2.1", "2.2",
    "3", "3.1", "3.2", "4", "4.1", "4.2", "5", "5.1", "5.2"))
_NVENCC_LEVEL_AV1 = tuple((v, v) for v in (
    "auto", "2", "2.1", "3", "3.1", "4", "4.1",
    "5", "5.1", "5.2", "5.3", "6", "6.1"))

# Profiles par codec
_NVENCC_PROFILE_HEVC = (("auto", "auto"), ("main", "main"),
                        ("main10", "main10 (10-bit)"), ("main444", "main444"))
_NVENCC_PROFILE_H264 = (("auto", "auto"), ("baseline", "baseline"), ("main", "main"),
                        ("high", "high"), ("high10", "high10"), ("high422", "high422"),
                        ("high444", "high444"))
_NVENCC_PROFILE_AV1 = (("auto", "auto"), ("main", "main"), ("high", "high"))


# -------- Helpers : groupes communs ------------------------------------------
# Beaucoup de groupes (Rate Control, GOP, AQ&Tune, HDR, VPP-*, Device, Quality
# metrics, Free) sont identiques entre les 3 codecs. On les construit via
# helpers pour éviter la duplication.

def _nvencc_group_rate_control() -> ParamGroup:
    return ParamGroup("Rate control", (
        # Modes RC mutuellement exclusifs : l'utilisateur choisit l'un d'eux.
        ParamSpec("cqp", "CQP (constant QP, I:P:B)", "text", default="20:23:25",
                  tooltip="Mode CQP — qualité constante. Format ``I:P:B`` ou ``X`` (auto-expansé en ``X:X:X``).\n"
                          "Référence absolue de qualité, pas de cible bitrate.\n"
                          "Typique remux UHD : 18:20:22 (très haute qualité)."),
        ParamSpec("vbr", "VBR (kbps)", "int", minimum=0, maximum=400000, suffix=" kbps",
                  tooltip="Mode VBR — bitrate moyen cible (kbps).\n"
                          "Combinaison usuelle : --vbr <bitrate> + --max-bitrate (1.5-2×) + --vbv-bufsize."),
        ParamSpec("vbrhq", "VBR-HQ (kbps)", "int", minimum=0, maximum=400000, suffix=" kbps",
                  tooltip="VBR haute qualité (NVEncC). Bitrate moyen cible avec moteur RC plus précis."),
        ParamSpec("cbr", "CBR (kbps)", "int", minimum=0, maximum=400000, suffix=" kbps",
                  tooltip="Mode CBR — bitrate constant. Utile pour streaming live et HW décodeurs limités."),
        ParamSpec("cbrhq", "CBR-HQ (kbps)", "int", minimum=0, maximum=400000, suffix=" kbps",
                  tooltip="CBR haute qualité avec moteur RC NVEncC plus précis."),
        ParamSpec("qvbr", "QVBR (qualité 0-51)", "float", minimum=0.0, maximum=51.0,
                  default=24.0, step=0.5,
                  tooltip="QVBR — quality-defined VBR (mode défaut NVEncC).\n"
                          "0 = auto, 1 = qualité maximale, 51 = qualité minimale.\n"
                          "Typique 22-26 pour HEVC HQ."),
        ParamSpec("vbr-quality", "VBR quality (0-51)", "float", minimum=0.0, maximum=51.0,
                  default=24.0, step=0.5,
                  tooltip="Cible qualité VBR (associée à --vbr ou --vbrhq)."),
        ParamSpec("max-bitrate", "Max bitrate (kbps)", "int", minimum=0, maximum=400000,
                  suffix=" kbps",
                  tooltip="Plafond instantané du débit. Typique 1.5-2× le bitrate moyen pour absorber les pics."),
        ParamSpec("vbv-bufsize", "VBV buffer (kbps)", "int", minimum=0, maximum=800000,
                  suffix=" kbps",
                  tooltip="Taille du buffer VBV. Typiquement 2× max-bitrate.\n"
                          "Plus grand = plus de souplesse pour les pics, latence de décodage accrue."),
        ParamSpec("multipass", "Multipass", "enum", default="none", options=_NVENCC_MULTIPASS,
                  tooltip="Encodage multi-passes pour mieux distribuer le bitrate.\n"
                          "• 2pass-full : qualité maximale (~30% plus lent). Requiert Turing+."),
        ParamSpec("lossless", "Lossless", "bool", bool_repr=(None, ""),
                  tooltip="Encodage sans perte (taille de fichier énorme).\n"
                          "Réservé à l'archivage / mastering ; incompatible avec la majorité des modes RC."),
        ParamSpec("qp-init", "QP init (I:P:B)", "text", default="20:23:25",
                  tooltip="QP initial des frames I/P/B en mode CQP.\n"
                          "Format ``I:P:B`` ou ``X`` (auto-expansé en ``X:X:X``)."),
        ParamSpec("qp-min", "QP min (I:P:B)", "text", default="0:0:0",
                  tooltip="Plancher QP.\n"
                          "Format ``I:P:B`` ou ``X`` (auto-expansé en ``X:X:X``)."),
        ParamSpec("qp-max", "QP max (I:P:B)", "text", default="51:51:51",
                  tooltip="Plafond QP.\n"
                          "Format ``I:P:B`` ou ``X`` (auto-expansé en ``X:X:X``)."),
        ParamSpec("chroma-qp-offset", "Chroma QP offset", "int", default=0,
                  minimum=-12, maximum=12,
                  tooltip="Décalage QP appliqué aux plans chroma vs luma.\n"
                          "Négatif = chroma plus précis (utile pour préserver les couleurs saturées)."),
        ParamSpec("dynamic-rc", "Dynamic RC", "text",
                  tooltip="Change le mode RC sur des plages de frames spécifiques.\n"
                          "Format avancé : ``<start>:<end>,param=val[,...]`` (cf. doc NVEncC)."),
    ))


def _nvencc_group_frame_gop(*, with_multiref: bool) -> ParamGroup:
    """Frame structure / GOP. ``with_multiref`` désactive l-list pour AV1."""
    params: list[ParamSpec] = [
        ParamSpec("bframes", "B-frames", "int", default=3, minimum=0, maximum=16,
                  tooltip="Nombre maximum de B-frames consécutives.\n"
                          "HEVC : jusqu'à 8. AV1 : jusqu'à 7. H.264 : jusqu'à 4."),
        ParamSpec("bref-mode", "B-ref mode", "enum", default="auto", options=_NVENCC_BREF,
                  tooltip="Permet aux B-frames de servir de référence.\n"
                          "• middle : seule la B du milieu est référence — meilleur compromis qualité.\n"
                          "Requiert Turing+."),
        ParamSpec("ref", "Reference frames", "int", default=3, minimum=1, maximum=16,
                  tooltip="Nombre de frames de référence pour la prédiction inter-image.\n"
                          "Plus = meilleure qualité scènes complexes, encodage plus lent."),
    ]
    if with_multiref:
        params.extend([
            ParamSpec("multiref-l0", "MultiRef L0", "int", minimum=0, maximum=7,
                      tooltip="Nombre max de frames de référence dans la liste L0 (HEVC/H.264 only)."),
            ParamSpec("multiref-l1", "MultiRef L1", "int", minimum=0, maximum=7,
                      tooltip="Nombre max de frames de référence dans la liste L1 (HEVC/H.264 only)."),
        ])
    params.extend([
        ParamSpec("gop-len", "GOP length", "int", default=250, minimum=1, maximum=600,
                  tooltip="Distance maximale entre keyframes.\n"
                          "Typique : 10× framerate. 250 pour 25fps, 240 pour 24p."),
        ParamSpec("strict-gop", "Strict GOP", "bool", bool_repr=(None, ""),
                  tooltip="Force une structure GOP strictement régulière.\n"
                          "Désactive la détection de scenecut. Utile pour ABR streaming."),
        ParamSpec("no-i-adapt", "Disable adaptive I-frame", "bool", bool_repr=(None, ""),
                  tooltip="Désactive l'insertion adaptative de keyframes sur scenecut."),
        ParamSpec("no-b-adapt", "Disable adaptive B-frame", "bool", bool_repr=(None, ""),
                  tooltip="Désactive l'insertion adaptative de B-frames."),
        ParamSpec("lookahead", "Lookahead (frames)", "int", default=16, minimum=0, maximum=32,
                  tooltip="Frames analysées en avance pour optimiser placement B-frames et QP.\n"
                          "16-32 = bon compromis. 0 désactive (perte qualité notable)."),
        ParamSpec("lookahead-level", "Lookahead level", "int", default=0, minimum=0, maximum=3,
                  tooltip="Profondeur d'analyse du lookahead.\n"
                          "0 = standard. 3 = analyse plus poussée (plus lent)."),
        ParamSpec("weightp", "Weighted P", "bool", bool_repr=(None, ""),
                  tooltip="Prédiction pondérée — améliore compression sur fondus, transitions de luminosité.\n"
                          "Requiert Pascal+."),
        ParamSpec("nonrefp", "Non-ref P", "bool", bool_repr=(None, ""),
                  tooltip="Active automatiquement les P-frames non-référencées.\n"
                          "Économise du bitrate au prix d'une légère perte qualité."),
        ParamSpec("unidirectb", "Uni-directional B", "bool", bool_repr=(None, ""),
                  tooltip="Force des B-frames uni-directionnelles (référence seule en avant)."),
        ParamSpec("mv-precision", "MV precision", "enum", default="auto",
                  options=_NVENCC_MV_PRECISION,
                  tooltip="Précision des vecteurs de mouvement.\n"
                          "Q-pel = quart de pixel (qualité), full-pel = pixel entier (rapide)."),
    ])
    return ParamGroup("Frame structure / GOP", tuple(params))


def _nvencc_group_aq_tune() -> ParamGroup:
    return ParamGroup("Adaptive Quant. & Tune", (
        ParamSpec("preset", "Preset", "enum", default="P4", options=_NVENCC_PRESETS,
                  tooltip="Preset NVEncC.\n"
                          "• default/performance/quality : presets historiques.\n"
                          "• P1 (rapide) → P7 (qualité max). P4 = défaut équilibré.\n"
                          "Note : ``--preset`` est aussi positionnel (-u) ; ce flag long-form fonctionne."),
        ParamSpec("tune", "Tune", "enum", default="hq", options=_NVENCC_TUNES,
                  tooltip="Profil d'usage de l'encodeur.\n"
                          "• hq/uhq : qualité maximale (offline) — recommandé.\n"
                          "• lowlatency/ultralowlatency : streaming.\n"
                          "• lossless : sans perte."),
        ParamSpec("aq", "Spatial AQ", "bool", bool_repr=(None, ""),
                  tooltip="Adaptive Quantization spatial : alloue plus de bits aux zones lisses où "
                          "la compression est visible (ciels, peau).\n"
                          "Améliore notablement la qualité perçue. Recommandé toujours actif."),
        ParamSpec("aq-temporal", "Temporal AQ", "bool", bool_repr=(None, ""),
                  tooltip="AQ temporel : propage la qualité dans le temps en privilégiant les frames "
                          "qui servent de référence.\n"
                          "Requiert Turing+. Gain ~5-10%."),
        ParamSpec("aq-strength", "AQ strength", "int", default=0, minimum=0, maximum=15,
                  tooltip="Intensité de l'AQ (1-15, 0=auto).\n"
                          "8 = défaut équilibré. 12+ peut introduire du flou sur détails fins."),
    ))


def _nvencc_group_hdr() -> ParamGroup:
    """Couleurs + HDR statique/dynamique + Dolby Vision (HEVC/AV1 only)."""
    return ParamGroup("HDR / Color", (
        ParamSpec("colorrange", "Color range", "enum", default="auto",
                  options=_NVENCC_COLORRANGE,
                  tooltip="• limited : 16-235 (broadcast standard).\n"
                          "• full : 0-255 (PC, certains streams)."),
        ParamSpec("videoformat", "Video format", "enum", default="undef",
                  options=_NVENCC_VIDEOFORMAT,
                  tooltip="Format source pour le signaling SEI."),
        ParamSpec("atc-sei", "ATC SEI", "text",
                  tooltip="Alternative transfer characteristics SEI (HEVC) — int ou nom de transfer."),
        ParamSpec("dolby-vision-rpu-prm", "DoVi RPU params", "text", default="crop=true",
                  tooltip="Paramètres RPU DoVi. Ex : ``crop=true`` pour préserver le crop L5 actif."),
    ))


def _nvencc_group_sei() -> ParamGroup:
    return ParamGroup("SEI / Framing", (
        ParamSpec("aud", "Insert AUD", "bool", bool_repr=(None, ""),
                  tooltip="Insert Access Unit Delimiter (HEVC/H.264).\n"
                          "Requis pour certains players Blu-ray et muxers TS."),
        ParamSpec("repeat-headers", "Repeat headers", "bool", bool_repr=(None, ""),
                  tooltip="Output VPS/SPS/PPS pour chaque IDR.\n"
                          "Requis pour streaming/seek arbitraire."),
        ParamSpec("pic-struct", "Pic struct SEI", "bool", bool_repr=(None, ""),
                  tooltip="Insert Picture Timing SEI."),
        ParamSpec("temporal-layers", "Temporal layers", "int", minimum=0, maximum=4,
                  tooltip="Nombre de couches temporelles (hierarchical coding).\n"
                          "0 = désactivé. Utile pour streaming adaptatif."),
    ))


def _nvencc_group_vpp_resize() -> ParamGroup:
    return ParamGroup("VPP — Resize / Géométrie", (
        ParamSpec("vpp-resize", "Resize algo", "enum", default="lanczos",
                  options=_NVENCC_VPP_RESIZE,
                  tooltip="Algorithme de redimensionnement.\n"
                          "• lanczos : qualité haute, défaut recommandé.\n"
                          "• spline36/64 : alternatives haute qualité.\n"
                          "• bilinear/nearest : rapide, qualité moindre.\n"
                          "Combiner avec --output-res pour cibler une résolution."),
        ParamSpec("vpp-rotate", "Rotation", "enum", default="90",
                  options=_NVENCC_VPP_ROTATE,
                  tooltip="Rotation de la vidéo (90/180/270°). Géré GPU."),
        ParamSpec("vpp-transform", "Transform", "text",
                  tooltip="Flip/transpose. Format : ``flip_x=true,flip_y=false,transpose=false``."),
        ParamSpec("vpp-pad", "Pad (L,T,R,B)", "text",
                  tooltip="Ajoute des bandes noires. Format : ``<L>,<T>,<R>,<B>`` en pixels."),
        ParamSpec("crop", "Crop (L,T,R,B)", "text",
                  tooltip="Découpe en pixels.\n"
                          "Format : ``<L>,<T>,<R>,<B>`` (pixels à retirer de chaque bord).\n"
                          "Ex: ``0,140,0,140`` pour retirer 140px haut/bas (passage 16:9 → 2.40)."),
    ))


def _nvencc_group_vpp_deinterlace() -> ParamGroup:
    return ParamGroup("VPP — Deinterlace / Frame ops", (
        ParamSpec("vpp-deinterlace", "Deinterlace", "enum", default="none",
                  options=_NVENCC_VPP_DEINTERLACE,
                  tooltip="Désentrelacement basique. Pour la qualité maximale, utiliser --vpp-nnedi ou --vpp-yadif."),
        ParamSpec("vpp-afs", "AFS (auto field shift)", "text",
                  tooltip="Auto Field Shift — désentrelaceur avancé pour sources NTSC/film telecine.\n"
                          "Format : ``preset=24fps,...``. Convertit télécine → 24p natif."),
        ParamSpec("vpp-yadif", "Yadif", "text",
                  tooltip="Désentrelaceur classique. Format : ``mode=auto`` (auto/tff/bff/bob/bob_tff/bob_bff)."),
        ParamSpec("vpp-nnedi", "NNEDI (neural net)", "text",
                  tooltip="Désentrelaceur neuronal — qualité maximale.\n"
                          "Format : ``nns=64,quality=slow,field=auto``. nns ∈ {16,32,64,128,256}."),
        ParamSpec("vpp-decomb", "Decomb", "text",
                  tooltip="Désentrelacement adaptatif. Format : ``full=true,threshold=10,blend=false``."),
        ParamSpec("vpp-decimate", "Decimate", "text",
                  tooltip="Drop des frames dupliquées (cadence fixe). Format : ``cycle=5,drop=1``."),
        ParamSpec("vpp-mpdecimate", "MP Decimate", "text",
                  tooltip="Drop des duplicats consécutifs (VFR). Format : ``hi=768,lo=320,frac=0.33``."),
        ParamSpec("vpp-select-every", "Select every", "text",
                  tooltip="Garde 1 frame sur N. Format : ``<N>,offset=0``. Ex: ``3,offset=1``."),
        ParamSpec("vpp-rff", "RFF flag", "bool", bool_repr=(None, ""),
                  tooltip="Honore le flag Repeat Field Flag du stream source."),
    ))


def _nvencc_group_vpp_denoise() -> ParamGroup:
    return ParamGroup("VPP — Denoise", (
        ParamSpec("vpp-knn", "KNN denoise", "text",
                  tooltip="K-nearest neighbors denoising — rapide, GPU.\n"
                          "Format : ``radius=4,strength=0.08,lerp=0.2``. strength ∈ [0,1]."),
        ParamSpec("vpp-pmd", "PMD denoise", "text",
                  tooltip="Piecewise Directional Motion denoiser.\n"
                          "Format : ``apply_count=2,strength=100,threshold=100``."),
        ParamSpec("vpp-nlmeans", "NLMeans denoise", "text",
                  tooltip="Non-local means — qualité haute, lent.\n"
                          "Format : ``h=0.05,patch=5,research=11``."),
        ParamSpec("vpp-denoise-dct", "DCT denoise", "text",
                  tooltip="Denoising DCT-based.\n"
                          "Format : ``sigma=4,block_size=8,quality=high``."),
        ParamSpec("vpp-fft3d", "FFT 3D denoise", "text",
                  tooltip="FFT 3D denoiser — qualité très haute, temporel.\n"
                          "Format : ``sigma=4,block_size=32,prev_frames=2,next_frames=2``."),
        ParamSpec("vpp-gauss", "Gaussian blur", "int", default=0, minimum=0, maximum=100,
                  tooltip="Flou gaussien (0-100). À utiliser avec parcimonie (perte de détail)."),
        ParamSpec("vpp-smooth", "Smooth", "text",
                  tooltip="Smoothing filter. Format : ``quality=3,strength=4``."),
        ParamSpec("vpp-convolution3d", "Convolution3D", "text",
                  tooltip="Convolution 3D denoiser. Format : ``matrix=standard,fast=true,ythresh=3``."),
        ParamSpec("vpp-nvvfx-denoise", "NVVFX denoise", "text",
                  tooltip="NVIDIA MAXINE webcam denoising. Format : ``strength=1.0`` (0..1).\n"
                          "Requiert SDK NVVFX installé."),
        ParamSpec("vpp-nvvfx-artifact-reduction", "NVVFX artifact reduction", "bool",
                  bool_repr=(None, ""),
                  tooltip="Réduction d'artefacts via NVVFX (sources lossy basse qualité)."),
    ))


def _nvencc_group_vpp_sharpen() -> ParamGroup:
    return ParamGroup("VPP — Sharpen / Enhance", (
        ParamSpec("vpp-unsharp", "Unsharp mask", "text",
                  tooltip="Masque de netteté.\n"
                          "Format : ``radius=3,weight=0.5,threshold=0.05``."),
        ParamSpec("vpp-edgelevel", "Edge level", "text",
                  tooltip="Renforcement des contours.\n"
                          "Format : ``strength=5,threshold=20,black=0,white=0``."),
        ParamSpec("vpp-warpsharp", "Warp sharp", "text",
                  tooltip="Warp-based sharpening (préserve les bords). Format avancé, voir doc NVEncC."),
        ParamSpec("vpp-msharpen", "M Sharpen", "text",
                  tooltip="Motion-aware sharpening. Format : ``threshold=15,strength=85``."),
    ))


def _nvencc_group_vpp_color() -> ParamGroup:
    return ParamGroup("VPP — Color / Tonemap", (
        ParamSpec("vpp-tweak", "Color tweak", "text",
                  tooltip="Réglages basiques.\n"
                          "Format : ``brightness=0.0,contrast=1.0,saturation=1.0,hue=0.0,gamma=1.0``."),
        ParamSpec("vpp-curves", "Color curves", "text",
                  tooltip="Courbes tonales par canal. Format avancé, voir doc NVEncC."),
        ParamSpec("vpp-deband", "Deband", "text",
                  tooltip="Suppression du banding (gradients postérisés).\n"
                          "Format : ``range=15,threshold=15,dither=true``."),
        ParamSpec("vpp-libplacebo-deband", "libplacebo deband", "text",
                  tooltip="Debanding libplacebo (qualité supérieure)."),
    ))


def _nvencc_group_vpp_logo_overlay() -> ParamGroup:
    return ParamGroup("VPP — Logo / Overlay / Subs", (
        ParamSpec("vpp-delogo", "Delogo", "text",
                  tooltip="Suppression de logo.\n"
                          "Format : ``<logofile.lgd>,select=0,depth=0,pos=0:0`` (fichier .lgd requis)."),
        ParamSpec("vpp-overlay", "Overlay", "text",
                  tooltip="Incrustation image/vidéo.\n"
                          "Format : ``file=<path>,x=0,y=0,alpha=1.0``."),
        ParamSpec("vpp-subburn", "Sub burn", "text",
                  tooltip="Burn-in des sous-titres dans l'image.\n"
                          "Format : ``track=1`` (track interne) ou ``filename=<srt/ass>``."),
    ))


def _nvencc_group_vpp_advanced() -> ParamGroup:
    return ParamGroup("VPP — Avancé / NVIDIA", (
        ParamSpec("vpp-libplacebo-shader", "libplacebo shader", "text",
                  tooltip="Custom GLSL shader via libplacebo (chemin .glsl)."),
        ParamSpec("vpp-fruc", "FRUC (frame rate up-conv)", "text",
                  tooltip="Frame Rate Up-Conversion (NVIDIA Optical Flow).\n"
                          "Format : ``mode=0`` (modes 0..N selon GPU). Ex: 30→60 fps."),
        ParamSpec("vpp-ngx-truehdr", "NGX TrueHDR (SDR→HDR)", "bool", bool_repr=(None, ""),
                  tooltip="NVIDIA NGX TrueHDR — convertit SDR en HDR par IA.\n"
                          "Requiert RTX 20xx+ et SDK NGX."),
        ParamSpec("vpp-perf-monitor", "Perf monitor", "bool", bool_repr=(None, ""),
                  tooltip="Affiche les métriques de performance des filtres VPP."),
        ParamSpec("vpp-nvvfx-model-dir", "NVVFX model dir", "text",
                  tooltip="Chemin vers les modèles NVVFX (SDK NVIDIA MAXINE)."),
    ))


def _nvencc_group_device() -> ParamGroup:
    return ParamGroup("Device / CUDA", (
        ParamSpec("device", "GPU device", "int", default=0, minimum=0, maximum=8,
                  tooltip="ID du GPU à utiliser (0 = premier). Cf. NVEncC --check-device."),
        ParamSpec("cuda-schedule", "CUDA schedule", "enum", default="auto",
                  options=_NVENCC_CUDA_SCHEDULE,
                  tooltip="Mode de scheduling CUDA.\n"
                          "• auto : laisse le driver choisir.\n"
                          "• spin : actif (latence basse, CPU élevé).\n"
                          "• yield/blocking : économe en CPU."),
        ParamSpec("cuda-stream", "CUDA streams", "int", minimum=1, maximum=8,
                  tooltip="Nombre de streams CUDA pour parallélisme intra-GPU."),
        ParamSpec("cuda-mt", "CUDA multi-thread", "int", default=0, minimum=0, maximum=1,
                  tooltip="Multi-threading CUDA. 0 = off, 1 = on."),
        ParamSpec("disable-nvml", "Disable NVML", "bool", bool_repr=(None, ""),
                  tooltip="Désactive NVIDIA Management Library (workaround driver bugs)."),
        ParamSpec("parallel", "Parallel", "text",
                  tooltip="Encodage parallèle de plusieurs segments.\n"
                          "Valeurs : ``auto`` ou un entier.\n"
                          "``all`` est un ancien alias accepté puis canonisé en ``auto``."),
        ParamSpec("split-enc", "Split enc", "enum", default="disable",
                  options=_NVENCC_SPLIT_ENC,
                  tooltip="Frame-split encoding NVENC.\n"
                          "Valeurs : ``disable``, ``auto``, ``auto_forced``, ``forced_2``, "
                          "``forced_3``, ``forced_4``.\n"
                          "À utiliser à la place de ``parallel`` ; les deux modes sont exclusifs."),
        ParamSpec("lowlatency", "Low latency", "bool", bool_repr=(None, ""),
                  tooltip="Mode faible latence pour streaming live."),
        ParamSpec("max-procfps", "Max process FPS", "int", minimum=0, maximum=240,
                  tooltip="Plafond de vitesse de traitement (frames/sec). 0 = illimité."),
        ParamSpec("fallback-bitdepth", "Fallback bitdepth", "bool", bool_repr=(None, ""),
                  tooltip="Fallback automatique si le GPU ne supporte pas la profondeur demandée."),
    ))


def _nvencc_group_quality_metrics() -> ParamGroup:
    return ParamGroup("Quality metrics", (
        ParamSpec("ssim", "SSIM", "bool", bool_repr=(None, ""),
                  tooltip="Calcule SSIM en fin d'encodage. Pour benchmark."),
        ParamSpec("psnr", "PSNR", "bool", bool_repr=(None, ""),
                  tooltip="Calcule PSNR en fin d'encodage. Pour benchmark."),
        ParamSpec("vmaf", "VMAF", "text",
                  tooltip="Calcule VMAF.\n"
                          "Format : ``model=vmaf_v0.6.1,threads=4,subsample=1,phone_model=false``."),
    ))


def _nvencc_group_free() -> ParamGroup:
    return ParamGroup("Avancé / libre", (
        ParamSpec("__free__", "Flags libres (ajoutés tel quel)", "text", default="",
                  tooltip="Tokens NVEncC additionnels concaténés en fin de commande.\n"
                          "Ex: ``--option-file <path>``, ``--input-option key=val``, "
                          "``--mux-option opt:val``, ``--avsync vfr``, etc."),
    ))


# -------- Schémas codec ------------------------------------------------------

def _nvencc_group_codec_output(*, profile_options, level_options,
                               with_tier: bool) -> ParamGroup:
    params: list[ParamSpec] = [
        ParamSpec("profile", "Profile", "enum",
                  default=profile_options[0][0], options=profile_options,
                  tooltip="Profil du codec — détermine les outils autorisés et la "
                          "compatibilité décodeurs."),
    ]
    if with_tier:
        params.append(
            ParamSpec("tier", "Tier", "enum", default="main",
                      options=(("main", "main"), ("high", "high")),
                      tooltip="Tier HEVC.\n"
                              "• main : bitrate plafonné selon le level (consumer).\n"
                              "• high : bitrate étendu — requis pour UHD HDR haut bitrate."))
    params.extend([
        ParamSpec("level", "Level", "enum", default="auto", options=level_options,
                  tooltip="Level du codec — limite résolution/framerate/bitrate.\n"
                          "auto = déduit du contenu. 5.1 = 4K@60p (HEVC) / 4K@30p (H.264)."),
        ParamSpec("output-depth", "Output depth", "enum", default="8",
                  options=_NVENCC_OUTPUT_DEPTH,
                  tooltip="Profondeur de bit de sortie. 10 = obligatoire pour HDR."),
        ParamSpec("output-csp", "Output color space", "enum", default="yuv420",
                  options=_NVENCC_OUTPUT_CSP,
                  tooltip="Espace couleur de sortie. yuv420 = standard ; yuv444/rgb = pro."),
        ParamSpec("output-res", "Output resolution", "text",
                  tooltip="Résolution de sortie.\n"
                          "Format : ``WxH`` (ex: ``1920x1080``) ou ``0x-2`` (préserve aspect ratio)."),
        ParamSpec("sar", "SAR (W:H)", "text",
                  tooltip="Sample Aspect Ratio. Ex: ``1:1`` (square), ``40:33`` (PAL anamorphique)."),
        ParamSpec("dar", "DAR (W:H)", "text",
                  tooltip="Display Aspect Ratio. Ex: ``16:9``, ``2.40:1``."),
    ])
    return ParamGroup("Codec / Profile / Output", tuple(params))


def _nvencc_group_hevc_specific() -> ParamGroup:
    return ParamGroup("HEVC-specific", (
        ParamSpec("cu-min", "CU min size", "enum", default="8",
                  options=(("8", "8"), ("16", "16"), ("32", "32")),
                  tooltip="Taille minimale des Coding Units HEVC.\n"
                          "Plus petit = meilleure adaptation aux détails fins, encodage plus lent."),
        ParamSpec("cu-max", "CU max size", "enum", default="32",
                  options=(("8", "8"), ("16", "16"), ("32", "32")),
                  tooltip="Taille maximale des Coding Units HEVC."),
        ParamSpec("tf-level", "Temporal filter level", "enum", default="0",
                  options=(("0", "off"), ("4", "level 4")),
                  tooltip="Filtrage temporel HEVC. 0 = off, 4 = activé."),
        ParamSpec("alpha-bitrate-ratio", "Alpha bitrate ratio", "int", minimum=0, maximum=100,
                  tooltip="Ratio bitrate alpha/luma. 0 = auto (alpha channel only)."),
        ParamSpec("alpha-channel-mode", "Alpha mode", "enum", default="straight",
                  options=(("straight", "straight"), ("premultiplied", "premultiplied")),
                  tooltip="Mode du canal alpha (alpha channel only)."),
    ))


def _nvencc_group_h264_specific() -> ParamGroup:
    return ParamGroup("H.264-specific", (
        ParamSpec("direct", "Direct mode", "enum", default="auto",
                  options=(("auto", "auto"), ("disabled", "disabled"),
                           ("spatial", "spatial"), ("temporal", "temporal")),
                  tooltip="Mode direct des B-frames. auto = recommandé."),
        ParamSpec("adapt-transform", "Adaptive transform", "bool",
                  bool_repr=("--no-adapt-transform", "--adapt-transform"),
                  tooltip="Transform 8x8 adaptatif (default ON).\n"
                          "Décocher pour compatibilité décodeurs anciens."),
        ParamSpec("hierarchial-p", "Hierarchical P", "bool", bool_repr=(None, ""),
                  tooltip="Active la structure hiérarchique des P-frames."),
        ParamSpec("hierarchial-b", "Hierarchical B", "bool", bool_repr=(None, ""),
                  tooltip="Active la structure hiérarchique des B-frames."),
        ParamSpec("cabac", "CABAC entropy", "bool", bool_repr=(None, ""),
                  tooltip="Active le codage entropique CABAC (default ON, plus efficace que CAVLC)."),
        ParamSpec("cavlc", "CAVLC entropy", "bool", bool_repr=(None, ""),
                  tooltip="Force CAVLC (compat décodeurs très anciens, qualité réduite)."),
        ParamSpec("bluray", "Blu-ray compat", "bool", bool_repr=(None, ""),
                  tooltip="Force le respect des contraintes Blu-ray."),
        ParamSpec("deblock", "Deblock filter", "bool",
                  bool_repr=("--no-deblock", "--deblock"),
                  tooltip="Filtre de déblocking (default ON).\n"
                          "Désactiver dégrade la qualité visuelle, à utiliser avec parcimonie."),
    ))


def _nvencc_group_av1_specific() -> ParamGroup:
    return ParamGroup("AV1-specific", (
        ParamSpec("part-size-min", "Partition size min", "enum", default="0",
                  options=(("0", "auto"), ("4", "4"), ("8", "8"),
                           ("16", "16"), ("32", "32"), ("64", "64")),
                  tooltip="Taille minimale des partitions AV1."),
        ParamSpec("part-size-max", "Partition size max", "enum", default="0",
                  options=(("0", "auto"), ("4", "4"), ("8", "8"),
                           ("16", "16"), ("32", "32"), ("64", "64")),
                  tooltip="Taille maximale des partitions AV1."),
        ParamSpec("tile-columns", "Tile columns", "enum", default="0",
                  options=(("0", "auto"), ("1", "1"), ("2", "2"), ("4", "4"),
                           ("8", "8"), ("16", "16"), ("32", "32"), ("64", "64")),
                  tooltip="Nombre de colonnes de tiles (parallélisme décodage)."),
        ParamSpec("tile-rows", "Tile rows", "enum", default="0",
                  options=(("0", "auto"), ("1", "1"), ("2", "2"), ("4", "4"),
                           ("8", "8"), ("16", "16"), ("32", "32"), ("64", "64")),
                  tooltip="Nombre de lignes de tiles."),
        ParamSpec("refs-forward", "Refs forward", "int", default=0, minimum=0, maximum=4,
                  tooltip="Nombre max de références forward (0 = auto)."),
        ParamSpec("refs-backward", "Refs backward", "int", default=0, minimum=0, maximum=3,
                  tooltip="Nombre max de références backward (0 = auto)."),
        ParamSpec("bitstream-padding", "Bitstream padding", "bool", bool_repr=(None, ""),
                  tooltip="Active le padding bitstream pour CBR strict."),
    ))


def _nvencc_schema(codec: str, *, codec_group: ParamGroup,
                   specific_group: ParamGroup,
                   include_hdr: bool,
                   with_multiref: bool) -> CodecSchema:
    """Construit un CodecSchema NVEncC complet à partir des groupes communs.

    H.264 a `include_hdr=False` (HDR non pertinent) et `with_multiref=True`.
    AV1 a `with_multiref=False` (multiref-l0/l1 spécifiques HEVC/H.264).
    """
    groups: list[ParamGroup] = [
        codec_group,
        _nvencc_group_rate_control(),
        _nvencc_group_frame_gop(with_multiref=with_multiref),
        _nvencc_group_aq_tune(),
        specific_group,
    ]
    if include_hdr:
        groups.append(_nvencc_group_hdr())
    groups.extend([
        _nvencc_group_sei(),
        _nvencc_group_vpp_resize(),
        _nvencc_group_vpp_deinterlace(),
        _nvencc_group_vpp_denoise(),
        _nvencc_group_vpp_sharpen(),
        _nvencc_group_vpp_color(),
        _nvencc_group_vpp_logo_overlay(),
        _nvencc_group_vpp_advanced(),
        _nvencc_group_device(),
        _nvencc_group_quality_metrics(),
        _nvencc_group_free(),
    ])
    return CodecSchema(codec=codec, style="nvencc_flags", groups=tuple(groups))


_NVENCC_HEVC = _nvencc_schema(
    "nvencc_hevc",
    codec_group=_nvencc_group_codec_output(
        profile_options=_NVENCC_PROFILE_HEVC,
        level_options=_NVENCC_LEVEL_HEVC,
        with_tier=True,
    ),
    specific_group=_nvencc_group_hevc_specific(),
    include_hdr=True,
    with_multiref=True,
)

_NVENCC_H264 = _nvencc_schema(
    "nvencc_h264",
    codec_group=_nvencc_group_codec_output(
        profile_options=_NVENCC_PROFILE_H264,
        level_options=_NVENCC_LEVEL_H264,
        with_tier=False,
    ),
    specific_group=_nvencc_group_h264_specific(),
    include_hdr=False,
    with_multiref=True,
)

_NVENCC_AV1 = _nvencc_schema(
    "nvencc_av1",
    codec_group=_nvencc_group_codec_output(
        profile_options=_NVENCC_PROFILE_AV1,
        level_options=_NVENCC_LEVEL_AV1,
        with_tier=False,
    ),
    specific_group=_nvencc_group_av1_specific(),
    include_hdr=True,
    with_multiref=False,
)


# -------- Catalogue ----------------------------------------------------------

_FFMPEG_SCHEMAS: dict[str, CodecSchema] = {
    "hevc_nvenc": _NVENC_HEVC,
    "h264_nvenc": _NVENC_H264,
    "av1_nvenc": _NVENC_AV1,
    "hevc_amf": _AMF_HEVC,
    "h264_amf": _AMF_H264,
    "av1_amf": _AMF_AV1,
    "hevc_qsv": _QSV_HEVC,
    "h264_qsv": _QSV_H264,
    "av1_qsv": _QSV_AV1,
    "hevc_vaapi": _VAAPI_HEVC,
    "h264_vaapi": _VAAPI_H264,
    "av1_vaapi": _VAAPI_AV1,
    "libx265": _X265_LIBX265,
    "libsvtav1": _LIBSVTAV1,
    "libx264": _LIBX264,
}

_NVENCC_SCHEMAS: dict[str, CodecSchema] = {
    "nvencc_hevc": _NVENCC_HEVC,
    "nvencc_h264": _NVENCC_H264,
    "nvencc_av1": _NVENCC_AV1,
}


def schema_for(codec: str) -> CodecSchema | None:
    normalized = str(codec or "").strip().lower()
    if backend_id_for_codec(normalized) == "nvencc":
        return _NVENCC_SCHEMAS.get(normalized)
    return _FFMPEG_SCHEMAS.get(normalized)


# =============================================================================
# Sérialisation / parsing
# =============================================================================

def _serialize_value(spec: ParamSpec, raw: Any) -> str | None:
    """Convertit la valeur d'un widget en string ffmpeg/x265. Retourne None pour omettre."""
    if spec.kind == "bool":
        # Pour bool : raw est bool. On émet bool_repr[1] si True, [0] si False (sauf si None).
        on_v = spec.bool_repr[1] if spec.bool_repr[1] is not None else ""
        off_v = spec.bool_repr[0]
        return on_v if raw else off_v
    if spec.kind in ("int", "float"):
        return str(raw)
    if spec.kind == "enum":
        return str(raw)
    if spec.kind == "text":
        text = str(raw).strip()
        return text if text else None
    return None


def _serialize(schema: CodecSchema, values: dict[str, tuple[bool, Any]]) -> str:
    """Construit la chaîne de sortie selon le style du codec.

    values : dict {param_key: (enabled, raw_value)}
    """
    free_tokens: list[str] = []
    x264_params_inline = ""

    if schema.style in ("x265_params", "svtav1_params"):
        parts: list[str] = []
        for group in schema.groups:
            for spec in group.params:
                if spec.key == "__free__":
                    enabled, raw = values.get(spec.key, (False, ""))
                    if enabled and str(raw).strip():
                        parts.append(str(raw).strip().strip(":"))
                    continue
                enabled, raw = values.get(spec.key, (False, None))
                if not enabled:
                    continue
                val = _serialize_value(spec, raw)
                if val is None:
                    continue
                if spec.kind == "bool" and spec.bool_repr[1] == "":
                    parts.append(spec.key)   # flag x265 sans valeur
                else:
                    parts.append(f"{spec.key}={val}")
        return ":".join(p for p in parts if p)

    if schema.style == "nvencc_flags":
        # NVEncC : long-form "--key value" + variantes booléennes.
        # bool_repr=(None, "")   → flag seul si activé (ex: --aq, --lossless)
        # bool_repr=("--no-X","--X") → ON/OFF explicites
        tokens_n: list[str] = []
        free_n: list[str] = []
        for group in schema.groups:
            for spec in group.params:
                if spec.key == "__free__":
                    enabled, raw = values.get(spec.key, (False, ""))
                    if enabled and str(raw).strip():
                        free_n.append(str(raw).strip())
                    continue
                enabled, raw = values.get(spec.key, (False, None))
                if not enabled:
                    continue
                if spec.kind == "bool":
                    on_v, off_v = spec.bool_repr[1], spec.bool_repr[0]
                    # Variante "--X" / "--no-X" : émettre la chaîne brute selon raw.
                    if on_v and on_v.startswith("--") and off_v and off_v.startswith("--"):
                        tokens_n.append(on_v if raw else off_v)
                        continue
                    # Flag seul si activé (on_v="" ou None).
                    if raw:
                        tokens_n.append(f"--{spec.key}")
                    continue
                val = _serialize_value(spec, raw)
                if spec.kind == "text":
                    val = _normalize_nvencc_text_value(spec.key, val)
                if val is None:
                    continue
                tokens_n.append(f"--{spec.key}")
                tokens_n.append(val)
        out_n = " ".join(tokens_n)
        if free_n:
            out_n = (out_n + " " + " ".join(free_n)).strip()
        return out_n

    # ffmpeg_flags style — tokens "-key value" séparés par espaces
    tokens: list[str] = []
    for group in schema.groups:
        for spec in group.params:
            if spec.key == "__free__":
                enabled, raw = values.get(spec.key, (False, ""))
                if enabled and str(raw).strip():
                    free_tokens.append(str(raw).strip())
                continue
            if spec.key == "__x264_params__":
                enabled, raw = values.get(spec.key, (False, ""))
                if enabled and str(raw).strip():
                    x264_params_inline = str(raw).strip().strip(":")
                continue
            enabled, raw = values.get(spec.key, (False, None))
            if not enabled:
                continue
            val = _serialize_value(spec, raw)
            if val is None:
                continue
            tokens.append(f"-{spec.key}")
            tokens.append(val)
    out = " ".join(tokens)
    if x264_params_inline:
        out = (out + f' -x264-params "{x264_params_inline}"').strip()
    if free_tokens:
        out = (out + " " + " ".join(free_tokens)).strip()
    return out


def _spec_by_key(schema: CodecSchema) -> dict[str, ParamSpec]:
    return {spec.key: spec for group in schema.groups for spec in group.params}


def _looks_like_number(token: str) -> bool:
    try:
        float(str(token).strip())
    except (TypeError, ValueError):
        return False
    return True


def _parse_existing(schema: CodecSchema, current: str) -> dict[str, tuple[bool, Any]]:
    """Pré-remplit les widgets en parsant la valeur existante.

    Best-effort : si on reconnaît une clé du schéma, on la coche avec sa valeur ;
    sinon on agrège dans __free__.
    """
    values: dict[str, tuple[bool, Any]] = {}
    if not current.strip():
        return values

    spec_map = _spec_by_key(schema)
    spec_keys = set(spec_map)

    if schema.style in ("x265_params", "svtav1_params"):
        leftovers: list[str] = []
        for chunk in current.strip().strip(":").split(":"):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "=" in chunk:
                k, v = chunk.split("=", 1)
                if k in spec_keys:
                    values[k] = (True, v)
                else:
                    leftovers.append(chunk)
            else:
                # flag sans valeur
                if chunk in spec_keys:
                    values[chunk] = (True, True)
                else:
                    leftovers.append(chunk)
        if leftovers:
            values["__free__"] = (True, ":".join(leftovers))
        return values

    if schema.style == "nvencc_flags":
        # Parse une commande NVEncC long-form "--key [value]". Reconnaît :
        #   • --no-key                   → bool OFF (si la spec a bool_repr ("--no-X","--X"))
        #   • --key (seul, prochain token = "--…" ou EOF) → flag seul, bool ON
        #   • --key value                → spec.kind in {int,float,enum,text} OU bool ON
        try:
            tokens = shlex.split(current)
        except ValueError:
            tokens = current.split()
        # Map les variantes --no-X → la spec d'origine (X)
        bool_pairs: dict[str, ParamSpec] = {}
        for grp in schema.groups:
            for sp in grp.params:
                if sp.kind == "bool" and sp.bool_repr[0] and sp.bool_repr[1] and sp.bool_repr[0].startswith("--"):
                    bool_pairs[sp.bool_repr[0]] = sp
                    bool_pairs[sp.bool_repr[1]] = sp
        leftovers = []
        i = 0
        ignored_keys = _NVENCC_IGNORED_PARSE_KEYS if schema.codec.startswith("nvencc_") else frozenset()
        while i < len(tokens):
            tok = tokens[i]
            if not tok.startswith("--"):
                leftovers.append(tok)
                i += 1
                continue
            # Variantes --no-X / --X explicites : on les détecte AVANT la résolution de clé.
            if tok in bool_pairs:
                sp = bool_pairs[tok]
                values[sp.key] = (True, tok == sp.bool_repr[1])
                i += 1
                continue
            if "=" in tok[2:]:
                key, inline_value = tok[2:].split("=", 1)
            else:
                key, inline_value = tok[2:], None
            if key in ignored_keys:
                next_is_value = (inline_value is None and i + 1 < len(tokens) and not tokens[i + 1].startswith("--"))
                i += 2 if next_is_value else 1
                continue
            if key in spec_keys:
                # Spec connue : décider si on consomme un argument suivant.
                next_is_value = (
                    inline_value is None
                    and i + 1 < len(tokens)
                    and not tokens[i + 1].startswith("--")
                )
                # Trouver la spec pour connaître son kind.
                spec_obj = spec_map.get(key)
                if spec_obj is not None and spec_obj.kind == "bool":
                    # bool flag seul (--aq, --lossless) — pas de valeur attendue.
                    values[key] = (True, True)
                    i += 1
                    continue
                if inline_value is not None:
                    values[key] = (True, inline_value)
                    i += 1
                    continue
                if next_is_value:
                    values[key] = (True, tokens[i + 1])
                    i += 2
                    continue
                # Spec attendant une valeur mais aucune fournie → on coche quand même.
                values[key] = (True, True)
                i += 1
                continue
            # Inconnu : pousser dans free.
            leftovers.append(tok)
            i += 1
        if leftovers:
            values["__free__"] = (True, " ".join(leftovers))
        return values

    # ffmpeg_flags
    try:
        tokens = shlex.split(current)
    except ValueError:
        tokens = current.split()
    leftovers = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "-x264-params" and i + 1 < len(tokens):
            values["__x264_params__"] = (True, tokens[i + 1])
            i += 2
            continue
        if tok.startswith("-"):
            key = tok[1:]
            if key in spec_keys:
                spec_obj = spec_map.get(key)
                next_tok = tokens[i + 1] if i + 1 < len(tokens) else None
                if spec_obj is not None and spec_obj.kind == "bool":
                    values[key] = (True, True)
                    i += 1
                    continue
                if next_tok is not None:
                    if not next_tok.startswith("-"):
                        values[key] = (True, next_tok)
                        i += 2
                        continue
                    if spec_obj is not None and spec_obj.kind in ("int", "float") and _looks_like_number(next_tok):
                        values[key] = (True, next_tok)
                        i += 2
                        continue
                values[key] = (True, True)
                i += 1
                continue
        leftovers.append(tok)
        i += 1
    if leftovers:
        values["__free__"] = (True, " ".join(leftovers))
    return values


# =============================================================================
# Infotip : popup immédiate sous la souris au mouseover
# =============================================================================

class _InfotipFilter(QObject):
    """Event filter qui affiche un QToolTip riche dès l'entrée souris.

    Au lieu d'attendre le délai natif (~700 ms), on appelle ``QToolTip.showText``
    sur ``QEvent.Enter`` avec la position du curseur. Le tooltip se ferme à
    ``QEvent.Leave``.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Enter and isinstance(obj, QWidget):
            tip = obj.toolTip()
            if tip:
                pos = QCursor.pos() + QPoint(12, 16)
                QToolTip.showText(pos, tip, obj)
        elif event.type() == QEvent.Type.Leave:
            QToolTip.hideText()
        return False   # ne consomme pas l'événement


# =============================================================================
# Widget par paramètre
# =============================================================================

class _ParamRow(QWidget):
    """Ligne : checkbox d'activation + label + widget de valeur.

    Le label porte un tooltip "infotip" qui s'affiche immédiatement sous la
    souris au mouseover (via _InfotipFilter installé par le dialog parent).
    """

    def __init__(
        self,
        spec: ParamSpec,
        initial: tuple[bool, Any] | None = None,
        infotip_filter: _InfotipFilter | None = None,
    ) -> None:
        super().__init__()
        self._spec = spec
        self._enabled_cb = QCheckBox()
        self._enabled_cb.setStyleSheet(_checkbox_style())
        self._enabled_cb.setToolTip(translate_text("Activer ce paramètre"))

        # Label : nom du param + clé technique en gris à droite, mouseover-friendly
        label_text = translate_text(spec.label)
        lbl = QLabel(f"{label_text}  <span style='color:{_C.TEXT_DIM};font-size:10px;'>{spec.key}</span>"
                     if not spec.key.startswith("__") else label_text)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setStyleSheet(f"color:{_C.TEXT_PRI};font-size:11px;background:transparent;")
        lbl.setMinimumWidth(220)
        lbl.setCursor(Qt.CursorShape.WhatsThisCursor)
        if spec.tooltip:
            tip_translated = translate_text(spec.tooltip)
            lbl.setToolTip(tip_translated)
            self._enabled_cb.setToolTip(tip_translated)
        if infotip_filter is not None:
            lbl.installEventFilter(infotip_filter)
        self._label = lbl

        self._value_widget: QWidget = self._build_value_widget(spec)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(8)
        row.addWidget(self._enabled_cb)
        row.addWidget(lbl)
        row.addWidget(self._value_widget, 1)

        self._enabled_cb.toggled.connect(self._value_widget.setEnabled)
        self._value_widget.setEnabled(False)

        if initial is not None:
            enabled, raw = initial
            self._enabled_cb.setChecked(enabled)
            self._set_widget_value(raw)

    def _build_value_widget(self, spec: ParamSpec) -> QWidget:
        if spec.kind == "int":
            spin_box = QSpinBox()
            spin_box.setRange(int(spec.minimum if spec.minimum is not None else -2**31),
                              int(spec.maximum if spec.maximum is not None else 2**31 - 1))
            if spec.step is not None:
                spin_box.setSingleStep(int(spec.step))
            if spec.default is not None:
                spin_box.setValue(int(spec.default))
            if spec.suffix:
                spin_box.setSuffix(spec.suffix)
            spin_box.setStyleSheet(_input_style())
            return spin_box
        if spec.kind == "float":
            double_spin_box = QDoubleSpinBox()
            double_spin_box.setRange(float(spec.minimum if spec.minimum is not None else -1e9),
                                     float(spec.maximum if spec.maximum is not None else 1e9))
            double_spin_box.setSingleStep(float(spec.step) if spec.step is not None else 0.1)
            double_spin_box.setDecimals(2)
            if spec.default is not None:
                double_spin_box.setValue(float(spec.default))
            if spec.suffix:
                double_spin_box.setSuffix(spec.suffix)
            double_spin_box.setStyleSheet(_input_style())
            return double_spin_box
        if spec.kind == "enum":
            combo_box = QComboBox()
            combo_box.setStyleSheet(_combo_style())
            for value, label in spec.options:
                combo_box.addItem(label, value)
            if spec.default is not None:
                idx = combo_box.findData(spec.default)
                if idx >= 0:
                    combo_box.setCurrentIndex(idx)
            return combo_box
        if spec.kind == "bool":
            check_box = QCheckBox(translate_text("Activé"))
            check_box.setStyleSheet(_checkbox_style())
            if spec.default == "1":
                check_box.setChecked(True)
            return check_box
        # text
        le = QLineEdit()
        le.setStyleSheet(_input_style())
        if spec.default:
            le.setText(str(spec.default))
        if spec.tooltip:
            le.setPlaceholderText(translate_text(spec.tooltip)[:60])
        return le

    def _set_widget_value(self, raw: Any) -> None:
        spec = self._spec
        w = self._value_widget
        if spec.kind == "int" and isinstance(w, QSpinBox):
            try:
                w.setValue(int(raw))
            except (TypeError, ValueError):
                pass
        elif spec.kind == "float" and isinstance(w, QDoubleSpinBox):
            try:
                w.setValue(float(raw))
            except (TypeError, ValueError):
                pass
        elif spec.kind == "enum" and isinstance(w, QComboBox):
            idx = w.findData(str(raw))
            if idx >= 0:
                w.setCurrentIndex(idx)
        elif spec.kind == "bool" and isinstance(w, QCheckBox):
            if isinstance(raw, bool):
                w.setChecked(raw)
            else:
                w.setChecked(str(raw) in ("1", "true", "True", "on"))
        elif spec.kind == "text" and isinstance(w, QLineEdit):
            w.setText(str(raw))

    def value(self) -> tuple[bool, Any]:
        w = self._value_widget
        if isinstance(w, QSpinBox):
            return (self._enabled_cb.isChecked(), w.value())
        if isinstance(w, QDoubleSpinBox):
            return (self._enabled_cb.isChecked(), round(w.value(), 4))
        if isinstance(w, QComboBox):
            return (self._enabled_cb.isChecked(), w.currentData())
        if isinstance(w, QCheckBox):
            return (self._enabled_cb.isChecked(), w.isChecked())
        if isinstance(w, QLineEdit):
            return (self._enabled_cb.isChecked(), w.text())
        return (False, None)


# =============================================================================
# QTabBar custom : tabs à gauche, texte horizontal
# =============================================================================

class _HorizontalTabBar(QTabBar):
    """TabBar vertical (West) avec texte horizontal au lieu du défaut 90°.

    Surcharge tabSizeHint pour fixer la largeur, et paintEvent pour dessiner
    le texte non-tourné.
    """

    def tabSizeHint(self, index: int) -> QSize:
        # Largeur fixe ; hauteur compacte (style menu latéral)
        return QSize(170, 26)

    def paintEvent(self, _event) -> None:   # noqa: D401
        painter = QStylePainter(self)
        opt = QStyleOptionTab()
        for i in range(self.count()):
            self.initStyleOption(opt, i)
            # Dessine le fond/bordure via le style natif (utilise la stylesheet
            # via QStyle.CE_TabBarTabShape) ...
            painter.drawControl(QStyle.ControlElement.CE_TabBarTabShape, opt)
            # ... mais on dessine le label nous-mêmes pour rester horizontal.
            rect = self.tabRect(i)
            text_rect = rect.adjusted(12, 0, -8, 0)
            painter.save()
            painter.setFont(self.font())
            painter.setPen(opt.palette.color(opt.palette.ColorRole.WindowText))
            painter.drawText(
                text_rect,
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                opt.text,
            )
            painter.restore()


class _SidebarTabWidget(QTabWidget):
    """QTabWidget configuré façon menu latéral fin (texte horizontal)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setTabBar(_HorizontalTabBar(self))
        self.setTabPosition(QTabWidget.TabPosition.West)
        self.setDocumentMode(True)


# =============================================================================
# Dialog principal
# =============================================================================

class ExtraParamsDialog(QDialog):
    """Modale d'édition des params avancés pour le codec sélectionné.

    La sortie est récupérable via ``result_text`` après ``exec()``.
    """

    def __init__(self, codec: str, current_value: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(translate_text("Paramètres avancés — {codec}", codec=codec))
        self.setModal(True)
        self.setMinimumSize(820, 540)
        self.setStyleSheet(f"QDialog{{background:{_C.BG_DEEP};}}")

        self._schema = schema_for(codec)
        self._rows: dict[str, _ParamRow] = {}
        self._result_text: str = current_value
        self._infotip_filter = _InfotipFilter(self)
        self._free_only: QPlainTextEdit | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        if self._schema is None:
            msg = QLabel(translate_text(
                "Aucun schéma défini pour <b>{codec}</b>.<br>"
                "Vous pouvez saisir les flags ffmpeg en libre ci-dessous.",
                codec=codec,
            ))
            msg.setStyleSheet(f"color:{_C.TEXT_SEC};background:transparent;")
            outer.addWidget(msg)
            self._free_only = QPlainTextEdit()
            self._free_only.setPlainText(current_value)
            self._free_only.setStyleSheet(_input_style())
            self._free_only.setMinimumHeight(160)
            outer.addWidget(self._free_only, 1)
        else:
            self._free_only = None
            header = QLabel(translate_text(
                "Codec : <b>{codec}</b> · Style : <i>{style}</i>",
                codec=codec, style=self._schema.style,
            ))
            header.setStyleSheet(f"color:{_C.TEXT_SEC};background:transparent;")
            outer.addWidget(header)

            tabs = _SidebarTabWidget()
            tabs.setStyleSheet(self._tabs_stylesheet())

            initial_values = _parse_existing(self._schema, current_value)
            for group in self._schema.groups:
                tabs.addTab(
                    self._build_group_page(group, initial_values),
                    translate_text(group.title),
                )
            self._setup_nvencc_parallel_split_enc_logic()

            outer.addWidget(tabs, 1)

            preview_lbl = QLabel(translate_text("Aperçu de la sortie :"))
            preview_lbl.setStyleSheet(f"color:{_C.TEXT_DIM};font-size:10px;background:transparent;")
            outer.addWidget(preview_lbl)
            self._preview = QPlainTextEdit()
            self._preview.setReadOnly(True)
            self._preview.setMaximumHeight(80)
            self._preview.setStyleSheet(_input_style())
            outer.addWidget(self._preview)

            self._refresh_preview()

        # Boutons
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel = _secondary_button(translate_text("Annuler"))
        cancel.clicked.connect(self.reject)
        ok = _primary_button(translate_text("Valider"))
        ok.clicked.connect(self._accept)
        btn_row.addWidget(cancel)
        btn_row.addWidget(ok)
        outer.addLayout(btn_row)

        apply_translations(self)

    @staticmethod
    def _tabs_stylesheet() -> str:
        # Style "menu latéral" : tabs fines, texte horizontal, accent en barre gauche
        return f"""
        QTabWidget::pane{{
            border:none;border-left:1px solid {_C.BORDER};
            background:{_C.BG_CARD};
        }}
        QTabWidget::tab-bar{{alignment:left;}}
        QTabBar{{background:transparent;qproperty-drawBase:0;outline:none;}}
        QTabBar::tab{{
            background:transparent;color:{_C.TEXT_DIM};
            padding:4px 14px 4px 10px;
            margin:0;
            border:none;border-left:2px solid transparent;
            min-width:150px;
            text-align:left;
            font-size:11px;font-weight:400;
        }}
        QTabBar::tab:hover:!selected{{color:{_C.TEXT_PRI};}}
        QTabBar::tab:selected{{
            color:{_C.TEXT_PRI};
            border-left:2px solid {_C.ACCENT};
            font-weight:600;
        }}
        """

    def _build_group_page(
        self,
        group: ParamGroup,
        initial_values: dict[str, tuple[bool, Any]],
    ) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        inner = QWidget()
        inner.setStyleSheet(f"background:{_C.BG_CARD};")
        v = QVBoxLayout(inner)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(6)
        for spec in group.params:
            row = _ParamRow(
                spec,
                initial=initial_values.get(spec.key),
                infotip_filter=self._infotip_filter,
            )
            self._rows[spec.key] = row
            row.findChild(QCheckBox).toggled.connect(self._refresh_preview)   # type: ignore[union-attr]
            # Connecte aussi le widget de valeur pour live preview
            self._connect_value_changes(row)
            v.addWidget(row)
        v.addStretch(1)
        scroll.setWidget(inner)
        return scroll

    def _connect_value_changes(self, row: _ParamRow) -> None:
        w = row._value_widget   # noqa: SLF001
        if isinstance(w, QSpinBox):
            w.valueChanged.connect(self._refresh_preview)
        elif isinstance(w, QDoubleSpinBox):
            w.valueChanged.connect(self._refresh_preview)
        elif isinstance(w, QComboBox):
            w.currentIndexChanged.connect(self._refresh_preview)
        elif isinstance(w, QCheckBox):
            w.toggled.connect(self._refresh_preview)
        elif isinstance(w, QLineEdit):
            w.textChanged.connect(self._refresh_preview)

    def _setup_nvencc_parallel_split_enc_logic(self) -> None:
        if self._schema is None or not self._schema.codec.startswith("nvencc_"):
            return
        parallel_row = self._rows.get("parallel")
        split_row = self._rows.get("split-enc")
        if parallel_row is None or split_row is None:
            return

        if isinstance(parallel_row._value_widget, QLineEdit):
            parallel_row._value_widget.textChanged.connect(self._on_nvencc_parallel_changed)
        parallel_row._enabled_cb.toggled.connect(self._on_nvencc_parallel_changed)

        if isinstance(split_row._value_widget, QComboBox):
            split_row._value_widget.currentIndexChanged.connect(self._on_nvencc_split_enc_changed)
        split_row._enabled_cb.toggled.connect(self._on_nvencc_split_enc_changed)

        if self._nvencc_split_enc_effective():
            parallel_row._enabled_cb.setChecked(False)

    def _nvencc_parallel_effective(self) -> bool:
        row = self._rows.get("parallel")
        if row is None or not row._enabled_cb.isChecked():
            return False
        if isinstance(row._value_widget, QLineEdit):
            return bool(row._value_widget.text().strip())
        return False

    def _nvencc_split_enc_effective(self) -> bool:
        row = self._rows.get("split-enc")
        if row is None or not row._enabled_cb.isChecked():
            return False
        if isinstance(row._value_widget, QComboBox):
            return str(row._value_widget.currentData() or "disable") != "disable"
        return False

    def _on_nvencc_parallel_changed(self, *_args) -> None:
        if not self._nvencc_parallel_effective():
            return
        split_row = self._rows.get("split-enc")
        if split_row is not None and split_row._enabled_cb.isChecked():
            split_row._enabled_cb.setChecked(False)

    def _on_nvencc_split_enc_changed(self, *_args) -> None:
        if not self._nvencc_split_enc_effective():
            return
        parallel_row = self._rows.get("parallel")
        if parallel_row is not None and parallel_row._enabled_cb.isChecked():
            parallel_row._enabled_cb.setChecked(False)

    def _collect(self) -> str:
        if self._schema is None:
            return self._free_only.toPlainText().strip() if self._free_only else ""
        values = {key: row.value() for key, row in self._rows.items()}
        return _serialize(self._schema, values)

    def _refresh_preview(self) -> None:
        if self._schema is None:
            return
        self._preview.setPlainText(self._collect())

    def _accept(self) -> None:
        self._result_text = self._collect()
        self.accept()

    @property
    def result_text(self) -> str:
        return self._result_text


# =============================================================================
# Helper one-shot
# =============================================================================

def edit_extra_params(codec: str, current: str, parent: QWidget | None = None) -> str | None:
    """Ouvre la modale et retourne la nouvelle chaîne, ou None si annulé."""
    dlg = ExtraParamsDialog(codec, current, parent)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        return dlg.result_text
    return None
