"""Tests for report CLI commands (under pbi reports group)."""

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from click.testing import CliRunner

from pbi_cli.cli import pbi
from pbi_cli.powerbi.report import GroupReports

# ---------------------------------------------------------------------------
# Help / discovery tests
# ---------------------------------------------------------------------------


def test_reports_group_help():
    """Test that the reports group is accessible and shows expected subcommands."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["reports", "--help"])
    assert result.exit_code == 0
    assert "list" in result.output
    assert "pages" in result.output
    assert "all-pages" not in result.output


def test_reports_list_help():
    """Test that reports list shows required options."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["reports", "list", "--help"])
    assert result.exit_code == 0
    assert "--group-id" in result.output or "-g" in result.output


def test_reports_pages_help():
    """Test that reports pages shows required and optional options."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["reports", "pages", "--help"])
    assert result.exit_code == 0
    assert "--group-id" in result.output or "-g" in result.output
    assert "--report-id" in result.output or "-r" in result.output


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


def test_reports_list_requires_group_id():
    """Test that reports list fails when group-id is missing."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["reports", "list"])
    assert result.exit_code != 0


def test_reports_pages_requires_group_id():
    """Test that reports pages fails when group-id is missing."""
    runner = CliRunner()
    result = runner.invoke(pbi, ["reports", "pages"])
    assert result.exit_code != 0


def test_reports_pages_succeeds_without_report_id():
    """Test that reports pages works when report-id is omitted (all-pages mode)."""
    fake_all_pages = [
        {
            "report_id": "report-1",
            "report_name": "Sales",
            "pages": {"value": [{"name": "p1", "displayName": "Overview"}]},
        }
    ]
    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.report.GroupReports.all_pages",
            return_value=fake_all_pages,
        ):
            result = runner.invoke(pbi, ["reports", "pages", "-g", "group-1"])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Functional tests – list
# ---------------------------------------------------------------------------


def test_reports_list_prints_to_console():
    """Test that reports list prints JSON to console when no target given."""
    fake_response = {
        "@odata.context": "https://api.powerbi.com/v1.0/myorg/$metadata#groups('group-1')/reports",
        "value": [
            {"id": "report-1", "name": "My Report", "webUrl": "https://example.com"},
        ],
    }

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.report.GroupReports.reports",
            new_callable=lambda: property(lambda self: fake_response),
        ):
            result = runner.invoke(pbi, ["reports", "list", "-g", "group-1"])

    assert result.exit_code == 0
    output = json.loads(result.output)
    assert output["value"][0]["id"] == "report-1"


def test_reports_list_saves_to_file(tmp_path):
    """Test that reports list saves JSON to a file when target is given."""
    fake_response = {
        "value": [{"id": "report-2", "name": "Another Report"}],
    }
    target_file = tmp_path / "reports.json"

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.report.GroupReports.reports",
            new_callable=lambda: property(lambda self: fake_response),
        ):
            result = runner.invoke(
                pbi,
                ["reports", "list", "-g", "group-1", "-t", str(target_file)],
            )

    assert result.exit_code == 0
    assert target_file.exists()
    with open(target_file) as fp:
        saved = json.load(fp)
    assert saved["value"][0]["id"] == "report-2"


# ---------------------------------------------------------------------------
# Functional tests – pages (single report)
# ---------------------------------------------------------------------------


def test_reports_pages_with_report_id_prints_to_console():
    """Test that reports pages with --report-id prints single-report JSON to console."""
    fake_response = {
        "@odata.context": "...",
        "value": [
            {"name": "ReportSection1", "displayName": "Overview", "order": 0},
            {"name": "ReportSection2", "displayName": "Details", "order": 1},
        ],
    }

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.report.Report.pages",
            new_callable=lambda: property(lambda self: fake_response),
        ):
            result = runner.invoke(
                pbi,
                ["reports", "pages", "-g", "group-1", "-r", "report-1"],
            )

    assert result.exit_code == 0
    output = json.loads(result.output)
    assert output["value"][0]["displayName"] == "Overview"


def test_reports_pages_with_report_id_saves_to_file(tmp_path):
    """Test that reports pages with --report-id saves JSON to a file."""
    fake_response = {
        "value": [{"name": "ReportSection1", "displayName": "Page 1", "order": 0}],
    }
    target_file = tmp_path / "pages.json"

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.report.Report.pages",
            new_callable=lambda: property(lambda self: fake_response),
        ):
            result = runner.invoke(
                pbi,
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


# ---------------------------------------------------------------------------
# Functional tests – pages (all reports, no report-id)
# ---------------------------------------------------------------------------


def test_reports_pages_without_report_id_prints_to_console():
    """Test that reports pages without --report-id prints combined JSON to console."""
    fake_all_pages = [
        {
            "report_id": "report-1",
            "report_name": "Sales Report",
            "pages": {"value": [{"name": "ReportSection1", "displayName": "Overview"}]},
        },
        {
            "report_id": "report-2",
            "report_name": "HR Report",
            "pages": {"value": [{"name": "ReportSection1", "displayName": "Summary"}]},
        },
    ]

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.report.GroupReports.all_pages",
            return_value=fake_all_pages,
        ):
            result = runner.invoke(pbi, ["reports", "pages", "-g", "group-1"])

    assert result.exit_code == 0
    output = json.loads(result.output)
    assert len(output) == 2
    assert output[0]["report_id"] == "report-1"
    assert output[1]["report_name"] == "HR Report"


def test_reports_pages_without_report_id_saves_to_file(tmp_path):
    """Test that reports pages without --report-id saves combined JSON to a file."""
    fake_all_pages = [
        {
            "report_id": "report-3",
            "report_name": "Finance",
            "pages": {"value": [{"name": "p1", "displayName": "Intro"}]},
        }
    ]
    target_file = tmp_path / "all_pages.json"

    runner = CliRunner()
    with patch("pbi_cli.cli.load_auth", return_value={"Authorization": "Bearer test"}):
        with patch(
            "pbi_cli.powerbi.report.GroupReports.all_pages",
            return_value=fake_all_pages,
        ):
            result = runner.invoke(
                pbi,
                ["reports", "pages", "-g", "group-1", "-t", str(target_file)],
            )

    assert result.exit_code == 0
    assert target_file.exists()
    with open(target_file) as fp:
        saved = json.load(fp)
    assert saved[0]["report_id"] == "report-3"


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
