"""The lake as DuckDB tables: what is loaded, and that the ids join them."""

import json

import pytest
from cloudpathlib import CloudPath
from sync_helpers import TENANT, World
from typer.testing import CliRunner

from pbi_cli.cli import app  # noqa: E402
from pbi_cli.config import PBIConfig  # noqa: E402
from pbi_cli.core import lakesql  # noqa: E402
from pbi_cli.core.lakesql import SCHEMAS, describe, load_lake  # noqa: E402
from pbi_cli.core.scan import RESULT_ENDPOINT, ScanFlags  # noqa: E402
from pbi_cli.errors import PBIError  # noqa: E402

duckdb = pytest.importorskip("duckdb")


FULL = ScanFlags(
    lineage=True,
    datasource_details=True,
    dataset_schema=True,
    dataset_expressions=True,
    get_artifact_users=True,
)


@pytest.fixture
def world(tmp_path):
    world = World(tmp_path / "src")
    world.run()
    world.run("scan", scan_flags=FULL)
    world.run("activity", days=2)
    world.run("report-users", "app-users")
    world.run("groups", force=True)  # a second version of the list of workspaces
    return world


@pytest.fixture
def con(world):
    connection = load_lake(world.store)
    yield connection
    connection.close()


def rows(con, sql):
    return con.execute(sql).fetchall()


def counts(con):
    return dict(describe(con))


def test_every_list_and_every_kind_of_scan_data_is_a_table(con):
    found = counts(con)

    for table in (
        "workspaces",
        "reports",
        "datasets",
        "dashboards",
        "dataflows",
        "apps",
        "capacities",
        "access",
        "events",
        "requests",
        *SCHEMAS,
    ):
        assert table in found, table
    assert found["workspaces"] == 12 and found["reports"] == 6


def test_only_the_newest_answer_of_a_request_is_loaded(con):
    # the list of workspaces was fetched twice
    assert counts(con)["workspaces"] == 12
    assert rows(
        con, "select count(*) from requests where endpoint = 'admin.groups'"
    ) == [(1,)]


def test_the_ids_join_a_report_to_its_workspace_and_dataset(con):
    found = rows(
        con,
        "select report, workspace, dataset from report_overview where report_id = 'rep-0002'",
    )

    assert found == [("Report 2", "Workspace 2", "Dataset 2")]


def test_a_list_keeps_its_columns_as_they_came(con):
    columns = {row[0] for row in rows(con, "describe reports")}

    assert {"tenant", "fetched_at", "id", "name", "datasetId", "workspaceId"} <= columns


def test_who_has_access_comes_from_the_operations_and_from_the_scans(con):
    found = rows(
        con, "select kind, source, count(*) from access group by all order by all"
    )

    assert ("report", "admin.reports.users", 6) in found
    assert ("app", "admin.apps.users", 6) in found
    assert ("report", "scan", 6) in found and ("workspace", "scan", 12) in found


def test_the_params_of_a_request_name_the_item_whose_users_they_are(con):
    mine = rows(
        con,
        "select email, access from access where kind = 'report' and item_id = 'rep-0002' "
        "and source = 'admin.reports.users'",
    )

    assert mine == [("ann-rep-0002@example.com", "Owner")]


def test_the_lineage_of_a_scan_is_a_table_of_edges(con):
    edges = {
        row
        for row in rows(
            con,
            "select from_kind, from_id, to_kind, to_id, relation from scan_edges",
        )
    }

    assert ("report", "rep-0001", "dataset", "ds-0001", "built on") in edges
    assert ("dashboard", "dash-0001", "report", "rep-0001", "shows") in edges
    assert ("dataset", "ds-0001", "datasource", "dsi-ds-0001", "reads from") in edges
    assert ("dataset", "ds-0001", "dataflow", "flow-0001", "built on") in edges


def test_a_question_that_crosses_the_tables_is_one_query(con):
    # who can open a report that reads from the server sql.example?
    found = rows(
        con,
        """
        select distinct r.report, a.email
        from scan_datasources s
        join scan_edges e on e.to_id = s.instance_id and e.to_kind = 'datasource'
        join scan_edges b on b.to_id = e.from_id and b.from_kind = 'report'
        join report_overview r on r.report_id = b.from_id
        join access a on a.item_id = r.report_id and a.kind = 'report'
        where s.server = 'sql.example' and a.source = 'admin.reports.users'
        order by 1
        """,
    )

    assert found[0] == ("Report 1", "ann-rep-0001@example.com")
    assert len(found) == 6


def test_the_model_of_a_dataset_is_flattened(con):
    assert rows(
        con,
        "select table_name, columns, measures from scan_tables "
        "where dataset_id = 'ds-0001' order by 1",
    ) == [("Calendar", 1, 0), ("Customers", 2, 0), ("Sales", 3, 1)]
    assert rows(
        con,
        "select expression from scan_columns where column_name = 'Margin' limit 1",
    ) == [("[Total] * 0.2",)]
    assert ("Total Sales", "SUM(Sales[Total])") in rows(
        con, "select measure_name, expression from scan_measures"
    )
    assert (
        "Sql.Database"
        in rows(
            con, "select source from scan_tables where table_name = 'Sales' limit 1"
        )[0][0]
    )


