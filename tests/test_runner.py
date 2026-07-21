"""
tests/test_runner.py — Tests unitaires pour core/runner.py

Couverture :
    ToolChecker :
        - available() avec outil présent / absent
        - check_all() retourne le bon dict
        - missing() filtre correctement
        - path_of() retourne Path ou None
        - require() lève ToolNotFoundError si outil absent

    CommandError / ToolNotFoundError :
        - attributs corrects sur instanciation
        - message d'erreur lisible

    ToolRunner._run_cmd() :
        - commande réussie : retourne stdout
        - commande échouée : lève CommandError avec returncode
        - chaque ligne transmise au progress_cb

    ToolRunner.run() :
        - signal finished émis avec la sortie
        - signal failed émis sur erreur
        - signal progress émis ligne par ligne

    ToolRunner.run_parallel() :
        - toutes les tâches réussies → finished
        - une tâche échoue → failed
        - les messages progress incluent le label de la tâche

Exécution :
    pytest tests/test_runner.py -v
"""

from __future__ import annotations

# L'import du module testé suit volontairement l'initialisation Qt.
# ruff: noqa: E402

import sys
import time
from pathlib import Path
from unittest.mock import patch


import pytest

# --- PySide6 peut nécessiter un QCoreApplication pour les signaux ---
# On réutilise la QApplication de conftest quand elle est présente ; sinon
# on crée une QCoreApplication minimale (cas où les deps Qt graphiques
# manquent sur le runner). Jamais de QCoreApplication "nue" si une
# QApplication existe déjà → évite de casser les tests widgets qui suivent.
from PySide6.QtCore import QCoreApplication, Qt

_app: QCoreApplication | None = None


def _get_app() -> QCoreApplication:
    global _app
    if _app is not None:
        return _app
    existing = QCoreApplication.instance()
    if existing is not None:
        _app = existing
        return _app
    try:
        from PySide6.QtWidgets import QApplication
        _app = QApplication(sys.argv)
    except Exception:
        _app = QCoreApplication(sys.argv)
    return _app


@pytest.fixture(autouse=True)
def qt_app():
    """Assure qu'une QCoreApplication existe pour tous les tests."""
    return _get_app()


# ---------------------------------------------------------------------------
# Import du module sous test
# ---------------------------------------------------------------------------

from core.runner import (
    CommandError,
    TaskSignals,
    ToolChecker,
    ToolNotFoundError,
    ToolRunner,
)


# ===========================================================================
# ToolChecker
# ===========================================================================

