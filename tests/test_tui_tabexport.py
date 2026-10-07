"""What each tab of the Explorer gives to an export: rows with names, from the same facts."""

from datetime import datetime, timezone

import pytest
from render_helpers import plain
from sync_helpers import TENANT, World

from pbi_cli.core.catalog import Access, Catalog, Item, Lineage, LineageNode, UsersView
from pbi_cli.core.scan import ScanFlags
from pbi_cli.core.scanmodel import dataset_model, workspace_model
from pbi_cli.tui import render, scanview, tabexport
from pbi_cli.tui.exporting import Exportable
from pbi_cli.tui.fetching import Fetching
from pbi_cli.tui.render import Entry, Plain, WorkspaceSet

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


@pytest.fixture
def world(tmp_path):
    world = World(tmp_path)
    world.run()
    world.run(
        "scan", scan_flags=ScanFlags.everything(), workspace_ids=("ws-0001", "ws-0002")
    )
    world.run("group-users", force=True)
    world.run("report-users", force=True)
    world.run("app-users", force=True)
    world.run("activity", days=2)
    return world


@pytest.fixture
def catalog(world):
    return Catalog(world.store, TENANT, clock=world.clock.now)


def squeezed(text):
    return " ".join(text.split())


def export(catalog, tab, subject, **kwargs):
    return tabexport.exportable(catalog, tab, subject, **kwargs)


# ---------------------------------------------------------------------------
# the people
# ---------------------------------------------------------------------------


def test_the_users_of_a_workspace_are_rows_of_their_own(catalog):
    found = export(catalog, "users", catalog.workspace("ws-0001"))

    assert found.title == "Users of workspace Workspace 1"
    assert found.columns == ("Name", "E-mail or id", "Access", "Type")
    assert found.rows == (
        ("Ann", "ann-ws-0001@example.com", "Admin", "User"),
        ("Bob", "bob-ws-0001@example.com", "Viewer", "User"),
    )


def test_the_users_of_a_report_and_of_an_app_are_named_by_what_they_are_of(catalog):
    report = export(catalog, "users", catalog.item("report", "rep-0001"))
    app = export(catalog, "users", catalog.item("app", "app-0001"))

    assert report.title == "Users of report Report 1"
    assert app.title == "Users of app App 1"
    assert [r[:3] for r in app.rows] == [
        ("Ann", "ann-app-0001@example.com", "Owner"),
        ("Bob", "bob-app-0001@example.com", "Read"),
    ]


def test_the_users_of_several_workspaces_come_with_the_workspace_of_each(catalog):
    found = export(catalog, "users", WorkspaceSet(("ws-0002", "ws-0001")))

    assert found.title == "Users of 2 workspaces"
    assert found.columns == (
        "Name",
        "E-mail or id",
        "Access",
        "Type",
        "Workspace",
        "Workspace id",
    )
    assert [r[4:] for r in found.rows] == [
        ("Workspace 1", "ws-0001"),
        ("Workspace 2", "ws-0002"),
        ("Workspace 1", "ws-0001"),
        ("Workspace 2", "ws-0002"),
    ]
    assert export(catalog, "users", WorkspaceSet(("ws-0001",))).title == (
        "Users of 1 workspace"
    )


def test_the_users_of_a_set_are_cut_where_the_tab_cuts_them(catalog):
    big = WorkspaceSet(("ws-0001",) * (render.MAX_SET + 3))

    found = export(catalog, "users", big)

    assert len(found.rows) == 2 * render.MAX_SET  # two people in each of the first 500


def test_people_that_the_lake_does_not_hold_give_nothing_to_export(catalog):
    assert export(catalog, "users", catalog.item("dataset", "ds-0001")) is None
    assert export(catalog, "users", WorkspaceSet(("nope",))) is None
    assert export(catalog, "users", Plain("capacity", "Capacity 1", {})) is None


def test_a_person_without_a_name_is_an_empty_cell_not_a_dash():
    class Stub:
        def users(self, subject):
            return UsersView(rows=[Access("", "x@example.com", "", "Group")])

    subject = Item("report", "r", "R", None, {})

    found = tabexport.exportable(Stub(), "users", subject)

    assert found.rows == (("", "x@example.com", "", "Group"),)
    assert found.cells == [("", "x@example.com", "", "Group")]


# ---------------------------------------------------------------------------
# the info
# ---------------------------------------------------------------------------


