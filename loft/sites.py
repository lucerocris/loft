"""The login bank: the web accounts every agent's browser starts signed in to.

One list of logins (Seller Center, a helpdesk, the work Google account for Sheets, ...) and one
persistent Chrome profile, shared by every Loft account. You sign in to each site once, in
Loft › Vault › Logins or Loft › Browser (both stream that profile's headless Chrome into the app).
Every agent Loft starts, on any Claude account, then gets a browser whose profile is a copy of the
bank's (caches and locks left out), plus the bank's live cookies when it is running, and its system
prompt names the logins so it knows where to go. No passwords are stored: only the browser session.
Sign-in pages that refuse headless Chrome get "Open in a window": the same profile in a normal
Chrome window, closed when you're done.

  logins     <config>/sites.json                     {"version": 2, "sites": [{id, name, url, note}]}
  profile    <data>/browser/shared/profile            the signed-in Chrome profile (session cookies: 0700)
  record     <data>/browser/shared/master.json        {mode: "headless"|"window", pid, port, profile}

Version 1 kept one list and one profile per Loft account. The first load merges the lists, and the
profile of the account with the most sites becomes the bank's (copied; the old ones stay on disk).
"""
import json, os, re, secrets, shutil, signal, sqlite3, subprocess, threading, time
from pathlib import Path
from urllib.parse import urlparse

from . import accounts as A, browser as BR, paths, scope as S

BANK = "shared"
BANK_LABEL = "Login bank"
ACC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MAX_SITES = 40
_LOCK = threading.RLock()
_WINDOWS = {}   # profile path -> Popen of an open window (reaped when it closes)

# Left out of copies: caches (big, rebuilt on demand), the profile lock, crash dumps, and tabs to restore.
SKIP_NAMES = {"Cache", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache", "GraphiteDawnCache", "DawnCache",
              "DawnGraphiteCache", "DawnWebGPUCache", "component_crx_cache", "extensions_crx_cache", "Crashpad",
              "Crash Reports", "BrowserMetrics", "Safe Browsing", "OptimizationHints", "optimization_guide_model_store",
              "OnDeviceHeadSuggestModel", "segmentation_platform", "screen_ai", "SODA", "SODALanguagePacks",
              "WasmTtsEngine", "Sessions", "Session Storage", "CacheStorage", "ScriptCache", "blob_storage",
              "lockfile", "RunningChromeVersion"}


def skip(name):
    return name in SKIP_NAMES or name.startswith("Singleton") or name.endswith((".tmp", ".lock")) \
        or name.startswith(".org.chromium.")


# ------------------------------------------------------------------ the list
def _file():
    return paths.config_dir() / "sites.json"


def _load():
    try:
        d = json.loads(_file().read_text())
    except Exception:
        return {"version": 2, "sites": []}
    if isinstance(d.get("sites"), list):
        return d
    if isinstance(d.get("accounts"), dict):
        return _migrate(d)
    return {"version": 2, "sites": []}


def _save(d):
    paths.write_private(_file(), json.dumps(d, indent=1))


def _migrate(old):
    """Version 1 (a list and a profile per Loft account) to one bank. Runs once, on the first load."""
    with _LOCK:
        lists = {k: v for k, v in old["accounts"].items() if isinstance(v, list) and ACC_RE.match(k or "")}
        order = sorted(lists, key=lambda k: -len(lists[k]))
        seen, merged = set(), []
        for acc in order:
            for s in lists[acc]:
                key = (s.get("url") or "").rstrip("/").lower()
                if key and key not in seen:
                    seen.add(key)
                    merged.append({"id": s.get("id") or secrets.token_hex(4), "name": s.get("name") or key,
                                   "url": s["url"], "note": s.get("note", "")})
        best = next((a for a in order if lists[a] and (paths.data_dir() / "browser" / a / "profile").is_dir()), None)
        if best and not profile().is_dir():
            try:
                stop_master(best)   # its cookies written to disk, its profile free
            except Exception:
                pass
            seed(best, profile(), src=paths.data_dir() / "browser" / best / "profile")
        d = {"version": 2, "sites": merged[:MAX_SITES], "migratedFrom": best}
        _save(d)
        return d


def check_account(bank):
    """The bank's id. Older callers (the phone) still send a Loft account id: that means the bank too."""
    if bank == BANK or (isinstance(bank, str) and ACC_RE.match(bank) and A.get(bank)):
        return BANK
    raise ValueError("No such login bank.")


