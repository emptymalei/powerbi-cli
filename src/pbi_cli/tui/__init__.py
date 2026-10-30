"""The terminal UI of pbi-cli: browse the data lake, and sync it.

It needs the optional dependency Textual (``pip install "pbi-cli[tui]"``). This package
imports nothing from Textual until `run` is called, so that the command line can tell the
user what to install instead of failing with an import error.
"""

import importlib.util
from typing import Optional

from pbi_cli.tui.backend import Backend, Identity

__all__ = ["Backend", "Identity", "run", "textual_available"]


def textual_available() -> bool:
    """Whether Textual is installed."""
    return importlib.util.find_spec("textual") is not None


def run(backend: Backend, tenant: Optional[str] = None) -> None:
    """Open the TUI and return when the user quits.

    :param backend: what the TUI needs from the world around it
    :param tenant: the tenant of the lake to browse (default: the tenant of the token, or
        the only tenant in the lake)
    """
    from pbi_cli.tui.app import PBIApp  # late: it imports Textual

    PBIApp(backend, tenant=tenant).run()
