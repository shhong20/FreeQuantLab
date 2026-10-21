"""Upbit public tape integrity; fills on this tape aren't hypothetical orders."""
from datetime import datetime, timezone, timedelta
import json
from hashlib import sha256

import pytest

from quantlab.continuous_portfolio import ASSETS
from quantlab.live_trade_evidence import (
    check_trade_prints,collect_trade_prints,audit_saved_prints,URL,
)


def prints(market,now):
    t=int(now.timestamp()*1000)
    return [
        {"market":market,"timestamp":t-i*1000,"trade_price":101.0,
         "trade_volume":.5,"sequential_id":10000+i,
         "ask_bid":"BID" if i%2==0 else "ASK"}
        for i in range(5)
    ]


def test_mock_frozen_public_trades_without_private_order_inference(tmp_path):
    now=datetime.now(timezone.utc)
    class Response:
        def __init__(self,market):
            self.content=json.dumps(prints(market,now)).encode()
        def raise_for_status(self): pass
    class Session:
        def get(self,url,**kwargs):
            assert url==URL
            assert kwargs["params"]["count"]==200
            assert "Authorization" not in kwargs["headers"]
            return Response(kwargs["params"]["market"])
    root=tmp_path/"trades"
    result=collect_trade_prints(root,session=Session())
    assert result["capture_count"]==4
    assert not result["real_orders_enabled"]
    assert result["past_trades_not_proof_of_counterfactual_fill"]
    assert audit_saved_prints(root)["count"]==4
    with pytest.raises(ValueError,match="overwritten"):
        collect_trade_prints(root,session=Session())
    (root/"prints-KRW-ETH.json").write_text("tampered")
    with pytest.raises(ValueError,match="SHA"):
        audit_saved_prints(root)


def test_trade_ids_are_unique_and_freshness_is_latest_not_oldest():
    now=datetime.now(timezone.utc)
    p=prints("KRW-ADA",now)
    p[-1]["timestamp"]-=2*3600*1000
    proof=check_trade_prints(p,"KRW-ADA",now)
    assert proof["trades"]==5
    assert proof["trade_window_seconds"]>=7200
    p[1]["sequential_id"]=p[0]["sequential_id"]
    with pytest.raises(ValueError,match="Duplicate"):
        check_trade_prints(p,"KRW-ADA",now)


def test_stale_missing_market_and_negative_trade_refused():
    now=datetime.now(timezone.utc)
    p=prints("KRW-BTC",now)
    p[0]["timestamp"]-=9*86400*1000
    with pytest.raises(ValueError,match="Stale"):
        check_trade_prints(p,"KRW-BTC",now)
    p=prints("KRW-BTC",now)
    p[0]["trade_volume"]=-.1
    with pytest.raises(ValueError,match="Invalid"):
        check_trade_prints(p,"KRW-BTC",now)
    p=prints("KRW-BTC",now)
    with pytest.raises(ValueError,match="Wrong"):
        check_trade_prints(p,"KRW-XRP",now)
    p=prints("KRW-BTC",now)
    p[0]["ask_bid"]="UNKNOWN"
    with pytest.raises(ValueError,match="Unknown"):
        check_trade_prints(p,"KRW-BTC",now)
