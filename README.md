# powerbi-cli

Power BI command line tool

## Setup

### Requirements

* Python 3.10 or higher
* [uv](https://docs.astral.sh/uv/) - Fast Python package installer and resolver

### Development

1. Install uv (if not already installed):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Install dependencies (including dev and docs extras):
   ```bash
   uv sync --all-extras
   ```

3. Install pre-commit hooks:
   ```bash
   uv run pre-commit install
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
