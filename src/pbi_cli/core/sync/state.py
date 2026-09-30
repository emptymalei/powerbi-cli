"""What a sync remembers between runs.

It is a small JSON document in the lake, ``tenant=<tenant>/_state/sync.json``. The data of a
sync is in the lake itself: a unit that is done has its snapshot there, and the lake says
how old it is. So the state only holds what the lake cannot say:

- how the latest runs ended;
- the units that failed or were held back by a quota, to show them and to count attempts;
- the scans that were started but whose result has not been collected, so that a run that
  stopped (an expired token, a crash) can pick them up instead of starting them again;
- when the last complete scan started, which is where the next one continues from.

A state is shared by the threads of a run. It writes itself to the lake at most once a
second while units finish, and always when a run begins or ends.
"""

import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from loguru import logger

from pbi_cli.core.scan import ScanFlags, ScanJob
from pbi_cli.core.store import LakeStore

#: Name of the state document in the lake.
STATE_NAME = "sync"

#: Version of the layout of the document.
SCHEMA = 1

#: How many finished runs are remembered.
MAX_RUNS = 20

#: Seconds between two writes while units finish.
FLUSH_EVERY = 1.0

#: How many failed or deferred units are remembered one by one; a run that holds back
#: thousands of units must not make the file grow without bound.
MAX_MARKED = 1000

