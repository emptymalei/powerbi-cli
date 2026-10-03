"""What the TUI shows, made from what the catalog knows.

Everything here is a plain function from catalog data to table rows or Rich renderables, so
that it can be tested without a terminal. The screens only put the results in widgets.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from rich.console import Group, RenderableType
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from pbi_cli.core.catalog import (
    KINDS,
    Catalog,
    Freshness,
    Item,
    Lineage,
    LineageNode,
    UsersView,
    Version,
    Workspace,
    id_of,
    label,
    name_of,
    scalar_fields,
)
from pbi_cli.core.details import Detail as ItemDetail
from pbi_cli.core.planfile import PlanFile, describe_scan
from pbi_cli.core.planrun import SequencePlan
from pbi_cli.core.store import EventDay
from pbi_cli.core.sync.plan import Plan
from pbi_cli.core.timefmt import format_age
from pbi_cli.tui.fetching import Fetching

#: Rows shown at most in the table, and nodes at most in the tree; a filter narrows them.
MAX_ROWS = 2000
MAX_NODES = 5000

#: Lines of JSON shown at most (the full answer is in the lake).
MAX_JSON_LINES = 1500

_DOTS = {
    Freshness.FRESH: ("●", "green"),
    Freshness.AGING: ("●", "yellow"),
    Freshness.OLD: ("●", "red"),
    Freshness.NONE: ("○", "grey50"),
}

_MEANING = {
    Freshness.FRESH: "fresh",
    Freshness.AGING: "older than its time to live",
    Freshness.OLD: "a week old or more",
    Freshness.NONE: "not in the lake",
}


@dataclass
class Plain:
    """Something that is shown by its fields only: a capacity, an event, a part of the lake.

    :param kind: ``capacity``, ``event`` or ``section``
    :param title: its name
    :param data: its fields
    """

    kind: str
    title: str
    data: Dict[str, Any]


Subject = Union[Workspace, Item, Plain, EventDay]


def dot(level: Freshness) -> Text:
    """The freshness dot: green, yellow, red, or an empty grey circle."""
    symbol, style = _DOTS[level]
    return Text(symbol, style=style)


def meaning(level: Freshness) -> str:
    """What a freshness dot says, in words."""
    return _MEANING[level]


def ago(catalog: Catalog, when: Optional[datetime]) -> str:
    """How long ago something happened: ``3 h ago``, or ``never``."""
    age = catalog.age(when)
    return "never" if age is None else f"{format_age(age)} ago"


def short_time(stamp: str) -> str:
    """A time as the API wrote it, for a table: ``2026-09-30 12:00``."""
    return stamp.replace("T", " ")[:16]


def short_id(value: str, width: int = 12) -> str:
    """The start of a long id, for the header."""
    return value if len(value) <= width else value[:width] + "…"


# -- table rows ------------------------------------------------------------------------


@dataclass
class Entry:
    """A row of the table of the Explorer.

    :param key: identifies the row in the table
    :param kind: what the row is: ``workspace``, ``report``, ``capacity``, ``day``, ...
    :param cells: what the columns show
    :param subject: what the detail pane shows when the row is selected
    :param text: the words a filter looks in (lower case)
    """

    key: str
    kind: str
    cells: Tuple[Union[str, Text], ...]
    subject: Subject
    text: str


WORKSPACE_COLUMNS = ("", "Name", "Type", "State", "Items", "Scanned")
ITEM_COLUMNS = ("Name", "Type", "Owner", "Updated", "From")
APP_COLUMNS = ("Name", "Workspace", "Published by", "Updated")
CAPACITY_COLUMNS = ("Name", "SKU", "Region", "State")
DAY_COLUMNS = ("Day", "Events", "State", "Updated")
EVENT_COLUMNS = ("Time", "Activity", "User", "Item", "Workspace")


def _entry(
    key: str, kind: str, cells: Sequence[Union[str, Text]], subject: Subject
) -> Entry:
    return Entry(
        key=key,
        kind=kind,
        cells=tuple(cells),
        subject=subject,
        text=" ".join(str(cell) for cell in cells).lower(),
    )


def workspace_entries(catalog: Catalog, workspaces: Iterable[Workspace]) -> List[Entry]:
    """Rows for a list of workspaces."""
    rows = []
    for workspace in workspaces:
        counts = catalog.counts(workspace.id)
        scanned = catalog.scan_as_of(workspace.id)
        rows.append(
            _entry(
                workspace.id,
                "workspace",
                (
                    dot(catalog.workspace_freshness(workspace.id)),
                    workspace.name,
                    workspace.type or "-",
                    workspace.state or "-",
                    str(sum(counts.values())) if counts else "-",
                    ago(catalog, scanned) if scanned else "-",
                ),
                workspace,
            )
        )
    return rows


def item_entries(items: Iterable[Item]) -> List[Entry]:
    """Rows for the items of a workspace."""
    return [
        _entry(
            f"{item.kind}:{item.id}",
            item.kind,
            (
                item.name,
                label(item.kind),
                item.owner or "-",
                short_time(item.updated) or "-",
                item.sources or "-",
            ),
            item,
        )
        for item in items
    ]


def app_entries(catalog: Catalog) -> List[Entry]:
    """Rows for the apps of the tenant."""
    names = {w.id: w.name for w in catalog.workspaces()}
    return [
        _entry(
            f"app:{item.id}",
            "app",
            (
                item.name,
                names.get(item.workspace_id or "", "-"),
                item.owner or "-",
                short_time(item.updated) or "-",
            ),
            item,
        )
        for item in catalog.all_items("app")
    ]


def capacity_entries(catalog: Catalog) -> List[Entry]:
    """Rows for the capacities of the tenant."""
    return [
        _entry(
            f"capacity:{id_of('capacity', row)}",
            "capacity",
            (
                name_of("capacity", row),
                str(row.get("sku") or "-"),
                str(row.get("region") or "-"),
                str(row.get("state") or "-"),
            ),
            Plain("capacity", name_of("capacity", row), row),
        )
        for row in catalog.capacities()
    ]


def day_entries(catalog: Catalog) -> List[Entry]:
    """Rows for the days of audit events in the lake."""
    return [
        _entry(
            f"day:{day.day.isoformat()}",
            "day",
            (
                day.day.isoformat(),
                str(day.rows),
                "complete" if day.sealed else "open",
                ago(catalog, day.updated_at),
            ),
            day,
        )
        for day in catalog.event_days()
    ]


def event_entries(events: Iterable[Dict[str, Any]]) -> List[Entry]:
    """Rows for the audit events of a day."""
    rows = []
    for number, event in enumerate(events):
        rows.append(
            _entry(
                f"event:{event.get('Id') or number}",
                "event",
                (
                    str(event.get("CreationTime") or "-").replace("T", " ")[:19],
                    str(event.get("Activity") or "-"),
                    str(event.get("UserId") or "-"),
                    str(
                        event.get("ItemName")
                        or event.get("ReportName")
                        or event.get("DatasetName")
                        or event.get("DashboardName")
                        or "-"
                    ),
                    str(
                        event.get("WorkSpaceName") or event.get("WorkspaceName") or "-"
                    ),
                ),
                Plain("event", str(event.get("Activity") or "event"), event),
            )
        )
    return rows


OVERVIEW_COLUMNS = ("", "What", "Count", "Fetched")


def lake_subject(catalog: Catalog) -> Plain:
    """What the detail pane shows for the tenant as a whole."""
    counts = {
        label(kind, True): len(catalog.all_items(kind))
        for kind in KINDS
        if kind != "app"
    }
    counts[label("app", True)] = len(catalog.all_items("app"))
    counts[label("capacity", True)] = len(catalog.capacities())
    counts["Days of audit events"] = len(catalog.event_days())
    workspaces = len(catalog.workspaces())
    if not workspaces and not any(counts.values()):
        return Plain(
            "section",
            "The lake is empty",
            {
                "Tenant": catalog.tenant,
                "Lake": str(catalog.location),
                "Next": "press s to open the Sync screen and fetch the tenant",
                "Sign in": "press a if the token expired or is missing",
            },
        )
    return Plain(
        "lake",
        "The data lake",
        {
            "Tenant": catalog.tenant,
            "Lake": str(catalog.location),
            "Workspaces": workspaces,
            **{name: count for name, count in counts.items() if count},
        },
    )


def overview_entries(catalog: Catalog) -> List[Entry]:
    """Rows for the lake as a whole: each list of the tenant, the scans, the events."""
    rows = []
    for kind in (
        "workspace",
        "report",
        "dataset",
        "dashboard",
        "dataflow",
        "app",
        "capacity",
    ):
        found = catalog.listing(kind)
        rows.append(
            _entry(
                f"section:{kind}",
                "section",
                (
                    dot(catalog.listing_freshness(kind)),
                    label(kind, True),
                    str(len(found.rows)) if found else "-",
                    ago(catalog, found.fetched_at) if found else "not in the lake",
                ),
                Plain(
                    "section",
                    label(kind, True),
                    {
                        "Operation": found.endpoint if found else "-",
                        "Rows": len(found.rows) if found else 0,
                        "Fetched": ago(catalog, found.fetched_at) if found else "never",
                        "Partial": (
                            "yes: fetched with a filter or a limit"
                            if found and found.partial
                            else "no"
                        ),
                        "Folder": str(found.snapshot.directory) if found else "-",
                    },
                ),
            )
        )
    days = catalog.event_days()
    rows.append(
        _entry(
            "section:activity",
            "section",
            (
                dot(Freshness.NONE if not days else Freshness.FRESH),
                "Audit events",
                f"{len(days)} day(s)" if days else "-",
                "-" if not days else f"{sum(d.rows for d in days)} events",
            ),
            Plain(
                "section",
                "Audit events",
                {
                    "Days": len(days),
                    "Events": sum(d.rows for d in days),
                    "Complete days": sum(1 for d in days if d.sealed),
                },
            ),
        )
    )
    return rows


def legend() -> Text:
    """What the freshness dots mean."""
    text = Text()
    for level in (Freshness.FRESH, Freshness.AGING, Freshness.OLD, Freshness.NONE):
        text.append_text(dot(level))
        text.append(f" {meaning(level)}   ", style="grey62")
    return text


def filter_entries(entries: Sequence[Entry], text: str) -> List[Entry]:
    """The rows that have all the words of ``text`` in them (case does not matter)."""
    words = text.lower().split()
    if not words:
        return list(entries)
    return [entry for entry in entries if all(word in entry.text for word in words)]


# -- the detail pane -------------------------------------------------------------------


def _grid(rows: Iterable[Tuple[str, Union[str, Text]]]) -> Table:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="grey62", no_wrap=True)
    grid.add_column(overflow="fold")
    for key, value in rows:
        grid.add_row(key, value)
    return grid


def _title(name: str, subtitle: str) -> Text:
    text = Text()
    text.append(name or "(no name)", style="bold")
    if subtitle:
        text.append("   " + subtitle, style="grey62")
    return text


def _words(names: Sequence[str]) -> str:
    """``reports``, ``reports and datasets``, ``reports, datasets and apps``."""
    if len(names) < 2:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def contents_rows(catalog: Catalog, workspace_id: str) -> List[Tuple[str, str]]:
    """What the lake holds of a workspace, by kind: how many, or that the list of the
    kind is not in the lake at all (so that nothing can be said about it)."""
    counts = catalog.counts(workspace_id)
    rows = []
    for kind in KINDS:
        if kind in counts:
            rows.append((label(kind, True), str(counts[kind])))
        elif not catalog.listed(kind, workspace_id):
            rows.append((label(kind, True), "the list is not in the lake"))
    return rows


@dataclass(frozen=True)
class Hint:
    """Why a workspace shows nothing, and what to do about it.

    :param short: a few words, for the title of the table
    :param long: a sentence, for the Info tab
    """

    short: str
    long: str


def empty_hint(catalog: Catalog, workspace_id: str) -> Hint:
    """Say why a workspace has no items: the lists are missing, it was never scanned, or
    it really is empty."""
    missing = [
        label(kind, True).lower()
        for kind in KINDS
        if not catalog.listed(kind, workspace_id)
    ]
    if missing:
        return Hint(
            "the lists of items are not in the lake: press s, then Run",
            f"The lists of {_words(missing)} are not in the lake, so there is nothing "
            "to show. Press s and Run to fetch the lists of the tenant, or r to scan "
            "this workspace.",
        )
    from_administrators = any(catalog.listing(kind) is not None for kind in KINDS)
    if from_administrators and catalog.scan_of(workspace_id) is None:
        return Hint(
            "never scanned: press r to scan it",
            "No list holds an item of this workspace and it was never scanned. Press r "
            "to scan it.",
        )
    return Hint(
        "it holds nothing",
        "This workspace holds no report, dataset, dashboard, dataflow or app.",
    )


def provenance(
    catalog: Catalog, subject: Subject
) -> List[Tuple[str, Union[str, Text]]]:
    """Where what is shown comes from, and how fresh it is."""
    rows: List[Tuple[str, Union[str, Text]]] = []
    if isinstance(subject, (Workspace, Item)):
        kind = subject.kind
        found = catalog.listing("workspace" if kind == "workspace" else kind)
        if found is not None:
            note = (
                " (fetched with a filter or a limit: it may be incomplete)"
                if found.partial
                else ""
            )
            rows.append(
                (
                    "List",
                    f"{found.endpoint}, fetched {ago(catalog, found.fetched_at)}{note}",
                )
            )
        if found is None and isinstance(subject, Item) and subject.workspace_id:
            made = catalog.user_list(kind, subject.workspace_id)
            if made is not None:
                rows.append(
                    (
                        "List",
                        f"{made.manifest.get('endpoint', 'a list')} of the workspace, "
                        f"fetched {ago(catalog, made.fetched_at)}",
                    )
                )
        if isinstance(subject, Workspace) and subject.visible_to:
            rows.append(("Visible to", ", ".join(subject.visible_to)))
        workspace_id = (
            subject.id if isinstance(subject, Workspace) else subject.workspace_id
        )
        view = catalog.scan_of(workspace_id) if workspace_id else None
        if view is not None:
            flags = view.flags.describe()
            rows.append(
                (
                    "Scan",
                    f"as of {ago(catalog, view.as_of)}, options: {flags}",
                )
            )
        elif workspace_id:
            rows.append(("Scan", "none in the lake"))
    return rows


def info(catalog: Catalog, subject: Subject) -> RenderableType:
    """The Info tab: the plain fields, and where they come from."""
    if isinstance(subject, Workspace):
        head = _title(subject.name, f"workspace · {subject.id}")
        fields = scalar_fields(subject.raw) or [("id", subject.id)]
        extra: List[RenderableType] = []
        rows = contents_rows(catalog, subject.id)
        if rows:
            extra.append(_grid(rows))
        if not catalog.counts(subject.id):
            extra.append(Text())
            extra.append(Text(empty_hint(catalog, subject.id).long, style="yellow"))
        return Group(
            head,
            Text(),
            _grid(fields),
            Text(),
            *extra,
            Text(),
            _grid(provenance(catalog, subject)),
        )
    if isinstance(subject, Item):
        head = _title(subject.name, f"{label(subject.kind).lower()} · {subject.id}")
        return Group(
            head,
            Text(),
            _grid(scalar_fields(subject.data)),
            Text(),
            _grid(provenance(catalog, subject)),
        )
    if isinstance(subject, EventDay):
        return Group(
            _title(subject.day.isoformat(), "audit events"),
            Text(),
            _grid(
                [
                    ("Events", str(subject.rows)),
                    (
                        "State",
                        (
                            "complete: nothing more will be added"
                            if subject.sealed
                            else "open: more events may arrive"
                        ),
                    ),
                    ("Updated", ago(catalog, subject.updated_at)),
                    ("Folder", str(subject.directory)),
                ]
            ),
        )
    parts: List[RenderableType] = [
        _title(
            subject.title, "" if subject.kind in ("section", "lake") else subject.kind
        ),
        Text(),
        _grid(scalar_fields(subject.data)),
    ]
    if subject.kind == "lake":
        parts.extend([Text(), legend()])
    return Group(*parts)


def users_rows(view: UsersView) -> List[Tuple[str, str, str, str]]:
    """The rows of the Users tab."""
    return [
        (a.name or "-", a.email or "-", a.role or "-", a.type or "-") for a in view.rows
    ]


def users_note(catalog: Catalog, view: UsersView) -> str:
    """The line above the users: where they come from, or what to do to get them."""
    if view.missing:
        return view.missing
    return f"{len(view.rows)} with access, from {view.source}, fetched {ago(catalog, view.fetched_at)}."


_KIND_STYLES = {
    "report": "cyan",
    "dataset": "magenta",
    "dashboard": "yellow",
    "dataflow": "blue",
    "datasource": "green",
}


def _node_text(node: LineageNode) -> Text:
    text = Text()
    text.append(label(node.kind).lower() + " ", style=_KIND_STYLES.get(node.kind, ""))
    text.append(node.name, style="bold")
    if node.workspace:
        text.append(f"  in {node.workspace}", style="grey62")
    if node.note:
        text.append(f"  ({node.note})", style="italic grey62")
    return text


def _add_nodes(parent: Tree, nodes: Sequence[LineageNode]) -> None:
    for node in nodes:
        branch = parent.add(_node_text(node))
        _add_nodes(branch, node.children)


def lineage_renderable(item: Item, lineage: Lineage) -> RenderableType:
    """The Lineage tab: what the item is built from, and what is built on it."""
    here = Text()
    here.append(label(item.kind).lower() + " ", style=_KIND_STYLES.get(item.kind, ""))
    here.append(item.name, style="bold reverse")
    parts: List[RenderableType] = []

    up = Tree(Text("Built from", style="bold"))
    if lineage.upstream:
        _add_nodes(up, lineage.upstream)
    else:
        up.add(Text("nothing the lake knows of", style="grey62"))
    parts.append(up)

    parts.append(Text())
    parts.append(here)
    parts.append(Text())

    down = Tree(Text("Built on it", style="bold"))
    if lineage.downstream:
        _add_nodes(down, lineage.downstream)
    else:
        down.add(Text("nothing the lake knows of", style="grey62"))
    parts.append(down)

    if lineage.notes:
        parts.append(Text())
        parts.extend(Text(f"· {note}", style="grey62") for note in lineage.notes)
    return Group(*parts)


def json_renderable(data: Any, where: Optional[str] = None) -> RenderableType:
    """The JSON tab: the data, highlighted, cut when it is very long."""
    lines = json.dumps(data, indent=2, ensure_ascii=False, default=str).splitlines()
    cut = len(lines) > MAX_JSON_LINES
    shown = "\n".join(lines[:MAX_JSON_LINES])
    parts: List[RenderableType] = [
        Syntax(shown, "json", theme="ansi_dark", word_wrap=False)
    ]
    if cut:
        parts.append(
            Text(f"… {len(lines) - MAX_JSON_LINES} more lines", style="italic grey62")
        )
    if where:
        parts.append(Text(f"stored in {where}", style="grey62"))
    return Group(*parts)


def versions_rows(
    catalog: Catalog, versions: Sequence[Version]
) -> List[Tuple[str, str, str, str, str, str]]:
    """The rows of the Versions tab."""
    return [
        (
            f"{v.fetched_at:%Y-%m-%d %H:%M}",
            ago(catalog, v.fetched_at),
            v.endpoint,
            "-" if v.rows is None else str(v.rows),
            _size(v.size),
            v.profile or "-",
        )
        for v in versions
    ]


def _size(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{count} B"


def where_stored(subject: Subject, catalog: Catalog) -> Optional[str]:
    """The folder of the stored answer a subject was read from, for the JSON tab."""
    if isinstance(subject, EventDay):
        return str(subject.directory)
    if isinstance(subject, (Workspace, Item)):
        workspace_id = (
            subject.id if isinstance(subject, Workspace) else subject.workspace_id
        )
        if isinstance(subject, Item) and subject.scan is None and subject.raw:
            found = catalog.listing(subject.kind)
            return str(found.snapshot.directory) if found else None
        view = catalog.scan_of(workspace_id) if workspace_id else None
        if view is not None:
            return str(view.ref.snapshot.directory)
        found = catalog.listing(subject.kind)
        return str(found.snapshot.directory) if found else None
    return None


def subject_data(subject: Subject) -> Any:
    """What the JSON tab shows for a subject."""
    if isinstance(subject, Workspace):
        return subject.raw
    if isinstance(subject, Item):
        return subject.data
    if isinstance(subject, EventDay):
        return subject.manifest
    return subject.data


# -- the tabs of the detail pane ---------------------------------------------------------

#: The tabs of the detail pane, in order.
TABS = ("info", "users", "lineage", "json", "versions", "details")

USER_COLUMNS = ("Name", "E-mail or id", "Access", "Type")
VERSION_COLUMNS = ("Fetched", "", "Operation", "Rows", "Size", "By profile")
DETAIL_COLUMNS = ("Detail", "State", "Fetched", "From, or how to get it")


@dataclass
class Detail:
    """What one tab of the detail pane shows.

    :param body: text or a renderable (tabs that are not a table)
    :param note: a line above a table
    :param columns: the columns of a table
    :param rows: the rows of a table (``None``: the tab is not a table)
    """

    body: Optional[RenderableType] = None
    note: str = ""
    columns: Tuple[str, ...] = ()
    rows: Optional[Sequence[Tuple[str, ...]]] = None


def _hint(text: str) -> Detail:
    return Detail(body=Text(text, style="grey62"))


def join_names(names: Sequence[str]) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    if len(names) < 2:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def details_rows(
    catalog: Catalog, details: Sequence[ItemDetail], fetching: Fetching
) -> List[Tuple[str, str, str, str]]:
    """The rows of the Details tab: each detail, what the lake holds of it, and how to get
    what it lacks."""
    rows = []
    for item in details:
        held = item.held
        if held is not None:
            count = "1 row" if held.rows == 1 else f"{held.rows} rows"
            rows.append(
                (
                    item.title,
                    count,
                    ago(catalog, held.fetched_at),
                    held.provider.endpoint.id,
                )
            )
        else:
            state = "refused" if item.problems else "missing"
            rows.append((item.title, state, "-", fetching.how(item) or "-"))
    return rows


def details_note(
    catalog: Catalog, details: Sequence[ItemDetail], fetching: Fetching
) -> str:
    """The lines above the Details tab: how many the lake holds, what ``f`` would fetch, and
    why a request was refused."""
    held = sum(1 for item in details if item.held is not None)
    lines = [f"The lake holds {held} of {len(details)} details of this."]
    wanted = fetching.wanted(list(details))
    if wanted and not fetching.view_only:
        names = join_names([item.title.lower() for item, _ in wanted])
        lines.append(
            f"Press f to fetch the {names}: it asks first, and shows the cost."
        )
    for item in details:
        if item.held is None and item.problems:
            lines.append(f"{item.title}: {item.problems[0]}")
    return "\n".join(lines)


def detail(
    catalog: Catalog,
    tab: str,
    subject: Optional[Subject],
    fetching: Optional[Fetching] = None,
) -> Detail:
    """What a tab shows for a subject.

    :param catalog: the lake
    :param tab: one of `TABS`
    :param subject: what is selected, or ``None``
    :param fetching: what the session can fetch (default: anything, nothing is said about
        view only)
    """
    fetching = fetching or Fetching()
    if subject is None:
        return _hint("Nothing is selected.")
    if tab == "info":
        return Detail(body=info(catalog, subject))
    if tab == "json":
        return Detail(
            body=json_renderable(subject_data(subject), where_stored(subject, catalog))
        )
    if tab == "users":
        if not isinstance(subject, (Workspace, Item)):
            return _hint(
                "Users are listed for workspaces, reports and the other items."
            )
        view = catalog.users(subject)
        note = users_note(catalog, view)
        if view.missing:
            users = next(
                (d for d in catalog.details(subject) if d.name == "users"), None
            )
            how = fetching.how(users) if users is not None else ""
            if how:
                note += f"\n{how[0].upper()}{how[1:]}."
        return Detail(note=note, columns=USER_COLUMNS, rows=users_rows(view))
    if tab == "details":
        if not isinstance(subject, (Workspace, Item)):
            return _hint(
                "Details are fetched for a workspace or an item, one at a time."
            )
        found = catalog.details(subject)
        if not found:
            return _hint(
                f"There is nothing more to fetch for this {label(subject.kind).lower()}."
            )
        return Detail(
            note=details_note(catalog, found, fetching),
            columns=DETAIL_COLUMNS,
            rows=details_rows(catalog, found, fetching),
        )
    if tab == "lineage":
        if isinstance(subject, Item):
            return Detail(body=lineage_renderable(subject, catalog.lineage(subject)))
        return _hint(
            "Pick a report, dataset, dashboard or dataflow in the table to see how it is "
            "connected."
        )
    if tab == "versions":
        if isinstance(subject, Workspace):
            found = catalog.versions("workspace", subject.id)
        elif isinstance(subject, Item):
            found = catalog.versions(subject.kind, subject.workspace_id)
        elif isinstance(subject, Plain) and subject.kind == "capacity":
            found = catalog.versions("capacity")
        else:
            return _hint("Versions are kept of the lists and the scans.")
        return Detail(
            note=f"{len(found)} stored answer(s) hold this, newest first.",
            columns=VERSION_COLUMNS,
            rows=versions_rows(catalog, found),
        )
    raise ValueError(f"unknown tab {tab!r}")


# -- the header ------------------------------------------------------------------------


def token_text(expires_at: Optional[datetime], now: datetime, signed_in: bool) -> Text:
    """The token indicator of the header: time left, in a colour that says how urgent."""
    if not signed_in:
        return Text("not signed in", style="bold red")
    if expires_at is None:
        return Text("token without expiry", style="grey62")
    left = expires_at - now
    if left <= timedelta(0):
        return Text(f"token expired {format_age(-left)} ago", style="bold red")
    style = "bold yellow" if left < timedelta(minutes=10) else "green"
    return Text(f"token {format_age(left)}", style=style)


# -- the sync screen -------------------------------------------------------------------


def _count(value: Optional[int]) -> str:
    return "?" if value is None else str(value)


def plan_rows(plan: Plan) -> List[Tuple[str, str, str, str, str, str]]:
    """The rows of the plan table."""
    return [
        (
            item.target.name + (" *" if item.implied else ""),
            item.target.endpoint,
            _count(item.units),
            _count(item.fresh if item.units is not None else None),
            _count(item.todo),
            _count(item.requests),
        )
        for item in plan.targets
    ]


def quota_rows(plan: Plan) -> List[Tuple[str, str, str, str, bool]]:
    """The rows of the quota table: operation, needed, quota, left now, fits."""
    return [
        (
            line.endpoint,
            str(line.requests),
            line.quota or "-",
            "-" if line.left is None else str(line.left),
            line.fits,
        )
        for line in plan.quota
    ]


def plan_notes(plan: Plan) -> List[str]:
    """What else to know about a plan: notes of the targets, and what does not fit."""
    notes = []
    if any(item.implied for item in plan.targets):
        notes.append("* only here because another target needs its rows")
    for item in plan.targets:
        notes.extend(f"{item.target.name}: {note}" for note in item.notes)
    if not plan.quota:
        notes.append("Nothing to fetch: the lake holds everything fresh.")
    for line in plan.quota:
        if not line.fits:
            later = f", about {line.hours} more hour(s)" if line.hours else ""
            notes.append(
                f"{line.endpoint} needs {line.requests} requests and {line.left} fit now "
                f"({line.quota}): the rest is held back{later}; run again to continue."
            )
    if plan.quota and all(line.fits for line in plan.quota):
        notes.append("Everything fits the quota now.")
    return notes


def sequence_rows(sequence: SequencePlan) -> List[Tuple[str, ...]]:
    """The rows of the plan table of a plan file: the number of the step comes first, on the
    first row of each step."""
    rows: List[Tuple[str, ...]] = []
    for number, (_, made) in enumerate(sequence.steps, 1):
        for index, row in enumerate(plan_rows(made)):
            rows.append((str(number) if index == 0 else "", *row))
    return rows


def sequence_quota_rows(
    sequence: SequencePlan,
) -> List[Tuple[str, str, str, str, bool]]:
    """The rows of the quota table of a plan file: what all its steps need together."""
    return quota_rows(Plan("", [], sequence.quota))


def sequence_notes(sequence: SequencePlan) -> List[str]:
    """What else to know about the plan of a plan file: the steps, the names that match no
    workspace, the notes of the steps, and how it fits the quotas."""
    notes = [
        f"step {number}: {step.title}"
        for number, (step, _) in enumerate(sequence.steps, 1)
    ]
    notes.extend(f"{where}: {problem}" for where, problem in sequence.unmatched)
    notes.extend(sequence.notes)
    if any(item.implied for _, made in sequence.steps for item in made.targets):
        notes.append("* only here because another target needs its rows")
    for number, (_, made) in enumerate(sequence.steps, 1):
        notes.extend(
            f"step {number}, {item.target.name}: {note}"
            for item in made.targets
            for note in item.notes
        )
    if not sequence.steps:
        notes.append("No step is planned yet.")
    elif not sequence.quota:
        notes.append("Nothing to fetch: the lake holds everything fresh.")
    for line in sequence.quota:
        if not line.fits:
            later = f", about {line.hours} more hour(s)" if line.hours else ""
            notes.append(
                f"{line.endpoint} needs {line.requests} requests and {line.left} fit now "
                f"({line.quota}): the rest is held back{later}; run again to continue."
            )
    if sequence.quota and all(line.fits for line in sequence.quota):
        notes.append("Everything fits the quota now.")
    return notes


def short_path(path: Path) -> str:
    """A folder as short as it can be: ``~`` for the home folder."""
    try:
        inside = path.resolve().relative_to(Path.home().resolve()).as_posix()
    except ValueError:  # not inside the home folder
        return str(path)
    return "~" if inside == "." else f"~/{inside}"


def plan_file_summary(plan: PlanFile) -> Text:
    """What a plan file says, for the left of the Sync screen: the accounts, the tenant, each
    entry for the workspaces, and the settings of the session."""
    text = Text()
    text.append("Plan file\n", style="bold")
    text.append(f"{plan.name}\n", style="cyan")
    if plan.path is not None:
        text.append(f"{short_path(plan.path.parent)}\n", style="grey62")
    admin = plan.accounts.admin or "the active profile"
    users = ", ".join(plan.accounts.user) or "the active profile"
    text.append("\nAccounts\n", style="bold")
    text.append(f"  administrator: {admin}\n  users: {users}\n")
    if plan.tenant is not None:
        tenant = plan.tenant
        text.append("\nTenant\n", style="bold")
        text.append("  " + (", ".join(tenant.targets) or "the plain sync"))
        if tenant.activity_days:
            text.append(f"; {tenant.activity_days} days of events")
        text.append("\n")
    if plan.workspaces:
        text.append("\nWorkspaces\n", style="bold")
    for entry in plan.workspaces:
        what = []
        if entry.scan is not None:
            what.append(f"scan ({describe_scan(entry.scan)})")
        what.extend(entry.details)
        text.append(f"  {entry.label}", style="cyan")
        text.append(f"\n    {', '.join(what)}; via {entry.via}\n")
    session = plan.session
    text.append("\nSession\n", style="bold")
    text.append(f"  lake: {session.lake or 'the work lake'}\n")
    if session.open:
        text.append(f"  open: {session.open}\n")
    text.append(f"  lazy: {session.lazy}\n")
    text.append("\nPress l to read the file again.", style="grey62")
    return text
