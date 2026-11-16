"""Reading Claude Code's background-agent state, shared by the CLI, the TUI and the app.

Every agent is ~/.claude/jobs/<id>/state.json. Its login is
providerEnv.CLAUDE_SECURESTORAGE_CONFIG_DIR (see accounts.py), which the daemon
reapplies on respawn.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import accounts as A
from . import paths

HOME = paths.HOME
DONE_DAYS = 3


def jobs_dir():
    return paths.claude_home() / "jobs"


def auto_pins_file():
    return jobs_dir() / ".cagents-autopins.json"


# ------------------------------------------------------------------ Loft's own per-agent notes
# <job>/loft-meta.json: the title and summary Loft wrote for an agent ({"title", "summary",
# "titleSource": "auto"|"user", "assistant": bool}). A sidecar, not state.json: a live
# worker rewrites state.json, and the file moves with the job dir when it is archived.

META = "loft-meta.json"


def meta_read(jid):
    try:
        m = json.loads((jobs_dir() / jid / META).read_text())
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def meta_write(jid, **fields):
    """Merge fields into an agent's loft-meta.json (None removes a field). Returns the result."""
    d = jobs_dir() / jid
    if not d.is_dir():
        raise FileNotFoundError(f"no agent {jid}")
    m = meta_read(jid)
    for k, v in fields.items():
        if v is None:
            m.pop(k, None)
        else:
            m[k] = v
    tmp = d / (META + ".tmp")
    tmp.write_text(json.dumps(m, indent=2))
    tmp.replace(d / META)
    return m


# ------------------------------------------------------------------ processes

def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False


