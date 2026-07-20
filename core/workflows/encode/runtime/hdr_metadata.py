"""HDR probing and static metadata helpers for encode workflows."""

from __future__ import annotations

import json
import random
import re
import subprocess
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, cast

from core.bluray import ffprobe_input_args
from core.subprocess_utils import subprocess_text_kwargs


class _LRUCache(OrderedDict):
    """Small bounded cache used for repeated preview probes."""

    def __init__(self, maxsize: int = 256) -> None:
        super().__init__()
        self._maxsize = maxsize

    def __setitem__(self, key, value) -> None:
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self._maxsize:
            self.popitem(last=False)


@dataclass(frozen=True)
class DynamicHdrPreviewFrame:
    time_s: float
    frame_index: int | None = None
    is_keyframe: bool = False
    pict_type: str = ""
    has_dovi: bool = False
    has_hdr10plus: bool = False


@dataclass(frozen=True)
class DynamicHdrPreviewProbeResult:
    keyframes: tuple[DynamicHdrPreviewFrame, ...] = ()
    dynamic_keyframes: tuple[DynamicHdrPreviewFrame, ...] = ()
    has_dovi: bool = False
    has_hdr10plus: bool = False
    warning: str = ""


@dataclass(frozen=True)
class DynamicHdrPreviewSceneSelection:
    requested_time_s: float
    scene_time_s: float
    reason: str = ""
    warning: str = ""
    hdr_kind: str = ""
    snapped: bool = False


