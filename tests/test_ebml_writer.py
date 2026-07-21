"""Tests des primitives d'écriture EBML."""

from __future__ import annotations

import pytest

from core.matroska.ebml import (
    encode_sint,
    encode_uint,
    encode_unknown_size_marker,
    encode_vint_size,
    encode_vint_size_minimal,
    encode_vint_size_prefer_length,
    void_element,
)


class TestEncodeVintSize:
    def test_one_byte_max_known(self):
        # length=1 → 7 bits data, max known = 126
        assert encode_vint_size(126, length=1) == b"\xfe"

    def test_two_bytes(self):
        # length=2 → 14 bits data, marker 0x40
        assert encode_vint_size(0, length=2) == b"\x40\x00"
        assert encode_vint_size(1, length=2) == b"\x40\x01"

    def test_unknown_value_for_length_rejected(self):
        # 127 ne tient pas sur 1 octet (max known = 126).
        with pytest.raises(ValueError):
            encode_vint_size(127, length=1)

    def test_negative_rejected(self):
        with pytest.raises(ValueError):
            encode_vint_size(-1, length=1)


class TestEncodeVintSizeMinimal:
    @pytest.mark.parametrize("value,expected_len", [
        (0, 1),
        (126, 1),
        (127, 2),     # 127 → length 2
        (16382, 2),   # 16382 = (1<<14)-2
        (16383, 3),   # length 3
    ])
    def test_picks_minimal_length(self, value, expected_len):
        out = encode_vint_size_minimal(value)
        assert len(out) == expected_len


class TestEncodeVintSizePreferLength:
    def test_keeps_preferred_when_fits(self):
        out = encode_vint_size_prefer_length(5, preferred_length=3)
        assert len(out) == 3

    def test_grows_when_value_doesnt_fit(self):
        # 127 ne tient pas sur 1 → on doit retourner 2 octets.
        out = encode_vint_size_prefer_length(127, preferred_length=1)
        assert len(out) == 2


class TestEncodeUint:
    def test_zero(self):
        assert encode_uint(0) == b"\x00"

    def test_minimal_byte_count(self):
        assert encode_uint(255) == b"\xff"
        assert encode_uint(256) == b"\x01\x00"
        assert encode_uint(65535) == b"\xff\xff"

    def test_negative_rejected(self):
        with pytest.raises(ValueError):
            encode_uint(-1)


class TestEncodeSint:
    def test_zero(self):
        assert encode_sint(0) == b"\x00"

    def test_negative_one(self):
        # -1 en two's complement 1 octet = 0xFF
        assert encode_sint(-1) == b"\xff"

    def test_positive_127_fits_one_byte(self):
        assert encode_sint(127) == b"\x7f"

    def test_positive_128_needs_two_bytes(self):
        # 128 ne tient pas en signed sur 1 octet (max = 127), donc 2 octets.
        assert encode_sint(128) == b"\x00\x80"


class TestVoidElement:
    def test_minimum_size_two_bytes(self):
        out = void_element(2)
        assert len(out) == 2
        assert out[0] == 0xEC
        # taille payload = 0 → VINT 1-byte = 0x80
        assert out[1] == 0x80

    def test_size_three_bytes(self):
        out = void_element(3)
        assert len(out) == 3
        assert out[0] == 0xEC
        assert out[1] == 0x81  # payload=1
        assert out[2] == 0x00

    def test_too_small_rejected(self):
        with pytest.raises(ValueError):
            void_element(1)

    def test_large_size_uses_long_header(self):
        out = void_element(1024)
        assert len(out) == 1024
        assert out[0] == 0xEC


class TestEncodeUnknownSizeMarker:
    def test_8_byte_default(self):
        # length=8 → bit marker = 0x01, data bits tous à 1
        out = encode_unknown_size_marker()
        assert len(out) == 8
        assert out == b"\x01" + b"\xff" * 7

    def test_4_byte(self):
        # length=4 → bit marker = 0x10, data bits tous à 1
        out = encode_unknown_size_marker(length=4)
        assert len(out) == 4
        assert out == b"\x1f\xff\xff\xff"
