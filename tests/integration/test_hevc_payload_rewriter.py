"""
tests/integration/test_hevc_payload_rewriter.py — Corpus temporel du lot 3.

Valide la réécriture de payloads HEVC sur des médias réels (libx265) :
- CFR sans B-frames ; CFR avec B-frames (PTS non monotones en ordre de
  décodage) ; VFR ;
- comparaison ffprobe : ordre de décodage, PTS/DTS, durée par paquet,
  keyframes, frame count ;
- hash des payloads après normalisation (identité et injection SEI) ;
- signalisation Dolby Vision (BlockAdditionMapping) ;
- assemblage multi-pistes vidéo depuis des artefacts réécrits ;
- offsets négatifs coupés avant zéro (contrat d'assemblage).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from core.matroska.assembly import (
    MatroskaAssemblyPlan,
    MatroskaAssemblyTrack,
    assembly_output_contract,
    compile_assembly_plan,
)
from core.matroska.editors.dovi import DolbyVisionConfigRecord
from core.matroska.hevc.access_units import split_into_access_units
from core.matroska.hevc.payload_rewriter import MatroskaHevcPayloadRewriter
from core.matroska.hevc.timing_skeleton import write_timing_skeleton
from core.matroska.mux_plan import deterministic_source_identity
from core.matroska.reader import MatroskaReader
from core.matroska.writer import MatroskaWriter


_PARAMETER_SET_TYPES = {32, 33, 34}
_AUD_TYPE = 35


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, f"{' '.join(cmd)}\n{result.stderr}"


def _encode_hevc_mkv(
    path: Path,
    *,
    bframes: int,
    frames: int = 48,
    vf: str | None = None,
) -> Path:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size=192x108:rate=24:duration={frames / 24 + 1}",
        "-frames:v", str(frames),
    ]
    if vf:
        cmd.extend(["-vf", vf, "-fps_mode", "vfr"])
    cmd.extend([
        "-c:v", "libx265", "-preset", "ultrafast",
        "-x265-params", f"bframes={bframes}:keyint=16:log-level=none",
        "-an", str(path),
    ])
    _run(cmd)
    return path


def _extract_annexb(mkv: Path, hevc: Path) -> Path:
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(mkv), "-map", "0:v:0", "-c", "copy",
        "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", str(hevc),
    ])
    return hevc


def _packet_timeline(path: Path) -> list[tuple[object, object, object, str]]:
    """(pts, dts, durée, flags K) par paquet, dans l'ordre du fichier (décodage)."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_packets", "-select_streams", "v:0", str(path),
        ],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    packets = json.loads(result.stdout or "{}").get("packets", [])
    return [
        (
            packet.get("pts"),
            packet.get("dts"),
            packet.get("duration"),
            "K" if "K" in str(packet.get("flags", "")) else "_",
        )
        for packet in packets
    ]


def _normalized_au_hashes(hevc: Path) -> list[str]:
    """Hash par AU après normalisation (parameter sets et AUD exclus)."""
    hashes: list[str] = []
    for access_unit in split_into_access_units(hevc.read_bytes()):
        digest = hashlib.sha256()
        for nal in access_unit.nal_units:
            if nal.nal_type in _PARAMETER_SET_TYPES or nal.nal_type == _AUD_TYPE:
                continue
            digest.update(len(nal.payload).to_bytes(4, "big"))
            digest.update(nal.payload)
        hashes.append(digest.hexdigest())
    return hashes


def _inject_prefix_sei(source: Path, target: Path) -> Path:
    """Simule une injection HDR10+ : un SEI prefix (NAL 39) ajouté par AU."""
    sei = b"\x00\x00\x00\x01" + bytes([39 << 1, 0x01, 0x04, 0x02, 0x00, 0x80])
    parts: list[bytes] = []
    for access_unit in split_into_access_units(source.read_bytes()):
        parts.append(sei + access_unit.payload)
    target.write_bytes(b"".join(parts))
    return target


_RPU_MARKER = bytes(range(1, 17))


def _inject_suffix_rpu(source: Path, target: Path) -> Path:
    """Simule dovi_tool inject-rpu : un NAL 62 ajouté après les slices de
    chaque frame (suffixe d'AU — motif réel des flux Dolby Vision)."""
    rpu = b"\x00\x00\x00\x01" + bytes([62 << 1, 0x01]) + _RPU_MARKER
    parts: list[bytes] = []
    for access_unit in split_into_access_units(source.read_bytes()):
        parts.append(access_unit.payload + rpu)
    target.write_bytes(b"".join(parts))
    return target


def _rewrite_identity(tmp_path: Path, encoded: Path, name: str) -> Path:
    annexb = _extract_annexb(encoded, tmp_path / f"{name}.hevc")
    output = tmp_path / f"{name}_rewritten.mkv"
    result = MatroskaHevcPayloadRewriter().rewrite(
        encoded_mkv=encoded, injected_hevc=annexb, output=output,
    )
    assert result.frames_rewritten == len(_packet_timeline(encoded))
    return output


