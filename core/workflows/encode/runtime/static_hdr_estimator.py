"""Static HDR10 metadata estimator for Dolby Vision P5 -> P8.1 conversions."""

from __future__ import annotations

import math
import re
import shutil
import sys
import subprocess
import tempfile
import time
from array import array
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from core.bluray import append_ffmpeg_input_args
from core.subprocess_utils import subprocess_text_kwargs


@dataclass(frozen=True)
class StaticHdrEstimate:
    master_display: str
    max_cll: str
    confidence: str
    sample_count: int
    coverage: float
    warnings: tuple[str, ...] = field(default_factory=tuple)
    source: str = "estimated_p5_to_p8"
    mode: str = "fast"
    analysis_method: str = ""
    active_sample_count: int = 0
    ignored_sample_count: int = 0
    active_crop: str = ""


@dataclass(frozen=True)
class _SignalStatsSample:
    ymax: float
    yavg: float
    bit_depth: int | None = None
    pts_time: float | None = None


@dataclass(frozen=True)
class _FrameLuminance:
    peak_nits: float
    average_nits: float


class StaticHdrEstimateService:
    """Estimate HDR10 static metadata from a converted P5 -> P8 bitstream."""

    FAST_MODE = "fast"
    PRECISE_MODE = "precise"
    ESTIMATOR_VERSION = "v2"
    TARGET_SAMPLES = 96
    PRECISE_DISCOVERY_SAMPLES = 160
    PRECISE_UNIFORM_SAMPLES = 128
    PRECISE_FINE_SAMPLES = 96
    PRECISE_ANALYSIS_WIDTH = 960
    PRECISE_ANALYSIS_HEIGHT = 540
    PRECISE_FINE_CANDIDATES = 8
    PRECISE_FINE_WINDOW_S = 12.0
    EDGE_SKIP_RATIO = 0.10
    DEFAULT_MAX_CLL = "1000,400"
    SOURCE_LABEL = "estimated_p5_to_p8"
    MIN_ACTIVE_PEAK_NITS = 5.0
    MIN_ACTIVE_AVERAGE_NITS = 0.25

    _SIGNALSTATS_RE = re.compile(
        r"lavfi\.signalstats\.(YMAX|YAVG|YBITDEPTH)\s*=\s*([0-9]+(?:\.[0-9]+)?)"
    )
    _PTS_TIME_RE = re.compile(r"(?:^|\s)pts_time:([0-9]+(?:\.[0-9]+)?)")
    _CROP_RE = re.compile(r"\bcrop=(\d+):(\d+):(\d+):(\d+)")
    _CROP_META_RE = re.compile(
        r"lavfi\.cropdetect\.(w|h|x|y)\s*=\s*([0-9]+(?:\.[0-9]+)?)"
    )

    def __init__(
        self,
        *,
        ffmpeg_bin: str,
        dovi_tool_bin: str,
    ) -> None:
        self._ffmpeg = ffmpeg_bin
        self._dovi_tool = dovi_tool_bin
        self._cache: dict[tuple[object, ...], StaticHdrEstimate] = {}
        self._precise_support: bool | None = None

    def estimate_p5_to_p8_static_hdr(
        self,
        source: Path,
        *,
        stream_index: int,
        duration_s: float | None,
        work_dir: Path | None,
        mode: str = PRECISE_MODE,
        progress_cb: Callable[[str], None] | None = None,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> StaticHdrEstimate:
        mode = self._normalize_mode(mode)
        cache_key = self._cache_key(source, stream_index, duration_s, mode)
        if cache_key in self._cache:
            return self._cache[cache_key]

        base_dir = Path(work_dir) if work_dir is not None else Path(tempfile.gettempdir())
        base_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(prefix="static_hdr_p5_", dir=str(base_dir)))
        try:
            self._check_cancelled(cancel_cb)
            annexb = tmp_dir / "source_annexb.hevc"
            converted = tmp_dir / "source_p8.hevc"

            self._emit(progress_cb, "Analyse HDR10 estimée : extraction HEVC P5…")
            extract_cmd = [
                self._ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-y",
            ]
            append_ffmpeg_input_args(extract_cmd, source)
            extract_cmd.extend([
                "-map",
                f"0:{int(stream_index)}",
                "-c",
                "copy",
                "-bsf:v",
                "hevc_mp4toannexb",
                "-f",
                "hevc",
                str(annexb),
            ])
            self._run_capture(extract_cmd, cancel_cb=cancel_cb)

            self._emit(progress_cb, "Analyse HDR10 estimée : conversion P5 → P8…")
            self._run_capture(
                [
                    self._dovi_tool,
                    "-m",
                    "3",
                    "convert",
                    "-i",
                    str(annexb),
                    "-o",
                    str(converted),
                ],
                cancel_cb=cancel_cb,
            )

            estimate = self.estimate_converted_static_hdr(
                converted,
                duration_s=duration_s,
                mode=mode,
                progress_cb=progress_cb,
                cancel_cb=cancel_cb,
            )
            self._cache[cache_key] = estimate
            return estimate
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def estimate_converted_static_hdr(
        self,
        converted: Path,
        *,
        duration_s: float | None,
        mode: str = PRECISE_MODE,
        progress_cb: Callable[[str], None] | None = None,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> StaticHdrEstimate:
        """Estimate static HDR metadata from an existing P8.1 Annex B stream."""
        normalized_mode = self._normalize_mode(mode)
        if normalized_mode == self.PRECISE_MODE:
            return self._estimate_precise_from_converted(
                converted,
                duration_s=duration_s,
                progress_cb=progress_cb,
                cancel_cb=cancel_cb,
            )
        return self._estimate_fast_from_converted(
            converted,
            duration_s=duration_s,
            progress_cb=progress_cb,
            cancel_cb=cancel_cb,
        )

    @classmethod
    def _normalize_mode(cls, mode: str | None) -> str:
        value = str(mode or cls.PRECISE_MODE).strip().lower()
        return cls.FAST_MODE if value == cls.FAST_MODE else cls.PRECISE_MODE

    def _estimate_fast_from_converted(
        self,
        converted: Path,
        *,
        duration_s: float | None,
        progress_cb: Callable[[str], None] | None = None,
        cancel_cb: Callable[[], bool] | None = None,
        extra_warnings: Iterable[str] = (),
        analysis_method: str = "signalstats",
    ) -> StaticHdrEstimate:
        self._check_cancelled(cancel_cb)
        self._emit(progress_cb, "Analyse HDR10 estimée : échantillonnage luminance rapide…")
        output = self._run_capture(
            self._analysis_cmd(converted, duration_s=duration_s),
            cancel_cb=cancel_cb,
        )
        return self.estimate_from_signalstats_output(
            output,
            mode=self.FAST_MODE,
            analysis_method=analysis_method,
            extra_warnings=tuple(extra_warnings),
            target_samples=self.TARGET_SAMPLES,
        )

    def _estimate_precise_from_converted(
        self,
        converted: Path,
        *,
        duration_s: float | None,
        progress_cb: Callable[[str], None] | None = None,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> StaticHdrEstimate:
        fallback_warnings: list[str] = []
        duration = float(duration_s or 0.0)
        if duration <= 0:
            fallback_warnings.append(
                "Durée indisponible — analyse précise impossible, mode rapide utilisé."
            )
            return self._estimate_fast_from_converted(
                converted,
                duration_s=duration_s,
                progress_cb=progress_cb,
                cancel_cb=cancel_cb,
                extra_warnings=fallback_warnings,
                analysis_method="signalstats_fallback",
            )
        if not self._precise_analysis_supported():
            fallback_warnings.append(
                "FFmpeg ne fournit pas zscale/gbrpf32le/grayf32le — "
                "analyse précise impossible, mode rapide utilisé."
            )
            return self._estimate_fast_from_converted(
                converted,
                duration_s=duration_s,
                progress_cb=progress_cb,
                cancel_cb=cancel_cb,
                extra_warnings=fallback_warnings,
                analysis_method="signalstats_fallback",
            )

        try:
            self._emit(progress_cb, "Analyse HDR10 estimée : repérage précis luminance/crop…")
            discovery = self._run_capture(
                self._discovery_cmd(converted, duration_s=duration),
                cancel_cb=cancel_cb,
            )
            samples = self._parse_signalstats(discovery)
            if not samples:
                raise RuntimeError("aucun échantillon de repérage exploitable")

            guardrail_peak = self._signalstats_guardrail_peak(samples)
            active_crop = self._select_active_crop(discovery)
            candidate_windows = self._candidate_windows(samples, duration)
            frames: list[_FrameLuminance] = []

            self._emit(progress_cb, "Analyse HDR10 estimée : mesure photométrique linéaire…")
            frames.extend(self._analyze_linear_frames(
                self._linear_frames_cmd(
                    converted,
                    duration_s=duration,
                    target_samples=self.PRECISE_UNIFORM_SAMPLES,
                    active_crop=active_crop,
                ),
                width=self.PRECISE_ANALYSIS_WIDTH,
                height=self.PRECISE_ANALYSIS_HEIGHT,
                cancel_cb=cancel_cb,
            ))
            if candidate_windows:
                self._emit(progress_cb, "Analyse HDR10 estimée : passe fine sur pics détectés…")
                frames.extend(self._analyze_linear_frames(
                    self._linear_frames_cmd(
                        converted,
                        duration_s=duration,
                        target_samples=self.PRECISE_FINE_SAMPLES,
                        active_crop=active_crop,
                        windows=candidate_windows,
                    ),
                    width=self.PRECISE_ANALYSIS_WIDTH,
                    height=self.PRECISE_ANALYSIS_HEIGHT,
                    cancel_cb=cancel_cb,
                ))
            if not frames:
                raise RuntimeError("aucune frame linéarisée exploitable")
            return self._estimate_from_linear_luminance(
                frames,
                guardrail_peak_nits=guardrail_peak,
                active_crop=active_crop,
            )
        except RuntimeError as exc:
            if "annulée" in str(exc):
                raise
            fallback_warnings.append(
                f"Analyse précise impossible ({exc}) — mode rapide utilisé."
            )
            return self._estimate_fast_from_converted(
                converted,
                duration_s=duration_s,
                progress_cb=progress_cb,
                cancel_cb=cancel_cb,
                extra_warnings=fallback_warnings,
                analysis_method="signalstats_fallback",
            )

    @classmethod
    def estimate_from_signalstats_output(
        cls,
        output: str,
        *,
        mode: str = FAST_MODE,
        analysis_method: str = "signalstats",
        extra_warnings: Iterable[str] = (),
        target_samples: int | None = None,
    ) -> StaticHdrEstimate:
        pairs = cls._parse_signalstats(output)
        warnings: list[str] = list(extra_warnings)
        if not pairs:
            warnings.append(
                "Aucun échantillon signalstats exploitable — valeurs HDR10 conservatrices appliquées."
            )
            return StaticHdrEstimate(
                master_display=cls.master_display_for_peak(1000),
                max_cll=cls.DEFAULT_MAX_CLL,
                confidence="low",
                sample_count=0,
                coverage=0.0,
                warnings=tuple(warnings),
                mode=mode,
                analysis_method=analysis_method,
                active_sample_count=0,
                ignored_sample_count=0,
            )

        luminance_pairs = [
            (
                cls.pq_code_value_to_nits(sample.ymax, bit_depth=sample.bit_depth),
                cls.pq_code_value_to_nits(sample.yavg, bit_depth=sample.bit_depth),
            )
            for sample in pairs
        ]
        active_pairs = cls._active_luminance_pairs(luminance_pairs)
        ignored_dark = len(luminance_pairs) - len(active_pairs)
        if active_pairs:
            luminance_pairs = active_pairs
            if ignored_dark:
                warnings.append(f"{ignored_dark} échantillon(s) quasi noir(s) ignoré(s).")
        else:
            warnings.append(
                "Aucun échantillon actif détecté — estimation basée sur les frames disponibles."
            )

        peaks = [ymax for ymax, _yavg in luminance_pairs]
        averages = [yavg for _ymax, yavg in luminance_pairs]
        peak_nits = max(peaks) if peaks else 0.0
        fall_nits = max(averages) if averages else 0.0
        max_content = cls.round_hdr_nits(peak_nits)
        max_average = min(max_content, cls.round_hdr_nits(fall_nits))
        sample_count = len(luminance_pairs)
        target = max(1, int(target_samples or cls.TARGET_SAMPLES))
        coverage = min(1.0, sample_count / target)
        confidence = cls.confidence_for_sample_count(sample_count)
        if sample_count < 24:
            warnings.append("Échantillonnage faible : estimation HDR10 peu fiable.")

        return StaticHdrEstimate(
            master_display=cls.master_display_for_peak(max_content),
            max_cll=f"{max_content},{max_average}",
            confidence=confidence,
            sample_count=sample_count,
            coverage=coverage,
            warnings=tuple(warnings),
            mode=mode,
            analysis_method=analysis_method,
            active_sample_count=sample_count,
            ignored_sample_count=ignored_dark,
        )

    @classmethod
    def _estimate_from_linear_luminance(
        cls,
        frames: list[_FrameLuminance],
        *,
        guardrail_peak_nits: float,
        active_crop: str,
    ) -> StaticHdrEstimate:
        warnings: list[str] = []
        active_frames = [
            frame for frame in frames
            if (
                frame.peak_nits >= cls.MIN_ACTIVE_PEAK_NITS
                or frame.average_nits >= cls.MIN_ACTIVE_AVERAGE_NITS
            )
        ]
        ignored = len(frames) - len(active_frames)
        if not active_frames:
            active_frames = frames
            warnings.append(
                "Aucune frame active détectée — estimation précise basée sur toutes les frames."
            )
        elif ignored:
            warnings.append(f"{ignored} frame(s) quasi noire(s) ignorée(s).")

        linear_peak = max((frame.peak_nits for frame in active_frames), default=0.0)
        max_peak = max(linear_peak, guardrail_peak_nits)
        max_fall = max((frame.average_nits for frame in active_frames), default=0.0)
        max_content = cls.round_hdr_nits(max_peak)
        max_average = min(max_content, cls.round_hdr_nits(max_fall))
        active_count = len(active_frames)
        target = cls.PRECISE_UNIFORM_SAMPLES + cls.PRECISE_FINE_SAMPLES
        coverage = min(1.0, active_count / max(1, target))
        confidence = cls.confidence_for_precise_sample_count(active_count, ignored)
        if guardrail_peak_nits > linear_peak * 1.15:
            warnings.append("MaxCLL protégé par garde-fou plein flux signalstats.")
        if active_count < 48:
            warnings.append("Échantillonnage précis faible : confiance réduite.")

        return StaticHdrEstimate(
            master_display=cls.master_display_for_peak(max_content),
            max_cll=f"{max_content},{max_average}",
            confidence=confidence,
            sample_count=active_count,
            coverage=coverage,
            warnings=tuple(warnings),
            mode=cls.PRECISE_MODE,
            analysis_method="linear_grayf32le",
            active_sample_count=active_count,
            ignored_sample_count=ignored,
            active_crop=active_crop,
        )

    @staticmethod
    def confidence_for_precise_sample_count(active_count: int, ignored_count: int) -> str:
        if active_count >= 120 and ignored_count <= active_count:
            return "high"
        if active_count >= 48:
            return "medium"
        return "low"

    @classmethod
    def _signalstats_guardrail_peak(cls, samples: list[_SignalStatsSample]) -> float:
        return max(
            (
                cls.pq_code_value_to_nits(sample.ymax, bit_depth=sample.bit_depth)
                for sample in samples
            ),
            default=0.0,
        )

    def _precise_analysis_supported(self) -> bool:
        if self._precise_support is not None:
            return self._precise_support
        try:
            filters = self._run_capture([self._ffmpeg, "-hide_banner", "-filters"])
            pix_fmts = self._run_capture([self._ffmpeg, "-hide_banner", "-pix_fmts"])
        except RuntimeError:
            self._precise_support = False
        else:
            self._precise_support = bool(
                "zscale" in filters
                and "gbrpf32le" in pix_fmts
                and "grayf32le" in pix_fmts
            )
        return bool(self._precise_support)

    def _discovery_cmd(self, converted: Path, *, duration_s: float | None) -> list[str]:
        filters = self._sampled_time_filters(
            duration_s=duration_s,
            target_samples=self.PRECISE_DISCOVERY_SAMPLES,
        )
        filters.extend([
            "signalstats",
            "cropdetect=limit=24:round=2:reset=0",
            "metadata=print:file=-",
        ])
        return [
            self._ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-i",
            str(converted),
            "-vf",
            ",".join(filters),
            "-an",
            "-f",
            "null",
            "-",
        ]

    def _linear_frames_cmd(
        self,
        converted: Path,
        *,
        duration_s: float,
        target_samples: int,
        active_crop: str = "",
        windows: list[tuple[float, float]] | None = None,
    ) -> list[str]:
        filters: list[str] = []
        if windows:
            total = sum(max(0.0, end - start) for start, end in windows)
            fps = max(0.1, min(24.0, target_samples / max(1.0, total)))
            expr = "+".join(
                f"between(t\\,{start:.3f}\\,{end:.3f})"
                for start, end in windows
                if end > start
            )
            filters.append(f"fps=fps={fps:.8f}")
            filters.append(f"select={expr or '0'}")
            filters.append("setpts=N/FRAME_RATE/TB")
        else:
            filters.extend(self._sampled_time_filters(
                duration_s=duration_s,
                target_samples=target_samples,
            ))
        if active_crop:
            filters.append(f"crop={active_crop}")
        filters.extend([
            (
                "zscale=matrixin=bt2020nc:transferin=smpte2084:primariesin=bt2020:"
                "matrix=gbr:transfer=linear:primaries=bt2020:range=full:npl=10000"
            ),
            "format=gbrpf32le",
            f"scale={self.PRECISE_ANALYSIS_WIDTH}:{self.PRECISE_ANALYSIS_HEIGHT}:flags=area",
            "format=gbrpf32le",
            (
                "colorchannelmixer="
                "rr=0.2627:rg=0.6780:rb=0.0593:"
                "gr=0.2627:gg=0.6780:gb=0.0593:"
                "br=0.2627:bg=0.6780:bb=0.0593"
            ),
            "format=gbrpf32le",
            "extractplanes=r",
            "format=grayf32le",
        ])
        return [
            self._ffmpeg,
            "-hide_banner",
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(converted),
            "-vf",
            ",".join(filters),
            "-an",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "grayf32le",
            "-",
        ]

    def _sampled_time_filters(self, *, duration_s: float | None, target_samples: int) -> list[str]:
        interval = 60.0
        skip_start = 0.0
        skip_end = 0.0
        if duration_s and duration_s > 0:
            skip_start, skip_end = self._analysis_window(duration_s)
            usable = max(1.0, skip_end - skip_start)
            interval = max(1.0 / 24.0, usable / max(1, target_samples))
        fps = 1.0 / interval
        filters: list[str] = []
        if skip_end > skip_start:
            filters.append(f"trim=start={skip_start:.3f}:end={skip_end:.3f}")
        filters.append(f"fps=fps={fps:.8f}")
        return filters

    @classmethod
    def _candidate_windows(
        cls,
        samples: list[_SignalStatsSample],
        duration_s: float,
    ) -> list[tuple[float, float]]:
        ranked = sorted(
            (
                (
                    cls.pq_code_value_to_nits(sample.ymax, bit_depth=sample.bit_depth),
                    sample.pts_time,
                )
                for sample in samples
                if sample.pts_time is not None
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        start_limit, end_limit = cls._analysis_window(duration_s)
        windows: list[tuple[float, float]] = []
        half = cls.PRECISE_FINE_WINDOW_S / 2.0
        for _peak, pts_time in ranked:
            if pts_time is None:
                continue
            start = max(start_limit, pts_time - half)
            end = min(end_limit, pts_time + half)
            if end <= start:
                continue
            if any(abs(pts_time - ((a + b) / 2.0)) < half for a, b in windows):
                continue
            windows.append((start, end))
            if len(windows) >= cls.PRECISE_FINE_CANDIDATES:
                break
        return windows

    @classmethod
    def _select_active_crop(cls, output: str) -> str:
        candidates = cls._parse_crop_candidates(output)
        if len(candidates) < 4:
            return ""
        crop, count = Counter(candidates).most_common(1)[0]
        if count < max(4, int(len(candidates) * 0.55)):
            return ""
        try:
            width, height, _x, _y = [int(part) for part in crop.split(":", 3)]
        except ValueError:
            return ""
        if width < 320 or height < 180:
            return ""
        return crop

    @classmethod
    def _parse_crop_candidates(cls, output: str) -> list[str]:
        candidates = [":".join(match.groups()) for match in cls._CROP_RE.finditer(output or "")]
        current: dict[str, int] = {}
        for line in (output or "").splitlines():
            if line.startswith("frame:"):
                cls._append_crop_candidate(candidates, current)
                current = {}
                continue
            match = cls._CROP_META_RE.search(line)
            if match is None:
                continue
            key, raw_value = match.groups()
            current[key] = int(float(raw_value))
        cls._append_crop_candidate(candidates, current)
        return candidates

    @staticmethod
    def _append_crop_candidate(candidates: list[str], current: dict[str, int]) -> None:
        if not all(key in current for key in ("w", "h", "x", "y")):
            return
        candidates.append(
            f"{current['w']}:{current['h']}:{current['x']}:{current['y']}"
        )

    def _analyze_linear_frames(
        self,
        cmd: list[str],
        *,
        width: int,
        height: int,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> list[_FrameLuminance]:
        frame_size = int(width) * int(height) * 4
        if frame_size <= 0:
            return []
        self._check_cancelled(cancel_cb)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        frames: list[_FrameLuminance] = []
        try:
            assert proc.stdout is not None
            while True:
                self._check_cancelled(cancel_cb, proc=proc)
                chunk = proc.stdout.read(frame_size)
                if not chunk:
                    break
                while len(chunk) < frame_size:
                    more = proc.stdout.read(frame_size - len(chunk))
                    if not more:
                        break
                    chunk += more
                if len(chunk) != frame_size:
                    break
                frames.append(self._linear_frame_luminance_from_bytes(chunk))
        finally:
            if proc.stdout is not None:
                proc.stdout.close()
        stderr_raw = proc.stderr.read() if proc.stderr is not None else b""
        returncode = proc.wait()
        if returncode != 0:
            stderr = stderr_raw.decode("utf-8", errors="replace")
            raise RuntimeError(f"Commande échouée ({returncode}) : {' '.join(cmd)}\n{stderr}")
        return frames

    @staticmethod
    def _linear_frame_luminance_from_bytes(frame: bytes) -> _FrameLuminance:
        values = array("f")
        values.frombytes(frame)
        if sys.byteorder != "little":
            values.byteswap()
        if not values:
            return _FrameLuminance(peak_nits=0.0, average_nits=0.0)
        peak = max(values)
        average = math.fsum(values) / len(values)
        return _FrameLuminance(
            peak_nits=max(0.0, min(10000.0, peak * 10000.0)),
            average_nits=max(0.0, min(10000.0, average * 10000.0)),
        )

    @classmethod
    def _parse_signalstats(cls, output: str) -> list[_SignalStatsSample]:
        current: dict[str, float] = {}
        pairs: list[_SignalStatsSample] = []
        saw_frame_marker = False

        def flush_current() -> None:
            if "YMAX" not in current or "YAVG" not in current:
                return
            bit_depth = current.get("YBITDEPTH")
            pairs.append(_SignalStatsSample(
                ymax=current["YMAX"],
                yavg=current["YAVG"],
                bit_depth=int(bit_depth) if bit_depth else None,
                pts_time=current.get("PTS_TIME"),
            ))

        for line in (output or "").splitlines():
            if line.startswith("frame:"):
                saw_frame_marker = True
                flush_current()
                current = {}
                pts_match = cls._PTS_TIME_RE.search(line)
                if pts_match is not None:
                    current["PTS_TIME"] = float(pts_match.group(1))
                continue
            match = cls._SIGNALSTATS_RE.search(line)
            if match is None:
                continue
            key, raw_value = match.groups()
            current[key] = float(raw_value)
            if not saw_frame_marker and "YMAX" in current and "YAVG" in current:
                flush_current()
                bit_depth = current.get("YBITDEPTH")
                current = {"YBITDEPTH": bit_depth} if bit_depth else {}
        flush_current()
        return pairs

    @classmethod
    def _active_luminance_pairs(cls, pairs: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [
            (peak, average)
            for peak, average in pairs
            if peak >= cls.MIN_ACTIVE_PEAK_NITS or average >= cls.MIN_ACTIVE_AVERAGE_NITS
        ]

    @staticmethod
    def pq_code_value_to_nits(value: float, *, bit_depth: int | None = None) -> float:
        """Convert a signalstats luma code value to approximate PQ nits."""
        try:
            raw = float(value)
        except (TypeError, ValueError):
            return 0.0
        if raw <= 0:
            return 0.0
        if bit_depth:
            scale = 2 ** max(0, int(bit_depth) - 8)
            black = 16.0 * scale
            white = 235.0 * scale
            normalized = (raw - black) / (white - black)
        elif raw <= 255:
            normalized = (raw - 16.0) / (235.0 - 16.0)
        elif raw <= 1023:
            normalized = (raw - 64.0) / (940.0 - 64.0)
        else:
            normalized = (raw - 4096.0) / (60160.0 - 4096.0)
        normalized = max(0.0, min(1.0, normalized))
        return StaticHdrEstimateService._pq_eotf(normalized)

    @staticmethod
    def _pq_eotf(normalized: float) -> float:
        if normalized <= 0:
            return 0.0
        m1 = 2610.0 / 16384.0
        m2 = 2523.0 / 32.0
        c1 = 3424.0 / 4096.0
        c2 = 2413.0 / 128.0
        c3 = 2392.0 / 128.0
        n = max(0.0, min(1.0, normalized))
        p = n ** (1.0 / m2)
        numerator = max(p - c1, 0.0)
        denominator = c2 - c3 * p
        if denominator <= 0:
            return 10000.0
        return 10000.0 * ((numerator / denominator) ** (1.0 / m1))

    @staticmethod
    def round_hdr_nits(value: float) -> int:
        bounded = round(max(1.0, min(10000.0, float(value or 0.0))), 3)
        step = 50 if bounded <= 1000 else 100
        return max(step, int(math.ceil(bounded / step) * step))

    @staticmethod
    def confidence_for_sample_count(sample_count: int) -> str:
        if sample_count >= 72:
            return "high"
        if sample_count >= 24:
            return "medium"
        return "low"

    @staticmethod
    def master_display_for_peak(max_cll: int) -> str:
        peak = int(max_cll or 0)
        if peak <= 1000:
            master_peak = 1000
        elif peak <= 4000:
            master_peak = 4000
        else:
            master_peak = 10000
        return (
            "G(8500,39850)"
            "B(6550,2300)"
            "R(35400,14600)"
            "WP(15635,16450)"
            f"L({master_peak * 10000},1)"
        )

    def _analysis_cmd(self, converted: Path, *, duration_s: float | None) -> list[str]:
        filters = self._sampled_time_filters(
            duration_s=duration_s,
            target_samples=self.TARGET_SAMPLES,
        )
        filters.extend([
            "signalstats",
            "metadata=print:file=-",
        ])
        return [
            self._ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-i",
            str(converted),
            "-vf",
            ",".join(filters),
            "-an",
            "-f",
            "null",
            "-",
        ]

    @classmethod
    def _analysis_window(cls, duration_s: float) -> tuple[float, float]:
        duration = max(0.0, float(duration_s or 0.0))
        if duration <= 0:
            return 0.0, 0.0
        skip = duration * cls.EDGE_SKIP_RATIO
        start = skip
        end = duration - skip
        if end <= start:
            return 0.0, duration
        return start, end

    def _run_capture(
        self,
        cmd: list[str],
        *,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> str:
        self._check_cancelled(cancel_cb)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **subprocess_text_kwargs(),
        )
        while True:
            self._check_cancelled(cancel_cb, proc=proc)
            try:
                stdout, stderr = proc.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                continue
        output = f"{stdout or ''}\n{stderr or ''}"
        if proc.returncode != 0:
            raise RuntimeError(f"Commande échouée ({proc.returncode}) : {' '.join(cmd)}\n{output}")
        return output

    @staticmethod
    def _check_cancelled(
        cancel_cb: Callable[[], bool] | None,
        *,
        proc: subprocess.Popen | None = None,
    ) -> None:
        if cancel_cb is not None and cancel_cb():
            if proc is not None and proc.poll() is None:
                proc.terminate()
                deadline = time.monotonic() + 2.0
                while proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                if proc.poll() is None:
                    proc.kill()
            raise RuntimeError("Analyse HDR10 estimée annulée.")

    @staticmethod
    def _emit(progress_cb: Callable[[str], None] | None, message: str) -> None:
        if progress_cb is not None:
            progress_cb(message)

    @staticmethod
    def _cache_key(
        source: Path,
        stream_index: int,
        duration_s: float | None,
        mode: str,
    ) -> tuple[object, ...]:
        try:
            stat = source.stat()
            return (
                str(source),
                stat.st_size,
                stat.st_mtime_ns,
                int(stream_index),
                round(float(duration_s or 0.0), 3),
                str(mode),
                StaticHdrEstimateService.ESTIMATOR_VERSION,
                "p5_to_p8",
            )
        except OSError:
            return (
                str(source),
                int(stream_index),
                round(float(duration_s or 0.0), 3),
                str(mode),
                StaticHdrEstimateService.ESTIMATOR_VERSION,
                "p5_to_p8",
            )


__all__ = ["StaticHdrEstimate", "StaticHdrEstimateService"]
