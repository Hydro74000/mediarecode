"""
core/workflows/matroska_element_ids.py

IDs binaires des éléments EBML/Matroska utilisés par les muxers et
éditeurs natifs Python du projet.

Source : ``matroska_elements_reference.json`` (RFC officielle, 2026-04-13).
Les IDs sont stockés en bytes pour usage direct avec les primitives
``ebml_writer`` (qui n'effectuent pas de re-encodage).
"""

from __future__ import annotations


def _hex_to_bytes(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str.removeprefix("0x"))


# ----------------------------------------------------------------------------
# Globals
# ----------------------------------------------------------------------------

VOID_ID = _hex_to_bytes("0xEC")
CRC32_ID = _hex_to_bytes("0xBF")

# ----------------------------------------------------------------------------
# Niveau 0 / EBML header
# ----------------------------------------------------------------------------

EBML_HEADER_ID = _hex_to_bytes("0x1A45DFA3")
EBML_VERSION_ID = _hex_to_bytes("0x4286")
EBML_READ_VERSION_ID = _hex_to_bytes("0x42F7")
EBML_MAX_ID_LENGTH_ID = _hex_to_bytes("0x42F2")
EBML_MAX_SIZE_LENGTH_ID = _hex_to_bytes("0x42F3")
DOC_TYPE_ID = _hex_to_bytes("0x4282")
DOC_TYPE_VERSION_ID = _hex_to_bytes("0x4287")
DOC_TYPE_READ_VERSION_ID = _hex_to_bytes("0x4285")

# ----------------------------------------------------------------------------
# Segment et SeekHead
# ----------------------------------------------------------------------------

SEGMENT_ID = _hex_to_bytes("0x18538067")
SEEK_HEAD_ID = _hex_to_bytes("0x114D9B74")
SEEK_ID = _hex_to_bytes("0x4DBB")
SEEK_ID_FIELD_ID = _hex_to_bytes("0x53AB")
SEEK_POSITION_ID = _hex_to_bytes("0x53AC")

# ----------------------------------------------------------------------------
# Info
# ----------------------------------------------------------------------------

INFO_ID = _hex_to_bytes("0x1549A966")
TIMESTAMP_SCALE_ID = _hex_to_bytes("0x2AD7B1")
DURATION_ID = _hex_to_bytes("0x4489")
MUXING_APP_ID = _hex_to_bytes("0x4D80")
WRITING_APP_ID = _hex_to_bytes("0x5741")
TITLE_ID = _hex_to_bytes("0x7BA9")

# ----------------------------------------------------------------------------
# Tracks
# ----------------------------------------------------------------------------

TRACKS_ID = _hex_to_bytes("0x1654AE6B")
TRACK_ENTRY_ID = _hex_to_bytes("0xAE")
TRACK_NUMBER_ID = _hex_to_bytes("0xD7")
TRACK_UID_ID = _hex_to_bytes("0x73C5")
TRACK_TYPE_ID = _hex_to_bytes("0x83")
FLAG_ENABLED_ID = _hex_to_bytes("0xB9")
FLAG_DEFAULT_ID = _hex_to_bytes("0x88")
FLAG_LACING_ID = _hex_to_bytes("0x9C")
DEFAULT_DURATION_ID = _hex_to_bytes("0x23E383")
LANGUAGE_ID = _hex_to_bytes("0x22B59C")
LANGUAGE_BCP47_ID = _hex_to_bytes("0x22B59D")
CODEC_ID_ID = _hex_to_bytes("0x86")
CODEC_PRIVATE_ID = _hex_to_bytes("0x63A2")
TRACK_TIMESTAMP_SCALE_ID = _hex_to_bytes("0x23314F")
NAME_ID = _hex_to_bytes("0x536E")
FLAG_FORCED_ID = _hex_to_bytes("0x55AA")

# Video
VIDEO_ID = _hex_to_bytes("0xE0")
PIXEL_WIDTH_ID = _hex_to_bytes("0xB0")
PIXEL_HEIGHT_ID = _hex_to_bytes("0xBA")
DISPLAY_WIDTH_ID = _hex_to_bytes("0x54B0")
DISPLAY_HEIGHT_ID = _hex_to_bytes("0x54BA")

