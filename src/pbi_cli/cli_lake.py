"""``pbi lake``: look into the data lake that the commands fill, and tidy it.

These commands read the lake only (no token, no network), so they work offline and with an
expired token. The lake is the ``lake`` folder in the cache folder
(``pbi config set-cache-folder``); see ``docs/lake.md`` for its layout.
"""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional, Sequence

import typer

from pbi_cli.cli_support import (
    BackupOption,
    BackupToOption,
    LakeOption,
    back_up,
    command,
    format_age,
    new_app,
    print_table,
)
from pbi_cli.cli_sync import fetch_users
from pbi_cli.config import PBIConfig
from pbi_cli.core.accessreport import (
    ACCESS_KINDS,
    COLUMNS,
    AccessReport,
    access_of,
    access_report,
    resolve_workspaces,
)
from pbi_cli.core.backup import SAFETY_SUFFIX, backup_lake, plan_restore, restore_lake
from pbi_cli.core.catalog import Catalog
from pbi_cli.core.lakesql import describe, load_lake
from pbi_cli.core.publish import EXCLUDABLE, PublishPlan, plan_publish, publish
from pbi_cli.core.registry import get_endpoint
from pbi_cli.core.store import LakeStore, ParameterSet, Snapshot, safe_name
from pbi_cli.errors import PBIError
from pbi_cli.session import as_path, lake_hint, lake_path, resolve_lake
from pbi_cli.tui import exporting

lake_app = new_app("lake")


@lake_app.callback(invoke_without_command=True)
def lake_group(ctx: typer.Context):
    """Browse the data lake of fetched API results"""
    if ctx.invoked_subcommand is None:
        typer.echo("Use pbi lake --help for help.")


# -- helpers -----------------------------------------------------------------------


def _store(location: Optional[str] = None) -> LakeStore:
    """The lake to look at: the one given with ``--lake``, else that of the cache folder.

    Only the lake of the cache folder can be written (and cleaned up with ``prune``).
    """
    opened = resolve_lake(location)
    if opened is None:
        raise PBIError(f"There is no data lake yet. {lake_hint()}")
    return opened.store


def _tenants(store: LakeStore, wanted: Optional[str]) -> List[str]:
    """The tenants to look at: the wanted one, or all that have data."""
    found = store.tenants()
    if wanted is None:
        return found
    name = safe_name(wanted)
    if name not in found:
        raise PBIError(
            f"The lake holds no data of tenant '{wanted}'. "
            f"Tenants in the lake: {', '.join(found) or 'none'}."
        )
    return [name]


def _one_tenant(store: LakeStore, wanted: Optional[str]) -> str:
    tenants = _tenants(store, wanted)
    if not tenants:
        raise PBIError(
            "The lake is empty. Commands such as `pbi workspaces list` fill it."
        )
    if len(tenants) > 1:
        raise PBIError(
            f"The lake holds several tenants ({', '.join(tenants)}): "
            "choose one with --tenant."
        )
    return tenants[0]


