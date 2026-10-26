"""KRX independently sourced trading-phase evidence is never trade-certified."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import pytest

from quantlab.intraday_krx_phase_evidence import PHASES, assess
from quantlab.intraday_kis_venues import VenueProbeError, capture

NOW = datetime(2026, 10, 26, 4, tzinfo=timezone.utc)


class Resp:
    def __init__(self, payload):
        self.content = json.dumps(payload).encode()

    def raise_for_status(self):
        return None


def krx_snapshot(root, *, symbol="005930", session="2026-10-23",
                 daily=1000, minute=800):
    day = session.replace("-", "")
    def get(url, **kwargs):
        if url.endswith("inquire-time-dailychartprice"):
            return Resp({"rt_cd": "0", "output2": [{
                "stck_bsop_date": day, "stck_cntg_hour": "090000",
                "stck_oprc": "100", "stck_hgpr": "100", "stck_lwpr": "100",
                "stck_prpr": "100", "cntg_vol": str(minute)
            }]})
        return Resp({"rt_cd": "0", "output2": [{
            "stck_bsop_date": day, "stck_clpr": "100",
            "acml_vol": str(daily)
        }]})
    capture(symbol=symbol, session=session, market="J", out_dir=root,
            key="SECRET_DUMMY_KEY", secret="SECRET_DUMMY_SECRET",
            token="SECRET_DUMMY_TOKEN", getter=get, now=NOW)


def generate(root, *, invalid_phase=None, quantity=-1,
             tamper=False, traversal=False, duplicate=False):
    root.mkdir(parents=True, exist_ok=True)
    data = b"TEST ORIGINAL VENDOR RECORDS FOR A SINGLE DAY"
    if not traversal:
        file = root/"original_vendor_record.bin"
        file.write_bytes(data)
    rows=[]
    for n, phase in enumerate(PHASES):
        rows.append({
            "symbol": "005930", "session": "2026-10-23",
            "phase": invalid_phase or phase,
            "volume": 50 if quantity < 0 else quantity,
            "provider": "KRX",
            "source_file": "../escape.bin" if traversal else "original_vendor_record.bin",
            "source_sha256": sha256(data).hexdigest(),
            "source_record_id": "vendorRecord123",
        })
    if duplicate:
        rows.append(dict(rows[0]))
    (root/"manifest.json").write_text(json.dumps({
        "format_version": 1, "entries": rows
    }))
    if tamper and not traversal:
        file.write_bytes(data+b"TAMPERED")


def test_without_external_phase_data_creates_honest_six_case_hold(tmp_path):
    source=tmp_path/"source"
    for session in ("2026-10-21","2026-10-22","2026-10-23"):
        for symbol in ("005930","000660"):
            krx_snapshot(source, symbol=symbol,session=session)
    out=assess(cohort_root=source,symbols=("005930","000660"),
               sessions=("2026-10-21","2026-10-22","2026-10-23"),
               out_dir=tmp_path/"out")
    s=out["audit"]
    assert s["compared_krx_cases"] == 6
    assert s["source_record_count"] == 0
    assert all(c["phases_with_evidence"]==0 for c in s["cases"])
    assert all(c["phases_without_evidence"]==list(PHASES) for c in s["cases"])
    assert all(c["unexplained_official_minus_observed"]==200 for c in s["cases"])
    assert all(c["difference_after_four_submitted_phase_totals"] is None
               for c in s["cases"])
    assert s["usable_for_backtesting"] is False
    assert s["no_missing_volume_as_zero"] is True
    p=Path(out["output"])
    assert sha256(p.read_bytes()).hexdigest()==(
        p.parent/"phase_evidence_audit.sha256").read_text().strip()
    assert "SECRET_DUMMY" not in p.read_text()
    with pytest.raises(VenueProbeError, match="already exists"):
        assess(cohort_root=source,symbols=("005930","000660"),
               sessions=("2026-10-21","2026-10-22","2026-10-23"),
               out_dir=tmp_path/"out")


def test_four_equal_volumes_do_not_approve_any_trading(tmp_path):
    root=tmp_path/"sources"
    krx_snapshot(root)
    ev=tmp_path/"vendor_evidence"
    generate(ev)
    s=assess(cohort_root=root, symbols=("005930",),
             sessions=("2026-10-23",), evidence_root=ev,
             out_dir=tmp_path/"audit")["audit"]
    c=s["cases"][0]
    assert s["source_record_count"]==4
    assert c["all_four_candidate_phase_records_present"] is True
    assert c["submitted_phase_volume_sum_partial"] == 200
    assert c["difference_after_four_submitted_phase_totals"] == 0
    assert c["vendor_phase_semantics_independently_certified"] is False
    assert c["arithmetic_match_is_not_proof_of_scope"] is True
    assert c["backtest_allowed"] is False
    assert all(v["source_bytes_integrity_verified"]
               for v in c["evidence_by_trading_phase"].values())
    assert all(not v["provider_authenticity_independently_verified"]
               for v in c["evidence_by_trading_phase"].values())
    assert s["no_bar_synthesis_or_vendor_data_rewrite"] is True


def test_partial_provenance_does_not_compute_full_residual(tmp_path):
    root=tmp_path/"sources"
    krx_snapshot(root)
    ev=tmp_path/"vendor_evidence"
    generate(ev)
    p=ev/"manifest.json"
    obj=json.loads(p.read_text())
    obj["entries"]=obj["entries"][:2]
    p.write_text(json.dumps(obj))
    s=assess(cohort_root=root, symbols=("005930",),
             sessions=("2026-10-23",), evidence_root=ev,
             out_dir=tmp_path/"audit")["audit"]
    c=s["cases"][0]
    assert c["phases_with_evidence"]==2
    assert c["phases_without_evidence"] == list(PHASES[2:])
    assert c["submitted_phase_volume_sum_partial"] == 100
    assert c["difference_after_four_submitted_phase_totals"] is None


@pytest.mark.parametrize("mode", ["tamper","traversal","duplicate","badphase",
                                   "negative","boolvol","badprovider"])
def test_invalid_external_evidence_fail_closed(tmp_path,mode):
    root=tmp_path/"sources"
    krx_snapshot(root)
    ev=tmp_path/"vendor_evidence"
    generate(ev,tamper=mode=="tamper",
             traversal=mode=="traversal",duplicate=mode=="duplicate",
             invalid_phase="MYSTERY_TRADE" if mode=="badphase" else None)
    if mode in ("negative","boolvol","badprovider"):
        p=ev/"manifest.json"
        rows=json.loads(p.read_text())
        rows["entries"][0][
            "volume" if mode in ("negative","boolvol") else "provider"
        ] = (-1 if mode=="negative" else True if mode=="boolvol"
             else "UNKNOWN_LICENSED_PROVIDER")
        p.write_text(json.dumps(rows))
    with pytest.raises(VenueProbeError):
        assess(cohort_root=root,symbols=("005930",),
               sessions=("2026-10-23",),evidence_root=ev,
               out_dir=tmp_path/"out")
    assert not (tmp_path/"out"/"phase_evidence_audit.json").exists()


def test_missing_krx_case_not_in_denominator(tmp_path):
    s=assess(cohort_root=tmp_path/"missing",
             symbols=("005930",),sessions=("2026-10-23",),
             out_dir=tmp_path/"out")["audit"]
    assert s["compared_krx_cases"]==0
    assert len(s["missing_krx_cases"])==1
    assert s["source_record_count"]==0
    assert s["usable_for_paper_or_live"] is False


def test_raw_krx_source_tamper_rejected_before_report(tmp_path):
    root=tmp_path/"sources"
    krx_snapshot(root)
    p=root/"KIS_J_EXT_005930_2026-10-23"/"raw"/"minute_000.json"
    p.write_bytes(p.read_bytes()+b"mutation")
    with pytest.raises(VenueProbeError):
        assess(cohort_root=root,symbols=("005930",),
               sessions=("2026-10-23",),out_dir=tmp_path/"out")
    assert not (tmp_path/"out"/"phase_evidence_audit.json").exists()
