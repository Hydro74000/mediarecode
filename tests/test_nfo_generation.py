"""
tests/test_nfo_generation.py — Tests pour la génération automatique de fichiers .nfo.

Couverture :
1. config.py  — valeur par défaut, lecture INI, écriture INI, persistance QSettings
2. settings_panel — checkbox generate_nfo exposée, sauvegarde vers INI
3. write_mediainfo_nfo — génération effective, gestion d'erreur silencieuse
4. RemuxWorkflow — NFO généré si generate_nfo=True, skippé si False
5. EncodeWorkflow — NFO généré si generate_nfo=True, skippé si False
6. Setters runtime — set_generate_nfo / set_mediainfo_bin propagation
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixture Qt partagée
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _qt_app(qt_app):
    return qt_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_qsettings():
    inst = MagicMock()
    inst.value.side_effect = lambda key, default=None: default
    return inst


def _make_config(tmp_path: Path, ini_content: str = ""):
    import core.config as cfg_mod
    from core.config import AppConfig

    ini_path = tmp_path / "config.ini"
    ini_path.write_text(ini_content, encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

    return cfg, ini_path


# ===========================================================================
# 1. AppConfig — generate_nfo
# ===========================================================================

class TestAppConfigGenerateNfo:

    def test_default_is_true(self, tmp_path):
        """Sans config, generate_nfo vaut True."""
        cfg, _ = _make_config(tmp_path)
        assert cfg.generate_nfo is True

    def test_ini_false_disables_nfo(self, tmp_path):
        """[metadata] generate_nfo = false désactive la génération."""
        cfg, _ = _make_config(tmp_path, "[metadata]\ngenerate_nfo = false\n")
        assert cfg.generate_nfo is False

    def test_ini_true_enables_nfo(self, tmp_path):
        """[metadata] generate_nfo = true active explicitement la génération."""
        cfg, _ = _make_config(tmp_path, "[metadata]\ngenerate_nfo = true\n")
        assert cfg.generate_nfo is True

    def test_save_persists_generate_nfo_false(self, tmp_path):
        """save() écrit metadata/generate_nfo dans QSettings."""
        import core.config as cfg_mod
        from core.config import AppConfig

        ini_path = tmp_path / "config.ini"
        ini_path.write_text("[metadata]\ngenerate_nfo = false\n", encoding="utf-8")

        with patch("core.config.QSettings") as mock_qs, \
             patch("core.config._app_data_dir", return_value=tmp_path), \
             patch.object(cfg_mod, "_INI_PATH", ini_path):
            inst = _mock_qsettings()
            mock_qs.return_value = inst
            cfg = AppConfig()
            cfg.save()

        set_calls = {c.args[0]: c.args[1] for c in inst.setValue.call_args_list}
        assert set_calls.get("metadata/generate_nfo") == "false"

    def test_save_persists_generate_nfo_true(self, tmp_path):
        """save() écrit 'true' quand generate_nfo est True."""
        import core.config as cfg_mod
        from core.config import AppConfig

        ini_path = tmp_path / "config.ini"
        ini_path.write_text("", encoding="utf-8")

        with patch("core.config.QSettings") as mock_qs, \
             patch("core.config._app_data_dir", return_value=tmp_path), \
             patch.object(cfg_mod, "_INI_PATH", ini_path):
            inst = _mock_qsettings()
            mock_qs.return_value = inst
            cfg = AppConfig()
            cfg.save()

        set_calls = {c.args[0]: c.args[1] for c in inst.setValue.call_args_list}
        assert set_calls.get("metadata/generate_nfo") == "true"

    def test_to_dict_includes_generate_nfo(self, tmp_path):
        """to_dict() expose generate_nfo dans la section metadata."""
        cfg, _ = _make_config(tmp_path)
        d = cfg.to_dict()
        assert "generate_nfo" in d["metadata"]
        assert d["metadata"]["generate_nfo"] is True

    def test_ini_field_groups_includes_generate_nfo(self):
        """INI_FIELD_GROUPS expose un champ generate_nfo dans la section metadata."""
        from core.config import INI_FIELD_GROUPS
        metadata_group = next(g for g in INI_FIELD_GROUPS if g["section"] == "metadata")
        keys = [f["key"] for f in metadata_group["fields"]]
        assert "generate_nfo" in keys

    def test_ini_field_groups_includes_mux_backend(self):
        """La section matroska expose le backend de muxage persistant."""
        from core.config import INI_FIELD_GROUPS
        matroska_group = next(g for g in INI_FIELD_GROUPS if g["section"] == "matroska")
        assert [field["key"] for field in matroska_group["fields"]] == ["mux_backend"]
        assert matroska_group["fields"][0]["attr"] == "matroska_mux_backend"


# ===========================================================================
# 2. SettingsPanel — checkbox generate_nfo
# ===========================================================================

class TestSettingsPanelGenerateNfo:

    def test_checkbox_exists_and_defaults_checked(self, qt_app, tmp_path):
        """Le panneau expose une checkbox generate_nfo cochée par défaut."""
        from ui.panels.settings_panel import SettingsPanel

        cfg, _ = _make_config(tmp_path)
        panel = SettingsPanel(cfg)
        cb = cast(Any, panel.widget_for("metadata", "generate_nfo"))
        assert cb.isChecked() is True

    def test_checkbox_reflects_false_from_config(self, qt_app, tmp_path):
        """La checkbox est décochée si generate_nfo=false dans l'INI."""
        from ui.panels.settings_panel import SettingsPanel

        cfg, _ = _make_config(tmp_path, "[metadata]\ngenerate_nfo = false\n")
        panel = SettingsPanel(cfg)
        cb = cast(Any, panel.widget_for("metadata", "generate_nfo"))
        assert cb.isChecked() is False

    def test_save_writes_false_to_ini(self, qt_app, tmp_path):
        """Décocher la checkbox et sauvegarder écrit generate_nfo = false dans l'INI."""
        import core.config as cfg_mod
        from core.config import AppConfig
        from ui.panels.settings_panel import SettingsPanel

        ini_path = tmp_path / "config.ini"
        ini_path.write_text("", encoding="utf-8")

        with patch("core.config.QSettings") as mock_qs, \
             patch("core.config._app_data_dir", return_value=tmp_path), \
             patch.object(cfg_mod, "_INI_PATH", ini_path):
            mock_qs.return_value = _mock_qsettings()
            cfg = AppConfig()

            panel = SettingsPanel(cfg)
            cb = cast(Any, panel.widget_for("metadata", "generate_nfo"))
            cb.setChecked(False)
            panel._on_save_clicked()

        assert "generate_nfo = false" in ini_path.read_text(encoding="utf-8")
        assert cfg.generate_nfo is False

    def test_save_writes_true_to_ini(self, qt_app, tmp_path):
        """Cocher la checkbox et sauvegarder écrit generate_nfo = true dans l'INI."""
        import core.config as cfg_mod
        from core.config import AppConfig
        from ui.panels.settings_panel import SettingsPanel

        ini_path = tmp_path / "config.ini"
        ini_path.write_text("[metadata]\ngenerate_nfo = false\n", encoding="utf-8")

        with patch("core.config.QSettings") as mock_qs, \
             patch("core.config._app_data_dir", return_value=tmp_path), \
             patch.object(cfg_mod, "_INI_PATH", ini_path):
            mock_qs.return_value = _mock_qsettings()
            cfg = AppConfig()

            panel = SettingsPanel(cfg)
            cb = cast(Any, panel.widget_for("metadata", "generate_nfo"))
            cb.setChecked(True)
            panel._on_save_clicked()

        assert "generate_nfo = true" in ini_path.read_text(encoding="utf-8")
        assert cfg.generate_nfo is True


