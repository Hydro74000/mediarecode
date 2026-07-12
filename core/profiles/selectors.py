"""Stable track selectors and exact-job export helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from core.lang_tags import Rfc5646LanguageTags
from core.workflows.common.sync_rewrite import (
    audio_bitrate_kbps_from_display_info,
    normalized_sync_rewrite_mode,
)
from core.workflows.remux_models import RemuxConfig, TrackEntry


FLAG_NAMES = (
    "enabled",
    "default",
    "forced",
    "hearing_impaired",
    "visual_impaired",
    "original",
    "commentary",
)

VIDEO_FLAG_RES_SD = 0x00000001
VIDEO_FLAG_RES_HD = 0x00000002
VIDEO_FLAG_RES_FHD = 0x00000004
VIDEO_FLAG_RES_UHD = 0x00000008
VIDEO_FLAG_HDR = 0x00000010
VIDEO_FLAG_HDR10 = 0x00000020
VIDEO_FLAG_HDR10PLUS = 0x00000040
VIDEO_FLAG_DOLBY_VISION = 0x00000080
VIDEO_FLAG_HLG = 0x00000100
VIDEO_FLAG_SDR = 0x00000200
VIDEO_FLAG_BIT_DEPTH_8 = 0x00001000
VIDEO_FLAG_BIT_DEPTH_10 = 0x00002000
VIDEO_FLAG_BIT_DEPTH_12 = 0x00004000
VIDEO_FLAG_HEX_WIDTH = 8


class SelectorResolutionError(RuntimeError):
    """Raised when an exact-job track selector cannot be resolved safely."""

    def __init__(self, message: str, *, report: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.report = report or {"valid": False, "errors": [message]}


def normalize_lang(tag: str | None, title: str | None = None) -> str:
    if not tag:
        return ""
    regionalized = Rfc5646LanguageTags.regionalize_track_language(str(tag), title)
    if regionalized:
        return regionalized
    canonical = Rfc5646LanguageTags.normalize(str(tag))
    return canonical or str(tag).strip()


def _channels_from_display(display_info: str) -> str:
    text = str(display_info or "")
    match = re.search(r"\b(?:mono|stereo|[1-9](?:\.[0-9])?)\b", text, flags=re.IGNORECASE)
    return match.group(0) if match else ""


def _audio_object_from_display(display_info: str) -> str:
    text = str(display_info or "").lower()
    if "atmos" in text:
        return "Atmos"
    if "dts:x" in text or "dtsx" in text:
        return "DTS:X"
    return ""


def _source_index_for_track(
    track: TrackEntry,
    source_index_by_file_id: Mapping[str, int] | None = None,
) -> int | None:
    if source_index_by_file_id and track.file_id in source_index_by_file_id:
        return int(source_index_by_file_id[track.file_id])
    file_id = str(track.file_id or "")
    if file_id.startswith("src"):
        try:
            return int(file_id[3:])
        except ValueError:
            return None
    return None


def _flag_value(track: TrackEntry, name: str, *, original: bool = True) -> bool:
    attr = f"{'orig_' if original else ''}flag_{name}"
    if name == "enabled":
        attr = f"{'orig_' if original else ''}flag_enabled"
    if not hasattr(track, attr):
        attr = f"flag_{name}"
    return bool(getattr(track, attr, False))


def _track_position(
    track: TrackEntry,
    tracks: list[TrackEntry],
    *,
    source_index: int | None,
    source_index_by_file_id: Mapping[str, int] | None = None,
) -> int:
    position = 0
    for candidate in tracks:
        candidate_source = _source_index_for_track(candidate, source_index_by_file_id)
        if candidate is track:
            return position
        if candidate.track_type == track.track_type and candidate_source == source_index:
            position += 1
    return position


def _video_text(track: TrackEntry) -> str:
    return " ".join(
        str(part or "")
        for part in (
            track.orig_display_info,
            track.display_info,
            track.orig_title,
            track.title,
            track.orig_codec,
            track.codec,
        )
    )


def _resolution_from_text(text: str) -> tuple[int, int] | None:
    match = re.search(r"\b(\d{3,5})\s*[xX\u00d7]\s*(\d{3,5})\b", text)
    if match:
        try:
            width = int(match.group(1))
            height = int(match.group(2))
        except ValueError:
            return None
        return (width, height) if width > 0 and height > 0 else None
    match = re.search(r"\b(720|1080|1440|2160|4320)p\b", text, flags=re.IGNORECASE)
    if not match:
        return None
    height = int(match.group(1))
    width = {
        720: 1280,
        1080: 1920,
        1440: 2560,
        2160: 3840,
        4320: 7680,
    }.get(height, 0)
    return (width, height) if width else None


def _video_resolution(track: TrackEntry) -> tuple[int, int] | None:
    return _resolution_from_text(_video_text(track))


def _resolution_bucket(width: int, height: int) -> str:
    longest = max(width, height)
    shortest = min(width, height)
    if longest >= 7000 or shortest >= 4000:
        return "8k"
    if longest >= 3000 or shortest >= 1800:
        return "uhd"
    if shortest >= 1000:
        return "fhd"
    if shortest >= 650:
        return "hd"
    return "sd"


def _resolution_flag(width: int, height: int) -> int:
    bucket = _resolution_bucket(width, height)
    if bucket in {"8k", "uhd"}:
        return VIDEO_FLAG_RES_UHD
    if bucket == "fhd":
        return VIDEO_FLAG_RES_FHD
    if bucket == "hd":
        return VIDEO_FLAG_RES_HD
    return VIDEO_FLAG_RES_SD


def _video_resolution_payload(track: TrackEntry) -> dict[str, Any]:
    resolution = _video_resolution(track)
    if not resolution:
        return {}
    width, height = resolution
    return {
        "width": width,
        "height": height,
        "bucket": _resolution_bucket(width, height),
    }


def _video_characteristic_flags(track: TrackEntry) -> int:
    if track.track_type != "video":
        return 0
    flags = 0
    resolution = _video_resolution(track)
    if resolution:
        flags |= _resolution_flag(*resolution)

    text = _video_text(track).lower()
    has_dv = "dolby vision" in text or "dovi" in text
    has_hdr10plus = "hdr10+" in text or "hdr10plus" in text
    has_hdr10 = bool(re.search(r"\bhdr\s*10\b", text)) or "hdr10" in text
    has_hlg = bool(re.search(r"\bhlg\b", text))
    has_hdr = has_dv or has_hdr10plus or has_hdr10 or has_hlg or bool(re.search(r"\bhdr\b", text))

    if has_hdr:
        flags |= VIDEO_FLAG_HDR
    if has_dv:
        flags |= VIDEO_FLAG_DOLBY_VISION
    if has_hdr10plus:
        flags |= VIDEO_FLAG_HDR10PLUS
    elif has_hdr10:
        flags |= VIDEO_FLAG_HDR10
    if has_hlg:
        flags |= VIDEO_FLAG_HLG
    if not has_hdr:
        flags |= VIDEO_FLAG_SDR

    if re.search(r"\b(?:8[\s-]*bit|8\s*bits|yuv\d*p8)\b", text):
        flags |= VIDEO_FLAG_BIT_DEPTH_8
    if re.search(r"\b(?:10[\s-]*bit|10\s*bits|main\s*10|yuv\d*p10)\b", text):
        flags |= VIDEO_FLAG_BIT_DEPTH_10
    if re.search(r"\b(?:12[\s-]*bit|12\s*bits|yuv\d*p12)\b", text):
        flags |= VIDEO_FLAG_BIT_DEPTH_12
    return flags


def _video_characteristic_flags_hex(track: TrackEntry) -> str:
    return f"0x{_video_characteristic_flags(track):0{VIDEO_FLAG_HEX_WIDTH}X}"


def _parse_video_flags(raw: Any) -> int:
    if isinstance(raw, int):
        return raw
    text = str(raw or "").strip()
    if not text:
        return 0
    try:
        return int(text, 16 if text.lower().startswith("0x") else 10)
    except ValueError:
        return 0


def _selector_resolution(selector: Mapping[str, Any]) -> tuple[int, int] | None:
    raw = selector.get("resolution")
    if isinstance(raw, Mapping):
        try:
            width = int(raw.get("width") or 0)
            height = int(raw.get("height") or 0)
        except (TypeError, ValueError):
            return None
        return (width, height) if width > 0 and height > 0 else None
    if isinstance(raw, str):
        return _resolution_from_text(raw)
    return None


def track_summary(
    track: TrackEntry,
    *,
    source_index_by_file_id: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    summary = {
        "source": _source_index_for_track(track, source_index_by_file_id),
        "id": track.mkv_tid,
        "type": track.track_type,
        "codec": track.codec,
        "language": normalize_lang(track.language, track.title),
        "title": track.title,
        "display_info": track.display_info,
        "flags": {name: _flag_value(track, name, original=False) for name in FLAG_NAMES},
    }
    if track.track_type == "video":
        resolution = _video_resolution_payload(track)
        if resolution:
            summary["resolution"] = resolution
        summary["video_flags_hex"] = _video_characteristic_flags_hex(track)
    return summary


def track_selector_for_entry(
    track: TrackEntry,
    *,
    source_index: int | None = None,
    tracks: list[TrackEntry] | None = None,
    source_index_by_file_id: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    resolved_source = source_index if source_index is not None else _source_index_for_track(track, source_index_by_file_id)
    all_tracks = tracks or [track]
    selector: dict[str, Any] = {
        "source": resolved_source,
        "type": track.track_type,
        "position": _track_position(
            track,
            all_tracks,
            source_index=resolved_source,
            source_index_by_file_id=source_index_by_file_id,
        ),
        "codec": track.orig_codec or track.codec,
        "language": normalize_lang(track.orig_language or track.language, track.orig_title or track.title),
    }
    if track.track_type == "audio":
        channels = _channels_from_display(track.orig_display_info or track.display_info)
        if channels:
            selector["channels"] = channels
        audio_object = _audio_object_from_display(track.orig_display_info or track.display_info)
        if audio_object:
            selector["audio_object"] = audio_object
    if track.track_type == "video":
        resolution = _video_resolution_payload(track)
        if resolution:
            selector["resolution"] = resolution
        selector["video_flags_hex"] = _video_characteristic_flags_hex(track)
    if track.orig_title:
        selector["title"] = track.orig_title
    selector["flags"] = {
        name: _flag_value(track, name, original=True)
        for name in FLAG_NAMES
        if name != "enabled" and _flag_value(track, name, original=True)
    }
    return {key: value for key, value in selector.items() if value not in (None, "", {})}


def _selector_source(selector: Mapping[str, Any]) -> int | None:
    raw = selector.get("source", selector.get("source_index"))
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _selector_languages(selector: Mapping[str, Any]) -> set[str]:
    raw = selector.get("languages")
    if isinstance(raw, list):
        return {normalize_lang(str(lang)) for lang in raw if str(lang).strip()}
    raw_lang = selector.get("language", selector.get("lang"))
    if raw_lang is None:
        return set()
    return {normalize_lang(str(raw_lang))}


def _selector_codecs(selector: Mapping[str, Any]) -> set[str]:
    raw = selector.get("codecs")
    if isinstance(raw, list):
        return {str(codec).strip().upper() for codec in raw if str(codec).strip()}
    raw_codec = selector.get("codec")
    if raw_codec is None:
        return set()
    return {str(raw_codec).strip().upper()}


def _matches_selector(
    track: TrackEntry,
    selector: Mapping[str, Any],
    tracks: list[TrackEntry],
    *,
    source_index_by_file_id: Mapping[str, int] | None = None,
) -> bool:
    source = _selector_source(selector)
    track_source = _source_index_for_track(track, source_index_by_file_id)
    if source is not None and track_source != source:
        return False

    track_type = selector.get("type", selector.get("track_type"))
    if track_type and str(track_type) != track.track_type:
        return False

    if "position" in selector or "type_index" in selector:
        raw_position = selector.get("position", selector.get("type_index"))
        try:
            expected_position = int(raw_position)
        except (TypeError, ValueError):
            expected_position = -1
        position = _track_position(
            track,
            tracks,
            source_index=track_source,
            source_index_by_file_id=source_index_by_file_id,
        )
        if position != expected_position:
            return False

    raw_id = selector.get("id", selector.get("mkv_tid", selector.get("stream")))
    if raw_id is not None:
        try:
            if int(raw_id) != int(track.mkv_tid):
                return False
        except (TypeError, ValueError):
            return False

    codecs = _selector_codecs(selector)
    if codecs and str(track.orig_codec or track.codec).upper() not in codecs and track.codec.upper() not in codecs:
        return False

    languages = _selector_languages(selector)
    if languages:
        track_lang = normalize_lang(track.orig_language or track.language, track.orig_title or track.title)
        if track_lang not in languages and normalize_lang(track.language, track.title) not in languages:
            return False

    if "channels" in selector:
        expected = str(selector.get("channels") or "").strip().lower()
        actual = _channels_from_display(track.orig_display_info or track.display_info).lower()
        if expected and actual != expected:
            return False

    if "atmos" in selector:
        has_object = bool(_audio_object_from_display(track.orig_display_info or track.display_info))
        if bool(selector["atmos"]) != has_object:
            return False

    if "audio_object" in selector:
        expected = str(selector.get("audio_object") or "").strip().lower()
        actual = _audio_object_from_display(track.orig_display_info or track.display_info).lower()
        if expected and expected != actual:
            return False

    if "title" in selector:
        expected_title = str(selector.get("title") or "")
        if expected_title not in {track.orig_title, track.title}:
            return False

    if "title_contains" in selector:
        needle = str(selector.get("title_contains") or "").lower()
        haystack = f"{track.orig_title} {track.title}".lower()
        if needle and needle not in haystack:
            return False

    if "display_contains" in selector:
        needle = str(selector.get("display_contains") or "").lower()
        haystack = f"{track.orig_display_info} {track.display_info}".lower()
        if needle and needle not in haystack:
            return False

    expected_resolution = _selector_resolution(selector)
    if expected_resolution and _video_resolution(track) != expected_resolution:
        return False

    expected_video_flags = _parse_video_flags(selector.get("video_flags_hex", selector.get("video_flags")))
    if expected_video_flags and (_video_characteristic_flags(track) & expected_video_flags) != expected_video_flags:
        return False

    flags = selector.get("flags")
    if isinstance(flags, Mapping):
        for name, expected in flags.items():
            if name in FLAG_NAMES and _flag_value(track, str(name), original=True) != bool(expected):
                return False

    entry_id = selector.get("entry_id")
    if entry_id and str(track.entry_id) != str(entry_id):
        return False

    return True


def match_track_selector(
    selector: Mapping[str, Any],
    tracks: list[TrackEntry],
    *,
    source_index_by_file_id: Mapping[str, int] | None = None,
) -> list[TrackEntry]:
    if not isinstance(selector, Mapping):
        return []
    return [
        track
        for track in tracks
        if _matches_selector(
            track,
            selector,
            tracks,
            source_index_by_file_id=source_index_by_file_id,
        )
    ]


def _relaxed_selector_variants(selector: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build safer fallback selectors for reusable GUI exact-job templates."""
    if not isinstance(selector, Mapping):
        return []
    original = dict(selector)
    track_type = str(original.get("type", original.get("track_type")) or "")
    variants: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()

    def add(removals: set[str]) -> None:
        variant = {key: value for key, value in original.items() if key not in removals}
        if variant == original:
            return
        key = tuple(sorted((str(k), repr(v)) for k, v in variant.items()))
        if key in seen:
            return
        seen.add(key)
        variants.append(variant)

    positional = {"position", "type_index", "id", "mkv_tid", "stream", "entry_id"}
    noisy = {"language", "lang", "channels", "audio_object", "title", "title_contains", "display_contains", "flags"}
    if track_type == "video":
        add({"language", "lang", "channels", "audio_object", "title", "title_contains", "display_contains"})
        add({"language", "lang", "channels", "audio_object", "title", "title_contains", "display_contains", "flags"})
        add(noisy)
        add(noisy | {"resolution", "video_flags", "video_flags_hex"})
        return variants

    add(positional)
    add(positional | {"title", "title_contains"})
    add(positional | {"flags"})
    add(positional | {"title", "title_contains", "flags"})
    add(positional | {"title", "title_contains", "flags", "channels", "audio_object"})
    return variants


