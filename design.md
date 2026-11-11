# Design — cagents

## Reader and job
Cris, all day, on a KDE desktop (monochrome rice), mouse + keyboard. Opens it to see
which Claude agents need him, start queued tasks on the right account, and jump into one.

## Character
Quiet instrument. A dark ops board that stays out of the way until something needs you.

## Site map and flow
One page: the board. Card click opens the detail panel on the right; "Open in terminal"
hands off to Konsole (`claude attach`). New task opens a small dialog (N).

## Page skeletons
Board: top bar (name, view switch, filter, New task) → sticky column header row →
Work lane → Personal lane. Detail panel slides over the right edge; the board keeps scrolling.

## Rhythm
Dense. Lanes divided by a hairline and their header row, not by surfaces. Cards are the
only boxed things. Loud only for "Needs you" cards.

## Emphasis
First look: the Needs you column. Account colour is carried by the lane head and a small
account dot on each card, never a bar down the card's edge. Accent coverage < 5%.

## Color  (tokens: `index.html` :root)
Dark only (matches the rice). Canvas `--bg #0c0d0f`, surfaces `--s1 #131518` `--s2 #1a1d21`,
border `--line #262a30`, text `--t1 #e6e8eb` `--t2 #a3a9b1` `--t3 #6f757d`.
Accounts: work `--work #5b8dff` (Ritchie), personal `--pers #46c07d` (lucerocris).
Status: needs you `--amber #e3b04b`, failed `--red #f0685a`, live `--live` = account color.

## Type
JetBrains Mono everywhere (system font). 13px UI, 12px meta, 15px panel title. Tabular nums.

## Shape and depth
Radius 6 (cards, inputs), 8 (panel, dialog). Borders only, no shadows except the panel's
one edge shadow. Lucide icons, 15px.

## Motion
Panel slides in 180ms ease-out; card hover border change only. Nothing else moves except
the live pulse dot. Reduced motion: no pulse, no slide.

## Voice
Plain verbs: "Open in terminal", "Start", "Add to Todo", "Move to Work", "Archive".
No emoji. Errors say what to do.

## Constraints
- Blue = work (Ritchie), green = personal (lucerocris), everywhere. Never swap.
- No soft gradients (user preference).
- Archive never deletes; running agents are only touched through `claude stop/attach`.
