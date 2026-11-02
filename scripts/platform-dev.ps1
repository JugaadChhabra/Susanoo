param([ValidateSet('up','down','status')][string]$Action = 'up')
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$State = Join-Path $Root '.platform'
New-Item -ItemType Directory -Path $State -Force | Out-Null
$Records = Join-Path $State 'dev-processes.json'
if ($Action -eq 'down') {
    if (Test-Path -LiteralPath $Records) {
        foreach ($Record in (Get-Content -LiteralPath $Records -Raw | ConvertFrom-Json)) {
            $Process = Get-CimInstance Win32_Process -Filter "ProcessId=$($Record.pid)"
            if ($Process -and $Process.CommandLine.Contains($Record.marker)) {
                & taskkill.exe /PID $Record.pid /T /F | Out-Null
            }
        }
        Remove-Item -LiteralPath $Records
    }
    Write-Output 'Stopped only recorded Susanoo development process trees; database and data preserved.'
    exit
}
if ($Action -eq 'status') {
    foreach ($Url in @('http://127.0.0.1:8000/api/v1/health', 'http://localhost:3000/evaluation')) {
        try { Write-Output "$Url $((Invoke-WebRequest -Uri $Url -TimeoutSec 10 -UseBasicParsing).StatusCode)" }
        catch { Write-Output "$Url unavailable" }
    }
    exit
}
if (Test-Path -LiteralPath $Records) { throw 'Recorded processes exist. Use status or down before restarting.' }
if (Get-NetTCPConnection -LocalPort 8000,3000 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Ports 8000/3000 must be available; no existing server will be stopped automatically.'
}
$Uv = (Get-Command uv.exe).Source
$Node = (Get-Command node.exe).Source
$Api = Start-Process -FilePath $Uv -ArgumentList @('run','--no-sync','python','-m','susanoo_api.platform.cli','serve') -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $State 'api.out.log') -RedirectStandardError (Join-Path $State 'api.err.log')
$Worker = Start-Process -FilePath $Uv -ArgumentList @('run','--no-sync','python','-m','susanoo_api.platform.cli','worker') -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $State 'worker.out.log') -RedirectStandardError (Join-Path $State 'worker.err.log')
$Backup = Start-Process -FilePath $Uv -ArgumentList @('run','--no-sync','python','-m','susanoo_api.platform.cli','backup-worker') -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $State 'backup.out.log') -RedirectStandardError (Join-Path $State 'backup.err.log')
$Web = Start-Process -FilePath $Node -ArgumentList @('node_modules/next/dist/bin/next','dev','--hostname','127.0.0.1','--port','3000') -WorkingDirectory (Join-Path $Root 'apps/web') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $State 'web.out.log') -RedirectStandardError (Join-Path $State 'web.err.log')
@(@{pid=$Api.Id;marker='susanoo_api.platform.cli'},@{pid=$Worker.Id;marker='susanoo_api.platform.cli'},@{pid=$Backup.Id;marker='susanoo_api.platform.cli'},@{pid=$Web.Id;marker='node_modules/next/dist/bin/next'}) | ConvertTo-Json | Set-Content -LiteralPath $Records
Write-Output 'Started loopback-only API, internal worker and web. http://localhost:3000/evaluation'
