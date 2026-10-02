"""The sync engine: snapshots, fan-outs, and what happens when things go wrong."""

import threading
import time
from datetime import timedelta

import pytest
from core_helpers import make_client, make_token
from sync_helpers import TENANT, World

from pbi_cli.core.registry import Scope
from pbi_cli.core.sync.engine import (
    COMPLETED,
    COMPLETED_WITH_FAILURES,
    INTERRUPTED,
    TOKEN_EXPIRED,
    SyncEngine,
)
from pbi_cli.core.sync.plan import SNAPSHOT
from pbi_cli.core.sync.runners import DEFERRED, DONE, FAILED, RUNNERS, SKIPPED
from pbi_cli.errors import AuthError, PBIError

PLAIN = [
    "admin.groups",
    "admin.apps",
    "admin.capacities",
    "admin.reports",
    "admin.datasets",
    "admin.dashboards",
    "admin.dataflows",
]
USERS = r"^/admin/reports/[^/]+/users$"


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------


def test_a_plain_sync_fetches_the_plain_targets_into_the_lake(world):
    report = world.run()

    assert report.status == COMPLETED
    assert report.counts[DONE] == 7 and not report.counts[FAILED]
    for endpoint in PLAIN:
        assert world.stored(endpoint) is not None, endpoint
    assert len(world.stored("admin.groups").load()["value"]) == 12
    assert world.stored("admin.groups").manifest["profile"] == "admin-nlm"
    assert world.fake.count(r"^/admin/groups$") == 1


def test_a_list_is_read_page_by_page(tmp_path):
    world = World(tmp_path, workspaces=5001, reports=0, datasets=0)

    world.run("groups")

    assert world.fake.count(r"^/admin/groups$") == 2
    stored = world.stored("admin.groups")
    assert len(stored.load()["value"]) == 5001 and stored.manifest["pages"] == 2


def test_a_second_sync_skips_what_is_fresh(world):
    world.run()
    world.fake.reset_calls()

    report = world.run()

    assert report.counts[SKIPPED] == 7 and not report.counts[DONE]
    assert world.fake.calls == []
    assert world.state()["runs"][-1]["counts"]["skipped"] == 7


def test_what_has_gone_stale_is_fetched_again(world):
    world.run()
    world.clock.advance(hours=25)
    world.fake.reset_calls()

    report = world.run()

    assert report.counts[DONE] == 7
    assert len(world.store.versions(TENANT, "admin.groups", {})) == 2


def test_force_fetches_again_what_is_fresh(world):
    world.run()
    world.fake.reset_calls()

    report = world.run(force=True)

    assert report.counts[DONE] == 7 and world.fake.count(r"^/admin/") == 7


def test_max_age_decides_what_is_fresh(world):
    world.run()
    world.clock.advance(minutes=30)

    young = world.run(max_age=timedelta(hours=1))
    old = world.run(max_age=timedelta(minutes=10))

    assert young.counts[SKIPPED] == 7 and old.counts[DONE] == 7


def test_the_targets_that_were_named_are_the_only_ones(world):
    report = world.run("apps", "groups")

    assert sorted(report.by_target) == ["apps", "groups"]
    assert world.stored("admin.reports") is None


# ---------------------------------------------------------------------------
# fan-outs
# ---------------------------------------------------------------------------


def test_a_fan_out_brings_its_parent_and_asks_once_per_row(world):
    report = world.run("report-users")

    assert report.status == COMPLETED
    assert report.by_target["reports"][DONE] == 1
    assert report.by_target["report-users"][DONE] == 6
    assert world.fake.count(USERS) == 6
    stored = world.stored("admin.reports.users", {"reportId": "rep-0003"})
    assert stored.load()["value"][0]["emailAddress"] == "ann-rep-0003@example.com"
    assert "groups" not in report.by_target


def test_a_fan_out_uses_a_fresh_parent_without_asking_for_it_again(world):
    world.run("reports")
    world.fake.reset_calls()

    report = world.run("report-users")

    assert report.by_target["reports"][SKIPPED] == 1
    assert world.fake.count(r"^/admin/reports$") == 0 and world.fake.count(USERS) == 6


