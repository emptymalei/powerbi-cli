# UV Installation Guide for PowerBI CLI

This project now supports installation with `uv`, a fast Python package installer and resolver.

## Installing uv

```bash
# On macOS and Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# On Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# Or with pip
pip install uv
```

## Installing PowerBI CLI with uv

### Method 1: Direct installation from source

```bash
# Clone the repository
git clone https://github.com/emptymalei/powerbi-cli.git
cd powerbi-cli

# Install with uv (this will install all dependencies including textual)
uv pip install -e .

# Verify installation
pbi --version
pbi tui
```

### Method 2: Install from PyPI (when published)

```bash
uv pip install pbi-cli
```

### Method 3: Install with specific requirements file

```bash
# Install only core dependencies
uv pip install -r requirements.txt

# Install with dev dependencies
uv pip install -r requirements-dev.txt
```

## Using uv for dependency management

```bash
# Create a virtual environment
uv venv

# Activate the virtual environment
source .venv/bin/activate  # On macOS/Linux
.venv\Scripts\activate     # On Windows

# Install the package in development mode
uv pip install -e .

# Install with optional dependencies
uv pip install -e ".[dev]"     # Development dependencies
uv pip install -e ".[docs]"    # Documentation dependencies
uv pip install -e ".[dev,docs]"  # All optional dependencies
```

## Why uv?

- **Fast**: 10-100x faster than pip
- **Reliable**: Uses the same dependency resolution as pip
- **Compatible**: Works with existing requirements.txt and pyproject.toml files
- **Modern**: Built in Rust for performance

## Troubleshooting

### Textual library not found

If you get an error that Textual library is not found after installation:

```bash
# Reinstall with uv to ensure all dependencies are installed
uv pip install --force-reinstall -e .

# Or explicitly install textual
uv pip install textual>=7.3.0
```

### Virtual environment issues

```bash
# Create a fresh virtual environment
rm -rf .venv
uv venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
uv pip install -e .
```

## Migration from Poetry

This project maintains compatibility with both Poetry and uv. You can use either:

- **Poetry**: `poetry install`
- **uv**: `uv pip install -e .`

The `pyproject.toml` now includes both `[tool.poetry]` and `[project]` sections for maximum compatibility.
