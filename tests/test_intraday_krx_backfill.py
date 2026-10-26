"""KRX 60-session backfill: calendar, rate limits, resume and CLI regression."""
from __future__ import annotations

from datetime import date
from hashlib import sha256
import json
from pathlib import Path

import pytest

import quantlab.intraday_krx_backfill as backfill
from quantlab.intraday_kis_cohort_capture import plan as kis_plan, run as kis_run
from quantlab.intraday_kis_venues import capture, VenueProbeError


class Response:
    def __init__(self, body):
        self.content=json.dumps(body).encode("utf-8")
    def raise_for_status(self):
        return None


def get(url, **kw):
    day=kw["params"].get("FID_INPUT_DATE_1","20261023")
    if url.endswith("inquire-daily-itemchartprice"):
        return Response({"rt_cd":"0","output2":[{
            "stck_bsop_date":day,"stck_clpr":"100","acml_vol":"150",
        }]})
    if not url.endswith("inquire-time-dailychartprice"):
        raise AssertionError("Unexpected KIS endpoint")
    return Response({"rt_cd":"0","output2":[{
        "stck_bsop_date":day,"stck_cntg_hour":"090000",
        "stck_oprc":"100","stck_hgpr":"100",
        "stck_lwpr":"100","stck_prpr":"100","cntg_vol":"100",
    }]})


def build_plan(tmp_path, *, count=60, with_existing=False):
    root=tmp_path/"sources"
    if with_existing:
        for ticker in ("005930","000660"):
            capture(symbol=ticker,session="2026-10-23",market="J",
                    out_dir=root,key="LOCAL_FAKE_KEY",
                    secret="LOCAL_FAKE_SECRET",token="LOCAL_FAKE_TOKEN",
                    getter=get)
    folder=tmp_path/"planning"
    p=backfill.create(plan_dir=folder,source_root=root,
                      end_session="2026-10-23",session_count=count,
                      symbols=("005930","000660"),today=date(2026,10,26))
    return folder,root,p


def test_real_xkrx_calendar_60_sessions_12_batches_and_provenance(tmp_path):
    folder,root,p=build_plan(tmp_path,with_existing=True)
    assert p["schema"]==backfill.SCHEMA
    assert p["target_sessions_per_symbol"]==60
    assert p["already_held_stock_day_snapshots"]==2
    assert p["pending_stock_day_snapshots"]==118
    assert p["sessions"][-3:]==[
        "2026-10-21","2026-10-22","2026-10-23"
    ]
    assert "2026-10-09" not in p["sessions"]  # Hangeul Day, XKRX closed
    assert "2026-10-05" not in p["sessions"]
    assert len(p["batches"])==12
    assert all(len(batch["sessions"])==5 for batch in p["batches"])
    assert all(len(batch["tasks"])==10 for batch in p["batches"])
    assert all(x["market"]=="J" for b in p["batches"] for x in b["tasks"])
    plan_file=folder/"plan.json"
    assert sha256(plan_file.read_bytes()).hexdigest()==(
        folder/"plan.sha256").read_text().strip()
    assert backfill.load(folder)==p
    b=backfill.run_batch(plan_dir=folder,batch_index=11)
    assert b["status"]=="PREVIEW_ONLY"
    assert b["verified_existing"]==2
    assert b["pending"]==8
    assert not (folder/"progress").exists()
    assert not (root/"batch_progress.json").exists()
    with pytest.raises(VenueProbeError,match="already exists"):
        build_plan(tmp_path)


def test_completed_batch_skips_credentials_and_requests(tmp_path):
    folder,root,p=build_plan(tmp_path,count=1,with_existing=True)
    r=backfill.run_batch(plan_dir=folder,batch_index=0,execute=True)
    assert r["status"]=="EXISTING_VERIFIED"
    assert r["pending"]==0
    assert not (root/"batch_progress.json").exists()


