"""Planning and running a plan file against the fake service."""

import threading
from datetime import datetime, timedelta, timezone

import pytest
from sync_helpers import TENANT, World

from pbi_cli.core.planfile import Overrides, PlanFile, PlanFileError
from pbi_cli.core.planrun import PlanRun, merge_quota, merge_reports
from pbi_cli.core.registry import Scope
from pbi_cli.core.scan import ScanFlags, latest_scan
from pbi_cli.core.sync.engine import (
    COMPLETED,
    COMPLETED_WITH_FAILURES,
    INTERRUPTED,
    TOKEN_EXPIRED,
    RunReport,
)
from pbi_cli.core.sync.plan import Plan, QuotaLine
from pbi_cli.core.sync.runners import DONE, FAILED, SKIPPED
from pbi_cli.errors import AuthError, PBIError


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def run_of(world, text, **overrides):
    """A plan run of a plan file, one worker, on the world's clock."""
    overrides.setdefault("workers", 1)
    return PlanRun(
        world.engine,
        world.store,
        PlanFile.parse(text, None),
        overrides=Overrides(**overrides),
        clock=world.clock.now,
    )


def stored(world, endpoint, placeholder):
    return sorted(
        found.params[placeholder]
        for found in world.store.parameter_sets(TENANT, endpoint)
    )


TENANT_AND_USERS = """\
version: 1
tenant: {targets: [groups, reports, datasets, dashboards, dataflows]}
workspaces:
  - {name: "Workspace 2", details: [users], via: admin}
"""


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------


def test_the_steps_for_the_tenant_run_first_and_the_workspaces_are_looked_up_after(
    world,
):
    run = run_of(world, TENANT_AND_USERS)
    steps = []

    report = run.run(on_step=lambda number, step: steps.append((number, step.title)))

    assert report.status == COMPLETED and report.counts[FAILED] == 0
    assert steps == [
        (1, "the tenant (admin-nlm)"),
        (
            2,
            "dashboard-users, dataflow-users, dataset-users, group-users, report-users "
            "for 1 workspace (admin-nlm)",
        ),
    ]
    # the name was found in the list that the first step fetched, and only its items
    assert stored(world, "admin.groups.users", "groupId") == ["ws-0002"]
    assert stored(world, "admin.reports.users", "reportId") == ["rep-0002"]
    assert stored(world, "admin.datasets.users", "datasetId") == ["ds-0002"]
    assert not world.store.parameter_sets(TENANT, "admin.dashboards.users")


def test_the_report_is_one_report_for_all_the_steps(world):
    report = run_of(world, TENANT_AND_USERS).run()

    runs = world.state()["runs"]
    assert len(runs) == 2  # each step is an ordinary run in the state of the lake
    assert (
        report.counts[DONE] == sum(r["counts"].get("done", 0) for r in runs)
        and report.counts[DONE] > 5
    )
    assert report.by_target["group-users"][DONE] == 1
    assert report.by_target["groups"][DONE] == 1
    assert report.started_at <= report.finished_at


def test_a_second_run_fetches_nothing_that_is_still_fresh(world):
    run_of(world, TENANT_AND_USERS).run()
    world.fake.reset_calls()

    again = run_of(world, TENANT_AND_USERS).run()

    assert again.status == COMPLETED and not again.counts[DONE]
    assert again.counts[SKIPPED] > 5
    assert world.fake.calls == []


def test_the_events_of_each_step_reach_the_caller(world):
    events = []

    run_of(world, TENANT_AND_USERS).run(on_event=events.append)

    stages = [e for e in events if e.kind == "stage"]
    assert len(stages) >= 2  # each step has its stages
    assert any(e.kind == "unit" and e.unit.target == "group-users" for e in events)


def test_a_scan_of_the_workspaces_a_pattern_names(world):
    run_of(
        world,
        "version: 1\ntenant: {targets: [groups]}\nworkspaces:\n"
        "  - {name: 'Workspace ?', scan: {lineage: true}}\n",
    ).run()

    posted = world.fake.calls_to(r"getInfo", "POST")
    ids = tuple(f"ws-000{n}" for n in range(1, 10))
    assert len(posted) == 1
    assert sorted(posted[0].body["workspaces"]) == list(ids)
    assert posted[0].query["lineage"] == "true"
    assert latest_scan(world.store, TENANT, ids, ScanFlags(lineage=True)) is not None


