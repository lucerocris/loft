"""Keys: dev keys and tokens agents get as env vars.

(Web logins aren't here: they live in the login bank, loft/sites.py, as a signed-in browser
session, never as a password.) Names and scopes live in a JSON file; every value lives in the OS
keychain:

  metadata   <config>/vault.json   {"version": 1, "keys": [...]}   (no secret values)
  secrets    libsecret (`secret-tool`, Linux) or the login Keychain (`security`, macOS),
             service "loft", account "<kind>:<id>"; or, with neither, <data>/vault-secrets.json (0600)

  key     {id, name: "STRIPE_SECRET_KEY", scope, note, created, updated}
  scope   {"kind": "global"} | {"kind": "account", "id": acc_id} | {"kind": "project", "id": project_id}

What an agent gets (loft/board.py launches it): the keys in scope, as env vars in its shell. A
SessionStart hook (`loft vault hook <ticket>`) writes them to $CLAUDE_ENV_FILE, so the values never
sit in its command line, saved state or transcript (unless it prints them).
"""
import json, os, re, secrets, shutil, subprocess, threading, time
from pathlib import Path

from . import paths

SERVICE = "loft"
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
RESERVED = {"PATH", "HOME", "USER", "SHELL", "PWD", "TERM", "LANG", "TMPDIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR",
            "CLAUDE_CONFIG_DIR", "CLAUDE_ENV_FILE", "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES",
            "PYTHONPATH", "NODE_OPTIONS", "BASH_ENV", "ENV", "PROMPT_COMMAND"}
MAX_ITEMS = 300
MAX_VALUE = 64 * 1024
_LOCK = threading.RLock()


# ------------------------------------------------------------------ secret backends
class Backend:
    name = "?"

    def get(self, ref): raise NotImplementedError
    def set(self, ref, label, value): raise NotImplementedError
    def delete(self, ref): raise NotImplementedError


class SecretTool(Backend):
    """libsecret over D-Bus (GNOME Keyring, KWallet's Secret Service)."""
    name = "keyring"
    label = "the system keyring (Secret Service)"

    def _run(self, args, data=None):
        return subprocess.run(["secret-tool", *args], input=data, capture_output=True, text=True, timeout=20)

    def get(self, ref):
        r = self._run(["lookup", "service", SERVICE, "account", ref])
        return r.stdout if r.returncode == 0 and r.stdout else None

    def set(self, ref, label, value):
        r = self._run(["store", f"--label=Loft: {label}", "service", SERVICE, "account", ref], value)
        if r.returncode:
            raise RuntimeError(f"Couldn't save to the keyring: {(r.stderr or '').strip() or r.returncode}")

    def delete(self, ref):
        self._run(["clear", "service", SERVICE, "account", ref])


class Keychain(Backend):
    """The macOS login Keychain. Values go in over stdin (`security -i`), never on a command line."""
    name = "keychain"
    label = "the macOS Keychain"

    @staticmethod
    def _q(s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def get(self, ref):
        r = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", ref, "-w"],
                           capture_output=True, text=True, timeout=20)
        if r.returncode:
            return None
        out = r.stdout[:-1] if r.stdout.endswith("\n") else r.stdout
        if re.fullmatch(r"(?:[0-9a-f]{2})+", out or "") and "\n" not in out:   # -w prints hex for non-ASCII values
            try:
                raw = bytes.fromhex(out).decode()
                if not raw.isascii():
                    return raw
            except ValueError:
                pass
        return out

    def set(self, ref, label, value):
        if "\n" in value:
            value = value.replace("\r\n", "\n")
        cmd = (f"add-generic-password -U -s {SERVICE} -a {self._q(ref)} -l {self._q('Loft: ' + label)} "
               f"-w {self._q(value)}\n")
        r = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True, timeout=20)
        if r.returncode or "error" in (r.stderr or "").lower():
            raise RuntimeError(f"Couldn't save to the Keychain: {(r.stderr or '').strip() or r.returncode}")

    def delete(self, ref):
        subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", ref],
                       capture_output=True, text=True, timeout=20)


