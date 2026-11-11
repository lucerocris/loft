"""Accounts: which Claude login each agent bills to.

Config lives in <config>/accounts.json:

  {
    "version": 1,
    "default": "personal",
    "accounts": [
      {"id": "personal", "label": "Personal", "color": "green", "creds_dir": "default", "email": "..."},
      {"id": "work", "label": "Work", "color": "blue", "creds_dir": "/abs/path/to/login/dir", "email": "..."}
    ],
    "rules": [{"match": "regex", "account": "work"}]
  }

How an account is selected
--------------------------
Claude Code picks its OAuth credential from CLAUDE_SECURESTORAGE_CONFIG_DIR:

* Linux: the credential is the file <dir>/.credentials.json. The default login is
  ~/.claude, and Loft always writes that path explicitly. Never leave the variable
  out: the claude daemon inherits the env of whichever shell started it, so an
  agent with no saved login silently bills whatever account that shell had.
* macOS: the credential is a Keychain item. For a dir it is
  "Claude Code-credentials-<sha256(NFC(dir string))[:8]>"; the default login is the
  unsuffixed "Claude Code-credentials", selected with the variable defined but EMPTY.
  An explicit ~/.claude path would hash to a different (empty) item, i.e. logged out.

Each background agent's state.json stores the env it was launched with in
`providerEnv`, and the daemon reapplies it on respawn, so the login sticks.
"""
import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path

from . import paths

VAR = "CLAUDE_SECURESTORAGE_CONFIG_DIR"
DEFAULT_CREDS = "default"

# Color names are the ones `claude agents` and /color understand, so the color we
# write into state.json shows up there too. Hex values are Loft's palette.
COLORS = {
    "blue": "#5B8DFF", "green": "#46C07D", "purple": "#A98BFF", "cyan": "#3EC1D3",
    "pink": "#F17DB8", "orange": "#F2924A", "yellow": "#E3CF5B", "red": "#F0685A",
}
PALETTE = ["blue", "green", "purple", "cyan", "pink", "orange", "yellow", "red"]


# ------------------------------------------------------------------ config file

def config_path():
    return paths.config_dir() / "accounts.json"


def configured():
    return config_path().is_file()


def implicit_config():
    """What Loft uses before setup: the default login as one account."""
    return {"version": 1, "default": "main",
            "accounts": [{"id": "main", "label": "Main", "color": "blue", "creds_dir": DEFAULT_CREDS}],
            "rules": []}


def load():
    try:
        cfg = json.loads(config_path().read_text())
    except FileNotFoundError:
        return implicit_config()
    except Exception as e:
        raise SystemExit(f"{config_path()} is not valid JSON ({e}). Fix or delete it, then run `loft setup`.")
    return normalize(cfg)


def normalize(cfg):
    cfg = dict(cfg or {})
    accs, seen = [], set()
    for i, a in enumerate(cfg.get("accounts") or []):
        if not isinstance(a, dict) or not a.get("id") or a["id"] in seen:
            continue
        seen.add(a["id"])
        a = dict(a)
        a.setdefault("label", a["id"].title())
        if a.get("color") not in COLORS:
            a["color"] = PALETTE[i % len(PALETTE)]
        a["creds_dir"] = a.get("creds_dir") or DEFAULT_CREDS
        accs.append(a)
    cfg["accounts"] = accs
    cfg["version"] = 1
    cfg["rules"] = [r for r in (cfg.get("rules") or []) if isinstance(r, dict) and r.get("match") and r.get("account")]
    if cfg.get("default") not in seen:
        dflt = next((a["id"] for a in accs if a["creds_dir"] == DEFAULT_CREDS), None)
        cfg["default"] = dflt or (accs[0]["id"] if accs else None)
    return cfg


def save(cfg):
    cfg = normalize(cfg)
    paths.write_private(config_path(), json.dumps(cfg, indent=2) + "\n")
    return cfg


def all_accounts(cfg=None):
    return (cfg or load())["accounts"]


def get(acc_id, cfg=None):
    cfg = cfg or load()
    return next((a for a in cfg["accounts"] if a["id"] == acc_id), None)


def default_account(cfg=None):
    cfg = cfg or load()
    return get(cfg.get("default"), cfg) or (cfg["accounts"][0] if cfg["accounts"] else None)


def slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:32] or "account"


def unique_id(name, cfg):
    base, n, ids = slug(name), 2, {a["id"] for a in cfg["accounts"]}
    out = base
    while out in ids:
        out = f"{base}-{n}"; n += 1
    return out


def next_color(cfg):
    used = {a["color"] for a in cfg["accounts"]}
    return next((c for c in PALETTE if c not in used), PALETTE[len(cfg["accounts"]) % len(PALETTE)])


def account_dir(acc_id):
    """Where `loft account add` keeps a new login."""
    return paths.data_dir() / "accounts" / acc_id


# ------------------------------------------------------------------ env per account

def default_dir():
    return str(paths.claude_home())


