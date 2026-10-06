"""The Scan tab and the tabs that combine several workspaces: what they say, as text."""

from datetime import datetime, timezone

import pytest
from render_helpers import plain
from sync_helpers import TENANT, World

from pbi_cli.core import catalog as catalog_module
from pbi_cli.core.catalog import Access, Catalog, WorkspaceAccess
from pbi_cli.core.scan import ScanFlags
from pbi_cli.core.scanmodel import (
    DatasetModel,
    Declared,
    Table,
    dataset_model,
    workspace_model,
)
from pbi_cli.tui import render, scanview
from pbi_cli.tui.fetching import Fetching
from pbi_cli.tui.render import MAX_COUNTED, MAX_SET, WorkspaceSet

EVERYTHING = ScanFlags(
    lineage=True,
    datasource_details=True,
    dataset_schema=True,
    dataset_expressions=True,
    get_artifact_users=True,
)
PLANNED = Fetching(planned=True)
NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


@pytest.fixture
def world(tmp_path):
    world = World(tmp_path)
    world.run()
    world.run("scan", scan_flags=EVERYTHING, workspace_ids=("ws-0001", "ws-0002"))
    world.run("group-users", force=True)
    return world


def catalog_of(world):
    return Catalog(world.store, TENANT, clock=world.clock.now)


@pytest.fixture
def catalog(world):
    return catalog_of(world)


def scan(catalog, subject, fetching=None):
    """The text of the Scan tab for a subject."""
    found = render.detail(catalog, "scan", subject, fetching or Fetching())
    return plain(found.body)


# ---------------------------------------------------------------------------
# a workspace
# ---------------------------------------------------------------------------


def test_the_scan_of_a_workspace_says_what_is_in_it_and_where_the_data_comes_from(
    catalog,
):
    text = scan(catalog, catalog.workspace("ws-0001"))

    assert text.startswith("Workspace 1   scan as of 0 s ago\noptions: lineage, ")
    assert (
        "1 report · 1 dataset · 1 dashboard · 1 dataflow · 3 tables · 1 measure" in text
    )
    assert "Where the data comes from (read from the queries)" in text
    assert "SQL Server      sql.example / ds-0001   1         1" in text
    assert "Excel workbook  C:\\data\\customers.xlsx  1         1" in text
    assert (
        "Data sources the scan lists" in text and "Sql   sql.example / ds-0001" in text
    )
    assert (
        "Dataset 1  3       6        1         SQL Server sql.example / ds-0001" in text
    )


def test_a_workspace_without_a_scan_says_how_to_get_one(catalog):
    found = render.detail(catalog, "scan", catalog.workspace("ws-0007"), Fetching())

    assert found.body is not None
    assert plain(found.body).startswith(
        "No scan of Workspace 7 is in the lake. Press r"
    )
    assert "pbi sync run scan" in plain(found.body)


def test_with_a_plan_file_it_says_which_line_to_add(catalog):
    found = render.detail(catalog, "scan", catalog.workspace("ws-0007"), PLANNED)

    text = plain(found.body)
    assert "Give its entry in the plan file a `scan:`" in text
    assert "dataset_expressions" in text and "press r to scan it now" in text


@pytest.mark.parametrize(
    "flags, missing",
    [
        (
            ScanFlags(lineage=True),
            [
                "without the dataset schema",
                "without the dataset expressions",
                "without the datasource details",
            ],
        ),
        (
            ScanFlags(dataset_schema=True, datasource_details=True),
            ["without the dataset expressions"],
        ),
        (
            ScanFlags(dataset_schema=True, dataset_expressions=True),
            ["without the datasource details"],
        ),
    ],
)
def test_what_a_scan_lacks_because_of_its_options_is_said(world, flags, missing):
    world.clock.advance(minutes=5)
    world.run("scan", scan_flags=flags, workspace_ids=("ws-0001",), force=True)
    catalog = catalog_of(world)

    text = scan(catalog, catalog.workspace("ws-0001"))

    for words in missing:
        assert words in text
    assert "the tenant setting for detailed metadata has to be on" in text
    assert "Scan again with those options (the Sync screen has them as options)" in text


