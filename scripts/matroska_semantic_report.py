#!/usr/bin/env python3
"""Build and compare normalized semantic reports for Matroska oracle tests."""
from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.workflows.matroska_reader import MatroskaChapter, MatroskaReader  # noqa: E402
from core.lang_tags import Rfc5646LanguageTags as LanguageTags  # noqa: E402


STREAM_FIELDS = (
    "codec_name", "codec_type", "profile", "width", "height", "pix_fmt",
    "color_range", "color_space", "color_transfer", "color_primaries",
    "sample_rate", "channels", "channel_layout",
)
DISPOSITION_FIELDS = (
    "default", "forced", "hearing_impaired", "visual_impaired", "original", "comment",
)


def _ffprobe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-show_packets",
         "-show_data_hash", "sha256", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def _chapter(chapter: MatroskaChapter) -> dict:
    return {
        "start_ns": chapter.start_ns,
        "end_ns": chapter.end_ns,
        "displays": [list(display) for display in chapter.displays],
        "children": [_chapter(child) for child in chapter.children],
    }


def semantic_report(path: Path) -> dict:
    path = Path(path)
    reader = MatroskaReader(path)
    probe = _ffprobe(path)
    native_tracks = reader.tracks()

    def effective_language(legacy: str, bcp47: str = "") -> str:
        value = str(bcp47 or legacy or "und")
        if "-" in value:
            return value.lower()
        return LanguageTags.to_iso639_2(value) or "und"
    streams = []
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "attachment":
            continue
        tags = stream.get("tags") or {}
        disposition = stream.get("disposition") or {}
        streams.append({
            **{key: stream[key] for key in STREAM_FIELDS if key in stream},
            "language": LanguageTags.to_iso639_2(tags.get("language", "und")) or "und",
            "title": tags.get("title", ""),
            "disposition": {key: int(disposition.get(key, 0)) for key in DISPOSITION_FIELDS},
        })
    packet_groups: dict[int, list[dict]] = {track.number: [] for track in native_tracks}

    def time_ns(value: object) -> int | None:
        if value in (None, "N/A"):
            return None
        return int(Decimal(str(value)) * 1_000_000_000)

    for packet in probe.get("packets", []):
        stream_index = int(packet.get("stream_index", -1))
        if not 0 <= stream_index < len(native_tracks):
            continue
        track = native_tracks[stream_index]
        data_hash = str(packet.get("data_hash", ""))
        packet_groups[track.number].append({
            "timestamp_ns": time_ns(packet.get("pts_time")),
            "duration_ns": time_ns(packet.get("duration_time")),
            "keyframe": bool("K" in str(packet.get("flags", ""))) if track.track_type == 1 else None,
            "payload_sha256": data_hash.partition(":")[2].lower(),
        })
    mime_aliases = {
        "application/x-truetype-font": "font/ttf",
        "application/x-font-ttf": "font/ttf",
        "application/vnd.ms-opentype": "font/otf",
    }
    attachments = sorted((
        {
            "name": item.name, "media_type": mime_aliases.get(item.media_type, item.media_type),
            "description": item.description, "data_sha256": hashlib.sha256(item.data).hexdigest(),
        }
        for item in reader.attachments()
    ), key=lambda item: (item["name"], item["data_sha256"]))
    track_refs = {track.uid: f"track:{index}" for index, track in enumerate(native_tracks, start=1)}
    attachment_refs = {
        item.uid: f"attachment:{item.name}:{hashlib.sha256(item.data).hexdigest()}"
        for item in reader.attachments()
    }
    edition_refs: dict[int, str] = {}
    chapter_refs: dict[int, str] = {}

    def index_chapters(chapters: tuple[MatroskaChapter, ...], prefix: str) -> None:
        for index, chapter in enumerate(chapters, start=1):
            location = f"{prefix}.{index}"
            chapter_refs[chapter.uid] = f"chapter:{location}"
            index_chapters(chapter.children, location)

    editions = reader.chapter_editions()
    for index, edition in enumerate(editions, start=1):
        edition_refs[edition.uid] = f"edition:{index}"
        index_chapters(edition.chapters, str(index))
    target_maps = {
        "63c5": track_refs, "63c6": attachment_refs,
        "63c4": chapter_refs, "63c9": edition_refs,
    }

    def normalized_targets(values: dict[str, int]) -> dict[str, str]:
        return {
            key: target_maps.get(key, {}).get(uid, f"unresolved:{uid}")
            for key, uid in sorted(values.items())
        }

    technical_tags = {
        "BPS", "DURATION", "ENCODER", "NUMBER_OF_BYTES", "NUMBER_OF_FRAMES",
        "_STATISTICS_WRITING_APP", "_STATISTICS_WRITING_DATE_UTC", "_STATISTICS_TAGS",
    }
    tags = sorted((
        {
            "targets": normalized_targets(item.targets),
            "values": sorted([list(value) for value in item.values if value[0].upper() not in technical_tags]),
        }
        for item in reader.tags()
        if any(value[0].upper() not in technical_tags for value in item.values)
    ), key=lambda item: json.dumps(item, sort_keys=True))
    return {
        "format": "matroska",
        "title": reader.segment_title(),
        "streams": streams,
        "tracks": [
            {
                "number": index,
                "type": track.track_type,
                "codec_id": track.codec_id,
                "codec_private_sha256": hashlib.sha256(track.codec_private).hexdigest(),
                "language": effective_language(track.language, track.language_bcp47),
                "name": track.name,
                "packets": packet_groups.get(track.number, []),
            }
            for index, track in enumerate(native_tracks, start=1)
        ],
        "chapters": [
            {"chapters": [_chapter(chapter) for chapter in edition.chapters]}
            for edition in editions
        ],
        "tags": tags,
        "attachments": attachments,
    }


def compare_reports(
    expected: dict,
    actual: dict,
    *,
    timestamp_tolerance_ns: int = 0,
) -> list[str]:
    failures: list[str] = []

    def visit(left: object, right: object, location: str) -> None:
        if type(left) is not type(right):
            failures.append(f"{location}: type {type(left).__name__} != {type(right).__name__}")
        elif isinstance(left, dict):
            if left.keys() != right.keys():
                failures.append(f"{location}: clés {sorted(left)} != {sorted(right)}")
            for key in left.keys() & right.keys():
                visit(left[key], right[key], f"{location}.{key}")
        elif isinstance(left, list):
            if len(left) != len(right):
                failures.append(f"{location}: longueur {len(left)} != {len(right)}")
            for index, (left_item, right_item) in enumerate(zip(left, right)):
                visit(left_item, right_item, f"{location}[{index}]")
        elif (
            isinstance(left, int) and isinstance(right, int)
            and location.rsplit(".", 1)[-1] in {"timestamp_ns", "duration_ns"}
            and abs(left - right) <= timestamp_tolerance_ns
        ):
            return
        elif left != right:
            failures.append(f"{location}: {left!r} != {right!r}")

    visit(expected, actual, "report")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("input", type=Path)
    report_parser.add_argument("--output", type=Path)
    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("expected", type=Path)
    compare_parser.add_argument("actual", type=Path)
    args = parser.parse_args()
    if args.command == "report":
        report = semantic_report(args.input)
        payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.write_text(payload, encoding="utf-8")
        else:
            print(payload, end="")
        return 0
    failures = compare_reports(semantic_report(args.expected), semantic_report(args.actual))
    if failures:
        print("\n".join(failures[:100]), file=sys.stderr)
        return 1
    print("Rapports Matroska sémantiquement équivalents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
