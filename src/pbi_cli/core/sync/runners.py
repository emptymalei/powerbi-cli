"""Doing the units of a sync: one runner for each kind of unit.

A runner gets the `Context` of the run and a `Unit`, makes its requests through the client,
writes what it got to the lake, and returns an `Outcome`. Runners do not handle errors: what
goes wrong propagates to the engine, which decides whether the unit failed, was held back by
a quota, or stops the whole run (an expired token).
"""

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from loguru import logger

from pbi_cli.core.client import PowerBIClient, rows_of
from pbi_cli.core.registry import Scope, get_endpoint
from pbi_cli.core.scan import batch_key, latest_scan, run_scan, store_scan
from pbi_cli.core.store import LakeStore
from pbi_cli.core.sync.plan import (
    EVENTS,
    MODIFIED,
    SCAN,
    SNAPSHOT,
    Planner,
    SyncOptions,
    Unit,
)
from pbi_cli.core.sync.state import SyncState
from pbi_cli.errors import ApiError, ScanError, ScanTimeout

DONE = "done"
SKIPPED = "skipped"
FAILED = "failed"
DEFERRED = "deferred"
CANCELLED = "cancelled"

#: HTTP statuses that mean "the link to continue an interrupted read is no longer good".
STALE_CURSOR = (400, 404, 410)


@dataclass
class Outcome:
    """What became of a unit.

    :param status: ``done`` (fetched), ``skipped`` (the lake held it fresh), ``failed``,
        ``deferred`` (held back by a quota) or ``cancelled`` (the run stopped first)
    :param message: what to tell the user
    :param rows: the rows that units of other targets are made from
    :param version: the version that was stored, or found fresh
    :param requests: how many requests it took
    :param retry_after: for a deferred unit, the seconds until a request fits
    :param endpoint: for a deferred unit, the operation that has no quota left
    """

    status: str
    message: str = ""
    rows: Optional[List[Any]] = None
    version: Optional[str] = None
    requests: int = 0
    retry_after: Optional[float] = None
    endpoint: Optional[str] = None


@dataclass
class Context:
    """What the runners of a run share.

    :param options: what the sync does
    :param store: the lake
    :param tenant: the tenant the lake data belongs to
    :param state: what earlier runs left behind, and what this one records
    :param planner: tells what the lake holds fresh
    :param client_for: the client that signs in for a kind of token and a profile (``None``:
        the active one)
    :param clock: the current time (aware, UTC)
    :param sleep: waits for some seconds
    :param monotonic: a clock for timeouts
    """

    options: SyncOptions
    store: LakeStore
    tenant: str
    state: SyncState
    planner: Planner
    client_for: Callable[[Scope, Optional[str]], PowerBIClient]
    clock: Callable[[], datetime]
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic


# -- snapshots and their fan-outs ------------------------------------------------------


def run_snapshot(ctx: Context, unit: Unit) -> Outcome:
    """Fetch one request into the lake, unless the lake holds it fresh."""
    client = ctx.client_for(unit.scope, unit.account)
    max_age = ctx.options.max_age if ctx.options.max_age is not None else unit.ttl
    result = client.fetch(
        unit.endpoint,
        unit.params,
        max_age=max_age,
        refresh=ctx.options.force,
        max_wait=ctx.options.max_wait,
    )
    endpoint = get_endpoint(unit.endpoint)
    rows = rows_of(endpoint, result.data) if unit.has_children else None
    version = result.snapshot.version if result.snapshot else None
    if result.from_cache:
        return Outcome(SKIPPED, "fresh", rows=rows, version=version)
    count = result.manifest.get("rows")
    return Outcome(
        DONE,
        f"{count} rows" if count is not None else "",
        rows=rows,
        version=version,
        requests=int(result.manifest.get("pages") or 1),
    )


def run_modified(ctx: Context, unit: Unit) -> Outcome:
    """List the workspaces to scan: all of them, or those modified since the last scan.

    The list is an intermediate result, not data: it is not stored, and it is asked for
    every time, as the API allows only 30 of these requests an hour.
    """
    client = ctx.client_for(unit.scope, unit.account)
    response = client.request(unit.endpoint, unit.params, max_wait=ctx.options.max_wait)
    ids = [
        str(row["id"])
        for row in rows_of(get_endpoint(unit.endpoint), response.data)
        if isinstance(row, dict) and row.get("id")
    ]
    since = unit.params.get("modifiedSince")
    note = f" modified since {since}" if since else ""
    return Outcome(DONE, f"{len(ids)} workspaces{note}", rows=ids, requests=1)


# -- events ----------------------------------------------------------------------------


def _quoted(moment: datetime) -> str:
    """A time the way ``activityevents`` wants it: UTC, milliseconds, single quotes."""
    moment = moment.astimezone(timezone.utc)
    return f"'{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}Z'"


