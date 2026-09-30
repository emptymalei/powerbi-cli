"""Planning a sync: units, dry runs from what the lake holds, and quota estimates."""

import shutil
from datetime import timedelta

import pytest
from sync_helpers import TENANT, World

import pbi_cli.core.sync.plan as plan_module
from pbi_cli.core.registry import Scope
from pbi_cli.core.scan import ScanFlags
from pbi_cli.core.store import params_hash
from pbi_cli.core.sync.plan import (
    EVENTS,
    MAX_DAYS,
    MAX_WORKERS,
    MODIFIED,
    SCAN,
    SNAPSHOT,
    QuotaLine,
    SyncOptions,
    unit_key,
)
from pbi_cli.errors import PBIError


@pytest.fixture
def world(tmp_path):
    return World(tmp_path, workspaces=250, reports=6, datasets=4)


def by_name(plan):
    return {p.target.name: p for p in plan.targets}


def planner(world, *targets, **options):
    return world.engine._session(world.options(*targets, **options)).planner


# ---------------------------------------------------------------------------
# options
# ---------------------------------------------------------------------------


def test_the_default_options_are_valid():
    options = SyncOptions()

    assert options.workers == 4 and options.days == MAX_DAYS
    assert options.summary()["scan_flags"]["lineage"] == "false"
    assert options.summary()["max_age"] is None
    assert SyncOptions(max_age=timedelta(hours=2)).summary()["max_age"] == 7200.0


@pytest.mark.parametrize(
    "options, message",
    [
        ({"workers": 0}, "workers must be between 1 and 16"),
        ({"workers": MAX_WORKERS + 1}, "workers must be between 1 and 16"),
        ({"days": 0}, "days must be between 1 and 28"),
        ({"days": MAX_DAYS + 1}, "days must be between 1 and 28"),
        ({"scan_interval": 0}, "greater than 0"),
        ({"scan_timeout": -1}, "greater than 0"),
        ({"max_wait": -1.0}, "cannot be negative"),
        ({"max_age": timedelta(seconds=-1)}, "cannot be negative"),
    ],
)
def test_options_that_make_no_sense_are_refused(options, message):
    with pytest.raises(PBIError, match=message):
        SyncOptions(**options)


def test_waiting_as_long_as_needed_is_allowed():
    assert SyncOptions(max_wait=None).max_wait is None


# ---------------------------------------------------------------------------
# units
# ---------------------------------------------------------------------------


def test_a_unit_is_named_after_its_request():
    assert unit_key("admin.groups", {}) == "admin.groups"
    assert (
        unit_key("user.report_pages", {"reportId": "r1", "groupId": "g1"})
        == "user.report_pages?groupId=g1&reportId=r1"
    )


def test_the_plain_targets_start_with_one_unit_each(world):
    units = planner(world).roots()

    assert [u.endpoint for u in units] == [
        "admin.groups",
        "admin.apps",
        "admin.capacities",
        "admin.reports",
        "admin.datasets",
        "admin.dashboards",
        "admin.dataflows",
    ]
    assert all(u.kind == SNAPSHOT and u.scope is Scope.ADMIN for u in units)
    assert not any(u.has_children for u in units)
    assert all(u.uses == (u.endpoint,) for u in units)


def test_a_unit_knows_whether_other_units_are_made_from_its_rows(world):
    units = {u.target: u for u in planner(world, "report-users").roots()}

    assert units["reports"].has_children is True
    assert "report-users" not in units  # it waits for the rows of reports


def test_event_days_start_with_the_oldest(world):
    units = planner(world, "activity", days=3).roots()

    assert [u.kind for u in units] == [EVENTS] * 3
    assert [u.day for u in units] == [
        world.clock.now().date() - timedelta(days=2),
        world.clock.now().date() - timedelta(days=1),
        world.clock.now().date(),
    ]
    assert units[0].key == f"admin.activityevents@{units[0].day}"


def test_a_scan_starts_with_the_list_of_workspaces(world):
    (unit,) = planner(world, "scan").roots()

    assert unit.kind == MODIFIED and unit.endpoint == "admin.workspaces.modified"
    assert unit.has_children and unit.uses == ("admin.workspaces.modified",)
    assert unit.params == {
        "excludePersonalWorkspaces": False,
        "excludeInActiveWorkspaces": False,
    }


