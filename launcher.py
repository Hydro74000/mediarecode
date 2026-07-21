#!/usr/bin/env python3
"""
launcher.py â€” Point d'entrée packagé de Muxiveo.

Vérifie la présence de config.ini dans le dossier de configuration utilisateur :
  Linux / macOS  â†’ $XDG_CONFIG_HOME/muxiveo/config.ini  (défaut : ~/.config/â€¦)
  Windows frozen â†’ %APPDATA%\\muxiveo\\config.ini
  Windows dev    â†’ racine du projet

Si absent â†’ lance le setup systÃ¨me, puis démarre l'application Qt.
"""

from __future__ import annotations

import atexit
import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from core.version import APP_CONFIG_DIR_NAME, APP_VERSION
except Exception:
    APP_CONFIG_DIR_NAME = "muxiveo"
    APP_VERSION = "0.0.0"


_DEVNULL_STREAMS: list = []


def _ensure_text_stream(name: str, mode: str) -> None:
    """Provide stdout/stderr when frozen without a console window."""
    if getattr(sys, name, None) is None:
        fh = open(os.devnull, mode, encoding="utf-8")
        _DEVNULL_STREAMS.append(fh)
        setattr(sys, name, fh)


def _close_devnull_streams() -> None:
    for fh in _DEVNULL_STREAMS:
        try:
            fh.close()
        except Exception:
            pass


_ensure_text_stream("stdout", "w")
_ensure_text_stream("stderr", "w")
atexit.register(_close_devnull_streams)


def _ensure_ssl_ca_bundle() -> None:
    """
    En mode frozen (PyInstaller/AppImage), l'OpenSSL embarqué référence
    des chemins CA de la machine de build qui n'existent pas sur la machine
    cible → toutes les requêtes HTTPS (TMDB, GitHub…) échouent avec
    CERTIFICATE_VERIFY_FAILED. On force SSL_CERT_FILE sur le bundle certifi
    embarqué si aucun CA valide n'est visible.
    """
    if not getattr(sys, "frozen", False):
        return
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE"):
        return
    try:
        import certifi  # embarqué par PyInstaller
    except Exception:
        return
    try:
        ca_path = certifi.where()
    except Exception:
        return
    if ca_path and os.path.exists(ca_path):
        os.environ["SSL_CERT_FILE"] = ca_path
        os.environ.setdefault("REQUESTS_CA_BUNDLE", ca_path)


_ensure_ssl_ca_bundle()

SETUP_RC_OK = 0
SETUP_RC_ERROR = 1
SETUP_RC_HANDOFF = 2


# ---------------------------------------------------------------------------
# Résolution du dossier de référence (lÃ  oÃ¹ config.ini doit vivre)
# ---------------------------------------------------------------------------

def _is_allinc() -> bool:
    """
    Retourne True si on tourne dans un AppImage all-inclusive.
    Détecté via le fichier marqueur _ALLINC placé dans le bundle par package_appimage.py.
    """
    if not getattr(sys, "frozen", False):
        return False
    # Le marqueur est Ã  cÃ´té de l'exécutable (dist/Muxiveo/_ALLINC)
    return (Path(sys.executable).parent / "_ALLINC").exists()


def _windows_config_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / APP_CONFIG_DIR_NAME
    return Path.home() / "AppData" / "Roaming" / APP_CONFIG_DIR_NAME


def _get_config_path() -> Path:
    """
    Retourne le chemin de config.ini selon la plateforme :
    - Linux / macOS  â†’ ~/.config/muxiveo/config.ini  (XDG)
    - Windows frozen â†’ %APPDATA%\\muxiveo\\config.ini
    - Windows dev    â†’ racine du projet
    """
    if sys.platform != "win32":
        xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return xdg / APP_CONFIG_DIR_NAME / "config.ini"
    if getattr(sys, "frozen", False):
        return _windows_config_dir() / "config.ini"
    return Path(__file__).parent / "config.ini"


def _windows_setup_version_marker_path() -> Path:
    return _windows_config_dir() / "setup.version"


def _needs_windows_post_install_setup() -> bool:
    """
    Sous Windows packagé, relance le setup au premier lancement après
    installation / mise à jour pour réinstaller et re-détecter les dépendances.
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False

    marker = _windows_setup_version_marker_path()
    try:
        recorded = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return True
    return recorded != APP_VERSION


def _windows_required_tools_missing() -> bool:
    """Return whether a frozen Windows build needs dependency repair."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    try:
        import setup as setup_mod  # noqa: PLC0415
        report = setup_mod.check_windows_required_tools(setup_mod._default_prefix())
        return not report.healthy
    except Exception:
        # A failed check must never validate the installation state.
        return True


