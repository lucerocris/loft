"""Cards and actions for Loft (shared by the web app and the terminal board).

A card is either a background agent (~/.claude/jobs/<id>/state.json) or a todo
of your own (~/.claude/jobs-board/todos.json); starting a todo turns it into a
real background agent on the account it was written for.

Terminal board columns move by themselves as agents change state:
    TODO  ->  WORKING  ->  NEEDS YOU  ->  DONE
"""
import contextlib, curses, io, json, os, re, shlex, shutil, subprocess, sys, textwrap, time, uuid
from datetime import datetime, timezone
from pathlib import Path

from . import accounts as A
from . import attachments as AT
from . import browser as BR
from . import core as C
from . import paths
from . import projects as P
from . import usage as U

HOME = paths.HOME
COLS = [("todo", "TODO"), ("running", "WORKING"), ("needs", "NEEDS YOU"), ("done", "DONE")]
REFRESH_MS = 2000


def jobs():
    return C.jobs_dir()


def board_dir():
    return paths.claude_home() / "jobs-board"


def todos_file():
    return board_dir() / "todos.json"


# Kept as attributes for code that reads B.JOBS (the server's timeline route).
class _Lazy:
    def __truediv__(self, other):
        return jobs() / other

    def __str__(self):
        return str(jobs())


JOBS = _Lazy()


# ---------------------------------------------------------------- data

def now_utc():
    return datetime.now(timezone.utc)


def live_shorts():
    try:
        return C.live_sessions()
    except Exception:
        return set()


def load_todos():
    try:
        return json.loads(todos_file().read_text())
    except Exception:
        return []


def save_todos(todos):
    board_dir().mkdir(parents=True, exist_ok=True)
    tmp = todos_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(todos, indent=2))
    tmp.replace(todos_file())


def pins_read():
    try:
        return list(json.loads((jobs() / "pins.json").read_text()))
    except Exception:
        return []


def pins_write(pins):
    if (jobs() / "pins.json.lock").exists():
        raise RuntimeError("pins.json is locked by Claude, try again")
    (jobs() / "pins.json").write_text(json.dumps(pins, indent=2))


def _acc_fields(acc, cfg):
    """`account` (id, None = a login Loft doesn't know) and the legacy `work` flag
    (true for any account that isn't the default one)."""
    dflt = cfg.get("default")
    return {"account": acc["id"] if acc else None, "work": (acc["id"] != dflt) if acc else True}


def todo_account(t, cfg):
    return A.get(t.get("account"), cfg) or A.default_account(cfg)


def load_cards(show_all_done):
    cfg = A.load()
    try:
        pcfg = P.load()
    except SystemExit:
        pcfg = P.empty()
    live = live_shorts()
    pins = set(pins_read())
    cards = []
    for sf in jobs().glob("*/state.json"):
        try:
            st = json.loads(sf.read_text())
        except Exception:
            continue
        jid = sf.parent.name
        b = C.bucket(st)
        if b == "failed":
            b = "needs"
        detail = (st.get("detail") or "").replace("\n", " ")
        if detail.startswith("(idle") and b == "needs":
            b = "todo"  # opened but never given a prompt: it is a todo
        upd = C.parse_t(st.get("updatedAt") or st.get("createdAt") or "")
        if b == "done" and not show_all_done and upd and (now_utc() - upd).days >= C.DONE_DAYS:
            continue
        out = st.get("output") if isinstance(st.get("output"), dict) else {}
        acc, _ = C.job_account(st, cfg)
        meta = C.meta_read(jid)
        cards.append({
            "kind": "agent", "id": jid, "name": meta.get("title") or st.get("name") or jid,
            "summary": meta.get("summary") or None, "assistant": bool(meta.get("assistant")),
            **_acc_fields(acc, cfg), "col": b, "state": st.get("state"),
            "detail": detail, "result": (out or {}).get("result") or "",
            "cwd": st.get("worktreePath") or st.get("cwd") or "", "folderName": C.project(st),
            "updated": upd, "live": jid in live, "pinned": jid in pins,
            "failed": st.get("state") == "failed",
            "links": [ch.get("href") for ch in (st.get("children") or []) if isinstance(ch, dict) and ch.get("href")],
            "pending": _pending_view(jid),
            "queue": _queue_view(jid, acc["id"] if acc else None, cfg),
        })
    for t in load_todos():
        cwd = os.path.expanduser(t.get("cwd") or "~")
        cards.append({
            "kind": "todo", "id": t["id"], "name": t.get("title") or "untitled", "summary": t.get("summary") or None,
            "assistant": False, **_acc_fields(todo_account(t, cfg), cfg),
            "col": "todo", "state": "todo", "detail": t.get("prompt") or "(no prompt yet)", "result": "",
            "cwd": cwd, "folderName": Path(cwd).name, "projectPin": t.get("project"),
            "updated": C.parse_t(t.get("created") or ""), "live": False, "pinned": False, "failed": False, "links": [],
            "pending": None, "files": [f for f in t.get("files") or [] if os.path.isfile(f)],
            "queue": {"items": [], "until": limit_until(todo_account(t, cfg)["id"], cfg) or ""} if t.get("queued") else None,
        })
    for c in cards:
        c["folder"] = c["cwd"]
        pin = c.pop("projectPin", None)
        if pin and P.get(pin, pcfg) and c["id"] not in pcfg["assignments"]:
            c["project"] = pin           # a todo written for a project
        else:
            c["project"] = P.project_of(c["id"], c["cwd"], pcfg)
    return cards


def _pending_view(jid):
    p = pending_read(jid)
    return {"account": p["account"], "error": p.get("error") or ""} if p else None


def sort_cards(cards):
    order = {a["id"]: i for i, a in enumerate(A.all_accounts())}
    return sorted(cards, key=lambda c: (not c["pinned"], order.get(c["account"], 99), c["name"].lower()))


def usage(account):
    """("5h %", "7d %") strings for an account id ("—" when unknown)."""
    acc = A.get(account)
    if not acc:
        return "—", "—"
    u = U.get(acc)
    s = lambda v: "—" if v is None else str(v)
    return s(u["five"]), s(u["seven"])


