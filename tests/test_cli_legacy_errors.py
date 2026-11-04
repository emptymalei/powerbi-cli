"""The commands built on the older API wrappers (``pbi_cli.powerbi``) report a failed
request as ``Error: ...`` with exit status 1, never with a traceback, and never save an
error answer as if it were the data."""

import json
import re

import pytest
import requests
from core_helpers import FakeAdapter, make_response
from typer.testing import CliRunner

from pbi_cli.cli import app
from pbi_cli.cli_support import describe_request_error
from pbi_cli.powerbi.admin import ScanNotReadyError, Workspaces
from pbi_cli.powerbi.base import ApiResponseError
from pbi_cli.web import DataRetriever

USER_TOKEN = {"Authorization": "Bearer user-token"}
ADMIN_TOKEN = {"Authorization": "Bearer admin-token"}
ERROR_BODY = {"error": {"code": "TokenExpired", "message": "Access token has expired"}}


def run(*args: str):
    return CliRunner().invoke(app, list(args))


def answer(status: int, body=None, reason: str = "", text=None) -> requests.Response:
    response = make_response(status, body, text=text)
    response.reason = reason
    return response


@pytest.fixture
def legacy_api(monkeypatch) -> FakeAdapter:
    """Answer the requests of the older wrappers (`DataRetriever`) from a script."""
    adapter = FakeAdapter()
    monkeypatch.setattr(
        DataRetriever, "session", property(lambda self: adapter.session())
    )
    return adapter


@pytest.fixture
def tokens(monkeypatch):
    """``load_auth`` returns a user token, and another one for the admin group."""
    monkeypatch.setattr(
        "pbi_cli.cli.load_auth",
        lambda profile=None, group="user": dict(
            ADMIN_TOKEN if group == "admin" else USER_TOKEN
        ),
    )


def assert_clean_error(result, *expected: str):
    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Error: " in result.output
    for text in expected:
        assert text in result.output


# -- pbi export -----------------------------------------------------------------------


def test_pbi_export_does_not_save_an_error_answer_as_the_report(
    tmp_path, legacy_api, tokens
):
    legacy_api.add("GET", "/Export", answer(401, ERROR_BODY, "Unauthorized"))
    target = tmp_path / "report.pbix"

    result = run("export", "-g", "group-1", "-r", "report-1", "-t", str(target))

    assert_clean_error(result, "401 Unauthorized", "TokenExpired")
    assert "Export saved" not in result.output
    assert not target.exists()


def test_pbi_export_still_saves_a_report(tmp_path, legacy_api, tokens):
    legacy_api.add("GET", "/Export", answer(200, text="PBIX"))
    target = tmp_path / "report.pbix"

    result = run("export", "-g", "group-1", "-r", "report-1", "-t", str(target))

    assert result.exit_code == 0
    assert target.read_bytes() == b"PBIX"


# -- the commands on the older wrappers -----------------------------------------------


def test_reports_export_reports_an_http_error(tmp_path, legacy_api, tokens):
    legacy_api.add("GET", "/Export", answer(404, ERROR_BODY, "Not Found"))

    result = run("reports", "export", "-g", "group-1", "-r", "report-1")

    assert_clean_error(result, "404 Not Found", "/reports/report-1/Export")


def test_scan_initiate_reports_an_empty_401(legacy_api, tokens):
    legacy_api.add("POST", "/getInfo", answer(401, reason="Unauthorized"))

    result = run("workspaces", "scan", "initiate", "ws-1")

    assert_clean_error(result, "401 Unauthorized", "pbi auth")


def test_scan_result_reports_an_http_error(legacy_api, tokens):
    legacy_api.add("GET", "/scanResult/", answer(403, ERROR_BODY, "Forbidden"))

    result = run("workspaces", "scan", "result", "scan-1")

    assert_clean_error(result, "403 Forbidden")


def test_scan_result_says_a_result_that_is_not_ready_yet(legacy_api, tokens):
    legacy_api.add("GET", "/scanResult/", answer(202, reason="Accepted"))

    result = run("workspaces", "scan", "result", "scan-1")

    assert_clean_error(result, "Scan scan-1 is not ready yet")


def test_scan_get_waits_for_a_result_that_is_not_there_yet(legacy_api, tokens):
    legacy_api.add("POST", "/getInfo", answer(202, {"id": "scan-1"}, "Accepted"))
    legacy_api.add(
        "GET", "/scanStatus/", answer(200, {"id": "scan-1", "status": "Succeeded"})
    )
    legacy_api.add(
        "GET",
        "/scanResult/",
        answer(202, reason="Accepted"),
        answer(200, {"workspaces": [{"id": "ws-1"}]}),
    )

    result = run("workspaces", "scan", "get", "ws-1", "--interval", "0.01")

    assert result.exit_code == 0, result.output
    assert '"id": "ws-1"' in result.output


def test_scan_get_reports_a_result_that_never_comes(legacy_api, tokens):
    legacy_api.add("POST", "/getInfo", answer(202, {"id": "scan-1"}, "Accepted"))
    legacy_api.add(
        "GET", "/scanStatus/", answer(200, {"id": "scan-1", "status": "Succeeded"})
    )
    legacy_api.add("GET", "/scanResult/", answer(202, reason="Accepted"))

    result = run(
        "workspaces", "scan", "get", "ws-1", "--interval", "0.01", "--timeout", "0.05"
    )

    assert_clean_error(result, "Scan scan-1 is not ready yet")


