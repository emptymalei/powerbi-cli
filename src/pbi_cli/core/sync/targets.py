"""What a sync can fetch: the targets, and how they depend on each other.

A *target* is one thing worth keeping: the list of workspaces, the audit events, the users
of every report. Each one is made of requests to a read-only operation of the registry
(`pbi_cli.core.registry`), and is one of four kinds:

- a **snapshot** target asks one operation once (``groups``, ``apps``, ...);
- a **fan-out** target asks an operation once for every row of another target, such as the
  users of every report (``report-users``, which needs ``reports``);
- an **events** target reads an append-only log one UTC day at a time (``activity``);
- the **scan** target scans the workspaces in batches of 100.

Targets that copy personal or confidential data are not part of a plain sync: they run
only when they are named, so that nobody collects e-mail addresses or queries by accident.
Naming ``default`` adds the plain ones, ``all`` adds everything.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from typing import AbstractSet, Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from pbi_cli.core.registry import Scope
from pbi_cli.errors import PBIError

#: Names that stand for several targets.
DEFAULT = "default"
ALL = "all"


class Mode(str, Enum):
    """How a target is fetched."""

    SNAPSHOT = "snapshot"
    FANOUT = "fanout"
    EVENTS = "events"
    SCAN = "scan"


@dataclass(frozen=True)
class Target:
    """One thing a sync keeps.

    :param name: what the user types
    :param title: what it is, in words
    :param mode: how it is fetched
    :param endpoint: registry id of the requests (for scans: where the results are stored)
    :param scope: the kind of token the requests need
    :param default: whether a sync without target names includes it
    :param default_user: whether it is part of the plain sync of someone who has only a user
        account (no administrator): what a user can see, workspace by workspace
    :param sensitive: why it is only fetched when named (empty for the plain targets)
    :param parent: the target whose rows it fans out over
    :param bind: how a fan-out fills the path of its requests: placeholder to
        ``("row", field)`` (a field of a row of the parent) or ``("param", name)`` (a
        parameter of the parent's request)
    :param params: fixed parameters of the requests
    :param ttl: how long a stored answer stays fresh (default: the endpoint's ``ttl``)
    """

    name: str
    title: str
    mode: Mode
    endpoint: str
    scope: Scope
    default: bool = False
    default_user: bool = False
    sensitive: str = ""
    parent: Optional[str] = None
    bind: Mapping[str, Tuple[str, str]] = field(default_factory=dict)
    params: Mapping[str, Any] = field(default_factory=dict)
    ttl: Optional[timedelta] = None


TARGETS: Tuple[Target, ...] = (
    Target("groups", "Workspaces", Mode.SNAPSHOT, "admin.groups", Scope.ADMIN, True),
    Target("apps", "Apps", Mode.SNAPSHOT, "admin.apps", Scope.ADMIN, True),
    Target(
        "capacities", "Capacities", Mode.SNAPSHOT, "admin.capacities", Scope.ADMIN, True
    ),
    Target("reports", "Reports", Mode.SNAPSHOT, "admin.reports", Scope.ADMIN, True),
    Target("datasets", "Datasets", Mode.SNAPSHOT, "admin.datasets", Scope.ADMIN, True),
    Target(
        "dashboards", "Dashboards", Mode.SNAPSHOT, "admin.dashboards", Scope.ADMIN, True
    ),
    Target(
        "dataflows", "Dataflows", Mode.SNAPSHOT, "admin.dataflows", Scope.ADMIN, True
    ),
    Target(
        "scan",
        "Metadata scan of every workspace",
        Mode.SCAN,
        "admin.scan.result",
        Scope.ADMIN,
        sensitive=(
            "the contents of every workspace; with the scan options also data source "
            "details, dataset schemas and queries (DAX, Power Query) and the users of "
            "every item"
        ),
        ttl=timedelta(hours=24),
    ),
    Target(
        "report-users",
        "The users of every report",
        Mode.FANOUT,
        "admin.reports.users",
        Scope.ADMIN,
        sensitive="the people who can open each report, with their e-mail addresses",
        parent="reports",
        bind={"reportId": ("row", "id")},
    ),
    Target(
        "datasources",
        "The data sources of every dataset",
        Mode.FANOUT,
        "admin.datasets.datasources",
        Scope.ADMIN,
        sensitive="connection details of the data sources: servers, databases, paths",
        parent="datasets",
        bind={"datasetId": ("row", "id")},
    ),
    Target(
        "activity",
        "Audit activity events, one log per UTC day",
        Mode.EVENTS,
        "admin.activityevents",
        Scope.ADMIN,
        sensitive=(
            "what each person did and when: e-mail addresses, IP addresses, devices"
        ),
        ttl=timedelta(hours=1),
    ),
    Target(
        "user-groups",
        "Workspaces of the user",
        Mode.SNAPSHOT,
        "user.groups",
        Scope.USER,
        default_user=True,
    ),
    Target(
        "user-apps",
        "Apps of the user",
        Mode.SNAPSHOT,
        "user.apps",
        Scope.USER,
        default_user=True,
    ),
    Target(
        "user-reports",
        "Reports of each workspace of the user",
        Mode.FANOUT,
        "user.group_reports",
        Scope.USER,
        default_user=True,
        parent="user-groups",
        bind={"groupId": ("row", "id")},
    ),
    Target(
        "user-datasets",
        "Datasets of each workspace of the user",
        Mode.FANOUT,
        "user.group_datasets",
        Scope.USER,
        default_user=True,
        parent="user-groups",
        bind={"groupId": ("row", "id")},
    ),
    Target(
        "user-dashboards",
        "Dashboards of each workspace of the user",
        Mode.FANOUT,
        "user.group_dashboards",
        Scope.USER,
        default_user=True,
        parent="user-groups",
        bind={"groupId": ("row", "id")},
    ),
    Target(
        "user-dataflows",
        "Dataflows of each workspace of the user",
        Mode.FANOUT,
        "user.group_dataflows",
        Scope.USER,
        default_user=True,
        parent="user-groups",
        bind={"groupId": ("row", "id")},
    ),
    Target(
        "user-group-users",
        "The users of each workspace of the user",
        Mode.FANOUT,
        "user.group_users",
        Scope.USER,
        sensitive="the people who have access to each workspace, with their e-mail addresses",
        parent="user-groups",
        bind={"groupId": ("row", "id")},
    ),
    Target(
        "user-pages",
        "Pages of each report of the user",
        Mode.FANOUT,
        "user.report_pages",
        Scope.USER,
        parent="user-reports",
        bind={"groupId": ("param", "groupId"), "reportId": ("row", "id")},
    ),
)

_BY_NAME: Dict[str, Target] = {target.name: target for target in TARGETS}


def get_target(name: str) -> Target:
    """The target with this name.

    :raises PBIError: if there is none
    """
    try:
        return _BY_NAME[name]
    except KeyError:
        raise PBIError(
            f"Unknown sync target '{name}'. Targets: "
            f"{', '.join(_BY_NAME)}; or '{DEFAULT}' and '{ALL}'."
        ) from None


def children_of(name: str) -> List[Target]:
    """The fan-out targets that work on the rows of a target."""
    return [target for target in TARGETS if target.parent == name]


@dataclass(frozen=True)
class Selection:
    """The targets of a sync.

    :param targets: what will be synced, parents before the targets that need them
    :param implied: names of the targets that were added only because another one
        needs their rows
    """

    targets: Tuple[Target, ...]
    implied: frozenset

    @property
    def names(self) -> List[str]:
        return [target.name for target in self.targets]


def _article(scope: Scope) -> str:
    return "an administrator" if scope is Scope.ADMIN else "a user"


def _require(target: Target, available: Optional[AbstractSet[Scope]]) -> None:
    """Refuse a target that needs an account that is not stored, and say what to do."""
    if available is None or target.scope in available:
        return
    have = ", ".join(sorted(f"{s.value}" for s in available))
    raise PBIError(
        f"'{target.name}' needs {_article(target.scope)} account, and none is stored. "
        f"Store one with `pbi auth -t <token> -g {target.scope.value}`."
        + (f" The accounts you have are: {have}." if have else "")
    )


def _plain(available: Optional[AbstractSet[Scope]]) -> List[str]:
    """The names of the plain targets: those of the administrator's account when there is
    one, else those of a user's."""
    if available is None or Scope.ADMIN in available:
        return [
            t.name
            for t in TARGETS
            if t.default and (available is None or t.scope in available)
        ]
    if Scope.USER in available:
        return [t.name for t in TARGETS if t.default_user and t.scope in available]
    return []


def select_targets(
    names: Sequence[str] = (), available: Optional[AbstractSet[Scope]] = None
) -> Selection:
    """Turn what the user named into the targets to sync.

    Without names the plain targets are synced. ``default`` stands for them, ``all`` for
    every target. A fan-out brings along the target it fans out over.

    :param names: what the user named
    :param available: the kinds of account that are stored (default: all of them). What the
        plain sync is depends on it: the administrator's lists when there is an
        administrator account, else what a user can see. ``all`` is every target the
        accounts can run, and naming one they cannot is an error.
    :raises PBIError: for a name that is not a target, or a target that needs an account
        that is not stored
    """
    wanted: Set[str] = set()
    for name in names or (DEFAULT,):
        if name == DEFAULT:
            wanted.update(_plain(available))
        elif name == ALL:
            wanted.update(
                t.name for t in TARGETS if available is None or t.scope in available
            )
        else:
            target = get_target(name)
            _require(target, available)
            wanted.add(target.name)

    implied: Set[str] = set()
    pending = list(wanted)
    while pending:
        parent = get_target(pending.pop()).parent
        if parent and parent not in wanted:
            wanted.add(parent)
            implied.add(parent)
            pending.append(parent)

    ordered = tuple(target for target in TARGETS if target.name in wanted)
    return Selection(targets=ordered, implied=frozenset(implied))