# =============================================================================
# 3.3 — Identité temporelle : CFR sans/avec B-frames, VFR
# =============================================================================

@pytest.mark.parametrize(
    ("name", "bframes", "vf"),
    [
        ("cfr_no_bframes", 0, None),
        ("cfr_bframes", 4, None),                       # PTS non monotones en décodage
        ("vfr", 4, "select='not(mod(n\\,3))'"),        # trous de présentation → VFR
    ],
)
def test_rewrite_preserves_timeline_and_payloads(tmp_path: Path, name: str, bframes: int, vf: str | None) -> None:
    encoded = _encode_hevc_mkv(tmp_path / f"{name}.mkv", bframes=bframes, vf=vf)
    reference_timeline = _packet_timeline(encoded)
    assert reference_timeline, "encodage libx265 vide"
    if bframes and vf is None:
        pts_values = [int(str(entry[0])) for entry in reference_timeline if entry[0] is not None]
        assert pts_values != sorted(pts_values), "le corpus B-frames doit réordonner les PTS"

    rewritten = _rewrite_identity(tmp_path, encoded, name)

    # Ordre de décodage, PTS/DTS, durées et keyframes strictement identiques.
    assert _packet_timeline(rewritten) == reference_timeline
    # Hash des payloads après normalisation : bitstream inchangé.
    reference_annexb = tmp_path / f"{name}.hevc"
    rewritten_annexb = _extract_annexb(rewritten, tmp_path / f"{name}_out.hevc")
    assert _normalized_au_hashes(rewritten_annexb) == _normalized_au_hashes(reference_annexb)
    # Cues : le fichier réécrit reste seekable sur ses keyframes.
    assert any(flag == "K" for *_rest, flag in _packet_timeline(rewritten))


# =============================================================================
# 3.3 — Injection simulée (HDR10+ SEI) et signalisation Dolby Vision
# =============================================================================

def test_injected_sei_keeps_timeline_and_signals_dovi(tmp_path: Path) -> None:
    encoded = _encode_hevc_mkv(tmp_path / "dv.mkv", bframes=4)
    reference_timeline = _packet_timeline(encoded)
    annexb = _extract_annexb(encoded, tmp_path / "dv.hevc")
    injected = _inject_prefix_sei(annexb, tmp_path / "dv_injected.hevc")
    record = DolbyVisionConfigRecord(
        profile=8, level=6, rpu_present=True, el_present=False,
        bl_present=True, bl_signal_compat_id=1,
    )
    output = tmp_path / "dv_rewritten.mkv"
    result = MatroskaHevcPayloadRewriter().rewrite(
        encoded_mkv=encoded, injected_hevc=injected, output=output,
        dovi_record=record,
    )
    assert result.dovi_mapping_written
    # Timeline inchangée malgré les payloads modifiés.
    assert _packet_timeline(output) == reference_timeline
    # Les payloads diffèrent (SEI injecté) mais le frame count est identique.
    out_hashes = _normalized_au_hashes(_extract_annexb(output, tmp_path / "dv_out.hevc"))
    in_hashes = _normalized_au_hashes(annexb)
    assert len(out_hashes) == len(in_hashes)
    assert out_hashes != in_hashes
    # Signalisation Dolby Vision écrite dans le TrackEntry (pas de post-patch).
    track = MatroskaReader(output).tracks()[0]
    assert track.block_addition_mappings
    assert track.block_addition_mappings[0]["extra_data"] == record.to_bytes()


# =============================================================================
# 3.3 — RPU suffixe (motif dovi_tool) : alignement frame-accurate
# =============================================================================

def test_suffix_rpu_stays_frame_accurate_through_rewrite(tmp_path: Path) -> None:
    """Le RPU (NAL 62, suffixe) doit rester dans le bloc de SA frame : ni
    décalage d'une frame, ni AU fantôme final (échec strict sinon)."""
    encoded = _encode_hevc_mkv(tmp_path / "rpu.mkv", bframes=4)
    reference_timeline = _packet_timeline(encoded)
    annexb = _extract_annexb(encoded, tmp_path / "rpu.hevc")
    injected = _inject_suffix_rpu(annexb, tmp_path / "rpu_injected.hevc")

    output = tmp_path / "rpu_rewritten.mkv"
    result = MatroskaHevcPayloadRewriter().rewrite(
        encoded_mkv=encoded, injected_hevc=injected, output=output,
    )
    assert result.frames_rewritten == len(reference_timeline)
    assert _packet_timeline(output) == reference_timeline
    # Chaque bloc vidéo du fichier réécrit porte exactement un RPU.
    blocks = [
        block for block in MatroskaReader(output).blocks()
        if block.payload
    ]
    assert len(blocks) == len(reference_timeline)
    for index, block in enumerate(blocks):
        assert block.payload.count(_RPU_MARKER) == 1, (
            f"bloc #{index}: RPU absent ou dupliqué"
        )


