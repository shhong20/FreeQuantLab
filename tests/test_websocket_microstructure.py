"""Websocket public-only multi-market capture, temporal joins & restart safety."""
from __future__ import annotations

from datetime import datetime,timezone
import json
import sqlite3
import pytest

from quantlab.continuous_portfolio import ASSETS
from quantlab.websocket_microstructure import (
    validate_frame,subscription,append_validated,open_db,capture,audit,
)

BASE=1792565549000


def book(asset="KRW-BTC",ts=BASE,stream="REALTIME"):
    return {
        "type":"orderbook","code":asset,"timestamp":ts,"level":0,
        "stream_type":stream,"orderbook_units":[
            {"ask_price":101.+i,"bid_price":99.-i,
             "ask_size":100.,"bid_size":100.} for i in range(30)],
        "total_ask_size":3000.,"total_bid_size":3000.,
    }


def trade(asset="KRW-BTC",ts=BASE+120,id=100):
    return {"type":"trade","code":asset,"timestamp":ts+2,
            "trade_timestamp":ts,"trade_price":100.,"trade_volume":.5,
            "ask_bid":"BID","sequential_id":id,"stream_type":"REALTIME"}


def encode(x):
    return json.dumps(x).encode()


def test_subscription_is_public_unaggregated_no_private_order_stream():
    payload=json.loads(subscription("test-only"))
    assert payload[1]["type"]=="orderbook"
    assert payload[1]["codes"]==[s+".30" for s in ASSETS]
    assert payload[2]["type"]=="trade"
    assert payload[2]["is_only_realtime"] is True
    assert payload[-1]=={"format":"DEFAULT"}
    assert not any(x.get("type") in ("myOrder","myAsset") for x in payload)


def test_causality_and_unique_trade_id_even_when_trade_seq_not_monotone(tmp_path):
    p=tmp_path/"ws.db"
    c=open_db(p)
    with c:
        c.execute("""INSERT INTO sessions
                  (id,started_ms,status,intended_seconds,endpoint,mode)
                  VALUES('s',?,'RUNNING',20,'wss://api.upbit.com/websocket/v1','PUBLIC_READ_ONLY')""",(BASE,))
    counts={"frames":0,"books":0,"trades":0,"duplicates":0,
            "causal_trade_links":0,"unlinked_trades":0}
    state={}
    with c:
        b=validate_frame(encode(book()),BASE+100)
        x=append_validated(c,"s",b,state,counts)
        assert x["seq"]>0
        first=append_validated(c,"s",validate_frame(encode(trade(id=900)),BASE+200),state,counts)
        assert first["status"]=="CAUSAL_VISIBLE_L2_REFERENCE_ONLY"
        assert first["seq"]>x["seq"]
        # Trade IDs can arrive in any ordering; only uniqueness counts.
        late=append_validated(c,"s",validate_frame(encode(trade(ts=BASE+180,id=100)),BASE+240),state,counts)
        assert late["status"]=="CAUSAL_VISIBLE_L2_REFERENCE_ONLY"
        duplicate=append_validated(c,"s",validate_frame(encode(trade(id=900)),BASE+260),state,counts)
        assert duplicate["status"]=="DUPLICATE_PUBLIC_TRADE"
        unlinked=append_validated(c,"s",validate_frame(encode(trade(ts=BASE+2500,id=901)),BASE+2600),state,counts)
        assert unlinked["status"]=="NO_CAUSAL_BOOK"
        # receipt time alone is insufficient: a trade timestamp from
        # BEFORE the best book must not match it.
        past=append_validated(c,"s",validate_frame(encode(trade(ts=BASE-100,id=902)),BASE+2700),state,counts)
        assert past["status"]=="NO_CAUSAL_BOOK"
        c.execute("UPDATE sessions SET status='BOUNDED_CAPTURE_COMPLETE',frame_count=?,duplicated_trade_ids=? WHERE id='s'",
                  (counts["frames"],counts["duplicates"]))
    c.close()
    summary=audit(p)
    assert summary["book_frames"]==1
    assert summary["market_trade_frames"]==4
    assert summary["trade_to_prior_book_links"]==2
    assert summary["raw_sha256_integrity"]=="PASS"


def test_invalid_wrong_market_snapshot_trade_timestamp_and_depth():
    bad=book(asset="KRW-UNKNOWN")
    with pytest.raises(ValueError):
        validate_frame(encode(bad),BASE+200)
    bad=book()
    bad["orderbook_units"][0]["ask_size"]=-1
    with pytest.raises(ValueError):
        validate_frame(encode(bad),BASE+200)
    with pytest.raises(ValueError,match="Trade snapshot"):
        validate_frame(encode(dict(trade(),stream_type="SNAPSHOT")),BASE+200)
    with pytest.raises(ValueError,match="future"):
        validate_frame(encode(trade(ts=BASE+10000)),BASE)
    with pytest.raises(ValueError,match="Stale"):
        validate_frame(encode(trade(ts=BASE-30000)),BASE)
    with pytest.raises(ValueError,match="WebSocket frame size"):
        validate_frame(b"x"*200001,BASE+200)


class SimClock:
    def __init__(self):
        self.elapsed=0.
    def mono(self):
        return self.elapsed
    def ms(self):
        return BASE+int(self.elapsed*1000)


