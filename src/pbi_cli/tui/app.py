"""The Textual application: the screens, the sign-in, and the sync that runs in the background.

```python
from pbi_cli.tui import Backend, run

run(Backend(store=store, client_for=pool, sign_in=sign_in))
```

Everything that talks to the lake or the API runs in a worker thread and reports back with
``call_from_thread``, so the screen never freezes. The app is read-only towards Power BI: the
only requests it can make are those of the sync engine, which only reads.
"""

from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, FrozenSet, List, Optional, Tuple, Union

from loguru import logger
from textual import work
from textual.app import App
from textual.binding import Binding
from textual.notifications import SeverityLevel

from pbi_cli.core.catalog import Catalog, Match
from pbi_cli.core.planfile import SHOW_ALL, find_workspace
from pbi_cli.core.planrun import PlanRun
from pbi_cli.core.sync.engine import INTERRUPTED, TOKEN_EXPIRED, RunReport
from pbi_cli.core.sync.plan import SyncOptions
from pbi_cli.core.timefmt import format_age
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
from pbi_cli.tui.planscreen import PlanSyncScreen
from pbi_cli.tui.run import RunState
from pbi_cli.tui.status import StatusBar
from pbi_cli.tui.styles import CSS, THEME
from pbi_cli.tui.syncscreen import SyncScreen

#: Seconds between two looks at the token (the countdown in the header).
IDENTITY_EVERY = 30

#: Seconds that a notice with something to read in it stays on the screen.
NOTICE_LONG = 15


