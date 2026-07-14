# Wells one-time setup (Windows) — puts the `wells` command on your PATH.
#
# Usage: .\install.ps1
#
# This does NOT build or install the Python package (that needs hatchling
# from PyPI, which corporate proxies often block). It just makes the
# wells.bat launcher in this directory runnable from anywhere. `wells`
# itself still handles the venv/deps automatically on first real run.

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$Entries = @()
if (-not [string]::IsNullOrEmpty($UserPath)) { $Entries = $UserPath -split ';' }

if ($Entries -contains $ScriptDir) {
    Write-Host "[wells] Already on PATH."
} else {
    $NewPath = if ($Entries.Count -eq 0) { $ScriptDir } else { "$UserPath;$ScriptDir" }
    [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
    Write-Host "[wells] Added $ScriptDir to your User PATH."
    Write-Host "[wells] Open a new terminal for it to take effect."
}

Write-Host "[wells] Setup complete. Open a new terminal, then try: wells info"