def test_a_unit_that_fails_is_recorded_and_the_others_go_on(world):
    world.fake.fail("GET", r"/admin/reports/rep-0003/users", 404)

    report = world.run("report-users")

    assert report.status == COMPLETED_WITH_FAILURES
    assert report.counts[DONE] == 6 and report.counts[FAILED] == 1
    ((key, message),) = report.failures
    assert key == "admin.reports.users?reportId=rep-0003"
    assert "not found (404)" in message
    record = world.state()["units"][key]
    assert record["status"] == "failed" and record["attempts"] == 1
    assert record["target"] == "report-users"


def test_only_what_failed_is_tried_again_and_then_forgotten(world):
    world.fake.fail("GET", r"/admin/reports/rep-0003/users", 404)
    world.run("report-users")
    world.fake.clear_faults()
    world.fake.reset_calls()

    report = world.run("report-users")

    assert report.status == COMPLETED
    assert world.fake.count(USERS) == 1
    assert report.counts[SKIPPED] == 6 and report.counts[DONE] == 1
    assert world.state()["units"] == {}


def test_an_operation_that_keeps_answering_403_is_dropped_after_a_few_tries(tmp_path):
    world = World(tmp_path, reports=10)
    world.fake.fail("GET", r"/admin/reports/[^/]+/users", 403)

    report = world.run("report-users")

    assert world.fake.count(USERS) == 3
    assert len(report.failures) == 10
    messages = [message for _, message in report.failures]
    assert sum("forbidden (403)" in m for m in messages) == 3
    assert sum("skipped: Power BI keeps answering 403" in m for m in messages) == 7
    assert sum("administrator" in m for m in messages) == 10


def test_403_of_a_user_are_not_a_reason_to_stop(tmp_path):
    world = World(tmp_path)
    world.fake.user_workspace_ids = [w["id"] for w in world.fake.workspaces[:6]]
    world.fake.fail("GET", r"^/groups/[^/]+/reports$", 403)

    report = world.run("user-reports")

    assert world.fake.count(r"^/groups/[^/]+/reports$") == 6
    assert len(report.failures) == 6


def test_a_fan_out_is_not_fetched_when_its_parent_failed(world):
    world.fake.fail("GET", r"^/admin/reports$", 500)

    report = world.run("report-users")

    assert report.status == COMPLETED_WITH_FAILURES
    assert world.fake.count(USERS) == 0
    assert any(
        "report-users was not fetched: reports did not finish" in n
        for n in report.notes
    )


def test_a_chain_of_fan_outs_fills_the_path_from_the_right_rows(world):
    report = world.run("user-pages")

    assert report.status == COMPLETED
    assert [
        report.by_target[t][DONE] for t in ("user-groups", "user-reports", "user-pages")
    ] == [
        1,
        3,
        3,
    ]
    pages = sorted(c.path for c in world.fake.calls_to(r"/pages$"))
    assert pages == [
        "/groups/ws-0001/reports/rep-0001/pages",
        "/groups/ws-0002/reports/rep-0002/pages",
        "/groups/ws-0003/reports/rep-0003/pages",
    ]
    assert world.stored(
        "user.report_pages", {"groupId": "ws-0002", "reportId": "rep-0002"}
    )
    assert world.stored("user.groups").manifest["profile"] == "user-nlm"


def test_a_row_without_the_field_that_fills_the_path_is_left_out(world):
    world.fake.reports[2].pop("id")  # a damaged row

    report = world.run("report-users")

    assert report.counts[DONE] == 1 + 5 and not report.failures


def test_duplicate_rows_are_fetched_once(world):
    world.fake.reports.append(dict(world.fake.reports[0]))

    world.run("report-users")

    assert world.fake.count(USERS) == 6


# ---------------------------------------------------------------------------
# when things go wrong
# ---------------------------------------------------------------------------


def test_an_expired_token_stops_the_run_and_the_next_run_continues(world):
    world.fake.expire_token_after(4)  # the list of reports and three users are answered

    report = world.run("report-users")

    assert report.status == TOKEN_EXPIRED
    assert "Sign in again" in report.message and "pbi auth" in report.message
    assert report.counts[DONE] == 4 and not report.counts[FAILED]
    assert len(world.fake.calls) == 5  # one request with the dead token, then nothing
    assert world.state()["runs"][-1]["status"] == "token_expired"

    world.fake.expire_token_after(None)
    world.fake.reset_calls()
    again = world.run("report-users")

    assert again.status == COMPLETED
    assert world.fake.count(USERS) == 3 and world.fake.count(r"^/admin/reports$") == 0
    assert again.counts[SKIPPED] == 4 and again.counts[DONE] == 3


