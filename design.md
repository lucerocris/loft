# Design — Loft

## Reader and job
A developer who runs several Claude Code background agents at once, often across more
than one Claude account, on a desktop with mouse and keyboard. They open Loft to see which
agents need them, answer them, start queued tasks on the right account, and jump into one.

## Character
Liquid Glass, after macOS 26. A calm window whose controls float on glass over a quiet
backdrop; it stays out of the way until something needs you. Figma: "Loft — Redesign", option B.

## Site map and flow
One page, two views: Inbox (list | selected agent) and Board (Todo, Working, Needs you,
Done). The right pane shows the selected agent, the New task form, or Accounts (gear in the
account filter row, or `,`). With no `accounts.json` yet, the pane is the first-run setup.

## Rhythm
Dense lists, generous pane. Glass only on the floating layer (sidebar, board toolbar, pane
controls, question bubble, composer, menus, toasts); content stays solid. Loud only for "Needs you".

## Emphasis
First look: Needs you. Account color is carried by a small dot (list items, cards, filters,
usage footer), never a bar down an edge. The accent is graphite (black/white), so color always
means an account or a status.

## Color  (tokens: `index.html` :root, light under `prefers-color-scheme: light`)
Dark and light follow the OS; there is no in-app switch.
- Backdrop `--canvas` #171C26 / #E1E8F2. Glass `--glass` rgba(40,42,48,.62) / rgba(255,255,255,.62)
  with `--blur` and `--glass-border`. Solid `--raised` #2C2E34 / #FFFFFF.
- Text `--t1` #F2F2F7 / #1D1D1F, `--t2` #A1A1A8 / #5F5F66, `--t3` #75757C / #86868D.
- Primary (graphite) `--primary` #F2F2F7 / #1D1D1F.
- Status: needs you `--needs` (text) #FFB340 / #A65A00 and `--needs-dot` #FFB340 / #E89A00;
  failed #FF453A / #D70015; ok #30D158 / #248A3D; live = the account's color with a soft halo.
- Account palette = Apple system colors, names match `claude agents` / `/color`
  (`loft/accounts.py`): blue #0A84FF, green #30D158, purple #BF5AF2, cyan #64D2FF, pink #FF375F,
  orange #FF9F0A, yellow #FFD60A, red #FF453A. New accounts take the first unused one.

## Type
System stack: SF Pro on macOS, then Geist (bundled in `static/fonts`). Mono: SF Mono, then
Geist Mono; the terminal always uses Geist Mono. 13px UI, 11–12px meta, 24px bold pane title,
16px question. Tabular nums.

## Shape and depth
Radius 7 / 10 / 16; pills for chips, toolbar buttons and the composer. Shadows
`--shadow-sm/md/lg`, lighter in light mode. Lucide icons, 16px.

## Motion
Recording mic pulse and short popover scale-in only. Reduced motion: none. Reduced
transparency: glass falls back to solid `--raised`.

## Voice
Plain verbs: "Start", "Save to Todo", "Move to <account>", "Move to <project>", "Archive", "Sign in", "Make default".
No emoji. Errors say what to do.

## Constraints
- An account keeps its color everywhere; never reuse one color for two accounts.
- No soft gradients. Blur only on the floating layer, never on content.
- Archive never deletes; running agents are only touched through `claude stop/attach`.
- Every agent Loft starts or moves carries an explicit login (see `loft/accounts.py`).
