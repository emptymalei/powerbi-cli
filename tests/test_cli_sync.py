"""``pbi sync plan | run | status`` against the fake service, in an empty home."""

import re
from datetime import timedelta
from typing import List

import pytest
from core_helpers import make_token
from fake_powerbi import FakePowerBI
from typer.testing import CliRunner

from pbi_cli.cli import app, store_token
from pbi_cli.config import PBIConfig
from pbi_cli.core.store import LakeStore
from pbi_cli.core.sync.state import STATE_NAME
from pbi_cli.errors import AuthError

PLAIN = "groups, apps, capacities, reports, datasets, dashboards, dataflows"


@pytest.fixture
def fake(monkeypatch):
    service = FakePowerBI(
        workspaces=250, reports=6, datasets=4, event_days=3, events_per_day=5
    )
    monkeypatch.setattr("pbi_cli.core.client.make_session", service.session)
    return service


@pytest.fixture
def ready(fake, cache_folder, signed_in):
    """A service to talk to, a lake to keep it in, and a token."""
    return fake


def sync(*args: str):
    return CliRunner().invoke(app, ["sync", *args])


def rows(output: str, header: str) -> List[List[str]]:
    """The rows of the table that starts with ``header``, split at runs of spaces."""
    lines = output.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(header))
    found = []
    for line in lines[start + 1 :]:
        if not line.strip():
            break
        found.append(re.split(r"\s{2,}", line.strip()))
    return found


def lake(cache_folder) -> LakeStore:
    return LakeStore(cache_folder / "lake")


# ---------------------------------------------------------------------------
# the group
# ---------------------------------------------------------------------------


def test_the_group_shows_a_hint_without_a_command():
    result = sync()

    assert result.exit_code == 0 and "Use pbi sync --help" in result.output


def test_the_help_lists_the_targets():
    result = sync("run", "--help")

    assert result.exit_code == 0
    for name in ("groups", "report-users", "activity", "scan", "user-pages"):
        assert name in result.output


@pytest.mark.parametrize("command", ["plan", "run"])
def test_a_sync_needs_a_lake(fake, signed_in, command):
    result = sync(command)

    assert result.exit_code == 1
    assert "pbi sync keeps what it fetches in the data lake" in result.output
    assert "pbi config set-cache-folder" in result.output
    assert fake.calls == []


def test_a_sync_needs_the_lake_to_be_switched_on(ready):
    from pbi_cli.config import PBIConfig

    PBIConfig().cache_enabled = False

    result = sync("run")

    assert result.exit_code == 1 and "pbi config enable-cache" in result.output


def test_an_unknown_target_is_refused_with_the_names(ready):
    result = sync("run", "groups", "nope")

    assert result.exit_code == 1
    assert (
        "Unknown sync target 'nope'" in result.output
        and "report-users" in result.output
    )
    assert ready.calls == []


@pytest.mark.parametrize(
    "option",
    [
        ["--workers", "0"],
        ["--workers", "17"],
        ["--days", "29"],
        ["--days", "0"],
        ["--wait", "-1"],
    ],
)
def test_options_out_of_range_are_usage_errors(ready, option):
    result = sync("run", *option)

    assert result.exit_code == 2 and ready.calls == []


def test_a_duration_needs_a_unit(ready):
    result = sync("plan", "--max-age", "10x")

    assert result.exit_code == 2
    assert "--max-age" in result.output and "is not a duration" in result.output


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def test_plan_shows_what_an_empty_lake_needs_without_calling_the_api(
    ready, cache_folder
):
    result = sync("plan")

    assert result.exit_code == 0
    assert f"Data lake: {cache_folder / 'lake'}" in result.output
    assert f"Targets: {PLAIN}" in result.output
    assert "Tenant: tenant-1" in result.output
    table = rows(result.output, "TARGET")
    assert table[0] == ["groups", "admin.groups", "1", "0", "1", "1"]
    assert len(table) == 7
    assert "Everything fits the quota now." in result.output
    assert ready.calls == []


