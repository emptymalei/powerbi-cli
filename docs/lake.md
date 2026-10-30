# Data lake

The data lake is a folder (or an S3 prefix) where pbi_cli keeps what it fetched from the
Power BI REST API, exactly as the API returned it. The API client in `pbi_cli.core` writes
to it whenever it fetches something, and answers from it when the data is fresh enough.

!!! note "Status"

    The client and the store described here are in place and tested, but the commands do
    not use them yet: until they do, the commands keep using the [cache](cache.md). This
    page describes how the lake works and what it guarantees, so that what it holds can
    already be read by other tools.

## Why

- The admin APIs allow few requests. The workspace list, for example, allows 50 per hour
  for the whole tenant, so asking again for something you already have is expensive.
- Some data disappears from the API: audit activity events only go back 28 days, and
  the list of modified workspaces only 30 days. A lake is the only way to keep a history.
- What was fetched is useful on its own: to browse offline, to compare two days, or as
  input for other tools.

## Layout

```text
<root>/
  tenant=<tenant id>/
    endpoint=admin_groups/
      params=ab8d3b0ee9eb/                  one folder per set of request parameters
        dt=2026-09-30/
          v=20260930T204900123456Z/         one folder per fetch
            data.json                       the response, all pages merged
            manifest.json                   what was asked, when, by whom, a checksum
    endpoint=admin_activityevents/
      dt=2026-09-29/                        events are kept per UTC day
        part-0000.jsonl                     one JSON object per line
        part-0001.jsonl
        manifest.json                       parts, row count, resume cursor, sealed?
```

The folder names are Hive-style partitions, so tools such as Athena, DuckDB and Spark can
read them. The tenant comes from the token (see [authentication](auth.md)); a token that
does not name a tenant is filed under its profile instead (`tenant=profile-<name>`), never
under a guess. `params=<hash>` is the first 12 hex digits of the SHA-256 of the canonical
request parameters; the manifest spells them out.

A request is identified by its endpoint and its *canonical* parameters: booleans are lower
case and lists of unordered values, such as `$expand`, are sorted. Asking for
`$expand=users,reports` and `$expand=reports, users` is the same request; asking for a
different `$expand`, or as another tenant, never returns somebody else's answer.

The manifest is written last. A version folder without one is an interrupted write and is
ignored.

### The manifest of a snapshot

| Field | Meaning |
| --- | --- |
| `schema` | Version of this layout (currently 1). |
| `kind` | `snapshot`, or `job` for the result of an asynchronous job. |
| `endpoint` | The endpoint id (see the table below). |
| `tenant`, `profile` | The tenant, and the name of the profile used. The token is never stored. |
| `fetched_at` | When the response was received, in UTC. |
| `request` | Method, path and canonical parameters that were sent, and a hash of the body. Never headers. |
| `params`, `params_hash` | The canonical parameters and the hash that names the `params=` folder. |
| `status` | The HTTP status of the answer. |
| `pages` | How many API pages were merged into `data.json`. |
| `rows` | How many rows the response holds, when it is a list. |
| `data_file`, `bytes`, `sha256` | The data file, its size and its SHA-256. |
| `cli_version` | The pbi_cli version that wrote it. |

The manifest of a day of events has `kind: events`, the `day`, the `parts`, the number of
`rows`, the `id_field`, a resume `cursor` and whether the day is `sealed` (complete).

## Snapshots, event logs and jobs

- A **snapshot** is one response. Every fetch adds a version; nothing is overwritten.
  Old versions can be pruned, keeping the newest of each request.
- An **event log** is append-only: the events of one UTC day, deduplicated by their `Id`, so
  fetching the same day twice adds only what is new. A day is sealed once it is complete.
- A **job** is a scan: start it, poll its status, fetch its result. The result is stored
  like a snapshot.

## Freshness

A stored snapshot is used when it is younger than the `max_age` you ask for; the default is
the `ttl` of the endpoint (24 hours for the admin inventories, 1 hour for what a user can
see). Three modes change that: `refresh` ignores the lake and fetches (the answer is still
stored), `offline` never calls the API and answers from the lake whatever the age, and
`max_age=timedelta(0)` always fetches. A fresh snapshot is served even when the token has
since expired.

## Endpoints and quotas

These are the operations pbi_cli may call. Only reads are listed, plus the scanner API
whose start request is a `POST` that only asks for metadata. Operations that change things
(refresh, delete, add user, ...) and operations that return business data (`Execute
Queries`) are left out on purpose.

