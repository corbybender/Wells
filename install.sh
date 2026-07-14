#!/usr/bin/env bash
# Wells one-time setup — puts the `wells` command on your PATH.
#
# Usage: ./install.sh
#
# This does NOT build or install the Python package (that needs hatchling
# from PyPI, which corporate proxies often block). It just makes the `wells`
# launcher script in this directory runnable from anywhere. `wells` itself
# still handles the venv/deps automatically on first real run.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
chmod +x "$SCRIPT_DIR/wells"

case ":$PATH:" in
  *":$SCRIPT_DIR:"*)
    echo "[wells] Already on PATH."
    ;;
  *)
    SHELL_RC="$HOME/.profile"
    case "$(basename "${SHELL:-}")" in
      zsh)  SHELL_RC="$HOME/.zshrc" ;;
      bash) SHELL_RC="$HOME/.bashrc" ;;
    esac

    if ! grep -qF "$SCRIPT_DIR" "$SHELL_RC" 2>/dev/null; then
      {
        echo ""
        echo "# Added by Wells installer"
        echo "export PATH=\"$SCRIPT_DIR:\$PATH\""
      } >> "$SHELL_RC"
      echo "[wells] Added $SCRIPT_DIR to PATH via $SHELL_RC"
    fi

    echo "[wells] Restart your terminal (or run: source $SHELL_RC) for it to take effect."
    ;;
esac

echo "[wells] Setup complete. Open a new terminal, then try: wells info"
