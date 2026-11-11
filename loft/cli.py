"""loft - one window for Claude Code background agents across any number of accounts.

  loft                      terminal board (in a terminal); `loft app` opens the window
  loft app                  open the Loft window (starts the local server if needed)
  loft serve [--port N]     run the server in the foreground (127.0.0.1 only)
  loft setup                first-run wizard: accounts, cswap import, open the app
  loft doctor               check claude, every account's login, the daemon, voice

  loft list [--all]         grouped text list of agents (--json for machines)
  loft watch [secs]         live text list
  loft attach <id|name>     open an agent with the account that owns it
  loft show <id|name>       full detail + result
  loft organize [--yes]     color by account, pin what needs you, give agents with
                            no saved login an explicit one
  loft migrate [--yes]      move agents started under `cswap run N` into ~/.claude/jobs

  loft account list
  loft account add [name] [--email E] [--color C] [--dry-run]
                            sign a new account in (opens the browser via `claude auth login`)
  loft account login <id>   sign an existing account in again
  loft account import-cswap [--dry-run] [num ...]
                            use claude-swap (cswap) profiles as accounts, in place
  loft account rename <id> <label>
  loft account color <id> <color>     (blue green purple cyan pink orange yellow red)
  loft account default <id>           account for new tasks and agents with no saved login
  loft account identify [id]          look up each login's email again
  loft account remove <id>
  loft migrate-config [--yes] [--force]
                            write accounts.json from the older two-account (Work/Personal) setup
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from . import __version__
from . import accounts as A
from . import core as C
from . import paths
from . import usage as U

DEFAULT_PORT = int(os.environ.get("LOFT_PORT") or os.environ.get("CAGENTS_PORT") or 7717)


def say(*a):
    print(*a, flush=True)


def ask(q, default=""):
    try:
        a = input(f"{q}{f' [{default}]' if default else ''}: ").strip()
    except EOFError:
        a = ""
    return a or default


def yes(q, default=False):
    a = ask(q + (" [Y/n]" if default else " [y/N]")).lower()
    return default if not a else a.startswith("y")


def flag(argv, name, default=None):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            v = argv[i + 1]
            del argv[i:i + 2]
            return v
        del argv[i]
    return default


# ------------------------------------------------------------------ accounts

def describe(acc, cfg, check=False):
    tag = " (default)" if acc["id"] == cfg.get("default") else ""
    where = "default login" if acc["creds_dir"] == A.DEFAULT_CREDS else acc["creds_dir"].replace(str(paths.HOME), "~")
    line = f"  {C.c(C.ANSI.get(acc['color'], '0'), '●')} {acc['id']:<12} {acc['label']:<14} {acc.get('email') or '-':<34} {where}{tag}"
    if check:
        try:
            t = U.token_info(acc)
            line += C.c(C.YEL, "  token expired (refreshes when claude next runs as it)") if t["expired"] else C.c(C.GRN, "  signed in")
        except U.TokenError as e:
            line += C.c(C.RED, f"  {e}")
    return line


def identify(acc):
    """Email for an account's login, or (None, reason)."""
    try:
        return U.profile(acc)["email"], ""
    except U.TokenError as e:
        return None, str(e)


def login_argv(acc, email=None):
    return [paths.claude_bin(), "auth", "login"] + (["--email", email] if email else [])


def login_env(acc):
    return A.apply_env(dict(os.environ), acc)


def new_account(cfg, name, color=None):
    """An account record with a fresh login dir (not saved yet)."""
    acc_id = A.unique_id(name, cfg)
    if color and color not in A.COLORS:
        raise SystemExit(f"unknown color {color}; pick one of {' '.join(A.PALETTE)}")
    return {"id": acc_id, "label": name, "color": color or A.next_color(cfg), "creds_dir": str(A.account_dir(acc_id))}


