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
from pbi_cli.core.store import LakeStore, Snapshot
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
    :param scanned_at: for a scan, when the scan whose result it stored (or found fresh)
        began: the changes made after that moment may be missing from it
    """

    status: str
    message: str = ""
    rows: Optional[List[Any]] = None
    version: Optional[str] = None
    requests: int = 0
    retry_after: Optional[float] = None
    endpoint: Optional[str] = None
    scanned_at: Optional[datetime] = None


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
    read again, once it is over, the events that arrive late have had time to arrive, and a
    read that went to its last page covered the day to its end.

    An interrupted read is continued with the window it was started with, which may end
    before the day did. The day's manifest says how far each read covers (``window_end``,
    and ``covered_until`` once a read went to its last page); when the continued read
    covered less than the day and the day is due to be sealed, the day is read once more
    whole before it is (only the events not stored yet are added).
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
    last_moment = day_end - timedelta(milliseconds=1)
    # today is not over: ask for what has happened so far, and come back for the rest
    end = min(last_moment, now)
    params = {"startDateTime": _quoted(start), "endDateTime": _quoted(end)}
    request = {"method": endpoint.method, "path": endpoint.path, "params": params}
    client = ctx.client_for(unit.scope, unit.account)
    profile = client.profile_name()
    # the ids of the stored events, kept between pages rather than read for each page
    known = store.event_ids(ctx.tenant, endpoint.id, day)
    covered = stored.covered_until if stored else None

    def read(start_at: Optional[str], window: Optional[datetime]) -> Tuple[int, int]:
        """Read to the last page, from a link to continue at or from the first page.

        :param window: where the window of the read ends (``None``: not known, for a read
            started by an older version)
        """
        nonlocal covered
        added = pages = 0
        for page in client.iter_pages(
            endpoint.id, params, url=start_at, max_wait=options.max_wait
        ):
            pages += 1
            body = page.data if isinstance(page.data, dict) else {}
            last = body.get("lastResultSet") is True
            following = None if last else body.get("continuationUri")
            extra: Dict[str, Any] = {}
            if window is not None:
                extra["window_end"] = window.isoformat()
                if not following and (covered is None or window > covered):
                    covered = window  # the read is over: it covers its whole window
                    extra["covered_until"] = window.isoformat()
            added += store.append_events(
                ctx.tenant,
                endpoint.id,
                day,
                rows_of(endpoint, body),
                cursor=following or "",  # "" clears it: there is nothing more to read
                profile=profile,
                request=request,
                at=now,
                known=known,
                extra=extra,
            )
            if last:
                break
        return added, pages

    resume = stored.cursor if stored and not options.force else None
    resumed_window: Optional[datetime] = None
    if resume and stored is not None:
        stamp = stored.manifest.get("window_end")
        resumed_window = datetime.fromisoformat(stamp) if stamp else None
    try:
        added, pages = read(resume, resumed_window if resume else end)
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
            known=known,
        )
        added, pages = read(None, end)

    due = now >= day_end + options.seal_grace
    if due and (covered is None or covered < last_moment):
        # the read that was continued stopped short of the end of the day: read it whole
        more, more_pages = read(None, end)
        added, pages = added + more, pages + more_pages
    complete = due and covered is not None and covered >= last_moment
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
        return Outcome(
            SKIPPED,
            "fresh",
            version=found.version if found else None,
            scanned_at=_scan_started(found) if found else None,
        )

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
            on_started=lambda job: ctx.state.set_job(
                batch, job, flags, unit.workspace_ids
            ),
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
        scanned_at=run.started_at,
    )


def _scan_started(snapshot: Snapshot) -> datetime:
    """When a stored scan began (when it was received, for a scan stored without it)."""
    stamp = snapshot.manifest.get("started_at")
    try:
        moment = datetime.fromisoformat(stamp) if stamp else None
    except (TypeError, ValueError):
        moment = None
    if moment is None:
        return snapshot.fetched_at
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


RUNNERS: Dict[str, Callable[[Context, Unit], Outcome]] = {
    SNAPSHOT: run_snapshot,
    MODIFIED: run_modified,
    EVENTS: run_events_day,
    SCAN: run_scan_batch,
}