def _size(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def _params_text(params: Dict[str, str]) -> str:
    return " ".join(f"{key}={value}" for key, value in params.items()) or "-"


def _matches(endpoint_id: str, given: str) -> bool:
    return given in (endpoint_id, safe_name(endpoint_id))


def _select(
    sets: List[ParameterSet], endpoint_id: str, pairs: Sequence[str], ref: Optional[str]
):
    """The stored requests of an endpoint that fit ``--ref`` and ``-p KEY=VALUE``."""
    wanted: Dict[str, str] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key:
            raise typer.BadParameter(f"'{pair}' is not KEY=VALUE", param_hint="--param")
        wanted[key] = value

    try:
        multi = get_endpoint(endpoint_id).multi_value
    except PBIError:
        multi = ()  # an endpoint of a newer pbi-cli: compare the text as it is

    def same(key: str, stored: Optional[str], given: str) -> bool:
        if key in multi:  # an unordered list: $expand=b,a is $expand=a,b
            return stored is not None and sorted(
                p.strip() for p in stored.split(",")
            ) == sorted(p.strip() for p in given.split(","))
        return stored == given

    return [
        pset
        for pset in sets
        if (ref is None or pset.hash.startswith(ref))
        and all(same(key, pset.params.get(key), value) for key, value in wanted.items())
    ]


# -- commands ----------------------------------------------------------------------


@command(lake_app, "ls")
def lake_ls(
    endpoint: Annotated[
        Optional[str],
        typer.Option(
            "--endpoint", "-e", help="Only this endpoint, for example admin.groups"
        ),
    ] = None,
    tenant: Annotated[
        Optional[str],
        typer.Option("--tenant", "-t", help="Only this tenant (default: all)"),
    ] = None,
    all_versions: Annotated[
        bool,
        typer.Option(
            "--all-versions",
            help="List every stored version, not only the newest of each request",
        ),
    ] = False,
    lake: LakeOption = None,
):
    """List what the data lake holds

    One line per request (an endpoint with one set of parameters) shows when it was
    fetched, how many rows it has and how many versions are stored. Use REF with
    `pbi lake show`.

    ```
    # Everything in the lake
    pbi lake ls

    # Only the workspace lists, with every stored version
    pbi lake ls -e admin.groups --all-versions

    # What someone shared, without any token
    pbi lake ls --lake s3://my-bucket/pbi-lake
    ```
    """
    store = _store(lake)
    typer.echo(f"Data lake: {store.root}")
    tenants = _tenants(store, tenant)
    if not tenants:
        typer.echo("The lake is empty. Commands such as `pbi workspaces list` fill it.")
        return

    now = datetime.now(timezone.utc)
    header = (
        ["ENDPOINT", "REF", "VERSION", "FETCHED (UTC)", "AGE", "ROWS", "SIZE"]
        if all_versions
        else ["ENDPOINT", "REF", "FETCHED (UTC)", "AGE", "ROWS", "SIZE", "VERSIONS"]
    ) + ["PARAMETERS"]

    shown = 0
    for tenant_name in tenants:
        rows: List[List[Any]] = []
        for endpoint_id in store.endpoints(tenant_name):
            if endpoint and not _matches(endpoint_id, endpoint):
                continue
            for pset in store.parameter_sets(tenant_name, endpoint_id):
                versions = store.versions(tenant_name, endpoint_id, pset.params)
                for snapshot in versions if all_versions else versions[:1]:
                    manifest = snapshot.manifest
                    row = [
                        endpoint_id,
                        pset.hash,
                        *([snapshot.version] if all_versions else []),
                        f"{snapshot.fetched_at:%Y-%m-%d %H:%M}",
                        format_age(snapshot.age(now)),
                        "-" if manifest.get("rows") is None else manifest["rows"],
                        _size(int(manifest.get("bytes", 0))),
                        *([] if all_versions else [len(versions)]),
                        _params_text(pset.params),
                    ]
                    rows.append(row)
            days = store.event_days(tenant_name, endpoint_id)
            for stored in days if all_versions else days[:1]:
                state = "sealed" if stored.sealed else "open"
                updated = stored.manifest.get("updated_at")
                written = datetime.fromisoformat(updated) if updated else None
                rows.append(
                    [
                        endpoint_id,
                        "events",
                        *([stored.day.isoformat()] if all_versions else []),
                        f"{written:%Y-%m-%d %H:%M}" if written else "-",
                        format_age(now - written) if written else "-",
                        stored.rows if all_versions else sum(d.rows for d in days),
                        "-",
                        *([] if all_versions else [f"{len(days)} day(s)"]),
                        state if all_versions else f"newest day {stored.day} ({state})",
                    ]
                )
        if not rows:
            continue
        shown += len(rows)
        typer.echo(f"\nTenant: {tenant_name}")
        print_table(header, rows)
    if not shown:
        typer.secho("Nothing matches.", fg="yellow")


@command(lake_app, "show")
def lake_show(
    endpoint: Annotated[
        str,
        typer.Argument(
            metavar="ENDPOINT",
            help="Endpoint id, for example admin.groups (see pbi lake ls)",
        ),
    ],
    param: Annotated[
        List[str],
        typer.Option(
            "--param",
            "-p",
            metavar="KEY=VALUE",
            help="A parameter of the stored request, to tell requests apart (repeatable)",
        ),
    ] = [],
    ref: Annotated[
        Optional[str],
        typer.Option("--ref", help="The REF of the request, as listed by pbi lake ls"),
    ] = None,
    tenant: Annotated[
        Optional[str],
        typer.Option(
            "--tenant", "-t", help="Tenant (only needed if the lake holds several)"
        ),
    ] = None,
    version: Annotated[
        Optional[str],
        typer.Option("--version", "-v", help="Version to show (default: the newest)"),
    ] = None,
    day: Annotated[
        Optional[str],
        typer.Option(
            "--day", "-d", help="For event logs: the UTC day to show (YYYY-MM-DD)"
        ),
    ] = None,
    manifest: Annotated[
        bool,
        typer.Option(
            "--manifest",
            "-m",
            help="Show the manifest (what was asked, when, checksum) instead of the data",
        ),
    ] = False,
    lake: LakeOption = None,
):
    """Print a stored response (or its manifest) as JSON

    When an endpoint was fetched with several sets of parameters, tell them apart with
    `-p KEY=VALUE` (as shown under PARAMETERS by `pbi lake ls`) or `--ref`. Event logs
    are kept per UTC day: choose the day with `--day`; events are printed one per line.

    ```
    # The newest workspace list (there is only one kind of request)
    pbi lake show admin.groups

    # The request made with --top 50
    pbi lake show admin.groups -p '$top=50'

    # Where it came from: parameters, time, checksum
    pbi lake show admin.groups --manifest

    # The audit events of one day
    pbi lake show admin.activityevents --day 2026-09-29
    ```
    """
    store = _store(lake)
    tenant_name = _one_tenant(store, tenant)
    known = store.endpoints(tenant_name)
    endpoint_id = next((e for e in known if _matches(e, endpoint)), None)
    if endpoint_id is None:
        raise PBIError(
            f"The lake holds nothing for '{endpoint}' (tenant {tenant_name}). "
            f"It holds: {', '.join(known) or 'nothing'}."
        )

    days = store.event_days(tenant_name, endpoint_id)
    if days:
        if day is None:
            listed = ", ".join(d.day.isoformat() for d in days[:10])
            raise PBIError(
                f"{endpoint_id} is an event log with one file set per UTC day: choose "
                f"one with --day YYYY-MM-DD. Stored days: {listed}"
                + (" ..." if len(days) > 10 else "")
            )
        try:
            chosen_day = date.fromisoformat(day)
        except ValueError:
            raise typer.BadParameter(
                f"'{day}' is not a date (YYYY-MM-DD)", param_hint="--day"
            ) from None
        stored = store.event_day(tenant_name, endpoint_id, chosen_day)
        if stored is None:
            raise PBIError(f"No events of {day} are stored for {endpoint_id}.")
        if manifest:
            typer.echo(json.dumps(stored.manifest, indent=2))
        else:
            for event in store.read_events(tenant_name, endpoint_id, chosen_day):
                typer.echo(json.dumps(event))
        return

    sets = store.parameter_sets(tenant_name, endpoint_id)
    candidates = _select(sets, endpoint_id, param, ref)
    if not candidates:
        stored_requests = "\n".join(
            f"  {pset.hash}  {_params_text(pset.params)}" for pset in sets
        )
        raise PBIError(
            f"No stored request of {endpoint_id} matches. Stored requests:\n{stored_requests}"
        )
    if len(candidates) > 1:
        listed = "\n".join(
            f"  {pset.hash}  {_params_text(pset.params)}" for pset in candidates
        )
        raise PBIError(
            f"{len(candidates)} stored requests of {endpoint_id} match; choose one with "
            f"-p KEY=VALUE or --ref:\n{listed}"
        )

    chosen: ParameterSet = candidates[0]
    snapshots = store.versions(tenant_name, endpoint_id, chosen.params)
    snapshot: Optional[Snapshot] = snapshots[0] if snapshots else None
    if version is not None:
        snapshot = next((s for s in snapshots if s.version == version), None) or next(
            (s for s in snapshots if s.version.startswith(version)), None
        )
        if snapshot is None:
            listed = ", ".join(s.version for s in snapshots[:5])
            raise PBIError(
                f"No version '{version}' of this request. Newest versions: {listed}"
            )
    assert snapshot is not None
    typer.echo(json.dumps(snapshot.manifest if manifest else snapshot.load(), indent=2))


@command(lake_app, "prune")
def lake_prune(
    keep: Annotated[
        int,
        typer.Option(
            "--keep", min=1, help="Versions of each request to keep (the newest)"
        ),
    ] = 1,
    endpoint: Annotated[
        Optional[str],
        typer.Option("--endpoint", "-e", help="Only this endpoint"),
    ] = None,
    tenant: Annotated[
        Optional[str],
        typer.Option("--tenant", "-t", help="Only this tenant (default: all)"),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            prompt="Delete the older versions from the data lake?",
            help="Confirm the action without prompting.",
        ),
    ] = False,
    lake: LakeOption = None,
    backup: BackupOption = False,
    backup_to: BackupToOption = None,
):
    """Delete old versions, keeping the newest of each request

    Every fetch adds a version and nothing is overwritten, so a lake grows. Pruning keeps
    the newest versions of each request (by default only the newest). Event logs are kept.

    ```
    # Keep only the newest version of every request
    pbi lake prune

    # Keep the last three workspace lists
    pbi lake prune -e admin.groups --keep 3

    # The same, with a copy of the lake taken first (a lake in S3, say)
    pbi lake prune --backup
    ```

    Only the lake of the cache folder can be pruned: a lake given with `--lake`, and a
    published lake, are read-only.
    """
    if not yes:
        raise typer.Abort()

    store = _store(lake)
    if not store.writable:
        raise PBIError(store.why_read_only())
    back_up(store.root, backup, backup_to, "pruning")
    removed = 0
    for tenant_name in _tenants(store, tenant):
        removed += store.prune(keep=keep, tenant=tenant_name, endpoint_id=endpoint)
    typer.secho(
        f"✓ Deleted {removed} old version(s), kept the newest {keep} of each request",
        fg="green",
    )


