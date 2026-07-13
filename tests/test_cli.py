from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import cli.main
from cli.commands import cmd_tools
from cli.batch import discover_direct_batch_jobs, run_batch
from cli.contract import validate_batch_contract, validate_job_contract
from cli.errors import CliError, ContractError
from cli.jobs import load_job
from cli.options import JobOverrides
from cli.parser import build_parser
from cli.profile import profile_validate, resolve_decision_profile_path
from cli.schema import (
    build_cli_json_schema,
    build_cli_json_schema_bundle,
    build_decision_profile_schema_v1,
    build_exact_job_schema_v1,
)
from cli.tmdb import inferred_season_episode, mark_auto_tmdb, normalized_tmdb_options


def test_cli_parser_exposes_expected_subcommands() -> None:
    parser = build_parser()
    args = parser.parse_args(["preview", "--config", "job.json", "--json"])
    assert args.command == "preview"
    assert args.json_output is True
    args = parser.parse_args(["validate", "--config", "job.json", "--json"])
    assert args.json_output is True
    args = parser.parse_args(["batch", "--template", "template.json", "-i", "a.mkv"])
    assert args.command == "batch"
    args = parser.parse_args(["batch", "--template", "template.json", "-i", "a.mkv", "--summary", "summary.json"])
    assert args.summary == "summary.json"
    args = parser.parse_args(
        [
            "batch",
            "--template",
            "template.json",
            "--input-dir",
            "season",
            "--recursive",
            "--include",
            "*.mkv",
            "--exclude",
            "*sample*",
        ]
    )
    assert args.input_dir == ["season"]
    assert args.recursive is True
    assert args.include == ["*.mkv"]
    assert args.exclude == ["*sample*"]
    args = parser.parse_args(["schema", "--output", "schema.json"])
    assert args.command == "schema"
    assert args.output == "schema.json"
    args = parser.parse_args(["schema", "--version", "decision-profile"])
    assert args.schema_version == "decision-profile"
    args = parser.parse_args(["run", "--config", "job.json", "--dry-run"])
    assert args.command == "run"
    assert args.dry_run is True
    args = parser.parse_args(["preview", "--profile", "p.json", "-i", "a.mkv", "--json"])
    assert args.command == "preview"
    assert args.profile == "p.json"
    args = parser.parse_args(["batch", "--profile", "p.json", "--input-dir", "season", "--output-dir", "out", "--auto-tmdb", "--no-cover", "--tmdb-apikey", "ABCDEF"])
    assert args.command == "batch"
    assert args.profile == "p.json"
    assert args.auto_tmdb is True
    assert args.no_cover is True
    assert args.tmdb_apikey == "ABCDEF"
    args = parser.parse_args(["profile", "preview", "--profile", "p.json", "-i", "a.mkv", "--json", "--tmdb-id", "123", "--no-attach"])
    assert args.command == "profile"
    assert args.profile_command == "preview"
    assert args.json_output is True
    assert args.tmdb_id == 123
    assert args.no_attach is True
    args = parser.parse_args(["tools"])
    assert args.command == "tools"
    assert args.func is cmd_tools


def test_cli_tools_exposes_resolved_commands_as_structured_json(capsys) -> None:
    config = SimpleNamespace(
        tool_commands=lambda: {
            "ffmpeg": "/opt/muxiveo/ffmpeg",
            "ffprobe": "/opt/muxiveo/ffprobe",
            "mediainfo": "/opt/muxiveo/mediainfo",
        }
    )

    assert cmd_tools(SimpleNamespace(), config, SimpleNamespace()) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "version": 1,
        "tools": {
            "ffmpeg": "/opt/muxiveo/ffmpeg",
            "ffprobe": "/opt/muxiveo/ffprobe",
            "mediainfo": "/opt/muxiveo/mediainfo",
        },
    }


def test_cli_main_only_exposes_entrypoint() -> None:
    assert cli.main.__all__ == ["main"]
    assert callable(cli.main.main)
    for moved_name in (
        "build_cli_json_schema",
        "build_parser",
        "validate_batch_contract",
        "validate_job_contract",
    ):
        assert not hasattr(cli.main, moved_name)


