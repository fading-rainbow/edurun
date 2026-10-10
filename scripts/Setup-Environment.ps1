param([string]$Sdk = "$env:LOCALAPPDATA\Android\Sdk")
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    uv sync --locked --group dev
    if ($LASTEXITCODE -ne 0) { throw 'Python依赖安装失败' }
    uv run python scripts/prepare_android.py all
    if ($LASTEXITCODE -ne 0) { throw '官方工具下载/校验失败' }
    $env:JAVA_HOME = 'E:\Android\AndroidStudio-2026.2.1.8\android-studio\jbr'
    $env:ANDROID_HOME = $Sdk
    $manager = Join-Path $root '.local\tools\android-cli-15859902\cmdline-tools\bin\sdkmanager.bat'
    & $manager --sdk_root=$Sdk 'system-images;android-34;google_apis_playstore;x86_64'
    if ($LASTEXITCODE -ne 0) { throw '镜像安装命令失败' }
    $image = Join-Path $Sdk 'system-images\android-34\google_apis_playstore\x86_64\source.properties'
    if (-not (Test-Path -LiteralPath $image)) { throw '镜像实际未安装，不能仅凭退出码宣称成功' }
    if (-not (Test-Path -LiteralPath (Join-Path $root '.local\avd\BudaoRun_API34_Play.avd\config.ini'))) {
        & (Join-Path $PSScriptRoot 'New-ExperimentalAVD.ps1') -Sdk $Sdk
        if ($LASTEXITCODE -ne 0) { throw '创建实验设备失败' }
    }
    & (Join-Path $PSScriptRoot 'Build-Diagnostic.ps1') -Sdk $Sdk
    & (Join-Path $PSScriptRoot 'New-StartMenuShortcuts.ps1')
} finally { Pop-Location }
