"""Tests for the quota counters and the limiter."""

import threading

import pytest
from core_helpers import FakeClock

from pbi_cli.core.ratelimit import Limiter, QuotaTracker, format_wait
from pbi_cli.core.registry import HOUR, MINUTE, RateLimit, get_endpoint
from pbi_cli.errors import RateLimitError, Stopped

GROUPS = get_endpoint("admin.groups")  # 50/h and 15/min
SCAN = get_endpoint("admin.scan.start")  # 500/h and 16 concurrent
USER_APPS = get_endpoint("user.apps")  # no documented quota

# ---------------------------------------------------------------------------
# tracker
# ---------------------------------------------------------------------------


def test_count_only_sees_the_window():
    clock = FakeClock()
    tracker = QuotaTracker(clock=clock)
    tracker.record("k", at=clock.now - 100)
    tracker.record("k", at=clock.now - 30)
    tracker.record("k", at=clock.now - 5)
    assert tracker.count("k", 60) == 2
    assert tracker.count("k", 3600) == 3
    assert tracker.count("other", 3600) == 0


def test_old_requests_are_forgotten_after_an_hour():
    clock = FakeClock()
    tracker = QuotaTracker(clock=clock)
    tracker.record("k")
    clock.now += HOUR + 1
    tracker.record("k")
    assert tracker.count("k", 2 * HOUR) == 1


def test_next_slot_is_zero_while_there_is_room():
    clock = FakeClock()
    tracker = QuotaTracker(clock=clock)
    limit = RateLimit(windows=((3, MINUTE),))
    tracker.record("k")
    tracker.record("k")
    assert tracker.next_slot("k", limit) == 0
    assert tracker.remaining("k", limit) == 1


def test_next_slot_waits_for_the_oldest_request_to_leave_the_window():
    clock = FakeClock()
    tracker = QuotaTracker(clock=clock)
    limit = RateLimit(windows=((3, 60),))
    for offset in (0, 10, 20):
        clock.now = 1_000_000.0 + offset
        tracker.record("k")
    clock.now = 1_000_030.0
    assert tracker.next_slot("k", limit) == pytest.approx(
        30
    )  # first call leaves at +60
    assert tracker.remaining("k", limit) == 0
    clock.now = 1_000_061.0
    assert tracker.next_slot("k", limit) == 0


def test_next_slot_takes_the_tightest_of_several_windows():
    clock = FakeClock()
    tracker = QuotaTracker(clock=clock)
    limit = RateLimit(windows=((50, HOUR), (15, MINUTE)))
    for _ in range(15):  # 15 requests in the same second fill the per-minute window
        tracker.record("k")
    assert tracker.next_slot("k", limit) == pytest.approx(60)
    assert tracker.remaining("k", limit) == 0
    clock.now += 61  # the minute has passed, the hour has 15 of 50 used
    assert tracker.next_slot("k", limit) == 0
    hour, minute = tracker.usage("k", limit)
    assert (hour.left, hour.used, hour.allowed) == (35, 15, 50)
    assert (minute.left, minute.used, minute.allowed) == (15, 0, 15)
    assert tracker.remaining("k", limit) == 15  # the minute window is the tighter one


def test_hour_window_dominates_when_it_is_full():
    clock = FakeClock()
    tracker = QuotaTracker(clock=clock)
    limit = RateLimit(windows=((50, HOUR), (15, MINUTE)))
    for i in range(50):  # spread over 50 minutes, one per minute
        clock.now = 1_000_000.0 + i * 60
        tracker.record("k")
    clock.now = 1_000_000.0 + 50 * 60
    # the first request leaves the hour window 3600s after it was made
    assert tracker.next_slot("k", limit) == pytest.approx(3600 - 50 * 60)


def test_counters_are_shared_through_the_file(tmp_path):
    path = tmp_path / "state" / "quota.json"
    clock = FakeClock()
    first = QuotaTracker(path, clock=clock)
    first.record("k")
    first.record("k")

    second = QuotaTracker(path, clock=clock)  # a later run of the command line
    assert second.count("k", HOUR) == 2
    second.record("k")
    assert first.count("k", HOUR) == 3  # picks up what the other process wrote


def test_unreadable_counter_file_is_ignored(tmp_path):
    path = tmp_path / "quota.json"
    path.write_text("{not json", encoding="utf-8")
    tracker = QuotaTracker(path, clock=FakeClock())
    assert tracker.count("k", HOUR) == 0
    tracker.record("k")
    assert QuotaTracker(path, clock=FakeClock()).count("k", HOUR) == 1


