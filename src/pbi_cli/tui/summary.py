"""What the lake holds for a tenant and how the last syncs went, for the Sync screen.

It is the TUI's counterpart of ``pbi sync status``: plain data that the screen puts in a
table, made from the lake and the state of the sync only (no token, no network).
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from pbi_cli.core.catalog import holding
from pbi_cli.core.ratelimit import Limiter
from pbi_cli.core.registry import ENDPOINTS
from pbi_cli.core.store import LakeStore
from pbi_cli.core.sync.state import DEFERRED, FAILED, SyncState
from pbi_cli.core.sync.targets import TARGETS
from pbi_cli.core.timefmt import format_age

#: How the status of a run is put in words.
STATUS_TEXT = {
    "completed": "completed",
    "completed_with_failures": "completed, with failures",
    "token_expired": "stopped: the token expired",
    "interrupted": "stopped before it was done",
    "running": "still running (or it was stopped without a word)",
}

#: Failed units listed one by one.
SHOWN_FAILURES = 8

#: A line of text and a Rich style.
Line = Tuple[str, str]


@dataclass
class LakeSummary:
    """What the Lake tab shows.

    :param holdings: for each target that has data: target, operation, stored, newest
    :param lines: how the last runs went, as lines of text
    :param quota: for each operation used in the last hour: operation, quota left
    """

    holdings: List[Tuple[str, str, str, str]] = field(default_factory=list)
    lines: List[Line] = field(default_factory=list)
    quota: List[Tuple[str, str]] = field(default_factory=list)


def _moment(stamp: Optional[str]) -> Optional[datetime]:
    if not stamp:
        return None
    value = datetime.fromisoformat(stamp)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def summarize(
    store: LakeStore,
    tenant: str,
    now: datetime,
    limiter: Optional[Limiter] = None,
) -> LakeSummary:
    """Gather what the lake holds and how the last runs went.

    :param store: the lake
    :param tenant: the tenant
    :param now: the current time (aware, UTC)
    :param limiter: where the quota counters are, to show what is left of each quota
    """
    summary = LakeSummary()
    for target in TARGETS:
        held = holding(store, tenant, target.endpoint)
        if held:
            newest = format_age(now - held.newest) + " ago" if held.newest else "-"
            summary.holdings.append(
                (target.name, target.endpoint, f"{held.count} {held.unit}(s)", newest)
            )

    state = SyncState(store, tenant)
    run = state.last_run
    if run is None:
        summary.lines.append(("No sync is recorded for this tenant yet.", "grey62"))
    else:
        started = _moment(run.get("started_at"))
        assert started is not None
        label = STATUS_TEXT.get(run["status"], run["status"])
        summary.lines.append(
            (
                f"Last sync: {started:%Y-%m-%d %H:%M} UTC ({format_age(now - started)} ago): {label}",
                "bold",
            )
        )
        counts = run.get("counts") or {}
        summary.lines.append(
            (
                f"  {', '.join(run.get('targets') or [])}: {counts.get('done', 0)} fetched, "
                f"{counts.get('skipped', 0)} fresh, {counts.get('failed', 0)} failed, "
                f"{counts.get('deferred', 0)} deferred",
                "",
            )
        )
        if run.get("message"):
            summary.lines.append((f"  {run['message'].splitlines()[0]}", "yellow"))

    failed = state.units_with(FAILED)
    if failed:
        summary.lines.append(
            (f"{len(failed)} failed unit(s), tried again by the next sync:", "red")
        )
        for key, unit in list(failed.items())[:SHOWN_FAILURES]:
            reason = (unit.get("error") or "").splitlines()
            summary.lines.append(
                (
                    f"  {key}: {reason[0] if reason else ''} (attempts: {unit.get('attempts', 1)})",
                    "",
                )
            )
        if len(failed) > SHOWN_FAILURES:
            summary.lines.append(
                (f"  ... and {len(failed) - SHOWN_FAILURES} more", "grey62")
            )

    deferred = state.units_with(DEFERRED)
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
        summary.lines.append(
            (f"{len(deferred)} unit(s) held back by a quota{when}.", "yellow")
        )

    scan = state.scan
    if scan.get("last_success_at"):
        flags = [n for n, v in (scan.get("flags") or {}).items() if v == "true"]
        since = _moment(scan["last_success_at"])
        assert since is not None
        summary.lines.append(
            (
                f"Last complete scan began {since:%Y-%m-%d %H:%M} UTC, options: "
                f"{', '.join(flags) or 'none'}; the next one continues from there.",
                "",
            )
        )
    if state.pending_jobs:
        summary.lines.append(
            (
                f"{state.pending_jobs} scan(s) were started and not collected: a sync of "
                "the scan target continues them.",
                "yellow",
            )
        )

    if limiter is not None:
        for endpoint in ENDPOINTS:
            windows = limiter.usage(endpoint, tenant)
            if any(window.used for window in windows):
                summary.quota.append(
                    (endpoint.id, "  ".join(w.describe() for w in windows))
                )
    return summary
