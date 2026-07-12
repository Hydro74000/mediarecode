#!/usr/bin/env python3
"""
package_appimage.py — Packaging multiplateforme de Muxiveo.

Modes :
  AppImage Linux (défaut)
    Muxiveo-<arch>.AppImage          outils installés au 1er lancement
    Muxiveo-<arch>_allinc.AppImage   tous les outils embarqués (--allinc)

  Installateur Windows (--windows)
    Muxiveo-Setup.exe                construit via Wine + PyInstaller + NSIS
    Nécessite : wine, winetricks, makensis (installés automatiquement si absents)

Étapes AppImage :
  1. Vérifie / installe PyInstaller + dépendances
  2. Construit un bundle --onedir avec PyInstaller (entrée : launcher.py)
  3. Assemble l'AppDir (structure AppImage standard)
     → si --allinc : télécharge et embarque ffmpeg, mediainfo, dovi_tool, hdr10plus_tool
  4. Télécharge appimagetool si nécessaire
  5. Produit l'AppImage finale

Étapes Windows :separate
  1. Vérifie / installe Wine + un préfixe Python Windows dédié
  2. Installe PyInstaller + dépendances dans le préfixe Wine
  3. Construit un bundle --onedir via wine python.exe -m PyInstaller
  4. Génère un script NSIS et produit l'installateur .exe via makensis

Usage :
    distrobox enter my-distrobox -- python3 package_appimage.py
    distrobox enter my-distrobox -- python3 package_appimage.py --allinc
    distrobox enter my-distrobox -- python3 package_appimage.py --skip-pyinstaller
    distrobox enter my-distrobox -- python3 package_appimage.py --arch aarch64
    distrobox enter my-distrobox -- python3 package_appimage.py --windows
    distrobox enter my-distrobox -- python3 package_appimage.py --windows --skip-pyinstaller

Options :
    --allinc             Embarque tous les outils externes dans l'AppImage
    --skip-pyinstaller   Réutilise le bundle PyInstaller existant dans dist/
    --arch ARCH          Architecture cible AppImage : x86_64 (défaut) ou aarch64
    --version TAG        Suffixe de version pour le fichier final (défaut: APP_VERSION)
    --dest PATH          Copie le fichier final vers un chemin personnalisé (dossier ou fichier)
    --windows            Build installateur Windows via Wine (cross-compilation)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import urllib.request
import zipfile
from pathlib import Path

from core.file_types import build_desktop_mime_type_string
from core.version import (
    APP_APPSTREAM_ID,
    APP_EXECUTABLE_NAME,
    APP_NAME,
    APP_VERSION,
    APP_WEBSITE_URL,
)

ROOT = Path(__file__).parent
DIST_DIR = ROOT / "dist"
DIST_RELEASES = ROOT / "dist" / "releases"
BUILD_DIR = ROOT / "build"
APPDIR = ROOT / "Muxiveo.AppDir"
APP_DISPLAY_NAME = APP_NAME
_APPIMAGE_UPDATE_OWNER = os.environ.get("MUXIVEO_APPIMAGE_UPDATE_OWNER", "Hydro74000").strip() or "Hydro74000"
_APPIMAGE_UPDATE_REPO = os.environ.get("MUXIVEO_APPIMAGE_UPDATE_REPO", "Muxiveo").strip() or "Muxiveo"
_APPIMAGE_UPDATE_RELEASE = os.environ.get("MUXIVEO_APPIMAGE_UPDATE_RELEASE", "latest").strip() or "latest"
_APPIMAGE_WEBSITE_URL = APP_WEBSITE_URL
_APPSTREAM_ID = APP_APPSTREAM_ID


def warn(msg: str) -> None:
    print(f"  WARN  {msg}")


def _ensure_linux_bundle_entrypoints(bundle_dir: Path) -> Path:
    """Normalise les noms de commandes dans le bundle Linux."""
    exe_path = bundle_dir / APP_EXECUTABLE_NAME
    legacy_exe = bundle_dir / APP_NAME

    if not exe_path.exists() and legacy_exe.exists():
        legacy_exe.rename(exe_path)
    elif legacy_exe.is_symlink() or legacy_exe.is_file():
        legacy_exe.unlink()

    legacy_cli = bundle_dir / f"{APP_NAME}-cli"
    if legacy_cli.is_symlink() or legacy_cli.is_file():
        legacy_cli.unlink()
    legacy_cli_lower = bundle_dir / f"{APP_EXECUTABLE_NAME}-cli"
    if legacy_cli_lower.is_symlink() or legacy_cli_lower.is_file():
        legacy_cli_lower.unlink()
    return exe_path

# Préfixe Wine dédié au build Windows (isolé du préfixe utilisateur ~/.wine)
WINE_PREFIX = ROOT / ".wine_build"
# Répertoire d'installation de Python Windows dans ce préfixe
_WINE_PY_VER  = "3.11.9"
_WINE_PY_URL  = f"https://www.python.org/ftp/python/{_WINE_PY_VER}/python-{_WINE_PY_VER}-amd64.exe"
_WINE_PY_DEST = ROOT / f"python-{_WINE_PY_VER}-amd64.exe"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text

def ok(msg: str)   -> None: print(_c("32",   f"  ✔  {msg}"))
def info(msg: str) -> None: print(_c("36",   f"  →  {msg}"))
def step(msg: str) -> None: print(_c("1;37", f"\n  ▸ {msg}"))
def err(msg: str)  -> None: print(_c("31",   f"  ✘  {msg}"), file=sys.stderr)


def run(cmd: list[str | Path], **kwargs) -> None:
    info(" ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True, **kwargs)


def _normalize_version_tag(version_tag: str | None) -> str:
    """
    Normalise un tag de version pour un nom de fichier.
    - fallback: APP_VERSION
    - espaces -> '-'
    - caractères autorisés: [A-Za-z0-9._-]
    """
    raw = (version_tag or "").strip() or APP_VERSION
    raw = re.sub(r"\s+", "-", raw)
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "", raw)
    return cleaned or APP_VERSION


def _versioned_output_path(path: Path, version_tag: str | None) -> Path:
    """
    Retourne un path suffixé par '-<version>' avant l'extension.
    Exemple: file.AppImage -> file-1.2.3.AppImage
    """
    tag = _normalize_version_tag(version_tag)
    stem = path.stem if path.suffix else path.name
    if stem.endswith(f"-{tag}"):
        return path
    if path.suffix:
        return path.with_name(f"{path.stem}-{tag}{path.suffix}")
    return path.with_name(f"{path.name}-{tag}")


def _resolve_dest_file(dest: str | None, default_output: Path, version_tag: str | None = None) -> Path:
    """
    Résout le chemin de destination du fichier final.
    - dest absent: place dans dist/releases/ (créé si besoin)
    - dest dossier (existant, trailing slash, ou sans extension): utilise le nom auto
    - dest fichier: utilise ce nom
    """
    if not dest or not dest.strip():
        return _versioned_output_path(DIST_RELEASES / default_output.name, version_tag)

    raw = dest.strip()
    target = Path(raw).expanduser()
    if not target.is_absolute():
        target = (Path.cwd() / target).resolve()

    if target.exists() and target.is_dir():
        return _versioned_output_path(target / default_output.name, version_tag)

    if raw.endswith(("/", "\\")):
        return _versioned_output_path(target / default_output.name, version_tag)

    if target.suffix == "":
        return _versioned_output_path(target / default_output.name, version_tag)

    return _versioned_output_path(target, version_tag)


def _copy_final_file_if_requested(src: Path, dest: str | None, version_tag: str | None = None) -> Path:
    """Déplace le fichier final vers dist/releases/ (ou --dest si fourni)."""
    target = _resolve_dest_file(dest, src, version_tag)

    src_resolved = src.resolve()
    target_resolved = target.resolve(strict=False)
    if src_resolved == target_resolved:
        return src

    target.parent.mkdir(parents=True, exist_ok=True)
    src.replace(target)
    ok(f"Fichier final : {target}")
    return target


# ---------------------------------------------------------------------------
# Étape 0 — Dépendances du script lui-même
# ---------------------------------------------------------------------------

# Paquets nécessaires au build (PyInstaller doit pouvoir les importer)
_BUILD_DEPS: list[str] = [
    "pyinstaller",
    "PySide6>=6.6.0",
    "pymediainfo>=6.1.0",
]


def _pip_install(packages: list[str]) -> None:
    run([sys.executable, "-m", "pip", "install", "--upgrade", *packages])


def _install_system_package(package: str, fatal: bool = True) -> bool:
    """
    Installe un paquet système via dnf ou apt selon la distro disponible.
    Retourne True si l'installation a réussi ou si le gestionnaire est connu.
    Si fatal=True, exit(1) en cas d'échec.
    """
    if shutil.which("dnf"):
        run(["sudo", "dnf", "install", "-y", package])
        return True
    elif shutil.which("apt-get"):
        run(["sudo", "apt-get", "install", "-y", package])
        return True
    else:
        err(f"Impossible d'installer '{package}' automatiquement — gestionnaire de paquets inconnu.")
        err(f"Installez-le manuellement : {package}")
        if fatal:
            sys.exit(1)
        return False


def ensure_build_deps() -> None:
    """Installe toutes les dépendances nécessaires au script de build."""
    step("Vérification des dépendances de build")

    # ── Dépendances Python ────────────────────────────────────────────────
    missing_py: list[str] = []

    if importlib.util.find_spec("PyInstaller") is None:
        missing_py.append("pyinstaller")

    if importlib.util.find_spec("PySide6") is None:
        missing_py.append("PySide6>=6.6.0")

    if importlib.util.find_spec("pymediainfo") is None:
        missing_py.append("pymediainfo>=6.1.0")

    if missing_py:
        info(f"Paquets Python manquants : {', '.join(missing_py)}")
        _pip_install(missing_py)

    # ── mksquashfs — appimagetool l'embarque en interne, non bloquant ────────
    if not shutil.which("mksquashfs"):
        info("mksquashfs introuvable — tentative d'installation de squashfs-tools…")
        _install_system_package("squashfs-tools", fatal=False)
        if not shutil.which("mksquashfs"):
            info("mksquashfs absent du PATH — appimagetool utilisera son mksquashfs interne.")

    ok("Toutes les dépendances sont présentes")


def ensure_zsyncmake() -> Path | None:
    """
    Vérifie que zsyncmake est disponible.
    Tente une installation système si absent.
    Retourne le chemin vers l'outil, ou None si introuvable.
    """
    found = shutil.which("zsyncmake")
    if found:
        return Path(found)

    info("zsyncmake introuvable — tentative d'installation de zsync…")
    _install_system_package("zsync", fatal=False)

    found = shutil.which("zsyncmake")
    if found:
        return Path(found)

    err("zsyncmake introuvable — le fichier .zsync ne sera pas généré.")
    err("Installez zsync manuellement : sudo dnf install zsync  (ou apt-get install zsync)")
    return None


# ---------------------------------------------------------------------------
# Étape 1 — PyInstaller
# ---------------------------------------------------------------------------


def build_onedir() -> Path:
    step("Compilation PyInstaller (--onedir)")

    out_dir = DIST_DIR / APP_NAME
    if out_dir.exists():
        info(f"Suppression du build précédent : {out_dir}")
        shutil.rmtree(out_dir)

    sep = ";" if sys.platform == "win32" else ":"

    # Modules Python à embarquer explicitement (imports dynamiques dans setup.py
    # ou dans launcher.py qui ne sont pas détectés automatiquement)
    hidden = [
        "PySide6.QtCore",
        "PySide6.QtWidgets",
        "PySide6.QtGui",
        "PySide6.QtSvg",
        "PySide6.QtOpenGL",
        "PySide6.QtOpenGLWidgets",
        "PySide6.QtDBus",
        "pymediainfo",
        "configparser",
        "tarfile",
        "zipfile",
        "urllib.request",
        "urllib.error",
    ]

    cmd: list[str | Path] = [
        sys.executable, "-m", "PyInstaller",
        "--name", APP_NAME,
        "--onedir",
        "--noconfirm",
        # Données non-Python à embarquer dans le bundle
        f"--add-data={ROOT / 'locales.json'}{sep}.",
        f"--add-data={ROOT / 'requirements.txt'}{sep}.",
        # collect-data uniquement : inclut les plugins Qt (plateforme, imageformats…)
        # sans aspirer les binaires QML Wayland/NFC qui causent des warnings
        "--collect-data=PySide6",
        "--collect-all=pymediainfo",
        "--collect-submodules=cli",
        # Exclusions — modules Python inutiles
        "--exclude-module=tkinter",
        "--exclude-module=matplotlib",
        "--exclude-module=numpy",
        "--exclude-module=scipy",
        "--exclude-module=PIL",
        "--exclude-module=test",
        "--exclude-module=unittest",
        # Exclusions — sous-modules PySide6 non utilisés par l'app
        # (évite les warnings "Library not found" pour Wayland compositor, NFC, etc.)
        "--exclude-module=PySide6.QtWaylandCompositor",
        "--exclude-module=PySide6.QtNfc",
        "--exclude-module=PySide6.QtBluetooth",
        "--exclude-module=PySide6.QtLocation",
        "--exclude-module=PySide6.QtPositioning",
        "--exclude-module=PySide6.QtRemoteObjects",
        "--exclude-module=PySide6.QtScxml",
        "--exclude-module=PySide6.QtSensors",
        "--exclude-module=PySide6.QtSerialPort",
        "--exclude-module=PySide6.QtTextToSpeech",
        "--exclude-module=PySide6.QtWebChannel",
        "--exclude-module=PySide6.QtWebEngineCore",
        "--exclude-module=PySide6.QtWebEngineWidgets",
        "--exclude-module=PySide6.QtWebSockets",
        "--exclude-module=PySide6.Qt3DCore",
        "--exclude-module=PySide6.Qt3DRender",
        "--exclude-module=PySide6.Qt3DInput",
        "--exclude-module=PySide6.Qt3DAnimation",
        "--exclude-module=PySide6.Qt3DLogic",
        "--exclude-module=PySide6.Qt3DExtras",
        "--exclude-module=PySide6.QtCharts",
        "--exclude-module=PySide6.QtDataVisualization",
        "--exclude-module=PySide6.QtVirtualKeyboard",
        # Répertoires de sortie
        f"--distpath={DIST_DIR}",
        f"--workpath={BUILD_DIR}",
        # Point d'entrée
        str(ROOT / "launcher.py"),
    ]

    for mod in hidden:
        cmd += ["--hidden-import", mod]

    run(cmd, cwd=ROOT)

    if not out_dir.exists():
        err(f"Build PyInstaller raté : {out_dir} introuvable")
        sys.exit(1)

    _ensure_linux_bundle_entrypoints(out_dir)
    ok(f"Bundle créé : {out_dir}")
    return out_dir


# ---------------------------------------------------------------------------
# Étape 2 — AppDir
# ---------------------------------------------------------------------------

def _convert_ico_to_png(src_ico: Path, dest_png: Path) -> bool:
    """Convertit un ICO en PNG via les plugins image Qt."""
    best_png = _extract_best_png_from_ico(src_ico)
    if best_png is not None:
        dest_png.parent.mkdir(parents=True, exist_ok=True)
        dest_png.write_bytes(best_png)
        return True

    try:
        from PySide6.QtGui import QImage
    except Exception:
        return False

    image = QImage(str(src_ico))
    if image.isNull():
        return False

    dest_png.parent.mkdir(parents=True, exist_ok=True)
    return bool(image.save(str(dest_png), b"PNG"))


def _extract_best_png_from_ico(src_ico: Path) -> bytes | None:
    """
    Retourne le plus grand PNG embarqué dans un ICO quand il existe.

    Sans ça, Qt charge souvent le premier frame (16x16) au lieu du 256x256.
    """
    data = src_ico.read_bytes()
    if len(data) < 6:
        return None

    reserved, icon_type, count = struct.unpack_from("<HHH", data, 0)
    if reserved != 0 or icon_type != 1 or count <= 0:
        return None

    best_payload: bytes | None = None
    best_area = -1
    for index in range(count):
        offset = 6 + index * 16
        if offset + 16 > len(data):
            break
        width, height, _colors, _reserved, _planes, _bpp, size, image_offset = struct.unpack_from(
            "<BBBBHHII",
            data,
            offset,
        )
        width = 256 if width == 0 else width
        height = 256 if height == 0 else height
        if image_offset + size > len(data):
            continue
        payload = data[image_offset:image_offset + size]
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            continue
        area = width * height
        if area > best_area:
            best_area = area
            best_payload = payload

    return best_payload


def _symlink_diricon(appdir: Path, icon_name: str) -> None:
    """Met à jour .DirIcon pour l'AppDir."""
    diricon = appdir / ".DirIcon"
    if diricon.exists() or diricon.is_symlink():
        diricon.unlink()
    diricon.symlink_to(icon_name)

