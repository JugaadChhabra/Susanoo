param([ValidateSet('up','stop','status')][string]$Action = 'status')
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/paths.ps1"
$RecordPath = "$StateRoot/.private/worker-process.json"
$Marker = "$PSScriptRoot/platform_worker.py"
$Current = $null
if (Test-Path -LiteralPath $RecordPath) {
    $Record = Get-Content -LiteralPath $RecordPath -Raw | ConvertFrom-Json
    $Candidate = Get-CimInstance Win32_Process -Filter "ProcessId=$($Record.pid)"
    if ($Candidate -and $Candidate.CommandLine.Contains($Marker)) { $Current = $Candidate }
}
if ($Action -eq 'status') {
    if ($Current) { Write-Output "Local Sim worker running, PID $($Current.ProcessId)" }
    else { Write-Output 'Local Sim worker stopped.' }
    exit
}
if ($Action -eq 'stop') {
    if ($Current) {
        Set-Content -LiteralPath "$StateRoot/.private/worker.stop" -Value 'Stop after the current assignment.'
        Write-Output 'Drain requested. The current assignment finishes before the worker exits.'
    } else { Write-Output 'Worker already stopped.' }
    exit
}
if ($Current) { Write-Output 'Worker already running.'; exit }
if (Test-Path -LiteralPath "$StateRoot/.private/worker.stop") {
    Remove-Item -LiteralPath "$StateRoot/.private/worker.stop"
}
& $Python "$PSScriptRoot/check-ready.py"
if ($LASTEXITCODE -ne 0) { throw 'Local runner configuration is not ready.' }
$Process = Start-Process -FilePath $Python -ArgumentList @("`"$Marker`"") -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput "$StateRoot/.private/worker.out.log" -RedirectStandardError "$StateRoot/.private/worker.err.log"
@{pid=$Process.Id} | ConvertTo-Json | Set-Content -LiteralPath $RecordPath
Write-Output "Started local Sim worker, PID $($Process.Id)."
