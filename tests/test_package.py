from __future__ import annotations

import json
import os
import plistlib
import subprocess
import struct
import zlib
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import package as package_mod
import package_appimage as package_appimage_mod


def _png_bytes(width: int, height: int) -> bytes:
    def _chunk(name: bytes, data: bytes) -> bytes:
        payload = name + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)

    raw = b""
    for _ in range(height):
        raw += b"\x00" + (b"\xff\x00\x00" * width)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def _ico_with_png_frames(frames: list[tuple[int, int, bytes]]) -> bytes:
    header = struct.pack("<HHH", 0, 1, len(frames))
    entries: list[bytes] = []
    payloads = b""
    offset = 6 + len(frames) * 16
    for width, height, png_data in frames:
        entries.append(
            struct.pack(
                "<BBBBHHII",
                0 if width >= 256 else width,
                0 if height >= 256 else height,
                0,
                0,
                1,
                32,
                len(png_data),
                offset,
            )
        )
        payloads += png_data
        offset += len(png_data)
    return header + b"".join(entries) + payloads


def test_pyinstaller_frontend_flag_uses_windowed_on_windows():
    assert package_mod._pyinstaller_frontend_flag("Windows") == "--windowed"


def test_pyinstaller_frontend_flag_keeps_console_on_linux():
    assert package_mod._pyinstaller_frontend_flag("Linux") == "--console"


def test_github_latest_asset_uses_ci_token_when_available(monkeypatch):
    response = MagicMock()
    response.read.return_value = json.dumps({
        "assets": [{"name": "dovi-x86_64", "browser_download_url": "https://example.test/dovi"}],
    }).encode()
    response.__enter__.return_value = response
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    with patch.object(package_appimage_mod.urllib.request, "urlopen", return_value=response) as urlopen:
        url = package_appimage_mod._gh_latest_asset("owner/repo", "x86_64")

    assert url == "https://example.test/dovi"
    request = urlopen.call_args.args[0]
    assert request.get_header("Authorization") == "Bearer test-token"


def test_desktop_entries_advertise_file_open_support():
    rendered = package_mod._DESKTOP_ENTRY.format(
        mime_types="video/x-matroska;audio/x-matroska;",
        website_url=package_mod._APPIMAGE_WEBSITE_URL,
    )
    assert "Exec=muxiveo %F" in rendered
    assert "MimeType=video/x-matroska;audio/x-matroska;" in rendered
    assert "X-AppImage-Website=https://muxiveo.fr/" in rendered
    assert "Terminal=false" in rendered
    rendered_appimage = package_appimage_mod._DESKTOP.format(
        mime_types="video/x-matroska;",
        website_url=package_appimage_mod._APPIMAGE_WEBSITE_URL,
    )
    assert "Exec=muxiveo %F" in rendered_appimage
    assert "MimeType=video/x-matroska;" in rendered_appimage
    assert "X-AppImage-Website=https://muxiveo.fr/" in rendered_appimage


def test_appstream_metainfo_advertises_homepage_and_desktop_launchable():
    rendered = package_mod._APPSTREAM_METAINFO.format(
        appstream_id=package_mod._APPSTREAM_ID,
        desktop_id="Muxiveo.desktop",
        website_url=package_mod._APPIMAGE_WEBSITE_URL,
    )
    assert "<url type=\"homepage\">https://muxiveo.fr/</url>" in rendered
    assert "<launchable type=\"desktop-id\">Muxiveo.desktop</launchable>" in rendered

    rendered_appimage = package_appimage_mod._APPSTREAM_METAINFO.format(
        appstream_id=package_appimage_mod._APPSTREAM_ID,
        desktop_id="Muxiveo.desktop",
        website_url=package_appimage_mod._APPIMAGE_WEBSITE_URL,
    )
    assert "<url type=\"homepage\">https://muxiveo.fr/</url>" in rendered_appimage
    assert "<launchable type=\"desktop-id\">Muxiveo.desktop</launchable>" in rendered_appimage


def test_windows_supported_types_block_registers_open_with_entries():
    with patch.object(package_mod, "ACCEPTED_EXTENSIONS", frozenset({".mkv", ".srt"})):
        block = package_mod._windows_supported_types_block()
    assert 'Applications\\\\Muxiveo.exe\\\\shell\\\\open\\\\command' in block
    assert '.mkv' in block
    assert '.srt' in block


def test_nsis_bundle_glob_uses_posix_separator_on_linux():
    with patch.object(package_mod, "OS", "Linux"):
        assert package_mod._nsis_bundle_glob(Path("/tmp/Muxiveo-win")) == "/tmp/Muxiveo-win/*"


def test_nsis_bundle_glob_uses_windows_separator_on_windows():
    with patch.object(package_mod, "OS", "Windows"):
        assert package_mod._nsis_bundle_glob(Path(r"C:\tmp\Muxiveo")) == r"C:\tmp\Muxiveo\*"


