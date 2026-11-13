#!/usr/bin/env python3
"""Loft (was cagents) app server: a tiny localhost API + one HTML page for the agents board.

Stdlib only. The rules (accounts, cards, actions, usage) live in the loft/
package next to this file, shared with the `loft` CLI and terminal board.

  python3 server.py [--port 7717] [--phone-port 7717] [--tailnet | --no-tailnet]
                    [--dev-loopback-devices]

Binds 127.0.0.1. Every API call there must carry the per-run token that is
baked into the served page (X-Cagents-Token), so other sites in the browser
can't drive it.

Phone access (Loft Mobile) is a second listener bound to this machine's
Tailscale IPv4 only (never 0.0.0.0). It is off by default; the desktop "Phone
access" switch turns it on and the choice is remembered in
~/.config/loft/config.json. That listener:
  * refuses peers outside 100.64.0.0/10,
  * accepts only `Authorization: Bearer <device token>` (per-phone tokens,
    stored as sha256 in ~/.config/loft/devices.json, mode 0600, revocable),
  * pins each token to the Tailscale node that first used it (tailscale whois)
    and only accepts nodes owned by this machine's own Tailscale user,
  * rate-limits failed auth (5 a minute -> 429 for 10 minutes),
  * never serves the desktop page, static files or the pairing routes.
--dev-loopback-devices (OFF by default) also accepts device tokens on
127.0.0.1, so an Android emulator can reach the server through 10.0.2.2.
"""
import base64, re, fcntl, ipaddress, pty, select, signal, socket, struct, termios, hashlib, importlib.machinery, importlib.util, json, os, secrets, shutil, subprocess, sys, threading, time
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse


def _arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


HOME = Path.home()
HERE = Path(__file__).resolve().parent
PORT = int(_arg("--port", 7717))
PHONE_PORT = int(_arg("--phone-port", PORT or 7717))
DEV_LOOPBACK_DEVICES = "--dev-loopback-devices" in sys.argv
TOKEN = ""  # set below: kept in the config dir so open windows survive a restart
# Never served. Kept in a 0600 file so the desktop app can challenge the server on the port
# (sha256(PROOF_KEY:nonce)) and know it is this user's live Loft, not a replay of TOKEN.
PROOF_KEY = secrets.token_hex(32)
CLAUDE = str(HOME / ".local/bin/claude")
VERSION = "2.1"
HOSTNAME = socket.gethostname().split(".")[0]
CONF_DIR = None  # set below from loft.paths (LOFT_CONF_DIR, XDG_CONFIG_HOME, ~/.config/loft)
TAILNET = ipaddress.ip_network("100.64.0.0/10")


sys.path.insert(0, str(HERE))
from loft import accounts as A, board as B, browser as BR, core as C, paths, projects as P, prompts as PR, transcript as TR, usage as U, webapi  # noqa: E402

CLAUDE = paths.claude_bin()
CONF_DIR = paths.config_dir()
LOCK = threading.Lock()  # one mutating action at a time
VENV_PY = paths.venv_python()


def _desktop_token():
    """The desktop page's token, the same across restarts.

    A new token on every start broke every open Loft window ("bad token", dead
    terminal) until it was reloaded. It is stored 0600 next to the rest of
    Loft's config; delete the file to rotate it.
    """
    f = CONF_DIR / "desktop-token"
    try:
        tok = f.read_text().strip()
        if re.fullmatch(r"[0-9a-f]{32}", tok):
            return tok
    except OSError:
        pass
    tok = secrets.token_hex(16)
    try:
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(tok + "\n")
        os.replace(tmp, f)
    except OSError:
        pass  # read-only config: fall back to a per-run token
    return tok


TOKEN = _desktop_token()


class Voice:
    """Lazily started local Whisper worker (voice_worker.py in the loft venv).

    One worker, one request at a time (the lock), shared by the desktop's
    POST /api/voice and the phone's live /ws/voice stream.
    """
    proc = None
    lock = threading.Lock()

    @classmethod
    def available(cls):
        return VENV_PY.exists()

    @classmethod
    def _request(cls, data, suffix, req):
        if not cls.available():
            raise RuntimeError("Voice isn't set up: ~/.local/share/loft/venv is missing.")
        import tempfile
        with cls.lock:
            if cls.proc is None or cls.proc.poll() is not None:
                cls.proc = subprocess.Popen([str(VENV_PY), str(HERE / "voice_worker.py")], stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
                ready = cls.proc.stdout.readline()
                if not ready:
                    cls.proc = None
                    raise RuntimeError("The voice model failed to load.")
            fd, path = tempfile.mkstemp(suffix=suffix, prefix="loft-voice-")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            cls.proc.stdin.write(json.dumps({**req, "path": path}) + "\n"); cls.proc.stdin.flush()
            out = json.loads(cls.proc.stdout.readline() or '{"error": "voice worker stopped"}')
        if "error" in out:
            raise RuntimeError(out["error"])
        return out["text"]

    @classmethod
    def transcribe(cls, audio, suffix=".webm"):
        """A whole recorded clip (webm/ogg/m4a/wav), final quality."""
        return cls._request(audio, suffix, {})

    @classmethod
    def transcribe_pcm(cls, pcm, mode="final"):
        """Raw PCM16LE 16 kHz mono. mode="partial" is the quick live pass."""
        return cls._request(pcm, ".pcm", {"raw": True, "mode": mode})


AUDIO_EXT = {"ogg": ".ogg", "mp4": ".m4a", "m4a": ".m4a", "aac": ".m4a", "x-m4a": ".m4a", "wav": ".wav",
             "x-wav": ".wav", "mpeg": ".mp3", "webm": ".webm"}


def audio_suffix(ctype):
    sub = (ctype or "").split(";")[0].strip().lower().rsplit("/", 1)[-1]
    return AUDIO_EXT.get(sub, ".webm")


# ------------------------------------------------------------ phone access
def _write_private(path, data):
    """Atomic write, mode 0600, in a 0700 directory."""
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONF_DIR, 0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_config():
    try:
        return json.loads((CONF_DIR / "config.json").read_text())
    except Exception:
        return {}


def save_config(cfg):
    _write_private(CONF_DIR / "config.json", cfg)


DEV_LOCK = threading.Lock()
_SEEN = {}


def load_devices():
    try:
        return json.loads((CONF_DIR / "devices.json").read_text())
    except Exception:
        return []


def save_devices(devs):
    _write_private(CONF_DIR / "devices.json", devs)


def _sha(tok):
    return hashlib.sha256(tok.encode()).hexdigest()


def device_for_token(tok):
    """The device record whose hash matches tok, or None. Constant-time compare."""
    if not tok or len(tok) > 200:
        return None
    h, found = _sha(tok), None
    for d in load_devices():
        if secrets.compare_digest(h, d.get("sha256", "")):
            found = d
    return found


def touch_device(dev_id, **fields):
    with DEV_LOCK:
        devs = load_devices()
        for d in devs:
            if d["id"] == dev_id:
                d.update(fields)
        save_devices(devs)


def public_device(d):
    return {k: d.get(k) for k in ("id", "name", "created", "last_seen", "last_ip", "node_name")}


def pair_device(name):
    tok = secrets.token_urlsafe(32)
    dev = {"id": "d" + secrets.token_hex(4), "name": (name or "phone").strip()[:40] or "phone", "sha256": _sha(tok),
           "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "last_seen": None, "last_ip": None,
           "node": None, "node_name": None}
    with DEV_LOCK:
        devs = load_devices(); devs.append(dev); save_devices(devs)
    return dev, tok


def revoke_device(dev_id):
    with DEV_LOCK:
        devs = load_devices()
        keep = [d for d in devs if d["id"] != dev_id]
        if len(keep) == len(devs):
            return False
        save_devices(keep)
    Conns.close(dev_id)
    return True


class Conns:
    """Open sockets per device, so revoking a phone drops its live streams now."""
    lock = threading.Lock()
    by_dev = {}

    @classmethod
    def add(cls, dev_id, sock):
        with cls.lock:
            cls.by_dev.setdefault(dev_id, set()).add(sock)

    @classmethod
    def remove(cls, dev_id, sock):
        with cls.lock:
            cls.by_dev.get(dev_id, set()).discard(sock)

    @classmethod
    def close(cls, dev_id=None):
        with cls.lock:
            socks = [s for k, v in cls.by_dev.items() if dev_id in (None, k) for s in v]
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class Guard:
    """Failed-auth rate limit per peer IP: 5 failures a minute -> 429 for 10 minutes."""
    lock = threading.Lock()
    fails, until = {}, {}
    LIMIT, WINDOW, BLOCK = 5, 60, 600

    @classmethod
    def blocked(cls, ip):
        with cls.lock:
            return cls.until.get(ip, 0) > time.time()

    @classmethod
    def fail(cls, ip):
        now = time.time()
        with cls.lock:
            recent = [t for t in cls.fails.get(ip, []) if now - t < cls.WINDOW] + [now]
            cls.fails[ip] = recent
            if len(recent) >= cls.LIMIT:
                cls.until[ip] = now + cls.BLOCK
                cls.fails[ip] = []


def _tailscale_json(*args):
    r = subprocess.run(["tailscale", args[0], "--json", *args[1:]], capture_output=True, text=True, timeout=5)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip() or "tailscale failed")
    return json.loads(r.stdout)


