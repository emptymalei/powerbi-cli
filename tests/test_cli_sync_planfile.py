"""``pbi sync plan | run --config``: a plan file instead of target names, from the command line."""

import re
from datetime import timedelta
from pathlib import Path
from typing import List

import pytest
from core_helpers import make_token
from fake_powerbi import FakePowerBI
from typer.testing import CliRunner

from pbi_cli.cli import app, store_token
from pbi_cli.core.store import LakeStore


@pytest.fixture
def fake(monkeypatch):
    service = FakePowerBI(workspaces=12, reports=6, datasets=4, event_days=3)
    monkeypatch.setattr("pbi_cli.core.client.make_session", service.session)
    return service


@pytest.fixture
def accounts(fake, cache_folder):
    """An administrator (adm) and two users (svc, bob), each with a token of its own, and
    the workspaces each user can see."""
    forever = timedelta(days=36500)
    for profile, group, oid in (
        ("adm", "admin", "o-adm"),
        ("svc", "user", "o-svc"),
        ("bob", "user", "o-bob"),
    ):
        store_token(
            make_token(tenant="tenant-1", expires_in=forever, oid=oid), profile, group
        )
    fake.visible_to = {"o-svc": ["ws-0001", "ws-0002"], "o-bob": ["ws-0003"]}
    return fake


def write(tmp_path: Path, text: str) -> str:
    path = tmp_path / "pbi-plan.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def sync(*args: str):
    return CliRunner().invoke(app, ["sync", *args])


def lake(cache_folder) -> LakeStore:
    return LakeStore(cache_folder / "lake")


def rows(output: str, header: str) -> List[List[str]]:
    """The rows of the first table that starts with ``header``."""
    lines = output.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(header))
    found = []
    for line in lines[start + 1 :]:
        if not line.strip():
            break
        found.append(re.split(r"\s{2,}", line.strip()))
    return found


TENANT_AND_USERS = """\
version: 1
accounts: {admin: adm}
tenant: {targets: [groups, reports, datasets, dashboards, dataflows]}
workspaces:
  - {name: "Workspace 2", details: [users], via: admin}
"""


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------


def test_a_plan_shows_each_step_what_it_costs_and_what_waits(
    accounts, tmp_path, cache_folder
):
    path = write(tmp_path, TENANT_AND_USERS)

    result = sync("plan", "--config", path)

    assert result.exit_code == 0, result.output
    assert f"Data lake: {cache_folder / 'lake'}" in result.output
    assert f"Plan file: {Path(path).resolve()}" in result.output
    assert "Accounts: adm (admin)" in result.output
    assert "Step 1: the tenant (adm)" in result.output
    assert "Step 2" not in result.output  # the workspaces wait for the list of them
    assert "workspaces[0] (Workspace 2): no workspace is called 'Workspace 2'" in (
        result.output
    )
    assert "the lake holds no list of workspaces yet" in result.output
    assert "Requests against the quota of each operation" in result.output
    assert "admin.groups" in result.output
    assert "Targets:" not in result.output  # not the plan of a sync of target names
    assert accounts.calls == []  # a plan calls nothing


def test_a_plan_after_a_run_has_every_step_and_nothing_left_to_fetch(
    accounts, tmp_path
):
    path = write(tmp_path, TENANT_AND_USERS)
    sync("run", "--config", path)

    result = sync("plan", "--config", path)

    assert result.exit_code == 0, result.output
    assert "Step 1: the tenant (adm)" in result.output
    assert (
        "Step 2: dashboard-users, dataflow-users, dataset-users, group-users, "
        "report-users for 1 workspace (adm)" in result.output
    )
    assert "dataset-users copies the people who can use each dataset" in result.output
    assert "Nothing to fetch: the lake holds everything fresh." in result.output
    assert "no workspace is called" not in result.output


def test_a_plan_without_steps_says_so(accounts, tmp_path):
    path = write(tmp_path, "version: 1\n")

    result = sync("plan", "--config", path)

    assert result.exit_code == 0 and "No step is planned yet." in result.output
    assert "Requests against the quota" not in result.output
    assert (
        "Nothing to fetch" not in result.output
    )  # nothing was planned: not "all fresh"