def test_the_workspaces_are_cut_in_batches_of_100(world):
    p = planner(world, "scan")
    (listing,) = p.roots()

    batches = p.expand(listing, [f"ws-{n:04d}" for n in range(250, 0, -1)])

    assert [len(b.workspace_ids) for b in batches] == [100, 100, 50]
    assert (
        batches[0].workspace_ids[0] == "ws-0001"
    )  # sorted, so that batches are stable
    assert all(b.kind == SCAN and b.endpoint == "admin.scan.result" for b in batches)
    assert len({b.key for b in batches}) == 3
    assert batches[0].uses == (
        "admin.scan.start",
        "admin.scan.status",
        "admin.scan.result",
    )
    assert p.expand(listing, []) == []


def test_a_fan_out_makes_a_unit_for_each_row_and_never_twice(world):
    p = planner(world, "report-users")
    (reports,) = [u for u in p.roots() if u.target == "reports"]
    rows = [
        {"id": "r1"},
        {"id": "r2"},
        {"id": "r1"},
        {"name": "no id"},
        "junk",
        {"id": ""},
    ]

    units = p.expand(reports, rows)

    assert [u.key for u in units] == [
        "admin.reports.users?reportId=r1",
        "admin.reports.users?reportId=r2",
    ]
    assert units[0].params == {"reportId": "r1"} and units[0].target == "report-users"


def test_a_fan_out_takes_the_path_from_the_parent_request_or_the_row(world):
    p = planner(world, "user-pages")
    groups, *_ = [u for u in p.roots() if u.target == "user-groups"]
    (report_list,) = p.expand(groups, [{"id": "g1"}])

    (pages,) = p.expand(report_list, [{"id": "r9"}])

    assert report_list.params == {"groupId": "g1"}
    assert pages.params == {"groupId": "g1", "reportId": "r9"}
    assert pages.endpoint == "user.report_pages" and pages.scope is Scope.USER


# ---------------------------------------------------------------------------
# a dry run
# ---------------------------------------------------------------------------


def test_an_empty_lake_needs_one_request_per_plain_target(world):
    plan = world.plan()

    assert plan.tenant == TENANT
    plans = by_name(plan)
    assert list(plans) == [
        "groups",
        "apps",
        "capacities",
        "reports",
        "datasets",
        "dashboards",
        "dataflows",
    ]
    assert all(
        (p.units, p.fresh, p.todo, p.requests) == (1, 0, 1, 1) for p in plans.values()
    )
    assert plan.requests == 7
    assert world.fake.calls == []  # a plan never asks the API


def test_the_quota_of_each_operation_is_shown_against_what_is_left(world):
    plan = world.plan("groups", "reports")

    lines = {line.endpoint: line for line in plan.quota}
    assert list(lines) == [
        "admin.groups",
        "admin.reports",
    ]  # in the order of the registry
    assert lines["admin.groups"].quota == "50/h, 15/min"
    assert lines["admin.groups"].left == 15  # the tightest window
    assert lines["admin.groups"].fits and lines["admin.groups"].hours is None


def test_what_the_quota_counters_hold_is_taken_into_account(world):
    for n in range(10):
        world.admin.request("admin.groups", {"$top": n + 1})

    line = world.plan("groups").quota[0]

    assert line.left == 5 and line.fits


def test_everything_is_fresh_after_a_sync(world):
    world.run()
    world.fake.reset_calls()

    plan = world.plan()

    assert all(
        (p.units, p.fresh, p.todo, p.requests) == (1, 1, 0, 0) for p in plan.targets
    )
    assert plan.quota == [] and plan.requests == 0 and world.fake.calls == []


def test_what_has_gone_stale_is_to_do_again(world):
    world.run()
    world.clock.advance(hours=25)

    plan = world.plan()

    assert all(p.todo == 1 for p in plan.targets)


def test_force_makes_everything_to_do_and_max_age_decides_freshness(world):
    world.run()
    world.clock.advance(minutes=30)

    assert all(p.todo == 1 for p in world.plan(force=True).targets)
    assert all(p.todo == 0 for p in world.plan(max_age=timedelta(hours=1)).targets)
    assert all(p.todo == 1 for p in world.plan(max_age=timedelta(minutes=10)).targets)


