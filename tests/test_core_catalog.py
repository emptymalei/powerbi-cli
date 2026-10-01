"""The catalog: the lake read the way a person browses it."""

import threading
from datetime import timedelta

import pytest
from sync_helpers import TENANT, World

from pbi_cli.core.catalog import (
    Catalog,
    Freshness,
    Item,
    judge,
    label,
    scalar_fields,
    worst,
)
from pbi_cli.core.scan import ScanFlags
from pbi_cli.core.store import LakeStore
from pbi_cli.core.sync.state import STATE_NAME

FULL = ScanFlags(lineage=True, datasource_details=True, get_artifact_users=True)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def catalog_of(world: World) -> Catalog:
    return Catalog(world.store, TENANT, clock=world.clock.now)


@pytest.fixture
def synced(world):
    """A lake with the plain targets, and a complete scan with every option."""
    world.run()
    world.run("scan", scan_flags=FULL)
    return world


def names(nodes):
    return [node.name for node in nodes]


def row(workspace):
    """A workspace the way the list returns it (without what the fake keeps for itself)."""
    return {k: v for k, v in workspace.items() if k != "modified_at"}


# ---------------------------------------------------------------------------
# an empty lake
# ---------------------------------------------------------------------------


def test_an_empty_lake_has_nothing_to_show(tmp_path):
    catalog = Catalog(LakeStore(tmp_path / "lake"), "tenant-1")

    assert catalog.workspaces() == []
    assert catalog.capacities() == []
    assert catalog.all_items("report") == []
    assert catalog.listing("workspace") is None
    assert catalog.listing_freshness("workspace") is Freshness.NONE
    assert catalog.workspace_freshness("ws-1") is Freshness.NONE
    assert catalog.scan_of("ws-1") is None
    assert catalog.event_days() == []
    assert catalog.holdings() == []
    assert catalog.versions("workspace") == []
    assert catalog.search("anything") == []


# ---------------------------------------------------------------------------
# the lists
# ---------------------------------------------------------------------------


def test_workspaces_are_listed_by_name(world):
    world.run("groups")

    found = catalog_of(world).workspaces()

    assert len(found) == 12
    assert [w.name for w in found[:3]] == [
        "Workspace 1",
        "Workspace 10",
        "Workspace 11",
    ]
    assert found[0].id == "ws-0001" and found[0].type == "Workspace"
    assert found[0].active and not found[0].personal


def test_personal_workspaces_can_be_told_apart(world):
    world.fake.workspaces[2]["type"] = "PersonalGroup"
    world.fake.workspaces[3]["state"] = "Deleted"
    world.run("groups")
    catalog = catalog_of(world)

    assert [w.id for w in catalog.workspaces(personal=True)] == ["ws-0003"]
    assert len(catalog.workspaces(personal=False)) == 11
    assert not catalog.workspace("ws-0004").active


def test_the_items_of_a_workspace_come_from_the_lists(world):
    world.run()
    catalog = catalog_of(world)

    assert catalog.counts("ws-0001") == {
        "report": 1,
        "dataset": 1,
        "dashboard": 1,
        "dataflow": 1,
        "app": 1,
    }
    assert catalog.counts("ws-0007") == {}
    found = catalog.items("ws-0001")
    assert [(i.kind, i.name) for i in found] == [
        ("report", "Report 1"),
        ("dataset", "Dataset 1"),
        ("dashboard", "Dashboard 1"),
        ("dataflow", "Dataflow 1"),
        ("app", "App 1"),
    ]
    assert {i.sources for i in found} == {"list"}
    dataset = next(i for i in found if i.kind == "dataset")
    assert dataset.owner == "owner@example.com"


def test_a_row_without_an_id_is_left_out(world):
    world.fake.reports.append({"name": "No id", "workspaceId": "ws-0001"})
    world.run("reports")

    assert [i.id for i in catalog_of(world).all_items("report")] == [
        f"rep-{n:04d}" for n in range(1, 7)
    ]


def test_capacities_and_the_tenant_lists(world):
    world.run()
    catalog = catalog_of(world)

    assert [c["displayName"] for c in catalog.capacities()] == ["Capacity 1"]
    assert len(catalog.all_items("report")) == 6
    assert catalog.all_items("report")[0].name == "Report 1"


