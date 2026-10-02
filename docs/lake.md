# Data lake

The data lake is a folder (or an S3 prefix) where pbi_cli keeps what it fetched from the
Power BI REST API, exactly as the API returned it. The commands that read the API write to
it, [`pbi sync`](sync.md) fills it on a schedule, `pbi lake` browses it, and other tools can
read it: it is plain JSON in Hive-style folders.

## Using it from the command line

### Turn it on

The lake is the `lake` folder inside the cache folder. Set the cache folder once:

```bash
pbi config set-cache-folder ~/PowerBI/cache           # the lake is ~/PowerBI/cache/lake
pbi config set-cache-folder s3://my-bucket/powerbi    # or a prefix in S3
```

Without a cache folder nothing is stored and the commands work as they always did.
`pbi config disable-cache` stops the commands from using the lake without forgetting the
folder, and `pbi config enable-cache` switches it back on. Browsing with `pbi lake` works
either way.

### What the commands store

| Command | What is stored | `--use-cache`, `--cache-only` |
| --- | --- | --- |
| `pbi workspaces list` | `admin.groups` | yes |
| `pbi users user-access` | `admin.users.artifact_access` | yes |
| `pbi apps list` | `user.apps`, or `admin.apps` with `--role admin` | yes |
| `pbi reports list` | `user.group_reports` | no: always asks the API |
| `pbi reports pages` | `user.report_pages` (and `user.group_reports` without `--report-id`) | no: always asks the API |
| `pbi workspaces scan batch` | `admin.scan.result`, one per batch of 100 workspaces | no: always scans |
| [`pbi sync run`](sync.md) | what its targets need: lists, audit events, scans, fan-outs | it skips what is fresh (`--max-age`, `--force`) |

