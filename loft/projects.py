"""Projects: how a person groups agents, independent of which account bills them.

Config lives in <config>/loft.json (next to accounts.json):

  {
    "version": 1,
    "projects": [{"id": "acme", "name": "Acme", "color": "orange",
                  "accounts": ["work", "side"],      # allowed accounts, [] = any
                  "defaultAccount": "auto",          # an account id, "auto", or null
                  "folders": ["/home/me/code/acme"]}],
    "assignments": {"<card id>": "<project id>"},    # manual moves, win over folders
    "view": {"groupBy": "status"}                    # the server's default grouping
  }

A card belongs to: its manual assignment, else the project whose folder holds
the card's working folder (the longest folder wins), else nothing ("Unsorted").

"auto" as a default account means: of the project's allowed accounts (all
accounts when it allows any), the one with the lowest 5-hour usage. Accounts
with unknown usage come after ones with a known number.

Every write takes an exclusive lock on loft.json.lock, so the CLI and the
server (and a phone) can't lose each other's changes.
"""
import contextlib
import fcntl
import json
import os
import re

from . import accounts as A
from . import paths

GROUP_BY = ("status", "account", "project", "folder", "none")
HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def valid_color(c):
    """A palette name (like account colors) or "#RRGGBB"."""
    return isinstance(c, str) and (c in A.COLORS or bool(HEX_RE.match(c)))
AUTO = "auto"


def config_path():
    return paths.config_dir() / "loft.json"


def empty():
    return {"version": 1, "projects": [], "assignments": {}, "view": {"groupBy": "status"}}


def _norm_folder(p):
    p = os.path.expanduser(str(p or "").strip())
    if not p:
        return ""
    return os.path.normpath(p) if os.path.isabs(p) else ""


def normalize(cfg, acc_ids=None):
    """Clean a loft.json dict. acc_ids: known account ids (unknown ones are dropped);
    None keeps whatever is there (used when accounts can't be read)."""
    cfg = dict(cfg or {})
    out = empty()
    seen = set()
    for i, p in enumerate(cfg.get("projects") or []):
        if not isinstance(p, dict) or not p.get("id") or p["id"] in seen or p["id"] == AUTO:
            continue
        seen.add(p["id"])
        accs = [a for a in (p.get("accounts") or []) if isinstance(a, str) and (acc_ids is None or a in acc_ids)]
        accs = list(dict.fromkeys(accs))
        dflt = p.get("defaultAccount")
        if dflt != AUTO and (not isinstance(dflt, str) or (acc_ids is not None and dflt not in acc_ids) or (accs and dflt not in accs)):
            dflt = None
        folders = list(dict.fromkeys(f for f in (_norm_folder(x) for x in (p.get("folders") or [])) if f))
        q = {"id": str(p["id"]), "name": str(p.get("name") or p["id"]).strip()[:60] or p["id"],
             "color": p.get("color") if valid_color(p.get("color")) else A.PALETTE[i % len(A.PALETTE)],
             "accounts": accs, "defaultAccount": dflt, "folders": folders}
        if p.get("emoji"):
            q["emoji"] = str(p["emoji"])[:8]
        out["projects"].append(q)
    out["assignments"] = {str(k): v for k, v in (cfg.get("assignments") or {}).items() if v in seen}
    g = (cfg.get("view") or {}).get("groupBy")
    out["view"]["groupBy"] = g if g in GROUP_BY else "status"
    return out


def _acc_ids():
    try:
        return {a["id"] for a in A.load()["accounts"]}
    except SystemExit:
        return None


def load():
    try:
        raw = json.loads(config_path().read_text())
    except FileNotFoundError:
        return empty()
    except Exception as e:
        raise SystemExit(f"{config_path()} is not valid JSON ({e}). Fix or delete it.")
    return normalize(raw, _acc_ids())


def save(cfg):
    cfg = normalize(cfg, _acc_ids())
    paths.write_private(config_path(), json.dumps(cfg, indent=2) + "\n")
    return cfg


@contextlib.contextmanager
def locked():
    """with locked() as cfg: mutate cfg; it is saved on exit (under an flock)."""
    lock = config_path().with_name("loft.json.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        cfg = load()
        yield cfg
        save(cfg)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ------------------------------------------------------------------ lookups

def get(pid, cfg=None):
    cfg = cfg or load()
    return next((p for p in cfg["projects"] if p["id"] == pid), None)


def find(key, cfg=None):
    """A project by id, or by name (case-insensitive)."""
    cfg = cfg or load()
    k = (key or "").strip().lower()
    return get(key, cfg) or next((p for p in cfg["projects"] if p["name"].lower() == k), None)


def slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:32] or "project"


def unique_id(name, cfg):
    base, n, ids = slug(name), 2, {p["id"] for p in cfg["projects"]} | {AUTO}
    out = base
    while out in ids:
        out = f"{base}-{n}"; n += 1
    return out


def next_color(cfg):
    used = {p["color"] for p in cfg["projects"]}
    return next((c for c in A.PALETTE if c not in used), A.PALETTE[len(cfg["projects"]) % len(A.PALETTE)])