def test_a_token_that_is_expired_before_the_start_stops_the_run_at_once(world):
    world.fake.expire_token_after(0)

    report = world.run()

    assert report.status == TOKEN_EXPIRED and report.counts[DONE] == 0
    assert len(world.fake.calls) == 1  # the first request found out; the others waited


def test_a_quota_that_is_used_up_defers_the_rest_without_asking(tmp_path):
    world = World(tmp_path, reports=210)  # admin.reports.users allows 200 an hour

    report = world.run("report-users", max_wait=0.0)

    assert report.status == COMPLETED
    assert report.counts[DONE] == 1 + 200 and report.counts[DEFERRED] == 10
    assert world.fake.count(USERS) == 200  # the quota counters failed the 10 locally
    assert len(report.deferred) == 10
    assert 3500 < report.retry_after <= 3600
    deferred = world.state()["units"]
    assert len(deferred) == 10 and {u["status"] for u in deferred.values()} == {
        "deferred"
    }

    world.clock.advance(seconds=3601)
    world.fake.reset_calls()
    again = world.run("report-users", max_wait=0.0)

    assert again.counts[DONE] == 10 and again.counts[SKIPPED] == 1 + 200
    assert world.fake.count(USERS) == 10
    assert world.state()["units"] == {}


def test_a_short_wait_for_quota_is_waited_out(tmp_path):
    world = World(tmp_path)
    # admin.groups allows 15 requests a minute: use them up, then run with a wait
    for n in range(15):
        world.admin.request("admin.groups", {"$top": n + 1})

    report = world.run("groups", max_wait=120.0)

    assert report.status == COMPLETED and report.counts[DONE] == 1
    assert sum(world.clock.slept) >= 1  # it waited for the window to move on


def test_an_unexpected_error_fails_the_unit_and_the_run_goes_on(world, monkeypatch):
    real = RUNNERS[SNAPSHOT]

    def runner(ctx, unit):
        if unit.target == "apps":
            raise ValueError("boom")
        return real(ctx, unit)

    monkeypatch.setitem(RUNNERS, SNAPSHOT, runner)

    report = world.run("groups", "apps")

    assert report.status == COMPLETED_WITH_FAILURES
    assert report.failures == [("admin.apps", "ValueError: boom")]
    assert report.counts[DONE] == 1


def test_ctrl_c_stops_the_run_and_keeps_what_is_done(world):
    seen = []

    def on_event(event):
        if event.kind == "unit":
            seen.append(event.unit.key)
            if len(seen) == 2:
                raise KeyboardInterrupt

    report = world.run(on_event=on_event)

    assert report.status == INTERRUPTED
    assert sum(report.counts.values()) == 2
    assert world.state()["runs"][-1]["status"] == "interrupted"
    assert world.stored("admin.groups") is not None


def test_a_stop_from_outside_ends_the_run_and_a_second_run_continues(world):
    stop = threading.Event()

    def on_event(event):
        if event.kind == "unit":
            stop.set()

    options = world.options()
    report = world.engine.run(options, on_event=on_event, stop=stop)

    assert report.status == INTERRUPTED
    assert report.cancelled >= 1
    assert sum(report.counts.values()) + report.cancelled == 7
    assert world.state()["runs"][-1]["status"] == "interrupted"
    done = report.counts[DONE]

    again = world.engine.run(options)

    assert again.status == COMPLETED and not again.cancelled
    assert again.counts[SKIPPED] == done  # what was done is not fetched again
    assert again.counts[DONE] == 7 - done


def test_a_stop_before_the_run_starts_makes_no_request(world):
    stop = threading.Event()
    stop.set()

    report = world.engine.run(world.options(), stop=stop)

    assert report.status == INTERRUPTED and report.cancelled == 7
    assert not report.counts and world.fake.calls == []


