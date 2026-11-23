"""Tests for scripts/wells_resumable_launch.ps1 (Windows only).

Drives the real launcher against a small fake job that writes the same
heartbeat shape as `wells bench run`, and checks the three behaviours the
launcher exists for: resume after a crash, kill a stalled run before
restarting it, and give up after -MaxRestarts.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="the PowerShell launcher is Windows-only"
)

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "wells_resumable_launch.ps1"

# -Hang: one heartbeat, then stall. -NeverComplete: crash even when resumed.
# Every attempt appends its PID to "<heartbeat>.pids" so the test can ask
# precisely which job processes are still alive, instead of grepping process
# command lines (which matches the probe's own command line).
FAKE_JOB = r"""
param([string] $Heartbeat, [int] $Seconds = 1, [switch] $Hang, [switch] $NeverComplete)
function Beat([string] $status) {
    @{ timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); status = $status } |
        ConvertTo-Json -Compress | Set-Content -LiteralPath $Heartbeat -Encoding ascii
}
Add-Content -LiteralPath "$Heartbeat.pids" -Value $PID -Encoding ascii
if ($args -contains '--resume' -and -not $NeverComplete) { Beat 'complete'; 'resumed'; exit 0 }
if ($Hang) { Beat 'running'; Start-Sleep -Seconds 600; exit 1 }
for ($i = 0; $i -lt $Seconds; $i++) { Beat 'running'; Start-Sleep -Seconds 1 }
'crashed'
exit 3
"""


def _powershell() -> str:
    return shutil.which("powershell") or shutil.which("pwsh") or ""


def _stray_jobs(pids_file: Path, job: Path) -> list[int]:
    """Job processes from the test's own PID records that are still alive."""
    try:
        pids = [int(x) for x in pids_file.read_text(encoding="utf-8").split()]
    except (OSError, ValueError):
        return []
    if not pids:
        return []
    out = subprocess.run(
        [_powershell(), "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process | ForEach-Object { "
         "\"$($_.ProcessId)|$($_.CommandLine)\" }"],
        capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace",
    ).stdout.splitlines()
    live: list[int] = []
    for line in out:
        pid_s, _, cmdline = line.partition("|")
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        if pid in pids and str(job) in cmdline:
            live.append(pid)
    return live


@pytest.fixture
def fake_job(tmp_path: Path):
    job = tmp_path / "fake_job.ps1"
    job.write_text(FAKE_JOB, encoding="ascii")
    yield job
    time.sleep(0.5)
    for pid in _stray_jobs(tmp_path / "hb.json.pids", job):  # non-empty only if broken
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)


def _launch(tmp_path: Path, cmd: str, *, max_restarts: int = 2) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            _powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(SCRIPT),
            "-Cmd", cmd,
            "-Heartbeat", str(tmp_path / "hb.json"),
            "-Log", str(tmp_path / "logs" / "job.log"),
            "-MaxRestarts", str(max_restarts),
            "-StaleAfter", "4",
            "-PollEvery", "1",
        ],
        capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace",
    )


def _cmd(job: Path, hb: Path, *switches: str) -> str:
    return (
        f'{_powershell()} -NoProfile -ExecutionPolicy Bypass -File "{job}" '
        f'-Heartbeat "{hb}" -Seconds 1 {" ".join(switches)}'.strip()
    )


def test_launcher_resumes_a_job_that_crashed(tmp_path: Path, fake_job: Path):
    hb = tmp_path / "hb.json"
    proc = _launch(tmp_path, _cmd(fake_job, hb))
    assert proc.returncode == 0, proc.stdout + proc.stderr

    assert "exited with code 3" in proc.stdout          # the first attempt died
    assert "restarting with --resume" in proc.stdout    # relaunched from checkpoint
    assert "heartbeat reports complete" in proc.stdout
    assert proc.stdout.count("restarting with --resume") == 1

    log = (tmp_path / "logs" / "job.log").read_text(encoding="utf-8", errors="replace")
    assert "crashed" in log and "resumed" in log        # both attempts share one log
    assert json.loads(hb.read_text(encoding="utf-8"))["status"] == "complete"
    assert (tmp_path / "logs" / "job_watchdog.log").is_file()
    assert _stray_jobs(tmp_path / "hb.json.pids", fake_job) == []


def test_launcher_kills_a_stalled_job_before_resuming(tmp_path: Path, fake_job: Path):
    hb = tmp_path / "hb.json"
    proc = _launch(tmp_path, _cmd(fake_job, hb, "-Hang"))
    assert proc.returncode == 0, proc.stdout + proc.stderr

    assert "heartbeat stale" in proc.stdout and "killing before restart" in proc.stdout
    assert "confirmed the process tree" in proc.stdout  # verified dead, not assumed
    assert "restarting with --resume" in proc.stdout
    assert "heartbeat reports complete" in proc.stdout
    assert _stray_jobs(tmp_path / "hb.json.pids", fake_job) == []  # the hung first attempt was really killed


def test_launcher_gives_up_after_max_restarts(tmp_path: Path, fake_job: Path):
    proc = _launch(tmp_path, _cmd(fake_job, tmp_path / "hb.json", "-NeverComplete"),
                   max_restarts=1)
    assert proc.returncode == 1
    assert proc.stdout.count("restarting with --resume") == 1
    assert "max restarts (1) reached" in proc.stdout
