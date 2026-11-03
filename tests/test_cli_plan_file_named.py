"""A plan file that is named once (``pbi config set-plan-file``, ``PBI_PLAN``) instead of on
every command, and never found by where it lies."""

from datetime import timedelta
from pathlib import Path

import pytest
from core_helpers import make_token
from fake_powerbi import FakePowerBI
from typer.testing import CliRunner

from pbi_cli import cli_tui
from pbi_cli.cli import app, store_token
from pbi_cli.config import PBIConfig

PLAN = "version: 1\naccounts: {admin: adm}\ntenant: {targets: [groups, apps]}\n"
OTHER = "version: 1\naccounts: {admin: adm}\ntenant: {targets: [capacities]}\n"


@pytest.fixture
def fake(monkeypatch):
    service = FakePowerBI(workspaces=12, reports=3, event_days=2)
    monkeypatch.setattr("pbi_cli.core.client.make_session", service.session)
    return service


@pytest.fixture
def ready(fake, cache_folder, monkeypatch):
    monkeypatch.delenv("PBI_PLAN", raising=False)
    token = make_token(tenant="tenant-1", expires_in=timedelta(days=36500), oid="o")
    store_token(token, "adm", "admin")
    return fake


def pbi(*args):
    return CliRunner().invoke(app, list(args))


def plan_at(folder: Path, text: str = PLAN, name: str = "pbi-plan.yaml") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def endpoints(cache_folder):
    return {p.name for p in (cache_folder / "lake").glob("tenant=*/endpoint=*")}


# ---------------------------------------------------------------------------
# naming it
# ---------------------------------------------------------------------------


def test_the_plan_file_is_kept_as_an_absolute_path_and_shown(ready, tmp_path):
    path = plan_at(tmp_path / "plans")

    result = pbi("config", "set-plan-file", str(path))

    assert result.exit_code == 0, result.output
    assert PBIConfig().plan_file == str(path.resolve())
    shown = pbi("config", "get-plan-file")
    assert f"Plan file: {path.resolve()} (from `pbi config set-plan-file`)" in (
        shown.output
    )


def test_a_file_that_cannot_be_used_is_refused_and_not_kept(ready, tmp_path):
    bad = plan_at(tmp_path, "version: 1\ntenant: {targets: [nonsense]}\n")

    result = pbi("config", "set-plan-file", str(bad))

    assert result.exit_code == 1 and "nonsense" in result.output
    assert PBIConfig().plan_file is None
    assert pbi("config", "set-plan-file", str(tmp_path / "missing.yaml")).exit_code == 2


def test_it_can_be_forgotten(ready, tmp_path):
    pbi("config", "set-plan-file", str(plan_at(tmp_path)))

    assert pbi("config", "clear-plan-file").exit_code == 0

    assert PBIConfig().plan_file is None
    assert "No plan file is named" in pbi("config", "get-plan-file").output


def test_the_environment_overrules_the_setting_and_get_says_so(
    ready, tmp_path, monkeypatch
):
    pbi("config", "set-plan-file", str(plan_at(tmp_path / "a")))
    other = plan_at(tmp_path / "b", OTHER)
    monkeypatch.setenv("PBI_PLAN", str(other))

    shown = pbi("config", "get-plan-file").output

    assert f"Plan file: {other} (from the environment variable PBI_PLAN)" in shown
    assert "is overruled by it" in shown


# ---------------------------------------------------------------------------
# using it
# ---------------------------------------------------------------------------


def test_a_plain_sync_runs_the_named_plan(ready, cache_folder, tmp_path):
    path = plan_at(tmp_path / "plans")
    pbi("config", "set-plan-file", str(path))

    result = pbi("sync", "run")

    assert result.exit_code == 0, result.output
    assert (
        f"Plan file: {path.resolve()} (from pbi config set-plan-file" in result.output
    )
    assert "Step 1: the tenant (adm)" in result.output
    assert endpoints(cache_folder) == {"endpoint=admin_groups", "endpoint=admin_apps"}


def test_the_plan_of_a_named_file_is_shown_too(ready, tmp_path):
    pbi("config", "set-plan-file", str(plan_at(tmp_path / "plans")))

    result = pbi("sync", "plan")

    assert result.exit_code == 0, result.output
    assert "Step 1: the tenant (adm)" in result.output