def test_build_pyinstaller_uses_windowed_on_native_windows(tmp_path):
    commands: list[list[str]] = []

    def fake_run(cmd, cwd=None):
        commands.append(cmd)
        exe_path = tmp_path / "dist" / "Muxiveo" / "Muxiveo.exe"
        exe_path.parent.mkdir(parents=True, exist_ok=True)
        exe_path.write_text("", encoding="utf-8")

    version_file = tmp_path / "version.txt"
    version_file.write_text("", encoding="utf-8")

    with patch.object(package_mod, "ROOT", tmp_path), \
         patch.object(package_mod, "OS", "Windows"), \
         patch.object(package_mod, "DATA_FILES", []), \
         patch.object(package_mod, "_ensure_windows_runtime_dlls_available"), \
         patch.object(package_mod, "_add_windows_ctypes_to_pyinstaller_native"), \
         patch.object(package_mod, "_add_windows_sqlite_to_pyinstaller_native"), \
         patch.object(package_mod, "_add_windows_ssl_to_pyinstaller_native"), \
         patch.object(package_mod, "_write_windows_version_file", return_value=version_file), \
         patch.object(package_mod, "_resolve_windows_icon_ico", return_value=None), \
         patch.object(package_mod, "_verify_windows_runtime_bundle"), \
         patch.object(package_mod, "_run", side_effect=fake_run):
        result = package_mod._build_pyinstaller(onefile=False)

    assert commands
    assert "--windowed" in commands[0]
    assert "--console" not in commands[0]
    assert result == tmp_path / "dist" / "Muxiveo" / "Muxiveo.exe"
    assert (tmp_path / "dist" / "Muxiveo" / "Muxiveo.exe").exists()


def test_build_pyinstaller_accepts_lowercase_windows_entrypoint(tmp_path):
    commands: list[list[str]] = []

    def fake_run(cmd, cwd=None):
        commands.append(cmd)
        exe_path = tmp_path / "dist" / "Muxiveo" / "muxiveo.exe"
        exe_path.parent.mkdir(parents=True, exist_ok=True)
        exe_path.write_text("", encoding="utf-8")

    version_file = tmp_path / "version.txt"
    version_file.write_text("", encoding="utf-8")

    with patch.object(package_mod, "ROOT", tmp_path), \
         patch.object(package_mod, "OS", "Windows"), \
         patch.object(package_mod, "DATA_FILES", []), \
         patch.object(package_mod, "_ensure_windows_runtime_dlls_available"), \
         patch.object(package_mod, "_add_windows_ctypes_to_pyinstaller_native"), \
         patch.object(package_mod, "_add_windows_sqlite_to_pyinstaller_native"), \
         patch.object(package_mod, "_add_windows_ssl_to_pyinstaller_native"), \
         patch.object(package_mod, "_write_windows_version_file", return_value=version_file), \
         patch.object(package_mod, "_resolve_windows_icon_ico", return_value=None), \
         patch.object(package_mod, "_verify_windows_runtime_bundle"), \
         patch.object(package_mod, "_run", side_effect=fake_run):
        result = package_mod._build_pyinstaller(onefile=False)

    assert commands
    assert result == tmp_path / "dist" / "Muxiveo" / "Muxiveo.exe"
    assert (tmp_path / "dist" / "Muxiveo" / "Muxiveo.exe").exists()
    assert not (tmp_path / "dist" / "Muxiveo" / "muxiveo.exe").exists()


def test_ensure_windows_bundle_entrypoint_does_not_delete_branded_exe_on_case_insensitive_fs(tmp_path):
    # Simulate Windows case-insensitive filesystem: Muxiveo.exe already exists,
    # and samefile(muxiveo.exe, Muxiveo.exe) returns True.  The function must
    # NOT unlink the branded exe.
    bundle_dir = tmp_path / "dist" / "Muxiveo"
    bundle_dir.mkdir(parents=True)
    branded = bundle_dir / "Muxiveo.exe"
    branded.write_text("exe", encoding="utf-8")

    with patch.object(package_mod, "_WINDOWS_EXE_NAME", "Muxiveo.exe"), \
         patch("pathlib.Path.samefile", return_value=True):
        result = package_mod._ensure_windows_bundle_entrypoint(bundle_dir)

    assert branded.exists(), "Muxiveo.exe was deleted — samefile guard is broken"
    assert result == bundle_dir / "Muxiveo.exe"


