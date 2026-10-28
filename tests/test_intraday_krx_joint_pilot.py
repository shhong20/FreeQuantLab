"""One-day KRX quote/tick/minute pilot: market-day gating and idempotence."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import pytest

from quantlab.intraday_kis_venues import VenueProbeError
from quantlab.intraday_krx_joint_pilot import (
    PILOT_SESSION, WINDOWS, preflight, execute, _load_local_kis_credentials,
)

WHEN=datetime.fromisoformat("2026-10-29T09:43:00+09:00")


def test_krx_calendar_preflight_and_clock_rejection():
    x=preflight(session=PILOT_SESSION,window="morning",now=WHEN)
    assert x["KRX_calendar_session"] is True
    assert x["expected_market_http_gets"]==12
    assert x["orders_enabled"] is False
    with pytest.raises(VenueProbeError,match="market session/window"):
        preflight(session=PILOT_SESSION,window="morning",
                  now=datetime.fromisoformat("2026-10-29T15:44:00+09:00"))
    with pytest.raises(VenueProbeError,match="Only"):
        preflight(session="2026-10-30",window="morning",now=WHEN)
    with pytest.raises(VenueProbeError,match="Only"):
        preflight(session=PILOT_SESSION,window="nope",now=WHEN)


def fixture_capture(directory:Path,session:str,window:str):
    folder=directory/f"{session}_{window}_raw"
    folder.mkdir(parents=True,mode=0o700)
    directory.chmod(0o700)
    report={"status":"READ_ONLY_KRX_J_NEAR_TIME_L2_TICKS_1M_NO_FILLS",
            "rounds":2,"symbols":["005930","000660"],
            "real_orders":0,"usable_for_backtesting":False}
    raw=(json.dumps(report,sort_keys=True)+"\n").encode()
    (folder/"manifest.json").write_bytes(raw)
    (folder/"manifest.sha256").write_text(sha256(raw).hexdigest()+"\n")
    return folder


def fixture_audit(directory:Path,session:str,window:str):
    folder=directory/f"{session}_{window}_audit"
    folder.mkdir(parents=True)
    src=directory/f"{session}_{window}_raw"/"manifest.json"
    body={"status":"KRX_NEAR_TIME_THREE_SOURCE_CONSISTENCY_NOT_FILL_VALIDATION",
          "broker_orders":0,"sample_triplets":4,
          "matched_tick_minute_row_count":15,
          "manifest_sha256":sha256(src.read_bytes()).hexdigest()}
    raw=(json.dumps(body,sort_keys=True)+"\n").encode()
    (folder/"joint_comparison.json").write_bytes(raw)
    (folder/"joint_comparison.sha256").write_text(sha256(raw).hexdigest()+"\n")
    return body


def test_research_pilot_captures_once_and_no_repeat_token(tmp_path,monkeypatch):
    counts={"tokens":0,"captures":0,"audits":0}
    def token():
        counts["tokens"]+=1
        return "SYNTHETIC-BEARER"
    def collector(*,out_dir,**kwargs):
        counts["captures"]+=1
        assert kwargs["count"]==2
        assert kwargs["token"]=="SYNTHETIC-BEARER"
        assert kwargs["key"]=="fixture-key" and kwargs["secret"]=="fixture-secret"
        fixture_capture(Path(out_dir).parent,PILOT_SESSION,"morning")
    def auditor(*,source_dir,out_dir):
        counts["audits"]+=1
        return fixture_audit(Path(out_dir).parent,PILOT_SESSION,"morning")
    monkeypatch.setenv("KIS_APP_KEY","fixture-key")
    monkeypatch.setenv("KIS_APP_SECRET","fixture-secret")
    kwargs=dict(session=PILOT_SESSION,window="morning",root=tmp_path/"pilot",
                clock=lambda:WHEN,token_supplier=token,
                collector=collector,auditor=auditor)
    first=execute(**kwargs)
    assert first["status"]=="AUDITED_RESEARCH_ONLY"
    assert first["sample_triplets"]==4
    assert first["broker_orders"]==0
    again=execute(**kwargs)
    assert again["status"]=="ALREADY_AUDITED"
    assert counts=={"tokens":1,"captures":1,"audits":1}
    assert (tmp_path/"pilot"/".pilot.lock").stat().st_mode & 0o077==0


def test_incomplete_source_cannot_be_treated_as_success(tmp_path,monkeypatch):
    root=tmp_path/"pilot"
    source=root/f"{PILOT_SESSION}_morning_raw"
    source.mkdir(parents=True,mode=0o700)
    root.chmod(0o700)
    monkeypatch.setenv("KIS_APP_KEY","fixture-key")
    monkeypatch.setenv("KIS_APP_SECRET","fixture-secret")
    with pytest.raises(VenueProbeError,match="Incomplete"):
        execute(session=PILOT_SESSION,window="morning",root=root,
                clock=lambda:WHEN,
                token_supplier=lambda:pytest.fail("must not authenticate"),
                collector=lambda **_:pytest.fail("no recapture"),
                auditor=lambda **_:pytest.fail("no audit"))


def test_existing_raw_is_audited_not_recaptured(tmp_path,monkeypatch):
    root=tmp_path/"pilot"
    fixture_capture(root,PILOT_SESSION,"morning")
    monkeypatch.setenv("KIS_APP_KEY","fixture-key")
    monkeypatch.setenv("KIS_APP_SECRET","fixture-secret")
    result=execute(session=PILOT_SESSION,window="morning",root=root,
                   clock=lambda:WHEN,
                   token_supplier=lambda:pytest.fail("should use old source"),
                   collector=lambda **_:pytest.fail("never recapture"),
                   auditor=lambda **_:fixture_audit(root,PILOT_SESSION,"morning"))
    assert result["status"]=="AUDITED_RESEARCH_ONLY"


def test_early_or_late_job_never_issues_token(tmp_path):
    with pytest.raises(VenueProbeError,match="market session/window"):
        execute(session=PILOT_SESSION,window="morning",root=tmp_path/"pilot",
                clock=lambda:datetime.fromisoformat("2026-10-29T08:00:00+09:00"),
                token_supplier=lambda:pytest.fail("not allowed before market"))
    assert not (tmp_path/"pilot").exists()


def test_bad_existing_audit_is_never_accepted(tmp_path,monkeypatch):
    root=tmp_path/"pilot"
    fixture_capture(root,PILOT_SESSION,"morning")
    path=root/f"{PILOT_SESSION}_morning_audit"
    fixture_audit(root,PILOT_SESSION,"morning")
    blob=json.loads((path/"joint_comparison.json").read_text())
    blob["broker_orders"]=1
    raw=(json.dumps(blob,sort_keys=True)+"\n").encode()
    (path/"joint_comparison.json").write_bytes(raw)
    (path/"joint_comparison.sha256").write_text(sha256(raw).hexdigest()+"\n")
    with pytest.raises(VenueProbeError,match="cannot be trusted"):
        execute(session=PILOT_SESSION,window="morning",root=root,clock=lambda:WHEN,
                token_supplier=lambda:pytest.fail("never authenticate"))


def test_credential_reader_does_not_execute_shell(tmp_path,monkeypatch):
    # Fixed private config path is under Path.home(), mocked in tests.
    import quantlab.intraday_krx_joint_pilot as pilot
    monkeypatch.setattr(pilot.Path,"home",lambda:tmp_path)
    config=tmp_path/".config"/"freequantlab"
    config.mkdir(parents=True)
    (config/"kis.env").write_text(
        "KIS_APP_KEY=fixture-key\nKIS_APP_SECRET='fixture-secret'\n")
    (config/"kis.env").chmod(0o600)
    monkeypatch.delenv("KIS_APP_KEY",raising=False)
    monkeypatch.delenv("KIS_APP_SECRET",raising=False)
    _load_local_kis_credentials()
    assert os.environ["KIS_APP_KEY"]=="fixture-key"
    assert os.environ["KIS_APP_SECRET"]=="fixture-secret"