# ===========================================================================
# 3. write_mediainfo_nfo — fonction module-level
# ===========================================================================

class TestWriteMediainfoNfo:

    def test_writes_nfo_file_with_mediainfo_output(self, tmp_path):
        """Écrit un .nfo contenant stdout de mediainfo."""
        from core.workflows.remux import write_mediainfo_nfo

        mkv = tmp_path / "film.mkv"
        mkv.touch()
        log_cb = MagicMock()

        fake_result = MagicMock()
        fake_result.stdout = "General\nFormat: Matroska\n"

        with patch("core.workflows.remux.subprocess.run", return_value=fake_result) as mock_run:
            write_mediainfo_nfo(mkv, log_cb=log_cb)

        nfo = tmp_path / "film.nfo"
        assert nfo.exists()
        assert "Format: Matroska" in nfo.read_text(encoding="utf-8")
        log_cb.assert_called_once_with("OK", "NFO généré : film.nfo")

        args = mock_run.call_args[0][0]
        kwargs = mock_run.call_args[1] if mock_run.call_args[1] else mock_run.call_args.kwargs
        assert args[0] == "mediainfo"
        assert args[1] == str(mkv.resolve())
        assert "cwd" not in kwargs

    def test_complete_name_is_cleaned_to_filename(self, tmp_path):
        """Complete name ne conserve pas le chemin complet dans le .nfo."""
        from core.workflows.remux import write_mediainfo_nfo

        mkv = tmp_path / "film.mkv"
        mkv.touch()
        fake_result = MagicMock()
        fake_result.stdout = (
            "General\n"
            f"Complete name                            : {mkv.resolve()}\n"
            "Format                                   : Matroska\n"
        )

        with patch("core.workflows.remux.subprocess.run", return_value=fake_result):
            write_mediainfo_nfo(mkv, log_cb=MagicMock())

        nfo_text = (tmp_path / "film.nfo").read_text(encoding="utf-8")
        assert "Complete name                            : film.mkv" in nfo_text
        assert str(tmp_path) not in nfo_text

    def test_complete_name_already_clean_is_preserved(self, tmp_path):
        """Complete name déjà réduit au nom de fichier reste inchangé."""
        from core.workflows.remux import write_mediainfo_nfo

        mkv = tmp_path / "film.mkv"
        mkv.touch()
        fake_result = MagicMock()
        fake_result.stdout = (
            "General\n"
            "Complete name                            : film.mkv\n"
            "Format                                   : Matroska\n"
        )

        with patch("core.workflows.remux.subprocess.run", return_value=fake_result):
            write_mediainfo_nfo(mkv, log_cb=MagicMock())

        nfo_text = (tmp_path / "film.nfo").read_text(encoding="utf-8")
        assert nfo_text == fake_result.stdout

    def test_uses_custom_mediainfo_bin(self, tmp_path):
        """mediainfo_bin personnalisé est passé à subprocess.run."""
        from core.workflows.remux import write_mediainfo_nfo

        mkv = tmp_path / "film.mkv"
        mkv.touch()
        fake_result = MagicMock()
        fake_result.stdout = "info"

        with patch("core.workflows.remux.subprocess.run", return_value=fake_result) as mock_run:
            write_mediainfo_nfo(mkv, log_cb=MagicMock(), mediainfo_bin="/opt/bin/mediainfo")

        assert mock_run.call_args[0][0][0] == "/opt/bin/mediainfo"

    def test_nfo_path_is_sibling_of_mkv(self, tmp_path):
        """Le .nfo est créé dans le même dossier que le MKV."""
        from core.workflows.remux import write_mediainfo_nfo

        subdir = tmp_path / "output"
        subdir.mkdir()
        mkv = subdir / "film.mkv"
        mkv.touch()
        fake_result = MagicMock()
        fake_result.stdout = "data"

        with patch("core.workflows.remux.subprocess.run", return_value=fake_result):
            write_mediainfo_nfo(mkv, log_cb=MagicMock())

        assert (subdir / "film.nfo").exists()

    def test_mediainfo_receives_absolute_output_path(self, tmp_path, monkeypatch):
        """Le chemin passé à mediainfo ne dépend pas du cwd ni d'un affichage relatif."""
        from core.workflows.remux import write_mediainfo_nfo

        subdir = tmp_path / "output"
        subdir.mkdir()
        mkv = subdir / "film.mkv"
        mkv.touch()
        fake_result = MagicMock()
        fake_result.stdout = "data"

        monkeypatch.chdir(tmp_path)
        with patch("core.workflows.remux.subprocess.run", return_value=fake_result) as mock_run:
            write_mediainfo_nfo(Path("output") / "film.mkv", log_cb=MagicMock())

        args = mock_run.call_args[0][0]
        kwargs = mock_run.call_args.kwargs
        assert args[1] == str(mkv.resolve())
        assert "cwd" not in kwargs
        assert (subdir / "film.nfo").exists()

    def test_exception_logs_warn_and_does_not_raise(self, tmp_path):
        """Une exception subprocess est attrapée et logguée en WARN sans lever."""
        from core.workflows.remux import write_mediainfo_nfo

        mkv = tmp_path / "film.mkv"
        mkv.touch()
        log_cb = MagicMock()

        with patch("core.workflows.remux.subprocess.run", side_effect=FileNotFoundError("mediainfo not found")):
            write_mediainfo_nfo(mkv, log_cb=log_cb)

        level, msg = log_cb.call_args[0]
        assert level == "WARN"
        assert "mediainfo not found" in msg
        assert not (tmp_path / "film.nfo").exists()


