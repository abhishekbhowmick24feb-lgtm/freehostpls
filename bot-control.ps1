# Bot Control - one small window with Start, Stop, Restart, a ping/uptime
# section and a link to the logs. The bot only runs when you press Start.
# Launched from the Desktop shortcut via bot-control.vbs, so no console appears.
# Status only (no window): powershell -ExecutionPolicy Bypass -File bot-control.ps1 -Status
param([switch]$Status)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$root = $PSScriptRoot
$startScript = Join-Path $root "start-bot.ps1"
$stopScript = Join-Path $root "stop-bot.ps1"
$logDir = Join-Path $root "logs"
$statusFile = Join-Path $root "data\status.json"

# The bot rewrites the status file every 10 seconds; older than this is stale.
$staleAfter = 45

$green = [System.Drawing.Color]::FromArgb(87, 242, 135)
$gold = [System.Drawing.Color]::FromArgb(241, 196, 15)
$red = [System.Drawing.Color]::FromArgb(237, 66, 69)
$blue = [System.Drawing.Color]::FromArgb(88, 101, 242)
$gray = [System.Drawing.Color]::FromArgb(150, 150, 150)
$text = [System.Drawing.Color]::Gainsboro

function Invoke-Hidden([string]$script, [string[]]$extra = @()) {
    $arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", "`"$script`"") + $extra
    Start-Process -FilePath "powershell.exe" -ArgumentList $arguments -WorkingDirectory $root -WindowStyle Hidden
}

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

function Get-BotState {
    $venvPython = Join-Path $root ".venv\Scripts\python.exe"
    $supervisors = @(Get-SupervisorProcesses)
    $bots = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine.Contains($venvPython) -and
            $_.CommandLine.Contains("main.py")
        })

    if ($bots.Count -gt 0) {
        return [pscustomobject]@{ State = "RUNNING"; Detail = "PID $($bots[0].ProcessId)" }
    }
    if ($supervisors.Count -gt 0) {
        return [pscustomobject]@{ State = "STARTING"; Detail = "waiting for login" }
    }
    return [pscustomobject]@{ State = "OFFLINE"; Detail = "not running" }
}

function Get-BotTelemetry {
    if (-not (Test-Path -LiteralPath $statusFile)) { return $null }
    try {
        $data = Get-Content -LiteralPath $statusFile -Raw | ConvertFrom-Json
    } catch {
        return $null
    }
    if ($null -eq $data.updated) { return $null }

    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    return [pscustomobject]@{
        Ping = $data.ping_ms
        Uptime = [int]$data.uptime
        Guilds = [int]$data.guilds
        Commands = [int]$data.commands
        Age = [int]($now - [double]$data.updated)
    }
}

function Format-Uptime([int]$seconds) {
    if ($seconds -lt 60) { return "$seconds s" }
    $span = [TimeSpan]::FromSeconds($seconds)
    if ($span.TotalHours -lt 1) { return ("{0}m {1:00}s" -f $span.Minutes, $span.Seconds) }
    if ($span.TotalDays -lt 1) { return ("{0}h {1:00}m" -f ([int][Math]::Floor($span.TotalHours)), $span.Minutes) }
    return ("{0}d {1}h" -f ([int][Math]::Floor($span.TotalDays)), $span.Hours)
}

function Get-PingView($state, $telemetry) {
    if ($state -eq "OFFLINE") {
        return [pscustomobject]@{ Text = "--"; Color = $gray; Detail = "bot is offline" }
    }
    if ($state -eq "STARTING") {
        return [pscustomobject]@{ Text = "--"; Color = $gold; Detail = "connecting to Discord..." }
    }
    if ($null -eq $telemetry) {
        return [pscustomobject]@{ Text = "--"; Color = $gold; Detail = "no status from the bot yet" }
    }
    if ($telemetry.Age -gt $staleAfter) {
        return [pscustomobject]@{ Text = "--"; Color = $gold; Detail = "no update for $($telemetry.Age)s" }
    }
    if ($null -eq $telemetry.Ping) {
        return [pscustomobject]@{ Text = "--"; Color = $gold; Detail = "waiting for the first ping" }
    }

    $uptime = Format-Uptime $telemetry.Uptime
    $servers = if ($telemetry.Guilds -eq 1) { "1 server" } else { "$($telemetry.Guilds) servers" }
    return [pscustomobject]@{
        Text = ("{0} ms" -f ([int]$telemetry.Ping))
        Color = $green
        Detail = "up $uptime | $servers | $($telemetry.Commands) commands"
    }
}

function Stop-Bot {
    Invoke-Hidden $stopScript "-Quiet"
    # Wait for the supervisor and bot to be gone so a restart cannot collide.
    for ($i = 0; $i -lt 20; $i++) {
        if ((Get-BotState).State -eq "OFFLINE") { break }
        Start-Sleep -Milliseconds 400
    }
}

function Start-Bot {
    if ((Get-BotState).State -ne "OFFLINE") { return }
    Invoke-Hidden $startScript
}

if ($Status) {
    $state = Get-BotState
    $view = Get-PingView $state.State (Get-BotTelemetry)
    Write-Host ("{0} - {1}" -f $state.State, $state.Detail)
    Write-Host ("Ping: {0}" -f $view.Text)
    Write-Host $view.Detail
    exit 0
}

