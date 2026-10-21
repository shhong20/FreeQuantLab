"""Deployment contract for pinned, read-only microstructure observer runtime."""
from pathlib import Path

UNIT_DIR=Path(__file__).resolve().parents[1]/"deploy"/"systemd"
RUNTIME="/home/hong/apps/FreeQuantLab-observer-runtime"
STATE="/home/hong/apps/FreeQuantLab/state/microstructure"


def test_collector_and_audit_run_from_stable_pinned_worktree():
    for suffix in ("freequant-microstructure.service",
                   "freequant-microstructure-audit.service"):
        content=(UNIT_DIR/suffix).read_text()
        assert f"WorkingDirectory={RUNTIME}" in content
        assert "FreeQuantLab-microstructure-20261021" not in content
        assert f"ReadWritePaths={STATE}" in content
        assert "NoNewPrivileges=true" in content
        assert "ProtectSystem=strict" in content
        assert "PrivateTmp=true" in content
        assert "ExecStart=" in content
        assert "quantlab.microstructure_observer" in content
        assert "api_key" not in content.lower()
        assert "orders" not in content.lower()
    audit=(UNIT_DIR/"freequant-microstructure-audit.service").read_text()
    assert "--require-fresh-seconds 1800" in audit
    collector=(UNIT_DIR/"freequant-microstructure.service").read_text()
    assert "/usr/bin/flock -n" in collector


def test_timer_registration_stays_independent_and_bounded():
    collector=(UNIT_DIR/"freequant-microstructure.timer").read_text()
    audit=(UNIT_DIR/"freequant-microstructure-audit.timer").read_text()
    assert "OnCalendar=*-*-* *:0/10:30" in collector
    assert "OnCalendar=*-*-* *:5/30:00" in audit
    assert "Persistent=true" in collector and "Persistent=true" in audit
    assert "freequant-shadow.service" not in collector+audit
    assert "freequant-qlib-lab.service" not in collector+audit
