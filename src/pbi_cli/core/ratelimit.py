"""Local counters of the requests made, to stay within the documented quotas.

The Power BI admin APIs allow few requests (for example 50 per hour for the workspace
list), the quotas are shared by everyone using the tenant, and the ``429`` answer is the
only authority. The counters here are therefore an *estimate* made from the requests this
machine sent: they let the client wait instead of being throttled, and let the UI show
how much of a quota is left. When the API does answer ``429 Retry-After`` with a long
wait, the endpoint is marked as blocked until then, so the next run does not hit the same
wall.

The counters are kept in a small JSON file so separate runs of the command line add up.
"""

import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Union

from loguru import logger

from pbi_cli.core.registry import Endpoint, RateLimit
from pbi_cli.errors import RateLimitError

#: Seconds of history that are kept: the longest window of any documented quota.
RETENTION = 3600.0


class _DefaultMaxWait:
    """Marks "use the limiter's own ``max_wait``" (``None`` already means "no limit")."""


DEFAULT_MAX_WAIT = _DefaultMaxWait()

#: How long one request may wait for quota: seconds, ``None`` (forever) or the default.
MaxWait = Union[float, None, _DefaultMaxWait]


def format_wait(seconds: float) -> str:
    """A duration in words, for messages: ``45 seconds``, ``12 minutes``, ``1 h 05 min``."""
    seconds = max(0, int(round(seconds)))
    if seconds < 90:
        return f"{seconds} seconds"
    minutes = int(round(seconds / 60))
    if minutes < 90:
        return f"{minutes} minutes"
    return f"{minutes // 60} h {minutes % 60:02d} min"


@dataclass(frozen=True)
class WindowUsage:
    """How much of one quota window is used.

    :param allowed: requests allowed in the window
    :param seconds: length of the window in seconds
    :param used: requests made during the last ``seconds``
    """

    allowed: int
    seconds: int
    used: int

    @property
    def left(self) -> int:
        """Requests that still fit in the window."""
        return max(0, self.allowed - self.used)

    def describe(self) -> str:
        """For example ``38/50 h``: requests left out of the requests allowed."""
        unit = {3600: "h", 60: "min"}.get(self.seconds, f"{self.seconds}s")
        return f"{self.left}/{self.allowed} {unit}"


