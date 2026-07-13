@echo off
REM Wells launcher (Windows) - runs the harness from the cloned repo, no install needed.
REM
REM Usage:
REM   wells.bat "your goal"
REM   wells.bat --workspace C:\path\to\project "fix bug"
REM   wells.bat config
REM   wells.bat info
REM
REM This wrapper AVOIDS building the local package (no hatchling needed from
REM PyPI) by running the harness module directly with PYTHONPATH=src. It tells
REM uv to use the Windows system certificate store (UV_SYSTEM_CERTS, or
REM UV_NATIVE_TLS on older uv) - needed on corporate networks whose proxy
REM presents a self-signed cert.

setlocal
cd /d "%~dp0"

REM Use the OS certificate store so corporate TLS-intercepting proxies work.
REM Newer uv renamed UV_NATIVE_TLS to UV_SYSTEM_CERTS (the old name still
REM works but prints a deprecation warning) - set whichever this uv knows.
if defined UV_SYSTEM_CERTS goto :tlsdone
if defined UV_NATIVE_TLS goto :tlsdone
uv sync --help 2>nul | findstr /C:"UV_SYSTEM_CERTS" >nul
if %errorlevel%==0 (set UV_SYSTEM_CERTS=1) else (set UV_NATIVE_TLS=1)
:tlsdone

REM Sync dependencies WITHOUT building the local package.
uv sync --no-install-project --quiet >nul 2>&1
if %errorlevel%==0 goto :run

echo [wells] installing dependencies (first run may take a minute) ...
uv sync --no-install-project
if errorlevel 1 goto :syncfail

:run
REM Auto-deploy the wells-index .pyd from the repo into the venv.
REM After a git pull the repo copy is newer; this keeps the venv in sync
REM without any manual copy step.
set "PYD_SRC=%~dp0wells-index\python\wells_index\_core.cp312-win_amd64.pyd"
set "PYD_DST=%~dp0.venv\Lib\site-packages\wells_index\_core.cp312-win_amd64.pyd"
if exist "%PYD_SRC%" (
    xcopy /Y /Q "%PYD_SRC%" "%PYD_DST%" >nul 2>&1
)

set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
uv run --no-sync python -m wells.main %*
goto :eof

:syncfail
echo.
echo [wells] Dependency install failed. This is usually a network/TLS issue. 1>&2
echo [wells] If behind a corporate proxy, try setting these environment vars: 1>&2
echo [wells]   set SSL_CERT_FILE=C:\path\to\your-corp-ca-bundle.pem 1>&2
echo [wells]   set REQUESTS_CA_BUNDLE=C:\path\to\your-corp-ca-bundle.pem 1>&2
exit /b 1
