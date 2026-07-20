"""
core/matroska/ebml.py

Primitives d'écriture EBML/Matroska pures Python.

Ce module factorise les routines d'encodage VINT/UINT/SINT/Float utilisées
par les muxers et éditeurs Matroska du projet (header_editor,
dovi_block_addition, native_muxer). Aucun parsing : c'est strictement le
côté écriture.

Référence : https://datatracker.ietf.org/doc/html/rfc8794 (EBML)
            https://www.matroska.org/technical/elements.html (Matroska)
"""

from __future__ import annotations

import struct


# ----------------------------------------------------------------------------
# Encodage VINT (Variable-size Integer) — utilisé pour les tailles d'éléments
# ----------------------------------------------------------------------------


def encode_vint_size(value: int, *, length: int) -> bytes:
    """
    Encode ``value`` comme une VINT EBML sur exactement ``length`` octets.

    Le bit de tête (marker) indique la longueur : 1 préfixe sur le 1er octet
    sur ``length`` bits, suivi de la valeur en big-endian sur les
    ``7 * length`` bits restants.

    Lève ValueError si la valeur ne tient pas sur la longueur demandée, ou
    si elle vaut exactement la valeur "unknown" (toutes les data bits à 1).
    """
    if value < 0:
        raise ValueError("VINT négatif interdit.")
    if length < 1 or length > 8:
        raise ValueError(f"Longueur VINT invalide : {length}")
    max_known = (1 << (7 * length)) - 2
    if value > max_known:
        raise ValueError(
            f"Valeur {value} trop grande pour VINT de {length} octets."
        )
    raw = value.to_bytes(length, "big")
    marker = 1 << (8 - length)
    return bytes([raw[0] | marker]) + raw[1:]


def encode_vint_size_minimal(value: int) -> bytes:
    """Encode ``value`` sur le minimum d'octets possible (1..8)."""
    if value < 0:
        raise ValueError("VINT négatif interdit.")
    for length in range(1, 9):
        max_known = (1 << (7 * length)) - 2
        if value <= max_known:
            return encode_vint_size(value, length=length)
    raise ValueError("Valeur VINT trop grande (>8 octets).")


def encode_vint_size_prefer_length(value: int, *, preferred_length: int) -> bytes:
    """
    Encode ``value`` sur ``preferred_length`` octets si possible, sinon sur
    le minimum d'octets nécessaire. Utile pour préserver la taille d'origine
    d'un élément patché in-place.
    """
    if preferred_length < 1 or preferred_length > 8:
        raise ValueError("preferred_length VINT invalide.")
    for length in range(preferred_length, 9):
        try:
            return encode_vint_size(value, length=length)
        except ValueError:
            continue
    raise ValueError("Impossible d'encoder la VINT demandée.")


# ----------------------------------------------------------------------------
# Encodage des types de données EBML
# ----------------------------------------------------------------------------


def encode_uint(value: int) -> bytes:
    """Encode un entier non signé en big-endian, longueur minimale."""
    if value < 0:
        raise ValueError("Uint négatif.")
    if value == 0:
        return b"\x00"
    n = (value.bit_length() + 7) // 8
    return value.to_bytes(n, "big")


def encode_sint(value: int) -> bytes:
    """Encode un entier signé en two's-complement, longueur minimale."""
    if value == 0:
        return b"\x00"
    # Détermine le nombre minimal d'octets pour représenter la valeur signée.
    n = max(1, (value.bit_length() + 8) // 8)
    return value.to_bytes(n, "big", signed=True)


def encode_float64(value: float) -> bytes:
    """Encode un float IEEE 754 64-bit (Matroska n'utilise plus le 32-bit)."""
    return struct.pack(">d", value)


def encode_string(value: str) -> bytes:
    """UTF-8."""
    return value.encode("utf-8")


def encode_ascii(value: str) -> bytes:
    """ASCII strict (CodecID, DocType, etc.)."""
    return value.encode("ascii")


# ----------------------------------------------------------------------------
# Composition d'éléments
# ----------------------------------------------------------------------------


def element(
    element_id: bytes,
    payload: bytes,
    *,
    size_length: int | None = None,
) -> bytes:
    """
    Sérialise un élément EBML complet : ID + size VINT + payload.

    ``size_length`` force la longueur de la VINT taille (1..8). None →
    longueur minimale.
    """
    if size_length is None:
        size = encode_vint_size_minimal(len(payload))
    else:
        size = encode_vint_size(len(payload), length=size_length)
    return element_id + size + payload


def uint_element(element_id: bytes, value: int) -> bytes:
    return element(element_id, encode_uint(value))


def sint_element(element_id: bytes, value: int) -> bytes:
    return element(element_id, encode_sint(value))


def float_element(element_id: bytes, value: float) -> bytes:
    return element(element_id, encode_float64(value))


def string_element(element_id: bytes, value: str) -> bytes:
    return element(element_id, encode_string(value))


def ascii_element(element_id: bytes, value: str) -> bytes:
    return element(element_id, encode_ascii(value))


def binary_element(element_id: bytes, value: bytes) -> bytes:
    return element(element_id, value)


def void_element(total_size: int) -> bytes:
    """
    Construit un élément Void (0xEC) occupant exactement ``total_size``
    octets sur disque (header inclus). Minimum 2 octets.
    """
    if total_size < 2:
        raise ValueError("Un Void occupe au minimum 2 octets.")
    if total_size < 9:
        payload_size = total_size - 2
        return b"\xec" + encode_vint_size(payload_size, length=1) + (b"\x00" * payload_size)
    # Au-delà de 8 octets de payload, on utilise une VINT 8 octets pour la
    # taille (header de 1 + 8 = 9 octets).
    payload_size = total_size - 9
    return b"\xec" + encode_vint_size(payload_size, length=8) + (b"\x00" * payload_size)


# ----------------------------------------------------------------------------
# Aides spécifiques Matroska
# ----------------------------------------------------------------------------


def encode_unknown_size_marker(*, length: int = 8) -> bytes:
    """
    Encode la marque "unknown size" pour un élément de taille non bornée
    (utilisé pour Segment quand on ne connaît pas la taille à l'avance ;
    convention : VINT avec data bits tous à 1).
    """
    if length < 1 or length > 8:
        raise ValueError("Longueur VINT invalide.")
    marker = 1 << (8 - length)
    raw = bytes([marker | ((1 << (8 - length)) - 1)]) + (b"\xff" * (length - 1))
    return raw


__all__ = [
    "ascii_element",
    "binary_element",
    "element",
    "encode_ascii",
    "encode_float64",
    "encode_sint",
    "encode_string",
    "encode_uint",
    "encode_unknown_size_marker",
    "encode_vint_size",
    "encode_vint_size_minimal",
    "encode_vint_size_prefer_length",
    "float_element",
    "sint_element",
    "string_element",
    "uint_element",
    "void_element",
]