# BlockAdditionMapping (DV signal)
BLOCK_ADDITION_MAPPING_ID = _hex_to_bytes("0x41E4")
BLOCK_ADD_ID_VALUE_ID = _hex_to_bytes("0x41F0")
BLOCK_ADD_ID_NAME_ID = _hex_to_bytes("0x41A4")
BLOCK_ADD_ID_TYPE_ID = _hex_to_bytes("0x41E7")
BLOCK_ADD_ID_EXTRA_DATA_ID = _hex_to_bytes("0x41ED")

# ----------------------------------------------------------------------------
# Clusters et Blocks
# ----------------------------------------------------------------------------

CLUSTER_ID = _hex_to_bytes("0x1F43B675")
TIMESTAMP_ID = _hex_to_bytes("0xE7")  # Cluster.Timestamp
SIMPLE_BLOCK_ID = _hex_to_bytes("0xA3")
BLOCK_GROUP_ID = _hex_to_bytes("0xA0")
BLOCK_ID = _hex_to_bytes("0xA1")
BLOCK_DURATION_ID = _hex_to_bytes("0x9B")
REFERENCE_BLOCK_ID = _hex_to_bytes("0xFB")
DISCARD_PADDING_ID = _hex_to_bytes("0x75A2")
CODEC_STATE_ID = _hex_to_bytes("0xA4")
BLOCK_ADDITIONS_ID = _hex_to_bytes("0x75A1")

# ----------------------------------------------------------------------------
# Cues
# ----------------------------------------------------------------------------

CUES_ID = _hex_to_bytes("0x1C53BB6B")
CUE_POINT_ID = _hex_to_bytes("0xBB")
CUE_TIME_ID = _hex_to_bytes("0xB3")
CUE_TRACK_POSITIONS_ID = _hex_to_bytes("0xB7")
CUE_TRACK_ID = _hex_to_bytes("0xF7")
CUE_CLUSTER_POSITION_ID = _hex_to_bytes("0xF1")
CUE_RELATIVE_POSITION_ID = _hex_to_bytes("0xF0")

# Attachments
ATTACHMENTS_ID = _hex_to_bytes("0x1941A469")
ATTACHED_FILE_ID = _hex_to_bytes("0x61A7")
FILE_DESCRIPTION_ID = _hex_to_bytes("0x467E")
FILE_NAME_ID = _hex_to_bytes("0x466E")
FILE_MEDIA_TYPE_ID = _hex_to_bytes("0x4660")
FILE_DATA_ID = _hex_to_bytes("0x465C")
FILE_UID_ID = _hex_to_bytes("0x46AE")

# Chapters
CHAPTERS_ID = _hex_to_bytes("0x1043A770")
EDITION_ENTRY_ID = _hex_to_bytes("0x45B9")
CHAPTER_ATOM_ID = _hex_to_bytes("0xB6")
CHAPTER_UID_ID = _hex_to_bytes("0x73C4")
CHAPTER_TIME_START_ID = _hex_to_bytes("0x91")
CHAPTER_TIME_END_ID = _hex_to_bytes("0x92")
CHAPTER_DISPLAY_ID = _hex_to_bytes("0x80")
CHAP_STRING_ID = _hex_to_bytes("0x85")
CHAP_LANGUAGE_ID = _hex_to_bytes("0x437C")

# Tags
TAGS_ID = _hex_to_bytes("0x1254C367")
TAG_ID = _hex_to_bytes("0x7373")
TARGETS_ID = _hex_to_bytes("0x63C0")
SIMPLE_TAG_ID = _hex_to_bytes("0x67C8")
TAG_NAME_ID = _hex_to_bytes("0x45A3")
TAG_STRING_ID = _hex_to_bytes("0x4487")

# ----------------------------------------------------------------------------
# Constantes Matroska / TrackType
# ----------------------------------------------------------------------------

TRACK_TYPE_VIDEO = 1
TRACK_TYPE_AUDIO = 2
TRACK_TYPE_SUBTITLE = 17

# Lacing flag : 0 = no lacing, 1 = Xiph lacing, 2 = fixed lacing, 3 = EBML
SIMPLE_BLOCK_FLAG_KEYFRAME = 0x80
SIMPLE_BLOCK_FLAG_INVISIBLE = 0x08
SIMPLE_BLOCK_FLAG_DISCARDABLE = 0x01

