# Loft

One calm window for every Claude Code background agent on your machine, across as many
Claude accounts as you use.

Claude Code can run agents in the background (`claude --bg`, `claude agents`). Loft shows
all of them in one place: what needs you, what's working, your own todo list, and what
finished. Each agent runs on the Claude account you pick and keeps it when it restarts, so a
client's work bills the client's subscription and your side project bills yours.

- **Inbox**: agents that need you show their question in plain words with a reply box;
  working agents show their live terminal.
- **Board**: Todo, Working, Needs you, Done. Drag a todo to Working to start it.
- **Accounts**: add, rename, recolor, remove and sign in accounts; per-account 5-hour and
  7-day usage.
- **Voice** (optional): press V anywhere you type; transcribed locally, nothing leaves the
  machine.
- **Terminal board**: `loft` in a terminal is a keyboard-only version of the board.

Stdlib Python and one HTML page. No build step, no Node, no Electron.

## Requirements

- macOS or Linux
- Python 3.9+
- [Claude Code](https://docs.anthropic.com/claude-code) signed in at least once
- A Chromium browser (Chrome, Chromium, Edge, Brave) for the app window; any browser works
  as a tab
- Optional: `ffmpeg` and ~500 MB disk for voice input

## Install

```sh
git clone https://github.com/<you>/loft.git ~/loft
cd ~/loft
./install.sh            # add --voice for speech input
loft setup
```

`install.sh` links `loft` into `~/.local/bin` and adds a launcher (a `.desktop` entry on
Linux, `~/Applications/Loft.app` on macOS). It needs no root and starts nothing.

`loft setup` walks you through accounts: it finds the login this machine already has, offers
to import [claude-swap](https://github.com/realiti4/claude-swap) profiles, lets you add more
accounts, then opens the app. You can also do all of this in the app (gear icon).

macOS: if `~/.local/bin` isn't on your `PATH`, add `export PATH="$HOME/.local/bin:$PATH"` to
`~/.zshrc`.

## Accounts

Every account is a Claude login. Loft never copies tokens: it tells Claude Code which login
to use through `CLAUDE_SECURESTORAGE_CONFIG_DIR`, and every agent stores that in its saved
state, so the daemon restarts it on the same account.

```sh
loft account list                      # who's signed in, as which email
loft account add Work                  # opens the browser: sign in with that account
loft account add Client --email me@client.com
loft account import-cswap              # use your cswap profiles, in place
loft account rename work "Day job"
loft account color work purple         # blue green purple cyan pink orange yellow red
loft account default work              # for new tasks and agents with no saved login
loft account remove client             # forgets it; the login files stay put
loft organize --yes                    # give every agent an explicit login + its account's color
loft doctor                            # check everything
```

When you add an account, sign in with **that** account in the browser. If claude.ai picks
your other account automatically, sign out of claude.ai in that browser first.

Config lives in `~/.config/loft/accounts.json` (respects `XDG_CONFIG_HOME`):

```json
{
  "version": 1,
  "default": "personal",
  "accounts": [
    {"id": "personal", "label": "Personal", "color": "green", "creds_dir": "default"},
    {"id": "work", "label": "Work", "color": "blue", "creds_dir": "/Users/me/.local/share/loft/accounts/work"}
  ],
  "rules": [{"match": "acme|invoice", "account": "work"}]
}
```

`creds_dir: "default"` is the login plain `claude` uses. `rules` are optional: `loft organize`
uses them to pick an account for older agents that have no saved login (by name, prompt or
folder). Logins added with `loft account add` live in `~/.local/share/loft/accounts/<id>/`.

### From claude-swap (cswap)

`loft account import-cswap` (or the button in Accounts) adds each `cswap` profile that has a
session dir (`cswap run N` creates it) as an account pointing at that dir. Nothing is copied;
cswap keeps managing it. Agents you started under `cswap run N` live in that profile's own
`jobs/`; `loft migrate --yes` moves them into the shared view and keeps their account.

## Desktop app

Loft ships as a real desktop app (Tauri v2, `desktop/`). It opens its own window, keeps a tray
icon (needs-you count, New task, Quit), allows one window at a time, and starts the Loft server
for you, or attaches to one already running on 127.0.0.1:7717. Quitting stops only a server the
app started. The app bundles the server files (`server.py`, `loft/`, `static/`, `index.html`,
`voice_worker.py`) and runs them with your system `python3`; config and data stay in the usual
user folders. `LOFT_REPO`, `LOFT_PORT` and `LOFT_PYTHON` override the defaults; the log is
`~/.cache/loft/desktop.log`.

Install a build from the GitHub release (or build it yourself, below):

- Fedora / RHEL: `sudo dnf install ./Loft-<version>-1.x86_64.rpm`
- Debian / Ubuntu: `sudo apt install ./Loft_<version>_amd64.deb`
- Any Linux: `chmod +x Loft_<version>_amd64.AppImage` and run it (bigger: it bundles GStreamer
  so voice recording works).
- macOS: open the `.dmg` and drag Loft to Applications. Builds are unsigned for now, so the first
  time right-click Loft and choose Open, or run `xattr -dr com.apple.quarantine /Applications/Loft.app`.

### Start at login (Linux)

To have Loft (and phone access, if it's on) ready as soon as you log in, add
`~/.config/autostart/Loft.desktop` with `Exec=loft-desktop --hidden`. `--hidden` starts it in the
tray; phone access keeps retrying until Tailscale is up.

`loft app` opens the desktop app when it's installed; `loft app --browser` opens the older
Chrome/Edge `--app` window instead.

Build it yourself: install Rust (`rustup`) and the Tauri CLI (`cargo install tauri-cli --version "^2" --locked`),
plus on Fedora `webkit2gtk4.1-devel javascriptcoregtk4.1-devel libsoup3-devel
libappindicator-gtk3-devel librsvg2-devel libxdo-devel patchelf`. Then `cd desktop && cargo tauri build`.
For the AppImage on Fedora set `GSTREAMER_PLUGINS_DIR=/usr/lib64/gstreamer-1.0
GSTREAMER_HELPERS_DIR=/usr/libexec/gstreamer-1.0`. The `desktop` GitHub workflow builds the macOS
`.dmg` and Linux packages on tags `desktop-v*`.

Signing (TODO): signed and notarized macOS builds need an Apple Developer account and these repo
secrets: `APPLE_CERTIFICATE`, `APPLE_CERTIFICATE_PASSWORD`, `APPLE_SIGNING_IDENTITY`, `APPLE_ID`,
`APPLE_PASSWORD`, `APPLE_TEAM_ID`.

## Using it

```sh
loft app             # open the desktop app (or --browser for the Chrome window)
loft                 # terminal board
loft list            # text list; --all includes old finished agents
loft attach <name>   # open an agent in this terminal with its own account
loft switch <name> <account> [--now]   # bill an agent to another account
```

Switching accounts: the account chip under an agent's title (or the ⋯ menu's "Bills to")
lists every account with its 5h/7d usage. An idle, waiting or finished agent moves at once
and continues on the new account at your next reply. A working agent moves after its
turn (the queued switch shows as a badge you can cancel), or now if you choose to stop
the turn.

Keys in the app: `N` new task, `B` inbox/board, `J`/`K` move, `T` terminal, `V` speak,
`,` accounts, `P` projects, `G` cycle Group by, `Esc` back.

## Prompt queue

Queue prompts for an agent from the box under its terminal, or just reply while it works. A
queued prompt goes out once the agent leaves Working and its account is under its 5-hour and
7-day limits, one at a time, in order. If the account is at a limit, it waits for the reset
time shown on the card. Starting a todo on an account at its limit schedules it the same way.

While anything is queued, Loft keeps the machine from idle-sleeping (`caffeinate` on macOS,
`systemd-inhibit` on Linux), so prompts go out at the reset. A closed lid on battery still
sleeps; anything due then goes out on the next wake. Queues live in
`~/.claude/jobs/<id>/loft-queue.json`.

## Shared browser

Every agent card has a **Browser** tab (W): a headless Chromium that Loft streams into the card.
Click, scroll and type in it as in any browser.

- **Preview**: Loft finds the dev server running in the agent's folder (any port a process
  inside it listens on) and opens it. The address bar takes a URL or just a port. The phone
  icon switches to a 390×844 phone viewport. A red count shows console errors, uncaught
  exceptions and failed requests.
- **Watch the agent test**: agents started from Loft in a web folder (one with `package.json`
  or `index.html`) get the same browser through the Playwright MCP (`--cdp-endpoint`), so you
  watch the agent click through its work live. **Take over** tells the agent you're driving;
  **Hand back** tells it to carry on. Needs `npx`; `LOFT_BROWSER=0` turns it off.
- **Comment**: click an element and write a note. The agent gets the note, the element's
  selector and text, and a cropped screenshot.
- **Checks**: **Record** reloads the page, then saves your clicks and typing as steps. Use
  **Check text** and click some text to assert it's there. Checks belong to the project. Each
  time an agent on that project leaves Working, Loft replays them in a fresh tab against the
  agent's dev server. Failures go back to the agent with the step, the error and a screenshot,
  up to 3 runs, then Loft leaves it to you. **Run now** runs them on demand. The result shows
  on the card (✓ 2/2 checks).
- **Agent walkthroughs**: recording happens inside the page, so it captures whoever drives the
  browser: clicks, filled fields, Enter and page loads. Agents started from Loft also get
  Loft's own tools (`loft/agent_mcp.py`): `loft_record_start`, `loft_record_expect`,
  `loft_record_save`, `loft_record_discard`, `loft_run_checks` and `loft_list_checks`. Ask an
  agent to "walk through checkout and save it as a check" and you watch the steps appear in
  the card as it goes. Password fields are never recorded (replay relies on the saved login).
  Each agent's tools use their own token, so they only reach that agent's browser.
- **Saved login**: log in once in the browser, then **Login → Save login from this page**.
  Loft keeps that page's cookies and local/session storage for the project. It puts them into
  every browser of the project when you open it and before every check run. A login saved on
  `localhost` also works on `127.0.0.1`, on any port.

Files: browsers and screenshots in `~/.cache/loft/browsers/` (deleted when the agent is
archived), checks in `~/.config/loft/checks.json`, logins in `~/.config/loft/logins.json`
(mode 0600: they are session tokens). Uses Chrome, Chromium, Edge or Brave (`LOFT_CHROME`
overrides). The Browser tab is desktop-only: phones can't reach it.

## Projects and grouping

Group agents by status, account, project, folder or nothing. A project is a set of folders plus
the accounts it may run on; an agent belongs to the project whose folder holds its cwd (longest
match), unless you move it by hand. New tasks in a project use its default account, or `auto`
(the allowed account with the most 5-hour usage left).

```sh
loft project add "Client A" --folder ~/code/client-a --account work --account personal --default auto
loft project list
loft project edit client-a --color green
loft project assign <agent> client-a     # or "none" to clear
loft project rm client-a
loft view project                        # default Group by: status|account|project|folder|none
```

Projects live in `~/.config/loft/loft.json` next to `accounts.json`.

## Voice

`./install.sh --voice` creates `~/.local/share/loft/venv` with faster-whisper. Loft starts the
model on first use (`small.en` on CPU; first clip takes a few seconds, then about a second
each). End a sentence with "start it" or "send it" to submit hands-free.
`LOFT_WHISPER_MODEL` picks another model, `LOFT_WHISPER_HINT` adds words it should expect.
Needs `ffmpeg` (`brew install ffmpeg`, `sudo dnf install ffmpeg`, `sudo apt install ffmpeg`).

## Phone

A companion phone app (over Tailscale, paired per device) is in progress.

## macOS notes

- Loft has been developed on Linux; the macOS rules are implemented and unit-tested by
  simulation but not yet run on a Mac. Please open an issue if something is off.
- On macOS Claude Code keeps each login in the Keychain. For a login dir the item is
  `Claude Code-credentials-<first 8 hex of sha256(dir path)>`; the default login is the plain
  `Claude Code-credentials` item, which Claude selects when `CLAUDE_SECURESTORAGE_CONFIG_DIR`
  is set but empty. Loft writes it exactly that way. (On Linux the default login is the
  explicit `~/.claude` path.)
- Usage numbers read each token with `security find-generic-password`. macOS may ask once
  whether `security` may read the item; choose Always Allow.
- `claude auth status` and `/usage` read the shared `~/.claude.json` and always show the most
  recently signed-in email, whichever account a session actually uses. Loft identifies each
  account from its own token instead.

## Security

- The server binds **127.0.0.1 only**. Every API call needs a random per-run token that is
  baked into the page it serves, so other websites open in your browser can't drive it.
- Tokens are read only to fetch your usage and email from Anthropic's OAuth endpoints. They
  are never written, logged, copied or cached.
- `accounts.json` and new login dirs are created `0600` / `0700`.

## Layout

```
bin/loft          entry point (`cagents` is an alias)
loft/accounts.py  config, per-platform login env, keychain naming, cswap import, migration
loft/usage.py     per-account token, usage and identity
loft/core.py      agent state, organize, cswap-profile migration, text list
loft/board.py     cards and actions, terminal board
loft/cli.py       commands, setup wizard, doctor
loft/webapi.py    account routes for the app
server.py         local HTTP server (SSE, embedded terminal over WebSocket, voice)
index.html        the whole app UI; design rules in design.md
tests/            python3 -m unittest discover -s tests
```
