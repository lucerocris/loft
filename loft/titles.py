"""Task titles and one-line summaries, so a glance tells sessions apart.

When a task starts, Loft asks a small model (`claude -p --model haiku`, on the task's own
account) for {title, summary} from the prompt and folder. It runs in a background thread
(or a detached `loft describe` from the CLI) and never holds up the request: the card shows
the title derived from the prompt until the better one lands.

Where it is kept:
  agent   <job>/loft-meta.json  {"title", "summary", "titleSource": "auto"|"user"} (core.meta_*)
  todo    the todo record in todos.json ("title", "summary", "titleSource")

A title the user typed is never replaced by a generated one; only its summary is filled in.

Agents Loft didn't start (a terminal `claude --bg`, another agent, a scheduled run) are picked
up by sweep(), which the server runs every SWEEP_EVERY seconds: anything without a summary
gets described once it has a prompt, a few at a time, at most MAX_TRIES times per agent.
"""
import json
import os
import re
import subprocess
import threading

from . import core as C
from . import paths

MODEL = "haiku"
TIMEOUT = 45
TITLE_WORDS = 6
SUMMARY_WORDS = 14

SYSTEM = (
    "You label tasks given to a coding agent so the user can tell sessions apart at a glance. "
    f"Return JSON with two fields. title: at most {TITLE_WORDS} words, imperative, specific "
    "(name the thing being changed, e.g. 'Fix Done column count on board'), no quotes, no trailing period. "
    f"summary: one plain sentence of at most {SUMMARY_WORDS} words saying what the session is for. "
    "Write in the language of the task. Never answer the task itself."
)
SCHEMA = json.dumps({"type": "object", "properties": {"title": {"type": "string"}, "summary": {"type": "string"}},
                     "required": ["title", "summary"]})

SWEEP_EVERY = 20       # seconds between sweeps (server.pending_loop)
SWEEP_BATCH = 3        # model calls a sweep may start
MAX_TRIES = 2          # per agent, counted in loft-meta.json "describeTries" (survives restarts)

_BUSY = set()          # ids being described right now (one model call per card at a time)
_LOCK = threading.Lock()


def derive_title(prompt):
    """A short card title from the first clause of the prompt (the fallback)."""
    first = re.split(r"(?<=[.!?\n])\s", (prompt or "").strip(), maxsplit=1)[0].strip().rstrip(".!?")
    first = re.sub(r"^(please|can you|could you|i want you to|go and)\s+", "", first, flags=re.I)
    words = first.split()
    title = " ".join(words[:7]) + ("…" if len(words) > 7 else "")
    return title[:1].upper() + title[1:]


def _clean(s, words, chars):
    s = " ".join(str(s or "").split()).strip().strip("\"'`").rstrip(".")
    w = s.split()
    if len(w) > words:
        s = " ".join(w[:words])
    return s[:chars].strip()


def ask_model(prompt, folder="", account=None, timeout=TIMEOUT):
    """{"title", "summary"} from the model, or None when it couldn't answer (no login, offline,
    timeout, bad output). `account` is anything board.account_env takes."""
    from . import board as B
    prompt = (prompt or "").strip()
    if not prompt:
        return None
    work = paths.data_dir() / "titles"   # a neutral folder: no project CLAUDE.md, nothing to read
    try:
        work.mkdir(parents=True, exist_ok=True)
    except OSError:
        work = paths.HOME
    text = (f"Folder: {folder}\n" if folder else "") + "Task:\n" + prompt[:4000]
    cmd = [paths.claude_bin(), "-p", "--model", MODEL, "--output-format", "json", "--no-session-persistence",
           "--safe-mode", "--strict-mcp-config", "--tools", "", "--json-schema", SCHEMA,
           "--system-prompt", SYSTEM, text]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(work),
                           env=B.account_env(account), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse(r.stdout)


def parse(out):
    """The model's {title, summary} from `claude -p --output-format json` output (or plain JSON)."""
    try:
        d = json.loads((out or "").strip().splitlines()[-1] if out and out.strip() else "")
    except (ValueError, IndexError):
        m = re.search(r"\{.*\}", out or "", re.S)
        try:
            d = json.loads(m.group(0)) if m else None
        except ValueError:
            d = None
    if not isinstance(d, dict):
        return None
    if d.get("is_error"):
        return None
    got = d.get("structured_output")
    if not isinstance(got, dict) and isinstance(d.get("result"), str):
        try:
            got = json.loads(re.search(r"\{.*\}", d["result"], re.S).group(0))
        except (AttributeError, ValueError):
            got = None
    if not isinstance(got, dict) and "title" in d:
        got = d
    if not isinstance(got, dict):
        return None
    title = _clean(got.get("title"), TITLE_WORDS + 2, 60)
    summary = _clean(got.get("summary"), SUMMARY_WORDS + 4, 140)
    if not title:
        return None
    return {"title": title[:1].upper() + title[1:], "summary": (summary + ".") if summary else None}


# ------------------------------------------------------------------ cards

