#!/usr/bin/env python3
"""Loft's tools for an agent: record a walkthrough of the shared browser as a check, and run checks.

A stdio MCP server (newline-delimited JSON-RPC), started by Claude Code from the --mcp-config Loft
passes when it launches the agent. It calls the Loft server on 127.0.0.1 with the agent's own token,
read from a 0600 file (never on a command line), so it can only touch its own agent's browser.

  agent_mcp.py --key-file <path> --port <loft port>
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
        return {"tools": TOOLS}
    if m == "tools/call":
        route = ROUTES.get(p.get("name"))
        if not route:
            raise LookupError(f"unknown tool {p.get('name')}")
        err, out = call_loft(route[0], route[1](p.get("arguments") or {}))
        return {"content": [{"type": "text", "text": json.dumps(out, indent=1, ensure_ascii=False)}], "isError": err}
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