def test_plan_shows_the_quota_against_what_is_left(ready):
    result = sync("plan", "groups")

    quota = rows(result.output, "OPERATION")
    assert quota == [["admin.groups", "1", "50/h, 15/min", "15"]]


def test_plan_after_a_sync_says_there_is_nothing_to_do(ready):
    sync("run")

    result = sync("plan")

    assert "Nothing to fetch: the lake holds everything fresh." in result.output
    assert rows(result.output, "TARGET")[0] == [
        "groups",
        "admin.groups",
        "1",
        "1",
        "0",
        "0",
    ]


def test_plan_says_what_a_fan_out_depends_on(ready):
    result = sync("plan", "report-users")

    table = {row[0]: row for row in rows(result.output, "TARGET")}
    assert table["reports *"][2] == "1"
    assert table["report-users"][2:] == ["?", "?", "?", "?"]
    assert "* only here because another target needs its rows" in result.output
    assert (
        "report-users: depends on reports, which has nothing in the lake yet"
        in result.output
    )


def test_plan_warns_about_the_targets_that_hold_personal_data(ready):
    result = sync("plan", "activity", "groups")

    assert "activity copies what each person did and when" in result.output
    assert "groups copies" not in result.output


def test_plan_takes_the_options_into_account(ready):
    sync("run", "groups")

    young = sync("plan", "groups", "--max-age", "1d")
    forced = sync("plan", "groups", "--force")

    assert rows(young.output, "TARGET")[0][3:5] == ["1", "0"]
    assert rows(forced.output, "TARGET")[0][3:5] == ["0", "1"]


def test_plan_of_events_counts_the_days(ready):
    result = sync("plan", "activity", "--days", "5")

    assert rows(result.output, "TARGET")[0] == [
        "activity",
        "admin.activityevents",
        "5",
        "0",
        "5",
        "5",
    ]


def test_plan_of_a_bigger_fan_out_than_the_quota_says_so(
    tmp_path, monkeypatch, cache_folder, signed_in
):
    service = FakePowerBI(reports=450, datasets=1)
    monkeypatch.setattr("pbi_cli.core.client.make_session", service.session)
    sync("run", "reports")

    result = sync("plan", "report-users")

    assert (
        "admin.reports.users needs 450 requests and 200 fit now (200/h)"
        in result.output
    )
    assert "about 2 more hour(s)" in result.output


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_a_plain_run_fetches_the_plain_targets(ready, cache_folder):
    result = sync("run")

    assert result.exit_code == 0
    assert f"Targets: {PLAIN}" in result.output
    assert "Stage 1: 7 unit(s) of groups, apps" in result.output
    assert "✓ admin.groups  250 rows" in result.output
    assert "Finished in" in result.output and "7 fetched, 0 fresh" in result.output
    assert lake(cache_folder).endpoints("tenant-1") == sorted(
        [
            "admin.groups",
            "admin.apps",
            "admin.capacities",
            "admin.reports",
            "admin.datasets",
            "admin.dashboards",
            "admin.dataflows",
        ]
    )


def test_a_second_run_fetches_nothing(ready):
    sync("run")
    ready.reset_calls()

    result = sync("run")

    assert result.exit_code == 0
    assert "0 fetched, 7 fresh (not fetched again)" in result.output
    assert "· admin.groups  fresh" in result.output
    assert ready.calls == []


def test_force_fetches_again(ready):
    sync("run")
    ready.reset_calls()

    result = sync("run", "--force")

    assert "7 fetched" in result.output and ready.count(r"^/admin/") == 7


def test_a_fan_out_is_fetched_with_its_parent(ready):
    result = sync("run", "report-users")

    assert result.exit_code == 0
    assert "Stage 1: 1 unit(s) of reports" in result.output
    assert "Stage 2: 6 unit(s) of report-users" in result.output
    assert "✓ admin.reports.users?reportId=rep-0003" in result.output


