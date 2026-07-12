"""
core/workflows/encode/runtime/nvencc.py — Intégration NVEncC (rigaya).

Public:
    NVENCC_VIDEO_CODECS     — frozenset des identifiants codec NVEncC
    NVENCC_CODEC_FLAG       — mapping codec → valeur du flag ``-c`` NVEncC
    NVENCC_OUTPUT_EXT       — mapping codec → extension du bitstream brut
    is_nvencc_codec(codec)  — True si ``codec`` est géré par NVEncC
    nvencc_binary_name()    — nom du binaire NVEncC selon plateforme
    detect_nvencc_available(nvencc_bin) — (available, supported_codecs)

    build_decode_pipe_cmd(...)    — phase 1 : ffmpeg → yuv4mpegpipe sur stdout
    build_nvencc_command(...)     — phase 2 : NVEncC --y4m -i - → bitstream brut
    build_remux_cmd(...)          — phase 3 : ffmpeg remux audio/subs/chapters
    build_nvencc_pipeline(...)    — agrégateur retournant les 3 commandes

Le pipeline est ``ffmpeg | NVEncC → ffmpeg`` : phase 1 et 2 communiquent via
``Popen.stdout = Popen.stdin`` à l'exécution (pattern ``merge_dovi.py``).
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from core.bluray import append_ffmpeg_input_args
from core.subprocess_utils import subprocess_text_kwargs
from core.workflows.encode.models import (
    QualityMode,
    VideoCropSettings,
    VideoEncodeSettings,
    VideoFilterSettings,
    VideoResizeSettings,
)
from core.workflows.encode.domain.codecs import build_vf as _build_ffmpeg_vf


NVENCC_VIDEO_CODECS: frozenset[str] = frozenset({
    "nvencc_hevc",
    "nvencc_h264",
    "nvencc_av1",
})

NVENCC_DYNAMIC_HDR_CODECS: frozenset[str] = frozenset({
    "nvencc_hevc",
    "nvencc_av1",
})

NVENCC_MANUAL_STATIC_HDR_CODECS: frozenset[str] = frozenset({
    "nvencc_hevc",
    "nvencc_av1",
})

NVENCC_WORKFLOW_OWNED_FLAGS: frozenset[str] = frozenset({
    "--master-display",
    "--max-cll",
    "--dhdr10-info",
    "--dolby-vision-profile",
    "--dolby-vision-rpu",
    "--dolby-vision-rpu-prm",
    "--avhw",
    "--avsw",
    "--colormatrix",
    "--colorprim",
    "--transfer",
    "--chromaloc",
    "--vpp-colorspace",
    "--vpp-libplacebo-tonemapping",
    "--vpp-libplacebo-tonemapping-lut",
    "--crop",
    "--output-res",
    "--vpp-resize",
    "--vpp-yadif",
    "--vpp-nlmeans",
})

NVENCC_QP_TRIPLET_FLAGS: frozenset[str] = frozenset({
    "--cqp",
    "--qp-init",
    "--qp-min",
    "--qp-max",
})

# Valeur du flag NVEncC ``-c <codec>`` à passer pour chaque identifiant interne.
NVENCC_CODEC_FLAG: dict[str, str] = {
    "nvencc_hevc": "hevc",
    "nvencc_h264": "h264",
    "nvencc_av1": "av1",
}

# Extension du fichier intermédiaire produit par NVEncC.
# Note importante : on utilise des conteneurs (.mkv) plutôt que des bitstreams
# bruts (.hevc/.h264/.ivf). Le pipe ``ffmpeg yuv4mpegpipe → NVEncC`` ne propage
# pas les timestamps, et ffmpeg refuse de muxer un bitstream sans timestamps
# (erreur "Can't write packet with unknown timestamp"). Faire écrire NVEncC dans
# un MKV résout ce problème : NVEncC utilise libavformat en interne et
# reconstruit les timestamps depuis l'input ``--y4m``.
NVENCC_OUTPUT_EXT: dict[str, str] = {
    "nvencc_hevc": ".mkv",
    "nvencc_h264": ".mkv",
    "nvencc_av1": ".mkv",
}


def is_nvencc_codec(codec: str | None) -> bool:
    """Retourne True si ``codec`` est l'un des identifiants NVEncC."""
    if not codec:
        return False
    return str(codec).strip().lower() in NVENCC_VIDEO_CODECS


