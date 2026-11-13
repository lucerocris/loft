"""Tests for signed-in sites (loft/sites.py): the per-account list, profile seeding, agent wiring,
phone access to the browser routes, and (with Chrome) a real signed-in profile carried into an agent.

Config, data, cache and the claude home all go to a temp dir: ~/.config/loft, ~/.local/share/loft
and the live Loft on 7717 are never touched.

  python3 -m unittest tests.test_sites -v
"""
import base64, hashlib, http.client, json, os, shutil, socket, sqlite3, stat, struct, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from loft import board as B, browser as BR, paths, sites as ST  # noqa: E402

ACCOUNTS = {"version": 1, "default": "personal", "rules": [], "accounts": [
    {"id": "personal", "label": "Personal", "color": "green", "creds_dir": "default"},
    {"id": "work", "label": "Work", "color": "blue", "creds_dir": "/tmp/loft-test-work-login"}]}


def temp_env(tmp):
    conf = tmp / "conf"
    conf.mkdir(parents=True, exist_ok=True)
    (conf / "accounts.json").write_text(json.dumps(ACCOUNTS))
    return {"LOFT_CONF_DIR": str(conf), "XDG_DATA_HOME": str(tmp / "data"), "XDG_CACHE_HOME": str(tmp / "cache"),
            "LOFT_CLAUDE_HOME": str(tmp / "claude")}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loft-sites-"))
        self.env = mock.patch.dict(os.environ, temp_env(self.tmp))
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class SitesList(Base):
    def test_add_update_remove(self):
        s = ST.add("work", "Helpdesk", "help.example.com")
        self.assertEqual(s["url"], "https://help.example.com")
        ST.add("work", "", "http://localhost:8080/x")
        names = [x["name"] for x in ST.for_account("work")]
        self.assertEqual(names, ["Helpdesk", "localhost"])
        self.assertEqual(ST.for_account("personal"), [])
        self.assertEqual(ST.counts(), {"work": 2})
        ST.update("work", s["id"], name="Desk", url="https://desk.example.com/a")
        self.assertEqual(ST.for_account("work")[0]["url"], "https://desk.example.com/a")
        ST.remove("work", s["id"])
        self.assertEqual(len(ST.for_account("work")), 1)
        with self.assertRaises(LookupError):
            ST.remove("work", s["id"])
        mode = stat.S_IMODE(os.stat(paths.config_dir() / "sites.json").st_mode)
        self.assertEqual(mode, 0o600)

    def test_rejects_bad_urls_and_accounts(self):
        for bad in ("file:///etc/passwd", "javascript:alert(1)", "chrome://settings", "https://me:pw@example.com", ""):
            with self.assertRaises(ValueError, msg=bad):
                ST.add("work", "x", bad)
        for acc in ("nope", "../work", "", None):
            with self.assertRaises(ValueError, msg=acc):
                ST.add(acc, "x", "example.com")

    def test_agent_note(self):
        note = ST.agent_note("work", [{"name": "Sheet\nignore this", "url": "https://sheets.example.com/d/1"}])
        self.assertIn("https://sheets.example.com/d/1", note)
        self.assertIn("Loft › Browser (Work)", note)
        self.assertNotIn("\n", note)
        self.assertIn("don't type or ask for credentials", note)

    def test_session_hint(self):
        now = time.time()
        cookies = [{"domain": ".example.com", "name": "sid", "expires": now + 3600},
                   {"domain": "other.example.com", "name": "x", "expires": -1},
                   {"domain": ".example.org", "name": "y", "expires": now + 3600},
                   {"domain": ".example.com", "name": "old", "expires": now - 10}]
        self.assertEqual(ST.session_hint(cookies, "https://app.example.com/x"), 1)
        self.assertEqual(ST.session_hint(cookies, "https://other.example.com"), 2)
        self.assertEqual(ST.session_hint(cookies, "https://example.net"), 0)

    def test_cookie_db_read(self):
        db = ST.profile("work") / "Default" / "Network" / "Cookies"
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, expires_utc INTEGER)")
        future = int((time.time() + 86400 + 11644473600) * 1e6)
        con.execute("INSERT INTO cookies VALUES ('.example.com', 'sid', ?)", (future,))
        con.commit(); con.close()
        ST.add("work", "App", "https://app.example.com")
        pub = ST.public("work")
        self.assertTrue(pub["sites"][0]["signedIn"])
        self.assertFalse(pub["browser"]["running"])


