"""Pilot failures are sanitized, immutable, and never retried as real fills."""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json

import pytest

from quantlab.intraday_kis_venues import VenueProbeError
from quantlab.intraday_krx_pilot_failure import read_failure, write_failure
from quantlab.intraday_krx_joint_pilot import execute, _get_result
from quantlab.intraday_krx_joint_pilot_review import summarize

NOW=datetime.fromisoformat("2026-10-29T09:43:00+09:00")


def _args(tmp_path,**extra):
    return dict(root=tmp_path/"pilot",window="morning",session="2026-10-29",
                clock=lambda:NOW,**extra)


def test_auth_failure_receipt_sanitizes_credentials_and_never_retries(tmp_path,monkeypatch):
    root=tmp_path/"pilot"
    monkeypatch.setenv("KIS_APP_KEY","very-private-appkey")
    monkeypatch.setenv("KIS_APP_SECRET","very-private-appsecret")
    calls=[]
    def token_supplier():
        calls.append("auth")
        raise VenueProbeError("topsecret-access-token from private provider response")
    with pytest.raises(VenueProbeError,match="topsecret"):
        execute(**_args(tmp_path,token_supplier=token_supplier,
              collector=lambda **kw:pytest.fail("collector must not execute")))
    failure=read_failure(root=root,window="morning")
    assert failure["failed_phase"]=="AUTH"
    assert failure["observed_market_responses_verified"]==0
    assert failure["current_request_symbol"] is None
    assert failure["credential_or_bearer_recorded"] is False
    assert failure["orders_sent"]==0
    raw=(root/"2026-10-29_morning_failure.json").read_bytes()
    assert b"topsecret" not in raw
    assert b"very-private" not in raw
    assert sha256(raw).hexdigest()==(
        root/"2026-10-29_morning_failure.sha256").read_text().strip()
    with pytest.raises(VenueProbeError,match="do not retry"):
        execute(**_args(tmp_path,token_supplier=lambda:pytest.fail("double OAuth")))
    assert calls==["auth"]
    row=summarize(root=root,write=False,now=NOW)["window_results"][0]
    assert row["status"]=="MARKET_CAPTURE_FAILED_RECORDED"
    assert row["failed_phase"]=="AUTH"


def test_partial_market_failure_counts_only_validated_responses(tmp_path,monkeypatch):
    monkeypatch.setenv("KIS_APP_KEY","test-key")
    monkeypatch.setenv("KIS_APP_SECRET","test-secret")
    def partial_collector(*,on_observation,**kw):
        on_observation("STARTED",0,"005930","book")
        on_observation("VERIFIED",0,"005930","book")
        on_observation("STARTED",0,"005930","ticks")
        on_observation("VERIFIED",0,"005930","ticks")
        on_observation("STARTED",0,"005930","minute")
        raise VenueProbeError("Provider body TOKEN=SENSITIVE")
    with pytest.raises(VenueProbeError,match="SENSITIVE"):
        execute(**_args(tmp_path,token_supplier=lambda:"synthetic-bearer",
              collector=partial_collector))
    root=tmp_path/"pilot"
    failure=read_failure(root=root,window="morning")
    assert failure["failed_phase"]=="MARKET_READ"
    assert failure["observed_market_responses_verified"]==2
    assert failure["current_request_symbol"]=="005930"
    assert failure["current_request_kind"]=="minute"
    assert failure["current_request_round"]==0
    assert "SENSITIVE" not in (
        root/"2026-10-29_morning_failure.json").read_text()
    result=summarize(root=root,write=False,now=NOW)
    assert result["verified_windows"]==0
    assert result["historical_strategy_returns"] is None


def test_damaged_or_forged_failure_evidence_never_certifies_capture(tmp_path):
    root=tmp_path/"pilot"
    root.mkdir(mode=0o700)
    write_failure(root=root,window="midday",phase="MARKET_READ",observed_count=4,
                  current_kind="book",current_symbol="000660",
                  current_round=0,
                  now=datetime.fromisoformat("2026-10-29T12:35:00+09:00"))
    path=root/"2026-10-29_midday_failure.json"
    with pytest.raises(VenueProbeError,match="already present"):
        write_failure(root=root,window="midday",phase="MARKET_READ",
                      observed_count=4,now=NOW)
    path.write_bytes(path.read_bytes()+b"extra")
    with pytest.raises(VenueProbeError,match="checksum"):
        read_failure(root=root,window="midday")
    result=summarize(root=root,write=False,now=NOW)
    assert result["verified_windows"]==0
    assert result["window_results"][1]["status"]=="EVIDENCE_FAILED_VERIFICATION"


def test_forged_undocumented_fields_and_future_receipt_rejected(tmp_path):
    root=tmp_path/"pilot"
    root.mkdir(mode=0o700)
    with pytest.raises(VenueProbeError,match="pilot session"):
        write_failure(root=root,window="morning",phase="AUTH",observed_count=0,
            now=datetime.fromisoformat("2026-10-30T10:00:00+09:00"))
    write_failure(root=root,window="morning",phase="AUTH",observed_count=0,now=NOW)
    p=root/"2026-10-29_morning_failure.json"
    doc=json.loads(p.read_bytes())
    doc["oauth_token"]="FORGED_SECRET"
    raw=(json.dumps(doc,sort_keys=True,indent=2)+"\n").encode()
    p.write_bytes(raw)
    (root/"2026-10-29_morning_failure.sha256").write_text(sha256(raw).hexdigest()+"\n")
    with pytest.raises(VenueProbeError,match="Unsafe"):
        read_failure(root=root,window="morning")


def test_completed_market_source_audit_failure_preserves_raw_not_recaptured(tmp_path,monkeypatch):
    from quantlab.intraday_krx_joint_pilot import _get_result
    from tests.test_intraday_krx_joint_pilot import fixture_capture
    monkeypatch.setenv("KIS_APP_KEY","test-key")
    monkeypatch.setenv("KIS_APP_SECRET","test-secret")
    def collector(*,out_dir,on_observation,**kw):
        for i in range(2):
            for sy in ("005930","000660"):
                for kind in ("book","ticks","minute"):
                    on_observation("STARTED",i,sy,kind)
                    on_observation("VERIFIED",i,sy,kind)
        fixture_capture(tmp_path/"pilot","2026-10-29","morning")
    def bad_audit(**kw):
        raise VenueProbeError("AUDIT confidential MARKET BODY")
    with pytest.raises(VenueProbeError,match="AUDIT"):
        execute(**_args(tmp_path,token_supplier=lambda:"synthetic-bearer",
                       collector=collector,auditor=bad_audit))
    root=tmp_path/"pilot"
    receipt=read_failure(root=root,window="morning")
    assert receipt["failed_phase"]=="AUDIT"
    assert receipt["observed_market_responses_verified"]==12
    assert _get_result(root,"2026-10-29","morning")["capture_exists"] is True
    assert "MARKET BODY" not in (
        root/"2026-10-29_morning_failure.json").read_text()
