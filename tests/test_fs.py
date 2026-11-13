"""Tests for the folder picker: loft/fs.py (listing, home-only, symlinks, hidden, cap,
recent) and the GET /api/fs route for the desktop page and a paired phone.

    python3 -m unittest tests.test_fs -v

Everything runs against a temp home. The route test starts server.py on a spare port
(LOFT_TEST_FS_PORT, default 7743) with HOME pointed at that temp home and
--dev-loopback-devices, so a device token works on 127.0.0.1 like the phone's.
"""
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from loft import fs as FS  # noqa: E402

PORT = int(os.environ.get("LOFT_TEST_FS_PORT", 7743))


def _free(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def make_home(root):
    home = root / "home"
    (home / "code" / "shop" / ".git").mkdir(parents=True)
    (home / "code" / "site").mkdir(parents=True)
    (home / "code" / "site" / "package.json").write_text("{}")
    (home / "code" / "Notes").mkdir()
    (home / "code" / "readme.txt").write_text("a file, not listed")
    (home / ".config").mkdir()
    (home / "Documents").mkdir()
    (root / "outside").mkdir()
    (home / "code" / "escape").symlink_to(root / "outside")    # leads out of home: not listed
    (home / "code" / "link-shop").symlink_to(home / "code" / "shop")
    return home


class Listing(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="loft-fs-")).resolve()
        self.home = make_home(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def ls(self, path="~", **kw):
        return FS.listdir(path, home=self.home, **kw)

    def test_home_default(self):
        r = self.ls()
        self.assertEqual((r["path"], r["display"], r["parent"]), (str(self.home), "~", None))
        self.assertEqual([d["name"] for d in r["dirs"]], ["code", "Documents"])   # hidden left out, sorted case-free
        self.assertFalse(r["truncated"])
        self.assertEqual(FS.listdir("", home=self.home)["path"], str(self.home))

    def test_subfolder_shape_and_markers(self):
        r = self.ls("~/code")
        self.assertEqual(r["display"], "~/code")
        self.assertEqual(r["parent"], str(self.home))
        self.assertEqual(r["recent"], [])                          # recent only at the top level
        by = {d["name"]: d for d in r["dirs"]}
        self.assertEqual([d["name"] for d in r["dirs"]], ["link-shop", "Notes", "shop", "site"])
        self.assertNotIn("readme.txt", by)                        # directories only
        self.assertNotIn("escape", by)                            # symlink out of home
        self.assertEqual(set(by["shop"]), {"name", "path", "display", "git", "web"})
        self.assertTrue(by["shop"]["git"]); self.assertFalse(by["shop"]["web"])
        self.assertTrue(by["site"]["web"]); self.assertFalse(by["site"]["git"])
        self.assertEqual(by["link-shop"]["path"], str(self.home / "code" / "shop"))   # resolved
        self.assertEqual(by["shop"]["display"], "~/code/shop")
        self.assertEqual(self.ls(str(self.home / "code"))["path"], str(self.home / "code"))   # absolute works too

    def test_hidden(self):
        self.assertIn(".config", [d["name"] for d in self.ls(hidden=True)["dirs"]])

    def test_refuses_outside_home(self):
        for p in ("/", str(self.root / "outside"), "~/..", "~/code/escape"):
            with self.assertRaises(FS.FsError) as e:
                self.ls(p)
            self.assertEqual(e.exception.code, 403, p)

    def test_bad_paths(self):
        for p, code in (("~/nope", 404), ("code", 400), ("~/code/readme.txt", 400)):
            with self.assertRaises(FS.FsError) as e:
                self.ls(p)
            self.assertEqual(e.exception.code, code, p)

    def test_cap(self):
        big = self.home / "big"
        for i in range(30):
            (big / f"d{i:02}").mkdir(parents=True)
        r = self.ls("~/big", limit=10)
        self.assertEqual(len(r["dirs"]), 10)
        self.assertTrue(r["truncated"])
        self.assertEqual(r["dirs"][0]["name"], "d00")

    def test_recent(self):
        recent = [str(self.home / "code" / "shop"), "~/code/site", str(self.home / "code" / "shop"),
                  str(self.root / "outside"), str(self.home / "gone"), str(self.home)]
        r = self.ls(recent=recent)
        self.assertEqual([d["display"] for d in r["recent"]], ["~/code/shop", "~/code/site"])   # dedup, home only, existing
        self.assertTrue(r["recent"][0]["git"])
        self.assertEqual(self.ls("~/code", recent=recent)["recent"], [])


@unittest.skipUnless(_free(PORT), f"port {PORT} busy")
class Route(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="loft-fs-srv-")).resolve()
        cls.home = make_home(cls.root)
        claude = cls.root / "claude"
        (claude / "jobs" / "aaaa1111").mkdir(parents=True)
        (claude / "jobs" / "aaaa1111" / "state.json").write_text(json.dumps(
            {"name": "x", "state": "done", "cwd": str(cls.home / "code" / "site")}))
        conf = cls.root / "conf"
        conf.mkdir()
        (conf / "accounts.json").write_text(json.dumps({"default": "a1", "accounts": [
            {"id": "a1", "label": "One", "color": "green", "creds_dir": "default"}]}))
        (conf / "loft.json").write_text(json.dumps({"projects": [
            {"id": "shop", "name": "Shop", "color": "blue", "folders": [str(cls.home / "code" / "shop")]}]}))
        cls.phone_tok = "phone-test-token"
        (conf / "devices.json").write_text(json.dumps([{"id": "dtest", "name": "phone", "sha256": hashlib.sha256(cls.phone_tok.encode()).hexdigest(),
                                                        "created": None, "last_seen": None, "last_ip": None, "node": None, "node_name": None}]))
        env = {**os.environ, "HOME": str(cls.home), "LOFT_CONF_DIR": str(conf), "LOFT_CLAUDE_HOME": str(claude),
               "XDG_CACHE_HOME": str(cls.root / "cache"), "XDG_DATA_HOME": str(cls.root / "data"), "LOFT_CLAUDE": "/bin/false"}
        cls.proc = subprocess.Popen([sys.executable, str(HERE / "server.py"), "--port", str(PORT), "--no-tailnet",
                                     "--dev-loopback-devices", "--no-pending-loop"],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if not _free(PORT):
                break
            time.sleep(0.1)
        _, html = cls.req("GET", "/", raw=True, headers={})
        cls.token = re.search(rb'const TOKEN = "([0-9a-f]+)"', html).group(1).decode()

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)
        shutil.rmtree(cls.root, ignore_errors=True)

    @classmethod
    def req(cls, method, path, raw=False, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        c.request(method, path, headers={"X-Cagents-Token": cls.token} if headers is None else headers)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, (data if raw else json.loads(data or b"{}"))

    def test_desktop(self):
        st, r = self.req("GET", "/api/fs")
        self.assertEqual(st, 200)
        self.assertEqual((r["display"], r["parent"]), ("~", None))
        self.assertEqual([d["name"] for d in r["dirs"]], ["code", "Documents"])
        self.assertEqual([d["display"] for d in r["recent"]], ["~/code/site", "~/code/shop"])   # agent folders, then projects
        st, r = self.req("GET", "/api/fs?path=" + quote("~/code"))
        self.assertEqual((st, r["display"]), (200, "~/code"))
        self.assertTrue(next(d for d in r["dirs"] if d["name"] == "shop")["git"])
        self.assertIn(".config", [d["name"] for d in self.req("GET", "/api/fs?hidden=1")[1]["dirs"]])
        self.assertEqual(self.req("GET", "/api/fs?path=/etc")[0], 403)
        self.assertEqual(self.req("GET", "/api/fs?path=" + quote("~/nope"))[0], 404)

    def test_needs_a_token(self):
        self.assertEqual(self.req("GET", "/api/fs", headers={})[0], 403)

    def test_phone_token(self):
        st, r = self.req("GET", "/api/fs?path=" + quote("~/code"), headers={"Authorization": f"Bearer {self.phone_tok}"})
        self.assertEqual((st, r["display"]), (200, "~/code"))
        self.assertEqual(self.req("GET", "/api/fs", headers={"Authorization": "Bearer wrong"})[0], 403)


if __name__ == "__main__":
    unittest.main()