def _cmdlines():
    """argv lists of running processes. /proc on Linux, `ps` elsewhere (macOS)."""
    if Path("/proc/self/cmdline").exists():
        for f in glob.glob("/proc/[0-9]*/cmdline"):
            try:
                yield [a.decode(errors="ignore") for a in Path(f).read_bytes().split(b"\0")]
            except OSError:
                continue
        return
    try:
        out = subprocess.run(["ps", "-axww", "-o", "command="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return
    for line in out.splitlines():
        yield line.split()


def live_sessions():
    """Agent ids (8-char) with a live process: daemon workers + interactive sessions."""
    ids = set()
    try:
        r = json.loads((paths.claude_home() / "daemon" / "roster.json").read_text())
        for short, w in (r.get("workers") or {}).items():
            if w.get("pid") and pid_alive(w["pid"]):
                ids.add(short)
    except Exception:
        pass
    for args in _cmdlines():
        for i, a in enumerate(args):
            if a in ("--session-id", "--resume") and i + 1 < len(args):
                ids.add(args[i + 1][:8])
    return ids


def daemon_env():
    """(pid, {var: value}) of the claude daemon's login vars, or (pid, None) when the
    env can't be read (macOS), or (None, None) when no daemon runs."""
    try:
        pid = json.loads((paths.claude_home() / "daemon" / "roster.json").read_text()).get("supervisorPid")
    except Exception:
        return None, None
    if not pid or not pid_alive(pid):
        return None, None
    try:
        env = dict(kv.split(b"=", 1) for kv in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0") if b"=" in kv)
    except OSError:
        return pid, None
    return pid, {k.decode(): v.decode(errors="ignore") for k, v in env.items()
                 if k in (b"CLAUDE_SECURESTORAGE_CONFIG_DIR", b"CLAUDE_CONFIG_DIR")}


# ------------------------------------------------------------------ state helpers

def parse_t(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def ago(t):
    if not t:
        return "?"
    s = (datetime.now(timezone.utc) - t).total_seconds()
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{int(s // n)}{unit}"
    return "now"


def project(st):
    wt = st.get("worktreePath") or ""
    if "/.claude/worktrees/" in wt:
        repo, br = wt.split("/.claude/worktrees/", 1)
        return f"{Path(repo).name}:{br}"
    cwd = st.get("cwd") or ""
    return "~" if cwd.rstrip("/") == str(HOME) else Path(cwd).name or "?"


def bucket(st):
    s, tempo = st.get("state"), st.get("tempo")
    if s in ("blocked",) or tempo == "blocked" or s == "needs_input":
        return "needs"
    if s == "failed":
        return "failed"
    if s == "working":
        return "running"
    return "done"


def transcript_of(st):
    p = st.get("linkScanPath")
    if p and Path(p).is_file():
        return Path(p)
    sid = st.get("sessionId")
    hits = glob.glob(str(paths.claude_home() / "projects" / "*" / f"{sid}.jsonl")) if sid else []
    return Path(hits[0]) if hits else None


def job_account(st, cfg):
    """(account or None, saved) for a state.json. saved=False: no login stored."""
    env = st.get("providerEnv") or {}
    return A.account_of_env(env, cfg), A.has_saved_login(env)


# ------------------------------------------------------------------ organize

def organize(dry, transcript=False, out=print):
    """Color every agent by its account, give agents with no saved login an explicit
    one (a rule match, else the default account), pin what needs you or is running,
    and order agents by account then name."""
    cfg = A.load()
    jd = jobs_dir()
    pins_f = jd / "pins.json"
    if (jd / "pins.json.lock").exists():
        raise SystemExit("pins.json is locked by Claude right now, try again in a second")
    try: pins = list(json.loads(pins_f.read_text()))
    except Exception: pins = []
    try: auto = set(json.loads(auto_pins_file().read_text()))
    except Exception: auto = set()
    live = live_sessions()
    changes = []
    order = {a["id"]: i for i, a in enumerate(cfg["accounts"])}
    rows = []
    for sf in sorted(jd.glob("*/state.json")):
        jid = sf.parent.name
        try: st = json.loads(sf.read_text())
        except Exception: continue
        env = st.get("providerEnv") or {}
        name = st.get("name") or jid
        new = dict(st)
        acc, saved = job_account(st, cfg)
        if not saved and jid not in live:
            hay = " ".join([name, (st.get("intent") or "")[:300], project(st)])
            acc = A.rule_account(hay, cfg) or A.default_account(cfg)
            if acc:
                new["providerEnv"] = A.apply_env(dict(env), acc)
                changes.append(f"  {jid}  login -> {acc['label']:<14} {name}")
        if acc and st.get("color") != acc["color"]:
            new["color"] = acc["color"]
            changes.append(f"  {jid}  color {st.get('color') or '-'} -> {acc['color']:<6} [{acc['label']}]  {name}")
            tp = transcript_of(st)
            if tp and transcript and not dry:
                with open(tp, "a") as fh:
                    fh.write(json.dumps({"type": "agent-color", "agentColor": acc["color"], "sessionId": st.get("sessionId")}) + "\n")
        b = bucket(st)
        want_pin = b in ("needs", "running")
        if want_pin and jid not in pins:
            pins.append(jid); auto.add(jid); changes.append(f"  {jid}  pin   ({b})                  {name}")
        elif not want_pin and jid in auto and jid in pins:
            pins.remove(jid); auto.discard(jid); changes.append(f"  {jid}  unpin ({b})                  {name}")
        if new != st and not dry:
            sf.write_text(json.dumps(new, indent=2))
        rows.append((order.get(acc["id"], 99) if acc else 99, name.lower(), sf.parent))
    # fixed order inside each state section: by account, then name.
    # `stateOrder` / `order` sidecars replace the timestamps the agents view sorts by.
    for i, (_, _, d) in enumerate(sorted(rows), start=1):
        for side in ("stateOrder", "order"):
            f = d / side
            if side == "order" and d.name not in pins:
                continue
            if not f.exists() or f.read_text().strip() != str(i):
                if not dry: f.write_text(str(i))
    pins = [p for p in pins if (jd / p).is_dir()]
    if not dry:
        pins_f.write_text(json.dumps(pins, indent=2))
        auto_pins_file().write_text(json.dumps(sorted(auto & set(pins))))
    out("\n".join(changes) or "already organized")
    if dry:
        out("(dry run - `loft organize --yes` to apply)")
    return changes


# ------------------------------------------------------------------ cswap profile jobs -> ~/.claude/jobs

def profile_dirs():
    root = A.cswap_root() / "sessions"
    return [p for p in sorted(root.glob("*/")) if (p / "jobs").is_dir()] if root.is_dir() else []


def migrate(dry, out=print):
    """Move agents started under `cswap run N` (each profile has its own jobs/ and
    daemon) into ~/.claude/jobs so one view shows them. Each keeps its login via
    providerEnv. The profile must be a Loft account first (`loft account import-cswap`)."""
    cfg = A.load()
    dest = jobs_dir()
    for prof in profile_dirs():
        jobs = [p for p in (prof / "jobs").iterdir() if (p / "state.json").is_file()]
        if not jobs:
            continue
        acc = A.account_for_value(str(prof), cfg)
        if not acc:
            out(f"skip {prof.name}: not a Loft account yet (`loft account import-cswap` first)")
            continue
        busy = [a for a in _cmdlines() if any(str(prof) in x for x in a)]
        if busy:
            out(f"skip {prof.name}: claude is running in that profile (close that `cswap run` window first)")
            continue
        for j in jobs:
            target = dest / j.name
            if target.exists():
                out(f"  {j.name}: already in {dest}, left alone"); continue
            st = json.loads((j / "state.json").read_text())
            env = {k: v for k, v in (st.get("providerEnv") or {}).items() if k not in ("CLAUDE_CONFIG_DIR", A.VAR)}
            out(f"  {j.name}  {prof.name} -> {acc['label']}  {st.get('name') or ''}")
            if dry:
                continue
            (j / "state.json.pre-merge").write_text((j / "state.json").read_text())
            st["providerEnv"] = A.apply_env(env, acc)
            (j / "state.json").write_text(json.dumps(st, indent=2))
            j.rename(target)
    if dry:
        out("(dry run - `loft migrate --yes` to do it)")


# ------------------------------------------------------------------ text list (`loft list`)

TTY = sys.stdout.isatty()
def c(code, s): return f"\033[{code}m{s}\033[0m" if TTY else s
BOLD, DIM, RED, YEL, GRN, CYN = "1", "2", "31", "33", "32", "36"
ANSI = {"blue": "34", "cyan": "36", "orange": "38;5;208", "green": "32", "yellow": "33", "purple": "35", "pink": "95", "red": "31"}


def load(assistant=False):
    """Every agent as a flat dict. The Loft Assistant's own session is left out unless asked for."""
    cfg = A.load()
    jobs = []
    for f in glob.glob(str(jobs_dir() / "*" / "state.json")):
        try:
            st = json.loads(Path(f).read_text())
        except Exception:
            continue
        jid = Path(f).parent.name
        meta = meta_read(jid)
        if meta.get("assistant") and not assistant:
            continue
        acc, _ = job_account(st, cfg)
        jobs.append({
            "id": jid,
            "name": meta.get("title") or st.get("name") or (st.get("intent") or "")[:40] or jid,
            "summary": meta.get("summary") or None,
            "account": acc["label"] if acc else "other login",
            "accountId": acc["id"] if acc else None,
            "bucket": bucket(st),
            "state": st.get("state"),
            "detail": st.get("detail") or "",
            "result": (st.get("output") or {}).get("result") if isinstance(st.get("output"), dict) else None,
            "project": project(st),
            "updated": st.get("updatedAt") or st.get("createdAt"),
            "created": st.get("createdAt"),
            "color": st.get("color"),
            "links": [ch.get("href") for ch in (st.get("children") or []) if isinstance(ch, dict) and ch.get("href")],
        })
    try: pins = set(json.loads((jobs_dir() / "pins.json").read_text()))
    except Exception: pins = set()
    for j in jobs: j["pinned"] = j["id"] in pins
    jobs.sort(key=lambda j: (not j["pinned"], j["updated"] and -parse_t(j["updated"]).timestamp() or 0))
    return jobs


SECTIONS = [("needs", "NEEDS YOU", RED), ("running", "RUNNING", YEL), ("failed", "FAILED", RED), ("done", "DONE", GRN)]


def render(jobs, show_all=False):
    width = shutil.get_terminal_size((120, 20)).columns
    now = datetime.now(timezone.utc)
    lines = []
    accts = sorted({j["account"] for j in jobs})
    lines.append(c(BOLD, f"agents across {len(accts)} accounts") + c(DIM, "  " + "  ".join(accts)))
    for key, title, col in SECTIONS:
        rows = [j for j in jobs if j["bucket"] == key]
        hidden = 0
        if key in ("done", "failed") and not show_all:
            keep = [j for j in rows if (t := parse_t(j["updated"] or "")) and (now - t).days < DONE_DAYS]
            hidden = len(rows) - len(keep)
            rows = keep
        if not rows and not hidden:
            continue
        lines.append("")
        lines.append(c(col + ";1", f"{title} ({len(rows)})") + (c(DIM, f"  +{hidden} older, --all") if hidden else ""))
        for j in rows:
            t = parse_t(j["updated"] or "")
            head = f"  {j['id']}  {j['account']:<14.14} {ago(t):>4}  {j['name'][:32]:<32}  {j['project'][:28]:<28}  "
            room = max(10, width - len(head))
            detail = j["detail"].replace("\n", " ")
            dot = c(ANSI.get(j["color"], DIM), "●") if j["color"] else c(DIM, "·")
            pin = c(YEL, "★") if j["pinned"] else " "
            lines.append(f"{pin}{dot}" + c(DIM, f"{j['id']}  ") + c(CYN, f"{j['account']:<14.14}") + f" {ago(t):>4}  " +
                         c(BOLD, f"{j['name'][:32]:<32}") + c(DIM, f"  {j['project'][:28]:<28}  ") +
                         (detail[:room - 1] + "…" if len(detail) > room else detail))
    lines.append("")
    lines.append(c(DIM, "loft attach <id|name>  ·  loft show <id>  ·  loft watch  ·  loft app"))
    return "\n".join(lines)


def find(jobs, q):
    q = q.lower()
    hits = [j for j in jobs if j["id"].startswith(q)] or [j for j in jobs if q in j["name"].lower()]
    if not hits:
        raise SystemExit(f"no agent matches '{q}'")
    if len(hits) > 1 and not any(j["id"] == q for j in hits):
        print("ambiguous, pick one:")
        for j in hits:
            print(f"  {j['id']}  {j['account']}  {j['name']}")
        raise SystemExit(1)
    return next((j for j in hits if j["id"] == q), hits[0])


def attach(j, extra):
    cfg = A.load()
    acc = A.get(j["accountId"], cfg) if j.get("accountId") else A.default_account(cfg)
    env = A.apply_env(dict(os.environ), acc) if acc else dict(os.environ)
    print(c(DIM, f"attaching {j['id']} ({j['name']}) as {j['account']}"), file=sys.stderr)
    os.chdir(str(HOME))
    os.execvpe(paths.claude_bin(), [paths.claude_bin(), "attach", j["id"], *extra], env)
