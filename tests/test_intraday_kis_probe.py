"""KIS one-day historical minute evidence tests: mocked data, no real accounts."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import pytest

from quantlab.intraday_kis_probe import KISMinuteError, capture_kis, probe_kis


NOW = datetime(2026, 10, 26, 4, 0, tzinfo=timezone.utc)
DAY = "2026-10-20"


def samples():
    bars = []
    for minute in range(9*60, 15*60+20):
        label = f"{minute//60:02d}{minute%60:02d}00"
        bars.append({
            "stck_bsop_date": "20261020", "stck_cntg_hour": label,
            "stck_oprc": "100", "stck_hgpr": "101",
            "stck_lwpr": "99", "stck_prpr": "100",
            "cntg_vol": "1000",
        })
    bars.append({
        "stck_bsop_date": "20261020", "stck_cntg_hour": "153000",
        "stck_oprc": "100", "stck_hgpr": "101",
        "stck_lwpr": "99", "stck_prpr": "100",
        "cntg_vol": "88888",
    })
    return sorted(bars, key=lambda x:x["stck_cntg_hour"], reverse=True)


class Resp:
    def __init__(self, payload):
        self.content = json.dumps(payload).encode()

    def raise_for_status(self):
        pass


def mocked(pages, calls):
    def getter(url, **kwargs):
        assert url.endswith("/inquire-time-dailychartprice")
        assert kwargs["headers"]["tr_id"] == "FHKST03010230"
        assert kwargs["params"]["FID_COND_MRKT_DIV_CODE"] == "J"
        calls.append(kwargs)
        assert len(calls) <= len(pages)
        return Resp({"rt_cd": "0", "output2": pages[len(calls)-1]})
    return getter


def test_kis_raw_pages_are_immutable_and_never_backtested(monkeypatch, tmp_path):
    monkeypatch.setenv("KIS_APP_KEY", "TEST-APP-KEY")
    monkeypatch.setenv("KIS_APP_SECRET", "TEST-APP-SECRET")
    full = samples()
    pages = [full[i:i+120] for i in range(0,len(full),120)]
    calls = []
    result = capture_kis(symbol="005930", session=DAY, out_dir=tmp_path,
                         token="TEST-BEARER", getter=mocked(pages,calls),
                         now=NOW)
    folder = Path(result["snapshot"])
    audit = json.loads((folder/"audit.json").read_text())
    assert len(calls) == 4
    assert audit["raw_pages"] == 4
    assert audit["unique_minute_labels"] == 381
    assert audit["continuous_reference_0900_1519_minute_labels"] == 380
    assert audit["missing_reference_labels"] == 0
    assert audit["closing_auction_time_labels"] == ["153000"]
    assert audit["automatic_5min_conversion"] is False
    assert audit["backtest_or_orders_allowed"] is False
    assert audit["timestamp_label_semantics"] == "UNVERIFIED_START_OR_END"
    assert audit["pagination_stop"] == "reached_open"
    assert all(calls[i]["params"]["FID_INPUT_HOUR_1"] >
               calls[i+1]["params"]["FID_INPUT_HOUR_1"] for i in range(3))
    for i, digest in enumerate(audit["raw_sha256"]):
        assert sha256((folder/"raw"/f"page_{i:03d}.json").read_bytes()).hexdigest() == digest
    assert "TEST-BEARER" not in (folder/"audit.json").read_text()
    assert "TEST-APP-SECRET" not in (folder/"audit.json").read_text()
    assert not (tmp_path/"paper.db").exists()
    with pytest.raises(KISMinuteError, match="already exists"):
        capture_kis(symbol="005930", session=DAY, out_dir=tmp_path,
                    token="TEST-BEARER", getter=mocked(pages, []), now=NOW)


def test_invalid_source_date_or_volume_fails_closed(monkeypatch):
    monkeypatch.setenv("KIS_APP_KEY","X")
    monkeypatch.setenv("KIS_APP_SECRET","Y")
    source = samples()
    source[0] = dict(source[0], stck_bsop_date="20261019")
    with pytest.raises(KISMinuteError, match="different session"):
        probe_kis(symbol="005930", session=DAY, token="BEARER",
                  getter=mocked([source[:120]],[]), now=NOW)
    source = samples()
    source[0] = dict(source[0], cntg_vol="-5")
    with pytest.raises(KISMinuteError, match="Contradictory"):
        probe_kis(symbol="005930", session=DAY, token="BEARER",
                  getter=mocked([source[:120]],[]), now=NOW)


def test_same_minute_conflict_and_invalid_day_blocked(monkeypatch):
    monkeypatch.setenv("KIS_APP_KEY","X")
    monkeypatch.setenv("KIS_APP_SECRET","Y")
    a = samples()
    collision = dict(a[119], stck_prpr="100.5")
    pages = [a[:120], [collision, a[120]]]
    with pytest.raises(KISMinuteError, match="Conflicting duplicate"):
        probe_kis(symbol="005930", session=DAY, token="BEARER",
                  getter=mocked(pages,[]), now=NOW)
    with pytest.raises(KISMinuteError, match="fully past"):
        probe_kis(symbol="005930", session="2026-10-26", token="BEARER",
                  getter=mocked([],[]), now=NOW)
    with pytest.raises(KISMinuteError, match="ticker"):
        probe_kis(symbol="../../", session=DAY, token="BEARER",
                  getter=mocked([],[]), now=NOW)


def test_short_provider_page_is_recorded_not_silently_completed(monkeypatch):
    monkeypatch.setenv("KIS_APP_KEY","X")
    monkeypatch.setenv("KIS_APP_SECRET","Y")
    pages=[samples()[:25]]
    snapshots, observed, audit=probe_kis(
        symbol="005930", session=DAY, token="BEARER", now=NOW,
        getter=mocked(pages,[]),
    )
    assert len(observed)==25
    assert audit["pagination_stop"] == "short_page_unproven_prior_history"
    assert audit["missing_reference_labels"] > 0
    assert audit["automatic_5min_conversion"] is False


def test_provider_error_prints_only_safe_code_never_untrusted_body(monkeypatch):
    from quantlab import intraday_kis_probe as mod
    monkeypatch.setenv("KIS_APP_KEY", "SECRET_KEY_EXAMPLE")
    monkeypatch.setenv("KIS_APP_SECRET", "SECRET_SECRET_EXAMPLE")

    class Rejection:
        content = json.dumps({
            "rt_cd": "1", "msg_cd": "EGW00202",
            "msg1": "ACCOUNT_OR_SECRET_MUST_NOT_BE_LOGGED",
        }).encode()
        def raise_for_status(self):
            return None

    def getter(url, **kw):
        return Rejection()

    with pytest.raises(KISMinuteError, match=r"msg_cd=EGW00202") as caught:
        mod.probe_kis(symbol="005930", session=DAY, token="TOKEN_NOT_FOR_OUTPUT",
                      getter=getter, now=NOW)
    message = str(caught.value)
    assert "ACCOUNT_OR_SECRET" not in message
    assert "TOKEN_NOT_FOR_OUTPUT" not in message
    assert "SECRET_KEY_EXAMPLE" not in message

    class UntrustedRejection(Rejection):
        content = json.dumps({
            "rt_cd": "1", "msg_cd": "TOKEN_NOT_FOR_OUTPUT: unsafe",
            "msg1": "secret text",
        }).encode()

    with pytest.raises(KISMinuteError, match=r"msg_cd=UNKNOWN"):
        mod.probe_kis(symbol="005930", session=DAY, token="TOKEN_NOT_FOR_OUTPUT",
                      getter=lambda *a, **kw: UntrustedRejection(), now=NOW)


def test_cli_shows_controlled_reason_but_not_credentials(monkeypatch, capsys):
    from quantlab import intraday_kis_probe as mod
    def fail_capture(**_):
        raise KISMinuteError("KIS minute required field missing: stck_oprc")
    monkeypatch.setattr(mod, "capture_kis", fail_capture)
    with pytest.raises(SystemExit) as caught:
        mod.main(["--symbol", "005930", "--session", DAY, "--out-dir", "/tmp/unused"])
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert "KIS minute required field missing: stck_oprc" in err
    assert "authorization" not in err

    def fail_untrusted(**_):
        raise OSError("SENSITIVE_FILESYSTEM_PATH_NOT_TO_SHOW")
    monkeypatch.setattr(mod, "capture_kis", fail_untrusted)
    with pytest.raises(SystemExit):
        mod.main(["--symbol", "005930", "--session", DAY, "--out-dir", "/tmp/unused"])
    err = capsys.readouterr().err
    assert "OSError" in err
    assert "SENSITIVE_FILESYSTEM_PATH_NOT_TO_SHOW" not in err
