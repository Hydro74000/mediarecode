#!/usr/bin/env python3
"""
Muxiveo — Setup & Dependency Installer
==========================================

Installs Python dependencies and external tools required by the application.

  Linux (Debian/Ubuntu)  → pip + apt packages + GitHub binaries
  Linux (Fedora/RHEL)    → pip + dnf packages + GitHub binaries
  macOS                  → pip + Homebrew packages + GitHub binaries
  Windows                → pip + winget packages + GitHub binaries (user-local)

Usage:
    See platform-specific command shown by --help output.

Options:
    --no-github     Skip downloading dovi_tool / hdr10plus_tool from GitHub
    --prefix PATH   Installation prefix for GitHub binaries
                    Default: /usr/local on Linux/macOS,
                             <Muxiveo folder>\\tools on Windows
    --dry-run       Print what would be done without executing anything
    --force         Retry installs and regenerate Windows tool paths
"""

from __future__ import annotations

import argparse
import ctypes
import configparser
import json
import locale
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Optional

from core.lang_tags import Rfc5646LanguageTags
from core.version import APP_CONFIG_DIR_NAME

# ---------------------------------------------------------------------------
# Terminal colours (no external deps)
# ---------------------------------------------------------------------------

def _ensure_text_stream(name: str, mode: str) -> None:
    """Provide a valid stdio stream when running without a console."""
    if getattr(sys, name, None) is None:
        setattr(sys, name, open(os.devnull, mode, encoding="utf-8"))


def _stream_isatty(stream: object) -> bool:
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except OSError:
        return False


_ensure_text_stream("stdout", "w")
_ensure_text_stream("stderr", "w")


def _can_stream_encode(stream: object, text: str) -> bool:
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return False
    try:
        text.encode(encoding)
        return True
    except Exception:
        return False


def _configure_stdio_for_windows() -> None:
    """
    Configure stdout/stderr on Windows so console output never crashes on
    Unicode glyphs. If UTF-8 reconfigure is unavailable, fallback to
    `errors=replace`.
    """
    if platform.system() != "Windows":
        return

    probe = "✔→⚠✘▸─"
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        if _can_stream_encode(stream, probe):
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            try:
                reconfigure(errors="replace")
            except Exception:
                pass


_configure_stdio_for_windows()
_USE_COLOR = _stream_isatty(sys.stdout) and platform.system() != "Windows"
_UI_UNICODE = _can_stream_encode(sys.stdout, "✔→⚠✘▸─")
_UI_OK = "✔" if _UI_UNICODE else "OK"
_UI_INFO = "→" if _UI_UNICODE else "->"
_UI_WARN = "⚠" if _UI_UNICODE else "!"
_UI_ERR = "✘" if _UI_UNICODE else "X"
_UI_STEP = "▸" if _UI_UNICODE else ">"
_UI_BAR = "─" if _UI_UNICODE else "-"

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text

def ok(msg: str)     -> None: print(_c("32", f"  {_UI_OK}  {msg}"))
def info(msg: str)   -> None: print(_c("36", f"  {_UI_INFO}  {msg}"))
def warn(msg: str)   -> None: print(_c("33", f"  {_UI_WARN}  {msg}"))
def error(msg: str)  -> None: print(_c("31", f"  {_UI_ERR}  {msg}"), file=sys.stderr)
def title(msg: str)  -> None: print(_c("1;34", f"\n{_UI_BAR*60}\n  {msg}\n{_UI_BAR*60}"))
def step(msg: str)   -> None: print(_c("1;37", f"\n  {_UI_STEP} {msg}"))

# ---------------------------------------------------------------------------
# OS detection
# ---------------------------------------------------------------------------

OS      = platform.system()           # "Linux" | "Windows" | "Darwin"
MACHINE = platform.machine().lower()  # "x86_64" | "aarch64" | "arm64" | "amd64"
PYTHON_CMD = "py" if OS == "Windows" else "python3"

WINDOWS_TOOL_FILENAMES: dict[str, tuple[str, ...]] = {
    "ffmpeg": ("ffmpeg.exe",),
    "ffprobe": ("ffprobe.exe",),
    "mediainfo": ("MediaInfo.exe", "mediainfo.exe"),
    "dovi_tool": ("dovi_tool.exe",),
    "hdr10plus_tool": ("hdr10plus_tool.exe",),
    "eac3to": ("eac3to.exe",),
}

WINDOWS_WINGET_PATTERNS: dict[str, tuple[str, ...]] = {
    "ffmpeg": ("Gyan.FFmpeg*",),
    "ffprobe": ("Gyan.FFmpeg*",),
    "mediainfo": ("MediaArea.MediaInfo_*",),
}

WINDOWS_CONFIG_TOOL_ORDER: tuple[str, ...] = (
    "ffmpeg",
    "ffprobe",
    "mediainfo",
    "dovi_tool",
    "hdr10plus_tool",
    "eac3to",
)

# Outils qui écrivent dans les dossiers protégés (Windows CFA allowlist).
WINDOWS_CFA_WRITER_TOOLS: tuple[str, ...] = (
    "ffmpeg",
)

def detect_linux_distro() -> str:
    """Return 'debian', 'fedora', or 'unknown'."""
    try:
        with open("/etc/os-release") as f:
            text = f.read().lower()
        if any(k in text for k in ("ubuntu", "debian", "linuxmint", "pop!_os", "kali", "raspbian")):
            return "debian"
        if any(k in text for k in ("fedora", "rhel", "centos", "rocky", "almalinux", "nobara")):
            return "fedora"
    except FileNotFoundError:
        pass
    if shutil.which("apt-get"):
        return "debian"
    if shutil.which("dnf") or shutil.which("yum"):
        return "fedora"
    return "unknown"

# ---------------------------------------------------------------------------
# Python requirements
# ---------------------------------------------------------------------------

PYTHON_PACKAGES = [
    "PySide6",
    "pymediainfo>=6.1.0",
]

# ---------------------------------------------------------------------------
# External tools definition
# ---------------------------------------------------------------------------

# Tools available via system package managers
# format: { "executable": {"apt": "pkg", "dnf": "pkg", "brew": "pkg",
#                          "winget": "id", "desc": "..."} }
SYSTEM_TOOLS: dict[str, dict] = {
    "pip": {
        "apt":    "python3-pip",
        "dnf":    "python3-pip",
        "brew":   "python",
        "winget": "buyukakyuz.install-nothing",
        "desc":   "Python Package Installer",
    },
    "openGL": {
        "apt":    "libegl1-mesa",
        "dnf":    "mesa-libEGL",
        "brew":   "xquartz",
        "winget": "",
        "desc":   "OpenGL libraries",
        "path_check": False,
    },
    "ffmpeg": {
        "apt":    "ffmpeg",
        "dnf":    "ffmpeg",
        "brew":   "ffmpeg",
        "winget": "Gyan.FFmpeg",
        "desc":   "Video/audio encoder and converter",
        "dnf_note": "Requires RPM Fusion (handled automatically)",
    },
    "ffprobe": {
        "apt":    "ffmpeg",
        "dnf":    "ffmpeg",
        "brew":   "ffmpeg",
        "winget": "Gyan.FFmpeg",
        "desc":   "Media file analyser (ships with ffmpeg)",
    },
    "mediainfo": {
        "apt":    "mediainfo",
        "dnf":    "mediainfo",
        "brew":   "mediainfo",
        "winget": "MediaArea.MediaInfo",
        "desc":   "Media metadata tool",
    },
}

# Tools distributed as GitHub release binaries.
#
# asset_patterns keys: (OS, arch_key)
#   OS       — "Linux" | "Darwin" | "Windows"
#   arch_key — "x86_64" | "arm64"
#
# Each value:
#   suffix  — substring that uniquely identifies the asset filename
#   fmt     — "tar.gz" or "zip"
GITHUB_TOOLS: dict[str, dict] = {
    "dovi_tool": {
        "repo": "quietvoid/dovi_tool",
        "desc": "Dolby Vision RPU extraction and injection",
        "binary_name": {
            "Linux":   "dovi_tool",
            "Darwin":  "dovi_tool",
            "Windows": "dovi_tool.exe",
        },
        "asset_patterns": {
            ("Linux",   "x86_64"): {"suffix": "x86_64-unknown-linux-musl.tar.gz",  "fmt": "tar.gz"},
            ("Linux",   "arm64"):  {"suffix": "aarch64-unknown-linux-musl.tar.gz", "fmt": "tar.gz"},
            ("Darwin",  "x86_64"): {"suffix": "universal-macOS.zip",               "fmt": "zip"},
            ("Darwin",  "arm64"):  {"suffix": "universal-macOS.zip",               "fmt": "zip"},
            ("Windows", "x86_64"): {"suffix": "x86_64-pc-windows-msvc.zip",        "fmt": "zip"},
            ("Windows", "arm64"):  {"suffix": "aarch64-pc-windows-msvc.zip",       "fmt": "zip"},
        },
    },
    "hdr10plus_tool": {
        "repo": "quietvoid/hdr10plus_tool",
        "desc": "HDR10+ metadata extraction and injection",
        "binary_name": {
            "Linux":   "hdr10plus_tool",
            "Darwin":  "hdr10plus_tool",
            "Windows": "hdr10plus_tool.exe",
        },
        "asset_patterns": {
            ("Linux",   "x86_64"): {"suffix": "x86_64-unknown-linux-musl.tar.gz",  "fmt": "tar.gz"},
            ("Linux",   "arm64"):  {"suffix": "aarch64-unknown-linux-musl.tar.gz", "fmt": "tar.gz"},
            ("Darwin",  "x86_64"): {"suffix": "universal-macOS.zip",               "fmt": "zip"},
            ("Darwin",  "arm64"):  {"suffix": "universal-macOS.zip",               "fmt": "zip"},
            ("Windows", "x86_64"): {"suffix": "x86_64-pc-windows-msvc.zip",        "fmt": "zip"},
            ("Windows", "arm64"):  {"suffix": "aarch64-pc-windows-msvc.zip",       "fmt": "zip"},
        },
    },
    "nvencc": {
        "repo": "rigaya/NVEnc",
        "desc": "NVEncC — wrapper standalone NVIDIA NVENC (rigaya)",
        # Conditionnel : ne s'installe que si NVENC est détecté sur la machine
        # (driver NVIDIA présent). Vérifié via _check_nvenc_available() avant download.
        "gate": "nvenc_available",
        # Plateformes supportées (macOS exclu, rigaya ne fournit pas de build).
        "platforms": ["Linux", "Windows"],
        "binary_name": {
            "Linux":   "nvencc",         # /usr/bin/nvencc (dnf) ou /usr/bin/NVEncC (apt)
            "Windows": "NVEncC64.exe",
        },
        "asset_patterns": {
            # Linux : .deb (Debian/Ubuntu) puis fallback .rpm (Fedora/RHEL).
            ("Linux",   "x86_64"): {"suffix": "_amd64.deb",       "fmt": "deb",
                                    "alt_suffix": ".x86_64.rpm",  "alt_fmt": "rpm"},
            # Windows : archive 7z. Nécessite py7zr (déclaré ci-dessous).
            ("Windows", "x86_64"): {"suffix": "_x64.7z",          "fmt": "7z"},
        },
    },
}

