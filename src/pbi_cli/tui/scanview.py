"""The Scan tab: what a scan says about a workspace, and where each table gets its data.

A scan (``pbi workspaces scan``, the ``scan`` target of ``pbi sync``) is the only operation
that looks inside a workspace: the tables, columns and measures of its datasets, the queries
that load the tables, and the data sources that are configured. This module puts what the
lake holds of it in front of the reader, as renderables made from `pbi_cli.core.scanmodel`:

- a workspace: how much is in it, where its data comes from (read out of the queries, see
  `pbi_cli.core.sources`), the data sources the scan lists, and a line for each dataset;
- a dataset: each table with what loads it, the places the tables read from, the native
  queries, the parameters, the measures;
- a report, a dashboard, a dataflow: what it is built on;
- several workspaces: the same, summed.

Everything is a `rich.text.Text` or a table of them, so that a name such as ``[DEV] Sales`` is
shown as it is.
"""

import re
from typing import Dict, List, Optional, Sequence, Set, Tuple

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from pbi_cli.core.catalog import Catalog, Item, Workspace
from pbi_cli.core.planfile import describe_scan
from pbi_cli.core.scan import ScanFlags
from pbi_cli.core.scanmodel import DatasetModel, Declared, Place, WorkspaceModel
from pbi_cli.core.sources import Source
from pbi_cli.core.timefmt import format_age

#: Rows of a section shown at most; the JSON tab has all of it.
MAX_TABLES = 200
MAX_MEASURES = 100
MAX_DATASETS = 200

#: Width of what is cut from an expression for a line.
EXPRESSION_SHOWN = 100


def one_line(text: str, width: int = EXPRESSION_SHOWN) -> str:
    """A text on one line, cut when it is longer than ``width``."""
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _grid(*headers: str) -> Table:
    table = Table(
        box=None, pad_edge=False, show_header=True, header_style="bold grey62"
    )
    for header in headers:
        table.add_column(header, overflow="fold")
    return table


def _row(table: Table, *cells: object) -> None:
    table.add_row(*[c if isinstance(c, Text) else Text(str(c)) for c in cells])


def _heading(text: str) -> Text:
    return Text(text, style="bold")


def _plural(count: int, word: str, many: str = "") -> str:
    return f"{count} {word if count == 1 else many or word + 's'}"


def _more(shown: int, total: int, what: str) -> Optional[Text]:
    if total <= shown:
        return None
    return Text(
        f"… and {total - shown} more {what}: the JSON tab has all of it", style="grey62"
    )


def _source_cells(source: Source) -> Tuple[Text, Text]:
    """The kind of a source and where it is, for a table."""
    where = Text(source.where)
    if source.unresolved:
        where.append(f"  (not known: {', '.join(source.unresolved)})", style="yellow")
    return Text(source.kind, style="cyan"), where


def _declared(declared: Declared) -> Tuple[Text, Text]:
    return Text(declared.type, style="cyan"), Text(declared.where)


def missing_options(flags: ScanFlags, has_datasets: bool, planned: bool) -> List[Text]:
    """What the scan lacks because of the options it was made with, and how to get it."""
    notes: List[str] = []
    if has_datasets and not flags.dataset_schema:
        notes.append(
            "The scan was made without the dataset schema: the tables, columns and "
            "measures of the datasets are not in it."
        )
    if has_datasets and not flags.dataset_expressions:
        notes.append(
            "The scan was made without the dataset expressions: the queries that load "
            "the tables are not in it, so where their data comes from cannot be read."
        )
    if has_datasets and not flags.datasource_details:
        notes.append(
            "The scan was made without the datasource details: the servers, databases and "
            "paths of the configured data sources are not in it."
        )
    if not notes:
        return []
    how = (
        "Scan again with those options: in the plan file `scan: [dataset_schema, "
        "dataset_expressions, datasource_details]` on its entry"
        if planned
        else "Scan again with those options (the Sync screen has them as options)"
    )
    return [Text(note, style="yellow") for note in notes] + [
        Text(
            how + "; the tenant setting for detailed metadata has to be on.",
            style="yellow",
        )
    ]


def _title(name: str, subtitle: str) -> Text:
    text = Text()
    text.append(name or "(no name)", style="bold")
    if subtitle:
        text.append("   " + subtitle, style="grey62")
    return text


def _when(catalog: Catalog, model: WorkspaceModel) -> str:
    age = catalog.age(model.as_of)
    return "scan" if age is None else f"scan as of {format_age(age)} ago"


def _places_table(places: Sequence[Place]) -> Table:
    """The places that tables read from, each with how many datasets and tables read it."""
    table = _grid("Kind", "Place", "Datasets", "Tables")
    for place in places:
        kind, where = _source_cells(place.source)
        datasets = {t.split(": ", 1)[0] for t in place.tables}
        _row(table, kind, where, len(datasets), len(place.tables))
    return table