def _show_publish_plan(plan: PublishPlan) -> None:
    typer.echo(f"From: {plan.source.root}")
    before = plan.before
    where = (
        f"(published by {before.published_by} on {before.published_at:%Y-%m-%d %H:%M} UTC)"
        if before
        else "(empty)"
    )
    typer.echo(f"To:   {plan.destination.root} {where}")
    typer.echo(f"Tenants: {', '.join(plan.tenants)}\n")
    rows = []
    for found in plan.summaries:
        if not found.files:
            continue
        name = found.category.name + (" (left out)" if found.excluded else "")
        note = ("⚠ " if found.category.excludable else "") + found.category.sensitive
        rows.append((name, str(found.files), _size(found.size), f"holds {note}"))
    print_table(("CATEGORY", "FILES", "SIZE", ""), rows)
    kind = "every version" if plan.history else "the newest version of each request"
    typer.echo(
        f"\nTo publish: {plan.files} file(s), {_size(plan.size)}; {kind}, every day of "
        "events."
    )
    if plan.prune:
        typer.echo("Older versions in the destination will be deleted afterwards.")
    typer.echo(
        "Whoever opens the published lake sees all of it. Leave a category out with "
        "--exclude."
    )


@command(lake_app, "publish")
def lake_publish(
    destination: Annotated[
        str,
        typer.Argument(
            metavar="DESTINATION",
            help="Where to publish: an empty folder, or a URL such as s3://bucket/folder",
        ),
    ],
    tenant: Annotated[
        Optional[str],
        typer.Option("--tenant", "-t", help="Only this tenant (default: all)"),
    ] = None,
    exclude: Annotated[
        List[str],
        typer.Option(
            "--exclude",
            "-x",
            metavar="CATEGORY",
            help=("Leave a category out: " + ", ".join(EXCLUDABLE) + " (repeatable)"),
        ),
    ] = [],
    history: Annotated[
        bool,
        typer.Option(
            "--history",
            help="Publish every stored version, not only the newest of each request",
        ),
    ] = False,
    prune: Annotated[
        bool,
        typer.Option(
            "--prune",
            help="Afterwards delete the versions in DESTINATION that are not the newest",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Show what would be published and write nothing"
        ),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Publish without asking")] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Publish over a lake that someone else published"),
    ] = False,
    lake: LakeOption = None,
    backup: BackupOption = False,
    backup_to: BackupToOption = None,
):
    """Publish the lake: a complete copy that others can open without an account

    The copy has the layout of the lake, so `pbi tui --lake DESTINATION` and
    `pbi lake ls --lake DESTINATION` read it as it is. It holds the newest version of every
    request, every day of audit events, the scans and how the last syncs went. It is marked
    as published (`publish.json`), and from then on nothing but a later publish by the same
    person writes to it: anyone who points a sync at it is refused.

    The publish shows what it would copy, with what each category holds, and asks before it
    writes. A category that holds personal data or queries can be left out. Nothing that is
    in DESTINATION is overwritten (a version never changes), and DESTINATION must be empty
    or the place of an earlier publish. The lake that is published is the lake of the cache
    folder, or the one given with `--lake`.

    ```
    # What would be published, without writing anything
    pbi lake publish s3://my-bucket/pbi-lake --dry-run

    # Publish, leaving out the audit events and who has access
    pbi lake publish s3://my-bucket/pbi-lake --exclude activity --exclude users

    # In a nightly job, after `pbi sync run`
    pbi lake publish s3://my-bucket/pbi-lake --yes

    # Copy the earlier publish first, in case this one stops half way
    pbi lake publish s3://my-bucket/pbi-lake --backup
    ```
    """
    opened = resolve_lake(lake)
    if opened is None:
        raise PBIError(f"There is no data lake yet. {lake_hint()}")
    try:
        target = LakeStore(as_path(destination))
    except Exception as error:  # a URL whose client library is missing, for example
        raise PBIError(
            f"Cannot use {destination} as the destination: {error}"
        ) from error
    plan = plan_publish(
        opened.store,
        target,
        tenants=[tenant] if tenant else None,
        exclude=exclude,
        history=history,
        prune=prune,
        force=force,
    )
    _show_publish_plan(plan)
    if dry_run:
        typer.echo("\nDry run: nothing was written.")
        return
    if not yes and not typer.confirm(f"\nPublish this to {target.root}?"):
        raise typer.Abort()
    if plan.before is not None or backup or backup_to is not None:
        # an earlier publish is there: what it holds is copied before it is updated
        back_up(target.root, backup, backup_to, "the publish")
    result = publish(plan)
    typer.secho(
        f"✓ Published to {target.root}: {result.copied} file(s) copied "
        f"({_size(result.size)}), {result.skipped} already there"
        + (f", {result.pruned} older version(s) deleted" if plan.prune else ""),
        fg="green",
    )
    typer.echo(f"Others open it with: pbi tui --lake {target.root}")