def test_steps_that_need_the_same_lists_say_that_they_share_them(accounts, tmp_path):
    path = write(
        tmp_path,
        "version: 1\ntenant: {targets: [groups, reports]}\nworkspaces:\n"
        "  - {id: ws-0001, details: [users], via: admin}\n",
    )

    result = sync("plan", "--config", path)

    assert result.exit_code == 0, result.output
    assert "Step 2: " in result.output
    assert "Steps share lists:" in result.output


def test_a_plan_names_every_account_its_steps_use(accounts, tmp_path):
    path = write(
        tmp_path,
        "version: 1\naccounts: {admin: adm, user: [svc, bob]}\n"
        "tenant: {targets: [groups, user-groups]}\n",
    )

    result = sync("plan", "--config", path)

    assert result.exit_code == 0, result.output
    assert "Accounts: adm (admin), svc (user), bob (user)" in result.output
    assert "Step 2: the tenant (svc)" in result.output
    assert "Step 3: the tenant (bob)" in result.output


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


def test_a_run_goes_step_after_step_and_keeps_what_it_fetched(
    accounts, tmp_path, cache_folder
):
    path = write(tmp_path, TENANT_AND_USERS)

    result = sync("run", "--config", path)

    assert result.exit_code == 0, result.output
    out = result.output
    assert f"Plan file: {Path(path).resolve()}" in out
    assert "Accounts: adm (admin)" in out
    assert out.index("Step 1: the tenant (adm)") < out.index("Step 2: ")
    assert "report-users for 1 workspace (adm)" in out
    assert "Finished in" in out and "0 failed" in out
    held = lake(cache_folder)
    ids = lambda endpoint, name: sorted(  # noqa: E731
        s.params[name] for s in held.parameter_sets("tenant-1", endpoint)
    )
    assert ids("admin.groups.users", "groupId") == ["ws-0002"]
    assert ids("admin.reports.users", "reportId") == ["rep-0002"]
    assert ids("admin.datasets.users", "datasetId") == ["ds-0002"]
    # only what the file says: not the plain sync of target names as well
    assert not accounts.calls_to(r"^/admin/apps$")
    assert not accounts.calls_to(r"^/admin/capacities$")


def test_a_second_run_fetches_nothing_that_is_fresh(accounts, tmp_path):
    path = write(tmp_path, TENANT_AND_USERS)
    sync("run", "--config", path)
    accounts.reset_calls()

    result = sync("run", "--config", path)

    assert result.exit_code == 0, result.output
    assert "0 fetched" in result.output
    assert accounts.calls == []


def test_users_read_what_only_a_user_can_through_the_account_that_lists_it(
    accounts, tmp_path, cache_folder
):
    path = write(
        tmp_path,
        "version: 1\naccounts: {user: [svc, bob]}\nworkspaces:\n"
        "  - {name: 'Workspace 2', details: [pages]}\n"
        "  - {name: 'Workspace 3', details: [pages]}\n",
    )

    result = sync("run", "--config", path)

    assert result.exit_code == 0, result.output
    assert "Accounts: adm (admin), svc (user), bob (user)" in result.output
    assert "Step 1: the workspaces of svc" in result.output
    assert "Step 2: the workspaces of bob" in result.output
    assert "Step 3: user-pages for 1 workspace (svc)" in result.output
    assert "Step 4: user-pages for 1 workspace (bob)" in result.output
    pages = lake(cache_folder).parameter_sets("tenant-1", "user.report_pages")
    assert sorted(p.params["reportId"] for p in pages) == ["rep-0002", "rep-0003"]


def test_a_name_that_matches_nothing_fails_the_run_but_the_rest_is_done(
    accounts, tmp_path
):
    path = write(
        tmp_path,
        "version: 1\ntenant: {targets: [groups]}\nworkspaces:\n"
        "  - {name: 'Nothing here', scan: true}\n"
        "  - {name: 'Workspace 2', details: [users], via: admin}\n",
    )

    result = sync("run", "--config", path)

    assert result.exit_code == 1
    assert (
        "failed: workspaces[0] (Nothing here): no workspace is called 'Nothing here'"
        in result.output
    )
    assert "Step 2: " in result.output  # the other entry was done


def test_an_expired_token_ends_the_run_and_says_how_to_continue(accounts, tmp_path):
    path = write(tmp_path, TENANT_AND_USERS)
    accounts.expire_token_after(2)

    result = sync("run", "--config", path)

    assert result.exit_code == 1
    assert "What is done is kept" in result.output
    assert f"run `pbi sync run --config {path}` again to continue" in result.output