def _sources_of(dataset: DatasetModel) -> str:
    """The places a dataset reads from, in a few words."""
    places = dataset.places()
    shown = [f"{p.source.kind} {p.source.where}".strip() for p in places[:2]]
    if len(places) > 2:
        shown.append(f"+{len(places) - 2} more")
    return "; ".join(shown) or "-"


def _declared_table(models: Sequence[DatasetModel]) -> Optional[Table]:
    """The data sources that the scan lists for the datasets, each with who uses it."""
    used: Dict[Tuple[str, str], List[str]] = {}
    shown: Dict[Tuple[str, str], Declared] = {}
    for model in models:
        for declared in model.declared:
            key = (declared.type, declared.where)
            shown.setdefault(key, declared)
            used.setdefault(key, [])
            if model.name not in used[key]:
                used[key].append(model.name)
    if not used:
        return None
    table = _grid("Type", "Place", "Used by")
    for key, declared in shown.items():
        kind, where = _declared(declared)
        _row(
            table,
            kind,
            where,
            ", ".join(used[key][:3]) + (" …" if len(used[key]) > 3 else ""),
        )
    return table


def workspace_body(
    catalog: Catalog, model: WorkspaceModel, planned: bool = False
) -> RenderableType:
    """The Scan tab of a workspace."""
    parts: List[RenderableType] = [
        _title(model.name, _when(catalog, model)),
        Text(f"options: {describe_scan(model.flags)}", style="grey62"),
        Text(),
        Text(
            " · ".join(
                [
                    _plural(len(model.reports), "report"),
                    _plural(len(model.datasets), "dataset"),
                    _plural(len(model.dashboards), "dashboard"),
                    _plural(len(model.dataflows), "dataflow"),
                    _plural(model.tables, "table"),
                    _plural(model.measures, "measure"),
                ]
            )
        ),
    ]
    notes = missing_options(model.flags, bool(model.datasets), planned)
    if notes:
        parts.append(Text())
        parts.extend(notes)
    places = model.places()
    if places:
        parts += [Text(), _heading("Where the data comes from (read from the queries)")]
        parts.append(_places_table(places))
    declared = _declared_table(model.datasets)
    if declared is not None:
        parts += [Text(), _heading("Data sources the scan lists"), declared]
    if model.datasets:
        parts += [Text(), _heading("Datasets")]
        table = _grid("Name", "Tables", "Columns", "Measures", "Reads from")
        for dataset in model.datasets[:MAX_DATASETS]:
            _row(
                table,
                dataset.name,
                len(dataset.tables) if dataset.has_schema else "-",
                dataset.columns if dataset.has_schema else "-",
                dataset.measures if dataset.has_schema else "-",
                _sources_of(dataset),
            )
        parts.append(table)
        more = _more(MAX_DATASETS, len(model.datasets), "datasets")
        if more is not None:
            parts.append(more)
    if model.dataflows:
        parts += [Text(), _heading("Dataflows")]
        table = _grid("Name", "Reads from")
        for flow in model.dataflows:
            _row(
                table, flow.name, "; ".join(d.describe() for d in flow.declared) or "-"
            )
        parts.append(table)
    return Group(*parts)


