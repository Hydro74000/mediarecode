#!/usr/bin/env python3
"""Reusable MKV / HEVC / Dolby Vision audit tool."""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from core.workflows.encode.catalog import (
    needs_static_hdr_bitstream_patch_codec,
    static_hdr_metadata_mode,
)
from core.matroska.editors.dovi import (
    MatroskaDoviBlockAdditionEditor,
)


_START_CODE_3 = b"\x00\x00\x01"
_VCL_RANGE = range(0, 32)
_IRAP_TYPES = frozenset({16, 17, 18, 19, 20, 21})
_PREFIX_BOUNDARY_TYPES = frozenset({32, 33, 34, 35, 39, 62, 63})
_SEI_PREFIX = 39
_SEI_SUFFIX = 40
_SEI_PIC_TIMING = 1
_SEI_MASTERING_DISPLAY = 137
_SEI_CONTENT_LIGHT = 144
_SEI_USER_DATA_REGISTERED_ITU_T_T35 = 4
_NAL_TYPE_NAMES = {
    0: "TRAIL_N",
    1: "TRAIL_R",
    2: "TSA_N",
    3: "TSA_R",
    4: "STSA_N",
    5: "STSA_R",
    6: "RADL_N",
    7: "RADL_R",
    8: "RASL_N",
    9: "RASL_R",
    16: "BLA_W_LP",
    17: "BLA_W_RADL",
    18: "BLA_N_LP",
    19: "IDR_W_RADL",
    20: "IDR_N_LP",
    21: "CRA_NUT",
    32: "VPS",
    33: "SPS",
    34: "PPS",
    35: "AUD",
    36: "EOS",
    37: "EOB",
    38: "FD",
    39: "PREFIX_SEI",
    40: "SUFFIX_SEI",
    41: "RSV_NVCL41",
    42: "RSV_NVCL42",
    43: "RSV_NVCL43",
    44: "RSV_NVCL44",
    45: "RSV_NVCL45",
    46: "RSV_NVCL46",
    47: "RSV_NVCL47",
    48: "UNSPEC48",
    49: "UNSPEC49",
    50: "UNSPEC50",
    51: "UNSPEC51",
    52: "UNSPEC52",
    53: "UNSPEC53",
    54: "UNSPEC54",
    55: "UNSPEC55",
    56: "UNSPEC56",
    57: "UNSPEC57",
    58: "UNSPEC58",
    59: "UNSPEC59",
    60: "UNSPEC60",
    61: "UNSPEC61",
    62: "UNSPEC62_DOVI_RPU",
    63: "UNSPEC63",
}
_SCENE_COUNT_RE = re.compile(r"Scene/shot count:\s*(\d+)")
_RPU_FRAMES_RE = re.compile(r"Frames:\s*(\d+)")
_RPU_PROFILE_RE = re.compile(r"Profile:\s*(\d+)")
_SEI_TYPE_NAMES = {
    _SEI_PIC_TIMING: "pic_timing",
    _SEI_USER_DATA_REGISTERED_ITU_T_T35: "user_data_registered_itu_t_t35",
    _SEI_MASTERING_DISPLAY: "mastering_display_colour_volume",
    _SEI_CONTENT_LIGHT: "content_light_level",
}


@dataclass
class Finding:
    severity: str
    message: str


@dataclass
class PacketAudit:
    packet_count: int = 0
    keyframe_packets: int = 0
    missing_pts: int = 0
    missing_dts: int = 0
    non_monotonic_pts: int = 0
    non_monotonic_dts: int = 0
    duplicate_pts: int = 0
    duplicate_dts: int = 0
    unique_duration_count: int = 0
    duration_samples: list[dict[str, Any]] = field(default_factory=list)
    first_pts: float | None = None
    last_pts: float | None = None
    min_pts_dts_delta: float | None = None
    max_pts_dts_delta: float | None = None
    average_bitrate_mbps: float | None = None
    peak_1s_bitrate_mbps: float | None = None
    peak_5s_bitrate_mbps: float | None = None
    max_packet_size: int = 0
    average_packet_size: float | None = None
    likely_bframe_reordering: bool = False


@dataclass
class DoviBlockMappingAudit:
    video_track_number: int | None = None
    codec_id: str = ""
    has_dovi_block_addition: bool = False
    dvcc_present_anywhere: bool = False
    dvvc_present_anywhere: bool = False


@dataclass
class HdrStaticSummary:
    unique_mastering_display: list[str] = field(default_factory=list)
    unique_content_light: list[str] = field(default_factory=list)


@dataclass
class HevcAudit:
    access_units: int = 0
    key_access_units: int = 0
    rpu_nal_count: int = 0
    access_units_with_rpu: int = 0
    access_units_with_hdr10plus: int = 0
    access_units_with_mdcv: int = 0
    access_units_with_cll: int = 0
    access_units_with_aud: int = 0
    key_access_units_with_param_sets: int = 0
    first_access_unit_has_vps: bool = False
    first_access_unit_has_sps: bool = False
    first_access_unit_has_pps: bool = False
    trail_n_nals: int = 0
    trail_r_nals: int = 0
    access_units_with_pic_timing: int = 0
    nal_type_counts: dict[str, int] = field(default_factory=dict)
    prefix_sei_payload_type_counts: dict[str, int] = field(default_factory=dict)
    static_hdr: HdrStaticSummary = field(default_factory=HdrStaticSummary)
    max_access_unit_size: int = 0
    average_access_unit_size: float | None = None


@dataclass
class DoviAudit:
    ffprobe_record: dict[str, Any] = field(default_factory=dict)
    block_mapping: DoviBlockMappingAudit = field(default_factory=DoviBlockMappingAudit)
    rpu_summary_text: str = ""
    rpu_frames: int | None = None
    rpu_profile: int | None = None
    scene_count: int | None = None


@dataclass
class Hdr10PlusAudit:
    verify_ok: bool = False
    verify_output: str = ""


@dataclass
class FilenameAudit:
    claims_truehd: bool = False
    claims_atmos: bool = False
    claims_7_1: bool = False
    claims_dv: bool = False
    claims_hdr10plus: bool = False


@dataclass
class HdrModeAudit:
    has_dovi: bool = False
    has_hdr10plus: bool = False
    has_hdr10: bool = False
    label: str = "SDR"


