"""Export a report to a file: a ``.pbix`` for Power BI Desktop, or a PDF, a PowerPoint
presentation, images and the like.

A ``.pbix`` is a plain download. The other formats are made by Power BI as a job: this
module starts it, waits for it (it reports how far it has come), and fetches the file. The
file is returned, never stored in the lake.

```python
from pbi_cli.core.reportexport import export_report

done = export_report(client, group_id, report_id, "pdf", pages=["ReportSection1"])
Path("sales" + done.extension).write_bytes(done.content)
```

What Power BI asks for: the formats other than ``pbix`` need the report in a workspace on a
Premium, Embedded or Fabric capacity (not Premium Per User); ``png`` is off in the tenant
until an administrator allows it; a report may hold at most 50 pages and visuals in one export
and its file at most 250 MB. Nothing here imports a CLI framework.
"""

import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from pbi_cli.core.client import PowerBIClient
from pbi_cli.errors import PBIError

#: A report as a file for Power BI Desktop (``.rdl`` for a paginated report).
PBIX = "pbix"
#: What a Power BI report can be exported to, besides `PBIX`.
POWER_BI_FORMATS = ("pdf", "pptx", "png")
#: What a paginated report can be exported to, besides `PBIX`, ``pdf`` and ``pptx``.
PAGINATED_FORMATS = ("xlsx", "docx", "csv", "xml", "mhtml", "accessiblepdf")
#: Every format that can be asked for.
FORMATS = (PBIX,) + POWER_BI_FORMATS + PAGINATED_FORMATS

#: How often a job that Power BI says to try again is started anew.
ATTEMPTS = 3
#: Seconds between two looks at a job when Power BI does not say, and the most it is waited.
POLL = 5.0
LONGEST_POLL = 30.0

_FILE_NAME = re.compile(r"""filename\*?=(?:UTF-8'')?"?([^";]+)"?""", re.IGNORECASE)


class ExportError(PBIError):
    """An export that did not produce a file."""


@dataclass(frozen=True)
class ExportedReport:
    """A report as a file.

    :param content: the bytes of the file
    :param extension: its ending, with the dot (``.pdf``; ``.zip`` for the pages of a
        report as images)
    :param name: the name of the report when Power BI said it, else empty
    :param format: the format that was asked for
    """

    content: bytes
    extension: str
    name: str
    format: str


def _header(headers: Mapping[str, str], name: str) -> Optional[str]:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _seconds(headers: Mapping[str, str]) -> Optional[float]:
    value = _header(headers, "Retry-After")
    try:
        return None if value is None else max(0.0, float(value))
    except ValueError:
        return None


def _file_extension(headers: Mapping[str, str], fallback: str) -> str:
    """The ending of a downloaded file, from its ``Content-Disposition`` when it says."""
    found = _FILE_NAME.search(_header(headers, "Content-Disposition") or "")
    if found and "." in found.group(1):
        return "." + found.group(1).rsplit(".", 1)[1].strip().lower()
    return fallback


def _request_body(
    fmt: str,
    pages: Sequence[str],
    report_filter: Optional[str],
    locale: Optional[str],
    include_hidden_pages: bool,
    parameters: Optional[Mapping[str, str]],
) -> Dict[str, Any]:
    """The body that starts an export to ``fmt``.

    :raises ExportError: for an option that the format cannot use
    """
    paginated = fmt in PAGINATED_FORMATS
    power_bi_only = {
        "pages": bool(pages),
        "a filter": bool(report_filter),
        "hidden pages": include_hidden_pages,
    }
    if paginated:
        used = [name for name, given in power_bi_only.items() if given]
        if used:
            raise ExportError(
                f"{', '.join(used)} can only be used for a Power BI report, not for "
                f"the paginated formats ({', '.join(PAGINATED_FORMATS)})."
            )
    elif parameters:
        raise ExportError(
            "Report parameters can only be used for a paginated report "
            f"({', '.join(PAGINATED_FORMATS)})."
        )

    body: Dict[str, Any] = {"format": fmt.upper()}
    if paginated:
        config: Dict[str, Any] = {}
        if parameters:
            config["parameterValues"] = [
                {"name": name, "value": value} for name, value in parameters.items()
            ]
        if locale:
            config["locale"] = locale
        if config:
            body["paginatedReportConfiguration"] = config
        return body
    config = {}
    if pages:
        config["pages"] = [{"pageName": name} for name in pages]
    if report_filter:
        config["reportLevelFilters"] = [{"filter": report_filter}]
    settings: Dict[str, Any] = {}
    if locale:
        settings["locale"] = locale
    if include_hidden_pages:
        settings["includeHiddenPages"] = True
    if settings:
        config["settings"] = settings
    if config:
        body["powerBIReportConfiguration"] = config
    return body