def test_the_environment_variable_names_a_plan_for_one_shell(
    ready, cache_folder, tmp_path, monkeypatch
):
    monkeypatch.setenv("PBI_PLAN", str(plan_at(tmp_path / "x", OTHER)))

    result = pbi("sync", "run")

    assert result.exit_code == 0, result.output
    assert "(from PBI_PLAN;" in result.output
    assert endpoints(cache_folder) == {"endpoint=admin_capacities"}


def test_config_on_the_command_line_wins(ready, cache_folder, tmp_path):
    pbi("config", "set-plan-file", str(plan_at(tmp_path / "a")))
    other = plan_at(tmp_path / "b", OTHER)

    result = pbi("sync", "run", "--config", str(other))

    assert result.exit_code == 0, result.output
    assert "from pbi config" not in result.output  # not the stored one
    assert endpoints(cache_folder) == {"endpoint=admin_capacities"}


def test_naming_targets_means_those_targets(ready, cache_folder, tmp_path):
    pbi("config", "set-plan-file", str(plan_at(tmp_path / "a")))

    result = pbi("sync", "run", "capacities")

    assert result.exit_code == 0, result.output
    assert "Plan file:" not in result.output
    assert endpoints(cache_folder) == {"endpoint=admin_capacities"}


def test_an_option_that_a_plan_takes_over_means_a_run_of_targets(
    ready, cache_folder, tmp_path
):
    pbi("config", "set-plan-file", str(plan_at(tmp_path / "a")))

    result = pbi(
        "sync", "plan", "--lineage"
    )  # no target named: only the option says it

    assert result.exit_code == 0, result.output
    assert "Plan file:" not in result.output
    assert "Targets:" in result.output  # the plan of a sync of target names


def test_a_plan_file_is_never_found_by_where_it_lies(
    ready, cache_folder, tmp_path, monkeypatch
):
    plan_at(tmp_path / "here")  # a pbi-plan.yaml in the current folder
    monkeypatch.chdir(tmp_path / "here")

    result = pbi("sync", "run")

    assert result.exit_code == 0, result.output
    assert "Plan file:" not in result.output
    assert "endpoint=admin_apps" in endpoints(cache_folder)  # the plain targets ran


def test_a_named_file_that_is_gone_is_said_so_with_the_way_out(ready, tmp_path):
    path = plan_at(tmp_path / "a")
    pbi("config", "set-plan-file", str(path))
    path.unlink()

    result = pbi("sync", "run")

    assert result.exit_code == 1
    assert "pbi config set-plan-file names the plan file" in result.output
    assert "pbi config clear-plan-file" in result.output
    gone = pbi("config", "get-plan-file")
    assert "is not a file" in gone.output


def test_the_command_that_continues_a_run_of_a_named_plan_is_the_same_command(
    ready, tmp_path
):
    ready.expire_token_after(1)
    pbi("config", "set-plan-file", str(plan_at(tmp_path / "a")))

    result = pbi("sync", "run")

    assert result.exit_code == 1
    assert "run `pbi sync run` again" in result.output


# ---------------------------------------------------------------------------
# the terminal UI
# ---------------------------------------------------------------------------


@pytest.fixture
def opened(monkeypatch):
    seen = {}

    def fake_launch(tenant, lake, config):
        seen["config"] = config

    monkeypatch.setattr(cli_tui, "launch", fake_launch)
    return seen


def test_the_tui_opens_with_the_named_plan(ready, opened, tmp_path, monkeypatch):
    path = plan_at(tmp_path / "a")
    pbi("config", "set-plan-file", str(path))

    result = pbi("tui")

    assert result.exit_code == 0, result.output
    assert opened["config"] == path.resolve()
    assert "(from pbi config set-plan-file)" in result.output


def test_the_tui_takes_config_over_the_named_plan(ready, opened, tmp_path):
    pbi("config", "set-plan-file", str(plan_at(tmp_path / "a")))
    other = plan_at(tmp_path / "b", OTHER)

    assert pbi("tui", "--config", str(other)).exit_code == 0

    assert opened["config"] == other


def test_the_tui_without_any_plan_opens_without_one(ready, opened):
    assert pbi("tui").exit_code == 0

    assert opened["config"] is None