def test_a_big_stage_shows_milestones_not_every_unit(
    tmp_path, monkeypatch, cache_folder, signed_in
):
    service = FakePowerBI(reports=60, datasets=1)
    monkeypatch.setattr("pbi_cli.core.client.make_session", service.session)

    result = sync("run", "report-users")

    assert result.exit_code == 0
    assert "Stage 2: 60 unit(s)" in result.output
    assert result.output.count("✓ admin.reports.users") == 0
    assert (
        "[6/60] report-users" in result.output
        and "[60/60] report-users" in result.output
    )


def test_a_unit_that_fails_is_reported_and_the_exit_status_says_so(ready):
    ready.fail("GET", r"^/admin/apps$", 500)

    result = sync("run")

    assert result.exit_code == 1
    assert "✗ admin.apps: admin.apps: Power BI answered HTTP 500" in result.output
    assert "6 fetched" in result.output and "1 failed" in result.output
    assert "failed: admin.apps:" in result.output


def test_an_expired_token_stops_the_run_with_a_way_to_continue(ready):
    ready.expire_token_after(3)

    result = sync("run", "groups", "apps", "reports", "datasets")

    assert result.exit_code == 1
    assert "Error:" in result.output and "pbi auth" in result.output
    assert (
        "What is done is kept: after signing in, run `pbi sync run groups apps reports datasets` again"
        in result.output
    )
    assert "3 fetched" in result.output


def test_a_quota_that_holds_a_unit_back_is_not_a_failure(ready):
    ready.fail("GET", r"^/admin/groups$", 429, times=1, headers={"Retry-After": "4000"})

    result = sync("run", "groups", "apps")

    assert result.exit_code == 0
    assert "… admin.groups:" in result.output
    assert (
        "1 unit(s) were held back by a quota: run the command again in about 67 minutes"
        in result.output
    )
    assert "1 fetched" in result.output and "1 deferred" in result.output


def test_events_are_fetched_per_day(ready, cache_folder):
    result = sync("run", "activity", "--days", "2")

    assert result.exit_code == 0
    assert "Stage 1: 2 unit(s) of activity" in result.output
    assert "new events" in result.output
    assert len(lake(cache_folder).event_days("tenant-1", "admin.activityevents")) == 2


def test_a_scan_is_fetched_in_batches_with_the_options(ready, cache_folder):
    result = sync(
        "run", "scan", "--lineage", "--workers", "2", "--scan-interval", "0.1"
    )

    assert result.exit_code == 0
    assert "scan copies the contents of every workspace" in result.output
    assert "Stage 2: 3 unit(s) of scan" in result.output
    queries = [c.query for c in ready.calls_to(r"getInfo", "POST")]
    assert len(queries) == 3 and all(q["lineage"] == "true" for q in queries)
    assert len(lake(cache_folder).parameter_sets("tenant-1", "admin.scan.result")) == 3


def test_both_kinds_of_token_can_be_used_in_one_run(ready):
    result = sync("run", "groups", "user-groups")

    assert result.exit_code == 0 and "2 fetched" in result.output


@pytest.fixture
def user_only(fake, cache_folder, monkeypatch):
    """Only a user is signed in: the administrators' group has no profile."""
    token = make_token(
        tenant="tenant-1",
        expires_in=timedelta(days=36500),
        oid="oid-svc",
        upn="svc@x.com",
    )

    def load_auth(profile=None, group="user"):
        if group == "admin":
            raise AuthError("No active profile set for group 'admin'.", group="admin")
        return {"Authorization": f"Bearer {token}"}

    monkeypatch.setattr("pbi_cli.cli.load_auth", load_auth)
    return fake