# =============================================================================
# Squelette de timing : réécriture depuis le squelette == depuis le MKV complet
# =============================================================================

@pytest.mark.parametrize(
    ("name", "bframes", "vf"),
    [
        ("skel_cfr_no_bframes", 0, None),
        ("skel_cfr_bframes", 4, None),
        ("skel_vfr", 4, "select='not(mod(n\\,3))'"),
    ],
)
def test_rewrite_from_timing_skeleton_matches_full_mkv(
    tmp_path: Path, name: str, bframes: int, vf: str | None,
) -> None:
    """Pic disque 2× : le squelette remplace le MKV encodé sans aucune dérive."""
    encoded = _encode_hevc_mkv(tmp_path / f"{name}.mkv", bframes=bframes, vf=vf)
    reference_timeline = _packet_timeline(encoded)
    annexb = _extract_annexb(encoded, tmp_path / f"{name}.hevc")
    injected = _inject_prefix_sei(annexb, tmp_path / f"{name}_injected.hevc")

    skeleton = tmp_path / f"{name}_timing.mkv"
    skeleton_result = write_timing_skeleton(encoded, skeleton)
    assert skeleton_result.blocks_written == len(reference_timeline)
    assert skeleton.stat().st_size < encoded.stat().st_size

    record = DolbyVisionConfigRecord(
        profile=8, level=6, rpu_present=True, el_present=False,
        bl_present=True, bl_signal_compat_id=1,
    )
    from_full = tmp_path / f"{name}_from_full.mkv"
    from_skeleton = tmp_path / f"{name}_from_skeleton.mkv"
    result_full = MatroskaHevcPayloadRewriter().rewrite(
        encoded_mkv=encoded, injected_hevc=injected, output=from_full,
        dovi_record=record,
    )
    result_skeleton = MatroskaHevcPayloadRewriter().rewrite(
        encoded_mkv=skeleton, injected_hevc=injected, output=from_skeleton,
        dovi_record=record,
    )
    assert result_skeleton.frames_rewritten == result_full.frames_rewritten
    assert result_skeleton.dovi_mapping_written and result_full.dovi_mapping_written

    # Les deux chemins produisent strictement le même fichier final.
    assert from_skeleton.read_bytes() == from_full.read_bytes()
    # Timeline conforme à l'encodeur, payloads injectés intacts, DoVi signalé.
    assert _packet_timeline(from_skeleton) == reference_timeline
    skeleton_hashes = _normalized_au_hashes(
        _extract_annexb(from_skeleton, tmp_path / f"{name}_skel_out.hevc")
    )
    assert skeleton_hashes == _normalized_au_hashes(injected)
    track = MatroskaReader(from_skeleton).tracks()[0]
    assert track.block_addition_mappings
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=format_name",
            "-of", "json", str(from_skeleton),
        ],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


# =============================================================================
# 3.3 — Multi-pistes vidéo et offsets négatifs (contrat d'assemblage)
# =============================================================================

def test_multi_video_assembly_from_rewritten_artifacts(tmp_path: Path) -> None:
    first = _rewrite_identity(
        tmp_path, _encode_hevc_mkv(tmp_path / "v1.mkv", bframes=0, frames=24), "v1",
    )
    second = _rewrite_identity(
        tmp_path, _encode_hevc_mkv(tmp_path / "v2.mkv", bframes=4, frames=24), "v2",
    )
    output = tmp_path / "multi.mkv"
    plan = MatroskaAssemblyPlan(
        output=output,
        ordered_tracks=(
            MatroskaAssemblyTrack(first, 0, deterministic_source_identity(first)),
            MatroskaAssemblyTrack(second, 0, deterministic_source_identity(second)),
        ),
    )
    plan = replace(plan, expected_output_contract=assembly_output_contract(plan))
    MatroskaWriter().write(compile_assembly_plan(plan))
    tracks = MatroskaReader(output).tracks()
    assert [track.track_type for track in tracks] == [1, 1]
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=format_name", "-of", "json", str(output)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_negative_offset_drops_packets_before_zero(tmp_path: Path) -> None:
    encoded = _encode_hevc_mkv(tmp_path / "neg.mkv", bframes=0, frames=24)
    rewritten = _rewrite_identity(tmp_path, encoded, "neg")
    reference_count = len(_packet_timeline(rewritten))
    output = tmp_path / "shifted.mkv"
    plan = MatroskaAssemblyPlan(
        output=output,
        ordered_tracks=(
            MatroskaAssemblyTrack(
                rewritten, 0, deterministic_source_identity(rewritten),
                time_shift_ms=-200,
            ),
        ),
    )
    plan = replace(plan, expected_output_contract=assembly_output_contract(plan))
    MatroskaWriter().write(compile_assembly_plan(plan))
    shifted = [block.timestamp_ms for block in MatroskaReader(output).blocks()]
    assert shifted
    assert len(shifted) < reference_count  # paquets < 0 coupés avant zéro
    assert min(shifted) >= 0