FAILED = "failed"
DEFERRED = "deferred"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(stamp: str) -> datetime:
    value = datetime.fromisoformat(stamp)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class SyncState:
    """The memory of a sync of one tenant.

    :param store: the lake the state is kept in
    :param tenant: the tenant the state belongs to
    :param clock: returns the current time (aware, UTC)
    :param monotonic: a clock for the write interval (replaced in tests)
    """

    def __init__(
        self,
        store: LakeStore,
        tenant: str,
        *,
        clock: Callable[[], datetime] = _utcnow,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._store = store
        self._tenant = tenant
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._dirty = False
        self._last_write = float("-inf")

        data = store.read_state(tenant, STATE_NAME) or {}
        if data and data.get("schema") != SCHEMA:
            logger.warning(f"Ignoring the sync state of schema {data.get('schema')}")
            data = {}
        self.runs: List[Dict[str, Any]] = list(data.get("runs") or [])
        self.units: Dict[str, Dict[str, Any]] = dict(data.get("units") or {})
        self.dropped: int = int(data.get("units_dropped") or 0)
        self.scan: Dict[str, Any] = dict(data.get("scan") or {})
        for run in self.runs:
            if run.get("status") == "running":  # the process that wrote it is gone
                run["status"] = "interrupted"

    # -- persistence ------------------------------------------------------------------

    def _touch(self) -> None:
        self._dirty = True
        self.flush()

    def flush(self, force: bool = False) -> None:
        """Write the state to the lake if it changed (at most once a second unless forced)."""
        with self._lock:
            if not self._dirty:
                return
            now = self._monotonic()
            if not force and now - self._last_write < FLUSH_EVERY:
                return
            self._store.write_state(
                self._tenant,
                STATE_NAME,
                {
                    "schema": SCHEMA,
                    "tenant": self._tenant,
                    "updated_at": self._clock().isoformat(),
                    "runs": self.runs,
                    "units": self.units,
                    "units_dropped": self.dropped,
                    "scan": self.scan,
                },
            )
            self._dirty = False
            self._last_write = now

    # -- runs -------------------------------------------------------------------------

    def begin_run(self, targets: Sequence[str], options: Mapping[str, Any]) -> str:
        """Record that a run starts.

        :return: the id of the run
        """
        with self._lock:
            started = self._clock()
            run_id = f"{started:%Y%m%dT%H%M%SZ}"
            for run in self.runs:
                if run.get("status") == "running":
                    run["status"] = "interrupted"
            self.runs.append(
                {
                    "id": run_id,
                    "started_at": started.isoformat(),
                    "finished_at": None,
                    "status": "running",
                    "targets": list(targets),
                    "options": dict(options),
                    "counts": {},
                    "message": "",
                }
            )
            self._prune_jobs(started)
            self.dropped = 0
            self._dirty = True
            self.flush(force=True)
            return run_id

    def end_run(
        self, status: str, counts: Mapping[str, int], message: str = ""
    ) -> None:
        """Record how the current run ended."""
        with self._lock:
            if not self.runs:
                return
            run = self.runs[-1]
            run.update(
                status=status,
                finished_at=self._clock().isoformat(),
                counts=dict(counts),
                message=message,
            )
            del self.runs[:-MAX_RUNS]
            self._dirty = True
            self.flush(force=True)

    @property
    def last_run(self) -> Optional[Dict[str, Any]]:
        """The record of the latest run, if there was one."""
        with self._lock:
            return dict(self.runs[-1]) if self.runs else None

    # -- units that did not finish ----------------------------------------------------

    def mark(
        self,
        key: str,
        status: str,
        *,
        target: str,
        endpoint: str,
        error: str = "",
        retry_after: Optional[float] = None,
    ) -> None:
        """Remember a unit that failed or was held back (``failed`` or ``deferred``)."""
        if status not in (FAILED, DEFERRED):
            raise ValueError(f"a unit is marked failed or deferred, not '{status}'")
        with self._lock:
            previous = self.units.get(key) or {}
            if not previous and len(self.units) >= MAX_MARKED:
                self.dropped += 1
                self._touch()
                return
            self.units[key] = {
                "status": status,
                "target": target,
                "endpoint": endpoint,
                "attempts": int(previous.get("attempts", 0)) + 1,
                "updated_at": self._clock().isoformat(),
                "error": error,
                "retry_after": retry_after,
            }
            self._touch()

    def clear(self, key: str) -> None:
        """Forget a unit that has been done since."""
        with self._lock:
            if self.units.pop(key, None) is not None:
                self._touch()

    def units_with(self, status: str) -> Dict[str, Dict[str, Any]]:
        """The units that failed or were deferred, by key."""
        with self._lock:
            return {
                k: dict(v) for k, v in self.units.items() if v.get("status") == status
            }

    # -- scans ------------------------------------------------------------------------

    def job(self, batch: str, flags: ScanFlags) -> Optional[ScanJob]:
        """A scan of this batch that was started and not collected, if the API still has it."""
        with self._lock:
            job = self._job_of((self.scan.get("jobs") or {}).get(batch))
            entry = (self.scan.get("jobs") or {}).get(batch) or {}
            if job is None or entry.get("flags") != flags.canonical():
                return None
            return None if job.expired(self._clock()) else job

    def set_job(self, batch: str, job: ScanJob, flags: ScanFlags) -> None:
        """Remember a scan that the API has accepted (written at once: it must not get lost)."""
        with self._lock:
            jobs = self.scan.setdefault("jobs", {})
            jobs[batch] = {
                "scan_id": job.scan_id,
                "started_at": job.started_at.isoformat(),
                "flags": flags.canonical(),
            }
            self._dirty = True
            self.flush(force=True)

    def clear_job(self, batch: str) -> None:
        """Forget a scan whose result was collected."""
        with self._lock:
            if (self.scan.get("jobs") or {}).pop(batch, None) is not None:
                self._touch()

    @staticmethod
    def _job_of(entry: Any) -> Optional[ScanJob]:
        try:
            return ScanJob(str(entry["scan_id"]), _parse(entry["started_at"]))
        except (KeyError, TypeError, ValueError):
            return None  # damaged: as good as unknown

    def _prune_jobs(self, now: datetime) -> None:
        jobs = self.scan.get("jobs") or {}
        for batch in [
            batch
            for batch, entry in jobs.items()
            if (job := self._job_of(entry)) is None or job.expired(now)
        ]:
            del jobs[batch]

    @property
    def pending_jobs(self) -> int:
        """How many scans were started and not collected."""
        with self._lock:
            return len(self.scan.get("jobs") or {})

    def scan_baseline(
        self, flags: ScanFlags, coverage: Mapping[str, bool]
    ) -> Optional[datetime]:
        """When the last complete scan of these workspaces with these flags started.

        A scan with other flags, or of other workspaces (for example without the personal
        ones), is no baseline: what it left out would be missing.

        :param flags: what the scan includes
        :param coverage: which workspaces it covers, for example ``{"personal": False}``
        :return: ``None`` if there was no such scan
        """
        with self._lock:
            stamp = self.scan.get("last_success_at")
            if (
                not stamp
                or self.scan.get("flags") != flags.canonical()
                or self.scan.get("coverage") != dict(coverage)
            ):
                return None
            return _parse(stamp)

    def set_scan_baseline(
        self, flags: ScanFlags, coverage: Mapping[str, bool], started_at: datetime
    ) -> None:
        """Record that every workspace of ``coverage`` was scanned as of ``started_at``."""
        with self._lock:
            self.scan["flags"] = flags.canonical()
            self.scan["coverage"] = dict(coverage)
            self.scan["last_success_at"] = started_at.isoformat()
            self._touch()
