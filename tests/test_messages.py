"""Tests for the chat view of a session: loft/transcript.py and
GET /api/cards/<id>/messages. The fixture is a made-up todo-app session.

    python3 -m unittest tests.test_messages -v
"""
import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from loft import transcript as TR  # noqa: E402
from test_accounts import Env  # noqa: E402
from test_switch import load_server  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "chat_session.jsonl"


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.page = TR.read_page(FIX, limit=100)
        self.items = self.page["items"]

    def kinds(self):
        return [i["kind"] for i in self.items]

    def test_whole_session_shape(self):
        self.assertEqual(self.kinds(), [
            "user", "assistant", "tool", "result", "tool", "result", "tool", "result",
            "note", "note", "note", "user", "tool", "result", "assistant", "question"])
        self.assertFalse(self.page["more"])
        self.assertEqual(self.page["start"], 0)
        self.assertEqual(self.page["cursor"], FIX.stat().st_size)
        self.assertEqual(self.page["session"], "chat_session")

    def test_prompt_drops_system_reminder(self):
        self.assertEqual(self.items[0]["text"], "Fix the failing date test in the todo app and tell me what was wrong.")

    def test_noise_is_skipped(self):
        blob = json.dumps(self.items)
        for gone in ("Let me look at the tests", "subagent chatter", "Caveat:", "continued from a previous", "xxxxx"):
            self.assertNotIn(gone, blob)

    def test_tool_rows_are_compact(self):
        bash, read, edit = [i for i in self.items if i["kind"] == "tool"][:3]
        self.assertEqual((bash["name"], bash["title"], bash["detail"]), ("Bash", "Run the date tests", "npm test -- dates"))
        self.assertEqual((read["name"], read["title"]), ("Read", "dates.ts"))
        self.assertEqual((edit["name"], edit["title"], edit["detail"]), ("Edit", "dates.ts", "/home/dev/todo-app/src/dates.ts"))
        plain = [i for i in self.items if i["kind"] == "tool"][3]
        self.assertEqual((plain["title"], plain["detail"]), ("npm test", ""))   # no description: the command is the title

    def test_results_match_calls_and_collapse(self):
        res = {i["for"]: i for i in self.items if i["kind"] == "result"}
        fail = res["toolu_bash1"]
        self.assertFalse(fail["ok"])
        self.assertEqual(fail["lines"], 5)
        self.assertEqual(fail["preview"].count("\n"), TR.PREVIEW_LINES - 1)
        self.assertIn("1 failed", fail["text"])
        self.assertEqual(res["toolu_edit1"]["diff"], {"add": 3, "del": 1})
        self.assertTrue(res["toolu_bash2"]["ok"])

    def test_notes_and_commands(self):
        notes = [i["text"] for i in self.items if i["kind"] == "note"]
        self.assertEqual(notes, ["Earlier conversation compacted", "Interrupted", 'Background command "Watch the tests" completed (exit code 0)'])
        self.assertEqual(self.items[11]["text"], "/review dates.ts")

    def test_markdown_kept(self):
        md = [i for i in self.items if i["kind"] == "assistant"][-1]["text"]
        self.assertIn("**local time**", md)
        self.assertIn("```ts", md)

    def test_question_and_pending(self):
        q = self.items[-1]
        self.assertEqual(q["questions"][0]["options"][0]["label"], "Yes, add it")
        self.assertIs(TR.pending_options(self.items), q)
        answered = self.items + [{"kind": "result", "for": "toolu_ask1"}]
        self.assertIsNone(TR.pending_options(answered))

    def test_ids_are_stable_and_unique(self):
        ids = [i["id"] for i in self.items]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, [i["id"] for i in TR.read_page(FIX, limit=100)["items"]])

    def test_paging_back_joins_up(self):
        last = TR.read_page(FIX, limit=5)
        self.assertTrue(last["more"])
        older = TR.read_page(FIX, limit=100, before=last["start"])
        self.assertEqual([i["id"] for i in older["items"] + last["items"]], [i["id"] for i in self.items])
        self.assertEqual(TR.read_page(FIX, after=older["start"], limit=100)["items"], self.items)


class LiveFileTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = Path(tempfile.mkdtemp(prefix="loft-msg-"))
        self.f = self.dir / "sess.jsonl"
        lines = FIX.read_text().splitlines(keepends=True)
        self.f.write_text("".join(lines[:8]))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_after_cursor_returns_only_new(self):
        first = TR.read_page(self.f)
        with open(self.f, "a") as fh:
            fh.write(json.dumps({"type": "assistant", "uuid": "new-1", "timestamp": "t", "message": {"content": [{"type": "text", "text": "Still going."}]}}) + "\n")
        new = TR.read_page(self.f, after=first["cursor"])
        self.assertEqual([i["id"] for i in new["items"]], ["new-1:0"])
        self.assertEqual(TR.read_page(self.f, after=new["cursor"])["items"], [])

    def test_half_written_line_waits(self):
        first = TR.read_page(self.f)
        with open(self.f, "a") as fh:
            fh.write('{"type": "assistant", "uuid": "half"')
        again = TR.read_page(self.f, after=first["cursor"])
        self.assertEqual((again["items"], again["cursor"]), ([], first["cursor"]))
        with open(self.f, "a") as fh:
            fh.write(', "message": {"content": [{"type": "text", "text": "Done."}]}}\n')
        self.assertEqual([i["text"] for i in TR.read_page(self.f, after=first["cursor"])["items"]], ["Done."])

    def test_shrunk_file_resets(self):
        page = TR.read_page(self.f, after=10 ** 9)
        self.assertTrue(page["reset"])

    def test_big_file_reads_only_the_tail(self):
        big = self.dir / "big.jsonl"
        pad = json.dumps({"type": "api-request-blob", "message": {"content": "y" * 100_000}}) + "\n"
        with open(big, "w") as fh:
            for _ in range(60):   # ~6 MB of noise, then a short chat
                fh.write(pad)
            fh.write(FIX.read_text())
        reads = []
        real = open

        def spy(*a, **k):
            fh = real(*a, **k)
            orig = fh.read
            fh.read = lambda n=-1: (reads.append(n), orig(n))[1]
            return fh
        with mock.patch("builtins.open", spy):
            page = TR.read_page(big, limit=3)
        self.assertEqual([i["kind"] for i in page["items"]], ["tool", "result", "assistant", "question"][-len(page["items"]):])
        self.assertTrue(page["more"])
        self.assertLessEqual(sum(n for n in reads if n and n > 0), 2 * TR.CHUNK)   # not the 6 MB before it


class RouteTests(Env):
    def setUp(self):
        super().setUp()
        self.S = load_server()
        proj = self.claude / "projects" / "-home-dev-todo-app"
        proj.mkdir(parents=True)
        shutil.copy(FIX, proj / "5e55a0n0.jsonl")
        self.job("j1", sessionId="5e55a0n0", state="needs_input", detail="Should I also add a test for the end of the month?")
        self.job("j2", sessionId="5e55a0n0", state="working", detail="Running the tests")
        self.job("j3", state="done")

    def test_needs_you_has_pending_question(self):
        code, j = self.S.messages("j1", "limit=50")
        self.assertEqual(code, 200)
        self.assertEqual(j["state"], "needs")
        self.assertEqual(j["pending"]["text"], "Should I also add a test for the end of the month?")
        self.assertEqual(j["pending"]["question"]["tool"], "toolu_ask1")
        self.assertEqual(len(j["items"]), 16)

    def test_poll_after_cursor_still_finds_the_question(self):
        _, j = self.S.messages("j1", "")
        code, again = self.S.messages("j1", f"after={j['cursor']}&session={j['session']}")
        self.assertEqual((code, again["items"]), (200, []))
        self.assertEqual(again["pending"]["question"]["tool"], "toolu_ask1")

    def test_other_session_resets(self):
        _, j = self.S.messages("j2", "after=5&session=old-session")
        self.assertTrue(j["reset"])
        self.assertTrue(j["working"])
        self.assertIsNone(j["pending"])
        self.assertEqual(len(j["items"]), 16)

    def test_no_transcript_and_unknown(self):
        code, j = self.S.messages("j3", "")
        self.assertEqual((code, j["items"], j["session"]), (200, [], None))
        self.assertEqual(self.S.messages("nope", "")[0], 404)
        self.assertEqual(self.S.messages("../etc", "")[0], 404)

    def test_phone_listener_allows_it(self):
        self.assertFalse("/api/cards/j1/messages".startswith(self.S.RemoteH.BLOCKED))


if __name__ == "__main__":
    unittest.main()