def test_a_stop_after_the_last_unit_does_not_make_a_finished_run_interrupted(world):
    stop = threading.Event()

    def on_event(event):
        if event.kind == "unit" and event.done == event.total:
            stop.set()

    report = world.engine.run(world.options("groups"), on_event=on_event, stop=stop)

    assert report.status == COMPLETED and report.cancelled == 0


def test_a_stop_cuts_a_wait_for_quota_short(world):
    # admin.groups allows 15 requests a minute: use them up, so that the next one waits
    for n in range(15):
        world.admin.request("admin.groups", {"$top": n + 1})
    # an engine that waits in real time (the World's waits on the fake clock instead)
    engine = SyncEngine(
        world.client_for,
        world.store,
        clock=world.clock.now,
        monotonic=world.clock.time,
    )
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    started = time.monotonic()

    report = engine.run(world.options("groups", max_wait=120.0), stop=stop)

    assert report.status == INTERRUPTED and report.cancelled == 1
    assert time.monotonic() - started < 10  # not the 60 seconds the quota asks for
    assert world.admin.limiter.interrupt is None  # the limiter is as it was


def test_tokens_of_different_tenants_are_refused(tmp_path):
    world = World(tmp_path)
    world.user = make_client(
        world.fake,
        clock=world.clock,
        store=world.store,
        token=make_token(tenant="tenant-2", expires_in=timedelta(days=1000)),
        group="user",
    )[0]

    with pytest.raises(PBIError, match="different tenants"):
        world.run("groups", "user-groups")


def test_no_account_at_all_is_reported_before_anything_is_fetched(tmp_path):
    world = World(tmp_path)

    def client_for(scope, profile=None):
        raise PBIError("No active profile set for group 'admin'.")

    world.engine._client_for = client_for

    with pytest.raises(AuthError, match="No account is stored.*-g admin.*-g user"):
        world.run()
    assert world.fake.calls == []


# ---------------------------------------------------------------------------
# bookkeeping
# ---------------------------------------------------------------------------


def test_the_run_is_recorded_in_the_state(world):
    report = world.run("groups", "apps")

    run = world.state()["runs"][-1]
    assert run["id"] == report.run_id and run["status"] == "completed"
    assert run["targets"] == ["groups", "apps"]
    assert run["counts"] == {"done": 2, "skipped": 0, "failed": 0, "deferred": 0}
    assert run["finished_at"] and run["started_at"]
    assert run["options"]["workers"] == 1


def test_progress_is_reported_stage_by_stage(world):
    events = []

    world.run("report-users", on_event=events.append)

    stages = [e for e in events if e.kind == "stage"]
    assert [(e.stage, e.units, e.targets) for e in stages] == [
        (1, 1, ("reports",)),
        (2, 6, ("report-users",)),
    ]
    units = [e for e in events if e.kind == "unit"]
    assert len(units) == 7 and all(e.outcome.status == DONE for e in units)
    last = [e for e in units if e.stage == 2][-1]
    assert (last.done, last.total) == (6, 6)


def test_workers_share_the_work_without_doing_anything_twice(tmp_path):
    world = World(tmp_path, reports=60)

    report = world.run("report-users", workers=8)

    assert report.status == COMPLETED and report.counts[DONE] == 61
    calls = world.fake.calls_to(USERS)
    assert len(calls) == 60 and len({c.path for c in calls}) == 60
    assert len(world.store.parameter_sets(TENANT, "admin.reports.users")) == 60


def test_failures_are_recorded_while_workers_run(tmp_path):
    world = World(tmp_path, reports=40)
    world.fake.fail("GET", r"/admin/reports/rep-00[1-2]\d/users", 404)  # 20 of them

    report = world.run("report-users", workers=8)

    assert report.counts[FAILED] == 20 and report.counts[DONE] == 1 + 20
    assert len(world.state()["units"]) == 20


# ---------------------------------------------------------------------------
# which accounts there are, and who a request is made as
# ---------------------------------------------------------------------------

SKELETON = [
    "user.groups",
    "user.apps",
    "user.group_reports",
    "user.group_datasets",
    "user.group_dashboards",
    "user.group_dataflows",
]