def resolve_track_selector_relaxed(
    selector: Mapping[str, Any],
    tracks: list[TrackEntry],
    *,
    context: str = "selector",
    source_index_by_file_id: Mapping[str, int] | None = None,
) -> TrackEntry | None:
    """Resolve a selector with deterministic fallbacks for batch templates."""
    full_matches = match_track_selector(selector, tracks, source_index_by_file_id=source_index_by_file_id)
    if len(full_matches) == 1:
        return full_matches[0]

    attempted: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    for variant in _relaxed_selector_variants(selector):
        matches = match_track_selector(variant, tracks, source_index_by_file_id=source_index_by_file_id)
        attempted.append({"selector": variant, "match_count": len(matches)})
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            ambiguous.append(
                {
                    "selector": variant,
                    "match_count": len(matches),
                    "matches": [track_summary(match, source_index_by_file_id=source_index_by_file_id) for match in matches],
                }
            )

    report = {
        "valid": False,
        "error": "track_selector_unmatched_relaxed" if not ambiguous else "track_selector_ambiguous_relaxed",
        "context": context,
        "selector": dict(selector),
        "match_count": len(full_matches),
        "matches": [track_summary(match, source_index_by_file_id=source_index_by_file_id) for match in full_matches],
        "relaxed_attempts": attempted,
    }
    if ambiguous:
        report["relaxed_ambiguous"] = ambiguous
    message = (
        f"Sélecteur de piste introuvable pour {context} même après fallback batch."
        if not ambiguous
        else f"Sélecteur de piste ambigu pour {context} après fallback batch."
    )
    raise SelectorResolutionError(message, report=report)


