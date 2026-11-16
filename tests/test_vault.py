"""Tests for the vault (loft/vault.py): keys and logins, scopes, the keys hook, TOTP, the agent launch
wiring (loft/board.py agent_extras) and the desktop-only routes.

Config, data and cache go to a temp dir and secrets to the file backend (LOFT_VAULT=file): the
real keyring and ~/.config/loft are never touched.

  python3 -m unittest tests.test_vault -v
"""
import json, os, shutil, stat, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from loft import agent_mcp as AM, board as B, browser as BR, vault as V  # noqa: E402

ACCOUNTS = {"version": 1, "default": "personal", "rules": [], "accounts": [
    {"id": "personal", "label": "Personal", "color": "green", "creds_dir": "default"},
    {"id": "work", "label": "Work", "color": "blue", "creds_dir": "/tmp/loft-test-work-login"}]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loft-vault-"))
        conf = self.tmp / "conf"; conf.mkdir()
        (conf / "accounts.json").write_text(json.dumps(ACCOUNTS))
        self.env = mock.patch.dict(os.environ, {"LOFT_CONF_DIR": str(conf), "XDG_DATA_HOME": str(self.tmp / "data"),
                                                "XDG_CACHE_HOME": str(self.tmp / "cache"), "LOFT_VAULT": "file",
                                                "LOFT_CLAUDE_HOME": str(self.tmp / "claude")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class Keys(Base):
    def test_add_and_scopes(self):
        V.add_key("API_URL_TOKEN", "global-1")
        V.add_key("STRIPE_SECRET_KEY", "sk_test_account", {"kind": "account", "id": "work"})
        V.add_key("STRIPE_SECRET_KEY", "sk_test_project", {"kind": "project", "id": "p1"})
        self.assertEqual(V.keys_for("personal", None), {"API_URL_TOKEN": "global-1"})
        self.assertEqual(V.keys_for("work", None)["STRIPE_SECRET_KEY"], "sk_test_account")
        self.assertEqual(V.keys_for("work", "p1")["STRIPE_SECRET_KEY"], "sk_test_project")   # project wins
        self.assertEqual(V.key_names_for("work", "p1"), ["API_URL_TOKEN", "STRIPE_SECRET_KEY"])

    def test_metadata_has_no_values(self):
        V.add_key("SECRET_ONE", "do-not-leak-1234")
        meta = (Path(os.environ["LOFT_CONF_DIR"]) / "vault.json").read_text()
        self.assertNotIn("do-not-leak-1234", meta)
        self.assertNotIn("do-not-leak-1234", json.dumps(V.public()))
        f = V.FileStore().path()
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)

    def test_names_checked(self):
        for bad in ("1ABC", "has space", "PATH", "LD_PRELOAD", "CLAUDE_X", "LOFT_PORT", ""):
            with self.assertRaises(ValueError, msg=bad):
                V.add_key(bad, "v")
        V.add_key("OK_NAME", "v")
        with self.assertRaises(ValueError):
            V.add_key("OK_NAME", "v2")        # same name, same scope

    def test_update_remove(self):
        k = V.add_key("A_KEY", "one")
        V.update_key(k["id"], value="two")
        self.assertEqual(V.key_value(k["id"]), "two")
        V.update_key(k["id"], value="")       # empty keeps it
        self.assertEqual(V.key_value(k["id"]), "two")
        V.remove_key(k["id"])
        self.assertIsNone(V.key_value(k["id"]))
        self.assertEqual(V.load()["keys"], [])

    def test_hook_writes_env_file(self):
        V.add_key("QUOTED_KEY", "it's $HOME `x`")
        V.add_key("PLAIN_KEY", "abc123")
        tid = V.make_ticket("personal", None)
        env_file = self.tmp / "envfile"
        self.assertEqual(V.run_hook(tid, str(env_file)), 2)
        text = env_file.read_text()
        out = os.popen(f"bash -c 'source {env_file}; printf %s \"$QUOTED_KEY\"'").read()
        self.assertEqual(out, "it's $HOME `x`")
        self.assertIn("export PLAIN_KEY=abc123", text)
        self.assertEqual(V.run_hook("not-a-ticket", str(env_file)), 0)
        V.drop_ticket(tid)
        self.assertIsNone(V.ticket(tid))

    def test_hook_settings_is_a_command_without_secrets(self):
        V.add_key("SOME_KEY", "very-secret-value")
        s = json.loads(V.hook_settings("0123456789abcdef", ["/usr/bin/python3", "/x/bin/loft"]))
        cmd = s["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertTrue(cmd.endswith("/usr/bin/python3 /x/bin/loft vault hook 0123456789abcdef"), cmd)
        self.assertIn("LOFT_CONF_DIR=", cmd)      # the worker's env isn't Loft's: the hook is told where to look
        self.assertNotIn("very-secret-value", json.dumps(s))
        with mock.patch.dict(os.environ, {"LOFT_CONF_DIR": "", "XDG_DATA_HOME": "", "LOFT_VAULT": ""}):
            s = json.loads(V.hook_settings("0123456789abcdef", ["/usr/bin/python3", "/x/bin/loft"]))
        self.assertEqual(s["hooks"]["SessionStart"][0]["hooks"][0]["command"],
                         "/usr/bin/python3 /x/bin/loft vault hook 0123456789abcdef")


class Logins(Base):
    def test_totp_rfc6238(self):
        # RFC 6238 test vector (SHA-1): secret "12345678901234567890", T=59 -> 94287082 (8 digits)
        import base64
        sec = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
        self.assertEqual(V.totp(sec, at=59, digits=8), "94287082")
        self.assertEqual(V.totp(sec, at=1111111109, digits=8), "07081804")
        self.assertEqual(len(V.totp(sec)), 6)

    def test_clean_totp_accepts_otpauth_and_spaces(self):
        self.assertEqual(V.clean_totp("jbsw y3dp ehpk 3pxp"), "JBSWY3DPEHPK3PXP")
        self.assertEqual(V.clean_totp("otpauth://totp/x?secret=JBSWY3DPEHPK3PXP&issuer=y"), "JBSWY3DPEHPK3PXP")
        with self.assertRaises(ValueError):
            V.clean_totp("not base32 !!")

    def test_add_and_secrets(self):
        lg = V.add_login("Staging admin", "admin@test.app", "hunter2pass", "staging.app.com/login",
                         {"kind": "project", "id": "p1"}, "JBSWY3DPEHPK3PXP")
        self.assertEqual(lg["url"], "https://staging.app.com/login")
        self.assertTrue(lg["totp"])
        pw, code = V.login_secret(lg["id"])
        self.assertEqual(pw, "hunter2pass")
        self.assertRegex(code, r"^\d{6}$")
        self.assertNotIn("hunter2pass", json.dumps(V.public()))
        self.assertEqual([x["id"] for x in V.logins_for("work", "p1")], [lg["id"]])
        self.assertEqual(V.logins_for("work", "other"), [])
        with self.assertRaises(ValueError):
            V.add_login("x", "u", "p", "https://user:pw@host")

    def test_picked_logins_only(self):
        a = V.add_login("A", "a@x", "pa")
        b = V.add_login("B", "b@x", "pb")
        self.assertEqual([x["id"] for x in V.logins_for("personal", None, [b["id"]])], [b["id"]])
        self.assertEqual(len(V.logins_for("personal", None)), 2)
        self.assertEqual(V.find_login("a@x", [a, b])["id"], a["id"])
        self.assertEqual(V.find_login("b", [a, b])["id"], b["id"])
        self.assertIsNone(V.find_login("c", [a, b]))

    def test_note_names_but_never_passwords(self):
        lg = V.add_login("Customer", "c1@test.app", "pw-secret-xyz", "https://app.test/login")
        V.add_key("EXPO_TOKEN", "tok-secret")
        note = V.agent_note([lg], ["EXPO_TOKEN"])
        self.assertIn("Customer (c1@test.app, app.test)", note)
        self.assertIn("loft_sign_in", note)
        self.assertIn("EXPO_TOKEN", note)
        self.assertNotIn("pw-secret-xyz", note)
        self.assertNotIn("tok-secret", note)


class Launch(Base):
    """agent_extras: what Loft adds to `claude --bg` for keys, logins and the phone."""

    def setUp(self):
        super().setUp()
        self.dir = self.tmp / "proj"; self.dir.mkdir()
        self.acc = {"id": "personal", "label": "Personal", "color": "green", "creds_dir": "default"}

    def extras(self, todo=None, phone=False, browser_ok=False):
        with mock.patch.object(B, "wants_phone", return_value=phone), \
                mock.patch.object(BR, "browser_ok", return_value=browser_ok), \
                mock.patch.object(BR, "_spawn", return_value=12345):
            return B.agent_extras(todo or {}, str(self.dir), self.acc)

    def flag(self, args, name):
        return args[args.index(name) + 1] if name in args else None

    def test_nothing_to_add(self):
        self.assertEqual(self.extras(), (None, []))

    def test_keys_only(self):
        V.add_key("MY_TOKEN", "value-abc")
        rec, args = self.extras()
        self.assertFalse(rec.get("port"))
        settings = json.loads(self.flag(args, "--settings"))
        self.assertIn("vault hook " + rec["ticket"], settings["hooks"]["SessionStart"][0]["hooks"][0]["command"])
        self.assertNotIn("value-abc", " ".join(args))
        self.assertIn("MY_TOKEN", self.flag(args, "--append-system-prompt"))
        self.assertEqual(json.loads(self.flag(args, "--mcp-config")), {"mcpServers": {}})
        self.assertNotIn("--disallowedTools", args)

    def test_phone_gets_phone_tools(self):
        rec, args = self.extras(phone=True)
        mcp = json.loads(self.flag(args, "--mcp-config"))["mcpServers"]
        self.assertEqual(set(mcp), {"loft"})
        self.assertEqual(mcp["loft"]["args"][-1], "phone")
        self.assertIn("loft_phone", self.flag(args, "--append-system-prompt"))
        self.assertTrue(Path(rec["keyfile"]).exists())

    def test_logins_without_chrome_get_signin_tool(self):
        lg = V.add_login("Admin", "admin@x", "pw-zzz")
        rec, args = self.extras({"logins": [lg["id"]]}, phone=True)
        mcp = json.loads(self.flag(args, "--mcp-config"))["mcpServers"]
        self.assertEqual(mcp["loft"]["args"][-1], "phone,signin")
        self.assertEqual(rec["logins"], [lg["id"]])
        self.assertNotIn("pw-zzz", " ".join(args))

    def test_logins_start_a_browser(self):
        V.add_login("Admin", "admin@x", "pw")
        with mock.patch.object(BR, "chrome_bin", return_value="/usr/bin/true"):
            rec, args = self.extras(browser_ok=True)
        self.assertTrue(rec.get("port"))
        mcp = json.loads(self.flag(args, "--mcp-config"))["mcpServers"]
        self.assertEqual(set(mcp), {"loft-browser", "loft"})
        self.assertEqual(mcp["loft"]["args"][-1], "checks,signin")
        self.assertIn("--disallowedTools", args)
        BR.discard(rec)

    def test_wants_phone_markers(self):
        from loft import mobile as M
        with mock.patch.object(M, "available", return_value=True):
            self.assertFalse(B.wants_phone(str(self.dir)))
            (self.dir / "pubspec.yaml").write_text("name: x")
            self.assertTrue(B.wants_phone(str(self.dir)))
            self.assertFalse(B.wants_phone(str(self.dir), {"phone": False}))
            self.assertTrue(B.wants_phone(str(self.tmp), {"phone": True}))
        with mock.patch.object(M, "available", return_value=False):
            self.assertFalse(B.wants_phone(str(self.dir), {"phone": True}))


class AgentTools(unittest.TestCase):
    def test_tool_groups(self):
        with mock.patch.object(sys, "argv", ["x", "--tools", "phone"]):
            names = {t["name"] for t in AM.tools()}
        self.assertIn("loft_phone_tap", names)
        self.assertNotIn("loft_sign_in", names)
        self.assertNotIn("loft_record_start", names)
        with mock.patch.object(sys, "argv", ["x"]):   # old launches: checks only
            self.assertEqual({t["name"] for t in AM.tools()}, {t["name"] for t in AM.TOOLS})

    def test_unlisted_tool_refused(self):
        with mock.patch.object(sys, "argv", ["x", "--tools", "checks"]):
            with self.assertRaises(LookupError):
                AM.handle({"method": "tools/call", "params": {"name": "loft_phone_tap", "arguments": {}}})

    def test_image_content(self):
        blocks = AM.content({"text": "Device x", "image": "aGVsbG8=", "mime": "image/jpeg"})
        self.assertEqual(blocks[0], {"type": "text", "text": "Device x"})
        self.assertEqual(blocks[-1], {"type": "image", "data": "aGVsbG8=", "mimeType": "image/jpeg"})
        self.assertEqual(AM.content({"message": "Done here"}), [{"type": "text", "text": "Done here"}])


if __name__ == "__main__":
    unittest.main()
