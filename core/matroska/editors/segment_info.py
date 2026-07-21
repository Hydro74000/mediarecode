"""
core/matroska/editors/segment_info.py

Matroska Segment Info editor (MuxingApp) with in-place binary patching strategy.

Key goals:
- no temp output file / no full-file copy fallback
- level-1 EBML index and void management
- safe skip on structural risks
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


_EBML_HEADER_ID = b"\x1a\x45\xdf\xa3"
_SEGMENT_ID = b"\x18\x53\x80\x67"
_INFO_ID = b"\x15\x49\xa9\x66"
_SEEKHEAD_ID = b"\x11\x4d\x9b\x74"
_SEEK_ID = b"\x4d\xbb"
_SEEKID_ID = b"\x53\xab"
_SEEKPOS_ID = b"\x53\xac"
_CLUSTER_ID = b"\x1f\x43\xb6\x75"
_VOID_ID = b"\xec"
_TRACKS_ID = b"\x16\x54\xae\x6b"
_ATTACHMENTS_ID = b"\x19\x41\xa4\x69"


@dataclass(frozen=True)
class MatroskaSegmentInfoHeaderEditorOptions:
    segment_id: bytes = _SEGMENT_ID
    info_id: bytes = _INFO_ID
    muxing_app_id: bytes = b"\x4d\x80"
    writing_app_id: bytes = b"\x57\x41"
    # Kept for API compatibility. Parsing is now file-wide.
    header_scan_bytes: int = 8 * 1024 * 1024
    edit_muxing_app: bool = True
    edit_writing_app: bool = False
    # Kept for compatibility: now means "allow in-place relocation/growth".
    rebuild_on_overflow: bool = True
    fallback_mode: str = "skip"
    parse_fast: bool = True


@dataclass(frozen=True)
class MatroskaSegmentInfoPatchResult:
    applied: bool
    skipped: bool
    reason: str = ""
    muxing_app_before: str = ""
    muxing_app_after: str = ""
    bytes_delta: int = 0


@dataclass
class _EbmlElement:
    element_id: bytes
    offset: int
    id_len: int
    size_offset: int
    size: int
    size_len: int
    payload_offset: int
    unknown_size: bool
    unresolved_size: bool = False

    @property
    def header_len(self) -> int:
        return self.id_len + self.size_len

    @property
    def end(self) -> int:
        if self.unknown_size:
            return self.payload_offset
        return self.payload_offset + self.size


@dataclass(frozen=True)
class _SegmentInfoContext:
    segment: _EbmlElement
    info: _EbmlElement
    muxing_app: _EbmlElement
    writing_app: _EbmlElement | None


@dataclass
class _AnalyzerState:
    file_size: int
    segment: _EbmlElement
    data: list[_EbmlElement]


class MatroskaSegmentInfoHeaderEditor:
    def __init__(
        self,
        *,
        options: MatroskaSegmentInfoHeaderEditorOptions | None = None,
    ) -> None:
        self.options = options or MatroskaSegmentInfoHeaderEditorOptions()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def apply_muxing_app_replace_with_header_rebuild(
        self,
        path: Path,
        *,
        app_prefix: str,
    ) -> MatroskaSegmentInfoPatchResult:
        prefix = (app_prefix or "").strip()
        if not prefix:
            return MatroskaSegmentInfoPatchResult(
                applied=False,
                skipped=True,
                reason="Préfixe muxing app vide.",
            )

        try:
            return self._apply_muxing_app_replace_with_header_rebuild_impl(path, app_prefix=prefix)
        except Exception as exc:
            if self.options.fallback_mode != "skip":
                raise
            return self.apply_muxing_app_replace_legacy_skip(path, app_prefix=prefix, cause=exc)

    def apply_muxing_app_replace_legacy_skip(
        self,
        path: Path,
        *,
        app_prefix: str,
        cause: Exception | None = None,
    ) -> MatroskaSegmentInfoPatchResult:
        _ = path
        _ = app_prefix
        reason = "Patch header ignoré (fallback legacy skip)."
        if cause is not None:
            reason += f" cause={cause}"
        return MatroskaSegmentInfoPatchResult(applied=False, skipped=True, reason=reason)

    def locate_info_application_fields(self, data: bytes) -> dict[bytes, tuple[int, int]]:
        context = self._locate_context_in_bytes(data)
        out: dict[bytes, tuple[int, int]] = {
            self.options.muxing_app_id: (context.muxing_app.payload_offset, context.muxing_app.size),
        }
        if context.writing_app is not None:
            out[self.options.writing_app_id] = (context.writing_app.payload_offset, context.writing_app.size)
        return out

    # Backward-compatible helper kept for tests and call sites that
    # introspect internals.
    def _locate_context(self, data: bytes) -> _SegmentInfoContext:
        return self._locate_context_in_bytes(data)

    # Backward-compatible pure-bytes variant.
    def _build_replaced_info_element(
        self,
        data: bytes,
        context: _SegmentInfoContext,
        *,
        new_muxing_app_text: str,
    ) -> bytes:
        info = context.info
        mux = context.muxing_app
        if info.unknown_size:
            raise ValueError("Info de taille inconnue non supporté.")

        old_info_payload = data[info.payload_offset:info.end]
        mux_rel_start = mux.offset - info.payload_offset
        mux_rel_end = mux.end - info.payload_offset
        if mux_rel_start < 0 or mux_rel_end > len(old_info_payload):
            raise ValueError("Offset MuxingApp hors payload Info.")

        new_mux_payload = new_muxing_app_text.encode("utf-8")
        new_mux_size = self._encode_ebml_size_prefer_length(
            len(new_mux_payload),
            preferred_length=mux.size_len,
        )
        new_mux_element = mux.element_id + new_mux_size + new_mux_payload

        new_info_payload = (
            old_info_payload[:mux_rel_start]
            + new_mux_element
            + old_info_payload[mux_rel_end:]
        )
        size_delta = len(new_info_payload) - len(old_info_payload)
        if size_delta != 0:
            adjusted = self._try_absorb_void_delta(new_info_payload, size_delta)
            if adjusted is not None:
                new_info_payload = adjusted

        new_info_payload = self._refresh_crc32_in_payload(new_info_payload)

        new_info_size = self._encode_ebml_size_prefer_length(
            len(new_info_payload),
            preferred_length=info.size_len,
        )
        return info.element_id + new_info_size + new_info_payload

    # ------------------------------------------------------------------
    # Generic level-1 element replacement (public helper)
    # ------------------------------------------------------------------

    def replace_level1_element(
        self,
        path: Path,
        *,
        element_id: bytes,
        new_element_bytes: bytes,
    ) -> int:
        """Remplace in-place un élément level-1 par `new_element_bytes`.

        Toutes les instances existantes de `element_id` sont supprimées,
        l'élément fourni est écrit (de préférence dans un Void, sinon en fin
        de fichier), les SeekHead sont mis à jour, les Void fusionnés.

        Retourne le delta d'octets sur la taille du fichier.
        """
        if not path.is_file():
            raise ValueError(f"Fichier introuvable: {path}")
        parsed = self._read_ebml_element_from_bytes(new_element_bytes, 0)
        if parsed.element_id != element_id:
            raise ValueError("ID du nouvel élément incohérent avec element_id.")

        with path.open("r+b") as fh:
            state = self._analyze_file(fh, parse_fast=self.options.parse_fast)
            before_size = state.file_size

            self._fix_unknown_size_for_last_level1_element(fh, state)
            self._overwrite_all_instances(fh, state, element_id)
            self._merge_void_elements(fh, state)
            new_idx = self._write_level1_element(fh, state, new_element_bytes, strategy_anywhere=True)
            new_offset = state.data[new_idx].offset
            self._remove_from_meta_seeks(fh, state, element_id)
            self._merge_void_elements(fh, state)
            new_idx = self._find_entry_index(state, element_id, new_offset)
            if new_idx < 0:
                raise ValueError("Élément level-1 introuvable après écriture/fusion.")
            self._add_to_meta_seek(fh, state, new_idx)
            self._merge_void_elements(fh, state)
            self._resync_meta_seeks(fh, state)

            fh.flush()
            after_size = fh.seek(0, 2)
            return after_size - before_size

    # ------------------------------------------------------------------
    # Core update flow for Info
    # ------------------------------------------------------------------

    def _apply_muxing_app_replace_with_header_rebuild_impl(
        self,
        path: Path,
        *,
        app_prefix: str,
    ) -> MatroskaSegmentInfoPatchResult:
        if not self.options.edit_muxing_app:
            return MatroskaSegmentInfoPatchResult(
                applied=False,
                skipped=True,
                reason="Option edit_muxing_app désactivée.",
            )
        if not path.is_file():
            raise ValueError(f"Fichier introuvable: {path}")

        with path.open("r+b") as fh:
            state = self._analyze_file(fh, parse_fast=self.options.parse_fast)
            context = self._locate_context_in_file(fh, state)

            old_raw = self._read_exact(fh, context.muxing_app.payload_offset, context.muxing_app.size)
            original_mux = self._decode_text(old_raw)
            target_mux = app_prefix

            if target_mux == original_mux: #Do not edit
                return MatroskaSegmentInfoPatchResult(
                    applied=False,
                    skipped=False,
                    reason="Valeur MuxingApp déjà à jour.",
                    muxing_app_before=original_mux,
                    muxing_app_after=target_mux,
                    bytes_delta=0,
                )

            if not self.options.rebuild_on_overflow and len(target_mux.encode("utf-8")) > context.muxing_app.size:
                raise ValueError("Overflow MuxingApp détecté et rebuild_on_overflow=False.")

            new_info = self._build_replaced_info_element_from_file(
                fh,
                context,
                new_muxing_app_text=target_mux,
            )

            before_size = state.file_size

            # Sequence for one level-1 element (Info).
            self._fix_unknown_size_for_last_level1_element(fh, state)
            self._overwrite_all_instances(fh, state, self.options.info_id)
            self._merge_void_elements(fh, state)
            info_idx = self._write_level1_element(fh, state, new_info, strategy_anywhere=True)
            info_offset = state.data[info_idx].offset
            self._remove_from_meta_seeks(fh, state, self.options.info_id)
            self._merge_void_elements(fh, state)
            info_idx = self._find_entry_index(state, self.options.info_id, info_offset)
            if info_idx < 0:
                raise ValueError("Info introuvable après écriture/fusion.")
            self._add_to_meta_seek(fh, state, info_idx)
            self._merge_void_elements(fh, state)
            self._resync_meta_seeks(fh, state)

            fh.flush()
            after_size = fh.seek(0, 2)

            ctx_after = self._locate_context_in_file(fh, state)
            new_raw = self._read_exact(fh, ctx_after.muxing_app.payload_offset, ctx_after.muxing_app.size)
            final_mux = self._decode_text(new_raw)
            if final_mux != target_mux:
                raise ValueError("Validation post-écriture: MuxingApp inattendue.")

            return MatroskaSegmentInfoPatchResult(
                applied=True,
                skipped=False,
                reason="Segment Info Matroska patché in-place.",
                muxing_app_before=original_mux,
                muxing_app_after=final_mux,
                bytes_delta=after_size - before_size,
            )

    # ------------------------------------------------------------------
    # Analyzer (fast + meta seek recursion)
    # ------------------------------------------------------------------

    def _analyze_file(self, fh: BinaryIO, *, parse_fast: bool) -> _AnalyzerState:
        file_size = self._file_size(fh)
        segment = self._locate_segment_in_file(fh, file_size)

        data: list[_EbmlElement] = []
        cursor = segment.payload_offset
        segment_end = segment.end if not segment.unknown_size else file_size
        seen_cluster = False
        seen_seekhead = False

        while cursor < segment_end:
            try:
                l1 = self._read_ebml_element_from_file(fh, cursor, file_size)
            except ValueError:
                break

            data.append(l1)

            if l1.element_id == _CLUSTER_ID:
                seen_cluster = True
            elif l1.element_id == _SEEKHEAD_ID:
                seen_seekhead = True

            if l1.unknown_size:
                break

            cursor = l1.end
            if parse_fast and seen_cluster and seen_seekhead:
                break

        self._read_all_meta_seeks(fh, segment, data, file_size)
        self._fix_unresolved_sizes(data, file_size)

        data.sort(key=lambda e: e.offset)
        return _AnalyzerState(file_size=file_size, segment=segment, data=data)

    def _read_all_meta_seeks(
        self,
        fh: BinaryIO,
        segment: _EbmlElement,
        data: list[_EbmlElement],
        file_size: int,
    ) -> None:
        positions_found: set[int] = {e.offset for e in data}
        seen_seek_heads: set[int] = set()

        def read_seek_head_recursive(pos: int) -> None:
            if pos in seen_seek_heads:
                return
            seen_seek_heads.add(pos)

            try:
                sh = self._read_ebml_element_from_file(fh, pos, file_size)
            except ValueError:
                return
            if sh.element_id != _SEEKHEAD_ID or sh.unknown_size:
                return

            for target_id, rel_pos in self._iter_seek_entries(fh, sh):
                abs_pos = segment.payload_offset + rel_pos
                if abs_pos in positions_found:
                    continue

                try:
                    entry = self._read_ebml_element_from_file(fh, abs_pos, file_size)
                except ValueError:
                    entry = _EbmlElement(
                        element_id=target_id,
                        offset=abs_pos,
                        id_len=0,
                        size_offset=abs_pos,
                        size=-1,
                        size_len=0,
                        payload_offset=abs_pos,
                        unknown_size=False,
                        unresolved_size=True,
                    )
                else:
                    if entry.element_id != target_id:
                        entry.unresolved_size = True

                data.append(entry)
                positions_found.add(abs_pos)
                if target_id == _SEEKHEAD_ID:
                    read_seek_head_recursive(abs_pos)

        for e in list(data):
            if e.element_id == _SEEKHEAD_ID:
                read_seek_head_recursive(e.offset)

    def _fix_unresolved_sizes(self, data: list[_EbmlElement], file_size: int) -> None:
        data.sort(key=lambda e: e.offset)
        for i, e in enumerate(data):
            if not e.unresolved_size:
                continue
            next_pos = data[i + 1].offset if i + 1 < len(data) else file_size
            if next_pos <= e.offset:
                continue
            e.size = next_pos - e.offset
            e.unresolved_size = False

    # ------------------------------------------------------------------
    # Sequence steps
    # ------------------------------------------------------------------

    def _fix_unknown_size_for_last_level1_element(self, fh: BinaryIO, state: _AnalyzerState) -> None:
        if not state.data:
            return
        last = state.data[-1]
        if not last.unknown_size:
            return

        segment_end = state.segment.end if not state.segment.unknown_size else self._file_size(fh)
        actual_size = segment_end - last.payload_offset
        if actual_size < 0:
            raise ValueError("Dernier élément level-1 taille inconnue invalide.")

        required_len = self._minimal_size_length_for_value(actual_size)
        if required_len > last.size_len:
            raise ValueError("Impossible de normaliser la taille inconnue du dernier élément level-1.")

        new_size = self._encode_ebml_size(actual_size, length=last.size_len)
        self._write_at(fh, last.size_offset, new_size)

        last.size = actual_size
        last.unknown_size = False

    def _overwrite_all_instances(self, fh: BinaryIO, state: _AnalyzerState, element_id: bytes) -> None:
        idx = 0
        while idx < len(state.data):
            e = state.data[idx]
            if e.element_id != element_id:
                idx += 1
                continue
            self._mark_removed(e)
            self._handle_void_elements(fh, state, idx)
            idx = max(0, idx - 1)

    def _write_level1_element(
        self,
        fh: BinaryIO,
        state: _AnalyzerState,
        element_bytes: bytes,
        *,
        strategy_anywhere: bool,
    ) -> int:
        element_size = len(element_bytes)

        start_idx = 0 if strategy_anywhere else max(0, len(state.data) - 1)
        for idx in range(start_idx, len(state.data)):
            slot = state.data[idx]
            if slot.element_id != _VOID_ID:
                continue
            if self._element_span(slot) < element_size:
                continue

            self._write_at(fh, slot.offset, element_bytes)
            new_elem = self._read_ebml_element_from_bytes(element_bytes, 0)
            slot.element_id = new_elem.element_id
            slot.id_len = new_elem.id_len
            slot.size_offset = slot.offset + new_elem.id_len
            slot.size = new_elem.size
            slot.size_len = new_elem.size_len
            slot.payload_offset = slot.size_offset + slot.size_len
            slot.unknown_size = False
            slot.unresolved_size = False

            self._handle_void_elements(fh, state, idx)
            return idx

        pos = self._file_size(fh)
        self._write_at(fh, pos, element_bytes)
        new_elem = self._read_ebml_element_from_bytes(element_bytes, 0)
        entry = _EbmlElement(
            element_id=new_elem.element_id,
            offset=pos,
            id_len=new_elem.id_len,
            size_offset=pos + new_elem.id_len,
            size=new_elem.size,
            size_len=new_elem.size_len,
            payload_offset=pos + new_elem.id_len + new_elem.size_len,
            unknown_size=False,
            unresolved_size=False,
        )
        state.data.append(entry)
        state.data.sort(key=lambda e: e.offset)
        self._adjust_segment_size(fh, state)
        return state.data.index(entry)

    def _remove_from_meta_seeks(self, fh: BinaryIO, state: _AnalyzerState, target_id: bytes) -> None:
        idx = 0
        while idx < len(state.data):
            e = state.data[idx]
            if e.element_id != _SEEKHEAD_ID:
                idx += 1
                continue
            if e.unknown_size:
                raise ValueError("SeekHead de taille inconnue non supporté en patch sécurisé.")

            old_total = e.header_len + e.size
            payload = self._read_exact(fh, e.payload_offset, e.size)

            keep_children: list[bytes] = []
            changed = False
            cursor = 0
            while cursor < len(payload):
                child = self._read_ebml_element_from_bytes(payload, cursor)
                child_raw = payload[child.offset:child.end]
                remove = False

                if child.element_id == _SEEK_ID and not child.unknown_size:
                    seek_payload = payload[child.payload_offset:child.end]
                    seek_id = self._extract_seek_id_from_seek_payload(seek_payload)
                    if seek_id == target_id:
                        remove = True

                if remove:
                    changed = True
                else:
                    keep_children.append(child_raw)

                cursor = child.end

            if not changed:
                idx += 1
                continue

            if not keep_children:
                self._mark_removed(e)
                self._handle_void_elements(fh, state, idx)
                idx = max(0, idx - 1)
                continue

            new_payload = b"".join(keep_children)
            new_payload = self._refresh_crc32_in_payload(new_payload)
            new_size = self._encode_ebml_size_prefer_length(len(new_payload), preferred_length=e.size_len)
            new_bytes = e.element_id + new_size + new_payload
            if len(new_bytes) > old_total:
                raise ValueError("SeekHead grossit pendant remove_from_meta_seeks: skip sécurisé.")

            self._write_at(fh, e.offset, new_bytes)

            e.id_len = len(e.element_id)
            e.size_offset = e.offset + e.id_len
            e.size_len = len(new_size)
            e.payload_offset = e.size_offset + e.size_len
            e.size = len(new_payload)

            if len(new_bytes) < old_total:
                self._handle_void_elements(fh, state, idx)
                idx = max(0, idx - 1)
            else:
                idx += 1

    def _add_to_meta_seek(self, fh: BinaryIO, state: _AnalyzerState, element_index: int, *, _depth: int = 0) -> None:
        if _depth > 3:
            raise ValueError("add_to_meta_seek recursion depth exceeded.")

        if element_index < 0 or element_index >= len(state.data):
            raise ValueError("Index élément invalide pour add_to_meta_seek.")
        target = state.data[element_index]
        seek_entry = self._build_seek_entry(target.element_id, target.offset - state.segment.payload_offset)
        has_seek_head = any(e.element_id == _SEEKHEAD_ID for e in state.data)
        if not has_seek_head:
            created = self._create_new_meta_seek_at_start(fh, state, seek_entry)
            if created:
                return
            # Safe fallback: content has been patched, but no room to host a
            # seek head without risky moves.
            return

        added = self._try_adding_to_existing_meta_seek(fh, state, seek_entry)
        if added:
            return

        created = self._create_new_meta_seek_at_start(fh, state, seek_entry)
        if created:
            return

        moved = self._move_level1_element_before_cluster_to_end_of_file(fh, state)
        if not moved:
            raise ValueError("Aucun placement SeekHead disponible.")

        # Re-locate target index after potential resort and retry once.
        for i, e in enumerate(state.data):
            if e.offset == target.offset and e.element_id == target.element_id:
                self._add_to_meta_seek(fh, state, i, _depth=_depth + 1)
                return
        raise ValueError("Élément cible introuvable après déplacement level-1.")

    def _resync_meta_seeks(self, fh: BinaryIO, state: _AnalyzerState) -> None:
        """Réaligne toutes les SeekPosition sur les offsets réels des éléments.

        Les étapes d'édition in-place peuvent déplacer un élément level-1
        après l'écriture de son entrée Seek (ex. résorption d'un gap d'un
        octet qui remonte l'élément suivant). Cette passe finale réécrit
        chaque SeekPosition d'après ``state.data`` — à longueur d'encodage
        constante (uint paddé en tête, valeur identique) pour un patch
        strictement in-place. Les entrées sans cible connue sont laissées
        telles quelles (cas déjà couvert par la suppression d'entrées).
        """
        offsets_by_id: dict[bytes, list[int]] = {}
        for entry in state.data:
            if entry.element_id in (_VOID_ID, _SEEKHEAD_ID):
                continue
            if self._element_span(entry) == 0:
                continue
            offsets_by_id.setdefault(entry.element_id, []).append(
                entry.offset - state.segment.payload_offset
            )
        for values in offsets_by_id.values():
            values.sort()
        consumed: dict[bytes, int] = {}

        for seek_head in state.data:
            if seek_head.element_id != _SEEKHEAD_ID or seek_head.unknown_size:
                continue
            payload = self._read_exact(fh, seek_head.payload_offset, seek_head.size)
            buf = bytearray(payload)
            cursor = 0
            changed = False
            while cursor < len(payload):
                child = self._read_ebml_element_from_bytes(payload, cursor)
                if child.unknown_size:
                    break
                if child.element_id == _SEEK_ID:
                    seek_payload = payload[child.payload_offset:child.end]
                    target_id = self._extract_seek_id_from_seek_payload(seek_payload)
                    candidates = offsets_by_id.get(target_id or b"", [])
                    if target_id is not None and candidates:
                        rank = consumed.get(target_id, 0)
                        expected = candidates[min(rank, len(candidates) - 1)]
                        consumed[target_id] = rank + 1
                        inner = 0
                        while inner < len(seek_payload):
                            sub = self._read_ebml_element_from_bytes(seek_payload, inner)
                            if sub.unknown_size:
                                break
                            if sub.element_id == _SEEKPOS_ID and sub.size > 0:
                                current = int.from_bytes(
                                    seek_payload[sub.payload_offset:sub.end], "big"
                                )
                                if current != expected:
                                    if expected >= 1 << (8 * sub.size):
                                        raise ValueError(
                                            "Resynchronisation SeekPosition impossible : "
                                            f"valeur {expected} hors capacité ({sub.size} octets)."
                                        )
                                    start = child.payload_offset + sub.payload_offset
                                    buf[start:start + sub.size] = expected.to_bytes(sub.size, "big")
                                    changed = True
                            inner = sub.end
                cursor = child.end

            if changed:
                new_payload = self._refresh_crc32_in_payload(bytes(buf))
                self._write_at(fh, seek_head.payload_offset, new_payload)

    def _try_adding_to_existing_meta_seek(self, fh: BinaryIO, state: _AnalyzerState, seek_entry: bytes) -> bool:
        for idx, sh in enumerate(state.data):
            if sh.element_id != _SEEKHEAD_ID or sh.unknown_size:
                continue

            payload = self._read_exact(fh, sh.payload_offset, sh.size)
            new_payload = payload + seek_entry
            new_payload = self._refresh_crc32_in_payload(new_payload)
            new_size = self._encode_ebml_size_prefer_length(len(new_payload), preferred_length=sh.size_len)
            new_bytes = sh.element_id + new_size + new_payload

            available = self._element_span(sh)
            if idx + 1 < len(state.data) and state.data[idx + 1].element_id == _VOID_ID:
                available += self._element_span(state.data[idx + 1])

            at_end = idx == len(state.data) - 1
            if not at_end and len(new_bytes) > available:
                continue

            self._write_at(fh, sh.offset, new_bytes)

            sh.size_offset = sh.offset + sh.id_len
            sh.size_len = len(new_size)
            sh.payload_offset = sh.size_offset + sh.size_len
            sh.size = len(new_payload)

            if at_end:
                self._adjust_segment_size(fh, state)
            else:
                self._handle_void_elements(fh, state, idx)

            self._ensure_front_seek_head_exists(fh, state, idx)
            return True

        return False

    def _create_new_meta_seek_at_start(self, fh: BinaryIO, state: _AnalyzerState, seek_entry: bytes) -> bool:
        seekhead = self._build_seekhead_from_entries([seek_entry])
        needed = len(seekhead)

        for idx, e in enumerate(state.data):
            if e.element_id == _CLUSTER_ID:
                break
            if e.element_id != _VOID_ID:
                continue
            # Guard: avoid "+1 byte" residual hole.
            slot_span = self._element_span(e)
            if slot_span != needed and slot_span < needed + 2:
                continue

            self._write_at(fh, e.offset, seekhead)
            parsed = self._read_ebml_element_from_bytes(seekhead, 0)
            e.element_id = _SEEKHEAD_ID
            e.id_len = parsed.id_len
            e.size_offset = e.offset + parsed.id_len
            e.size_len = parsed.size_len
            e.payload_offset = e.size_offset + parsed.size_len
            e.size = parsed.size
            e.unknown_size = False
            e.unresolved_size = False

            self._handle_void_elements(fh, state, idx)
            return True

        return False

    def _move_level1_element_before_cluster_to_end_of_file(self, fh: BinaryIO, state: _AnalyzerState) -> bool:
        candidates: list[tuple[int, int]] = []
        for idx, e in enumerate(state.data):
            if e.element_id == _CLUSTER_ID:
                break
            if e.element_id == _ATTACHMENTS_ID:
                candidates.append((10, idx))
            elif e.element_id == _TRACKS_ID:
                candidates.append((20, idx))
            elif e.element_id == _INFO_ID:
                candidates.append((30, idx))

        if not candidates:
            return False

        candidates.sort()
        _, idx = candidates[0]
        elt = state.data[idx]
        if elt.unknown_size:
            return False

        raw = self._read_exact(fh, elt.offset, self._element_span(elt))
        new_pos = self._file_size(fh)
        self._write_at(fh, new_pos, raw)

        moved = _EbmlElement(
            element_id=elt.element_id,
            offset=new_pos,
            id_len=elt.id_len,
            size_offset=new_pos + elt.id_len,
            size=elt.size,
            size_len=elt.size_len,
            payload_offset=new_pos + elt.id_len + elt.size_len,
            unknown_size=False,
            unresolved_size=False,
        )
        state.data.append(moved)

        self._mark_removed(elt)
        self._handle_void_elements(fh, state, idx)
        state.data.sort(key=lambda e: e.offset)

        try:
            moved_idx = state.data.index(moved)
        except ValueError:
            return False

        self._add_to_meta_seek(fh, state, moved_idx)
        return True

    def _ensure_front_seek_head_exists(self, fh: BinaryIO, state: _AnalyzerState, seek_head_idx: int) -> None:
        if seek_head_idx < 0 or seek_head_idx >= len(state.data):
            return
        sh = state.data[seek_head_idx]
        if sh.element_id != _SEEKHEAD_ID:
            return

        for e in state.data:
            if e.element_id == _CLUSTER_ID:
                break
            if e.element_id == _SEEKHEAD_ID:
                return

        # No seek head before first cluster: create forward one if possible.
        rel_pos = sh.offset - state.segment.payload_offset
        forward_seek = self._build_seek_entry(_SEEKHEAD_ID, rel_pos)
        created = self._create_new_meta_seek_at_start(fh, state, forward_seek)
        if not created:
            raise ValueError("Impossible de créer un SeekHead en tête de segment.")

    # ------------------------------------------------------------------
    # Void handling
    # ------------------------------------------------------------------

    def _handle_void_elements(self, fh: BinaryIO, state: _AnalyzerState, data_idx: int) -> bool:
        if data_idx < 0 or data_idx >= len(state.data):
            return False

        end_idx = data_idx + 1
        while end_idx < len(state.data) and state.data[end_idx].element_id == _VOID_ID:
            end_idx += 1

        if end_idx == len(state.data):
            cur = state.data[data_idx]
            truncate_to = cur.offset + self._element_span(cur)
            fh.truncate(truncate_to)
            state.file_size = truncate_to
            self._adjust_segment_size(fh, state)
            if self._element_span(cur) == 0:
                del state.data[data_idx:]
            return False

        if end_idx > data_idx + 1:
            del state.data[data_idx + 1:end_idx]

        cur = state.data[data_idx]
        nxt = state.data[data_idx + 1]

        void_pos = cur.offset + self._element_span(cur)
        void_size = nxt.offset - void_pos

        if void_size == 0:
            if self._element_span(cur) == 0:
                del state.data[data_idx]
            return False

        if void_size == 1:
            # Handling for 1-byte gap.
            if nxt.id_len <= 0 or nxt.size_len <= 0:
                nxt = self._read_ebml_element_from_file(fh, nxt.offset, self._file_size(fh))
                state.data[data_idx + 1] = nxt

            move_up = nxt.size_len < 8
            new_pos = nxt.offset - 1 if move_up else nxt.offset + 1
            new_size_len = nxt.size_len + 1 if move_up else 7
            if new_size_len < 1 or new_size_len > 8:
                raise ValueError("Impossible de gérer le gap de 1 octet (size length).")

            new_head = nxt.element_id + self._encode_ebml_size(nxt.size, length=new_size_len)
            self._write_at(fh, new_pos, new_head)

            nxt.offset = new_pos
            nxt.size_offset = new_pos + nxt.id_len
            nxt.size_len = new_size_len
            nxt.payload_offset = nxt.size_offset + nxt.size_len

            if not move_up:
                empty_void = self._build_void_element(2)
                self._write_at(fh, new_pos - 2, empty_void)
                state.data.insert(
                    data_idx + 1,
                    _EbmlElement(
                        element_id=_VOID_ID,
                        offset=new_pos - 2,
                        id_len=1,
                        size_offset=new_pos - 1,
                        size=0,
                        size_len=1,
                        payload_offset=new_pos,
                        unknown_size=False,
                        unresolved_size=False,
                    ),
                )
                data_idx += 1

            # Keep indexes coherent after positional changes.
            state.data.sort(key=lambda e: e.offset)
            return False

        # void_size >= 2
        void_bytes = self._build_void_element(void_size)
        self._write_at(fh, void_pos, void_bytes)
        void_hdr = self._read_ebml_element_from_bytes(void_bytes, 0)

        new_void = _EbmlElement(
            element_id=_VOID_ID,
            offset=void_pos,
            id_len=void_hdr.id_len,
            size_offset=void_pos + void_hdr.id_len,
            size=void_hdr.size,
            size_len=void_hdr.size_len,
            payload_offset=void_pos + void_hdr.id_len + void_hdr.size_len,
            unknown_size=False,
            unresolved_size=False,
        )

        state.data.insert(data_idx + 1, new_void)
        if self._element_span(cur) == 0:
            del state.data[data_idx]
        return True

    def _merge_void_elements(self, fh: BinaryIO, state: _AnalyzerState) -> None:
        state.data.sort(key=lambda e: e.offset)

        idx = 0
        while idx < len(state.data):
            if state.data[idx].element_id != _VOID_ID:
                idx += 1
                continue

            end = idx + 1
            total_size = self._element_span(state.data[idx])
            while end < len(state.data) and state.data[end].element_id == _VOID_ID:
                total_size += self._element_span(state.data[end])
                end += 1

            if end == idx + 1:
                idx += 1
                continue

            start_pos = state.data[idx].offset
            merged = self._build_void_element(total_size)
            self._write_at(fh, start_pos, merged)
            hdr = self._read_ebml_element_from_bytes(merged, 0)

            state.data[idx] = _EbmlElement(
                element_id=_VOID_ID,
                offset=start_pos,
                id_len=hdr.id_len,
                size_offset=start_pos + hdr.id_len,
                size=hdr.size,
                size_len=hdr.size_len,
                payload_offset=start_pos + hdr.id_len + hdr.size_len,
                unknown_size=False,
                unresolved_size=False,
            )
            del state.data[idx + 1:end]
            idx += 1

        # Remove trailing voids and truncate.
        start_idx = len(state.data)
        while start_idx > 0 and state.data[start_idx - 1].element_id == _VOID_ID:
            start_idx -= 1

        if start_idx < len(state.data):
            truncate_to = state.data[start_idx].offset
            fh.truncate(truncate_to)
            state.file_size = truncate_to
            del state.data[start_idx:]
            self._adjust_segment_size(fh, state)

    # ------------------------------------------------------------------
    # Size / segment adjustments
    # ------------------------------------------------------------------

    def _adjust_segment_size(self, fh: BinaryIO, state: _AnalyzerState) -> None:
        if state.segment.unknown_size:
            state.file_size = self._file_size(fh)
            return

        file_end = self._file_size(fh)
        new_seg_payload_size = file_end - state.segment.payload_offset
        if new_seg_payload_size < 0:
            raise ValueError("Taille Segment négative.")

        # Keep encoded size length stable for safe in-place patch.
        encoded = self._encode_ebml_size(new_seg_payload_size, length=state.segment.size_len)
        self._write_at(fh, state.segment.size_offset, encoded)

        state.segment.size = new_seg_payload_size
        state.file_size = file_end

    # ------------------------------------------------------------------
    # Context location (file + bytes)
    # ------------------------------------------------------------------

    def _locate_context_in_file(self, fh: BinaryIO, state: _AnalyzerState) -> _SegmentInfoContext:
        for e in state.data:
            if e.element_id != self.options.info_id or e.unknown_size:
                continue
            parsed = self._parse_info_children_from_file(fh, e)
            if parsed is None:
                continue
            muxing_app, writing_app = parsed
            return _SegmentInfoContext(
                segment=state.segment,
                info=e,
                muxing_app=muxing_app,
                writing_app=writing_app,
            )
        raise ValueError("Segment Info valide introuvable.")

    def _locate_context_in_bytes(self, data: bytes) -> _SegmentInfoContext:
        segment = self._locate_segment_in_bytes(data)
        cursor = segment.payload_offset

        while cursor < len(data):
            elem = self._read_ebml_element_from_bytes(data, cursor)
            if elem.element_id == self.options.info_id and not elem.unknown_size:
                if elem.end <= len(data):
                    parsed = self._parse_info_children_in_bytes(data, elem)
                    if parsed is not None:
                        muxing_app, writing_app = parsed
                        return _SegmentInfoContext(
                            segment=segment,
                            info=elem,
                            muxing_app=muxing_app,
                            writing_app=writing_app,
                        )
            if elem.unknown_size:
                break
            cursor = elem.end

        raise ValueError("Segment Info valide introuvable dans les données fournies.")

    # ------------------------------------------------------------------
    # Building Info payload replacement
    # ------------------------------------------------------------------

    def _build_replaced_info_element_from_file(
        self,
        fh: BinaryIO,
        context: _SegmentInfoContext,
        *,
        new_muxing_app_text: str,
    ) -> bytes:
        info = context.info
        mux = context.muxing_app

        if info.unknown_size:
            raise ValueError("Info de taille inconnue non supporté.")

        old_info_payload = self._read_exact(fh, info.payload_offset, info.size)
        mux_rel_start = mux.offset - info.payload_offset
        mux_rel_end = mux.end - info.payload_offset
        if mux_rel_start < 0 or mux_rel_end > len(old_info_payload):
            raise ValueError("Offset MuxingApp hors payload Info.")

        new_mux_payload = new_muxing_app_text.encode("utf-8")
        new_mux_size = self._encode_ebml_size_prefer_length(
            len(new_mux_payload),
            preferred_length=mux.size_len,
        )
        new_mux_element = mux.element_id + new_mux_size + new_mux_payload

        new_info_payload = (
            old_info_payload[:mux_rel_start]
            + new_mux_element
            + old_info_payload[mux_rel_end:]
        )

        # Try to absorb delta via inner Void if possible.
        size_delta = len(new_info_payload) - len(old_info_payload)
        if size_delta != 0:
            adjusted = self._try_absorb_void_delta(new_info_payload, size_delta)
            if adjusted is not None:
                new_info_payload = adjusted

        new_info_payload = self._refresh_crc32_in_payload(new_info_payload)

        new_info_size = self._encode_ebml_size_prefer_length(
            len(new_info_payload),
            preferred_length=info.size_len,
        )
        return info.element_id + new_info_size + new_info_payload

    def _refresh_crc32_in_payload(self, payload: bytes) -> bytes:
        """Recalcule le CRC-32 EBML (ID 0xBF) s'il est présent en tête du payload.

        À appeler après toute modification du contenu d'un élément conteneur
        (Info, Tracks, etc.) pour maintenir l'intégrité structurelle. Sans CRC,
        retourne le payload inchangé.

        Le CRC-32 Matroska (spec §11.1) couvre tous les octets de l'élément
        conteneur APRÈS le sous-élément CRC-32 lui-même, jusqu'à la fin du
        payload. La valeur est encodée en little-endian sur 4 octets.
        """
        if not payload or payload[0] != 0xBF:
            return payload

        try:
            crc_elem = self._read_ebml_element_from_bytes(payload, 0)
        except ValueError:
            return payload

        if crc_elem.unknown_size or crc_elem.size != 4:
            return payload

        crc_data_start = crc_elem.end
        computed = zlib.crc32(payload[crc_data_start:]) & 0xFFFFFFFF
        new_crc_bytes = struct.pack("<I", computed)

        return (
            payload[: crc_elem.payload_offset]
            + new_crc_bytes
            + payload[crc_data_start:]
        )

    def _try_absorb_void_delta(self, payload: bytes, delta: int) -> bytes | None:
        cursor = 0
        while cursor < len(payload):
            child = self._read_ebml_element_from_bytes(payload, cursor)
            if child.unknown_size:
                break
            if child.element_id == _VOID_ID:
                void_total = child.header_len + child.size
                target_total = void_total - delta

                if target_total == 0:
                    return payload[:cursor] + payload[child.end:]
                if target_total < 2:
                    return None

                try:
                    new_void = self._build_void_element(target_total)
                except ValueError:
                    return None

                if len(new_void) != target_total:
                    return None
                return payload[:cursor] + new_void + payload[child.end:]

            cursor = child.end

        return None

    # ------------------------------------------------------------------
    # Seek parsing/building
    # ------------------------------------------------------------------

    def _iter_seek_entries(self, fh: BinaryIO, seek_head: _EbmlElement) -> list[tuple[bytes, int]]:
        payload = self._read_exact(fh, seek_head.payload_offset, seek_head.size)
        out: list[tuple[bytes, int]] = []

        cursor = 0
        while cursor < len(payload):
            child = self._read_ebml_element_from_bytes(payload, cursor)
            if child.unknown_size:
                break
            if child.element_id == _SEEK_ID:
                seek_payload = payload[child.payload_offset:child.end]
                seek_id = self._extract_seek_id_from_seek_payload(seek_payload)
                seek_pos = self._extract_seek_pos_from_seek_payload(seek_payload)
                if seek_id is not None and seek_pos is not None:
                    out.append((seek_id, seek_pos))
            cursor = child.end

        return out

    def _extract_seek_id_from_seek_payload(self, payload: bytes) -> bytes | None:
        cursor = 0
        while cursor < len(payload):
            child = self._read_ebml_element_from_bytes(payload, cursor)
            if child.unknown_size:
                break
            if child.element_id == _SEEKID_ID:
                return payload[child.payload_offset:child.end]
            cursor = child.end
        return None

    def _extract_seek_pos_from_seek_payload(self, payload: bytes) -> int | None:
        cursor = 0
        while cursor < len(payload):
            child = self._read_ebml_element_from_bytes(payload, cursor)
            if child.unknown_size:
                break
            if child.element_id == _SEEKPOS_ID:
                raw = payload[child.payload_offset:child.end]
                if not raw:
                    return 0
                return int.from_bytes(raw, "big", signed=False)
            cursor = child.end
        return None

    def _build_seek_entry(self, target_id: bytes, rel_pos: int) -> bytes:
        if rel_pos < 0:
            raise ValueError("SeekPosition négative.")

        seek_id = _SEEKID_ID + self._encode_ebml_size(len(target_id), length=1) + target_id

        pos_payload = self._encode_uint(rel_pos)
        seek_pos = _SEEKPOS_ID + self._encode_ebml_size(len(pos_payload), length=1) + pos_payload

        payload = seek_id + seek_pos
        size = self._encode_ebml_size_prefer_length(len(payload), preferred_length=1)
        return _SEEK_ID + size + payload

    def _build_seekhead_from_entries(self, entries: list[bytes]) -> bytes:
        payload = b"".join(entries)
        size = self._encode_ebml_size_prefer_length(len(payload), preferred_length=1)
        return _SEEKHEAD_ID + size + payload

    # ------------------------------------------------------------------
    # Byte/file EBML parsing helpers
    # ------------------------------------------------------------------

    def _locate_segment_in_file(self, fh: BinaryIO, file_size: int) -> _EbmlElement:
        start = 0
        try:
            ebml_id, ebml_id_len = self._read_ebml_id_from_file(fh, 0, file_size)
            if ebml_id == _EBML_HEADER_ID:
                ebml_size, ebml_size_len, ebml_unknown = self._read_ebml_size_from_file(fh, ebml_id_len, file_size)
                if not ebml_unknown:
                    start = ebml_id_len + ebml_size_len + ebml_size
        except ValueError:
            start = 0

        cursor = start
        while cursor < file_size:
            try:
                e = self._read_ebml_element_from_file(fh, cursor, file_size)
            except ValueError:
                break
            if e.element_id == self.options.segment_id:
                return e
            if e.unknown_size:
                break
            cursor = e.end

        # Fallback pattern search across file (no fixed scan limit).
        idx = self._find_pattern_in_file(fh, self.options.segment_id, file_size)
        if idx < 0:
            raise ValueError("Segment Matroska introuvable.")
        e = self._read_ebml_element_from_file(fh, idx, file_size)
        if e.element_id != self.options.segment_id:
            raise ValueError("Segment Matroska introuvable (fallback mismatch).")
        return e

    def _locate_segment_in_bytes(self, data: bytes) -> _EbmlElement:
        start = 0
        try:
            eid, eid_len = self._read_ebml_id_from_bytes(data, 0)
            if eid == _EBML_HEADER_ID:
                sz, sz_len, unknown = self._read_ebml_size_from_bytes(data, eid_len)
                if not unknown:
                    start = eid_len + sz_len + sz
        except ValueError:
            start = 0

        cursor = start
        while cursor < len(data):
            try:
                e = self._read_ebml_element_from_bytes(data, cursor)
            except ValueError:
                break
            if e.element_id == self.options.segment_id:
                return e
            if e.unknown_size:
                break
            cursor = e.end

        pos = start
        while True:
            idx = data.find(self.options.segment_id, pos)
            if idx < 0:
                raise ValueError("Segment Matroska introuvable.")
            pos = idx + 1
            try:
                e = self._read_ebml_element_from_bytes(data, idx)
            except ValueError:
                continue
            if e.element_id == self.options.segment_id:
                return e

    def _parse_info_children_from_file(
        self,
        fh: BinaryIO,
        info: _EbmlElement,
    ) -> tuple[_EbmlElement, _EbmlElement | None] | None:
        payload = self._read_exact(fh, info.payload_offset, info.size)
        return self._parse_info_children_in_bytes(payload, _EbmlElement(
            element_id=info.element_id,
            offset=0,
            id_len=info.id_len,
            size_offset=info.id_len,
            size=info.size,
            size_len=info.size_len,
            payload_offset=0,
            unknown_size=False,
        ), base_offset=info.payload_offset)

    def _parse_info_children_in_bytes(
        self,
        data: bytes,
        info: _EbmlElement,
        *,
        base_offset: int = 0,
    ) -> tuple[_EbmlElement, _EbmlElement | None] | None:
        mux: _EbmlElement | None = None
        writing: _EbmlElement | None = None
        cursor = info.payload_offset
        end = info.end

        while cursor < end:
            child = self._read_ebml_element_from_bytes(data, cursor)
            if child.unknown_size:
                return None
            if child.end > end:
                return None

            abs_child = _EbmlElement(
                element_id=child.element_id,
                offset=base_offset + child.offset,
                id_len=child.id_len,
                size_offset=base_offset + child.size_offset,
                size=child.size,
                size_len=child.size_len,
                payload_offset=base_offset + child.payload_offset,
                unknown_size=False,
            )
            if child.element_id == self.options.muxing_app_id:
                mux = abs_child
            elif child.element_id == self.options.writing_app_id:
                writing = abs_child

            cursor = child.end

        if mux is None:
            return None
        return mux, writing

    @staticmethod
    def _decode_text(raw: bytes) -> str:
        return raw.decode("utf-8", errors="replace").rstrip(" \x00")

    # ------------------------------------------------------------------
    # EBML primitive parsing/encoding
    # ------------------------------------------------------------------

    @staticmethod
    def _ebml_vint_length(first_byte: int) -> int:
        mask = 0x80
        for length in range(1, 9):
            if first_byte & mask:
                return length
            mask >>= 1
        return 0

    def _read_ebml_id_from_file(self, fh: BinaryIO, offset: int, file_size: int) -> tuple[bytes, int]:
        if offset < 0 or offset >= file_size:
            raise ValueError("Offset EBML ID invalide.")
        first = self._read_exact(fh, offset, 1)[0]
        length = self._ebml_vint_length(first)
        if length == 0 or length > 4:
            raise ValueError("Longueur EBML ID invalide.")
        end = offset + length
        if end > file_size:
            raise ValueError("Données insuffisantes pour EBML ID.")
        return self._read_exact(fh, offset, length), length

    def _read_ebml_size_from_file(self, fh: BinaryIO, offset: int, file_size: int) -> tuple[int, int, bool]:
        if offset < 0 or offset >= file_size:
            raise ValueError("Offset EBML size invalide.")
        first = self._read_exact(fh, offset, 1)[0]
        length = self._ebml_vint_length(first)
        if length == 0 or length > 8:
            raise ValueError("Longueur EBML size invalide.")
        end = offset + length
        if end > file_size:
            raise ValueError("Données insuffisantes pour EBML size.")

        raw = self._read_exact(fh, offset, length)
        value = raw[0] & (0xFF >> length)
        for b in raw[1:]:
            value = (value << 8) | b

        max_value = (1 << (7 * length)) - 1
        return value, length, value == max_value

    def _read_ebml_element_from_file(self, fh: BinaryIO, offset: int, file_size: int) -> _EbmlElement:
        element_id, id_len = self._read_ebml_id_from_file(fh, offset, file_size)
        size_offset = offset + id_len
        size, size_len, unknown = self._read_ebml_size_from_file(fh, size_offset, file_size)
        payload_offset = size_offset + size_len

        if unknown:
            return _EbmlElement(
                element_id=element_id,
                offset=offset,
                id_len=id_len,
                size_offset=size_offset,
                size=0,
                size_len=size_len,
                payload_offset=payload_offset,
                unknown_size=True,
            )

        payload_end = payload_offset + size
        if payload_end > file_size:
            raise ValueError("Payload EBML hors plage.")

        return _EbmlElement(
            element_id=element_id,
            offset=offset,
            id_len=id_len,
            size_offset=size_offset,
            size=size,
            size_len=size_len,
            payload_offset=payload_offset,
            unknown_size=False,
        )

    def _read_ebml_id_from_bytes(self, data: bytes, offset: int) -> tuple[bytes, int]:
        if offset < 0 or offset >= len(data):
            raise ValueError("Offset EBML ID invalide.")
        length = self._ebml_vint_length(data[offset])
        if length == 0 or length > 4:
            raise ValueError("Longueur EBML ID invalide.")
        end = offset + length
        if end > len(data):
            raise ValueError("Données insuffisantes pour EBML ID.")
        return data[offset:end], length

    def _read_ebml_size_from_bytes(self, data: bytes, offset: int) -> tuple[int, int, bool]:
        if offset < 0 or offset >= len(data):
            raise ValueError("Offset EBML size invalide.")
        length = self._ebml_vint_length(data[offset])
        if length == 0 or length > 8:
            raise ValueError("Longueur EBML size invalide.")
        end = offset + length
        if end > len(data):
            raise ValueError("Données insuffisantes pour EBML size.")

        value = data[offset] & (0xFF >> length)
        for b in data[offset + 1:end]:
            value = (value << 8) | b

        max_value = (1 << (7 * length)) - 1
        return value, length, value == max_value

    def _read_ebml_element_from_bytes(self, data: bytes, offset: int) -> _EbmlElement:
        element_id, id_len = self._read_ebml_id_from_bytes(data, offset)
        size_offset = offset + id_len
        size, size_len, unknown = self._read_ebml_size_from_bytes(data, size_offset)
        payload_offset = size_offset + size_len

        if unknown:
            return _EbmlElement(
                element_id=element_id,
                offset=offset,
                id_len=id_len,
                size_offset=size_offset,
                size=0,
                size_len=size_len,
                payload_offset=payload_offset,
                unknown_size=True,
            )

        payload_end = payload_offset + size
        if payload_end > len(data):
            raise ValueError("Payload EBML hors plage.")

        return _EbmlElement(
            element_id=element_id,
            offset=offset,
            id_len=id_len,
            size_offset=size_offset,
            size=size,
            size_len=size_len,
            payload_offset=payload_offset,
            unknown_size=False,
        )

    @classmethod
    def _encode_ebml_size_prefer_length(cls, value: int, *, preferred_length: int) -> bytes:
        if preferred_length < 1 or preferred_length > 8:
            raise ValueError("Longueur EBML size préférée invalide.")
        for length in range(preferred_length, 9):
            try:
                return cls._encode_ebml_size(value, length=length)
            except ValueError:
                continue
        raise ValueError("Impossible d'encoder la taille EBML demandée.")

    @staticmethod
    def _encode_ebml_size(value: int, *, length: int) -> bytes:
        if value < 0:
            raise ValueError("Taille EBML négative.")
        if length < 1 or length > 8:
            raise ValueError("Longueur EBML size invalide.")
        max_known = (1 << (7 * length)) - 2
        if value > max_known:
            raise ValueError("Valeur EBML trop grande pour la longueur demandée.")

        raw = value.to_bytes(length, "big")
        marker = 1 << (8 - length)
        return bytes([raw[0] | marker]) + raw[1:]

    @staticmethod
    def _minimal_size_length_for_value(value: int) -> int:
        if value < 0:
            raise ValueError("Valeur négative")
        for length in range(1, 9):
            if value <= (1 << (7 * length)) - 2:
                return length
        raise ValueError("Valeur EBML trop grande")

    @staticmethod
    def _encode_uint(value: int) -> bytes:
        if value < 0:
            raise ValueError("Valeur uint négative")
        if value == 0:
            return b"\x00"
        n = (value.bit_length() + 7) // 8
        return value.to_bytes(n, "big")

    def _build_void_element(self, total_size: int) -> bytes:
        if total_size < 2:
            raise ValueError("Un Void occupe au minimum 2 octets.")
        if total_size < 9:
            payload_size = total_size - 2
            size = self._encode_ebml_size(payload_size, length=1)
            out = _VOID_ID + size + (b"\x00" * payload_size)
            if len(out) != total_size:
                raise ValueError("Construction Void invalide (taille).")
            return out

        payload_size = total_size - 9
        size = self._encode_ebml_size(payload_size, length=8)
        out = _VOID_ID + size + (b"\x00" * payload_size)
        if len(out) != total_size:
            raise ValueError("Construction Void invalide (taille).")
        return out

    # ------------------------------------------------------------------
    # IO helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _file_size(fh: BinaryIO) -> int:
        cur = fh.tell()
        end = fh.seek(0, 2)
        fh.seek(cur)
        return end

    @staticmethod
    def _element_span(e: _EbmlElement) -> int:
        # For actively removed entries we normalize header lengths to 0.
        if e.id_len == 0 and e.size_len == 0 and e.size == 0:
            return 0
        return e.header_len + e.size

    @staticmethod
    def _mark_removed(e: _EbmlElement) -> None:
        e.size = 0
        e.id_len = 0
        e.size_len = 0
        e.size_offset = e.offset
        e.payload_offset = e.offset
        e.unknown_size = False
        e.unresolved_size = False

    @staticmethod
    def _read_exact(fh: BinaryIO, offset: int, size: int) -> bytes:
        if size < 0:
            raise ValueError("Taille de lecture négative.")
        fh.seek(offset)
        data = fh.read(size)
        if len(data) != size:
            raise ValueError("Lecture tronquée.")
        return data

    @staticmethod
    def _write_at(fh: BinaryIO, offset: int, data: bytes) -> None:
        fh.seek(offset)
        fh.write(data)

    @staticmethod
    def _find_pattern_in_file(fh: BinaryIO, pattern: bytes, file_size: int) -> int:
        if not pattern:
            return -1
        chunk_size = 1024 * 1024
        overlap = max(0, len(pattern) - 1)
        pos = 0
        tail = b""

        while pos < file_size:
            fh.seek(pos)
            chunk = fh.read(min(chunk_size, file_size - pos))
            if not chunk:
                break
            blob = tail + chunk
            idx = blob.find(pattern)
            if idx >= 0:
                return pos - len(tail) + idx
            pos += len(chunk)
            tail = blob[-overlap:] if overlap else b""

        return -1

    @staticmethod
    def _find_entry_index(state: _AnalyzerState, element_id: bytes, offset: int) -> int:
        for i, e in enumerate(state.data):
            if e.element_id == element_id and e.offset == offset:
                return i
        return -1