def test_cli_json_schema_exposes_public_contract_keys() -> None:
    schema = build_cli_json_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["version"] == {"const": 1}
    assert "rules" not in schema["properties"]
    for key in ("sources", "output", "output_template", "output_all", "tracks", "track_order", "audio_variants", "chapters", "tmdb"):
        assert key in schema["properties"]
    assert "rules" not in schema["$defs"]
    for definition in ("source", "selector", "track_edit", "track_order_item", "audio_variant", "chapters", "tmdb"):
        assert definition in schema["$defs"]
    assert "auto_detect_episode" in schema["$defs"]["tmdb"]["properties"]
    assert "auto_metadata" in schema["$defs"]["tmdb"]["properties"]


def test_cli_json_schema_bundle_excludes_removed_v2_contract() -> None:
    bundle = build_cli_json_schema_bundle()
    assert len(bundle["oneOf"]) == 3
    assert not any(item.get("properties", {}).get("version") == {"const": 2} for item in bundle["oneOf"])


def test_profile_schemas_expose_exact_and_decision_contracts() -> None:
    exact = build_exact_job_schema_v1()
    assert exact["properties"]["kind"] == {"const": "exact-job"}
    assert "variables" in exact["properties"]
    decision = build_decision_profile_schema_v1()
    assert decision["properties"]["kind"] == {"const": "decision-profile"}
    assert "expr" in decision["$defs"]["condition"]["properties"]
    assert "aliases" in decision["properties"]["variables"]["properties"]
    assert "rule" in decision["$defs"]
    assert "action" in decision["$defs"]


def test_cli_profile_path_resolution_adds_json_and_uses_default_dir(tmp_path: Path) -> None:
    direct = tmp_path / "direct.json"
    default_dir = tmp_path / "profiles" / "decision"
    default_dir.mkdir(parents=True)
    default_profile = default_dir / "BestOfAll.json"
    payload = {
        "version": 1,
        "kind": "decision-profile",
        "name": "BestOfAll",
        "rules": [],
    }
    direct.write_text(json.dumps(payload), encoding="utf-8")
    default_profile.write_text(json.dumps(payload), encoding="utf-8")
    config: Any = SimpleNamespace(profiles_dir=tmp_path / "profiles")

    assert resolve_decision_profile_path(tmp_path / "direct", config) == direct
    assert resolve_decision_profile_path("BestOfAll", config) == default_profile
    assert profile_validate("BestOfAll", config=config) == 0


def test_cli_metadata_overrides_enable_tmdb_and_disable_attachments() -> None:
    job = load_job(
        JobOverrides(
            input=["source.mkv"],
            output="out.mkv",
            auto_tmdb=True,
            tmdb_id=123,
            no_cover=True,
            no_attach=True,
        )
    )

    assert job["tmdb"]["enabled"] is True
    assert job["tmdb"]["id"] == 123
    assert job["tmdb"]["cover"] is False
    assert job["sources"] == [{"path": "source.mkv", "attachments": "none"}]
    assert job["extra_attachments"] == []


def test_cli_tmdb_apikey_override_injects_into_job_block() -> None:
    job = load_job(
        JobOverrides(
            input=["source.mkv"],
            output="out.mkv",
            tmdb_apikey="DEADBEEF",
        )
    )

    assert job["tmdb"]["enabled"] is True
    assert job["tmdb"]["api_key"] == "DEADBEEF"


def test_output_template_sanitizes_forbidden_chars() -> None:
    from cli.output_template import sanitize_token

    assert sanitize_token("a/b:c") == "a.b.c"
    assert sanitize_token('"foo<bar>"') == ".foo.bar.."
    assert sanitize_token("") == ""


def test_output_template_extracts_release_group() -> None:
    from cli.output_template import extract_release_group

    assert extract_release_group("Movie.2024.1080p.WEB.x264-RARBG") == "RARBG"
    assert extract_release_group("Show.S01E02.NTb") == "NTb"
    assert extract_release_group("noextension") == ""


def test_output_template_renders_with_full_context() -> None:
    from core.media_info_fetcher import MediaDetails
    from cli.output_template import build_output_context, render_output_template

    details = MediaDetails(title="X", year="2024", season="1", episode="2", episode_title="Pilot")
    ctx = build_output_context(Path("Foo.S01E02-NTb.mkv"), details)
    out = render_output_template(
        "{title}.{year}.S{season}E{episode}.{episode_title}-{group}", ctx
    )
    assert out == "X.2024.S01E02.Pilot-NTb.mkv"


