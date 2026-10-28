"""Private, immutable failure receipt for the one-day KRX market-data pilot.

Only allowlisted metadata; NEVER include raw exception texts, KIS HTTP
headers, bearer tokens, provider responses, or unverified quote prices.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile

from .intraday_kis_venues import VenueProbeError

SESSION="2026-10-29"
WINDOWS=("morning","midday","afternoon")
PHASES=("AUTH","MARKET_READ","SOURCE_VALIDATION","AUDIT","INTERNAL")
KINDS=("book","ticks","minute")
STEPS=("STARTED","VERIFIED")
SYMBOLS=("005930","000660")
MAX_RESPONSES=12


def _files(root:Path,window:str)->tuple[Path,Path]:
    if window not in WINDOWS:
        raise VenueProbeError("Unknown KRX pilot failure window")
    return (root/f"{SESSION}_{window}_failure.json",
            root/f"{SESSION}_{window}_failure.sha256")


def write_failure(*,root:Path|str,window:str,phase:str,
                  observed_count:int,current_kind:str|None=None,
                  current_symbol:str|None=None,
                  current_round:int|None=None,
                  now:datetime|None=None)->dict:
    root=Path(root).expanduser().resolve()
    file,stamp=_files(root,window)
    if (phase not in PHASES or type(observed_count)!=int or
        not 0<=observed_count<=MAX_RESPONSES or
        current_kind not in (*KINDS,None) or
        current_symbol not in (*SYMBOLS,None) or
        (current_round is not None and
            (type(current_round)!=int or not 0<=current_round<2))):
        raise VenueProbeError("Unexpected KRX pilot failure metadata")
    moment=now or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise VenueProbeError("Aware failure receipt time required")
    from zoneinfo import ZoneInfo
    kst=moment.astimezone(ZoneInfo("Asia/Seoul"))
    if kst.date().isoformat()!=SESSION:
        raise VenueProbeError("Failure can only be recorded on the pilot session")
    if file.exists() or stamp.exists():
        raise VenueProbeError("Immutable KRX pilot failure already present")
    if not root.is_dir():
        raise VenueProbeError("Pilot root directory missing")
    if root.stat().st_mode & 0o077:
        raise VenueProbeError("Pilot failure root must be private")
    document={
        "schema":"KRX_PILOT_RESEARCH_FAILURE_RECEIPT_V1",
        "status":"FAILED_CAPTURE_OR_AUDIT_NOT_CERTIFIED",
        "session":SESSION,"window":window,
        "observed_market_responses_verified":observed_count,
        "expected_market_responses":MAX_RESPONSES,
        "failed_phase":phase,
        "current_request_symbol":current_symbol,
        "current_request_kind":current_kind,
        "current_request_round":current_round,
        "observed_at_kst":kst.isoformat(),
        "provider_error_body_recorded":False,
        "credential_or_bearer_recorded":False,
        "market_data_broker_fills":False,
        "orders_sent":0,"certified_strategy_returns":None,
        "retry_or_fake_backfill_authorized":False,
    }
    data=(json.dumps(document,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode()
    for destination,payload in ((file,data),(stamp,(sha256(data).hexdigest()+"\n").encode())):
        fd,tmp=tempfile.mkstemp(prefix=".krx-failure-",dir=root)
        try:
            os.fchmod(fd,0o600)
            with os.fdopen(fd,"wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            if destination.exists():
                raise VenueProbeError("Concurrent immutable failure receipt")
            os.link(tmp,destination,follow_symlinks=False)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)
    return document


def read_failure(*,root:Path|str,window:str)->dict|None:
    path=Path(root).expanduser().resolve()
    file,stamp=_files(path,window)
    if not file.exists() and not stamp.exists():
        return None
    if not file.exists() or not stamp.exists():
        raise VenueProbeError("Incomplete KRX failure receipt")
    raw=file.read_bytes()
    if (len(raw)>4096 or
        sha256(raw).hexdigest()!=stamp.read_text().strip()):
        raise VenueProbeError("KRX failure receipt checksum mismatch")
    try:doc=json.loads(raw)
    except (TypeError,ValueError,UnicodeError) as exc:
        raise VenueProbeError("Malformed KRX failure receipt") from exc
    if (not isinstance(doc,dict) or
        doc.get("schema")!="KRX_PILOT_RESEARCH_FAILURE_RECEIPT_V1" or
        doc.get("status")!="FAILED_CAPTURE_OR_AUDIT_NOT_CERTIFIED" or
        doc.get("session")!=SESSION or doc.get("window")!=window or
        doc.get("failed_phase") not in PHASES or
        doc.get("current_request_symbol") not in (*SYMBOLS,None) or
        doc.get("current_request_kind") not in (*KINDS,None) or
        doc.get("current_request_round") not in (0,1,None) or
        type(doc.get("observed_market_responses_verified"))!=int or
        not 0<=doc["observed_market_responses_verified"]<=MAX_RESPONSES or
        doc.get("expected_market_responses")!=MAX_RESPONSES or
        doc.get("market_data_broker_fills") is not False or
        doc.get("provider_error_body_recorded") is not False or
        doc.get("credential_or_bearer_recorded") is not False or
        doc.get("orders_sent")!=0 or
        doc.get("certified_strategy_returns") is not None or
        doc.get("retry_or_fake_backfill_authorized") is not False or
        set(doc)!={
            "schema","status","session","window",
            "observed_market_responses_verified","expected_market_responses",
            "failed_phase","current_request_symbol","current_request_kind",
            "current_request_round","observed_at_kst",
            "provider_error_body_recorded","credential_or_bearer_recorded",
            "market_data_broker_fills","orders_sent",
            "certified_strategy_returns","retry_or_fake_backfill_authorized",
        }):
        raise VenueProbeError("Unsafe or mutated KRX failure receipt")
    from zoneinfo import ZoneInfo
    try:
        recorded=datetime.fromisoformat(doc["observed_at_kst"])
        if (recorded.tzinfo is None or recorded.utcoffset() is None or
            recorded.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()!=SESSION):
            raise ValueError("Wrong pilot evidence date")
    except (TypeError,ValueError,KeyError) as exc:
        raise VenueProbeError("Invalid KRX failure receipt clock") from exc
    return doc
