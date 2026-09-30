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
`ACCEPTED_SURFACE_CHANGES` or `ACCEPTED_HELP_CHANGES` in that test with the reason (and
add a line to `docs/changelog.md`). Regenerate the fixtures only after a reviewed,
intentional change:

```bash
uv run python tests/cli_surface.py --write
```
