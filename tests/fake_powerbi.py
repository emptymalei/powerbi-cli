"""A fake Power BI service for the tests: the APIs that pbi-cli reads, in memory.

It is a ``requests`` transport (like ``core_helpers.FakeAdapter``) that answers like the
real service for the operations in ``pbi_cli.core.registry``, and it enforces the rules
of the documentation so that the code under test meets them too:

- ``GET admin/groups`` and ``admin/apps`` need ``$top``; ``$skip`` pages through the list;
- ``getInfo`` takes 1 to 100 workspace ids and answers ``202``; a scan is ``Running`` for
  a configurable number of status checks and then ``Succeeded`` (or ``Failed``); the result
  is ``202`` until it is ready and is gone after 24 hours;
- ``modifiedSince`` must be between 30 minutes and 30 days old;
- activity events need ``startDateTime`` and ``endDateTime`` in single quotes, in the same
  UTC day and within the last 28 days, and come in pages that are chained by
  ``continuationUri``;
- what a user can see depends on who asks, by the ``oid`` of the token: `visible_to` maps an
  ``oid`` to the workspaces of that account (other tokens see ``user_workspace_ids``), and
  with `require_admin` the ``admin`` operations answer ``403`` to a token without the claim
  ``admin``.

Faults can be injected (`FakePowerBI.fail`, `throttle`, `expire_token_after`), every call
is recorded, and the service can be shared by threads.
"""

import json
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Pattern, Tuple
from urllib.parse import parse_qs, urlsplit

import requests
from core_helpers import make_response
from requests.adapters import BaseAdapter

from pbi_cli.core.jwt import decode_claims

UTC = timezone.utc
PREFIX = "/v1.0/myorg"

Handler = Callable[..., Any]


@dataclass
class Call:
    """One request the service received."""

    method: str
    path: str  # relative to /v1.0/myorg
    query: Dict[str, str]
    body: Any


@dataclass
class Fault:
    method: str
    pattern: Pattern[str]
    status: int
    body: Any
    headers: Dict[str, str]
    times: Optional[int]  # None: every time
    query: Dict[str, Pattern[str]]  # the request must carry these, matching


def _error(status: int, code: str, message: str) -> Tuple[int, Any]:
    return status, {"error": {"code": code, "message": message}}


