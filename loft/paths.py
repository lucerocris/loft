"""Where things live. Everything is derived from the environment, nothing is
specific to one machine.

  config   $LOFT_CONF_DIR | $XDG_CONFIG_HOME/loft | ~/.config/loft
  data     $XDG_DATA_HOME/loft | ~/.local/share/loft    (account logins, voice venv)
  cache    $XDG_CACHE_HOME/loft | ~/.cache/loft          (usage numbers)
  claude   $LOFT_CLAUDE_HOME | ~/.claude                  (jobs/, daemon/, the default login)
"""
import os
import shutil
import sys
from pathlib import Path

HOME = Path.home()
REPO = Path(__file__).resolve().parent.parent

# Tests flip this to "darwin" to exercise the macOS rules on Linux.
PLATFORM = sys.platform


def is_mac():
    return PLATFORM == "darwin"


def _xdg(var, fallback):
    v = os.environ.get(var, "")
    if v and os.path.isabs(os.path.expanduser(v)):
        return Path(os.path.expanduser(v))
    return HOME / fallback


def config_dir():
    if os.environ.get("LOFT_CONF_DIR"):
        return Path(os.path.expanduser(os.environ["LOFT_CONF_DIR"]))
    return _xdg("XDG_CONFIG_HOME", ".config") / "loft"


def data_dir():
    return _xdg("XDG_DATA_HOME", ".local/share") / "loft"


def cache_dir():
    return _xdg("XDG_CACHE_HOME", ".cache") / "loft"


def claude_home():
    """The shared Claude Code dir that holds jobs/ and the daemon. Deliberately not
    $CLAUDE_CONFIG_DIR: Loft may be started from a shell that has another
    profile's config dir set, and the agents view always lives in ~/.claude."""
    return Path(os.path.expanduser(os.environ.get("LOFT_CLAUDE_HOME") or str(HOME / ".claude")))


def claude_bin():
    """Path to the claude CLI (LOFT_CLAUDE overrides)."""
    if os.environ.get("LOFT_CLAUDE"):
        return os.environ["LOFT_CLAUDE"]
    found = shutil.which("claude")
    if found:
        return found
    for p in (HOME / ".local/bin/claude", HOME / ".claude/local/claude", Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude")):
        if p.exists():
            return str(p)
    return "claude"


def venv_python():
    return data_dir() / "venv" / "bin" / "python"


def write_private(path, text):
    """Atomic write, mode 0600, parent dir 0700."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)
