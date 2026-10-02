"""The Sync screen: choose what to fetch, see what it costs, run it, watch it, stop it.

The plan comes from the same planner as ``pbi sync plan`` (so it cannot promise what a run
does not do) and the run from the same engine as ``pbi sync run``. A run goes on while the
user looks at the Explorer, and the screen shows it again when it is opened.
"""

from typing import Any, List, Optional, Set, Union

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.events import ScreenResume
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Input,
    Label,
    ProgressBar,
    SelectionList,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.selection_list import Selection
from textual.worker import get_current_worker

from pbi_cli.core.registry import Scope
from pbi_cli.core.scan import ScanFlags
from pbi_cli.core.sync.plan import MAX_DAYS, MAX_WORKERS, Plan, SyncOptions
from pbi_cli.core.sync.targets import TARGETS, get_target
from pbi_cli.errors import PBIError
from pbi_cli.tui import render
from pbi_cli.tui.status import StatusBar
from pbi_cli.tui.summary import summarize

#: Seconds to wait after the last change before the plan is worked out again.
REPLAN_AFTER = 0.3

#: Seconds between two looks at the run, to show how it goes.
TICK = 0.3

#: Lines of the log of a run that are shown at most (the end of it).
LOG_SHOWN = 2000

_OPTION_BOXES = {
    "force": "Fetch again what is fresh",
    "full-scan": "Scan every workspace, not only what changed",
    "lineage": "Scan: lineage",
    "datasource-details": "Scan: data source details",
    "dataset-schema": "Scan: dataset schema",
    "dataset-expressions": "Scan: dataset expressions",
    "artifact-users": "Scan: users of every item",
}


