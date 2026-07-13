"""
core/config.py — Configuration centralisée de l'application.

Priorité de résolution :
    1. config.ini  (fichier statique à côté de main.py)
    2. Fichier de configuration persistant (QSettings)
    3. Valeur par défaut
"""

from __future__ import annotations

import configparser
import json
import locale
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from types import ModuleType

from PySide6.QtCore import QSettings, QStandardPaths

from core.lang_tags import Rfc5646LanguageTags
from core.subprocess_utils import subprocess_text_kwargs
from core.version import (
    APP_CONFIG_DIR_NAME,
    APP_ENV_PREFIX,
    APP_TEMP_WORK_DIR_NAME,
)
from core.workflows.common.ffmpeg_runtime import (
    default_ffmpeg_thread_count as _default_ffmpeg_thread_count,
    normalize_ffmpeg_thread_count as _normalize_ffmpeg_thread_count,
    normalize_max_parallel_video_encodes as _normalize_max_parallel_video_encodes,
)
from core.workdir import (
    clear_work_dir as clear_work_dir_contents,
    prepare_process_work_dir,
    work_dir_entries as list_work_dir_entries,
    work_dir_has_entries,
)


# ---------------------------------------------------------------------------
# Chemin du fichier config.ini
# ---------------------------------------------------------------------------

# Linux/macOS : config.ini dans le dossier XDG user (~/.config/muxiveo).
# Windows frozen : config.ini dans %APPDATA%\\muxiveo.
# Windows dev : config.ini à la racine du projet.
def _windows_config_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / APP_CONFIG_DIR_NAME
    return Path.home() / "AppData" / "Roaming" / APP_CONFIG_DIR_NAME


def _resolve_ini_path() -> Path:
    """
    Résout le chemin de config.ini selon la plateforme et le contexte :
    - Linux / macOS  → ~/.config/muxiveo/config.ini  (XDG, dev ET frozen)
    - Windows frozen → %APPDATA%\\muxiveo\\config.ini
    - Windows dev    → racine du projet (parent de core/)

    Sur Linux/macOS, on utilise toujours le chemin XDG — y compris en mode
    développement — car setup.py y écrit les chemins absolus des outils détectés.
    """
    explicit_config_home = os.environ.get("MUXIVEO_CONFIG_HOME")
    if explicit_config_home:
        return Path(explicit_config_home).expanduser() / "config.ini"
    if sys.platform != "win32":
        xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return xdg / APP_CONFIG_DIR_NAME / "config.ini"
    # Windows
    if getattr(sys, "frozen", False):
        return _windows_config_dir() / "config.ini"
    return Path(__file__).parent.parent / "config.ini"

_INI_PATH = _resolve_ini_path()
_MISSING = object()

_WINDOWS_TOOL_FILENAMES: dict[str, tuple[str, ...]] = {
    "ffmpeg": ("ffmpeg.exe",),
    "ffprobe": ("ffprobe.exe",),
    "mediainfo": ("MediaInfo.exe", "mediainfo.exe"),
    "dovi_tool": ("dovi_tool.exe",),
    "hdr10plus_tool": ("hdr10plus_tool.exe",),
    "eac3to": ("eac3to.exe",),
    "nvencc": ("NVEncC64.exe", "NVEncC.exe"),
}

_WINDOWS_WINGET_PATTERNS: dict[str, tuple[str, ...]] = {
    "ffmpeg": ("Gyan.FFmpeg*",),
    "ffprobe": ("Gyan.FFmpeg*",),
    "mediainfo": ("MediaArea.MediaInfo_*",),
}

_DEFAULT_AUDIO_BITRATE_PER_CHANNEL_KBPS = 96

AUDIO_BITRATE_STEPS: list[int] = [
    32, 40, 48, 56, 64, 80, 96, 112,
    128, 160, 192, 224, 256, 320, 384, 448, 512, 576, 640,
]

_REMOVED_INI_KEYS: dict[str, set[str]] = {
    "audio_encoding": {
        "default_bitrate_per_channel_kbps",
        "bitrate_step_per_channel_kbps",
    },
    "ui": {
        "verbose_file_logging",
    },
}


def _load_ini() -> configparser.ConfigParser:
    """Charge config.ini s'il existe, retourne un parser vide sinon."""
    parser = configparser.ConfigParser(
        inline_comment_prefixes=("#",),
        default_section="DEFAULT",
    )
    if _INI_PATH.exists():
        _sanitize_windows_ini_file(_INI_PATH)
        parser.read(_INI_PATH, encoding="utf-8")
    return parser


def _command_exists(command: str | os.PathLike[str] | None) -> bool:
    """True si la commande configurée est résoluble ou pointe vers un fichier."""
    if command in (None, ""):
        return False
    assert command is not None
    value = os.fspath(command)
    if shutil.which(value) is not None:
        return True
    return Path(value).is_file()


def _command_path(command: str | os.PathLike[str] | None) -> Path | None:
    """Retourne le chemin résolu d'une commande, y compris si elle est absolue."""
    if command in (None, ""):
        return None
    assert command is not None
    value = os.fspath(command)
    found = shutil.which(value)
    if found:
        return Path(found)
    candidate = Path(value)
    return candidate if candidate.is_file() else None


def _is_windows() -> bool:
    return sys.platform == "win32"


def _normalize_ui_scale_percent(value: int | None) -> int:
    """Clamp UI scale percentage to a safe interactive range."""
    if value is None:
        return 100
    return max(50, min(200, int(value)))


def _appimage_tools_dir() -> Path | None:
    """
    Dans un AppImage all-inclusive, retourne le chemin absolu de usr/bin/tools/.
    $APPDIR est exporté par AppRun avant le lancement de l'exécutable.
    Retourne None si on ne tourne pas dans un AppImage allinc.
    """
    appdir = os.environ.get("APPDIR")
    if not appdir:
        return None
    tools = Path(appdir) / "usr" / "bin" / "tools"
    return tools if tools.is_dir() else None


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
    if not _is_windows() or "\\" not in text:
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


def _normalize_ini_value(section: str, value: str) -> str:
    if _is_windows() and section.lower() in {"paths", "tools"}:
        return _normalize_windows_backslashes(value)
    return value


