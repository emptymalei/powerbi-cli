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

2. Install dependencies (including dev and docs extras):
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
counters (`ratelimit.py`) and the data lake store (`store.py`). See
`docs/lake.md`; the commands will move onto this client step by step.

### Running the tests

```bash
uv run pytest
```

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
