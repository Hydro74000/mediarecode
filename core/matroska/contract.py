"""Semantic contract for a Matroska output."""

from __future__ import annotations

from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class ExpectedTrackFlags:
    enabled: bool | None = None
    default: bool | None = None
    forced: bool | None = None
    hearing_impaired: bool | None = None
    visual_impaired: bool | None = None
    original: bool | None = None
    commentary: bool | None = None


@dataclass(frozen=True)
class ExpectedMatroskaTrack:
    track_type: str
    name: str | None = None
    language: str | None = None
    flags: ExpectedTrackFlags | None = None
    require_packets: bool = False
    require_block_addition_mapping: bool = False


@dataclass(frozen=True)
class ExpectedMatroskaAttachment:
    name: str
    media_type: str | None = None
    uid: int | None = None
    size: int | None = None


@dataclass(frozen=True)
class MatroskaOutputContract:
    """Expected tracks and metadata for a successfully muxed output."""

    track_types: tuple[str, ...]
    expected_tracks: tuple[ExpectedMatroskaTrack, ...] = ()
    track_names: tuple[str, ...] = ()
    track_languages: tuple[str, ...] = ()
    expects_chapters: bool = False
    expects_tags: bool = False
    attachment_names: tuple[str, ...] = ()
    expected_attachments: tuple[ExpectedMatroskaAttachment, ...] = ()
    require_block_addition_mapping: bool = False
    strict_attachment_names: bool = False
    duration_coherent: bool = True
    #: Sous-titres copiés via un mapping optionnel (``-map …:s?``) sans
    #: pré-scan complet : les pistes sous-titres non listées sont acceptées
    #: (les autres types restent comparés strictement).
    allow_unexpected_subtitles: bool = False
    extras: dict[str, object] = field(default_factory=dict)


def without_expected_attachment(
    contract: MatroskaOutputContract, name: str,
) -> MatroskaOutputContract:
    """Contrat sans l'attente de l'attachment ``name``.

    Utilisé quand une préparation optionnelle échoue proprement (ex. cover
    TMDB non téléchargée, annoncée en warning) : la sortie reste valide et
    ne doit plus être rejetée pour cet attachment manquant.
    """
    return replace(
        contract,
        expected_attachments=tuple(
            item for item in contract.expected_attachments if item.name != name
        ),
        attachment_names=tuple(
            item for item in contract.attachment_names if item != name
        ),
    )


__all__ = [
    "ExpectedMatroskaAttachment",
    "ExpectedMatroskaTrack",
    "ExpectedTrackFlags",
    "MatroskaOutputContract",
    "without_expected_attachment",
]