_DESKTOP = textwrap.dedent("""\
    [Desktop Entry]
    Name=Muxiveo
    Comment=MKV/MP4 Workflow — DoVi · HDR10+ · Remux · Encode
    Exec=muxiveo %F
    Icon=Muxiveo
    Type=Application
    Categories=AudioVideo;Video;
    MimeType={mime_types}
    X-AppImage-Website={website_url}
    Terminal=false
""")

_APPSTREAM_METAINFO = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <component type="desktop-application">
      <id>{appstream_id}</id>
      <metadata_license>MIT</metadata_license>
      <project_license>GPL-3.0-or-later</project_license>
      <name>Muxiveo</name>
      <summary>MKV/MP4 workflow for DoVi, HDR10+, remux and encode</summary>
      <description>
        <p>Muxiveo helps prepare MKV and MP4 video workflows with remuxing, encoding, Dolby Vision and HDR10+ tooling.</p>
      </description>
      <launchable type="desktop-id">{desktop_id}</launchable>
      <url type="homepage">{website_url}</url>
    </component>
""")

# Icône SVG de secours (64×64) — remplacez par un PNG 256×256 dans le projet
_ICON_SVG = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
      <rect width="64" height="64" rx="12" fill="#141720"/>
      <text x="32" y="44" font-size="32" text-anchor="middle"
            font-family="monospace" fill="#4f6ef7">M</text>
    </svg>
""")

