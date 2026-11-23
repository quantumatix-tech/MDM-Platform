param(
    [string]$Config = 'config/oracle_e2e_acceptance.yaml',
    [string]$SqlPlus,
    [switch]$PreflightOnly,
    [switch]$TargetCatalogGrantsOnly
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    $launcherArgs = @('tests/e2e/oracle/live_launcher.py', '--config', $Config)
    if ($SqlPlus) {
        $launcherArgs += @('--sqlplus', $SqlPlus)
    }
    if ($PreflightOnly) {
        $launcherArgs += '--preflight-only'
    }
    if ($TargetCatalogGrantsOnly) {
        $launcherArgs += '--target-catalog-grants-only'
    }
    python @launcherArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Oracle LIVE E2E stopped with exit code $LASTEXITCODE. Review the stage output before retrying."
    }
}
finally {
    Pop-Location
}
