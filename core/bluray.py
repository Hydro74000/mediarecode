"""Blu-ray BDMV/MPLS helpers.

Muxiveo keeps the selected ``.mpls`` path as the logical source path.  When an
external FFmpeg-family tool needs to read it, these helpers translate that path
to the libbluray protocol form: ``-playlist N -i bluray:/disc/root``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


_MPLS_RE = re.compile(r"^(\d{5})\.mpls$", re.IGNORECASE)
_MPLS_TIMEBASE = 45_000.0


@dataclass(frozen=True)
class BluRaySegment:
    clip_id: str
    path: Path
    in_time_s: float
    out_time_s: float
    duration_s: float


@dataclass(frozen=True)
class BluRayStreamInfo:
    kind: str
    ordinal: int
    pid: int
    coding_type: int
    language: str = ""


@dataclass(frozen=True)
class BluRayTitle:
    playlist_id: int
    playlist_path: Path
    disc_root: Path
    duration_s: float
    segments: tuple[BluRaySegment, ...] = field(default_factory=tuple)
    streams: tuple[BluRayStreamInfo, ...] = field(default_factory=tuple)

    @property
    def size_bytes(self) -> int:
        total = 0
        seen: set[Path] = set()
        for segment in self.segments:
            if segment.path in seen:
                continue
            seen.add(segment.path)
            try:
                total += segment.path.stat().st_size
            except OSError:
                pass
        return total

    @property
    def label(self) -> str:
        return f"{self.playlist_id:05d}.mpls"

    @property
    def stream_by_pid(self) -> dict[int, BluRayStreamInfo]:
        return {stream.pid: stream for stream in self.streams}


def playlist_id_from_path(path: Path) -> int | None:
    match = _MPLS_RE.match(Path(path).name)
    if not match:
        return None
    return int(match.group(1))


def is_bluray_playlist(path: Path | str) -> bool:
    path = Path(path)
    return playlist_id_from_path(path) is not None and path.parent.name.upper() == "PLAYLIST"


def find_disc_root(path: Path | str) -> Path | None:
    path = Path(path)
    candidates: list[Path] = []
    if path.is_file():
        candidates.extend([path.parent.parent.parent, path.parent.parent, path.parent])
    else:
        candidates.extend([path, path.parent])

    for candidate in candidates:
        if not candidate:
            continue
        if (candidate / "BDMV" / "index.bdmv").is_file() or (candidate / "BDMV" / "BACKUP" / "index.bdmv").is_file():
            return candidate
        if candidate.name.upper() == "BDMV" and (
            (candidate / "index.bdmv").is_file()
            or (candidate / "BACKUP" / "index.bdmv").is_file()
        ):
            return candidate.parent
    return None


def playlist_path_for(path: Path | str, playlist_id: int | None = None) -> Path | None:
    path = Path(path)
    if is_bluray_playlist(path):
        return path
    disc_root = find_disc_root(path)
    if disc_root is None or playlist_id is None:
        return None
    candidate = disc_root / "BDMV" / "PLAYLIST" / f"{int(playlist_id):05d}.mpls"
    return candidate if candidate.is_file() else None


def _decode_ascii(value: bytes) -> str:
    return value.decode("ascii", errors="ignore")


def _parse_stream_pid(entry: bytes) -> int | None:
    if len(entry) < 4 or entry[1] != 0x01:
        return None
    return int.from_bytes(entry[2:4], "big")


def _parse_mpls_stn_table(item: bytes) -> tuple[BluRayStreamInfo, ...]:
    stn_start = 34
    if stn_start + 16 > len(item):
        return ()
    stn_length = int.from_bytes(item[stn_start:stn_start + 2], "big")
    if stn_length <= 0 or stn_start + 2 + stn_length > len(item):
        return ()

    counts = item[stn_start + 4:stn_start + 11]
    if len(counts) < 7:
        return ()
    stream_kinds = (
        "video",
        "audio",
        "subtitle",
        "interactive_graphics",
        "secondary_audio",
        "secondary_video",
        "pip_subtitle",
    )
    pos = stn_start + 16
    streams: list[BluRayStreamInfo] = []
    for kind, count in zip(stream_kinds, counts):
        for ordinal in range(int(count)):
            if pos >= len(item):
                return tuple(streams)
            entry_len = int(item[pos])
            entry = item[pos:pos + 1 + entry_len]
            pos += 1 + entry_len
            if pos >= len(item):
                return tuple(streams)
            attr_len = int(item[pos])
            attr = item[pos:pos + 1 + attr_len]
            pos += 1 + attr_len

            pid = _parse_stream_pid(entry)
            if pid is None or len(attr) < 2:
                continue
            coding_type = int(attr[1])
            language = ""
            if kind in {"audio", "secondary_audio"} and len(attr) >= 6:
                language = _decode_ascii(attr[3:6])
            elif kind in {"subtitle", "interactive_graphics", "pip_subtitle"} and len(attr) >= 5:
                language = _decode_ascii(attr[2:5])
            streams.append(
                BluRayStreamInfo(
                    kind=kind,
                    ordinal=ordinal,
                    pid=pid,
                    coding_type=coding_type,
                    language=language,
                )
            )
    return tuple(streams)


def _parse_mpls_playitems(
    playlist_path: Path,
    disc_root: Path,
) -> tuple[tuple[BluRaySegment, ...], tuple[BluRayStreamInfo, ...]]:
    data = playlist_path.read_bytes()
    if len(data) < 20 or data[:4] != b"MPLS":
        return (), ()
    playlist_start = int.from_bytes(data[8:12], "big")
    if playlist_start <= 0 or playlist_start + 10 > len(data):
        return (), ()
    item_count = int.from_bytes(data[playlist_start + 6:playlist_start + 8], "big")
    pos = playlist_start + 10
    segments: list[BluRaySegment] = []
    streams: tuple[BluRayStreamInfo, ...] = ()
    stream_dir = disc_root / "BDMV" / "STREAM"
    for _ in range(item_count):
        if pos + 2 > len(data):
            break
        item_len = int.from_bytes(data[pos:pos + 2], "big")
        item = data[pos:pos + 2 + item_len]
        pos += 2 + item_len
        if len(item) < 22:
            continue
        clip_id = _decode_ascii(item[2:7])
        codec_id = _decode_ascii(item[7:11])
        if not re.fullmatch(r"\d{5}", clip_id) or codec_id != "M2TS":
            continue
        if not streams:
            streams = _parse_mpls_stn_table(item)
        in_ticks = int.from_bytes(item[14:18], "big")
        out_ticks = int.from_bytes(item[18:22], "big")
        duration_ticks = max(0, out_ticks - in_ticks)
        segments.append(
            BluRaySegment(
                clip_id=clip_id,
                path=stream_dir / f"{clip_id}.m2ts",
                in_time_s=in_ticks / _MPLS_TIMEBASE,
                out_time_s=out_ticks / _MPLS_TIMEBASE,
                duration_s=duration_ticks / _MPLS_TIMEBASE,
            )
        )
    return tuple(segments), streams


def title_for_playlist(path: Path | str) -> BluRayTitle | None:
    playlist_path = Path(path)
    playlist_id = playlist_id_from_path(playlist_path)
    disc_root = find_disc_root(playlist_path)
    if playlist_id is None or disc_root is None or not playlist_path.is_file():
        return None
    segments, streams = _parse_mpls_playitems(playlist_path, disc_root)
    return BluRayTitle(
        playlist_id=playlist_id,
        playlist_path=playlist_path,
        disc_root=disc_root,
        duration_s=sum(segment.duration_s for segment in segments),
        segments=segments,
        streams=streams,
    )


def discover_titles(path: Path | str, *, min_duration_s: float = 0.0) -> list[BluRayTitle]:
    disc_root = find_disc_root(path)
    if disc_root is None:
        return []
    playlist_dir = disc_root / "BDMV" / "PLAYLIST"
    titles: list[BluRayTitle] = []
    for playlist_path in sorted(playlist_dir.glob("*.mpls")):
        title = title_for_playlist(playlist_path)
        if title is None or title.duration_s <= 0 or title.duration_s < min_duration_s:
            continue
        titles.append(title)
    return sorted(
        titles,
        key=lambda title: (-title.duration_s, -title.size_bytes, title.playlist_id),
    )


def ffmpeg_input_args(path: Path | str) -> list[str]:
    path = Path(path)
    title = title_for_playlist(path) if is_bluray_playlist(path) else None
    if title is None:
        return ["-i", str(path)]
    return ["-playlist", str(title.playlist_id), "-i", f"bluray:{title.disc_root}"]


def append_ffmpeg_input_args(cmd: list[str], path: Path | str, cli_path=lambda value: str(value)) -> None:
    title = title_for_playlist(path) if is_bluray_playlist(path) else None
    if title is None:
        cmd.extend(["-i", cli_path(path)])
        return
    cmd.extend(["-playlist", str(title.playlist_id), "-i", f"bluray:{cli_path(title.disc_root)}"])


def ffprobe_input_args(path: Path | str) -> list[str]:
    path = Path(path)
    title = title_for_playlist(path) if is_bluray_playlist(path) else None
    if title is None:
        return [str(path)]
    return ["-playlist", str(title.playlist_id), "-i", f"bluray:{title.disc_root}"]


def validate_bluray_source(path: Path | str) -> list[str]:
    path = Path(path)
    if not is_bluray_playlist(path):
        return []
    title = title_for_playlist(path)
    if title is None:
        return [f"Playlist Blu-ray invalide : {path}"]
    missing = [segment.path.name for segment in title.segments if not segment.path.is_file()]
    if missing:
        preview = ", ".join(missing[:5])
        if len(missing) > 5:
            preview += ", ..."
        return [f"Segments Blu-ray introuvables pour {path.name} : {preview}"]
    return []


__all__ = [
    "BluRaySegment",
    "BluRayStreamInfo",
    "BluRayTitle",
    "append_ffmpeg_input_args",
    "discover_titles",
    "ffmpeg_input_args",
    "ffprobe_input_args",
    "find_disc_root",
    "is_bluray_playlist",
    "playlist_id_from_path",
    "playlist_path_for",
    "title_for_playlist",
    "validate_bluray_source",
]
