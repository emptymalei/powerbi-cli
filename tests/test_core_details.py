"""The details of an item: which there are, who can fetch them, and what the lake holds."""

import pytest
from sync_helpers import TENANT, World

from pbi_cli.core.catalog import Catalog, Item
from pbi_cli.core.details import (
    choose,
    collect,
    detail_names,
    fetch_options,
    has_margin,
    providers_of,
)
from pbi_cli.core.registry import Scope, get_endpoint
from pbi_cli.core.store import LakeStore
from pbi_cli.core.sync.state import STATE_NAME


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def catalog_of(world: World) -> Catalog:
    return Catalog(world.store, TENANT, clock=world.clock.now)


# ---------------------------------------------------------------------------
# which details there are
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind, names",
    [
        ("report", ["users", "pages"]),
        ("dataset", ["users", "datasources", "refreshes", "parameters"]),
        ("dashboard", ["users", "tiles"]),
        ("dataflow", ["users", "datasources"]),
        ("workspace", ["users"]),
        ("app", []),
        ("capacity", []),
    ],
)
def test_an_item_has_the_details_the_targets_can_fetch_for_its_kind(kind, names):
    assert detail_names(kind) == names


# ---------------------------------------------------------------------------
# who can fetch one
# ---------------------------------------------------------------------------


def test_a_detail_can_be_fetched_by_an_administrator_and_by_a_user_in_that_order():
    found = providers_of("dataset", "users", "ds-1", "ws-1")

    assert [(p.target.name, p.scope) for p in found] == [
        ("dataset-users", Scope.ADMIN),
        ("user-dataset-users", Scope.USER),
    ]
    assert [p.params for p in found] == [
        {"datasetId": "ds-1"},
        {"groupId": "ws-1", "datasetId": "ds-1"},
    ]
    assert [p.only for p in found] == [
        {"datasetId": ["ds-1"]},
        {"groupId": ["ws-1"], "datasetId": ["ds-1"]},
    ]


def test_a_user_cannot_fetch_what_needs_the_workspace_when_there_is_none():
    found = providers_of("dataset", "users", "ds-1", None)

    assert [p.target.name for p in found] == ["dataset-users"]


def test_the_id_of_a_dataflow_and_of_a_workspace_are_what_the_requests_use():
    flow = providers_of("dataflow", "datasources", "flow-1", "ws-1")
    workspace = providers_of("workspace", "users", "ws-1", "ws-1")

    assert [p.params for p in flow] == [
        {"dataflowId": "flow-1"},
        {"groupId": "ws-1", "dataflowId": "flow-1"},
    ]
    assert [p.params for p in workspace] == [{"groupId": "ws-1"}, {"groupId": "ws-1"}]


def test_a_summary_of_the_tenant_is_a_way_to_fetch_the_refreshes_of_a_dataset():
    found = providers_of("dataset", "refreshes", "ds-1", "ws-1")

    assert [p.target.name for p in found] == ["refreshables", "user-dataset-refreshes"]
    assert found[0].params == {} and found[0].only == {}  # one list, for every dataset


def test_a_provider_says_what_the_account_must_be():
    admin, user = providers_of("dataset", "datasources", "ds-1", "ws-1")

    assert admin.needs == "an administrator account"
    assert user.needs == "a user account with Write permission on the dataset"
    plain = providers_of("report", "pages", "rep-1", "ws-1")[0]
    assert plain.needs == "a user account"


def test_a_provider_knows_the_key_of_its_unit_in_a_sync():
    admin, user = providers_of("dataset", "users", "ds-1", "ws-1")

    assert admin.key == "admin.datasets.users?datasetId=ds-1"
    assert user.key == "user.dataset_users?datasetId=ds-1&groupId=ws-1"


# ---------------------------------------------------------------------------
# what the lake holds
# ---------------------------------------------------------------------------


def details_of(world, kind, item_id, workspace_id):
    state = world.store.read_state(TENANT, STATE_NAME) or {}
    return {
        d.name: d
        for d in collect(
            world.store, TENANT, kind, item_id, workspace_id, state.get("units") or {}
        )
    }


def test_an_item_with_nothing_fetched_has_every_detail_missing(world):
    found = details_of(world, "dataset", "ds-0001", "ws-0001")

    assert list(found) == ["users", "datasources", "refreshes", "parameters"]
    assert all(d.held is None and d.problems == () for d in found.values())
    assert all(d.providers for d in found.values())


