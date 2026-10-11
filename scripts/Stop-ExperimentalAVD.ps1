param([ValidateSet('api34','api36','api30')][string]$Variant='api34')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Experimental-Avd.ps1')
$spec = Get-ExperimentalAvd $Variant
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    uv run budao stop --serial "emulator-$($spec.Port)" --avd $spec.Name
    if ($LASTEXITCODE -ne 0) { throw '停止失败；未使用按名称批量杀进程' }
    # emu kill只确认请求已接收；等待本项目端口和进程关闭，避免紧接着启动时报占用。
    $pattern = '-avd\s+"?' + [regex]::Escape($spec.Name) + '"?(?:\s|$)'
    for ($i=0; $i -lt 20; $i++) {
        $ports = Get-NetTCPConnection -LocalPort $spec.Port,($spec.Port+1) -State Listen -ErrorAction SilentlyContinue
        $owned = Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('emulator.exe','qemu-system-x86_64.exe') -and $_.CommandLine -match $pattern }
        if (-not $ports -and -not $owned) { Write-Output "已确认 $($spec.Name) 完全退出"; return }
        Start-Sleep -Seconds 1
    }
    throw '停止请求已发送，但20秒内未确认完全退出；未强制终止任何进程'
}
finally { Pop-Location }