def _windows_is_admin() -> bool:
    """Retourne True si le processus courant tourne en mode élevé (admin)."""
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _windows_ensure_admin() -> bool:
    """
    Sur Windows, si le processus n'est pas élevé, le relance en mode admin
    via ShellExecute 'runas' et retourne True (le processus appelant doit
    alors quitter immédiatement).

    Retourne False si déjÃ  admin ou hors Windows (rien Ã  faire).
    """
    if sys.platform != "win32" or _windows_is_admin():
        return False

    try:
        if getattr(sys, "frozen", False):
            exe = sys.executable
            params = None
        else:
            exe = sys.executable
            params = f'"{Path(__file__).resolve()}"'

        ret = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            exe,
            params,
            None,
            1,
        )
        return int(ret) > 32
    except Exception:
        return False


def _restart_current_app() -> bool:
    """
    Restart the current launcher as a fresh process.

    This is required after updating Windows Controlled Folder Access allowlists:
    the newly allowed executable only gains write access on its next start.
    """
    try:
        if getattr(sys, "frozen", False):
            cmd = [sys.executable]
        else:
            cmd = [sys.executable, str(Path(__file__).resolve())]
        subprocess.Popen(cmd)
        return True
    except Exception:
        return False


def _windows_show_restart_required_popup() -> None:
    """Affiche une popup Windows indiquant qu'un redémarrage manuel est requis."""
    if sys.platform != "win32":
        return
    text = (
        "Windows Security a été mis Ã  jour.\n\n"
        "Veuillez fermer cette fenÃªtre puis relancer Muxiveo manuellement."
    )
    title = "Muxiveo - Redémarrage requis"
    mb_ok = 0x00000000
    mb_icon_info = 0x00000040
    mb_topmost = 0x00040000
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, mb_ok | mb_icon_info | mb_topmost)
    except Exception:
        pass


def _windows_show_setup_error_popup(details: str) -> None:
    """Affiche une popup Windows pour une erreur bloquante pendant le setup."""
    if sys.platform != "win32":
        return
    text = (
        "Muxiveo n'a pas pu terminer son initialisation.\n\n"
        f"{details}"
    )
    title = "Muxiveo - Erreur d'initialisation"
    mb_ok = 0x00000000
    mb_icon_error = 0x00000010
    mb_topmost = 0x00040000
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, mb_ok | mb_icon_error | mb_topmost)
    except Exception:
        pass


def _windows_show_missing_tools_popup(missing: tuple[str, ...]) -> None:
    """Warn without blocking startup when a dependency repair did not succeed."""
    if sys.platform != "win32" or not missing:
        return
    text = (
        "Muxiveo démarre, mais certaines fonctions restent indisponibles.\n\n"
        "Outils manquants : " + ", ".join(missing) + "\n\n"
        "Une nouvelle tentative d'installation sera effectuée au prochain lancement."
    )
    try:
        ctypes.windll.user32.MessageBoxW(
            None, text, "Muxiveo - Dépendances manquantes", 0x00000030 | 0x00040000
        )
    except Exception:
        pass


