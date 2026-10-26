"""Offline KIS minute reconciliation to provisional 5m research candles.

Input is an immutable local historical probe snapshot; no API calls, keys,
order placement, operating DB writes, or automatic strategy evaluation.
Minute time-label meaning is ASSUMED, not certified by provider.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile


class KISReconciliationError(ValueError):
    pass


MINUTES = tuple(f"{m//60:02d}{m%60:02d}00" for m in range(540, 920))
RAW_FIELDS = ("stck_bsop_date", "stck_cntg_hour", "stck_oprc",
              "stck_hgpr", "stck_lwpr", "stck_prpr", "cntg_vol")
FIELD_MAP = {"open": "stck_oprc", "high": "stck_hgpr",
             "low": "stck_lwpr", "close": "stck_prpr", "volume": "cntg_vol"}
CSV_FIELDS = ("timestamp", "open", "high", "low", "close", "volume")


def _read_snapshot(folder: Path) -> tuple[str, str, list[dict], dict]:
    try:
        audit = json.loads((folder / "audit.json").read_bytes())
        raw_path = folder / "minute_observations_unverified.json"
        blob = raw_path.read_bytes()
        if not isinstance(audit, dict) or audit.get("status") != (
            "KIS_ONE_DAY_MINUTE_PROVENANCE_ONLY_NO_5MIN_BACKTEST"
        ):
            raise KISReconciliationError("Snapshot is not a KIS provenance-only audit")
        if sha256(blob).hexdigest() != audit.get("observations_sha256"):
            raise KISReconciliationError("Minute observation SHA-256 mismatch")
        digests = audit.get("raw_sha256")
        pages = sorted((folder / "raw").glob("page_*.json"))
        if (not isinstance(digests, list) or not pages or
                len(pages) != audit.get("raw_pages") or len(pages) != len(digests)):
            raise KISReconciliationError("KIS raw page manifest mismatch")
        for index, page in enumerate(pages):
            if page.name != f"page_{index:03d}.json":
                raise KISReconciliationError("Non-contiguous KIS raw page index")
            if sha256(page.read_bytes()).hexdigest() != digests[index]:
                raise KISReconciliationError("KIS raw page SHA-256 mismatch")
        symbol, session = audit.get("symbol"), audit.get("session")
        if not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}", symbol):
            raise KISReconciliationError("Invalid snapshot ticker")
        if not isinstance(session, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", session):
            raise KISReconciliationError("Invalid snapshot session")
        date.fromisoformat(session)
        if folder.name != f"KIS_{symbol}_{session}":
            raise KISReconciliationError("Snapshot directory does not match source identity")
        data = json.loads(blob)
        if not isinstance(data, list):
            raise KISReconciliationError("Minute observations must be a JSON array")
        return symbol, session, data, audit
    except (OSError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, KISReconciliationError):
            raise
        raise KISReconciliationError("Cannot read validated KIS snapshot") from exc


def _candles(data: list[dict], session: str) -> tuple[list[dict], dict]:
    if len(data) != 381:
        raise KISReconciliationError("Require 380 complete continuous minutes + 15:30 auction")
    seen = {}
    for item in data:
        if not isinstance(item, dict) or any(k not in item for k in RAW_FIELDS):
            raise KISReconciliationError("Missing required minute row/field")
        if str(item["stck_bsop_date"]) != session.replace("-", ""):
            raise KISReconciliationError("Cross-session minute contamination")
        label = str(item["stck_cntg_hour"])
        if label in seen:
            raise KISReconciliationError("Duplicate minute label in normalized data")
        seen[label] = item
    if set(seen) != set(MINUTES) | {"153000"}:
        raise KISReconciliationError("Unexpected/missing continuous or auction minute labels")

    normalized = {}
    for label, item in seen.items():
        try:
            vals = {name: int(item[source]) for name, source in FIELD_MAP.items()}
        except (ValueError, TypeError, OverflowError) as exc:
            raise KISReconciliationError("KIS minute OHLCV is not integer-valued") from exc
        if (min(vals["open"], vals["high"], vals["low"], vals["close"]) <= 0 or
                vals["volume"] < 0 or
                vals["high"] < max(vals["open"], vals["low"], vals["close"]) or
                vals["low"] > min(vals["open"], vals["high"], vals["close"])):
            raise KISReconciliationError("Contradictory minute OHLCV")
        normalized[label] = vals

    bars = []
    for begin in range(0, len(MINUTES), 5):
        group = [normalized[label] for label in MINUTES[begin:begin + 5]]
        if len(group) != 5:
            raise KISReconciliationError("Partial 5-minute bucket")
        key = MINUTES[begin]
        bars.append({
            "timestamp": f"{session}T{key[:2]}:{key[2:4]}:00+09:00",
            "open": group[0]["open"],
            "high": max(v["high"] for v in group),
            "low": min(v["low"] for v in group),
            "close": group[-1]["close"],
            "volume": sum(v["volume"] for v in group),
        })
    if len(bars) != 76 or bars[0]["timestamp"][11:16] != "09:00" or (
            bars[-1]["timestamp"][11:16] != "15:15"):
        raise KISReconciliationError("Unexpected KRX 76-bar continuous trading window")
    auction = normalized["153000"]
    closing_price = auction["close"]
    regular_volume = sum(b["volume"] for b in bars)
    audit = {
        "continuous_open": bars[0]["open"],
        "continuous_high": max(b["high"] for b in bars),
        "continuous_low": min(b["low"] for b in bars),
        "continuous_last_price": bars[-1]["close"],
        "continuous_volume": regular_volume,
        "closing_auction_time_label": "153000",
        "closing_auction_price": closing_price,
        "closing_auction_volume": auction["volume"],
        "candidate_total_volume": regular_volume + auction["volume"],
        "candles_5m": len(bars),
        "minutely_bars": len(MINUTES),
        "input_labels_assumed_start": True,
    }
    return bars, audit


def _reconcile_daily(path: str | Path | None, *, symbol: str, session: str,
                     candidate: dict) -> dict:
    if path is None:
        return {"status": "NO_DAILY_REFERENCE", "approved": False}
    # URI ro prevents accidental mutations of existing evaluation/raw ledgers.
    db = Path(path).expanduser().resolve()
    if not db.is_file():
        return {"status": "DAILY_REFERENCE_MISSING", "approved": False}
    try:
        with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True,
                             timeout=2) as connection:
            row = connection.execute(
                "SELECT open,high,low,close,volume,first_raw_sha256 FROM long_bars "
                "WHERE market=? AND ticker=? AND session_date=?",
                ("KRX", symbol, session),
            ).fetchone()
            if row is None:
                return {"status": "DAILY_SESSION_NOT_FOUND", "approved": False}
            source = connection.execute(
                "SELECT provider FROM raw_history WHERE raw_sha256=? LIMIT 1",
                (row[5],),
            ).fetchone()
    except sqlite3.Error as exc:
        raise KISReconciliationError("Read-only daily comparison query failed") from exc
    names = ("open", "high", "low", "close", "volume")
    reference = dict(zip(names, row[:5]))
    observed = {
        "open": candidate["continuous_open"],
        "high": candidate["continuous_high"],
        "low": candidate["continuous_low"],
        "close": candidate["closing_auction_price"],
        "volume": candidate["candidate_total_volume"],
    }
    differences = {
        k: {"historical_daily": reference[k], "minute_candidate": observed[k],
            "delta": observed[k] - reference[k]}
        for k in names if float(observed[k]) != float(reference[k])
    }
    return {
        "status": "SOURCE_DISAGREEMENT_HOLD" if differences else "MATCHED_UNVERIFIED",
        "approved": False,
        "reference_provider": source[0] if source else "unknown",
        "source_daily_sha256": row[5],
        "reference_daily": reference,
        "minute_candidate_daily": observed,
        "differences": differences,
        "note": "Provider coverage/corporate-action/time-label meaning unverified; "
                "matching OHLCV does not authorize trading/backtests",
    }


def inspect_snapshot(*, snapshot: str | Path, out_dir: str | Path,
                     daily_db: str | Path | None = None) -> dict:
    folder = Path(snapshot).expanduser().resolve()
    symbol, session, data, source_audit = _read_snapshot(folder)
    bars, candidate_daily = _candles(data, session)
    reconciliation = _reconcile_daily(
        daily_db, symbol=symbol, session=session, candidate=candidate_daily
    )
    output_root = Path(out_dir).expanduser().resolve()
    dest = output_root / f"KRX_5M_CANDIDATE_{symbol}_{session}"
    if dest.exists():
        raise KISReconciliationError("Immutable 5-minute candidate already exists")
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(bars)
    csv_blob = buffer.getvalue().encode("utf-8")
    report = {
        "status": "KIS_5MIN_CANDIDATE_UNVERIFIED_NO_BACKTEST",
        "symbol": symbol, "session": session, "source_snapshot": str(folder),
        "source_observations_sha256": source_audit["observations_sha256"],
        "source_page_sha256": source_audit["raw_sha256"],
        "candidate_csv_sha256": sha256(csv_blob).hexdigest(),
        "source_verified_date_only": True,
        "minute_label_semantics": "ASSUMED_BAR_START_NEEDS_VENDOR_VERIFICATION",
        "cntg_vol_semantics": "ASSUMED_PER_MINUTE_EXECUTED_VOLUME_NEEDS_VERIFICATION",
        "continuous_window": "09:00-15:20 KST (exclusive end)",
        "auction_isolated_from_continuous_bars": True,
        "daily": candidate_daily,
        "reference_reconciliation": reconciliation,
        "past_or_future_signal_data_written": False,
        "usable_for_backtesting": False,
        "usable_for_paper_or_live": False,
        "reason": "Single session, timestamps/volume semantics and official daily "
                  "provenance not independently approved",
    }
    output_root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".krx-5min-staging-", dir=output_root))
    try:
        (stage / "candles_5m_CANDIDATE_ONLY.csv").write_bytes(csv_blob)
        (stage / "audit.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)+"\n",
            encoding="utf-8",
        )
        if dest.exists():
            raise KISReconciliationError("Concurrent immutable snapshot collision")
        stage.rename(dest)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"output": str(dest), "audit": report}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline KIS raw snapshot reconciliation: no backtest, network or trading"
    )
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--daily-db")
    args = parser.parse_args(argv)
    try:
        result = inspect_snapshot(
            snapshot=args.snapshot, out_dir=args.out_dir, daily_db=args.daily_db
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (KISReconciliationError, OSError) as exc:
        # Local paths may contain sensitive contents; do not leak arbitrary
        # exception messages. Defined diagnostics have known, static messages.
        if isinstance(exc, KISReconciliationError):
            parser.exit(2, f"FAIL CLOSED: {exc}\n")
        parser.exit(2, "FAIL CLOSED: local file error\n")


if __name__ == "__main__":
    raise SystemExit(main())
