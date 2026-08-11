[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$RepoRoot
)

$resolvedRoot = (Resolve-Path -LiteralPath $RepoRoot -ErrorAction Stop).Path
$toolPath = Join-Path $resolvedRoot "tools\functional_uat_kr_trade.py"
if (-not (Test-Path -LiteralPath $toolPath -PathType Leaf)) {
    throw "V2 snapshot detection tool is unavailable."
}

$uv = Get-Command uv -ErrorAction Stop
Push-Location -LiteralPath $resolvedRoot
try {
    # The scheduled task calls only the explicit detector.  It cannot approve,
    # build, activate a snapshot, or start a service.
    & $uv.Source run --locked --no-sync --group dev python tools/functional_uat_kr_trade.py `
        detect-v2-source-changes --confirm-network
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    Pop-Location
}