def test_build_pyinstaller_lowercases_linux_entrypoints(tmp_path):
    commands: list[list[str]] = []

    def fake_run(cmd, cwd=None):
        commands.append(cmd)
        exe_path = tmp_path / "dist" / "Muxiveo" / "Muxiveo"
        exe_path.parent.mkdir(parents=True, exist_ok=True)
        exe_path.write_text("", encoding="utf-8")

    with patch.object(package_mod, "ROOT", tmp_path), \
         patch.object(package_mod, "OS", "Linux"), \
         patch.object(package_mod, "DATA_FILES", []), \
         patch.object(package_mod, "_run", side_effect=fake_run):
        result = package_mod._build_pyinstaller(onefile=False)

    assert result == tmp_path / "dist" / "Muxiveo" / "muxiveo"
    assert (tmp_path / "dist" / "Muxiveo" / "muxiveo").exists()
    assert not (tmp_path / "dist" / "Muxiveo" / "Muxiveo").exists()
    assert not (tmp_path / "dist" / "Muxiveo" / ("muxiveo" + "-cli")).exists()
    assert commands[0][commands[0].index("--name") + 1] == "Muxiveo"


def test_rename_unix_executable_handles_existing_samefile_target(tmp_path):
    exe_path = tmp_path / "Muxiveo"
    target = tmp_path / "muxiveo"
    exe_path.write_text("bin", encoding="utf-8")
    os.link(exe_path, target)

    with patch.object(package_mod, "OS", "Linux"):
        result = package_mod._rename_unix_executable(exe_path)

    assert result == target
    assert target.read_text(encoding="utf-8") == "bin"
    assert not exe_path.exists()


def test_build_pyinstaller_accepts_lowercase_macos_entrypoint(tmp_path):
    commands: list[list[str]] = []

    def fake_run(cmd, cwd=None):
        commands.append(cmd)
        exe_path = tmp_path / "dist" / "Muxiveo.app" / "Contents" / "MacOS" / "muxiveo"
        exe_path.parent.mkdir(parents=True, exist_ok=True)
        exe_path.write_text("", encoding="utf-8")

    with patch.object(package_mod, "ROOT", tmp_path), \
         patch.object(package_mod, "OS", "Darwin"), \
         patch.object(package_mod, "DATA_FILES", []), \
         patch.object(package_mod, "_run", side_effect=fake_run):
        result = package_mod._build_pyinstaller(onefile=False)

    assert result == tmp_path / "dist" / "Muxiveo.app" / "Contents" / "MacOS" / "muxiveo"
    assert commands[0][commands[0].index("--name") + 1] == "Muxiveo"


def test_ensure_wine_deps_pins_pyside6_and_verifies_runtime():
    with patch.object(package_mod, "_WIN_PYSIDE6_VER", "6.10.2"), \
         patch.object(package_mod, "_wine_pip") as mock_wine_pip, \
         patch.object(package_mod, "_ensure_wine_qt_icu_runtime") as mock_icu, \
         patch.object(package_mod, "_verify_wine_pyside6_runtime") as mock_verify:
        package_mod._ensure_wine_deps()

    mock_wine_pip.assert_called_once_with(
        "pyinstaller",
        "PySide6==6.10.2",
        "pymediainfo>=6.1.0",
    )
    mock_icu.assert_called_once_with()
    mock_verify.assert_called_once_with()


def test_verify_wine_pyside6_runtime_raises_with_missing_dlls(tmp_path):
    wine_python = tmp_path / "drive_c" / "Python311" / "python.exe"
    pyside_dir = wine_python.parent / "Lib" / "site-packages" / "PySide6"
    pyside_dir.mkdir(parents=True, exist_ok=True)
    (pyside_dir / "Qt6Core.dll").write_text("", encoding="utf-8")

    stderr = "\n".join(
        [
            "0044:err:module:import_dll Library icuuc.dll (which is needed by L\"C:\\\\Python311\\\\Lib\\\\site-packages\\\\PySide6\\\\Qt6Core.dll\") not found",
            "0044:err:module:import_dll Library Qt6Core.dll (which is needed by L\"C:\\\\Python311\\\\Lib\\\\site-packages\\\\PySide6\\\\QtCore.pyd\") not found",
            "ImportError: DLL load failed while importing QtCore: Module introuvable.",
        ]
    )

    with patch.object(package_mod, "_WIN_PY_EXE", wine_python), \
         patch.object(package_mod, "_wine_env", return_value={}), \
         patch.object(
             package_mod.subprocess,
             "run",
             return_value=subprocess.CompletedProcess(args=["wine"], returncode=1, stdout="", stderr=stderr),
         ):
        try:
            package_mod._verify_wine_pyside6_runtime()
        except RuntimeError as exc:
            text = str(exc)
        else:
            raise AssertionError("RuntimeError not raised")

    assert "PySide6.QtCore" in text
    assert "icuuc.dll" in text
    assert "Absentes du package PySide6" in text


