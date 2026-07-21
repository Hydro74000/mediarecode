"""Tests pour l'éditeur Matroska DoVi BlockAdditionMapping."""

from __future__ import annotations

import struct

import pytest

from core.matroska.editors.dovi import (
    DolbyVisionConfigRecord,
    MatroskaDoviBlockAdditionEditor,
)


# ---------------------------------------------------------------------------
# Helpers de construction d'un MKV minimaliste pour les tests
# ---------------------------------------------------------------------------


def _vint(value: int, *, length: int = 1) -> bytes:
    """Encode une VINT EBML (taille) sur ``length`` octets."""
    if value < 0:
        raise ValueError
    max_known = (1 << (7 * length)) - 2
    if value > max_known:
        raise ValueError(f"valeur {value} trop grande pour length={length}")
    raw = value.to_bytes(length, "big")
    marker = 1 << (8 - length)
    return bytes([raw[0] | marker]) + raw[1:]


def _wrap(element_id: bytes, payload: bytes, *, size_len: int = 1) -> bytes:
    return element_id + _vint(len(payload), length=size_len) + payload


def _uint_payload(value: int) -> bytes:
    if value == 0:
        return b"\x00"
    n = (value.bit_length() + 7) // 8
    return value.to_bytes(n, "big")


def _build_minimal_mkv(
    *,
    codec_id: str = "V_MPEGH/ISO/HEVC",
    track_number: int = 1,
    add_dovi_bam: bool = False,
) -> bytes:
    # EBML header (un seul élément factice DocType)
    ebml_header = _wrap(
        b"\x1a\x45\xdf\xa3",
        _wrap(b"\x42\x82", b"matroska"),  # DocType = "matroska"
        size_len=2,
    )

    # Track entry
    track_entry_children = (
        _wrap(b"\xd7", _uint_payload(track_number))     # TrackNumber
        + _wrap(b"\x73\xc5", _uint_payload(track_number))  # TrackUID (16-bit ID)
        + _wrap(b"\x83", _uint_payload(1))              # TrackType = 1 (video)
        + _wrap(b"\x86", codec_id.encode("ascii"))      # CodecID
    )
    if add_dovi_bam:
        bam_payload = (
            _wrap(b"\x41\xf0", _uint_payload(1))        # BlockAddIDValue
            + _wrap(b"\x41\xa4", b"Dolby Vision configuration")  # BlockAddIDName
            + _wrap(b"\x41\xe7", _uint_payload(0x64766343))      # BlockAddIDType = "dvcC"
            + _wrap(b"\x41\xed", b"\x00" * 24)          # BlockAddIDExtraData
        )
        track_entry_children += _wrap(b"\x41\xe4", bam_payload, size_len=2)

    track_entry = _wrap(b"\xae", track_entry_children, size_len=2)
    tracks = _wrap(b"\x16\x54\xae\x6b", track_entry, size_len=2)

    # Segment Info minimal (MuxingApp + WritingApp).
    info = _wrap(
        b"\x15\x49\xa9\x66",
        _wrap(b"\x4d\x80", b"libebml v1.4.0 + libmatroska v1.6.0")
        + _wrap(b"\x57\x41", b"test"),
        size_len=2,
    )

    segment_payload = info + tracks
    segment = _wrap(b"\x18\x53\x80\x67", segment_payload, size_len=4)

    return ebml_header + segment


# ---------------------------------------------------------------------------
# Tests du record DOVI
# ---------------------------------------------------------------------------


class TestDolbyVisionConfigRecord:
    def test_to_bytes_p8_1_layout(self):
        rec = DolbyVisionConfigRecord(
            profile=8, level=6,
            rpu_present=True, el_present=False, bl_present=True,
            bl_signal_compat_id=1,
        )
        out = rec.to_bytes()
        assert len(out) == 24
        assert out[0] == 1   # version major
        assert out[1] == 0   # version minor
        # profile=8 → 7 bits = 0001000 ; level=6 → high bit du level = 0
        assert out[2] == (8 << 1) | 0
        # 5 bits low du level (6) | rpu(1)<<2 | el(0)<<1 | bl(1)
        expected_byte3 = ((6 & 0x1F) << 3) | (1 << 2) | 0 | 1
        assert out[3] == expected_byte3
        # compat_id=1 dans les 4 bits hauts
        assert out[4] == (1 << 4)
        # reste à 0
        assert out[5:] == b"\x00" * 19

    def test_invalid_profile_rejected(self):
        with pytest.raises(ValueError):
            DolbyVisionConfigRecord(profile=200, level=6, rpu_present=True,
                                    el_present=False, bl_present=True,
                                    bl_signal_compat_id=1)

    def test_invalid_compat_id_rejected(self):
        with pytest.raises(ValueError):
            DolbyVisionConfigRecord(profile=8, level=6, rpu_present=True,
                                    el_present=False, bl_present=True,
                                    bl_signal_compat_id=99)


