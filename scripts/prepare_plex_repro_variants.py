#!/usr/bin/env python3
"""Build minimal Dolby Vision / HDR reproduction variants for Plex testing."""

from __future__ import annotations

# Imports applicatifs placés après l'ajout explicite de la racine du dépôt.
# ruff: noqa: E402

import argparse
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
class VariantSpec:
    name: str
    description: str
    keep_dovi: bool
    keep_hdr10plus: bool


VARIANTS: tuple[VariantSpec, ...] = (
    VariantSpec(
        name="dv_hdr10",
        description="Keep Dolby Vision and static HDR10, remove HDR10+",
        keep_dovi=True,
        keep_hdr10plus=False,
    ),
    VariantSpec(
        name="hdr10plus_hdr10",
        description="Keep HDR10+ and static HDR10, remove Dolby Vision",
        keep_dovi=False,
        keep_hdr10plus=True,
    ),
    VariantSpec(
        name="hdr10_only",
        description="Keep only static HDR10, remove Dolby Vision and HDR10+",
        keep_dovi=False,
        keep_hdr10plus=False,
    ),
)


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


def build_extract_clip_cmd(
    ffmpeg_bin: str,
    *,
    source: Path,
    dest: Path,
    clip_start: str,
    clip_duration: str,
) -> list[str]:
    return [
        ffmpeg_bin,
        "-hide_banner",
        "-y",
        "-ss",
        clip_start,
        "-t",
        clip_duration,
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-c:v",
        "copy",
        "-map",
        "0:a?",
        "-c:a",
        "copy",
        "-map_metadata",
        "0",
        "-map_chapters",
        "-1",
        "-sn",
        "-dn",
        "-avoid_negative_ts",
        "make_zero",
        "-max_interleave_delta",
        "0",
        str(dest),
    ]


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
        "-map_metadata",
        "1",
        "-map_chapters",
        "-1",
        "-sn",
        "-dn",
        "-max_interleave_delta",
        "0",
        str(output_mkv),
    ]


def build_variant_transform_cmd(
    runner: ToolRunner,
    *,
    source_hevc: Path,
    spec: VariantSpec,
    dest_hevc: Path,
) -> list[str]:
    if spec.keep_dovi and not spec.keep_hdr10plus:
        return [
            runner.hdr10plus_tool_bin,
            "remove",
            "-i",
            str(source_hevc),
            "-o",
            str(dest_hevc),
        ]
    if not spec.keep_dovi and spec.keep_hdr10plus:
        return [
            runner.dovi_tool_bin,
            "remove",
            "-i",
            str(source_hevc),
            "-o",
            str(dest_hevc),
        ]
    if not spec.keep_dovi and not spec.keep_hdr10plus:
        return [
            runner.dovi_tool_bin,
            "--drop-hdr10plus",
            "remove",
            "-i",
            str(source_hevc),
            "-o",
            str(dest_hevc),
        ]
    raise ValueError(f"Unsupported variant: {spec}")


def patch_dovi_mapping(
    runner: ToolRunner,
    *,
    variant_mkv: Path,
    variant_hevc: Path,
    temp_dir: Path | None = None,
) -> None:
    temp_root = str(temp_dir) if temp_dir is not None else None
    with tempfile.TemporaryDirectory(prefix="plex_repro_rpu_", dir=temp_root) as tmpdir:
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
            raise ToolError("Unable to build Dolby Vision configuration record from the variant RPU.")
        MatroskaDoviBlockAdditionEditor().patch(variant_mkv, record=record)


def build_output_name(source: Path, spec: VariantSpec) -> str:
    return f"{source.stem}.{spec.name}.mkv"


def build_clip_output_name(
    source: Path,
    spec: VariantSpec,
    *,
    clip_start: str | None,
    clip_duration: str | None,
) -> str:
    if not clip_duration:
        return build_output_name(source, spec)
    start_label = (clip_start or "0").replace(":", "-")
    duration_label = clip_duration.replace(":", "-")
    return f"{source.stem}.clip_{start_label}_{duration_label}.{spec.name}.mkv"


def prepare_working_source(
    runner: ToolRunner,
    *,
    source: Path,
    work_dir: Path,
    clip_start: str | None,
    clip_duration: str | None,
) -> Path:
    if not clip_start or not clip_duration:
        return source
    clipped_source = work_dir / "source_clip.mkv"
    run_checked(
        build_extract_clip_cmd(
            runner.ffmpeg_bin,
            source=source,
            dest=clipped_source,
            clip_start=clip_start,
            clip_duration=clip_duration,
        )
    )
    return clipped_source


