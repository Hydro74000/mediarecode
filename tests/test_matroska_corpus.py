from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core.matroska.reader import MatroskaReader
from scripts.matroska_semantic_report import compare_reports, semantic_report


CORPUS = Path(__file__).parent / "corpus" / "matroska"
EXPECTED_CODECS = {
    "avc_aac_srt_chapters.mkv": ["V_MPEG4/ISO/AVC", "A_AAC", "S_TEXT/UTF8"],
    "hevc_flac_ass_hdr10.mkv": ["V_MPEGH/ISO/HEVC", "A_FLAC", "S_TEXT/ASS"],
    "av1_opus_webvtt.mkv": ["V_AV1", "A_OPUS", "D_WEBVTT/SUBTITLES"],
}


def test_checked_in_corpus_matches_sha256_manifest() -> None:
    entries = {}
    for line in (CORPUS / "MANIFEST.sha256").read_text(encoding="ascii").splitlines():
        checksum, name = line.split("  ", 1)
        entries[name] = checksum
    assert set(entries) == set(EXPECTED_CODECS)
    for name, checksum in entries.items():
        assert hashlib.sha256((CORPUS / name).read_bytes()).hexdigest() == checksum


@pytest.mark.parametrize("name,codecs", EXPECTED_CODECS.items())
def test_corpus_is_readable_by_internal_reader_and_ffprobe(name: str, codecs: list[str]) -> None:
    path = CORPUS / name
    reader = MatroskaReader(path)
    assert [track.codec_id for track in reader.tracks()] == codecs
    assert len(list(reader.blocks())) > len(codecs)
    if shutil.which("ffprobe"):
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=format_name", "-of", "json", str(path)],
            capture_output=True, text=True, check=True,
        )
        assert json.loads(probe.stdout)["format"]["format_name"].startswith("matroska")


def test_corpus_covers_chapters_attachments_tags_and_hdr10() -> None:
    avc = MatroskaReader(CORPUS / "avc_aac_srt_chapters.mkv")
    hdr = MatroskaReader(CORPUS / "hevc_flac_ass_hdr10.mkv")
    assert len(avc.chapter_editions()[0].chapters) == 2
    assert avc.tags()
    assert avc.attachments()[0].name == "MuxiveoSans.txt"
    assert hdr.attachments()[0].media_type == "application/x-truetype-font"
    if shutil.which("ffmpeg"):
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "info", "-i", str(hdr.path),
             "-map", "0:v:0", "-c", "copy", "-bsf:v", "trace_headers", "-f", "null", "-"],
            capture_output=True, text=True, check=True,
        )
        trace = result.stderr
        assert "colour_primaries" in trace and "00001001 = 9" in trace
        assert "transfer_characteristics" in trace and "00010000 = 16" in trace


def test_semantic_oracle_report_is_stable_and_self_equivalent() -> None:
    first = semantic_report(CORPUS / "avc_aac_srt_chapters.mkv")
    second = semantic_report(CORPUS / "avc_aac_srt_chapters.mkv")
    assert compare_reports(first, second) == []
    assert first["tracks"][0]["packets"][0]["payload_sha256"]
