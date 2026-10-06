"""What a scan says about the inside of a workspace, as things."""

from datetime import datetime, timezone

import pytest
from sync_helpers import TENANT, World

from pbi_cli.core.catalog import Catalog
from pbi_cli.core.scan import ScanFlags
from pbi_cli.core.scanmodel import (
    Declared,
    dataset_model,
    declared_source,
    workspace_model,
)

EVERYTHING = ScanFlags(
    lineage=True,
    datasource_details=True,
    dataset_schema=True,
    dataset_expressions=True,
    get_artifact_users=True,
)
NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


@pytest.fixture
def world(tmp_path):
    world = World(tmp_path)
    world.run()
    world.run("scan", scan_flags=EVERYTHING)
    return world


@pytest.fixture
def catalog(world):
    return Catalog(world.store, TENANT, clock=world.clock.now)


# ---------------------------------------------------------------------------
# a workspace of a scan
# ---------------------------------------------------------------------------


def test_a_workspace_of_a_scan_has_its_items_and_the_inside_of_its_datasets(catalog):
    model = catalog.scan_model("ws-0001")

    assert model is not None and model.name == "Workspace 1"
    assert model.flags == EVERYTHING
    assert [d.name for d in model.datasets] == ["Dataset 1"]
    assert [r.name for r in model.reports] == ["Report 1"]
    assert model.reports[0].dataset_id == "ds-0001"
    assert [d.name for d in model.dashboards] == ["Dashboard 1"]
    assert model.dashboards[0].tiles == 1
    assert model.tables == 3 and model.measures == 1


def test_a_table_has_its_columns_its_measures_and_what_loads_it(catalog):
    dataset = catalog.scan_model("ws-0001").datasets[0]
    sales, customers, calendar = dataset.tables

    assert [c.name for c in sales.columns] == ["Id", "Total", "Margin"]
    margin = sales.columns[2]
    assert (margin.kind, margin.expression) == ("Calculated", "[Total] * 0.2")
    assert [(m.name, m.expression) for m in sales.measures] == [
        ("Total Sales", "SUM(Sales[Total])")
    ]
    assert (sales.language, customers.language, calendar.language) == ("M", "M", "DAX")
    assert dataset.columns == 6 and dataset.measures == 1


def test_where_each_table_reads_from_comes_out_of_its_query(catalog):
    dataset = catalog.scan_model("ws-0001").datasets[0]

    assert [[s.describe() for s in t.sources] for t in dataset.tables] == [
        ["SQL Server  sql.example / ds-0001  dbo.Sales"],  # the parameter is followed
        ["Excel workbook  C:\\data\\customers.xlsx  Customers"],
        [],  # a calculated table reads from nothing
    ]


def test_the_places_of_a_dataset_say_which_tables_read_from_each(catalog):
    places = catalog.scan_model("ws-0001").datasets[0].places()

    assert [(p.source.kind, p.tables) for p in places] == [
        ("SQL Server", ["Dataset 1: Sales"]),
        ("Excel workbook", ["Dataset 1: Customers"]),
    ]


def test_the_places_of_a_workspace_join_those_of_its_datasets(world):
    ws = world.fake.datasets[0]["workspaceId"]
    extra = {**world.fake.datasets[0], "id": "ds-extra", "name": "Another"}
    world.fake.datasets.append({**extra, "workspaceId": ws})
    world.clock.advance(minutes=5)
    world.run("scan", scan_flags=EVERYTHING, force=True)

    places = Catalog(world.store, TENANT, clock=world.clock.now).scan_model(ws).places()

    excel = [p for p in places if p.source.kind == "Excel workbook"]
    assert len(excel) == 1  # the same file, read by two datasets
    assert excel[0].tables == ["Dataset 1: Customers", "Another: Customers"]
    assert (
        len([p for p in places if p.source.kind == "SQL Server"]) == 2
    )  # two databases


def test_tables_that_read_from_one_place_share_it():
    same = 'let S = Sql.Database("s", "d"), T = S{[Schema="dbo",Item="%s"]}[Data] in T'
    model = dataset_model(
        {
            "name": "M",
            "tables": [
                {"name": "A", "source": [{"expression": same % "a"}]},
                {"name": "B", "source": [{"expression": same % "b"}]},
            ],
        }
    )

    [place] = model.places()

    assert place.tables == ["M: A", "M: B"]
    assert [s.item for s in model.sources] == ["dbo.a", "dbo.b"]


