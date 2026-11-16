#!/usr/bin/env python3
"""Loft's tools for an agent: browser checks, the shared Android phone, and the login bank.

A stdio MCP server (newline-delimited JSON-RPC), started by Claude Code from the --mcp-config Loft
passes when it launches the agent. It calls the Loft server on 127.0.0.1 with the agent's own token,
read from a 0600 file (never on a command line), so it can only touch its own agent's browser.

  agent_mcp.py --key-file <path> --port <loft port> [--tools checks,phone,logins]

Tool groups: checks (record and run browser checks), phone (loft_phone_*: the Android device Loft
shares with the agent, loft/mobile.py), logins (loft_logins: which sites the agent's browser is
signed in to through the login bank, and a refresh after the user signs in again, loft/sites.py).
"""
import json, sys, urllib.error, urllib.request

TOOLS = [
    {"name": "loft_record_start", "description": "Start recording the shared browser as a new check for this project. "
     "Then walk the flow with the browser tools: clicks, filled fields, Enter and page loads become steps. "
     "Passwords are never recorded (the project's saved login covers sign-in).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "loft_record_expect", "description": "While recording: add a step asserting that text is visible on the page "
     "(optionally inside a CSS selector).",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "selector": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "loft_record_save", "description": "Stop recording and save the steps as a named check. Every agent on this "
     "project must then pass it.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "loft_record_discard", "description": "Stop recording without saving.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "loft_run_checks", "description": "Replay this project's recorded checks against your dev server "
     "(or base_url) and report which pass.",
     "inputSchema": {"type": "object", "properties": {"base_url": {"type": "string"}}}},
    {"name": "loft_list_checks", "description": "List this project's recorded checks and their steps.",
     "inputSchema": {"type": "object", "properties": {}}},
]
ROUTES = {"loft_record_start": ("record", lambda a: {"on": True}),
          "loft_record_expect": ("expect", lambda a: {"text": a.get("text", ""), "selector": a.get("selector")}),
          "loft_record_save": ("record", lambda a: {"on": False, "name": a.get("name", "")}),
          "loft_record_discard": ("record", lambda a: {"on": False}),
          "loft_run_checks": ("run", lambda a: {"base": a.get("base_url")}),
          "loft_list_checks": ("checks", lambda a: None)}

XY = {"x": {"type": "number", "description": "x in the screenshot's pixels"},
      "y": {"type": "number", "description": "y in the screenshot's pixels"}}
LOOK = {"look": {"type": "boolean", "description": "Return a fresh screenshot afterwards (default true)"}}
PHONE_TOOLS = [
    {"name": "loft_phone_screen", "description": "See the shared Android phone: a screenshot plus a numbered list of "
     "the elements on screen (text, ids, tappable/editable, centre point in screenshot pixels). Also says which "
     "device it is (its adb serial) and which app is in front. Starts an emulator if none is running yet.",
     "inputSchema": {"type": "object", "properties": {"elements": {"type": "boolean", "description": "Include the element list (default true)"}}}},
    {"name": "loft_phone_tap", "description": "Tap an element from loft_phone_screen's list (element: its number), or a "
     "point (x, y in screenshot pixels). long: true for a long press.",
     "inputSchema": {"type": "object", "properties": {"element": {"type": "integer"}, **XY, "long": {"type": "boolean"}, **LOOK}}},
    {"name": "loft_phone_swipe", "description": "Swipe: direction up/down/left/right (scrolls the content the other "
     "way, like a finger), or from (x1, y1) to (x2, y2) in screenshot pixels.",
     "inputSchema": {"type": "object", "properties": {"direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                                                      "x1": {"type": "number"}, "y1": {"type": "number"},
                                                      "x2": {"type": "number"}, "y2": {"type": "number"},
                                                      "ms": {"type": "integer"}, **LOOK}}},
    {"name": "loft_phone_type", "description": "Type text into the focused field (tap the field first). submit: "
     "true presses Enter after. clear: true empties the field first. ASCII only.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "submit": {"type": "boolean"},
                                                      "clear": {"type": "boolean"}, **LOOK}, "required": ["text"]}},
    {"name": "loft_phone_key", "description": "Press a key: back, home, enter, tab, delete, recents, up, down, left, "
     "right, power, volume_up, volume_down, rotate (or any KEYCODE_ name).",
     "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}, **LOOK}, "required": ["key"]}},
    {"name": "loft_phone_open", "description": "Open an app by package name (com.example.app), an activity "
     "(com.example.app/.MainActivity), or a URL / deep link (https://..., myapp://...).",
     "inputSchema": {"type": "object", "properties": {"target": {"type": "string"}, **LOOK}, "required": ["target"]}},
    {"name": "loft_phone_install", "description": "Install (or update) an APK you built, by its path on this computer.",
     "inputSchema": {"type": "object", "properties": {"apk": {"type": "string"}}, "required": ["apk"]}},
    {"name": "loft_phone_logs", "description": "Recent logcat lines (errors and crashes show here), optionally only "
     "for one app's package. level: V, D, I, W, E (default I).",
     "inputSchema": {"type": "object", "properties": {"package": {"type": "string"}, "lines": {"type": "integer"},
                                                      "level": {"type": "string"}}}},
    {"name": "loft_phone_use", "description": "List the phones and emulators Loft has (no device), or switch to one: "
     "a device serial or an emulator name (Loft starts it if needed).",
     "inputSchema": {"type": "object", "properties": {"device": {"type": "string"}}}},
]
LOGIN_TOOLS = [
    {"name": "loft_logins", "description": "The login bank: the sites the user signed in to once in Loft, which your "
     "browser started signed in to (Seller Center, a helpdesk, Google Sheets, ...), each with its address, a note "
     "(e.g. which account it is) and whether it still looks signed in. Pass login to ask about one. After the user "
     "signs in again in Loft, pass refresh: true to copy the fresh session into your browser. Loft never gives "
     "you passwords.",
     "inputSchema": {"type": "object", "properties": {"login": {"type": "string", "description": "A login's name or host"},
                                                      "refresh": {"type": "boolean"}}}},
]
CHECK_NAMES = {t["name"] for t in TOOLS}
for _t in PHONE_TOOLS:
    ROUTES[_t["name"]] = ("phone-" + _t["name"].rsplit("_", 1)[-1], lambda a: dict(a))