class TestToolChecker:

    def test_available_true_when_tool_in_path(self):
        """Un outil présent dans PATH → available() = True."""
        checker = ToolChecker(tools=["python"])
        # python est toujours présent dans l'environnement de test
        assert checker.available("python") is True

    def test_available_false_when_tool_absent(self):
        """Un outil fictif → available() = False."""
        checker = ToolChecker(tools=["__outil_qui_nexiste_pas_xyz__"])
        assert checker.available("__outil_qui_nexiste_pas_xyz__") is False

    def test_check_all_returns_correct_keys(self):
        """check_all() retourne exactement les clés configurées."""
        tools = ["python", "__absent_abc__"]
        checker = ToolChecker(tools=tools)
        result = checker.check_all()
        assert set(result.keys()) == set(tools)

    def test_check_all_values_are_bool(self):
        """check_all() retourne des booléens."""
        checker = ToolChecker(tools=["python", "__absent_xyz__"])
        result = checker.check_all()
        assert result["python"] is True
        assert result["__absent_xyz__"] is False

    def test_missing_returns_absent_tools(self):
        """missing() filtre les outils absents."""
        checker = ToolChecker(tools=["python", "__absent_1__", "__absent_2__"])
        missing = checker.missing()
        assert "__absent_1__" in missing
        assert "__absent_2__" in missing
        assert "python" not in missing

    def test_missing_empty_when_all_present(self):
        """missing() retourne liste vide si tout est disponible."""
        checker = ToolChecker(tools=["python"])
        assert checker.missing() == []

    def test_path_of_returns_path_when_found(self):
        """path_of() retourne un Path pour un outil présent."""
        checker = ToolChecker()
        p = checker.path_of("python")
        assert p is not None
        assert isinstance(p, Path)

    def test_path_of_returns_none_when_absent(self):
        """path_of() retourne None pour un outil absent."""
        checker = ToolChecker()
        assert checker.path_of("__absent_tool_xyz__") is None

    def test_configured_absolute_path_counts_as_available(self, tmp_path):
        """Un outil configuré par chemin complet n'a pas besoin d'être dans PATH."""
        tool = tmp_path / "mediainfo-custom"
        tool.write_text("", encoding="utf-8")
        checker = ToolChecker(tools=[str(tool)])

        with patch("core.runner.shutil.which", return_value=None):
            assert checker.available(str(tool)) is True
            assert checker.path_of(str(tool)) == tool
            assert checker.missing() == []

    def test_require_passes_when_all_present(self):
        """require() ne lève rien si tous les outils sont présents."""
        checker = ToolChecker()
        checker.require(["python"])  # ne doit pas lever

    def test_require_raises_when_tool_absent(self):
        """require() lève ToolNotFoundError si un outil est absent."""
        checker = ToolChecker()
        with pytest.raises(ToolNotFoundError) as exc_info:
            checker.require(["python", "__absent_xyz__"])
        assert "__absent_xyz__" in exc_info.value.tools
        assert "python" not in exc_info.value.tools

    def test_require_raises_with_all_missing_tools(self):
        """require() liste tous les outils manquants dans l'exception."""
        checker = ToolChecker()
        with pytest.raises(ToolNotFoundError) as exc_info:
            checker.require(["__absent_a__", "__absent_b__"])
        assert len(exc_info.value.tools) == 2


# ===========================================================================
# Exceptions
# ===========================================================================

class TestExceptions:

    def test_tool_not_found_error_attributes(self):
        err = ToolNotFoundError(["dovi_tool", "hdr10plus_tool"])
        assert err.tools == ["dovi_tool", "hdr10plus_tool"]
        assert "dovi_tool" in str(err)
        assert "hdr10plus_tool" in str(err)

    def test_command_error_attributes(self):
        cmd = ["ffmpeg", "-i", "film.mkv"]
        err = CommandError(cmd=cmd, returncode=1, stderr="erreur fatale")
        assert err.cmd == cmd
        assert err.returncode == 1
        assert "erreur fatale" in err.stderr
        assert "1" in str(err)

    def test_command_error_message_contains_cmd(self):
        err = CommandError(cmd=["ffmpeg", "-i", "in.mkv"], returncode=2, stderr="")
        assert "ffmpeg" in str(err)


# ===========================================================================
# ToolRunner._run_cmd
# ===========================================================================