def test_apps_app_reports_an_error_answer(legacy_api, tokens):
    legacy_api.add("GET", "/apps/app-1", answer(401, ERROR_BODY, "Unauthorized"))

    result = run("apps", "app", "-a", "app-1")

    assert_clean_error(result, "TokenExpired: Access token has expired")


def test_apps_app_reports_an_answer_that_is_not_json(legacy_api, tokens):
    legacy_api.add("GET", "/apps/app-1", answer(401, reason="Unauthorized"))

    result = run("apps", "app", "-a", "app-1")

    assert_clean_error(result, "not JSON")


def test_apps_augment_reports_a_network_failure(tmp_path, legacy_api, tokens):
    source = tmp_path / "apps.json"
    source.write_text(json.dumps({"value": [{"id": "app-1", "name": "A"}]}))
    legacy_api.add("GET", "/apps/app-1", requests.ConnectionError("no network"))
    target = tmp_path / "out.json"

    result = run("apps", "augment", "-s", str(source), "-t", str(target))

    assert result.exit_code == 0, result.output
    assert "Cannot download" in result.output and "no network" in result.output
    assert json.loads(target.read_text()) == []


# -- pbi reports users ----------------------------------------------------------------

APP = {"id": "app-1", "name": "A"}
REPORTS = {"value": [{"id": "r-1", "name": "One"}, {"id": "r-2", "name": "Two"}]}


def script_app(legacy_api: FakeAdapter) -> None:
    legacy_api.add("GET", re.compile(r"/myorg/apps/app-1$"), answer(200, dict(APP)))
    legacy_api.add("GET", "/myorg/apps/app-1/reports", answer(200, REPORTS))
    legacy_api.add("GET", "/myorg/apps/app-1/dashboards", answer(200, {"value": []}))


def test_reports_users_asks_with_the_admin_token(tmp_path, legacy_api, tokens):
    source = tmp_path / "apps.json"
    source.write_text(json.dumps({"value": [APP]}))
    script_app(legacy_api)
    legacy_api.add("GET", "/admin/reports/", answer(200, {"value": []}))
    target = tmp_path / "out.json"

    result = run("reports", "users", "-s", str(source), "-t", str(target))

    assert result.exit_code == 0, result.output
    admin_calls = [r for r in legacy_api.requests if "/admin/reports/" in r.url]
    assert len(admin_calls) == 2
    assert all(r.headers["Authorization"] == "Bearer admin-token" for r in admin_calls)
    user_calls = [r for r in legacy_api.requests if "/admin/" not in r.url]
    assert all(r.headers["Authorization"] == "Bearer user-token" for r in user_calls)


def test_reports_users_goes_on_after_a_report_that_fails(tmp_path, legacy_api, tokens):
    source = tmp_path / "apps.json"
    source.write_text(json.dumps({"value": [APP]}))
    script_app(legacy_api)
    legacy_api.add("GET", "/admin/reports/r-1/", answer(403, ERROR_BODY, "Forbidden"))
    legacy_api.add(
        "GET", "/admin/reports/r-2/", answer(200, {"value": [{"identifier": "x"}]})
    )
    target = tmp_path / "out.json"

    result = run("reports", "users", "-s", str(source), "-t", str(target))

    assert result.exit_code == 0, result.output
    assert "Can not download the users of report One (r-1)" in result.output
    assert "403 Forbidden" in result.output
    assert "1 report(s) could not be downloaded: r-1" in result.output
    [saved] = json.loads(target.read_text())
    assert [r["id"] for r in saved["reports"]] == ["r-2"]


# -- the messages ---------------------------------------------------------------------


def test_an_error_answer_is_described_by_status_reason_and_error(legacy_api):
    legacy_api.add("GET", "/x", answer(429, ERROR_BODY, "Too Many Requests"))
    response = DataRetriever(session_query_configs={}).get("https://api.powerbi.com/x")

    with pytest.raises(requests.HTTPError) as error:
        response.raise_for_status()

    assert describe_request_error(error.value) == (
        "The API answered 429 Too Many Requests to GET https://api.powerbi.com/x: "
        "TokenExpired: Access token has expired"
    )


def test_other_errors_of_the_wrappers_are_described():
    assert "not ready yet" in describe_request_error(
        ScanNotReadyError("Scan s is not ready yet")
    )
    assert describe_request_error(ApiResponseError(ERROR_BODY)) == (
        "The API answered with an error: TokenExpired: Access token has expired"
    )
    assert "no network" in describe_request_error(
        requests.ConnectionError("no network")
    )


def test_an_error_answer_of_a_wrapper_is_still_a_value_error():
    assert issubclass(ApiResponseError, ValueError)


# -- no workspaces --------------------------------------------------------------------


def test_flattening_no_workspaces_gives_empty_lists():
    flattened = Workspaces(auth={}).flatten_workspaces([])

    assert set(flattened) == {
        "users",
        "reports",
        "dashboards",
        "datasets",
        "dataflows",
        "workbooks",
        "workspace",
    }
    assert all(v == [] for v in flattened.values())


def test_format_convert_handles_a_file_without_workspaces(tmp_path):
    source = tmp_path / "workspaces.json"
    source.write_text(json.dumps({"value": []}), encoding="utf-8")
    target = tmp_path / "workspaces.xlsx"

    result = run("workspaces", "format-convert", "-s", str(source), "-t", str(target))

    assert result.exit_code == 0, result.output
    assert target.exists()