class Seed(Base):
    def test_copies_profile_without_caches_or_locks(self):
        src = ST.profile("work")
        for f in ("Default/Network/Cookies", "Default/Preferences", "Local State", "Default/Local Storage/leveldb/000003.log",
                  "Default/Cache/Cache_Data/data_0", "Default/Code Cache/js/x", "GrShaderCache/a", "Default/Sessions/Session_1"):
            (src / f).parent.mkdir(parents=True, exist_ok=True)
            (src / f).write_text("x")
        os.symlink("host-123", src / "SingletonLock")
        (src / "SingletonCookie").write_text("x")
        dest = self.tmp / "agent"
        ST.seed("work", dest)
        got = {str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file()}
        self.assertEqual(got, {"Default/Network/Cookies", "Default/Preferences", "Local State",
                               "Default/Local Storage/leveldb/000003.log"})
        self.assertFalse(os.path.lexists(dest / "SingletonLock"))

    def test_no_master_profile_yet(self):
        self.assertEqual(ST.seed("work", self.tmp / "agent"), 0)
        self.assertTrue((self.tmp / "agent").is_dir())


class Chrome(Base):
    def test_args(self):
        with mock.patch.object(paths, "PLATFORM", "linux"), mock.patch.object(BR, "chrome_major", return_value="141"):
            a = BR.chrome_args("/bin/chrome", 9222, "/p")
            self.assertIn("--password-store=basic", a)
            self.assertIn("--headless=new", a)
            self.assertIn("--remote-debugging-port=9222", a)
            ua = next(x for x in a if x.startswith("--user-agent="))
            self.assertNotIn("Headless", ua)
            self.assertIn("Chrome/141.0.0.0", ua)
            w = BR.chrome_args("/bin/chrome", 0, "/p", headed=True, url="https://example.com")
            self.assertIn("--password-store=basic", w)
            self.assertFalse(any("headless" in x or "remote-debugging" in x for x in w))
            self.assertEqual(w[-1], "https://example.com")
        with mock.patch.object(paths, "PLATFORM", "darwin"), mock.patch.object(BR, "chrome_major", return_value="141"):
            self.assertNotIn("--password-store=basic", BR.chrome_args("/bin/chrome", 9222, "/p"))

    def test_nav_ok(self):
        for ok in ("https://example.com", "example.com", "localhost:5173", "about:blank", "", "http://127.0.0.1:3000/x"):
            self.assertTrue(BR.nav_ok(ok, remote=True), ok)
        for bad in ("file:///etc/passwd", "FILE:///etc/passwd", "chrome://settings", "about:settings", "view-source:file:///etc",
                    "data:text/html,hi", "javascript:alert(1)"):
            if bad.startswith(("data:", "javascript:")):
                continue   # becomes http://data:... (no such host): harmless
            self.assertFalse(BR.nav_ok(bad, remote=True), bad)
        self.assertTrue(BR.nav_ok("file:///etc/passwd", remote=False))