def clean_url(url):
    """https:// is added to a bare host. Only http and https; no credentials in the URL."""
    url = " ".join((url or "").split())
    if not url:
        raise ValueError("Give the site's address, e.g. https://app.example.com")
    m = re.match(r"([A-Za-z][A-Za-z0-9+.-]*):(?!\d)", url)
    if m and m.group(1).lower() not in ("http", "https"):   # javascript:, file:, chrome:, data: ...
        raise ValueError("Use a web address (http or https), e.g. https://app.example.com")
    if "://" not in url:
        url = "https://" + url
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname or "@" in u.netloc or len(url) > 2000:
        raise ValueError("Use a web address (http or https), e.g. https://app.example.com")
    return url


def clean_name(name, url):
    name = " ".join((name or "").split())[:60]
    return name or urlparse(url).hostname


def clean_note(note):
    return " ".join(str(note or "").split())[:160]


def items():
    """Every login in the bank: [{id, name, url, note}]."""
    return [dict(s, note=s.get("note", "")) for s in _load()["sites"]]


def counts():
    n = len(_load()["sites"])
    return {BANK: n} if n else {}


def add(name, url, note=""):
    url = clean_url(url)
    with _LOCK:
        d = _load()
        if len(d["sites"]) >= MAX_SITES:
            raise ValueError(f"That's {MAX_SITES} logins already. Remove one first.")
        if any(s["url"].rstrip("/").lower() == url.rstrip("/").lower() for s in d["sites"]):
            raise ValueError("That address is in the bank already.")
        site = {"id": secrets.token_hex(4), "name": clean_name(name, url), "url": url, "note": clean_note(note)}
        d["sites"].append(site)
        _save(d)
    return site


def update(sid, name=None, url=None, note=None):
    with _LOCK:
        d = _load()
        for s in d["sites"]:
            if s["id"] == sid:
                if url is not None:
                    s["url"] = clean_url(url)
                if name is not None:
                    s["name"] = clean_name(name, s["url"])
                if note is not None:
                    s["note"] = clean_note(note)
                _save(d)
                return dict(s)
    raise LookupError("No such login.")


def remove(sid):
    with _LOCK:
        d = _load()
        keep = [s for s in d["sites"] if s["id"] != sid]
        if len(keep) == len(d["sites"]):
            raise LookupError("No such login.")
        d["sites"] = keep
        _save(d)


def find(name, sites=None):
    """A login by id, name or host (what an agent passes to loft_logins), else the first name containing it."""
    n = " ".join(str(name or "").split()).lower()
    if not n:
        return None
    sites = items() if sites is None else sites
    for s in sites:
        if n in (s["id"].lower(), s["name"].lower(), (urlparse(s["url"]).hostname or "").lower()):
            return s
    return next((s for s in sites if n in s["name"].lower()), None)


def line(s):
    note = clean_note(s.get("note"))
    return f"{' '.join(str(s['name']).split())[:60]} ({s['url']}{'; ' + note if note else ''})"


def agent_note(sites):
    """The part of an agent's system prompt that names the bank's logins. Names, URLs and notes are the user's own."""
    return (f"Loft's login bank: the user signed in to these once, and your browser (the loft-browser tools) "
            f"started already signed in to them: {'; '.join(line(s) for s in sites)}. When a task touches one of "
            f"them, open it there; don't use another browser or tool for it, and don't look for credentials "
            f"elsewhere. loft_logins lists them with whether each still looks signed in. If one shows a sign-in "
            f"page or says the session expired, don't type or ask for a password: ask the user to sign in again "
            f"in Loft › Vault › Logins, then call loft_logins with refresh: true and retry.")


# ------------------------------------------------------------------ the master profile
def root(acc_id=BANK):
    if not ACC_RE.match(acc_id or ""):
        raise ValueError("No such login bank.")
    return paths.data_dir() / "browser" / acc_id


def profile(acc_id=BANK):
    return root(acc_id) / "profile"


def _rec_file(acc_id=BANK):
    return root(acc_id) / "master.json"


def _read(acc_id=BANK):
    try:
        return json.loads(_rec_file(acc_id).read_text())
    except Exception:
        return None


def _write(acc_id, rec):
    paths.write_private(_rec_file(acc_id), json.dumps(rec))


def _ours(pid, prof):
    """The pid is a live Chrome on this profile (pids get reused)."""
    if not pid:
        return False
    w = _WINDOWS.get(prof)
    if w is not None and w.pid == pid:
        return w.poll() is None
    try:
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return f"--user-data-dir={prof}" in cmd


