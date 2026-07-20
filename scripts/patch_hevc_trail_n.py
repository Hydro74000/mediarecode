#!/usr/bin/env python3
"""Patch HEVC TRAIL_N slices to TRAIL_R inside an MKV for decoder testing."""

from __future__ import annotations

# Imports applicatifs placés après l'ajout explicite de la racine du dépôt.
# ruff: noqa: E402

import argparse
import json
import mmap
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit_mkv import ToolError, ToolRunner, _extract_dovi_rpu_with_fallback
from core.workflows.encode.runtime.metadata_inject import _build_dovi_record_from_rpu
from core.matroska.editors.dovi import MatroskaDoviBlockAdditionEditor


@dataclass(frozen=True)
class PatchStats:
    trail_n_to_r: int
    total_nals: int


@dataclass(frozen=True)
class TrailCounts:
    trail_n: int
    trail_r: int
    total_nals: int


def run_checked(args: list[str]) -> str:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ToolError(f"Unable to run {' '.join(args)}: {exc}") from exc
    if result.returncode != 0:
        raise ToolError(
            f"Command failed with rc={result.returncode}: {' '.join(args)}\n"
            f"{(result.stderr or '').strip()}"
        )
    return result.stdout


def ffprobe_framerate(ffprobe_bin: str, source: Path) -> str:
    raw = run_checked(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=r_frame_rate,avg_frame_rate",
            "-of",
            "default=nw=1:nk=1",
            str(source),
        ]
    ).splitlines()
    for value in raw:
        value = value.strip()
        if value and value != "0/0":
            return value
    return "24000/1001"


def ffprobe_has_dovi(ffprobe_bin: str, source: Path) -> bool:
    payload = json.loads(
        run_checked(
            [
                ffprobe_bin,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                str(source),
            ]
        )
        or "{}"
    )
    for stream in payload.get("streams", []):
        if stream.get("codec_type") != "video" or stream.get("codec_name") != "hevc":
            continue
        for side_data in stream.get("side_data_list", []):
            if side_data.get("side_data_type") == "DOVI configuration record":
                return True
    return False


def patch_annexb_trail_n_to_trail_r(source: bytes) -> tuple[bytes, PatchStats]:
    out = bytearray()
    total_nals = 0
    trail_n_to_r = 0
    i = 0
    while i < len(source):
        start_len = 0
        if source[i:i + 4] == b"\x00\x00\x00\x01":
            start_len = 4
        elif source[i:i + 3] == b"\x00\x00\x01":
            start_len = 3
        if start_len == 0:
            out.append(source[i])
            i += 1
            continue
        out.extend(source[i:i + start_len])
        i += start_len
        if i >= len(source):
            break
        total_nals += 1
        nal_type = (source[i] >> 1) & 0x3F
        if nal_type == 0:
            out.append((source[i] & 0x81) | (1 << 1))
            trail_n_to_r += 1
            i += 1
            continue
        out.append(source[i])
        i += 1
    return bytes(out), PatchStats(trail_n_to_r=trail_n_to_r, total_nals=total_nals)


def patch_annexb_file(source: Path, dest: Path) -> PatchStats:
    total_nals = 0
    trail_n_to_r = 0

    with source.open("rb") as src_fh, dest.open("wb") as dst_fh:
        with mmap.mmap(src_fh.fileno(), length=0, access=mmap.ACCESS_READ) as mm:
            n = len(mm)
            pos = 0
            write_from = 0
            while pos < n:
                start_len = 0
                if pos + 4 <= n and mm[pos:pos + 4] == b"\x00\x00\x00\x01":
                    start_len = 4
                elif pos + 3 <= n and mm[pos:pos + 3] == b"\x00\x00\x01":
                    start_len = 3
                if start_len == 0:
                    pos += 1
                    continue

                header_idx = pos + start_len
                if header_idx >= n:
                    break

                if write_from < header_idx:
                    dst_fh.write(mm[write_from:header_idx])

                first_byte = mm[header_idx]
                nal_type = (first_byte >> 1) & 0x3F
                total_nals += 1
                if nal_type == 0:
                    dst_fh.write(bytes([(first_byte & 0x81) | (1 << 1)]))
                    trail_n_to_r += 1
                else:
                    dst_fh.write(bytes([first_byte]))

                write_from = header_idx + 1
                pos = write_from

            if write_from < n:
                dst_fh.write(mm[write_from:n])

    return PatchStats(trail_n_to_r=trail_n_to_r, total_nals=total_nals)