def nvencc_supports_dynamic_hdr(codec: str | None) -> bool:
    if not codec:
        return False
    return str(codec).strip().lower() in NVENCC_DYNAMIC_HDR_CODECS


def nvencc_supports_manual_static_hdr(codec: str | None) -> bool:
    if not codec:
        return False
    return str(codec).strip().lower() in NVENCC_MANUAL_STATIC_HDR_CODECS


def nvencc_binary_name() -> str:
    """Nom du binaire NVEncC attendu sur le PATH selon la plateforme.

    - Windows : ``NVEncC64.exe`` (archive .7z rigaya).
    - Linux   : ``nvencc`` (lowercase — c'est le nom posé par les paquets
      .deb/.rpm rigaya, et le binaire produit par ``make``).
    """
    if sys.platform == "win32":
        return "NVEncC64.exe"
    return "nvencc"


# ---------------------------------------------------------------------------
# Détection à l'exécution
# ---------------------------------------------------------------------------

# `--check-features` produit une liste de codecs supportés par le GPU.
# On parse les sections du genre "Codec: H.264/AVC", "Codec: H.265/HEVC",
# "Codec: AV1". L'ordre / le format exact peut varier mais ces tokens sont
# stables dans la sortie de rigaya.
_FEATURE_TOKEN_BY_CODEC: dict[str, tuple[str, ...]] = {
    "nvencc_h264": ("H.264/AVC", "H.264", "AVC"),
    "nvencc_hevc": ("H.265/HEVC", "H.265", "HEVC"),
    "nvencc_av1": ("AV1",),
}


