"""Contrat d'assemblage Matroska partagé entre remux et encode.

``MatroskaAssemblyPlan`` décrit la sortie finale : pistes ordonnées (chacune
adossée à un artefact Matroska lisible), chapitres, tags, attachments, titre
de segment et contrat de validation. Le remux et l'encode compilent vers ce
même plan ; :func:`compile_assembly_plan` le traduit en
:class:`MatroskaMuxPlan` consommé par le writer bas niveau.

L'assembleur final ne lance jamais implicitement un encode : les services de
préparation restent responsables de transformer une source non-Matroska ou
une piste encodée en artefact Matroska.
"""

from __future__ import annotations

import hashlib
import heapq
import mimetypes
from collections.abc import Iterator
from dataclasses import dataclass
from math import gcd
from pathlib import Path

from core.version import APP_VERSION_LABEL, WRITING_APPLICATION_TAG

from .contract import (
    ExpectedMatroskaAttachment,
    ExpectedMatroskaTrack,
    ExpectedTrackFlags,
    MatroskaOutputContract,
)
from .ids import CHAPTERS_ID, TAGS_ID
from .language import bcp47_for_language, matroska_legacy_language
from .mux_plan import (
    MatroskaMuxPacket,
    MatroskaMuxPlan,
    MatroskaMuxTrack,
    deterministic_source_identity,
    deterministic_uid,
    deterministic_uid128,
)
from .reader import MatroskaAttachment, MatroskaReader
from .validation import TRACK_TYPE_LABELS
from .writer import (
    build_attachments_element,
    build_chapters_element,
    build_track_statistics_tags_element,
    build_tags_element,
    rewrite_tag_target_uids,
)


def canonical_attachment_output_name(path: Path) -> str:
    """Nom de sortie d'un attachment fichier — commun à TOUS les backends.

    Les covers gardent leur extension avec un stem canonique en minuscules
    (convention cover-art Matroska : ``cover.jpg``/``cover.png``). Commandes
    de muxage, assembleur natif et contrats de validation doivent produire
    et attendre exactement le même nom, quel que soit le backend.
    """
    if path.stem.lower() == "cover" and path.suffix:
        return f"cover{path.suffix.lower()}"
    return path.name


@dataclass(frozen=True)
class MatroskaTrackFlags:
    """Flags MKV explicites d'une piste de sortie."""

    enabled: bool = True
    default: bool = False
    forced: bool = False
    hearing_impaired: bool = False
    visual_impaired: bool = False
    original: bool = False
    commentary: bool = False


@dataclass(frozen=True)
class MatroskaAssemblyTrack:
    """Piste préparée : artefact Matroska concret + identité + métadonnées.

    ``language_value``, ``name`` et ``flags`` à ``None`` préservent les
    valeurs du TrackEntry de l'artefact (copie sans édition) ; une valeur
    explicite est écrite dans le TrackEntry de sortie.
    """

    artifact: Path
    artifact_track_index: int
    source_identity: str
    language_value: str | None = None
    name: str | None = None
    flags: MatroskaTrackFlags | None = None
    time_shift_ms: int = 0
    #: Provenance complémentaire pour reconstruire les UIDs de façon stable
    #: (ex. ``"audio:2:aac"`` pour une variante matérialisée).
    provenance: str = ""


@dataclass(frozen=True)
class MatroskaAssemblyAttachment:
    """Attachment copié depuis un artefact Matroska existant."""

    artifact: Path
    local_index: int
    source_identity: str


@dataclass(frozen=True)
class MatroskaAssemblyPlan:
    """Plan d'assemblage final commun remux/encode."""

    output: Path
    ordered_tracks: tuple[MatroskaAssemblyTrack, ...]
    attachments: tuple[MatroskaAssemblyAttachment, ...] = ()
    extra_attachment_files: tuple[Path, ...] = ()
    #: None → chapitres copiés depuis ``chapter_source`` (si défini) ;
    #: tuple → chapitres reconstruits depuis ces entrées (vide = aucun).
    chapter_entries: tuple | None = None
    chapter_source: Path | None = None
    tag_overrides: dict[str, str] | None = None
    tag_copy_sources: tuple[Path, ...] = ()
    #: ``None`` préserve le titre source ; une chaîne vide le supprime.
    segment_title: str | None = None
    title_tag_value: str = ""
    writing_app: str = ""
    #: Ordre d'énumération des artefacts pour la fusion des paquets (stabilité
    #: inter-sources) ; vide → premier-vu dans ``ordered_tracks``.
    artifact_order: tuple[Path, ...] = ()
    segment_info_source: Path | None = None
    expected_output_contract: MatroskaOutputContract | None = None