The endpoint ids are explained under [Endpoints and quotas](#endpoints-and-quotas). The
other commands that call the API (`workspaces scan initiate|status|result|get`,
`workspaces report-users`, `reports users`, `reports export`, `apps app`, `apps augment`
and `export`) do not use the lake yet.

Every call asks the API, waits when a [quota](#throttling) is used up, and adds a new
version to the lake: nothing is overwritten. The commands that print a table say which
version they stored (`Saved to the data lake (version: 20260930T214332941747Z)`); the
commands that print JSON, such as `pbi reports list`, print only the JSON so that it can be
piped, and store silently.

### Reuse what is stored

```bash
# A stored answer to this very request, of any age; the API only if there is none
pbi workspaces list --use-cache

# Only a stored answer: fails if there is none, never calls the API
pbi workspaces list --cache-only
```

- A stored answer belongs to a *request*: the endpoint, its parameters (`--top`,
  `--expand`, `--odata-filter`, the user id, the role) and the tenant. `--use-cache` with
  another `--top` or `--expand` does not return the answer to the first one: it asks the
  API. The order of the `--expand` values does not matter. (The earlier
  [cache](cache.md) kept one entry per command, so it could answer with data that had been
  fetched with other options, or by another tenant.)
- With `--use-cache` and `--cache-only` together, `--cache-only` wins.
- The commands read your stored token even for `--cache-only`: its tenant tells which data
  is yours. An expired token is fine, as nothing is sent.
- Without a lake (no cache folder, or caching disabled) `--cache-only` stops and says how
  to set one up; `--use-cache` just asks the API.
- A lake that cannot be read (a damaged file, S3 not reachable) does not stop `--use-cache`:
  the command warns and asks the API, which stores a fresh answer. `--cache-only` has no API
  to fall back on, so it stops and says why.

### Look into the lake: `pbi lake`

`pbi lake` reads the lake only: it needs no token and no network, so it works offline and
with an expired token.

`pbi lake ls` lists what is stored, one line per request (an endpoint with one set of
parameters), with its `REF`: the name of its `params=` folder. `--all-versions` lists
every stored version instead of the newest of each request.

```text
$ pbi lake ls
Data lake: ~/PowerBI/cache/lake

Tenant: 0b6e7f5a-3c1d-4f7e-9a52-7d3c1e8b2f40
ENDPOINT              REF           FETCHED (UTC)     AGE     ROWS  SIZE   VERSIONS  PARAMETERS
admin.activityevents  events        2026-09-30 21:56  0 s     1     -      1 day(s)  newest day 2026-09-28 (sealed)
admin.groups          60177fd656f3  2026-09-30 21:44  12 min  4     143 B  2         $expand=dashboards,dataflows,datasets,reports,users,workbooks $top=1000
admin.groups          9a414e1d2726  2026-09-30 18:56  3 h     1     44 B   1         $top=50
user.apps             44136fa355b3  2026-09-30 20:56  1 h     1     42 B   1         -

$ pbi lake ls -e admin.groups --all-versions
Data lake: ~/PowerBI/cache/lake

Tenant: 0b6e7f5a-3c1d-4f7e-9a52-7d3c1e8b2f40
ENDPOINT      REF           VERSION                 FETCHED (UTC)     AGE     ROWS  SIZE   PARAMETERS
admin.groups  60177fd656f3  20260930T214424436365Z  2026-09-30 21:44  12 min  4     143 B  $expand=dashboards,dataflows,datasets,reports,users,workbooks $top=1000
admin.groups  60177fd656f3  20260928T215624436365Z  2026-09-28 21:56  2 d     3     110 B  $expand=dashboards,dataflows,datasets,reports,users,workbooks $top=1000
admin.groups  9a414e1d2726  20260930T185624436365Z  2026-09-30 18:56  3 h     1     44 B   $top=50
```

`pbi lake show ENDPOINT` prints a stored response as JSON. When an endpoint was fetched
with several sets of parameters, say which one with `-p KEY=VALUE` (as shown under
`PARAMETERS`; the order of the values of `$expand` does not matter) or with `--ref`.
`--version` picks an older version and `--manifest` prints what was asked, when, by which
profile and a checksum, instead of the data.

```text
$ pbi lake show admin.groups
Error: 2 stored requests of admin.groups match; choose one with -p KEY=VALUE or --ref:
  60177fd656f3  $expand=dashboards,dataflows,datasets,reports,users,workbooks $top=1000
  9a414e1d2726  $top=50

$ pbi lake show admin.groups -p '$top=50'
{
  "value": [
    {
      "id": "g0",
      "name": "Workspace 0"
    }
  ]
}

$ pbi lake show admin.groups -p '$top=50' --manifest
{
  "schema": 1,
  "kind": "snapshot",
  "endpoint": "admin.groups",
  "tenant": "0b6e7f5a-3c1d-4f7e-9a52-7d3c1e8b2f40",
  "profile": "admin-nlm",
  "fetched_at": "2026-09-30T18:56:24.436365+00:00",
  "request": {
    "method": "GET",
    "path": "/admin/groups",
    "params": {
      "$top": "50"
    }
  },
  "params": {
    "$top": "50"
  },
  "params_hash": "9a414e1d2726",
  "status": 200,
  "pages": 1,
  "rows": 1,
  "data_file": "data.json",
  "bytes": 44,
  "sha256": "ecf45bef6dda1dbfac0201cd0cd4596ad7481b9ab4ab982b27cbdf0072500695",
  "cli_version": "0.4.0"
}
```

Event logs are kept per UTC day: choose the day with `--day`. The events are printed one
per line.

```text
$ pbi lake show admin.activityevents --day 2026-09-28
{"Id": "e1", "Activity": "ViewReport", "UserId": "alice@example.com"}
```

The lake grows, because every call adds a version. `pbi lake prune` deletes old versions
and keeps the newest of each request (`--keep N` keeps more; `--endpoint` and `--tenant`
narrow it down). It asks first, or pass `--yes`. Event logs are never pruned.

```text
$ pbi lake prune --yes
✓ Deleted 1 old version(s), kept the newest 1 of each request
```

`pbi cache clear` never touches the lake; see [Cache (legacy)](cache.md).

Every one of these commands (and `pbi sync status` and `pbi tui`) can look at another lake
than the one of your cache folder, such as one that a colleague shared, with
`--lake <folder or s3://bucket/folder>`. A lake opened that way is only read, and `prune`
refuses it. `pbi lake publish` makes a copy of your lake for others to open. See
[Sharing a lake](sharing.md).

To look around instead of listing, open the [terminal UI](tui.md) (`pbi tui`): the
workspaces of the tenant, what is in them, who can open it, how it is connected, and how
fresh each part is, all read from the lake.

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
    endpoint=admin_scan_result/
      params=5904788ae261/                  one folder per scan: its workspaces and options
        dt=2026-09-30/v=.../                a scan is stored like a snapshot (a "job")
    _state/
      sync.json                             what `pbi sync` remembers between runs
  publish.json                              only in a published lake: who, when, what was left out
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

A lake with a `publish.json` at its root has been [published](sharing.md#publish-a-lake). It
is protected: only `pbi lake publish` writes to it.

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
`rows`, the `id_field`, a resume `cursor`, when it was last written (`updated_at`) and
whether the day is `sealed` (complete).

The manifest of a scan has `kind: job`. `params` holds the options of the scan and `batch`,
which names its set of workspaces; `workspace_ids` lists them, and `scan_id` and
`started_at` tell which scan of the API it is. `rows` is the number of workspaces.

## Snapshots, event logs and jobs

- A **snapshot** is one response. Every fetch adds a version; nothing is overwritten.
  Old versions can be pruned, keeping the newest of each request.
- An **event log** is append-only: the events of one UTC day, deduplicated by their `Id`, so
  fetching the same day twice adds only what is new. A day is sealed once it is complete.
- A **job** is a scan: start it, poll its status, fetch its result. The result is stored
  like a snapshot, one request per set of workspaces and options, so scanning the same
  workspaces again adds a version. See [Sync](sync.md#scans).

## Freshness

A stored snapshot is used when it is younger than the `max_age` you ask for; the default is
the `ttl` of the endpoint (24 hours for the admin inventories, 1 hour for what a user can
see). Three modes change that: `refresh` ignores the lake and fetches (the answer is still
stored), `offline` never calls the API and answers from the lake whatever the age, and
`max_age=timedelta(0)` always fetches. A fresh snapshot is served even when the token has
since expired.

The commands do not use the `ttl`: they ask the API unless you pass `--use-cache` (a
stored answer of any age) or `--cache-only` (`offline`). The `ttl` applies to
`client.fetch` calls that pass no `max_age`, as in the Python example below.

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
| `admin.groups.users` | [Users of a workspace](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/groups-get-group-users-as-admin) | admin | 200/h | snapshot |
| `admin.datasets.users` | [Users of a dataset](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/datasets-get-dataset-users-as-admin) | admin | 200/h | snapshot |
| `admin.dashboards.users` | [Users of a dashboard](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/dashboards-get-dashboard-users-as-admin) | admin | 200/h | snapshot |
| `admin.dataflows.users` | [Users of a dataflow](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/dataflows-get-dataflow-users-as-admin) | admin | 200/h | snapshot |
| `admin.dataflows.datasources` | [Data sources of a dataflow](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/dataflows-get-dataflow-datasources-as-admin) | admin | – | snapshot |
| `admin.refreshables` | [How the datasets of the tenant refresh (a summary of each)](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/get-refreshables) | admin | 200/h | snapshot |
| `admin.workspaces.modified` | [IDs of the workspaces modified since a time](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/workspace-info-get-modified-workspaces) | admin | 30/h | snapshot |
| `admin.activityevents` | [Audit activity events of one UTC day](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/get-activity-events) | admin | 200/h | events |
| `admin.scan.start` | [Start a metadata scan of up to 100 workspaces](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/workspace-info-post-workspace-info) | admin | 500/h, 16 concurrent | job |
| `admin.scan.status` | [Status of a scan](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/workspace-info-get-scan-status) | admin | 10000/h | job |
| `admin.scan.result` | [Result of a finished scan](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/workspace-info-get-scan-result) | admin | 500/h | job |
| `user.groups` | [Workspaces the user has access to](https://learn.microsoft.com/en-us/rest/api/power-bi/groups/get-groups) | user | – | snapshot |
| `user.apps` | [Apps installed by the user](https://learn.microsoft.com/en-us/rest/api/power-bi/apps/get-apps) | user | – | snapshot |
| `user.group_reports` | [Reports of a workspace](https://learn.microsoft.com/en-us/rest/api/power-bi/reports/get-reports-in-group) | user | – | snapshot |
| `user.group_datasets` | [Datasets of a workspace](https://learn.microsoft.com/en-us/rest/api/power-bi/datasets/get-datasets-in-group) | user | – | snapshot |
| `user.group_dashboards` | [Dashboards of a workspace](https://learn.microsoft.com/en-us/rest/api/power-bi/dashboards/get-dashboards-in-group) | user | – | snapshot |
| `user.group_dataflows` | [Dataflows of a workspace](https://learn.microsoft.com/en-us/rest/api/power-bi/dataflows/get-dataflows) | user | – | snapshot |
| `user.group_users` | [Users of a workspace](https://learn.microsoft.com/en-us/rest/api/power-bi/groups/get-group-users) | user | – | snapshot |
| `user.report_pages` | [Pages of a report](https://learn.microsoft.com/en-us/rest/api/power-bi/reports/get-pages-in-group) | user | – | snapshot |
| `user.dataset_users` | [Users of a dataset](https://learn.microsoft.com/en-us/rest/api/power-bi/datasets/get-dataset-users-in-group) | user | – | snapshot |
| `user.dataset_datasources` | [Data sources of a dataset](https://learn.microsoft.com/en-us/rest/api/power-bi/datasets/get-datasources-in-group) | user | – | snapshot |
| `user.dataflow_datasources` | [Data sources of a dataflow](https://learn.microsoft.com/en-us/rest/api/power-bi/dataflows/get-dataflow-data-sources) | user | – | snapshot |
| `user.dataset_refreshes` | [Refresh history of a dataset](https://learn.microsoft.com/en-us/rest/api/power-bi/datasets/get-refresh-history-in-group) | user | – | snapshot |
| `user.dataset_parameters` | [Parameters of a dataset](https://learn.microsoft.com/en-us/rest/api/power-bi/datasets/get-parameters-in-group) | user | – | snapshot |
| `user.dashboard_tiles` | [Tiles of a dashboard](https://learn.microsoft.com/en-us/rest/api/power-bi/dashboards/get-tiles-in-group) | user | – | snapshot |

Some operations of a user need more than being able to see the item. The users of a dataset
(`user.dataset_users`) need Reshare permission on it, and its data sources and refresh history
(`user.dataset_datasources`, `user.dataset_refreshes`) need Write permission. An account that
lacks it gets `403`, and the error says which permission is missing. The operations that
answer about one item (`...users`, `...datasources`, `user.dataset_refreshes`,
`user.dataset_parameters`, `user.dashboard_tiles`, `user.report_pages`) take the id of the
item in their path, and the lake keeps the answer for each item under it.

`admin.refreshables` is different: one list of the whole tenant, with a summary of how each
dataset refreshed in the last week (how often, how long, how many failed, the last refresh and
the schedule). It is how an administrator sees the refreshes of a dataset, because the
refresh history itself (`user.dataset_refreshes`) is read with a user's account.

## Throttling

The client counts the requests it sends (the commands keep the counts in
`~/.pbi_cli/quota.json`, so a second run knows what the first one used) and waits when a
documented quota is used up, instead of being refused. A command waits at most two minutes
for quota; if that is not enough it stops and says when to run it again. The
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

The commands build their client with `pbi_cli.session.open_client`, which finds the lake
from the cache folder of the settings and keeps the quota counters in
`~/.pbi_cli/quota.json`. The reference of the modules is under
[References](references/core/client.md) and [`pbi_cli.session`](references/session.md).
