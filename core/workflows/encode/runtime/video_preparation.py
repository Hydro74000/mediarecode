"""Video-only command builders and preparation policy helpers."""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.bluray import append_ffmpeg_input_args
from core.runner import TaskCancelledError, TaskSignals
from core.workflows.encode.domain import (
    EncodeCodecDomainCallbacks,
    build_encoder_vf,
    hardware_input_args,
    hdr_meta_args,
    needs_hdr_vui,
    video_codec_args,
    video_codec_args_bitrate,
)
from core.workflows.encode.models import EncodeConfig, QualityMode, VideoEncodeSettings
from core.workflows.encode.runtime_helpers import VideoPreparationResourcePolicy, VideoTrackPrepSpec


@dataclass(frozen=True)
class VideoOnlyCommandBuilderCallbacks:
    ffmpeg_bin: str
    ffmpeg_progress_args: Callable[[], list[str]]
    ffmpeg_thread_args: Callable[[int | None], list[str]]
    offset_input_args: Callable[[int], list[str]]
    codec_domain_callbacks: Callable[[], EncodeCodecDomainCallbacks]
    primary_video_settings: Callable[[EncodeConfig], VideoEncodeSettings]
    video_source_path: Callable[[EncodeConfig], Path]
    video_stream_from_settings: Callable[[VideoEncodeSettings], int]
    size_to_bitrate_kbps: Callable[[EncodeConfig], int]
    size_to_bitrate_kbps_for_video: Callable[[EncodeConfig, VideoEncodeSettings], int]


