from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration._synth import make_av_container


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe requis pour les tests d'intégration CLI",
)


def _run_cli(root: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(root / "main.py"),
            "--cli",
            *args,
            "--ffmpeg",
            "ffmpeg",
            "--ffprobe",
            "ffprobe",
            "--mediainfo",
            "/bin/false",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_inspect_validate_preview_on_synthetic_media(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    src = tmp_path / "source.mkv"
    out = tmp_path / "out.mkv"
    make_av_container(src, duration=0.3)

    inspect = _run_cli(root, "inspect", str(src))
    assert inspect.returncode == 0, inspect.stderr
    payload = json.loads(inspect.stdout)
    assert payload["files"][0]["tracks"]

    config = tmp_path / "job.json"
    config.write_text(
        json.dumps({"version": 1, "sources": [{"path": str(src)}], "output": str(out)}),
        encoding="utf-8",
    )

    validate = _run_cli(root, "validate", "--config", str(config))
    assert validate.returncode == 0, validate.stderr

    preview = _run_cli(root, "preview", "--config", str(config))
    assert preview.returncode == 0, preview.stderr
    assert "ffmpeg" in preview.stdout
    assert str(out) in preview.stdout

    validate_json = _run_cli(root, "validate", "--config", str(config), "--json")
    assert validate_json.returncode == 0, validate_json.stderr
    validate_payload = json.loads(validate_json.stdout)
    assert validate_payload["valid"] is True
    assert validate_payload["output"] == str(out)

    preview_json = _run_cli(root, "preview", "--config", str(config), "--json")
    assert preview_json.returncode == 0, preview_json.stderr
    preview_payload = json.loads(preview_json.stdout)
    assert preview_payload["valid"] is True
    assert preview_payload["command"][0] == "ffmpeg"
    assert preview_payload["command_text"].startswith("# Backend: native Matroska")
    assert preview_payload["selected_backend"] == "native"
    assert preview_payload["plan_version"] == 1
    assert preview_payload["execution_preview"]["action"] == "internal_matroska_write"


def test_cli_remux_dry_run_refuses_invalid_json_before_inspection(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    config = tmp_path / "bad.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": [{"path": str(tmp_path / "missing.mkv")}],
                "output": str(tmp_path / "out.mkv"),
                "tracks": "invalid",
            }
        ),
        encoding="utf-8",
    )

    result = _run_cli(root, "remux", "--config", str(config), "--dry-run")
    assert result.returncode == 2
    assert "tracks" in result.stderr