def _sanitize_windows_ini_lines(lines: list[str]) -> list[str]:
    if not _is_windows():
        return lines

    current_section = ""
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped[1:-1].strip().lower()
            continue
        if current_section not in {"paths", "tools"}:
            continue
        if not stripped or stripped.startswith(("#", ";")) or "=" not in stripped:
            continue
        lhs, rhs = stripped.split("=", 1)
        normalized = _normalize_ini_value(current_section, rhs.strip())
        if normalized != rhs.strip():
            lines[index] = f"{lhs.strip()} = {normalized}"
    return lines


def _sanitize_windows_ini_file(path: Path) -> None:
    if not _is_windows() or not path.exists():
        return

    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    sanitized_lines = _sanitize_windows_ini_lines(lines.copy())
    sanitized = "\n".join(sanitized_lines).rstrip() + "\n"
    if sanitized != original:
        path.write_text(sanitized, encoding="utf-8")


def rerun_application_setup() -> None:
    """
    Relance la logique de setup depuis l'application déjà ouverte.

    Réutilise le même flux que le premier lancement pour réinstaller les
    dépendances/outils externes et re-générer config.ini si besoin.
    """
    import setup as setup_mod  # noqa: PLC0415

    os_name = platform.system()
    prefix = setup_mod._default_prefix()
    dry_run = False
    force = False

    if os_name == "Linux":
        distro = setup_mod.detect_linux_distro()
        if distro == "debian":
            setup_mod.install_apt(dry_run, force=force)
        elif distro == "fedora":
            setup_mod.install_dnf(dry_run, force=force)
        setup_mod.install_github_tools(prefix, dry_run, force=force)
        setup_mod.check_tools_presence()
    elif os_name == "Darwin":
        setup_mod.install_brew(dry_run, force=force)
        setup_mod.install_github_tools(prefix, dry_run, force=force)
        setup_mod.check_tools_presence()
    elif os_name == "Windows":
        setup_mod.ensure_windows_required_tools(prefix, dry_run=dry_run, force=force)
        setup_mod.offer_windows_controlled_folder_access_setup(prefix, dry_run, force=force)

    setup_mod.initialize_config_ini_language(dry_run, force=force, ini_path=_INI_PATH)

    if not getattr(sys, "frozen", False):
        setup_mod.install_python_packages(dry_run, force=force)


def restart_application() -> bool:
    """
    Redémarre l'application courante.

    Préfère le helper du launcher s'il est disponible pour conserver le
    comportement frozen/dev déjà existant.
    """
    launcher_mod: ModuleType | None
    try:
        import launcher as imported_launcher  # noqa: PLC0415
        launcher_mod = imported_launcher
    except Exception:
        launcher_mod = None

    restart_fn = getattr(launcher_mod, "_restart_current_app", None) if launcher_mod else None
    if callable(restart_fn):
        try:
            return bool(restart_fn())
        except Exception:
            return False

    try:
        # start_new_session détache le nouveau process du tty/groupe parent :
        # évite que sa fermeture entraîne le process courant ou l'inverse.
        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform != "win32":
            popen_kwargs["start_new_session"] = True
        if getattr(sys, "frozen", False):
            subprocess.Popen([sys.executable], **popen_kwargs)
        else:
            launcher_path = Path(__file__).parent.parent / "launcher.py"
            subprocess.Popen([sys.executable, str(launcher_path)], **popen_kwargs)
        return True
    except Exception:
        return False


def _upsert_ini_section(
    lines: list[str],
    section: str,
    values: dict[str, str],
    *,
    replace_blank_only: bool = False,
) -> list[str]:
    start, end = _section_bounds(lines, section)

    if start == -1:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([f"[{section}]"])
        start = len(lines) - 1
        end = len(lines)

    insert_at = end
    for key, value in values.items():
        rendered = _normalize_ini_value(section, value)
        updated = False
        for index in range(start + 1, end):
            stripped = lines[index].strip()
            if not stripped or stripped.startswith(("#", ";")) or "=" not in stripped:
                continue
            lhs, rhs = stripped.split("=", 1)
            if lhs.strip().lower() != key.lower():
                continue
            if (not replace_blank_only) or (not rhs.strip()):
                lines[index] = f"{key} = {rendered}"
            updated = True
            break

        if not updated:
            lines.insert(insert_at, f"{key} = {rendered}")
            insert_at += 1
            end += 1

    return _sanitize_windows_ini_lines(lines)


def _remove_ini_keys(lines: list[str], removed: dict[str, set[str]]) -> list[str]:
    updated = lines
    for section, keys in removed.items():
        start, end = _section_bounds(updated, section)
        if start == -1:
            continue
        lowered_keys = {key.lower() for key in keys}
        kept_lines: list[str] = []
        for line in updated[start + 1:end]:
            stripped = line.strip()
            if stripped and not stripped.startswith(("#", ";")) and "=" in stripped:
                lhs, _rhs = stripped.split("=", 1)
                if lhs.strip().lower() in lowered_keys:
                    continue
            kept_lines.append(line)
        updated = updated[:start + 1] + kept_lines + updated[end:]
    return _sanitize_windows_ini_lines(updated)



