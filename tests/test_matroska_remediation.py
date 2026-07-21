"""Tests de remédiation du backend Matroska natif (PLAN_Rem_NativeMKV).

Couvre : bit keyframe restauré (BlockGroup → SimpleBlock), positions de Cues
relatives au payload du Cluster, index des sous-titres avec CueDuration,
sélection des Cues par TrackType (multi-vidéo, audio-only, subtitle-only),
taille de Segment connue, SeekHead complet, durée dérivée de DefaultDuration,
BCP-47 préservé/régénéré, WritingApp Muxiveo, SegmentUID enrichi, resync des
SeekPosition de l'éditeur segment_info et hvcC dérivé du SPS.
"""

from dataclasses import replace
from io import BytesIO
from pathlib import Path

from core.matroska import ebml
from core.matroska.assembly import (
    MatroskaAssemblyPlan,
    MatroskaAssemblyTrack,
    assembly_output_contract,
    compile_assembly_plan,
)
from core.matroska.ids import (
    CHAPTERS_ID, CHAPTER_ATOM_ID, CHAPTER_TIME_START_ID, CHAPTER_UID_ID,
    CODEC_ID_ID, CUES_ID, CUE_CLUSTER_POSITION_ID, CUE_DURATION_ID,
    CUE_POINT_ID, CUE_RELATIVE_POSITION_ID, CUE_TIME_ID, CUE_TRACK_ID,
    CUE_TRACK_POSITIONS_ID, DEFAULT_DURATION_ID, EDITION_ENTRY_ID,
    INFO_ID, LANGUAGE_BCP47_ID, LANGUAGE_ID, SEEK_HEAD_ID, SEEK_ID,
    SEEK_ID_FIELD_ID, SEEK_POSITION_ID, TRACKS_ID, TRACK_NUMBER_ID,
    TRACK_TYPE_ID, TRACK_UID_ID,
)
from core.matroska.mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack
from core.matroska.reader import (
    MatroskaBlock, MatroskaReader, MatroskaTrack, iter_children,
    payload_children, read_element,
)
from core.matroska.writer import MatroskaWriter
from core.version import WRITING_APPLICATION_TAG


def _track(number: int, uid: int, codec: str, kind: int, *, extra: bytes = b"") -> MatroskaTrack:
    raw = b"".join((
        ebml.uint_element(TRACK_NUMBER_ID, number),
        ebml.uint_element(TRACK_UID_ID, uid),
        ebml.uint_element(TRACK_TYPE_ID, kind),
        ebml.ascii_element(CODEC_ID_ID, codec),
        extra,
    ))
    default_duration = 0
    for element_id, value in payload_children(raw):
        if element_id == DEFAULT_DURATION_ID:
            default_duration = int.from_bytes(value, "big")
    return MatroskaTrack(
        number, uid, kind, codec, b"", "", "und", "", raw,
        default_duration_ns=default_duration,
    )


def _simple_chapters() -> bytes:
    atom = ebml.element(CHAPTER_ATOM_ID, b"".join((
        ebml.uint_element(CHAPTER_UID_ID, 11),
        ebml.uint_element(CHAPTER_TIME_START_ID, 0),
    )))
    return ebml.element(CHAPTERS_ID, ebml.element(EDITION_ENTRY_ID, atom))


def _cue_entries(path: Path) -> list[tuple[int, int, int, int, int | None]]:
    """(time, track, cluster_pos, relative_pos, duration|None) depuis les Cues."""
    reader = MatroskaReader(path)
    cues = next(item for item in reader.top_level() if item.element_id == CUES_ID)
    out: list[tuple[int, int, int, int, int | None]] = []
    for point_id, point in payload_children(reader.payload(cues)):
        if point_id != CUE_POINT_ID:
            continue
        cue_time = 0
        for child_id, child in payload_children(point):
            if child_id == CUE_TIME_ID:
                cue_time = int.from_bytes(child, "big")
            elif child_id == CUE_TRACK_POSITIONS_ID:
                fields = dict(payload_children(child))
                duration_raw = fields.get(CUE_DURATION_ID)
                out.append((
                    cue_time,
                    int.from_bytes(fields[CUE_TRACK_ID], "big"),
                    int.from_bytes(fields[CUE_CLUSTER_POSITION_ID], "big"),
                    int.from_bytes(fields[CUE_RELATIVE_POSITION_ID], "big"),
                    int.from_bytes(duration_raw, "big") if duration_raw else None,
                ))
    return out


