"""What the older commands print and write: one row per item, and files read and written
as UTF-8 whatever the locale of the system is."""

import json
import re

import pandas as pd
from core_helpers import make_response
from typer.testing import CliRunner

from pbi_cli import cli
from pbi_cli.cli import app
from pbi_cli.powerbi import app as powerbi_app
from pbi_cli.powerbi import workspace as powerbi_workspace

ACCESS = re.compile(r"/admin/users/alice%40example\.com/artifactAccess$")
ARTIFACTS = [
    {"artifactId": "a1", "displayName": "Sales", "artifactType": "Report"},
    {"artifactId": "a2", "displayName": "HR", "artifactType": "Dataset"},
]


def run(*args: str):
    return CliRunner().invoke(app, list(args))


# -- users user-access ----------------------------------------------------------------


def test_user_access_prints_one_row_per_artifact(fake_api, signed_in):
    fake_api.add(
        "GET", ACCESS, make_response(200, {"ArtifactAccessEntities": ARTIFACTS})
    )

    result = run("users", "user-access", "-u", "alice@example.com")

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    header = next(line for line in lines if "displayName" in line)
    assert "artifactType" in header and "artifacts" not in header
    assert any("Sales" in line and "Report" in line for line in lines)
    assert any("HR" in line and "Dataset" in line for line in lines)


def test_user_access_says_when_there_is_nothing(fake_api, signed_in):
    fake_api.add("GET", ACCESS, make_response(200, {"ArtifactAccessEntities": []}))

    result = run("users", "user-access", "-u", "alice@example.com")

    assert result.exit_code == 0
    assert "No access found for: alice@example.com" in result.stdout


def test_user_access_writes_one_excel_row_per_artifact(tmp_path, fake_api, signed_in):
    fake_api.add(
        "GET", ACCESS, make_response(200, {"ArtifactAccessEntities": ARTIFACTS})
    )
    target = tmp_path / "out"

    result = run(
        "users",
        "user-access",
        "-u",
        "alice@example.com",
        "-tf",
        str(target),
        "-ft",
        "excel",
    )

    assert result.exit_code == 0
    df = pd.read_excel(target / "alice-example-com.xlsx")
    assert list(df["displayName"]) == ["Sales", "HR"]


# -- workspaces report-users ----------------------------------------------------------

REPORT_USERS = {
    "Finance": [
        {
            "name": "Finance",
            "reports_id": "r-1",
            "reports_name": "Budget",
            "reports_users_value": [
                {"emailAddress": "a@example.com", "reportUserAccessRight": "Owner"},
                {"identifier": "group-1", "principalType": "Group"},
            ],
        }
    ],
    "HR": [],
}


def test_report_users_prints_one_row_per_report(tmp_path, monkeypatch):
    monkeypatch.setattr("pbi_cli.cli.load_auth", lambda profile=None, group="user": {})
    monkeypatch.setattr(
        powerbi_workspace.Workspaces, "_load_cache", lambda self, path: {"x": 1}
    )
    monkeypatch.setattr(
        powerbi_workspace.Workspaces,
        "report_users",
        lambda self, **kwargs: REPORT_USERS,
    )
    source = tmp_path / "workspaces.xlsx"
    source.write_bytes(b"")

    result = run("workspaces", "report-users", "-s", str(source), "-wn", "Finance")

    assert result.exit_code == 0, result.output
    assert "Found 1 report(s) across workspaces" in result.output
    [row] = [line for line in result.output.splitlines() if "Budget" in line]
    assert "Finance" in row and "r-1" in row
    assert "a@example.com, group-1" in row


# -- files are UTF-8 ------------------------------------------------------------------


def watch_text_opens(monkeypatch, module) -> list:
    """The encodings that ``module`` opens text files with."""
    seen: list = []
    real = open

    def watching(file, mode="r", *args, **kwargs):
        if "b" not in mode:
            seen.append(kwargs.get("encoding"))
        return real(file, mode, *args, **kwargs)

    monkeypatch.setattr(module, "open", watching, raising=False)
    return seen


def test_format_convert_reads_its_source_as_utf8(tmp_path, monkeypatch):
    seen = watch_text_opens(monkeypatch, cli)
    source = tmp_path / "workspaces.json"
    source.write_text(
        json.dumps({"value": [{"id": "w", "name": "Zürich"}]}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = run(
        "workspaces",
        "format-convert",
        "-s",
        str(source),
        "-t",
        str(tmp_path / "w.xlsx"),
    )

    assert result.exit_code == 0, result.output
    assert seen == ["utf-8"]


def test_apps_app_writes_its_file_as_utf8(tmp_path, monkeypatch):
    seen = watch_text_opens(monkeypatch, cli)
    monkeypatch.setattr("pbi_cli.cli.load_auth", lambda profile=None, group="user": {})
    monkeypatch.setattr(
        powerbi_app.App, "__call__", lambda self: {"id": "a", "name": "Zürich"}
    )
    target = tmp_path / "app.json"

    result = run("apps", "app", "-a", "a", "-t", str(target))

    assert result.exit_code == 0, result.output
    assert seen == ["utf-8"]
    assert json.loads(target.read_text(encoding="utf-8"))["name"] == "Zürich"


def test_the_apps_file_is_read_as_utf8(tmp_path, monkeypatch):
    seen = watch_text_opens(monkeypatch, powerbi_app)
    source = tmp_path / "apps.json"
    source.write_text(
        json.dumps({"value": [{"id": "a", "name": "Zürich"}]}, ensure_ascii=False),
        encoding="utf-8",
    )

    apps = powerbi_app.Apps(auth={}, cache_file=source)

    assert seen == ["utf-8"]
    assert apps.cache["value"][0]["name"] == "Zürich"
