"""Squelette de timing Matroska d'un MKV vidéo mono-piste encodé.

Le pipeline d'injection HDR (lot 3) n'a besoin du MKV encodé complet que
pour ses métadonnées temporelles : TrackEntry (CodecPrivate compris),
échelle de temps, ordre de décodage, PTS, durées, keyframes et références
des blocs. Ce module écrit ces métadonnées dans un MKV « squelette » dont
les payloads de blocs sont vides (quelques Mo), ce qui permet de supprimer
le MKV encodé complet dès l'extraction annexB — le pic disque du pipeline
retombe à 2× la vidéo au lieu de 3×.

Le squelette est un artefact strictement interne : il n'est jamais livré.
Le fichier final est produit par :class:`MatroskaHevcPayloadRewriter`, qui
remplace chaque payload vide par l'access unit injecté correspondant.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack
from ..reader import MatroskaReader
from ..writer import MatroskaWriteProgress, MatroskaWriter


_TRACK_TYPE_VIDEO = 1


class TimingSkeletonError(RuntimeError):
    """Le MKV encodé ne peut pas être réduit en squelette de timing."""


@dataclass(frozen=True)
class TimingSkeletonResult:
    """Résultat de l'écriture du squelette de timing."""

    output: Path
    blocks_written: int


def write_timing_skeleton(
    encoded_mkv: Path,
    output: Path,
    *,
    cancel_cb: Callable[[], bool] | None = None,
    progress_cb: Callable[[MatroskaWriteProgress], None] | None = None,
) -> TimingSkeletonResult:
    """Écrit ``output`` : structure et timing du MKV encodé, payloads vides.

    Le TrackEntry est rejoué tel quel (numéro et UID de piste inclus), les
    blocs conservent PTS, durées, keyframes, références, discard padding et
    BlockAdditions à l'identique — seul le payload est vidé. Les blocs lacés
    sont refusés strictement : le réécrivain lockstep suppose 1 bloc = 1
    access unit.
    """
    reader = MatroskaReader(encoded_mkv)
    reader.segment()
    tracks = reader.tracks()
    video_tracks = [track for track in tracks if track.track_type == _TRACK_TYPE_VIDEO]
    if len(video_tracks) != 1 or len(tracks) != 1:
        raise TimingSkeletonError(
            "MKV encodé mono-piste vidéo attendu : "
            f"{len(video_tracks)} piste(s) vidéo / {len(tracks)} piste(s) au total."
        )
    video = video_tracks[0]
    mux_track = MatroskaMuxTrack(
        source=encoded_mkv,
        source_track=video,
        output_number=video.number,
        output_uid=video.uid or 1,
        patch_language=False,
        patch_name=False,
        patch_flags=False,
    )

    blocks_written = {"count": 0}

    def _packets() -> Iterator[MatroskaMuxPacket]:
        """Rejoue les blocs vidéo en ordre de décodage, payloads vidés."""
        for sequence, block in enumerate(reader.blocks()):
            if block.track_number != video.number:
                continue
            if block.lace_count > 1 or block.lacing_mode:
                raise TimingSkeletonError(
                    "Blocs lacés non supportés par le squelette de timing "
                    f"(bloc #{sequence + 1})."
                )
            blocks_written["count"] += 1
            yield MatroskaMuxPacket(
                video.number,
                dataclasses.replace(block, payload=b"", encoded_frames_payload=b""),
                sequence,
            )

    plan = MatroskaMuxPlan(
        output=output,
        tracks=(mux_track,),
        packets=_packets(),
        timestamp_scale_ns=reader.timestamp_scale_ns(),
        muxing_app="Muxiveo timing skeleton",
        writing_app="Muxiveo",
    )
    MatroskaWriter().write(plan, cancel_cb=cancel_cb, progress_cb=progress_cb)
    return TimingSkeletonResult(output=output, blocks_written=blocks_written["count"])


__all__ = [
    "TimingSkeletonError",
    "TimingSkeletonResult",
    "write_timing_skeleton",
]
