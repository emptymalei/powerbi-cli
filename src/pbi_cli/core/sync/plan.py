"""Planning a sync: what there is to do, what it costs, and what is already in the lake.

The work of a sync is cut in *units*: one request (or, for a scan, one job) and the place in
the lake where its result goes. The `Planner` builds the units. The same code serves a dry
run (`Planner.dry_run`, which only reads the lake and never calls the API) and a real run
(`pbi_cli.core.sync.engine`), so that the plan cannot promise something a run does not do.

Units come in stages. The first stage holds what needs no other result: lists, event
days, the list of modified workspaces. Units of later stages are made from the rows of an
earlier stage: the users of each report from the list of reports, the scan of each batch of
100 workspaces from the list of modified workspaces. A dry run takes those rows from what
the lake holds, however old, and says so when it has none.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from math import ceil
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from pbi_cli.core.client import rows_of
from pbi_cli.core.registry import Scope, get_endpoint
from pbi_cli.core.scan import MAX_WORKSPACES, ScanFlags, batch_key, chunked, latest_scan
from pbi_cli.core.store import LakeStore
from pbi_cli.core.sync.state import SyncState
from pbi_cli.core.sync.targets import Mode, Selection, Target, children_of, get_target
from pbi_cli.errors import PBIError

#: Most threads a sync may use (a scan job is one thread, and 16 scans may run at once).
MAX_WORKERS = 16

#: Most days of audit events the API keeps.
MAX_DAYS = 28

#: A scan looks this far back before the start of the previous one, to be sure that no
#: change in between is missed (it costs a little rescanning).
MODIFIED_OVERLAP = timedelta(hours=1)

#: ``modifiedSince`` may not be younger than this (changes need time to take effect) ...
MODIFIED_MIN_AGE = timedelta(minutes=31)

#: ... nor older than 30 days; beyond this every workspace is scanned again.
MODIFIED_MAX_AGE = timedelta(days=29)

#: Requests that one scan of a batch is expected to need: start, result, and a few checks.
SCAN_REQUESTS = {"admin.scan.start": 1, "admin.scan.status": 3, "admin.scan.result": 1}

#: Events per request, when estimating how many requests a day of events takes.
EVENTS_PER_REQUEST = 1000

SNAPSHOT = "snapshot"
EVENTS = "events"
MODIFIED = "modified"
SCAN = "scan"


@dataclass(frozen=True)
class SyncOptions:
    """What a sync does, besides which targets.

    :param targets: names of the targets (none: the plain ones)
    :param force: fetch again even what the lake holds fresh (complete days of events are
        never fetched again: they cannot change)
    :param max_age: a stored answer younger than this is fresh (default: per target)
    :param workers: units worked on at the same time, at most 16
    :param max_wait: seconds a request may wait for quota before its unit is held back;
        ``None`` waits as long as needed
    :param days: how many days of events, today included, at most 28
    :param seal_grace: a day of events is complete only this long after it ended, because
        events arrive late
    :param scan_flags: what the scans include
    :param full_scan: scan every workspace, not only those that changed since the last scan
        (``force`` does so too)
    :param exclude_personal: leave the personal workspaces out of the scans
    :param exclude_inactive: leave the inactive workspaces out of the scans
    :param scan_interval: seconds between two status checks of a scan
    :param scan_timeout: seconds to wait for one scan to succeed
    """

    targets: Tuple[str, ...] = ()
    force: bool = False
    max_age: Optional[timedelta] = None
    workers: int = 4
    max_wait: Optional[float] = 120.0
    days: int = MAX_DAYS
    seal_grace: timedelta = timedelta(hours=24)
    scan_flags: ScanFlags = ScanFlags()
    full_scan: bool = False
    exclude_personal: bool = False
    exclude_inactive: bool = False
    scan_interval: float = 5.0
    scan_timeout: float = 600.0

    def __post_init__(self) -> None:
        if not 1 <= self.workers <= MAX_WORKERS:
            raise PBIError(f"workers must be between 1 and {MAX_WORKERS}")
        if not 1 <= self.days <= MAX_DAYS:
            raise PBIError(f"days must be between 1 and {MAX_DAYS}")
        if self.scan_interval <= 0 or self.scan_timeout <= 0:
            raise PBIError("the scan interval and timeout must be greater than 0")
        if self.max_wait is not None and self.max_wait < 0:
            raise PBIError("the time to wait for quota cannot be negative")
        if self.max_age is not None and self.max_age < timedelta(0):
            raise PBIError("max age cannot be negative")

    def summary(self) -> Dict[str, Any]:
        """The options as plain values, for the record of a run."""
        return {
            "force": self.force,
            "max_age": self.max_age.total_seconds() if self.max_age else None,
            "workers": self.workers,
            "days": self.days,
            "scan_flags": self.scan_flags.canonical(),
            "full_scan": self.full_scan,
            "exclude_personal": self.exclude_personal,
            "exclude_inactive": self.exclude_inactive,
        }


@dataclass(frozen=True)
class Unit:
    """One piece of work.

    :param key: stable identifier, for example ``admin.reports.users?reportId=rep-1``
    :param target: name of the target it belongs to
    :param kind: ``snapshot``, ``events``, ``modified`` (the workspaces to scan) or ``scan``
    :param endpoint: registry id of the requests
    :param scope: the kind of token the requests need
    :param params: parameters of the request
    :param ttl: how long a stored answer stays fresh (default: the endpoint's)
    :param day: the UTC day of an ``events`` unit
    :param workspace_ids: the workspaces of a ``scan`` unit
    :param uses: the operations whose quota the unit spends
    :param has_children: whether the units of another target are made from its rows
    """

    key: str
    target: str
    kind: str
    endpoint: str
    scope: Scope
    params: Mapping[str, Any] = field(default_factory=dict)
    ttl: Optional[timedelta] = None
    day: Optional[date] = None
    workspace_ids: Tuple[str, ...] = ()
    uses: Tuple[str, ...] = ()
    has_children: bool = False


def unit_key(endpoint_id: str, params: Mapping[str, str]) -> str:
    """A readable identifier of a request: the endpoint and its canonical parameters."""
    if not params:
        return endpoint_id
    return endpoint_id + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class TargetPlan:
    """What a dry run found out about one target.

    :param target: the target
    :param implied: whether it is only here because another target needs its rows
    :param units: how many units it has, ``None`` while that depends on a result that is not
        in the lake yet
    :param fresh: how many of them the lake already answers
    :param requests: requests the remaining ones are expected to need, ``None`` if unknown
    :param notes: what else the reader should know
    """

    target: Target
    implied: bool = False
    units: Optional[int] = 0
    fresh: int = 0
    requests: Optional[int] = 0
    notes: List[str] = field(default_factory=list)

    @property
    def todo(self) -> Optional[int]:
        return None if self.units is None else self.units - self.fresh

    def add_note(self, note: str) -> None:
        """Add a note, once."""
        if note not in self.notes:
            self.notes.append(note)


@dataclass
class QuotaLine:
    """The requests a sync needs from one operation, against its quota.

    :param endpoint: registry id
    :param requests: requests the sync is expected to need
    :param quota: the documented quota in words, empty if there is none
    :param left: requests that fit right now, ``None`` without a documented quota
    :param hourly: requests per hour the quota allows, if it says so
    """

    endpoint: str
    requests: int
    quota: str
    left: Optional[int]
    hourly: Optional[int] = None

    @property
    def fits(self) -> bool:
        return self.left is None or self.requests <= self.left

    @property
    def hours(self) -> Optional[int]:
        """Whole hours until everything could be fetched, when it does not fit now."""
        if self.fits or not self.hourly:
            return None
        return ceil((self.requests - (self.left or 0)) / self.hourly)


@dataclass
class Plan:
    """What a sync would do.

    :param tenant: the tenant whose lake it is
    :param targets: one entry per target
    :param quota: one line per operation that needs requests
    :param notes: what else the reader should know
    """

    tenant: str
    targets: List[TargetPlan]
    quota: List[QuotaLine] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def requests(self) -> int:
        return sum(line.requests for line in self.quota)


class Planner:
    """Builds the units of a sync.

    :param selection: the targets
    :param options: what the sync does
    :param store: the lake
    :param tenant: the tenant whose data it holds
    :param state: what earlier runs left behind (for incremental scans)
    :param clock: returns the current time (aware, UTC)
    """

    def __init__(
        self,
        selection: Selection,
        options: SyncOptions,
        *,
        store: LakeStore,
        tenant: str,
        state: SyncState,
        clock: Callable[[], datetime] = _utcnow,
    ):
        self.selection = selection
        self.options = options
        self._store = store
        self._tenant = tenant
        self._state = state
        self._clock = clock
        self._selected = {target.name for target in selection.targets}

    # -- building units ---------------------------------------------------------------

    def _children(self, name: str) -> List[Target]:
        """The selected targets that work on the rows of a target."""
        return [child for child in children_of(name) if child.name in self._selected]

    def _snapshot(self, target: Target, params: Mapping[str, Any]) -> Unit:
        endpoint = get_endpoint(target.endpoint)
        return Unit(
            key=unit_key(endpoint.id, endpoint.canonical_params(params)),
            target=target.name,
            kind=SNAPSHOT,
            endpoint=endpoint.id,
            scope=target.scope,
            params=dict(params),
            ttl=target.ttl,
            uses=(endpoint.id,),
            has_children=bool(self._children(target.name)),
        )

    def coverage(self) -> Dict[str, bool]:
        """Which workspaces the scans cover (for telling what an earlier scan covered)."""
        return {
            "excludePersonalWorkspaces": self.options.exclude_personal,
            "excludeInActiveWorkspaces": self.options.exclude_inactive,
        }

    def modified_since(self) -> Optional[datetime]:
        """Where an incremental scan continues from, or ``None`` if everything is scanned.

        Everything is scanned when asked to (``full_scan``, or ``force``, which fetches
        everything again), when there is no earlier complete scan with the same flags and
        coverage, or when it was too long ago for the API to tell.
        """
        if self.options.full_scan or self.options.force:
            return None
        baseline = self._state.scan_baseline(self.options.scan_flags, self.coverage())
        if baseline is None:
            return None
        now = self._clock()
        since = baseline - MODIFIED_OVERLAP
        if now - since > MODIFIED_MAX_AGE:
            return None
        return min(since, now - MODIFIED_MIN_AGE)

    def _modified(self, target: Target) -> Unit:
        params: Dict[str, Any] = dict(self.coverage())
        since = self.modified_since()
        if since is not None:
            params["modifiedSince"] = f"{since:%Y-%m-%dT%H:%M:%S}.000Z"
        endpoint = get_endpoint("admin.workspaces.modified")
        return Unit(
            key=unit_key(endpoint.id, endpoint.canonical_params(params)),
            target=target.name,
            kind=MODIFIED,
            endpoint=endpoint.id,
            scope=target.scope,
            params=params,
            uses=(endpoint.id,),
            has_children=True,
        )

    def _scan_batch(
        self, target: Target, ids: Sequence[str], since: Optional[str] = None
    ) -> Unit:
        return Unit(
            key=f"admin.scan@{batch_key(ids)}",
            target=target.name,
            kind=SCAN,
            endpoint=target.endpoint,
            scope=target.scope,
            params={"modifiedSince": since} if since else {},
            ttl=target.ttl,
            workspace_ids=tuple(ids),
            uses=tuple(SCAN_REQUESTS),
        )

    def _event_days(self, target: Target) -> List[Unit]:
        today = self._clock().date()
        first = today - timedelta(days=self.options.days - 1)
        # the oldest first: those are the ones about to leave the 28 days the API keeps
        return [
            Unit(
                key=f"{target.endpoint}@{day}",
                target=target.name,
                kind=EVENTS,
                endpoint=target.endpoint,
                scope=target.scope,
                ttl=target.ttl,
                day=day,
                uses=(target.endpoint,),
            )
            for day in (
                first + timedelta(days=n) for n in range((today - first).days + 1)
            )
        ]

    def roots(self) -> List[Unit]:
        """The units that need no other result."""
        units: List[Unit] = []
        for target in self.selection.targets:
            if target.mode is Mode.SNAPSHOT:
                units.append(self._snapshot(target, target.params))
            elif target.mode is Mode.EVENTS:
                units.extend(self._event_days(target))
            elif target.mode is Mode.SCAN:
                units.append(self._modified(target))
        return units

    def expand(self, unit: Unit, rows: Sequence[Any]) -> List[Unit]:
        """The units that follow from the rows of a unit.

        :param unit: a unit that has been done (or is fresh)
        :param rows: its rows: the rows of the response, or the workspace ids of a
            ``modified`` unit
        """
        if unit.kind == MODIFIED:
            target = get_target(unit.target)
            ids = sorted({str(row) for row in rows})
            since = unit.params.get("modifiedSince")
            return [
                self._scan_batch(target, batch, since)
                for batch in chunked(ids, MAX_WORKSPACES)
            ]

        children: List[Unit] = []
        for child in self._children(unit.target):
            endpoint = get_endpoint(child.endpoint)
            seen = set()
            for row in rows:
                if not isinstance(row, dict):
                    continue
                params: Dict[str, Any] = {}
                for placeholder, (source, name) in child.bind.items():
                    value = row.get(name) if source == "row" else unit.params.get(name)
                    if value in (None, ""):
                        break
                    params[placeholder] = value
                else:
                    made = self._snapshot(child, {**child.params, **params})
                    if made.key not in seen:
                        seen.add(made.key)
                        children.append(made)
        return children

    # -- what the lake holds ----------------------------------------------------------

    def _limit(self, unit: Unit) -> timedelta:
        if self.options.max_age is not None:
            return self.options.max_age
        if unit.ttl is not None:
            return unit.ttl
        return get_endpoint(unit.endpoint).ttl

    def is_fresh(self, unit: Unit) -> bool:
        """Whether the lake answers a unit already, so that it needs no request."""
        now = self._clock()
        if unit.kind == EVENTS:
            assert unit.day is not None
            stored = self._store.event_day(self._tenant, unit.endpoint, unit.day)
            if stored is None:
                return False
            if stored.sealed:
                return True  # complete days cannot change, not even with --force
            written = stored.updated_at
            return bool(
                not self.options.force
                and not stored.cursor  # a traversal that stopped half way is continued
                and written is not None
                and now - written < self._limit(unit)
            )
        if self.options.force or unit.kind == MODIFIED:
            return False
        if unit.kind == SCAN:
            found = latest_scan(
                self._store, self._tenant, unit.workspace_ids, self.options.scan_flags
            )
            return found is not None and found.age(now) < self._limit(unit)
        endpoint = get_endpoint(unit.endpoint)
        snapshot = self._store.latest(
            self._tenant, endpoint.id, endpoint.canonical_params(unit.params)
        )
        return snapshot is not None and snapshot.age(now) < self._limit(unit)

    def requests(self, unit: Unit) -> Dict[str, int]:
        """The requests a unit is expected to need, by operation.

        It is a guess from what the lake holds: how many pages the answer had last time.
        """
        if unit.kind == SCAN:
            return dict(SCAN_REQUESTS)
        if unit.kind == MODIFIED:
            return {unit.endpoint: 1}
        if unit.kind == EVENTS:
            assert unit.day is not None
            stored = self._store.event_day(self._tenant, unit.endpoint, unit.day)
            return {
                unit.endpoint: max(
                    1, ceil((stored.rows if stored else 0) / EVENTS_PER_REQUEST)
                )
            }
        endpoint = get_endpoint(unit.endpoint)
        snapshot = self._store.latest(
            self._tenant, endpoint.id, endpoint.canonical_params(unit.params)
        )
        return {
            endpoint.id: (
                max(1, int(snapshot.manifest.get("pages", 1))) if snapshot else 1
            )
        }

    def stored_rows(self, unit: Unit) -> Optional[List[Any]]:
        """The rows of the newest stored answer of a unit, whatever its age; ``None`` if none."""
        endpoint = get_endpoint(unit.endpoint)
        snapshot = self._store.latest(
            self._tenant, endpoint.id, endpoint.canonical_params(unit.params)
        )
        return rows_of(endpoint, snapshot.load()) if snapshot else None

    # -- a dry run --------------------------------------------------------------------

    def _count(
        self, plans: Dict[str, TargetPlan], needed: Dict[str, int], unit: Unit
    ) -> None:
        plan = plans[unit.target]
        assert plan.units is not None and plan.requests is not None
        plan.units += 1
        if self.is_fresh(unit):
            plan.fresh += 1
            return
        for endpoint, n in self.requests(unit).items():
            plan.requests += n
            needed[endpoint] = needed.get(endpoint, 0) + n

    def _mark_unknown(
        self, plans: Dict[str, TargetPlan], name: str, because: str
    ) -> None:
        """A target whose units depend on rows that are not in the lake: nothing is known."""
        plan = plans[name]
        plan.units = plan.requests = None
        plan.add_note(f"depends on {because}, which has nothing in the lake yet")
        for child in self._children(name):
            self._mark_unknown(plans, child.name, name)

    def dry_run(self) -> Tuple[List[TargetPlan], Dict[str, int]]:
        """Work out the units from what the lake holds, without calling the API.

        The rows that later stages work on are taken from the newest stored answer,
        however old.

        :return: one `TargetPlan` per target, and the requests expected from each operation
        """
        plans = {
            target.name: TargetPlan(
                target, implied=target.name in self.selection.implied
            )
            for target in self.selection.targets
        }
        needed: Dict[str, int] = {}

        frontier = self.roots()
        while frontier:
            for unit in frontier:
                self._count(plans, needed, unit)
            following: List[Unit] = []
            missing: Dict[str, Tuple[str, int]] = {}
            for unit in frontier:
                if unit.kind == MODIFIED:
                    self._plan_scan(unit, plans, needed)
                elif unit.has_children:
                    rows = self.stored_rows(unit)
                    if rows is None:
                        for child in self._children(unit.target):
                            _, n = missing.get(child.name, (unit.target, 0))
                            missing[child.name] = (unit.target, n + 1)
                        continue
                    made = self.expand(unit, rows)
                    following.extend(made)
                    if not self.is_fresh(unit):
                        for child in self._children(unit.target):
                            plans[child.name].add_note(
                                f"worked out from the {unit.target} stored earlier: "
                                "there may be more or fewer once they are fetched again"
                            )
            for name, (parent, n) in missing.items():
                plan = plans[name]
                if plan.units is None:
                    continue
                if not plan.units and not any(u.target == name for u in following):
                    self._mark_unknown(plans, name, parent)
                else:
                    plan.add_note(
                        f"{n} of the {parent} units have nothing in the lake yet, "
                        "so there will be more than this"
                    )
            frontier = following
        return list(plans.values()), needed

    def _plan_scan(
        self, unit: Unit, plans: Dict[str, TargetPlan], needed: Dict[str, int]
    ) -> None:
        """The batches of a scan are made from the workspaces, which a dry run does not know.

        An incremental scan depends on what changed, which only the API can tell. A full
        scan is estimated from the list of workspaces in the lake.
        """
        plan = plans[unit.target]
        since = unit.params.get("modifiedSince")
        if since:
            plan.units = plan.requests = None
            plan.add_note(
                f"incremental: only the workspaces that changed since {since} are "
                "scanned, and which they are is not known until they are listed"
            )
            return
        rows = self.stored_rows(self._snapshot(get_target("groups"), {}))
        if rows is None:
            plan.units = plan.requests = None
            plan.add_note(
                "every workspace is scanned, but how many there are is not known: "
                "the list of workspaces (groups) is not in the lake yet"
            )
            return
        ids = sorted(
            {str(row["id"]) for row in rows if isinstance(row, dict) and row.get("id")}
        )
        target = get_target(unit.target)
        for batch in chunked(ids, MAX_WORKSPACES):
            self._count(plans, needed, self._scan_batch(target, batch))
        plan.add_note(
            f"every workspace is scanned: {len(ids)} are in the stored list of workspaces, "
            f"in batches of up to {MAX_WORKSPACES} (the list may be out of date)"
        )
