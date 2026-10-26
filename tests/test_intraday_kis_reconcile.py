"""Synthetic KIS one-day offline reconciliation contracts (no provider calls)."""
from __future__ import annotations

from hashlib import sha256
import csv
import json
from pathlib import Path
import sqlite3

import pytest

from quantlab.intraday_kis_reconcile import (
    KISReconciliationError, inspect_snapshot,
)


SESSION = "2026-10-23"
SYMBOL = "005930"


def make_snapshot(tmp_path: Path, *, missing_label: str | None = None,
                  wrong_day=False, tamper_page=False) -> Path:
    folder = tmp_path / f"KIS_{SYMBOL}_{SESSION}"
    (folder/"raw").mkdir(parents=True)
    minutes = []
    for m in range(9*60, 15*60+20):
        label = f"{m//60:02d}{m%60:02d}00"
        if label == missing_label:
            continue
        minutes.append({
            "stck_bsop_date": "20261022" if wrong_day else "20261023",
            "stck_cntg_hour": label, "stck_oprc": "100",
            "stck_hgpr": "102", "stck_lwpr": "99",
            "stck_prpr": "101", "cntg_vol": "10",
        })
    minutes.append({
        "stck_bsop_date": "20261023", "stck_cntg_hour": "153000",
        "stck_oprc": "105", "stck_hgpr": "105", "stck_lwpr": "105",
        "stck_prpr": "105", "cntg_vol": "777",
    })
    blob = (json.dumps(minutes, ensure_ascii=False, sort_keys=True)+"\n").encode()
    (folder/"minute_observations_unverified.json").write_bytes(blob)
    raw = json.dumps({"output2": minutes}).encode()
    (folder/"raw"/"page_000.json").write_bytes(
        raw + (b"tamper" if tamper_page else b"")
    )
    audit = {
        "status": "KIS_ONE_DAY_MINUTE_PROVENANCE_ONLY_NO_5MIN_BACKTEST",
        "symbol": SYMBOL, "session": SESSION, "raw_pages": 1,
        "raw_sha256": [sha256(raw).hexdigest()],
        "observations_sha256": sha256(blob).hexdigest(),
    }
    (folder/"audit.json").write_text(json.dumps(audit))
    return folder


def daily_db(tmp_path: Path, *, close=104.0, volume=4444.0) -> Path:
    path = tmp_path/"daily.sqlite3"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE long_bars (market TEXT, ticker TEXT, session_date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, first_raw_sha256 TEXT)")
        c.execute("CREATE TABLE raw_history (raw_sha256 TEXT, provider TEXT)")
        c.execute("INSERT INTO raw_history VALUES (?,?)", ("x"*64, "naver_unofficial_siseJson"))
        c.execute(
            "INSERT INTO long_bars VALUES (?,?,?,?,?,?,?,?,?)",
            ("KRX",SYMBOL,SESSION,100,102,99,close,volume,"x"*64),
        )
    return path


