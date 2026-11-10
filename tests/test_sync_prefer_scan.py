"""``prefer_scan``: the users and data sources of the items are taken from the scan, which
holds them for up to 100 workspaces per request, instead of one request per item.

It is a switch: without it nothing changes, and with it the plan and the run say which
targets were left to the scan.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from core_helpers import make_token
from fake_powerbi import FakePowerBI
from sync_helpers import TENANT, World
from typer.testing import CliRunner

from pbi_cli.cli import app, store_token
from pbi_cli.core.catalog import Catalog
from pbi_cli.core.planfile import Accounting, PlanFile, PlanFileError
from pbi_cli.core.registry import Scope
from pbi_cli.core.scan import ScanFlags
from pbi_cli.core.sync.plan import SyncOptions
from pbi_cli.core.sync.targets import ALL, SCAN_ANSWERS, select_targets
from pbi_cli.errors import PBIError

USERS = {
    "group-users",
    "report-users",
    "dataset-users",
    "dashboard-users",
    "dataflow-users",
}
SOURCES = {"datasources", "dataflow-datasources"}
BOTH = {"get_artifact_users", "datasource_details"}


# ---------------------------------------------------------------------------
# which targets
# ---------------------------------------------------------------------------


def test_without_the_switch_nothing_is_left_to_the_scan():
    chosen = select_targets([ALL])

    assert USERS | SOURCES <= set(chosen.names)
    assert chosen.left_to_scan == () and chosen.notes() == []


def test_a_scan_with_users_and_sources_answers_seven_fan_outs():
    chosen = select_targets([ALL], scan_options=BOTH)

    assert set(chosen.left_to_scan) == USERS | SOURCES == set(SCAN_ANSWERS)
    assert not (USERS | SOURCES) & set(chosen.names)
    assert "app-users" in chosen.names  # the people of an app are not in a scan
    assert "scan" in chosen.names


def test_only_what_the_scan_holds_is_left_to_it():
    chosen = select_targets([ALL], scan_options={"get_artifact_users"})

    assert set(chosen.left_to_scan) == USERS
    assert SOURCES <= set(chosen.names)
    assert set(chosen.not_in_scan) == {
        ("datasources", "datasource_details"),
        ("dataflow-datasources", "datasource_details"),
    }
    [left, *kept] = chosen.notes()
    assert left.startswith("Left to the scan (prefer_scan): report-users, group-users")
    assert kept[0] == (
        "datasources is still fetched one item at a time: prefer_scan leaves it to the "
        "scan only when the scan is made with datasource_details."
    )


def test_a_target_that_is_named_is_left_to_the_scan_too():
    chosen = select_targets(["report-users", "scan"], scan_options=BOTH)

    assert chosen.names == ["scan"]  # nor are the reports fetched for it
    assert chosen.left_to_scan == ("report-users",)


def test_preferring_a_scan_that_is_not_made_is_refused():
    with pytest.raises(PBIError, match="no scan is among the targets"):
        select_targets(["reports", "report-users"], scan_options=BOTH)


def test_preferring_the_scan_with_nothing_it_answers_is_fine():
    chosen = select_targets(["reports", "app-users"], scan_options=BOTH)

    assert chosen.names == ["apps", "reports", "app-users"]
    assert chosen.notes() == []


def test_the_options_of_a_sync_choose_through_the_switch():
    flags = ScanFlags(get_artifact_users=True)
    asked = ("reports", "report-users", "scan")

    assert SyncOptions(asked, scan_flags=flags).select().names == [
        "reports",
        "scan",
        "report-users",
    ]
    preferred = SyncOptions(asked, scan_flags=flags, prefer_scan=True)
    assert preferred.select().names == ["reports", "scan"]
    assert preferred.summary()["prefer_scan"] is True


# ---------------------------------------------------------------------------
# a sync
# ---------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


TARGETS = ("reports", "report-users", "datasets", "dataset-users", "scan")
FLAGS = ScanFlags(get_artifact_users=True)


def test_the_plan_does_not_count_requests_for_what_the_scan_answers(world):
    world.run("reports", "datasets")

    each = world.plan(*TARGETS, scan_flags=FLAGS)
    preferred = world.plan(*TARGETS, scan_flags=FLAGS, prefer_scan=True)

    operations = {line.endpoint for line in each.quota}
    assert {"admin.reports.users", "admin.datasets.users"} <= operations
    assert not {"admin.reports.users", "admin.datasets.users"} & {
        line.endpoint for line in preferred.quota
    }
    assert preferred.requests < each.requests
    assert preferred.notes[0].startswith(
        "Left to the scan (prefer_scan): report-users, dataset-users."
    )
    assert each.notes == []


def test_a_run_asks_for_no_users_and_finds_them_in_the_scan(world):
    report = world.run(*TARGETS, scan_flags=FLAGS, prefer_scan=True)

    assert world.fake.calls_to(r"/users$") == []
    assert report.notes[0].startswith("Left to the scan (prefer_scan):")
    catalog = Catalog(world.store, TENANT, clock=world.clock.now)
    found = catalog.users(catalog.item("report", "rep-0001"))
    assert found.source == "scan"
    assert [row.email for row in found.rows] == ["ann@example.com"]


# ---------------------------------------------------------------------------
# the plan file
# ---------------------------------------------------------------------------


def read(text: str) -> PlanFile:
    return PlanFile.parse(text, Path("/work/pbi-plan.yaml"))


EVERYTHING = """\
version: 1
tenant:
  targets: [all]
  scan: all
  prefer_scan: true
