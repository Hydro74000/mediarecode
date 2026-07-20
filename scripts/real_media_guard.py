#!/usr/bin/env python3
"""Garde-fou obligatoire des tests sur corpus média réel.

Préflight : calcule et journalise tailles, estimations, mémoire disponible,
swap, espace disque et système de fichiers du dossier de travail, puis
refuse le test si les seuils du protocole ne sont pas respectés :

- ``MemAvailable`` < 4 Gio ;
- pic RAM estimé > 50 % de ``MemAvailable`` ;
- espace libre < 1,25 × (sortie + temporaires) + 2 Gio ;
- dossier de travail sur tmpfs (RAM) ;
- un autre test média réel déjà actif (verrou).

Exécution : séquentielle (verrou fichier), timeout obligatoire, limite
mémoire via cgroup utilisateur (``systemd-run --user``) ou ``prlimit`` ;
sans mécanisme de limite, seuls les clips < 2 Gio sont autorisés. Le pic
RSS est enregistré via ``/usr/bin/time -v``.

Le corpus est fourni via ``MUXIVEO_REAL_CORPUS_DIR`` (lecture seule) ; les
sorties vont dans ``$XDG_CACHE_HOME/muxiveo/real-media-tests``.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time as time_module
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TextIO

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.matroska.reader import MatroskaReader


GIB = 1024 ** 3

#: Seuils du protocole (voir Plan_Matroska.md, section « Protocole obligatoire »).
MIN_MEM_AVAILABLE_BYTES = 4 * GIB
PEAK_RSS_MAX_FRACTION_OF_AVAILABLE = 0.50
DISK_SAFETY_FACTOR = 1.25
DISK_SAFETY_MARGIN_BYTES = 2 * GIB
UNLIMITED_MAX_SOURCE_BYTES = 2 * GIB

ENV_CORPUS_DIR = "MUXIVEO_REAL_CORPUS_DIR"


def default_output_root() -> Path:
    """Dossier de sorties des tests réels (jamais dans ``/tmp`` ni le corpus)."""
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return cache_home / "muxiveo" / "real-media-tests"


def default_lock_path() -> Path:
    return default_output_root() / ".real-media-guard.lock"


# =============================================================================
# Mesures système
# =============================================================================

def parse_meminfo(content: str) -> tuple[int, int]:
    """Retourne ``(mem_available_bytes, swap_used_bytes)`` depuis /proc/meminfo."""
    values: dict[str, int] = {}
    for line in content.splitlines():
        match = re.match(r"^(\w+):\s+(\d+)\s*kB", line)
        if match:
            values[match.group(1)] = int(match.group(2)) * 1024
    mem_available = values.get("MemAvailable", 0)
    swap_used = max(0, values.get("SwapTotal", 0) - values.get("SwapFree", 0))
    return mem_available, swap_used


def read_meminfo(path: Path = Path("/proc/meminfo")) -> tuple[int, int]:
    return parse_meminfo(path.read_text(encoding="ascii", errors="replace"))


def filesystem_type(path: Path) -> str:
    """Système de fichiers du point de montage de ``path`` (via findmnt)."""
    try:
        result = subprocess.run(
            ["findmnt", "-no", "FSTYPE", "--target", str(path)],
            capture_output=True, text=True, check=False,
        )
    except (FileNotFoundError, OSError):
        return ""
    return (result.stdout or "").strip()


def disk_free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def probe_duration_and_bitrate(path: Path, ffprobe_bin: str = "ffprobe") -> tuple[float | None, int | None]:
    """Durée (s) et bitrate global (b/s) via ffprobe ; (None, None) si indisponible."""
    try:
        result = subprocess.run(
            [
                ffprobe_bin, "-v", "quiet", "-print_format", "json",
                "-show_entries", "format=duration,bit_rate", str(path),
            ],
            capture_output=True, text=True, check=False,
        )
        data = json.loads(result.stdout or "{}").get("format", {})
        duration = float(data["duration"]) if "duration" in data else None
        bit_rate = int(data["bit_rate"]) if "bit_rate" in data else None
        return duration, bit_rate
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None, None


# =============================================================================
# Décision de préflight (fonction pure, testable avec valeurs injectées)
# =============================================================================

@dataclass(frozen=True)
class GuardReport:
    """Mesures et estimations journalisées avant chaque test réel."""

    sources: tuple[str, ...]
    source_bytes: int
    durations_s: tuple[float, ...] = ()
    bitrates_bps: tuple[int, ...] = ()
    selected_tracks: tuple[str, ...] = ()
    attachment_bytes: int = 0
    estimated_output_bytes: int = 0
    estimated_temp_bytes: int = 0
    estimated_peak_rss_bytes: int = 0
    mem_available_bytes: int = 0
    swap_used_bytes: int = 0
    disk_free_bytes: int = 0
    workdir: str = ""
    workdir_filesystem: str = ""
    memory_limit_mechanism: str = ""  # "cgroup" | "prlimit" | ""


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reasons: tuple[str, ...]
    report: GuardReport

    def to_json(self) -> str:
        return json.dumps(
            {"allowed": self.allowed, "reasons": list(self.reasons), "report": asdict(self.report)},
            ensure_ascii=False, indent=2,
        )


def evaluate(report: GuardReport, *, lock_held_by_other: bool = False) -> GuardDecision:
    """Applique les règles de refus du protocole sur des mesures déjà collectées."""
    reasons: list[str] = []
    if lock_held_by_other:
        reasons.append("Un autre test média réel est déjà actif (verrou présent).")
    if report.mem_available_bytes < MIN_MEM_AVAILABLE_BYTES:
        reasons.append(
            f"MemAvailable insuffisant : {report.mem_available_bytes / GIB:.2f} Gio < 4 Gio."
        )
    if (
        report.estimated_peak_rss_bytes
        and report.mem_available_bytes
        and report.estimated_peak_rss_bytes > PEAK_RSS_MAX_FRACTION_OF_AVAILABLE * report.mem_available_bytes
    ):
        reasons.append(
            f"Pic RAM estimé ({report.estimated_peak_rss_bytes / GIB:.2f} Gio) "
            f"> 50 % de MemAvailable ({report.mem_available_bytes / GIB:.2f} Gio)."
        )
    required_disk = round(
        DISK_SAFETY_FACTOR * (report.estimated_output_bytes + report.estimated_temp_bytes)
        + DISK_SAFETY_MARGIN_BYTES
    )
    if report.disk_free_bytes < required_disk:
        reasons.append(
            f"Espace disque insuffisant : {report.disk_free_bytes / GIB:.2f} Gio libres "
            f"< {required_disk / GIB:.2f} Gio requis (1,25 × (sortie + temporaires) + 2 Gio)."
        )
    if report.workdir_filesystem.lower() in {"tmpfs", "ramfs"}:
        reasons.append(
            f"Dossier de travail en RAM ({report.workdir_filesystem}) : {report.workdir}."
        )
    if not report.memory_limit_mechanism and report.source_bytes > UNLIMITED_MAX_SOURCE_BYTES:
        reasons.append(
            "Aucun mécanisme de limite mémoire (cgroup/prlimit) : "
            f"sources limitées à 2 Gio, obtenu {report.source_bytes / GIB:.2f} Gio."
        )
    return GuardDecision(allowed=not reasons, reasons=tuple(reasons), report=report)


# =============================================================================
# Verrou d'exécution séquentielle
# =============================================================================

class RealMediaLock:
    """Verrou noyau : un seul test média réel actif à la fois."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_lock_path()
        self._handle: TextIO | None = None

    def held_by_other(self) -> bool:
        if self._handle is not None:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = self.path.open("a+", encoding="utf-8")
        except OSError:
            return True
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return True
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
        return False

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeError(f"Verrou test réel déjà actif : {self.path}")
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()} {' '.join(sys.argv)}\n")
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "RealMediaLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