def count_annexb_trail_types(source: Path) -> TrailCounts:
    total_nals = 0
    trail_n = 0
    trail_r = 0
    with source.open("rb") as src_fh:
        with mmap.mmap(src_fh.fileno(), length=0, access=mmap.ACCESS_READ) as mm:
            n = len(mm)
            pos = 0
            while pos < n:
                start_len = 0
                if pos + 4 <= n and mm[pos:pos + 4] == b"\x00\x00\x00\x01":
                    start_len = 4
                elif pos + 3 <= n and mm[pos:pos + 3] == b"\x00\x00\x01":
                    start_len = 3
                if start_len == 0:
                    pos += 1
                    continue
                header_idx = pos + start_len
                if header_idx >= n:
                    break
                nal_type = (mm[header_idx] >> 1) & 0x3F
                total_nals += 1
                if nal_type == 0:
                    trail_n += 1
                elif nal_type == 1:
                    trail_r += 1
                pos = header_idx + 1
    return TrailCounts(trail_n=trail_n, trail_r=trail_r, total_nals=total_nals)


def _find_start_code(data: bytearray, offset: int) -> tuple[int, int] | None:
    pos = data.find(b"\x00\x00\x01", offset)
    if pos == -1:
        return None
    if pos > 0 and data[pos - 1] == 0x00:
        return pos - 1, 4
    return pos, 3


def count_trail_types_via_ffmpeg_pipe(ffmpeg_bin: str, source: Path) -> TrailCounts:
    args = [
        ffmpeg_bin,
        "-v",
        "error",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-c",
        "copy",
        "-bsf:v",
        "hevc_mp4toannexb",
        "-f",
        "hevc",
        "-",
    ]
    try:
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise ToolError(f"Unable to run {' '.join(args)}: {exc}") from exc

    assert process.stdout is not None
    assert process.stderr is not None

    trail_n = 0
    trail_r = 0
    total_nals = 0
    buffer = bytearray()

    def consume_nal(nal_payload: bytes) -> None:
        nonlocal trail_n, trail_r, total_nals
        if len(nal_payload) < 2:
            return
        nal_type = (nal_payload[0] >> 1) & 0x3F
        total_nals += 1
        if nal_type == 0:
            trail_n += 1
        elif nal_type == 1:
            trail_r += 1

    while True:
        chunk = process.stdout.read(8 * 1024 * 1024)
        if not chunk:
            break
        buffer.extend(chunk)
        while True:
            first = _find_start_code(buffer, 0)
            if first is None:
                if len(buffer) > 4:
                    del buffer[:-4]
                break
            second = _find_start_code(buffer, first[0] + first[1])
            if second is None:
                if first[0] > 0:
                    del buffer[:first[0]]
                break
            payload_start = first[0] + first[1]
            nal_payload = bytes(buffer[payload_start:second[0]])
            consume_nal(nal_payload)
            del buffer[:second[0]]

    if buffer:
        first = _find_start_code(buffer, 0)
        if first is not None:
            payload_start = first[0] + first[1]
            nal_payload = bytes(buffer[payload_start:])
            consume_nal(nal_payload)

    stderr = process.stderr.read().decode("utf-8", errors="replace").strip()
    rc = process.wait()
    if rc != 0:
        raise ToolError(f"Command failed with rc={rc}: {' '.join(args)}\n{stderr}")
    return TrailCounts(trail_n=trail_n, trail_r=trail_r, total_nals=total_nals)