def test_the_scan_of_chosen_workspaces_is_the_scan_of_those(accounts, tmp_path):
    path = write(
        tmp_path,
        "version: 1\ntenant: {targets: [groups]}\nworkspaces:\n"
        "  - {name: 'Workspace 1?', scan: {lineage: true}}\n",
    )

    result = sync("run", "--config", path)

    assert result.exit_code == 0, result.output
    assert "Step 2: scan of 3 workspaces (lineage)" in result.output
    posted = accounts.calls_to(r"getInfo", "POST")
    assert sorted(posted[0].body["workspaces"]) == ["ws-0010", "ws-0011", "ws-0012"]


# ---------------------------------------------------------------------------
# what goes with a plan file and what does not
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["plan", "run"])
def test_target_names_and_a_plan_file_are_not_given_together(
    accounts, tmp_path, command
):
    path = write(tmp_path, TENANT_AND_USERS)

    result = sync(command, "groups", "--config", path)

    assert result.exit_code == 1
    assert "Name targets or give a plan file, not both" in result.output
    assert accounts.calls == []


@pytest.mark.parametrize(
    "flag, where",
    [
        (["--lineage"], "scan: {lineage: true}"),
        (["--datasource-details"], "scan: {datasource_details: true}"),
        (["--dataset-schema"], "scan: {dataset_schema: true}"),
        (["--dataset-expressions"], "scan: {dataset_expressions: true}"),
        (["--get-artifact-users"], "scan: {get_artifact_users: true}"),
        (["--full-scan"], "tenant: {full_scan: true}"),
        (["--exclude-personal"], "tenant: {exclude_personal: true}"),
        (["--exclude-inactive"], "tenant: {exclude_inactive: true}"),
        (["--admin-profile", "adm"], "accounts: {admin: <profile>}"),
        (["--user-profile", "svc"], "accounts: {user: [<profile>]}"),
    ],
)
def test_what_the_file_says_cannot_be_said_again_on_the_command_line(
    accounts, tmp_path, flag, where
):
    path = write(tmp_path, TENANT_AND_USERS)

    result = sync("plan", "--config", path, *flag)

    assert result.exit_code == 1
    assert f"{flag[0]} cannot be used with --config" in result.output
    assert f"({where})" in result.output
    assert accounts.calls == []


EVENTS = """\
version: 1
tenant: {targets: [activity], activity_days: 7}
"""


def activity_units(output: str) -> str:
    return next(r[2] for r in rows(output, "TARGET") if r[0] == "activity")


def test_the_days_of_the_file_are_used_unless_the_command_line_says_other(
    accounts, tmp_path
):
    path = write(tmp_path, EVENTS)

    from_file = sync("plan", "--config", path)
    typed = sync("plan", "--config", path, "--days", "3")
    same = sync("plan", "--config", path, "--days", "28")

    assert activity_units(from_file.output) == "7"
    assert activity_units(typed.output) == "3"
    assert activity_units(same.output) == "28"  # typed, even if it is the default


def test_force_applies_to_every_step(accounts, tmp_path):
    path = write(tmp_path, TENANT_AND_USERS)
    sync("run", "--config", path)

    result = sync("plan", "--config", path, "--force")

    assert result.exit_code == 0, result.output
    assert "Nothing to fetch" not in result.output
    table = rows(result.output, "TARGET")
    assert all(row[3] == "0" for row in table)  # nothing is fresh when it is forced


def test_max_age_applies_to_every_step(accounts, tmp_path):
    path = write(tmp_path, TENANT_AND_USERS)
    sync("run", "--config", path)

    result = sync("plan", "--config", path, "--max-age", "0s")

    assert result.exit_code == 0, result.output
    assert "Nothing to fetch" not in result.output


def test_a_bad_max_age_is_a_usage_error(accounts, tmp_path):
    path = write(tmp_path, TENANT_AND_USERS)

    result = sync("plan", "--config", path, "--max-age", "soon")

    assert result.exit_code == 2 and "not a duration" in result.output