def test_a_table_that_joins_two_items_of_one_place_is_listed_once_in_it():
    model = dataset_model(
        {
            "name": "M",
            "tables": [
                {
                    "name": "J",
                    "source": [
                        {
                            "expression": 'let A = Sql.Database("s", "d"), B = Sql.Database("s", "d"), '
                            'X = A{[Item="x"]}[Data], Y = B{[Item="y"]}[Data] in X'
                        }
                    ],
                }
            ],
        }
    )

    [place] = model.places()

    assert place.tables == ["M: J"]
    assert len(model.sources) == 2  # two items of the one place


def test_two_tables_that_read_the_very_same_source_give_it_once():
    same = {"expression": 'let S = Sql.Database("s", "d") in S'}
    model = dataset_model(
        {
            "name": "M",
            "tables": [
                {"name": "A", "source": [same]},
                {"name": "B", "source": [same]},
            ],
        }
    )

    assert len(model.sources) == 1 and len(model.places()) == 1


def test_the_parameters_and_shared_queries_of_a_dataset_are_listed(catalog):
    dataset = catalog.scan_model("ws-0001").datasets[0]

    [shared] = dataset.shared
    assert (shared.name, shared.parameter, shared.value) == (
        "ServerName",
        True,
        "sql.example",
    )
    assert shared.description == "The server of the warehouse"


def test_the_data_sources_the_scan_lists_for_a_dataset_are_those_it_uses(catalog):
    dataset = catalog.scan_model("ws-0001").datasets[0]

    assert [d.describe() for d in dataset.declared] == ["Sql  sql.example / ds-0001"]


def test_a_dataset_is_the_one_of_an_item(catalog):
    item = catalog.item("dataset", "ds-0001")

    model = catalog.dataset_model(item)

    assert model is not None and model.name == "Dataset 1" and len(model.tables) == 3
    assert catalog.dataset_model(catalog.item("report", "rep-0001")) is None
    assert catalog.workspace("ws-0001") is not None


def test_a_dataset_that_is_in_no_scan_has_no_model(world):
    plain = World(world.store.root.parent / "other")
    plain.run()
    found = Catalog(plain.store, TENANT, clock=plain.clock.now)

    assert found.dataset_model(found.item("dataset", "ds-0001")) is None
    assert found.scan_model("ws-0001") is None


def test_the_model_of_a_workspace_is_kept_until_the_lake_is_read_again(world, catalog):
    first = catalog.scan_model("ws-0001")

    assert catalog.scan_model("ws-0001") is first
    catalog.reload()
    assert catalog.scan_model("ws-0001") is not first


def test_only_so_many_models_are_kept(world, catalog, monkeypatch):
    monkeypatch.setattr("pbi_cli.core.catalog.VIEW_CACHE", 2)
    world.run("scan", scan_flags=EVERYTHING, force=True, full_scan=True)
    catalog.reload()

    first = catalog.scan_model("ws-0001")
    catalog.scan_model("ws-0002")
    catalog.scan_model("ws-0003")  # the first is dropped

    assert catalog.scan_model("ws-0001") is not first


# ---------------------------------------------------------------------------
# a scan made with fewer options
# ---------------------------------------------------------------------------


def test_a_scan_without_the_schema_has_no_tables(world):
    world.clock.advance(minutes=5)  # a newer scan, with fewer options
    world.run("scan", scan_flags=ScanFlags(lineage=True), force=True)
    found = Catalog(world.store, TENANT, clock=world.clock.now)

    dataset = found.scan_model("ws-0001").datasets[0]

    assert (
        dataset.tables == [] and not dataset.has_schema and not dataset.has_expressions
    )
    assert dataset.sources == [] and dataset.declared == []  # no source details either


def test_a_scan_with_the_schema_but_not_the_expressions_has_tables_but_no_sources(
    world,
):
    world.clock.advance(minutes=5)
    world.run("scan", scan_flags=ScanFlags(dataset_schema=True), force=True)
    found = Catalog(world.store, TENANT, clock=world.clock.now)

    dataset = found.scan_model("ws-0001").datasets[0]

    assert [t.name for t in dataset.tables] == ["Sales", "Customers", "Calendar"]
    assert dataset.has_schema and not dataset.has_expressions
    assert all(t.sources == [] and t.expression == "" for t in dataset.tables)
    assert [m.expression for t in dataset.tables for m in t.measures] == [""]