def test_the_fields_of_the_info_are_those_that_the_tab_shows(catalog):
    day = catalog.event_days()[0]
    subjects = [
        catalog.workspace("ws-0001"),
        catalog.item("report", "rep-0001"),
        catalog.item("dataset", "ds-0001"),
        WorkspaceSet(("ws-0001", "ws-0002")),
        day,
        render.lake_subject(catalog),
    ]

    for subject in subjects:
        found = export(catalog, "info", subject)
        shown = squeezed(plain(render.info(catalog, subject)))
        assert found.columns == ("Field", "Value") and found.rows
        for field, value in found.rows:
            assert squeezed(field) in shown and squeezed(value) in shown, (
                subject,
                field,
            )


def test_the_info_of_a_workspace_has_what_the_lake_holds_of_it(catalog):
    found = export(catalog, "info", catalog.workspace("ws-0001"))

    fields = dict(found.rows)
    assert found.title == "Info of workspace Workspace 1"
    assert fields["name"] == "Workspace 1" and fields["Reports"] == "1"
    assert "Scan" in fields and "List" in fields


# ---------------------------------------------------------------------------
# the lineage, the JSON, the versions and the details
# ---------------------------------------------------------------------------


def test_the_lineage_is_flattened_with_the_level_of_each_step():
    class Stub:
        def lineage(self, item):
            deep = LineageNode("datasource", "ds", "Sql: srv", note="from the scan")
            flow = LineageNode("dataflow", "df", "Flow", "Other", children=[deep])
            return Lineage(
                upstream=[LineageNode("dataset", "d", "Data", children=[flow])],
                downstream=[LineageNode("report", "r", "Rep")],
            )

    found = tabexport.exportable(
        Stub(), "lineage", Item("dataset", "d", "Data", "w", {})
    )

    assert found.title == "Lineage of dataset Data"
    assert found.columns == (
        "Direction",
        "Level",
        "Kind",
        "Name",
        "Workspace",
        "Note",
        "Id",
    )
    assert found.rows == (
        ("built from", "1", "dataset", "Data", "", "", "d"),
        ("built from", "2", "dataflow", "Flow", "Other", "", "df"),
        ("built from", "3", "data source", "Sql: srv", "", "from the scan", "ds"),
        ("built on it", "1", "report", "Rep", "", "", "r"),
    )


def test_what_is_not_connected_and_what_is_not_an_item_give_no_lineage(catalog):
    class Alone:
        def lineage(self, item):
            return Lineage()

    assert (
        tabexport.exportable(Alone(), "lineage", Item("report", "r", "R", None, {}))
        is None
    )
    assert export(catalog, "lineage", catalog.workspace("ws-0001")) is None


def test_the_json_is_the_data_that_the_lake_holds(catalog):
    found = export(catalog, "json", catalog.workspace("ws-0001"))

    assert found.title == "JSON of workspace Workspace 1" and not found.is_table
    assert found.data["id"] == "ws-0001"
    assert export(catalog, "json", Plain("capacity", "C", {})) is None


def test_the_versions_and_the_details_are_the_rows_that_the_tab_shows(catalog):
    subject = catalog.item("report", "rep-0001")

    versions = export(catalog, "versions", subject)
    details = export(catalog, "details", subject)

    shown = render.detail(catalog, "details", subject, Fetching())
    assert versions.title == "Versions of report Report 1"
    assert versions.columns == (
        "Fetched",
        "Age",
        "Operation",
        "Rows",
        "Size",
        "By profile",
    )
    assert details.columns == shown.columns and details.rows == tuple(
        tuple(r) for r in shown.rows
    )
    assert (
        details.cells[1][1] == "missing" and details.cells[1][2] == ""
    )  # the dash is blank
    assert export(catalog, "versions", WorkspaceSet(("ws-0001",))) is None


# ---------------------------------------------------------------------------
# the scan
# ---------------------------------------------------------------------------


def test_the_sources_of_a_workspace_are_a_row_for_each_place_of_each_table(catalog):
    found = export(catalog, "scan", catalog.workspace("ws-0001"))

    assert found.title == "Sources of workspace Workspace 1"
    assert found.columns == scanview.SOURCE_COLUMNS
    sales = next(r for r in found.rows if r[2] == "Sales")
    assert sales[:9] == (
        "Workspace 1",
        "Dataset 1",
        "Sales",
        "M",
        "SQL Server",
        "sql.example",
        "ds-0001",
        "",
        "dbo.Sales",
    )
    assert "Sql.Database" in sales[-1]  # the whole expression