# =============================================================================
# Limite mémoire et exécution mesurée
# =============================================================================

def memory_limit_mechanism() -> str:
    """Mécanisme de limite mémoire disponible : cgroup utilisateur ou prlimit."""
    if shutil.which("systemd-run"):
        try:
            probe = subprocess.run(
                [
                    "systemd-run", "--user", "--scope", "--quiet",
                    "-p", "MemoryMax=268435456", "true",
                ],
                capture_output=True, text=True, check=False, timeout=10,
            )
            if probe.returncode == 0:
                return "cgroup"
        except (OSError, subprocess.TimeoutExpired):
            pass
    if shutil.which("prlimit"):
        try:
            probe = subprocess.run(
                ["prlimit", "--as=268435456", "true"],
                capture_output=True, text=True, check=False, timeout=10,
            )
            if probe.returncode == 0:
                return "prlimit"
        except (OSError, subprocess.TimeoutExpired):
            pass
    return ""


def wrap_with_memory_limit(cmd: list[str], limit_bytes: int, mechanism: str) -> list[str]:
    """Encapsule ``cmd`` sous la limite mémoire demandée."""
    if mechanism == "cgroup":
        return [
            "systemd-run", "--user", "--scope", "--quiet",
            "-p", f"MemoryMax={limit_bytes}", "-p", "MemorySwapMax=0",
            *cmd,
        ]
    if mechanism == "prlimit":
        return ["prlimit", f"--as={limit_bytes}", *cmd]
    return cmd


