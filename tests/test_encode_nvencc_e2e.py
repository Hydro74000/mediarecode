"""
tests/test_encode_nvencc_e2e.py — Tests E2E du pipeline NVEncC (ffmpeg | nvencc → ffmpeg).

Ces tests :
1. génèrent un petit MKV via ffmpeg (testsrc 1s @ 240p),
2. construisent les 3 commandes du pipeline NVEncC,
3. exécutent réellement le pipeline (Popen + pipe stdin/stdout),
4. vérifient que la sortie est un MKV valide.

Skippés automatiquement si :
- ffmpeg n'est pas dans le PATH ;
- NVEncC n'est pas détecté ;
- aucun GPU NVIDIA accessible (gate `--check-features`).

Doivent tourner dans la distrobox configurée pour NVIDIA (CUDA + driver host).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.workflows.encode.models import QualityMode, VideoEncodeSettings  # noqa: E402
from core.workflows.encode.runtime.nvencc import (  # noqa: E402
    build_nvencc_pipeline,
    detect_nvencc_available,
    is_expected_nvencc_pipe_producer_exit,
    nvencc_intermediate_path,
)


# ---------------------------------------------------------------------------
# Détection runtime — skip propre si environnement non outillé
# ---------------------------------------------------------------------------

def _which(name: str) -> str | None:
    return shutil.which(name)


FFMPEG = _which("ffmpeg")
NVENCC = _which("nvencc") or _which("NVEncC") or _which("NVEncC64")

_skip_reason: str | None = None
if FFMPEG is None:
    _skip_reason = "ffmpeg introuvable dans le PATH"
elif NVENCC is None:
    _skip_reason = "nvencc introuvable dans le PATH (paquet rigaya non installé)"
else:
    _ok, _codecs = detect_nvencc_available(NVENCC)
    if not _ok:
        _skip_reason = "nvencc présent mais aucun codec NVENC supporté (pas de GPU NVIDIA ?)"

pytestmark = pytest.mark.skipif(
    _skip_reason is not None,
    reason=_skip_reason or "(unreachable)",
)

# Alias typés str : le module est skippé si l'un des binaires est None,
# donc dans les corps de test ils sont garantis présents.
FFMPEG_BIN: str = FFMPEG or "ffmpeg"
NVENCC_BIN: str = NVENCC or "nvencc"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_test_mkv(path: Path, duration: float = 1.0) -> None:
    """Génère un MKV de test (testsrc 240p, sans audio) via ffmpeg."""
    cmd = [
        FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=320x240:rate=25",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _run_nvencc_pipeline(commands: list[list[str]]) -> tuple[int, str]:
    """Exécute la séquence ``[decode, encode, remux]``.

    Phase 1 et 2 sont chaînées via Popen (stdout phase 1 → stdin phase 2).
    Phase 3 démarre une fois le bitstream intermédiaire complet.

    Returns:
        (returncode_total, stderr_combiné).  Un returncode != 0 signale un
        échec dans l'une des phases.
    """
    decode_cmd, encode_cmd, remux_cmd = commands

    # Phase 1 → Phase 2 : pipe.
    p1 = subprocess.Popen(decode_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p2 = subprocess.Popen(
        encode_cmd, stdin=p1.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    # Closer la copie stdout du parent côté p1 pour que SIGPIPE atteigne p1
    # quand p2 termine.
    if p1.stdout is not None:
        p1.stdout.close()

    try:
        _p2_out, p2_err = p2.communicate()
        p1_err = p1.stderr.read() if p1.stderr is not None else b""
        p1.wait()
    finally:
        for proc in (p2, p1):
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        for stream in (p1.stderr, p2.stdout, p2.stderr):
            if stream is not None:
                stream.close()

    # Vérifier le consommateur en premier : lorsqu'il échoue, FFmpeg reçoit
    # ensuite SIGPIPE et son « Broken pipe » n'est qu'une conséquence qui
    # masquerait sinon le vrai diagnostic NVEncC.
    if p2.returncode != 0:
        return p2.returncode, (
            f"phase2 failed: {p2_err.decode(errors='replace')}\n"
            f"phase1 stderr: {p1_err.decode(errors='replace')}"
        )
    p1_stderr = p1_err.decode(errors="replace")
    if not is_expected_nvencc_pipe_producer_exit(p1.returncode, p1_stderr):
        return p1.returncode, f"phase1 failed: {p1_err.decode(errors='replace')}"

    # Phase 3 : remux.
    p3 = subprocess.run(remux_cmd, capture_output=True)
    if p3.returncode != 0:
        return p3.returncode, f"phase3 failed: {p3.stderr.decode(errors='replace')}"
    return 0, ""


def _ffprobe_video_codec(path: Path) -> str:
    """Retourne le nom du codec vidéo de la première piste (ffprobe)."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