def _run_nvencc_check_features(nvencc_bin: str) -> str | None:
    """Exécute ``NVEncC --check-features`` et retourne sa sortie texte."""
    try:
        proc = subprocess.run(
            [nvencc_bin, "--check-features"],
            capture_output=True,
            check=False,
            timeout=10,
            **subprocess_text_kwargs(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    # NVEncC écrit l'output principal sur stdout, mais certains warnings
    # peuvent partir sur stderr. On combine pour robustesse.
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0 and not combined.strip():
        return None
    return combined


def _parse_supported_codecs(features_output: str) -> set[str]:
    """Extrait les codecs NVEncC supportés depuis la sortie ``--check-features``."""
    if not features_output:
        return set()
    supported: set[str] = set()
    for codec_id, tokens in _FEATURE_TOKEN_BY_CODEC.items():
        for token in tokens:
            # Match insensible à la casse, en évitant les sous-chaînes
            # ambiguës (le token "AV1" est court mais isolé dans la doc).
            pattern = re.compile(rf"\b{re.escape(token)}\b", re.IGNORECASE)
            if pattern.search(features_output):
                supported.add(codec_id)
                break
    return supported


def detect_nvencc_available(nvencc_bin: str | None) -> tuple[bool, set[str]]:
    """
    Détecte si NVEncC est utilisable et liste les codecs supportés par le GPU.

    Args:
        nvencc_bin: chemin résolu vers le binaire (``AppConfig.tool_nvencc``).
                    None ou chaîne vide → indisponible.

    Returns:
        (available, codecs)
            available : True si le binaire répond et au moins 1 codec dispo.
            codecs    : sous-ensemble de ``NVENCC_VIDEO_CODECS``.

    Le caller (HardwareEncoderDetector) doit avoir préalablement vérifié que
    NVENC ffmpeg est disponible — NVEncC n'est jamais exposé sans NVENC.
    """
    if not nvencc_bin:
        return False, set()
    output = _run_nvencc_check_features(nvencc_bin)
    if output is None:
        return False, set()
    supported = _parse_supported_codecs(output)
    return bool(supported), supported


# ---------------------------------------------------------------------------
# Construction du pipeline ffmpeg → NVEncC → ffmpeg
# ---------------------------------------------------------------------------

def build_decode_pipe_cmd(
    ffmpeg_bin: str,
    source: Path | str,
    *,
    stream_index: int = 0,
    extra_input_args: list[str] | None = None,
    vf: str | None = None,
) -> list[str]:
    """Phase 1 : décode ffmpeg → yuv4mpegpipe sur stdout.

    Le ``-vf`` optionnel sert uniquement aux préfiltrages portables que
    NVEncC ne couvre pas nativement dans l'application.
    """
    cmd: list[str] = [str(ffmpeg_bin), "-hide_banner", "-loglevel", "error", "-y"]
    if extra_input_args:
        cmd.extend(extra_input_args)
    append_ffmpeg_input_args(cmd, source)
    cmd.extend([
        "-map", f"0:{int(stream_index)}",
    ])
    if vf:
        cmd.extend(["-vf", str(vf)])
    cmd.extend(["-f", "yuv4mpegpipe", "-strict", "-1", "-"])
    return cmd


def nvencc_requires_ffmpeg_filter_pipe(video: VideoEncodeSettings) -> bool:
    """True when portable FFmpeg prefiltering is required before NVEncC."""
    filters = video.filters
    crop = video.crop
    resize = video.resize
    return bool(
        (filters.deblock_enabled or filters.chroma_smooth_enabled)
        or (crop.is_active() and crop.unit == "percent")
        or (resize.is_active() and str(resize.mode or "").strip().lower() == "percent")
    )


def nvencc_ffmpeg_filter_vf(video: VideoEncodeSettings) -> str:
    return _build_ffmpeg_vf(video)


def nvencc_pipe_encode_video(video: VideoEncodeSettings) -> VideoEncodeSettings:
    """Return settings already filtered by FFmpeg, keeping encode/HDR knobs."""
    return replace(
        video,
        resize=VideoResizeSettings(),
        crop=VideoCropSettings(),
        filters=VideoFilterSettings(),
        tonemap_to_sdr=False,
    )


def _rate_control_args(video: VideoEncodeSettings) -> list[str]:
    """Mode RC NVEncC dérivé de ``QualityMode``.

    - ``CRF``     → ``--cqp <crf>:<crf+2>:<crf+4>`` (qualité constante I/P/B)
    - ``CQ``      → ``--qvbr <cq>`` (mode qualité-VBR, défaut NVEncC)
    - ``BITRATE`` → ``--vbr <kbps>`` (bitrate moyen)
    - ``SIZE``    → traité par le caller (conversion size→bitrate amont)
    """
    mode = video.quality_mode
    if mode == QualityMode.CRF:
        crf = max(0, int(video.crf))
        return ["--cqp", f"{crf}:{min(51, crf + 2)}:{min(51, crf + 4)}"]
    if mode == QualityMode.CQ:
        return ["--qvbr", str(int(video.cq))]
    if mode == QualityMode.BITRATE:
        return ["--vbr", str(int(video.bitrate_kbps))]
    # SIZE : on suppose que bitrate_kbps a été calculé en amont.
    return ["--vbr", str(int(video.bitrate_kbps))]


def _output_depth_args(video: VideoEncodeSettings) -> list[str]:
    """``--output-depth 8/10`` : 10-bit obligatoire pour HDR, 8-bit forcé H.264."""
    is_h264 = video.codec == "nvencc_h264"
    if is_h264 and bool(getattr(video, "force_8bit", False)):
        return ["--output-depth", "8"]
    if bool(getattr(video, "force_10bit", False)):
        return ["--output-depth", "10"]
    return []


def _hdr_static_args(video: VideoEncodeSettings) -> list[str]:
    """Métadonnées HDR statiques (master display + MaxCLL/MaxFALL)."""
    args: list[str] = []
    if not getattr(video, "inject_hdr_meta", False):
        return args
    md = (video.master_display or "").strip()
    if md:
        args.extend(["--master-display", md])
    cll = (video.max_cll or "").strip()
    if cll:
        args.extend(["--max-cll", cll])
    return args


def map_nvencc_dovi_profile(profile: str | None) -> str | None:
    """Mappe la sémantique UI legacy vers l'option NVEncC attendue.

    NVEncC attend les profils Dolby Vision sous leur forme décimale
    (ex. ``8.1``), alors que notre UI et certains chemins legacy manipulent
    aussi des alias compacts ou symboliques (ex. ``2`` pour "normaliser en
    P8.1"). On normalise donc ici vers la représentation textuelle acceptée
    par NVEncC.
    """
    value = str(profile or "").strip().lower()
    if value in {"", "0", "copy"}:
        return "copy"

    aliases = {
        "2": "8.1",
        "8": "8.1",
        "81": "8.1",
        "82": "8.2",
        "84": "8.4",
        "50": "5.0",
        "100": "10.0",
        "101": "10.1",
        "102": "10.2",
        "104": "10.4",
    }
    if value in aliases:
        return aliases[value]

    if value in {"5.0", "8.1", "8.2", "8.4", "10.0", "10.1", "10.2", "10.4"}:
        return value
    return value


def _auto_source_hdr_args(video: VideoEncodeSettings) -> list[str]:
    """Recopie les caractéristiques HDR source quand NVEncC lit le fichier."""
    if not (getattr(video, "copy_dv", False) or getattr(video, "copy_hdr10plus", False)):
        return []
    return [
        "--colormatrix", "auto",
        "--colorprim", "auto",
        "--transfer", "auto",
        "--chromaloc", "auto",
        "--master-display", "copy",
        "--max-cll", "copy",
    ]


def map_nvencc_tonemap_args(video: VideoEncodeSettings) -> list[str]:
    """Mappe le tone-map UI vers les VPP NVEncC."""
    if not getattr(video, "tonemap_to_sdr", False):
        return []

    algo = str(getattr(video, "tonemap_algorithm", "") or "hable").strip().lower()
    if algo in {"hable", "mobius", "reinhard", "bt2390"}:
        return [
            "--vpp-colorspace",
            f"matrix=bt2020nc:bt709,hdr2sdr={algo}",
        ]
    if algo in {"clip", "gamma", "linear"}:
        return [
            "--vpp-libplacebo-tonemapping",
            f"src_csp=hdr10,dst_csp=sdr,tonemapping_function={algo}",
        ]
    return [
        "--vpp-colorspace",
        "matrix=bt2020nc:bt709,hdr2sdr=hable",
    ]


def _nvencc_resize_args(video: VideoEncodeSettings) -> list[str]:
    resize = video.resize
    if not resize.is_active():
        return []
    mode = str(resize.mode or "preset").strip().lower()
    if mode == "percent":
        # Percent resize needs source dimensions, so keep it in FFmpeg when
        # callers require exact scaling. Direct NVEncC keeps native settings.
        return []
    if mode == "size":
        width = max(2, int(resize.width or 2))
        height = max(2, int(resize.height or 2))
    else:
        presets = {
            "720p": (1280, 720),
            "1080p": (1920, 1080),
            "1440p": (2560, 1440),
            "2160p": (3840, 2160),
        }
        width, height = presets.get(str(resize.preset or "720p"), presets["720p"])
    args = ["--output-res", f"{width}x{height}"]
    algo = str(resize.algorithm or "lanczos").strip().lower()
    nvencc_algo = {
        "lanczos": "lanczos",
        "bicubic": "bicubic",
        "bilinear": "bilinear",
        "spline": "spline36",
    }.get(algo, "lanczos")
    args.extend(["--vpp-resize", f"algo={nvencc_algo}"])
    return args


def _nvencc_crop_args(video: VideoEncodeSettings) -> list[str]:
    crop = video.crop
    if not crop.is_active() or crop.auto or crop.unit == "percent":
        return []
    left = max(0, int(crop.left))
    top = max(0, int(crop.top))
    right = max(0, int(crop.right))
    bottom = max(0, int(crop.bottom))
    return ["--crop", f"{left},{top},{right},{bottom}"]


def _nvencc_filter_args(video: VideoEncodeSettings) -> list[str]:
    filters = video.filters
    args: list[str] = []
    if filters.yadif_enabled:
        mode = str(filters.yadif_mode or "send_frame").strip().lower()
        mapped = {
            "send_frame": "auto",
            "send_field": "bob",
            "bob": "bob",
            "auto": "auto",
        }.get(mode, "auto")
        args.extend(["--vpp-yadif", f"mode={mapped}"])
    if filters.nlmeans_enabled:
        strength = {
            "ultralight": (0.003, 0.035, 3, 7),
            "light": (0.005, 0.050, 5, 11),
            "medium": (0.008, 0.065, 7, 13),
            "strong": (0.012, 0.080, 7, 15),
        }.get(str(filters.nlmeans_strength or "light").strip().lower(), (0.005, 0.050, 5, 11))
        sigma, h, patch, search = strength
        args.extend(["--vpp-nlmeans", f"sigma={sigma},h={h},patch={patch},search={search}"])
    return args


def map_nvencc_video_transform_args(video: VideoEncodeSettings) -> list[str]:
    args: list[str] = []
    args.extend(_nvencc_crop_args(video))
    args.extend(_nvencc_resize_args(video))
    args.extend(_nvencc_filter_args(video))
    return args


def normalize_nvencc_qp_triplet(value: str | None) -> str | None:
    """Normalise ``X`` en ``X:X:X`` pour les champs QP I:P:B."""
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d+", text):
        return f"{text}:{text}:{text}"
    return text


def normalize_nvencc_parallel(value: str | None) -> str | None:
    """Canonise l'ancien alias ``all`` vers ``auto``."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.lower() == "all":
        return "auto"
    return text


def sanitize_nvencc_extra_params(extra_params: str) -> list[str]:
    """Retire les flags pilotés par le workflow et normalise les triplets QP."""
    raw = (extra_params or "").strip()
    if not raw:
        return []
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()

    sanitized: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token.startswith("--"):
            sanitized.append(token)
            i += 1
            continue

        if "=" in token:
            option, value = token.split("=", 1)
            if option in NVENCC_WORKFLOW_OWNED_FLAGS:
                i += 1
                continue
            if option == "--parallel":
                normalized_parallel = normalize_nvencc_parallel(value)
                if normalized_parallel:
                    sanitized.extend([option, normalized_parallel])
                i += 1
                continue
            if option in NVENCC_QP_TRIPLET_FLAGS:
                normalized = normalize_nvencc_qp_triplet(value)
                if normalized:
                    sanitized.extend([option, normalized])
                i += 1
                continue
            sanitized.append(token)
            i += 1
            continue

        next_is_value = i + 1 < len(tokens) and not tokens[i + 1].startswith("--")
        if token in NVENCC_WORKFLOW_OWNED_FLAGS:
            i += 2 if next_is_value else 1
            continue
        if token == "--parallel" and next_is_value:
            normalized_parallel = normalize_nvencc_parallel(tokens[i + 1])
            sanitized.append(token)
            if normalized_parallel:
                sanitized.append(normalized_parallel)
            i += 2
            continue
        if token in NVENCC_QP_TRIPLET_FLAGS and next_is_value:
            normalized = normalize_nvencc_qp_triplet(tokens[i + 1])
            sanitized.append(token)
            if normalized:
                sanitized.append(normalized)
            i += 2
            continue

        sanitized.append(token)
        i += 1
    return sanitized


def strip_nvencc_parallel_args(args: list[str]) -> list[str]:
    """Retire ``--parallel`` de la liste d'arguments NVEncC.

    Le mode parallel encode de NVEncC segmente le flux en plusieurs workers.
    Avec la recopie native DoVi/HDR10+, ce chemin peut échouer très tôt côté
    NVENC (``nvEncLockBitstream: invalid param`` / ``PECOLLECT``). On
    neutralise donc explicitement ``--parallel`` quand le workflow demande une
    copie HDR dynamique native.
    """
    stripped: list[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--parallel":
            next_is_value = i + 1 < len(args) and not args[i + 1].startswith("--")
            i += 2 if next_is_value else 1
            continue
        if token.startswith("--parallel="):
            i += 1
            continue
        stripped.append(token)
        i += 1
    return stripped


def strip_nvencc_latency_args(args: list[str]) -> list[str]:
    """Retire les réglages NVEncC orientés faible latence.

    Le tune `lowlatency`/`ultralowlatency` et le flag `--lowlatency`
    privilégient le pipeline live/streaming. Sur le chemin de copie HDR
    dynamique natif (DoVi/HDR10+), ces modes peuvent casser très tôt côté
    encodeur. On les retire donc quand le workflow demande cette recopie
    native, tout en laissant les autres tunes (`hq`, `uhq`, `lossless`) intacts.
    """
    stripped: list[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--lowlatency":
            i += 1
            continue
        if token.startswith("--tune="):
            tune_value = token.split("=", 1)[1].strip().lower()
            if tune_value in {"lowlatency", "ultralowlatency"}:
                i += 1
                continue
            stripped.append(token)
            i += 1
            continue
        if token == "--tune":
            next_is_value = i + 1 < len(args) and not args[i + 1].startswith("--")
            if next_is_value:
                tune_value = str(args[i + 1]).strip().lower()
                if tune_value in {"lowlatency", "ultralowlatency"}:
                    i += 2
                    continue
            stripped.append(token)
            if next_is_value:
                stripped.append(args[i + 1])
                i += 2
            else:
                i += 1
            continue
        stripped.append(token)
        i += 1
    return stripped


def _hdr_dynamic_args(
    video: VideoEncodeSettings,
    *,
    hdr10plus_json: Path | str | None = None,
    dovi_rpu: Path | str | None = None,
    dovi_rpu_prm: str | None = None,
) -> list[str]:
    """Flux HDR10+ et DoVi : passthrough (``copy``) ou fichiers extraits amont."""
    args: list[str] = []
    if hdr10plus_json is not None:
        args.extend(["--dhdr10-info", str(hdr10plus_json)])
    elif getattr(video, "copy_hdr10plus", False):
        args.extend(["--dhdr10-info", "copy"])
    if dovi_rpu is not None:
        args.extend(["--dolby-vision-rpu", str(dovi_rpu)])
        mapped_profile = map_nvencc_dovi_profile(video.dovi_profile)
        # Quand on injecte un RPU externe, le profil doit être explicite ou
        # absent ; `copy` n'a de sens qu'en passthrough direct depuis la source.
        if mapped_profile and mapped_profile != "copy":
            args.extend(["--dolby-vision-profile", mapped_profile])
    elif getattr(video, "copy_dv", False):
        args.extend(["--dolby-vision-rpu", "copy"])
        mapped_profile = map_nvencc_dovi_profile(video.dovi_profile)
        if mapped_profile in {None, "copy"}:
            mapped_profile = "8.1"
        if mapped_profile:
            args.extend(["--dolby-vision-profile", mapped_profile])
    if dovi_rpu_prm and "--dolby-vision-rpu" in args:
        args.extend(["--dolby-vision-rpu-prm", str(dovi_rpu_prm)])
    return args


def build_nvencc_command(
    nvencc_bin: str,
    video: VideoEncodeSettings,
    output_path: Path | str,
    *,
    input_path: Path | str | None = None,
    stream_index: int | None = None,
    input_reader: str | None = None,
    input_fps: str | None = None,
    input_avsync: str | None = None,
    hdr10plus_json: Path | str | None = None,
    dovi_rpu: Path | str | None = None,
    dovi_rpu_prm: str | None = None,
) -> list[str]:
    """Phase 2 : commande NVEncC complète (stdin = yuv4mpegpipe phase 1).

    Args:
        nvencc_bin     : chemin vers le binaire NVEncC.
        video          : settings de la piste vidéo.
        output_path    : fichier intermédiaire (.hevc/.h264/.ivf).
        hdr10plus_json : JSON HDR10+ extrait amont (sinon ``copy_hdr10plus`` → 'copy').
        dovi_rpu       : RPU DoVi (.bin) extrait amont (sinon ``copy_dv`` → 'copy').

    Le caller appliquera les ``extra_params`` du dialog (``shlex.split``)
    en concaténation finale.
    """
    codec_flag = NVENCC_CODEC_FLAG.get(video.codec)
    if codec_flag is None:
        raise ValueError(f"Codec NVEncC inconnu : {video.codec}")

    cmd: list[str] = [str(nvencc_bin), "-c", codec_flag]
    if input_path is None:
        cmd.extend(["--y4m", "-i", "-"])
    else:
        if input_reader in {"avhw", "avsw"}:
            cmd.append(f"--{input_reader}")
        cmd.extend(["-i", str(input_path)])
        if input_avsync:
            cmd.extend(["--avsync", str(input_avsync)])
        if input_fps:
            # Sur certains elementary streams (ex. HEVC Annex B), le reader
            # avformat ne conserve pas toujours la cadence correcte et peut
            # retomber sur un hint implicite. On transmet donc explicitement
            # le fps source quand l'appelant en dispose.
            cmd.extend(["--fps", str(input_fps)])
        if stream_index is not None:
            # ``stream_index`` correspond à l'index ffprobe/libavformat du flux
            # source ; NVEncC attend cela via ``--video-streamid`` et non
            # ``--video-track`` (qui utilise sa propre notion de track id).
            cmd.extend(["--video-streamid", str(int(stream_index))])
    cmd.extend(_rate_control_args(video))
    cmd.extend(_output_depth_args(video))

    # Preset NVEncC : si l'utilisateur a sélectionné un preset valide pour
    # NVEncC (default/performance/quality/P1..P7), on l'applique. On ne
    # propage PAS les presets x265/NVENC ffmpeg qui ne s'appliquent pas.
    preset = (video.preset or "").strip()
    if preset and (preset.lower() in {"default", "performance", "quality"}
                   or (preset.upper() in {"P1", "P2", "P3", "P4", "P5", "P6", "P7"})):
        cmd.extend(["-u", preset])

    if input_path is not None and not getattr(video, "inject_hdr_meta", False):
        cmd.extend(_auto_source_hdr_args(video))
    cmd.extend(_hdr_static_args(video))
    cmd.extend(
        _hdr_dynamic_args(
            video,
            hdr10plus_json=hdr10plus_json,
            dovi_rpu=dovi_rpu,
            dovi_rpu_prm=dovi_rpu_prm,
        )
    )
    cmd.extend(map_nvencc_video_transform_args(video))
    cmd.extend(map_nvencc_tonemap_args(video))

    # extra_params experts : on retire les flags possédés par le workflow
    # standard avant de concaténer le reliquat en fin de commande.
    extra_args = sanitize_nvencc_extra_params(video.extra_params)
    if getattr(video, "copy_dv", False) or getattr(video, "copy_hdr10plus", False):
        extra_args = strip_nvencc_parallel_args(extra_args)
        extra_args = strip_nvencc_latency_args(extra_args)
    cmd.extend(extra_args)

    cmd.extend(["-o", str(output_path)])
    return cmd


def build_remux_cmd(
    ffmpeg_bin: str,
    encoded_video: Path | str,
    source: Path | str,
    output: Path | str,
    *,
    map_audio: bool = True,
    map_subtitles: bool = True,
    map_chapters: bool = True,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Phase 3 : remux ffmpeg.

    - Input 0 : bitstream encodé (vidéo seule)
    - Input 1 : source originale (audio/subs/chapitres)

    Tous les flux sont copiés sans réencodage. Si le caller veut
    réencoder l'audio, il fournit ``extra_args`` avec les options ``-c:a …``.
    """
    cmd: list[str] = [
        str(ffmpeg_bin), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(encoded_video),
    ]
    append_ffmpeg_input_args(cmd, source)
    cmd.extend(["-map", "0:v:0"])
    if map_audio:
        cmd.extend(["-map", "1:a?"])
    if map_subtitles:
        cmd.extend(["-map", "1:s?"])
    if map_chapters:
        cmd.extend(["-map_chapters", "1"])
    cmd.extend(["-c", "copy"])
    if extra_args:
        cmd.extend(extra_args)
    cmd.append(str(output))
    return cmd


def build_nvencc_pipeline(
    *,
    ffmpeg_bin: str,
    nvencc_bin: str,
    video: VideoEncodeSettings,
    source: Path | str,
    output: Path | str,
    intermediate: Path | str,
    stream_index: int = 0,
    hdr10plus_json: Path | str | None = None,
    dovi_rpu: Path | str | None = None,
    audio_args: list[str] | None = None,
    map_audio: bool = True,
    map_subtitles: bool = True,
    map_chapters: bool = True,
) -> list[list[str]]:
    """Agrégateur : retourne les 3 commandes du pipeline NVEncC.

    Returns:
        [decode_cmd, encode_cmd, remux_cmd]

    À l'exécution, les phases 1 et 2 doivent être lancées en parallèle
    via ``Popen`` avec ``p1.stdout = p2.stdin`` (cf. ``merge_dovi.py``).
    La phase 3 démarre une fois ``intermediate`` complet.
    """
    needs_prefilter = nvencc_requires_ffmpeg_filter_pipe(video)
    decode = build_decode_pipe_cmd(
        ffmpeg_bin,
        source,
        stream_index=stream_index,
        vf=nvencc_ffmpeg_filter_vf(video) if needs_prefilter else None,
    )
    encode = build_nvencc_command(
        nvencc_bin,
        nvencc_pipe_encode_video(video) if needs_prefilter else video,
        intermediate,
        hdr10plus_json=hdr10plus_json,
        dovi_rpu=dovi_rpu,
    )
    remux = build_remux_cmd(
        ffmpeg_bin, intermediate, source, output,
        map_audio=map_audio,
        map_subtitles=map_subtitles,
        map_chapters=map_chapters,
        extra_args=audio_args,
    )
    return [decode, encode, remux]


def nvencc_intermediate_path(work_dir: Path, codec: str, base_name: str = "nvencc") -> Path:
    """Chemin du fichier intermédiaire (bitstream brut) à partir du codec."""
    ext = NVENCC_OUTPUT_EXT.get(codec, ".bin")
    return Path(work_dir) / f"{base_name}{ext}"


__all__ = [
    "NVENCC_VIDEO_CODECS",
    "NVENCC_DYNAMIC_HDR_CODECS",
    "NVENCC_MANUAL_STATIC_HDR_CODECS",
    "NVENCC_WORKFLOW_OWNED_FLAGS",
    "NVENCC_QP_TRIPLET_FLAGS",
    "NVENCC_CODEC_FLAG",
    "NVENCC_OUTPUT_EXT",
    "is_nvencc_codec",
    "nvencc_supports_dynamic_hdr",
    "nvencc_supports_manual_static_hdr",
    "nvencc_binary_name",
    "detect_nvencc_available",
    "build_decode_pipe_cmd",
    "nvencc_requires_ffmpeg_filter_pipe",
    "nvencc_ffmpeg_filter_vf",
    "nvencc_pipe_encode_video",
    "build_nvencc_command",
    "build_remux_cmd",
    "build_nvencc_pipeline",
    "map_nvencc_dovi_profile",
    "map_nvencc_tonemap_args",
    "map_nvencc_video_transform_args",
    "normalize_nvencc_qp_triplet",
    "sanitize_nvencc_extra_params",
    "nvencc_intermediate_path",
]
