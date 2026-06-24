"""
Patch in-place des timecodes vidéo Matroska depuis une source de référence.

Lorsqu'un HEVC brut est remuxé avec ffmpeg via ``-f hevc`` et des PTS
synthétiques, les streams avec B-frames perdent la correspondance entre
ordre packet (ordre de décodage) et PTS de présentation. Le résultat peut
être déclaré à 23.976 fps tout en donnant une cadence visuelle saccadée.

Ce module corrige le MKV déjà produit en remplaçant uniquement les deux
octets de timecode des ``SimpleBlock`` de la piste vidéo. Le payload HEVC,
les pistes audio/sous-titres et les métadonnées ne sont pas réécrits.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from core.workflows.matroska_dovi_block_addition import MatroskaDoviBlockAdditionEditor
from core.workflows.matroska_header_editor import (
    MatroskaSegmentInfoHeaderEditor,
    MatroskaSegmentInfoHeaderEditorOptions,
)
from core.workflows.matroska_timestamp_reader import MatroskaTimestampReader


_CLUSTER_ID = b"\x1f\x43\xb6\x75"
_INFO_ID = b"\x15\x49\xa9\x66"
_TIMESTAMP_SCALE_ID = b"\x2a\xd7\xb1"
_TIMESTAMP_ID = b"\xe7"
_SIMPLE_BLOCK_ID = b"\xa3"
_DEFAULT_TIMESTAMP_SCALE_NS = 1_000_000


@dataclass(frozen=True)
class MatroskaVideoTimecodePatchResult:
    patched_blocks: int
    source_pts: int
    first_pts_ms: int | None
    last_pts_ms: int | None


class MatroskaVideoTimecodePatcher:
    """Réapplique les PTS packet-order source aux SimpleBlock vidéo."""

    def __init__(self, *, ffprobe_bin: str = "ffprobe") -> None:
        self._timestamp_reader = MatroskaTimestampReader(ffprobe_bin=ffprobe_bin)
        self._base = MatroskaSegmentInfoHeaderEditor(
            options=MatroskaSegmentInfoHeaderEditorOptions(fallback_mode="raise"),
        )

    def patch(
        self,
        *,
        target_mkv: Path,
        source_for_timestamps: Path,
    ) -> MatroskaVideoTimecodePatchResult:
        if not target_mkv.is_file():
            raise ValueError(f"Fichier cible introuvable: {target_mkv}")
        if not source_for_timestamps.is_file():
            raise ValueError(f"Source timestamps introuvable: {source_for_timestamps}")

        pts_seq = self._timestamp_reader.read(
            source_for_timestamps,
            sort_by_pts=False,
        )
        video_track_number = self._find_video_track_number(target_mkv)
        pts_iter = iter(pts_seq.pts_ms)
        patched = 0
        first_pts = pts_seq.pts_ms[0] if pts_seq.pts_ms else None
        last_pts = max(pts_seq.pts_ms) if pts_seq.pts_ms else None

        with target_mkv.open("r+b") as fh:
            state = self._base._analyze_file(fh, parse_fast=False)
            timestamp_scale_ns = self._read_timestamp_scale_ns(fh, state)
            if timestamp_scale_ns != _DEFAULT_TIMESTAMP_SCALE_NS:
                raise RuntimeError(
                    "TimestampScale Matroska non supporté pour ce patch "
                    f"({timestamp_scale_ns} ns/tick, attendu "
                    f"{_DEFAULT_TIMESTAMP_SCALE_NS} ns/tick)."
                )
            clusters = [e for e in state.data if e.element_id == _CLUSTER_ID]
            for cluster in clusters:
                cluster_timestamp = self._read_cluster_timestamp(fh, cluster)
                cursor = cluster.payload_offset
                while cursor < cluster.end:
                    try:
                        child = self._base._read_ebml_element_from_file(
                            fh,
                            cursor,
                            state.file_size,
                        )
                    except ValueError:
                        break
                    if child.element_id == _SIMPLE_BLOCK_ID:
                        if self._patch_simple_block(
                            fh,
                            child.payload_offset,
                            video_track_number=video_track_number,
                            cluster_timestamp=cluster_timestamp,
                            pts_iter=pts_iter,
                        ):
                            patched += 1
                    if child.unknown_size:
                        break
                    cursor = child.end

        if patched != len(pts_seq.pts_ms):
            raise RuntimeError(
                f"Nombre de blocs vidéo patchés incohérent : {patched} "
                f"vs {len(pts_seq.pts_ms)} PTS source."
            )

        return MatroskaVideoTimecodePatchResult(
            patched_blocks=patched,
            source_pts=len(pts_seq.pts_ms),
            first_pts_ms=first_pts,
            last_pts_ms=last_pts,
        )

    def _find_video_track_number(self, target_mkv: Path) -> int:
        editor = MatroskaDoviBlockAdditionEditor()
        with target_mkv.open("rb") as fh:
            _, tracks_payload = editor._read_tracks_payload(fh)
        for entry in editor._parse_track_entries(tracks_payload):
            if entry.is_hevc:
                return int(entry.track_number)
        raise RuntimeError("Piste vidéo HEVC introuvable dans le MKV cible.")

    def _read_timestamp_scale_ns(self, fh: BinaryIO, state) -> int:
        for entry in state.data:
            if entry.element_id != _INFO_ID:
                continue
            cursor = entry.payload_offset
            while cursor < entry.end:
                child = self._base._read_ebml_element_from_file(
                    fh,
                    cursor,
                    entry.end,
                )
                if child.element_id == _TIMESTAMP_SCALE_ID:
                    return self._read_uint(fh, child.payload_offset, child.size)
                if child.unknown_size:
                    break
                cursor = child.end
            break
        return _DEFAULT_TIMESTAMP_SCALE_NS

    def _read_cluster_timestamp(self, fh: BinaryIO, cluster) -> int:
        cursor = cluster.payload_offset
        while cursor < cluster.end:
            child = self._base._read_ebml_element_from_file(
                fh,
                cursor,
                cluster.end,
            )
            if child.element_id == _TIMESTAMP_ID:
                return self._read_uint(fh, child.payload_offset, child.size)
            if child.unknown_size:
                break
            cursor = child.end
        return 0

    def _patch_simple_block(
        self,
        fh: BinaryIO,
        payload_offset: int,
        *,
        video_track_number: int,
        cluster_timestamp: int,
        pts_iter,
    ) -> bool:
        track_number, track_len = self._read_vint_value(fh, payload_offset)
        if track_number != video_track_number:
            return False

        try:
            pts_ms = next(pts_iter)
        except StopIteration as exc:
            raise RuntimeError("Plus de PTS source disponibles pour les blocs vidéo.") from exc

        relative_timecode = int(pts_ms) - int(cluster_timestamp)
        if not -32768 <= relative_timecode <= 32767:
            raise RuntimeError(
                f"Timecode relatif hors limites int16 : {relative_timecode} ms "
                f"(PTS={pts_ms}, cluster={cluster_timestamp})."
            )

        timecode_offset = payload_offset + track_len
        fh.seek(timecode_offset)
        fh.write(int(relative_timecode).to_bytes(2, "big", signed=True))
        return True

    @staticmethod
    def _read_uint(fh: BinaryIO, offset: int, size: int) -> int:
        fh.seek(offset)
        raw = fh.read(size)
        return int.from_bytes(raw, "big") if raw else 0

    @staticmethod
    def _read_vint_value(fh: BinaryIO, offset: int) -> tuple[int, int]:
        fh.seek(offset)
        first_raw = fh.read(1)
        if not first_raw:
            raise RuntimeError("VINT incomplet dans SimpleBlock.")
        first = first_raw[0]
        mask = 0x80
        length = 1
        while length <= 8 and not (first & mask):
            mask >>= 1
            length += 1
        if length > 8:
            raise RuntimeError("VINT SimpleBlock invalide.")
        rest = fh.read(length - 1)
        if len(rest) != length - 1:
            raise RuntimeError("VINT SimpleBlock tronqué.")
        value = first & (mask - 1)
        for byte in rest:
            value = (value << 8) | byte
        return value, length


__all__ = [
    "MatroskaVideoTimecodePatchResult",
    "MatroskaVideoTimecodePatcher",
]
