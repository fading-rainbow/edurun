param([switch]$Visible, [string]$Sdk = "$env:LOCALAPPDATA\Android\Sdk")
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$env:ANDROID_AVD_HOME = Join-Path $root '.local\avd'
$env:ANDROID_HOME = $Sdk
$name = 'BudaoRun_API34_Play'
if (-not (Test-Path -LiteralPath (Join-Path $env:ANDROID_AVD_HOME "$name.avd\config.ini"))) { throw '先创建本项目AVD' }
if (Get-NetTCPConnection -LocalPort 5580,5581 -State Listen -ErrorAction SilentlyContinue) { throw '5580/5581端口被占用，未终止占用进程' }
$output = Join-Path $root '.local\runs'
New-Item -ItemType Directory -Path $output -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$arguments = @('-avd',$name,'-port','5580','-memory','2048','-no-snapshot-load','-no-snapshot-save','-no-boot-anim','-gpu','auto','-no-audio')
if (-not $Visible) { $arguments += '-no-window' }
$style = if ($Visible) { 'Normal' } else { 'Hidden' }
$process = Start-Process -FilePath (Join-Path $Sdk 'emulator\emulator.exe') -ArgumentList $arguments -WindowStyle $style -PassThru -RedirectStandardOutput (Join-Path $output "$stamp-emulator.stdout.log") -RedirectStandardError (Join-Path $output "$stamp-emulator.stderr.log")
Write-Output "Started only $name, PID=$($process.Id), serial=emulator-5580"
