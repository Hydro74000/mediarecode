#!/usr/bin/env python3
"""
Render the Homebrew formula used to publish Muxiveo for Linux and macOS.
"""

from __future__ import annotations

# Import applicatif placé après l'ajout explicite de la racine du dépôt.
# ruff: noqa: E402

import argparse
import base64
import struct
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.file_types import build_desktop_mime_type_string


_DESKTOP_MIME_TYPES = build_desktop_mime_type_string()
_SETUP_BREW_TEMPLATE = (ROOT / "setup_brew.py").read_text(encoding="utf-8")


def _extract_largest_png_from_ico(path: Path) -> bytes:
    data = path.read_bytes()
    if len(data) < 6:
        raise ValueError(f"Invalid ICO file: {path}")

    _reserved, icon_type, count = struct.unpack_from("<HHH", data, 0)
    if icon_type != 1 or count <= 0:
        raise ValueError(f"Unsupported ICO file: {path}")

    best_payload: bytes | None = None
    best_area = -1
    for index in range(count):
        offset = 6 + index * 16
        if offset + 16 > len(data):
            break
        width, height, _colors, _reserved, _planes, _bpp, size, image_offset = struct.unpack_from(
            "<BBBBHHII",
            data,
            offset,
        )
        width = 256 if width == 0 else width
        height = 256 if height == 0 else height
        if image_offset + size > len(data):
            continue
        payload = data[image_offset:image_offset + size]
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            continue
        area = width * height
        if area > best_area:
            best_area = area
            best_payload = payload

    if best_payload is None:
        raise ValueError(f"No embedded PNG frame found in {path}")
    return best_payload


_LINUX_ICON_PNG_BASE64 = base64.b64encode(
    _extract_largest_png_from_ico(ROOT / "icon.ico")
).decode("ascii")


def _render_setup_brew_script() -> str:
    script = _SETUP_BREW_TEMPLATE
    script = script.replace("__MUXIVEO_ICON_PNG_BASE64__", _LINUX_ICON_PNG_BASE64)
    script = script.replace("__MUXIVEO_DESKTOP_MIME_TYPES__", _DESKTOP_MIME_TYPES)
    return script