def resolve_account(x, cfg=None):
    """An account from a card, account dict, account id, or the legacy work bool."""
    cfg = cfg or A.load()
    if isinstance(x, dict) and "creds_dir" in x:
        return x
    if isinstance(x, dict):
        x = x.get("account")
        if x is None:
            return A.default_account(cfg)
    if isinstance(x, bool):  # legacy callers: True = the first non-default account
        if not x:
            return A.default_account(cfg)
        return A.get("work", cfg) or next((a for a in cfg["accounts"] if a["id"] != cfg.get("default")), A.default_account(cfg))
    return A.get(x, cfg) or A.default_account(cfg)


def account_env(x):
    """os.environ pointed at an account's login (always explicit)."""
    acc = resolve_account(x)
    env = dict(os.environ)
    return A.apply_env(env, acc) if acc else env


def account_label(x):
    acc = resolve_account(x)
    return acc["label"] if acc else "?"


def config_dirs():
    try:
        return list(json.loads((paths.config_dir() / "config.json").read_text()).get("dirs") or [])
    except Exception:
        return []


def recent_dirs():
    seen, out = set(), []
    for d in config_dirs():
        p = os.path.expanduser(d)
        if os.path.isdir(p) and p not in seen:
            seen.add(p); out.append(p)
    for sf in sorted(jobs().glob("*/state.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            st = json.loads(sf.read_text())
        except Exception:
            continue
        p = st.get("originCwd") or st.get("cwd")
        if p and p != str(HOME) and os.path.isdir(p) and "/.claude/worktrees/" not in p and p not in seen:
            seen.add(p); out.append(p)
    return out[:9]


# ---------------------------------------------------------------- actions

def claude(*args, card=None, timeout=60):
    return subprocess.run([paths.claude_bin(), *args], capture_output=True, text=True,
                          env=account_env(card), cwd=str(HOME), timeout=timeout)


def write_state(jid, mutate):
    sf = jobs() / jid / "state.json"
    st = json.loads(sf.read_text())
    new = mutate(dict(st))
    if new != st:
        sf.write_text(json.dumps(new, indent=2))


def settle(card):
    """Finished agents keep an idle worker alive for a while. Stop it so the card can be
    edited. Returns an error string if the agent is actually busy."""
    if not card.get("live"):
        return None
    if card["col"] not in ("done",) and card.get("state") not in ("done", "failed"):
        return "it's running - stop it first"
    claude("stop", card["id"], card=card)
    for _ in range(50):
        if card["id"] not in live_shorts():
            card["live"] = False
            return None
        time.sleep(0.1)
    return "couldn't stop its idle worker, try again"


def act_rename(card, name, summary=None):
    """Set a title (and/or summary) by hand. Works while the agent runs: Loft keeps the title in
    its own sidecar (core.meta_*); state.json follows when no worker is alive to overwrite it."""
    from . import titles as TI
    TI.save(card, title=name or None, summary=summary, source="user")
    return "renamed" if name else "saved"


# ---------------------------------------------------------------- account switch
#
# An agent's login is providerEnv[CLAUDE_SECURESTORAGE_CONFIG_DIR] in its
# state.json. A cold (re)start reads it from there, but the daemon caches the
# env of a live worker in daemon/roster.json and an instant respawn on stop
# (e.g. when notifications are pending) reuses that cached env. So a switch
# writes the login, stops the idle worker, and checks the roster: a worker
# that came back on the old login is stopped again, a few times.
#
# A running agent is switched after its turn by default: the wish is kept in
# a sidecar (PENDING in the job dir) and the server's poll loop applies it once
# the card leaves Working (apply_pending). "now" stops the turn instead.

PENDING = "loft-pending-account.json"
STOP_TRIES = 4          # stops before giving up on a worker that keeps respawning on the old login
STOP_WAIT = 5.0         # seconds to wait for one stop to settle
PENDING_TRIES = 3       # poll-loop attempts before a queued switch is left with its error showing


def pending_read(jid):
    try:
        p = json.loads((jobs() / jid / PENDING).read_text())
        return p if isinstance(p, dict) and p.get("account") else None
    except Exception:
        return None


def pending_write(jid, data):
    f = jobs() / jid / PENDING
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(f)


def pending_clear(jid):
    try:
        (jobs() / jid / PENDING).unlink()
        return True
    except FileNotFoundError:
        return False


def roster_worker(jid):
    """The daemon's live worker record for an agent, or None."""
    try:
        w = (json.loads((paths.claude_home() / "daemon" / "roster.json").read_text()).get("workers") or {}).get(jid)
    except Exception:
        return None
    if not w or not w.get("pid") or not C.pid_alive(w["pid"]):
        return None
    return w


def worker_account(w, cfg):
    """Account id a live worker's cached env bills (None = unknown login)."""
    acc = A.account_of_env(((w or {}).get("dispatch") or {}).get("env") or {}, cfg)
    return acc["id"] if acc else None


def stop_on(card, target, cfg=None, tries=STOP_TRIES, wait=STOP_WAIT, sleep=None):
    """Stop an agent's worker so its next start uses the login just saved for `target`.

    Returns None when no worker is left on the old login (gone, or respawned on the
    new one), else an error string. A worker the daemon respawns instantly from its
    cached env (roster.json) is stopped again, up to `tries` stops."""
    cfg = cfg or A.load()
    sleep = sleep or time.sleep
    jid = card["id"]
    w = roster_worker(jid)
    if not w:
        return None
    for _ in range(tries):
        old_pid = w["pid"]
        claude("stop", jid, card=card)
        waited, w = 0.0, roster_worker(jid)
        while w and w["pid"] == old_pid and waited < wait:
            sleep(0.2); waited += 0.2
            w = roster_worker(jid)
        if not w:
            return None
        if w["pid"] == old_pid:
            return "couldn't stop its worker, try again"
        if worker_account(w, cfg) == target["id"]:
            return None          # respawned, but already on the new login
        sleep(0.5)                # respawned from the cached old env: stop it again
    return (f"its worker keeps restarting on the old login (tried {tries} times); "
            f"{target['label']} is saved and takes over the next time it starts")


def _save_login(jid, target):
    def m(st):
        env = {k: v for k, v in (st.get("providerEnv") or {}).items() if k not in (A.VAR, "CLAUDE_CONFIG_DIR")}
        return {**st, "providerEnv": A.apply_env(env, target), "color": target["color"]}
    write_state(jid, m)


def _who(target):
    return target["label"]


def switch_account(card, to=None, when=None, cfg=None):
    """Move a card to another account. Returns {"ok", "message", "pending"}.

    `to` is an account id (None = the next account in the list). `when` only
    matters for a running agent: "after" (default) queues it for the end of the
    turn, "now" stops the turn. Picking the account the card already bills
    cancels a queued switch."""
    cfg = cfg or A.load()
    accs = cfg["accounts"]
    out = lambda ok, msg, pend=None: {"ok": ok, "message": msg, "pending": pend}
    if when not in (None, "after", "now"):
        return out(False, f"when must be 'after' or 'now', not '{when}'")
    if len(accs) < 2:
        return out(False, "only one account is set up (add one with `loft account add`)")
    if to:
        target = A.get(to, cfg)
        if not target:
            return out(False, f"no account '{to}'")
    else:
        ids = [a["id"] for a in accs]
        cur = ids.index(card["account"]) if card.get("account") in ids else -1
        target = accs[(cur + 1) % len(accs)]
    if card["kind"] == "todo":
        todos = load_todos()
        for t in todos:
            if t["id"] == card["id"]:
                t["account"] = target["id"]
        save_todos(todos)
        return out(True, f"Now bills {_who(target)}")
    jid = card["id"]
    if target["id"] == card.get("account"):
        if pending_clear(jid):
            return out(True, f"Cancelled; it stays on {_who(target)}")
        return out(True, f"Already bills {_who(target)}")
    running = card.get("col") == "running" or card.get("state") == "working"
    if running and when != "now":
        pending_write(jid, {"account": target["id"], "requested": now_utc().isoformat(), "tries": 0})
        return out(True, f"Bills {_who(target)} after this turn", target["id"])
    pending_clear(jid)
    _save_login(jid, target)
    err = stop_on(card, target, cfg)
    if err:
        return out(False, f"Saved {_who(target)}, but {err}")
    if running:
        return out(True, f"Stopped the turn. Now bills {_who(target)}; continues there on your next reply")
    return out(True, f"Now bills {_who(target)} — continues there on your next reply")


def act_switch_account(card, to=None, when=None):
    """Message-only form of switch_account (terminal board, older callers)."""
    return switch_account(card, to, when)["message"]


def apply_pending(cards=None, cfg=None, log=print):
    """Apply queued switches whose agent is no longer Working. Called by the server's
    poll loop. Returns the list of (id, result) it acted on."""
    cfg = cfg or A.load()
    done = []
    for sf in jobs().glob(f"*/{PENDING}"):
        jid = sf.parent.name
        p = pending_read(jid)
        if not p:
            continue
        card = next((c for c in (cards or []) if c["id"] == jid), None) if cards is not None else None
        if card is None:
            card = next((c for c in load_cards(True) if c["id"] == jid), None)
        if not card:
            pending_clear(jid); continue
        if card["col"] == "running" or card.get("state") == "working":
            continue
        if p.get("tries", 0) >= PENDING_TRIES:
            continue                       # gave up; the badge shows the error until cancelled
        target = A.get(p["account"], cfg)
        if not target:
            pending_clear(jid); continue
        if card.get("account") != target["id"]:
            _save_login(jid, target)
        err = stop_on(card, target, cfg)
        if err:
            p.update(tries=p.get("tries", 0) + 1, error=f"Saved {_who(target)}, but {err}")
            pending_write(jid, p)
            r = {"ok": False, "message": p["error"], "pending": target["id"]}
        else:
            pending_clear(jid)
            r = {"ok": True, "message": f"Now bills {_who(target)} — continues there on your next reply", "pending": None}
        log(f"pending switch {jid} -> {target['id']}: {r['message']}")
        done.append((jid, r))
    return done


# ---------------------------------------------------------------- prompt queue
# Prompts wait in <job>/loft-queue.json until the agent is out of Working and its
# account is under its 5-hour and 7-day limits; the server's poll loop sends them
# one at a time. A todo started while its account is limited is marked "queued"
# and launched by the same loop once the limit resets.

QUEUE = "loft-queue.json"
QUEUE_GAP = 20      # seconds after a send before the next: state.json lags the turn's start
QUEUE_TRIES = 3     # failed sends before an item is left with its error showing


def queue_read(jid):
    try:
        q = json.loads((jobs() / jid / QUEUE).read_text())
        return q if isinstance(q, dict) and isinstance(q.get("items"), list) else {"items": []}
    except Exception:
        return {"items": []}


def queue_write(jid, q, keep=False):
    """Save a queue; an empty one is deleted unless keep (to remember when it last sent)."""
    f = jobs() / jid / QUEUE
    if not q.get("items") and not keep:
        f.unlink(missing_ok=True)
        return
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(q, indent=2))
    tmp.replace(f)


def queue_add(jid, text):
    """Append a prompt; returns how many are now waiting."""
    q = queue_read(jid)
    q["items"].append({"id": uuid.uuid4().hex[:8], "text": text, "added": now_utc().isoformat()})
    queue_write(jid, q)
    return len(q["items"])


def queue_remove(jid, item_id=None):
    """Drop one item, or all of them when item_id is None. True if anything went."""
    q = queue_read(jid)
    keep = [] if item_id is None else [i for i in q["items"] if i["id"] != item_id]
    if len(keep) == len(q["items"]):
        return False
    q["items"] = keep
    queue_write(jid, q)
    return True


def limit_until(acc_id, cfg=None, now=None):
    """The reset time (ISO) if the account is at its 5-hour or 7-day limit, else None;
    "" when it is at a limit with no known reset. A reset already past counts as free:
    the cached usage can lag the reset by a few minutes."""
    acc = A.get(acc_id, cfg) if acc_id else None
    if not acc:
        return None
    u = U.get(acc)
    now = now or now_utc()
    hit = []
    for pct, reset in ((u["five"], u["fiveReset"]), (u["seven"], u["sevenReset"])):
        if pct is None or pct < 100:
            continue
        t = C.parse_t(reset) if reset else None
        if t and t <= now:
            continue
        hit.append(t)
    if not hit:
        return None
    known = [t for t in hit if t]
    return max(known).isoformat() if len(known) == len(hit) else ""


def busy(card):
    return card.get("col") == "running" or card.get("state") == "working"


def _queue_view(jid, acc_id, cfg):
    q = queue_read(jid)
    if not q["items"]:
        return None
    return {"items": [{"id": i["id"], "text": i["text"], "error": i.get("error") or ""} for i in q["items"]],
            "until": limit_until(acc_id, cfg) or ""}


def queue_due(cards=None, cfg=None, now=None):
    """Take what can go now off the queues. Returns ("send", card, item) for prompts and
    ("start", todo, None) for queued todos. The caller sends them outside its lock and
    hands failures back to queue_failed."""
    cfg = cfg or A.load()
    now = now or now_utc()
    cards = cards if cards is not None else load_cards(True)
    by_id = {c["id"]: c for c in cards}
    out = []
    for f in jobs().glob(f"*/{QUEUE}"):
        jid = f.parent.name
        card = by_id.get(jid)
        if not card:
            continue                    # archived or finished elsewhere; its folder moved with it
        q = queue_read(jid)
        if not q["items"] or busy(card) or limit_until(card.get("account"), cfg, now) is not None:
            continue
        last = C.parse_t(q.get("sentAt") or "")
        if last and (now - last).total_seconds() < QUEUE_GAP:
            continue
        item = q["items"][0]
        if item.get("tries", 0) >= QUEUE_TRIES:
            continue                    # stuck; the card shows the error until removed
        q["items"] = q["items"][1:]
        q["sentAt"] = now.isoformat()
        queue_write(jid, q, keep=True)  # keeps sentAt so the next prompt still waits its gap
        out.append(("send", card, item))
    todos = load_todos()
    ready = [t for t in todos if t.get("queued") and limit_until(todo_account(t, cfg)["id"], cfg, now) is None]
    if ready:
        save_todos([t for t in todos if t not in ready])
        out += [("start", t, None) for t in ready]
    return out


def queue_failed(kind, x, item, err):
    """Put a failed prompt back at the front with its error, or a failed todo back in Todo."""
    if kind == "start":
        save_todos(load_todos() + [{**x, "queued": False}])
        return
    q = queue_read(x["id"])
    q["items"].insert(0, {**item, "tries": item.get("tries", 0) + 1, "error": err})
    queue_write(x["id"], q)


def queue_pending():
    """True while anything waits: a queued prompt or a queued todo."""
    return any(queue_read(f.parent.name)["items"] for f in jobs().glob(f"*/{QUEUE}")) \
        or any(t.get("queued") for t in load_todos())


def act_archive(card):
    if card["kind"] == "todo":
        save_todos([t for t in load_todos() if t["id"] != card["id"]]); return "todo deleted"
    err = settle(card)
    if err:
        return err
    dest = paths.claude_home() / "jobs-archive" / datetime.now().strftime("%Y-%m-%d")
    dest.mkdir(parents=True, exist_ok=True)
    shutil.move(str(jobs() / card["id"]), str(dest / card["id"]))
    try:
        pins_write([p for p in pins_read() if p != card["id"]])
    except Exception:
        pass
    try:
        rec = BR.record(card["id"]) or {}
        if rec.get("ticket"):
            from . import vault as V
            V.drop_ticket(rec["ticket"])   # its keys hook stops working
        from . import mobile as M
        M.release(card["id"])              # its phone is free for the next agent
    except Exception:
        pass
    try:
        BR.stop(card["id"])   # its shared browser and profile go with it
    except Exception:
        pass
    return f"archived to {str(dest).replace(str(HOME), '~')}"


def act_pin(card):
    if card["kind"] == "todo":
        return "todos aren't pinnable"
    pins = pins_read()
    if card["id"] in pins:
        pins.remove(card["id"]); msg = "unpinned"
    else:
        pins.append(card["id"]); msg = "pinned"
    pins_write(pins)
    return msg


def act_organize():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        C.organize(dry=False)
    n = len([l for l in buf.getvalue().splitlines() if l.strip() and "already" not in l])
    return f"organized ({n} changes)"


def spawn_bg(cwd, acc, args, prompt, name):
    """`claude --bg <args> --name <name> <prompt>` in cwd on an account. (jid, None) or (None, error).
    Flags that take several values (--mcp-config, --allowedTools...) must be followed by another
    flag in args: --name ends their list."""
    cmd = [paths.claude_bin(), "--bg", *args, "--name", name or "agent", prompt]
    r = subprocess.run(cmd, cwd=cwd, env=account_env(acc), capture_output=True, text=True, timeout=90)
    m = re.search(r"backgrounded\s*·\s*([0-9a-f]{8})", r.stdout + r.stderr)
    if not m:
        lines = (r.stdout + r.stderr).strip().splitlines()
        return None, lines[-1] if lines else "launch failed"
    jid = m.group(1)
    for _ in range(20):
        if (jobs() / jid / "state.json").exists():
            try:
                write_state(jid, lambda st: {**st, "color": acc["color"]})
            except Exception:
                pass
            break
        time.sleep(0.1)
    return jid, None


MOBILE_MARKERS = ("pubspec.yaml", "android", "app.json", "build.gradle", "build.gradle.kts", "settings.gradle",
                  "settings.gradle.kts", "AndroidManifest.xml", "capacitor.config.json", "capacitor.config.ts")


def wants_phone(cwd, todo=None):
    """Give a new agent an Android phone? When the task asks for one, or the folder is a mobile app."""
    from . import mobile as M
    if (todo or {}).get("phone") is False or os.environ.get("LOFT_PHONE") == "0" or not M.available():
        return False
    if (todo or {}).get("phone"):
        return True
    return any((Path(cwd) / m).exists() for m in MOBILE_MARKERS)


def agent_extras(todo, cwd, acc):
    """What Loft adds to an agent's launch: its browser, phone, the login bank and keys. (record or None, claude args).

    One agent record (loft/browser.py, <cache>/browsers/<jid>.json) carries the token its Loft tools
    use; the browser itself is only started for web folders and while the login bank (loft/sites.py)
    has logins: then every agent, on any account, starts signed in to them, unless its task turned
    the bank off (todo["bank"] = False)."""
    from . import vault as V
    try:
        project = todo.get("project") or P.project_of("", cwd, P.load())
    except Exception:
        project = todo.get("project")
    try:
        key_names = V.key_names_for(acc["id"], project)
    except Exception:
        key_names = []
    phone = wants_phone(cwd, todo)
    try:
        br = BR.prepare(cwd, acc["id"], tools=bool(phone or key_names), bank=todo.get("bank") is not False)
    except Exception:
        br = None
    if not br:
        return None, []
    br["phone"] = phone
    br["project"] = project
    notes = [BR.agent_note(br)] if br.get("port") else []
    if phone:
        notes.append(PHONE_NOTE)
    if key_names:
        notes.append(V.agent_note(key_names))
    extra = ["--mcp-config", BR.mcp_config(br)]
    if br.get("port"):
        extra += ["--disallowedTools", BR.DENY_TOOLS]
    if key_names:
        br["ticket"] = V.make_ticket(acc["id"], project)
        extra += ["--settings", V.hook_settings(br["ticket"], [sys.executable, str(paths.REPO / "bin" / "loft")])]
    if notes:
        extra += ["--append-system-prompt", " ".join(n for n in notes if n)]
    return br, extra


PHONE_NOTE = ("Loft shares an Android phone with you (an emulator, or a real phone over adb): the loft_phone_* tools "
              "show its screen with the tappable elements, and tap, swipe, type, press keys, open apps and deep "
              "links, install APKs and read logs. The user watches it live in Loft › Devices and may take over. "
              "loft_phone_screen also tells you the device's serial: build and install with the usual tools on it "
              "(`flutter run -d <serial>`, `npx expo run:android --device <serial>`, `./gradlew installDebug` with "
              "ANDROID_SERIAL=<serial>, `adb -s <serial> ...`). Don't start or stop emulators yourself; ask Loft "
              "with loft_phone_use. After a change, look at the screen to check it.")


def launch_todo(todo):
    cwd = os.path.expanduser(todo.get("cwd") or "~")
    if os.path.realpath(cwd) == os.path.realpath(str(HOME)):
        return None, "pick a project folder: Claude won't start background agents in your home folder"
    if not os.path.isdir(cwd):
        return None, f"folder missing: {cwd}"
    cfg = A.load()
    acc = todo_account(todo, cfg)
    br, extra = agent_extras(todo, cwd, acc)
    files = todo.get("files") or []
    extra = extra + AT.add_dir_args(files)          # --add-dir: --name follows it in spawn_bg
    jid, err = spawn_bg(cwd, acc, extra, AT.prompt_with(todo["prompt"], files), todo.get("title") or "agent")
    if not jid:
        BR.discard(br)
        return None, err
    if br:
        BR.adopt(jid, br)
    if todo.get("project"):   # keep the project the task was written for, even outside its folders
        try:
            with P.locked() as pcfg:
                if P.get(todo["project"], pcfg) and P.project_of(jid, cwd, {**pcfg, "assignments": {}}) != todo["project"]:
                    P.assign(jid, todo["project"], pcfg)
        except Exception:
            pass
    return jid, f"started {jid} on {acc['label']}"


def create_task(prompt, cwd="", account=None, project=None, title="", start=False, bank=None, phone=None, files=None):
    """A new task from the app, the phone or `loft new`: saved to Todo, or started now.
    Returns (status, message, jid or None, todo). A title typed here is kept as is; without
    one the card gets a title from the prompt until the generated one lands (loft/titles.py)."""
    from . import titles as TI
    proj = None
    if project:
        proj = P.get(project)
        if not proj:
            return 400, "That project is gone. Pick another one.", None, None
    prompt = (prompt or "").strip()
    cwd = (cwd or "").strip() or (proj["folders"][0] if proj and proj["folders"] else "")
    typed = (title or "").strip()
    title = typed or TI.derive_title(prompt)
    if not title:
        return 400, "Write what the agent should do.", None, None
    if not cwd or not os.path.isdir(os.path.expanduser(cwd)):
        return 400, "Pick a project folder that exists.", None, None
    if os.path.realpath(os.path.expanduser(cwd)) == os.path.realpath(str(HOME)):
        return 400, "Pick a project folder: Claude won't start background agents in your home folder.", None, None
    acc = P.pick_account(account or None, proj, A.load(), U.get)
    todo = {"id": "t" + uuid.uuid4().hex[:8], "title": title, "titleSource": "user" if typed else "auto",
            "account": acc["id"], "cwd": cwd.replace(str(HOME), "~"), "prompt": prompt,
            "created": now_utc().isoformat()}
    if proj:
        todo["project"] = proj["id"]
    if bank is False:   # this one starts without the login bank's sign-ins
        todo["bank"] = False
    if phone is not None:
        todo["phone"] = bool(phone)
    files = AT.clean(files)
    if files:           # pasted / dropped / picked files, listed under the prompt when it starts
        todo["files"] = files
    if start:
        if not prompt:
            return 400, "Write a prompt to start it with.", None, todo
        jid, msg = launch_todo(todo)
        return (200, msg, jid, todo) if jid else (500, msg, None, todo)
    todos = load_todos(); todos.append(todo); save_todos(todos)
    return 200, "Added to Todo", None, todo


# ---------------------------------------------------------------- bulk actions
# POST /api/bulk, `loft archive --done`, `loft stop --all`. The Loft Assistant's own session
# is never touched; todos only when named by id (archiving one deletes it).

BULK_FILTERS = ("done", "idle", "all")


def bulk_targets(cards, action, filt="done", ids=None):
    """The cards a bulk action applies to. `filt` is ignored when ids are given."""
    cards = [c for c in cards if not c.get("assistant")]
    if ids:
        want = set(ids)
        return [c for c in cards if c["id"] in want and (action == "archive" or c["kind"] == "agent")]
    agents = [c for c in cards if c["kind"] == "agent"]
    if filt == "done":
        picked = [c for c in agents if c["col"] == "done"]
    elif filt == "idle":
        picked = [c for c in agents if not busy(c)]
    elif filt == "all":
        picked = agents
    else:
        raise ValueError(f"filter is one of {', '.join(BULK_FILTERS)}")
    if action == "stop":    # only what has a worker to stop
        picked = [c for c in picked if c.get("live") or busy(c)]
    return picked


def stop_agent(card, wait=5.0):
    """`claude stop` an agent and wait for its worker to go. None, or an error string."""
    r = claude("stop", card["id"], card=card)
    waited = 0.0
    while card["id"] in live_shorts() and waited < wait:
        time.sleep(0.2); waited += 0.2
    if card["id"] in live_shorts():
        out = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
        return out[-1] if out and r.returncode else "its worker is still running"
    card["live"] = False
    return None


def bulk(action, filt="done", ids=None, cards=None):
    """Archive or stop many agents. Returns {"message", "count", "ids", "errors"}."""
    import concurrent.futures as cf
    if action not in ("archive", "stop"):
        raise ValueError("action is archive or stop")
    cards = cards if cards is not None else load_cards(True)
    targets = bulk_targets(cards, action, filt, ids)

    def one(c):
        if c["kind"] == "todo":
            act_archive(c); return None
        if action == "stop" or busy(c) or c.get("live"):
            err = stop_agent(c)
            if err:
                return err
        if action == "archive":
            c = {**c, "col": "done", "state": "done", "live": False}   # stopped above: nothing left running
            msg = act_archive(c)
            if not msg.startswith(("archived", "todo deleted")):
                return msg
        return None

    done, errors = [], []
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        for c, err in zip(targets, pool.map(one, targets)):
            if err:
                errors.append({"id": c["id"], "name": c["name"], "error": err})
            else:
                done.append(c["id"])
    verb = {"archive": "Archived", "stop": "Stopped"}[action]
    n = len(done)
    if not targets:
        what = {"done": "done", "idle": "idle", "all": ""}.get(filt, "") if not ids else ""
        msg = f"Nothing {what + ' ' if what else ''}to {action}".replace("  ", " ")
    else:
        msg = f"{verb} {n} agent{'s' if n != 1 else ''}"
        if errors:
            msg += f"; {len(errors)} couldn't: " + "; ".join(f"{e['name']} ({e['error']})" for e in errors[:3])
    return {"message": msg, "count": n, "ids": done, "errors": errors}


def terminal_command(card):
    """argv that opens `claude attach <id>` in a new terminal window with the card's login.
    LOFT_TERMINAL (or CAGENTS_TERMINAL) picks the terminal on Linux, e.g. "konsole"."""
    env = account_env(card)
    inner = (f"cd {shlex.quote(str(HOME))} && env -u CLAUDE_CONFIG_DIR {A.VAR}={shlex.quote(env[A.VAR])} "
             f"{shlex.quote(paths.claude_bin())} attach {card['id']}")
    if paths.is_mac():
        script = 'tell application "Terminal" to do script ' + json.dumps(inner) + "\n" \
                 'tell application "Terminal" to activate'
        return ["osascript", "-e", script], env
    term = os.environ.get("LOFT_TERMINAL") or os.environ.get("CAGENTS_TERMINAL")
    if not term:
        term = next((t for t in ("konsole", "gnome-terminal", "kitty", "alacritty", "wezterm", "xfce4-terminal", "x-terminal-emulator", "xterm")
                     if shutil.which(t)), "xterm")
    argv = [paths.claude_bin(), "attach", card["id"]]
    if term == "konsole":
        return ["konsole", "-p", f"tabtitle={account_label(card)} · {card['name']}", "-e", *argv], env
    if term == "gnome-terminal":
        return ["gnome-terminal", "--", *argv], env
    if term == "wezterm":
        return ["wezterm", "start", "--", *argv], env
    return [term, "-e", *argv], env


# ---------------------------------------------------------------- terminal UI

HELP = """
  arrows / hjkl   move between cards and columns
  enter           open agent (claude attach) / start a TODO card
  n               new TODO card (account, folder, title, prompt)
  e               rename card
  a               move agent/card to the next account (a working agent moves after its turn)
  s               stop a running agent
  x               archive agent (to ~/.claude/jobs-archive) / delete TODO card
  p               pin / unpin
  0 1-9           show all / one account
  tab             show all DONE (default: last 3 days)
  /               filter by text (empty = clear)
  o               organize (colors, pins, order) like `loft organize`
  r               refresh now
  q               quit
"""
CURSES_COLORS = {"blue": "BLUE", "green": "GREEN", "purple": "MAGENTA", "cyan": "CYAN", "pink": "MAGENTA",
                 "orange": "YELLOW", "yellow": "YELLOW", "red": "RED"}


class Board:
    def __init__(self, scr):
        self.scr = scr
        self.view = None  # None = all, else an account id
        self.show_all_done = False
        self.filter = ""
        self.col = 0
        self.sel = {k: 0 for k, _ in COLS}
        self.top = {k: 0 for k, _ in COLS}
        self.msg = ""
        self.msg_at = 0
        self.cards = []
        curses.curs_set(0)
        curses.use_default_colors()
        names = ["BLUE", "GREEN", "RED", "YELLOW", "CYAN", "MAGENTA"]
        for i, n in enumerate(names, start=1):
            curses.init_pair(i, getattr(curses, "COLOR_" + n), -1)
        self.pair = {n: curses.color_pair(i) for i, n in enumerate(names, start=1)}
        self.RED, self.CYAN = self.pair["RED"], self.pair["CYAN"]
        scr.timeout(REFRESH_MS)
        scr.keypad(True)

    def acc_attr(self, acc_id):
        acc = A.get(acc_id, self.cfg) if acc_id else None
        return self.pair.get(CURSES_COLORS.get(acc["color"] if acc else "", ""), curses.A_DIM)

    def refresh_data(self):
        self.cfg = A.load()
        cards = load_cards(self.show_all_done)
        if self.view:
            cards = [c for c in cards if c["account"] == self.view]
        if self.filter:
            q = self.filter.lower()
            cards = [c for c in cards if q in (c["name"] + " " + c["detail"] + " " + c["folderName"]).lower()]
        self.cards = cards
        self.bycol = {k: sort_cards([c for c in cards if c["col"] == k]) for k, _ in COLS}
        for k, _ in COLS:
            n = len(self.bycol[k])
            self.sel[k] = max(0, min(self.sel[k], n - 1))

    def current(self):
        k = COLS[self.col][0]
        lst = self.bycol.get(k) or []
        return lst[self.sel[k]] if lst else None

    def say(self, m):
        self.msg, self.msg_at = m, time.time()

    def put(self, y, x, s, attr=0, w=None):
        h, W = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= W:
            return
        s = s if w is None else s[:max(0, w)]
        try:
            self.scr.addstr(y, x, s[:max(0, W - x - (1 if y == h - 1 else 0))], attr)
        except curses.error:
            pass

    def draw(self):
        scr = self.scr
        scr.erase()
        h, W = scr.getmaxyx()
        self.put(0, 0, " loft ", curses.A_REVERSE | curses.A_BOLD)
        x = 7
        pct = lambda v: v if v == "—" else v + "%"
        for acc in self.cfg["accounts"]:
            f, s = usage(acc["id"])
            seg = f" {acc['label']}  5h {pct(f)}  7d {pct(s)} "
            if x + len(seg) > W - 30:
                break
            self.put(0, x, seg, self.acc_attr(acc["id"]) | curses.A_BOLD); x += len(seg) + 1
        tag = f"view:{self.view or 'all'}" + (f"  filter:{self.filter}" if self.filter else "") + ("  done:all" if self.show_all_done else "")
        if x + len(tag) < W - 10:
            self.put(0, W - len(tag) - 10, tag, curses.A_DIM)
        self.put(0, W - 9, time.strftime("%H:%M:%S"), curses.A_DIM)

        prev_h = 7
        body_top, body_bot = 2, h - prev_h - 1
        wide = W >= 96
        visible = list(range(len(COLS))) if wide else [self.col]
        colw = (W - (len(visible) - 1)) // len(visible)
        for vi, ci in enumerate(visible):
            k, title = COLS[ci]
            x0 = vi * (colw + 1)
            lst = self.bycol.get(k) or []
            head = f" {title} {len(lst)} "
            attr = curses.A_BOLD | (curses.A_REVERSE if ci == self.col else 0)
            if k == "needs" and lst:
                attr |= self.RED
            self.put(1, x0, head.ljust(colw), attr)
            if not wide:
                self.put(1, x0 + len(head) + 2, "←→ " + "  ".join(t for _, t in COLS), curses.A_DIM)
            card_h = 4
            rows = max(1, (body_bot - body_top) // card_h)
            sel = self.sel[k]
            if sel < self.top[k]:
                self.top[k] = sel
            if sel >= self.top[k] + rows:
                self.top[k] = sel - rows + 1
            for i, card in enumerate(lst[self.top[k]: self.top[k] + rows]):
                idx = self.top[k] + i
                y = body_top + i * card_h
                selected = ci == self.col and idx == sel
                acc = self.acc_attr(card["account"])
                base = curses.A_REVERSE if selected else 0
                for dy in range(3):
                    self.put(y + dy, x0, "▌", acc | curses.A_BOLD)
                mark = "★" if card["pinned"] else ("◆" if card["kind"] == "todo" else ("●" if card["live"] and card["col"] != "done" else " "))
                if card["failed"]:
                    mark = "✗"
                self.put(y, x0 + 1, f"{mark} {card['name']}".ljust(colw - 1), base | curses.A_BOLD, colw - 1)
                ago = C.ago(card["updated"]) if card["updated"] else ""
                meta = f"{account_label(card)[:10]} · {card['folderName'][:colw - 20]}"
                self.put(y + 1, x0 + 2, meta.ljust(colw - 2 - len(ago) - 1), acc | (base and curses.A_BOLD), colw - 2)
                self.put(y + 1, x0 + colw - len(ago) - 1, ago, curses.A_DIM)
                self.put(y + 2, x0 + 2, card["detail"], curses.A_DIM | (self.RED if card["failed"] else 0), colw - 3)
            hidden = len(lst) - (self.top[k] + rows)
            if hidden > 0:
                self.put(body_bot, x0 + 2, f"↓ {hidden} more", curses.A_DIM)
            if self.top[k] > 0:
                self.put(body_top - 1, x0 + colw - 9, f"↑ {self.top[k]} more", curses.A_DIM)

        y0 = h - prev_h - 1
        self.put(y0, 0, "─" * W, curses.A_DIM)
        c = self.current()
        if c:
            acc = self.acc_attr(c["account"])
            who = account_label(c)
            state = "todo card" if c["kind"] == "todo" else (c["state"] + (" · live" if c["live"] else ""))
            self.put(y0 + 1, 1, c["name"], curses.A_BOLD)
            self.put(y0 + 1, min(W - 1, len(c["name"]) + 3), who, acc | curses.A_BOLD)
            self.put(y0 + 1, min(W - 1, len(c["name"]) + len(who) + 6), f"{c['id']} · {state} · {c['cwd'].replace(str(HOME), '~')}", curses.A_DIM)
            text = c["result"] if c["col"] == "done" and c["result"] else c["detail"]
            for i, ln in enumerate(textwrap.wrap(text, max(20, W - 4))[:prev_h - 3]):
                self.put(y0 + 2 + i, 2, ln)
            if c["links"]:
                self.put(y0 + prev_h - 1, 2, "  ".join(c["links"])[:W - 4], self.CYAN)
        foot = " ↵ open/start  n new  e rename  a account  s stop  x archive  p pin  0-9 view  / filter  tab done  ? help  q quit "
        if self.msg and time.time() - self.msg_at < 6:
            foot = " " + self.msg + " "
        self.put(h - 1, 0, foot.ljust(W), curses.A_REVERSE)
        scr.refresh()

    def ask(self, prompt, default=""):
        h, W = self.scr.getmaxyx()
        curses.curs_set(1)
        self.scr.timeout(-1)
        buf = list(default)
        while True:
            self.put(h - 1, 0, (" " + prompt + " " + "".join(buf)).ljust(W), curses.A_REVERSE)
            self.scr.move(h - 1, min(W - 2, len(prompt) + 2 + len(buf)))
            ch = self.scr.get_wch()
            if ch in ("\n", "\r", curses.KEY_ENTER):
                break
            if ch == "\x1b":
                buf = None; break
            if ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                buf = buf[:-1]
            elif isinstance(ch, str) and ch.isprintable():
                buf.append(ch)
        curses.curs_set(0)
        self.scr.timeout(REFRESH_MS)
        return None if buf is None else "".join(buf).strip()

    def confirm(self, q):
        return (self.ask(q + " [y/N]") or "").lower().startswith("y")

    def pick(self, title, options, typed="or type a path"):
        h, W = self.scr.getmaxyx()
        y = 3
        self.put(y - 1, 2, f" {title} ".ljust(W - 4), curses.A_REVERSE | curses.A_BOLD)
        for i, o in enumerate(options, start=1):
            self.put(y + i - 1, 2, f" {i}  {o.replace(str(HOME), '~')}".ljust(W - 4), curses.A_REVERSE)
        if typed:
            self.put(y + len(options), 2, f" {typed} ".ljust(W - 4), curses.A_REVERSE | curses.A_DIM)
        a = self.ask("number:" if not typed else "number or path:")
        if a is None:
            return None
        if a.isdigit() and 1 <= int(a) <= len(options):
            return options[int(a) - 1]
        return os.path.expanduser(a) if a and typed else None

    def shell(self, cmd, env=None, cwd=None):
        curses.def_prog_mode()
        curses.endwin()
        try:
            subprocess.call(cmd, env=env, cwd=cwd)
        finally:
            curses.reset_prog_mode()
            self.scr.refresh()

    def new_todo(self):
        accs = self.cfg["accounts"]
        acc = A.get(self.view, self.cfg) if self.view else None
        if not acc:
            self.draw()
            label = self.pick("account", [f"{a['label']}  {a.get('email') or ''}" for a in accs], typed=None)
            if not label:
                return self.say("cancelled")
            acc = accs[[f"{a['label']}  {a.get('email') or ''}" for a in accs].index(label)]
        self.draw()
        cwd = self.pick("folder", recent_dirs())
        if not cwd:
            return self.say("cancelled")
        title = self.ask("title:")
        if not title:
            return self.say("cancelled")
        prompt = self.ask("prompt (empty = just a todo for now):") or ""
        todo = {"id": "t" + uuid.uuid4().hex[:7], "title": title, "account": acc["id"],
                "cwd": cwd.replace(str(HOME), "~"), "prompt": prompt, "created": now_utc().isoformat()}
        todos = load_todos(); todos.append(todo); save_todos(todos)
        self.say(f"added to TODO ({acc['label']}). enter on it to start")

    def enter(self):
        c = self.current()
        if not c:
            return
        if c["kind"] == "todo":
            todo = next((t for t in load_todos() if t["id"] == c["id"]), None)
            if not todo:
                return
            if not todo.get("prompt"):
                p = self.ask("prompt to start it with:")
                if not p:
                    return self.say("cancelled")
                todo["prompt"] = p
            self.say("starting…"); self.draw()
            jid, msg = launch_todo(todo)
            if jid:
                save_todos([t for t in load_todos() if t["id"] != todo["id"]])
            self.say(msg)
            return
        self.shell([paths.claude_bin(), "attach", c["id"]], env=account_env(c), cwd=str(HOME))
        self.say(f"back from {c['name']}")

    def handle(self, ch):
        c = self.current()
        k = COLS[self.col][0]
        n = len(self.bycol.get(k) or [])
        if ch in (curses.KEY_RIGHT, ord("l")):
            self.col = (self.col + 1) % len(COLS)
        elif ch in (curses.KEY_LEFT, ord("h")):
            self.col = (self.col - 1) % len(COLS)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.sel[k] = min(n - 1, self.sel[k] + 1) if n else 0
        elif ch in (curses.KEY_UP, ord("k")):
            self.sel[k] = max(0, self.sel[k] - 1)
        elif ch in (curses.KEY_HOME, ord("g")):
            self.sel[k] = 0
        elif ch in (curses.KEY_END, ord("G")):
            self.sel[k] = max(0, n - 1)
        elif ch in (10, 13, curses.KEY_ENTER):
            self.enter()
        elif ch == ord("n"):
            self.new_todo()
        elif ch == ord("e") and c:
            name = self.ask("new name:", c["name"])
            if name:
                self.say(act_rename(c, name))
        elif ch == ord("a") and c:
            self.say(act_switch_account(c))
        elif ch == ord("s") and c and c["kind"] == "agent":
            if self.confirm(f"stop '{c['name']}'?"):
                r = claude("stop", c["id"], card=c)
                out = (r.stdout + r.stderr).strip().splitlines()
                self.say(out[-1] if out else "stopped")
        elif ch == ord("x") and c:
            if self.confirm(f"{'delete todo' if c['kind'] == 'todo' else 'archive'} '{c['name']}'?"):
                self.say(act_archive(c))
        elif ch == ord("p") and c:
            try:
                self.say(act_pin(c))
            except Exception as e:
                self.say(str(e))
        elif ord("0") <= ch <= ord("9"):
            i = ch - ord("0")
            accs = self.cfg["accounts"]
            self.view = None if i == 0 or i > len(accs) else accs[i - 1]["id"]
        elif ch == 9:
            self.show_all_done = not self.show_all_done
        elif ch == ord("/"):
            f = self.ask("filter:", self.filter)
            if f is not None:
                self.filter = f
        elif ch == ord("o"):
            try:
                self.say(act_organize())
            except SystemExit as e:
                self.say(str(e))
        elif ch == ord("?"):
            self.help()
        elif ch == ord("r"):
            self.say("refreshed")

    def help(self):
        h, W = self.scr.getmaxyx()
        lines = HELP.strip("\n").splitlines()
        y0 = max(1, (h - len(lines) - 2) // 2)
        for i, ln in enumerate([" keys"] + lines + ["", " any key to close"]):
            self.put(y0 + i, 4, ln.ljust(min(W - 8, 70)), curses.A_REVERSE)
        self.scr.timeout(-1); self.scr.getch(); self.scr.timeout(REFRESH_MS)

    def loop(self):
        while True:
            try:
                self.refresh_data()
            except Exception as e:
                self.say(f"load error: {e}")
            self.draw()
            ch = self.scr.getch()
            if ch == -1:
                continue
            if ch in (ord("q"), 27):
                return
            try:
                self.handle(ch)
            except Exception as e:  # never crash the board on one bad action
                self.say(f"error: {e}")


def run():
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(lambda scr: Board(scr).loop())
