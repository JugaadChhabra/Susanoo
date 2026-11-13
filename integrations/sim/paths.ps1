$RepoRoot = (Resolve-Path "$PSScriptRoot/../..").Path
$StateRoot = if ($env:SUSANOO_SIM_STATE_DIR) { [IO.Path]::GetFullPath($env:SUSANOO_SIM_STATE_DIR) } else { Join-Path $RepoRoot '.platform/sim-local' }
$Python = if ($IsLinux -or $IsMacOS) { Join-Path $RepoRoot '.venv/bin/python' } else { Join-Path $RepoRoot '.venv/Scripts/python.exe' }
$SimSource = if ($env:SIM_SOURCE_DIR) { [IO.Path]::GetFullPath($env:SIM_SOURCE_DIR) } else { [IO.Path]::GetFullPath("$RepoRoot/../sim") }
