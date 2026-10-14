param([string]$RuntimeFile = '.local/launcher/current/runtime.active.json')
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$runtime = [IO.Path]::GetFullPath((Join-Path $root $RuntimeFile))
$private = Join-Path $root '.local'
if (-not $runtime.StartsWith($private + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Service runtime must stay in this workspace .local.'
}
if (-not (Test-Path -LiteralPath $runtime -PathType Leaf)) { throw 'Private runtime is missing.' }
if (Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Port 8766 already has a listener; do not start a duplicate service.'
}
$python = Join-Path $root '.venv/Scripts/python.exe'
$bootRequest = Join-Path $private 'launcher/boot-once.json'
if (Test-Path -LiteralPath $bootRequest -PathType Leaf) {
    $request = Get-Content -LiteralPath $bootRequest -Raw | ConvertFrom-Json
    if ($request.request -ne 'open_owned_player' -or $request.expires_epoch -lt [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) {
        throw 'Owned-player boot request is invalid or expired.'
    }
    # Consume only our fixed, one-shot setup request. No record/login files are touched.
    Remove-Item -LiteralPath $bootRequest
    Push-Location $root
    try {
        & $python -c "from pathlib import Path; from budao.mumu import MuMuBridge; b=MuMuBridge(Path('.local/setup/mumu-owned-instance.json'),allow_stopped=True); b.start_owned()"
        if ($LASTEXITCODE -ne 0) { throw 'Owned player startup was not confirmed.' }
    } finally { Pop-Location }
}
$profile = Join-Path $root '.local/profiles/current/profile.native-trial.json'
$output = Join-Path $root '.local/launcher-service.stdout.txt'
$errorLog = Join-Path $root '.local/launcher-service.stderr.txt'
$arguments = @('-m','budao.cli','launcher-service','--profile',('"' + $profile + '"'),
    '--runtime',('"' + $runtime + '"'))
# Only an idle API is started here. Real running still requires the native button.
$process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $root `
    -WindowStyle Hidden -PassThru -RedirectStandardOutput $output -RedirectStandardError $errorLog
$receipt = @{ pid=$process.Id; owned_by_this_task=$true; port=8766; scheduled_service=$true;
    runtime=$runtime; source_commit=(& git -C $root rev-parse HEAD).Trim() }
$receipt | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $private 'setup/launcher-api-process-native-20261014.json')
$process.WaitForExit()
exit $process.ExitCode
