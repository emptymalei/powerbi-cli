"""Tests for report CLI commands (under pbi reports group)."""

import json
import logging
import re
from datetime import timedelta
from unittest.mock import patch

import requests
from core_helpers import make_response
from typer.testing import CliRunner

from pbi_cli.cli import app
from pbi_cli.core.store import LakeStore
from pbi_cli.powerbi.report import GroupReports

# The commands go through the API client; its answers are scripted with ``fake_api``.
REPORTS_URL = re.compile(r"/groups/group-1/reports$")
PAGES_URL = re.compile(r"/groups/group-1/reports/[^/]+/pages$")
REPORT_1 = {"id": "report-1", "name": "Sales"}
PAGES = {"value": [{"name": "p1", "displayName": "Overview"}]}

# ---------------------------------------------------------------------------
# Help / discovery tests
# ---------------------------------------------------------------------------


def test_reports_group_help():
    """Test that the reports group is accessible and shows expected subcommands."""
    runner = CliRunner()
    result = runner.invoke(app, ["reports", "--help"])
    assert result.exit_code == 0
    assert "list" in result.output
    assert "pages" in result.output
    assert "all-pages" not in result.output


def test_reports_list_help():
    """Test that reports list shows required options."""
    runner = CliRunner()
    result = runner.invoke(app, ["reports", "list", "--help"])
    assert result.exit_code == 0
    assert "--group-id" in result.output or "-g" in result.output


def test_reports_pages_help():
    """Test that reports pages shows required and optional options."""
    runner = CliRunner()
    result = runner.invoke(app, ["reports", "pages", "--help"])
    assert result.exit_code == 0
    assert "--group-id" in result.output or "-g" in result.output
    assert "--report-id" in result.output or "-r" in result.output


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


def test_reports_list_requires_group_id():
    """Test that reports list fails when group-id is missing."""
    runner = CliRunner()
    result = runner.invoke(app, ["reports", "list"])
    assert result.exit_code != 0


def test_reports_pages_requires_group_id():
    """Test that reports pages fails when group-id is missing."""
    runner = CliRunner()
    result = runner.invoke(app, ["reports", "pages"])
    assert result.exit_code != 0


def test_reports_pages_succeeds_without_report_id(fake_api, signed_in):
    """Test that reports pages works when report-id is omitted (all-pages mode)."""
    fake_api.add("GET", REPORTS_URL, make_response(200, {"value": [REPORT_1]}))
    fake_api.add("GET", PAGES_URL, make_response(200, PAGES))
    runner = CliRunner()
    result = runner.invoke(app, ["reports", "pages", "-g", "group-1"])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Functional tests – list
# ---------------------------------------------------------------------------


def test_reports_list_prints_to_console(fake_api, signed_in):
    """Test that reports list prints JSON to console when no target given."""
    fake_response = {
        "@odata.context": "https://api.powerbi.com/v1.0/myorg/$metadata#groups('group-1')/reports",
        "value": [
            {"id": "report-1", "name": "My Report", "webUrl": "https://example.com"},
        ],
    }
    fake_api.add("GET", REPORTS_URL, make_response(200, fake_response))

    runner = CliRunner()
    result = runner.invoke(app, ["reports", "list", "-g", "group-1"])

    assert result.exit_code == 0
    # stdout is the JSON and nothing else, so it can be piped
    assert json.loads(result.stdout) == fake_response
    assert result.stderr == ""


def test_reports_list_saves_to_file(tmp_path, fake_api, signed_in):
    """Test that reports list saves JSON to a file when target is given."""
    fake_response = {
        "value": [{"id": "report-2", "name": "Another Report"}],
    }
    fake_api.add("GET", REPORTS_URL, make_response(200, fake_response))
    target_file = tmp_path / "reports.json"

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["reports", "list", "-g", "group-1", "-t", str(target_file)],
    )

    assert result.exit_code == 0
    assert target_file.exists()
    with open(target_file) as fp:
        saved = json.load(fp)
    assert saved["value"][0]["id"] == "report-2"


