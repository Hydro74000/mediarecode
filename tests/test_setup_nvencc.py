"""Tests pour les helpers d'install NVEncC dans setup.py."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(scope="module")
def setup_mod():
    """Importe setup.py comme module (script CLI) et l'enregistre dans sys.modules."""
    import sys
    spec = importlib.util.spec_from_file_location(
        "setup_module",
        Path(__file__).parent.parent / "setup.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["setup_module"] = mod  # requis pour patch("setup_module.X")
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# GITHUB_TOOLS — entrée nvencc
# ---------------------------------------------------------------------------

class TestNvenccGithubToolEntry:
    def test_nvencc_registered(self, setup_mod):
        assert "nvencc" in setup_mod.GITHUB_TOOLS

    def test_macos_excluded(self, setup_mod):
        meta = setup_mod.GITHUB_TOOLS["nvencc"]
        assert "Darwin" not in meta.get("platforms", [])
        assert ("Darwin", "x86_64") not in meta.get("asset_patterns", {})

    def test_gate_is_nvenc_available(self, setup_mod):
        assert setup_mod.GITHUB_TOOLS["nvencc"].get("gate") == "nvenc_available"

    def test_linux_x86_64_has_deb_with_rpm_fallback(self, setup_mod):
        pattern = setup_mod.GITHUB_TOOLS["nvencc"]["asset_patterns"][("Linux", "x86_64")]
        assert pattern["fmt"] == "deb"
        assert pattern["suffix"].endswith(".deb")
        assert pattern.get("alt_fmt") == "rpm"
        assert pattern.get("alt_suffix", "").endswith(".rpm")

    def test_windows_x86_64_uses_7z(self, setup_mod):
        pattern = setup_mod.GITHUB_TOOLS["nvencc"]["asset_patterns"][("Windows", "x86_64")]
        assert pattern["fmt"] == "7z"
        assert pattern["suffix"].endswith(".7z")


# ---------------------------------------------------------------------------
# _check_nvenc_available — gate runtime
# ---------------------------------------------------------------------------

class TestCheckNvencAvailable:
    def test_nvidia_smi_success_returns_true(self, setup_mod):
        fake = subprocess.CompletedProcess(
            args=["nvidia-smi"], returncode=0,
            stdout=b"GPU 0: NVIDIA RTX 4070 (UUID: GPU-...)",
            stderr=b"",
        )
        with patch("subprocess.run", return_value=fake):
            assert setup_mod._check_nvenc_available() is True

    def test_nvidia_smi_failure_falls_back_to_dev_node(self, setup_mod):
        fake = subprocess.CompletedProcess(
            args=["nvidia-smi"], returncode=255,
            stdout=b"", stderr=b"NVIDIA-SMI has failed",
        )
        # Path("/dev/nvidia0").exists() doit retourner True via le mock pathlib.
        with patch("subprocess.run", return_value=fake), \
             patch("pathlib.Path.exists", return_value=True), \
             patch.object(setup_mod, "OS", "Linux"):
            assert setup_mod._check_nvenc_available() is True

    def test_no_nvidia_smi_no_dev_node_returns_false(self, setup_mod):
        with patch("subprocess.run", side_effect=FileNotFoundError()), \
             patch.object(setup_mod, "OS", "Linux"):
            with patch("pathlib.Path.exists", return_value=False):
                assert setup_mod._check_nvenc_available() is False

    def test_windows_no_dev_node_check(self, setup_mod):
        # Sur Windows : seul nvidia-smi est consulté (pas de /dev/nvidia0).
        with patch("subprocess.run", side_effect=FileNotFoundError()), \
             patch.object(setup_mod, "OS", "Windows"):
            assert setup_mod._check_nvenc_available() is False

    def test_timeout_returns_false(self, setup_mod):
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=5),
        ), patch.object(setup_mod, "OS", "Linux"):
            with patch("pathlib.Path.exists", return_value=False):
                assert setup_mod._check_nvenc_available() is False