def test_a_scan_that_has_everything_lacks_nothing(catalog):
    text = scan(catalog, catalog.workspace("ws-0001"))

    assert "without the" not in text


def test_the_note_about_missing_options_names_the_line_of_the_plan_file(world):
    world.clock.advance(minutes=5)
    world.run("scan", scan_flags=ScanFlags(), workspace_ids=("ws-0001",), force=True)
    catalog = catalog_of(world)

    text = scan(catalog, catalog.workspace("ws-0001"), PLANNED)

    assert "`scan: [dataset_schema, dataset_expressions, datasource_details]`" in text


def test_a_scan_of_a_workspace_without_datasets_says_nothing_is_missing(world):
    world.fake.datasets.clear()
    world.clock.advance(minutes=5)
    world.run("scan", scan_flags=ScanFlags(), workspace_ids=("ws-0001",), force=True)
    catalog = catalog_of(world)

    text = scan(catalog, catalog.workspace("ws-0001"))

    assert "0 datasets" in text and "without the" not in text


# ---------------------------------------------------------------------------
# a dataset
# ---------------------------------------------------------------------------


def test_the_scan_of_a_dataset_lists_its_tables_and_where_each_reads_from(catalog):
    text = scan(catalog, catalog.item("dataset", "ds-0001"))

    assert text.startswith("Dataset 1   dataset · in Workspace 1 · scan as of 0 s ago")
    assert "configured by owner@example.com · 3 tables · 6 columns · 1 measure" in text
    assert (
        "Sales      3        1         M          SQL Server  sql.example / ds-0001  dbo.Sales"
        in text
    )
    assert (
        "Customers  2        0         M          Excel workbook  C:\\data\\customers.xlsx  Customers"
        in text
    )
    assert "Calendar   1        0         DAX        calculated table (DAX)" in text


def test_the_places_the_tables_read_from_are_listed_with_their_tables(catalog):
    text = scan(catalog, catalog.item("dataset", "ds-0001"))

    assert "Where the data comes from\n" in text
    assert "SQL Server      sql.example / ds-0001   Sales" in text
    assert "Excel workbook  C:\\data\\customers.xlsx  Customers" in text


def test_the_parameters_and_the_measures_are_listed_as_they_are_written(catalog):
    text = scan(catalog, catalog.item("dataset", "ds-0001"))

    assert "ServerName  parameter  sql.example" in text
    assert (
        "Sales  Total Sales  SUM(Sales[Total])" in text
    )  # DAX has brackets: shown as is


def test_the_data_sources_the_scan_lists_for_the_dataset_are_listed(catalog):
    text = scan(catalog, catalog.item("dataset", "ds-0001"))

    assert "Data sources the scan lists for it" in text
    assert "Sql   sql.example / ds-0001  Dataset 1" in text


def test_a_native_query_and_the_tables_it_reads_are_listed():
    dataset = dataset_model(
        {
            "id": "d",
            "name": "Orders model",
            "tables": [
                {
                    "name": "Orders",
                    "source": [
                        {
                            "expression": 'let S = Sql.Database("srv", "db", [Query="select * from dbo.Orders o '
                            'join dbo.Customers c on o.c = c.id"]) in S'
                        }
                    ],
                }
            ],
        }
    )

    text = plain(scanview.dataset_body(_empty_catalog(), dataset, "WS", None))

    assert "Native queries" in text
    assert (
        "Orders" in text and "select * from dbo.Orders o join dbo.Customers c" in text
    )
    assert "dbo.Orders, dbo.Customers" in text


def test_a_name_that_could_not_be_followed_is_said_next_to_the_place():
    dataset = dataset_model(
        {
            "name": "M",
            "tables": [
                {
                    "name": "T",
                    "source": [
                        {"expression": 'let S = Sql.Database(ServerName, "db") in S'}
                    ],
                }
            ],
        }
    )

    text = plain(scanview.dataset_body(_empty_catalog(), dataset, "WS", None))

    assert "SQL Server  db  (not known: ServerName)" in text