# ---------------------------------------------------------------------------
# rows as they come
# ---------------------------------------------------------------------------


def test_a_row_without_anything_is_an_empty_dataset():
    model = dataset_model({})

    assert (model.id, model.name, model.tables, model.shared, model.declared) == (
        "",
        "",
        [],
        [],
        [],
    )
    assert model.sources == [] and model.places() == []


def test_a_row_with_odd_values_does_not_break_the_model():
    model = dataset_model(
        {
            "id": 7,
            "name": None,
            "tables": [
                "not a table",
                {"name": "T", "columns": "nope", "measures": [None], "source": [1, {}]},
            ],
            "expressions": [{"name": "P"}, 3],
            "datasourceUsages": [{"datasourceInstanceId": "x"}, "y"],
        },
        [{"datasourceId": "x", "connectionDetails": "not a mapping"}, {}],
    )

    assert model.id == "7" and model.name == ""
    assert [t.name for t in model.tables] == ["T"] and model.tables[0].columns == []
    assert [q.name for q in model.shared] == ["P"]
    assert [d.id for d in model.declared] == ["x"]


def test_a_parameter_is_told_from_a_shared_query():
    model = dataset_model(
        {
            "expressions": [
                {"name": "P", "expression": '"x" meta [IsParameterQuery=true]'},
                {"name": "Q", "expression": "let a = 1 in a"},
            ]
        }
    )

    assert [(q.name, q.parameter, q.value) for q in model.shared] == [
        ("P", True, "x"),
        ("Q", False, ""),
    ]


def test_the_last_expression_of_a_table_is_the_one_that_loads_it():
    model = dataset_model(
        {
            "tables": [
                {
                    "name": "T",
                    "source": [
                        {"expression": 'let S = Sql.Database("a", "b") in S'},
                        {"expression": 'let S = Sql.Database("c", "d") in S'},
                    ],
                }
            ]
        }
    )

    assert model.tables[0].sources[0].server == "c"


def test_the_data_sources_of_a_dataflow_are_those_it_uses():
    model = workspace_model(
        {
            "id": "w",
            "name": "W",
            "dataflows": [
                {
                    "objectId": "f1",
                    "name": "Flow",
                    "datasourceUsages": [
                        {"datasourceInstanceId": "a"},
                        {"datasourceInstanceId": "zzz"},
                    ],
                }
            ],
        },
        [
            {
                "datasourceId": "a",
                "datasourceType": "File",
                "connectionDetails": {"path": "/x.csv"},
            }
        ],
        NOW,
        ScanFlags(),
    )

    [flow] = model.dataflows
    assert (flow.id, flow.name) == ("f1", "Flow")
    assert [d.describe() for d in flow.declared] == ["File  /x.csv"]


def test_a_dashboard_is_named_by_either_field():
    model = workspace_model(
        {
            "dashboards": [
                {"id": "d1", "displayName": "A", "tiles": [{}, {}]},
                {"id": "d2", "name": "B"},
            ]
        },
        [],
        None,
        ScanFlags(),
    )

    assert [(d.name, d.tiles) for d in model.dashboards] == [("A", 2), ("B", 0)]
    assert model.as_of is None and model.dataset("nope") is None


def test_how_a_data_source_is_put_in_words():
    sql = declared_source(
        {
            "datasourceId": "1",
            "datasourceType": "Sql",
            "connectionDetails": {"server": "s", "database": "d", "extra": "e"},
        }
    )
    file = declared_source(
        {
            "datasourceId": "2",
            "datasourceType": "File",
            "connectionDetails": {"path": "C:\\x.xlsx"},
        }
    )
    odd = declared_source(
        {
            "datasourceId": "3",
            "datasourceType": "Custom",
            "connectionDetails": {"thing": "t", "n": 5, "x": None, "o": {}},
        }
    )

    assert sql == Declared("1", "Sql", (("server", "s"), ("database", "d")))
    assert sql.describe() == "Sql  s / d"
    assert file.describe() == "File  C:\\x.xlsx"
    assert odd.describe() == "Custom  t  5"
    assert declared_source({}).describe() == ""