class VideoOnlyCommandBuilder:
    def __init__(self, callbacks: VideoOnlyCommandBuilderCallbacks) -> None:
        self._cb = callbacks

    def build_video_track_base_cmd(
        self,
        *,
        video: VideoEncodeSettings,
        source: Path,
        stream_index: int,
        offset_ms: int = 0,
        thread_count: int | None = None,
    ) -> list[str]:
        cb = self._cb
        cmd = [cb.ffmpeg_bin, "-hide_banner", "-y"]
        cmd.extend(cb.ffmpeg_progress_args())
        cmd.extend(cb.offset_input_args(offset_ms))
        cmd.extend(hardware_input_args(video, callbacks=cb.codec_domain_callbacks()))
        append_ffmpeg_input_args(cmd, source)
        vf = build_encoder_vf(video, callbacks=cb.codec_domain_callbacks())
        if vf:
            cmd.extend(["-vf", vf])
        cmd.extend(cb.ffmpeg_thread_args(thread_count))
        cmd.extend(["-map", f"0:{int(stream_index)}"])
        return cmd

    def append_video_codec_and_hdr_args(
        self,
        cmd: list[str],
        video: VideoEncodeSettings,
        *,
        bitrate_kbps: int | None = None,
        include_hdr_meta: bool = True,
    ) -> None:
        callbacks = self._cb.codec_domain_callbacks()
        if bitrate_kbps is None:
            cmd.extend(video_codec_args(video, video.bitrate_kbps, callbacks=callbacks))
        else:
            cmd.extend(video_codec_args_bitrate(video, bitrate_kbps, callbacks=callbacks))
        if include_hdr_meta and needs_hdr_vui(video):
            cmd.extend(hdr_meta_args(video))

    def build_multi_video_track_encode_commands(
        self,
        config: EncodeConfig,
        video: VideoEncodeSettings,
        source: Path,
        output_path: Path,
        *,
        offset_ms: int = 0,
        passlog_prefix: Path | None = None,
        thread_count: int | None = None,
        for_preview: bool = False,
    ) -> list[list[str]]:
        _ = for_preview
        cb = self._cb
        stream_index = cb.video_stream_from_settings(video)

        if video.quality_mode == QualityMode.SIZE:
            bitrate = cb.size_to_bitrate_kbps_for_video(config, video)
            pass1 = self.build_video_track_base_cmd(
                video=video,
                source=source,
                stream_index=stream_index,
                offset_ms=offset_ms,
                thread_count=thread_count,
            )
            self.append_video_codec_and_hdr_args(pass1, video, bitrate_kbps=bitrate, include_hdr_meta=False)
            if passlog_prefix is not None:
                pass1.extend(["-passlogfile", str(passlog_prefix)])
            pass1.extend(["-pass", "1", "-an", "-sn", "-dn", "-f", "null", os.devnull])

            pass2 = self.build_video_track_base_cmd(
                video=video,
                source=source,
                stream_index=stream_index,
                offset_ms=offset_ms,
                thread_count=thread_count,
            )
            self.append_video_codec_and_hdr_args(pass2, video, bitrate_kbps=bitrate)
            if passlog_prefix is not None:
                pass2.extend(["-passlogfile", str(passlog_prefix)])
            pass2.extend(["-pass", "2"])
            pass2.extend(["-an", "-sn", "-dn", str(output_path)])
            return [pass1, pass2]

        cmd = self.build_video_track_base_cmd(
            video=video,
            source=source,
            stream_index=stream_index,
            offset_ms=offset_ms,
            thread_count=thread_count,
        )
        self.append_video_codec_and_hdr_args(cmd, video)
        cmd.extend(["-an", "-sn", "-dn", str(output_path)])
        return [cmd]

    def build_video_only_mkv_commands(
        self,
        config: EncodeConfig,
        video: VideoEncodeSettings,
        source: Path,
        output_mkv: Path,
        *,
        offset_ms: int = 0,
        passlog_prefix: Path | None = None,
        thread_count: int | None = None,
    ) -> list[list[str]]:
        """Encode vidéo-seule vers un MKV mono-piste horodaté (lot 3).

        ``-fps_mode passthrough`` préserve les timestamps produits par le
        graphe (CFR comme VFR) : le MKV encodé devient la source de vérité
        temporelle, plus jamais réassociée aux timestamps de la source.
        """
        stream_index = self._cb.video_stream_from_settings(video)
        if video.quality_mode == QualityMode.SIZE:
            bitrate = self._cb.size_to_bitrate_kbps_for_video(config, video)
            pass1 = self.build_video_track_base_cmd(
                video=video, source=source, stream_index=stream_index,
                offset_ms=offset_ms, thread_count=thread_count,
            )
            self.append_video_codec_and_hdr_args(pass1, video, bitrate_kbps=bitrate, include_hdr_meta=False)
            if passlog_prefix is not None:
                pass1.extend(["-passlogfile", str(passlog_prefix)])
            pass1.extend(["-pass", "1", "-fps_mode", "passthrough", "-an", "-sn", "-dn", "-f", "null", os.devnull])

            pass2 = self.build_video_track_base_cmd(
                video=video, source=source, stream_index=stream_index,
                offset_ms=offset_ms, thread_count=thread_count,
            )
            self.append_video_codec_and_hdr_args(pass2, video, bitrate_kbps=bitrate)
            if passlog_prefix is not None:
                pass2.extend(["-passlogfile", str(passlog_prefix)])
            pass2.extend(["-pass", "2", "-fps_mode", "passthrough", "-an", "-sn", "-dn", str(output_mkv)])
            return [pass1, pass2]

        cmd = self.build_video_track_base_cmd(
            video=video, source=source, stream_index=stream_index,
            offset_ms=offset_ms, thread_count=thread_count,
        )
        self.append_video_codec_and_hdr_args(cmd, video)
        cmd.extend(["-fps_mode", "passthrough", "-an", "-sn", "-dn", str(output_mkv)])
        return [cmd]

    def build_video_only_cmd(self, config: EncodeConfig, output_hevc: Path) -> list[str]:
        video = self._cb.primary_video_settings(config)
        return self.build_video_only_cmd_for_track(
            config,
            video,
            self._cb.video_source_path(config),
            output_hevc,
        )

    def build_video_only_cmd_for_track(
        self,
        config: EncodeConfig,
        video: VideoEncodeSettings,
        source: Path,
        output_hevc: Path,
        *,
        offset_ms: int = 0,
        thread_count: int | None = None,
    ) -> list[str]:
        _ = config
        cmd = self.build_video_track_base_cmd(
            video=video,
            source=source,
            stream_index=self._cb.video_stream_from_settings(video),
            offset_ms=offset_ms,
            thread_count=thread_count,
        )
        self.append_video_codec_and_hdr_args(cmd, video)
        cmd.extend(["-fps_mode", "passthrough", "-an", "-f", "hevc", str(output_hevc)])
        return cmd

    def build_video_only_two_pass(self, config: EncodeConfig, output_hevc: Path) -> list[list[str]]:
        video = self._cb.primary_video_settings(config)
        return self.build_video_only_two_pass_for_track(
            config,
            video,
            self._cb.video_source_path(config),
            output_hevc,
            bitrate_kbps=self._cb.size_to_bitrate_kbps(config),
        )

    def build_video_only_two_pass_for_track(
        self,
        config: EncodeConfig,
        video: VideoEncodeSettings,
        source: Path,
        output_hevc: Path,
        *,
        offset_ms: int = 0,
        passlog_prefix: Path | None = None,
        thread_count: int | None = None,
        bitrate_kbps: int | None = None,
    ) -> list[list[str]]:
        bitrate = bitrate_kbps if bitrate_kbps is not None else self._cb.size_to_bitrate_kbps_for_video(config, video)
        stream_index = self._cb.video_stream_from_settings(video)
        pass1 = self.build_video_track_base_cmd(
            video=video,
            source=source,
            stream_index=stream_index,
            offset_ms=offset_ms,
            thread_count=thread_count,
        )
        self.append_video_codec_and_hdr_args(pass1, video, bitrate_kbps=bitrate, include_hdr_meta=False)
        if passlog_prefix is not None:
            pass1.extend(["-passlogfile", str(passlog_prefix)])
        pass1.extend(["-fps_mode", "passthrough", "-pass", "1", "-an", "-f", "null", os.devnull])
        pass2 = self.build_video_track_base_cmd(
            video=video,
            source=source,
            stream_index=stream_index,
            offset_ms=offset_ms,
            thread_count=thread_count,
        )
        self.append_video_codec_and_hdr_args(pass2, video, bitrate_kbps=bitrate)
        if passlog_prefix is not None:
            pass2.extend(["-passlogfile", str(passlog_prefix)])
        pass2.extend(["-fps_mode", "passthrough", "-pass", "2"])
        pass2.extend(["-an", "-f", "hevc", str(output_hevc)])
        return [pass1, pass2]