def test_output_template_unknown_token_renders_empty() -> None:
    from cli.output_template import render_output_template

    out = render_output_template("{title}.{unknown}.mkv", {"title": "A"})
    assert out == "A.mkv"


def test_output_template_numeric_variants() -> None:
    from core.media_info_fetcher import MediaDetails
    from cli.output_template import build_output_context, render_output_template

    details = MediaDetails(title="X", year="2024", season="1", episode="2")
    ctx = build_output_context(Path("foo.mkv"), details)
    assert render_output_template("S{season_num:03d}", ctx) == "S001.mkv"


def test_output_template_appends_default_mkv_extension() -> None:
    from cli.output_template import render_output_template

    out = render_output_template("{title}", {"title": "Bar"})
    assert out == "Bar.mkv"


def test_output_template_keeps_explicit_extension() -> None:
    from cli.output_template import render_output_template

    out = render_output_template("{title}.mp4", {"title": "Bar"})
    assert out == "Bar.mp4"


def test_output_template_preserves_relative_path_components() -> None:
    from cli.output_template import render_output_template

    assert render_output_template("./out/{title}", {"title": "Bar"}) == "./out/Bar.mkv"
    assert render_output_template("../out/{title}", {"title": "Bar"}) == "../out/Bar.mkv"


def test_output_template_supports_release_title_modifier() -> None:
    from core.media_info_fetcher import MediaDetails
    from cli.output_template import build_output_context, render_output_template

    details = MediaDetails(title="Été Violent: Le Retour", year="2024", episode_title="L'été revient")
    ctx = build_output_context(Path("source.mkv"), details)

    assert render_output_template("{title:release}.{year}", ctx) == "Ete.Violent.Le.Retour.2024.mkv"
    assert render_output_template("{episode_title:release}", ctx) == "Lete.revient.mkv"
    assert render_output_template("{title:release}", {"title": "Des œufs et du æther"}) == "Des.oeufs.et.du.aether.mkv"


def test_output_template_track_keywords_best_all_and_output_all() -> None:
    from cli.output_template import build_output_context, render_output_template
    from core.workflows.remux_models import TrackEntry

    tracks = [
        TrackEntry(0, "video", "HEVC", "3840x2160  HDR10+", "", "", file_id="src0"),
        TrackEntry(1, "audio", "EAC3", "5.1  640 kbps", "fr-FR", "VF", file_id="src0"),
        TrackEntry(2, "audio", "TRUEHD", "7.1  4000 kbps  Atmos", "en-US", "VO", file_id="src0"),
        TrackEntry(3, "subtitle", "SUBRIP", "", "fr-FR", "Forced", file_id="src0"),
    ]
    order = [(0, 0, tracks[0].entry_id), (0, 1, tracks[1].entry_id), (0, 2, tracks[2].entry_id), (0, 3, tracks[3].entry_id)]
    ctx = build_output_context(Path("Movie.2024.WEB-DL-GRP.mkv"), None, tracks=tracks, track_order=order)

    assert render_output_template("{audio-lang:all}.{sub-lang:all}", ctx) == "fr-FR+en-US.fr-FR.mkv"
    assert render_output_template("{audio-codec-release:best}.{audio-channels:best}.{audio-immersive}", ctx) == "TrueHD.7.1.Atmos.mkv"
    assert render_output_template("{video-source}.{video-resolution:best}.{video-10bit}.{video-hdr:best}.{video-codec-release:best}-{group}", ctx) == "WEB.2160p.10Bits.HDR10P.x265-GRP.mkv"

    output_all_ctx = build_output_context(
        Path("Movie.2024.WEB-DL-GRP.mkv"),
        None,
        tracks=tracks,
        track_order=order,
        output_all=True,
    )
    assert render_output_template("{audio-lang:best}", output_all_ctx) == "fr-FR+en-US.mkv"
    assert render_output_template("{audio-codec-release:best}", output_all_ctx) == "DDP+TrueHD.mkv"


