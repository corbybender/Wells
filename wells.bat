@echo off
REM Wells launcher (Windows) - runs the harness from the cloned repo, no install needed.
REM
REM Usage:
REM   wells.bat "your goal"
REM   wells.bat --workspace C:\path\to\project "fix bug"
REM   wells.bat config
REM   wells.bat info
REM
REM Thin wrapper around `uv run coding-harness`. The venv is created
REM automatically on first run and cached thereafter.

setlocal
REM Resolve to the directory this script lives in (the repo root).
cd /d "%~dp0"

REM Ensure dependencies are installed (fast no-op after first run).
uv sync --quiet >nul 2>&1
if errorlevel 1 uv sync

REM Forward all arguments to the harness.
uv run coding-harness %*