# Tools with no automated install path (optional)
MANUAL_TOOLS: dict[str, dict] = {
    "eac3to": {
        "desc": "Advanced audio conversion (Windows only, optional)",
        "note": "Download from: https://forum.doom9.org/showthread.php?t=125966",
        "platforms": ["Windows"],
    },
}

# ---------------------------------------------------------------------------
# Command runner
# ---------------------------------------------------------------------------

def _windows_no_window_subprocess_kwargs() -> dict[str, Any]:
    """Return subprocess kwargs that hide console windows on Windows."""
    if OS != "Windows":
        return {}
    # If a console is already visible (first-launch setup), keep child process
    # output in that same console.
    try:
        if bool(ctypes.windll.kernel32.GetConsoleWindow()):  # type: ignore[attr-defined]
            return {}
    except Exception:
        pass

    if not getattr(sys, "frozen", False):
        # CLI execution from a terminal should keep standard behavior.
        return {}

    kwargs: dict[str, Any] = {}

    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if create_no_window:
        kwargs["creationflags"] = create_no_window

    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_cls is not None:
        startupinfo = startupinfo_cls()
        startf_use_showwindow = getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        if startf_use_showwindow:
            startupinfo.dwFlags |= startf_use_showwindow
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = startupinfo

    return kwargs


def run(cmd: list[str], dry_run: bool = False, check: bool = True,
        capture: bool = False) -> Optional[subprocess.CompletedProcess]:
    """Run a shell command, optionally printing only (dry_run)."""
    display = " ".join(str(c) for c in cmd)
    info(f"$ {display}")
    if dry_run:
        return None
    result = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        check=False,
        **_windows_no_window_subprocess_kwargs(),
    )
    if check and result.returncode != 0:
        stderr = getattr(result, "stderr", "")
        raise RuntimeError(
            f"Command failed (exit {result.returncode}): {display}"
            + (f"\n{stderr.strip()}" if stderr else "")
        )
    return result


# ---------------------------------------------------------------------------
# Privilege helpers
# ---------------------------------------------------------------------------

def is_root() -> bool:
    return os.getuid() == 0 if hasattr(os, "getuid") else False

def sudo_prefix(dry_run: bool = False) -> list[str]:
    """Return ['sudo'] if not root and sudo is available, else [].
    In dry-run mode never raises — returns ['sudo'] as a placeholder."""
    if is_root():
        return []
    if shutil.which("sudo"):
        return ["sudo"]
    if dry_run:
        return ["sudo"]
    raise RuntimeError(
        "This step requires root privileges. "
        "Re-run as root or install sudo."
    )

# ---------------------------------------------------------------------------
# Architecture helpers
# ---------------------------------------------------------------------------

def _arch_key() -> str:
    """Normalise platform.machine() to 'x86_64' or 'arm64'."""
    m = MACHINE
    if m in ("x86_64", "amd64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    raise RuntimeError(
        f"Unsupported architecture '{platform.machine()}'. "
        "Install dovi_tool and hdr10plus_tool manually."
    )

def _is_msix_install() -> bool:
    """
    True si l'application tourne depuis un package MSIX installé
    (dossier Program Files\\WindowsApps, lecture seule).
    """
    if OS != "Windows":
        return False
    try:
        exe = Path(sys.executable).resolve()
    except Exception:
        return False
    lower = str(exe).lower()
    return "\\windowsapps\\" in lower or "/windowsapps/" in lower


def _exe_dir_is_readonly() -> bool:
    """
    True si le dossier contenant l'exécutable n'est pas writable
    (cas typique : installation admin dans Program Files).
    """
    if OS != "Windows":
        return False
    try:
        exe_dir = Path(sys.executable).resolve().parent
    except Exception:
        return False
    try:
        probe = exe_dir / ".Muxiveo_write_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return False
    except Exception:
        return True


def _windows_user_tools_dir() -> Path:
    """
    Dossier d'installation des outils binaires côté utilisateur.
    Sous MSIX, %LOCALAPPDATA%\\Muxiveo\\tools est la seule
    localisation writable sans élévation.
    """
    local_appdata = os.environ.get("LOCALAPPDATA")
    base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
    return base / "Muxiveo" / "tools"


def _default_prefix() -> Path:
    """Return a sensible default install prefix for the current OS."""
    if OS == "Windows":
        # Sous MSIX, Program Files\\WindowsApps est read-only pour l'utilisateur
        # → on installe dans %LOCALAPPDATA%\\Muxiveo\\tools.
        # En dev / installeur classique : tools/ à côté de setup.py.
        if _is_msix_install() or _exe_dir_is_readonly():
            return _windows_user_tools_dir()
        return Path(__file__).parent / "tools"
    return Path("/usr/local")


def _windows_config_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / APP_CONFIG_DIR_NAME
    return Path.home() / "AppData" / "Roaming" / APP_CONFIG_DIR_NAME


def _is_windows_frozen() -> bool:
    return OS == "Windows" and bool(getattr(sys, "frozen", False))


def _windows_frozen_runtime_dir() -> Path | None:
    """
    Return the frozen runtime directory containing python extension modules/DLLs.
    Supports onedir (`_internal`) and onefile (`_MEIPASS`) layouts.
    """
    if not _is_windows_frozen():
        return None

    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(str(meipass)))

    exe_dir = Path(sys.executable).resolve().parent
    candidates.append(exe_dir / "_internal")
    candidates.append(exe_dir)

    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def verify_windows_frozen_python_runtime() -> None:
    """
    Ensure critical Python runtime pieces are present in frozen Windows bundles.
    We explicitly check `_ctypes.pyd` + `libffi-*.dll`.
    """
    runtime_dir = _windows_frozen_runtime_dir()
    if runtime_dir is None:
        return

    required_patterns: dict[str, str] = {
        "_ctypes.pyd": "_ctypes.pyd",
        "libffi": "libffi-*.dll",
    }
    missing: list[str] = []
    for label, pattern in required_patterns.items():
        if not any(runtime_dir.glob(pattern)):
            missing.append(f"{label} ({pattern})")

    if missing:
        raise RuntimeError(
            "Bundle Python Windows incomplet (runtime manquant): "
            + ", ".join(missing)
            + ". Rebuild requis."
        )

    ok(f"Frozen Python runtime OK ({runtime_dir})")


def _config_ini_path() -> Path:
    """Return the config.ini path used by the application on the current OS."""
    if OS == "Windows":
        if getattr(sys, "frozen", False):
            return _windows_config_dir() / "config.ini"
        return Path(__file__).parent / "config.ini"
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return xdg / APP_CONFIG_DIR_NAME / "config.ini"


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _tools_section_bounds(lines: list[str]) -> tuple[int, int]:
    start = -1
    end = len(lines)

    for index, line in enumerate(lines):
        if line.strip().lower() == "[tools]":
            start = index
            break

    if start == -1:
        return -1, len(lines)

    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break

    return start, end


def _section_bounds(lines: list[str], section: str) -> tuple[int, int]:
    start = -1
    end = len(lines)
    target = f"[{section.lower()}]"

    for index, line in enumerate(lines):
        if line.strip().lower() == target:
            start = index
            break

    if start == -1:
        return -1, len(lines)

    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break

    return start, end


def _normalize_windows_backslashes(value: str) -> str:
    """Collapse repeated Windows path separators while preserving UNC prefixes."""
    text = str(value)
    if OS != "Windows" or "\\" not in text:
        return text

    leading = len(text) - len(text.lstrip("\\"))
    body = text[leading:]
    body = re.sub(r"\\{2,}", r"\\", body)

    if leading >= 2:
        prefix = "\\\\"
    elif leading == 1:
        prefix = "\\"
    else:
        prefix = ""
    return prefix + body


def _sanitize_windows_tools_lines(lines: list[str]) -> list[str]:
    if OS != "Windows":
        return lines

    start, end = _tools_section_bounds(lines)
    if start == -1:
        return lines

    for index in range(start + 1, end):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith(("#", ";")) or "=" not in stripped:
            continue
        lhs, rhs = stripped.split("=", 1)
        normalized = _normalize_windows_backslashes(rhs.strip())
        if normalized != rhs.strip():
            lines[index] = f"{lhs.strip()} = {normalized}"
    return lines


