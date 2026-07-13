"""Blocking semantic oracle. This module intentionally requires MKVToolNix."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.matroska_semantic_report import compare_reports, semantic_report


ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "tests" / "corpus" / "matroska"

pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("ffmpeg", "ffprobe", "mkvmerge")),
    reason="job oracle dédié : FFmpeg, FFprobe et mkvmerge requis",
)


@pytest.mark.parametrize("source", sorted(CORPUS.glob("*.mkv")), ids=lambda path: path.name)
def test_native_output_matches_mkvmerge_oracle(tmp_path: Path, source: Path) -> None:
    native = tmp_path / f"native-{source.name}"
    oracle = tmp_path / f"oracle-{source.name}"
    job = tmp_path / "job.json"
    job.write_text(json.dumps({
        "version": 1,
        "kind": "exact-job",
        "sources": [{"path": str(source), "attachments": "all", "copy_tags": True}],
        "output": str(native),
        "chapters": {"source_index": 0},
        "mux_backend": "native",
    }), encoding="utf-8")
    native_result = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--cli", "run", "--config", str(job),
         "--ffmpeg", "ffmpeg", "--ffprobe", "ffprobe", "--mediainfo", "mediainfo"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert native_result.returncode == 0, native_result.stderr
    oracle_result = subprocess.run(
        ["mkvmerge", "--quiet", "-o", str(oracle), str(source)],
        capture_output=True, text=True,
    )
    assert oracle_result.returncode in {0, 1}, oracle_result.stderr
    failures = compare_reports(semantic_report(oracle), semantic_report(native))
    assert not failures, "\n".join(failures[:100])
