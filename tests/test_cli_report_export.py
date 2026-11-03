"""``pbi reports export`` and ``pbi export``: a report as a .pbix, a PDF, a PowerPoint ..."""

import json
import re

import pytest
from core_helpers import FakeAdapter, make_response
from typer.testing import CliRunner

from pbi_cli.cli import app
from pbi_cli.core import reportexport

GROUP, REPORT = "group-1", "report-1"
EXPORT = f"/groups/{GROUP}/reports/{REPORT}/Export"
START = f"/groups/{GROUP}/reports/{REPORT}/ExportTo"
JOB = re.compile(rf"/reports/{REPORT}/exports/[^/]+$")
FILE = re.compile(rf"/reports/{REPORT}/exports/[^/]+/file$")
ERROR = {"error": {"code": "TokenExpired", "message": "Access token has expired"}}

COMMANDS = [["reports", "export"], ["export"]]
IDS = ["-g", GROUP, "-r", REPORT]


def pbi(*args):
    return CliRunner().invoke(app, list(args))


def binary(content: bytes, status=200, **headers):
    response = make_response(status, headers=headers)
    response._content = content
    return response


def pdf_job(adapter: FakeAdapter, content=b"%PDF", extension=".pdf"):
    adapter.add(
        "POST", START, make_response(202, {"id": "job-1", "status": "NotStarted"})
    )
    adapter.add(
        "GET",
        JOB,
        make_response(
            200,
            {
                "id": "job-1",
                "status": "Succeeded",
                "percentComplete": 100,
                "reportName": "Sales: Q1",
                "resourceFileExtension": extension,
            },
        ),
    )
    adapter.add("GET", FILE, binary(content))


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(reportexport.time, "sleep", lambda seconds: None)


@pytest.mark.parametrize("command", COMMANDS)
def test_a_pbix_is_saved_where_asked(command, fake_api, signed_in, tmp_path):
    fake_api.add("GET", EXPORT, binary(b"PBIX"))
    target = tmp_path / "out" / "sales.pbix"

    result = pbi(*command, *IDS, "-t", str(target))

    assert result.exit_code == 0, result.output
    assert "Export saved" in result.output
    assert target.read_bytes() == b"PBIX"  # the folder was made too
    assert fake_api.requests[0].headers["Authorization"].startswith("Bearer ")


@pytest.mark.parametrize("command", COMMANDS)
def test_an_error_answer_is_never_saved_as_the_report(
    command, fake_api, signed_in, tmp_path
):
    fake_api.add("GET", EXPORT, make_response(401, ERROR))
    target = tmp_path / "report.pbix"

    result = pbi(*command, *IDS, "-t", str(target))

    assert result.exit_code == 1 and "Traceback" not in result.output
    assert "pbi auth" in result.output
    assert not target.exists()


def test_without_a_target_it_says_what_came_and_saves_nothing(
    fake_api, signed_in, tmp_path, monkeypatch
):
    fake_api.add("GET", EXPORT, binary(b"PBIX"))
    monkeypatch.chdir(tmp_path)

    result = pbi("reports", "export", *IDS)

    assert result.exit_code == 0, result.output
    assert "Content size: 4 bytes" in result.output and "Format: pbix" in result.output
    assert list(tmp_path.iterdir()) == []


def test_a_pdf_is_made_waited_for_and_saved(fake_api, signed_in, tmp_path):
    pdf_job(fake_api)
    target = tmp_path / "sales.pdf"

    result = pbi("reports", "export", *IDS, "-f", "pdf", "-t", str(target))

    assert result.exit_code == 0, result.output
    assert target.read_bytes() == b"%PDF"
    assert "Succeeded 100%" in result.output
    assert json.loads(fake_api.requests[0].body) == {"format": "PDF"}


