"""KIS 2026 fixed-close phase rank collector safety and CLI tests."""
from __future__ import annotations

from datetime import datetime as RealDateTime, timezone
from hashlib import sha256
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import quantlab.intraday_kis_fixed_close_rank as app
from quantlab.intraday_kis_venues import VenueProbeError

KST = ZoneInfo("Asia/Seoul")
PRE = RealDateTime(2026,10,26,8,39,tzinfo=KST)
POST = RealDateTime(2026,10,26,15,55,tzinfo=KST)
DAY="2026-10-26"
SYMS=("005930","000660")


class Response:
    def __init__(self, data):
        self.content=json.dumps(data).encode()
    def raise_for_status(self):
        return None


def vendor_data(*, include_first=True, invalid=None):
    rows=[]
    if include_first:
        rows.append({
            "stck_shrn_iscd":"005930",
            "mkob_otcp_vol":"900",
            "mkfa_otcp_vol":"111",
        })
    rows.append({
        "stck_shrn_iscd":"000660",
        "mkob_otcp_vol":"33",
        "mkfa_otcp_vol":"222",
    })
    if invalid=="duplicate":
        rows.append(dict(rows[-1]))
    if invalid=="missing_number":
        rows[-1].pop("mkfa_otcp_vol")
    return {"rt_cd":"0","output":rows}


def make_getter(*, response=None, called=None):
    calls=called if called is not None else []
    def getter(url,**kw):
        assert url==app.URL
        assert kw["headers"]["tr_id"]==app.TR_ID
        assert kw["headers"]["authorization"]=="Bearer SAFE_TOKEN"
        assert kw["params"]["FID_COND_MRKT_DIV_CODE"]=="J"
        assert kw["params"]["FID_COND_SCR_DIV_CODE"]=="20176"
        assert kw["params"]["FID_INPUT_ISCD"]=="0000"
        assert kw["params"]["FID_RANK_SORT_CLS_CODE"] in ("1","2")
        assert "FID_INPUT_DATE_1" not in kw["params"]
        calls.append(kw["params"]["FID_RANK_SORT_CLS_CODE"])
        return Response(response or vendor_data())
    return getter


def collect(root,*,phase="PRE",when=PRE,response=None):
    return app.capture(
        session=DAY,phase=phase,symbols=SYMS,root=root,
        key="SAFE_KEY",secret="SAFE_SECRET",token="SAFE_TOKEN",
        getter=make_getter(response=response),now=when
    )


def test_different_phase_has_correct_rank_code_and_immutable_raw(tmp_path):
    req=[]
    for phase,now,expected in (("PRE",PRE,"1"),("POST",POST,"2")):
        result=app.capture(
            session=DAY,phase=phase,symbols=SYMS,root=tmp_path,
            key="SAFE_KEY",secret="SAFE_SECRET",token="SAFE_TOKEN",
            getter=make_getter(called=req),now=now,
        )
        report=result["audit"]
        assert report["phase"]==phase
        assert report["source_is_not_historical_execution_tape"] is True
        assert report["ranking_pagination_exhaustively_retrieved"] is False
        assert report["can_reconcile_krx_daily_gap"] is False
        assert report["usable_for_backtesting"] is False
        assert report["target_observations"][0]["observed_phase_volume_candidate"] == (
            900 if phase=="PRE" else 111
        )
        stored=app.inspect(root=tmp_path,session=DAY,phase=phase,symbols=SYMS)
        assert stored["captured_at_kst"]==now.isoformat()
        assert sha256((Path(result["output"])/"raw.json").read_bytes()).hexdigest()==stored["raw_sha256"]
        assert "SAFE_SECRET" not in (Path(result["output"])/"audit.json").read_text()
        assert "SAFE_TOKEN" not in (Path(result["output"])/"audit.json").read_text()
        with pytest.raises(VenueProbeError,match="already exists"):
            collect(tmp_path,phase=phase,when=now)
    assert req==["1","2"]


def test_missing_from_rank_not_equivalent_to_zero_volume(tmp_path):
    result=collect(tmp_path,response=vendor_data(include_first=False))
    observations=result["audit"]["target_observations"]
    assert observations[0]["present_in_returned_ranking"] is False
    assert observations[0]["observed_phase_volume_candidate"] is None
    assert observations[0]["missing_not_a_zero_volume_trade"] is True
    assert observations[1]["observed_phase_volume_candidate"]==33