def test_a_user_without_an_administrator_plans_what_a_user_can_see(user_only):
    result = sync("plan")

    assert result.exit_code == 0, result.output
    assert (
        "Targets: user-groups, user-apps, user-reports, user-datasets, "
        "user-dashboards, user-dataflows" in result.output
    )
    assert "user-group-users" in result.output  # not plain: named to be included
    advertised = next(
        line for line in result.output.splitlines() if line.startswith("Not included")
    )
    assert "user-group-users" in advertised and "user-pages" in advertised
    assert "scan" not in advertised and "activity" not in advertised  # no admin account
    assert "Tenant: tenant-1" in result.output
    assert user_only.calls == []


def test_a_user_without_an_administrator_syncs_what_a_user_can_see(
    user_only, cache_folder
):
    result = sync("run")

    assert result.exit_code == 0, result.output
    header = result.output.split("Stage 1")[0]
    assert (
        "Targets: user-groups, user-apps, user-reports, user-datasets, "
        "user-dashboards, user-dataflows" in header
    )
    assert "capacities" not in header  # none of the administrator's targets
    assert not user_only.calls_to(r"^/admin")
    assert user_only.calls_to(r"^/groups$") and user_only.calls_to(r"^/apps$")
    held = lake(cache_folder).parameter_sets("tenant-1", "user.groups")
    assert [s.params["_as"] for s in held] == ["oid-svc"]
    assert held[0].latest.manifest["tenant"] == "tenant-1"


def test_a_user_who_names_an_admin_target_is_told_what_to_do(user_only):
    result = sync("run", "scan")

    assert result.exit_code == 1
    assert "'scan' needs an administrator account" in result.output
    assert "pbi auth -t <token> -g admin" in result.output
    assert "The accounts you have are: user." in result.output
    assert user_only.calls == []


def test_a_plan_and_a_run_name_the_profiles_they_use(fake, cache_folder):
    forever = timedelta(days=36500)
    for profile, group, oid in (
        ("adm", "admin", "o-adm"),
        ("svc", "user", "o-svc"),
        ("bob", "user", "o-bob"),
    ):
        token = make_token(tenant="tenant-1", expires_in=forever, oid=oid)
        store_token(token, profile, group)

    both = sync("plan", "groups", "user-groups")
    other = sync("plan", "user-groups", "--user-profile", "bob")
    run = sync("run", "groups")

    assert "Accounts: adm (admin), svc (user)" in both.output
    assert "Accounts: bob (user)" in other.output and "adm" not in other.output
    assert "Accounts: adm (admin)" in run.output and "svc" not in run.output


def test_a_plan_without_an_account_names_none(fake, cache_folder):
    result = sync("plan")

    assert result.exit_code == 1 and "Accounts:" not in result.output


def test_no_account_at_all_is_reported_before_anything_is_fetched(fake, cache_folder):
    result = sync("run")  # nobody is signed in

    assert result.exit_code == 1
    assert "No account is stored" in result.output
    assert "pbi auth -t <token> -g admin" in result.output
    assert "-g user" in result.output
    assert fake.calls == []


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_of_a_lake_without_a_sync(cache_folder):
    result = sync("status")

    assert result.exit_code == 0 and "Nothing has been synced yet" in result.output


def test_status_looks_at_another_lake_with_no_cache_folder_and_no_token(
    ready, cache_folder, monkeypatch
):
    sync("run", "groups")
    root = cache_folder / "lake"
    PBIConfig().set("cache_folder", None)  # no work lake at all now

    def refuse(profile=None, group="user"):
        raise AssertionError("status asked for a token")

    monkeypatch.setattr("pbi_cli.cli.load_auth", refuse)

    result = sync("status", "--lake", str(root))

    assert result.exit_code == 0, result.output
    assert f"Data lake: {root}" in result.output and "Tenant: tenant-1" in result.output


def test_status_needs_a_lake(fake):
    result = sync("status")

    assert result.exit_code == 1 and "There is no data lake yet" in result.output


