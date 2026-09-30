"""``pbi lake``: look into the data lake that the commands fill, and tidy it.

These commands read the lake only (no token, no network), so they work offline and with an
expired token. The lake is the ``lake`` folder in the cache folder
(``pbi config set-cache-folder``); see ``docs/lake.md`` for its layout.
"""

import json
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Dict, List, Optional, Sequence

import typer

from pbi_cli.cli_support import command, new_app
from pbi_cli.config import PBIConfig
from pbi_cli.core.registry import get_endpoint
from pbi_cli.core.store import LakeStore, ParameterSet, Snapshot, safe_name
from pbi_cli.errors import PBIError
from pbi_cli.session import lake_hint, lake_path

lake_app = new_app("lake")


@lake_app.callback(invoke_without_command=True)
def lake_group(ctx: typer.Context):
    """Browse the data lake of fetched API results"""
    if ctx.invoked_subcommand is None:
        typer.echo("Use pbi lake --help for help.")


# -- helpers -----------------------------------------------------------------------


def _store() -> LakeStore:
    """The lake of the configured cache folder."""
    config = PBIConfig()
    path = lake_path(config)
    if path is None:
        raise PBIError(f"There is no data lake yet. {lake_hint(config)}")
    return LakeStore(path)


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


def _age(delta: timedelta) -> str:
    """How old something is, in the largest unit that keeps it short: 40 s, 5 min, 3 h, 2 d."""
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 90:
        return f"{seconds} s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours = round(seconds / 3600)
    if hours < 48:
        return f"{hours} h"
    return f"{round(seconds / 86400)} d"


def _size(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def _params_text(params: Dict[str, str]) -> str:
    return " ".join(f"{key}={value}" for key, value in params.items()) or "-"


def _print_table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    """Print rows under a header; every column but the last is padded to its widest cell."""
    cells = [[str(cell) for cell in row] for row in rows]
    widths = [
        max(len(header[i]), *(len(row[i]) for row in cells)) for i in range(len(header))
    ]
    for line in [list(header), *cells]:
        typer.echo(
            "  ".join(
                cell if i == len(line) - 1 else cell.ljust(widths[i])
                for i, cell in enumerate(line)
            ).rstrip()
        )


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
    ```
    """
    store = _store()
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
                        _age(snapshot.age(now)),
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
                        _age(now - written) if written else "-",
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
        _print_table(header, rows)
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
    store = _store()
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
    """
    if not yes:
        raise typer.Abort()

    store = _store()
    removed = 0
    for tenant_name in _tenants(store, tenant):
        removed += store.prune(keep=keep, tenant=tenant_name, endpoint_id=endpoint)
    typer.secho(
        f"✓ Deleted {removed} old version(s), kept the newest {keep} of each request",
        fg="green",
    )
