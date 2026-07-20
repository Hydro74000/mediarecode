"""Sélection du backend de muxage final Matroska pour les workflows encode.

Le backend vidéo (FFmpeg ou NVEncC selon le codec) reste une décision
orthogonale : ce module ne choisit que l'assembleur final du conteneur.

Politique (lot 2) :

- ``ffmpeg`` : tous les assemblages finaux restent FFmpeg (post-patchs actifs) ;
- ``native`` : strict, aucune incompatibilité tolérée — signalée avant
  l'encodage lourd, aucun repli FFmpeg ;
- ``auto`` : le natif n'est choisi que lorsque tous les artefacts requis sont
  déjà matérialisés ou peuvent l'être sans perte de contrat (NVEncC en MKV,
  multi-vidéo, injection HDR — dont l'artefact réécrit du lot 3 porte
  timestamps et signalisation DoVi —, audio matérialisable, pistes copiées
  depuis du Matroska). Le chemin FFmpeg direct monopasse reste sélectionné
  tant que le natif n'imposerait qu'une passe disque supplémentaire.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.workflows.encode.models import EncodeConfig
from core.workflows.remux_models import normalize_mux_backend
from core.workflows.remux_plan import MATROSKA_EXTENSIONS


PIPELINE_METADATA_INJECT = "metadata_inject"
PIPELINE_MULTI_VIDEO = "multi_video"
PIPELINE_NVENCC_DIRECT = "nvencc_direct"
PIPELINE_FFMPEG_DIRECT = "ffmpeg_direct"


@dataclass(frozen=True)
class EncodeMuxDecision:
    """Décision de backend de muxage final, prise avant tout encodage lourd."""

    requested: str
    selected: str
    pipeline: str
    reason: str
    diagnostics: tuple[str, ...] = ()

    @property
    def uses_fallback(self) -> bool:
        return self.requested == "auto" and self.selected == "ffmpeg" and bool(self.diagnostics)


def _copied_container_sources(config: EncodeConfig, pipeline: str) -> set[Path]:
    """Sources conteneur dont des pistes sont copiées telles quelles."""
    sources: set[Path] = set()
    for audio in config.audio_tracks:
        # Seules les pistes copiées telles quelles exigent une source
        # Matroska ; une piste réencodée (ou TrueHD core à extraire) est
        # matérialisée en MKV mono-piste par FFmpeg avant assemblage.
        codec = str(audio.codec or "copy").strip().lower()
        if codec == "copy" and not getattr(audio, "extract_truehd_core", False):
            sources.add(Path(audio.source_path or config.source))
    for subtitle in config.subtitle_tracks or []:
        sources.add(Path(subtitle[0]))
    if config.copy_subtitles and not config.subtitle_tracks:
        # La copie implicite des sous-titres balaie TOUTES les sources du
        # layout (résolution du plan) : chacune doit être Matroska.
        from core.workflows.encode.planning.sources import resolve_source_layout

        sources.update(Path(source) for source in resolve_source_layout(config).sources)
    if config.keep_chapters and config.chapter_overrides is None:
        sources.add(Path(config.source))
    if pipeline == PIPELINE_MULTI_VIDEO:
        for video in config.video_tracks or []:
            if str(video.codec or "").strip().lower() == "copy":
                sources.add(Path(video.source_path or config.source))
    return sources


def encode_native_mux_blockers(config: EncodeConfig, *, pipeline: str) -> tuple[str, ...]:
    """Motifs empêchant l'assemblage final natif pour ce job encode.

    Depuis le lot 3, les chemins d'injection HEVC sont éligibles au natif :
    l'artefact réécrit porte les timestamps de l'encodeur, le CodecPrivate et
    la signalisation Dolby Vision dans son ``TrackEntry``.
    """
    reasons: list[str] = []
    if config.output.suffix.lower() != ".mkv":
        reasons.append("le backend natif écrit uniquement des sorties .mkv")
    if config.track_time_offsets:
        reasons.append("décalages temporels à matérialiser par FFmpeg (synchronisation)")
    for source in sorted(_copied_container_sources(config, pipeline)):
        if source.suffix.lower() not in MATROSKA_EXTENSIONS:
            reasons.append(
                f"{source.name}: source non Matroska — pistes copiées à canonicaliser par FFmpeg"
            )
    if config.attachment_streams:
        reasons.append("attachments par stream à transposer par FFmpeg")
    if config.tag_sources and config.tag_overrides is None:
        reasons.append("copie de tags sources non transposée nativement")
    for patch in config.track_meta_edits:
        if any(
            flag is not None
            for flag in (
                patch.flag_default, patch.flag_forced, patch.flag_hearing_impaired,
                patch.flag_visual_impaired, patch.flag_original, patch.flag_commentary,
            )
        ):
            reasons.append("édition de flags de piste à appliquer via FFmpeg")
            break
    if pipeline == PIPELINE_NVENCC_DIRECT:
        # NVEncC écrit lui-même son MKV : la signalisation DoVi/HDR10+ de cet
        # intermédiaire n'est pas encore vérifiée pour l'assemblage natif.
        for video in config.video_tracks or ([config.video] if config.video else []):
            if video is None:
                continue
            if video.copy_dv or video.copy_hdr10plus:
                reasons.append(
                    "HDR dynamique NVEncC : signalisation de l'intermédiaire non vérifiée — chemin FFmpeg"
                )
                break
    return tuple(dict.fromkeys(reasons))


def select_encode_mux_backend(config: EncodeConfig, *, pipeline: str) -> EncodeMuxDecision:
    """Décide du backend d'assemblage final, avant tout démarrage effectif.

    Aucun repli automatique ne survient après le démarrage d'un backend : en
    mode ``auto`` le repli est décidé ici, au préflight, et journalisé avant
    toute écriture.
    """
    requested = normalize_mux_backend(getattr(config, "mux_backend", "ffmpeg"))
    if requested == "ffmpeg":
        return EncodeMuxDecision(
            requested=requested, selected="ffmpeg", pipeline=pipeline,
            reason="backend FFmpeg demandé",
        )
    blockers = encode_native_mux_blockers(config, pipeline=pipeline)
    if requested == "native":
        return EncodeMuxDecision(
            requested=requested, selected="native", pipeline=pipeline,
            reason="backend natif demandé (strict, sans repli)",
            diagnostics=blockers,
        )
    if blockers:
        return EncodeMuxDecision(
            requested="auto", selected="ffmpeg", pipeline=pipeline,
            reason="; ".join(blockers), diagnostics=blockers,
        )
    if pipeline == PIPELINE_FFMPEG_DIRECT:
        return EncodeMuxDecision(
            requested="auto", selected="ffmpeg", pipeline=pipeline,
            reason=(
                "chemin FFmpeg direct monopasse conservé : le natif n'imposerait "
                "qu'une passe disque supplémentaire sans bénéfice fonctionnel"
            ),
        )
    return EncodeMuxDecision(
        requested="auto", selected="native", pipeline=pipeline,
        reason="artefacts Matroska matérialisés — assemblage natif sans post-patch",
    )


__all__ = [
    "EncodeMuxDecision",
    "PIPELINE_FFMPEG_DIRECT",
    "PIPELINE_METADATA_INJECT",
    "PIPELINE_MULTI_VIDEO",
    "PIPELINE_NVENCC_DIRECT",
    "encode_native_mux_blockers",
    "select_encode_mux_backend",
]
