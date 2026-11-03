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
"""

from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Set, Tuple

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


def _wanted_workspaces(
    catalog: Catalog, workspaces: Optional[Iterable[str]]
) -> Optional[Set[str]]:
    """The ids of the workspaces that were asked for by id or by name, ``None`` for all.

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


def access_report(
    catalog: Catalog,
    kind: str,
    workspaces: Optional[Sequence[str]] = None,
) -> AccessReport:
    """Everyone with access to every item of a kind, as far as the lake knows.

    :param catalog: the lake
    :param kind: one of `ACCESS_KINDS`
    :param workspaces: only the items of these workspaces, by id or by name (default: all;
        an app is in no workspace, so none is given with one)
    :raises ValueError: for a kind that is not in `ACCESS_KINDS`, or a workspace that the
        lake does not have
    """
    if kind not in ACCESS_KINDS:
        raise ValueError(
            f"Unknown kind '{kind}'. Choose one of: {', '.join(ACCESS_KINDS)}."
        )
    wanted = _wanted_workspaces(catalog, workspaces)
    found = AccessReport()
    names = {w.id: w.name for w in catalog.workspaces()}

    if kind == "workspace":
        subjects: List[Workspace] = [
            w for w in catalog.workspaces() if wanted is None or w.id in wanted
        ]
        for workspace in subjects:
            view = catalog.users(workspace)
            found.items += 1
            if not view.rows:
                found.missing.append(workspace.name)
            for person in view.rows:
                found.rows.append(
                    AccessRow(
                        "workspace",
                        workspace.name,
                        workspace.id,
                        workspace.name,
                        workspace.id,
                        person,
                        view.source,
                    )
                )
        found.rows.sort(key=_order)
        return found

    items: List[Item] = [
        i
        for i in catalog.all_items(kind)
        if wanted is None or (i.workspace_id or "") in wanted
    ]
    for item in items:
        view = catalog.users(item)
        where = names.get(item.workspace_id or "", "")
        found.items += 1
        if not view.rows:
            found.missing.append(_label_of(item.name, where))
        for person in view.rows:
            found.rows.append(
                AccessRow(
                    kind,
                    item.name,
                    item.id,
                    where,
                    item.workspace_id or "",
                    person,
                    view.source,
                )
            )
    found.rows.sort(key=_order)
    return found


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
) -> AccessReport:
    """Everything that one person has access to, as far as the lake knows.

    The person is matched by e-mail address or name, ignoring case. Access that comes
    through a group shows as the group: the lake does not hold who is in a group.

    :param who: an e-mail address or a name (a part of one with ``partial``)
    :param kinds: only these kinds (default: all of `ACCESS_KINDS`)
    :param workspaces: only the items of these workspaces, by id or name (apps are in
        none, so they are left out)
    :param partial: match a part of the e-mail address or name, not all of it
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
        part = access_report(catalog, kind, workspaces)
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
