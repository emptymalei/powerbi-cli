# Sync

`pbi sync` keeps what Power BI knows about your tenant in the [data lake](lake.md): the
lists of workspaces, apps, reports and more, the audit events, and the metadata scans of
the workspaces. You run it as often as you like, for example every night. It respects the
quotas of the API, never fetches twice what is still fresh, and continues where the last
run stopped: after an expired token, after a quota ran out, after a failure.

!!! warning "Requires Admin"

    The admin targets need a token of a Fabric administrator (see
    [Authentication](auth.md)); the `user-...` targets need a user's token.

## Quick start

Set the cache folder once (the lake is its `lake` folder, see [Data lake](lake.md)), then
look at what a sync would do, run it, and look at how it went:

```bash
pbi config set-cache-folder ~/PowerBI/cache

pbi sync plan      # what would be fetched, and what it costs: no request is made
pbi sync run       # fetch it
pbi sync status    # how the last runs went, and what the lake holds
```

## What can be synced

A sync without names fetches the *plain* targets. The other targets copy personal data or
queries, or need a user's token, so they run only when you name them, for example
`pbi sync run default activity`. `default` stands for the plain targets and `all` for
every target.

| Target | What it keeps | Operation | Needs | In a plain sync |
| --- | --- | --- | --- | --- |
| `groups` | Workspaces | `admin.groups` | admin | yes |
| `apps` | Apps | `admin.apps` | admin | yes |
| `capacities` | Capacities | `admin.capacities` | admin | yes |
| `reports` | Reports | `admin.reports` | admin | yes |
| `datasets` | Datasets | `admin.datasets` | admin | yes |
| `dashboards` | Dashboards | `admin.dashboards` | admin | yes |
| `dataflows` | Dataflows | `admin.dataflows` | admin | yes |
| `scan` | Metadata scan of every workspace | `admin.scan.result` | admin | no: the contents of every workspace; with the scan options also data source details, dataset schemas and queries (DAX, Power Query) and the users of every item |
| `report-users` | The users of every report | `admin.reports.users` | admin | no: the people who can open each report, with their e-mail addresses |
| `datasources` | The data sources of every dataset | `admin.datasets.datasources` | admin | no: connection details of the data sources: servers, databases, paths |
| `activity` | Audit activity events, one log per UTC day | `admin.activityevents` | admin | no: what each person did and when: e-mail addresses, IP addresses, devices |
| `user-groups` | Workspaces of the user | `user.groups` | user | no: needs a user's token |
| `user-apps` | Apps of the user | `user.apps` | user | no: needs a user's token |
| `user-reports` | Reports of each workspace of the user | `user.group_reports` | user | no: needs a user's token |
| `user-pages` | Pages of each report of the user | `user.report_pages` | user | no: needs a user's token |