def first_trouble(report: RunReport) -> str:
    """The first thing that went wrong in a run, in a sentence (empty when nothing did)."""
    if report.failures:
        where, problem = report.failures[0]
        lines = problem.strip().splitlines()
        return f" First failure: {where}: {lines[0] if lines else 'no reason given'}."
    if report.deferred:
        key, wait = report.deferred[0]
        when = (
            f", to be tried again in {format_age(timedelta(seconds=wait))}"
            if wait
            else ""
        )
        return f" First held back: {key}{when}."
    return ""


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

    A backend with a plan file (``pbi tui --config``) gives the Sync screen of the plan file,
    and the settings of its ``session`` section: the workspace to open, and what to do about
    a detail the lake lacks.
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
        # before the app reads it: a plan file has a Sync screen of its own
        self.SCREENS = {
            "explorer": ExplorerScreen,
            "sync": PlanSyncScreen if backend.plan is not None else SyncScreen,
        }
        super().__init__()
        self.backend = backend
        self._wanted = tenant
        self._opened = False
        self.identity = Identity()
        self.identities: List[Identity] = []
        self.tenant: Optional[str] = None
        self.catalog: Optional[Catalog] = None
        self.run_state: Optional[RunState] = None
        self.lake_label = lake_label(backend.store.root)
        self._resume: Optional[Tuple[Union[SyncOptions, PlanRun], str]] = None
        self._signed_in_as: Optional[Tuple[str, Optional[str]]] = None
        self._sink: Optional[int] = None
        #: whether the Explorer shows every workspace (else only those the plan file names)
        self.show_all_workspaces = (
            backend.plan is None or backend.plan.session.workspaces == SHOW_ALL
        )

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
        if self.backend.note:
            self.notify(
                self.backend.note, title="Lake", severity="warning", timeout=NOTICE_LONG
            )
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

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: Optional[float] = None,
        markup: bool = True,
    ) -> None:
        """Show a notification. What it says is shown as it is, whatever ``markup`` asks for:
        it holds names (``[Confidential]Sales``) and what an API answered, and a ``[`` in it is
        not a tag (one that Textual cannot read ends the app)."""
        super().notify(
            message, title=title, severity=severity, timeout=timeout, markup=False
        )

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
            logger.info(
                f"Stored a token for the profile {modal.stored_profile} ({signed})"
            )
            self.refresh_identity()
            self.notify(f"Signed in ({signed}).")
            if self._resume is not None:
                work_to_do, label_text = self._resume
                self._resume = None
                # if the run that goes on stops for this very account, the token was no good
                self._signed_in_as = (signed, modal.stored_profile)
                self.start_sync(work_to_do, label_text)

        asked = (reason.strip().splitlines() or [""])[0]
        logger.info(
            f"Asking for a token of {profile or 'the active profile'} ({group}): {asked}"
        )
        modal = SignInModal(self.backend, group, reason, profile)
        self.push_screen(modal, signed_in)

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
        self,
        error: BaseException,
        resume: Optional[Tuple[Union[SyncOptions, PlanRun], str]],
        signed: Optional[Tuple[str, Optional[str]]] = None,
    ) -> None:
        """Say why a sync could not run; for a missing or expired token, ask to sign in.

        :param signed: the kind and the profile of the token that was stored to let this sync
            go on, when it is the sync that was held up for that
        """
        if isinstance(error, AuthError):
            self._resume = resume
            # ask for the kind of token that is missing or expired, not always the admin's,
            # and store it under the profile it is about (a plan can use several accounts)
            group = error.group or "admin"
            reason = str(error)
            if (
                signed is not None
                and signed[0] == group
                and error.profile in (None, signed[1])
            ):
                # the token that was stored a moment ago did not do: say so, so that it is
                # not taken for the same question again
                reason = (
                    f"The token that was just stored for {signed[1]} ({group}) was "
                    f"refused as well. {reason}"
                )
            self.action_sign_in(reason=reason, group=group, profile=error.profile)
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
        if not [name for name in found if name != self.tenant]:
            # nothing to choose: the lake holds the tenant that is shown, or nothing
            self.notify(
                "The lake holds only one tenant."
                if found
                else "The lake holds no data yet."
            )
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

    def start_sync(
        self, work_to_do: Union[SyncOptions, PlanRun], label_text: str
    ) -> bool:
        """Run a sync, or the steps of a plan file, in the background.

        :param work_to_do: the options of one sync, or a plan run
        :return: ``False`` if one is running already, or the lake is only looked at
        """
        if self.refuse_when_view_only():
            return False
        if self.run_state is not None and self.run_state.running:
            self.notify("A sync is already running.", severity="warning")
            return False
        state = RunState(label_text, work_to_do, self.backend.clock())
        self.run_state = state
        self._run_sync(state)
        self.refresh_bars()
        return True

    @work(thread=True, group="sync")
    def _run_sync(self, state: RunState) -> None:
        try:
            if isinstance(state.work, PlanRun):
                report = state.work.run(
                    on_event=state.on_event, on_step=state.on_step, stop=state.stop
                )
            else:
                report = self.backend.engine().run(
                    state.work, on_event=state.on_event, stop=state.stop
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
        resume = (state.work, state.label)
        signed, self._signed_in_as = self._signed_in_as, None
        if error is not None:
            self.explain_sync_problem(error, resume, signed)
            return
        assert report is not None
        if report.status == TOKEN_EXPIRED:
            expired = TokenExpiredError(
                report.message, group=report.group, profile=report.profile
            )
            self.explain_sync_problem(expired, resume, signed)
        elif report.status == INTERRUPTED:
            self.notify(
                f"{state.label}: stopped. What is done is kept; run it again to continue.",
                severity="warning",
            )
        elif report.failures or report.deferred:
            self.notify(
                f"{state.label}: finished, with {len(report.failures)} failure(s) and "
                f"{len(report.deferred)} unit(s) held back.{first_trouble(report)} "
                "The Run tab of the Sync screen (s) has the rest.",
                severity="warning",
                timeout=NOTICE_LONG,
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

    @property
    def lazy(self) -> str:
        """What the session does about a detail the lake lacks (``session.lazy`` of the plan
        file): ``ask``, ``auto`` or ``off``."""
        plan = self.backend.plan
        return plan.session.lazy if plan is not None else "ask"

    def fetching(self, workspace: Optional[str] = None) -> Fetching:
        """What this session can fetch for one item: which accounts are stored, whether the
        lake can be written, and whether it fetches by itself.

        :param workspace: the id of the workspace of the item, to pick the account of the plan
            file whose own list holds it
        """
        found = (
            self.catalog.workspace(workspace) if self.catalog and workspace else None
        )
        admin, user = self.backend.profiles_for(found.visible_to if found else ())
        return Fetching(
            self.backend.available_scopes() or None,
            self.backend.readonly,
            self.lazy,
            admin,
            user,
            self.backend.plan is not None,
        )

    def workspace_scope(
        self, catalog: Optional[Catalog] = None
    ) -> Optional[FrozenSet[str]]:
        """The ids of the workspaces the Explorer shows, when it shows only those that the plan
        file names; ``None`` when it shows them all (no plan file, a plan that names no
        workspace, or the choice of every workspace)."""
        plan = self.backend.plan
        catalog = catalog or self.catalog
        if (
            plan is None
            or not plan.workspaces
            or self.show_all_workspaces
            or catalog is None
        ):
            return None
        return frozenset(plan.named_workspaces(catalog.workspaces()))

    def action_toggle_scope(self) -> None:
        """Show only the workspaces that the plan file names, or every workspace."""
        plan = self.backend.plan
        if plan is None or not plan.workspaces:
            self.notify(
                "There is no plan file that names workspaces: every workspace is shown.",
                severity="warning",
            )
            return
        self.show_all_workspaces = not self.show_all_workspaces
        self._scope_changed()

    def _scope_changed(self) -> None:
        explorer = self.get_screen("explorer")
        if isinstance(explorer, ExplorerScreen) and explorer.is_mounted:
            explorer.rebuild_tree()
        self._announce_scope()

    def _announce_scope(self) -> None:
        catalog = self.catalog
        total = len(catalog.workspaces()) if catalog is not None else 0
        scope = self.workspace_scope()
        self.notify(
            f"Showing every workspace ({total}). Press w for those of the plan file."
            if scope is None
            else f"Showing the {len(scope)} workspace(s) of the plan file, of {total}. "
            "Press w for every workspace."
        )

    def reveal_workspace(self, workspace_id: str) -> bool:
        """Make sure the Explorer can show a workspace: when it shows only the plan file's and
        this is not one of them, show every workspace and say so.

        :return: whether the choice changed, so that the Explorer has to draw its tree again
        """
        scope = self.workspace_scope()
        if scope is None or workspace_id in scope:
            return False
        self.show_all_workspaces = True
        self._announce_scope()
        return True

    def workspace_to_open(self) -> Optional[str]:
        """The id of the workspace that the plan file says to select at the start, once the
        lake is read; ``None`` when it says none, or after it was selected."""
        plan = self.backend.plan
        if self._opened or self.catalog is None:
            return None
        self._opened = True
        if plan is None or not plan.session.open:
            return None
        found = find_workspace(plan.session.open, self.catalog.workspaces())
        if found is None:
            self.notify(
                f"No workspace of the lake matches '{plan.session.open}' "
                "(session.open of the plan file).",
                severity="warning",
            )
            return None
        return str(found.id)

    def stop_sync(self) -> None:
        """Ask the sync that runs to stop: it finishes the requests in flight and starts
        nothing new."""
        state = self.run_state
        if state is not None and state.running:
            state.request_stop()
