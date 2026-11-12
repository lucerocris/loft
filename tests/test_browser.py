"""Tests for the shared agent browser: dev-server detection, live viewer, recorded checks, saved logins.

Runs a real headless Chrome against a throwaway static site; skipped without Chrome.
Config and cache go to a temp dir, so ~/.config/loft is never touched.

  python3 -m unittest tests.test_browser -v
"""
import json, os, shutil, socket, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from loft import browser as BR  # noqa: E402

PAGES = {
    "index.html": """<!doctype html><title>t</title><input name="q"><input type="password" name="pw"><button data-testid="go"
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

    def test_safe_path(self):
        for ok in ("/", "/a?b=1#c", "/me.html"):
            self.assertTrue(BR.safe_path(ok), ok)
        for bad in ("//evil.com/x", "http://evil.com", "evil", "/\\evil.com", "/@evil.com", None):
            self.assertFalse(BR.safe_path(bad), bad)

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
        v = BR.Viewer(BR.ensure(jid)["port"], frames.append, events.append, scope, jid)
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
            v.record()
        self.assertIsNone(BR.recording("j2"))

    def test_dev_ports_finds_server_in_folder(self):
        self.assertIn(self.port, BR.dev_ports(str(self.site)))
        self.assertNotIn(self.port, BR.dev_ports(str(self.tmp / "elsewhere")))

    def test_you_record_then_replay(self):
        v, frames, events = self.viewer("j1")
        BR.subscribe("j1", events.append)
        self.addCleanup(BR.unsubscribe, "j1", events.append)
        v.navigate(self.base + "/")
        self.assertTrue(self.wait(lambda: frames and BR.page_eval(v.cdp, v.session, "document.readyState") == "complete"))
        BR.page_eval(v.cdp, v.session, "document.querySelector('[name=q]').value = 'dirty'")
        v.record()                          # reloads: recording starts from the state the replay starts from
        self.addCleanup(BR.rec_stop, "j1")
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
        self.assertTrue(self.wait(lambda: len(BR.recording("j1") or []) >= 5))
        self.assertEqual(BR.page_eval(v.cdp, v.session, "typeof window.__loftRec"), "undefined")   # page can't fake steps
        BR.page_eval(v.cdp, v.session, "document.querySelector('[data-testid=go]').click()")      # untrusted: ignored
        time.sleep(0.3)
        steps = BR.rec_stop("j1")
        self.assertEqual([s["action"] for s in steps], ["goto", "click", "fill", "click", "expect"])
        self.assertEqual(steps[0]["path"], "/")
        self.assertEqual(steps[2]["value"], "Sam")
        self.assertEqual(steps[3]["css"], '[data-testid="go"]')
        self.assertEqual(steps[4]["text"], "Hello Sam")
        self.assertTrue(any(e["type"] == "steps" and e["on"] is False for e in events))

        port = BR.ensure("j1")["port"]
        self.assertEqual(BR.run_check(port, self.base, steps, self.tmp / "s1.png"), {"ok": True})
        bad = steps[:4] + [{**steps[4], "text": "Hello Bob"}]
        r = BR.run_check(port, self.base, bad, self.tmp / "s2.png")
        self.assertFalse(r["ok"]); self.assertEqual(r["step"], 5)
        r = BR.run_check(port, self.base, [{"action": "goto", "path": "//example.com/"}], self.tmp / "s7.png")
        self.assertFalse(r["ok"]); self.assertIn("refused", r["error"])
        self.assertTrue(Path(r["shot"]).stat().st_size > 1000)

    def test_agent_drives_and_is_recorded(self):
        """The agent drives over its own CDP connection (as the Playwright MCP does), in a tab it opens."""
        port = BR.ensure("j2")["port"]
        BR.rec_start("j2", port)
        self.addCleanup(BR.rec_stop, "j2")
        cdp = BR.CDP(BR._get(port, "/json/version")["webSocketDebuggerUrl"])
        self.addCleanup(cdp.close)
        tid = cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        s = cdp.call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
        cdp.call("Page.enable", session=s)
        time.sleep(0.5)                     # the recorder attaches to the new tab
        cdp.call("Page.navigate", {"url": self.base + "/"}, session=s)
        self.assertTrue(self.wait(lambda: BR.page_eval(cdp, s, "document.readyState") == "complete"))
        time.sleep(0.3)

        def click(css):
            r = BR.page_eval(cdp, s, f"__loft.find({css!r}, '', false)")
            for kind in ("mousePressed", "mouseReleased"):
                cdp.call("Input.dispatchMouseEvent", {"type": kind, "x": r["x"], "y": r["y"], "button": "left", "clickCount": 1}, session=s)
            time.sleep(0.2)
        click("[name=q]"); cdp.call("Input.insertText", {"text": "Ann"}, session=s)
        click("[name=pw]"); cdp.call("Input.insertText", {"text": "hunter2"}, session=s)
        click("[data-testid=go]")
        BR.rec_expect("j2", "Hello Ann")
        steps = BR.rec_stop("j2")
        self.assertEqual([x["action"] for x in steps], ["goto", "click", "fill", "click", "fill", "click", "expect"])
        self.assertEqual(steps[4], {**steps[4], "secret": True, "value": ""})   # passwords never stored
        self.assertNotIn("hunter2", json.dumps(steps))
        self.assertEqual(BR.run_check(port, self.base, steps, self.tmp / "s6.png"), {"ok": True})

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

    def test_agent_tools_over_mcp(self):
        """loft/agent_mcp.py -> a real Loft server -> the agent's own browser, with its token."""
        jid = "feedbeef"
        job = self.tmp / "claude/jobs" / jid
        job.mkdir(parents=True, exist_ok=True)
        (job / "state.json").write_text(json.dumps({"name": "t", "state": "working", "cwd": str(self.site)}))
        rec = BR.ensure(jid)
        rec.update(token="t0k-" + jid, keyfile=str(self.tmp / "agent.token"))
        BR.paths.write_private(rec["keyfile"], rec["token"])
        BR._write(jid, rec)
        self.addCleanup(BR.stop, jid)
        v, _, _ = self.viewer(jid)
        v.navigate(self.base + "/")
        self.assertTrue(self.wait(lambda: BR.page_eval(v.cdp, v.session, "document.readyState") == "complete"))

        lport = BR.free_port()
        env = {**os.environ, "LOFT_CLAUDE_HOME": str(self.tmp / "claude")}
        srv = subprocess.Popen([sys.executable, str(Path(BR.__file__).parent.parent / "server.py"), "--port", str(lport),
                                "--no-tailnet", "--no-pending-loop"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (srv.terminate(), srv.wait()))
        def up():
            try:
                socket.create_connection(("127.0.0.1", lport), timeout=0.2).close(); return True
            except OSError:
                return False
        self.assertTrue(self.wait(up))

        mcp = subprocess.Popen([sys.executable, str(Path(BR.__file__).with_name("agent_mcp.py")), "--key-file", rec["keyfile"],
                                "--port", str(lport)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
        self.addCleanup(lambda: (mcp.stdin.close(), mcp.wait()))
        ids = iter(range(1, 100))

        def rpc(method, params=None):
            i = next(ids)
            mcp.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
            mcp.stdin.flush()
            r = json.loads(mcp.stdout.readline())
            self.assertEqual(r["id"], i)
            return r["result"]

        def tool(tool_name, **args):
            r = rpc("tools/call", {"name": tool_name, "arguments": args})
            return r["isError"], json.loads(r["content"][0]["text"])

        self.assertEqual(rpc("initialize", {"protocolVersion": "2025-06-18"})["serverInfo"]["name"], "loft")
        self.assertIn("loft_record_start", [t["name"] for t in rpc("tools/list")["tools"]])
        self.assertFalse(tool("loft_record_start")[0])
        self.assertTrue(tool("loft_record_start")[0])                   # already recording
        self.assertFalse(tool("loft_record_expect", text="Go")[0])
        err, out = tool("loft_record_save", name="Has a Go button")
        self.assertFalse(err, out)
        self.assertEqual([s["action"] for s in out["steps"]], ["goto", "expect"])
        self.assertIn("approves", out["message"])
        saved = tool("loft_list_checks")[1]["checks"][0]
        self.assertEqual((saved["name"], saved["pending"], saved["by"]), ("Has a Go button", True, jid))
        self.assertEqual(os.stat(BR._checks_file()).st_mode & 0o777, 0o600)
        err, res = tool("loft_run_checks", base_url=self.base)
        self.assertFalse(err, res); self.assertTrue(res["ok"])

        # another token gets nothing
        import urllib.request, urllib.error
        req = urllib.request.Request(f"http://127.0.0.1:{lport}/api/agent/checks", headers={"X-Loft-Agent": "nope"})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(e.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