class FileStore(Backend):
    """No keychain (a headless box, or LOFT_VAULT=file): a 0600 file in Loft's data folder. Not encrypted."""
    name = "file"
    label = "a private file in Loft's data folder"

    def path(self):
        return paths.data_dir() / "vault-secrets.json"

    def _all(self):
        try:
            return json.loads(self.path().read_text())
        except Exception:
            return {}

    def get(self, ref):
        return self._all().get(ref)

    def set(self, ref, label, value):
        with _LOCK:
            d = self._all(); d[ref] = value
            paths.write_private(self.path(), json.dumps(d))

    def delete(self, ref):
        with _LOCK:
            d = self._all()
            if d.pop(ref, None) is not None:
                paths.write_private(self.path(), json.dumps(d))


_BACKEND = None


def backend():
    global _BACKEND
    want = os.environ.get("LOFT_VAULT", "")
    if _BACKEND is not None and getattr(_BACKEND, "_want", None) == want:
        return _BACKEND
    if want == "file":
        b = FileStore()
    elif want == "keychain" or (not want and paths.is_mac() and shutil.which("security")):
        b = Keychain()
    elif want == "keyring" or (not want and shutil.which("secret-tool") and os.environ.get("DBUS_SESSION_BUS_ADDRESS",
                                                                                           "x")):
        b = SecretTool()
    else:
        b = FileStore()
    b._want = want
    _BACKEND = b
    return b


# ------------------------------------------------------------------ metadata
def _file():
    return paths.config_dir() / "vault.json"


def load():
    try:
        d = json.loads(_file().read_text())
    except Exception:
        d = {}
    return {"version": 1, "keys": list(d.get("keys") or [])}