def test_pages_filter_and_locale_reach_the_request(fake_api, signed_in, tmp_path):
    pdf_job(fake_api)

    result = pbi(
        "reports",
        "export",
        *IDS,
        "-f",
        "PDF",  # the case does not matter
        "-p",
        "ReportSection1",
        "-p",
        "ReportSection3",
        "--filter",
        "Store/Territory eq 'NC'",
        "--locale",
        "de-de",
        "--include-hidden-pages",
        "-t",
        str(tmp_path / "x.pdf"),
    )

    assert result.exit_code == 0, result.output
    assert json.loads(fake_api.requests[0].body)["powerBIReportConfiguration"] == {
        "pages": [{"pageName": "ReportSection1"}, {"pageName": "ReportSection3"}],
        "reportLevelFilters": [{"filter": "Store/Territory eq 'NC'"}],
        "settings": {"locale": "de-de", "includeHiddenPages": True},
    }


def test_paginated_parameters_are_name_value_pairs(fake_api, signed_in, tmp_path):
    pdf_job(fake_api, extension=".xlsx")

    result = pbi(
        "reports", "export", *IDS, "-f", "xlsx", "--param", "Year=2026",
        "--param", "Region=a=b", "-t", str(tmp_path / "x.xlsx"),
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    assert json.loads(fake_api.requests[0].body)["paginatedReportConfiguration"] == {
        "parameterValues": [
            {"name": "Year", "value": "2026"},
            {"name": "Region", "value": "a=b"},
        ]
    }
    bad = pbi("reports", "export", *IDS, "-f", "xlsx", "--param", "Year")
    assert bad.exit_code == 2 and "is not NAME=VALUE" in bad.output


def test_a_name_without_an_ending_gets_the_one_of_the_format(
    fake_api, signed_in, tmp_path
):
    pdf_job(fake_api)

    result = pbi("reports", "export", *IDS, "-f", "pdf", "-t", str(tmp_path / "sales"))

    assert result.exit_code == 0, result.output
    assert (tmp_path / "sales.pdf").read_bytes() == b"%PDF"


def test_a_folder_gets_a_file_named_after_the_report(fake_api, signed_in, tmp_path):
    pdf_job(fake_api)

    result = pbi("reports", "export", *IDS, "-f", "pdf", "-t", str(tmp_path))

    assert result.exit_code == 0, result.output
    assert (tmp_path / "Sales- Q1.pdf").read_bytes() == b"%PDF"  # no ":" in a name


def test_pages_as_images_are_a_zip_and_the_name_is_checked_against_it(
    fake_api, signed_in, tmp_path
):
    pdf_job(fake_api, content=b"PK", extension=".zip")
    target = tmp_path / "pages.png"

    result = pbi("reports", "export", *IDS, "-f", "png", "-t", str(target))

    assert result.exit_code == 0, result.output
    assert target.read_bytes() == b"PK"
    assert "Power BI sent a .zip file, the name says .png" in result.output


def test_a_report_that_cannot_be_exported_says_why_and_saves_nothing(
    fake_api, signed_in, tmp_path
):
    fake_api.add("POST", START, make_response(202, {"id": "job-1"}))
    fake_api.add("GET", JOB, make_response(200, {"id": "job-1", "status": "Failed"}))
    target = tmp_path / "x.pdf"

    result = pbi("reports", "export", *IDS, "-f", "pdf", "-t", str(target))

    assert result.exit_code == 1 and "Traceback" not in result.output
    assert "Premium, Embedded or Fabric" in result.output
    assert not target.exists()


def test_a_format_or_option_that_does_not_fit_is_a_line_not_a_traceback(
    fake_api, signed_in
):
    unknown = pbi("reports", "export", *IDS, "-f", "gif")
    mixed = pbi("reports", "export", *IDS, "-f", "pbix", "-p", "ReportSection1")

    assert unknown.exit_code == 1 and "Cannot export to 'gif'" in unknown.output
    assert mixed.exit_code == 1 and "the whole report" in mixed.output
    assert fake_api.requests == []


def test_the_timeout_is_passed_on(fake_api, signed_in, monkeypatch):
    seen = {}

    def fake_export(*args, **kwargs):
        seen.update(kwargs)
        return reportexport.ExportedReport(b"x", ".pdf", "", "pdf")

    monkeypatch.setattr("pbi_cli.cli.export_report", fake_export)

    assert pbi("reports", "export", *IDS, "-f", "pdf", "--timeout", "42").exit_code == 0

    assert seen["timeout"] == 42.0
    assert pbi("reports", "export", *IDS, "--timeout", "0").exit_code == 2
