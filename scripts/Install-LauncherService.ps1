param([switch]$StartNow)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$path = '\BudaoRun\'
$name = 'LauncherApi'
$description = 'BudaoRun owned localhost API; workspace=' + $root
$existing = Get-ScheduledTask -TaskPath $path -TaskName $name -ErrorAction SilentlyContinue
if ($existing -and $existing.Description -ne $description) { throw 'Not this workspace owned task; left unchanged.' }
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$hostExe = (Get-Command pwsh.exe).Source
$script = Join-Path $root 'scripts/Run-LauncherService.ps1'
$action = New-ScheduledTaskAction -Execute $hostExe `
    -Argument ('-NoProfile -WindowStyle Hidden -File "' + $script + '"') -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $sid
$principal = New-ScheduledTaskPrincipal -UserId $sid -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskPath $path -TaskName $name -Description $description `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
if ($StartNow) { Start-ScheduledTask -TaskPath $path -TaskName $name }
Get-ScheduledTask -TaskPath $path -TaskName $name | Select-Object TaskName,State,Description