@command(lake_app, "backup")
def lake_backup(
    to: Annotated[
        Optional[str],
        typer.Option(
            "--to",
            help=(
                "Where the copy goes: an empty folder or URL (default: next to the lake, "
                "as <lake>-backup-<UTC time>)"
            ),
        ),
    ] = None,
    lake: LakeOption = None,
):
    """Copy a whole data lake, to have a way back

    A local lake replaces each file in one step, so a file is never half written. S3 has no
    such step: a sync, a prune or a publish that stops half way leaves some objects new and
    some old. Take a copy before; `pbi sync run`, `pbi lake prune` and `pbi lake publish`
    also take one with `--backup`.

    The copy has the layout of a lake: `pbi tui --lake COPY` opens it. Its `backup.json` is
    written last; a copy without it is incomplete. Between two places of the same cloud the
    objects are copied by the cloud, not through this machine.

    ```
    # The lake of the cache folder, next to it
    pbi lake backup

    # A lake in S3, to a place of your choice
    pbi lake backup --lake s3://my-bucket/pbi-lake --to s3://my-bucket/backups/2026-10-30
    ```

    To go back, copy the backup over the lake, for example with
    `aws s3 sync s3://my-bucket/pbi-lake-backup-... s3://my-bucket/pbi-lake --delete`.
    """
    opened = resolve_lake(lake)
    if opened is None:
        raise PBIError(f"There is no data lake yet. {lake_hint()}")
    root = opened.store.root
    typer.echo(f"Backing up {root} ...")
    done = backup_lake(
        root,
        None if to is None else as_path(to),
        on_file=lambda n: n % 500 == 0 and typer.echo(f"  {n} file(s) ..."),
    )
    if done is None:
        typer.echo("The lake holds nothing yet: there is nothing to back up.")
        return
    typer.secho(
        f"✓ Backed up {done.files} file(s) ({_size(done.size)}) to {done.destination}",
        fg="green",
    )
    typer.echo(f"Open it with: pbi tui --lake {done.destination}")


