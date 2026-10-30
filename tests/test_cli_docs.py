"""The generated CLI reference (docs/references/cli.md) must match the commands.

If this fails, a command, option or docstring changed without regenerating the page:

    uv run python scripts/gen_cli_docs.py
"""

import importlib.util
from pathlib import Path

import cli_surface as surface

GENERATOR = Path(__file__).resolve().parents[1] / "scripts" / "gen_cli_docs.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_cli_docs", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_reference_page_is_up_to_date():
    generator = _load_generator()
    current = generator.OUTPUT.read_text(encoding="utf-8")
    assert current == generator.render_page(), (
        "docs/references/cli.md is out of date. "
        "Run: uv run python scripts/gen_cli_docs.py"
    )


def test_cli_reference_page_documents_every_command():
    generator = _load_generator()
    page = generator.OUTPUT.read_text(encoding="utf-8")
    _, _, command = surface.load_cli()
    missing = [
        " ".join(path)
        for path, _ in surface.walk(command)
        if f"`{' '.join(path)}`" not in page and len(path) > 1
    ]
    assert not missing, f"commands missing from the CLI reference: {missing}"
