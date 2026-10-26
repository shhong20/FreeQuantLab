"""No-network KIS rolling overtime daily source and non-reconciling bridge."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import pytest

from quantlab.intraday_kis_overtime_daily import (
    SOURCE, URL, TR_ID, parse_response, capture_one, _read_snapshot,
    bridge, main,
)
from quantlab.intraday_kis_venues import capture, VenueProbeError

NOW = datetime(2026, 10, 26, 4, tzinfo=timezone.utc)
DAYS = ("2026-10-21", "2026-10-22", "2026-10-23")
TICKER = "005930"


class Response:
    def __init__(self, data):
        self.content = json.dumps(data).encode("utf-8")

    def raise_for_status(self):
        return None


def response_overtime(*, include_all=True, amount="632089"):
    rows = [
        {"stck_bsop_date":"20261021", "ovtm_untp_vol":amount,
         "acml_vol":"17269100"},
        {"stck_bsop_date":"20261022", "ovtm_untp_vol":"111",
         "acml_vol":"10000"},
    ]
    if include_all:
        rows.append({
            "stck_bsop_date":"20261023", "ovtm_untp_vol":"179083",
            "acml_vol":"16145972",
        })
    return {"rt_cd":"0", "output1":{"stck_prpr":"LIVE_VOLATILE"},
            "output2":rows}


def offline_capture(tmp_path, *, include_all=True, amount="632089"):
    calls=[]
    def getter(url, **kw):
        calls.append((url, kw))
        return Response(response_overtime(include_all=include_all, amount=amount))
    result=capture_one(symbol=TICKER, sessions=DAYS, out_dir=tmp_path,
                       key="PRIVATE_KEY",secret="PRIVATE_SECRET",
                       token="PRIVATE_BEARER",getter=getter)
    return result,calls


def make_krx_daily(root: Path, *, daily_total=1000, minute_total=800):
    def getter(url, **kw):
        if url.endswith("inquire-time-dailychartprice"):
            return Response({"rt_cd":"0", "output2":[{
                "stck_bsop_date":"20261023",
                "stck_cntg_hour":"090000",
                "stck_oprc":"100", "stck_hgpr":"100",
                "stck_lwpr":"100", "stck_prpr":"100",
                "cntg_vol":str(minute_total),
            }]})
        return Response({"rt_cd":"0", "output2":[{
            "stck_bsop_date":"20261023", "stck_clpr":"100",
            "acml_vol":str(daily_total),
        }]})
    capture(symbol=TICKER,session="2026-10-23",market="J",
            out_dir=root,key="DUMMY",secret="DUMMY",token="DUMMY",
            getter=getter,now=NOW)


def test_official_params_read_only_and_date_matching_with_hashes(tmp_path):
    result,calls=offline_capture(tmp_path)
    report=result["audit"]
    assert len(calls)==1
    path,kw=calls[0]
    assert path==URL
    assert kw["params"]=={
        "FID_COND_MRKT_DIV_CODE":"J",
        "FID_INPUT_ISCD":TICKER,
    }
    assert kw["headers"]["tr_id"]==TR_ID
    assert kw["headers"]["authorization"]=="Bearer PRIVATE_BEARER"
    assert report["endpoint_scope"]=="ROLLING_LAST_30_NO_HISTORICAL_DATE_QUERY"
    assert len(report["matched_requested_days"])==3
    assert report["requested_days_not_in_response"]==[]
    assert report["usable_to_reconcile_krx_daily_minute_gap"] is False
    assert report["orders_enabled"] is False
    folder=Path(result["output"])
    assert sha256((folder/"raw.json").read_bytes()).hexdigest()==report["raw_sha256"]
    assert _read_snapshot(tmp_path,TICKER,DAYS)["raw_sha256"]==report["raw_sha256"]
    serialized=(folder/"audit.json").read_text()
    assert "PRIVATE_KEY" not in serialized
    assert "PRIVATE_SECRET" not in serialized
    assert "PRIVATE_BEARER" not in serialized
    with pytest.raises(VenueProbeError,match="already exists"):
        offline_capture(tmp_path)


def test_recent_window_absence_is_not_zero_and_no_wrong_day_assignment(tmp_path):
    result,calls=offline_capture(tmp_path,include_all=False)
    report=result["audit"]
    assert report["requested_days_not_in_response"]==["2026-10-23"]
    assert len(report["matched_requested_days"])==2
    assert not any(x["session"]=="2026-10-23" for x in report["matched_requested_days"])


@pytest.mark.parametrize("value", ["bad", "-99", "1.1", None])
def test_unverified_volume_is_not_coerced_to_a_fabricated_number(value):
    raw=json.dumps({"rt_cd":"0","output2":[
        {"stck_bsop_date":"20261023","ovtm_untp_vol":value}
    ]}).encode()
    report=parse_response(raw,symbol=TICKER,sessions=("2026-10-23",))
    row=report["matched_requested_days"][0]
    assert row["overtime_single_price_volume_reported"] is None
    assert row["missing_or_invalid_volume_field"] is True


def test_duplicate_date_and_provider_errors_fail_closed():
    with pytest.raises(VenueProbeError,match="Duplicate"):
        parse_response(json.dumps({"rt_cd":"0","output2":[
            {"stck_bsop_date":"20261023","ovtm_untp_vol":"10"},
            {"stck_bsop_date":"20261023","ovtm_untp_vol":"10"},
        ]}).encode(),symbol=TICKER,sessions=("2026-10-23",))
    with pytest.raises(VenueProbeError,match="msg_cd=UNKNOWN") as e:
        parse_response(json.dumps({"rt_cd":"1",
            "msg_cd":"ATTACK_PRIVATE_BEARER!",
            "msg1":"PRIVATE_SECRET_DO_NOT_PRINT"}).encode(),
            symbol=TICKER,sessions=("2026-10-23",))
    assert "PRIVATE_SECRET" not in str(e.value)


def test_tampered_raw_snapshot_rejected(tmp_path):
    result,_=offline_capture(tmp_path)
    p=Path(result["output"])/"raw.json"
    p.write_bytes(p.read_bytes()+b"BAD")
    with pytest.raises(VenueProbeError,match="SHA mismatch"):
        _read_snapshot(tmp_path,TICKER,DAYS)


def test_bridge_values_even_when_equal_are_unverified(tmp_path):
    overtime=tmp_path/"overtime"
    cohort=tmp_path/"cohort"
    offline_capture(overtime)
    make_krx_daily(cohort,daily_total=1000,minute_total=800)
    o=bridge(overtime_root=overtime,cohort_root=cohort,
             symbols=(TICKER,),sessions=("2026-10-23",),
             out_dir=tmp_path/"bridge")
    r=o["audit"]["cases"][0]
    assert r["krx_daily_minus_observed_minute_volume"]==200
    assert r["overtime_reported_untp_volume"]==179083
    assert r["comparison_difference_gap_minus_untp_volume"]==-178883
    assert r["same_venue_and_time_scope_proven"] is False
    assert o["audit"]["all_krx_volume_gaps_attributed"] is False
    assert o["audit"]["usable_for_backtesting"] is False
    file=Path(o["output"])
    assert sha256(file.read_bytes()).hexdigest()==(
        file.parent/"bridge.sha256").read_text().strip()
    with pytest.raises(VenueProbeError,match="exists"):
        bridge(overtime_root=overtime,cohort_root=cohort,
               symbols=(TICKER,),sessions=("2026-10-23",),
               out_dir=tmp_path/"bridge")


def test_default_cli_is_no_auth_no_files(tmp_path,capsys):
    path=tmp_path/"new"
    assert main(["--out-dir",str(path)])==0
    report=json.loads(capsys.readouterr().out)
    assert report["status"]=="DRY_RUN_NO_API_CALL"
    assert report["requests"]==2
    assert report["historical_date_in_request"] is False
    assert not path.exists()


def test_equal_venue_gap_and_overtime_volume_still_cannot_certify_scope(tmp_path):
    overtime=tmp_path/"overtime"
    cohort=tmp_path/"cohort"
    offline_capture(overtime)
    # Numerically equal values do not prove market/phase identity.
    make_krx_daily(cohort,daily_total=179883,minute_total=800)
    result=bridge(overtime_root=overtime,cohort_root=cohort,
                  symbols=(TICKER,),sessions=("2026-10-23",),
                  out_dir=tmp_path/"compare")["audit"]
    record=result["cases"][0]
    assert record["comparison_difference_gap_minus_untp_volume"] == 0
    assert record["same_venue_and_time_scope_proven"] is False
    assert record["disposition"] == "NO_AUTOMATIC_VOLUME_REPAIR_OR_BACKTEST"
    assert result["all_krx_volume_gaps_attributed"] is False
    assert result["usable_for_backtesting"] is False
