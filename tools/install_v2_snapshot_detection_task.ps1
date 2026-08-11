[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [switch]$ConfirmInstall,

    [ValidatePattern("^[A-Za-z0-9][A-Za-z0-9_. -]{2,127}$")]
    [string]$TaskName = "CustomsDataV2GuidanceChangeDetection",

    [ValidateSet("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")]
    [string]$Weekday = "Monday",

    [ValidatePattern("^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
    [string]$Time = "03:00",

    [string]$RepoRoot = (Join-Path $PSScriptRoot "..")
)

if (-not $ConfirmInstall) {
    throw "Scheduled-task installation requires -ConfirmInstall. No task was created."
}

$resolvedRoot = (Resolve-Path -LiteralPath $RepoRoot -ErrorAction Stop).Path
$runnerPath = Join-Path $resolvedRoot "tools\run_v2_snapshot_detection.ps1"
if (-not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) {
    throw "V2 snapshot detection runner is unavailable."
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    throw "A scheduled task with this name already exists. This installer never replaces existing tasks."
}

$quotedRunner = '"' + $runnerPath.Replace('"', '""') + '"'
$quotedRoot = '"' + $resolvedRoot.Replace('"', '""') + '"'
$arguments = "-NoProfile -ExecutionPolicy Bypass -File $quotedRunner -RepoRoot $quotedRoot"
$action = New-ScheduledTaskAction -Execute "$PSHOME\powershell.exe" -Argument $arguments
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Weekday -At $Time

if ($PSCmdlet.ShouldProcess($TaskName, "create weekly V2 source-change detection task")) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Description "Runs only the local official V2 guidance source-change detector." | Out-Null
    Write-Output "Created scheduled task '$TaskName' for $Weekday $Time local time."
}