class TestWindowsRequiredTools:
    def test_excludes_eac3to_and_includes_nvencc_only_with_nvidia(self, setup_mod):
        with patch.object(setup_mod, "_check_nvenc_available", return_value=False):
            assert setup_mod.windows_required_tool_names() == setup_mod.WINDOWS_REQUIRED_TOOLS
        with patch.object(setup_mod, "_check_nvenc_available", return_value=True):
            assert setup_mod.windows_required_tool_names() == (
                *setup_mod.WINDOWS_REQUIRED_TOOLS, "nvencc"
            )

    def test_report_returns_only_required_missing_tools(self, setup_mod, tmp_path):
        paths = {"ffmpeg": r"C:\\ffmpeg.exe", "eac3to": r"C:\\eac3to.exe"}
        with patch.object(setup_mod, "_check_nvenc_available", return_value=False), \
             patch.object(setup_mod, "_detect_tool_path", side_effect=lambda name, _prefix: paths.get(name)):
            report = setup_mod.check_windows_required_tools(tmp_path)

        assert report.found == {"ffmpeg": r"C:\\ffmpeg.exe"}
        assert report.missing == ("ffprobe", "mediainfo", "dovi_tool", "hdr10plus_tool")
        assert "eac3to" not in report.required

    def test_report_honors_explicit_windows_tool_path(self, setup_mod, tmp_path):
        ffmpeg = tmp_path / "ffmpeg.exe"
        ffmpeg.write_text("placeholder", encoding="utf-8")
        ini = tmp_path / "config.ini"
        ini.write_text(f"[tools]\nffmpeg = {ffmpeg}\n", encoding="utf-8")
        with patch.object(setup_mod, "OS", "Windows"), \
             patch.object(setup_mod, "_check_nvenc_available", return_value=False), \
             patch.object(setup_mod, "_config_ini_path", return_value=ini), \
             patch.object(setup_mod, "_detect_windows_tool_path", return_value=None), \
             patch.object(setup_mod.shutil, "which", return_value=None):
            report = setup_mod.check_windows_required_tools(tmp_path)

        assert report.found["ffmpeg"] == str(ffmpeg)

    def test_ensure_installs_only_missing_groups(self, setup_mod, tmp_path):
        missing = setup_mod.ToolPresenceReport(
            setup_mod.WINDOWS_REQUIRED_TOOLS, {}, ("ffprobe", "dovi_tool")
        )
        healthy = setup_mod.ToolPresenceReport(
            setup_mod.WINDOWS_REQUIRED_TOOLS, {"ffprobe": "x", "dovi_tool": "y"}, ()
        )
        with patch.object(setup_mod, "check_windows_required_tools", side_effect=[missing, healthy]), \
             patch.object(setup_mod, "install_winget") as install_winget, \
             patch.object(setup_mod, "install_github_tools") as install_github, \
             patch.object(setup_mod, "autofill_windows_config_ini") as autofill:
            report = setup_mod.ensure_windows_required_tools(tmp_path)

        assert report.healthy
        install_winget.assert_called_once_with(False, force=False, tool_names={"ffprobe"})
        install_github.assert_called_once_with(tmp_path, False, force=False, tool_names={"dovi_tool"})
        autofill.assert_called_once_with(tmp_path, False, force=False)


# ---------------------------------------------------------------------------
# _is_atomic_distro — skip natif install sur Silverblue/Kinoite
# ---------------------------------------------------------------------------

class TestIsAtomicDistro:
    def test_ostree_booted_marker(self, setup_mod, tmp_path: Path):
        # /run/ostree-booted présent → distro atomique.
        with patch("pathlib.Path.exists", return_value=True):
            assert setup_mod._is_atomic_distro() is True

    def test_no_marker_no_rpm_ostree(self, setup_mod):
        with patch("pathlib.Path.exists", return_value=False), \
             patch("shutil.which", return_value=None):
            assert setup_mod._is_atomic_distro() is False

    def test_rpm_ostree_status_ok(self, setup_mod):
        fake = subprocess.CompletedProcess(
            args=["rpm-ostree", "status"], returncode=0,
            stdout=b"State: idle", stderr=b"",
        )
        with patch("pathlib.Path.exists", return_value=False), \
             patch("shutil.which", return_value="/usr/bin/rpm-ostree"), \
             patch("subprocess.run", return_value=fake):
            assert setup_mod._is_atomic_distro() is True


# ---------------------------------------------------------------------------
# install_dnf — skip propre sur distro atomique
# ---------------------------------------------------------------------------

