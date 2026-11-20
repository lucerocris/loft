"""Phones: Android emulators (and USB / Wi-Fi Android phones) in Loft, shared by you and your agents.

Loft › Devices streams a device's screen into the app and sends your clicks, drags and keys to it.
Agents drive the same device with the loft_phone_* tools (loft/agent_mcp.py -> /api/agent/phone-*),
so you watch them work, and can take over, like the shared browser.

  emulators   started headless: `emulator -avd <name> -port <even port> -no-window -no-audio`
  screen      `adb exec-out screenrecord --output-format=h264 -` piped through ffmpeg into JPEG frames
              (screenrecord stops every 3 minutes: Loft restarts it); without ffmpeg, PNG screenshots
  input       one long-lived `adb shell` per device: `input tap|swipe|text|keyevent`
  leases      <cache>/mobile/leases.json  {serial: {"agent": jid, "at": epoch}}: one agent per device

iOS: macOS only (Xcode's simulator); see docs/ios-handoff.md. The provider below is Android only.
"""
import base64, json, os, re, shlex, shutil, signal, socket, subprocess, threading, time, xml.etree.ElementTree as ET
from pathlib import Path

from . import paths
from . import scope as S

MAX_RUNNING = int(os.environ.get("LOFT_MAX_EMULATORS", "2"))
STREAM_WIDTH = 720
BOOT_TIMEOUT = 240
_LOCK = threading.RLock()
_BOOTING = {}     # avd -> {"serial", "proc", "at", "error"}
_SHELLS = {}      # serial -> Popen of `adb shell`
SERIAL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
AVD_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


# ------------------------------------------------------------------ the SDK
def sdk_root():
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        v = os.environ.get(var)
        if v and Path(v, "platform-tools").is_dir():
            return Path(v)
    adb = shutil.which("adb")
    if adb:
        p = Path(os.path.realpath(adb)).parent.parent
        if (p / "platform-tools").is_dir():
            return p
    home = Path.home()
    for p in (home / "Android/Sdk", home / "Library/Android/sdk", home / "develop/android-sdk", home / "android-sdk",
              Path("/opt/android-sdk")):
        if (p / "platform-tools").is_dir():
            return p
    return None


def adb_bin():
    r = sdk_root()
    p = r / "platform-tools" / "adb" if r else None
    return str(p) if p and p.exists() else shutil.which("adb")


def emulator_bin():
    r = sdk_root()
    p = r / "emulator" / "emulator" if r else None
    return str(p) if p and p.exists() else shutil.which("emulator")


def available():
    return bool(adb_bin())


def adb(serial, *args, timeout=20, text=True, input=None):
    cmd = [adb_bin()] + (["-s", serial] if serial else []) + list(args)
    return subprocess.run(cmd, capture_output=True, text=text, timeout=timeout, input=input)


