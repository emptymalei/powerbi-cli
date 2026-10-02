"""The Textual application: the screens, the sign-in, and the sync that runs in the background.

```python
from pbi_cli.tui import Backend, run

run(Backend(store=store, client_for=pool, sign_in=sign_in))
```

Everything that talks to the lake or the API runs in a worker thread and reports back with
``call_from_thread``, so the screen never freezes. The app is read-only towards Power BI: the
only requests it can make are those of the sync engine, which only reads.
"""

from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from loguru import logger
from textual import work
from textual.app import App
from textual.binding import Binding

from pbi_cli.core.catalog import Catalog, Match
from pbi_cli.core.sync.engine import INTERRUPTED, TOKEN_EXPIRED, RunReport
from pbi_cli.core.sync.plan import SyncOptions
from pbi_cli.errors import AuthError, PBIError, TokenExpiredError
from pbi_cli.tui.backend import Backend, Identity
from pbi_cli.tui.explorer import ExplorerScreen
from pbi_cli.tui.fetching import Fetching
from pbi_cli.tui.modals import (
    WORK_LAKE,
    AccountsModal,
    ChoiceModal,
    ConfirmModal,
    OpenLakeModal,
    SignInModal,
)
from pbi_cli.tui.palette import CommandsProvider, GotoProvider
from pbi_cli.tui.run import RunState
from pbi_cli.tui.status import StatusBar
from pbi_cli.tui.styles import CSS, THEME
from pbi_cli.tui.syncscreen import SyncScreen

#: Seconds between two looks at the token (the countdown in the header).
IDENTITY_EVERY = 30


def lake_label(root: Any, width: int = 36) -> str:
    """Where the lake is, short enough for the header: ``~`` for the home folder."""
    text = str(root)
    home = str(Path.home())
    if text.startswith(home):
        text = "~" + text[len(home) :]
    return text if len(text) <= width else "…" + text[-(width - 1) :]


