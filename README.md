# Loft (formerly cagents)

One calm window for every Claude Code background agent across both accounts
(Work = Ritchie, Personal = lucerocris). Two panes: the inbox (Needs you,
Working, Todo, Done) and the selected agent. Agents that need you show their
question in plain words with a reply box; working agents show their live
terminal. Design: Figma file 4JZHgYBCfSwFEwFIynSTdv (Loft v2 screens).

## Voice
Press **V** (or the mic) anywhere you type: new task, reply, todo prompt, or the
terminal. Speech is transcribed locally by faster-whisper (small.en, CPU) in
`~/.local/share/loft/venv`; nothing leaves the machine. End a sentence with
"start it" / "send it" to submit hands-free. Set it up once with:

    python3 -m venv ~/.local/share/loft/venv
    ~/.local/share/loft/venv/bin/pip install faster-whisper

## Replying without the terminal
`POST /api/cards/<id>/reply` attaches to the agent in a hidden pty, pastes the
text, presses Enter and detaches (there is no `claude send`).

---

# cagents

Lightweight board for Claude Code background agents across two accounts
(work = Ritchie, blue; personal = lucerocris, green).

- `cagents-app` (or the "cagents" launcher entry) opens it as its own Chrome app window.
  It starts `server.py` on 127.0.0.1:7717 if it isn't running.
- `server.py`: stdlib-only API + live updates (server-sent events). Reuses
  `~/.local/bin/cagents` and `~/.local/share/cagents/board.py`.
- `index.html`: the whole UI. Design rules in `design.md`.

Columns: Todo, Working, Needs you, Done. Lanes: Work, Personal.
Keys: N new task, / filter, 1/2/3 view, O open selected in terminal, Esc close.