# ---------------------------------------------------------------------------
# Tests de l'éditeur
# ---------------------------------------------------------------------------


class TestMatroskaDoviBlockAdditionEditor:
    def _make_record(self) -> DolbyVisionConfigRecord:
        return DolbyVisionConfigRecord(
            profile=8, level=6,
            rpu_present=True, el_present=False, bl_present=True,
            bl_signal_compat_id=1,
        )

    def test_patch_injects_block_addition_mapping(self, tmp_path):
        mkv = tmp_path / "in.mkv"
        mkv.write_bytes(_build_minimal_mkv())

        editor = MatroskaDoviBlockAdditionEditor()
        result = editor.patch(mkv, record=self._make_record())

        assert result.applied is True
        assert result.skipped is False
        assert result.patched_track_number == 1

        # Le fichier doit maintenant contenir un BlockAdditionMapping (0x41E4)
        # avec le FourCC dvcC (0x64766343).
        data = mkv.read_bytes()
        assert b"\x41\xe4" in data
        assert struct.pack(">I", 0x64766343) in data
        # Et le payload 24 octets DOVI v1 (premier byte = 1, second = 0).
        # On cherche la signature spécifique : 01 00 [profile<<1|level_hi] ...
        # On valide indirectement via re-parse.

    def test_patch_skips_when_already_present(self, tmp_path):
        mkv = tmp_path / "in.mkv"
        mkv.write_bytes(_build_minimal_mkv(add_dovi_bam=True))
        size_before = mkv.stat().st_size

        editor = MatroskaDoviBlockAdditionEditor()
        result = editor.patch(mkv, record=self._make_record())

        assert result.applied is False
        assert result.skipped is True
        assert "déjà" in result.reason
        assert mkv.stat().st_size == size_before

    def test_patch_skips_when_no_hevc_track(self, tmp_path):
        mkv = tmp_path / "in.mkv"
        mkv.write_bytes(_build_minimal_mkv(codec_id="V_VP9"))

        editor = MatroskaDoviBlockAdditionEditor()
        result = editor.patch(mkv, record=self._make_record())

        assert result.applied is False
        assert result.skipped is True
        assert "HEVC" in result.reason

    def test_round_trip_record_can_be_decoded_back(self, tmp_path):
        mkv = tmp_path / "in.mkv"
        mkv.write_bytes(_build_minimal_mkv())

        record = DolbyVisionConfigRecord(
            profile=8, level=4,
            rpu_present=True, el_present=False, bl_present=True,
            bl_signal_compat_id=1,
        )
        editor = MatroskaDoviBlockAdditionEditor()
        editor.patch(mkv, record=record)

        # Re-parser et chercher l'extra_data.
        data = mkv.read_bytes()
        # Localiser la marque dvcC.
        idx = data.index(struct.pack(">I", 0x64766343))
        # Après ça, on cherche l'élément BlockAddIDExtraData (0x41ED) qui suit.
        extra_idx = data.index(b"\x41\xed", idx)
        # Lire size VINT (1 octet ici car payload=24 → fits sur 1 octet de size).
        size_byte = data[extra_idx + 2]
        assert size_byte == 0x80 | 24
        payload = data[extra_idx + 3:extra_idx + 3 + 24]
        assert len(payload) == 24
        assert payload[0] == 1
        assert payload[1] == 0
        # profile=8, level=4
        assert payload[2] == (8 << 1) | 0
        expected_byte3 = ((4 & 0x1F) << 3) | (1 << 2) | 0 | 1
        assert payload[3] == expected_byte3

    def test_patch_idempotent(self, tmp_path):
        mkv = tmp_path / "in.mkv"
        mkv.write_bytes(_build_minimal_mkv())

        editor = MatroskaDoviBlockAdditionEditor()
        first = editor.patch(mkv, record=self._make_record())
        assert first.applied is True

        second = editor.patch(mkv, record=self._make_record())
        assert second.applied is False
        assert second.skipped is True
