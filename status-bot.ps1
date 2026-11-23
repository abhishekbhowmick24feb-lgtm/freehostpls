# Shows whether the Discord bot is currently running.
# Opens a small status window (or prints text with -Quiet).
param([switch]$Quiet)
$ErrorActionPreference = "Continue"
Add-Type -AssemblyName System.Windows.Forms

$root = $PSScriptRoot
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

# Finds the supervisor that keeps the bot alive. It is matched by script name
# as well as by path, because a supervisor launched with a relative path
# ("-File start-bot.ps1") has no folder in its command line.
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

$supervisors = @(Get-SupervisorProcesses)

$bots = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object {
        $_.CommandLine -and
        $_.CommandLine.Contains($venvPython) -and
        $_.CommandLine.Contains("main.py")
    })

$lastLogin = ""
$logPath = Join-Path $root "logs\bot.log"
if (Test-Path -LiteralPath $logPath) {
    $match = Select-String -LiteralPath $logPath -Pattern "Logged in as" |
        Select-Object -Last 1
    if ($match) {
        $lastLogin = $match.Line.Trim()
    }
}

if ($bots.Count -gt 0) {
    $state = "RUNNING"
    $icon = [System.Windows.Forms.MessageBoxIcon]::Information
    $lines = @(
        "Discord bot: RUNNING",
        "Bot process: PID $($bots[0].ProcessId)",
        "Supervisor: $(if ($supervisors.Count -gt 0) { 'yes' } else { 'no (manual start)' })"
    )
    if ($lastLogin) { $lines += "Last login: $lastLogin" }
} elseif ($supervisors.Count -gt 0) {
    $state = "STARTING"
    $icon = [System.Windows.Forms.MessageBoxIcon]::Warning
    $lines = @(
        "Discord bot: STARTING / RESTARTING",
        "The supervisor is running but no bot process was found right now.",
        "Check logs\bot.log in a few seconds."
    )
} else {
    $state = "OFFLINE"
    $icon = [System.Windows.Forms.MessageBoxIcon]::Warning
    $lines = @(
        "Discord bot: OFFLINE",
        "No supervisor or bot process is running.",
        "Use the Start Bot shortcut to bring it online."
    )
    if ($lastLogin) { $lines += "Last login seen: $lastLogin" }
}

$message = $lines -join "`n"

if ($Quiet) {
    Write-Host "$state`n$message"
} else {
    [System.Windows.Forms.MessageBox]::Show(
        $message,
        "Bot status",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        $icon
    ) | Out-Null
}
