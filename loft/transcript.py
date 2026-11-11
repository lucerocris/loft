"""A job's session transcript (.jsonl) as a clean chat for Loft Mobile.

GET /api/cards/<id>/messages reads the tail of the transcript, never the whole
file (they reach tens of MB), and turns it into a short list of items:

  user       {text}                         a prompt you (or Loft) sent
  assistant  {text}                         the agent's prose, markdown
  tool       {tool, name, title, detail, plan?}  one tool call, compact ("Bash · Run the tests");
                                            ExitPlanMode carries the plan (markdown)
  result     {for, ok, preview, lines, text, diff?}  a tool's output, matched to its call by `for`
  question   {tool, questions:[{question, header, multi, options:[{label, description}]}]}
  note       {text}                         interrupted, compacted, background task update

Every item has a stable `id` (the entry's uuid, plus the block index) and `at`.
Thinking, system reminders, meta, sidechain and compact-summary entries are dropped.

Cursors are byte offsets into the transcript: `cursor` is where this read
ended (pass it back as after= to get only new items); `start` is where it began
(pass it as before= to page back). `session` names the file; when it changes
(a resumed session), the old cursors mean nothing and the phone starts over.
"""
import json
import os
import re
from pathlib import Path

CHUNK = 256 * 1024
MAX_SCAN = 24 * 1024 * 1024     # bytes one request may read; a page can come back short
MAX_FORWARD = 8 * 1024 * 1024   # after=: more new bytes than this and we read only the newest
TEXT_CAP = 4000                 # result text kept for "show more"
PREVIEW_LINES = 4
HOME = str(Path.home())

_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_TAG = re.compile(r"<([a-z-]+)>(.*?)</\1>", re.S)
_NOISE_TAGS = ("local-command-caveat", "local-command-stdout", "bash-stdout", "bash-stderr")


def _home(s):
    return s.replace(HOME, "~") if isinstance(s, str) else s


def _one_line(s, n=160):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ------------------------------------------------------------------ reading
def _complete_end(f, size):
    """Offset just past the last newline (a line still being written is left out)."""
    if size == 0:
        return 0
    pos = size
    while pos > 0:
        n = min(CHUNK, pos)
        f.seek(pos - n)
        buf = f.read(n)
        i = buf.rfind(b"\n")
        if i >= 0:
            return pos - n + i + 1
        pos -= n
    return 0


def lines_backward(f, end):
    """(offset, bytes) for each complete line before `end`, newest first."""
    pos, carry = end, b""
    while pos > 0:
        n = min(CHUNK, pos)
        pos -= n
        f.seek(pos)
        buf = f.read(n) + carry
        parts = buf.split(b"\n")
        carry = parts[0]                     # may continue in the previous chunk
        off = pos + len(parts[0]) + 1
        tail = []
        for p in parts[1:]:
            tail.append((off, p))
            off += len(p) + 1
        for o, p in reversed(tail):
            if p.strip():
                yield o, p
    if carry.strip():
        yield 0, carry


def _entries(lines):
    for off, raw in lines:
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if isinstance(e, dict):
            yield off, e


# ------------------------------------------------------------------ parsing
def _clean_user_text(text):
    """A prompt without system reminders and harness wrappers; '' = not a prompt."""
    t = _REMINDER.sub("", text or "").strip()
    if not t:
        return "", None
    if t.startswith("<task-notification>"):
        m = re.search(r"<summary>(.*?)</summary>", t, re.S)
        return "", _one_line(m.group(1) if m else "Background task update", 200)
    tags = dict(_TAG.findall(t))
    if tags:
        if "command-name" in tags:
            args = tags.get("command-args", "").strip()
            return (tags["command-name"].strip() + (" " + args if args else "")), None
        if "bash-input" in tags:
            return "! " + tags["bash-input"].strip(), None
        if any(k in tags for k in _NOISE_TAGS):
            rest = _TAG.sub("", t).strip()
            return rest, None
    if t.startswith("[Request interrupted"):
        return "", "Interrupted"
    return t, None


