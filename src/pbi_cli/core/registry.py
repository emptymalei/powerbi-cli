"""The Power BI REST operations pbi-cli may call, with their documented quotas.

Only read-only operations are listed (plus the scanner API, whose start request is a
``POST`` that only asks for metadata). Operations that change things (refresh, delete,
add user, ...) and operations that return business data (``Execute Queries``) are not
here on purpose: they are never cached or synced.

A rate limit is recorded only when the documentation page of the operation states one
(verified 2026-09-30, see ``doc_url``). ``limit=None`` means the documentation states
none; such endpoints are not throttled locally and rely on the ``429 Retry-After``
answer of the API.
"""

import re
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import quote

from pbi_cli.errors import PBIError

#: Every path below is relative to this.
BASE_URL = "https://api.powerbi.com/v1.0/myorg"

_PLACEHOLDER = re.compile(r"{([A-Za-z0-9_]+)}")
_DOCS = "https://learn.microsoft.com/en-us/rest/api/power-bi"

HOUR = 3600
MINUTE = 60


class Scope(str, Enum):
    """Which kind of token an operation needs."""

    ADMIN = "admin"
    USER = "user"


class Kind(str, Enum):
    """How the result is stored in the lake."""

    #: One response, stored as a versioned snapshot.
    SNAPSHOT = "snapshot"
    #: Append-only events, stored per UTC day.
    EVENTS = "events"
    #: A step of an asynchronous job (scan start, status, result).
    JOB = "job"


class Paging(str, Enum):
    """How a list that does not fit in one response is read."""

    NONE = "none"
    #: ``$top`` and ``$skip`` (OData).
    SKIP = "odata-skip"
    #: ``continuationUri`` / ``continuationToken`` in the response.
    CONTINUATION = "continuation"


@dataclass(frozen=True)
class RateLimit:
    """A documented quota.

    :param windows: ``(max requests, per seconds)`` pairs that all apply
    :param max_concurrent: maximum simultaneous requests, when documented
    """

    windows: Tuple[Tuple[int, int], ...]
    max_concurrent: Optional[int] = None

    def describe(self) -> str:
        """The quota in words, for example ``50/h, 15/min``."""
        names = {HOUR: "h", MINUTE: "min"}
        parts = [
            f"{count}/{names.get(seconds, f'{seconds}s')}"
            for count, seconds in sorted(self.windows, key=lambda w: -w[1])
        ]
        if self.max_concurrent:
            parts.append(f"{self.max_concurrent} concurrent")
        return ", ".join(parts)


