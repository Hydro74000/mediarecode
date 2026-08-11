"""Plan d'exécution unique du remux : ``MuxExecutionPlan``.

Le plan est immuable et compilé une seule fois par :func:`plan_remux`.
La validation, la preview texte/JSON et l'exécution consomment ce même
objet — la preview ne reconstruit plus une approximation indépendante de
l'exécution.

Le contrôle de capacité du backend natif est scopé aux pistes réellement
sélectionnées (``track_order``) et aux métadonnées effectivement demandées :
une piste incompatible ou chiffrée non sélectionnée ne provoque plus de
repli.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from core.bluray import append_ffmpeg_input_args
from core.subtitle_codec import plan_subtitle_codec
from core.workflows.common.attachments import (
    ATTACHMENT_EXT_BY_MIME,
    canonical_attachment_output_name,
    sanitize_filename,
)
from core.workflows.common.sync_rewrite import (
    audio_bitrate_kbps_from_display_info,
    normalized_rewrite_codec,
)
from core.matroska.contract import (
    ExpectedMatroskaAttachment,
    ExpectedMatroskaTrack,
    ExpectedTrackFlags,
    MatroskaOutputContract,
)
from core.matroska.reader import MatroskaReader
from core.workdir import normalized_tmdb_cover_filename
from core.workflows.remux_mapping import (
    MappedTrack,
    normalized_language_value,
    resolve_mapped_tracks,
    track_order_parts,
)
from core.workflows.remux_models import (
    RemuxConfig,
    RemuxError,
    SourceInput,
    TrackEntry,
    normalize_mux_backend,
)


MATROSKA_EXTENSIONS = frozenset({".mkv", ".webm", ".mka", ".mks", ".mk3d"})

#: Extensions vidéo brutes nécessitant un ``-r`` explicite à la canonicalisation.
RAW_VIDEO_SUFFIXES = frozenset({
    ".264", ".avc", ".h264", ".x264", ".265", ".hevc", ".h265", ".x265",
    ".av1", ".obu", ".ivf", ".vc1", ".m1v", ".m2v", ".mpv",
})

#: Codecs audio dont les variantes sont matérialisées par le backend natif.
NATIVE_AUDIO_VARIANT_ENCODERS: dict[str, str] = {
    "aac": "aac", "ac3": "ac3", "eac3": "eac3", "flac": "flac",
}

#: Marqueur de dossier temporaire dans les commandes de preview.
PREVIEW_TEMPORARY_DIR = "<temporary>"


# =============================================================================
# Décision de backend
# =============================================================================

@dataclass(frozen=True)
class MuxBackendDecision:
    requested: str
    selected: str
    native_reasons: tuple[str, ...] = ()

    @property
    def uses_fallback(self) -> bool:
        return self.requested == "auto" and self.selected == "ffmpeg"


@dataclass(frozen=True)
class SourceParticipation:
    """Rôles d'une source dans le plan natif final."""

    source_file_index: int
    selected_streams: tuple[int, ...] = ()
    attachment_streams: tuple[int, ...] = ()
    copy_tags: bool = False
    copy_chapters: bool = False
    segment_info: bool = False

    @property
    def participates(self) -> bool:
        return bool(
            self.selected_streams
            or self.attachment_streams
            or self.copy_tags
            or self.copy_chapters
            or self.segment_info
        )


# =============================================================================
# Sélection de pistes et participation des sources
# =============================================================================

def selected_tracks_by_source(config: RemuxConfig) -> dict[int, list[TrackEntry]]:
    """Pistes réellement sélectionnées par ``track_order``, groupées par source.

    Les références invalides sont ignorées ici : la validation commune les
    signale séparément.
    """
    by_id = {
        (source.file_index, track.entry_id): track
        for source in config.sources
        for track in source.tracks
    }
    by_pair: dict[tuple[int, int], TrackEntry] = {}
    for source in config.sources:
        for track in source.tracks:
            by_pair.setdefault((source.file_index, track.mkv_tid), track)
    selected: dict[int, list[TrackEntry]] = {}
    for order_item in config.track_order:
        file_index, mkv_tid, entry_id = track_order_parts(order_item)
        selected_track = (
            by_id.get((file_index, entry_id))
            if entry_id
            else by_pair.get((file_index, mkv_tid))
        )
        if selected_track is not None:
            selected.setdefault(file_index, []).append(selected_track)
    return selected


