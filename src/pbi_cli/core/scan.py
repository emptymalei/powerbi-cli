"""Metadata scans of workspaces: the ``getInfo`` job, its status and its result.

A scan is asynchronous. ``getInfo`` accepts 1 to 100 workspace ids and answers with a scan
id; the status of the scan is polled until it has succeeded; then the result can be
fetched, for the next 24 hours. One scan of 100 workspaces costs three or four requests,
where scanning them one by one costs hundreds, so callers should always fill a scan:

```python
for batch in chunked(workspace_ids):
    run = run_scan(client, batch, ScanFlags(lineage=True))
    for workspace_id, result in split_scan_result(run.result).items():
        ...
```

`run_scan` can resume a scan that was started earlier (``resume=``): the scan id is reported
through ``on_started`` as soon as the API has accepted the scan, so a caller that keeps it
can pick the scan up again after a crash or an expired token instead of starting over.
"""

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, TypeVar

from pbi_cli.core.client import PowerBIClient, body_hash
from pbi_cli.core.ratelimit import DEFAULT_MAX_WAIT, MaxWait
from pbi_cli.core.store import LakeStore, Snapshot
from pbi_cli.errors import ApiError, ScanError, ScanTimeout

#: Workspace ids per ``getInfo`` call.
MAX_WORKSPACES = 100

#: How long the API keeps the result of a scan.
RESULT_KEPT = timedelta(hours=24)

#: The lake endpoint that holds the results of scans.
RESULT_ENDPOINT = "admin.scan.result"

T = TypeVar("T")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def chunked(items: Sequence[T], size: int = MAX_WORKSPACES) -> List[List[T]]:
    """Split a list in pieces of at most ``size`` items, keeping the order."""
    if size < 1:
        raise ValueError("size must be at least 1")
    return [list(items[start : start + size]) for start in range(0, len(items), size)]


@dataclass(frozen=True)
class ScanFlags:
    """What a scan includes besides the structure of the workspaces.

    :param lineage: upstream dataflows, tiles and data source ids
    :param datasource_details: connection details of the data sources
    :param dataset_schema: tables, columns and measures (needs the tenant setting for
        detailed metadata)
    :param dataset_expressions: DAX and Power Query expressions (same tenant setting)
    :param get_artifact_users: the users of reports, dashboards and other items
    """

    lineage: bool = False
    datasource_details: bool = False
    dataset_schema: bool = False
    dataset_expressions: bool = False
    get_artifact_users: bool = False

    def query(self) -> Dict[str, bool]:
        """The query parameters of ``getInfo``."""
        return {
            "lineage": self.lineage,
            "datasourceDetails": self.datasource_details,
            "datasetSchema": self.dataset_schema,
            "datasetExpressions": self.dataset_expressions,
            "getArtifactUsers": self.get_artifact_users,
        }

    def canonical(self) -> Dict[str, str]:
        """The flags as they identify a scan in the lake: ``"true"`` or ``"false"``."""
        return {name: str(value).lower() for name, value in self.query().items()}

    def describe(self) -> str:
        """The flags that are on, in words (``none`` for a plain scan)."""
        return (
            ", ".join(name for name, value in self.query().items() if value) or "none"
        )

    @classmethod
    def from_query(cls, query: Mapping[str, Any]) -> "ScanFlags":
        """The inverse of `query` (and of `canonical`)."""

        def on(name: str) -> bool:
            return str(query.get(name, False)).lower() == "true"

        return cls(
            lineage=on("lineage"),
            datasource_details=on("datasourceDetails"),
            dataset_schema=on("datasetSchema"),
            dataset_expressions=on("datasetExpressions"),
            get_artifact_users=on("getArtifactUsers"),
        )


@dataclass(frozen=True)
class ScanJob:
    """A scan the API has accepted.

    :param scan_id: the id to poll and to fetch the result with
    :param started_at: when the API accepted the scan (aware, UTC)
    """

    scan_id: str
    started_at: datetime

    def expired(self, now: Optional[datetime] = None) -> bool:
        """Whether the API has dropped the result by now (it keeps it for 24 hours)."""
        return (now or _utcnow()) - self.started_at >= RESULT_KEPT


