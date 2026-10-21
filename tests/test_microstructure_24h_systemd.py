"""Systemd watchdog checks integrity, freshness and independent 24h rolling coverage."""
from pathlib import Path


def test_audit_service_checks_both_data_validity_and_24h_schedule_coverage():
    root=Path(__file__).resolve().parents[1]/"deploy"/"systemd"
    audit=(root/"freequant-microstructure-audit.service").read_text()
    assert audit.count("ExecStart=")==2
    assert "quantlab.microstructure_observer --audit --require-fresh-seconds 1800" in audit
    assert "quantlab.microstructure_24h_gate --enforce-after-24h" in audit
    assert "FreeQuantLab-observer-runtime" in audit
    assert "NoNewPrivileges=true" in audit
    assert "ProtectSystem=strict" in audit
    assert "freequant-shadow" not in audit
    timer=(root/"freequant-microstructure-audit.timer").read_text()
    assert "OnCalendar=*-*-* *:5/30:00" in timer
    assert "Persistent=true" in timer