def test_a_list_fetched_with_a_limit_does_not_hide_the_complete_one(world):
    world.run("groups")
    world.clock.advance(minutes=5)
    world.store.write_snapshot(
        TENANT,
        "admin.groups",
        {"$top": "2"},
        {"value": [row(w) for w in world.fake.workspaces[:2]]},
        fetched_at=world.clock.now(),
    )

    found = catalog_of(world)

    assert len(found.workspaces()) == 12
    assert not found.listing("workspace").partial


def test_only_a_partial_list_is_used_and_said_to_be_partial(world):
    world.store.write_snapshot(
        TENANT,
        "admin.groups",
        {"$filter": "name eq 'x'"},
        {"value": [{"id": "ws-9", "name": "X"}]},
        fetched_at=world.clock.now(),
    )

    found = catalog_of(world)

    assert [w.name for w in found.workspaces()] == ["X"]
    assert found.listing("workspace").partial


def test_the_newest_of_several_complete_lists_wins(world):
    world.run("groups")
    world.fake.workspaces[0]["name"] = "Renamed"
    world.clock.advance(hours=2)
    world.run("groups", force=True)

    assert catalog_of(world).workspace("ws-0001").name == "Renamed"


def test_the_newest_of_complete_lists_fetched_with_other_options_wins(world):
    world.run("groups")
    world.store.write_snapshot(
        TENANT,
        "admin.groups",
        {"$expand": "users"},
        {"value": [{"id": "ws-0001", "name": "Expanded", "users": []}]},
        fetched_at=world.clock.now() + timedelta(hours=1),
    )
    world.store.write_snapshot(
        TENANT,
        "admin.groups",
        {"$expand": "reports"},
        {"value": [{"id": "ws-0001", "name": "Older expansion"}]},
        fetched_at=world.clock.now() + timedelta(minutes=30),
    )

    found = catalog_of(world)

    assert [w.name for w in found.workspaces()] == ["Expanded"]
    assert found.listing("workspace").snapshot.manifest["params"] == {
        "$expand": "users"
    }


def test_a_workspace_list_of_the_user_is_the_fallback(world):
    world.run("user-groups")

    found = catalog_of(world)

    assert found.listing("workspace").endpoint == "user.groups"
    assert len(found.workspaces()) == 3


def test_an_unreadable_list_is_ignored(world):
    world.run("groups")
    stored = world.stored("admin.groups")
    (stored.directory / "data.json").write_text("{not json", encoding="utf-8")

    found = catalog_of(world)

    assert found.workspaces() == [] and found.listing("workspace") is None


def test_reload_picks_up_a_newer_sync(world):
    world.run("groups")
    catalog = catalog_of(world)
    assert catalog.items("ws-0001") == []
    assert catalog.scan_of("ws-0001") is None

    world.run("reports", "scan")
    catalog.reload()

    assert [i.name for i in catalog.items("ws-0001")][0] == "Report 1"
    assert catalog.scan_of("ws-0001") is not None


# ---------------------------------------------------------------------------
# scans
# ---------------------------------------------------------------------------


def test_the_scan_is_laid_over_the_list(synced):
    found = catalog_of(synced).items("ws-0001")

    report = next(i for i in found if i.kind == "report")
    assert report.sources == "list + scan"
    assert report.scan["users"][0]["emailAddress"] == "ann@example.com"
    assert report.data["webUrl"] and report.data["users"]  # list and scan together


def test_what_only_the_scan_knows_is_listed_too(world):
    world.run()
    world.fake.reports.append(
        {
            "id": "rep-0099",
            "name": "Brand new",
            "datasetId": "ds-0001",
            "workspaceId": "ws-0001",
        }
    )
    world.run("scan", scan_flags=FULL)
    catalog = catalog_of(world)

    reports = [i for i in catalog.items("ws-0001") if i.kind == "report"]

    assert [(r.name, r.sources) for r in reports] == [
        ("Brand new", "scan"),
        ("Report 1", "list + scan"),
    ]
    assert catalog.counts("ws-0001")["report"] == 1  # the lists do not have it yet
    assert catalog.item("report", "rep-0099", "ws-0001").name == "Brand new"
    assert catalog.item("report", "rep-0099") is None  # without a place to look