@dataclass(frozen=True)
class Endpoint:
    """One operation of the Power BI REST API.

    :param id: short stable name, used in the lake and on the command line
    :param title: what the operation does
    :param method: ``GET`` or ``POST``
    :param path: path relative to `BASE_URL`, with ``{placeholders}``
    :param scope: the kind of token the operation needs
    :param kind: how the result is stored
    :param doc_url: the documentation page of the operation
    :param limit: the documented quota, ``None`` when the documentation states none
    :param paging: how lists are read
    :param page_size: largest page the API accepts (for `Paging.SKIP`)
    :param items_keys: response keys that hold the list of rows, first match wins;
        empty when the response body is the list itself
    :param query: the query parameters the operation accepts
    :param multi_value: query parameters that take an unordered comma separated list
    :param ttl: how long a snapshot counts as fresh
    :param parent: id of the operation that lists the items this one is called for
    :param parent_field: field of the parent's rows that fills the path placeholder
    """

    id: str
    title: str
    method: str
    path: str
    scope: Scope
    kind: Kind
    doc_url: str
    limit: Optional[RateLimit] = None
    paging: Paging = Paging.NONE
    page_size: Optional[int] = None
    items_keys: Tuple[str, ...] = ("value",)
    query: Tuple[str, ...] = ()
    multi_value: Tuple[str, ...] = ()
    ttl: timedelta = timedelta(hours=24)
    parent: Optional[str] = None
    parent_field: str = "id"

    @property
    def path_params(self) -> Tuple[str, ...]:
        """Names of the ``{placeholders}`` in the path, in order."""
        return tuple(_PLACEHOLDER.findall(self.path))

    def split_params(
        self, params: Optional[Mapping[str, Any]]
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Split caller parameters into path placeholders and query parameters.

        ``None`` values are dropped.

        :raises ValueError: for a missing placeholder or an unknown query parameter
        """
        given = {k: v for k, v in (params or {}).items() if v is not None}
        path_params = {k: given.pop(k) for k in self.path_params if k in given}
        missing = [k for k in self.path_params if k not in path_params]
        if missing:
            raise ValueError(f"{self.id}: missing path parameter(s) {missing}")
        unknown = sorted(set(given) - set(self.query))
        if unknown:
            raise ValueError(
                f"{self.id}: unknown parameter(s) {unknown}; "
                f"accepted: {sorted(self.path_params + self.query)}"
            )
        return path_params, given

    def split_canonical(
        self, params: Optional[Mapping[str, Any]]
    ) -> Tuple[Dict[str, str], Dict[str, str]]:
        """Like `split_params`, with every value as a canonical string.

        Equal requests give equal results whatever the spelling: booleans are lower
        case, and lists of unordered values (``$expand``) are sorted.
        """
        path_params, query = self.split_params(params)
        return (
            {
                key: _canonical_value(key, value, False)
                for key, value in sorted(path_params.items())
            },
            {
                key: _canonical_value(key, value, key in self.multi_value)
                for key, value in sorted(query.items())
            },
        )

    def canonical_params(self, params: Optional[Mapping[str, Any]]) -> Dict[str, str]:
        """Path and query parameters together as sorted canonical strings.

        This is what identifies a request in the lake.
        """
        path_params, query = self.split_canonical(params)
        return dict(sorted({**path_params, **query}.items()))

    def build_url(
        self,
        path_params: Mapping[str, Any],
        query: Mapping[str, str],
        base_url: str = BASE_URL,
    ) -> str:
        """The full URL for canonical path and query parameters."""
        path = self.path
        for name in self.path_params:
            path = path.replace(f"{{{name}}}", quote(str(path_params[name]), safe=""))
        url = f"{base_url}{path}"
        if query:
            url += "?" + "&".join(
                f"{quote(k, safe='')}={quote(str(v), safe='')}"
                for k, v in sorted(query.items())
            )
        return url


def _canonical_value(name: str, value: Any, multi: bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set, frozenset)):
        if not multi:
            raise ValueError(f"parameter '{name}' takes a single value, got a list")
        return ",".join(
            sorted({str(item).strip() for item in value if str(item).strip()})
        )
    text = str(value)
    if multi:
        return ",".join(sorted({p.strip() for p in text.split(",") if p.strip()}))
    return text


def _admin_list(
    endpoint_id: str,
    title: str,
    path: str,
    doc: str,
    limit: RateLimit,
    **kwargs: Any,
) -> Endpoint:
    return Endpoint(
        id=endpoint_id,
        title=title,
        method="GET",
        path=path,
        scope=Scope.ADMIN,
        kind=Kind.SNAPSHOT,
        doc_url=f"{_DOCS}/admin/{doc}",
        limit=limit,
        **kwargs,
    )


_PER_HOUR_AND_MINUTE_LARGE = RateLimit(windows=((50, HOUR), (5, MINUTE)))

ENDPOINTS: Tuple[Endpoint, ...] = (
    # --- admin: inventory ---------------------------------------------------------
    _admin_list(
        "admin.groups",
        "Workspaces of the tenant",
        "/admin/groups",
        "groups-get-groups-as-admin",
        RateLimit(windows=((50, HOUR), (15, MINUTE))),
        paging=Paging.SKIP,
        page_size=5000,
        query=("$expand", "$filter", "$top", "$skip"),
        multi_value=("$expand",),
    ),
    _admin_list(
        "admin.apps",
        "Apps of the tenant",
        "/admin/apps",
        "apps-get-apps-as-admin",
        RateLimit(windows=((200, HOUR),)),
        paging=Paging.SKIP,
        page_size=200,
        query=("$top", "$skip"),
    ),
    _admin_list(
        "admin.reports",
        "Reports of the tenant",
        "/admin/reports",
        "reports-get-reports-as-admin",
        _PER_HOUR_AND_MINUTE_LARGE,
        query=("$filter", "$top", "$skip"),
    ),
    _admin_list(
        "admin.datasets",
        "Datasets of the tenant",
        "/admin/datasets",
        "datasets-get-datasets-as-admin",
        _PER_HOUR_AND_MINUTE_LARGE,
        query=("$filter", "$top", "$skip"),
    ),
    _admin_list(
        "admin.dashboards",
        "Dashboards of the tenant",
        "/admin/dashboards",
        "dashboards-get-dashboards-as-admin",
        _PER_HOUR_AND_MINUTE_LARGE,
        query=("$expand", "$filter", "$top", "$skip"),
        multi_value=("$expand",),
    ),
    _admin_list(
        "admin.dataflows",
        "Dataflows of the tenant",
        "/admin/dataflows",
        "dataflows-get-dataflows-as-admin",
        RateLimit(windows=((200, HOUR),)),
        query=("$filter", "$top", "$skip"),
    ),
    _admin_list(
        "admin.capacities",
        "Capacities of the tenant",
        "/admin/capacities",
        "get-capacities-as-admin",
        RateLimit(windows=((200, HOUR),)),
        query=("$expand",),
        multi_value=("$expand",),
    ),
    # --- admin: one item at a time --------------------------------------------------
    _admin_list(
        "admin.users.artifact_access",
        "Items a user has access to",
        "/admin/users/{userId}/artifactAccess",
        "users-get-user-artifact-access-as-admin",
        RateLimit(windows=((200, HOUR),)),
        paging=Paging.CONTINUATION,
        # The documentation spells the key in camel case; older responses used Pascal
        # case (which the existing commands read), so accept both.
        items_keys=("artifactAccessEntities", "ArtifactAccessEntities"),
        query=("artifactTypes", "continuationToken"),
        multi_value=("artifactTypes",),
    ),
    _admin_list(
        "admin.reports.users",
        "Users of a report",
        "/admin/reports/{reportId}/users",
        "reports-get-report-users-as-admin",
        RateLimit(windows=((200, HOUR),)),
        parent="admin.reports",
    ),
    _admin_list(
        "admin.datasets.datasources",
        "Data sources of a dataset",
        "/admin/datasets/{datasetId}/datasources",
        "datasets-get-datasources-as-admin",
        RateLimit(windows=((300, HOUR),)),
        parent="admin.datasets",
    ),
    # --- admin: change tracking and audit ---------------------------------------------
    _admin_list(
        "admin.workspaces.modified",
        "IDs of the workspaces modified since a time",
        "/admin/workspaces/modified",
        "workspace-info-get-modified-workspaces",
        RateLimit(windows=((30, HOUR),)),
        items_keys=(),  # the body is the list
        query=(
            "modifiedSince",
            "excludePersonalWorkspaces",
            "excludeInActiveWorkspaces",
        ),
        ttl=timedelta(hours=1),
    ),
    Endpoint(
        id="admin.activityevents",
        title="Audit activity events of one UTC day",
        method="GET",
        path="/admin/activityevents",
        scope=Scope.ADMIN,
        kind=Kind.EVENTS,
        doc_url=f"{_DOCS}/admin/get-activity-events",
        limit=RateLimit(windows=((200, HOUR),)),
        paging=Paging.CONTINUATION,
        items_keys=("activityEventEntities",),
        query=("startDateTime", "endDateTime", "continuationToken", "$filter"),
    ),
    # --- admin: workspace scans (an asynchronous job) -----------------------------------
    Endpoint(
        id="admin.scan.start",
        title="Start a metadata scan of up to 100 workspaces",
        method="POST",
        path="/admin/workspaces/getInfo",
        scope=Scope.ADMIN,
        kind=Kind.JOB,
        doc_url=f"{_DOCS}/admin/workspace-info-post-workspace-info",
        limit=RateLimit(windows=((500, HOUR),), max_concurrent=16),
        items_keys=(),
        query=(
            "lineage",
            "datasourceDetails",
            "datasetSchema",
            "datasetExpressions",
            "getArtifactUsers",
        ),
    ),
    Endpoint(
        id="admin.scan.status",
        title="Status of a scan",
        method="GET",
        path="/admin/workspaces/scanStatus/{scanId}",
        scope=Scope.ADMIN,
        kind=Kind.JOB,
        doc_url=f"{_DOCS}/admin/workspace-info-get-scan-status",
        limit=RateLimit(windows=((10000, HOUR),)),
        items_keys=(),
    ),
    Endpoint(
        id="admin.scan.result",
        title="Result of a finished scan",
        method="GET",
        path="/admin/workspaces/scanResult/{scanId}",
        scope=Scope.ADMIN,
        kind=Kind.JOB,
        doc_url=f"{_DOCS}/admin/workspace-info-get-scan-result",
        limit=RateLimit(windows=((500, HOUR),)),
        items_keys=(),
    ),
    # --- user: what the signed in user can see ---------------------------------------
    Endpoint(
        id="user.groups",
        title="Workspaces the user has access to",
        method="GET",
        path="/groups",
        scope=Scope.USER,
        kind=Kind.SNAPSHOT,
        doc_url=f"{_DOCS}/groups/get-groups",
        query=("$filter", "$top", "$skip"),
        ttl=timedelta(hours=1),
    ),
    Endpoint(
        id="user.apps",
        title="Apps installed by the user",
        method="GET",
        path="/apps",
        scope=Scope.USER,
        kind=Kind.SNAPSHOT,
        doc_url=f"{_DOCS}/apps/get-apps",
        ttl=timedelta(hours=1),
    ),
    Endpoint(
        id="user.group_reports",
        title="Reports of a workspace",
        method="GET",
        path="/groups/{groupId}/reports",
        scope=Scope.USER,
        kind=Kind.SNAPSHOT,
        doc_url=f"{_DOCS}/reports/get-reports-in-group",
        ttl=timedelta(hours=1),
        parent="user.groups",
    ),
    Endpoint(
        id="user.report_pages",
        title="Pages of a report",
        method="GET",
        path="/groups/{groupId}/reports/{reportId}/pages",
        scope=Scope.USER,
        kind=Kind.SNAPSHOT,
        doc_url=f"{_DOCS}/reports/get-pages-in-group",
        ttl=timedelta(hours=1),
    ),
)

_BY_ID: Dict[str, Endpoint] = {endpoint.id: endpoint for endpoint in ENDPOINTS}


def get_endpoint(endpoint_id: str) -> Endpoint:
    """Return the endpoint with this id.

    :raises PBIError: if there is no such endpoint
    """
    try:
        return _BY_ID[endpoint_id]
    except KeyError:
        raise PBIError(
            f"Unknown endpoint '{endpoint_id}'. Known endpoints: "
            + ", ".join(sorted(_BY_ID))
        ) from None
