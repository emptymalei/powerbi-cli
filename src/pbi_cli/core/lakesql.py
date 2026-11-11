"""Load a data lake into a DuckDB database, to question it with SQL.

The lake keeps Power BI's answers as they came: one folder per request, nested JSON inside.
This turns the newest answer of each request into tables with the ids that join them (a
report's ``dataset_id`` is a dataset's ``id``, and so on), so that a question such as "which
reports are built on datasets that read this server, and who can open them" is one query.

```python
from pbi_cli.core.lakesql import load_lake

con = load_lake(LakeStore("~/pbi/lake"), "lake.duckdb")
con.sql("select kind, count(*) from access group by all").show()
```

DuckDB is an optional dependency (``pip install "pbi-cli[duckdb]"``). The database is a copy:
load again to see what a later sync fetched. Only a lake in a folder can be loaded (copy a
lake from S3 to a folder first). Nothing here calls the API.
"""

import json
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from loguru import logger

from pbi_cli.core.scan import RESULT_ENDPOINT
from pbi_cli.core.store import LakeStore
from pbi_cli.errors import PBIError

#: A table of the lists of the tenant, and the operation whose answer it is.
LISTS = {
    "workspaces": "admin.groups",
    "reports": "admin.reports",
    "datasets": "admin.datasets",
    "dashboards": "admin.dashboards",
    "dataflows": "admin.dataflows",
    "apps": "admin.apps",
    "capacities": "admin.capacities",
}

#: The operations that list the people of one item: the kind of the item, and the parameter
#: that holds its id.
USER_OPERATIONS: Dict[str, Tuple[str, str]] = {
    "admin.reports.users": ("report", "reportId"),
    "admin.datasets.users": ("dataset", "datasetId"),
    "admin.dashboards.users": ("dashboard", "dashboardId"),
    "admin.dataflows.users": ("dataflow", "dataflowId"),
    "admin.apps.users": ("app", "appId"),
    "admin.groups.users": ("workspace", "groupId"),
    "user.group_users": ("workspace", "groupId"),
    "user.dataset_users": ("dataset", "datasetId"),
}

#: A list fetched with one of these holds only some of the rows.
PARTIAL = ("$filter", "$top", "$skip")

ACTIVITY = "admin.activityevents"

_SCAN_KINDS = (
    ("report", "reports", "id", "name"),
    ("dataset", "datasets", "id", "name"),
    ("dashboard", "dashboards", "id", "displayName"),
    ("dataflow", "dataflows", "objectId", "name"),
)

#: The columns of the tables that are made from the nested JSON, in ``read_json`` terms.
SCHEMAS: Dict[str, Dict[str, str]] = {
    "requests": {
        "tenant": "VARCHAR",
        "endpoint": "VARCHAR",
        "params_hash": "VARCHAR",
        "params": "VARCHAR",
        "fetched_at": "TIMESTAMPTZ",
        "rows": "BIGINT",
        "path": "VARCHAR",
    },
    "access": {
        "tenant": "VARCHAR",
        "kind": "VARCHAR",
        "item_id": "VARCHAR",
        "workspace_id": "VARCHAR",
        "name": "VARCHAR",
        "email": "VARCHAR",
        "access": "VARCHAR",
        "type": "VARCHAR",
        "source": "VARCHAR",
        "fetched_at": "TIMESTAMPTZ",
    },
    "scan_items": {
        "tenant": "VARCHAR",
        "workspace_id": "VARCHAR",
        "kind": "VARCHAR",
        "id": "VARCHAR",
        "name": "VARCHAR",
        "dataset_id": "VARCHAR",
        "scanned_at": "TIMESTAMPTZ",
    },
    "scan_edges": {
        "tenant": "VARCHAR",
        "from_kind": "VARCHAR",
        "from_id": "VARCHAR",
        "to_kind": "VARCHAR",
        "to_id": "VARCHAR",
        "to_workspace_id": "VARCHAR",
        "relation": "VARCHAR",
    },
    "scan_datasources": {
        "tenant": "VARCHAR",
        "instance_id": "VARCHAR",
        "type": "VARCHAR",
        "connection": "VARCHAR",
        "server": "VARCHAR",
        "database": "VARCHAR",
        "path": "VARCHAR",
        "url": "VARCHAR",
    },
    "scan_tables": {
        "tenant": "VARCHAR",
        "workspace_id": "VARCHAR",
        "dataset_id": "VARCHAR",
        "table_name": "VARCHAR",
        "is_hidden": "BOOLEAN",
        "columns": "BIGINT",
        "measures": "BIGINT",
        "source": "VARCHAR",
    },
    "scan_columns": {
        "tenant": "VARCHAR",
        "dataset_id": "VARCHAR",
        "table_name": "VARCHAR",
        "column_name": "VARCHAR",
        "data_type": "VARCHAR",
        "column_type": "VARCHAR",
        "expression": "VARCHAR",
    },
    "scan_measures": {
        "tenant": "VARCHAR",
        "dataset_id": "VARCHAR",
        "table_name": "VARCHAR",
        "measure_name": "VARCHAR",
        "expression": "VARCHAR",
    },
}


