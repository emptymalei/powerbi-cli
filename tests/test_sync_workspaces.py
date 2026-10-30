"""A sync of chosen workspaces: the items of the other workspaces are not fetched for."""

import pytest
from sync_helpers import World

from pbi_cli.core.registry import Scope
from pbi_cli.core.sync.engine import COMPLETED
from pbi_cli.core.sync.plan import SyncOptions
from pbi_cli.core.sync.targets import TARGETS, Mode, get_target
from pbi_cli.errors import PBIError


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def stored(world, endpoint, placeholder):
    """The ids that the lake holds an answer of ``endpoint`` for."""
    return sorted(
        found.params[placeholder]
        for found in world.store.parameter_sets("tenant-1", endpoint)
    )


# ---------------------------------------------------------------------------
# the administrator's fan-outs over lists of items
# ---------------------------------------------------------------------------


def test_the_users_of_reports_are_fetched_only_for_the_reports_of_the_chosen_workspaces(
    world,
):
    report = world.run("report-users", workspace_ids=("ws-0002", "ws-0004"))

    assert report.status == COMPLETED
    assert stored(world, "admin.reports.users", "reportId") == ["rep-0002", "rep-0004"]
    assert world.fake.count(r"^/admin/reports/[^/]+/users$") == 2
    # the list the reports come from is whole: the workspace of a report is in its rows
    assert world.stored("admin.reports", {}).manifest["rows"] == 6


@pytest.mark.parametrize(
    "target, endpoint, placeholder, workspace, expected",
    [
        ("dataset-users", "admin.datasets.users", "datasetId", "ws-0003", ["ds-0003"]),
        (
            "datasources",
            "admin.datasets.datasources",
            "datasetId",
            "ws-0004",
            ["ds-0004"],
        ),
        (
            "dashboard-users",
            "admin.dashboards.users",
            "dashboardId",
            "ws-0001",
            ["dash-0001"],
        ),
        ("dashboard-users", "admin.dashboards.users", "dashboardId", "ws-0002", []),
        (
            "dataflow-users",
            "admin.dataflows.users",
            "dataflowId",
            "ws-0001",
            ["flow-0001"],
        ),
        (
            "dataflow-datasources",
            "admin.dataflows.datasources",
            "dataflowId",
            "ws-0002",
            [],
        ),
    ],
)
def test_every_kind_of_item_is_limited_by_the_workspace_it_is_in(
    world, target, endpoint, placeholder, workspace, expected
):
    report = world.run(target, workspace_ids=(workspace,))

    assert report.status == COMPLETED
    assert stored(world, endpoint, placeholder) == expected


def test_the_users_of_workspaces_are_fetched_only_for_the_chosen_workspaces(world):
    world.run("group-users", workspace_ids=("ws-0001", "ws-0005"))

    assert stored(world, "admin.groups.users", "groupId") == ["ws-0001", "ws-0005"]
    assert world.stored("admin.groups", {}).manifest["rows"] == 12  # the list is whole


def test_a_sync_that_names_no_workspace_fetches_for_every_item(world):
    world.run("report-users")

    assert len(stored(world, "admin.reports.users", "reportId")) == 6


def test_the_workspaces_and_the_wanted_items_both_have_to_allow(world):
    world.run(
        "report-users",
        workspace_ids=("ws-0002",),
        only={"reportId": ["rep-0001", "rep-0002"]},
    )

    assert stored(world, "admin.reports.users", "reportId") == ["rep-0002"]


def test_a_target_that_is_one_list_is_not_narrowed(world):
    world.run("refreshables", workspace_ids=("ws-0002",))

    assert world.fake.count(r"^/admin/capacities/refreshables$") == 1
    assert world.stored("admin.refreshables", {}).manifest["rows"] == 4


def test_the_plan_counts_the_items_of_the_chosen_workspaces(world):
    world.run("reports")

    plan = world.plan("report-users", workspace_ids=("ws-0002", "ws-0003"))

    child = next(t for t in plan.targets if t.target.name == "report-users")
    assert (child.units, child.todo, child.requests) == (2, 2, 2)


# ---------------------------------------------------------------------------
# rows that do not say which workspace they are in
# ---------------------------------------------------------------------------


