# Changelog

## 0.4.0 (unreleased)

### Changed

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