def _block_positions(path: Path) -> dict[tuple[int, int], tuple[int, int]]:
    """(cluster_pos, payload_relative_pos) par (piste, timestamp) des blocks."""
    reader = MatroskaReader(path)
    segment = reader.segment()
    size = path.stat().st_size
    positions: dict[tuple[int, int], tuple[int, int]] = {}
    with path.open("rb") as fh:
        for cluster in reader.top_level():
            if cluster.element_id != MatroskaReader.CLUSTER_ID:
                continue
            cluster_time = 0
            fh.seek(cluster.payload_offset)
            for child in iter_children(fh, cluster, file_size=size):
                relative = child.offset - cluster.payload_offset
                if child.element_id == MatroskaReader.TIMESTAMP_ID and child.size is not None:
                    cluster_time = int.from_bytes(reader.payload(child), "big")
                elif child.element_id in (MatroskaReader.SIMPLE_BLOCK_ID, MatroskaReader.BLOCK_GROUP_ID):
                    raw = reader.payload(child)
                    if child.element_id == MatroskaReader.BLOCK_GROUP_ID:
                        inner = dict(payload_children(raw))
                        raw = inner[MatroskaReader.BLOCK_ID]
                    stream = BytesIO(raw)
                    first = stream.read(1)[0]
                    length = 1
                    while not first & (0x80 >> (length - 1)):
                        length += 1
                    track = first & (0xFF >> length)
                    for byte in stream.read(length - 1):
                        track = (track << 8) | byte
                    offset = int.from_bytes(stream.read(2), "big", signed=True)
                    positions[(track, cluster_time + offset)] = (
                        cluster.offset - segment.payload_offset,
                        relative,
                    )
    return positions


# ---------------------------------------------------------------------------
# Writer : flags, cues, durée, taille de segment, SeekHead
# ---------------------------------------------------------------------------


def test_blockgroup_keyframe_regains_simpleblock_flag(tmp_path: Path) -> None:
    """Une keyframe issue d'un BlockGroup source garde son bit 0x80 en SimpleBlock."""
    video = _track(1, 10, "V_MPEG4/ISO/AVC", 1)
    track = MatroskaMuxTrack(Path("v.mkv"), video, 1, 20)
    packets = (
        MatroskaMuxPacket(1, MatroskaBlock(1, 0, 0x00, b"KEY", is_keyframe=True), 0),
        MatroskaMuxPacket(1, MatroskaBlock(1, 40, 0x00, b"P", references=(-40,), is_keyframe=False), 1),
        MatroskaMuxPacket(1, MatroskaBlock(1, 80, 0x00, b"KEY2", is_keyframe=True), 2),
    )
    output = tmp_path / "kf.mkv"
    MatroskaWriter().write(MatroskaMuxPlan(output, (track,), packets))
    observed = [
        (block.timestamp_ms, bool(block.flags & 0x80), block.is_keyframe)
        for block in MatroskaReader(output).blocks()
    ]
    assert observed == [(0, True, True), (40, False, False), (80, True, True)]


def test_cue_relative_position_is_cluster_payload_relative(tmp_path: Path) -> None:
    """CueRelativePosition == offset du block dans le payload du Cluster (RFC 9559)."""
    video = _track(1, 10, "V_MPEG4/ISO/AVC", 1)
    track = MatroskaMuxTrack(Path("v.mkv"), video, 1, 20)
    packets = tuple(
        MatroskaMuxPacket(1, MatroskaBlock(1, index * 40, 0x80 if index % 2 == 0 else 0x00, b"x" * 24), index)
        for index in range(6)
    )
    output = tmp_path / "cues.mkv"
    MatroskaWriter().write(MatroskaMuxPlan(output, (track,), packets))
    positions = _block_positions(output)
    entries = _cue_entries(output)
    assert entries, "Cues attendues pour les keyframes vidéo"
    for cue_time, cue_track, cluster_pos, relative_pos, _duration in entries:
        assert positions[(cue_track, cue_time)] == (cluster_pos, relative_pos)