ROUTES["loft_logins"] = ("logins", lambda a: dict(a))


def tools():
    groups = set((arg("--tools") or "checks").split(","))
    out = []
    if "checks" in groups:
        out += TOOLS
    if "phone" in groups:
        out += PHONE_TOOLS
    if "logins" in groups:
        out += LOGIN_TOOLS
    return out


def content(out):
    """Loft's JSON answer as MCP content: a screenshot becomes an image block, the rest text."""
    out = dict(out)
    img = out.pop("image", None)
    mime = out.pop("mime", "image/png")
    text = out.pop("text", None)
    blocks = []
    if text:
        blocks.append({"type": "text", "text": text})
    rest = {k: v for k, v in out.items() if v not in (None, "", [])}
    msg = rest.pop("message", None)
    if msg and not text:
        blocks.append({"type": "text", "text": msg})
    if rest:
        blocks.append({"type": "text", "text": json.dumps(rest, indent=1, ensure_ascii=False)})
    if img:
        blocks.append({"type": "image", "data": img, "mimeType": mime})
    return blocks or [{"type": "text", "text": "Done."}]


def arg(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


def call_loft(action, body):
    token = open(arg("--key-file")).read().strip()
    req = urllib.request.Request(f"http://127.0.0.1:{arg('--port') or 7717}/api/agent/{action}",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"X-Loft-Agent": token, "Content-Type": "application/json"},
                                 method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return False, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return True, json.loads(e.read())
        except ValueError:
            return True, {"message": f"Loft answered {e.code}"}
    except OSError as e:
        return True, {"message": f"Couldn't reach Loft: {e}"}


def handle(msg):
    m, p = msg.get("method"), msg.get("params") or {}
    if m == "initialize":
        return {"protocolVersion": p.get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}},
                "serverInfo": {"name": "loft", "version": "1"}}
    if m == "ping":
        return {}
    if m == "tools/list":
        return {"tools": tools()}
    if m == "tools/call":
        route = ROUTES.get(p.get("name"))
        if not route or p.get("name") not in {t["name"] for t in tools()}:
            raise LookupError(f"unknown tool {p.get('name')}")
        err, out = call_loft(route[0], route[1](p.get("arguments") or {}))
        if p.get("name") in CHECK_NAMES:
            return {"content": [{"type": "text", "text": json.dumps(out, indent=1, ensure_ascii=False)}], "isError": err}
        return {"content": content(out), "isError": err}
    raise LookupError(f"unknown method {m}")


def main():
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "id" not in msg:
            continue   # notifications (initialized, cancelled): nothing to answer
        try:
            out = {"jsonrpc": "2.0", "id": msg["id"], "result": handle(msg)}
        except LookupError as e:
            out = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": str(e)}}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
