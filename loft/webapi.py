"""Account routes for the Loft web app (kept out of server.py so it stays small).

  GET  /api/accounts                      accounts + status + usage, setup hints
  POST /api/accounts/add                  {label, color?} -> new account with its own login dir
  POST /api/accounts/setup                {label, color?, migrate?, importCswap?} first run
  POST /api/accounts/import-cswap         {nums?: [...]}
  POST /api/accounts/<id>/rename|color|default|remove|identify
  WS   /ws/term/login-<id>?t=TOKEN        `claude auth login` for that account in a pty

Desktop only: when the server also has a phone listener, requests from it are refused.
"""
import base64
import fcntl
import hashlib
import json
import os
import pty
import select
import signal
import struct
import termios
import threading
import time

from . import accounts as A
from . import cli as LC
from . import core as C
from . import paths
from . import usage as U

LOCK = threading.Lock()


def _desktop(h):
    f = getattr(h, "_desktop", None)  # present once phone access is merged in
    return f() if callable(f) else True


def _body(h):
    try:
        return json.loads(h.rfile.read(int(h.headers.get("Content-Length") or 0)) or b"{}")
    except Exception:
        return {}


def status(acc):
    try:
        t = U.token_info(acc)
        return "expired" if t["expired"] else "ok", ""
    except U.TokenError as e:
        return "missing", str(e)


def account_view(acc, cfg):
    st, why = status(acc)
    u = U.get(acc)
    return {**A.public(acc, cfg), "pending": bool(acc.get("pending")), "status": st, "statusText": why,
            "usage": u, "loginDir": A.env_value(acc).replace(str(paths.HOME), "~") or "(default login)",
            "keychain": A.keychain_service(acc) if paths.is_mac() else ""}


def listing():
    cfg = A.load()
    out = {"configured": A.configured(), "accounts": [account_view(a, cfg) for a in cfg["accounts"]],
           "default": cfg.get("default"), "colors": [{"name": n, "hex": A.COLORS[n]} for n in A.PALETTE],
           "platform": "macos" if paths.is_mac() else "linux", "cswap": [], "legacy": False, "defaultEmail": ""}
    if not out["configured"]:
        out["legacy"] = bool(A.legacy_setup()["work_dir"])
        email, _ = LC.identify({"id": "x", "creds_dir": A.DEFAULT_CREDS, "label": "", "color": "blue"})
        out["defaultEmail"] = email or ""
    try:
        dmail = next((a.get("email") for a in cfg["accounts"] if a["creds_dir"] == A.DEFAULT_CREDS and a.get("email")), "") or out["defaultEmail"]
        _, log = A.import_cswap(json.loads(json.dumps(cfg)), default_email=dmail or None)
        out["cswap"] = [l[len("added "):] for l in log if l.startswith("added ")]
        out["cswapSkipped"] = [l for l in log if l.startswith("skip") and "no session" in l]
    except Exception:
        pass
    return out


def handle_get(h, path):
    if not _desktop(h):
        return h._send(404, {"error": "not found"})
    if path == "/api/accounts":
        return h._send(200, listing())
    if path.startswith("/ws/term/login-"):
        return login_ws(h, path.rsplit("/", 1)[-1][len("login-"):])
    return h._send(404, {"error": "not found"})


def handle_post(h, rest):
    if not _desktop(h):
        return h._send(404, {"error": "not found"})
    body = _body(h)
    with LOCK:
        try:
            code, payload = post(rest, body)
        except SystemExit as e:
            code, payload = 400, {"message": str(e)}
        except Exception as e:
            code, payload = 500, {"message": f"{type(e).__name__}: {e}"}
    return h._send(code, payload)