def test_a_query_with_no_source_it_knows_says_so():
    dataset = dataset_model(
        {
            "name": "M",
            "tables": [
                {
                    "name": "T",
                    "source": [{"expression": "let S = Table.Combine({A, B}) in S"}],
                },
                {"name": "U"},
            ],
        }
    )

    text = plain(scanview.dataset_body(_empty_catalog(), dataset, "WS", None))

    assert "no source this reading knows (see the JSON tab)" in text


def test_a_hidden_table_and_a_hidden_measure_say_so():
    dataset = dataset_model(
        {
            "name": "M",
            "tables": [
                {
                    "name": "Secret",
                    "isHidden": True,
                    "measures": [
                        {"name": "Hidden measure", "isHidden": True, "expression": "1"}
                    ],
                }
            ],
        }
    )

    text = plain(scanview.dataset_body(_empty_catalog(), dataset, "WS", None))

    assert "Secret (hidden)" in text and "Hidden measure (hidden)" in text


def test_a_dataset_with_many_tables_and_measures_is_cut():
    many = {
        "name": "Big",
        "tables": [
            {
                "name": f"Tbl{n:04d}",
                "measures": [{"name": f"M{n:04d}", "expression": "1"}],
            }
            for n in range(scanview.MAX_TABLES + 5)
        ],
    }

    text = plain(
        scanview.dataset_body(_empty_catalog(), dataset_model(many), "WS", None)
    )

    assert "… and 5 more tables: the JSON tab has all of it" in text
    assert "… and 105 more measures: the JSON tab has all of it" in text
    last_table = f"Tbl{scanview.MAX_TABLES - 1:04d}"
    assert last_table in text and f"Tbl{scanview.MAX_TABLES:04d}" not in text
    assert f"M{scanview.MAX_MEASURES - 1:04d}" in text
    assert f"M{scanview.MAX_MEASURES:04d}" not in text


def test_a_workspace_with_many_datasets_lists_the_first_ones():
    workspace = {
        "id": "w",
        "name": "W",
        "datasets": [
            {"id": f"d{n}", "name": f"Dataset {n:04d}"}
            for n in range(scanview.MAX_DATASETS + 3)
        ],
    }
    model = workspace_model(workspace, [], NOW, ScanFlags(dataset_schema=True))

    text = plain(scanview.workspace_body(_empty_catalog(), model))

    assert "… and 3 more datasets: the JSON tab has all of it" in text
    assert f"Dataset {scanview.MAX_DATASETS - 1:04d}" in text
    assert f"Dataset {scanview.MAX_DATASETS:04d}" not in text


def test_a_long_expression_is_cut_to_a_line():
    long = "SUM(" + "x" * 300 + ")"

    assert scanview.one_line(long, 20) == "SUM(xxxxxxxxxxxxxxx…"
    assert scanview.one_line("a\n   b\t\tc") == "a b c"


def test_a_table_without_a_query_has_a_dash_to_read_from(world):
    world.clock.advance(minutes=5)
    world.run(
        "scan",
        scan_flags=ScanFlags(dataset_schema=True),
        workspace_ids=("ws-0001",),
        force=True,
    )
    catalog = catalog_of(world)

    text = scan(catalog, catalog.item("dataset", "ds-0001"))

    assert (
        "Sales      3        1         -          -" in text
    )  # not loaded by M or DAX, reads from nothing known
    assert "without the dataset expressions" in text


def test_a_native_query_is_cut_to_a_line_in_the_list():
    dataset = dataset_model(
        {
            "name": "M",
            "tables": [
                {
                    "name": "T",
                    "source": [
                        {
                            "expression": 'let S = Sql.Database("a", "b", [Query="select '
                            + "x, " * 60
                            + 'y from t"]) in S'
                        }
                    ],
                }
            ],
        }
    )

    text = plain(scanview.dataset_body(_empty_catalog(), dataset, "WS", None))

    native = text.split("Native queries")[1]
    assert "select x, x, x" in native and "…" in native
    assert "y from t" not in native  # cut to a hundred characters
    assert native.count("x, ") <= 33  # and not folded over several lines