def test_sync_windows_icu_dlls_copies_versioned_files_and_aliases(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "icuuc72.dll").write_text("icuuc", encoding="utf-8")
    (source / "icuin72.dll").write_text("icuin", encoding="utf-8")
    (source / "icudt72.dll").write_text("icudt", encoding="utf-8")

    target = tmp_path / "dst"
    copied = package_mod._sync_windows_icu_dlls(target, sorted(source.glob("icu*.dll")))
    copied_names = {path.name for path in copied}

    assert "icuuc72.dll" in copied_names
    assert "icuin72.dll" in copied_names
    assert "icudt72.dll" in copied_names
    assert "icuuc.dll" in copied_names
    assert "icuin.dll" in copied_names
    assert "icudt.dll" in copied_names
    assert (target / "icuuc.dll").read_text(encoding="utf-8") == "icuuc"


def test_select_windows_icu_runtime_dlls_accepts_icu_dll_data_provider(tmp_path):
    root = tmp_path / "icu"
    root.mkdir()
    (root / "icuuc.dll").write_text("", encoding="utf-8")
    (root / "icuin.dll").write_text("", encoding="utf-8")
    (root / "icu.dll").write_text("", encoding="utf-8")

    selected = package_mod._select_windows_icu_runtime_dlls([root])
    names = {path.name.lower() for path in selected}

    assert "icuuc.dll" in names
    assert "icuin.dll" in names
    assert "icu.dll" in names


def test_verify_windows_runtime_bundle_accepts_icu_dll_data_runtime(tmp_path):
    bundle = tmp_path / "bundle"
    internal = bundle / "_internal"
    internal.mkdir(parents=True)

    for filename in (
        "_ctypes.pyd",
        "_ssl.pyd",
        "libffi-8.dll",
        "libssl-3.dll",
        "libcrypto-3.dll",
        "icuuc.dll",
        "icuin.dll",
        "icu.dll",
    ):
        (internal / filename).write_text("", encoding="utf-8")

    package_mod._verify_windows_runtime_bundle(bundle)


def test_add_windows_icu_to_pyinstaller_native_falls_back_to_system_runtime():
    cmd: list[str] = []
    system_dlls = [
        Path(r"C:\Windows\System32\icuuc.dll"),
        Path(r"C:\Windows\System32\icuin.dll"),
        Path(r"C:\Windows\System32\icu.dll"),
    ]

    with patch.object(package_mod, "_select_windows_icu_runtime_dlls", side_effect=[[], system_dlls]), \
         patch.object(package_mod, "_native_windows_system_icu_search_dirs", return_value=[Path(r"C:\Windows\System32")]), \
         patch.object(package_mod, "_warn") as mock_warn:
        package_mod._add_windows_icu_to_pyinstaller_native(cmd)

    expected: list[str] = []
    for dll in system_dlls:
        expected.extend(["--add-binary", f"{dll};."])
    assert cmd == expected
    mock_warn.assert_not_called()


def test_convert_ico_to_png_uses_largest_embedded_png_frame(tmp_path):
    ico_path = tmp_path / "icon.ico"
    ico_path.write_bytes(
        _ico_with_png_frames(
            [
                (16, 16, _png_bytes(16, 16)),
                (256, 256, _png_bytes(256, 256)),
            ]
        )
    )
    out_path = tmp_path / "icon.png"

    assert package_mod._convert_ico_to_png(ico_path, out_path) is True

    data = out_path.read_bytes()
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (256, 256)


def _prepare_linux_bundle(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "dist" / "Muxiveo"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "Muxiveo").write_text("", encoding="utf-8")


def test_build_appdir_prefers_icon_ico_when_available(tmp_path):
    _prepare_linux_bundle(tmp_path)
    ico_path = tmp_path / "icon.ico"
    ico_path.write_bytes(b"ico")
    png_path = tmp_path / "icon.png"
    png_path.write_bytes(b"png-fallback")

    def fake_convert(_src: Path, dest: Path) -> bool:
        dest.write_bytes(b"png-from-ico")
        return True

    with patch.object(package_mod, "ROOT", tmp_path), \
         patch.object(package_mod, "ICON_ICO", ico_path), \
         patch.object(package_mod, "ICON_PNG", png_path), \
         patch.object(package_mod, "_resolve_windows_icon_ico", return_value=ico_path), \
         patch.object(package_mod, "_convert_ico_to_png", side_effect=fake_convert):
        appdir = package_mod._build_appdir()

    assert (appdir / "Muxiveo.png").read_bytes() == b"png-from-ico"
    assert (appdir / "Muxiveo.ico").read_bytes() == b"ico"
    assert (appdir / "Muxiveo" / "muxiveo").exists()
    assert not (appdir / "Muxiveo" / ("muxiveo" + "-cli")).exists()
    assert (appdir / ".DirIcon").is_symlink()
    assert (appdir / ".DirIcon").readlink() == Path("Muxiveo.png")