def resolve_track_selector(
    selector: Mapping[str, Any],
    tracks: list[TrackEntry],
    *,
    context: str = "selector",
    strict: bool = True,
    suggested_profile: str | None = None,
    source_index_by_file_id: Mapping[str, int] | None = None,
) -> TrackEntry | None:
    matches = match_track_selector(selector, tracks, source_index_by_file_id=source_index_by_file_id)
    if len(matches) == 1:
        return matches[0]
    if not strict:
        return matches[0] if matches else None
    error = "track_selector_unmatched" if not matches else "track_selector_ambiguous"
    report = {
        "valid": False,
        "error": error,
        "context": context,
        "selector": dict(selector),
        "match_count": len(matches),
        "matches": [track_summary(match, source_index_by_file_id=source_index_by_file_id) for match in matches],
    }
    if suggested_profile:
        report["suggested_profile"] = suggested_profile
    message = (
        f"Sélecteur de piste introuvable pour {context}."
        if not matches
        else f"Sélecteur de piste ambigu pour {context}: {len(matches)} correspondances."
    )
    raise SelectorResolutionError(message, report=report)


def apply_track_spec(track: TrackEntry, spec: Mapping[str, Any]) -> None:
    if "enabled" in spec:
        track.enabled = bool(spec["enabled"])
    if "language" in spec:
        track.language = normalize_lang(str(spec["language"]), track.title)
    if "title" in spec:
        track.title = str(spec["title"])
    flags = spec.get("flags")
    if isinstance(flags, Mapping):
        for name, value in flags.items():
            if name in FLAG_NAMES:
                setattr(track, f"flag_{name}", bool(value))
    if "time_shift_ms" in spec:
        track.time_shift_ms = int(spec["time_shift_ms"] or 0)
    if "sync_rewrite_mode" in spec:
        track.sync_rewrite_mode = normalized_sync_rewrite_mode(str(spec["sync_rewrite_mode"] or ""))


