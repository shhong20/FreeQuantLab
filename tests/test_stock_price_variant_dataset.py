"""Versioned stock-price variants: sidecar is never eligible for trading."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from quantlab import stock_price_variant_dataset as v
from quantlab.stock_etf_distribution_study import study
from test_stock_etf_distribution_study import _gap


def _fixtures(tmp_path,monkeypatch):
    root=tmp_path/"stock_daily"
    root.mkdir()
    obs=root/"observations.sqlite3"
    hist=root/"history.sqlite3"
    obs.write_bytes(b"initial stock observations immutable")
    hist.write_bytes(b"initial independent long history immutable")
    gap=_gap()
    digest="a"*64
    src=study(gap,digest)
    study_dir=tmp_path/"pinned_study"
    study_dir.mkdir()
    raw=(json.dumps(src,sort_keys=True,indent=2)+"\n").encode()
    (study_dir/"study.json").write_bytes(raw)
    (study_dir/"study.sha256").write_text(sha256(raw).hexdigest()+"\n")
    gap_path=tmp_path/"pinned_gap.json"
    gap_path.write_text("no real stock source mutations")
    monkeypatch.setattr(v,"_checked_gap_report",lambda gap_path_,o,h:(gap,digest))
    kwargs={"study_dir":study_dir,"gap_report":gap_path,
            "observations":obs,"history":hist,"output_dir":tmp_path/"dataset"}
    return kwargs,src,gap


def test_build_is_versioned_readonly_and_independently_sha_verified(tmp_path,monkeypatch):
    args,src,_=_fixtures(tmp_path,monkeypatch)
    a=args["observations"].read_bytes()
    b=args["history"].read_bytes()
    manifest=v.build(**args)
    assert manifest["schema_version"]==v.VERSION
    assert manifest["status"]==v.STATUS
    assert manifest["etf_sessions"]==4
    assert manifest["etf_ohlc_variant_records"]==12
    assert manifest["us_unresolved_sessions"]==5
    assert manifest["source_immutable_stock_gap_report_sha256"]=="a"*64
    assert manifest["approved_for_model_training"] is False
    assert manifest["approved_for_backtest"] is False
    assert manifest["approved_for_paper_live"] is False
    assert manifest["broker_orders"]==0
    assert manifest["filled_missing_primary_history_days"]==0
    assert args["observations"].read_bytes()==a
    assert args["history"].read_bytes()==b
    assert v.verify(**args)==manifest
    out=args["output_dir"]
    assert out.stat().st_mode&0o777==0o700
    assert all(p.stat().st_mode&0o777==0o600 for p in out.iterdir())
    rows=[json.loads(s) for s in (out/"etf_variants.jsonl").read_text().splitlines()]
    assert len(rows)==4
    assert rows[-1]["session"]=="2026-10-28"
    assert rows[-1]["variants"][0]["ohlc_krw"][-1]==95650.
    assert len(rows[-1]["variants"])==3
    assert [r["variant_id"] for r in rows[-1]["variants"]]==list(v.VARIANTS)
    assert all(r["historical_executable_price_certified"] is False
               for row in rows for r in row["variants"])
    assert all(row["canonical_trade_price_selected"] is False for row in rows)
    assert all(row["eligible_for_training_validation_test"] is False for row in rows)
    assert all(row["eligible_for_backtesting_paper_live"] is False for row in rows)
    us=[json.loads(s) for s in (out/"unresolved_us.jsonl").read_text().splitlines()]
    assert len(us)==5 and len({(r["symbol"],r["session"]) for r in us})==5
    assert all(r["distribution_factor_applied"] is False for r in us)


def test_existing_output_not_rewritten(tmp_path,monkeypatch):
    args,_,_=_fixtures(tmp_path,monkeypatch)
    v.build(**args)
    before={p.name:sha256(p.read_bytes()).hexdigest()
            for p in args["output_dir"].iterdir()}
    with pytest.raises(ValueError,match="exists"):
        v.build(**args)
    assert before=={p.name:sha256(p.read_bytes()).hexdigest()
                   for p in args["output_dir"].iterdir()}


def test_payload_tampering_fails_even_if_source_files_not_changed(tmp_path,monkeypatch):
    args,_,_=_fixtures(tmp_path,monkeypatch)
    v.build(**args)
    path=args["output_dir"]/"etf_variants.jsonl"
    path.write_bytes(path.read_bytes()+b"\n")
    with pytest.raises(ValueError,match="payload differs"):
        v.verify(**args)


def test_rehashed_manifest_with_false_live_approval_rejected(tmp_path,monkeypatch):
    args,_,_=_fixtures(tmp_path,monkeypatch)
    v.build(**args)
    p=args["output_dir"]/"manifest.json"
    tampered=json.loads(p.read_text())
    tampered["approved_for_paper_live"]=True
    payload=(json.dumps(tampered,sort_keys=True,indent=2)+"\n").encode()
    p.write_bytes(payload)
    (args["output_dir"]/"manifest.sha256").write_text(sha256(payload).hexdigest()+"\n")
    with pytest.raises(ValueError,match="manifest or provenance changed"):
        v.verify(**args)


def test_study_rehash_cannot_override_independent_gap_provenance(tmp_path,monkeypatch):
    args,_,_=_fixtures(tmp_path,monkeypatch)
    p=args["study_dir"]/"study.json"
    doc=json.loads(p.read_text())
    doc["broker_orders"]=1
    payload=(json.dumps(doc,sort_keys=True,indent=2)+"\n").encode()
    p.write_bytes(payload)
    (args["study_dir"]/"study.sha256").write_text(sha256(payload).hexdigest()+"\n")
    with pytest.raises(ValueError,match="does not match current"):
        v.build(**args)
    assert not args["output_dir"].exists()


def test_factor_or_historical_price_changed_fails_closed(tmp_path,monkeypatch):
    args,_,_=_fixtures(tmp_path,monkeypatch)
    p=args["study_dir"]/"study.json"
    data=json.loads(p.read_text())
    data["issuer_distribution_evidence"]["hypothetical_multiplier"]=0.99
    payload=(json.dumps(data,sort_keys=True,indent=2)+"\n").encode()
    p.write_bytes(payload)
    (args["study_dir"]/"study.sha256").write_text(sha256(payload).hexdigest()+"\n")
    with pytest.raises(ValueError,match="does not match current"):
        v.build(**args)
    assert not args["output_dir"].exists()


def test_extra_us_gap_and_missing_etf_date_both_disallowed():
    src=study(_gap(),"a"*64)
    e=dict(src)
    e["etf_comparisons"]=src["etf_comparisons"][:3]
    with pytest.raises(ValueError,match="Missing four"):
        v._create_rows(e)
    e=dict(src)
    e["us_unresolved"]=[*src["us_unresolved"],src["us_unresolved"][0]]
    with pytest.raises(ValueError,match="five unique"):
        v._create_rows(e)


def test_no_misleading_canonical_variant_or_positive_eligibility_fields():
    src=study(_gap(),"a"*64)
    etf,us=v._create_rows(src)
    assert all(row["canonical_trade_price_selected"] is False for row in etf+us)
    assert all(row["eligible_for_training_validation_test"] is False
               for row in etf+us)
    assert all(row["eligible_for_backtesting_paper_live"] is False
               for row in etf+us)
    assert all("adjusted_price" not in row for row in etf+us)
    assert len(etf)==4 and len(us)==5