def source_participations(config: RemuxConfig) -> tuple[SourceParticipation, ...]:
    """Décrit précisément pourquoi chaque source participe au muxage."""
    if not config.sources:
        return ()
    selected = selected_tracks_by_source(config)
    chapter_source: int | None = None
    if config.chapter_overrides is None and config.keep_chapters:
        chapter_source = config.chapter_source_index
        if chapter_source is None:
            chapter_source = next(
                (source.file_index for source in config.sources if source.has_chapters),
                config.sources[0].file_index,
            )
    first_index = config.sources[0].file_index
    return tuple(
        SourceParticipation(
            source_file_index=source.file_index,
            selected_streams=tuple(track.mkv_tid for track in selected.get(source.file_index, ())),
            attachment_streams=tuple(
                int(attachment.index)
                for attachment in source.selected_attachments
                if int(getattr(attachment, "index", -1)) >= 0
            ),
            copy_tags=bool(source.copy_tags),
            copy_chapters=source.file_index == chapter_source,
            segment_info=source.file_index == first_index,
        )
        for source in config.sources
    )


def participating_source_indexes(config: RemuxConfig) -> set[int]:
    """Sources effectivement lues par le backend natif.

    Une source sans piste sélectionnée, sans attachment, sans tags copiés et
    hors stratégie de chapitres n'est jamais ouverte : elle ne peut donc pas
    bloquer le préflight.
    """
    return {
        item.source_file_index
        for item in source_participations(config)
        if item.participates
    }


def _metadata_carrier_stream(source: SourceInput) -> int | None:
    """Premier stream sûr servant uniquement de porteur metadata."""
    for track_type in ("video", "audio", "subtitle"):
        for track in source.tracks:
            if track.track_type != track_type:
                continue
            if track_type == "subtitle":
                try:
                    plan_subtitle_codec(track.codec)
                except ValueError:
                    continue
            return int(track.mkv_tid)
    return None


# =============================================================================
# Préflight natif (scopé aux pistes sélectionnées)
# =============================================================================

def _shared_reader(
    readers: dict[Path, MatroskaReader] | None, path: Path
) -> MatroskaReader:
    """Réutilise un ``MatroskaReader`` mémoïsé pour ``path`` sur une compilation.

    Le cache interne du reader (segment/tracks) n'aide qu'entre appels d'une même
    instance ; partager l'instance évite de reparcourir une source lue à la fois
    par le préflight natif et le contrat de sortie.
    """
    if readers is None:
        return MatroskaReader(path)
    reader = readers.get(path)
    if reader is None:
        reader = MatroskaReader(path)
        readers[path] = reader
    return reader


def native_capability_reasons(
    config: RemuxConfig,
    *,
    readers: dict[Path, MatroskaReader] | None = None,
) -> tuple[str, ...]:
    """Blocages du backend natif, limités au périmètre réellement demandé.

    Centraliser ce contrôle garantit que les incréments du writer ne font que
    retirer des blocages, sans changer silencieusement la sémantique v1.
    """
    reasons: list[str] = []
    if config.output.suffix.lower() != ".mkv":
        reasons.append("le backend natif écrit uniquement des sorties .mkv")
    selected = selected_tracks_by_source(config)
    participations = {
        item.source_file_index: item for item in source_participations(config)
    }
    participating = {
        index for index, item in participations.items() if item.participates
    }
    for source in config.sources:
        selected_tracks = selected.get(source.file_index, [])
        for track in selected_tracks:
            if track.track_type == "subtitle":
                try:
                    plan_subtitle_codec(track.codec)
                except ValueError as exc:
                    reasons.append(f"{source.path.name}: {exc}")
            if track.sync_rewrite_label and track.time_shift_ms:
                reasons.append(
                    f"{source.path.name}: réécriture de synchronisation avancée à matérialiser par FFmpeg"
                )
        if source.file_index not in participating:
            continue
        participation = participations[source.file_index]
        if source.path.suffix.lower() not in MATROSKA_EXTENSIONS:
            unsupported_attachments = [
                attachment for attachment in source.selected_attachments
                if not bool(getattr(attachment, "is_attached_pic", False))
            ]
            if unsupported_attachments:
                reasons.append(
                    f"{source.path.name}: attachments non-Matroska non matérialisables nativement"
                )
            needs_container = bool(
                participation.selected_streams
                or participation.copy_tags
                or participation.copy_chapters
                or participation.segment_info
            )
            if needs_container and not participation.selected_streams and _metadata_carrier_stream(source) is None:
                reasons.append(
                    f"{source.path.name}: aucun stream compatible pour transporter les métadonnées"
                )
        if source.path.suffix.lower() not in MATROSKA_EXTENSIONS:
            continue
        if not source.path.is_file():
            continue
        try:
            reader = _shared_reader(readers, source.path)
            reader.segment()
            native_tracks = reader.tracks()
            if selected_tracks and not native_tracks:
                reasons.append(f"{source.path.name}: aucune piste Matroska lisible")
                continue
            encodings = reader.content_encodings_by_track()
            for track in selected_tracks:
                if 0 <= track.mkv_tid < len(encodings) and encodings[track.mkv_tid][1]:
                    reasons.append(
                        f"{source.path.name}: piste Matroska chiffrée non transposable "
                        f"(piste #{track.mkv_tid})"
                    )
        except (OSError, ValueError) as exc:
            reasons.append(f"{source.path.name}: structure Matroska illisible ({exc})")
    return tuple(dict.fromkeys(reasons))