def test_subtitle_blocks_are_indexed_with_duration(tmp_path: Path) -> None:
    """Chaque entrée sous-titre est indexée dans les Cues avec sa durée."""
    video = _track(1, 10, "V_MPEG4/ISO/AVC", 1)
    subs = _track(2, 11, "S_TEXT/UTF8", 17)
    tracks = (
        MatroskaMuxTrack(Path("v.mkv"), video, 1, 20),
        MatroskaMuxTrack(Path("s.mkv"), subs, 2, 21),
    )
    packets = (
        MatroskaMuxPacket(1, MatroskaBlock(1, 0, 0x80, b"video"), 0),
        MatroskaMuxPacket(2, MatroskaBlock(2, 500, 0x00, b"Bonjour", duration_ms=1500), 0),
        MatroskaMuxPacket(2, MatroskaBlock(2, 2500, 0x00, b"Suite", duration_ms=1000), 1),
    )
    output = tmp_path / "subs.mkv"
    MatroskaWriter().write(MatroskaMuxPlan(output, tracks, packets))
    subtitle_entries = [entry for entry in _cue_entries(output) if entry[1] == 2]
    assert [(entry[0], entry[4]) for entry in subtitle_entries] == [(500, 1500), (2500, 1000)]
    positions = _block_positions(output)
    for cue_time, cue_track, cluster_pos, relative_pos, _duration in subtitle_entries:
        assert positions[(cue_track, cue_time)] == (cluster_pos, relative_pos)


def test_multi_video_cues_index_all_video_tracks(tmp_path: Path) -> None:
    """MKV-S1 : les keyframes de toutes les pistes vidéo sont indexées."""
    video_a = _track(1, 10, "V_MPEG4/ISO/AVC", 1)
    video_b = _track(2, 11, "V_MPEGH/ISO/HEVC", 1)
    tracks = (
        MatroskaMuxTrack(Path("a.mkv"), video_a, 1, 20),
        MatroskaMuxTrack(Path("b.mkv"), video_b, 2, 21),
    )
    packets = (
        MatroskaMuxPacket(1, MatroskaBlock(1, 0, 0x80, b"A0"), 0),
        MatroskaMuxPacket(2, MatroskaBlock(2, 0, 0x80, b"B0"), 0),
        MatroskaMuxPacket(1, MatroskaBlock(1, 40, 0x00, b"A1"), 1),
        MatroskaMuxPacket(2, MatroskaBlock(2, 40, 0x80, b"B1"), 1),
    )
    output = tmp_path / "multivideo.mkv"
    MatroskaWriter().write(MatroskaMuxPlan(output, tracks, packets))
    by_track: dict[int, list[int]] = {}
    for cue_time, cue_track, _cluster, _relative, _duration in _cue_entries(output):
        by_track.setdefault(cue_track, []).append(cue_time)
    assert by_track == {1: [0], 2: [0, 40]}


def test_audio_only_primary_track_one_cue_per_cluster(tmp_path: Path) -> None:
    """MKV-S2 : audio-only — au plus un point de la piste primaire par Cluster."""
    audio_a = _track(1, 10, "A_AAC", 2)
    audio_b = _track(2, 11, "A_FLAC", 2)
    tracks = (
        MatroskaMuxTrack(Path("a.mkv"), audio_a, 1, 20),
        MatroskaMuxTrack(Path("b.mkv"), audio_b, 2, 21),
    )
    # Deux groupes temporels espacés de plus de 30 s → deux Clusters.
    times = [0, 20, 40, 60] + [40_000, 40_020, 40_040]
    packets = tuple(
        MatroskaMuxPacket(number, MatroskaBlock(number, ms, 0x80, b"pcm"), index)
        for index, (ms, number) in enumerate(
            (ms, number) for ms in times for number in (1, 2)
        )
    )
    output = tmp_path / "audio-only.mkv"
    MatroskaWriter().write(MatroskaMuxPlan(output, tracks, packets))
    entries = _cue_entries(output)
    assert [(cue_time, cue_track) for cue_time, cue_track, *_ in entries] == [
        (0, 1), (40_000, 1),
    ]