def render_formula(
    *,
    version: str,
    linux_url: str,
    linux_sha256: str,
    macos_url: str,
    macos_sha256: str,
    dovi_tool_macos_url: str,
    dovi_tool_macos_sha256: str,
    hdr10plus_tool_macos_url: str,
    hdr10plus_tool_macos_sha256: str,
    homepage: str = "https://github.com/Hydro74000/Muxiveo",
) -> str:
    setup_brew_script = textwrap.indent(_render_setup_brew_script().rstrip(), "      ")
    return f"""class Muxiveo < Formula
  desc "GUI video workflow tool for remuxing, encoding, Dolby Vision and HDR10+"
  homepage "{homepage}"
  version "{version}"
  license "MIT"

  on_linux do
    url "{linux_url}"
    sha256 "{linux_sha256}"
  end

  on_macos do
    url "{macos_url}"
    sha256 "{macos_sha256}"

    depends_on "ffmpeg"
    depends_on "mediainfo"

    resource "dovi_tool" do
      url "{dovi_tool_macos_url}"
      sha256 "{dovi_tool_macos_sha256}"
    end

    resource "hdr10plus_tool" do
      url "{hdr10plus_tool_macos_url}"
      sha256 "{hdr10plus_tool_macos_sha256}"
    end
  end

  def install_setup_brew_helper
    (libexec/"setup_brew.py").write <<~PY
{setup_brew_script}
    PY
    chmod 0755, libexec/"setup_brew.py"
  end

  def install_uninstall_shortcuts_script
    (libexec/"muxiveo-uninstall-shortcuts").write <<~EOS
      #!/bin/bash
      set -euo pipefail
      PYTHON_BIN="$(command -v python3 || command -v python || true)"
      if [ -z "${{PYTHON_BIN}}" ]; then
        echo "python3 is required to clean Muxiveo shortcuts" >&2
        exit 1
      fi
      exec "${{PYTHON_BIN}}" "#{{opt_libexec}}/setup_brew.py" cleanup
    EOS
    chmod 0755, libexec/"muxiveo-uninstall-shortcuts"
    bin.install_symlink libexec/"muxiveo-uninstall-shortcuts"
  end

  def run_setup_brew(*args)
    python = which("python3") || which("python")
    odie "python3 is required for Muxiveo Homebrew integration" if python.nil?

    system python, libexec/"setup_brew.py", *args.map(&:to_s)
  end

  def install
    if OS.mac?
      prefix.install "Muxiveo.app"
      (libexec/"tools").mkpath
      install_setup_brew_helper
      install_uninstall_shortcuts_script

      resource("dovi_tool").stage do
        libexec.install Dir["**/dovi_tool"].first => "tools/dovi_tool"
      end

      resource("hdr10plus_tool").stage do
        libexec.install Dir["**/hdr10plus_tool"].first => "tools/hdr10plus_tool"
      end

      chmod 0755, libexec/"tools/dovi_tool"
      chmod 0755, libexec/"tools/hdr10plus_tool"

      (libexec/"muxiveo").write <<~EOS
        #!/bin/bash
        set -euo pipefail
        CONFIG_DIR="${{XDG_CONFIG_HOME:-$HOME/.config}}/muxiveo"
        CONFIG_FILE="${{CONFIG_DIR}}/config.ini"
        mkdir -p "${{CONFIG_DIR}}"
        if [ ! -f "${{CONFIG_FILE}}" ]; then
          cat > "${{CONFIG_FILE}}" <<'CFG'
# Muxiveo - configuration locale
# Fichier cree par le wrapper Homebrew pour eviter le setup interactif.
CFG
        fi
        export PATH="#{{opt_libexec}}/tools:#{{HOMEBREW_PREFIX}}/bin:$PATH"
        exec "#{{opt_prefix}}/Muxiveo.app/Contents/MacOS/muxiveo" "$@"
      EOS
      chmod 0755, libexec/"muxiveo"
      bin.install_symlink libexec/"muxiveo"
    else
      libexec.install Dir["*.AppImage"].first => "Muxiveo.AppImage"
      chmod 0755, libexec/"Muxiveo.AppImage"
      install_setup_brew_helper
      install_uninstall_shortcuts_script

      (libexec/"muxiveo").write <<~EOS
        #!/bin/bash
        set -euo pipefail
        CONFIG_DIR="${{XDG_CONFIG_HOME:-$HOME/.config}}/muxiveo"
        CONFIG_FILE="${{CONFIG_DIR}}/config.ini"
        mkdir -p "${{CONFIG_DIR}}"
        if [ ! -f "${{CONFIG_FILE}}" ]; then
          cat > "${{CONFIG_FILE}}" <<'CFG'
# Muxiveo - configuration locale
# Fichier cree par le wrapper Homebrew.
CFG
        fi
        if [ ! -e /dev/fuse ]; then
          export APPIMAGE_EXTRACT_AND_RUN=1
        fi
        PYTHON_BIN="$(command -v python3 || command -v python || true)"
        if [ -n "${{PYTHON_BIN}}" ]; then
          "${{PYTHON_BIN}}" "#{{opt_libexec}}/setup_brew.py" post-install --platform linux --opt-bin "#{{opt_bin}}" --opt-share "#{{opt_share}}" --opt-prefix "#{{opt_prefix}}" >/dev/null 2>&1 || true
        fi
        exec "#{{opt_libexec}}/Muxiveo.AppImage" "$@"
      EOS
      chmod 0755, libexec/"muxiveo"
      bin.install_symlink libexec/"muxiveo"
    end
  end

  def post_install
    if OS.mac?
      run_setup_brew("post-install", "--platform", "macos", "--opt-bin", opt_bin, "--opt-share", opt_share, "--opt-prefix", opt_prefix)
    else
      run_setup_brew("post-install", "--platform", "linux", "--opt-bin", opt_bin, "--opt-share", opt_share, "--opt-prefix", opt_prefix)
    end
  end

  def caveats
    <<~EOS
      User session shortcuts are installed outside the Homebrew prefix.
      If the desktop shortcut does not appear immediately on Linux, run:
        brew postinstall Muxiveo
      Diagnostic log:
        ~/.local/state/Muxiveo/setup_brew.log
      Before `brew uninstall Muxiveo`, run:
        muxiveo-uninstall-shortcuts
    EOS
  end

  test do
    if OS.mac?
      assert_predicate prefix/"Muxiveo.app", :exist?
      assert_predicate bin/"muxiveo", :exist?
    else
      assert_predicate libexec/"Muxiveo.AppImage", :exist?
      assert_predicate bin/"muxiveo", :exist?
    end
  end
end
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render the Muxiveo Homebrew formula.")
    parser.add_argument("--version", required=True)
    parser.add_argument("--linux-url", required=True)
    parser.add_argument("--linux-sha256", required=True)
    parser.add_argument("--macos-url", required=True)
    parser.add_argument("--macos-sha256", required=True)
    parser.add_argument("--dovi-tool-macos-url", required=True)
    parser.add_argument("--dovi-tool-macos-sha256", required=True)
    parser.add_argument("--hdr10plus-tool-macos-url", required=True)
    parser.add_argument("--hdr10plus-tool-macos-sha256", required=True)
    parser.add_argument("--homepage", default="https://github.com/Hydro74000/Muxiveo")
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_formula(
            version=args.version,
            linux_url=args.linux_url,
            linux_sha256=args.linux_sha256,
            macos_url=args.macos_url,
            macos_sha256=args.macos_sha256,
            dovi_tool_macos_url=args.dovi_tool_macos_url,
            dovi_tool_macos_sha256=args.dovi_tool_macos_sha256,
            hdr10plus_tool_macos_url=args.hdr10plus_tool_macos_url,
            hdr10plus_tool_macos_sha256=args.hdr10plus_tool_macos_sha256,
            homepage=args.homepage,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