class DummySocket:
    def __init__(self,clock:SimClock,frames):
        self.clock=clock
        self.frames=list(frames)
        self.sent=[]
        self.closed=False
    def send(self,data):
        self.sent.append(data)
    def settimeout(self,x):
        assert 2<=x<=3
    def recv(self):
        self.clock.elapsed+=0.25
        if self.frames:
            return encode(self.frames.pop(0))
        self.clock.elapsed+=1.
        raise TimeoutError()
    def close(self):self.closed=True


def test_realtime_complete_4_market_frames_and_replay_dedupe(tmp_path):
    t=SimClock()
    frames=[]
    for s in ASSETS:
        frames.append(book(s,BASE+int(.1*1000)))
        frames.append(trade(s,BASE+int(.16*1000),id=100+ASSETS.index(s)))
    frames.append(trade("KRW-BTC",BASE+int(.16*1000),id=100))
    sock=DummySocket(t,frames)
    p=tmp_path/"sealed.db"
    r=capture(p,duration=5,socket_factory=lambda:sock,clock=t.ms,mono=t.mono)
    assert r["status"]=="BOUNDED_CAPTURE_COMPLETE",r
    assert r["books"]==4 and r["trades"]==4
    assert r["duplicates"]==1
    assert r["causal_trade_links"]==4
    assert sock.closed
    assert json.loads(sock.sent[0])[2]["type"]=="trade"
    proof=audit(p)
    assert proof["complete_sessions"]==1
    assert proof["trade_to_prior_book_links"]==4
    assert not proof["real_orders_enabled"]
    assert proof["still_no_verified_own_fills"]


def test_tampered_immutable_raw_detected_on_separate_database(tmp_path):
    t=SimClock()
    sock=DummySocket(t,[book(),trade()])
    db=tmp_path/"tamper.sqlite"
    r=capture(db,duration=5,socket_factory=lambda:sock,clock=t.ms,mono=t.mono)
    assert r["books"]==1 and r["trades"]==1
    with sqlite3.connect(db) as c:
        c.execute("UPDATE frames SET raw=? WHERE kind='trade'",(b'broken',))
    with pytest.raises(ValueError,match="Tampered"):
        audit(db)


def test_disconnect_reconnect_never_reuses_old_quote(tmp_path):
    t=SimClock()
    class FailOnce(DummySocket):
        def recv(self):
            if self.frames:
                return super().recv()
            raise ConnectionResetError("reconnect")
    first=FailOnce(t,[book()])
    second=DummySocket(t,[trade()])
    sockets=iter((first,second))
    r=capture(tmp_path/"recover.db",duration=5,socket_factory=lambda:next(sockets),
              clock=t.ms,mono=t.mono)
    assert r["reconnects"]==1
    assert r["unlinked_trades"]==1
    assert r["status"]=="CAPTURE_DEGRADED_GAPS_OR_BAD_MESSAGES"
    assert audit(tmp_path/"recover.db")["trade_to_prior_book_links"]==0


def test_invalid_duration_fails_closed_before_any_socket_io(tmp_path):
    with pytest.raises(ValueError,match="bounded"):
        capture(tmp_path/"no.db",duration=120,socket_factory=lambda:None)
    assert not (tmp_path/"no.db").exists()


def test_forged_future_depth_reference_fails_offline_audit(tmp_path):
    clock=SimClock()
    socket=DummySocket(clock,[book(),trade()])
    p=tmp_path/"forged.db"
    r=capture(p,duration=5,socket_factory=lambda:socket,
              clock=clock.ms,mono=clock.mono)
    assert r["causal_trade_links"]==1
    with sqlite3.connect(p) as c:
        # Wrong lag and wrong temporal contract, even with preserved hashes.
        c.execute("UPDATE frames SET asof_exchange_lag_ms=99999 WHERE kind='trade'")
    with pytest.raises(ValueError,match="Invalid lookahead"):
        audit(p)


def test_separate_capture_sessions_preserve_trade_deduplication(tmp_path):
    path=tmp_path/"dedup.db"
    a=SimClock()
    b=SimClock()
    first=capture(path,duration=5,socket_factory=lambda:DummySocket(a,[book(),trade()]),
                  clock=a.ms,mono=a.mono)
    second=capture(path,duration=5,socket_factory=lambda:DummySocket(b,[book(),trade()]),
                   clock=b.ms,mono=b.mono)
    assert first["trades"]==1
    assert second["duplicates"]==1
    assert second["trades"]==0
    assert second["status"]=="INSUFFICIENT_MARKET_TYPES"
    q=audit(path)
    assert q["sessions"]==2 and q["market_trade_frames"]==1
    assert q["degraded_or_failed_sessions"]==1


def test_public_websocket_service_is_bounded_and_independent():
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]/"deploy"/"systemd"
    x=(root/"freequant-websocket.service").read_text()
    y=(root/"freequant-websocket.timer").read_text()
    z=(root/"freequant-websocket-audit.service").read_text()
    assert "WorkingDirectory=/home/hong/apps/FreeQuantLab-observer-runtime" in x
    assert "--seconds 30" in x and "/usr/bin/flock -n" in x
    assert "ReadWritePaths=/home/hong/apps/FreeQuantLab/state/microstructure" in x
    assert "ProtectSystem=strict" in x and "NoNewPrivileges=true" in x
    assert "OnCalendar=*-*-* *:2/10:00" in y
    assert "Persistent=true" in y
    assert "--audit --audit-hours 2 --require-recent-seconds 2400" in z
    assert "websocket_quality --enforce" in z
    assert "freequant-shadow" not in x+y+z
