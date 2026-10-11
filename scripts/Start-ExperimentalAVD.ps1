param([switch]$Visible, [string]$Sdk = "$env:LOCALAPPDATA\Android\Sdk", [ValidateSet('api34','api36','api30')][string]$Variant='api34')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Experimental-Avd.ps1')
$spec = Get-ExperimentalAvd $Variant
$root = Split-Path -Parent $PSScriptRoot
$env:ANDROID_AVD_HOME = Join-Path $root '.local\avd'
$env:ANDROID_HOME = $Sdk
$name = $spec.Name
if (-not (Test-Path -LiteralPath (Join-Path $env:ANDROID_AVD_HOME "$name.avd\config.ini"))) { throw '先创建本项目AVD' }
if (Get-NetTCPConnection -LocalPort $spec.Port,($spec.Port+1) -State Listen -ErrorAction SilentlyContinue) { throw '目标实验端口被占用，未终止占用进程' }
$output = Join-Path $root '.local\runs'
New-Item -ItemType Directory -Path $output -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$arguments = @('-avd',$name,'-port',[string]$spec.Port,'-memory',[string]$spec.Ram,'-no-snapshot-load','-no-snapshot-save','-no-boot-anim','-gpu','auto','-no-audio','-timezone','Asia/Shanghai')
if (-not $Visible) { $arguments += '-no-window' }
$style = if ($Visible) { 'Normal' } else { 'Hidden' }
$process = Start-Process -FilePath (Join-Path $Sdk 'emulator\emulator.exe') -ArgumentList $arguments -WindowStyle $style -PassThru -RedirectStandardOutput (Join-Path $output "$stamp-emulator.stdout.log") -RedirectStandardError (Join-Path $output "$stamp-emulator.stderr.log")
Write-Output "Started only $name, PID=$($process.Id), serial=emulator-$($spec.Port)"
