"""Physical timestamp rewrite helpers for explicit per-track sync offsets."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, cast

from core.bluray import append_ffmpeg_input_args, ffprobe_input_args
from core.subprocess_utils import (
    decode_subprocess_output,
    subprocess_text_kwargs,
    subprocess_windows_no_window_kwargs,
)
from core.subtitle_codec import CONVERT_TO_SRT
from core.workflows.remux_models import RemuxError


TEXT_SUBTITLE_CODECS: frozenset[str] = frozenset({
    "subrip",
    "srt",
    "ass",
    "ssa",
    "webvtt",
})

REWRITE_SUBTITLE_CODECS: frozenset[str] = TEXT_SUBTITLE_CODECS | CONVERT_TO_SRT
REWRITE_AUDIO_CODECS: frozenset[str] = frozenset({"ac3", "eac3", "aac"})
SYNC_REWRITE_STAGE_PREFIX = "__MRE_SYNC_REWRITE_STAGE__ "
SYNC_REWRITE_MODE_AUTO = ""
SYNC_REWRITE_MODE_OFFSET = "offset"
SYNC_REWRITE_OFFSET_LABEL = "Sync offset"
SYNC_REWRITE_ADVANCED_AUDIO_LABEL = "Sync réelle · audio avancé"

_OBJECT_AUDIO_MARKERS = (
    "atmos",
    "joc",
    "dolby digital plus with dolby atmos",
    "dts:x",
    "dtsx",
    "xll x",
    "truehd atmos",
)

_DTS_AUDIO_CODECS = frozenset({"dts", "dca", "dtshd", "dts_hd"})
_TRUEHD_AUDIO_CODECS = frozenset({"truehd", "mlp"})
_LOSSLESS_AUDIO_CODECS = frozenset({"flac", "alac", "wavpack", "wv", "tta"})
_LOSSLESS_TO_FLAC_AUDIO_CODECS = frozenset({"ape", "wmalossless"})
_LOSSY_AUDIO_CODECS = frozenset({"mp2", "mp3", "opus", "vorbis", "wmav1", "wmav2"})

_AC3_STANDARD_BITRATES_KBPS = (
    32, 40, 48, 56, 64, 80, 96, 112,
    128, 160, 192, 224, 256, 320, 384, 448, 512, 576, 640,
)


@dataclass(frozen=True)
class SyncRewriteUiPreview:
    label: str = ""
    can_toggle: bool = False
    is_advanced: bool = False
    warning_tooltip: str = ""
    precision_label: str = ""
    residual_label: str = ""
    strategy: str = ""


@dataclass(frozen=True)
class SyncRewritePreparedInput:
    """A physical rewrite materialized as an extra mono-stream FFmpeg input."""

    path: Path
    input_idx: int
    track_type: str
    codec: str
    mode_label: str
    bitrate_kbps: int | None = None
    strategy: str = ""
    is_advanced: bool = False
    precision_label: str = ""
    residual_label: str = ""


def normalized_rewrite_codec(codec: str) -> str:
    raw = str(codec or "").strip().lower()
    compact = re.sub(r"[\s_./+]+", "-", raw)
    aliases = {
        "subrip": "srt",
        "e-ac-3": "eac3",
        "eac3-joc": "eac3",
        "e-ac-3-joc": "eac3",
        "ddp": "eac3",
        "dd+": "eac3",
        "dd-": "eac3",
        "aac-lc": "aac",
        "dca": "dts",
        "dts-hd": "dts",
        "dts-hd-ma": "dts",
        "dts-hd-hra": "dts",
        "dts:x": "dts",
        "dtsx": "dts",
        "dtshd": "dts",
        "lpcm": "pcm_s24le",
        "pcm": "pcm_s24le",
        "wv": "wavpack",
        "wma-lossless": "wmalossless",
        "wma1": "wmav1",
        "wma2": "wmav2",
    }
    if compact in aliases:
        return aliases[compact]
    if compact.startswith("dts"):
        return "dts"
    if compact.startswith("truehd"):
        return "truehd"
    if compact.startswith("pcm-"):
        return raw.replace("-", "_")
    return aliases.get(raw, raw)


def _is_pcm_audio_codec(codec_key: str) -> bool:
    return codec_key.startswith("pcm_") or codec_key in {"pcm", "lpcm"}


def _is_dts_audio_codec(codec_key: str) -> bool:
    return codec_key in _DTS_AUDIO_CODECS or codec_key.startswith("dts")


def _is_truehd_audio_codec(codec_key: str) -> bool:
    return codec_key in _TRUEHD_AUDIO_CODECS


def _advanced_audio_kind(codec_key: str, *, object_audio: bool) -> str:
    if codec_key == "eac3" and object_audio:
        return "eac3_joc"
    if _is_dts_audio_codec(codec_key):
        return "dts_object" if object_audio else "dts"
    if _is_truehd_audio_codec(codec_key):
        return "truehd_object" if object_audio else "truehd"
    if _is_pcm_audio_codec(codec_key):
        return "pcm_lossless"
    if codec_key in _LOSSLESS_AUDIO_CODECS:
        return "lossless"
    if codec_key in _LOSSLESS_TO_FLAC_AUDIO_CODECS:
        return "lossless_to_flac"
    if codec_key in _LOSSY_AUDIO_CODECS:
        return "lossy"
    return ""


def _advanced_audio_kind_supports_offset(kind: str, offset_ms: int) -> bool:
    if kind in {"eac3_joc", "dts", "dts_object", "truehd_object"}:
        return int(offset_ms or 0) < 0
    if kind in {"truehd", "pcm_lossless", "lossless", "lossless_to_flac", "lossy"}:
        return True
    return False


def _advanced_audio_warning_parts(
    *,
    codec_key: str,
    kind: str,
    offset_ms: int,
) -> tuple[str, str, str, str, str]:
    if kind == "eac3_joc":
        return (
            "EAC3+JOC / Atmos",
            "copie/coupe par paquets audio",
            "frontière frame/TU",
            "jusqu'à une frame audio",
            "Les métadonnées objet sont conservées uniquement par copie. Retard positif laissé en sync offset.",
        )
    if kind in {"dts", "dts_object"}:
        return (
            "DTS / DTS-HD / DTS:X",
            "copie/coupe par access unit",
            "frontière access-unit",
            "jusqu'à une access unit",
            "Pas de génération de silence DTS fiable; les retards positifs restent en sync offset.",
        )
    if kind == "truehd_object":
        return (
            "TrueHD Atmos",
            "copie/coupe validée par decode",
            "frontière paquet TrueHD",
            "jusqu'à une frame audio",
            "Les métadonnées Atmos ne sont pas réencodées; retard positif laissé en sync offset.",
        )
    if kind == "truehd":
        return (
            "TrueHD / MLP sans Atmos",
            "decode PCM + trim/silence + réencodage TrueHD/MLP",
            "échantillon PCM",
            "aucun résiduel attendu si le layout est supporté",
            "Réencodage lossless expérimental, non bit-perfect; fallback si layout non supporté.",
        )
    if kind in {"pcm_lossless", "lossless"}:
        return (
            "Audio lossless",
            "trim/silence + réencodage lossless",
            "échantillon PCM",
            "aucun résiduel attendu",
            "Le contenu reste lossless mais le bitstream n'est pas identique à la source.",
        )
    if kind == "lossless_to_flac":
        return (
            "Audio lossless converti",
            "trim/silence + conversion FLAC",
            "échantillon PCM",
            "aucun résiduel attendu",
            "Le codec de sortie devient FLAC quand le codec source n'a pas d'encodeur fiable.",
        )
    if kind == "lossy":
        return (
            "Audio lossy",
            "trim/silence + réencodage lossy",
            "échantillon PCM",
            "aucun résiduel attendu",
            "Réencodage avec perte générationnelle possible.",
        )
    return (
        codec_key.upper() or "Audio",
        "stratégie avancée",
        "inconnue",
        "inconnu",
        "Fallback sync offset si l'opération n'est pas sûre.",
    )


def _advanced_audio_warning_tooltip(
    *,
    codec_key: str,
    kind: str,
    offset_ms: int,
) -> tuple[str, str, str]:
    family, strategy, precision, residual, limitation = _advanced_audio_warning_parts(
        codec_key=codec_key,
        kind=kind,
        offset_ms=offset_ms,
    )
    offset_label = f"{int(offset_ms):+d} ms"
    tooltip = "\n".join((
        "Sync réelle audio avancée",
        f"Codec/famille : {family}",
        f"Décalage demandé : Δt {offset_label}",
        f"Stratégie : {strategy}",
        f"Précision attendue : {precision}",
        f"Résiduel possible : {residual}",
        f"Limitation : {limitation}",
    ))
    return tooltip, precision, residual


def track_has_object_audio_metadata(
    *,
    codec: str = "",
    title: str = "",
    display_info: str = "",
    profile: str = "",
    codec_long_name: str = "",
) -> bool:
    haystack = " ".join(
        str(part or "").lower()
        for part in (codec, title, display_info, profile, codec_long_name)
    )
    return any(marker in haystack for marker in _OBJECT_AUDIO_MARKERS)


def audio_bitrate_kbps_from_display_info(display_info: str) -> int | None:
    match = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(?:kbps|kb/s|kbit/s)",
        str(display_info or ""),
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    try:
        return max(1, round(float(match.group(1).replace(",", "."))))
    except ValueError:
        return None


def normalized_sync_rewrite_mode(value: str | None) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if raw in {"offset", "sync_offset", "standard", "sync_standard"}:
        return SYNC_REWRITE_MODE_OFFSET
    return SYNC_REWRITE_MODE_AUTO


def sync_rewrite_forced_offset(track) -> bool:
    return normalized_sync_rewrite_mode(
        str(getattr(track, "sync_rewrite_mode", "") or "")
    ) == SYNC_REWRITE_MODE_OFFSET


def ui_sync_rewrite_preview_for_track(
    track,
    *,
    enabled: bool,
    advanced_audio_enabled: bool = False,
) -> SyncRewriteUiPreview:
    """Best-effort UI preview; runtime still performs the authoritative probe."""
    offset_ms = int(getattr(track, "time_shift_ms", 0) or 0)
    if not enabled or offset_ms == 0:
        return SyncRewriteUiPreview()
    track_type = str(getattr(track, "track_type", "") or "").strip().lower()
    raw_codec = str(getattr(track, "codec", "") or "")
    codec = normalized_rewrite_codec(raw_codec)
    if track_type == "subtitle":
        if codec in REWRITE_SUBTITLE_CODECS:
            return SyncRewriteUiPreview(label="Sync réelle", can_toggle=True)
        return SyncRewriteUiPreview(label=SYNC_REWRITE_OFFSET_LABEL)
    if track_type == "audio":
        object_audio = track_has_object_audio_metadata(
            codec=" ".join([raw_codec, codec]),
            title=str(getattr(track, "title", "") or ""),
            display_info=str(getattr(track, "orig_display_info", "") or getattr(track, "display_info", "") or ""),
        )
        if codec in REWRITE_AUDIO_CODECS and not object_audio:
            return SyncRewriteUiPreview(
                label="Sync réelle · audio réencodé",
                can_toggle=True,
            )
        kind = _advanced_audio_kind(codec, object_audio=object_audio)
        if not kind or not advanced_audio_enabled or not _advanced_audio_kind_supports_offset(kind, offset_ms):
            return SyncRewriteUiPreview(label=SYNC_REWRITE_OFFSET_LABEL)
        tooltip, precision, residual = _advanced_audio_warning_tooltip(
            codec_key=codec,
            kind=kind,
            offset_ms=offset_ms,
        )
        return SyncRewriteUiPreview(
            label=SYNC_REWRITE_ADVANCED_AUDIO_LABEL,
            can_toggle=True,
            is_advanced=True,
            warning_tooltip=tooltip,
            precision_label=precision,
            residual_label=residual,
            strategy=kind,
        )
    return SyncRewriteUiPreview()


def ui_sync_rewrite_auto_label_for_track(
    track,
    *,
    enabled: bool,
    advanced_audio_enabled: bool = False,
) -> str:
    return ui_sync_rewrite_preview_for_track(
        track,
        enabled=enabled,
        advanced_audio_enabled=advanced_audio_enabled,
    ).label


def ui_sync_rewrite_label_for_track(
    track,
    *,
    enabled: bool,
    advanced_audio_enabled: bool = False,
) -> str:
    if sync_rewrite_forced_offset(track) and ui_sync_rewrite_can_toggle(
        track,
        enabled=enabled,
        advanced_audio_enabled=advanced_audio_enabled,
    ):
        return SYNC_REWRITE_OFFSET_LABEL
    return ui_sync_rewrite_auto_label_for_track(
        track,
        enabled=enabled,
        advanced_audio_enabled=advanced_audio_enabled,
    )


def ui_sync_rewrite_can_toggle(
    track,
    *,
    enabled: bool,
    advanced_audio_enabled: bool = False,
) -> bool:
    return ui_sync_rewrite_preview_for_track(
        track,
        enabled=enabled,
        advanced_audio_enabled=advanced_audio_enabled,
    ).can_toggle


def sync_rewrite_output_token(source_path: Path | str, stream_index: int, track_type: str) -> str:
    stem = Path(str(source_path)).stem if str(source_path) else "track"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._") or "track"
    return f"{safe}_s{int(stream_index)}_{track_type}"


def sync_rewrite_stage_progress_line(track_type: str, name: str) -> str:
    payload = {
        "track_type": str(track_type or "").strip().lower(),
        "name": " ".join(str(name or "").split()),
    }
    return SYNC_REWRITE_STAGE_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class SyncRewriteService:
    def __init__(
        self,
        *,
        ffmpeg_bin: str,
        ffprobe_bin: str = "ffprobe",
        ffmpeg_progress_args: list[str] | None = None,
        ffmpeg_thread_args: list[str] | None = None,
        log_cb: Callable[[str], None] | None = None,
        progress_cb: Callable[[str], None] | None = None,
        audio_bitrate_per_channel: Mapping[str, int] | None = None,
        advanced_audio_enabled: bool = False,
    ) -> None:
        self._ffmpeg = ffmpeg_bin
        self._ffprobe = ffprobe_bin
        self._progress_args = list(ffmpeg_progress_args or [])
        self._thread_args = list(ffmpeg_thread_args or [])
        self._log = log_cb or (lambda _message: None)
        self._progress = progress_cb
        self._advanced_audio_enabled = bool(advanced_audio_enabled)
        bitrates = dict(audio_bitrate_per_channel or {})
        self._audio_bitrate_per_channel = {
            "aac": int(bitrates.get("aac", 96) or 96),
            "ac3": int(bitrates.get("ac3", bitrates.get("eac3", 96)) or 96),
            "eac3": int(bitrates.get("eac3", 96) or 96),
        }

    def maybe_materialize(
        self,
        *,
        source_path: Path | str,
        stream_index: int,
        track_type: str,
        codec: str,
        title: str = "",
        display_info: str = "",
        offset_ms: int,
        tmp_dir: Path,
        input_idx: int,
        token: str | None = None,
        preserve_source_audio_params: bool = True,
        audio_target_codec: str = "",
        audio_target_bitrate_kbps: int | None = None,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> SyncRewritePreparedInput | None:
        if int(offset_ms or 0) == 0:
            return None
        if cancel_cb is not None and cancel_cb():
            raise RemuxError("Réécriture sync annulée.")

        track_type = str(track_type or "").strip().lower()
        codec_key = normalized_rewrite_codec(codec)
        source = Path(str(source_path))
        token = token or sync_rewrite_output_token(source, stream_index, track_type)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        if track_type == "subtitle":
            if codec_key not in REWRITE_SUBTITLE_CODECS:
                self._log(
                    "Sync réelle ignorée: sous-titre non texte ou non supporté "
                    f"(codec={codec or 'inconnu'}, stream={stream_index})."
                )
                return None
            self._emit_stage_progress(
                track_type="subtitle",
                title=title,
                stream_index=int(stream_index),
            )
            out = self._rewrite_subtitle(
                source=source,
                stream_index=int(stream_index),
                codec_key=codec_key,
                offset_ms=int(offset_ms),
                tmp_dir=tmp_dir,
                token=token,
                cancel_cb=cancel_cb,
            )
            self._log(
                "Sync réelle sous-titre: timestamps réécrits "
                f"(stream={stream_index}, offset={int(offset_ms)} ms)."
            )
            return SyncRewritePreparedInput(
                path=out,
                input_idx=int(input_idx),
                track_type="subtitle",
                codec=codec_key,
                mode_label="Sync réelle",
            )

        if track_type == "audio":
            probe = self._probe_stream(source, int(stream_index))
            source_codec_key = normalized_rewrite_codec(str(probe.get("codec_name") or codec_key))
            object_audio = self._audio_probe_has_object_metadata(
                codec_key=source_codec_key,
                title=title,
                display_info=display_info,
                probe=probe,
            ) or track_has_object_audio_metadata(
                codec=codec,
                title=title,
                display_info=display_info,
            )
            try:
                channels_count = int(str(probe.get("channels") or "0"))
            except ValueError:
                channels_count = 0
            if source_codec_key in REWRITE_AUDIO_CODECS and not object_audio:
                if channels_count <= 0:
                    self._log(
                        "Sync réelle ignorée: audio AC3/EAC3/AAC non prouvé simple "
                        f"(stream={stream_index}); fallback offset."
                    )
                    return None
                self._emit_stage_progress(
                    track_type="audio",
                    title=title,
                    stream_index=int(stream_index),
                    probe=probe,
                )
                rewrite_codec_key = source_codec_key
                rewrite_bitrate_kbps: int | None = None
                if preserve_source_audio_params:
                    rewrite_bitrate_kbps = self._source_audio_bitrate_kbps(
                        codec_key=source_codec_key,
                        probe=probe,
                        display_info=display_info,
                        channels=channels_count,
                    )
                    if rewrite_bitrate_kbps is None:
                        self._log(
                            "Sync réelle audio: bitrate source introuvable; "
                            "fallback sur le débit configuré."
                        )
                else:
                    requested_codec = normalized_rewrite_codec(audio_target_codec or codec_key)
                    if requested_codec not in REWRITE_AUDIO_CODECS:
                        self._log(
                            "Sync réelle ignorée: codec audio cible non éligible "
                            f"(codec={requested_codec or 'inconnu'}, stream={stream_index}); fallback offset."
                        )
                        return None
                    rewrite_codec_key = requested_codec
                    rewrite_bitrate_kbps = self._normalize_audio_bitrate_kbps(
                        rewrite_codec_key,
                        audio_target_bitrate_kbps,
                        channels_count,
                    )
                out = self._rewrite_audio(
                    source=source,
                    stream_index=int(stream_index),
                    codec_key=rewrite_codec_key,
                    offset_ms=int(offset_ms),
                    tmp_dir=tmp_dir,
                    token=token,
                    channels=channels_count,
                    bitrate_kbps=rewrite_bitrate_kbps,
                    cancel_cb=cancel_cb,
                )
                bitrate_label = f", bitrate={rewrite_bitrate_kbps} kbps" if rewrite_bitrate_kbps else ""
                self._log(
                    "Sync réelle audio: piste réencodée après coupe/silence "
                    f"(codec={rewrite_codec_key.upper()}{bitrate_label}, stream={stream_index}, "
                    f"offset={int(offset_ms)} ms)."
                )
                return SyncRewritePreparedInput(
                    path=out,
                    input_idx=int(input_idx),
                    track_type="audio",
                    codec=rewrite_codec_key,
                    mode_label="Sync réelle · audio réencodé",
                    bitrate_kbps=rewrite_bitrate_kbps,
                )

            if not self._advanced_audio_enabled:
                self._log(
                    "Sync réelle ignorée: audio avancé désactivé "
                    f"(codec={source_codec_key or codec or 'inconnu'}, stream={stream_index})."
                )
                return None

            kind = _advanced_audio_kind(source_codec_key, object_audio=object_audio)
            if not kind or not _advanced_audio_kind_supports_offset(kind, int(offset_ms)):
                self._log(
                    "Sync réelle audio avancée ignorée: stratégie absente ou non sûre "
                    f"(codec={source_codec_key or codec or 'inconnu'}, stream={stream_index}, "
                    f"offset={int(offset_ms)} ms); fallback offset."
                )
                return None

            self._emit_stage_progress(
                track_type="audio",
                title=title,
                stream_index=int(stream_index),
                probe=probe,
            )
            tooltip, precision, residual = _advanced_audio_warning_tooltip(
                codec_key=source_codec_key,
                kind=kind,
                offset_ms=int(offset_ms),
            )
            _ = tooltip
            if kind in {"eac3_joc", "dts", "dts_object", "truehd_object"}:
                out = self._copy_trim_audio(
                    source=source,
                    stream_index=int(stream_index),
                    offset_ms=int(offset_ms),
                    tmp_dir=tmp_dir,
                    token=token,
                    cancel_cb=cancel_cb,
                )
                self._log(
                    "Sync réelle audio avancée: piste copiée/coupée sans réencodage "
                    f"(codec={source_codec_key.upper()}, stream={stream_index}, "
                    f"offset={int(offset_ms)} ms, précision={precision}, résiduel={residual})."
                )
                return SyncRewritePreparedInput(
                    path=out,
                    input_idx=int(input_idx),
                    track_type="audio",
                    codec=source_codec_key,
                    mode_label=SYNC_REWRITE_ADVANCED_AUDIO_LABEL,
                    strategy=kind,
                    is_advanced=True,
                    precision_label=precision,
                    residual_label=residual,
                )

            rewrite_codec_key = self._advanced_audio_output_codec(source_codec_key, kind)
            if kind == "truehd" and not self._truehd_reencode_supported(probe):
                self._log(
                    "Sync réelle audio avancée ignorée: TrueHD/MLP sans Atmos hors layout "
                    f"supporté (stream={stream_index}); fallback offset."
                )
                return None
            rewrite_bitrate_kbps = self._advanced_audio_bitrate_kbps(
                codec_key=rewrite_codec_key,
                source_codec_key=source_codec_key,
                kind=kind,
                audio_target_bitrate_kbps=audio_target_bitrate_kbps,
                probe=probe,
                display_info=display_info,
                channels=channels_count,
            )
            out = self._rewrite_audio_advanced(
                source=source,
                stream_index=int(stream_index),
                codec_key=rewrite_codec_key,
                offset_ms=int(offset_ms),
                tmp_dir=tmp_dir,
                token=token,
                bitrate_kbps=rewrite_bitrate_kbps,
                experimental=kind == "truehd",
                cancel_cb=cancel_cb,
            )
            bitrate_label = f", bitrate={rewrite_bitrate_kbps} kbps" if rewrite_bitrate_kbps else ""
            self._log(
                "Sync réelle audio avancée: piste matérialisée après coupe/silence "
                f"(codec={rewrite_codec_key.upper()}{bitrate_label}, stream={stream_index}, "
                f"offset={int(offset_ms)} ms, précision={precision}, résiduel={residual})."
            )
            return SyncRewritePreparedInput(
                path=out,
                input_idx=int(input_idx),
                track_type="audio",
                codec=rewrite_codec_key,
                mode_label=SYNC_REWRITE_ADVANCED_AUDIO_LABEL,
                bitrate_kbps=rewrite_bitrate_kbps,
                strategy=kind,
                is_advanced=True,
                precision_label=precision,
                residual_label=residual,
            )

        return None

    def _probe_stream(self, source: Path, stream_index: int) -> dict[str, object]:
        cmd = [
            self._ffprobe,
            "-v", "error",
            "-show_entries",
            "stream=index,codec_name,codec_long_name,profile,channels,channel_layout,bit_rate,sample_rate:stream_tags=title",
            "-of", "json",
        ]
        cmd.extend(ffprobe_input_args(source))
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=30,
            **subprocess_text_kwargs(),
        )
        if result.returncode != 0:
            return {}
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return {}
        for stream in payload.get("streams") or []:
            try:
                if int(stream.get("index", -1)) == int(stream_index):
                    return dict(stream)
            except (TypeError, ValueError):
                continue
        return {}

    def _audio_probe_has_object_metadata(
        self,
        *,
        codec_key: str,
        title: str,
        display_info: str,
        probe: Mapping[str, object],
    ) -> bool:
        tags = probe.get("tags") if isinstance(probe.get("tags"), dict) else {}
        probe_title = str(tags.get("title") or "") if isinstance(tags, dict) else ""
        return track_has_object_audio_metadata(
            codec=codec_key,
            title=" ".join([title, probe_title]),
            display_info=display_info,
            profile=str(probe.get("profile") or ""),
            codec_long_name=str(probe.get("codec_long_name") or ""),
        )

    def _audio_probe_is_simple(
        self,
        *,
        codec_key: str,
        title: str,
        display_info: str,
        probe: Mapping[str, object],
    ) -> bool:
        try:
            channels = int(str(probe.get("channels") or "0"))
        except ValueError:
            channels = 0
        if channels <= 0:
            return False
        return not self._audio_probe_has_object_metadata(
            codec_key=codec_key,
            title=title,
            display_info=display_info,
            probe=probe,
        )

    def _rewrite_audio(
        self,
        *,
        source: Path,
        stream_index: int,
        codec_key: str,
        offset_ms: int,
        tmp_dir: Path,
        token: str,
        channels: int,
        bitrate_kbps: int | None = None,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> Path:
        destination = self._unique_path(tmp_dir, f"sync_rewrite_{token}.mka")
        if offset_ms > 0:
            audio_filter = f"adelay={int(offset_ms)}:all=1,asetpts=PTS-STARTPTS"
        else:
            audio_filter = f"atrim=start={abs(offset_ms) / 1000.0:.3f},asetpts=PTS-STARTPTS"
        bitrate = bitrate_kbps if bitrate_kbps and bitrate_kbps > 0 else self._audio_bitrate_kbps(codec_key, channels)
        cmd = [
            self._ffmpeg,
            "-hide_banner", "-y",
            *self._progress_args,
        ]
        append_ffmpeg_input_args(cmd, source)
        cmd.extend([
            "-map", f"0:{stream_index}",
            "-vn", "-sn", "-dn",
            *self._thread_args,
            "-af", audio_filter,
            "-c:a", codec_key,
            "-b:a", f"{bitrate}k",
            "-f", "matroska",
            str(destination),
        ])
        self._run_checked(cmd, destination, "Réécriture sync audio échouée", cancel_cb=cancel_cb)
        return destination

    @staticmethod
    def _advanced_audio_output_codec(source_codec_key: str, kind: str) -> str:
        if kind == "lossless_to_flac":
            return "flac"
        if source_codec_key == "wv":
            return "wavpack"
        return source_codec_key

    @staticmethod
    def _truehd_reencode_supported(probe: Mapping[str, object]) -> bool:
        try:
            channels = int(str(probe.get("channels") or "0"))
        except ValueError:
            channels = 0
        # FFmpeg's TrueHD/MLP encoder is experimental and currently tops out at
        # 5.1-class layouts. Refuse unknown/7.1+ layouts instead of guessing.
        return 0 < channels <= 6

    def _advanced_audio_bitrate_kbps(
        self,
        *,
        codec_key: str,
        source_codec_key: str,
        kind: str,
        audio_target_bitrate_kbps: int | None,
        probe: Mapping[str, object],
        display_info: str,
        channels: int,
    ) -> int | None:
        if kind != "lossy":
            return None
        requested = self._normalize_audio_bitrate_kbps(codec_key, audio_target_bitrate_kbps, channels)
        if requested is not None:
            return requested
        source = self._source_audio_bitrate_kbps(
            codec_key=source_codec_key,
            probe=probe,
            display_info=display_info,
            channels=channels,
        )
        if source is not None:
            return source
        return self._audio_bitrate_kbps_for(codec_key, channels, 96)

    @staticmethod
    def _advanced_audio_encoder_args(codec_key: str, *, bitrate_kbps: int | None, experimental: bool) -> list[str]:
        encoder = {
            "mp3": "libmp3lame",
            "opus": "libopus",
            "vorbis": "libvorbis",
            "wv": "wavpack",
        }.get(codec_key, codec_key)
        args: list[str] = []
        if experimental:
            args.extend(["-strict", "-2"])
        args.extend(["-c:a", encoder])
        if bitrate_kbps and bitrate_kbps > 0:
            args.extend(["-b:a", f"{int(bitrate_kbps)}k"])
        return args

    def _rewrite_audio_advanced(
        self,
        *,
        source: Path,
        stream_index: int,
        codec_key: str,
        offset_ms: int,
        tmp_dir: Path,
        token: str,
        bitrate_kbps: int | None = None,
        experimental: bool = False,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> Path:
        destination = self._unique_path(tmp_dir, f"sync_rewrite_{token}.mka")
        if offset_ms > 0:
            audio_filter = f"adelay={int(offset_ms)}:all=1,asetpts=PTS-STARTPTS"
        else:
            audio_filter = f"atrim=start={abs(offset_ms) / 1000.0:.3f},asetpts=PTS-STARTPTS"
        cmd = [
            self._ffmpeg,
            "-hide_banner", "-y",
            *self._progress_args,
        ]
        append_ffmpeg_input_args(cmd, source)
        cmd.extend([
            "-map", f"0:{stream_index}",
            "-vn", "-sn", "-dn",
            *self._thread_args,
            "-af", audio_filter,
            *self._advanced_audio_encoder_args(
                codec_key,
                bitrate_kbps=bitrate_kbps,
                experimental=experimental,
            ),
            "-f", "matroska",
            str(destination),
        ])
        self._run_checked(cmd, destination, "Réécriture sync audio avancée échouée", cancel_cb=cancel_cb)
        return destination

    def _copy_trim_audio(
        self,
        *,
        source: Path,
        stream_index: int,
        offset_ms: int,
        tmp_dir: Path,
        token: str,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> Path:
        if int(offset_ms or 0) >= 0:
            raise RemuxError("Copie/coupe audio avancée impossible pour un retard positif.")
        destination = self._unique_path(tmp_dir, f"sync_rewrite_{token}.mka")
        cmd = [
            self._ffmpeg,
            "-hide_banner", "-y",
            *self._progress_args,
        ]
        append_ffmpeg_input_args(cmd, source)
        cmd.extend([
            "-map", f"0:{stream_index}",
            "-vn", "-sn", "-dn",
            "-ss", f"{abs(int(offset_ms)) / 1000.0:.3f}",
            "-c:a", "copy",
            "-avoid_negative_ts", "make_zero",
            "-f", "matroska",
            str(destination),
        ])
        self._run_checked(cmd, destination, "Copie/coupe sync audio avancée échouée", cancel_cb=cancel_cb)
        return destination

    def _rewrite_subtitle(
        self,
        *,
        source: Path,
        stream_index: int,
        codec_key: str,
        offset_ms: int,
        tmp_dir: Path,
        token: str,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> Path:
        text_kind, ext, codec_arg = self._subtitle_text_plan(codec_key)
        extracted = self._unique_path(tmp_dir, f"sync_rewrite_{token}_raw{ext}")
        shifted = self._unique_path(tmp_dir, f"sync_rewrite_{token}_shifted{ext}")
        destination = self._unique_path(tmp_dir, f"sync_rewrite_{token}.mks")
        extract_cmd = [
            self._ffmpeg,
            "-hide_banner", "-y",
            *self._progress_args,
        ]
        append_ffmpeg_input_args(extract_cmd, source)
        extract_cmd.extend([
            "-map", f"0:{stream_index}",
            "-c:s", codec_arg,
            str(extracted),
        ])
        self._run_checked(extract_cmd, extracted, "Extraction sous-titre pour sync réelle échouée", cancel_cb=cancel_cb)
        text = extracted.read_text(encoding="utf-8-sig", errors="replace")
        shifted.write_text(self._shift_subtitle_text(text, text_kind, offset_ms), encoding="utf-8")
        wrap_cmd = [
            self._ffmpeg,
            "-hide_banner", "-y",
            *self._progress_args,
            "-i", str(shifted),
            "-map", "0:0",
            "-c:s", "copy",
            "-f", "matroska",
            str(destination),
        ]
        self._run_checked(wrap_cmd, destination, "Encapsulation sous-titre sync réelle échouée", cancel_cb=cancel_cb)
        return destination

    @staticmethod
    def _subtitle_text_plan(codec_key: str) -> tuple[str, str, str]:
        if codec_key in {"ass", "ssa"}:
            return "ass", ".ass", "copy"
        if codec_key == "webvtt":
            return "webvtt", ".vtt", "copy"
        if codec_key in {"subrip", "srt"}:
            return "srt", ".srt", "copy"
        return "srt", ".srt", "srt"

    def _emit_stage_progress(
        self,
        *,
        track_type: str,
        title: str,
        stream_index: int,
        probe: Mapping[str, object] | None = None,
    ) -> None:
        if self._progress is None:
            return
        self._progress(sync_rewrite_stage_progress_line(
            track_type,
            self._progress_track_name(title=title, stream_index=stream_index, probe=probe),
        ))

    @staticmethod
    def _progress_track_name(
        *,
        title: str,
        stream_index: int,
        probe: Mapping[str, object] | None = None,
    ) -> str:
        tags = probe.get("tags") if isinstance(probe, Mapping) and isinstance(probe.get("tags"), Mapping) else {}
        probe_title = str(tags.get("title") or "") if isinstance(tags, Mapping) else ""
        name = " ".join(str(title or probe_title or "").split())
        return name or f"#{int(stream_index)}"

    def _emit_tool_progress(self, line: str) -> None:
        if self._progress is not None:
            self._progress(line)
        else:
            self._log(line)

    def _run_checked(
        self,
        cmd: list[str],
        destination: Path,
        error_prefix: str,
        *,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> None:
        self._emit_tool_progress("$ " + " ".join(str(part) for part in cmd))
        with subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **subprocess_windows_no_window_kwargs(),
        ) as proc:
            assert proc.stdout is not None
            lines: list[str] = []
            buf = b""
            while chunk := proc.stdout.read(256):
                if cancel_cb is not None and cancel_cb():
                    proc.kill()
                    raise RemuxError("Réécriture sync annulée.")
                chunk_bytes: bytes
                if isinstance(chunk, str):
                    chunk_bytes = chunk.encode()
                else:
                    chunk_bytes = cast(bytes, chunk)
                buf += chunk_bytes.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                *complete, buf = buf.split(b"\n")
                for raw in complete:
                    stripped = decode_subprocess_output(raw).rstrip()
                    if not stripped:
                        continue
                    lines.append(stripped)
                    self._emit_tool_progress(stripped)
            if buf.strip():
                stripped = decode_subprocess_output(buf.strip())
                if stripped:
                    lines.append(stripped)
                    self._emit_tool_progress(stripped)
            proc.wait()

        output = "\n".join(lines[-10000:])
        if proc.returncode != 0 or not destination.exists() or destination.stat().st_size == 0:
            raise RemuxError(f"{error_prefix}: {output[-2000:].strip()}")

    @staticmethod
    def _audio_bitrate_kbps_for(codec_key: str, channels: int, per_channel: int) -> int:
        bitrate = max(1, int(channels or 2)) * max(1, int(per_channel or 96))
        if codec_key == "ac3":
            return min(640, bitrate)
        if codec_key == "eac3":
            return min(6144, bitrate)
        return bitrate

    def _audio_bitrate_kbps(self, codec_key: str, channels: int) -> int:
        return self._audio_bitrate_kbps_for(
            codec_key,
            channels,
            self._audio_bitrate_per_channel.get(codec_key, 96),
        )

    @classmethod
    def _normalize_audio_bitrate_kbps(
        cls,
        codec_key: str,
        bitrate_kbps: int | None,
        channels: int,
    ) -> int | None:
        try:
            bitrate = int(bitrate_kbps or 0)
        except (TypeError, ValueError):
            bitrate = 0
        if bitrate <= 0:
            return None
        if codec_key == "ac3":
            return min(_AC3_STANDARD_BITRATES_KBPS, key=lambda choice: abs(choice - bitrate))
        if codec_key == "eac3":
            return min(6144, bitrate)
        if codec_key == "aac":
            return max(1, bitrate)
        return max(1, bitrate)

    @classmethod
    def _source_audio_bitrate_kbps(
        cls,
        *,
        codec_key: str,
        probe: Mapping[str, object],
        display_info: str,
        channels: int,
    ) -> int | None:
        raw_bps = probe.get("bit_rate")
        try:
            bitrate_bps = int(str(raw_bps or "0"))
        except ValueError:
            bitrate_bps = 0
        if bitrate_bps > 0:
            return cls._normalize_audio_bitrate_kbps(
                codec_key,
                max(1, round(bitrate_bps / 1000)),
                channels,
            )
        bitrate = audio_bitrate_kbps_from_display_info(display_info)
        if bitrate is None:
            return None
        return cls._normalize_audio_bitrate_kbps(codec_key, bitrate, channels)

    @classmethod
    def _shift_subtitle_text(cls, text: str, kind: str, offset_ms: int) -> str:
        if kind == "ass":
            return cls._shift_ass(text, offset_ms)
        if kind == "webvtt":
            return cls._shift_webvtt(text, offset_ms)
        return cls._shift_srt(text, offset_ms)

    @staticmethod
    def _shift_ms(start_ms: int, end_ms: int, offset_ms: int) -> tuple[int, int] | None:
        new_start = int(start_ms) + int(offset_ms)
        new_end = int(end_ms) + int(offset_ms)
        if new_end <= 0:
            return None
        new_start = max(0, new_start)
        if new_end <= new_start:
            return None
        return new_start, new_end

    @classmethod
    def _shift_srt(cls, text: str, offset_ms: int) -> str:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        blocks = re.split(r"\n{2,}", normalized.strip())
        out: list[str] = []
        index = 1
        for block in blocks:
            lines = block.split("\n")
            time_idx = next((i for i, line in enumerate(lines) if "-->" in line), -1)
            if time_idx < 0:
                continue
            parsed = cls._parse_timing_line(lines[time_idx], comma=True)
            if parsed is None:
                continue
            start_ms, end_ms, suffix = parsed
            shifted = cls._shift_ms(start_ms, end_ms, offset_ms)
            if shifted is None:
                continue
            body = lines[time_idx + 1:]
            out.append("\n".join([
                str(index),
                f"{cls._format_srt_time(shifted[0])} --> {cls._format_srt_time(shifted[1])}{suffix}",
                *body,
            ]))
            index += 1
        return "\n\n".join(out) + ("\n" if out else "")

    @classmethod
    def _shift_webvtt(cls, text: str, offset_ms: int) -> str:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        blocks = re.split(r"\n{2,}", normalized.strip())
        out: list[str] = []
        for block in blocks:
            lines = block.split("\n")
            time_idx = next((i for i, line in enumerate(lines) if "-->" in line), -1)
            if time_idx < 0:
                out.append(block)
                continue
            parsed = cls._parse_timing_line(lines[time_idx], comma=False)
            if parsed is None:
                out.append(block)
                continue
            shifted = cls._shift_ms(parsed[0], parsed[1], offset_ms)
            if shifted is None:
                continue
            lines[time_idx] = (
                f"{cls._format_vtt_time(shifted[0])} --> {cls._format_vtt_time(shifted[1])}{parsed[2]}"
            )
            out.append("\n".join(lines))
        return "\n\n".join(out).rstrip() + "\n"

    @classmethod
    def _shift_ass(cls, text: str, offset_ms: int) -> str:
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        start_idx = 1
        end_idx = 2
        out: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.lower().startswith("format:"):
                fields = [part.strip().lower() for part in stripped.split(":", 1)[1].split(",")]
                if "start" in fields and "end" in fields:
                    start_idx = fields.index("start")
                    end_idx = fields.index("end")
                out.append(line)
                continue
            if not stripped.lower().startswith("dialogue:"):
                out.append(line)
                continue
            prefix, payload = line.split(":", 1)
            field_count = max(start_idx, end_idx) + 1
            parts = payload.lstrip().split(",", field_count)
            if len(parts) <= max(start_idx, end_idx):
                out.append(line)
                continue
            start_ms = cls._parse_ass_time(parts[start_idx].strip())
            end_ms = cls._parse_ass_time(parts[end_idx].strip())
            if start_ms is None or end_ms is None:
                out.append(line)
                continue
            shifted = cls._shift_ms(start_ms, end_ms, offset_ms)
            if shifted is None:
                continue
            parts[start_idx] = cls._format_ass_time(shifted[0])
            parts[end_idx] = cls._format_ass_time(shifted[1])
            out.append(f"{prefix}: {','.join(parts)}")
        return "\n".join(out).rstrip() + "\n"

    @staticmethod
    def _parse_timing_line(line: str, *, comma: bool) -> tuple[int, int, str] | None:
        match = re.match(r"\s*(\S+)\s+-->\s+(\S+)(.*)$", line)
        if not match:
            return None
        start = SyncRewriteService._parse_sub_time(match.group(1), comma=comma)
        end = SyncRewriteService._parse_sub_time(match.group(2), comma=comma)
        if start is None or end is None:
            return None
        return start, end, match.group(3)

    @staticmethod
    def _parse_sub_time(value: str, *, comma: bool) -> int | None:
        sep = "," if comma else "."
        pattern = rf"^(?:(\d+):)?(\d{{2}}):(\d{{2}}){re.escape(sep)}(\d{{3}})$"
        match = re.match(pattern, value.strip())
        if not match:
            return None
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2))
        seconds = int(match.group(3))
        millis = int(match.group(4))
        return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis

    @staticmethod
    def _format_srt_time(ms: int) -> str:
        base = SyncRewriteService._split_ms(ms)
        return f"{base[0]:02d}:{base[1]:02d}:{base[2]:02d},{base[3]:03d}"

    @staticmethod
    def _format_vtt_time(ms: int) -> str:
        base = SyncRewriteService._split_ms(ms)
        return f"{base[0]:02d}:{base[1]:02d}:{base[2]:02d}.{base[3]:03d}"

    @staticmethod
    def _split_ms(ms: int) -> tuple[int, int, int, int]:
        value = max(0, int(ms))
        millis = value % 1000
        total_seconds = value // 1000
        seconds = total_seconds % 60
        total_minutes = total_seconds // 60
        minutes = total_minutes % 60
        hours = total_minutes // 60
        return hours, minutes, seconds, millis

    @staticmethod
    def _parse_ass_time(value: str) -> int | None:
        match = re.match(r"^(\d+):(\d{2}):(\d{2})[.](\d{1,2})$", value.strip())
        if not match:
            return None
        centis = int(match.group(4).ljust(2, "0")[:2])
        return ((int(match.group(1)) * 60 + int(match.group(2))) * 60 + int(match.group(3))) * 1000 + centis * 10

    @staticmethod
    def _format_ass_time(ms: int) -> str:
        hours, minutes, seconds, millis = SyncRewriteService._split_ms(ms)
        return f"{hours}:{minutes:02d}:{seconds:02d}.{millis // 10:02d}"

    @staticmethod
    def _unique_path(base_dir: Path, filename: str) -> Path:
        candidate = base_dir / filename
        if not candidate.exists():
            return candidate
        stem = candidate.stem
        suffix = candidate.suffix
        index = 1
        while True:
            alt = base_dir / f"{stem}_{index}{suffix}"
            if not alt.exists():
                return alt
            index += 1


__all__ = [
    "REWRITE_AUDIO_CODECS",
    "REWRITE_SUBTITLE_CODECS",
    "SYNC_REWRITE_MODE_AUTO",
    "SYNC_REWRITE_MODE_OFFSET",
    "SYNC_REWRITE_ADVANCED_AUDIO_LABEL",
    "SYNC_REWRITE_OFFSET_LABEL",
    "SYNC_REWRITE_STAGE_PREFIX",
    "SyncRewritePreparedInput",
    "SyncRewriteService",
    "SyncRewriteUiPreview",
    "audio_bitrate_kbps_from_display_info",
    "normalized_rewrite_codec",
    "normalized_sync_rewrite_mode",
    "sync_rewrite_forced_offset",
    "sync_rewrite_stage_progress_line",
    "sync_rewrite_output_token",
    "track_has_object_audio_metadata",
    "ui_sync_rewrite_auto_label_for_track",
    "ui_sync_rewrite_can_toggle",
    "ui_sync_rewrite_label_for_track",
    "ui_sync_rewrite_preview_for_track",
]