def test_a_user_reads_what_only_a_user_can_through_the_account_that_lists_the_workspace(
    world,
):
    world.accounts(ana="oid-ana", bob="oid-bob")
    world.fake.visible_to = {"oid-ana": ["ws-0001", "ws-0002"], "oid-bob": ["ws-0003"]}
    run = run_of(
        world,
        "version: 1\naccounts: {user: [ana, bob]}\nworkspaces:\n"
        "  - {name: 'Workspace 2', details: [pages]}\n"
        "  - {name: 'Workspace 3', details: [pages]}\n",
    )
    steps = []

    report = run.run(on_step=lambda number, step: steps.append(step))

    assert report.status == COMPLETED and report.counts[FAILED] == 0
    assert [(s.options.targets, s.options.user_profile) for s in steps] == [
        (("user-groups",), "ana"),  # who lists what: one request each
        (("user-groups",), "bob"),
        (("user-pages",), "ana"),
        (("user-pages",), "bob"),
    ]
    assert stored(world, "user.report_pages", "reportId") == ["rep-0002", "rep-0003"]
    assert len(world.store.parameter_sets(TENANT, "user.groups")) == 2


def test_a_name_that_matches_nothing_is_a_failure_but_the_rest_is_done(world):
    report = run_of(
        world,
        "version: 1\ntenant: {targets: [groups, reports]}\nworkspaces:\n"
        "  - {name: 'Nothing here', scan: true}\n"
        "  - {name: 'Workspace 2', details: [users], via: admin}\n",
    ).run()

    assert report.status == COMPLETED_WITH_FAILURES
    assert report.counts[FAILED] == 1
    assert report.failures == [
        ("workspaces[0] (Nothing here)", "no workspace is called 'Nothing here'")
    ]
    assert stored(world, "admin.groups.users", "groupId") == ["ws-0002"]  # still done


def test_an_expired_token_ends_the_plan_and_the_next_run_continues(world):
    run = run_of(world, TENANT_AND_USERS)
    world.fake.expire_token_after(2)

    report = run.run()

    assert report.status == TOKEN_EXPIRED
    assert "pbi auth" in report.message
    assert not world.store.parameter_sets(TENANT, "admin.groups.users")

    world.fake.expire_token_after(None)
    again = run.run()

    assert again.status == COMPLETED
    assert stored(world, "admin.groups.users", "groupId") == ["ws-0002"]


def test_the_steps_that_were_not_started_are_counted_when_a_token_expires(world):
    world.accounts(ana="oid-ana", bob="oid-bob")
    run = run_of(
        world,
        "version: 1\naccounts: {user: [ana, bob]}\ntenant: {targets: [user-groups]}\n",
    )
    world.fake.expire_token_after(0)

    report = run.run()

    assert report.status == TOKEN_EXPIRED
    assert any("1 step(s) were not started" in note for note in report.notes)


def test_a_stop_ends_the_plan_between_the_steps(world):
    stop = threading.Event()
    run = run_of(world, TENANT_AND_USERS)

    report = run.run(
        on_step=lambda number, step: stop.set() if number == 1 else None, stop=stop
    )

    assert report.status == INTERRUPTED
    assert not world.store.parameter_sets(TENANT, "admin.groups.users")
    assert report.message


def test_a_stop_before_the_start_does_nothing(world):
    stop = threading.Event()
    stop.set()

    report = run_of(world, TENANT_AND_USERS).run(stop=stop)

    assert report.status == INTERRUPTED and world.fake.calls == []
    assert any("step(s) were not started" in n for n in report.notes)


def test_a_plan_with_nothing_in_it_is_done_at_once(world):
    report = run_of(world, "version: 1\n").run()

    assert report.status == COMPLETED and not report.counts and world.fake.calls == []


def test_the_flags_of_the_command_line_apply_to_every_step(world):
    run_of(world, TENANT_AND_USERS).run()
    world.fake.reset_calls()

    report = run_of(world, TENANT_AND_USERS, force=True).run()

    assert report.counts[DONE] > 5 and not report.counts[SKIPPED]
    assert world.fake.count(r"^/admin/groups$") >= 1


def test_a_plan_that_asks_for_something_needs_an_account(world):
    def nobody(scope, profile=None):
        raise PBIError("No profile set")

    world.engine._client_for = nobody

    with pytest.raises(AuthError, match="No account is stored.*pbi auth -t <token>"):
        run_of(world, TENANT_AND_USERS).run()
    with pytest.raises(AuthError, match="No account is stored"):
        run_of(world, TENANT_AND_USERS).plan()
    # a plan that asks for nothing needs nothing
    assert run_of(world, "version: 1\n").run().status == COMPLETED
    assert world.fake.calls == []


def test_the_accounts_that_are_not_stored_are_said_before_anything_runs(world):
    world.only_user()

    with pytest.raises(PlanFileError, match="accounts.admin: no token is stored"):
        run_of(world, "version: 1\naccounts: {admin: nope}\n").run()
    assert world.fake.calls == []


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


