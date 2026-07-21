#!/usr/bin/env python3
"""
Run a real integration battery across remux and encode workflows.

The script:
  - prepares short host-derived MKV fixtures,
  - executes remux/encode cases against all main workflow branches,
  - amends a JSON report after each case,
  - records functional checks and workdir cleanup observations,
  - writes a Markdown summary at the end.
"""

from __future__ import annotations

# Imports applicatifs placés après l'ajout explicite de la racine du dépôt.
# ruff: noqa: E402

import copy
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtCore import QCoreApplication, Qt

from core.inspector import ChapterEntry, FileInfo, FileInspector
from core.workdir import process_folder_name_from_output, work_dir_entries
from core.workflows.encode.models import (
    AudioTrackSettings,
    EncodeConfig,
    QualityMode,
    TrackMetaEdit,
    VideoEncodeSettings,
)
from core.workflows.encode.workflow import EncodeWorkflow
from core.workflows.remux import RemuxWorkflow
from core.workflows.remux_models import (
    RemuxConfig,
    SourceInput,
    TrackEntry,
    tracks_from_file_info,
)


ARTIFACT_ROOT = REPO_ROOT / "build" / "integration_workflow_tests"
CORPUS_ROOT = ARTIFACT_ROOT / "corpus"
RUNS_ROOT = ARTIFACT_ROOT / "runs"
BIN_ROOT = ARTIFACT_ROOT / "bin"
REPORT_PATH = ARTIFACT_ROOT / "report.json"
SUMMARY_PATH = ARTIFACT_ROOT / "summary.md"

HOST_SDR_SAMPLE = Path()
HOST_DV_HDR10P = Path()
REAL_FONT = Path()
SAMPLE_START_S = 0.0
SAMPLE_DURATION_S = 12.0

CASE_TIMEOUT_S = 1800.0


@dataclass(frozen=True)
class ToolPaths:
    ffmpeg: str
    ffprobe: str
    mediainfo: str
    dovi_tool: str
    hdr10plus_tool: str


@dataclass(frozen=True)
class PreparedSources:
    sdr_attach_source: Path
    sdr_meta_source: Path
    dv_hdr10plus_source: Path


def ensure_app() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication(sys.argv)
    return app


