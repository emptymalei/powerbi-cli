"""What the command palette can do, now: every action of the UI, by name.

The palette (``:`` or ``Ctrl+P``) lists these and runs the one that is chosen. What is
listed depends on the screen it was opened over, on the accounts, and on whether the lake
is only looked at, so that every command that is offered can be run.
"""

from dataclasses import dataclass
from functools import partial
from typing import Any, Callable, List

from pbi_cli.tui.explorer import ExplorerScreen
from pbi_cli.tui.modals import WORK_LAKE
from pbi_cli.tui.syncscreen import SyncScreen

#: The tabs of the Explorer and of the Sync screen: key, name, title, what the tab shows.
EXPLORER_TABS = (
    (
        "1",
        "info",
        "Info",
        "The plain fields of what is selected, and where they come from",
    ),
    ("2", "users", "Users", "Who has access to it"),
    ("3", "lineage", "Lineage", "What it is built from, and what is built on it"),
    ("4", "json", "JSON", "The stored answer, as the API gave it"),
    ("5", "versions", "Versions", "Every stored answer that holds it, newest first"),
    ("6", "details", "Details", "What more there is to know, and what the lake lacks"),
)
SYNC_TABS = (
    ("1", "plan", "Plan", "What the sync would do, and what it costs"),
    ("2", "run", "Run", "The log of the sync that runs, or ran last"),
    ("3", "lake", "Lake", "What the lake holds, and how the last sync went"),
)

#: How many of the lakes opened lately the palette offers.
RECENT_LAKES = 5


@dataclass(frozen=True)
class Command:
    """One thing the palette can do.

    :param title: what it does, as a short phrase that can be searched
    :param help: one line that says more, ending with the key that does the same
    :param run: does it
    :param keywords: other words that find it
    """

    title: str
    help: str
    run: Callable[[], Any]
    keywords: str = ""


def _help(text: str, key: str = "") -> str:
    """The line under a command: what it does, and the key that does the same."""
    return f"{text}  ·  key {key}" if key else text


def _who(account: Any) -> str:
    """What the Accounts dialog knows about a profile, in a few words."""
    parts = [account.name or "", "active" if account.active else ""]
    return ", ".join(part for part in parts if part) or "no name in the token"


def _explorer(screen: Any, view_only: bool) -> List[Command]:
    found: List[Command] = []
    choice = None if view_only else screen.refresh_choice()
    if choice is not None:
        found.append(
            Command(
                choice[0],
                _help("Asks first, and shows the cost against the quotas", "r"),
                screen.action_refresh,
                "fetch again refresh scan sync",
            )
        )
    fetch = None if view_only else screen.fetch_choice()
    if fetch is not None:
        found.append(
            Command(
                fetch[0],
                _help("Asks first, and shows the cost against the quotas", "f"),
                screen.action_fetch_details,
                "details users data sources pages refresh history tiles parameters",
            )
        )
    found.append(
        Command(
            "Filter the tree or the table",
            _help("Show only the rows that have all of your words", "/"),
            screen.action_filter,
            "search find narrow",
        )
    )
    if screen.filtering:
        found.append(
            Command(
                "Clear the filter",
                _help("Show everything again", "Esc"),
                screen.action_clear_filter,
            )
        )
    for key, name, title, text in EXPLORER_TABS:
        found.append(
            Command(
                f"Show the {title} tab",
                _help(text, key),
                partial(screen.action_tab, name),
                "details",
            )
        )
    return found


def _sync(screen: Any) -> List[Command]:
    found: List[Command] = []
    if screen.can_run:
        found.append(
            Command(
                "Run the plan" if screen.plan_mode else "Run the sync",
                _help(
                    (
                        "Go through the steps of the plan file, within the quotas"
                        if screen.plan_mode
                        else "Fetch what the plan lists, within the quotas"
                    ),
                    "r",
                ),
                screen.action_run,
                "start fetch go",
            )
        )
    if screen.plan_mode:
        found.append(
            Command(
                "Read the plan file again",
                _help("Use what the file says now, and plan it again", "l"),
                screen.action_reload_plan,
                "reload config yaml",
            )
        )
    for key, name, title, text in SYNC_TABS:
        found.append(
            Command(
                f"Show the {title} tab",
                _help(text, key),
                partial(screen.action_tab, name),
            )
        )
    return found