def tailscale_ip():
    r = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5)
    ip = (r.stdout.strip().splitlines() or [""])[0]
    if r.returncode or not ip:
        raise RuntimeError("Tailscale isn't up (tailscale ip -4 gave nothing).")
    if ipaddress.ip_address(ip) not in TAILNET:
        raise RuntimeError(f"{ip} isn't a Tailscale address; refusing to listen on it.")
    return ip


class Whois:
    """Tailscale identity of a peer IP (cached for a minute)."""
    cache, lock = {}, threading.Lock()
    self_user = None

    @classmethod
    def of(cls, ip):
        with cls.lock:
            hit = cls.cache.get(ip)
            if hit and time.time() - hit[0] < 60:
                return hit[1]
        d = _tailscale_json("whois", ip)
        who = {"node": d["Node"].get("StableID") or str(d["Node"].get("ID")),
               "node_name": d["Node"].get("ComputedName") or d["Node"].get("Name", "").split(".")[0],
               "user": (d.get("UserProfile") or {}).get("ID")}
        with cls.lock:
            cls.cache[ip] = (time.time(), who)
        return who

    @classmethod
    def owner(cls):
        if cls.self_user is None:
            cls.self_user = _tailscale_json("status")["Self"].get("UserID")
        return cls.self_user


def pin_check(dev, ip):
    """(ok, reason). First use pins the token to that Tailscale node."""
    try:
        who = Whois.of(ip)
        owner = Whois.owner()
    except Exception as e:
        return False, f"tailscale whois failed: {e}"
    if who["user"] != owner:
        return False, "that Tailscale node belongs to someone else"
    if not dev.get("node"):
        touch_device(dev["id"], node=who["node"], node_name=who["node_name"])
        return True, ""
    if not secrets.compare_digest(str(dev["node"]), str(who["node"])):
        return False, f"this pairing belongs to {dev.get('node_name') or 'another device'}"
    return True, ""


WATCH_OFF = False


class Phone:
    """The tailnet listener for Loft Mobile."""
    srv = None
    ip = None
    error = ""
    lock = threading.Lock()

    @classmethod
    def status(cls):
        return {"on": bool(load_config().get("phone")), "running": cls.srv is not None, "ip": cls.ip,
                "port": PHONE_PORT, "error": cls.error, "host": HOSTNAME,
                "devices": [public_device(d) for d in load_devices()]}

    @classmethod
    def start(cls):
        with cls.lock:
            if cls.srv:
                return
            try:
                ip = tailscale_ip()
                srv = ThreadingHTTPServer((ip, PHONE_PORT), RemoteH)
            except Exception as e:
                cls.error = str(e)
                print(f"phone access off: {e}", flush=True)
                return
            srv.daemon_threads = True
            cls.srv, cls.ip, cls.error = srv, ip, ""
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            print(f"phone access on http://{ip}:{PHONE_PORT} (tailnet only)", flush=True)

    @classmethod
    def stop(cls):
        with cls.lock:
            srv, cls.srv = cls.srv, None
        if srv:
            srv.shutdown(); srv.server_close()
            Conns.close()
            print("phone access off", flush=True)

    @classmethod
    def watch(cls):
        """Keep the phone listener up while it's switched on. At login Tailscale often comes up after
        Loft, and its address can change: retry until it's reachable, re-bind when the address moves."""
        while True:
            time.sleep(10)
            try:
                if not load_config().get("phone") or WATCH_OFF:
                    continue
                if cls.srv is None:
                    cls.start()
                elif tailscale_ip() != cls.ip:
                    cls.stop(); cls.start()
            except Exception as e:
                cls.error = str(e)

    @classmethod
    def set(cls, on):
        cfg = load_config(); cfg["phone"] = bool(on); save_config(cfg)
        cls.start() if on else cls.stop()
        return cls.status()


def derive_title(prompt):
    """A short card title from the first clause of the prompt."""
    import re
    first = re.split(r"(?<=[.!?\n])\s", prompt.strip(), maxsplit=1)[0].strip().rstrip(".!?")
    first = re.sub(r"^(please|can you|could you|i want you to|go and)\s+", "", first, flags=re.I)
    words = first.split()
    title = " ".join(words[:7]) + ("…" if len(words) > 7 else "")
    return title[:1].upper() + title[1:]


def _attached(card, steps=(), idle=0.8, read_after=0.8):
    """Attach to a background agent in a hidden pty, wait for the screen to
    settle, write each step (a string of keys; "\x00SLEEP:<s>" pauses), and
    detach. The session keeps running, exactly as after `claude attach`.
    Returns the raw bytes the attach printed (for reading a prompt off it)."""
    env = B.account_env(card)
    env.update(TERM="xterm-256color", COLORTERM="truecolor")
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(str(HOME))
        os.execvpe(CLAUDE, [CLAUDE, "attach", card["id"]], env)
    out = bytearray()
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 160, 0, 0))

        def drain(idle, limit):
            got, start, last = 0, time.time(), time.time()
            while time.time() - start < limit:
                r, _, _ = select.select([fd], [], [], 0.1)
                if r:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        return got
                    if not chunk:
                        return got
                    got += len(chunk); last = time.time(); out.extend(chunk)
                elif got and time.time() - last > idle:
                    return got
            return got

        # the first paint lands within ~1.5 s; a spinner may keep it from going idle
        if not drain(idle, 3.5) and not drain(idle, 8):
            raise RuntimeError("Couldn't open the agent's session.")
        for st in steps:
            if st.startswith("\x00SLEEP:"):
                drain(9, float(st[7:])); continue
            os.write(fd, st.encode())
            drain(0.35, 1.2)
        if steps:
            drain(read_after, 3)
    finally:
        try:
            os.kill(pid, signal.SIGHUP)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        for _ in range(20):
            try:
                if os.waitpid(pid, os.WNOHANG)[0]:
                    break
            except ChildProcessError:
                break
            time.sleep(0.05)
        else:
            try:
                os.kill(pid, signal.SIGKILL); os.waitpid(pid, 0)
            except OSError:
                pass
    return bytes(out)


def send_to_agent(card, text):
    """Type text into a background agent's prompt and press Enter.

    There is no `claude send`, so this attaches briefly in a hidden pty, pastes
    the text (bracketed paste, so multi-line stays one message), presses Enter
    and detaches. Not for pickers: Enter there picks the highlighted row (see
    answer_route, which drives them with keys)."""
    _attached(card, ["\x1b[200~" + text + "\x1b[201~", "\x00SLEEP:0.4", "\r"], idle=1.2)
    return "Sent"


# ------------------------------------------------------------------ pickers
_PROMPTS = {}   # (job id, tool_use id) -> parsed permission prompt, so polls don't re-attach


def _job_state(jid):
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", jid or ""):
        return None
    try:
        return json.loads((B.JOBS / jid / "state.json").read_text())
    except (OSError, ValueError):
        return None


_SCRAPING = set()


def _scrape(key, card=None):
    try:
        card = card or find_card(key[0])
        got = PR.parse_choices(PR.screen_lines(_attached(card))) if card else None
    except Exception:
        got = None
    if len(_PROMPTS) > 200:
        _PROMPTS.clear()
    if got:
        _PROMPTS[key] = got
    _SCRAPING.discard(key)