def test_a_fan_out_is_unknown_until_its_parent_is_in_the_lake(world):
    plans = by_name(world.plan("report-users"))

    assert plans["reports"].implied and plans["reports"].units == 1
    unknown = plans["report-users"]
    assert unknown.units is None and unknown.requests is None and unknown.todo is None
    assert "depends on reports, which has nothing in the lake yet" in unknown.notes


def test_a_fan_out_is_counted_from_the_rows_in_the_lake(world):
    world.run("reports")

    plans = by_name(world.plan("report-users"))

    assert plans["reports"].fresh == 1
    assert (plans["report-users"].units, plans["report-users"].requests) == (6, 6)
    assert plans["report-users"].notes == []  # the parent is fresh: the count is firm


def test_a_fan_out_from_a_stale_parent_says_the_count_may_change(world):
    world.run("reports")
    world.clock.advance(hours=25)

    plans = by_name(world.plan("report-users"))

    assert plans["reports"].todo == 1 and plans["report-users"].units == 6
    assert any(
        "worked out from the reports stored earlier" in n
        for n in plans["report-users"].notes
    )


def test_a_fan_out_that_is_done_is_fresh_unit_by_unit(world):
    world.run("report-users")
    world.fake.reset_calls()

    plans = by_name(world.plan("report-users"))

    assert (plans["report-users"].units, plans["report-users"].fresh) == (6, 6)
    assert plans["report-users"].requests == 0


def test_a_chain_of_fan_outs_is_unknown_all_the_way_down(world):
    plans = by_name(world.plan("user-pages"))

    assert plans["user-groups"].units == 1
    assert plans["user-reports"].units is None and plans["user-pages"].units is None
    assert (
        "depends on user-reports, which has nothing in the lake yet"
        in plans["user-pages"].notes
    )


def test_a_chain_of_fan_outs_is_counted_from_the_lake(world):
    world.run("user-pages")

    plans = by_name(world.plan("user-pages"))

    assert [
        (plans[n].units, plans[n].fresh)
        for n in ("user-groups", "user-reports", "user-pages")
    ] == [
        (1, 1),
        (3, 3),
        (3, 3),
    ]


def test_some_parents_without_rows_make_a_count_that_is_only_a_minimum(world):
    world.run("user-reports")  # the reports of three groups are stored
    third = params_hash({"groupId": "ws-0003"})
    shutil.rmtree(
        world.store.root
        / f"tenant={TENANT}"
        / "endpoint=user_group_reports"
        / f"params={third}"
    )

    plans = by_name(world.plan("user-pages"))

    assert plans["user-pages"].units == 2
    assert any(
        "1 of the user-reports units have nothing in the lake yet" in n
        for n in plans["user-pages"].notes
    )


def test_a_request_needs_as_many_pages_as_the_last_answer_had(tmp_path):
    world = World(tmp_path, workspaces=5001, reports=0, datasets=0)
    world.run("groups")
    world.clock.advance(hours=25)

    plan = world.plan("groups")

    assert plan.quota[0].requests == 2  # two pages last time


# -- scans -----------------------------------------------------------------------------


def test_a_scan_needs_the_list_of_workspaces_to_be_estimated(world):
    plan = by_name(world.plan("scan"))["scan"]

    assert plan.units is None and plan.requests is None
    assert any(
        "`groups` is not in the lake yet" in n or "(groups) is not in the lake yet" in n
        for n in plan.notes
    )


def test_a_full_scan_is_estimated_from_the_workspaces_in_the_lake(world):
    world.run("groups")

    plan = world.plan("scan")

    scan = by_name(plan)["scan"]
    assert scan.units == 1 + 3 and scan.requests == 1 + 3 * 5
    lines = {line.endpoint: line.requests for line in plan.quota}
    assert lines == {
        "admin.workspaces.modified": 1,
        "admin.scan.start": 3,
        "admin.scan.status": 9,
        "admin.scan.result": 3,
    }
    assert any("250 are in the stored list of workspaces" in n for n in scan.notes)


