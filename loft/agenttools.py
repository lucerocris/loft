"""What the loft_phone_* and loft_sign_in tools do (loft/agent_mcp.py -> POST /api/agent/<action> -> here).

An agent drives only the phone Loft leased to it (loft/mobile.py) and signs in only with the logins its
task was given (its record's "logins", loft/vault.py). Coordinates the agent sees and sends are in the
screenshot's pixels; Loft scales them to the device.
"""
import base64, time

from . import browser as BR, mobile as M, vault as V

SHOT_W = 540


def _scale(serial):
    w, h = M.size(serial)
    s = (w / SHOT_W) if M.shutil.which("ffmpeg") and w > SHOT_W else 1.0
    return w, h, s


def look(serial, elements=True, note=""):
    """A screenshot (small JPEG) and the element list, both in screenshot pixels."""
    w, h, s = _scale(serial)
    png = M.screenshot_png(serial)
    img, mime, _ = M.small_jpeg(png, SHOT_W) if s != 1.0 else (png, "image/png", None)
    lines = ""
    if elements:
        try:
            els = M.ui(serial)
            for e in els:
                e["x"], e["y"] = int(e["x"] / s), int(e["y"] / s)
                e["w"], e["h"] = int(e["w"] / s), int(e["h"] / s)
            lines = M.ui_lines(els) or "(no elements: the screen may be animating, or a game / web view)"
        except Exception as e:
            lines = f"(couldn't read the elements: {e})"
    app = M.focused_app(serial)
    head = (f"{note + ' ' if note else ''}Device {serial} · screen {int(w / s)}x{int(h / s)} (screenshot pixels"
            f"{'; the device is ' + str(w) + 'x' + str(h) if s != 1.0 else ''}) · in front: {app or '?'}")
    return {"text": head + ("\n" + lines if elements else ""), "image": base64.b64encode(img).decode(), "mime": mime}


def _element(serial, n, s):
    els = M.ui(serial)
    e = next((x for x in els if x["n"] == int(n)), None)
    if not e:
        raise ValueError(f"No element {n} on screen now. Call loft_phone_screen for a fresh list.")
    return e["x"], e["y"], e


def phone(jid, action, body, alive):
    """One phone tool call for agent `jid`. Returns (code, payload)."""
    if not M.available():
        return 400, {"message": "No Android SDK found (adb). Install Android Studio or the platform tools."}
    want = (body.get("device") or "").strip() if action == "use" else None
    if action == "use" and not want:
        st = M.status()
        mine = M.lease_of(jid)
        lines = [f"{d['serial']} · {d.get('avd') or d.get('model') or 'device'} · "
                 f"{'yours' if d['serial'] == mine else 'used by another agent' if d.get('agent') else 'free'}"
                 f"{'' if d['ready'] else ' · starting'}" for d in st["devices"]]
        lines += [f"emulator {a['name']} (not running: loft_phone_use with device={a['name']} starts it)"
                  for a in st["avds"] if not a["running"]]
        return 200, {"text": "\n".join(lines) or "No phones or emulators. Create an emulator in Android Studio "
                                                 "(Device Manager), or plug in a phone with USB debugging on."}
    serial, note = M.device_for(jid, want, alive=alive)
    if action in ("screen", "use"):
        return 200, look(serial, body.get("elements", True) is not False, note)
    w, h, s = _scale(serial)
    if action == "tap":
        if body.get("element") is not None:
            x, y, _ = _element(serial, body["element"], s)
        elif body.get("x") is not None and body.get("y") is not None:
            x, y = float(body["x"]) * s, float(body["y"]) * s
        else:
            raise ValueError("Give an element number, or x and y.")
        M.swipe(serial, x, y, x, y, 700, wait=True) if body.get("long") else M.tap(serial, x, y, wait=True)
        msg = f"Tapped ({int(x / s)}, {int(y / s)})"
    elif action == "swipe":
        d = body.get("direction")
        ms = int(body.get("ms") or 300)
        if d:
            cx, cy = w / 2, h / 2
            dx, dy = {"up": (0, -h * 0.3), "down": (0, h * 0.3), "left": (-w * 0.35, 0), "right": (w * 0.35, 0)}[d]
            M.swipe(serial, cx - dx / 2, cy - dy / 2, cx + dx / 2, cy + dy / 2, ms, wait=True)
        else:
            try:
                x1, y1, x2, y2 = (float(body[k]) * s for k in ("x1", "y1", "x2", "y2"))
            except (KeyError, TypeError, ValueError):
                raise ValueError("Give a direction, or x1, y1, x2, y2.")
            M.swipe(serial, x1, y1, x2, y2, ms, wait=True)
        msg = "Swiped"
    elif action == "type":
        text = body.get("text") or ""
        if not text.isascii():
            raise ValueError("Only ASCII text can be typed over adb. Use plain characters.")
        if body.get("clear"):
            M.clear_field(serial)
        M.type_text(serial, text, wait=True)
        if body.get("submit"):
            M.key(serial, "enter", wait=True)
        msg = f"Typed {len(text)} characters" + (" and pressed Enter" if body.get("submit") else "")
    elif action == "key":
        M.key(serial, body.get("key"), wait=True)
        msg = f"Pressed {body.get('key')}"
    elif action == "open":
        msg = M.launch(serial, body.get("target"))
    elif action == "install":
        return 200, {"message": M.install(serial, body.get("apk"))}
    elif action == "logs":
        return 200, {"text": M.logs(serial, body.get("package") or None, body.get("lines") or 150,
                                    (body.get("level") or "I").upper()[:1]) or "(no log lines)"}
    else:
        return 404, {"message": f"Unknown phone action {action}"}
    if body.get("look") is False:
        return 200, {"message": msg}
    time.sleep(0.9 if action in ("tap", "key", "type") else 1.4)
    return 200, look(serial, True, msg + ".")