def test_the_plan_has_one_plan_for_each_step_and_one_quota_for_all(world):
    world.run("groups", "reports", "datasets", "dashboards", "dataflows")
    run = run_of(world, TENANT_AND_USERS)

    plan = run.plan()

    assert [s.title for s, _ in plan.steps] == [
        "the tenant (admin-nlm)",
        "dashboard-users, dataflow-users, dataset-users, group-users, report-users "
        "for 1 workspace (admin-nlm)",
    ]
    assert plan.accounts == ["admin-nlm (admin)"]
    assert plan.unmatched == []
    assert (
        plan.requests == 3
    )  # the users of the workspace, of a report and of a dataset
    by_endpoint = {line.endpoint: line.requests for line in plan.quota}
    assert by_endpoint["admin.reports.users"] == 1
    assert by_endpoint["admin.groups.users"] == 1


def test_the_plan_before_the_first_sync_says_the_names_are_not_known_yet(world):
    plan = run_of(world, TENANT_AND_USERS).plan()

    assert len(plan.steps) == 1  # the tenant: the workspaces wait for its list
    assert plan.unmatched[0][0] == "workspaces[0] (Workspace 2)"
    assert "no list of workspaces yet" in plan.unmatched[0][1]


def test_the_plan_says_that_nobody_was_asked_yet_and_then_that_nobody_lists_it(world):
    world.accounts(ana="oid-ana")
    world.fake.visible_to = {"oid-ana": ["ws-0001"]}
    run = run_of(
        world,
        "version: 1\naccounts: {user: [ana]}\nworkspaces:\n"
        "  - {id: ws-0002, details: [pages]}\n",
    )

    before = run.plan()
    world.run("user-groups", user_profile="ana")  # what the first step of the plan does
    after = run.plan()

    assert any(
        "not known yet" in n and "for ana" in n for n in before.notes
    ), before.notes
    assert not any("not known yet" in n for n in after.notes)
    assert any(
        "no user account of the plan lists ws-0002 (ana)" in n for n in after.notes
    ), after.notes


def test_the_run_assigns_a_workspace_to_the_account_that_lists_it_once_it_knows(world):
    world.accounts(ana="oid-ana", bob="oid-bob")
    world.fake.visible_to = {"oid-ana": ["ws-0001"], "oid-bob": ["ws-0002"]}
    run = run_of(
        world,
        "version: 1\naccounts: {user: [ana, bob]}\nworkspaces:\n"
        "  - {id: ws-0002, details: [pages]}\n",
    )
    steps = []

    report = run.run(on_step=lambda number, step: steps.append(step))

    assert report.status == COMPLETED and report.counts[FAILED] == 0
    assert [(s.options.targets, s.options.user_profile) for s in steps][-1] == (
        ("user-pages",),
        "bob",  # known only after the lists were fetched
    )
    assert not any("not known yet" in n for n in report.notes)


def test_the_plan_adds_up_the_requests_and_says_that_steps_share_lists(world):
    plan = run_of(
        world,
        "version: 1\ntenant: {targets: [groups, reports]}\nworkspaces:\n"
        "  - {id: ws-0001, details: [users], via: admin}\n",
    ).plan()

    reports = next(l for l in plan.quota if l.endpoint == "admin.reports")
    assert reports.requests == 2  # the tenant step and the details step each count it
    assert any("share lists" in note for note in plan.notes)


def test_the_plan_counts_with_the_flags_of_the_command_line(world):
    run_of(world, TENANT_AND_USERS).run()

    normal = run_of(world, TENANT_AND_USERS).plan()
    forced = run_of(world, TENANT_AND_USERS, force=True).plan()

    assert normal.requests == 0
    assert all(t.todo == 0 for _, p in normal.steps for t in p.targets)
    assert forced.requests > 0
    assert all(t.fresh == 0 for _, p in forced.steps for t in p.targets)


def test_a_machine_with_only_named_profiles_finds_the_tenant_through_them(world):
    def client_for(scope, profile=None):
        if scope is Scope.ADMIN or profile is None:  # no active profile at all
            raise PBIError("No active profile set")
        return world.user

    world.engine._client_for = client_for
    run = run_of(
        world,
        "version: 1\naccounts: {user: [svc]}\nworkspaces:\n"
        "  - {name: 'Workspace 2', details: [pages], via: svc}\n",
    )

    first, later = run.steps()

    assert first == [] and later.unmatched[0][0] == "workspaces[0] (Workspace 2)"
    assert "no list of workspaces yet" in later.unmatched[0][1]


def test_a_plan_with_one_step_has_nothing_to_say_about_sharing(world):
    plan = run_of(world, "version: 1\ntenant: {targets: [groups]}\n").plan()

    assert not any("share lists" in note for note in plan.notes)


