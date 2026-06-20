"""Batch processing helpers for CLI commands."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from glob import glob
from pathlib import Path
from typing import Any

from core.config import AppConfig
from core.bluray import discover_titles, find_disc_root
from core.file_types import is_accepted

from cli.constants import EXIT_ARGS, EXIT_OK, EXIT_PARTIAL, EXIT_WORKFLOW
from cli.contract import validate_batch_contract, validate_job_contract
from cli.errors import CliError
from cli.inspection import source_path_items
from cli.jobs import apply_metadata_overrides
from cli.json_io import deep_merge, load_json, write_json
from cli.logging import Logger
from cli.options import CommonOptions
from cli.remux_config import build_remux_config
from cli.runtime import preview_remux_config, run_remux_config


@dataclass(frozen=True)
class BatchDiscovery:
    jobs: list[dict[str, Any]]
    scanned: int
    selected: int
    explicit_inputs: int
    roots: list[str]


def _matches_any(path: Path, patterns: list[str] | None) -> bool:
    if not patterns:
        return False
    rel = path.as_posix()
    name = path.name
    return any(fnmatch(rel, pattern) or fnmatch(name, pattern) for pattern in patterns)


def _expand_input_dirs(raw_dir: str) -> list[Path]:
    expanded = Path(str(raw_dir)).expanduser()
    raw_text = str(expanded)
    if any(char in raw_text for char in "*?["):
        matches = sorted(Path(match) for match in glob(raw_text))
        dirs = [path for path in matches if path.is_dir()]
        if not dirs:
            raise CliError(f"Aucun dossier --input-dir ne correspond au glob : {raw_dir}", EXIT_ARGS)
        return dirs
    return [expanded]


def _generated_output_path(output_dir: str | None, relative_path: Path) -> str | None:
    if not output_dir:
        return None
    return str(Path(output_dir).expanduser() / relative_path.with_suffix(".mkv"))


def _job_for_source(
    path: Path,
    *,
    output: str | None = None,
    output_dir: str | None = None,
    output_template: str = "",
    output_all: bool = False,
) -> dict[str, Any]:
    job: dict[str, Any] = {"sources": [{"path": str(path)}]}
    if output_template:
        job["output_template"] = output_template
        if output_all:
            job["output_all"] = True
        if output_dir:
            job["_batch_output_dir"] = str(Path(output_dir).expanduser())
        job["_batch_generated_output"] = True
    elif output:
        job["output"] = output
        job["_batch_generated_output"] = True
    return job


def _assert_unique_generated_outputs(jobs: list[dict[str, Any]]) -> None:
    seen: dict[str, str] = {}
    for job in jobs:
        if not job.get("_batch_generated_output"):
            continue
        output = str(job.get("output") or "")
        if not output:
            # Template à résoudre plus tard — dedup post-rendu fait dans la boucle batch.
            continue
        input_path = job_primary_input(job)
        previous = seen.get(output)
        if previous is not None:
            raise CliError(
                "Sortie générée en double : "
                f"{output} pour {previous} et {input_path}. "
                "Utilisez des dossiers distincts ou un batch JSON avec sorties explicites.",
                EXIT_ARGS,
            )
        seen[output] = input_path


def discover_direct_batch_jobs(
    *,
    cli_inputs: list[str] | None = None,
    input_dirs: list[str] | None = None,
    output_dir: str | None = None,
    recursive: bool = False,
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    output_template: str = "",
    output_all: bool = False,
) -> BatchDiscovery:
    jobs: list[dict[str, Any]] = []
    scanned = 0
    roots: list[str] = []

    for raw in cli_inputs or []:
        path = Path(str(raw)).expanduser()
        scanned += 1
        output = _generated_output_path(output_dir, Path(path.name)) if not output_template else None
        jobs.append(_job_for_source(path, output=output, output_dir=output_dir, output_template=output_template, output_all=output_all))

    for raw_dir in input_dirs or []:
        for root in _expand_input_dirs(str(raw_dir)):
            if not root.exists():
                raise CliError(f"Dossier batch introuvable : {root}", EXIT_ARGS)
            if not root.is_dir():
                raise CliError(f"Chemin --input-dir invalide, attendu dossier : {root}", EXIT_ARGS)
            roots.append(str(root))
            bluray_titles = discover_titles(root, min_duration_s=60.0)
            if bluray_titles:
                title = bluray_titles[0]
                output_relative = Path(title.disc_root.name or title.label)
                jobs.append(_job_for_source(
                    title.playlist_path,
                    output=(_generated_output_path(output_dir, output_relative) if not output_template else None),
                    output_dir=output_dir,
                    output_template=output_template,
                    output_all=output_all,
                ))
                continue
            iterator = root.rglob("*") if recursive else root.iterdir()
            candidates = sorted(
                (path for path in iterator if path.is_file()),
                key=lambda path: path.relative_to(root).as_posix().lower(),
            )
            for path in candidates:
                scanned += 1
                if find_disc_root(path.parent) is not None and path.suffix.lower() == ".m2ts":
                    continue
                relative = path.relative_to(root)
                if not is_accepted(path, video_only=True):
                    continue
                if include_patterns and not _matches_any(relative, include_patterns):
                    continue
                if _matches_any(relative, exclude_patterns):
                    continue
                output = _generated_output_path(output_dir, relative) if not output_template else None
                jobs.append(_job_for_source(path, output=output, output_dir=output_dir, output_template=output_template, output_all=output_all))

    _assert_unique_generated_outputs(jobs)
    return BatchDiscovery(
        jobs=jobs,
        scanned=scanned,
        selected=len(jobs),
        explicit_inputs=len(cli_inputs or []),
        roots=roots,
    )


def batch_jobs(batch: dict[str, Any]) -> list[dict[str, Any]]:
    raw_jobs = batch.get("jobs", batch.get("inputs", []))
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise CliError("Le batch doit contenir `jobs` ou `inputs`.", EXIT_ARGS)
    jobs: list[dict[str, Any]] = []
    for item in raw_jobs:
        if isinstance(item, dict):
            jobs.append(item)
        else:
            jobs.append({"sources": [str(item)]})
    return jobs


def job_primary_input(job: dict[str, Any]) -> str:
    try:
        first = source_path_items(job)[0]["path"]
        return str(first)
    except Exception:
        return ""


def write_batch_summary(path: str | None, payload: dict[str, Any]) -> None:
    if not path:
        return
    write_json(Path(path).expanduser(), payload)


def log_batch_failures(logger: Logger, summary_jobs: list[dict[str, Any]], *, event: str = "batch_failure") -> None:
    """Récapitule les fichiers non traités et la cause en fin de batch."""
    failed = [job for job in summary_jobs if job.get("status") == "failed"]
    if not failed:
        return
    logger.emit("warn", f"{len(failed)} fichier(s) non traité(s) :", event=f"{event}_report", count=len(failed))
    for job in failed:
        cause = str(job.get("error") or f"exit_code={job.get('exit_code')}")
        logger.emit(
            "error",
            f"  - {job.get('input') or '?'} : {cause}",
            event=event,
            job_index=job.get("job_index"),
            input=job.get("input"),
            output=job.get("output"),
            exit_code=job.get("exit_code"),
            error=job.get("error"),
        )


def run_batch(
    *,
    template_path: str,
    batch_path: str | None,
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
    direct_mode = bool(cli_inputs or input_dirs or recursive or include_patterns or exclude_patterns)
    if batch_path and direct_mode:
        raise CliError(
            "`--batch` ne peut pas être combiné avec `-i/--input`, `--input-dir`, "
            "`--recursive`, `--include` ou `--exclude`.",
            EXIT_ARGS,
        )

    template = load_json(Path(template_path).expanduser())
    validate_job_contract(template, require_version=True)

    discovery: BatchDiscovery | None = None
    if batch_path:
        batch = load_json(Path(batch_path).expanduser())
    else:
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
            raise CliError("Aucun fichier vidéo compatible trouvé pour le batch.", EXIT_ARGS)
        logger.emit(
            "info",
            f"Découverte batch : {discovery.selected}/{discovery.scanned} fichier(s) sélectionné(s).",
            event="batch_discovery",
            scanned=discovery.scanned,
            selected=discovery.selected,
            explicit_inputs=discovery.explicit_inputs,
            roots=discovery.roots,
            recursive=recursive,
        )
        batch = {"jobs": discovery.jobs}

    validate_batch_contract(batch)
    failures = 0
    total = 0
    summary_jobs: list[dict[str, Any]] = []
    seen_rendered_outputs: dict[str, str] = {}
    for item in batch_jobs(batch):
        job_index = total
        total += 1
        job = deep_merge(template, item)
        if str(template.get("kind") or "") == "exact-job":
            job["_relaxed_selectors"] = True
        apply_metadata_overrides(
            job,
            auto_tmdb=auto_tmdb,
            tmdb=tmdb,
            tmdb_id=tmdb_id,
            tmdb_apikey=tmdb_apikey,
            no_cover=no_cover,
            no_attach=no_attach,
        )
        if output_template and "output_template" not in job:
            job["output_template"] = output_template
            if output_all:
                job["output_all"] = True
            if output_dir:
                job["_batch_output_dir"] = str(Path(output_dir).expanduser())
            job["_batch_generated_output"] = True
        elif output_all:
            job["output_all"] = True
        input_label = job_primary_input(job)
        output_label = str(job.get("output") or "")
        logger.emit(
            "info",
            f"Batch job {job_index + 1} démarré",
            event="batch_job",
            job_index=job_index,
            input=input_label,
            output=output_label,
            status="started",
        )
        try:
            if "output" not in job and "output_template" not in job and output_dir:
                first = source_path_items(job)[0]["path"]
                job["output"] = str(Path(output_dir).expanduser() / (Path(str(first)).stem + ".mkv"))
                job["_batch_generated_output"] = True
                output_label = str(job["output"])
            if dry_run and job.get("_batch_generated_output"):
                job["_allow_missing_output_dir"] = True
            if not dry_run and job.get("_batch_generated_output") and job.get("output"):
                Path(str(job["output"])).expanduser().parent.mkdir(parents=True, exist_ok=True)
            validate_job_contract(job, path=f"jobs[{total - 1}]", require_version=True)
            remux_config = build_remux_config(job, config, options, logger)
            output_label = str(remux_config.output)
            if job.get("output_template"):
                previous_input = seen_rendered_outputs.get(output_label)
                if previous_input is not None:
                    raise CliError(
                        f"Sortie générée en double : {output_label} pour "
                        f"{previous_input} et {input_label}. Le template "
                        f"'{job.get('output_template')}' ne discrimine pas les deux "
                        "sources — ajoutez {source_name}, {episode} ou {season_episode}.",
                        EXIT_ARGS,
                    )
                seen_rendered_outputs[output_label] = input_label
                if not dry_run:
                    Path(output_label).expanduser().parent.mkdir(parents=True, exist_ok=True)
            if dry_run:
                rc = preview_remux_config(config, options, logger, remux_config)
            else:
                rc = run_remux_config(config, options, logger, remux_config, force=force)
            if rc != EXIT_OK:
                failures += 1
                summary_jobs.append(
                    {"job_index": job_index, "input": input_label, "output": output_label, "status": "failed", "exit_code": rc}
                )
                logger.emit(
                    "error",
                    f"Batch job {job_index + 1} échoué",
                    event="batch_job",
                    job_index=job_index,
                    input=input_label,
                    output=output_label,
                    status="failed",
                    exit_code=rc,
                )
            else:
                summary_jobs.append(
                    {"job_index": job_index, "input": input_label, "output": output_label, "status": "success", "exit_code": EXIT_OK}
                )
                logger.emit(
                    "info",
                    f"Batch job {job_index + 1} terminé",
                    event="batch_job",
                    job_index=job_index,
                    input=input_label,
                    output=output_label,
                    status="success",
                )
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
            logger.emit(
                "error",
                f"Job batch échoué : {exc}",
                event="batch_job",
                job_index=job_index,
                input=input_label,
                output=output_label,
                status="failed",
                exception=repr(exc),
            )
            if not continue_on_error:
                break
    exit_code = EXIT_OK if failures == 0 else EXIT_PARTIAL
    summary = {
        "total": total,
        "successes": total - failures,
        "failures": failures,
        "exit_code": exit_code,
        "jobs": summary_jobs,
    }
    write_batch_summary(summary_path, summary)
    log_batch_failures(logger, summary_jobs)
    logger.emit("info", f"Batch terminé : {total - failures}/{total} succès.", event="batch_summary", total=total, failures=failures)
    return exit_code
