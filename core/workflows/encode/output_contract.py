"""Construction du contrat Matroska commun pour les sorties d'encode.

Le contrat est compilé avant le muxage final.  Il reste volontairement
indépendant de FFmpeg : les mêmes attentes sont utilisables par l'assembleur
natif et par la transaction FFmpeg.
"""

from __future__ import annotations

from pathlib import Path

from core.workflows.common.attachments import (
    canonical_attachment_output_name,
    mime_for_path,
)
from core.workflows.encode.models import EncodeConfig
from core.workflows.encode.planning.plan_models import EncodePlan
from core.workflows.encode.planning.track_metadata import (
    looks_like_matroska as _looks_like_matroska,
    source_matroska_track as _source_track,
)
from core.matroska.contract import (
    ExpectedMatroskaTrack,
    ExpectedMatroskaAttachment,
    ExpectedTrackFlags,
    MatroskaOutputContract,
)
from core.matroska.ids import CHAPTERS_ID, TAGS_ID
from core.matroska.reader import MatroskaReader


def build_encode_output_contract(
    config: EncodeConfig,
    plan: EncodePlan,
    *,
    require_dovi_video_indexes: set[int] | None = None,
    attachment_stream_expectations: tuple[ExpectedMatroskaAttachment, ...] = (),
    source_has_chapters: bool | None = None,
) -> MatroskaOutputContract:
    """Compile les pistes, métadonnées et attachments attendus avant écriture.

    ``attachment_stream_expectations`` : attentes des attachments embarqués
    copiés via ``attachment_streams`` — calculées par l'appelant avec le
    même descripteur que la commande de muxage (noms identiques garantis).
    ``source_has_chapters`` : présence de chapitres dans une source
    non-Matroska (sondée par l'appelant) — la sonde native ne couvre que
    les sources Matroska et laisserait une perte de chapitres MP4/MOV
    passer la validation.
    """
    expected: list[ExpectedMatroskaTrack] = []
    for metadata in plan.track_metadata:
        flags = metadata.flags
        expected.append(ExpectedMatroskaTrack(
            track_type=metadata.track_type,
            name=metadata.name,
            language=metadata.language,
            flags=(
                ExpectedTrackFlags(
                    enabled=flags.enabled,
                    default=flags.default,
                    forced=flags.forced,
                    hearing_impaired=flags.hearing_impaired,
                    visual_impaired=flags.visual_impaired,
                    original=flags.original,
                    commentary=flags.commentary,
                )
                if flags is not None
                else None
            ),
            require_packets=metadata.track_type in {"video", "audio"},
        ))

    dovi_indexes = require_dovi_video_indexes or set()
    video_position = 0
    for index, current in enumerate(expected):
        if current.track_type != "video":
            continue
        if video_position in dovi_indexes:
            expected[index] = ExpectedMatroskaTrack(
                track_type=current.track_type,
                name=current.name,
                language=current.language,
                flags=current.flags,
                require_packets=current.require_packets,
                require_block_addition_mapping=True,
            )
        video_position += 1

    # Attachments embarqués copiés (mêmes noms que la commande) PUIS fichiers
    # attachés — ordre identique à celui de la commande de muxage.
    expected_attachments = attachment_stream_expectations + tuple(
        ExpectedMatroskaAttachment(
            name=canonical_attachment_output_name(Path(path)),
            # Même source de vérité que ``EncodeStreamMappingService`` : le
            # contrat doit attendre exactement le MIME écrit par FFmpeg.
            media_type=mime_for_path(Path(path)),
            size=Path(path).stat().st_size if Path(path).is_file() else None,
        )
        for path in config.extra_attachments
    )
    attachment_names = tuple(item.name for item in expected_attachments)
    expects_tags = bool(config.tag_overrides)
    if not expects_tags and config.tag_overrides is None:
        for source in config.tag_sources:
            try:
                source_path = Path(source)
                if _looks_like_matroska(source_path) and MatroskaReader(source_path).raw_top_level(TAGS_ID):
                    expects_tags = True
                    break
            except (OSError, ValueError):
                continue
    expects_chapters = bool(config.chapter_overrides)
    if not expects_chapters and config.chapter_overrides is None and config.keep_chapters:
        if source_has_chapters is not None:
            expects_chapters = bool(source_has_chapters)
        else:
            try:
                source_path = Path(config.source)
                expects_chapters = bool(
                    _looks_like_matroska(source_path)
                    and MatroskaReader(source_path).raw_top_level(CHAPTERS_ID)
                )
            except (OSError, ValueError):
                expects_chapters = False
    return MatroskaOutputContract(
        track_types=tuple(track.track_type for track in expected),
        expected_tracks=tuple(expected),
        expects_chapters=expects_chapters,
        expects_tags=expects_tags,
        attachment_names=attachment_names,
        expected_attachments=expected_attachments,
        strict_attachment_names=True,
        # Copie de sous-titres par mapping optionnel (pré-scan incomplet) :
        # les pistes sous-titres de la sortie ne sont pas prévisibles.
        allow_unexpected_subtitles=bool(
            config.copy_subtitles
            and not config.subtitle_tracks
            and not plan.subtitles_resolved
        ),
    )


__all__ = ["build_encode_output_contract", "_source_track"]
