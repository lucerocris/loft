# Loft browser protocol (desktop page + paired phone)

Two kinds of live browser, one wire format:

- **Agent browsers**: one headless Chromium per agent (`loft/browser.py`). The agent drives it
  through the `loft-browser` MCP tools; you watch and drive it over `/ws/browser/<agent id>`.
- **Sites browser** (Loft › Browser): one signed-in Chrome profile per Loft account
  (`loft/sites.py`). You sign in to the account's sites there once; every agent started on that
  account gets a copy of the profile, so it opens those sites already signed in. Streamed over
  `/ws/sites/<account id>`.

## Auth

Same as every other route.

| Client | How |
|---|---|
| Desktop page | `X-Cagents-Token: <token>` header; WebSockets pass `?t=<token>` |
| Paired phone (tailnet listener) | `Authorization: Bearer <device token>` on the HTTP request and on the WebSocket upgrade |

Phone sessions can do everything below except: `POST /api/sites/<account>/window` (it opens a
window on the desktop: 404 for phones), the `/api/browser/<agent id>` routes (desktop only:
local ports, check results), and navigating anywhere but web pages. For phone sessions `nav` and
`site` only go to `http://`, `https://` or `about:blank`; `file://`, `chrome://`, `about:*`,
`view-source:` and the like are refused with a `toast` (`err: true`).

## JSON routes

All bodies and replies are JSON. Errors are `{"message": "..."}` with 400 (bad input) or 404.

### `GET /api/sites`

```json
{
  "chrome": true,
  "accounts": [
    {
      "account": "work", "label": "Work", "hex": "#5B8DFF",
      "sites": [
        {"id": "3f9c01ab", "name": "Helpdesk", "url": "https://help.example.com",
         "cookies": 7, "signedIn": true}
      ],
      "browser": {"running": true, "window": false},
      "profile": true
    }
  ],
  "agents": [
    {"id": "1a2b3c4d", "name": "Fix the import", "account": "work", "col": "running",
     "url": "https://help.example.com/tickets", "live": true, "sitesAccount": "work"}
  ]
}
```

- `signedIn` is a hint: the profile holds unexpired cookies for the site's host (or a parent
  domain). `false` means surely signed out; `true` means probably signed in.
- `browser.window`: the profile is open in a normal Chrome window on the desktop; the stream
  is unavailable until it is closed.
- `agents`: agents that have a Loft browser. `live` = it is on a page (not a blank tab).
  `sitesAccount` = whose sites it started signed in to (null for a plain web-project browser).

### `GET /api/sites/<account>`

One entry of `accounts` above.

### `POST /api/sites/<account>`  `{"name": "Helpdesk", "url": "help.example.com"}`

Adds a site. `https://` is added to a bare host; only http/https; no `user@` in the URL. The
name defaults to the host. Reply: `{"message", "site": {id, name, url}, ...the account entry}`.

### `PATCH /api/sites/<account>/<site id>`  `{"name"?: "...", "url"?: "..."}`

Reply `{"message": "Saved", "site": {...}}`.

### `DELETE /api/sites/<account>/<site id>`

Reply `{"message": "Removed"}`.

### `POST /api/sites/<account>/window`  `{"open": true, "url"?: "https://..."}` (desktop only)

Closes the headless browser and opens the profile in a normal Chrome window (for sign-ins that
refuse headless Chrome: captcha, 2FA prompts, "this browser may not be secure"). `{"open": false}`
closes the window cleanly (cookies saved); the next `/ws/sites` connection starts headless again.

### Snapshot additions (`GET /api/state`, SSE `/api/events`)

- `snapshot.sites`: `{account id: number of sites}` (accounts without sites are left out).
- `card.browser` (agents only, `null` when the agent has no Loft browser):

```json
{"mcp": true, "running": false, "url": "https://help.example.com/tickets", "live": true,
 "account": "work", "sites": 2, "checks": {"ok": true, "passed": 2, "total": 2, "attempts": 0, "at": 1731500000}}
```

`live`, `url` come from the browser's newest tab (refreshed every ~3 s). `account`/`sites` only
appear when the agent's profile was seeded from that account's signed-in sites. Show a
"Browser" chip on the card when `live` is true; one tap opens `/ws/browser/<id>`.

## WebSockets

`GET /ws/browser/<agent id>` and `GET /ws/sites/<account id>`, standard RFC 6455 upgrade.

### Server → client

