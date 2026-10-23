<#
.SYNOPSIS
  Audit, or explicitly install, the user-scoped Windows resume-to-WSL task.
.DESCRIPTION
  Triggers on Windows System/Power-Troubleshooter event ID 1 after resume.
  Does NOT inhibit sleep, backfill missed quotes, or change existing logon tasks.
  Audit is the default and has no side effects.
.EXAMPLE
  powershell.exe -NoProfile -File .\ops\windows\wsl_resume_task.ps1 -Mode Audit
.EXAMPLE
  powershell.exe -NoProfile -File .\ops\windows\wsl_resume_task.ps1 -Mode Install
#>
[CmdletBinding()]
param(
    [ValidateSet("Audit", "Install")]
    [string]$Mode = "Audit"
)
$ErrorActionPreference = "Stop"
$taskName = "FreeQuantLab-WSL-Resume-Recovery"
$expectedAction = Join-Path $env:WINDIR "System32\wsl.exe"
$expectedArguments = "--exec /usr/bin/true"
$subscription = '<QueryList><Query Id="0" Path="System"><Select Path="System">*[System[Provider[@Name="Microsoft-Windows-Power-Troubleshooter"] and (EventID=1)]]</Select></Query></QueryList>'

$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -eq $task -and $Mode -eq "Install") {
    $userId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $escaped = [System.Security.SecurityElement]::Escape($subscription)
    $taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Restart WSL user session after Windows resume; public data gaps are not recreated.</Description></RegistrationInfo>
  <Triggers>
    <EventTrigger>
      <Enabled>true</Enabled>
      <Subscription>$escaped</Subscription>
      <Delay>PT20S</Delay>
    </EventTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$userId</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT1M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>C:\Windows\System32\wsl.exe</Command>
      <Arguments>--exec /usr/bin/true</Arguments>
    </Exec>
  </Actions>
</Task>
"@
    # Never overwrite a preexisting task; unexpected settings require investigation.
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        throw "Recovery task appeared concurrently; refusing to overwrite"
    }
    $null = Register-ScheduledTask -TaskName $taskName -Xml $taskXml
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
}
if ($null -eq $task) {
    [pscustomobject]@{
        Status = "RESUME_TASK_NOT_INSTALLED"
        TaskName = $taskName
        WindowsSleepPrevented = $false
        MarketBackfillPerformed = $false
    } | ConvertTo-Json -Compress
    exit 2
}

$actualExe = [string]$task.Actions[0].Execute
$actualArguments = [string]$task.Actions[0].Arguments
$actualQuery = [string]$task.Triggers[0].Subscription
$matches = (
    $task.Actions.Count -eq 1 -and
    $task.Triggers.Count -eq 1 -and
    $actualExe -ieq $expectedAction -and
    $actualArguments -eq $expectedArguments -and
    $actualQuery -eq $subscription -and
    [string]$task.Principal.LogonType -eq "Interactive" -and
    [string]$task.Principal.RunLevel -eq "Limited"
)
[pscustomobject]@{
    Status = $(if ($matches) { "RESUME_TASK_CONFIG_MATCH" } else { "RESUME_TASK_CONFIG_DRIFT" })
    TaskName = $taskName
    Mode = $Mode
    TaskState = [string]$task.State
    TaskActions = $task.Actions.Count
    TaskTriggers = $task.Triggers.Count
    WindowsSleepPrevented = $false
    ResumingBeforeInteractiveLogonGuaranteed = $false
    ActualResumeEventTested = $false
    MarketBackfillPerformed = $false
    ExistingLoginTaskChanged = $false
} | ConvertTo-Json -Compress
if (-not $matches) { exit 2 }