def test_subtitle_only_first_track_indexed_with_duration(tmp_path: Path) -> None:
    """MKV-S3 : subtitle-only — la première piste garde aussi ses CueDuration."""
    subs_a = _track(1, 10, "S_TEXT/UTF8", 17)
    subs_b = _track(2, 11, "S_TEXT/ASS", 17)
    tracks = (
        MatroskaMuxTrack(Path("a.mkv"), subs_a, 1, 20),
        MatroskaMuxTrack(Path("b.mkv"), subs_b, 2, 21),
    )
    packets = (
        MatroskaMuxPacket(1, MatroskaBlock(1, 0, 0x80, b"Un", duration_ms=1000), 0),
        MatroskaMuxPacket(2, MatroskaBlock(2, 100, 0x80, b"One", duration_ms=900), 0),
        MatroskaMuxPacket(1, MatroskaBlock(1, 2000, 0x80, b"Deux", duration_ms=500), 1),
    )
    output = tmp_path / "subs-only.mkv"
    MatroskaWriter().write(MatroskaMuxPlan(output, tracks, packets))
    entries = sorted(
        (cue_time, cue_track, duration)
        for cue_time, cue_track, _cluster, _relative, duration in _cue_entries(output)
    )
    assert entries == [(0, 1, 1000), (100, 2, 900), (2000, 1, 500)]


def test_segment_size_is_finalized(tmp_path: Path) -> None:
    """La taille du Segment est patchée (plus de taille inconnue)."""
    video = _track(1, 10, "V_MPEG4/ISO/AVC", 1)
    output = tmp_path / "size.mkv"
    MatroskaWriter().write(MatroskaMuxPlan(
        output,
        (MatroskaMuxTrack(Path("v.mkv"), video, 1, 20),),
        (MatroskaMuxPacket(1, MatroskaBlock(1, 0, 0x80, b"video")),),
    ))
    segment = MatroskaReader(output).segment()
    assert segment.size is not None
    assert segment.payload_offset + segment.size == output.stat().st_size


def test_seek_head_references_all_written_level1_elements(tmp_path: Path) -> None:
    """Le SeekHead référence Info, Tracks, les top-level opaques et les Cues."""
    video = _track(1, 10, "V_MPEG4/ISO/AVC", 1)
    output = tmp_path / "seek.mkv"
    MatroskaWriter().write(MatroskaMuxPlan(
        output,
        (MatroskaMuxTrack(Path("v.mkv"), video, 1, 20),),
        (MatroskaMuxPacket(1, MatroskaBlock(1, 0, 0x80, b"video")),),
        opaque_top_level=(_simple_chapters(),),
    ))
    reader = MatroskaReader(output)
    seek_head = next(item for item in reader.top_level() if item.element_id == SEEK_HEAD_ID)
    targets: dict[bytes, int] = {}
    for child_id, child in payload_children(reader.payload(seek_head)):
        if child_id != SEEK_ID:
            continue
        fields = dict(payload_children(child))
        targets[fields[SEEK_ID_FIELD_ID]] = int.from_bytes(fields[SEEK_POSITION_ID], "big")
    assert set(targets) == {INFO_ID, TRACKS_ID, CHAPTERS_ID, CUES_ID}
    segment = reader.segment()
    observed = {
        item.element_id: item.offset - segment.payload_offset
        for item in reader.top_level()
    }
    for element_id, position in targets.items():
        assert observed[element_id] == position


def test_duration_extends_to_last_packet_end_with_default_duration(tmp_path: Path) -> None:
    """Durée = fin du dernier paquet (DefaultDuration × laces, pas son début)."""
    default_duration_ns = 40_000_000  # 40 ms/frame
    video = _track(
        1, 10, "V_MPEG4/ISO/AVC", 1,
        extra=ebml.uint_element(DEFAULT_DURATION_ID, default_duration_ns),
    )
    track = MatroskaMuxTrack(Path("v.mkv"), video, 1, 20)
    packets = tuple(
        MatroskaMuxPacket(1, MatroskaBlock(1, index * 40, 0x80, b"frame"), index)
        for index in range(5)
    )
    output = tmp_path / "duration.mkv"
    MatroskaWriter().write(MatroskaMuxPlan(output, (track,), packets))
    assert MatroskaReader(output).segment_duration_ns() == 4 * 40_000_000 + default_duration_ns


def test_duration_extends_to_trailing_subtitle(tmp_path: Path) -> None:
    """Un sous-titre débordant prolonge la durée du segment (parité muxeurs)."""
    video = _track(
        1, 10, "V_MPEG4/ISO/AVC", 1,
        extra=ebml.uint_element(DEFAULT_DURATION_ID, 40_000_000),
    )
    subs = _track(2, 11, "S_TEXT/UTF8", 17)
    tracks = (
        MatroskaMuxTrack(Path("v.mkv"), video, 1, 20),
        MatroskaMuxTrack(Path("s.mkv"), subs, 2, 21),
    )
    packets = (
        MatroskaMuxPacket(1, MatroskaBlock(1, 0, 0x80, b"frame"), 0),
        MatroskaMuxPacket(1, MatroskaBlock(1, 40, 0x80, b"frame"), 1),
        MatroskaMuxPacket(2, MatroskaBlock(2, 20, 0x00, b"Long sous-titre", duration_ms=5000), 0),
    )
    output = tmp_path / "trailing.mkv"
    MatroskaWriter().write(MatroskaMuxPlan(output, tracks, packets))
    assert MatroskaReader(output).segment_duration_ns() == 5_020_000_000