def select_mux_backend(
    config: RemuxConfig,
    *,
    readers: dict[Path, MatroskaReader] | None = None,
) -> MuxBackendDecision:
    requested = normalize_mux_backend(config.mux_backend)
    if requested == "ffmpeg":
        # Backend FFmpeg forcé : inutile d'exécuter le préflight de capacité
        # native. Le contrat peut néanmoins lire les pistes vidéo Matroska
        # afin de vérifier la préservation d'éventuels BlockAdditionMapping.
        return MuxBackendDecision(requested=requested, selected="ffmpeg")
    reasons = native_capability_reasons(config, readers=readers)
    if not reasons:
        return MuxBackendDecision(requested=requested, selected="native")
    if requested == "native":
        return MuxBackendDecision(requested=requested, selected="native", native_reasons=reasons)
    return MuxBackendDecision(requested=requested, selected="ffmpeg", native_reasons=reasons)


# =============================================================================
# Canonicalisation sélective
# =============================================================================

def canonicalization_stream_selection(
    source: SourceInput,
    selected_tracks: list[TrackEntry],
) -> tuple[list[int], dict[int, int]]:
    """Streams ffmpeg à transposer + correspondance index source → index canonique.

    Seules les pistes sélectionnées (et les streams d'attachments demandés)
    sont mappées ; la correspondance est conservée explicitement dans le plan.
    """
    wanted = {track.mkv_tid for track in selected_tracks}
    wanted.update(
        attachment.index
        for attachment in source.selected_attachments
        if int(getattr(attachment, "index", -1)) >= 0
    )
    ordered = sorted(index for index in wanted if index >= 0)
    return ordered, {original: canonical for canonical, original in enumerate(ordered)}


def build_canonicalization_command(
    source: SourceInput,
    target: Path,
    ffmpeg_bin: str,
    *,
    selected_streams: list[int] | None = None,
) -> list[str]:
    """Commande de canonicalisation Matroska d'une source non-MKV.

    ``selected_streams`` limite le mapping aux pistes nécessaires ; None
    conserve le comportement historique ``-map 0``.
    """
    command = [ffmpeg_bin, "-y", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if source.path.suffix.lower() in RAW_VIDEO_SUFFIXES:
        display = next((track.display_info for track in source.tracks if track.track_type == "video"), "")
        fps_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:fps|FPS)", display)
        if fps_match:
            command.extend(["-r", fps_match.group(1).replace(",", ".")])
    append_ffmpeg_input_args(command, source.path)
    if selected_streams is None:
        command.extend(["-map", "0"])
        subtitle_tids = [track.mkv_tid for track in source.tracks if track.track_type == "subtitle"]
    else:
        for stream_index in selected_streams:
            command.extend(["-map", f"0:{stream_index}"])
        subtitle_tids = [
            stream_index for stream_index in selected_streams
            if any(
                track.mkv_tid == stream_index and track.track_type == "subtitle"
                for track in source.tracks
            )
        ]
    command.extend(["-c", "copy"])
    codec_by_tid = {track.mkv_tid: track.codec for track in source.tracks}
    seen_tids: set[int] = set()
    subtitle_index = 0
    for tid in subtitle_tids:
        if tid in seen_tids:
            continue
        seen_tids.add(tid)
        codec_arg, _warning = plan_subtitle_codec(codec_by_tid.get(tid, ""))
        if codec_arg != "copy":
            command.extend([f"-c:s:{subtitle_index}", codec_arg])
        subtitle_index += 1
    command.append(str(target))
    return command


# =============================================================================
# Variantes audio natives
# =============================================================================

def passthrough_source_refs(
    mapped_tracks: list[MappedTrack],
) -> list[tuple[Path, int]] | None:
    """Sources des pistes de sortie quand rien ne transforme leurs frames.

    Retourne ``None`` dès qu'une piste est réencodée, décalée, resynchronisée,
    convertie ou nouvellement créée : les compteurs de la source ne
    décriraient alors plus la piste écrite.
    """
    refs: list[tuple[Path, int]] = []
    for item in mapped_tracks:
        track = item.track
        if getattr(track, "is_new", False):
            return None
        if int(getattr(track, "time_shift_ms", 0) or 0):
            return None
        if str(getattr(track, "sync_rewrite_mode", "") or "").strip():
            return None
        target = normalized_rewrite_codec(track.codec)
        source_codec = normalized_rewrite_codec(track.orig_codec or track.codec)
        if target != source_codec:
            return None
        if track.track_type == "subtitle":
            try:
                codec_arg, _warning = plan_subtitle_codec(track.codec)
            except ValueError:
                return None
            if codec_arg != "copy":
                return None
        refs.append((Path(item.source_path), int(item.stream_index)))
    return refs or None


