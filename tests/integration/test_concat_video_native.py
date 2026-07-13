from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

from tests.integration._synth import ffprobe_json, make_av_container, streams_of_type


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "concat_video.py"
SPEC = importlib.util.spec_from_file_location("concat_video_native_integration", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
concat_video = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(concat_video)

pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("ffmpeg", "ffprobe", "mediainfo")),
    reason="FFmpeg, FFprobe et MediaInfo requis",
)


@pytest.mark.parametrize(
    "use_intro,use_outro",
    [(True, False), (False, True), (True, True)],
    ids=("intro", "outro", "intro-outro"),
)
def test_concat_uses_muxiveo_native_without_mkvmerge(
    tmp_path: Path,
    monkeypatch,
    use_intro: bool,
    use_outro: bool,
) -> None:
    main = tmp_path / "main.mkv"
    intro = tmp_path / "intro.mkv"
    outro = tmp_path / "outro.mkv"
    output = tmp_path / "output.mkv"
    make_av_container(main, duration=0.8, vcodec="libx264", acodec="aac")
    make_av_container(intro, duration=0.3, vcodec="libx264", acodec="aac")
    make_av_container(outro, duration=0.3, vcodec="libx264", acodec="aac")
    monkeypatch.setattr(concat_video, "MUXIVEO", str(ROOT / "main.py"))
    monkeypatch.setattr(concat_video, "MKVMERGE", str(tmp_path / "missing-mkvmerge"))
    monkeypatch.setattr(concat_video, "FFMPEG", shutil.which("ffmpeg"))
    monkeypatch.setattr(concat_video, "FFPROBE", shutil.which("ffprobe"))
    monkeypatch.setattr(concat_video, "MEDIAINFO", shutil.which("mediainfo"))

    assert concat_video.concat_videos(
        str(main), str(output),
        intro_path=str(intro) if use_intro else None,
        outro_path=str(outro) if use_outro else None,
        workdir=str(tmp_path / "work"),
        mux_backend="auto",
    )

    probe = ffprobe_json(output)
    assert streams_of_type(probe, "video")
    assert streams_of_type(probe, "audio")
    assert not output.with_suffix(output.suffix + ".partial").exists()
