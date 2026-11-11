"""Per-account usage (5-hour and 7-day windows) and identity, from each login's own token.

`claude auth status` and `/usage` read the shared ~/.claude.json, so they can't
tell accounts apart. The token can: it is read from the account's login
(<dir>/.credentials.json on Linux, the Keychain on macOS) and used read-only
against Anthropic's OAuth usage/profile endpoints. Tokens are never written,
copied or cached; only the resulting numbers are cached.
"""
import json
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from . import accounts as A
from . import paths

API = "https://api.anthropic.com"
HEADERS = {"accept": "application/json", "anthropic-beta": "oauth-2025-04-20", "user-agent": "loft/3"}
STALE = 300  # seconds between refreshes per account


class TokenError(Exception):
    pass


def read_credentials(acc):
    """The raw credential JSON for an account, or None. Read-only."""
    value = A.env_value(acc)
    if paths.is_mac():
        try:
            r = subprocess.run(["/usr/bin/security", "find-generic-password", "-a", A.keychain_account(),
                                "-w", "-s", A.keychain_service(value)], capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
        if not value:  # default login: no dir file to fall back to
            value = A.default_dir()
    try:
        return (Path(value) / ".credentials.json").read_text()
    except (OSError, ValueError):
        return None


def token_info(acc):
    """{"token", "expired", "subscription"} or raises TokenError with a plain reason."""
    raw = read_credentials(acc)
    if not raw:
        where = "the Keychain" if paths.is_mac() else f"{A.env_value(acc)}/.credentials.json"
        raise TokenError(f"not signed in (no credential in {where})")
    try:
        o = json.loads(raw).get("claudeAiOauth") or {}
    except Exception:
        raise TokenError("credential is unreadable")
    tok = o.get("accessToken")
    if not tok:
        raise TokenError("credential has no OAuth token (API-key logins have no usage)")
    exp = o.get("expiresAt")
    return {"token": tok, "expired": bool(exp and exp / 1000 < time.time()), "subscription": o.get("subscriptionType")}


def _get(path, token, timeout=8):
    req = urllib.request.Request(API + path, headers={**HEADERS, "authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def profile(acc):
    """{"email", "name", "org"} for an account's login (network)."""
    t = token_info(acc)
    try:
        j = _get("/api/oauth/profile", t["token"])
    except Exception as e:
        raise TokenError(("token expired; it refreshes the next time claude runs as this account" if t["expired"]
                          else f"profile lookup failed ({e})"))
    a, o = j.get("account") or {}, j.get("organization") or {}
    return {"email": a.get("email") or "", "name": a.get("display_name") or a.get("full_name") or "", "org": o.get("name") or ""}


def parse_usage(j):
    """(five, seven, five_reset, seven_reset) from the usage response. Percent ints or None."""
    def pick(legacy, group):
        v = j.get(legacy) or {}
        if v.get("utilization") is not None:
            return round(v["utilization"]), v.get("resets_at") or ""
        lim = sorted([x for x in (j.get("limits") or []) if x.get("group") == group and x.get("percent") is not None],
                     key=lambda x: -x["percent"])
        return (round(lim[0]["percent"]), lim[0].get("resets_at") or "") if lim else (None, "")
    f, fr = pick("five_hour", "session")
    s, sr = pick("seven_day", "weekly")
    return f, s, fr, sr


def fetch(acc):
    t = token_info(acc)
    try:
        j = _get("/api/oauth/usage", t["token"])
    except Exception as e:
        raise TokenError("token expired" if t["expired"] else f"usage lookup failed ({e})")
    f, s, fr, sr = parse_usage(j)
    return {"five": f, "seven": s, "fiveReset": fr, "sevenReset": sr}


# ------------------------------------------------------------------ cache shared by CLI and server

_lock = threading.Lock()
_busy = set()


def _cache_file(acc_id):
    return paths.cache_dir() / f"usage-{acc_id}.json"


def cached(acc):
    try:
        f = _cache_file(acc["id"])
        return json.loads(f.read_text()), time.time() - f.stat().st_mtime
    except Exception:
        return {}, 1e9


def _refresh(acc):
    try:
        out = {**fetch(acc), "error": ""}
    except TokenError as e:
        out = {**cached(acc)[0], "error": str(e)}
    except Exception as e:
        out = {**cached(acc)[0], "error": f"{type(e).__name__}: {e}"}
    try:
        paths.cache_dir().mkdir(parents=True, exist_ok=True)
        _cache_file(acc["id"]).write_text(json.dumps(out))
    except OSError:
        pass
    finally:
        with _lock:
            _busy.discard(acc["id"])


def get(acc, wait=False):
    """Cached usage for an account; refreshes in the background when stale."""
    data, age = cached(acc)
    if age > STALE:
        with _lock:
            start = acc["id"] not in _busy
            _busy.add(acc["id"])
        if start:
            if wait:
                _refresh(acc)
                data, _ = cached(acc)
            else:
                threading.Thread(target=_refresh, args=(acc,), daemon=True).start()
    return {"five": data.get("five"), "seven": data.get("seven"), "fiveReset": data.get("fiveReset") or "",
            "sevenReset": data.get("sevenReset") or "", "error": data.get("error") or ""}
