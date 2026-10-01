"""The sync the TUI runs: what it is doing, written by the thread that runs it and read by the
screens that show it.

The thread that runs a sync calls `RunState.on_event` as units finish; the screens read the
state on a timer, so they need no messages between threads and a screen that is opened in
the middle of a run shows everything from the start.
"""

import threading
from collections import Counter
from datetime import datetime
from typing import Callable, List, Optional, Tuple

from pbi_cli.core.ratelimit import format_wait
from pbi_cli.core.sync.engine import Event, RunReport
from pbi_cli.core.sync.plan import SyncOptions
from pbi_cli.core.sync.runners import DEFERRED, DONE, FAILED, SKIPPED

#: Lines kept for the log of a run; older ones are dropped.
MAX_LINES = 20_000

SYMBOLS = {DONE: "✓", SKIPPED: "·", FAILED: "✗", DEFERRED: "…"}
_STYLES = {DONE: "green", SKIPPED: "grey62", FAILED: "red", DEFERRED: "yellow"}

#: A line of the log: its text and a Rich style.
Line = Tuple[str, str]


def describe(event: Event) -> Optional[Line]:
    """The log line for an event, or ``None`` when a big stage should stay quiet.

    Every unit of a small stage is logged, and the failures and holds of any stage; a stage
    of hundreds of units logs only a line every tenth of the way.
    """
    if event.kind == "stage":
        return (
            f"Stage {event.stage}: {event.units} unit(s) of {', '.join(event.targets)}",
            "bold",
        )
    assert event.unit is not None and event.outcome is not None
    unit, outcome = event.unit, event.outcome
    symbol = SYMBOLS.get(outcome.status, "?")
    style = _STYLES.get(outcome.status, "")
    message = outcome.message.splitlines()[0] if outcome.message else ""
    if outcome.status in (FAILED, DEFERRED):
        return f"{symbol} {unit.key}: {message}", style
    if event.total <= 25:
        return f"{symbol} {unit.key}  {message}".rstrip(), style
    if event.done == event.total or event.done % max(1, event.total // 10) == 0:
        return f"[{event.done}/{event.total}] {unit.target} ...", "grey62"
    return None


#: Failed units listed one by one in the log; the rest are counted.
SHOWN_FAILURES = 10


def report_lines(report: RunReport) -> List[Line]:
    """How a run went, as lines for the log (the words of ``pbi sync run``)."""
    took = (report.finished_at or report.started_at) - report.started_at
    counts = report.counts
    lines: List[Line] = [
        (
            f"Finished in {format_wait(took.total_seconds())}: {counts[DONE]} fetched, "
            f"{counts[SKIPPED]} fresh (not fetched again), {counts[FAILED]} failed, "
            f"{counts[DEFERRED]} deferred.",
            "bold",
        )
    ]
    for key, message in report.failures[:SHOWN_FAILURES]:
        lines.append((f"  failed: {key}: {message.splitlines()[0]}", "red"))
    if len(report.failures) > SHOWN_FAILURES:
        lines.append(
            (
                f"  ... and {len(report.failures) - SHOWN_FAILURES} more: see the Lake tab",
                "red",
            )
        )
    if report.deferred:
        wait = report.retry_after
        later = f" in about {format_wait(wait)}" if wait else " later"
        lines.append(
            (
                f"  {len(report.deferred)} unit(s) were held back by a quota: run the sync "
                f"again{later}; it continues with them.",
                "yellow",
            )
        )
    if report.cancelled:
        lines.append(
            (
                f"  Stopped: {report.cancelled} unit(s) were not started. What is done is "
                "kept; run the sync again to continue.",
                "yellow",
            )
        )
    lines.extend((f"  {note}", "yellow") for note in report.notes)
    return lines


class RunState:
    """One run of a sync, from its start to its end.

    :param label: what the run is for, in words
    :param options: what the sync does
    :param started_at: when it began
    """

    def __init__(self, label: str, options: SyncOptions, started_at: datetime):
        self.label = label
        self.options = options
        self.started_at = started_at
        self.finished_at: Optional[datetime] = None
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self._running = True
        self.stage = 0
        self.stage_units = 0
        self.stage_done = 0
        self.stage_targets: Tuple[str, ...] = ()
        self.counts: Counter = Counter()
        self._lines: List[Line] = []
        self._dropped = 0
        self.report: Optional[RunReport] = None
        self.error = ""

    # -- written by the thread that runs ----------------------------------------------

    def log(self, text: str, style: str = "") -> None:
        """Add a line to the log."""
        with self._lock:
            self._lines.append((text, style))
            if len(self._lines) > MAX_LINES:
                del self._lines[0]
                self._dropped += 1

    def on_event(self, event: Event) -> None:
        """Record an event of the engine."""
        with self._lock:
            if event.kind == "stage":
                self.stage = event.stage
                self.stage_units = event.units
                self.stage_done = 0
                self.stage_targets = event.targets
            else:
                assert event.outcome is not None
                self.stage_done = event.done
                self.stage_units = event.total
                self.counts[event.outcome.status] += 1
        line = describe(event)
        if line is not None:
            self.log(*line)

    def finish(
        self,
        finished_at: datetime,
        report: Optional[RunReport] = None,
        error: str = "",
    ) -> None:
        """Record that the run is over, and put how it went in the log."""
        if report is not None:
            for text, style in report_lines(report):
                self.log(text, style)
        with self._lock:
            self.finished_at = finished_at
            self.report = report
            self.error = error
            self._running = False

    # -- read by the screens ------------------------------------------------------------

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def stopping(self) -> bool:
        return self.running and self.stop.is_set()

    def request_stop(self) -> None:
        """Ask the run to stop: what is being fetched finishes, nothing new starts."""
        if self.running and not self.stop.is_set():
            self.stop.set()
            self.log("Stopping: the requests in flight finish first ...", "yellow")

    def read(self, since: int = 0) -> Tuple[List[Line], int]:
        """The log lines from number ``since`` on, and the number to ask for next time."""
        with self._lock:
            end = self._dropped + len(self._lines)
            start = max(since, self._dropped)
            return self._lines[start - self._dropped :], end

    def progress(self) -> Tuple[int, int, int, Tuple[str, ...], Counter]:
        """The stage, the units done in it, its units, its targets, and the counts so far."""
        with self._lock:
            return (
                self.stage,
                self.stage_done,
                self.stage_units,
                self.stage_targets,
                Counter(self.counts),
            )

    def summary(self, clock: Callable[[], datetime]) -> str:
        """One line on how the run is going or went."""
        stage, done, units, targets, counts = self.progress()
        if self.running:
            where = (
                f"stage {stage}: {', '.join(targets)} {done}/{units}"
                if stage
                else "starting"
            )
            return f"{self.label}: {where}"
        took = (self.finished_at or clock()) - self.started_at
        return (
            f"{self.label}: finished in {int(took.total_seconds())} s, "
            f"{counts[DONE]} fetched, {counts[SKIPPED]} fresh, {counts[FAILED]} failed, "
            f"{counts[DEFERRED]} deferred"
        )
