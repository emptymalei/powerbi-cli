"""``pbi lake``: look into the data lake that the commands fill, and tidy it.

These commands read the lake only (no token, no network), so they work offline and with an
expired token. The lake is the ``lake`` folder in the cache folder
(``pbi config set-cache-folder``); see ``docs/lake.md`` for its layout.
"""

import json
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Dict, List, Optional, Sequence

import typer

from pbi_cli.cli_support import LakeOption, command, format_age, new_app, print_table
from pbi_cli.config import PBIConfig
from pbi_cli.core.publish import EXCLUDABLE, PublishPlan, plan_publish, publish
from pbi_cli.core.registry import get_endpoint
from pbi_cli.core.store import LakeStore, ParameterSet, Snapshot, safe_name
from pbi_cli.errors import PBIError
from pbi_cli.session import as_path, lake_hint, resolve_lake

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
):
    """Delete old versions, keeping the newest of each request

    Every fetch adds a version and nothing is overwritten, so a lake grows. Pruning keeps
    the newest versions of each request (by default only the newest). Event logs are kept.

    ```
    # Keep only the newest version of every request
    pbi lake prune

    # Keep the last three workspace lists
    pbi lake prune -e admin.groups --keep 3
    ```

    Only the lake of the cache folder can be pruned: a lake given with `--lake`, and a
    published lake, are read-only.
    """
    if not yes:
        raise typer.Abort()

    store = _store(lake)
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
    result = publish(plan)
    typer.secho(
        f"✓ Published to {target.root}: {result.copied} file(s) copied "
        f"({_size(result.size)}), {result.skipped} already there"
        + (f", {result.pruned} older version(s) deleted" if plan.prune else ""),
        fg="green",
    )
    typer.echo(f"Others open it with: pbi tui --lake {target.root}")