def test_duration_uses_subtitles_when_no_media_track(tmp_path: Path) -> None:
    """Fichier sous-titres seul : la durée reste dérivée des sous-titres."""
    subs = _track(1, 11, "S_TEXT/UTF8", 17)
    track = MatroskaMuxTrack(Path("s.mkv"), subs, 1, 21)
    packets = (
        MatroskaMuxPacket(1, MatroskaBlock(1, 500, 0x00, b"Texte", duration_ms=1500), 0),
    )
    output = tmp_path / "subsonly.mkv"
    MatroskaWriter().write(MatroskaMuxPlan(output, (track,), packets))
    assert MatroskaReader(output).segment_duration_ns() == 2_000_000_000


def test_duration_falls_back_to_last_observed_delta(tmp_path: Path) -> None:
    """Sans durée explicite ni DefaultDuration, la fin est estimée au dernier delta."""
    video = _track(1, 10, "V_MPEG4/ISO/AVC", 1)
    track = MatroskaMuxTrack(Path("v.mkv"), video, 1, 20)
    packets = (
        MatroskaMuxPacket(1, MatroskaBlock(1, 0, 0x80, b"a"), 0),
        MatroskaMuxPacket(1, MatroskaBlock(1, 40, 0x80, b"b"), 1),
        MatroskaMuxPacket(1, MatroskaBlock(1, 80, 0x80, b"c"), 2),
    )
    output = tmp_path / "delta.mkv"
    MatroskaWriter().write(MatroskaMuxPlan(output, (track,), packets))
    assert MatroskaReader(output).segment_duration_ns() == 120_000_000


# ---------------------------------------------------------------------------
# Assemblage : BCP-47, WritingApp, SegmentUID
# ---------------------------------------------------------------------------


def _write_source_with_bcp47(path: Path) -> None:
    video = _track(
        1, 10, "V_MPEG4/ISO/AVC", 1,
        extra=ebml.string_element(LANGUAGE_ID, "fre") + ebml.string_element(LANGUAGE_BCP47_ID, "fr"),
    )
    MatroskaWriter().write(MatroskaMuxPlan(
        path,
        (MatroskaMuxTrack(Path("v.mkv"), video, 1, 20, patch_language=False),),
        (MatroskaMuxPacket(1, MatroskaBlock(1, 0, 0x80, b"video")),),
    ))


def _compiled_plan(source: Path, output: Path, *, language_value: str | None, **plan_kwargs) -> MatroskaMuxPlan:
    assembly = MatroskaAssemblyPlan(
        output=output,
        ordered_tracks=(MatroskaAssemblyTrack(
            artifact=source, artifact_track_index=0, source_identity="test",
            language_value=language_value,
        ),),
        **plan_kwargs,
    )
    assembly = replace(assembly, expected_output_contract=assembly_output_contract(assembly))
    return compile_assembly_plan(assembly)


def test_bcp47_kept_when_base_language_matches(tmp_path: Path) -> None:
    """Patch de langue même base (fr-FR sur source fr) : BCP-47 source conservé."""
    source = tmp_path / "src.mkv"
    _write_source_with_bcp47(source)
    plan = _compiled_plan(source, tmp_path / "out.mkv", language_value="fr-FR")
    assert plan.tracks[0].language == "fre"
    assert plan.tracks[0].language_bcp47 == "fr"


def test_bcp47_regenerated_when_language_changes(tmp_path: Path) -> None:
    """Changement de langue : BCP-47 régénéré depuis la valeur demandée."""
    source = tmp_path / "src.mkv"
    _write_source_with_bcp47(source)
    plan = _compiled_plan(source, tmp_path / "out.mkv", language_value="deu")
    assert plan.tracks[0].language == "ger"
    assert plan.tracks[0].language_bcp47 == "de"