def avds():
    emu = emulator_bin()
    if not emu:
        return []
    try:
        r = subprocess.run([emu, "-list-avds"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [l.strip() for l in r.stdout.splitlines() if AVD_RE.match(l.strip()) and not l.startswith("INFO")]


def parse_devices(text):
    """`adb devices -l` -> [{serial, state, model}]"""
    out = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or not SERIAL_RE.match(parts[0]) or not re.fullmatch(r"[a-z]+", parts[1]):
            continue
        info = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        out.append({"serial": parts[0], "state": parts[1], "model": info.get("model", "").replace("_", " "),
                    "emulator": parts[0].startswith("emulator-")})
    return out


_AVD_NAMES = {}


def avd_of(serial):
    if serial in _AVD_NAMES:
        return _AVD_NAMES[serial]
    try:
        r = adb(serial, "emu", "avd", "name", timeout=5)
        name = (r.stdout.splitlines() or [""])[0].strip()
    except Exception:
        name = ""
    if name and name != "OK":
        _AVD_NAMES[serial] = name
    return name if name != "OK" else ""


def devices():
    if not available():
        return []
    try:
        r = adb(None, "devices", "-l", timeout=10)
    except Exception:
        return []
    out = parse_devices(r.stdout)
    for d in out:
        if d["emulator"] and d["state"] == "device":
            d["avd"] = avd_of(d["serial"])
    return out


def online(serial):
    return any(d["serial"] == serial and d["state"] == "device" for d in devices())


def booted(serial):
    try:
        return adb(serial, "shell", "getprop", "sys.boot_completed", timeout=5).stdout.strip() == "1"
    except Exception:
        return False


# ------------------------------------------------------------------ emulators
def _free_port():
    used = {int(d["serial"].split("-")[1]) for d in devices() if d["emulator"] and d["serial"].split("-")[1].isdigit()}
    used |= {int(b["serial"].split("-")[1]) for b in _BOOTING.values()}
    for p in range(5600, 5680, 2):
        if p in used:
            continue
        try:
            socket.create_connection(("127.0.0.1", p), timeout=0.1).close()
            continue
        except OSError:
            return p
    raise RuntimeError("No free emulator port.")


def running_emulators():
    return [d for d in devices() if d["emulator"]]


def boot(avd, wipe=False):
    """Start an emulator headless. Returns its serial at once; it's ready when booted(serial) is true."""
    if not AVD_RE.match(avd or "") or avd not in avds():
        raise ValueError(f"No emulator named {avd!r}. Create one in Android Studio (Device Manager) or with avdmanager.")
    with _LOCK:
        for d in running_emulators():
            if d.get("avd") == avd:
                return d["serial"]
        b = _BOOTING.get(avd)
        if b and b["proc"].poll() is None:
            return b["serial"]
        if len(running_emulators()) + sum(1 for x in _BOOTING.values() if x["proc"].poll() is None) >= MAX_RUNNING:
            raise RuntimeError(f"{MAX_RUNNING} emulators are running already. Stop one first "
                               f"(or raise LOFT_MAX_EMULATORS).")
        return _spawn(avd, _free_port(), GPUS[0] if not os.environ.get("LOFT_EMULATOR_GPU") else
                      os.environ["LOFT_EMULATOR_GPU"], wipe)


# The host GPU first (fast; SwiftShader's software GL crashes on some new CPUs and kernels), then software.
GPUS = ["host", "swiftshader_indirect"]


def _spawn(avd, port, gpu, wipe=False):
    log = paths.cache_dir() / "mobile" / f"{avd}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    args = [emulator_bin(), "-avd", avd, "-port", str(port), "-no-window", "-no-audio", "-no-boot-anim", "-gpu", gpu]
    if wipe:
        args.append("-wipe-data")
    with open(log, "ab") as f:
        proc = subprocess.Popen(S.wrap(args), stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True)
    serial = f"emulator-{port}"
    _BOOTING[avd] = {"serial": serial, "proc": proc, "at": time.time(), "log": str(log), "gpu": gpu, "port": port}
    _AVD_NAMES[serial] = avd
    return serial


def _retry(avd):
    """The emulator died while starting: try it once more with the other GPU mode."""
    b = _BOOTING.get(avd)
    if not b or b.get("retried") or os.environ.get("LOFT_EMULATOR_GPU"):
        return False
    other = next((g for g in GPUS if g != b["gpu"]), None)
    if not other:
        return False
    _spawn(avd, b["port"], other)
    _BOOTING[avd]["retried"] = True
    return True


def wait_booted(serial, timeout=BOOT_TIMEOUT):
    end = time.time() + timeout
    while time.time() < end:
        if booted(serial):
            return True
        for avd, b in list(_BOOTING.items()):
            if b["serial"] == serial and b["proc"].poll() is not None:
                with _LOCK:
                    if _retry(avd):
                        break
                tail = ""
                try:
                    tail = Path(b["log"]).read_text(errors="replace").strip().splitlines()[-1]
                except Exception:
                    pass
                raise RuntimeError(f"The emulator quit while starting. {tail}".strip())
        time.sleep(1.5)
    raise RuntimeError("The emulator is still starting. Try again in a minute.")


def booting():
    """{avd: serial} still starting."""
    out = {}
    for avd, b in list(_BOOTING.items()):
        if b["proc"].poll() is not None or booted(b["serial"]):
            if b["proc"].poll() is not None or time.time() - b["at"] > 5:
                _BOOTING.pop(avd, None)
            if b["proc"].poll() is not None:
                continue
            continue
        out[avd] = b["serial"]
    return out


def stop(serial):
    check_serial(serial)
    close_shell(serial)
    release_serial(serial)
    if serial.startswith("emulator-"):
        try:
            adb(serial, "emu", "kill", timeout=10)
        except Exception:
            pass
        for avd, b in list(_BOOTING.items()):
            if b["serial"] == serial:
                try:
                    os.killpg(b["proc"].pid, signal.SIGTERM)
                except OSError:
                    pass
                _BOOTING.pop(avd, None)
        return "Stopping the emulator"
    return "That's a real phone: unplug it or turn off USB debugging to disconnect it."


def check_serial(serial):
    if not SERIAL_RE.match(serial or ""):
        raise ValueError("Bad device id.")
    return serial


# ------------------------------------------------------------------ leases (one agent per device)
def _lease_file():
    return paths.cache_dir() / "mobile" / "leases.json"


def leases():
    try:
        return json.loads(_lease_file().read_text())
    except Exception:
        return {}


def _save_leases(d):
    f = _lease_file(); f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(d))


def lease_of(jid):
    return next((s for s, l in leases().items() if l.get("agent") == jid), None)


def release(jid):
    with _LOCK:
        d = leases()
        n = {s: l for s, l in d.items() if l.get("agent") != jid}
        if n != d:
            _save_leases(n)


def release_serial(serial):
    with _LOCK:
        d = leases()
        if d.pop(serial, None) is not None:
            _save_leases(d)


def take(jid, serial):
    with _LOCK:
        d = {s: l for s, l in leases().items() if l.get("agent") != jid}
        d[serial] = {"agent": jid, "at": time.time()}
        _save_leases(d)


def device_for(jid, want=None, alive=lambda jid: True):
    """The device an agent drives: its lease if still online, else `want` (serial or emulator name), else a
    free online device, else a booted emulator. Returns (serial, note)."""
    devs = [d for d in devices() if d["state"] == "device"]
    held = leases()
    mine = lease_of(jid)
    free = [d for d in devs if d["serial"] not in held or held[d["serial"]]["agent"] == jid
            or not alive(held[d["serial"]]["agent"])]
    if want:
        hit = next((d for d in devs if want in (d["serial"], d.get("avd"))), None)
        if hit:
            if hit not in free:
                raise RuntimeError(f"{want} is in use by another agent.")
            take(jid, hit["serial"]); return hit["serial"], ""
        if want in avds():
            s = boot(want); take(jid, s); wait_booted(s); return s, f"Started the {want} emulator."
        raise ValueError(f"No device or emulator named {want!r}.")
    if mine and any(d["serial"] == mine for d in devs):
        return mine, ""
    if free:
        take(jid, free[0]["serial"]); return free[0]["serial"], ""
    names = avds()
    running = {d.get("avd") for d in running_emulators()}
    spare = [a for a in names if a not in running]
    if not spare:
        raise RuntimeError("Every device is in use by another agent." if devs else
                           "No Android emulator set up. Create one in Android Studio (Device Manager), then try again.")
    s = boot(spare[0]); take(jid, s); wait_booted(s)
    return s, f"Started the {spare[0]} emulator."


# ------------------------------------------------------------------ input
def size(serial):
    """Physical screen size (w, h) in pixels, as the device is held now."""
    r = adb(serial, "shell", "wm", "size", timeout=8).stdout
    m = re.findall(r"(\d+)x(\d+)", r)
    if not m:
        raise RuntimeError("Couldn't read the screen size.")
    w, h = map(int, m[-1])    # "Override size" comes last when set
    try:
        rot = adb(serial, "shell", "dumpsys", "input", timeout=8).stdout
        o = re.search(r"SurfaceOrientation:\s*(\d)", rot)
        if o and o.group(1) in ("1", "3"):
            w, h = h, w
    except Exception:
        pass
    return w, h


def _shell(serial):
    with _LOCK:
        p = _SHELLS.get(serial)
        if p and p.poll() is None:
            return p
        p = subprocess.Popen([adb_bin(), "-s", serial, "shell"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, text=True, bufsize=1)
        _SHELLS[serial] = p
        return p


def close_shell(serial):
    p = _SHELLS.pop(serial, None)
    if p:
        try:
            p.stdin.close(); p.terminate()
        except Exception:
            pass


def run_input(serial, line):
    """One `input ...` command on the device's long-lived shell (fast: no adb start-up per tap)."""
    p = _shell(serial)
    try:
        p.stdin.write(line + "\n"); p.stdin.flush()
    except (BrokenPipeError, OSError):
        close_shell(serial)
        p = _shell(serial); p.stdin.write(line + "\n"); p.stdin.flush()


def tap(serial, x, y, wait=False):
    cmd = f"input tap {int(x)} {int(y)}"
    adb(serial, "shell", cmd, timeout=10) if wait else run_input(serial, cmd)


def swipe(serial, x1, y1, x2, y2, ms=300, wait=False):
    cmd = f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {max(1, int(ms))}"
    adb(serial, "shell", cmd, timeout=15) if wait else run_input(serial, cmd)


def long_press(serial, x, y, ms=700):
    swipe(serial, x, y, x, y, ms)


def text_arg(text):
    """`input text` wants spaces as %s; everything goes through the device shell, so quote it."""
    return shlex.quote(text.replace("%", "\\%").replace(" ", "%s"))


def type_text(serial, text, wait=False):
    if not text:
        return
    for chunk in re.findall(r"[\s\S]{1,200}", text):
        lines = chunk.split("\n")
        for i, part in enumerate(lines):
            if part:
                if wait:
                    adb(serial, "shell", "input", "text", text_arg(part), timeout=20)
                else:
                    run_input(serial, f"input text {text_arg(part)}")
            if i < len(lines) - 1:
                key(serial, "enter", wait)


KEYCODES = {"back": 4, "home": 3, "enter": 66, "tab": 61, "delete": 67, "backspace": 67, "del": 112,
            "forward_delete": 112, "recents": 187, "app_switch": 187, "menu": 82, "escape": 111, "esc": 111,
            "up": 19, "down": 20, "left": 21, "right": 22, "power": 26, "volume_up": 24, "volume_down": 25,
            "space": 62, "search": 84, "move_home": 122, "move_end": 123, "page_up": 92, "page_down": 93,
            "rotate": None}
BROWSER_KEYS = {"Enter": "enter", "Backspace": "delete", "Delete": "forward_delete", "Tab": "tab", "Escape": "back",
                "ArrowUp": "up", "ArrowDown": "down", "ArrowLeft": "left", "ArrowRight": "right", "Home": "move_home",
                "End": "move_end", "PageUp": "page_up", "PageDown": "page_down"}


def key(serial, name, wait=False):
    name = str(name or "").lower().replace(" ", "_")
    if name == "rotate":
        return rotate(serial)
    code = KEYCODES.get(name)
    if code is None and not re.fullmatch(r"(KEYCODE_)?[A-Z0-9_]{1,40}", str(name).upper()):
        raise ValueError(f"Unknown key {name!r}. Try back, home, enter, tab, delete, recents, up/down/left/right.")
    arg = str(code) if code is not None else str(name).upper()
    if wait:
        adb(serial, "shell", "input", "keyevent", arg, timeout=10)
    else:
        run_input(serial, f"input keyevent {arg}")


def rotate(serial):
    cur = adb(serial, "shell", "settings", "get", "system", "user_rotation", timeout=8).stdout.strip()
    nxt = "0" if cur in ("1", "3") else "1"
    adb(serial, "shell", "settings", "put", "system", "accelerometer_rotation", "0", timeout=8)
    adb(serial, "shell", "settings", "put", "system", "user_rotation", nxt, timeout=8)


def clear_field(serial):
    """Select all in the focused field and delete it."""
    adb(serial, "shell", "input keycombination 113 29 2>/dev/null; input keyevent 67", timeout=10)


# ------------------------------------------------------------------ what's on screen
def screenshot_png(serial):
    r = adb(serial, "exec-out", "screencap", "-p", timeout=20, text=False)
    if r.returncode or not r.stdout.startswith(b"\x89PNG"):
        raise RuntimeError("Couldn't take a screenshot.")
    return r.stdout


def small_jpeg(png, width=540):
    """A smaller JPEG for the agent (fewer tokens). Needs ffmpeg; else the PNG as is."""
    ff = shutil.which("ffmpeg")
    if not ff:
        return png, "image/png", None
    try:
        r = subprocess.run([ff, "-loglevel", "error", "-f", "png_pipe", "-i", "-", "-vf", f"scale={width}:-2",
                            "-q:v", "5", "-f", "mjpeg", "-"], input=png, capture_output=True, timeout=20)
        if r.returncode == 0 and r.stdout[:2] == b"\xff\xd8":
            return r.stdout, "image/jpeg", width
    except Exception:
        pass
    return png, "image/png", None


def png_size(png):
    import struct
    return struct.unpack(">II", png[16:24])


BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def parse_ui(xml_text, limit=150):
    """uiautomator XML -> visible elements worth naming: [{n, text, desc, id, cls, click, edit, x, y, w, h}]"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for node in root.iter("node"):
        a = node.attrib
        m = BOUNDS.match(a.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        text, desc = a.get("text", "").strip(), a.get("content-desc", "").strip()
        rid = a.get("resource-id", "").split(":id/")[-1]
        click = a.get("clickable") == "true" or a.get("long-clickable") == "true"
        cls = a.get("class", "").rsplit(".", 1)[-1]
        edit = cls in ("EditText", "AutoCompleteTextView") or a.get("password") == "true"
        scroll = a.get("scrollable") == "true"
        if not (text or desc or click or edit or scroll):
            continue
        e = {"n": len(out) + 1, "cls": cls, "x": (x1 + x2) // 2, "y": (y1 + y2) // 2, "w": x2 - x1, "h": y2 - y1}
        if text and a.get("password") != "true":
            e["text"] = text[:120]
        if desc:
            e["desc"] = desc[:120]
        if rid:
            e["id"] = rid[:80]
        for flag, on in (("click", click), ("edit", edit), ("scroll", scroll), ("focused", a.get("focused") == "true"),
                         ("checked", a.get("checked") == "true"), ("password", a.get("password") == "true"),
                         ("off", a.get("enabled") == "false")):
            if on:
                e[flag] = True
        out.append(e)
        if len(out) >= limit:
            break
    return out


def ui(serial):
    r = adb(serial, "exec-out", "uiautomator", "dump", "/dev/tty", timeout=30)
    text = r.stdout
    i = text.find("<?xml")
    j = text.rfind("</hierarchy>")
    if i < 0 or j < 0:
        r = adb(serial, "shell", "uiautomator dump /sdcard/loft-ui.xml >/dev/null && cat /sdcard/loft-ui.xml", timeout=30)
        text = r.stdout; i = text.find("<?xml"); j = text.rfind("</hierarchy>")
    if i < 0 or j < 0:
        raise RuntimeError("Couldn't read the screen's elements (an animation or a secure screen may block it).")
    return parse_ui(text[i:j + len("</hierarchy>")])


def ui_lines(elems):
    out = []
    for e in elems:
        label = e.get("text") or e.get("desc") or e.get("id") or ""
        flags = ",".join(k for k in ("click", "edit", "scroll", "focused", "checked", "password", "off") if e.get(k))
        out.append(f"[{e['n']}] {e['cls']} {json.dumps(label, ensure_ascii=False) if label else ''}"
                   f"{' id=' + e['id'] if e.get('id') and e.get('id') != label else ''} @({e['x']},{e['y']})"
                   f"{' ' + flags if flags else ''}")
    return "\n".join(out)


def focused_app(serial):
    try:
        r = adb(serial, "shell", "dumpsys", "window", timeout=10).stdout
        m = re.search(r"mCurrentFocus=Window\{[^ ]+ [^ ]+ ([^}\s]+)\}", r) or re.search(r"mFocusedApp=.*? ([\w.]+/[\w.$]+)", r)
        return m.group(1) if m else ""
    except Exception:
        return ""


def launch(serial, target):
    """An app by package (com.example.app), a component (pkg/.Activity), or a URL / deep link."""
    target = str(target or "").strip()
    if re.match(r"^[a-z][a-z0-9+.-]*://", target, re.I):
        r = adb(serial, "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", shlex.quote(target), timeout=20)
    elif re.fullmatch(r"[A-Za-z0-9_.]+/[A-Za-z0-9_.$]+", target):
        r = adb(serial, "shell", "am", "start", "-n", target, timeout=20)
    elif re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+", target):
        r = adb(serial, "shell", "monkey", "-p", target, "-c", "android.intent.category.LAUNCHER", "1", timeout=20)
        if "No activities found" in r.stdout + r.stderr:
            raise ValueError(f"{target} isn't installed (or has no launcher screen).")
    else:
        raise ValueError("Give a package name (com.example.app), pkg/.Activity, or a URL.")
    out = (r.stdout + r.stderr).strip()
    if r.returncode or "Error" in out:
        raise RuntimeError(out.splitlines()[-1] if out else "Couldn't start it.")
    return f"Opened {target}"


def install(serial, apk):
    apk = os.path.expanduser(str(apk or ""))
    if not apk.endswith(".apk") or not os.path.isfile(apk):
        raise ValueError("Give the path of a built .apk file.")
    r = adb(serial, "install", "-r", "-t", apk, timeout=300)
    out = (r.stdout + r.stderr).strip()
    if "Success" not in out:
        raise RuntimeError(out.splitlines()[-1] if out else "Install failed.")
    return f"Installed {os.path.basename(apk)}"


def logs(serial, package=None, lines=150, level="I"):
    lines = max(10, min(int(lines or 150), 1000))
    args = ["logcat", "-d", "-t", str(lines), f"*:{level if level in 'VDIWEF' else 'I'}"]
    if package:
        if not re.fullmatch(r"[A-Za-z0-9_.]+", package):
            raise ValueError("Bad package name.")
        pid = adb(serial, "shell", "pidof", package, timeout=8).stdout.strip().split()
        if pid:
            args += [f"--pid={pid[0]}"]
    return adb(serial, *args, timeout=20).stdout[-20000:]


# ------------------------------------------------------------------ the live screen
class Screen:
    """Streams a device's screen as JPEG frames: on_frame(bytes). on_event(dict) gets {"type": "size", w, h}."""

    def __init__(self, serial, on_frame, on_event):
        self.serial, self.on_frame, self.on_event = serial, on_frame, on_event
        self.closed = False
        self.procs = []
        self.w, self.h = size(serial)
        on_event({"type": "size", "w": self.w, "h": self.h})
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()
        threading.Thread(target=self._watch_rotation, daemon=True).start()

    def _run(self):
        ff = shutil.which("ffmpeg")
        try:   # a still at once: the video only sends frames when something on screen changes
            self.on_frame(small_jpeg(screenshot_png(self.serial), min(STREAM_WIDTH, self.w))[0])
        except Exception:
            pass
        while not self.closed:
            try:
                if ff:
                    self._video(ff)
                else:
                    self._stills()
            except Exception:
                time.sleep(1)
            if not self.closed and not online(self.serial):
                self.on_event({"type": "gone"}); return

    def _video(self, ff):
        w = min(STREAM_WIDTH, self.w)
        h = int(self.h * w / self.w) // 2 * 2
        rec = subprocess.Popen([adb_bin(), "-s", self.serial, "exec-out", "screenrecord", "--output-format=h264",
                                "--size", f"{w}x{h}", "--bit-rate", "6000000", "-"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        dec = subprocess.Popen([ff, "-loglevel", "quiet", "-threads", "1", "-flags", "low_delay",
                                "-probesize", "32", "-analyzeduration", "0",
                                "-f", "h264", "-i", "-", "-pix_fmt", "yuvj420p", "-strict", "unofficial",
                                "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "6", "-"],
                               stdin=rec.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        rec.stdout.close()
        self.procs = [rec, dec]
        buf = b""
        try:
            while not self.closed:
                chunk = dec.stdout.read1(1 << 16) if hasattr(dec.stdout, "read1") else os.read(dec.stdout.fileno(), 1 << 16)
                if not chunk:
                    break
                buf += chunk
                while True:
                    s = buf.find(b"\xff\xd8")
                    e = buf.find(b"\xff\xd9", s + 2) if s >= 0 else -1
                    if s < 0 or e < 0:
                        if s > 0:
                            buf = buf[s:]
                        break
                    self.on_frame(buf[s:e + 2])
                    buf = buf[e + 2:]
        finally:
            self._kill()

    def _stills(self):
        while not self.closed:
            png = screenshot_png(self.serial)
            jpg, _, _ = small_jpeg(png, min(STREAM_WIDTH, self.w))
            self.on_frame(jpg)
            time.sleep(0.4)

    def _watch_rotation(self):
        while not self.closed:
            time.sleep(2)
            try:
                w, h = size(self.serial)
            except Exception:
                continue
            if (w, h) != (self.w, self.h):
                self.w, self.h = w, h
                self.on_event({"type": "size", "w": w, "h": h})
                self._kill()     # screenrecord restarts at the new size

    def _kill(self):
        for p in self.procs:
            try:
                p.kill()
            except Exception:
                pass
        self.procs = []

    def poke(self):
        """screenrecord only sends frames when something changes: a no-op key wakes it after a reconnect."""
        try:
            run_input(self.serial, "input keyevent 0")
        except Exception:
            pass

    def close(self):
        self.closed = True
        self._kill()

    # your input, from the app: x/y are 0..1 of the picture
    def pointer(self, msg, state):
        x, y = float(msg.get("x", 0)) * self.w, float(msg.get("y", 0)) * self.h
        kind = msg.get("kind")
        if kind == "down":
            state["down"] = (x, y, time.time())
        elif kind == "up" and state.get("down"):
            x0, y0, t0 = state.pop("down")
            ms = int((time.time() - t0) * 1000)
            if abs(x - x0) < 12 and abs(y - y0) < 12:
                long_press(self.serial, x, y, ms) if ms > 500 else tap(self.serial, x, y)
            else:
                swipe(self.serial, x0, y0, x, y, max(80, min(ms, 2000)))
        elif kind == "wheel":
            dy = float(msg.get("dy", 0))
            dist = max(-0.4, min(0.4, -dy / 1000.0)) * self.h
            swipe(self.serial, x, y, x, max(1, min(self.h - 1, y + dist)), 150)

    def key(self, msg):
        if msg.get("text"):
            return type_text(self.serial, msg["text"])
        k = BROWSER_KEYS.get(msg.get("key", ""))
        if k:
            key(self.serial, k)


def status():
    devs = devices()
    held = leases()
    starting = booting()
    names = avds()
    return {"adb": available(), "emulator": bool(emulator_bin()), "ffmpeg": bool(shutil.which("ffmpeg")),
            "sdk": str(sdk_root() or ""), "max": MAX_RUNNING,
            "devices": [{**d, "agent": (held.get(d["serial"]) or {}).get("agent"),
                         "ready": d["state"] == "device" and (not d["emulator"] or booted(d["serial"]))} for d in devs],
            "avds": [{"name": a, "running": any(d.get("avd") == a for d in devs) or a in starting,
                      "starting": a in starting, "serial": starting.get(a) or next((d["serial"] for d in devs
                                                                                  if d.get("avd") == a), None)}
                     for a in names]}