def test_build_appdir_falls_back_to_icon_png_if_ico_conversion_fails(tmp_path):
    _prepare_linux_bundle(tmp_path)
    ico_path = tmp_path / "icon.ico"
    ico_path.write_bytes(b"ico")
    png_path = tmp_path / "icon.png"
    png_path.write_bytes(b"png-fallback")

    with patch.object(package_mod, "ROOT", tmp_path), \
         patch.object(package_mod, "ICON_ICO", ico_path), \
         patch.object(package_mod, "ICON_PNG", png_path), \
         patch.object(package_mod, "_convert_ico_to_png", return_value=False):
        appdir = package_mod._build_appdir()

    assert (appdir / "Muxiveo.png").read_bytes() == b"png-fallback"
    assert (appdir / "Muxiveo" / "muxiveo").exists()


def test_package_appimage_build_appdir_prefers_icon_ico_when_available(tmp_path):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "Muxiveo").write_text("", encoding="utf-8")
    ico_path = tmp_path / "icon.ico"
    ico_path.write_bytes(b"ico")

    def fake_convert(_src: Path, dest: Path) -> bool:
        dest.write_bytes(b"png-from-ico")
        return True

    with patch.object(package_appimage_mod, "ROOT", tmp_path), \
         patch.object(package_appimage_mod, "APPDIR", tmp_path / "Muxiveo.AppDir"), \
         patch.object(package_appimage_mod, "_convert_ico_to_png", side_effect=fake_convert):
        appdir = package_appimage_mod.build_appdir(bundle_dir)

    assert (appdir / "Muxiveo.ico").read_bytes() == b"ico"
    assert (appdir / "Muxiveo.png").read_bytes() == b"png-from-ico"
    assert (appdir / "usr" / "bin" / "muxiveo").exists()
    assert not (appdir / "usr" / "bin" / ("muxiveo" + "-cli")).exists()
    assert (appdir / ".DirIcon").is_symlink()
    assert (appdir / ".DirIcon").readlink() == Path("Muxiveo.png")


def test_package_versioned_output_path_uses_app_version_by_default():
    with patch.object(package_mod, "APP_VERSION", "9.9.9"):
        output = package_mod._versioned_output_path(
            Path("/tmp/Muxiveo-x86_64.AppImage"),
            None,
        )
    assert output.name == "Muxiveo-x86_64-9.9.9.AppImage"


def test_msix_manifest_contains_full_trust_metadata():
    with patch.object(package_mod, "_MSIX_IDENTITY", "Hydro74000.Muxiveo"), \
         patch.object(package_mod, "_MSIX_PUBLISHER", "CN=Hydro74000"), \
         patch.object(package_mod, "_MSIX_PUBLISHER_DISPLAY_NAME", "Hydro74000"), \
         patch.object(package_mod, "_MSIX_DESCRIPTION", "Muxiveo video workflow"), \
         patch.object(package_mod, "_msix_processor_architecture", return_value="x64"):
        manifest = package_mod._msix_manifest_content(
            "1.3.2",
            r"VFS\ProgramFilesX64\Muxiveo\Muxiveo.exe",
        )

    assert 'Name="Hydro74000.Muxiveo"' in manifest
    assert 'Publisher="CN=Hydro74000"' in manifest
    assert 'Version="1.3.2.0"' in manifest
    assert 'ProcessorArchitecture="x64"' in manifest
    assert 'EntryPoint="Windows.FullTrustApplication"' in manifest
    assert '<rescap:Capability Name="runFullTrust" />' in manifest
    assert r'Executable="VFS\ProgramFilesX64\Muxiveo\Muxiveo.exe"' in manifest
    assert 'xmlns:desktop="http://schemas.microsoft.com/appx/manifest/desktop/windows10"' in manifest
    assert '<desktop:Extension Category="windows.fullTrustProcess"' in manifest
    assert "<desktop:FullTrustProcess />" in manifest
    assert '<uap:Extension Category="windows.fileTypeAssociation">' in manifest
    assert '<uap:FileTypeAssociation Name="muxiveo">' in manifest
    assert '<uap:FileType>.mkv</uap:FileType>' in manifest
    assert '<uap:FileType>.srt</uap:FileType>' in manifest


def test_msix_manifest_forces_revision_zero():
    with patch.object(package_mod, "_MSIX_IDENTITY", "Hydro74000.Muxiveo"), \
         patch.object(package_mod, "_MSIX_PUBLISHER", "CN=Hydro74000"), \
         patch.object(package_mod, "_MSIX_PUBLISHER_DISPLAY_NAME", "Hydro74000"), \
         patch.object(package_mod, "_MSIX_DESCRIPTION", "Muxiveo video workflow"), \
         patch.object(package_mod, "_msix_processor_architecture", return_value="x64"):
        manifest = package_mod._msix_manifest_content(
            "1.4.0.1",
            r"VFS\ProgramFilesX64\Muxiveo\Muxiveo.exe",
        )

    assert 'Version="1.4.0.0"' in manifest


