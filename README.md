# powerbi-cli

Power BI command line tool

## Setup

### Requirements

* Python 3.10 or higher
* [uv](https://docs.astral.sh/uv/) - Fast Python package installer and resolver
* [pre-commit](https://pre-commit.com/) - Git hook scripts (optional, for development)

### Development

1. Install uv (if not already installed):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Install dependencies (including the dev, docs and tui extras):
   ```bash
   uv sync --all-extras
   ```

3. Install pre-commit (if not already installed) and set up hooks:
   ```bash
   # Install pre-commit at the user level using uv
   uv tool install pre-commit

   # Install the git hooks
   pre-commit install
   ```

### Testing the CLI

You can test the CLI tool without installing it globally using:

```bash
uv run pbi --help
```

Or activate the virtual environment and use the command directly:

```bash
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pbi --help
```

### How the CLI is built

The CLI uses [Typer](https://typer.tiangolo.com). The commands live in
`src/pbi_cli/cli.py`, one `typer.Typer()` per group. Register a command with the
`command()` decorator from `src/pbi_cli/cli_support.py` (it turns a `PBIError`, from
`src/pbi_cli/errors.py`, into `Error: <message>` with exit status 1) and declare options
with `Annotated[..., typer.Option(...)]`. Keep the option names explicit, including short
names such as `-ft`: existing scripts use them.

The code that talks to the Power BI API and keeps what it fetched lives in
`src/pbi_cli/core` and has no dependency on the command line framework: the endpoint
registry with the documented quotas (`registry.py`), the client (`client.py`), the quota
counters (`ratelimit.py`) and the data lake store (`store.py`). See `docs/lake.md`.

The commands that read the API (`workspaces list`, `users user-access`, `apps list` and
`reports list|pages`) go through that client and keep what they fetch in the data lake.
They build it with `pbi_cli.session.open_client`, from the credentials of `load_auth`:
see `_fetch` in `cli.py`. `pbi lake` (`src/pbi_cli/cli_lake.py`) browses the lake.

`pbi sync` (`src/pbi_cli/cli_sync.py`) is a thin layer over the sync engine in
`src/pbi_cli/core/sync`: the catalog of targets (`targets.py`), the planner that works out
units of work from what the lake holds (`plan.py`), the runners (`runners.py`), the
threaded scheduler with its error policy (`engine.py`) and the state it leaves in the lake
(`state.py`). The scan job itself is `src/pbi_cli/core/scan.py`, which `pbi workspaces scan
batch` uses too. See `docs/sync.md`.

`pbi tui` (`src/pbi_cli/cli_tui.py`) opens the terminal UI in `src/pbi_cli/tui`, which is
built with [Textual](https://textual.textualize.io) and is an optional extra
(`pip install "pbi-cli[tui]"`). What it shows is read by `src/pbi_cli/core/catalog.py`, a
read model over the lake with no terminal in it; what it fetches goes through the sync
engine. The `pbi_cli.tui` package imports nothing from Textual until it is started, so
every other command works without the extra. See `docs/tui.md`.

### Running the tests

```bash
uv run pytest
```

Every test runs with an empty home folder and an in-memory keyring (`tests/conftest.py`),
so no test reads or changes your settings, tokens or data lake. To test a command against
the API, script the answers with the `fake_api` fixture; `signed_in` and `cache_folder`
give it a token and a lake (see `tests/test_cli_lake_routing.py`). For the sync engine and
the scans there is a fake Power BI service, `tests/fake_powerbi.py`, that behaves like the
admin API (paging, scans, audit events, quotas) and can inject failures; `tests/sync_helpers.py`
puts it, a lake and an engine on one fake clock.

The tests of the terminal UI (`tests/test_tui_app.py`, `test_tui_explorer.py` and
`test_tui_sync.py`) start the real app with Textual's test pilot on that fake service
(`tests/tui_helpers.py`); `tests/conftest.py` leaves them out when Textual is not installed
(`uv sync --extra tui`). The tests of what the UI is made of that need no terminal
(`test_core_catalog.py`, `test_tui_core.py`, `test_cli_tui.py`) always run.

### The CLI surface snapshot

Existing scripts depend on the exact flags of the CLI (for example `-ft`, `-tf`, `-wn`
and `-wi`). `tests/test_cli_surface.py` guards them: it walks the whole command tree and
compares every command, flag, type, default and `--help` output with the fixtures in
`tests/fixtures/` (`cli_surface.json` and `cli_help.json`).

If the test fails, the CLI changed. If the change was intentional, record it in
`ACCEPTED_SURFACE_CHANGES` in that test with the reason (and add a line to
`docs/changelog.md`). Then re-record the `--help` output, which is expected to change
with every new option or docstring:

```bash
uv run python tests/cli_surface.py --write help
```

`cli_surface.json` is the baseline recorded from the click based CLI that preceded
Typer; only re-record it (`--write surface`) for a reviewed, intentional flag change.

### Building the docs

The docs are built with [Zensical](https://zensical.org); the configuration is in
`zensical.toml`. The command reference (`docs/references/cli.md`) is generated from the
commands, so regenerate it after changing a command, option or docstring. A test, and
the docs workflows, fail when it is stale.

```bash
uv run python scripts/gen_cli_docs.py   # regenerate docs/references/cli.md
uv run zensical serve                   # preview the docs locally
uv run zensical build --clean           # build them into site/
```

The pictures of `docs/tui.md` are made from the real app, on a made-up tenant. Run
`uv run python scripts/gen_tui_screenshots.py` when the UI changes and commit the SVG files
in `docs/images`.