def _duckdb() -> Any:
    try:
        import duckdb
    except ImportError:
        raise PBIError(
            "DuckDB is not installed. Install it with: pip install 'pbi-cli[duckdb]'"
        ) from None
    return duckdb


def _text(value: Any) -> Optional[str]:
    return None if value is None or value == "" else str(value)


def _dicts(value: Any) -> List[Dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        logger.warning(f"Skipping unreadable {path}: {error}")
        return None


class _Rows:
    """Rows of one table, written to a temporary file that DuckDB reads."""

    def __init__(self, folder: Path, name: str):
        self.name = name
        self.path = folder / f"{name}.jsonl"
        self.count = 0
        self._file = self.path.open("w", encoding="utf-8")

    def add(self, **row: Any) -> None:
        self._file.write(json.dumps(row, default=str) + "\n")
        self.count += 1

    def close(self) -> None:
        self._file.close()


def newest_requests(store: LakeStore) -> Iterator[Dict[str, Any]]:
    """The newest answer of every request in the lake, as a row of ``requests``."""
    for tenant in store.tenants():
        for endpoint in store.endpoints(tenant):
            for stored in store.parameter_sets(tenant, endpoint):
                snapshot = stored.latest
                yield {
                    "tenant": tenant,
                    "endpoint": endpoint,
                    "params_hash": stored.hash,
                    "params": json.dumps(stored.params, sort_keys=True),
                    "fetched_at": snapshot.fetched_at.isoformat(),
                    "rows": snapshot.manifest.get("rows"),
                    "path": str(
                        snapshot.directory
                        / snapshot.manifest.get("data_file", "data.json")
                    ),
                }


def _person(row: Dict[str, Any]) -> Dict[str, Optional[str]]:
    rights = [str(v) for k, v in row.items() if k.endswith("AccessRight") and v]
    return {
        "name": _text(row.get("displayName") or row.get("identifier")),
        "email": _text(row.get("emailAddress") or row.get("identifier")),
        "access": rights[0] if rights else None,
        "type": _text(row.get("principalType")),
    }


def _access_rows(
    requests: Sequence[Dict[str, Any]], scans: Iterable[Tuple[str, str, Dict[str, Any]]]
) -> Iterator[Dict[str, Any]]:
    """Everybody with access to something, from the user operations and from the scans."""
    for request in requests:
        operation = USER_OPERATIONS.get(request["endpoint"])
        if operation is None:
            continue
        kind, key = operation
        params = json.loads(request["params"])
        data = _read(Path(request["path"]))
        rows = _dicts(data.get("value")) if isinstance(data, dict) else []
        for row in rows:
            yield {
                "tenant": request["tenant"],
                "kind": kind,
                "item_id": params.get(key),
                "workspace_id": params.get("groupId"),
                "source": request["endpoint"],
                "fetched_at": request["fetched_at"],
                **_person(row),
            }
    for tenant, scanned_at, workspace in scans:
        found = [("workspace", workspace.get("id"), workspace)]
        for kind, key, id_field, _ in _SCAN_KINDS:
            found += [(kind, i.get(id_field), i) for i in _dicts(workspace.get(key))]
        for kind, item_id, item in found:
            for row in _dicts(item.get("users")):
                yield {
                    "tenant": tenant,
                    "kind": kind,
                    "item_id": _text(item_id),
                    "workspace_id": _text(workspace.get("id")),
                    "source": "scan",
                    "fetched_at": scanned_at,
                    **_person(row),
                }


def _newest_scans(
    requests: Sequence[Dict[str, Any]],
) -> Tuple[List[Tuple[str, str, Dict[str, Any]]], Dict[str, Dict[str, Any]]]:
    """The newest scan of each workspace, and the data source instances of the scans.

    :return: ``(tenant, when, workspace)`` for each workspace, and the instances by
        ``tenant/id``
    """
    newest: Dict[Tuple[str, str], Tuple[str, Dict[str, Any]]] = {}
    instances: Dict[str, Dict[str, Any]] = {}
    scans = sorted(
        (r for r in requests if r["endpoint"] == RESULT_ENDPOINT),
        key=lambda r: r["fetched_at"],
    )
    for request in scans:
        data = _read(Path(request["path"]))
        if not isinstance(data, dict):
            continue
        for workspace in _dicts(data.get("workspaces")):
            if workspace.get("id"):
                newest[(request["tenant"], str(workspace["id"]))] = (
                    request["fetched_at"],
                    workspace,
                )
        for instance in _dicts(data.get("datasourceInstances")):
            if instance.get("datasourceId"):
                key = f"{request['tenant']}/{instance['datasourceId']}"
                instances[key] = {**instance, "_tenant": request["tenant"]}
    found = [(tenant, when, w) for (tenant, _), (when, w) in sorted(newest.items())]
    return found, instances


def _scan_tables(
    scans: Sequence[Tuple[str, str, Dict[str, Any]]],
    instances: Dict[str, Dict[str, Any]],
    out: Dict[str, _Rows],
) -> None:
    for tenant, when, workspace in scans:
        workspace_id = _text(workspace.get("id"))
        for kind, key, id_field, name_field in _SCAN_KINDS:
            for item in _dicts(workspace.get(key)):
                item_id = _text(item.get(id_field))
                out["scan_items"].add(
                    tenant=tenant,
                    workspace_id=workspace_id,
                    kind=kind,
                    id=item_id,
                    name=_text(item.get(name_field)),
                    dataset_id=_text(item.get("datasetId")),
                    scanned_at=when,
                )
                _edges(tenant, kind, item_id, item, out["scan_edges"])
                if kind == "dataset":
                    _model(tenant, workspace_id, item_id, item, out)
    for key, instance in sorted(instances.items()):
        details = instance.get("connectionDetails") or {}
        out["scan_datasources"].add(
            tenant=instance["_tenant"],
            instance_id=_text(instance.get("datasourceId")),
            type=_text(instance.get("datasourceType")),
            connection=json.dumps(details, sort_keys=True) if details else None,
            server=_text(details.get("server")),
            database=_text(details.get("database")),
            path=_text(details.get("path")),
            url=_text(details.get("url")),
        )


def _edges(
    tenant: str, kind: str, item_id: Optional[str], item: Dict[str, Any], out: _Rows
) -> None:
    def edge(to_kind: str, to_id: Any, relation: str, workspace: Any = None) -> None:
        if to_id:
            out.add(
                tenant=tenant,
                from_kind=kind,
                from_id=item_id,
                to_kind=to_kind,
                to_id=str(to_id),
                to_workspace_id=_text(workspace),
                relation=relation,
            )

    if kind == "report":
        edge("dataset", item.get("datasetId"), "built on")
    if kind == "dashboard":
        for tile in _dicts(item.get("tiles")):
            edge("report", tile.get("reportId"), "shows")
            edge("dataset", tile.get("datasetId"), "shows")
    if kind in ("dataset", "dataflow"):
        for up in _dicts(item.get("upstreamDataflows")):
            edge("dataflow", up.get("targetDataflowId"), "built on", up.get("groupId"))
        for up in _dicts(item.get("upstreamDatasets")):
            edge("dataset", up.get("targetDatasetId"), "built on", up.get("groupId"))
    if kind == "dataset":
        for usage in _dicts(item.get("datasourceUsages")):
            edge("datasource", usage.get("datasourceInstanceId"), "reads from")


def _model(
    tenant: str,
    workspace_id: Optional[str],
    dataset_id: Optional[str],
    dataset: Dict[str, Any],
    out: Dict[str, _Rows],
) -> None:
    for table in _dicts(dataset.get("tables")):
        name = _text(table.get("name"))
        columns, measures = _dicts(table.get("columns")), _dicts(table.get("measures"))
        sources = _dicts(table.get("source"))
        out["scan_tables"].add(
            tenant=tenant,
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            table_name=name,
            is_hidden=bool(table.get("isHidden")),
            columns=len(columns),
            measures=len(measures),
            source=_text(sources[0].get("expression")) if sources else None,
        )
        for column in columns:
            out["scan_columns"].add(
                tenant=tenant,
                dataset_id=dataset_id,
                table_name=name,
                column_name=_text(column.get("name")),
                data_type=_text(column.get("dataType")),
                column_type=_text(column.get("columnType")),
                expression=_text(column.get("expression")),
            )
        for measure in measures:
            out["scan_measures"].add(
                tenant=tenant,
                dataset_id=dataset_id,
                table_name=name,
                measure_name=_text(measure.get("name")),
                expression=_text(measure.get("expression")),
            )


def _create(con: Any, name: str, rows: _Rows) -> None:
    columns = ", ".join(f"'{c}': '{t}'" for c, t in SCHEMAS[name].items())
    rows.close()
    con.execute(
        f"CREATE TABLE {name} AS SELECT * FROM read_json(?, format='newline_delimited', "
        f"columns={{{columns}}})",
        [str(rows.path)],
    )


def _lists(con: Any) -> None:
    """The lists of the tenant: the newest complete list of each, with its columns as they
    came (a nested ``users`` or ``reports`` stays a nested column)."""
    for table, endpoint in LISTS.items():
        paths = con.execute(
            """
            SELECT path FROM (
                SELECT path, row_number() OVER (
                    PARTITION BY tenant ORDER BY fetched_at DESC) AS n
                FROM requests
                WHERE endpoint = ? AND NOT regexp_matches(params, '\\$(filter|top|skip)')
            ) WHERE n = 1
            """,
            [endpoint],
        ).fetchall()
        if not paths:
            continue
        con.execute(
            f"""
            CREATE TABLE {table} AS
            SELECT r.tenant, r.fetched_at, v.* FROM (
                SELECT j.filename AS path, unnest(j.value) AS v
                FROM read_json(?, filename=true, union_by_name=true) j
            ) x JOIN requests r ON r.path = x.path
            """,
            [[p[0] for p in paths]],
        )


def _events(con: Any, store: LakeStore, folder: Path) -> None:
    files = folder / "event_files.jsonl"
    count = 0
    with files.open("w", encoding="utf-8") as out:
        for tenant in store.tenants():
            for day in store.event_days(tenant, ACTIVITY):
                for part in day.manifest.get("parts", []):
                    out.write(
                        json.dumps(
                            {
                                "path": str(day.directory / part),
                                "tenant": tenant,
                                "day": day.day.isoformat(),
                            }
                        )
                        + "\n"
                    )
                    count += 1
    if not count:
        return
    con.execute(
        "CREATE TABLE event_files AS SELECT * FROM read_json(?, format='newline_delimited', "
        "columns={'path': 'VARCHAR', 'tenant': 'VARCHAR', 'day': 'DATE'})",
        [str(files)],
    )
    con.execute(
        """
        CREATE TABLE events AS
        SELECT f.tenant, f.day, j.* EXCLUDE (filename)
        FROM read_json(?, format='newline_delimited', filename=true,
                       union_by_name=true) j
        JOIN event_files f ON f.path = j.filename
        """,
        [[row[0] for row in con.execute("SELECT path FROM event_files").fetchall()]],
    )


_VIEWS = """
CREATE VIEW report_overview AS
SELECT r.tenant, r.id AS report_id, r.name AS report, w.id AS workspace_id,
       w.name AS workspace, d.id AS dataset_id, d.name AS dataset
FROM reports r
LEFT JOIN workspaces w ON w.tenant = r.tenant AND w.id = r.workspaceId
LEFT JOIN datasets d ON d.tenant = r.tenant AND d.id = r.datasetId
"""


def load_lake(
    store: LakeStore, database: Optional[str] = None, tenant: Optional[str] = None
) -> Any:
    """Load the newest answer of every request of a lake into a DuckDB database.

    :param store: the lake (in a folder)
    :param database: the file of the database (default: in memory); an existing file is
        replaced
    :param tenant: only this tenant (default: all of them)
    :return: the DuckDB connection, with the tables described in ``docs/lake.md``
    :raises PBIError: when DuckDB is not installed or the lake is not in a folder
    """
    duckdb = _duckdb()
    if store.root.__class__.__module__.startswith("cloudpathlib"):
        raise PBIError(
            "Only a lake in a folder can be loaded into DuckDB: copy it from "
            f"{store.root} to a folder first (for example with `aws s3 sync`)."
        )
    if database and database != ":memory:" and Path(database).exists():
        Path(database).unlink()
    con = duckdb.connect(database or ":memory:")
    requests = [
        r for r in newest_requests(store) if tenant is None or r["tenant"] == tenant
    ]
    with tempfile.TemporaryDirectory() as temp:
        folder = Path(temp)
        out = {name: _Rows(folder, name) for name in SCHEMAS}
        for request in requests:
            out["requests"].add(**request)
        _create(con, "requests", out["requests"])
        _lists(con)

        scans, instances = _newest_scans(requests)
        _scan_tables(scans, instances, out)
        for row in _access_rows(requests, scans):
            out["access"].add(**row)
        for name in SCHEMAS:
            if name != "requests":
                _create(con, name, out[name])
        _events(con, store, folder)
    tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
    if {"reports", "workspaces", "datasets"} <= tables:
        con.execute(_VIEWS)
    return con


def describe(con: Any) -> List[Tuple[str, int]]:
    """The tables and views of a loaded lake with their row counts."""
    names = [row[0] for row in con.execute("SHOW TABLES").fetchall()]
    return [
        (name, con.execute(f'SELECT count(*) FROM "{name}"').fetchall()[0][0])
        for name in names
    ]