class TestInstallDnfAtomic:
    def test_skips_dnf_install_on_atomic_distros(self, setup_mod, capsys):
        with patch.object(setup_mod, "_is_atomic_distro", return_value=True), \
             patch(
                 "shutil.which",
                 side_effect=lambda exe: None if exe in {"ffmpeg", "openGL"} else f"/usr/bin/{exe}",
             ), \
             patch.object(setup_mod, "_ensure_rpmfusion") as ensure_rpmfusion, \
             patch.object(setup_mod, "run") as run_mock:
            setup_mod.install_dnf(dry_run=False, force=False)

        out = capsys.readouterr().out
        assert "distribution atomique" in out.lower()
        assert "rpm-ostree" in out
        assert "mediainfo" in out
        assert "mesa-libEGL" in out
        assert "distrobox" in out
        ensure_rpmfusion.assert_not_called()
        run_mock.assert_not_called()


# ---------------------------------------------------------------------------
# install_github_tools — gate macOS et NVENC
# ---------------------------------------------------------------------------

class TestInstallGithubToolsGates:
    def test_skips_nvencc_on_macos(self, setup_mod, tmp_path: Path, capsys):
        # macOS exclu de la liste platforms → skip silencieux.
        fake_meta = {
            "nvencc": {
                "repo": "rigaya/NVEnc",
                "desc": "test",
                "gate": "nvenc_available",
                "platforms": ["Linux", "Windows"],
                "binary_name": {"Linux": "nvencc", "Windows": "NVEncC.exe"},
                "asset_patterns": {},
            },
        }
        with patch.object(setup_mod, "GITHUB_TOOLS", fake_meta), \
             patch.object(setup_mod, "OS", "Darwin"), \
             patch.object(setup_mod, "_check_nvenc_available", return_value=True), \
             patch.object(setup_mod, "_update_ini_tools_section"):
            setup_mod.install_github_tools(tmp_path, dry_run=True, force=False)
        out = capsys.readouterr().out
        assert "nvencc" in out
        # Pas de download tenté.
        assert "download" not in out.lower() or "skipping" in out.lower()

    def test_skips_nvencc_when_no_nvidia(self, setup_mod, tmp_path: Path, capsys):
        fake_meta = {
            "nvencc": {
                "repo": "rigaya/NVEnc",
                "desc": "test",
                "gate": "nvenc_available",
                "platforms": ["Linux"],
                "binary_name": {"Linux": "nvencc"},
                "asset_patterns": {("Linux", "x86_64"): {"suffix": ".deb", "fmt": "deb"}},
            },
        }
        with patch.object(setup_mod, "GITHUB_TOOLS", fake_meta), \
             patch.object(setup_mod, "OS", "Linux"), \
             patch.object(setup_mod, "_check_nvenc_available", return_value=False), \
             patch.object(setup_mod, "_update_ini_tools_section"):
            setup_mod.install_github_tools(tmp_path, dry_run=True, force=False)
        out = capsys.readouterr().out
        assert "NVENC not detected" in out or "skipping" in out.lower()

    def test_proceeds_when_nvenc_detected(self, setup_mod, tmp_path: Path):
        # Ne fait pas le download réel ; on vérifie juste que le gate ne bloque
        # pas et qu'on atteint la phase d'asset selection.
        fake_meta = {
            "nvencc": {
                "repo": "rigaya/NVEnc",
                "desc": "test",
                "gate": "nvenc_available",
                "platforms": ["Linux"],
                "binary_name": {"Linux": "nvencc"},
                "asset_patterns": {
                    ("Linux", "x86_64"): {"suffix": ".deb", "fmt": "deb"},
                },
            },
        }
        with patch.object(setup_mod, "GITHUB_TOOLS", fake_meta), \
             patch.object(setup_mod, "OS", "Linux"), \
             patch.object(setup_mod, "_check_nvenc_available", return_value=True), \
             patch.object(setup_mod, "_update_ini_tools_section"), \
             patch.object(setup_mod, "_arch_key", return_value="x86_64"):
            # dry_run=True évite le download/install réel
            setup_mod.install_github_tools(tmp_path, dry_run=True, force=False)
        # Pas d'exception → gate passé.
