"""
core/matroska/editors/language.py

Post-action EBML qui corrige les champs Language / LanguageBCP47 des
TrackEntry dans un MKV.

ffmpeg écrit la valeur passée via ``-metadata:s:x:N language=xx-XX`` dans
l'élément Matroska ``Language`` (0x22B59C), qui est spécifié en ISO 639-2
(3 lettres). Lorsque la valeur est un tag BCP-47 (``fr-FR``), le champ
devient invalide et ``LanguageBCP47`` (0x22B59D) n'est pas émis.

Ce module, après mux ffmpeg :
- détecte les TrackEntry dont ``Language`` contient un BCP-47,
- réécrit ``Language`` avec le code ISO 639-2/B correspondant (``fre``),
- ajoute ``LanguageBCP47`` avec la valeur BCP-47 d'origine.

Le patch passe par ``MatroskaSegmentInfoHeaderEditor.replace_level1_element``
pour réécrire l'élément Tracks entier avec gestion des Void/SeekHead.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..language import canonicalize_bcp47, iso639_2_bibliographic
from .segment_info import (
    MatroskaSegmentInfoHeaderEditor,
    MatroskaSegmentInfoHeaderEditorOptions,
)


_TRACKS_ID = b"\x16\x54\xae\x6b"
_TRACK_ENTRY_ID = b"\xae"
_LANGUAGE_ID = b"\x22\xb5\x9c"
_LANGUAGE_BCP47_ID = b"\x22\xb5\x9d"


@dataclass(frozen=True)
class TrackLanguageFix:
    track_entry_offset: int
    language_before: str | None
    language_after: str
    language_bcp47_before: str | None
    language_bcp47_after: str


@dataclass(frozen=True)
class MatroskaLanguagePatchResult:
    applied: bool
    skipped: bool
    reason: str = ""
    fixes: tuple[TrackLanguageFix, ...] = ()
    bytes_delta: int = 0


class MatroskaLanguageEditor:
    """Corrige Language / LanguageBCP47 dans les TrackEntry d'un MKV."""

    def __init__(
        self,
        *,
        editor: MatroskaSegmentInfoHeaderEditor | None = None,
    ) -> None:
        self._editor = editor or MatroskaSegmentInfoHeaderEditor(
            options=MatroskaSegmentInfoHeaderEditorOptions(fallback_mode="skip")
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def apply(self, path: Path) -> MatroskaLanguagePatchResult:
        try:
            return self._apply_impl(path)
        except Exception as exc:
            return MatroskaLanguagePatchResult(
                applied=False,
                skipped=True,
                reason=f"Patch langues ignoré: {exc}",
            )

    # ------------------------------------------------------------------
    # Implementation
    # ------------------------------------------------------------------

    def _apply_impl(self, path: Path) -> MatroskaLanguagePatchResult:
        if not path.is_file():
            raise ValueError(f"Fichier introuvable: {path}")

        ed = self._editor
        with path.open("rb") as fh:
            state = ed._analyze_file(fh, parse_fast=ed.options.parse_fast)
            tracks = self._find_tracks_element(state.data)
            if tracks is None:
                return MatroskaLanguagePatchResult(
                    applied=False, skipped=True, reason="Élément Tracks absent.",
                )
            old_tracks_payload = ed._read_exact(fh, tracks.payload_offset, tracks.size)

        new_tracks_payload, fixes = self._rebuild_tracks_payload(
            old_tracks_payload,
            tracks_abs_payload_offset=tracks.payload_offset,
        )
        if not fixes:
            return MatroskaLanguagePatchResult(
                applied=False,
                skipped=False,
                reason="Aucune correction de langue nécessaire.",
            )

        new_tracks_payload = ed._refresh_crc32_in_payload(new_tracks_payload)
        new_size = ed._encode_ebml_size_prefer_length(
            len(new_tracks_payload), preferred_length=tracks.size_len,
        )
        new_tracks_element = tracks.element_id + new_size + new_tracks_payload

        delta = ed.replace_level1_element(
            path,
            element_id=_TRACKS_ID,
            new_element_bytes=new_tracks_element,
        )

        return MatroskaLanguagePatchResult(
            applied=True,
            skipped=False,
            reason="Tracks Matroska patché (Language + LanguageBCP47).",
            fixes=tuple(fixes),
            bytes_delta=delta,
        )

    # ------------------------------------------------------------------
    # Tracks payload rebuilder
    # ------------------------------------------------------------------

    def _rebuild_tracks_payload(
        self,
        payload: bytes,
        *,
        tracks_abs_payload_offset: int,
    ) -> tuple[bytes, list[TrackLanguageFix]]:
        ed = self._editor
        out = bytearray()
        fixes: list[TrackLanguageFix] = []

        cursor = 0
        while cursor < len(payload):
            child = ed._read_ebml_element_from_bytes(payload, cursor)
            if child.unknown_size or child.end > len(payload):
                out.extend(payload[cursor:])
                break

            if child.element_id != _TRACK_ENTRY_ID:
                out.extend(payload[child.offset:child.end])
                cursor = child.end
                continue

            old_entry_bytes = payload[child.offset:child.end]
            new_entry_bytes, fix = self._rewrite_track_entry(
                old_entry_bytes,
                track_entry_abs_offset=tracks_abs_payload_offset + child.offset,
            )
            out.extend(new_entry_bytes)
            if fix is not None:
                fixes.append(fix)
            cursor = child.end

        return bytes(out), fixes

    def _rewrite_track_entry(
        self,
        entry_bytes: bytes,
        *,
        track_entry_abs_offset: int,
    ) -> tuple[bytes, TrackLanguageFix | None]:
        """Réécrit un TrackEntry pour normaliser Language / LanguageBCP47.

        Stratégie :
        - parcourt les enfants pour localiser Language et LanguageBCP47 existants,
        - détermine la valeur BCP-47 cible (priorité : LanguageBCP47 existant,
          sinon Language si BCP-47 détectable, sinon rien),
        - détermine la valeur ISO 639-2 cible,
        - reconstruit le payload : remplace Language, remplace/ajoute LanguageBCP47.
        """
        ed = self._editor
        entry = ed._read_ebml_element_from_bytes(entry_bytes, 0)
        payload_start = entry.payload_offset
        payload_end = entry.end

        lang_child: tuple[int, int, bytes] | None = None  # (start, end, raw)
        bcp_child: tuple[int, int, bytes] | None = None

        cursor = payload_start
        while cursor < payload_end:
            c = ed._read_ebml_element_from_bytes(entry_bytes, cursor)
            if c.unknown_size or c.end > payload_end:
                break
            if c.element_id == _LANGUAGE_ID:
                lang_child = (c.offset, c.end, entry_bytes[c.payload_offset:c.end])
            elif c.element_id == _LANGUAGE_BCP47_ID:
                bcp_child = (c.offset, c.end, entry_bytes[c.payload_offset:c.end])
            cursor = c.end

        old_lang = lang_child[2].decode("utf-8", errors="replace").strip() if lang_child else None
        old_bcp = bcp_child[2].decode("utf-8", errors="replace").strip() if bcp_child else None

        target_bcp, target_iso = self._resolve_target_codes(old_lang, old_bcp)
        if target_bcp is None or target_iso is None:
            return entry_bytes, None

        needs_fix = (
            old_lang != target_iso
            or old_bcp != target_bcp
            or bcp_child is None
        )
        if not needs_fix:
            return entry_bytes, None

        # Reconstruit les octets du payload.
        new_lang_elem = _LANGUAGE_ID + ed._encode_ebml_size_prefer_length(
            len(target_iso.encode("utf-8")), preferred_length=1,
        ) + target_iso.encode("utf-8")
        new_bcp_elem = _LANGUAGE_BCP47_ID + ed._encode_ebml_size_prefer_length(
            len(target_bcp.encode("utf-8")), preferred_length=1,
        ) + target_bcp.encode("utf-8")

        new_payload = bytearray()
        cursor = payload_start
        replaced_lang = False
        replaced_bcp = False
        while cursor < payload_end:
            c = ed._read_ebml_element_from_bytes(entry_bytes, cursor)
            if c.unknown_size or c.end > payload_end:
                new_payload.extend(entry_bytes[cursor:payload_end])
                break
            if c.element_id == _LANGUAGE_ID:
                new_payload.extend(new_lang_elem)
                replaced_lang = True
                # Insert BCP47 juste après le Language pour rester groupés.
                if not replaced_bcp:
                    new_payload.extend(new_bcp_elem)
                    replaced_bcp = True
            elif c.element_id == _LANGUAGE_BCP47_ID:
                if not replaced_bcp:
                    new_payload.extend(new_bcp_elem)
                    replaced_bcp = True
                # sinon on drop l'ancien (déjà émis)
            else:
                new_payload.extend(entry_bytes[c.offset:c.end])
            cursor = c.end

        if not replaced_lang:
            new_payload.extend(new_lang_elem)
        if not replaced_bcp:
            new_payload.extend(new_bcp_elem)

        new_payload_bytes = ed._refresh_crc32_in_payload(bytes(new_payload))
        new_entry_size = ed._encode_ebml_size_prefer_length(
            len(new_payload_bytes), preferred_length=entry.size_len,
        )
        new_entry_bytes = _TRACK_ENTRY_ID + new_entry_size + new_payload_bytes

        fix = TrackLanguageFix(
            track_entry_offset=track_entry_abs_offset,
            language_before=old_lang,
            language_after=target_iso,
            language_bcp47_before=old_bcp,
            language_bcp47_after=target_bcp,
        )
        return new_entry_bytes, fix

    # ------------------------------------------------------------------
    # Language resolution
    # ------------------------------------------------------------------

    def _resolve_target_codes(
        self,
        old_lang: str | None,
        old_bcp: str | None,
    ) -> tuple[str | None, str | None]:
        """Détermine les valeurs cibles BCP-47 (normalisée) et ISO 639-2/B.

        Règles :
        - la source BCP-47 est ``LanguageBCP47`` si présent, sinon ``Language``
          s'il ressemble à un BCP-47 (contient ``-`` ou est 2 lettres),
        - le tag est canonicalisé (lang lower, region UPPER, script Title),
        - le code ISO 639-2 cible est la forme /B (bibliographique).

        Retourne ``(None, None)`` si aucune source BCP-47 n'est identifiable ;
        dans ce cas on laisse le fichier intact plutôt que de forcer ``und``.
        """
        source_bcp = None
        if old_bcp:
            source_bcp = old_bcp
        elif old_lang and ("-" in old_lang or len(old_lang) == 2):
            source_bcp = old_lang

        if source_bcp is None:
            return None, None

        normalized = canonicalize_bcp47(source_bcp)
        iso_b = iso639_2_bibliographic(normalized)
        if iso_b is None:
            return None, None
        return normalized, iso_b

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_tracks_element(self, data_list):
        for e in data_list:
            if e.element_id == _TRACKS_ID and not e.unknown_size:
                return e
        return None
