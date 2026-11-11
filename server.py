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
import base64, fcntl, ipaddress, pty, select, signal, socket, struct, termios, hashlib, importlib.machinery, importlib.util, json, os, secrets, subprocess, sys, threading, time
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
TOKEN = secrets.token_hex(16)
CLAUDE = str(HOME / ".local/bin/claude")
VERSION = "2.1"
HOSTNAME = socket.gethostname().split(".")[0]
CONF_DIR = None  # set below from loft.paths (LOFT_CONF_DIR, XDG_CONFIG_HOME, ~/.config/loft)
TAILNET = ipaddress.ip_network("100.64.0.0/10")


sys.path.insert(0, str(HERE))
from loft import accounts as A, board as B, core as C, paths, projects as P, usage as U, webapi  # noqa: E402

CLAUDE = paths.claude_bin()
CONF_DIR = paths.config_dir()
LOCK = threading.Lock()  # one mutating action at a time
VENV_PY = paths.venv_python()


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


def send_to_agent(card, text):
    """Type text into a background agent's prompt and press Enter.

    There is no `claude send`, so this attaches briefly in a hidden pty, pastes
    the text (bracketed paste, so multi-line stays one message), presses Enter
    and detaches. The session keeps running, exactly as after `claude attach`.
    """
    env = B.account_env(card)
    env.update(TERM="xterm-256color", COLORTERM="truecolor")
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(str(HOME))
        os.execvpe(CLAUDE, [CLAUDE, "attach", card["id"]], env)
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))

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
                    got += len(chunk); last = time.time()
                elif got and time.time() - last > idle:
                    return got
            return got

        if not drain(1.2, 12):
            raise RuntimeError("Couldn't open the agent's session.")
        os.write(fd, b"\x1b[200~" + text.encode() + b"\x1b[201~")
        time.sleep(0.4)
        os.write(fd, b"\r")
        drain(0.8, 3)
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
    return "Sent"


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


def snapshot():
    cards = B.load_cards(show_all_done=True)
    for c in cards:
        c["updated"] = c["updated"].isoformat() if c["updated"] else None
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
    """Apply queued account switches once their agent leaves Working."""
    while True:
        time.sleep(every)
        try:
            with LOCK:
                B.apply_pending(log=lambda m: print(m, flush=True))
        except Exception as e:
            print(f"pending switch loop: {type(e).__name__}: {e}", flush=True)


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
        return 200, send_to_agent(card, text)
    if action == "prompt" and card["kind"] == "todo":
        todos = B.load_todos()
        for t in todos:
            if t["id"] == cid:
                t["prompt"] = (body.get("prompt") or "").strip()
        B.save_todos(todos)
        return 200, "Prompt saved"
    return 400, f"Unknown action {action}"


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
            if u.path == "/api/events" or u.path.startswith("/ws/term/"):
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
        if path.startswith("/static/"):
            f = (HERE / path.lstrip("/")).resolve()
            if HERE in f.parents and f.is_file():
                ctype = {"js": "text/javascript", "css": "text/css", "svg": "image/svg+xml", "png": "image/png",
                         "woff2": "font/woff2"}.get(f.suffix.lstrip("."), "application/octet-stream")
                return self._send(200, f.read_bytes(), ctype)
            return self._send(404, {"error": "not found"})
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
        if path == "/api/events":
            return self.events()
        if path.startswith("/api/accounts") or path.startswith("/ws/term/login-"):
            return webapi.handle_get(self, path)
        if path.startswith("/ws/term/"):
            return self.term_ws(path.rsplit("/", 1)[-1])
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

    def do_POST(self):
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
        if parts[:2] != ["api", "cards"]:
            return self._send(404, {"error": "not found"})
        cid, action = (None, "create") if len(parts) == 2 else (parts[2], parts[3] if len(parts) > 3 else "")
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
    # Private (0600) copy of this run's token: the desktop app reads it to confirm the server
    # on the port is the user's own Loft before trusting it with the microphone.
    try:
        tf = paths.cache_dir() / f"server-{port}.token"
        tf.parent.mkdir(parents=True, exist_ok=True)
        tf.unlink(missing_ok=True)
        with os.fdopen(os.open(tf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as fh:
            fh.write(TOKEN)
    except OSError as e:
        print(f"couldn't write the token file: {e}", file=sys.stderr, flush=True)
    print(f"loft on http://127.0.0.1:{port}", flush=True)
    if "--exit-with-stdin" in sys.argv:   # desktop app: exit when the parent goes away
        threading.Thread(target=lambda: (sys.stdin.read(), os._exit(0)), daemon=True).start()
    if DEV_LOOPBACK_DEVICES:
        print("DEV: device tokens are accepted on 127.0.0.1 too (--dev-loopback-devices)", flush=True)
    on = load_config().get("phone", False)
    if "--tailnet" in sys.argv:
        on = True
    if "--no-tailnet" in sys.argv:
        on = False
    if on:
        Phone.start()
    if "--no-pending-loop" not in sys.argv:
        threading.Thread(target=pending_loop, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