# AppRun standard
_APPRUN = textwrap.dedent("""\
    #!/bin/bash
    # AppRun — lanceur AppImage pour Muxiveo
    set -e

    HERE="$(dirname "$(readlink -f "$0")")"
    BIN="${HERE}/usr/bin"

    # PyInstaller 6 place les dépendances dans _internal/
    if [ -d "${BIN}/_internal" ]; then
        INTERNAL="${BIN}/_internal"
    else
        INTERNAL="${BIN}"
    fi

    export PATH="${BIN}:${PATH}"
    export LD_LIBRARY_PATH="${INTERNAL}:${LD_LIBRARY_PATH:-}"
    export QT_PLUGIN_PATH="${INTERNAL}/PySide6/Qt/plugins"
    export QML2_IMPORT_PATH="${INTERNAL}/PySide6/Qt/qml"
    export APPDIR="${HERE}"

    exec "${BIN}/muxiveo" "$@"
""")

# AppRun all-inclusive : les outils embarqués ont priorité sur les outils système
_APPRUN_ALLINC = textwrap.dedent("""\
    #!/bin/bash
    # AppRun — lanceur AppImage all-inclusive pour Muxiveo
    set -e

    HERE="$(dirname "$(readlink -f "$0")")"
    BIN="${HERE}/usr/bin"
    TOOLS="${BIN}/tools"

    # PyInstaller 6 place les dépendances dans _internal/
    if [ -d "${BIN}/_internal" ]; then
        INTERNAL="${BIN}/_internal"
    else
        INTERNAL="${BIN}"
    fi

    # Les outils embarqués ont la priorité sur les outils système
    export PATH="${TOOLS}:${BIN}:${PATH}"
    # NVEncC : ses libs ffmpeg accompagnantes sont bundlées séparément pour
    # ne pas écraser celles utilisées par PySide6 / l'app principale.
    # libcuda.so.1 reste fournie par le driver NVIDIA hôte (non bundlable).
    NVENCC_LIBS="${TOOLS}/lib_nvencc"
    export LD_LIBRARY_PATH="${NVENCC_LIBS}:${INTERNAL}:${LD_LIBRARY_PATH:-}"
    export QT_PLUGIN_PATH="${INTERNAL}/PySide6/Qt/plugins"
    export QML2_IMPORT_PATH="${INTERNAL}/PySide6/Qt/qml"
    export APPDIR="${HERE}"

    exec "${BIN}/muxiveo" "$@"
""")