def assembly_output_contract(
    plan: MatroskaAssemblyPlan,
    *,
    require_block_addition_mapping: bool | set[int] = False,
) -> MatroskaOutputContract:
    """Contrat de sortie dérivé du plan (types dans l'ordre d'assemblage)."""
    if plan.expected_output_contract is not None:
        return plan.expected_output_contract
    readers: dict[Path, MatroskaReader] = {}
    types: list[str] = []
    expected_tracks: list[ExpectedMatroskaTrack] = []
    required_mapping_indexes = (
        set(require_block_addition_mapping)
        if isinstance(require_block_addition_mapping, set)
        else set()
    )
    for output_index, track in enumerate(plan.ordered_tracks):
        reader = readers.setdefault(track.artifact, MatroskaReader(track.artifact))
        native_tracks = reader.tracks()
        if not 0 <= track.artifact_track_index < len(native_tracks):
            raise ValueError(
                f"Piste d'assemblage introuvable : {track.artifact} #{track.artifact_track_index}"
            )
        source_track = native_tracks[track.artifact_track_index]
        track_type = TRACK_TYPE_LABELS.get(source_track.track_type, str(source_track.track_type))
        types.append(track_type)
        flags = track.flags
        expected_tracks.append(ExpectedMatroskaTrack(
            track_type=track_type,
            name=track.name if track.name is not None else source_track.name,
            language=(
                track.language_value
                if track.language_value is not None
                else (source_track.language_bcp47 or source_track.language)
            ),
            flags=ExpectedTrackFlags(
                enabled=flags.enabled if flags is not None else source_track.flag_enabled,
                default=flags.default if flags is not None else source_track.flag_default,
                forced=flags.forced if flags is not None else source_track.flag_forced,
                hearing_impaired=(flags.hearing_impaired if flags is not None else source_track.flag_hearing_impaired),
                visual_impaired=(flags.visual_impaired if flags is not None else source_track.flag_visual_impaired),
                original=flags.original if flags is not None else source_track.flag_original,
                commentary=flags.commentary if flags is not None else source_track.flag_commentary,
            ),
            require_packets=source_track.track_type in (1, 2),
            require_block_addition_mapping=(
                (bool(require_block_addition_mapping) and not required_mapping_indexes and source_track.track_type == 1)
                or output_index in required_mapping_indexes
            ),
        ))
    # Les chapitres ne sont attendus que si la source de chapitres en
    # contient réellement — keep_chapters sur une source sans chapitres ne
    # doit pas rendre le contrat inatteignable.
    expects_chapters = bool(plan.chapter_entries) or (
        plan.chapter_entries is None
        and plan.chapter_source is not None
        and bool(
            readers.setdefault(
                plan.chapter_source, MatroskaReader(plan.chapter_source),
            ).raw_top_level(CHAPTERS_ID)
        )
    )
    expected_attachments: list[ExpectedMatroskaAttachment] = []
    for extra in plan.extra_attachment_files:
        path = Path(extra)
        output_name = canonical_attachment_output_name(path)
        expected_attachments.append(ExpectedMatroskaAttachment(
            name=output_name,
            media_type=mimetypes.guess_type(output_name)[0],
            size=path.stat().st_size if path.is_file() else None,
        ))
    for attachment in plan.attachments:
        headers = readers.setdefault(
            attachment.artifact, MatroskaReader(attachment.artifact),
        ).attachment_headers()
        if 0 <= attachment.local_index < len(headers):
            header = headers[attachment.local_index]
            expected_attachments.append(ExpectedMatroskaAttachment(
                name=header.name,
                media_type=header.media_type,
                size=header.size,
            ))
    expects_tags = bool(plan.tag_overrides)
    if plan.tag_overrides is None:
        expects_tags = any(
            bool(readers.setdefault(source, MatroskaReader(source)).raw_top_level(TAGS_ID))
            for source in plan.tag_copy_sources
        ) or bool(plan.title_tag_value)
    return MatroskaOutputContract(
        track_types=tuple(types),
        expected_tracks=tuple(expected_tracks),
        expects_chapters=expects_chapters,
        expects_tags=expects_tags,
        attachment_names=tuple(item.name for item in expected_attachments),
        expected_attachments=tuple(expected_attachments),
        require_block_addition_mapping=bool(require_block_addition_mapping),
        strict_attachment_names=True,
    )


