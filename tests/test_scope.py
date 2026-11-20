"""Tests for loft/scope.py: agents run in their own memory-capped systemd scopes.

The live tests start real transient scopes with `systemd-run --user` and are skipped where
that isn't available (macOS, CI without a user manager).

  python3 -m unittest tests.test_scope -v
"""
import os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from loft import scope as S  # noqa: E402


def cgroup_of(pid):
    with open(f"/proc/{pid}/cgroup") as f:
        return f.read().strip().splitlines()[-1].split(":", 2)[2]


class Wrap(unittest.TestCase):
    def setUp(self):
        self._ok = S._OK

    def tearDown(self):
        S._OK = self._ok

    def test_unchanged_when_unavailable(self):
        S._OK = False
        self.assertEqual(S.wrap(["claude", "--bg", "x"]), ["claude", "--bg", "x"])

    def test_off_switch(self):
        S._OK = None
        with mock.patch.dict(os.environ, {"LOFT_SCOPE": "0"}):
            self.assertFalse(S.available())
        self.assertEqual(S.wrap(["a"]), ["a"])

    def test_prefix_and_limits(self):
        S._OK = True
        with mock.patch.dict(os.environ, {"LOFT_AGENTS_MEMORY_MAX": "12G"}):
            argv = S.wrap(["claude", "--bg", "costs $5"])
        self.assertTrue(argv[0].endswith("systemd-run"))
        self.assertEqual(argv[-4:], ["--", "claude", "--bg", "costs $5"])
        head = argv[:-4]
        for flag in ("--user", "--scope", "--collect", "--expand-environment=no",
                     "MemoryMax=12G", "MemoryHigh=70%", "MemorySwapMax=2G", "OOMPolicy=continue"):
            self.assertIn(flag, head)

    def test_claude_helper(self):
        S._OK = False
        with mock.patch.dict(os.environ, {"LOFT_CLAUDE": "/opt/claude"}):
            self.assertEqual(S.claude("attach", "ab12"), ["/opt/claude", "attach", "ab12"])

    def test_own_unit(self):
        def unit(text):
            with mock.patch("builtins.open", mock.mock_open(read_data=text)):
                return S.own_unit()
        loft = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-Loft@e4b2.service\n"
        self.assertEqual(unit(loft), "app-Loft@e4b2.service")
        self.assertIsNone(unit("0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-org.kde.konsole-1.scope\n"))
        self.assertIsNone(unit("0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-firefox@x.service\n"))


@unittest.skipUnless(S.available(), "no systemd user manager")
class Live(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_runaway_is_killed_in_its_scope_and_the_daemon_survives(self):
        """The 2026-11-20 crash, small: a 'daemon' and a test that eats memory share a scope.
        The hog is OOM-killed inside the scope; the daemon and this process carry on."""
        pidf, hog = self.tmp / "daemon.pid", self.tmp / "hog.done"
        script = (f"setsid sleep 30 </dev/null >/dev/null 2>&1 & echo $! > {pidf}; "
                  "python3 -c 'b=[bytearray(10**7) for _ in range(60)]' && touch " + str(hog))
        with mock.patch.dict(os.environ, {"LOFT_AGENTS_MEMORY_MAX": "150M", "LOFT_AGENTS_MEMORY_HIGH": "infinity",
                                          "LOFT_AGENTS_SWAP_MAX": "0"}):
            r = subprocess.run(S.wrap(["sh", "-c", script]), capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)          # the hog was killed (status 137)
        self.assertFalse(hog.exists())
        daemon = int(pidf.read_text())
        try:
            os.kill(daemon, 0)                         # still running after the kill
            cg = cgroup_of(daemon)
            self.assertTrue(cg.endswith(".scope"), cg)
            self.assertNotEqual(cg, cgroup_of(os.getpid()))
            self.assertEqual(Path(f"/sys/fs/cgroup{cg}/memory.max").read_text().strip(), str(150 * 2**20))
        finally:
            os.kill(daemon, 9)

    def test_pid_and_arguments_kept(self):
        arg = "a $b ${c} %n \\ end $$ `x`"
        p = subprocess.Popen(S.wrap(["sh", "-c", 'echo $$; printf "%s\\n" "$1"', "x", arg]),
                             stdout=subprocess.PIPE, text=True)
        out = p.communicate(timeout=30)[0].splitlines()
        self.assertEqual(out, [str(p.pid), arg])


if __name__ == "__main__":
    unittest.main()
