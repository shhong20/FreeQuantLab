"""Every unresolved stock gap retains immutable, independently checked provenance."""
from __future__ import annotations

from hashlib import sha256
import json
import sqlite3

import pytest

from quantlab import stock_gap_provenance as g
from test_stock_long_history import fixture,run_history,synthetic_naver


def test_vendor_price_gap_audit_is_readonly_and_flags_quarantined_days(tmp_path):
    original,history=fixture(tmp_path)
    def changed(url,**kwargs):
        r=synthetic_naver(url,**kwargs)
        d=__import__("ast").literal_eval(r.content.decode())
        d[-1][4]+=0.5
        r.content=repr(d).encode()
        return r
    run_history(original,history,naver_getter=changed)
    before=history.read_bytes()  # stable fixture, no WAL after capture
    report=g.inspect(observations=original,history=history,since="2026-10-19")
    assert report["status"]=="VENDOR_HISTORY_GAP_PROVENANCE_UNRESOLVED_NO_TRADING"
    assert report["missing_original_observed_days_in_long_history"]==3
    assert report["counts_by_latest_classification"]=={"MATERIAL_OHLC_PRICE_CONFLICT":3}
    assert all(r["session"]=="2026-10-21" and
               r["material_price_conflict_unresolved"] and
               r["vendor_observation_variant_count"]==1
               for r in report["gaps"])
    assert report["broker_orders"]==0
    assert report["confirmed_historical_strategy_returns"] is None
    assert report["paper_or_live_trading_eligible"] is False
    assert history.read_bytes()==before
    assert g._kind((100,101,99,100,5),(100,101,99,100,9))=="VOLUME_ONLY_REVISION"


def test_adjustment_like_and_us_precision_are_only_diagnostics():
    base=(88805.,90120.,85790.,86525.,29860349.)
    latest=(88678.,89991.,85668.,86401.,31056428.)
    assert g._kind(base,latest)==(
        "COHERENT_OHLC_SCALE_SHIFT_POSSIBLE_ADJUSTMENT_NOT_VERIFIED")
    assert g._kind((344.93499755859375,348.3599853515625,342.2799987792969,
                    348.2200012207031,46845411.),
                   (344.94000244140625,348.3599853515625,342.2799987792969,
                    348.2200012207031,46898900.))==(
        "SMALL_OHLC_ROUNDING_DIFFERENCE_NOT_VERIFIED")
    assert g._kind((532.7000122070312,533.5960083007812,520.2999877929688,
                    521.0800170898438,21246740.),
                   (533.1400146484375,533.5999755859375,520.2999877929688,
                    521.0800170898438,21266600.))=="MATERIAL_OHLC_PRICE_CONFLICT"


def test_tampered_vendor_conflict_sha_fails_closed(tmp_path):
    original,history=fixture(tmp_path)
    def changed(url,**kwargs):
        r=synthetic_naver(url,**kwargs)
        d=__import__("ast").literal_eval(r.content.decode())
        d[-1][4]+=0.5
        r.content=repr(d).encode()
        return r
    run_history(original,history,naver_getter=changed)
    with sqlite3.connect(history) as con:
        con.execute("UPDATE raw_history SET raw=? WHERE ticker=?",
                    (b"corrupt", "005930"))
    with pytest.raises(ValueError,match="Invalid immutable"):
        g.inspect(observations=original,history=history)


def test_immutable_report_and_hash_permissions(tmp_path,monkeypatch):
    original,history=fixture(tmp_path)
    run_history(original,history)
    root=tmp_path/"reports"
    out=root/"gap.json"
    rc=g.main(["--observations",str(original),"--history",str(history),
               "--since","2026-10-19","--report",str(out)])
    assert rc==0
    assert out.exists()
    assert out.stat().st_mode&0o777==0o600
    assert (root/"gap.json.sha256").stat().st_mode&0o777==0o600
    assert sha256(out.read_bytes()).hexdigest()==(
        root/"gap.json.sha256").read_text().strip()
    with pytest.raises(ValueError,match="already exists"):
        g.main(["--observations",str(original),"--history",str(history),
                "--since","2026-10-19","--report",str(out)])
    assert json.loads(out.read_text())["no_modification_to_immutable_stock_databases"]


def test_unrelated_mutating_database_path_is_refused(tmp_path):
    original,history=fixture(tmp_path)
    with pytest.raises(ValueError,match="Separate stock daily"):
        g.inspect(observations=original,history=tmp_path/"shadow.db")