@dataclass(frozen=True)
class NativeAudioVariantPlan:
    """Variante audio à matérialiser avant l'assemblage natif."""

    order_index: int
    source_file_index: int
    stream_index: int
    entry_id: str
    target_codec: str
    encoder: str
    bitrate_kbps: int | None
    downmix_51: bool
    target_name: str


def plan_native_audio_variants(config: RemuxConfig) -> tuple[NativeAudioVariantPlan, ...]:
    """Variantes audio (AAC/AC3/EAC3/FLAC) exigées par ``track_order``.

    Les références invalides sont ignorées ici (validation commune séparée).
    """
    variants: list[NativeAudioVariantPlan] = []
    source_by_index = {source.file_index: source for source in config.sources}
    for order_index, order_item in enumerate(config.track_order):
        file_index, mkv_tid, entry_id = track_order_parts(order_item)
        source = source_by_index.get(file_index)
        if source is None:
            continue
        candidates = [track for track in source.tracks if track.mkv_tid == mkv_tid]
        track = next(
            (item for item in candidates if not entry_id or item.entry_id == entry_id),
            None,
        )
        if track is None:
            continue
        target = normalized_rewrite_codec(track.codec)
        original = normalized_rewrite_codec(track.orig_codec or track.codec)
        needs_audio_encode = (
            track.track_type == "audio"
            and target in NATIVE_AUDIO_VARIANT_ENCODERS
            and (track.is_new or target != original)
        )
        if not needs_audio_encode:
            continue
        bitrate = audio_bitrate_kbps_from_display_info(track.display_info)
        channel_match = re.search(r"\b(\d)\.(\d)\b", str(track.display_info or ""))
        channel_count = sum(map(int, channel_match.groups())) if channel_match else 0
        variants.append(NativeAudioVariantPlan(
            order_index=order_index,
            source_file_index=file_index,
            stream_index=mkv_tid,
            entry_id=track.entry_id,
            target_codec=target,
            encoder=NATIVE_AUDIO_VARIANT_ENCODERS[target],
            bitrate_kbps=int(bitrate) if (target != "flac" and bitrate) else None,
            downmix_51=(target == "ac3" and channel_count > 6),
            target_name=f"audio_variant_{order_index}.mkv",
        ))
    return tuple(variants)


def build_audio_variant_command(
    variant: NativeAudioVariantPlan,
    source_path: Path,
    stream_index: int,
    target: Path,
    ffmpeg_bin: str,
) -> list[str]:
    """Commande ffmpeg de matérialisation d'une variante audio isolée."""
    command = [
        ffmpeg_bin, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(source_path), "-map", f"0:{stream_index}",
        "-c:a", variant.encoder,
    ]
    if variant.bitrate_kbps:
        command.extend(["-b:a", f"{variant.bitrate_kbps}k"])
    if variant.downmix_51:
        command.extend(["-ac:a", "6", "-channel_layout:a", "5.1"])
    command.append(str(target))
    return command


# =============================================================================
# Actions de préparation (preview == exécution)
# =============================================================================

@dataclass(frozen=True)
class RemuxPreparationAction:
    """Action de préparation ordonnée, partagée entre preview et exécution."""

    kind: str
    description: str
    source_file_index: int | None = None
    source_local_index: int | None = None
    target_name: str = ""
    command: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "description": self.description,
            "source_file_index": self.source_file_index,
            "source_local_index": self.source_local_index,
            "target_name": self.target_name,
            "command": list(self.command),
        }