@dataclass
class WorkflowAudit:
    workflow_codec: str = "hevc_nvenc"
    detected_mode: HdrModeAudit = field(default_factory=HdrModeAudit)
    static_hdr_mode: str = "none"
    metadata_inject_required: bool = False
    expected_steps: list[str] = field(default_factory=list)
    observed_checks: dict[str, Any] = field(default_factory=dict)
    overall_consistent: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class AuditReport:
    input_path: str
    container: dict[str, Any]
    packet_audit: PacketAudit
    hevc_audit: HevcAudit
    dovi_audit: DoviAudit
    hdr10plus_audit: Hdr10PlusAudit
    filename_audit: FilenameAudit
    workflow_audit: WorkflowAudit
    findings: list[Finding]


class ToolError(RuntimeError):
    """Raised when an external tool fails."""


class ToolRunner:
    def __init__(
        self,
        *,
        ffprobe_bin: str,
        ffmpeg_bin: str,
        dovi_tool_bin: str,
        hdr10plus_tool_bin: str,
    ) -> None:
        self.ffprobe_bin = ffprobe_bin
        self.ffmpeg_bin = ffmpeg_bin
        self.dovi_tool_bin = dovi_tool_bin
        self.hdr10plus_tool_bin = hdr10plus_tool_bin

    def run_text(self, args: list[str]) -> str:
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
            stderr = (result.stderr or "").strip()
            raise ToolError(
                f"Command failed with rc={result.returncode}: {' '.join(args)}\n{stderr}"
            )
        return result.stdout

    def run_json(self, args: list[str]) -> dict[str, Any]:
        raw = self.run_text(args)
        try:
            return json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise ToolError(f"Invalid JSON from {' '.join(args)}: {exc}") from exc

    def popen(self, args: list[str]) -> subprocess.Popen[bytes]:
        try:
            return subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise ToolError(f"Unable to run {' '.join(args)}: {exc}") from exc


