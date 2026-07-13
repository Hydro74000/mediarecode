"""CLI support for decision-profile v1."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.config import AppConfig
from core.profiles.decision import (
    DECISION_PROFILE_KIND,
    DecisionProfileError,
    DecisionProfileManager,
    apply_decision_profile,
    validate_decision_profile,
)
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry, normalize_mux_backend

from cli.batch import discover_direct_batch_jobs, job_primary_input, log_batch_failures, write_batch_summary
from cli.constants import EXIT_ARGS, EXIT_OK, EXIT_PARTIAL, EXIT_VALIDATION, EXIT_WORKFLOW
from cli.errors import CliError
from cli.inspection import inspect_sources, source_path_items
from cli.jobs import apply_metadata_overrides
from cli.json_io import deep_merge, json_default, load_json
from cli.logging import Logger
from cli.options import CommonOptions
from cli.remux_config import (
    merge_tmdb_tag_overrides,
    resolve_final_output,
    resolve_metadata_file_title,
    resolve_tmdb_metadata,
)
from cli.runtime import preview_remux_config, run_remux_config, workflow
from cli.serializers import serialize_remux_config, serialize_track_preview
from cli.tmdb import auto_tmdb_metadata_enabled


def _profile_path_candidates(raw_profile: str | Path, config: AppConfig | None = None) -> list[Path]:
    raw_text = str(raw_profile or "").strip()
    raw = Path(raw_text).expanduser()
    candidates: list[Path] = []

    def add(path: Path) -> None:
        if path not in candidates:
            candidates.append(path)

    add(raw)
    if not raw.suffix:
        add(raw.with_suffix(".json"))

    if config is not None:
        default_dir = Path(config.profiles_dir) / "decision"
        default_base = Path(raw.name) if raw.is_absolute() else raw
        add(default_dir / default_base)
        if not default_base.suffix:
            add(default_dir / default_base.with_suffix(".json"))
        safe_name = raw.stem if raw.suffix.lower() == ".json" else raw_text
        if safe_name:
            add(DecisionProfileManager(default_dir).path_for_name(safe_name))

    return candidates


def resolve_decision_profile_path(raw_profile: str | Path, config: AppConfig | None = None) -> Path:
    for candidate in _profile_path_candidates(raw_profile, config):
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(path) for path in _profile_path_candidates(raw_profile, config))
    raise CliError(f"Profil introuvable : {raw_profile}. Chemins testés : {searched}", EXIT_ARGS)


def load_decision_profile(path: str | Path, config: AppConfig | None = None) -> dict[str, Any]:
    profile_path = resolve_decision_profile_path(path, config)
    profile = load_json(profile_path)
    try:
        validate_decision_profile(profile)
    except DecisionProfileError as exc:
        raise CliError(str(exc), EXIT_ARGS) from exc
    if profile.get("kind") != DECISION_PROFILE_KIND:
        raise CliError("Le fichier n'est pas un decision-profile v1.", EXIT_ARGS)
    return profile


def _refresh_sources_tracks(sources: list[SourceInput], tracks: list[TrackEntry]) -> None:
    by_file_id: dict[str, list[TrackEntry]] = {}
    for track in tracks:
        by_file_id.setdefault(track.file_id, []).append(track)
    for source in sources:
        source.tracks = by_file_id.get(f"src{source.file_index}", source.tracks)


def _track_order(tracks: list[TrackEntry]) -> list[tuple[int, int, str]]:
    order: list[tuple[int, int, str]] = []
    for track in tracks:
        if not track.enabled:
            continue
        source_index = 0
        if str(track.file_id).startswith("src"):
            try:
                source_index = int(str(track.file_id)[3:])
            except ValueError:
                source_index = 0
        order.append((source_index, int(track.mkv_tid), track.entry_id))
    return order


def _output_variables(profile: dict[str, Any], metadata_job: dict[str, Any] | None) -> dict[str, Any]:
    metadata_variables = (metadata_job or {}).get("variables")
    profile_variables = profile.get("variables")
    if isinstance(metadata_variables, dict) and isinstance(profile_variables, dict):
        return deep_merge(metadata_variables, profile_variables)
    if isinstance(profile_variables, dict):
        return dict(profile_variables)
    if isinstance(metadata_variables, dict):
        return dict(metadata_variables)
    return {}


def build_profile_remux_config(
    profile: dict[str, Any],
    *,
    cli_inputs: list[str],
    cli_output: str | None,
    config: AppConfig,
    options: CommonOptions,
    logger: Logger,
    preview: bool = False,
    metadata_job: dict[str, Any] | None = None,
) -> tuple[RemuxConfig, dict[str, Any]]:
    if not cli_inputs:
        raise CliError("Au moins une entrée est requise avec `--profile` (`-i/--input`).", EXIT_ARGS)
    job = {"sources": [{"path": value} for value in cli_inputs]}
    sources, _infos, tracks = inspect_sources(job, config, options, logger)
    source_index_by_file_id = {f"src{source.file_index}": source.file_index for source in sources}
    result = apply_decision_profile(
        profile,
        tracks,
        source_index_by_file_id=source_index_by_file_id,
    )
    if not result.report.get("valid", True):
        raise CliError(json.dumps(result.report, ensure_ascii=False, default=json_default), EXIT_VALIDATION)
    _refresh_sources_tracks(sources, result.tracks)
    metadata = metadata_job or {}
    tmdb_title = ""
    tmdb_tags = None
    tmdb_cover = None
    tmdb_details = None
    if metadata.get("tmdb"):
        tmdb_title, tmdb_tags, tmdb_cover, tmdb_details = resolve_tmdb_metadata(
            metadata,
            config,
            sources[0].path,
            logger,
            source_title=_infos[0].title if _infos else "",
        )
    final_track_order = _track_order(result.tracks)
    output = resolve_final_output(
        cli_output=cli_output,
        job=metadata,
        sources=sources,
        details=tmdb_details,
        tracks=result.tracks,
        final_track_order=final_track_order,
        preview=preview,
        variables=_output_variables(profile, metadata),
    )
    tmdb_wins = auto_tmdb_metadata_enabled(metadata)
    tag_overrides = merge_tmdb_tag_overrides(
        tmdb_tags,
        metadata.get("tag_overrides", None),
        tmdb_wins=tmdb_wins,
    )
    remux_config = RemuxConfig(
        sources=sources,
        output=output,
        track_order=final_track_order,
        keep_chapters=True,
        file_title=resolve_metadata_file_title(metadata, tmdb_title, tmdb_wins=tmdb_wins),
        tag_overrides=tag_overrides if isinstance(tag_overrides, dict) else None,
        tmdb_cover=tmdb_cover,
        work_dir=Path(str(options.work_dir or config.work_dir)).expanduser(),
        allow_missing_output_dir=preview,
        mux_backend=normalize_mux_backend(str(metadata.get("mux_backend", config.remux_mux_backend))),
    )
    return remux_config, result.report


def profile_validate(profile_path: str, *, json_output: bool = False, config: AppConfig | None = None) -> int:
    profile = load_decision_profile(profile_path, config)
    payload = {
        "valid": True,
        "kind": profile.get("kind"),
        "version": profile.get("version"),
        "name": profile.get("name", ""),
        "rules": len(profile.get("rules", []) if isinstance(profile.get("rules"), list) else []),
    }
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))
    return EXIT_OK


def profile_preview(
    profile_path: str,
    *,
    inputs: list[str],
    output: str | None,
    json_output: bool,
    config: AppConfig,
    options: CommonOptions,
    logger: Logger,
    include_command: bool = True,
    metadata_job: dict[str, Any] | None = None,
) -> int:
    profile = load_decision_profile(profile_path, config)
    try:
        remux_config, report = build_profile_remux_config(
            profile,
            cli_inputs=inputs,
            cli_output=output,
            config=config,
            options=options,
            logger=logger,
            preview=True,
            metadata_job=metadata_job,
        )
    except CliError as exc:
        if json_output and exc.exit_code == EXIT_VALIDATION:
            try:
                payload = json.loads(str(exc))
            except json.JSONDecodeError:
                raise
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))
            return EXIT_VALIDATION
        raise
    wf = workflow(config, options, logger)
    errors = wf.validate(remux_config)
    payload = {
        "valid": not errors,
        "errors": errors,
        "profile_report": report,
        "tracks": [serialize_track_preview(track) for source in remux_config.sources for track in source.tracks],
        **serialize_remux_config(remux_config),
    }
    if not errors and include_command:
        payload["command"] = wf.build_command(remux_config)
        payload["command_text"] = wf.preview_command(remux_config)
        execution = wf.execution_preview(remux_config)
        payload.update({
            "selected_backend": execution["selected_backend"],
            "plan_version": execution["plan_version"],
            "preparation_commands": execution["preparation_commands"],
            "native_diagnostics": execution["native_diagnostics"],
            "execution_preview": execution,
        })
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))
    else:
        if errors:
            for error in errors:
                logger.emit("error", error)
            return EXIT_VALIDATION
        if include_command:
            print(payload.get("command_text", ""))
        else:
            logger.emit("info", "Configuration profil valide.")
    return EXIT_OK if not errors else EXIT_VALIDATION


def profile_apply(
    profile_path: str,
    *,
    inputs: list[str],
    output: str | None,
    force: bool,
    dry_run: bool,
    config: AppConfig,
    options: CommonOptions,
    logger: Logger,
    metadata_job: dict[str, Any] | None = None,
) -> int:
    profile = load_decision_profile(profile_path, config)
    remux_config, _report = build_profile_remux_config(
        profile,
        cli_inputs=inputs,
        cli_output=output,
        config=config,
        options=options,
        logger=logger,
        preview=dry_run,
        metadata_job=metadata_job,
    )
    if dry_run:
        return preview_remux_config(config, options, logger, remux_config)
    return run_remux_config(config, options, logger, remux_config, force=force)


def profile_batch(
    profile_path: str,
    *,
    cli_inputs: list[str] | None,
    input_dirs: list[str] | None,
    recursive: bool,
    include_patterns: list[str] | None,
    exclude_patterns: list[str] | None,
    output_dir: str | None,
    dry_run: bool,
    force: bool,
    continue_on_error: bool,
    summary_path: str | None,
    config: AppConfig,
    options: CommonOptions,
    logger: Logger,
    auto_tmdb: bool = False,
    tmdb: bool = False,
    tmdb_id: int | None = None,
    tmdb_apikey: str = "",
    output_template: str = "",
    output_all: bool = False,
    no_cover: bool = False,
    no_attach: bool = False,
) -> int:
    if not output_dir:
        raise CliError("`profile batch` requiert `--output-dir`.", EXIT_ARGS)
    profile = load_decision_profile(profile_path, config)
    metadata_template: dict[str, Any] = {}
    apply_metadata_overrides(
        metadata_template,
        auto_tmdb=auto_tmdb,
        tmdb=tmdb,
        tmdb_id=tmdb_id,
        tmdb_apikey=tmdb_apikey,
        no_cover=no_cover,
        no_attach=no_attach,
    )
    if output_template:
        metadata_template["output_template"] = output_template
        if output_all:
            metadata_template["output_all"] = True
        metadata_template["_batch_output_dir"] = str(Path(output_dir).expanduser())
    elif output_all:
        metadata_template["output_all"] = True
    discovery = discover_direct_batch_jobs(
        cli_inputs=cli_inputs,
        input_dirs=input_dirs,
        output_dir=output_dir,
        recursive=recursive,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        output_template=output_template,
        output_all=output_all,
    )
    if not discovery.jobs:
        raise CliError("Aucun fichier vidéo compatible trouvé pour le batch profil.", EXIT_ARGS)
    logger.emit(
        "info",
        f"Découverte batch profil : {discovery.selected}/{discovery.scanned} fichier(s) sélectionné(s).",
        event="profile_batch_discovery",
        scanned=discovery.scanned,
        selected=discovery.selected,
        roots=discovery.roots,
        recursive=recursive,
    )
    failures = 0
    summary_jobs: list[dict[str, Any]] = []
    seen_rendered_outputs: dict[str, str] = {}
    for job_index, job in enumerate(discovery.jobs):
        input_label = job_primary_input(job)
        output_label = str(job.get("output") or "")
        try:
            inputs = [item["path"] for item in source_path_items(job)]
            if not dry_run and output_label:
                Path(output_label).expanduser().parent.mkdir(parents=True, exist_ok=True)
            remux_config, _report = build_profile_remux_config(
                profile,
                cli_inputs=inputs,
                cli_output=output_label,
                config=config,
                options=options,
                logger=logger,
                preview=dry_run,
                metadata_job=metadata_template,
            )
            output_label = str(remux_config.output)
            if output_template:
                previous_input = seen_rendered_outputs.get(output_label)
                if previous_input is not None:
                    raise CliError(
                        f"Sortie générée en double : {output_label} pour "
                        f"{previous_input} et {input_label}. Le template "
                        f"'{output_template}' ne discrimine pas les deux "
                        "sources — ajoutez {source_name}, {episode} ou {season_episode}.",
                        EXIT_ARGS,
                    )
                seen_rendered_outputs[output_label] = input_label
                if not dry_run:
                    Path(output_label).expanduser().parent.mkdir(parents=True, exist_ok=True)
            rc = preview_remux_config(config, options, logger, remux_config) if dry_run else run_remux_config(
                config,
                options,
                logger,
                remux_config,
                force=force,
            )
            status = "success" if rc == EXIT_OK else "failed"
            failures += 0 if rc == EXIT_OK else 1
            summary_jobs.append({"job_index": job_index, "input": input_label, "output": output_label, "status": status, "exit_code": rc})
        except Exception as exc:
            failures += 1
            summary_jobs.append(
                {
                    "job_index": job_index,
                    "input": input_label,
                    "output": output_label,
                    "status": "failed",
                    "exit_code": getattr(exc, "exit_code", EXIT_WORKFLOW),
                    "error": str(exc),
                }
            )
            logger.emit("error", f"Job profil batch échoué : {exc}", event="profile_batch_job", job_index=job_index, status="failed")
            if not continue_on_error:
                break
    total = len(summary_jobs)
    exit_code = EXIT_OK if failures == 0 else EXIT_PARTIAL
    write_batch_summary(
        summary_path,
        {
            "total": total,
            "successes": total - failures,
            "failures": failures,
            "exit_code": exit_code,
            "jobs": summary_jobs,
        },
    )
    log_batch_failures(logger, summary_jobs, event="profile_batch_failure")
    logger.emit("info", f"Batch profil terminé : {total - failures}/{total} succès.", event="profile_batch_summary", total=total, failures=failures)
    return exit_code
