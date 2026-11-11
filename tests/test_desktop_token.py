"""The desktop page's token survives a restart, so open Loft windows keep working."""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def token(conf):
    env = {**os.environ, "LOFT_CONF_DIR": str(conf)}
    out = subprocess.run([sys.executable, "-c", "import server; print(server.TOKEN)"],
                         cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    return out.stdout.strip().splitlines()[-1]


class DesktopToken(unittest.TestCase):
    def setUp(self):
        self.conf = Path(tempfile.mkdtemp(prefix="loft-token-"))

    def tearDown(self):
        shutil.rmtree(self.conf, ignore_errors=True)

    def test_same_token_after_restart(self):
        first = token(self.conf)
        self.assertRegex(first, r"^[0-9a-f]{32}$")
        self.assertEqual(token(self.conf), first)

    def test_stored_private(self):
        token(self.conf)
        mode = stat.S_IMODE((self.conf / "desktop-token").stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_bad_file_is_replaced(self):
        (self.conf / "desktop-token").write_text("not a token\n")
        tok = token(self.conf)
        self.assertRegex(tok, r"^[0-9a-f]{32}$")
        self.assertEqual((self.conf / "desktop-token").read_text().strip(), tok)


if __name__ == "__main__":
    unittest.main()
