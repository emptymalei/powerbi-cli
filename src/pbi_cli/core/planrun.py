"""Plan and run a plan file: the sync runs it is made of, one after the other.

`PlanRun` is what ``pbi sync plan --config``, ``pbi sync run --config`` and the Sync screen of
``pbi tui --config`` use. The steps of a plan file come in two stages, because the second
depends on what the first leaves in the lake: the steps for the tenant fetch the list of
workspaces, and the names of the workspaces of the file are looked up in it.

```python
run = PlanRun(engine, store, PlanFile.load("pbi-plan.yaml"))
plan = run.plan()                  # what it would do: reads the lake only, calls nothing
report = run.run(on_event=print)   # does it
```

A run ends at the first step whose token expired, or when it is stopped; what is done is
kept, and running again continues, as with any sync, because every step skips what the lake
holds fresh.
"""

import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from pbi_cli.core.catalog import Catalog, Workspace
from pbi_cli.core.planfile import Accounting, Compiled, Overrides, PlanFile, Step
from pbi_cli.core.registry import ENDPOINTS
from pbi_cli.core.store import LakeStore
from pbi_cli.core.sync.engine import (
    COMPLETED,
    COMPLETED_WITH_FAILURES,
    INTERRUPTED,
    TOKEN_EXPIRED,
    Event,
    RunReport,
    SyncEngine,
)
from pbi_cli.core.sync.plan import Plan, QuotaLine, SyncOptions
from pbi_cli.core.sync.runners import FAILED
from pbi_cli.errors import AuthError

