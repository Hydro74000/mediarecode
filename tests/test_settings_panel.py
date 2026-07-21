"""
tests/test_settings_panel.py — Régressions ciblées pour le panneau des réglages.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock, patch

from core.i18n import translate_text


def _mock_qsettings():
    inst = MagicMock()
    inst.value.side_effect = lambda key, default=None: default
    return inst


def _field_widget(panel: object, section: str, key: str) -> Any:
    return cast(Any, panel).widget_for(section, key)


def test_settings_panel_language_combo_shows_full_language_name(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nlanguage = fre\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

    panel = SettingsPanel(cfg)
    combo = _field_widget(panel, "ui", "language")
    assert combo.currentData() == "fra"
    assert "=" not in combo.currentText()
    assert "Français" in combo.currentText() or "French" in combo.currentText()


def test_settings_panel_writes_selected_language_to_ini(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nlanguage = eng\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        combo = _field_widget(panel, "ui", "language")
        index = combo.findData("fra")
        assert index >= 0
        combo.setCurrentIndex(index)
        panel._on_save_clicked()

        assert "language = fra" in ini_path.read_text(encoding="utf-8")
        assert cfg.language == "fra"


def test_settings_panel_writes_selected_startup_panel_to_ini(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nstartup_panel = dashboard\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        combo = _field_widget(panel, "ui", "startup_panel")
        index = combo.findData("encoding")
        assert index >= 0
        combo.setCurrentIndex(index)
        panel._on_save_clicked()

        assert "startup_panel = encoding" in ini_path.read_text(encoding="utf-8")
        assert cfg.startup_panel == "encoding"


def test_settings_panel_writes_startup_menu_compact_to_ini(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nstartup_menu_compact = false\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        checkbox = _field_widget(panel, "ui", "startup_menu_compact")
        checkbox.setChecked(True)
        panel._on_save_clicked()

        assert "startup_menu_compact = true" in ini_path.read_text(encoding="utf-8")
        assert cfg.startup_menu_compact is True


def test_settings_panel_writes_startup_logs_expanded_to_ini(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nstartup_logs_expanded = false\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        checkbox = _field_widget(panel, "ui", "startup_logs_expanded")
        checkbox.setChecked(True)
        panel._on_save_clicked()

        assert "startup_logs_expanded = true" in ini_path.read_text(encoding="utf-8")
        assert cfg.startup_logs_expanded is True


def test_settings_panel_writes_enable_file_logging_to_ini(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nenable_file_logging = false\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        checkbox = _field_widget(panel, "ui", "enable_file_logging")
        checkbox.setChecked(True)
        panel._on_save_clicked()

        assert "enable_file_logging = true" in ini_path.read_text(encoding="utf-8")
        assert cfg.enable_file_logging is True


def test_settings_panel_writes_file_logging_level_to_ini(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nfile_logging_level = standard\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        combo = _field_widget(panel, "ui", "file_logging_level")
        index = combo.findData("verbose")
        assert index >= 0
        combo.setCurrentIndex(index)
        panel._on_save_clicked()

        assert "file_logging_level = verbose" in ini_path.read_text(encoding="utf-8")
        assert cfg.file_logging_level == "verbose"


def test_settings_panel_disables_file_logging_level_when_file_logging_is_off(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nenable_file_logging = false\nfile_logging_level = verbose\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        checkbox = _field_widget(panel, "ui", "enable_file_logging")
        combo = _field_widget(panel, "ui", "file_logging_level")
        assert combo.isEnabled() is False

        checkbox.setChecked(True)
        assert combo.isEnabled() is True


def test_settings_panel_reload_resyncs_file_logging_level_state(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nenable_file_logging = true\nfile_logging_level = verbose\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        checkbox = _field_widget(panel, "ui", "enable_file_logging")
        combo = _field_widget(panel, "ui", "file_logging_level")

        assert checkbox.isChecked() is True
        assert combo.isEnabled() is True

        ini_path.write_text("[ui]\nenable_file_logging = false\nfile_logging_level = verbose\n", encoding="utf-8")
        panel._on_reload_clicked()

        assert checkbox.isChecked() is False
        assert combo.isEnabled() is False


def test_settings_panel_prefills_verbose_log_dir_full_path(tmp_path, qt_app):
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

        edit = _field_widget(panel, "ui", "verbose_log_dir")
        assert edit.text() == str(tmp_path / "logs")


def test_settings_panel_writes_verbose_log_dir_to_ini(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    target = tmp_path / "custom_logs"
    ini_path.write_text("[ui]\nverbose_log_dir = /tmp/old_logs\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        edit = _field_widget(panel, "ui", "verbose_log_dir")
        edit.setText(str(target))
        panel._on_save_clicked()

        assert f"verbose_log_dir = {target}" in ini_path.read_text(encoding="utf-8")
        assert cfg.verbose_log_dir == target


def test_settings_panel_rejects_empty_verbose_log_dir_when_logging_enabled(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nenable_file_logging = true\nverbose_log_dir = /tmp/logs\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        checkbox = _field_widget(panel, "ui", "enable_file_logging")
        checkbox.setChecked(True)
        edit = _field_widget(panel, "ui", "verbose_log_dir")
        edit.setText("")
        with patch("ui.panels.settings_panel.QMessageBox.warning") as warn:
            panel._on_save_clicked()

        warn.assert_called_once()
        assert "verbose_log_dir = /tmp/logs" in ini_path.read_text(encoding="utf-8")


def test_settings_panel_writes_ui_scale_percent_to_ini(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nui_scale_percent = 100\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        spin = _field_widget(panel, "ui", "ui_scale_percent")
        spin.setValue(125)
        panel._on_save_clicked()

        assert "ui_scale_percent = 130" in ini_path.read_text(encoding="utf-8")
        assert cfg.ui_scale_percent == 130


def test_settings_panel_writes_sync_rewrite_enabled_to_ini(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[sync]\nrewrite_enabled = false\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        checkbox = _field_widget(panel, "sync", "rewrite_enabled")
        assert checkbox.isChecked() is False
        checkbox.setChecked(True)
        panel._on_save_clicked()

        assert "rewrite_enabled = true" in ini_path.read_text(encoding="utf-8")
        assert cfg.sync_rewrite_enabled is True


def test_settings_panel_writes_advanced_audio_sync_rewrite_and_tooltip(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[sync]\nadvanced_audio_rewrite_enabled = false\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        checkbox = _field_widget(panel, "sync", "advanced_audio_rewrite_enabled")
        assert checkbox.isChecked() is False
        assert "TrueHD/MLP" in checkbox.toolTip()
        assert "Atmos/DTS:X" in checkbox.toolTip()
        checkbox.setChecked(True)
        panel._on_save_clicked()

        content = ini_path.read_text(encoding="utf-8")
        assert "advanced_audio_rewrite_enabled = true" in content
        assert cfg.sync_advanced_audio_rewrite_enabled is True


def test_settings_panel_does_not_expose_legacy_audio_encoding_settings(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text(
        "[audio_encoding]\n"
        "default_bitrate_per_channel_kbps = 192\n"
        "bitrate_step_per_channel_kbps = 64\n",
        encoding="utf-8",
    )

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        assert ("audio_encoding", "default_bitrate_per_channel_kbps") not in panel._field_widgets
        assert ("audio_encoding", "bitrate_step_per_channel_kbps") not in panel._field_widgets
        panel._on_save_clicked()

        ini_text = ini_path.read_text(encoding="utf-8")
        assert "default_bitrate_per_channel_kbps" not in ini_text
        assert "bitrate_step_per_channel_kbps" not in ini_text
        assert not hasattr(cfg, "audio_default_bitrate_per_channel_kbps")
        assert not hasattr(cfg, "audio_bitrate_step_per_channel_kbps")


def test_settings_panel_writes_ffmpeg_threads_to_ini(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ffmpeg]\nthreads = 12\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        spin = _field_widget(panel, "ffmpeg", "threads")
        spin.setValue(18)
        panel._on_save_clicked()

        assert "threads = 18" in ini_path.read_text(encoding="utf-8")
        assert cfg.ffmpeg_threads == 18


def test_settings_panel_writes_max_parallel_video_encodes_to_ini(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[encoding]\nmax_parallel_video_encodes = 1\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        spin = _field_widget(panel, "encoding", "max_parallel_video_encodes")
        spin.setValue(3)
        panel._on_save_clicked()

        assert "max_parallel_video_encodes = 3" in ini_path.read_text(encoding="utf-8")
        assert cfg.max_parallel_video_encodes == 3


def test_settings_panel_rerun_setup_restarts_app_on_confirmation(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel
    from PySide6.QtWidgets import QMessageBox

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nlanguage = eng\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

    panel = SettingsPanel(cfg)
    with patch.object(cfg, "rerun_setup") as mock_rerun, \
         patch.object(cfg, "restart_application", return_value=True) as mock_restart, \
         patch("ui.panels.settings_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
        panel._on_rerun_setup_clicked()

    mock_rerun.assert_called_once_with()
    mock_restart.assert_called_once_with()
    assert panel._status_label is not None
    assert panel._status_label.text() == translate_text(
        "Setup relancé avec succès. Un redémarrage de l'application est recommandé."
    )


def test_settings_panel_rerun_setup_shows_error_message(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nlanguage = eng\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

    panel = SettingsPanel(cfg)
    with patch.object(cfg, "rerun_setup", side_effect=RuntimeError("boom")), \
         patch("ui.panels.settings_panel.QMessageBox.warning") as mock_warning:
        panel._on_rerun_setup_clicked()

    mock_warning.assert_called_once()
    assert panel._status_label is not None
    assert "boom" in panel._status_label.text()
