"""Architecture guards for the native Matroska package."""

from __future__ import annotations

import ast
import importlib
import importlib.util
from pathlib import Path

import pytest

import core.matroska.reader as reader_module
from core.matroska.editors.language import MatroskaLanguageEditor
from core.matroska.editors.segment_info import MatroskaSegmentInfoHeaderEditor
from core.matroska.reader import MatroskaAttachmentHeader, MatroskaReader
from core.workflows.common.matroska_finalize import (
    MatroskaLanguagePostAction,
    MatroskaMuxingAppPostAction,
)
from core.workflows.encode.runtime.bindings import (
    SignalBindingService,
    SignalBindingServiceCallbacks,
)


ROOT = Path(__file__).resolve().parents[1]
MATROSKA_ROOT = ROOT / "core" / "matroska"

MATROSKA_MODULES = (
    "core.matroska.ebml",
    "core.matroska.ids",
    "core.matroska.reader",
    "core.matroska.writer",
    "core.matroska.mux_plan",
    "core.matroska.native_muxer",
    "core.matroska.assembly",
    "core.matroska.contract",
    "core.matroska.language",
    "core.matroska.validation",
    "core.matroska.timestamps",
    "core.matroska.editors.segment_info",
    "core.matroska.editors.language",
    "core.matroska.editors.dovi",
    "core.matroska.editors.video_timecodes",
    "core.matroska.hevc.access_units",
    "core.matroska.hevc.payload_rewriter",
    "core.matroska.hevc.timing_skeleton",
)

_OLD_WORKFLOWS_PACKAGE = "core." + "workflows"
REMOVED_MODULES = tuple(
    f"{_OLD_WORKFLOWS_PACKAGE}.{name}"
    for name in (
        "ebml_writer",
        "matroska_element_ids",
        "matroska_reader",
        "matroska_writer",
        "matroska_mux_plan",
        "matroska_native_muxer",
        "matroska_assembly",
        "matroska_header_editor",
        "matroska_language_editor",
        "matroska_dovi_block_addition",
        "matroska_timestamp_reader",
        "matroska_video_timecode_patcher",
        "matroska_hevc_au_splitter",
        "matroska_hevc_payload_rewriter",
        "matroska_output_validation",
        "matroska_output_transaction",
    )
) + ("core." + "matroska_attachment_extractor",)


@pytest.mark.parametrize("module_name", MATROSKA_MODULES)
def test_matroska_modules_are_importable(module_name: str) -> None:
    assert importlib.import_module(module_name).__name__ == module_name


@pytest.mark.parametrize("module_name", REMOVED_MODULES)
def test_removed_module_has_no_compatibility_alias(module_name: str) -> None:
    assert importlib.util.find_spec(module_name) is None


def test_removed_modules_have_no_source_files() -> None:
    workflows = ROOT / "core" / "workflows"
    assert not (workflows / "ebml_writer.py").exists()
    assert list(workflows.glob("matroska_*.py")) == []
    assert not (ROOT / "core" / "matroska_attachment_extractor.py").exists()


def test_matroska_package_does_not_depend_on_workflows() -> None:
    violations: list[str] = []
    for source in MATROSKA_ROOT.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("core.workflows"):
                violations.append(f"{source.relative_to(ROOT)}:{node.lineno}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("core.workflows"):
                        violations.append(f"{source.relative_to(ROOT)}:{node.lineno}")
    assert violations == []


def test_package_initializers_do_not_reexport_symbols() -> None:
    for source in (
        MATROSKA_ROOT / "__init__.py",
        MATROSKA_ROOT / "editors" / "__init__.py",
        MATROSKA_ROOT / "hevc" / "__init__.py",
    ):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in tree.body)


def test_contract_types_are_not_exposed_by_validation_module() -> None:
    validation = importlib.import_module("core.matroska.validation")
    assert not hasattr(validation, "MatroskaOutputContract")
    assert not hasattr(validation, "ExpectedMatroskaTrack")
    validation_source = (MATROSKA_ROOT / "validation.py").read_text(encoding="utf-8")
    assert ".editors" not in validation_source


def test_editors_do_not_expose_workflow_post_actions() -> None:
    language = importlib.import_module("core.matroska.editors.language")
    segment_info = importlib.import_module("core.matroska.editors.segment_info")
    assert not hasattr(language, "MatroskaLanguagePostAction")
    assert not hasattr(segment_info, "MatroskaMuxingAppPostAction")
    assert not hasattr(MatroskaLanguagePostAction, "bind_on_success")
    assert not hasattr(MatroskaMuxingAppPostAction, "bind_on_success")
    assert MatroskaLanguagePostAction(editor=MatroskaLanguageEditor())
    assert MatroskaMuxingAppPostAction(editor=MatroskaSegmentInfoHeaderEditor())


def test_signal_bindings_have_no_container_post_patch_hooks() -> None:
    callback_fields = set(SignalBindingServiceCallbacks.__dataclass_fields__)
    assert callback_fields == {"write_nfo", "remove_path"}
    assert not hasattr(SignalBindingService, "bind_matroska_segment_muxing_patch")
    for source in (
        ROOT / "core" / "workflows" / "encode" / "workflow.py",
        ROOT / "core" / "workflows" / "encode" / "runtime" / "bindings.py",
    ):
        text = source.read_text(encoding="utf-8")
        assert "bind_on_success" not in text
        assert "include_segment_patch" not in text


def test_attachment_data_reads_only_selected_payload(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "attachments.mkv"
    source.write_bytes(b"xAAAAselected")
    reader = MatroskaReader(source)
    headers = [
        MatroskaAttachmentHeader(1, "first.bin", "application/octet-stream", "", 4, 1),
        MatroskaAttachmentHeader(2, "selected.bin", "application/octet-stream", "", 8, 5),
    ]
    monkeypatch.setattr(reader, "attachment_headers", lambda: headers)
    reads: list[tuple[int, int]] = []
    original_read_exact = reader_module._read_exact

    def _record_read(handle, count: int) -> bytes:
        reads.append((handle.tell(), count))
        return original_read_exact(handle, count)

    monkeypatch.setattr(reader_module, "_read_exact", _record_read)

    assert reader.attachment_data(1) == b"selected"
    assert reads == [(5, 8)]


def test_attachment_data_rejects_invalid_index(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "attachments.mkv"
    source.write_bytes(b"x")
    reader = MatroskaReader(source)
    monkeypatch.setattr(reader, "attachment_headers", lambda: [])

    with pytest.raises(ValueError, match="négatif"):
        reader.attachment_data(-1)
    with pytest.raises(ValueError, match="introuvable"):
        reader.attachment_data(0)
