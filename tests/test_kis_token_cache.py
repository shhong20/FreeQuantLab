"""KIS token reuse tests: cross-process ownership, expiry, OAuth refusal, no disclosure."""
from __future__ import annotations

from datetime import datetime,timedelta,timezone
from pathlib import Path
import json
import os
import stat

import pytest

from quantlab.kis_token_cache import get_token,inspect_cache
from quantlab.intraday_kis_venues import VenueProbeError

NOW=datetime.fromisoformat("2026-10-28T06:10:00+00:00")
VALID="bearer-"+"T"*80

class Reply:
    def __init__(self,status=200,token=VALID,expires="2026-10-29 14:10:00"):
        self.status_code=status
        self.token=token
        self.expires=expires
    def json(self):
        return {"access_token":self.token,
                "access_token_token_expired":self.expires}

def auth(cache,issuer,**kwargs):
    return get_token(key="MY-KIS-KEY",secret="MY-KIS-SECRET",
                     cache_dir=cache,issuer=issuer,now=lambda:NOW,**kwargs)

def test_reuses_across_independent_invocations_with_secure_permissions(tmp_path):
    path=tmp_path/"auth"
    calls=[]
    def issuer(url,**args):
        calls.append((url,args["timeout"]))
        assert "access_token" not in args["json"]
        return Reply()
    assert auth(path,issuer)==VALID
    assert auth(path,lambda *args,**kwargs:pytest.fail("Unexpected extra OAuth"))==VALID
    assert len(calls)==1
    assert stat.S_IMODE(path.stat().st_mode)==0o700
    for name in ("kis_access_token.json",".kis_access_token.lock"):
        assert stat.S_IMODE((path/name).stat().st_mode)==0o600
    data=json.loads((path/"kis_access_token.json").read_text())
    assert data["access_token"]==VALID
    assert "MY-KIS-SECRET" not in (path/"kis_access_token.json").read_text()

def test_different_app_credential_never_reuses_old_token(tmp_path):
    cache=tmp_path/"auth"
    assert auth(cache,lambda *a,**k:Reply())==VALID
    changed=get_token(key="MY-KIS-KEY",secret="CHANGED",
        cache_dir=cache,issuer=lambda *a,**k:Reply(token="bearer-"+ "Q"*80),
        now=lambda:NOW)
    assert changed!="bearer-"+"T"*80
    assert changed=="bearer-"+"Q"*80

def test_expiry_triggers_one_new_issue_without_logging_token(tmp_path):
    path=tmp_path/"auth"
    auth(path,lambda *a,**k:Reply(expires="2026-10-28 15:13:00"))
    value=get_token(key="MY-KIS-KEY",secret="MY-KIS-SECRET",cache_dir=path,
        issuer=lambda *a,**k:Reply(token="bearer-"+"N"*80),
        now=lambda:NOW+timedelta(minutes=4))
    assert value=="bearer-"+"N"*80

def test_oauth_forbidden_fails_closed_no_retry_and_no_secret_in_error(tmp_path):
    cache=tmp_path/"auth"
    calls=[]
    def issuer(*args,**kw):
        calls.append(1)
        return Reply(status=403)
    with pytest.raises(VenueProbeError) as caught:
        auth(cache,issuer)
    assert len(calls)==1
    assert "MY-KIS-SECRET" not in str(caught.value)
    assert VALID not in str(caught.value)
    assert not (cache/"kis_access_token.json").exists()
    with pytest.raises(VenueProbeError,match="cooldown"):
        auth(cache,lambda *a,**k:pytest.fail("OAuth called during cooldown"))
    marker=cache/".kis_oauth_cooldown.json"
    assert stat.S_IMODE(marker.stat().st_mode)==0o600
    assert "MY-KIS-KEY" not in marker.read_text()
    assert "MY-KIS-SECRET" not in marker.read_text()

def test_world_readable_token_cache_and_symlink_are_rejected(tmp_path):
    cache=tmp_path/"auth"
    auth(cache,lambda *a,**k:Reply())
    token=cache/"kis_access_token.json"
    token.chmod(0o644)
    with pytest.raises(VenueProbeError,match="Insecure"):
        auth(cache,lambda *a,**k:pytest.fail("network forbidden"))
    token.chmod(0o600)
    target=tmp_path/"bad-link"
    target.symlink_to(cache, target_is_directory=True)
    with pytest.raises(VenueProbeError,match="symlink"):
        auth(target,lambda *a,**k:pytest.fail("network forbidden"))
    token.unlink()
    token.symlink_to(tmp_path/"some-other")
    with pytest.raises((OSError,VenueProbeError)):
        auth(cache,lambda *a,**k:pytest.fail("network forbidden"))

def test_expiry_invalid_or_far_future_never_persisted(tmp_path):
    cache=tmp_path/"auth"
    with pytest.raises(VenueProbeError,match="expiry"):
        auth(cache,lambda *a,**k:Reply(expires="2026-11-01 15:10:00"))
    assert not (cache/"kis_access_token.json").exists()
    with pytest.raises(VenueProbeError,match="expiry"):
        auth(cache,lambda *a,**k:Reply(expires="BAD"))
    assert not (cache/"kis_access_token.json").exists()

def test_concurrent_threads_serialize_issuance_under_file_lock(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading,time
    calls=[]
    lock=threading.Lock()
    def issuer(*args,**kwargs):
        with lock:calls.append(1)
        time.sleep(.03)
        return Reply()
    cache=tmp_path/"auth"
    with ThreadPoolExecutor(max_workers=5) as pool:
        results=list(pool.map(lambda _:auth(cache,issuer),range(9)))
    assert results==[VALID]*9
    assert len(calls)==1


def test_legacy_data_token_path_uses_shared_cache(tmp_path,monkeypatch):
    import quantlab.data as data
    import quantlab.kis_token_cache as auth_cache
    calls=[]
    def fake_get_token(**kwargs):
        calls.append(kwargs)
        return VALID
    monkeypatch.setattr(auth_cache,"get_token",fake_get_token)
    monkeypatch.setenv("KIS_APP_KEY","MY-KIS-KEY")
    monkeypatch.setenv("KIS_APP_SECRET","MY-KIS-SECRET")
    assert data._kis_token()==VALID
    assert data._kis_token()==VALID
    assert len(calls)==2  # no forever-stale in-process lru_cache
    assert all(x["key"]=="MY-KIS-KEY" and x["secret"]=="MY-KIS-SECRET" for x in calls)

def test_offline_status_does_not_issue_or_disclose_token(tmp_path):
    cache=tmp_path/"auth"
    first=inspect_cache(key="MY-KIS-KEY",secret="MY-KIS-SECRET",
        cache_dir=cache,now=lambda:NOW)
    assert first["status"]=="TOKEN_MISSING_OR_EXPIRED"
    assert first["network_requests"]==0
    auth(cache,lambda *a,**k:Reply())
    second=inspect_cache(key="MY-KIS-KEY",secret="MY-KIS-SECRET",
        cache_dir=cache,now=lambda:NOW)
    assert second["status"]=="REUSABLE_TOKEN"
    assert "access_token" not in second
    assert VALID not in repr(second)