def _canonicalization_actions(
    config: RemuxConfig,
    ffmpeg_bin: str,
) -> tuple[list[RemuxPreparationAction], dict[int, dict[int, int]]]:
    actions: list[RemuxPreparationAction] = []
    index_maps: dict[int, dict[int, int]] = {}
    selected = selected_tracks_by_source(config)
    participations = {
        item.source_file_index: item for item in source_participations(config)
    }
    for source in config.sources:
        if source.path.suffix.lower() in MATROSKA_EXTENSIONS:
            continue
        participation = participations.get(source.file_index)
        if participation is None or not participation.participates:
            continue
        has_container_role = bool(
            participation.selected_streams
            or participation.copy_tags
            or participation.copy_chapters
            or participation.segment_info
            or any(
                not bool(getattr(attachment, "is_attached_pic", False))
                for attachment in source.selected_attachments
            )
        )
        if not has_container_role:
            continue
        streams, index_map = canonicalization_stream_selection(
            source, selected.get(source.file_index, []),
        )
        # Les attached-pictures sont matérialisées séparément ; elles ne sont
        # jamais interprétées comme attachments positionnels du MKV canonique.
        attached_pic_streams = {
            int(attachment.index)
            for attachment in source.selected_attachments
            if bool(getattr(attachment, "is_attached_pic", False))
        }
        streams = [stream for stream in streams if stream not in attached_pic_streams]
        index_map = {original: mapped for mapped, original in enumerate(streams)}
        if not streams:
            carrier = _metadata_carrier_stream(source)
            if carrier is None:
                continue
            streams = [carrier]
            index_map = {carrier: 0}
        index_maps[source.file_index] = index_map
        target_name = f"source_{source.file_index}.mkv"
        command = build_canonicalization_command(
            source,
            Path(f"{PREVIEW_TEMPORARY_DIR}/{target_name}"),
            ffmpeg_bin,
            selected_streams=streams,
        )
        actions.append(RemuxPreparationAction(
            kind="canonicalize_source",
            description=f"Canonicalisation Matroska de {source.path.name}",
            source_file_index=source.file_index,
            target_name=target_name,
            command=tuple(command),
        ))
    return actions, index_maps


def _attached_picture_actions(
    config: RemuxConfig,
    ffmpeg_bin: str,
) -> list[RemuxPreparationAction]:
    """Matérialise les attached-pictures non-MKV en fichiers indépendants."""
    actions: list[RemuxPreparationAction] = []
    participating = participating_source_indexes(config)
    for source in config.sources:
        if source.file_index not in participating:
            continue
        if source.path.suffix.lower() in MATROSKA_EXTENSIONS:
            continue
        for attachment in source.selected_attachments:
            if not bool(getattr(attachment, "is_attached_pic", False)):
                continue
            index = int(getattr(attachment, "index", -1))
            raw_name = sanitize_filename(
                str(getattr(attachment, "filename", "") or ""),
                f"attachment_{index}",
            )
            suffix = Path(raw_name).suffix
            if not suffix:
                suffix = ATTACHMENT_EXT_BY_MIME.get(
                    str(getattr(attachment, "mimetype", "") or "").strip().lower(),
                    ".jpg",
                )
                raw_name = f"{raw_name}{suffix}"
            target_name = f"attachment_{source.file_index}_{int(getattr(attachment, 'local_index', 0))}_{raw_name}"
            target = Path(f"{PREVIEW_TEMPORARY_DIR}/{target_name}")
            command = (
                ffmpeg_bin, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-i", str(source.path), "-map", f"0:{index}", "-threads", "1",
                "-frames:v", "1", str(target),
            )
            actions.append(RemuxPreparationAction(
                kind="extract_attachment",
                description=f"Extraction attachment {raw_name} depuis {source.path.name}",
                source_file_index=source.file_index,
                source_local_index=int(getattr(attachment, "local_index", 0)),
                target_name=target_name,
                command=command,
            ))
    return actions


def _audio_variant_actions(
    config: RemuxConfig,
    ffmpeg_bin: str,
    variants: tuple[NativeAudioVariantPlan, ...],
) -> list[RemuxPreparationAction]:
    actions: list[RemuxPreparationAction] = []
    source_by_index = {source.file_index: source for source in config.sources}
    for variant in variants:
        source = source_by_index[variant.source_file_index]
        command = build_audio_variant_command(
            variant,
            source.path,
            variant.stream_index,
            Path(f"{PREVIEW_TEMPORARY_DIR}/{variant.target_name}"),
            ffmpeg_bin,
        )
        actions.append(RemuxPreparationAction(
            kind="audio_variant",
            description=(
                f"Variante audio {variant.target_codec.upper()} depuis "
                f"{source.path.name} (piste #{variant.stream_index})"
            ),
            source_file_index=variant.source_file_index,
            target_name=variant.target_name,
            command=tuple(command),
        ))
    return actions


def native_preparation_commands(config: RemuxConfig, ffmpeg_bin: str) -> list[list[str]]:
    """Commandes de préparation du backend natif (canonicalisations + variantes).

    Les cibles utilisent le marqueur ``<temporary>`` ; l'exécution emploie les
    mêmes constructeurs avec le dossier temporaire réel.
    """
    canonical_actions, _maps = _canonicalization_actions(config, ffmpeg_bin)
    variant_actions = _audio_variant_actions(
        config, ffmpeg_bin, plan_native_audio_variants(config),
    )
    return [list(action.command) for action in (*canonical_actions, *variant_actions)]


# =============================================================================
# Plan d'exécution
# =============================================================================

