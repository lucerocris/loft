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

## Using it

```sh
loft app             # open the window (starts the local server if needed)
loft                 # terminal board
loft list            # text list; --all includes old finished agents
loft attach <name>   # open an agent in this terminal with its own account
```

Keys in the app: `N` new task, `B` inbox/board, `J`/`K` move, `T` terminal, `V` speak,
`,` accounts, `Esc` back.

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
