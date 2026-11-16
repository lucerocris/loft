# Handoff: iOS Simulator in Loft › Devices (for the Mac team)

Status (2026-11-14): Loft › Devices works for **Android** on Linux and macOS: live screen, your
clicks and keys, and agents driving the phone with the `loft_phone_*` tools. **iOS still needs to be
built, and it needs a Mac with Xcode.** Apple's simulator only runs on macOS. The Linux projects that
try to run it (x86sim, macbox) are experimental and conflict with Apple's license, so we don't use them.

This doc is everything needed to add an iOS Simulator provider next to the Android one, without
changing the app page or the agent tools.

## What exists (read these first)

| File | What it does |
|---|---|
| `loft/mobile.py` | The Android provider: list and boot devices, leases (one agent per device), `Screen` (live JPEG frames + your input), `ui()` (element list), `tap/swipe/type_text/key`, `launch`, `install`, `logs` |
| `loft/agenttools.py` | What each `loft_phone_*` tool does, using `mobile.py`. Coordinates are in the agent's screenshot pixels and scaled to the device |
| `loft/agent_mcp.py` | The MCP tool list the agent sees (`PHONE_TOOLS`) |
| `server.py` | `/api/mobile` (list, boot, stop, free), `/ws/mobile/<serial>` (stream + input), `agent_route` (phone tools) |
| `index.html` | Loft › Devices pane (`paneDevices`, `mountDevice`, `devInput`) |
| `tests/test_mobile.py` | Unit tests; `LOFT_TEST_DEVICE=1` also drives a real device |

The page and the agent tools only use these `mobile.py` functions, so an iOS provider has to supply
the same ones:

```
devices() -> [{serial, state: "device"|..., model, emulator: bool, avd}]
avds() / boot(name) / wait_booted(serial) / booted(serial) / stop(serial)
size(serial) -> (w, h) in pixels
Screen(serial, on_frame(jpeg_bytes), on_event(dict))  with .pointer(msg, state), .key(msg), .close(), .w, .h
screenshot_png(serial) / ui(serial) -> [{n, cls, text, desc, id, x, y, w, h, click, edit, password, focused, ...}]
tap / swipe / type_text / key / clear_field / launch / install / logs
```

## Plan

1. **Split providers.** Move the Android code into an `Android` class (or a module) and make
   `mobile.py` route by serial. iOS simulators use their UDID as the serial (36 chars, already
   allowed by `SERIAL_RE`). `devices()` / `avds()` merge both providers. The `status()` shape stays
   the same; add `"platform": "android"|"ios"` per device so the page can show an Apple or Android icon.

2. **iOS provider (macOS only: `paths.is_mac()` and `xcrun` present).**
   - List: `xcrun simctl list devices available --json`. Booted ones are devices; the rest work
     like AVDs (bootable by name or UDID).
   - Boot / stop: `xcrun simctl boot <udid>` / `xcrun simctl shutdown <udid>`. No Simulator.app
     window is needed (headless boot works). `open -a Simulator` is optional, for "Open in a window".
   - Screen: pick one.
     - **idb** (`brew install idb-companion`, `pip install fb-idb`): `idb video-stream --udid U
       --format h264 --fps 30` outputs raw H.264, so it can go through the **same ffmpeg -> JPEG
       pipeline** as Android (`Screen._video`, just a different first command). This is the
       recommended option.
     - **serve-sim** (Evan Bacon, Apache-2.0, Apple Silicon only): it already serves MJPEG plus a
       WebSocket for input. Read its MJPEG and pass the frames on.
     - Fallback: `xcrun simctl io <udid> screenshot --type=jpeg -` in a loop (2-4 fps, no extras).
   - Input: `idb ui tap X Y`, `idb ui swipe X1 Y1 X2 Y2 --duration`, `idb ui text "..."`,
     `idb ui key <hid code>`, `idb ui button HOME|LOCK|SIRI`. iOS coordinates are **points**, not
     pixels: divide by the screen scale (`idb describe` gives it).
   - Elements: `idb ui describe-all --json` gives an accessibility tree. Map it to the same dicts
     `parse_ui()` returns: AXLabel becomes text/desc, AXUniqueId becomes id, frame becomes the centre
     x/y, and `type == "TextField"|"SecureTextField"` gives edit, with SecureTextField also setting
     password.
   - Apps: `xcrun simctl launch <udid> <bundle id>`, `xcrun simctl openurl <udid> <url>`,
     `xcrun simctl install <udid> <path/to/App.app>` (a simulator build, not an .ipa),
     logs from `xcrun simctl spawn <udid> log show --last 2m --predicate 'process == "<App>"'`.
   - Keys from the page: map `Enter`/`Backspace`/arrows to HID codes for `idb ui key`.
3. **Agent tools.** No new tools are needed. In `loft_phone_install`, accept `.app` folders for iOS.
   In the tool descriptions and `PHONE_NOTE` (`loft/board.py`), mention `xcrun simctl` /
   `npx expo run:ios --device <udid>` / `flutter run -d <udid>` next to the Android commands.
4. **Which agents get an iOS phone.** `wants_phone()` in `loft/board.py` checks folder markers. Add
   `ios/`, `*.xcodeproj`, `Podfile`. When the task says "iOS", or the project only has `ios/`,
   lease an iOS simulator first. A `--phone ios|android` option on `loft new` and in New task can
   come later.
5. **Remote devices (later, so Linux can use the Mac's simulator).** A Mac Loft already exposes
   `/api/mobile` and `/ws/mobile/<serial>` to paired devices over Tailscale. A Linux Loft could list
   a peer's devices and proxy the WebSocket.

## Android on the Mac (already works)

- The SDK is found at `~/Library/Android/sdk` (or `ANDROID_HOME`). Apple Silicon needs arm64
  system images, which Android Studio picks by default.
- Loft starts emulators with `-gpu host` and falls back to `swiftshader_indirect` if the emulator
  dies while starting. On the Linux dev box, SwiftShader crashed (emulator 36.6 and 37.2, kernel
  7.2, Core Ultra 9) and `-gpu host` worked. Override with `LOFT_EMULATOR_GPU`.
- `ffmpeg` (`brew install ffmpeg`) gives the smooth live screen. Without it, Loft sends stills at
  about 2 fps.

## How to check it works

```sh
python3 -m unittest tests.test_mobile -v                     # parsing, leases, sign-in (no device)
LOFT_TEST_DEVICE=1 python3 -m unittest tests.test_mobile -v  # with a booted simulator / emulator
loft phone list                                              # shows simulators and emulators
```

Then in the app: Settings › Devices, pick the simulator, click around, and type. Start a task with
"Android phone" on (or in an `ios/` project) that says "open Settings on the phone and tell me
what you see": the agent should use `loft_phone_screen` / `loft_phone_open` and you watch it live.

## Sources

- serve-sim: https://github.com/EvanBacon/serve-sim
- idb video streaming: https://fbidb.io/docs/video
- Callstack agent-device (iOS + Android tooling for agents, a good reference for idb usage):
  https://github.com/callstackincubator/agent-device