# TimestampScale : 1_000_000 ns/tick = 1 ms/tick (valeur Matroska standard).
DEFAULT_TIMESTAMP_SCALE_NS = 1_000_000


__all__ = [
    "ATTACHMENTS_ID", "ATTACHED_FILE_ID",
    "BLOCK_ADDITION_MAPPING_ID",
    "BLOCK_ADD_ID_EXTRA_DATA_ID",
    "BLOCK_ADD_ID_NAME_ID",
    "BLOCK_ADD_ID_TYPE_ID",
    "BLOCK_ADD_ID_VALUE_ID",
    "BLOCK_DURATION_ID",
    "BLOCK_ADDITIONS_ID",
    "BLOCK_GROUP_ID",
    "BLOCK_ID",
    "CLUSTER_ID",
    "CHAPTERS_ID", "CHAPTER_ATOM_ID", "CHAPTER_DISPLAY_ID", "CHAPTER_TIME_END_ID", "CHAPTER_TIME_START_ID", "CHAPTER_UID_ID", "CHAP_LANGUAGE_ID", "CHAP_STRING_ID",
    "CODEC_ID_ID",
    "CODEC_PRIVATE_ID",
    "CODEC_STATE_ID",
    "CRC32_ID",
    "CUES_ID",
    "CUE_CLUSTER_POSITION_ID",
    "CUE_POINT_ID",
    "CUE_RELATIVE_POSITION_ID",
    "CUE_TIME_ID",
    "CUE_TRACK_ID",
    "CUE_TRACK_POSITIONS_ID",
    "DEFAULT_DURATION_ID",
    "DISCARD_PADDING_ID",
    "DEFAULT_TIMESTAMP_SCALE_NS",
    "DISPLAY_HEIGHT_ID",
    "DISPLAY_WIDTH_ID",
    "DOC_TYPE_ID",
    "DOC_TYPE_READ_VERSION_ID",
    "DOC_TYPE_VERSION_ID",
    "DURATION_ID",
    "EDITION_ENTRY_ID",
    "EBML_HEADER_ID",
    "EBML_MAX_ID_LENGTH_ID",
    "EBML_MAX_SIZE_LENGTH_ID",
    "EBML_READ_VERSION_ID",
    "EBML_VERSION_ID",
    "FLAG_DEFAULT_ID",
    "FLAG_ENABLED_ID",
    "FLAG_FORCED_ID",
    "FLAG_LACING_ID",
    "FILE_DATA_ID", "FILE_DESCRIPTION_ID", "FILE_MEDIA_TYPE_ID", "FILE_NAME_ID", "FILE_UID_ID",
    "INFO_ID",
    "LANGUAGE_BCP47_ID",
    "LANGUAGE_ID",
    "MUXING_APP_ID",
    "NAME_ID",
    "PIXEL_HEIGHT_ID",
    "PIXEL_WIDTH_ID",
    "REFERENCE_BLOCK_ID",
    "SEEK_HEAD_ID",
    "SEEK_ID",
    "SEEK_ID_FIELD_ID",
    "SEEK_POSITION_ID",
    "SEGMENT_ID",
    "SIMPLE_BLOCK_FLAG_DISCARDABLE",
    "SIMPLE_BLOCK_FLAG_INVISIBLE",
    "SIMPLE_BLOCK_FLAG_KEYFRAME",
    "SIMPLE_BLOCK_ID",
    "TIMESTAMP_ID",
    "TIMESTAMP_SCALE_ID",
    "TAGS_ID", "TAG_ID", "TAG_NAME_ID", "TAG_STRING_ID", "TARGETS_ID", "SIMPLE_TAG_ID", "TITLE_ID",
    "TRACKS_ID",
    "TRACK_ENTRY_ID",
    "TRACK_NUMBER_ID",
    "TRACK_TIMESTAMP_SCALE_ID",
    "TRACK_TYPE_AUDIO",
    "TRACK_TYPE_ID",
    "TRACK_TYPE_SUBTITLE",
    "TRACK_TYPE_VIDEO",
    "TRACK_UID_ID",
    "VIDEO_ID",
    "VOID_ID",
    "WRITING_APP_ID",
]
