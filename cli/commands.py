"""Command handlers for the headless CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.config import AppConfig
from core.inspector import FileInspector

from cli.batch import run_batch
from cli.constants import EXIT_ARGS, EXIT_OK, EXIT_VALIDATION
from cli.errors import CliError
from cli.inspection import config_template_from_info
from cli.jobs import apply_metadata_overrides
from cli.jobs import load_job
from cli.json_io import json_default, write_json
from cli.logging import Logger
from cli.options import JobOverrides, common_options
from cli.profile import profile_apply, profile_batch, profile_preview, profile_validate
from cli.remux_config import build_remux_config, config_to_template
from cli.runtime import preview_remux_config, run_remux_config, workflow
from cli.schema import (
    build_cli_json_schema,
    build_cli_json_schema_bundle,
    build_decision_profile_schema_v1,
    build_exact_job_schema_v1,
)
from cli.serializers import serialize_file_info, serialize_remux_config


def _metadata_job_from_args(args: argparse.Namespace) -> dict:
    job: dict = {}
    output_template = str(getattr(args, "output_template", "") or "")
    if output_template:
        job["output_template"] = output_template
    if bool(getattr(args, "output_all", False)):
        job["output_all"] = True
    mux_backend = getattr(args, "mux_backend", None)
    if mux_backend:
        job["mux_backend"] = mux_backend
    apply_metadata_overrides(
        job,
        auto_tmdb=bool(getattr(args, "auto_tmdb", False)),
        tmdb=bool(getattr(args, "tmdb", False)),
        tmdb_id=getattr(args, "tmdb_id", None),
        tmdb_apikey=str(getattr(args, "tmdb_apikey", "") or ""),
        no_cover=bool(getattr(args, "no_cover", False)),
        no_attach=bool(getattr(args, "no_attach", False)),
        mux_backend=getattr(args, "mux_backend", None),
    )
    return job


def cmd_inspect(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    options = common_options(args)
    ffprobe = options.ffprobe or config.tool_ffprobe
    mediainfo = options.mediainfo or config.tool_mediainfo
    inspector = FileInspector(ffprobe_bin=str(ffprobe), mediainfo_bin=str(mediainfo))
    infos = [inspector.inspect(Path(value).expanduser()) for value in args.input]
    if args.config_template:
        payload = config_template_from_info(infos[0], output=args.output or "")
    else:
        payload = {"files": [serialize_file_info(info) for info in infos]}
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))
    return EXIT_OK


def cmd_schema(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    version = str(getattr(args, "schema_version", "1") or "1")
    if version == "exact-job":
        payload = build_exact_job_schema_v1()
    elif version == "decision-profile":
        payload = build_decision_profile_schema_v1()
    elif version == "all":
        payload = build_cli_json_schema_bundle()
    else:
        payload = build_cli_json_schema()
    if args.output:
        write_json(Path(args.output).expanduser(), payload)
        logger.emit("info", f"Schéma JSON sauvegardé : {args.output}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))
    return EXIT_OK


def cmd_validate(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    options = common_options(args)
    profile_path = str(getattr(args, "profile", "") or "")
    if profile_path:
        if getattr(args, "config", None):
            raise CliError("`--profile` ne se combine pas avec `--config`.", EXIT_VALIDATION)
        if not getattr(args, "input", None):
            rc = profile_validate(profile_path, json_output=bool(getattr(args, "json_output", False)), config=config)
            if rc == EXIT_OK and not getattr(args, "json_output", False):
                logger.emit("info", "Profil décisionnel valide.")
            return rc
        return profile_preview(
            profile_path,
            inputs=args.input or [],
            output=args.output,
            json_output=bool(getattr(args, "json_output", False)),
            config=config,
            options=options,
            logger=logger,
            include_command=False,
            metadata_job=_metadata_job_from_args(args),
        )
    job = load_job(JobOverrides.from_namespace(args))
    remux_config = build_remux_config(job, config, options, logger)
    errors = workflow(config, options, logger).validate(remux_config)
    if getattr(args, "json_output", False):
        payload = {
            "valid": not errors,
            "errors": errors,
            **serialize_remux_config(remux_config),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))
        return EXIT_OK if not errors else EXIT_VALIDATION
    if errors:
        for error in errors:
            logger.emit("error", error)
        return EXIT_VALIDATION
    logger.emit("info", "Configuration valide.")
    return EXIT_OK


def cmd_preview(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    options = common_options(args)
    profile_path = str(getattr(args, "profile", "") or "")
    if profile_path:
        if getattr(args, "config", None):
            raise CliError("`--profile` ne se combine pas avec `--config`.", EXIT_VALIDATION)
        return profile_preview(
            profile_path,
            inputs=args.input or [],
            output=args.output,
            json_output=bool(getattr(args, "json_output", False)),
            config=config,
            options=options,
            logger=logger,
            metadata_job=_metadata_job_from_args(args),
        )
    job = load_job(JobOverrides.from_namespace(args))
    remux_config = build_remux_config(job, config, options, logger)
    wf = workflow(config, options, logger)
    errors = wf.validate(remux_config)
    if getattr(args, "json_output", False):
        payload = {
            "valid": not errors,
            "errors": errors,
            **serialize_remux_config(remux_config),
        }
        if not errors:
            command = wf.build_command(remux_config)
            payload["command"] = command
            payload["command_text"] = wf.preview_command(remux_config)
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))
        return EXIT_OK if not errors else EXIT_VALIDATION
    if errors:
        for error in errors:
            logger.emit("error", error)
        return EXIT_VALIDATION
    print(wf.preview_command(remux_config))
    return EXIT_OK


def cmd_remux(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    options = common_options(args)
    profile_path = str(getattr(args, "profile", "") or "")
    if profile_path:
        if getattr(args, "config", None):
            raise CliError("`--profile` ne se combine pas avec `--config`.", EXIT_VALIDATION)
        return profile_apply(
            profile_path,
            inputs=args.input or [],
            output=args.output,
            force=bool(getattr(args, "force", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            config=config,
            options=options,
            logger=logger,
            metadata_job=_metadata_job_from_args(args),
        )
    job = load_job(JobOverrides.from_namespace(args))
    if args.save:
        write_json(Path(args.save).expanduser(), config_to_template(job))
        logger.emit("info", f"Template sauvegardé : {args.save}")
    remux_config = build_remux_config(job, config, options, logger)
    if getattr(args, "dry_run", False):
        return preview_remux_config(config, options, logger, remux_config)
    return run_remux_config(config, options, logger, remux_config, force=bool(args.force))


def cmd_batch(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    profile_path = str(getattr(args, "profile", "") or "")
    if profile_path:
        if getattr(args, "template", None) or getattr(args, "batch", None):
            raise CliError("`--profile` ne se combine pas avec `--template` ou `--batch`.", EXIT_ARGS)
        return profile_batch(
            profile_path,
            cli_inputs=args.input,
            input_dirs=args.input_dir,
            recursive=bool(args.recursive),
            include_patterns=args.include,
            exclude_patterns=args.exclude,
            output_dir=args.output_dir,
            dry_run=bool(args.dry_run),
            force=bool(args.force),
            continue_on_error=bool(args.continue_on_error),
            summary_path=args.summary,
            auto_tmdb=bool(getattr(args, "auto_tmdb", False)),
            tmdb=bool(getattr(args, "tmdb", False)),
            tmdb_id=getattr(args, "tmdb_id", None),
            tmdb_apikey=str(getattr(args, "tmdb_apikey", "") or ""),
            output_template=str(getattr(args, "output_template", "") or ""),
            output_all=bool(getattr(args, "output_all", False)),
            no_cover=bool(getattr(args, "no_cover", False)),
            no_attach=bool(getattr(args, "no_attach", False)),
            config=config,
            options=common_options(args),
            logger=logger,
        )
    if not args.template:
        raise CliError("`batch` requiert `--template` ou `--profile`.", EXIT_ARGS)
    return run_batch(
        template_path=args.template,
        batch_path=args.batch,
        cli_inputs=args.input,
        input_dirs=args.input_dir,
        recursive=bool(args.recursive),
        include_patterns=args.include,
        exclude_patterns=args.exclude,
        output_dir=args.output_dir,
        dry_run=bool(args.dry_run),
        force=bool(args.force),
        continue_on_error=bool(args.continue_on_error),
        summary_path=args.summary,
        auto_tmdb=bool(getattr(args, "auto_tmdb", False)),
        tmdb=bool(getattr(args, "tmdb", False)),
        tmdb_id=getattr(args, "tmdb_id", None),
        tmdb_apikey=str(getattr(args, "tmdb_apikey", "") or ""),
        output_template=str(getattr(args, "output_template", "") or ""),
        output_all=bool(getattr(args, "output_all", False)),
        no_cover=bool(getattr(args, "no_cover", False)),
        no_attach=bool(getattr(args, "no_attach", False)),
        config=config,
        options=common_options(args),
        logger=logger,
    )


def cmd_profile(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    command = str(getattr(args, "profile_command", "") or "")
    if command == "validate":
        rc = profile_validate(args.profile, json_output=bool(getattr(args, "json_output", False)), config=config)
        if rc == EXIT_OK and not getattr(args, "json_output", False):
            logger.emit("info", "Profil décisionnel valide.")
        return rc
    if command == "preview":
        return profile_preview(
            args.profile,
            inputs=args.input or [],
            output=args.output,
            json_output=bool(getattr(args, "json_output", False)),
            config=config,
            options=common_options(args),
            logger=logger,
            metadata_job=_metadata_job_from_args(args),
        )
    if command == "apply":
        return profile_apply(
            args.profile,
            inputs=args.input or [],
            output=args.output,
            force=bool(getattr(args, "force", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            config=config,
            options=common_options(args),
            logger=logger,
            metadata_job=_metadata_job_from_args(args),
        )
    if command == "batch":
        return profile_batch(
            args.profile,
            cli_inputs=args.input,
            input_dirs=args.input_dir,
            recursive=bool(args.recursive),
            include_patterns=args.include,
            exclude_patterns=args.exclude,
            output_dir=args.output_dir,
            dry_run=bool(args.dry_run),
            force=bool(args.force),
            continue_on_error=bool(args.continue_on_error),
            summary_path=args.summary,
            auto_tmdb=bool(getattr(args, "auto_tmdb", False)),
            tmdb=bool(getattr(args, "tmdb", False)),
            tmdb_id=getattr(args, "tmdb_id", None),
            tmdb_apikey=str(getattr(args, "tmdb_apikey", "") or ""),
            output_template=str(getattr(args, "output_template", "") or ""),
            output_all=bool(getattr(args, "output_all", False)),
            no_cover=bool(getattr(args, "no_cover", False)),
            no_attach=bool(getattr(args, "no_attach", False)),
            config=config,
            options=common_options(args),
            logger=logger,
        )
    raise CliError("Sous-commande profile inconnue.", EXIT_VALIDATION)


def cmd_run(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    options = common_options(args)
    profile_path = str(getattr(args, "profile", "") or "")
    if profile_path:
        if getattr(args, "config", None):
            raise CliError("`--profile` ne se combine pas avec `--config`.", EXIT_VALIDATION)
        return profile_apply(
            profile_path,
            inputs=args.input or [],
            output=args.output,
            force=bool(getattr(args, "force", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            config=config,
            options=options,
            logger=logger,
            metadata_job=_metadata_job_from_args(args),
        )
    job = load_job(JobOverrides.from_namespace(args))
    remux_config = build_remux_config(job, config, options, logger)
    if getattr(args, "dry_run", False):
        return preview_remux_config(config, options, logger, remux_config)
    return run_remux_config(config, options, logger, remux_config, force=bool(args.force))
