from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from core.matroska.ebml import ascii_element, element, uint_element
from core.matroska.ids import (
    CLUSTER_ID,
    CODEC_ID_ID,
    EBML_HEADER_ID,
    FLAG_DEFAULT_ID,
    FLAG_FORCED_ID,
    LANGUAGE_BCP47_ID,
    NAME_ID,
    SEGMENT_ID,
    SIMPLE_BLOCK_ID,
    TIMESTAMP_ID,
    TRACK_ENTRY_ID,
    TRACK_NUMBER_ID,
    TRACK_TYPE_ID,
    TRACK_UID_ID,
    TRACKS_ID,
)
from core.matroska.validation import validate_matroska_output
from core.workflows.common.track_types import TrackMetaPatch
from core.workflows.encode.models import AudioTrackSettings, EncodeConfig, VideoEncodeSettings
from core.workflows.encode.output_contract import build_encode_output_contract
from core.workflows.encode.planning.track_metadata import resolve_track_metadata
from core.workflows.encode.runtime.mux_assembly import (
    TrackMetadataArgsBuilder,
    TrackMetadataArgsBuilderCallbacks,
)
from core.workflows.encode.runtime.native_mux import (
    NativeVideoArtifactRef,
    build_encode_assembly_plan,
)


def _entry(
    number: int,
    track_type: int,
    codec: str,
    *,
    name: str = "",
    language: str = "und",
    default: bool = False,
    forced: bool = False,
) -> bytes:
    payload = b"".join((
        uint_element(TRACK_NUMBER_ID, number),
        uint_element(TRACK_UID_ID, number),
        uint_element(TRACK_TYPE_ID, track_type),
        ascii_element(CODEC_ID_ID, codec),
        ascii_element(NAME_ID, name),
        ascii_element(LANGUAGE_BCP47_ID, language),
        uint_element(FLAG_DEFAULT_ID, int(default)),
        uint_element(FLAG_FORCED_ID, int(forced)),
    ))
    return element(TRACK_ENTRY_ID, payload)


def _cluster(track_number: int, payload: bytes = b"payload") -> bytes:
    block = bytes([0x80 | track_number]) + b"\x00\x00\x80" + payload
    return element(CLUSTER_ID, uint_element(TIMESTAMP_ID, 0) + element(SIMPLE_BLOCK_ID, block))


def _mkv(path: Path, entries: list[bytes], clusters: bytes = b"") -> Path:
    path.write_bytes(
        element(EBML_HEADER_ID, b"")
        + SEGMENT_ID
        + b"\xff"
        + element(TRACKS_ID, b"".join(entries))
        + clusters
    )
    return path


def _config(source: Path, output: Path, **kwargs) -> EncodeConfig:
    values = dict(
        source=source,
        output=output,
        video=VideoEncodeSettings(source_path=source, stream_index=0, codec="libx265"),
        audio_tracks=[AudioTrackSettings(stream_index=1, codec="aac")],
        copy_subtitles=False,
        subtitle_tracks=[(source, 2)],
    )
    values.update(kwargs)
    return EncodeConfig(**values)


def test_source_values_and_positional_overrides_are_resolved_once(tmp_path: Path) -> None:
    source = _mkv(tmp_path / "source.mkv", [
        _entry(1, 1, "V_MPEGH/ISO/HEVC", name="Main Video", language="en-US", default=True),
        _entry(2, 2, "A_EAC3", name="French Atmos", language="fr-FR"),
        _entry(3, 17, "S_TEXT/UTF8", name="English SDH", language="en-US", forced=True),
    ])
    config = _config(
        source,
        tmp_path / "out.mkv",
        track_meta_edits=[
            TrackMetaPatch(track_order=1),
            TrackMetaPatch(track_order=2, title="", language="und", flag_default=True),
        ],
    )

    metadata = resolve_track_metadata(
        config,
        video_refs=[(source, 0)],
        subtitle_refs=[(source, 2)],
    )

    assert [(item.track_type, item.name, item.language) for item in metadata] == [
        ("video", "Main Video", "en-US"),
        ("audio", "", "und"),
        ("subtitle", "English SDH", "en-US"),
    ]
    assert metadata[0].flags is not None and metadata[0].flags.default is True
    assert metadata[1].flags is not None and metadata[1].flags.default is True
    assert metadata[2].flags is not None and metadata[2].flags.forced is True


def test_unknown_source_does_not_invent_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    config = _config(
        source,
        tmp_path / "out.mkv",
        audio_tracks=[],
        subtitle_tracks=[],
        track_meta_edits=[TrackMetaPatch(track_order=1, title="Explicit", language="jpn")],
    )

    metadata = resolve_track_metadata(config, video_refs=[(source, 0)], subtitle_refs=[])
    assert metadata[0].name == "Explicit"
    assert metadata[0].language == "jpn"
    assert metadata[0].flags is None


def test_ffmpeg_args_contract_and_native_plan_share_values(tmp_path: Path) -> None:
    source = _mkv(tmp_path / "source.mkv", [
        _entry(1, 1, "V_MPEGH/ISO/HEVC", name="Original Video", language="eng", default=True),
        _entry(2, 2, "A_EAC3", name="Original Audio", language="fra"),
    ])
    artifact = _mkv(
        tmp_path / "wrapped.mkv",
        [_entry(1, 1, "V_MPEGH/ISO/HEVC")],
        _cluster(1),
    )
    config = _config(
        source,
        tmp_path / "out.mkv",
        subtitle_tracks=[],
        audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy")],
    )
    metadata = resolve_track_metadata(config, video_refs=[(source, 0)], subtitle_refs=[])
    plan = SimpleNamespace(
        track_metadata=metadata,
        resolved_subtitle_tracks=(),
        subtitles_resolved=True,
    )

    args = TrackMetadataArgsBuilder(TrackMetadataArgsBuilderCallbacks(
        video_tracks=lambda cfg: [cfg.video],
        log_warn=lambda _message: None,
    )).build(config, plan=plan)
    contract = build_encode_output_contract(config, plan)
    assembly = build_encode_assembly_plan(
        config,
        video_artifacts=[NativeVideoArtifactRef(artifact)],
        materialized_audio={},
        resolved_subtitles=[],
        track_metadata=metadata,
    )

    assert "language=en-US" in args and "title=Original Video" in args
    assert "language=fr-FR" in args and "title=Original Audio" in args
    assert [(item.name, item.language) for item in contract.expected_tracks] == [
        ("Original Video", "eng"),
        ("Original Audio", "fra"),
    ]
    assert assembly.ordered_tracks[0].name == "Original Video"
    assert assembly.ordered_tracks[0].language_value == "eng"


def test_packet_diagnostic_rejects_empty_audio_not_empty_subtitle(tmp_path: Path) -> None:
    output = _mkv(
        tmp_path / "empty_tracks.mkv",
        [
            _entry(1, 1, "V_MPEG4/ISO/AVC"),
            _entry(2, 2, "A_AAC"),
            _entry(3, 17, "S_TEXT/UTF8"),
        ],
        _cluster(1),
    )
    source = output
    config = _config(source, tmp_path / "unused.mkv")
    metadata = resolve_track_metadata(
        config,
        video_refs=[(source, 0)],
        subtitle_refs=[(source, 2)],
    )
    plan = SimpleNamespace(
        track_metadata=metadata,
        resolved_subtitle_tracks=((source, 2),),
        subtitles_resolved=True,
    )
    errors = validate_matroska_output(output, build_encode_output_contract(config, plan))

    assert len(errors) == 1
    assert "#2 (audio, position de sortie #2)" in errors[0]
    assert "subtitle" not in errors[0]
