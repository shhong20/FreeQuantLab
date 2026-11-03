<#
Read-only Windows TaskScheduler state at 08:50 / 09:10 KST.
Scheduled by FreeQuantLab-WSL-Warm-Receipt after the existing 08:40/09:05
Ubuntu warm task. No WSL calls, no broker, no price request.
A slot receipt PROVES ONLY the task scheduler's last-run status at that time.
#>
[CmdletBinding()]
param([ValidateSet("Audit","Capture")][string]$Mode="Audit")
$ErrorActionPreference="Stop"
$taskName="FreeQuantLab-WSL-PreOpen-Warm"
$now=[DateTimeOffset]::Now
$today=$now.Date
$time=$now.ToString("HH:mm")
$slot=$null
$startTime=$null
if ($time -ge "08:50" -and $time -le "08:54") {
    $slot="08:40"
    $startTime=$today.AddHours(8).AddMinutes(39)
} elseif ($time -ge "09:10" -and $time -le "09:14") {
    $slot="09:05"
    $startTime=$today.AddHours(9).AddMinutes(4)
}
$task=Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
$info=Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction Stop
$exe=Join-Path $env:WINDIR "System32\wsl.exe"
$expectedArgs="--distribution Ubuntu --exec /usr/bin/true"
$identityMatches=$false
try {
    $registered=(New-Object System.Security.Principal.NTAccount([string]$task.Principal.UserId)).Translate([System.Security.Principal.SecurityIdentifier]).Value
    $current=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $identityMatches=($registered -eq $current)
} catch {
    $identityMatches=$false
}
$triggerTimes=@($task.Triggers | ForEach-Object {
    try {([datetime]::Parse($_.StartBoundary)).ToString("HH:mm")}
    catch {"INVALID"}
} | Sort-Object)
$configMatches=(
    $task.Actions.Count -eq 1 -and
    $task.Triggers.Count -eq 2 -and
    [string]$task.Actions[0].Execute -ieq $exe -and
    [string]$task.Actions[0].Arguments -eq $expectedArgs -and
    ($triggerTimes -join ",") -eq "08:40,09:05" -and
    @($task.Triggers | Where-Object { $_.CimClass.CimClassName -ne "MSFT_TaskDailyTrigger" -or $_.DaysInterval -ne 1 }).Count -eq 0 -and
    [string]$task.Principal.LogonType -eq "Interactive" -and
    [string]$task.Principal.RunLevel -eq "Limited" -and
    $identityMatches -eq $true -and
    $task.Settings.WakeToRun -eq $false -and
    $task.Settings.StartWhenAvailable -eq $true -and
    $task.Settings.Enabled -eq $true
)
$last=$info.LastRunTime
$lastUtc=if ($last.Year -gt 2000) { $last.ToUniversalTime().ToString("o") } else {$null}
$observedWithinSlot=$false
if ($slot -ne $null -and $last.Year -gt 2000) {
    $observedWithinSlot=($last -ge $startTime -and $last -le $now.LocalDateTime)
}
$successful=($slot -ne $null -and $configMatches -and
             $observedWithinSlot -and $info.LastTaskResult -eq 0)
$result=[ordered]@{
    schema="FREEQUANTLAB_WINDOWS_WARM_RUN_RECEIPT_V1_20261103"
    task_name=$taskName
    audit_time_utc=$now.UtcDateTime.ToString("o")
    kst_date=$now.ToString("yyyy-MM-dd")
    scheduled_warm_slot_kst=$slot
    evidence_window_valid=($slot -ne $null)
    existing_warm_config_match=[bool]$configMatches
    warm_last_run_time_utc=$lastUtc
    warm_last_task_result=[int64]$info.LastTaskResult
    warm_executed_in_allowed_slot=[bool]$observedWithinSlot
    successful_windows_warm_task_execution=[bool]$successful
    actual_wsl_remained_online_after_execution_proven=$false
    actual_cold_boot_or_sleep_resume_proven=$false
    created_historical_paper_orders=0
    broker_orders=0
    wrote_stock_or_crypto_prices=$false
    mode=$Mode
}
if ($Mode -eq "Audit") {
    [pscustomobject]$result | ConvertTo-Json -Compress
    exit $(if ($configMatches) {0} else {2})
}
# Outside these exact local windows a catch-up/manual run may NOT assert
# 08:40/09:05 success or manufacture a retroactive receipt.
if ($slot -eq $null) {
    [pscustomobject]$result | ConvertTo-Json -Compress
    exit 2
}
$root=Join-Path $env:LOCALAPPDATA "FreeQuantLab\preopen_warm_audit"
$null=New-Item -Path $root -ItemType Directory -Force
$stamp=$now.UtcDateTime.ToString("yyyyMMddTHHmmssfffffffZ")
$path=Join-Path $root ("windowswarm-"+$stamp+"-"+$slot.Replace(":","")+".json")
$payload=([pscustomobject]$result | ConvertTo-Json -Depth 5 -Compress)+"`n"
$bytes=(New-Object System.Text.UTF8Encoding($false)).GetBytes($payload)
$sha=[BitConverter]::ToString(([System.Security.Cryptography.SHA256]::Create()).ComputeHash($bytes)).Replace("-","").ToLowerInvariant()
$file=[System.IO.File]::Open($path,[System.IO.FileMode]::CreateNew,[System.IO.FileAccess]::Write,[System.IO.FileShare]::None)
try { $file.Write($bytes,0,$bytes.Length) }
finally {$file.Close()}
$pin=$path+".sha256"
$pinBytes=[System.Text.Encoding]::ASCII.GetBytes($sha+"`n")
$hashFile=[System.IO.File]::Open($pin,[System.IO.FileMode]::CreateNew,[System.IO.FileAccess]::Write,[System.IO.FileShare]::None)
try {$hashFile.Write($pinBytes,0,$pinBytes.Length)}
finally {$hashFile.Close()}
[pscustomobject]@{Status=$(if ($successful) {"WINDOWS_WARM_TASK_SUCCESS_EVIDENCE"} else {"WINDOWS_WARM_TASK_UNVERIFIED"});FileName=[System.IO.Path]::GetFileName($path);Sha256=$sha;Slot=$slot;NoBrokerOrders=$true} | ConvertTo-Json -Compress
exit $(if ($successful){0}else{2})