def env_value(acc):
    """The exact CLAUDE_SECURESTORAGE_CONFIG_DIR value for an account."""
    if acc["creds_dir"] == DEFAULT_CREDS:
        return "" if paths.is_mac() else default_dir()
    return os.path.expanduser(acc["creds_dir"])


def apply_env(env, acc):
    """Point an env dict at an account's login. Always explicit (see module doc)."""
    env.pop("CLAUDE_CONFIG_DIR", None)
    env[VAR] = env_value(acc)
    return env


def _norm(p):
    return os.path.normpath(os.path.expanduser(p)) if p else p


def account_for_value(value, cfg=None, present=True):
    """Which account a saved CLAUDE_SECURESTORAGE_CONFIG_DIR value belongs to.

    present=False means the variable is absent: the agent inherits the daemon's
    login, which we can't know, so it is treated as the default account (and
    `loft organize` pins it down)."""
    cfg = cfg or load()
    if not present:
        return default_account(cfg)
    if value == "" or _norm(value) == _norm(default_dir()):
        return next((a for a in cfg["accounts"] if a["creds_dir"] == DEFAULT_CREDS), None)
    v = _norm(value)
    return next((a for a in cfg["accounts"] if a["creds_dir"] != DEFAULT_CREDS and _norm(a["creds_dir"]) == v), None)


def account_of_env(env, cfg=None):
    """Account for a job's providerEnv. None = a login Loft doesn't know."""
    env = env or {}
    if VAR in env:
        return account_for_value(env[VAR], cfg)
    if env.get("CLAUDE_CONFIG_DIR"):  # e.g. a `cswap run` profile
        return account_for_value(env["CLAUDE_CONFIG_DIR"], cfg)
    return account_for_value(None, cfg, present=False)


def has_saved_login(env):
    env = env or {}
    return VAR in env or bool(env.get("CLAUDE_CONFIG_DIR"))


def rule_account(text, cfg=None):
    """Account named by the first matching rule (for agents with no saved login)."""
    cfg = cfg or load()
    for r in cfg.get("rules") or []:
        try:
            if re.search(r["match"], text or "", re.I) and get(r["account"], cfg):
                return get(r["account"], cfg)
        except re.error:
            continue
    return None


# ------------------------------------------------------------------ macOS keychain naming

def keychain_service(acc_or_value):
    """Keychain service Claude Code uses for an account (macOS)."""
    v = env_value(acc_or_value) if isinstance(acc_or_value, dict) else acc_or_value
    if not v:
        return "Claude Code-credentials"
    digest = hashlib.sha256(unicodedata.normalize("NFC", str(v)).encode("utf-8")).hexdigest()[:8]
    return f"Claude Code-credentials-{digest}"


def keychain_account():
    """Keychain account name, mirroring Claude Code ($USER, then the OS user)."""
    if os.environ.get("USER"):
        return os.environ["USER"]
    try:
        import pwd
        return pwd.getpwuid(os.geteuid()).pw_name
    except Exception:
        return "claude-code-user"


# ------------------------------------------------------------------ cswap (claude-swap)

def cswap_root():
    """claude-swap's data root (mirrors claude_swap/paths.py get_backup_root)."""
    if os.environ.get("LOFT_CSWAP_ROOT"):
        return Path(os.path.expanduser(os.environ["LOFT_CSWAP_ROOT"]))
    if not paths.is_mac():
        xdg = os.environ.get("XDG_DATA_HOME", "")
        if xdg and os.path.isabs(os.path.expanduser(xdg)):
            return Path(os.path.expanduser(xdg)) / "claude-swap"
        return paths.HOME / ".local/share/claude-swap"
    return paths.HOME / ".claude-swap-backup"


def cswap_profiles(root=None):
    """cswap accounts that have a session dir Loft can point at.

    Returns [{num, email, dir, alias}] read from sequence.json plus the
    sessions/<num>-<email-slug>/ dirs. Accounts cswap knows but never ran with
    `cswap run N` have no session dir yet; they come back with dir=None."""
    root = Path(root) if root else cswap_root()
    seq = {}
    try:
        seq = json.loads((root / "sequence.json").read_text()).get("accounts") or {}
    except Exception:
        pass
    sessions = {}
    for d in sorted((root / "sessions").glob("*/")) if (root / "sessions").is_dir() else []:
        m = re.match(r"(\d+)-(.+)$", d.name)
        if m:
            sessions[m.group(1)] = d
    out = []
    for num in sorted(set(seq) | set(sessions), key=lambda n: int(n) if str(n).isdigit() else 0):
        info = seq.get(num) or {}
        d = sessions.get(str(num))
        email = info.get("email") or (_email_from_slug(d.name.split("-", 1)[1]) if d else "")
        out.append({"num": str(num), "email": email, "dir": str(d) if d else None,
                    "alias": info.get("alias") or ""})
    return out