class QuotaTracker:
    """Timestamps of the requests made, per key, kept for an hour.

    :param path: JSON file that keeps the counters between runs; ``None`` keeps them in
        memory only
    :param clock: returns the current time in seconds since the epoch
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        clock: Callable[[], float] = time.time,
    ):
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._calls: Dict[str, List[float]] = {}
        self._blocked: Dict[str, float] = {}
        self._mtime: Optional[int] = None
        self._load()

    # -- persistence ---------------------------------------------------------------

    def _load(self) -> None:
        """Re-read the file if another process changed it."""
        if self._path is None:
            return
        try:
            mtime = self._path.stat().st_mtime_ns
        except OSError:
            return
        if mtime == self._mtime:
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._calls = {
                key: [float(t) for t in stamps]
                for key, stamps in data.get("calls", {}).items()
                if isinstance(stamps, list)
            }
            self._blocked = {
                key: float(until) for key, until in data.get("blocked", {}).items()
            }
        except (OSError, ValueError, TypeError, AttributeError):
            logger.warning(f"Ignoring unreadable quota counters in {self._path}")
            self._calls, self._blocked = {}, {}
        self._mtime = mtime

    def _save(self) -> None:
        if self._path is None:
            return
        state = {"version": 1, "calls": self._calls, "blocked": self._blocked}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self._path)
            self._mtime = self._path.stat().st_mtime_ns
        except OSError as error:
            logger.warning(
                f"Could not save the quota counters to {self._path}: {error}"
            )

    def _trim(self, now: float) -> None:
        cutoff = now - RETENTION
        for key in list(self._calls):
            kept = [t for t in self._calls[key] if t > cutoff]
            if kept:
                self._calls[key] = kept
            else:
                del self._calls[key]
        self._blocked = {k: until for k, until in self._blocked.items() if until > now}

    # -- use -----------------------------------------------------------------------

    def record(self, key: str, at: Optional[float] = None) -> None:
        """Note that a request was made."""
        with self._lock:
            self._load()
            now = self._clock()
            self._calls.setdefault(key, []).append(now if at is None else at)
            self._trim(now)
            self._save()

    def block(self, key: str, seconds: float) -> None:
        """Refuse requests for ``seconds`` (the API asked to wait that long)."""
        with self._lock:
            self._load()
            now = self._clock()
            self._blocked[key] = max(self._blocked.get(key, 0.0), now + seconds)
            self._trim(now)
            self._save()

    def count(self, key: str, window: float, now: Optional[float] = None) -> int:
        """Requests made during the last ``window`` seconds."""
        with self._lock:
            self._load()
            return self._count(key, window, self._clock() if now is None else now)

    def _count(self, key: str, window: float, now: float) -> int:
        return sum(1 for t in self._calls.get(key, []) if t > now - window)

    def next_slot(
        self, key: str, limit: Optional[RateLimit], now: Optional[float] = None
    ) -> float:
        """Seconds to wait until a request is allowed (0: now).

        A request is allowed when it fits every window of ``limit`` and the key is not
        blocked.
        """
        with self._lock:
            self._load()
            return self._next_slot(key, limit, self._clock() if now is None else now)

    def _next_slot(self, key: str, limit: Optional[RateLimit], now: float) -> float:
        wait = max(0.0, self._blocked.get(key, 0.0) - now)
        if limit is None:
            return wait
        stamps = sorted(self._calls.get(key, []))
        for allowed, seconds in limit.windows:
            inside = [t for t in stamps if t > now - seconds]
            if len(inside) >= allowed:
                # the request fits once enough of the oldest ones have left the window
                wait = max(wait, inside[len(inside) - allowed] + seconds - now)
        return max(0.0, wait)

    def usage(
        self, key: str, limit: RateLimit, now: Optional[float] = None
    ) -> List[WindowUsage]:
        """The usage of every window of ``limit``, longest window first."""
        with self._lock:
            self._load()
            now = self._clock() if now is None else now
            return [
                WindowUsage(allowed, seconds, self._count(key, seconds, now))
                for allowed, seconds in sorted(limit.windows, key=lambda w: -w[1])
            ]

    def remaining(self, key: str, limit: RateLimit, now: Optional[float] = None) -> int:
        """Requests that fit right now: the room left in the tightest window."""
        with self._lock:
            self._load()
            now = self._clock() if now is None else now
            return min(
                max(0, allowed - self._count(key, seconds, now))
                for allowed, seconds in limit.windows
            )


class Limiter:
    """Makes requests wait for free quota and counts them.

    :param tracker: where the requests are counted
    :param sleep: waits for some seconds; replaced in tests
    :param max_wait: longest time one request may wait for quota before
        `RateLimitError` is raised; ``None`` waits as long as
        needed
    """

    def __init__(
        self,
        tracker: QuotaTracker,
        sleep: Callable[[float], None] = time.sleep,
        max_wait: Optional[float] = 120.0,
    ):
        self.tracker = tracker
        self._sleep = sleep
        self._max_wait = max_wait
        self._reserve = threading.Lock()
        self._semaphores: Dict[str, threading.BoundedSemaphore] = {}
        self._semaphore_lock = threading.Lock()

    @staticmethod
    def key(endpoint: Endpoint, tenant: str) -> str:
        """The counter of an endpoint in a tenant."""
        return f"{tenant}/{endpoint.id}"

    def usage(self, endpoint: Endpoint, tenant: str) -> List[WindowUsage]:
        """Usage of each quota window, longest first; empty without a quota."""
        if endpoint.limit is None:
            return []
        return self.tracker.usage(self.key(endpoint, tenant), endpoint.limit)

    def remaining(self, endpoint: Endpoint, tenant: str) -> Optional[int]:
        """Requests that fit right now; ``None`` without a documented quota."""
        if endpoint.limit is None:
            return None
        return self.tracker.remaining(self.key(endpoint, tenant), endpoint.limit)

    def wait_time(self, endpoint: Endpoint, tenant: str) -> float:
        """Seconds until the next request is allowed; 0 when it is allowed now."""
        return self.tracker.next_slot(self.key(endpoint, tenant), endpoint.limit)

    def block(self, endpoint: Endpoint, tenant: str, seconds: float) -> None:
        """Refuse requests to an endpoint for ``seconds``: the API asked to wait."""
        self.tracker.block(self.key(endpoint, tenant), seconds)

    def _semaphore(self, key: str, size: int) -> threading.BoundedSemaphore:
        with self._semaphore_lock:
            if key not in self._semaphores:
                self._semaphores[key] = threading.BoundedSemaphore(size)
            return self._semaphores[key]

    @contextmanager
    def slot(
        self,
        endpoint: Endpoint,
        tenant: str,
        max_wait: MaxWait = DEFAULT_MAX_WAIT,
    ) -> Iterator[None]:
        """Wait for quota, count the request, and hold a concurrency slot meanwhile.

        Use around one request: ``with limiter.slot(endpoint, tenant): send()``.

        :param max_wait: overrides the limiter's ``max_wait`` for this request (``None``
            waits as long as needed)
        :raises RateLimitError: if the quota is used up (or the API asked to wait) and
            waiting would take longer than ``max_wait``
        """
        limit = endpoint.limit
        key = self.key(endpoint, tenant)
        allowed = self._max_wait if isinstance(max_wait, _DefaultMaxWait) else max_wait

        concurrency = limit.max_concurrent if limit else None
        semaphore = self._semaphore(key, concurrency) if concurrency else None
        if semaphore is not None:
            semaphore.acquire()
        try:
            self._wait_for_quota(endpoint, key, limit, allowed)
            yield
        finally:
            if semaphore is not None:
                semaphore.release()

    def _wait_for_quota(
        self,
        endpoint: Endpoint,
        key: str,
        limit: Optional[RateLimit],
        max_wait: Optional[float],
    ) -> None:
        waited = 0.0
        while True:
            with self._reserve:
                wait = self.tracker.next_slot(key, limit)
                if wait <= 0:
                    self.tracker.record(key)
                    return
            if max_wait is not None and waited + wait > max_wait:
                quota = f" ({limit.describe()})" if limit else ""
                raise RateLimitError(
                    f"No request to {endpoint.id} is possible right now{quota}: the "
                    f"quota is used up or Power BI asked to wait. The next request fits "
                    f"in {format_wait(wait)}; run the command again later.",
                    status=None,
                    retry_after=wait,
                    endpoint=endpoint.id,
                )
            logger.info(
                f"Waiting {format_wait(wait)} before calling {endpoint.id} "
                "(quota used up or throttled)"
            )
            self._sleep(wait)
            waited += wait