@dataclass(frozen=True)
class VideoPreparationPolicyCallbacks:
    vaapi_device: Callable[[], str | None]
    ffmpeg_thread_count: Callable[[], int]
    ram_buffer_threshold_pct: int
    total_ram_bytes: Callable[[], int]


class VideoPreparationPolicyService:
    def __init__(self, callbacks: VideoPreparationPolicyCallbacks) -> None:
        self._cb = callbacks

    def resource_policy(self) -> VideoPreparationResourcePolicy:
        return VideoPreparationResourcePolicy(
            vaapi_device=self._cb.vaapi_device(),
            ffmpeg_threads=max(1, int(self._cb.ffmpeg_thread_count() or 1)),
        )

    def video_encode_resource_key(self, video: VideoEncodeSettings) -> str:
        return self.resource_policy().resource_key(video)

    def parallel_video_min_available_ram_bytes(self) -> int:
        total_ram = self._cb.total_ram_bytes()
        if total_ram <= 0 or self._cb.ram_buffer_threshold_pct <= 0:
            return 0
        return int(total_ram * self._cb.ram_buffer_threshold_pct / 100)

    def video_prep_estimated_ram_bytes(self, spec: VideoTrackPrepSpec) -> int:
        source_size = 0
        try:
            if spec.source.exists():
                source_size = max(0, spec.source.stat().st_size)
        except OSError:
            source_size = 0
        return self.resource_policy().estimated_ram_bytes(
            spec.video,
            source_size=source_size,
        )


class TwoPassLogCleanupService:
    @staticmethod
    def log_prefix(work_dir: Path, token: str) -> Path:
        safe_token = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(token)).strip("._")
        if not safe_token:
            safe_token = "video"
        return work_dir / f"ffmpeg2pass-{safe_token}"

    @staticmethod
    def cleanup(cwd: Path | None) -> None:
        base_dir = cwd or Path.cwd()
        for path in base_dir.glob("ffmpeg2pass-*.log*"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def cleanup_for_prefix(prefix: Path) -> None:
        base_dir = prefix.parent
        for path in base_dir.glob(f"{prefix.name}-*.log*"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


@dataclass(frozen=True)
class TwoPassRunnerCallbacks:
    log_info: Callable[[str], None]
    run_cmd: Callable[..., str]
    cleanup_two_pass_logs: Callable[[Path | None], None]


class TwoPassRunner:
    def __init__(self, callbacks: TwoPassRunnerCallbacks) -> None:
        self._cb = callbacks

    def run(
        self,
        cmds: list[list[str]],
        cwd: Path | None,
        signals: TaskSignals | None = None,
    ) -> TaskSignals:
        if signals is None:
            signals = TaskSignals()
        executor = ThreadPoolExecutor(max_workers=1)

        def _task() -> None:
            cleaned_passlogs = False

            def _cleanup_passlogs() -> None:
                nonlocal cleaned_passlogs
                if cleaned_passlogs:
                    return
                cleaned_passlogs = True
                self._cb.cleanup_two_pass_logs(cwd)

            try:
                self._cb.log_info("Passe 1/2 (analyse)…")
                self._cb.run_cmd(
                    cmds[0],
                    cwd=cwd,
                    label="ffmpeg-pass1",
                    progress_cb=lambda line: signals.progress.emit(line),
                    signals=signals,
                )
                self._cb.log_info("Passe 2/2 (encodage)…")
                output = self._cb.run_cmd(
                    cmds[1],
                    cwd=cwd,
                    label="ffmpeg-pass2",
                    progress_cb=lambda line: signals.progress.emit(line),
                    signals=signals,
                )
                _cleanup_passlogs()
                signals.finished.emit(output)
            except TaskCancelledError:
                _cleanup_passlogs()
                signals.cancelled.emit()
            except Exception as exc:
                _cleanup_passlogs()
                signals.failed.emit(str(exc), exc)
            finally:
                _cleanup_passlogs()

        executor.submit(_task)
        executor.shutdown(wait=False)
        return signals