def _update_ini_tools_section(
    path: Path,
    tool_values: dict[str, str],
    dry_run: bool = False,
    replace_keys: set[str] | None = None,
    prune_keys: set[str] | None = None,
) -> None:
    """Ajoute les chemins détectés dans [tools] sans écraser une valeur explicite."""
    if not tool_values and not prune_keys:
        return
    replace_keys = {key.lower() for key in (replace_keys or set())}
    prune_keys = {key.lower() for key in (prune_keys or set())}

    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = _sanitize_windows_tools_lines(text.splitlines())
    start, end = _tools_section_bounds(lines)

    if start == -1:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["[tools]"])
        start = len(lines) - 1
        end = len(lines)

    if prune_keys:
        for index in range(end - 1, start, -1):
            stripped = lines[index].strip()
            if not stripped or stripped.startswith(("#", ";")) or "=" not in stripped:
                continue
            lhs, _rhs = stripped.split("=", 1)
            if lhs.strip().lower() not in prune_keys:
                continue
            del lines[index]
            end -= 1

    insert_at = end
    for key, value in tool_values.items():
        rendered = _normalize_windows_backslashes(value)
        updated = False
        for index in range(start + 1, end):
            stripped = lines[index].strip()
            if not stripped or stripped.startswith(("#", ";")) or "=" not in stripped:
                continue
            lhs, rhs = stripped.split("=", 1)
            if lhs.strip().lower() != key.lower():
                continue
            if not rhs.strip() or key.lower() in replace_keys:
                lines[index] = f"{key} = {rendered}"
            updated = True
            break

        if not updated:
            lines.insert(insert_at, f"{key} = {rendered}")
            insert_at += 1
            end += 1

    lines = _sanitize_windows_tools_lines(lines)

    if dry_run:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _system_language_code() -> str:
    candidates: list[str | None] = [
        os.environ.get("LC_ALL"),
        (os.environ.get("LANGUAGE") or "").split(":", 1)[0] or None,
        os.environ.get("LANG"),
    ]
    try:
        candidates.append(locale.getlocale()[0])
    except (TypeError, ValueError):
        pass

    for candidate in candidates:
        code = Rfc5646LanguageTags.from_locale_name(candidate)
        if code:
            return code
    return "eng"


def _available_ui_languages() -> list[tuple[str, str]]:
    """
    Return the list of UI languages available in locales.json as
    (iso639-2 code, display name) pairs, sorted by display name.
    """
    iso_names: dict[str, str] = {
        "eng": "English",
        "fra": "Français",
        "deu": "Deutsch",
        "spa": "Español",
        "ita": "Italiano",
        "por": "Português",
        "nld": "Nederlands",
        "pol": "Polski",
        "rus": "Русский",
        "jpn": "日本語",
        "zho": "中文",
        "kor": "한국어",
        "ara": "العربية",
    }
    try:
        locales_path = Path(__file__).parent / "locales.json"
        data: dict = json.loads(locales_path.read_text(encoding="utf-8"))
        codes: set[str] = set()
        for values in data.values():
            if isinstance(values, dict):
                codes.update(str(key).lower() for key in values)
        if codes:
            items = [(code, iso_names.get(code, code)) for code in sorted(codes)]
            return sorted(items, key=lambda item: item[1].lower())
    except Exception:
        pass
    return [("eng", "English"), ("fra", "Français")]


def _ask_language_dialog_qt_in_process(languages: list[tuple[str, str]]) -> str | None:
    """Show the language dialog in-process and return the selected code."""
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import (
            QApplication,
            QComboBox,
            QDialog,
            QDialogButtonBox,
            QLabel,
            QVBoxLayout,
        )
    except Exception:
        return None

    _app = QApplication.instance() or QApplication(sys.argv[:1])
    dlg = QDialog()
    dlg.setWindowTitle("Muxiveo - Interface Language")
    dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
    dlg.setMinimumWidth(380)
    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.setSpacing(10)
    label = QLabel("Select the interface language / Choisissez la langue:")
    label.setWordWrap(True)
    layout.addWidget(label)
    combo = QComboBox()
    for code, name in languages:
        combo.addItem(name, code)
    layout.addWidget(combo)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
    buttons.accepted.connect(dlg.accept)
    layout.addWidget(buttons)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        selected = combo.currentData()
        if isinstance(selected, str):
            return selected.strip().lower()
    return None


def _python_executable_for_qt_subprocess() -> str | None:
    """Return a Python interpreter suitable for `python -c` subprocess calls."""
    candidates = [getattr(sys, "executable", ""), getattr(sys, "_base_executable", "")]
    for candidate in candidates:
        if not candidate:
            continue
        name = Path(candidate).name.lower()
        if name.startswith("python") or name in {"py", "py.exe"}:
            return candidate

    for command in ("python3", "python", "py"):
        found = shutil.which(command)
        if found:
            return found
    return None


def _ask_language_dialog(languages: list[tuple[str, str]]) -> str | None:
    """Show a language picker and return the chosen ISO 639-2 code."""
    if not languages:
        return None

    valid_codes = {code for code, _ in languages}

    if getattr(sys, "frozen", False):
        # In PyInstaller bundles, sys.executable is Muxiveo.exe, not python.exe.
        code = _ask_language_dialog_qt_in_process(languages)
        if code in valid_codes:
            return code
    else:
        qt_script = """\
import sys, json
from PySide6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QLabel, QComboBox, QDialogButtonBox,
)
from PySide6.QtCore import Qt

languages = json.loads(sys.argv[1])
app = QApplication.instance() or QApplication(sys.argv[:1])
dlg = QDialog()
dlg.setWindowTitle("Muxiveo - Interface Language")
dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
dlg.setMinimumWidth(380)
layout = QVBoxLayout(dlg)
layout.setContentsMargins(16, 16, 16, 16)
layout.setSpacing(10)
label = QLabel("Select the interface language / Choisissez la langue:")
label.setWordWrap(True)
layout.addWidget(label)
combo = QComboBox()
for code, name in languages:
    combo.addItem(name, code)
layout.addWidget(combo)
buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
buttons.accepted.connect(dlg.accept)
layout.addWidget(buttons)
if dlg.exec() == QDialog.DialogCode.Accepted:
    print(combo.currentData(), end="")
"""
        python_exe = _python_executable_for_qt_subprocess()
        if python_exe:
            try:
                result = subprocess.run(
                    [python_exe, "-c", qt_script, json.dumps(languages)],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=120,
                    **_windows_no_window_subprocess_kwargs(),
                )
                if result.returncode == 0:
                    code = result.stdout.strip().lower()
                    if code in valid_codes:
                        return code
            except Exception:
                pass

    if not _stream_isatty(sys.stdin) or not _stream_isatty(sys.stdout):
        return None

    bar = _UI_BAR * 40
    print(f"\n  {bar}")
    print("  Select interface language / Choisissez la langue :")
    for index, (code, name) in enumerate(languages, 1):
        print(f"    {index:2d}. {name}  ({code})")
    print(f"  {bar}")
    try:
        raw = input(f"  Choice [1-{len(languages)}] (Enter = auto-detect): ").strip()
    except (EOFError, RuntimeError):
        return None
    if not raw:
        return None
    if raw.isdigit():
        selected_index = int(raw) - 1
        if 0 <= selected_index < len(languages):
            return languages[selected_index][0]
    return None


def initialize_config_ini_language(
    dry_run: bool,
    force: bool = False,
    ini_path: Path | None = None,
) -> None:
    """
    Initialise la langue UI dans config.ini.

    - Windows : popup de sélection de langue.
    - Linux / macOS : détection automatique depuis la locale système.
    """
    title("Step 5 — config.ini UI language")

    if ini_path is None:
        ini_path = _config_ini_path()

    parser = configparser.ConfigParser(
        inline_comment_prefixes=("#",),
        default_section="DEFAULT",
    )
    if ini_path.exists():
        parser.read(ini_path, encoding="utf-8")

    existing = ""
    if parser.has_option("ui", "language"):
        existing = parser.get("ui", "language").strip()

    if existing and not force:
        ok(f"config.ini already defines ui.language = {existing}")
        return

    chosen: str | None = None
    if not dry_run and OS == "Windows":
        chosen = _ask_language_dialog(_available_ui_languages())
        if chosen:
            info(f"Language selected by user: {chosen}")
        else:
            info("Language dialog cancelled or unavailable — falling back to system detection")

    detected = chosen or _system_language_code()
    info(f"UI language: {detected}")

    text = ini_path.read_text(encoding="utf-8") if ini_path.exists() else ""
    lines = text.splitlines()
    start, end = _section_bounds(lines, "ui")

    if start == -1:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["[ui]"])
        start = len(lines) - 1
        end = len(lines)

    updated = False
    for index in range(start + 1, end):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith(("#", ";")) or "=" not in stripped:
            continue
        lhs, _rhs = stripped.split("=", 1)
        if lhs.strip().lower() != "language":
            continue
        lines[index] = f"language = {detected}"
        updated = True
        break

    if not updated:
        lines.insert(end, f"language = {detected}")

    if dry_run:
        ok(f"[dry-run] config.ini UI language would be set to {detected}")
        return

    ini_path.parent.mkdir(parents=True, exist_ok=True)
    ini_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    ok(f"config.ini UI language set to {detected}")


def _windows_winget_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
    return Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"


def _windows_program_files_dirs() -> list[Path]:
    dirs: list[Path] = []
    for env_name, default in (
        ("ProgramFiles", r"C:\Program Files"),
        ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ):
        raw = os.environ.get(env_name, default)
        if raw:
            dirs.append(Path(raw))
    return _dedupe_paths(dirs)


def _is_windows_mediainfo_cli_path(path: str) -> bool:
    raw = (path or "").strip()
    if not raw:
        return False

    lower = raw.lower()
    if lower in ("mediainfo", "mediainfo.exe"):
        return True
    if lower == "mediaarea.mediainfo":
        return True
    if "mediaarea.mediainfo.cli" in lower:
        return True
    if "\\mediainfo cli\\" in lower or "\\mediainfocli\\" in lower:
        return True
    return False


