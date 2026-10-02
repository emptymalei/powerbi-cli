"""``pbi tui``: browse the data lake and sync it, in a terminal UI.

The UI itself is in `pbi_cli.tui` and needs the optional dependency Textual. This module
is what the command line adds to it: it finds the lake and the credentials, and keeps the
logger from writing over the screen.
"""

import os
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from loguru import logger

from pbi_cli.cli_support import ClientPool, LakeOption
from pbi_cli.config import PBIConfig
from pbi_cli.core.store import LakeStore
from pbi_cli.errors import PBIError
from pbi_cli.session import LAKE_ENV, lake_hint, lake_path, resolve_lake
from pbi_cli.tui import Backend, run, textual_available

#: Where the log of the TUI goes: the terminal belongs to the screen.
LOG_NAME = "tui.log"


def log_file() -> Path:
    """The file the TUI writes its log to."""
    return Path.home() / ".pbi_cli" / LOG_NAME


def should_launch() -> bool:
    """Whether a bare ``pbi`` opens the TUI: in a terminal, with Textual and a data lake
    (the one of the cache folder, or the one that ``PBI_LAKE`` names)."""
    return bool(
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and textual_available()
        and (lake_path() is not None or os.environ.get(LAKE_ENV, "").strip())
    )


def build_backend(pool: ClientPool, lake: Optional[str] = None) -> Backend:
    """What the TUI needs, from the settings and the stored tokens.

    :param pool: the API clients (the TUI resets it after a new token is stored)
    :param lake: the lake to look at, as given with ``--lake`` (default: ``PBI_LAKE``, else
        the lake of the cache folder)
    :raises PBIError: when there is no data lake to browse, or the one asked for cannot
        be read
    """
    from pbi_cli.cli import store_token  # late: pbi_cli.cli imports this module

    opened = resolve_lake(lake)
    if opened is None:
        raise PBIError(
            f"The TUI browses the data lake. {lake_hint()} Or look at a lake that "
            "someone shared: pbi tui --lake <folder or s3://bucket/folder>."
        )
    if not opened.work:
        PBIConfig().remember_lake(str(opened.store.root))

    def sign_in(token: str, profile: str, group: str) -> None:
        store_token(token, profile, group)
        pool.reset()  # the clients look their token up once: make them look again

    def open_lake(location: str) -> LakeStore:
        found = resolve_lake(location)
        assert found is not None  # a location was given
        if not found.work:
            PBIConfig().remember_lake(str(found.store.root))
        return found.store

    work = lake_path()
    return Backend(
        store=opened.store,
        client_for=pool,
        sign_in=sign_in,
        active_profile=lambda group: PBIConfig().get_group_active_profile(group),
        open_lake=open_lake,
        recent_lakes=lambda: PBIConfig().recent_lakes,
        work_lake=str(work) if work is not None else None,
    )


def launch(tenant: Optional[str] = None, lake: Optional[str] = None) -> None:
    """Open the TUI.

    :param tenant: the tenant of the lake to browse (default: that of the token)
    :param lake: the lake to look at (default: ``PBI_LAKE``, else the lake of the cache
        folder); a lake given here is only read
    :raises PBIError: when Textual is not installed or there is no data lake
    """
    if not textual_available():
        raise PBIError(
            "The TUI needs Textual. Install it with: pip install 'pbi-cli[tui]'"
        )
    with ClientPool() as pool:
        backend = build_backend(pool, lake)
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
    lake: LakeOption = None,
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

    # Look at a lake that someone shared: no token, no network to Power BI, read-only
    pbi tui --lake s3://my-bucket/pbi-lake
    ```

    A lake given with `--lake` is only read: nothing can be fetched into it, and no account
    is needed. Only the lake of the cache folder is written by a sync.
    """
    launch(tenant, lake)
