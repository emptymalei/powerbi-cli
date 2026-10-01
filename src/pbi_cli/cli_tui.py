"""``pbi tui``: browse the data lake and sync it, in a terminal UI.

The UI itself is in `pbi_cli.tui` and needs the optional dependency Textual. This module
is what the command line adds to it: it finds the lake and the credentials, and keeps the
logger from writing over the screen.
"""

import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from loguru import logger

from pbi_cli.cli_support import ClientPool
from pbi_cli.config import PBIConfig
from pbi_cli.core.store import LakeStore
from pbi_cli.errors import PBIError
from pbi_cli.session import lake_hint, lake_path
from pbi_cli.tui import Backend, run, textual_available

#: Where the log of the TUI goes: the terminal belongs to the screen.
LOG_NAME = "tui.log"


def log_file() -> Path:
    """The file the TUI writes its log to."""
    return Path.home() / ".pbi_cli" / LOG_NAME


def should_launch() -> bool:
    """Whether a bare ``pbi`` opens the TUI: in a terminal, with Textual and a data lake."""
    return bool(
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and textual_available()
        and lake_path() is not None
    )


def build_backend(pool: ClientPool) -> Backend:
    """What the TUI needs, from the settings and the stored tokens.

    :param pool: the API clients (the TUI resets it after a new token is stored)
    :raises PBIError: when there is no data lake to browse
    """
    from pbi_cli.cli import store_token  # late: pbi_cli.cli imports this module

    path = lake_path()
    if path is None:
        raise PBIError(f"The TUI browses the data lake. {lake_hint()}")

    def sign_in(token: str, profile: str, group: str) -> None:
        store_token(token, profile, group)
        pool.reset()  # the clients look their token up once: make them look again

    return Backend(
        store=LakeStore(path),
        client_for=pool,
        sign_in=sign_in,
        active_profile=lambda group: PBIConfig().get_group_active_profile(group),
    )


def launch(tenant: Optional[str] = None) -> None:
    """Open the TUI.

    :param tenant: the tenant of the lake to browse (default: that of the token)
    :raises PBIError: when Textual is not installed or there is no data lake
    """
    if not textual_available():
        raise PBIError(
            "The TUI needs Textual. Install it with: pip install 'pbi-cli[tui]'"
        )
    with ClientPool() as pool:
        backend = build_backend(pool)
        logger.remove()  # the screen is the terminal's: logs go to a file
        target = log_file()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            logger.add(target, level="INFO", rotation="1 MB", retention=2)
        except OSError:
            pass  # no log is better than no TUI
        try:
            run(backend, tenant=tenant)
        finally:
            logger.remove()
            logger.add(sys.stderr, level="INFO", enqueue=True)


def tui(
    tenant: Annotated[
        Optional[str],
        typer.Option(
            "--tenant",
            "-t",
            help="Tenant of the lake to browse (default: that of the token)",
        ),
    ] = None,
):
    """Browse the data lake, and sync it, in a terminal UI

    The Explorer shows the workspaces of the tenant, what is in them, who can open it,
    how it is connected and how fresh each part is, all from the data lake: it works
    without a token and without a network. The Sync screen plans a sync (what it would
    fetch, and what it costs against the quotas), runs it, and stops it. When the token
    expires, the UI asks for a fresh one and goes on where it stopped.

    Needs the optional dependency Textual: `pip install "pbi-cli[tui]"`. The lake is the
    one of `pbi config set-cache-folder`; it keeps what `pbi sync run` fetches. A bare
    `pbi` in a terminal opens the UI too.

    ```
    pbi tui
    ```
    """
    launch(tenant)