def _accounts(app: Any) -> List[Command]:
    backend = app.backend
    found = [
        Command(
            "Accounts…",
            _help("The stored profiles: make one active, or store a new token", "p"),
            app.action_accounts,
            "profile switch token login",
        ),
        Command(
            "Sign in…",
            _help("Store a fresh bearer token", "a"),
            app.action_sign_in,
            "token login expired",
        ),
    ]
    for account in backend.accounts():
        if backend.activate is not None and not account.active:
            found.append(
                Command(
                    f"Make {account.profile} the active {account.group} account",
                    _help(_who(account)),
                    partial(app.activate_profile, account.group, account.profile),
                    "switch profile",
                )
            )
        found.append(
            Command(
                f"Store a new token for {account.profile}",
                _help(f"The {account.group} account " + _who(account)),
                partial(
                    app.action_sign_in, group=account.group, profile=account.profile
                ),
                "login expired",
            )
        )
    return found


def _lakes(app: Any) -> List[Command]:
    backend = app.backend
    found = [
        Command(
            "Open a lake…",
            _help("Look at another lake, such as a shared one (view only)", "o"),
            app.action_open_lake,
            "s3 folder shared published bucket cache",
        )
    ]
    here = str(backend.store.root)
    if backend.readonly and backend.work_lake:
        found.append(
            Command(
                "Open the work lake",
                _help("The lake of your cache folder, the one you can fetch into"),
                partial(app.open_lake_at, WORK_LAKE),
                "cache folder own",
            )
        )
    lately = [
        place
        for place in backend.recent_lakes()
        if place not in (here, backend.work_lake)
    ]
    for place in lately[:RECENT_LAKES]:
        found.append(
            Command(
                f"Open {place}",
                _help("A lake you opened lately (view only)"),
                partial(app.open_lake_at, place),
                "recent s3 folder",
            )
        )
    return found


def commands_for(app: Any, screen: Any) -> List[Command]:
    """What can be done now, from the screen the palette was opened over.

    :param app: the application
    :param screen: the screen under the palette
    """
    backend = app.backend
    view_only = bool(backend.readonly)
    state = app.run_state
    running = state is not None and state.running
    on_explorer = isinstance(screen, ExplorerScreen)

    found: List[Command] = []
    if on_explorer:
        found.extend(_explorer(screen, view_only))
    elif isinstance(screen, SyncScreen):
        found.extend(_sync(screen))
    if running and not state.stopping:
        found.append(
            Command(
                "Stop the sync",
                _help("Finish the requests in flight and start nothing new", "x"),
                app.stop_sync,
                "cancel interrupt halt",
            )
        )
    if not on_explorer:
        found.append(
            Command(
                "Open the Explorer",
                _help("Browse the lake", "e"),
                app.action_open_explorer,
                "tree workspaces browse",
            )
        )
    if not isinstance(screen, SyncScreen):
        found.append(
            Command(
                "Open the Sync screen",
                _help("Plan and run a sync", "s"),
                app.action_open_sync,
                "fetch plan quota",
            )
        )
    found.append(
        Command(
            "Reload the lake",
            _help("Read the lake again, for example after another sync", "l"),
            partial(app.reload_catalog, True),
            "refresh read",
        )
    )
    if not view_only:
        found.extend(_accounts(app))
    if backend.open_lake is not None:
        found.extend(_lakes(app))
    found.append(
        Command(
            "Choose the tenant…",
            _help("The lake keeps each tenant apart", "t"),
            app.action_choose_tenant,
            "switch directory",
        )
    )
    return found