def _why_it_failed(status: Mapping[str, Any]) -> str:
    return (
        "Power BI could not export the report. Usual causes: the workspace is not on a "
        "Premium, Embedded or Fabric capacity (Premium Per User does not count); png is "
        "not allowed in the tenant settings; the report holds a visual that cannot be "
        "exported (R, Python or custom visuals); it has more than 50 pages or visuals, or "
        "the file would be over 250 MB"
        + (f" (job {status['id']})." if status.get("id") else ".")
    )


def export_report(
    client: PowerBIClient,
    group_id: str,
    report_id: str,
    fmt: str = PBIX,
    *,
    pages: Sequence[str] = (),
    report_filter: Optional[str] = None,
    locale: Optional[str] = None,
    include_hidden_pages: bool = False,
    parameters: Optional[Mapping[str, str]] = None,
    live_connection: bool = False,
    timeout: float = 600.0,
    on_progress: Optional[Callable[[str, int], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ExportedReport:
    """Export a report to a file.

    :param client: the client of a user account (the account needs to see the report)
    :param group_id: the workspace
    :param report_id: the report
    :param fmt: one of `FORMATS`
    :param pages: only these pages, by name (see ``pbi reports pages``), in this order
    :param report_filter: a filter in the syntax of the URL of a report, such as
        ``Store/Territory eq 'NC'`` (a Power BI report)
    :param locale: how numbers and dates are written, such as ``en-us``
    :param include_hidden_pages: also the pages that are hidden (when all are exported)
    :param parameters: values of the parameters of a paginated report
    :param live_connection: for ``pbix``: keep the connection to the dataset, and do not
        include the model
    :param timeout: seconds to wait for the job before giving up
    :param on_progress: called with the state of the job and its percentage
    :param sleep: waits for some seconds (replaced in tests)
    :param clock: the time in seconds (replaced in tests)
    :raises ExportError: for a format or option that does not fit, a job that failed or
        took too long
    """
    if fmt not in FORMATS:
        raise ExportError(f"Cannot export to '{fmt}'. Formats: {', '.join(FORMATS)}.")
    ids = {"groupId": group_id, "reportId": report_id}

    if fmt == PBIX:
        if pages or report_filter or locale or include_hidden_pages or parameters:
            raise ExportError(
                "A .pbix is the whole report: pages, filters, locale, hidden pages and "
                "parameters are for the other formats."
            )
        query = {"downloadType": "LiveConnect"} if live_connection else {}
        file = client.request("user.report_pbix", {**ids, **query})
        return ExportedReport(
            file.data, _file_extension(file.headers, ".pbix"), "", PBIX
        )
    if live_connection:
        raise ExportError("--live-connection is for a .pbix only.")

    body = _request_body(
        fmt, pages, report_filter, locale, include_hidden_pages, parameters
    )
    deadline = clock() + timeout
    for attempt in range(1, ATTEMPTS + 1):
        started = client.request("user.report_export.start", ids, body=body)
        job = started.data or {}
        export_id = job.get("id")
        if not export_id:
            raise ExportError("Power BI did not say which export job it started.")
        job_ids = {**ids, "exportId": export_id}
        while True:
            answer = client.request("user.report_export.status", job_ids)
            status = answer.data or {}
            state = str(status.get("status") or "Undefined")
            if on_progress is not None:
                on_progress(state, int(status.get("percentComplete") or 0))
            if state in ("Succeeded", "Failed"):
                break
            if clock() >= deadline:
                raise ExportError(
                    f"The export took longer than {timeout:.0f} seconds (job "
                    f"{export_id}, {status.get('percentComplete') or 0}% done). Power "
                    "BI may still finish it: try a longer --timeout, or fewer pages."
                )
            wait = _seconds(answer.headers)
            sleep(min(wait if wait else POLL, LONGEST_POLL))
        if state == "Succeeded":
            file = client.request("user.report_export.file", job_ids)
            extension = str(status.get("resourceFileExtension") or f".{fmt}")
            return ExportedReport(
                file.data,
                extension if extension.startswith(".") else f".{extension}",
                str(status.get("reportName") or ""),
                fmt,
            )
        busy = _seconds(answer.headers)
        if busy is None or attempt == ATTEMPTS:
            raise ExportError(_why_it_failed(status))
        sleep(min(busy, LONGEST_POLL))  # Power BI is busy: start the whole job again
    raise ExportError(_why_it_failed({}))  # pragma: no cover
