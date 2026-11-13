"""Folder browsing for the New task / project folder picker (GET /api/fs).

Directories only, never file contents, and never outside the user's home:

    listdir("~/code") -> {"path": "/home/me/code", "display": "~/code", "parent": "/home/me",
                          "dirs": [{"name", "path", "display", "git", "web"}, ...],
                          "recent": [...], "truncated": False}

`path` may start with ~ (default ~). Symlinks are resolved; a link that leads out of
home is left out of the list, and asking for one gets FsError(403). Hidden folders
(.name) are left out unless hidden=True. `recent` (same item shape as `dirs`) is only
filled at the top level (home), from the paths the caller passes in.
"""
import os
from pathlib import Path

LIMIT = 500
WEB_MARKERS = ("package.json", "index.html")   # "web": looks like something a browser shows


class FsError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _home(home=None):
    return Path(home or Path.home()).resolve()


def display(p, home=None):
    h, s = str(_home(home)), str(p)
    return "~" if s == h else "~" + s[len(h):] if s.startswith(h + os.sep) else s


def _inside(p, home):
    return p == home or home in p.parents


def _expand(path, home):
    raw = str(path or "~").strip() or "~"
    return str(home) + raw[1:] if raw == "~" or raw.startswith("~/") else raw


def resolve(path, home=None):
    """'~', '~/x' or an absolute path -> resolved Path inside home, else FsError."""
    home = _home(home)
    raw = _expand(path, home)
    if not os.path.isabs(raw):
        raise FsError(400, "Give a full path or one starting with ~.")
    try:
        if not _inside(Path(raw).resolve(), home):   # outside home: 403 whether it exists or not
            raise FsError(403, "Only folders inside your home folder can be browsed.")
        p = Path(raw).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise FsError(404, f"No such folder: {display(raw, home)}")
    if not _inside(p, home):
        raise FsError(403, "Only folders inside your home folder can be browsed.")
    if not p.is_dir():
        raise FsError(400, f"Not a folder: {display(p, home)}")
    return p


def item(p, home=None):
    p = Path(p)
    return {"name": p.name or str(p), "path": str(p), "display": display(p, home),
            "git": os.path.exists(p / ".git"), "web": any(os.path.isfile(p / m) for m in WEB_MARKERS)}


def listdir(path="~", hidden=False, home=None, limit=LIMIT, recent=()):
    home = _home(home)
    p = resolve(path, home)
    names = []
    try:
        with os.scandir(p) as it:
            for e in it:
                if not hidden and e.name.startswith("."):
                    continue
                try:
                    if e.is_dir():   # follows symlinks
                        names.append(e.name)
                except OSError:
                    continue
    except PermissionError:
        raise FsError(403, f"No permission to read {display(p, home)}")
    except OSError as e:
        raise FsError(400, f"Couldn't read {display(p, home)}: {e.strerror or e}")
    names.sort(key=lambda n: (n.lower(), n))
    dirs, truncated = [], False
    for n in names:
        try:
            t = (p / n).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not _inside(t, home) or not t.is_dir():
            continue
        if len(dirs) >= limit:
            truncated = True
            break
        it = item(t, home)
        it["name"] = n   # the name as it appears here, even when it is a link
        dirs.append(it)
    rec = []
    if p == home:
        seen = set()
        for r in recent or ():
            try:
                t = Path(_expand(r, home)).resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                continue
            if t in seen or t == home or not _inside(t, home) or not t.is_dir():
                continue
            seen.add(t); rec.append(item(t, home))
    return {"path": str(p), "display": display(p, home), "parent": None if p == home else str(p.parent),
            "dirs": dirs, "recent": rec, "truncated": truncated}