def test_output_template_ignores_disabled_tracks() -> None:
    from cli.output_template import build_output_context, render_output_template
    from core.workflows.remux_models import TrackEntry

    disabled = TrackEntry(1, "audio", "TRUEHD", "7.1  Atmos", "en-US", "VO", enabled=False, file_id="src0")
    enabled = TrackEntry(2, "audio", "AC3", "5.1", "fr-FR", "VF", file_id="src0")
    ctx = build_output_context(Path("Movie.mkv"), None, tracks=[disabled, enabled], track_order=[(0, 1, disabled.entry_id), (0, 2, enabled.entry_id)])

    assert render_output_template("{audio-lang:all}.{audio-codec-release:best}", ctx) == "fr-FR.AC3.mkv"


def test_output_template_applies_aliases_and_regional_lang_names() -> None:
    from cli.output_template import build_output_context, render_output_template
    from core.workflows.remux_models import TrackEntry

    tracks = [
        TrackEntry(1, "audio", "EAC3", "5.1  640 kbps", "fr-FR", "VFF", file_id="src0"),
        TrackEntry(2, "audio", "AC3", "5.1  640 kbps", "fr-CA", "VFQ", file_id="src0"),
    ]
    order = [(0, 1, tracks[0].entry_id), (0, 2, tracks[1].entry_id)]
    ctx = build_output_context(
        Path("Movie.mkv"),
        None,
        tracks=tracks,
        track_order=order,
        variables={
            "aliases": {
                "*": {"EAC3": "DDP", "French": "FR"},
                "lang_name": {"French": "Français"},
            }
        },
    )

    assert render_output_template("{audio-codec:best}.{audio-lang-name:first}.{audio-lang-name:all}", ctx) == "DDP.Français.Français+French (Canada).mkv"


def test_cli_parser_exposes_output_template() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["remux", "--config", "j.json", "--output-template", "{title}.mkv", "--output-all"]
    )
    assert args.output_template == "{title}.mkv"
    assert args.output_all is True


def test_load_job_injects_output_template_into_job() -> None:
    job = load_job(
        JobOverrides(
            input=["a.mkv"],
            output_template="{source_name}.X.mkv",
            output_all=True,
        )
    )
    assert job["output_template"] == "{source_name}.X.mkv"
    assert job["output_all"] is True


def test_cli_tmdb_options_infer_season_episode_like_gui() -> None:
    source = Path("Devil.May.Cry.2025.S01E02.MULTi.1080p.WEB.x264.mkv")

    assert inferred_season_episode(source) == ("1", "2")
    tmdb = normalized_tmdb_options({"tmdb": {"enabled": True}}, source)

    assert tmdb is not None
    assert tmdb["season"] == "1"
    assert tmdb["episode"] == "2"
    assert tmdb["kind"] == "tv"


def test_cli_auto_tmdb_overrides_exported_episode_from_source_filename() -> None:
    source = Path("Spider-Noir.S01E02.MULTi.VF2.HDR.DV.2160p.WEB.H265-SUPPLY.mkv")
    job = {"tmdb": {"enabled": True, "kind": "tv", "season": "1", "episode": "1"}}
    mark_auto_tmdb(job["tmdb"])

    tmdb = normalized_tmdb_options(
        job,
        source,
        source_title="Spider Noir - S01E01 - Pilot",
    )

    assert tmdb is not None
    assert tmdb["season"] == "1"
    assert tmdb["episode"] == "2"
    assert tmdb["kind"] == "tv"


def test_cli_tmdb_auto_season_episode_prefers_source_filename() -> None:
    source = Path("Spider-Noir.S01E03.MULTi.VF2.HDR.DV.2160p.WEB.H265-SUPPLY.mkv")

    tmdb = normalized_tmdb_options(
        {"tmdb": {"enabled": True, "kind": "tv", "season": "auto", "episode": "auto"}},
        source,
        source_title="Spider Noir - S01E01 - Pilot",
    )

    assert tmdb is not None
    assert tmdb["season"] == "1"
    assert tmdb["episode"] == "3"


