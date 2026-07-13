"""Typed option objects at the argparse boundary."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any

from core.config import AppConfig

from cli.logging import Logger


@dataclass(frozen=True)
class CommonOptions:
    ffmpeg: str | None = None
    ffprobe: str | None = None
    mediainfo: str | None = None
    work_dir: str | None = None
    threads: int | None = None
    log_format: str = "text"
    verbose: bool = False
    nfo: bool | None = None
    writing_application: str = ""

    @classmethod
    def from_namespace(cls, args: argparse.Namespace) -> "CommonOptions":
        return cls(
            ffmpeg=getattr(args, "ffmpeg", None),
            ffprobe=getattr(args, "ffprobe", None),
            mediainfo=getattr(args, "mediainfo", None),
            work_dir=getattr(args, "work_dir", None),
            threads=getattr(args, "threads", None),
            log_format=getattr(args, "log_format", "text"),
            verbose=bool(getattr(args, "verbose", False)),
            nfo=getattr(args, "nfo", None),
            writing_application=str(getattr(args, "writing_application", "") or ""),
        )


@dataclass(frozen=True)
class JobOverrides:
    config: str | None = None
    template: str | None = None
    input: list[str] | None = None
    output: str | None = None
    auto_tmdb: bool = False
    tmdb: bool = False
    tmdb_id: int | None = None
    tmdb_apikey: str = ""
    output_template: str = ""
    output_all: bool = False
    no_cover: bool = False
    no_attach: bool = False
    mux_backend: str | None = None

    @classmethod
    def from_namespace(cls, args: argparse.Namespace) -> "JobOverrides":
        return cls(
            config=getattr(args, "config", None),
            template=getattr(args, "template", None),
            input=getattr(args, "input", None),
            output=getattr(args, "output", None),
            auto_tmdb=bool(getattr(args, "auto_tmdb", False)),
            tmdb=bool(getattr(args, "tmdb", False)),
            tmdb_id=getattr(args, "tmdb_id", None),
            tmdb_apikey=str(getattr(args, "tmdb_apikey", "") or ""),
            output_template=str(getattr(args, "output_template", "") or ""),
            output_all=bool(getattr(args, "output_all", False)),
            no_cover=bool(getattr(args, "no_cover", False)),
            no_attach=bool(getattr(args, "no_attach", False)),
            mux_backend=getattr(args, "mux_backend", None),
        )


@dataclass(frozen=True)
class CliContext:
    config: AppConfig
    logger: Logger
    options: CommonOptions


def common_options(args: argparse.Namespace) -> CommonOptions:
    return CommonOptions.from_namespace(args)


def cli_context(args: argparse.Namespace, config: AppConfig, logger: Logger) -> CliContext:
    return CliContext(config=config, logger=logger, options=common_options(args))


def namespace_value(args: argparse.Namespace, name: str, default: Any = None) -> Any:
    return getattr(args, name, default)