def test_cli_schema_outputs_and_writes_json(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    schema_path = tmp_path / "schema.json"

    stdout_result = subprocess.run(
        [sys.executable, str(root / "main.py"), "--cli", "schema"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert stdout_result.returncode == 0, stdout_result.stderr
    stdout_payload = json.loads(stdout_result.stdout)
    assert stdout_payload["properties"]["version"] == {"const": 1}

    file_result = subprocess.run(
        [sys.executable, str(root / "main.py"), "--cli", "schema", "--output", str(schema_path)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert file_result.returncode == 0, file_result.stderr
    file_payload = json.loads(schema_path.read_text(encoding="utf-8"))
    assert file_payload["$id"].endswith("cli-job-v1.json")

    decision_result = subprocess.run(
        [sys.executable, str(root / "main.py"), "--cli", "schema", "--version", "decision-profile"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert decision_result.returncode == 0, decision_result.stderr
    decision_payload = json.loads(decision_result.stdout)
    assert decision_payload["properties"]["kind"] == {"const": "decision-profile"}

    main_cli_result = subprocess.run(
        [sys.executable, str(root / "main.py"), "--cli", "schema", "--version", "decision-profile"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert main_cli_result.returncode == 0, main_cli_result.stderr
    main_cli_payload = json.loads(main_cli_result.stdout)
    assert main_cli_payload["properties"]["kind"] == {"const": "decision-profile"}


def test_cli_profile_preview_on_synthetic_media(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    src = tmp_path / "source.mkv"
    out = tmp_path / "out.mkv"
    make_av_container(src, duration=0.3)
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "decision-profile",
                "name": "rename",
                "rules": [
                    {
                        "id": "rename_audio",
                        "scope": "all",
                        "match": {"all": [{"field": "type", "op": "is", "value": "audio"}]},
                        "actions": [{"type": "set_title", "pattern": "{codec} {channels}"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    validate = _run_cli(root, "validate", "--profile", str(profile), "--json")
    assert validate.returncode == 0, validate.stderr
    assert json.loads(validate.stdout)["kind"] == "decision-profile"

    preview = _run_cli(root, "preview", "--profile", str(profile), "-i", str(src), "-o", str(out), "--json")
    assert preview.returncode == 0, preview.stderr
    payload = json.loads(preview.stdout)
    assert payload["valid"] is True
    assert payload["profile_report"]["applied_rules"] >= 1
    assert any(track["type"] == "audio" and track["title"] for track in payload["tracks"])

    forced = _run_cli(
        root, "preview", "--profile", str(profile), "-i", str(src), "-o", str(out),
        "--mux-backend", "ffmpeg", "--json",
    )
    assert forced.returncode == 0, forced.stderr
    forced_payload = json.loads(forced.stdout)
    assert forced_payload["selected_backend"] == "ffmpeg"
    assert forced_payload["mux_backend"] == "ffmpeg"


def test_cli_profile_argument_falls_back_to_user_profile_dir_without_json_suffix(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    xdg_home = tmp_path / "xdg"
    profile_dir = xdg_home / "muxiveo" / "profiles" / "decision"
    profile_dir.mkdir(parents=True)
    (profile_dir / "SavedProfile.json").write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "decision-profile",
                "name": "SavedProfile",
                "rules": [],
            }
        ),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "XDG_CONFIG_HOME": str(xdg_home),
        "MUXIVEO_CONFIG_HOME": str(xdg_home / "muxiveo"),
    }

    result = _run_cli(root, "validate", "--profile", "SavedProfile", "--json", env=env)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["name"] == "SavedProfile"


def test_cli_batch_dry_run_jsonl_reports_job_status(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    src = tmp_path / "source.mkv"
    out = tmp_path / "out.mkv"
    summary = tmp_path / "summary.json"
    make_av_container(src, duration=0.3)

    template = tmp_path / "template.json"
    template.write_text(
        json.dumps({"version": 1}),
        encoding="utf-8",
    )
    batch = tmp_path / "batch.json"
    batch.write_text(
        json.dumps({"jobs": [{"sources": [{"path": str(src)}], "output": str(out)}]}),
        encoding="utf-8",
    )

    result = _run_cli(
        root,
        "batch",
        "--template",
        str(template),
        "--batch",
        str(batch),
        "--dry-run",
        "--log-format",
        "jsonl",
        "--summary",
        str(summary),
    )
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stderr.splitlines() if line.strip()]
    assert any(event.get("event") == "batch_job" and event.get("status") == "success" for event in events)
    assert any(event.get("event") == "batch_summary" for event in events)
    summary_payload = json.loads(summary.read_text(encoding="utf-8"))
    assert summary_payload["total"] == 1
    assert summary_payload["failures"] == 0
    assert summary_payload["jobs"][0]["status"] == "success"
    assert summary_payload["jobs"][0]["output"] == str(out)


def test_cli_batch_input_dir_dry_run_preserves_relative_outputs(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    input_root = tmp_path / "in"
    nested = input_root / "Saison 01"
    nested.mkdir(parents=True)
    src_a = input_root / "E01.mkv"
    src_b = nested / "E02.mkv"
    ignored = input_root / "E01.srt"
    out_root = tmp_path / "out"
    summary = tmp_path / "summary.json"
    make_av_container(src_a, duration=0.3)
    make_av_container(src_b, duration=0.3)
    ignored.write_text("1\n00:00:00,000 --> 00:00:01,000\nIgnored\n", encoding="utf-8")

    template = tmp_path / "template.json"
    template.write_text(
        json.dumps({"version": 1}),
        encoding="utf-8",
    )

    result = _run_cli(
        root,
        "batch",
        "--template",
        str(template),
        "--input-dir",
        str(input_root),
        "--recursive",
        "--output-dir",
        str(out_root),
        "--dry-run",
        "--log-format",
        "jsonl",
        "--summary",
        str(summary),
    )
    assert result.returncode == 0, result.stderr
    assert not out_root.exists()
    events = [json.loads(line) for line in result.stderr.splitlines() if line.strip()]
    discovery = next(event for event in events if event.get("event") == "batch_discovery")
    assert discovery["scanned"] == 3
    assert discovery["selected"] == 2
    summary_payload = json.loads(summary.read_text(encoding="utf-8"))
    assert summary_payload["total"] == 2
    assert summary_payload["failures"] == 0
    assert [job["output"] for job in summary_payload["jobs"]] == [
        str(out_root / "E01.mkv"),
        str(out_root / "Saison 01" / "E02.mkv"),
    ]


def test_cli_batch_input_dir_supports_exact_job_template(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    input_root = tmp_path / "in"
    input_root.mkdir()
    src = input_root / "episode.mkv"
    out_root = tmp_path / "out"
    make_av_container(src, duration=0.3)

    template = tmp_path / "exact-job.json"
    template.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "exact-job",
                "track_order": [
                    {"selector": {"source": 0, "type": "video", "position": 0}},
                    {"selector": {"source": 0, "type": "audio", "position": 0}},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = _run_cli(
        root,
        "batch",
        "--template",
        str(template),
        "--input-dir",
        str(input_root),
        "--output-dir",
        str(out_root),
        "--dry-run",
    )
    assert result.returncode == 0, result.stderr
    assert str(out_root / "episode.mkv") in result.stdout


def test_cli_preview_respects_multisource_explicit_track_order(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    src_a = tmp_path / "a.mkv"
    src_b = tmp_path / "b.mkv"
    out = tmp_path / "ordered.mkv"
    make_av_container(src_a, duration=0.3)
    make_av_container(src_b, duration=0.3)

    config = tmp_path / "ordered.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": [{"path": str(src_a)}, {"path": str(src_b)}],
                "output": str(out),
                "track_order": [{"source": 1, "id": 0}, {"source": 0, "id": 1}],
            }
        ),
        encoding="utf-8",
    )

    result = _run_cli(root, "preview", "--config", str(config))
    assert result.returncode == 0, result.stderr
    assert result.stdout.index("-map 1:0") < result.stdout.index("-map 0:1")


def test_cli_exact_job_preview_resolves_track_selectors(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    src = tmp_path / "source.mkv"
    out = tmp_path / "exact.mkv"
    make_av_container(src, duration=0.3)

    config = tmp_path / "exact-job.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "exact-job",
                "sources": [{"path": str(src)}],
                "output": str(out),
                "tracks": [
                    {
                        "selector": {"source": 0, "type": "audio", "position": 0},
                        "language": "fr-FR",
                        "title": "VF",
                    }
                ],
                "track_order": [
                    {"selector": {"source": 0, "type": "video", "position": 0}},
                    {"selector": {"source": 0, "type": "audio", "position": 0}},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = _run_cli(root, "preview", "--config", str(config), "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert [(item["source"], item["id"]) for item in payload["track_order"]] == [(0, 0), (0, 1)]
    assert "-metadata:s:a:0 language=fr-FR" in payload["command_text"]


def test_cli_remux_headless_runs_and_refuses_existing_output(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    src = tmp_path / "source.mkv"
    out = tmp_path / "out.mkv"
    make_av_container(src, duration=0.3)

    first = _run_cli(root, "remux", "-i", str(src), "-o", str(out), "--no-nfo")
    assert first.returncode == 0, first.stderr
    assert out.exists() and out.stat().st_size > 0

    second = _run_cli(root, "remux", "-i", str(src), "-o", str(out), "--no-nfo")
    assert second.returncode == 5
    assert "Sortie déjà existante" in second.stderr

    forced = _run_cli(root, "remux", "-i", str(src), "-o", str(out), "--no-nfo", "--force")
    assert forced.returncode == 0, forced.stderr