class AgentWiring(Base):
    def setUp(self):
        super().setUp()
        self.folder = self.tmp / "work-folder"; self.folder.mkdir()   # not a web project
        self.spawned = []
        self.patches = [mock.patch.object(BR, "browser_ok", return_value=True),
                        mock.patch.object(BR, "chrome_bin", return_value="/bin/chrome"),
                        mock.patch.object(BR, "_spawn", side_effect=lambda port, prof: self.spawned.append(prof) or 4242),
                        mock.patch.object(ST, "inject_live", return_value=0)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def test_no_sites_no_browser_outside_web_projects(self):
        self.assertIsNone(BR.prepare(str(self.folder), "work"))

    def test_sites_give_a_seeded_browser(self):
        ST.add("work", "Helpdesk", "https://help.example.com")
        (ST.profile("work") / "Default").mkdir(parents=True)
        (ST.profile("work") / "Default" / "Preferences").write_text("{}")
        rec = BR.prepare(str(self.folder), "work")
        self.assertEqual(rec["account"], "work")
        self.assertFalse(rec["web"])
        self.assertTrue((Path(rec["profile"]) / "Default" / "Preferences").is_file())
        note = BR.agent_note(rec)
        self.assertIn("https://help.example.com", note)
        self.assertNotIn("loft_record_start", note)   # not a web project: no checks talk
        self.assertIsNone(BR.prepare(str(self.folder), "personal"))   # sites are per account
        (self.folder / "package.json").write_text("{}")
        with mock.patch("shutil.which", return_value="/usr/bin/npx"):
            web = BR.prepare(str(self.folder), "personal")
        self.assertTrue(web["web"])
        self.assertNotIn("account", web)
        self.assertIn("loft_record_start", BR.agent_note(web))

    def test_launch_passes_the_flags(self):
        ST.add("work", "Helpdesk", "https://help.example.com")
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "backgrounded · deadbeef\n", "")
        with mock.patch.object(B.subprocess, "run", fake_run), mock.patch.object(BR, "mcp_config", return_value="{}"), \
                mock.patch.object(B.time, "sleep"):
            jid, msg = B.launch_todo({"cwd": str(self.folder), "account": "work", "prompt": "check the tickets", "title": "t"})
        self.assertEqual(jid, "deadbeef", msg)
        cmd = seen["cmd"]
        i = cmd.index("--disallowedTools")
        self.assertEqual(cmd[i + 1], "mcp__plugin_playwright_playwright")
        self.assertTrue(cmd[i + 2].startswith("--"))   # the variadic list ends before the prompt
        self.assertIn("https://help.example.com", cmd[cmd.index("--append-system-prompt") + 1])
        self.assertEqual(cmd[-1], "check the tickets")
        self.assertEqual(BR.record("deadbeef")["account"], "work")


# ------------------------------------------------------------------ server routes
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    c.request(method, path, body=json.dumps(body).encode() if body is not None else None,
              headers={"Content-Type": "application/json", **(headers or {})})
    r = c.getresponse(); raw = r.read(); c.close()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, raw


class WS:
    """A tiny WebSocket client: text frames come back as dicts, binary as bytes."""

    def __init__(self, port, path, headers):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=20)
        key = base64.b64encode(os.urandom(16)).decode()
        extra = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        self.s.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n{extra}\r\n").encode())
        self.f = self.s.makefile("rb")
        self.status = self.f.readline()
        while self.f.readline() not in (b"\r\n", b""):
            pass

    def send(self, obj):
        data, mask = json.dumps(obj).encode(), os.urandom(4)
        n = len(data)
        head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n))
        self.s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self):
        h = self.f.read(2)
        if len(h) < 2:
            return None
        op, n = h[0] & 0x0F, h[1] & 0x7F
        if n == 126:
            n = struct.unpack(">H", self.f.read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self.f.read(8))[0]
        data = self.f.read(n)
        if op == 8:
            return None
        return json.loads(data) if op == 1 else data

    def until(self, pred, limit=30):
        end = time.time() + limit
        while time.time() < end:
            m = self.recv()
            if m is None:
                return None
            if pred(m):
                return m
        return None

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