def test_what_was_fetched_is_held_with_who_fetched_it_and_when(world):
    world.run("dataset-users")

    found = details_of(world, "dataset", "ds-0001", "ws-0001")

    held = found["users"].held
    assert held is not None and held.provider.target.name == "dataset-users"
    assert held.rows == 2 and held.fetched_at == held.snapshot.fetched_at
    assert found["datasources"].held is None  # that one was not fetched


def test_the_newest_answer_wins_whoever_gave_it(world):
    world.run("dataset-users")
    world.clock.advance(hours=3)
    world.only_user()
    world.run("user-dataset-users")

    held = details_of(world, "dataset", "ds-0001", "ws-0001")["users"].held

    assert held is not None and held.provider.target.name == "user-dataset-users"


def test_a_summary_of_the_tenant_holds_the_rows_that_are_about_the_dataset(world):
    world.run("refreshables")

    found = details_of(world, "dataset", "ds-0002", "ws-0002")["refreshes"]

    assert found.held is not None and found.held.rows == 1
    assert found.held.provider.target.name == "refreshables"


def test_a_dataset_the_summary_does_not_list_is_held_with_no_rows(world):
    world.run("refreshables")

    found = details_of(world, "dataset", "ds-9999", "ws-0001")["refreshes"]

    assert found.held is not None and found.held.rows == 0


def test_what_a_sync_could_not_fetch_is_said(world):
    world.only_user()
    world.fake.restricted_datasets.add("ds-0001")
    world.run("user-dataset-datasources", "user-dataset-users")

    found = details_of(world, "dataset", "ds-0001", "ws-0001")

    assert found["datasources"].held is None
    assert len(found["datasources"].problems) == 1
    assert "needs Write permission on the dataset" in found["datasources"].problems[0]
    assert "Reshare permission" in found["users"].problems[0]
    other = details_of(world, "dataset", "ds-0002", "ws-0002")
    assert other["datasources"].held is not None and other["datasources"].problems == ()


def test_a_problem_is_forgotten_once_the_detail_has_been_fetched(world):
    world.only_user()
    world.fake.restricted_datasets.add("ds-0001")
    world.run("user-dataset-datasources")
    world.fake.restricted_datasets.clear()
    world.clock.advance(hours=3)

    world.run("user-dataset-datasources")
    found = details_of(world, "dataset", "ds-0001", "ws-0001")["datasources"]

    assert found.problems == () and found.held is not None


# ---------------------------------------------------------------------------
# the catalog
# ---------------------------------------------------------------------------


def test_the_catalog_gives_the_details_of_an_item_and_of_a_workspace(world):
    world.run("datasets", "group-users", "dataset-users")
    catalog = catalog_of(world)

    dataset = catalog.details(catalog.item("dataset", "ds-0001"))
    workspace = catalog.details(catalog.workspace("ws-0001"))

    assert [d.name for d in dataset] == [
        "users",
        "datasources",
        "refreshes",
        "parameters",
    ]
    assert dataset[0].held is not None
    assert [d.name for d in workspace] == ["users"] and workspace[0].held is not None


def test_the_users_of_a_dataset_come_from_what_was_fetched(world):
    world.run("datasets", "dataset-users")
    catalog = catalog_of(world)

    users = catalog.users(catalog.item("dataset", "ds-0001"))

    assert users.source == "admin.datasets.users"
    assert [(a.name, a.email, a.role) for a in users.rows] == [
        ("Ann", "ann-ds-0001@example.com", "Owner"),
        ("Bob", "bob-ds-0001@example.com", "Read"),
    ]


def test_the_users_of_a_dataset_that_a_user_fetched_are_only_known_by_identifier(world):
    world.only_user()
    world.run("user-dataset-users")
    catalog = catalog_of(world)

    users = catalog.users(catalog.item("dataset", "ds-0001"))

    assert users.source == "user.dataset_users"
    assert [(a.name, a.email, a.role, a.type) for a in users.rows] == [
        ("", "ann-ds-0001@example.com", "ReadWriteReshare", "User"),
        ("", "grp-ds-0001", "Read", "Group"),
    ]


def test_the_users_of_a_workspace_come_from_the_administrators_way_too(world):
    world.run("groups", "group-users")
    catalog = catalog_of(world)

    users = catalog.users(catalog.workspace("ws-0001"))

    assert users.source == "admin.groups.users" and len(users.rows) == 2


