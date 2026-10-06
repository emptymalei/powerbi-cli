# Changelog

## 0.4.0 (unreleased)

### Changed

- The buttons of the [terminal UI](tui.md) are slim, one line each, instead of three-line
  blocks; everything a button does is also a key and a command of the palette.
- `pbi workspaces scan batch` scans in batches of up to 100 workspaces per request, not one
  request per workspace: the API allows 500 scan requests an hour, so the old way ran out
  of quota after 500 workspaces. Every workspace still gets its own file, named as before,
  now holding the result for that workspace alone (the workspace and the data sources it
  uses). A batch that fails is scanned again one workspace at a time, so one failing
  workspace still does not block the rest. An expired or missing token, or Power BI asking to
  wait, is the same for every workspace: the command stops and says so instead of listing
  every workspace as failed. The scans are kept in the [data lake](lake.md) and count
  against the quota.
- `pbi workspaces list`, `pbi users user-access` and `pbi apps list` now use the API client
  and keep what they fetch in the [data lake](lake.md) (the `lake` folder of the cache
  folder). Without `--use-cache` and `--cache-only` they ask the API, as before, and store
  the answer. `--use-cache` takes a stored answer of any age to the same request;
  `--cache-only` never calls the API.
- A stored answer is now the answer to one *request*: the endpoint, its parameters
  (`--top`, `--expand`, `--odata-filter`, the user id, the role) and the tenant. The earlier
  cache had one entry per command, so `--use-cache` could return an answer that had been
  fetched with other options or by another tenant. Entries of the earlier cache are not
  used or migrated; `pbi cache clear` removes them.
- A lake that cannot be read (a damaged file, S3 not reachable) does not stop `--use-cache`:
  the command warns and asks the API. `--cache-only` has no API to fall back on, so it
  stops and says why.
- `pbi apps list --role admin` and `pbi users user-access` read every page. They used to
  return only the first 200 apps and the first page of access entries.
- `pbi reports list` and `pbi reports pages` use the client as well. They always ask the
  API, store the answers in the lake, and print the same JSON as before (only the JSON).
- When a documented quota is used up a command waits, at most two minutes, and otherwise
  stops and says when to try again. A rejected, expired or forbidden request ends with
  `Error: <message>` that says what to do, for example which `pbi auth` command to run.
  The quota counters are kept in `~/.pbi_cli/quota.json`.
- `pbi cache` is now the legacy cache, see [Cache (legacy)](cache.md). `pbi cache clear`
  never deletes the data lake, which is the `lake` folder of the same cache folder, and
  `lake` cannot be used as a cache key.
- The `--help` of the commands above, and of `pbi cache`, describes the data lake.
- The scans kept in the data lake carry the name, type and state of their workspaces in the
  manifest, so that a lake that holds only scans can be browsed by name.