def test_a_dataset_of_a_scan_without_options_tells_so_in_its_own_tab(world):
    world.clock.advance(minutes=5)
    world.run("scan", scan_flags=ScanFlags(), workspace_ids=("ws-0001",), force=True)
    catalog = catalog_of(world)

    text = scan(catalog, catalog.item("dataset", "ds-0001"))

    assert "The scan was made without the dataset schema" in text
    assert "Tables" not in text  # nothing to list


# ---------------------------------------------------------------------------
# the other items
# ---------------------------------------------------------------------------


def test_a_report_says_which_dataset_it_is_built_on_and_what_that_reads(catalog):
    text = scan(catalog, catalog.item("report", "rep-0001"))

    assert text.startswith("Report 1   report · in Workspace 1\nscan as of 0 s ago")
    assert "Built on the dataset Dataset 1:" in text
    assert "SQL Server  sql.example / ds-0001  dbo.Sales" in text


def test_a_report_on_a_dataset_the_scan_does_not_hold_says_so(world):
    for report in world.fake.reports:
        if report["id"] == "rep-0001":
            report["datasetId"] = "ds-elsewhere"
    world.clock.advance(minutes=5)
    world.run("scan", scan_flags=EVERYTHING, workspace_ids=("ws-0001",), force=True)
    catalog = catalog_of(world)

    text = scan(catalog, catalog.item("report", "rep-0001"))

    assert "Built on the dataset ds-elsewhere, which this scan does not hold" in text


def test_a_dashboard_lists_its_tiles(catalog):
    text = scan(catalog, catalog.item("dashboard", "dash-0001"))

    assert text.startswith("Dashboard 1   dashboard · in Workspace 1")
    assert (
        "Tiles (1)" in text and "tile-1" not in text
    )  # a tile has no title in the fake
    assert "rep-0001" in text


def test_a_dataflow_lists_what_it_reads_or_says_the_option_is_missing(catalog):
    text = scan(catalog, catalog.item("dataflow", "flow-0001"))

    assert text.startswith("Dataflow 1   dataflow · in Workspace 1")
    assert "The scan lists no data source for it" in text


def test_an_item_that_the_scan_does_not_have_says_so(world):
    world.fake.reports.append(
        {**world.fake.reports[0], "id": "rep-new", "name": "New report"}
    )
    world.run("reports", force=True)
    catalog = catalog_of(world)

    found = render.detail(
        catalog, "scan", catalog.item("report", "rep-new"), Fetching()
    )

    assert plain(found.body) == (
        "The newest scan of Workspace 1 does not hold this report."
    )


def test_an_item_in_no_scan_says_how_to_get_one(catalog):
    found = render.detail(
        catalog, "scan", catalog.item("report", "rep-0003"), Fetching()
    )

    assert plain(found.body).startswith("No scan of Workspace 3 is in the lake.")


def test_what_is_not_a_workspace_or_an_item_has_no_scan(catalog):
    found = render.detail(catalog, "scan", render.lake_subject(catalog), Fetching())
    nothing = render.detail(catalog, "scan", None, Fetching())

    assert (
        plain(found.body) == "Select a workspace or an item to see what its scan says."
    )
    assert plain(nothing.body) == "Nothing is selected."


# ---------------------------------------------------------------------------
# several workspaces
# ---------------------------------------------------------------------------


def workspaces(catalog, *ids):
    return WorkspaceSet(tuple(ids or [w.id for w in catalog.workspaces()]))


def test_the_scan_of_several_workspaces_sums_what_they_say(catalog):
    text = scan(catalog, workspaces(catalog))

    assert text.startswith(
        "12 workspaces   2 scanned, 10 not\n2 datasets · 6 tables · 2 measures"
    )
    assert "Excel workbook  C:\\data\\customers.xlsx  2           2         2" in text
    assert "SQL Server      sql.example / ds-0001   1           1         1" in text
    assert "Workspace 1  0 s ago  1         3       1         2" in text
    assert (
        "Not scanned: Workspace 10, Workspace 11, Workspace 12, Workspace 3, Workspace 4 and 5 more"
        in text
    )


