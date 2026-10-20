from __future__ import annotations

from datetime import datetime, timezone
import pytest
from quantlab.data import DataError, naver_daily
from quantlab import data


class FakeResponse:
    def __init__(self, raw):
        self.content = raw


def _xml(items: list[str], symbol: str = "005930") -> bytes:
    content = "<?xml version=\"1.0\" encoding=\"EUC-KR\" ?><protocol><chartdata symbol=\"" + symbol + "\">"
    for s in items:
        content += '<item data="' + s + '"/>'
    content += "</chartdata></protocol>"
    return content.encode("euc-kr")


def test_naver_equity_parse_and_excludes_current_session(monkeypatch):
    records = []
    # 100 consecutive days, including today's incomplete current session.
    from datetime import timedelta
    start = datetime(2026, 7, 13)
    for i in range(100):
        d = start + timedelta(days=i)
        records.append(d.strftime("%Y%m%d") + "|100|105|95|101|12345")
    monkeypatch.setattr(data, "_get", lambda url, **kwargs: FakeResponse(_xml(records)))
    now = datetime(2026, 10, 20, 2, tzinfo=timezone.utc)
    result = naver_daily("005930", 100, now=now)
    assert result["date"].max().date() < datetime(2026, 10, 20).date()
    assert all(result["high"] >= result["low"])


def test_naver_rejects_untrusted_bodies(monkeypatch):
    for symbol in ("00593", "ABCDEF", "００５９３０", "005930.KS"):
        with pytest.raises(DataError):
            naver_daily(symbol, 100)
    monkeypatch.setattr(data, "_get", lambda url, **kwargs: FakeResponse(b"<!doctype html><html>blocked</html>"))
    with pytest.raises(DataError):
        naver_daily("005930", 100)
    monkeypatch.setattr(data, "_get", lambda url, **kwargs: FakeResponse(_xml(["20261001|100|110|90|105|3"] * 100, "000660")))
    with pytest.raises(DataError):
        naver_daily("005930", 100)
