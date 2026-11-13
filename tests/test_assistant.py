"""Tests for smart titles + summaries, bulk actions, the Loft Assistant and their CLI.

`claude` is a fake script (LOFT_CLAUDE) that answers `-p` with a title, starts "background
agents" by writing their state.json into the temp jobs dir, and logs every call.

    python3 -m unittest tests.test_assistant -v
"""
import io
import json
import os
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from loft import accounts as A, assistant as AS, board as B, cli, core as C, titles as TI, usage as U  # noqa: E402
from test_accounts import Env  # noqa: E402
from test_switch import load_server  # noqa: E402

FAKE = r'''#!/usr/bin/env python3
import json, os, sys, uuid
from pathlib import Path
a = sys.argv[1:]
log = os.environ["FAKE_LOG"]
with open(log, "a") as f:
    f.write(json.dumps({"argv": a, "cwd": os.getcwd(), "login": os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")}) + "\n")
mode = Path(os.environ["FAKE_MODE"]).read_text().strip() if Path(os.environ["FAKE_MODE"]).exists() else "ok"
if "-p" in a:
    if mode == "fail":
        print("not logged in", file=sys.stderr); sys.exit(1)
    task = a[-1]
    words = [w for w in task.split("Task:\n", 1)[-1].split() if w.lower() != "the"]
    word = words[0] if words else "thing"
    print(json.dumps({"type": "result", "is_error": False, "result": "",
                      "structured_output": {"title": f"Fix the {word} bug", "summary": f"Fixes the {word} bug in the app"}}))
    sys.exit(0)
if "--bg" in a:
    jid = uuid.uuid4().hex[:8]
    d = Path(os.environ["LOFT_CLAUDE_HOME"]) / "jobs" / jid
    d.mkdir(parents=True)
    name = a[a.index("--name") + 1] if "--name" in a else ""
    (d / "state.json").write_text(json.dumps({"name": name, "state": "working", "intent": a[-1], "cwd": os.getcwd(),
        "providerEnv": {"CLAUDE_SECURESTORAGE_CONFIG_DIR": os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR", "")}}))
    print(f"backgrounded · {jid}")
    sys.exit(0)
if a and a[0] == "stop":
    print("stopped"); sys.exit(0)
sys.exit(0)
'''