def test_counter_file_with_unexpected_content_is_ignored(tmp_path):
    path = tmp_path / "quota.json"
    path.write_text('{"calls": {"k": "nope", "j": ["x"]}}', encoding="utf-8")
    assert QuotaTracker(path, clock=FakeClock()).count("k", HOUR) == 0


# ---------------------------------------------------------------------------
# limiter
# ---------------------------------------------------------------------------


def make_limiter(clock, max_wait=120.0):
    tracker = QuotaTracker(clock=clock)
    return Limiter(tracker, sleep=clock.sleep, max_wait=max_wait), tracker


def test_request_inside_the_quota_does_not_wait():
    clock = FakeClock()
    limiter, tracker = make_limiter(clock)
    with limiter.slot(GROUPS, "t1"):
        pass
    assert clock.slept == []
    assert tracker.count("t1/admin.groups", HOUR) == 1
    hour, minute = limiter.usage(GROUPS, "t1")
    assert hour.describe() == "49/50 h"
    assert minute.describe() == "14/15 min"
    assert limiter.remaining(GROUPS, "t1") == 14


def test_quotas_are_per_tenant_and_endpoint():
    clock = FakeClock()
    limiter, _ = make_limiter(clock)
    with limiter.slot(GROUPS, "t1"):
        pass
    assert limiter.usage(GROUPS, "t2")[0].describe() == "50/50 h"
    assert limiter.usage(GROUPS, "t1")[0].describe() == "49/50 h"
    assert limiter.usage(get_endpoint("admin.apps"), "t1")[0].describe() == "200/200 h"


def test_request_waits_until_the_quota_frees_up():
    clock = FakeClock()
    limiter, _ = make_limiter(clock)
    for _ in range(15):  # the per-minute window of admin.groups is now full
        with limiter.slot(GROUPS, "t1"):
            pass
    assert limiter.wait_time(GROUPS, "t1") == pytest.approx(60)
    with limiter.slot(GROUPS, "t1"):
        pass
    assert clock.slept == [pytest.approx(60)]


def test_request_fails_fast_when_waiting_takes_too_long():
    clock = FakeClock()
    limiter, _ = make_limiter(clock, max_wait=30)
    for _ in range(15):
        with limiter.slot(GROUPS, "t1"):
            pass
    with pytest.raises(RateLimitError) as excinfo:
        with limiter.slot(GROUPS, "t1"):
            pass
    assert excinfo.value.retry_after == pytest.approx(60)
    assert "admin.groups" in str(excinfo.value)
    assert "50/h, 15/min" in str(excinfo.value)
    assert clock.slept == []


def test_max_wait_can_be_overridden_per_request():
    clock = FakeClock()
    limiter, _ = make_limiter(clock, max_wait=30)
    for _ in range(15):
        with limiter.slot(GROUPS, "t1"):
            pass
    with limiter.slot(GROUPS, "t1", max_wait=None):  # wait as long as needed
        pass
    assert clock.slept == [pytest.approx(60)]


def test_an_interrupt_ends_the_wait_for_quota():
    clock = FakeClock()
    limiter, tracker = make_limiter(clock)
    for _ in range(15):
        with limiter.slot(GROUPS, "t1"):
            pass
    stop = threading.Event()
    limiter.interrupt = stop
    threading.Timer(0.1, stop.set).start()

    with pytest.raises(Stopped, match="waiting for quota"):
        with limiter.slot(GROUPS, "t1"):
            pass

    assert (
        clock.slept == []
    )  # it did not "sleep" the 60 seconds: it waited for the event
    assert tracker.count("t1/admin.groups", MINUTE) == 15  # and spent no request


def test_a_wait_that_is_not_interrupted_is_a_wait():
    clock = FakeClock()
    limiter, _ = make_limiter(clock)
    limiter.interrupt = threading.Event()  # set up, but never set
    for _ in range(15):
        with limiter.slot(GROUPS, "t1"):
            pass

    def times_out(seconds):
        clock.now += seconds  # the time passes ...
        return False  # ... and the event was not set

    limiter.interrupt.wait = times_out
    with limiter.slot(GROUPS, "t1"):
        pass

    assert clock.slept == []  # the wait was the event's, not the sleep function's
    assert clock.now >= 1_000_000.0 + 60


