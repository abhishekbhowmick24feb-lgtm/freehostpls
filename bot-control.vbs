' Launches the Bot Control panel without a PowerShell console window.
' The .lnk shortcut on the Desktop points at this file via wscript.exe.
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & scriptDir & "\bot-control.ps1"""
shell.Run command, 0, False