def test_reports_list_asks_the_api_for_the_group(fake_api, signed_in):
    fake_api.add("GET", REPORTS_URL, make_response(200, {"value": []}))

    CliRunner().invoke(app, ["reports", "list", "-g", "group-1"])

    assert fake_api.urls() == [
        "https://api.powerbi.com/v1.0/myorg/groups/group-1/reports"
    ]
    assert fake_api.calls()[0].headers["Authorization"] == f"Bearer {signed_in}"


def test_reports_list_keeps_the_answer_in_the_lake(cache_folder, fake_api, signed_in):
    body = {"value": [{"id": "report-1", "name": "Sales"}]}
    fake_api.add("GET", REPORTS_URL, make_response(200, body))

    CliRunner().invoke(app, ["reports", "list", "-g", "group-1"])

    store = LakeStore(cache_folder / "lake")
    snapshot = store.latest("tenant-1", "user.group_reports", {"groupId": "group-1"})
    assert snapshot is not None
    assert snapshot.load() == body


def test_reports_list_always_asks_the_api_and_adds_a_version(
    cache_folder, fake_api, signed_in
):
    """There are no cache flags: every call is live and adds to the lake."""
    fake_api.add("GET", REPORTS_URL, make_response(200, {"value": []}))

    runner = CliRunner()
    runner.invoke(app, ["reports", "list", "-g", "group-1"])
    runner.invoke(app, ["reports", "list", "-g", "group-1"])

    assert len(fake_api.calls()) == 2
    versions = LakeStore(cache_folder / "lake").versions(
        "tenant-1", "user.group_reports", {"groupId": "group-1"}
    )
    assert len(versions) == 2


def test_reports_list_works_without_a_cache_folder(tmp_path, fake_api, signed_in):
    fake_api.add("GET", REPORTS_URL, make_response(200, {"value": []}))

    result = CliRunner().invoke(app, ["reports", "list", "-g", "group-1"])

    assert result.exit_code == 0
    assert not list(tmp_path.rglob("tenant=*"))


def test_reports_list_reports_api_errors(fake_api, signed_in):
    fake_api.add(
        "GET",
        REPORTS_URL,
        make_response(404, {"error": {"code": "PowerBIEntityNotFound"}}),
    )

    result = CliRunner().invoke(app, ["reports", "list", "-g", "group-1"])

    assert result.exit_code == 1
    assert "Error: user.group_reports: not found (404)" in result.output
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# Functional tests – pages (single report)
# ---------------------------------------------------------------------------


def test_reports_pages_with_report_id_prints_to_console(fake_api, signed_in):
    """Test that reports pages with --report-id prints single-report JSON to console."""
    fake_response = {
        "@odata.context": "...",
        "value": [
            {"name": "ReportSection1", "displayName": "Overview", "order": 0},
            {"name": "ReportSection2", "displayName": "Details", "order": 1},
        ],
    }
    fake_api.add("GET", PAGES_URL, make_response(200, fake_response))

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["reports", "pages", "-g", "group-1", "-r", "report-1"],
    )

    assert result.exit_code == 0
    output = json.loads(result.stdout)
    assert output["value"][0]["displayName"] == "Overview"
    assert fake_api.urls() == [
        "https://api.powerbi.com/v1.0/myorg/groups/group-1/reports/report-1/pages"
    ]


def test_reports_pages_with_report_id_saves_to_file(tmp_path, fake_api, signed_in):
    """Test that reports pages with --report-id saves JSON to a file."""
    fake_response = {
        "value": [{"name": "ReportSection1", "displayName": "Page 1", "order": 0}],
    }
    fake_api.add("GET", PAGES_URL, make_response(200, fake_response))
    target_file = tmp_path / "pages.json"

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reports",
            "pages",
            "-g",
            "group-1",
            "-r",
            "report-1",
            "-t",
            str(target_file),
        ],
    )

    assert result.exit_code == 0
    assert target_file.exists()
    with open(target_file) as fp:
        saved = json.load(fp)
    assert saved["value"][0]["displayName"] == "Page 1"


