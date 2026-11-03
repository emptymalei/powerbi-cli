"""``pbi lake users``: who has access to every item of a kind, on the terminal or in a file."""

import csv
import json
import shutil

import pytest
from sync_helpers import World
from typer.testing import CliRunner

from pbi_cli.cli import app
from pbi_cli.config import PBIConfig
from pbi_cli.core.scan import ScanFlags

FULL = ScanFlags(lineage=True, datasource_details=True, get_artifact_users=True)


def users(*args):
    return CliRunner().invoke(app, ["lake", "users", *args])


@pytest.fixture
def work(isolated_home):
    PBIConfig().cache_folder = str(isolated_home / "cache")
    world = World(isolated_home / "cache")
    world.run("groups", "reports", "report-users", "apps", "app-users")
    return world


def test_the_people_of_every_report_are_printed(work):
    result = users("report")

    assert result.exit_code == 0, result.output
    assert (
        "E-mail or id" in result.output and "ann-rep-0001@example.com" in result.output
    )
    assert "different people" in result.output
    assert "pbi sync" not in result.output  # nothing is missing


def test_a_file_has_every_column_and_the_format_follows_its_ending(work, tmp_path):
    target = tmp_path / "out" / "report-users.csv"

    result = users("report", "-o", str(target))

    assert result.exit_code == 0, result.output
    assert "Saved" in result.output and str(target) in result.output
    with open(target, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows and {"Item id", "Workspace id", "Source"} <= set(rows[0])
    assert rows[0]["Source"] == "admin.reports.users"


@pytest.mark.parametrize(
    "name, check",
    [
        ("people.json", lambda t: json.loads(t)[0]["Kind"] == "Report"),
        ("people.md", lambda t: t.startswith("| Kind |")),
        ("people.tsv", lambda t: "\t" in t.splitlines()[0]),
    ],
)
def test_other_formats(work, tmp_path, name, check):
    assert users("report", "-o", str(tmp_path / name)).exit_code == 0
    assert check((tmp_path / name).read_text(encoding="utf-8-sig"))


def test_a_workbook(work, tmp_path):
    from openpyxl import load_workbook

    assert users("app", "-o", str(tmp_path / "apps.xlsx")).exit_code == 0
    sheet = load_workbook(tmp_path / "apps.xlsx").active
    assert sheet["A1"].value == "Kind" and sheet["A2"].value == "App"


def test_the_format_can_be_chosen_when_the_name_does_not_say(work, tmp_path):
    target = tmp_path / "people"

    assert users("report", "-o", str(target), "-f", "json").exit_code == 0
    assert json.loads(target.read_text(encoding="utf-8"))


def test_an_unknown_format_is_refused_before_anything_is_written(work, tmp_path):
    result = users("report", "-o", str(tmp_path / "people.docx"))

    assert result.exit_code == 1 and "Cannot write 'docx'" in result.output
    assert not (tmp_path / "people.docx").exists()


def test_a_workspace_narrows_it_down(work):
    everything = users("report")
    one = users("report", "-w", "ws-0001")

    assert one.exit_code == 0, one.output
    assert len(one.output.splitlines()) < len(everything.output.splitlines())
    assert "Report 1 " in one.output and "Report 2 " not in one.output
    assert "Report 2 " in everything.output
    assert users("report", "-w", "nope").exit_code == 1


def test_items_without_users_are_named_after_the_table(isolated_home):
    PBIConfig().cache_folder = str(isolated_home / "cache")
    world = World(isolated_home / "cache")
    world.run("groups", "reports", "report-users")
    world.run("datasets")  # datasets are listed, nobody who can open them

    result = users("dataset")

    assert result.exit_code == 1
    assert "none of the users of its" in result.output
    assert "--get-artifact-users" in result.output


def test_some_items_without_users_are_a_note_not_an_error(work):
    # the lake forgets the people of one report
    sets = work.store.parameter_sets("tenant-1", "admin.reports.users")
    gone = next(ps for ps in sets if ps.params.get("reportId") == "rep-0002")
    folder = (
        work.store.root
        / "tenant=tenant-1"
        / "endpoint=admin_reports_users"
        / f"params={gone.hash}"
    )
    shutil.rmtree(folder)

    result = users("report")

    assert result.exit_code == 0, result.output
    assert "1 report(s) have no users in the lake: Report 2" in result.output


def test_an_unknown_kind_is_a_usage_error(work):
    result = users("table")

    assert result.exit_code == 2
    assert "is not a kind" in result.output


def test_an_empty_lake_has_nobody_to_list(isolated_home):
    PBIConfig().cache_folder = str(isolated_home / "cache")

    result = users("report")

    assert result.exit_code == 1