def tool_summary(name, inp):
    """(label, title, detail) for a tool call: 'Bash', 'Run the tests', 'npm test'."""
    inp = inp if isinstance(inp, dict) else {}
    label = name
    if name.startswith("mcp__"):
        bits = name.split("__")
        server = bits[1].replace("plugin_", "").split("_")[-1] if len(bits) > 1 else "mcp"
        label = f"{server} · {bits[-1]}"
    path = inp.get("file_path") or inp.get("notebook_path") or inp.get("path")
    if name == "Bash":
        cmd = str(inp.get("command") or "")
        desc = inp.get("description")
        title = desc or (cmd.strip().splitlines()[0] if cmd.strip() else "Run a command")
        detail = _home(_one_line(cmd, 300)) if desc or "\n" in cmd.strip() or len(cmd.strip()) > 120 else ""
        return label, _one_line(_home(title), 120), detail
    if name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit") and path:
        return label, os.path.basename(str(path)), _home(str(path))
    if name in ("Grep", "Glob"):
        where = f" in {_home(path)}" if path else ""
        return label, _one_line(inp.get("pattern") or "", 120), _one_line(f"{inp.get('pattern') or ''}{where}", 300)
    if name in ("Agent", "Task"):
        return label, _one_line(inp.get("description") or "Subagent", 120), _one_line(inp.get("prompt") or "", 300)
    if name == "WebFetch":
        return label, _one_line(inp.get("url") or "", 120), _one_line(inp.get("prompt") or "", 300)
    if name == "WebSearch":
        return label, _one_line(inp.get("query") or "", 120), ""
    if name == "ExitPlanMode":
        return "Plan", "Ready to code?", ""
    if name == "TodoWrite":
        todos = inp.get("todos") or []
        return "Todos", f"{len(todos)} items", ""
    for k in ("description", "title", "subject", "query", "prompt", "command", "url", "name"):
        if isinstance(inp.get(k), str) and inp[k].strip():
            return label, _one_line(_home(inp[k]), 120), ""
    for v in inp.values():
        if isinstance(v, str) and v.strip():
            return label, _one_line(_home(v), 120), ""
    return label, "", ""


def _result_text(content):
    if isinstance(content, str):
        return content, 0
    out, images = [], 0
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(b.get("text") or "")
        elif isinstance(b, dict) and b.get("type") == "image":
            images += 1
    return "\n".join(out), images


def _diff(tur):
    if not isinstance(tur, dict) or not isinstance(tur.get("structuredPatch"), list):
        return None
    add = rem = 0
    for h in tur["structuredPatch"]:
        for ln in (h or {}).get("lines") or []:
            if ln.startswith("+"):
                add += 1
            elif ln.startswith("-"):
                rem += 1
    if tur.get("type") == "create" and not add and isinstance(tur.get("content"), str):
        add = tur["content"].count("\n") + 1
    return {"add": add, "del": rem}


def _result_item(eid, at, b, tur):
    text, images = _result_text(b.get("content"))
    text = _REMINDER.sub("", text).strip("\n")
    lines = text.splitlines()
    item = {"id": eid, "kind": "result", "at": at, "for": b.get("tool_use_id"), "ok": not b.get("is_error"),
            "preview": "\n".join(_one_line(ln, 200) if len(ln) > 200 else ln for ln in lines[:PREVIEW_LINES]),
            "lines": len(lines), "text": text[:TEXT_CAP], "cut": len(text) > TEXT_CAP}
    if images:
        item["images"] = images
    d = _diff(tur)
    if d:
        item["diff"] = d
    return item