- The CLI is now built with [Typer](https://typer.tiangolo.com) instead of click.
  Commands, flags (including `-ft`, `-tf`, `-wi` and `-wn`), defaults, prompts and exit
  codes are unchanged. `click` is no longer a direct dependency.
- `--help` follows Typer's layout: option values are shown as `<int>` or `<json|excel>`,
  options show their `[default: ...]`, and arguments are listed in an `Arguments:`
  section. The sub-commands of a group are still listed alphabetically.
- `pbi workspaces list --top` is no longer marked `[required]` in `--help`; it always
  had a default of 1000.
- `--interval` and `--timeout` of `pbi workspaces scan get` still reject values of 0 or
  less. The error now reads `must be greater than 0`.
- The docs are built with [Zensical](https://zensical.org) instead of MkDocs Material.
  `mkdocs.yml` is replaced by `zensical.toml` and the look is unchanged (the `classic`
  theme variant). The `mkdocs-jupyter` plugin (no notebooks in the docs) and the unused
  math scripts are gone; code blocks gain a copy button.
- The CLI reference page is generated from the commands by `scripts/gen_cli_docs.py`
  instead of the `mkdocs-click` plugin, which cannot read Typer commands. The docs
  workflows check that the page is up to date before they build.

### Added

- The [terminal UI](tui.md) has a **Scan** tab (key `7`) that shows what the newest scan says: for a
  workspace, how much is in it and **where its data comes from**; for a dataset, each table with its
  columns, measures and what loads it, the places the tables read from, the native queries, the
  parameters and the DAX of the measures; for a report, the dataset it is built on; for a
  dashboard, its tiles; for a dataflow, the data sources it reads. The places are **read out of the
  Power Query expressions** of the tables (`pbi_cli.core.sources`): the server and database of a SQL,
  Oracle, PostgreSQL, MySQL, Snowflake, Synapse, Analysis Services (and other) source, the file and
  sheet of a workbook or CSV, a SharePoint site, a web address, a folder, the SQL of a native query (on
  one line: the `#(lf)` of M is a blank) and
  the tables it reads; a name that stands for a text (a parameter, an earlier step) is followed, and
  what cannot be followed is said. What the scan lacks because of its options (schema, expressions,
  datasource details) is said with how to scan again. `pbi_cli.core.scanmodel` makes the same
  things (`Catalog.scan_model`, `Catalog.dataset_model`) for a script.
- The **Workspaces** node of the Explorer covers all the workspaces its table lists: its Info, Users
  and Scan tabs combine them (everybody with access to any of them, one line for each person and
  workspace, counting a person once; the scans summed, with the places that several workspaces read
  from; how many have their people in the lake), and the table filter `/` narrows the set. Pick a row
  for one workspace. What is combined is read from the lake, so it is shared with it.
- A plan file's **`session.workspaces`** (`plan` or `all`) and the key **`w`**: with a plan file that
  names workspaces the Explorer lists only those (`1 of 4,321`), and `w`, or the palette, lists every
  workspace of the lake and back. Searching and `session.open` respect it, and going to a
  workspace the file does not name lists them all and says so.
- The overview of the lake in the Explorer says **where the lake is** and, when it holds nothing for
  the tenant, what to do (press `s` and Run, or `t` when the lake holds another tenant: `t` used to
  say that the lake holds only one tenant even when it was not the one shown). The header wraps onto
  more lines on a narrow terminal instead of cutting off the lake.
- **A plan file** names what to keep in the lake, for which workspaces and through which
  account, in one YAML file (`pbi_cli.core.planfile`). It is strict: an unknown key, a key
  that does nothing and a profile that has no token are errors that name the file, the line and
  the key. It compiles to ordinary sync runs, one step for the tenant and one for each set of
  workspaces, details and account (`pbi_cli.core.planrun` plans and runs them one after the
  other, and reports them as one). A name in the file is a pattern looked up in the list of
  workspaces in the lake (an `id` may come with a `name` that is only its label, as in the
  file of `pbi workspaces scan batch`); `via: auto` reads with the administrator's account
  when it can and else with the user account whose own list of workspaces holds the
  workspace, and leaves out, with a note, what no stored account can fetch. `scan:` takes a
  mapping or a list of the names of the options, and under `tenant` it asks for the scan of
  every workspace. A plan made before the lake holds the list of workspaces of a user
  account says that it is not known yet which account lists a workspace, instead of saying
  that none does. `session.lake` that names the cache folder (or its `lake` folder) is the
  work lake even before the first sync has made it, a lake that is not there says where its
  location came from and what to leave out, and `pbi sync plan|run --config` says that a
  `session.lake` that is not the work lake is only for `pbi tui --config`.
- **`pbi tui --config FILE`** opens the [terminal UI](tui.md#with-a-plan-file) with a plan file.
  The Sync screen shows the file and the plan of its steps (one table with the number of each
  step, what they cost together, the names that match nothing yet), **Run plan** goes through the
  steps and logs each, and `l` reads the file again. A token that expires asks for the token of
  the account that expired (its kind and its profile, which a plan can use several of; a
  report of a run now says whose token it was), and the plan goes on from its step. The `session`
  section chooses the lake (below `--lake` and `PBI_LAKE`, above the work lake; relative to the
  file), the workspace to select at the start, and what to do about a missing detail: `ask`
  (press `f`), `off`, or `auto`, which fetches by itself the *harmless* details (those that copy no
  personal data, queries or connection details) of the item you stay on, while more than half of
  the smallest allowance of the operation is left, never into a lake that is only looked at.
- **`pbi sync plan --config FILE` and `pbi sync run --config FILE`** take a [plan file](plan-file.md)
  instead of target names. `plan` shows each step and what they cost together against the
  quotas (the steps for the workspaces are worked out after the tenant's, so the plan before
  the first run says which names it cannot look up yet), and `run` goes step after step and
  ends at the first expired token (running again continues). `--force`, `--max-age`, `--days`,
  `--workers`, `--wait`, `--scan-interval` and `--scan-timeout` apply to every step; what the
  file says (target names, the scan options, `--full-scan`, the profiles) is refused on the
  command line with a message that says where in the file it goes.
- `SyncOptions.workspace_ids` limits a sync to some workspaces: besides the scan, the targets
  that fan out over items or workspaces fetch only for those of the chosen workspaces
  (`Target.workspace` says which field of a row holds its workspace; a row that has none is
  left out, and the plan and the run say so). `SyncEngine` can say whether an account is
  stored (`has_account`), under which profile (`profile_of`) and the tenant of a sync.
- **Details of one item, on demand.** Who has access to a dataset, a dashboard, a dataflow or
  a workspace, the data sources of a dataflow, the refresh history and the parameters of a
  dataset, the tiles of a dashboard: each costs a request per item, so a sync keeps them only
  when asked, and the [terminal UI](tui.md#details-one-item-at-a-time) fetches them for the one
  item you look at. The new **Details** tab (`6`) lists what an item can have, what the lake
  holds of each (rows, source, age) and how to get the rest; `f` fetches what is missing, after
  showing the cost, with an administrator's account or a user's, whichever is stored (and says
  which permission a user lacks when the API refuses). The same row is selected again
  afterwards, also after `r`. The palette offers it as a command, and the Users tab says how to
  get the users of the item.
- New operations in the registry, with the targets that fetch them for every item: the users
  of a workspace, dataset, dashboard and dataflow (`group-users`, `dataset-users`,
  `dashboard-users`, `dataflow-users`, 200 requests an hour each), the data sources of a
  dataflow (`dataflow-datasources`), the refresh summaries of the tenant (`refreshables`,
  200 an hour), and for a user account the users, data sources and refresh history of a
  dataset (`user-dataset-users`, `user-dataset-datasources`, `user-dataset-refreshes`, which
  need Reshare or Write permission on the dataset), its parameters, the data sources of a
  dataflow and the tiles of a dashboard. None is in a plain sync. See [Sync](sync.md).
- `SyncOptions.only` limits a sync to some items: the targets that fan out over rows skip the
  rows whose id (`datasetId`, `reportId`, `groupId`, ...) is not wanted. `pbi_cli.core.details`
  says which details an item has, which operations can fetch each, and `Catalog.details` what the
  lake holds of them; `Catalog.users` now reads the users of every kind of item from what was
  fetched, by an administrator or by a user.
- The [terminal UI](tui.md) has a **command palette**: `:` (as in Vim) or `Ctrl+P` (as in VS
  Code and Obsidian), or a click on `: commands` in the header, opens a search bar over every
  action of the UI, each with what it does and the key that does the same. Typing narrows
  the list, `Enter` runs a command. It lists what can be done now (on the Explorer: fetch
  again what is selected, filter, the tabs; on the Sync screen: run, the tabs; while a sync
  runs: stop it, from any screen; everywhere: the other screen, reload, accounts, sign in,
  make a stored profile active, open another lake), and a name jumps to a workspace or an
  item, as before. A place typed in (`s3://bucket/lake`, `/shared/lake`) offers to open it.
- **An administrator account is optional.** pbi works with an administrator's token, with a
  user's token (a service account, for example), or with both. `pbi sync` with no names
  syncs the lists of the tenant when an administrator account is stored, and else what a
  user can see, workspace by workspace: the new targets `user-datasets`, `user-dashboards`
  and `user-dataflows` join `user-groups`, `user-apps` and `user-reports`, and
  `user-group-users` (who has access to each workspace of the account) is there when named.
  Naming a target whose account is not stored fails at once, before anything is fetched, and
  says which `pbi auth` command to run (`pbi sync run scan` with only a user account, for
  example). With no account at all `pbi sync` says what to store. See
  [Authentication](auth.md#which-accounts-you-need) and [Sync](sync.md#which-accounts-a-sync-uses).
- **Several accounts.** `pbi sync plan` and `pbi sync run` take `--admin-profile` and
  `--user-profile` to use other profiles than the active ones for one run. What depends on
  who asks, the workspaces and the apps of a user, is kept in the lake per account, by the
  object id in the token (`oid`, or `appid` of a service principal), so that two accounts
  never overwrite each other's lists; what is the same for everybody who can open a
  workspace stays under the workspace. A token also yields a name (`upn`, ...) to show.
- `pbi sync plan` and `pbi sync run` say which accounts they use (`Accounts: admin-nlm
  (admin), svc-finance (user)`), and so does the plan on the Sync screen of the terminal UI.
  `SyncEngine.accounts(options)` and `Plan.accounts` give the same from Python.
- The [terminal UI](tui.md) knows about accounts. The header shows every account that is
  stored with how long its token lasts, `p` opens the Accounts dialog (the stored profiles
  of both groups; make one active, or store a new token for it), a sync that fails for a
  token asks for the kind that is missing or expired, and the Sync screen dims the targets
  whose account is not stored and says what to do. With only a user account the Explorer
  shows the items of the workspaces that account sees, `r` fetches what you can see, and the
  Info tab of a workspace says which accounts see it.
- `pbi_cli.core.catalog`: items come from the lists a user made of each workspace as well as
  from the lists of the tenant (the administrator's list wins for an item both have),
  `Workspace.visible_to` names the accounts that see a workspace, and `Catalog.listed` says
  whether the lake holds a list that would show the items of a workspace.
- A lake can be [shared](sharing.md). `pbi tui`, `pbi lake ls|show|prune` and `pbi sync
  status` take `--lake <folder or s3://bucket/folder>` (or the environment variable
  `PBI_LAKE`) to look at another lake than the one of the cache folder, for example one that
  a colleague made, with no token. Only the work lake (the cache folder's) is ever written:
  a lake opened any other way is read-only, and the TUI marks it **view only** and asks about
  no account. `o` in the TUI opens another lake, or the work lake again, and remembers the
  lakes it opened. `pbi lake publish DESTINATION` copies a complete, consistent snapshot of
  the lake to a folder or an S3 prefix: it shows what it holds by category (scans, who has
  access, data sources, audit events), lets you leave categories out, asks, never overwrites
  a version, and marks the copy as published (`publish.json`), after which nothing but a
  later publish writes to it.
- `pbi tui` opens a [terminal UI](tui.md) built with [Textual](https://textual.textualize.io)
  (the optional extra `pbi-cli[tui]`). The Explorer shows the workspaces of the tenant, what
  is in them, who can open it, what it is built from and what is built on it (lineage), the
  JSON as stored, and every stored version, with a dot for how fresh each part is, all read
  from the data lake, so it needs no token and no network. The Sync screen plans a sync with
  its cost against the quotas, runs it, shows its log and stops it. When the token expires
  the UI asks for a new one in a dialog and goes on where it stopped. `r` fetches again
  what is selected, after showing what it costs, and the command palette (`Ctrl+P`) jumps
  to a workspace or an item by name. It only reads: the requests are those of `pbi sync`.
- A bare `pbi` in a terminal opens the UI when Textual is installed and a cache folder is
  set; anywhere else it greets as before.
- `pbi_cli.core.catalog`: the read model the UI is made of (workspaces, their items, users,
  lineage, freshness, versions, audit events), for use from Python.
- `pbi_cli.core.sync`: `SyncEngine.run` takes a `stop` event that ends a run (the report
  says `interrupted` and how many units were not started), `SyncOptions.workspace_ids`
  scans only the chosen workspaces without moving the point the next incremental scan
  continues from, and a stop also cuts short a wait for quota.
- Shell completion: `pbi --install-completion` and `pbi --show-completion`.
- `pbi sync plan`, `pbi sync run` and `pbi sync status` keep the tenant in the data lake:
  the lists of workspaces, apps, reports and more, audit events one log per UTC day, and
  metadata scans of every workspace, 100 per request. They work within the quotas, never
  fetch twice what is still fresh, and continue where the last run stopped after an expired
  token, a quota that ran out, a failure or Ctrl-C. Targets that copy personal data or
  queries run only when named. A scan continues from the last complete one with the same
  options. See [Sync](sync.md).
- `pbi_cli.core.scan` and `pbi_cli.core.sync`: the scan job (start, poll, collect, resume)
  and the sync engine, for use from Python.
- `pbi lake ls`, `pbi lake show` and `pbi lake prune`: see what the data lake holds, print a
  stored response or its manifest, and delete old versions. They need no token and no
  network.
- `pbi_cli.core`: an API client with a local [data lake](lake.md). The client reads lists
  page by page, waits when a documented quota is used up or the API answers
  `429 Retry-After`, and keeps what it fetched as versioned snapshots and per-day event
  logs on disk or in S3. A request is identified by its endpoint, its canonical parameters
  and the tenant, so a different `$expand` or another tenant never gets somebody else's
  cached answer.
- `pbi_cli.session` builds the lake and the client from the settings (the cache folder,
  and the quota counters in `~/.pbi_cli`), so the commands share both.
- A registry of the read-only operations the client may call, with the quota of each taken
  from its documentation page.
- pbi_cli reads the tenant and the expiry from a token locally (nothing is sent or
  stored), and says when a token has expired. See [Authentication](auth.md).
- The guides [Authentication](auth.md) and [Data lake](lake.md), and the API reference of
  `pbi_cli.core` and `pbi_cli.errors`.
- Descriptions for the arguments of `pbi workspaces scan`, `pbi profile` and
  `pbi config` in `--help`.

### Fixed

- `pbi tui --config` no longer refuses to start when `session.lake` names a local folder that holds no
  lake: it opens the work lake and says so, with the command that makes that folder the work lake
  (`pbi config set-cache-folder`). `pbi sync plan|run --config` says the same. A bucket that holds no
  lake, and `--lake` or `PBI_LAKE` that name a place without one, are still errors.
- **Where a token is stored** is told truthfully. On Windows a Power BI token is longer than the
  Credential Manager holds, so the credentials file is used; `pbi auth` said `saved securely` right
  after `Keyring not available`, and advised installing a keyring backend, which does not help. It now
  says where the token went and why. A token that did not fit also left an older, shorter token for
  the same profile in the keyring, which is read first, so the new one was never used: it is
  removed.
- The [terminal UI](tui.md) showed names as markup: a workspace called
  `[Confidential]IT Management` was shown as `IT Management` (the bracket was read as a style
  tag), `[Experiment] Test` lost its tag the same way, and a name such as `Sales [/Q4]`
  ended the whole app with a `MarkupError` the moment it was selected. Names, titles,
  tables, dialogs, notices and the details are now shown exactly as they are.
- The empty state of a workspace in the [terminal UI](tui.md) says what the last sync did
  about a list it lacks: that it could not be fetched (with the reason Power BI gave), or was
  held back by a quota. It also names the lists that are missing when only some are, instead
  of always saying "the lists of items". The notice at the end of a sync that had failures
  or held units back names the first problem, and stays fifteen seconds.
- The sign-in dialog of the [terminal UI](tui.md) no longer stores a token that has expired
  already (it said nothing, and the next request asked again), and says when the token it
  stored a moment ago is refused as well, so that a second dialog is not taken for the same
  question. A token that is submitted twice (Enter, then a click before the dialog is gone) is
  stored, and answered, once. With a [plan file](plan-file.md) the header shows the accounts
  of the file, and `f` and `lazy: auto` fetch through them: they used the *active* profile of
  the kind, which could be an account the plan does not name, and was asked for a token the
  plan never uses.
- The [terminal UI](tui.md) no longer fails with an error about a widget that is gone, now and
  then, when it is quit while a sync is ending: what a worker hands over is dropped once the
  app is closing.
- The dialogs of the [terminal UI](tui.md) all come up in the middle of the screen over the
  dimmed one: they are made from one class, `Dialog`, that carries the styles, and a test
  fails for a dialog that is not.
- A workspace that shows no items in the [terminal UI](tui.md) says why: the lists of items
  are not in the lake (after `pbi sync run groups`, for example), or it was never scanned,
  or it is empty. Before, it showed `0 item(s)` and nothing else.
- On Windows the files of the lake were written with CRLF line breaks, and replacing a file
  that another process had open (the TUI reading what a sync rewrites) could fail with
  `PermissionError`. Files are written as they are, and the replace is tried again a few
  times.
- `pbi users user-access --target-folder` wrote nothing: the code that writes the files
  could not run. It now writes the JSON and Excel files.
- `pbi workspaces list --use-cache --file-type excel` crashed; it writes the Excel file.
- `pbi workspaces list --top` below 1 is reported as a usage error.
- `pbi apps augment` and `pbi reports users` with `--file-type excel` and a target that
  has a file extension now report a usage error. They used to crash with a `TypeError`.

### Development

- `pbi_cli.tui.plain` has the widgets that show a `str` as the text it is (`PlainStatic`,
  `PlainLabel`, `PlainTable`); the screens use them instead of Textual's, which read a `str`
  as markup, and `PBIApp.notify` never reads a notice as markup. Nothing in the UI is
  written in markup: styled text is a `rich.text.Text`.
- The fake service in `tests/fake_powerbi.py` knows who asks: the account is the `oid` of the
  token, each account can see its own workspaces (`visible_to`), the admin operations can be
  made to refuse a token that is not an administrator's (`require_admin`), and it serves the
  per-workspace lists a user can read. `World.only_user()` and `World.accounts(...)` make a
  world with only a user, or with several.
- The tests of the terminal UI start the real app with Textual's test pilot against the
  fake service, and are left out when Textual is not installed. The pictures of the
  [terminal UI](tui.md) are made from the real app by `scripts/gen_tui_screenshots.py`.
- `tests/test_sync_http.py` runs the sync engine with 16 workers against the fake service
  behind a real local socket. It found that the HTTP session kept only 10 connections, so
  that the workers of a busy sync opened (and logged a warning for) a new connection per
  request: the session now keeps 32.
- `tests/fake_powerbi.py` is an in-memory Power BI service that enforces the rules of the
  documentation (mandatory `$top`, 1 to 100 ids per scan, events within 28 days in one UTC day,
  the window of `modifiedSince`, results kept for 24 hours) and can inject faults and an
  expiring token. The sync engine and the commands are tested against it.
- Every test runs with an empty home folder and an in-memory keyring (`tests/conftest.py`).
  Some tests stored and deleted tokens in the real keyring, so a test run overwrote or
  removed the entries of profiles named like the ones they use (`admin-nlm`, `user-nlm`)
  on a developer's machine.
- `tests/test_cli_surface.py` compares every command, flag, default and `--help` output
  with recorded fixtures, so a refactor cannot silently change the CLI.
- `tests/test_cli_docs.py` fails when `docs/references/cli.md` is out of date.
