#!/usr/bin/env python3
"""Build and compare normalized semantic reports for Matroska oracle tests."""
from __future__ import annotations

import argparse
import hashlib
import json
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
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
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
    for block in reader.blocks():
        packet_groups.setdefault(block.track_number, []).append({
            "timestamp_ns": block.timestamp_ns,
            "duration_ns": block.duration_ns,
            "keyframe": bool(block.flags & 0x80),
            "payload_sha256": hashlib.sha256(block.payload).hexdigest(),
        })
    attachments = sorted((
        {
            "name": item.name, "media_type": item.media_type,
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

    tags = sorted((
        {
            "targets": normalized_targets(item.targets),
            "values": sorted([list(value) for value in item.values]),
        }
        for item in reader.tags()
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
                "language": LanguageTags.to_iso639_2(track.language) or "und",
                "language_bcp47": track.language_bcp47,
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


def compare_reports(expected: dict, actual: dict) -> list[str]:
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