@dataclass(frozen=True)
class GuardedRunResult:
    returncode: int
    duration_s: float
    peak_rss_bytes: int | None
    stdout: str = ""
    time_output: str = ""


def run_guarded(
    cmd: list[str],
    *,
    timeout_s: float,
    memory_limit_bytes: int | None = None,
    mechanism: str | None = None,
    cwd: Path | None = None,
) -> GuardedRunResult:
    """Exécute ``cmd`` avec timeout, limite mémoire et mesure du pic RSS.

    Le pic RSS est extrait de ``/usr/bin/time -v`` (« Maximum resident set
    size (kbytes) »).
    """
    wrapped = list(cmd)
    if memory_limit_bytes:
        wrapped = wrap_with_memory_limit(
            wrapped,
            memory_limit_bytes,
            memory_limit_mechanism() if mechanism is None else mechanism,
        )
    time_bin = "/usr/bin/time"
    use_time = Path(time_bin).is_file()
    if use_time:
        wrapped = [time_bin, "-v", *wrapped]
    started = time_module.monotonic()
    result = subprocess.run(
        wrapped, capture_output=True, text=True, timeout=timeout_s,
        cwd=str(cwd) if cwd else None, check=False,
    )
    elapsed = time_module.monotonic() - started
    peak_rss: int | None = None
    time_output = result.stderr or ""
    match = re.search(r"Maximum resident set size \(kbytes\):\s*(\d+)", time_output)
    if match:
        peak_rss = int(match.group(1)) * 1024
    return GuardedRunResult(
        returncode=result.returncode,
        duration_s=elapsed,
        peak_rss_bytes=peak_rss,
        stdout=result.stdout or "",
        time_output=time_output,
    )


# =============================================================================
# Préflight complet
# =============================================================================

def preflight(
    sources: list[Path],
    *,
    work_dir: Path,
    estimated_output_bytes: int,
    estimated_temp_bytes: int,
    estimated_peak_rss_bytes: int,
    selected_tracks: list[str] | None = None,
    attachment_bytes: int = 0,
    ffprobe_bin: str = "ffprobe",
    lock: RealMediaLock | None = None,
    output: Path | None = None,
    check_lock: bool = True,
) -> GuardDecision:
    """Collecte les mesures puis applique :func:`evaluate`."""
    resolved_sources = [source.expanduser().resolve() for source in sources]
    work_dir = work_dir.expanduser().resolve()
    corpus_value = os.environ.get(ENV_CORPUS_DIR, "").strip()
    corpus_dir = Path(corpus_value).expanduser().resolve() if corpus_value else None
    source_bytes = 0
    durations: list[float] = []
    bitrates: list[int] = []
    measured_attachment_bytes = 0
    for source in resolved_sources:
        source_bytes += source.stat().st_size
        duration, bitrate = probe_duration_and_bitrate(source, ffprobe_bin)
        if duration is not None:
            durations.append(duration)
        if bitrate is not None:
            bitrates.append(bitrate)
        if source.suffix.lower() in {".mkv", ".mka", ".mks", ".mk3d", ".webm"}:
            try:
                with source.open("rb") as handle:
                    is_ebml = handle.read(4) == b"\x1a\x45\xdf\xa3"
                if is_ebml:
                    measured_attachment_bytes += sum(
                        header.size for header in MatroskaReader(source).attachment_headers()
                    )
            except (OSError, ValueError):
                pass
    work_dir.mkdir(parents=True, exist_ok=True)
    mem_available, swap_used = read_meminfo()
    report = GuardReport(
        sources=tuple(str(source) for source in resolved_sources),
        source_bytes=source_bytes,
        durations_s=tuple(durations),
        bitrates_bps=tuple(bitrates),
        selected_tracks=tuple(selected_tracks or ()),
        attachment_bytes=attachment_bytes or measured_attachment_bytes,
        estimated_output_bytes=estimated_output_bytes,
        estimated_temp_bytes=estimated_temp_bytes,
        estimated_peak_rss_bytes=estimated_peak_rss_bytes,
        mem_available_bytes=mem_available,
        swap_used_bytes=swap_used,
        disk_free_bytes=disk_free_bytes(work_dir),
        workdir=str(work_dir),
        workdir_filesystem=filesystem_type(work_dir),
        memory_limit_mechanism=memory_limit_mechanism(),
    )
    guard_lock = lock or RealMediaLock()
    decision = evaluate(
        report,
        lock_held_by_other=check_lock and guard_lock.held_by_other(),
    )
    reasons = list(decision.reasons)
    if work_dir == Path("/tmp") or Path("/tmp") in work_dir.parents:
        reasons.append(f"Dossier de travail interdit sous /tmp : {work_dir}.")
    resolved_output = output.expanduser().resolve() if output is not None else None
    if corpus_dir is not None:
        if work_dir == corpus_dir or corpus_dir in work_dir.parents:
            reasons.append(f"Dossier de travail situé dans le corpus réel : {work_dir}.")
        if resolved_output is not None and (
            resolved_output == corpus_dir or corpus_dir in resolved_output.parents
        ):
            reasons.append(f"Sortie située dans le corpus réel : {resolved_output}.")
    return GuardDecision(allowed=not reasons, reasons=tuple(reasons), report=report)


