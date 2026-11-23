# Creates a single "Bot Control" shortcut on your Desktop.
# It opens without a PowerShell console window (launched through bot-control.vbs).
# Run once:  powershell -ExecutionPolicy Bypass -File install-desktop-shortcuts.ps1
$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell

# The panel replaces these older one-action shortcuts.
foreach ($old in @("Start Bot", "Stop Bot", "Bot Status")) {
    $oldPath = Join-Path $desktop "$old.lnk"
    if (Test-Path -LiteralPath $oldPath) {
        Remove-Item -LiteralPath $oldPath -Force
        Write-Host "Removed old shortcut: $oldPath"
    }
}

$path = Join-Path $desktop "Bot Control.lnk"
$shortcut = $shell.CreateShortcut($path)
$shortcut.TargetPath = Join-Path $env:SystemRoot "System32\wscript.exe"
$shortcut.Arguments = "`"$root\bot-control.vbs`""
$shortcut.WorkingDirectory = $root
$shortcut.WindowStyle = 1
$shortcut.Description = "Turn the Discord marketplace bot on or off."
$shortcut.IconLocation = "$root\.venv\Scripts\python.exe,0"
$shortcut.Save()
Write-Host "Created $path"

Write-Host "Done. Double-click Bot Control on your desktop."
