$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$directory = Join-Path ([Environment]::GetFolderPath('Programs')) 'BudaoRun'
New-Item -ItemType Directory -Path $directory -Force | Out-Null
$shell = New-Object -ComObject WScript.Shell
$pwsh = (Get-Command pwsh).Source
foreach($entry in @(
    @{Name='Android Studio';File='Open-AndroidStudio.ps1';Arguments=''},
    @{Name='实验安卓模拟器';File='Start-ExperimentalAVD.ps1';Arguments='-Visible'},
    @{Name='停止实验模拟器';File='Stop-ExperimentalAVD.ps1';Arguments=''}
)) {
    $path = Join-Path $directory ($entry.Name + '.lnk')
    if (Test-Path -LiteralPath $path) { Write-Output "已有入口保留：$($entry.Name)"; continue }
    $shortcut = $shell.CreateShortcut($path)
    $shortcut.TargetPath = $pwsh
    $file = Join-Path $PSScriptRoot $entry.File
    $shortcut.Arguments = '-NoProfile -WindowStyle Hidden -File "' + $file + '" ' + $entry.Arguments
    $shortcut.WorkingDirectory = $root
    $shortcut.Description = 'BudaoRun独立实验环境；不修改旧AVD或真机'
    $shortcut.Save()
    Write-Output "已创建入口：$($entry.Name)"
}