def test_load_msix_store_metadata_prefers_config_file(tmp_path):
    config_path = tmp_path / "msix_store.json"
    config_path.write_text(
        json.dumps({
            "identity": "Contoso.Muxiveo",
            "publisher": "CN=Contoso",
            "publisher_display_name": "Contoso",
            "description": "Store build",
            "display_name": "Muxiveo Store",
        }),
        encoding="utf-8",
    )

    with patch.object(package_mod, "_MSIX_IDENTITY", "Fallback.Identity"), \
         patch.object(package_mod, "_MSIX_PUBLISHER", "CN=Fallback"), \
         patch.object(package_mod, "_MSIX_PUBLISHER_DISPLAY_NAME", "Fallback"), \
         patch.object(package_mod, "_MSIX_DESCRIPTION", "Fallback description"):
        metadata = package_mod._load_msix_store_metadata(config_path)

    assert metadata == {
        "identity": "Contoso.Muxiveo",
        "publisher": "CN=Contoso",
        "publisher_display_name": "Contoso",
        "description": "Store build",
        "display_name": "Muxiveo Store",
    }


def test_stage_msix_layout_embeds_file_associations_and_bundle(tmp_path):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "Muxiveo.exe").write_text("", encoding="utf-8")

    metadata = {
        "identity": "Contoso.Muxiveo",
        "publisher": "CN=Contoso",
        "publisher_display_name": "Contoso",
        "description": "Store build",
        "display_name": "Muxiveo Store",
    }

    with patch.object(package_mod, "ROOT", tmp_path), \
         patch.object(package_mod, "APP_NAME", "Muxiveo"), \
         patch.object(package_mod, "_MSIX_PACKAGE_NAME", "AOTRMuxiveo"), \
         patch.object(package_mod, "_msix_processor_architecture", return_value="x64"), \
         patch.object(package_mod, "_load_msix_store_metadata", return_value=metadata):
        layout_dir = package_mod._stage_msix_layout(bundle_dir, "1.3.2", metadata=metadata)

    manifest = (layout_dir / "AppxManifest.xml").read_text(encoding="utf-8")
    assert '<uap:Extension Category="windows.fileTypeAssociation">' in manifest
    assert '<uap:FileTypeAssociation Name="muxiveo">' in manifest
    assert '<uap:FileType>.mkv</uap:FileType>' in manifest
    assert (layout_dir / "VFS" / "ProgramFilesX64" / "AOTRMuxiveo" / "Muxiveo.exe").exists()


def test_build_msixupload_wraps_msix_for_partner_center(tmp_path):
    msix_path = tmp_path / "Muxiveo-1.3.2.msix"
    msix_path.write_text("msix", encoding="utf-8")

    upload_path = package_mod._build_msixupload(msix_path, version_tag="1.3.2")

    assert upload_path.name == "Muxiveo-1.3.2.msixupload"
    with package_mod.zipfile.ZipFile(upload_path) as archive:
        assert archive.namelist() == ["Muxiveo-1.3.2.msix"]
        assert archive.read("Muxiveo-1.3.2.msix") == b"msix"


def test_build_msix_package_invokes_makeappx_and_signing(tmp_path):
    bundle_dir = tmp_path / "dist" / "Muxiveo"
    bundle_dir.mkdir(parents=True)
    layout_dir = tmp_path / "layout"
    layout_dir.mkdir()
    output_path = tmp_path / "AOTRMuxiveo-1.3.2.msix"
    commands: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        commands.append([str(part) for part in cmd])
        output_path.write_text("msix", encoding="utf-8")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with patch.object(package_mod, "OS", "Windows"), \
         patch.object(package_mod, "ROOT", tmp_path), \
         patch.object(package_mod, "_MSIX_PACKAGE_NAME", "AOTRMuxiveo"), \
         patch.object(package_mod, "_stage_msix_layout", return_value=layout_dir), \
         patch.object(package_mod, "_ensure_windows_sdk_tool", return_value="C:\\sdk\\makeappx.exe"), \
         patch.object(package_mod, "_sign_msix_package") as mock_sign, \
         patch.object(package_mod.subprocess, "run", side_effect=fake_run):
        result = package_mod._build_msix_package(bundle_dir, version_tag="1.3.2")

    assert result == output_path
    assert commands == [[
        "C:\\sdk\\makeappx.exe",
        "pack",
        "/v",
        "/d",
        str(layout_dir),
        "/p",
        str(output_path),
        "/o",
    ]]
    mock_sign.assert_called_once_with(output_path)