`report-users`, `datasources`, `user-reports` and `user-pages` ask once for every row of
another target (the users of every report, the pages of every report of every workspace).
Naming one brings the target it needs along, and the plan says so. The quota of each
operation is in the [table of the data lake](lake.md#endpoints-and-quotas).

## Look before you fetch

`pbi sync plan` works out what a sync would do from what the lake holds. It makes no
request. It counts how much of each target is fresh, how many requests the rest needs,
and how that compares with the quota that is left:

```text
$ pbi sync plan
Data lake: ~/PowerBI/cache/lake
Targets: groups, apps, capacities, reports, datasets, dashboards, dataflows
Not included (name them to include them, see --help): scan, report-users, datasources, activity, user-groups, user-apps, user-reports, user-pages
Tenant: 0b6e7f5a-3c1d-4f7e-9a52-7d3c1e8b2f40

TARGET      OPERATION         UNITS  FRESH  TO DO  REQUESTS
groups      admin.groups      1      0      1      1
apps        admin.apps        1      0      1      1
capacities  admin.capacities  1      0      1      1
reports     admin.reports     1      0      1      1
datasets    admin.datasets    1      0      1      1
dashboards  admin.dashboards  1      0      1      1
dataflows   admin.dataflows   1      0      1      1

Requests against the quota of each operation
OPERATION         NEEDED  QUOTA         LEFT NOW
admin.groups      1       50/h, 15/min  15
admin.apps        1       200/h         200
admin.reports     1       50/h, 5/min   5
admin.datasets    1       50/h, 5/min   5
admin.dashboards  1       50/h, 5/min   5
admin.dataflows   1       200/h         200
admin.capacities  1       200/h         200

Everything fits the quota now.
```

Some numbers cannot be known yet. The users of every report depend on the list of reports,
and a scan depends on how many workspaces there are; until those are in the lake the plan
shows `?` and says what it is waiting for. Once they are, it counts them. This is the plan
after the plain targets were fetched, for a sync that also takes a scan and three days of
audit events:

```text
$ pbi sync plan default activity scan --lineage --days 3
Data lake: ~/PowerBI/cache/lake
Targets: groups, apps, capacities, reports, datasets, dashboards, dataflows, scan, activity
  scan copies the contents of every workspace; with the scan options also data source details, dataset schemas and queries (DAX, Power Query) and the users of every item
  activity copies what each person did and when: e-mail addresses, IP addresses, devices
Tenant: 0b6e7f5a-3c1d-4f7e-9a52-7d3c1e8b2f40

TARGET      OPERATION             UNITS  FRESH  TO DO  REQUESTS
groups      admin.groups          1      1      0      0
apps        admin.apps            1      1      0      0
capacities  admin.capacities      1      1      0      0
reports     admin.reports         1      1      0      0
datasets    admin.datasets        1      1      0      0
dashboards  admin.dashboards      1      1      0      0
dataflows   admin.dataflows       1      1      0      0
scan        admin.scan.result     4      0      4      16
activity    admin.activityevents  3      0      3      3
  scan: every workspace is scanned: 250 are in the stored list of workspaces, in batches of up to 100 (the list may be out of date)

Requests against the quota of each operation
OPERATION                  NEEDED  QUOTA                 LEFT NOW
admin.workspaces.modified  1       30/h                  30
admin.activityevents       3       200/h                 200
admin.scan.start           3       500/h, 16 concurrent  500
admin.scan.status          9       10000/h               10000
admin.scan.result          3       500/h                 500

Everything fits the quota now.
```

## Fetch

`pbi sync run` fetches what the plan shows, working through the units with a few threads
(`--workers`, 4 by default, at most 16). It prints the units that are done, in the order
they finish; a big stage prints only its milestones and the units that failed.

```text
$ pbi sync run
Data lake: ~/PowerBI/cache/lake
Targets: groups, apps, capacities, reports, datasets, dashboards, dataflows
Not included (name them to include them, see --help): scan, report-users, datasources, activity, user-groups, user-apps, user-reports, user-pages

Stage 1: 7 unit(s) of groups, apps, capacities, reports, datasets, dashboards, dataflows
  ✓ admin.capacities  1 rows
  ✓ admin.reports  12 rows
  ✓ admin.apps  3 rows
  ✓ admin.dashboards  1 rows
  ✓ admin.groups  250 rows
  ✓ admin.datasets  6 rows
  ✓ admin.dataflows  1 rows

Finished in 0 seconds: 7 fetched, 0 fresh (not fetched again), 0 failed, 0 deferred.
```

Run it again at once and nothing is fetched: what the lake holds fresh is skipped. That is
also what makes it safe to run from a scheduler.

```text
$ pbi sync run default activity scan --lineage --days 3 --scan-interval 0.1
Data lake: ~/PowerBI/cache/lake
Targets: groups, apps, capacities, reports, datasets, dashboards, dataflows, scan, activity
  scan copies the contents of every workspace; with the scan options also data source details, dataset schemas and queries (DAX, Power Query) and the users of every item
  activity copies what each person did and when: e-mail addresses, IP addresses, devices

Stage 1: 11 unit(s) of groups, apps, capacities, reports, datasets, dashboards, dataflows, scan, activity
  · admin.datasets  fresh
  · admin.groups  fresh
  · admin.apps  fresh
  · admin.reports  fresh
  · admin.capacities  fresh
  · admin.dashboards  fresh
  · admin.dataflows  fresh
  ✓ admin.workspaces.modified?excludeInActiveWorkspaces=false&excludePersonalWorkspaces=false  250 workspaces
  ✓ admin.activityevents@2026-09-29  40 new events, day still open
  ✓ admin.activityevents@2026-09-30  39 new events, day still open
  ✓ admin.activityevents@2026-09-28  40 new events, complete

Stage 2: 3 unit(s) of scan
  ✓ admin.scan@ced635e0ae7a  100 workspaces
  ✓ admin.scan@f39af6f207db  50 workspaces
  ✓ admin.scan@d6d8c78938c6  100 workspaces

Finished in 0 seconds: 7 fetched, 7 fresh (not fetched again), 0 failed, 0 deferred.
```

### When is something fresh?

A stored answer is fresh when it is younger than a limit that depends on the target: 24
hours for the lists, scans and lists of users, 1 hour for the audit events of a day that is
not over, and 1 hour for what a user sees. `--max-age 6h` (or `30m`, `2d`) replaces the
limit. `--force` fetches everything again, and scans every workspace again; the one thing it
does not fetch again is a complete day of audit events, which cannot change.

### Quotas

Every request counts against the quota of its operation (see the
[table](lake.md#endpoints-and-quotas)), and the counts are kept between runs in
`~/.pbi_cli/quota.json`. A request waits for quota for at most `--wait` seconds (120 by
default, 0 to never wait). When it would have to wait longer, its unit is *deferred*: it is
not an error, the run goes on with the rest, and the next run continues with the deferred
units. A quota of 200 an hour means that 1,000 units take five runs, an hour apart; the plan
tells how long it would take.

If an administrator operation keeps answering `403 Forbidden` (three times in a row) the
token is probably not an administrator's, and the run stops asking for that operation.

## Audit events

The `activity` target keeps one log per UTC day, for the last `--days` days (28 by default,
which is all the API keeps), oldest day first because those are the ones about to be lost.

- A day is read page by page, and each page is stored, with the link to the next one,
  before that next page is asked for. A read that is interrupted continues from that link;
  if the API no longer accepts the link, the day is read again from the start.
- Events are told apart by their `Id`, so reading a day again adds only what is new.
- Today is not over: its window ends at the time of the run, and the day is read again by
  the next runs (when it is older than `--max-age`, 1 hour by default).
- Events arrive late, so a day is *sealed*, and never read again, a day after it ended.

The logs are in the lake as `endpoint=admin_activityevents/dt=<day>/part-NNNN.jsonl` (see
[Data lake](lake.md)), and `pbi lake show admin.activityevents --day 2026-09-28` prints one.

## Scans

The `scan` target scans the workspaces with the
[scanner API](https://learn.microsoft.com/en-us/rest/api/power-bi/admin/workspace-info-post-workspace-info):
100 workspaces per request, where scanning one by one would use up the 500 requests an hour
after 500 workspaces. First the list of workspaces is asked for (an intermediate result, not stored; the API
allows 30 of these requests an hour), then the workspaces are cut in batches of 100 and the
batches are scanned side by side, one thread each, up to `--workers`. Each batch is
started, polled every `--scan-interval` seconds (5 by default) and collected; each result
is stored in the lake as a *job*: `endpoint=admin_scan_result`, with one `params=` folder
per batch, and the workspaces, the options and the scan id in its manifest.

The options of the scan are the same as those of `pbi workspaces scan`: `--lineage`,
`--datasource-details`, `--dataset-schema`, `--dataset-expressions` and
`--get-artifact-users`. The schema and the expressions are returned only when the tenant
settings for detailed metadata are switched on
([Microsoft's instructions](https://learn.microsoft.com/en-us/power-bi/admin/service-admin-metadata-scanning-setup)).

**Incremental scans.** The first scan covers every workspace. The next one covers only the
workspaces that changed since the *start* of the last complete scan, less an hour to be
safe. "Complete" means that every batch was scanned: a scan that failed, or was held back by
a quota, is not a starting point, so nothing that changed is ever missed. The scans are only
continued from when they had the same options and covered the same workspaces
(`--exclude-personal`, `--exclude-inactive`): a scan without lineage does not tell what a
scan with lineage would have found. The API only tells what changed in the last 30 days,
so a longer gap means a full scan too. `--full-scan` (and `--force`) scan everything.

**Scans that do not finish.** A scan that was started and not collected, because the
token expired or the run was stopped, is remembered in the state. The next run continues
it; the API keeps a result for 24 hours. A scan that takes longer than `--scan-timeout`
(10 minutes) is reported as failed, but it may still finish, so the next run looks at it
again before starting another. A scan that failed is started again.

## Stopping, and continuing

A sync can be stopped at any time, and run again:

- **The token expired.** The run stops at once, and says how to continue. What is done is in
  the lake, and a scan that was started is in the state:

```text
$ pbi sync run report-users
Data lake: ~/PowerBI/cache/lake
Targets: reports, report-users
  report-users copies the people who can open each report, with their e-mail addresses

Stage 1: 1 unit(s) of reports
  · admin.reports  fresh

Stage 2: 12 unit(s) of report-users
  ✓ admin.reports.users?reportId=rep-0002  1 rows
  ✓ admin.reports.users?reportId=rep-0001  1 rows
  ✓ admin.reports.users?reportId=rep-0003  1 rows

Finished in 0 seconds: 3 fetched, 1 fresh (not fetched again), 0 failed, 0 deferred.
Error: Power BI rejected the token (401 Unauthorized) for admin.reports.users. Sign in again and store a fresh token with `pbi auth -t <token> -g admin`.
What is done is kept: after signing in, run `pbi sync run report-users` again to continue.
[exit status 1]
```

  Sign in again (`pbi auth`) and run the same command; it skips what is done.
- **A quota is used up.** The units that need it are deferred, the others go on. The run ends
  with `run the command again in about ...`.
- **A unit fails**, for example a report that cannot be read. It is reported, the run goes on,
  and the next run tries it again. The exit status is 1.
- **Ctrl-C** ends the run like an expired token does (exit status 130).

The exit status is 0 when everything that could be done was done, even if some units were
deferred, 1 when a unit failed or the token expired, and 130 when the run was interrupted.

## Status

`pbi sync status` shows how the last runs went, the units that failed or are held back, the
scans that were started and not collected, the quota used in the last hour, and what the lake
holds for each target. It reads the lake only, so it needs no token and no network.

```text
$ pbi sync status
Data lake: ~/PowerBI/cache/lake

Tenant: 0b6e7f5a-3c1d-4f7e-9a52-7d3c1e8b2f40
Last run: 2026-09-30 23:20 UTC (0 s ago): stopped: the token expired
  targets: reports, report-users
  3 fetched, 1 fresh, 0 failed, 0 deferred
  Power BI rejected the token (401 Unauthorized) for admin.reports.users. Sign in again and store a fresh token with `pbi auth -t <token> -g admin`.

Last complete scan: started 2026-09-30 23:20 UTC, options: lineage; the next one continues from there.

Quota left in the last hour (requests left/allowed)
OPERATION                  LEFT
admin.groups               49/50 h  14/15 min
admin.apps                 199/200 h
admin.reports              49/50 h  4/5 min
admin.datasets             49/50 h  4/5 min
admin.dashboards           49/50 h  4/5 min
admin.dataflows            199/200 h
admin.capacities           199/200 h
admin.reports.users        194/200 h
admin.workspaces.modified  29/30 h
admin.activityevents       194/200 h
admin.scan.start           497/500 h
admin.scan.status          9997/10000 h
admin.scan.result          497/500 h

What the lake holds
TARGET        OPERATION             STORED        NEWEST
groups        admin.groups          1 request(s)  0 s ago
apps          admin.apps            1 request(s)  0 s ago
capacities    admin.capacities      1 request(s)  0 s ago
reports       admin.reports         1 request(s)  0 s ago
datasets      admin.datasets        1 request(s)  0 s ago
dashboards    admin.dashboards      1 request(s)  0 s ago
dataflows     admin.dataflows       1 request(s)  0 s ago
scan          admin.scan.result     3 request(s)  0 s ago
report-users  admin.reports.users   3 request(s)  0 s ago
activity      admin.activityevents  3 day(s)      0 s ago
```

## On a schedule

Run a sync as often as you want the lake to be up to date; nothing is fetched twice, so a
run that finds nothing to do costs nothing. A run needs a valid token. pbi_cli stores the
token you give it (see [Authentication](auth.md)) and it lasts about an hour, so a scheduled
run works when something stores a fresh token just before it. Some advice:

- Use a `--max-age` a little below the interval of the schedule (`--max-age 20h` for a
  nightly run), or what was fetched 23 hours and 55 minutes ago is still fresh and skipped.
- Audit events are the data that is lost if you do not fetch it: they are kept 28 days.
  A nightly `pbi sync run default activity --days 7` keeps them all.
- A run that stops because of an expired token, or a quota, is not a problem for the
  schedule: the next one continues.

## What a sync remembers

The data is in the lake, and the lake tells how old it is. What the lake cannot tell is kept
in a small document, `tenant=<tenant>/_state/sync.json`: the latest runs and how they ended,
the units that failed or were deferred, the scans that were started and not collected, and
the start of the last complete scan. It is safe to delete: the next run then fetches what is
not fresh, and scans every workspace.

Keep one sync per tenant running at a time: two at once would fetch the same things twice.

## Using it from Python

```python
from pbi_cli.core.store import LakeStore
from pbi_cli.core.sync.engine import SyncEngine
from pbi_cli.core.sync.plan import SyncOptions

engine = SyncEngine(lambda scope: client_for(scope), LakeStore("~/PowerBI/cache/lake"))
options = SyncOptions(targets=("default", "activity"), days=7)

plan = engine.plan(options)       # reads the lake only
report = engine.run(options)      # report.status, report.counts, report.failures ...
```

`client_for` returns a `PowerBIClient` (see [Data lake](lake.md#using-it-from-python)) for
`Scope.ADMIN` or `Scope.USER`. The reference of the modules is under
[References](references/core/sync/engine.md).
