"""The sync engine: work out what is to do, then fetch it with a pool of threads.

```python
engine = SyncEngine(lambda scope: client_for(scope), store)
plan = engine.plan(SyncOptions())            # only reads the lake
report = engine.run(SyncOptions(targets=("groups", "activity")))
```

A run works in stages (see `pbi_cli.core.sync.plan`). Within a stage the units are worked
on by ``workers`` threads. What goes wrong is sorted out per unit:

- **the token expired** (or was refused): nothing more can succeed, so the run stops. What
  was finished is in the lake, and what was started and not collected (a scan) is in the
  state, so running again continues;
- **a quota is used up**: the unit is *deferred*, and so are the others that need the same
  operation (the quota counters fail them at once, without a request). Running again
  later continues;
- **any other error** (a report that cannot be read, a scan that fails): the unit *failed*;
  it is recorded and the run goes on. A run that keeps getting ``403`` from an
  administrator operation stops asking for it, as the token is not an administrator's;
- **Ctrl-C** ends the run like an expired token does.

Nothing is fetched twice: a unit whose answer is in the lake, and still fresh, is skipped.
"""

import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Set, Tuple

from loguru import logger

from pbi_cli.core.client import PowerBIClient
from pbi_cli.core.registry import ENDPOINTS, Scope, get_endpoint
from pbi_cli.core.store import LakeStore
from pbi_cli.core.sync.plan import Plan, Planner, QuotaLine, SyncOptions, Unit
from pbi_cli.core.sync.runners import (
    CANCELLED,
    DEFERRED,
    DONE,
    FAILED,
    RUNNERS,
    SKIPPED,
    Context,
    Outcome,
)
from pbi_cli.core.sync.state import DEFERRED as MARK_DEFERRED
from pbi_cli.core.sync.state import FAILED as MARK_FAILED
from pbi_cli.core.sync.state import SyncState
from pbi_cli.core.sync.targets import Selection, select_targets
from pbi_cli.errors import (
    ApiError,
    AuthError,
    PBIError,
    RateLimitError,
    Stopped,
    TokenExpiredError,
)

COMPLETED = "completed"
COMPLETED_WITH_FAILURES = "completed_with_failures"
TOKEN_EXPIRED = "token_expired"
INTERRUPTED = "interrupted"

#: After this many ``403`` in a row from an administrator operation, it is not asked again.
FORBIDDEN_STREAK = 3

_ORDER = {endpoint.id: n for n, endpoint in enumerate(ENDPOINTS)}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Event:
    """Something that happened during a run, for showing progress.

    :param kind: ``stage`` (a stage begins) or ``unit`` (a unit is finished)
    :param stage: number of the stage, from 1
    :param units: for a stage, how many units it has
    :param targets: for a stage, the targets that have units in it
    :param unit: for a unit, the unit
    :param outcome: for a unit, what became of it
    :param done: for a unit, how many units of the stage are finished
    :param total: for a unit, how many units the stage has
    """

    kind: str
    stage: int = 0
    units: int = 0
    targets: Tuple[str, ...] = ()
    unit: Optional[Unit] = None
    outcome: Optional[Outcome] = None
    done: int = 0
    total: int = 0


@dataclass
class RunReport:
    """How a run went.

    :param run_id: the id of the run, as it is in the state
    :param status: ``completed``, ``completed_with_failures``, ``token_expired`` or
        ``interrupted``
    :param started_at: when the run began
    :param finished_at: when it ended
    :param counts: how many units were ``done``, ``skipped``, ``failed`` or ``deferred``
    :param by_target: the same by target
    :param failures: the failed units with the reason
    :param deferred: the deferred units with the seconds until a request fits
    :param cancelled: how many units were not done because the run was stopped
    :param notes: what else the reader should know
    :param message: the reason a run was stopped
    :param group: for a run that stopped for an expired token: the kind of account it was
    :param profile: and the profile, when it is known, so that the new token can be stored
        under it
    """

    run_id: str
    started_at: datetime
    status: str = ""
    finished_at: Optional[datetime] = None
    counts: Counter = field(default_factory=Counter)
    by_target: Dict[str, Counter] = field(default_factory=dict)
    failures: List[Tuple[str, str]] = field(default_factory=list)
    deferred: List[Tuple[str, Optional[float]]] = field(default_factory=list)
    cancelled: int = 0
    notes: List[str] = field(default_factory=list)
    message: str = ""
    group: Optional[str] = None
    profile: Optional[str] = None

    @property
    def retry_after(self) -> Optional[float]:
        """Seconds until the first deferred unit can be tried again."""
        waits = [wait for _, wait in self.deferred if wait is not None]
        return min(waits) if waits else None