def _windows_default_tool_candidates(tool_name: str, prefix: Path) -> list[Path]:
    exe_names = WINDOWS_TOOL_FILENAMES.get(tool_name, (f"{tool_name}.exe",))
    candidates: list[Path] = []

    include_prefix_dirs = tool_name != "mediainfo"
    if include_prefix_dirs:
        user_tools = _windows_user_tools_dir()
        for directory in (
            prefix,
            prefix / "bin",
            user_tools,
            user_tools / "bin",
            Path(__file__).parent / "tools",
            Path(__file__).parent / "tools" / "bin",
        ):
            for exe_name in exe_names:
                candidates.append(directory / exe_name)

    winget_root = _windows_winget_root()
    if winget_root.exists():
        for pattern in WINDOWS_WINGET_PATTERNS.get(tool_name, ()):
            for package_dir in winget_root.glob(pattern):
                for exe_name in exe_names:
                    candidates.append(package_dir / exe_name)
                    candidates.extend(path for path in package_dir.rglob(exe_name))

    for base_dir in _windows_program_files_dirs():
        if tool_name in ("ffmpeg", "ffprobe"):
            for folder in ("ffmpeg", "FFmpeg"):
                for exe_name in exe_names:
                    candidates.append(base_dir / folder / "bin" / exe_name)
        elif tool_name == "mediainfo":
            for folder in ("MediaInfo", "MediaInfo CLI", "MediaInfoCLI"):
                for exe_name in exe_names:
                    candidates.append(base_dir / folder / exe_name)
        elif tool_name == "eac3to":
            for exe_name in exe_names:
                candidates.append(base_dir / "eac3to" / exe_name)

    return _dedupe_paths(candidates)


def _detect_windows_tool_path(tool_name: str, prefix: Path) -> str | None:
    resolved = shutil.which(tool_name)
    if resolved:
        return resolved

    for candidate in _windows_default_tool_candidates(tool_name, prefix):
        if candidate.is_file():
            return str(candidate)

    return None


