"""Tests for the per-agent account switch: queued (after this turn) switches, the
stop-and-verify retry when the daemon respawns a worker from its cached env, the
CLI and the POST /api/cards/<id>/account route's validation.

Everything runs in temp dirs; `claude stop` and the daemon roster are faked.

    python3 -m unittest tests.test_switch -v
"""
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from loft import accounts as A, board as B, core as C, cli  # noqa: E402
from test_accounts import Env  # noqa: E402

HERE = Path(__file__).resolve().parent.parent


def load_server():
    spec = importlib.util.spec_from_file_location("loft_server_under_test", HERE / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Roster:
    """A fake daemon roster.json plus a fake `claude stop` that scripts what the
    daemon does after each stop."""

    def __init__(self, claude_dir):
        self.f = claude_dir / "daemon" / "roster.json"
        self.f.parent.mkdir(parents=True, exist_ok=True)
        self.workers = {}
        self.after_stop = []      # one callable per stop; the last one repeats
        self.stops = 0
        self.save()

    def save(self):
        self.f.write_text(json.dumps({"supervisorPid": 1, "workers": self.workers}))

    def worker(self, jid, pid, login):
        self.workers[jid] = {"pid": pid, "dispatch": {"env": {A.VAR: login}}}
        self.save()

    def gone(self, jid):
        self.workers.pop(jid, None)
        self.save()

    def claude(self, *args, card=None, timeout=60):
        assert args[0] == "stop"
        self.stops += 1
        step = self.after_stop[min(self.stops, len(self.after_stop)) - 1]
        step(args[1])
        return mock.Mock(stdout="", stderr="", returncode=0)


class SwitchEnv(Env):
    def setUp(self):
        super().setUp()
        self.cfg = self.cfg3()
        self.personal = str(self.claude)
        self.roster = Roster(self.claude)
        self.p = [mock.patch.object(B, "claude", self.roster.claude),
                  mock.patch.object(C, "pid_alive", lambda pid: pid is not None and pid > 0),
                  mock.patch.object(B.time, "sleep", lambda s: None)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        super().tearDown()

    def card(self, jid):
        with mock.patch.object(C, "live_sessions", side_effect=lambda: {k for k in self.roster.workers}):
            return next(c for c in B.load_cards(True) if c["id"] == jid)

    def login(self, sf):
        return json.loads(sf.read_text())["providerEnv"][A.VAR]


class PendingSwitch(SwitchEnv):
    def test_running_agent_queues_then_applies_after_turn(self):
        sf = self.job("aaaa0001", {A.VAR: self.personal}, state="working")
        r = B.switch_account(self.card("aaaa0001"), "work")
        self.assertTrue(r["ok"]); self.assertEqual(r["pending"], "work")
        self.assertEqual(self.login(sf), self.personal)               # untouched while it works
        self.assertEqual(self.card("aaaa0001")["pending"], {"account": "work", "error": ""})
        self.assertEqual(B.apply_pending(log=lambda m: None), [])     # still working: nothing yet
        st = json.loads(sf.read_text()); st["state"] = "done"; sf.write_text(json.dumps(st))
        done = B.apply_pending(log=lambda m: None)
        self.assertEqual([j for j, _ in done], ["aaaa0001"])
        self.assertEqual(self.login(sf), self.work_dir)
        self.assertEqual(json.loads(sf.read_text())["color"], "blue")
        self.assertIsNone(B.pending_read("aaaa0001"))
        self.assertEqual(self.roster.stops, 0)                         # no live worker, nothing to stop

    def test_picking_current_account_cancels_queue(self):
        self.job("aaaa0002", {A.VAR: self.personal}, state="working")
        B.switch_account(self.card("aaaa0002"), "side")
        r = B.switch_account(self.card("aaaa0002"), "personal")
        self.assertTrue(r["ok"]); self.assertIn("Cancelled", r["message"])
        self.assertIsNone(B.pending_read("aaaa0002"))

    def test_now_stops_the_turn(self):
        sf = self.job("aaaa0003", {A.VAR: self.personal}, state="working")
        self.roster.worker("aaaa0003", 101, self.personal)
        self.roster.after_stop = [self.roster.gone]
        r = B.switch_account(self.card("aaaa0003"), "work", "now")
        self.assertTrue(r["ok"], r["message"]); self.assertIsNone(r["pending"])
        self.assertEqual(self.login(sf), self.work_dir)
        self.assertEqual(self.roster.stops, 1)

    def test_todo_just_changes(self):
        B.save_todos([{"id": "t1", "title": "x", "account": "personal", "cwd": "~", "prompt": ""}])
        r = B.switch_account(self.card("t1"), "side", "now")
        self.assertTrue(r["ok"]); self.assertEqual(B.load_todos()[0]["account"], "side")

    def test_queued_switch_for_archived_agent_is_dropped(self):
        self.job("aaaa0004", {A.VAR: self.personal}, state="working")
        B.pending_write("aaaa0004", {"account": "nope", "tries": 0})
        st = self.claude / "jobs" / "aaaa0004" / "state.json"
        st.write_text(json.dumps({**json.loads(st.read_text()), "state": "done"}))
        B.apply_pending(log=lambda m: None)
        self.assertIsNone(B.pending_read("aaaa0004"))   # unknown account: dropped


class CachedRespawn(SwitchEnv):
    def test_idle_worker_stopped_and_gone(self):
        sf = self.job("bbbb0001", {A.VAR: self.personal})
        self.roster.worker("bbbb0001", 201, self.personal)
        self.roster.after_stop = [self.roster.gone]
        r = B.switch_account(self.card("bbbb0001"), "work")
        self.assertTrue(r["ok"], r["message"])
        self.assertIn("continues there on your next reply", r["message"])
        self.assertEqual((self.login(sf), self.roster.stops), (self.work_dir, 1))

    def test_respawn_on_cached_old_env_is_stopped_again(self):
        self.job("bbbb0002", {A.VAR: self.personal})
        self.roster.worker("bbbb0002", 301, self.personal)
        self.roster.after_stop = [lambda j: self.roster.worker(j, 302, self.personal),   # daemon claims a spare, old env
                                  self.roster.gone]
        r = B.switch_account(self.card("bbbb0002"), "work")
        self.assertTrue(r["ok"], r["message"])
        self.assertEqual(self.roster.stops, 2)

    def test_respawn_on_new_env_is_fine(self):
        self.job("bbbb0003", {A.VAR: self.personal})
        self.roster.worker("bbbb0003", 401, self.personal)
        self.roster.after_stop = [lambda j: self.roster.worker(j, 402, self.work_dir)]
        r = B.switch_account(self.card("bbbb0003"), "work")
        self.assertTrue(r["ok"], r["message"])
        self.assertEqual(self.roster.stops, 1)

    def test_gives_up_and_says_so(self):
        sf = self.job("bbbb0004", {A.VAR: self.personal})
        self.roster.worker("bbbb0004", 500, self.personal)
        n = iter(range(501, 600))
        self.roster.after_stop = [lambda j: self.roster.worker(j, next(n), self.personal)]
        r = B.switch_account(self.card("bbbb0004"), "work")
        self.assertFalse(r["ok"])
        self.assertIn("keeps restarting on the old login", r["message"])
        self.assertEqual(self.roster.stops, B.STOP_TRIES)
        self.assertEqual(self.login(sf), self.work_dir)              # saved anyway: next cold start uses it

    def test_queued_switch_keeps_error_and_retries_stop(self):
        sf = self.job("bbbb0005", {A.VAR: self.personal}, state="done")
        B.pending_write("bbbb0005", {"account": "work", "tries": 0})
        self.roster.worker("bbbb0005", 600, self.personal)
        n = iter(range(601, 700))
        self.roster.after_stop = [lambda j: self.roster.worker(j, next(n), self.personal)]
        B.apply_pending(log=lambda m: None)
        p = B.pending_read("bbbb0005")
        self.assertEqual(p["tries"], 1); self.assertIn("old login", p["error"])
        self.assertEqual(self.login(sf), self.work_dir)
        # next poll: the daemon lets go this time -> the queued switch completes
        self.roster.after_stop = [self.roster.gone]; self.roster.stops = 0
        B.apply_pending(log=lambda m: None)
        self.assertIsNone(B.pending_read("bbbb0005"))

    def test_worker_that_wont_stop(self):
        self.job("bbbb0006", {A.VAR: self.personal})
        self.roster.worker("bbbb0006", 700, self.personal)
        self.roster.after_stop = [lambda j: None]
        r = B.switch_account(self.card("bbbb0006"), "work")
        self.assertFalse(r["ok"]); self.assertIn("couldn't stop", r["message"])


class Route(SwitchEnv):
    @classmethod
    def setUpClass(cls):
        cls.S = load_server()

    def test_validation(self):
        self.job("cccc0001", {A.VAR: self.personal}, state="working")
        card = self.card("cccc0001")
        self.assertEqual(self.S.switch_route(card, {"account": "nope"})[0], 400)
        self.assertEqual(self.S.switch_route(card, {"account": 5})[0], 400)
        self.assertEqual(self.S.switch_route(card, {"account": "work", "when": "later"})[0], 400)
        code, msg = self.S.switch_route(card, {"account": "work"})
        self.assertEqual(code, 200); self.assertIn("after this turn", msg)
        self.assertEqual(self.S.switch_route(card, {"cancel": True}), (200, "Switch cancelled"))
        self.assertEqual(self.S.switch_route(card, {"cancel": True}), (200, "Nothing was queued"))
        B.save_todos([{"id": "t2", "title": "x", "account": "personal", "cwd": "~", "prompt": ""}])
        self.assertEqual(self.S.switch_route(self.card("t2"), {"cancel": True})[0], 400)

    def test_failed_stop_is_409(self):
        self.job("cccc0002", {A.VAR: self.personal})
        self.roster.worker("cccc0002", 800, self.personal)
        self.roster.after_stop = [lambda j: None]
        self.assertEqual(self.S.switch_route(self.card("cccc0002"), {"account": "work"})[0], 409)

    def test_phone_listener_reaches_the_route(self):
        # The tailnet handler only blocks pairing/admin/static paths; card actions go through act().
        self.assertFalse(any("/api/cards".startswith(b) for b in self.S.RemoteH.BLOCKED))


class Cli(SwitchEnv):
    def test_switch_by_name_and_label(self):
        sf = self.job("dddd0001", {A.VAR: self.personal}, name="Scrape forums")
        out = []
        with mock.patch.object(cli, "say", out.append), mock.patch.object(cli, "server_up", return_value=True), \
                mock.patch.object(C, "live_sessions", return_value=set()):
            self.assertEqual(cli.main(["switch", "scrape", "Side"]), 0)
        self.assertEqual(self.login(sf), str(self.tmp / "logins" / "side"))
        self.assertIn("Now bills Side", out[-1])

    def test_switch_running_queues_and_now(self):
        sf = self.job("dddd0002", {A.VAR: self.personal}, state="working")
        with mock.patch.object(cli, "say", lambda *a: None), mock.patch.object(cli, "server_up", return_value=True), \
                mock.patch.object(C, "live_sessions", return_value=set()):
            cli.main(["switch", "dddd0002", "work"])
            self.assertEqual(B.pending_read("dddd0002")["account"], "work")
            cli.main(["switch", "dddd0002", "--cancel"])
            self.assertIsNone(B.pending_read("dddd0002"))
            cli.main(["switch", "dddd0002", "work", "--now"])
        self.assertEqual(self.login(sf), self.work_dir)

    def test_unknown_account(self):
        self.job("dddd0003", {A.VAR: self.personal})
        with mock.patch.object(C, "live_sessions", return_value=set()), self.assertRaises(SystemExit):
            cli.main(["switch", "dddd0003", "nobody"])


if __name__ == "__main__":
    unittest.main()
