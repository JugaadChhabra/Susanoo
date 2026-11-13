param([ValidateSet('up','stop','status','logs')][string]$Action = 'status')
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/paths.ps1"
if (-not (Test-Path -LiteralPath "$SimSource/docker-compose.prod.yml")) { throw 'Clone the pinned upstream Sim source or set SIM_SOURCE_DIR.' }
$ComposeArgs = @('--env-file', "$StateRoot/.env", '-p', 'susanoo-local-sim', '-f', "$SimSource/docker-compose.prod.yml", '-f', "$PSScriptRoot/compose.local.yml")
switch ($Action) {
    'up' { & docker compose @ComposeArgs up -d }
    'stop' { & docker compose @ComposeArgs stop }
    'status' { & docker compose @ComposeArgs ps }
    'logs' { & docker compose @ComposeArgs logs --tail 60 }
}
exit $LASTEXITCODE