@dataclass(frozen=True)
class SelectedTrackRef:
    """Référence stable d'une piste sélectionnée, dans l'ordre de sortie."""

    source_file_index: int
    stream_index: int
    entry_id: str
    track_type: str
    codec: str


@dataclass(frozen=True)
class MuxExecutionPlan:
    """Plan immuable compilé une seule fois par :func:`plan_remux`."""

    config: RemuxConfig
    requested_backend: str
    selected_backend: str
    native_diagnostics: tuple[str, ...]
    selected_tracks: tuple[SelectedTrackRef, ...]
    mapped_tracks: tuple[MappedTrack, ...]
    source_participation: tuple[SourceParticipation, ...]
    canonical_index_maps: tuple[tuple[int, tuple[tuple[int, int], ...]], ...]
    audio_variants: tuple[NativeAudioVariantPlan, ...]
    preparation_actions: tuple[RemuxPreparationAction, ...]
    required_tools: tuple[str, ...]
    candidate_output: Path
    output_contract: MatroskaOutputContract
    mapping_errors: tuple[str, ...] = ()
    output_commit_mode: str = "atomic"

    @property
    def fallback(self) -> bool:
        return self.requested_backend == "auto" and self.selected_backend == "ffmpeg" and bool(self.native_diagnostics)

    @property
    def fallback_reason(self) -> str:
        return "; ".join(self.native_diagnostics) if self.fallback else ""

    def canonical_index_map(self, file_index: int) -> dict[int, int]:
        """Correspondance index source original → index du MKV canonicalisé."""
        for mapped_index, pairs in self.canonical_index_maps:
            if mapped_index == file_index:
                return dict(pairs)
        return {}


