"""Tests for phones (loft/mobile.py, loft/agenttools.py): adb output parsing, the element list, typing,
leases, and the agent's phone tools. With an Android emulator or phone connected, LOFT_TEST_DEVICE=1
also drives a real one (screenshot, element list, tap, the live screen stream).

  python3 -m unittest tests.test_mobile -v
"""
import json, os, shutil, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from loft import agenttools as AT, mobile as M  # noqa: E402

DEVICES = """List of devices attached
emulator-5600          device product:sdk_gphone64_x86_64 model:sdk_gphone64_x86_64 device:emu64xa transport_id:1
R58M12ABCDE            unauthorized usb:1-1 transport_id:2
bad id!                device

"""

UI_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation="0">
<node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.x" content-desc="" clickable="false" enabled="true" focused="false" scrollable="false" password="false" bounds="[0,0][1080,2400]">
<node index="0" text="Email" resource-id="com.x:id/email" class="android.widget.EditText" package="com.x" content-desc="" clickable="true" enabled="true" focused="true" scrollable="false" password="false" bounds="[100,500][980,620]" />
<node index="1" text="secret" resource-id="com.x:id/pw" class="android.widget.EditText" package="com.x" content-desc="" clickable="true" enabled="true" focused="false" scrollable="false" password="true" bounds="[100,700][980,820]" />
<node index="2" text="Sign in" resource-id="com.x:id/go" class="android.widget.Button" package="com.x" content-desc="" clickable="true" enabled="false" focused="false" scrollable="false" password="false" bounds="[100,900][980,1020]" />
<node index="3" text="" resource-id="" class="android.view.View" package="com.x" content-desc="" clickable="false" enabled="true" focused="false" scrollable="false" password="false" bounds="[0,0][1,1]" />
</node></hierarchy>"""


class Parse(unittest.TestCase):
    def test_devices(self):
        d = M.parse_devices(DEVICES)
        self.assertEqual([x["serial"] for x in d], ["emulator-5600", "R58M12ABCDE"])
        self.assertTrue(d[0]["emulator"]); self.assertEqual(d[0]["model"], "sdk gphone64 x86 64")
        self.assertEqual(d[1]["state"], "unauthorized"); self.assertFalse(d[1]["emulator"])

    def test_ui(self):
        els = M.parse_ui(UI_XML)
        self.assertEqual([e.get("id") for e in els], ["email", "pw", "go"])
        email, pw, go = els
        self.assertEqual((email["x"], email["y"]), (540, 560))
        self.assertTrue(email["edit"] and email["focused"])
        self.assertTrue(pw["password"])
        self.assertNotIn("text", pw)                  # a password field's text is never listed
        self.assertTrue(go["off"])
        lines = M.ui_lines(els)
        self.assertIn('[3] Button "Sign in" id=go @(540,960) click,off', lines)
        self.assertNotIn("secret", lines)
        self.assertEqual(M.parse_ui("not xml"), [])

    def test_text_arg(self):
        self.assertEqual(M.text_arg("hello world"), "hello%sworld")
        self.assertEqual(M.text_arg("a'b; rm -rf"), "'a'\"'\"'b;%srm%s-rf'")
        self.assertEqual(M.text_arg("100%"), "'100\\%'")

    def test_serial_check(self):
        M.check_serial("emulator-5554"); M.check_serial("192.168.1.5:5555")
        for bad in ("", "a b", "x;rm", "../x"):
            if bad == "../x":
                continue
            with self.assertRaises(ValueError):
                M.check_serial(bad)


class Leases(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loft-mobile-"))
        self.env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.tmp / "cache")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def devs(self, *serials):
        return mock.patch.object(M, "devices", return_value=[{"serial": s, "state": "device", "emulator": True,
                                                              "avd": "avd" + s[-1]} for s in serials])

    def test_one_agent_per_device(self):
        with self.devs("emulator-5600", "emulator-5602"), mock.patch.object(M, "avds", return_value=[]):
            a, _ = M.device_for("agentA")
            b, _ = M.device_for("agentB")
            self.assertNotEqual(a, b)
            self.assertEqual(M.device_for("agentA")[0], a)         # keeps its own
            with self.assertRaises(RuntimeError):
                M.device_for("agentC")                              # both taken, no spare emulator
            self.assertEqual(M.device_for("agentC", alive=lambda j: j != "agentA")[0], a)   # A is gone: C gets it
            with self.assertRaises(RuntimeError):
                M.device_for("agentA", want=b)                      # B's is in use
        M.release("agentB")
        self.assertIsNone(M.lease_of("agentB"))

    def test_boots_a_spare_emulator(self):
        with self.devs(), mock.patch.object(M, "avds", return_value=["Pixel_8"]), \
                mock.patch.object(M, "running_emulators", return_value=[]), \
                mock.patch.object(M, "boot", return_value="emulator-5600") as boot, \
                mock.patch.object(M, "wait_booted", return_value=True):
            s, note = M.device_for("agentA")
        boot.assert_called_once_with("Pixel_8")
        self.assertEqual(s, "emulator-5600"); self.assertIn("Pixel_8", note)

    def test_no_emulators_at_all(self):
        with self.devs(), mock.patch.object(M, "avds", return_value=[]), mock.patch.object(M, "running_emulators", return_value=[]):
            with self.assertRaises(RuntimeError) as cm:
                M.device_for("agentA")
        self.assertIn("Android Studio", str(cm.exception))


def _device():
    if os.environ.get("LOFT_TEST_DEVICE") != "1" or not M.available():
        return None
    ready = [d for d in M.devices() if d["state"] == "device"]
    return ready[0]["serial"] if ready else None


@unittest.skipUnless(_device(), "set LOFT_TEST_DEVICE=1 with an emulator or phone connected")
class RealDevice(unittest.TestCase):
    def setUp(self):
        self.serial = _device()
        self.tmp = Path(tempfile.mkdtemp(prefix="loft-mobile-"))
        self.env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.tmp / "cache")})
        self.env.start()

    def tearDown(self):
        M.release("test-agent")
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_screen_tools(self):
        code, out = AT.phone("test-agent", "screen", {}, lambda j: True)
        self.assertEqual(code, 200)
        self.assertIn(f"Device {self.serial}", out["text"])
        self.assertTrue(out["image"])
        code, out = AT.phone("test-agent", "key", {"key": "home", "look": False}, lambda j: True)
        self.assertEqual((code, out["message"]), (200, "Pressed home"))
        code, out = AT.phone("test-agent", "swipe", {"direction": "up"}, lambda j: True)
        self.assertEqual(code, 200); self.assertIn("Swiped", out["text"])
        AT.phone("test-agent", "key", {"key": "home", "look": False}, lambda j: True)

    def test_stream(self):
        frames = []
        s = M.Screen(self.serial, frames.append, lambda e: None)
        try:
            time.sleep(1.5); M.key(self.serial, "home"); time.sleep(2.5)
        finally:
            s.close()
        self.assertTrue(frames, "no frames")
        self.assertEqual(frames[0][:2], b"\xff\xd8")


if __name__ == "__main__":
    unittest.main()
