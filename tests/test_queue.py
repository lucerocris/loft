"""Tests for the prompt queue: prompts wait while the agent works or its account is at
its 5-hour / 7-day limit, then go out one at a time; todos started at a limit start
after the reset. Usage and sending are faked; everything runs in temp dirs.

    python3 -m unittest tests.test_queue -v
"""
import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from loft import accounts as A, board as B, core as C, usage as U  # noqa: E402
from test_accounts import Env  # noqa: E402
from test_switch import load_server  # noqa: E402


class QueueEnv(Env):
    def setUp(self):
        super().setUp()
        self.cfg3()
        self.now = B.now_utc()
        self.usage = {}      # account id -> (five, seven, fiveReset, sevenReset)
        self.p = [mock.patch.object(U, "get", self.fake_usage),
                  mock.patch.object(C, "live_sessions", lambda: set())]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        super().tearDown()

    def fake_usage(self, acc, wait=False):
        f, s, fr, sr = self.usage.get(acc["id"], (10, 10, "", ""))
        return {"five": f, "seven": s, "fiveReset": fr, "sevenReset": sr, "error": ""}

    def at(self, minutes):
        return (self.now + timedelta(minutes=minutes)).isoformat()

    def agent(self, jid, state="done"):
        self.job(jid, {A.VAR: str(self.claude)}, state=state)   # the default (personal) login

    def set_state(self, jid, state):
        sf = self.claude / "jobs" / jid / "state.json"
        st = json.loads(sf.read_text()); st["state"] = state; sf.write_text(json.dumps(st))


class LimitUntil(QueueEnv):
    def test_under_limit_is_free(self):
        self.usage["personal"] = (99, 50, self.at(30), self.at(900))
        self.assertIsNone(B.limit_until("personal", now=self.now))

    def test_five_hour_limit_gives_its_reset(self):
        self.usage["personal"] = (100, 50, self.at(30), self.at(900))
        self.assertEqual(B.limit_until("personal", now=self.now), self.at(30))

    def test_both_limits_wait_for_the_later_reset(self):
        self.usage["personal"] = (100, 100, self.at(30), self.at(900))
        self.assertEqual(B.limit_until("personal", now=self.now), self.at(900))

    def test_reset_already_past_counts_as_free(self):
        self.usage["personal"] = (100, 50, self.at(-1), "")
        self.assertIsNone(B.limit_until("personal", now=self.now))

    def test_unknown_reset_is_empty_string(self):
        self.usage["personal"] = (100, 50, "", "")
        self.assertEqual(B.limit_until("personal", now=self.now), "")


class QueueDue(QueueEnv):
    def test_waits_for_turn_then_sends_one_at_a_time(self):
        self.agent("aaaa0001", state="working")
        B.queue_add("aaaa0001", "first"); B.queue_add("aaaa0001", "second")
        self.assertEqual(B.queue_due(now=self.now), [])                  # still working
        self.set_state("aaaa0001", "done")
        due = B.queue_due(now=self.now)
        self.assertEqual([(k, i["text"]) for k, _, i in due], [("send", "first")])
        self.assertEqual(B.queue_due(now=self.now + timedelta(seconds=5)), [])   # inside the gap
        due = B.queue_due(now=self.now + timedelta(seconds=B.QUEUE_GAP + 1))
        self.assertEqual([i["text"] for _, _, i in due], ["second"])
        self.assertFalse(B.queue_pending())

    def test_holds_at_limit_and_sends_after_reset(self):
        self.agent("aaaa0002")
        self.usage["personal"] = (100, 40, self.at(30), self.at(900))
        B.queue_add("aaaa0002", "go on")
        self.assertEqual(B.queue_due(now=self.now), [])
        card = next(c for c in B.load_cards(True) if c["id"] == "aaaa0002")
        self.assertEqual(card["queue"]["until"][:16], self.at(30)[:16])
        self.assertEqual(len(B.queue_due(now=self.now + timedelta(minutes=31))), 1)

    def test_failed_send_goes_back_to_the_front_then_sticks(self):
        self.agent("aaaa0003")
        B.queue_add("aaaa0003", "a"); B.queue_add("aaaa0003", "b")
        t = self.now
        for n in range(B.QUEUE_TRIES):
            t += timedelta(seconds=B.QUEUE_GAP + 1)
            (kind, card, item), = B.queue_due(now=t)
            self.assertEqual(item["text"], "a")
            B.queue_failed(kind, card, item, "boom")
        self.assertEqual(B.queue_due(now=t + timedelta(seconds=B.QUEUE_GAP + 1)), [])   # stuck: shows its error
        self.assertEqual([i["text"] for i in B.queue_read("aaaa0003")["items"]], ["a", "b"])
        self.assertEqual(B.queue_read("aaaa0003")["items"][0]["error"], "boom")

    def test_queued_todo_starts_after_reset(self):
        B.save_todos([{"id": "t1", "title": "x", "account": "personal", "cwd": "/tmp", "prompt": "do", "queued": True}])
        self.usage["personal"] = (100, 40, self.at(30), "")
        self.assertEqual(B.queue_due(now=self.now), [])
        due = B.queue_due(now=self.now + timedelta(minutes=31))
        self.assertEqual([(k, t["id"]) for k, t, _ in due], [("start", "t1")])
        self.assertEqual(B.load_todos(), [])


class ReplyRoute(QueueEnv):
    def setUp(self):
        super().setUp()
        self.srv = load_server()
        self.sent = []
        self.p2 = [mock.patch.object(self.srv, "send_to_agent", lambda c, t: self.sent.append(t) or "Sent"),
                   mock.patch.object(self.srv.Awake, "hold", lambda on: None)]
        for p in self.p2:
            p.start()

    def tearDown(self):
        for p in self.p2:
            p.stop()
        super().tearDown()

    def test_idle_agent_gets_it_now(self):
        self.agent("bbbb0001", state="blocked")
        self.assertEqual(self.srv.act("bbbb0001", "reply", {"text": "hi"}), (200, "Sent"))
        self.assertEqual(self.sent, ["hi"])

    def test_working_agent_queues(self):
        self.agent("bbbb0002", state="working")
        code, msg = self.srv.act("bbbb0002", "reply", {"text": "next"})
        self.assertEqual((code, msg), (200, "Queued: sends after this turn"))
        self.assertEqual(self.sent, [])

    def test_limited_agent_queues_until_reset(self):
        self.agent("bbbb0003", state="blocked")
        self.usage["personal"] = (100, 40, self.at(30), "")
        code, msg = self.srv.act("bbbb0003", "reply", {"text": "later"})
        self.assertIn("sends when the limit resets at", msg)
        self.assertEqual(self.sent, [])

    def test_remove_one_queued_prompt(self):
        self.agent("bbbb0004", state="working")
        self.srv.act("bbbb0004", "reply", {"text": "x"})
        item = B.queue_read("bbbb0004")["items"][0]["id"]
        self.assertEqual(self.srv.act("bbbb0004", "queue", {"item": item}), (200, "Removed"))
        self.assertFalse(B.queue_pending())


if __name__ == "__main__":
    unittest.main()
