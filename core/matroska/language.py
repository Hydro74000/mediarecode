"""Pure language normalization rules used by Matroska services."""

from __future__ import annotations

from core.lang_tags import Rfc5646LanguageTags as LangTags


_ISO639_2_T_TO_B: dict[str, str] = {
    "sqi": "alb", "hye": "arm", "eus": "baq", "zho": "chi",
    "ces": "cze", "nld": "dut", "fra": "fre", "kat": "geo",
    "deu": "ger", "ell": "gre", "isl": "ice", "mkd": "mac",
    "msa": "may", "fas": "per", "ron": "rum", "slk": "slo",
    "cym": "wel",
}


def iso639_2_bibliographic(tag: str) -> str | None:
    """Return the ISO 639-2/B code for a recognized language tag."""
    iso_t = LangTags.to_iso639_2(str(tag or ""))
    if iso_t is None:
        return None
    return _ISO639_2_T_TO_B.get(iso_t, iso_t)


def matroska_legacy_language(tag: str) -> str:
    """Return the ISO 639-2/B value used by Matroska ``Language``."""
    return iso639_2_bibliographic(tag) or "und"


def bcp47_for_language(tag: str) -> str:
    """BCP-47 canonique pour une valeur de langue (IETF ou ISO 639-2).

    Retourne une chaîne vide si la valeur n'est pas reconnue — l'appelant
    omet alors l'élément plutôt que d'écrire une balise invalide.
    """
    cleaned = str(tag or "").strip()
    if not cleaned:
        return ""
    normalized = LangTags.normalize(cleaned) or LangTags.from_iso639_2(cleaned)
    return canonicalize_bcp47(normalized) if normalized else ""


def canonicalize_bcp47(tag: str) -> str:
    """Normalize the casing of a BCP-47 tag without validating its registry."""
    parts = [part for part in tag.strip().split("-") if part]
    if not parts:
        return tag

    normalized: list[str] = []
    seen_primary = False
    private_use = False
    for part in parts:
        if private_use:
            normalized.append(part.lower())
            continue
        if part.lower() == "x":
            normalized.append("x")
            private_use = True
            continue
        if not seen_primary:
            normalized.append(part.lower())
            seen_primary = True
            continue
        if len(part) == 4 and part.isalpha():
            normalized.append(part.title())
        elif len(part) == 2 and part.isalpha():
            normalized.append(part.upper())
        elif len(part) == 3 and part.isdigit():
            normalized.append(part)
        else:
            normalized.append(part.lower())
    return "-".join(normalized)


__all__ = [
    "bcp47_for_language",
    "canonicalize_bcp47",
    "iso639_2_bibliographic",
    "matroska_legacy_language",
]
