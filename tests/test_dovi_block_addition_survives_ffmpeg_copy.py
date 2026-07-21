"""
Test d'intégration : vérifie que le ``BlockAdditionMapping`` Dolby Vision
ajouté par notre muxer natif (ou par l'éditeur post-mux) survit à un
``ffmpeg -c:v copy`` qui simule le mux final STEP 9 du pipeline d'encode.

Pourquoi c'est important
========================
STEP 9 du pipeline d'encode reconstruit le conteneur MKV final via
``ffmpeg -i enc_wrapped.mkv -i source.mkv ... -c copy out.mkv``. Si ffmpeg
**réécrit** le bloc Tracks au lieu de simplement copier le stream, il peut
omettre le ``BlockAdditionMapping`` que le muxer natif a soigneusement
ajouté — et notre signal DV au niveau conteneur disparaît silencieusement
de la sortie finale, malgré tous les efforts en amont.

Ce test confirme expérimentalement le comportement de ffmpeg sur la
version installée. S'il échoue, le filet de sécurité est le post-patch
``MatroskaDoviBlockAdditionEditor`` appliqué après STEP 9 dans
``metadata_inject.py`` (idempotent).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from core.matroska.editors.dovi import (
    DolbyVisionConfigRecord,
    MatroskaDoviBlockAdditionEditor,
)
from core.matroska.native_muxer import MatroskaNativeMuxer


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg / ffprobe non disponibles",
)


# ---------------------------------------------------------------------------
# Helpers HEVC factice (réutilisés depuis test_matroska_native_muxer)
# ---------------------------------------------------------------------------


def _nal(nal_type: int, *, first_slice: bool = False, extra: bytes = b"") -> bytes:
    header_byte_0 = (nal_type & 0x3F) << 1
    header_byte_1 = 0x01
    rbsp_first = 0x80 if first_slice else 0x00
    return bytes([header_byte_0, header_byte_1, rbsp_first]) + extra


def _build_fake_hevc(frame_count: int = 5) -> bytes:
    out = b""

    def _put(nal: bytes) -> None:
        nonlocal out
        out += b"\x00\x00\x00\x01" + nal

    _put(_nal(32, extra=b"\x40\x01"))
    _put(_nal(33, extra=b"\x42\x01"))
    _put(_nal(34, extra=b"\x44\x01"))
    _put(_nal(19, first_slice=True, extra=b"\x00" * 32))
    for i in range(1, frame_count):
        _put(_nal(1, first_slice=True, extra=bytes([i & 0xFF]) * 16))
    return out


def _make_packets_json(pts_list_s: list[float]) -> str:
    return json.dumps({"packets": [{"pts_time": f"{t:.6f}"} for t in pts_list_s]})


# ---------------------------------------------------------------------------
# Détection du BlockAdditionMapping dans un MKV en parsant directement
# (ffprobe ne le remonte que pour les codecs qu'il décode vraiment ; sur
# notre HEVC factice, on ne peut pas s'y fier)
# ---------------------------------------------------------------------------


def _mkv_contains_dovi_block_addition_mapping(mkv: Path) -> tuple[bool, dict]:
    """
    Détecte la présence du BlockAdditionMapping DV en cherchant la signature
    binaire dans le fichier. Renvoie (présent, détails).

    Accepte ``dvcC`` (v1, ce que nous écrivons) ET ``dvvC`` (v2, ce que
    ffmpeg ≥ 4.4 utilise quand il remuxe). Les deux FourCCs sont
    sémantiquement équivalents pour les players modernes.
    """
    data = mkv.read_bytes()
    bam_id = b"\x41\xe4"
    fourcc_dvcc = b"dvcC"  # version 1 (notre muxer natif)
    fourcc_dvvc = b"dvvC"  # version 2 (ce que ffmpeg réécrit en remux)
    has_bam = bam_id in data
    has_dvcc = fourcc_dvcc in data
    has_dvvc = fourcc_dvvc in data
    has_any_dv_fourcc = has_dvcc or has_dvvc
    return (has_bam and has_any_dv_fourcc, {
        "bam_marker_present": has_bam,
        "dvcc_present": has_dvcc,
        "dvvc_present": has_dvvc,
        "size_bytes": len(data),
    })


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDoviBlockAdditionSurvivesFfmpegCopy:
    """
    Construit un MKV avec BlockAdditionMapping DV via le muxer natif, le
    passe à travers ``ffmpeg -c copy``, et vérifie ce qui survit.
    """

    def _build_input_mkv_with_dv_signal(self, tmp_path: Path) -> Path:
        hevc = tmp_path / "in.hevc"
        hevc.write_bytes(_build_fake_hevc(frame_count=5))
        out = tmp_path / "with_dv.mkv"

        record = DolbyVisionConfigRecord(
            profile=8, level=6,
            rpu_present=True, el_present=False, bl_present=True,
            bl_signal_compat_id=1,
        )
        with patch(
            "core.matroska.timestamps.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=_make_packets_json([i * 0.041666 for i in range(5)]),
                stderr="",
            ),
        ):
            MatroskaNativeMuxer().mux(
                hevc_input=hevc,
                source_for_timestamps=tmp_path / "src.mkv",
                output=out,
                pixel_width=3840,
                pixel_height=2160,
                dovi_record=record,
            )
        return out

    def test_input_mkv_contains_dv_signal(self, tmp_path):
        """Pré-condition : le MKV produit par le muxer natif contient bien le signal DV."""
        mkv = self._build_input_mkv_with_dv_signal(tmp_path)
        present, details = _mkv_contains_dovi_block_addition_mapping(mkv)
        assert present, f"Pré-condition cassée : {details}"

    def test_ffmpeg_remux_simulation_step9_preserves_dv_signal(self, tmp_path):
        """
        Simule STEP 9 : ``ffmpeg -i with_dv.mkv -map 0:v:0 -c:v copy out.mkv``.

        Constat empirique sur ffmpeg ≥ 4.4 : le BlockAdditionMapping est
        **préservé** mais ffmpeg **convertit** le BlockAddIDType de ``dvcC``
        (notre v1) vers ``dvvC`` (v2 standardisée). Le payload
        BlockAddIDExtraData (24 octets DOVI config record) reste identique.

        Conclusion pratique : le signal DV survit à STEP 9. Le post-patch
        ``MatroskaDoviBlockAdditionEditor`` devient un filet de sécurité
        idempotent (il détecte ``dvvC`` comme déjà DV-marqué et ne fait
        rien) plutôt qu'une nécessité.
        """
        src = self._build_input_mkv_with_dv_signal(tmp_path)
        out = tmp_path / "after_ffmpeg_copy.mkv"

        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-y", "-loglevel", "error",
             "-i", str(src),
             "-map", "0:v:0",
             "-c:v", "copy",
             str(out)],
            capture_output=True, text=True,
        )
        if result.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            pytest.skip(
                f"ffmpeg n'a pas pu remuxer le HEVC factice : rc={result.returncode}, "
                f"stderr={result.stderr[:200]}"
            )

        present, details = _mkv_contains_dovi_block_addition_mapping(out)
        assert present, (
            f"Le BlockAdditionMapping DV doit survivre au remux ffmpeg. "
            f"Détails : {details}"
        )
        # Vérifie le payload DOVI config record (24 octets) intact — c'est
        # le contenu sémantique qui compte pour les players, pas le FourCC.
        # On retrouve les premiers octets : 01 (version_major) 00 (version_minor)
        # 10 (profile=8 << 1) ...
        data = out.read_bytes()
        assert b"\x01\x00\x10" in data, (
            "Premier octets du DOVI config record introuvables — "
            "le payload a été altéré ?"
        )

    def test_post_patch_is_idempotent_after_ffmpeg_copy(self, tmp_path):
        """
        Filet de sécurité : appeler MatroskaDoviBlockAdditionEditor.patch()
        sur un MKV qui contient déjà un BAM DV (en v1 ou v2) doit être
        un no-op. Garantit qu'on n'altère pas un fichier déjà conforme.
        """
        src = self._build_input_mkv_with_dv_signal(tmp_path)
        out = tmp_path / "after_ffmpeg_copy.mkv"

        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-y", "-loglevel", "error",
             "-i", str(src),
             "-map", "0:v:0",
             "-c:v", "copy",
             str(out)],
            capture_output=True, text=True,
        )
        if result.returncode != 0 or not out.exists():
            pytest.skip(
                f"ffmpeg n'a pas pu remuxer : rc={result.returncode}, "
                f"stderr={result.stderr[:200]}"
            )

        record = DolbyVisionConfigRecord(
            profile=8, level=6,
            rpu_present=True, el_present=False, bl_present=True,
            bl_signal_compat_id=1,
        )
        size_before_patch = out.stat().st_size
        patch_result = MatroskaDoviBlockAdditionEditor().patch(out, record=record)

        # Le BAM est déjà là (en dvvC après ffmpeg) → patch doit skip.
        assert patch_result.skipped is True
        assert patch_result.applied is False
        # Le fichier ne doit pas avoir grossi.
        assert out.stat().st_size == size_before_patch

        # Et le signal DV reste présent.
        present, details = _mkv_contains_dovi_block_addition_mapping(out)
        assert present, f"Signal DV perdu après no-op patch ? Détails : {details}"