def test_writing_app_is_muxiveo_tag(tmp_path: Path) -> None:
    """La sortie native déclare Muxiveo comme WritingApp, pas l'app source."""
    source = tmp_path / "src.mkv"
    _write_source_with_bcp47(source)
    plan = _compiled_plan(source, tmp_path / "out.mkv", language_value=None)
    assert plan.writing_app == WRITING_APPLICATION_TAG
    output = tmp_path / "out.mkv"
    MatroskaWriter().write(plan)
    _muxing, writing = MatroskaReader(output).segment_info_apps()
    assert writing == WRITING_APPLICATION_TAG


def test_segment_uid_covers_opaque_metadata(tmp_path: Path) -> None:
    """SegmentUID stable pour un même plan, distinct dès que les métadonnées changent."""
    source = tmp_path / "src.mkv"
    _write_source_with_bcp47(source)
    base_a = _compiled_plan(source, tmp_path / "a.mkv", language_value=None)
    base_b = _compiled_plan(source, tmp_path / "b.mkv", language_value=None)
    assert base_a.segment_uid == base_b.segment_uid
    with_title = _compiled_plan(
        source, tmp_path / "c.mkv", language_value=None, segment_title="Autre titre",
    )
    assert with_title.segment_uid != base_a.segment_uid


# ---------------------------------------------------------------------------
# Éditeur segment_info : resynchronisation des SeekPosition
# ---------------------------------------------------------------------------


def test_resync_meta_seeks_fixes_stale_position(tmp_path: Path) -> None:
    """Une SeekPosition obsolète (off-by-one) est réalignée sur l'offset réel."""
    from core.matroska.editors.segment_info import MatroskaSegmentInfoHeaderEditor

    info = ebml.element(INFO_ID, b"".join((
        ebml.uint_element(bytes.fromhex("2AD7B1"), 1_000_000),
        ebml.string_element(bytes.fromhex("4D80"), "muxer"),
        ebml.string_element(bytes.fromhex("5741"), "writer"),
    )))
    tracks = ebml.element(TRACKS_ID, ebml.element(bytes.fromhex("AE"), b"".join((
        ebml.uint_element(TRACK_NUMBER_ID, 1),
        ebml.uint_element(TRACK_UID_ID, 10),
        ebml.uint_element(TRACK_TYPE_ID, 1),
        ebml.ascii_element(CODEC_ID_ID, "V_MPEG4/ISO/AVC"),
    ))))

    def seek_entry(target: bytes, position: int) -> bytes:
        return ebml.element(SEEK_ID, b"".join((
            ebml.binary_element(SEEK_ID_FIELD_ID, target),
            ebml.element(SEEK_POSITION_ID, position.to_bytes(2, "big")),
        )))

    # Positions réelles : SeekHead occupe l'espace [0, len(seek_head)).
    provisional = ebml.element(SEEK_HEAD_ID, seek_entry(INFO_ID, 0) + seek_entry(TRACKS_ID, 0))
    info_offset = len(provisional)
    tracks_offset = info_offset + len(info)
    # SeekPosition Info volontairement décalée d'un octet (bug historique).
    seek_head = ebml.element(
        SEEK_HEAD_ID,
        seek_entry(INFO_ID, info_offset + 1) + seek_entry(TRACKS_ID, tracks_offset),
    )
    assert len(seek_head) == len(provisional)
    segment_payload = seek_head + info + tracks
    header = ebml.element(bytes.fromhex("1A45DFA3"), b"".join((
        ebml.uint_element(bytes.fromhex("4286"), 1),
        ebml.uint_element(bytes.fromhex("42F7"), 1),
        ebml.uint_element(bytes.fromhex("42F2"), 4),
        ebml.uint_element(bytes.fromhex("42F3"), 8),
        ebml.string_element(bytes.fromhex("4282"), "matroska"),
        ebml.uint_element(bytes.fromhex("4287"), 4),
        ebml.uint_element(bytes.fromhex("4285"), 2),
    )))
    path = tmp_path / "stale.mkv"
    path.write_bytes(header + ebml.element(bytes.fromhex("18538067"), segment_payload))

    editor = MatroskaSegmentInfoHeaderEditor()
    with path.open("r+b") as fh:
        state = editor._analyze_file(fh, parse_fast=False)
        editor._resync_meta_seeks(fh, state)

    reader = MatroskaReader(path)
    segment = reader.segment()
    observed = {
        item.element_id: item.offset - segment.payload_offset
        for item in reader.top_level()
    }
    seek_head_element = next(item for item in reader.top_level() if item.element_id == SEEK_HEAD_ID)
    for child_id, child in payload_children(reader.payload(seek_head_element)):
        if child_id != SEEK_ID:
            continue
        fields = dict(payload_children(child))
        target = fields[SEEK_ID_FIELD_ID]
        position = int.from_bytes(fields[SEEK_POSITION_ID], "big")
        assert observed[target] == position


