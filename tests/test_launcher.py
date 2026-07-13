from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import launcher


class TestLauncherWindowsControlledFolderAccess:
    def test_core_logging_import_stays_headless_without_i18n(self):
        previous_logging = sys.modules.pop("core.logging", None)
        try:
            with patch.dict(sys.modules, {"core.i18n": None}):
                module = importlib.import_module("core.logging")
                assert module.get_logger("Muxiveo.tmdb").name == "Muxiveo.tmdb"
        finally:
            sys.modules.pop("core.logging", None)
            if previous_logging is not None:
                sys.modules["core.logging"] = previous_logging

    def test_restart_current_app_uses_sys_executable_when_frozen(self):
        with patch.object(launcher.sys, "executable", r"C:\Apps\Muxiveo\Muxiveo.exe"), \
             patch.object(launcher.sys, "frozen", True, create=True), \
             patch("launcher.subprocess.Popen") as mock_popen:
            assert launcher._restart_current_app() is True

        mock_popen.assert_called_once_with(
            [r"C:\Apps\Muxiveo\Muxiveo.exe"]
        )

    def test_first_time_setup_shows_popup_after_cfa_update(self, tmp_path):
        fake_setup = SimpleNamespace(
            _default_prefix=lambda: tmp_path / "tools",
            check_windows_required_tools=MagicMock(
                return_value=SimpleNamespace(healthy=False, missing=("ffmpeg",))
            ),
            ensure_windows_required_tools=MagicMock(
                return_value=SimpleNamespace(healthy=True, missing=())
            ),
            offer_windows_controlled_folder_access_setup=MagicMock(
                return_value={"status": "updated", "added": [], "skipped": [], "message": ""}
            ),
            initialize_config_ini_language=MagicMock(),
            install_python_packages=MagicMock(),
        )

        with patch("platform.system", return_value="Windows"), \
             patch.object(launcher, "_is_allinc", return_value=False), \
             patch.object(launcher, "_windows_ensure_admin", return_value=False), \
             patch.dict(sys.modules, {"setup": fake_setup}), \
             patch.object(launcher, "_windows_open_setup_console", return_value=("token", False)) as mock_open_console, \
             patch.object(launcher, "_windows_close_setup_console") as mock_close_console, \
             patch.object(launcher, "_windows_show_restart_required_popup") as mock_popup:
            rc = launcher._run_first_time_setup(tmp_path)

        assert rc == launcher.SETUP_RC_HANDOFF
        mock_popup.assert_called_once_with()
        mock_open_console.assert_called_once_with()
        mock_close_console.assert_called_once_with(("token", False))

    def test_first_time_setup_shows_error_popup_on_windows_exception(self, tmp_path):
        fake_setup = SimpleNamespace(
            _default_prefix=lambda: tmp_path / "tools",
            initialize_config_ini_language=MagicMock(side_effect=RuntimeError("boom")),
        )

        with patch("platform.system", return_value="Windows"), \
             patch.object(launcher, "_is_allinc", return_value=True), \
             patch.dict(sys.modules, {"setup": fake_setup}), \
             patch.object(launcher, "_windows_open_setup_console", return_value=("token", False)) as mock_open_console, \
             patch.object(launcher, "_windows_close_setup_console") as mock_close_console, \
             patch.object(launcher, "_windows_show_setup_error_popup") as mock_popup:
            rc = launcher._run_first_time_setup(tmp_path)

        assert rc == launcher.SETUP_RC_ERROR
        mock_popup.assert_called_once_with("boom")
        mock_open_console.assert_called_once_with()
        mock_close_console.assert_called_once_with(("token", False))

    def test_main_does_not_launch_qt_when_setup_handoffs(self, tmp_path):
        config_path = tmp_path / "config.ini"
        fake_main = SimpleNamespace(main=MagicMock(return_value=42))

        with patch.object(launcher, "_get_config_path", return_value=config_path), \
             patch.object(launcher, "_run_first_time_setup", return_value=launcher.SETUP_RC_HANDOFF), \
             patch.dict(sys.modules, {"main": fake_main}):
            rc = launcher.main()

        assert rc == launcher.SETUP_RC_OK
        fake_main.main.assert_not_called()

    def test_needs_windows_post_install_setup_when_marker_missing(self, tmp_path):
        marker = tmp_path / "setup.version"
        with patch.object(launcher.sys, "platform", "win32"), \
             patch.object(launcher.sys, "frozen", True, create=True), \
             patch.object(launcher, "_windows_setup_version_marker_path", return_value=marker):
            assert launcher._needs_windows_post_install_setup() is True

    def test_needs_windows_post_install_setup_when_marker_matches_version(self, tmp_path):
        marker = tmp_path / "setup.version"
        marker.write_text(launcher.APP_VERSION, encoding="utf-8")
        with patch.object(launcher.sys, "platform", "win32"), \
             patch.object(launcher.sys, "frozen", True, create=True), \
             patch.object(launcher, "_windows_setup_version_marker_path", return_value=marker):
            assert launcher._needs_windows_post_install_setup() is False

    def test_main_runs_setup_after_windows_install_version_change(self, tmp_path):
        config_path = tmp_path / "config.ini"
        config_path.write_text("", encoding="utf-8")
        fake_main = SimpleNamespace(main=MagicMock(return_value=42))

        with patch.object(launcher, "_get_config_path", return_value=config_path), \
             patch.object(launcher, "_needs_windows_post_install_setup", return_value=True), \
             patch.object(launcher, "_run_first_time_setup", return_value=launcher.SETUP_RC_OK) as mock_setup, \
             patch.dict(sys.modules, {"main": fake_main}):
            rc = launcher.main()

        assert rc == 42
        mock_setup.assert_called_once_with(config_path.parent)
        fake_main.main.assert_called_once_with()

    def test_main_skips_setup_when_windows_dependencies_are_healthy(self, tmp_path):
        config_path = tmp_path / "config.ini"
        config_path.write_text("", encoding="utf-8")
        fake_main = SimpleNamespace(main=MagicMock(return_value=42))

        with patch.object(launcher, "_get_config_path", return_value=config_path), \
             patch.object(launcher, "_needs_windows_post_install_setup", return_value=False), \
             patch.object(launcher, "_windows_required_tools_missing", return_value=False), \
             patch.object(launcher, "_run_first_time_setup") as mock_setup, \
             patch.dict(sys.modules, {"main": fake_main}):
            assert launcher.main() == 42

        mock_setup.assert_not_called()
        fake_main.main.assert_called_once_with()

    def test_incomplete_windows_repair_keeps_marker_invalid(self, tmp_path):
        marker = tmp_path / "setup.version"
        marker.write_text(launcher.APP_VERSION, encoding="utf-8")
        missing = SimpleNamespace(healthy=False, missing=("mediainfo",))
        fake_setup = SimpleNamespace(
            _default_prefix=lambda: tmp_path / "tools",
            check_windows_required_tools=MagicMock(return_value=missing),
            ensure_windows_required_tools=MagicMock(return_value=missing),
            offer_windows_controlled_folder_access_setup=MagicMock(return_value={"status": "not_run"}),
            initialize_config_ini_language=MagicMock(),
            install_python_packages=MagicMock(),
        )

        with patch("platform.system", return_value="Windows"), \
             patch.object(launcher, "_is_allinc", return_value=False), \
             patch.object(launcher, "_windows_ensure_admin", return_value=False), \
             patch.object(launcher, "_windows_setup_version_marker_path", return_value=marker), \
             patch.object(launcher, "_windows_open_setup_console", return_value=None), \
             patch.object(launcher, "_windows_show_missing_tools_popup") as popup, \
             patch.dict(sys.modules, {"setup": fake_setup}):
            assert launcher._run_first_time_setup(tmp_path) == launcher.SETUP_RC_OK

        assert not marker.exists()
        fake_setup.ensure_windows_required_tools.assert_called_once_with(
            tmp_path / "tools", dry_run=False, force=False
        )
        popup.assert_called_once_with(("mediainfo",))

    def test_healthy_windows_setup_does_not_request_elevation(self, tmp_path):
        healthy = SimpleNamespace(healthy=True, missing=())
        fake_setup = SimpleNamespace(
            _default_prefix=lambda: tmp_path / "tools",
            check_windows_required_tools=MagicMock(return_value=healthy),
            ensure_windows_required_tools=MagicMock(return_value=healthy),
            offer_windows_controlled_folder_access_setup=MagicMock(),
            initialize_config_ini_language=MagicMock(),
            install_python_packages=MagicMock(),
        )

        with patch("platform.system", return_value="Windows"), \
             patch.object(launcher, "_is_allinc", return_value=False), \
             patch.object(launcher, "_windows_ensure_admin") as ensure_admin, \
             patch.object(launcher, "_windows_open_setup_console") as open_console, \
             patch.dict(sys.modules, {"setup": fake_setup}):
            assert launcher._run_first_time_setup(tmp_path) == launcher.SETUP_RC_OK

        ensure_admin.assert_not_called()
        open_console.assert_not_called()
        fake_setup.offer_windows_controlled_folder_access_setup.assert_not_called()

    def test_main_requires_explicit_cli_flag(self, tmp_path):
        fake_cli = SimpleNamespace(main=MagicMock(return_value=17))
        fake_main = SimpleNamespace(main=MagicMock(return_value=42))
        config_path = tmp_path / "config.ini"
        config_path.write_text("", encoding="utf-8")

        with patch.object(launcher.sys, "argv", ["muxiveo", "inspect", "file.mkv"]), \
             patch.dict(sys.modules, {"cli.main": fake_cli, "main": fake_main}), \
             patch.object(launcher, "_get_config_path", return_value=config_path), \
             patch.object(launcher, "_needs_windows_post_install_setup", return_value=False), \
             patch.object(launcher, "_run_first_time_setup") as mock_setup:
            rc = launcher.main()

        assert rc == 42
        fake_cli.main.assert_not_called()
        fake_main.main.assert_called_once_with()
        mock_setup.assert_not_called()

    def test_main_routes_explicit_cli_flag_without_setup_or_gui(self):
        fake_cli = SimpleNamespace(main=MagicMock(return_value=19))
        fake_main = SimpleNamespace(main=MagicMock(return_value=42))

        with patch.object(launcher.sys, "argv", ["Muxiveo", "--cli", "preview", "--config", "job.json"]), \
             patch.dict(sys.modules, {"cli.main": fake_cli, "main": fake_main}), \
             patch.object(launcher, "_run_first_time_setup") as mock_setup:
            rc = launcher.main()

        assert rc == 19
        fake_cli.main.assert_called_once_with(["preview", "--config", "job.json"])
        fake_main.main.assert_not_called()
        mock_setup.assert_not_called()
