from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core.workflows.hevc_static_hdr_metadata import (
    inject_static_hdr_sei,
    inject_static_hdr_sei_file,
)
from core.matroska.hevc.access_units import split_into_access_units


_MASTER_DISPLAY = "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)"
_MAX_CLL = "1000,400"


def _nal(nal_type: int, *, first_slice: bool = False, extra: bytes = b"") -> bytes:
    header_byte_0 = (nal_type & 0x3F) << 1
    header_byte_1 = 0x01
    rbsp_first = 0x80 if first_slice else 0x00
    return bytes([header_byte_0, header_byte_1, rbsp_first]) + extra


def _stream(*nals: bytes) -> bytes:
    return b"".join(b"\x00\x00\x00\x01" + nal for nal in nals)


def _prefix_sei_payload_types(nal_payload: bytes) -> set[int]:
    if len(nal_payload) <= 2:
        return set()

    ebsp = nal_payload[2:]
    rbsp = bytearray()
    zero_run = 0
    index = 0
    while index < len(ebsp):
        byte = ebsp[index]
        if zero_run >= 2 and byte == 0x03:
            zero_run = 0
            index += 1
            continue
        rbsp.append(byte)
        zero_run = zero_run + 1 if byte == 0x00 else 0
        index += 1

    payload_types: set[int] = set()
    offset = 0
    while offset < len(rbsp):
        if offset == len(rbsp) - 1 and rbsp[offset] == 0x80:
            break
        payload_type = 0
        while offset < len(rbsp) and rbsp[offset] == 0xFF:
            payload_type += 0xFF
            offset += 1
        if offset >= len(rbsp):
            break
        payload_type += rbsp[offset]
        offset += 1

        payload_size = 0
        while offset < len(rbsp) and rbsp[offset] == 0xFF:
            payload_size += 0xFF
            offset += 1
        if offset >= len(rbsp):
            break
        payload_size += rbsp[offset]
        offset += 1

        if offset + payload_size > len(rbsp):
            break
        payload_types.add(payload_type)
        offset += payload_size
    return payload_types


class TestStaticHdrSeiInjection:
    def test_injects_static_hdr_on_first_au_and_keyframes(self):
        stream = _stream(
            _nal(32),
            _nal(33),
            _nal(34),
            _nal(19, first_slice=True, extra=b"\x11" * 8),
            _nal(1, first_slice=True, extra=b"\x22" * 8),
            _nal(19, first_slice=True, extra=b"\x33" * 8),
        )

        injected, result = inject_static_hdr_sei(
            stream,
            master_display=_MASTER_DISPLAY,
            max_cll=_MAX_CLL,
        )

        assert result.applied is True
        assert result.injected_access_units == 2

        access_units = split_into_access_units(injected)
        assert len(access_units) == 3

        payload_types_by_au = [
            {
                payload_type
                for nal in access_unit.nal_units
                if nal.nal_type == 39
                for payload_type in _prefix_sei_payload_types(nal.payload)
            }
            for access_unit in access_units
        ]

        assert {137, 144}.issubset(payload_types_by_au[0])
        assert 137 not in payload_types_by_au[1]
        assert 144 not in payload_types_by_au[1]
        assert {137, 144}.issubset(payload_types_by_au[2])

    def test_reinjection_is_idempotent(self):
        stream = _stream(
            _nal(32),
            _nal(33),
            _nal(34),
            _nal(19, first_slice=True, extra=b"\x44" * 8),
            _nal(1, first_slice=True, extra=b"\x55" * 8),
        )

        first_pass, first_result = inject_static_hdr_sei(
            stream,
            master_display=_MASTER_DISPLAY,
            max_cll=_MAX_CLL,
        )
        second_pass, second_result = inject_static_hdr_sei(
            first_pass,
            master_display=_MASTER_DISPLAY,
            max_cll=_MAX_CLL,
        )

        assert first_result.applied is True
        assert second_result.applied is False
        assert second_result.preserved_access_units >= 1
        assert second_pass == first_pass


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe requis",
)
def test_file_injection_is_visible_to_ffprobe(tmp_path: Path) -> None:
    source = tmp_path / "source.hevc"
    injected = tmp_path / "injected.hevc"

    encode = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x64:rate=2",
            "-t",
            "1",
            "-c:v",
            "libx265",
            "-preset",
            "ultrafast",
            "-x265-params",
            "repeat-headers=1",
            "-an",
            "-f",
            "hevc",
            str(source),
        ],
        capture_output=True,
        text=True,
    )
    if encode.returncode != 0:
        pytest.skip(f"Generation HEVC impossible: {encode.stderr.strip()}")

    result = inject_static_hdr_sei_file(
        source,
        injected,
        master_display=_MASTER_DISPLAY,
        max_cll=_MAX_CLL,
    )
    assert result.applied is True

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_frames",
            "-read_intervals",
            "%+#1",
            "-print_format",
            "json",
            str(injected),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr

    frames = json.loads(probe.stdout or "{}").get("frames") or []
    assert frames, "ffprobe n'a retourne aucune frame"
    side_data = frames[0].get("side_data_list") or []
    side_data_types = {entry.get("side_data_type") for entry in side_data}
    assert "Mastering display metadata" in side_data_types
    assert "Content light level metadata" in side_data_types