def test_exact_76_bars_and_auction_isolation_and_differences(tmp_path):
    snapshot = make_snapshot(tmp_path)
    db = daily_db(tmp_path)
    result = inspect_snapshot(
        snapshot=snapshot, daily_db=db, out_dir=tmp_path/"derived"
    )
    dest = Path(result["output"])
    audit = result["audit"]
    with (dest/"candles_5m_CANDIDATE_ONLY.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 76
    assert rows[0]["timestamp"] == SESSION+"T09:00:00+09:00"
    assert rows[-1]["timestamp"] == SESSION+"T15:15:00+09:00"
    assert int(rows[0]["volume"]) == 50
    assert int(rows[-1]["volume"]) == 50
    assert audit["daily"]["continuous_volume"] == 3800
    assert audit["daily"]["closing_auction_volume"] == 777
    assert audit["daily"]["candidate_total_volume"] == 4577
    assert audit["daily"]["closing_auction_price"] == 105
    assert audit["daily"]["continuous_last_price"] == 101
    assert audit["reference_reconciliation"]["status"] == "SOURCE_DISAGREEMENT_HOLD"
    assert audit["requested_vendor_venue_code"] == "J"
    assert audit["requested_vendor_venue"] == "KRX"
    assert audit["scope_is_kis_krx_regular_closing_auction_only"] is True
    assert audit["nxt_or_extended_hours_rows_in_snapshot"] is False
    assert audit["daily"]["krx_1530_regular_close"] == 105
    assert audit["daily"]["krx_1530_regular_volume_candidate"] == 4577
    reconciliation = audit["reference_reconciliation"]
    assert reconciliation["comparison_is_like_for_like"] is False
    assert reconciliation["candidate_venue"] == "KRX"
    assert reconciliation["candidate_price_reference"] == "KRX_15_30_CLOSING_AUCTION"
    assert reconciliation["reference_venue_time_scope"] == (
        "UNVERIFIED_KRX_NXT_EXTENDED_HOURS_MIX_POSSIBLE")
    assert reconciliation["difference_attribution"] == "NOT_DETERMINABLE_FROM_TWO_TOTALS_ALONE"
    assert reconciliation["volume_delta_cannot_be_called_nxt_volume"] is True
    assert audit["reference_reconciliation"]["reference_provider"] == "naver_unofficial_siseJson"
    assert audit["reference_reconciliation"]["differences"]["close"]["delta"] == 1
    assert audit["reference_reconciliation"]["differences"]["volume"]["delta"] == 133
    assert audit["usable_for_backtesting"] is False
    assert audit["usable_for_paper_or_live"] is False
    assert sha256((dest/"candles_5m_CANDIDATE_ONLY.csv").read_bytes()).hexdigest() == audit["candidate_csv_sha256"]
    assert json.loads((dest/"audit.json").read_text()) == audit
    with pytest.raises(KISReconciliationError, match="already exists"):
        inspect_snapshot(snapshot=snapshot, daily_db=db, out_dir=tmp_path/"derived")


@pytest.mark.parametrize("broken", [
    {"missing_label": "112300"},
    {"wrong_day": True},
    {"tamper_page": True},
])
def test_refuse_missing_cross_session_and_raw_page_tamper(tmp_path, broken):
    folder = make_snapshot(tmp_path, **broken)
    with pytest.raises(KISReconciliationError):
        inspect_snapshot(snapshot=folder, out_dir=tmp_path/"derived")
    assert not (tmp_path/"derived"/f"KRX_5M_CANDIDATE_{SYMBOL}_{SESSION}").exists()


def test_missing_official_daily_record_keeps_backtest_blocked(tmp_path):
    folder = make_snapshot(tmp_path)
    result = inspect_snapshot(
        snapshot=folder, out_dir=tmp_path/"derived", daily_db=tmp_path/"no.db",
    )
    assert result["audit"]["reference_reconciliation"]["status"] == "DAILY_REFERENCE_MISSING"
    assert result["audit"]["usable_for_backtesting"] is False


def test_no_time_offset_or_future_minute_and_no_fake_zero_fill(tmp_path):
    folder = make_snapshot(tmp_path)
    obsfile=folder/"minute_observations_unverified.json"
    auditfile=folder/"audit.json"
    observations=json.loads(obsfile.read_text())
    observations[0]["stck_cntg_hour"]="153100"
    blob=(json.dumps(observations)+"\n").encode()
    obsfile.write_bytes(blob)
    audit=json.loads(auditfile.read_text())
    audit["observations_sha256"]=sha256(blob).hexdigest()
    auditfile.write_text(json.dumps(audit))
    with pytest.raises(KISReconciliationError, match="Unexpected/missing"):
        inspect_snapshot(snapshot=folder, out_dir=tmp_path/"derived")


def test_equal_legacy_daily_values_do_not_prove_venue_parity(tmp_path):
    snapshot = make_snapshot(tmp_path)
    db = daily_db(tmp_path, close=105.0, volume=4577.0)
    # The synthetic 15:30 auction (105) is also the full-day high.
    with sqlite3.connect(db) as c:
        c.execute("UPDATE long_bars SET high=105 WHERE ticker=?", (SYMBOL,))
    result = inspect_snapshot(
        snapshot=snapshot, daily_db=db, out_dir=tmp_path/"derived",
    )
    rec = result["audit"]["reference_reconciliation"]
    assert rec["differences"] == {}
    assert rec["status"] == "MATCHED_UNVERIFIED"
    assert rec["comparison_is_like_for_like"] is False
    assert rec["approved"] is False
    assert result["audit"]["usable_for_backtesting"] is False