def test_real_capture_run_mocks_http_and_keeps_progress_out_of_raw_root(
    tmp_path,monkeypatch
):
    folder,root,p=build_plan(tmp_path,count=1)
    original=kis_run
    invocations=[]
    def local_run(**kw):
        invocations.append(kw["execute"])
        return original(**(kw | {"between_tasks_seconds": 0.0}),
                        getter=get,sleep=lambda _:None,clock=lambda:0.0)
    monkeypatch.setattr(backfill,"capture_run",local_run)
    # One trading day x two stocks, J only. The fake response exercises
    # real KIS page capture and hash verification, but no credentials/net.
    out=backfill.run_batch(plan_dir=folder,batch_index=0,
                           execute=True,key="FAKE_KEY",
                           secret="FAKE_SECRET",token="FAKE_TOKEN")
    assert out["status"]=="CAPTURES_COMPLETED"
    assert out["verified_existing_or_new"]==2
    assert out["http_get_requests"]==4
    assert invocations==[False,True]
    assert (folder/"progress"/"batch_00"/"batch_progress.json").is_file()
    assert not (root/"batch_progress.json").exists()
    assert backfill.run_batch(plan_dir=folder,batch_index=0)["pending"]==0
    assert backfill.run_batch(plan_dir=folder,batch_index=0,execute=True)[
        "status"]=="EXISTING_VERIFIED"
    report=(folder/"progress"/"batch_00"/"batch_progress.json").read_text()
    for secret in ("FAKE_KEY","FAKE_SECRET","FAKE_TOKEN"):
        assert secret not in report


def test_tampered_saved_source_and_manifest_block_before_creds(tmp_path):
    folder,root,p=build_plan(tmp_path,count=1,with_existing=True)
    original=root/"KIS_J_EXT_005930_2026-10-23"/"raw"/"minute_000.json"
    original.write_bytes(original.read_bytes()+b"TAMPERED")
    with pytest.raises(VenueProbeError,match="hash"):
        backfill.run_batch(plan_dir=folder,batch_index=0)
    old_plan=(folder/"plan.json").read_bytes()
    (folder/"plan.json").write_bytes(old_plan+b"modified")
    with pytest.raises(VenueProbeError,match="SHA mismatch"):
        backfill.load(folder)


def test_invalid_day_market_bound_and_path(tmp_path):
    with pytest.raises(VenueProbeError,match="past"):
        backfill.create(plan_dir=tmp_path/"a",source_root=tmp_path/"src",
                        end_session="2026-10-26",session_count=60,
                        today=date(2026,10,26))
    with pytest.raises(VenueProbeError,match="session"):
        backfill.create(plan_dir=tmp_path/"a",source_root=tmp_path/"src",
                        end_session="2026-10-24",session_count=60,
                        today=date(2026,10,26))
    with pytest.raises(VenueProbeError):
        backfill.create(plan_dir=tmp_path/"a",source_root=tmp_path/"src",
                        end_session="2026-10-23",session_count=121,
                        today=date(2026,10,26))
    with pytest.raises(VenueProbeError):
        backfill.create(plan_dir=tmp_path/"a",source_root=tmp_path/"src",
                        end_session="2026-10-23",session_count=1,
                        symbols=("../bad",),today=date(2026,10,26))
    folder,root,p=build_plan(tmp_path,count=1)
    with pytest.raises(VenueProbeError,match="index"):
        backfill.run_batch(plan_dir=folder,batch_index=12)
    assert len(kis_plan(symbols="005930,000660",
                        sessions="2026-10-23",markets="J"))==2


def test_cli_init_and_non_auth_preview_reuses_pinned_manifest(tmp_path,capsys):
    folder=tmp_path/"plan"
    source=tmp_path/"source"
    assert backfill.main([
        "--init","--source-root",str(source),"--plan-dir",str(folder),
        "--end-session","2026-10-23","--session-count","60"
    ])==0
    output=json.loads(capsys.readouterr().out)
    assert output["status"]=="IMMUTABLE_PLAN_CREATED_NO_API_CALL"
    assert output["batch_count"]==12
    assert output["existing_verified"]==0
    assert output["pending"]==120
    assert backfill.main(["--batch","11","--plan-dir",str(folder)])==0
    x=json.loads(capsys.readouterr().out)
    assert x["pending"]==10
    assert x["status"]=="PREVIEW_ONLY"
    assert not source.exists()
