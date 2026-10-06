"""The targets that fetch a detail of each item, and the fetch that is limited to some items."""

import pytest
from sync_helpers import World

from pbi_cli.core.sync.engine import COMPLETED, COMPLETED_WITH_FAILURES
from pbi_cli.core.sync.plan import SyncOptions
from pbi_cli.core.sync.runners import DONE, FAILED


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def stored_ids(world, endpoint, placeholder):
    """The ids that the lake holds an answer of ``endpoint`` for."""
    return sorted(
        found.params[placeholder]
        for found in world.store.parameter_sets("tenant-1", endpoint)
    )


# ---------------------------------------------------------------------------
# what an administrator can fetch of each item
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target, endpoint, placeholder, ids",
    [
        ("report-users", "admin.reports.users", "reportId", 6),
        ("dataset-users", "admin.datasets.users", "datasetId", 4),
        ("dashboard-users", "admin.dashboards.users", "dashboardId", 1),
        ("dataflow-users", "admin.dataflows.users", "dataflowId", 1),
        ("dataflow-datasources", "admin.dataflows.datasources", "dataflowId", 1),
        ("datasources", "admin.datasets.datasources", "datasetId", 4),
        ("group-users", "admin.groups.users", "groupId", 12),
    ],
)
def test_a_detail_is_fetched_for_every_item_of_its_parent(
    world, target, endpoint, placeholder, ids
):
    report = world.run(target)

    assert report.status == COMPLETED and report.counts[FAILED] == 0
    assert len(stored_ids(world, endpoint, placeholder)) == ids
    first = world.stored(
        endpoint, {placeholder: stored_ids(world, endpoint, placeholder)[0]}
    )
    assert first is not None and first.manifest["rows"] >= 1


def test_the_ids_of_a_dataflow_come_from_its_object_id(world):
    world.run("dataflow-users")

    assert stored_ids(world, "admin.dataflows.users", "dataflowId") == ["flow-0001"]


def test_the_refresh_summaries_of_the_tenant_are_one_list(world):
    report = world.run("refreshables")

    assert report.counts[DONE] == 1
    found = world.stored("admin.refreshables", {})
    assert found is not None and found.manifest["rows"] == 4
    assert world.fake.count(r"^/admin/capacities/refreshables$") == 1


def test_the_new_admin_operations_are_only_called_for_an_administrator(world):
    world.run("dataset-users", "dataflow-datasources", "refreshables")

    admin = world.fake.calls_to(r"^/admin/")
    assert admin and not [
        c for c in world.fake.calls_to(r"^/groups") if c.path.endswith("/users")
    ]


# ---------------------------------------------------------------------------
# what a user can fetch of each item
# ---------------------------------------------------------------------------


@pytest.fixture
def user_world(world):
    world.only_user()
    return world


@pytest.mark.parametrize(
    "target, endpoint",
    [
        ("user-dataset-users", "user.dataset_users"),
        ("user-dataset-datasources", "user.dataset_datasources"),
        ("user-dataset-refreshes", "user.dataset_refreshes"),
        ("user-dataset-parameters", "user.dataset_parameters"),
    ],
)
def test_a_user_fetches_a_detail_of_each_dataset_of_each_workspace(
    user_world, target, endpoint
):
    report = user_world.run(target)

    assert report.status == COMPLETED
    held = user_world.store.parameter_sets("tenant-1", endpoint)
    assert sorted((p.params["groupId"], p.params["datasetId"]) for p in held) == [
        ("ws-0001", "ds-0001"),
        ("ws-0002", "ds-0002"),
        ("ws-0003", "ds-0003"),
    ]
    assert not user_world.fake.calls_to(r"^/admin")


def test_a_user_fetches_the_tiles_of_a_dashboard_and_the_data_sources_of_a_dataflow(
    user_world,
):
    user_world.run("user-dashboard-tiles", "user-dataflow-datasources")

    tiles = user_world.store.parameter_sets("tenant-1", "user.dashboard_tiles")
    sources = user_world.store.parameter_sets("tenant-1", "user.dataflow_datasources")
    assert [p.params for p in tiles] == [
        {"groupId": "ws-0001", "dashboardId": "dash-0001"}
    ]
    assert [p.params for p in sources] == [
        {"groupId": "ws-0001", "dataflowId": "flow-0001"}
    ]