#: A kind of token and a profile (``None``: the active one): who a request is made as.
Account = Tuple[Scope, Optional[str]]


def _label(scope: Scope, client: PowerBIClient) -> str:
    """How a plan calls an account: ``profile (kind)``."""
    profile = client.profile_name()
    return f"{profile} ({scope.value})" if profile else scope.value


@dataclass
class _Session:
    """Everything one plan or run works with."""

    options: SyncOptions
    selection: Selection
    clients: Dict[Account, PowerBIClient]
    tenant: str
    state: SyncState
    planner: Planner
    context: Context
    stop: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    forbidden_streak: Counter = field(default_factory=Counter)
    forbidden: Set[str] = field(default_factory=set)

    def client(self, scope: Scope) -> PowerBIClient:
        """The client of a kind of token, for the profile this run uses."""
        return self.clients[(scope, self.options.profile_of(scope))]


class SyncEngine:
    """Plans and runs syncs of one tenant into a lake.

    :param client_for: returns the client that signs in for a kind of token, as the profile
        it is given when it is given one (``client_for(scope)`` or ``client_for(scope,
        profile)``); it may raise `PBIError` (no profile, no token), which means that there
        is no account of that kind
    :param store: the lake
    :param clock: the current time (aware, UTC)
    :param sleep: waits for some seconds; by default a wait that a stop cuts short
    :param monotonic: a clock for timeouts
    """

    def __init__(
        self,
        client_for: Callable[..., PowerBIClient],
        store: LakeStore,
        *,
        clock: Callable[[], datetime] = _utcnow,
        sleep: Optional[Callable[[float], None]] = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._client_for = client_for
        self._store = store
        self._clock = clock
        self._sleep = sleep
        self._monotonic = monotonic

    # -- setting up --------------------------------------------------------------------

    def _client(self, scope: Scope, account: Optional[str] = None) -> PowerBIClient:
        if account is None:
            return self._client_for(scope)
        return self._client_for(scope, account)

    def has_account(self, scope: Scope, profile: Optional[str] = None) -> bool:
        """Whether an account is stored: a token under the profile (default: the active one
        of the kind). Nothing is sent: a token is only looked up, and read for its tenant.
        """
        try:
            self._client(scope, profile).tenant_key()
        except PBIError:  # no profile, or no token under it
            return False
        return True

    def profile_of(self, scope: Scope, profile: Optional[str] = None) -> Optional[str]:
        """The name of the profile an account is stored under: the one asked for, else the
        active one of the kind; ``None`` when no account is stored."""
        if not self.has_account(scope, profile):
            return None
        return self._client(scope, profile).profile_name()

    def available_scopes(self, options: Optional[SyncOptions] = None) -> Set[Scope]:
        """The kinds of account that are stored: those whose client knows its token.

        Nothing is sent: a token is only looked up, and read for its tenant.

        :param options: the profiles the sync is to use (default: the active ones)
        """
        return {
            scope
            for scope in Scope
            if self.has_account(scope, options.profile_of(scope) if options else None)
        }

    def tenant(self, options: SyncOptions) -> str:
        """The tenant whose lake a sync with these options works on.

        :raises PBIError: when no account is stored, or the accounts belong to different
            tenants
        """
        return self._session(options).tenant

    def accounts(self, options: SyncOptions) -> List[str]:
        """The accounts a sync with these options would use, each as ``profile (kind)``.

        Nothing is sent. The answer is empty when no account is stored: a plan or a run
        then says what to store.

        :raises PBIError: for a target that needs an account that is not stored
        """
        available = self.available_scopes(options)
        if not available:
            return []
        selection = select_targets(options.targets, available)
        return [
            _label(scope, self._client(scope, options.profile_of(scope)))
            for scope in sorted(
                {t.scope for t in selection.targets}, key=lambda s: s.value
            )
        ]

    def _session(
        self, options: SyncOptions, stop: Optional[threading.Event] = None
    ) -> _Session:
        available = self.available_scopes(options)
        if not available:
            raise AuthError(
                "No account is stored. Store a token with `pbi auth -t <token> -g admin` "
                "(an administrator's) or `pbi auth -t <token> -g user` (a user's)."
            )
        selection = select_targets(options.targets, available)
        if not selection.targets:
            raise PBIError(
                "There is nothing to sync with the accounts that are stored."
            )
        accounts = sorted(
            {(t.scope, options.profile_of(t.scope)) for t in selection.targets},
            key=lambda a: (a[0].value, a[1] or ""),
        )
        clients = {account: self._client(*account) for account in accounts}
        tenants = {client.tenant_key() for client in clients.values()}
        if len(tenants) != 1:
            raise PBIError(
                "The tokens of the targets belong to different tenants "
                f"({', '.join(sorted(tenants))}): a sync keeps one tenant in a lake."
            )
        tenant = tenants.pop()
        state = SyncState(
            self._store, tenant, clock=self._clock, monotonic=self._monotonic
        )
        planner = Planner(
            selection,
            options,
            store=self._store,
            tenant=tenant,
            state=state,
            clock=self._clock,
            identity=lambda scope, account: clients[(scope, account)].identity_key(),
        )
        stop = threading.Event() if stop is None else stop

        def sleep(seconds: float) -> None:
            if stop.is_set():
                raise Stopped()
            if self._sleep is not None:
                self._sleep(seconds)
            elif stop.wait(seconds):
                raise Stopped()

        context = Context(
            options=options,
            store=self._store,
            tenant=tenant,
            state=state,
            planner=planner,
            client_for=lambda scope, account=None: clients[(scope, account)],
            clock=self._clock,
            sleep=sleep,
            monotonic=self._monotonic,
        )
        return _Session(
            options=options,
            selection=selection,
            clients=clients,
            tenant=tenant,
            state=state,
            planner=planner,
            context=context,
            stop=stop,
        )

    # -- planning ----------------------------------------------------------------------

    def plan(self, options: SyncOptions) -> Plan:
        """What a sync with these options would do. Only the lake is read, never the API."""
        session = self._session(options)
        targets, needed = session.planner.dry_run()
        lines = []
        for endpoint_id in sorted(needed, key=lambda e: _ORDER.get(e, len(_ORDER))):
            endpoint = get_endpoint(endpoint_id)
            client = session.client(endpoint.scope)
            hourly = None
            if endpoint.limit is not None:
                hourly = next(
                    (n for n, seconds in endpoint.limit.windows if seconds == 3600),
                    None,
                )
            lines.append(
                QuotaLine(
                    endpoint=endpoint_id,
                    requests=needed[endpoint_id],
                    quota=endpoint.limit.describe() if endpoint.limit else "",
                    left=client.limiter.remaining(endpoint, session.tenant),
                    hourly=hourly,
                )
            )
        return Plan(
            tenant=session.tenant,
            targets=targets,
            quota=lines,
            accounts=[
                _label(scope, client) for (scope, _), client in session.clients.items()
            ],
        )

    # -- running -----------------------------------------------------------------------

    def run(
        self,
        options: SyncOptions,
        on_event: Optional[Callable[[Event], None]] = None,
        stop: Optional[threading.Event] = None,
    ) -> RunReport:
        """Fetch what the targets need into the lake.

        :param options: what to sync, and how
        :param on_event: called (from the thread that runs) as stages begin and units end
        :param stop: set it, from any thread, to end the run: units that are being fetched
            finish, no other unit starts, and the report says ``interrupted``. What is
            done is kept, so running again continues.
        """
        session = self._session(options, stop)
        if self._sleep is None:  # real time: a stop also ends the waits for quota
            for client in session.clients.values():
                client.limiter.interrupt = session.stop
        started = self._clock()
        run_id = session.state.begin_run(session.selection.names, options.summary())
        report = RunReport(run_id=run_id, started_at=started)
        created: Dict[str, int] = {}
        stage = 0
        units = session.planner.roots()
        for unit in units:
            created[unit.target] = created.get(unit.target, 0) + 1
        try:
            while units:
                stage += 1
                outcomes = self._run_stage(session, units, stage, report, on_event)
                units = self._following(session, outcomes, created)
            if report.cancelled:
                report.status = INTERRUPTED
                report.message = "stopped before everything was done"
        except TokenExpiredError as error:
            report.status, report.message = TOKEN_EXPIRED, str(error)
            report.group, report.profile = error.group, error.profile
        except KeyboardInterrupt:
            report.status, report.message = INTERRUPTED, "interrupted by the user"
        finally:
            if stop is None:  # the run's own: release whatever still waits on it
                session.stop.set()
            for client in session.clients.values():
                client.limiter.interrupt = None
        self._finish(session, report, created, started)
        return report

    def _run_stage(
        self,
        session: _Session,
        units: List[Unit],
        stage: int,
        report: RunReport,
        on_event: Optional[Callable[[Event], None]],
    ) -> List[Tuple[Unit, Outcome]]:
        names = tuple(dict.fromkeys(unit.target for unit in units))
        if on_event:
            on_event(Event("stage", stage=stage, units=len(units), targets=names))

        finished: List[Tuple[Unit, Outcome]] = []
        pending = iter(units)
        running: Dict[Future, Unit] = {}
        token_error: Optional[TokenExpiredError] = None
        pool = ThreadPoolExecutor(max_workers=session.options.workers)

        def submit() -> None:
            while (
                len(running) < session.options.workers * 2 and not session.stop.is_set()
            ):
                unit = next(pending, None)
                if unit is None:
                    return
                running[pool.submit(self._execute, session, unit)] = unit

        try:
            submit()
            while running:
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    unit = running.pop(future)
                    try:
                        outcome = future.result()
                    except TokenExpiredError as error:
                        session.stop.set()
                        token_error = token_error or error
                        continue
                    if outcome.status == CANCELLED:
                        report.cancelled += 1
                        continue
                    self._record(session, unit, outcome, report)
                    finished.append((unit, outcome))
                    if on_event:
                        on_event(
                            Event(
                                "unit",
                                stage=stage,
                                unit=unit,
                                outcome=outcome,
                                done=len(finished),
                                total=len(units),
                            )
                        )
                submit()
        except BaseException:
            session.stop.set()  # Ctrl-C: let the units that wait give up
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        if token_error is not None:
            raise token_error
        # the units that were never started, because the run was stopped
        report.cancelled += sum(1 for _ in pending)
        return finished

    def _execute(self, session: _Session, unit: Unit) -> Outcome:
        """Do one unit, in a worker thread. Only an expired token escapes."""
        if session.stop.is_set():
            return Outcome(CANCELLED)
        blocked = self._blocked(session, unit)
        if blocked is not None:
            return blocked
        try:
            outcome = RUNNERS[unit.kind](session.context, unit)
        except TokenExpiredError:
            session.stop.set()  # at once: a queued unit must not ask with a dead token
            raise
        except Stopped:
            return Outcome(CANCELLED)
        except RateLimitError as error:
            return Outcome(
                DEFERRED,
                str(error),
                retry_after=error.retry_after,
                endpoint=error.endpoint or unit.uses[0],
            )
        except ApiError as error:
            self._forbidden(session, unit, error)
            return Outcome(FAILED, str(error))
        except PBIError as error:
            return Outcome(FAILED, str(error))
        except Exception as error:  # a bug must not end the run: record it, go on
            logger.opt(exception=error).debug(f"{unit.key} failed unexpectedly")
            return Outcome(FAILED, f"{type(error).__name__}: {error}")
        with session.lock:
            for endpoint in unit.uses:
                session.forbidden_streak[endpoint] = 0
        return outcome

    def _blocked(self, session: _Session, unit: Unit) -> Optional[Outcome]:
        """A unit that needs an operation Power BI keeps refusing: it is not asked again."""
        with session.lock:
            for endpoint in unit.uses:
                if endpoint in session.forbidden:
                    return Outcome(
                        FAILED,
                        f"skipped: Power BI keeps answering 403 to {endpoint}; "
                        "the token may not be an administrator's",
                    )
        return None

    @staticmethod
    def _forbidden(session: _Session, unit: Unit, error: ApiError) -> None:
        if error.status != 403 or unit.scope is not Scope.ADMIN:
            return
        with session.lock:
            for endpoint in unit.uses:
                session.forbidden_streak[endpoint] += 1
                if session.forbidden_streak[endpoint] >= FORBIDDEN_STREAK:
                    session.forbidden.add(endpoint)

    def _record(
        self, session: _Session, unit: Unit, outcome: Outcome, report: RunReport
    ) -> None:
        """Count an outcome and remember what did not finish (main thread only)."""
        report.counts[outcome.status] += 1
        report.by_target.setdefault(unit.target, Counter())[outcome.status] += 1
        state = session.state
        if outcome.status == FAILED:
            report.failures.append((unit.key, outcome.message))
            state.mark(
                unit.key,
                MARK_FAILED,
                target=unit.target,
                endpoint=unit.endpoint,
                error=outcome.message,
            )
        elif outcome.status == DEFERRED:
            report.deferred.append((unit.key, outcome.retry_after))
            state.mark(
                unit.key,
                MARK_DEFERRED,
                target=unit.target,
                endpoint=unit.endpoint,
                error=outcome.message,
                retry_after=outcome.retry_after,
            )
        else:
            state.clear(unit.key)

    def _following(
        self,
        session: _Session,
        outcomes: List[Tuple[Unit, Outcome]],
        created: Dict[str, int],
    ) -> List[Unit]:
        """The units of the next stage: made from the rows of the units that are done."""
        following: Dict[str, Unit] = {}
        for unit, outcome in outcomes:
            if (
                outcome.status in (DONE, SKIPPED)
                and unit.has_children
                and outcome.rows is not None
            ):
                for child in session.planner.expand(unit, outcome.rows):
                    following.setdefault(child.key, child)
        for unit in following.values():
            created[unit.target] = created.get(unit.target, 0) + 1
        return list(following.values())

    def _finish(
        self,
        session: _Session,
        report: RunReport,
        created: Dict[str, int],
        started: datetime,
    ) -> None:
        report.finished_at = self._clock()
        if not report.status:
            report.status = (
                COMPLETED_WITH_FAILURES if report.counts[FAILED] else COMPLETED
            )

        report.notes.extend(session.planner.unplaced_notes().values())

        for target in session.selection.targets:
            if target.parent and not created.get(target.name):
                parent = report.by_target.get(target.parent, Counter())
                if (
                    parent[FAILED]
                    or parent[DEFERRED]
                    or report.status in (TOKEN_EXPIRED, INTERRUPTED)
                ):
                    report.notes.append(
                        f"{target.name} was not fetched: {target.parent} did not finish, "
                        "and its rows are needed"
                    )

        # Every workspace is scanned as of the start of this run when the units of the scan
        # target all succeeded (the list of workspaces, then every batch) and the run was
        # not cut short: the next scan can then continue from here. A scan of chosen
        # workspaces covers only those, so it moves nothing.
        scanned = report.by_target.get("scan")
        if (
            scanned
            and not session.options.workspace_ids
            and report.status not in (TOKEN_EXPIRED, INTERRUPTED)
            and not scanned[FAILED]
            and not scanned[DEFERRED]
        ):
            session.state.set_scan_baseline(
                session.options.scan_flags, session.planner.coverage(), started
            )
        session.state.end_run(
            report.status,
            {
                status: report.counts[status]
                for status in (DONE, SKIPPED, FAILED, DEFERRED)
            },
            report.message,
        )
