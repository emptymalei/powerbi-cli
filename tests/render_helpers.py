"""Read what a renderer returns, as the text a terminal would show (no Textual needed)."""

import io
from typing import Any

from rich.console import Console


def plain(renderable: Any) -> str:
    """The text of anything a widget can show."""
    if isinstance(renderable, str):
        return renderable
    console = Console(
        file=io.StringIO(),
        width=200,
        force_terminal=False,
        color_system=None,
        record=True,
    )
    console.print(renderable)
    return console.export_text().rstrip("\n")