def _output_contract(
    config: RemuxConfig,
    mapped_refs: tuple[SelectedTrackRef, ...],
    mapped_tracks: list[TrackEntry],
    selected_backend: str,
    preparation_actions: tuple[RemuxPreparationAction, ...],
    *,
    readers: dict[Path, MatroskaReader] | None = None,
) -> MatroskaOutputContract:
    expects_chapters = bool(config.chapter_overrides) or (
        config.chapter_overrides is None
        and config.keep_chapters
        and any(source.has_chapters for source in config.sources)
    )
    expected_attachments: list[ExpectedMatroskaAttachment] = []
    materialized_attachment_names = {
        (action.source_file_index, action.source_local_index): action.target_name
        for action in preparation_actions
        if action.kind == "extract_attachment"
    }
    # Réplique EXACTE du nommage des attached-pictures extraites par le
    # backend FFmpeg : même ordre d'itération (tri par local_index), même
    # extension MIME et mêmes collisions insensibles à la casse.
    ffmpeg_pic_names: dict[tuple[int, int], str] = {}
    if selected_backend == "ffmpeg":
        used_pic_names: set[str] = set()
        for source in config.sources:
            for attachment in sorted(
                source.selected_attachments,
                key=lambda item: int(getattr(item, "local_index", 0)),
            ):
                if not bool(getattr(attachment, "is_attached_pic", False)):
                    continue
                stream_index = int(getattr(attachment, "index", 0))
                raw_name = sanitize_filename(
                    str(getattr(attachment, "filename", "") or ""), f"attachment_{stream_index}",
                )
                suffix = Path(raw_name).suffix.lower() or ATTACHMENT_EXT_BY_MIME.get(
                    str(getattr(attachment, "mimetype", "") or "").strip().lower(), ".jpg",
                )
                stem = Path(raw_name).stem or f"attachment_{stream_index}"
                name = f"{stem}{suffix}"
                counter = 1
                while name.casefold() in used_pic_names:
                    name = f"{stem}_{counter}{suffix}"
                    counter += 1
                used_pic_names.add(name.casefold())
                ffmpeg_pic_names[(source.file_index, int(getattr(attachment, "local_index", 0)))] = name

    for source in config.sources:
        for attachment in source.selected_attachments:
            raw_filename = str(getattr(attachment, "filename", "") or "")
            is_attached_pic = bool(getattr(attachment, "is_attached_pic", False))
            materialized = materialized_attachment_names.get(
                (source.file_index, int(getattr(attachment, "local_index", 0))),
            )
            if materialized is not None or (is_attached_pic and selected_backend == "ffmpeg"):
                # Image extraite puis ré-attachée : le contenu est réencodé —
                # taille et MIME de la source ne sont plus des attentes valides.
                expected_attachments.append(ExpectedMatroskaAttachment(
                    name=materialized if materialized is not None else ffmpeg_pic_names[
                        (source.file_index, int(getattr(attachment, "local_index", 0)))
                    ],
                ))
                continue
            if selected_backend == "ffmpeg":
                # La commande écrit toujours ``filename=`` avec un nom complété
                # (extension déduite du MIME quand absente) — le contrat doit
                # attendre exactement ce nom.
                stream_index = int(getattr(attachment, "index", 0))
                name = sanitize_filename(raw_filename, f"attachment_{stream_index}")
                if not Path(name).suffix:
                    mime = str(getattr(attachment, "mimetype", "") or "").strip().lower()
                    name = f"{name}{ATTACHMENT_EXT_BY_MIME.get(mime, '.bin')}"
            else:
                # Backend natif : copie brute, noms de la source inchangés.
                name = raw_filename
            if name:
                expected_attachments.append(ExpectedMatroskaAttachment(
                    name=name,
                    media_type=str(getattr(attachment, "mimetype", "") or "") or None,
                    size=getattr(attachment, "size_bytes", None),
                ))
    for extra in config.extra_attachments:
        path = Path(extra)
        expected_attachments.append(ExpectedMatroskaAttachment(
            name=canonical_attachment_output_name(path),
            size=path.stat().st_size if path.is_file() else None,
        ))
    if config.tmdb_cover is not None:
        tmdb_filename = normalized_tmdb_cover_filename(config.tmdb_cover[1])
        expected_attachments.append(ExpectedMatroskaAttachment(
            name=canonical_attachment_output_name(Path(tmdb_filename)),
        ))

    block_mapping_by_output: set[int] = set()
    source_by_index = {source.file_index: source for source in config.sources}
    for output_index, ref in enumerate(mapped_refs):
        if ref.track_type != "video":
            continue
        selected_source = source_by_index.get(ref.source_file_index)
        if (
            selected_source is None
            or selected_source.path.suffix.lower() not in MATROSKA_EXTENSIONS
        ):
            continue
        try:
            native_tracks = _shared_reader(readers, selected_source.path).tracks()
            if 0 <= ref.stream_index < len(native_tracks) and native_tracks[ref.stream_index].block_addition_mappings:
                block_mapping_by_output.add(output_index)
        except (OSError, ValueError):
            continue

    expected_tracks = tuple(
        ExpectedMatroskaTrack(
            track_type=track.track_type,
            name=track.title,
            language=normalized_language_value(track),
            flags=ExpectedTrackFlags(
                enabled=track.flag_enabled,
                default=track.flag_default,
                forced=track.flag_forced,
                hearing_impaired=track.flag_hearing_impaired,
                visual_impaired=track.flag_visual_impaired,
                original=track.flag_original,
                commentary=track.flag_commentary,
            ),
            require_packets=track.track_type in {"video", "audio"},
            require_block_addition_mapping=index in block_mapping_by_output,
        )
        for index, track in enumerate(mapped_tracks)
    )

    return MatroskaOutputContract(
        track_types=tuple(track.track_type for track in mapped_tracks),
        expected_tracks=expected_tracks,
        track_names=tuple(track.title for track in mapped_tracks),
        track_languages=tuple(normalized_language_value(track) for track in mapped_tracks),
        expects_chapters=expects_chapters,
        expects_tags=bool(config.tag_overrides) or (
            config.tag_overrides is None and any(source.copy_tags for source in config.sources)
        ),
        attachment_names=tuple(item.name for item in expected_attachments),
        expected_attachments=tuple(expected_attachments),
        require_block_addition_mapping=bool(block_mapping_by_output),
        strict_attachment_names=True,
    )


