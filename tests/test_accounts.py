"""Unit tests for accounts, env building, keychain naming, config, migration and cswap import.

    python3 -m unittest discover -s tests

Everything runs against temp dirs; macOS rules are simulated by flipping
loft.paths.PLATFORM (nothing here talks to a Keychain or the network).
"""
import hashlib
import json
import os
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from loft import accounts as A, board as B, core as C, paths, usage as U  # noqa: E402


class Env(unittest.TestCase):
    """Each test gets its own HOME-like tree and config dir."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loft-test-"))
        self.claude = self.tmp / "claude"
        (self.claude / "jobs").mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {
            "LOFT_CONF_DIR": str(self.tmp / "conf"), "XDG_DATA_HOME": str(self.tmp / "data"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"), "LOFT_CLAUDE_HOME": str(self.claude),
            "LOFT_CSWAP_ROOT": str(self.tmp / "cswap"), "USER": "tester"})
        self.env.start()
        self.plat = mock.patch.object(paths, "PLATFORM", "linux")
        self.plat.start()
        self.work_dir = str(self.tmp / "logins" / "work")
        os.makedirs(self.work_dir)

    def tearDown(self):
        self.plat.stop()
        self.env.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cfg3(self):
        return A.save({"default": "personal", "accounts": [
            {"id": "personal", "label": "Personal", "color": "green", "creds_dir": "default"},
            {"id": "work", "label": "Work", "color": "blue", "creds_dir": self.work_dir},
            {"id": "side", "label": "Side", "color": "purple", "creds_dir": str(self.tmp / "logins" / "side")}]})

    def job(self, jid, env=None, **st):
        d = self.claude / "jobs" / jid
        d.mkdir(parents=True)
        state = {"name": jid, "state": "done", **st}
        if env is not None:
            state["providerEnv"] = env
        (d / "state.json").write_text(json.dumps(state))
        return d / "state.json"


class ConfigTests(Env):
    def test_implicit_config_before_setup(self):
        self.assertFalse(A.configured())
        cfg = A.load()
        self.assertEqual([a["creds_dir"] for a in cfg["accounts"]], ["default"])
        self.assertEqual(cfg["default"], "main")

    def test_save_load_roundtrip_private(self):
        self.cfg3()
        self.assertTrue(A.configured())
        self.assertEqual(oct(A.config_path().stat().st_mode & 0o777), "0o600")
        cfg = A.load()
        self.assertEqual([a["id"] for a in cfg["accounts"]], ["personal", "work", "side"])

    def test_normalize_fixes_bad_entries(self):
        cfg = A.normalize({"default": "nope", "accounts": [
            {"id": "a", "color": "chartreuse"}, {"id": "a"}, {"label": "no id"}, {"id": "b", "creds_dir": "/x"}]})
        self.assertEqual([a["id"] for a in cfg["accounts"]], ["a", "b"])
        self.assertIn(cfg["accounts"][0]["color"], A.COLORS)
        self.assertEqual(cfg["accounts"][0]["creds_dir"], "default")
        self.assertEqual(cfg["default"], "a")  # falls back to the default-login account

    def test_xdg_config_home_respected(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp / "xdg")}):
            os.environ.pop("LOFT_CONF_DIR")
            self.assertEqual(A.config_path(), self.tmp / "xdg" / "loft" / "accounts.json")

    def test_unique_ids_and_colors(self):
        cfg = self.cfg3()
        self.assertEqual(A.unique_id("Work", cfg), "work-2")
        self.assertEqual(A.next_color(cfg), "cyan")


class EnvBuildingTests(Env):
    def test_linux_default_is_explicit_path(self):
        cfg = self.cfg3()
        env = A.apply_env({"CLAUDE_CONFIG_DIR": "/leak"}, A.get("personal", cfg))
        self.assertEqual(env[A.VAR], str(self.claude))
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)

    def test_macos_default_is_defined_but_empty(self):
        cfg = self.cfg3()
        with mock.patch.object(paths, "PLATFORM", "darwin"):
            env = A.apply_env({}, A.get("personal", cfg))
        self.assertIn(A.VAR, env)
        self.assertEqual(env[A.VAR], "")

    def test_named_dir_same_on_both(self):
        cfg = self.cfg3()
        for plat in ("linux", "darwin"):
            with mock.patch.object(paths, "PLATFORM", plat):
                self.assertEqual(A.env_value(A.get("work", cfg)), self.work_dir)

    def test_account_env_never_absent(self):
        self.cfg3()
        with mock.patch.dict(os.environ, {A.VAR: self.work_dir, "CLAUDE_CONFIG_DIR": "/x"}):
            env = B.account_env({"account": "personal"})
        self.assertEqual(env[A.VAR], str(self.claude))
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)

    def test_legacy_bool_still_resolves(self):
        self.cfg3()
        self.assertEqual(B.resolve_account(True)["id"], "work")
        self.assertEqual(B.resolve_account(False)["id"], "personal")
        self.assertEqual(B.resolve_account("side")["id"], "side")
        self.assertEqual(B.resolve_account("gone")["id"], "personal")


class DetectionTests(Env):
    def test_detect_each_account(self):
        cfg = self.cfg3()
        self.assertEqual(A.account_of_env({A.VAR: str(self.claude)}, cfg)["id"], "personal")
        self.assertEqual(A.account_of_env({A.VAR: str(self.claude) + "/"}, cfg)["id"], "personal")
        self.assertEqual(A.account_of_env({A.VAR: ""}, cfg)["id"], "personal")  # macOS-style default
        self.assertEqual(A.account_of_env({A.VAR: self.work_dir}, cfg)["id"], "work")
        self.assertEqual(A.account_of_env({A.VAR: str(self.tmp / "logins" / "side")}, cfg)["id"], "side")
        self.assertEqual(A.account_of_env({"CLAUDE_CONFIG_DIR": self.work_dir}, cfg)["id"], "work")

    def test_unknown_and_missing(self):
        cfg = self.cfg3()
        self.assertIsNone(A.account_of_env({A.VAR: "/somewhere/else"}, cfg))
        self.assertEqual(A.account_of_env({}, cfg)["id"], "personal")
        self.assertFalse(A.has_saved_login({}))
        self.assertTrue(A.has_saved_login({A.VAR: ""}))

    def test_cards_carry_account_and_work_flag(self):
        self.cfg3()
        self.job("aaaa0001", {A.VAR: self.work_dir})
        self.job("aaaa0002", {A.VAR: str(self.claude)})
        self.job("aaaa0003", {A.VAR: str(self.tmp / "logins" / "side")})
        self.job("aaaa0004", {A.VAR: "/elsewhere"})
        with mock.patch.object(C, "live_sessions", return_value=set()):
            cards = {c["id"]: c for c in B.load_cards(True)}
        self.assertEqual((cards["aaaa0001"]["account"], cards["aaaa0001"]["work"]), ("work", True))
        self.assertEqual((cards["aaaa0002"]["account"], cards["aaaa0002"]["work"]), ("personal", False))
        self.assertEqual(cards["aaaa0003"]["account"], "side")
        self.assertIsNone(cards["aaaa0004"]["account"])

    def test_switch_account_cycles_and_picks(self):
        self.cfg3()
        sf = self.job("bbbb0001", {A.VAR: str(self.claude), "OTHER": "1"})
        card = {"kind": "agent", "id": "bbbb0001", "account": "personal", "live": False, "col": "done"}
        B.act_switch_account(card)
        st = json.loads(sf.read_text())
        self.assertEqual(st["providerEnv"], {A.VAR: self.work_dir, "OTHER": "1"})
        self.assertEqual(st["color"], "blue")
        B.act_switch_account({**card, "account": "work"}, to="side")
        self.assertEqual(json.loads(sf.read_text())["color"], "purple")

    def test_organize_gives_explicit_logins(self):
        cfg = self.cfg3()
        cfg["rules"] = [{"match": r"\bacme\b", "account": "side"}]
        A.save(cfg)
        a = self.job("cccc0001", None, name="acme report")
        b = self.job("cccc0002", None, name="notes")
        with mock.patch.object(C, "live_sessions", return_value=set()):
            C.organize(dry=False, out=lambda *_: None)
        self.assertEqual(json.loads(a.read_text())["providerEnv"][A.VAR], str(self.tmp / "logins" / "side"))
        self.assertEqual(json.loads(b.read_text())["providerEnv"][A.VAR], str(self.claude))
        self.assertEqual(json.loads(b.read_text())["color"], "green")


class KeychainTests(Env):
    def test_service_names(self):
        self.assertEqual(A.keychain_service(""), "Claude Code-credentials")
        d = "/Users/me/.local/share/loft/accounts/work"
        want = "Claude Code-credentials-" + hashlib.sha256(d.encode()).hexdigest()[:8]
        self.assertEqual(A.keychain_service(d), want)

    def test_nfc_normalized(self):
        nfd = unicodedata.normalize("NFD", "/Users/José/x")
        nfc = unicodedata.normalize("NFC", "/Users/José/x")
        self.assertEqual(A.keychain_service(nfd), A.keychain_service(nfc))

    def test_default_account_on_macos_uses_unsuffixed_item(self):
        cfg = self.cfg3()
        with mock.patch.object(paths, "PLATFORM", "darwin"):
            self.assertEqual(A.keychain_service(A.get("personal", cfg)), "Claude Code-credentials")
            self.assertTrue(A.keychain_service(A.get("work", cfg)).startswith("Claude Code-credentials-"))

    def test_macos_reads_keychain_with_security(self):
        cfg = self.cfg3()
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return mock.Mock(returncode=0, stdout=json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": 0}}) + "\n")
        with mock.patch.object(paths, "PLATFORM", "darwin"), mock.patch("subprocess.run", fake_run):
            info = U.token_info(A.get("work", cfg))
        self.assertEqual(info["token"], "t")
        self.assertEqual(calls[0][:2], ["/usr/bin/security", "find-generic-password"])
        self.assertIn("tester", calls[0])
        self.assertIn(A.keychain_service(self.work_dir), calls[0])

    def test_linux_reads_credentials_file(self):
        cfg = self.cfg3()
        Path(self.work_dir, ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "abc"}}))
        self.assertEqual(U.token_info(A.get("work", cfg))["token"], "abc")
        with self.assertRaises(U.TokenError):
            U.token_info(A.get("side", cfg))

    def test_parse_usage_both_shapes(self):
        self.assertEqual(U.parse_usage({"five_hour": {"utilization": 23.4, "resets_at": "r1"}, "seven_day": None,
                                        "limits": [{"group": "weekly", "percent": 40, "resets_at": "r2"},
                                                   {"group": "weekly", "percent": 61, "resets_at": "r3"}]}),
                         (23, 61, "r1", "r3"))


class CswapTests(Env):
    def make_cswap(self, with_alias=False):
        root = self.tmp / "cswap"
        (root / "sessions" / "1-me_example.com").mkdir(parents=True)
        (root / "sessions" / "2-work_corp.example").mkdir(parents=True)
        accs = {"1": {"email": "me@example.com"}, "2": {"email": "work@corp.example"}, "3": {"email": "never@ran.example"}}
        if with_alias:
            accs["2"]["alias"] = "job"
        (root / "sequence.json").write_text(json.dumps({"accounts": accs}))
        return root

    def test_profiles_parsed(self):
        self.make_cswap(with_alias=True)
        profs = A.cswap_profiles()
        self.assertEqual([p["num"] for p in profs], ["1", "2", "3"])
        self.assertEqual(profs[1]["alias"], "job")
        self.assertIsNone(profs[2]["dir"])

    def test_profiles_without_sequence(self):
        root = self.tmp / "cswap"
        (root / "sessions" / "4-first.last_mail.example").mkdir(parents=True)
        self.assertEqual(A.cswap_profiles()[0]["email"], "first.last@mail.example")

    def test_import_skips_default_and_known(self):
        self.make_cswap()
        cfg = A.save({"default": "me", "accounts": [{"id": "me", "label": "Me", "creds_dir": "default", "email": "me@example.com"}]})
        cfg, log = A.import_cswap(cfg)
        self.assertEqual([a["id"] for a in cfg["accounts"]], ["me", "work"])
        self.assertTrue(cfg["accounts"][1]["creds_dir"].endswith("2-work_corp.example"))
        self.assertTrue(any("same login" in l for l in log))
        self.assertTrue(any("cswap run 3" in l for l in log))
        cfg, log = A.import_cswap(cfg)
        self.assertEqual(len(cfg["accounts"]), 2)

    def test_cswap_root_per_platform(self):
        os.environ.pop("LOFT_CSWAP_ROOT")
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/xdg"}):
            self.assertEqual(A.cswap_root(), Path("/xdg/claude-swap"))
            with mock.patch.object(paths, "PLATFORM", "darwin"):
                self.assertEqual(A.cswap_root(), paths.HOME / ".claude-swap-backup")


class MigrateTests(Env):
    def test_migrate_from_jobs(self):
        with mock.patch.object(A, "legacy_setup", return_value={"work_dir": None, "rule": None}):
            self.job("dddd0001", {A.VAR: self.work_dir})
            self.job("dddd0002", {A.VAR: str(self.claude)})
            self.job("dddd0003", {A.VAR: str(self.tmp / "third")})
            cfg = A.migrate_config()
        ids = [a["id"] for a in cfg["accounts"]]
        self.assertEqual(ids[:2], ["personal", "work"])
        self.assertEqual(cfg["default"], "personal")
        self.assertEqual(A.get("work", cfg)["color"], "blue")
        self.assertEqual(A.get("personal", cfg)["color"], "green")
        self.assertEqual(len(ids), 3)

    def test_migrate_from_legacy_cli(self):
        with mock.patch.object(A, "legacy_setup", return_value={"work_dir": self.work_dir, "rule": "acme"}):
            cfg = A.migrate_config()
        self.assertEqual(A.get("work", cfg)["creds_dir"], self.work_dir)
        self.assertEqual(cfg["rules"], [{"match": "acme", "account": "work"}])

    def test_legacy_parser(self):
        fake_home = self.tmp / "home"
        (fake_home / ".local/bin").mkdir(parents=True)
        (fake_home / ".local/bin/cagents").write_text(
            'WORK_CREDS = Path(os.environ.get("CLAUDE_WORK_CREDS", SWAP / "sessions/9-x_y.z"))\n'
            'AREAS = [\n    ("job", "blue",   r"acme|corp"),\n]\n')
        with mock.patch.object(paths, "HOME", fake_home):
            found = A.legacy_setup()
        self.assertEqual(found["work_dir"], str(self.tmp / "cswap" / "sessions/9-x_y.z"))
        self.assertEqual(found["rule"], "acme|corp")

    def test_existing_jobs_keep_accounts_after_migration(self):
        with mock.patch.object(A, "legacy_setup", return_value={"work_dir": self.work_dir, "rule": None}):
            A.save(A.migrate_config())
        self.job("eeee0001", {A.VAR: self.work_dir})
        self.job("eeee0002", {A.VAR: str(self.claude)})
        with mock.patch.object(C, "live_sessions", return_value=set()):
            cards = {c["id"]: c for c in B.load_cards(True)}
        self.assertEqual(cards["eeee0001"]["account"], "work")
        self.assertTrue(cards["eeee0001"]["work"])
        self.assertEqual(cards["eeee0002"]["account"], "personal")
        self.assertFalse(cards["eeee0002"]["work"])


if __name__ == "__main__":
    unittest.main()
