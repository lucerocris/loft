"""Tests for files attached to a new task (loft/attachments.py and its wiring in loft/board.py).

Data goes to a temp dir; no agent is started (spawn_bg is mocked).

  python3 -m unittest tests.test_attachments -v
"""
import json, os, shutil, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from loft import attachments as AT, board as B  # noqa: E402

ACCOUNTS = {"version": 1, "default": "personal", "rules": [], "accounts": [
    {"id": "personal", "label": "Personal", "color": "green", "creds_dir": "default"}]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loft-att-")).resolve()
        conf = self.tmp / "conf"; conf.mkdir()
        (conf / "accounts.json").write_text(json.dumps(ACCOUNTS))
        self.proj = self.tmp / "proj"; self.proj.mkdir()
        self.env = mock.patch.dict(os.environ, {"LOFT_CONF_DIR": str(conf), "XDG_DATA_HOME": str(self.tmp / "data"),
                                                "XDG_CACHE_HOME": str(self.tmp / "cache"), "LOFT_VAULT": "file",
                                                "LOFT_CLAUDE_HOME": str(self.tmp / "claude")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class Store(Base):
    def test_save(self):
        r = AT.save("shot.png", b"\x89PNG data")
        p = Path(r["path"])
        self.assertEqual((p.name, p.read_bytes(), r["size"]), ("shot.png", b"\x89PNG data", 9))
        self.assertEqual(p.parent.parent, AT.root())
        self.assertEqual(p.parent.stat().st_mode & 0o777, 0o700)

    def test_names_cant_escape(self):
        for bad in ("../../etc/passwd", "/abs/x.txt", "..", "", "a\x00b"):
            p = Path(AT.save(bad, b"x")["path"])
            self.assertEqual(p.parent.parent, AT.root(), bad)
        self.assertLessEqual(len(AT.safe_name("x" * 300 + ".png")), 120)
        self.assertTrue(AT.safe_name("x" * 300 + ".png").endswith(".png"))

    def test_limits(self):
        with self.assertRaises(ValueError):
            AT.save("e.txt", b"")
        with mock.patch.object(AT, "MAX_BYTES", 4), self.assertRaises(ValueError):
            AT.save("big.txt", b"12345")

    def test_clean_only_keeps_uploads(self):
        ok = AT.save("a.txt", b"a")["path"]
        outside = self.tmp / "secret.txt"; outside.write_text("s")
        sneaky = str(AT.root() / ".." / ".." / "secret.txt")
        self.assertEqual(AT.clean([ok, str(outside), sneaky, ok, 5, str(AT.root() / "nope.txt")]), [ok])

    def test_prompt(self):
        a, b = AT.save("a.png", b"a")["path"], AT.save("b.pdf", b"b")["path"]
        self.assertEqual(AT.prompt_with("do it", []), "do it")
        one = AT.prompt_with("do it", [a])
        self.assertTrue(one.startswith("do it\n\nAttached file (open it with the Read tool"))
        self.assertIn(f"- {a}", one)
        self.assertIn("Attached files", AT.prompt_with("do it", [a, b]))
        self.assertEqual(AT.add_dir_args([a]), ["--add-dir", str(AT.root())])
        self.assertEqual(AT.add_dir_args([]), [])


class Task(Base):
    def test_todo_keeps_files_and_start_lists_them(self):
        a = AT.save("shot.png", b"img")["path"]
        code, msg, jid, todo = B.create_task("fix the header", str(self.proj), files=[a, "/etc/passwd"])
        self.assertEqual((code, jid, todo["files"]), (200, None, [a]))
        with mock.patch.object(B, "save_todos", lambda t: None), mock.patch.object(B, "load_todos", lambda: [todo]):
            card = next(c for c in B.load_cards(True) if c["id"] == todo["id"])
        self.assertEqual(card["files"], [a])
        seen = {}

        def spawn(cwd, acc, args, prompt, name):
            seen.update(args=args, prompt=prompt)
            return "abcd1234", None
        with mock.patch.object(B, "spawn_bg", spawn), mock.patch.object(B, "agent_extras", lambda *a: (None, ["--x", "1"])):
            jid, msg = B.launch_todo(todo)
        self.assertEqual(jid, "abcd1234")
        self.assertEqual(seen["args"], ["--x", "1", "--add-dir", str(AT.root())])
        self.assertTrue(seen["prompt"].startswith("fix the header\n\nAttached file"))
        self.assertIn(a, seen["prompt"])

    def test_no_files_no_change(self):
        code, _, _, todo = B.create_task("plain", str(self.proj))
        self.assertNotIn("files", todo)
        seen = {}
        with mock.patch.object(B, "spawn_bg", lambda cwd, acc, args, prompt, name: (seen.update(args=args, prompt=prompt), ("abcd1234", None))[1]), \
                mock.patch.object(B, "agent_extras", lambda *a: (None, [])):
            B.launch_todo(todo)
        self.assertEqual((seen["args"], seen["prompt"]), ([], "plain"))


if __name__ == "__main__":
    unittest.main()