# ---------------------------------------------------------------------------
# hvcC dérivé du SPS
# ---------------------------------------------------------------------------


def test_hvcc_header_matches_source_sps() -> None:
    """Le hvcC reconstruit reprend profil/niveau/chroma/bit-depth du SPS réel."""
    from core.matroska.native_muxer import _HvccComponents, _build_hvcc

    corpus = Path(__file__).parent / "corpus" / "matroska" / "hevc_flac_ass_hdr10.mkv"
    source = MatroskaReader(corpus).tracks()[0]
    original = source.codec_private
    assert original and original[0] == 1, "hvcC source attendu dans le corpus"

    # Extraction des arrays VPS/SPS/PPS du hvcC d'origine.
    arrays: dict[int, list[bytes]] = {}
    cursor = 23
    for _ in range(original[22]):
        nal_type = original[cursor] & 0x3F
        count = int.from_bytes(original[cursor + 1:cursor + 3], "big")
        cursor += 3
        for _ in range(count):
            size = int.from_bytes(original[cursor:cursor + 2], "big")
            cursor += 2
            arrays.setdefault(nal_type, []).append(original[cursor:cursor + size])
            cursor += size

    rebuilt = _build_hvcc(_HvccComponents(
        vps=arrays.get(32, []), sps=arrays.get(33, []), pps=arrays.get(34, []),
    ))
    # profile_space/tier/profile_idc, compatibility, constraints, level.
    assert rebuilt[1:13] == original[1:13]
    # chroma_format_idc et bit depths (2/3 bits utiles).
    assert rebuilt[16] & 0x03 == original[16] & 0x03
    assert rebuilt[17] & 0x07 == original[17] & 0x07
    assert rebuilt[18] & 0x07 == original[18] & 0x07


def test_read_element_helper_available() -> None:
    """Garde d'API : read_element reste exposé pour les outils de diagnostic."""
    stream = BytesIO(ebml.uint_element(TRACK_NUMBER_ID, 1))
    item = read_element(stream, limit=None)
    assert item is not None and item.element_id == TRACK_NUMBER_ID


def test_validation_tolerates_subtitles_only_when_flagged(tmp_path: Path) -> None:
    """`allow_unexpected_subtitles` n'assouplit QUE les pistes sous-titres."""
    from dataclasses import replace as dc_replace

    from core.matroska.contract import ExpectedMatroskaTrack, MatroskaOutputContract
    from core.matroska.validation import validate_matroska_output

    video = _track(1, 10, "V_MPEG4/ISO/AVC", 1)
    subs = _track(2, 11, "S_TEXT/UTF8", 17)
    tracks = (
        MatroskaMuxTrack(Path("v.mkv"), video, 1, 20),
        MatroskaMuxTrack(Path("s.mkv"), subs, 2, 21),
    )
    packets = (
        MatroskaMuxPacket(1, MatroskaBlock(1, 0, 0x80, b"video"), 0),
        MatroskaMuxPacket(2, MatroskaBlock(2, 100, 0x00, b"Sub", duration_ms=500), 0),
    )
    output = tmp_path / "wild.mkv"
    MatroskaWriter().write(MatroskaMuxPlan(output, tracks, packets))

    strict = MatroskaOutputContract(
        track_types=("video",),
        expected_tracks=(ExpectedMatroskaTrack("video", require_packets=True),),
        duration_coherent=False,
    )
    assert any("Pistes de sortie inattendues" in error
               for error in validate_matroska_output(output, strict))

    relaxed = dc_replace(strict, allow_unexpected_subtitles=True)
    assert validate_matroska_output(output, relaxed) == []

    # Une piste audio surnuméraire reste signalée même avec le flag.
    audio_only = dc_replace(
        strict,
        track_types=("video", "audio"),
        expected_tracks=(
            ExpectedMatroskaTrack("video", require_packets=True),
            ExpectedMatroskaTrack("audio", require_packets=True),
        ),
        allow_unexpected_subtitles=True,
    )
    assert any("Pistes de sortie inattendues" in error
               for error in validate_matroska_output(output, audio_only))