@pytest.fixture
def overrides(monkeypatch):
    """The flags that reached the plan run, whatever command it was."""
    from pbi_cli import cli_sync

    seen = {}
    original = cli_sync.PlanRun.__init__

    def spy(self, *args, **kwargs):
        seen["overrides"] = kwargs["overrides"]
        original(self, *args, **kwargs)

    monkeypatch.setattr(cli_sync.PlanRun, "__init__", spy)
    return seen


def test_the_flags_that_were_typed_reach_every_step_of_a_run(
    accounts, tmp_path, overrides
):
    from pbi_cli.core.planfile import Overrides

    path = write(tmp_path, "version: 1\n")

    sync(
        "run",
        "--config",
        path,
        "--force",
        "--max-age",
        "2h",
        "--days",
        "3",
        "--workers",
        "2",
        "--wait",
        "0",
        "--scan-interval",
        "1.5",
        "--scan-timeout",
        "30",
    )

    assert overrides["overrides"] == Overrides(
        force=True,
        max_age=timedelta(hours=2),
        days=3,
        workers=2,
        wait=0.0,
        scan_interval=1.5,
        scan_timeout=30.0,
    )


def test_the_flags_that_were_not_typed_change_nothing(accounts, tmp_path, overrides):
    from pbi_cli.core.planfile import Overrides

    path = write(tmp_path, "version: 1\n")

    sync("run", "--config", path)
    sync("plan", "--config", path)

    assert overrides["overrides"] == Overrides()


def test_the_flags_of_a_plan_are_those_a_plan_has(accounts, tmp_path, overrides):
    from pbi_cli.core.planfile import Overrides

    path = write(tmp_path, "version: 1\n")

    sync("plan", "--config", path, "--force", "--days", "5", "--scan-timeout", "9")

    assert overrides["overrides"] == Overrides(force=True, days=5, scan_timeout=9.0)


def test_workers_and_wait_apply_to_a_run(accounts, tmp_path):
    path = write(tmp_path, TENANT_AND_USERS)

    result = sync("run", "--config", path, "--workers", "2", "--wait", "0")

    assert result.exit_code == 0, result.output
    assert "Finished in" in result.output


# ---------------------------------------------------------------------------
# what is wrong with the file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["plan", "run"])
def test_a_file_that_does_not_exist_is_a_usage_error(accounts, tmp_path, command):
    result = sync(command, "--config", str(tmp_path / "nope.yaml"))

    assert result.exit_code == 2 and "does not exist" in result.output


@pytest.mark.parametrize("command", ["plan", "run"])
def test_a_mistake_in_the_file_is_said_with_its_line_and_nothing_is_fetched(
    accounts, tmp_path, command
):
    path = write(tmp_path, "version: 1\nworkspace: []\n")

    result = sync(command, "--config", path)

    assert result.exit_code == 1
    assert "pbi-plan.yaml:2: workspace: unknown key. Did you mean 'workspaces'?" in (
        result.output
    )
    assert accounts.calls == []


@pytest.mark.parametrize("command", ["plan", "run"])
def test_a_profile_without_a_token_is_said_with_the_command_that_stores_one(
    accounts, tmp_path, command
):
    path = write(tmp_path, "version: 1\naccounts: {admin: nope}\ntenant:\n")

    result = sync(command, "--config", path)

    assert result.exit_code == 1
    assert "accounts.admin: no token is stored under the profile 'nope'" in (
        result.output
    )
    assert "pbi auth -t <token> -p nope -g admin" in result.output
    assert accounts.calls == []


@pytest.mark.parametrize("command", ["plan", "run"])
def test_a_plan_file_needs_a_lake(fake, signed_in, tmp_path, command):
    path = write(tmp_path, TENANT_AND_USERS)

    result = sync(command, "--config", path)

    assert result.exit_code == 1
    assert "pbi sync keeps what it fetches in the data lake" in result.output
    assert fake.calls == []


def test_the_session_section_does_not_matter_to_a_sync(accounts, tmp_path):
    path = write(
        tmp_path,
        "version: 1\ntenant: {targets: [groups]}\n"
        "session: {lake: /somewhere/else, open: Finance, lazy: auto}\n",
    )

    result = sync("run", "--config", path)

    assert result.exit_code == 0, result.output
    assert "/somewhere/else" not in result.output


def test_the_help_tells_about_the_plan_file():
    for command in ("plan", "run"):
        result = sync(command, "--help")
        assert "--config" in result.output and "-c" in result.output
        assert "plan file" in result.output.lower()