FetchOption = Annotated[
    bool,
    typer.Option(
        "--fetch",
        help=(
            "Fetch the users that the lake lacks first (as `pbi sync run report-users` and "
            "its kin do), after showing what it costs against the quotas. Needs an admin "
            "account, and writes into the lake of the cache folder"
        ),
    ),
]
FetchYesOption = Annotated[
    bool, typer.Option("--yes", help="With --fetch: do not ask before fetching")
]


def _fetch_users(
    store: LakeStore,
    catalog: Catalog,
    kinds: Sequence[str],
    workspaces: Optional[Sequence[str]],
    yes: bool,
) -> None:
    """Fetch the users that the lake lacks of these kinds (for ``--fetch``).

    :raises PBIError: for a lake that is only read, or a workspace that is not in the lake
    """
    if not store.writable:
        raise PBIError(
            "--fetch writes into the lake of your cache folder: "
            + (store.why_read_only() or "this lake is only read.")
        )
    try:
        ids = resolve_workspaces(catalog, workspaces) or set()
    except ValueError as error:
        raise PBIError(str(error)) from error
    fetch_users(kinds, sorted(ids), yes=yes)


#: The columns of ``pbi lake users`` on the terminal; a file has them all.
_SHOWN = (0, 1, 3, 5, 6, 7)