# ---------------------------------------------------------------------------
# Téléchargement des outils externes (mode --allinc)
# ---------------------------------------------------------------------------

def _gh_latest_asset(repo: str, *patterns: str) -> str:
    """Retourne l'URL du premier asset GitHub dont le nom contient un des patterns."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Muxiveo-builder",
    }
    # Les builds CI peuvent dépasser rapidement la limite anonyme de l'API
    # GitHub en téléchargeant les outils embarqués. Un token est facultatif
    # pour conserver le script utilisable hors GitHub Actions.
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url,
        headers=headers,
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    for asset in data["assets"]:
        name: str = asset["name"]
        if any(p in name for p in patterns):
            return asset["browser_download_url"]
    raise RuntimeError(f"Aucun asset trouvé pour {patterns} dans {repo} (assets: {[a['name'] for a in data['assets']]})")


def _download(url: str, dest: Path, timeout: int = 30) -> None:
    """Télécharge url vers dest avec timeout, progress et reprise sur erreur."""
    info(f"Téléchargement : {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "Muxiveo-builder"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk = 65536
            with open(dest, "wb") as f:
                while True:
                    buf = resp.read(chunk)
                    if not buf:
                        break
                    f.write(buf)
                    downloaded += len(buf)
                    if total:
                        pct = downloaded * 100 // total
                        mb = downloaded / 1_048_576
                        print(f"\r    {pct:3d}%  {mb:.1f} Mo", end="", flush=True)
            print()  # newline après la barre
    except TimeoutError as e:
        raise RuntimeError(
            f"Timeout ({timeout}s) lors du téléchargement de {url}\n"
            f"Vérifiez votre connexion ou la disponibilité du serveur."
        ) from e


def _chmod_x(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _extract_from_tar(archive: Path, binary_names: list[str], dest_dir: Path) -> None:
    """Extrait des binaires nommés depuis une archive tar (gz/bz2/xz)."""
    with tarfile.open(archive) as tf:
        for member in tf.getmembers():
            bname = Path(member.name).name
            if bname in binary_names and member.isfile():
                member.name = bname  # aplatit le chemin
                tf.extract(member, dest_dir)
                _chmod_x(dest_dir / bname)
                binary_names = [b for b in binary_names if b != bname]
    if binary_names:
        raise RuntimeError(f"Binaires introuvables dans l'archive : {binary_names}")


def _dl_ffmpeg(tools_dir: Path, arch: str) -> None:
    """
    Télécharge ffmpeg/ffprobe depuis BtbN/FFmpeg-Builds (master, GPL static).

    Ces builds incluent NVENC, VAAPI, QSV, AMF, libsvtav1.
    Ce sont des binaires statiques — aucune lib externe à embarquer.
    URL directe : github.com/BtbN/FFmpeg-Builds/releases/download/latest/
                  ffmpeg-master-latest-linux64-gpl.tar.xz
    """
    step("Téléchargement ffmpeg + ffprobe (BtbN/FFmpeg-Builds, master GPL static)")

    _arch_tag = {"x86_64": "linux64", "aarch64": "linuxarm64"}.get(arch, f"linux{arch}")
    filename = f"ffmpeg-master-latest-{_arch_tag}-gpl.tar.xz"
    url = f"https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/{filename}"
    info(f"Asset : {filename}")

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "ffmpeg.tar.xz"
        _download(url, archive, timeout=120)
        _extract_from_tar(archive, ["ffmpeg", "ffprobe"], tools_dir)

    ok("ffmpeg + ffprobe (BtbN master GPL) installés")


def _mediainfo_latest_version() -> str:
    """Retourne la dernière version de mediainfo en scrapant le répertoire mediaarea.net."""
    import re
    base = "https://mediaarea.net/download/binary/mediainfo/"
    req = urllib.request.Request(base, headers={"User-Agent": "Muxiveo-builder"})
    with urllib.request.urlopen(req) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    versions = re.findall(r'href="(\d{2}\.\d{2})/"', html)
    if not versions:
        raise RuntimeError(f"Aucune version mediainfo trouvée sur {base}")
    versions.sort(key=lambda v: tuple(int(x) for x in v.split(".")), reverse=True)
    return versions[0]


def _dl_mediainfo(tools_dir: Path, arch: str) -> None:
    """
    Télécharge le CLI mediainfo depuis mediaarea.net.
    MediaArea ne fournit pas de binaire Linux natif ; on utilise le build
    Lambda (statiquement lié, compatible Linux x86_64 / arm64).
    """
    step("Téléchargement mediainfo CLI (mediaarea.net — Lambda build)")
    ver = _mediainfo_latest_version()
    info(f"mediainfo version : {ver}")
    # Lambda_x86_64 / Lambda_arm64  — binaires statiques, fonctionnent hors AWS
    _lambda_arch = {"x86_64": "x86_64", "aarch64": "arm64"}.get(arch, arch)
    base = "https://mediaarea.net/download/binary/mediainfo/"
    url = f"{base}{ver}/MediaInfo_CLI_{ver}_Lambda_{_lambda_arch}.zip"
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "mediainfo.zip"
        _download(url, archive)
        with zipfile.ZipFile(archive) as zf:
            # Le zip contient directement "mediainfo" (ou dans un sous-dossier)
            candidates = [n for n in zf.namelist() if Path(n).name == "mediainfo"]
            if not candidates:
                raise RuntimeError(
                    f"Binaire 'mediainfo' introuvable dans l'archive. "
                    f"Contenu : {zf.namelist()}"
                )
            data = zf.read(candidates[0])
            dest = tools_dir / "mediainfo"
            dest.write_bytes(data)
            _chmod_x(dest)
    ok("mediainfo installé")


def _dl_dovi_tool(tools_dir: Path, arch: str) -> None:
    step("Téléchargement dovi_tool (GitHub)")
    _sfx = {"x86_64": "x86_64-unknown-linux-musl", "aarch64": "aarch64-unknown-linux-musl"}.get(arch, arch)
    url = _gh_latest_asset("quietvoid/dovi_tool", _sfx)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "dovi_tool.tar.gz"
        _download(url, archive)
        _extract_from_tar(archive, ["dovi_tool"], tools_dir)
    ok("dovi_tool installé")


def _dl_hdr10plus_tool(tools_dir: Path, arch: str) -> None:
    step("Téléchargement hdr10plus_tool (GitHub)")
    _sfx = {"x86_64": "x86_64-unknown-linux-musl", "aarch64": "aarch64-unknown-linux-musl"}.get(arch, arch)
    url = _gh_latest_asset("quietvoid/hdr10plus_tool", _sfx)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "hdr10plus_tool.tar.gz"
        _download(url, archive)
        _extract_from_tar(archive, ["hdr10plus_tool"], tools_dir)
    ok("hdr10plus_tool installé")


def _dl_nvencc(tools_dir: Path, arch: str) -> None:
    """Télécharge NVEncC (rigaya) — uniquement x86_64 Linux.

    Note importante : NVEncC dépend de ``libcuda.so.1`` qui est fourni par le
    driver NVIDIA hôte (non redistribuable). L'AppImage ne bundle PAS libcuda.
    L'utilisateur doit avoir un driver NVIDIA ≥ 452.39 sur sa machine.

    On utilise le .deb plutôt que le .rpm car ``dpkg-deb -x`` est plus
    largement disponible (binutils ``ar`` partout). Le .deb contient
    également des libs ffmpeg statiques qu'on bundle pour résoudre les deps
    runtime sans contaminer le système hôte.
    """
    if arch != "x86_64":
        warn("NVEncC : pas de build aarch64 disponible chez rigaya — skip.")
        return
    step("Téléchargement NVEncC (rigaya/NVEnc)")
    url = _gh_latest_asset("rigaya/NVEnc", "_amd64.deb")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / "nvencc.deb"
        _download(url, archive)
        # Extraction via dpkg-deb -x (présent dans la plupart des images de build).
        dpkg_deb = shutil.which("dpkg-deb")
        if dpkg_deb is None:
            warn("dpkg-deb introuvable — NVEncC ignoré (installer 'dpkg' dans la build image).")
            return
        extract_dir = tmp_path / "nvencc_extracted"
        extract_dir.mkdir()
        subprocess.run([dpkg_deb, "-x", str(archive), str(extract_dir)], check=True)
        # Copie binaire (le .deb installe sous /usr/bin/NVEncC selon la version).
        binary_candidates = [
            extract_dir / "usr" / "bin" / "NVEncC",
            extract_dir / "usr" / "bin" / "nvencc",
        ]
        binary = next((p for p in binary_candidates if p.is_file()), None)
        if binary is None:
            # Recherche tolérante.
            found = next(
                (p for p in extract_dir.rglob("*") if p.is_file()
                 and p.name.lower() == "nvencc"),
                None,
            )
            binary = found
        if binary is None:
            warn("Binaire NVEncC introuvable dans le .deb — skip.")
            return
        shutil.copy2(binary, tools_dir / "nvencc")
        _chmod_x(tools_dir / "nvencc")
        # Symlink PascalCase pour compat avec la résolution Linux .deb-style.
        try:
            (tools_dir / "NVEncC").symlink_to("nvencc")
        except FileExistsError:
            pass
        # Bundle les libs accompagnantes embarquées dans le .deb (libavcodec etc.)
        # pour ne pas dépendre d'un ffmpeg système avec des versions différentes.
        # libcuda reste hôte-only — non bundlable légalement.
        bundled_libs = extract_dir / "usr" / "lib" / "x86_64-linux-gnu"
        if bundled_libs.is_dir():
            lib_dest = tools_dir / "lib_nvencc"
            lib_dest.mkdir(exist_ok=True)
            for lib in bundled_libs.glob("*.so*"):
                shutil.copy2(lib, lib_dest / lib.name)
            info(f"NVEncC libs bundled : {len(list(lib_dest.iterdir()))} fichier(s) .so*")
    ok("NVEncC embarqué (driver NVIDIA hôte requis pour l'exécution)")


def _bundle_licenses(appdir: Path) -> None:
    """Copie NOTICE, SOURCES.md et LICENSES/ dans l'AppImage (conformité licences)."""
    dest = appdir / "usr" / "share" / "licenses" / "muxiveo"
    dest.mkdir(parents=True, exist_ok=True)

    for fname in ("NOTICE", "SOURCES.md"):
        src = ROOT / fname
        if src.is_file():
            shutil.copy2(src, dest / fname)

    licenses_src = ROOT / "LICENSES"
    if licenses_src.is_dir():
        licenses_dest = dest / "LICENSES"
        if licenses_dest.exists():
            shutil.rmtree(licenses_dest)
        shutil.copytree(licenses_src, licenses_dest)

    ok(f"Fichiers de licence embarqués dans {dest}")


