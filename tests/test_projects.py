"""Tests for projects: config CRUD, card -> project resolution, account pick for new
tasks (incl. auto by lowest 5h usage), the snapshot fields and the HTTP routes.

    python3 -m unittest tests.test_projects -v

Everything runs against temp dirs. The route tests start server.py on a spare
port (LOFT_TEST_FLEX_PORT, default 7741) with a throwaway config and jobs dir.
"""
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from loft import accounts as A, board as B, paths, projects as P  # noqa: E402

PORT = int(os.environ.get("LOFT_TEST_FLEX_PORT", 7741))


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loft-proj-"))
        self.claude = self.tmp / "claude"
        (self.claude / "jobs").mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {
            "LOFT_CONF_DIR": str(self.tmp / "conf"), "XDG_DATA_HOME": str(self.tmp / "data"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"), "LOFT_CLAUDE_HOME": str(self.claude), "USER": "tester"})
        self.env.start()
        self.plat = mock.patch.object(paths, "PLATFORM", "linux")
        self.plat.start()
        A.save({"default": "a1", "accounts": [
            {"id": "a1", "label": "One", "color": "green", "creds_dir": "default"},
            {"id": "a2", "label": "Two", "color": "blue", "creds_dir": str(self.tmp / "l2")},
            {"id": "a3", "label": "Three", "color": "purple", "creds_dir": str(self.tmp / "l3")}]})

    def tearDown(self):
        self.plat.stop()
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def job(self, jid, cwd, **st):
        d = self.claude / "jobs" / jid
        d.mkdir(parents=True)
        (d / "state.json").write_text(json.dumps({"name": jid, "state": "done", "cwd": cwd, **st}))


class Config(Env):
    def test_missing_file_is_empty_default(self):
        cfg = P.load()
        self.assertEqual(cfg["projects"], [])
        self.assertEqual(cfg["view"]["groupBy"], "status")
        self.assertFalse(P.config_path().exists())

    def test_create_update_remove(self):
        with P.locked() as cfg:
            p = P.create({"name": "Acme Corp", "folders": ["/srv/acme/"], "accounts": ["a2", "nope"], "defaultAccount": "a2"}, cfg)
        self.assertEqual(p["id"], "acme-corp")
        cfg = P.load()
        q = P.get("acme-corp", cfg)
        self.assertEqual(q["folders"], ["/srv/acme"])
        self.assertEqual(q["accounts"], ["a2"])            # unknown account dropped
        self.assertEqual(q["defaultAccount"], "a2")
        self.assertIn(q["color"], A.COLORS)
        self.assertEqual(oct(P.config_path().stat().st_mode & 0o777), "0o600")
        with P.locked() as cfg:
            P.update("acme-corp", {"name": "Acme", "defaultAccount": "auto", "color": "orange"}, cfg)
            P.assign("job1", "acme-corp", cfg)
        cfg = P.load()
        self.assertEqual(P.get("acme-corp", cfg)["name"], "Acme")
        self.assertEqual(P.get("acme-corp", cfg)["defaultAccount"], "auto")
        self.assertEqual(cfg["assignments"], {"job1": "acme-corp"})
        with P.locked() as cfg:
            self.assertTrue(P.remove("acme-corp", cfg))
        cfg = P.load()
        self.assertEqual(cfg["projects"], [])
        self.assertEqual(cfg["assignments"], {})          # assignments to it go too

    def test_validation(self):
        cfg = P.load()
        P.create({"name": "X"}, cfg)
        with self.assertRaises(ValueError):
            P.create({"name": "x"}, cfg)                   # duplicate name, any case
        with self.assertRaises(ValueError):
            P.create({"name": "  "}, cfg)
        with self.assertRaises(ValueError):
            P.update("x", {"color": "mauve"}, cfg)
        with self.assertRaises(KeyError):
            P.update("missing", {"name": "Y"}, cfg)
        with self.assertRaises(KeyError):
            P.assign("j", "missing", cfg)

    def test_colors_palette_or_hex(self):
        cfg = P.load()
        p = P.create({"name": "Hex", "color": "#12abEF"}, cfg)
        self.assertEqual(P.public(p)["hex"], "#12ABEF")
        self.assertEqual(P.public(P.create({"name": "Pal", "color": "pink"}, cfg))["hex"], A.COLORS["pink"])
        with self.assertRaises(ValueError):
            P.create({"name": "Bad", "color": "#12ab"}, cfg)
        self.assertEqual(P.public(p, {})["count"], 0)

    def test_default_outside_allowed_is_dropped(self):
        cfg = P.save({"projects": [{"id": "p", "name": "P", "accounts": ["a2"], "defaultAccount": "a3"}]})
        self.assertIsNone(cfg["projects"][0]["defaultAccount"])

    def test_bad_group_by_falls_back(self):
        self.assertEqual(P.save({"view": {"groupBy": "planet"}})["view"]["groupBy"], "status")

    def test_lock_serializes_writers(self):
        def add(i):
            with P.locked() as cfg:
                P.create({"name": f"p{i}"}, cfg)
        ts = [threading.Thread(target=add, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(P.load()["projects"]), 8)


class Resolution(Env):
    def setUp(self):
        super().setUp()
        with P.locked() as cfg:
            P.create({"name": "Mono", "folders": ["/code/mono"]}, cfg)
            P.create({"name": "Web", "folders": ["/code/mono/web"]}, cfg)
            P.create({"name": "Other", "folders": ["/code/monolith-not"]}, cfg)

    def test_longest_folder_wins(self):
        cfg = P.load()
        self.assertEqual(P.project_of("j", "/code/mono/web/src", cfg), "web")
        self.assertEqual(P.project_of("j", "/code/mono/api", cfg), "mono")
        self.assertEqual(P.project_of("j", "/code/mono", cfg), "mono")
        self.assertEqual(P.project_of("j", "/code/mono/.claude/worktrees/x", cfg), "mono")

    def test_prefix_is_not_a_match(self):
        self.assertIsNone(P.project_of("j", "/code/monorepo", P.load()))
        self.assertIsNone(P.project_of("j", "/elsewhere", P.load()))
        self.assertIsNone(P.project_of("j", "", P.load()))

    def test_manual_beats_folder(self):
        with P.locked() as cfg:
            P.assign("j", "other", cfg)
        self.assertEqual(P.project_of("j", "/code/mono/web", P.load()), "other")

    def test_cards_carry_project_and_folder(self):
        self.job("aaaa1111", "/code/mono/web")
        self.job("bbbb2222", "/tmp/elsewhere")
        cards = {c["id"]: c for c in B.load_cards(show_all_done=True)}
        self.assertEqual(cards["aaaa1111"]["project"], "web")
        self.assertEqual(cards["aaaa1111"]["folder"], "/code/mono/web")
        self.assertEqual(cards["aaaa1111"]["folderName"], "web")
        self.assertIsNone(cards["bbbb2222"]["project"])
        counts = P.count(cards.values())
        self.assertEqual(counts["web"]["total"], 1)
        self.assertEqual(counts[None]["done"], 1)

    def test_todo_keeps_its_project(self):
        B.save_todos([{"id": "t1", "title": "x", "cwd": "/tmp", "project": "mono", "account": "a1"}])
        card = next(c for c in B.load_cards(True) if c["id"] == "t1")
        self.assertEqual(card["project"], "mono")


class AccountPick(Env):
    def usage(self, table):
        return lambda acc: {"five": table.get(acc["id"])}

    def test_auto_picks_lowest_five_hour(self):
        acc_cfg = A.load()
        proj = {"id": "p", "accounts": ["a2", "a3"], "defaultAccount": "auto"}
        got = P.pick_account(None, proj, acc_cfg, self.usage({"a1": 1, "a2": 70, "a3": 12}))
        self.assertEqual(got["id"], "a3")                  # a1 is lower but not allowed

    def test_auto_unknown_usage_goes_last(self):
        acc_cfg = A.load()
        proj = {"id": "p", "accounts": [], "defaultAccount": "auto"}
        got = P.pick_account(None, proj, acc_cfg, self.usage({"a1": None, "a2": "88", "a3": None}))
        self.assertEqual(got["id"], "a2")
        got = P.pick_account(None, proj, acc_cfg, self.usage({}))
        self.assertEqual(got["id"], "a1")                  # nothing known: config order

    def test_explicit_wins(self):
        acc_cfg = A.load()
        proj = {"id": "p", "accounts": ["a2"], "defaultAccount": "a2"}
        self.assertEqual(P.pick_account("a3", proj, acc_cfg, self.usage({}))["id"], "a3")
        self.assertEqual(P.pick_account("auto", None, acc_cfg, self.usage({"a1": 50, "a2": 5, "a3": 9}))["id"], "a2")

    def test_project_default_then_global(self):
        acc_cfg = A.load()
        self.assertEqual(P.pick_account(None, {"id": "p", "accounts": [], "defaultAccount": "a2"}, acc_cfg, self.usage({}))["id"], "a2")
        self.assertEqual(P.pick_account(None, None, acc_cfg, self.usage({}))["id"], "a1")
        # global default not allowed by the project: first allowed account
        self.assertEqual(P.pick_account(None, {"id": "p", "accounts": ["a3"], "defaultAccount": None}, acc_cfg, self.usage({}))["id"], "a3")


def _free(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        return False
    except OSError:
        return True


@unittest.skipUnless(_free(PORT), f"port {PORT} busy")
class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="loft-proj-srv-"))
        claude = cls.tmp / "claude"
        (claude / "jobs" / "cccc3333").mkdir(parents=True)
        cls.repo = cls.tmp / "code" / "shop"
        cls.repo.mkdir(parents=True)
        (claude / "jobs" / "cccc3333" / "state.json").write_text(json.dumps(
            {"name": "fix cart", "state": "done", "cwd": str(cls.repo / "api")}))
        conf = cls.tmp / "conf"
        conf.mkdir()
        (conf / "accounts.json").write_text(json.dumps({"default": "a1", "accounts": [
            {"id": "a1", "label": "One", "color": "green", "creds_dir": "default"},
            {"id": "a2", "label": "Two", "color": "blue", "creds_dir": str(cls.tmp / "l2")}]}))
        env = {**os.environ, "LOFT_CONF_DIR": str(conf), "LOFT_CLAUDE_HOME": str(claude),
               "XDG_CACHE_HOME": str(cls.tmp / "cache"), "XDG_DATA_HOME": str(cls.tmp / "data"),
               "LOFT_CLAUDE": "/bin/false"}
        cls.proc = subprocess.Popen([sys.executable, str(HERE / "server.py"), "--port", str(PORT), "--no-tailnet"],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import time
        for _ in range(100):
            if not _free(PORT):
                break
            time.sleep(0.1)
        _, html = cls.req("GET", "/", raw=True)
        cls.token = re.search(rb'const TOKEN = "([0-9a-f]+)"', html).group(1).decode()

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def req(cls, method, path, body=None, raw=False, token=True):
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        h = {"Content-Type": "application/json"}
        if token and getattr(cls, "token", None):
            h["X-Cagents-Token"] = cls.token
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, (data if raw else json.loads(data or b"{}"))

    def test_flow(self):
        st, _ = self.req("POST", "/api/projects", {"name": "Shop"}, token=False)
        self.assertEqual(st, 403)                          # token-protected like the rest
        st, snap = self.req("GET", "/api/state")
        self.assertEqual(snap["projects"], [])
        self.assertEqual(snap["groupBy"], "status")
        self.assertFalse(snap["organized"])
        card = next(c for c in snap["cards"] if c["id"] == "cccc3333")
        self.assertIsNone(card["project"])
        for k in ("cwd", "account", "folder", "folderName", "col", "name"):
            self.assertIn(k, card)

        st, r = self.req("POST", "/api/projects", {"name": "Shop", "folders": [str(self.repo)], "accounts": ["a1", "a2"], "defaultAccount": "auto"})
        self.assertEqual(st, 200, r)
        pid = r["project"]["id"]
        self.assertEqual(r["id"], pid)                      # the project object itself, plus {project: ...}
        st, snap = self.req("GET", "/api/state")
        self.assertTrue(snap["organized"])
        self.assertEqual(next(c for c in snap["cards"] if c["id"] == "cccc3333")["project"], pid)
        self.assertEqual(snap["projects"][0]["counts"]["total"], 1)
        self.assertEqual(snap["projects"][0]["count"], 1)

        st, r = self.req("PATCH", f"/api/projects/{pid}", {"color": "orange", "name": "Shop Inc"})
        self.assertEqual((st, r["project"]["color"], r["project"]["name"]), (200, "orange", "Shop Inc"))
        self.assertEqual(self.req("PATCH", "/api/projects/nope", {"name": "x"})[0], 404)
        self.assertEqual(self.req("POST", "/api/projects", {"name": "shop inc"})[0], 400)

        st, r = self.req("POST", "/api/projects", {"name": "Side"})
        side = r["project"]["id"]
        st, r = self.req("POST", "/api/cards/cccc3333/project", {"project": side})
        self.assertEqual(st, 200, r)
        snap = self.req("GET", "/api/state")[1]
        self.assertEqual(next(c for c in snap["cards"] if c["id"] == "cccc3333")["project"], side)
        self.req("POST", "/api/cards/cccc3333/project", {"project": None})
        snap = self.req("GET", "/api/state")[1]
        self.assertEqual(next(c for c in snap["cards"] if c["id"] == "cccc3333")["project"], pid)

        # create a todo for the project: folder from the project, account by the auto rule
        st, r = self.req("POST", "/api/cards", {"prompt": "add a coupon field", "project": pid})
        self.assertEqual(st, 200, r)
        snap = self.req("GET", "/api/state")[1]
        todo = next(c for c in snap["cards"] if c["kind"] == "todo")
        self.assertEqual(todo["project"], pid)
        self.assertEqual(todo["cwd"], str(self.repo).replace(str(Path.home()), "~"))
        self.assertIn(todo["account"], ("a1", "a2"))
        st, r = self.req("POST", "/api/cards", {"prompt": "explicit auto", "project": pid, "account": "auto"})
        self.assertEqual(st, 200, r)
        st, r = self.req("POST", "/api/cards", {"prompt": "x", "project": "gone"})
        self.assertEqual(st, 400)

        self.assertEqual(self.req("POST", "/api/view", {"groupBy": "project"})[0], 200)
        self.assertEqual(self.req("GET", "/api/state")[1]["groupBy"], "project")
        self.assertEqual(self.req("POST", "/api/view", {"groupBy": "moon"})[0], 400)

        st, r = self.req("POST", "/api/projects/from-folders", {"folders": [str(self.repo), str(self.tmp / "code" / "blog")]})
        self.assertEqual(r["created"], ["blog"])             # the shop folder already has a project

        self.assertEqual(self.req("DELETE", f"/api/projects/{side}")[0], 200)
        self.assertEqual(self.req("DELETE", f"/api/projects/{side}")[0], 404)


if __name__ == "__main__":
    unittest.main()