@command(lake_app, "users")
def lake_users(
    kind: Annotated[
        str,
        typer.Argument(
            metavar="KIND",
            help=f"Whose users: {', '.join(ACCESS_KINDS)}",
        ),
    ],
    output: Annotated[
        Optional[str],
        typer.Option(
            "--output",
            "-o",
            help=(
                "Save to this file instead of printing; the format follows its ending: "
                ".csv, .xlsx, .json, .md or .tsv"
            ),
        ),
    ] = None,
    fmt: Annotated[
        Optional[str],
        typer.Option(
            "--format",
            "-f",
            help="csv, xlsx, json, md or tsv (default: from the file name, else csv)",
        ),
    ] = None,
    workspace: Annotated[
        Optional[List[str]],
        typer.Option(
            "--workspace",
            "-w",
            help="Only the items of this workspace, by id or name (repeat for more)",
        ),
    ] = None,
    tenant: Annotated[
        Optional[str],
        typer.Option(
            "--tenant", "-t", help="Tenant (only needed if the lake holds several)"
        ),
    ] = None,
    lake: LakeOption = None,
    fetch: FetchOption = False,
    yes: FetchYesOption = False,
):
    """Who has access to every item of a kind, as a table or a file

    One row for each person and item: every person of every report, of every workspace,
    of every app, and so on, from what the lake holds (no token, no network). Items whose
    users the lake does not hold are listed at the end, with what fetches them.

    ```
    # Every person of every report, on the terminal
    pbi lake users report

    # ... in a spreadsheet
    pbi lake users report -o report-users.xlsx

    # Everyone with access to the workspaces of one team
    pbi lake users workspace -w Finance -o finance.csv
    ```

    The users come from `pbi sync run report-users`, `app-users` or a scan with
    `--get-artifact-users`; `pbi lake ls` shows what the lake holds. `--fetch` fetches the
    ones the lake lacks first (it shows the cost and asks, `--yes` skips that). The terminal
    UI exports the same for the item or the workspace that you are looking at (`x`).
    """
    if kind not in ACCESS_KINDS:
        raise typer.BadParameter(
            f"'{kind}' is not a kind. Choose one of: {', '.join(ACCESS_KINDS)}.",
            param_hint="KIND",
        )
    store = _store(lake)
    tenant_name = _one_tenant(store, tenant)
    catalog = Catalog(store, tenant_name)
    try:
        found = access_report(catalog, kind, workspace)
        if fetch and (found.missing or not found.items):
            _fetch_users(store, catalog, [kind], workspace, yes)
            catalog = Catalog(store, tenant_name)  # reads what was just fetched
            found = access_report(catalog, kind, workspace)
    except ValueError as error:
        raise PBIError(str(error)) from error

    title = f"Users of {kind}s" if kind != "workspace" else "Users of workspaces"
    if not found.rows:
        raise PBIError(_nobody(kind, found))
    _deliver(
        found,
        title,
        _SHOWN,
        output,
        fmt,
        saved=f"{len(found.rows)} row(s) for {found.items} {kind}(s)",
        summary=(
            f"{len(found.rows)} row(s): {found.people} different people, "
            f"{found.items - len(found.missing)} of {found.items} {kind}(s) with users."
        ),
    )


def _deliver(
    found: AccessReport,
    title: str,
    shown: Sequence[int],
    output: Optional[str],
    fmt: Optional[str],
    *,
    saved: str,
    summary: str,
) -> None:
    """Print an access report as a table, or save it as a file; then name the items whose
    users the lake does not hold.

    :param shown: the columns (indexes into `COLUMNS`) that the terminal shows
    :param saved: what the file holds, for the message
    :param summary: the line under the table
    """
    table = exporting.Exportable(title, COLUMNS, found.table)
    if output is not None:
        chosen = fmt or Path(output).suffix.lstrip(".").lower() or exporting.CSV
        if chosen not in exporting.TABLE_FORMATS:
            raise PBIError(
                f"Cannot write '{chosen}'. Formats: {', '.join(exporting.TABLE_FORMATS)}."
            )
        path = exporting.write(table, chosen, output)
        typer.secho(f"✓ Saved {saved} to {path}", fg="green")
    else:
        print_table(
            [COLUMNS[i] for i in shown],
            [[row[i] for i in shown] for row in table.cells],
        )
        typer.echo(f"\n{summary}")
    if found.missing:
        listed = ", ".join(found.missing[:5])
        more = f" and {len(found.missing) - 5} more" if len(found.missing) > 5 else ""
        typer.secho(
            f"{len(found.missing)} of {found.items} item(s) have no users in the lake, "
            f"so this may be incomplete: {listed}{more}.",
            fg="yellow",
        )


def _nobody(kind: str, found: AccessReport) -> str:
    count = found.items
    if not count:
        return f"The lake holds no {kind}s. Fetch them with `pbi sync run`."
    hint = {
        "workspace": "`pbi sync run scan --get-artifact-users`",
        "app": "`pbi sync run app-users`",
    }.get(kind, "`pbi sync run report-users` or a scan with `--get-artifact-users`")
    return f"The lake holds none of the users of its {count} {kind}(s). Fetch them with {hint}."


#: The columns of ``pbi lake access`` on the terminal, without and with the person.
_SHOWN_ITEMS = (0, 1, 3, 7, 9)
_SHOWN_PEOPLE = (0, 1, 3, 5, 6, 7)