def bundle_tools(appdir: Path, arch: str) -> None:
    """Télécharge tous les outils externes et les place dans usr/bin/tools/."""
    step("Téléchargement des outils externes (mode all-inclusive)")
    tools_dir = appdir / "usr" / "bin" / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)

    _dl_ffmpeg(tools_dir, arch)
    _dl_mediainfo(tools_dir, arch)
    _dl_dovi_tool(tools_dir, arch)
    _dl_hdr10plus_tool(tools_dir, arch)
    _dl_nvencc(tools_dir, arch)
    _bundle_licenses(appdir)

    ok(f"Tous les outils embarqués dans {tools_dir}")


# ---------------------------------------------------------------------------
# Étape 2 — AppDir
# ---------------------------------------------------------------------------

def _clean_appdir(path: Path) -> None:
    """Supprime le répertoire AppDir, même si les fichiers appartiennent à un autre uid."""
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
        return
    except PermissionError:
        pass
    # Tentative via rm système (peut échouer en sandbox sans sudo)
    result = subprocess.run(["rm", "-rf", str(path)], check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"Impossible de supprimer {path} (fichiers appartenant à un autre uid).\n"
            "Supprimez-le manuellement depuis le distrobox :\n"
            f"  rm -rf {path}"
        )


def build_appdir(bundle_dir: Path, allinc: bool = False, arch: str = "x86_64") -> Path:
    step("Construction de l'AppDir")

    appdir = APPDIR
    if appdir.exists():
        try:
            _clean_appdir(appdir)
        except RuntimeError as exc:
            # Dossier non suppressible (permissions uid différent) → utilise un répertoire temporaire
            import tempfile
            appdir = Path(tempfile.mkdtemp(prefix="Muxiveo.AppDir.", dir=ROOT))
            info(f"AppDir alternatif utilisé : {appdir}  ({exc})")
    if not appdir.exists():
        appdir.mkdir()

    # usr/bin/ ← contenu du bundle PyInstaller
    usr_bin = appdir / "usr" / "bin"
    usr_bin.mkdir(parents=True)
    info(f"Copie du bundle → {usr_bin} …")
    shutil.copytree(bundle_dir, usr_bin, dirs_exist_ok=True)
    ok("Bundle copié")
    _ensure_linux_bundle_entrypoints(usr_bin)
    ok("Entrée Unix AppImage créée")

    # Marqueur all-inclusive lu par launcher.py au démarrage
    if allinc:
        (usr_bin / "_ALLINC").touch()
        bundle_tools(appdir, arch)

    # AppRun
    apprun = appdir / "AppRun"
    apprun.write_text(_APPRUN_ALLINC if allinc else _APPRUN)
    apprun.chmod(apprun.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    ok("AppRun créé")

    # .desktop
    desktop = appdir / f"{APP_NAME}.desktop"
    desktop.write_text(
        _DESKTOP.format(
            mime_types=build_desktop_mime_type_string(),
            website_url=_APPIMAGE_WEBSITE_URL,
        )
    )
    ok(".desktop créé")

    metainfo_dir = appdir / "usr" / "share" / "metainfo"
    metainfo_dir.mkdir(parents=True, exist_ok=True)
    metainfo = metainfo_dir / f"{_APPSTREAM_ID}.metainfo.xml"
    metainfo.write_text(
        _APPSTREAM_METAINFO.format(
            appstream_id=_APPSTREAM_ID,
            desktop_id=desktop.name,
            website_url=_APPIMAGE_WEBSITE_URL,
        )
    )
    ok("Métadonnées AppStream créées")

    # Icône — embarque icon.ico si présent et l'utilise en priorité pour
    # générer le PNG attendu par l'AppDir.
    project_icon_ico = ROOT / "icon.ico"
    if project_icon_ico.exists():
        shutil.copy(project_icon_ico, appdir / f"{APP_NAME}.ico")
        ok(f"Icône ICO copiée : {project_icon_ico.name}")

    # Icône — cherche d'abord un PNG/SVG/ICO dans le projet
    icon_src: Path | None = None
    for candidate in [
        ROOT / "icon.ico",
        ROOT / "icon.png",
        ROOT / f"{APP_NAME}.png",
        ROOT / "assets" / "icon.png",
        ROOT / "assets" / f"{APP_NAME}.png",
    ]:
        if candidate.exists():
            icon_src = candidate
            break

    if icon_src and icon_src.suffix.lower() == ".ico":
        dest_icon = appdir / f"{APP_NAME}.png"
        if _convert_ico_to_png(icon_src, dest_icon):
            _symlink_diricon(appdir, dest_icon.name)
            ok(f"Icône convertie depuis {icon_src.name}")
        else:
            svg_path = appdir / f"{APP_NAME}.svg"
            svg_path.write_text(_ICON_SVG)
            _symlink_diricon(appdir, svg_path.name)
            info("Conversion icon.ico impossible — icône SVG de secours utilisée")
    elif icon_src:
        dest_icon = appdir / f"{APP_NAME}{icon_src.suffix}"
        shutil.copy(icon_src, dest_icon)
        _symlink_diricon(appdir, dest_icon.name)
        ok(f"Icône copiée : {icon_src.name}")
    else:
        svg_path = appdir / f"{APP_NAME}.svg"
        svg_path.write_text(_ICON_SVG)
        _symlink_diricon(appdir, svg_path.name)
        info("Icône SVG de secours utilisée (ajoutez icon.png 256×256 à la racine du projet)")

    ok(f"AppDir prêt : {appdir}")
    return appdir


# ---------------------------------------------------------------------------
# Étape 3 — appimagetool
# ---------------------------------------------------------------------------

_APPIMAGETOOL_URLS: dict[str, str] = {
    "x86_64":  "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage",
    "aarch64": "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-aarch64.AppImage",
}


def get_appimagetool(arch: str) -> Path:
    step("Vérification de appimagetool")

    found = shutil.which("appimagetool")
    if found:
        ok(f"appimagetool trouvé sur le PATH : {found}")
        return Path(found)

    dest = ROOT / f"appimagetool-{arch}.AppImage"
    if dest.exists():
        ok(f"appimagetool déjà téléchargé : {dest.name}")
    else:
        url = _APPIMAGETOOL_URLS.get(arch)
        if not url:
            err(f"Architecture non supportée par appimagetool : {arch}")
            sys.exit(1)
        info(f"Téléchargement de appimagetool ({arch})…")
        urllib.request.urlretrieve(url, dest)
        ok(f"Téléchargé : {dest.name}")

    dest.chmod(dest.stat().st_mode | stat.S_IEXEC)
    return dest


# ---------------------------------------------------------------------------
# Étape 4 — AppImage finale
# ---------------------------------------------------------------------------

def build_appimage(
    appimagetool: Path,
    appdir: Path,
    arch: str,
    allinc: bool = False,
    version_tag: str | None = None,
) -> Path:
    step("Création de l'AppImage")

    suffix = "_allinc" if allinc else ""
    output = _versioned_output_path(ROOT / f"{APP_DISPLAY_NAME}-{arch}{suffix}.AppImage", version_tag)
    if output.exists():
        output.unlink()

    env = os.environ.copy()
    env["ARCH"] = arch
    update_information = _appimage_update_information(
        arch,
        allinc=allinc,
        version_tag=version_tag,
    )
    env["UPDATE_INFORMATION"] = update_information
    info(f"UPDATE_INFORMATION: {update_information}")
    # appimagetool est lui-même une AppImage : sans FUSE (distrobox, CI…)
    # il faut lui demander de s'extraire dans un dossier tmp plutôt que
    # de se monter via FUSE.
    env["APPIMAGE_EXTRACT_AND_RUN"] = "1"

    run(
        [
            appimagetool,
            "-u", update_information,
            str(appdir),
            str(output),
        ],
        env=env,
    )

    output.chmod(output.stat().st_mode | stat.S_IEXEC)
    ok(f"AppImage créée : {output.name}")
    return output


def _appimage_update_information(
    arch: str,
    allinc: bool = False,
    version_tag: str | None = None,
) -> str:
    """
    Chaîne UPDATE_INFORMATION AppImage pour GitHub Releases.
    Format: gh-releases-zsync|OWNER|REPO|RELEASE|FILENAME.zsync

    En mode "reuse" (tag de version `latest` ou `latest-*`), on embarque
    directement la release cible et le nom exact du .zsync pour éviter toute
    configuration manuelle dans Gear Lever.
    """
    suffix = "_allinc" if allinc else ""
    normalized_tag = _normalize_version_tag(version_tag)
    reuse_channel = normalized_tag == "latest" or normalized_tag.startswith("latest-")
    release = normalized_tag if reuse_channel and _APPIMAGE_UPDATE_RELEASE == "latest" else _APPIMAGE_UPDATE_RELEASE
    if reuse_channel:
        filename = f"{APP_DISPLAY_NAME}-{arch}{suffix}-{normalized_tag}.AppImage.zsync"
    else:
        filename = f"{APP_DISPLAY_NAME}-{arch}{suffix}-*.AppImage.zsync"
    return (
        "gh-releases-zsync|"
        f"{_APPIMAGE_UPDATE_OWNER}|"
        f"{_APPIMAGE_UPDATE_REPO}|"
        f"{release}|"
        f"{filename}"
    )


# ---------------------------------------------------------------------------
# Étape 5 — Fichier .zsync (mise à jour automatique AppImage)
# ---------------------------------------------------------------------------

def generate_zsync(appimage_path: Path, zsyncmake: Path) -> Path:
    """
    Génère le fichier .zsync à côté de l'AppImage.

    Le .zsync est requis pour AppImageUpdate / appimaged.
    Il doit être uploadé sur GitHub Releases avec l'AppImage.

    L'URL embarquée dans le .zsync pointe vers l'AppImage finale
    sur GitHub Releases (gh-releases-zsync attend ce format).
    La valeur réelle de l'URL n'est pas critique pour zsyncmake ;
    AppImageUpdate utilise UPDATE_INFORMATION (déjà intégrée dans l'AppImage)
    pour résoudre le .zsync — on passe le nom de fichier uniquement.
    """
    step("Génération du fichier .zsync (mise à jour automatique)")
    zsync_path = appimage_path.with_suffix(".AppImage.zsync")
    if zsync_path.exists():
        zsync_path.unlink()

    # zsyncmake doit tourner depuis le dossier contenant l'AppImage pour que
    # le champ Filename du .zsync ne contienne que le nom sans chemin absolu.
    run(
        [zsyncmake, "-C", "-u", appimage_path.name, "-o", zsync_path.name, appimage_path.name],
        cwd=appimage_path.parent,
    )

    ok(f".zsync généré : {zsync_path.name}")
    info("Uploadez ces deux fichiers sur GitHub Releases :")
    info(f"  • {appimage_path.name}")
    info(f"  • {zsync_path.name}")
    return zsync_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--allinc",
        action="store_true",
        help=(
            "Embarque ffmpeg, mediainfo, dovi_tool et hdr10plus_tool "
            "dans l'AppImage. Produit Muxiveo-<arch>_allinc.AppImage. "
            "Au premier lancement, seule la configuration est initialisée."
        ),
    )
    p.add_argument(
        "--skip-pyinstaller",
        action="store_true",
        help="Réutilise le bundle PyInstaller existant dans dist/Muxiveo/",
    )
    p.add_argument(
        "--arch",
        default=platform.machine(),
        choices=list(_APPIMAGETOOL_URLS),
        help="Architecture cible (défaut : machine courante)",
    )
    p.add_argument(
        "--dest",
        metavar="PATH",
        help=(
            "Chemin de copie du fichier final (dossier ou nom de fichier). "
            "Si le nom de fichier est omis, le nom auto-généré est conservé."
        ),
    )
    p.add_argument(
        "--version",
        metavar="TAG",
        default=APP_VERSION,
        help=(
            "Tag version à suffixer dans le nom du fichier final "
            f"(défaut: {APP_VERSION})."
        ),
    )
    return p.parse_args()


