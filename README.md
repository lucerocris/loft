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
