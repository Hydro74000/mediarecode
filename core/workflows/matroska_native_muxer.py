"""
core/workflows/matroska_native_muxer.py

Muxer Matroska natif Python pour streams HEVC mono-track.

Cas d'usage : encapsuler un flux HEVC annexB ré-encodé (ayant subi
l'injection RPU DoVi / HDR10+ par dovi_tool / hdr10plus_tool) dans un MKV
en réutilisant les timestamps de la source d'origine. Permet de
préserver les sources VFR sans dépendre de mkvmerge.

Pourquoi un muxer natif et pas ffmpeg ?
========================================
ffmpeg ``-f hevc -c copy → mkv`` :
  - ne sait pas générer des PTS depuis un fichier sidecar ;
  - écrit les PTS via le BSF ``setts=pts=N/(fps*TB)`` qui suppose un
    framerate constant — détruit l'alignement audio en VFR ;
  - n'écrit PAS le ``BlockAdditionMapping`` Dolby Vision au niveau
    conteneur (déjà documenté dans matroska_dovi_block_addition.py).

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
from typing import BinaryIO

from core.workflows.ebml_writer import (
    ascii_element,
    binary_element,
    element,
    encode_uint,
    encode_unknown_size_marker,
    encode_vint_size,
    encode_vint_size_minimal,
    float_element,
    string_element,
    uint_element,
    void_element,
)
from core.workflows.matroska_dovi_block_addition import DolbyVisionConfigRecord
from core.workflows.matroska_element_ids import (
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
    SEGMENT_ID,
    SIMPLE_BLOCK_FLAG_KEYFRAME,
    SIMPLE_BLOCK_ID,
    TIMESTAMP_ID,
    TIMESTAMP_SCALE_ID,
    TRACKS_ID,
    TRACK_ENTRY_ID,
    TRACK_NUMBER_ID,
    TRACK_TYPE_ID,
    TRACK_TYPE_VIDEO,
    TRACK_UID_ID,
    VIDEO_ID,
    VOID_ID,
    WRITING_APP_ID,
)
from core.workflows.matroska_hevc_au_splitter import (
    HevcAccessUnit,
    split_into_access_units,
)
from core.workflows.matroska_timestamp_reader import (
    MatroskaTimestampReader,
    TimestampSequence,
)


# --- FourCCs DoVi ----------------------------------------------------------

_FOURCC_DVCC = 0x64766343  # "dvcC"

# --- Réglages muxer ---------------------------------------------------------

#: Taille cible (frames) d'un Cluster. mkvmerge utilise ~1 s ; à 24 fps,
#: 24 frames/cluster donne une granularité de seek correcte sans bloquer
#: la lecture (Cluster trop gros → pic mémoire chez le démuxeur).
_DEFAULT_FRAMES_PER_CLUSTER = 24

#: Espace réservé pour le SeekHead initial (assez pour ~5 entrées de 30
#: octets chacune + Void de queue). Permet de finaliser le SeekHead à la
#: fin sans réécrire tout le fichier.
_SEEK_HEAD_RESERVED_BYTES = 256

#: Espace réservé pour le champ Duration de Info (qui ne peut pas être
#: rempli avant d'avoir muxé la dernière frame).
_INFO_RESERVED_EXTRA_BYTES = 64


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


def _build_hvcc(components: _HvccComponents, sps_bytes: bytes | None = None) -> bytes:
    """
    Construit un CodecPrivate ``hvcC`` ISO/IEC 14496-15 minimal.

    Pour un muxage Matroska (qui fournit le bitstream en annexB via
    SimpleBlock), de nombreux champs hvcC peuvent rester à 0/défaut tant
    que les NAL arrays VPS/SPS/PPS sont corrects. C'est ce que mkvmerge
    fait pour les pistes HEVC en mode "raw HEVC → MKV".
    """
    _ = sps_bytes  # extension future : extraire les vrais champs depuis le SPS

    # Header minimaliste hvcC (23 octets) + arrays NAL.
    out = bytearray()
    out.append(1)             # configurationVersion
    # general_profile_space(2)|tier_flag(1)|profile_idc(5)
    out.append(0x21)          # profile_space=0, tier_flag=0, profile_idc=1 (Main)
    out.extend(b"\x00\x00\x00\x00")     # general_profile_compatibility_flags
    out.extend(b"\x00\x00\x00\x00\x00\x00")  # general_constraint_indicator_flags
    out.append(0x5A)          # general_level_idc (level 9.0 = laisse lecteurs libres)
    # min_spatial_segmentation_idc (12 bits, padded)
    out.extend(b"\xF0\x00")
    out.append(0xFC)          # parallelismType (fields padded)
    out.append(0xFC)          # chromaFormat (4:2:0 par défaut, padded)
    out.append(0xF8)          # bitDepthLumaMinus8
    out.append(0xF8)          # bitDepthChromaMinus8
    out.extend(b"\x00\x00")   # avgFrameRate
    # constantFrameRate(2)|numTemporalLayers(3)|temporalIdNested(1)|lengthSizeMinusOne(2)
    # lengthSizeMinusOne = 3 → tailles NAL sur 4 octets dans CodecPrivate (ISO BMFF).
    # Mais en Matroska on émet du annexB dans les SimpleBlocks, donc cette valeur
    # n'est pas critique. mkvmerge met 3.
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
            f"({total_size}) — augmenter _SEEK_HEAD_RESERVED_BYTES."
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
    timestamp_ms: int         # Timestamp absolu du Cluster (ms)
    cue_points: list[tuple[int, int]]  # (frame_pts_ms, relative_block_offset)


def _build_cues(clusters: list[_ClusterRecord], track_number: int) -> bytes:
    """
    Construit l'élément Cues à partir des records de clusters.

    Pour limiter la taille, on n'écrit qu'un CuePoint par keyframe (les
    clusters ont un cue_points liste pour les keyframes ; en pratique,
    1 CuePoint = 1 Cluster est suffisant pour le seek MKV courant).
    """
    points: list[bytes] = []
    for cluster in clusters:
        for frame_ms, relative_position in cluster.cue_points:
            cue_track_pos = element(CUE_TRACK_POSITIONS_ID, b"".join([
                uint_element(CUE_TRACK_ID, track_number),
                uint_element(CUE_CLUSTER_POSITION_ID, cluster.relative_offset),
                uint_element(CUE_RELATIVE_POSITION_ID, relative_position),
            ]))
            point = element(CUE_POINT_ID, b"".join([
                uint_element(CUE_TIME_ID, frame_ms),
                cue_track_pos,
            ]))
            points.append(point)
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
        writing_app: str = "Muxiveo",
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
        from core.workflows.matroska_mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack
        from core.workflows.matroska_reader import MatroskaBlock, MatroskaReader, MatroskaTrack, read_element
        from core.workflows.matroska_writer import MatroskaWriter

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
        packets = tuple(
            MatroskaMuxPacket(
                track_number,
                MatroskaBlock(
                    track_number=track_number,
                    timestamp_ms=pts,
                    flags=SIMPLE_BLOCK_FLAG_KEYFRAME if access_unit.is_keyframe else 0,
                    payload=access_unit.payload,
                    duration_ms=duration,
                    timestamp_ns=pts * 1_000_000,
                    duration_ns=duration * 1_000_000,
                ),
            )
            for access_unit, pts, duration in zip(access_units, pts_seq.pts_ms, pts_seq.durations_ms)
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

    def _write_clusters(
        self,
        *,
        fh: BinaryIO,
        payload_start: int,
        access_units: list[HevcAccessUnit],
        pts_seq: TimestampSequence,
        track_number: int,
        clusters: list[_ClusterRecord],
    ) -> None:
        idx = 0
        n = len(access_units)
        while idx < n:
            cluster_start_in_segment = fh.tell() - payload_start
            cluster_pts_ms = pts_seq.pts_ms[idx]

            # Taille de cluster : on prend frames_per_cluster mais on coupe
            # si l'offset relatif du SimpleBlock dépasse int16 (32 s).
            cluster_aus: list[tuple[int, int, HevcAccessUnit]] = []
            cue_points: list[tuple[int, int]] = []
            block_offsets: list[int] = []
            written_payload = bytearray()

            for j in range(self._frames_per_cluster):
                if idx + j >= n:
                    break
                au = access_units[idx + j]
                pts = pts_seq.pts_ms[idx + j]
                offset = pts - cluster_pts_ms
                if not -32768 <= offset <= 32767:
                    # Trop loin du timestamp de cluster → on referme le
                    # cluster ici pour respecter int16.
                    break
                block = _build_simple_block(
                    track_number=track_number,
                    timestamp_offset=offset,
                    payload=au.payload,
                    is_keyframe=au.is_keyframe,
                )
                # Position du SimpleBlock relative au début du Cluster
                # *avant* d'écrire le payload Cluster (utile pour Cues).
                rel_offset = len(written_payload)
                block_offsets.append(rel_offset)
                written_payload.extend(block)
                cluster_aus.append((idx + j, pts, au))

            if not cluster_aus:
                # Sécurité : ne jamais boucler infiniment.
                raise RuntimeError(
                    f"AU {idx} ne tient dans aucun cluster (offset > int16)."
                )

            # Cluster.Timestamp + SimpleBlocks
            cluster_payload = (
                uint_element(TIMESTAMP_ID, cluster_pts_ms)
                + bytes(written_payload)
            )
            cluster_bytes = element(CLUSTER_ID, cluster_payload)
            fh.write(cluster_bytes)

            # On émet 1 cue point par cluster (sur le 1er keyframe trouvé,
            # ou à défaut le 1er AU du cluster).
            keyframe_in_cluster = next(
                ((global_idx, pts) for (global_idx, pts, au) in cluster_aus if au.is_keyframe),
                None,
            )
            if keyframe_in_cluster is None:
                keyframe_in_cluster = (cluster_aus[0][0], cluster_aus[0][1])
            cue_points.append((keyframe_in_cluster[1], 0))

            clusters.append(
                _ClusterRecord(
                    relative_offset=cluster_start_in_segment,
                    timestamp_ms=cluster_pts_ms,
                    cue_points=cue_points,
                )
            )
            idx += len(cluster_aus)


__all__ = [
    "MatroskaNativeMuxResult",
    "MatroskaNativeMuxer",
]
