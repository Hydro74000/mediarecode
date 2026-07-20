"""Tests du splitter HEVC en access units."""

from __future__ import annotations

from core.matroska.hevc.access_units import (
    split_into_access_units,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _nal(nal_type: int, *, first_slice: bool = False, extra: bytes = b"") -> bytes:
    """
    Construit un NAL HEVC factice :
      - 2 octets header NAL : nal_type sur les bits 1..6 du 1er octet
      - puis ``first_slice_segment_in_pic_flag`` au bit MSB du 3e octet (RBSP)
      - puis ``extra``
    """
    header_byte_0 = (nal_type & 0x3F) << 1
    header_byte_1 = 0x01  # layer=0, temporal_id_plus1=1
    rbsp_first = 0x80 if first_slice else 0x00
    return bytes([header_byte_0, header_byte_1, rbsp_first]) + extra


def _stream(*nals: bytes) -> bytes:
    return b"".join(b"\x00\x00\x00\x01" + n for n in nals)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSplitIntoAccessUnits:
    def test_empty_stream_returns_empty_list(self):
        assert split_into_access_units(b"") == []

    def test_single_keyframe(self):
        # IDR_W_RADL (type 19), first_slice=True, avec VPS/SPS/PPS devant.
        stream = _stream(
            _nal(32),                    # VPS
            _nal(33),                    # SPS
            _nal(34),                    # PPS
            _nal(19, first_slice=True),  # IDR keyframe
        )
        aus = split_into_access_units(stream)
        assert len(aus) == 1
        assert aus[0].is_keyframe is True
        assert len(aus[0].nal_units) == 4

    def test_two_consecutive_frames(self):
        stream = _stream(
            _nal(33),                    # SPS
            _nal(19, first_slice=True),  # IDR (frame 1)
            _nal(1, first_slice=True),   # TRAIL_R (frame 2)
        )
        aus = split_into_access_units(stream)
        assert len(aus) == 2
        assert aus[0].is_keyframe is True
        assert aus[1].is_keyframe is False

    def test_prefix_sei_attached_to_next_au(self):
        # Un SEI prefix entre 2 frames doit appartenir au 2e AU.
        stream = _stream(
            _nal(19, first_slice=True),  # frame 1 IDR
            _nal(39),                    # SEI prefix (HDR10+ par exemple)
            _nal(1, first_slice=True),   # frame 2
        )
        aus = split_into_access_units(stream)
        assert len(aus) == 2
        # Le SEI prefix doit être dans le 2e AU.
        au2_types = [n.nal_type for n in aus[1].nal_units]
        assert 39 in au2_types

    def test_dovi_rpu_treated_as_suffix(self):
        # NAL type 62 (DV RPU) est un suffixe : dovi_tool l'écrit après les
        # slices de SA frame — il doit rester dans l'AU courant, sinon les
        # métadonnées DV glissent d'une frame (corpus réel).
        stream = _stream(
            _nal(19, first_slice=True),  # frame 1
            _nal(62),                    # DV RPU de la frame 1
            _nal(1, first_slice=True),   # frame 2
        )
        aus = split_into_access_units(stream)
        assert len(aus) == 2
        assert [n.nal_type for n in aus[0].nal_units] == [19, 62]
        assert [n.nal_type for n in aus[1].nal_units] == [1]

    def test_short_start_code_supported(self):
        # Start code 3 octets (0x000001) au lieu de 4.
        stream = b"\x00\x00\x01" + _nal(19, first_slice=True)
        aus = split_into_access_units(stream)
        assert len(aus) == 1
        assert aus[0].is_keyframe

    def test_keyframe_detection_for_irap_types(self):
        for irap_type in (16, 17, 18, 19, 20, 21):
            stream = _stream(_nal(irap_type, first_slice=True))
            aus = split_into_access_units(stream)
            assert len(aus) == 1, f"Type {irap_type} : AU non créé"
            assert aus[0].is_keyframe is True, f"Type {irap_type} non keyframe"

    def test_payload_round_trip_preserves_nal_bytes(self):
        nal1 = _nal(19, first_slice=True, extra=b"\xde\xad")
        nal2 = _nal(1, first_slice=True, extra=b"\xbe\xef")
        stream = _stream(nal1, nal2)
        aus = split_into_access_units(stream)
        assert len(aus) == 2
        # Le payload de l'AU doit ré-émettre les NAL avec start codes 4 octets.
        assert aus[0].payload == b"\x00\x00\x00\x01" + nal1
        assert aus[1].payload == b"\x00\x00\x00\x01" + nal2
