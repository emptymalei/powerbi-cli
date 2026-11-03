"""Exporting a report to a file: the .pbix download and the jobs for PDF, PowerPoint ..."""

import json
import re

import pytest
from core_helpers import FakeAdapter, FakeClock, make_client, make_response

from pbi_cli.core.registry import DOWNLOADS, ENDPOINTS, get_endpoint
from pbi_cli.core.reportexport import (
    ATTEMPTS,
    FORMATS,
    PAGINATED_FORMATS,
    ExportError,
    export_report,
)
from pbi_cli.errors import ApiError

GROUP, REPORT = "g-1", "r-1"
EXPORT = f"/groups/{GROUP}/reports/{REPORT}/Export"
START = f"/groups/{GROUP}/reports/{REPORT}/ExportTo"
JOB = re.compile(rf"/groups/{GROUP}/reports/{REPORT}/exports/[^/]+$")
FILE = re.compile(rf"/groups/{GROUP}/reports/{REPORT}/exports/[^/]+/file$")


def binary(content: bytes, **headers) -> object:
    response = make_response(200, headers=headers)
    response._content = content
    return response


def job(state="Succeeded", percent=100, **more):
    return make_response(
        200,
        {
            "id": "job-1",
            "status": state,
            "percentComplete": percent,
            "reportName": "Sales",
            "resourceFileExtension": ".pdf",
            **more,
        },
    )


def started(job_id="job-1"):
    return make_response(202, {"id": job_id, "status": "NotStarted"})


@pytest.fixture
def clock():
    return FakeClock()


def run(adapter, clock, fmt="pdf", **kw):
    client, _ = make_client(adapter)
    return export_report(
        client,
        GROUP,
        REPORT,
        fmt,
        sleep=clock.sleep,
        clock=clock,
        **kw,
    )


# ---------------------------------------------------------------------------
# the .pbix
# ---------------------------------------------------------------------------


def test_a_pbix_is_a_download_of_bytes(clock):
    adapter = FakeAdapter().add("GET", EXPORT, binary(b"PK\x03\x04pbix"))

    done = run(adapter, clock, "pbix")

    assert done.content == b"PK\x03\x04pbix" and done.extension == ".pbix"
    assert done.format == "pbix"
    sent = adapter.requests[0]
    assert sent.headers["Accept"] == "*/*"  # a file, not JSON
    assert "downloadType" not in sent.url


def test_a_pbix_can_keep_the_connection_to_the_dataset(clock):
    adapter = FakeAdapter().add("GET", EXPORT, binary(b"x"))

    run(adapter, clock, "pbix", live_connection=True)

    assert "downloadType=LiveConnect" in adapter.requests[0].url


def test_a_paginated_report_comes_as_the_file_the_api_names(clock):
    adapter = FakeAdapter().add(
        "GET",
        EXPORT,
        binary(
            b"<Report/>", **{"Content-Disposition": 'attachment; filename="Sales.rdl"'}
        ),
    )

    assert run(adapter, clock, "pbix").extension == ".rdl"


@pytest.mark.parametrize(
    "option",
    [
        {"pages": ["P1"]},
        {"report_filter": "T/F eq 'x'"},
        {"locale": "en-us"},
        {"include_hidden_pages": True},
        {"parameters": {"a": "b"}},
    ],
)
def test_a_pbix_is_the_whole_report(clock, option):
    adapter = FakeAdapter()

    with pytest.raises(ExportError, match="the whole report"):
        run(adapter, clock, "pbix", **option)
    assert adapter.requests == []


# ---------------------------------------------------------------------------
# the jobs
# ---------------------------------------------------------------------------


def test_a_pdf_is_started_waited_for_and_fetched(clock):
    adapter = (
        FakeAdapter()
        .add("POST", START, started())
        .add(
            "GET",
            JOB,
            make_response(
                200,
                {"id": "job-1", "status": "Running", "percentComplete": 40},
                headers={"Retry-After": "2"},
            ),
            job(),
        )
        .add("GET", FILE, binary(b"%PDF-1.7"))
    )
    seen = []

    done = run(adapter, clock, "pdf", on_progress=lambda s, p: seen.append((s, p)))

    assert done.content == b"%PDF-1.7"
    assert (done.extension, done.name, done.format) == (".pdf", "Sales", "pdf")
    assert seen == [("Running", 40), ("Succeeded", 100)]
    assert clock.slept == [2.0]  # as long as Power BI said
    assert json.loads(adapter.requests[0].body) == {"format": "PDF"}
    assert adapter.requests[-1].url.endswith("/exports/job-1/file")


def test_the_polling_goes_on_at_the_pace_of_the_api_and_not_faster_than_it_asks(clock):
    adapter = (
        FakeAdapter()
        .add("POST", START, started())
        .add(
            "GET",
            JOB,
            make_response(200, {"id": "job-1", "status": "Running"}),  # no Retry-After
            make_response(
                200,
                {"id": "job-1", "status": "Running"},
                headers={"Retry-After": "999"},
            ),
            job(),
        )
        .add("GET", FILE, binary(b"x"))
    )

    run(adapter, clock)

    assert clock.slept == [5.0, 30.0]  # the usual pause, and at most 30 seconds


