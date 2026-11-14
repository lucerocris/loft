# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
A developer who runs several Claude Code background agents at once, often across more than one
Claude account (work, clients, side projects), on a desktop with mouse and keyboard. They open
Loft to see which agents need them, answer them, start queued tasks on the right account, and
jump into one. A paired phone can also reach it.

## Product Purpose
One calm window for every Claude Code background agent on the machine, across as many Claude
accounts as the user has. Success: the user never misses an agent waiting on them, and every
agent bills the account it belongs to.

## Positioning
Multi-account by design: each agent runs on an explicitly chosen Claude login and keeps it across
restarts, so a client's work bills the client's subscription. Local, stdlib Python and one HTML
page; no build step, no Electron.

## Operating Context
- Views: Inbox (agent list + selected agent with live terminal or reply box) and Board
  (Todo, Working, Needs you, Done; drag to start). Right pane also hosts New task and Accounts.
- First run with no `accounts.json` shows setup in the right pane.
- Keyboard-first; voice input (V) transcribed locally; browser preview and recording for agents.
- Shipped as a browser tab, a Chromium app window, a Tauri desktop app (`desktop/`), and a macOS
  launcher (`macos/`). A keyboard-only terminal board exists (`loft` CLI).

## Capabilities and Constraints
- Account colors use the names Claude Code knows (`claude agents`, `/color`): blue, green,
  purple, cyan, pink, orange, yellow, red. Names are binding; hex values may be retuned.
- An account keeps its color everywhere; never two accounts on one color.
- Archive never deletes; running agents are only touched through `claude stop/attach`.
- Every agent Loft starts or moves carries an explicit login.
- No build step: UI stays a single `index.html` with assets in `static/`.

## Brand Commitments
- Name "Loft"; house-shaped mark with two account dots (blue, green).
- Voice: plain verbs ("Start", "Save to Todo", "Move to <account>", "Archive", "Sign in"). No
  emoji. Errors say what to do.
- Quality bar named by the user (2026-11-14): Apple / iOS. Both dark and light themes required.

## Product Principles
1. What needs you comes first; everything else stays quiet.
2. The account is always visible: color identifies who pays.
3. Never destroy work: archive, stop, and attach only.
4. Keyboard and voice reach everything.
