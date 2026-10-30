"""Publish a lake: a complete, consistent copy that others can read.

The copy has the layout of any lake, so the TUI and ``pbi lake`` read it as it is, and it
carries a ``publish.json`` (see `pbi_cli.core.store.PublishInfo`) that says by whom and when,
and that protects it: nothing but a later publish by the same person writes to it. A
published lake is a snapshot; the people who read it need no account.

What is copied: the newest version of every request (every version with ``history``), every
day of events, the scans, and the part of the sync state that the Explorer's freshness dots
read. A category that holds personal data or queries can be left out. The marker is written
last (a first publish marks the place as unfinished before it copies anything), and a version
is never overwritten, so a publish that stops halfway is harmless: run it again.

```python
plan = plan_publish(source, destination, exclude=["activity"])
print(plan.files, plan.size)
result = publish(plan)
```
"""

import getpass
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from pbi_cli.core.fsutil import inside_place, same_place
from pbi_cli.core.store import MANIFEST, LakeStore, PublishInfo, cli_version, safe_name
from pbi_cli.core.sync.state import SCHEMA as STATE_SCHEMA
from pbi_cli.core.sync.state import STATE_NAME
from pbi_cli.core.sync.targets import get_target
from pbi_cli.errors import PBIError

#: How many finished runs of the sync a published lake keeps for ``pbi sync status``.
RUNS_KEPT = 5


@dataclass(frozen=True)
class Category:
    """A kind of data in a lake, for deciding what to publish.

    :param name: what ``--exclude`` takes
    :param title: what it is
    :param endpoints: registry ids of the operations whose answers it holds
    :param sensitive: what it holds that people may not want to share
    :param excludable: whether it can be left out (the lists cannot: a lake without them
        shows nothing)
    """

    name: str
    title: str
    endpoints: Tuple[str, ...]
    sensitive: str
    excludable: bool = True


def _sensitive(target: str) -> str:
    return get_target(target).sensitive


CATEGORIES: Tuple[Category, ...] = (
    Category(
        "lists",
        "Lists of workspaces, items and apps",
        (),
        "the names of workspaces and items, and the people who own or last changed them "
        "(e-mail addresses)",
        excludable=False,
    ),
    Category(
        "scans",
        "Metadata scans",
        ("admin.scan.start", "admin.scan.status", "admin.scan.result"),
        _sensitive("scan"),
    ),
    Category(
        "users",
        "Who has access",
        ("admin.reports.users", "admin.users.artifact_access"),
        _sensitive("report-users"),
    ),
    Category(
        "datasources",
        "Data sources",
        ("admin.datasets.datasources",),
        _sensitive("datasources"),
    ),
    Category(
        "activity", "Audit events", ("admin.activityevents",), _sensitive("activity")
    ),
)

#: The names ``--exclude`` takes.
EXCLUDABLE = tuple(c.name for c in CATEGORIES if c.excludable)

_BY_FOLDER: Dict[str, str] = {
    safe_name(endpoint): category.name
    for category in CATEGORIES
    for endpoint in category.endpoints
}


def category_of(endpoint_folder: str) -> str:
    """The category of an ``endpoint=`` folder value (everything else is a list)."""
    return _BY_FOLDER.get(endpoint_folder, "lists")


def publisher_name() -> str:
    """Who is publishing, for the marker: the user and the machine."""
    try:
        user = getpass.getuser()
    except Exception:  # no login name in some containers
        user = "someone"
    return f"{user}@{socket.gethostname() or 'unknown'}"


@dataclass(frozen=True)
class Leaf:
    """A folder that is copied whole: a version of a request, or a day of events.

    :param tenant: the tenant folder value it belongs to
    :param endpoint: its ``endpoint=`` folder value
    :param category: the name of its `Category`
    :param source: the folder in the lake that is published
    :param parts: its path below the root of the lake
    :param files: ``(name, size)`` of each file, the manifest last
    :param mutable: whether it can change (a day of events); a version cannot
    """

    tenant: str
    endpoint: str
    category: str
    source: Any
    parts: Tuple[str, ...]
    files: Tuple[Tuple[str, int], ...]
    mutable: bool

    @property
    def size(self) -> int:
        return sum(size for _, size in self.files)


@dataclass
class CategorySummary:
    """What a category holds in the lake, and whether it is left out.

    :param category: the category
    :param files: how many files of it the lake holds
    :param size: how many bytes
    :param excluded: whether it is left out of the publish
    """

    category: Category
    files: int = 0
    size: int = 0
    excluded: bool = False


