$ErrorActionPreference = 'Stop'
$env:ANDROID_HOME = "$env:LOCALAPPDATA\Android\Sdk"
$studio = 'E:\Android\AndroidStudio-2026.2.1.8\android-studio\bin\studio64.exe'
if (-not (Test-Path -LiteralPath $studio)) { throw 'Android Studio 未安装' }
# 用户主动打开IDE，使用正常交互窗口；不改变全局Java/SDK环境变量。
Start-Process -FilePath $studio -WindowStyle Normal
