param([string]$Sdk = "$env:LOCALAPPDATA\Android\Sdk")
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$java = 'C:\Program Files\Eclipse Adoptium\jdk-17.0.20.8-hotspot'
if (-not (Test-Path -LiteralPath "$java\bin\javac.exe")) { throw '找不到既有 JDK17；未修改 JAVA_HOME' }
$env:JAVA_HOME = $java
$tools = Join-Path $Sdk 'build-tools\35.0.1'
$androidJar = Join-Path $Sdk 'platforms\android-34\android.jar'
$build = Join-Path $root ('.local\build\diagnostic-' + [guid]::NewGuid().ToString('N'))
$classes = Join-Path $build 'classes'
$dex = Join-Path $build 'dex'
New-Item -ItemType Directory -Path $classes,$dex -Force | Out-Null
function Check-Exit { if ($LASTEXITCODE -ne 0) { throw "构建命令失败：$LASTEXITCODE" } }
$source = Join-Path $root 'android\diagnostic\src\com\edurun\diagnostic\DiagnosticActivity.java'
$motionSource = Join-Path $root 'android\diagnostic\src\com\edurun\diagnostic\RawMotionProbe.java'
$serviceSource = Join-Path $root 'android\diagnostic\src\com\edurun\diagnostic\RawMotionService.java'
& "$java\bin\javac.exe" -source 8 -target 8 -encoding UTF-8 -classpath $androidJar -d $classes $source $motionSource $serviceSource
Check-Exit
$jar = Join-Path $build 'classes.jar'
& "$java\bin\jar.exe" cf $jar -C $classes .
Check-Exit
& "$tools\d8.bat" --lib $androidJar --min-api 26 --output $dex $jar
Check-Exit
$unsigned = Join-Path $build 'unsigned.apk'
& "$tools\aapt2.exe" link -I $androidJar --manifest (Join-Path $root 'android\diagnostic\AndroidManifest.xml') -o $unsigned
Check-Exit
Push-Location $dex
try { & "$tools\aapt.exe" add $unsigned classes.dex; Check-Exit } finally { Pop-Location }
$aligned = Join-Path $build 'aligned.apk'
& "$tools\zipalign.exe" -p 4 $unsigned $aligned
Check-Exit
$keystore = Join-Path $root '.local\build\diagnostic-debug.keystore'
if (-not (Test-Path -LiteralPath $keystore)) {
    & "$java\bin\keytool.exe" -genkeypair -keystore $keystore -storepass android -keypass android -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 3650 -dname 'CN=BudaoRun Local Diagnostic' -noprompt
    Check-Exit
}
$apk = Join-Path $build 'diagnostic.apk'
& "$tools\apksigner.bat" sign --ks $keystore --ks-pass pass:android --key-pass pass:android --out $apk $aligned
Check-Exit
& "$tools\apksigner.bat" verify $apk
Check-Exit
$receipt = Join-Path $root '.local\setup\diagnostic-build.json'
New-Item -ItemType Directory -Path (Split-Path -Parent $receipt) -Force | Out-Null
@{apk=$apk;sha256=(Get-FileHash -LiteralPath $apk -Algorithm SHA256).Hash} | ConvertTo-Json | Set-Content -LiteralPath $receipt -Encoding utf8
Write-Output "Diagnostic built and signature verified: $apk"
