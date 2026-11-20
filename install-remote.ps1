# Wells remote installer (Windows) — the one-liner entry point.
#
#   irm https://raw.githubusercontent.com/corbybender/Wells/master/install-remote.ps1 | iex
#
# What this does (nothing pre-installed except PowerShell):
#   1. Installs `uv` if missing (uv also manages Python — no Python needed).
#   2. Installs the `wells` command from PyPI (`uv tool install wells-agent`).
#   3. Launches first-run provider setup (`wells config`).
#
# Update an existing install by re-running this script.

$ErrorActionPreference = "Stop"

function Say([string]$msg) { Write-Host "[wells] $msg" -ForegroundColor Cyan }
Say "Wells installer starting ..."

# --- 1. uv -------------------------------------------------------------------
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Say "Installing uv (manages Python for us - nothing else needed) ..."
    irm https://astral.sh/uv/install.ps1 | iex
    # uv installs to %USERPROFILE%\.local\bin — put it on THIS session's PATH
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host "[wells] uv installed but not on PATH this session." -ForegroundColor Red
        Write-Host "        Open a NEW terminal and re-run this installer." -ForegroundColor Red
        exit 1
    }
} else {
    Say "uv already present."
}

# --- 2. wells ----------------------------------------------------------------
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Write-Host "[wells] note: git not found (only needed for repo installs)." -ForegroundColor Yellow }
Say "Installing the wells command (also fetches Python 3.12 if needed) ..."
uv tool install --force --python 3.12 "wells-agent[index]"

# --- 3. first-run setup --------------------------------------------------------
Say "Installed. Starting provider setup - pick a model and paste a key."
try { wells config } catch { Say "Run 'wells config' yourself when ready." }
Say "Done. cd into any project and run: wells"
