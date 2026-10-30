"""What a scan says about the inside of a workspace, as things rather than as JSON.

A scan result (`pbi_cli.core.scan`) holds, for each workspace, its reports, datasets,
dashboards and dataflows. Given the options ``datasetSchema`` and ``datasetExpressions`` it
holds the inside of each dataset too: its tables with their columns and measures, the
expression (Power Query, or DAX for a calculated table) that loads each table, and the shared
queries and parameters. With ``datasourceDetails`` it lists the data sources that were
configured, and says which dataset uses which.

This module turns that into small typed objects, and reads the sources out of the queries
(`pbi_cli.core.sources`), so that the terminal UI, or a script, can ask "where does this table
come from" without walking the JSON:

```python
model = dataset_model(scan_row, scan_result["datasourceInstances"])
for table in model.tables:
    print(table.name, [s.describe() for s in table.sources])
```

Nothing here reads the lake or calls the API: it works on the rows it is given, and a row that
lacks a part (a scan made without the options) gives an object without it, with a flag that
says so.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pbi_cli.core.scan import ScanFlags
from pbi_cli.core.sources import Source, extract_sources, language_of, parameter_values

#: The fields of the connection details of a data source that say where it is, in the order
#: they are shown.
_CONNECTION_KEYS = ("server", "database", "url", "path", "kind")


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _dicts(value: Any) -> List[Dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


@dataclass(frozen=True)
class Column:
    """A column of a table.

    :param name: its name
    :param data_type: ``Int64``, ``String``, ...
    :param kind: ``Data``, ``Calculated``, ...
    :param hidden: whether it is hidden from the people who use the report
    :param expression: the DAX of a calculated column
    """

    name: str
    data_type: str = ""
    kind: str = ""
    hidden: bool = False
    expression: str = ""


@dataclass(frozen=True)
class Measure:
    """A measure of a table.

    :param name: its name
    :param expression: its DAX
    :param description: what the author says it is
    :param hidden: whether it is hidden
    """

    name: str
    expression: str = ""
    description: str = ""
    hidden: bool = False


@dataclass
class Table:
    """A table of a dataset.

    :param name: its name
    :param hidden: whether it is hidden
    :param columns: its columns
    :param measures: its measures
    :param expression: what loads it: Power Query (M), or DAX for a calculated table; empty
        when the scan was made without the expressions
    :param language: ``M``, ``DAX``, or empty
    :param sources: where its query reads from
    """

    name: str
    hidden: bool = False
    columns: List[Column] = field(default_factory=list)
    measures: List[Measure] = field(default_factory=list)
    expression: str = ""
    language: str = ""
    sources: List[Source] = field(default_factory=list)


@dataclass(frozen=True)
class SharedQuery:
    """A shared query or a parameter of a dataset.

    :param name: its name
    :param expression: its M
    :param description: what the author says it is
    :param value: the text it stands for, when it is a text (a parameter often is)
    :param parameter: whether it is a parameter (``IsParameterQuery=true``)
    """

    name: str
    expression: str = ""
    description: str = ""
    value: str = ""
    parameter: bool = False


@dataclass(frozen=True)
class Declared:
    """A data source that was configured, as the scan lists it.

    :param id: its id (``datasourceId``)
    :param type: ``Sql``, ``File``, ``Web``, ...
    :param details: where it is: the server and database, the path, the URL
    """

    id: str
    type: str
    details: Tuple[Tuple[str, str], ...] = ()

    @property
    def where(self) -> str:
        """The place in a few words: ``server / database``, a path or a URL."""
        values = [v for k, v in self.details if k in ("server", "database")]
        if values:
            return " / ".join(values)
        return "  ".join(v for _, v in self.details)

    def describe(self) -> str:
        return "  ".join(part for part in (self.type, self.where) if part)


def declared_source(instance: Mapping[str, Any]) -> Declared:
    """A data source instance of a scan result as an object."""
    details = instance.get("connectionDetails")
    details = details if isinstance(details, dict) else {}
    shown: List[Tuple[str, str]] = [
        (key, _text(details[key])) for key in _CONNECTION_KEYS if details.get(key)
    ]
    if not shown:
        shown = [
            (key, _text(value))
            for key, value in details.items()
            if isinstance(value, (str, int)) and value
        ]
    return Declared(
        _text(instance.get("datasourceId")),
        _text(instance.get("datasourceType")),
        tuple(shown),
    )


@dataclass
class Place:
    """A place that some tables read from, with the tables that do.

    :param source: the place (and one of the items taken from it)
    :param tables: the tables that read from it, as ``dataset: table``
    """

    source: Source
    tables: List[str] = field(default_factory=list)


@dataclass
class DatasetModel:
    """A dataset as the scan has it.

    :param id: its id
    :param name: its name
    :param configured_by: who configured it
    :param storage_mode: ``Import``, ``DirectQuery``, ...
    :param tables: its tables (empty when the scan has no schema)
    :param shared: its shared queries and parameters
    :param declared: the data sources the scan says it uses
    :param has_schema: whether the scan carries the tables
    :param has_expressions: whether it carries the queries that load them
    """

    id: str
    name: str
    configured_by: str = ""
    storage_mode: str = ""
    tables: List[Table] = field(default_factory=list)
    shared: List[SharedQuery] = field(default_factory=list)
    declared: List[Declared] = field(default_factory=list)
    has_schema: bool = False
    has_expressions: bool = False

    @property
    def columns(self) -> int:
        return sum(len(t.columns) for t in self.tables)

    @property
    def measures(self) -> int:
        return sum(len(t.measures) for t in self.tables)

    @property
    def sources(self) -> List[Source]:
        """Every source that a query of the dataset reads from, each once."""
        found: List[Source] = []
        for table in self.tables:
            for source in table.sources:
                if source not in found:
                    found.append(source)
        return found

    def places(self) -> List[Place]:
        """The places the tables read from, each with the tables that read from it."""
        found: Dict[Tuple[str, str, str, str], Place] = {}
        for table in self.tables:
            for source in table.sources:
                place = found.setdefault(source.key, Place(source))
                label = f"{self.name}: {table.name}"
                if label not in place.tables:
                    place.tables.append(label)
        return list(found.values())


@dataclass(frozen=True)
class ReportModel:
    """A report: ``dataset_id`` is the dataset it is built on."""

    id: str
    name: str
    dataset_id: str = ""


@dataclass(frozen=True)
class DashboardModel:
    """A dashboard, with how many tiles it has."""

    id: str
    name: str
    tiles: int = 0


@dataclass
class DataflowModel:
    """A dataflow, with the data sources the scan says it uses."""

    id: str
    name: str
    declared: List[Declared] = field(default_factory=list)


@dataclass
class WorkspaceModel:
    """What a scan says about one workspace.

    :param id: the workspace
    :param name: its name
    :param as_of: how recent the scan is
    :param flags: what the scan included
    """

    id: str
    name: str
    as_of: Optional[datetime]
    flags: ScanFlags
    reports: List[ReportModel] = field(default_factory=list)
    datasets: List[DatasetModel] = field(default_factory=list)
    dashboards: List[DashboardModel] = field(default_factory=list)
    dataflows: List[DataflowModel] = field(default_factory=list)

    @property
    def tables(self) -> int:
        return sum(len(d.tables) for d in self.datasets)

    @property
    def measures(self) -> int:
        return sum(d.measures for d in self.datasets)

    def places(self) -> List[Place]:
        """The places the tables of the workspace read from, each with the tables."""
        found: Dict[Tuple[str, str, str, str], Place] = {}
        for dataset in self.datasets:
            for place in dataset.places():
                have = found.setdefault(place.source.key, Place(place.source))
                have.tables.extend(t for t in place.tables if t not in have.tables)
        return list(found.values())

    def dataset(self, dataset_id: str) -> Optional[DatasetModel]:
        return next((d for d in self.datasets if d.id == dataset_id), None)


def _table(row: Mapping[str, Any], parameters: Mapping[str, str]) -> Table:
    expression = ""
    for part in _dicts(row.get("source")):
        expression = _text(part.get("expression")) or expression
    return Table(
        name=_text(row.get("name")),
        hidden=bool(row.get("isHidden")),
        columns=[
            Column(
                _text(c.get("name")),
                _text(c.get("dataType")),
                _text(c.get("columnType")),
                bool(c.get("isHidden")),
                _text(c.get("expression")),
            )
            for c in _dicts(row.get("columns"))
        ],
        measures=[
            Measure(
                _text(m.get("name")),
                _text(m.get("expression")),
                _text(m.get("description")),
                bool(m.get("isHidden")),
            )
            for m in _dicts(row.get("measures"))
        ],
        expression=expression,
        language=language_of(expression),
        sources=extract_sources(expression, parameters) if expression else [],
    )


def _shared(row: Mapping[str, Any]) -> List[SharedQuery]:
    queries = _dicts(row.get("expressions"))
    values = parameter_values(queries)
    return [
        SharedQuery(
            name=_text(q.get("name")),
            expression=_text(q.get("expression")),
            description=_text(q.get("description")),
            value=values.get(_text(q.get("name")), ""),
            parameter="IsParameterQuery" in _text(q.get("expression")),
        )
        for q in queries
    ]


def dataset_model(
    row: Mapping[str, Any], instances: Sequence[Mapping[str, Any]] = ()
) -> DatasetModel:
    """A dataset of a scan as an object.

    :param row: the dataset as the scan has it
    :param instances: the data source instances of the scan result, to name the ones the
        dataset uses
    """
    shared = _shared(row)
    parameters = {q.name: q.value for q in shared if q.value}
    tables = [_table(t, parameters) for t in _dicts(row.get("tables"))]
    used = {
        _text(u.get("datasourceInstanceId"))
        for u in _dicts(row.get("datasourceUsages"))
    }
    return DatasetModel(
        id=_text(row.get("id")),
        name=_text(row.get("name")),
        configured_by=_text(row.get("configuredBy")),
        storage_mode=_text(row.get("targetStorageMode")),
        tables=tables,
        shared=shared,
        declared=[
            declared_source(i)
            for i in instances
            if _text(i.get("datasourceId")) in used
        ],
        has_schema="tables" in row,
        has_expressions=any(t.expression for t in tables) or "expressions" in row,
    )


def workspace_model(
    workspace: Mapping[str, Any],
    instances: Sequence[Mapping[str, Any]],
    as_of: Optional[datetime],
    flags: ScanFlags,
) -> WorkspaceModel:
    """A workspace of a scan result as an object.

    :param workspace: the workspace as the scan has it
    :param instances: the data source instances of the scan result
    :param as_of: how recent the scan is
    :param flags: what the scan included
    """
    used = {_text(i.get("datasourceId")): declared_source(i) for i in instances}
    return WorkspaceModel(
        id=_text(workspace.get("id")),
        name=_text(workspace.get("name")),
        as_of=as_of,
        flags=flags,
        reports=[
            ReportModel(
                _text(r.get("id")), _text(r.get("name")), _text(r.get("datasetId"))
            )
            for r in _dicts(workspace.get("reports"))
        ],
        datasets=[
            dataset_model(d, instances) for d in _dicts(workspace.get("datasets"))
        ],
        dashboards=[
            DashboardModel(
                _text(d.get("id")),
                _text(d.get("displayName") or d.get("name")),
                len(_dicts(d.get("tiles"))),
            )
            for d in _dicts(workspace.get("dashboards"))
        ],
        dataflows=[
            DataflowModel(
                _text(f.get("objectId") or f.get("id")),
                _text(f.get("name")),
                [
                    used[i]
                    for i in (
                        _text(u.get("datasourceInstanceId"))
                        for u in _dicts(f.get("datasourceUsages"))
                    )
                    if i in used
                ],
            )
            for f in _dicts(workspace.get("dataflows"))
        ],
    )