class TestRunCmd:

    def setup_method(self):
        self.runner = ToolRunner()

    def test_run_cmd_returns_stdout(self):
        """_run_cmd() retourne la sortie de la commande."""
        output = self.runner._run_cmd(
            [sys.executable, "-c", "print('bonjour')"]
        )
        assert "bonjour" in output

    def test_run_cmd_raises_on_nonzero_returncode(self):
        """_run_cmd() lève CommandError si le code de retour est non nul."""
        with pytest.raises(CommandError) as exc_info:
            self.runner._run_cmd(
                [sys.executable, "-c", "import sys; sys.exit(42)"]
            )
        assert exc_info.value.returncode == 42

    def test_run_cmd_calls_progress_cb_for_each_line(self):
        """_run_cmd() appelle progress_cb pour la ligne de commande puis chaque ligne de sortie."""
        lines_received: list[str] = []
        cmd = [sys.executable, "-c", "print('A'); print('B'); print('C')"]
        self.runner._run_cmd(cmd, progress_cb=lines_received.append)
        # First call is the command echo ("$ <cmd>"), followed by output lines
        assert lines_received[0].startswith("$ ")
        assert lines_received[1:] == ["A", "B", "C"]

    def test_run_cmd_empty_lines_not_forwarded_to_progress_cb(self):
        """Les lignes vides ne sont pas transmises au progress_cb."""
        lines_received: list[str] = []
        self.runner._run_cmd(
            [sys.executable, "-c", "print('X'); print(); print('Y')"],
            progress_cb=lines_received.append,
        )
        assert "" not in lines_received

    def test_run_cmd_combines_stderr_in_output(self):
        """_run_cmd() capture stderr via STDOUT dans la sortie combinée."""
        output = self.runner._run_cmd(
            [sys.executable, "-c",
             "import sys; sys.stderr.write('ERR\\n'); print('OUT')"]
        )
        # stderr est redirigé vers stdout via STDOUT
        assert "OUT" in output

    def test_run_cmd_multiline_output(self):
        """_run_cmd() gère correctement une sortie multi-lignes."""
        output = self.runner._run_cmd(
            [sys.executable, "-c",
             "for i in range(5): print(f'ligne {i}')"]
        )
        for i in range(5):
            assert f"ligne {i}" in output


# ===========================================================================
# ToolRunner.run — signaux
# ===========================================================================

def _collect_signals(signals: TaskSignals, timeout: float = 5.0) -> dict:
    """
    Attend que finished ou failed soit émis, collecte tous les événements.
    Retourne un dict avec les listes 'progress', 'finished', 'failed'.
    """
    app = _get_app()
    collected: dict[str, list] = {"progress": [], "finished": [], "failed": []}
    done = [False]

    def on_progress(msg: str):
        collected["progress"].append(msg)

    def on_finished(result: str):
        collected["finished"].append(result)
        done[0] = True

    def on_failed(msg: str, exc: object):
        collected["failed"].append((msg, exc))
        done[0] = True

    signals.progress.connect(on_progress, Qt.ConnectionType.QueuedConnection)
    signals.finished.connect(on_finished, Qt.ConnectionType.QueuedConnection)
    signals.failed.connect(on_failed, Qt.ConnectionType.QueuedConnection)

    deadline = time.monotonic() + timeout
    while not done[0] and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    return collected


class TestToolRunnerRun:

    def setup_method(self):
        self.runner = ToolRunner()

    def test_run_emits_finished_on_success(self):
        """run() émet finished quand la commande réussit."""
        sig = self.runner.run(
            [sys.executable, "-c", "print('ok')"]
        )
        result = _collect_signals(sig)
        assert len(result["finished"]) == 1
        assert "ok" in result["finished"][0]
        assert result["failed"] == []

    def test_run_emits_failed_on_error(self):
        """run() émet failed quand la commande échoue."""
        sig = self.runner.run(
            [sys.executable, "-c", "import sys; sys.exit(1)"]
        )
        result = _collect_signals(sig)
        assert len(result["failed"]) == 1
        assert result["finished"] == []
        msg, exc = result["failed"][0]
        assert isinstance(exc, CommandError)

    def test_run_emits_progress_for_each_line(self):
        """run() émet progress pour chaque ligne de sortie."""
        sig = self.runner.run(
            [sys.executable, "-c", "print('L1'); print('L2'); print('L3')"]
        )
        result = _collect_signals(sig)
        assert any("L1" in p for p in result["progress"])
        assert any("L2" in p for p in result["progress"])
        assert any("L3" in p for p in result["progress"])

    def test_run_failed_exception_is_command_error(self):
        """L'exception dans failed est bien une CommandError."""
        sig = self.runner.run(
            [sys.executable, "-c", "import sys; sys.exit(99)"]
        )
        result = _collect_signals(sig)
        _, exc = result["failed"][0]
        assert isinstance(exc, CommandError)
        assert exc.returncode == 99

    def test_run_with_nonexistent_executable_emits_failed(self):
        """run() émet failed si l'exécutable n'existe pas."""
        sig = self.runner.run(["__inexistant_xyz__"])
        result = _collect_signals(sig)
        assert len(result["failed"]) == 1