def test_several_workspaces_with_no_scan_say_how_to_get_one(world):
    catalog = catalog_of(world)

    found = render.detail(
        catalog, "scan", workspaces(catalog, "ws-0005", "ws-0006"), Fetching()
    )
    planned = render.detail(catalog, "scan", workspaces(catalog, "ws-0005"), PLANNED)

    assert plain(found.body).endswith("Select one and press r to scan it.")
    assert plain(planned.body).endswith(
        "Put a `scan:` on their entries in the plan file and run the plan."
    )
    assert plain(found.body).startswith("2 workspaces   0 scanned, 2 not")


def test_a_set_of_scans_made_with_fewer_options_says_what_is_lacking(world):
    world.clock.advance(minutes=5)
    world.run(
        "scan",
        scan_flags=ScanFlags(dataset_schema=True),
        workspace_ids=("ws-0001",),
        force=True,
    )
    catalog = catalog_of(world)

    text = scan(catalog, workspaces(catalog, "ws-0001", "ws-0002"))

    assert "without the dataset expressions" in text  # one of the two lacks them
    assert "without the dataset schema" not in text


def test_a_set_whose_first_scan_has_everything_still_says_what_another_lacks(world):
    world.clock.advance(minutes=5)
    world.run("scan", scan_flags=ScanFlags(), workspace_ids=("ws-0002",), force=True)
    catalog = catalog_of(world)

    text = scan(catalog, workspaces(catalog, "ws-0001", "ws-0002"))

    assert "without the dataset schema" in text  # the second one lacks it


def test_the_places_of_a_set_come_by_how_many_tables_read_them():
    def workspace(name, tables):
        return {
            "id": name,
            "name": name,
            "datasets": [
                {
                    "id": f"d-{name}",
                    "name": f"D {name}",
                    "tables": [
                        {
                            "name": f"T{n}",
                            "source": [{"expression": expression}],
                        }
                        for n, expression in enumerate(tables)
                    ],
                }
            ],
        }

    sql = 'let S = Sql.Database("s", "d") in S'
    excel = 'let S = Excel.Workbook(File.Contents("a.xlsx")) in S'
    models = {
        "w1": workspace_model(
            workspace("w1", [sql, sql, sql, excel]), [], NOW, ScanFlags()
        ),
    }

    class Stub:
        def scan_batch(self, workspace_id):
            return ""

        def scan_model(self, workspace_id):
            return models.get(workspace_id)

        def workspace(self, workspace_id):
            return None

        def age(self, when):
            return None

    text = plain(scanview.set_body(Stub(), ["w1"]))

    places = text.split("Where the data comes from (read from the queries)")[1]
    assert places.index("SQL Server") < places.index(
        "Excel workbook"
    )  # 3 tables before 1


def test_a_set_reads_each_stored_scan_once_even_when_its_workspaces_alternate(
    world, monkeypatch
):
    world.clock.advance(minutes=5)
    world.run("scan", scan_flags=EVERYTHING, workspace_ids=("ws-0003", "ws-0004"))
    world.run("scan", scan_flags=EVERYTHING, workspace_ids=("ws-0005", "ws-0006"))
    catalog = catalog_of(world)
    reads = []
    split = catalog_module.split_scan_result
    monkeypatch.setattr(catalog_module, "SCAN_CACHE", 1)  # one result at a time
    monkeypatch.setattr(
        catalog_module,
        "split_scan_result",
        lambda result: reads.append(1) or split(result),
    )
    alternating = ["ws-0003", "ws-0005", "ws-0004", "ws-0006"]

    text = plain(scanview.set_body(catalog, alternating))

    assert text.startswith("4 workspaces   4 scanned, 0 not")
    assert len(reads) == 2  # not four