def pending_prompt(jid, st, items, card=None, scrape="sync"):
    """What the agent is waiting on, for a needs-you card:
    {"kind": "question", "tool", "questions"} for AskUserQuestion,
    {"kind": "permission", "tool", "title", "options": [{n, label}]} for a tool
    permission or plan approval (options read off the attach screen, cached),
    or None (it asked in plain text: a normal reply is right)."""
    ask = TR.pending_options(items)
    if ask:
        return {"kind": "question", "tool": ask["tool"], "questions": ask["questions"]}
    answered = {it["for"] for it in items if it["kind"] == "result"}
    last = next((it for it in reversed(items) if it["kind"] in ("tool", "user", "assistant")), None)
    if not last or last["kind"] != "tool" or last["tool"] in answered:
        return None
    key = (jid, last["tool"])
    if key not in _PROMPTS:
        if scrape == "off":
            return None
        if scrape == "async":   # a poll: read the screen in the background, report it next poll
            if key not in _SCRAPING:
                _SCRAPING.add(key)
                threading.Thread(target=_scrape, args=(key, card), daemon=True).start()
            return None
        _scrape(key, card)
    got = _PROMPTS.get(key)
    if not got:
        return None
    title = (st.get("needs") or "").strip() if isinstance(st.get("needs"), str) else ""
    return {"kind": "permission", "tool": last["tool"], "title": got["title"] or "Do you want to proceed?",
            "action": title or f'{last["name"]} · {last["title"]}', "options": got["options"]}


def answer_route(jid, body):
    """POST /api/cards/<id>/answer: pick on the agent's picker with keys.
    {"tool": <tool_use id>, "answers": [{"choice": i} | {"choices": [..]} | {"other": "text"}, ...]}
    {"tool": <tool_use id>, "permission": <option number>}
    409 when the agent is no longer waiting on that tool (the question changed)."""
    st = _job_state(jid)
    card = find_card(jid) if st else None
    if not st or not card or card.get("kind") != "agent":
        return 404, {"message": "No such agent"}
    tp = C.transcript_of(st)
    items = TR.read_page(tp, limit=40)["items"] if tp else []
    pend = pending_prompt(jid, st, items, card) if C.bucket(st) in ("needs", "failed") else None
    tool = body.get("tool")
    if not pend or pend["tool"] != tool:
        return 409, {"message": "The question changed. Here is the current one.", "pending": pend}
    try:
        if pend["kind"] == "question":
            keys = PR.question_keys(pend["questions"], body.get("answers"))
        else:
            keys = PR.permission_keys(body.get("permission"), pend["options"])
    except PR.AnswerError as e:
        return 400, {"message": str(e)}
    _attached(card, keys)
    _PROMPTS.pop((jid, tool), None)
    for _ in range(16):   # confirm from the transcript: the tool got its result
        time.sleep(0.25)
        if tp and any(it["kind"] == "result" and it["for"] == tool for it in TR.read_page(tp, limit=40)["items"]):
            return 200, {"message": "Answered", "confirmed": True}
    return 200, {"message": "Sent", "confirmed": False}


def timeline(jid, limit=40):
    f = B.JOBS / jid / "timeline.jsonl"
    out = []
    try:
        for line in f.read_text().splitlines()[-limit:]:
            try:
                e = json.loads(line)
                out.append({"at": e.get("at"), "state": e.get("state"), "detail": e.get("detail") or ""})
            except Exception:
                pass
    except Exception:
        pass
    return out


def messages(jid, query):
    """GET /api/cards/<id>/messages?limit=&before=&after= : the session as chat items.
    See loft/transcript.py for the item shapes and cursors. (status, payload)."""
    import re
    from urllib.parse import parse_qs
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", jid or ""):
        return 404, {"error": "not found"}
    q = {k: v[-1] for k, v in parse_qs(query).items()}
    num = lambda k: int(q[k]) if q.get(k, "").isdigit() else None
    sf = B.JOBS / jid / "state.json"
    try:
        st = json.loads(sf.read_text())
    except FileNotFoundError:
        if any(t.get("id") == jid for t in B.load_todos()):
            return 200, {"id": jid, "state": "todo", "working": False, "detail": "", "session": None, "cursor": 0,
                         "start": 0, "more": False, "gap": False, "items": [], "pending": None}
        return 404, {"error": "not found"}
    except ValueError:
        return 500, {"error": "unreadable state"}
    col = C.bucket(st)
    detail = (st.get("detail") or "").strip()
    if col == "needs" and detail.startswith("(idle"):
        col = "todo"
    out = {"id": jid, "state": col, "working": col == "running", "detail": detail, "pending": None}
    tp = C.transcript_of(st)
    if not tp:
        return 200, {**out, "session": None, "cursor": 0, "start": 0, "more": False, "gap": False, "items": []}
    after = num("after")
    if after is not None and q.get("session") and q["session"] != tp.stem:
        after = None   # a different session file now (resumed): send a fresh page
    try:
        page = TR.read_page(tp, limit=num("limit") or 60, before=num("before"), after=after)
    except OSError:
        return 200, {**out, "session": None, "cursor": 0, "start": 0, "more": False, "gap": False, "items": []}
    if after is None and q.get("session") and q["session"] != tp.stem:
        page["reset"] = True
    if col in ("needs", "failed"):
        tail = page["items"] if after is None and num("before") is None else TR.read_page(tp, limit=40)["items"]
        ask = TR.pending_options(tail)
        prompt = pending_prompt(jid, st, tail, scrape="off" if q.get("scrape") == "0" else "async") if col == "needs" else None
        out["pending"] = {"text": detail, "question": ask, "prompt": prompt}
    return 200, {**out, **page}


def snapshot():
    cards = B.load_cards(show_all_done=True)
    for c in cards:
        c["updated"] = c["updated"].isoformat() if c["updated"] else None
        if c["kind"] == "agent":
            c["browser"] = browser_badge(c["id"])
        c["cwd"] = c["cwd"].replace(str(HOME), "~")
        c["folder"] = c["cwd"]
    try:
        pcfg = P.load()
    except SystemExit:
        pcfg = P.empty()
    counts = P.count(cards)
    cfg = A.load()
    cfg["accounts"] = [a for a in cfg["accounts"] if not a.get("pending")]  # still signing in
    usage = {}  # keyed by account id; "five"/"seven" stay strings ("—" = unknown) as before
    for acc in cfg["accounts"]:
        u = U.get(acc)
        usage[acc["id"]] = {"five": "—" if u["five"] is None else str(u["five"]), "seven": "—" if u["seven"] is None else str(u["seven"]),
                            "fiveReset": u["fiveReset"], "sevenReset": u["sevenReset"], "error": u["error"]}
    return {
        "cards": sorted(cards, key=lambda c: (not c["pinned"], c["name"].lower())),
        "usage": usage,
        "accounts": [A.public(a, cfg) for a in cfg["accounts"]],
        "defaultAccount": cfg.get("default"),
        "setup": not A.configured(),
        "dirs": [d.replace(str(HOME), "~") for d in B.recent_dirs()],
        "doneDays": C.DONE_DAYS,
        "voice": Voice.available(),
        "projects": [P.public(p, counts) for p in pcfg["projects"]],
        "unsorted": counts.get(None, {"total": 0, "needs": 0, "running": 0, "todo": 0, "done": 0}),
        "groupBy": pcfg["view"]["groupBy"],
        "organized": P.config_path().is_file(),
    }


def new_task_account(explicit, proj):
    """explicit account (id or "auto") > the project's default (auto = lowest 5h usage) > global default."""
    return P.pick_account(explicit, proj, A.load(), U.get)


