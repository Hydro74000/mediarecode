"""
core/workdir.py — Helpers pour gérer le répertoire de travail applicatif.
"""

from __future__ import annotations

import re
import shutil
import ssl
import urllib.error
import urllib.request
from pathlib import Path


def _urlopen_image(req: urllib.request.Request, timeout: int = 30):
    """urlopen avec fallback SSL non-vérifié si la vérification échoue."""
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.URLError as exc:
        cur: BaseException | None = exc
        is_ssl = False
        while cur is not None:
            if isinstance(cur, ssl.SSLError):
                is_ssl = True
                break
            if isinstance(getattr(cur, "reason", None), ssl.SSLError):
                is_ssl = True
                break
            cur = cur.__cause__ or cur.__context__
        if not is_ssl:
            raise
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return urllib.request.urlopen(req, timeout=timeout, context=ctx)


_PROCESS_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_process_folder_name(name: str, fallback: str = "job") -> str:
    """Normalise un nom de dossier process à partir d'un texte libre."""
    raw = (name or "").strip()
    normalized = _PROCESS_NAME_RE.sub("_", raw).strip("._-")
    return normalized or fallback


def process_folder_name_from_output(output_path: Path, fallback: str = "job") -> str:
    """
    Retourne un nom de dossier process dérivé du fichier de sortie.

    Ex: /videos/Mon.Film.mkv -> "Mon.Film"
    """
    stem = output_path.stem if output_path else ""
    return sanitize_process_folder_name(stem, fallback=fallback)


def ensure_work_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def work_dir_entries(path: Path) -> list[Path]:
    """Retourne les entrées de premier niveau dans le work_dir."""
    if not path.exists() or not path.is_dir():
        return []
    return sorted(
        [p for p in path.iterdir() if not _is_ignorable_work_dir_entry(p)],
        key=lambda p: p.name.lower(),
    )


def work_dir_has_entries(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    return any(not _is_ignorable_work_dir_entry(p) for p in path.iterdir())


def clear_work_dir(path: Path) -> None:
    """Supprime le contenu d'un work_dir sans supprimer le dossier racine."""
    ensure_work_dir(path)
    for entry in list(path.iterdir()):
        remove_path(entry)


def remove_path(path: Path) -> None:
    """Supprime un fichier ou dossier, en tolérant les cas limites de FS."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # Peut être un lien cassé/objet non standard : fallback rmtree.
        shutil.rmtree(path, ignore_errors=True)


def _is_ignorable_work_dir_entry(entry: Path) -> bool:
    """
    Retourne True pour une entrée à ignorer dans le check de démarrage.

    Règle métier :
      - `tmdb_covers` est ignoré s'il ne contient aucun fichier utile.
    """
    if entry.name != "tmdb_covers" or not entry.is_dir():
        return False
    return not _dir_has_payload_files(entry)


def _dir_has_payload_files(directory: Path) -> bool:
    """True si le dossier contient au moins un fichier/symlink (récursif)."""
    try:
        for child in directory.rglob("*"):
            if child.is_file() or child.is_symlink():
                return True
    except OSError:
        # En cas de problème de lecture, on préfère considérer le dossier non vide.
        return True
    return False


def prepare_process_work_dir(
    work_root: Path,
    *,
    output_path: Path | None = None,
    process_name: str | None = None,
    fallback_name: str = "job",
) -> Path:
    """
    Prépare le dossier dédié au process courant.

    - Crée `work_root` si absent.
    - Dérive un nom depuis `process_name` ou `output_path`.
    - Si le dossier process existe déjà, le vide avant usage.
    """
    root = ensure_work_dir(work_root)
    if process_name is not None:
        folder_name = sanitize_process_folder_name(process_name, fallback=fallback_name)
    elif output_path is not None:
        folder_name = process_folder_name_from_output(output_path, fallback=fallback_name)
    else:
        folder_name = fallback_name

    process_dir = root / folder_name
    if process_dir.exists():
        clear_work_dir(process_dir)
    else:
        process_dir.mkdir(parents=True, exist_ok=True)
    return process_dir


def normalized_tmdb_cover_filename(filename: str) -> str:
    """Retourne le nom de fichier TMDB effectivement écrit sur disque."""
    raw = (filename or "").strip()
    name = Path(raw).name if raw else ""
    return name if name not in {"", ".", ".."} else "cover.jpg"


def download_tmdb_cover(url: str, filename: str, target_dir: Path) -> Path:
    """
    Télécharge une cover depuis l'URL TMDB et la place dans target_dir.

    Retourne le chemin du fichier téléchargé.
    Lève une OSError ou urllib.error.URLError en cas d'échec réseau/disque.
    """
    from core.version import APP_USER_AGENT

    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / normalized_tmdb_cover_filename(filename)
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "image/*,*/*;q=0.8",
            "User-Agent": APP_USER_AGENT,
        },
    )
    with _urlopen_image(req, timeout=30) as resp:
        dest.write_bytes(resp.read())
    return dest


def relocate_tmdb_covers_to_process_dir(
    attachments: list[Path],
    *,
    work_root: Path,
    process_dir: Path,
) -> list[Path]:
    """
    Déplace les covers TMDB (situées sous `<work_root>/tmdb_covers`) dans `process_dir`.

    Les chemins retournés sont ceux à utiliser par le workflow.
    Les fichiers déplacés sont supprimés de `tmdb_covers` (move/rename).
    """
    tmdb_root = work_root / "tmdb_covers"
    destination_root = process_dir / "attachments"
    destination_root.mkdir(parents=True, exist_ok=True)

    relocated: list[Path] = []
    for path in attachments:
        src = Path(path)
        if not _is_path_under(src, tmdb_root):
            relocated.append(src)
            continue
        if not src.exists():
            relocated.append(src)
            continue
        dest = _unique_destination(destination_root, src.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        src.replace(dest)
        _prune_empty_parents(src.parent, stop_at=tmdb_root)
        relocated.append(dest)
    return relocated


def _is_path_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def _unique_destination(directory: Path, filename: str) -> Path:
    base = directory / filename
    if not base.exists():
        return base
    stem = base.stem
    suffix = base.suffix
    idx = 1
    while True:
        candidate = directory / f"{stem}_{idx}{suffix}"
        if not candidate.exists():
            return candidate
        idx += 1


def _prune_empty_parents(start: Path, *, stop_at: Path) -> None:
    current = start
    stop = stop_at.resolve()
    while True:
        try:
            cur_resolved = current.resolve()
        except OSError:
            break
        if cur_resolved == stop:
            break
        try:
            current.rmdir()
        except OSError:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