def first_prompt(jid):
    """The prompt an agent was started with: state.json intent, else the transcript's first message."""
    try:
        st = json.loads((C.jobs_dir() / jid / "state.json").read_text())
    except (OSError, ValueError):
        return "", ""
    folder = C.project(st)
    if (st.get("intent") or "").strip():
        return st["intent"].strip(), folder
    tp = C.transcript_of(st)
    if tp:
        from . import transcript as TR
        try:
            items = TR.read_page(tp, limit=400)["items"]
        except OSError:
            items = []
        for it in items:
            if it["kind"] == "user" and it.get("text", "").strip():
                return it["text"].strip(), folder
    return "", folder


def describe(card, keep_title=None, ask=None):
    """Generate and save {title, summary} for a card (agent or todo). keep_title=None keeps a
    title the user typed and replaces an automatic one. Returns {"title", "summary", "ok"};
    ok=False means the model didn't answer and nothing but a fallback title was saved."""
    from . import board as B
    ask = ask or ask_model
    jid = card["id"]
    if card["kind"] == "todo":
        todo = next((t for t in B.load_todos() if t["id"] == jid), None)
        if not todo:
            raise KeyError(jid)
        prompt, folder, user_title = todo.get("prompt") or "", os.path.basename((todo.get("cwd") or "").rstrip("/")), todo.get("titleSource", "user") == "user"
    else:
        prompt, folder = first_prompt(jid)
        user_title = C.meta_read(jid).get("titleSource") == "user"
    keep = user_title if keep_title is None else bool(keep_title)
    got = ask(prompt, folder, card) if prompt else None
    if not got:
        title = card["name"] if keep or not prompt else (derive_title(prompt) or card["name"])
        return {"title": title, "summary": card.get("summary"), "ok": False}
    title = card["name"] if keep else got["title"]
    save(card, title=None if keep else title, summary=got["summary"], source="auto")
    return {"title": title, "summary": got["summary"], "ok": True}


def save(card, title=None, summary=None, source="user"):
    """Write a title and/or summary (None = leave as is, "" = clear the summary)."""
    from . import board as B
    jid = card["id"]
    if card["kind"] == "todo":
        todos = B.load_todos()
        for t in todos:
            if t["id"] == jid:
                if title:
                    t["title"], t["titleSource"] = title, source
                if summary is not None:
                    t["summary"] = summary or None
        B.save_todos(todos)
        return
    fields = {}
    if title:
        fields.update(title=title, titleSource=source)
    if summary is not None:
        fields["summary"] = summary or None
    if fields:
        C.meta_write(jid, **fields)
    if title and not card.get("live"):   # keep `claude agents` in step while no worker would overwrite it
        try:
            B.write_state(jid, lambda st: {**st, "name": title, "nameSource": "user"})
        except (OSError, ValueError):
            pass


def describe_async(card, keep_title=None, log=print, done=None):
    """describe() in a daemon thread; at most one at a time per card. False if one is running."""
    with _LOCK:
        if card["id"] in _BUSY:
            return False
        _BUSY.add(card["id"])

    def run():
        try:
            r = describe(card, keep_title)
            log(f"describe {card['id']}: {r['title']!r}" + ("" if r["ok"] else " (model didn't answer; kept the title)"))
            if done:
                done(r)
        except Exception as e:
            log(f"describe {card['id']} failed: {type(e).__name__}: {e}")
        finally:
            with _LOCK:
                _BUSY.discard(card["id"])
    threading.Thread(target=run, daemon=True).start()
    return True


def after_launch(jid, todo, log=print):
    """A todo just became agent `jid`: carry its title and summary over, or describe it now."""
    src = todo.get("titleSource", "user")
    fields = {"title": todo.get("title"), "titleSource": src}
    if todo.get("summary"):
        fields["summary"] = todo["summary"]
    try:
        C.meta_write(jid, **fields)
    except OSError:
        return
    if todo.get("summary"):
        return
    card = {"id": jid, "kind": "agent", "name": todo.get("title") or jid, "account": todo.get("account"),
            "live": True, "summary": None}
    describe_async(card, keep_title=src == "user", log=log)


def sweep(cards, log=print, batch=SWEEP_BATCH):
    """Describe the newest agents that have no summary yet (started outside Loft, or whose
    first describe never landed). Returns how many describes it started."""
    started = 0
    todo = [c for c in cards if c["kind"] == "agent" and not c.get("summary") and not c.get("assistant")]
    todo.sort(key=lambda c: c["updated"].timestamp() if c.get("updated") else 0, reverse=True)
    for c in todo:
        if started >= batch:
            break
        with _LOCK:
            if c["id"] in _BUSY:
                continue
        meta = C.meta_read(c["id"])
        tries = int(meta.get("describeTries") or 0)
        if meta.get("summary") or tries >= MAX_TRIES:
            continue
        prompt, _ = first_prompt(c["id"])
        if not prompt:
            continue                     # not given a task yet: look again next sweep
        keep = None
        if not meta.get("titleSource"):  # a name the user gave the agent in Claude Code stays
            try:
                st = json.loads((C.jobs_dir() / c["id"] / "state.json").read_text())
                keep = True if st.get("nameSource") == "user" else None
            except (OSError, ValueError):
                pass
        try:
            C.meta_write(c["id"], describeTries=tries + 1)
        except OSError:
            continue
        if describe_async(c, keep_title=keep, log=log):
            started += 1
    return started