def post(rest, body):
    cfg = A.load()
    if rest == ["setup"]:
        if A.configured():
            return 400, {"message": "Already set up."}
        if body.get("migrate") and A.legacy_setup()["work_dir"]:
            cfg = A.migrate_config()
            for a in cfg["accounts"]:
                a["email"] = LC.identify(a)[0] or ""
        else:
            label = (body.get("label") or "").strip() or "Personal"
            acc = {"id": A.slug(label), "label": label, "color": body.get("color") if body.get("color") in A.COLORS else "blue",
                   "creds_dir": A.DEFAULT_CREDS}
            acc["email"] = LC.identify(acc)[0] or ""
            cfg = {"version": 1, "default": acc["id"], "accounts": [acc], "rules": []}
        if body.get("importCswap"):
            dflt = next((a for a in cfg["accounts"] if a["creds_dir"] == A.DEFAULT_CREDS), {})
            cfg, _ = A.import_cswap(cfg, default_email=dflt.get("email") or None)
        A.save(cfg)
        return 200, {"message": "Saved. Add more accounts any time.", **listing()}
    if rest == ["add"]:
        label = (body.get("label") or "").strip()
        if not label:
            return 400, {"message": "Give the account a name."}
        acc = LC.new_account(cfg, label, body.get("color") if body.get("color") in A.COLORS else None)
        acc["pending"] = True
        os.makedirs(acc["creds_dir"], mode=0o700, exist_ok=True)
        cfg["accounts"].append(acc)
        A.save(cfg)
        return 200, {"message": f"Sign {label} in below.", "id": acc["id"]}
    if rest == ["import-cswap"]:
        dflt = next((a for a in cfg["accounts"] if a["creds_dir"] == A.DEFAULT_CREDS), {})
        cfg, log = A.import_cswap(cfg, default_email=dflt.get("email") or None, only=body.get("nums") or None)
        A.save(cfg)
        n = len([l for l in log if l.startswith("added")])
        return 200, {"message": f"Imported {n} account{'s' if n != 1 else ''} from cswap." if n else "Nothing new to import.", "log": log}
    if rest == ["organize"]:
        lines = []
        C.organize(dry=False, out=lines.append)
        n = len([l for l in "\n".join(lines).splitlines() if l.strip() and "already" not in l])
        return 200, {"message": f"Organized ({n} changes)."}
    if len(rest) != 2:
        return 404, {"message": "Unknown account route."}
    acc_id, action = rest
    acc = A.get(acc_id, cfg)
    if not acc:
        return 404, {"message": "That account is gone."}
    if action == "rename":
        label = (body.get("label") or "").strip()
        if not label:
            return 400, {"message": "A name can't be empty."}
        acc["label"] = label[:40]
    elif action == "color":
        if body.get("color") not in A.COLORS:
            return 400, {"message": "Unknown color."}
        acc["color"] = body["color"]
    elif action == "default":
        cfg["default"] = acc_id
    elif action == "remove":
        msg = LC.remove_account(cfg, acc_id)
        return (200 if msg.startswith("removed") else 400), {"message": msg}
    elif action == "identify":
        email, why = LC.identify(acc)
        if not email:
            return 400, {"message": f"{acc['label']}: {why}."}
        dupe = next((a for a in cfg["accounts"] if a["id"] != acc_id and (a.get("email") or "").lower() == email.lower()), None)
        acc["email"] = email
        acc.pop("pending", None)
        A.save(cfg)
        return 200, {"message": f"{acc['label']} is signed in as {email}." + (f" Note: {dupe['label']} is the same login." if dupe else "")}
    else:
        return 404, {"message": f"Unknown action {action}."}
    A.save(cfg)
    return 200, {"message": "Saved."}


# ------------------------------------------------------------------ login terminal

def _ws_handshake(h):
    key = h.headers.get("Sec-WebSocket-Key")
    accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
    h.protocol_version = "HTTP/1.1"  # WebKit (Safari, Loft.app) rejects an HTTP/1.0 101
    h.send_response(101)
    h.send_header("Upgrade", "websocket")
    h.send_header("Connection", "Upgrade")
    h.send_header("Sec-WebSocket-Accept", accept)
    h.end_headers()
    h.close_connection = True


def login_ws(h, acc_id):
    """`claude auth login` for one account, in a pty bridged to xterm.js."""
    acc = A.get(acc_id)
    if not h.headers.get("Sec-WebSocket-Key") or not acc:
        return h._send(404, {"error": "no such account"})
    _ws_handshake(h)
    env = LC.login_env(acc)
    env.update(TERM="xterm-256color", COLORTERM="truecolor")
    d = A.env_value(acc)
    if d:
        os.makedirs(d, mode=0o700, exist_ok=True)
    argv = LC.login_argv(acc, acc.get("email") or None)
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.chdir(str(paths.HOME))
            os.execvpe(argv[0], argv, env)
        finally:
            os._exit(127)
    lock = threading.Lock()
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    intro = (f"\x1b[2mSigning in {acc['label']} · {A.VAR}={d!r}\r\n"
             "A browser window opens. Sign in with the account you want here; if claude.ai picks\r\n"
             "the wrong one automatically, sign out of claude.ai in that browser first.\x1b[0m\r\n\r\n").encode()
    h._ws_send(2, intro, lock)

    def pump():
        try:
            while True:
                r, _, _ = select.select([fd], [], [], 1.0)
                if fd in r:
                    data = os.read(fd, 65536)
                    if not data:
                        break
                    h._ws_send(2, data, lock)
        except OSError:
            pass
        try:
            h._ws_send(2, b"\r\n\x1b[2m[login finished]\x1b[0m\r\n", lock)
            h._ws_send(8, b"", lock)
        except OSError:
            pass

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    try:
        while True:
            op, data = h._ws_read()
            if op is None or op == 8:
                break
            if op == 9:
                h._ws_send(10, data, lock)
            elif op == 2:
                os.write(fd, data)
            elif op == 1:
                try:
                    msg = json.loads(data)
                except Exception:
                    continue
                if msg.get("type") == "resize":
                    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", int(msg.get("rows") or 30), int(msg.get("cols") or 100), 0, 0))
    except (OSError, ConnectionError):
        pass
    finally:
        for sig in (signal.SIGHUP, signal.SIGKILL):
            try:
                os.kill(pid, sig)
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
                continue
            break
        try:
            os.close(fd)
        except OSError:
            pass
