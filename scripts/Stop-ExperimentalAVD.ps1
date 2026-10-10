param([ValidateSet('api34','api36')][string]$Variant='api34')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Experimental-Avd.ps1')
$spec = Get-ExperimentalAvd $Variant
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try { uv run budao stop --serial "emulator-$($spec.Port)" --avd $spec.Name; if ($LASTEXITCODE -ne 0) { throw '停止失败；未使用按名称批量杀进程' } }
finally { Pop-Location }
