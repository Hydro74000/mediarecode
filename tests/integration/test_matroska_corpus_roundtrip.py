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
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg et FFprobe requis",
)


@pytest.mark.parametrize("source", sorted(CORPUS.glob("*.mkv")), ids=lambda path: path.name)
def test_native_corpus_roundtrip_is_semantically_equivalent(tmp_path: Path, source: Path) -> None:
    output = tmp_path / source.name
    job = tmp_path / "job.json"
    job.write_text(json.dumps({
        "version": 1,
        "kind": "exact-job",
        "sources": [{"path": str(source), "attachments": "all", "copy_tags": True}],
        "output": str(output),
        "chapters": {"source_index": 0},
        "mux_backend": "native",
    }), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--cli", "run", "--config", str(job),
         "--ffmpeg", "ffmpeg", "--ffprobe", "ffprobe", "--mediainfo", "/bin/false"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    failures = compare_reports(semantic_report(source), semantic_report(output))
    assert not failures, "\n".join(failures[:100])
    assert not output.with_suffix(output.suffix + ".partial").exists()