# ---------------------------------------------------------------------------
# Tests E2E
# ---------------------------------------------------------------------------

class TestNvenccPipelineE2E:
    def test_hevc_pipeline_produces_valid_mkv(self, tmp_path: Path):
        # 1. Source synthétique
        src = tmp_path / "src.mkv"
        _generate_test_mkv(src, duration=1.0)
        assert src.exists() and src.stat().st_size > 0

        # 2. Construire le pipeline NVEncC
        out = tmp_path / "out.mkv"
        intermediate = nvencc_intermediate_path(tmp_path, "nvencc_hevc")
        video = VideoEncodeSettings(
            codec="nvencc_hevc",
            quality_mode=QualityMode.CRF,
            crf=24,  # rapide pour le test
        )
        commands = build_nvencc_pipeline(
            ffmpeg_bin=FFMPEG_BIN,
            nvencc_bin=NVENCC_BIN,
            video=video,
            source=src,
            output=out,
            intermediate=intermediate,
        )
        assert len(commands) == 3

        # 3. Exécution réelle
        rc, err = _run_nvencc_pipeline(commands)
        assert rc == 0, f"pipeline failed (rc={rc}): {err}"

        # 4. Vérif sortie
        assert out.exists() and out.stat().st_size > 0, "fichier de sortie vide ou absent"
        assert _ffprobe_video_codec(out) == "hevc"

    def test_h264_pipeline_produces_valid_mkv(self, tmp_path: Path):
        src = tmp_path / "src.mkv"
        _generate_test_mkv(src, duration=1.0)

        out = tmp_path / "out.mkv"
        intermediate = nvencc_intermediate_path(tmp_path, "nvencc_h264")
        video = VideoEncodeSettings(
            codec="nvencc_h264",
            quality_mode=QualityMode.CRF,
            crf=24,
        )
        commands = build_nvencc_pipeline(
            ffmpeg_bin=FFMPEG_BIN,
            nvencc_bin=NVENCC_BIN,
            video=video,
            source=src,
            output=out,
            intermediate=intermediate,
        )

        rc, err = _run_nvencc_pipeline(commands)
        assert rc == 0, f"pipeline failed (rc={rc}): {err}"
        assert out.exists() and out.stat().st_size > 0
        assert _ffprobe_video_codec(out) == "h264"

    def test_av1_pipeline_produces_valid_mkv(self, tmp_path: Path):
        # AV1 nécessite Ada Lovelace (RTX 40xx) ; sinon skip propre.
        _, codecs = detect_nvencc_available(NVENCC)
        if "nvencc_av1" not in codecs:
            pytest.skip("AV1 non supporté par ce GPU (Ada Lovelace requis)")

        src = tmp_path / "src.mkv"
        _generate_test_mkv(src, duration=1.0)

        out = tmp_path / "out.mkv"
        intermediate = nvencc_intermediate_path(tmp_path, "nvencc_av1")
        video = VideoEncodeSettings(
            codec="nvencc_av1",
            quality_mode=QualityMode.CRF,
            crf=30,
        )
        commands = build_nvencc_pipeline(
            ffmpeg_bin=FFMPEG_BIN,
            nvencc_bin=NVENCC_BIN,
            video=video,
            source=src,
            output=out,
            intermediate=intermediate,
        )

        rc, err = _run_nvencc_pipeline(commands)
        assert rc == 0, f"pipeline failed (rc={rc}): {err}"
        assert out.exists() and out.stat().st_size > 0
        assert _ffprobe_video_codec(out) == "av1"

    def test_extra_params_propagated_to_nvencc(self, tmp_path: Path):
        """Vérifie que les flags --aq --aq-temporal --aq-strength 12 sont actifs."""
        src = tmp_path / "src.mkv"
        _generate_test_mkv(src, duration=1.0)

        out = tmp_path / "out.mkv"
        intermediate = nvencc_intermediate_path(tmp_path, "nvencc_hevc")
        video = VideoEncodeSettings(
            codec="nvencc_hevc",
            quality_mode=QualityMode.CRF,
            crf=24,
            extra_params="--aq --aq-temporal --aq-strength 12",
        )
        commands = build_nvencc_pipeline(
            ffmpeg_bin=FFMPEG_BIN,
            nvencc_bin=NVENCC_BIN,
            video=video,
            source=src,
            output=out,
            intermediate=intermediate,
        )
        # Phase 2 doit contenir tous les flags.
        encode_cmd = commands[1]
        assert "--aq" in encode_cmd
        assert "--aq-temporal" in encode_cmd
        assert "12" in encode_cmd

        rc, err = _run_nvencc_pipeline(commands)
        assert rc == 0, f"pipeline avec extra_params failed (rc={rc}): {err}"
        assert out.exists() and out.stat().st_size > 0
        assert _ffprobe_video_codec(out) == "hevc"