def build_variants(
    runner: ToolRunner,
    *,
    source: Path,
    output_dir: Path,
    temp_dir: Path | None = None,
    clip_start: str | None = None,
    clip_duration: str | None = None,
) -> list[tuple[VariantSpec, Path]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    if temp_dir is not None:
        temp_dir.mkdir(parents=True, exist_ok=True)

    built: list[tuple[VariantSpec, Path]] = []
    temp_root = str(temp_dir) if temp_dir is not None else None
    with tempfile.TemporaryDirectory(prefix="plex_repro_variants_", dir=temp_root) as tmpdir:
        tmp = Path(tmpdir)
        working_source = prepare_working_source(
            runner,
            source=source,
            work_dir=tmp,
            clip_start=clip_start,
            clip_duration=clip_duration,
        )
        framerate = ffprobe_framerate(runner.ffprobe_bin, working_source)

        source_hevc = tmp / "source.hevc"
        run_checked(build_extract_annexb_cmd(runner.ffmpeg_bin, source=working_source, dest=source_hevc))

        for spec in VARIANTS:
            variant_hevc = tmp / f"{spec.name}.hevc"
            wrapped_variant_mkv = tmp / f"{spec.name}.wrapped.mkv"
            variant_mkv = output_dir / build_clip_output_name(
                source,
                spec,
                clip_start=clip_start,
                clip_duration=clip_duration,
            )

            run_checked(
                build_variant_transform_cmd(
                    runner,
                    source_hevc=source_hevc,
                    spec=spec,
                    dest_hevc=variant_hevc,
                )
            )
            run_checked(
                build_wrap_hevc_cmd(
                    runner.ffmpeg_bin,
                    variant_hevc=variant_hevc,
                    wrapped_video_mkv=wrapped_variant_mkv,
                    framerate=framerate,
                )
            )
            run_checked(
                build_remux_cmd(
                    runner.ffmpeg_bin,
                    wrapped_video_mkv=wrapped_variant_mkv,
                    source_mkv=working_source,
                    output_mkv=variant_mkv,
                )
            )
            if spec.keep_dovi:
                patch_dovi_mapping(
                    runner,
                    variant_mkv=variant_mkv,
                    variant_hevc=variant_hevc,
                    temp_dir=temp_dir,
                )
            built.append((spec, variant_mkv))

    return built


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare HDR/DV reproduction variants for Plex testing")
    parser.add_argument("input", type=Path, help="Source MKV to transform")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.cwd() / "plex_repro_variants",
        help="Output directory for generated MKV variants",
    )
    parser.add_argument("--ffprobe-bin", default="ffprobe")
    parser.add_argument("--ffmpeg-bin", default="ffmpeg")
    parser.add_argument("--dovi-tool-bin", default="dovi_tool")
    parser.add_argument("--hdr10plus-tool-bin", default="hdr10plus_tool")
    parser.add_argument(
        "--temp-dir",
        type=Path,
        default=None,
        help="Directory to use for temporary work files instead of the system temp dir",
    )
    parser.add_argument(
        "--clip-start",
        default=None,
        help="Optional clip start time, e.g. 00:30:00 or 1800",
    )
    parser.add_argument(
        "--clip-duration",
        default=None,
        help="Optional clip duration, e.g. 120 or 00:02:00",
    )
    parser.add_argument(
        "--clean-output-dir",
        action="store_true",
        help="Remove the output directory before generating new variants",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.input.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    temp_dir = args.temp_dir.expanduser().resolve() if args.temp_dir is not None else None

    if not source.is_file():
        print(f"Input file not found: {source}")
        return 2
    if bool(args.clip_start) != bool(args.clip_duration):
        print("--clip-start and --clip-duration must be provided together.")
        return 2
    if args.clean_output_dir and output_dir.exists():
        shutil.rmtree(output_dir)

    runner = ToolRunner(
        ffprobe_bin=args.ffprobe_bin,
        ffmpeg_bin=args.ffmpeg_bin,
        dovi_tool_bin=args.dovi_tool_bin,
        hdr10plus_tool_bin=args.hdr10plus_tool_bin,
    )

    try:
        built = build_variants(
            runner,
            source=source,
            output_dir=output_dir,
            temp_dir=temp_dir,
            clip_start=args.clip_start,
            clip_duration=args.clip_duration,
        )
    except ToolError as exc:
        print(str(exc))
        return 1

    print(f"Source: {source}")
    print(f"Output directory: {output_dir}")
    print()
    for spec, path in built:
        print(f"- {spec.name}: {path}")
        print(f"  {spec.description}")
    print()
    print("Suggested test order:")
    print("1. dv_hdr10")
    print("2. hdr10_only")
    print("3. hdr10plus_hdr10")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