class SyncScreen(Screen):
    """Plan, run and stop a sync."""

    BINDINGS = [
        Binding("escape", "back", "Explorer"),
        Binding("r", "run", "Run"),
        Binding("x", "stop", "Stop"),
        Binding("1", "tab('plan')", "Plan", show=False),
        Binding("2", "tab('run')", "Run", show=False),
        Binding("3", "tab('lake')", "Lake", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._timer: Optional[Timer] = None
        self._state: Any = None
        self._cursor = 0
        self._log = Text()
        self._log_lines = 0
        self._finished_state: Any = None
        self._planned: Optional[SyncOptions] = None
        self._problem = ""
        self._replan_timer: Optional[Timer] = None
        self.plans = 0  # how many plans were shown: a test waits for the next

    @property
    def pbi(self) -> Any:
        return self.app

    # -- layout --------------------------------------------------------------------------

    def _available(self) -> Set[Scope]:
        """The kinds of account that are stored (none known: all are assumed)."""
        return self.pbi.backend.available_scopes()

    def compose(self) -> ComposeResult:
        available = self._available()
        yield StatusBar()
        with Horizontal(id="sync"):
            with VerticalScroll(id="sync-left"):
                yield Label(
                    Text.assemble(
                        "Targets", ("   ⚠ copies personal data or queries", "grey62")
                    ),
                    classes="heading",
                )
                yield SelectionList[str](
                    *[
                        Selection(
                            Text.assemble(
                                (target.name, "bold"),
                                f"  {target.title}",
                                ("  ⚠" if target.sensitive else "", "yellow"),
                            ),
                            target.name,
                            self._plain(target, available),
                            id=target.name,
                        )
                        for target in TARGETS
                    ],
                    id="targets",
                )
                yield Static("", id="targets-note")
                yield Label("Options", classes="heading")
                for name, text in _OPTION_BOXES.items():
                    yield Checkbox(text, id=name)
                with Horizontal(classes="number"):
                    yield Label("Days of events")
                    yield Input(value=str(MAX_DAYS), type="integer", id="days")
                    yield Label("At once")
                    yield Input(value="4", type="integer", id="workers")
            with Vertical(id="sync-right"):
                with Horizontal(id="sync-buttons"):
                    yield Button("Run sync", variant="primary", id="run", compact=True)
                    yield Button(
                        "Stop", variant="error", id="stop", disabled=True, compact=True
                    )
                    yield Static("", id="run-line")
                with TabbedContent(initial="tab-plan", id="sync-tabs"):
                    with TabPane("Plan", id="tab-plan"):
                        with VerticalScroll():
                            yield Static("", id="plan-head")
                            yield DataTable(
                                id="plan", cursor_type="none", zebra_stripes=True
                            )
                            yield Static("", id="plan-notes")
                            yield Label(
                                "Requests against the quota of each operation",
                                classes="heading",
                            )
                            yield DataTable(
                                id="quota", cursor_type="none", zebra_stripes=True
                            )
                    with TabPane("Run", id="tab-run"):
                        yield ProgressBar(total=1, show_eta=False, id="progress")
                        with VerticalScroll(id="log-scroll"):
                            yield Static("", id="log")
                    with TabPane("Lake", id="tab-lake"):
                        with VerticalScroll():
                            yield DataTable(
                                id="holdings", cursor_type="none", zebra_stripes=True
                            )
                            yield Static("", id="lake-lines")
                            yield Label(
                                "Quota left in the last hour", classes="heading"
                            )
                            yield DataTable(
                                id="used", cursor_type="none", zebra_stripes=True
                            )
        yield Footer(show_command_palette=False)

    @staticmethod
    def _plain(target: Any, available: Set[Scope]) -> bool:
        """Whether a target is chosen at first: the plain ones of the administrator, or of a
        user who has no administrator account."""
        if not available or Scope.ADMIN in available:
            return bool(target.default)
        return bool(target.default_user)

    def _apply_accounts(self) -> None:
        """Dim the targets whose account is not stored, and drop them from the choice."""
        available = self._available()
        targets = self.query_one("#targets", SelectionList)
        for target in TARGETS:
            if available and target.scope not in available:
                targets.disable_option(target.name)
                targets.deselect(target.name)
            else:
                targets.enable_option(target.name)
        highlighted = targets.highlighted
        if highlighted is not None:  # the note names the accounts that are missing
            self._note_for(str(targets.get_option_at_index(highlighted).value))

    def on_mount(self) -> None:
        for table, columns in (
            ("#plan", ("Target", "Operation", "Units", "Fresh", "To do", "Requests")),
            ("#quota", ("Operation", "Needed", "Quota", "Left now")),
            ("#holdings", ("Target", "Operation", "Stored", "Newest")),
            ("#used", ("Operation", "Left (requests left/allowed)")),
        ):
            self.query_one(table, DataTable).add_columns(*columns)
        self._timer = self.set_interval(TICK, self._tick)
        self.replan()
        self.refresh_lake()

    @on(ScreenResume)
    def _resumed(self) -> None:
        self._apply_accounts()
        self.replan()
        self.refresh_lake()
        self._tick()

    def action_back(self) -> None:
        self.pbi.action_open_explorer()

    def action_tab(self, name: str) -> None:
        self.query_one("#sync-tabs", TabbedContent).active = f"tab-{name}"

    # -- what to sync ----------------------------------------------------------------------

    def options(self) -> SyncOptions:
        """The sync the widgets describe.

        :raises PBIError: when a number is not valid
        """
        selected = tuple(self.query_one("#targets", SelectionList).selected)

        def checked(name: str) -> bool:
            return self.query_one(f"#{name}", Checkbox).value

        def number(name: str, low: int, high: int, what: str) -> int:
            raw = self.query_one(f"#{name}", Input).value.strip()
            if not raw.isdigit() or not low <= int(raw) <= high:
                raise PBIError(f"{what} must be a number from {low} to {high}")
            return int(raw)

        return SyncOptions(
            targets=selected,
            force=checked("force"),
            full_scan=checked("full-scan"),
            workers=number("workers", 1, MAX_WORKERS, "Requests at once"),
            days=number("days", 1, MAX_DAYS, "Days of audit events"),
            scan_flags=ScanFlags(
                lineage=checked("lineage"),
                datasource_details=checked("datasource-details"),
                dataset_schema=checked("dataset-schema"),
                dataset_expressions=checked("dataset-expressions"),
                get_artifact_users=checked("artifact-users"),
            ),
        )

    def _note_for(self, name: str) -> None:
        target = get_target(name)
        note = Text()
        note.append(f"{target.name}: {target.title}", style="bold")
        if target.sensitive:
            note.append(f"\nCopies {target.sensitive}.", style="yellow")
        available = self._available()
        for scope in Scope:
            if available and scope not in available:
                kind = "an administrator" if scope is Scope.ADMIN else "a user"
                note.append(
                    f"\nThe dimmed targets need {kind} account, and none is stored: "
                    f"press p, or run `pbi auth -t <token> -g {scope.value}`.",
                    style="yellow",
                )
        self.query_one("#targets-note", Static).update(note)

    @on(SelectionList.SelectionHighlighted)
    def _highlighted(self, event: SelectionList.SelectionHighlighted) -> None:
        self._note_for(str(event.selection.value))

    @on(SelectionList.SelectedChanged)
    @on(Checkbox.Changed)
    @on(Input.Changed)
    def _changed(self) -> None:
        self.replan()

    # -- the plan ----------------------------------------------------------------------------

    def replan(self) -> None:
        """Work out the plan again, a moment after the last change."""
        if self._timer is None:
            return
        if self._replan_timer is not None:
            self._replan_timer.stop()
        self._replan_timer = self.set_timer(REPLAN_AFTER, self._start_plan)

    def _start_plan(self) -> None:
        reason = self.pbi.backend.readonly
        if reason:  # a lake that is only looked at: nothing to plan, nothing to run
            self._show_problem(f"View only. {reason}", style="yellow")
            return
        try:
            options = self.options()
        except PBIError as error:
            self._show_problem(str(error))
            return
        if not options.targets:
            self._show_problem("Select at least one target.")
            return
        self._compute_plan(options)

    @work(thread=True, exclusive=True, group="plan")
    def _compute_plan(self, options: SyncOptions) -> None:
        worker = get_current_worker()
        result: Union[Plan, str]
        try:
            result = self.pbi.backend.engine().plan(options)
        except PBIError as error:
            result = str(error)
        except Exception as error:  # an unreadable lake file must not end the UI
            result = f"{type(error).__name__}: {error}"
        if not worker.is_cancelled:
            self.app.call_from_thread(self._show_plan, options, result)

    def _show_problem(self, text: str, style: str = "red") -> None:
        self.plans += 1
        self._planned = None
        self._problem = text
        self.query_one("#plan-head", Static).update(Text(text, style=style))
        for table in ("#plan", "#quota"):
            self.query_one(table, DataTable).clear()
        self.query_one("#plan-notes", Static).update("")
        self._buttons()

    def _show_plan(self, options: SyncOptions, result: Union[Plan, str]) -> None:
        if isinstance(result, str):
            self._show_problem(result)
            return
        self.plans += 1
        self._planned = options
        self._problem = ""
        head = f"Tenant: {result.tenant}"
        if result.accounts:
            head += f"\nAccounts: {', '.join(result.accounts)}"
        self.query_one("#plan-head", Static).update(Text(head, style="grey62"))
        plan = self.query_one("#plan", DataTable)
        plan.clear()
        for row in render.plan_rows(result):
            plan.add_row(*row)
        quota = self.query_one("#quota", DataTable)
        quota.clear()
        for operation, needed, allowed, left, fits in render.quota_rows(result):
            style = "" if fits else "bold red"
            quota.add_row(
                Text(operation, style=style),
                Text(needed, style=style),
                allowed,
                Text(left, style=style),
            )
        self.query_one("#plan-notes", Static).update(
            Text(
                "\n".join(f"· {note}" for note in render.plan_notes(result)),
                style="grey62",
            )
        )
        self._buttons()

    @property
    def can_run(self) -> bool:
        """Whether **Run** would start the sync now: it is planned, nothing runs, and the lake
        is not only looked at."""
        state = self.pbi.run_state
        running = state is not None and state.running
        return (
            not running and self._planned is not None and not self.pbi.backend.readonly
        )

    def _buttons(self) -> None:
        state = self.pbi.run_state
        running = state is not None and state.running
        self.query_one("#run", Button).disabled = running or self._planned is None
        self.query_one("#stop", Button).disabled = not running or state.stopping

    # -- running -----------------------------------------------------------------------------

    @on(Button.Pressed, "#run")
    def action_run(self) -> None:
        if self._planned is None:
            self.notify(self._problem or "Wait for the plan.", severity="warning")
            return
        if self.pbi.start_sync(self._planned, "Sync"):
            self.action_tab("run")

    @on(Button.Pressed, "#stop")
    def action_stop(self) -> None:
        self.pbi.stop_sync()
        self._buttons()

    def _tick(self) -> None:
        """Show how the run is going, twice a second and when the screen is shown."""
        try:
            self._show_run()
        except NoMatches:
            return  # the app is closing and the screen is taken apart: nothing to show

    def _show_run(self) -> None:
        state = self.pbi.run_state
        if state is not self._state:
            self._state = state
            self._cursor = 0
            self._log = Text()
            self._log_lines = 0
            self.query_one("#log", Static).update("")
        if state is None:
            return
        lines, self._cursor = state.read(self._cursor)
        if lines:
            self._show_lines(state, lines)
        _, done, units, _, _ = state.progress()
        bar = self.query_one("#progress", ProgressBar)
        bar.update(total=max(units, 1), progress=done)
        self.query_one("#run-line", Static).update(
            state.summary(self.pbi.backend.clock)
        )
        self._buttons()
        if not state.running and self._finished_state is not state:
            self._finished_state = state  # it ended: the lake has changed
            self.refresh_lake()
            self.replan()

    def _show_lines(self, state: Any, lines: List[Any]) -> None:
        """Add lines to the log, which scrolls on only if it was at its end."""
        scroll = self.query_one("#log-scroll", VerticalScroll)
        at_end = scroll.scroll_y >= scroll.max_scroll_y - 1
        for text, style in lines:
            self._log.append(text + "\n", style=style)
        self._log_lines += len(lines)
        if self._log_lines > 2 * LOG_SHOWN:  # a very long run: keep the end of it
            tail, _ = state.read(max(0, self._cursor - LOG_SHOWN))
            self._log = Text()
            for text, style in tail:
                self._log.append(text + "\n", style=style)
            self._log_lines = len(tail)
        self.query_one("#log", Static).update(self._log)
        if at_end:
            scroll.scroll_end(animate=False)

    # -- the lake ------------------------------------------------------------------------------

    def refresh_lake(self) -> None:
        """Read what the lake holds and how the last syncs went."""
        if self.pbi.tenant is not None:
            self._read_lake(self.pbi.tenant)

    @work(thread=True, exclusive=True, group="lake")
    def _read_lake(self, tenant: str) -> None:
        worker = get_current_worker()
        backend = self.pbi.backend
        try:
            if backend.readonly:  # only looked at: no account is asked about
                raise PBIError("view only")
            limiter = backend.client_for(Scope.ADMIN).limiter
        except Exception:
            limiter = None  # not signed in: the quota counters stay out of it
        summary: Any = None
        problem = ""
        try:
            summary = summarize(backend.store, tenant, backend.clock(), limiter)
        except Exception as error:  # an unreadable lake file must not end the UI
            problem = f"{type(error).__name__}: {error}"
        if not worker.is_cancelled:
            self.app.call_from_thread(self._show_lake, summary, problem)

    def _show_lake(self, summary: Any, problem: str) -> None:
        holdings = self.query_one("#holdings", DataTable)
        used = self.query_one("#used", DataTable)
        holdings.clear()
        used.clear()
        if summary is None:
            self.query_one("#lake-lines", Static).update(Text(problem, style="red"))
            return
        for row in summary.holdings:
            holdings.add_row(*row)
        for row in summary.quota:
            used.add_row(*row)
        text = Text()
        for line, style in summary.lines:
            text.append(line + "\n", style=style)
        self.query_one("#lake-lines", Static).update(text)