def test_a_dataset_the_user_may_not_read_does_not_stop_the_others(user_world):
    user_world.fake.restricted_datasets.add("ds-0002")

    report = user_world.run("user-dataset-datasources")

    assert report.status == COMPLETED_WITH_FAILURES
    assert report.counts[FAILED] == 1
    held = user_world.store.parameter_sets("tenant-1", "user.dataset_datasources")
    assert sorted(p.params["datasetId"] for p in held) == ["ds-0001", "ds-0003"]


# ---------------------------------------------------------------------------
# a fetch that is limited to some items
# ---------------------------------------------------------------------------


def test_a_fan_out_can_be_limited_to_the_items_that_are_wanted(world):
    world.run("datasets")  # the list is in the lake: only the detail is left to fetch
    world.fake.reset_calls()

    report = world.run("dataset-users", only={"datasetId": ["ds-0002"]})

    assert report.status == COMPLETED
    assert stored_ids(world, "admin.datasets.users", "datasetId") == ["ds-0002"]
    assert world.fake.count(r"^/admin/datasets/[^/]+/users$") == 1
    assert world.fake.count(r"^/admin/datasets$") == 0  # fresh: not fetched again


def test_a_fan_out_that_is_not_limited_fetches_every_item(world):
    world.run("dataset-users", only={"reportId": ["rep-0001"]})  # not its placeholder

    assert len(stored_ids(world, "admin.datasets.users", "datasetId")) == 4


def test_a_user_fan_out_is_limited_at_each_level(user_world):
    report = user_world.run(
        "user-dataset-users", only={"groupId": ["ws-0001"], "datasetId": ["ds-0001"]}
    )

    assert report.status == COMPLETED
    held = user_world.store.parameter_sets("tenant-1", "user.dataset_users")
    assert [p.params for p in held] == [{"groupId": "ws-0001", "datasetId": "ds-0001"}]
    # one workspace was asked for its datasets, not the three the user can see
    assert user_world.fake.count(r"^/groups/[^/]+/datasets$") == 1


def test_nothing_is_fetched_for_an_item_that_is_not_in_the_list(world):
    world.run("datasets")
    world.fake.reset_calls()

    report = world.run("dataset-users", only={"datasetId": ["ds-9999"]})

    assert report.status == COMPLETED
    assert world.fake.count(r"^/admin/datasets/[^/]+/users$") == 0


def test_the_plan_of_a_limited_fetch_counts_one_request(world):
    world.run("datasets")

    plan = world.plan("dataset-users", only={"datasetId": ["ds-0001"]})

    child = next(t for t in plan.targets if t.target.name == "dataset-users")
    assert (child.units, child.todo, child.requests) == (1, 1, 1)


# ---------------------------------------------------------------------------
# the options
# ---------------------------------------------------------------------------


def test_only_is_normalised_and_the_options_can_still_be_hashed():
    options = SyncOptions(only={"reportId": ["a", "a", "", "b"], "groupId": ("g",)})

    assert options.only == {"reportId": ("a", "b"), "groupId": ("g",)}
    assert hash(options) == hash(SyncOptions(only={"other": ["x"]}))  # not part of it
    assert options != SyncOptions(only={"reportId": ["a"]})


def test_what_is_wanted_is_decided_per_placeholder():
    options = SyncOptions(only={"reportId": ["a"], "groupId": []})

    assert options.allows("reportId", "a") and not options.allows("reportId", "b")
    assert not options.allows("groupId", "anything")  # an empty list wants nothing
    assert options.allows("datasetId", "anything")  # not named: not narrowed


def test_the_record_of_a_run_says_how_many_items_were_wanted(world):
    world.run("datasets")
    world.run("dataset-users", only={"datasetId": ["ds-0001", "ds-0002"]})

    run = world.state()["runs"][-1]

    assert run["options"]["only"] == {"datasetId": 2}