def run_cmd(
    cmd: list[str],
    *,
    check: bool = True,
    timeout: float | None = None,
    capture_output: bool = True,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=capture_output,
        text=True,
        check=False,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{stderr}")
    return proc


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def append_case_result(report: dict[str, Any], result: dict[str, Any]) -> None:
    report["cases"].append(result)
    report["updated_at"] = iso_now()
    summary = {
        "pass": 0,
        "warn": 0,
        "fail": 0,
        "skipped": 0,
    }
    for case in report["cases"]:
        summary[case["status"]] += 1
    report["summary"] = summary
    write_json_atomic(REPORT_PATH, report)


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def create_wrapper(path: Path, target_cmd: list[str]) -> Path:
    quoted = " ".join(sh_quote(part) for part in target_cmd)
    content = "#!/bin/sh\nexec " + quoted + ' "$@"\n'
    write_text(path, content)
    path.chmod(0o755)
    return path


def sh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def _resolve_tool(name: str, *, distrobox_name: str | None) -> str:
    host_tool = shutil.which(name)
    if host_tool:
        return host_tool
    if not distrobox_name:
        raise RuntimeError(
            f"Outil requis introuvable sur le host : {name}. "
            "Utiliser --distrobox NOM pour autoriser sa résolution dans un conteneur."
        )
    distrobox = shutil.which("distrobox")
    if not distrobox:
        raise RuntimeError("--distrobox demandé mais l'exécutable distrobox est introuvable.")
    lookup = subprocess.run(
        [distrobox, "enter", "-n", distrobox_name, "--", "sh", "-lc", f"command -v {name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    inner_tool = first_line(lookup.stdout)
    if lookup.returncode != 0 or not inner_tool.startswith("/"):
        raise RuntimeError(
            f"Outil requis introuvable dans la Distrobox {distrobox_name} : {name}"
        )
    return str(create_wrapper(
        BIN_ROOT / f"{name}-distrobox",
        [distrobox, "enter", "-n", distrobox_name, "--", inner_tool],
    ))


def create_tool_wrappers(distrobox_name: str | None = None) -> ToolPaths:
    BIN_ROOT.mkdir(parents=True, exist_ok=True)
    return ToolPaths(
        ffmpeg=_resolve_tool("ffmpeg", distrobox_name=distrobox_name),
        ffprobe=_resolve_tool("ffprobe", distrobox_name=distrobox_name),
        mediainfo=_resolve_tool("mediainfo", distrobox_name=distrobox_name),
        dovi_tool=_resolve_tool("dovi_tool", distrobox_name=distrobox_name),
        hdr10plus_tool=_resolve_tool("hdr10plus_tool", distrobox_name=distrobox_name),
    )


def tool_versions(tools: ToolPaths) -> dict[str, str]:
    return {
        "ffmpeg": first_line(run_cmd([tools.ffmpeg, "-version"]).stdout),
        "ffprobe": first_line(run_cmd([tools.ffprobe, "-version"]).stdout),
        "mediainfo": first_line(run_cmd([tools.mediainfo, "--Version"]).stdout),
        "dovi_tool": first_line(run_cmd([tools.dovi_tool, "--version"]).stdout),
        "hdr10plus_tool": first_line(run_cmd([tools.hdr10plus_tool, "--version"]).stdout),
    }


def first_line(text: str | None) -> str:
    return ((text or "").strip().splitlines() or [""])[0]


def packet_counts(path: Path, tools: ToolPaths) -> dict[int, tuple[str, int]]:
    payload = json.loads(run_cmd([
        tools.ffprobe, "-v", "error", "-count_packets",
        "-show_entries", "stream=index,codec_type,nb_read_packets",
        "-of", "json", str(path.resolve()),
    ]).stdout or "{}")
    counts: dict[int, tuple[str, int]] = {}
    for stream in payload.get("streams", []):
        try:
            count = int(stream.get("nb_read_packets", 0))
        except (TypeError, ValueError):
            count = 0
        counts[int(stream["index"])] = (str(stream.get("codec_type") or ""), count)
    return counts


def require_packet_streams(
    path: Path, tools: ToolPaths, expected: dict[int, str],
) -> dict[int, tuple[str, int]]:
    counts = packet_counts(path, tools)
    errors: list[str] = []
    for index, track_type in expected.items():
        observed_type, count = counts.get(index, ("absent", 0))
        if observed_type != track_type or count <= 0:
            errors.append(
                f"stream #{index}: attendu {track_type} non vide, "
                f"obtenu {observed_type} avec {count} paquet(s)"
            )
    if errors:
        raise RuntimeError(f"Corpus réel inutilisable ({path.name}) : " + "; ".join(errors))
    return counts


def prepare_corpus(tools: ToolPaths) -> PreparedSources:
    CORPUS_ROOT.mkdir(parents=True, exist_ok=True)

    base_clip = CORPUS_ROOT / "sdr_base_short.mkv"
    sdr_attach = CORPUS_ROOT / "sdr_attach_source_short.mkv"
    sdr_meta = CORPUS_ROOT / "sdr_meta_source_short.mkv"
    dv_hdr10p = CORPUS_ROOT / "dv_hdr10plus_source_short.mkv"
    cover = CORPUS_ROOT / "cover.jpg"
    chapters = CORPUS_ROOT / "meta_source_chapters.ffmetadata"

    run_cmd([
        tools.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        str(SAMPLE_START_S),
        "-t",
        str(SAMPLE_DURATION_S),
        "-i",
        str(HOST_SDR_SAMPLE.resolve()),
        "-map",
        "0",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        str(base_clip.resolve()),
    ])

    run_cmd([
        tools.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=64x64:d=0.1",
        "-frames:v",
        "1",
        str(cover.resolve()),
    ])

    run_cmd([
        tools.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(base_clip.resolve()),
        "-map",
        "0",
        "-c",
        "copy",
        "-attach",
        str(REAL_FONT.resolve()),
        "-metadata:s:t:0",
        "mimetype=application/x-truetype-font",
        "-metadata:s:t:0",
        "filename=DejaVuSans.ttf",
        "-attach",
        str(cover.resolve()),
        "-metadata:s:t:1",
        "mimetype=image/jpeg",
        "-metadata:s:t:1",
        "filename=cover.jpg",
        str(sdr_attach.resolve()),
    ])

    duration_ms = max(3, int(round(SAMPLE_DURATION_S * 1000)))
    first_end = duration_ms // 3
    second_end = (duration_ms * 2) // 3
    write_text(
        chapters,
        (
            ";FFMETADATA1\n\n"
            f"[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND={first_end}\ntitle=Intro\n\n"
            f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={first_end}\nEND={second_end}\ntitle=Middle\n\n"
            f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={second_end}\nEND={duration_ms}\ntitle=End\n"
        ),
    )

    run_cmd([
        tools.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(base_clip.resolve()),
        "-i",
        str(chapters.resolve()),
        "-map",
        "0",
        "-c",
        "copy",
        "-map_metadata",
        "0",
        "-map_chapters",
        "1",
        "-metadata",
        "title=Host Meta Source",
        "-metadata",
        "SOURCE=host-meta-source",
        "-metadata",
        "TMDB_ID=424242",
        "-metadata",
        "COMMENT=Prepared from host sample for workflow integration tests",
        str(sdr_meta.resolve()),
    ])

    run_cmd([
        tools.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        str(SAMPLE_START_S),
        "-t",
        str(SAMPLE_DURATION_S),
        "-i",
        str(HOST_DV_HDR10P.resolve()),
        "-map",
        "0",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        str(dv_hdr10p.resolve()),
    ])

    require_packet_streams(base_clip, tools, {0: "video", 1: "audio", 2: "audio"})
    require_packet_streams(dv_hdr10p, tools, {0: "video", 1: "audio", 3: "audio"})
    if not has_dovi(dv_hdr10p, tools):
        raise RuntimeError("Prepared DV/HDR10+ source lost Dolby Vision metadata.")
    if not has_hdr10plus(dv_hdr10p, tools):
        raise RuntimeError("Prepared DV/HDR10+ source lost HDR10+ metadata.")

    return PreparedSources(
        sdr_attach_source=sdr_attach.resolve(),
        sdr_meta_source=sdr_meta.resolve(),
        dv_hdr10plus_source=dv_hdr10p.resolve(),
    )


def _discover_font(explicit: str | None) -> Path:
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise RuntimeError(f"Police de test introuvable : {candidate}")
    candidates = sorted(Path("/usr/share/fonts").rglob("*.ttf"))
    if not candidates:
        candidates = sorted(Path("/usr/share/fonts").rglob("*.otf"))
    if not candidates:
        raise RuntimeError("Aucune police TTF/OTF réelle trouvée ; utiliser --font.")
    return candidates[0].resolve()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdr-source", default=os.environ.get("MUXIVEO_TEST_SDR_SOURCE"))
    parser.add_argument("--dv-source", default=os.environ.get("MUXIVEO_TEST_DV_SOURCE"))
    parser.add_argument(
        "--artifact-root",
        default=os.environ.get("MUXIVEO_TEST_ARTIFACT_ROOT", str(ARTIFACT_ROOT)),
    )
    parser.add_argument(
        "--sample-start", type=float,
        default=float(os.environ.get("MUXIVEO_TEST_SAMPLE_START", "0")),
    )
    parser.add_argument(
        "--sample-duration", type=float,
        default=float(os.environ.get("MUXIVEO_TEST_SAMPLE_DURATION", "12")),
    )
    parser.add_argument("--distrobox", default=os.environ.get("MUXIVEO_TEST_DISTROBOX"))
    parser.add_argument("--font", default=os.environ.get("MUXIVEO_TEST_FONT"))
    args = parser.parse_args(argv)
    if not args.sdr_source or not args.dv_source:
        parser.error(
            "sources réelles obligatoires : --sdr-source et --dv-source "
            "(ou MUXIVEO_TEST_SDR_SOURCE / MUXIVEO_TEST_DV_SOURCE)"
        )
    if args.sample_start < 0 or args.sample_duration <= 0:
        parser.error("--sample-start doit être >= 0 et --sample-duration > 0")
    for name in ("sdr_source", "dv_source"):
        path = Path(getattr(args, name)).expanduser().resolve()
        if not path.is_file():
            parser.error(f"source réelle introuvable : {path}")
        setattr(args, name, path)
    return args


def configure_runtime(args: argparse.Namespace) -> None:
    global ARTIFACT_ROOT, CORPUS_ROOT, RUNS_ROOT, BIN_ROOT, REPORT_PATH, SUMMARY_PATH
    global HOST_SDR_SAMPLE, HOST_DV_HDR10P, REAL_FONT, SAMPLE_START_S, SAMPLE_DURATION_S
    ARTIFACT_ROOT = Path(args.artifact_root).expanduser().resolve()
    CORPUS_ROOT = ARTIFACT_ROOT / "corpus"
    RUNS_ROOT = ARTIFACT_ROOT / "runs"
    BIN_ROOT = ARTIFACT_ROOT / "bin"
    REPORT_PATH = ARTIFACT_ROOT / "report.json"
    SUMMARY_PATH = ARTIFACT_ROOT / "summary.md"
    HOST_SDR_SAMPLE = Path(args.sdr_source)
    HOST_DV_HDR10P = Path(args.dv_source)
    REAL_FONT = _discover_font(args.font)
    SAMPLE_START_S = float(args.sample_start)
    SAMPLE_DURATION_S = float(args.sample_duration)


def wait_task(signals, timeout_s: float) -> dict[str, Any]:
    app = ensure_app()
    state: dict[str, Any] = {
        "finished": None,
        "failed": None,
        "cancelled": False,
        "progress": [],
    }
    done = {"value": False}

    signals.progress.connect(
        lambda msg: state["progress"].append(msg),
        Qt.ConnectionType.QueuedConnection,
    )

    def on_finished(result: object) -> None:
        state["finished"] = result
        done["value"] = True

    def on_failed(msg: str, exc: object) -> None:
        state["failed"] = {"message": msg, "exception": str(exc)}
        done["value"] = True

    def on_cancelled() -> None:
        state["cancelled"] = True
        done["value"] = True

    signals.finished.connect(on_finished, Qt.ConnectionType.QueuedConnection)
    signals.failed.connect(on_failed, Qt.ConnectionType.QueuedConnection)
    signals.cancelled.connect(on_cancelled, Qt.ConnectionType.QueuedConnection)

    deadline = time.monotonic() + timeout_s
    while not done["value"] and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    if not done["value"]:
        raise TimeoutError(f"Timeout waiting for workflow after {timeout_s:.1f}s")
    return state


def workflow_logs(workflow: Any) -> list[dict[str, str]]:
    logs: list[dict[str, str]] = []
    workflow.log_message.connect(
        lambda level, msg: logs.append({"level": str(level), "message": str(msg)}),
        Qt.ConnectionType.QueuedConnection,
    )
    return logs


def inspect_info(path: Path, tools: ToolPaths) -> FileInfo:
    inspector = FileInspector(ffprobe_bin=tools.ffprobe, mediainfo_bin=tools.mediainfo)
    return inspector.inspect(path.resolve())


def probe_json(path: Path, tools: ToolPaths) -> dict[str, Any]:
    proc = run_cmd([
        tools.ffprobe,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        str(path.resolve()),
    ])
    return json.loads(proc.stdout or "{}")


def tag_lookup(tags: dict[str, Any] | None, key: str) -> str | None:
    if not tags:
        return None
    for existing, value in tags.items():
        if str(existing).lower() == key.lower():
            return str(value)
    return None


def file_title(info: FileInfo) -> str:
    return (info.title or "").strip()


def chapter_titles(info: FileInfo) -> list[str]:
    if info.chapters is None:
        return []
    return [entry.name for entry in info.chapters.entries]


def attachment_names(info: FileInfo) -> list[str]:
    return [att.filename for att in info.attachments]


def has_attachment_name(info: FileInfo, *candidates: str) -> bool:
    names = {name.lower() for name in attachment_names(info)}
    return any(candidate.lower() in names for candidate in candidates)


def stream_titles(streams: list[Any]) -> list[str]:
    return [(getattr(stream, "title", "") or "").strip() for stream in streams]


def stream_languages(streams: list[Any]) -> list[str]:
    return [(getattr(stream, "language", "") or "").strip() for stream in streams]


def extract_main_video_hevc(input_path: Path, output_hevc: Path, tools: ToolPaths) -> None:
    run_cmd([
        tools.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(input_path.resolve()),
        "-map",
        "0:v:0",
        "-c",
        "copy",
        "-f",
        "hevc",
        str(output_hevc.resolve()),
    ])


def has_dovi(path: Path, tools: ToolPaths) -> bool:
    tmp = ARTIFACT_ROOT / "tmp" / f"{path.stem}.{uuid.uuid4().hex}.hevc"
    rpu = tmp.with_suffix(".rpu.bin")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        extract_main_video_hevc(path, tmp, tools)
        proc = run_cmd(
            [
                tools.dovi_tool,
                "extract-rpu",
                "-i",
                str(tmp.resolve()),
                "-o",
                str(rpu.resolve()),
            ],
            check=False,
        )
        return proc.returncode == 0 and rpu.exists() and rpu.stat().st_size > 0
    finally:
        tmp.unlink(missing_ok=True)
        rpu.unlink(missing_ok=True)


def has_hdr10plus(path: Path, tools: ToolPaths) -> bool:
    tmp = ARTIFACT_ROOT / "tmp" / f"{path.stem}.{uuid.uuid4().hex}.hevc"
    meta = tmp.with_suffix(".hdr10plus.json")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        extract_main_video_hevc(path, tmp, tools)
        proc = run_cmd(
            [
                tools.hdr10plus_tool,
                "extract",
                str(tmp.resolve()),
                "-o",
                str(meta.resolve()),
            ],
            check=False,
        )
        if proc.returncode != 0 or not meta.exists() or meta.stat().st_size == 0:
            return False
        payload = json.loads(meta.read_text(encoding="utf-8"))
        scene_info = payload.get("SceneInfo") or payload.get("scene_info") or []
        return bool(scene_info)
    finally:
        tmp.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)


def mediainfo_hdr(path: Path, tools: ToolPaths) -> str:
    proc = run_cmd(
        [tools.mediainfo, "--Inform=Video;%HDR_Format%|%HDR_Format_Compatibility%", str(path.resolve())],
        check=False,
    )
    return (proc.stdout or "").strip()


def process_dir_state(work_root: Path, output_path: Path) -> dict[str, Any]:
    process_dir = work_root / process_folder_name_from_output(output_path.resolve())
    top_entries = [str(p.name) for p in work_dir_entries(process_dir)]
    payload_files: list[str] = []
    if process_dir.exists():
        for child in sorted(process_dir.rglob("*")):
            if child.is_file():
                payload_files.append(str(child.relative_to(process_dir)))
    return {
        "process_dir": str(process_dir),
        "exists": process_dir.exists(),
        "top_entries": top_entries,
        "payload_files": payload_files[:50],
        "payload_file_count": len(payload_files),
    }


def make_cover_under_tmdb_root(work_root: Path, filename: str = "cover.jpg") -> Path:
    target = work_root / "tmdb_covers" / uuid.uuid4().hex / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    run_cmd([
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=green:s=96x96:d=0.1",
        "-frames:v",
        "1",
        str(target.resolve()),
    ])
    return target


def seed_stale_file(work_root: Path, output_path: Path) -> Path:
    process_dir = work_root / process_folder_name_from_output(output_path.resolve())
    process_dir.mkdir(parents=True, exist_ok=True)
    stale = process_dir / "stale.tmp"
    stale.write_text("stale", encoding="utf-8")
    return stale


def build_source_input(
    source_path: Path,
    tools: ToolPaths,
    *,
    file_index: int = 0,
    track_mutator: Callable[[list[TrackEntry]], list[TrackEntry]] | None = None,
    include_all_attachments: bool = False,
) -> tuple[SourceInput, FileInfo]:
    info = inspect_info(source_path, tools)
    tracks = tracks_from_file_info(info)
    if track_mutator is not None:
        tracks = track_mutator(copy.deepcopy(tracks))
    source = SourceInput(
        path=source_path.resolve(),
        file_index=file_index,
        tracks=tracks,
        selected_attachments=copy.deepcopy(info.attachments) if include_all_attachments else [],
        attachment_count=len(info.attachments),
        copy_tags=False,
    )
    return source, info


def select_tracks(tracks: list[TrackEntry], *, keep: list[tuple[str, int]] | None = None) -> list[tuple[int, int]]:
    if keep is None:
        return [(0, track.mkv_tid) for track in tracks]
    grouped: dict[str, list[TrackEntry]] = {"video": [], "audio": [], "subtitle": []}
    for track in tracks:
        grouped.setdefault(track.track_type, []).append(track)
    order: list[tuple[int, int]] = []
    for track_type, index in keep:
        order.append((0, grouped[track_type][index].mkv_tid))
    return order


def preserve_tracks_mutator(tracks: list[TrackEntry]) -> list[TrackEntry]:
    audios = [track for track in tracks if track.track_type == "audio"]
    subs = [track for track in tracks if track.track_type == "subtitle"]
    videos = [track for track in tracks if track.track_type == "video"]

    if videos:
        videos[0].title = "Main SDR Video"
    if audios:
        audios[0].language = "fr-FR"
        audios[0].title = "VF Principale"
    if len(audios) > 1:
        audios[1].language = "en-US"
        audios[1].title = "VO Atmos"
    if subs:
        subs[0].language = "fr-FR"
        subs[0].title = "FR Forced"
        subs[0].flag_default = True
        subs[0].flag_forced = True
    if len(subs) > 1:
        subs[1].language = "fr-FR"
        subs[1].title = "FR SDH"
        subs[1].flag_hearing_impaired = True
    return tracks


def cleanup_tracks_mutator(tracks: list[TrackEntry]) -> list[TrackEntry]:
    audios = [track for track in tracks if track.track_type == "audio"]
    if audios:
        audios[0].language = ""
        audios[0].title = ""
    return tracks


def override_tracks_mutator(tracks: list[TrackEntry]) -> list[TrackEntry]:
    audios = [track for track in tracks if track.track_type == "audio"]
    subs = [track for track in tracks if track.track_type == "subtitle"]
    if audios:
        audios[0].language = "fr-FR"
        audios[0].title = "VF Retenue"
    if subs:
        subs[0].language = "fr-FR"
        subs[0].title = "Forced FR Kept"
        subs[0].flag_default = True
        subs[0].flag_forced = True
    return tracks


def make_track_meta_edits(
    *,
    audio_count: int,
    subtitle_count: int,
    video: dict[str, Any] | None = None,
    audios: list[dict[str, Any]] | None = None,
    subtitles: list[dict[str, Any]] | None = None,
) -> list[TrackMetaEdit]:
    edits: list[TrackMetaEdit] = []
    if video is not None:
        edits.append(track_meta_edit(1, **video))
    for offset, payload in enumerate(audios or [], start=2):
        edits.append(track_meta_edit(offset, **payload))
    sub_start = 2 + audio_count
    for offset, payload in enumerate(subtitles or [], start=sub_start):
        edits.append(track_meta_edit(offset, **payload))
    return edits


def track_meta_edit(track_order: int, **payload: Any) -> TrackMetaEdit:
    data = {
        "track_order": track_order,
        "language": payload.get("language", ""),
        "title": payload.get("title"),
        "flag_default": payload.get("flag_default"),
        "flag_forced": payload.get("flag_forced"),
        "flag_hearing_impaired": payload.get("flag_hearing_impaired"),
        "flag_visual_impaired": payload.get("flag_visual_impaired"),
        "flag_original": payload.get("flag_original"),
        "flag_commentary": payload.get("flag_commentary"),
    }
    return TrackMetaEdit(**data)


def audio_settings_from_info(
    info: FileInfo,
    specs: list[tuple[int, str, int | None]],
) -> list[AudioTrackSettings]:
    by_index = {track.index: track for track in info.audio_tracks}
    items: list[AudioTrackSettings] = []
    for stream_index, codec, bitrate in specs:
        src = by_index[stream_index]
        items.append(AudioTrackSettings(
            stream_index=stream_index,
            codec=codec,
            bitrate_kbps=bitrate or 384,
            input_channels=src.channels,
            input_channel_layout=src.channel_layout,
        ))
    return items


def summarize_info(info: FileInfo, tools: ToolPaths, path: Path) -> dict[str, Any]:
    main_video_codec = info.video_tracks[0].codec if info.video_tracks else ""
    dynamic_hdr = main_video_codec.lower() == "hevc"
    return {
        "path": str(path),
        "video_tracks": len(info.video_tracks),
        "audio_tracks": len(info.audio_tracks),
        "subtitle_tracks": len(info.subtitle_tracks),
        "attachments": len(info.attachments),
        "attachment_names": attachment_names(info),
        "chapters": 0 if info.chapters is None else info.chapters.count,
        "chapter_titles": chapter_titles(info),
        "title": file_title(info),
        "global_tags": info.global_tags,
        "audio_languages": stream_languages(info.audio_tracks),
        "audio_titles": stream_titles(info.audio_tracks),
        "subtitle_languages": stream_languages(info.subtitle_tracks),
        "subtitle_titles": stream_titles(info.subtitle_tracks),
        "has_dovi": has_dovi(path, tools) if dynamic_hdr else False,
        "has_hdr10plus": has_hdr10plus(path, tools) if dynamic_hdr else False,
        "mediainfo_hdr": mediainfo_hdr(path, tools),
    }


def finalize_case(
    *,
    case_id: str,
    workflow_name: str,
    branch: str,
    source: Path,
    case_root: Path,
    output_path: Path,
    work_root: Path,
    started: float,
    logs: list[dict[str, str]],
    signal_state: dict[str, Any] | None,
    info: FileInfo | None,
    functional_checks: dict[str, bool],
    cleanup_checks: dict[str, bool],
    notes: list[str],
    tools: ToolPaths,
    exception: Exception | None = None,
    backend: str = "",
) -> dict[str, Any]:
    ended = time.monotonic()
    work_state = process_dir_state(work_root, output_path)
    result: dict[str, Any] = {
        "id": case_id,
        "workflow": workflow_name,
        "branch": branch,
        "backend": backend,
        "source": str(source),
        "output": str(output_path),
        "started_at": iso_now(),
        "duration_s": round(ended - started, 3),
        "logs": logs,
        "progress_tail": (signal_state or {}).get("progress", [])[-25:],
        "signal_state": signal_state,
        "functional_checks": functional_checks,
        "cleanup_checks": cleanup_checks,
        "notes": notes,
        "workdir_state": work_state,
        "output_summary": summarize_info(info, tools, output_path) if info is not None else None,
    }
    if exception is not None:
        result["status"] = "fail"
        result["error"] = str(exception)
    else:
        functional_pass = all(functional_checks.values()) if functional_checks else True
        cleanup_pass = all(cleanup_checks.values()) if cleanup_checks else True
        if not functional_pass:
            result["status"] = "fail"
        elif not cleanup_pass or notes:
            result["status"] = "warn"
        else:
            result["status"] = "pass"
    return result


def append_output_checks(result: dict[str, Any], tools: ToolPaths) -> dict[str, Any]:
    output = Path(str(result.get("output") or ""))
    if result.get("status") == "fail" or not output.is_file():
        result["output_checks"] = {"ffprobe": False}
        return result
    checks: dict[str, bool] = {}
    diagnostics: list[str] = []
    for name, command in (("ffprobe", [tools.ffprobe, "-v", "error", "-show_format", str(output)]),):
        proc = run_cmd(command, check=False)
        checks[name] = proc.returncode == 0
        if proc.returncode != 0:
            diagnostics.append(f"{name}: {(proc.stderr or proc.stdout or '').strip()}")
    result["output_checks"] = checks
    if not all(checks.values()):
        result["status"] = "fail"
        result["error"] = "Validation de sortie échouée : " + " ; ".join(diagnostics)
    return result


def skipped_case_result(
    *,
    case_id: str,
    workflow_name: str,
    branch: str,
    source: Path,
    reason: str,
) -> dict[str, Any]:
    return {
        "id": case_id,
        "workflow": workflow_name,
        "branch": branch,
        "source": str(source),
        "output": "",
        "started_at": iso_now(),
        "duration_s": 0.0,
        "logs": [],
        "progress_tail": [],
        "signal_state": None,
        "functional_checks": {},
        "cleanup_checks": {},
        "notes": [reason],
        "workdir_state": None,
        "output_summary": None,
        "status": "skipped",
        "skip_reason": reason,
    }


def build_encode_workflow(tools: ToolPaths) -> EncodeWorkflow:
    workflow = EncodeWorkflow(
        ffmpeg_bin=tools.ffmpeg,
        dovi_tool_bin=tools.dovi_tool,
        hdr10plus_bin=tools.hdr10plus_tool,
        ram_buffer_enabled=False,
        ffmpeg_threads=4,
    )
    workflow._bins["mediainfo"] = tools.mediainfo
    return workflow


def run_remux_case_attachments(
    case_id: str, sources: PreparedSources, tools: ToolPaths, *, backend: str,
) -> dict[str, Any]:
    case_root = RUNS_ROOT / case_id
    case_root.mkdir(parents=True, exist_ok=True)
    output_path = (case_root / "out.mkv").resolve()
    work_root = (case_root / "work").resolve()
    stale = seed_stale_file(work_root, output_path)

    workflow = RemuxWorkflow(
        ffmpeg_bin=tools.ffmpeg,
        ffprobe_bin=tools.ffprobe,
        ffmpeg_threads=4,
    )
    logs = workflow_logs(workflow)
    started = time.monotonic()

    source, source_info = build_source_input(
        sources.sdr_attach_source,
        tools,
        track_mutator=preserve_tracks_mutator,
        include_all_attachments=True,
    )
    cfg = RemuxConfig(
        sources=[source],
        output=output_path,
        track_order=select_tracks(source.tracks),
        keep_chapters=False,
        file_title="Attach Source Remux FFmpeg",
        tag_overrides={"TEST_CASE": case_id, "SOURCE": "remux-ffmpeg-attachments"},
        work_dir=work_root,
        mux_backend=backend,
    )

    try:
        state = wait_task(workflow.run(cfg), CASE_TIMEOUT_S)
        info = inspect_info(output_path, tools)
        functional = {
            "output_exists": output_path.exists(),
            "audio_count_2": len(info.audio_tracks) == 2,
            "subtitle_count_4": len(info.subtitle_tracks) == 4,
            "attachments_2": len(info.attachments) == 2,
            "has_font_attachment": "DejaVuSans.ttf" in attachment_names(info),
            "has_cover_attachment": has_attachment_name(info, "cover", "cover.jpg"),
            "title_written": file_title(info) == "Attach Source Remux FFmpeg",
            "test_case_tag_written": tag_lookup(info.global_tags, "TEST_CASE") == case_id,
            "audio_titles_written": stream_titles(info.audio_tracks)[:2] == ["VF Principale", "VO Atmos"],
            "subtitle_title_written": stream_titles(info.subtitle_tracks)[0] == "FR Forced",
        }
        cleanup = {
            "stale_file_removed": not stale.exists(),
            "process_dir_removed": not Path(process_dir_state(work_root, output_path)["process_dir"]).exists(),
        }
        return finalize_case(
            case_id=case_id,
            workflow_name="remux",
            branch="source_attachments_preserve",
            source=sources.sdr_attach_source,
            case_root=case_root,
            output_path=output_path,
            work_root=work_root,
            started=started,
            logs=logs,
            signal_state=state,
            info=info,
            functional_checks=functional,
            cleanup_checks=cleanup,
            notes=[],
            tools=tools,
            backend=backend,
        )
    except Exception as exc:
        return finalize_case(
            case_id=case_id,
            workflow_name="remux",
            branch="source_attachments_preserve",
            source=sources.sdr_attach_source,
            case_root=case_root,
            output_path=output_path,
            work_root=work_root,
            started=started,
            logs=logs,
            signal_state=None,
            info=None,
            functional_checks={},
            cleanup_checks={},
            notes=[],
            tools=tools,
            exception=exc,
            backend=backend,
        )


def run_remux_case_override(
    case_id: str, sources: PreparedSources, tools: ToolPaths, *, backend: str,
) -> dict[str, Any]:
    case_root = RUNS_ROOT / case_id
    case_root.mkdir(parents=True, exist_ok=True)
    output_path = (case_root / "out.mkv").resolve()
    work_root = (case_root / "work").resolve()
    stale = seed_stale_file(work_root, output_path)
    extra_cover = make_cover_under_tmdb_root(work_root)

    workflow = RemuxWorkflow(
        ffmpeg_bin=tools.ffmpeg,
        ffprobe_bin=tools.ffprobe,
        ffmpeg_threads=4,
    )
    logs = workflow_logs(workflow)
    started = time.monotonic()

    source, _ = build_source_input(
        sources.sdr_meta_source,
        tools,
        track_mutator=override_tracks_mutator,
        include_all_attachments=False,
    )
    cfg = RemuxConfig(
        sources=[source],
        output=output_path,
        track_order=select_tracks(source.tracks, keep=[("video", 0), ("audio", 0), ("subtitle", 0)]),
        chapter_overrides=[
            ChapterEntry(timecode_s=0.0, name="Case Intro"),
            ChapterEntry(timecode_s=9.0, name="Case Outro"),
        ],
        file_title="Meta Override FFmpeg",
        tag_overrides={
            "SOURCE": "override-source",
            "TMDB_ID": "R2",
            "COMMENT": "override comment",
        },
        extra_attachments=[extra_cover.resolve()],
        work_dir=work_root,
        mux_backend=backend,
    )

    try:
        state = wait_task(workflow.run(cfg), CASE_TIMEOUT_S)
        info = inspect_info(output_path, tools)
        tmdb_root = work_root / "tmdb_covers"
        functional = {
            "output_exists": output_path.exists(),
            "audio_count_1": len(info.audio_tracks) == 1,
            "subtitle_count_1": len(info.subtitle_tracks) == 1,
            "attachments_1": len(info.attachments) == 1,
            "cover_attached": has_attachment_name(info, "cover", "cover.jpg"),
            "title_override_written": file_title(info) == "Meta Override FFmpeg",
            "source_tag_override": tag_lookup(info.global_tags, "SOURCE") == "override-source",
            "tmdb_id_override": tag_lookup(info.global_tags, "TMDB_ID") == "R2",
            "custom_chapters_written": chapter_titles(info) == ["Case Intro", "Case Outro"],
        }
        cleanup = {
            "stale_file_removed": not stale.exists(),
            "tmdb_root_empty": not any(tmdb_root.rglob("*")) if tmdb_root.exists() else True,
            "process_dir_removed": not Path(process_dir_state(work_root, output_path)["process_dir"]).exists(),
        }
        return finalize_case(
            case_id=case_id,
            workflow_name="remux",
            branch="tags_chapters_override_tmdb_cover",
            source=sources.sdr_meta_source,
            case_root=case_root,
            output_path=output_path,
            work_root=work_root,
            started=started,
            logs=logs,
            signal_state=state,
            info=info,
            functional_checks=functional,
            cleanup_checks=cleanup,
            notes=[],
            tools=tools,
            backend=backend,
        )
    except Exception as exc:
        return finalize_case(
            case_id=case_id,
            workflow_name="remux",
            branch="tags_chapters_override_tmdb_cover",
            source=sources.sdr_meta_source,
            case_root=case_root,
            output_path=output_path,
            work_root=work_root,
            started=started,
            logs=logs,
            signal_state=None,
            info=None,
            functional_checks={},
            cleanup_checks={},
            notes=[],
            tools=tools,
            exception=exc,
            backend=backend,
        )


def run_remux_case_cleanup(
    case_id: str, sources: PreparedSources, tools: ToolPaths, *, backend: str,
) -> dict[str, Any]:
    case_root = RUNS_ROOT / case_id
    case_root.mkdir(parents=True, exist_ok=True)
    output_path = (case_root / "out.mkv").resolve()
    work_root = (case_root / "work").resolve()
    stale = seed_stale_file(work_root, output_path)

    workflow = RemuxWorkflow(
        ffmpeg_bin=tools.ffmpeg,
        ffprobe_bin=tools.ffprobe,
        ffmpeg_threads=4,
    )
    logs = workflow_logs(workflow)
    started = time.monotonic()

    source, _ = build_source_input(
        sources.sdr_meta_source,
        tools,
        track_mutator=cleanup_tracks_mutator,
        include_all_attachments=False,
    )
    cfg = RemuxConfig(
        sources=[source],
        output=output_path,
        track_order=select_tracks(source.tracks, keep=[("video", 0), ("audio", 0)]),
        keep_chapters=False,
        file_title="",
        tag_overrides={},
        work_dir=work_root,
        mux_backend=backend,
    )

    try:
        state = wait_task(workflow.run(cfg), CASE_TIMEOUT_S)
        info = inspect_info(output_path, tools)
        functional = {
            "output_exists": output_path.exists(),
            "audio_count_1": len(info.audio_tracks) == 1,
            "subtitle_count_0": len(info.subtitle_tracks) == 0,
            "attachments_0": len(info.attachments) == 0,
            "chapters_removed": chapter_titles(info) == [],
            "title_removed": file_title(info) == "",
            "source_tag_removed": tag_lookup(info.global_tags, "SOURCE") is None,
            "tmdb_id_removed": tag_lookup(info.global_tags, "TMDB_ID") is None,
            "audio_language_removed": stream_languages(info.audio_tracks)[0] == "",
        }
        cleanup = {
            "stale_file_removed": not stale.exists(),
            "process_dir_removed": not Path(process_dir_state(work_root, output_path)["process_dir"]).exists(),
        }
        return finalize_case(
            case_id=case_id,
            workflow_name="remux",
            branch="cleanup_delete_metadata",
            source=sources.sdr_meta_source,
            case_root=case_root,
            output_path=output_path,
            work_root=work_root,
            started=started,
            logs=logs,
            signal_state=state,
            info=info,
            functional_checks=functional,
            cleanup_checks=cleanup,
            notes=[],
            tools=tools,
            backend=backend,
        )
    except Exception as exc:
        return finalize_case(
            case_id=case_id,
            workflow_name="remux",
            branch="cleanup_delete_metadata",
            source=sources.sdr_meta_source,
            case_root=case_root,
            output_path=output_path,
            work_root=work_root,
            started=started,
            logs=logs,
            signal_state=None,
            info=None,
            functional_checks={},
            cleanup_checks={},
            notes=[],
            tools=tools,
            exception=exc,
            backend=backend,
        )


def run_encode_case_copy_attachments(case_id: str, sources: PreparedSources, tools: ToolPaths) -> dict[str, Any]:
    case_root = RUNS_ROOT / case_id
    case_root.mkdir(parents=True, exist_ok=True)
    output_path = (case_root / "out.mkv").resolve()
    work_root = (case_root / "work").resolve()
    stale = seed_stale_file(work_root, output_path)

    info = inspect_info(sources.sdr_attach_source, tools)
    attachments = [(sources.sdr_attach_source.resolve(), att.index) for att in info.attachments]

    workflow = build_encode_workflow(tools)
    logs = workflow_logs(workflow)
    started = time.monotonic()

    cfg = EncodeConfig(
        source=sources.sdr_attach_source.resolve(),
        output=output_path,
        video=VideoEncodeSettings(codec="copy", quality_mode=QualityMode.CRF, crf=18, preset="ultrafast"),
        audio_tracks=audio_settings_from_info(info, [(1, "copy", None), (2, "copy", None)]),
        copy_subtitles=False,
        subtitle_tracks=[
            (sources.sdr_attach_source.resolve(), 3),
            (sources.sdr_attach_source.resolve(), 4),
        ],
        attachment_streams=attachments,
        tag_overrides={"TEST_CASE": case_id, "SOURCE": "encode-copy-attachments"},
        track_meta_edits=make_track_meta_edits(
            audio_count=2,
            subtitle_count=2,
            video={"title": "Copy Video"},
            audios=[
                {"language": "fr-FR", "title": "VF Copy", "flag_default": True},
                {"language": "en-US", "title": "VO Copy"},
            ],
            subtitles=[
                {"language": "fr-FR", "title": "Copy Forced", "flag_default": True, "flag_forced": True},
                {"language": "fr-FR", "title": "Copy Full"},
            ],
        ),
        file_title="Encode Copy Attach",
        keep_chapters=False,
        work_dir=work_root,
    )

    try:
        state = wait_task(workflow.run(cfg), CASE_TIMEOUT_S)
        out_info = inspect_info(output_path, tools)
        work_state = process_dir_state(work_root, output_path)
        functional = {
            "output_exists": output_path.exists(),
            "audio_count_2": len(out_info.audio_tracks) == 2,
            "subtitle_count_2": len(out_info.subtitle_tracks) == 2,
            "attachments_2": len(out_info.attachments) == 2,
            "font_attachment_present": "DejaVuSans.ttf" in attachment_names(out_info),
            "cover_attachment_present": has_attachment_name(out_info, "cover", "cover.jpg"),
            "title_written": file_title(out_info) == "Encode Copy Attach",
            "test_case_tag_written": tag_lookup(out_info.global_tags, "TEST_CASE") == case_id,
            "audio_titles_written": stream_titles(out_info.audio_tracks) == ["VF Copy", "VO Copy"],
        }
        cleanup = {
            "stale_file_removed": not stale.exists(),
            "attachment_tmp_dir_cleaned": not any("enc_attachments_" in part for part in work_state["payload_files"]),
        }
        notes: list[str] = []
        if work_state["payload_file_count"] > 0:
            notes.append("Le process_dir encode copy reste présent avec du contenu résiduel.")
        return finalize_case(
            case_id=case_id,
            workflow_name="encode",
            branch="copy_source_attachments",
            source=sources.sdr_attach_source,
            case_root=case_root,
            output_path=output_path,
            work_root=work_root,
            started=started,
            logs=logs,
            signal_state=state,
            info=out_info,
            functional_checks=functional,
            cleanup_checks=cleanup,
            notes=notes,
            tools=tools,
        )
    except Exception as exc:
        return finalize_case(
            case_id=case_id,
            workflow_name="encode",
            branch="copy_source_attachments",
            source=sources.sdr_attach_source,
            case_root=case_root,
            output_path=output_path,
            work_root=work_root,
            started=started,
            logs=logs,
            signal_state=None,
            info=None,
            functional_checks={},
            cleanup_checks={},
            notes=[],
            tools=tools,
            exception=exc,
        )


def run_encode_case_single_pass(case_id: str, sources: PreparedSources, tools: ToolPaths) -> dict[str, Any]:
    case_root = RUNS_ROOT / case_id
    case_root.mkdir(parents=True, exist_ok=True)
    output_path = (case_root / "out.mkv").resolve()
    work_root = (case_root / "work").resolve()
    stale = seed_stale_file(work_root, output_path)
    extra_cover = make_cover_under_tmdb_root(work_root)

    info = inspect_info(sources.sdr_meta_source, tools)
    workflow = build_encode_workflow(tools)
    logs = workflow_logs(workflow)
    started = time.monotonic()

    cfg = EncodeConfig(
        source=sources.sdr_meta_source.resolve(),
        output=output_path,
        video=VideoEncodeSettings(codec="libx264", quality_mode=QualityMode.CRF, crf=30, preset="ultrafast"),
        audio_tracks=audio_settings_from_info(info, [(1, "copy", None), (2, "aac", 192)]),
        copy_subtitles=False,
        subtitle_tracks=[(sources.sdr_meta_source.resolve(), 3)],
        keep_chapters=True,
        chapter_overrides=[
            ChapterEntry(timecode_s=0.0, name="E2 Intro"),
            ChapterEntry(timecode_s=9.0, name="E2 Outro"),
        ],
        extra_attachments=[extra_cover.resolve()],
        tag_overrides={"SOURCE": "encode-single", "TMDB_ID": "E2", "COMMENT": "single-pass"},
        track_meta_edits=make_track_meta_edits(
            audio_count=2,
            subtitle_count=1,
            video={"title": "Single Pass Video"},
            audios=[
                {"language": "fr-FR", "title": "VF Copy", "flag_default": True},
                {"language": "en-US", "title": "VO AAC"},
            ],
            subtitles=[
                {"language": "fr-FR", "title": "Forced FR", "flag_default": True, "flag_forced": True},
            ],
        ),
        file_title="Encode Single Pass",
        duration_s=info.duration_s,
        work_dir=work_root,
    )

    try:
        state = wait_task(workflow.run(cfg), CASE_TIMEOUT_S)
        out_info = inspect_info(output_path, tools)
        work_state = process_dir_state(work_root, output_path)
        functional = {
            "output_exists": output_path.exists(),
            "video_codec_h264": probe_json(output_path, tools)["streams"][0]["codec_name"] == "h264",
            "audio_count_2": len(out_info.audio_tracks) == 2,
            "subtitle_count_1": len(out_info.subtitle_tracks) == 1,
            "cover_attached": has_attachment_name(out_info, "cover", "cover.jpg"),
            "title_written": file_title(out_info) == "Encode Single Pass",
            "source_tag_override": tag_lookup(out_info.global_tags, "SOURCE") == "encode-single",
            "tmdb_id_override": tag_lookup(out_info.global_tags, "TMDB_ID") == "E2",
            "custom_chapters_written": chapter_titles(out_info) == ["E2 Intro", "E2 Outro"],
            "audio_titles_written": stream_titles(out_info.audio_tracks) == ["VF Copy", "VO AAC"],
        }
        cleanup = {
            "stale_file_removed": not stale.exists(),
            "chapter_tmp_dir_cleaned": not any("enc_chapters_" in part for part in work_state["payload_files"]),
        }
        notes: list[str] = []
        if work_state["payload_file_count"] > 0:
            notes.append("Le process_dir encode single-pass garde des fichiers après succès.")
        return finalize_case(
            case_id=case_id,
            workflow_name="encode",
            branch="single_pass_reencode",
            source=sources.sdr_meta_source,
            case_root=case_root,
            output_path=output_path,
            work_root=work_root,
            started=started,
            logs=logs,
            signal_state=state,
            info=out_info,
            functional_checks=functional,
            cleanup_checks=cleanup,
            notes=notes,
            tools=tools,
        )
    except Exception as exc:
        return finalize_case(
            case_id=case_id,
            workflow_name="encode",
            branch="single_pass_reencode",
            source=sources.sdr_meta_source,
            case_root=case_root,
            output_path=output_path,
            work_root=work_root,
            started=started,
            logs=logs,
            signal_state=None,
            info=None,
            functional_checks={},
            cleanup_checks={},
            notes=[],
            tools=tools,
            exception=exc,
        )


def run_encode_case_two_pass(case_id: str, sources: PreparedSources, tools: ToolPaths) -> dict[str, Any]:
    case_root = RUNS_ROOT / case_id
    case_root.mkdir(parents=True, exist_ok=True)
    output_path = (case_root / "out.mkv").resolve()
    work_root = (case_root / "work").resolve()
    stale = seed_stale_file(work_root, output_path)

    info = inspect_info(sources.sdr_meta_source, tools)
    workflow = build_encode_workflow(tools)
    logs = workflow_logs(workflow)
    started = time.monotonic()

    cfg = EncodeConfig(
        source=sources.sdr_meta_source.resolve(),
        output=output_path,
        video=VideoEncodeSettings(
            codec="libx264",
            quality_mode=QualityMode.SIZE,
            target_size_mb=10,
            bitrate_kbps=2500,
            preset="ultrafast",
        ),
        audio_tracks=audio_settings_from_info(info, [(1, "copy", None)]),
        copy_subtitles=False,
        subtitle_tracks=[],
        keep_chapters=False,
        tag_overrides={},
        track_meta_edits=make_track_meta_edits(
            audio_count=1,
            subtitle_count=0,
            audios=[{"language": "und", "title": ""}],
        ),
        file_title="",
        duration_s=info.duration_s,
        work_dir=work_root,
    )

    try:
        state = wait_task(workflow.run(cfg), CASE_TIMEOUT_S)
        out_info = inspect_info(output_path, tools)
        work_state = process_dir_state(work_root, output_path)
        functional = {
            "output_exists": output_path.exists(),
            "video_codec_h264": probe_json(output_path, tools)["streams"][0]["codec_name"] == "h264",
            "audio_count_1": len(out_info.audio_tracks) == 1,
            "subtitle_count_0": len(out_info.subtitle_tracks) == 0,
            "chapters_removed": chapter_titles(out_info) == [],
            "title_removed": file_title(out_info) == "",
            "source_tag_removed": tag_lookup(out_info.global_tags, "SOURCE") is None,
        }
        cleanup = {
            "stale_file_removed": not stale.exists(),
            "passlog_cleaned": not any("ffmpeg2pass" in part for part in work_state["payload_files"]),
        }
        notes: list[str] = []
        if not cleanup["passlog_cleaned"]:
            notes.append("Les fichiers ffmpeg2pass restent dans le process_dir après le 2-pass.")
        return finalize_case(
            case_id=case_id,
            workflow_name="encode",
            branch="two_pass_size_cleanup",
            source=sources.sdr_meta_source,
            case_root=case_root,
            output_path=output_path,
            work_root=work_root,
            started=started,
            logs=logs,
            signal_state=state,
            info=out_info,
            functional_checks=functional,
            cleanup_checks=cleanup,
            notes=notes,
            tools=tools,
        )
    except Exception as exc:
        return finalize_case(
            case_id=case_id,
            workflow_name="encode",
            branch="two_pass_size_cleanup",
            source=sources.sdr_meta_source,
            case_root=case_root,
            output_path=output_path,
            work_root=work_root,
            started=started,
            logs=logs,
            signal_state=None,
            info=None,
            functional_checks={},
            cleanup_checks={},
            notes=[],
            tools=tools,
            exception=exc,
        )


def run_encode_case_copy_dv_hdr10p(case_id: str, sources: PreparedSources, tools: ToolPaths) -> dict[str, Any]:
    case_root = RUNS_ROOT / case_id
    case_root.mkdir(parents=True, exist_ok=True)
    output_path = (case_root / "out.mkv").resolve()
    work_root = (case_root / "work").resolve()
    stale = seed_stale_file(work_root, output_path)

    info = inspect_info(sources.dv_hdr10plus_source, tools)
    workflow = build_encode_workflow(tools)
    logs = workflow_logs(workflow)
    started = time.monotonic()

    cfg = EncodeConfig(
        source=sources.dv_hdr10plus_source.resolve(),
        output=output_path,
        video=VideoEncodeSettings(codec="copy", quality_mode=QualityMode.CRF, crf=18, preset="ultrafast"),
        audio_tracks=audio_settings_from_info(info, [(1, "copy", None), (3, "copy", None)]),
        copy_subtitles=False,
        subtitle_tracks=[
            (sources.dv_hdr10plus_source.resolve(), 4),
            (sources.dv_hdr10plus_source.resolve(), 8),
        ],
        keep_chapters=False,
        tag_overrides={"TEST_CASE": case_id},
        track_meta_edits=make_track_meta_edits(
            audio_count=2,
            subtitle_count=2,
            audios=[
                {"language": "fr-FR", "title": "VF2"},
                {"language": "en-US", "title": "VO Atmos"},
            ],
            subtitles=[
                {"language": "fr-FR", "title": "FR Forced", "flag_default": True, "flag_forced": True},
                {"language": "en-US", "title": "SDH"},
            ],
        ),
        file_title="Encode Copy DV HDR10+",
        copy_dv=True,
        copy_hdr10plus=True,
        duration_s=info.duration_s,
        work_dir=work_root,
    )

    try:
        state = wait_task(workflow.run(cfg), CASE_TIMEOUT_S)
        out_info = inspect_info(output_path, tools)
        warnings = [entry["message"] for entry in logs if entry["level"] == "WARN"]
        functional = {
            "output_exists": output_path.exists(),
            "audio_count_2": len(out_info.audio_tracks) == 2,
            "subtitle_count_2": len(out_info.subtitle_tracks) == 2,
            "title_written": file_title(out_info) == "Encode Copy DV HDR10+",
            "dovi_preserved": has_dovi(output_path, tools),
            "hdr10plus_preserved": has_hdr10plus(output_path, tools),
        }
        cleanup = {
            "stale_file_removed": not stale.exists(),
        }
        notes: list[str] = []
        if any("HDR10+" in warning for warning in warnings):
            notes.append("Le workflow a signalé un faux négatif HDR10+ côté détection ffprobe, mais le passthrough a conservé les métadonnées.")
        return finalize_case(
            case_id=case_id,
            workflow_name="encode",
            branch="copy_passthrough_dv_hdr10plus_requested",
            source=sources.dv_hdr10plus_source,
            case_root=case_root,
            output_path=output_path,
            work_root=work_root,
            started=started,
            logs=logs,
            signal_state=state,
            info=out_info,
            functional_checks=functional,
            cleanup_checks=cleanup,
            notes=notes,
            tools=tools,
        )
    except Exception as exc:
        return finalize_case(
            case_id=case_id,
            workflow_name="encode",
            branch="copy_passthrough_dv_hdr10plus_requested",
            source=sources.dv_hdr10plus_source,
            case_root=case_root,
            output_path=output_path,
            work_root=work_root,
            started=started,
            logs=logs,
            signal_state=None,
            info=None,
            functional_checks={},
            cleanup_checks={},
            notes=[],
            tools=tools,
            exception=exc,
        )


def dv_encode_config(
    source: Path,
    output: Path,
    info: FileInfo,
    *,
    copy_dv: bool,
    copy_hdr10plus: bool,
    work_dir: Path,
    file_title: str,
    test_case: str,
    mux_backend: str = "ffmpeg",
) -> EncodeConfig:
    return EncodeConfig(
        source=source.resolve(),
        output=output.resolve(),
        video=VideoEncodeSettings(codec="libx265", quality_mode=QualityMode.CRF, crf=34, preset="ultrafast"),
        audio_tracks=audio_settings_from_info(info, [(3, "copy", None)]),
        copy_subtitles=False,
        subtitle_tracks=[(source.resolve(), 8)],
        keep_chapters=False,
        tag_overrides={"TEST_CASE": test_case},
        track_meta_edits=make_track_meta_edits(
            audio_count=1,
            subtitle_count=1,
            audios=[{"language": "en-US", "title": "VO Atmos"}],
            subtitles=[{"language": "en-US", "title": "SDH"}],
        ),
        file_title=file_title,
        copy_dv=copy_dv,
        copy_hdr10plus=copy_hdr10plus,
        duration_s=info.duration_s,
        work_dir=work_dir.resolve(),
        mux_backend=mux_backend,
    )


def run_encode_case_dv_inject(
    case_id: str,
    sources: PreparedSources,
    tools: ToolPaths,
    *,
    request_hdr10plus: bool,
    backend: str = "ffmpeg",
) -> dict[str, Any]:
    case_root = RUNS_ROOT / case_id
    case_root.mkdir(parents=True, exist_ok=True)
    output_path = (case_root / "out.mkv").resolve()
    work_root = (case_root / "work").resolve()
    stale = seed_stale_file(work_root, output_path)

    info = inspect_info(sources.dv_hdr10plus_source, tools)
    workflow = build_encode_workflow(tools)
    logs = workflow_logs(workflow)
    started = time.monotonic()
    cfg = dv_encode_config(
        sources.dv_hdr10plus_source,
        output_path,
        info,
        copy_dv=True,
        copy_hdr10plus=request_hdr10plus,
        work_dir=work_root,
        file_title="Encode DV Inject" if not request_hdr10plus else "Encode DV+HDR10+ Requested",
        test_case=case_id,
        mux_backend=backend,
    )

    try:
        state = wait_task(workflow.run(cfg), CASE_TIMEOUT_S)
        out_info = inspect_info(output_path, tools)
        work_state = process_dir_state(work_root, output_path)
        dovi_ok = has_dovi(output_path, tools)
        hdr10_ok = has_hdr10plus(output_path, tools)
        functional = {
            "output_exists": output_path.exists(),
            "dovi_preserved": dovi_ok,
            "hdr10plus_preserved": hdr10_ok if request_hdr10plus else not hdr10_ok,
            "audio_count_1": len(out_info.audio_tracks) == 1,
            "subtitle_count_1": len(out_info.subtitle_tracks) == 1,
            "video_language_preserved": (
                stream_languages(out_info.video_tracks)[:1]
                == stream_languages(info.video_tracks)[:1]
            ),
        }
        cleanup = {
            "stale_file_removed": not stale.exists(),
            "inject_temp_dirs_cleaned": not any("Muxiveo_encode_" in part for part in work_state["payload_files"]),
        }
        notes: list[str] = []
        if request_hdr10plus and not hdr10_ok:
            notes.append("HDR10+ demandé sur une source qui le contient, mais absent en sortie.")
        if request_hdr10plus:
            if any("HDR10+" in entry["message"] and "ignorée" in entry["message"] for entry in logs):
                notes.append("La détection ffprobe a désactivé HDR10+ avant le pipeline d'injection.")
        return finalize_case(
            case_id=case_id,
            workflow_name="encode",
            branch="reencode_dv_inject" if not request_hdr10plus else "reencode_dv_hdr10plus_requested",
            source=sources.dv_hdr10plus_source,
            case_root=case_root,
            output_path=output_path,
            work_root=work_root,
            started=started,
            logs=logs,
            signal_state=state,
            info=out_info,
            functional_checks=functional,
            cleanup_checks=cleanup,
            notes=notes,
            tools=tools,
            backend=backend,
        )
    except Exception as exc:
        return finalize_case(
            case_id=case_id,
            workflow_name="encode",
            branch="reencode_dv_inject" if not request_hdr10plus else "reencode_dv_hdr10plus_requested",
            source=sources.dv_hdr10plus_source,
            case_root=case_root,
            output_path=output_path,
            work_root=work_root,
            started=started,
            logs=logs,
            signal_state=None,
            info=None,
            functional_checks={},
            cleanup_checks={},
            notes=[],
            tools=tools,
            exception=exc,
            backend=backend,
        )


def build_summary_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Workflow Integration Summary",
        "",
        f"- Generated: `{report.get('updated_at', '')}`",
        f"- Cases: `{len(report.get('cases', []))}`",
        f"- Pass: `{report.get('summary', {}).get('pass', 0)}`",
        f"- Warn: `{report.get('summary', {}).get('warn', 0)}`",
        f"- Fail: `{report.get('summary', {}).get('fail', 0)}`",
        f"- Skipped: `{report.get('summary', {}).get('skipped', 0)}`",
        "",
        "| Case | Workflow | Backend | Branch | Status | Functional | Cleanup | Oracles | Key Notes |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for case in report.get("cases", []):
        if case.get("status") == "skipped":
            functional = "N/A"
            cleanup = "N/A"
        else:
            functional = "OK" if (not case.get("error") and all(case.get("functional_checks", {}).values())) else "KO"
            cleanup = "OK" if (not case.get("error") and all(case.get("cleanup_checks", {}).values())) else "KO"
        oracles = "OK" if all(case.get("output_checks", {}).values()) else "KO"
        notes = "; ".join(case.get("notes", [])[:2])
        lines.append(
            "| "
            + " | ".join([
                case["id"],
                case["workflow"],
                case.get("backend", ""),
                case["branch"],
                case["status"],
                functional,
                cleanup,
                oracles,
                notes.replace("|", "/"),
            ])
            + " |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_runtime(args)
    ensure_app()
    shutil.rmtree(RUNS_ROOT, ignore_errors=True)
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)

    tools = create_tool_wrappers(args.distrobox)
    sources = prepare_corpus(tools)

    report: dict[str, Any] = {
        "created_at": iso_now(),
        "updated_at": iso_now(),
        "repo_root": str(REPO_ROOT),
        "artifact_root": str(ARTIFACT_ROOT),
        "host_sources": {
            "sdr_sample": str(HOST_SDR_SAMPLE),
            "dv_hdr10plus": str(HOST_DV_HDR10P),
        },
        "sample": {"start_s": SAMPLE_START_S, "duration_s": SAMPLE_DURATION_S},
        "prepared_sources": {
            "sdr_attach_source": str(sources.sdr_attach_source),
            "sdr_meta_source": str(sources.sdr_meta_source),
            "dv_hdr10plus_source": str(sources.dv_hdr10plus_source),
        },
        "tools": tool_versions(tools),
        "cases": [],
        "summary": {"pass": 0, "warn": 0, "fail": 0, "skipped": 0},
    }
    write_json_atomic(REPORT_PATH, report)

    cases: list[Callable[[], dict[str, Any]]] = [
        lambda: run_remux_case_attachments("R1_ffmpeg_attach", sources, tools, backend="ffmpeg"),
        lambda: run_remux_case_override("R2_ffmpeg_override", sources, tools, backend="ffmpeg"),
        lambda: run_remux_case_cleanup("R3_ffmpeg_cleanup", sources, tools, backend="ffmpeg"),
        lambda: run_remux_case_attachments("R4_native_attach", sources, tools, backend="native"),
        lambda: run_remux_case_override("R5_native_override", sources, tools, backend="native"),
        lambda: run_remux_case_cleanup("R6_native_cleanup", sources, tools, backend="native"),
        lambda: run_remux_case_attachments("R7_auto_attach", sources, tools, backend="auto"),
        lambda: run_remux_case_override("R8_auto_override", sources, tools, backend="auto"),
        lambda: run_remux_case_cleanup("R9_auto_cleanup", sources, tools, backend="auto"),
        lambda: run_encode_case_copy_attachments("E1_encode_copy_attach", sources, tools),
        lambda: run_encode_case_single_pass("E2_encode_single", sources, tools),
        lambda: run_encode_case_two_pass("E3_encode_two_pass", sources, tools),
        lambda: run_encode_case_copy_dv_hdr10p("E4_encode_copy_dv_hdr10p", sources, tools),
        lambda: run_encode_case_dv_inject("E5_encode_dv_inject", sources, tools, request_hdr10plus=False),
        lambda: run_encode_case_dv_inject("E6_encode_dv_hdr10p_requested", sources, tools, request_hdr10plus=True),
        lambda: run_encode_case_dv_inject(
            "E7_native_dv_inject", sources, tools,
            request_hdr10plus=False, backend="native",
        ),
        lambda: run_encode_case_dv_inject(
            "E8_native_dv_hdr10p", sources, tools,
            request_hdr10plus=True, backend="native",
        ),
        lambda: run_encode_case_dv_inject(
            "E9_auto_dv_inject", sources, tools,
            request_hdr10plus=False, backend="auto",
        ),
        lambda: run_encode_case_dv_inject(
            "E10_auto_dv_hdr10p", sources, tools,
            request_hdr10plus=True, backend="auto",
        ),
    ]

    for runner in cases:
        result = runner()
        if not result.get("backend"):
            result["backend"] = "ffmpeg"
        result = append_output_checks(result, tools)
        append_case_result(report, result)

    final_report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    write_text(SUMMARY_PATH, build_summary_markdown(final_report))

    print(f"JSON report: {REPORT_PATH}")
    print(f"Summary MD:  {SUMMARY_PATH}")
    print(json.dumps(final_report["summary"], ensure_ascii=False))
    return 1 if final_report["summary"].get("fail", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
