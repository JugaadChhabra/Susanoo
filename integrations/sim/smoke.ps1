$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/paths.ps1"
Push-Location $PSScriptRoot
try {
    & $Python "$PSScriptRoot/smoke.py"
    if ($LASTEXITCODE -ne 0) { throw 'Local integration smoke failed; see smoke-report.json and the private journal.' }
} finally { Pop-Location }