# ===========================================================================
# 4. RemuxWorkflow — generate_nfo
# ===========================================================================

class TestRemuxWorkflowNfo:

    def test_default_generate_nfo_is_true(self):
        """RemuxWorkflow sans argument a generate_nfo=True."""
        from core.workflows.remux import RemuxWorkflow
        wf = RemuxWorkflow()
        assert wf._generate_nfo is True

    def test_generate_nfo_false_skips_write(self, tmp_path):
        """_write_nfo ne fait rien si _generate_nfo est False."""
        from core.workflows.remux import RemuxWorkflow

        wf = RemuxWorkflow(generate_nfo=False)
        mkv = tmp_path / "film.mkv"
        mkv.touch()

        with patch("core.workflows.remux.write_mediainfo_nfo") as mock_fn:
            wf._write_nfo(mkv)

        mock_fn.assert_not_called()

    def test_generate_nfo_true_calls_write(self, tmp_path):
        """_write_nfo appelle write_mediainfo_nfo si _generate_nfo est True."""
        from core.workflows.remux import RemuxWorkflow

        wf = RemuxWorkflow(generate_nfo=True, mediainfo_bin="/usr/bin/mediainfo")
        mkv = tmp_path / "film.mkv"
        mkv.touch()

        with patch("core.workflows.remux.write_mediainfo_nfo") as mock_fn:
            wf._write_nfo(mkv)

        mock_fn.assert_called_once_with(
            mkv,
            log_cb=wf.log_message.emit,
            mediainfo_bin="/usr/bin/mediainfo",
        )

    def test_set_generate_nfo_updates_flag(self):
        """set_generate_nfo met à jour _generate_nfo."""
        from core.workflows.remux import RemuxWorkflow

        wf = RemuxWorkflow(generate_nfo=True)
        wf.set_generate_nfo(False)
        assert wf._generate_nfo is False
        wf.set_generate_nfo(True)
        assert wf._generate_nfo is True

    def test_set_mediainfo_bin_updates_bin(self):
        """set_mediainfo_bin met à jour _mediainfo_bin."""
        from core.workflows.remux import RemuxWorkflow

        wf = RemuxWorkflow()
        wf.set_mediainfo_bin("/custom/mediainfo")
        assert wf._mediainfo_bin == "/custom/mediainfo"

    def test_constructor_passes_mediainfo_bin_to_write(self, tmp_path):
        """Le mediainfo_bin passé au constructeur est utilisé lors de la génération."""
        from core.workflows.remux import RemuxWorkflow

        wf = RemuxWorkflow(mediainfo_bin="mediainfo-custom")
        mkv = tmp_path / "film.mkv"
        mkv.touch()

        with patch("core.workflows.remux.write_mediainfo_nfo") as mock_fn:
            wf._write_nfo(mkv)

        _, kwargs = mock_fn.call_args
        assert kwargs["mediainfo_bin"] == "mediainfo-custom"


