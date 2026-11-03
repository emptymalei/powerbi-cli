"""``pbi lake users --fetch`` and ``pbi lake access --fetch``: fetch the users that the lake
lacks, after saying what it costs."""

import pytest
from fake_powerbi import FakePowerBI
from typer.testing import CliRunner

from pbi_cli.cli import app

REPORT_USERS = r"^/admin/reports/[^/]+/users$"


def pbi(*args, input=None):
    return CliRunner().invoke(app, list(args), input=input)


@pytest.fixture
def fake(monkeypatch):
    service = FakePowerBI(workspaces=12, reports=6, datasets=4, event_days=1)
    monkeypatch.setattr("pbi_cli.core.client.make_session", service.session)
    return service


@pytest.fixture
def listed(fake, cache_folder, signed_in):
    """The lists are in the lake; nobody who can open a report is."""
    assert pbi("sync", "run", "groups", "reports", "datasets").exit_code == 0
    assert fake.count(REPORT_USERS) == 0
    return fake


def test_without_fetch_the_gap_is_only_named(listed):
    result = pbi("lake", "users", "report")

    assert result.exit_code == 1 and "none of the users" in result.output
    assert listed.count(REPORT_USERS) == 0  # reading never calls the API


def test_fetch_gets_what_is_missing_and_then_shows_it(listed):
    result = pbi("lake", "users", "report", "--fetch", "--yes")

    assert result.exit_code == 0, result.output
    assert listed.count(REPORT_USERS) == 6
    assert "ann-rep-0001@example.com" in result.output
    assert "may be incomplete" not in result.output
    assert (
        "To fetch the users that the lake lacks" in result.output
    )  # the plan came first


def test_it_asks_first_and_a_no_fetches_nothing(listed):
    result = pbi("lake", "users", "report", "--fetch", input="n\n")

    assert result.exit_code == 1 and "Fetch them now?" in result.output
    assert listed.count(REPORT_USERS) == 0


def test_a_yes_at_the_prompt_fetches(listed):
    result = pbi("lake", "users", "report", "--fetch", input="y\n")

    assert result.exit_code == 0, result.output
    assert listed.count(REPORT_USERS) == 6


def test_what_the_lake_holds_is_not_fetched_again(listed):
    pbi("lake", "users", "report", "--fetch", "--yes")

    again = pbi("lake", "users", "report", "--fetch", "--yes")

    assert again.exit_code == 0, again.output
    assert listed.count(REPORT_USERS) == 6  # nothing was missing: no request
    assert "To fetch the users" not in again.output


def test_a_workspace_limits_what_is_fetched(listed):
    result = pbi("lake", "users", "report", "-w", "ws-0001", "--fetch", "--yes")

    assert result.exit_code == 0, result.output
    assert listed.count(REPORT_USERS) == 1  # the one report of that workspace
    assert "ann-rep-0001@example.com" in result.output


def test_only_the_gaps_cost_a_request(listed):
    pbi("lake", "users", "report", "-w", "ws-0001", "--fetch", "--yes")
    assert listed.count(REPORT_USERS) == 1

    result = pbi("lake", "users", "report", "--fetch", "--yes")

    assert result.exit_code == 0, result.output
    assert listed.count(REPORT_USERS) == 6  # five more, not six


def test_what_one_person_has_access_to_can_fetch_too(listed):
    result = pbi(
        "lake", "access", "ann-rep-0003@example.com", "-k", "report", "--fetch", "--yes"
    )

    assert result.exit_code == 0, result.output
    assert "Report 3" in result.output
    assert listed.count(REPORT_USERS) == 6


def test_a_lake_that_is_only_read_is_not_written_into(listed, tmp_path):
    other = tmp_path / "shared"
    pbi("lake", "publish", str(other), "--yes")

    result = pbi("lake", "users", "report", "--lake", str(other), "--fetch", "--yes")

    assert result.exit_code == 1
    assert "--fetch writes into the lake of your cache folder" in result.output
    assert listed.count(REPORT_USERS) == 0


def test_a_workspace_that_is_not_there_is_named_before_anything_is_fetched(listed):
    result = pbi("lake", "users", "report", "-w", "nope", "--fetch", "--yes")

    assert result.exit_code == 1 and "no workspace 'nope'" in result.output
    assert listed.count(REPORT_USERS) == 0


def test_an_expired_token_stops_it_with_the_way_to_continue(listed):
    listed.expire_token_after(2)

    result = pbi("lake", "users", "report", "--fetch", "--yes")

    assert result.exit_code == 1 and "pbi auth" in result.output
    assert "pbi sync run report-users" in result.output


def test_without_a_kind_the_users_of_every_kind_are_fetched(listed):
    result = pbi("lake", "access", "ann-rep-0003@example.com", "--fetch", "--yes")

    assert result.exit_code == 0, result.output
    assert listed.count(REPORT_USERS) == 6
    assert listed.count(r"^/admin/datasets/[^/]+/users$") == 4
    assert listed.count(r"^/admin/groups/[^/]+/users$") == 12


def test_a_fetch_of_no_kind_is_an_error_not_a_sync_of_the_plain_targets(listed):
    from pbi_cli.cli_sync import fetch_users
    from pbi_cli.errors import PBIError

    before = len(listed.calls)

    with pytest.raises(PBIError, match="no kind of item"):
        fetch_users([], yes=True)

    assert len(listed.calls) == before