def test_reports_pages_keeps_the_answer_in_the_lake(cache_folder, fake_api, signed_in):
    fake_api.add("GET", PAGES_URL, make_response(200, PAGES))

    CliRunner().invoke(app, ["reports", "pages", "-g", "group-1", "-r", "report-1"])

    snapshot = LakeStore(cache_folder / "lake").latest(
        "tenant-1",
        "user.report_pages",
        {"groupId": "group-1", "reportId": "report-1"},
    )
    assert snapshot is not None and snapshot.load() == PAGES


# ---------------------------------------------------------------------------
# Functional tests – pages (all reports, no report-id)
# ---------------------------------------------------------------------------


def test_reports_pages_without_report_id_prints_to_console(fake_api, signed_in):
    """Test that reports pages without --report-id prints combined JSON to console."""
    fake_api.add(
        "GET",
        REPORTS_URL,
        make_response(
            200,
            {
                "value": [
                    {"id": "report-1", "name": "Sales Report"},
                    {"id": "report-2", "name": "HR Report"},
                ]
            },
        ),
    )
    fake_api.add(
        "GET",
        re.compile(r"/reports/report-1/pages$"),
        make_response(
            200, {"value": [{"name": "ReportSection1", "displayName": "Overview"}]}
        ),
    )
    fake_api.add(
        "GET",
        re.compile(r"/reports/report-2/pages$"),
        make_response(
            200, {"value": [{"name": "ReportSection1", "displayName": "Summary"}]}
        ),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["reports", "pages", "-g", "group-1"])

    assert result.exit_code == 0
    output = json.loads(result.stdout)
    assert len(output) == 2
    assert output[0]["report_id"] == "report-1"
    assert output[0]["pages"]["value"][0]["displayName"] == "Overview"
    assert output[1]["report_name"] == "HR Report"


def test_reports_pages_without_report_id_saves_to_file(tmp_path, fake_api, signed_in):
    """Test that reports pages without --report-id saves combined JSON to a file."""
    fake_api.add(
        "GET",
        REPORTS_URL,
        make_response(200, {"value": [{"id": "report-3", "name": "Finance"}]}),
    )
    fake_api.add(
        "GET",
        PAGES_URL,
        make_response(200, {"value": [{"name": "p1", "displayName": "Intro"}]}),
    )
    target_file = tmp_path / "all_pages.json"

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["reports", "pages", "-g", "group-1", "-t", str(target_file)],
    )

    assert result.exit_code == 0
    assert target_file.exists()
    with open(target_file) as fp:
        saved = json.load(fp)
    assert saved[0]["report_id"] == "report-3"


def test_reports_pages_without_report_id_skips_a_report_it_cannot_read(
    cache_folder, fake_api, signed_in
):
    """One report without access does not spoil the others (as GroupReports.all_pages)."""
    fake_api.add(
        "GET",
        REPORTS_URL,
        make_response(
            200,
            {
                "value": [
                    {"id": "report-ok", "name": "Good Report"},
                    {"id": "report-bad", "name": "Bad Report"},
                ]
            },
        ),
    )
    fake_api.add(
        "GET",
        re.compile(r"/reports/report-bad/pages$"),
        make_response(403, {"error": {"code": "PowerBIEntityNotFound"}}),
    )
    fake_api.add("GET", PAGES_URL, make_response(200, PAGES))

    result = CliRunner().invoke(app, ["reports", "pages", "-g", "group-1"])

    assert result.exit_code == 0
    assert [r["report_id"] for r in json.loads(result.stdout)] == ["report-ok"]
    # every answer that was received is in the lake: the list and the readable pages
    store = LakeStore(cache_folder / "lake")
    assert store.latest(
        "tenant-1",
        "user.report_pages",
        {"groupId": "group-1", "reportId": "report-ok"},
    )
    assert not store.latest(
        "tenant-1",
        "user.report_pages",
        {"groupId": "group-1", "reportId": "report-bad"},
    )


