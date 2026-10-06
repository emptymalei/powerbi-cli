"""A read model over the data lake, for browsing it.

The lake keeps raw responses (`pbi_cli.core.store`). This module reads them the way a person
looks things up: the workspaces of a tenant, what is in each of them, who can open it, what
it is built from and what is built on it, and how fresh each part is. The TUI is made of
it, and it can be used from a script as well:

```python
catalog = Catalog(LakeStore("~/pbi/lake"), tenant="72f988bf-...")
for workspace in catalog.workspaces():
    print(workspace.name, catalog.counts(workspace.id))
lineage = catalog.lineage(catalog.item("report", "rep-0001"))
```

It never calls the API and it needs no token, so it works offline. What it shows is what the
lake holds: the lists of the tenant (``groups``, ``reports``, ... of `pbi sync`) say what
exists, and the newest scan of a workspace says what is inside, who may open it and how it
is connected (the scan has to be made with ``--lineage`` and ``--get-artifact-users`` for
those). Where something is missing, the answer says what to sync to get it instead of
leaving the reader to wonder.

A `Catalog` reads the lists when it is created (and again on `Catalog.reload`) and loads the
scan of a workspace the first time something about it is asked, so that opening a lake with
thousands of workspaces is quick. It can be shared by threads.
"""

import threading
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
    Union,
)

from loguru import logger

from pbi_cli.core.client import rows_of
from pbi_cli.core.details import Detail, collect
from pbi_cli.core.registry import IDENTITY_PARAM, get_endpoint
from pbi_cli.core.scan import RESULT_ENDPOINT, ScanFlags, split_scan_result
from pbi_cli.core.store import EventDay, LakeStore, Snapshot
from pbi_cli.core.sync.state import STATE_NAME
from pbi_cli.core.sync.targets import TARGETS

#: The things that live in a workspace, in the order they are listed.
KINDS: Tuple[str, ...] = ("report", "dataset", "dashboard", "dataflow", "app")

#: Words for the kinds, singular and plural.
LABELS: Dict[str, Tuple[str, str]] = {
    "workspace": ("Workspace", "Workspaces"),
    "report": ("Report", "Reports"),
    "dataset": ("Dataset", "Datasets"),
    "dashboard": ("Dashboard", "Dashboards"),
    "dataflow": ("Dataflow", "Dataflows"),
    "app": ("App", "Apps"),
    "capacity": ("Capacity", "Capacities"),
    "datasource": ("Data source", "Data sources"),
}

#: The operation whose lists say what exists.
LIST_ENDPOINTS: Dict[str, str] = {
    "workspace": "admin.groups",
    "report": "admin.reports",
    "dataset": "admin.datasets",
    "dashboard": "admin.dashboards",
    "dataflow": "admin.dataflows",
    "app": "admin.apps",
    "capacity": "admin.capacities",
}

#: Where the workspaces of the user are, when the lake has no list of the tenant.
USER_WORKSPACES = "user.groups"

#: What an account that is not an administrator can list of a workspace, one request for each
#: workspace: the lake keeps each of these lists under the workspace it was asked for.
USER_LISTS: Dict[str, str] = {
    "report": "user.group_reports",
    "dataset": "user.group_datasets",
    "dashboard": "user.group_dashboards",
    "dataflow": "user.group_dataflows",
}
USER_APPS = "user.apps"
USER_GROUP_USERS = "user.group_users"

ACTIVITY_ENDPOINT = "admin.activityevents"
REPORT_USERS_ENDPOINT = "admin.reports.users"

_ID_FIELD = {"dataflow": "objectId"}
_NAME_FIELD = {"dashboard": "displayName", "capacity": "displayName"}

#: Where a scan keeps the items of a workspace.
SCAN_KEYS = {
    "report": "reports",
    "dataset": "datasets",
    "dashboard": "dashboards",
    "dataflow": "dataflows",
}

#: A list fetched with one of these holds only some of the rows.
PARTIAL_PARAMS = frozenset({"$filter", "$top", "$skip"})

#: Workspace types that are somebody's personal workspace.
PERSONAL_TYPES = frozenset({"PersonalGroup", "Personal"})

#: Beyond this age, whatever the target's own time to live, data counts as old.
OLD_AFTER = timedelta(days=7)

#: How many loaded scans, and how many workspaces of them, are kept in memory.
SCAN_CACHE = 4
VIEW_CACHE = 64

#: How many steps of lineage are followed.
MAX_DEPTH = 6

#: Fields shown first, in this order, when a row is described.
_FIRST_FIELDS = ("name", "displayName", "id", "objectId", "type", "state")

#: Fields of the rows that hold when something was changed or created.
_UPDATED_FIELDS = (
    "modifiedDateTime",
    "lastUpdate",
    "modifiedDate",
    "createdDateTime",
    "createdDate",
)
_OWNER_FIELDS = ("configuredBy", "modifiedBy", "createdBy", "publishedBy", "owner")