def _email_from_slug(s):
    # best effort: cswap replaced "@" (and "+") with "_"; the last "_" before a dot-domain was "@"
    m = re.match(r"(.+)_([^_]+\.[^_]+)$", s)
    return f"{m.group(1)}@{m.group(2)}" if m else s


def import_cswap(cfg, profiles=None, default_email=None, only=None):
    """Add cswap profiles as accounts (pointing at their session dirs; no secrets are
    copied). Skips ones already configured, ones without a session dir, and the
    one that is the same login as the default account. Returns (cfg, log lines)."""
    profiles = cswap_profiles() if profiles is None else profiles
    log = []
    known = {_norm(a["creds_dir"]) for a in cfg["accounts"] if a["creds_dir"] != DEFAULT_CREDS}
    emails = {(a.get("email") or "").lower() for a in cfg["accounts"]} - {""}
    if default_email:
        emails.add(default_email.lower())
    for p in profiles:
        if only and p["num"] not in only and p["email"] not in only:
            continue
        name = p["alias"] or p["email"].split("@")[0] or f"cswap {p['num']}"
        if not p["dir"]:
            log.append(f"skip #{p['num']} {p['email']}: no session yet (run `cswap run {p['num']}` once, then import again)")
            continue
        if _norm(p["dir"]) in known:
            log.append(f"skip #{p['num']} {p['email']}: already an account")
            continue
        if p["email"] and p["email"].lower() in emails:
            log.append(f"skip #{p['num']} {p['email']}: same login as an existing account")
            continue
        acc = {"id": unique_id(name, cfg), "label": name, "color": next_color(cfg),
               "creds_dir": p["dir"], "email": p["email"], "source": f"cswap #{p['num']}"}
        cfg["accounts"].append(acc)
        known.add(_norm(p["dir"])); emails.add(p["email"].lower())
        log.append(f"added {acc['label']} ({p['email']}) -> {p['dir']}")
    return cfg, log


# ------------------------------------------------------------------ migration from the two-account setup

def legacy_setup():
    """Read the old hardcoded pair (cagents CLI + jobs on disk) without running it."""
    found = {"work_dir": None, "rule": None}
    old = paths.HOME / ".local/bin/cagents"
    try:
        text = old.read_text()
        m = re.search(r'WORK_CREDS\s*=\s*Path\(os\.environ\.get\("CLAUDE_WORK_CREDS",\s*SWAP\s*/\s*"([^"]+)"\)\)', text)
        if m:
            found["work_dir"] = str(cswap_root() / m.group(1))
        m = re.search(r'\("[^"]+",\s*"blue",\s*r"([^"]+)"\)', text)  # the first (work) area regex
        if m:
            found["rule"] = m.group(1)
    except OSError:
        pass
    if os.environ.get("CLAUDE_WORK_CREDS"):
        found["work_dir"] = os.environ["CLAUDE_WORK_CREDS"]
    return found


def migrate_config(jobs_dir=None):
    """accounts.json for a machine that ran the old Work/Personal build:
    Personal = the default login (green), Work = the work login dir (blue), plus
    any other login dir that existing agents use. Ids stay "work"/"personal" so
    saved todos and filters keep working."""
    legacy = legacy_setup()
    jobs_dir = Path(jobs_dir) if jobs_dir else paths.claude_home() / "jobs"
    dirs = []
    for sf in sorted(jobs_dir.glob("*/state.json")):
        try:
            v = (json.loads(sf.read_text()).get("providerEnv") or {}).get(VAR)
        except Exception:
            continue
        if v and _norm(v) != _norm(default_dir()) and _norm(v) not in [_norm(d) for d in dirs]:
            dirs.append(v)
    work = legacy["work_dir"] or (dirs[0] if dirs else None)
    cfg = {"version": 1, "default": "personal", "rules": [],
           "accounts": [{"id": "personal", "label": "Personal", "color": "green", "creds_dir": DEFAULT_CREDS}]}
    if work:
        cfg["accounts"].append({"id": "work", "label": "Work", "color": "blue", "creds_dir": work})
        if legacy["rule"]:
            cfg["rules"].append({"match": legacy["rule"], "account": "work"})
    for d in dirs:
        if work and _norm(d) == _norm(work):
            continue
        name = Path(d).name
        cfg["accounts"].append({"id": unique_id(name, cfg), "label": name, "color": next_color(cfg), "creds_dir": d})
    return normalize(cfg)


# ------------------------------------------------------------------ public view

def public(acc, cfg=None):
    cfg = cfg or load()
    return {"id": acc["id"], "label": acc["label"], "color": acc["color"], "hex": COLORS[acc["color"]],
            "email": acc.get("email") or "", "default": acc["id"] == cfg.get("default"),
            "defaultLogin": acc["creds_dir"] == DEFAULT_CREDS,
            "credsDir": "default" if acc["creds_dir"] == DEFAULT_CREDS else acc["creds_dir"].replace(str(paths.HOME), "~"),
            "source": acc.get("source") or ""}
