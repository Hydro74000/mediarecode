from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.workflows.encode.models import AudioTrackSettings, EncodeConfig, EncodeError, QualityMode, VideoEncodeSettings
from core.workflows.encode.runtime.attachment_preparation import (
    AttachmentPreparationService,
    AttachmentPreparationServiceCallbacks,
)
from core.workflows.encode.runtime.storage_guard import (
    ensure_inject_storage_available,
    estimate_inject_storage_requirements,
)


def _make_config(source: Path, output: Path, **kwargs) -> EncodeConfig:
    config = EncodeConfig(
        source=source,
        output=output,
        video=kwargs.pop(
            "video",
            VideoEncodeSettings(codec="libx265", quality_mode=QualityMode.BITRATE, bitrate_kbps=8_000),
        ),
    )
    for key, value in kwargs.items():
        setattr(config, key, value)
    return config


def test_estimate_inject_storage_requirements_counts_audio_and_attachments(tmp_path):
    src = tmp_path / "src.mkv"
    src.write_bytes(b"\x00" * 200_000)
    attachment = tmp_path / "cover.jpg"
    attachment.write_bytes(b"\x01" * 4_096)
    cfg = _make_config(
        src,
        tmp_path / "out.mkv",
        audio_tracks=[AudioTrackSettings(stream_index=1, codec="aac", bitrate_kbps=192)],
        extra_attachments=[attachment],
    )

    work_required, output_required = estimate_inject_storage_requirements(
        cfg,
        probe_duration_seconds=lambda _source: 10.0,
        size_to_bitrate_kbps=lambda _config: pytest.fail("size mode should not be used"),
    )

    # Pic lot 3 avec squelette de timing : au plus 2 copies vidéo simultanées
    # + forfait squelette (payloads vides).
    assert work_required == (2 * 128 * 1024 * 1024) + (64 * 1024 * 1024) + (32 * 1024 * 1024)
    assert output_required == (128 * 1024 * 1024) + (64 * 1024 * 1024) + 240_000 + 4_096


def test_ensure_inject_storage_available_raises_on_same_fs_shortfall(tmp_path):
    src = tmp_path / "src.mkv"
    src.write_bytes(b"\x00")
    cfg = _make_config(src, tmp_path / "out.mkv", work_dir=tmp_path / "work")
    logs: list[str] = []

    with pytest.raises(EncodeError, match="Espace disque insuffisant"):
        ensure_inject_storage_available(
            cfg,
            estimate_requirements=lambda _config: (10_000, 20_000),
            log_info=logs.append,
            ram_buffer_enabled=False,
            ram_buffer_dir=lambda: None,
            disk_usage=cast(Any, lambda _path: SimpleNamespace(total=100_000, used=90_000, free=25_000)),
            stat=cast(Any, lambda _path: SimpleNamespace(st_dev=42)),
            temp_dir=lambda: str(tmp_path / "tmp"),
        )

    assert any("Estimation espace injection" in message for message in logs)


def test_attachment_preparation_service_materializes_all_selected_attachments(tmp_path):
    src = tmp_path / "src.mkv"
    src.touch()
    font = tmp_path / "font.ttf"
    extracted: list[tuple[Path, int, Path]] = []
    cfg = _make_config(
        src,
        tmp_path / "out.mkv",
        attachment_streams=[(src, 5), (src, 6)],
        extra_attachments=[font],
    )

    def _extract_attached_pic(source: Path, stream_idx: int, dest: Path, _signals: Any) -> None:
        extracted.append((source, stream_idx, dest))
        dest.write_bytes(b"img")

    def _extract_attachment_data(source: Path, stream_idx: int, dest: Path, _signals: Any) -> None:
        extracted.append((source, stream_idx, dest))
        dest.write_bytes(b"font")

    service = AttachmentPreparationService(
        AttachmentPreparationServiceCallbacks(
            check_cancelled=lambda _signals: None,
            describe_attachment_stream=lambda _source, stream_idx: (
                {"is_attached_pic": True, "filename": "cover.png", "mimetype": "image/png"}
                if stream_idx == 5
                else {"is_attached_pic": False, "filename": "font.ttf", "mimetype": "font/ttf"}
            ),
            attachment_filename=lambda meta, _stream_idx: str(meta["filename"]),
                unique_attachment_path=lambda tmp_dir, filename: tmp_dir / filename,
                extract_attached_pic=_extract_attached_pic,
                extract_attachment_data=_extract_attachment_data,
        )
    )

    prepared, cleanup_dir = service.prepare(cfg, work_dir=tmp_path)

    assert cleanup_dir is not None
    assert prepared.attachment_streams == []
    assert prepared.extra_attachments[0].name == "cover.png"
    assert prepared.extra_attachments[1].name == "font.ttf"
    assert prepared.extra_attachments[2] == font
    assert extracted == [
        (src, 5, Path(cleanup_dir) / "cover.png"),
        (src, 6, Path(cleanup_dir) / "font.ttf"),
    ]
    assert (Path(cleanup_dir) / "cover.png").exists()