def build_extract_annexb_cmd(ffmpeg_bin: str, *, source: Path, dest: Path) -> list[str]:
    return [
        ffmpeg_bin,
        "-hide_banner",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-c",
        "copy",
        "-bsf:v",
        "hevc_mp4toannexb",
        "-f",
        "hevc",
        str(dest),
    ]


def build_wrap_hevc_cmd(
    ffmpeg_bin: str,
    *,
    variant_hevc: Path,
    wrapped_video_mkv: Path,
    framerate: str,
) -> list[str]:
    return [
        ffmpeg_bin,
        "-hide_banner",
        "-y",
        "-f",
        "hevc",
        "-framerate",
        framerate,
        "-fflags",
        "+genpts",
        "-i",
        str(variant_hevc),
        "-map",
        "0:v:0",
        "-c:v",
        "copy",
        "-bsf:v",
        f"setts=pts=N/({framerate}*TB)",
        str(wrapped_video_mkv),
    ]


def build_remux_cmd(
    ffmpeg_bin: str,
    *,
    wrapped_video_mkv: Path,
    source_mkv: Path,
    output_mkv: Path,
) -> list[str]:
    return [
        ffmpeg_bin,
        "-hide_banner",
        "-y",
        "-i",
        str(wrapped_video_mkv),
        "-i",
        str(source_mkv),
        "-map",
        "0:v:0",
        "-c:v",
        "copy",
        "-map",
        "1:a?",
        "-c:a",
        "copy",
        "-map",
        "1:s?",
        "-c:s",
        "copy",
        "-map_metadata",
        "1",
        "-map_chapters",
        "1",
        "-dn",
        "-max_interleave_delta",
        "0",
        str(output_mkv),
    ]


def patch_dovi_mapping(
    runner: ToolRunner,
    *,
    variant_mkv: Path,
    variant_hevc: Path,
    temp_dir: Path | None = None,
) -> None:
    temp_root = str(temp_dir) if temp_dir is not None else None
    with tempfile.TemporaryDirectory(prefix="patch_trailn_rpu_", dir=temp_root) as tmpdir:
        rpu_path = Path(tmpdir) / "variant.rpu.bin"
        _extract_dovi_rpu_with_fallback(
            runner,
            input_path=variant_hevc,
            output_path=rpu_path,
        )
        record = _build_dovi_record_from_rpu(
            rpu_bin=rpu_path,
            dovi_tool_bin=runner.dovi_tool_bin,
        )
        if record is None:
            raise ToolError("Unable to build Dolby Vision configuration record from the patched RPU stream.")
        MatroskaDoviBlockAdditionEditor().patch(variant_mkv, record=record)


def patch_mkv(
    runner: ToolRunner,
    *,
    source: Path,
    output: Path,
    temp_dir: Path | None = None,
) -> PatchStats:
    if temp_dir is not None:
        temp_dir.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_root = str(temp_dir) if temp_dir is not None else None
    with tempfile.TemporaryDirectory(prefix="patch_trailn_", dir=temp_root) as tmpdir:
        tmp = Path(tmpdir)
        print("1/6 Probe source", flush=True)
        framerate = ffprobe_framerate(runner.ffprobe_bin, source)
        has_dovi = ffprobe_has_dovi(runner.ffprobe_bin, source)
        source_hevc = tmp / "source.hevc"
        patched_hevc = tmp / "patched.hevc"
        wrapped_video_mkv = tmp / "patched_video.mkv"
        print("2/6 Extract source HEVC", flush=True)
        run_checked(build_extract_annexb_cmd(runner.ffmpeg_bin, source=source, dest=source_hevc))
        print("3/6 Patch TRAIL_N -> TRAIL_R (streaming)", flush=True)
        stats = patch_annexb_file(source_hevc, patched_hevc)
        print("4/6 Wrap patched HEVC", flush=True)
        run_checked(
            build_wrap_hevc_cmd(
                runner.ffmpeg_bin,
                variant_hevc=patched_hevc,
                wrapped_video_mkv=wrapped_video_mkv,
                framerate=framerate,
            )
        )
        print("5/6 Remux final MKV", flush=True)
        run_checked(
            build_remux_cmd(
                runner.ffmpeg_bin,
                wrapped_video_mkv=wrapped_video_mkv,
                source_mkv=source,
                output_mkv=output,
            )
        )
        if has_dovi:
            print("6/6 Patch Matroska Dolby Vision mapping", flush=True)
            patch_dovi_mapping(
                runner,
                variant_mkv=output,
                variant_hevc=patched_hevc,
                temp_dir=temp_dir,
            )
        return stats


