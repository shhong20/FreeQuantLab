"""Private cross-process KIS access-token cache for read-only market research.

KIS's documented token is valid for 24h and should be reused across process
restarts. Atomic 0600 storage under a private 0700 directory; flock prevents
duplicate issuance. Never log access_token, appkey, secret or HTTP response.
No broker order endpoints are called here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Callable
from zoneinfo import ZoneInfo

import requests

from .intraday_kis_venues import VenueProbeError

TOKEN_URL="https://openapi.koreainvestment.com:9443/oauth2/tokenP"
KST=ZoneInfo("Asia/Seoul")
DEFAULT_CACHE=Path.home()/".config"/"freequantlab"/"auth_cache"
MIN_VALID_SECONDS=120
MAX_VALID_SECONDS=25*3600


def _private_dir(path:Path)->None:
    if path.is_symlink():
        raise VenueProbeError("KIS cache directory must not be a symlink")
    path.mkdir(parents=True,mode=0o700,exist_ok=True)
    if path.is_symlink():
        raise VenueProbeError("KIS cache directory must not be a symlink")
    st=path.stat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid!=os.getuid() or st.st_mode & 0o077:
        raise VenueProbeError("KIS cache directory requires owner-only permissions")


def _private_open(path:Path, *, flags:int, mode:int=0o600)->int:
    fd=os.open(path, flags|os.O_NOFOLLOW|os.O_CLOEXEC,mode)
    st=os.fstat(fd)
    if (not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or
        st.st_mode&0o077 or st.st_nlink!=1):
        os.close(fd)
        raise VenueProbeError("Insecure or aliased KIS token cache file")
    return fd


def _read_cached(cache:Path, identity:str, now:datetime) -> str|None:
    try:
        fd=_private_open(cache,flags=os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        with os.fdopen(fd,"r",encoding="utf-8") as f:
            # Refuse unbounded externally modified caches.
            blob=f.read(4097)
    except UnicodeError as exc:
        raise VenueProbeError("Malformed KIS token cache") from exc
    if len(blob)>4096:
        raise VenueProbeError("Oversized KIS token cache")
    try:
        data=json.loads(blob)
        token=data["access_token"]
        expiry=datetime.fromisoformat(data["expires_utc"])
        if (data["identity"]!=identity or not isinstance(token,str) or
            not 30<=len(token)<=2048 or
            expiry.tzinfo is None or expiry.utcoffset() is None):
            return None
        remaining=(expiry-now).total_seconds()
        return token if MIN_VALID_SECONDS<remaining<=MAX_VALID_SECONDS else None
    except (ValueError,TypeError,KeyError):
        return None


def _backoff_active(path:Path, identity:str, now:datetime) -> bool:
    try:
        fd=_private_open(path,flags=os.O_RDONLY)
    except FileNotFoundError:
        return False
    with os.fdopen(fd,"r",encoding="utf-8") as f:
        blob=f.read(2049)
    if len(blob)>2048:
        raise VenueProbeError("Oversized KIS OAuth cooldown marker")
    try:
        d=json.loads(blob)
        deadline=datetime.fromisoformat(d["retry_after_utc"])
        return (d.get("identity")==identity and
                deadline.tzinfo is not None and
                now<deadline<=now+timedelta(minutes=15))
    except (ValueError,TypeError,KeyError):
        return False


def _write_cached(cache:Path, content:dict)->None:
    payload=(json.dumps(content,sort_keys=True,ensure_ascii=True)+"\n").encode()
    if len(payload)>4096:raise VenueProbeError("KIS token response too large for private cache")
    name=None
    try:
        fd,name=tempfile.mkstemp(prefix=".tmp-kis-",dir=cache.parent)
        os.fchmod(fd,0o600)
        with os.fdopen(fd,"wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        if cache.is_symlink():
            raise VenueProbeError("KIS cache target must not be symlinked")
        if cache.exists():
            existing=_private_open(cache,flags=os.O_RDONLY)
            os.close(existing)
        os.replace(name,cache)
        name=None
        directory=os.open(cache.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if name and os.path.exists(name):os.unlink(name)


def _parse_expiry(data:dict, now:datetime)->datetime:
    raw=data.get("access_token_token_expired")
    if not isinstance(raw,str):
        raise VenueProbeError("KIS token response has no explicit expiry")
    try:
        expiry=datetime.strptime(raw,"%Y-%m-%d %H:%M:%S").replace(
            tzinfo=KST).astimezone(timezone.utc)
    except ValueError as exc:
        raise VenueProbeError("KIS token expiry timestamp malformed") from exc
    delta=(expiry-now).total_seconds()
    if not MIN_VALID_SECONDS<delta<=MAX_VALID_SECONDS:
        raise VenueProbeError("KIS token expiry outside accepted 24h window")
    return expiry


def get_token(*, key:str, secret:str, cache_dir:Path|str=DEFAULT_CACHE,
              issuer:Callable=requests.post,
              now:Callable=lambda:datetime.now(timezone.utc)) -> str:
    """Return cached token or issue exactly once; never retry failed OAuth.

    Cache scope binds to both key and secret, preventing cross-account reuse.
    Access tokens remain sensitive bearer credentials despite file chmod 0600.
    """
    if not isinstance(key,str) or not isinstance(secret,str) or not key or not secret:
        raise VenueProbeError("KIS app credentials not configured")
    parent=Path(cache_dir).expanduser()
    _private_dir(parent)
    identity=sha256((key+"\0"+secret).encode()).hexdigest()
    cache=parent/"kis_access_token.json"
    lock=parent/".kis_access_token.lock"
    cooldown=parent/".kis_oauth_cooldown.json"
    fd=_private_open(lock,flags=os.O_CREAT|os.O_RDWR)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        current=now()
        if current.tzinfo is None or current.utcoffset() is None:
            raise VenueProbeError("Timezone-aware KIS auth clock required")
        current=current.astimezone(timezone.utc)
        present=_read_cached(cache,identity,current)
        if present is not None:return present
        if _backoff_active(cooldown,identity,current):
            raise VenueProbeError("KIS OAuth 403 cooldown active; no retry")
        try:
            response=issuer(TOKEN_URL,
                            json={"grant_type":"client_credentials",
                                  "appkey":key,"appsecret":secret},
                            timeout=15)
            # Never include response text or request headers in exceptions.
            if response.status_code==403:
                _write_cached(cooldown,{"identity":identity,
                    "retry_after_utc":(current+timedelta(minutes=15)).isoformat()})
                raise VenueProbeError("KIS OAuth 403; retry suppressed for 15 minutes")
            if response.status_code!=200:
                raise VenueProbeError("KIS OAuth refused new issuance; no automatic retries")
            obj=response.json()
        except VenueProbeError:
            raise
        except (requests.RequestException,ValueError,TypeError,AttributeError):
            raise VenueProbeError("KIS OAuth unavailable; credentials hidden") from None
        if not isinstance(obj,dict):
            raise VenueProbeError("KIS OAuth response invalid")
        token=obj.get("access_token")
        if not isinstance(token,str) or not 30<=len(token)<=2048:
            raise VenueProbeError("Invalid KIS token response shape")
        expiry=_parse_expiry(obj,current)
        _write_cached(cache,{"schema":"KIS_TOKEN_CACHE_1","identity":identity,
                            "expires_utc":expiry.isoformat(),"access_token":token})
        return token
    finally:
        fcntl.flock(fd,fcntl.LOCK_UN)
        os.close(fd)

def inspect_cache(*, key:str,secret:str,cache_dir:Path|str=DEFAULT_CACHE,
                  now:Callable=lambda:datetime.now(timezone.utc)) -> dict:
    """Offline diagnostics. Never returns or prints the bearer token."""
    if not key or not secret:
        raise VenueProbeError("KIS app credentials not configured")
    parent=Path(cache_dir).expanduser()
    _private_dir(parent)
    identity=sha256((key+"\0"+secret).encode()).hexdigest()
    now_utc=now()
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise VenueProbeError("Timezone-aware KIS auth clock required")
    now_utc=now_utc.astimezone(timezone.utc)
    fd=_private_open(parent/".kis_access_token.lock",flags=os.O_CREAT|os.O_RDWR)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        usable=_read_cached(parent/"kis_access_token.json",identity,now_utc) is not None
        cooldown=_backoff_active(parent/".kis_oauth_cooldown.json",identity,now_utc)
        return {"status":("REUSABLE_TOKEN" if usable else
                          "REFRESH_BLOCKED_BY_403_COOLDOWN" if cooldown
                          else "TOKEN_MISSING_OR_EXPIRED"),
                "cache_owner_only":True,
                "token_value_exposed":False,
                "network_requests":0}
    finally:
        fcntl.flock(fd,fcntl.LOCK_UN)
        os.close(fd)


def main(argv:list[str]|None=None)->int:
    import argparse
    parser=argparse.ArgumentParser(description="KIS bearer cache OFFLINE status; no token shown")
    parser.add_argument("--status",action="store_true")
    args=parser.parse_args(argv)
    if not args.status:parser.exit(2,"Only --status is supported for offline inspection\n")
    try:
        result=inspect_cache(key=os.getenv("KIS_APP_KEY",""),
                             secret=os.getenv("KIS_APP_SECRET",""))
        print(json.dumps(result,sort_keys=True))
        return 0
    except VenueProbeError:
        parser.exit(2,"KIS private cache unavailable or insecure; no token displayed\n")


if __name__=="__main__":
    raise SystemExit(main())