def _macos_show_homebrew_required_popup() -> None:
    """Explain the only missing first-install prerequisite on a macOS DMG."""
    if sys.platform != "darwin":
        return
    script = (
        'display alert "Muxiveo — Homebrew required" '
        'message "Homebrew is required on first launch to install FFmpeg and MediaInfo. '
        'Install it from https://brew.sh, then restart Muxiveo.\\n\\n'
        'Homebrew est requis au premier lancement pour installer FFmpeg et MediaInfo. '
        'Installez-le depuis https://brew.sh puis relancez Muxiveo." as critical'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        pass


def _windows_open_setup_console() -> tuple[tuple[object, object, object], bool] | None:
    """Open a visible console dedicated to first-launch setup logs."""
    if sys.platform != "win32":
        return None

    old_streams = (sys.stdin, sys.stdout, sys.stderr)
    allocated = False

    try:
        has_console = bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
        has_console = False

    try:
        if not has_console:
            allocated = bool(ctypes.windll.kernel32.AllocConsole())
            if not allocated:
                return None

        sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        return (old_streams, allocated)
    except Exception:
        sys.stdin, sys.stdout, sys.stderr = old_streams
        if allocated:
            try:
                ctypes.windll.kernel32.FreeConsole()
            except Exception:
                pass
        return None


def _windows_close_setup_console(token: tuple[tuple[object, object, object], bool] | None) -> None:
    """Restore original stdio streams and close setup console when needed."""
    if sys.platform != "win32" or token is None:
        return

    old_streams, allocated = token
    current_streams = (sys.stdin, sys.stdout, sys.stderr)
    for stream in current_streams:
        if stream in old_streams:
            continue
        try:
            stream.flush()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    sys.stdin, sys.stdout, sys.stderr = old_streams

    if allocated:
        try:
            ctypes.windll.kernel32.FreeConsole()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Setup premier lancement
# ---------------------------------------------------------------------------

def _run_first_time_setup(install_dir: Path) -> int:
    """
    Lance le setup systÃ¨me (outils externes + config.ini initial).
    Retourne SETUP_RC_OK, SETUP_RC_ERROR, ou SETUP_RC_HANDOFF.
    """
    import platform

    allinc = _is_allinc()

    print("\n" + "=" * 60)
    if allinc:
        print("  Muxiveo â€” Initialisation (all-inclusive)")
    else:
        print("  Muxiveo â€” PremiÃ¨re installation")
    print("=" * 60)
    print(f"\n  config.ini introuvable dans : {install_dir}")
    if allinc:
        print("  Les outils sont déjÃ  embarqués â€” initialisation de la configuration...\n")
    else:
        print("  Lancement du setup...\n")

    # Dans un bundle PyInstaller, les sources sont accessibles via sys._MEIPASS
    if getattr(sys, "frozen", False):
        bundle_root = Path(getattr(sys, "_MEIPASS", ""))
        if bundle_root and str(bundle_root) not in sys.path:
            sys.path.insert(0, str(bundle_root))

    try:
        import setup as _setup  # bundled alongside launcher or in project root
    except ImportError as exc:
        print(f"\n  ERREUR : impossible d'importer setup.py â€” {exc}", file=sys.stderr)
        _windows_show_setup_error_popup(f"Impossible d'importer setup.py.\n\n{exc}")
        return SETUP_RC_ERROR

    _os = platform.system()
    prefix = _setup._default_prefix()
    dry_run = False
    force = False
    cfa_result: dict[str, object] = {"status": "not_run"}
    windows_report = None
    requires_windows_repair = False

    try:
        install_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    if _os == "Windows" and not allinc:
        windows_report = _setup.check_windows_required_tools(prefix)
        requires_windows_repair = not windows_report.healthy
        # A healthy install may only need its config marker refreshed: do not
        # create an unnecessary UAC prompt in that case.
        if requires_windows_repair and _windows_ensure_admin():
            print(
                "\n  Ã‰lévation des privilÃ¨ges demandée."
                "\n  Muxiveo va redémarrer avec les droits administrateur...\n"
            )
            return SETUP_RC_HANDOFF

    setup_console_token = (
        _windows_open_setup_console()
        if _os == "Windows" and (allinc or requires_windows_repair)
        else None
    )

    try:
        if allinc:
            # Mode all-inclusive : outils déjÃ  embarqués dans l'AppImage,
            # on initialise uniquement la langue / config.ini.
            _setup.initialize_config_ini_language(
                dry_run, force=force, ini_path=install_dir / "config.ini"
            )
        else:
            if _os == "Linux":
                distro = _setup.detect_linux_distro()
                print(f"  Distribution détectée : {distro}")
                if distro == "debian":
                    _setup.install_apt(dry_run, force=force)
                elif distro == "fedora":
                    _setup.install_dnf(dry_run, force=force)
                else:
                    print(
                        "  Distribution non reconnue â€” installez manuellement :\n"
                        "    ffmpeg  mediainfo",
                        file=sys.stderr,
                    )
                _setup.install_github_tools(prefix, dry_run, force=force)
                _setup.check_tools_presence()

            elif _os == "Darwin":
                if not shutil.which("brew"):
                    _macos_show_homebrew_required_popup()
                    raise RuntimeError(
                        "Homebrew is required on macOS to install FFmpeg and MediaInfo. "
                        "Install it from https://brew.sh, then restart Muxiveo."
                    )
                _setup.install_brew(dry_run, force=force)
                _setup.install_github_tools(prefix, dry_run, force=force)
                _setup.check_tools_presence()

            elif _os == "Windows":
                windows_report = _setup.ensure_windows_required_tools(
                    prefix, dry_run=dry_run, force=force
                )
                if requires_windows_repair:
                    cfa_result = _setup.offer_windows_controlled_folder_access_setup(
                        prefix, dry_run, force=force
                    )

            else:
                print(f"  Plateforme inconnue '{_os}' â€” setup systÃ¨me ignoré.", file=sys.stderr)

            # Initialise la langue dans QSettings (et config.ini si absents)
            _setup.initialize_config_ini_language(
                dry_run, force=force, ini_path=install_dir / "config.ini"
            )

            # Packages Python : inutiles dans un bundle (déjÃ  embarqués)
            if not getattr(sys, "frozen", False):
                _setup.install_python_packages(dry_run, force=force)

    except Exception as exc:
        print(f"\n  ERREUR pendant le setup : {exc}", file=sys.stderr)
        _windows_show_setup_error_popup(str(exc))
        try:
            answer = input("\n  Continuer quand mÃªme ? [o/N] ").strip().lower()
        except (EOFError, OSError, RuntimeError):
            answer = ""
        if answer not in ("o", "oui", "y", "yes"):
            _windows_close_setup_console(setup_console_token)
            return SETUP_RC_ERROR

    # Crée un config.ini marqueur dans install_dir pour signaler que le setup
    # a été effectué (évite de relancer le setup Ã  chaque démarrage).
    marker = install_dir / "config.ini"
    if not marker.exists():
        try:
            install_dir.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                "# Muxiveo â€” configuration locale\n"
                "# Décommentez et modifiez les clés pour surcharger les valeurs par défaut.\n"
                "# Voir la section Configuration dans CLAUDE.md pour la liste complÃ¨te.\n",
                encoding="utf-8",
            )
        except OSError:
            # Dossier en lecture seule (AppImage dans /opt, /usr â€¦) â€” non bloquant
            pass

    if _os == "Windows":
        setup_version_marker = _windows_setup_version_marker_path()
        if windows_report is not None and windows_report.healthy:
            try:
                setup_version_marker.parent.mkdir(parents=True, exist_ok=True)
                setup_version_marker.write_text(APP_VERSION, encoding="utf-8")
            except OSError:
                pass
        else:
            # Do not let an old success marker hide an incomplete repair.
            try:
                setup_version_marker.unlink()
            except OSError:
                pass

    if _os == "Windows" and str(cfa_result.get("status") or "") == "updated":
        print(
            "\n  Windows Security a été mise Ã  jour."
            "\n  Veuillez redémarrer Muxiveo manuellement pour appliquer l'autorisation.\n"
        )
        _windows_show_restart_required_popup()
        _windows_close_setup_console(setup_console_token)
        return SETUP_RC_HANDOFF

    if _os == "Windows" and windows_report is not None and not windows_report.healthy:
        print("\n  Dépendances toujours manquantes : " + ", ".join(windows_report.missing))
        _windows_show_missing_tools_popup(windows_report.missing)

    print("\n  Setup terminé. Démarrage de l'application...\n")
    _windows_close_setup_console(setup_console_token)
    return SETUP_RC_OK


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def _run_tmdb_smoke_test() -> int:
    """
    Smoke-test TMDB dans le binaire packagé : effectue une vraie requête réseau
    et retourne 0 si ≥ 1 résultat reçu, 1 sinon. Utilisé par la CI pour valider
    la chaîne SSL (certifi CA bundle + fallback non-vérifié) en mode frozen
    sur Linux / Windows / macOS.
    """
    import traceback

    try:
        sys.stdout.write(f"[tmdb-smoke] frozen={bool(getattr(sys, 'frozen', False))}\n")
        sys.stdout.write(f"[tmdb-smoke] SSL_CERT_FILE={os.environ.get('SSL_CERT_FILE', '')!r}\n")
        sys.stdout.flush()
        from core.media_info_fetcher import TmdbFetcher, default_tmdb_bearer_token
        token = default_tmdb_bearer_token()
        if not token:
            sys.stderr.write("[tmdb-smoke] aucun token Bearer disponible\n")
            return 1
        fetcher = TmdbFetcher(bearer_token=token)
        results = fetcher.search("Inception", kind="movie", year="2010")
        sys.stdout.write(f"[tmdb-smoke] {len(results)} résultat(s)\n")
        for r in results[:3]:
            sys.stdout.write(f"  - tmdb_id={r.tmdb_id} title={r.title!r}\n")
        sys.stdout.flush()
        return 0 if results else 1
    except Exception as exc:
        sys.stderr.write(f"[tmdb-smoke] ÉCHEC: {exc!r}\n")
        traceback.print_exc()
        return 1


def _is_cli_invocation() -> bool:
    return "--cli" in sys.argv[1:]


def _run_cli_entrypoint() -> int:
    argv = list(sys.argv[1:])
    try:
        argv.remove("--cli")
    except ValueError:
        pass
    from cli.main import main as _cli_main  # noqa: PLC0415
    return _cli_main(argv)


def main() -> int:
    if "--tmdb-smoke-test" in sys.argv:
        return _run_tmdb_smoke_test()

    if _is_cli_invocation():
        return _run_cli_entrypoint()

    config_path = _get_config_path()

    if (
        not config_path.exists()
        or _needs_windows_post_install_setup()
        or _windows_required_tools_missing()
    ):
        rc = _run_first_time_setup(config_path.parent)
        if rc == SETUP_RC_HANDOFF:
            return SETUP_RC_OK
        if rc != SETUP_RC_OK:
            try:
                input("Appuyez sur Entrée pour quitter...")
            except (EOFError, OSError, RuntimeError):
                pass
            return rc

    # Lance l'application Qt
    from main import main as _app_main  # noqa: PLC0415
    return _app_main()


if __name__ == "__main__":
    sys.exit(main())