def entry_items(e):
    """Chat items for one transcript entry (chronological), [] for noise."""
    t = e.get("type")
    uid, at = e.get("uuid") or "", e.get("timestamp")
    if t == "system":
        if e.get("subtype") == "compact_boundary":
            return [{"id": uid, "kind": "note", "at": at, "text": "Earlier conversation compacted"}]
        return []
    if t not in ("user", "assistant") or e.get("isSidechain") or e.get("isMeta") or e.get("isVisibleInTranscriptOnly"):
        return []
    if e.get("isCompactSummary"):
        return []
    msg = e.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    out = []
    for i, b in enumerate(content):
        if not isinstance(b, dict):
            continue
        bid, bt = f"{uid}:{i}", b.get("type")
        if t == "user":
            if bt == "text":
                text, note = _clean_user_text(b.get("text"))
                if note:
                    out.append({"id": bid, "kind": "note", "at": at, "text": note})
                elif text:
                    if out and out[-1]["kind"] == "user":
                        out[-1]["text"] += "\n\n" + text
                    else:
                        out.append({"id": bid, "kind": "user", "at": at, "text": text})
            elif bt == "image":
                if out and out[-1]["kind"] == "user":
                    out[-1]["images"] = out[-1].get("images", 0) + 1
                else:
                    out.append({"id": bid, "kind": "user", "at": at, "text": "", "images": 1})
            elif bt == "tool_result":
                out.append(_result_item(bid, at, b, e.get("toolUseResult")))
        else:
            if bt == "text":
                text = (b.get("text") or "").strip()
                if text and text != "(no content)":
                    out.append({"id": bid, "kind": "assistant", "at": at, "text": text})
            elif bt == "tool_use":
                name, inp = b.get("name") or "tool", b.get("input") or {}
                if name == "AskUserQuestion":
                    qs = []
                    for q in inp.get("questions") or []:
                        qs.append({"question": q.get("question") or "", "header": q.get("header") or "",
                                   "multi": bool(q.get("multiSelect")),
                                   "options": [{"label": o.get("label") or "", "description": o.get("description") or ""}
                                               for o in q.get("options") or [] if isinstance(o, dict)]})
                    out.append({"id": bid, "kind": "question", "at": at, "tool": b.get("id"), "questions": qs})
                    continue
                label, title, detail = tool_summary(name, inp)
                item = {"id": bid, "kind": "tool", "at": at, "tool": b.get("id"), "name": label,
                        "title": title, "detail": detail}
                if name == "ExitPlanMode" and isinstance(inp.get("plan"), str):
                    item["plan"] = inp["plan"][:TEXT_CAP]
                out.append(item)
    # The final 'user' text of a tool_result-only entry is never a prompt.
    return out


# ------------------------------------------------------------------ pages
def read_page(path, limit=60, before=None, after=None):
    """One page of chat items from the transcript at `path`.

    after=N: everything new since offset N (oldest first, at most `limit`;
    `gap` = true when older new items were skipped). before=N: the `limit`
    items just before offset N. Neither: the newest `limit` items.
    """
    limit = max(1, min(int(limit or 60), 300))
    path = Path(path)
    with open(path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        end = _complete_end(f, size)
        res = {"session": path.stem, "size": size, "cursor": end, "gap": False}
        if after is not None:
            after = int(after)
            if after > end:   # the file shrank or was replaced: start over
                return {**read_page(path, limit), "reset": True}
            start = after
            if end - after > MAX_FORWARD:
                start, res["gap"] = end - MAX_FORWARD, True
            f.seek(start)
            blob = f.read(end - start)
            if start != after:   # we jumped into the middle of a line
                cut = blob.find(b"\n") + 1
                start, blob = start + cut, blob[cut:]
            off, lines = start, []
            for raw in blob.split(b"\n"):
                if raw.strip():
                    lines.append((off, raw))
                off += len(raw) + 1
            items = [it for _, e in _entries(lines) for it in entry_items(e)]
            if len(items) > limit:
                items, res["gap"] = items[-limit:], True
            res.update(items=items, start=after, more=after > 0)
            return res
        stop = end if before is None else min(int(before), end)
        items, first, scanned = [], stop, 0
        for off, e in _entries(lines_backward(f, stop)):
            scanned = stop - off
            first = off
            items[:0] = entry_items(e)
            if len(items) >= limit or scanned > MAX_SCAN:
                break
        else:
            first = 0
        res.update(items=items, start=first, more=first > 0)
        return res


def pending_options(items):
    """Options of the last AskUserQuestion that has no answer yet (else None)."""
    answered = {it["for"] for it in items if it["kind"] == "result"}
    for it in reversed(items):
        if it["kind"] == "question":
            return None if it["tool"] in answered else it
        if it["kind"] in ("user", "assistant"):
            return None
    return None
