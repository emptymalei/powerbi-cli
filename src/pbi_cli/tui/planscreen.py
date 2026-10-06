"""The Sync screen of a session that has a plan file (``pbi tui --config``).

What to sync is not chosen in the screen: it is what the file says. The left side shows the
file, the plan is the plan of its steps (the one of ``pbi sync plan --config``, one table with
the number of the step, and the quota that all steps need together), and **Run** goes through
the steps one after the other. ``l`` reads the file again.
"""

from typing import Union

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Button, DataTable, Static
from textual.worker import get_current_worker

from pbi_cli.core.planrun import SequencePlan
from pbi_cli.errors import PBIError
from pbi_cli.tui import render
from pbi_cli.tui.plain import PlainStatic
from pbi_cli.tui.syncscreen import SyncScreen


class PlanSyncScreen(SyncScreen):
    """Plan, run and stop the steps of a plan file."""

    plan_mode = True
    BINDINGS = [Binding("l", "reload_plan", "Read file")]
    PLAN_COLUMNS = (
        "Step",
        "Target",
        "Operation",
        "Units",
        "Fresh",
        "To do",
        "Requests",
    )

    def on_mount(self) -> None:
        # (Textual goes on to the on_mount of SyncScreen by itself: calling it here too would
        # add the columns of the tables twice)
        self.query_one("#run", Button).label = "Run plan"

    # -- the file --------------------------------------------------------------------------

    def compose_left(self) -> ComposeResult:
        yield PlainStatic(self._summary(), id="plan-file")

    def _summary(self) -> Text:
        plan = self.pbi.backend.plan
        return render.plan_file_summary(plan) if plan is not None else Text("")

    def action_reload_plan(self) -> None:
        """Read the plan file again, and plan it again."""
        try:
            self.pbi.backend.read_plan_again()
        except PBIError as error:
            # the file in use stays: a mistake in the new one must not end the session
            self.notify(str(error), title="Plan file", severity="error")
            return
        self.query_one("#plan-file", Static).update(self._summary())
        self.notify("Read the plan file again.")
        self.replan()

    def _apply_accounts(self) -> None:
        """The file says which accounts: nothing to dim."""

    # -- the plan ---------------------------------------------------------------------------

    def _start_plan(self) -> None:
        reason = self.pbi.backend.readonly
        if reason:  # a lake that is only looked at: nothing to plan, nothing to run
            self._show_problem(f"View only. {reason}", style="yellow")
            return
        self._compute_sequence()

    @work(thread=True, exclusive=True, group="plan")
    def _compute_sequence(self) -> None:
        worker = get_current_worker()
        result: Union[SequencePlan, str]
        try:
            result = self.pbi.backend.plan_run().plan()
        except PBIError as error:
            result = str(error)
        except Exception as error:  # an unreadable lake file must not end the UI
            result = f"{type(error).__name__}: {error}"
        if not worker.is_cancelled:
            self.app.call_from_thread(self._show_sequence, result)

    def _show_sequence(self, result: Union[SequencePlan, str]) -> None:
        if isinstance(result, str):
            self._show_problem(result)
            return
        self.plans += 1
        self._planned = result
        self._problem = ""
        plan = self.pbi.backend.plan
        head = f"Plan file: {plan.name if plan is not None else ''}"
        if result.accounts:
            head += f"\nAccounts: {', '.join(result.accounts)}"
        head += f"\nSteps: {len(result.steps)}"
        self.query_one("#plan-head", Static).update(Text(head, style="grey62"))
        table = self.query_one("#plan", DataTable)
        table.clear()
        for row in render.sequence_rows(result):
            table.add_row(*row)
        quota = self.query_one("#quota", DataTable)
        quota.clear()
        for operation, needed, allowed, left, fits in render.sequence_quota_rows(
            result
        ):
            style = "" if fits else "bold red"
            quota.add_row(
                Text(operation, style=style),
                Text(needed, style=style),
                allowed,
                Text(left, style=style),
            )
        self.query_one("#plan-notes", Static).update(
            Text(
                "\n".join(f"· {note}" for note in render.sequence_notes(result)),
                style="grey62",
            )
        )
        self._buttons()

    # -- running ------------------------------------------------------------------------------

    def action_run(self) -> None:
        if self._planned is None:
            self.notify(self._problem or "Wait for the plan.", severity="warning")
            return
        try:
            plan_run = self.pbi.backend.plan_run()
        except PBIError as error:
            self.notify(str(error), title="Plan file", severity="error")
            return
        if self.pbi.start_sync(plan_run, f"Plan {plan_run.plan_file.name}"):
            self.action_tab("run")
