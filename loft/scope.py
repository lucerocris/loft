"""Keep agents and their builds out of Loft's own memory (Linux, systemd).

Opened from the desktop launcher, Loft runs as a systemd user unit (app-Loft@….service), and
everything it starts lands in that unit's cgroup. That includes the Claude daemon, which
the first `claude` command Loft runs starts on demand, and so every agent and every build or
test an agent runs. One runaway test (a node test at 25 GB) then counted as Loft: the kernel
OOM-killed it, and systemd's OOMPolicy=stop for the unit stopped all of Loft and every agent
with it, again and again.

wrap() prefixes a command with `systemd-run --user --scope` so it gets its own scope:
- a scope holds whatever the command leaves behind. For `claude …` that is the daemon if
  this call started it, and with it every agent; otherwise the scope goes when the command
  exits;
- MemoryHigh/MemoryMax/MemorySwapMax cap the scope, so a runaway is killed inside it while
  the rest of the machine still has room;
- OOMPolicy=continue: one process killed there leaves the other agents running.

limit_own_unit() caps Loft's own unit, which now holds only the window, the server and voice.

Off (commands run as before) on macOS, without a systemd user manager, on systemd < 254
(no --expand-environment), or with LOFT_SCOPE=0. Limits: LOFT_AGENTS_MEMORY_HIGH (70%),
LOFT_AGENTS_MEMORY_MAX (80%), LOFT_AGENTS_SWAP_MAX (2G), LOFT_MEMORY_MAX for Loft itself (4G);
values use systemd's syntax (bytes with K/M/G/T, or a percentage of RAM).
"""
import os
import shutil
import subprocess
import sys

_OK = None      # cached probe result


def _flags():
    env = os.environ.get
    return ["--user", "--scope", "--quiet", "--collect",
            "--expand-environment=no",     # never touch "$" in prompts
            "-p", f"MemoryHigh={env('LOFT_AGENTS_MEMORY_HIGH') or '70%'}",
            "-p", f"MemoryMax={env('LOFT_AGENTS_MEMORY_MAX') or '80%'}",
            "-p", f"MemorySwapMax={env('LOFT_AGENTS_SWAP_MAX') or '2G'}",
            "-p", "OOMPolicy=continue"]


def available():
    """True when commands can be put in their own systemd scope (probed once)."""
    global _OK
    if _OK is None:
        _OK = False
        if sys.platform.startswith("linux") and os.environ.get("LOFT_SCOPE", "1") != "0":
            exe = shutil.which("systemd-run")
            if exe:
                try:
                    r = subprocess.run([exe, *_flags(), "--", "true"], stdin=subprocess.DEVNULL,
                                       capture_output=True, timeout=10)
                    _OK = r.returncode == 0
                except (OSError, subprocess.SubprocessError):
                    pass
    return _OK


def wrap(argv):
    """argv run in a new memory-capped systemd scope, or unchanged where that isn't available.
    The command keeps its pid, environment, terminal and arguments (systemd-run execs it)."""
    argv = list(argv)
    if not available():
        return argv
    return [shutil.which("systemd-run") or "systemd-run", *_flags(), "--", *argv]


def own_unit():
    """The systemd unit this process runs in, when it is Loft's own app unit, else None."""
    try:
        with open("/proc/self/cgroup") as f:
            path = f.read().strip().splitlines()[-1].split(":", 2)[2]
    except (OSError, IndexError):
        return None
    unit = path.rstrip("/").rsplit("/", 1)[-1]
    return unit if unit.endswith(".service") and unit.lower().startswith("app-loft") else None


def limit_own_unit():
    """Cap Loft's own unit (window + server + voice). Returns a short note, or None."""
    unit = own_unit()
    if not unit or not available() or not shutil.which("systemctl"):
        return None
    cap = os.environ.get("LOFT_MEMORY_MAX") or "4G"
    try:
        r = subprocess.run(["systemctl", "--user", "set-property", "--runtime", unit, f"MemoryMax={cap}"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return f"{unit}: MemoryMax={cap}" if r.returncode == 0 else None


def claude(*args):
    """argv for `claude <args>` in its own scope: whichever call starts the daemon puts it there."""
    from . import paths
    return wrap([paths.claude_bin(), *args])