def test_a_dataset_a_report_and_several_workspaces_give_their_sources(catalog):
    dataset = export(catalog, "scan", catalog.item("dataset", "ds-0001"))
    report = export(catalog, "scan", catalog.item("report", "rep-0001"))
    several = export(catalog, "scan", WorkspaceSet(("ws-0001", "ws-0002", "ws-0009")))

    assert dataset.title == "Sources of dataset Dataset 1"
    assert report.title == "Sources of report Report 1" and report.rows == dataset.rows
    assert several.title == "Sources of 3 workspaces"
    assert {r[0] for r in several.rows} == {"Workspace 1", "Workspace 2"}


def test_a_workspace_that_was_not_scanned_gives_no_sources(catalog):
    assert export(catalog, "scan", catalog.workspace("ws-0005")) is None
    assert export(catalog, "scan", WorkspaceSet(("ws-0005",))) is None
    assert export(catalog, "scan", Plain("capacity", "C", {})) is None


def test_a_table_that_reads_from_nothing_it_knows_has_a_row_with_empty_cells():
    model = workspace_model(
        {
            "id": "w",
            "name": "W",
            "datasets": [
                {
                    "id": "d",
                    "name": "D",
                    "tables": [
                        {"name": "Calc", "source": [{"expression": "SUM(x)"}]},
                        {
                            "name": "Two",
                            "source": [
                                {
                                    "expression": 'let A = Sql.Database("s", "d"), B = Excel.Workbook(File.Contents("a.xlsx")) in A'
                                }
                            ],
                        },
                    ],
                }
            ],
        },
        [],
        NOW,
        ScanFlags.everything(),
    )

    rows = scanview.source_rows([("W", d) for d in model.datasets])

    calc = [r for r in rows if r[2] == "Calc"]
    two = [r for r in rows if r[2] == "Two"]
    assert len(calc) == 1 and calc[0][4:12] == ("",) * 8 and calc[0][12] == "SUM(x)"
    assert [r[4] for r in two] == [
        "SQL Server",
        "Excel workbook",
    ]  # a row for each place


def test_an_expression_is_cut_to_what_a_cell_of_a_spreadsheet_holds():
    long = 'let S = Sql.Database("s", "d", [Query="' + "x" * 40000 + '"]) in S'
    dataset = dataset_model(
        {
            "id": "d",
            "name": "D",
            "tables": [{"name": "T", "source": [{"expression": long}]}],
        }
    )

    (row,) = scanview.source_rows([("W", dataset)])

    assert len(row[-1]) == scanview.CELL_LIMIT and row[-1].endswith("…")


# ---------------------------------------------------------------------------
# the table, and what is not a tab
# ---------------------------------------------------------------------------


def test_the_table_is_exported_as_it_is_shown_with_the_dots_in_words():
    fresh = Entry(
        "a",
        "workspace",
        (render.dot(render.Freshness.FRESH), "Alpha", "-"),
        Plain("x", "x", {}),
        "alpha",
    )
    never = Entry(
        "b",
        "workspace",
        (render.dot(render.Freshness.NONE), "Beta", "Workspace"),
        Plain("x", "x", {}),
        "beta",
    )

    found = tabexport.entries("Workspaces", ("", "Name", "Type"), [fresh, never])

    assert found.title == "Workspaces"
    assert found.columns == ("Freshness", "Name", "Type")
    assert found.rows == (
        ("fresh", "Alpha", "-"),
        ("not in the lake", "Beta", "Workspace"),
    )
    assert found.cells[0] == ("fresh", "Alpha", "")  # what shows as a dash is empty


def test_a_table_without_rows_gives_nothing():
    assert tabexport.entries("Workspaces", ("Name",), []) is None


def test_a_cell_that_is_not_a_dot_is_its_text():
    from rich.text import Text

    assert render.dot_words(Text("●", style="blue")) is None
    assert render.dot_words("plain") is None
    assert render.dot_words(render.dot(render.Freshness.OLD)) == "a week old or more"


def test_nothing_selected_gives_nothing_and_an_unknown_tab_is_an_error(catalog):
    assert export(catalog, "users", None) is None
    with pytest.raises(ValueError, match="unknown tab 'nope'"):
        export(catalog, "nope", catalog.workspace("ws-0001"))


def test_an_export_is_an_exportable():
    assert isinstance(
        tabexport.entries(
            "t", ("a",), [Entry("k", "x", ("v",), Plain("x", "x", {}), "v")]
        ),
        Exportable,
    )
