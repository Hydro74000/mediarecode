"""Intégration : statistiques de pistes des sorties d'encodage.

MediaInfo ne présente le « Count of elements » des sous-titres (et les BPS
par piste) que si les tags ``_STATISTICS_*`` accompagnent
``NUMBER_OF_FRAMES``. Ces tests vérifient la garantie sur les trois backends
de muxage, en copie comme en réencodage réel.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.workflows.encode import (
    EncodeConfig,
    EncodeWorkflow,
    QualityMode,
    VideoEncodeSettings,
)
from core.workflows.encode.models import AudioTrackSettings

from tests.integration._synth import (
    ffprobe_json,
    make_mkv_with_srt,
    streams_of_type,
    wait_task,
)


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe requis pour les tests d'intégration encode",
)


@pytest.fixture(autouse=True)
def _qt_app(qt_app):
    return qt_app


def _workflow() -> EncodeWorkflow:
    return EncodeWorkflow(
        ffmpeg_bin="ffmpeg",
        dovi_tool_bin="dovi_tool",
        hdr10plus_bin="hdr10plus_tool",
        mediainfo_bin="mediainfo",
        ram_buffer_enabled=False,
        ffmpeg_threads=1,
        generate_nfo=False,
    )


def _assert_statistics(output: Path, *, expected_subtitle_elements: int) -> None:
    probe = ffprobe_json(output)
    for stream in probe.get("streams", []):
        stream_tags = stream.get("tags", {})
        assert stream_tags.get("NUMBER_OF_FRAMES", "").isdigit(), stream_tags
        assert stream_tags.get("NUMBER_OF_BYTES", "").isdigit(), stream_tags
        assert stream_tags.get("_STATISTICS_TAGS")
        assert stream_tags.get("_STATISTICS_WRITING_APP", "").startswith("Muxiveo")
    subtitle = streams_of_type(probe, "subtitle")[0]
    assert int(subtitle["tags"]["NUMBER_OF_FRAMES"]) == expected_subtitle_elements


@pytest.mark.parametrize("backend", ["auto", "ffmpeg", "native"])
def test_every_mux_backend_writes_track_statistics(tmp_path: Path, backend: str) -> None:
    src = tmp_path / "src.mkv"
    make_mkv_with_srt(src, duration=2.0)

    out = tmp_path / f"copy_{backend}.mkv"
    cfg = EncodeConfig(
        source=src,
        output=out,
        video=VideoEncodeSettings(codec="copy", source_path=src, stream_index=0),
        audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy", source_path=src)],
        copy_subtitles=False,
        subtitle_tracks=[(src, 2)],
        keep_chapters=False,
        duration_s=2.0,
        mux_backend=backend,
    )
    state = wait_task(_workflow().run(cfg), timeout=120.0)

    assert state["failed"] is None, state["failed"]
    _assert_statistics(out, expected_subtitle_elements=1)


def test_reencoded_output_statistics_match_written_packets(tmp_path: Path) -> None:
    """Réencodage réel : les statistiques décrivent la sortie, pas la source."""
    src = tmp_path / "src.mkv"
    make_mkv_with_srt(src, duration=2.0)

    out = tmp_path / "reencoded.mkv"
    cfg = EncodeConfig(
        source=src,
        output=out,
        video=VideoEncodeSettings(
            codec="libx264", source_path=src, stream_index=0,
            quality_mode=QualityMode.CRF, crf=30, preset="ultrafast",
        ),
        audio_tracks=[AudioTrackSettings(stream_index=1, codec="aac", source_path=src)],
        copy_subtitles=False,
        subtitle_tracks=[(src, 2)],
        keep_chapters=False,
        duration_s=2.0,
    )
    state = wait_task(_workflow().run(cfg), timeout=180.0)

    assert state["failed"] is None, state["failed"]
    _assert_statistics(out, expected_subtitle_elements=1)
    probe = ffprobe_json(out)
    video = streams_of_type(probe, "video")[0]
    assert video.get("codec_name") == "h264"
    # Octets mesurés sur la sortie réencodée : jamais ceux de la source.
    assert 0 < int(video["tags"]["NUMBER_OF_BYTES"]) < src.stat().st_size