_ORDER = {endpoint.id: n for n, endpoint in enumerate(ENDPOINTS)}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SequencePlan:
    """What a plan file would do.

    :param steps: each step, with the plan the sync engine makes for it
    :param notes: what else the reader should know
    :param unmatched: the entries of the file that match no workspace the lake knows: where
        each is in the file, and what is wrong with it
    :param accounts: the accounts the steps use, each as ``profile (kind)``
    :param quota: the requests of all steps against the quota of each operation
    """

    steps: List[Tuple[Step, Plan]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    unmatched: List[Tuple[str, str]] = field(default_factory=list)
    accounts: List[str] = field(default_factory=list)
    quota: List[QuotaLine] = field(default_factory=list)

    @property
    def requests(self) -> int:
        return sum(line.requests for line in self.quota)


def merge_quota(plans: Sequence[Plan]) -> List[QuotaLine]:
    """The requests of several plans, against the quota of each operation: the requests are
    added up, and what is left now is what is left (the least, if the plans differ)."""
    merged: Dict[str, QuotaLine] = {}
    for plan in plans:
        for line in plan.quota:
            have = merged.get(line.endpoint)
            if have is None:
                merged[line.endpoint] = QuotaLine(
                    line.endpoint, line.requests, line.quota, line.left, line.hourly
                )
                continue
            have.requests += line.requests
            if line.left is not None:
                have.left = (
                    line.left if have.left is None else min(have.left, line.left)
                )
    return sorted(merged.values(), key=lambda q: _ORDER.get(q.endpoint, len(_ORDER)))


def merge_reports(
    reports: Sequence[RunReport],
    *,
    started: datetime,
    finished: datetime,
    unmatched: Sequence[Tuple[str, str]] = (),
    notes: Sequence[str] = (),
    halted: str = "",
    message: str = "",
    skipped: int = 0,
) -> RunReport:
    """One report for the runs of a plan.

    :param reports: how each step went
    :param started: when the first step began
    :param finished: when the last one ended
    :param unmatched: the entries of the file that matched no workspace; they count as failures
    :param notes: what else to say
    :param halted: ``token_expired`` or ``interrupted`` when the plan stopped before its end
    :param message: why it stopped
    :param skipped: how many steps were not started
    """
    merged = RunReport(
        run_id=reports[-1].run_id if reports else "",
        started_at=started,
        finished_at=finished,
    )
    for report in reports:
        merged.counts.update(report.counts)
        for target, counts in report.by_target.items():
            merged.by_target.setdefault(target, Counter()).update(counts)
        merged.failures.extend(report.failures)
        merged.deferred.extend(report.deferred)
        merged.cancelled += report.cancelled
        for note in report.notes:
            if note not in merged.notes:
                merged.notes.append(note)
    for where, problem in unmatched:
        merged.failures.append((where, problem))
        merged.counts[FAILED] += 1
    for note in notes:
        if note not in merged.notes:
            merged.notes.append(note)
    if skipped:
        merged.notes.append(
            f"{skipped} step(s) were not started: run the plan again to continue with them"
        )
    if halted:
        merged.status, merged.message = halted, message
        # when a token stopped it, the step that stopped it says whose it was
        if halted == TOKEN_EXPIRED and reports:
            merged.group, merged.profile = reports[-1].group, reports[-1].profile
    elif merged.counts[FAILED] or any(
        r.status == COMPLETED_WITH_FAILURES for r in reports
    ):
        merged.status = COMPLETED_WITH_FAILURES
    else:
        merged.status = COMPLETED
    return merged


class PlanRun:
    """A plan file, ready to be planned and run.

    :param engine: the sync engine of the lake
    :param store: the lake (to look the workspaces of the file up in)
    :param plan: the plan file
    :param overrides: what the flags of the command line change in every step
    :param clock: the current time (aware, UTC)
    """

    def __init__(
        self,
        engine: SyncEngine,
        store: LakeStore,
        plan: PlanFile,
        *,
        overrides: Overrides = Overrides(),
        clock: Callable[[], datetime] = _utcnow,
    ):
        self.engine = engine
        self.store = store
        self.plan_file = plan
        self.overrides = overrides
        self._clock = clock

    # -- what the lake knows ------------------------------------------------------------------

    def accounting(self) -> Accounting:
        """Which accounts the plan uses (see `PlanFile.accounting`).

        :raises PlanFileError: when the plan names a profile that has no token
        :raises AuthError: when the plan asks for something and no account is stored
        """
        accounting = self.plan_file.accounting(self.engine)
        wants_something = bool(self.plan_file.tenant or self.plan_file.workspaces)
        if wants_something and not accounting.available:
            raise AuthError(
                "No account is stored. Store a token with `pbi auth -t <token> -g admin` "
                "(an administrator's) or `pbi auth -t <token> -g user` (a user's)."
            )
        return accounting

    def _workspaces(self, accounting: Accounting) -> List[Workspace]:
        """The workspaces the lake knows, as the accounts of the plan see the tenant."""
        options = SyncOptions(
            admin_profile=accounting.admin,
            user_profile=accounting.users[0] if accounting.users else None,
        )
        return Catalog(self.store, self.engine.tenant(options)).workspaces()

    def _first(self, accounting: Accounting) -> List[Step]:
        return self.plan_file.first_steps(accounting)

    def _later(self, accounting: Accounting) -> Compiled:
        # without workspaces there is nothing to look up: the lake need not be read
        if not self.plan_file.workspaces:
            return Compiled()
        return self.plan_file.workspace_steps(accounting, self._workspaces(accounting))

    def steps(self) -> Tuple[List[Step], Compiled]:
        """The steps as the lake is now: those for the tenant, and those for the workspaces
        (which a run works out again once the first have run).

        :raises PBIError: when the plan cannot be carried out with the accounts that are stored
        """
        accounting = self.accounting()
        return self._first(accounting), self._later(accounting)

    # -- planning ------------------------------------------------------------------------------

    def plan(self) -> SequencePlan:
        """What the plan would do. Only the lake is read, never the API.

        The steps for the workspaces are worked out from the list of workspaces the lake
        holds now; a run works them out again after the steps for the tenant, so what it does
        can differ from this when the lake was out of date.

        :raises PBIError: when the plan cannot be carried out with the accounts that are stored
        """
        first, later = self.steps()
        found = SequencePlan(notes=list(later.notes), unmatched=list(later.unmatched))
        plans = []
        for step in [*first, *later.steps]:
            made = self.engine.plan(self.overrides.apply(step.options))
            found.steps.append((step, made))
            plans.append(made)
            for account in made.accounts:
                if account not in found.accounts:
                    found.accounts.append(account)
        found.quota = merge_quota(plans)
        shared = Counter(line.endpoint for plan in plans for line in plan.quota)
        if any(count > 1 for count in shared.values()):
            found.notes.append(
                "Steps share lists: one that an earlier step fetches is not fetched again by "
                "the next, so the requests may be fewer than the sum shown"
            )
        return found

    # -- running -------------------------------------------------------------------------------

    def run(
        self,
        on_event: Optional[Callable[[Event], None]] = None,
        on_step: Optional[Callable[[int, Step], None]] = None,
        stop: Optional[threading.Event] = None,
    ) -> RunReport:
        """Run the steps of the plan, one after the other, and report how it went as one.

        The steps for the tenant run first. The steps for the workspaces are worked out after
        them, from the list of workspaces they left in the lake.

        :param on_event: called as stages begin and units end, as `SyncEngine.run` does
        :param on_step: called as each step begins, with its number (from 1) and the step
        :param stop: set it, from any thread, to end the run: the units in flight finish, no
            other starts, and the report says ``interrupted``
        :raises PBIError: when the plan cannot be carried out with the accounts that are
            stored, or a step cannot start
        """
        stop = threading.Event() if stop is None else stop
        started = self._clock()
        accounting = self.accounting()
        reports: List[RunReport] = []
        number = 0
        halted, message = "", ""
        waiting: List[Step] = []

        def go(steps: Sequence[Step]) -> None:
            nonlocal number, halted, message
            for index, step in enumerate(steps):
                if stop.is_set():
                    halted, message = INTERRUPTED, "stopped before everything was done"
                    waiting.extend(steps[index:])
                    return
                number += 1
                if on_step is not None:
                    on_step(number, step)
                report = self.engine.run(
                    self.overrides.apply(step.options), on_event=on_event, stop=stop
                )
                reports.append(report)
                if report.status in (TOKEN_EXPIRED, INTERRUPTED):
                    halted, message = report.status, report.message
                    waiting.extend(steps[index + 1 :])
                    return

        go(self._first(accounting))
        later = Compiled()
        if not halted:
            # the lake now holds what the first steps fetched
            later = self._later(accounting)
            go(later.steps)
        return merge_reports(
            reports,
            started=started,
            finished=self._clock(),
            unmatched=later.unmatched,
            notes=later.notes,
            halted=halted,
            message=message,
            skipped=len(waiting),
        )