def test_relaxed_subtitles_still_validate_known_tracks(tmp_path: Path) -> None:
    """`allow_unexpected_subtitles` : les pistes connues restent validées.

    Même avec un sous-titre inconnu intercalé, une sortie DoVi sans
    BlockAdditionMapping ou un flag erroné doit être rejetée.
    """
    from dataclasses import replace as dc_replace

    from core.matroska.contract import (
        ExpectedMatroskaTrack, ExpectedTrackFlags, MatroskaOutputContract,
    )
    from core.matroska.validation import validate_matroska_output

    video = _track(1, 10, "V_MPEGH/ISO/HEVC", 1)
    subs = _track(2, 11, "S_TEXT/UTF8", 17)
    tracks = (
        MatroskaMuxTrack(Path("v.mkv"), video, 1, 20, flag_default=True),
        MatroskaMuxTrack(Path("s.mkv"), subs, 2, 21),
    )
    packets = (
        MatroskaMuxPacket(1, MatroskaBlock(1, 0, 0x80, b"video"), 0),
        MatroskaMuxPacket(2, MatroskaBlock(2, 100, 0x00, b"Sub", duration_ms=500), 0),
    )
    output = tmp_path / "dovi_relaxed.mkv"
    MatroskaWriter().write(MatroskaMuxPlan(output, tracks, packets))

    dovi_contract = MatroskaOutputContract(
        track_types=("video",),
        expected_tracks=(ExpectedMatroskaTrack(
            "video", require_packets=True, require_block_addition_mapping=True,
        ),),
        duration_coherent=False,
        allow_unexpected_subtitles=True,
    )
    errors = validate_matroska_output(output, dovi_contract)
    assert any("BlockAdditionMapping" in error for error in errors)

    flag_contract = dc_replace(
        dovi_contract,
        expected_tracks=(ExpectedMatroskaTrack(
            "video", require_packets=True,
            flags=ExpectedTrackFlags(default=False),
        ),),
    )
    errors = validate_matroska_output(output, flag_contract)
    assert any("Flag default" in error for error in errors)

    # Sous-titre attendu absent de la sortie : signalé même en mode assoupli.
    missing_sub_contract = dc_replace(
        dovi_contract,
        track_types=("video", "subtitle", "subtitle"),
        expected_tracks=(
            ExpectedMatroskaTrack("video", require_packets=True),
            ExpectedMatroskaTrack("subtitle"),
            ExpectedMatroskaTrack("subtitle"),
        ),
    )
    errors = validate_matroska_output(output, missing_sub_contract)
    assert any("Sous-titres attendus manquants" in error for error in errors)


def test_contract_without_expected_attachment() -> None:
    """Retrait d'une attente d'attachment (cover TMDB non téléchargée)."""
    from core.matroska.contract import (
        ExpectedMatroskaAttachment, MatroskaOutputContract, without_expected_attachment,
    )

    contract = MatroskaOutputContract(
        track_types=("video",),
        attachment_names=("cover.jpg", "font.ttf"),
        expected_attachments=(
            ExpectedMatroskaAttachment(name="cover.jpg"),
            ExpectedMatroskaAttachment(name="font.ttf"),
        ),
        strict_attachment_names=True,
    )
    adjusted = without_expected_attachment(contract, "cover.jpg")
    assert adjusted.attachment_names == ("font.ttf",)
    assert tuple(item.name for item in adjusted.expected_attachments) == ("font.ttf",)
    assert adjusted.strict_attachment_names is True


def test_native_extra_attachment_cover_is_canonical(tmp_path: Path) -> None:
    """Assembleur natif : COVER.JPG attaché sous le nom canonique cover.jpg
    (même règle que les commandes FFmpeg — détection de cover identique
    quel que soit le backend)."""
    source = tmp_path / "src.mkv"
    _write_source_with_bcp47(source)
    cover = tmp_path / "COVER.JPG"
    cover.write_bytes(b"\xff\xd8\xff\xe0fake")
    output = tmp_path / "out.mkv"
    plan = _compiled_plan(
        source, output, language_value=None,
        extra_attachment_files=(cover,),
    )
    MatroskaWriter().write(plan)
    names = [attachment.name for attachment in MatroskaReader(output).attachments()]
    assert names == ["cover.jpg"]