def _flags_payload(track: TrackEntry) -> dict[str, bool]:
    return {
        name: _flag_value(track, name, original=False)
        for name in FLAG_NAMES
        if name != "enabled"
    }


def remux_config_to_exact_job(
    config: RemuxConfig,
    *,
    name: str = "",
    fallback_profile: str = "",
) -> dict[str, Any]:
    """Serialize a remux state as an exact CLI job."""
    source_index_by_file_id: dict[str, int] = {}
    for source in config.sources:
        for track in source.tracks:
            source_index_by_file_id[track.file_id] = source.file_index

    all_tracks = [track for source in config.sources for track in source.tracks]
    track_by_order_key = {
        (source.file_index, track.mkv_tid, track.entry_id): track
        for source in config.sources
        for track in source.tracks
    }
    track_by_basic_key = {
        (source.file_index, track.mkv_tid): track
        for source in config.sources
        for track in source.tracks
        if not track.is_new
    }

    def _track_selector(track: TrackEntry) -> dict[str, Any]:
        source_idx = source_index_by_file_id.get(track.file_id)
        source_tracks = next(
            (source.tracks for source in config.sources if source.file_index == source_idx),
            all_tracks,
        )
        return track_selector_for_entry(
            track,
            source_index=source_idx,
            tracks=list(source_tracks),
            source_index_by_file_id=source_index_by_file_id,
        )

    tracks_payload: list[dict[str, Any]] = []
    audio_variants: list[dict[str, Any]] = []
    for track in all_tracks:
        payload = {
            "selector": _track_selector(track),
            "enabled": bool(track.enabled),
            "language": track.language,
            "title": track.title,
            "flags": _flags_payload(track),
            "time_shift_ms": int(track.time_shift_ms or 0),
        }
        if track.sync_rewrite_mode:
            payload["sync_rewrite_mode"] = track.sync_rewrite_mode
        if track.is_new:
            payload["entry_id"] = track.entry_id
            payload["codec"] = track.codec
            bitrate = audio_bitrate_kbps_from_display_info(track.display_info)
            if bitrate:
                payload["bitrate_kbps"] = bitrate
            source_track = next(
                (candidate for candidate in all_tracks if candidate.entry_id == track.source_entry_id),
                None,
            )
            if source_track is not None:
                payload["source_selector"] = _track_selector(source_track)
            audio_variants.append(payload)
        else:
            tracks_payload.append(payload)

    order_payload: list[dict[str, Any]] = []
    for item in config.track_order:
        source_index = int(item[0])
        mkv_tid = int(item[1])
        entry_id = str(item[2]) if len(item) > 2 else ""
        ordered_track = (
            track_by_order_key.get((source_index, mkv_tid, entry_id))
            if entry_id
            else track_by_basic_key.get((source_index, mkv_tid))
        )
        if ordered_track is not None:
            if ordered_track.is_new:
                # Variante (piste recréée) : identité stable par entry_id ; son
                # sélecteur générique porte les métadonnées de la source et serait
                # ambigu (même mkv_tid/codec d'origine que la piste copiée).
                order_payload.append({"selector": {
                    "source": source_index,
                    "type": ordered_track.track_type,
                    "entry_id": ordered_track.entry_id,
                }})
            else:
                order_payload.append({"selector": _track_selector(ordered_track)})

    sources_payload = []
    for source in config.sources:
        attachment_names = [att.filename for att in source.selected_attachments]
        sources_payload.append(
            {
                "path": str(source.path),
                "attachments": attachment_names if attachment_names else "none",
                "copy_tags": bool(source.copy_tags),
            }
        )

    chapters: dict[str, Any] | bool
    if not config.keep_chapters:
        chapters = False
    elif config.chapter_overrides is not None:
        chapters = {
            "source_index": config.chapter_source_index,
            "include_source": False,
            "add": [
                {
                    "timestamp": getattr(chapter, "timecode_s", 0),
                    "chaptername": getattr(chapter, "name", ""),
                }
                for chapter in config.chapter_overrides
            ],
        }
    else:
        chapters = {"source_index": config.chapter_source_index} if config.chapter_source_index is not None else {}

    tmdb_hint: dict[str, Any] | None = None
    tags = config.tag_overrides if isinstance(config.tag_overrides, dict) else {}
    if str(tags.get("SEASON") or tags.get("EPISODE") or "").strip():
        tmdb_hint = {
            "enabled": False,
            "kind": "tv",
            "season": "auto",
            "episode": "auto",
        }

    job: dict[str, Any] = {
        "version": 1,
        "kind": "exact-job",
        "sources": sources_payload,
        "output": str(config.output),
        "tracks": tracks_payload,
        "track_order": order_payload,
        "chapters": chapters,
        "extra_attachments": [str(path) for path in config.extra_attachments],
        "file_title": config.file_title,
        "tag_overrides": config.tag_overrides,
        "tmdb": tmdb_hint,
    }
    if name:
        job["name"] = name
    if fallback_profile:
        job["fallback_profile"] = fallback_profile
    if audio_variants:
        job["audio_variants"] = audio_variants
    return {key: value for key, value in job.items() if value not in ({}, [], None, "")}