def master(acc_id=BANK):
    """The running master browser's record ({mode, pid, port, profile}), or None."""
    rec = _read(acc_id)
    if not rec:
        return None
    if rec.get("mode") == "window":
        return rec if _ours(rec.get("pid"), rec.get("profile")) else None
    return rec if rec.get("port") and BR.alive(rec["port"]) else None


def ensure_master(acc_id=BANK):
    """The bank's signed-in browser, headless, started if needed. Raises while it is open in a window."""
    with _LOCK:
        rec = master(acc_id)
        if rec and rec["mode"] == "window":
            raise RuntimeError("The browser is open in a window. Close it first.")
        if rec:
            return rec
        prof = profile(acc_id)
        _private_dir(prof)
        port = BR.free_port()
        rec = {"mode": "headless", "port": port, "profile": str(prof), "pid": BR._spawn(port, str(prof))}
        _write(acc_id, rec)
        return rec


def _private_dir(d):
    d.mkdir(parents=True, exist_ok=True)
    for x in (d, d.parent):
        try:
            os.chmod(x, 0o700)
        except OSError:
            pass


def _gone(rec, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if not _ours(rec.get("pid"), rec.get("profile")):
            return True
        time.sleep(0.1)
    return False


def _shutdown(rec, timeout=8):
    """Close a master Chrome so its cookies are written and its profile is free again."""
    if not rec:
        return
    if rec.get("mode") == "headless" and rec.get("port"):
        try:   # the clean way: Chrome saves everything and exits
            cdp = BR.CDP(BR._get(rec["port"], "/json/version")["webSocketDebuggerUrl"], timeout=3)
            try:
                cdp.send("Browser.close")
            finally:
                time.sleep(0.2); cdp.close()
        except Exception:
            pass
        if _gone(rec, timeout):
            return
    pid = rec.get("pid")
    if pid and _ours(pid, rec.get("profile")):
        try:   # SIGTERM is a normal shutdown for Chrome too (cookies saved)
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        if not _gone(rec, timeout):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
            _gone(rec, 3)
    w = _WINDOWS.pop(rec.get("profile"), None)
    if w is not None:
        try:
            w.wait(timeout=1)
        except Exception:
            pass


def stop_master(acc_id=BANK):
    with _LOCK:
        _shutdown(master(acc_id))
        _rec_file(acc_id).unlink(missing_ok=True)


def open_window(acc_id=BANK, url=None):
    """The signed-in profile in a normal Chrome window on this machine, for sign-ins that refuse headless."""
    exe = BR.chrome_bin()
    if not exe:
        raise RuntimeError("No Chrome, Chromium, Edge or Brave found (set LOFT_CHROME).")
    url = clean_url(url) if url else "about:blank"
    with _LOCK:
        rec = master(acc_id)
        if rec and rec["mode"] == "window":
            return rec
        _shutdown(rec)
        prof = profile(acc_id)
        _private_dir(prof)
        p = subprocess.Popen(S.wrap(BR.chrome_args(exe, 0, str(prof), headed=True, url=url)), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        _WINDOWS[str(prof)] = p
        time.sleep(1.5)
        if p.poll() is not None:
            _WINDOWS.pop(str(prof), None)
            raise RuntimeError("Chrome didn't open a window (is there a desktop session?).")
        rec = {"mode": "window", "pid": p.pid, "profile": str(prof), "opened": time.time()}
        _write(acc_id, rec)
        return rec


def close_window(acc_id=BANK):
    with _LOCK:
        rec = master(acc_id)
        if not rec or rec["mode"] != "window":
            return False
        _shutdown(rec)
        _rec_file(acc_id).unlink(missing_ok=True)
        return True


# ------------------------------------------------------------------ cookies
COOKIE_KEYS = ("name", "value", "domain", "path", "secure", "httpOnly", "sameSite", "expires", "priority",
               "sourceScheme", "sourcePort", "partitionKey")


def _browser_cdp(port):
    return BR.CDP(BR._get(port, "/json/version")["webSocketDebuggerUrl"])


def live_cookies(acc_id=BANK):
    """All cookies of the running headless master, straight from Chrome; None if it isn't running."""
    rec = master(acc_id)
    if not rec or rec["mode"] != "headless":
        return None
    cdp = _browser_cdp(rec["port"])
    try:
        return cdp.call("Storage.getCookies")["cookies"]
    finally:
        cdp.close()


def cookie_params(cookies):
    out = []
    for c in cookies:
        p = {k: c[k] for k in COOKIE_KEYS if k in c}
        if c.get("session") or p.get("expires") in (-1, None, 0):
            p.pop("expires", None)
        if not isinstance(p.get("partitionKey"), dict):
            p.pop("partitionKey", None)
        out.append(p)
    return out


def set_cookies(port, cookies):
    """Put cookies into a running Chrome (all of them, browser-wide). Returns how many."""
    params = cookie_params(cookies)
    if not params:
        return 0
    cdp = _browser_cdp(port)
    try:
        try:
            cdp.call("Storage.setCookies", {"cookies": params})
        except RuntimeError:   # an older Chrome that doesn't know some field: plain cookies only
            plain = [{k: v for k, v in p.items() if k not in ("partitionKey", "sourceScheme", "sourcePort")}
                     for p in params if "partitionKey" not in p]
            cdp.call("Storage.setCookies", {"cookies": plain})
            return len(plain)
    finally:
        cdp.close()
    return len(params)


def inject_live(acc_id, port):
    """An agent's browser on `port` gets the running master's current cookies (copied files may lag)."""
    cookies = live_cookies(acc_id)
    return set_cookies(port, cookies) if cookies else 0


def _db_cookies(prof):
    """(domain, name, expires) from the profile's cookie database without starting Chrome. Values stay encrypted."""
    out = []
    for f in (Path(prof) / "Default" / "Network" / "Cookies", Path(prof) / "Default" / "Cookies"):
        if not f.is_file():
            continue
        try:
            con = sqlite3.connect(f"file:{f}?mode=ro&immutable=1", uri=True, timeout=1)
            try:
                rows = con.execute("SELECT host_key, name, expires_utc FROM cookies").fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            continue
        # Chrome stores microseconds since 1601; 0 = a session cookie
        out += [{"domain": h, "name": n, "expires": (e / 1e6 - 11644473600) if e else -1} for h, n, e in rows]
        break
    return out


def cookie_index(acc_id=BANK):
    """[(domain, name, expires)] for the account's signed-in profile: live if running, else from disk."""
    try:
        live = live_cookies(acc_id)
    except Exception:
        live = None
    if live is not None:
        return [{"domain": c["domain"], "name": c["name"], "expires": c.get("expires", -1)} for c in live]
    return _db_cookies(profile(acc_id))


def _matches(domain, host):
    d = (domain or "").lstrip(".").lower()
    host = (host or "").lower()
    return bool(d) and (host == d or host.endswith("." + d)) and "." in d


def session_hint(cookies, url):
    """How many cookies the profile holds for a site. Zero = surely signed out; some = probably signed in."""
    host = urlparse(url).hostname
    now = time.time()
    return sum(1 for c in cookies if _matches(c["domain"], host) and (c.get("expires", -1) in (-1, None) or c["expires"] > now))


# ------------------------------------------------------------------ agent profiles
def seed(acc_id, dest, src=None):
    """Copy the bank's signed-in profile to a new agent profile. Returns how many files were copied.
    Files that vanish or are locked mid-copy (the master may be running) are skipped."""
    src = Path(src) if src else profile(acc_id)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if not src.is_dir():
        return 0
    n = 0
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if not skip(d)]
        rel = Path(dirpath).relative_to(src)
        (dest / rel).mkdir(parents=True, exist_ok=True)
        for f in filenames:
            if skip(f):
                continue
            s = Path(dirpath) / f
            if s.is_symlink():
                continue
            try:
                shutil.copy2(s, dest / rel / f)
                n += 1
            except OSError:
                pass
    try:
        os.chmod(dest, 0o700)
    except OSError:
        pass
    return n


def status(sites=None):
    """The logins with a signed-in hint each: cookies = how many the profile holds for the site."""
    sites = items() if sites is None else [dict(x) for x in sites]
    if sites:
        try:
            idx = cookie_index()
        except Exception:
            idx = []
        for s in sites:
            s["cookies"] = session_hint(idx, s["url"])
            s["signedIn"] = s["cookies"] > 0
    return sites


def public(acc_id=BANK, with_hints=True):
    """The bank's logins and browser state, for the UI and the phone. `account` stays for older clients."""
    rec = master()
    sites = status() if with_hints else items()
    return {"account": BANK, "label": BANK_LABEL, "hex": "#6E737B", "sites": sites,
            "browser": {"running": bool(rec), "window": bool(rec and rec["mode"] == "window")},
            "profile": profile().is_dir()}