def _non_windows_tool_candidates(tool_name: str, prefix: Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    if prefix is not None:
        candidates.extend([prefix / "bin" / tool_name, prefix / tool_name])

    repo_root = Path(__file__).parent
    candidates.extend(
        [
            repo_root / "tools" / tool_name,
            repo_root / "tools" / "bin" / tool_name,
            Path.home() / ".local" / "bin" / tool_name,
            Path("/usr/local/bin") / tool_name,
            Path("/usr/bin") / tool_name,
        ]
    )
    if OS == "Darwin":
        candidates.extend(
            [
                Path("/opt/homebrew/bin") / tool_name,
                Path("/opt/local/bin") / tool_name,
            ]
        )

    return _dedupe_paths(candidates)


def _detect_non_windows_tool_path(tool_name: str, prefix: Path | None = None) -> str | None:
    resolved = shutil.which(tool_name)
    if resolved:
        return resolved

    ini_value = _existing_ini_tool_values(_config_ini_path()).get(tool_name.lower(), "")
    if ini_value and Path(ini_value).is_file():
        return ini_value

    for candidate in _non_windows_tool_candidates(tool_name, prefix):
        if candidate.is_file():
            return str(candidate)

    return None


def _existing_ini_tool_values(path: Path) -> dict[str, str]:
    parser = configparser.ConfigParser(
        inline_comment_prefixes=("#",),
        default_section="DEFAULT",
    )
    if path.exists():
        parser.read(path, encoding="utf-8")
    if not parser.has_section("tools"):
        return {}
    return {
        key.strip().lower(): value.strip()
        for key, value in parser.items("tools")
        if value.strip()
    }


def autofill_windows_config_ini(prefix: Path, dry_run: bool, force: bool = False) -> None:
    """Détecte les outils Windows et remplit config.ini avec leurs chemins."""
    title("Step 4 — Windows config.ini tool paths")

    ini_path = _config_ini_path()
    existing_values = _existing_ini_tool_values(ini_path)
    detected: dict[str, str] = {}
    replace_keys: set[str] = set()

    for tool_name in WINDOWS_CONFIG_TOOL_ORDER:
        if force:
            replace_keys.add(tool_name)
            resolved = _detect_windows_tool_path(tool_name, prefix)
            if resolved:
                detected[tool_name] = resolved
            continue

        existing_value = existing_values.get(tool_name.lower(), "")
        if existing_value:
            if tool_name != "mediainfo":
                continue
            if _is_windows_mediainfo_cli_path(existing_value):
                continue
            replace_keys.add(tool_name)
        resolved = _detect_windows_tool_path(tool_name, prefix)
        if resolved:
            detected[tool_name] = resolved

    if not detected:
        warn("No default Windows tool path detected to write into config.ini")
        return

    for tool_name, path in detected.items():
        info(f"{tool_name:15s} → {path}")

    _update_ini_tools_section(
        ini_path,
        detected,
        dry_run=dry_run,
        replace_keys=replace_keys,
        prune_keys=set(WINDOWS_CONFIG_TOOL_ORDER) if force else None,
    )
    if dry_run:
        ok(f"[dry-run] config.ini would be updated at {ini_path}")
    else:
        ok(f"config.ini updated: {ini_path}")

# ---------------------------------------------------------------------------
# Step 1 — Python packages
# ---------------------------------------------------------------------------

def install_python_packages(dry_run: bool, force: bool = False) -> None:
    title("Step 1 — Python packages")

    if force:
        step(f"Installing: {', '.join(PYTHON_PACKAGES)}")
        run(
            [sys.executable, "-m", "pip", "install", "--upgrade"] + PYTHON_PACKAGES,
            dry_run=dry_run,
        )
        ok("Python packages installed")
        return

    missing = []
    for pkg in PYTHON_PACKAGES:
        module = pkg.split("[")[0].lower().replace("-", "_")
        try:
            __import__(module)
            ok(f"{pkg} already installed")
        except ImportError:
            missing.append(pkg)

    if not missing:
        ok("All Python packages already satisfied")
        return

    step(f"Installing: {', '.join(missing)}")
    run(
        [sys.executable, "-m", "pip", "install", "--upgrade"] + missing,
        dry_run=dry_run,
    )
    ok("Python packages installed")

# ---------------------------------------------------------------------------
# Step 2a — System packages: apt (Debian/Ubuntu)
# ---------------------------------------------------------------------------

def _apt_packages_to_install(force: bool = False) -> list[str]:
    already_seen: set[str] = set()
    to_install: list[str] = []
    for exe, meta in SYSTEM_TOOLS.items():
        pkg = meta["apt"]
        if pkg in already_seen:
            continue
        already_seen.add(pkg)
        if not force and shutil.which(exe):
            ok(f"{exe} already present")
        else:
            to_install.append(pkg)
    return to_install

def install_apt(dry_run: bool, force: bool = False) -> None:
    title("Step 2 — System packages (apt / Debian·Ubuntu)")
    sudo = sudo_prefix(dry_run)

    step("Refreshing package index")
    run(sudo + ["apt-get", "update", "-qq"], dry_run=dry_run)

    to_install = _apt_packages_to_install(force=force)
    if not to_install:
        ok("All system packages already installed")
        return

    pkgs = sorted(set(to_install))
    step(f"Installing: {' '.join(pkgs)}")
    run(sudo + ["apt-get", "install", "-y"] + pkgs, dry_run=dry_run)
    ok("System packages installed")

# ---------------------------------------------------------------------------
# Step 2b — System packages: dnf (Fedora/RHEL)
# ---------------------------------------------------------------------------

def _ensure_rpmfusion(dry_run: bool, sudo: list[str]) -> None:
    """Enable RPM Fusion Free repo if not already enabled (required for ffmpeg on Fedora)."""
    result = run(
        ["dnf", "repolist", "--enabled"],
        dry_run=False, check=False, capture=True,
    )
    if result and "rpmfusion-free" in (result.stdout or "").lower():
        ok("RPM Fusion Free already enabled")
        return

    warn("RPM Fusion Free not found — enabling it (required for ffmpeg)")
    ver_result = run(
        ["rpm", "-E", "%fedora"],
        dry_run=False, check=False, capture=True,
    )
    fedora_ver = (ver_result.stdout or "").strip() if ver_result else "39"
    if not fedora_ver.isdigit():
        fedora_ver = "39"

    rpmfusion_url = (
        f"https://mirrors.rpmfusion.org/free/fedora/"
        f"rpmfusion-free-release-{fedora_ver}.noarch.rpm"
    )
    step(f"Installing RPM Fusion Free (Fedora {fedora_ver})")
    run(sudo + ["dnf", "install", "-y", rpmfusion_url], dry_run=dry_run)
    ok("RPM Fusion Free enabled")

def install_dnf(dry_run: bool, force: bool = False) -> None:
    title("Step 2 — System packages (dnf / Fedora·RHEL)")

    already_seen: set[str] = set()
    to_install: list[str] = []
    for exe, meta in SYSTEM_TOOLS.items():
        pkg = meta["dnf"]
        if pkg in already_seen:
            continue
        already_seen.add(pkg)
        if not force and shutil.which(exe):
            ok(f"{exe} already present")
        else:
            to_install.append(pkg)

    if not to_install:
        ok("All system packages already installed")
        return

    pkgs = sorted(set(to_install))
    if _is_atomic_distro():
        _warn_atomic_install_skip("System packages", pkgs)
        return

    sudo = sudo_prefix(dry_run)
    if force or "ffmpeg" in to_install:
        _ensure_rpmfusion(dry_run, sudo)

    step(f"Installing: {' '.join(pkgs)}")
    run(sudo + ["dnf", "install", "-y", "--allowerasing"] + pkgs, dry_run=dry_run)
    ok("System packages installed")

# ---------------------------------------------------------------------------
# Step 2c — System packages: Homebrew (macOS)
# ---------------------------------------------------------------------------

def install_brew(dry_run: bool, force: bool = False) -> None:
    title("Step 2 — System packages (Homebrew / macOS)")

    brew = shutil.which("brew")
    if not brew:
        warn(
            "Homebrew not found — cannot auto-install system packages.\n"
            "   Install Homebrew from: https://brew.sh\n"
            "   Then re-run this script, or install tools manually (see links below)."
        )
        return

    already_seen: set[str] = set()
    to_install: list[str] = []
    for exe, meta in SYSTEM_TOOLS.items():
        pkg = meta.get("brew", "")
        if not pkg or pkg in already_seen:
            continue
        already_seen.add(pkg)
        if not force and shutil.which(exe):
            ok(f"{exe} already present")
        else:
            to_install.append(pkg)

    if not to_install:
        ok("All system packages already installed")
        return

    pkgs = sorted(set(to_install))
    step(f"Installing via brew: {' '.join(pkgs)}")
    run([brew, "install"] + pkgs, dry_run=dry_run)
    ok("System packages installed")

# ---------------------------------------------------------------------------
# Step 2d — System packages: winget (Windows)
# ---------------------------------------------------------------------------

def install_winget(dry_run: bool, force: bool = False) -> None:
    title("Step 2 — System packages (winget / Windows)")

    winget = shutil.which("winget")
    if not winget:
        warn(
            "winget not found — cannot auto-install system packages.\n"
            "   winget ships with Windows 10 (1809+) and Windows 11.\n"
            "   Install tools manually (see links below)."
        )
        return

    already_seen: set[str] = set()
    to_install: list[str] = []
    for exe, meta in SYSTEM_TOOLS.items():
        winget_id = meta.get("winget", "")
        if not winget_id or winget_id in already_seen:
            continue
        already_seen.add(winget_id)
        if winget_id == "buyukakyuz.install-nothing" and force:
            if shutil.which(exe):
                ok(f"{exe} already present")
            else:
                warn("Skipping pip force-reinstall on Windows (winget placeholder package)")
            continue
        if not force and shutil.which(exe):
            ok(f"{exe} already present")
        else:
            to_install.append(winget_id)

    if not to_install:
        ok("All system packages already installed")
        return

    for pkg_id in to_install:
        step(f"Installing via winget: {pkg_id}")
        result = run(
            [winget, "install", "--id", pkg_id, "--silent", "--accept-package-agreements",
             "--accept-source-agreements"],
            dry_run=dry_run,
            check=False,
        )
        if result is not None and result.returncode != 0:
            warn(f"winget returned exit {result.returncode} for {pkg_id}; continuing")
    ok("System packages installed")

# ---------------------------------------------------------------------------
# Step 3 — GitHub binary tools
# ---------------------------------------------------------------------------

def _github_latest_release(repo: str) -> dict:
    """Fetch latest release metadata from GitHub API."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={"User-Agent": "Muxiveo-setup/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        if OS == "Windows":
            try:
                payload = _windows_fetch_text(url)
                return json.loads(payload)
            except Exception as fallback_error:
                raise RuntimeError(
                    f"Cannot reach GitHub API for {repo}: {e} "
                    f"(Windows fallback failed: {fallback_error})"
                ) from fallback_error
        raise RuntimeError(f"Cannot reach GitHub API for {repo}: {e}") from e

def _download_file(url: str, dest: Path) -> None:
    """Download url → dest with a simple progress indicator."""
    info(f"Downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "Muxiveo-setup/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as out:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 65536
            while chunk := resp.read(chunk_size):
                out.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded * 100 // total
                    print(f"\r    {pct:3d}%  {downloaded//1024} KB / {total//1024} KB  ", end="", flush=True)
            print()
    except Exception as e:
        if OS == "Windows":
            try:
                _windows_download_file(url, dest)
                return
            except Exception as fallback_error:
                raise RuntimeError(
                    f"Download failed: {url}\n  {e}\n  "
                    f"Windows fallback failed: {fallback_error}"
                ) from fallback_error
        raise RuntimeError(f"Download failed: {url}\n  {e}") from e


def _windows_powershell() -> Optional[str]:
    """Return powershell executable path when available."""
    return shutil.which("powershell") or shutil.which("pwsh")


def _powershell_single_quote(value: str) -> str:
    """Return a PowerShell single-quoted literal."""
    return "'" + value.replace("'", "''") + "'"


def _windows_is_admin() -> bool:
    """Return True when the current Windows process already runs elevated."""
    if OS != "Windows":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:
        return False


def _windows_message_box(text: str, title_text: str, flags: int) -> int:
    """Display a native Windows message box."""
    try:
        return int(ctypes.windll.user32.MessageBoxW(None, text, title_text, flags))  # type: ignore[attr-defined]
    except Exception:
        return 0


def _windows_yes_no(text: str, title_text: str, default_no: bool = True) -> bool:
    """Ask a Yes/No question using a native Windows dialog when possible."""
    if OS == "Windows":
        MB_YESNO = 0x00000004
        MB_ICONQUESTION = 0x00000020
        MB_DEFBUTTON2 = 0x00000100
        flags = MB_YESNO | MB_ICONQUESTION
        if default_no:
            flags |= MB_DEFBUTTON2
        return _windows_message_box(text, title_text, flags) == 6  # IDYES

    try:
        answer = input(f"{text} [{'y/N' if default_no else 'Y/n'}] ").strip().lower()
    except (EOFError, RuntimeError):
        return False
    if not answer:
        return not default_no
    return answer in {"y", "yes", "o", "oui"}


def _windows_controlled_folder_access_state() -> int | None:
    """
    Return the current Controlled Folder Access mode, or None if unavailable.

    Common values:
      0 = disabled
      1 = enabled (blocking)
      2 = audit mode
    """
    ps = _windows_powershell()
    if not ps:
        return None

    script = "$p=Get-MpPreference; [int]$p.EnableControlledFolderAccess"
    result = subprocess.run(
        [ps, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
        **_windows_no_window_subprocess_kwargs(),
    )
    if result.returncode != 0:
        return None

    for line in reversed((result.stdout or "").splitlines()):
        value = line.strip()
        if value.lstrip("-").isdigit():
            return int(value)
    return None


def _windows_cfa_candidate_apps(prefix: Path) -> list[Path]:
    """
    Return the executables that should be allowlisted for protected folders.

    The actual output writes are performed by ffmpeg, so that executable
    matters more than the launcher itself.
    """
    candidates: list[Path] = []

    if getattr(sys, "frozen", False):
        app_exe = Path(sys.executable)
        if app_exe.is_file() and app_exe.suffix.lower() == ".exe":
            candidates.append(app_exe)

    ini_values = _existing_ini_tool_values(_config_ini_path())
    for tool_name in WINDOWS_CFA_WRITER_TOOLS:
        raw = ini_values.get(tool_name.lower(), "")
        if not raw:
            raw = _detect_windows_tool_path(tool_name, prefix) or ""
        if not raw:
            continue
        path = Path(raw)
        if path.is_file():
            candidates.append(path)

    return _dedupe_paths(candidates)


def _windows_apply_controlled_folder_access_allowlist(
    targets: list[Path],
) -> dict[str, Any]:
    """
    Add missing executables to the Controlled Folder Access allowlist.

    The helper script runs elevated when needed and writes a JSON result file so
    Python can report what happened after the UAC prompt closes.
    """
    ps = _windows_powershell()
    if not ps:
        raise RuntimeError("PowerShell introuvable sur Windows")

    targets = [p for p in _dedupe_paths(targets) if p.is_file()]
    if not targets:
        return {"status": "no_targets", "added": [], "skipped": []}

    script = r"""
param(
  [Parameter(Mandatory = $true)]
  [string]$ResultPath,

  [Parameter(Mandatory = $true)]
  [string]$TargetsPath
)

$ErrorActionPreference = 'Stop'

$result = [ordered]@{
  status  = 'unknown'
  added   = @()
  skipped = @()
  missing = @()
  message = ''
}

try {
  $pref  = Get-MpPreference
  $state = [int]$pref.EnableControlledFolderAccess
  if ($state -eq 0) {
    $result.status = 'disabled'
  }
  else {
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    $targets = @()
    foreach ($raw in @((Get-Content -LiteralPath $TargetsPath -Raw | ConvertFrom-Json))) {
      if (-not $raw) { continue }
      try {
        $full = [System.IO.Path]::GetFullPath($raw)
      }
      catch {
        continue
      }
      if ((Test-Path -LiteralPath $full -PathType Leaf) -and $seen.Add($full)) {
        $targets += $full
      }
    }

    $current = @($pref.ControlledFolderAccessAllowedApplications)
    foreach ($target in $targets) {
      $already = $false
      foreach ($entry in $current) {
        if (-not $entry) { continue }
        try {
          $entryFull = [System.IO.Path]::GetFullPath($entry)
        }
        catch {
          $entryFull = $entry
        }
        if ($entryFull.Equals($target, [System.StringComparison]::OrdinalIgnoreCase)) {
          $already = $true
          break
        }
      }

      if ($already) {
        $result.skipped += $target
        continue
      }

      Add-MpPreference -ControlledFolderAccessAllowedApplications $target
      $result.added += $target
    }

    if ($result.added.Count -gt 0) {
      $result.status = 'updated'
      $result.message = 'Applications ajoutées à l''allowlist.'
    }
    else {
      $result.status = 'already_allowed'
      $result.message = 'Applications déjà présentes dans l''allowlist.'
    }
  }
}
catch {
  $result.status = 'error'
  $result.message = $_.Exception.Message
}

$result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ResultPath -Encoding UTF8
if ($result.status -eq 'error') { exit 1 }
exit 0
"""

    with tempfile.TemporaryDirectory(prefix="Muxiveo_cfa_") as tmp_dir:
        tmp = Path(tmp_dir)
        script_path = tmp / "controlled_folder_access.ps1"
        result_path = tmp / "controlled_folder_access_result.json"
        targets_path = tmp / "controlled_folder_access_targets.json"
        script_path.write_text(script.strip() + "\n", encoding="utf-8")
        targets_path.write_text(
            json.dumps([str(path) for path in targets], ensure_ascii=False),
            encoding="utf-8",
        )

        cmd = [
            ps,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
            "-ResultPath",
            str(result_path),
            "-TargetsPath",
            str(targets_path),
        ]
        if _windows_is_admin():
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                **_windows_no_window_subprocess_kwargs(),
            )
        else:
            launcher = (
                f"$proc = Start-Process -FilePath {_powershell_single_quote(ps)} "
                "-ArgumentList @("
                "'-NoProfile',"
                "'-ExecutionPolicy','Bypass',"
                f"'-File',{_powershell_single_quote(str(script_path))},"
                f"'-ResultPath',{_powershell_single_quote(str(result_path))},"
                f"'-TargetsPath',{_powershell_single_quote(str(targets_path))}"
                ") "
                "-Verb RunAs -Wait -PassThru; "
                "exit $proc.ExitCode"
            )
            result = subprocess.run(
                [ps, "-NoProfile", "-NonInteractive", "-Command", launcher],
                capture_output=True,
                text=True,
                check=False,
                **_windows_no_window_subprocess_kwargs(),
            )

        payload: dict[str, object]
        if result_path.exists():
            payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
        else:
            stderr = (result.stderr or "").strip()
            payload = {
                "status": "cancelled" if result.returncode != 0 else "unknown",
                "added": [],
                "skipped": [],
                "message": stderr or (
                    "Windows Security n'a pas renvoyé de résultat exploitable. "
                    "L'élévation a peut-être échoué après validation ou le script "
                    "élevé n'a pas pu s'exécuter correctement."
                ),
            }

        if result.returncode != 0 and payload.get("status") not in {"error", "cancelled"}:
            payload["status"] = "error"
            payload["message"] = payload.get("message") or f"PowerShell exited with {result.returncode}"
        return payload


def offer_windows_controlled_folder_access_setup(
    prefix: Path,
    dry_run: bool,
    force: bool = False,
) -> dict[str, object]:
    """
    Offer to allowlist Muxiveo writer tools in Windows Security.

    This is intentionally opt-in because it modifies the Defender allowlist.
    """
    if OS != "Windows":
        return {"status": "unsupported", "added": [], "skipped": [], "message": ""}

    title("Step 4b — Windows Security (Controlled Folder Access)")

    state = _windows_controlled_folder_access_state()
    if state is None:
        warn("Unable to query Controlled Folder Access state from Windows Security")
        return {"status": "unavailable", "added": [], "skipped": [], "message": ""}
    if state == 0:
        ok("Controlled Folder Access disabled — no allowlist change needed")
        return {"status": "disabled", "added": [], "skipped": [], "message": ""}
    if state == 2:
        info("Controlled Folder Access is in audit mode")
    else:
        info("Controlled Folder Access is enabled")

    targets = _windows_cfa_candidate_apps(prefix)
    if not targets:
        warn("No Windows writer executable found to propose for the allowlist")
        return {"status": "no_targets", "added": [], "skipped": [], "message": ""}

    for target in targets:
        info(f"Allowlist candidate: {target}")

    if dry_run:
        ok("[dry-run] Controlled Folder Access prompt would be shown if needed")
        return {"status": "dry_run", "added": [], "skipped": [], "message": ""}

    if not force:
        message = (
            "Windows Security Controlled Folder Access is active.\n\n"
            "Muxiveo can ask Windows Security to allow its executables "
            "to write into the protected user libraries such as Videos, "
            "Documents, Pictures, and similar folders.\n\n"
            "Without this exception, saving directly into those folders "
            "may be blocked by Windows, even if the folders exist.\n\n"
            "This allowlist can include Muxiveo itself and the writer tools "
            "it uses, such as ffmpeg.\n\n"
            "An administrator approval prompt may appear.\n\n"
            "Add missing applications now?"
        )
        if not _windows_yes_no(message, "Muxiveo setup", default_no=True):
            info("Controlled Folder Access allowlist step skipped by user")
            return {"status": "skipped", "added": [], "skipped": [], "message": ""}

    result = _windows_apply_controlled_folder_access_allowlist(targets)
    status = str(result.get("status") or "unknown")
    added = [str(item) for item in result.get("added", [])]
    skipped = [str(item) for item in result.get("skipped", [])]
    message = str(result.get("message") or "").strip()

    if status == "updated":
        ok("Controlled Folder Access allowlist updated")
        for item in added:
            info(f"Allowed: {item}")
        if skipped:
            info(f"Already allowed: {len(skipped)} application(s)")
        return result
    if status == "already_allowed":
        ok("Controlled Folder Access allowlist already up to date")
        return result
    if status == "disabled":
        ok("Controlled Folder Access disabled — no allowlist change needed")
        return result
    if status == "cancelled":
        warn("Controlled Folder Access update cancelled or refused")
        if message:
            warn(message)
        warn(
            "Without the Windows Security exception, direct saves to protected "
            "libraries such as Videos or Documents may remain blocked."
        )
        return result

    warn("Controlled Folder Access allowlist update failed")
    if message:
        warn(message)
    return result


def _windows_fetch_text(url: str) -> str:
    """Fetch URL text payload on Windows without relying on urllib+ssl."""
    ps = _windows_powershell()
    if ps:
        script = (
            "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; "
            "$ProgressPreference='SilentlyContinue'; "
            "$h=@{'User-Agent'='Muxiveo-setup/1.0'}; "
            "(Invoke-WebRequest -Uri $env:MR_URL -Headers $h -UseBasicParsing).Content"
        )
        env = os.environ.copy()
        env["MR_URL"] = url
        result = subprocess.run(
            [ps, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            **_windows_no_window_subprocess_kwargs(),
        )
        if result.returncode == 0:
            return result.stdout
        stderr = (result.stderr or "").strip()
        raise RuntimeError(stderr or f"PowerShell exited with {result.returncode}")

    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise RuntimeError("No PowerShell/curl fallback available on Windows")
    result = subprocess.run(
        [curl, "-fsSL", "-H", "User-Agent: Muxiveo-setup/1.0", url],
        capture_output=True,
        text=True,
        check=False,
        **_windows_no_window_subprocess_kwargs(),
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(stderr or f"curl exited with {result.returncode}")
    return result.stdout


def _windows_download_file(url: str, dest: Path) -> None:
    """Download URL to file on Windows without relying on urllib+ssl."""
    ps = _windows_powershell()
    if ps:
        script = (
            "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; "
            "$ProgressPreference='SilentlyContinue'; "
            "$h=@{'User-Agent'='Muxiveo-setup/1.0'}; "
            "Invoke-WebRequest -Uri $env:MR_URL -Headers $h -OutFile $env:MR_DEST -UseBasicParsing"
        )
        env = os.environ.copy()
        env["MR_URL"] = url
        env["MR_DEST"] = str(dest)
        result = subprocess.run(
            [ps, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            **_windows_no_window_subprocess_kwargs(),
        )
        if result.returncode == 0:
            return
        stderr = (result.stderr or "").strip()
        raise RuntimeError(stderr or f"PowerShell exited with {result.returncode}")

    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise RuntimeError("No PowerShell/curl fallback available on Windows")
    result = subprocess.run(
        [curl, "-fL", "-H", "User-Agent: Muxiveo-setup/1.0", "-o", str(dest), url],
        capture_output=True,
        text=True,
        check=False,
        **_windows_no_window_subprocess_kwargs(),
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(stderr or f"curl exited with {result.returncode}")

def _find_asset(release: dict, suffix: str) -> Optional[str]:
    """Return the browser_download_url of the first asset whose name ends with suffix."""
    for asset in release.get("assets", []):
        if asset["name"].endswith(suffix):
            return asset["browser_download_url"]
    return None

def _extract_binary(archive_path: Path, binary_name: str, fmt: str, dest_dir: Path) -> Path:
    """Extract binary_name from archive (tar.gz / zip / deb / rpm / 7z) into dest_dir."""
    if fmt == "tar.gz":
        with tarfile.open(archive_path, "r:gz") as tar:
            member = next(
                (m for m in tar.getmembers()
                 if Path(m.name).name == binary_name and m.isfile()),
                None,
            )
            if not member:
                raise RuntimeError(f"Binary '{binary_name}' not found inside archive")
            member.name = binary_name  # flatten path
            tar.extract(member, path=dest_dir)
    elif fmt == "zip":
        with zipfile.ZipFile(archive_path) as zf:
            entry = next(
                (e for e in zf.namelist() if Path(e).name == binary_name),
                None,
            )
            if not entry:
                raise RuntimeError(f"Binary '{binary_name}' not found inside archive")
            data = zf.read(entry)
            out = dest_dir / binary_name
            out.write_bytes(data)
    elif fmt == "deb":
        _extract_deb_binary(archive_path, binary_name, dest_dir)
    elif fmt == "rpm":
        _extract_rpm_binary(archive_path, binary_name, dest_dir)
    elif fmt == "7z":
        _extract_7z_binary(archive_path, binary_name, dest_dir)
    else:
        raise RuntimeError(f"Unknown archive format: {fmt}")

    extracted = dest_dir / binary_name
    extracted.chmod(extracted.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return extracted


def _extract_deb_binary(archive_path: Path, binary_name: str, dest_dir: Path) -> None:
    """Extrait ``binary_name`` d'un paquet Debian (.deb).

    Préfère ``dpkg-deb -x`` quand disponible (gère les variantes de format),
    sinon fallback ``ar x`` + extraction du data.tar.* via tarfile.
    """
    extract_dir = dest_dir / "_deb_extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)
    dpkg_deb = shutil.which("dpkg-deb")
    if dpkg_deb:
        subprocess.run(
            [dpkg_deb, "-x", str(archive_path), str(extract_dir)],
            check=True,
        )
    else:
        # Fallback : ar x archive.deb → debian-binary, control.tar.*, data.tar.*
        # On extrait juste le data.tar.* puis on l'untare.
        ar = shutil.which("ar")
        if not ar:
            raise RuntimeError(
                "Neither 'dpkg-deb' nor 'ar' available — cannot extract .deb. "
                "Install 'dpkg' (Debian) or 'binutils' (others)."
            )
        cwd_backup = os.getcwd()
        try:
            os.chdir(extract_dir)
            subprocess.run([ar, "x", str(archive_path)], check=True)
            data_tarball = next(
                (p for p in extract_dir.glob("data.tar.*")), None,
            )
            if not data_tarball:
                raise RuntimeError("No data.tar.* found inside .deb archive")
            with tarfile.open(data_tarball, "r:*") as tar:
                tar.extractall(path=extract_dir)
        finally:
            os.chdir(cwd_backup)
    # Localise binary_name dans l'arborescence extraite (typiquement usr/bin/).
    candidates = list(extract_dir.rglob(binary_name))
    if not candidates:
        # Tolérant à la casse (NVEncC vs nvencc).
        candidates = [p for p in extract_dir.rglob("*")
                      if p.is_file() and p.name.lower() == binary_name.lower()]
    if not candidates:
        raise RuntimeError(f"Binary '{binary_name}' not found inside .deb archive")
    shutil.copy2(candidates[0], dest_dir / binary_name)


def _extract_rpm_binary(archive_path: Path, binary_name: str, dest_dir: Path) -> None:
    """Extrait ``binary_name`` d'un paquet RPM via ``rpm2cpio | cpio -idm``."""
    extract_dir = dest_dir / "_rpm_extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)
    rpm2cpio = shutil.which("rpm2cpio")
    cpio = shutil.which("cpio")
    if not rpm2cpio or not cpio:
        raise RuntimeError(
            "rpm2cpio and cpio required to extract .rpm without dnf/rpm install. "
            "Install 'rpm' (Fedora) or 'rpm2cpio' (Debian/Ubuntu)."
        )
    cwd_backup = os.getcwd()
    try:
        os.chdir(extract_dir)
        with open(archive_path, "rb") as fp:
            r = subprocess.Popen([rpm2cpio], stdin=fp, stdout=subprocess.PIPE)
            c = subprocess.Popen([cpio, "-idm", "--quiet"], stdin=r.stdout)
            if r.stdout:
                r.stdout.close()
            c.communicate()
            r.wait()
            if r.returncode != 0 or c.returncode != 0:
                raise RuntimeError("rpm2cpio | cpio extraction failed")
    finally:
        os.chdir(cwd_backup)
    candidates = list(extract_dir.rglob(binary_name))
    if not candidates:
        candidates = [p for p in extract_dir.rglob("*")
                      if p.is_file() and p.name.lower() == binary_name.lower()]
    if not candidates:
        raise RuntimeError(f"Binary '{binary_name}' not found inside .rpm archive")
    shutil.copy2(candidates[0], dest_dir / binary_name)


def _extract_7z_binary(archive_path: Path, binary_name: str, dest_dir: Path) -> None:
    """Extrait ``binary_name`` d'une archive .7z via ``py7zr`` (Python pur)."""
    try:
        import py7zr  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "py7zr required to extract .7z archives. "
            "Install via 'pip install py7zr' (auto-installé par setup.py si requirements.txt à jour)."
        ) from exc
    extract_dir = dest_dir / "_7z_extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)
    with py7zr.SevenZipFile(archive_path, mode="r") as z:
        z.extractall(path=extract_dir)
    candidates = list(extract_dir.rglob(binary_name))
    if not candidates:
        candidates = [p for p in extract_dir.rglob("*")
                      if p.is_file() and p.name.lower() == binary_name.lower()]
    if not candidates:
        raise RuntimeError(f"Binary '{binary_name}' not found inside .7z archive")
    shutil.copy2(candidates[0], dest_dir / binary_name)


