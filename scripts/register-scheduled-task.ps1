<#
.SYNOPSIS
    Registers (or re-registers) the Windows Scheduled Task that runs the
    Telegram Batch Media Downloader every 6 hours, using the project's
    .venv interpreter.

.DESCRIPTION
    - Runs `.venv\Scripts\python.exe -m src.main` from the project root.
    - Triggers every 6 hours, starting now, indefinitely.
    - "Logged on only": runs under the current user's interactive session;
      does not run if you're logged off, and does not require storing your
      Windows account password. If a trigger is missed (PC off/asleep), it
      catches up as soon as you're next logged on (-StartWhenAvailable).
    - Re-running this script updates the existing task in place rather than
      creating a duplicate.

.NOTES
    Requires the .venv to already exist (see README.md "Development" /
    Virtual env setup) and data/downloader.session to already be
    authenticated (interactive login isn't possible from a scheduled task).
#>

$ErrorActionPreference = "Stop"

$ProjectRoot = "D:\telegram_media_grabber"
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$TaskName = "TelegramMediaGrabber"

if (-not (Test-Path $PythonExe)) {
    throw "Virtual env python not found at $PythonExe. Create it first: python -m venv .venv"
}
if (-not (Test-Path (Join-Path $ProjectRoot "data\downloader.session"))) {
    Write-Warning "No data\downloader.session found yet. The task will fail non-interactively until you run 'python -m src.main' manually once to complete Telegram login."
}

$Action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "-m src.main" `
    -WorkingDirectory $ProjectRoot

$Trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Hours 6) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Description "Telegram Batch Media Downloader: scans configured channels for new media every 6 hours (logged-on sessions only)." `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName'."
Get-ScheduledTaskInfo -TaskName $TaskName | Select-Object TaskName, LastRunTime, NextRunTime, LastTaskResult