# ===========================================================================
# 5. EncodeWorkflow — generate_nfo
# ===========================================================================

class TestEncodeWorkflowNfo:

    def test_default_generate_nfo_is_true(self):
        """EncodeWorkflow sans argument a _generate_nfo=True."""
        from core.workflows.encode.workflow import EncodeWorkflow
        wf = EncodeWorkflow()
        assert wf._generate_nfo is True

    def test_generate_nfo_false_skips_bind(self, tmp_path):
        """_bind_nfo_write ne déclenche pas write_mediainfo_nfo si _generate_nfo est False."""
        from core.workflows.encode.workflow import EncodeWorkflow
        from core.runner import TaskSignals
        from PySide6.QtCore import QCoreApplication
        import time

        wf = EncodeWorkflow(generate_nfo=False)
        signals = TaskSignals()
        output = tmp_path / "film.mkv"
        output.touch()
        called = []

        with patch("core.workflows.encode.workflow.write_mediainfo_nfo", side_effect=lambda *a, **kw: called.append(1)):
            wf._bind_nfo_write(signals, output)
            signals.finished.emit("done")
            app = QCoreApplication.instance()
            assert app is not None
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                app.processEvents()

        assert called == []

    def test_generate_nfo_true_binds_slot(self, tmp_path):
        """_bind_nfo_write déclenche write_mediainfo_nfo si _generate_nfo est True."""
        from core.workflows.encode.workflow import EncodeWorkflow
        from core.runner import TaskSignals
        from PySide6.QtCore import QCoreApplication
        import time

        wf = EncodeWorkflow(generate_nfo=True, mediainfo_bin="mi-bin")
        signals = TaskSignals()
        output = tmp_path / "film.mkv"
        output.touch()
        called = []

        with patch("core.workflows.encode.workflow.write_mediainfo_nfo", side_effect=lambda *a, **kw: called.append(kw)):
            wf._bind_nfo_write(signals, output)
            signals.finished.emit("done")
            app = QCoreApplication.instance()
            assert app is not None
            deadline = time.monotonic() + 3.0
            while not called and time.monotonic() < deadline:
                app.processEvents()

        assert len(called) == 1
        assert called[0]["mediainfo_bin"] == "mi-bin"

    def test_bound_slot_calls_write_mediainfo_nfo(self, tmp_path):
        """Le slot branché par _bind_nfo_write passe le bon output_path."""
        from core.workflows.encode.workflow import EncodeWorkflow
        from core.runner import TaskSignals
        from PySide6.QtCore import QCoreApplication
        import time

        wf = EncodeWorkflow(generate_nfo=True, mediainfo_bin="mi-bin")
        signals = TaskSignals()
        output = tmp_path / "film.mkv"
        output.touch()
        captured = []

        with patch("core.workflows.encode.workflow.write_mediainfo_nfo", side_effect=lambda *a, **kw: captured.append(a[0])):
            wf._bind_nfo_write(signals, output)
            signals.finished.emit("done")
            app = QCoreApplication.instance()
            assert app is not None
            deadline = time.monotonic() + 3.0
            while not captured and time.monotonic() < deadline:
                app.processEvents()

        assert captured == [output]

    def test_set_generate_nfo_updates_flag(self):
        """set_generate_nfo met à jour _generate_nfo."""
        from core.workflows.encode.workflow import EncodeWorkflow

        wf = EncodeWorkflow(generate_nfo=True)
        wf.set_generate_nfo(False)
        assert wf._generate_nfo is False

    def test_set_mediainfo_bin_updates_bins_dict(self):
        """set_mediainfo_bin met à jour _bins['mediainfo']."""
        from core.workflows.encode.workflow import EncodeWorkflow

        wf = EncodeWorkflow(mediainfo_bin="mediainfo-default")
        wf.set_mediainfo_bin("/opt/mediainfo")
        assert wf._bins["mediainfo"] == "/opt/mediainfo"

    def test_constructor_mediainfo_bin_stored_in_bins(self):
        """Le mediainfo_bin passé au constructeur est stocké dans _bins."""
        from core.workflows.encode.workflow import EncodeWorkflow

        wf = EncodeWorkflow(mediainfo_bin="/custom/mi")
        assert wf._bins["mediainfo"] == "/custom/mi"

    def test_encode_uses_dedicated_postprocess_service(self):
        """EncodeWorkflow n'embarque plus un RemuxWorkflow interne pour le post-traitement."""
        from core.workflows.common.remux_postprocess import RemuxPostprocessService
        from core.workflows.encode.workflow import EncodeWorkflow

        wf = EncodeWorkflow(generate_nfo=True)
        assert isinstance(wf._postprocess_service, RemuxPostprocessService)