@dataclass
class PublishPlan:
    """What a publish would copy, and where. Made without writing anything.

    :param source: the lake that is published
    :param destination: where it goes
    :param publisher: who publishes
    :param tenants: the tenant folder values that are published
    :param leaves: the folders that are copied
    :param summaries: what the lake holds, by category, with what is left out
    :param states: the trimmed sync state to write for each tenant
    :param excluded: the categories left out
    :param history: every version is copied, not only the newest
    :param prune: afterwards the older versions at the destination are deleted
    :param before: what the destination's marker said before, if it was published already
    """

    source: LakeStore
    destination: LakeStore
    publisher: str
    tenants: List[str]
    leaves: List[Leaf]
    summaries: List[CategorySummary]
    states: Dict[str, Dict[str, Any]]
    excluded: List[str]
    history: bool = False
    prune: bool = False
    before: Optional[PublishInfo] = None

    @property
    def files(self) -> int:
        """How many files the published lake holds."""
        return sum(len(leaf.files) for leaf in self.leaves)

    @property
    def size(self) -> int:
        """How many bytes they hold."""
        return sum(leaf.size for leaf in self.leaves)


@dataclass
class PublishResult:
    """What a publish did.

    :param copied: files written
    :param skipped: files that were there already (versions never change)
    :param size: bytes written
    :param pruned: older versions deleted at the destination
    :param info: what the marker now says
    """

    copied: int
    skipped: int
    size: int
    pruned: int
    info: PublishInfo = field(repr=False)


def _check_excluded(exclude: Iterable[str]) -> List[str]:
    wanted = []
    for name in exclude:
        name = name.strip().lower()
        if name not in EXCLUDABLE:
            raise PBIError(
                f"Cannot leave out '{name}'. Categories that can be left out: "
                f"{', '.join(EXCLUDABLE)}."
            )
        if name not in wanted:
            wanted.append(name)
    return wanted


def _check_places(source: LakeStore, destination: LakeStore) -> None:
    if same_place(source.root, destination.root):
        raise PBIError(
            f"The destination {destination.root} is the lake that is published. "
            "Publish to another place."
        )
    if inside_place(destination.root, source.root):
        raise PBIError(
            f"The destination {destination.root} is inside the lake that is published. "
            "Publish to a place of its own."
        )
    if inside_place(source.root, destination.root):
        raise PBIError(
            f"The lake that is published is inside the destination {destination.root}. "
            "Publish to a place of its own."
        )


def _check_destination(
    destination: LakeStore, publisher: str, force: bool
) -> Optional[PublishInfo]:
    marker = destination.refresh_marker()  # not what was read when the store was made
    root = destination.root
    try:
        has_content = bool(root.exists() and any(True for _ in root.iterdir()))
    except OSError:
        has_content = False
    if marker is None and has_content:
        raise PBIError(
            f"{root} has content and is not a published lake. Publish into an empty "
            "folder, or into the folder of an earlier publish, so that nothing that is "
            "there is overwritten."
        )
    if marker is not None and marker.published_by != publisher and not force:
        raise PBIError(
            f"{root} was published by {marker.published_by} on "
            f"{marker.published_at:%Y-%m-%d %H:%M} UTC. Publishing over it needs --force."
        )
    return marker


def _files_of(directory: Any) -> Tuple[Tuple[str, int], ...]:
    found = [
        (child.name, child.stat().st_size)
        for child in directory.iterdir()
        if child.is_file() and not child.name.endswith(".tmp")
    ]
    return tuple(sorted(found, key=lambda f: (f[0] == MANIFEST, f[0])))  # manifest last


def _parts(root: Any, path: Any) -> Tuple[str, ...]:
    """The folders between the root of the lake and a folder in it."""
    below = str(path)[len(str(root)) :]
    return tuple(part for part in below.replace("\\", "/").split("/") if part)


def _published_state(
    state: Optional[Dict[str, Any]], tenant: str, scans: bool
) -> Dict[str, Any]:
    """What a published lake keeps of the sync state: how the last runs went, and (with
    the scans) when the last complete scan started. Never the units that failed, and never
    the ids of scans that were started and not collected."""
    state = state or {}
    scan = {k: v for k, v in (state.get("scan") or {}).items() if k != "jobs"}
    return {
        "schema": STATE_SCHEMA,
        "tenant": tenant,
        "updated_at": state.get("updated_at"),
        "runs": list(state.get("runs") or [])[-RUNS_KEPT:],
        "units": {},
        "units_dropped": 0,
        "scan": scan if scans else {},
    }