def test_auto_tmdb_metadata_replaces_exported_gui_tags_and_title() -> None:
    from cli.remux_config import merge_tmdb_tag_overrides, resolve_metadata_file_title

    explicit_tags = {
        "EPISODE": "1",
        "SYNOPSIS": "Old episode",
        "ENCODER_SETTINGS": "muxiveo gui export",
    }
    tmdb_tags = {"EPISODE": "2", "SYNOPSIS": "Fresh episode"}

    assert resolve_metadata_file_title({"file_title": "Old"}, "Fresh", tmdb_wins=True) == "Fresh"
    assert merge_tmdb_tag_overrides(tmdb_tags, explicit_tags, tmdb_wins=True) == {
        "EPISODE": "2",
        "SYNOPSIS": "Fresh episode",
        "ENCODER_SETTINGS": "muxiveo gui export",
    }
    merged = merge_tmdb_tag_overrides(tmdb_tags, explicit_tags, tmdb_wins=False)
    assert merged is not None and merged["EPISODE"] == "1"


def test_documented_cli_json_examples_are_valid() -> None:
    root = Path(__file__).resolve().parent.parent
    for path in (root / "docs" / "cli").glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict)


def test_validate_job_contract_ignores_legacy_exact_job_rules_key() -> None:
    validate_job_contract(
        {
            "version": 1,
            "sources": [{"path": "source.mkv"}],
            "output": "out.mkv",
            "rules": {"tracks": {"audio": {"languages": ["fr-FR", 42]}}},
        },
        require_version=True,
    )


def test_validate_job_contract_requires_version_for_json_files() -> None:
    with pytest.raises(ContractError) as excinfo:
        validate_job_contract(
            {"sources": [{"path": "source.mkv"}], "output": "out.mkv"},
            require_version=True,
        )

    assert "$.version: champ requis" in str(excinfo.value)


def test_validate_job_contract_accepts_exact_job_selectors() -> None:
    validate_job_contract(
        {
            "version": 1,
            "kind": "exact-job",
            "sources": [{"path": "source.mkv"}],
            "output": "out.mkv",
            "tracks": [
                {
                    "selector": {"source": 0, "type": "audio", "position": 0, "codec": "EAC3"},
                    "enabled": True,
                    "language": "fr-FR",
                }
            ],
            "track_order": [{"selector": {"source": 0, "type": "video", "position": 0}}],
            "audio_variants": [
                {
                    "source_selector": {"source": 0, "type": "audio", "position": 0},
                    "codec": "eac3",
                    "bitrate_kbps": 640,
                }
            ],
            "fallback_profile": {"name": "series-fr"},
        },
        require_version=True,
    )


def test_validate_job_contract_rejects_invalid_chapter_shape() -> None:
    with pytest.raises(ContractError) as excinfo:
        validate_job_contract(
            {
                "version": 1,
                "sources": [{"path": "source.mkv"}],
                "output": "out.mkv",
                "chapters": {"add": [{"chaptername": "No timestamp"}]},
            },
            require_version=True,
        )

    assert "chapters.add[0].timestamp" in str(excinfo.value)


def test_validate_job_contract_reports_track_order_paths() -> None:
    with pytest.raises(ContractError) as excinfo:
        validate_job_contract(
            {
                "version": 1,
                "sources": [{"path": "source.mkv"}],
                "output": "out.mkv",
                "track_order": [
                    {"source": "zero"},
                    [0],
                    [0, "audio"],
                    7,
                ],
            },
            require_version=True,
        )

    message = str(excinfo.value)
    assert "track_order[0].source" in message
    assert "track_order[0].id" in message
    assert "track_order[1]" in message
    assert "track_order[2][1]" in message
    assert "track_order[3]" in message


def test_validate_batch_contract_reports_job_paths() -> None:
    with pytest.raises(ContractError) as excinfo:
        validate_batch_contract(
            {
                "jobs": [
                    {"sources": [{"path": "source.mkv"}], "tracks": "bad"},
                    42,
                ]
            }
        )

    message = str(excinfo.value)
    assert "$.jobs[0].tracks" in message
    assert "$.jobs[1]" in message