def test_endpoint_without_quota_is_counted_but_never_waits():
    clock = FakeClock()
    limiter, tracker = make_limiter(clock)
    for _ in range(300):
        with limiter.slot(USER_APPS, "t1"):
            pass
    assert clock.slept == []
    assert tracker.count("t1/user.apps", HOUR) == 300
    assert limiter.remaining(USER_APPS, "t1") is None
    assert limiter.usage(USER_APPS, "t1") == []
    assert limiter.wait_time(USER_APPS, "t1") == 0


def test_a_failed_request_still_counts():
    clock = FakeClock()
    limiter, tracker = make_limiter(clock)
    with pytest.raises(RuntimeError):
        with limiter.slot(GROUPS, "t1"):
            raise RuntimeError("connection reset")
    assert tracker.count("t1/admin.groups", HOUR) == 1


def test_concurrency_is_limited():
    clock = FakeClock()
    limiter, _ = make_limiter(clock)
    inside = threading.Event()
    with limiter.slot(SCAN, "t1") as _first:
        # 16 requests may run at once: take the other 15 slots as well
        stack = []
        for _ in range(15):
            ctx = limiter.slot(SCAN, "t1")
            ctx.__enter__()
            stack.append(ctx)

        def seventeenth():
            with limiter.slot(SCAN, "t1"):
                inside.set()

        worker = threading.Thread(target=seventeenth, daemon=True)
        worker.start()
        assert not inside.wait(0.2)  # blocked: all 16 slots are taken
        stack.pop().__exit__(None, None, None)  # free one slot
        assert inside.wait(5)
        worker.join(5)
        for ctx in stack:
            ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0 seconds"),
        (45, "45 seconds"),
        (89, "89 seconds"),
        (600, "10 minutes"),
        (3900, "65 minutes"),
        (7300, "2 h 02 min"),
    ],
)
def test_format_wait(seconds, expected):
    assert format_wait(seconds) == expected


# ---------------------------------------------------------------------------
# blocking (the API asked to wait)
# ---------------------------------------------------------------------------


def test_a_blocked_endpoint_waits_until_the_block_ends():
    clock = FakeClock()
    limiter, _ = make_limiter(clock, max_wait=None)
    limiter.block(GROUPS, "t1", 90)
    assert limiter.wait_time(GROUPS, "t1") == pytest.approx(90)
    with limiter.slot(GROUPS, "t1"):
        pass
    assert clock.slept == [pytest.approx(90)]
    assert limiter.wait_time(GROUPS, "t1") == 0


def test_a_block_applies_to_endpoints_without_a_documented_quota_too():
    clock = FakeClock()
    limiter, _ = make_limiter(clock, max_wait=10)
    limiter.block(USER_APPS, "t1", 600)
    with pytest.raises(RateLimitError) as excinfo:
        with limiter.slot(USER_APPS, "t1"):
            pass
    assert excinfo.value.retry_after == pytest.approx(600)
    assert "user.apps" in str(excinfo.value)
    # other tenants and endpoints are not affected
    with limiter.slot(USER_APPS, "t2"):
        pass
    with limiter.slot(get_endpoint("user.groups"), "t1"):
        pass


def test_a_block_is_kept_between_runs_and_expires(tmp_path):
    path = tmp_path / "quota.json"
    clock = FakeClock()
    first = QuotaTracker(path, clock=clock)
    first.block("t1/admin.groups", 300)

    second = QuotaTracker(path, clock=clock)  # the next run of the command line
    assert second.next_slot("t1/admin.groups", None) == pytest.approx(300)
    clock.now += 301
    assert second.next_slot("t1/admin.groups", None) == 0


def test_a_shorter_block_does_not_shorten_a_longer_one():
    clock = FakeClock()
    tracker = QuotaTracker(clock=clock)
    tracker.block("k", 600)
    tracker.block("k", 30)
    assert tracker.next_slot("k", None) == pytest.approx(600)


def test_the_counters_are_put_in_place_with_the_helper_that_waits_for_readers(
    tmp_path, monkeypatch
):
    moved = []
    monkeypatch.setattr(
        "pbi_cli.core.ratelimit.replace_file",
        lambda source, target: moved.append(target),
    )
    path = tmp_path / "quota.json"

    QuotaTracker(path, clock=FakeClock()).record("k")

    assert moved == [path]