def test_missing_field_from_rank_not_a_zero(tmp_path):
    result=collect(tmp_path,phase="POST",when=POST,
                   response=vendor_data(invalid="missing_number"))
    row=result["audit"]["target_observations"][1]
    assert row["present_in_returned_ranking"] is True
    assert row["observed_phase_volume_candidate"] is None
    assert row["invalid_or_absent_number_not_a_zero_volume_trade"] is True


@pytest.mark.parametrize("bad",[
    RealDateTime(2026,10,26,8,29,tzinfo=KST),
    RealDateTime(2026,10,26,9,10,tzinfo=KST),
    RealDateTime(2026,10,25,8,39,tzinfo=KST),
    RealDateTime(2026,10,24,8,39,tzinfo=KST),
    RealDateTime(2026,10,26,8,39),
])
def test_pre_session_is_strictly_clock_and_day_bound(bad):
    with pytest.raises(VenueProbeError):
        app.validate(session=DAY,phase="PRE",symbols=SYMS,captured_at=bad)


def test_post_does_not_accept_pre_capture_time():
    with pytest.raises(VenueProbeError):
        app.validate(session=DAY,phase="POST",symbols=SYMS,captured_at=PRE)


def test_replay_hash_mismatch_is_detected(tmp_path):
    result=collect(tmp_path)
    file=Path(result["output"])/"raw.json"
    file.write_bytes(file.read_bytes()+b"TAMPER")
    with pytest.raises(VenueProbeError,match="SHA"):
        app.inspect(root=tmp_path,session=DAY,phase="PRE",symbols=SYMS)


def test_wrong_symbols_or_duplicate_in_rank_fails_closed(tmp_path):
    with pytest.raises(VenueProbeError):
        app.validate(session=DAY,phase="PRE",symbols=("../A",),captured_at=PRE)
    with pytest.raises(VenueProbeError):
        collect(tmp_path,response=vendor_data(invalid="duplicate"))
    assert not (tmp_path/f"KIS_AFTER_HOUR_RANK_J_{DAY}_PRE").exists()


def test_http_provider_denial_does_not_expose_raw_msg(tmp_path):
    denied={
        "rt_cd":"1","msg_cd":"DONT_EXPOSE_THAT",
        "msg1":"KIS SECRET CONTENT NEVER LOG THIS",
    }
    with pytest.raises(VenueProbeError) as exc:
        collect(tmp_path,response=denied)
    assert "KIS SECRET" not in str(exc.value)


def test_live_cli_with_mocked_oauth_get_and_resume_no_second_oauth(
    tmp_path, monkeypatch, capsys
):
    class Frozen(RealDateTime):
        @classmethod
        def now(cls,tz=None):
            return PRE.astimezone(tz) if tz is not None else PRE
    monkeypatch.setattr(app,"datetime",Frozen)
    monkeypatch.setenv("KIS_APP_KEY","LOCAL_TEST_KEY")
    monkeypatch.setenv("KIS_APP_SECRET","LOCAL_TEST_SECRET")
    token_calls=[]
    monkeypatch.setattr(app,"_kis_token",
                        lambda:token_calls.append(1) or "SAFE_TOKEN")
    requests=[]
    def get(url,**kw):
        requests.append(kw["params"]["FID_RANK_SORT_CLS_CODE"])
        return make_getter()(url,**kw)
    monkeypatch.setattr(app,"_get",lambda url,**kw: get(url,**kw).content)
    cmd=["--phase","PRE","--expected-session",DAY,
         "--out-dir",str(tmp_path),"--execute"]
    assert app.main(cmd)==0
    summary=json.loads(capsys.readouterr().out)
    assert summary["target_observations"][0]["observed_phase_volume_candidate"]==900
    assert token_calls==[1]
    assert requests==["1"]
    monkeypatch.setattr(app,"_kis_token",lambda:pytest.fail("resume OAuth issued"))
    assert app.main(cmd)==0
    resumed=json.loads(capsys.readouterr().out)
    assert resumed["status"]=="EXISTING_VERIFIED"
    assert requests==["1"]
    assert token_calls==[1]
