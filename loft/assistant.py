"""The Loft Assistant: one long-lived Claude session whose only job is operating Loft.

It runs as an ordinary background agent (`claude --bg`) on the default account, in its own
folder under Loft's data dir (Claude won't start background agents in $HOME), with a
CLAUDE.md there and an --append-system-prompt that make it Loft's operator. It acts only
through this install's `loft` CLI, which it may run without permission prompts.

  <config>/assistant.json      {"id": <job id>, "account", "started"}: the current session
  <job>/loft-meta.json         {"assistant": true, ...}: marks its card everywhere

Its card is left out of the inbox, the board, `loft list` and every bulk action.
"""
import json
import os

from . import accounts as A
from . import board as B
from . import core as C
from . import paths

NAME = "Loft Assistant"
SUMMARY = "Runs Loft for you: starts, stops, archives and answers about agents."
# Loft's own env overrides, handed to the assistant's tools so its `loft` calls see the same
# config and jobs as the Loft that started it (a test server, a second install).
PASS_ENV = ("LOFT_CONF_DIR", "LOFT_CLAUDE_HOME", "LOFT_CLAUDE", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
            "XDG_CACHE_HOME", "LOFT_PORT", "LOFT_CSWAP_ROOT")


def state_file():
    return paths.config_dir() / "assistant.json"


def home():
    return paths.data_dir() / "assistant"


def loft_bin():
    """This install's CLI, absolute (a clone, or /usr/lib/Loft)."""
    return str(paths.REPO / "bin" / "loft")


def allowed_tools():
    b = loft_bin()
    return [f"Bash({b} *)", f"Bash({b})"]


def read():
    try:
        d = json.loads(state_file().read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def write(d):
    paths.write_private(state_file(), json.dumps(d, indent=2))


def current():
    """The assistant's job id while its session exists (not archived), else None."""
    jid = read().get("id")
    if jid and (C.jobs_dir() / jid / "state.json").is_file():
        return jid
    return None


def card(cards=None):
    jid = current()
    if not jid:
        return None
    cards = cards if cards is not None else B.load_cards(True)
    return next((c for c in cards if c["id"] == jid), None)


def account(cfg=None):
    return A.default_account(cfg or A.load())


def info(cards=None):
    """GET /api/assistant. running = it is working on a turn right now."""
    c = card(cards)
    acc = account()
    return {"id": c["id"] if c else None, "card": c, "account": (c or {}).get("account") or (acc["id"] if acc else None),
            "running": bool(c and B.busy(c))}


def instructions():
    loft = loft_bin()
    return f"""# Loft Assistant

You are the Loft Assistant. Loft is the user's app for running Claude Code background agents
(also called sessions or tasks) across several Claude accounts. Your only job is to do in Loft
what the user asks, and to answer questions about their agents. You act only through the
`loft` command line, always by this absolute path:

    {loft}

## Reading
- `{loft} list --json`: every agent with id, name (its title), summary, account, bucket
  (needs = waiting on the user, running = working, failed, done), detail, result, project, updated.
- `{loft} show <id|name>`: one agent in full.
- `{loft} project list`, `{loft} account list`.

## Acting
- `{loft} archive <id|name>...`, or `--done` (everything finished), `--idle` (everything not working).
  Archive never deletes: it moves agents to ~/.claude/jobs-archive.
- `{loft} stop <id|name>...`, or `--all`: stops workers. Sessions are kept and can be resumed.
- `{loft} new "<prompt>" --dir <folder> [--account <id>] [--project <id|name>] [--title "<title>"]`:
  start a new agent. Without --dir it uses the project's first folder.
- `{loft} reply <id|name> "<message>"`: send a message to an agent (queued if it is mid-turn).
- `{loft} rename <id|name> [--title "<t>"] [--summary "<s>"]`, `{loft} describe <id|name>` or `--missing`.
- `{loft} switch <id|name> <account> [--now]`: bill an agent to another account.

## How to work
- Do it, then answer in one or two short sentences: what you did, with counts and names. No preamble.
- Clear requests need no confirmation: "archive everything that's done", "stop all agents",
  "close all the sessions". Ask first only when a destructive request is ambiguous: a name
  that matches several agents, or archiving something still working that the user didn't clearly mean.
- "What needs me?": the agents in bucket needs (and failed), one line each: name, then what it asks.
- Never read, edit or run anything in the user's code folders yourself, and never use tools other
  than `{loft}` for Loft work. For real work, start an agent with `{loft} new` in the right folder;
  if you can't tell the folder from the projects or recent agents, ask.
- You can't see the Loft window. The user sees your changes there live.
"""


def system_prompt():
    return (f"You are the Loft Assistant: you operate Loft for the user through {loft_bin()} only. "
            "Follow the CLAUDE.md in your working folder. Answer briefly.")


def settings_json():
    env = {k: os.environ[k] for k in PASS_ENV if os.environ.get(k)}
    return json.dumps({"env": env}) if env else None


def start(message):
    """Start a fresh assistant session with `message` as its first prompt. (jid, error)."""
    cfg = A.load()
    acc = account(cfg)
    if not acc:
        return None, "No account is set up yet."
    d = home()
    d.mkdir(parents=True, exist_ok=True)
    (d / "CLAUDE.md").write_text(instructions())
    args = []
    sj = settings_json()
    if sj:
        args += ["--settings", sj]
    # --allowedTools takes several values: the flag after it ends the list
    args += ["--allowedTools", *allowed_tools(), "--append-system-prompt", system_prompt()]
    jid, err = B.spawn_bg(str(d), acc, args, message, NAME)
    if not jid:
        return None, err
    try:
        C.meta_write(jid, assistant=True, title=NAME, titleSource="user", summary=SUMMARY)
    except OSError:
        pass
    write({"id": jid, "account": acc["id"], "started": B.now_utc().isoformat()})
    return jid, None


def send(message, deliver):
    """POST /api/assistant: send a message, starting the session first when there is none.
    deliver(card, text) types it into a live session. Returns (status, {id, message})."""
    message = (message or "").strip()
    if not message:
        return 400, {"message": "Write a message first."}
    c = card()
    if not c:
        jid, err = start(message)
        if not jid:
            return 500, {"message": err or "Couldn't start the assistant."}
        return 200, {"id": jid, "message": "Started"}
    if B.busy(c) or B.queue_read(c["id"])["items"]:
        n = B.queue_add(c["id"], message)
        return 200, {"id": c["id"], "message": f"Queued ({n} waiting): sends after this turn" if n > 1 else "Queued: sends after this turn"}
    deliver(c, message)
    return 200, {"id": c["id"], "message": "Sent"}


def reset():
    """Archive the current session; the next message starts a fresh one. (status, {message})."""
    c = card()
    if c:
        err = B.stop_agent(c) if (c.get("live") or B.busy(c)) else None
        if err:
            return 409, {"message": f"Couldn't stop the assistant: {err}"}
        B.act_archive({**c, "col": "done", "state": "done", "live": False})
    write({})
    return 200, {"message": "Fresh conversation"}
