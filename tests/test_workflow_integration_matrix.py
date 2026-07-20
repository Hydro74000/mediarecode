from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import scripts.run_workflow_integration_matrix as matrix


def test_parse_args_requires_real_existing_sources(tmp_path: Path) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"media")
    args = matrix.parse_args([
        "--sdr-source", str(source),
        "--dv-source", str(source),
        "--sample-duration", "4.5",
    ])
    assert args.sdr_source == source.resolve()
    assert args.dv_source == source.resolve()
    assert args.sample_duration == 4.5

    with pytest.raises(SystemExit):
        matrix.parse_args([])


def test_distrobox_wrapper_pins_inner_absolute_tool_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(matrix, "BIN_ROOT", tmp_path)
    monkeypatch.setattr(
        matrix.shutil,
        "which",
        lambda name: "/usr/bin/distrobox" if name == "distrobox" else None,
    )
    monkeypatch.setattr(
        matrix.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="/usr/bin/mkvmerge\n", stderr="",
        ),
    )

    wrapper = Path(matrix._resolve_tool("mkvmerge", distrobox_name="my-distrobox"))
    content = wrapper.read_text(encoding="utf-8")

    assert "'/usr/bin/distrobox' 'enter' '-n' 'my-distrobox'" in content
    assert "'/usr/bin/mkvmerge'" in content
    assert "'mkvmerge'" not in content
