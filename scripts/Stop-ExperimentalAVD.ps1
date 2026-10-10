$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try { uv run budao stop --serial emulator-5580; if ($LASTEXITCODE -ne 0) { throw '停止失败；未使用按名称批量杀进程' } }
finally { Pop-Location }