@command(lake_app, "access")
def lake_access(
    who: Annotated[
        str,
        typer.Argument(
            metavar="USER",
            help="An e-mail address or a name (ignoring case)",
        ),
    ],
    kind: Annotated[
        Optional[List[str]],
        typer.Option(
            "--kind",
            "-k",
            help=f"Only this kind (repeat for more): {', '.join(ACCESS_KINDS)}",
        ),
    ] = None,
    workspace: Annotated[
        Optional[List[str]],
        typer.Option(
            "--workspace",
            "-w",
            help="Only the items of this workspace, by id or name (repeat for more)",
        ),
    ] = None,
    partial: Annotated[
        bool,
        typer.Option(
            "--partial",
            help="Match a part of the e-mail address or name (for example a domain)",
        ),
    ] = False,
    output: Annotated[
        Optional[str],
        typer.Option(
            "--output",
            "-o",
            help="Save to this file instead of printing: .csv, .xlsx, .json, .md or .tsv",
        ),
    ] = None,
    fmt: Annotated[
        Optional[str],
        typer.Option(
            "--format",
            "-f",
            help="csv, xlsx, json, md or tsv (default: from the file name, else csv)",
        ),
    ] = None,
    tenant: Annotated[
        Optional[str],
        typer.Option(
            "--tenant", "-t", help="Tenant (only needed if the lake holds several)"
        ),
    ] = None,
    lake: LakeOption = None,
    fetch: FetchOption = False,
    yes: FetchYesOption = False,
):
    """What one person has access to: workspaces, reports, datasets, apps ...

    The other way round from `pbi lake users`: it looks through the users of every item the
    lake holds, and lists the ones where the person is. Nothing is sent to Power BI.

    ```
    # Everything that one person can open
    pbi lake access ann@example.com

    # ... only reports and dashboards, in a spreadsheet
    pbi lake access ann@example.com -k report -k dashboard -o ann.xlsx

    # Everybody of one domain
    pbi lake access --partial @contractor.com -o contractors.csv
    ```

    The answer is as complete as the lake: items whose users the lake does not hold are
    named under the table (fetch them with `pbi sync run report-users`, `app-users` or a
    scan with `--get-artifact-users`, or let `--fetch` do it: it shows the cost against the
    quotas and asks first, `--yes` skips that; every kind of item costs a request each, so
    narrow it with `-k` and `-w`). Access through a group shows as the group, because
    the lake does not hold who is in it. For the answer of Power BI itself, with an admin
    account, use `pbi users user-access -u <user id>`.
    """
    store = _store(lake)
    tenant_name = _one_tenant(store, tenant)
    catalog = Catalog(store, tenant_name)
    try:
        found = access_of(
            catalog, who, kinds=kind, workspaces=workspace, partial=partial
        )
        if fetch and (found.missing or not found.items):
            _fetch_users(store, catalog, kind or ACCESS_KINDS, workspace, yes)
            catalog = Catalog(store, tenant_name)  # reads what was just fetched
            found = access_of(
                catalog, who, kinds=kind, workspaces=workspace, partial=partial
            )
    except ValueError as error:
        raise PBIError(str(error)) from error
    if not found.rows:
        held = found.items - len(found.missing)
        raise PBIError(
            f"Nothing in the lake gives '{who}' access: {held} of {found.items} item(s) "
            f"have their users in the lake and none lists them"
            + (
                f"; {len(found.missing)} have no users in the lake (e.g. "
                f"{found.missing[0]})."
                if found.missing
                else "."
            )
            + (" Try --partial for a part of a name." if not partial else "")
        )
    places = {r.workspace_id or r.item_id for r in found.rows if r.kind != "app"}
    _deliver(
        found,
        f"Access of {who}",
        _SHOWN_PEOPLE if partial else _SHOWN_ITEMS,
        output,
        fmt,
        saved=f"{len(found.rows)} row(s) for {who}",
        summary=(
            f"{len(found.rows)} item(s) in {len(places)} workspace(s) for '{who}'"
            + (f" ({found.people} different people)" if partial else "")
            + "."
        ),
    )