def _is_atomic_distro() -> bool:
    """Détecte une distro atomique/immuable (Silverblue, Kinoite, Bazzite, etc.).

    Sur ces systèmes, ``dnf install`` est désactivé au profit de ``rpm-ostree`` —
    qui exige un reboot pour appliquer les changements. Au lieu d'installer,
    on guide l'utilisateur vers distrobox ou l'AppImage allinc.
    """
    if Path("/run/ostree-booted").exists():
        return True
    if shutil.which("rpm-ostree"):
        # Vérification supplémentaire : rpm-ostree status réussit-il ?
        try:
            r = subprocess.run(
                ["rpm-ostree", "status"], capture_output=True,
                check=False, timeout=5,
            )
            if r.returncode == 0:
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
    return False


def _warn_atomic_install_skip(subject: str, packages: list[str] | None = None) -> None:
    """Explique pourquoi setup.py skip les installs système sur distro atomique."""
    pkg_hint = ""
    if packages:
        pkg_hint = f"\n  → Paquets concernés : {' '.join(packages)}"
    warn(
        f"{subject}: distribution atomique détectée (rpm-ostree). "
        f"L'installation automatique sur le système de base n'est pas prise en charge par setup.py."
        f"{pkg_hint}\n"
        f"  → Recommandé : lancer setup.py dans une distrobox Fedora/Ubuntu :\n"
        f"      distrobox enter <box> -- python3 setup.py\n"
        f"    ou utiliser l'AppImage allinc.\n"
        f"  → Alternative manuelle : layer les paquets avec rpm-ostree puis redémarrer."
    )