def test_a_lake_with_only_scans_can_be_browsed_by_name(world):
    world.run("scan", scan_flags=FULL)  # no lists at all
    catalog = catalog_of(world)

    assert catalog.listing("workspace") is None
    assert len(catalog.workspaces()) == 12
    assert catalog.workspace("ws-0001").name == "Workspace 1"
    assert catalog.workspace("ws-0001").type == "Workspace"
    names = [(i.kind, i.name) for i in catalog.items("ws-0001")]
    assert ("report", "Report 1") in names and ("dashboard", "Dashboard 1") in names


def test_the_newest_scan_of_a_workspace_wins(world):
    world.run("scan", scan_flags=ScanFlags())
    world.clock.advance(days=2)
    world.run("scan", scan_flags=FULL, full_scan=True)

    view = catalog_of(world).scan_of("ws-0001")

    assert view.flags.lineage and view.flags.get_artifact_users
    assert view.workspace["name"] == "Workspace 1"
    assert view.instance("dsi-ds-0001")["datasourceType"] == "Sql"
    assert view.instance("nope") is None
    assert view.find("report", "rep-0001")["name"] == "Report 1"
    assert view.find("report", "rep-0002") is None  # that is in another workspace
    assert [d["id"] for d in view.items("dataset")] == ["ds-0001"]


def test_loaded_scans_are_kept_and_the_cache_is_bounded(world):
    world.run("scan")
    catalog = catalog_of(world)

    first = catalog.scan_of("ws-0001")

    assert catalog.scan_of("ws-0001") is first
    for n in range(1, 13):
        catalog.scan_of(f"ws-{n:04d}")
    assert len(catalog._pieces) <= 4 and len(catalog._views) <= 64


def test_an_unreadable_scan_is_ignored(world):
    world.run("scan")
    for found in world.store.parameter_sets(TENANT, "admin.scan.result"):
        (found.latest.directory / "data.json").write_text("{", encoding="utf-8")

    assert catalog_of(world).scan_of("ws-0001") is None