def dataset_body(
    catalog: Catalog,
    dataset: DatasetModel,
    workspace: str,
    model: Optional[WorkspaceModel],
    planned: bool = False,
) -> RenderableType:
    """The Scan tab of a dataset: its tables, where they read from, its measures."""
    subtitle = f"dataset · in {workspace}" if workspace else "dataset"
    if model is not None:
        subtitle += f" · {_when(catalog, model)}"
    facts = [
        f"configured by {dataset.configured_by}" if dataset.configured_by else "",
        dataset.storage_mode,
        _plural(len(dataset.tables), "table"),
        _plural(dataset.columns, "column"),
        _plural(dataset.measures, "measure"),
    ]
    parts: List[RenderableType] = [
        _title(dataset.name, subtitle),
        Text(" · ".join(f for f in facts if f), style="grey62"),
    ]
    if model is not None and not (dataset.has_schema and dataset.has_expressions):
        parts += [Text()] + missing_options(model.flags, True, planned)
    if dataset.tables:
        parts += [Text(), _heading("Tables")]
        table = _grid("Name", "Columns", "Measures", "Loaded by", "Reads from")
        for entry in dataset.tables[:MAX_TABLES]:
            reads = Text()
            for position, source in enumerate(entry.sources):
                if position:
                    reads.append("\n")
                kind, where = _source_cells(source)
                reads.append_text(kind)
                reads.append("  ")
                reads.append_text(where)
                if source.item:
                    reads.append(f"  {source.item}", style="bold")
            if not entry.sources and entry.language == "DAX":
                reads = Text("calculated table (DAX)", style="grey62")
            elif not entry.sources and entry.language == "M":
                reads = Text(
                    "no source this reading knows (see the JSON tab)", style="grey62"
                )
            elif not entry.expression:
                reads = Text("-", style="grey62")
            name = Text(entry.name + (" (hidden)" if entry.hidden else ""))
            _row(
                table,
                name,
                len(entry.columns),
                len(entry.measures),
                entry.language or "-",
                reads,
            )
        parts.append(table)
        more = _more(MAX_TABLES, len(dataset.tables), "tables")
        if more is not None:
            parts.append(more)
    places = dataset.places()
    if places:
        parts += [Text(), _heading("Where the data comes from")]
        table = _grid("Kind", "Place", "Tables")
        for place in places:
            kind, where = _source_cells(place.source)
            _row(
                table,
                kind,
                where,
                ", ".join(t.split(": ", 1)[-1] for t in place.tables),
            )
        parts.append(table)
    native = [(t.name, s) for t in dataset.tables for s in t.sources if s.query]
    if native:
        parts += [Text(), _heading("Native queries")]
        table = _grid("Table", "Query", "Reads")
        for name, source in native:
            _row(table, name, one_line(source.query), ", ".join(source.tables) or "-")
        parts.append(table)
    declared = _declared_table([dataset])
    if declared is not None:
        parts += [Text(), _heading("Data sources the scan lists for it"), declared]
    if dataset.shared:
        parts += [Text(), _heading("Parameters and shared queries")]
        table = _grid("Name", "Is", "Value or query")
        for query in dataset.shared:
            _row(
                table,
                query.name,
                "parameter" if query.parameter else "query",
                query.value or one_line(query.expression),
            )
        parts.append(table)
    measures = [(t.name, m) for t in dataset.tables for m in t.measures]
    if measures:
        parts += [Text(), _heading("Measures")]
        table = _grid("Table", "Measure", "Expression")
        for name, measure in measures[:MAX_MEASURES]:
            _row(
                table,
                name,
                measure.name + (" (hidden)" if measure.hidden else ""),
                one_line(measure.expression) or "-",
            )
        parts.append(table)
        more = _more(MAX_MEASURES, len(measures), "measures")
        if more is not None:
            parts.append(more)
    return Group(*parts)


def item_body(
    catalog: Catalog, item: Item, planned: bool = False
) -> Optional[RenderableType]:
    """The Scan tab of an item, or ``None`` when no scan of its workspace has it."""
    if not item.scan:
        return None
    model = catalog.scan_model(item.workspace_id or "")
    workspace = catalog.workspace(item.workspace_id or "")
    where = workspace.name if workspace else ""
    if item.kind == "dataset":
        dataset = catalog.dataset_model(item)
        if dataset is None:
            return None
        return dataset_body(catalog, dataset, where, model, planned)
    parts: List[RenderableType] = [
        _title(item.name, f"{item.kind} · in {where}" if where else item.kind)
    ]
    if model is not None:
        parts.append(Text(_when(catalog, model), style="grey62"))
    if item.kind == "report":
        dataset_id = str(item.scan.get("datasetId") or "")
        built = model.dataset(dataset_id) if model is not None else None
        parts.append(Text())
        if built is not None:
            parts.append(Text(f"Built on the dataset {built.name}:"))
            parts.append(dataset_body(catalog, built, where, model, planned))
        elif dataset_id:
            parts.append(
                Text(
                    f"Built on the dataset {dataset_id}, which this scan does not hold "
                    "(it is in another workspace).",
                    style="grey62",
                )
            )
    elif item.kind == "dashboard":
        tiles = [t for t in item.scan.get("tiles") or [] if isinstance(t, dict)]
        parts += [Text(), _heading(f"Tiles ({len(tiles)})")]
        table = _grid("Title", "Report", "Dataset")
        for tile in tiles:
            _row(
                table,
                tile.get("title") or "-",
                tile.get("reportId") or "-",
                tile.get("datasetId") or "-",
            )
        parts.append(table)
    elif item.kind == "dataflow" and model is not None:
        flow = next((f for f in model.dataflows if f.id == item.id), None)
        parts.append(Text())
        if flow is not None and flow.declared:
            parts.append(_heading("Reads from"))
            table = _grid("Type", "Place")
            for declared in flow.declared:
                kind, place = _declared(declared)
                _row(table, kind, place)
            parts.append(table)
        else:
            parts.append(
                Text(
                    "The scan lists no data source for it (it needs the datasource "
                    "details option).",
                    style="grey62",
                )
            )
    return Group(*parts)