_CONNECTION_KEYS = ("server", "database", "url", "path", "kind")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(stamp: Any) -> Optional[datetime]:
    if not stamp:
        return None
    try:
        value = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _dicts(value: Any) -> List[Dict[str, Any]]:
    """The dictionaries of a list; anything else in it, or a missing list, is left out."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def label(kind: str, plural: bool = False) -> str:
    """The word for a kind of thing, for example ``Dashboards``."""
    words = LABELS.get(kind)
    return words[1 if plural else 0] if words else kind.capitalize()


def id_of(kind: str, row: Mapping[str, Any]) -> str:
    """The id of a row of a list or of a scan."""
    return _text(row.get(_ID_FIELD.get(kind, "id")))


def name_of(kind: str, row: Mapping[str, Any]) -> str:
    """The name of a row of a list or of a scan, or its id when it has none."""
    return _text(row.get(_NAME_FIELD.get(kind, "name"))) or id_of(kind, row)


# -- freshness -------------------------------------------------------------------------


class Freshness(str, Enum):
    """How up to date something in the lake is."""

    #: younger than the time to live of its target
    FRESH = "fresh"
    #: older than that, but not by much (less than a week)
    AGING = "aging"
    #: a week old or more
    OLD = "old"
    #: not in the lake
    NONE = "none"


def judge(age: Optional[timedelta], ttl: timedelta) -> Freshness:
    """Say how fresh something of this age is, given how long its target stays fresh."""
    if age is None:
        return Freshness.NONE
    if age < ttl:
        return Freshness.FRESH
    return Freshness.AGING if age < OLD_AFTER else Freshness.OLD


_WORST = (Freshness.NONE, Freshness.OLD, Freshness.AGING, Freshness.FRESH)


def worst(levels: Iterable[Freshness]) -> Freshness:
    """The least fresh of several; ``NONE`` for no levels at all."""
    found = set(levels)
    return next((level for level in _WORST if level in found), Freshness.NONE)


# -- what the catalog hands out --------------------------------------------------------


@dataclass(frozen=True)
class Listing:
    """A stored list of the tenant (all workspaces, all reports, ...).

    :param endpoint: the operation it came from
    :param snapshot: the stored answer
    :param rows: its rows
    :param partial: whether it was fetched with a filter or a limit, so that it may hold
        only some of the rows
    """

    endpoint: str
    snapshot: Snapshot
    rows: List[Dict[str, Any]]
    partial: bool = False

    @property
    def fetched_at(self) -> datetime:
        return self.snapshot.fetched_at


@dataclass(eq=False)
class Workspace:
    """A workspace.

    :param id: its id
    :param name: its name
    :param type: ``Workspace``, ``PersonalGroup``, ...
    :param state: ``Active``, ``Deleted``, ...
    :param raw: the row of the list it came from
    :param visible_to: the accounts whose own list of workspaces holds it (by the name of
        their profile), as far as the lake knows
    """

    id: str
    name: str
    type: str
    state: str
    raw: Dict[str, Any]
    visible_to: List[str] = field(default_factory=list)

    kind = "workspace"

    @property
    def personal(self) -> bool:
        """Whether it is somebody's personal workspace."""
        return self.type in PERSONAL_TYPES

    @property
    def active(self) -> bool:
        return self.state in ("", "Active")

    @property
    def data(self) -> Dict[str, Any]:
        return self.raw


@dataclass(eq=False)
class Item:
    """A report, dataset, dashboard, dataflow or app.

    :param kind: one of `KINDS`
    :param id: its id
    :param name: its name
    :param workspace_id: the workspace it is in, when the lake says
    :param raw: the row of the list it came from (empty if only a scan knows the item)
    :param scan: the item as the newest scan of its workspace has it
    """

    kind: str
    id: str
    name: str
    workspace_id: Optional[str]
    raw: Dict[str, Any]
    scan: Optional[Dict[str, Any]] = None

    @property
    def data(self) -> Dict[str, Any]:
        """Everything known about the item: the row of the list, with the scan over it."""
        return {**self.raw, **(self.scan or {})}

    @property
    def owner(self) -> str:
        """Who configured, created or published it, when the lake says."""
        data = self.data
        return next((_text(data[f]) for f in _OWNER_FIELDS if data.get(f)), "")

    @property
    def updated(self) -> str:
        """When it was changed or created, as the API wrote it."""
        data = self.data
        return next((_text(data[f]) for f in _UPDATED_FIELDS if data.get(f)), "")

    @property
    def sources(self) -> str:
        """Where the lake knows it from: ``list``, ``scan`` or ``list + scan``."""
        parts = [
            name for name, held in (("list", self.raw), ("scan", self.scan)) if held
        ]
        return " + ".join(parts)


Subject = Union[Workspace, Item]


@dataclass(frozen=True)
class ScanRef:
    """The newest stored scan that has a workspace.

    :param snapshot: the stored result (of up to 100 workspaces)
    :param flags: what the scan included
    """

    snapshot: Snapshot
    flags: ScanFlags

    @property
    def fetched_at(self) -> datetime:
        return self.snapshot.fetched_at


class ScanView:
    """What a scan says about one workspace.

    :param ref: the scan
    :param workspace: the workspace as the scan has it: its items, users and so on
    :param instances: the data source instances the workspace uses
    :param as_of: how recent the scan is: when it was fetched, or when the complete scan it
        was part of began, whichever is later (an incremental scan does not fetch the
        workspaces that did not change)
    """

    def __init__(
        self,
        ref: ScanRef,
        workspace: Dict[str, Any],
        instances: List[Dict[str, Any]],
        as_of: datetime,
    ):
        self.ref = ref
        self.workspace = workspace
        self.instances = instances
        self.as_of = as_of
        self._index: Dict[Tuple[str, str], Dict[str, Any]] = {
            (kind, id_of(kind, row)): row
            for kind, key in SCAN_KEYS.items()
            for row in _dicts(workspace.get(key))
        }

    @property
    def flags(self) -> ScanFlags:
        return self.ref.flags

    def items(self, kind: str) -> List[Dict[str, Any]]:
        """The items of a kind in the workspace."""
        return _dicts(self.workspace.get(SCAN_KEYS[kind]))

    def find(self, kind: str, item_id: str) -> Optional[Dict[str, Any]]:
        """The item of a kind with this id, if the workspace has it."""
        return self._index.get((kind, item_id))

    def instance(self, instance_id: str) -> Optional[Dict[str, Any]]:
        """The data source instance with this id, if the workspace uses it."""
        return next(
            (i for i in self.instances if _text(i.get("datasourceId")) == instance_id),
            None,
        )