def test_rows_without_a_workspace_are_left_out_and_the_run_says_so(world):
    del world.fake.reports[0]["workspaceId"]

    report = world.run("report-users", workspace_ids=("ws-0001", "ws-0002"))

    assert stored(world, "admin.reports.users", "reportId") == ["rep-0002"]
    assert any(
        "1 row(s) of reports have no workspaceId" in note for note in report.notes
    ), report.notes


def test_the_plan_says_so_too(world):
    world.run("reports")
    del world.fake.reports[0]["workspaceId"]
    world.run("reports", force=True)

    plan = world.plan("report-users", workspace_ids=("ws-0001", "ws-0002"))

    child = next(t for t in plan.targets if t.target.name == "report-users")
    assert child.units == 1
    assert any("have no workspaceId" in note for note in child.notes), child.notes


def test_rows_without_a_workspace_are_fetched_when_no_workspace_is_asked_for(world):
    del world.fake.reports[0]["workspaceId"]

    report = world.run("report-users")

    assert len(stored(world, "admin.reports.users", "reportId")) == 6
    assert not any("workspaceId" in note for note in report.notes)


# ---------------------------------------------------------------------------
# a user's fan-outs
# ---------------------------------------------------------------------------


def test_a_user_sync_of_chosen_workspaces_reads_only_those(world):
    world.only_user()

    report = world.run("user-pages", workspace_ids=("ws-0002",))

    assert report.status == COMPLETED
    assert stored(world, "user.group_reports", "groupId") == ["ws-0002"]
    assert stored(world, "user.report_pages", "reportId") == ["rep-0002"]
    assert world.fake.count(r"^/groups/[^/]+/reports$") == 1
    # the list of workspaces the user can see is whole
    assert len(world.stored("user.groups", {}).load()["value"]) == 3


# ---------------------------------------------------------------------------
# how the options and the targets say it
# ---------------------------------------------------------------------------


def test_workspace_ids_narrow_the_requests_about_one_workspace_only():
    options = SyncOptions(workspace_ids=("a", "b"))

    assert options.allows("groupId", "a") and not options.allows("groupId", "c")
    assert options.allows("reportId", "anything")  # not about a workspace
    assert SyncOptions().allows("groupId", "c")  # no workspace named: all are wanted


def test_a_target_says_where_the_workspace_of_a_row_is_when_it_needs_to():
    for target in TARGETS:
        parent = get_target(target.parent) if target.parent else None
        if target.workspace:
            assert target.mode is Mode.FANOUT and parent is not None, target.name
        elif (
            target.mode is Mode.FANOUT
            and parent is not None
            and parent.scope is Scope.ADMIN
            and parent.default
            and parent.name != "groups"
        ):
            raise AssertionError(f"{target.name} fans out over items: where is theirs?")
    assert get_target("report-users").workspace == "workspaceId"
    assert get_target("dataflow-datasources").workspace == "workspaceId"


# ---------------------------------------------------------------------------
# the engine says which accounts there are
# ---------------------------------------------------------------------------


def test_the_engine_knows_which_accounts_are_stored(world):
    assert world.engine.has_account(Scope.ADMIN)
    assert world.engine.profile_of(Scope.USER) == "user-nlm"
    assert world.engine.profile_of(Scope.ADMIN) == "admin-nlm"

    world.only_user()

    assert not world.engine.has_account(Scope.ADMIN)
    assert world.engine.profile_of(Scope.ADMIN) is None
    assert world.engine.has_account(Scope.USER)


def test_the_engine_asks_for_a_profile_by_name(world):
    world.accounts(ana="oid-ana", bob="oid-bob")

    assert world.engine.has_account(Scope.USER, "bob")
    assert world.engine.profile_of(Scope.USER, "bob") == "bob"
    assert world.engine.profile_of(Scope.USER) == "ana"  # the first: the active one
    assert not world.engine.has_account(Scope.USER, "cy")
    assert world.engine.profile_of(Scope.USER, "cy") is None


def test_the_engine_names_the_tenant_of_a_sync(world):
    assert world.engine.tenant(World.options("groups")) == "tenant-1"

    world.engine._client_for = lambda scope, profile=None: (_ for _ in ()).throw(
        PBIError("No profile set")
    )
    with pytest.raises(PBIError):
        world.engine.tenant(World.options("groups"))