def test_data_sources_have_their_connection_details(con):
    assert rows(
        con,
        "select type, server, database from scan_datasources "
        "where instance_id = 'dsi-ds-0001'",
    ) == [("Sql", "sql.example", "ds-0001")]


def test_the_events_come_with_their_day(con):
    assert rows(
        con, "select day::varchar, count(*) from events group by all order by 1"
    ) == [
        ("2026-09-29", 5),
        ("2026-09-30", 3),
    ]


def test_the_newest_scan_of_a_workspace_wins(world, tmp_path):
    world.clock.advance(hours=2)
    world.run("scan", scan_flags=FULL, force=True)

    con = load_lake(world.store)

    assert rows(con, "select count(*) from scan_items where kind = 'report'") == [(6,)]
    assert len(rows(con, "select distinct scanned_at from scan_items")) == 1
    con.close()


def test_a_partial_list_is_not_taken_for_the_list(world):
    from pbi_cli.core.store import LakeStore

    store = LakeStore(world.store.root)
    store.write_snapshot(
        TENANT,
        "admin.groups",
        {"$top": "1"},
        {"value": [{"id": "only-one", "name": "Only one"}]},
    )

    con = load_lake(store)

    assert counts(con)["workspaces"] == 12
    con.close()


def test_a_database_file_is_made_and_replaced(world, tmp_path):
    target = tmp_path / "out" / "lake.duckdb"
    target.parent.mkdir()
    target.write_text("not a database", encoding="utf-8")

    con = load_lake(world.store, str(target))
    con.close()

    reopened = duckdb.connect(str(target), read_only=True)
    assert reopened.execute("select count(*) from reports").fetchall() == [(6,)]
    reopened.close()


def test_one_tenant_can_be_chosen(world):
    con = load_lake(world.store, tenant="tenant-that-is-not-there")

    assert counts(con)["requests"] == 0
    assert "reports" not in counts(con)
    con.close()


def test_a_lake_in_the_cloud_is_refused_with_the_way_out(local_s3):
    from pbi_cli.core.store import LakeStore

    with pytest.raises(PBIError, match="copy it"):
        load_lake(LakeStore(CloudPath("s3://bucket/lake")))


def test_without_duckdb_the_error_says_how_to_get_it(world, monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "duckdb", None)

    with pytest.raises(PBIError, match=r"pip install 'pbi-cli\[duckdb\]'"):
        load_lake(world.store)


# ---------------------------------------------------------------------------
# pbi lake duckdb
# ---------------------------------------------------------------------------


def test_the_command_writes_a_database_and_lists_its_tables(
    world, isolated_home, tmp_path
):
    PBIConfig().cache_folder = str(world.store.root.parent)
    target = tmp_path / "pbi.duckdb"

    result = CliRunner().invoke(app, ["lake", "duckdb", str(target)])

    assert result.exit_code == 0, result.output
    assert "reports" in result.output and "6 row(s)" in result.output
    assert f"Open it with: duckdb {target}" in result.output
    con = duckdb.connect(str(target), read_only=True)
    assert con.execute("select count(*) from workspaces").fetchall() == [(12,)]
    con.close()


def test_a_scanned_report_knows_its_dataset(con):
    assert rows(
        con,
        "select dataset_id, workspace_id from scan_items where id = 'rep-0001'",
    ) == [("ds-0001", "ws-0001")]


def test_the_newest_of_several_lists_is_the_list(world):
    # a second list of workspaces, asked for with other parameters, and newer
    world.clock.advance(hours=3)
    world.store.write_snapshot(
        TENANT,
        "admin.groups",
        {"$expand": "users"},
        {"value": [{"id": "ws-new", "name": "Newest"}]},
        fetched_at=world.clock.now(),
    )

    con = load_lake(world.store)

    assert rows(con, "select id from workspaces") == [("ws-new",)]
    con.close()


def test_the_newest_scan_of_a_workspace_wins_whatever_the_names_of_the_files(tmp_path):
    older, newer = tmp_path / "z-older.json", tmp_path / "a-newer.json"
    for path, name in ((older, "Old name"), (newer, "New name")):
        path.write_text(
            json.dumps({"workspaces": [{"id": "ws-1", "name": name}]}), encoding="utf-8"
        )
    requests = [
        {
            "tenant": "t",
            "endpoint": RESULT_ENDPOINT,
            "path": str(older),
            "fetched_at": "2026-10-01T10:00:00+00:00",
        },
        {
            "tenant": "t",
            "endpoint": RESULT_ENDPOINT,
            "path": str(newer),
            "fetched_at": "2026-10-02T10:00:00+00:00",
        },
    ]

    scans, _ = lakesql._newest_scans(requests)

    assert [(t, when, w["name"]) for t, when, w in scans] == [
        ("t", "2026-10-02T10:00:00+00:00", "New name")
    ]