def run_login(acc, email=None):
    d = Path(A.env_value(acc)) if A.env_value(acc) else None
    if d:
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    say(f"\nSigning in {acc['label']}. A browser window opens: sign in with the account you want here.")
    say("(If claude.ai picks the wrong account automatically, sign out of claude.ai in that browser first.)\n")
    r = subprocess.call(login_argv(acc, email), env=login_env(acc), cwd=str(paths.HOME))
    return r == 0


def cmd_account(argv):
    sub = argv[0] if argv else "list"
    rest = argv[1:]
    cfg = A.load()
    real = A.configured()
    if sub in ("list", "ls"):
        if not real:
            say(C.c(C.DIM, f"no {A.config_path()} yet; this is the implicit single account. Run `loft setup`."))
        for a in cfg["accounts"]:
            say(describe(a, cfg, check=True))
        return
    if sub == "add":
        dry = "--dry-run" in rest
        rest = [x for x in rest if x != "--dry-run"]
        email = flag(rest, "--email")
        color = flag(rest, "--color")
        name = rest[0] if rest else ask("Name for this account (e.g. Work, Client, Side project)")
        if not name:
            raise SystemExit("an account needs a name")
        acc = new_account(cfg, name, color)
        say(f"account   {acc['id']} ({acc['label']}, {acc['color']})")
        say(f"login dir {acc['creds_dir']}")
        say(f"command   {A.VAR}={A.env_value(acc)!r} {' '.join(login_argv(acc, email))}")
        if paths.is_mac():
            say(f"keychain  {A.keychain_service(acc)}")
        if dry:
            say("(dry run: nothing created, no login started)")
            return acc
        if not run_login(acc, email):
            raise SystemExit("claude auth login didn't finish; nothing was saved. Try again with `loft account add`.")
        found, why = identify(acc)
        if not found:
            raise SystemExit(f"no login landed in {acc['creds_dir']} ({why}); nothing was saved.")
        dupe = next((a for a in cfg["accounts"] if (a.get("email") or "").lower() == found.lower()), None)
        if dupe and not yes(f"{found} is already the account '{dupe['label']}'. Add it again anyway?"):
            raise SystemExit("not added")
        acc["email"] = found
        cfg["accounts"].append(acc)
        A.save(cfg)
        say(f"added {acc['label']} ({found})")
        return acc
    if sub == "login":
        acc = A.get(rest[0], cfg) if rest else None
        if not acc:
            raise SystemExit("usage: loft account login <id>")
        if "--dry-run" in rest:
            say(f"{A.VAR}={A.env_value(acc)!r} {' '.join(login_argv(acc))}"); return
        run_login(acc, acc.get("email"))
        found, why = identify(acc)
        say(f"{acc['label']}: {found or why}")
        if found and found != acc.get("email"):
            acc["email"] = found; A.save(cfg)
        return
    if sub in ("import-cswap", "import"):
        dry = "--dry-run" in rest
        only = [x for x in rest if not x.startswith("--")]
        profs = A.cswap_profiles()
        if not profs:
            raise SystemExit(f"no cswap accounts found under {A.cswap_root()}")
        dflt = next((a for a in cfg["accounts"] if a["creds_dir"] == A.DEFAULT_CREDS), None)
        dmail = (dflt or {}).get("email") or (identify(dflt)[0] if dflt else None)
        if dflt and dmail and not dflt.get("email"):
            dflt["email"] = dmail
        cfg, log = A.import_cswap(cfg, profs, default_email=dmail, only=only or None)
        for l in log:
            say("  " + l)
        if dry:
            say("(dry run: nothing saved)"); return
        A.save(cfg)
        say(f"saved {A.config_path()}")
        return
    if sub in ("rename", "color", "default", "remove", "rm"):
        acc = A.get(rest[0], cfg) if rest else None
        if not acc:
            raise SystemExit(f"usage: loft account {sub} <id> ...   (ids: {', '.join(a['id'] for a in cfg['accounts'])})")
        if sub == "rename":
            if len(rest) < 2: raise SystemExit("usage: loft account rename <id> <label>")
            acc["label"] = " ".join(rest[1:])
        elif sub == "color":
            if len(rest) < 2 or rest[1] not in A.COLORS: raise SystemExit(f"colors: {' '.join(A.PALETTE)}")
            acc["color"] = rest[1]
        elif sub == "default":
            cfg["default"] = acc["id"]
        else:
            msg = remove_account(cfg, acc["id"])
            say(msg); return
        A.save(cfg)
        say(describe(acc, cfg))
        return
    if sub == "identify":
        for a in cfg["accounts"]:
            if rest and a["id"] not in rest:
                continue
            found, why = identify(a)
            say(f"  {a['label']:<14} {found or why}")
            if found:
                a["email"] = found
        if real:
            A.save(cfg)
        return
    raise SystemExit(__doc__)


