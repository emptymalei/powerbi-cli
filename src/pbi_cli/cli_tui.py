"""``pbi tui``: browse the data lake and sync it, in a terminal UI.

The UI itself is in `pbi_cli.tui` and needs the optional dependency Textual. This module
is what the command line adds to it: it finds the lake and the credentials, and keeps the
logger from writing over the screen.
"""

import os
import sys
from pathlib import Path
from typing import Annotated, List, Optional

import typer
from loguru import logger

from pbi_cli.cli_support import ClientPool, LakeOption
from pbi_cli.config import VALID_GROUPS, PBIConfig
from pbi_cli.core.jwt import TokenInfo, token_info
from pbi_cli.core.planfile import PlanFile
from pbi_cli.core.store import LakeStore
from pbi_cli.errors import PBIError
from pbi_cli.session import LAKE_ENV, lake_hint, lake_path, resolve_lake
from pbi_cli.tui import Backend, run, textual_available
from pbi_cli.tui.backend import AccountInfo

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


def build_backend(
    pool: ClientPool, lake: Optional[str] = None, plan: Optional[PlanFile] = None
) -> Backend:
    """What the TUI needs, from the settings and the stored tokens.

    :param pool: the API clients (the TUI resets it after a new token is stored)
    :param lake: the lake to look at, as given with ``--lake`` (default: ``PBI_LAKE``, else
        the one the plan file names, else the lake of the cache folder)
    :param plan: the plan file of the session (``pbi tui --config``), if there is one
    :raises PBIError: when there is no data lake to browse, or the one asked for cannot
        be read
    """
    from pbi_cli.cli import store_token  # late: pbi_cli.cli imports this module

    opened = resolve_lake(lake, plan_lake=plan.lake if plan is not None else None)
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

    def accounts() -> List[AccountInfo]:
        """The stored profiles of both groups, with who their tokens are for."""
        from pbi_cli.cli import _get_credential  # late: pbi_cli.cli imports this module

        config = PBIConfig()
        found = []
        for group in VALID_GROUPS:
            active = config.get_group_active_profile(group)
            for profile in config.get_group_profiles(group):
                token = _get_credential(profile)
                info = token_info(token) if token else TokenInfo()
                found.append(
                    AccountInfo(
                        group=group,
                        profile=profile,
                        active=profile == active,
                        name=info.name,
                        tenant=info.tenant_id,
                        expires_at=info.expires_at,
                        has_token=token is not None,
                    )
                )
        return found

    def activate(group: str, profile: str) -> None:
        PBIConfig().set_group_active_profile(group, profile)
        pool.reset()  # the clients look their token up once: make them look again

    def reload_plan() -> PlanFile:
        assert plan is not None and plan.path is not None
        return PlanFile.load(plan.path)

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
        accounts=accounts,
        activate=activate,
        plan=plan,
        reload_plan=reload_plan if plan is not None and plan.path else None,
    )


def launch(
    tenant: Optional[str] = None,
    lake: Optional[str] = None,
    config: Optional[Path] = None,
) -> None:
    """Open the TUI.

    :param tenant: the tenant of the lake to browse (default: that of the token)
    :param lake: the lake to look at (default: ``PBI_LAKE``, else the one the plan file
        names, else the lake of the cache folder); a lake given here is only read
    :param config: a plan file: the Sync screen plans and runs its steps, and its ``session``
        section says which lake to open, which workspace to select and what to do about a
        detail the lake lacks
    :raises PBIError: when Textual is not installed, the plan file is wrong or there is no
        data lake
    """
    if not textual_available():
        raise PBIError(
            "The TUI needs Textual. Install it with: pip install 'pbi-cli[tui]'"
        )
    plan = PlanFile.load(config) if config is not None else None
    with ClientPool() as pool:
        backend = build_backend(pool, lake, plan)
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
    config: Annotated[
        Optional[Path],
        typer.Option(
            "--config",
            "-c",
            exists=True,
            dir_okay=False,
            help=(
                "A plan file (YAML): the Sync screen plans and runs its steps, and its "
                "session section says which lake to open, which workspace to select and "
                "what to do about a detail the lake lacks"
            ),
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

    # Look at a lake that someone shared: no token, no network to Power BI, read-only
    pbi tui --lake s3://my-bucket/pbi-lake

    # With a plan file: its steps on the Sync screen, and its session settings
    pbi tui --config pbi-plan.yaml
    ```

    A lake given with `--lake` is only read: nothing can be fetched into it, and no account
    is needed. Only the lake of the cache folder is written by a sync. With `--config` the
    lake is the one of `--lake`, else of `PBI_LAKE`, else of the plan file's `session.lake`,
    else the lake of the cache folder.
    """
    launch(tenant, lake, config)