# =============================================================================
# CLI
# =============================================================================

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    pre = sub.add_parser("preflight", help="Vérifie les seuils avant un test réel.")
    pre.add_argument("--source", action="append", required=True, type=Path, dest="sources")
    pre.add_argument("--work-dir", type=Path, default=default_output_root())
    pre.add_argument("--output-estimate", type=int, required=True, help="Taille estimée de la sortie (octets).")
    pre.add_argument("--temp-estimate", type=int, default=0, help="Taille estimée des temporaires (octets).")
    pre.add_argument("--peak-estimate", type=int, default=0, help="Pic RAM estimé (octets).")
    pre.add_argument("--ffprobe", default="ffprobe")

    run = sub.add_parser(
        "run",
        help="Préflight et exécution indivisibles sous verrou (commande autorisée).",
    )
    run.add_argument("--source", action="append", required=True, type=Path, dest="sources")
    run.add_argument("--work-dir", type=Path, default=default_output_root())
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--output-estimate", type=int, required=True)
    run.add_argument("--temp-estimate", type=int, default=0)
    run.add_argument("--peak-estimate", type=int, default=0)
    run.add_argument("--ffprobe", default="ffprobe")
    run.add_argument("--timeout", type=float, required=True, help="Timeout en secondes.")
    run.add_argument("--memory-limit", type=int, default=0, help="Limite mémoire en octets (0 = aucune).")
    run.add_argument("cmd", nargs=argparse.REMAINDER, help="Commande à exécuter (après --).")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "preflight":
        decision = preflight(
            list(args.sources),
            work_dir=args.work_dir,
            estimated_output_bytes=args.output_estimate,
            estimated_temp_bytes=args.temp_estimate,
            estimated_peak_rss_bytes=args.peak_estimate,
            ffprobe_bin=args.ffprobe,
        )
        print(decision.to_json())
        return 0 if decision.allowed else 2
    if args.command == "run":
        cmd = [part for part in args.cmd if part != "--"]
        if not cmd:
            print("Aucune commande fournie.", file=sys.stderr)
            return 2
        with RealMediaLock():
            decision = preflight(
                list(args.sources),
                work_dir=args.work_dir,
                output=args.output,
                estimated_output_bytes=args.output_estimate,
                estimated_temp_bytes=args.temp_estimate,
                estimated_peak_rss_bytes=args.peak_estimate,
                ffprobe_bin=args.ffprobe,
                check_lock=False,
            )
            if not decision.allowed:
                print(decision.to_json())
                return 2
            result = run_guarded(
                cmd,
                timeout_s=args.timeout,
                memory_limit_bytes=args.memory_limit or None,
                mechanism=decision.report.memory_limit_mechanism,
                cwd=args.work_dir,
            )
        peak = f"{result.peak_rss_bytes}" if result.peak_rss_bytes is not None else "inconnu"
        print(json.dumps({
            "returncode": result.returncode,
            "duration_s": round(result.duration_s, 3),
            "peak_rss_bytes": result.peak_rss_bytes,
            "peak_rss_label": peak,
            "memory_limit_mechanism": decision.report.memory_limit_mechanism,
            "command_stdout": result.stdout[-4000:],
            "command_stderr": result.time_output[-4000:] if result.returncode else "",
        }, ensure_ascii=False))
        return result.returncode
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