- **Binary frames**: one JPEG per frame (Chrome screencast, quality 70, at most 1600 px on the
  long side). Show the newest; drop older ones if you fall behind. Frames only come when the page
  changes, so a still page sends nothing.
- **Text frames**: JSON objects with a `type`:

| type | fields | when |
|---|---|---|
| `url` | `url` | the tab navigated, or the stream switched to another tab |
| `hello` (agent) | `mcp`, `ports` (dev-server ports in its folder), `login` (bool), `recording` (steps or null) | after connecting |
| `hello` / `sites` (sites) | the account entry from `GET /api/sites/<account>` | after connecting / on `{"type":"sites"}` |
| `window` (sites) | — | the profile is open in a window on the desktop; the socket then closes |
| `err` | `text` | page error, console error, failed request (agent browsers mostly) |
| `toast` | `text`, `err?` | a message to show; `err: true` = something failed |
| `steps` | `steps`, `on` | (agent) a recording changed |
| `saved`, `login`, `recoff` | | (agent) desktop-only check/login UI updates; phones can ignore |

A `url` event usually arrives before `hello`. When the browser goes away (closed for a window,
crashed) the sites socket closes; reconnect to start it again.

### Client → server (text frames, JSON)

Coordinates are **0..1 of the picture** (x from the left edge, y from the top), so they don't
depend on how big you draw it.

| type | fields | notes |
|---|---|---|
| `mouse` | `kind`: `mousePressed` \| `mouseReleased` \| `mouseMoved` \| `mouseWheel`; `x`, `y`; `button`: `left`\|`middle`\|`right` (press/release); `clicks` (1, 2 for double); `mods`; `dx`, `dy` (wheel, CSS px) | a tap = `mousePressed` then `mouseReleased` at the same point |
| `key` | either `text` (characters to type, any length) or `key` (`Enter`, `Backspace`, `Tab`, `Escape`, `Delete`, `ArrowLeft`/`Up`/`Right`/`Down`, `Home`, `End`, `PageUp`, `PageDown`) with `mods` | send typed text as `text`; use `key` for editing keys |
| `nav` | `url` | a URL, a bare host (`example.com` becomes `http://example.com`) |
| `site` | `id` | (sites) open one of the account's sites |
| `back`, `reload` | | |
| `viewport` | `mobile`: bool | 390×844 phone viewport with touch emulation, or back to 1280×800 |
| `sites` | | (sites) ask for a fresh `sites` event (signed-in hints) |
| `comment` | `x`, `y`, `note` | (agent) send the agent a note about the element at x,y, with a screenshot |
| `rec` | `on`, `name?` | (agent) start / stop-and-save a recorded check |

`mods` is a bit mask: Alt 1, Ctrl 2, Meta/Cmd 4, Shift 8. Scrolling on a phone: send
`mouseWheel` with `dy` = the finger's movement in picture pixels × (page width / picture width),
negated (finger up = positive `dy` = scroll down).

Ping frames are answered with pong. Close the socket to stop watching; the browser keeps running.

## Agents

When a task starts (`board.launch_todo`) the agent gets a browser if its folder looks like a web
project, or if its Loft account has sites. With sites, its profile is a copy of the account's
signed-in profile (caches and lock files left out) plus, if that browser is running, its current
cookies. The agent's system prompt lists the sites and their URLs, tells it to use the
`loft-browser` tools for them, and to ask you to sign in again in Loft › Browser rather than
type credentials. `--disallowedTools mcp__plugin_playwright_playwright` keeps it from opening
the Playwright plugin's separate, signed-out window.

## Files

| What | Where |
|---|---|
| sites list | `<config>/sites.json` (0600): `{"version": 1, "accounts": {"<account id>": [{id, name, url}]}}` |
| signed-in profile | `<data>/browser/<account id>/profile/` (0700) |
| its browser record | `<data>/browser/<account id>/master.json` (`{mode: "headless"\|"window", pid, port, profile}`) |
| agent browsers | `<cache>/browsers/<agent id>.json`, `profile-*` (deleted when the agent is archived) |

`<config>` = `$LOFT_CONF_DIR` or `~/.config/loft`, `<data>` = `$XDG_DATA_HOME/loft` or
`~/.local/share/loft`, `<cache>` = `$XDG_CACHE_HOME/loft` or `~/.cache/loft`. Every Loft Chrome
runs with `--password-store=basic` on Linux so cookies stay readable in a copied profile, and
headless ones send a normal Chrome user agent.