def projects_route(method, parts, body):
    """/api/projects[/<id>] and /api/view. Returns (status, payload)."""
    try:
        if parts == ["api", "view"] and method == "POST":
            g = body.get("groupBy")
            if g not in P.GROUP_BY:
                return 400, {"message": "groupBy is one of " + ", ".join(P.GROUP_BY)}
            with P.locked() as pcfg:
                pcfg["view"]["groupBy"] = g
            return 200, {"message": "Saved", "groupBy": g}
        if parts == ["api", "projects"] and method == "POST":
            with P.locked() as pcfg:
                p = P.create(body, pcfg)
            pub = P.public(P.get(p["id"]))
            return 200, {**pub, "message": f"Created {p['name']}", "project": pub}
        if parts == ["api", "projects", "from-folders"] and method == "POST":
            made = []
            with P.locked() as pcfg:
                for f in body.get("folders") or []:
                    full = os.path.expanduser(str(f))
                    if not os.path.isabs(full) or P.folder_match(full, pcfg):
                        continue
                    name = Path(full).name or full
                    if any(q["name"].lower() == name.lower() for q in pcfg["projects"]):
                        name = f"{name} ({Path(full).parent.name})"
                    made.append(P.create({"name": name, "folders": [full]}, pcfg)["name"])
            n = len(made)
            return 200, {"message": f"Created {n} project{'s' if n != 1 else ''}" if n else "Nothing new to create", "created": made}
        if len(parts) == 3 and parts[:2] == ["api", "projects"]:
            pid = parts[2]
            with P.locked() as pcfg:
                if method == "DELETE":
                    ok = P.remove(pid, pcfg)
                    return (200, {"message": "Project removed. Its agents are unsorted now."}) if ok else (404, {"message": "No such project"})
                if method == "PATCH":
                    P.update(pid, body, pcfg)
            pub = P.public(P.get(pid))
            return 200, {**pub, "message": "Saved", "project": pub}
    except KeyError:
        return 404, {"message": "No such project"}
    except ValueError as e:
        return 400, {"message": str(e)}
    return 404, {"message": "not found"}


def find_card(cid):
    for c in B.load_cards(show_all_done=True):
        if c["id"] == cid:
            return c
    return None