def plan_remux(
    config: RemuxConfig,
    *,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
) -> MuxExecutionPlan:
    """Compile le plan d'exécution unique (backend, préparations, contrat)."""
    _ = ffprobe_bin  # symétrie d'API : le second validateur est toujours ffprobe
    # Instances Matroska mémoïsées partagées sur toute la compilation : évite de
    # reparcourir une même source dans le préflight natif puis le contrat.
    readers: dict[Path, MatroskaReader] = {}
    decision = select_mux_backend(config, readers=readers)

    mapping_errors: list[str] = []
    mapped_refs: list[SelectedTrackRef] = []
    mapped_track_entries: list[TrackEntry] = []
    compiled_mapped_tracks: list[MappedTrack] = []
    try:
        for mapped in resolve_mapped_tracks(config):
            compiled_mapped_tracks.append(mapped)
            mapped_refs.append(SelectedTrackRef(
                source_file_index=mapped.source_file_index,
                stream_index=mapped.stream_index,
                entry_id=mapped.track.entry_id,
                track_type=mapped.track.track_type,
                codec=mapped.track.codec,
            ))
            mapped_track_entries.append(mapped.track)
    except RemuxError as exc:
        mapping_errors.append(str(exc))

    preparation_actions: list[RemuxPreparationAction] = []
    if config.tmdb_cover is not None:
        tmdb_filename = normalized_tmdb_cover_filename(config.tmdb_cover[1])
        preparation_actions.append(RemuxPreparationAction(
            kind="download_tmdb_cover",
            description=f"Téléchargement de la cover TMDB {tmdb_filename}",
            target_name=tmdb_filename,
        ))

    canonical_index_maps: dict[int, dict[int, int]] = {}
    audio_variants: tuple[NativeAudioVariantPlan, ...] = ()
    if decision.selected == "native":
        preparation_actions.extend(_attached_picture_actions(config, ffmpeg_bin))
        canonical_actions, canonical_index_maps = _canonicalization_actions(config, ffmpeg_bin)
        preparation_actions.extend(canonical_actions)
        audio_variants = plan_native_audio_variants(config)
        preparation_actions.extend(_audio_variant_actions(config, ffmpeg_bin, audio_variants))
    else:
        if config.chapter_overrides:
            preparation_actions.append(RemuxPreparationAction(
                kind="materialize_chapters",
                description="Matérialisation des chapitres FFMetadata",
                target_name="chapters.ffmetadata",
            ))

    if decision.selected == "native":
        needs_ffmpeg_preparation = any(
            action.command
            for action in preparation_actions
            if action.kind in {"canonicalize_source", "extract_attachment", "audio_variant"}
        )
        required_tools = ("ffprobe", "ffmpeg") if needs_ffmpeg_preparation else ("ffprobe",)
    else:
        required_tools = ("ffmpeg", "ffprobe")

    output_contract = _output_contract(
        config,
        tuple(mapped_refs),
        mapped_track_entries,
        decision.selected,
        tuple(preparation_actions),
        readers=readers,
    )
    seen_attachment_names: set[str] = set()
    duplicate_attachment_names: list[str] = []
    for name in output_contract.attachment_names:
        if name in seen_attachment_names and name not in duplicate_attachment_names:
            duplicate_attachment_names.append(name)
        seen_attachment_names.add(name)
    if duplicate_attachment_names:
        mapping_errors.append(
            "Noms d'attachments de sortie non uniques : "
            + ", ".join(duplicate_attachment_names)
        )

    return MuxExecutionPlan(
        config=config,
        requested_backend=decision.requested,
        selected_backend=decision.selected,
        native_diagnostics=decision.native_reasons,
        selected_tracks=tuple(mapped_refs),
        mapped_tracks=tuple(compiled_mapped_tracks),
        source_participation=source_participations(config),
        canonical_index_maps=tuple(
            (file_index, tuple(sorted(index_map.items())))
            for file_index, index_map in sorted(canonical_index_maps.items())
        ),
        audio_variants=audio_variants,
        preparation_actions=tuple(preparation_actions),
        required_tools=required_tools,
        candidate_output=config.output.with_suffix(config.output.suffix + ".partial"),
        output_contract=output_contract,
        mapping_errors=tuple(mapping_errors),
    )


# =============================================================================
# Rapport structuré (projection du plan)
# =============================================================================

def mux_backend_report_from_plan(plan: MuxExecutionPlan) -> dict[str, object]:
    """Rapport structuré JSON-compatible, projection directe du plan."""
    return {
        "requested_backend": plan.requested_backend,
        "selected_backend": plan.selected_backend,
        "plan_version": 1,
        "fallback": plan.fallback,
        "fallback_reason": plan.fallback_reason,
        "native_diagnostics": list(plan.native_diagnostics),
        "preparation_commands": (
            [list(action.command) for action in plan.preparation_actions if action.command]
            if plan.selected_backend == "native" else []
        ),
        "preparation_actions": [action.to_dict() for action in plan.preparation_actions],
        "required_tools": list(plan.required_tools),
        "output_commit_mode": plan.output_commit_mode,
        "candidate_output": str(plan.candidate_output),
    }


def mux_backend_report(config: RemuxConfig, *, ffmpeg_bin: str = "ffmpeg") -> dict[str, object]:
    """Façade de compatibilité : compile un plan puis le projette."""
    return mux_backend_report_from_plan(plan_remux(config, ffmpeg_bin=ffmpeg_bin))


__all__ = [
    "MATROSKA_EXTENSIONS",
    "MuxBackendDecision",
    "MuxExecutionPlan",
    "NativeAudioVariantPlan",
    "PREVIEW_TEMPORARY_DIR",
    "RemuxPreparationAction",
    "SelectedTrackRef",
    "SourceParticipation",
    "build_audio_variant_command",
    "passthrough_source_refs",
    "build_canonicalization_command",
    "canonicalization_stream_selection",
    "mux_backend_report",
    "mux_backend_report_from_plan",
    "native_capability_reasons",
    "native_preparation_commands",
    "plan_native_audio_variants",
    "plan_remux",
    "select_mux_backend",
    "selected_tracks_by_source",
    "source_participations",
]
