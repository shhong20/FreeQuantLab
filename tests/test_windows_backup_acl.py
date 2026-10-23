"""Static fail-closed contracts for the Windows-only local-backup ACL tool.

Windows provides the authoritative ACL inspection; Linux CI ensures the
versioned script remains scoped, opt-in for changes, and rollback capable.
"""
from pathlib import Path


def _script() -> str:
    return (Path(__file__).resolve().parents[1] /
            "ops/windows/clean_backup_acl.ps1").read_text("utf-8")


def test_ntfs_acl_changes_are_opt_in_and_target_only_clean_backup():
    source = _script()
    assert "[string]$Mode = 'Audit'" in source
    assert "ValidateSet('Audit','Apply')" in source
    assert "$root='D:\\FreeQuantLab-Backups\\clean-90d'" in source
    assert "$parent='D:\\FreeQuantLab-Backups'" in source
    assert "if ($Mode -eq 'Apply' -and $before.Count -gt 0)" in source
    assert "Get-ChildItem -LiteralPath $root -Recurse -Force" in source
    assert "ObjectsChecked" in source


def test_acl_snapshot_before_change_and_rollback():
    source = _script()
    assert "/save $aclBackup /T /C" in source
    assert "if ($LASTEXITCODE -ne 0)" in source
    assert "/restore $aclBackup /C" in source
    assert "HONG" not in source  # dynamic current user only
    assert "/inheritance:d /T /C" in source
    assert "/remove:g '*S-1-5-11' '*S-1-5-32-545' '*S-1-1-0'" in source


def test_acl_audit_does_not_claim_offhost_protection_or_backfill():
    source = _script()
    assert "BackupOnDifferentHost=$false" in source
    assert "LiveCleanLedgerModified=$false" in source
    assert "Authenticated Users|BUILTIN\\\\Users|Everyone" in source
    assert "AreAccessRulesProtected" in source
    for unsafe in ("Remove-Item", "format.com", "powercfg /set", "wsl --shutdown",
                   "git switch", "Stop-Service", "Set-ExecutionPolicy"):
        assert unsafe not in source