"""


def test_the_plan_file_switches_it_on_for_the_steps_of_the_administrator():
    plan = read(EVERYTHING)
    steps = plan.first_steps(Accounting(admin="adm", users=("svc",)))

    assert plan.tenant.prefer_scan is True
    by_title = {step.title: step.options for step in steps}
    assert by_title["the tenant (adm)"].prefer_scan is True
    assert by_title["the tenant (svc)"].prefer_scan is False
    assert set(by_title["the tenant (adm)"].select().left_to_scan) == set(SCAN_ANSWERS)


def test_it_is_off_unless_the_plan_file_says_so():
    plan = read(EVERYTHING.replace("  prefer_scan: true\n", ""))

    assert plan.tenant.prefer_scan is False


def test_the_plan_file_refuses_it_without_a_scan():
    with pytest.raises(PlanFileError) as error:
        read(
            "version: 1\ntenant:\n  targets: [reports, report-users]\n  prefer_scan: true\n"
        )

    assert "tenant.prefer_scan" in str(error.value)
    assert "has no effect: 'scan' is not among tenant.targets" in str(error.value)


def test_the_plan_file_wants_a_yes_or_no():
    with pytest.raises(PlanFileError, match="prefer_scan"):
        read(EVERYTHING.replace("prefer_scan: true", "prefer_scan: maybe"))


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------


@pytest.fixture
def admin(monkeypatch, cache_folder):
    service = FakePowerBI(workspaces=3, reports=4, datasets=2)
    monkeypatch.setattr("pbi_cli.core.client.make_session", service.session)
    token = make_token(tenant="tenant-1", expires_in=timedelta(days=36500))
    store_token(token, "adm", "admin")
    return service


def sync(*args: str):
    return CliRunner().invoke(app, ["sync", *args])


def test_pbi_sync_plan_says_what_is_left_to_the_scan(admin):
    asked = ["reports", "report-users", "scan", "--get-artifact-users"]

    plain = sync("plan", *asked)
    preferred = sync("plan", *asked, "--prefer-scan")

    assert plain.exit_code == 0, plain.output
    assert preferred.exit_code == 0, preferred.output
    assert "Targets: reports, scan, report-users" in plain.output
    assert "Left to the scan" not in plain.output
    assert "Targets: reports, scan\n" in preferred.output
    assert "Left to the scan (prefer_scan): report-users." in preferred.output


def test_pbi_sync_run_asks_for_no_users(admin):
    result = sync(
        "run",
        "reports",
        "report-users",
        "scan",
        "--get-artifact-users",
        "--prefer-scan",
    )

    assert result.exit_code == 0, result.output
    assert "Left to the scan (prefer_scan): report-users." in result.output
    assert admin.calls_to(r"/users$") == []


def test_pbi_sync_refuses_to_prefer_a_scan_it_does_not_make(admin):
    result = sync("plan", "reports", "report-users", "--prefer-scan")

    assert result.exit_code == 1
    assert "no scan is among the targets" in result.output


def test_a_plan_file_takes_the_switch_over_from_the_command_line(admin, tmp_path):
    path = tmp_path / "pbi-plan.yaml"
    path.write_text(EVERYTHING, encoding="utf-8")

    result = sync("plan", "--config", str(path), "--prefer-scan")

    assert result.exit_code != 0
    assert "tenant: {prefer_scan: true}" in result.output


# ---------------------------------------------------------------------------
# the terminal UI
# ---------------------------------------------------------------------------


def test_the_terminal_ui_shows_what_is_left_to_the_scan(world):
    from pbi_cli.tui.render import plan_notes

    plan = world.plan(*TARGETS, scan_flags=FLAGS, prefer_scan=True)

    assert any(
        n.startswith("Left to the scan (prefer_scan):") for n in plan_notes(plan)
    )