def test_the_workspaces_of_a_set_are_listed_by_name_whatever_order_they_come_in(
    catalog,
):
    # ws-0001 and ws-0002 are scanned (together); the others are not
    ids = ["ws-0012", "ws-0002", "ws-0001", "ws-0005", "ws-0003"]

    text = plain(scanview.set_body(catalog, ids))

    listed = text.split("\nWorkspaces\n")[1]
    assert listed.index("Workspace 1") < listed.index("Workspace 2")
    assert "Not scanned: Workspace 12, Workspace 3, Workspace 5" in text


def test_the_workspaces_of_a_set_are_looked_at_up_to_a_limit(catalog):
    found = render.detail(
        catalog,
        "scan",
        WorkspaceSet(("ws-0001",) + ("nope",) * (MAX_SET + 10)),
        Fetching(),
    )

    text = plain(found.body)
    assert text.startswith(f"{MAX_SET} workspaces   1 scanned, {MAX_SET - 1} not")
    assert (
        f"Only the first {MAX_SET} of the {MAX_SET + 11} workspaces are combined"
        in text
    )


def test_the_info_of_several_workspaces_counts_what_the_lake_holds_of_them(catalog):
    text = plain(render.info(catalog, workspaces(catalog)))

    assert text.startswith("Workspaces   12 workspaces")
    assert "Scanned                        2 of 12" in text
    assert "With their people (users tab)  12 of 12" in text
    assert "Reports                        6" in text
    assert "The Users and Scan tabs combine these workspaces." in text


def test_the_info_counts_the_workspaces_whose_people_the_lake_holds(tmp_path):
    world = World(tmp_path)
    world.run("groups")
    world.run("group-users", workspace_ids=("ws-0001",))
    catalog = catalog_of(world)

    text = plain(
        render.info(catalog, workspaces(catalog, "ws-0001", "ws-0002", "ws-0003"))
    )

    assert "With their people (users tab)  1 of 3" in text
    assert "Scanned                        0 of 3" in text


def test_the_info_of_many_workspaces_does_not_ask_the_lake_whose_people_it_holds(
    catalog,
):
    edge = plain(render.info(catalog, WorkspaceSet(("ws-0001",) * MAX_COUNTED)))
    many = plain(render.info(catalog, WorkspaceSet(("ws-0001",) * (MAX_COUNTED + 1))))

    assert f"With their people (users tab)  {MAX_COUNTED} of {MAX_COUNTED}" in edge
    assert f"not counted for more than {MAX_COUNTED}: narrow the table with /" in many
    assert f"{MAX_COUNTED + 1} of {MAX_COUNTED + 1}" in many  # the scans still are


def test_the_info_of_too_many_workspaces_says_it_looked_at_the_first(catalog):
    big = WorkspaceSet(("ws-0001",) * (MAX_SET + 1))

    text = plain(render.info(catalog, big))

    assert (
        f"Only the first {MAX_SET} of the {MAX_SET + 1} workspaces are looked at"
        in text
    )


def test_the_json_of_several_workspaces_is_their_ids(catalog):
    assert render.subject_data(workspaces(catalog, "ws-0001", "ws-0002")) == {
        "workspaces": ["ws-0001", "ws-0002"]
    }
    assert render.where_stored(workspaces(catalog, "ws-0001"), catalog) is None


@pytest.mark.parametrize("tab", ["lineage", "versions", "details"])
def test_the_tabs_of_one_workspace_say_so_for_several(catalog, tab):
    found = render.detail(
        catalog, tab, workspaces(catalog, "ws-0001", "ws-0002"), Fetching()
    )

    assert found.body is not None or found.rows is not None


def test_the_users_of_several_workspaces_are_listed_with_their_workspace(catalog):
    found = render.detail(
        catalog, "users", workspaces(catalog, "ws-0001", "ws-0002"), Fetching()
    )

    assert found.columns == ("Name", "E-mail or id", "Access", "Type", "Workspace")
    assert found.note == "4 entries: 4 people with access to 2 of 2 workspaces."
    assert (
        "Ann",
        "ann-ws-0001@example.com",
        "Admin",
        "User",
        "Workspace 1",
    ) in found.rows
    assert (
        "Bob",
        "bob-ws-0002@example.com",
        "Viewer",
        "User",
        "Workspace 2",
    ) in found.rows


