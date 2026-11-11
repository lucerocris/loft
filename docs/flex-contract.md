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
