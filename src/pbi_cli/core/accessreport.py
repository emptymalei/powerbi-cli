"""Who has access to every item of a kind, from what the lake holds.

The Users tab of the terminal UI shows the people of one report or one workspace. This is
the same for all of them at once: every person of every report, or of every workspace, one
row for each person and item, ready to be saved as a spreadsheet.

```python
from pbi_cli.core.accessreport import access_report

found = access_report(catalog, "report")
for row in found.rows:
    print(row.item, row.person.email, row.person.role)
print(found.missing)  # the reports whose users the lake does not hold
```

Nothing here calls the API: a person is listed when the lake holds the users of the item
(fetch them with ``pbi sync run report-users``, ``app-users`` or a scan with
``--get-artifact-users``). Nothing here imports a CLI framework or Textual.

**The access table.** The users of every item are spread over thousands of small files (one
answer per item, and the scans), and reading them all takes seconds to minutes. So they are
read once into one table, kept in the lake beside the snapshots it comes from
(``tenant=<tenant>/_tables/access.csv``, and ``access-gaps.csv`` for the items whose users the
lake does not hold), and every question reads that. Writing a snapshot marks the table stale
(`pbi_cli.core.store.LakeStore.tables_stale`); the next question, or `refresh_access_table`
at the end of a sync, builds it again. A lake that is only read gets its table built in
memory, each time it is opened.
"""

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from loguru import logger

from pbi_cli.core.catalog import KINDS, Access, Catalog, Item, Workspace, label

#: The kinds that can be asked for: a workspace, or one of `pbi_cli.core.catalog.KINDS`.
ACCESS_KINDS: Tuple[str, ...] = ("workspace",) + KINDS

#: The columns of the report as a table.
COLUMNS = (
    "Kind",
    "Item",
    "Item id",
    "Workspace",
    "Workspace id",
    "Name",
    "E-mail or id",
    "Access",
    "Type",
    "Source",
)


@dataclass(frozen=True)
class AccessRow:
    """One person (or group) with access to one item.

    :param kind: ``workspace``, ``report``, ...
    :param item: the name of the item
    :param item_id: its id
    :param workspace: the name of the workspace it is in (empty for an app, or an item
        whose workspace the lake does not know)
    :param workspace_id: that workspace's id
    :param person: who, and what they may do
    :param source: where the lake has the people from (an operation, or ``scan``)
    """

    kind: str
    item: str
    item_id: str
    workspace: str
    workspace_id: str
    person: Access
    source: str

    def cells(self) -> Tuple[str, ...]:
        """The row as text, in the order of `COLUMNS`."""
        return (
            label(self.kind),
            self.item,
            self.item_id,
            self.workspace,
            self.workspace_id,
            self.person.name,
            self.person.email,
            self.person.role,
            self.person.type,
            self.source,
        )


@dataclass
class AccessReport:
    """The people of many items.

    :param rows: one for each person and item, by item and then person
    :param missing: the items whose users the lake does not hold, as ``name (workspace)``
    :param items: how many items were looked at
    """

    rows: List[AccessRow] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    items: int = 0

    @property
    def table(self) -> Tuple[Tuple[str, ...], ...]:
        """The rows as text, for `pbi_cli.tui.exporting.Exportable`."""
        return tuple(row.cells() for row in self.rows)

    @property
    def people(self) -> int:
        """How many different people (by e-mail address or id) there are."""
        return len(
            {row.person.email.casefold() for row in self.rows if row.person.email}
        )


def resolve_workspaces(
    catalog: Catalog, workspaces: Optional[Iterable[str]]
) -> Optional[Set[str]]:
    """The ids of the workspaces that were asked for by id or by name, ``None`` for all
    (public, so that a command can restrict what it fetches to the same workspaces).

    :raises ValueError: for a name or id that no workspace has
    """
    if not workspaces:
        return None
    known = catalog.workspaces()
    wanted: Set[str] = set()
    for given in workspaces:
        text = given.strip()
        hits = [
            w
            for w in known
            if w.id.casefold() == text.casefold()
            or w.name.casefold() == text.casefold()
        ]
        if not hits:
            raise ValueError(f"The lake has no workspace '{given}' (by id or name).")
        wanted.update(w.id for w in hits)
    return wanted


