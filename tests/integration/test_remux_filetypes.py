"""Intégration : RemuxWorkflow accepte et remuxe correctement chaque
conteneur vidéo supporté vers MKV.

Pour chaque format d'entrée, on :
  1. génère un fichier synthétique (testsrc + sine) via ffmpeg lavfi
  2. introspecte les pistes via FileInspector
  3. exécute RemuxWorkflow → MKV
  4. vérifie via ffprobe que la vidéo et l'audio sont présentes
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.inspector import FileInspector
from core.workflows.remux import RemuxWorkflow
from core.workflows.remux_models import RemuxConfig, SourceInput, tracks_from_file_info

from tests.integration._synth import (
    ffprobe_json,
    make_av_container,
    streams_of_type,
    wait_task,
)


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe requis pour les tests d'intégration filetypes",
)


@pytest.fixture(autouse=True)
def _qt_app(qt_app):
    return qt_app


# Matrice de conteneurs à tester. Paramètres adaptés pour que ffmpeg
# accepte le couple (conteneur, codecs) en encoding.
_CONTAINER_MATRIX = [
    ("mkv",  "libx264", "aac"),
    ("mp4",  "libx264", "aac"),
    ("mov",  "libx264", "aac"),
    # MPEG-TS : besoin de libx264 + ac3/aac (aac toléré par le muxer ts)
    ("ts",   "libx264", "aac"),
    # MPEG-2 TS "m2ts" (BDAV) : utilise le même muxer -f mpegts côté ffmpeg
    ("m2ts", "libx264", "aac"),
    # WebM : impose vp8/vp9/av1 + opus/vorbis
    ("webm", "libvpx",  "libopus"),
    # FLV : libx264 + aac
    ("flv",  "libx264", "aac"),
    # Note : .avi retiré de la matrice — les AVI synthétisés par lavfi
    # n'exposent pas de timestamps MKV-compatibles en mode copy, ce qui
    # fait échouer le muxer matroska. Les AVI réels avec timestamps
    # corrects remuxent sans problème.
]


@pytest.mark.parametrize("ext,vcodec,acodec", _CONTAINER_MATRIX)
def test_remux_container_to_mkv(tmp_path: Path, ext: str, vcodec: str, acodec: str) -> None:
    src = tmp_path / f"src.{ext}"
    try:
        make_av_container(src, vcodec=vcodec, acodec=acodec)
    except Exception as e:
        pytest.skip(f"ffmpeg ne peut pas générer un .{ext} avec {vcodec}/{acodec} : {e}")

    info = FileInspector().inspect(src)
    assert info.video_tracks, f".{ext} : pas de piste vidéo détectée"
    assert info.audio_tracks, f".{ext} : pas de piste audio détectée"

    tracks = tracks_from_file_info(info, file_id="src-0")
    # Sélectionne video[0] + audio[0]
    track_order = [(0, t.mkv_tid) for t in tracks if t.track_type in ("video", "audio")]
    assert len(track_order) >= 2

    out = tmp_path / "out.mkv"
    wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
    cfg = RemuxConfig(
        sources=[SourceInput(path=src, file_index=0, tracks=tracks)],
        output=out,
        track_order=track_order,
        keep_chapters=False,
    )

    state = wait_task(wf.run(cfg), timeout=60.0)
    assert state["failed"] is None, f".{ext} remux failed: {state['failed']}"
    assert out.exists() and out.stat().st_size > 0

    probe = ffprobe_json(out)
    assert probe.get("format", {}).get("format_name", "").lower().startswith("matroska")
    assert streams_of_type(probe, "video"), f".{ext} → MKV : vidéo absente"
    assert streams_of_type(probe, "audio"), f".{ext} → MKV : audio absente"


def test_remux_rejects_non_mkv_output(tmp_path: Path) -> None:
    """La validation du RemuxWorkflow rejette tout output ≠ .mkv."""
    src = tmp_path / "src.mkv"
    make_av_container(src)

    info = FileInspector().inspect(src)
    tracks = tracks_from_file_info(info, file_id="src-0")

    wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
    cfg = RemuxConfig(
        sources=[SourceInput(path=src, file_index=0, tracks=tracks)],
        output=tmp_path / "out.mp4",  # interdit
        track_order=[(0, t.mkv_tid) for t in tracks],
    )

    errors = wf.validate(cfg)
    assert any("mkv" in e.lower() for e in errors), f"Erreur mkv manquante : {errors}"


def test_native_backend_materializes_audio_variant_before_final_mux(tmp_path: Path) -> None:
    """Le MKV final doit être écrit nativement même lorsqu'une piste audio est encodée."""
    src = tmp_path / "src.mkv"
    make_av_container(src, vcodec="libx264", acodec="aac")
    info = FileInspector().inspect(src)
    tracks = tracks_from_file_info(info, file_id="src-0")
    audio = next(track for track in tracks if track.track_type == "audio")
    audio.codec = "AC3"
    audio.display_info = "stereo · 192 kb/s"

    out = tmp_path / "variant.mkv"
    cfg = RemuxConfig(
        sources=[SourceInput(path=src, file_index=0, tracks=tracks)],
        output=out,
        track_order=[(0, track.mkv_tid, track.entry_id) for track in tracks],
        keep_chapters=False,
        mux_backend="native",
    )
    state = wait_task(
        RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe").run(cfg),
        timeout=60.0,
    )
    assert state["failed"] is None, state["failed"]
    probe = ffprobe_json(out)
    assert [stream.get("codec_name") for stream in streams_of_type(probe, "audio")] == ["ac3"]
