#!/usr/bin/env python3
"""cagents app server: a tiny localhost API + one HTML page for the agents board.

Stdlib only. Reuses ~/.local/bin/cagents (accounts, organize) and
~/.local/share/cagents/board.py (cards, actions) so the TUI and the app share
one set of rules.

  python3 server.py [--port 7717]

Binds 127.0.0.1 only. Every API call must carry the per-run token that is baked
into the served page (X-Cagents-Token), so other sites in the browser can't
drive it.
"""
import base64, fcntl, pty, select, signal, struct, termios, hashlib, importlib.machinery, importlib.util, json, os, secrets, subprocess, sys, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

HOME = Path.home()
HERE = Path(__file__).resolve().parent
PORT = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 7717
TOKEN = secrets.token_hex(16)
CLAUDE = str(HOME / ".local/bin/claude")


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


C = _load("cagents_cli", HOME / ".local/bin/cagents")
B = _load("cagents_board", HOME / ".local/share/cagents/board.py")
B.C = C
LOCK = threading.Lock()  # one mutating action at a time


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
    w5, w7 = B.usage("work")
    p5, p7 = B.usage("personal")
    resets = {}
    for acc in ("work", "personal"):
        try:
            lines = (B.RUNTIME / f"claude-usage-cache-{acc}").read_text().splitlines()
            resets[acc] = {"five": lines[2] if len(lines) > 2 else "", "seven": lines[3] if len(lines) > 3 else ""}
        except Exception:
            resets[acc] = {"five": "", "seven": ""}
    return {
        "cards": sorted(cards, key=lambda c: (not c["pinned"], c["name"].lower())),
        "usage": {
            "work": {"five": w5, "seven": w7, **{f"{k}Reset": v for k, v in resets["work"].items()}},
            "personal": {"five": p5, "seven": p7, **{f"{k}Reset": v for k, v in resets["personal"].items()}},
        },
        "dirs": [d.replace(str(HOME), "~") for d in B.recent_dirs()],
        "doneDays": C.DONE_DAYS,
    }


def find_card(cid):
    for c in B.load_cards(show_all_done=True):
        if c["id"] == cid:
            return c
    return None


def open_terminal(card):
    env = B.account_env(card["work"])
    title = f"{'WORK' if card['work'] else 'PERSONAL'} · {card['name']}"
    term = os.environ.get("CAGENTS_TERMINAL", "konsole")
    if term == "konsole":
        cmd = ["konsole", "-p", f"tabtitle={title}", "-e", CLAUDE, "attach", card["id"]]
    else:
        cmd = [term, "-e", CLAUDE, "attach", card["id"]]
    subprocess.Popen(cmd, env=env, cwd=str(HOME), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return f"opened {card['name']} in a terminal"


def act(cid, action, body):
    """Run one card action. Returns (status, message)."""
    if action == "create":
        cwd = (body.get("cwd") or "").strip()
        title = (body.get("title") or "").strip()
        if not title:
            return 400, "Give the task a title."
        if not cwd or not os.path.isdir(os.path.expanduser(cwd)):
            return 400, "Pick a project folder that exists."
        if os.path.realpath(os.path.expanduser(cwd)) == str(HOME):
            return 400, "Pick a project folder: Claude won't start background agents in your home folder."
        todo = {"id": "t" + secrets.token_hex(4), "title": title,
                "account": "work" if body.get("account") == "work" else "personal",
                "cwd": cwd.replace(str(HOME), "~"), "prompt": (body.get("prompt") or "").strip(),
                "created": B.now_utc().isoformat()}
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
        return 200, B.act_switch_account(card)
    if action == "pin":
        return 200, B.act_pin(card)
    if action == "archive":
        return 200, B.act_archive(card)
    if action == "stop":
        r = subprocess.run([CLAUDE, "stop", cid], capture_output=True, text=True,
                           env=B.account_env(card["work"]), cwd=str(HOME), timeout=60)
        out = (r.stdout + r.stderr).strip().splitlines()
        return 200, out[-1] if out else "Stopped"
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

    def _authed(self):
        tok = self.headers.get("X-Cagents-Token", "")
        if not tok:  # EventSource can't send headers; it passes ?t= (GET /api/events only)
            from urllib.parse import parse_qs
            u = urlparse(self.path)
            if u.path == "/api/events" or u.path.startswith("/ws/term/"):
                tok = (parse_qs(u.query).get("t") or [""])[0]
        return secrets.compare_digest(tok, TOKEN)

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
        if path == "/api/state":
            return self._send(200, snapshot())
        if path.startswith("/api/timeline/"):
            return self._send(200, timeline(path.rsplit("/", 1)[-1]))
        if path == "/api/events":
            return self.events()
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
        except (BrokenPipeError, ConnectionResetError):
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

    def term_ws(self, cid):
        key = self.headers.get("Sec-WebSocket-Key")
        card = find_card(cid)
        if not key or not card or card["kind"] != "agent":
            return self._send(404, {"error": "no such agent"})
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        env = B.account_env(card["work"])
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

    def do_POST(self):
        if not self._authed():
            return self._send(403, {"error": "bad token"})
        parts = urlparse(self.path).path.strip("/").split("/")  # api/cards/<id>/<action> | api/cards
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except Exception:
            body = {}
        if parts[:2] != ["api", "cards"]:
            return self._send(404, {"error": "not found"})
        cid, action = (None, "create") if len(parts) == 2 else (parts[2], parts[3] if len(parts) > 3 else "")
        with LOCK:
            try:
                code, msg = act(cid, action, body)
            except Exception as e:
                code, msg = 500, f"{type(e).__name__}: {e}"
        return self._send(code, {"message": msg})


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    srv.daemon_threads = True
    print(f"cagents on http://127.0.0.1:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
