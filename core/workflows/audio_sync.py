"""Audio fingerprint sync workflow for explicit remux-panel alignment."""

from __future__ import annotations

import json
import math
import subprocess
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.bluray import append_ffmpeg_input_args, ffprobe_input_args
from core.subprocess_utils import subprocess_text_kwargs, subprocess_windows_no_window_kwargs


class AudioSyncError(RuntimeError):
    """Raised when enhanced audio synchronization cannot compute a reliable delta."""


@dataclass(frozen=True)
class AudioSyncTrack:
    source_path: Path
    stream_index: int


@dataclass(frozen=True)
class AudioProbe:
    channels: int
    channel_layout: str
    duration_s: float | None = None


@dataclass(frozen=True)
class AudioSyncResult:
    offset_ms: int
    confidence: float
    lag_ms: int


@dataclass(frozen=True)
class _MarkerSignature:
    name: str
    values: list[float]
    marker_count: int
    prominence: float
    marker_positions: tuple[int, ...] = ()


@dataclass(frozen=True)
class _LagSearchResult:
    lag_steps: int
    score: float
    margin: float


@dataclass(frozen=True)
class _StereoCandidate:
    rank: float
    name: str
    lag: _LagSearchResult
    matched_markers: int
    marker_ratio: float


class AudioSyncWorkflow:
    """
    Explicit audio-content synchronization.

    For surround tracks it extracts a low-rate mono signature from
    LFE/surround-ish channels, then correlates target against a chosen
    reference. Stereo tracks are only accepted against another stereo track and
    use sparse transient markers on the same side (left/left, right/right, then
    shared stereo if the markers are strong enough).

    The returned offset follows the existing TrackEntry.time_shift_ms
    convention: positive delays target, negative advances target.
    """

    _STEREO_MIN_CONFIDENCE = 0.55
    _STEREO_MIN_MARKERS = 3
    _STEREO_MIN_MATCHED_MARKERS = 3
    _STEREO_MIN_MARGIN = 0.050
    _STEREO_STEPS_PER_SECOND = 20
    _STEREO_MIN_MARKER_SEPARATION_STEPS = 8

    def __init__(
        self,
        *,
        ffmpeg_bin: str = "ffmpeg",
        ffprobe_bin: str = "ffprobe",
        log_cb: Callable[[str, str], None] | None = None,
        sample_rate: int = 8000,
        analysis_seconds: int = 900,
        max_offset_seconds: int = 120,
    ) -> None:
        self._ffmpeg = ffmpeg_bin
        self._ffprobe = ffprobe_bin
        self._log = log_cb or (lambda _level, _message: None)
        self._sample_rate = int(sample_rate)
        self._analysis_seconds = int(analysis_seconds)
        self._max_offset_seconds = int(max_offset_seconds)

    def detect_offset(self, reference: AudioSyncTrack, target: AudioSyncTrack) -> AudioSyncResult:
        ref_probe = self._probe(reference)
        target_probe = self._probe(target)
        analysis_mode = self._analysis_mode(ref_probe, target_probe)

        hop_ms = 100
        if analysis_mode == "stereo":
            lag_steps, confidence = self._detect_stereo_lag(reference, target)
            hop_ms = int(round(1000 / self._STEREO_STEPS_PER_SECOND))
            if confidence < self._STEREO_MIN_CONFIDENCE:
                raise AudioSyncError(f"corrélation audio stéréo insuffisante ({confidence:.2f})")
        else:
            ref_signature = self._signature(reference, ref_probe)
            target_signature = self._signature(target, target_probe)
            if len(ref_signature) < 20 or len(target_signature) < 20:
                raise AudioSyncError("signature audio trop courte")

            lag_steps, confidence = self._best_lag(ref_signature, target_signature)
            if confidence < 0.35:
                raise AudioSyncError(f"corrélation audio insuffisante ({confidence:.2f})")

        lag_ms = int(round(lag_steps * hop_ms))
        return AudioSyncResult(
            offset_ms=-lag_ms,
            confidence=confidence,
            lag_ms=lag_ms,
        )

    def _probe(self, track: AudioSyncTrack) -> AudioProbe:
        cmd = [
            self._ffprobe,
            "-v", "error",
            "-select_streams", f"a:{self._audio_stream_ordinal(track)}",
            "-show_entries", "stream=channels,channel_layout,duration",
            "-of", "json",
        ]
        cmd.extend(ffprobe_input_args(track.source_path))
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=30,
            **subprocess_text_kwargs(),
        )
        if result.returncode != 0:
            raise AudioSyncError((result.stderr or "").strip() or "ffprobe audio échoué")
        try:
            payload = json.loads(result.stdout or "{}")
            stream = (payload.get("streams") or [])[0]
        except Exception as exc:
            raise AudioSyncError("réponse ffprobe audio invalide") from exc
        channels = int(stream.get("channels") or 0)
        duration_raw = stream.get("duration")
        try:
            duration_s = float(duration_raw) if duration_raw not in (None, "") else None
        except (TypeError, ValueError):
            duration_s = None
        return AudioProbe(
            channels=channels,
            channel_layout=str(stream.get("channel_layout") or ""),
            duration_s=duration_s,
        )

    def _audio_stream_ordinal(self, track: AudioSyncTrack) -> int:
        cmd = [
            self._ffprobe,
            "-v", "error",
            "-show_entries", "stream=index,codec_type",
            "-of", "json",
        ]
        cmd.extend(ffprobe_input_args(track.source_path))
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=30,
            **subprocess_text_kwargs(),
        )
        if result.returncode != 0:
            raise AudioSyncError((result.stderr or "").strip() or "ffprobe streams échoué")
        payload = json.loads(result.stdout or "{}")
        audio_ord = 0
        for stream in payload.get("streams") or []:
            if stream.get("codec_type") != "audio":
                continue
            if int(stream.get("index", -1)) == int(track.stream_index):
                return audio_ord
            audio_ord += 1
        raise AudioSyncError(f"piste audio introuvable: stream={track.stream_index}")

    @classmethod
    def _ensure_compatible(cls, probe: AudioProbe, label: str) -> None:
        if cls._is_surround_probe(probe) or cls._is_stereo_probe(probe):
            return
        raise AudioSyncError(f"piste {label} non compatible: ni 5.1+ ni stéréo")

    @classmethod
    def _analysis_mode(cls, reference: AudioProbe, target: AudioProbe) -> str:
        cls._ensure_compatible(reference, "reference")
        cls._ensure_compatible(target, "target")
        if cls._is_surround_probe(reference) and cls._is_surround_probe(target):
            return "surround"
        if cls._is_stereo_probe(reference) and cls._is_stereo_probe(target):
            return "stereo"
        raise AudioSyncError(
            "pistes audio non compatibles: la synchronisation stéréo nécessite deux pistes stéréo"
        )

    @staticmethod
    def _is_surround_probe(probe: AudioProbe) -> bool:
        layout = probe.channel_layout.lower()
        if probe.channels >= 6 or "5.1" in layout or "7.1" in layout:
            return True
        return False

    @staticmethod
    def _is_stereo_probe(probe: AudioProbe) -> bool:
        layout = probe.channel_layout.lower()
        return probe.channels == 2 or "stereo" in layout or "2.0" in layout

    def _signature(self, track: AudioSyncTrack, probe: AudioProbe) -> list[float]:
        channel_indices = self._focus_channel_indices(probe.channels)
        pan_terms = "+".join(
            f"{1.0 / len(channel_indices):.6f}*c{idx}"
            for idx in channel_indices
        )
        audio_filter = f"pan=mono|c0={pan_terms},highpass=f=25"
        samples = self._extract_samples(
            track,
            audio_filter=audio_filter,
            channels=1,
        )
        return self._envelope(samples)

    def _extract_samples(
        self,
        track: AudioSyncTrack,
        *,
        audio_filter: str,
        channels: int,
    ) -> array:
        cmd = [
            self._ffmpeg,
            "-hide_banner", "-loglevel", "error",
        ]
        append_ffmpeg_input_args(cmd, track.source_path)
        cmd.extend([
            "-map", f"0:{int(track.stream_index)}",
            "-vn", "-sn", "-dn",
            "-t", str(self._analysis_seconds),
        ])
        if audio_filter:
            cmd.extend(["-af", audio_filter])
        cmd.extend([
            "-ar", str(self._sample_rate),
            "-ac", str(int(channels)),
            "-f", "s16le",
            "pipe:1",
        ])
        self._log("INFO", "$ " + " ".join(str(part) for part in cmd))
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=max(120, self._analysis_seconds * 3),
            **subprocess_windows_no_window_kwargs(),
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip() if isinstance(result.stderr, bytes) else str(result.stderr or "")
            raise AudioSyncError(stderr or "extraction signature audio échouée")
        samples = array("h")
        samples.frombytes(result.stdout)
        if not samples:
            raise AudioSyncError("signature audio vide")
        return samples

    @staticmethod
    def _focus_channel_indices(channels: int) -> list[int]:
        if channels >= 8:
            return [3, 4, 5, 6, 7]
        return [3, 4, 5]

    def _envelope(self, samples: array) -> list[float]:
        window = max(1, self._sample_rate // 10)
        values: list[float] = []
        previous = 0.0
        for start in range(0, len(samples) - window + 1, window):
            chunk = samples[start:start + window]
            energy = math.sqrt(sum(float(s) * float(s) for s in chunk) / len(chunk))
            novelty = max(0.0, energy - previous)
            values.append(novelty)
            previous = energy
        return self._normalize(values)

    @staticmethod
    def _normalize(values: list[float]) -> list[float]:
        if not values:
            return []
        mean = sum(values) / len(values)
        centered = [value - mean for value in values]
        variance = sum(value * value for value in centered) / len(centered)
        scale = math.sqrt(variance) or 1.0
        return [value / scale for value in centered]

    def _best_lag(self, reference: list[float], target: list[float]) -> tuple[int, float]:
        result = self._best_lag_details(reference, target)
        return result.lag_steps, result.score

    def _best_lag_details(
        self,
        reference: list[float],
        target: list[float],
        *,
        steps_per_second: int = 10,
    ) -> _LagSearchResult:
        max_lag = min(
            self._max_offset_seconds * int(steps_per_second),
            len(reference) - 5,
            len(target) - 5,
        )
        if max_lag <= 0:
            raise AudioSyncError("fenêtre de corrélation audio insuffisante")

        min_overlap = max(20, int(min(len(reference), len(target)) * 0.5))
        scores: list[tuple[int, float]] = []
        for lag in range(-max_lag, max_lag + 1):
            if lag >= 0:
                ref_slice = reference[:len(reference) - lag]
                target_slice = target[lag:lag + len(ref_slice)]
            else:
                target_slice = target[:len(target) + lag]
                ref_slice = reference[-lag:-lag + len(target_slice)]
            if len(ref_slice) < min_overlap or len(target_slice) < min_overlap:
                continue
            denom = math.sqrt(sum(v * v for v in ref_slice) * sum(v * v for v in target_slice))
            if denom <= 0:
                continue
            score = sum(a * b for a, b in zip(ref_slice, target_slice)) / denom
            scores.append((lag, score))

        if not scores:
            raise AudioSyncError("corrélation audio impossible")

        best_lag, best_score = max(scores, key=lambda item: item[1])
        second_scores = [
            score
            for lag, score in scores
            if abs(lag - best_lag) > 2
        ]
        second_best = max(second_scores) if second_scores else -1.0

        if best_score < -0.5:
            raise AudioSyncError("corrélation audio impossible")
        return _LagSearchResult(
            lag_steps=best_lag,
            score=float(best_score),
            margin=float(best_score - second_best),
        )

    def _detect_stereo_lag(
        self,
        reference: AudioSyncTrack,
        target: AudioSyncTrack,
    ) -> tuple[int, float]:
        reference_signatures = {
            signature.name: signature
            for signature in self._stereo_signatures(reference)
        }
        target_signatures = {
            signature.name: signature
            for signature in self._stereo_signatures(target)
        }

        candidates: list[_StereoCandidate] = []
        for name, side_bonus in (("left", 0.035), ("right", 0.035), ("both", 0.0)):
            ref_signature = reference_signatures.get(name)
            target_signature = target_signatures.get(name)
            if ref_signature is None or target_signature is None:
                continue
            try:
                lag = self._best_lag_details(
                    ref_signature.values,
                    target_signature.values,
                    steps_per_second=self._STEREO_STEPS_PER_SECOND,
                )
            except AudioSyncError:
                continue
            matched = self._matched_marker_count(ref_signature, target_signature, lag.lag_steps)
            marker_floor = min(ref_signature.marker_count, target_signature.marker_count)
            marker_ratio = matched / max(1, marker_floor)
            if matched < self._STEREO_MIN_MATCHED_MARKERS:
                continue
            if marker_floor >= 8 and marker_ratio < 0.30:
                continue

            marker_quality = min(ref_signature.prominence, target_signature.prominence, 10.0)
            rank = (
                lag.score +
                side_bonus +
                min(matched, 10) * 0.006 +
                marker_ratio * 0.035 +
                marker_quality * 0.004
            )
            candidates.append(_StereoCandidate(
                rank=rank,
                name=name,
                lag=lag,
                matched_markers=matched,
                marker_ratio=marker_ratio,
            ))

        if not candidates:
            raise AudioSyncError("aucun gros marqueur stéréo fiable trouvé")

        candidates.sort(key=lambda item: item.rank, reverse=True)
        best = candidates[0]
        for challenger in candidates[1:]:
            if abs(challenger.lag.lag_steps - best.lag.lag_steps) <= 2:
                continue
            if challenger.rank >= best.rank - 0.045:
                raise AudioSyncError("marqueurs stéréo trop ambigus")

        name = best.name
        lag = best.lag
        if lag.score < self._STEREO_MIN_CONFIDENCE:
            raise AudioSyncError(f"corrélation audio stéréo insuffisante ({lag.score:.2f})")
        if lag.margin < self._STEREO_MIN_MARGIN:
            raise AudioSyncError("marqueurs stéréo trop ambigus")

        label = {
            "left": "gauche",
            "right": "droite",
            "both": "gauche+droite",
        }.get(name, name)
        self._log(
            "INFO",
            (
                "Analyse stéréo: canal {label}, {markers} gros marqueurs alignés, "
                "confiance {confidence:.2f}."
            ).format(label=label, markers=best.matched_markers, confidence=lag.score),
        )
        return lag.lag_steps, lag.score

    def _stereo_signatures(self, track: AudioSyncTrack) -> list[_MarkerSignature]:
        samples = self._extract_samples(
            track,
            audio_filter="highpass=f=120",
            channels=2,
        )
        frame_count = len(samples) // 2
        if frame_count <= 0:
            raise AudioSyncError("signature audio stéréo vide")
        if len(samples) != frame_count * 2:
            samples = samples[:frame_count * 2]

        left_samples = samples[0::2]
        right_samples = samples[1::2]
        left = self._transient_envelope(left_samples)
        right = self._transient_envelope(right_samples)
        shared = [(a + b) * 0.5 for a, b in zip(left, right)]

        candidates = [
            self._marker_signature("left", left, companion=right),
            self._marker_signature("right", right, companion=left),
            self._marker_signature("both", shared, threshold_boost=1.35),
        ]
        kept = [
            signature
            for signature in candidates
            if signature.marker_count >= self._STEREO_MIN_MARKERS
        ]
        if not kept:
            raise AudioSyncError("pas assez de gros marqueurs stéréo")
        return kept

    def _transient_envelope(self, samples: array) -> list[float]:
        window = max(1, self._sample_rate // self._STEREO_STEPS_PER_SECOND)
        rms_values: list[float] = []
        peak_values: list[float] = []
        previous_energy = 0.0
        for start in range(0, len(samples) - window + 1, window):
            chunk = samples[start:start + window]
            if not chunk:
                continue
            energy = math.sqrt(sum(float(s) * float(s) for s in chunk) / len(chunk))
            peak = float(max(abs(int(s)) for s in chunk))
            rms_values.append(energy)
            peak_values.append(peak)

        values: list[float] = []
        for idx, (energy, peak) in enumerate(zip(rms_values, peak_values)):
            previous = rms_values[max(0, idx - 8):idx]
            previous_floor = self._percentile(previous, 0.50) if previous else previous_energy
            attack = max(0.0, energy - previous_floor)
            crest = peak / max(energy, 1.0)
            transient = max(attack * 1.40, peak * 0.28)
            if crest < 2.2 and attack < max(previous_floor * 1.20, 400.0):
                transient *= 0.35
            values.append(transient)
            previous_energy = energy
        return values

    def _marker_signature(
        self,
        name: str,
        values: list[float],
        *,
        companion: list[float] | None = None,
        threshold_boost: float = 1.0,
    ) -> _MarkerSignature:
        if len(values) < 20:
            return _MarkerSignature(
                name=name,
                values=list(values),
                marker_count=0,
                prominence=0.0,
            )

        threshold = self._marker_threshold(values) * max(1.0, threshold_boost)
        strong_threshold = threshold * 1.45
        max_markers = max(12, min(96, len(values) // 45))
        raw_candidates: list[tuple[int, float]] = []
        for idx, value in enumerate(values):
            if value < threshold or not self._is_local_peak(values, idx):
                continue
            local_floor = self._local_marker_floor(values, idx)
            if value < max(threshold, local_floor * 2.25):
                continue
            weight = value
            if companion is not None:
                other = companion[idx] if idx < len(companion) else 0.0
                ratio = value / max(other, 1e-9)
                if ratio >= 1.35:
                    weight = value
                elif value >= strong_threshold:
                    weight = value * 0.72
                else:
                    continue
            raw_candidates.append((idx, weight))

        candidates = self._select_spaced_markers(raw_candidates, max_markers=max_markers)

        signature = [0.0] * len(values)
        marker_positions: list[int] = []
        for idx, weight in candidates:
            scaled = min(weight / max(threshold, 1e-9), 12.0)
            signature[idx] = max(signature[idx], scaled)
            marker_positions.append(idx)
            if idx > 0:
                signature[idx - 1] = max(signature[idx - 1], scaled * 0.45)
            if idx + 1 < len(signature):
                signature[idx + 1] = max(signature[idx + 1], scaled * 0.45)

        prominence = (
            sum(weight / max(threshold, 1e-9) for _idx, weight in candidates) / len(candidates)
            if candidates else
            0.0
        )
        return _MarkerSignature(
            name=name,
            values=signature,
            marker_count=len(candidates),
            prominence=float(prominence),
            marker_positions=tuple(marker_positions),
        )

    @classmethod
    def _marker_threshold(cls, values: list[float]) -> float:
        positive = [value for value in values if value > 0]
        if not positive:
            return 1.0
        median = cls._percentile(positive, 0.50)
        q90 = cls._percentile(positive, 0.90)
        q975 = cls._percentile(positive, 0.975)
        deviations = [abs(value - median) for value in positive]
        mad = cls._percentile(deviations, 0.50) or 1.0
        return max(q975, median + 8.0 * mad, q90 * 2.0, 1.0)

    @classmethod
    def _local_marker_floor(cls, values: list[float], idx: int) -> float:
        radius = 80
        start = max(0, idx - radius)
        end = min(len(values), idx + radius + 1)
        local = [
            value
            for local_idx, value in enumerate(values[start:end], start=start)
            if abs(local_idx - idx) > 2
        ]
        return cls._percentile(local, 0.85) if local else 0.0

    @classmethod
    def _select_spaced_markers(
        cls,
        candidates: list[tuple[int, float]],
        *,
        max_markers: int,
    ) -> list[tuple[int, float]]:
        selected: list[tuple[int, float]] = []
        for idx, weight in sorted(candidates, key=lambda item: item[1], reverse=True):
            if any(
                abs(idx - selected_idx) < cls._STEREO_MIN_MARKER_SEPARATION_STEPS
                for selected_idx, _selected_weight in selected
            ):
                continue
            selected.append((idx, weight))
            if len(selected) >= max_markers:
                break
        return sorted(selected, key=lambda item: item[0])

    @staticmethod
    def _percentile(values: list[float], ratio: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        pos = max(0.0, min(1.0, ratio)) * (len(ordered) - 1)
        low = int(math.floor(pos))
        high = int(math.ceil(pos))
        if low == high:
            return float(ordered[low])
        weight = pos - low
        return float(ordered[low] * (1.0 - weight) + ordered[high] * weight)

    @staticmethod
    def _is_local_peak(values: list[float], idx: int) -> bool:
        value = values[idx]
        left = values[idx - 1] if idx > 0 else -1.0
        right = values[idx + 1] if idx + 1 < len(values) else -1.0
        return value >= left and value > right

    @staticmethod
    def _matched_marker_windows(reference: list[float], target: list[float], lag: int) -> int:
        if lag >= 0:
            ref_slice = reference[:len(reference) - lag]
            target_slice = target[lag:lag + len(ref_slice)]
        else:
            target_slice = target[:len(target) + lag]
            ref_slice = reference[-lag:-lag + len(target_slice)]
        return sum(
            1
            for ref_value, target_value in zip(ref_slice, target_slice)
            if ref_value > 0 and target_value > 0
        )

    @classmethod
    def _matched_marker_count(
        cls,
        reference: _MarkerSignature,
        target: _MarkerSignature,
        lag: int,
    ) -> int:
        if not reference.marker_positions or not target.marker_positions:
            return cls._matched_marker_windows(reference.values, target.values, lag)
        target_positions = set(target.marker_positions)
        matched = 0
        for idx in reference.marker_positions:
            target_idx = idx + lag
            if any((target_idx + delta) in target_positions for delta in (-1, 0, 1)):
                matched += 1
        return matched


__all__ = [
    "AudioProbe",
    "AudioSyncError",
    "AudioSyncResult",
    "AudioSyncTrack",
    "AudioSyncWorkflow",
]