def remove_account(cfg, acc_id):
    """Forget an account. Its login dir is left alone (and agents keep their saved env)."""
    if len(cfg["accounts"]) < 2:
        return "can't remove the only account"
    users = []
    for sf in C.jobs_dir().glob("*/state.json"):
        try:
            st = json.loads(sf.read_text())
        except Exception:
            continue
        acc, saved = C.job_account(st, cfg)
        if acc and acc["id"] == acc_id and saved:
            users.append(sf.parent.name)
    cfg["accounts"] = [a for a in cfg["accounts"] if a["id"] != acc_id]
    if cfg.get("default") == acc_id:
        cfg["default"] = None
    A.save(cfg)
    note = f" {len(users)} agent(s) still bill it until you move them." if users else ""
    return f"removed {acc_id} from Loft (its login files were left in place).{note}"


# ------------------------------------------------------------------ doctor

def cmd_doctor(argv):
    ok = True
    def line(good, text, hint=""):
        nonlocal ok
        ok &= bool(good) or good is None
        mark = C.c(C.GRN, "ok  ") if good else (C.c(C.YEL, "note") if good is None else C.c(C.RED, "FAIL"))
        say(f"  {mark} {text}" + (C.c(C.DIM, f"\n         {hint}") if hint and good is not True else ""))
    say(C.c(C.BOLD, f"loft {__version__} on {sys.platform}"))
    line(sys.version_info >= (3, 9), f"python {sys.version.split()[0]}", "Loft needs Python 3.9+")
    cl = paths.claude_bin()
    found = shutil.which(cl) or os.path.exists(cl)
    ver = ""
    if found:
        try:
            ver = subprocess.run([cl, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception:
            pass
    line(found, f"claude CLI {ver or cl}", "" if found else "install Claude Code: https://docs.anthropic.com/claude-code")
    line(True if A.configured() else None, f"config {A.config_path()}", "" if A.configured() else "run `loft setup`")
    cfg = A.load()
    for acc in cfg["accounts"]:
        try:
            t = U.token_info(acc)
            em, why = identify(acc)
            if em:
                same = not acc.get("email") or acc["email"].lower() == em.lower()
                line(same, f"{acc['label']}: signed in as {em}",
                     "" if same else f"config says {acc['email']}; run `loft account identify` if this is right")
            else:
                line(None if t["expired"] else False, f"{acc['label']}: token found, {why}")
        except U.TokenError as e:
            line(False, f"{acc['label']}: {e}", f"loft account login {acc['id']}")
    pid, env = C.daemon_env()
    if pid is None:
        line(None, "claude daemon not running (it starts with the first background agent)")
    elif env is None:
        line(None, f"claude daemon pid {pid}: its env can't be read on this OS",
             "agents launched by Loft always carry an explicit login, so this is fine")
    else:
        v = env.get(A.VAR, env.get("CLAUDE_CONFIG_DIR"))
        acc = A.account_for_value(v, cfg) if v is not None else None
        if v is None:
            line(True, f"claude daemon pid {pid} has no login var set (agents without one use the default login)")
        else:
            line(None, f"claude daemon pid {pid} was started with {A.VAR}={v!r} ({acc['label'] if acc else 'unknown login'})",
                 "agents with no saved login inherit this; `loft organize --yes` gives each one an explicit login")
    missing = []
    for sf in C.jobs_dir().glob("*/state.json"):
        try:
            if not A.has_saved_login(json.loads(sf.read_text()).get("providerEnv")):
                missing.append(sf.parent.name)
        except Exception:
            pass
    line(not missing, f"{len(missing)} agent(s) without a saved login" if missing else "every agent has a saved login",
         "run `loft organize --yes`" if missing else "")
    unknown = []
    for sf in C.jobs_dir().glob("*/state.json"):
        try:
            st = json.loads(sf.read_text())
        except Exception:
            continue
        acc, saved = C.job_account(st, cfg)
        if saved and not acc:
            unknown.append((st.get("providerEnv") or {}).get(A.VAR) or (st.get("providerEnv") or {}).get("CLAUDE_CONFIG_DIR"))
    if unknown:
        line(None, f"{len(unknown)} agent(s) use a login Loft doesn't know: {sorted(set(unknown))[:3]}",
             "add it: `loft account import-cswap`, or edit accounts.json")
    if shutil.which("cswap"):
        line(True, f"cswap found ({len(A.cswap_profiles())} profile(s) under {A.cswap_root()})")
    vp = paths.venv_python()
    line(True if vp.exists() else None, "voice venv " + (str(vp) if vp.exists() else "not installed"),
         "" if vp.exists() else "optional: ./install.sh --voice")
    line(True if shutil.which("ffmpeg") else None, "ffmpeg " + ("found" if shutil.which("ffmpeg") else "missing (voice needs it)"),
         "" if shutil.which("ffmpeg") else ("brew install ffmpeg" if paths.is_mac() else "sudo dnf/apt install ffmpeg"))
    port_up = server_up(DEFAULT_PORT)
    line(True if port_up else None, f"server on 127.0.0.1:{DEFAULT_PORT} " + ("running" if port_up else "not running (`loft app` starts it)"))
    return 0 if ok else 1


# ------------------------------------------------------------------ server / window

def server_up(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def start_server(port):
    if server_up(port):
        return True
    log = Path(os.environ.get("XDG_RUNTIME_DIR") or paths.cache_dir()) / "loft.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    # launched from a .desktop entry or Loft.app the PATH can be minimal: make claude/ffmpeg findable
    extra = [str(paths.HOME / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    env["PATH"] = os.pathsep.join([p for p in extra if p not in env.get("PATH", "").split(os.pathsep)] + [env.get("PATH", "/usr/bin:/bin")])
    with open(log, "ab") as fh:
        subprocess.Popen([sys.executable, str(paths.REPO / "server.py"), "--port", str(port)], stdout=fh, stderr=fh, env=env,
                         stdin=subprocess.DEVNULL, start_new_session=True, cwd=str(paths.REPO))
    for _ in range(50):
        if server_up(port):
            return True
        time.sleep(0.1)
    say(f"server didn't start; see {log}")
    return False


MAC_BROWSERS = ["Google Chrome", "Chromium", "Microsoft Edge", "Brave Browser", "Arc"]
LINUX_BROWSERS = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "brave-browser"]


def open_window(url):
    profile = os.environ.get("LOFT_CHROME_PROFILE") or str(paths.data_dir() / "chrome")
    if paths.is_mac():
        for app in MAC_BROWSERS:
            if any(Path(base, f"{app}.app").exists() for base in ("/Applications", str(paths.HOME / "Applications"))):
                subprocess.Popen(["open", "-na", app, "--args", f"--app={url}", f"--user-data-dir={profile}"])
                return app
        subprocess.Popen(["open", url])
        return "default browser"
    for b in LINUX_BROWSERS:
        if shutil.which(b):
            subprocess.Popen([b, f"--app={url}", "--class=loft", f"--user-data-dir={profile}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return b
    webbrowser.open(url)
    return "default browser"


def cmd_app(argv):
    port = int(flag(argv, "--port", DEFAULT_PORT))
    if not start_server(port):
        return 1
    url = f"http://127.0.0.1:{port}/"
    if "--no-open" in argv:
        say(url); return 0
    how = open_window(url)
    say(f"Loft at {url} ({how})")
    return 0


def cmd_serve(argv):
    port = flag(list(argv), "--port", str(DEFAULT_PORT))
    os.execv(sys.executable, [sys.executable, str(paths.REPO / "server.py"), "--port", str(port), *[a for a in argv if a not in ("--port", port)]])


# ------------------------------------------------------------------ setup wizard

def cmd_setup(argv):
    say(C.c(C.BOLD, "Loft setup") + C.c(C.DIM, f"  (config: {A.config_path()})"))
    if not (shutil.which(paths.claude_bin()) or os.path.exists(paths.claude_bin())):
        raise SystemExit("Claude Code isn't installed (no `claude` on PATH). Install it first, then run `loft setup` again.")
    if A.configured():
        cfg = A.load()
        say("\nYou already have accounts:")
        for a in cfg["accounts"]:
            say(describe(a, cfg))
    else:
        legacy = A.legacy_setup()
        if legacy["work_dir"] and yes("\nFound the older two-account setup (Work/Personal). Bring it over as is?", True):
            cfg = A.save(A.migrate_config())
            say(f"wrote {A.config_path()}")
            for a in cfg["accounts"]:
                say(describe(a, cfg))
        else:
            cfg = {"version": 1, "default": None, "accounts": [], "rules": []}
            base = {"id": "x", "creds_dir": A.DEFAULT_CREDS, "label": "", "color": "blue"}
            email, why = identify(base)
            if email:
                say(f"\nYour current Claude login is {email}.")
            else:
                say(f"\nNo default Claude login found ({why}).")
                if yes("Sign in now with `claude auth login`?", True):
                    run_login(base)
                    email, why = identify(base)
            label = ask("Name for this account", "Personal")
            cfg["accounts"].append({"id": A.unique_id(label, cfg), "label": label, "color": "green" if label.lower() == "personal" else "blue",
                                    "creds_dir": A.DEFAULT_CREDS, "email": email or ""})
            cfg = A.save(cfg)
            say(f"saved {A.config_path()}")
    profs = [p for p in A.cswap_profiles() if p["dir"]] if A.cswap_root().exists() else []
    if profs:
        dflt = next((a for a in cfg["accounts"] if a["creds_dir"] == A.DEFAULT_CREDS), None)
        preview, log = A.import_cswap(json.loads(json.dumps(cfg)), profs, default_email=(dflt or {}).get("email"))
        new = [l for l in log if l.startswith("added")]
        if new:
            say(f"\nclaude-swap (cswap) profiles you can use as accounts:")
            for l in new:
                say("  " + l.replace("added ", ""))
            if yes("Import them? (their logins stay where they are; nothing is copied)", True):
                cfg = A.save(preview)
    while yes("\nAdd another Claude account (opens the browser to sign in)?", False):
        try:
            cmd_account(["add"])
        except SystemExit as e:
            say(str(e))
        cfg = A.load()
    cfg = A.load()
    if len(cfg["accounts"]) > 1:
        say("\nDefault account for new tasks and for agents with no saved login:")
        for i, a in enumerate(cfg["accounts"], 1):
            say(f"  {i}. {a['label']} {a.get('email') or ''}")
        pick = ask("Number", str(next((i for i, a in enumerate(cfg["accounts"], 1) if a["id"] == cfg["default"]), 1)))
        if pick.isdigit() and 1 <= int(pick) <= len(cfg["accounts"]):
            cfg["default"] = cfg["accounts"][int(pick) - 1]["id"]; A.save(cfg)
    if any(True for _ in C.jobs_dir().glob("*/state.json")):
        say("")
        changes = C.organize(dry=True, out=lambda *_: None)
        if changes and yes(f"Give your {len(changes)} existing agent change(s) explicit logins and account colors now?", True):
            C.organize(dry=False)
    if "--no-open" not in argv and yes("\nOpen Loft now?", True):
        return cmd_app([])
    say("\nDone. `loft app` opens the window, `loft doctor` checks everything.")
    return 0


def cmd_migrate_config(argv):
    if A.configured() and "--force" not in argv:
        say(f"{A.config_path()} already exists; nothing to do (--force to overwrite).")
        return 0
    cfg = A.migrate_config()
    for a in cfg["accounts"]:
        found, _ = identify(a)
        if found:
            a["email"] = found
    say(json.dumps(cfg, indent=2))
    if "--yes" not in argv:
        say(f"(dry run: `loft migrate-config --yes` writes {A.config_path()})")
        return 0
    A.save(cfg)
    say(f"wrote {A.config_path()}")
    return 0


# ------------------------------------------------------------------ main

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    show_all = "--all" in argv
    argv = [a for a in argv if a != "--all"]
    if argv and argv[0] in ("-h", "--help", "help"):
        say(__doc__.strip()); return 0
    if argv and argv[0] in ("--version", "version"):
        say(f"loft {__version__}"); return 0
    if "--json" in argv:
        say(json.dumps(C.load(), indent=2)); return 0
    if not argv and sys.stdout.isatty():
        argv = ["board"]
    cmd = argv[0] if argv else "list"
    rest = argv[1:]
    if cmd == "board":
        from . import board
        board.run(); return 0
    if cmd in ("app", "open"):
        return cmd_app(rest)
    if cmd == "serve":
        return cmd_serve(rest)
    if cmd == "setup":
        return cmd_setup(rest)
    if cmd == "doctor":
        return cmd_doctor(rest)
    if cmd in ("account", "accounts"):
        cmd_account(rest); return 0
    if cmd == "migrate-config":
        return cmd_migrate_config(rest)
    if cmd in ("attach", "a"):
        if not rest: raise SystemExit("usage: loft attach <id|name>")
        C.attach(C.find(C.load(), rest[0]), rest[1:])
    elif cmd == "show":
        if not rest: raise SystemExit("usage: loft show <id|name>")
        j = C.find(C.load(), rest[0])
        for k in ("id", "name", "account", "state", "project", "created", "updated"):
            say(f"{C.c(C.DIM, k + ':'):<20} {j[k]}")
        say(f"\n{C.c(C.BOLD, 'detail')}\n{j['detail']}")
        if j["result"]: say(f"\n{C.c(C.BOLD, 'result')}\n{j['result']}")
        for l in j["links"]: say(C.c(C.CYN, l))
    elif cmd == "watch":
        secs = float(rest[0]) if rest else 3
        try:
            while True:
                out = C.render(C.load(), show_all)
                sys.stdout.write("\033[H\033[2J" + out + "\n" + C.c(C.DIM, time.strftime("updated %H:%M:%S  (ctrl-c to quit)")) + "\n")
                sys.stdout.flush()
                time.sleep(secs)
        except KeyboardInterrupt:
            pass
    elif cmd == "migrate":
        C.migrate(dry="--yes" not in rest)
        if "--yes" in rest:
            C.organize(dry=False)
    elif cmd == "organize":
        C.organize(dry="--yes" not in rest, transcript="--transcript" in rest)
    elif cmd == "list":
        say(C.render(C.load(), show_all))
    else:
        C.attach(C.find(C.load(), cmd), rest)  # `loft <id>` shorthand
    return 0


if __name__ == "__main__":
    sys.exit(main())