class HdrMetadataProbeService:
    """Probe dynamic HDR presence and static HDR metadata with local caching."""

    _MASTER_DISPLAY_PRIMARIES: dict[str, tuple[tuple[float, float], ...]] = {
        "bt.2020":    ((0.170, 0.797), (0.131, 0.046), (0.708, 0.292), (0.3127, 0.3290)),
        "display p3": ((0.265, 0.690), (0.150, 0.060), (0.680, 0.320), (0.3127, 0.3290)),
        "p3-d65":     ((0.265, 0.690), (0.150, 0.060), (0.680, 0.320), (0.3127, 0.3290)),
        "bt.709":     ((0.300, 0.600), (0.150, 0.060), (0.640, 0.330), (0.3127, 0.3290)),
    }

    DEFAULT_MAX_CLL = "1000,400"

    def __init__(
        self,
        *,
        ffmpeg_bin: Callable[[], str],
        tool_bin: Callable[[str], str],
    ) -> None:
        self._ffmpeg_bin = ffmpeg_bin
        self._tool_bin = tool_bin
        self._ffprobe_payload_cache: _LRUCache = _LRUCache(maxsize=256)
        self._ffprobe_frame_hdr_cache: _LRUCache = _LRUCache(maxsize=256)
        self._preview_scene_cache: _LRUCache = _LRUCache(maxsize=64)
        self._preview_keyframes_cache: _LRUCache = _LRUCache(maxsize=64)
        self._mediainfo_hdr_cache: _LRUCache = _LRUCache(maxsize=256)

    @staticmethod
    def ffprobe_bin_from_ffmpeg(ffmpeg_bin: str) -> str:
        ffmpeg_path = Path(ffmpeg_bin)
        name = ffmpeg_path.name.lower()
        if name in {"ffmpeg", "ffmpeg.exe"}:
            return str(ffmpeg_path.with_name("ffprobe" + ffmpeg_path.suffix))
        return "ffprobe"

    def load_mediainfo_video_track(self, path: Path) -> dict | None:
        mediainfo_bin = self._tool_bin("mediainfo")
        try:
            result = subprocess.run(
                [mediainfo_bin, "--Output=JSON", str(path)],
                capture_output=True,
                check=False,
                **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError):
            return None
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return None
        media = data.get("media") or {}
        for track in media.get("track") or []:
            if isinstance(track, dict) and track.get("@type") == "Video":
                return track
        return None

    def detect_source_dynamic_hdr_presence(
        self,
        source: Path,
        *,
        ffprobe_streams_payload: Callable[[Path], dict[str, object] | None] | None = None,
        ffprobe_stream_dicts: Callable[[dict[str, object]], list[dict[str, object]]] | None = None,
        mediainfo_hdr_flags: Callable[[Path], tuple[bool, bool] | None] | None = None,
        ffprobe_frame_dynamic_hdr_flags: Callable[[Path], tuple[bool, bool] | None] | None = None,
    ) -> tuple[bool, bool] | None:
        payload_fn = ffprobe_streams_payload or self.ffprobe_streams_payload
        stream_dicts_fn = ffprobe_stream_dicts or self.ffprobe_stream_dicts
        mediainfo_fn = mediainfo_hdr_flags or self.mediainfo_hdr_flags
        frame_flags_fn = ffprobe_frame_dynamic_hdr_flags or self.ffprobe_frame_dynamic_hdr_flags

        payload = payload_fn(source)
        has_dv = False
        has_hdr10plus = False
        frame_flags: tuple[bool, bool] | None = None
        if payload is not None:
            for stream in stream_dicts_fn(payload):
                if stream.get("codec_type") != "video":
                    continue
                side_data_obj = stream.get("side_data_list")
                side_data: list[dict[str, object]] = []
                if isinstance(side_data_obj, list):
                    for item in side_data_obj:
                        if isinstance(item, dict):
                            side_data.append(cast(dict[str, object], item))
                if any(sd.get("side_data_type") == "DOVI configuration record" for sd in side_data):
                    has_dv = True
                if any(
                    sd.get("side_data_type") == "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"
                    for sd in side_data
                ):
                    has_hdr10plus = True
                if has_dv and has_hdr10plus:
                    break

        mediainfo_flags = mediainfo_fn(source)
        if mediainfo_flags is not None:
            mi_dv, mi_hdr10plus = mediainfo_flags
            has_dv = has_dv or mi_dv
            has_hdr10plus = has_hdr10plus or mi_hdr10plus

        if not has_dv or not has_hdr10plus:
            frame_flags = frame_flags_fn(source)
            if frame_flags is not None:
                frame_dv, frame_hdr10plus = frame_flags
                has_dv = has_dv or frame_dv
                has_hdr10plus = has_hdr10plus or frame_hdr10plus

        if payload is None and mediainfo_flags is None and frame_flags is None:
            return None
        return has_dv, has_hdr10plus

    def ffprobe_streams_payload(self, source: Path) -> dict[str, object] | None:
        cache_key = self.source_cache_key(source)
        if cache_key is not None and cache_key in self._ffprobe_payload_cache:
            return self._ffprobe_payload_cache[cache_key]

        cmd = [
            self.ffprobe_bin_from_ffmpeg(self._ffmpeg_bin()),
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
        ]
        cmd.extend(ffprobe_input_args(source))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                timeout=20,
                **subprocess_text_kwargs(),
            )
        except FileNotFoundError:
            payload = None
        else:
            if result.returncode != 0:
                payload = None
            else:
                try:
                    payload = json.loads(result.stdout or "{}")
                except json.JSONDecodeError:
                    payload = None

        if cache_key is not None:
            self._ffprobe_payload_cache[cache_key] = payload
        return payload

    @staticmethod
    def source_cache_key(source: Path) -> tuple[str, int, int] | None:
        try:
            st = source.stat()
        except OSError:
            return None
        return (str(source), st.st_mtime_ns, st.st_size)

    @staticmethod
    def ffprobe_stream_dicts(payload: dict[str, object]) -> list[dict[str, object]]:
        streams_obj = payload.get("streams")
        if not isinstance(streams_obj, list):
            return []
        out: list[dict[str, object]] = []
        for item in streams_obj:
            if isinstance(item, dict):
                out.append(cast(dict[str, object], item))
        return out

    def ffprobe_frame_dynamic_hdr_flags(
        self,
        source: Path,
        *,
        max_frames: int = 240,
    ) -> tuple[bool, bool] | None:
        cache_key = self.source_cache_key(source)
        if cache_key is not None and cache_key in self._ffprobe_frame_hdr_cache:
            return self._ffprobe_frame_hdr_cache[cache_key]

        cmd = [
            self.ffprobe_bin_from_ffmpeg(self._ffmpeg_bin()),
            "-v", "quiet",
            "-print_format", "json",
            "-select_streams", "v:0",
            "-read_intervals", f"%+#{max(1, int(max_frames))}",
            "-show_frames",
            "-show_entries", "frame_side_data=side_data_type",
        ]
        cmd.extend(ffprobe_input_args(source))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                timeout=30,
                **subprocess_text_kwargs(),
            )
        except FileNotFoundError:
            flags: tuple[bool, bool] | None = None
        else:
            if result.returncode != 0:
                flags = None
            else:
                try:
                    payload = json.loads(result.stdout or "{}")
                except json.JSONDecodeError:
                    flags = None
                else:
                    frames_obj = payload.get("frames")
                    has_dv = False
                    has_hdr10plus = False
                    if isinstance(frames_obj, list):
                        for frame in frames_obj:
                            if not isinstance(frame, dict):
                                continue
                            side_data_obj = frame.get("side_data_list")
                            if not isinstance(side_data_obj, list):
                                continue
                            for side_data in side_data_obj:
                                if not isinstance(side_data, dict):
                                    continue
                                side_type = str(side_data.get("side_data_type", "") or "")
                                side_type_lower = side_type.lower()
                                if ("dolby vision" in side_type_lower) or (side_type == "DOVI configuration record"):
                                    has_dv = True
                                if (
                                    "hdr dynamic metadata smpte2094-40" in side_type_lower
                                    or "hdr10+" in side_type_lower
                                    or "smpte st 2094" in side_type_lower
                                    or "smpte2094" in side_type_lower
                                ):
                                    has_hdr10plus = True
                                if has_dv and has_hdr10plus:
                                    break
                            if has_dv and has_hdr10plus:
                                break
                    flags = (has_dv, has_hdr10plus)

        if cache_key is not None:
            self._ffprobe_frame_hdr_cache[cache_key] = flags
        return flags

    def select_preview_scene(
        self,
        source: Path,
        *,
        stream_index: int = 0,
        requested_time_s: float = 0.0,
        preview_duration_s: float = 5.0,
        source_duration_s: float | None = None,
        random_scene: bool = False,
        prefer_dovi: bool = False,
        prefer_hdr10plus: bool = False,
        progress_cb: Callable[[str], None] | None = None,
        progress_pct_cb: Callable[[int], None] | None = None,
    ) -> DynamicHdrPreviewSceneSelection:
        duration = float(source_duration_s or 0.0)
        preview_duration = max(0.1, float(preview_duration_s or 0.1))
        max_start = max(0.0, duration - preview_duration) if duration > 0 else None

        def _clamp(value: float) -> float:
            value = max(0.0, float(value or 0.0))
            if max_start is not None:
                value = min(value, max_start)
            return value

        requested = _clamp(float(requested_time_s or 0.0))
        wants_dynamic = bool(prefer_dovi or prefer_hdr10plus)

        if not wants_dynamic:
            if random_scene:
                ceiling = max_start or max(duration, 0.0) or 0.0
                requested = random.uniform(0.0, ceiling) if ceiling > 0 else 0.0
            return DynamicHdrPreviewSceneSelection(
                requested_time_s=requested,
                scene_time_s=_clamp(requested),
            )

        # HDR : snap-back sur la keyframe la plus proche via packet-probe rapide.
        keyframes = self.preview_keyframe_times(
            source,
            stream_index=stream_index,
            source_duration_s=source_duration_s,
            progress_cb=progress_cb,
            progress_pct_cb=progress_pct_cb,
        )
        in_range = [k for k in keyframes if max_start is None or k <= max_start]
        if not in_range:
            if random_scene:
                ceiling = max_start or max(duration, 0.0) or 0.0
                requested = random.uniform(0.0, ceiling) if ceiling > 0 else 0.0
            return DynamicHdrPreviewSceneSelection(
                requested_time_s=requested,
                scene_time_s=_clamp(requested),
                warning="Keyframes introuvables : extraction sans snap-back.",
            )

        if random_scene:
            requested = random.uniform(0.0, max_start or max(duration, 0.0) or 0.0)
            candidates_sorted = list(in_range)
            random.shuffle(candidates_sorted)
        else:
            candidates_sorted = sorted(in_range, key=lambda k: abs(k - requested))

        chosen = candidates_sorted[0]
        verified = False
        for candidate in candidates_sorted[:8]:
            if self.verify_keyframe_hdr_at(
                source,
                time_s=candidate,
                stream_index=stream_index,
                require_dovi=prefer_dovi,
                require_hdr10plus=prefer_hdr10plus,
            ):
                chosen = candidate
                verified = True
                break

        labels = [name for name, flag in (("DoVi", prefer_dovi), ("HDR10+", prefer_hdr10plus)) if flag]
        return DynamicHdrPreviewSceneSelection(
            requested_time_s=requested,
            scene_time_s=chosen,
            reason=f"I-frame {'/'.join(labels)}" if verified else f"I-frame {'/'.join(labels)} (non vérifiée)",
            hdr_kind="/".join(labels),
            snapped=abs(chosen - requested) > 0.001,
            warning="" if verified else "Aucune keyframe vérifiée avec les métadonnées HDR demandées.",
        )

    def select_preview_scenes_random(
        self,
        source: Path,
        *,
        count: int,
        stream_index: int = 0,
        source_duration_s: float | None = None,
        skip_lead_s: float = 1.0,
        skip_tail_s: float = 5.0,
        prefer_dovi: bool = False,
        prefer_hdr10plus: bool = False,
        progress_cb: Callable[[str], None] | None = None,
        progress_pct_cb: Callable[[int], None] | None = None,
    ) -> tuple[DynamicHdrPreviewSceneSelection, ...]:
        """Choisit `count` keyframes uniformément réparties sur la durée.

        - SDR : pas de probe (ffmpeg snappera sur la keyframe la plus proche au moment du extract).
        - HDR : on récupère les timestamps des keyframes par demux paquet (rapide, sans décodage)
          puis on tire au hasard une keyframe par bucket de durée. Garantit que les frames
          extraites portent DoVi RPU / HDR10+ SEI (qui ne sont fiables qu'aux keyframes).
        """
        if count <= 0:
            return ()

        duration = float(source_duration_s or 0.0)
        min_t = max(0.0, float(skip_lead_s))
        max_t = max(min_t + 0.1, duration - float(skip_tail_s)) if duration > 0 else None
        if max_t is None or max_t <= min_t:
            return ()

        wants_dynamic = bool(prefer_dovi or prefer_hdr10plus)
        if not wants_dynamic:
            bucket_size = (max_t - min_t) / count
            selections: list[DynamicHdrPreviewSceneSelection] = []
            for i in range(count):
                lo = min_t + i * bucket_size
                hi = min_t + (i + 1) * bucket_size
                t = random.uniform(lo, hi)
                selections.append(DynamicHdrPreviewSceneSelection(
                    requested_time_s=t,
                    scene_time_s=t,
                ))
            if progress_pct_cb is not None:
                progress_pct_cb(100)
            return tuple(selections)

        keyframes = self.preview_keyframe_times(
            source,
            stream_index=stream_index,
            source_duration_s=source_duration_s,
            progress_cb=progress_cb,
            progress_pct_cb=progress_pct_cb,
        )
        in_range = [k for k in keyframes if min_t <= k <= max_t]
        if not in_range:
            # Pas de keyframe dans la fenêtre utile, fallback au tirage uniforme
            bucket_size = (max_t - min_t) / count
            return tuple(
                DynamicHdrPreviewSceneSelection(
                    requested_time_s=(t := random.uniform(min_t + i * bucket_size, min_t + (i + 1) * bucket_size)),
                    scene_time_s=t,
                )
                for i in range(count)
            )

        bucket_size = (max_t - min_t) / count
        labels = [n for n, f in (("DoVi", prefer_dovi), ("HDR10+", prefer_hdr10plus)) if f]
        verified_reason = f"I-frame {'/'.join(labels)}"
        fallback_reason = f"I-frame {'/'.join(labels)} (non vérifiée)"

        def _verify(time_s: float) -> bool:
            return self.verify_keyframe_hdr_at(
                source,
                time_s=time_s,
                stream_index=stream_index,
                require_dovi=prefer_dovi,
                require_hdr10plus=prefer_hdr10plus,
            )

        results: list[DynamicHdrPreviewSceneSelection] = []
        used: set[float] = set()
        for i in range(count):
            lo = min_t + i * bucket_size
            hi = min_t + (i + 1) * bucket_size
            bucket = [k for k in in_range if lo <= k <= hi and k not in used]
            random.shuffle(bucket)
            pick = None
            verified = False
            for candidate in bucket[:6]:
                if _verify(candidate):
                    pick = candidate
                    verified = True
                    break
            if pick is None and bucket:
                pick = bucket[0]
            if pick is None:
                continue
            used.add(pick)
            results.append(DynamicHdrPreviewSceneSelection(
                requested_time_s=pick,
                scene_time_s=pick,
                reason=verified_reason if verified else fallback_reason,
            ))

        if len(results) < count:
            remaining = [k for k in in_range if k not in used]
            random.shuffle(remaining)
            for candidate in remaining:
                if len(results) >= count:
                    break
                verified = _verify(candidate)
                used.add(candidate)
                results.append(DynamicHdrPreviewSceneSelection(
                    requested_time_s=candidate,
                    scene_time_s=candidate,
                    reason=verified_reason if verified else fallback_reason,
                ))

        results.sort(key=lambda s: s.scene_time_s)
        return tuple(results)

    def source_hdr_transfer(self, source: Path) -> str:
        """Renvoie "pq" (HDR10/DoVi), "hlg" ou "" (SDR) selon le color_transfer du média."""
        cmd = [
            self.ffprobe_bin_from_ffmpeg(self._ffmpeg_bin()),
            "-v", "quiet",
            "-select_streams", "v:0",
            "-show_entries", "stream=color_transfer",
            "-of", "csv=p=0",
        ]
        cmd.extend(ffprobe_input_args(source))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                timeout=20,
                **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return ""
        if result.returncode != 0:
            return ""
        transfer = (result.stdout or "").strip().lower()
        if transfer in {"smpte2084", "smpte2084le"}:
            return "pq"
        if transfer in {"arib-std-b67", "bt2020-10", "bt2020-12"}:
            return "hlg" if transfer == "arib-std-b67" else "pq"
        return ""

    def preview_scene_probe(
        self,
        source: Path,
        *,
        stream_index: int = 0,
        source_duration_s: float | None = None,
        progress_cb: Callable[[str], None] | None = None,
        progress_pct_cb: Callable[[int], None] | None = None,
    ) -> DynamicHdrPreviewProbeResult:
        source_key = self.source_cache_key(source)
        cache_key = (*source_key, int(stream_index)) if source_key is not None else None
        if cache_key is not None and cache_key in self._preview_scene_cache:
            return self._preview_scene_cache[cache_key]

        cmd = [
            self.ffprobe_bin_from_ffmpeg(self._ffmpeg_bin()),
            "-v", "quiet",
            "-skip_frame", "nokey",
            "-print_format", "json",
            "-select_streams", str(max(0, int(stream_index))),
            "-show_frames",
            "-show_entries",
            (
                "frame=best_effort_timestamp_time,pkt_pts_time,pkt_dts_time,"
                "coded_picture_number,display_picture_number,key_frame,pict_type:"
                "frame_side_data=side_data_type"
            ),
        ]
        cmd.extend(ffprobe_input_args(source))
        if progress_cb is not None:
            progress_cb("Analyse des keyframes HDR (ffprobe)…")
        if progress_pct_cb is not None:
            progress_pct_cb(0)
        estimated_total = max(50, int(float(source_duration_s or 60.0) / 2.0))
        timeout_s = 300
        probe: DynamicHdrPreviewProbeResult
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,
                **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError) as exc:
            probe = DynamicHdrPreviewProbeResult(warning=f"Probe HDR dynamique indisponible : {exc}")
        else:
            output_parts: list[str] = []
            key_count = 0
            last_pct = -1
            deadline = time.monotonic() + timeout_s
            timed_out = False
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    if time.monotonic() > deadline:
                        timed_out = True
                        break
                    output_parts.append(line)
                    if '"key_frame": 1' in line:
                        key_count += 1
                        pct = min(95, int(key_count * 100 / estimated_total))
                        if progress_pct_cb is not None and pct != last_pct:
                            progress_pct_cb(pct)
                            last_pct = pct
                        if progress_cb is not None and key_count % 100 == 0:
                            progress_cb(f"Probe HDR : {key_count} keyframes analysées…")
                if timed_out:
                    proc.kill()
                    proc.wait(timeout=5)
                    probe = DynamicHdrPreviewProbeResult(
                        warning=f"Probe HDR dynamique : timeout après {timeout_s}s."
                    )
                else:
                    proc.wait(timeout=10)
                    if proc.returncode != 0:
                        probe = DynamicHdrPreviewProbeResult(
                            warning="Probe HDR dynamique ffprobe impossible."
                        )
                    else:
                        try:
                            payload = json.loads("".join(output_parts) or "{}")
                        except json.JSONDecodeError:
                            probe = DynamicHdrPreviewProbeResult(
                                warning="Réponse ffprobe HDR dynamique illisible."
                            )
                        else:
                            probe = self.preview_scene_probe_from_payload(payload)
                            if progress_cb is not None:
                                progress_cb(f"Probe HDR : {key_count} keyframes analysées.")
            except (OSError, subprocess.TimeoutExpired) as exc:
                try:
                    proc.kill()
                except OSError:
                    pass
                probe = DynamicHdrPreviewProbeResult(warning=f"Probe HDR dynamique : {exc}")
        if progress_pct_cb is not None:
            progress_pct_cb(100)

        if cache_key is not None:
            self._preview_scene_cache[cache_key] = probe
        return probe

    def verify_keyframe_hdr_at(
        self,
        source: Path,
        *,
        time_s: float,
        stream_index: int = 0,
        require_dovi: bool = False,
        require_hdr10plus: bool = False,
    ) -> bool:
        """Décode 1 keyframe à `time_s` et vérifie la présence des side_data demandées."""
        if not (require_dovi or require_hdr10plus):
            return True
        cmd = [
            self.ffprobe_bin_from_ffmpeg(self._ffmpeg_bin()),
            "-v", "quiet",
            "-read_intervals", f"{max(0.0, float(time_s)):.3f}%+#1",
            "-select_streams", f"v:{max(0, int(stream_index))}",
            "-show_entries", "frame_side_data=side_data_type",
            "-of", "json",
        ]
        cmd.extend(ffprobe_input_args(source))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                timeout=20,
                **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return True  # ne bloque pas si la vérif échoue
        if result.returncode != 0:
            return True
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return True
        frames = payload.get("frames")
        if not isinstance(frames, list) or not frames:
            return False
        side_data = frames[0].get("side_data_list") if isinstance(frames[0], dict) else None
        if not isinstance(side_data, list):
            return False
        has_dovi = False
        has_hdr10plus = False
        for entry in side_data:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("side_data_type", "") or "").lower()
            if "dolby vision" in kind or "dovi" in kind or "rpu" in kind:
                has_dovi = True
            if "smpte2094-40" in kind or "smpte st 2094" in kind or "hdr10+" in kind:
                has_hdr10plus = True
        if require_dovi and not has_dovi:
            return False
        if require_hdr10plus and not has_hdr10plus:
            return False
        return True

    def preview_keyframe_times(
        self,
        source: Path,
        *,
        stream_index: int = 0,
        source_duration_s: float | None = None,
        progress_cb: Callable[[str], None] | None = None,
        progress_pct_cb: Callable[[int], None] | None = None,
    ) -> tuple[float, ...]:
        """Récupère les timestamps des keyframes via demux paquet (sans décodage).

        Beaucoup plus rapide que `preview_scene_probe` (qui décode chaque keyframe pour
        extraire les side_data HDR). Les DoVi RPU / HDR10+ SEI étant attachés aux
        keyframes du flux HEVC, leur position est suffisante pour notre snap-back.
        """
        source_key = self.source_cache_key(source)
        cache_key = (*source_key, int(stream_index)) if source_key is not None else None
        if cache_key is not None and cache_key in self._preview_keyframes_cache:
            return self._preview_keyframes_cache[cache_key]

        cmd = [
            self.ffprobe_bin_from_ffmpeg(self._ffmpeg_bin()),
            "-v", "quiet",
            "-select_streams", f"v:{max(0, int(stream_index))}",
            "-show_entries", "packet=pts_time,flags",
            "-of", "csv=p=0",
        ]
        cmd.extend(ffprobe_input_args(source))
        if progress_cb is not None:
            progress_cb("Index des keyframes (ffprobe packets)…")
        if progress_pct_cb is not None:
            progress_pct_cb(0)
        duration = max(1.0, float(source_duration_s or 0.0))
        keyframes: list[float] = []
        timed_out = False
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,
                **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError):
            if cache_key is not None:
                self._preview_keyframes_cache[cache_key] = ()
            return ()

        last_pct = -1
        deadline = time.monotonic() + 600
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if time.monotonic() > deadline:
                    timed_out = True
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    pts_str, flags = line.split(",", 1)
                    pts = float(pts_str)
                except (ValueError, AttributeError):
                    continue
                if "K" in flags:
                    keyframes.append(pts)
                    if progress_cb is not None and len(keyframes) % 500 == 0:
                        progress_cb(f"Index keyframes : {len(keyframes)} trouvées…")
                if progress_pct_cb is not None:
                    pct = min(95, int(pts * 100 / duration))
                    if pct != last_pct:
                        progress_pct_cb(pct)
                        last_pct = pct
            if timed_out:
                try:
                    proc.kill()
                except OSError:
                    pass
            else:
                proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass

        if progress_pct_cb is not None:
            progress_pct_cb(100)
        result = tuple(keyframes)
        if cache_key is not None:
            self._preview_keyframes_cache[cache_key] = result
        return result

    @classmethod
    def preview_scene_probe_from_payload(
        cls,
        payload: dict[str, object],
    ) -> DynamicHdrPreviewProbeResult:
        frames_obj = payload.get("frames")
        keyframes: list[DynamicHdrPreviewFrame] = []
        dynamic_keyframes: list[DynamicHdrPreviewFrame] = []
        has_dovi = False
        has_hdr10plus = False
        if not isinstance(frames_obj, list):
            return DynamicHdrPreviewProbeResult()

        for frame_index, raw_frame in enumerate(frames_obj):
            if not isinstance(raw_frame, dict):
                continue
            frame = cls._preview_frame_from_ffprobe_dict(raw_frame, fallback_index=frame_index)
            if frame is None:
                continue
            has_dovi = has_dovi or frame.has_dovi
            has_hdr10plus = has_hdr10plus or frame.has_hdr10plus
            if frame.is_keyframe:
                keyframes.append(frame)
                if frame.has_dovi or frame.has_hdr10plus:
                    dynamic_keyframes.append(frame)

        return DynamicHdrPreviewProbeResult(
            keyframes=tuple(keyframes),
            dynamic_keyframes=tuple(dynamic_keyframes),
            has_dovi=has_dovi,
            has_hdr10plus=has_hdr10plus,
        )

    @staticmethod
    def _preview_frame_from_ffprobe_dict(
        frame: dict[str, object],
        *,
        fallback_index: int,
    ) -> DynamicHdrPreviewFrame | None:
        time_s: float | None = None
        for key in ("best_effort_timestamp_time", "pkt_pts_time", "pkt_dts_time"):
            raw_time = frame.get(key)
            try:
                time_s = float(str(raw_time))
                break
            except (TypeError, ValueError):
                continue
        if time_s is None:
            return None

        pict_type = str(frame.get("pict_type", "") or "").upper()
        key_raw = frame.get("key_frame", 0)
        try:
            key_frame = int(str(key_raw)) == 1
        except (TypeError, ValueError):
            key_frame = False
        is_keyframe = key_frame or pict_type == "I"

        frame_number: int | None = None
        for number_key in ("coded_picture_number", "display_picture_number"):
            raw_number = frame.get(number_key)
            try:
                frame_number = int(str(raw_number))
                break
            except (TypeError, ValueError):
                continue
        if frame_number is None:
            frame_number = fallback_index

        has_dovi = False
        has_hdr10plus = False
        side_data_obj = frame.get("side_data_list")
        if isinstance(side_data_obj, list):
            for side_data in side_data_obj:
                if not isinstance(side_data, dict):
                    continue
                side_type = str(side_data.get("side_data_type", "") or "")
                side_type_lower = side_type.lower()
                if (
                    "dolby vision" in side_type_lower
                    or "dovi" in side_type_lower
                    or "rpu" in side_type_lower
                ):
                    has_dovi = True
                if (
                    "hdr dynamic metadata smpte2094-40" in side_type_lower
                    or "hdr10+" in side_type_lower
                    or "smpte st 2094" in side_type_lower
                    or "smpte2094" in side_type_lower
                ):
                    has_hdr10plus = True

        return DynamicHdrPreviewFrame(
            time_s=max(0.0, time_s),
            frame_index=frame_number,
            is_keyframe=is_keyframe,
            pict_type=pict_type,
            has_dovi=has_dovi,
            has_hdr10plus=has_hdr10plus,
        )

    @staticmethod
    def _selection_from_frame(
        requested: float,
        frame: DynamicHdrPreviewFrame,
        *,
        warning: str = "",
    ) -> DynamicHdrPreviewSceneSelection:
        labels: list[str] = []
        if frame.has_dovi:
            labels.append("DoVi")
        if frame.has_hdr10plus:
            labels.append("HDR10+")
        hdr_kind = "/".join(labels)
        reason = f"I-frame {hdr_kind} metadata" if hdr_kind else "I-frame metadata"
        return DynamicHdrPreviewSceneSelection(
            requested_time_s=requested,
            scene_time_s=frame.time_s,
            reason=reason,
            warning=warning,
            hdr_kind=hdr_kind,
            snapped=abs(frame.time_s - requested) > 0.001,
        )

    def mediainfo_hdr_flags(self, source: Path) -> tuple[bool, bool] | None:
        cache_key = self.source_cache_key(source)
        if cache_key is not None and cache_key in self._mediainfo_hdr_cache:
            return self._mediainfo_hdr_cache[cache_key]

        mediainfo_bin = self._tool_bin("mediainfo")
        try:
            hdr_format = subprocess.run(
                [mediainfo_bin, "--Inform=Video;%HDR_Format%", str(source)],
                capture_output=True,
                check=False,
                timeout=20,
                **subprocess_text_kwargs(),
            )
            hdr_compat = subprocess.run(
                [mediainfo_bin, "--Inform=Video;%HDR_Format_Compatibility%", str(source)],
                capture_output=True,
                check=False,
                timeout=20,
                **subprocess_text_kwargs(),
            )
        except FileNotFoundError:
            result: tuple[bool, bool] | None = None
        else:
            hdr_text = f"{hdr_format.stdout or ''}\n{hdr_compat.stdout or ''}".lower()
            result = (
                "dolby vision" in hdr_text,
                (
                    "hdr10+" in hdr_text
                    or "smpte st 2094" in hdr_text
                    or "smpte2094" in hdr_text
                ),
            )

        if cache_key is not None:
            self._mediainfo_hdr_cache[cache_key] = result
        return result

    def build_master_display_for_primaries(self, primaries_label: str) -> str:
        primaries = self._MASTER_DISPLAY_PRIMARIES.get(primaries_label.strip().lower())
        if not primaries:
            return ""
        (gx, gy), (bx, by), (rx, ry), (wx, wy) = primaries

        def c(value: float) -> int:
            return int(round(value * 50000))

        return (
            f"G({c(gx)},{c(gy)})"
            f"B({c(bx)},{c(by)})"
            f"R({c(rx)},{c(ry)})"
            f"WP({c(wx)},{c(wy)})"
            f"L(10000000,1)"
        )

    def color_primaries_label(self, source: Path) -> str:
        cmd = [
            self._tool_bin("ffprobe"),
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=color_primaries",
            "-of", "default=nw=1:nk=1",
        ]
        cmd.extend(ffprobe_input_args(source))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True, check=False, timeout=10,
                **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError):
            return ""
        return (result.stdout or "").strip().lower()

    def extract_static_hdr_via_ffprobe(self, source: Path) -> tuple[str, str]:
        cmd = [
            self._tool_bin("ffprobe"),
            "-v", "error",
            "-select_streams", "v:0",
            "-show_frames",
            "-read_intervals", "%+#1",
            "-print_format", "json",
        ]
        cmd.extend(ffprobe_input_args(source))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True, check=False, timeout=20,
                **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError):
            return "", ""
        if result.returncode != 0:
            return "", ""
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return "", ""
        frames = data.get("frames") or []
        if not frames:
            return "", ""
        side_data_list = frames[0].get("side_data_list") or []

        def _num(rat: str) -> int:
            try:
                return int(str(rat).split("/", 1)[0])
            except (ValueError, AttributeError):
                return 0

        master_display = ""
        max_cll = ""
        for sd in side_data_list:
            stype = sd.get("side_data_type") or ""
            if stype == "Mastering display metadata":
                gx, gy = _num(sd.get("green_x")), _num(sd.get("green_y"))
                bx, by = _num(sd.get("blue_x")), _num(sd.get("blue_y"))
                rx, ry = _num(sd.get("red_x")), _num(sd.get("red_y"))
                wx, wy = _num(sd.get("white_point_x")), _num(sd.get("white_point_y"))
                lmin = _num(sd.get("min_luminance"))
                lmax = _num(sd.get("max_luminance"))
                if lmax > 0 and (rx > 0 or gx > 0 or bx > 0):
                    master_display = (
                        f"G({gx},{gy})B({bx},{by})R({rx},{ry})"
                        f"WP({wx},{wy})L({lmax},{lmin})"
                    )
            elif stype == "Content light level metadata":
                try:
                    mc = int(sd.get("max_content") or 0)
                    ma = int(sd.get("max_average") or 0)
                except (TypeError, ValueError):
                    mc = ma = 0
                if mc > 0:
                    max_cll = f"{mc},{ma}"
        return master_display, max_cll

    def extract_static_hdr_metadata(self, source: Path) -> tuple[str, str]:
        mi_video = self.load_mediainfo_video_track(source)
        if mi_video is None:
            return "", ""

        master_display = ""
        primaries_label = str(mi_video.get("MasteringDisplay_ColorPrimaries") or "").strip().lower()
        primaries = self._MASTER_DISPLAY_PRIMARIES.get(primaries_label)
        try:
            lmin = float(mi_video.get("MasteringDisplay_Luminance_Min") or 0)
            lmax = float(mi_video.get("MasteringDisplay_Luminance_Max") or 0)
        except (TypeError, ValueError):
            lmin = lmax = 0.0
        if primaries and lmax > 0:
            (gx, gy), (bx, by), (rx, ry), (wx, wy) = primaries

            def c(value: float) -> int:
                return int(round(value * 50000))

            def l_(value: float) -> int:
                return int(round(value * 10000))

            master_display = (
                f"G({c(gx)},{c(gy)})"
                f"B({c(bx)},{c(by)})"
                f"R({c(rx)},{c(ry)})"
                f"WP({c(wx)},{c(wy)})"
                f"L({l_(lmax)},{l_(lmin)})"
            )

        max_cll = ""
        try:
            max_content = int(re.sub(r"[^\d]", "", str(mi_video.get("MaxCLL") or "")) or 0)
            max_average = int(re.sub(r"[^\d]", "", str(mi_video.get("MaxFALL") or "")) or 0)
        except (TypeError, ValueError):
            max_content = max_average = 0
        if max_content > 0:
            max_cll = f"{max_content},{max_average}"
        return master_display, max_cll
