# 2026-11-03 USA daily source gaps: Yahoo vs Stock Analysis displayed OHLC

## Purpose
A prior append-only stock-history refresh restored **2026-11-02** for all six tracked stocks but left five U.S. ticker/session stock-history gaps (AAPL: 10/22,10/27; MSFT: 10/22,10/27; SPY: 10/27) after conflicting vendor OHLC history. Treat these records as evidence conflicts, not automatically repairable missed paper fills.

A new read-only study captures six fixed, public daily-history pages, not Yahoo's private or order APIs. URLs:
- https://finance.yahoo.com/quote/AAPL/history/
- https://finance.yahoo.com/quote/MSFT/history/
- https://finance.yahoo.com/quote/SPY/history/
- https://stockanalysis.com/stocks/aapl/history/
- https://stockanalysis.com/stocks/msft/history/
- https://stockanalysis.com/etf/spy/history/

Neither publisher's displayed cents constitute independently certified exchange raw prints, even when their values agree; **common upstream data is possible**. The original first-observed feed and later vendor source also have their own underlying data lineage that cannot be inferred from HTTP page matching.

## Direct real remote-Windows WSL probe, 2026-11-03 KST
GET 6 read-only, unauthenticated HTTPS pages succeeded with HTTP 200. Full response bytes are private immutable 0600 HTML snapshots. Each source is parsed by stdlib HTMLParser in an exact title/ticker/market day + OHLC table format, storing independent raw SHA values. No login, OAuth, account, order or broker endpoints accessed.

| Session | Symbol | First observed discrepancy | Both publishers' displayed values | Result |
|---|---|---|---|---|
| 2026-10-22 | AAPL | First open 344.934997..., later 344.940002... | Open **344.94** | Half-cent float/display ambiguity; unresolved |
| 2026-10-27 | AAPL | First open 343.165008..., later 343.170013... | Open **343.17** | Round to same displayed cent; precision unresolved |
| 2026-10-22 | MSFT | First open 532.700012..., later 533.140014... | Open **533.14** | **Material ~$0.44 open disagreement**, unresolved |
| 2026-10-27 | MSFT | First low 528.330993..., later 528.330017... | Low **528.33** | Same cent; precision unresolved |
| 2026-10-27 | SPY | First high 771.130004..., later 771.140014... | High **771.14** | One-cent different published high; unresolved |

**All 5** two-public-page OHLC vectors agree at the *displayed cent* and **all 5** later vendor OHLC vectors round to that published vector. **This does NOT establish correct trade prints or prove that replacing the 5 absent long-history dates is safe**. The first observed snapshot and later vendor snapshot are not edited. Sub-cent double precision, tick price convention, correction timestamps, and 2026-10-22 MSFT open must not be flattened into a universal rounding rule.

## Immutable reference evidence

Research-only directory:
```text
/home/hong/apps/FreeQuantLab/state/stock_daily/quality_reports/
  us_public_two_source_20261103_r1/
    stockanalysis_AAPL.html, stockanalysis_MSFT.html, stockanalysis_SPY.html
    yahoo_AAPL.html, yahoo_MSFT.html, yahoo_SPY.html
    report.json, report.sha256
    manifest.json, manifest.sha256
```

Stored directory mode 0700; every file 0600. Captured HTML is for internal reproducibility only and **should not be redistributed**.

Independent verification command:
```bash
cd /home/hong/apps/FreeQuantLab-observer-runtime
P=/home/hong/apps/FreeQuantLab/.venv/bin/python
R=/home/hong/apps/FreeQuantLab/state/stock_daily/quality_reports
$P -m quantlab.stock_us_public_price_audit --verify-only \
  --gap-report "$R/gap_provenance_20261103_r2.json" \
  --root "$R/us_public_two_source_20261103_r1"
```
Verification reopens both original stock-daily SQLite files in read-only mode and rechecks their raw vendor SHA-backed conflicts; it reparses all six saved website HTML pages and rebuilds the comparison in full. Immutable manifest and report cannot be overwritten. Falsifying price data or even replacing approval flags while regenerating their own hash must fail source audit.

## KODEX 200 distribution: stronger supporting timeline but still not exchange-certified adjustment methodology

The original issuer's 2026-10-27 notice confirms the KODEX 200 **069500** payout is 136 won:
- https://www.samsungfund.com/etf/lounge/notice-view.do?no=80553
- Another published mirror https://www.funetf.co.kr/membersLounge/notice/8914 lists **069500 KODEX 200 ETF 136 KRW** by exact code.

A third-party dividend-history page lists **2026-10-29 ex-distribution**, **2026-10-30 record**, **2026-11-03 payment**:
- https://kr.investing.com/etfs/samsung-kodex-kospi-200-securities-dividends

The independent third-party calendar corroborates the former ex-day hypothesis, but **it is not a KRX certified raw event message**. PR #123 verified an issuer-based 136 KRW ratio hypothesis fits sixteen prior-day OHLC to <=1 KRW and PR #124 quarantined the original, later-vendor and arithmetic variants with separate provenance. This study does **not** modify those reports or promote their series into canonical trading prices. Before choosing an official adjusted-history convention, seek exchange-side ex-event reference, vendor correction history, and raw/non-adjusted transaction price verification.

## Safety boundary and next gate
- 5 U.S. daily gaps and 4 KODEX 200 gaps remain formally unresolved.
- Do not replace `observations.sqlite3` / `history.sqlite3` rows, silently merge adjusted into unadjusted prices, manufacture 90-day prospective paper trades, or call the trading APIs.
- Source approval for model training, backtest, paper or live remains **false**.
- 90-day clean cohort remains independently incomplete; market history restoration must not convert missing prospective decisions into real backdated signals.
- This is a **public displayed-price consistency report**, not a venue-certified execution audit, broker account fill evidence, or realized performance statement.