def plan_publish(
    source: LakeStore,
    destination: LakeStore,
    *,
    tenants: Optional[Iterable[str]] = None,
    exclude: Iterable[str] = (),
    history: bool = False,
    prune: bool = False,
    force: bool = False,
    publisher: Optional[str] = None,
) -> PublishPlan:
    """Work out what a publish would copy. Nothing is written.

    :param source: the lake to publish
    :param destination: where to publish it: empty, or the place of an earlier publish
    :param tenants: only these tenants (default: all)
    :param exclude: categories to leave out (`EXCLUDABLE`)
    :param history: copy every version, not only the newest of each request
    :param prune: afterwards delete the older versions at the destination
    :param force: publish over a lake that someone else published
    :param publisher: who publishes (default: the user and the machine)
    :raises PBIError: for a destination that is the lake, inside it or around it, that has
        content but is not a published lake, or that someone else published; for a
        category that cannot be left out; or when there is nothing to publish
    """
    excluded = _check_excluded(exclude)
    if prune and history:
        raise PBIError(
            "--prune keeps the newest version of each request, so it cannot go with "
            "--history."
        )
    publisher = publisher or publisher_name()
    _check_places(source, destination)

    found = source.tenants()
    if tenants:
        wanted = [safe_name(t) for t in tenants]
        missing = [t for t in wanted if t not in found]
        if missing:
            raise PBIError(
                f"The lake holds no data of tenant '{missing[0]}'. Tenants in the lake: "
                f"{', '.join(found) or 'none'}."
            )
        found = [t for t in found if t in wanted]
    if not found:
        raise PBIError("The lake holds nothing to publish.")
    before = _check_destination(destination, publisher, force)

    summaries = {
        c.name: CategorySummary(c, excluded=c.name in excluded) for c in CATEGORIES
    }
    leaves: List[Leaf] = []
    for tenant in found:
        for endpoint, directory, mutable in source.publishable(tenant, history):
            name = category_of(endpoint)
            files = _files_of(directory)
            summary = summaries[name]
            summary.files += len(files)
            summary.size += sum(size for _, size in files)
            if name in excluded:
                continue
            leaves.append(
                Leaf(
                    tenant,
                    endpoint,
                    name,
                    directory,
                    _parts(source.root, directory),
                    files,
                    mutable,
                )
            )
    if not leaves:
        raise PBIError("There is nothing left to publish after what was left out.")
    states = {
        tenant: _published_state(
            source.read_state(tenant, STATE_NAME), tenant, "scans" not in excluded
        )
        for tenant in found
    }
    return PublishPlan(
        source=source,
        destination=destination,
        publisher=publisher,
        tenants=found,
        leaves=leaves,
        summaries=list(summaries.values()),
        states=states,
        excluded=excluded,
        history=history,
        prune=prune,
        before=before,
    )


def publish(
    plan: PublishPlan,
    *,
    now: Optional[datetime] = None,
    on_leaf: Optional[Callable[[int, int], None]] = None,
) -> PublishResult:
    """Copy what the plan says, and mark the destination as published.

    :param plan: from `plan_publish`
    :param now: when it is published (default: now)
    :param on_leaf: called as ``(done, total)`` after each folder, for a progress line
    """
    writer = LakeStore(plan.destination.root, publishing=True)
    when = now or datetime.now(timezone.utc)
    if plan.before is None:
        # the first publish: protect the place before the first file is written, and let a
        # publish that stops half way be finished by publishing again
        writer.write_marker(
            PublishInfo(
                published_at=when,
                published_by=plan.publisher,
                tenants=sorted(plan.tenants),
                excluded=sorted(plan.excluded),
                history=plan.history,
                cli_version=cli_version(),
                complete=False,
            )
        )
    copied = skipped = size = 0
    for done, leaf in enumerate(plan.leaves, start=1):
        target = writer.root.joinpath(*leaf.parts)
        if not leaf.mutable and (target / MANIFEST).exists():
            skipped += len(leaf.files)  # a version never changes: it is there already
        else:
            for (
                name,
                _,
            ) in leaf.files:  # the manifest last: a copy that stops is ignored
                payload = (leaf.source / name).read_bytes()
                writer.write_file(target / name, payload)
                copied += 1
                size += len(payload)
        if on_leaf is not None:
            on_leaf(done, len(plan.leaves))
    for tenant, state in plan.states.items():
        writer.write_state(tenant, STATE_NAME, state)
    pruned = 0
    if plan.prune:
        for tenant in plan.tenants:
            pruned += writer.prune(keep=1, tenant=tenant)
    info = PublishInfo(
        published_at=when,
        published_by=plan.publisher,
        tenants=sorted(plan.tenants),
        excluded=sorted(plan.excluded),
        history=plan.history,
        files=plan.files,
        size=plan.size,
        cli_version=cli_version(),
    )
    writer.write_marker(info)  # last: it says the copy is complete
    return PublishResult(copied, skipped, size, pruned, info)
