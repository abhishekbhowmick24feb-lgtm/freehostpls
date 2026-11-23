# Stops the bot: ends the supervisor (so it cannot restart the bot) and any
# bot process still running from this folder. Run via the desktop shortcut.
param([switch]$Quiet)
$ErrorActionPreference = "Continue"

$root = $PSScriptRoot
$stopped = 0

# Finds the supervisor that restarts the bot. It is matched by script name as
# well as by path: a supervisor launched with a relative path ("-File
# start-bot.ps1") has no folder in its command line, and a path-only match
# missed it, so the bot came back 15 seconds after Stop.
function Get-SupervisorProcesses {
    $found = @()
    $seen = @{}
    $pidFile = Join-Path $root "data\supervisor.pid"
    if (Test-Path -LiteralPath $pidFile) {
        $savedPid = 0
        $raw = ((Get-Content -LiteralPath $pidFile -Raw -ErrorAction SilentlyContinue) -replace "\s", "")
        if ($raw -and [int]::TryParse($raw, [ref]$savedPid) -and $savedPid -gt 0) {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$savedPid" -ErrorAction SilentlyContinue
            if ($proc -and $proc.CommandLine -and $proc.CommandLine -match "start-bot\.ps1") {
                $found += $proc
                $seen[[int]$proc.ProcessId] = $true
            }
        }
    }
    $running = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match "start-bot\.ps1" -and $_.ProcessId -ne $PID })
    foreach ($proc in $running) {
        if (-not $seen.ContainsKey([int]$proc.ProcessId)) {
            $found += $proc
            $seen[[int]$proc.ProcessId] = $true
        }
    }
    return @($found)
}

# 1. Supervisors first, otherwise they would immediately restart the bot.
$supervisors = @(Get-SupervisorProcesses)

foreach ($proc in $supervisors) {
    & taskkill.exe /T /F /PID $proc.ProcessId 2>$null | Out-Null
    $stopped++
}

$pidFile = Join-Path $root "data\supervisor.pid"
if (Test-Path -LiteralPath $pidFile) {
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

# 2. Any bot processes left behind (venv launcher and its child interpreter).
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$bots = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object {
        $_.CommandLine -and
        $_.CommandLine.Contains($venvPython) -and
        $_.CommandLine.Contains("main.py")
    }

foreach ($proc in $bots) {
    & taskkill.exe /T /F /PID $proc.ProcessId 2>$null | Out-Null
    $stopped++
}

# 3. Confirm nothing is left that would bring the bot back.
Start-Sleep -Milliseconds 600
$leftover = @(Get-SupervisorProcesses).Count
if ($leftover -gt 0) {
    Write-Host "Warning: $leftover supervisor process(es) are still running."
}

if (-not $Quiet) {
    Add-Type -AssemblyName System.Windows.Forms
    if ($stopped -gt 0) {
        [System.Windows.Forms.MessageBox]::Show(
            "Discord bot stopped.",
            "Bot control",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Information
        ) | Out-Null
    } else {
        [System.Windows.Forms.MessageBox]::Show(
            "The bot was not running.",
            "Bot control",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Information
        ) | Out-Null
    }
}

Write-Host "Stopped $stopped process(es)."
