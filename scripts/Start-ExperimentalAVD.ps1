param([switch]$Visible, [string]$Sdk = "$env:LOCALAPPDATA\Android\Sdk", [ValidateSet('api34','api36','api30')][string]$Variant='api34', [ValidateSet(0,2048,3072,4096,6144,8192)][int]$MemoryMiB=0, [ValidateSet('auto','host','swiftshader')][string]$GpuMode='auto')
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
$ram = if ($MemoryMiB) { $MemoryMiB } elseif ($Visible -and $Variant -eq 'api34') { 4096 } else { $spec.Ram }
$arguments = @('-avd',$name,'-port',[string]$spec.Port,'-memory',[string]$ram,'-no-snapshot-load','-no-snapshot-save','-no-boot-anim','-gpu',$GpuMode,'-no-audio','-timezone','Asia/Shanghai')
if (-not $Visible) { $arguments += '-no-window' }
$style = if ($Visible) { 'Normal' } else { 'Hidden' }
$process = Start-Process -FilePath (Join-Path $Sdk 'emulator\emulator.exe') -ArgumentList $arguments -WindowStyle $style -PassThru -RedirectStandardOutput (Join-Path $output "$stamp-emulator.stdout.log") -RedirectStandardError (Join-Path $output "$stamp-emulator.stderr.log")
Write-Output "Started only $name, PID=$($process.Id), serial=emulator-$($spec.Port)"