@dataclass
class ScanRun:
    """A finished scan.

    :param scan_id: the id of the scan
    :param started_at: when the API accepted the scan
    :param finished_at: when the result arrived
    :param result: what ``scanResult`` returned: ``workspaces``, ``datasourceInstances`` ...
    :param polls: how many status checks it took
    :param resumed: whether the scan was started by an earlier call
    """

    scan_id: str
    started_at: datetime
    finished_at: datetime
    result: Dict[str, Any]
    polls: int
    resumed: bool = False


def _check_body(data: Any, scan_id: Optional[str]) -> Dict[str, Any]:
    """The API may answer ``200``/``202`` and still carry an ``error`` in the body."""
    body = data if isinstance(data, dict) else {}
    if body.get("error"):
        raise ScanError(f"The scan was rejected or failed: {body['error']}", scan_id)
    return body


def run_scan(
    client: PowerBIClient,
    workspace_ids: Sequence[str],
    flags: ScanFlags = ScanFlags(),
    *,
    interval: float = 5.0,
    timeout: float = 300.0,
    resume: Optional[ScanJob] = None,
    on_started: Optional[Callable[[ScanJob], None]] = None,
    on_poll: Optional[Callable[[int, str, float], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    clock: Callable[[], datetime] = _utcnow,
    max_wait: MaxWait = DEFAULT_MAX_WAIT,
) -> ScanRun:
    """Scan up to 100 workspaces: start the scan, wait for it, fetch the result.

    :param client: signs in as an administrator
    :param workspace_ids: 1 to 100 workspace ids
    :param flags: what to include in the scan
    :param interval: seconds between two status checks
    :param timeout: seconds to wait for the scan to succeed
    :param resume: a scan that was started earlier for these workspaces: wait for it
        instead of starting another (when the API no longer knows it, a new one is started)
    :param on_started: called with the `ScanJob` as soon as the API accepted the scan
    :param on_poll: called with the number of the check, the status and the seconds that
        will be waited, before each wait
    :param sleep: waits for some seconds (replaced in tests)
    :param monotonic: a clock for the timeout (replaced in tests)
    :param clock: the current time, aware UTC (replaced in tests)
    :param max_wait: how long requests may wait for quota
    :raises ScanTimeout: if the scan does not finish in time (it may still succeed)
    :raises ScanError: if the scan fails or is rejected
    :raises TokenExpiredError: if the token expired (the scan keeps running: ``on_started``
        has reported its id)
    :raises ApiError: for any other failed request
    """
    ids = list(workspace_ids)
    if not 1 <= len(ids) <= MAX_WORKSPACES:
        raise ValueError(
            f"a scan takes 1 to {MAX_WORKSPACES} workspaces, got {len(ids)}"
        )

    def start() -> ScanJob:
        response = client.request(
            "admin.scan.start",
            flags.query(),
            body={"workspaces": ids},
            max_wait=max_wait,
        )
        body = _check_body(response.data, None)
        scan_id = body.get("id")
        if not scan_id:
            raise ScanError(f"Unexpected answer to the scan request: {body}")
        job = ScanJob(scan_id=str(scan_id), started_at=clock())
        if on_started is not None:
            on_started(job)
        return job

    def wait_and_fetch(job: ScanJob, resumed: bool) -> ScanRun:
        deadline = monotonic() + timeout
        polls = 0
        while True:
            polls += 1
            response = client.request(
                "admin.scan.status", {"scanId": job.scan_id}, max_wait=max_wait
            )
            body = _check_body(response.data, job.scan_id)
            status = str(body.get("status"))
            if status == "Failed":
                raise ScanError(f"Scan {job.scan_id} failed.", job.scan_id)
            if status == "Succeeded":
                fetched = client.request(
                    "admin.scan.result", {"scanId": job.scan_id}, max_wait=max_wait
                )
                if fetched.status != 202 and isinstance(fetched.data, dict):
                    return ScanRun(
                        scan_id=job.scan_id,
                        started_at=job.started_at,
                        finished_at=clock(),
                        result=fetched.data,
                        polls=polls,
                        resumed=resumed,
                    )
                status = "Succeeded, result not ready"  # 202: ask again shortly
            if monotonic() >= deadline:
                raise ScanTimeout(
                    f"Scan {job.scan_id} did not complete within {timeout}s "
                    f"(last status: {status}).",
                    job.scan_id,
                )
            wait = min(interval, max(deadline - monotonic(), 0.0))
            if on_poll is not None:
                on_poll(polls, status, wait)
            sleep(wait)

    if resume is None:
        return wait_and_fetch(start(), resumed=False)
    try:
        return wait_and_fetch(resume, resumed=True)
    except ApiError as error:
        if error.status != 404:
            raise
        # the API no longer knows the scan (its result is kept for 24 hours): start anew
        return wait_and_fetch(start(), resumed=False)


# -- results ---------------------------------------------------------------------------


def _used_instance_ids(node: Any, found: Set[str]) -> None:
    """Collect the data source instances that an item of a scan result uses."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in (
                "datasourceUsages",
                "misconfiguredDatasourceUsages",
            ) and isinstance(value, list):
                found.update(
                    str(usage["datasourceInstanceId"])
                    for usage in value
                    if isinstance(usage, dict) and usage.get("datasourceInstanceId")
                )
            else:
                _used_instance_ids(value, found)
    elif isinstance(node, list):
        for item in node:
            _used_instance_ids(item, found)


def split_scan_result(result: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Cut the result of a scan into one result per workspace.

    Each piece looks like the result of a scan of that workspace alone: its workspace, and
    the data source instances that the workspace uses (found through the
    ``datasourceUsages`` of its datasets, dataflows and other items). A data source
    instance that cannot be attributed, because it carries no ``datasourceId``, is
    repeated in every piece so that nothing is lost. Other top-level fields are kept.

    :return: the pieces by workspace id, in the order of the result
    """
    instances = list(result.get("datasourceInstances") or [])
    misconfigured = list(result.get("misconfiguredDatasourceInstances") or [])
    extra = {
        key: value
        for key, value in result.items()
        if key
        not in ("workspaces", "datasourceInstances", "misconfiguredDatasourceInstances")
    }

    def belonging(
        candidates: List[Dict[str, Any]], used: Set[str]
    ) -> List[Dict[str, Any]]:
        return [
            item
            for item in candidates
            if not item.get("datasourceId") or str(item["datasourceId"]) in used
        ]

    pieces: Dict[str, Dict[str, Any]] = {}
    for workspace in result.get("workspaces") or []:
        used: Set[str] = set()
        _used_instance_ids(workspace, used)
        pieces[str(workspace.get("id"))] = {
            **extra,
            "workspaces": [workspace],
            "datasourceInstances": belonging(instances, used),
            "misconfiguredDatasourceInstances": belonging(misconfigured, used),
        }
    return pieces


# -- the lake --------------------------------------------------------------------------


def batch_key(workspace_ids: Sequence[str]) -> str:
    """A short identifier of a set of workspaces, whatever the order they were given in."""
    digest = hashlib.sha256("\n".join(sorted(workspace_ids)).encode("utf-8"))
    return digest.hexdigest()[:12]


def scan_params(workspace_ids: Sequence[str], flags: ScanFlags) -> Dict[str, str]:
    """The parameters that identify a scan in the lake: the workspaces and the flags."""
    return {"batch": batch_key(workspace_ids), **flags.canonical()}


def latest_scan(
    store: LakeStore,
    tenant: str,
    workspace_ids: Sequence[str],
    flags: ScanFlags,
) -> Optional[Snapshot]:
    """The newest stored scan of exactly these workspaces with these flags."""
    return store.latest(tenant, RESULT_ENDPOINT, scan_params(workspace_ids, flags))


def store_scan(
    store: LakeStore,
    tenant: str,
    run: ScanRun,
    workspace_ids: Sequence[str],
    flags: ScanFlags,
    *,
    profile: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Snapshot:
    """Keep the result of a scan in the lake, as a job snapshot.

    The manifest lists the workspaces and the flags, and the id of the scan.
    """
    ids = sorted(workspace_ids)
    return store.write_snapshot(
        tenant,
        RESULT_ENDPOINT,
        scan_params(ids, flags),
        run.result,
        kind="job",
        profile=profile,
        rows=len(run.result.get("workspaces") or []),
        fetched_at=run.finished_at,
        request={
            "method": "POST",
            "path": "/admin/workspaces/getInfo",
            "params": flags.canonical(),
            "body_sha256": body_hash({"workspaces": ids}),
        },
        extra={
            "scan_id": run.scan_id,
            "started_at": run.started_at.isoformat(),
            "polls": run.polls,
            "workspace_ids": ids,
            "flags": flags.canonical(),
            **dict(extra or {}),
        },
    )