def _update_ini_tools_section(path: Path, tool_values: dict[str, str]) -> None:
    """Ajoute les chemins détectés dans [tools] sans écraser une valeur explicite."""
    if not tool_values:
        return

    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    lines = _upsert_ini_section(lines, "tools", tool_values, replace_blank_only=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_ini_settings(section_values: dict[str, dict[str, str]]) -> None:
    """Écrit des valeurs explicites dans config.ini en conservant les commentaires."""
    text = _INI_PATH.read_text(encoding="utf-8") if _INI_PATH.exists() else ""
    lines = text.splitlines()

    for section, values in section_values.items():
        lines = _upsert_ini_section(lines, section, values)
    removed_keys = {
        section: _REMOVED_INI_KEYS[section]
        for section in section_values
        if section in _REMOVED_INI_KEYS
    }
    if removed_keys:
        lines = _remove_ini_keys(lines, removed_keys)

    _INI_PATH.parent.mkdir(parents=True, exist_ok=True)
    _INI_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _windows_user_tools_dir() -> Path:
    """
    Dossier d'installation des outils binaires côté utilisateur
    (%LOCALAPPDATA%\\Muxiveo\\tools). Utilisé quand l'exécutable
    est installé en lecture seule (MSIX, Program Files).
    """
    local_appdata = os.environ.get("LOCALAPPDATA")
    base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
    return base / "Muxiveo" / "tools"


def _windows_repo_tool_dirs() -> list[Path]:
    base_dirs = [_INI_PATH.parent]
    if getattr(sys, "frozen", False):
        base_dirs.append(Path(sys.executable).parent)
    else:
        base_dirs.append(Path(__file__).parent.parent)

    dirs: list[Path] = []
    for base_dir in _dedupe_paths(base_dirs):
        dirs.extend([base_dir / "tools", base_dir / "tools" / "bin"])

    user_tools = _windows_user_tools_dir()
    dirs.extend([user_tools, user_tools / "bin"])
    return _dedupe_paths(dirs)


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


def _windows_winget_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
    return Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"


def _windows_default_tool_candidates(tool_name: str) -> list[Path]:
    exe_names = _WINDOWS_TOOL_FILENAMES.get(tool_name, (f"{tool_name}.exe",))
    candidates: list[Path] = []

    for directory in _windows_repo_tool_dirs():
        for exe_name in exe_names:
            candidates.append(directory / exe_name)

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

    winget_root = _windows_winget_root()
    if winget_root.exists():
        for pattern in _WINDOWS_WINGET_PATTERNS.get(tool_name, ()): 
            for package_dir in winget_root.glob(pattern):
                for exe_name in exe_names:
                    candidates.append(package_dir / exe_name)
                    candidates.extend(path for path in package_dir.rglob(exe_name))

    return _dedupe_paths(candidates)


def _detect_windows_tool_path(tool_name: str, current_value: str) -> str:
    current_value = (current_value or "").strip()
    if not _is_windows():
        return current_value

    current_path = Path(current_value)
    if current_value and current_path.is_file():
        return str(current_path)

    resolved = shutil.which(current_value) if current_value else None
    if resolved:
        return resolved

    for candidate in _windows_default_tool_candidates(tool_name):
        if candidate.is_file():
            return str(candidate)

    return current_value


def _non_windows_tool_candidates(tool_name: str) -> list[Path]:
    repo_root = Path(__file__).parent.parent
    candidates = [
        repo_root / "tools" / tool_name,
        repo_root / "tools" / "bin" / tool_name,
        Path.home() / ".local" / "bin" / tool_name,
        Path("/usr/local/bin") / tool_name,
        Path("/usr/bin") / tool_name,
    ]
    if sys.platform == "darwin":
        candidates.extend(
            [
                Path("/opt/homebrew/bin") / tool_name,
                Path("/opt/local/bin") / tool_name,
            ]
        )
    return _dedupe_paths(candidates)


@dataclass(frozen=True)
class ToolVersionInfo:
    """Version détectée d'un outil externe."""

    text: str | None = None
    major: int | None = None


class ToolVersionRegistry:
    """
    Registre lazy des versions d'outils externes.

    Les versions sont résolues à la demande via `<tool> --version` puis
    mémorisées en cache.
    """

    def __init__(self, commands: dict[str, str]) -> None:
        self._commands = commands
        self._cache: dict[str, ToolVersionInfo] = {}

    @staticmethod
    def _extract_major(version_text: str) -> int | None:
        # Exemples ciblés:
        # - "sometool v98.0 ('Codename')"    ← format vX.Y
        # - "ffmpeg version 8.1 ..."          ← format version X.Y
        # - "MediaInfo Command line, MediaInfoLib - v24.12"
        patterns = (
            r"\bv(\d+)\.",
            r"\bversion\s+(\d+)\.",
            r"\b(\d+)\.(\d+)",
        )
        for pattern in patterns:
            m = re.search(pattern, version_text, flags=re.IGNORECASE)
            if m:
                try:
                    return int(m.group(1))
                except ValueError:
                    return None
        return None

    def _probe(self, command: str) -> ToolVersionInfo:
        try:
            result = subprocess.run(
                [command, "--version"],
                capture_output=True,
                check=False,
                timeout=5,
                **subprocess_text_kwargs(),
            )
        except Exception:
            return ToolVersionInfo()

        text = (result.stdout or "").strip() or (result.stderr or "").strip()
        if not text:
            return ToolVersionInfo()

        first_line = text.splitlines()[0].strip()
        return ToolVersionInfo(
            text=first_line or None,
            major=self._extract_major(first_line),
        )

    def get(self, name: str) -> ToolVersionInfo:
        if name in self._cache:
            return self._cache[name]
        command = self._commands.get(name, name)
        info = self._probe(command)
        self._cache[name] = info
        return info

    def major(self, name: str) -> int | None:
        return self.get(name).major

    def text(self, name: str) -> str | None:
        return self.get(name).text

    def snapshot(self) -> dict[str, ToolVersionInfo]:
        return {name: self.get(name) for name in self._commands}


# ---------------------------------------------------------------------------
# Chemins applicatifs
# ---------------------------------------------------------------------------

def _app_data_dir() -> Path:
    """Retourne le dossier de données persistantes de l'application."""
    raw = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation
    )
    if raw:
        root = Path(raw)
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        root = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    p = root / APP_CONFIG_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def _default_work_dir() -> Path:
    tmp = Path(tempfile.gettempdir())
    p = tmp / APP_TEMP_WORK_DIR_NAME
    return p


def _default_output_dir() -> Path:
    raw = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.MoviesLocation
    )
    return Path(raw) if raw else Path.home() / "Videos"


def _default_verbose_log_dir() -> Path:
    return _app_data_dir() / "logs"


def _default_language_code() -> str:
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


def _normalize_language_code(code: str | None) -> str:
    if not code:
        return _default_language_code()
    raw = code.strip()
    if not raw:
        return _default_language_code()
    if len(raw) == 3:
        ietf = Rfc5646LanguageTags.from_iso639_2(raw)
        if ietf:
            canonical = Rfc5646LanguageTags.to_iso639_2(ietf) or raw.lower()
            return canonical.lower()
    converted = Rfc5646LanguageTags.from_locale_name(raw)
    return converted or _default_language_code()


UI_STARTUP_PANEL_CHOICES: tuple[tuple[str, str], ...] = (
    ("dashboard", "Tableau de bord"),
    ("container", "Conteneur"),
    ("encoding", "Encodage"),
    ("dovi", "DoVi / HDR10+"),
    ("settings", "Paramètres"),
)

def _normalize_startup_panel(value: str | None) -> str:
    if not value:
        return "dashboard"
    raw = value.strip().lower()
    aliases = {
        "dashboard": "dashboard",
        "tableau_de_bord": "dashboard",
        "tableau de bord": "dashboard",
        "home": "dashboard",
        "container": "container",
        "conteneur": "container",
        "encoding": "encoding",
        "encodage": "encoding",
        "dovi": "dovi",
        "dovi / hdr10+": "dovi",
        "settings": "settings",
        "parametres": "settings",
        "paramètres": "settings",
    }
    return aliases.get(raw, "dashboard")


