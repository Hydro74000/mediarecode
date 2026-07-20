"""
core/matroska/native_muxer.py

Muxer Matroska natif Python pour streams HEVC mono-track.

Cas d'usage : encapsuler un flux HEVC annexB ré-encodé (ayant subi
l'injection RPU DoVi / HDR10+ par dovi_tool / hdr10plus_tool) dans un MKV
en réutilisant les timestamps de la source d'origine. Permet de
préserver les sources VFR sans outil externe.

Pourquoi un muxer natif et pas ffmpeg ?
========================================
ffmpeg ``-f hevc -c copy → mkv`` :
  - ne sait pas générer des PTS depuis un fichier sidecar ;
  - écrit les PTS via le BSF ``setts=pts=N/(fps*TB)`` qui suppose un
    framerate constant — détruit l'alignement audio en VFR ;
  - n'écrit PAS le ``BlockAdditionMapping`` Dolby Vision au niveau
    conteneur (déjà documenté dans ``editors/dovi.py``).

Le muxer natif :
  1. Parse le HEVC en access units (1 AU = 1 frame) via
     ``MatroskaHevcAuSplitter``.
  2. Lit les PTS source via ``MatroskaTimestampReader`` (ffprobe).
  3. Émet un MKV minimal mais valide : EBML header + Segment + SeekHead +
     Info + Tracks (avec CodecPrivate hvcC + BlockAdditionMapping DV
     optionnel) + Clusters de SimpleBlocks + Cues d'index.
  4. Aucune dépendance autre que ffprobe (pour la lecture des PTS), qui
     fait déjà partie des prérequis du projet.

Limitations
===========
  - Mono-track : un seul stream vidéo HEVC. L'audio/sub/chapitres sont
    ajoutés ensuite par le mux final ffmpeg (STEP 9), qui copie le
    BlockAdditionMapping existant.
  - Pas de lacing (un SimpleBlock = une frame). Lacing pénalisant en HEVC
    de toute façon.
  - Pas de B-frame reordering forcé : par défaut on écrit les frames dans
    l'ordre des PTS croissants. Pour un HEVC brut copié/injecté depuis une
    source existante, ``timestamp_order="packet"`` conserve l'ordre packet
    source afin que les B-frames gardent leurs PTS d'origine.
  - CodecPrivate (hvcC) extrait des NAL VPS/SPS/PPS du 1er AU.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.version import WRITING_APPLICATION_TAG

from .ebml import (
    ascii_element,
    binary_element,
    element,
    encode_vint_size_minimal,
    float_element,
    string_element,
    uint_element,
    void_element,
)
from .editors.dovi import DolbyVisionConfigRecord
from .ids import (
    BLOCK_ADDITION_MAPPING_ID,
    BLOCK_ADD_ID_EXTRA_DATA_ID,
    BLOCK_ADD_ID_NAME_ID,
    BLOCK_ADD_ID_TYPE_ID,
    BLOCK_ADD_ID_VALUE_ID,
    CLUSTER_ID,
    CODEC_ID_ID,
    CODEC_PRIVATE_ID,
    CUES_ID,
    CUE_CLUSTER_POSITION_ID,
    CUE_DURATION_ID,
    CUE_POINT_ID,
    CUE_RELATIVE_POSITION_ID,
    CUE_TIME_ID,
    CUE_TRACK_ID,
    CUE_TRACK_POSITIONS_ID,
    DEFAULT_TIMESTAMP_SCALE_NS,
    DOC_TYPE_ID,
    DOC_TYPE_READ_VERSION_ID,
    DOC_TYPE_VERSION_ID,
    DURATION_ID,
    EBML_HEADER_ID,
    EBML_MAX_ID_LENGTH_ID,
    EBML_MAX_SIZE_LENGTH_ID,
    EBML_READ_VERSION_ID,
    EBML_VERSION_ID,
    FLAG_DEFAULT_ID,
    FLAG_ENABLED_ID,
    FLAG_LACING_ID,
    INFO_ID,
    LANGUAGE_ID,
    MUXING_APP_ID,
    PIXEL_HEIGHT_ID,
    PIXEL_WIDTH_ID,
    SEEK_HEAD_ID,
    SEEK_ID,
    SEEK_ID_FIELD_ID,
    SEEK_POSITION_ID,
    SIMPLE_BLOCK_FLAG_KEYFRAME,
    SIMPLE_BLOCK_ID,
    TIMESTAMP_SCALE_ID,
    TRACKS_ID,
    TRACK_ENTRY_ID,
    TRACK_NUMBER_ID,
    TRACK_TYPE_ID,
    TRACK_TYPE_VIDEO,
    TRACK_UID_ID,
    VIDEO_ID,
    WRITING_APP_ID,
)
from .hevc.access_units import (
    HevcAccessUnit,
    split_into_access_units,
)
from .timestamps import (
    MatroskaTimestampReader,
    TimestampSequence,
)


# --- FourCCs DoVi ----------------------------------------------------------

_FOURCC_DVCC = 0x64766343  # "dvcC"

# --- Réglages muxer ---------------------------------------------------------

#: Taille cible (frames) d'un Cluster. L'usage courant est ~1 s ; à 24 fps,
#: 24 frames/cluster donne une granularité de seek correcte sans bloquer
#: la lecture (Cluster trop gros → pic mémoire chez le démuxeur).
_DEFAULT_FRAMES_PER_CLUSTER = 24

# --- HEVC config record (hvcC) extraction ----------------------------------


@dataclass(frozen=True)
class _HvccComponents:
    """Composants extraits des NAL VPS/SPS/PPS pour fabriquer un hvcC."""
    vps: list[bytes]
    sps: list[bytes]
    pps: list[bytes]


def _extract_hvcc_components(au: HevcAccessUnit) -> _HvccComponents:
    """Extrait les NAL VPS/SPS/PPS du 1er access unit (ils précèdent l'IRAP)."""
    vps: list[bytes] = []
    sps: list[bytes] = []
    pps: list[bytes] = []
    for nal in au.nal_units:
        if nal.nal_type == 32:
            vps.append(nal.payload)
        elif nal.nal_type == 33:
            sps.append(nal.payload)
        elif nal.nal_type == 34:
            pps.append(nal.payload)
    return _HvccComponents(vps=vps, sps=sps, pps=pps)


class _BitReader:
    """Lecture bit à bit big-endian d'un RBSP (exp-golomb inclus)."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._position = 0

    def u(self, count: int) -> int:
        value = 0
        for _ in range(count):
            byte = self._data[self._position >> 3]
            value = (value << 1) | ((byte >> (7 - (self._position & 7))) & 1)
            self._position += 1
        return value

    def ue(self) -> int:
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
            if zeros > 32:
                raise ValueError("Exp-Golomb invalide (préfixe trop long).")
        return (1 << zeros) - 1 + (self.u(zeros) if zeros else 0)


def _rbsp_from_nal(nal: bytes) -> bytes:
    """Supprime les octets d'emulation prevention (00 00 03) d'un NAL."""
    out = bytearray()
    zeros = 0
    for byte in nal:
        if zeros >= 2 and byte == 3:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


@dataclass(frozen=True)
class _SpsSummary:
    """Champs du SPS HEVC nécessaires au header hvcC."""

    profile_space: int
    tier_flag: int
    profile_idc: int
    compatibility_flags: int      # 32 bits
    constraint_flags: int         # 48 bits
    level_idc: int
    chroma_format_idc: int
    bit_depth_luma_minus8: int
    bit_depth_chroma_minus8: int
    max_sub_layers_minus1: int
    temporal_id_nesting: int


def _parse_sps_summary(sps_nal: bytes) -> _SpsSummary | None:
    """Extrait profile_tier_level, chroma et bit depths du SPS (ITU-T H.265 §7.3.2.2)."""
    try:
        rbsp = _rbsp_from_nal(sps_nal)
        reader = _BitReader(rbsp[2:])  # saute le header NAL (2 octets)
        reader.u(4)  # sps_video_parameter_set_id
        max_sub_layers_minus1 = reader.u(3)
        temporal_id_nesting = reader.u(1)
        # profile_tier_level(1, max_sub_layers_minus1) — partie générale : 12 octets.
        profile_space = reader.u(2)
        tier_flag = reader.u(1)
        profile_idc = reader.u(5)
        compatibility_flags = reader.u(32)
        constraint_flags = reader.u(48)
        level_idc = reader.u(8)
        return _finish_sps_parse(
            reader, max_sub_layers_minus1, temporal_id_nesting,
            profile_space, tier_flag, profile_idc,
            compatibility_flags, constraint_flags, level_idc,
        )
    except (IndexError, ValueError):
        return None


def _finish_sps_parse(
    reader: _BitReader,
    max_sub_layers_minus1: int,
    temporal_id_nesting: int,
    profile_space: int,
    tier_flag: int,
    profile_idc: int,
    compatibility_flags: int,
    constraint_flags: int,
    level_idc: int,
) -> _SpsSummary:
    if max_sub_layers_minus1:
        presence = [(reader.u(1), reader.u(1)) for _ in range(max_sub_layers_minus1)]
        for _ in range(8 - max_sub_layers_minus1):
            reader.u(2)  # reserved_zero_2bits
        for profile_present, level_present in presence:
            if profile_present:
                reader.u(88)
            if level_present:
                reader.u(8)
    reader.ue()  # sps_seq_parameter_set_id
    chroma_format_idc = reader.ue()
    if chroma_format_idc == 3:
        reader.u(1)  # separate_colour_plane_flag
    reader.ue()  # pic_width_in_luma_samples
    reader.ue()  # pic_height_in_luma_samples
    if reader.u(1):  # conformance_window_flag
        reader.ue(); reader.ue(); reader.ue(); reader.ue()
    bit_depth_luma_minus8 = reader.ue()
    bit_depth_chroma_minus8 = reader.ue()
    return _SpsSummary(
        profile_space=profile_space,
        tier_flag=tier_flag,
        profile_idc=profile_idc,
        compatibility_flags=compatibility_flags,
        constraint_flags=constraint_flags,
        level_idc=level_idc,
        chroma_format_idc=chroma_format_idc,
        bit_depth_luma_minus8=bit_depth_luma_minus8,
        bit_depth_chroma_minus8=bit_depth_chroma_minus8,
        max_sub_layers_minus1=max_sub_layers_minus1,
        temporal_id_nesting=temporal_id_nesting,
    )


def _build_hvcc(components: _HvccComponents, sps_bytes: bytes | None = None) -> bytes:
    """
    Construit un CodecPrivate ``hvcC`` ISO/IEC 14496-15 depuis le bitstream.

    Le profile_tier_level (profil, tier, niveau, flags de compatibilité et
    de contrainte), le chroma_format_idc et les bit depths sont extraits du
    SPS ; si le SPS est illisible, un repli neutre est utilisé (les NAL
    arrays VPS/SPS/PPS restent la source de vérité pour les décodeurs).
    """
    sps_source = sps_bytes or (components.sps[0] if components.sps else None)
    summary = _parse_sps_summary(sps_source) if sps_source else None

    out = bytearray()
    out.append(1)             # configurationVersion
    if summary is not None:
        out.append((summary.profile_space << 6) | (summary.tier_flag << 5) | summary.profile_idc)
        out.extend(summary.compatibility_flags.to_bytes(4, "big"))
        out.extend(summary.constraint_flags.to_bytes(6, "big"))
        out.append(summary.level_idc)
    else:
        # Repli : profil Main / niveau 3.0, champs de compatibilité neutres.
        out.append(0x21)
        out.extend(b"\x00\x00\x00\x00")
        out.extend(b"\x00\x00\x00\x00\x00\x00")
        out.append(0x5A)
    # min_spatial_segmentation_idc (12 bits, padded)
    out.extend(b"\xF0\x00")
    out.append(0xFC)          # parallelismType (padded)
    if summary is not None:
        out.append(0xFC | (summary.chroma_format_idc & 0x03))
        out.append(0xF8 | (summary.bit_depth_luma_minus8 & 0x07))
        out.append(0xF8 | (summary.bit_depth_chroma_minus8 & 0x07))
    else:
        out.append(0xFC)      # chromaFormat inconnu
        out.append(0xF8)      # bitDepthLumaMinus8 inconnu
        out.append(0xF8)      # bitDepthChromaMinus8 inconnu
    out.extend(b"\x00\x00")   # avgFrameRate (non signalé)
    # constantFrameRate(2)|numTemporalLayers(3)|temporalIdNested(1)|lengthSizeMinusOne(2)
    # lengthSizeMinusOne = 3 → tailles NAL sur 4 octets (convention usuelle).
    if summary is not None:
        num_temporal_layers = min(summary.max_sub_layers_minus1 + 1, 7)
        out.append((num_temporal_layers << 3) | (summary.temporal_id_nesting << 2) | 0x03)
    else:
        out.append(0x03)
    # numOfArrays
    arrays: list[tuple[int, list[bytes]]] = []
    if components.vps:
        arrays.append((32, components.vps))
    if components.sps:
        arrays.append((33, components.sps))
    if components.pps:
        arrays.append((34, components.pps))
    out.append(len(arrays))
    for nal_type, nals in arrays:
        # array_completeness(1)|reserved(1)|NAL_unit_type(6)
        out.append(0x80 | (nal_type & 0x3F))
        out.extend(len(nals).to_bytes(2, "big"))  # numNalus
        for nal in nals:
            out.extend(len(nal).to_bytes(2, "big"))
            out.extend(nal)
    return bytes(out)


# --- SimpleBlock encoding --------------------------------------------------


def _encode_track_number_vint(track_number: int) -> bytes:
    """
    Encode un TrackNumber en VINT (1..n octets selon la valeur).
    Pour des TrackNumber 1..126, c'est 1 octet.
    """
    return encode_vint_size_minimal(track_number)


def _build_simple_block(
    *,
    track_number: int,
    timestamp_offset: int,
    payload: bytes,
    is_keyframe: bool,
) -> bytes:
    """
    Sérialise un SimpleBlock : VINT(TrackNumber) + int16 BE(timestamp offset
    relatif au cluster, en TimestampScale ticks) + flags(1) + payload NAL.

    ``timestamp_offset`` doit tenir dans ``int16``. À TimestampScale=1ms,
    ça donne ±32.7 s par cluster, largement assez pour un cluster de 1 s.
    """
    if not -32768 <= timestamp_offset <= 32767:
        raise ValueError(
            f"Offset SimpleBlock {timestamp_offset} ms hors plage int16 — "
            "Cluster trop long ?"
        )
    track_vint = _encode_track_number_vint(track_number)
    flags = SIMPLE_BLOCK_FLAG_KEYFRAME if is_keyframe else 0
    block_payload = (
        track_vint
        + timestamp_offset.to_bytes(2, "big", signed=True)
        + bytes([flags])
        + payload
    )
    return element(SIMPLE_BLOCK_ID, block_payload)


def _length_prefixed_payload(access_unit: HevcAccessUnit, length_size: int) -> bytes:
    """Reframe un AU annexB vers le framing hvcC (NAL préfixées longueur).

    Aucune NAL n'est supprimée : parameter sets, AUD, SEI et RPU DoVi restent
    dans le payload, dans l'ordre du flux (parité muxeurs de référence).
    ``length_size`` vient du champ lengthSizeMinusOne du CodecPrivate hvcC.
    """
    parts: list[bytes] = []
    for nal in access_unit.nal_units:
        parts.append(len(nal.payload).to_bytes(length_size, "big"))
        parts.append(nal.payload)
    return b"".join(parts)


# --- DoVi BlockAdditionMapping (réutilise le record commun) ----------------


def _build_dovi_block_addition_mapping(
    record: DolbyVisionConfigRecord,
    *,
    id_value: int = 1,
    id_name: str = "Dolby Vision configuration",
) -> bytes:
    children = b"".join([
        uint_element(BLOCK_ADD_ID_VALUE_ID, id_value),
        string_element(BLOCK_ADD_ID_NAME_ID, id_name),
        uint_element(BLOCK_ADD_ID_TYPE_ID, _FOURCC_DVCC),
        binary_element(BLOCK_ADD_ID_EXTRA_DATA_ID, record.to_bytes()),
    ])
    return element(BLOCK_ADDITION_MAPPING_ID, children)


# --- Track entry HEVC ------------------------------------------------------


def _build_video_track_entry(
    *,
    track_number: int,
    track_uid: int,
    codec_private: bytes,
    pixel_width: int,
    pixel_height: int,
    dovi_record: DolbyVisionConfigRecord | None,
    language: str = "und",
) -> bytes:
    video_master = element(VIDEO_ID, b"".join([
        uint_element(PIXEL_WIDTH_ID, pixel_width),
        uint_element(PIXEL_HEIGHT_ID, pixel_height),
    ]))

    children = b"".join([
        uint_element(TRACK_NUMBER_ID, track_number),
        uint_element(TRACK_UID_ID, track_uid),
        uint_element(TRACK_TYPE_ID, TRACK_TYPE_VIDEO),
        uint_element(FLAG_ENABLED_ID, 1),
        uint_element(FLAG_DEFAULT_ID, 1),
        uint_element(FLAG_LACING_ID, 0),
        string_element(LANGUAGE_ID, language),
        ascii_element(CODEC_ID_ID, "V_MPEGH/ISO/HEVC"),
        binary_element(CODEC_PRIVATE_ID, codec_private),
        video_master,
    ])
    if dovi_record is not None:
        children += _build_dovi_block_addition_mapping(dovi_record)
    return element(TRACK_ENTRY_ID, children)


# --- Tracks ----------------------------------------------------------------


def _build_tracks(track_entry: bytes) -> bytes:
    return element(TRACKS_ID, track_entry)


# --- Info ------------------------------------------------------------------


def _build_info(*, duration_ms: float, muxing_app: str, writing_app: str) -> bytes:
    payload = b"".join([
        uint_element(TIMESTAMP_SCALE_ID, DEFAULT_TIMESTAMP_SCALE_NS),
        float_element(DURATION_ID, float(duration_ms)),
        string_element(MUXING_APP_ID, muxing_app),
        string_element(WRITING_APP_ID, writing_app),
    ])
    return element(INFO_ID, payload)


# --- SeekHead --------------------------------------------------------------


def _build_seek_entry(target_id: bytes, segment_relative_offset: int) -> bytes:
    payload = (
        binary_element(SEEK_ID_FIELD_ID, target_id)
        + uint_element(SEEK_POSITION_ID, segment_relative_offset)
    )
    return element(SEEK_ID, payload)


def _build_seek_head(entries: list[tuple[bytes, int]], *, total_size: int) -> bytes:
    """
    Construit un SeekHead occupant exactement ``total_size`` octets sur
    disque (Void de queue compris). Permet de réserver l'espace dès le
    début du Segment et de le remplir à la fin.
    """
    seeks = b"".join(_build_seek_entry(tid, pos) for tid, pos in entries)
    body = element(SEEK_HEAD_ID, seeks)
    if len(body) > total_size:
        raise ValueError(
            f"SeekHead ({len(body)} octets) dépasse la taille réservée "
            f"({total_size}) — augmenter la réserve du writer."
        )
    pad = total_size - len(body)
    if pad == 0:
        return body
    if pad == 1:
        # Un Void minimal fait 2 octets ; on étire le SeekHead lui-même.
        seeks_with_pad = seeks + void_element(2)
        body2 = element(SEEK_HEAD_ID, seeks_with_pad)
        if len(body2) <= total_size:
            return body2 + b"\x00" * (total_size - len(body2))
        raise ValueError("Padding SeekHead impossible (1 octet).")
    return body + void_element(pad)


# --- Cluster + Cues --------------------------------------------------------


@dataclass
class _ClusterRecord:
    relative_offset: int      # offset du Cluster vs début du Segment
    timestamp_ms: int         # Timestamp absolu du Cluster (ticks)
    #: Entrées d'index : (time_ticks, track_number, relative_position,
    #: duration_ticks | None). ``relative_position`` est relatif au premier
    #: octet du payload du Cluster (RFC 9559, CueRelativePosition).
    cue_points: list[tuple[int, int, int, int | None]]


def _build_cues(clusters: list[_ClusterRecord]) -> bytes:
    """
    Construit l'élément Cues : CuePoints triés par CueTime croissant, un
    CueTrackPositions par piste indexée à ce temps (vidéo : keyframes de
    toutes les pistes ; sous-titres : chaque entrée, avec CueDuration quand
    elle est connue ; audio-only : un point de la piste primaire par Cluster).
    """
    entries: list[tuple[int, int, int, int, int | None]] = []
    for cluster in clusters:
        for time_ticks, track_number, relative_position, duration_ticks in cluster.cue_points:
            entries.append((
                time_ticks, track_number, cluster.relative_offset,
                relative_position, duration_ticks,
            ))
    entries.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    points: list[bytes] = []
    index = 0
    while index < len(entries):
        time_ticks = entries[index][0]
        positions = b""
        while index < len(entries) and entries[index][0] == time_ticks:
            _time, track_number, cluster_offset, relative_position, duration_ticks = entries[index]
            body = b"".join([
                uint_element(CUE_TRACK_ID, track_number),
                uint_element(CUE_CLUSTER_POSITION_ID, cluster_offset),
                uint_element(CUE_RELATIVE_POSITION_ID, relative_position),
            ])
            if duration_ticks:
                body += uint_element(CUE_DURATION_ID, duration_ticks)
            positions += element(CUE_TRACK_POSITIONS_ID, body)
            index += 1
        points.append(element(CUE_POINT_ID, uint_element(CUE_TIME_ID, time_ticks) + positions))
    return element(CUES_ID, b"".join(points))


# --- Header EBML -----------------------------------------------------------


def _build_ebml_header() -> bytes:
    payload = b"".join([
        uint_element(EBML_VERSION_ID, 1),
        uint_element(EBML_READ_VERSION_ID, 1),
        uint_element(EBML_MAX_ID_LENGTH_ID, 4),
        uint_element(EBML_MAX_SIZE_LENGTH_ID, 8),
        string_element(DOC_TYPE_ID, "matroska"),
        uint_element(DOC_TYPE_VERSION_ID, 4),
        uint_element(DOC_TYPE_READ_VERSION_ID, 2),
    ])
    return element(EBML_HEADER_ID, payload)


# --- Muxer ---------------------------------------------------------------


@dataclass(frozen=True)
class MatroskaNativeMuxResult:
    output_path: Path
    track_number: int
    frames_written: int
    cluster_count: int
    duration_ms: int


class MatroskaNativeMuxer:
    """
    Muxer Matroska natif Python pour 1 piste vidéo HEVC + timestamps source.

    Utilisation typique :

        muxer = MatroskaNativeMuxer()
        muxer.mux(
            hevc_input=Path("enc_dv.hevc"),
            source_for_timestamps=Path("source.mkv"),
            output=Path("enc_wrapped.mkv"),
            pixel_width=3840,
            pixel_height=2160,
            dovi_record=record,         # optionnel : signal DV au niveau MKV
        )

    Le muxer :
      - lit les PTS source via ``MatroskaTimestampReader`` ;
      - parse le HEVC en access units via ``MatroskaHevcAuSplitter`` ;
      - vérifie que le nombre d'AU correspond au nombre de PTS (frame
        count guard appelé en amont — ici on lève si désaligné) ;
      - écrit un MKV valide avec EBML header + Segment + SeekHead +
        Info + Tracks + Clusters + Cues, sans dépendance externe autre
        que ffprobe pour la lecture initiale.
    """

    def __init__(
        self,
        *,
        ffprobe_bin: str = "ffprobe",
        muxing_app: str = "Muxiveo native muxer",
        writing_app: str = WRITING_APPLICATION_TAG,
        frames_per_cluster: int = _DEFAULT_FRAMES_PER_CLUSTER,
    ) -> None:
        self._timestamp_reader = MatroskaTimestampReader(ffprobe_bin=ffprobe_bin)
        self._muxing_app = muxing_app
        self._writing_app = writing_app
        self._frames_per_cluster = max(1, frames_per_cluster)

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def mux(
        self,
        *,
        hevc_input: Path,
        source_for_timestamps: Path,
        output: Path,
        pixel_width: int,
        pixel_height: int,
        dovi_record: DolbyVisionConfigRecord | None = None,
        track_number: int = 1,
        track_uid: int = 1,
        language: str = "und",
        timestamp_order: str = "presentation",
    ) -> MatroskaNativeMuxResult:
        if timestamp_order not in {"presentation", "packet"}:
            raise ValueError("timestamp_order doit être 'presentation' ou 'packet'.")
        # 1) Parser le HEVC
        hevc_bytes = hevc_input.read_bytes()
        access_units = split_into_access_units(hevc_bytes)
        if not access_units:
            raise RuntimeError(f"Aucun access unit HEVC trouvé dans {hevc_input}.")

        # 2) Lire les PTS source
        pts_seq = self._timestamp_reader.read(
            source_for_timestamps,
            sort_by_pts=(timestamp_order == "presentation"),
        )

        # 3) Vérifier l'alignement (le frame count guard a normalement déjà
        #    aligné les choses ; on lève ici si quelque chose a glissé).
        if len(access_units) != len(pts_seq):
            raise RuntimeError(
                f"Désalignement frame count : {len(access_units)} access "
                f"units HEVC vs {len(pts_seq)} PTS source. L'audit "
                "frame_count_guard a-t-il été exécuté ?"
            )

        # 4) Extraire VPS/SPS/PPS pour CodecPrivate
        components = _extract_hvcc_components(access_units[0])
        if not components.sps:
            # Si le 1er AU ne contient pas de SPS, on tente d'en trouver un
            # plus loin (cas des AppendVPS/SPS/PPS écrits par certains
            # encodeurs au 1er keyframe seulement).
            for au in access_units[1:8]:
                comp_extra = _extract_hvcc_components(au)
                if comp_extra.sps:
                    components = comp_extra
                    break
        if not (components.vps and components.sps and components.pps):
            raise RuntimeError(
                "VPS/SPS/PPS manquants dans le HEVC source — "
                "CodecPrivate impossible à construire."
            )
        codec_private = _build_hvcc(components)

        # 5) Écrire le fichier
        return self._write_mkv(
            access_units=access_units,
            pts_seq=pts_seq,
            output=output,
            codec_private=codec_private,
            pixel_width=pixel_width,
            pixel_height=pixel_height,
            dovi_record=dovi_record,
            track_number=track_number,
            track_uid=track_uid,
            language=language,
        )

    # ------------------------------------------------------------------
    # Écriture
    # ------------------------------------------------------------------

    def _write_mkv(
        self,
        *,
        access_units: list[HevcAccessUnit],
        pts_seq: TimestampSequence,
        output: Path,
        codec_private: bytes,
        pixel_width: int,
        pixel_height: int,
        dovi_record: DolbyVisionConfigRecord | None,
        track_number: int,
        track_uid: int,
        language: str,
    ) -> MatroskaNativeMuxResult:
        # Compatibility façade: build the historical HEVC TrackEntry, then
        # delegate the document to the generic deterministic writer.
        from io import BytesIO
        from .mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack
        from .reader import MatroskaBlock, MatroskaReader, MatroskaTrack, read_element
        from .writer import MatroskaWriter

        track_entry_raw = _build_video_track_entry(
            track_number=track_number,
            track_uid=track_uid,
            codec_private=codec_private,
            pixel_width=pixel_width,
            pixel_height=pixel_height,
            dovi_record=dovi_record,
            language=language,
        )
        stream = BytesIO(track_entry_raw)
        entry_element = read_element(stream, limit=len(track_entry_raw))
        if entry_element is None or entry_element.size is None:
            raise RuntimeError("TrackEntry HEVC natif invalide")
        raw_entry = track_entry_raw[entry_element.payload_offset:entry_element.end]
        source_track = MatroskaTrack(
            number=track_number, uid=track_uid, track_type=TRACK_TYPE_VIDEO,
            codec_id="V_MPEGH/ISO/HEVC", codec_private=codec_private,
            language_bcp47="", language=language, name="", raw_entry=raw_entry,
        )
        mux_track = MatroskaMuxTrack(
            source=output, source_track=source_track,
            output_number=track_number, output_uid=track_uid,
            language=language, flag_default=True,
        )
        # Blocks : payload reframé annexB → length-prefixed selon le hvcC,
        # toutes NAL conservées. SimpleBlocks avec le vrai bit keyframe (AU
        # IRAP) — aucune durée par bloc, sinon le writer bascule en
        # BlockGroup sans ReferenceBlock et chaque frame serait vue
        # keyframe ; la durée totale vit dans Info.Duration via le plan.
        # Générateur (ordre producteur = ordre PTS source) : une seule copie
        # reframée en vol à la fois.
        length_size = (codec_private[21] & 0x03) + 1
        packets = (
            MatroskaMuxPacket(
                track_number,
                MatroskaBlock(
                    track_number=track_number,
                    timestamp_ms=pts,
                    flags=SIMPLE_BLOCK_FLAG_KEYFRAME if access_unit.is_keyframe else 0,
                    payload=_length_prefixed_payload(access_unit, length_size),
                    timestamp_ns=pts * 1_000_000,
                ),
                source_sequence=sequence,
            )
            for sequence, (access_unit, pts) in enumerate(zip(access_units, pts_seq.pts_ms))
        )
        MatroskaWriter().write(MatroskaMuxPlan(
            output=output, tracks=(mux_track,), packets=packets,
            duration_ms=pts_seq.total_duration_ms,
            duration_ns=pts_seq.total_duration_ms * 1_000_000,
            muxing_app=self._muxing_app, writing_app=self._writing_app,
        ))
        cluster_count = sum(
            item.element_id == CLUSTER_ID for item in MatroskaReader(output).top_level()
        )
        return MatroskaNativeMuxResult(
            output_path=output,
            track_number=track_number,
            frames_written=len(access_units),
            cluster_count=cluster_count,
            duration_ms=pts_seq.total_duration_ms,
        )

__all__ = [
    "MatroskaNativeMuxResult",
    "MatroskaNativeMuxer",
]