@command(lake_app, "duckdb")
def lake_duckdb(
    database: Annotated[
        Path,
        typer.Argument(
            metavar="DATABASE",
            help="The DuckDB file to make (an existing one is replaced)",
        ),
    ] = Path("pbi-lake.duckdb"),
    tenant: Annotated[
        Optional[str],
        typer.Option("--tenant", "-t", help="Only this tenant (default: all)"),
    ] = None,
    lake: LakeOption = None,
):
    """Load the lake into a DuckDB database, to question it with SQL

    Makes a table of each list (`workspaces`, `reports`, `datasets`, ...), of who has access
    to what (`access`), of what a scan found (`scan_items`, `scan_edges` for the lineage,
    `scan_datasources`, `scan_tables`, `scan_columns`, `scan_measures`), of the audit events
    (`events`), and of the requests the lake holds (`requests`). Each table has the ids that
    join it to the others. Needs `pip install "pbi-cli[duckdb]"`. The database is a copy of
    the newest answers: load again after a sync. Reads the lake only.

    ```
    pbi lake duckdb
    duckdb pbi-lake.duckdb "select workspace, report, dataset from report_overview"

    # Who can open a report that reads from one server?
    duckdb pbi-lake.duckdb "
      select distinct a.email, r.report
      from scan_datasources s
      join scan_edges e on e.to_id = s.instance_id and e.to_kind = 'datasource'
      join scan_edges b on b.to_id = e.from_id and b.from_kind = 'report'
      join report_overview r on r.report_id = b.from_id
      join access a on a.item_id = r.report_id and a.kind = 'report'
      where s.server = 'sql.example'"
    ```

    See the data model in the documentation of the data lake.
    """
    store = _store(lake)
    typer.echo(f"Loading {store.root} ...")
    con = load_lake(store, str(database), tenant)
    try:
        tables = describe(con)
    finally:
        con.close()
    for name, count in tables:
        typer.echo(f"  {name:<18} {count} row(s)")
    typer.secho(f"✓ Wrote {database} ({len(tables)} tables and views)", fg="green")
    typer.echo(f"Open it with: duckdb {database}")


@command(lake_app, "restore")
def lake_restore(
    backup: Annotated[
        str,
        typer.Argument(
            metavar="BACKUP",
            help="The backup made by `pbi lake backup`: a folder or a URL such as s3://...",
        ),
    ],
    yes: Annotated[bool, typer.Option("--yes", help="Restore without asking")] = False,
    no_safety_copy: Annotated[
        bool,
        typer.Option(
            "--no-safety-copy",
            help="Do not copy the lake as it is now before replacing it",
        ),
    ] = False,
    keep_extra: Annotated[
        bool,
        typer.Option(
            "--keep-extra",
            help="Keep the files of the lake that the backup does not have",
        ),
    ] = False,
    allow_incomplete: Annotated[
        bool,
        typer.Option(
            "--allow-incomplete",
            help="Also restore a copy that has no backup.json (it stopped half way)",
        ),
    ] = False,
):
    """Put a backup back over the data lake of the cache folder

    The lake becomes what the backup holds: the files of the backup are copied back, and the
    files that it does not have are deleted (`--keep-extra` keeps them). Everything is copied
    before anything is deleted, so a restore that stops half way has lost nothing; run it
    again. Unless you say `--no-safety-copy`, the lake as it is now is copied first, next to it
    as `<lake>-before-restore-<time>`, so that a restore can be undone too. A backup that has
    no `backup.json` stopped before it was complete and is refused.

    ```
    pbi lake restore s3://my-bucket/pbi-lake-backup-20261030T224000Z
    pbi lake restore ~/PowerBI/cache/lake-backup-20261030T224000Z --yes
    ```

    Close the terminal UI and stop any sync first: nothing should write to the lake while it
    is replaced. Only the lake of the cache folder can be restored; a published lake is
    protected.
    """
    work = lake_path()
    if work is None:
        raise PBIError(f"There is no data lake yet. {lake_hint()}")
    store = LakeStore(work)
    if not store.writable:
        raise PBIError(store.why_read_only())
    try:
        source = as_path(backup)
    except Exception as error:
        raise PBIError(f"Cannot use {backup} as the backup: {error}") from error
    plan = plan_restore(
        source, work, keep_extra=keep_extra, allow_incomplete=allow_incomplete
    )
    when = f", made {plan.made_at[:16].replace('T', ' ')} UTC" if plan.made_at else ""
    typer.echo(f"Backup: {plan.backup}{when}")
    if plan.source:
        typer.echo(f"  of:   {plan.source}")
    typer.echo(f"Lake:   {plan.lake}")
    typer.echo(
        f"\nTo restore: copy {len(plan.copy)} file(s) ({_size(plan.size)})"
        + (
            f", delete {len(plan.delete)} file(s) that the backup does not have"
            if plan.delete
            else ""
        )
        + "."
    )
    if not plan.complete:
        typer.secho("The backup has no backup.json: it may be incomplete.", fg="yellow")
    if not yes and not typer.confirm(
        f"\nReplace the lake {plan.lake} with the backup?"
    ):
        raise typer.Abort()
    if not no_safety_copy:
        saved = backup_lake(work, suffix=SAFETY_SUFFIX)
        if saved is None:
            typer.echo("The lake holds nothing yet: no safety copy is needed.")
        else:
            typer.secho(
                f"✓ The lake as it was is kept in {saved.destination}", fg="green"
            )
    done = restore_lake(
        plan, on_file=lambda n: n % 500 == 0 and typer.echo(f"  {n} file(s) ...")
    )
    typer.secho(
        f"✓ Restored: {done.copied} file(s) copied ({_size(done.size)}), "
        f"{done.deleted} deleted",
        fg="green",
    )