@dataclass(frozen=True)
class Access:
    """Somebody (or some group) with access to something.

    :param name: display name
    :param email: e-mail address or identifier
    :param role: what they may do (``Admin``, ``Member``, ``Read``, ...)
    :param type: ``User``, ``Group``, ``App``
    """

    name: str
    email: str
    role: str
    type: str


@dataclass
class UsersView:
    """Who has access to something, and where the lake knows that from.

    :param rows: the people and groups
    :param source: where they come from: an operation or ``scan``
    :param fetched_at: when that was stored
    :param missing: when there are none because the lake does not hold them, what to sync
    """

    rows: List[Access] = field(default_factory=list)
    source: str = ""
    fetched_at: Optional[datetime] = None
    missing: str = ""


@dataclass
class LineageNode:
    """One step of the lineage of an item.

    :param kind: ``report``, ``dataset``, ``dashboard``, ``dataflow`` or ``datasource``
    :param id: its id
    :param name: its name (its id when the lake does not know it)
    :param workspace: the name of its workspace, when it is another one than the item's
    :param note: what else the reader should know, for example that it is not in the lake
    :param children: the next steps
    """

    kind: str
    id: str
    name: str
    workspace: str = ""
    note: str = ""
    children: List["LineageNode"] = field(default_factory=list)


@dataclass
class Lineage:
    """What an item is built from and what is built on it.

    :param upstream: what it is built from: a report's dataset, a dataset's dataflows and
        data sources, and so on, each with what that is built from in turn
    :param downstream: what is built on it, for example the reports of a dataset
    :param notes: what limits the answer, for example that the workspace was never scanned
    """

    upstream: List[LineageNode] = field(default_factory=list)
    downstream: List[LineageNode] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class Version:
    """One stored answer that holds something.

    :param endpoint: the operation it came from
    :param version: the version folder name
    :param fetched_at: when it was received
    :param rows: how many rows it has, when it is a list
    :param size: its size in bytes
    :param profile: the profile that fetched it
    :param params: the parameters of the request
    :param snapshot: the stored answer itself
    """

    endpoint: str
    version: str
    fetched_at: datetime
    rows: Optional[int]
    size: int
    profile: Optional[str]
    params: Dict[str, str]
    snapshot: Snapshot


@dataclass(frozen=True)
class Match:
    """A workspace or item found by name.

    :param kind: what it is
    :param id: its id
    :param name: its name
    :param workspace_id: the workspace it is in (for an item)
    :param workspace: that workspace's name
    """

    kind: str
    id: str
    name: str
    workspace_id: Optional[str] = None
    workspace: str = ""


@dataclass(frozen=True)
class Holding:
    """What the lake holds for one operation.

    :param endpoint: the operation
    :param count: how many requests (or, for events, days) are stored
    :param unit: ``request`` or ``day``
    :param newest: when the newest of them was stored
    """

    endpoint: str
    count: int
    unit: str
    newest: Optional[datetime]


def holding(store: LakeStore, tenant: str, endpoint: str) -> Optional[Holding]:
    """How much of an operation the lake holds and how new it is; ``None`` if nothing."""
    days = store.event_days(tenant, endpoint)
    if days:
        written = [d.updated_at for d in days if d.updated_at]
        return Holding(endpoint, len(days), "day", max(written) if written else None)
    sets = store.parameter_sets(tenant, endpoint)
    if not sets:
        return None
    return Holding(
        endpoint, len(sets), "request", max(s.latest.fetched_at for s in sets)
    )


# -- the catalog -----------------------------------------------------------------------


@dataclass
class _Baseline:
    """The start of the last complete scan, from the state of the sync."""

    at: datetime
    flags: Dict[str, str]
    exclude_personal: bool
    exclude_inactive: bool


@dataclass
class _State:
    """Everything `Catalog.reload` reads, swapped in as a whole."""

    listings: Dict[str, Optional[Listing]]
    workspaces: Dict[str, Workspace]
    items: Dict[str, Dict[str, Item]]
    by_workspace: Dict[str, Dict[str, List[Item]]]
    reports_of_dataset: Dict[str, List[Item]]
    scans: Dict[str, ScanRef]
    baseline: Optional[_Baseline]
    user_lists: Dict[Tuple[str, str], Snapshot]
    user_apps: Optional[datetime]
    accounts_listing: FrozenSet[str] = frozenset()