def _normalize_file_logging_level(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"standard", "verbose"}:
        return raw
    return "standard"



INI_FIELD_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "section": "paths",
        "title": "Chemins",
        "fields": (
            {
                "key": "work_dir",
                "attr": "work_dir",
                "kind": "directory",
                "label": "Dossier de travail",
                "description": "Répertoire des fichiers intermédiaires utilisés par les workflows.",
            },
            {
                "key": "output_dir",
                "attr": "output_dir",
                "kind": "directory",
                "label": "Dossier de sortie",
                "description": "Répertoire par défaut des fichiers produits.",
            },
        ),
    },
    {
        "section": "tools",
        "title": "Outils externes",
        "fields": (
            {"key": "ffmpeg", "attr": "tool_ffmpeg", "kind": "tool", "label": "FFmpeg", "description": "Binaire FFmpeg utilisé pour l'encodage et certaines inspections."},
            {"key": "ffprobe", "attr": "tool_ffprobe", "kind": "tool", "label": "FFprobe", "description": "Binaire FFprobe utilisé pour l'analyse des médias."},
            {"key": "mediainfo", "attr": "tool_mediainfo", "kind": "tool", "label": "MediaInfo", "description": "Binaire MediaInfo utilisé pour enrichir l'inspection."},
            {"key": "dovi_tool", "attr": "tool_dovi_tool", "kind": "tool", "label": "dovi_tool", "description": "Outil Dolby Vision utilisé pour les workflows DoVi."},
            {"key": "hdr10plus_tool", "attr": "tool_hdr10plus", "kind": "tool", "label": "hdr10plus_tool", "description": "Outil HDR10+ utilisé pour les workflows HDR."},
            {"key": "eac3to", "attr": "tool_eac3to", "kind": "tool", "label": "eac3to", "description": "Option facultative sous Windows pour la conversion audio avancée."},
            {"key": "nvencc", "attr": "tool_nvencc", "kind": "tool", "label": "NVEncC", "description": "Wrapper NVIDIA NVENC standalone (rigaya) — encodage avancé. Détecté uniquement si un GPU NVIDIA est présent."},
        ),
    },
    {
        "section": "ffmpeg",
        "title": "FFmpeg",
        "fields": (
            {
                "key": "threads",
                "attr": "ffmpeg_threads",
                "kind": "int",
                "label": "Nombre de threads FFmpeg",
                "description": "Nombre de threads passé à FFmpeg via -threads. 0 laisse FFmpeg choisir automatiquement. La valeur par défaut est calculée à partir du nombre de coeurs × 1,5.",
            },
        ),
    },
    {
        "section": "hdr",
        "title": "HDR",
        "fields": (
            {"key": "dovi_profile", "attr": "dovi_profile", "kind": "text", "label": "Profil DoVi", "description": "Profil Dolby Vision utilisé lors de l'injection RPU."},
            {"key": "dovi_compat_id", "attr": "dovi_compat_id", "kind": "text", "label": "Compatibility ID DoVi", "description": "Compatibility ID Dolby Vision appliqué lors de l'injection."},
        ),
    },
    {
        "section": "encoding",
        "title": "Encodage",
        "fields": (
            {"key": "ram_buffer_enabled", "attr": "ram_buffer_enabled", "kind": "bool", "label": "Buffer RAM activé", "description": "Active le buffer RAM pour les fichiers HEVC intermédiaires quand le seuil le permet."},
            {"key": "ram_buffer_threshold_pct", "attr": "ram_buffer_threshold_pct", "kind": "int", "label": "Seuil buffer RAM (%)", "description": "Pourcentage minimal de RAM libre à conserver."},
            {"key": "max_parallel_video_encodes", "attr": "max_parallel_video_encodes", "kind": "int", "label": "Encodages vidéo parallèles max", "description": "Nombre maximal de pistes vidéo encodées simultanément dans le pipeline multi-pistes. 1 = séquentiel (défaut)."},
        ),
    },
    {
        "section": "remux",
        "title": "Remux Matroska",
        "fields": (
            {"key": "mux_backend", "attr": "remux_mux_backend", "kind": "choice", "label": "Backend de muxage", "description": "Auto privilégie le muxeur Matroska natif et journalise un repli FFmpeg lorsque nécessaire.", "options": (("auto", "Auto"), ("native", "Natif Matroska"), ("ffmpeg", "FFmpeg"))},
        ),
    },
    {
        "section": "sync",
        "title": "Synchronisation",
        "fields": (
            {
                "key": "rewrite_enabled",
                "attr": "sync_rewrite_enabled",
                "kind": "bool",
                "label": "Réécrire physiquement les décalages audio/sous-titres",
                "description": "Option expérimentale. Si activée, certains décalages peuvent être matérialisés par réécriture des timestamps sous-titres ou réencodage audio simple. Désactivée par défaut.",
            },
            {
                "key": "advanced_audio_rewrite_enabled",
                "attr": "sync_advanced_audio_rewrite_enabled",
                "kind": "bool",
                "label": "Activer la sync réelle audio avancée",
                "description": "Autorise les stratégies audio avancées pour les formats non simples lorsque la réécriture physique est activée.",
                "tooltip": "Active des stratégies expérimentales pour TrueHD/MLP, DTS/DTS-HD/DTS:X, EAC3+JOC/Atmos, PCM/LPCM, formats lossless et formats lossy. Selon le codec, la piste peut être recopiée par blocs, réencodée, convertie en lossless/lossy, ou rester en sync offset si l'opération n'est pas sûre. Les pistes objet Atmos/DTS:X ne sont pas réencodées.",
            },
        ),
    },
    {
        "section": "ui",
        "title": "Interface",
        "fields": (
            {"key": "language", "attr": "language", "kind": "language", "label": "Langue de l'interface", "description": "Langue utilisée pour l'UI et les messages internes."},
            {"key": "log_max_lines", "attr": "log_max_lines", "kind": "int", "label": "Nombre max de lignes de log", "description": "Nombre maximum de lignes conservées dans le panneau de log."},
            {"key": "theme", "attr": "theme", "kind": "choice", "label": "Thème", "description": "Thème principal pour l'interface. Le changement de thème nécessite de redémarrer l'application.", "options": (("dark", "Sombre"), ("light", "Clair"))},
            {"key": "ui_scale_percent", "attr": "ui_scale_percent", "kind": "int", "label": "Échelle de l'interface (%)", "description": "Facteur d'échelle appliqué à l'interface. Le changement est appliqué immédiatement autant que possible, mais un redémarrage peut être recommandé pour uniformiser tout l'affichage.", "min": 50, "max": 200},
            {"key": "startup_panel", "attr": "startup_panel", "kind": "choice", "label": "Panneau à afficher au démarrage", "description": "Panneau chargé en premier au lancement de l'application.", "options": UI_STARTUP_PANEL_CHOICES},
            {"key": "startup_menu_compact", "attr": "startup_menu_compact", "kind": "bool", "label": "Démarrer avec le menu en mode Compact", "description": "Si activé, le menu latéral est réduit en mode icônes au lancement."},
            {"key": "startup_logs_expanded", "attr": "startup_logs_expanded", "kind": "bool", "label": "Ouvrir les logs au démarrage de l'application", "description": "Si activé, le panneau de logs est déplié au lancement."},
            {"key": "enable_file_logging", "attr": "enable_file_logging", "kind": "bool", "label": "Activer le logging fichier", "description": "Si activé, les logs applicatifs sont aussi écrits dans un fichier texte sous app_data/logs/."},
            {"key": "file_logging_level", "attr": "file_logging_level", "kind": "choice", "label": "Niveau de logging fichier", "description": "Standard écrit le flux visible dans la fenêtre. Verbose ajoute les sorties techniques détaillées des outils.", "options": (("standard", "Standard"), ("verbose", "Verbose"))},
            {"key": "verbose_log_dir", "attr": "verbose_log_dir", "kind": "directory", "label": "Dossier des logs fichier", "description": "Dossier où écrire les logs fichier. Prérempli par défaut avec le chemin complet actuel."},
        ),
    },
    {
        "section": "audio_encoding",
        "title": "Encodage audio",
        "fields": (
            {
                "key": "aac_bitrate_per_channel_kbps",
                "attr": "aac_bitrate_per_channel_kbps",
                "kind": "stepped_slider",
                "steps": AUDIO_BITRATE_STEPS,
                "label": "Débit AAC par canal (kbps)",
                "description": f"Débit par canal utilisé pour le calcul du débit AAC par défaut. Débit total = cette valeur × nombre de canaux. Défaut : {_DEFAULT_AUDIO_BITRATE_PER_CHANNEL_KBPS} kbps.",
            },
            {
                "key": "eac3_bitrate_per_channel_kbps",
                "attr": "eac3_bitrate_per_channel_kbps",
                "kind": "stepped_slider",
                "steps": AUDIO_BITRATE_STEPS,
                "label": "Débit EAC-3 par canal (kbps)",
                "description": f"Débit par canal utilisé pour le calcul du débit EAC-3 par défaut. Débit total = cette valeur × nombre de canaux. Défaut : {_DEFAULT_AUDIO_BITRATE_PER_CHANNEL_KBPS} kbps.",
            },
        ),
    },
    {
        "section": "metadata",
        "title": "Métadonnées",
        "fields": (
            {"key": "tmdb_api_key", "attr": "tmdb_api_key", "kind": "text", "label": "Clé API TMDB", "description": "Clé API TMDB v3 (gratuite sur https://www.themoviedb.org/settings/api)."},
            {"key": "tmdb_bearer_token", "attr": "tmdb_bearer_token", "kind": "text", "label": "Token Bearer TMDB", "description": f"Token v4 TMDB optionnel. Utilisé si la clé API est vide. Peut aussi être défini via {APP_ENV_PREFIX}_TMDB_BEARER_TOKEN."},
            {"key": "generate_nfo", "attr": "generate_nfo", "kind": "bool", "label": "Générer un fichier .nfo après remux/encodage", "description": "Crée automatiquement un fichier .nfo (même nom que le MKV) contenant la sortie brute de mediainfo après chaque workflow réussi."},
        ),
    },
)