def test_reports_pages_without_report_id_stops_when_the_token_is_rejected(
    fake_api, signed_in
):
    """A problem that affects every request is not hidden behind 'skipped' reports."""
    fake_api.add(
        "GET",
        REPORTS_URL,
        make_response(200, {"value": [{"id": "report-1", "name": "A"}]}),
    )
    fake_api.add(
        "GET", PAGES_URL, make_response(401, {"error": {"code": "TokenExpired"}})
    )

    result = CliRunner().invoke(app, ["reports", "pages", "-g", "group-1"])

    assert result.exit_code == 1
    assert "Power BI rejected the token" in result.output


def test_reports_pages_without_report_id_looks_the_token_up_once(fake_api, monkeypatch):
    """Not in the settings and the keyring again for each of the requests."""
    from core_helpers import make_token

    calls = []
    token = make_token(expires_in=timedelta(days=36500))

    def load_auth(profile=None, group="user"):
        calls.append(group)
        return {"Authorization": f"Bearer {token}"}

    monkeypatch.setattr("pbi_cli.cli.load_auth", load_auth)
    fake_api.add(
        "GET",
        REPORTS_URL,
        make_response(
            200, {"value": [{"id": f"report-{i}", "name": str(i)} for i in range(4)]}
        ),
    )
    fake_api.add("GET", PAGES_URL, make_response(200, PAGES))

    result = CliRunner().invoke(app, ["reports", "pages", "-g", "group-1"])

    assert result.exit_code == 0
    assert len(fake_api.calls()) == 5
    assert calls == ["user"]


def test_reports_pages_without_report_id_uses_one_connection_for_all_reports(
    fake_api, signed_in, monkeypatch
):
    sessions = []
    monkeypatch.setattr(
        "pbi_cli.core.client.make_session",
        lambda: sessions.append(1) or fake_api.session(),
    )
    fake_api.add(
        "GET",
        REPORTS_URL,
        make_response(
            200,
            {"value": [{"id": f"report-{i}", "name": str(i)} for i in range(5)]},
        ),
    )
    fake_api.add("GET", PAGES_URL, make_response(200, PAGES))

    CliRunner().invoke(app, ["reports", "pages", "-g", "group-1"])

    assert len(fake_api.calls()) == 6
    assert len(sessions) == 1


# ---------------------------------------------------------------------------
# Unit tests – GroupReports.all_pages error handling
# ---------------------------------------------------------------------------


def test_all_pages_skips_failed_reports_and_logs_error(caplog):
    """Test that all_pages skips reports whose pages cannot be fetched."""
    fake_reports = {
        "value": [
            {"id": "report-ok", "name": "Good Report"},
            {"id": "report-bad", "name": "Bad Report"},
        ]
    }

    def fake_pages(self):
        if self.report_id == "report-bad":
            raise requests.HTTPError("404 Not Found")
        return {"value": [{"name": "p1", "displayName": "Page 1"}]}

    with patch(
        "pbi_cli.powerbi.report.GroupReports.reports",
        new_callable=lambda: property(lambda self: fake_reports),
    ):
        with patch(
            "pbi_cli.powerbi.report.Report.pages",
            new_callable=lambda: property(fake_pages),
        ):
            group = GroupReports(
                auth={"Authorization": "Bearer test"},
                group_id="group-1",
                verify=False,
            )
            with caplog.at_level(logging.ERROR, logger="pbi_cli.powerbi.report"):
                result = group.all_pages()

    # Only the successful report is included
    assert len(result) == 1
    assert result[0]["report_id"] == "report-ok"

    # An error was logged for the failing report
    assert any(
        "report-bad" in record.message or "Bad Report" in record.message
        for record in caplog.records
    )