def test_build_macos_dmg_retries_hdiutil_create(tmp_path):
    app_path = tmp_path / "dist" / "Muxiveo.app"
    contents = app_path / "Contents"
    contents.mkdir(parents=True)
    (contents / "Info.plist").write_text("plist", encoding="utf-8")

    commands: list[list[str]] = []
    attempts = {"count": 0}

    def fake_run(cmd, **kwargs):
        commands.append([str(part) for part in cmd])
        if cmd[:2] == ["hdiutil", "create"]:
            attempts["count"] += 1
            if attempts["count"] == 1:
                return subprocess.CompletedProcess(args=cmd, returncode=1)
            output = Path(cmd[-1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("dmg", encoding="utf-8")
            return subprocess.CompletedProcess(args=cmd, returncode=0)
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    with patch.object(package_mod, "ROOT", tmp_path), \
         patch.object(package_mod.shutil, "which", return_value="/usr/bin/hdiutil"), \
         patch.object(package_mod, "_run", side_effect=fake_run), \
         patch.object(package_mod.time, "sleep") as mock_sleep:
        result = package_mod._build_macos_dmg(app_path, version_tag="1.2.3")

    create_commands = [cmd for cmd in commands if cmd[:2] == ["hdiutil", "create"]]
    assert len(create_commands) == 2
    assert result == tmp_path / "dist" / "Muxiveo-1.2.3.dmg"
    assert result.read_text(encoding="utf-8") == "dmg"
    assert not (tmp_path / "dist" / "dmg_staging").exists()
    mock_sleep.assert_called_once_with(2)


def test_patch_macos_info_plist_uses_lowercase_bundle_executable(tmp_path):
    app_path = tmp_path / "dist" / "Muxiveo.app"
    plist_path = app_path / "Contents" / "Info.plist"
    plist_path.parent.mkdir(parents=True)
    with plist_path.open("wb") as f:
        plistlib.dump({}, f)

    package_mod._patch_macos_info_plist(app_path, version_tag="1.2.3")

    with plist_path.open("rb") as f:
        plist = plistlib.load(f)
    assert plist["CFBundleName"] == "Muxiveo"
    assert plist["CFBundleExecutable"] == "muxiveo"
    assert plist["CFBundleShortVersionString"] == "1.2.3"


def test_package_appimage_versioned_output_path_uses_app_version_by_default():
    with patch.object(package_appimage_mod, "APP_VERSION", "8.8.8"):
        output = package_appimage_mod._versioned_output_path(
            Path("/tmp/Muxiveo-x86_64_allinc.AppImage"),
            None,
        )
    assert output.name == "Muxiveo-x86_64_allinc-8.8.8.AppImage"


def test_package_update_information_uses_github_release_pattern():
    with patch.object(package_mod, "_APPIMAGE_UPDATE_OWNER", "Hydro74000"), \
         patch.object(package_mod, "_APPIMAGE_UPDATE_REPO", "Muxiveo"), \
         patch.object(package_mod, "_APPIMAGE_UPDATE_RELEASE", "latest"):
        assert package_mod._appimage_update_information("x86_64") == (
            "gh-releases-zsync|Hydro74000|Muxiveo|latest|Muxiveo-x86_64-*.AppImage.zsync"
        )


def test_package_appimage_update_information_uses_github_release_pattern_for_allinc():
    with patch.object(package_appimage_mod, "_APPIMAGE_UPDATE_OWNER", "Hydro74000"), \
         patch.object(package_appimage_mod, "_APPIMAGE_UPDATE_REPO", "Muxiveo"), \
         patch.object(package_appimage_mod, "_APPIMAGE_UPDATE_RELEASE", "latest"):
        assert package_appimage_mod._appimage_update_information("x86_64", allinc=True) == (
            "gh-releases-zsync|Hydro74000|Muxiveo|latest|Muxiveo-x86_64_allinc-*.AppImage.zsync"
        )


def test_package_appimage_update_information_supports_latest_unstable_channel():
    with patch.object(package_appimage_mod, "_APPIMAGE_UPDATE_OWNER", "Hydro74000"), \
         patch.object(package_appimage_mod, "_APPIMAGE_UPDATE_REPO", "Muxiveo"), \
         patch.object(package_appimage_mod, "_APPIMAGE_UPDATE_RELEASE", "latest-unstable"):
        assert package_appimage_mod._appimage_update_information(
            "x86_64",
            allinc=True,
            version_tag="latest-unstable",
        ) == (
            "gh-releases-zsync|Hydro74000|Muxiveo|latest-unstable|"
            "Muxiveo-x86_64_allinc-latest-unstable.AppImage.zsync"
        )


def test_package_appimage_update_information_auto_reuses_latest_channel_from_version_tag():
    with patch.object(package_appimage_mod, "_APPIMAGE_UPDATE_OWNER", "Hydro74000"), \
         patch.object(package_appimage_mod, "_APPIMAGE_UPDATE_REPO", "Muxiveo"), \
         patch.object(package_appimage_mod, "_APPIMAGE_UPDATE_RELEASE", "latest"):
        assert package_appimage_mod._appimage_update_information(
            "x86_64",
            allinc=True,
            version_tag="latest-unstable",
        ) == (
            "gh-releases-zsync|Hydro74000|Muxiveo|latest-unstable|"
            "Muxiveo-x86_64_allinc-latest-unstable.AppImage.zsync"
        )


def test_package_build_appimage_sets_update_information_env(tmp_path):
    appdir = tmp_path / "Muxiveo.AppDir"
    appdir.mkdir(parents=True, exist_ok=True)
    appimagetool = tmp_path / "appimagetool"
    appimagetool.write_text("", encoding="utf-8")
    captured_env: dict[str, str] = {}

    def fake_run(cmd, env=None, **kwargs):
        if env:
            captured_env.update(env)
        output = Path(cmd[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    with patch.object(package_mod, "ROOT", tmp_path), \
         patch.object(package_mod.platform, "machine", return_value="x86_64"), \
         patch.object(package_mod, "_ensure_appimagetool", return_value=appimagetool), \
         patch.object(package_mod, "_run", side_effect=fake_run), \
         patch.object(package_mod, "_APPIMAGE_UPDATE_OWNER", "Hydro74000"), \
         patch.object(package_mod, "_APPIMAGE_UPDATE_REPO", "Muxiveo"), \
         patch.object(package_mod, "_APPIMAGE_UPDATE_RELEASE", "latest"):
        package_mod._build_appimage(appdir, version_tag="1.2.3")

    assert captured_env["UPDATE_INFORMATION"] == (
        "gh-releases-zsync|Hydro74000|Muxiveo|latest|Muxiveo-x86_64-*.AppImage.zsync"
    )


def test_package_appimage_build_appimage_sets_update_information_env(tmp_path):
    appdir = tmp_path / "Muxiveo.AppDir"
    appdir.mkdir(parents=True, exist_ok=True)
    appimagetool = tmp_path / "appimagetool"
    appimagetool.write_text("", encoding="utf-8")
    captured_env: dict[str, str] = {}

    def fake_run(cmd, **kwargs):
        env = kwargs.get("env")
        if env:
            captured_env.update(env)
        output = Path(cmd[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    with patch.object(package_appimage_mod, "ROOT", tmp_path), \
         patch.object(package_appimage_mod, "run", side_effect=fake_run), \
         patch.object(package_appimage_mod, "_APPIMAGE_UPDATE_OWNER", "Hydro74000"), \
         patch.object(package_appimage_mod, "_APPIMAGE_UPDATE_REPO", "Muxiveo"), \
         patch.object(package_appimage_mod, "_APPIMAGE_UPDATE_RELEASE", "latest"):
        package_appimage_mod.build_appimage(
            appimagetool=appimagetool,
            appdir=appdir,
            arch="x86_64",
            allinc=True,
            version_tag="1.2.3",
        )

    assert captured_env["UPDATE_INFORMATION"] == (
        "gh-releases-zsync|Hydro74000|Muxiveo|latest|Muxiveo-x86_64_allinc-*.AppImage.zsync"
    )


def test_package_appimage_build_appimage_sets_reuse_update_information_env(tmp_path):
    appdir = tmp_path / "Muxiveo.AppDir"
    appdir.mkdir(parents=True, exist_ok=True)
    appimagetool = tmp_path / "appimagetool"
    appimagetool.write_text("", encoding="utf-8")
    captured_env: dict[str, str] = {}

    def fake_run(cmd, **kwargs):
        env = kwargs.get("env")
        if env:
            captured_env.update(env)
        output = Path(cmd[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    with patch.object(package_appimage_mod, "ROOT", tmp_path), \
         patch.object(package_appimage_mod, "run", side_effect=fake_run), \
         patch.object(package_appimage_mod, "_APPIMAGE_UPDATE_OWNER", "Hydro74000"), \
         patch.object(package_appimage_mod, "_APPIMAGE_UPDATE_REPO", "Muxiveo"), \
         patch.object(package_appimage_mod, "_APPIMAGE_UPDATE_RELEASE", "latest"):
        package_appimage_mod.build_appimage(
            appimagetool=appimagetool,
            appdir=appdir,
            arch="x86_64",
            allinc=True,
            version_tag="latest-unstable",
        )

    assert captured_env["UPDATE_INFORMATION"] == (
        "gh-releases-zsync|Hydro74000|Muxiveo|latest-unstable|"
        "Muxiveo-x86_64_allinc-latest-unstable.AppImage.zsync"
    )