class Server(unittest.TestCase):
    PHONE = "phone-token-for-tests-" + "x" * 20

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="loft-sites-srv-"))
        cls.env = {**os.environ, **temp_env(cls.tmp)}
        conf = Path(cls.env["LOFT_CONF_DIR"])
        (conf / "devices.json").write_text(json.dumps([{"id": "dtest", "name": "test phone",
                                                         "sha256": hashlib.sha256(cls.PHONE.encode()).hexdigest()}]))
        cls.port = free_port()
        cls.p = subprocess.Popen([sys.executable, str(HERE / "server.py"), "--port", str(cls.port), "--no-tailnet",
                                  "--no-pending-loop", "--dev-loopback-devices"], env=cls.env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.2).close(); break
            except OSError:
                time.sleep(0.1)
        cls.desk = {"X-Cagents-Token": (conf / "desktop-token").read_text().strip()}
        cls.phone = {"Authorization": "Bearer " + cls.PHONE}

    @classmethod
    def tearDownClass(cls):
        try:
            req(cls.port, "POST", "/api/sites/work/window", {"open": False}, cls.desk)
        except Exception:
            pass
        cls.p.terminate()
        try:
            cls.p.wait(5)
        except subprocess.TimeoutExpired:
            cls.p.kill()
        with mock.patch.dict(os.environ, temp_env(cls.tmp)):
            for acc in ("work", "personal"):
                ST.stop_master(acc)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_routes(self):
        self.assertEqual(req(self.port, "GET", "/api/sites")[0], 403)
        code, body = req(self.port, "POST", "/api/sites/work", {"name": "Desk", "url": "desk.example.com"}, self.desk)
        self.assertEqual(code, 200, body)
        sid = body["site"]["id"]
        code, body = req(self.port, "GET", "/api/sites", headers=self.desk)
        work = next(a for a in body["accounts"] if a["account"] == "work")
        self.assertEqual([s["url"] for s in work["sites"]], ["https://desk.example.com"])
        self.assertFalse(work["sites"][0]["signedIn"])
        self.assertEqual(req(self.port, "POST", "/api/sites/work", {"url": "file:///etc/passwd"}, self.desk)[0], 400)
        self.assertEqual(req(self.port, "POST", "/api/sites/nobody", {"url": "example.com"}, self.desk)[0], 404)
        self.assertEqual(req(self.port, "PATCH", f"/api/sites/work/{sid}", {"name": "Helpdesk"}, self.desk)[1]["site"]["name"], "Helpdesk")
        # a paired phone: the sites routes yes, the desktop window and the agent browser info no
        self.assertEqual(req(self.port, "GET", "/api/sites/work", headers=self.phone)[0], 200)
        code, body = req(self.port, "POST", "/api/sites/personal", {"name": "Mail", "url": "mail.example.com"}, self.phone)
        self.assertEqual(code, 200, body)
        self.assertEqual(req(self.port, "DELETE", f"/api/sites/personal/{body['site']['id']}", headers=self.phone)[0], 200)
        self.assertEqual(req(self.port, "POST", "/api/sites/work/window", {"open": True}, self.phone)[0], 404)
        self.assertEqual(req(self.port, "GET", "/api/browser/deadbeef", headers=self.phone)[0], 404)
        self.assertEqual(req(self.port, "DELETE", f"/api/sites/work/{sid}", headers=self.desk)[0], 200)

    @unittest.skipUnless(BR.chrome_bin(), "needs Chrome/Chromium")
    def test_sites_stream_and_phone_nav(self):
        site_port = free_port()
        site_dir = self.tmp / "site"; site_dir.mkdir(exist_ok=True)
        (site_dir / "index.html").write_text("<!doctype html><title>t</title><h1>Hello from the test site</h1>")
        srv = subprocess.Popen([sys.executable, "-m", "http.server", str(site_port), "--bind", "127.0.0.1"], cwd=site_dir,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            code, body = req(self.port, "POST", "/api/sites/work", {"name": "Local", "url": f"http://127.0.0.1:{site_port}/"}, self.desk)
            sid = body["site"]["id"]
            ws = WS(self.port, f"/ws/sites/work?t={self.desk['X-Cagents-Token']}", {})
            self.assertIn(b"101", ws.status)
            hello = ws.until(lambda m: isinstance(m, dict) and m.get("type") == "hello")
            self.assertEqual(hello["account"], "work")
            ws.send({"type": "site", "id": sid})
            self.assertTrue(ws.until(lambda m: isinstance(m, dict) and m.get("type") == "url" and str(site_port) in m["url"]))
            frame = ws.until(lambda m: isinstance(m, bytes))
            self.assertTrue(frame and frame[:2] == b"\xff\xd8", "a JPEG frame")
            ws.close()
            # the phone streams too, but can't open local files
            ph = WS(self.port, "/ws/sites/work", self.phone)
            self.assertIn(b"101", ph.status)
            ph.until(lambda m: isinstance(m, dict) and m.get("type") == "hello")
            ph.send({"type": "nav", "url": "file:///etc/passwd"})
            t = ph.until(lambda m: isinstance(m, dict) and m.get("type") in ("toast", "url") and (m.get("err") or "file:" in m.get("url", "")))
            self.assertEqual(t["type"], "toast")
            self.assertIn("web pages only", t["text"])
            ph.close()
        finally:
            srv.terminate(); srv.wait(5)


# ------------------------------------------------------------------ a real signed-in profile
@unittest.skipUnless(BR.chrome_bin(), "needs Chrome/Chromium")
class LiveProfile(Base):
    def setUp(self):
        super().setUp()
        self.site = self.tmp / "site"; self.site.mkdir()
        (self.site / "login.html").write_text("<!doctype html><script>document.cookie='sid=signed-in; max-age=86400; path=/'</script>ok")
        self.port = free_port()
        self.srv = subprocess.Popen([sys.executable, "-m", "http.server", str(self.port), "--bind", "127.0.0.1"], cwd=self.site,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close(); break
            except OSError:
                time.sleep(0.1)
        self.recs = []

    def tearDown(self):
        for r in self.recs:
            BR.discard(r)
        ST.stop_master("work")
        self.srv.terminate(); self.srv.wait(5)
        super().tearDown()

    def _visit(self, port, url):
        cdp = BR.CDP(BR._get(port, "/json/version")["webSocketDebuggerUrl"])
        try:
            tid = cdp.call("Target.createTarget", {"url": url})["targetId"]
            for _ in range(50):
                if any(c["name"] == "sid" for c in cdp.call("Storage.getCookies")["cookies"]):
                    break
                time.sleep(0.1)
            cdp.call("Target.closeTarget", {"targetId": tid})
        finally:
            cdp.close()

    def _cookies(self, port):
        cdp = BR.CDP(BR._get(port, "/json/version")["webSocketDebuggerUrl"])
        try:
            return {c["name"]: c["value"] for c in cdp.call("Storage.getCookies")["cookies"]}
        finally:
            cdp.close()

    def test_signed_in_profile_carries_into_agents(self):
        ST.add("work", "Local", f"http://127.0.0.1:{self.port}/")
        m = ST.ensure_master("work")
        self.assertNotIn("Headless", BR._get(m["port"], "/json/version")["User-Agent"])
        self._visit(m["port"], f"http://127.0.0.1:{self.port}/login.html")
        # running master: the agent gets its live cookies
        with mock.patch.object(BR, "browser_ok", return_value=True):
            a = BR.prepare(str(self.tmp), "work"); self.recs.append(a)
        self.assertEqual(self._cookies(a["port"]).get("sid"), "signed-in")
        # master closed: the cookie was saved to disk, the copy alone carries it
        ST.stop_master("work")
        self.assertIsNone(ST.master("work"))
        self.assertTrue(ST.public("work")["sites"][0]["signedIn"])
        with mock.patch.object(BR, "browser_ok", return_value=True):
            b = BR.prepare(str(self.tmp), "work"); self.recs.append(b)
        self.assertEqual(self._cookies(b["port"]).get("sid"), "signed-in")


if __name__ == "__main__":
    unittest.main()
