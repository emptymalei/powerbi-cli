# powerbi-cli

Power BI command line tool with interactive Terminal User Interface (TUI)

## Quick Start

### Interactive TUI Mode

Launch the interactive Terminal User Interface for easy access to all features:

```bash
pbi tui
```

![TUI Main Menu](docs/images/tui_01_welcome.svg)

The TUI provides an intuitive interface for:
- Authentication management
- Configuration settings
- Workspaces management
- Apps management
- Reports management
- Users management

See the [TUI documentation](docs/tui.md) for more details.

### Command Line Mode

Use traditional CLI commands:

```bash
pbi --help
```

## Setup

### Requirements

* Python 3.10 or higher

### Installation

#### Using uv (Recommended - Fast & Modern)

```bash
# Install uv if you haven't already
curl -LsSf https://astral.sh/uv/install.sh | sh  # macOS/Linux
# or: pip install uv

# Install pbi-cli
uv pip install pbi-cli

# Or install from source
git clone https://github.com/emptymalei/powerbi-cli.git
cd powerbi-cli
uv pip install -e .
```

See [UV_INSTALLATION.md](UV_INSTALLATION.md) for detailed uv installation instructions.

#### Using pip

```bash
pip install pbi-cli
```

#### Using Poetry

```bash
poetry install
```

### Development

1. Install pre-commit hooks: `pre-commit install`
2. Install dependencies:
   - With uv: `uv pip install -e ".[dev]"`
   - With Poetry: `poetry install`
