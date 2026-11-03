<#
.SYNOPSIS
  Audit or explicitly install two Windows daily WSL pre-open warm starts.
.DESCRIPTION
  Existing login and resume tasks are NEVER touched. This user-scoped task
  starts the Ubuntu WSL distro at 08:40 and 09:05 local Windows time, if
  Windows is awake with an interactive user token. It does not wake a sleeping
  computer, turn on a powered-off computer, call brokerage APIs, or backfill
  past paper trades. Audit is read-only by default.
#>
[CmdletBinding()]
param(
    [ValidateSet("Audit","Install")]
    [string]$Mode = "Audit"
)
$ErrorActionPreference="Stop"
$taskName="FreeQuantLab-WSL-PreOpen-Warm"
$exe=Join-Path $env:WINDIR "System32\wsl.exe"
$argsExpected="--distribution Ubuntu --exec /usr/bin/true"
$expectedTimes=@("08:40","09:05")
$currentUser=[Security.Principal.WindowsIdentity]::GetCurrent().Name

$task=Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -eq $task -and $Mode -eq "Install") {
    $action=New-ScheduledTaskAction -Execute $exe -Argument $argsExpected
    $triggers=@(
        (New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.AddHours(8).AddMinutes(40))),
        (New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.AddHours(9).AddMinutes(5)))
    )
    $settings=New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew
    $principal=New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
    if ($null -ne (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
        throw "Preopen task appeared concurrently; refusing to overwrite"
    }
    $null=Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description "Wake running Windows user WSL distro for upcoming public-market observation; no sleep changes/no paper fills."
    $task=Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
}
if ($null -eq $task) {
    [pscustomobject]@{
        Status="PREOPEN_WSL_TASK_NOT_INSTALLED";TaskName=$taskName
        WindowsWakeEnabled=$false;HistoricalTradesBackfilled=$false
        ExistingWindowsTasksModified=$false
    } | ConvertTo-Json -Compress
    exit 2
}

$times=@()
foreach ($trigger in $task.Triggers) {
    try {
        $boundary=[datetime]::Parse($trigger.StartBoundary)
        $times+= $boundary.ToString("HH:mm")
    } catch {
        $times+= "INVALID"
    }
}
$times=@($times | Sort-Object)
$expectedSorted=@($expectedTimes | Sort-Object)
$valid=(
    $task.Actions.Count -eq 1 -and
    $task.Triggers.Count -eq 2 -and
    [string]$task.Actions[0].Execute -ieq $exe -and
    [string]$task.Actions[0].Arguments -eq $argsExpected -and
    @($task.Triggers | Where-Object { $_.CimClass.CimClassName -ne "MSFT_TaskDailyTrigger" -or $_.DaysInterval -ne 1 }).Count -eq 0 -and
    ($times -join ",") -eq ($expectedSorted -join ",") -and
    [string]$task.Principal.LogonType -eq "Interactive" -and
    [string]$task.Principal.RunLevel -eq "Limited" -and
    [string]$task.Principal.UserId -ieq $currentUser -and
    $task.Settings.WakeToRun -eq $false -and
    $task.Settings.StartWhenAvailable -eq $true -and
    $task.Settings.DisallowStartIfOnBatteries -eq $false -and
    $task.Settings.StopIfGoingOnBatteries -eq $false -and
    $task.Settings.Enabled -eq $true
)
[pscustomobject]@{
    Status=$(if ($valid) {"PREOPEN_WSL_TASK_CONFIG_MATCH"} else {"PREOPEN_WSL_TASK_CONFIG_DRIFT"})
    TaskName=$taskName
    Mode=$Mode
    TaskState=[string]$task.State
    TriggerTimesKST=$times
    UserScopedInteractive=$true
    ExpectedUbuntuDistro="Ubuntu"
    WindowsWakeEnabled=$false
    StartsPoweredOffPC=$false
    RequiresInteractiveSession=$true
    ActualColdBootOrSleepResumptionTested=$false
    HistoricalTradesBackfilled=$false
    ExistingWindowsTasksModified=$false
} | ConvertTo-Json -Compress
if (-not $valid) {exit 2}
