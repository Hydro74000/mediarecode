from __future__ import annotations

from pathlib import Path

ATTACHMENT_MIME_BY_EXT: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".ttf": "application/x-truetype-font",
    ".otf": "font/otf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".txt": "text/plain",
    ".xml": "application/xml",
}

ATTACHMENT_EXT_BY_MIME: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
    "image/tiff": ".tiff",
    "application/x-truetype-font": ".ttf",
    "font/ttf": ".ttf",
    "font/otf": ".otf",
    "font/woff": ".woff",
    "font/woff2": ".woff2",
    "text/plain": ".txt",
    "application/xml": ".xml",
}


def mime_for_path(path: Path) -> str:
    return ATTACHMENT_MIME_BY_EXT.get(path.suffix.lower(), "application/octet-stream")


def extension_for_mime(mime: str, default: str = ".bin") -> str:
    return ATTACHMENT_EXT_BY_MIME.get((mime or "").strip().lower(), default)


def sanitize_filename(name: str, fallback: str) -> str:
    clean = Path((name or "").strip()).name
    return clean or fallback


def canonical_attachment_output_name(path: Path) -> str:
    """Nom de sortie d'un attachment fichier — délègue à la règle commune.

    Implémentation unique dans :mod:`core.matroska.assembly` : commandes de
    muxage, assembleur natif et contrats doivent produire le même nom.
    """
    from core.matroska.assembly import (
        canonical_attachment_output_name as _canonical_name,
    )

    return _canonical_name(path)


def attachment_filename_from_meta(meta: dict[str, object], stream_idx: int) -> str:
    raw_name = str(meta.get("filename") or "").strip()
    name = Path(raw_name).name if raw_name else f"attachment_{stream_idx}"
    if Path(name).suffix:
        return name
    mime = str(meta.get("mimetype") or "").lower()
    suffix = extension_for_mime(mime)
    return f"{name}{suffix}"