def test_the_kinds_of_account_that_are_stored_are_the_kinds_that_have_a_token(world):
    assert world.engine.available_scopes() == {Scope.ADMIN, Scope.USER}

    world.only_user()
    assert world.engine.available_scopes() == {Scope.USER}

    world.engine._client_for = lambda scope, profile=None: world.admin
    assert world.engine.available_scopes() == {Scope.ADMIN, Scope.USER}

    def nobody(scope, profile=None):
        raise PBIError("no profile")

    world.engine._client_for = nobody
    assert world.engine.available_scopes() == set()


def test_the_profile_that_the_sync_asks_for_decides_which_accounts_there_are(world):
    asked = []

    def client_for(scope, profile=None):
        asked.append((scope, profile))
        if profile == "missing":
            raise PBIError("no such profile")
        return world.client_for(scope)

    world.engine._client_for = client_for

    found = world.engine.available_scopes(world.options(user_profile="missing"))

    assert found == {Scope.ADMIN}
    assert (Scope.USER, "missing") in asked


def test_without_an_administrator_the_plain_sync_is_what_a_user_can_see(world):
    world.only_user()

    plan = world.plan()
    report = world.run()

    assert [t.target.name for t in plan.targets] == [
        "user-groups",
        "user-apps",
        "user-reports",
        "user-datasets",
        "user-dashboards",
        "user-dataflows",
    ]
    assert report.status == COMPLETED
    assert not world.fake.calls_to(r"^/admin")
    assert set(SKELETON) <= set(world.store.endpoints(TENANT))
    assert world.stored("user.groups") is not None


def test_a_target_of_the_missing_account_is_refused_before_anything_is_fetched(world):
    world.only_user()

    with pytest.raises(PBIError, match="'scan' needs an administrator account"):
        world.run("scan")
    with pytest.raises(PBIError, match="'groups' needs an administrator account"):
        world.plan("groups")

    assert world.fake.calls == []


def test_all_is_what_the_stored_accounts_can_run(world):
    world.only_user()

    plan = world.plan("all")

    assert all(t.target.scope is Scope.USER for t in plan.targets)


def test_a_client_that_takes_no_profile_still_works_when_none_is_asked_for(world):
    world.engine._client_for = lambda scope: world.client_for(scope)

    assert world.run("groups", "user-groups").status == COMPLETED


def test_the_profile_of_a_run_is_the_one_the_requests_are_made_as(world):
    asked = []
    real = world.client_for

    def client_for(scope, profile=None):
        asked.append((scope, profile))
        return real(scope)

    world.engine._client_for = client_for
    units = []

    world.run(
        "groups",
        "user-groups",
        admin_profile="adm",
        user_profile="svc",
        on_event=lambda e: units.append(e.unit) if e.unit else None,
    )

    assert {(s, p) for s, p in asked} >= {(Scope.ADMIN, "adm"), (Scope.USER, "svc")}
    assert {(u.scope, u.account) for u in units} == {
        (Scope.ADMIN, "adm"),
        (Scope.USER, "svc"),
    }


@pytest.mark.parametrize(
    "targets",
    [("groups",), ("activity",), ("scan",), ("user-groups", "user-reports")],
)
def test_every_kind_of_unit_is_made_as_the_profile_of_the_run(world, targets):
    units = []

    report = world.run(
        *targets,
        admin_profile="adm",
        user_profile="svc",
        days=1,
        on_event=lambda e: units.append(e.unit) if e.unit else None,
    )

    assert report.status == COMPLETED and units
    wanted = {Scope.ADMIN: "adm", Scope.USER: "svc"}
    assert [u.key for u in units if u.account != wanted[u.scope]] == []


def test_an_incremental_scan_is_made_as_the_profile_of_the_run(world):
    world.run("scan")  # the first one is complete: the next asks what changed
    world.clock.advance(hours=2)
    world.fake.modify_workspace("ws-0001", world.clock.now())
    units = []

    report = world.run(
        "scan",
        admin_profile="adm",
        on_event=lambda e: units.append(e.unit) if e.unit else None,
    )

    assert report.status == COMPLETED
    assert {u.kind for u in units} == {"modified", "scan"}
    assert {u.account for u in units} == {"adm"}


