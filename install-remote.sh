#!/usr/bin/env bash
# Wells remote installer — the one-liner entry point.
#
#   curl -fsSL https://raw.githubusercontent.com/corbybender/Wells/master/install-remote.sh | bash
#
# What this does (and why it needs nothing pre-installed except curl):
#   1. Installs `uv` if missing (uv's own official installer; it also
#      manages Python versions, so no Python needed either).
#   2. Installs the `wells` command from PyPI (`uv tool install wells-agent`).
#   3. Launches first-run provider setup (`wells config`).
#
# Requires: curl (git is only needed if you install from the repo instead).
# Update an existing install by re-running this script.

set -e

say() { printf '\033[1;34m[wells]\033[0m %s\n' "$*"; }

# --- 1. uv (its installer is a no-op-safe official one-liner) -------------
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (manages Python for us — nothing else needed) ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || {
    echo "[wells] uv was installed but isn't on PATH this session." >&2
    echo "        Open a new terminal and re-run this installer, or:" >&2
    echo "        export PATH=\"\$HOME/.local/bin:\$PATH\"" >&2
    exit 1
  }
else
  say "uv already present."
fi

# --- 2. wells --------------------------------------------------------------
if ! command -v git >/dev/null 2>&1; then
  say "note: git not found (only needed if you install from the repo instead)."
fi
say "Installing the wells command (fetches a managed Python if needed) ..."
uv tool install --force wells-agent

# --- 3. first-run setup -----------------------------------------------------
say "Installed. Starting provider setup — pick a model and paste a key."
wells config || say "Run 'wells config' yourself when ready."
say "Done. cd into any project and run: wells"
