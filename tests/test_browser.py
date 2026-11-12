"""Tests for the shared agent browser: dev-server detection, live viewer, recorded checks, saved logins.

Runs a real headless Chrome against a throwaway static site; skipped without Chrome.
Config and cache go to a temp dir, so ~/.config/loft is never touched.

  python3 -m unittest tests.test_browser -v
"""
import os, shutil, socket, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from loft import browser as BR  # noqa: E402

PAGES = {
    "index.html": """<!doctype html><title>t</title><input name="q"><button data-testid="go"
        onclick="document.getElementById('out').textContent='Hello '+document.querySelector('[name=q]').value">Go</button>
        <div id="out"></div>""",
    "login.html": """<!doctype html><script>document.cookie='sid=abc; path=/'; localStorage.setItem('user','sam')</script>in""",
    "me.html": """<!doctype html><div id="who"></div><script>document.getElementById('who').textContent =
        document.cookie.includes('sid=abc') && localStorage.getItem('user') === 'sam' ? 'welcome sam' : 'please log in'</script>""",
}


class Parse(unittest.TestCase):
    def test_parse_lsof(self):
        self.assertEqual(BR.parse_lsof("p12\nn*:3000\nn127.0.0.1:5173\np40\nn/Users/x/app\n"),
                         {12: ["*:3000", "127.0.0.1:5173"], 40: ["/Users/x/app"]})

    def test_rehost_local_only(self):
        c = [{"name": "a", "domain": "localhost"}, {"name": "b", "domain": ".example.com"}]
        self.assertEqual(BR._rehost(c, "localhost", "127.0.0.1")[0]["domain"], "127.0.0.1")
        self.assertEqual(BR._rehost(c, "localhost", "127.0.0.1")[1]["domain"], ".example.com")
        self.assertEqual(BR._rehost(c, "app.dev", "127.0.0.1"), c)


@unittest.skipUnless(BR.chrome_bin(), "needs Chrome/Chromium")
class Live(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="loft-br-"))
        cls.site = cls.tmp / "site"; cls.site.mkdir()
        for name, html in PAGES.items():
            (cls.site / name).write_text(html)
        cls.env = mock.patch.dict(os.environ, {"LOFT_CONF_DIR": str(cls.tmp / "conf"), "XDG_CACHE_HOME": str(cls.tmp / "cache")})
        cls.env.start()
        cls.port = BR.free_port()
        cls.srv = subprocess.Popen([sys.executable, "-m", "http.server", str(cls.port), "--bind", "127.0.0.1"],
                                   cwd=cls.site, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.2).close(); break
            except OSError:
                time.sleep(0.1)
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        for j in ("j1", "j2"):
            BR.stop(j)
        cls.srv.terminate(); cls.srv.wait()
        cls.env.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def viewer(self, jid, scope=None):
        frames, events = [], []
        v = BR.Viewer(BR.ensure(jid)["port"], frames.append, events.append, scope)
        self.addCleanup(v.close)
        return v, frames, events

    def wait(self, cond, limit=10):
        end = time.time() + limit
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.1)
        return False

    def at(self, v, css):
        """Normalized screen position of an element, like the card sends."""
        r = BR.page_eval(v.cdp, v.session, f"__loft.find({css!r}, '', false)")
        return {"x": r["x"] / v.meta["deviceWidth"], "y": r["y"] / v.meta["deviceHeight"]}

    def test_record_needs_a_page(self):
        v, _, _ = self.viewer("j2")
        v.navigate("about:blank")
        time.sleep(0.3)
        with self.assertRaises(RuntimeError):
            v.record(True)

    def test_dev_ports_finds_server_in_folder(self):
        self.assertIn(self.port, BR.dev_ports(str(self.site)))
        self.assertNotIn(self.port, BR.dev_ports(str(self.tmp / "elsewhere")))

    def test_record_then_replay(self):
        v, frames, events = self.viewer("j1")
        v.navigate(self.base + "/")
        self.assertTrue(self.wait(lambda: frames and BR.page_eval(v.cdp, v.session, "document.readyState") == "complete"))
        BR.page_eval(v.cdp, v.session, "document.querySelector('[name=q]').value = 'dirty'")
        v.record(True)                      # reloads: recording starts from the state the replay starts from
        time.sleep(0.5)
        self.assertTrue(self.wait(lambda: BR.page_eval(v.cdp, v.session, "document.readyState") == "complete"))
        self.assertEqual(BR.page_eval(v.cdp, v.session, "document.querySelector('[name=q]').value"), "")
        for kind in ("mousePressed", "mouseReleased"):
            v.mouse({**self.at(v, "[name=q]"), "kind": kind})
        time.sleep(0.2)
        v.key({"text": "Sam"})
        for kind in ("mousePressed", "mouseReleased"):
            v.mouse({**self.at(v, "[data-testid=go]"), "kind": kind})
        self.assertTrue(self.wait(lambda: "Hello Sam" in BR.page_eval(v.cdp, v.session, "document.body.innerText")))
        v.mouse({**self.at(v, "#out"), "kind": "mousePressed", "expect": True})
        steps = v.record(False)
        self.assertEqual([s["action"] for s in steps], ["goto", "click", "type", "click", "expect"])
        self.assertEqual(steps[3]["css"], '[data-testid="go"]')
        self.assertEqual(steps[4]["text"], "Hello Sam")

        port = BR.ensure("j1")["port"]
        self.assertEqual(BR.run_check(port, self.base, steps, self.tmp / "s1.png"), {"ok": True})
        bad = steps[:4] + [{**steps[4], "text": "Hello Bob"}]
        r = BR.run_check(port, self.base, bad, self.tmp / "s2.png")
        self.assertFalse(r["ok"]); self.assertEqual(r["step"], 5)
        self.assertTrue(Path(r["shot"]).stat().st_size > 1000)
        self.assertTrue(any(e["type"] == "steps" for e in events))

    def test_saved_login_carries_to_a_fresh_browser(self):
        v, frames, _ = self.viewer("j1", scope="p1")
        v.navigate(self.base + "/login.html")
        self.assertTrue(self.wait(lambda: BR.page_eval(v.cdp, v.session, "localStorage.getItem('user')") == "sam"))
        self.assertIn("1 cookies", v.save_login())
        self.assertEqual(os.stat(BR._logins_file()).st_mode & 0o777, 0o600)
        steps = [{"action": "goto", "path": "/me.html"}, {"action": "expect", "css": "#who", "text": "welcome sam"}]
        port2 = BR.ensure("j2")["port"]            # its own profile: no cookies of its own
        self.assertFalse(BR.run_check(port2, self.base, steps, self.tmp / "s3.png")["ok"])
        self.assertTrue(BR.run_check(port2, self.base, steps, self.tmp / "s4.png", scope="p1")["ok"])
        # never into another site's page
        v.navigate("about:blank")
        time.sleep(0.5)
        with self.assertRaises(RuntimeError):
            BR.login_storage(v.cdp, v.session, "p1")
        # and on localhost, where the login wasn't saved
        self.assertTrue(BR.run_check(port2, f"http://localhost:{self.port}", steps, self.tmp / "s5.png", scope="p1")["ok"])


if __name__ == "__main__":
    unittest.main()