def test_the_record_of_a_run_names_the_profiles_it_used(world):
    world.run("groups", "user-groups", admin_profile="adm", user_profile="svc")

    options = world.state()["runs"][-1]["options"]

    assert (options["admin_profile"], options["user_profile"]) == ("adm", "svc")


def test_without_a_profile_the_units_use_the_active_one(world):
    units = []

    world.run("groups", on_event=lambda e: units.append(e.unit) if e.unit else None)

    assert [u.account for u in units] == [None]


def test_two_user_accounts_keep_their_workspaces_apart_and_each_is_fresh_for_its_owner(
    world,
):
    world.accounts(ana="oid-ana", bob="oid-bob")

    world.run("user-groups", user_profile="ana")
    world.run("user-groups", user_profile="bob")  # ana's answer is not bob's: it asks
    assert world.fake.count(r"^/groups$") == 2
    world.fake.reset_calls()
    world.run("user-groups", user_profile="ana")
    world.run("user-groups", user_profile="bob")

    assert world.fake.calls == []  # each is fresh for the account it was fetched as
    sets = world.store.parameter_sets(TENANT, "user.groups")
    assert sorted(s.params["_as"] for s in sets) == ["oid-ana", "oid-bob"]
    assert {s.latest.manifest["profile"] for s in sets} == {"ana", "bob"}


def test_the_plan_of_an_account_counts_what_is_fresh_for_that_account(world):
    world.accounts(ana="oid-ana", bob="oid-bob")
    world.run("user-groups", user_profile="ana")

    ana = world.plan("user-groups", user_profile="ana")
    bob = world.plan("user-groups", user_profile="bob")

    assert (ana.targets[0].fresh, ana.targets[0].todo) == (1, 0)
    assert (bob.targets[0].fresh, bob.targets[0].todo) == (0, 1)


def test_the_unit_of_an_account_says_whose_it_is(world):
    world.accounts(ana="oid-ana")
    keys = []

    world.run(
        "user-groups",
        user_profile="ana",
        on_event=lambda e: keys.append(e.unit.key) if e.unit else None,
    )

    assert keys == ["user.groups?_as=oid-ana"]


def test_what_does_not_depend_on_who_asks_is_shared_between_accounts(world):
    world.accounts(ana="oid-ana", bob="oid-bob")
    world.run("user-groups", "user-reports", user_profile="ana")
    world.fake.reset_calls()

    world.run("user-reports", user_profile="bob")  # the reports of a workspace: same

    assert world.fake.count(r"^/groups/[^/]+/reports$") == 0  # fresh, whoever made it


# ---------------------------------------------------------------------------
# a plan says which accounts it uses
# ---------------------------------------------------------------------------


def test_a_plan_names_the_accounts_it_uses(world):
    both = world.plan("groups", "user-groups")
    only_admin = world.plan("groups")
    only_user = world.plan("user-groups")

    assert both.accounts == ["admin-nlm (admin)", "user-nlm (user)"]
    assert only_admin.accounts == ["admin-nlm (admin)"]
    assert only_user.accounts == ["user-nlm (user)"]
    assert (
        world.engine.accounts(world.options("groups", "user-groups")) == both.accounts
    )


def test_the_accounts_follow_the_profiles_the_sync_is_asked_for(world):
    world.accounts(ana="oid-ana", bob="oid-bob")

    found = world.engine.accounts(world.options("user-groups", user_profile="bob"))

    assert found == ["bob (user)"]
    assert world.plan("user-groups", user_profile="bob").accounts == ["bob (user)"]


def test_without_an_administrator_the_accounts_are_the_users(world):
    world.only_user()

    assert world.engine.accounts(world.options()) == ["user-nlm (user)"]
    assert world.plan().accounts == ["user-nlm (user)"]


def test_with_no_account_there_are_no_accounts_to_name(world):
    def nobody(scope, profile=None):
        raise PBIError("no profile")

    world.engine._client_for = nobody

    assert world.engine.accounts(world.options()) == []
    assert (
        world.engine.accounts(world.options("groups")) == []
    )  # a plan says what to store


def test_asking_for_the_accounts_of_a_target_whose_account_is_missing_refuses(world):
    world.only_user()

    with pytest.raises(PBIError, match="'scan' needs an administrator account"):
        world.engine.accounts(world.options("scan"))
