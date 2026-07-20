"""tests/test_real_media_guard.py — Règles de refus du garde corpus réel."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.real_media_guard import (
    GIB,
    GuardReport,
    RealMediaLock,
    evaluate,
    parse_meminfo,
    run_guarded,
    memory_limit_mechanism,
    wrap_with_memory_limit,
)


def _report(**overrides) -> GuardReport:
    base = GuardReport(
        sources=("clip.mkv",),
        source_bytes=1 * GIB,
        estimated_output_bytes=1 * GIB,
        estimated_temp_bytes=1 * GIB,
        estimated_peak_rss_bytes=1 * GIB,
        mem_available_bytes=16 * GIB,
        swap_used_bytes=0,
        disk_free_bytes=100 * GIB,
        workdir="/cache/muxiveo",
        workdir_filesystem="btrfs",
        memory_limit_mechanism="cgroup",
    )
    return replace(base, **overrides)


class TestEvaluateRules:

    def test_nominal_case_is_allowed(self) -> None:
        decision = evaluate(_report())
        assert decision.allowed is True
        assert decision.reasons == ()

    def test_low_mem_available_is_refused(self) -> None:
        decision = evaluate(_report(mem_available_bytes=3 * GIB))
        assert decision.allowed is False
        assert any("MemAvailable" in reason for reason in decision.reasons)

    def test_peak_over_half_available_is_refused(self) -> None:
        decision = evaluate(_report(
            mem_available_bytes=8 * GIB, estimated_peak_rss_bytes=5 * GIB,
        ))
        assert decision.allowed is False
        assert any("Pic RAM" in reason for reason in decision.reasons)

    def test_disk_below_safety_margin_is_refused(self) -> None:
        # 1,25 × (1 + 1) + 2 = 4,5 Gio requis
        decision = evaluate(_report(disk_free_bytes=4 * GIB))
        assert decision.allowed is False
        assert any("Espace disque" in reason for reason in decision.reasons)

    def test_tmpfs_workdir_is_refused(self) -> None:
        decision = evaluate(_report(workdir_filesystem="tmpfs"))
        assert decision.allowed is False
        assert any("RAM" in reason for reason in decision.reasons)

    def test_concurrent_test_is_refused(self) -> None:
        decision = evaluate(_report(), lock_held_by_other=True)
        assert decision.allowed is False
        assert any("déjà actif" in reason for reason in decision.reasons)

    def test_no_memory_limit_restricts_source_size(self) -> None:
        decision = evaluate(_report(memory_limit_mechanism="", source_bytes=3 * GIB))
        assert decision.allowed is False
        assert any("limite mémoire" in reason for reason in decision.reasons)
        small = evaluate(_report(memory_limit_mechanism="", source_bytes=1 * GIB))
        assert small.allowed is True


class TestSystemProbes:

    def test_parse_meminfo(self) -> None:
        content = (
            "MemTotal:       32000000 kB\n"
            "MemAvailable:   16000000 kB\n"
            "SwapTotal:       8000000 kB\n"
            "SwapFree:        6000000 kB\n"
        )
        mem_available, swap_used = parse_meminfo(content)
        assert mem_available == 16_000_000 * 1024
        assert swap_used == 2_000_000 * 1024

    def test_wrap_with_memory_limit(self) -> None:
        cgroup = wrap_with_memory_limit(["ffmpeg", "-i", "in"], 4 * GIB, "cgroup")
        assert cgroup[0] == "systemd-run"
        assert f"MemoryMax={4 * GIB}" in cgroup
        prlimit = wrap_with_memory_limit(["ffmpeg"], 4 * GIB, "prlimit")
        assert prlimit[:2] == ["prlimit", f"--as={4 * GIB}"]
        assert wrap_with_memory_limit(["ffmpeg"], 4 * GIB, "") == ["ffmpeg"]


class TestLockAndRun:

    def test_lock_lifecycle(self, tmp_path: Path) -> None:
        lock = RealMediaLock(tmp_path / "guard.lock")
        assert lock.held_by_other() is False
        with lock:
            assert lock.path.read_text().startswith(str(os.getpid()) + " ")
            assert lock.held_by_other() is False  # tenu par ce processus
        assert lock.path.exists()  # fichier diagnostique, verrou noyau libéré
        assert RealMediaLock(lock.path).held_by_other() is False

    def test_orphan_lock_is_recovered(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "guard.lock"
        lock_path.write_text("999999999")  # PID inexistant
        assert RealMediaLock(lock_path).held_by_other() is False

    def test_second_lock_cannot_enter(self, tmp_path: Path) -> None:
        first = RealMediaLock(tmp_path / "guard.lock")
        second = RealMediaLock(first.path)
        with first:
            assert second.held_by_other() is True
            with pytest.raises(RuntimeError):
                second.acquire()
        with second:
            assert second.held_by_other() is False

    def test_systemd_run_failure_falls_back_to_prlimit(self) -> None:
        def fake_which(name: str):
            return f"/usr/bin/{name}"

        failed = MagicMock(returncode=1)
        succeeded = MagicMock(returncode=0)
        with patch("scripts.real_media_guard.shutil.which", side_effect=fake_which), patch(
            "scripts.real_media_guard.subprocess.run", side_effect=[failed, succeeded],
        ):
            assert memory_limit_mechanism() == "prlimit"

    def test_run_guarded_reports_returncode_and_duration(self, tmp_path: Path) -> None:
        result = run_guarded(
            ["python3", "-c", "print('ok')"], timeout_s=30.0, cwd=tmp_path,
        )
        assert result.returncode == 0
        assert result.duration_s >= 0.0
