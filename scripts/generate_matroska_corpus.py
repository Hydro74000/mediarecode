#!/usr/bin/env python3
"""Generate/verify the compact, redistributable Matroska test corpus.

All sources are synthetic and free. Normal tests only verify checked-in files
against MANIFEST.sha256 and never access the network.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "corpus" / "matroska"
MEDIA_FILES = ("avc_aac_srt_chapters.mkv", "hevc_flac_ass_hdr10.mkv", "av1_opus_webvtt.mkv")


def run_ffmpeg(arguments: list[str]) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", *arguments], check=True)


def generate() -> None:
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg est requis pour régénérer le corpus")
    CORPUS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="muxiveo-corpus-") as directory:
        work = Path(directory)
        srt = work / "captions.srt"
        srt.write_text(
            "1\n00:00:00,000 --> 00:00:00,250\nMuxiveo native\n\n"
            "2\n00:00:00,300 --> 00:00:00,550\nMatroska corpus\n", encoding="utf-8",
        )
        ass = work / "captions.ass"
        ass.write_text(
            "[Script Info]\nScriptType: v4.00+\nPlayResX: 96\nPlayResY: 54\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
            "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding\n"
            "Style: Default,MuxiveoSans,12,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
            "0,0,0,0,100,100,0,0,1,1,0,2,4,4,4,1\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
            "Effect, Text\nDialogue: 0,0:00:00.00,0:00:00.50,Default,,0,0,0,,HDR10\n",
            encoding="utf-8",
        )
        vtt = work / "captions.vtt"
        vtt.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:00.500\nAV1 + Opus\n", encoding="utf-8")
        metadata = work / "chapters.ffmeta"
        metadata.write_text(
            ";FFMETADATA1\ntitle=Corpus AVC\nartist=Muxiveo\n"
            "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=300\ntitle=Intro\n"
            "[CHAPTER]\nTIMEBASE=1/1000\nSTART=300\nEND=600\ntitle=Main\n", encoding="utf-8",
        )
        font = work / "MuxiveoSans.txt"
        font.write_text("Synthetic font attachment used by the Muxiveo corpus.\n", encoding="utf-8")

        run_ffmpeg([
            "-f", "lavfi", "-i", "testsrc2=size=96x54:rate=24000/1001:duration=0.6",
            "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=0.6",
            "-i", str(srt), "-f", "ffmetadata", "-i", str(metadata),
            "-map", "0:v:0", "-map", "1:a:0", "-map", "2:s:0", "-map_metadata", "3",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-g", "12",
            "-c:a", "aac", "-b:a", "96k", "-c:s", "srt",
            "-metadata:s:v:0", "language=eng", "-metadata:s:v:0", "title=AVC 23.976",
            "-metadata:s:a:0", "language=fra", "-metadata:s:a:0", "title=AAC stereo",
            "-metadata:s:s:0", "language=eng", "-metadata:s:s:0", "title=English SRT",
            "-disposition:s:0", "default+forced",
            "-attach", str(font), "-metadata:s:t:0", "mimetype=text/plain",
            "-metadata:s:t:0", "filename=MuxiveoSans.txt",
            "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
            "-y", str(CORPUS / MEDIA_FILES[0]),
        ])
        run_ffmpeg([
            "-f", "lavfi", "-i", "testsrc2=size=96x54:rate=25:duration=0.5",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.5",
            "-i", str(ass), "-map", "0:v:0", "-map", "1:a:0", "-map", "2:s:0",
            "-c:v", "libx265", "-preset", "ultrafast", "-x265-params",
            "log-level=error:repeat-headers=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc",
            "-pix_fmt", "yuv420p10le", "-color_primaries", "bt2020",
            "-color_trc", "smpte2084", "-colorspace", "bt2020nc", "-color_range", "tv",
            "-c:a", "flac", "-c:s", "ass", "-metadata", "title=Corpus HDR10",
            "-metadata:s:v:0", "title=HEVC Main10 HDR10", "-metadata:s:a:0", "language=jpn",
            "-metadata:s:s:0", "language=fra", "-metadata:s:s:0", "title=French ASS",
            "-attach", str(font), "-metadata:s:t:0", "mimetype=application/x-truetype-font",
            "-metadata:s:t:0", "filename=MuxiveoSans.txt",
            "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
            "-y", str(CORPUS / MEDIA_FILES[1]),
        ])
        run_ffmpeg([
            "-f", "lavfi", "-i", "testsrc2=size=64x36:rate=12:duration=0.5",
            "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=0.5",
            "-i", str(vtt), "-map", "0:v:0", "-map", "1:a:0", "-map", "2:s:0",
            "-c:v", "libaom-av1", "-cpu-used", "8", "-crf", "45", "-b:v", "0",
            "-c:a", "libopus", "-b:a", "64k", "-c:s", "webvtt",
            "-metadata", "title=Corpus AV1", "-metadata:s:v:0", "language=und",
            "-metadata:s:a:0", "language=deu", "-metadata:s:s:0", "language=deu",
            "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
            "-y", str(CORPUS / MEDIA_FILES[2]),
        ])
    write_manifest()


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def write_manifest() -> None:
    lines = [f"{digest(CORPUS / name)}  {name}" for name in MEDIA_FILES]
    (CORPUS / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="ascii")


def verify() -> None:
    expected = {}
    for line in (CORPUS / "MANIFEST.sha256").read_text(encoding="ascii").splitlines():
        checksum, name = line.split("  ", 1)
        expected[name] = checksum
    missing = set(MEDIA_FILES) - set(expected)
    if missing:
        raise SystemExit(f"Entrées absentes du manifeste : {sorted(missing)}")
    failures = [name for name in MEDIA_FILES if not (CORPUS / name).is_file() or digest(CORPUS / name) != expected[name]]
    if failures:
        raise SystemExit(f"Corpus absent ou altéré : {failures}")
    print(f"Corpus Matroska vérifié : {len(MEDIA_FILES)} fichiers")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generate", action="store_true", help="Régénérer les médias et le manifeste")
    args = parser.parse_args()
    generate() if args.generate else verify()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
