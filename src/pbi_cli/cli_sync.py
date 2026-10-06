"""``pbi sync``: keep what Power BI knows about the tenant in the data lake.

``plan`` shows what a sync would fetch and what it costs, without calling the API; ``run``
fetches it, within the quotas, and can be run again and again: it continues where the last
run stopped and never fetches twice what is still fresh; ``status`` shows how the last runs
went and what the lake holds (it reads the lake only, so it needs no token).
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AbstractSet, Annotated, List, Optional, Sequence, Tuple

import typer

from pbi_cli.cli_support import (
    ClientPool,
    LakeOption,
    ScanArtifactUsers,
    ScanDatasetExpressions,
    ScanDatasetSchema,
    ScanDatasourceDetails,
    ScanLineage,
    command,
    format_age,
    new_app,
    parse_duration,
    print_table,
)
from pbi_cli.config import PBIConfig
from pbi_cli.core.catalog import holding
from pbi_cli.core.planfile import Overrides, PlanFile, Step
from pbi_cli.core.planrun import PlanRun, SequencePlan
from pbi_cli.core.ratelimit import Limiter, QuotaTracker, format_wait
from pbi_cli.core.registry import ENDPOINTS, Scope
from pbi_cli.core.scan import ScanFlags
from pbi_cli.core.store import LakeStore, safe_name
from pbi_cli.core.sync.engine import (
    COMPLETED_WITH_FAILURES,
    INTERRUPTED,
    TOKEN_EXPIRED,
    Event,
    RunReport,
    SyncEngine,
)
from pbi_cli.core.sync.plan import MAX_DAYS, MAX_WORKERS, Plan, QuotaLine, SyncOptions
from pbi_cli.core.sync.runners import DEFERRED, DONE, FAILED, SKIPPED
from pbi_cli.core.sync.state import DEFERRED as MARKED_DEFERRED
from pbi_cli.core.sync.state import FAILED as MARKED_FAILED
from pbi_cli.core.sync.state import STATE_NAME, SyncState
from pbi_cli.core.sync.targets import ALL, DEFAULT, TARGETS, select_targets
from pbi_cli.errors import PBIError
from pbi_cli.session import (
    as_path,
    is_work_lake,
    lake_hint,
    lake_root,
    open_lake,
    quota_file,
    resolve_lake,
)

sync_app = new_app("sync")

#: How the status of a run is put in words.
STATUS_TEXT = {
    "completed": "completed",
    "completed_with_failures": "completed, with failures",
    "token_expired": "stopped: the token expired",
    "interrupted": "interrupted",
    "running": "still running (or it was stopped without a word)",
}

#: Failures listed in full; the rest are counted.
SHOWN_FAILURES = 10


@sync_app.callback(invoke_without_command=True)
def sync_group(ctx: typer.Context):
    """Keep what Power BI knows about the tenant in the data lake

    `pbi sync run` fetches the lists of workspaces, apps, reports and more, the audit
    events, and the metadata scans of the workspaces, within the quotas of the API. Run it
    as often as you like, for example every night: it continues where the last run stopped
    and never fetches twice what is still fresh. `pbi sync plan` shows what it would do
    and `pbi sync status` how the last runs went. What was fetched is in the data lake, see
    `pbi lake`.
    """
    if ctx.invoked_subcommand is None:
        typer.echo("Use pbi sync --help for help.")


# -- options and helpers ---------------------------------------------------------------


def _complete_targets(incomplete: str) -> List[str]:
    return [
        name
        for name in [DEFAULT, ALL, *(target.name for target in TARGETS)]
        if name.startswith(incomplete)
    ]


SyncTargets = Annotated[
    Optional[List[str]],
    typer.Argument(
        metavar="[TARGET]...",
        help=(
            "What to sync (default: the plain targets). Names: "
            + ", ".join(t.name for t in TARGETS)
            + f"; '{DEFAULT}' stands for the plain ones, '{ALL}' for everything."
        ),
        autocompletion=_complete_targets,
    ),
]
MaxAgeOption = Annotated[
    Optional[str],
    typer.Option(
        "--max-age",
        metavar="DURATION",
        help=(
            "Treat a stored answer younger than this as fresh, for example 30m, 6h or 2d "
            "(default: 24 hours for lists, 1 hour for audit events)"
        ),
    ),
]
ForceOption = Annotated[
    bool,
    typer.Option(
        "--force",
        help=(
            "Fetch again what the lake holds fresh, and scan every workspace again "
            "(a complete day of audit events cannot change and is never fetched again)"
        ),
    ),
]
DaysOption = Annotated[
    int,
    typer.Option(
        "--days",
        min=1,
        max=MAX_DAYS,
        help="Days of audit events to fetch, today included (the API keeps 28)",
    ),
]
FullScanOption = Annotated[
    bool,
    typer.Option(
        "--full-scan",
        help="Scan every workspace, not only those that changed since the last scan",
    ),
]
ExcludePersonalOption = Annotated[
    bool,
    typer.Option("--exclude-personal", help="Leave personal workspaces out of scans"),
]
ExcludeInactiveOption = Annotated[
    bool,
    typer.Option("--exclude-inactive", help="Leave inactive workspaces out of scans"),
]
ScanIntervalOption = Annotated[
    float,
    typer.Option(
        "--scan-interval", min=0.1, help="Seconds between status checks of a scan"
    ),
]
AdminProfileOption = Annotated[
    Optional[str],
    typer.Option(
        "--admin-profile",
        help="The profile of the administrator account to use (default: the active "
        "profile of the group admin)",
    ),
]
UserProfileOption = Annotated[
    Optional[str],
    typer.Option(
        "--user-profile",
        help="The profile of the user account to use (default: the active profile of "
        "the group user)",
    ),
]
ScanTimeoutOption = Annotated[
    float,
    typer.Option(
        "--scan-timeout",
        min=1,
        help="Seconds to wait for one scan (a timed out scan is continued next run)",
    ),
]
ConfigOption = Annotated[
    Optional[Path],
    typer.Option(
        "--config",
        "-c",
        exists=True,
        dir_okay=False,
        help=(
            "A plan file (YAML) that says what to keep, for which workspaces and through "
            "which account, instead of target names. --force, --max-age, --days, "
            "--workers, --wait, --scan-interval and --scan-timeout still apply to every "
            "step of it"
        ),
    ),
]


def _options(
    targets: Optional[List[str]],
    *,
    force: bool,
    max_age: Optional[str],
    days: int,
    lineage: bool,
    datasource_details: bool,
    dataset_schema: bool,
    dataset_expressions: bool,
    get_artifact_users: bool,
    full_scan: bool,
    exclude_personal: bool,
    exclude_inactive: bool,
    scan_interval: float,
    scan_timeout: float,
    workers: int = 4,
    wait: float = 120.0,
    admin_profile: Optional[str] = None,
    user_profile: Optional[str] = None,
) -> SyncOptions:
    return SyncOptions(
        targets=tuple(targets or ()),
        force=force,
        max_age=parse_duration(max_age, "--max-age") if max_age else None,
        workers=workers,
        max_wait=wait,
        days=days,
        scan_flags=ScanFlags(
            lineage=lineage,
            datasource_details=datasource_details,
            dataset_schema=dataset_schema,
            dataset_expressions=dataset_expressions,
            get_artifact_users=get_artifact_users,
        ),
        full_scan=full_scan,
        exclude_personal=exclude_personal,
        exclude_inactive=exclude_inactive,
        scan_interval=scan_interval,
        scan_timeout=scan_timeout,
        admin_profile=admin_profile,
        user_profile=user_profile,
    )


def _lake() -> LakeStore:
    """The lake a sync writes to: one that is configured and switched on."""
    config = PBIConfig()
    store = open_lake(config)
    if store is None:
        raise PBIError(
            f"pbi sync keeps what it fetches in the data lake. {lake_hint(config)}"
        )
    return store


def _print_targets(
    names: List[str],
    store: LakeStore,
    available: Optional[AbstractSet[Scope]] = None,
    accounts: Sequence[str] = (),
) -> None:
    """Say what is synced.

    :param available: the kinds of account that are stored; what the plain sync is, and
        what can be named, depends on them
    :param accounts: the accounts the sync uses, each as ``profile (kind)``
    """
    typer.echo(f"Data lake: {store.root}")
    if available is not None and not available:
        return  # no account: the engine says so, with what to do
    selection = select_targets(names, available)
    if accounts:
        typer.echo(f"Accounts: {', '.join(accounts)}")
    typer.echo(f"Targets: {', '.join(selection.names)}")
    for target in selection.targets:
        if target.sensitive and target.name not in selection.implied:
            typer.secho(f"  {target.name} copies {target.sensitive}", fg="yellow")
    if not names:
        rest = [
            t.name
            for t in TARGETS
            if t.name not in selection.names
            and (available is None or t.scope in available)
        ]
        if rest:
            typer.echo(
                "Not included (name them to include them, see --help): "
                f"{', '.join(rest)}"
            )


def _count(value: Optional[int]) -> str:
    return "?" if value is None else str(value)


# -- a plan file instead of target names -------------------------------------------------

#: The options that say what a sync does; with a plan file they are written in the file.
_IN_THE_FILE = {
    "lineage": ("--lineage", "scan: {lineage: true}"),
    "datasource_details": ("--datasource-details", "scan: {datasource_details: true}"),
    "dataset_schema": ("--dataset-schema", "scan: {dataset_schema: true}"),
    "dataset_expressions": (
        "--dataset-expressions",
        "scan: {dataset_expressions: true}",
    ),
    "get_artifact_users": ("--get-artifact-users", "scan: {get_artifact_users: true}"),
    "full_scan": ("--full-scan", "tenant: {full_scan: true}"),
    "exclude_personal": ("--exclude-personal", "tenant: {exclude_personal: true}"),
    "exclude_inactive": ("--exclude-inactive", "tenant: {exclude_inactive: true}"),
    "admin_profile": ("--admin-profile", "accounts: {admin: <profile>}"),
    "user_profile": ("--user-profile", "accounts: {user: [<profile>]}"),
}


def _given(ctx: typer.Context, name: str) -> bool:
    """Whether an option was typed on the command line (and not just left at its default)."""
    return getattr(ctx.get_parameter_source(name), "name", "") == "COMMANDLINE"


def _refuse_with_a_plan_file(ctx: typer.Context, targets: Optional[List[str]]) -> None:
    """Refuse what says again, or against, what the plan file says.

    :raises PBIError: for target names, or an option that the file takes over
    """
    if targets:
        raise PBIError(
            "Name targets or give a plan file, not both: the file says what to sync "
            "(under tenant: targets: [...])."
        )
    for name, (flag, where) in _IN_THE_FILE.items():
        if _given(ctx, name):
            raise PBIError(
                f"{flag} cannot be used with --config: the plan file says it "
                f"({where})."
            )


def _overrides(
    ctx: typer.Context,
    *,
    force: bool,
    max_age: Optional[str],
    days: int,
    scan_interval: float,
    scan_timeout: float,
    workers: Optional[int] = None,
    wait: Optional[float] = None,
) -> Overrides:
    """What the flags typed on the command line change in every step of a plan file: those
    that were typed (a flag left at its default does not overrule the file)."""
    return Overrides(
        force=force,
        max_age=parse_duration(max_age, "--max-age") if max_age else None,
        days=days if _given(ctx, "days") else None,
        workers=workers if _given(ctx, "workers") else None,
        wait=wait if _given(ctx, "wait") else None,
        scan_interval=scan_interval if _given(ctx, "scan_interval") else None,
        scan_timeout=scan_timeout if _given(ctx, "scan_timeout") else None,
    )


def _say_what_session_lake_is_for(plan_file: PlanFile) -> None:
    """A sync writes to the work lake and nowhere else: say so when the plan file names
    another lake (``session.lake`` is the one that ``pbi tui --config`` opens)."""
    wanted = plan_file.lake
    if not wanted:
        return
    try:
        here = is_work_lake(lake_root(as_path(wanted)))
    except Exception:  # a place that cannot be read is not the work lake either
        here = False
    if not here:
        typer.secho(
            f"Note: session.lake ({wanted}) is the lake that `pbi tui --config` opens; a "
            "sync writes to the work lake above, and only there.",
            fg="yellow",
        )


def _plan_file_plan(config: Path, overrides: Overrides) -> None:
    """``pbi sync plan --config``."""
    store = _lake()
    plan_file = PlanFile.load(config)
    with ClientPool() as clients:
        run = PlanRun(SyncEngine(clients, store), store, plan_file, overrides=overrides)
        sequence = run.plan()
    typer.echo(f"Data lake: {store.root}")
    typer.echo(f"Plan file: {plan_file.path}")
    _say_what_session_lake_is_for(plan_file)
    if sequence.accounts:
        typer.echo(f"Accounts: {', '.join(sequence.accounts)}")
    _print_sequence(sequence)


# -- plan ------------------------------------------------------------------------------


def _print_target_rows(plan: Plan) -> None:
    """The targets of a plan: how much of each is fresh, what is left, what it costs."""
    rows = []
    for item in plan.targets:
        rows.append(
            [
                item.target.name + (" *" if item.implied else ""),
                item.target.endpoint,
                _count(item.units),
                _count(item.fresh if item.units is not None else None),
                _count(item.todo),
                _count(item.requests),
            ]
        )
    print_table(["TARGET", "OPERATION", "UNITS", "FRESH", "TO DO", "REQUESTS"], rows)
    if any(item.implied for item in plan.targets):
        typer.echo("* only here because another target needs its rows")
    for item in plan.targets:
        for note in item.notes:
            typer.echo(f"  {item.target.name}: {note}")


def _print_quota(quota: Sequence[QuotaLine]) -> None:
    """The requests a plan needs against the quota of each operation."""
    if not quota:
        typer.echo("\nNothing to fetch: the lake holds everything fresh.")
        return
    typer.echo("\nRequests against the quota of each operation")
    print_table(
        ["OPERATION", "NEEDED", "QUOTA", "LEFT NOW"],
        [
            [
                line.endpoint,
                line.requests,
                line.quota or "-",
                "-" if line.left is None else line.left,
            ]
            for line in quota
        ],
    )
    over = [line for line in quota if not line.fits]
    if not over:
        typer.echo("\nEverything fits the quota now.")
    for line in over:
        later = f", about {line.hours} more hour(s)" if line.hours else ""
        typer.secho(
            f"\n{line.endpoint} needs {line.requests} requests and {line.left} fit now "
            f"({line.quota}): the rest is held back{later}; `pbi sync run` continues "
            "where it stopped.",
            fg="yellow",
        )


def _print_plan(plan: Plan) -> None:
    typer.echo(f"Tenant: {plan.tenant}\n")
    _print_target_rows(plan)
    _print_quota(plan.quota)


def _print_sequence(sequence: SequencePlan) -> None:
    """What a plan file would do: each step as a plan, then what is wrong or worth knowing,
    then what all of them need from the quotas."""
    for number, (step, made) in enumerate(sequence.steps, 1):
        typer.secho(f"\nStep {number}: {step.title}", bold=True)
        for item in made.targets:
            if item.target.sensitive and not item.implied:
                typer.secho(
                    f"  {item.target.name} copies {item.target.sensitive}", fg="yellow"
                )
        _print_target_rows(made)
    if not sequence.steps:
        typer.echo("\nNo step is planned yet.")
    for where, problem in sequence.unmatched:
        typer.secho(f"  {where}: {problem}", fg="yellow")
    for note in sequence.notes:
        typer.secho(f"  {note}", fg="yellow")
    if sequence.steps:
        _print_quota(sequence.quota)


@command(sync_app, "plan")
def sync_plan(
    ctx: typer.Context,
    targets: SyncTargets = None,
    max_age: MaxAgeOption = None,
    force: ForceOption = False,
    days: DaysOption = MAX_DAYS,
    lineage: ScanLineage = False,
    datasource_details: ScanDatasourceDetails = False,
    dataset_schema: ScanDatasetSchema = False,
    dataset_expressions: ScanDatasetExpressions = False,
    get_artifact_users: ScanArtifactUsers = False,
    full_scan: FullScanOption = False,
    exclude_personal: ExcludePersonalOption = False,
    exclude_inactive: ExcludeInactiveOption = False,
    scan_interval: ScanIntervalOption = 5.0,
    scan_timeout: ScanTimeoutOption = 600.0,
    admin_profile: AdminProfileOption = None,
    user_profile: UserProfileOption = None,
    config: ConfigOption = None,
):
    """Show what a sync would fetch and what it costs, without calling the API

    The plan is worked out from what the lake holds: how much of each target is fresh, how
    many requests are needed, and how they compare with the quotas (which `pbi sync run`
    keeps). Where the number of requests depends on a list that is not in the lake yet,
    such as the users of every report before the reports have been fetched, it says so.

    ```
    # What a plain sync would do
    pbi sync plan

    # Plus the audit events of the last week, and a scan with lineage
    pbi sync plan default activity scan --days 7 --lineage

    # What a plan file would do: each of its steps, and what they cost together
    pbi sync plan --config pbi-plan.yaml
    ```

    With `--config` the plan file says what to sync, so no target names and none of the
    options that it takes over (the scan options, the profiles) can be given; `--force`,
    `--max-age`, `--days` and the like still apply to every step of it.

    !!! warning "Requires Admin"

        The admin targets need an admin account; the `user-...` targets need a user account.
        Without names the plain targets are synced: the administrator's lists when there is
        an administrator account, and else what a user can see, workspace by workspace.
        `--admin-profile` and `--user-profile` choose other profiles than the active ones.

    """
    if config is not None:
        _refuse_with_a_plan_file(ctx, targets)
        _plan_file_plan(
            config,
            _overrides(
                ctx,
                force=force,
                max_age=max_age,
                days=days,
                scan_interval=scan_interval,
                scan_timeout=scan_timeout,
            ),
        )
        return
    options = _options(
        targets,
        force=force,
        max_age=max_age,
        days=days,
        lineage=lineage,
        datasource_details=datasource_details,
        dataset_schema=dataset_schema,
        dataset_expressions=dataset_expressions,
        get_artifact_users=get_artifact_users,
        full_scan=full_scan,
        exclude_personal=exclude_personal,
        exclude_inactive=exclude_inactive,
        scan_interval=scan_interval,
        scan_timeout=scan_timeout,
        admin_profile=admin_profile,
        user_profile=user_profile,
    )
    store = _lake()
    with ClientPool() as clients:
        engine = SyncEngine(clients, store)
        _print_targets(
            list(options.targets),
            store,
            engine.available_scopes(options),
            engine.accounts(options),
        )
        _print_plan(engine.plan(options))


# -- run -------------------------------------------------------------------------------

SYMBOLS = {DONE: "✓", SKIPPED: "·", FAILED: "✗", DEFERRED: "…"}


class _Progress:
    """Prints what a run does: every unit of a small stage, milestones of a big one."""

    def __call__(self, event: Event) -> None:
        if event.kind == "stage":
            typer.echo(
                f"\nStage {event.stage}: {event.units} unit(s) of {', '.join(event.targets)}"
            )
            return
        assert event.unit is not None and event.outcome is not None
        unit, outcome = event.unit, event.outcome
        milestone = (
            event.done == event.total or event.done % max(1, event.total // 10) == 0
        )
        if outcome.status in (FAILED, DEFERRED):
            colour = "red" if outcome.status == FAILED else "yellow"
            typer.secho(
                f"  {SYMBOLS[outcome.status]} {unit.key}: {outcome.message.splitlines()[0]}",
                fg=colour,
            )
        elif event.total <= 25:
            detail = f"  {outcome.message}" if outcome.message else ""
            typer.echo(f"  {SYMBOLS[outcome.status]} {unit.key}{detail}")
        elif milestone:
            typer.echo(f"  [{event.done}/{event.total}] {unit.target} ...")


def _print_report(report: RunReport) -> None:
    took = (report.finished_at or report.started_at) - report.started_at
    counts = report.counts
    typer.echo(
        f"\nFinished in {format_wait(took.total_seconds())}: {counts[DONE]} fetched, "
        f"{counts[SKIPPED]} fresh (not fetched again), {counts[FAILED]} failed, "
        f"{counts[DEFERRED]} deferred."
    )
    for key, message in report.failures[:SHOWN_FAILURES]:
        typer.secho(f"  failed: {key}: {message.splitlines()[0]}", fg="red")
    if len(report.failures) > SHOWN_FAILURES:
        typer.secho(
            f"  ... and {len(report.failures) - SHOWN_FAILURES} more: see `pbi sync status`",
            fg="red",
        )
    if report.deferred:
        wait = report.retry_after
        later = f" in about {format_wait(wait)}" if wait else " later"
        typer.secho(
            f"  {len(report.deferred)} unit(s) were held back by a quota: run the command "
            f"again{later}; it continues with them.",
            fg="yellow",
        )
    for note in report.notes:
        typer.secho(f"  {note}", fg="yellow")


@command(sync_app, "run")
def sync_run(
    ctx: typer.Context,
    targets: SyncTargets = None,
    max_age: MaxAgeOption = None,
    force: ForceOption = False,
    days: DaysOption = MAX_DAYS,
    workers: Annotated[
        int,
        typer.Option(
            "--workers",
            min=1,
            max=MAX_WORKERS,
            help="Requests (and scans) worked on at the same time",
        ),
    ] = 4,
    wait: Annotated[
        float,
        typer.Option(
            "--wait",
            min=0,
            help=(
                "Seconds a request may wait for quota before its unit is held back "
                "(0: never wait; the next run continues with it)"
            ),
        ),
    ] = 120.0,
    lineage: ScanLineage = False,
    datasource_details: ScanDatasourceDetails = False,
    dataset_schema: ScanDatasetSchema = False,
    dataset_expressions: ScanDatasetExpressions = False,
    get_artifact_users: ScanArtifactUsers = False,
    full_scan: FullScanOption = False,
    exclude_personal: ExcludePersonalOption = False,
    exclude_inactive: ExcludeInactiveOption = False,
    scan_interval: ScanIntervalOption = 5.0,
    scan_timeout: ScanTimeoutOption = 600.0,
    admin_profile: AdminProfileOption = None,
    user_profile: UserProfileOption = None,
    config: ConfigOption = None,
):
    """Fetch what the targets need into the data lake, within the quotas

    Nothing is fetched twice: what the lake holds fresh is skipped, so the command can be
    run again and again, and it continues where the last run stopped.

    When the token expires the run stops and keeps what is done: sign in again with
    `pbi auth` and run the command again. What a quota holds back is tried again next
    time. A unit that fails, such as a report that cannot be read, is reported and the
    run goes on; it is tried again next time. A scan that was started and not collected is
    continued, not started again.

    Audit events are kept one log per UTC day; a day is complete, and never fetched again,
    a day after it ended. A scan fetches metadata of 100 workspaces per request and
    continues from the start of the last complete scan with the same options; the first
    one, or one with other options, covers every workspace.

    ```
    # The plain targets: workspaces, apps, capacities, reports, datasets, ...
    pbi sync run

    # Also the audit events of the last 28 days, and a scan with lineage
    pbi sync run default activity scan --lineage

    # Only the users of every report (it fetches the reports first)
    pbi sync run report-users

    # Every night, from a scheduler: the events of the last week
    pbi sync run default activity --days 7

    # What a plan file says, step after step (the steps for the tenant first)
    pbi sync run --config pbi-plan.yaml
    ```

    With `--config` the plan file says what to sync, so no target names and none of the
    options that it takes over (the scan options, the profiles) can be given; `--force`,
    `--max-age`, `--days`, `--workers` and the like still apply to every step of it. The
    run ends at the first step whose token expired; run it again after signing in and it
    continues, as every step skips what the lake holds fresh.

    !!! warning "Requires Admin"

        The admin targets need an admin account; the `user-...` targets need a user account.
        Without names the plain targets are synced: the administrator's lists when there is
        an administrator account, and else what a user can see, workspace by workspace.
        `--admin-profile` and `--user-profile` choose other profiles than the active ones.

    """
    if config is not None:
        _refuse_with_a_plan_file(ctx, targets)
        report, again = _plan_file_run(
            config,
            _overrides(
                ctx,
                force=force,
                max_age=max_age,
                days=days,
                scan_interval=scan_interval,
                scan_timeout=scan_timeout,
                workers=workers,
                wait=wait,
            ),
        )
        _finish_run(report, again)
        return
    options = _options(
        targets,
        force=force,
        max_age=max_age,
        days=days,
        lineage=lineage,
        datasource_details=datasource_details,
        dataset_schema=dataset_schema,
        dataset_expressions=dataset_expressions,
        get_artifact_users=get_artifact_users,
        full_scan=full_scan,
        exclude_personal=exclude_personal,
        exclude_inactive=exclude_inactive,
        scan_interval=scan_interval,
        scan_timeout=scan_timeout,
        workers=workers,
        wait=wait,
        admin_profile=admin_profile,
        user_profile=user_profile,
    )
    store = _lake()
    with ClientPool() as clients:
        engine = SyncEngine(clients, store)
        _print_targets(
            list(options.targets),
            store,
            engine.available_scopes(options),
            engine.accounts(options),
        )
        report = engine.run(options, on_event=_Progress())
    _finish_run(
        report, "pbi sync run" + "".join(f" {name}" for name in options.targets)
    )


def _plan_file_run(config: Path, overrides: Overrides) -> Tuple[RunReport, str]:
    """``pbi sync run --config``: the steps of the plan file, one after the other.

    :return: how it went, and the command that continues it
    """
    store = _lake()
    plan_file = PlanFile.load(config)
    typer.echo(f"Data lake: {store.root}")
    typer.echo(f"Plan file: {plan_file.path}")
    _say_what_session_lake_is_for(plan_file)
    with ClientPool() as clients:
        run = PlanRun(SyncEngine(clients, store), store, plan_file, overrides=overrides)
        labels = run.accounting().labels()
        if labels:
            typer.echo(f"Accounts: {', '.join(labels)}")

        def announce(number: int, step: Step) -> None:
            typer.secho(f"\nStep {number}: {step.title}", bold=True)

        report = run.run(on_event=_Progress(), on_step=announce)
    return report, f"pbi sync run --config {config}"


def _finish_run(report: RunReport, again: str) -> None:
    """Say how a run went and end the command as it deserves.

    :param again: the command that continues it
    """
    _print_report(report)
    if report.status == TOKEN_EXPIRED:
        raise PBIError(
            f"{report.message}\nWhat is done is kept: after signing in, run `{again}` "
            "again to continue."
        )
    if report.status == INTERRUPTED:
        typer.secho(
            f"\nInterrupted. What is done is kept: run `{again}` to continue.",
            fg="yellow",
        )
        raise typer.Exit(130)
    if report.status == COMPLETED_WITH_FAILURES:
        raise typer.Exit(1)


# -- status ----------------------------------------------------------------------------


def _moment(stamp: Optional[str]) -> Optional[datetime]:
    if not stamp:
        return None
    value = datetime.fromisoformat(stamp)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _show_tenant(store: LakeStore, tenant: str, now: datetime) -> None:
    state = SyncState(store, tenant)
    typer.echo(f"\nTenant: {tenant}")

    run = state.last_run
    if run is None:
        typer.echo("No run is recorded.")
    else:
        started = _moment(run.get("started_at"))
        assert started is not None
        label = STATUS_TEXT.get(run["status"], run["status"])
        typer.echo(
            f"Last run: {started:%Y-%m-%d %H:%M} UTC ({format_age(now - started)} ago): {label}"
        )
        typer.echo(f"  targets: {', '.join(run.get('targets') or [])}")
        counts = run.get("counts") or {}
        typer.echo(
            f"  {counts.get('done', 0)} fetched, {counts.get('skipped', 0)} fresh, "
            f"{counts.get('failed', 0)} failed, {counts.get('deferred', 0)} deferred"
        )
        if run.get("message"):
            typer.echo(f"  {run['message'].splitlines()[0]}")

    failed = state.units_with(MARKED_FAILED)
    if failed:
        typer.secho(
            f"\nFailed units ({len(failed)}; they are tried again by the next run):",
            fg="red",
        )
        for key, unit in list(failed.items())[:SHOWN_FAILURES]:
            reason = (unit.get("error") or "").splitlines()
            typer.echo(
                f"  {key}: {reason[0] if reason else ''} "
                f"(attempts: {unit.get('attempts', 1)})"
            )
        if len(failed) > SHOWN_FAILURES:
            typer.echo(f"  ... and {len(failed) - SHOWN_FAILURES} more")

    deferred = state.units_with(MARKED_DEFERRED)
    if deferred:
        ready = []
        for unit in deferred.values():
            at = _moment(unit.get("updated_at"))
            if at is not None:
                ready.append(
                    at + timedelta(seconds=float(unit.get("retry_after") or 0))
                )
        when = ""
        if ready:
            soonest = min(ready)
            when = (
                ", the first can be tried again now"
                if soonest <= now
                else f", the first can be tried again in {format_age(soonest - now)}"
            )
        typer.secho(
            f"\nHeld back by a quota: {len(deferred)} unit(s){when}.", fg="yellow"
        )
    if state.dropped:
        typer.echo(f"  (and {state.dropped} more that are not listed one by one)")

    scan = state.scan
    if scan.get("last_success_at"):
        flags = [
            name for name, value in (scan.get("flags") or {}).items() if value == "true"
        ]
        since = _moment(scan["last_success_at"])
        assert since is not None
        typer.echo(
            f"\nLast complete scan: started {since:%Y-%m-%d %H:%M} UTC, options: "
            f"{', '.join(flags) or 'none'}; the next one continues from there."
        )
    if state.pending_jobs:
        typer.secho(
            f"{state.pending_jobs} scan(s) were started and not collected: `pbi sync run scan` "
            "continues them.",
            fg="yellow",
        )

    limiter = Limiter(QuotaTracker(path=quota_file()))
    usage = []
    for endpoint in ENDPOINTS:
        windows = limiter.usage(endpoint, tenant)
        if any(window.used for window in windows):
            usage.append([endpoint.id] + ["  ".join(w.describe() for w in windows)])
    if usage:
        typer.echo("\nQuota left in the last hour (requests left/allowed)")
        print_table(["OPERATION", "LEFT"], usage)

    rows = []
    for target in TARGETS:
        found = _holdings(store, tenant, target.endpoint, now)
        if found:
            rows.append([target.name, target.endpoint, *found])
    if rows:
        typer.echo("\nWhat the lake holds")
        print_table(["TARGET", "OPERATION", "STORED", "NEWEST"], rows)


def _holdings(store: LakeStore, tenant: str, endpoint: str, now: datetime) -> List[str]:
    """How much of an operation the lake holds and how new it is (empty if nothing)."""
    held = holding(store, tenant, endpoint)
    if held is None:
        return []
    newest = format_age(now - held.newest) + " ago" if held.newest else "-"
    return [f"{held.count} {held.unit}(s)", newest]


@command(sync_app, "status")
def sync_status(
    tenant: Annotated[
        Optional[str],
        typer.Option(
            "--tenant", "-t", help="Only this tenant (default: every tenant synced)"
        ),
    ] = None,
    lake: LakeOption = None,
):
    """Show how the last runs went and what the lake holds

    Lists the latest run, the units that failed or that a quota held back, the scans that
    were started and not collected, the quota used in the last hour, and what the lake
    holds for each target. It reads the lake only, so it needs no token and no network.

    ```
    pbi sync status

    # A lake that someone shared
    pbi sync status --lake s3://my-bucket/pbi-lake
    ```
    """
    opened = resolve_lake(lake)
    if opened is None:
        raise PBIError(f"There is no data lake yet. {lake_hint()}")
    store = opened.store
    typer.echo(f"Data lake: {store.root}")
    tenants = [
        t for t in store.tenants() if store.read_state(t, STATE_NAME) is not None
    ]
    if tenant is not None:
        tenants = [t for t in tenants if t == safe_name(tenant)]
    if not tenants:
        typer.echo(
            "Nothing has been synced yet. `pbi sync plan` shows what a sync would do and "
            "`pbi sync run` does it."
        )
        return
    now = datetime.now(timezone.utc)
    for name in tenants:
        _show_tenant(store, name, now)