def test_the_users_of_a_set_are_ordered_by_person_then_workspace(world):
    catalog = catalog_of(world)

    found = render.detail(catalog, "users", workspaces(catalog), Fetching())

    names = [row[0] for row in found.rows]
    assert names == sorted(names, key=str.casefold)


def test_workspaces_whose_users_are_not_in_the_lake_are_named(tmp_path):
    world = World(tmp_path)
    world.run("groups")  # the list of workspaces without their people
    for workspace in world.fake.workspaces:
        workspace.pop("users", None)
    catalog = catalog_of(world)

    found = render.detail(
        catalog,
        "users",
        workspaces(catalog, "ws-0001", "ws-0002", "ws-0003", "ws-0004", "ws-0005"),
        Fetching(),
    )
    planned = render.detail(catalog, "users", workspaces(catalog, "ws-0001"), PLANNED)

    assert found.note.startswith(
        "0 entries: 0 people with access to 0 of 5 workspaces."
    )
    assert (
        "The lake holds no users for Workspace 1, Workspace 2, Workspace 3 and 2 more."
        in found.note
    )
    assert "Select a workspace and press f to fetch them" in found.note
    assert "Put `details: [users]`" in planned.note
    assert found.rows == []


def test_only_so_many_workspaces_are_combined_in_the_users_tab(catalog):
    big = WorkspaceSet(("ws-0001",) * (MAX_SET + 3))

    found = render.detail(catalog, "users", big, Fetching())

    assert found.note.startswith(
        f"{2 * MAX_SET} entries: 2 people with access to {MAX_SET} of {MAX_SET} workspaces."
    )
    assert (
        f"Only the first {MAX_SET} of the {MAX_SET + 3} workspaces are combined"
        in found.note
    )


class Roster:
    """A catalog that knows who has access to which workspace, and nothing else."""

    def __init__(self, entries, missing=()):
        self.entries, self.missing = entries, list(missing)

    def workspace_users(self, ids):
        return self.entries, self.missing


def access(workspace, name="Ann", email="ann@example.com"):
    person = Access(name, email, "Admin", "User")
    return WorkspaceAccess(workspace, workspace.upper(), person)


def test_a_person_with_access_to_several_workspaces_is_counted_once():
    several = Roster([access("a"), access("b"), access("b", "Bob", "bob@example.com")])

    found = render.set_users(several, WorkspaceSet(("a", "b")), Fetching())

    assert found.note == "3 entries: 2 people with access to 2 of 2 workspaces."
    assert len(found.rows) == 3


def test_the_same_name_with_another_address_is_another_person():
    others = Roster([access("a"), access("b", "Ann", "ann@elsewhere.example")])

    found = render.set_users(others, WorkspaceSet(("a", "b")), Fetching())

    assert found.note.startswith("2 entries: 2 people ")


def test_one_entry_of_one_person_in_one_workspace_is_said_in_the_singular():
    found = render.set_users(Roster([access("a")]), WorkspaceSet(("a",)), Fetching())

    assert found.note == "1 entry: 1 person with access to 1 of 1 workspace."


def test_the_catalog_lists_the_people_of_several_workspaces(catalog):
    entries, missing = catalog.workspace_users(["ws-0002", "ws-0001", "nope"])

    assert missing == []
    assert [(e.access.name, e.workspace) for e in entries] == [
        ("Ann", "Workspace 1"),
        ("Ann", "Workspace 2"),
        ("Bob", "Workspace 1"),
        ("Bob", "Workspace 2"),
    ]
    assert entries[0].workspace_id == "ws-0001"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _empty_catalog():
    """A catalog of nothing, for the renderers that only need to say how old a scan is."""

    class Nothing:
        def age(self, when):
            return None

    return Nothing()


def test_the_declared_sources_and_the_tables_are_plain_objects():
    table = Table("T")
    model = DatasetModel("d", "D", tables=[table], declared=[Declared("1", "Sql")])

    assert model.places() == [] and model.sources == []
    assert Declared("1", "Sql").describe() == "Sql"