def sign_in(jid, rec, body, alive):
    """loft_sign_in: fill one of the agent's logins into its browser or phone. Never returns a secret."""
    allowed = [x for x in (V.login(i) for i in (rec or {}).get("logins") or []) if x]
    lg = V.find_login(body.get("login"), allowed)
    if not lg:
        names = ", ".join(f"{x['label']} ({x['username']})" for x in allowed) or "none"
        return 400, {"message": f"No login named {body.get('login')!r} for this task. Its logins: {names}."}
    pw, _ = V.login_secret(lg["id"])
    if not pw:
        return 400, {"message": f"The password for {lg['label']} is missing from the keychain. Ask the user to "
                                f"save it again in Loft › Vault."}
    code_fn = (lambda: V.login_secret(lg["id"])[1]) if lg.get("totp") else None
    where = body.get("where") or ("browser" if (rec or {}).get("port") else "phone")
    if where == "phone":
        return 200, {"message": phone_sign_in(jid, lg, pw, code_fn, alive)}
    if not (rec or {}).get("port"):
        return 400, {"message": "You have no Loft browser. Use where: phone, or ask the user."}
    port = BR.ensure(jid)["port"]
    url = (body.get("url") or "").strip() or None
    if url and not BR.nav_ok(url):
        raise ValueError("url is a web address (http or https).")
    if not url and lg.get("url") and BR.page_url(port, ttl=0) in ("", "about:blank"):
        url = lg["url"]
    out = BR.sign_in(port, lg["username"], pw, code_fn, url)
    return 200 if out["ok"] else 409, {"message": f"{lg['label']}: {out['message']}"}


def phone_sign_in(jid, lg, pw, code_fn, alive):
    serial, _ = M.device_for(jid, None, alive=alive)
    els = M.ui(serial)
    edits = [e for e in els if e.get("edit")]
    pwf = next((e for e in edits if e.get("password")), None)
    did = []
    user = None
    if pwf:
        before = [e for e in edits if not e.get("password") and e["n"] < pwf["n"]]
        user = before[-1] if before else None
    elif edits:
        user = next((e for e in edits if e.get("focused")), edits[0])
    if not user and not pwf:
        return "No text fields on screen. Open the app's sign-in screen first."
    if user:
        M.tap(serial, user["x"], user["y"], wait=True); time.sleep(0.4)
        M.clear_field(serial); M.type_text(serial, lg["username"], wait=True); did.append("username")
        if not pwf:   # two-step: username, Next, then the password screen
            M.key(serial, "enter", wait=True); time.sleep(2.0)
            pwf = next((e for e in M.ui(serial) if e.get("password")), None)
    if pwf:
        M.tap(serial, pwf["x"], pwf["y"], wait=True); time.sleep(0.4)
        M.type_text(serial, pw, wait=True); did.append("password")
        M.key(serial, "enter", wait=True)
    if code_fn:
        time.sleep(2.5)
        fields = [e for e in M.ui(serial) if e.get("edit") and not e.get("password")]
        if fields and not any(e.get("password") for e in M.ui(serial)):
            f = next((e for e in fields if e.get("focused")), fields[0])
            M.tap(serial, f["x"], f["y"], wait=True); time.sleep(0.3)
            M.type_text(serial, code_fn(), wait=True); M.key(serial, "enter", wait=True); did.append("2FA code")
    time.sleep(1.5)
    still = any(e.get("password") for e in M.ui(serial))
    return (f"{lg['label']}: filled {', '.join(did) or 'nothing'} and submitted."
            + (" A password field is still showing, so it may have failed: look at the screen." if still else
               " Look at the screen to check you're in."))
