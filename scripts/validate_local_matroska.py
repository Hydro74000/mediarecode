#!/usr/bin/env python3
"""Read-only source validation against native Matroska round-trips.

Outputs and exact-jobs are created under a temporary directory. Source files
are never modified. This helper deliberately has no MKVToolNix dependency.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.matroska_semantic_report import compare_reports, semantic_report  # noqa: E402


def validate_source(source: Path, root: Path) -> list[str]:
    case = root / source.stem
    case.mkdir(parents=True, exist_ok=True)
    output = case / "native.mkv"
    job = case / "exact-job.json"
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
         "--ffmpeg", "ffmpeg", "--ffprobe", "ffprobe", "--mediainfo", "mediainfo"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if result.returncode:
        return [f"exécution native (code {result.returncode}): {result.stderr.strip()}"]
    failures = compare_reports(semantic_report(source), semantic_report(output))
    partial = output.with_suffix(output.suffix + ".partial")
    if partial.exists():
        failures.append(f"fichier partiel résiduel: {partial}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", nargs="+", type=Path)
    parser.add_argument("--keep-output", type=Path)
    args = parser.parse_args()
    missing = [str(path) for path in args.input if not path.is_file()]
    if missing:
        parser.error(f"sources absentes: {missing}")
    context = None
    if args.keep_output:
        root = args.keep_output
        root.mkdir(parents=True, exist_ok=True)
    else:
        context = tempfile.TemporaryDirectory(prefix="muxiveo-local-matroska-")
        root = Path(context.name)
    failed = False
    try:
        for source in args.input:
            failures = validate_source(source.resolve(), root)
            status = "PASS" if not failures else "FAIL"
            print(f"{status} {source}")
            for failure in failures[:100]:
                print(f"  {failure}")
            failed = failed or bool(failures)
    finally:
        if context is not None:
            context.cleanup()
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