def test_the_plan_reads_the_lake_and_calls_nothing(world):
    run_of(world, TENANT_AND_USERS).plan()

    assert world.fake.calls == []


def test_the_steps_are_shown_as_the_lake_is_now(world):
    world.run("groups")

    first, later = run_of(world, TENANT_AND_USERS).steps()

    assert [s.title for s in first] == ["the tenant (admin-nlm)"]
    assert len(later.steps) == 1 and later.unmatched == []


# ---------------------------------------------------------------------------
# the pieces
# ---------------------------------------------------------------------------


def line(endpoint, requests, left=None, quota="200/h", hourly=200):
    return QuotaLine(endpoint, requests, quota, left, hourly)


def test_the_requests_of_plans_are_added_up_per_operation_in_the_order_of_the_registry():
    first = Plan(
        "t", [], [line("admin.reports.users", 3, 100), line("admin.groups", 1, 10)]
    )
    second = Plan("t", [], [line("admin.groups", 2, 8), line("admin.apps", 1, None)])

    merged = merge_quota([first, second])

    assert [(l.endpoint, l.requests, l.left) for l in merged] == [
        ("admin.groups", 3, 8),  # the least that is left
        ("admin.apps", 1, None),
        ("admin.reports.users", 3, 100),
    ]
    assert first.quota[1].requests == 1  # the plans themselves are not changed


def test_a_quota_that_is_known_in_one_plan_only_is_kept():
    merged = merge_quota(
        [
            Plan("t", [], [line("admin.groups", 1, None)]),
            Plan("t", [], [line("admin.groups", 1, 5)]),
        ]
    )

    assert merged[0].left == 5 and merged[0].requests == 2


def report(status=COMPLETED, done=0, failed=0, run_id="r"):
    result = RunReport(
        run_id=run_id, started_at=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    result.status = status
    result.counts.update({DONE: done, FAILED: failed})
    return result


def test_the_reports_of_the_steps_are_added_up():
    first, second = report(done=3, run_id="a"), report(done=4, failed=1, run_id="b")
    first.by_target["groups"] = first.counts.__class__({DONE: 3})
    second.by_target["groups"] = second.counts.__class__({DONE: 1})
    second.failures.append(("unit", "boom"))
    second.deferred.append(("late", 5.0))
    second.cancelled = 2
    second.notes.append("note")
    first.notes.append("note")

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    merged = merge_reports(
        [first, second], started=start, finished=start + timedelta(minutes=1)
    )

    assert merged.counts[DONE] == 7 and merged.counts[FAILED] == 1
    assert merged.by_target["groups"][DONE] == 4
    assert merged.failures == [("unit", "boom")] and merged.deferred == [("late", 5.0)]
    assert merged.cancelled == 2 and merged.notes == ["note"]
    assert merged.run_id == "b" and merged.finished_at - merged.started_at == timedelta(
        minutes=1
    )
    assert (
        merged.status == COMPLETED_WITH_FAILURES
    )  # a failure in a step is one in the plan


def test_a_plan_that_stops_for_a_token_says_whose_it_was():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    done, expired = report(done=2), report(TOKEN_EXPIRED)
    expired.group, expired.profile = "user", "svc"

    merged = merge_reports(
        [done, expired], started=start, finished=start, halted=TOKEN_EXPIRED
    )
    stopped = merge_reports(
        [done, expired], started=start, finished=start, halted=INTERRUPTED
    )

    assert (merged.group, merged.profile) == ("user", "svc")
    assert (stopped.group, stopped.profile) == (
        None,
        None,
    )  # not a token that stopped it


def test_a_plan_is_complete_when_every_step_is():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    assert (
        merge_reports([report(done=1)], started=start, finished=start).status
        == COMPLETED
    )
    assert merge_reports([], started=start, finished=start).status == COMPLETED
    assert merge_reports([], started=start, finished=start).run_id == ""
    assert (
        merge_reports(
            [report(COMPLETED_WITH_FAILURES)], started=start, finished=start
        ).status
        == COMPLETED_WITH_FAILURES
    )


def test_entries_that_matched_nothing_are_failures_and_a_halt_is_what_stopped_it():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    merged = merge_reports(
        [report(done=1)],
        started=start,
        finished=start,
        unmatched=[("workspaces[0] (A: B)", "no workspace is called 'A: B'")],
        notes=["a note"],
        halted=TOKEN_EXPIRED,
        message="Sign in again",
        skipped=2,
    )

    assert merged.status == TOKEN_EXPIRED and merged.message == "Sign in again"
    assert merged.failures == [
        ("workspaces[0] (A: B)", "no workspace is called 'A: B'")
    ]
    assert merged.counts[FAILED] == 1
    assert (
        merged.notes[0] == "a note" and "2 step(s) were not started" in merged.notes[1]
    )
