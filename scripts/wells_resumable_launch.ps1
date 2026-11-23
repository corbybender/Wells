<#
.SYNOPSIS
Run a long `wells bench run` / `wells evolve gate` detached, with a watchdog
that resumes it from its checkpoint if it dies.

.DESCRIPTION
The Windows counterpart of scripts/wells_resumable_launch.sh. The command
must be checkpoint/resume-capable: it has to accept --resume and write a
heartbeat file via --heartbeat PATH (`wells bench run` and `wells evolve
gate` both do).

What this does:
  - Starts the command in its own (hidden) console, so it outlives the shell
    or agent session that launched it.
  - Polls the heartbeat file's `timestamp`. If it goes stale (no update in
    -StaleAfter seconds) or the process is gone, relaunches the command with
    --resume, up to -MaxRestarts times, then gives up and leaves the log.
  - Exits cleanly once the heartbeat reports status "complete", or the
    command exits 0 on its own.

What it does not do:
  - Survive a reboot (register it with Task Scheduler for that).
  - Retry a command that is simply wrong; -MaxRestarts bounds the damage.

Unlike the bash version there is no pidfile: Start-Process hands back the
process itself, so liveness is read from that handle and can never be
confused by a recycled PID.

.EXAMPLE
powershell -File scripts\wells_resumable_launch.ps1 `
  -Cmd "uv run python -c `"from wells.main import main; main()`" evolve gate <id> --split val --profile zai --timeout 1200" `
  -Heartbeat .wells\evolve\mutations\<id>\heartbeat.json `
  -Log logs\evolve_gate_<id>.log
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $Cmd,
    [Parameter(Mandatory = $true)] [string] $Heartbeat,
    [Parameter(Mandatory = $true)] [string] $Log,
    [int] $MaxRestarts = 20,
    [int] $StaleAfter = 900,
    [int] $PollEvery = 60
)

$ErrorActionPreference = 'Stop'

function Resolve-FullPath([string] $Path) {
    return $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
}

$Log = Resolve-FullPath $Log
$Heartbeat = Resolve-FullPath $Heartbeat
$logDir = [IO.Path]::GetDirectoryName($Log)
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$WatchdogLog = [IO.Path]::Combine(
    $logDir, [IO.Path]::GetFileNameWithoutExtension($Log) + '_watchdog.log')

function Write-Watch([string] $Message) {
    $line = '{0} [watchdog] {1}' -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'), $Message
    Write-Output $line
    Add-Content -LiteralPath $WatchdogLog -Value $line -Encoding utf8
}

function Read-Heartbeat {
    try {
        $h = Get-Content -Raw -LiteralPath $Heartbeat -ErrorAction Stop | ConvertFrom-Json
        return @{ Status = [string] $h.status; Timestamp = [long] $h.timestamp }
    } catch {
        return @{ Status = ''; Timestamp = 0 }
    }
}

function Stop-Tree([System.Diagnostics.Process] $Proc) {
    # /T reaches the grandchildren too (uv run -> python): killing only the
    # tracked process leaves them running, still writing the checkpoint and
    # heartbeat files a replacement would also be writing.
    if ($Proc.HasExited) { return }
    & taskkill.exe /F /T /PID $Proc.Id 2>&1 | Out-Null
    if (-not $Proc.WaitForExit(10000)) {
        throw "PID $($Proc.Id) is still alive 10s after taskkill; refusing to launch a duplicate."
    }
}

function Start-Job([bool] $Resume) {
    # The command line lives in a .cmd file rather than in an argument list:
    # cmd.exe then parses its quotes exactly as it would interactively,
    # instead of Start-Process re-quoting a flattened argument string.
    $runner = [IO.Path]::Combine(
        $logDir, [IO.Path]::GetFileNameWithoutExtension($Log) + '.cmd')
    $full = if ($Resume) { "$Cmd --resume" } else { $Cmd }
    Set-Content -LiteralPath $runner -Value ('{0} >> "{1}" 2>&1' -f $full, $Log) -Encoding ascii
    return Start-Process -FilePath $env:ComSpec `
        -ArgumentList '/c', ('"{0}"' -f $runner) -WindowStyle Hidden -PassThru
}

Write-Watch '=== wells_resumable_launch starting ==='
Write-Watch "cmd: $Cmd"
Write-Watch "heartbeat: $Heartbeat  log: $Log"
Write-Watch "max_restarts=$MaxRestarts stale_after=${StaleAfter}s poll_every=${PollEvery}s"

$restarts = 0
$proc = Start-Job $false
Write-Watch ('launched (attempt {0}/{1}), PID={2}' -f ($restarts + 1), ($MaxRestarts + 1), $proc.Id)

while ($true) {
    Start-Sleep -Seconds $PollEvery
    $hb = Read-Heartbeat
    if ($hb.Status -eq 'complete') {
        Write-Watch 'heartbeat reports complete - done, no more restarts.'
        exit 0
    }

    $alive = -not $proc.HasExited
    $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - $hb.Timestamp
    if ($alive -and $hb.Timestamp -gt 0 -and $age -lt $StaleAfter) {
        Write-Watch ('alive, heartbeat {0}s old, status={1} - OK' -f $age, $hb.Status)
        continue
    }

    if ($alive) {
        Write-Watch ('PID {0} alive but heartbeat stale ({1}s > {2}s) - killing before restart' -f $proc.Id, $age, $StaleAfter)
        Stop-Tree $proc
        Write-Watch ('confirmed the process tree of PID {0} is dead.' -f $proc.Id)
    } else {
        Write-Watch ('PID {0} exited with code {1} (heartbeat age {2}s, status={3})' -f $proc.Id, $proc.ExitCode, $age, $hb.Status)
        if ($proc.ExitCode -eq 0) {
            Write-Watch 'command finished on its own - done, no more restarts.'
            exit 0
        }
    }

    if ($restarts -ge $MaxRestarts) {
        Write-Watch "max restarts ($MaxRestarts) reached - giving up. Check $Log and $Heartbeat by hand."
        exit 1
    }
    $restarts++
    $proc = Start-Job $true
    Write-Watch ('restarting with --resume (attempt {0}/{1}), PID={2}' -f ($restarts + 1), ($MaxRestarts + 1), $proc.Id)
}
