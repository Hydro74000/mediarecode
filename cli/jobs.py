"""Load and merge CLI job JSON with command-line overrides."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cli.contract import validate_job_contract
from cli.json_io import deep_merge, load_json
from cli.options import JobOverrides
from cli.tmdb import mark_auto_tmdb


def _tmdb_block(job: dict[str, Any]) -> dict[str, Any]:
    raw_tmdb = job.get("tmdb", {})
    tmdb: dict[str, Any] = dict(raw_tmdb) if isinstance(raw_tmdb, dict) else {"enabled": True}
    job["tmdb"] = tmdb
    return tmdb


def _disable_source_attachments(job: dict[str, Any]) -> None:
    raw_sources = job.get("sources")
    if isinstance(raw_sources, (str, Path)):
        job["sources"] = [{"path": str(raw_sources), "attachments": "none"}]
        return
    if not isinstance(raw_sources, list):
        return
    sources: list[dict[str, Any]] = []
    for source in raw_sources:
        item = dict(source) if isinstance(source, dict) else {"path": str(source)}
        item["attachments"] = "none"
        sources.append(item)
    job["sources"] = sources


def apply_metadata_overrides(
    job: dict[str, Any],
    *,
    auto_tmdb: bool = False,
    tmdb: bool = False,
    tmdb_id: int | None = None,
    tmdb_apikey: str = "",
    no_cover: bool = False,
    no_attach: bool = False,
) -> None:
    tmdb_block: dict[str, Any] | None
    apikey = (tmdb_apikey or "").strip()
    if auto_tmdb or tmdb or tmdb_id is not None or apikey:
        tmdb_block = _tmdb_block(job)
        tmdb_block["enabled"] = True
        if auto_tmdb or tmdb:
            mark_auto_tmdb(tmdb_block)
        if tmdb_id is not None:
            tmdb_block["id"] = tmdb_id
        if apikey:
            tmdb_block["api_key"] = apikey
    elif no_cover or no_attach:
        raw_tmdb = job.get("tmdb")
        if isinstance(raw_tmdb, dict):
            tmdb_block = dict(raw_tmdb)
            job["tmdb"] = tmdb_block
        else:
            tmdb_block = _tmdb_block(job) if raw_tmdb else None
    else:
        tmdb_block = None

    if (no_cover or no_attach) and tmdb_block is not None:
        tmdb_block["cover"] = False
    if no_attach:
        _disable_source_attachments(job)
        job["extra_attachments"] = []


def load_job(overrides: JobOverrides) -> dict[str, Any]:
    job: dict[str, Any] = {}
    require_version = False
    if overrides.config:
        loaded = load_json(Path(overrides.config).expanduser())
        validate_job_contract(loaded, require_version=True)
        job = deep_merge(job, loaded)
        require_version = True
    if overrides.template:
        template = load_json(Path(overrides.template).expanduser())
        validate_job_contract(template, require_version=True)
        job = deep_merge(template, job)
        require_version = True
    if overrides.input:
        job["sources"] = [{"path": item} for item in overrides.input]
    if overrides.output:
        job["output"] = overrides.output
    if overrides.output_template:
        job["output_template"] = overrides.output_template
    if overrides.output_all:
        job["output_all"] = True
    if overrides.mux_backend:
        job["mux_backend"] = overrides.mux_backend
    apply_metadata_overrides(
        job,
        auto_tmdb=overrides.auto_tmdb,
        tmdb=overrides.tmdb,
        tmdb_id=overrides.tmdb_id,
        tmdb_apikey=overrides.tmdb_apikey,
        no_cover=overrides.no_cover,
        no_attach=overrides.no_attach,
    )
    validate_job_contract(job, require_version=require_version)
    return job
