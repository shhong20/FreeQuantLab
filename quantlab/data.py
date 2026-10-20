"""Public read-only market feeds; real orders are NEVER sent by this module.

Upbit KR: daily candles are labeled by their UTC session start (KST 09:00).
KIS: daily date is Korea Exchange session date. Stooq: US exchange session date.
All sources exclude the still-open local session conservatively.
"""
from __future__ import annotations

import io
import os
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")


class DataError(RuntimeError):
    pass


def normalize(frame: pd.DataFrame, *, today: str | None = None) -> pd.DataFrame:
    required = ["date", "open", "high", "low", "close", "volume"]
    if not set(required).issubset(frame.columns):
        raise DataError(f"Missing OHLCV columns: {set(required) - set(frame.columns)}")
    df = frame.loc[:, required].copy()
    df["date"] = pd.to_datetime(df["date"], errors="raise").dt.tz_localize(None).dt.normalize()
    for key in required[1:]:
        df[key] = pd.to_numeric(df[key], errors="raise")
    if today is not None:
        df = df[df["date"] < pd.Timestamp(today)]  # discard incomplete current session
    df = df.drop_duplicates(subset="date", keep="last").sort_values("date").reset_index(drop=True)
    if df.empty or df[required[1:]].isna().any().any():
        raise DataError("Empty or missing OHLCV values")
    if (df[["open", "high", "low", "close"]] <= 0).any().any() or (df["volume"] < 0).any():
        raise DataError("Invalid OHLCV values")
    if (df["high"] < df[["open", "close", "low"]].max(axis=1)).any() or (df["low"] > df[["open", "close", "high"]].min(axis=1)).any():
        raise DataError("Contradictory OHLC data")
    return df


def _get(url: str, **kwargs) -> requests.Response:
    try:
        headers = {"User-Agent": "FreeQuantLab/0.1 (personal research)", **kwargs.pop("headers", {})}
        response = requests.get(url, timeout=15, headers=headers, **kwargs)
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        raise DataError(f"Market data request failed: {url}: {exc}") from exc