class FakePowerBI(BaseAdapter):
    """The service. Build one, then pass ``fake.session()`` to the client under test.

    :param clock: returns the current time (aware, UTC); the service's own idea of "now"
    :param workspaces: how many workspaces the tenant has (``ws-0001``, ``ws-0002``, ...)
    :param reports: how many reports (``rep-0001`` ..., spread over the workspaces)
    :param datasets: how many datasets (``ds-0001`` ...)
    :param event_days: how many days back (today included) have activity events
    :param events_per_day: how many events each of those days has
    :param events_page_size: events per page of ``activityevents``
    :param scan_polls: status checks that answer ``Running`` before ``Succeeded``
    """

    def __init__(
        self,
        *,
        clock: Optional[Callable[[], datetime]] = None,
        workspaces: int = 12,
        reports: int = 6,
        datasets: int = 4,
        event_days: int = 3,
        events_per_day: int = 5,
        events_page_size: int = 2,
        scan_polls: int = 0,
    ) -> None:
        super().__init__()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self.calls: List[Call] = []
        self.scan_polls = scan_polls
        self.events_page_size = events_page_size
        self.result_not_ready_times = 0
        self.empty_first_events_page = False
        self.link_after_last_events_page = False
        self.failing_scan_workspaces: set = set()

        now = self.now()
        self.workspaces = [
            {
                "id": f"ws-{i:04d}",
                "name": f"Workspace {i}",
                "type": "Workspace",
                "state": "Active",
                "isReadOnly": False,
                "isOnDedicatedCapacity": False,
                "modified_at": now - timedelta(days=10, hours=i),
            }
            for i in range(1, workspaces + 1)
        ]
        self.reports = [
            {
                "id": f"rep-{i:04d}",
                "name": f"Report {i}",
                "datasetId": f"ds-{(i - 1) % max(datasets, 1) + 1:04d}",
                "workspaceId": self.workspaces[(i - 1) % len(self.workspaces)]["id"],
                "webUrl": f"https://app.example/reports/rep-{i:04d}",
            }
            for i in range(1, reports + 1)
            if self.workspaces
        ]
        self.datasets = [
            {
                "id": f"ds-{i:04d}",
                "name": f"Dataset {i}",
                "workspaceId": self.workspaces[(i - 1) % len(self.workspaces)]["id"],
                "configuredBy": "owner@example.com",
            }
            for i in range(1, datasets + 1)
            if self.workspaces
        ]
        first = self.workspaces[0]["id"] if self.workspaces else None
        self.dashboards = [
            {"id": "dash-0001", "displayName": "Dashboard 1", "workspaceId": first}
        ]
        self.dataflows = [
            {"objectId": "flow-0001", "name": "Dataflow 1", "workspaceId": first}
        ]
        self.capacities = [{"id": "cap-0001", "displayName": "Capacity 1", "sku": "P1"}]
        self.apps = [
            {
                "id": f"app-{i:04d}",
                "name": f"App {i}",
                "description": "",
                "workspaceId": self.workspaces[(i - 1) % len(self.workspaces)]["id"],
            }
            for i in range(1, 4)
            if self.workspaces
        ]
        self.user_workspace_ids: List[str] = [str(w["id"]) for w in self.workspaces[:3]]
        self.visible_to: Dict[str, List[str]] = {}
        self.require_admin = False
        #: datasets on which an account that is not an administrator lacks the permission that
        #: reading their users (Reshare), data sources and refresh history (Write) needs
        self.restricted_datasets: set = set()
        self._claims: Dict[str, Any] = {}
        self._events: Dict[date, List[Dict[str, Any]]] = {}
        for back in range(event_days):
            self.add_events(now.date() - timedelta(days=back), events_per_day)
        self._scans: Dict[str, Dict[str, Any]] = {}
        self._scan_counter = 0
        self.in_flight_scans = 0
        self.max_in_flight_scans = 0
        self._faults: List[Fault] = []
        self._expire_after: Optional[int] = None
        self._routes: List[Tuple[str, Pattern[str], Handler]] = [
            ("GET", re.compile(r"^/admin/groups$"), self._groups),
            ("GET", re.compile(r"^/admin/apps$"), self._admin_apps),
            ("GET", re.compile(r"^/admin/capacities$"), self._list("capacities")),
            ("GET", re.compile(r"^/admin/reports$"), self._list("reports")),
            ("GET", re.compile(r"^/admin/datasets$"), self._list("datasets")),
            ("GET", re.compile(r"^/admin/dashboards$"), self._list("dashboards")),
            ("GET", re.compile(r"^/admin/dataflows$"), self._list("dataflows")),
            ("GET", re.compile(r"^/admin/reports/(?P<id>[^/]+)/users$"), self._users),
            (
                "GET",
                re.compile(r"^/admin/datasets/(?P<id>[^/]+)/datasources$"),
                self._datasources,
            ),
            (
                "GET",
                re.compile(r"^/admin/groups/(?P<id>[^/]+)/users$"),
                self._admin_users("workspaces", "id", "groupUserAccessRight"),
            ),
            (
                "GET",
                re.compile(r"^/admin/datasets/(?P<id>[^/]+)/users$"),
                self._admin_users("datasets", "id", "datasetUserAccessRight"),
            ),
            (
                "GET",
                re.compile(r"^/admin/dashboards/(?P<id>[^/]+)/users$"),
                self._admin_users("dashboards", "id", "dashboardUserAccessRight"),
            ),
            (
                "GET",
                re.compile(r"^/admin/dataflows/(?P<id>[^/]+)/users$"),
                self._admin_users("dataflows", "objectId", "dataflowUserAccessRight"),
            ),
            (
                "GET",
                re.compile(r"^/admin/dataflows/(?P<id>[^/]+)/datasources$"),
                self._sources_of("dataflows", "objectId", admin=True),
            ),
            (
                "GET",
                re.compile(r"^/admin/capacities/refreshables$"),
                self._refreshables,
            ),
            ("GET", re.compile(r"^/admin/workspaces/modified$"), self._modified),
            ("POST", re.compile(r"^/admin/workspaces/getInfo$"), self._get_info),
            (
                "GET",
                re.compile(r"^/admin/workspaces/scanStatus/(?P<id>[^/]+)$"),
                self._scan_status,
            ),
            (
                "GET",
                re.compile(r"^/admin/workspaces/scanResult/(?P<id>[^/]+)$"),
                self._scan_result,
            ),
            ("GET", re.compile(r"^/admin/activityevents$"), self._activity),
            ("GET", re.compile(r"^/groups$"), self._user_groups),
            ("GET", re.compile(r"^/apps$"), self._user_apps),
            (
                "GET",
                re.compile(r"^/groups/(?P<group>[^/]+)/reports$"),
                self._group_reports,
            ),
            (
                "GET",
                re.compile(r"^/groups/(?P<group>[^/]+)/datasets$"),
                self._group_list("datasets", ("id", "name", "configuredBy")),
            ),
            (
                "GET",
                re.compile(r"^/groups/(?P<group>[^/]+)/dashboards$"),
                self._group_list("dashboards", ("id", "displayName")),
            ),
            (
                "GET",
                re.compile(r"^/groups/(?P<group>[^/]+)/dataflows$"),
                self._group_list("dataflows", ("objectId", "name")),
            ),
            (
                "GET",
                re.compile(r"^/groups/(?P<group>[^/]+)/users$"),
                self._group_users,
            ),
            (
                "GET",
                re.compile(
                    r"^/groups/(?P<group>[^/]+)/reports/(?P<report>[^/]+)/pages$"
                ),
                self._pages,
            ),
            (
                "GET",
                re.compile(r"^/groups/(?P<group>[^/]+)/datasets/(?P<id>[^/]+)/users$"),
                self._dataset_users_of_a_user,
            ),
            (
                "GET",
                re.compile(
                    r"^/groups/(?P<group>[^/]+)/datasets/(?P<id>[^/]+)/datasources$"
                ),
                self._sources_of("datasets", "id", restricted=True),
            ),
            (
                "GET",
                re.compile(
                    r"^/groups/(?P<group>[^/]+)/dataflows/(?P<id>[^/]+)/datasources$"
                ),
                self._sources_of("dataflows", "objectId"),
            ),
            (
                "GET",
                re.compile(
                    r"^/groups/(?P<group>[^/]+)/datasets/(?P<id>[^/]+)/refreshes$"
                ),
                self._refreshes,
            ),
            (
                "GET",
                re.compile(
                    r"^/groups/(?P<group>[^/]+)/datasets/(?P<id>[^/]+)/parameters$"
                ),
                self._parameters,
            ),
            (
                "GET",
                re.compile(
                    r"^/groups/(?P<group>[^/]+)/dashboards/(?P<id>[^/]+)/tiles$"
                ),
                self._tiles,
            ),
        ]

    # -- the transport ---------------------------------------------------------------

    def now(self) -> datetime:
        return self._clock()

    def session(self) -> requests.Session:
        session = requests.Session()
        session.mount("https://", self)
        session.mount("http://", self)
        return session

    def close(self) -> None:
        pass

    def send(self, request, **kwargs):  # type: ignore[no-untyped-def]
        parts = urlsplit(str(request.url))
        path = parts.path
        if not path.startswith(PREFIX):
            raise AssertionError(f"FakePowerBI: unexpected URL {request.url}")
        path = path[len(PREFIX) :]
        query = {
            k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()
        }
        body = json.loads(request.body) if request.body else None
        origin = f"{parts.scheme}://{parts.netloc}"

        with self._lock:
            self.calls.append(Call(request.method, path, query, body))
            self._claims = self._claims_of(request)
            fault = self._fault_for(request.method, path, query)
            if fault is not None:
                response = make_response(
                    fault.status, fault.body, headers=fault.headers
                )
            elif (
                self._expire_after is not None and len(self.calls) > self._expire_after
            ):
                status, payload = _error(
                    401, "TokenExpired", "Access token has expired"
                )
                response = make_response(status, payload)
            else:
                response = self._route(request.method, path, query, body, origin)
        response.url = str(request.url)
        response.request = request
        return response

    @staticmethod
    def _claims_of(request: Any) -> Dict[str, Any]:
        """The claims of the bearer token of a request (it is only read, as pbi-cli does)."""
        header = str(request.headers.get("Authorization") or "")
        return decode_claims(header.partition(" ")[2].strip())

    def _visible(self) -> List[str]:
        """The workspaces of the account that asks."""
        return self.visible_to.get(
            str(self._claims.get("oid")), self.user_workspace_ids
        )

    def _fault_for(
        self, method: str, path: str, query: Dict[str, str]
    ) -> Optional[Fault]:
        for fault in self._faults:
            if (
                fault.method == method
                and fault.pattern.search(path)
                and all(
                    name in query and pattern.search(query[name])
                    for name, pattern in fault.query.items()
                )
            ):
                if fault.times is None:
                    return fault
                if fault.times > 0:
                    fault.times -= 1
                    return fault
        return None

    def _route(
        self, method: str, path: str, query: Dict[str, str], body: Any, origin: str
    ) -> requests.Response:
        if (
            self.require_admin
            and path.startswith("/admin/")
            and not self._claims.get("admin")
        ):
            return make_response(
                403,
                {
                    "error": {
                        "code": "PowerBINotAuthorizedException",
                        "message": "an administrator is needed",
                    }
                },
            )
        for route_method, pattern, handler in self._routes:
            match = pattern.match(path)
            if route_method == method and match:
                result = handler(match, query, body, origin)
                if isinstance(result, requests.Response):
                    return result
                status, payload = result
                return make_response(status, payload)
        return make_response(404, {"error": {"code": "NotFound", "message": path}})

    # -- faults and inspection -------------------------------------------------------

    def fail(
        self,
        method: str,
        path: str,
        status: int,
        *,
        body: Any = None,
        times: Optional[int] = None,
        headers: Optional[Dict[str, str]] = None,
        query: Optional[Dict[str, str]] = None,
    ) -> None:
        """Answer requests whose path matches the regex ``path`` with an error status.

        :param query: only requests whose query parameters match these regexes
            (for example ``{"continuationToken": "^'2026-09-29"}``)
        """
        if body is None:
            body = {"error": {"code": f"Error{status}", "message": "injected"}}
        self._faults.append(
            Fault(
                method,
                re.compile(path),
                status,
                body,
                headers or {},
                times,
                {name: re.compile(regex) for name, regex in (query or {}).items()},
            )
        )

    def throttle(self, path: str, *, retry_after: int = 1, times: int = 1) -> None:
        """Answer ``429`` with ``Retry-After`` for the next ``times`` requests to ``path``."""
        self.fail(
            "GET",
            path,
            429,
            times=times,
            headers={"Retry-After": str(retry_after)},
        )

    def expire_token_after(self, calls: Optional[int]) -> None:
        """Answer ``401`` to every request after ``calls`` requests (``None``: never)."""
        self._expire_after = calls

    def expire_token_in(self, calls: int) -> None:
        """Answer ``401`` to every request after the next ``calls`` requests."""
        self._expire_after = len(self.calls) + calls

    def clear_faults(self) -> None:
        self._faults.clear()
        self._expire_after = None

    def calls_to(self, path: str, method: Optional[str] = None) -> List[Call]:
        """The recorded requests whose path matches the regex ``path``."""
        pattern = re.compile(path)
        return [
            call
            for call in self.calls
            if pattern.search(call.path) and (method is None or call.method == method)
        ]

    def count(self, path: str, method: Optional[str] = None) -> int:
        return len(self.calls_to(path, method))

    def reset_calls(self) -> None:
        self.calls.clear()

    # -- changing the world ----------------------------------------------------------

    def add_events(self, day: date, count: int) -> None:
        """Give ``day`` ``count`` more events, spread over the day (ids keep counting)."""
        events = self._events.setdefault(day, [])
        start = len(events)
        total = start + count
        for k in range(start, total):
            at = datetime.combine(day, time.min, tzinfo=UTC) + timedelta(
                seconds=int((k + 1) * 86400 / (total + 1))
            )
            events.append(
                {
                    "Id": f"ev-{day:%Y%m%d}-{k:03d}",
                    "CreationTime": at.strftime("%Y-%m-%dT%H:%M:%S"),
                    "Operation": "ViewReport",
                    "Activity": "ViewReport",
                    "UserId": f"user{k % 3}@example.com",
                    "ClientIP": "10.0.0.1",
                }
            )

    def modify_workspace(self, workspace_id: str, at: datetime) -> None:
        for workspace in self.workspaces:
            if workspace["id"] == workspace_id:
                workspace["modified_at"] = at

    def expire_scans(self) -> None:
        """Forget the scan results, as the service does after 24 hours."""
        self._scans.clear()

    # -- handlers: lists -------------------------------------------------------------

    def _paged(self, rows: List[Dict[str, Any]], query: Dict[str, str], top_max: int):
        if "$top" not in query:
            return _error(400, "InvalidRequest", "$top is required")
        top = int(query["$top"])
        if not 1 <= top <= top_max:
            return _error(
                400, "InvalidRequest", f"$top must be between 1 and {top_max}"
            )
        skip = int(query.get("$skip", 0))
        return 200, {
            "@odata.count": len(rows),
            "value": rows[skip : skip + top],
        }

    def _groups(self, match, query, body, origin):
        rows = [
            {k: v for k, v in w.items() if k != "modified_at"} for w in self.workspaces
        ]
        return self._paged(rows, query, 5000)

    def _admin_apps(self, match, query, body, origin):
        return self._paged(list(self.apps), query, 1000)

    def _list(self, attribute: str) -> Handler:
        def handler(match, query, body, origin):
            return 200, {"value": list(getattr(self, attribute))}

        return handler

    def _users(self, match, query, body, origin):
        report = match.group("id")
        if not any(r.get("id") == report for r in self.reports):
            return _error(404, "PowerBIEntityNotFound", f"no report {report}")
        return 200, {
            "value": [
                {
                    "displayName": "Ann",
                    "emailAddress": f"ann-{report}@example.com",
                    "reportUserAccessRight": "Owner",
                    "principalType": "User",
                }
            ]
        }

    def _datasources(self, match, query, body, origin):
        dataset = match.group("id")
        if not any(d.get("id") == dataset for d in self.datasets):
            return _error(404, "PowerBIEntityNotFound", f"no dataset {dataset}")
        return 200, {
            "value": [
                {
                    "datasourceType": "Sql",
                    "datasourceId": f"dsi-{dataset}",
                    "connectionDetails": {"server": "sql.example", "database": dataset},
                }
            ]
        }

    # -- handlers: one more item at a time, as an administrator ------------------------

    def _rows_of(self, attribute: str) -> List[Dict[str, Any]]:
        return list(
            self.workspaces if attribute == "workspaces" else getattr(self, attribute)
        )

    def _admin_users(self, attribute: str, key: str, right: str) -> Handler:
        """The people who have access to one item, as an administrator sees them."""

        def handler(match, query, body, origin):
            item = match.group("id")
            if not any(row.get(key) == item for row in self._rows_of(attribute)):
                return _error(404, "PowerBIEntityNotFound", f"no item {item}")
            return 200, {
                "value": [
                    {
                        "displayName": "Ann",
                        "emailAddress": f"ann-{item}@example.com",
                        right: "Owner" if attribute != "workspaces" else "Admin",
                        "identifier": f"ann-{item}@example.com",
                        "graphId": f"graph-{item}",
                        "principalType": "User",
                    },
                    {
                        "displayName": "Bob",
                        "emailAddress": f"bob-{item}@example.com",
                        right: "Read" if attribute != "workspaces" else "Viewer",
                        "identifier": f"bob-{item}@example.com",
                        "graphId": f"graph-bob-{item}",
                        "principalType": "User",
                    },
                ]
            }

        return handler

    def _sources_of(
        self,
        attribute: str,
        key: str,
        *,
        admin: bool = False,
        restricted: bool = False,
    ) -> Handler:
        """The data sources of a dataset or a dataflow (an account that is not an
        administrator must see the workspace, and for a dataset have Write on it)."""

        def handler(match, query, body, origin):
            item = match.group("id")
            if not admin:
                group = match.group("group")
                if group not in self._visible():
                    return _error(
                        403, "PowerBINotAuthorizedException", f"no access to {group}"
                    )
            if not any(row.get(key) == item for row in self._rows_of(attribute)):
                return _error(404, "PowerBIEntityNotFound", f"no item {item}")
            if restricted and item in self.restricted_datasets:
                return _error(
                    403, "PowerBINotAuthorizedException", "Write permission is needed"
                )
            return 200, {
                "value": [
                    {
                        "datasourceType": "Sql",
                        "datasourceId": f"dsi-{item}",
                        "gatewayId": "gw-0001",
                        "connectionDetails": {
                            "server": "sql.example",
                            "database": item,
                        },
                    }
                ]
            }

        return handler

    def _refreshables(self, match, query, body, origin):
        rows = [
            {
                "id": dataset["id"],
                "name": dataset["name"],
                "kind": "Dataset",
                "refreshCount": 14,
                "refreshFailures": 1,
                "averageDuration": 125.5,
                "medianDuration": 120.0,
                "refreshesPerDay": 2,
                "lastRefresh": {
                    "refreshType": "Scheduled",
                    "startTime": "2026-09-30T05:00:00Z",
                    "endTime": "2026-09-30T05:02:05Z",
                    "status": "Completed",
                },
                "configuredBy": [dataset.get("configuredBy", "")],
            }
            for dataset in self.datasets
        ]
        return self._paged(rows, query, 1000)

    # -- handlers: scans -------------------------------------------------------------

    def _modified(self, match, query, body, origin):
        rows = self.workspaces
        since = query.get("modifiedSince")
        if since:
            try:
                since_at = datetime.fromisoformat(since.replace("Z", "+00:00"))
            except ValueError:
                return _error(400, "InvalidRequest", "bad modifiedSince")
            age = self.now() - since_at
            if age < timedelta(minutes=30) or age > timedelta(days=30):
                return _error(
                    400,
                    "InvalidRequest",
                    "modifiedSince must be between 30 minutes and 30 days ago",
                )
            rows = [w for w in rows if w["modified_at"] >= since_at]
        return 200, [{"id": w["id"]} for w in rows]

    def _get_info(self, match, query, body, origin):
        ids = (body or {}).get("workspaces")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
            return _error(400, "InvalidRequest", "workspaces must hold 1 to 100 ids")
        flags = {
            name: query.get(name, "false").lower() == "true"
            for name in (
                "lineage",
                "datasourceDetails",
                "datasetSchema",
                "datasetExpressions",
                "getArtifactUsers",
            )
        }
        self._scan_counter += 1
        scan_id = f"scan-{self._scan_counter:04d}"
        self._scans[scan_id] = {
            "ids": list(ids),
            "flags": flags,
            "polls": 0,
            "created": self.now(),
            "fetched": False,
        }
        self.in_flight_scans += 1
        self.max_in_flight_scans = max(self.max_in_flight_scans, self.in_flight_scans)
        return 202, {
            "id": scan_id,
            "createdDateTime": self.now().isoformat(),
            "status": "NotStarted",
        }

    def _scan(self, scan_id: str) -> Optional[Dict[str, Any]]:
        scan = self._scans.get(scan_id)
        if scan and self.now() - scan["created"] > timedelta(hours=24):
            return None
        return scan

    def _scan_status(self, match, query, body, origin):
        scan_id = match.group("id")
        scan = self._scan(scan_id)
        if scan is None:
            return _error(404, "NotFound", f"no scan {scan_id}")
        scan["polls"] += 1
        if self.failing_scan_workspaces & set(scan["ids"]):
            return 200, {
                "id": scan_id,
                "status": "Failed",
                "error": {"code": "ScanFailed", "message": "the scan failed"},
            }
        status = "Succeeded" if scan["polls"] > self.scan_polls else "Running"
        return 200, {"id": scan_id, "status": status}

    def _scan_result(self, match, query, body, origin):
        scan_id = match.group("id")
        scan = self._scan(scan_id)
        if scan is None:
            return _error(404, "NotFound", f"no scan {scan_id}")
        if scan["polls"] <= self.scan_polls or self.result_not_ready_times > 0:
            if self.result_not_ready_times > 0 and scan["polls"] > self.scan_polls:
                self.result_not_ready_times -= 1
            return make_response(202, {})  # accepted, but there is no result yet
        if not scan["fetched"]:
            scan["fetched"] = True
            self.in_flight_scans -= 1
        flags = scan["flags"]
        workspaces = []
        instances = []
        for workspace_id in scan["ids"]:
            known = next((w for w in self.workspaces if w["id"] == workspace_id), None)
            if known is None:
                continue
            datasets = [d for d in self.datasets if d["workspaceId"] == workspace_id]
            entry: Dict[str, Any] = {
                "id": workspace_id,
                "name": known["name"],
                "state": known["state"],
                "type": known["type"],
                "reports": [
                    {"id": r["id"], "name": r["name"], "datasetId": r["datasetId"]}
                    for r in self.reports
                    if r["workspaceId"] == workspace_id
                ],
                "users": [
                    {
                        "displayName": "Owner",
                        "emailAddress": "owner@example.com",
                        "groupUserAccessRight": "Admin",
                        "principalType": "User",
                    }
                ],
                "datasets": [],
                "dashboards": [],
                "dataflows": [
                    {"objectId": f["objectId"], "name": f["name"]}
                    for f in self.dataflows
                    if f["workspaceId"] == workspace_id
                ],
            }
            for n, d in enumerate(datasets):
                dataset: Dict[str, Any] = {
                    "id": d["id"],
                    "name": d["name"],
                    "configuredBy": d["configuredBy"],
                    "datasourceUsages": (
                        [{"datasourceInstanceId": f"dsi-{d['id']}"}]
                        if flags["datasourceDetails"]
                        else []
                    ),
                }
                if flags["lineage"]:
                    if n == 0 and self.dataflows:
                        flow = self.dataflows[0]
                        dataset["upstreamDataflows"] = [
                            {
                                "targetDataflowId": flow["objectId"],
                                "groupId": flow["workspaceId"],
                            }
                        ]
                    else:
                        dataset["upstreamDatasets"] = [
                            {
                                "targetDatasetId": datasets[0]["id"],
                                "groupId": workspace_id,
                            }
                        ]
                entry["datasets"].append(dataset)
            if flags["lineage"]:
                for dash in self.dashboards:
                    if dash["workspaceId"] == workspace_id:
                        entry["dashboards"].append(
                            {
                                "id": dash["id"],
                                "displayName": dash["displayName"],
                                "tiles": (
                                    [
                                        {
                                            "id": "tile-1",
                                            "reportId": self.reports[0]["id"],
                                        }
                                    ]
                                    if self.reports
                                    else []
                                ),
                            }
                        )
            if flags["getArtifactUsers"]:
                for report in entry["reports"]:
                    report["users"] = [
                        {
                            "displayName": "Ann",
                            "emailAddress": "ann@example.com",
                            "reportUserAccessRight": "Owner",
                            "principalType": "User",
                        }
                    ]
            workspaces.append(entry)
            if flags["datasourceDetails"]:
                instances.extend(
                    {
                        "datasourceType": "Sql",
                        "datasourceId": f"dsi-{d['id']}",
                        "connectionDetails": {
                            "server": "sql.example",
                            "database": d["id"],
                        },
                    }
                    for d in datasets
                )
        return 200, {
            "workspaces": workspaces,
            "datasourceInstances": instances,
            "misconfiguredDatasourceInstances": [],
        }

    # -- handlers: activity events ---------------------------------------------------

    @staticmethod
    def _quoted_time(value: Optional[str], name: str) -> datetime:
        if not value or not (value.startswith("'") and value.endswith("'")):
            raise ValueError(f"{name} must be wrapped in single quotes")
        return datetime.fromisoformat(value.strip("'").replace("Z", "+00:00"))

    def _activity(self, match, query, body, origin):
        token = query.get("continuationToken")
        if token:
            day_text, offset_text, end_text = token.strip("'").split("|")
            day = date.fromisoformat(day_text)
            offset = int(offset_text)
            start = datetime.combine(day, time.min, tzinfo=UTC)
            end = datetime.fromisoformat(end_text.replace("Z", "+00:00"))
        else:
            try:
                start = self._quoted_time(query.get("startDateTime"), "startDateTime")
                end = self._quoted_time(query.get("endDateTime"), "endDateTime")
            except ValueError as error:
                return _error(400, "InvalidRequest", str(error))
            if start.date() != end.date():
                return _error(
                    400, "InvalidRequest", "start and end must be in one UTC day"
                )
            if start < self.now() - timedelta(days=28):
                return _error(400, "InvalidRequest", "events are kept for 28 days")
            if end < start:
                return _error(400, "InvalidRequest", "end is before start")
            day, offset = start.date(), 0

        matching = [
            event
            for event in self._events.get(day, [])
            if start
            <= datetime.fromisoformat(event["CreationTime"]).replace(tzinfo=UTC)
            <= end
        ]
        size = self.events_page_size
        if self.empty_first_events_page and offset == 0 and not token:
            page: List[Dict[str, Any]] = []
            following = 0
        else:
            page = matching[offset : offset + size]
            following = offset + len(page)
        more = following < len(matching) or (
            self.empty_first_events_page and offset == 0 and not token and matching
        )
        payload: Dict[str, Any] = {
            "activityEventEntities": page,
            "continuationUri": None,
            "continuationToken": None,
            "lastResultSet": not more,
        }
        if more or self.link_after_last_events_page:
            next_token = f"{day.isoformat()}|{following}|{end:%Y-%m-%dT%H:%M:%S.%f}Z"
            payload["continuationToken"] = next_token
            payload["continuationUri"] = (
                f"{origin}{PREFIX}/admin/activityevents?continuationToken='{next_token}'"
            )
        return 200, payload

    # -- handlers: what a user sees --------------------------------------------------

    def _user_groups(self, match, query, body, origin):
        visible = self._visible()
        return 200, {
            "value": [
                {"id": w["id"], "name": w["name"], "isReadOnly": False}
                for w in self.workspaces
                if w["id"] in visible
            ]
        }

    def _user_apps(self, match, query, body, origin):
        return 200, {"value": list(self.apps)}

    def _group_reports(self, match, query, body, origin):
        group = match.group("group")
        if group not in self._visible():
            return _error(403, "PowerBINotAuthorizedException", f"no access to {group}")
        return 200, {
            "value": [
                {"id": r["id"], "name": r["name"], "datasetId": r["datasetId"]}
                for r in self.reports
                if r["workspaceId"] == group
            ]
        }

    def _pages(self, match, query, body, origin):
        report = match.group("report")
        return 200, {
            "value": [
                {"name": f"{report}-page1", "displayName": "Overview", "order": 0}
            ]
        }

    def _group_list(self, attribute: str, fields: Tuple[str, ...]) -> Handler:
        """What a user sees of one kind of item in a workspace of theirs."""

        def handler(match, query, body, origin):
            group = match.group("group")
            if group not in self._visible():
                return _error(
                    403, "PowerBINotAuthorizedException", f"no access to {group}"
                )
            return 200, {
                "value": [
                    {k: row[k] for k in fields if k in row}
                    for row in getattr(self, attribute)
                    if row.get("workspaceId") == group
                ]
            }

        return handler

    def _group_users(self, match, query, body, origin):
        group = match.group("group")
        if group not in self._visible():
            return _error(403, "PowerBINotAuthorizedException", f"no access to {group}")
        return 200, {
            "value": [
                {
                    "displayName": "Owner of " + group,
                    "emailAddress": f"owner@{group}.example.com",
                    "groupUserAccessRight": "Admin",
                    "identifier": f"owner@{group}.example.com",
                    "principalType": "User",
                },
                {
                    "displayName": "Reader of " + group,
                    "emailAddress": f"reader@{group}.example.com",
                    "groupUserAccessRight": "Viewer",
                    "identifier": f"reader@{group}.example.com",
                    "principalType": "User",
                },
            ]
        }

    # -- handlers: what a user sees of one item ---------------------------------------

    def _dataset_of_a_group(self, match):
        """The dataset the path names, or the error that answers for it."""
        group, item = match.group("group"), match.group("id")
        if group not in self._visible():
            return None, _error(
                403, "PowerBINotAuthorizedException", f"no access to {group}"
            )
        found = next(
            (d for d in self.datasets if d["id"] == item and d["workspaceId"] == group),
            None,
        )
        if found is None:
            return None, _error(404, "PowerBIEntityNotFound", f"no dataset {item}")
        return found, None

    def _dataset_users_of_a_user(self, match, query, body, origin):
        dataset, error = self._dataset_of_a_group(match)
        if error is not None:
            return error
        if dataset["id"] in self.restricted_datasets:
            return _error(
                403, "PowerBINotAuthorizedException", "Reshare permission is needed"
            )
        return 200, {
            "value": [
                {
                    "identifier": f"ann-{dataset['id']}@example.com",
                    "principalType": "User",
                    "datasetUserAccessRight": "ReadWriteReshare",
                },
                {
                    "identifier": f"grp-{dataset['id']}",
                    "principalType": "Group",
                    "datasetUserAccessRight": "Read",
                },
            ]
        }

    def _refreshes(self, match, query, body, origin):
        dataset, error = self._dataset_of_a_group(match)
        if error is not None:
            return error
        if dataset["id"] in self.restricted_datasets:
            return _error(
                403, "PowerBINotAuthorizedException", "Write permission is needed"
            )
        top = int(query.get("$top", 60))
        entries = [
            {
                "refreshType": "Scheduled",
                "startTime": f"2026-09-{30 - n:02d}T05:00:00Z",
                "endTime": f"2026-09-{30 - n:02d}T05:02:00Z",
                "status": "Failed" if n == 2 else "Completed",
                "requestId": f"req-{dataset['id']}-{n}",
            }
            for n in range(0, 5)
        ]
        return 200, {"value": entries[:top]}

    def _parameters(self, match, query, body, origin):
        dataset, error = self._dataset_of_a_group(match)
        if error is not None:
            return error
        return 200, {
            "value": [
                {
                    "name": "ServerName",
                    "type": "Text",
                    "isRequired": True,
                    "currentValue": "sql.example",
                }
            ]
        }

    def _tiles(self, match, query, body, origin):
        group, item = match.group("group"), match.group("id")
        if group not in self._visible():
            return _error(403, "PowerBINotAuthorizedException", f"no access to {group}")
        if not any(
            d["id"] == item and d.get("workspaceId") == group for d in self.dashboards
        ):
            return _error(404, "PowerBIEntityNotFound", f"no dashboard {item}")
        return 200, {
            "value": [
                {
                    "id": f"tile-{item}-1",
                    "title": "Sales by month",
                    "reportId": self.reports[0]["id"] if self.reports else None,
                    "datasetId": self.datasets[0]["id"] if self.datasets else None,
                    "rowSpan": 1,
                    "colSpan": 2,
                }
            ]
        }
