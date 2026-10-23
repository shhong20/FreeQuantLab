"""Static release safety contracts for the Windows-only recovery installer.

The actual Task Scheduler audit is run on Windows; Linux CI verifies that
the bundled provisioning script cannot silently change sleep policy or
overwrite a pre-existing task.
"""
from pathlib import Path


def _script() -> str:
    return (Path(__file__).resolve().parents[1] /
            "ops/windows/wsl_resume_task.ps1").read_text("utf-8")


def test_resume_task_has_explicit_event_and_safe_noop_action():
    script = _script()
    assert "Microsoft-Windows-Power-Troubleshooter" in script
    assert "(EventID=1)" in script
    assert "--exec /usr/bin/true" in script
    assert "PT20S" in script
    assert "InteractiveToken" in script
    assert "LeastPrivilege" in script


def test_resume_task_is_audit_by_default_without_power_mutation():
    script = _script()
    assert '[string]$Mode = "Audit"' in script
    assert 'ValidateSet("Audit", "Install")' in script
    assert "if ($null -eq $task -and $Mode -eq \"Install\")" in script
    assert 'throw "Recovery task appeared concurrently; refusing to overwrite"' in script
    assert "WindowsSleepPrevented = $false" in script
    assert "MarketBackfillPerformed = $false" in script
    for prohibited in ("powercfg /set", "powercfg /change", "Restart-Computer",
                       "Stop-ScheduledTask", "Remove-ScheduledTask", "wsl --shutdown"):
        assert prohibited not in script