def _try_native_pkg_install(
    archive_path: Path, exe: str, fmt: str, *, dry_run: bool,
) -> bool:
    """Tente l'installation native via apt (.deb) ou dnf (.rpm).

    L'avantage : le package manager résout les dépendances runtime
    (libcuda, libavformat, libavcodec, …) que l'extraction binaire ignorerait.

    Sur les distros atomiques (Silverblue/Kinoite/Bazzite), `dnf install` n'est
    pas applicable au système de base — on retourne False pour que le caller
    skip avec un guidage utilisateur (distrobox ou AppImage allinc).

    Retourne True en cas de succès, False sinon (caller fera l'extraction pure).
    """
    if _is_atomic_distro():
        _warn_atomic_install_skip(exe)
        return False

    sudo = sudo_prefix(dry_run) if not is_root() else []
    if fmt == "deb":
        apt = shutil.which("apt") or shutil.which("apt-get")
        if not apt:
            return False
        info(f"Installing {exe} via apt (native deps included)")
        if dry_run:
            info(f"[dry-run] Would run: apt install -y {archive_path}")
            return True
        rc = subprocess.run(
            sudo + [apt, "install", "-y", str(archive_path)],
            check=False,
        ).returncode
        return rc == 0
    if fmt == "rpm":
        dnf = shutil.which("dnf") or shutil.which("yum")
        if not dnf:
            return False
        info(f"Installing {exe} via dnf (native deps included)")
        if dry_run:
            info(f"[dry-run] Would run: dnf install -y {archive_path}")
            return True
        rc = subprocess.run(
            sudo + [dnf, "install", "-y", str(archive_path)],
            check=False,
        ).returncode
        return rc == 0
    return False