def compile_assembly_plan(plan: MatroskaAssemblyPlan) -> MatroskaMuxPlan:
    """Compile le plan d'assemblage vers le plan bas niveau du writer natif."""
    if not plan.ordered_tracks:
        raise ValueError("Plan d'assemblage sans piste.")
    if plan.expected_output_contract is None:
        raise ValueError(
            "MatroskaAssemblyPlan.expected_output_contract doit être compilé avant écriture."
        )

    # Lecteurs partagés : artefacts de pistes, chapitres, tags, attachments.
    reader_order: list[Path] = []

    def _register(path: Path | None) -> None:
        if path is not None and path not in reader_order:
            reader_order.append(path)

    for artifact in plan.artifact_order:
        _register(artifact)
    for track in plan.ordered_tracks:
        _register(track.artifact)
    _register(plan.segment_info_source)
    _register(plan.chapter_source if plan.chapter_entries is None else None)
    for source in plan.tag_copy_sources:
        _register(source)
    for attachment in plan.attachments:
        _register(attachment.artifact)

    readers = {path: MatroskaReader(path) for path in reader_order}
    native_tracks = {path: reader.tracks() for path, reader in readers.items()}

    timestamp_scale_ns = 0
    for reader in readers.values():
        timestamp_scale_ns = gcd(timestamp_scale_ns, reader.timestamp_scale_ns())
    for track in plan.ordered_tracks:
        timestamp_scale_ns = gcd(timestamp_scale_ns, abs(int(track.time_shift_ms or 0)) * 1_000_000)
    timestamp_scale_ns = timestamp_scale_ns or 1_000_000

    output_tracks: list[MatroskaMuxTrack] = []
    statistics_sources: list[tuple[int, Path, int, int, int]] = []
    track_uid_maps: dict[Path, dict[int, int]] = {}
    # artefact → numéro de piste source → [(piste sortie, offset ms)].
    packet_routes: dict[Path, dict[int, list[tuple[int, int]]]] = {}
    for output_index, track in enumerate(plan.ordered_tracks, start=1):
        artifact_tracks = native_tracks[track.artifact]
        if not 0 <= track.artifact_track_index < len(artifact_tracks):
            raise ValueError(
                f"Piste d'assemblage introuvable : {track.artifact} #{track.artifact_track_index}"
            )
        source_track = artifact_tracks[track.artifact_track_index]
        identity = (
            f"{track.source_identity}:{track.provenance}"
            if track.provenance
            else track.source_identity
        )
        uid_language = (
            track.language_value
            if track.language_value is not None
            else (source_track.language_bcp47 or source_track.language)
        )
        uid_name = track.name if track.name is not None else source_track.name
        uid_default = track.flags.default if track.flags is not None else source_track.flag_default
        uid_forced = track.flags.forced if track.flags is not None else source_track.flag_forced
        uid = deterministic_uid(
            identity, source_track.uid, output_index,
            uid_language, uid_name, uid_default, uid_forced,
        )
        track_uid_maps.setdefault(track.artifact, {})[source_track.uid] = uid

        if track.language_value is not None:
            legacy_language = matroska_legacy_language(track.language_value)
            source_bcp47 = source_track.language_bcp47 or ""
            if source_bcp47 and matroska_legacy_language(source_bcp47) == legacy_language:
                # Même langue de base : le BCP-47 source (plus précis) est
                # conservé tel quel.
                language_bcp47 = source_bcp47
            else:
                # Langue changée ou source sans BCP-47 : balise régénérée
                # depuis la valeur demandée — jamais supprimée silencieusement.
                language_bcp47 = bcp47_for_language(track.language_value)
            patch_language = True
        else:
            legacy_language, language_bcp47, patch_language = "und", "", False
        flags = track.flags or MatroskaTrackFlags()
        output_tracks.append(MatroskaMuxTrack(
            source=track.artifact, source_track=source_track,
            output_number=output_index, output_uid=uid,
            language=legacy_language,
            language_bcp47=language_bcp47,
            name=track.name if track.name is not None else "",
            flag_enabled=flags.enabled,
            flag_default=flags.default,
            flag_forced=flags.forced,
            flag_hearing_impaired=flags.hearing_impaired,
            flag_visual_impaired=flags.visual_impaired,
            flag_original=flags.original,
            flag_commentary=flags.commentary,
            patch_language=patch_language,
            patch_name=track.name is not None,
            patch_flags=track.flags is not None,
        ))
        offset = int(track.time_shift_ms or 0)
        statistics_sources.append((
            uid,
            track.artifact,
            source_track.number,
            offset * 1_000_000,
            source_track.default_duration_ns,
        ))
        packet_routes.setdefault(track.artifact, {}).setdefault(
            source_track.number, []).append((output_index, offset))

    def artifact_packet_stream(artifact: Path) -> Iterator[MatroskaMuxPacket]:
        """Une passe streaming sur les blocks d'un artefact (mémoire bornée)."""
        routes = packet_routes.get(artifact, {})
        for source_sequence, block in enumerate(readers[artifact].blocks()):
            targets = routes.get(block.track_number)
            if not targets:
                continue
            if block.lace_count > 1 and block.lace_index > 0:
                continue
            source_timestamp_ns = block.timestamp_ns if block.timestamp_ns is not None else block.timestamp_ms * 1_000_000
            for output_index, offset_ms in targets:
                shifted_timestamp_ns = source_timestamp_ns + offset_ms * 1_000_000
                if shifted_timestamp_ns < 0:
                    continue
                shifted = block if not offset_ms else block.__class__(**{
                    **block.__dict__,
                    "timestamp_ms": round(shifted_timestamp_ns / 1_000_000),
                    "timestamp_ns": shifted_timestamp_ns,
                })
                yield MatroskaMuxPacket(output_index, shifted, source_sequence)

    active_artifacts = [path for path in readers if packet_routes.get(path)]
    if len(active_artifacts) <= 1:
        packet_stream: Iterator[MatroskaMuxPacket] = (
            artifact_packet_stream(active_artifacts[0]) if active_artifacts else iter(())
        )
    else:
        # Ordre interne de chaque artefact préservé ; fusion inter-artefacts
        # par timestamp décalé (heapq.merge ne réordonne jamais un flux).
        packet_stream = heapq.merge(
            *(artifact_packet_stream(path) for path in active_artifacts),
            key=lambda packet: (
                packet.block.timestamp_ns
                if packet.block.timestamp_ns is not None
                else packet.block.timestamp_ms * 1_000_000
            ),
        )

    opaque: list[bytes] = []
    attachments: list[MatroskaAttachment] = []
    attachment_uid_maps: dict[Path, dict[int, int]] = {}
    for selected in plan.attachments:
        available = readers[selected.artifact].attachments()
        if not 0 <= selected.local_index < len(available):
            raise ValueError(
                f"Attachment d'assemblage introuvable : {selected.artifact} #{selected.local_index}"
            )
        item = available[selected.local_index]
        output_uid = deterministic_uid(selected.source_identity, item.uid, item.name)
        attachment_uid_maps.setdefault(selected.artifact, {})[item.uid] = output_uid
        attachments.append(MatroskaAttachment(
            uid=output_uid, name=item.name,
            media_type=item.media_type, description=item.description, data=item.data,
        ))
    for path in plan.extra_attachment_files:
        attachment_path = Path(path)
        output_name = canonical_attachment_output_name(attachment_path)
        attachments.append(MatroskaAttachment(
            uid=deterministic_uid(deterministic_source_identity(attachment_path), output_name),
            name=output_name,
            media_type=mimetypes.guess_type(attachment_path.name)[0] or "application/octet-stream",
            description="", data=attachment_path.read_bytes(),
        ))
    attachment_element = build_attachments_element(attachments)
    if attachment_element:
        opaque.append(attachment_element)

    if plan.chapter_entries is not None:
        chapter_element = build_chapters_element(list(plan.chapter_entries))
        if chapter_element:
            opaque.append(chapter_element)
    elif plan.chapter_source is not None:
        opaque.extend(readers[plan.chapter_source].raw_top_level(CHAPTERS_ID))

    if plan.tag_overrides is not None:
        tag_element = build_tags_element(dict(plan.tag_overrides))
        if tag_element:
            opaque.append(tag_element)
    else:
        for source in plan.tag_copy_sources:
            opaque.extend(
                rewrite_tag_target_uids(
                    raw,
                    track_uids=track_uid_maps.get(source, {}),
                    attachment_uids=attachment_uid_maps.get(source, {}),
                    drop_chapter_targets=plan.chapter_entries is not None,
                    drop_track_statistics=True,
                )
                for raw in readers[source].raw_top_level(TAGS_ID)
            )
        if plan.title_tag_value:
            title_tag = build_tags_element({"title": plan.title_tag_value.strip()})
            if title_tag:
                opaque.append(title_tag)

    # Regenerate BPS, DURATION, NUMBER_OF_FRAMES and NUMBER_OF_BYTES from the
    # selected output packets:
    # source values are stale after selection, offsets or a track remap.  This
    # remains a bounded streaming pass and does not materialize packet data.
    statistics_routes: dict[Path, dict[int, list[tuple[int, int, int]]]] = {}
    statistics: dict[int, dict[str, int]] = {}
    for output_uid, source_path, source_track_number, offset_ns, default_duration_ns in statistics_sources:
        statistics_routes.setdefault(source_path, {}).setdefault(
            source_track_number, [],
        ).append((output_uid, offset_ns, default_duration_ns))
        statistics[output_uid] = {
            "frame_count": 0,
            "payload_bytes": 0,
            "duration_ns": 0,
            "last_timestamp_ns": -1,
            "last_delta_ns": 0,
        }
    for source_path, routes in statistics_routes.items():
        for block in readers[source_path].blocks():
            targets = routes.get(block.track_number)
            if not targets:
                continue
            timestamp_ns = (
                block.timestamp_ns
                if block.timestamp_ns is not None
                else block.timestamp_ms * 1_000_000
            )
            explicit_duration_ns = (
                block.duration_ns
                if block.duration_ns is not None
                else ((block.duration_ms or 0) * 1_000_000 if block.duration_ms is not None else None)
            )
            for output_uid, offset_ns, default_duration_ns in targets:
                shifted_timestamp_ns = timestamp_ns + offset_ns
                if shifted_timestamp_ns < 0:
                    continue
                item = statistics[output_uid]
                item["frame_count"] += 1
                item["payload_bytes"] += len(block.payload)
                previous_timestamp_ns = item["last_timestamp_ns"]
                if shifted_timestamp_ns > previous_timestamp_ns >= 0:
                    item["last_delta_ns"] = shifted_timestamp_ns - previous_timestamp_ns
                item["last_timestamp_ns"] = shifted_timestamp_ns
                duration_ns = explicit_duration_ns
                if duration_ns is None and default_duration_ns:
                    duration_ns = default_duration_ns * max(1, block.lace_count)
                if duration_ns is None:
                    duration_ns = item["last_delta_ns"]
                item["duration_ns"] = max(
                    item["duration_ns"], shifted_timestamp_ns + duration_ns,
                )
    # La date de génération des statistiques varie à chaque muxage. Elle ne
    # doit pas rendre le SegmentUID instable pour un plan sémantiquement
    # identique ; les données de piste et les autres métadonnées sont déjà
    # couvertes par ce digest.
    opaque_digest = hashlib.sha256(b"".join(opaque)).hexdigest()
    statistics_tags = build_track_statistics_tags_element({
        output_uid: (
            values["frame_count"],
            values["payload_bytes"],
            values["duration_ns"],
        )
        for output_uid, values in statistics.items()
    }, writing_app=f"Muxiveo {APP_VERSION_LABEL.removeprefix('v')}")
    if statistics_tags:
        opaque.append(statistics_tags)

    info_source = plan.segment_info_source or plan.ordered_tracks[0].artifact
    info_reader = readers[info_source]
    segment_title = (
        info_reader.segment_title()
        if plan.segment_title is None
        else plan.segment_title.strip()
    )
    # SegmentUID déterministe dérivé du contenu sémantique complet du plan :
    # pistes (UID/numéros déjà dérivés du contenu source), titre, digest des
    # top-level opaques (chapitres/tags/attachments) et TimestampScale. Deux
    # plans distincts ne partagent jamais un UID ; un même plan reste stable.
    segment_uid = deterministic_uid128(
        "segment",
        tuple((track.output_uid, track.output_number) for track in output_tracks),
        segment_title,
        opaque_digest,
        timestamp_scale_ns,
    )
    return MatroskaMuxPlan(
        plan.output, tuple(output_tracks), packet_stream, duration_ms=0,
        duration_ns=0, timestamp_scale_ns=timestamp_scale_ns,
        segment_uid=segment_uid,
        muxing_app=f"Muxiveo {APP_VERSION_LABEL.removeprefix('v')}",
        writing_app=plan.writing_app or WRITING_APPLICATION_TAG,
        title=segment_title,
        opaque_top_level=tuple(opaque),
    )


__all__ = [
    "MatroskaAssemblyAttachment",
    "MatroskaAssemblyPlan",
    "MatroskaAssemblyTrack",
    "MatroskaTrackFlags",
    "assembly_output_contract",
    "canonical_attachment_output_name",
    "compile_assembly_plan",
]
