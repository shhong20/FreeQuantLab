"""No-network tests for immutable KIS three-market historical replay audits."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import pytest

from quantlab.intraday_kis_replay_audit import compare_replay
from quantlab.intraday_kis_venues import VenueProbeError, capture


NOW = datetime(2026, 10, 26, 4, tzinfo=timezone.utc)
DATE = "2026-10-23"
SYMBOL = "005930"


class Resp:
    def __init__(self, result):
        self.content = json.dumps(result).encode("utf-8")

    def raise_for_status(self):
        return None


def row(tm, close, vol, high=None, low=None):
    return {
        "stck_bsop_date": "20261023", "stck_cntg_hour": tm,
        "stck_oprc": str(close), "stck_hgpr": str(high or close),
        "stck_lwpr": str(low or close), "stck_prpr": str(close),
        "cntg_vol": str(vol),
    }


def sample(market, *, corrected=False):
    if market == "J":
        return [row("090700", 205000, 102331),
                row("090800", 204000, 157838, 205000, 203500),
                row("195900", 213500, 3211)]
    if market == "NX":
        return [row("090700", 205000, 41781),
                row("090800", 204000, 226919, 205500, 203500),
                row("195900", 213500, 30258)]
    if market == "UN":
        return [row("090700", 205000, 143952),
                row("090800", 204000, 384757, 205500, 203500)
                if corrected else row("090800", 205000, 406),
                row("195900", 213500, 33469)]
    raise AssertionError("Invalid mock market")


def make_source(root, market, *, corrected=False):
    def getter(url, **kw):
        assert kw["params"]["FID_COND_MRKT_DIV_CODE"] == market
        if url.endswith("inquire-time-dailychartprice"):
            return Resp({"rt_cd": "0", "output2": sample(market, corrected=corrected)})
        return Resp({"rt_cd": "0", "output2": [{
            "stck_bsop_date": "20261023", "stck_clpr": "213500",
            "acml_vol": "1000000",
        }]})
    capture(
        symbol=SYMBOL, session=DATE, market=market, out_dir=root,
        key="DUMMY_KEY", secret="DUMMY_SECRET", token="DUMMY_BEARER",
        getter=getter, now=NOW,
    )


def setup_roots(tmp_path, *, corrected=False):
    baseline, replay = tmp_path/"baseline", tmp_path/"repeat"
    for market in ("J", "NX", "UN"):
        make_source(baseline, market)
    make_source(replay, "UN", corrected=corrected)
    return baseline, replay


def test_vendor_anomaly_replayed_unchanged_still_blocked(tmp_path):
    baseline, replay = setup_roots(tmp_path)
    out = compare_replay(
        baseline_root=baseline, replay_root=replay,
        symbol=SYMBOL, session=DATE, markets=("UN",),
        out_dir=tmp_path/"audit",
    )
    result=out["audit"]
    assert result["status"] == "REPLAY_OBSERVATIONS_REPRODUCED"
    un=result["markets"]["UN"]
    assert un["changed_label_count"] == 0
    assert un["raw_page_hashes_identical"] is True
    focus=un["baseline_venue_crosscheck"]
    assert focus["counts"]["baseline_volume_mismatch"] == 2
    assert focus["counts"]["replay_volume_mismatch"] == 2
    assert focus["counts"]["baseline_envelope_mismatch"] == 1
    assert focus["counts"]["replay_envelope_mismatch"] == 1
    assert focus["focus_labels"]["090800"]["replay"]["integrated_minus_venue_volume"] == -384351
    assert focus["focus_labels"]["090800"]["replay"]["integrated_matches_price_envelope"] is False
    assert result["replayed_market_data_proven_correct"] is False
    assert result["usable_for_backtesting"] is False
    path=Path(out["output"])
    digest=sha256((path/"audit.json").read_bytes()).hexdigest()
    assert (path/"audit.sha256").read_text().strip() == digest
    with pytest.raises(VenueProbeError, match="already exists"):
        compare_replay(
            baseline_root=baseline, replay_root=replay, symbol=SYMBOL,
            session=DATE, markets=("UN",), out_dir=tmp_path/"audit",
        )


def test_changed_replay_identifies_exact_source_delta_not_autocorrect(tmp_path):
    baseline,replay=setup_roots(tmp_path,corrected=True)
    out=compare_replay(baseline_root=baseline,replay_root=replay,
                       symbol=SYMBOL,session=DATE,markets=("UN",),
                       out_dir=tmp_path/"audit")
    result=out["audit"]
    assert result["status"] == "REPLAY_OBSERVATIONS_CHANGED_HOLD"
    un=result["markets"]["UN"]
    assert un["changed_label_count"] == 1
    assert un["changed_labels"][0]["clock"] == "090800"
    assert un["changed_labels"][0]["field_changes"]["cntg_vol"] == {
        "baseline": "406", "replay": "384757"
    }
    cross=un["baseline_venue_crosscheck"]
    assert cross["counts"]["replay_volume_mismatch"] == 1
    assert cross["counts"]["replay_envelope_mismatch"] == 0
    assert result["usable_for_paper_or_live"] is False
    # Original immutable source was never replaced.
    original_file = baseline / f"KIS_UN_EXT_{SYMBOL}_{DATE}" / "minute_observations_UNVERIFIED.json"
    assert any(z["cntg_vol"]=="406" for z in json.loads(original_file.read_text()))


@pytest.mark.parametrize("bad", ["baseline_same_root", "missing_replay", "tamper_raw"])
def test_missing_or_tampered_venue_fails_closed(tmp_path, bad):
    baseline,replay=setup_roots(tmp_path)
    if bad == "baseline_same_root":
        replay=baseline
    if bad == "missing_replay":
        (replay / f"KIS_UN_EXT_{SYMBOL}_{DATE}").rename(replay/"renamed")
    if bad == "tamper_raw":
        file = replay/f"KIS_UN_EXT_{SYMBOL}_{DATE}"/"raw"/"minute_000.json"
        file.write_bytes(file.read_bytes()+b"unauthorized-change")
    with pytest.raises(VenueProbeError):
        compare_replay(baseline_root=baseline,replay_root=replay,
                       symbol=SYMBOL,session=DATE,markets=("UN",),
                       out_dir=tmp_path/"audit")
    assert not (tmp_path/"audit"/f"KIS_REPLAY_UN_{SYMBOL}_{DATE}").exists()


def test_without_krx_nxt_baseline_market_comparison_fails_closed(tmp_path):
    baseline,replay=setup_roots(tmp_path)
    (baseline/f"KIS_NX_EXT_{SYMBOL}_{DATE}").rename(baseline/"NXT_unavailable")
    with pytest.raises(VenueProbeError):
        compare_replay(baseline_root=baseline,replay_root=replay,
                       symbol=SYMBOL,session=DATE,markets=("UN",),
                       out_dir=tmp_path/"audit")


def test_replay_multi_market_identity_is_per_venue(tmp_path):
    baseline,replay=setup_roots(tmp_path)
    for market in ("J", "NX"):
        make_source(replay,market)
    out=compare_replay(baseline_root=baseline,replay_root=replay,
                       symbol=SYMBOL,session=DATE,markets=("J","NX","UN"),
                       out_dir=tmp_path/"audit")
    result=out["audit"]
    assert result["status"] == "REPLAY_OBSERVATIONS_REPRODUCED"
    assert set(result["markets"]) == {"J","NX","UN"}
    assert all(result["markets"][m]["changed_label_count"]==0 for m in ("J","NX","UN"))


def _rewrite_mock_vendor_raw(root, *, part, mutate):
    """Test helper: simulate an independently valid second vendor response."""
    folder=root/f"KIS_UN_EXT_{SYMBOL}_{DATE}"
    file=folder/"raw"/part
    payload=json.loads(file.read_bytes())
    mutate(payload)
    blob=json.dumps(payload).encode("utf-8")
    file.write_bytes(blob)
    manifest=folder/"audit.json"
    report=json.loads(manifest.read_bytes())
    if part=="daily.json":
        report["daily_comparison"]["raw_sha256"]=sha256(blob).hexdigest()
    else:
        report["page_sha256"][int(part.removeprefix("minute_").removesuffix(".json"))] = sha256(blob).hexdigest()
    manifest.write_text(json.dumps(report), encoding="utf-8")


def test_raw_output1_changes_without_historical_output2_changes(tmp_path):
    baseline,replay=setup_roots(tmp_path)
    _rewrite_mock_vendor_raw(
        replay, part="minute_000.json",
        mutate=lambda p: p.update(output1={
            "stck_prpr":"CUSTOM_VENDOR_LIVE_HEADER_VALUE_SHOULD_NOT_LEAK",
            "acml_vol":"7654321",
        }),
    )
    _rewrite_mock_vendor_raw(
        replay, part="daily.json",
        mutate=lambda p: p.update(output1={"prdy_ctrt":"123.4"}),
    )
    result=compare_replay(baseline_root=baseline,replay_root=replay,
                          symbol=SYMBOL,session=DATE,markets=("UN",),
                          out_dir=tmp_path/"comparison")["audit"]
    un=result["markets"]["UN"]
    raw=un["raw_historical_payload_comparison"]
    assert result["status"] == "REPLAY_OBSERVATIONS_REPRODUCED"
    assert un["normal_observation_content_identical"] is True
    assert un["raw_page_hashes_identical"] is False
    assert un["official_daily_source"]["same_raw_sha256"] is False
    assert raw["classification"] == "HISTORICAL_BARS_SAME_VENDOR_OUTPUT1_CHANGED"
    assert raw["minute_page_historical_output2_all_identical"] is True
    assert raw["historical_daily_output2_identical"] is True
    assert raw["daily_live_quote_header_output1_identical"] is False
    assert raw["minute_page_comparisons"][0]["historical_output2_identical"] is True
    assert raw["minute_page_comparisons"][0]["changed_header_field_names_only"] == [
        "acml_vol", "stck_prpr"
    ]
    assert "CUSTOM_VENDOR_LIVE_HEADER_VALUE_SHOULD_NOT_LEAK" not in json.dumps(result)
    assert raw["raw_body_values_disclosed"] is False


def test_raw_output2_change_is_held_even_if_normalized_snapshot_unchanged(tmp_path):
    baseline,replay=setup_roots(tmp_path)
    _rewrite_mock_vendor_raw(
        replay, part="minute_000.json",
        mutate=lambda p: p["output2"][0].update(vendor_unverified_new_field="different"),
    )
    result=compare_replay(baseline_root=baseline,replay_root=replay,
                          symbol=SYMBOL,session=DATE,markets=("UN",),
                          out_dir=tmp_path/"comparison")["audit"]
    raw=result["markets"]["UN"]["raw_historical_payload_comparison"]
    assert result["status"] == "REPLAY_HISTORICAL_PAYLOAD_CHANGED_HOLD"
    assert raw["classification"] == "HISTORICAL_PAGE_OR_DAILY_PAYLOAD_DIFFERENCE_HOLD"
    assert raw["minute_page_historical_output2_all_identical"] is False
    assert result["markets"]["UN"]["normal_observation_content_identical"] is True
    assert result["usable_for_backtesting"] is False