def test_pages_filter_locale_and_hidden_pages_go_into_the_request(clock):
    adapter = (
        FakeAdapter()
        .add("POST", START, started())
        .add("GET", JOB, job())
        .add("GET", FILE, binary(b"x"))
    )

    run(
        adapter,
        clock,
        "pptx",
        pages=["ReportSection2", "ReportSection1"],
        report_filter="Store/Territory eq 'NC'",
        locale="de-de",
        include_hidden_pages=True,
    )

    assert json.loads(adapter.requests[0].body) == {
        "format": "PPTX",
        "powerBIReportConfiguration": {
            "pages": [{"pageName": "ReportSection2"}, {"pageName": "ReportSection1"}],
            "reportLevelFilters": [{"filter": "Store/Territory eq 'NC'"}],
            "settings": {"locale": "de-de", "includeHiddenPages": True},
        },
    }


def test_a_paginated_report_takes_its_parameters(clock):
    adapter = (
        FakeAdapter()
        .add("POST", START, started())
        .add("GET", JOB, job(resourceFileExtension=".xlsx"))
        .add("GET", FILE, binary(b"x"))
    )

    done = run(adapter, clock, "xlsx", parameters={"Year": "2026"}, locale="en-us")

    assert done.extension == ".xlsx"
    assert json.loads(adapter.requests[0].body) == {
        "format": "XLSX",
        "paginatedReportConfiguration": {
            "parameterValues": [{"name": "Year", "value": "2026"}],
            "locale": "en-us",
        },
    }


def test_options_for_the_other_kind_of_report_are_refused_before_anything_is_sent(
    clock,
):
    adapter = FakeAdapter()

    with pytest.raises(ExportError, match="only be used for a Power BI report"):
        run(adapter, clock, "xlsx", pages=["P1"])
    with pytest.raises(ExportError, match="only be used for a paginated report"):
        run(adapter, clock, "pdf", parameters={"a": "b"})
    with pytest.raises(ExportError, match="--live-connection is for a .pbix"):
        run(adapter, clock, "pdf", live_connection=True)
    assert adapter.requests == []


def test_a_format_that_does_not_exist_is_refused(clock):
    with pytest.raises(ExportError, match="Cannot export to 'gif'"):
        run(FakeAdapter(), clock, "gif")
    assert set(PAGINATED_FORMATS) <= set(FORMATS) and "pbix" in FORMATS


def test_a_job_that_failed_says_what_usually_goes_wrong(clock):
    adapter = (
        FakeAdapter().add("POST", START, started()).add("GET", JOB, job("Failed", 0))
    )

    with pytest.raises(ExportError) as error:
        run(adapter, clock)

    assert "Premium, Embedded or Fabric" in str(error.value)
    assert "Premium Per User" in str(error.value) and "job-1" in str(error.value)
    assert len(adapter.calls("POST")) == 1  # no Retry-After: it failed for good


def test_a_busy_service_is_given_another_try_after_the_pause_it_asks_for(clock):
    busy = make_response(
        200, {"id": "job-1", "status": "Failed"}, headers={"Retry-After": "7"}
    )
    adapter = (
        FakeAdapter()
        .add("POST", START, started("job-1"), started("job-2"))
        .add("GET", JOB, busy, job())
        .add("GET", FILE, binary(b"x"))
    )

    done = run(adapter, clock)

    assert done.content == b"x"
    assert clock.slept == [7.0] and len(adapter.calls("POST")) == 2


def test_a_service_that_stays_busy_is_given_up_on(clock):
    busy = make_response(
        200, {"id": "job-1", "status": "Failed"}, headers={"Retry-After": "1"}
    )
    adapter = FakeAdapter().add("POST", START, started()).add("GET", JOB, busy)

    with pytest.raises(ExportError, match="could not export"):
        run(adapter, clock)

    assert len(adapter.calls("POST")) == ATTEMPTS


def test_a_job_that_takes_too_long_is_named_so_it_can_be_looked_at(clock):
    running = make_response(
        200, {"id": "job-1", "status": "Running", "percentComplete": 20}
    )
    adapter = FakeAdapter().add("POST", START, started()).add("GET", JOB, running)

    with pytest.raises(ExportError) as error:
        run(adapter, clock, timeout=60)

    assert "longer than 60 seconds" in str(error.value)
    assert "job-1" in str(error.value) and "20% done" in str(error.value)


def test_a_start_without_a_job_id_is_an_error(clock):
    adapter = FakeAdapter().add("POST", START, make_response(202, {}))

    with pytest.raises(ExportError, match="did not say which export job"):
        run(adapter, clock)


def test_a_refused_start_is_the_error_of_the_api(clock):
    adapter = FakeAdapter().add(
        "POST",
        START,
        make_response(403, {"error": {"code": "Forbidden", "message": "no capacity"}}),
    )

    with pytest.raises(ApiError) as error:
        run(adapter, clock)

    assert "403" in str(error.value) and "Premium, Embedded or Fabric" in str(
        error.value
    )


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------


def test_downloads_are_known_to_the_client_but_are_not_an_allow_listed_read():
    assert {e.id for e in DOWNLOADS} == {
        "user.report_pbix",
        "user.report_export.start",
        "user.report_export.status",
        "user.report_export.file",
    }
    assert not {e.id for e in DOWNLOADS} & {e.id for e in ENDPOINTS}
    assert get_endpoint("user.report_pbix").binary
    assert get_endpoint("user.report_export.file").binary
    assert not get_endpoint("user.report_export.status").binary