def test_status_shows_the_last_run_and_what_the_lake_holds_without_a_token(
    ready, monkeypatch
):
    sync("run", "groups", "apps")

    def refuse(profile=None, group="user"):  # the status must not need a token
        raise AssertionError("status asked for a token")

    monkeypatch.setattr("pbi_cli.cli.load_auth", refuse)
    ready.reset_calls()

    result = sync("status")

    assert result.exit_code == 0 and ready.calls == []
    assert "Tenant: tenant-1" in result.output
    assert re.search(
        r"Last run: \d{4}-\d\d-\d\d \d\d:\d\d UTC \(\d+ s ago\): completed",
        result.output,
    )
    assert "targets: groups, apps" in result.output
    assert "2 fetched, 0 fresh, 0 failed, 0 deferred" in result.output
    holds = {row[0]: row for row in rows(result.output, "TARGET")}
    assert holds["groups"][1:3] == ["admin.groups", "1 request(s)"]
    assert holds["groups"][3].endswith("ago")
    quota = {row[0]: row for row in rows(result.output, "OPERATION")}
    assert quota["admin.groups"][1].startswith("49/50 h")  # one request of 50 was made


def test_status_lists_the_units_that_failed_and_those_held_back(ready):
    ready.fail("GET", r"/admin/reports/rep-0003/users", 404)
    sync("run", "report-users")

    result = sync("status")

    assert "completed, with failures" in result.output
    assert "Failed units (1; they are tried again by the next run):" in result.output
    assert (
        "admin.reports.users?reportId=rep-0003:" in result.output
        and "(attempts: 1)" in result.output
    )


def test_status_says_when_held_back_units_can_be_tried_again(ready):
    ready.fail("GET", r"^/admin/groups$", 429, times=1, headers={"Retry-After": "4000"})
    sync("run", "groups")

    result = sync("status")

    assert (
        "Held back by a quota: 1 unit(s), the first can be tried again in 1 h"
        in result.output
    )


def test_status_tells_about_scans_that_were_started_and_not_collected(ready):
    ready.scan_polls = 10**6
    sync("run", "scan", "--scan-timeout", "1", "--scan-interval", "0.2")

    result = sync("status")

    assert "3 scan(s) were started and not collected" in result.output
    ready.scan_polls = 0
    sync("run", "scan")
    done = sync("status")
    assert "were started and not collected" not in done.output
    assert (
        "Last complete scan: started" in done.output and "options: none" in done.output
    )


def test_status_shows_the_token_expiry_of_the_last_run(ready):
    ready.expire_token_after(2)
    sync("run", "groups", "apps", "reports")

    result = sync("status")

    assert "stopped: the token expired" in result.output


def test_status_of_one_tenant(ready):
    sync("run", "groups")

    assert "Tenant: tenant-1" in sync("status", "--tenant", "tenant-1").output
    assert "Nothing has been synced yet" in sync("status", "--tenant", "other").output


def test_the_state_is_a_document_in_the_lake(ready, cache_folder):
    sync("run", "groups")

    state = lake(cache_folder).read_state("tenant-1", STATE_NAME)

    assert state["runs"][-1]["targets"] == ["groups"]


def test_a_plain_sync_says_which_targets_it_leaves_out(ready):
    plain = sync("plan")
    named = sync("plan", "groups")

    assert (
        "Not included (name them to include them, see --help): scan, report-users, "
        "datasources, group-users, dataset-users, dashboard-users, dataflow-users, "
        "dataflow-datasources, refreshables, activity, user-groups, user-apps, "
        "user-reports, user-datasets, user-dashboards, user-dataflows, "
        "user-group-users, user-pages, user-dataset-users, user-dataset-datasources, "
        "user-dataflow-datasources, user-dataset-refreshes, user-dataset-parameters, "
        "user-dashboard-tiles"
    ) in plain.output
    assert "Not included" not in named.output
