# Loft "flexible" organization contract (shared by desktop + mobile agents)

Goal: Loft is not tied to one person's Work/Personal pair. Users organize agents however they think:
by account, by project/group, by folder/repo, by status, or flat. A project can use one OR several accounts.

## Use cases to design for
1. Solo: one account, many repos. Grouping by folder or project; account UI mostly hidden.
2. Two accounts, personal vs work (Cris): group by account (today's behaviour).
3. Freelancer: a project per client, each client project pinned to one account.
4. Heavy user: one project spread over 2-3 accounts to share load; New task picks the project's default account, or the one with most usage left ("auto").
5. Team member / agency: many projects, few accounts; mostly group by project.
Hide what doesn't apply: with one account, no account filter; with no projects, no project UI except "Create project".

## Data (config ~/.config/loft/loft.json next to accounts.json; respect XDG_CONFIG_HOME)
projects: [{ id, name, color, emoji?,
             accounts: [accountId,...]   # allowed accounts, [] = any
             defaultAccount: accountId | "auto",   # auto = allowed account with lowest 5h usage
             folders: ["/abs/path", ...] # agents whose cwd is inside belong to this project
           }]
assignments: { "<jobId>": "<projectId>" }   # manual override, wins over folder match
view: { groupBy: "status" | "account" | "project" | "folder" | "none" }  # default per device; server stores a default

## Snapshot additions (keep every existing field)
snapshot.projects = [...projects with counts]
snapshot.groupBy = server default
card.project = projectId | null   (manual > longest folder match > null = "Unsorted")
card.folder = cwd (already there? ensure) ; card.account (exists)

## Routes (token-protected like the rest; phone listener allowed for card assign + read, not for project CRUD unless trivial)
GET  (in snapshot)
POST   /api/projects            {name,color,accounts,defaultAccount,folders}  -> project
PATCH  /api/projects/<id>       partial update
DELETE /api/projects/<id>
POST   /api/cards/<id>/project  {project: id|null}
POST   /api/cards (create) accepts optional project; account resolved: explicit > project.defaultAccount (auto rule) > global default; folder defaults to project's first folder.

## UI rules (desktop + mobile)
- Sidebar/top "Group by" control: Status | Account | Project | Folder | None; remembered per device.
- Filter list under it shows the active dimension's values (All, then each account / project / folder) with counts and color dots.
- Board columns stay status; with groupBy account/project/folder the board shows swimlanes by that dimension.
- Card shows project chip + account dot (only when >1 account exists).
- New task: Project picker first (optional), then account limited to the project's accounts with "Auto" option, folder prefilled.
- Projects screen: create/edit (name, color, accounts multi-select, default account incl. Auto, folders), and "move agent to project" from a card menu.
- First run: "How do you want to organize?" Accounts / Projects / Folders / Just a list; sets groupBy and offers to create projects from recent folders.
- No Cris-specific copy ("Work"/"Personal" only appear as his account labels from config).

## As built (desktop branch loft-flex) — additive to the above, nothing removed
Server and config
- Config lives in `loft.json` under the config dir (`XDG_CONFIG_HOME/loft`). Writes take an flock on `loft.json.lock`. `server.py` now uses the XDG config dir too; before, it ignored XDG.
- `color` is a palette name (blue, green, purple, cyan, pink, orange, yellow, red) or `"#RRGGBB"`. Every project in the snapshot also carries `hex`, the resolved color.
- Each snapshot project also has `counts: {total, needs, running, todo, done}` and `count` (= `counts.total`). Folders are shown with `~` for the home dir.
- Other snapshot fields:
  - `unsorted`: the number of cards with no project.
  - `organized`: false until loft.json exists. Desktop uses it to show the one-time "How do you want to organize?" screen.
  - `groupBy`.
- Card fields:
  - `card.project` is now a project id or null.
  - The old `card.project` value (the folder's display name) moved to `card.folderName`.
  - `card.folder` is the cwd.
- `card.project` resolution order:
  1. Manual assignment.
  2. A todo's own `project` field.
  3. The longest folder match: cwd equal to a folder or under `folder/`.
  4. null.
- Account choice for `POST /api/cards` (create), in this order:
  1. An explicit account id.
  2. `account: "auto"` (or a project default of "auto"). This picks the allowed account with the lowest 5h usage. Accounts with unknown usage go last, and ties keep config order.
  3. The project's `defaultAccount`.
  4. The global default, if the project allows it.
  5. Otherwise, the first account the project allows.
- An unknown project on create returns 400. The todo stores `project`.

Routes
- `POST /api/projects` returns the project's fields at the top level and also `{project: {...}, message}`. `PATCH /api/projects/<id>` does a partial update and returns the same shape.
- `POST /api/cards/<id>/project {project: null}` clears the manual assignment, so the card falls back to folder match.
- `POST /api/view {groupBy}` sets the server default.
- `POST /api/projects/from-folders {folders:[...]}` creates one project per folder. The first-run screen uses it.
- All project routes are also allowed on the phone (tailnet) listener, with the same token.
- The mobile client may read an optional `card.pendingAccount`. The server doesn't emit it yet.

CLI
- `loft project list|add|edit|rm|assign`:
  - `add "Name" --color blue --account work --account personal --default auto --folder ~/x`
  - `assign <job> <project|none>`
- `loft view <status|account|project|folder|none>`.

Desktop UI notes
- `g` cycles Group by and `p` opens Projects. The gear in the top bar opens Accounts or Projects.
- Account switching for a card stays with the loft-switch branch. The card menu only adds "Move to project".