def folder_match(folder, cfg):
    """The project whose folder holds `folder` (longest folder wins), or None."""
    f = _norm_folder(folder)
    if not f:
        return None
    best, best_len = None, -1
    for p in cfg["projects"]:
        for root in p["folders"]:
            if (f == root or f.startswith(root.rstrip("/") + "/")) and len(root) > best_len:
                best, best_len = p, len(root)
    return best


def project_of(card_id, folder, cfg):
    """Project id for a card: manual assignment > longest folder match > None."""
    pid = cfg["assignments"].get(card_id)
    if pid and get(pid, cfg):
        return pid
    p = folder_match(folder, cfg)
    return p["id"] if p else None


# ------------------------------------------------------------------ edits

def _apply(p, body, cfg):
    if "name" in body:
        name = str(body.get("name") or "").strip()
        if not name:
            raise ValueError("A project needs a name.")
        p["name"] = name[:60]
    if "color" in body:
        if not valid_color(body["color"]):
            raise ValueError("Unknown color.")
        p["color"] = body["color"]
    if "emoji" in body:
        p["emoji"] = str(body.get("emoji") or "")[:8]
    if "accounts" in body:
        p["accounts"] = [a for a in (body.get("accounts") or []) if isinstance(a, str)]
    if "defaultAccount" in body:
        p["defaultAccount"] = body.get("defaultAccount") or None
    if "folders" in body:
        f = body.get("folders") or []
        p["folders"] = [x for x in (f if isinstance(f, list) else [f]) if isinstance(x, str)]
    return p


def create(body, cfg):
    name = str(body.get("name") or "").strip()
    if not name:
        raise ValueError("A project needs a name.")
    if any(p["name"].lower() == name.lower() for p in cfg["projects"]):
        raise ValueError(f"There is already a project called {name}.")
    p = {"id": unique_id(name, cfg), "name": name, "color": next_color(cfg), "accounts": [],
         "defaultAccount": None, "folders": []}
    _apply(p, {**body, "name": name}, cfg)
    cfg["projects"].append(p)
    return p


def update(pid, body, cfg):
    p = get(pid, cfg)
    if not p:
        raise KeyError(pid)
    name = str(body.get("name") or "").strip()
    if name and any(q["id"] != pid and q["name"].lower() == name.lower() for q in cfg["projects"]):
        raise ValueError(f"There is already a project called {name}.")
    return _apply(p, body, cfg)


def remove(pid, cfg):
    before = len(cfg["projects"])
    cfg["projects"] = [p for p in cfg["projects"] if p["id"] != pid]
    cfg["assignments"] = {k: v for k, v in cfg["assignments"].items() if v != pid}
    return len(cfg["projects"]) < before


def assign(card_id, pid, cfg):
    """Pin a card to a project (pid None/"" clears the manual pin)."""
    if pid:
        if not get(pid, cfg):
            raise KeyError(pid)
        cfg["assignments"][card_id] = pid
    else:
        cfg["assignments"].pop(card_id, None)


# ------------------------------------------------------------------ accounts for new work

def _five(u):
    try:
        return float(u.get("five"))
    except (TypeError, ValueError, AttributeError):
        return None


def auto_account(candidates, usage_of):
    """Of candidate accounts, the one with the lowest 5h usage (unknown last, then config order)."""
    best = None
    for i, a in enumerate(candidates):
        f = _five(usage_of(a))
        key = (f is None, f if f is not None else 0, i)
        if best is None or key < best[0]:
            best = (key, a)
    return best[1] if best else None


def pick_account(explicit, project, acc_cfg, usage_of):
    """Account for a new task: explicit > project's default (auto rule) > global default.

    explicit may be an id or "auto". usage_of(acc) -> {"five": ...}.
    An explicit account outside the project's allowed list is still honoured
    (the person chose it)."""
    accs = [a for a in acc_cfg["accounts"] if not a.get("pending")] or acc_cfg["accounts"]
    allowed = [a for a in accs if project and project["accounts"] and a["id"] in project["accounts"]] or accs
    if explicit and explicit != AUTO:
        acc = A.get(explicit, acc_cfg)
        if acc:
            return acc
    want = explicit if explicit == AUTO else (project or {}).get("defaultAccount")
    if want == AUTO:
        return auto_account(allowed, usage_of)
    if want:
        acc = A.get(want, acc_cfg)
        if acc:
            return acc
    dflt = A.default_account(acc_cfg)
    if project and project["accounts"] and dflt and dflt["id"] not in project["accounts"]:
        return allowed[0]
    return dflt


def public(p, counts=None):
    out = dict(p)
    out["hex"] = A.COLORS.get(p["color"]) or p["color"].upper()
    out["folders"] = [f.replace(str(paths.HOME), "~", 1) if f.startswith(str(paths.HOME)) else f for f in p["folders"]]
    if counts is not None:
        out["counts"] = counts.get(p["id"], {"total": 0, "needs": 0, "running": 0, "todo": 0, "done": 0})
        out["count"] = out["counts"]["total"]
    return out


def count(cards):
    """{project id | None: {total, needs, running, todo, done}}"""
    out = {}
    for c in cards:
        d = out.setdefault(c.get("project"), {"total": 0, "needs": 0, "running": 0, "todo": 0, "done": 0})
        d["total"] += 1
        if c.get("col") in d:
            d[c["col"]] += 1
    return out