def _check_nvenc_available() -> bool:
    """Détection lightweight du GPU NVIDIA pour gate l'install NVEncC.

    Retourne True si nvidia-smi répond ou /dev/nvidia0 existe (Linux).
    Sur Windows, on ne peut pas tester via /dev — on regarde nvidia-smi seul.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True, check=False, timeout=5,
        )
        if result.returncode == 0 and (result.stdout or b"").strip():
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    if OS == "Linux":
        return Path("/dev/nvidia0").exists()
    return False

def install_github_tools(prefix: Path, dry_run: bool, force: bool = False) -> None:
    title("Step 3 — GitHub binary tools (dovi_tool, hdr10plus_tool)")

    arch = _arch_key()

    # On Windows: binaries go directly into prefix (Muxiveo/tools/).
    # On Linux/macOS: binaries go into prefix/bin/ (/usr/local/bin/).
    if OS == "Windows":
        bin_dir = prefix
    else:
        bin_dir = prefix / "bin"

    # On Windows we install to a user-writable directory (no sudo needed).
    use_sudo = OS != "Windows"
    sudo = sudo_prefix(dry_run) if use_sudo else []
    ini_path = _config_ini_path()

    path_reminder_shown = False
    detected_tool_paths: dict[str, str] = {}

    for exe, meta in GITHUB_TOOLS.items():
        # Skip silencieux si la plateforme n'est pas dans la liste blanche du tool.
        platforms = meta.get("platforms")
        if platforms and OS not in platforms:
            info(f"{exe} not supported on {OS} — skipping.")
            continue

        # Gate optionnel (ex: nvencc requiert un GPU NVIDIA détecté).
        gate = meta.get("gate")
        if gate == "nvenc_available" and not _check_nvenc_available():
            info(f"{exe}: NVENC not detected on this machine — skipping.")
            continue

        binary_name = meta["binary_name"].get(OS, meta["binary_name"].get("Linux"))
        dest = bin_dir / binary_name

        if not force and dest.is_file():
            ok(f"{exe} already present ({dest})")
            detected_tool_paths[exe] = str(dest)
            continue

        # Recherche tolérante à la casse : .rpm Fedora installe `nvencc` (minuscules)
        # alors que .deb Debian installe `NVEncC` (PascalCase). On accepte les deux.
        existing = shutil.which(exe) or shutil.which(exe.lower()) or shutil.which(exe.capitalize())
        if not force and existing:
            ok(f"{exe} already present ({existing})")
            detected_tool_paths[exe] = existing
            continue

        pattern_key = (OS, arch)
        pattern = meta["asset_patterns"].get(pattern_key)
        if not pattern:
            warn(
                f"No pre-built binary for {exe} on {OS}/{arch}. "
                f"Build from source: https://github.com/{meta['repo']}"
            )
            continue

        step(f"Installing {exe}  ({meta['desc']})")
        info(f"Fetching latest release from github.com/{meta['repo']}")

        if dry_run:
            info(f"[dry-run] Would download and install {exe} to {dest}")
            detected_tool_paths[exe] = str(dest)
            ok(f"{exe} installed → {dest}")
            continue

        release = _github_latest_release(meta["repo"])
        tag = release.get("tag_name", "?")
        info(f"Latest release: {tag}")

        # Sélection asset : suffix principal puis alt_suffix (ex: .deb → .rpm).
        download_url = _find_asset(release, pattern["suffix"])
        chosen_fmt = pattern["fmt"]
        if not download_url and pattern.get("alt_suffix"):
            download_url = _find_asset(release, pattern["alt_suffix"])
            if download_url:
                chosen_fmt = pattern.get("alt_fmt", chosen_fmt)
                info(f"Using alternate asset format ({chosen_fmt})")
        if not download_url:
            available = [a["name"] for a in release.get("assets", [])]
            raise RuntimeError(
                f"No asset ending with '{pattern['suffix']}' found in release {tag}.\n"
                f"Available: {available}\n"
                f"Download manually from: https://github.com/{meta['repo']}/releases"
            )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            archive_path = tmp_path / f"{exe}_archive.{chosen_fmt}"
            _download_file(download_url, archive_path)

            # Tentative d'installation native via package manager (Linux .deb/.rpm) :
            # gère automatiquement les dépendances (libcuda, libavformat, etc.).
            installed_natively = False
            if OS == "Linux" and chosen_fmt in ("deb", "rpm"):
                installed_natively = _try_native_pkg_install(
                    archive_path, exe, chosen_fmt, dry_run=dry_run,
                )
                if installed_natively:
                    # Le pkg manager a installé le binaire — on récupère son chemin.
                    found = shutil.which(binary_name) or shutil.which(binary_name.lower()) \
                            or shutil.which(binary_name.capitalize())
                    if found:
                        detected_tool_paths[exe] = found
                        ok(f"{exe} installed natively → {found}")
                        continue
                    warn(f"{exe}: native install reported success but binary not found on PATH.")

            # Fallback : extraction binaire pure (pas de gestion des deps libs).
            try:
                extracted = _extract_binary(archive_path, binary_name, chosen_fmt, tmp_path)
            except RuntimeError as exc:
                warn(f"{exe}: extraction failed ({exc}). Skipping.")
                continue

            info(f"Installing to {dest}")

            if use_sudo and not is_root():
                run(sudo + ["mkdir", "-p", str(bin_dir)])
                run(sudo + ["install", "-m", "755", str(extracted), str(dest)])
            else:
                bin_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(extracted, dest)
                dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        detected_tool_paths[exe] = str(dest)
        ok(f"{exe} installed → {dest}")

        # Remind Windows users to add the directory to PATH once.
        # Non requis quand on installe dans le dossier utilisateur (%LOCALAPPDATA%)
        # car config.ini reçoit les chemins absolus détectés.
        user_tools = _windows_user_tools_dir()
        installs_to_user_dir = bin_dir == user_tools or bin_dir == (user_tools / "bin")
        if OS == "Windows" and not path_reminder_shown and not installs_to_user_dir:
            path_reminder_shown = True
            warn(
                f"Add the following directory to your PATH so Windows can find these tools:\n"
                f"    {bin_dir}\n"
                f"  (Settings → System → About → Advanced system settings → Environment Variables)"
            )

    if detected_tool_paths:
        _update_ini_tools_section(ini_path, detected_tool_paths, dry_run=dry_run)
        if dry_run:
            ok(f"[dry-run] config.ini would be updated at {ini_path}")
        else:
            ok(f"config.ini updated: {ini_path}")

# ---------------------------------------------------------------------------
# Tool presence check (fallback / final verification)
# ---------------------------------------------------------------------------

def check_tools_presence(prefix: Path | None = None) -> None:
    title("External tool check")

    all_tools = list(SYSTEM_TOOLS.keys()) + list(GITHUB_TOOLS.keys())
    seen: set[str] = set()
    missing: list[tuple[str, str]] = []

    for exe in all_tools:
        if exe in seen:
            continue
        seen.add(exe)

        meta = SYSTEM_TOOLS.get(exe, GITHUB_TOOLS.get(exe, {}))
        if meta.get("path_check", True) is False:
            continue

        path = shutil.which(exe)
        if not path and OS == "Windows" and prefix is not None and exe in WINDOWS_TOOL_FILENAMES:
            path = _detect_windows_tool_path(exe, prefix)
        if not path and OS != "Windows":
            path = _detect_non_windows_tool_path(exe, prefix)
        if path:
            ok(f"{exe:20s}  →  {path}")
        else:
            desc = meta.get("desc", "")
            missing.append((exe, desc))

    if missing:
        print()
        warn("The following tools were NOT found in PATH:")
        print()

        install_hints = {
            "ffmpeg":         "https://ffmpeg.org/download.html",
            "ffprobe":        "https://ffmpeg.org/download.html  (ships with ffmpeg)",
            "mediainfo":      "https://mediaarea.net/en/MediaInfo/Download",
            "dovi_tool":      "https://github.com/quietvoid/dovi_tool/releases",
            "hdr10plus_tool": "https://github.com/quietvoid/hdr10plus_tool/releases",
        }

        max_len = max(len(e) for e, _ in missing)
        for exe, desc in missing:
            hint = install_hints.get(exe, "Install manually")
            print(_c("33", f"    {exe:{max_len}}  —  {desc}"))
            print(_c("2",  f"    {'':>{max_len}}     Download: {hint}"))
            print()

        print(_c("36",
            "  The application will start without these tools, but the\n"
            "  corresponding features will be unavailable or will fail.\n"
        ))
    else:
        ok("All external tools found in PATH")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    default_prefix = str(_default_prefix())
    module_doc = __doc__ or ""
    description = module_doc.replace(
        "See platform-specific command shown by --help output.",
        f"{PYTHON_CMD} setup.py [--no-github] [--prefix PATH] [--dry-run] [--force]",
    )
    p = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--no-github",
        action="store_true",
        help="Skip downloading dovi_tool / hdr10plus_tool from GitHub",
    )
    p.add_argument(
        "--prefix",
        default=default_prefix,
        metavar="PATH",
        help=f"Install prefix for GitHub binaries (default: {default_prefix})",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without executing anything",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Retry installations and regenerate Windows tool paths",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    dry_run = args.dry_run
    prefix  = Path(args.prefix)
    force   = args.force

    print(_c("1;34", """
╔══════════════════════════════════════════╗
║          Muxiveo — Setup             ║
╚══════════════════════════════════════════╝
"""))

    info(f"Python  : {sys.version.split()[0]}")
    info(f"Platform: {OS} / {platform.machine()}")
    if force:
        warn("FORCE mode - installations will be retried and Windows tool paths regenerated")
    if dry_run:
        warn("DRY-RUN mode — no changes will be made")

    if OS == "Windows":
        try:
            verify_windows_frozen_python_runtime()
        except Exception as e:
            error(f"Windows Python runtime check failed: {e}")
            sys.exit(1)

    # ── Platform-specific ─────────────────────────────────────────────────
    if OS == "Linux":
        distro = detect_linux_distro()
        info(f"Distro family: {distro}")

        if distro == "debian":
            try:
                install_apt(dry_run, force=force)
            except Exception as e:
                error(f"apt install failed: {e}")
                sys.exit(1)

        elif distro == "fedora":
            try:
                install_dnf(dry_run, force=force)
            except Exception as e:
                error(f"dnf install failed: {e}")
                sys.exit(1)

        else:
            warn(
                "Unrecognised Linux distribution — cannot auto-install system packages.\n"
                "   Install manually: ffmpeg  mediainfo"
            )

        if not args.no_github:
            try:
                install_github_tools(prefix, dry_run, force=force)
            except Exception as e:
                error(f"GitHub tool installation failed: {e}")
                warn("Install dovi_tool and hdr10plus_tool manually:")
                warn("  → https://github.com/quietvoid/dovi_tool/releases")
                warn("  → https://github.com/quietvoid/hdr10plus_tool/releases")
        else:
            info("Skipping GitHub tools (--no-github)")

        title("Final tool verification")
        check_tools_presence()

    elif OS == "Darwin":
        try:
            install_brew(dry_run, force=force)
        except Exception as e:
            error(f"Homebrew install failed: {e}")

        if not args.no_github:
            try:
                install_github_tools(prefix, dry_run, force=force)
            except Exception as e:
                error(f"GitHub tool installation failed: {e}")
                warn("Install dovi_tool and hdr10plus_tool manually:")
                warn("  → https://github.com/quietvoid/dovi_tool/releases")
                warn("  → https://github.com/quietvoid/hdr10plus_tool/releases")
        else:
            info("Skipping GitHub tools (--no-github)")

        title("Final tool verification")
        check_tools_presence()

    elif OS == "Windows":
        try:
            install_winget(dry_run, force=force)
        except Exception as e:
            error(f"winget install failed: {e}")

        if not args.no_github:
            try:
                install_github_tools(prefix, dry_run, force=force)
            except Exception as e:
                error(f"GitHub tool installation failed: {e}")
                warn("Install dovi_tool and hdr10plus_tool manually:")
                warn("  → https://github.com/quietvoid/dovi_tool/releases")
                warn("  → https://github.com/quietvoid/hdr10plus_tool/releases")
        else:
            info("Skipping GitHub tools (--no-github)")

        try:
            autofill_windows_config_ini(prefix, dry_run, force=force)
        except Exception as e:
            error(f"config.ini auto-fill failed: {e}")

        title("Final tool verification")
        check_tools_presence(prefix)

        try:
            offer_windows_controlled_folder_access_setup(
                prefix, dry_run, force=force
            )
        except Exception as e:
            error(f"Windows Security allowlist setup failed: {e}")

    else:
        warn(f"Unknown platform '{OS}' — skipping system package installation")
        check_tools_presence()

    try:
        initialize_config_ini_language(dry_run, force=force)
    except Exception as e:
        error(f"config.ini language initialisation failed: {e}")

    # ── Python packages (all platforms) ──────────────────────────────────
    try:
        install_python_packages(dry_run, force=force)
    except Exception as e:
        error(f"Python packages: {e}")
        sys.exit(1)

    # ── Done ──────────────────────────────────────────────────────────────
    title("Setup complete")
    ok("Run the application with:")
    print(_c("1;37", f"\n    {PYTHON_CMD} main.py\n"))


if __name__ == "__main__":
    main()