def upbit_daily(market: str, bars: int = 800, *, now: datetime | None = None) -> pd.DataFrame:
    if not market.startswith("KRW-"):
        raise DataError("Initial version supports only KRW spot pairs")
    if not 80 <= bars <= 3000:
        raise DataError("bars must be 80..3000")
    current = now or datetime.now(timezone.utc)
    utc_today = current.astimezone(timezone.utc).date()
    cursor = f"{utc_today.isoformat()}T00:00:00Z"  # exclude partial current UTC candle
    records: list[dict] = []
    for _ in range((bars + 199) // 200 + 1):
        page = _get("https://api.upbit.com/v1/candles/days", params={"market": market, "count": min(200, bars - len(records)), "to": cursor}).json()
        if not isinstance(page, list) or not page:
            break
        records.extend(page)
        oldest = min(rec["candle_date_time_utc"] for rec in page)
        new_cursor = oldest + "Z"
        if new_cursor == cursor:
            break
        cursor = new_cursor
        if len(records) >= bars:
            break
        time.sleep(0.15)
    if not records:
        raise DataError(f"No Upbit data for {market}")
    df = pd.DataFrame([{
        "date": x["candle_date_time_utc"][:10], "open": x["opening_price"],
        "high": x["high_price"], "low": x["low_price"], "close": x["trade_price"],
        "volume": x["candle_acc_trade_volume"],
    } for x in records])
    return normalize(df, today=utc_today.isoformat()).tail(bars).reset_index(drop=True)


def stooq_daily(symbol: str, bars: int = 800, *, now: datetime | None = None) -> pd.DataFrame:
    if not symbol.replace(".", "").replace("-", "").isalnum():
        raise DataError("Invalid ticker")
    current = now or datetime.now(timezone.utc)
    today = current.astimezone(NY).date()
    response = _get("https://stooq.com/q/d/l/", params={"s": symbol.lower(), "i": "d", "d1": "20100101", "d2": today.strftime("%Y%m%d")})
    try:
        df = pd.read_csv(io.StringIO(response.text)).rename(columns={"Date": "date", "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
        return normalize(df, today=today.isoformat()).tail(bars).reset_index(drop=True)
    except (ValueError, KeyError, pd.errors.ParserError) as exc:
        raise DataError("Stooq returned invalid or inaccessible history; use CSV or KIS fallback") from exc


def naver_daily(symbol: str, bars: int = 1000, *, now: datetime | None = None) -> pd.DataFrame:
    """RESEARCH ONLY: unofficial public Naver equity daily history.

    No uptime guarantee or verified corporate action adjustments.
    Does not support orders, simulated fills, or portfolio promotion.
    """
    from xml.etree import ElementTree as ET

    if not (isinstance(symbol, str) and len(symbol) == 6
            and symbol.isascii() and symbol.isdecimal()):
        raise DataError("Naver symbol must be exactly six ASCII digits")
    if not 80 <= bars <= 3000:
        raise DataError("bars must be 80..3000")
    current = now or datetime.now(timezone.utc)
    today = current.astimezone(KST).date()
    res = _get(
        "https://fchart.stock.naver.com/sise.nhn",
        params={"symbol": symbol, "timeframe": "day",
                "count": bars + 10, "requestType": 0},
    )
    raw = res.content
    if not isinstance(raw, bytes) or len(raw) > 2_000_000:
        raise DataError("Invalid/oversized Naver history response")
    try:
        # Python's expat may not support EUC-KR bytes directly. Convert to
        # UTF-8 before XML parsing; never relax tag/field validation.
        xml_text = raw.decode('euc-kr')
        xml_text = xml_text.replace('encoding="EUC-KR"', 'encoding="UTF-8"')
        root = ET.fromstring(xml_text.encode('utf-8'))
    except (ET.ParseError, UnicodeError, ValueError) as exc:
        raise DataError("Naver did not return parseable historical XML") from exc
    header = root.find("chartdata")
    if root.tag != "protocol" or header is None or header.get("symbol") != symbol:
        raise DataError("Unexpected Naver history payload")
    rows = []
    for item in header.findall("item"):
        pieces = (item.get("data") or "").split("|")
        if len(pieces) != 6:
            raise DataError("Malformed Naver OHLCV item")
        try:
            dt = datetime.strptime(pieces[0], "%Y%m%d").date().isoformat()
            vals = [float(v) for v in pieces[1:]]
        except (ValueError, TypeError) as exc:
            raise DataError("Malformed Naver OHLCV fields") from exc
        rows.append(dict(zip(
            ("date", "open", "high", "low", "close", "volume"), (dt, *vals),
        )))
    if len(rows) < 80:
        raise DataError("Insufficient Naver daily history")
    return normalize(pd.DataFrame(rows), today=today.isoformat()).tail(bars).reset_index(drop=True)


@lru_cache(maxsize=1)
def _kis_token() -> str:
    key, secret = os.getenv("KIS_APP_KEY"), os.getenv("KIS_APP_SECRET")
    if not key or not secret:
        raise DataError("KIS_APP_KEY and KIS_APP_SECRET required in environment")
    base = "https://openapi.koreainvestment.com:9443"
    try:
        auth = requests.post(base + "/oauth2/tokenP", json={"grant_type": "client_credentials", "appkey": key, "appsecret": secret}, timeout=15)
        auth.raise_for_status()
        return auth.json()["access_token"]
    except (requests.RequestException, ValueError, KeyError) as exc:
        raise DataError("KIS OAuth token request failed") from exc


def kis_daily(symbol: str, bars: int = 800, *, now: datetime | None = None) -> pd.DataFrame:
    """KIS OAuth credentials from environment. No trading scope used."""
    if not (symbol.isdigit() and len(symbol) == 6):
        raise DataError("KIS symbol must be a six-digit KRX ticker")
    key, secret = os.getenv("KIS_APP_KEY"), os.getenv("KIS_APP_SECRET")
    if not key or not secret:
        raise DataError("KIS_APP_KEY and KIS_APP_SECRET required in environment")
    if not 80 <= bars <= 3000:
        raise DataError("bars must be 80..3000")
    base = "https://openapi.koreainvestment.com:9443"
    token = _kis_token()
    today = (now or datetime.now(timezone.utc)).astimezone(KST).date()
    cutoff = today - timedelta(days=max(600, int(bars * 2)))
    cursor = today - timedelta(days=1)
    all_rows: list[dict] = []
    for _ in range((bars + 99) // 100 + 10):
        if cursor < cutoff:
            break
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol,
                  "FID_INPUT_DATE_1": cutoff.strftime("%Y%m%d"),
                  "FID_INPUT_DATE_2": cursor.strftime("%Y%m%d"),
                  "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"}
        header = {"authorization": f"Bearer {token}", "appkey": key, "appsecret": secret,
                  "tr_id": "FHKST03010100", "custtype": "P"}
        payload = _get(base + "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", params=params, headers=header).json()
        if str(payload.get("rt_cd")) != "0":
            raise DataError(f"KIS data failed: {payload.get('msg_cd', 'unknown')}")
        page = payload.get("output2") or []
        page = [r for r in page if r.get("stck_bsop_date") and float(r.get("stck_clpr") or 0) > 0]
        if not page:
            break
        all_rows.extend(page)
        oldest = min(datetime.strptime(row["stck_bsop_date"], "%Y%m%d").date() for row in page)
        if oldest >= cursor:
            break
        cursor = oldest - timedelta(days=1)
        if len(all_rows) >= bars:
            break
        time.sleep(0.18)
    if not all_rows:
        raise DataError("KIS returned no historical daily data")
    rows = [{"date": x["stck_bsop_date"], "open": x["stck_oprc"], "high": x["stck_hgpr"],
             "low": x["stck_lwpr"], "close": x["stck_clpr"], "volume": x["acml_vol"]} for x in all_rows]
    # KIS YYYYMMDD must be parsed explicitly to avoid unix nanoseconds interpretation
    for row in rows:
        row["date"] = datetime.strptime(row["date"], "%Y%m%d").date().isoformat()
    return normalize(pd.DataFrame(rows), today=today.isoformat()).tail(bars).reset_index(drop=True)


def csv_daily(path: str | Path, bars: int | None = None) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.lower() for c in df.columns]
    result = normalize(df)
    return result.tail(bars).reset_index(drop=True) if bars else result


def demo_daily(bars: int = 1000, *, seed: int = 17) -> pd.DataFrame:
    """Synthetic TEST data only; never a proxy for live profitability."""
    rng = np.random.default_rng(seed)
    base = np.zeros(bars)
    base[0] = 100.0
    closes = [100.0]
    for i in range(1, bars):
        drift = (0.004 if (i // 75) % 2 == 0 else -0.004)
        ret = np.clip(drift + rng.normal(0, 0.014), -0.10, 0.10)
        closes.append(closes[-1] * np.exp(ret))
    opening = np.array(closes) * np.exp(rng.normal(0, 0.002, bars))
    highs = np.maximum(opening, closes) * 1.003
    lows = np.minimum(opening, closes) * 0.997
    return normalize(pd.DataFrame({"date": pd.date_range("2021-01-01", periods=bars),
       "open": opening, "high": highs, "low": lows, "close": closes,
       "volume": rng.uniform(1000, 5000, bars)}))


def fetch(asset: dict) -> pd.DataFrame:
    provider, symbol = asset["provider"], asset.get("symbol", "DEMO")
    bars = int(asset.get("bars", 800))
    if provider == "upbit":
        return upbit_daily(symbol, bars)
    if provider == "stooq":
        return stooq_daily(symbol, bars)
    if provider == "naver":
        return naver_daily(symbol, bars)
    if provider == "kis":
        return kis_daily(symbol, bars)
    if provider == "csv":
        return csv_daily(asset["path"], bars)
    if provider == "demo":
        return demo_daily(bars, seed=int(asset.get("seed", 17)))
    raise DataError(f"Unknown provider: {provider}")


def live_paper_quote(provider: str, symbol: str, *, now: datetime | None = None) -> tuple[float, str]:
    """Forward paper quote at CURRENT market time, never previous close as a fill.

    Returns price and local market-session date. Not a tradeable/orderbook quote;
    forward paper results still differ from real fills, especially around spreads.
    """
    t = now or datetime.now(timezone.utc)
    if t.tzinfo is None:
        raise DataError("Timezone-aware now is required")
    if provider == "upbit":
        local = t.astimezone(timezone.utc)
        body = _get("https://api.upbit.com/v1/ticker", params={"markets": symbol}).json()
        if not isinstance(body, list) or len(body) != 1 or body[0].get("market") != symbol:
            raise DataError("Upbit ticker response invalid")
        price = float(body[0]["trade_price"])
        # Exclude stale public quote (e.g., trading halted) using trade_timestamp.
        trade_millis = float(body[0].get("trade_timestamp") or 0)
        age_s = local.timestamp() - trade_millis / 1000.0
        if not -120 <= age_s <= 300:
            raise DataError(f"Upbit quote stale: {age_s:.0f}s")
        if price <= 0:
            raise DataError("Nonpositive quote")
        return price, local.date().isoformat()
    if provider == "kis":
        local = t.astimezone(KST)
        try:
            import exchange_calendars as xcals
            market_open = bool(xcals.get_calendar("XKRX").is_session(local.date().isoformat()))
        except Exception as exc:
            raise DataError(f"KRX exchange calendar unavailable: {exc}") from exc
        if not market_open or not ((9, 10) <= (local.hour, local.minute) <= (15, 15)):
            raise DataError("KIS paper quotes enabled only 09:10~15:15 KST on an XKRX trading day")
        key, secret = os.getenv("KIS_APP_KEY"), os.getenv("KIS_APP_SECRET")
        headers = {"authorization": f"Bearer {_kis_token()}", "appkey": key, "appsecret": secret,
                   "tr_id": "FHKST01010100", "custtype": "P"}
        reply = _get("https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/inquire-price",
                     params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol}, headers=headers).json()
        if str(reply.get("rt_cd")) != "0" or not isinstance(reply.get("output"), dict):
            raise DataError("KIS paper price request rejected")
        output = reply["output"]
        price = float(output.get("stck_prpr") or 0)
        if price <= 0 or float(output.get("acml_vol") or 0) <= 0:
            raise DataError("KIS price/volume invalid or possible nontrading day")
        return price, local.date().isoformat()
    raise DataError("Paper live quote supports Upbit and KIS only; stooq/csv/demo are research-only")
