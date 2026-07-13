"""Build RemuxConfig objects from CLI JSON jobs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.config import AppConfig
from core.media_info_fetcher import (
    MediaDetails,
    TmdbError,
    TmdbFetcher,
    clean_filename_for_search,
    default_tmdb_bearer_token,
    extract_year_from_filename,
)
from core.version import APP_CONFIG_DIR_NAME, APP_ENV_PREFIX
from core.workflows.remux_models import (
    RemuxConfig, SourceInput, TrackEntry, clone_track_entry, normalize_mux_backend,
)
from core.profiles.selectors import (
    SelectorResolutionError,
    apply_track_spec,
    resolve_track_selector,
    resolve_track_selector_relaxed,
)

from cli.chapters import chapter_entries
from cli.constants import EXIT_ARGS, EXIT_VALIDATION
from cli.contract import validate_job_contract
from cli.errors import CliError
from cli.inspection import inspect_sources
from cli.json_io import deep_merge
from cli.logging import Logger
from cli.options import CommonOptions
from cli.output_template import build_output_context, render_output_template
from cli.tmdb import auto_tmdb_metadata_enabled, normalized_tmdb_options


TMDB_MKV_TAG_KEYS = frozenset(
    {
        "DATE_RELEASED",
        "GENRE",
        "DIRECTOR",
        "CAST",
        "SUBTITLE",
        "SYNOPSIS",
        "COUNTRY",
        "URL",
        "DESCRIPTION",
        "COLLECTION",
        "SEASON",
        "EPISODE",
    }
)


def _fallback_profile_name(job: dict[str, Any]) -> str:
    fallback = job.get("fallback_profile")
    if isinstance(fallback, dict):
        return str(fallback.get("name") or "").strip()
    return str(fallback or "").strip()


def _track_from_spec(
    spec: dict[str, Any],
    tracks: list[TrackEntry],
    *,
    strict_selectors: bool = False,
    relaxed_selectors: bool = False,
    context: str = "tracks",
) -> TrackEntry | None:
    selector = spec.get("selector")
    if isinstance(selector, dict):
        if relaxed_selectors:
            return resolve_track_selector_relaxed(
                selector,
                tracks,
                context=context,
            )
        return resolve_track_selector(
            selector,
            tracks,
            context=context,
            strict=strict_selectors,
        )
    raw_source = spec.get("source", spec.get("source_index", 0))
    source_index = int(0 if raw_source is None else raw_source)
    file_id = f"src{source_index}"
    tid = spec.get("id", spec.get("mkv_tid", spec.get("stream")))
    if tid is None:
        return None
    track_id = int(tid)
    lookup = {(t.file_id, t.mkv_tid): t for t in tracks}
    return lookup.get((file_id, track_id))


def apply_explicit_track_edits(
    job: dict[str, Any],
    tracks: list[TrackEntry],
    *,
    strict_selectors: bool = False,
    relaxed_selectors: bool = False,
) -> None:
    specs = job.get("tracks", [])
    if not isinstance(specs, list):
        return
    source_tracks = [track for track in tracks if not track.is_new]
    for index, spec in enumerate(specs):
        if not isinstance(spec, dict):
            continue
        try:
            track = _track_from_spec(
                spec,
                source_tracks,
                strict_selectors=strict_selectors,
                relaxed_selectors=relaxed_selectors,
                context=f"tracks[{index}]",
            )
        except SelectorResolutionError as exc:
            if (
                relaxed_selectors
                and spec.get("enabled") is False
                and exc.report.get("error") == "track_selector_unmatched_relaxed"
            ):
                continue
            if _fallback_profile_name(job):
                exc.report["suggested_profile"] = _fallback_profile_name(job)
            raise
        if track is None:
            continue
        apply_track_spec(track, spec)


def apply_audio_variants(
    job: dict[str, Any],
    sources,
    tracks: list[TrackEntry],
    *,
    strict_selectors: bool = False,
    relaxed_selectors: bool = False,
) -> None:
    specs = job.get("audio_variants", job.get("derived_audio_tracks", []))
    if not isinstance(specs, list):
        return
    source_by_file_id = {
        track.file_id: source
        for source in sources
        for track in source.tracks
    }
    for index, spec in enumerate(specs):
        if not isinstance(spec, dict):
            continue
        selector = spec.get("source_selector", spec.get("selector"))
        if not isinstance(selector, dict):
            continue
        source_tracks = [track for track in tracks if not track.is_new]
        try:
            source_track = (
                resolve_track_selector_relaxed(
                    selector,
                    source_tracks,
                    context=f"audio_variants[{index}]",
                )
                if relaxed_selectors
                else resolve_track_selector(
                    selector,
                    source_tracks,
                    context=f"audio_variants[{index}]",
                    strict=strict_selectors,
                )
            )
        except SelectorResolutionError as exc:
            if _fallback_profile_name(job):
                exc.report["suggested_profile"] = _fallback_profile_name(job)
            raise
        if source_track is None or source_track.track_type != "audio":
            continue
        new_entry = clone_track_entry(source_track, entry_id=str(spec.get("entry_id") or "") or None)
        codec = str(spec.get("codec") or spec.get("target_codec") or "").strip()
        bitrate = int(spec.get("bitrate_kbps") or 0)
        if codec:
            new_entry.codec = codec.upper() if codec.lower() != "copy" else source_track.codec
        if codec and codec.lower() != "copy":
            parts = [
                part.strip()
                for part in str(source_track.display_info or "").replace("·", "  ").split("  ")
                if part.strip() and "kbps" not in part.lower()
            ]
            if bitrate > 0:
                parts.append(f"{bitrate} kbps")
            new_entry.display_info = "  ".join(parts)
        apply_track_spec(new_entry, {**spec, "enabled": spec.get("enabled", True)})
        source = source_by_file_id.get(source_track.file_id)
        if source is not None:
            source.tracks.append(new_entry)
        tracks.append(new_entry)


def track_order(
    job: dict[str, Any],
    tracks: list[TrackEntry],
    *,
    strict_selectors: bool = False,
    relaxed_selectors: bool = False,
) -> list[tuple[int, int, str]]:
    explicit = job.get("track_order")
    if isinstance(explicit, list):
        order: list[tuple[int, int, str]] = []
        by_key = {(t.file_id, t.mkv_tid): t for t in tracks}
        source_tracks = [track for track in tracks if not track.is_new]
        for index, item in enumerate(explicit):
            if isinstance(item, dict):
                selector = item.get("selector")
                if isinstance(selector, dict):
                    selector_tracks = tracks if selector.get("entry_id") else source_tracks
                    try:
                        track = (
                            resolve_track_selector_relaxed(
                                selector,
                                selector_tracks,
                                context=f"track_order[{index}]",
                            )
                            if relaxed_selectors
                            else resolve_track_selector(
                                selector,
                                selector_tracks,
                                context=f"track_order[{index}]",
                                strict=strict_selectors,
                            )
                        )
                    except SelectorResolutionError as exc:
                        if _fallback_profile_name(job):
                            exc.report["suggested_profile"] = _fallback_profile_name(job)
                        raise
                    if track is not None and track.enabled:
                        source_index = int(track.file_id.removeprefix("src")) if track.file_id.startswith("src") else 0
                        order.append((source_index, track.mkv_tid, track.entry_id))
                    continue
                raw_source = item.get("source", item.get("source_index", 0))
                source_index = int(0 if raw_source is None else raw_source)
                raw_tid = item.get("id", item.get("mkv_tid", item.get("stream")))
                if raw_tid is None:
                    continue
                tid = int(raw_tid)
            else:
                source_index = int(item[0])
                tid = int(item[1])
            track = by_key.get((f"src{source_index}", tid))
            if track is not None and track.enabled:
                order.append((source_index, tid, track.entry_id))
        return order
    return [
        (int(track.file_id.removeprefix("src")), track.mkv_tid, track.entry_id)
        for track in tracks
        if track.enabled
    ]


def resolve_tmdb_metadata(
    job: dict[str, Any],
    config: AppConfig,
    first_source: Path,
    logger: Logger,
    *,
    source_title: str = "",
) -> tuple[str, dict[str, str] | None, tuple[str, str] | None, MediaDetails | None]:
    tmdb = normalized_tmdb_options(job, first_source, source_title=source_title)
    if not tmdb:
        return "", None, None, None

    api_key = str(tmdb.get("api_key") or config.tmdb_api_key or "").strip()
    bearer = str(tmdb.get("bearer_token") or config.tmdb_bearer_token or "").strip()
    if not api_key and not bearer:
        bearer = default_tmdb_bearer_token()
    if not api_key and not bearer:
        raise CliError(
            "Authentification TMDB manquante. Renseigner --tmdb-apikey, "
            f"la variable d'environnement {APP_ENV_PREFIX}_TMDB_BEARER_TOKEN, "
            "ou la clé `metadata/tmdb_api_key` (resp. `tmdb_bearer_token`) "
            f"dans ~/.config/{APP_CONFIG_DIR_NAME}/config.ini.",
            EXIT_VALIDATION,
        )
    fetcher = TmdbFetcher(
        api_key=api_key,
        bearer_token=bearer,
        language=str(tmdb.get("language") or "fr-FR"),
    )
    kind = str(tmdb.get("kind") or "all")
    season = str(tmdb.get("season") or "")
    episode = str(tmdb.get("episode") or "")
    tmdb_id = tmdb.get("id", tmdb.get("tmdb_id"))
    if tmdb_id:
        title = str(tmdb.get("title") or clean_filename_for_search(first_source) or first_source.stem)
        from core.media_info_fetcher import MediaSearchResult

        result_kind = str(tmdb.get("kind") or "all")
        if result_kind not in {"movie", "tv"}:
            result_kind = "tv" if season and episode else "movie"
        result = MediaSearchResult(
            tmdb_id=int(tmdb_id),
            title=title,
            year=str(tmdb.get("year") or ""),
            kind=result_kind,
        )
    else:
        query = str(tmdb.get("query") or clean_filename_for_search(first_source) or first_source.stem)
        year = str(tmdb.get("year") or extract_year_from_filename(first_source) or "")
        results = fetcher.search(query, kind=kind, year=year)
        if not results:
            raise CliError(f"Aucun résultat TMDB pour : {query}", EXIT_VALIDATION)
        result = results[0]
        logger.emit("info", f"TMDB premier résultat retenu : {result.title} ({result.year}) #{result.tmdb_id}")

    details = fetcher.get_details(result, season=season, episode=episode)
    title = details.formatted_container_title()
    cover = (
        (details.cover_url, details.cover_filename)
        if tmdb.get("cover", True) and details.cover_url and details.cover_filename
        else None
    )
    return title, details.to_mkv_tags(), cover, details


def resolve_final_output(
    *,
    cli_output: str | None,
    job: dict[str, Any],
    sources: list[SourceInput],
    details: MediaDetails | None,
    tracks: list[TrackEntry] | None = None,
    final_track_order: list[tuple[int, int, str]] | None = None,
    preview: bool = False,
    variables: dict[str, Any] | None = None,
) -> Path:
    """Calcule le chemin de sortie final, en rendant le template si présent.

    Priorité : ``cli_output`` > ``job.output_template`` > ``job.output`` >
    fallback preview > erreur.
    """
    if cli_output:
        return Path(str(cli_output)).expanduser()
    template = str(job.get("output_template") or "").strip()
    if template and sources:
        template_variables = variables if isinstance(variables, dict) else job.get("variables")
        if not isinstance(template_variables, dict):
            template_variables = {}
        ctx = build_output_context(
            sources[0].path,
            details,
            tracks=tracks,
            track_order=final_track_order,
            output_all=bool(job.get("output_all", False)),
            variables=template_variables,
        )
        rendered = render_output_template(template, ctx)
        rendered_path = Path(rendered)
        if rendered_path.is_absolute():
            return rendered_path
        base_dir = str(job.get("_batch_output_dir") or "").strip()
        if base_dir:
            return Path(base_dir).expanduser() / rendered_path
        return rendered_path.expanduser()
    raw = job.get("output")
    if raw:
        return Path(str(raw)).expanduser()
    if preview and sources:
        return sources[0].path.with_suffix(".profile-preview.mkv")
    raise CliError(
        "Une sortie est requise (`--output`, `--output-template`, ou `output` JSON).",
        EXIT_ARGS,
    )


def resolve_metadata_file_title(job: dict[str, Any], tmdb_title: str, *, tmdb_wins: bool = False) -> str:
    explicit = str(job.get("file_title") or "")
    if tmdb_wins and tmdb_title:
        return tmdb_title
    return explicit or tmdb_title or ""


def merge_tmdb_tag_overrides(
    tmdb_tags: dict[str, str] | None,
    tag_overrides: Any,
    *,
    tmdb_wins: bool = False,
) -> dict[str, str] | None:
    explicit = dict(tag_overrides) if isinstance(tag_overrides, dict) else None
    if not tmdb_tags:
        return explicit
    if explicit is None:
        return dict(tmdb_tags)
    if tmdb_wins:
        cleaned_explicit = {
            key: value
            for key, value in explicit.items()
            if key not in TMDB_MKV_TAG_KEYS or key not in tmdb_tags
        }
        return deep_merge(cleaned_explicit, tmdb_tags)
    return deep_merge(tmdb_tags, explicit)


def build_remux_config(
    job: dict[str, Any],
    config: AppConfig,
    options: CommonOptions,
    logger: Logger,
    *,
    cli_inputs: list[str] | None = None,
    cli_output: str | None = None,
) -> RemuxConfig:
    validate_job_contract(job, require_version=False)
    sources, infos, tracks = inspect_sources(job, config, options, logger, cli_inputs=cli_inputs)
    strict_selectors = str(job.get("kind") or "") == "exact-job"
    relaxed_selectors = bool(job.get("_relaxed_selectors"))
    apply_audio_variants(
        job,
        sources,
        tracks,
        strict_selectors=strict_selectors,
        relaxed_selectors=relaxed_selectors,
    )
    apply_explicit_track_edits(
        job,
        tracks,
        strict_selectors=strict_selectors,
        relaxed_selectors=relaxed_selectors,
    )

    keep_chapters, chapter_overrides, chapter_source_index = chapter_entries(job, infos)
    tmdb_title = ""
    tmdb_tags = None
    tmdb_cover = None
    tmdb_details: MediaDetails | None = None
    try:
        tmdb_title, tmdb_tags, tmdb_cover, tmdb_details = resolve_tmdb_metadata(
            job,
            config,
            sources[0].path,
            logger,
            source_title=infos[0].title if infos else "",
        )
    except TmdbError as exc:
        raise CliError(str(exc), EXIT_VALIDATION) from exc

    final_track_order = track_order(
        job,
        tracks,
        strict_selectors=strict_selectors,
        relaxed_selectors=relaxed_selectors,
    )
    output = resolve_final_output(
        cli_output=cli_output,
        job=job,
        sources=sources,
        details=tmdb_details,
        tracks=tracks,
        final_track_order=final_track_order,
        variables=job.get("variables") if isinstance(job.get("variables"), dict) else None,
    )

    tmdb_wins = auto_tmdb_metadata_enabled(job)
    tag_overrides = merge_tmdb_tag_overrides(
        tmdb_tags,
        job.get("tag_overrides", None),
        tmdb_wins=tmdb_wins,
    )

    work_dir = Path(str(options.work_dir or job.get("work_dir") or config.work_dir)).expanduser()
    return RemuxConfig(
        sources=sources,
        output=output,
        track_order=final_track_order,
        keep_chapters=keep_chapters,
        chapter_overrides=chapter_overrides,
        chapter_source_index=chapter_source_index,
        extra_attachments=[Path(str(p)).expanduser() for p in job.get("extra_attachments", [])],
        work_dir=work_dir,
        file_title=resolve_metadata_file_title(job, tmdb_title, tmdb_wins=tmdb_wins),
        tag_overrides=tag_overrides if isinstance(tag_overrides, dict) else None,
        tmdb_cover=tmdb_cover,
        allow_missing_output_dir=bool(job.get("_allow_missing_output_dir", False)),
        mux_backend=normalize_mux_backend(str(job.get("mux_backend", config.remux_mux_backend))),
    )


def config_to_template(job: dict[str, Any], *, include_output: bool = False) -> dict[str, Any]:
    template = {
        "version": 1,
        "kind": "exact-job",
        "chapters": job.get("chapters", {}),
        "tmdb": job.get("tmdb", False),
        "extra_attachments": job.get("extra_attachments", []),
        "mux_backend": job.get("mux_backend", "auto"),
        "tag_overrides": job.get("tag_overrides", None),
    }
    if include_output and job.get("output"):
        template["output"] = job["output"]
    return {k: v for k, v in template.items() if v not in ({}, None, False, [])}