def test_what_is_missing_names_the_targets_that_can_fetch_it(world):
    world.run("datasets")
    catalog = catalog_of(world)

    missing = catalog.users(catalog.item("dataset", "ds-0001")).missing

    assert "`pbi sync run dataset-users`" in missing
    assert "`pbi sync run user-dataset-users`" in missing
    assert "--get-artifact-users" in missing


def test_a_lake_with_nothing_in_it_holds_no_detail_and_offers_the_administrators_way(
    tmp_path,
):
    empty = Catalog(LakeStore(tmp_path / "lake"), "tenant-1")

    found = empty.details(Item("dataset", "ds-1", "Sales", None, {}))

    assert [d.name for d in found] == [
        "users",
        "datasources",
        "refreshes",
        "parameters",
    ]
    assert all(d.held is None and d.problems == () for d in found)
    # the item says no workspace, so only what needs no workspace can be asked for
    assert [[p.target.name for p in d.providers] for d in found] == [
        ["dataset-users"],
        ["datasources"],
        ["refreshables"],
        [],
    ]


# ---------------------------------------------------------------------------
# choosing a way, and the sync that takes it
# ---------------------------------------------------------------------------


def test_the_administrators_way_is_chosen_when_both_are_stored(world):
    users = details_of(world, "dataset", "ds-0001", "ws-0001")["users"]

    assert choose(users, {Scope.ADMIN, Scope.USER}).target.name == "dataset-users"
    assert choose(users, None).target.name == "dataset-users"  # any account is assumed


def test_the_way_of_a_kind_of_account_that_is_stored_is_chosen(world):
    users = details_of(world, "dataset", "ds-0001", "ws-0001")["users"]
    report_users = details_of(world, "report", "rep-0001", "ws-0001")["users"]

    assert choose(users, {Scope.USER}).target.name == "user-dataset-users"
    assert (
        choose(report_users, {Scope.USER}) is None
    )  # only an administrator reads those
    assert choose(users, set()) is None


def test_a_way_that_was_refused_the_last_time_is_not_chosen_while_another_is_there(
    world,
):
    world.run("datasets")  # the list is in the lake: only the users are asked for
    world.fake.require_admin = True  # a token without the admin claim is refused
    world.run("dataset-users")
    found = details_of(world, "dataset", "ds-0001", "ws-0001")["users"]

    assert found.refused == ("admin.datasets.users?datasetId=ds-0001",)
    assert choose(found, {Scope.ADMIN, Scope.USER}).target.name == "user-dataset-users"
    # when it is all there is, it is tried again
    assert choose(found, {Scope.ADMIN}).target.name == "dataset-users"


def test_the_sync_that_fetches_chosen_ways_names_their_targets_and_the_item_once(world):
    details = details_of(world, "dataset", "ds-0001", "ws-0001")
    chosen = [
        choose(details["users"], {Scope.ADMIN, Scope.USER}),
        choose(details["datasources"], {Scope.ADMIN, Scope.USER}),
        choose(details["parameters"], {Scope.ADMIN, Scope.USER}),
    ]

    options = fetch_options(chosen)

    assert options.targets == (
        "dataset-users",
        "datasources",
        "user-dataset-parameters",
    )
    assert options.only == {"datasetId": ("ds-0001",), "groupId": ("ws-0001",)}


def test_the_sync_of_one_summary_of_the_tenant_names_no_item(world):
    refreshes = details_of(world, "dataset", "ds-0001", "ws-0001")["refreshes"]

    options = fetch_options([choose(refreshes, {Scope.ADMIN})])

    assert options.targets == ("refreshables",) and options.only == {}


def test_a_unit_that_was_only_held_back_is_not_a_problem(world):
    key = providers_of("dataset", "users", "ds-0001", "ws-0001")[0].key
    units = {key: {"status": "deferred", "error": "the quota ran out"}}

    found = {
        d.name: d
        for d in collect(world.store, TENANT, "dataset", "ds-0001", "ws-0001", units)
    }

    assert found["users"].problems == () and found["users"].refused == ()


def test_the_margin_is_half_of_the_smallest_allowance_of_the_operation():
    users = get_endpoint("admin.reports.users")  # 200 an hour
    groups = get_endpoint("admin.groups")  # 50 an hour and 15 a minute
    pages = get_endpoint("user.report_pages")  # no documented quota

    assert has_margin(users, 101) and not has_margin(users, 100)
    assert has_margin(groups, 8) and not has_margin(
        groups, 7
    )  # 15 a minute: half is 7.5
    assert has_margin(pages, 0) and has_margin(pages, None)
    assert has_margin(users, None)