class Catalog:
    """Reads the lake the way a person browses it.

    :param store: the lake
    :param tenant: the tenant to browse (as the lake has it, see `LakeStore.tenants`)
    :param clock: returns the current time (aware, UTC)
    """

    def __init__(
        self,
        store: LakeStore,
        tenant: str,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ):
        self._store = store
        self.tenant = tenant
        self._clock = clock
        self._lock = threading.Lock()
        self._pieces: "OrderedDict[str, Dict[str, Dict[str, Any]]]" = OrderedDict()
        self._views: "OrderedDict[str, ScanView]" = OrderedDict()
        self._s = self._read()

    # -- reading the lake --------------------------------------------------------------

    def reload(self) -> None:
        """Read the lake again, for example after a sync."""
        state = self._read()
        with self._lock:
            self._pieces.clear()
            self._views.clear()
            self._s = state

    def now(self) -> datetime:
        return self._clock()

    @property
    def location(self) -> Any:
        """The folder of the lake (a path on disk or in the cloud)."""
        return self._store.root

    def _listing(self, endpoint_id: str) -> Optional[Listing]:
        """The newest complete list of an operation, else the newest partial one."""
        sets = self._store.parameter_sets(self.tenant, endpoint_id)
        if not sets:
            return None
        complete = [s for s in sets if not PARTIAL_PARAMS & set(s.params)]
        chosen = max(complete or sets, key=lambda s: s.latest.fetched_at)
        try:
            data = chosen.latest.load()
        except (OSError, ValueError) as error:
            logger.warning(f"Ignoring unreadable {endpoint_id} in the lake: {error}")
            return None
        rows = _dicts(rows_of(get_endpoint(endpoint_id), data))
        return Listing(endpoint_id, chosen.latest, rows, partial=not complete)

    def _scan_refs(self) -> Dict[str, ScanRef]:
        refs: Dict[str, ScanRef] = {}
        for found in self._store.parameter_sets(self.tenant, RESULT_ENDPOINT):
            manifest = found.latest.manifest
            ref = ScanRef(
                found.latest, ScanFlags.from_query(manifest.get("flags") or {})
            )
            for workspace_id in manifest.get("workspace_ids") or []:
                current = refs.get(str(workspace_id))
                if current is None or ref.fetched_at > current.fetched_at:
                    refs[str(workspace_id)] = ref
        return refs

    def _baseline(self) -> Optional[_Baseline]:
        scan = (self._store.read_state(self.tenant, STATE_NAME) or {}).get("scan") or {}
        at = _parse(scan.get("last_success_at"))
        if at is None:
            return None
        coverage = scan.get("coverage") or {}
        return _Baseline(
            at=at,
            flags=dict(scan.get("flags") or {}),
            exclude_personal=bool(coverage.get("excludePersonalWorkspaces")),
            exclude_inactive=bool(coverage.get("excludeInActiveWorkspaces")),
        )

    def _read(self) -> _State:
        listings: Dict[str, Optional[Listing]] = {
            kind: self._listing(endpoint) for kind, endpoint in LIST_ENDPOINTS.items()
        }
        if listings["workspace"] is None:
            listings["workspace"] = self._listing(USER_WORKSPACES)

        workspaces: Dict[str, Workspace] = {}
        found = listings["workspace"]
        for row in found.rows if found else []:
            workspace_id = id_of("workspace", row)
            if workspace_id:
                workspaces[workspace_id] = Workspace(
                    id=workspace_id,
                    name=name_of("workspace", row),
                    type=_text(row.get("type")),
                    state=_text(row.get("state")),
                    raw=row,
                )

        # the workspaces that accounts see, each by its own list: the union of them all
        accounts_listing = set()
        for ps in self._store.parameter_sets(self.tenant, USER_WORKSPACES):
            who = _text(ps.latest.manifest.get("profile")) or _text(
                ps.params.get(IDENTITY_PARAM)
            )
            if who:
                accounts_listing.add(who)  # it has a list, even an empty one
            try:
                rows = _dicts(rows_of(get_endpoint(USER_WORKSPACES), ps.latest.load()))
            except (OSError, ValueError) as error:
                logger.warning(f"Ignoring unreadable user.groups in the lake: {error}")
                continue
            for row in rows:
                workspace_id = id_of("workspace", row)
                if not workspace_id:
                    continue
                if workspace_id not in workspaces:
                    workspaces[workspace_id] = Workspace(
                        id=workspace_id,
                        name=name_of("workspace", row),
                        type=_text(row.get("type")),
                        state=_text(row.get("state")),
                        raw=row,
                    )
                seen_by = workspaces[workspace_id].visible_to
                if who and who not in seen_by:
                    seen_by.append(who)

        items: Dict[str, Dict[str, Item]] = {kind: {} for kind in KINDS}
        by_workspace: Dict[str, Dict[str, List[Item]]] = {}
        reports_of_dataset: Dict[str, List[Item]] = {}

        def add(kind: str, row: Dict[str, Any], home: Optional[str]) -> None:
            item_id = id_of(kind, row)
            if not item_id or item_id in items[kind]:  # the first list to have it wins
                return
            item = Item(kind, item_id, name_of(kind, row), home, row)
            items[kind][item_id] = item
            if home:
                by_workspace.setdefault(home, {}).setdefault(kind, []).append(item)
            if kind == "report" and row.get("datasetId"):
                reports_of_dataset.setdefault(_text(row["datasetId"]), []).append(item)

        for kind in KINDS:
            listing = listings[kind]
            for row in listing.rows if listing else []:
                add(kind, row, _text(row.get("workspaceId")) or None)

        # what an account lists of a workspace: below the lists of the administrators
        user_lists: Dict[Tuple[str, str], Snapshot] = {}
        for kind, endpoint_id in USER_LISTS.items():
            for ps in self._store.parameter_sets(self.tenant, endpoint_id):
                group = _text(ps.params.get("groupId"))
                if not group:
                    continue
                try:
                    rows = _dicts(rows_of(get_endpoint(endpoint_id), ps.latest.load()))
                except (OSError, ValueError) as error:
                    logger.warning(f"Ignoring unreadable {endpoint_id}: {error}")
                    continue
                user_lists[(kind, group)] = ps.latest
                for row in rows:
                    add(kind, row, group)
        user_apps: Optional[datetime] = None
        for ps in self._store.parameter_sets(self.tenant, USER_APPS):
            try:
                rows = _dicts(rows_of(get_endpoint(USER_APPS), ps.latest.load()))
            except (OSError, ValueError) as error:
                logger.warning(f"Ignoring unreadable user.apps: {error}")
                continue
            fetched = ps.latest.fetched_at
            user_apps = fetched if user_apps is None else max(user_apps, fetched)
            for row in rows:
                add("app", row, _text(row.get("workspaceId")) or None)

        scans = self._scan_refs()
        for workspace_id, ref in scans.items():  # scans know workspaces no list has
            if workspace_id not in workspaces:
                known = (ref.snapshot.manifest.get("workspaces") or {}).get(
                    workspace_id
                )
                known = known if isinstance(known, dict) else {}
                workspaces[workspace_id] = Workspace(
                    id=workspace_id,
                    name=_text(known.get("name")) or workspace_id,
                    type=_text(known.get("type")),
                    state=_text(known.get("state")),
                    raw={},
                )
        return _State(
            listings=listings,
            workspaces=workspaces,
            items=items,
            by_workspace=by_workspace,
            reports_of_dataset=reports_of_dataset,
            scans=scans,
            baseline=self._baseline(),
            user_lists=user_lists,
            user_apps=user_apps,
            accounts_listing=frozenset(accounts_listing),
        )

    # -- the lists of the tenant -------------------------------------------------------

    def listing(self, kind: str) -> Optional[Listing]:
        """The stored list a kind of thing is made from: ``workspace``, ``report``, ..."""
        return self._s.listings.get(kind)

    def age(self, when: Optional[datetime]) -> Optional[timedelta]:
        """How long ago something was stored."""
        return None if when is None else max(timedelta(0), self.now() - when)

    def listing_freshness(self, kind: str) -> Freshness:
        """How fresh the list of a kind of thing is."""
        found = self.listing(kind)
        if found is None:
            return Freshness.NONE
        return judge(self.age(found.fetched_at), get_endpoint(found.endpoint).ttl)

    def workspaces(self, personal: Optional[bool] = None) -> List[Workspace]:
        """The workspaces, by name.

        :param personal: ``True`` for the personal ones only, ``False`` to leave them out
        """
        found = [
            w
            for w in self._s.workspaces.values()
            if personal is None or w.personal == personal
        ]
        return sorted(found, key=lambda w: (w.name.casefold(), w.id))

    def workspace(self, workspace_id: str) -> Optional[Workspace]:
        return self._s.workspaces.get(workspace_id)

    def capacities(self) -> List[Dict[str, Any]]:
        """The capacities of the tenant, by name."""
        found = self.listing("capacity")
        rows = found.rows if found else []
        return sorted(rows, key=lambda r: name_of("capacity", r).casefold())

    def all_items(self, kind: str) -> List[Item]:
        """Every item of a kind that the lists have, by name."""
        return sorted(
            self._s.items.get(kind, {}).values(),
            key=lambda i: (i.name.casefold(), i.id),
        )

    def has_apps(self) -> bool:
        """Whether the lake holds a list of apps: the tenant's, or one that an account made."""
        return self.listing("app") is not None or self._s.user_apps is not None

    def apps_freshness(self) -> Freshness:
        """How fresh the apps are: the tenant's list, else the newest list of an account."""
        if self.listing("app") is not None:
            return self.listing_freshness("app")
        if self._s.user_apps is None:
            return Freshness.NONE
        return judge(self.age(self._s.user_apps), get_endpoint(USER_APPS).ttl)

    def listed(self, kind: str, workspace_id: str) -> bool:
        """Whether the lake holds a list that would show the items of a kind in a workspace:
        the list of the whole tenant, or the list an account made for that workspace."""
        if self._s.listings.get(kind) is not None:
            return True
        if kind == "app":
            return self._s.user_apps is not None
        return (kind, workspace_id) in self._s.user_lists

    def accounts_with_a_list(self) -> FrozenSet[str]:
        """The accounts (by the name of their profile) whose own list of workspaces the lake
        holds, even an empty one: for the others, ``Workspace.visible_to`` knows nothing.
        """
        return self._s.accounts_listing

    def user_list(self, kind: str, workspace_id: str) -> Optional[Snapshot]:
        """The list that an account made of one kind of item in a workspace, if there is
        one (what is shown of an item that only such a list has)."""
        return self._s.user_lists.get((kind, workspace_id))

    def counts(self, workspace_id: str) -> Dict[str, int]:
        """How many items of each kind the lists have for a workspace."""
        return {
            kind: len(found)
            for kind, found in self._s.by_workspace.get(workspace_id, {}).items()
        }

    # -- scans -------------------------------------------------------------------------

    def _loaded(self, ref: ScanRef) -> Dict[str, Dict[str, Any]]:
        """The pieces (one per workspace) of a stored scan, kept for the next question."""
        key = str(ref.snapshot.directory)
        with self._lock:
            if key in self._pieces:
                self._pieces.move_to_end(key)
                return self._pieces[key]
        try:
            pieces = split_scan_result(ref.snapshot.load())
        except (OSError, ValueError) as error:
            logger.warning(f"Ignoring unreadable scan {key}: {error}")
            pieces = {}
        with self._lock:
            self._pieces[key] = pieces
            while len(self._pieces) > SCAN_CACHE:
                self._pieces.popitem(last=False)
        return pieces

    def scan_as_of(self, workspace_id: str) -> Optional[datetime]:
        """How recent what the lake knows from scans about a workspace is.

        That is when its newest scan was fetched, or when the complete scan began that
        found it unchanged, whichever is later.
        """
        ref = self._s.scans.get(workspace_id)
        if ref is None:
            return None
        at = ref.fetched_at
        base = self._s.baseline
        workspace = self._s.workspaces.get(workspace_id)
        if (
            base is not None
            and base.flags == ref.flags.canonical()
            and not base.exclude_inactive
            and not (base.exclude_personal and workspace and workspace.personal)
        ):
            at = max(at, base.at)
        return at

    def scan_of(self, workspace_id: str) -> Optional[ScanView]:
        """What the newest scan of a workspace says, if there is one."""
        with self._lock:
            if workspace_id in self._views:
                self._views.move_to_end(workspace_id)
                return self._views[workspace_id]
        ref = self._s.scans.get(workspace_id)
        as_of = self.scan_as_of(workspace_id)
        if ref is None or as_of is None:
            return None
        piece = self._loaded(ref).get(workspace_id)
        if piece is None:
            return None
        workspaces = _dicts(piece.get("workspaces"))
        view = ScanView(
            ref,
            workspaces[0] if workspaces else {},
            _dicts(piece.get("datasourceInstances")),
            as_of,
        )
        with self._lock:
            self._views[workspace_id] = view
            while len(self._views) > VIEW_CACHE:
                self._views.popitem(last=False)
        return view

    def workspace_freshness(self, workspace_id: str) -> Freshness:
        """How fresh what the lake knows about a workspace is: the least fresh of its lists
        and its scan (when it has one)."""
        levels = [self.listing_freshness("workspace")]
        scanned = self.scan_as_of(workspace_id)
        if scanned is not None:
            ttl = next(t.ttl for t in TARGETS if t.name == "scan")
            levels.append(judge(self.age(scanned), ttl or timedelta(hours=24)))
        return worst(levels)

    # -- items -------------------------------------------------------------------------

    def items(self, workspace_id: str) -> List[Item]:
        """Everything in a workspace: what the lists have, with the scan over it, and what
        only the scan knows. Ordered by kind, then name."""
        view = self.scan_of(workspace_id)
        found: List[Item] = []
        seen: Set[Tuple[str, str]] = set()
        for kind in KINDS:
            for item in self._s.by_workspace.get(workspace_id, {}).get(kind, []):
                scanned = (
                    view.find(kind, item.id) if view and kind in SCAN_KEYS else None
                )
                found.append(replace(item, scan=scanned))
                seen.add((kind, item.id))
        if view:
            for kind in SCAN_KEYS:
                for row in view.items(kind):
                    item_id = id_of(kind, row)
                    if item_id and (kind, item_id) not in seen:
                        found.append(
                            Item(
                                kind, item_id, name_of(kind, row), workspace_id, {}, row
                            )
                        )
        order = {kind: n for n, kind in enumerate(KINDS)}
        return sorted(found, key=lambda i: (order[i.kind], i.name.casefold(), i.id))

    def item(
        self, kind: str, item_id: str, workspace_id: Optional[str] = None
    ) -> Optional[Item]:
        """One item, with its scan, if the lake knows it.

        :param workspace_id: where to look when the lists do not have the item
        """
        item = self._s.items.get(kind, {}).get(item_id)
        workspace_id = item.workspace_id if item else workspace_id
        view = self.scan_of(workspace_id) if workspace_id else None
        scanned = view.find(kind, item_id) if view and kind in SCAN_KEYS else None
        if item is not None:
            return replace(item, scan=scanned) if scanned else item
        if scanned is None:
            return None
        return Item(kind, item_id, name_of(kind, scanned), workspace_id, {}, scanned)

    def search(self, text: str, limit: int = 50) -> List[Match]:
        """Workspaces and items whose name contains ``text`` (or whose id it is)."""
        needle = text.strip().casefold()
        if len(needle) < 2:
            return []
        names = {w.id: w.name for w in self._s.workspaces.values()}
        found: List[Match] = []
        for workspace in self._s.workspaces.values():
            if needle in workspace.name.casefold() or needle == workspace.id.casefold():
                found.append(Match("workspace", workspace.id, workspace.name))
        for kind in KINDS:
            for item in self._s.items.get(kind, {}).values():
                if needle in item.name.casefold() or needle == item.id.casefold():
                    found.append(
                        Match(
                            kind,
                            item.id,
                            item.name,
                            item.workspace_id,
                            names.get(item.workspace_id or "", ""),
                        )
                    )
        found.sort(key=lambda m: (m.name.casefold(), m.kind, m.id))
        return found[:limit]

    # -- users -------------------------------------------------------------------------

    @staticmethod
    def _access(rows: Any) -> List[Access]:
        found = []
        for row in _dicts(rows):
            role = next(
                (_text(v) for k, v in row.items() if k.endswith("AccessRight") and v),
                "",
            )
            found.append(
                Access(
                    name=_text(row.get("displayName") or row.get("graphId")),
                    email=_text(row.get("emailAddress") or row.get("identifier")),
                    role=role,
                    type=_text(row.get("principalType") or row.get("userType")),
                )
            )
        return sorted(found, key=lambda a: (a.name.casefold(), a.email.casefold()))

    def details(self, subject: Subject) -> List[Detail]:
        """The details of a workspace or an item (who has access to it, its data sources,
        the pages of a report, ...), each with what the lake holds of it, and the targets
        that can fetch it. See `pbi_cli.core.details`."""
        if isinstance(subject, Workspace):
            kind, workspace_id = "workspace", subject.id
        else:
            kind, workspace_id = subject.kind, subject.workspace_id
        state = self._store.read_state(self.tenant, STATE_NAME) or {}
        return collect(
            self._store,
            self.tenant,
            kind,
            subject.id,
            workspace_id,
            state.get("units") or {},
        )

    def users(self, subject: Subject) -> UsersView:
        """Who has access to a workspace or an item, as far as the lake knows."""
        if isinstance(subject, Workspace) and subject.raw.get("users"):
            found = self.listing("workspace")
            return UsersView(
                self._access(subject.raw["users"]),
                "admin.groups",
                found.fetched_at if found else None,
            )
        detail = next((d for d in self.details(subject) if d.name == "users"), None)
        if detail is not None and detail.held is not None:
            held = detail.held
            rows = rows_of(held.provider.endpoint, held.snapshot.load())
            return UsersView(
                self._access(rows), held.provider.endpoint.id, held.fetched_at
            )
        if isinstance(subject, Workspace):
            view = self.scan_of(subject.id)
            if view is not None and view.workspace.get("users"):
                return UsersView(
                    self._access(view.workspace["users"]), "scan", view.as_of
                )
        else:
            scanned = subject.scan or {}
            if scanned.get("users"):
                view = self.scan_of(subject.workspace_id or "")
                return UsersView(
                    self._access(scanned["users"]),
                    "scan",
                    view.as_of if view else None,
                )
        targets = " or ".join(
            f"`pbi sync run {p.target.name}`"
            for p in (detail.providers if detail is not None else ())
        )
        if isinstance(subject, Workspace):
            ways = (
                "a scan (`pbi sync run scan`), or list the workspaces with `-e users`"
            )
            ways = f"{targets}, {ways}" if targets else ways
            return UsersView(
                missing=f"The lake does not hold the users of this workspace. Fetch them with {ways}."
            )
        ways = "a scan with `--get-artifact-users`"
        ways = f"{targets}, or {ways}" if targets else ways
        return UsersView(
            missing=f"The lake does not hold the users of this item. Fetch them with {ways}."
        )

    # -- lineage -----------------------------------------------------------------------

    def _workspace_name(self, workspace_id: Optional[str], home: Optional[str]) -> str:
        """The name of a workspace, unless it is the one the item being looked at is in."""
        if not workspace_id or workspace_id == home:
            return ""
        workspace = self._s.workspaces.get(workspace_id)
        return workspace.name if workspace else workspace_id

    def _node(
        self,
        kind: str,
        item_id: str,
        workspace_id: Optional[str],
        home: Optional[str],
    ) -> LineageNode:
        found = self.item(kind, item_id, workspace_id)
        if found is None:
            return LineageNode(
                kind,
                item_id,
                item_id,
                self._workspace_name(workspace_id, home),
                note="not in the lake",
            )
        return LineageNode(
            kind,
            item_id,
            found.name,
            self._workspace_name(found.workspace_id, home),
        )

    @staticmethod
    def _datasource_name(instance: Mapping[str, Any]) -> str:
        details = instance.get("connectionDetails")
        details = details if isinstance(details, dict) else {}
        shown = [str(details[k]) for k in _CONNECTION_KEYS if details.get(k)]
        if not shown:
            shown = [
                str(v) for v in details.values() if isinstance(v, (str, int)) and v
            ]
        kind = _text(instance.get("datasourceType"))
        return f"{kind}: {', '.join(shown)}" if shown else kind or "data source"

    def _datasource_nodes(
        self, data: Mapping[str, Any], view: Optional[ScanView]
    ) -> List[LineageNode]:
        nodes = []
        for usage in _dicts(data.get("datasourceUsages")):
            instance_id = _text(usage.get("datasourceInstanceId"))
            if not instance_id:
                continue
            instance = view.instance(instance_id) if view else None
            nodes.append(
                LineageNode(
                    "datasource",
                    instance_id,
                    self._datasource_name(instance) if instance else instance_id,
                    note="" if instance else "details not in the scan",
                )
            )
        return nodes

    def _upstream(
        self,
        kind: str,
        item_id: str,
        workspace_id: Optional[str],
        home: Optional[str],
        path: Tuple[Tuple[str, str], ...],
        depth: int,
    ) -> List[LineageNode]:
        """What an item is built from, each with what that is built from."""
        item = self.item(kind, item_id, workspace_id)
        if item is None:
            return []
        data = item.data
        view = self.scan_of(item.workspace_id) if item.workspace_id else None
        refs: List[Tuple[str, str, Optional[str]]] = []  # (kind, id, workspace)
        if kind == "report" and data.get("datasetId"):
            refs.append(("dataset", _text(data["datasetId"]), None))
        if kind == "dashboard":
            for tile in _dicts(data.get("tiles")):
                if tile.get("reportId"):
                    refs.append(("report", _text(tile["reportId"]), item.workspace_id))
                if tile.get("datasetId"):
                    refs.append(("dataset", _text(tile["datasetId"]), None))
        if kind in ("dataset", "dataflow"):
            for up in _dicts(data.get("upstreamDataflows")):
                if up.get("targetDataflowId"):
                    refs.append(
                        ("dataflow", _text(up["targetDataflowId"]), up.get("groupId"))
                    )
            for up in _dicts(data.get("upstreamDatasets")):
                if up.get("targetDatasetId"):
                    refs.append(
                        ("dataset", _text(up["targetDatasetId"]), up.get("groupId"))
                    )

        nodes: List[LineageNode] = []
        seen: Set[Tuple[str, str]] = set()
        for ref_kind, ref_id, ref_workspace in refs:
            if (ref_kind, ref_id) in seen:
                continue
            seen.add((ref_kind, ref_id))
            node = self._node(ref_kind, ref_id, ref_workspace, home)
            if (ref_kind, ref_id) in path:
                node.note = node.note or "shown above"
            elif depth < MAX_DEPTH:
                node.children = self._upstream(
                    ref_kind,
                    ref_id,
                    ref_workspace,
                    home,
                    path + ((ref_kind, ref_id),),
                    depth + 1,
                )
            nodes.append(node)
        nodes.extend(self._datasource_nodes(data, view))
        return nodes

    def _downstream(
        self,
        item: Item,
        home: Optional[str],
        path: Tuple[Tuple[str, str], ...],
        depth: int,
    ) -> List[LineageNode]:
        """What is built on an item, each with what is built on that.

        Reports come from the lists of the whole tenant; the rest only from the scan of the
        item's own workspace.
        """
        found: List[Item] = []
        view = self.scan_of(item.workspace_id) if item.workspace_id else None
        seen: Set[Tuple[str, str]] = set()

        def add(candidate: Optional[Item]) -> None:
            if candidate and (candidate.kind, candidate.id) not in seen:
                seen.add((candidate.kind, candidate.id))
                found.append(candidate)

        if item.kind == "dataset":
            for report in self._s.reports_of_dataset.get(item.id, []):
                add(self.item("report", report.id))
        if view is not None:
            for kind in SCAN_KEYS:
                for row in view.items(kind):
                    row_id = id_of(kind, row)
                    if self._uses(kind, row, item):
                        add(self.item(kind, row_id, item.workspace_id))

        nodes = []
        for user in sorted(found, key=lambda i: (i.kind, i.name.casefold())):
            node = self._node(user.kind, user.id, user.workspace_id, home)
            if (user.kind, user.id) in path or (user.kind, user.id) == (
                item.kind,
                item.id,
            ):
                node.note = "shown above"
            elif depth < MAX_DEPTH:
                node.children = self._downstream(
                    user, home, path + ((user.kind, user.id),), depth + 1
                )
            nodes.append(node)
        return nodes

    @staticmethod
    def _uses(kind: str, row: Mapping[str, Any], item: Item) -> bool:
        """Whether an item of a scan is built on ``item``."""
        if item.kind == "dataset":
            return (
                (kind == "report" and _text(row.get("datasetId")) == item.id)
                or (
                    kind == "dataset"
                    and any(
                        _text(up.get("targetDatasetId")) == item.id
                        for up in _dicts(row.get("upstreamDatasets"))
                    )
                )
                or (
                    kind == "dashboard"
                    and any(
                        _text(t.get("datasetId")) == item.id
                        for t in _dicts(row.get("tiles"))
                    )
                )
            )
        if item.kind == "report":
            return kind == "dashboard" and any(
                _text(t.get("reportId")) == item.id for t in _dicts(row.get("tiles"))
            )
        if item.kind == "dataflow":
            return kind in ("dataset", "dataflow") and any(
                _text(up.get("targetDataflowId")) == item.id
                for up in _dicts(row.get("upstreamDataflows"))
            )
        return False

    def lineage(self, item: Item) -> Lineage:
        """What an item is built from, and what is built on it.

        Upstream is followed across workspaces (the references name the workspace of what
        they point to). Downstream is the reports of a dataset, from the lists of the whole
        tenant, and everything else found in the scan of the item's own workspace: another
        workspace may build on the item without the lake knowing.
        """
        home = item.workspace_id
        start = ((item.kind, item.id),)
        lineage = Lineage(
            upstream=self._upstream(item.kind, item.id, home, home, start, 1),
            downstream=(
                self._downstream(item, home, start, 1) if item.kind != "app" else []
            ),
        )
        view = self.scan_of(home) if home else None
        if item.kind in ("app", "dashboard") and not item.scan:
            lineage.notes.append("Dashboards and apps are connected through a scan.")
        if view is None:
            lineage.notes.append(
                "No scan of this workspace is in the lake: only what the lists say is "
                "shown (the dataset of a report, the reports of a dataset). "
                "`pbi sync run scan --lineage` adds the rest."
            )
        elif not view.flags.lineage:
            lineage.notes.append(
                "The scan of this workspace was made without lineage: scan again with "
                "`--lineage` to see what datasets are built on."
            )
        if item.kind != "app":
            lineage.notes.append(
                "What is built on the item is searched in the scan of its own workspace "
                "(reports of a dataset in every workspace)."
            )
        return lineage

    # -- versions ----------------------------------------------------------------------

    def _versions_of(
        self, endpoint_id: str, keep: Callable[[Snapshot], bool]
    ) -> List[Version]:
        found = []
        for stored in self._store.parameter_sets(self.tenant, endpoint_id):
            if not keep(stored.latest):
                continue
            for snapshot in self._store.versions(
                self.tenant, endpoint_id, stored.params
            ):
                found.append(
                    Version(
                        endpoint=endpoint_id,
                        version=snapshot.version,
                        fetched_at=snapshot.fetched_at,
                        rows=snapshot.manifest.get("rows"),
                        size=int(snapshot.manifest.get("bytes") or 0),
                        profile=snapshot.manifest.get("profile"),
                        params=dict(snapshot.manifest.get("params") or {}),
                        snapshot=snapshot,
                    )
                )
        return found

    def versions(self, kind: str, workspace_id: Optional[str] = None) -> List[Version]:
        """The stored answers that hold a kind of thing, newest first.

        :param kind: ``workspace``, ``report``, ... (what the list is of)
        :param workspace_id: only the scans that include this workspace, besides the list
        """
        found = self._versions_of(
            LIST_ENDPOINTS.get(kind, USER_WORKSPACES), lambda snapshot: True
        )
        if workspace_id:
            found.extend(
                self._versions_of(
                    RESULT_ENDPOINT,
                    lambda snapshot: workspace_id
                    in (snapshot.manifest.get("workspace_ids") or []),
                )
            )
        return sorted(found, key=lambda v: v.fetched_at, reverse=True)

    # -- activity ----------------------------------------------------------------------

    def event_days(self) -> List[EventDay]:
        """The days of audit events in the lake, newest first."""
        return self._store.event_days(self.tenant, ACTIVITY_ENDPOINT)

    def events(self, day: date) -> List[Dict[str, Any]]:
        """The events of a day, newest first."""
        found = list(self._store.read_events(self.tenant, ACTIVITY_ENDPOINT, day))
        return sorted(found, key=lambda e: _text(e.get("CreationTime")), reverse=True)

    # -- what the lake holds -----------------------------------------------------------

    def holdings(self) -> List[Tuple[str, Holding]]:
        """For each target of a sync that has anything in the lake, what it holds."""
        found = []
        for target in TARGETS:
            held = holding(self._store, self.tenant, target.endpoint)
            if held:
                found.append((target.name, held))
        return found


def scalar_fields(data: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """The plain fields of a row (not the lists and objects in it), the usual ones first."""
    plain = {
        key: value
        for key, value in data.items()
        if not isinstance(value, (dict, list)) and value not in (None, "")
    }
    first = [key for key in _FIRST_FIELDS if key in plain]
    rest = sorted((key for key in plain if key not in first), key=str.casefold)
    return [(key, _text(plain[key])) for key in first + rest]