def _label_of(name: str, workspace: str) -> str:
    return f"{name} ({workspace})" if workspace else name


@dataclass(frozen=True)
class Gap:
    """An item whose users the lake does not hold.

    :param kind: ``workspace``, ``report``, ...
    :param item: its name
    :param item_id: its id
    :param workspace: the name of its workspace (empty when there is none)
    :param workspace_id: that workspace's id
    """

    kind: str
    item: str
    item_id: str
    workspace: str
    workspace_id: str

    @property
    def label(self) -> str:
        return _label_of(self.item, self.workspace)


@dataclass
class AccessTable:
    """Everybody with access to every item the lake knows, and the items whose users it
    does not hold.

    :param rows: one for each person and item, by kind (in the order of `ACCESS_KINDS`),
        then item, then person
    :param gaps: the items without users, by kind, then name
    :param built_at: when it was worked out
    """

    rows: List[AccessRow] = field(default_factory=list)
    gaps: List[Gap] = field(default_factory=list)
    built_at: Optional[datetime] = None

    def report(self, kind: str, wanted: Optional[Set[str]] = None) -> AccessReport:
        """The part about one kind, for the items of some workspaces (``None``: all)."""
        found = AccessReport()
        found.rows = [
            r
            for r in self.rows
            if r.kind == kind and (wanted is None or r.workspace_id in wanted)
        ]
        gaps = [
            g
            for g in self.gaps
            if g.kind == kind and (wanted is None or g.workspace_id in wanted)
        ]
        found.missing = [g.label for g in gaps]
        found.items = len({r.item_id for r in found.rows}) + len(gaps)
        return found


#: The version of the files of the access table; a table of another version is built again.
TABLE_VERSION = 1
_ROWS = "access.csv"
_GAPS = "access-gaps.csv"
_MANIFEST = "access.json"
_ROW_FIELDS = (
    "kind",
    "item",
    "item_id",
    "workspace",
    "workspace_id",
    "name",
    "email",
    "access",
    "type",
    "source",
)
_GAP_FIELDS = ("kind", "item", "item_id", "workspace", "workspace_id")


def _walk(catalog: Catalog, kind: str) -> Tuple[List[AccessRow], List[Gap]]:
    """The people of every item of a kind, read from the snapshots and the scans.

    The walk goes in the order of the stored scans, so that each is read once: the users of
    many items are only in their scan. The rows are put in order of item and person, and the
    gaps in order of name, at the end.
    """
    rows: List[AccessRow] = []
    names = {w.id: w.name for w in catalog.workspaces()}
    if kind == "workspace":
        order = {w.id: n for n, w in enumerate(catalog.workspaces())}
        by_id = {w.id: w for w in catalog.workspaces()}
        lacking: List[Workspace] = []
        for workspace_id in catalog.scan_order(by_id):
            workspace = by_id[workspace_id]
            view = catalog.users(workspace)
            if not view.rows:
                lacking.append(workspace)
            rows.extend(
                AccessRow(
                    "workspace",
                    workspace.name,
                    workspace.id,
                    workspace.name,
                    workspace.id,
                    person,
                    view.source,
                )
                for person in view.rows
            )
        lacking.sort(key=lambda w: order[w.id])
        gaps = [Gap("workspace", w.name, w.id, w.name, w.id) for w in lacking]
    else:
        without: List[Item] = []
        for item in catalog.walk_items(kind):
            view = catalog.users(item)
            where = names.get(item.workspace_id or "", "")
            if not view.rows:
                without.append(item)
            rows.extend(
                AccessRow(
                    kind,
                    item.name,
                    item.id,
                    where,
                    item.workspace_id or "",
                    person,
                    view.source,
                )
                for person in view.rows
            )
        without.sort(key=lambda i: (i.name.casefold(), i.id))
        gaps = [
            Gap(
                kind,
                i.name,
                i.id,
                names.get(i.workspace_id or "", ""),
                i.workspace_id or "",
            )
            for i in without
        ]
    rows.sort(key=_order)
    return rows, gaps