class TestRealHardwareDetection:
    """Vérifie la détection réelle (pas de mock) sur la machine NVIDIA hôte."""

    def test_detect_nvencc_real(self):
        ok, codecs = detect_nvencc_available(NVENCC)
        assert ok is True
        assert "nvencc_hevc" in codecs
        assert "nvencc_h264" in codecs
        # AV1 dépend du GPU ; on vérifie juste que la sortie est cohérente.

    def test_hardware_detector_includes_nvencc(self):
        from core.workflows.encode.hardware import HardwareEncoderDetector

        detector = HardwareEncoderDetector()
        available, _ff = detector.detect("ffmpeg", nvencc_bin=NVENCC)
        assert "hevc_nvenc" in available, "NVENC ffmpeg requis pour exposer NVEncC"
        assert "nvencc_hevc" in available
        assert "nvencc_h264" in available


class TestDashboardNvenccBadges:
    """Vérifie que la DashboardPage expose et active les 3 badges NVEncC."""

    def test_dashboard_marks_nvencc_badges_available(self, tmp_path: Path):
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])  # noqa: F841

        from core.config import AppConfig
        from ui.main_window import DashboardPage

        cfg = AppConfig()

        # Si la machine de test n'a pas NVEncC réellement résolu, on skip.
        if not cfg.tool_nvencc or not Path(cfg.tool_nvencc).exists():
            pytest.skip("tool_nvencc non résolu — test dashboard sans pertinence ici")

        page = DashboardPage(cfg, log=lambda *a, **k: None)

        # Les 3 badges nvencc_* doivent être enregistrés
        assert "nvencc_hevc" in page._hw_badges
        assert "nvencc_h264" in page._hw_badges
        assert "nvencc_av1" in page._hw_badges

        # Lancer la détection synchrone (pas via _start_hw_detection qui passe
        # par un executor — on appelle la méthode worker directement).
        emitted: list[set[str]] = []
        page._hw_detected.connect(emitted.append)
        page._run_hw_detection()
        # Forcer le traitement de la queue Qt pour faire arriver le signal.
        app.processEvents()

        assert emitted, "aucun signal _hw_detected émis"
        available = emitted[-1]
        # Au moins HEVC + H.264 doivent passer (AV1 dépend du GPU).
        assert "nvencc_hevc" in available
        assert "nvencc_h264" in available

        # Appliquer manuellement le slot pour vérifier que les badges
        # passent bien en état "available".
        page._on_hw_detected(available)
        for codec in ("nvencc_hevc", "nvencc_h264"):
            badge, _label = page._hw_badges[codec]
            # Le style appliqué pour "available" inclut un accent vert distinct.
            # On vérifie au minimum que le tooltip ou le style ne reste pas en "pending".
            ss = badge.styleSheet()
            assert "available" in ss.lower() or "#" in ss
            # Mieux : on relit les attributs custom appliqués au QLabel.
            # (_apply_encoder_badge_state n'expose pas un état, mais le ss change.)
            assert ss, f"badge {codec} sans style appliqué"
