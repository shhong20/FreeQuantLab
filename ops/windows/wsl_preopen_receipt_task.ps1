<#
Install or audit a second, read-only Windows Task Scheduler task.
Does NOT change FreeQuantLab-WSL-PreOpen-Warm, login or resume tasks.
SourcePath is the trusted WSL repo file (UNC) at one-time install only.
The installed PowerShell payload lives under Windows LOCALAPPDATA (not WSL UNC)
so it works even before Ubuntu starts.
#>
[CmdletBinding()]
param(
    [ValidateSet("Audit","Install")][string]$Mode="Audit",
    [string]$SourcePath=""
)
$ErrorActionPreference="Stop"
$name="FreeQuantLab-WSL-Warm-Receipt"
$sourceName="wsl_preopen_warm_capture.ps1"
$targetDir=Join-Path $env:LOCALAPPDATA "FreeQuantLab\ops"
$targetFile=Join-Path $targetDir $sourceName
$exe=Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$expectedArgs="-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$targetFile`" -Mode Capture"
$currentUser=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$schedule=Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue

if ($Mode -eq "Install" -and $null -eq $schedule) {
    if (-not (Test-Path -LiteralPath $SourcePath -PathType Leaf)) {
        throw "A trusted source script file is required for install"
    }
    # Copy the reviewed Git source to Windows LOCALAPPDATA once; neither
    # overwrite an old payload nor alter existing Windows scheduled tasks.
    $null=New-Item -ItemType Directory -Force -Path $targetDir
    if (Test-Path -LiteralPath $targetFile) {
        if ((Get-FileHash -LiteralPath $targetFile -Algorithm SHA256).Hash -ne
            (Get-FileHash -LiteralPath $SourcePath -Algorithm SHA256).Hash) {
            throw "Installed Windows audit script differs from reviewed source"
        }
    } else {
        Copy-Item -LiteralPath $SourcePath -Destination $targetFile -ErrorAction Stop
    }
    $action=New-ScheduledTaskAction -Execute $exe -Argument $expectedArgs
    $trigger=@(
        (New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.AddHours(8).AddMinutes(50))),
        (New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.AddHours(9).AddMinutes(10)))
    )
    $settings=New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew
    $principal=New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
    if ($null -ne (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)) {
        throw "Audit task appeared concurrently: refusing to overwrite"
    }
    $null=Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Record the existing Windows WSL warmup task result without editing it; no broker/orders/power changes."
    $schedule=Get-ScheduledTask -TaskName $name -ErrorAction Stop
}
if ($null -eq $schedule) {
    [pscustomobject]@{Status="WINDOWS_WARM_RECEIPT_TASK_NOT_INSTALLED";NoOrders=$true} |
        ConvertTo-Json -Compress
    exit 2
}
$identity=$false
try {
    $sid=(New-Object System.Security.Principal.NTAccount([string]$schedule.Principal.UserId)).Translate([System.Security.Principal.SecurityIdentifier]).Value
    $identity=($sid -eq [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value)
} catch { $identity=$false }
$times=@($schedule.Triggers | ForEach-Object {
    try {([datetime]::Parse($_.StartBoundary)).ToString("HH:mm")} catch {"INVALID"}
} | Sort-Object)
$sourceHash=$null
if (Test-Path -LiteralPath $targetFile -PathType Leaf) {
    $sourceHash=(Get-FileHash -LiteralPath $targetFile -Algorithm SHA256).Hash.ToLowerInvariant()
}
if ($SourcePath -ne "") {
    if (-not (Test-Path -LiteralPath $SourcePath -PathType Leaf)) {
        throw "Cannot compare reviewed source"
    }
    $reviewedHash=(Get-FileHash -LiteralPath $SourcePath -Algorithm SHA256).Hash.ToLowerInvariant()
} else {
    $reviewedHash=$sourceHash
}
$valid=(
    $schedule.Actions.Count -eq 1 -and $schedule.Triggers.Count -eq 2 -and
    [string]$schedule.Actions[0].Execute -ieq $exe -and
    [string]$schedule.Actions[0].Arguments -eq $expectedArgs -and
    ($times -join ",") -eq "08:50,09:10" -and
    @($schedule.Triggers | Where-Object { $_.CimClass.CimClassName -ne "MSFT_TaskDailyTrigger" -or $_.DaysInterval -ne 1 }).Count -eq 0 -and
    [string]$schedule.Principal.LogonType -eq "Interactive" -and
    [string]$schedule.Principal.RunLevel -eq "Limited" -and
    $identity -eq $true -and
    $schedule.Settings.WakeToRun -eq $false -and
    $schedule.Settings.StartWhenAvailable -eq $true -and
    $schedule.Settings.Enabled -eq $true -and
    $null -ne $sourceHash -and $sourceHash -eq $reviewedHash
)
[pscustomobject]@{
    Status=$(if ($valid) {"WINDOWS_WARM_RECEIPT_CONFIG_MATCH"} else {"WINDOWS_WARM_RECEIPT_CONFIG_DRIFT"})
    Mode=$Mode
    TaskName=$name
    TaskState=$schedule.State.ToString()
    TriggerTimesLocal=$times
    InstalledPowerShellSha256=$sourceHash
    InstallerAndRuntimeAreWindowsLocal=$true
    ExistingWarmLoginResumeTasksModified=$false
    WindowsWakeEnabled=$false
    ExternallyProvableScheduleTaskExecutionPending=$true
    RealBrokerOrders=0
} | ConvertTo-Json -Compress
exit $(if ($valid){0}else{2})
