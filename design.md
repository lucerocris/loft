# Design — Loft

## Reader and job
A developer who runs several Claude Code background agents at once, often across more
than one Claude account, on a desktop with mouse and keyboard. They open Loft to see which
agents need them, answer them, start queued tasks on the right account, and jump into one.

## Character
Quiet instrument. A dark ops board that stays out of the way until something needs you.

## Site map and flow
One page, two views: Inbox (list | selected agent) and Board (Todo, Working, Needs you,
Done). The right pane shows the selected agent, the New task form, or Accounts (gear in the
account filter row, or `,`), or Browser (globe in the header, Shift+W): the account's signed-in
sites as tabs over the live browser, with the agent browsers listed under it. With no `accounts.json` yet, the pane is the first-run setup.

## Rhythm
Dense. Sections divided by hairlines and labels, not surfaces. Cards and the accounts list
are the only boxed things. Loud only for "Needs you".

## Emphasis
First look: Needs you. Account color is carried by a small dot (list items, cards, filters,
usage footer), never a bar down an edge. Accent coverage < 5%.

## Color  (tokens: `index.html` :root)
Dark only. Canvas `--canvas #0F1012`, surfaces `--surface #15171A` `--raised #1C1F23`
`--selected #262A2F`, borders `--border #24272C` `--border-strong #30343A`,
text `--t1 #EDEEF0` `--t2 #A4A8AF` `--t3 #6E737B`.
Status: needs you `--needs #F2B544`, failed `--failed #F0685A`, live = the account's color.
Account palette (names match `claude agents` / `/color`, so agents show the same color there):
blue `#5B8DFF`, green `#46C07D`, purple `#A98BFF`, cyan `#3EC1D3`, pink `#F17DB8`,
orange `#F2924A`, yellow `#E3CF5B`, red `#F0685A`. New accounts take the first unused one.

## Type
Geist (UI) and Geist Mono (paths, meta, terminal), bundled in `static/fonts`. 13px UI,
11–12px meta, 18px pane title. Tabular nums.

## Shape and depth
Radius 6 / 10 / 14. Borders only; the one shadow is on menus and dialogs. Lucide icons, 16px.

## Motion
Live pulse dot and the recording mic only. Reduced motion: none.

## Voice
Plain verbs: "Start", "Save to Todo", "Move to <account>", "Move to <project>", "Archive", "Sign in", "Make default".
No emoji. Errors say what to do.

## Constraints
- An account keeps its color everywhere; never reuse one color for two accounts.
- No soft gradients.
- Archive never deletes; running agents are only touched through `claude stop/attach`.
- Every agent Loft starts or moves carries an explicit login (see `loft/accounts.py`).
