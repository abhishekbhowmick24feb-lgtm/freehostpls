# Keeps the Discord bot running: starts it, and restarts it if the process exits.
# Waits for an internet connection first, so a manual start right after boot does not crash-loop.
# The bot's console output is written to logs\bot-console.log / bot-console.err.log.

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$logDir = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$supervisorLog = Join-Path $logDir "supervisor.log"
$consoleLog = Join-Path $logDir "bot-console.log"
$consoleErr = Join-Path $logDir "bot-console.err.log"
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

function Write-SupervisorLog([string]$message) {
    $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    Add-Content -LiteralPath $supervisorLog -Value "[$stamp] $message"
}

function Test-Online {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $connect = $client.ConnectAsync("discord.com", 443)
        if (-not $connect.Wait(5000)) {
            $client.Close()
            return $false
        }
        $ok = -not $connect.IsFaulted
        $client.Close()
        return $ok
    } catch {
        return $false
    }
}

function Rotate-Log([string]$path) {
    if (Test-Path -LiteralPath $path) {
        $size = (Get-Item -LiteralPath $path).Length
        if ($size -gt 5MB) {
            Move-Item -LiteralPath $path -Destination "$path.old" -Force
        }
    }
}

# Only one supervisor may run, so logging in twice cannot start two bots.
$createdNew = $false
$mutex = New-Object System.Threading.Mutex($true, "DiscordMarketplaceBotSupervisor", [ref]$createdNew)
if (-not $createdNew) {
    Write-SupervisorLog "another supervisor is already running; exiting"
    exit 0
}

# Recorded so Stop and Status can find this supervisor even when it was launched
# with a relative path and its command line does not contain the folder.
$dataDir = Join-Path $PSScriptRoot "data"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$pidFile = Join-Path $dataDir "supervisor.pid"
Set-Content -LiteralPath $pidFile -Value $PID -Encoding ASCII
Write-SupervisorLog "supervisor running as PID $PID"

# Wait for a working connection before the first launch.
$waited = 0
while (-not (Test-Online)) {
    if ($waited -eq 0) {
        Write-SupervisorLog "no internet connection yet; waiting before starting the bot"
    }
    Start-Sleep -Seconds 5
    $waited += 5
}
if ($waited -gt 0) {
    Write-SupervisorLog "internet is available after $waited s; starting the bot"
}

$quickExits = 0
while ($true) {
    Rotate-Log $consoleLog
    Rotate-Log $consoleErr
    Write-SupervisorLog "starting bot"
    $started = Get-Date
    $code = -1
    try {
        $process = Start-Process -FilePath $python `
            -ArgumentList "main.py" `
            -WorkingDirectory $PSScriptRoot `
            -RedirectStandardOutput $consoleLog `
            -RedirectStandardError $consoleErr `
            -WindowStyle Hidden -PassThru -Wait
        $code = $process.ExitCode
    } catch {
        Write-SupervisorLog "could not start bot: $_"
    }

    $ranFor = [int]((Get-Date) - $started).TotalSeconds
    if ($ranFor -lt 30) {
        $quickExits++
    } else {
        $quickExits = 0
    }
    $delay = [Math]::Min(15 * [Math]::Max($quickExits, 1), 120)
    Write-SupervisorLog ("bot exited with code {0} after {1}s; restarting in {2}s" -f $code, $ranFor, $delay)
    Start-Sleep -Seconds $delay
}
