"""What each tab of the Explorer gives to `pbi_cli.tui.exporting`.

The Users tab gives the people with access (with the workspace of each, for several
workspaces), the Scan tab the place each table reads its data from, the JSON tab the JSON
the lake holds, and so on: the same facts as the tab shows, but as rows with a name for each
column, and with nothing in the cells that show ``-``. Nothing here needs Textual.
"""

from typing import Any, List, Optional, Sequence, Tuple

from rich.text import Text

from pbi_cli.core.catalog import Catalog, Item, Workspace, label, scalar_fields
from pbi_cli.core.scanmodel import DatasetModel
from pbi_cli.core.store import EventDay
from pbi_cli.tui import render, scanview
from pbi_cli.tui.exporting import Exportable, rows_of
from pbi_cli.tui.fetching import Fetching
from pbi_cli.tui.render import Entry, Subject, WorkspaceSet

USER_COLUMNS = ("Name", "E-mail or id", "Access", "Type")
USER_SET_COLUMNS = USER_COLUMNS + ("Workspace", "Workspace id")
FIELD_COLUMNS = ("Field", "Value")
LINEAGE_COLUMNS = ("Direction", "Level", "Kind", "Name", "Workspace", "Note", "Id")
VERSION_COLUMNS = ("Fetched", "Age", "Operation", "Rows", "Size", "By profile")


def _what(subject: Any) -> str:
    """What a subject is, for a title: ``report Sales``, ``12 workspaces``."""
    if isinstance(subject, WorkspaceSet):
        count = len(subject.ids)
        return f"{count} workspace{'' if count == 1 else 's'}"
    if isinstance(subject, EventDay):
        return f"audit events of {subject.day.isoformat()}"
    if isinstance(subject, (Workspace, Item)):
        return f"{label(subject.kind).lower()} {subject.name}"
    return str(getattr(subject, "title", "") or "the lake")


def _plain(value: Any) -> str:
    return value.plain if isinstance(value, Text) else str(value)


def _users(catalog: Catalog, subject: Subject) -> Optional[Exportable]:
    if isinstance(subject, WorkspaceSet):
        entries, _ = catalog.workspace_users(subject.ids[: render.MAX_SET])
        rows = [
            (
                e.access.name,
                e.access.email,
                e.access.role,
                e.access.type,
                e.workspace,
                e.workspace_id,
            )
            for e in entries
        ]
        columns = USER_SET_COLUMNS
    elif isinstance(subject, (Workspace, Item)):
        view = catalog.users(subject)
        rows = [(a.name, a.email, a.role, a.type) for a in view.rows]
        columns = USER_COLUMNS
    else:
        return None
    if not rows:
        return None
    return Exportable(f"Users of {_what(subject)}", columns, rows_of(rows))


def _info(catalog: Catalog, subject: Subject) -> Optional[Exportable]:
    if isinstance(subject, Workspace):
        rows = list(scalar_fields(subject.raw) or [("id", subject.id)])
        rows += render.contents_rows(catalog, subject.id)
        rows += render.provenance(catalog, subject)
    elif isinstance(subject, WorkspaceSet):
        rows = render.set_info_rows(catalog, subject)
    elif isinstance(subject, Item):
        rows = list(scalar_fields(subject.data)) + render.provenance(catalog, subject)
    elif isinstance(subject, EventDay):
        rows = render.event_day_rows(catalog, subject)
    else:
        rows = list(scalar_fields(subject.data))
    if not rows:
        return None
    return Exportable(
        f"Info of {_what(subject)}",
        FIELD_COLUMNS,
        rows_of([(key, _plain(value)) for key, value in rows]),
    )


def _lineage(catalog: Catalog, subject: Subject) -> Optional[Exportable]:
    if not isinstance(subject, Item):
        return None
    found = catalog.lineage(subject)
    rows: List[Tuple[str, ...]] = []

    def walk(direction: str, nodes: Sequence[Any], level: int) -> None:
        for node in nodes:
            rows.append(
                (
                    direction,
                    str(level),
                    label(node.kind).lower(),
                    node.name,
                    node.workspace,
                    node.note,
                    node.id,
                )
            )
            walk(direction, node.children, level + 1)

    walk("built from", found.upstream, 1)
    walk("built on it", found.downstream, 1)
    if not rows:
        return None
    return Exportable(f"Lineage of {_what(subject)}", LINEAGE_COLUMNS, tuple(rows))


def _sources(catalog: Catalog, subject: Subject) -> Optional[Exportable]:
    """Where each table of a scan reads its data from."""
    datasets: List[Tuple[str, DatasetModel]] = []
    if isinstance(subject, Workspace):
        model = catalog.scan_model(subject.id)
        datasets = [(model.name, d) for d in model.datasets] if model else []
    elif isinstance(subject, WorkspaceSet):
        for workspace_id in subject.ids[: render.MAX_SET]:
            model = catalog.scan_model(workspace_id)
            if model is not None:
                datasets += [(model.name, d) for d in model.datasets]
    elif isinstance(subject, Item) and subject.workspace_id:
        model = catalog.scan_model(subject.workspace_id)
        where = model.name if model else ""
        if subject.kind == "dataset":
            dataset = catalog.dataset_model(subject)
            datasets = [(where, dataset)] if dataset is not None else []
        elif subject.kind == "report" and model is not None and subject.scan:
            built = model.dataset(str(subject.scan.get("datasetId") or ""))
            datasets = [(where, built)] if built is not None else []
    rows = scanview.source_rows(datasets)
    if not rows:
        return None
    return Exportable(
        f"Sources of {_what(subject)}", scanview.SOURCE_COLUMNS, rows_of(rows)
    )


def exportable(
    catalog: Catalog,
    tab: str,
    subject: Optional[Subject],
    fetching: Optional[Fetching] = None,
) -> Optional[Exportable]:
    """What a tab gives for a subject, or ``None`` when it shows nothing that can leave (a
    hint, an empty list, the people that the lake does not hold).

    :param tab: one of `pbi_cli.tui.render.TABS`
    """
    if subject is None:
        return None
    if tab == "users":
        return _users(catalog, subject)
    if tab == "info":
        return _info(catalog, subject)
    if tab == "lineage":
        return _lineage(catalog, subject)
    if tab == "scan":
        return _sources(catalog, subject)
    if tab == "json":
        data = render.subject_data(subject)
        return Exportable(f"JSON of {_what(subject)}", data=data) if data else None
    if tab in ("versions", "details"):
        shown = render.detail(catalog, tab, subject, fetching or Fetching())
        if not shown.rows:
            return None
        columns = VERSION_COLUMNS if tab == "versions" else shown.columns
        return Exportable(
            f"{tab.capitalize()} of {_what(subject)}", columns, rows_of(shown.rows)
        )
    raise ValueError(f"unknown tab {tab!r}")


def entries(
    title: str, columns: Sequence[str], rows: Sequence[Entry]
) -> Optional[Exportable]:
    """The table of the Explorer as it is shown (after the filter), with the freshness dots
    in words.

    :param title: what the table lists
    :param columns: its columns; the one without a name is the dots
    """
    if not rows:
        return None
    headers = tuple(name or "Freshness" for name in columns)
    shown = [
        tuple(render.dot_words(cell) or _plain(cell) for cell in entry.cells)
        for entry in rows
    ]
    return Exportable(title, headers, rows_of(shown))