# ---- window ----
$form = New-Object System.Windows.Forms.Form
$form.Text = "Marketplace Bot"
$form.ClientSize = New-Object System.Drawing.Size(380, 316)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedSingle"
$form.MaximizeBox = $false
$form.BackColor = [System.Drawing.Color]::FromArgb(32, 34, 37)
$form.Font = New-Object System.Drawing.Font("Segoe UI", 9)

$title = New-Object System.Windows.Forms.Label
$title.Text = "Marketplace Guard"
$title.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 15)
$title.ForeColor = $gold
$title.AutoSize = $true
$title.Location = New-Object System.Drawing.Point(20, 16)
$form.Controls.Add($title)

$statusLabel = New-Object System.Windows.Forms.Label
$statusLabel.Text = "Checking..."
$statusLabel.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 11)
$statusLabel.ForeColor = $text
$statusLabel.AutoSize = $true
$statusLabel.Location = New-Object System.Drawing.Point(22, 52)
$form.Controls.Add($statusLabel)

$detailLabel = New-Object System.Windows.Forms.Label
$detailLabel.Text = ""
$detailLabel.ForeColor = $gray
$detailLabel.AutoSize = $true
$detailLabel.Location = New-Object System.Drawing.Point(24, 78)
$form.Controls.Add($detailLabel)

$pingBox = New-Object System.Windows.Forms.Panel
$pingBox.Location = New-Object System.Drawing.Point(20, 104)
$pingBox.Size = New-Object System.Drawing.Size(340, 78)
$pingBox.BackColor = [System.Drawing.Color]::FromArgb(41, 43, 47)
$form.Controls.Add($pingBox)

$pingTitle = New-Object System.Windows.Forms.Label
$pingTitle.Text = "PING"
$pingTitle.Font = New-Object System.Drawing.Font("Segoe UI", 8)
$pingTitle.ForeColor = $gray
$pingTitle.AutoSize = $true
$pingTitle.Location = New-Object System.Drawing.Point(12, 8)
$pingBox.Controls.Add($pingTitle)

$pingValue = New-Object System.Windows.Forms.Label
$pingValue.Text = "--"
$pingValue.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 15)
$pingValue.ForeColor = $gray
$pingValue.AutoSize = $true
$pingValue.Location = New-Object System.Drawing.Point(12, 24)
$pingBox.Controls.Add($pingValue)

$pingDetail = New-Object System.Windows.Forms.Label
$pingDetail.Text = ""
$pingDetail.ForeColor = $text
$pingDetail.AutoSize = $true
$pingDetail.Location = New-Object System.Drawing.Point(12, 55)
$pingBox.Controls.Add($pingDetail)

function New-Button([string]$text, [int]$x, [int]$y, [int]$w, $back, $fore) {
    $button = New-Object System.Windows.Forms.Button
    $button.Text = $text
    $button.Size = New-Object System.Drawing.Size($w, 36)
    $button.Location = New-Object System.Drawing.Point($x, $y)
    $button.FlatStyle = "Flat"
    $button.FlatAppearance.BorderSize = 0
    $button.BackColor = $back
    $button.ForeColor = $fore
    $button.Cursor = [System.Windows.Forms.Cursors]::Hand
    return $button
}

$startButton = New-Button "Start" 20 194 104 $green ([System.Drawing.Color]::FromArgb(20, 20, 20))
$stopButton = New-Button "Stop" 138 194 104 $red ([System.Drawing.Color]::White)
$restartButton = New-Button "Restart" 256 194 104 $blue ([System.Drawing.Color]::White)
$form.Controls.AddRange(@($startButton, $stopButton, $restartButton))

$logsButton = New-Button "Open logs" 256 240 104 ([System.Drawing.Color]::FromArgb(54, 57, 63)) $text
$form.Controls.Add($logsButton)

$hint = New-Object System.Windows.Forms.Label
$hint.Text = "The bot only runs when you press Start.`nClosing this window leaves it as it is."
$hint.ForeColor = $gray
$hint.AutoSize = $true
$hint.Location = New-Object System.Drawing.Point(22, 248)
$form.Controls.Add($hint)

function Update-View {
    $state = Get-BotState
    $view = Get-PingView $state.State (Get-BotTelemetry)

    $statusLabel.Text = $state.State
    $statusLabel.ForeColor = switch ($state.State) {
        "RUNNING" { $green }
        "STARTING" { $gold }
        default { $red }
    }
    $detailLabel.Text = $state.Detail

    $pingValue.Text = $view.Text
    $pingValue.ForeColor = $view.Color
    $pingDetail.Text = $view.Detail

    $running = ($state.State -ne "OFFLINE")
    $startButton.Enabled = -not $running
    $stopButton.Enabled = $running
    $restartButton.Enabled = $running
}

$startButton.Add_Click({ Start-Bot; Start-Sleep -Milliseconds 700; Update-View })
$stopButton.Add_Click({ Stop-Bot; Update-View })
$restartButton.Add_Click({ Stop-Bot; Start-Bot; Start-Sleep -Milliseconds 700; Update-View })
$logsButton.Add_Click({ Start-Process -FilePath "explorer.exe" -ArgumentList "`"$logDir`"" })

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 2000
$timer.Add_Tick({ Update-View })
$timer.Start()

Update-View
[void]$form.ShowDialog()
$timer.Stop()