def test_batch_rejects_json_batch_mixed_with_direct_inputs() -> None:
    with pytest.raises(CliError) as excinfo:
        run_batch(
            template_path="missing-template.json",
            batch_path="batch.json",
            cli_inputs=["a.mkv"],
            input_dirs=None,
            recursive=False,
            include_patterns=None,
            exclude_patterns=None,
            output_dir=None,
            dry_run=True,
            force=False,
            continue_on_error=False,
            summary_path=None,
            config=None,  # type: ignore[arg-type]
            options=None,  # type: ignore[arg-type]
            logger=None,  # type: ignore[arg-type]
        )

    assert "--batch" in str(excinfo.value)
    assert "--input-dir" in str(excinfo.value)


def test_discover_direct_batch_jobs_filters_video_and_preserves_relative_outputs(tmp_path: Path) -> None:
    root = tmp_path / "in"
    nested = root / "Saison 01"
    nested.mkdir(parents=True)
    for path in (
        root / "E00.sample.mkv",
        root / "E01.mkv",
        root / "E01.srt",
        root / "audio.flac",
        nested / "E02.mp4",
        nested / "E03.avi",
    ):
        path.write_bytes(b"")

    non_recursive = discover_direct_batch_jobs(
        input_dirs=[str(root)],
        output_dir=str(tmp_path / "out"),
        recursive=False,
    )
    assert [Path(job["sources"][0]["path"]).name for job in non_recursive.jobs] == [
        "E00.sample.mkv",
        "E01.mkv",
    ]

    recursive = discover_direct_batch_jobs(
        input_dirs=[str(root)],
        output_dir=str(tmp_path / "out"),
        recursive=True,
        include_patterns=["*.mkv", "Saison 01/*.mp4"],
        exclude_patterns=["*sample*"],
    )

    assert [Path(job["sources"][0]["path"]).as_posix().split("/in/")[-1] for job in recursive.jobs] == [
        "E01.mkv",
        "Saison 01/E02.mp4",
    ]
    assert [Path(job["output"]).as_posix().split("/out/")[-1] for job in recursive.jobs] == [
        "E01.mkv",
        "Saison 01/E02.mkv",
    ]
    assert recursive.scanned == 6
    assert recursive.selected == 2


def test_discover_direct_batch_jobs_accepts_input_dir_globs(tmp_path: Path) -> None:
    base = tmp_path / "downloads"
    first = base / "Spider-Noir.S01E01.MULTi.VF2.HDR.DV.2160p.WEB.H265-SUPPLY"
    second = base / "Spider-Noir.S01E02.MULTi.VF2.HDR.DV.2160p.WEB.H265-SUPPLY"
    other = base / "Other.Show.S01E01"
    for folder in (first, second, other):
        folder.mkdir(parents=True)
    (first / "Spider-Noir.S01E01.mkv").write_bytes(b"")
    (second / "Spider-Noir.S01E02.mkv").write_bytes(b"")
    (other / "Other.Show.S01E01.mkv").write_bytes(b"")

    discovery = discover_direct_batch_jobs(
        input_dirs=[str(base / "Spider-Noir.S*E*.MULTi.VF2.HDR.DV.2160p.WEB.H265-SUPPLY")],
        output_dir=str(tmp_path / "out"),
    )

    assert [Path(job["sources"][0]["path"]).name for job in discovery.jobs] == [
        "Spider-Noir.S01E01.mkv",
        "Spider-Noir.S01E02.mkv",
    ]
    assert discovery.roots == [str(first), str(second)]
    assert discovery.selected == 2


def test_discover_direct_batch_jobs_errors_when_no_video_matches(tmp_path: Path) -> None:
    root = tmp_path / "in"
    root.mkdir()
    (root / "subtitle.srt").write_text("1\n", encoding="utf-8")
    template = tmp_path / "template.json"
    template.write_text(json.dumps({"version": 1}), encoding="utf-8")

    with pytest.raises(CliError) as excinfo:
        run_batch(
            template_path=str(template),
            batch_path=None,
            cli_inputs=None,
            input_dirs=[str(root)],
            recursive=False,
            include_patterns=None,
            exclude_patterns=None,
            output_dir=None,
            dry_run=True,
            force=False,
            continue_on_error=False,
            summary_path=None,
            config=None,  # type: ignore[arg-type]
            options=None,  # type: ignore[arg-type]
            logger=None,  # type: ignore[arg-type]
        )

    assert "Aucun fichier vidéo compatible" in str(excinfo.value)