def test_scans_can_be_asked_from_threads(world):
    world.run("scan", scan_flags=FULL)
    catalog = catalog_of(world)
    problems = []

    def ask(n):
        try:
            for round_ in range(20):
                view = catalog.scan_of(f"ws-{(n + round_) % 12 + 1:04d}")
                assert view is not None and view.workspace["name"]
        except Exception as error:  # the test reports it below
            problems.append(error)

    threads = [threading.Thread(target=ask, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert problems == []


def test_reload_drops_what_it_had_loaded_of_the_old_scans(world):
    world.run("scan", scan_flags=ScanFlags())
    catalog = catalog_of(world)
    assert not catalog.scan_of("ws-0001").flags.lineage
    world.clock.advance(days=2)
    world.run("scan", scan_flags=FULL, full_scan=True)

    catalog.reload()

    assert catalog.scan_of("ws-0001").flags.lineage


def _hand_scan(world, batch, at, name, flags):
    """A stored scan of the workspace ``w``, made by hand."""
    world.store.write_snapshot(
        TENANT,
        "admin.scan.result",
        {"batch": batch, **flags.canonical()},
        {"workspaces": [{"id": "w", "name": name}], "datasourceInstances": []},
        kind="job",
        extra={
            "workspace_ids": ["w"],
            "flags": flags.canonical(),
            "workspaces": {"w": {"name": name}},
        },
        fetched_at=at,
    )


@pytest.mark.parametrize("older_folder_first", [True, False])
def test_the_newest_scan_wins_whatever_the_order_of_the_folders(
    tmp_path, older_folder_first
):
    from datetime import datetime, timezone

    from pbi_cli.core.store import params_hash

    flags = ScanFlags()
    names = [f"batch-{n}" for n in range(40)]
    older, newer = next(
        (a, b)
        for a in names
        for b in names
        if (
            params_hash({"batch": a, **flags.canonical()})
            < params_hash({"batch": b, **flags.canonical()})
        )
        == older_folder_first
    )
    world = World(tmp_path)
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    _hand_scan(world, older, start, "older scan", flags)
    _hand_scan(world, newer, start + timedelta(hours=1), "newer scan", flags)

    view = catalog_of(world).scan_of("w")

    assert view.workspace["name"] == "newer scan"


def test_a_scan_newer_than_the_complete_one_is_as_recent_as_itself(world):
    world.run("groups", "scan", scan_flags=FULL)
    complete = world.clock.now()
    world.clock.advance(days=1)
    world.run("scan", workspace_ids=("ws-0001",), force=True, scan_flags=FULL)
    catalog = catalog_of(world)

    assert catalog.scan_as_of("ws-0001") == world.clock.now()  # its own scan
    assert catalog.scan_as_of("ws-0002") == complete  # vouched for by the complete one


# ---------------------------------------------------------------------------
# freshness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "age, ttl, expected",
    [
        (None, timedelta(hours=24), Freshness.NONE),
        (timedelta(hours=1), timedelta(hours=24), Freshness.FRESH),
        (timedelta(hours=24), timedelta(hours=24), Freshness.AGING),
        (timedelta(days=6), timedelta(hours=24), Freshness.AGING),
        (timedelta(days=7), timedelta(hours=24), Freshness.OLD),
        (timedelta(days=20), timedelta(days=30), Freshness.FRESH),  # a long ttl counts
        (timedelta(days=30), timedelta(days=30), Freshness.OLD),
    ],
)
def test_judging_an_age(age, ttl, expected):
    assert judge(age, ttl) is expected


def test_the_least_fresh_wins():
    assert worst([Freshness.FRESH, Freshness.AGING]) is Freshness.AGING
    assert worst([Freshness.FRESH, Freshness.NONE, Freshness.OLD]) is Freshness.NONE
    assert worst([Freshness.FRESH]) is Freshness.FRESH
    assert worst([]) is Freshness.NONE


def test_data_ages_from_fresh_to_old(world):
    world.run()
    catalog = catalog_of(world)
    assert catalog.listing_freshness("workspace") is Freshness.FRESH
    assert catalog.workspace_freshness("ws-0001") is Freshness.FRESH

    world.clock.advance(hours=25)
    assert catalog.listing_freshness("workspace") is Freshness.AGING

    world.clock.advance(days=7)
    assert catalog.listing_freshness("workspace") is Freshness.OLD
    assert catalog.listing_freshness("report") is Freshness.OLD
    assert catalog.age(catalog.listing("workspace").fetched_at) > timedelta(days=7)
    assert catalog.age(None) is None


def test_a_workspace_is_as_fresh_as_its_least_fresh_source(world):
    world.run("groups", "scan")
    catalog = catalog_of(world)
    world.clock.advance(hours=12)
    world.run("groups", force=True)  # the list is new, the scan is not
    catalog.reload()
    assert (
        catalog.workspace_freshness("ws-0001") is Freshness.FRESH
    )  # the scan is 12 h old

    world.clock.advance(hours=13)
    world.run("groups", force=True)
    catalog.reload()

    assert catalog.listing_freshness("workspace") is Freshness.FRESH
    assert (
        catalog.workspace_freshness("ws-0001") is Freshness.AGING
    )  # the scan is 25 h old


def test_a_scan_that_found_nothing_changed_still_counts_as_recent(world):
    world.run("groups", "scan", scan_flags=FULL)
    world.clock.advance(days=10)
    world.run("groups", "scan", scan_flags=FULL)  # incremental: nothing changed
    catalog = catalog_of(world)

    view = catalog.scan_of("ws-0001")

    assert world.fake.count(r"getInfo", "POST") == 1  # only the first scan scanned
    assert (
        view.ref.fetched_at < view.as_of
    )  # the workspace itself was not scanned again
    assert catalog.age(view.as_of) < timedelta(minutes=1)
    assert catalog.workspace_freshness("ws-0001") is Freshness.FRESH


def _state(world, **scan):
    world.store.write_state(TENANT, STATE_NAME, {"schema": 1, "scan": scan})


def test_a_complete_scan_with_other_options_does_not_vouch_for_this_one(world):
    world.run("scan", scan_flags=FULL)
    first = world.clock.now()
    world.clock.advance(days=3)
    _state(
        world,
        last_success_at=world.clock.now().isoformat(),
        flags=ScanFlags(lineage=True).canonical(),
        coverage={},
    )

    assert catalog_of(world).scan_as_of("ws-0001") == first


def test_a_complete_scan_that_left_out_personal_workspaces_does_not_vouch_for_them(
    world,
):
    world.fake.workspaces[1]["type"] = "PersonalGroup"
    world.run("groups", "scan", scan_flags=FULL)
    first = world.clock.now()
    world.clock.advance(days=3)
    _state(
        world,
        last_success_at=world.clock.now().isoformat(),
        flags=FULL.canonical(),
        coverage={
            "excludePersonalWorkspaces": True,
            "excludeInActiveWorkspaces": False,
        },
    )
    catalog = catalog_of(world)

    assert catalog.scan_as_of("ws-0002") == first  # personal: not covered
    assert catalog.scan_as_of("ws-0001") == world.clock.now()  # covered


def test_a_complete_scan_that_left_out_inactive_workspaces_vouches_for_none(world):
    world.run("groups", "scan", scan_flags=FULL)
    first = world.clock.now()
    world.clock.advance(days=3)
    _state(
        world,
        last_success_at=world.clock.now().isoformat(),
        flags=FULL.canonical(),
        coverage={
            "excludePersonalWorkspaces": False,
            "excludeInActiveWorkspaces": True,
        },
    )

    assert (
        catalog_of(world).scan_as_of("ws-0001") == first
    )  # which are inactive is unknown


def test_a_damaged_state_means_no_baseline(world):
    world.run("scan", scan_flags=FULL)
    world.store.write_state(
        TENANT, STATE_NAME, {"schema": 1, "scan": {"last_success_at": "soon"}}
    )

    catalog = catalog_of(world)

    assert catalog.scan_as_of("ws-0001") == catalog._s.scans["ws-0001"].fetched_at


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


def test_the_users_of_a_workspace_come_from_the_scan(synced):
    catalog = catalog_of(synced)

    users = catalog.users(catalog.workspace("ws-0001"))

    assert [(a.name, a.email, a.role, a.type) for a in users.rows] == [
        ("Owner", "owner@example.com", "Admin", "User")
    ]
    assert users.source == "scan" and users.fetched_at and not users.missing


def test_the_users_of_a_workspace_come_from_the_list_when_it_was_expanded(world):
    world.fake.workspaces[0]["users"] = [
        {
            "displayName": "Zed",
            "emailAddress": "zed@example.com",
            "groupUserAccessRight": "Member",
        }
    ]
    world.run("groups")
    catalog = catalog_of(world)

    users = catalog.users(catalog.workspace("ws-0001"))

    assert [(a.name, a.role) for a in users.rows] == [("Zed", "Member")]
    assert users.source == "admin.groups"


def test_a_workspace_without_a_scan_says_how_to_get_its_users(world):
    world.run("groups")
    catalog = catalog_of(world)

    users = catalog.users(catalog.workspace("ws-0001"))

    assert users.rows == [] and "pbi sync run scan" in users.missing


def test_the_users_of_a_report_come_from_the_fetched_ones_first(world):
    world.run("report-users", "scan", scan_flags=FULL)
    catalog = catalog_of(world)

    users = catalog.users(catalog.item("report", "rep-0001"))

    assert [(a.name, a.email, a.role) for a in users.rows] == [
        ("Ann", "ann-rep-0001@example.com", "Owner")
    ]
    assert users.source == "admin.reports.users"


def test_the_users_of_a_report_come_from_the_scan_otherwise(synced):
    catalog = catalog_of(synced)

    users = catalog.users(catalog.item("report", "rep-0001"))

    assert [(a.email, a.role) for a in users.rows] == [("ann@example.com", "Owner")]
    assert users.source == "scan"


def test_what_is_missing_about_users_is_said(world):
    world.run()
    catalog = catalog_of(world)

    report = catalog.users(catalog.item("report", "rep-0001"))
    dataset = catalog.users(catalog.item("dataset", "ds-0001"))

    assert "report-users" in report.missing and "--get-artifact-users" in report.missing
    assert "--get-artifact-users" in dataset.missing
    assert "report-users" not in dataset.missing


# ---------------------------------------------------------------------------
# lineage
# ---------------------------------------------------------------------------


def test_the_lineage_of_a_report_goes_up_to_the_data_sources(synced):
    catalog = catalog_of(synced)

    lineage = catalog.lineage(catalog.item("report", "rep-0001"))

    (dataset,) = lineage.upstream
    assert (dataset.kind, dataset.name, dataset.workspace) == (
        "dataset",
        "Dataset 1",
        "",
    )
    assert [(n.kind, n.name) for n in dataset.children] == [
        ("dataflow", "Dataflow 1"),
        ("datasource", "Sql: sql.example, ds-0001"),
    ]
    (dashboard,) = lineage.downstream
    assert (dashboard.kind, dashboard.name) == ("dashboard", "Dashboard 1")


def test_the_lineage_of_a_dataset_goes_both_ways_across_workspaces(synced):
    catalog = catalog_of(synced)

    lineage = catalog.lineage(catalog.item("dataset", "ds-0001"))

    assert names(lineage.upstream) == ["Dataflow 1", "Sql: sql.example, ds-0001"]
    first, second = lineage.downstream
    assert (first.name, first.workspace) == ("Report 1", "")
    assert names(first.children) == ["Dashboard 1"]
    # the report of another workspace comes from the lists of the tenant
    assert (second.name, second.workspace) == ("Report 5", "Workspace 5")
    assert second.children == []


def test_the_lineage_of_a_dataflow_goes_down_to_the_dashboards(synced):
    catalog = catalog_of(synced)

    lineage = catalog.lineage(catalog.item("dataflow", "flow-0001"))

    assert lineage.upstream == []
    (dataset,) = (
        lineage.downstream
    )  # ds-0002 uses it too, from another workspace's scan
    assert dataset.name == "Dataset 1"
    assert names(dataset.children) == ["Report 1", "Report 5"]
    assert names(dataset.children[0].children) == ["Dashboard 1"]
    assert any("own workspace" in note for note in lineage.notes)


def test_the_lineage_of_a_dashboard_goes_up_through_its_tiles(synced):
    catalog = catalog_of(synced)

    lineage = catalog.lineage(catalog.item("dashboard", "dash-0001"))

    (report,) = lineage.upstream
    assert report.name == "Report 1"
    (dataset,) = report.children
    assert dataset.name == "Dataset 1" and len(dataset.children) == 2
    assert lineage.downstream == []


def test_a_dataflow_built_on_another_dataflow(world):
    _scan_with(
        world,
        {
            "id": "w",
            "name": "W",
            "dataflows": [
                {"objectId": "a", "name": "A"},
                {
                    "objectId": "b",
                    "name": "B",
                    "upstreamDataflows": [{"targetDataflowId": "a", "groupId": "w"}],
                },
            ],
        },
    )
    catalog = catalog_of(world)

    assert names(catalog.lineage(catalog.item("dataflow", "a", "w")).downstream) == [
        "B"
    ]
    assert names(catalog.lineage(catalog.item("dataflow", "b", "w")).upstream) == ["A"]


def test_a_dataset_built_on_another_dataset(tmp_path):
    world = World(tmp_path, workspaces=2, datasets=4, reports=4)
    world.run()
    world.run("scan", scan_flags=FULL)
    catalog = catalog_of(world)

    lineage = catalog.lineage(
        catalog.item("dataset", "ds-0003")
    )  # in ws-0001, on ds-0001

    assert names(lineage.upstream)[:1] == ["Dataset 1"]
    down = catalog.lineage(catalog.item("dataset", "ds-0001")).downstream
    assert "Dataset 3" in names(down)


def test_the_lineage_with_only_lists_shows_what_the_lists_say(world):
    world.run()
    catalog = catalog_of(world)

    lineage = catalog.lineage(catalog.item("report", "rep-0001"))

    (dataset,) = lineage.upstream
    assert dataset.name == "Dataset 1" and dataset.children == []
    assert any("No scan of this workspace" in note for note in lineage.notes)
    down = catalog.lineage(catalog.item("dataset", "ds-0001")).downstream
    assert names(down) == ["Report 1", "Report 5"]


def test_the_lineage_of_a_scan_without_lineage_says_so(world):
    world.run()
    world.run("scan", scan_flags=ScanFlags())
    catalog = catalog_of(world)

    lineage = catalog.lineage(catalog.item("dataset", "ds-0001"))

    assert any("without lineage" in note for note in lineage.notes)
    assert lineage.upstream == []


def test_a_reference_names_the_workspace_to_look_in(world):
    # no list knows the dataflow: the dataset's scan says which workspace it is in
    _scan_with(
        world,
        {
            "id": "w1",
            "name": "One",
            "datasets": [
                {
                    "id": "a",
                    "name": "A",
                    "upstreamDataflows": [{"targetDataflowId": "f", "groupId": "w2"}],
                }
            ],
        },
    )
    _scan_with(
        world,
        {"id": "w2", "name": "Two", "dataflows": [{"objectId": "f", "name": "Flow F"}]},
    )
    catalog = catalog_of(world)

    (flow,) = catalog.lineage(catalog.item("dataset", "a", "w1")).upstream

    assert (flow.kind, flow.name, flow.workspace, flow.note) == (
        "dataflow",
        "Flow F",
        "Two",
        "",
    )


def test_a_reference_to_an_unknown_item_is_a_node_that_says_so(world):
    world.run("groups")
    world.store.write_snapshot(
        TENANT,
        "admin.reports",
        {},
        {
            "value": [
                {"id": "r", "name": "R", "datasetId": "gone", "workspaceId": "ws-0001"}
            ]
        },
        fetched_at=world.clock.now(),
    )
    catalog = catalog_of(world)

    (node,) = catalog.lineage(catalog.item("report", "r")).upstream

    assert (node.kind, node.id, node.name, node.note) == (
        "dataset",
        "gone",
        "gone",
        "not in the lake",
    )


def _scan_with(world, workspace):
    """Store a scan result made by hand, for shapes the fake service cannot produce."""
    world.store.write_snapshot(
        TENANT,
        "admin.scan.result",
        {"batch": f"by-hand-{workspace['id']}", **FULL.canonical()},
        {"workspaces": [workspace], "datasourceInstances": []},
        kind="job",
        extra={
            "workspace_ids": [workspace["id"]],
            "flags": FULL.canonical(),
            "workspaces": {workspace["id"]: {"name": workspace["name"]}},
        },
        fetched_at=world.clock.now(),
    )


def test_a_cycle_in_the_lineage_is_cut(world):
    _scan_with(
        world,
        {
            "id": "w",
            "name": "W",
            "datasets": [
                {
                    "id": "a",
                    "name": "A",
                    "upstreamDatasets": [{"targetDatasetId": "b", "groupId": "w"}],
                },
                {
                    "id": "b",
                    "name": "B",
                    "upstreamDatasets": [{"targetDatasetId": "a", "groupId": "w"}],
                },
            ],
        },
    )
    catalog = catalog_of(world)

    lineage = catalog.lineage(catalog.item("dataset", "a", "w"))

    (b,) = lineage.upstream
    (again,) = b.children
    assert (b.name, again.name, again.note) == ("B", "A", "shown above")
    assert again.children == []
    (down,) = lineage.downstream
    assert down.name == "B" and down.children[0].note == "shown above"


def test_the_lineage_is_not_followed_without_end(world):
    chain = [
        {
            "id": f"d{n}",
            "name": f"D{n}",
            "upstreamDatasets": [{"targetDatasetId": f"d{n + 1}", "groupId": "w"}],
        }
        for n in range(30)
    ]
    _scan_with(world, {"id": "w", "name": "W", "datasets": chain})
    catalog = catalog_of(world)

    node = catalog.lineage(catalog.item("dataset", "d0", "w")).upstream[0]
    depth = 1
    while node.children:
        node = node.children[0]
        depth += 1

    assert 3 <= depth <= 10


def test_a_data_source_without_details_is_still_shown(world):
    _scan_with(
        world,
        {
            "id": "w",
            "name": "W",
            "datasets": [
                {
                    "id": "a",
                    "name": "A",
                    "datasourceUsages": [{"datasourceInstanceId": "x"}],
                }
            ],
        },
    )

    (node,) = (
        catalog_of(world).lineage(catalog_of(world).item("dataset", "a", "w")).upstream
    )

    assert (node.kind, node.name, node.note) == (
        "datasource",
        "x",
        "details not in the scan",
    )


# ---------------------------------------------------------------------------
# versions, events, holdings, search
# ---------------------------------------------------------------------------


def test_versions_are_the_stored_answers_that_hold_something(world):
    world.run("reports", "scan")
    world.clock.advance(days=2)
    world.run("reports", "scan", force=True)
    catalog = catalog_of(world)

    only_list = catalog.versions("report")
    with_scans = catalog.versions("report", "ws-0001")

    assert [v.endpoint for v in only_list] == ["admin.reports"] * 2
    assert only_list[0].fetched_at > only_list[1].fetched_at
    assert sorted({v.endpoint for v in with_scans}) == [
        "admin.reports",
        "admin.scan.result",
    ]
    assert len(with_scans) == 4
    assert with_scans[0].rows is not None and with_scans[0].size > 0
    assert with_scans[0].profile == "admin-nlm" and with_scans[0].version


def test_the_events_of_a_day_come_newest_first(world):
    world.run("activity", days=2)
    catalog = catalog_of(world)

    days = catalog.event_days()
    events = catalog.events(days[1].day)  # yesterday: the fake has five events a day

    assert len(days) == 2 and days[0].day > days[1].day
    assert len(events) == 5
    stamps = [e["CreationTime"] for e in events]
    assert stamps == sorted(stamps, reverse=True)


def test_what_the_lake_holds_is_listed_by_target(world):
    world.run("groups", "activity", days=2)

    held = dict(catalog_of(world).holdings())

    assert held["groups"].endpoint == "admin.groups"
    assert (held["groups"].count, held["groups"].unit) == (1, "request")
    assert (held["activity"].count, held["activity"].unit) == (2, "day")
    assert held["groups"].newest is not None
    assert "reports" not in held


def test_search_finds_workspaces_and_items_by_name_or_id(world):
    world.run()
    catalog = catalog_of(world)

    found = catalog.search("report 1")
    assert [(m.kind, m.name, m.workspace) for m in found] == [
        ("report", "Report 1", "Workspace 1")
    ]
    assert [m.id for m in catalog.search("rep-0003")] == ["rep-0003"]
    assert catalog.search("Workspace 1")[0].kind == "workspace"
    assert len(catalog.search("workspace 1", limit=2)) == 2
    assert catalog.search("w") == []  # one letter matches too much
    assert len(catalog.search("wo", limit=100)) == 12
    assert catalog.search("nothing like this") == []


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def test_scalar_fields_put_the_usual_ones_first_and_skip_the_nested_ones():
    data = {
        "zeta": 1,
        "id": "i",
        "users": [{"a": 1}],
        "name": "N",
        "empty": "",
        "none": None,
        "Alpha": True,
        "nested": {"a": 1},
        "state": "Active",
    }

    assert scalar_fields(data) == [
        ("name", "N"),
        ("id", "i"),
        ("state", "Active"),
        ("Alpha", "True"),
        ("zeta", "1"),
    ]


def test_labels():
    assert label("report") == "Report" and label("report", True) == "Reports"
    assert label("capacity", True) == "Capacities"
    assert label("odd") == "Odd"


def test_an_item_knows_when_it_was_updated_and_by_whom():
    item = Item(
        "report",
        "r",
        "R",
        None,
        {
            "createdDateTime": "2026-01-01T00:00:00Z",
            "modifiedDateTime": "2026-02-02T10:00:00Z",
            "createdBy": "a@b",
        },
        {"modifiedBy": "c@d"},
    )

    assert item.updated == "2026-02-02T10:00:00Z"
    assert item.owner == "c@d"  # configuredBy, then modifiedBy, then createdBy
    assert Item("report", "r", "R", None, {}).owner == ""
    assert Item("report", "r", "R", None, {}).sources == ""