def build_access_table(catalog: Catalog) -> AccessTable:
    """Work out the access table from the snapshots and scans of the lake (slow on a big
    lake: it reads the users of every item)."""
    table = AccessTable(built_at=datetime.now(timezone.utc))
    for kind in ACCESS_KINDS:
        rows, gaps = _walk(catalog, kind)
        table.rows += rows
        table.gaps += gaps
    return table


def _csv(fields: Sequence[str], rows: Iterable[Sequence[str]]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(fields)
    writer.writerows(rows)
    return out.getvalue().encode("utf-8-sig")  # a spreadsheet then opens it as UTF-8


def _read_csv(payload: bytes) -> List[Dict[str, str]]:
    return list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig"))))


def save_access_table(catalog: Catalog, table: AccessTable) -> None:
    """Keep the table in the lake (the stale mark must have been removed before it was
    worked out: see `refresh_access_table`)."""
    store, tenant = catalog.store, catalog.tenant
    store.write_table(
        tenant,
        _ROWS,
        _csv(
            _ROW_FIELDS,
            (
                (
                    r.kind,
                    r.item,
                    r.item_id,
                    r.workspace,
                    r.workspace_id,
                    r.person.name,
                    r.person.email,
                    r.person.role,
                    r.person.type,
                    r.source,
                )
                for r in table.rows
            ),
        ),
    )
    store.write_table(
        tenant,
        _GAPS,
        _csv(
            _GAP_FIELDS,
            (
                (g.kind, g.item, g.item_id, g.workspace, g.workspace_id)
                for g in table.gaps
            ),
        ),
    )
    manifest = {
        "version": TABLE_VERSION,
        "built_at": (table.built_at or datetime.now(timezone.utc)).isoformat(),
        "rows": len(table.rows),
        "gaps": len(table.gaps),
    }
    # last: a table without its manifest is not taken
    store.write_table(tenant, _MANIFEST, json.dumps(manifest, indent=2).encode())


def load_access_table(catalog: Catalog) -> Optional[AccessTable]:
    """The table the lake keeps, or ``None`` when there is none, it is stale (a snapshot was
    written after it), or it cannot be read."""
    store, tenant = catalog.store, catalog.tenant
    if store.tables_stale(tenant):
        return None
    raw = store.read_table(tenant, _MANIFEST)
    if raw is None:
        return None
    try:
        manifest = json.loads(raw)
        if manifest.get("version") != TABLE_VERSION:
            return None
        rows, gaps = store.read_table(tenant, _ROWS), store.read_table(tenant, _GAPS)
        if rows is None or gaps is None:
            return None
        return AccessTable(
            rows=[
                AccessRow(
                    r["kind"],
                    r["item"],
                    r["item_id"],
                    r["workspace"],
                    r["workspace_id"],
                    Access(r["name"], r["email"], r["access"], r["type"]),
                    r["source"],
                )
                for r in _read_csv(rows)
            ],
            gaps=[Gap(**{k: g[k] for k in _GAP_FIELDS}) for g in _read_csv(gaps)],
            built_at=datetime.fromisoformat(manifest["built_at"]),
        )
    except (ValueError, KeyError, TypeError) as error:
        logger.warning(f"The stored access table cannot be read ({error}): building it")
        return None


def refresh_access_table(catalog: Catalog) -> AccessTable:
    """Work the table out again, and keep it in the lake when the lake can be written."""
    writable = catalog.store.writable
    if writable:
        catalog.store.begin_tables(catalog.tenant)
    table = build_access_table(catalog)
    if writable:
        save_access_table(catalog, table)
    return table


#: The name `access_table` keeps its table under in the catalog.
_DERIVED = "access-table"


def access_table(
    catalog: Catalog, on_build: Optional[Callable[[], None]] = None
) -> AccessTable:
    """The access table: the one the catalog read already, else the one the lake keeps,
    else worked out now (and kept in the lake when it can be written).

    :param on_build: called before the table is worked out, which takes a while on a big
        lake (to say so)
    """

    def make() -> AccessTable:
        found = load_access_table(catalog)
        if found is not None:
            return found
        if on_build is not None:
            on_build()
        return refresh_access_table(catalog)

    return catalog.derived(_DERIVED, make)


def access_report(
    catalog: Catalog,
    kind: str,
    workspaces: Optional[Sequence[str]] = None,
    on_build: Optional[Callable[[], None]] = None,
) -> AccessReport:
    """Everyone with access to every item of a kind, as far as the lake knows.

    :param catalog: the lake
    :param kind: one of `ACCESS_KINDS`
    :param workspaces: only the items of these workspaces, by id or by name (default: all;
        an app is in no workspace, so none is given with one)
    :param on_build: see `access_table`
    :raises ValueError: for a kind that is not in `ACCESS_KINDS`, or a workspace that the
        lake does not have
    """
    if kind not in ACCESS_KINDS:
        raise ValueError(
            f"Unknown kind '{kind}'. Choose one of: {', '.join(ACCESS_KINDS)}."
        )
    wanted = resolve_workspaces(catalog, workspaces)
    return access_table(catalog, on_build).report(kind, wanted)


def _is_person(person: Access, who: str, partial: bool) -> bool:
    wanted = who.strip().casefold()
    fields = (person.email.casefold(), person.name.casefold())
    if partial:
        return any(wanted in field for field in fields)
    return wanted in fields


def access_of(
    catalog: Catalog,
    who: str,
    *,
    kinds: Optional[Sequence[str]] = None,
    workspaces: Optional[Sequence[str]] = None,
    partial: bool = False,
    on_build: Optional[Callable[[], None]] = None,
) -> AccessReport:
    """Everything that one person has access to, as far as the lake knows.

    The person is matched by e-mail address or name, ignoring case. Access that comes
    through a group shows as the group: the lake does not hold who is in a group.

    :param who: an e-mail address or a name (a part of one with ``partial``)
    :param kinds: only these kinds (default: all of `ACCESS_KINDS`)
    :param workspaces: only the items of these workspaces, by id or name (apps are in
        none, so they are left out)
    :param partial: match a part of the e-mail address or name, not all of it
    :param on_build: see `access_table`
    :return: one row for each item they have access to; ``items`` counts the items looked
        at and ``missing`` those whose users the lake does not hold, so the answer may be
        incomplete
    :raises ValueError: for a kind that is not in `ACCESS_KINDS`, a workspace the lake does
        not have, or nobody to look for
    """
    if not who.strip():
        raise ValueError("Say whose access to look for: an e-mail address or a name.")
    wanted = list(kinds or ACCESS_KINDS)
    for kind in wanted:
        if kind not in ACCESS_KINDS:
            raise ValueError(
                f"Unknown kind '{kind}'. Choose one of: {', '.join(ACCESS_KINDS)}."
            )
    found = AccessReport()
    for kind in wanted:
        if workspaces and kind == "app":
            continue
        part = access_report(catalog, kind, workspaces, on_build)
        found.items += part.items
        found.missing += [f"{kind} {name}" for name in part.missing]
        found.rows += [r for r in part.rows if _is_person(r.person, who, partial)]
    order = {kind: n for n, kind in enumerate(ACCESS_KINDS)}
    found.rows.sort(key=lambda r: (order[r.kind],) + _order(r))
    return found


def _order(row: AccessRow) -> Tuple[str, str, str, str]:
    return (
        row.item.casefold(),
        row.item_id,
        row.person.name.casefold(),
        row.person.email.casefold(),
    )
