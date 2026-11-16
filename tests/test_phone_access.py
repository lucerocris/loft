"""Tests for Loft's phone access (tailnet listener, device tokens) and live voice.

Starts real servers on spare ports with a throwaway config dir, so the live
Loft on 7717 and ~/.config/loft are never touched. Needs Tailscale up (the
phone listener binds this machine's tailnet IP). The voice test needs the loft
venv and espeak-ng; it is skipped without them.

  python3 -m unittest tests.test_phone_access -v
"""
import base64, http.client, importlib.machinery, importlib.util, json, os, re, shutil, socket, struct, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
PORT, PHONE_PORT = int(os.environ.get("LOFT_TEST_PORT", 7718)), int(os.environ.get("LOFT_TEST_PHONE_PORT", 7719))
TS_IP = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True).stdout.strip()


def start_server(conf, *extra):
    env = {**os.environ, "LOFT_CONF_DIR": conf}
    p = subprocess.Popen([sys.executable, str(HERE / "server.py"), "--port", str(PORT), "--phone-port", str(PHONE_PORT), *extra],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    return p


def stop_server(p):
    p.terminate()
    try:
        p.wait(5)
    except subprocess.TimeoutExpired:
        p.kill()
    p.stdout.close()


def req(host, port, method, path, body=None, headers=None, timeout=10):
    c = http.client.HTTPConnection(host, port, timeout=timeout)
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    h = {"Content-Type": "application/json", **(headers or {})}
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    raw = r.read()
    c.close()
    try:
        return r.status, json.loads(raw)
    except Exception:
        return r.status, raw


def desktop_token():
    _, html = req("127.0.0.1", PORT, "GET", "/")
    return re.search(rb'const TOKEN = "([0-9a-f]+)"', html).group(1).decode()


class WS:
    """Minimal RFC 6455 client (masked frames), enough for the tests."""

    def __init__(self, host, port, path, headers):
        self.s = socket.create_connection((host, port), timeout=60)
        key = base64.b64encode(os.urandom(16)).decode()
        hs = [f"GET {path} HTTP/1.1", f"Host: {host}:{port}", "Upgrade: websocket", "Connection: Upgrade",
              f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"] + [f"{k}: {v}" for k, v in headers.items()]
        self.s.sendall(("\r\n".join(hs) + "\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.s.recv(1)
            if not chunk:
                break
            resp += chunk
        self.status = int(resp.split(b" ")[1]) if resp else 0

    def send(self, op, data):
        mask = os.urandom(4)
        n = len(data)
        head = bytes([0x80 | op])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _exact(self, n):
        b = b""
        while len(b) < n:
            c = self.s.recv(n - len(b))
            if not c:
                raise EOFError
            b += c
        return b

    def recv(self):
        h = self._exact(2)
        op, n = h[0] & 0x0F, h[1] & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._exact(8))[0]
        return op, self._exact(n)


@unittest.skipUnless(TS_IP.startswith("100."), "Tailscale is not up")
class PhoneAccess(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conf = tempfile.mkdtemp(prefix="loft-test-conf-")
        cls.proc = start_server(cls.conf)
        cls.tok = desktop_token()
        cls.D = {"X-Cagents-Token": cls.tok}

    @classmethod
    def tearDownClass(cls):
        stop_server(cls.proc)
        shutil.rmtree(cls.conf, ignore_errors=True)

    def phone(self, method, path, token=None, **kw):
        h = {"Authorization": f"Bearer {token}"} if token else {}
        h.update(kw.pop("headers", {}))
        return req(TS_IP, PHONE_PORT, method, path, headers=h, **kw)

    def pair(self, name="test-phone"):
        st, j = req("127.0.0.1", PORT, "POST", "/api/pair", {"name": name}, self.D)
        self.assertEqual(st, 200, j)
        return j

    def test_01_off_by_default(self):
        st, j = req("127.0.0.1", PORT, "GET", "/api/phone", headers=self.D)
        self.assertEqual((st, j["on"], j["running"]), (200, False, False))
        st, j = req("127.0.0.1", PORT, "POST", "/api/pair", {"name": "x"}, self.D)
        self.assertEqual(st, 409)
        with self.assertRaises(OSError):
            socket.create_connection((TS_IP, PHONE_PORT), timeout=1).close()

    def test_02_turn_on_binds_tailnet_ip_only(self):
        st, j = req("127.0.0.1", PORT, "POST", "/api/phone", {"on": True}, self.D)
        self.assertEqual((st, j["on"], j["running"], j["ip"]), (200, True, True, TS_IP))
        cfg = Path(self.conf) / "config.json"
        self.assertEqual(json.loads(cfg.read_text()), {"phone": True})
        self.assertEqual(oct(cfg.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(Path(self.conf).stat().st_mode & 0o777), "0o700")
        ss = subprocess.run(["ss", "-ltnH"], capture_output=True, text=True).stdout
        binds = [l.split()[3] for l in ss.splitlines() if l.split()[3].endswith((f":{PORT}", f":{PHONE_PORT}"))]
        self.assertEqual(sorted(binds), sorted([f"127.0.0.1:{PORT}", f"{TS_IP}:{PHONE_PORT}"]))

    def test_03_good_token(self):
        p = self.pair()
        self.assertRegex(p["uri"], rf"^loft://pair\?h={re.escape(TS_IP)}:{PHONE_PORT}&t=[\w-]+&n=\w+")
        st, j = self.phone("GET", "/api/ping", p["token"])
        self.assertEqual(st, 200, j)
        self.assertEqual(j["device"], "test-phone")
        st, j = self.phone("GET", "/api/state", p["token"])
        self.assertEqual(st, 200)
        self.assertIn("cards", j)
        devs = json.loads((Path(self.conf) / "devices.json").read_text())
        self.assertEqual(oct((Path(self.conf) / "devices.json").stat().st_mode & 0o777), "0o600")
        self.assertNotIn(p["token"], json.dumps(devs))            # only the hash is stored
        d = next(d for d in devs if d["id"] == p["id"])
        self.assertTrue(d["node"])                                  # pinned to this tailnet node on first use
        st, lst = req("127.0.0.1", PORT, "GET", "/api/devices", headers=self.D)
        self.assertNotIn("sha256", json.dumps(lst))

    def test_04_bad_and_missing_and_desktop_tokens(self):
        self.assertEqual(self.phone("GET", "/api/ping", "nope")[0], 401)
        self.assertEqual(self.phone("GET", "/api/ping")[0], 401)
        self.assertEqual(self.phone("GET", "/api/ping", headers=self.D)[0], 401)          # desktop token refused
        self.assertEqual(self.phone("GET", f"/api/events?t={self.tok}")[0], 401)           # no query tokens
        Guard_reset(self)

    def test_05_blocked_routes_on_phone_listener(self):
        p = self.pair("routes")
        for path in ("/", "/index.html", "/static/xterm.js", "/api/devices", "/api/phone"):
            self.assertEqual(self.phone("GET", path, p["token"])[0], 404, path)
        self.assertEqual(self.phone("POST", "/api/pair", p["token"], body={"name": "evil"})[0], 404)
        self.assertEqual(self.phone("POST", "/api/phone", p["token"], body={"on": False})[0], 404)
        self.assertEqual(self.phone("DELETE", f"/api/devices/{p['id']}", p["token"])[0], 404)

    def test_06_device_token_refused_on_loopback_without_dev_flag(self):
        p = self.pair("loop")
        st, _ = req("127.0.0.1", PORT, "GET", "/api/ping", headers={"Authorization": f"Bearer {p['token']}"})
        self.assertEqual(st, 403)

    def test_07_revoke_kills_token_and_open_stream(self):
        p = self.pair("revoke-me")
        c = http.client.HTTPConnection(TS_IP, PHONE_PORT, timeout=10)
        c.request("GET", "/api/events", headers={"Authorization": f"Bearer {p['token']}"})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertTrue(r.fp.readline().startswith(b"data: "))
        t0 = time.time()
        st, _ = req("127.0.0.1", PORT, "DELETE", f"/api/devices/{p['id']}", headers=self.D)
        self.assertEqual(st, 200)
        try:
            while r.fp.readline():
                pass
        except OSError:
            pass
        self.assertLess(time.time() - t0, 2.5)                      # the stream drops right away
        self.assertEqual(self.phone("GET", "/api/ping", p["token"])[0], 401)

    def test_08_rate_limit(self):
        p = self.pair("after-block")
        codes = [self.phone("GET", "/api/ping", "wrong")[0] for _ in range(6)]
        self.assertEqual(codes[-1], 429, codes)
        self.assertLessEqual(codes.index(429), 5, codes)          # blocked by the 6th try at the latest
        self.assertEqual(self.phone("GET", "/api/ping", p["token"])[0], 429)   # blocked even with a good token

    def test_09_phone_sends_a_file(self):
        Guard_reset(self)   # test 08 left this IP blocked
        p = self.pair("files")
        for path in ("/api/files", "/api/uploads"):
            st, j = self.phone("POST", f"{path}?name=shot%201.png", p["token"], body=b"\x89PNG img",
                               headers={"Content-Type": "application/octet-stream"})
            self.assertEqual(st, 200, j)
            f = Path(j["path"])
            try:
                self.assertEqual((f.name, f.read_bytes(), f.parent.parent.name), ("shot 1.png", b"\x89PNG img", "attachments"))
            finally:
                shutil.rmtree(f.parent, ignore_errors=True)
        self.assertEqual(self.phone("POST", "/api/files?name=e", p["token"], body=b"")[0], 400)
        self.assertEqual(self.phone("POST", "/api/files?name=x", headers={"Content-Type": "application/octet-stream"}, body=b"x")[0], 401)


def Guard_reset(tc):
    """Test 04 used 4 failures; wait them out of the window isn't practical, so
    restart the server process instead to keep the rate-limit test exact."""
    stop_server(tc.proc)
    type(tc).proc = start_server(tc.conf)
    type(tc).tok = desktop_token()
    type(tc).D = {"X-Cagents-Token": type(tc).tok}


def _load_server():
    loader = importlib.machinery.SourceFileLoader("loft_server", str(HERE / "server.py"))
    spec = importlib.util.spec_from_loader("loft_server", loader)
    mod = importlib.util.module_from_spec(spec)
    old = sys.argv
    sys.argv = ["server.py"]
    try:
        loader.exec_module(mod)
    finally:
        sys.argv = old
    return mod


class InProcess(unittest.TestCase):
    """Checks that need control over the peer address or the Tailscale identity."""

    @classmethod
    def setUpClass(cls):
        cls.conf = tempfile.mkdtemp(prefix="loft-test-conf-")
        os.environ["LOFT_CONF_DIR"] = cls.conf
        cls.S = _load_server()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("LOFT_CONF_DIR", None)
        shutil.rmtree(cls.conf, ignore_errors=True)

    def test_non_tailnet_peer_refused(self):
        S = self.S
        dev, tok = S.pair_device("p")
        srv = S.ThreadingHTTPServer(("127.0.0.1", 0), S.RemoteH)     # a peer of 127.0.0.1 is not in 100.64.0.0/10
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            st, j = req("127.0.0.1", srv.server_address[1], "GET", "/api/ping", headers={"Authorization": f"Bearer {tok}"})
            self.assertEqual((st, j["error"]), (403, "tailnet only"))
        finally:
            srv.shutdown(); srv.server_close()

    def test_pin_refuses_other_node_and_other_user(self):
        S = self.S
        dev, _ = S.pair_device("pinned")
        owner = S.Whois.owner()
        orig = S.Whois.of
        try:
            S.Whois.of = classmethod(lambda cls, ip: {"node": "nodeA", "node_name": "phone-1", "user": owner})
            self.assertEqual(S.pin_check(dev, "100.1.2.3"), (True, ""))
            dev = next(d for d in S.load_devices() if d["id"] == dev["id"])
            self.assertEqual(dev["node"], "nodeA")
            S.Whois.of = classmethod(lambda cls, ip: {"node": "nodeB", "node_name": "other", "user": owner})
            ok, why = S.pin_check(dev, "100.1.2.4")
            self.assertFalse(ok); self.assertIn("phone-1", why)
            dev2, _ = S.pair_device("fresh")
            S.Whois.of = classmethod(lambda cls, ip: {"node": "nodeC", "node_name": "nuc", "user": -1})
            self.assertFalse(S.pin_check(dev2, "100.1.2.5")[0])     # someone else's node can't claim a pairing
        finally:
            S.Whois.of = orig

    def test_rate_limit_window(self):
        G = self.S.Guard
        for _ in range(4):
            G.fail("100.9.9.9")
        self.assertFalse(G.blocked("100.9.9.9"))
        G.fail("100.9.9.9")
        self.assertTrue(G.blocked("100.9.9.9"))

    def test_audio_suffix(self):
        f = self.S.audio_suffix
        self.assertEqual([f("audio/mp4"), f("audio/aac"), f("audio/m4a"), f("audio/webm;codecs=opus"), f("audio/ogg"), f("audio/wav")],
                         [".m4a", ".m4a", ".m4a", ".webm", ".ogg", ".wav"])


VENV = Path.home() / ".local/share/loft/venv/bin/python"


@unittest.skipUnless(TS_IP.startswith("100.") and VENV.exists() and shutil.which("espeak-ng"), "needs Tailscale, the loft venv and espeak-ng")
class LiveVoice(unittest.TestCase):
    SENTENCE = "Check the appeal cards in the GitHub repo and open a pull request for the fix. Send it."

    @classmethod
    def setUpClass(cls):
        cls.conf = tempfile.mkdtemp(prefix="loft-test-conf-")
        cls.proc = start_server(cls.conf, "--tailnet")
        tok = desktop_token()
        cls.D = {"X-Cagents-Token": tok}
        st, j = req("127.0.0.1", PORT, "POST", "/api/pair", {"name": "voice"}, cls.D)
        cls.token = j["token"]
        wav = Path(cls.conf) / "s.wav"
        subprocess.run(["espeak-ng", "-s", "150", "-w", str(wav), cls.SENTENCE], check=True)
        cls.wav = wav.read_bytes()
        cls.pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(wav), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                                 capture_output=True, check=True).stdout

    @classmethod
    def tearDownClass(cls):
        stop_server(cls.proc)
        shutil.rmtree(cls.conf, ignore_errors=True)

    def test_stream_partials_then_final(self):
        ws = WS(TS_IP, PHONE_PORT, "/ws/voice", {"Authorization": f"Bearer {self.token}"})
        self.assertEqual(ws.status, 101)
        got, stop = [], threading.Event()

        def reader():
            try:
                while True:
                    op, data = ws.recv()
                    if op == 8:
                        break
                    got.append((time.time(), json.loads(data)))
            except (EOFError, OSError):
                pass
            stop.set()

        threading.Thread(target=reader, daemon=True).start()
        step = 3200                                   # 100 ms frames, sent in real time
        for i in range(0, len(self.pcm), step):
            ws.send(2, self.pcm[i:i + step])
            time.sleep(0.1)
        t_stop = time.time()
        ws.send(1, json.dumps({"type": "stop"}).encode())
        self.assertTrue(stop.wait(60))
        partials = [m for _, m in got if m["type"] == "partial"]
        finals = [(t, m) for t, m in got if m["type"] == "final"]
        print(f"\n  {len(partials)} partials over {len(self.pcm) / 32000:.1f}s of audio; e.g. {partials[len(partials) // 2]['text']!r}")
        print(f"  final after {finals[0][0] - t_stop:.2f}s: {finals[0][1]['text']!r}")
        self.assertGreaterEqual(len(partials), 3)
        self.assertEqual(len(finals), 1)
        text = finals[0][1]["text"].lower()
        for w in ("appeal", "github", "pull request", "send it"):
            self.assertIn(w, text)

    def test_cancel(self):
        ws = WS(TS_IP, PHONE_PORT, "/ws/voice", {"Authorization": f"Bearer {self.token}"})
        ws.send(2, self.pcm[:32000])
        ws.send(1, json.dumps({"type": "cancel"}).encode())
        msgs = []
        try:
            while True:
                op, data = ws.recv()
                if op == 8:
                    break
                msgs.append(json.loads(data))
        except (EOFError, OSError):
            pass
        self.assertFalse([m for m in msgs if m["type"] == "final"])

    def test_ws_voice_needs_token(self):
        ws = WS(TS_IP, PHONE_PORT, "/ws/voice", {})
        self.assertEqual(ws.status, 401)

    def test_post_voice_wav_from_phone(self):
        st, j = req(TS_IP, PHONE_PORT, "POST", "/api/voice", self.wav,
                    {"Authorization": f"Bearer {self.token}", "Content-Type": "audio/wav"}, timeout=60)
        self.assertEqual(st, 200, j)
        self.assertIn("appeal", j["text"].lower())
        self.assertIn("ms", j)


if __name__ == "__main__":
    unittest.main(verbosity=2)