def main() -> None:
    if sys.platform != "linux":
        err("Ce script ne fonctionne que sur Linux.")
        sys.exit(1)

    args = parse_args()
    arch = args.arch
    allinc = args.allinc
    version_tag = args.version

    print(_c("1;34", """
╔══════════════════════════════════════════╗
║      Muxiveo — AppImage Builder      ║
╚══════════════════════════════════════════╝
"""))
    info(f"Architecture  : {arch}")
    info(f"Mode          : {'all-inclusive (outils embarqués)' if allinc else 'standard (outils installés au 1er lancement)'}")
    info(f"Racine projet : {ROOT}")

    ensure_build_deps()
    zsyncmake = ensure_zsyncmake() if allinc else None

    if args.skip_pyinstaller:
        bundle_dir = DIST_DIR / APP_NAME
        if not bundle_dir.exists():
            err(f"--skip-pyinstaller : bundle introuvable dans {bundle_dir}")
            sys.exit(1)
        info(f"Bundle existant réutilisé : {bundle_dir}")
    else:
        bundle_dir = build_onedir()

    appdir = build_appdir(bundle_dir, allinc=allinc, arch=arch)
    appimagetool = get_appimagetool(arch)
    appimage_path = build_appimage(
        appimagetool,
        appdir,
        arch,
        allinc=allinc,
        version_tag=version_tag,
    )
    final_appimage = _copy_final_file_if_requested(appimage_path, args.dest, version_tag=version_tag)

    zsync_path: Path | None = None
    if allinc and zsyncmake is not None:
        zsync_path = generate_zsync(final_appimage, zsyncmake)

    print(_c("1;32", """
╔══════════════════════════════════════════╗
║  Build terminé avec succès !             ║
╚══════════════════════════════════════════╝"""))
    print(f"\n  AppImage : {final_appimage}")
    if zsync_path:
        print(f"  .zsync   : {zsync_path}")
    info("Lancez l'application avec :")
    print(f"\n    chmod +x \"{final_appimage}\"")
    print(f"    \"{final_appimage}\"\n")
    if allinc:
        info("Mode all-inclusive : au 1er lancement, seule la configuration")
        info("est initialisée (~/.config/muxiveo/config.ini).")
        if zsync_path:
            info("Pour activer les mises à jour automatiques, uploadez sur GitHub Releases :")
            info(f"  • {final_appimage.name}")
            info(f"  • {zsync_path.name}")
        else:
            info("zsyncmake absent — mises à jour automatiques désactivées (upload .zsync manquant).")
    else:
        info("Au 1er lancement, le setup s'exécute si")
        info("~/.config/muxiveo/config.ini est absent.")


if __name__ == "__main__":
    main()