class AssistantEnv(Env):
    def setUp(self):
        super().setUp()
        self.cfg3()
        fake = self.tmp / "fake-claude"
        fake.write_text(FAKE); fake.chmod(0o755)
        self.log = self.tmp / "claude.log"
        self.mode = self.tmp / "claude.mode"
        self.env2 = mock.patch.dict(os.environ, {"LOFT_CLAUDE": str(fake), "FAKE_LOG": str(self.log),
                                                  "FAKE_MODE": str(self.mode)})
        self.env2.start()
        self.folder = self.tmp / "code" / "shop"
        self.folder.mkdir(parents=True)
        self.p = [mock.patch.object(C, "live_sessions", lambda: set()),
                  mock.patch.object(U, "get", lambda acc, wait=False: {"five": 1, "seven": 1, "fiveReset": "", "sevenReset": "", "error": ""}),
                  mock.patch.object(B.BR, "prepare", lambda *a, **k: None)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        self.env2.stop()
        super().tearDown()

    def calls(self, kind=None):
        if not self.log.exists():
            return []
        out = [json.loads(l) for l in self.log.read_text().splitlines()]
        return [c for c in out if kind is None or kind in c["argv"] or (c["argv"] and c["argv"][0] == kind)]

    def card(self, jid):
        return next(c for c in B.load_cards(True) if c["id"] == jid)

    def agent(self, jid, state="done", **st):
        self.job(jid, {A.VAR: str(self.claude)}, state=state, **st)


class Titles(AssistantEnv):
    def test_parse_claude_json(self):
        out = json.dumps({"type": "result", "result": "", "structured_output": {"title": "Wrap board cards.", "summary": "Make cards wrap"}})
        self.assertEqual(TI.parse(out), {"title": "Wrap board cards", "summary": "Make cards wrap."})

    def test_parse_result_text_and_garbage(self):
        out = json.dumps({"result": 'Sure: {"title": "add login", "summary": "Adds a login page."}'})
        self.assertEqual(TI.parse(out)["title"], "Add login")
        self.assertIsNone(TI.parse("oops"))
        self.assertIsNone(TI.parse(json.dumps({"is_error": True, "result": "x"})))

    def test_ask_model_uses_the_cards_account_and_haiku(self):
        got = TI.ask_model("checkout breaks on mobile", "shop", {"account": "work"})
        self.assertEqual(got["title"], "Fix the checkout bug")
        call = self.calls("-p")[-1]
        self.assertIn("haiku", call["argv"])
        self.assertIn("--no-session-persistence", call["argv"])
        self.assertEqual(call["login"], self.work_dir)

    def test_ask_model_failure_is_none(self):
        self.mode.write_text("fail")
        self.assertIsNone(TI.ask_model("anything", "x", None))

    def test_start_derives_then_describes(self):
        code, msg, jid, todo = B.create_task("checkout breaks on mobile. please look", str(self.folder), "work", start=True)
        self.assertEqual(code, 200, msg)
        self.assertEqual(todo["titleSource"], "auto")
        TI.after_launch(jid, todo, log=lambda m: None)
        for _ in range(100):
            if self.card(jid).get("summary"):
                break
            time.sleep(0.05)
        c = self.card(jid)
        self.assertEqual(c["name"], "Fix the checkout bug")
        self.assertEqual(c["summary"], "Fixes the checkout bug in the app.")
        self.assertEqual(c["account"], "work")

    def test_typed_title_is_kept(self):
        code, msg, jid, todo = B.create_task("checkout breaks", str(self.folder), title="My own title", start=True)
        C.meta_write(jid, title=todo["title"], titleSource=todo["titleSource"])
        r = TI.describe(self.card(jid))
        self.assertTrue(r["ok"])
        c = self.card(jid)
        self.assertEqual(c["name"], "My own title")
        self.assertEqual(c["summary"], "Fixes the checkout bug in the app.")
        # describe again without keepTitle replaces it only when asked
        TI.describe(self.card(jid), keep_title=False)
        self.assertEqual(self.card(jid)["name"], "Fix the checkout bug")

    def test_failure_keeps_title_and_says_so(self):
        self.agent("aaaa0001", intent="refactor the parser")
        self.mode.write_text("fail")
        r = TI.describe(self.card("aaaa0001"))
        self.assertFalse(r["ok"])
        self.assertEqual(self.card("aaaa0001")["name"], "aaaa0001")
        self.assertIsNone(self.card("aaaa0001")["summary"])

    def test_todo_summary_carries_into_the_agent(self):
        code, msg, _, todo = B.create_task("search is slow", str(self.folder))
        TI.describe({**todo, "kind": "todo", "name": todo["title"]})
        t = next(t for t in B.load_todos() if t["id"] == todo["id"])
        self.assertEqual((t["title"], t["summary"]), ("Fix the search bug", "Fixes the search bug in the app."))
        n = len(self.calls("-p"))
        jid, _ = B.launch_todo(t)
        TI.after_launch(jid, t, log=lambda m: None)
        self.assertEqual(len(self.calls("-p")), n)          # no second model call
        self.assertEqual(self.card(jid)["summary"], "Fixes the search bug in the app.")

    def test_rename_while_running_uses_the_sidecar(self):
        self.agent("aaaa0002", state="working")
        B.act_rename(self.card("aaaa0002"), "New name", "What it is for")
        c = self.card("aaaa0002")
        self.assertEqual((c["name"], c["summary"]), ("New name", "What it is for"))
        self.assertTrue((self.claude / "jobs" / "aaaa0002" / C.META).is_file())
        B.act_rename(c, None, "")       # clear the summary only
        self.assertIsNone(self.card("aaaa0002")["summary"])
        self.assertEqual(self.card("aaaa0002")["name"], "New name")


class Bulk(AssistantEnv):
    def setUp(self):
        super().setUp()
        self.agent("d0000001"); self.agent("d0000002")
        self.agent("w0000001", state="working")
        self.agent("n0000001", state="needs_input")
        self.agent("a0000001")
        C.meta_write("a0000001", assistant=True)
        B.save_todos([{"id": "t0000001", "title": "later", "account": "work", "cwd": "~/x", "prompt": ""}])

    def ids(self, action, filt, ids=None):
        return sorted(c["id"] for c in B.bulk_targets(B.load_cards(True), action, filt, ids))

    def test_targets(self):
        self.assertEqual(self.ids("archive", "done"), ["d0000001", "d0000002"])
        self.assertEqual(self.ids("archive", "idle"), ["d0000001", "d0000002", "n0000001"])
        self.assertEqual(self.ids("archive", "all"), ["d0000001", "d0000002", "n0000001", "w0000001"])
        self.assertEqual(self.ids("stop", "all"), ["w0000001"])           # nothing else has a worker
        self.assertEqual(self.ids("archive", "all", ["a0000001", "t0000001"]), ["t0000001"])   # never the assistant
        self.assertEqual(self.ids("stop", "done", ["t0000001", "d0000001"]), ["d0000001"])     # todos can't be stopped

    def test_archive_done(self):
        r = B.bulk("archive", "done")
        self.assertEqual((r["count"], sorted(r["ids"])), (2, ["d0000001", "d0000002"]))
        self.assertEqual(r["message"], "Archived 2 agents")
        left = {c["id"] for c in B.load_cards(True)}
        self.assertEqual(left, {"w0000001", "n0000001", "a0000001", "t0000001"})
        self.assertTrue(any((self.claude / "jobs-archive").glob("*/d0000001/state.json")))

    def test_archive_running_by_id_stops_it_first(self):
        r = B.bulk("archive", ids=["w0000001"])
        self.assertEqual(r["count"], 1)
        self.assertTrue(self.calls("stop"))

    def test_stop_all(self):
        r = B.bulk("stop", "all")
        self.assertEqual((r["count"], r["ids"]), (1, ["w0000001"]))
        self.assertEqual([c["argv"][:2] for c in self.calls("stop")], [["stop", "w0000001"]])

    def test_nothing_to_do(self):
        B.bulk("archive", "done")
        self.assertEqual(B.bulk("archive", "done")["message"], "Nothing done to archive")


class Server(AssistantEnv):
    def setUp(self):
        super().setUp()
        self.S = load_server()
        self.sent = []
        self.ps = [mock.patch.object(self.S, "send_to_agent", lambda c, t: self.sent.append((c["id"], t)) or "Sent")]
        for p in self.ps:
            p.start()

    def tearDown(self):
        for p in self.ps:
            p.stop()
        super().tearDown()

    def test_bulk_route(self):
        self.agent("d0000001")
        self.assertEqual(self.S.bulk_route({"action": "nuke"})[0], 400)
        self.assertEqual(self.S.bulk_route({"action": "archive", "filter": "old"})[0], 400)
        self.assertEqual(self.S.bulk_route({"action": "archive", "ids": "d0000001"})[0], 400)
        code, r = self.S.bulk_route({"action": "archive", "filter": "done"})
        self.assertEqual((code, r["count"], r["ids"]), (200, 1, ["d0000001"]))
        self.assertIn("message", r)

    def test_rename_route(self):
        self.agent("d0000001")
        self.assertEqual(self.S.act("d0000001", "rename", {"title": " "})[0], 400)
        self.assertEqual(self.S.act("d0000001", "rename", {})[0], 400)
        self.assertEqual(self.S.act("d0000001", "rename", {"summary": "Builds the thing"})[0], 200)
        self.assertEqual(self.S.act("d0000001", "rename", {"name": "Old style"})[0], 200)
        c = next(c for c in self.S.snapshot()["cards"] if c["id"] == "d0000001")
        self.assertEqual((c["name"], c["summary"], c["assistant"]), ("Old style", "Builds the thing", False))

    def test_describe_route(self):
        self.agent("d0000001", intent="the cart total is wrong")
        code, r = self.S.describe_route("d0000001", {})
        self.assertEqual((code, r["title"], r["summary"]), (200, "Fix the cart bug", "Fixes the cart bug in the app."))
        self.mode.write_text("fail")
        self.assertEqual(self.S.describe_route("d0000001", {"keepTitle": True})[0], 502)
        self.assertEqual(self.S.describe_route("nope", {})[0], 404)

    def test_create_start_describes_in_background(self):
        code, msg = self.S.act(None, "create", {"prompt": "invoices are blank", "cwd": str(self.folder), "start": True})
        self.assertEqual(code, 200, msg)
        jid = msg.split()[1]
        self.assertEqual(self.card(jid)["name"], "Invoices are blank")     # derived until the model answers
        for _ in range(100):
            if self.card(jid).get("summary"):
                break
            time.sleep(0.05)
        self.assertEqual(self.card(jid)["name"], "Fix the invoices bug")

    def test_assistant_flow(self):
        self.assertEqual(self.S.assistant_route("GET", ["api", "assistant"], {})[1],
                         {"id": None, "card": None, "account": "personal", "running": False})
        self.assertEqual(self.S.assistant_route("POST", ["api", "assistant"], {"message": " "})[0], 400)
        code, r = self.S.assistant_route("POST", ["api", "assistant"], {"message": "archive everything that's done"})
        self.assertEqual((code, r["message"]), (200, "Started"))
        jid = r["id"]
        bg = self.calls("--bg")[-1]
        loft = str(AS.loft_bin())
        self.assertIn(f"Bash({loft} *)", bg["argv"])
        self.assertEqual(bg["argv"][-1], "archive everything that's done")
        self.assertEqual(Path(bg["cwd"]), AS.home())
        self.assertEqual(bg["login"], str(self.claude))      # the default (personal) account
        self.assertIn(loft, (AS.home() / "CLAUDE.md").read_text())
        settings = json.loads(bg["argv"][bg["argv"].index("--settings") + 1])
        self.assertEqual(settings["env"]["LOFT_CLAUDE_HOME"], str(self.claude))
        # marked, and kept out of everything else
        snap = self.S.snapshot()
        mine = next(c for c in snap["cards"] if c["id"] == jid)
        self.assertTrue(mine["assistant"])
        self.assertEqual(mine["name"], "Loft Assistant")
        self.assertNotIn(jid, [j["id"] for j in C.load()])
        self.assertEqual(self.S.bulk_route({"action": "archive", "filter": "all"})[1]["ids"], [])
        info = self.S.assistant_route("GET", ["api", "assistant"], {})[1]
        json.dumps(info)                                      # the card is sent as JSON
        self.assertEqual((info["id"], info["running"]), (jid, True))
        # busy: queued behind the turn; free: typed in
        code, r = self.S.assistant_route("POST", ["api", "assistant"], {"message": "and stop the rest"})
        self.assertTrue(r["message"].startswith("Queued"))
        B.queue_remove(jid)
        sf = self.claude / "jobs" / jid / "state.json"
        st = json.loads(sf.read_text()); st["state"] = "done"; sf.write_text(json.dumps(st))
        code, r = self.S.assistant_route("POST", ["api", "assistant"], {"message": "what needs me?"})
        self.assertEqual((r["id"], r["message"]), (jid, "Sent"))
        self.assertEqual(self.sent, [(jid, "what needs me?")])
        # reset archives it; the next message starts a fresh session
        self.assertEqual(self.S.assistant_route("POST", ["api", "assistant", "reset"], {})[0], 200)
        self.assertFalse((self.claude / "jobs" / jid).exists())
        self.assertIsNone(self.S.assistant_route("GET", ["api", "assistant"], {})[1]["id"])
        code, r = self.S.assistant_route("POST", ["api", "assistant"], {"message": "hi"})
        self.assertNotEqual(r["id"], jid)

    def test_assistant_restarts_after_archive(self):
        code, r = self.S.assistant_route("POST", ["api", "assistant"], {"message": "hi"})
        B.act_archive({**self.card(r["id"]), "col": "done", "live": False})
        code, r2 = self.S.assistant_route("POST", ["api", "assistant"], {"message": "again"})
        self.assertEqual(r2["message"], "Started")
        self.assertNotEqual(r2["id"], r["id"])


class Cli(AssistantEnv):
    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(cli, "_describe_later", lambda *a, **k: None):
            try:
                code = cli.main(list(argv))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (print(e.code) or 1)
        return code, buf.getvalue()

    def test_list_json_has_summary_and_hides_the_assistant(self):
        self.agent("d0000001"); self.agent("a0000001")
        C.meta_write("d0000001", summary="Builds the thing", title="Thing")
        C.meta_write("a0000001", assistant=True)
        code, out = self.run_cli("list", "--json")
        jobs = json.loads(out)
        self.assertEqual([(j["id"], j["name"], j["summary"]) for j in jobs], [("d0000001", "Thing", "Builds the thing")])

    def test_archive_done_and_by_name(self):
        self.agent("d0000001", name="old docs"); self.agent("d0000002", name="other")
        self.agent("w0000001", state="working")
        code, out = self.run_cli("archive", "old docs")
        self.assertEqual((code, out.strip()), (0, "Archived 1 agent"))
        code, out = self.run_cli("archive", "--done")
        self.assertEqual(out.strip(), "Archived 1 agent")
        self.assertEqual([c["id"] for c in B.load_cards(True)], ["w0000001"])
        self.assertEqual(self.run_cli("archive")[0], 1)

    def test_stop_all(self):
        self.agent("w0000001", state="working"); self.agent("d0000001")
        code, out = self.run_cli("stop", "--all")
        self.assertEqual(out.strip(), "Stopped 1 agent")

    def test_new_rename_describe(self):
        code, out = self.run_cli("new", "the export button does nothing", "--dir", str(self.folder), "--account", "Work")
        self.assertEqual(code, 0, out)
        jid = out.split()[1]
        c = self.card(jid)
        self.assertEqual((c["name"], c["account"]), ("The export button does nothing", "work"))
        code, out = self.run_cli("describe", jid)
        self.assertIn("Fix the export bug — Fixes the export bug in the app.", out)
        code, out = self.run_cli("rename", jid, "--title", "Export button", "--summary", "Make export work")
        c = self.card(jid)
        self.assertEqual((c["name"], c["summary"]), ("Export button", "Make export work"))
        code, out = self.run_cli("new", "x", "--dir", str(self.tmp / "missing"))
        self.assertEqual(code, 1)

    def test_describe_missing(self):
        self.agent("d0000001", intent="login is slow"); self.agent("d0000002", intent="dark mode")
        C.meta_write("d0000002", summary="Already has one")
        code, out = self.run_cli("describe", "--missing")
        self.assertEqual(code, 0)
        self.assertIn("d0000001  Fix the login bug", out)
        self.assertNotIn("d0000002", out)

    def test_reply_queues_while_working(self):
        self.agent("w0000001", state="working")
        with mock.patch.object(cli, "server_up", lambda p: True):
            code, out = self.run_cli("reply", "w0000001", "also", "do", "this")
        self.assertIn("queued", out)
        self.assertEqual([i["text"] for i in B.queue_read("w0000001")["items"]], ["also do this"])
        self.agent("d0000001")
        from loft import agentio
        with mock.patch.object(agentio, "send_to_agent", lambda c, t: "Sent") as _:
            code, out = self.run_cli("reply", "d0000001", "thanks")
        self.assertEqual(out.strip(), "sent to d0000001")


if __name__ == "__main__":
    unittest.main()
