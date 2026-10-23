<#
.SYNOPSIS
  Audit or explicitly restrict only the FreeQuantLab clean-backup NTFS subtree.
.DESCRIPTION
  Read-only Audit is default. Apply preserves a timestamped ACL snapshot in
  the current user's LOCALAPPDATA, then removes inherited broad group access
  from the dedicated D:\FreeQuantLab-Backups\clean-90d subtree only.
  This is NOT an offhost backup, ransomware-proof store or WORM archive.
#>
[CmdletBinding()]
param([ValidateSet('Audit','Apply')][string]$Mode = 'Audit')
$ErrorActionPreference='Stop'
$root='D:\FreeQuantLab-Backups\clean-90d'
$parent='D:\FreeQuantLab-Backups'
if (-not (Test-Path -LiteralPath $root -PathType Container)) {
    throw 'Clean backup subtree does not exist'
}
$targets=@((Get-Item -LiteralPath $root))+@(Get-ChildItem -LiteralPath $root -Recurse -Force)
if ($targets.Count -gt 10000) {throw 'Unexpectedly large ACL target; refuse change'}
function Get-ACLViolations($items) {
    @($items | Where-Object {
        $acl=Get-Acl -LiteralPath $_.FullName
        (($_.FullName -eq $root) -and (-not $acl.AreAccessRulesProtected)) -or
        (@($acl.Access | Where-Object {
            $_.AccessControlType -eq 'Allow' -and
            $_.IdentityReference.Value -match 'Authenticated Users|BUILTIN\\Users|Everyone'
        })).Count -gt 0
    })
}
$before=Get-ACLViolations $targets
if ($Mode -eq 'Apply' -and $before.Count -gt 0) {
    $stamp=(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
    $aclBackup=Join-Path $env:LOCALAPPDATA ('FreeQuantLab-clean-90d-ACL-'+$stamp+'.txt')
    if (Test-Path -LiteralPath $aclBackup) {throw 'Existing prechange ACL backup: refusing overwrite'}
    & icacls.exe $root /save $aclBackup /T /C | Out-Null
    if ($LASTEXITCODE -ne 0) {throw 'Unable to preserve original ACL'}
    $ownerGrant=('{0}\{1}:(OI)(CI)F' -f $env:COMPUTERNAME,$env:USERNAME)
    $writeStarted=$false
    try {
        & icacls.exe $root /grant $ownerGrant '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /T /C | Out-Null
        if ($LASTEXITCODE -ne 0) {throw 'Grant for owner/SYSTEM/Administrators failed'}
        $writeStarted=$true
        & icacls.exe $root /inheritance:d /T /C | Out-Null
        if ($LASTEXITCODE -ne 0) {throw 'Disabling inherited NTFS rules failed'}
        & icacls.exe $root /remove:g '*S-1-5-11' '*S-1-5-32-545' '*S-1-1-0' /T /C | Out-Null
        if ($LASTEXITCODE -ne 0) {throw 'Removing broad NTFS group rights failed'}
        $targets=@((Get-Item -LiteralPath $root))+@(Get-ChildItem -LiteralPath $root -Recurse -Force)
        $after=Get-ACLViolations $targets
        if ($after.Count -gt 0) {throw ('Post-check violations '+$after.Count)}
    }
    catch {
        if ($writeStarted) {
            & icacls.exe $parent /restore $aclBackup /C | Out-Null
            if ($LASTEXITCODE -ne 0) {
                Write-Warning 'ACL restore failed: manual repair using saved original ACL required'
            }
        }
        throw
    }
}
$targets=@((Get-Item -LiteralPath $root))+@(Get-ChildItem -LiteralPath $root -Recurse -Force)
$bad=Get-ACLViolations $targets
[pscustomobject]@{
    Status=$(if ($bad.Count) { 'BACKUP_NTFS_ACL_TOO_BROAD' } else { 'BACKUP_NTFS_ACL_RESTRICTED' })
    Mode=$Mode
    Root=$root
    ObjectsChecked=$targets.Count
    BroadRightsViolations=$bad.Count
    BackupOnDifferentHost=$false
    SystemSleepPrevented=$false
    LiveCleanLedgerModified=$false
} | ConvertTo-Json -Compress
if ($bad.Count -gt 0) {exit 2}