def open_terminal(card):
    cmd, env = B.terminal_command(card)
    subprocess.Popen(cmd, env=env, cwd=str(HOME), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return f"opened {card['name']} in a terminal"


def switch_route(card, body):
    """POST /api/cards/<id>/account {account, when?: "after"|"now", cancel?: true}.
    Desktop and phone alike (same rules)."""
    if body.get("cancel"):
        if card["kind"] != "agent":
            return 400, "Only agents can have a queued switch."
        return 200, "Switch cancelled" if B.pending_clear(card["id"]) else "Nothing was queued"
    to, when = body.get("account"), body.get("when")
    if to is not None and (not isinstance(to, str) or not A.get(to)):
        return 400, f"No account '{to}'."
    if when not in (None, "after", "now"):
        return 400, "when must be 'after' or 'now'."
    r = B.switch_account(card, to, when)
    return (200 if r["ok"] else 409), r["message"]


def pending_loop(every=3.0):
    """Apply queued account switches once their agent leaves Working, then send queued
    prompts and start queued todos that can go now."""
    while True:
        time.sleep(every)
        try:
            with LOCK:
                B.apply_pending(log=lambda m: print(m, flush=True))
        except Exception as e:
            print(f"pending switch loop: {type(e).__name__}: {e}", flush=True)
        try:
            run_queue()
        except Exception as e:
            print(f"prompt queue loop: {type(e).__name__}: {e}", flush=True)
        try:
            check_gate()
        except Exception as e:
            print(f"browser checks: {type(e).__name__}: {e}", flush=True)


def run_queue():
    with LOCK:
        due = B.queue_due()
    for kind, x, item in due:    # sending takes seconds: outside the lock so the app stays responsive
        try:
            if kind == "send":
                send_to_agent(x, item["text"])
                print(f"queue: sent to {x['id']}", flush=True)
            else:
                jid, msg = B.launch_todo(x)
                if not jid:
                    raise RuntimeError(msg)
                print(f"queue: started todo {x['id']} as {jid}", flush=True)
        except Exception as e:
            print(f"queue: {kind} {x['id']} failed: {e}", flush=True)
            with LOCK:
                B.queue_failed(kind, x, item, str(e))
    Awake.hold(B.queue_pending())


class Awake:
    """Keeps the machine from idle-sleeping while prompts wait, so they go out at the
    reset. macOS: caffeinate (-s holds only on AC power; tied to this server's pid).
    Linux: systemd-inhibit, also ended when this server exits. A closed lid on battery
    still sleeps; what is due then goes out on the next wake."""
    proc = None

    @classmethod
    def hold(cls, on):
        alive = cls.proc is not None and cls.proc.poll() is None
        if on == alive:
            return
        if not on:
            cls.proc.terminate(); cls.proc = None
            return
        pid = str(os.getpid())
        if sys.platform == "darwin":
            cmd = ["caffeinate", "-i", "-s", "-w", pid]
        elif shutil.which("systemd-inhibit"):
            cmd = ["systemd-inhibit", "--what=idle:sleep", "--who=Loft", "--why=Prompts are queued",
                   "tail", f"--pid={pid}", "-f", "/dev/null"]
        else:
            return
        try:
            cls.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            cls.proc = None


def at_time(iso):
    """'3:40 PM' today, 'Fri 3:40 PM' otherwise (local time)."""
    t = datetime.fromisoformat(iso).astimezone()
    hm = t.strftime("%I:%M %p").lstrip("0")
    return hm if t.date() == datetime.now().astimezone().date() else t.strftime("%a ") + hm


def queued_msg(until, n=1):
    """until: B.limit_until's answer (None = not limited, so it waits for the turn)."""
    if until is None:
        when = "sends after this turn"
    elif until:
        when = f"sends when the limit resets at {at_time(until)}"
    else:
        when = "sends when the limit resets"
    return f"Queued ({n} waiting): {when}" if n > 1 else f"Queued: {when}"


def act(cid, action, body):
    """Run one card action. Returns (status, message)."""
    if action == "create":
        proj = None
        if body.get("project"):
            proj = P.get(body["project"])
            if not proj:
                return 400, "That project is gone. Pick another one."
        cwd = (body.get("cwd") or "").strip() or (proj["folders"][0] if proj and proj["folders"] else "")
        title = (body.get("title") or "").strip() or derive_title(body.get("prompt") or "")
        if not title:
            return 400, "Write what the agent should do."
        if not cwd or not os.path.isdir(os.path.expanduser(cwd)):
            return 400, "Pick a project folder that exists."
        if os.path.realpath(os.path.expanduser(cwd)) == str(HOME):
            return 400, "Pick a project folder: Claude won't start background agents in your home folder."
        acc = new_task_account(body.get("account") or None, proj)
        todo = {"id": "t" + secrets.token_hex(4), "title": title,
                "account": acc["id"],
                "cwd": cwd.replace(str(HOME), "~"), "prompt": (body.get("prompt") or "").strip(),
                "created": B.now_utc().isoformat()}
        if proj:
            todo["project"] = proj["id"]
        if body.get("start"):
            if not todo["prompt"]:
                return 400, "Write a prompt to start it with."
            jid, msg = B.launch_todo(todo)
            return (200, msg) if jid else (500, msg)
        todos = B.load_todos(); todos.append(todo); B.save_todos(todos)
        return 200, "Added to Todo"

    card = find_card(cid)
    if not card:
        return 404, "That card is gone (archived or finished elsewhere)."
    if action == "open":
        if card["kind"] == "todo":
            return 400, "Start it first."
        return 200, open_terminal(card)
    if action == "start":
        if card["kind"] != "todo":
            return 400, "Already started."
        todo = next((t for t in B.load_todos() if t["id"] == cid), None)
        if body.get("prompt"):
            todo["prompt"] = body["prompt"].strip()
        if not todo.get("prompt"):
            return 400, "Write a prompt to start it with."
        until = B.limit_until(B.todo_account(todo, A.load())["id"])
        if until is not None:      # at its limit: the poll loop starts it after the reset
            todos = B.load_todos()
            for t in todos:
                if t["id"] == cid:
                    t.update(prompt=todo["prompt"], queued=True)
            B.save_todos(todos)
            Awake.hold(True)
            return 200, f"Starts when the limit resets{' at ' + at_time(until) if until else ''}"
        jid, msg = B.launch_todo(todo)
        if not jid:
            return 500, msg
        B.save_todos([t for t in B.load_todos() if t["id"] != cid])
        return 200, msg
    if action == "rename":
        name = (body.get("name") or "").strip()
        if not name:
            return 400, "A name can't be empty."
        return 200, B.act_rename(card, name)
    if action == "account":
        return switch_route(card, body)
    if action == "project":
        pid = body.get("project") or None
        with P.locked() as pcfg:
            if pid and not P.get(pid, pcfg):
                return 400, "That project is gone."
            P.assign(cid, pid, pcfg)
            name = P.get(pid, pcfg)["name"] if pid else None
        if card["kind"] == "todo":   # a todo carries its project into the agent it becomes
            todos = B.load_todos()
            for t in todos:
                if t["id"] == cid:
                    t["project"] = pid
            B.save_todos(todos)
        return 200, f"Moved to {name}" if name else "Removed from its project"
    if action == "pin":
        return 200, B.act_pin(card)
    if action == "archive":
        return 200, B.act_archive(card)
    if action == "stop":
        r = subprocess.run([CLAUDE, "stop", cid], capture_output=True, text=True,
                           env=B.account_env(card), cwd=str(HOME), timeout=60)
        out = (r.stdout + r.stderr).strip().splitlines()
        return 200, out[-1] if out else "Stopped"
    if action == "reply":
        text = (body.get("text") or "").strip()
        if not text:
            return 400, "Write or say a reply first."
        if card["kind"] == "todo":
            return 400, "Start it first."
        until = B.limit_until(card.get("account"))
        if body.get("queue") or until is not None or B.busy(card) or B.queue_read(card["id"])["items"]:
            n = B.queue_add(card["id"], text)   # behind anything already waiting, to keep the order
            Awake.hold(True)
            return 200, queued_msg(until, n)
        return 200, send_to_agent(card, text)
    if action == "queue":   # {item: id} removes one queued prompt, {} clears them; a todo is unscheduled
        if card["kind"] == "todo":
            todos = B.load_todos()
            for t in todos:
                if t["id"] == cid:
                    t["queued"] = False
            B.save_todos(todos)
            return 200, "Won't start by itself"
        gone = B.queue_remove(cid, body.get("item"))
        return 200, ("Removed" if body.get("item") else "Queue cleared") if gone else "Nothing to remove"
    if action == "prompt" and card["kind"] == "todo":
        todos = B.load_todos()
        for t in todos:
            if t["id"] == cid:
                t["prompt"] = (body.get("prompt") or "").strip()
        B.save_todos(todos)
        return 200, "Prompt saved"
    return 400, f"Unknown action {action}"


# ------------------------------------------------------------------ shared agent browser
# One headless Chromium per agent (loft/browser.py). The card streams it, records click flows
# as per-project checks, and replays them when the agent finishes a turn.
_LAST_COL, _GATING = {}, set()
GATE_TRIES = 3   # failed runs sent back to the agent before Loft stops and leaves it to you


def browser_badge(jid):
    rec, res = BR.record(jid), BR.results(jid)
    if not rec and not res:
        return None
    out = {"mcp": bool(rec and rec.get("mcp")), "running": jid in _GATING}
    if res:
        out["checks"] = {k: res.get(k) for k in ("ok", "passed", "total", "attempts", "skipped", "at")}
    return out


def browser_info(card):
    scope = BR.scope_of(card)
    lg = BR.login_for(scope)
    return {"ports": BR.dev_ports(card["cwd"]), "scope": scope, "checks": BR.checks_for(scope),
            "results": BR.results(card["id"]), "running": card["id"] in _GATING,
            "login": {"host": lg["host"], "saved": lg["saved"]} if lg else None, "badge": browser_badge(card["id"])}


def gate(card, base=None, reply=True):
    """Replay the project's checks against the agent's dev server; tell the agent what failed."""
    try:
        if not base:
            ports = BR.dev_ports(card["cwd"])
            if not ports:
                res = {"at": time.time(), "ok": None, "skipped": "No dev server running in the agent's folder",
                       "attempts": (BR.results(card["id"]) or {}).get("attempts", 0)}
                (BR.home() / f"{card['id']}-checks.json").write_text(json.dumps(res))
                return res
            base = f"http://localhost:{ports[0]}"
        res = BR.run_all(card["id"], card, base)
        if reply and not res["ok"] and res["attempts"] <= GATE_TRIES:
            msg = BR.failure_message(res)
            try:
                send_to_agent(card, msg)
            except Exception as e:   # couldn't reach it now: the prompt queue retries when it's free
                print(f"checks {card['id']}: couldn't tell the agent ({e}); queued", flush=True)
                with LOCK:
                    B.queue_add(card["id"], msg)
        return res
    finally:
        _GATING.discard(card["id"])


def check_gate():
    """An agent left Working and its project has checks: run them (in the background)."""
    allc = BR.checks_all()
    for c in B.load_cards(show_all_done=True):
        if c["kind"] != "agent":
            continue
        prev, _LAST_COL[c["id"]] = _LAST_COL.get(c["id"]), c["col"]
        if prev == "running" and c["col"] in ("needs", "done") and allc.get(BR.scope_of(c)) and c["id"] not in _GATING:
            _GATING.add(c["id"])
            threading.Thread(target=lambda c=c: _log_gate(c), daemon=True).start()


def _log_gate(card):
    try:
        res = gate(card)
        summary = res.get("skipped") or f"{res['passed']}/{res['total']} passed"
        print(f"checks {card['id']}: {summary}", flush=True)
    except Exception as e:
        print(f"checks {card['id']} failed to run: {type(e).__name__}: {e}", flush=True)


def browser_route(jid, action, body):
    card = find_card(jid)
    if not card or card["kind"] != "agent":
        return 404, {"message": "No such agent."}
    scope = BR.scope_of(card)
    if action == "run":
        if jid in _GATING:
            return 409, {"message": "Checks are already running."}
        if not BR.checks_for(scope):
            return 400, {"message": "No checks yet. Record one first."}
        _GATING.add(jid)
        res = gate(card, base=(body.get("base") or "").strip() or None, reply=False)
        return 200, {"message": res.get("skipped") or f"{res['passed']}/{res['total']} checks passed", "results": res}
    if action == "check-approve":   # an agent recorded it: now every agent on the project must pass it
        BR.approve_check(scope, body.get("id"))
        return 200, {"message": "Check approved for the project"}
    if action == "check-delete":
        BR.checks_save(scope, [c for c in BR.checks_for(scope) if c["id"] != body.get("id")])
        return 200, {"message": "Check deleted"}
    if action == "login-forget":
        BR.login_drop(scope)
        return 200, {"message": "Saved login forgotten"}
    if action == "takeover":
        text = ("I'm taking over the shared browser for a moment. Don't use the browser tools until I hand it back."
                if body.get("on") else "I handed the shared browser back. Carry on from the page it's on now.")
        def tell():
            try:
                send_to_agent(card, text)
            except Exception as e:
                print(f"takeover {jid}: couldn't tell the agent: {e}", flush=True)
        threading.Thread(target=tell, daemon=True).start()
        return 200, {"message": "Told the agent you're driving" if body.get("on") else "Handed back to the agent"}
    return 400, {"message": f"Unknown action {action}"}


def agent_route(card, action, body):
    jid, scope = card["id"], BR.scope_of(card)
    if action == "checks":
        return 200, {"checks": BR.checks_for(scope), "last": BR.results(jid)}
    if body is None:
        return 405, {"message": "POST this."}
    if action == "record":
        if body.get("on"):
            BR.rec_start(jid, BR.ensure(jid)["port"])
            return 200, {"message": "Recording. Walk the flow with the browser tools, add loft_record_expect "
                                    "for text that must show, then loft_record_save with a name."}
        steps = BR.rec_stop(jid)
        if steps is None:
            return 409, {"message": "Not recording."}
        if not (body.get("name") or "").strip():
            return 200, {"message": f"Discarded {len(steps)} steps."}
        return 200, {"message": BR.save_check(scope, body["name"], steps, by=jid), "steps": steps}
    if action == "expect":
        BR.rec_expect(jid, body.get("text"), (body.get("selector") or "").strip() or None)
        return 200, {"message": "Added", "steps": BR.recording(jid)}
    if action == "run":
        if jid in _GATING:
            return 409, {"message": "Checks are already running."}
        if not BR.checks_for(scope):
            return 400, {"message": "This project has no checks yet."}
        _GATING.add(jid)
        res = gate(card, base=(body.get("base") or "").strip() or None, reply=False)
        return 200, res
    return 404, {"message": f"Unknown tool action {action}"}


class H(BaseHTTPRequestHandler):
    server_version = "cagents"

    def log_message(self, *a):
        pass

    def _send(self, code, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    device = None   # the paired phone making this request, if any

    def _desktop(self):
        """The request carries the desktop page's per-run token."""
        tok = self.headers.get("X-Cagents-Token", "")
        if not tok:  # EventSource can't send headers; it passes ?t= (GET /api/events only)
            from urllib.parse import parse_qs
            u = urlparse(self.path)
            if u.path == "/api/events" or u.path.startswith(("/ws/term/", "/ws/browser/")):
                tok = (parse_qs(u.query).get("t") or [""])[0]
        return secrets.compare_digest(tok, TOKEN)

    def _bearer(self):
        h = self.headers.get("Authorization", "")
        return h[7:].strip() if h[:7].lower() == "bearer " else ""

    def _authed(self):
        if self._desktop():
            return True
        if DEV_LOOPBACK_DEVICES:   # dev only: emulator -> 10.0.2.2 -> 127.0.0.1
            dev = device_for_token(self._bearer())
            if dev:
                self._attach(dev)
                return True
        return False

    def _attach(self, dev):
        self.device = dev
        Conns.add(dev["id"], self.connection)
        ip, now = self.client_address[0], time.time()
        if now - _SEEN.get(dev["id"], 0) > 30 or dev.get("last_ip") != ip:   # don't rewrite the file every request
            _SEEN[dev["id"]] = now
            touch_device(dev["id"], last_seen=time.strftime("%Y-%m-%dT%H:%M:%S%z"), last_ip=ip)

    def finish(self):
        if self.device:
            Conns.remove(self.device["id"], self.connection)
        super().finish()

    def _admin(self):
        """Pairing and phone settings: the desktop page only, never a phone."""
        return self._desktop() and not self.device

    def _admin_get(self, path):
        if path == "/api/phone":
            return self._send(200, Phone.status())
        if path == "/api/devices":
            return self._send(200, [public_device(d) for d in load_devices()])
        return None

    def _admin_post(self, parts, body):
        if parts == ["api", "phone"]:
            self._send(200, Phone.set(body.get("on"))); return True
        if parts == ["api", "pair"]:
            if not Phone.srv:
                self._send(409, {"message": "Turn on phone access first."}); return True
            dev, tok = pair_device(body.get("name") or "phone")
            from urllib.parse import quote
            uri = f"loft://pair?h={Phone.ip}:{PHONE_PORT}&t={quote(tok)}&n={quote(HOSTNAME)}"
            self._send(200, {"id": dev["id"], "uri": uri, "host": f"{Phone.ip}:{PHONE_PORT}", "token": tok}); return True
        if len(parts) == 4 and parts[:2] == ["api", "devices"] and parts[3] == "revoke":
            ok = revoke_device(parts[2])
            self._send(200 if ok else 404, {"message": "Revoked" if ok else "No such device"}); return True
        return None

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            html = (HERE / "index.html").read_text().replace("__CAGENTS_TOKEN__", TOKEN)
            return self._send(200, html.encode(), "text/html; charset=utf-8")
        if path == "/favicon.ico":
            self.send_response(204); self.end_headers(); return
        if path == "/api/loft-proof":
            from urllib.parse import parse_qs
            n = (parse_qs(urlparse(self.path).query).get("n") or [""])[0]
            if len(n) != 32 or any(c not in "0123456789abcdef" for c in n):
                return self._send(400, {"error": "bad nonce"})
            return self._send(200, hashlib.sha256(f"{PROOF_KEY}:{n}".encode()).hexdigest().encode(), "text/plain")
        if path.startswith("/static/"):
            f = (HERE / path.lstrip("/")).resolve()
            if HERE in f.parents and f.is_file():
                ctype = {"js": "text/javascript", "css": "text/css", "svg": "image/svg+xml", "png": "image/png",
                         "woff2": "font/woff2"}.get(f.suffix.lstrip("."), "application/octet-stream")
                return self._send(200, f.read_bytes(), ctype)
            return self._send(404, {"error": "not found"})
        if path.startswith("/api/agent/"):
            return self.agent_api(path, None)
        if not self._authed():
            return self._send(403, {"error": "bad token"})
        if path in ("/api/phone", "/api/devices"):
            return self._admin_get(path) if self._admin() else self._send(404, {"error": "not found"})
        if path == "/api/ping":
            return self._send(200, {"host": HOSTNAME, "version": VERSION, "voice": Voice.available(),
                                    "device": self.device and self.device.get("name")})
        if path == "/ws/voice":
            return self.voice_ws()
        if path == "/api/state":
            return self._send(200, snapshot())
        if path.startswith("/api/timeline/"):
            return self._send(200, timeline(path.rsplit("/", 1)[-1]))
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "cards"] and parts[3] == "messages":
            return self._send(*messages(parts[2], urlparse(self.path).query))
        if path == "/api/events":
            return self.events()
        if path.startswith("/api/accounts") or path.startswith("/ws/term/login-"):
            return webapi.handle_get(self, path)
        if path.startswith("/ws/term/"):
            return self.term_ws(path.rsplit("/", 1)[-1])
        if path.startswith(("/ws/browser/", "/api/browser/")):   # desktop only: shows local files and drives a browser
            if not self._admin():
                return self._send(404, {"error": "not found"})
            if path.startswith("/ws/"):
                return self.browser_ws(path.rsplit("/", 1)[-1])
            card = find_card(path.rsplit("/", 1)[-1])
            return self._send(200, browser_info(card)) if card else self._send(404, {"error": "no such agent"})
        return self._send(404, {"error": "not found"})

    def events(self):
        """Server-sent events: push the whole snapshot whenever it changes."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last = None
        try:
            while True:
                snap = snapshot()
                digest = hashlib.sha1(json.dumps(snap, sort_keys=True).encode()).hexdigest()
                if digest != last:
                    self.wfile.write(b"data: " + json.dumps(snap).encode() + b"\n\n")
                    last = digest
                else:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                time.sleep(2)
        except OSError:   # client gone, or the phone was revoked (socket shut down)
            return

    # ------------------------------------------------------------ embedded terminal
    # A minimal RFC 6455 WebSocket that bridges xterm.js to `claude attach <id>`
    # running in a pty with the agent's own account login. Closing it only
    # detaches; the agent keeps running in the daemon.
    def _ws_send(self, op, data, lock):
        n = len(data)
        head = bytes([0x80 | op])
        if n < 126:
            head += bytes([n])
        elif n < 65536:
            head += bytes([126]) + struct.pack(">H", n)
        else:
            head += bytes([127]) + struct.pack(">Q", n)
        with lock:
            self.connection.sendall(head + data)

    def _ws_read(self):
        h = self.rfile.read(2)
        if len(h) < 2:
            return None, None
        op, n, masked = h[0] & 0x0F, h[1] & 0x7F, h[1] & 0x80
        if n == 126:
            n = struct.unpack(">H", self.rfile.read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self.rfile.read(8))[0]
        mask = self.rfile.read(4) if masked else b""
        data = self.rfile.read(n)
        if masked:
            m = (mask * (n // 4 + 1))[:n]
            data = (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big") if n else b""
        return op, data

    def _ws_accept(self):
        key = self.headers.get("Sec-WebSocket-Key")
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.protocol_version = "HTTP/1.1"   # WebKit (desktop app, Safari) rejects an HTTP/1.0 101
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True

    def term_ws(self, cid):
        key = self.headers.get("Sec-WebSocket-Key")
        card = find_card(cid)
        if not key or not card or card["kind"] != "agent":
            return self._send(404, {"error": "no such agent"})
        self._ws_accept()
        env = B.account_env(card)
        env.update(TERM="xterm-256color", COLORTERM="truecolor")
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(str(HOME))
                os.execvpe(CLAUDE, [CLAUDE, "attach", cid], env)
            finally:
                os._exit(127)
        lock = threading.Lock()
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))

        def pump():
            try:
                while True:
                    r, _, _ = select.select([fd], [], [], 1.0)
                    if fd in r:
                        data = os.read(fd, 65536)
                        if not data:
                            break
                        self._ws_send(2, data, lock)
            except OSError:
                pass
            try:
                self._ws_send(8, b"", lock)
            except OSError:
                pass

        t = threading.Thread(target=pump, daemon=True)
        t.start()
        try:
            while True:
                op, data = self._ws_read()
                if op is None or op == 8:
                    break
                if op == 9:
                    self._ws_send(10, data, lock)
                elif op == 2:
                    os.write(fd, data)
                elif op == 1:
                    try:
                        msg = json.loads(data)
                    except Exception:
                        continue
                    if msg.get("type") == "resize":
                        rows, cols = int(msg.get("rows") or 40), int(msg.get("cols") or 120)
                        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
                    elif msg.get("type") == "input":
                        os.write(fd, msg.get("data", "").encode())
        except (OSError, ConnectionError):
            pass
        finally:
            try:
                os.kill(pid, signal.SIGHUP)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
            for _ in range(20):
                try:
                    if os.waitpid(pid, os.WNOHANG)[0]:
                        break
                except ChildProcessError:
                    break
                time.sleep(0.05)
            else:
                try:
                    os.kill(pid, signal.SIGKILL); os.waitpid(pid, 0)
                except OSError:
                    pass

    # ------------------------------------------------------------ shared browser
    # Binary frames out = JPEG screencast. Text frames out = {"type": hello|url|err|steps|toast|saved}.
    # Text frames in = {"type": mouse|key|nav|back|reload|viewport|rec|comment|login-save|login-use, ...};
    # mouse x/y are 0..1 of the picture.
    def browser_ws(self, jid):
        card = find_card(jid)
        if not self.headers.get("Sec-WebSocket-Key") or not card or card["kind"] != "agent":
            return self._send(404, {"error": "no such agent"})
        self._ws_accept()
        lock = threading.Lock()

        def frame(b):
            try:
                self._ws_send(2, b, lock)
            except OSError:
                pass

        def ev(d):
            try:
                self._ws_send(1, json.dumps(d).encode(), lock)
            except OSError:
                pass

        scope = BR.scope_of(card)
        try:
            rec = BR.ensure(jid)
            v = BR.Viewer(rec["port"], frame, ev, scope, jid)
        except Exception as e:
            ev({"type": "toast", "err": True, "text": f"Couldn't open the browser: {e}"})
            try:
                self._ws_send(8, b"", lock)
            except OSError:
                pass
            return
        BR.subscribe(jid, ev)   # recording steps, whoever is recording (you or the agent)
        ev({"type": "hello", "mcp": bool(rec.get("mcp")), "ports": BR.dev_ports(card["cwd"]),
            "login": bool(BR.login_for(scope)), "recording": BR.recording(jid)})
        try:
            while True:
                op, data = self._ws_read()
                if op is None or op == 8:
                    break
                if op == 9:
                    self._ws_send(10, data, lock); continue
                if op != 1:
                    continue
                try:
                    msg = json.loads(data)
                    t = msg.get("type")
                    if t == "mouse":
                        v.mouse(msg)
                    elif t == "key":
                        v.key(msg)
                    elif t == "nav":
                        v.navigate(msg.get("url") or "")
                    elif t == "back":
                        v.back()
                    elif t == "reload":
                        v.reload()
                    elif t == "viewport":
                        v.viewport(msg.get("mobile"))
                    elif t == "rec":
                        if msg.get("on"):
                            try:
                                v.record()
                            except Exception:
                                ev({"type": "recoff"}); raise
                        else:
                            steps = BR.rec_stop(jid) or []
                            if (msg.get("name") or "").strip():
                                ev({"type": "toast", "text": BR.save_check(scope, msg["name"], steps)}); ev({"type": "saved"})
                    elif t == "comment":   # delivering takes seconds: don't hold up the mouse and keys
                        def deliver(msg=msg):
                            try:
                                ev({"type": "toast", "text": self._browser_comment(card, v, msg)})
                            except Exception as e:
                                ev({"type": "toast", "err": True, "text": f"Comment not sent: {e}"})
                        threading.Thread(target=deliver, daemon=True).start()
                    elif t == "login-save":
                        ev({"type": "toast", "text": v.save_login()}); ev({"type": "login", "saved": True})
                    elif t == "login-use":
                        ev({"type": "toast", "text": v.use_login()})
                except (OSError, ConnectionError):
                    raise
                except Exception as e:
                    ev({"type": "toast", "err": True, "text": str(e)})
        except (OSError, ConnectionError):
            pass
        finally:
            BR.unsubscribe(jid, ev)
            v.close()

    @staticmethod
    def _browser_comment(card, v, msg):
        note = (msg.get("note") or "").strip()
        if not note:
            raise ValueError("Write what's wrong first.")
        x, y = msg["x"] * v.meta["deviceWidth"], msg["y"] * v.meta["deviceHeight"]
        el = v.pick(x, y, {})
        if not el:
            raise ValueError("Nothing there to comment on.")
        r, pad = el["rect"], 16
        shot = BR.screenshot(v.cdp, v.session, BR.home() / f"shots-{card['id']}" / f"comment-{int(time.time())}.png",
                             {"x": max(0, r["x"] - pad), "y": max(0, r["y"] - pad),
                              "width": max(1, r["w"] + 2 * pad), "height": max(1, r["h"] + 2 * pad)})
        text = (f"Feedback on {v.url()} in the shared browser, about {el['desc']} (selector `{el['css']}`"
                f"{', text “' + el['text'] + '”' if el.get('text') else ''}): {note}\nScreenshot: {shot}")
        with LOCK:
            code, m = act(card["id"], "reply", {"text": text})
        if code != 200:
            raise ValueError(m)
        return m

    # ------------------------------------------------------------ live voice
    # The phone streams PCM16LE 16 kHz mono as binary frames while the user talks.
    # Every ~0.8 s (when there is new audio and the worker is free) the server
    # sends {"type":"partial","text"}; {"type":"stop"} from the phone runs the
    # final pass over the whole clip (with the vocab hint) and sends
    # {"type":"final","text","ms"}, then closes. {"type":"cancel"} just closes.
    RATE = 16000 * 2          # bytes per second of PCM16 mono
    PARTIAL_EVERY = 0.8       # seconds between partial passes
    PARTIAL_WINDOW = 24       # seconds: longer than this, older audio is committed
    COMMIT_CHUNK = 18         # seconds committed at a time
    MAX_SECONDS = 600

    def voice_ws(self):
        if not self.headers.get("Sec-WebSocket-Key"):
            return self._send(400, {"error": "websocket only"})
        if not Voice.available():
            return self._send(503, {"message": "Voice isn't set up on this machine."})
        self._ws_accept()
        lock = threading.Lock()
        buf = bytearray()
        state = {"committed": 0, "ctext": "", "sent": "", "done": False}
        stop = threading.Event()

        def send(obj):
            try:
                self._ws_send(1, json.dumps(obj).encode(), lock)
            except OSError:
                stop.set()

        def partials():
            last_at, last_len = 0.0, 0
            while not stop.wait(0.1):
                n = len(buf)
                if n - last_len < self.RATE * 0.3 or time.time() - last_at < self.PARTIAL_EVERY:
                    continue
                last_at, last_len = time.time(), n
                try:
                    if n - state["committed"] > self.PARTIAL_WINDOW * self.RATE:
                        end = state["committed"] + self.COMMIT_CHUNK * self.RATE
                        piece = Voice.transcribe_pcm(bytes(buf[state["committed"]:end]), "partial")
                        state["ctext"] = (state["ctext"] + " " + piece).strip()
                        state["committed"] = end
                    tail = Voice.transcribe_pcm(bytes(buf[state["committed"]:n]), "partial")
                except Exception as e:
                    send({"type": "error", "message": str(e)}); continue
                if stop.is_set():
                    return
                text = (state["ctext"] + " " + tail).strip()
                if text != state["sent"]:
                    state["sent"] = text
                    send({"type": "partial", "text": text, "seconds": round(n / self.RATE, 1)})

        worker = threading.Thread(target=partials, daemon=True)
        worker.start()
        final = False
        try:
            while True:
                op, data = self._ws_read()
                if op is None or op == 8:
                    break
                if op == 9:
                    self._ws_send(10, data, lock)
                elif op in (0, 2):
                    if len(buf) + len(data) <= self.MAX_SECONDS * self.RATE:
                        buf.extend(data)
                elif op == 1:
                    try:
                        msg = json.loads(data)
                    except Exception:
                        continue
                    if msg.get("type") == "stop":
                        final = True
                        break
                    if msg.get("type") == "cancel":
                        break
        except (OSError, ConnectionError, struct.error):
            pass
        stop.set()
        worker.join(timeout=30)
        if final:
            t0 = time.time()
            try:
                pcm = bytes(buf[: len(buf) - len(buf) % 2])
                text = Voice.transcribe_pcm(pcm, "final") if len(pcm) >= self.RATE * 0.3 else ""
                send({"type": "final", "text": text, "ms": int((time.time() - t0) * 1000),
                      "seconds": round(len(pcm) / self.RATE, 1)})
            except Exception as e:
                send({"type": "error", "message": str(e)})
        try:
            self._ws_send(8, b"", lock)
        except OSError:
            pass

    def do_DELETE(self):
        if not self._authed():
            return self._send(403, {"error": "bad token"})
        parts = urlparse(self.path).path.strip("/").split("/")
        if parts[:2] == ["api", "projects"]:
            code, payload = projects_route("DELETE", parts, {})
            return self._send(code, payload)
        if self._admin() and len(parts) == 3 and parts[:2] == ["api", "devices"]:
            ok = revoke_device(parts[2])
            return self._send(200 if ok else 404, {"message": "Revoked" if ok else "No such device"})
        return self._send(404, {"error": "not found"})

    def do_PATCH(self):
        if not self._authed():
            return self._send(403, {"error": "bad token"})
        parts = urlparse(self.path).path.strip("/").split("/")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except Exception:
            body = {}
        if parts[:2] == ["api", "projects"]:
            code, payload = projects_route("PATCH", parts, body)
            return self._send(code, payload)
        return self._send(404, {"error": "not found"})

    def agent_api(self, path, body):
        """Loft's tools for an agent (loft/agent_mcp.py). The token names the agent; only its own browser."""
        jid = BR.jid_for_token(self.headers.get("X-Loft-Agent", ""))
        card = jid and find_card(jid)
        if not card or self.device is not None:
            return self._send(403, {"message": "Not a Loft agent token."})
        try:
            code, payload = agent_route(card, path.rsplit("/", 1)[-1], body)
        except Exception as e:
            code, payload = 400, {"message": str(e)}
        return self._send(code, payload)

    def do_POST(self):
        if urlparse(self.path).path.startswith("/api/agent/"):
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except Exception:
                body = {}
            return self.agent_api(urlparse(self.path).path, body)
        if not self._authed():
            return self._send(403, {"error": "bad token"})
        parts = urlparse(self.path).path.strip("/").split("/")  # api/cards/<id>/<action> | api/cards
        if parts[:2] == ["api", "accounts"]:
            return webapi.handle_post(self, parts[2:])
        if parts == ["api", "voice"]:
            n = int(self.headers.get("Content-Length") or 0)
            if not n or n > 25_000_000:
                return self._send(400, {"message": "No audio received."})
            audio = self.rfile.read(n)
            ext = audio_suffix(self.headers.get("Content-Type"))
            t0 = time.time()
            try:
                return self._send(200, {"text": Voice.transcribe(audio, ext), "ms": int((time.time() - t0) * 1000)})
            except Exception as e:
                return self._send(500, {"message": str(e)})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except Exception:
            body = {}
        if parts[:2] in (["api", "phone"], ["api", "pair"], ["api", "devices"]):
            if self._admin():
                if self._admin_post(parts, body):
                    return
            return self._send(404, {"error": "not found"})
        if parts[:2] == ["api", "projects"] or parts == ["api", "view"]:
            code, payload = projects_route("POST", parts, body)
            return self._send(code, payload)
        if parts[:2] == ["api", "browser"] and len(parts) == 4:
            if not self._admin():
                return self._send(404, {"error": "not found"})
            try:
                code, payload = browser_route(parts[2], parts[3], body)
            except Exception as e:
                code, payload = 500, {"message": f"{type(e).__name__}: {e}"}
            return self._send(code, payload)
        if parts[:2] != ["api", "cards"]:
            return self._send(404, {"error": "not found"})
        cid, action = (None, "create") if len(parts) == 2 else (parts[2], parts[3] if len(parts) > 3 else "")
        if action == "answer":
            with LOCK:
                try:
                    code, payload = answer_route(cid, body)
                except Exception as e:
                    code, payload = 500, {"message": f"{type(e).__name__}: {e}"}
            return self._send(code, payload)
        with LOCK:
            try:
                code, msg = act(cid, action, body)
            except Exception as e:
                code, msg = 500, f"{type(e).__name__}: {e}"
        return self._send(code, {"message": msg})


class RemoteH(H):
    """Handler for the tailnet listener (Loft Mobile). See the module docstring."""
    server_version = "loft"
    BLOCKED = ("/api/pair", "/api/devices", "/api/phone", "/static/")

    def _gate(self):
        ip = self.client_address[0]
        try:
            peer_ok = ipaddress.ip_address(ip) in TAILNET
        except ValueError:
            peer_ok = False
        if not peer_ok:
            self._send(403, {"error": "tailnet only"}); return False
        path = urlparse(self.path).path
        if path in ("/", "/index.html") or path.startswith(self.BLOCKED):
            self._send(404, {"error": "not found"}); return False
        if Guard.blocked(ip):
            self._send(429, {"error": "too many failed attempts; try again in 10 minutes"}); return False
        dev = device_for_token(self._bearer())
        if not dev:
            Guard.fail(ip)
            self._send(401, {"error": "not paired (or the pairing was revoked)"}); return False
        ok, why = pin_check(dev, ip)
        if not ok:
            Guard.fail(ip)
            self._send(403, {"error": why}); return False
        self._attach(dev)
        return True

    def _authed(self):
        return self.device is not None   # _gate() already checked the device token

    def _desktop(self):
        return False

    def do_GET(self):
        if self._gate():
            super().do_GET()

    def do_POST(self):
        if self._gate():
            super().do_POST()

    def do_DELETE(self):
        if self._gate():
            super().do_DELETE()

    def do_PATCH(self):
        if self._gate():
            super().do_PATCH()


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    srv.daemon_threads = True
    # --port 0 picks a free port; this line is the handshake the desktop app reads.
    port = srv.server_address[1]
    # PROOF_KEY in a private (0600) file: the desktop app challenges the server with it before
    # trusting the port with the microphone. A file left by a crash is harmless: the key dies
    # with the process and was never sent over the wire.
    kf = paths.cache_dir() / f"server-{port}.key"
    def drop_key():
        try: kf.unlink(missing_ok=True)
        except OSError: pass
    try:
        kf.parent.mkdir(parents=True, exist_ok=True)
        drop_key()
        with os.fdopen(os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as fh:
            fh.write(PROOF_KEY)
        import atexit; atexit.register(drop_key)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    except OSError as e:
        print(f"couldn't write the key file: {e}", file=sys.stderr, flush=True)
    os.environ["LOFT_PORT"] = str(port)   # agents started from here reach Loft's tools on this port
    print(f"loft on http://127.0.0.1:{port}", flush=True)
    if "--exit-with-stdin" in sys.argv:   # desktop app: exit when the parent goes away
        threading.Thread(target=lambda: (sys.stdin.read(), drop_key(), os._exit(0)), daemon=True).start()
    if DEV_LOOPBACK_DEVICES:
        print("DEV: device tokens are accepted on 127.0.0.1 too (--dev-loopback-devices)", flush=True)
    on = load_config().get("phone", False)
    if "--tailnet" in sys.argv:
        on = True
    if "--no-tailnet" in sys.argv:
        on = False
    global WATCH_OFF
    WATCH_OFF = "--no-tailnet" in sys.argv
    if on:
        Phone.start()
    threading.Thread(target=Phone.watch, daemon=True).start()
    if "--no-pending-loop" not in sys.argv:
        threading.Thread(target=pending_loop, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