def _extract_dovi_rpu_with_fallback(
    runner: ToolRunner,
    *,
    input_path: Path,
    output_path: Path,
) -> tuple[str, bool]:
    direct_args = [
        runner.dovi_tool_bin,
        "extract-rpu",
        str(input_path),
        "-o",
        str(output_path),
    ]
    try:
        stdout = runner.run_text(direct_args)
        return stdout, False
    except ToolError as direct_exc:
        ffmpeg_cmd = [
            runner.ffmpeg_bin,
            "-v",
            "error",
            "-i",
            str(input_path),
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
        dovi_cmd = [
            runner.dovi_tool_bin,
            "extract-rpu",
            "-",
            "-o",
            str(output_path),
        ]
        try:
            ffmpeg_proc = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert ffmpeg_proc.stdout is not None
            dovi_proc = subprocess.Popen(
                dovi_cmd,
                stdin=ffmpeg_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise ToolError(f"Unable to run fallback extraction pipeline: {exc}") from exc

        ffmpeg_proc.stdout.close()
        dovi_stdout, dovi_stderr = dovi_proc.communicate()
        ffmpeg_stderr = ffmpeg_proc.stderr.read().decode("utf-8", errors="replace") if ffmpeg_proc.stderr else ""
        ffmpeg_rc = ffmpeg_proc.wait()
        if dovi_proc.returncode != 0 or ffmpeg_rc != 0:
            raise ToolError(
                "Direct DOVI extraction failed and fallback annex-b extraction failed.\n"
                f"Direct error: {direct_exc}\n"
                f"ffmpeg rc={ffmpeg_rc}: {ffmpeg_stderr.strip()}\n"
                f"dovi_tool rc={dovi_proc.returncode}: {(dovi_stderr or '').strip()}"
            )
        return dovi_stdout or "", True


def _compact_float(value: float | None, digits: int = 3) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def _find_start_code(data: bytearray, offset: int) -> tuple[int, int] | None:
    pos = data.find(_START_CODE_3, offset)
    while pos != -1:
        if pos > 0 and data[pos - 1] == 0x00:
            return pos - 1, 4
        return pos, 3
    return None


def _remove_emulation_prevention_bytes(data: bytes) -> bytes:
    out = bytearray()
    zero_count = 0
    for byte in data:
        if zero_count >= 2 and byte == 0x03:
            zero_count = 0
            continue
        out.append(byte)
        if byte == 0:
            zero_count += 1
        else:
            zero_count = 0
    return bytes(out)


def _parse_mastering_display(payload: bytes) -> str | None:
    if len(payload) < 24:
        return None
    values = [int.from_bytes(payload[i:i + 2], "big") for i in range(0, 16, 2)]
    max_lum = int.from_bytes(payload[16:20], "big")
    min_lum = int.from_bytes(payload[20:24], "big")
    return (
        "G({gx},{gy}) B({bx},{by}) R({rx},{ry}) WP({wx},{wy}) "
        "L({max_lum},{min_lum})"
    ).format(
        gx=values[2],
        gy=values[3],
        bx=values[4],
        by=values[5],
        rx=values[0],
        ry=values[1],
        wx=values[6],
        wy=values[7],
        max_lum=max_lum,
        min_lum=min_lum,
    )


def _parse_content_light(payload: bytes) -> str | None:
    if len(payload) < 4:
        return None
    max_cll = int.from_bytes(payload[0:2], "big")
    max_fall = int.from_bytes(payload[2:4], "big")
    return f"{max_cll},{max_fall}"


def _is_hdr10plus_t35(payload: bytes) -> bool:
    if len(payload) < 6:
        return False
    if payload[0] != 0xB5:
        return False
    provider_code = int.from_bytes(payload[1:3], "big")
    provider_oriented_code = int.from_bytes(payload[3:5], "big")
    application_identifier = payload[5]
    return (
        provider_code == 0x003C
        and provider_oriented_code == 0x0001
        and application_identifier == 0x04
    )


def _parse_sei_messages(nal_payload: bytes) -> list[tuple[int, bytes]]:
    if len(nal_payload) <= 2:
        return []
    rbsp = _remove_emulation_prevention_bytes(nal_payload[2:])
    pos = 0
    messages: list[tuple[int, bytes]] = []
    length = len(rbsp)
    while pos + 1 < length:
        payload_type = 0
        while pos < length and rbsp[pos] == 0xFF:
            payload_type += 0xFF
            pos += 1
        if pos >= length:
            break
        payload_type += rbsp[pos]
        pos += 1

        payload_size = 0
        while pos < length and rbsp[pos] == 0xFF:
            payload_size += 0xFF
            pos += 1
        if pos >= length:
            break
        payload_size += rbsp[pos]
        pos += 1

        if pos + payload_size > length:
            break
        payload = rbsp[pos:pos + payload_size]
        pos += payload_size
        messages.append((payload_type, payload))
        if pos < length and rbsp[pos] == 0x80 and pos == length - 1:
            break
    return messages


def _name_nal_type(nal_type: int) -> str:
    return _NAL_TYPE_NAMES.get(nal_type, f"NAL_{nal_type}")


def _audit_filename_claims(path: Path) -> FilenameAudit:
    name = path.name.lower()
    return FilenameAudit(
        claims_truehd="truehd" in name or ".mlp" in name,
        claims_atmos="atmos" in name,
        claims_7_1="7.1" in name or "7_1" in name,
        claims_dv=".dv." in name or " dolby vision " in name or ".dv" in name,
        claims_hdr10plus="hdr10p" in name or "hdr10+" in name,
    )


def _audit_ffprobe_container(runner: ToolRunner, path: Path) -> dict[str, Any]:
    payload = runner.run_json(
        [
            runner.ffprobe_bin,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            str(path),
        ]
    )
    return payload


def _audit_dovi_block_mapping(path: Path) -> DoviBlockMappingAudit:
    editor = MatroskaDoviBlockAdditionEditor()
    with path.open("rb") as fh:
        _, tracks_payload = editor._read_tracks_payload(fh)
    entries = editor._parse_track_entries(tracks_payload)
    target = next((entry for entry in entries if entry.is_hevc), None)
    return DoviBlockMappingAudit(
        video_track_number=target.track_number if target else None,
        codec_id=target.codec_id if target else "",
        has_dovi_block_addition=bool(target and target.has_dovi_block_addition),
        dvcc_present_anywhere=b"dvcC" in tracks_payload,
        dvvc_present_anywhere=b"dvvC" in tracks_payload,
    )


def _audit_packets(runner: ToolRunner, path: Path) -> PacketAudit:
    args = [
        runner.ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "packet=pts_time,dts_time,duration_time,size,flags",
        "-of",
        "csv=p=0",
        str(path),
    ]
    process = runner.popen(args)
    assert process.stdout is not None
    assert process.stderr is not None

    audit = PacketAudit()
    duration_counter: Counter[str] = Counter()
    last_pts: float | None = None
    last_dts: float | None = None
    min_pts_dts_delta: float | None = None
    max_pts_dts_delta: float | None = None
    total_size = 0
    peak_1_queue: deque[tuple[float, int]] = deque()
    peak_5_queue: deque[tuple[float, int]] = deque()
    peak_1_sum = 0
    peak_5_sum = 0
    peak_1_bits = 0.0
    peak_5_bits = 0.0

    reader = csv.reader((line.decode("utf-8", errors="replace") for line in process.stdout))
    for row in reader:
        if not row:
            continue
        while len(row) < 5:
            row.append("")
        pts_raw, dts_raw, duration_raw, size_raw, flags = row[:5]
        pts = float(pts_raw) if pts_raw and pts_raw != "N/A" else None
        dts = float(dts_raw) if dts_raw and dts_raw != "N/A" else None
        size = int(size_raw) if size_raw and size_raw != "N/A" else 0
        audit.packet_count += 1
        total_size += size
        audit.max_packet_size = max(audit.max_packet_size, size)
        if "K" in flags:
            audit.keyframe_packets += 1
        if pts is None:
            audit.missing_pts += 1
        if dts is None:
            audit.missing_dts += 1
        if pts is not None:
            if audit.first_pts is None:
                audit.first_pts = pts
            audit.last_pts = pts
            if last_pts is not None:
                if pts < last_pts:
                    audit.non_monotonic_pts += 1
                elif pts == last_pts:
                    audit.duplicate_pts += 1
            last_pts = pts
            peak_1_queue.append((pts, size))
            peak_5_queue.append((pts, size))
            peak_1_sum += size
            peak_5_sum += size
            while peak_1_queue and pts - peak_1_queue[0][0] >= 1.0:
                _, old_size = peak_1_queue.popleft()
                peak_1_sum -= old_size
            while peak_5_queue and pts - peak_5_queue[0][0] >= 5.0:
                _, old_size = peak_5_queue.popleft()
                peak_5_sum -= old_size
            peak_1_bits = max(peak_1_bits, peak_1_sum * 8.0)
            peak_5_bits = max(peak_5_bits, peak_5_sum * 8.0 / 5.0)
        if dts is not None:
            if last_dts is not None:
                if dts < last_dts:
                    audit.non_monotonic_dts += 1
                elif dts == last_dts:
                    audit.duplicate_dts += 1
            last_dts = dts
        if pts is not None and dts is not None:
            delta = pts - dts
            min_pts_dts_delta = delta if min_pts_dts_delta is None else min(min_pts_dts_delta, delta)
            max_pts_dts_delta = delta if max_pts_dts_delta is None else max(max_pts_dts_delta, delta)
        if duration_raw and duration_raw != "N/A":
            duration_counter[duration_raw] += 1

    stderr = process.stderr.read().decode("utf-8", errors="replace").strip()
    rc = process.wait()
    if rc != 0:
        raise ToolError(f"ffprobe packet audit failed (rc={rc}): {stderr}")

    audit.unique_duration_count = len(duration_counter)
    audit.duration_samples = [
        {"duration_time": duration, "count": count}
        for duration, count in duration_counter.most_common(8)
    ]
    audit.min_pts_dts_delta = _compact_float(min_pts_dts_delta, 6)
    audit.max_pts_dts_delta = _compact_float(max_pts_dts_delta, 6)
    if audit.packet_count:
        audit.average_packet_size = round(total_size / audit.packet_count, 2)
    if audit.first_pts is not None and audit.last_pts is not None and audit.last_pts > audit.first_pts:
        duration = audit.last_pts - audit.first_pts
        audit.average_bitrate_mbps = _compact_float((total_size * 8.0) / duration / 1_000_000.0)
    audit.likely_bframe_reordering = bool(
        audit.non_monotonic_pts > 0
        and audit.non_monotonic_dts == 0
        and (audit.min_pts_dts_delta or 0) >= 0
        and (audit.max_pts_dts_delta or 0) > 0
    )
    audit.peak_1s_bitrate_mbps = _compact_float(peak_1_bits / 1_000_000.0)
    audit.peak_5s_bitrate_mbps = _compact_float(peak_5_bits / 1_000_000.0)
    return audit


@dataclass
class _CurrentAu:
    has_slice: bool = False
    is_keyframe: bool = False
    has_rpu: bool = False
    has_hdr10plus: bool = False
    has_mdcv: bool = False
    has_cll: bool = False
    has_aud: bool = False
    has_pic_timing: bool = False
    has_vps_before_slice: bool = False
    has_sps_before_slice: bool = False
    has_pps_before_slice: bool = False
    nal_count: int = 0
    byte_count: int = 0


def _audit_hevc_bitstream(runner: ToolRunner, path: Path) -> HevcAudit:
    args = [
        runner.ffmpeg_bin,
        "-v",
        "error",
        "-i",
        str(path),
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
    process = runner.popen(args)
    assert process.stdout is not None
    assert process.stderr is not None

    audit = HevcAudit()
    nal_counts: Counter[int] = Counter()
    sei_payload_counts: Counter[int] = Counter()
    mastering_display_values: set[str] = set()
    content_light_values: set[str] = set()
    buffer = bytearray()
    current = _CurrentAu()
    first_au_seen = False
    total_au_bytes = 0

    def finalize_current() -> None:
        nonlocal current, first_au_seen, total_au_bytes
        if current.nal_count == 0 or not current.has_slice:
            current = _CurrentAu()
            return
        audit.access_units += 1
        total_au_bytes += current.byte_count
        audit.max_access_unit_size = max(audit.max_access_unit_size, current.byte_count)
        if current.is_keyframe:
            audit.key_access_units += 1
            if (
                current.has_vps_before_slice
                and current.has_sps_before_slice
                and current.has_pps_before_slice
            ):
                audit.key_access_units_with_param_sets += 1
        if current.has_rpu:
            audit.access_units_with_rpu += 1
        if current.has_hdr10plus:
            audit.access_units_with_hdr10plus += 1
        if current.has_mdcv:
            audit.access_units_with_mdcv += 1
        if current.has_cll:
            audit.access_units_with_cll += 1
        if current.has_aud:
            audit.access_units_with_aud += 1
        if current.has_pic_timing:
            audit.access_units_with_pic_timing += 1
        if not first_au_seen:
            audit.first_access_unit_has_vps = current.has_vps_before_slice
            audit.first_access_unit_has_sps = current.has_sps_before_slice
            audit.first_access_unit_has_pps = current.has_pps_before_slice
            first_au_seen = True
        current = _CurrentAu()

    def consume_nal(nal_payload: bytes, start_code_len: int) -> None:
        nonlocal current
        if len(nal_payload) < 2:
            return
        nal_type = (nal_payload[0] >> 1) & 0x3F
        is_slice = nal_type in _VCL_RANGE
        first_slice = bool(is_slice and len(nal_payload) >= 3 and (nal_payload[2] & 0x80))
        if is_slice and first_slice and current.has_slice:
            finalize_current()
        if not is_slice and current.has_slice and nal_type in _PREFIX_BOUNDARY_TYPES:
            finalize_current()

        nal_counts[nal_type] += 1
        current.nal_count += 1
        current.byte_count += start_code_len + len(nal_payload)

        if not current.has_slice:
            if nal_type == 32:
                current.has_vps_before_slice = True
            elif nal_type == 33:
                current.has_sps_before_slice = True
            elif nal_type == 34:
                current.has_pps_before_slice = True

        if nal_type in _IRAP_TYPES:
            current.is_keyframe = True
        if nal_type in (62, 63):
            current.has_rpu = True
        if nal_type == 35:
            current.has_aud = True
        if nal_type in (_SEI_PREFIX, _SEI_SUFFIX):
            for payload_type, payload in _parse_sei_messages(nal_payload):
                sei_payload_counts[payload_type] += 1
                if payload_type == _SEI_PIC_TIMING:
                    current.has_pic_timing = True
                if payload_type == _SEI_MASTERING_DISPLAY:
                    current.has_mdcv = True
                    value = _parse_mastering_display(payload)
                    if value:
                        mastering_display_values.add(value)
                elif payload_type == _SEI_CONTENT_LIGHT:
                    current.has_cll = True
                    value = _parse_content_light(payload)
                    if value:
                        content_light_values.add(value)
                elif payload_type == _SEI_USER_DATA_REGISTERED_ITU_T_T35 and _is_hdr10plus_t35(payload):
                    current.has_hdr10plus = True
        if is_slice:
            current.has_slice = True

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
            consume_nal(nal_payload, first[1])
            del buffer[:second[0]]

    if buffer:
        first = _find_start_code(buffer, 0)
        if first is not None:
            payload_start = first[0] + first[1]
            nal_payload = bytes(buffer[payload_start:])
            consume_nal(nal_payload, first[1])
    finalize_current()

    stderr = process.stderr.read().decode("utf-8", errors="replace").strip()
    rc = process.wait()
    if rc != 0:
        raise ToolError(f"ffmpeg HEVC audit failed (rc={rc}): {stderr}")

    audit.nal_type_counts = {
        _name_nal_type(nal_type): count
        for nal_type, count in sorted(nal_counts.items())
    }
    audit.trail_n_nals = nal_counts.get(0, 0)
    audit.trail_r_nals = nal_counts.get(1, 0)
    audit.prefix_sei_payload_type_counts = {
        _SEI_TYPE_NAMES.get(payload_type, f"sei_{payload_type}"): count
        for payload_type, count in sorted(sei_payload_counts.items())
    }
    audit.rpu_nal_count = nal_counts.get(62, 0) + nal_counts.get(63, 0)
    audit.static_hdr.unique_mastering_display = sorted(mastering_display_values)
    audit.static_hdr.unique_content_light = sorted(content_light_values)
    if audit.access_units:
        audit.average_access_unit_size = round(total_au_bytes / audit.access_units, 2)
    return audit


def _audit_dovi_metadata(runner: ToolRunner, path: Path, ffprobe_payload: dict[str, Any]) -> DoviAudit:
    streams = cast(list[dict[str, Any]], ffprobe_payload.get("streams") or [])
    video_stream: dict[str, Any] = next(
        (stream for stream in streams if stream.get("codec_type") == "video" and stream.get("codec_name") == "hevc"),
        {},
    )
    ffprobe_record: dict[str, Any] = next(
        (
            side_data
            for side_data in video_stream.get("side_data_list", [])
            if side_data.get("side_data_type") == "DOVI configuration record"
        ),
        {},
    )
    block_mapping = _audit_dovi_block_mapping(path)
    with tempfile.TemporaryDirectory(prefix="audit_mkv_dovi_") as tmpdir:
        rpu_path = Path(tmpdir) / "video.rpu.bin"
        extract_stdout, used_fallback = _extract_dovi_rpu_with_fallback(
            runner,
            input_path=path,
            output_path=rpu_path,
        )
        summary = runner.run_text(
            [
                runner.dovi_tool_bin,
                "info",
                "-s",
                "-i",
                str(rpu_path),
            ]
        ).strip()
    rpu_frames = None
    rpu_profile = None
    scene_count = None
    match = _RPU_FRAMES_RE.search(summary)
    if match:
        rpu_frames = int(match.group(1))
    match = _RPU_PROFILE_RE.search(summary)
    if match:
        rpu_profile = int(match.group(1))
    match = _SCENE_COUNT_RE.search(summary)
    if match:
        scene_count = int(match.group(1))
    return DoviAudit(
        ffprobe_record=ffprobe_record,
        block_mapping=block_mapping,
        rpu_summary_text=(
            ("Extraction mode: ffmpeg annex-b fallback\n" if used_fallback else "Extraction mode: direct dovi_tool\n")
            + ((extract_stdout.strip() + "\n") if extract_stdout.strip() else "")
            + summary
        ),
        rpu_frames=rpu_frames,
        rpu_profile=rpu_profile,
        scene_count=scene_count,
    )


def _audit_hdr10plus(runner: ToolRunner, path: Path) -> Hdr10PlusAudit:
    args = [runner.hdr10plus_tool_bin, "--verify", "extract", str(path)]
    try:
        output = runner.run_text(args).strip()
        return Hdr10PlusAudit(
            verify_ok="detected" in output.lower(),
            verify_output=output,
        )
    except ToolError as exc:
        return Hdr10PlusAudit(
            verify_ok=False,
            verify_output=str(exc),
        )


def _detect_hdr_mode(
    *,
    container: dict[str, Any],
    hevc_audit: HevcAudit,
    dovi_audit: DoviAudit,
    hdr10plus_audit: Hdr10PlusAudit,
) -> HdrModeAudit:
    streams = cast(list[dict[str, Any]], container.get("streams") or [])
    video_stream: dict[str, Any] = next(
        (
            stream for stream in streams
            if stream.get("codec_type") == "video"
            and stream.get("codec_name") == "hevc"
            and not stream.get("disposition", {}).get("attached_pic")
        ),
        {},
    )
    transfer = str(video_stream.get("color_transfer") or "").lower()
    primaries = str(video_stream.get("color_primaries") or "").lower()
    colorspace = str(video_stream.get("color_space") or "").lower()
    has_static_hdr_payload = bool(
        hevc_audit.static_hdr.unique_mastering_display
        and hevc_audit.static_hdr.unique_content_light
    )
    pq_bt2020 = (
        transfer == "smpte2084"
        and primaries == "bt2020"
        and colorspace == "bt2020nc"
    )
    has_dovi = bool(dovi_audit.ffprobe_record)
    has_hdr10plus = bool(hdr10plus_audit.verify_ok and hevc_audit.access_units_with_hdr10plus)
    has_hdr10 = bool(has_static_hdr_payload and pq_bt2020)

    parts: list[str] = []
    if has_dovi:
        parts.append("DoVi")
    if has_hdr10plus:
        parts.append("HDR10+")
    if has_hdr10:
        parts.append("HDR10")
    if not parts:
        parts.append("SDR")
    return HdrModeAudit(
        has_dovi=has_dovi,
        has_hdr10plus=has_hdr10plus,
        has_hdr10=has_hdr10,
        label=" + ".join(parts),
    )


def _build_workflow_audit(report: AuditReport, *, workflow_codec: str) -> WorkflowAudit:
    mode = _detect_hdr_mode(
        container=report.container,
        hevc_audit=report.hevc_audit,
        dovi_audit=report.dovi_audit,
        hdr10plus_audit=report.hdr10plus_audit,
    )
    static_hdr_mode = static_hdr_metadata_mode(workflow_codec).value
    dynamic_copy = mode.has_dovi or mode.has_hdr10plus
    static_patch = mode.has_hdr10 and needs_static_hdr_bitstream_patch_codec(workflow_codec)
    metadata_inject_required = dynamic_copy or static_patch

    expected_steps = ["Encode video-only elementary stream (`enc.hevc`)"]
    if mode.has_dovi:
        expected_steps.insert(0, "Extract Dolby Vision RPU from source")
    if mode.has_hdr10plus:
        insert_at = 1 if mode.has_dovi else 0
        expected_steps.insert(insert_at, "Extract HDR10+ metadata from source")
    if dynamic_copy:
        expected_steps.append("Run frame-count alignment audit between source, encoded stream and dynamic metadata")
    if mode.has_hdr10plus:
        expected_steps.append("Inject HDR10+ metadata into encoded HEVC")
    if mode.has_dovi:
        expected_steps.append("Inject Dolby Vision RPU into encoded HEVC")
    if static_patch:
        expected_steps.append("Patch static HDR10 SEI into encoded HEVC bitstream")
    if metadata_inject_required:
        expected_steps.append("Wrap injected HEVC, then reconstruct final MKV")
    else:
        expected_steps.append("Direct encode / remux path without metadata injection")
    if mode.has_dovi:
        expected_steps.append("Patch Matroska Dolby Vision BlockAdditionMapping (`dvcC`/`dvvC`)")

    first_au_keyframe = bool(report.hevc_audit.key_access_units and report.hevc_audit.first_access_unit_has_vps)
    static_hdr_on_expected_aus = report.hevc_audit.access_units_with_mdcv in {
        report.hevc_audit.key_access_units,
        report.hevc_audit.key_access_units + (0 if first_au_keyframe else 1),
    } and report.hevc_audit.access_units_with_cll in {
        report.hevc_audit.key_access_units,
        report.hevc_audit.key_access_units + (0 if first_au_keyframe else 1),
    }
    observed_checks = {
        "rpu_nal_matches_access_units": report.hevc_audit.rpu_nal_count in {
            report.hevc_audit.access_units - 1,
            report.hevc_audit.access_units,
            report.hevc_audit.access_units + 1,
        },
        "hdr10plus_present_in_all_access_units": (
            report.hevc_audit.access_units_with_hdr10plus == report.hevc_audit.access_units
        ),
        "static_hdr_present_on_expected_access_units": static_hdr_on_expected_aus,
        "dovi_block_mapping_present": report.dovi_audit.block_mapping.has_dovi_block_addition,
        "ffprobe_dovi_record_present": bool(report.dovi_audit.ffprobe_record),
        "packet_timestamps_monotonic": (
            report.packet_audit.non_monotonic_dts == 0
            and (
                report.packet_audit.non_monotonic_pts == 0
                or report.packet_audit.likely_bframe_reordering
            )
        ),
        "parameter_sets_repeated_on_keyframes": (
            report.hevc_audit.key_access_units_with_param_sets == report.hevc_audit.key_access_units
        ),
    }

    overall_consistent = True
    notes: list[str] = []
    if mode.has_dovi:
        dv_ok = (
            observed_checks["rpu_nal_matches_access_units"]
            and observed_checks["ffprobe_dovi_record_present"]
            and observed_checks["dovi_block_mapping_present"]
        )
        overall_consistent = overall_consistent and dv_ok
        if dv_ok:
            notes.append("Dolby Vision postconditions match the Muxiveo metadata-inject path.")
    if mode.has_hdr10plus:
        hdr10p_ok = bool(observed_checks["hdr10plus_present_in_all_access_units"])
        overall_consistent = overall_consistent and hdr10p_ok
        if hdr10p_ok:
            notes.append("HDR10+ appears to have been reinjected frame-for-frame as expected.")
    if static_patch:
        static_ok = bool(observed_checks["static_hdr_present_on_expected_access_units"])
        overall_consistent = overall_consistent and static_ok
        if static_ok:
            notes.append(
                f"Static HDR footprint is consistent with `{workflow_codec}` "
                f"using `{static_hdr_mode}`."
            )
    overall_consistent = overall_consistent and bool(observed_checks["packet_timestamps_monotonic"])
    if report.packet_audit.likely_bframe_reordering:
        notes.append("Packet-level PTS reordering looks consistent with normal B-frame decode order, not with broken mux timestamps.")
    if overall_consistent and metadata_inject_required:
        notes.append("The final MKV is structurally consistent with the repo NVENC metadata workflow.")
    if not metadata_inject_required:
        notes.append("No metadata-injection workflow would be required for this detected mode.")

    return WorkflowAudit(
        workflow_codec=workflow_codec,
        detected_mode=mode,
        static_hdr_mode=static_hdr_mode,
        metadata_inject_required=metadata_inject_required,
        expected_steps=expected_steps,
        observed_checks=observed_checks,
        overall_consistent=overall_consistent,
        notes=notes,
    )


def _build_findings(report: AuditReport) -> list[Finding]:
    findings: list[Finding] = []
    ffprobe_record = report.dovi_audit.ffprobe_record
    dovi_mapping = report.dovi_audit.block_mapping
    hevc = report.hevc_audit
    packet = report.packet_audit
    workflow = report.workflow_audit
    streams = report.container.get("streams", [])
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    max_channels = max((stream.get("channels", 0) or 0 for stream in audio_streams), default=0)
    has_truehd = any(stream.get("codec_name") in {"truehd", "mlp"} for stream in audio_streams)

    if ffprobe_record and not dovi_mapping.has_dovi_block_addition:
        findings.append(
            Finding(
                "high",
                "Dolby Vision is present in the HEVC bitstream, but the Matroska track lacks a DOVI BlockAdditionMapping (dvcC/dvvC). This is a common reason for DV not activating reliably in Plex and other players that inspect Matroska track signaling.",
            )
        )
    if (
        ffprobe_record.get("rpu_present_flag") == 1
        and hevc.access_units
        and hevc.rpu_nal_count not in {
            hevc.access_units - 1,
            hevc.access_units,
            hevc.access_units + 1,
        }
    ):
        findings.append(
            Finding(
                "high",
                f"RPU cadence looks inconsistent: {hevc.rpu_nal_count} RPU NAL units for {hevc.access_units} access units, while ffprobe reports rpu_present_flag=1.",
            )
        )
    if ffprobe_record.get("dv_bl_signal_compatibility_id") == 1 and not (
        hevc.access_units_with_mdcv and hevc.access_units_with_cll
    ):
        findings.append(
            Finding(
                "high",
                "DV compatibility id indicates HDR10 fallback, but static HDR10 SEI metadata is missing from at least part of the stream. HDR fallback behavior may be inconsistent on Plex clients.",
            )
        )
    if hevc.key_access_units and hevc.key_access_units_with_param_sets != hevc.key_access_units:
        findings.append(
            Finding(
                "medium",
                f"Parameter sets are not repeated before every key access unit ({hevc.key_access_units_with_param_sets}/{hevc.key_access_units}). Some players are tolerant, but random access and network recovery can be less robust.",
            )
        )
    if packet.non_monotonic_dts:
        findings.append(
            Finding(
                "high",
                f"Video packet DTS are not monotonic (DTS regressions={packet.non_monotonic_dts}). This can absolutely cause decoder instability.",
            )
        )
    elif packet.non_monotonic_pts and not packet.likely_bframe_reordering:
        findings.append(
            Finding(
                "medium",
                f"Video packet PTS are not monotonic (PTS regressions={packet.non_monotonic_pts}) outside the usual B-frame reordering pattern.",
            )
        )
    if packet.duplicate_pts or packet.duplicate_dts:
        findings.append(
            Finding(
                "medium",
                f"Duplicate video timestamps were found (PTS duplicates={packet.duplicate_pts}, DTS duplicates={packet.duplicate_dts}). This is worth checking against the muxing path.",
            )
        )
    if packet.peak_1s_bitrate_mbps and packet.peak_1s_bitrate_mbps > 80:
        findings.append(
            Finding(
                "medium",
                f"Peak 1-second video bitrate is high at about {packet.peak_1s_bitrate_mbps} Mb/s, which may stress weaker Plex clients even when the average bitrate is fine.",
            )
        )
    if report.filename_audit.claims_truehd and not has_truehd:
        findings.append(
            Finding(
                "medium",
                "The filename claims TrueHD, but no TrueHD/MLP audio track is present in the MKV.",
            )
        )
    if report.filename_audit.claims_7_1 and max_channels < 8:
        findings.append(
            Finding(
                "medium",
                f"The filename claims 7.1 audio, but the largest audio layout found is {max_channels} channels.",
            )
        )
    if report.filename_audit.claims_hdr10plus and not report.hdr10plus_audit.verify_ok:
        findings.append(
            Finding(
                "high",
                "The filename claims HDR10+, but hdr10plus_tool did not confirm dynamic HDR10+ metadata.",
            )
        )
    if report.filename_audit.claims_dv and not ffprobe_record:
        findings.append(
            Finding(
                "high",
                "The filename claims Dolby Vision, but ffprobe did not expose a DOVI configuration record.",
            )
        )
    if workflow.detected_mode.has_dovi and workflow.detected_mode.has_hdr10plus:
        findings.append(
            Finding(
                "high",
                "This file carries both Dolby Vision and HDR10+ metadata. Even when the bitstream is structurally valid, this combination is known to trigger playback bugs on some Plex clients and hardware decoders.",
            )
        )
    if workflow.detected_mode.has_dovi and hevc.trail_n_nals:
        findings.append(
            Finding(
                "medium",
                f"This Dolby Vision stream contains {hevc.trail_n_nals} TRAIL_N slices. That is not automatically invalid, but it is a meaningful compatibility difference versus simpler HEVC/DV streams and can correlate with drops or fallback on picky hardware decoders.",
            )
        )
    if workflow.detected_mode.has_hdr10plus and hevc.access_units_with_hdr10plus != hevc.access_units:
        findings.append(
            Finding(
                "high",
                f"HDR10+ is present but not on every access unit ({hevc.access_units_with_hdr10plus}/{hevc.access_units}), which would be inconsistent with the workflow inject path.",
            )
        )
    if workflow.detected_mode.has_hdr10 and workflow.metadata_inject_required and not workflow.observed_checks.get("static_hdr_present_on_expected_access_units", False):
        findings.append(
            Finding(
                "high",
                f"Static HDR10 SEI distribution does not match the expected `{workflow.workflow_codec}` post-patch footprint.",
            )
        )
    if workflow.detected_mode.has_dovi and not workflow.overall_consistent:
        findings.append(
            Finding(
                "medium",
                "The file contains Dolby Vision, but one or more workflow postconditions do not line up cleanly with the Muxiveo NVENC metadata-inject path.",
            )
        )
    if not findings:
        findings.append(
            Finding(
                "info",
                "No structural issue was detected by the automated audit. Playback problems would then be more likely tied to client-specific decoder behavior or to the encode recipe rather than to an obvious MKV/bitstream defect.",
            )
        )
    return findings


def _summarize_container(ffprobe_payload: dict[str, Any]) -> dict[str, Any]:
    fmt = ffprobe_payload.get("format", {})
    streams = ffprobe_payload.get("streams", [])
    chapters = ffprobe_payload.get("chapters", [])
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video" and not stream.get("disposition", {}).get("attached_pic")]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    subtitle_streams = [stream for stream in streams if stream.get("codec_type") == "subtitle"]
    attachments = [stream for stream in streams if stream.get("disposition", {}).get("attached_pic")]
    return {
        "format_name": fmt.get("format_name"),
        "duration": fmt.get("duration"),
        "size": fmt.get("size"),
        "bit_rate": fmt.get("bit_rate"),
        "nb_streams": fmt.get("nb_streams"),
        "title": (fmt.get("tags") or {}).get("title"),
        "video_stream_count": len(video_streams),
        "audio_stream_count": len(audio_streams),
        "subtitle_stream_count": len(subtitle_streams),
        "attachment_count": len(attachments),
        "chapter_count": len(chapters),
        "streams": streams,
        "chapters": chapters,
    }


def audit_file(path: Path, runner: ToolRunner, *, workflow_codec: str = "hevc_nvenc") -> AuditReport:
    ffprobe_payload = _audit_ffprobe_container(runner, path)
    container = _summarize_container(ffprobe_payload)
    packet_audit = _audit_packets(runner, path)
    hevc_audit = _audit_hevc_bitstream(runner, path)
    dovi_audit = _audit_dovi_metadata(runner, path, ffprobe_payload)
    hdr10plus_audit = _audit_hdr10plus(runner, path)
    filename_audit = _audit_filename_claims(path)
    report = AuditReport(
        input_path=str(path),
        container=container,
        packet_audit=packet_audit,
        hevc_audit=hevc_audit,
        dovi_audit=dovi_audit,
        hdr10plus_audit=hdr10plus_audit,
        filename_audit=filename_audit,
        workflow_audit=WorkflowAudit(workflow_codec=workflow_codec),
        findings=[],
    )
    report.workflow_audit = _build_workflow_audit(report, workflow_codec=workflow_codec)
    report.findings = _build_findings(report)
    return report


def _report_to_dict(report: AuditReport) -> dict[str, Any]:
    return {
        "input_path": report.input_path,
        "container": report.container,
        "packet_audit": asdict(report.packet_audit),
        "hevc_audit": asdict(report.hevc_audit),
        "dovi_audit": asdict(report.dovi_audit),
        "hdr10plus_audit": asdict(report.hdr10plus_audit),
        "filename_audit": asdict(report.filename_audit),
        "workflow_audit": asdict(report.workflow_audit),
        "findings": [asdict(finding) for finding in report.findings],
    }


def _print_text_report(report: AuditReport) -> None:
    print(f"Input: {report.input_path}")
    print()
    print("Findings:")
    for finding in report.findings:
        print(f"- [{finding.severity}] {finding.message}")
    print()
    print("Container:")
    print(
        f"- format={report.container.get('format_name')} "
        f"duration={report.container.get('duration')}s "
        f"size={report.container.get('size')} bytes "
        f"bitrate={report.container.get('bit_rate')} bps"
    )
    print(
        f"- streams: video={report.container.get('video_stream_count')} "
        f"audio={report.container.get('audio_stream_count')} "
        f"subtitles={report.container.get('subtitle_stream_count')} "
        f"attachments={report.container.get('attachment_count')} "
        f"chapters={report.container.get('chapter_count')}"
    )
    print()
    print("Video Packet Audit:")
    print(
        f"- packets={report.packet_audit.packet_count} "
        f"key_packets={report.packet_audit.keyframe_packets} "
        f"durations={report.packet_audit.unique_duration_count} variants"
    )
    print(
        f"- bitrate avg={report.packet_audit.average_bitrate_mbps} Mb/s "
        f"peak1s={report.packet_audit.peak_1s_bitrate_mbps} Mb/s "
        f"peak5s={report.packet_audit.peak_5s_bitrate_mbps} Mb/s"
    )
    print(
        f"- pts regressions={report.packet_audit.non_monotonic_pts} "
        f"dts regressions={report.packet_audit.non_monotonic_dts} "
        f"pts duplicates={report.packet_audit.duplicate_pts} "
        f"dts duplicates={report.packet_audit.duplicate_dts}"
    )
    print(f"- likely_bframe_reordering={report.packet_audit.likely_bframe_reordering}")
    print()
    print("HEVC Bitstream Audit:")
    print(
        f"- access_units={report.hevc_audit.access_units} "
        f"key_access_units={report.hevc_audit.key_access_units} "
        f"rpu_aus={report.hevc_audit.access_units_with_rpu} "
        f"hdr10plus_aus={report.hevc_audit.access_units_with_hdr10plus}"
    )
    print(
        f"- mdcv_aus={report.hevc_audit.access_units_with_mdcv} "
        f"cll_aus={report.hevc_audit.access_units_with_cll} "
        f"aud_aus={report.hevc_audit.access_units_with_aud}"
    )
    print(
        f"- first_au parameter sets: "
        f"VPS={report.hevc_audit.first_access_unit_has_vps} "
        f"SPS={report.hevc_audit.first_access_unit_has_sps} "
        f"PPS={report.hevc_audit.first_access_unit_has_pps}"
    )
    print(
        f"- trail_n={report.hevc_audit.trail_n_nals} "
        f"trail_r={report.hevc_audit.trail_r_nals} "
        f"pic_timing_aus={report.hevc_audit.access_units_with_pic_timing}"
    )
    print(
        f"- key_aus_with_param_sets={report.hevc_audit.key_access_units_with_param_sets}/"
        f"{report.hevc_audit.key_access_units}"
    )
    if report.hevc_audit.prefix_sei_payload_type_counts:
        print(f"- prefix_sei_payloads={json.dumps(report.hevc_audit.prefix_sei_payload_type_counts, ensure_ascii=True)}")
    if report.hevc_audit.static_hdr.unique_mastering_display:
        print("- mastering_display:")
        for value in report.hevc_audit.static_hdr.unique_mastering_display:
            print(f"  - {value}")
    if report.hevc_audit.static_hdr.unique_content_light:
        print("- content_light:")
        for value in report.hevc_audit.static_hdr.unique_content_light:
            print(f"  - {value}")
    print()
    print("Dolby Vision Audit:")
    print(f"- ffprobe_record={json.dumps(report.dovi_audit.ffprobe_record, ensure_ascii=True)}")
    print(
        f"- block_mapping present={report.dovi_audit.block_mapping.has_dovi_block_addition} "
        f"dvcc={report.dovi_audit.block_mapping.dvcc_present_anywhere} "
        f"dvvc={report.dovi_audit.block_mapping.dvvc_present_anywhere}"
    )
    print(
        f"- rpu_frames={report.dovi_audit.rpu_frames} "
        f"rpu_profile={report.dovi_audit.rpu_profile} "
        f"scene_count={report.dovi_audit.scene_count}"
    )
    print("- rpu_summary:")
    for line in report.dovi_audit.rpu_summary_text.splitlines():
        print(f"  {line}")
    print()
    print("HDR10+ Audit:")
    print(f"- verify_ok={report.hdr10plus_audit.verify_ok}")
    if report.hdr10plus_audit.verify_output:
        print(f"- verify_output={report.hdr10plus_audit.verify_output}")
    print()
    print("Workflow Audit:")
    print(
        f"- workflow_codec={report.workflow_audit.workflow_codec} "
        f"detected_mode={report.workflow_audit.detected_mode.label} "
        f"metadata_inject_required={report.workflow_audit.metadata_inject_required} "
        f"static_hdr_mode={report.workflow_audit.static_hdr_mode}"
    )
    print(f"- overall_consistent={report.workflow_audit.overall_consistent}")
    print("- expected_steps:")
    for step in report.workflow_audit.expected_steps:
        print(f"  - {step}")
    print("- observed_checks:")
    for key, value in report.workflow_audit.observed_checks.items():
        print(f"  - {key}={value}")
    if report.workflow_audit.notes:
        print("- notes:")
        for note in report.workflow_audit.notes:
            print(f"  - {note}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deep MKV / HEVC / DV / HDR10+ audit tool")
    parser.add_argument("input", type=Path, help="Input MKV path")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    parser.add_argument("--ffprobe-bin", default="ffprobe")
    parser.add_argument("--ffmpeg-bin", default="ffmpeg")
    parser.add_argument("--dovi-tool-bin", default="dovi_tool")
    parser.add_argument("--hdr10plus-tool-bin", default="hdr10plus_tool")
    parser.add_argument("--workflow-codec", default="hevc_nvenc", help="Codec workflow to audit against, e.g. hevc_nvenc")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    path = args.input.expanduser().resolve()
    if not path.is_file():
        print(f"Input file not found: {path}", file=sys.stderr)
        return 2
    runner = ToolRunner(
        ffprobe_bin=args.ffprobe_bin,
        ffmpeg_bin=args.ffmpeg_bin,
        dovi_tool_bin=args.dovi_tool_bin,
        hdr10plus_tool_bin=args.hdr10plus_tool_bin,
    )
    try:
        report = audit_file(path, runner, workflow_codec=args.workflow_codec)
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(_report_to_dict(report), indent=2, ensure_ascii=True))
    else:
        _print_text_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