def run_events_day(ctx: Context, unit: Unit) -> Outcome:
    """Read the events of one UTC day into its log, page by page.

    Every page is stored before the next one is asked for, with the link to the next page,
    so that an interrupted read continues where it stopped. Events are told apart by
    their ``Id``, so reading a day again adds only what is new. A day is sealed, and never
    read again, once it is over and the events that arrive late have had time to arrive.
    """
    assert unit.day is not None
    options, store = ctx.options, ctx.store
    endpoint = get_endpoint(unit.endpoint)
    day = unit.day
    stored = store.event_day(ctx.tenant, endpoint.id, day)
    if ctx.planner.is_fresh(unit):
        return Outcome(SKIPPED, "complete" if stored and stored.sealed else "fresh")

    now = ctx.clock()
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    day_end = start + timedelta(days=1)
    # today is not over: ask for what has happened so far, and come back for the rest
    end = min(day_end - timedelta(milliseconds=1), now)
    params = {"startDateTime": _quoted(start), "endDateTime": _quoted(end)}
    request = {"method": endpoint.method, "path": endpoint.path, "params": params}
    client = ctx.client_for(unit.scope, unit.account)
    profile = client.profile_name()

    def read(start_at: Optional[str]) -> Tuple[int, int]:
        added = pages = 0
        for page in client.iter_pages(
            endpoint.id, params, url=start_at, max_wait=options.max_wait
        ):
            pages += 1
            body = page.data if isinstance(page.data, dict) else {}
            last = body.get("lastResultSet") is True
            following = None if last else body.get("continuationUri")
            added += store.append_events(
                ctx.tenant,
                endpoint.id,
                day,
                rows_of(endpoint, body),
                cursor=following or "",  # "" clears it: there is nothing more to read
                profile=profile,
                request=request,
                at=now,
            )
            if last:
                break
        return added, pages

    resume = stored.cursor if stored and not options.force else None
    try:
        added, pages = read(resume)
    except ApiError as error:
        if resume is None or error.status not in STALE_CURSOR:
            raise
        logger.warning(
            f"The link to resume {unit.key} is no longer good; starting over"
        )
        store.append_events(
            ctx.tenant,
            endpoint.id,
            day,
            [],
            cursor="",
            profile=profile,
            request=request,
            at=now,
        )
        added, pages = read(None)

    complete = now >= day_end + options.seal_grace
    if complete:
        store.seal_day(ctx.tenant, endpoint.id, day, at=now)
    return Outcome(
        DONE,
        f"{added} new events" + (", complete" if complete else ", day still open"),
        requests=pages,
    )


# -- scans -----------------------------------------------------------------------------


def run_scan_batch(ctx: Context, unit: Unit) -> Outcome:
    """Scan up to 100 workspaces and keep the result.

    A scan that an earlier run started and did not collect (the token expired, the run was
    stopped) is continued, not started again.
    """
    options = ctx.options
    flags = options.scan_flags
    if ctx.planner.is_fresh(unit):
        found = latest_scan(ctx.store, ctx.tenant, unit.workspace_ids, flags)
        return Outcome(SKIPPED, "fresh", version=found.version if found else None)

    batch = batch_key(unit.workspace_ids)
    client = ctx.client_for(unit.scope, unit.account)
    try:
        run = run_scan(
            client,
            unit.workspace_ids,
            flags,
            interval=options.scan_interval,
            timeout=options.scan_timeout,
            resume=ctx.state.job(batch, flags),
            on_started=lambda job: ctx.state.set_job(batch, job, flags),
            sleep=ctx.sleep,
            monotonic=ctx.monotonic,
            clock=ctx.clock,
            max_wait=options.max_wait,
        )
    except ScanTimeout:
        raise  # the scan may still succeed: its id stays in the state for the next run
    except ScanError:
        ctx.state.clear_job(batch)  # it failed: a new scan is needed
        raise
    snapshot = store_scan(
        ctx.store,
        ctx.tenant,
        run,
        unit.workspace_ids,
        flags,
        profile=client.profile_name(),
        extra={"modified_since": unit.params.get("modifiedSince")},
    )
    ctx.state.clear_job(batch)
    count = len(run.result.get("workspaces") or [])
    return Outcome(
        DONE,
        f"{count} workspaces" + (", continued an earlier scan" if run.resumed else ""),
        version=snapshot.version,
        requests=(0 if run.resumed else 1) + run.polls + 1,
    )


RUNNERS: Dict[str, Callable[[Context, Unit], Outcome]] = {
    SNAPSHOT: run_snapshot,
    MODIFIED: run_modified,
    EVENTS: run_events_day,
    SCAN: run_scan_batch,
}