class PBIApp(App[None]):
    """The pbi terminal UI.

    :param backend: the lake, the clients and the sign-in
    :param tenant: the tenant to browse (default: that of the token, or the only one in the
        lake)
    """

    TITLE = "pbi"
    CSS = CSS
    SCREENS = {"explorer": ExplorerScreen, "sync": SyncScreen}
    COMMANDS = {CommandsProvider, GotoProvider}
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("a", "sign_in", "Sign in"),
        Binding("s", "open_sync", "Sync"),
        Binding("e", "open_explorer", "Explorer", show=False),
        Binding("t", "choose_tenant", "Tenant", show=False),
        Binding("o", "open_lake", "Open lake"),
        Binding("p", "accounts", "Accounts"),
        # the command palette: ":" as in Vim, and Ctrl+P as in VS Code and Obsidian. ":" is a
        # plain key, so a box that is being typed in (a filter, a token) takes it as text
        Binding(":", "command_palette", "Commands", key_display=":"),
        Binding("ctrl+p", "command_palette", "Commands", show=False, priority=True),
    ]

    def __init__(self, backend: Backend, tenant: Optional[str] = None):
        super().__init__()
        self.backend = backend
        self._wanted = tenant
        self.identity = Identity()
        self.identities: List[Identity] = []
        self.tenant: Optional[str] = None
        self.catalog: Optional[Catalog] = None
        self.run_state: Optional[RunState] = None
        self.lake_label = lake_label(backend.store.root)
        self._resume: Optional[Tuple[SyncOptions, str]] = None
        self._sink: Optional[int] = None

    # -- start and end -----------------------------------------------------------------------

    def on_mount(self) -> None:
        self.register_theme(THEME)
        self.theme = "powerbi"
        self.identity = self.backend.identity()
        self.identities = self.backend.identities()
        self.tenant = self._pick_tenant()
        self._sink = logger.add(self._to_log, level="INFO", format="{message}")
        self.push_screen("explorer")
        self.set_interval(IDENTITY_EVERY, self.refresh_identity)
        if self.tenant is not None:
            self.reload_catalog()
        elif len(self.backend.store.tenants()) > 1:
            self.call_after_refresh(self.action_choose_tenant)
        elif self.backend.readonly:
            self.notify(
                "Nothing in this lake yet.", title="View only", severity="warning"
            )
        else:
            self.notify(
                self.identity.problem or "Nothing in the lake yet.",
                title="Not signed in",
                severity="warning",
            )

    def on_unmount(self) -> None:
        if self._sink is not None:
            logger.remove(self._sink)

    def call_from_thread(
        self, callback: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """Hand something to the app from a worker thread, as Textual does, unless the app is
        closing: then the screens that a callback would draw on are being taken apart, and
        what the worker has to say is of no use (it used to fail, now and then, when the
        app was quit while a sync was ending)."""

        def hand_over() -> Any:
            if self._exit or not self.is_running:
                return None
            return callback(*args, **kwargs)

        return super().call_from_thread(hand_over)

    def action_quit(self) -> None:
        """Quit; when a sync is running, ask first, and stop it."""
        state = self.run_state
        if state is None or not state.running:
            self.exit()
            return

        def answered(confirmed: Optional[bool]) -> None:
            if confirmed:
                state.request_stop()  # what is done is kept: running it again continues
                self.exit()

        self.push_screen(
            ConfirmModal(
                "A sync is running",
                "Quit and stop it? The requests in flight finish first, what is done is "
                "kept, and running the sync again continues where it stopped.",
                "Quit",
            ),
            answered,
        )

    def _to_log(self, message: Any) -> None:
        """What the library logs while a sync runs (a quota wait, for example) is shown in
        the log of the run."""
        state = self.run_state
        if state is not None and state.running:
            state.log(str(message).rstrip(), "grey62")

    def _pick_tenant(self) -> Optional[str]:
        if self._wanted:
            return self._wanted
        if self.identity.tenant:
            return self.identity.tenant
        found = self.backend.store.tenants()
        return found[0] if len(found) == 1 else None

    # -- who is signed in --------------------------------------------------------------------

    def refresh_identity(self) -> None:
        """Look at the stored token again (it may have been replaced, or be about to run out)."""
        self.identity = self.backend.identity()
        self.identities = self.backend.identities()
        if self.tenant is None and self.identity.tenant:
            self.tenant = self.identity.tenant
            self.reload_catalog()
        self.refresh_bars()

    def refresh_bars(self) -> None:
        """Draw the header of the screen that is shown again, now."""
        for bar in self.screen.query(StatusBar):
            bar.refresh_status()

    def refuse_when_view_only(self) -> bool:
        """Say so, and return ``True``, when the lake is only looked at."""
        reason = self.backend.readonly
        if reason:
            self.notify(reason, title="View only", severity="warning")
        return bool(reason)

    def action_sign_in(
        self, reason: str = "", group: str = "admin", profile: Optional[str] = None
    ) -> None:
        """Ask for a fresh token (of ``profile``, or of the active one of the group)."""
        if isinstance(self.screen, SignInModal):
            return
        if self.refuse_when_view_only():
            return

        def signed_in(signed: Optional[str]) -> None:
            if signed is None:
                self._resume = None
                return
            self.refresh_identity()
            self.notify(f"Signed in ({signed}).")
            if self._resume is not None:
                options, label_text = self._resume
                self._resume = None
                self.start_sync(options, label_text)

        self.push_screen(SignInModal(self.backend, group, reason, profile), signed_in)

    def action_accounts(self) -> None:
        """List the stored profiles: make one active, or store a new token for it."""
        if self._in_modal() or self.refuse_when_view_only():
            return

        def chosen(picked: Optional[Tuple[str, str, str]]) -> None:
            if picked is None:
                return
            action, group, profile = picked
            if action == "sign_in":
                self.action_sign_in(group=group, profile=profile)
            else:
                self.activate_profile(group, profile)

        self.push_screen(
            AccountsModal(self.backend.accounts(), self.backend.clock()), chosen
        )

    def activate_profile(self, group: str, profile: str) -> None:
        """Make a stored profile the active one of its group, as ``pbi profile switch``
        does (what the Accounts dialog and the command palette ask for)."""
        activate = self.backend.activate
        if activate is None:
            self.notify("This session cannot switch profiles.", severity="warning")
            return
        try:
            activate(group, profile)
        except Exception as error:  # a settings problem must not end the UI
            self.notify(f"Cannot switch: {error}", severity="error")
            return
        self.refresh_identity()
        self.notify(f"{profile} is now the active profile of the group {group}.")

    def explain_sync_problem(
        self, error: BaseException, resume: Optional[Tuple[SyncOptions, str]]
    ) -> None:
        """Say why a sync could not run; for a missing or expired token, ask to sign in."""
        if isinstance(error, AuthError):
            self._resume = resume
            # ask for the kind of token that is missing or expired, not always the admin's
            self.action_sign_in(reason=str(error), group=error.group or "admin")
        elif isinstance(error, PBIError):
            self.notify(str(error), title="Cannot sync", severity="error")
        else:
            self.notify(
                f"{type(error).__name__}: {error}",
                title="Cannot sync",
                severity="error",
            )

    # -- the lake ----------------------------------------------------------------------------

    def reload_catalog(self, announce: bool = False) -> None:
        """Read the lake again, in the background, and rebuild what shows it."""
        if self.tenant is not None:
            self._read_catalog(self.tenant, announce)

    @work(thread=True, exclusive=True, group="catalog")
    def _read_catalog(self, tenant: str, announce: bool) -> None:
        try:
            catalog = Catalog(self.backend.store, tenant, clock=self.backend.clock)
        except Exception as error:  # an unreadable lake must not end the UI
            self.call_from_thread(
                self.notify, f"Cannot read the lake: {error}", severity="error"
            )
            return
        self.call_from_thread(self._catalog_ready, catalog, announce)

    def _catalog_ready(self, catalog: Catalog, announce: bool) -> None:
        self.catalog = catalog
        explorer = self.get_screen("explorer")
        if isinstance(explorer, ExplorerScreen) and explorer.is_mounted:
            explorer.rebuild_tree()
        if announce:
            self.notify("Read the lake again.")

    def action_choose_tenant(self) -> None:
        """Choose which tenant of the lake to browse."""
        found = self.backend.store.tenants()
        if len(found) < 2:
            self.notify("The lake holds only one tenant.")
            return

        def chosen(tenant: Optional[str]) -> None:
            if tenant is not None and tenant != self.tenant:
                self.tenant = tenant
                self.catalog = None
                self.reload_catalog()
                self.refresh_bars()

        self.push_screen(
            ChoiceModal(
                "Which tenant?",
                [(name, name) for name in found],
                "The lake keeps each tenant apart.",
            ),
            chosen,
        )

    def _can_open_lakes(self) -> bool:
        """Whether another lake can be opened now; if not, say why."""
        if self._in_modal():
            return False
        if self.backend.open_lake is None:
            self.notify("This session cannot open another lake.", severity="warning")
            return False
        if self.run_state is not None and self.run_state.running:
            self.notify(
                "A sync is running: stop it, or wait for it, before opening another lake.",
                severity="warning",
            )
            return False
        return True

    def open_lake_at(self, location: str) -> None:
        """Open a lake by its location, without the dialog (the command palette's way)."""
        if self._can_open_lakes():
            self.switch_lake(
                self.backend.work_lake if location == WORK_LAKE else location
            )

    def action_open_lake(self) -> None:
        """Choose another lake to look at (or the work lake again)."""
        if not self._can_open_lakes():
            return

        def chosen(location: Optional[str]) -> None:
            if location is not None:
                self.switch_lake(
                    self.backend.work_lake if location == WORK_LAKE else location
                )

        self.push_screen(
            OpenLakeModal(
                self.backend.work_lake,
                self.backend.recent_lakes(),
                str(self.backend.store.root),
            ),
            chosen,
        )

    def switch_lake(self, location: Optional[str]) -> None:
        """Open a lake, in the background, and show it."""
        if location:
            self._open_lake(location)

    @work(thread=True, exclusive=True, group="open-lake")
    def _open_lake(self, location: str) -> None:
        opener = self.backend.open_lake
        assert opener is not None
        try:
            store = opener(location)
        except Exception as error:  # a bucket that cannot be read must not end the UI
            self.call_from_thread(
                self.notify, f"Cannot open {location}: {error}", severity="error"
            )
            return
        self.call_from_thread(self._lake_opened, store)

    def _lake_opened(self, store: Any) -> None:
        self.backend.store = store
        self.lake_label = lake_label(store.root)
        self.catalog = None
        self.identity = self.backend.identity()
        self.tenant = self._pick_tenant()
        explorer = self.get_screen("explorer")
        if isinstance(explorer, ExplorerScreen) and explorer.is_mounted:
            explorer.rebuild_tree()
        if self.tenant is not None:
            self.reload_catalog()
        elif len(store.tenants()) > 1:
            self.call_after_refresh(self.action_choose_tenant)
        self.refresh_bars()
        self.notify(
            f"Opened {store.root}"
            + (" (view only)." if self.backend.readonly else "."),
        )

    # -- syncing -----------------------------------------------------------------------------

    def start_sync(self, options: SyncOptions, label_text: str) -> bool:
        """Run a sync in the background.

        :return: ``False`` if one is running already, or the lake is only looked at
        """
        if self.refuse_when_view_only():
            return False
        if self.run_state is not None and self.run_state.running:
            self.notify("A sync is already running.", severity="warning")
            return False
        state = RunState(label_text, options, self.backend.clock())
        self.run_state = state
        self._run_sync(state)
        self.refresh_bars()
        return True

    @work(thread=True, group="sync")
    def _run_sync(self, state: RunState) -> None:
        try:
            report = self.backend.engine().run(
                state.options, on_event=state.on_event, stop=state.stop
            )
        except Exception as error:
            # no token for a target, tokens of two tenants, or a bug
            state.log(f"{type(error).__name__}: {error}", "red")
            state.finish(self.backend.clock(), error=str(error))
            self._tell(self._sync_done, state, None, error)
            return
        state.finish(self.backend.clock(), report=report)
        self._tell(self._sync_done, state, report, None)

    def _tell(self, callback: Any, *args: Any) -> None:
        """Run something on the screen's thread, unless the app was closed meanwhile."""
        try:
            self.call_from_thread(callback, *args)
        except RuntimeError:  # the user quit while the sync was finishing
            pass

    def _sync_done(
        self,
        state: RunState,
        report: Optional[RunReport],
        error: Optional[BaseException],
    ) -> None:
        self.reload_catalog()
        self.refresh_bars()
        resume = (state.options, state.label)
        if error is not None:
            self.explain_sync_problem(error, resume)
            return
        assert report is not None
        if report.status == TOKEN_EXPIRED:
            self.explain_sync_problem(TokenExpiredError(report.message), resume)
        elif report.status == INTERRUPTED:
            self.notify(
                f"{state.label}: stopped. What is done is kept; run it again to continue.",
                severity="warning",
            )
        elif report.failures or report.deferred:
            self.notify(
                f"{state.label}: finished, with {len(report.failures)} failure(s) and "
                f"{len(report.deferred)} unit(s) held back.",
                severity="warning",
            )
        else:
            self.notify(f"{state.label}: done.")

    # -- moving between screens ----------------------------------------------------------------

    def _in_modal(self) -> bool:
        return not isinstance(self.screen, (ExplorerScreen, SyncScreen))

    def action_open_sync(self) -> None:
        if not self._in_modal() and not isinstance(self.screen, SyncScreen):
            self.push_screen("sync")

    def action_open_explorer(self) -> None:
        if isinstance(self.screen, SyncScreen):
            self.pop_screen()

    def goto(self, found: Match) -> None:
        """Show a workspace or an item that the command palette found."""
        self.action_open_explorer()
        explorer = self.get_screen("explorer")
        if isinstance(explorer, ExplorerScreen):
            explorer.goto(found.kind, found.id, found.workspace_id)

    def fetching(self) -> Fetching:
        """What this session can fetch for one item: which accounts are stored, and whether
        the lake can be written."""
        return Fetching(self.backend.available_scopes() or None, self.backend.readonly)

    def stop_sync(self) -> None:
        """Ask the sync that runs to stop: it finishes the requests in flight and starts
        nothing new."""
        state = self.run_state
        if state is not None and state.running:
            state.request_stop()