def iter_ini_fields() -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for group in INI_FIELD_GROUPS:
        fields.extend(group["fields"])
    return fields


# ---------------------------------------------------------------------------
# AppConfig
# ---------------------------------------------------------------------------

class AppConfig:
    """
    Configuration centralisée de l'application.

    Les propriétés sont persistées via INI :
    - Windows : QSettings pointe directement vers config.ini
    - Linux/macOS : QSettings user-scope INI
    config.ini a priorité sur les valeurs sauvegardées, elles-mêmes
    prioritaires sur les défauts.

    Une clé présente mais vide dans config.ini revient explicitement au défaut
    documenté au lieu de retomber sur une ancienne valeur QSettings.
    """

    _SETTINGS_ORG = APP_CONFIG_DIR_NAME
    _SETTINGS_APP = APP_CONFIG_DIR_NAME

    def __init__(self) -> None:
        if _is_windows():
            _INI_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._settings = QSettings(str(_INI_PATH), QSettings.Format.IniFormat)
        else:
            self._settings = QSettings(
                QSettings.Format.IniFormat,
                QSettings.Scope.UserScope,
                self._SETTINGS_ORG,
                self._SETTINGS_APP,
            )
        self._ini = _load_ini()
        self._detected_ini_tools: dict[str, str] = {}
        self._tool_versions = ToolVersionRegistry({})
        self._load()
        self._persist_detected_windows_tools()

    def _persist_detected_windows_tools(self) -> None:
        if _is_windows() and self._detected_ini_tools:
            _update_ini_tools_section(_INI_PATH, self._detected_ini_tools)
            self._ini = _load_ini()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ini_lookup(self, section: str, key: str) -> str | object:
        if self._ini.has_option(section, key):
            return self._ini.get(section, key).strip()
        return _MISSING

    def _resolve_text(self, section: str, key: str, settings_key: str, default: str) -> str:
        ini_value = self._ini_lookup(section, key)
        if ini_value is not _MISSING:
            return default if ini_value == "" else str(ini_value)
        value = self._settings.value(settings_key, default)
        return str(value if value not in (None, "") else default)

    def _resolve_path(self, section: str, key: str, settings_key: str, default: Path) -> Path:
        return Path(self._resolve_text(section, key, settings_key, str(default)))

    def _resolve_int(self, section: str, key: str, settings_key: str, default: int) -> int:
        def _parse_int(raw: object) -> int | None:
            try:
                return int(str(raw).strip())
            except (TypeError, ValueError):
                return None

        ini_value = self._ini_lookup(section, key)
        if ini_value is not _MISSING:
            if ini_value == "":
                return default
            parsed_ini = _parse_int(ini_value)
            return parsed_ini if parsed_ini is not None else default
        value = self._settings.value(settings_key, default)
        if value in (None, ""):
            return default
        if isinstance(value, int):
            return value
        parsed_settings = _parse_int(value)
        return parsed_settings if parsed_settings is not None else default

    def _resolve_bool(self, section: str, key: str, settings_key: str, default: bool) -> bool:
        def _as_bool(raw: str) -> bool:
            return raw.strip().lower() not in ("0", "false", "no", "off")

        ini_value = self._ini_lookup(section, key)
        if ini_value is not _MISSING:
            return default if ini_value == "" else _as_bool(str(ini_value))

        default_text = "true" if default else "false"
        value = self._settings.value(settings_key, default_text)
        return _as_bool(str(value if value not in (None, "") else default_text))

    def _resolve_tool_value(self, ini_key: str, settings_key: str, default: str) -> str:
        """
        Résout la valeur d'un outil externe.

        Priorité :
          1. AppImage allinc  — chemin absolu dans $APPDIR/usr/bin/tools/
          2. config.ini       — valeur explicite dans [tools]
          3. Linux / macOS    — nom brut (ex: "ffmpeg") appelé directement via PATH
             Windows          — autodetect (Program Files, WinGet, QSettings)
        """
        # Priorité 1 : AppImage allinc
        tools_dir = _appimage_tools_dir()
        if tools_dir is not None:
            candidate = tools_dir / ini_key
            if candidate.is_file():
                return str(candidate)

        # Priorité 2 : config.ini
        ini_value = self._ini_lookup("tools", ini_key)
        if ini_value is not _MISSING and ini_value != "":
            return str(ini_value)

        # Priorité 3a : valeur persistée par l'UI.
        raw = self._settings.value(settings_key, None)
        if raw not in (None, ""):
            current_value = str(raw)
            if current_value != default or _command_path(current_value) is not None:
                return current_value

        # Priorité 3b : Linux / macOS — appel direct, résolution par le PATH à l'exécution
        if not _is_windows():
            resolved = shutil.which(default)
            if resolved:
                return resolved
            for candidate in _non_windows_tool_candidates(ini_key):
                if candidate.is_file():
                    return str(candidate)
            return default

        # Priorité 3c : Windows — autodetect étendu + persistance dans QSettings
        current_value = default
        resolved = _detect_windows_tool_path(ini_key, current_value)
        if Path(resolved).is_file():
            self._detected_ini_tools.setdefault(ini_key, resolved)
        return resolved

    # ------------------------------------------------------------------
    # Chargement
    # ------------------------------------------------------------------

    def _load(self) -> None:
        self.work_dir = self._resolve_path("paths", "work_dir", "paths/work_dir", _default_work_dir())
        self.output_dir = self._resolve_path("paths", "output_dir", "paths/output_dir", _default_output_dir())
        self.config_dir = _INI_PATH.parent
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.profiles_dir = self.config_dir / "profiles"
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.app_data_dir = _app_data_dir()

        self.tool_ffmpeg = self._resolve_tool_value("ffmpeg", "tools/ffmpeg", "ffmpeg")
        self.tool_ffprobe = self._resolve_tool_value("ffprobe", "tools/ffprobe", "ffprobe")
        self.tool_mediainfo = self._resolve_tool_value("mediainfo", "tools/mediainfo", "mediainfo")
        self.tool_dovi_tool = self._resolve_tool_value("dovi_tool", "tools/dovi_tool", "dovi_tool")
        self.tool_hdr10plus = self._resolve_tool_value("hdr10plus_tool", "tools/hdr10plus_tool", "hdr10plus_tool")
        self.tool_eac3to = self._resolve_tool_value("eac3to", "tools/eac3to", "eac3to")
        # NVEncC: nom du binaire varie selon plateforme et packaging :
        #   • Windows : NVEncC64.exe (rigaya release .7z)
        #   • Linux .deb (Debian/Ubuntu) : NVEncC (PascalCase, /usr/bin/NVEncC)
        #   • Linux .rpm (Fedora/RHEL) : nvencc (minuscules, /usr/bin/nvencc)
        # Pas de support macOS — sera résolu en None silencieusement si introuvable.
        if _is_windows():
            self.tool_nvencc = self._resolve_tool_value("nvencc", "tools/nvencc", "NVEncC64.exe")
        else:
            # Essai PascalCase d'abord (.deb), puis minuscules (.rpm).
            resolved = self._resolve_tool_value("nvencc", "tools/nvencc", "NVEncC")
            if shutil.which(resolved) is None and not Path(resolved).is_file():
                fallback = shutil.which("nvencc")
                if fallback:
                    resolved = fallback
            self.tool_nvencc = resolved
        self._tool_versions = ToolVersionRegistry(self.tool_commands())

        self.ffmpeg_threads = _normalize_ffmpeg_thread_count(
            self._resolve_int("ffmpeg", "threads", "ffmpeg/threads", _default_ffmpeg_thread_count())
        )
        self.dovi_profile = self._resolve_text("hdr", "dovi_profile", "hdr/dovi_profile", "8")
        self.dovi_compat_id = self._resolve_text("hdr", "dovi_compat_id", "hdr/dovi_compat_id", "1")

        self.ram_buffer_enabled = self._resolve_bool("encoding", "ram_buffer_enabled", "encoding/ram_buffer_enabled", True)
        self.ram_buffer_threshold_pct = self._resolve_int("encoding", "ram_buffer_threshold_pct", "encoding/ram_buffer_threshold_pct", 15)
        self.max_parallel_video_encodes = _normalize_max_parallel_video_encodes(
            self._resolve_int("encoding", "max_parallel_video_encodes", "encoding/max_parallel_video_encodes", 1)
        )

        self.aac_bitrate_per_channel_kbps = self._resolve_int(
            "audio_encoding", "aac_bitrate_per_channel_kbps",
            "audio_encoding/aac_bitrate_per_channel_kbps",
            _DEFAULT_AUDIO_BITRATE_PER_CHANNEL_KBPS,
        )
        self.eac3_bitrate_per_channel_kbps = self._resolve_int(
            "audio_encoding", "eac3_bitrate_per_channel_kbps",
            "audio_encoding/eac3_bitrate_per_channel_kbps",
            _DEFAULT_AUDIO_BITRATE_PER_CHANNEL_KBPS,
        )
        self.sync_rewrite_enabled = self._resolve_bool(
            "sync", "rewrite_enabled", "sync/rewrite_enabled", False
        )
        self.sync_advanced_audio_rewrite_enabled = self._resolve_bool(
            "sync",
            "advanced_audio_rewrite_enabled",
            "sync/advanced_audio_rewrite_enabled",
            False,
        )
        raw_mux_backend = self._resolve_text("remux", "mux_backend", "remux/mux_backend", "auto").lower()
        self.remux_mux_backend = raw_mux_backend if raw_mux_backend in {"auto", "native", "ffmpeg"} else "auto"

        self.language = _normalize_language_code(
            self._resolve_text("ui", "language", "ui/language", _default_language_code())
        )
        self.log_max_lines = self._resolve_int("ui", "log_max_lines", "ui/log_max_lines", 2000)
        legacy_verbose_file_logging = self._resolve_bool(
            "ui", "verbose_file_logging", "ui/verbose_file_logging", False
        )
        enable_file_logging_ini = self._ini_lookup("ui", "enable_file_logging")
        if enable_file_logging_ini is _MISSING and self._ini_lookup("ui", "verbose_file_logging") is not _MISSING:
            self.enable_file_logging = legacy_verbose_file_logging
        else:
            self.enable_file_logging = self._resolve_bool(
                "ui", "enable_file_logging", "ui/enable_file_logging", legacy_verbose_file_logging
            )
        file_logging_level_ini = self._ini_lookup("ui", "file_logging_level")
        default_file_logging_level = "verbose" if legacy_verbose_file_logging else "standard"
        if file_logging_level_ini is _MISSING and self._ini_lookup("ui", "verbose_file_logging") is not _MISSING:
            self.file_logging_level = default_file_logging_level
        else:
            self.file_logging_level = _normalize_file_logging_level(
                self._resolve_text(
                    "ui",
                    "file_logging_level",
                    "ui/file_logging_level",
                    default_file_logging_level,
                )
            )
        # Compat: ancien nom conservé pour les usages résiduels internes/tests.
        self.verbose_file_logging = self.enable_file_logging
        self.verbose_log_dir = self._resolve_path(
            "ui", "verbose_log_dir", "ui/verbose_log_dir", _default_verbose_log_dir()
        )
        self.theme = self._resolve_text("ui", "theme", "ui/theme", "dark")
        self.ui_scale_percent = _normalize_ui_scale_percent(
            self._resolve_int("ui", "ui_scale_percent", "ui/ui_scale_percent", 100)
        )
        self.startup_panel = _normalize_startup_panel(
            self._resolve_text("ui", "startup_panel", "ui/startup_panel", "dashboard")
        )
        self.startup_menu_compact = self._resolve_bool(
            "ui", "startup_menu_compact", "ui/startup_menu_compact", False
        )
        self.startup_logs_expanded = self._resolve_bool(
            "ui",
            "startup_logs_expanded",
            "ui/startup_logs_expanded",
            False,
        )
        geometry_value = self._settings.value("ui/geometry", None)
        self.window_geometry: bytes | None = geometry_value if isinstance(geometry_value, bytes) else None

        self.tmdb_api_key = self._resolve_text("metadata", "tmdb_api_key", "metadata/tmdb_api_key", "")
        self.tmdb_bearer_token = self._resolve_text(
            "metadata",
            "tmdb_bearer_token",
            "metadata/tmdb_bearer_token",
            "",
        )
        self.generate_nfo = self._resolve_bool("metadata", "generate_nfo", "metadata/generate_nfo", True)

    def reload(self) -> None:
        self._ini = _load_ini()
        self._detected_ini_tools = {}
        self._load()
        self._persist_detected_windows_tools()

    # ------------------------------------------------------------------
    # Sauvegarde
    # ------------------------------------------------------------------

    def save(self) -> None:
        """Persiste la configuration dans QSettings pour les fallbacks runtime."""
        s = self._settings

        s.setValue("paths/work_dir", str(self.work_dir))
        s.setValue("paths/output_dir", str(self.output_dir))

        s.setValue("tools/ffmpeg", self.tool_ffmpeg)
        s.setValue("tools/ffprobe", self.tool_ffprobe)
        s.setValue("tools/mediainfo", self.tool_mediainfo)
        s.setValue("tools/dovi_tool", self.tool_dovi_tool)
        s.setValue("tools/hdr10plus_tool", self.tool_hdr10plus)
        s.setValue("tools/eac3to", self.tool_eac3to)

        s.setValue("ffmpeg/threads", self.ffmpeg_threads)

        s.setValue("hdr/dovi_profile", self.dovi_profile)
        s.setValue("hdr/dovi_compat_id", self.dovi_compat_id)

        s.setValue("encoding/ram_buffer_enabled", "true" if self.ram_buffer_enabled else "false")
        s.setValue("encoding/ram_buffer_threshold_pct", self.ram_buffer_threshold_pct)
        s.setValue("encoding/max_parallel_video_encodes", self.max_parallel_video_encodes)

        s.setValue("audio_encoding/aac_bitrate_per_channel_kbps", self.aac_bitrate_per_channel_kbps)
        s.setValue("audio_encoding/eac3_bitrate_per_channel_kbps", self.eac3_bitrate_per_channel_kbps)
        s.setValue("sync/rewrite_enabled", "true" if self.sync_rewrite_enabled else "false")
        s.setValue(
            "sync/advanced_audio_rewrite_enabled",
            "true" if self.sync_advanced_audio_rewrite_enabled else "false",
        )
        s.setValue("remux/mux_backend", self.remux_mux_backend)

        s.setValue("ui/language", self.language)
        s.setValue("ui/log_max_lines", self.log_max_lines)
        s.setValue(
            "ui/enable_file_logging",
            "true" if self.enable_file_logging else "false",
        )
        s.setValue("ui/file_logging_level", self.file_logging_level)
        s.setValue("ui/verbose_log_dir", str(self.verbose_log_dir))
        s.setValue("ui/theme", self.theme)
        s.setValue("ui/ui_scale_percent", self.ui_scale_percent)
        s.setValue("ui/startup_panel", self.startup_panel)
        s.setValue(
            "ui/startup_menu_compact",
            "true" if self.startup_menu_compact else "false",
        )
        s.setValue(
            "ui/startup_logs_expanded",
            "true" if self.startup_logs_expanded else "false",
        )

        s.setValue("metadata/tmdb_api_key", self.tmdb_api_key)
        s.setValue("metadata/tmdb_bearer_token", self.tmdb_bearer_token)
        s.setValue("metadata/generate_nfo", "true" if self.generate_nfo else "false")
        s.sync()
        _sanitize_windows_ini_file(_INI_PATH)

    def save_to_ini(self) -> None:
        write_ini_settings(self.to_ini_sections())

    def save_geometry(self, geometry: bytes) -> None:
        self._settings.setValue("ui/geometry", geometry)
        self._settings.sync()
        _sanitize_windows_ini_file(_INI_PATH)

    # ------------------------------------------------------------------
    # Utilitaires
    # ------------------------------------------------------------------

    def tool_commands(self) -> dict[str, str]:
        """Retourne la map normalisée {tool_name: commande configurée}."""
        return {
            "ffmpeg": self.tool_ffmpeg,
            "ffprobe": self.tool_ffprobe,
            "mediainfo": self.tool_mediainfo,
            "dovi_tool": self.tool_dovi_tool,
            "hdr10plus_tool": self.tool_hdr10plus,
            "eac3to": self.tool_eac3to,
        }

    def refresh_tool_versions(self) -> None:
        """Réinitialise le registre de versions des outils."""
        self._tool_versions = ToolVersionRegistry(self.tool_commands())

    def rerun_setup(self) -> None:
        """Relance setup.py et recharge ensuite la configuration."""
        rerun_application_setup()
        self.reload()

    def restart_application(self) -> bool:
        """Redémarre l'application courante."""
        return restart_application()

    def tool_version_info(self, name: str) -> ToolVersionInfo:
        """Retourne les infos de version d'un outil."""
        return self._tool_versions.get(name)

    def tool_version_text(self, name: str) -> str | None:
        """Retourne la première ligne de version d'un outil."""
        return self._tool_versions.text(name)

    def tool_major_version(self, name: str) -> int | None:
        """Retourne la version majeure d'un outil."""
        return self._tool_versions.major(name)

    def tool_path(self, name: str) -> Path | None:
        attr = f"tool_{name.replace('-', '_')}"
        value: str = getattr(self, attr, name)
        return _command_path(value)

    def all_tools_available(self) -> dict[str, bool]:
        return {name: _command_exists(cmd) for name, cmd in self.tool_commands().items()}

    def ensure_work_dir(self) -> Path:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        return self.work_dir

    def work_dir_entries(self) -> list[Path]:
        """Retourne les entrées présentes dans le work_dir."""
        return list_work_dir_entries(self.ensure_work_dir())

    def work_dir_has_leftovers(self) -> bool:
        """True si le work_dir contient des éléments non nettoyés."""
        return work_dir_has_entries(self.ensure_work_dir())

    def clear_work_dir(self) -> Path:
        """Vide le contenu du work_dir (sans supprimer le dossier racine)."""
        root = self.ensure_work_dir()
        clear_work_dir_contents(root)
        return root

    def prepare_process_work_dir(
        self,
        output_path: Path,
        *,
        process_name: str | None = None,
    ) -> Path:
        """
        Prépare un dossier process dédié sous work_dir.

        Le nom du dossier est dérivé du nom du fichier de sortie.
        Si le dossier existe déjà, il est vidé avant usage.
        """
        return prepare_process_work_dir(
            self.ensure_work_dir(),
            output_path=output_path,
            process_name=process_name,
            fallback_name="job",
        )

    def ensure_output_dir(self) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        return self.output_dir

    def to_ini_sections(self) -> dict[str, dict[str, str]]:
        section_values: dict[str, dict[str, str]] = {}
        for group in INI_FIELD_GROUPS:
            section = group["section"]
            section_values[section] = {}
            for field in group["fields"]:
                value = getattr(self, field["attr"])
                if isinstance(value, Path):
                    rendered = str(value)
                elif isinstance(value, bool):
                    rendered = "true" if value else "false"
                else:
                    rendered = str(value)
                section_values[section][field["key"]] = rendered
        return section_values

    def to_dict(self) -> dict[str, Any]:
        return {
            "paths": {
                "work_dir": str(self.work_dir),
                "output_dir": str(self.output_dir),
                "config_dir": str(self.config_dir),
                "profiles_dir": str(self.profiles_dir),
                "app_data": str(self.app_data_dir),
            },
            "tools": {
                "ffmpeg": self.tool_ffmpeg,
                "ffprobe": self.tool_ffprobe,
                "mediainfo": self.tool_mediainfo,
                "dovi_tool": self.tool_dovi_tool,
                "hdr10plus_tool": self.tool_hdr10plus,
                "eac3to": self.tool_eac3to,
            },
            "tool_versions": {
                name: {
                    "text": info.text,
                    "major": info.major,
                }
                for name, info in self._tool_versions.snapshot().items()
            },
            "ffmpeg": {
                "threads": self.ffmpeg_threads,
            },
            "hdr": {
                "dovi_profile": self.dovi_profile,
                "dovi_compat_id": self.dovi_compat_id,
            },
            "encoding": {
                "ram_buffer_enabled": self.ram_buffer_enabled,
                "ram_buffer_threshold_pct": self.ram_buffer_threshold_pct,
                "max_parallel_video_encodes": self.max_parallel_video_encodes,
            },
            "sync": {
                "rewrite_enabled": self.sync_rewrite_enabled,
                "advanced_audio_rewrite_enabled": self.sync_advanced_audio_rewrite_enabled,
            },
            "remux": {"mux_backend": self.remux_mux_backend},
            "ui": {
                "language": self.language,
                "log_max_lines": self.log_max_lines,
                "enable_file_logging": self.enable_file_logging,
                "file_logging_level": self.file_logging_level,
                "verbose_log_dir": str(self.verbose_log_dir),
                "theme": self.theme,
                "ui_scale_percent": self.ui_scale_percent,
                "startup_panel": self.startup_panel,
                "startup_menu_compact": self.startup_menu_compact,
                "startup_logs_expanded": self.startup_logs_expanded,
            },
            "metadata": {
                "tmdb_api_key": self.tmdb_api_key,
                "tmdb_bearer_token": self.tmdb_bearer_token,
                "generate_nfo": self.generate_nfo,
            },
        }

    def __repr__(self) -> str:
        return f"AppConfig({json.dumps(self.to_dict(), indent=2)})"