The quotas are copied from the documentation page of each operation (checked on
2026-09-30). A dash means the page states no quota; those endpoints are not throttled
locally and rely on the `429` answer of the API.

| Endpoint | Operation | Needs | Quota | Stored as |
| --- | --- | --- | --- | --- |
| `admin.groups` | [Workspaces of the tenant](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/groups-get-groups-as-admin) | admin | 50/h, 15/min | snapshot |
| `admin.apps` | [Apps of the tenant](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/apps-get-apps-as-admin) | admin | 200/h | snapshot |
| `admin.reports` | [Reports of the tenant](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/reports-get-reports-as-admin) | admin | 50/h, 5/min | snapshot |
| `admin.datasets` | [Datasets of the tenant](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/datasets-get-datasets-as-admin) | admin | 50/h, 5/min | snapshot |
| `admin.dashboards` | [Dashboards of the tenant](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/dashboards-get-dashboards-as-admin) | admin | 50/h, 5/min | snapshot |
| `admin.dataflows` | [Dataflows of the tenant](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/dataflows-get-dataflows-as-admin) | admin | 200/h | snapshot |
| `admin.capacities` | [Capacities of the tenant](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/get-capacities-as-admin) | admin | 200/h | snapshot |
| `admin.users.artifact_access` | [Items a user has access to](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/users-get-user-artifact-access-as-admin) | admin | 200/h | snapshot |
| `admin.reports.users` | [Users of a report](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/reports-get-report-users-as-admin) | admin | 200/h | snapshot |
| `admin.datasets.datasources` | [Data sources of a dataset](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/datasets-get-datasources-as-admin) | admin | 300/h | snapshot |
| `admin.workspaces.modified` | [IDs of the workspaces modified since a time](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/workspace-info-get-modified-workspaces) | admin | 30/h | snapshot |
| `admin.activityevents` | [Audit activity events of one UTC day](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/get-activity-events) | admin | 200/h | events |
| `admin.scan.start` | [Start a metadata scan of up to 100 workspaces](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/workspace-info-post-workspace-info) | admin | 500/h, 16 concurrent | job |
| `admin.scan.status` | [Status of a scan](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/workspace-info-get-scan-status) | admin | 10000/h | job |
| `admin.scan.result` | [Result of a finished scan](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/workspace-info-get-scan-result) | admin | 500/h | job |
| `user.groups` | [Workspaces the user has access to](https://learn.microsoft.com/en-us/rest/api/power-bi/groups/get-groups) | user | – | snapshot |
| `user.apps` | [Apps installed by the user](https://learn.microsoft.com/en-us/rest/api/power-bi/apps/get-apps) | user | – | snapshot |
| `user.group_reports` | [Reports of a workspace](https://learn.microsoft.com/en-us/rest/api/power-bi/reports/get-reports-in-group) | user | – | snapshot |
| `user.report_pages` | [Pages of a report](https://learn.microsoft.com/en-us/rest/api/power-bi/reports/get-pages-in-group) | user | – | snapshot |

## Throttling

The client counts the requests it sends (in `~/.pbi_cli/quota.json` when the command line
uses it) and waits when a documented quota is used up, instead of being refused. The
counts are an estimate: other people and tools share the same quota, so the `429 Too Many
Requests` answer of the API always wins. On a `429` the client waits for the time the API
asks for (`Retry-After`). If that is longer than five minutes it does not wait: it
reports when to try again and remembers it, so the next run does not hit the same wall.

## Sensitive data

The lake holds what the API returns, and that includes names, e-mail addresses, the users
of each report, IP addresses in activity events and, with the right scan options, the
queries behind datasets. Treat it like the tenant's administration data.

- Tokens are never written to the lake, to logs or to error messages.
- On a local disk the lake folder is readable by its owner only and so is every file in it.
- In S3 access is governed by the bucket policy; use a private bucket and encryption.
- Keep a lake out of version control.

## Using it from Python

```python
from datetime import timedelta

from pbi_cli.core.auth import Credentials
from pbi_cli.core.client import PowerBIClient
from pbi_cli.core.store import LakeStore

client = PowerBIClient(
    lambda: Credentials(token="<your token>", profile="admin-nlm", group="admin"),
    store=LakeStore("~/PowerBI/lake"),
)

result = client.fetch(
    "admin.groups", {"$expand": ["users", "reports"]}, max_age=timedelta(hours=1)
)
print(result.from_cache, len(result.data["value"]))

# Later, without the network:
result = client.fetch("admin.groups", {"$expand": ["users", "reports"]}, offline=True)
```

The reference of the modules is under [References](references/core/client.md).