def audit_input_trail_types(
    runner: ToolRunner,
    *,
    source: Path,
    temp_dir: Path | None = None,
) -> TrailCounts:
    suffix = source.suffix.lower()
    if suffix in {".hevc", ".h265"}:
        return count_annexb_trail_types(source)
    print("1/1 Scan HEVC stream via ffmpeg pipe", flush=True)
    return count_trail_types_via_ffmpeg_pipe(runner.ffmpeg_bin, source)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Patch HEVC TRAIL_N slices to TRAIL_R for MKV playback testing")
    parser.add_argument("input", type=Path, help="Source MKV")
    parser.add_argument("output", type=Path, nargs="?", help="Output MKV (not required with --audit)")
    parser.add_argument("--ffprobe-bin", default="ffprobe")
    parser.add_argument("--ffmpeg-bin", default="ffmpeg")
    parser.add_argument("--dovi-tool-bin", default="dovi_tool")
    parser.add_argument(
        "--temp-dir",
        type=Path,
        default=None,
        help="Directory to use for temporary work files instead of the system temp dir",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Remove the output file if it already exists",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Only audit TRAIL_N/TRAIL_R counts, do not generate output",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.input.expanduser().resolve()
    output = args.output.expanduser().resolve() if args.output is not None else None
    temp_dir = args.temp_dir.expanduser().resolve() if args.temp_dir is not None else None

    if not source.is_file():
        print(f"Input file not found: {source}")
        return 2
    if args.audit:
        runner = ToolRunner(
            ffprobe_bin=args.ffprobe_bin,
            ffmpeg_bin=args.ffmpeg_bin,
            dovi_tool_bin=args.dovi_tool_bin,
            hdr10plus_tool_bin="hdr10plus_tool",
        )
        try:
            counts = audit_input_trail_types(
                runner,
                source=source,
                temp_dir=temp_dir,
            )
        except ToolError as exc:
            print(str(exc))
            return 1
        print(f"Input: {source}")
        print(f"TRAIL_N: {counts.trail_n}")
        print(f"TRAIL_R: {counts.trail_r}")
        print(f"Total NAL units seen: {counts.total_nals}")
        return 0
    if output is None:
        print("Missing required output path (or use --audit).")
        return 2
    if output.exists():
        if not args.overwrite:
            print(f"Output already exists: {output}")
            return 2
        if output.is_dir():
            shutil.rmtree(output)
        else:
            output.unlink()

    runner = ToolRunner(
        ffprobe_bin=args.ffprobe_bin,
        ffmpeg_bin=args.ffmpeg_bin,
        dovi_tool_bin=args.dovi_tool_bin,
        hdr10plus_tool_bin="hdr10plus_tool",
    )
    try:
        stats = patch_mkv(
            runner,
            source=source,
            output=output,
            temp_dir=temp_dir,
        )
    except ToolError as exc:
        print(str(exc))
        return 1

    print(f"Source: {source}")
    print(f"Output: {output}")
    print(f"Patched TRAIL_N -> TRAIL_R: {stats.trail_n_to_r}")
    print(f"Total NAL units seen: {stats.total_nals}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