def save(d):
    paths.write_private(_file(), json.dumps(d, indent=1))


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def clean_scope(s):
    s = s if isinstance(s, dict) else {}
    kind = s.get("kind") or "global"
    if kind == "global":
        return {"kind": "global"}
    if kind not in ("account", "project"):
        raise ValueError("Scope is global, an account or a project.")
    sid = str(s.get("id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}", sid):
        raise ValueError(f"Pick the {kind} this is for.")
    return {"kind": kind, "id": sid}


def in_scope(item, account=None, project=None):
    s = item.get("scope") or {"kind": "global"}
    k = s.get("kind")
    return k == "global" or (k == "account" and s.get("id") == account) or (k == "project" and s.get("id") == project)


def _ref(kind, iid, part=""):
    return f"{kind}:{iid}" + (f":{part}" if part else "")


def _find(d, kind, iid):
    item = next((x for x in d[kind] if x["id"] == iid), None)
    if not item:
        raise LookupError("It's gone. Refresh.")
    return item


# ------------------------------------------------------------------ keys
def clean_name(name):
    name = str(name or "").strip()
    if not NAME_RE.match(name):
        raise ValueError("A key's name is an env var name: letters, digits and _, not starting with a digit.")
    if name.upper() in RESERVED or name.upper().startswith(("LOFT_", "CLAUDE_", "ANTHROPIC_")):
        raise ValueError(f"{name} is used by the system or by Loft. Pick another name.")
    return name


def _value(v, what="value"):
    if not isinstance(v, str) or not v:
        raise ValueError(f"Paste the {what}.")
    if len(v) > MAX_VALUE:
        raise ValueError(f"That {what} is too long.")
    if "\x00" in v:
        raise ValueError(f"That {what} has a NUL character.")
    return v


def add_key(name, value, scope=None, note=""):
    name, scope, value = clean_name(name), clean_scope(scope), _value(value)
    with _LOCK:
        d = load()
        if len(d["keys"]) >= MAX_ITEMS:
            raise ValueError("Too many keys.")
        if any(k["name"] == name and k.get("scope") == scope for k in d["keys"]):
            raise ValueError(f"{name} already exists for that scope. Edit it instead.")
        k = {"id": secrets.token_hex(4), "name": name, "scope": scope, "note": str(note or "")[:200],
             "created": _now(), "updated": _now()}
        backend().set(_ref("key", k["id"]), name, value)
        d["keys"].append(k); save(d)
        return k


def update_key(kid, name=None, value=None, scope=None, note=None):
    with _LOCK:
        d = load(); k = _find(d, "keys", kid)
        if name is not None:
            k["name"] = clean_name(name)
        if scope is not None:
            k["scope"] = clean_scope(scope)
        if note is not None:
            k["note"] = str(note)[:200]
        if value is not None and value != "":
            backend().set(_ref("key", kid), k["name"], _value(value))
        if any(o is not k and o["name"] == k["name"] and o.get("scope") == k.get("scope") for o in d["keys"]):
            raise ValueError(f"{k['name']} already exists for that scope.")
        k["updated"] = _now(); save(d)
        return k


def remove_key(kid):
    with _LOCK:
        d = load(); _find(d, "keys", kid)
        backend().delete(_ref("key", kid))
        d["keys"] = [k for k in d["keys"] if k["id"] != kid]; save(d)


def key_value(kid):
    return backend().get(_ref("key", kid))


def keys_for(account=None, project=None):
    """{NAME: value} for an agent: a project's key beats an account's, which beats a global one."""
    rank = {"global": 0, "account": 1, "project": 2}
    best = {}
    for k in load()["keys"]:
        if in_scope(k, account, project):
            r = rank[(k.get("scope") or {}).get("kind", "global")]
            if k["name"] not in best or r > best[k["name"]][0]:
                best[k["name"]] = (r, k)
    out = {}
    for name, (_, k) in sorted(best.items()):
        v = key_value(k["id"])
        if v is not None:
            out[name] = v
    return out


def key_names_for(account=None, project=None):
    return sorted({k["name"] for k in load()["keys"] if in_scope(k, account, project)})


def mask(v):
    if not v:
        return ""
    return ("•" * 6) + (v[-4:] if len(v) >= 12 else "")


# ------------------------------------------------------------------ what an agent gets
def agent_note(key_names):
    parts = []
    if key_names:
        parts.append("Loft put these dev keys in your shell's environment: " + ", ".join(key_names) + ". Use them as "
                     "env vars ($NAME) in commands and dev servers; `loft vault run -- <cmd>` also passes them to a "
                     "command. Never print, echo, log or commit their values, and never write them into files "
                     "except a git-ignored .env.local when a tool needs one.")
    return " ".join(parts)


TICKETS = "vault-tickets"


def ticket_dir():
    return paths.data_dir() / TICKETS


def make_ticket(account, project):
    """A launch ticket: which account and project an agent's keys come from. Its id goes in the hook command."""
    tid = secrets.token_hex(8)
    paths.write_private(ticket_dir() / f"{tid}.json", json.dumps({"account": account, "project": project,
                                                                  "at": time.time()}))
    return tid


def ticket(tid):
    if not re.fullmatch(r"[0-9a-f]{16}", tid or ""):
        return None
    try:
        return json.loads((ticket_dir() / f"{tid}.json").read_text())
    except Exception:
        return None


def drop_ticket(tid):
    if re.fullmatch(r"[0-9a-f]{16}", tid or ""):
        try:
            (ticket_dir() / f"{tid}.json").unlink()
        except OSError:
            pass


def hook_settings(tid, loft_cmd):
    """--settings JSON for an agent: a SessionStart hook that exports its keys into its shell."""
    # The hook runs in the agent's worker, whose env comes from Claude's daemon, not from this Loft: carry
    # where this Loft keeps its config and secrets so the hook reads the same ones.
    carry = [f"{k}={os.environ[k]}" for k in ("LOFT_CONF_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "LOFT_VAULT")
             if os.environ.get(k)]
    cmd = " ".join(_sh(a) for a in [*(["env", *carry] if carry else []), *loft_cmd, "vault", "hook", tid])
    return json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": cmd, "timeout": 30}]}]}})


def _sh(a):
    return a if re.fullmatch(r"[A-Za-z0-9_./:=@+-]+", a) else "'" + a.replace("'", "'\\''") + "'"


def export_lines(env):
    return "".join(f"export {k}={_sh(v) if v else chr(39) * 2}\n" for k, v in env.items())


def run_hook(tid, env_file):
    """The SessionStart hook: append `export NAME=value` lines for the ticket's keys to $CLAUDE_ENV_FILE."""
    t = ticket(tid)
    if not t or not env_file:
        return 0
    env = keys_for(t.get("account"), t.get("project"))
    if env:
        fd = os.open(env_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(export_lines(env))
    return len(env)


# ------------------------------------------------------------------ for the app (no secret values)
def public(accounts=None, projects=None):
    d = load()
    return {"backend": backend().name, "where": backend().label,
            "keys": [{k: x.get(k) for k in ("id", "name", "scope", "note", "updated")} for x in d["keys"]]}