def set_body(
    catalog: Catalog,
    workspace_ids: Sequence[str],
    planned: bool = False,
    total: Optional[int] = None,
) -> RenderableType:
    """The Scan tab for several workspaces at once: what the scans say, summed.

    :param workspace_ids: the workspaces that are combined
    :param planned: whether the session has a plan file
    :param total: how many workspaces the table lists, when more than these are combined
    """
    models: List[WorkspaceModel] = []
    unscanned: List[str] = []
    # the workspaces of one stored scan together, so that each result is read once
    for workspace_id in sorted(workspace_ids, key=catalog.scan_batch):
        model = catalog.scan_model(workspace_id)
        if model is not None:
            models.append(model)
        else:
            workspace = catalog.workspace(workspace_id)
            unscanned.append(workspace.name if workspace else workspace_id)
    models.sort(key=lambda m: m.name.casefold())
    unscanned.sort(key=str.casefold)
    parts: List[RenderableType] = [
        _title(
            f"{_plural(len(workspace_ids), 'workspace')}",
            f"{len(models)} scanned, {len(unscanned)} not",
        )
    ]
    if total is not None and total > len(workspace_ids):
        parts.append(
            Text(
                f"Only the first {len(workspace_ids)} of the {total} workspaces are "
                "combined: narrow the table with /.",
                style="yellow",
            )
        )
    if not models:
        parts += [
            Text(),
            Text(
                "No scan of these workspaces is in the lake. "
                + (
                    "Put a `scan:` on their entries in the plan file and run the plan."
                    if planned
                    else "Select one and press r to scan it."
                ),
                style="yellow",
            ),
        ]
        return Group(*parts)
    parts.append(
        Text(
            " · ".join(
                [
                    _plural(sum(len(m.datasets) for m in models), "dataset"),
                    _plural(sum(m.tables for m in models), "table"),
                    _plural(sum(m.measures for m in models), "measure"),
                ]
            )
        )
    )
    flags = [m.flags for m in models]
    notes = missing_options(
        ScanFlags(
            dataset_schema=all(f.dataset_schema for f in flags),
            dataset_expressions=all(f.dataset_expressions for f in flags),
            datasource_details=all(f.datasource_details for f in flags),
        ),
        any(m.datasets for m in models),
        planned,
    )
    if notes:
        parts += [Text()] + notes
    merged: Dict[Tuple[str, str, str, str], Place] = {}
    owners: Dict[Tuple[str, str, str, str], Set[str]] = {}
    for model in models:
        for place in model.places():
            have = merged.setdefault(place.source.key, Place(place.source))
            have.tables.extend(place.tables)
            owners.setdefault(place.source.key, set()).add(model.name)
    if merged:
        parts += [Text(), _heading("Where the data comes from (read from the queries)")]
        table = _grid("Kind", "Place", "Workspaces", "Datasets", "Tables")
        for key, place in sorted(
            merged.items(), key=lambda kv: (-len(kv[1].tables), kv[0])
        ):
            kind, where = _source_cells(place.source)
            datasets = {t.split(": ", 1)[0] for t in place.tables}
            _row(table, kind, where, len(owners[key]), len(datasets), len(place.tables))
        parts.append(table)
    parts += [Text(), _heading("Workspaces")]
    table = _grid("Name", "Scan", "Datasets", "Tables", "Measures", "Places")
    for model in models:
        _row(
            table,
            model.name,
            _when(catalog, model).replace("scan as of ", ""),
            len(model.datasets),
            model.tables,
            model.measures,
            len(model.places()),
        )
    parts.append(table)
    if unscanned:
        shown = ", ".join(unscanned[:5]) + (
            f" and {len(unscanned) - 5} more" if len(unscanned) > 5 else ""
        )
        parts += [Text(), Text(f"Not scanned: {shown}", style="grey62")]
    return Group(*parts)


def workspace_hint(workspace: Optional[Workspace], planned: bool) -> str:
    """What to say when the lake holds no scan of a workspace, and how to get one."""
    name = workspace.name if workspace else "this workspace"
    if planned:
        return (
            f"No scan of {name} is in the lake. Give its entry in the plan file a "
            "`scan:` (with `get_artifact_users`, `dataset_schema`, `dataset_expressions` "
            "and `datasource_details` for the inside of its datasets) and run the plan, or "
            "press r to scan it now."
        )
    return (
        f"No scan of {name} is in the lake. Press r to scan it, or run `pbi sync run scan` "
        "with the options you want (`--dataset-schema --dataset-expressions "
        "--datasource-details`)."
    )