def test_an_incremental_scan_cannot_be_estimated(world):
    world.run("scan")

    scan = by_name(world.plan("scan"))["scan"]

    assert scan.units is None and any("incremental" in n for n in scan.notes)


def test_a_full_scan_that_is_done_is_fresh_except_for_the_list(world):
    world.run("scan")
    world.run("groups")

    scan = by_name(world.plan("scan", full_scan=True))["scan"]

    assert (scan.units, scan.fresh, scan.todo) == (4, 3, 1)


# -- events ----------------------------------------------------------------------------


def test_event_days_are_fresh_when_complete_or_recently_read(tmp_path):
    world = World(tmp_path, event_days=3, events_per_day=5)
    world.run("activity", days=3)

    assert by_name(world.plan("activity", days=3))["activity"].fresh == 3
    world.clock.advance(hours=3)
    plan = by_name(world.plan("activity", days=3))["activity"]
    assert (plan.units, plan.fresh, plan.todo) == (
        3,
        1,
        2,
    )  # the complete day stays fresh
    assert by_name(world.plan("activity", days=3, force=True))["activity"].fresh == 1


def test_a_day_needs_a_request_per_thousand_events_it_had(tmp_path):
    world = World(tmp_path, event_days=0, events_page_size=5000)
    yesterday = world.clock.now().date() - timedelta(days=1)
    world.fake.add_events(yesterday, 2500)
    world.run("activity", days=2)

    p = planner(world, "activity", days=2)
    unit = next(u for u in p.roots() if u.day == yesterday)

    assert p.requests(unit) == {"admin.activityevents": 3}
    unread = next(
        u for u in p.roots() if u.day != yesterday
    )  # today: nothing stored yet
    assert p.requests(unread) == {"admin.activityevents": 1}


# -- quota lines -----------------------------------------------------------------------


def test_a_quota_line_tells_whether_it_fits_and_when_it_would(world):
    fits = QuotaLine("admin.groups", 5, "50/h", left=10, hourly=50)
    free = QuotaLine("user.apps", 500, "", left=None)
    tight = QuotaLine("admin.reports.users", 450, "200/h", left=200, hourly=200)
    no_hour = QuotaLine("admin.groups", 20, "15/min", left=15, hourly=None)

    assert fits.fits and fits.hours is None
    assert free.fits and free.hours is None
    assert not tight.fits and tight.hours == 2  # 250 more, at 200 an hour
    assert not no_hour.fits and no_hour.hours is None


def test_a_fan_out_bigger_than_the_quota_says_how_long_it_takes(tmp_path):
    world = World(tmp_path, reports=450)
    world.run("reports")

    line = [
        l
        for l in world.plan("report-users").quota
        if l.endpoint == "admin.reports.users"
    ][0]

    assert (line.requests, line.left, line.hourly) == (450, 200, 200)
    assert not line.fits and line.hours == 2


# ---------------------------------------------------------------------------
# where an incremental scan continues from
# ---------------------------------------------------------------------------


def set_baseline(world, ago, flags=ScanFlags()):
    p = planner(world, "scan")
    p._state.set_scan_baseline(flags, p.coverage(), world.clock.now() - ago)
    return p


def test_there_is_nothing_to_continue_from_without_an_earlier_scan(world):
    assert planner(world, "scan").modified_since() is None


def test_an_incremental_scan_looks_back_an_hour_before_the_last_one(world):
    p = set_baseline(world, timedelta(hours=5))

    assert p.modified_since() == world.clock.now() - timedelta(hours=6)


def test_the_window_the_api_accepts_is_respected(world, monkeypatch):
    monkeypatch.setattr(plan_module, "MODIFIED_OVERLAP", timedelta(0))
    p = set_baseline(world, timedelta(minutes=5))  # far too recent for the API

    assert p.modified_since() == world.clock.now() - plan_module.MODIFIED_MIN_AGE
    assert set_baseline(world, timedelta(days=40)).modified_since() is None  # too old


def test_a_full_scan_or_force_leaves_no_starting_point(world):
    set_baseline(world, timedelta(hours=5))

    assert planner(world, "scan", full_scan=True).modified_since() is None
    assert planner(world, "scan", force=True).modified_since() is None
    assert planner(world, "scan").modified_since() is not None
