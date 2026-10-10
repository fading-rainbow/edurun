param([string]$Sdk = "$env:LOCALAPPDATA\Android\Sdk", [ValidateSet('api34','api36')][string]$Variant='api34')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Experimental-Avd.ps1')
$spec = Get-ExperimentalAvd $Variant
$root = Split-Path -Parent $PSScriptRoot
$env:JAVA_HOME = 'E:\Android\AndroidStudio-2026.2.1.8\android-studio\jbr'
$env:ANDROID_AVD_HOME = Join-Path $root '.local\avd'
$env:ANDROID_HOME = $Sdk
$env:ANDROID_SDK_ROOT = $Sdk
$name = $spec.Name
$installedCli = Join-Path $Sdk 'cmdline-tools\22.0'
if (-not (Test-Path -LiteralPath $installedCli)) {
    New-Item -ItemType Directory -Path $installedCli | Out-Null
    $sourceCli = Join-Path $root '.local\tools\android-cli-15859902\cmdline-tools'
    Get-ChildItem -LiteralPath $sourceCli | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $installedCli -Recurse
    }
}
if (-not (Select-String -LiteralPath (Join-Path $installedCli 'source.properties') -Pattern '^Pkg.Revision=22.0$' -Quiet)) {
    throw 'SDK中同名工具版本不符，保留而不覆盖'
}
$manager = Join-Path $installedCli 'bin\avdmanager.bat'
$target = Join-Path $env:ANDROID_AVD_HOME ($name + '.avd')
New-Item -ItemType Directory -Path $env:ANDROID_AVD_HOME -Force | Out-Null
if (Test-Path -LiteralPath $target) { throw '实验AVD已存在；保留原数据，不强制重建' }
'no' | & $manager create avd --name $name --package $spec.Package --device pixel_6 --path $target
if ($LASTEXITCODE -ne 0) { throw "AVD创建失败：$LASTEXITCODE" }
$config = Join-Path $target 'config.ini'
$lines = Get-Content -LiteralPath $config
$changes = @{'hw.ramSize'=[string]$spec.Ram;'hw.cpu.ncore'='4';'hw.gpu.enabled'='yes';'hw.gpu.mode'='auto';'hw.lcd.width'='720';'hw.lcd.height'='1280';'hw.lcd.density'='240';'hw.camera.back'='none';'hw.camera.front'='none'}
foreach($key in $changes.Keys) {
    $pattern = '^' + [regex]::Escape($key) + '\s*='
    $lines = @($lines | Where-Object { $_ -notmatch $pattern }) + ($key + '=' + $changes[$key])
}
$lines | Set-Content -LiteralPath $config -Encoding ascii
Write-Output "Created independent AVD: $name ($($spec.Ram)MiB; existing AVDs unchanged)"