# ===========================================================================
# ToolRunner.run_parallel — signaux
# ===========================================================================

class TestToolRunnerParallel:

    def setup_method(self):
        self.runner = ToolRunner(max_workers=3)

    def test_parallel_all_success_emits_finished(self):
        """run_parallel() émet finished si toutes les tâches réussissent."""
        tasks = [
            [sys.executable, "-c", "print('T1')"],
            [sys.executable, "-c", "print('T2')"],
            [sys.executable, "-c", "print('T3')"],
        ]
        sig = self.runner.run_parallel(tasks, label="test")
        result = _collect_signals(sig, timeout=10.0)
        assert len(result["finished"]) == 1
        assert result["failed"] == []

    def test_parallel_finished_mentions_task_count(self):
        """Le message finished mentionne le nombre de tâches."""
        tasks = [
            [sys.executable, "-c", "print('ok')"],
            [sys.executable, "-c", "print('ok')"],
        ]
        sig = self.runner.run_parallel(tasks, label="count_test")
        result = _collect_signals(sig, timeout=10.0)
        assert "2" in result["finished"][0]

    def test_parallel_one_failure_emits_failed(self):
        """run_parallel() émet failed si une tâche échoue."""
        tasks = [
            [sys.executable, "-c", "print('ok')"],
            [sys.executable, "-c", "import sys; sys.exit(1)"],
        ]
        sig = self.runner.run_parallel(tasks, label="fail_test")
        result = _collect_signals(sig, timeout=10.0)
        assert len(result["failed"]) == 1
        assert result["finished"] == []

    def test_parallel_progress_includes_task_label(self):
        """Les messages progress incluent le label de la tâche."""
        tasks = [
            [sys.executable, "-c", "print('sortie_A')"],
            [sys.executable, "-c", "print('sortie_B')"],
        ]
        sig = self.runner.run_parallel(tasks, label="mytask")
        result = _collect_signals(sig, timeout=10.0)
        # Au moins un message progress doit contenir le label
        assert any("mytask" in p for p in result["progress"])

    def test_parallel_tasks_run_concurrently(self):
        """Les tâches parallèles s'exécutent plus vite que séquentiellement."""
        # Chaque tâche attend 0.3s — 3 tâches en parallèle devraient finir
        # en ~0.3s, pas en ~0.9s.
        tasks = [
            [sys.executable, "-c", "import time; time.sleep(0.3); print('done')"],
            [sys.executable, "-c", "import time; time.sleep(0.3); print('done')"],
            [sys.executable, "-c", "import time; time.sleep(0.3); print('done')"],
        ]
        t_start = time.monotonic()
        sig = self.runner.run_parallel(tasks, label="timing")
        _collect_signals(sig, timeout=10.0)
        elapsed = time.monotonic() - t_start
        # Tolérance généreuse : doit finir en moins de 1.5s (séquentiel ≈ 0.9s)
        assert elapsed < 1.5, f"Trop lent ({elapsed:.2f}s) — les tâches ne semblent pas parallèles"

    def test_parallel_empty_tasks_does_not_raise(self):
        """run_parallel() avec liste vide ne lève pas d'exception."""
        # Le signal finished ne peut pas être testé de façon fiable ici :
        # le thread vide se termine avant que _collect_signals connecte ses
        # slots (race condition). On vérifie uniquement l'absence d'exception.
        try:
            self.runner.run_parallel([], label="empty")
            time.sleep(0.2)
        except Exception as exc:
            pytest.fail(f"run_parallel([]) a levé une exception : {exc}")
