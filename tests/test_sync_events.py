"""The audit event logs: one per UTC day, read page by page, sealed when complete."""

import json
from datetime import timedelta

import pytest
from sync_helpers import TENANT, World

from pbi_cli.core.sync.engine import COMPLETED, COMPLETED_WITH_FAILURES
from pbi_cli.core.sync.runners import DONE, FAILED, SKIPPED

EVENTS = "admin.activityevents"
PATH = r"^/admin/activityevents$"


@pytest.fixture
def world(tmp_path):
    # 3 days of 5 events, two per page; "now" is 2026-09-30 12:00 UTC
    return World(tmp_path, event_days=3, events_per_day=5, events_page_size=2)


def days(world):
    today = world.clock.now().date()
    return today, today - timedelta(days=1), today - timedelta(days=2)


def logs(world):
    return {d.day: d for d in world.store.event_days(TENANT, EVENTS)}


def ids(world, day):
    return [e["Id"] for e in world.store.read_events(TENANT, EVENTS, day)]


def test_each_day_gets_its_own_log_and_old_days_are_sealed(world):
    today, yesterday, before = days(world)

    report = world.run("activity", days=3)

    assert report.status == COMPLETED and report.counts[DONE] == 3
    found = logs(world)
    assert found[before].sealed
    assert not found[yesterday].sealed and not found[today].sealed
    # today is read only up to now (12:00): three of its five events have happened
    assert (found[before].rows, found[yesterday].rows, found[today].rows) == (5, 5, 3)
    assert not any(log.cursor for log in found.values())
    assert found[today].manifest["profile"] == "admin-nlm"


def test_a_day_is_read_page_by_page_and_the_window_is_the_day(world):
    today, yesterday, _ = days(world)

    world.run("activity", days=2)

    starts = [c for c in world.fake.calls if "startDateTime" in c.query]
    assert [c.query["startDateTime"] for c in starts] == [
        f"'{yesterday}T00:00:00.000Z'",
        f"'{today}T00:00:00.000Z'",
    ]
    assert starts[0].query["endDateTime"] == f"'{yesterday}T23:59:59.999Z'"
    # today is not over: the window ends now, not at the end of the day
    assert starts[1].query["endDateTime"] == f"'{today}T12:00:00.000Z'"
    continuations = [c for c in world.fake.calls if "continuationToken" in c.query]
    # 5 events make 3 pages and 3 events make 2 pages: 2 + 1 requests follow the first
    assert len(continuations) == 3 and len(world.fake.calls) == 5


def test_the_oldest_days_are_read_first(world):
    _, yesterday, before = days(world)

    world.run("activity", days=3)

    starts = [
        c.query["startDateTime"] for c in world.fake.calls if "startDateTime" in c.query
    ]
    assert starts[0].startswith(f"'{before}") and starts[1].startswith(f"'{yesterday}")


def test_a_second_run_reads_nothing_again(world):
    world.run("activity", days=3)
    world.fake.reset_calls()

    report = world.run("activity", days=3)

    assert report.counts[SKIPPED] == 3 and world.fake.calls == []


def test_an_open_day_is_read_again_and_only_the_new_events_are_added(world):
    today, yesterday, _ = days(world)
    world.run("activity", days=3)
    world.clock.advance(hours=5)  # 17:00: the event of 16:00 has happened
    events = []

    report = world.run("activity", days=3, on_event=events.append)

    assert report.counts[DONE] == 2 and report.counts[SKIPPED] == 1
    assert logs(world)[today].rows == 4
    assert ids(world, today) == [f"ev-{today:%Y%m%d}-00{k}" for k in range(4)]
    messages = {e.unit.day: e.outcome.message for e in events if e.kind == "unit"}
    assert messages[today] == "1 new events, day still open"
    assert messages[yesterday] == "0 new events, day still open"


def test_events_that_arrive_late_are_picked_up_before_a_day_is_sealed(world):
    _, yesterday, _ = days(world)
    world.run("activity", days=3)
    assert not logs(world)[yesterday].sealed
    world.fake.add_events(yesterday, 2)  # two events turn up late

    world.clock.advance(hours=2)
    world.run("activity", days=3)

    assert logs(world)[yesterday].rows == 7 and not logs(world)[yesterday].sealed


def test_a_day_is_sealed_once_the_late_events_have_had_a_day_to_arrive(world):
    _, yesterday, _ = days(world)
    world.run("activity", days=3)
    world.clock.advance(hours=13)  # 01:00 the next day: yesterday ended 25 hours ago

    world.run("activity", days=3)

    assert logs(world)[yesterday].sealed
    world.fake.reset_calls()
    world.clock.advance(hours=5)
    assert world.run("activity", days=3).counts[SKIPPED] >= 1
    assert world.fake.count(PATH) <= 4  # the sealed days are not read again


def test_the_time_to_wait_before_sealing_can_be_changed(world):
    _, yesterday, _ = days(world)

    world.run("activity", days=3, seal_grace=timedelta(0))

    assert logs(world)[yesterday].sealed and not logs(world)[days(world)[0]].sealed


def test_force_reads_open_days_again_but_never_a_complete_one(world):
    world.run("activity", days=3)
    world.fake.reset_calls()

    report = world.run("activity", days=3, force=True)

    assert report.counts[DONE] == 2 and report.counts[SKIPPED] == 1
    starts = [c for c in world.fake.calls if "startDateTime" in c.query]
    assert len(starts) == 2


def test_the_number_of_days_is_the_number_of_logs(world):
    world.run("activity", days=1)

    assert list(logs(world)) == [days(world)[0]]


# -- a read that is interrupted --------------------------------------------------------


def test_a_read_that_stops_half_way_continues_where_it_stopped(world):
    _, yesterday, _ = days(world)
    world.fake.fail(
        "GET",
        r"activityevents",
        500,
        query={"continuationToken": rf"^'{yesterday}\|2\|"},
        times=1,
    )

    first = world.run("activity", days=3)

    assert first.status == COMPLETED_WITH_FAILURES
    assert [key for key, _ in first.failures] == [f"{EVENTS}@{yesterday}"]
    half = logs(world)[yesterday]
    assert (
        half.rows == 2 and half.cursor
    )  # the first page is kept, with the link to go on
    world.fake.reset_calls()

    second = world.run("activity", days=3)

    assert second.status == COMPLETED
    requests = world.fake.calls_to(PATH)
    assert "continuationToken" in requests[0].query  # it did not start over
    assert not any("startDateTime" in c.query for c in requests)
    assert ids(world, yesterday) == [f"ev-{yesterday:%Y%m%d}-00{k}" for k in range(5)]
    assert logs(world)[yesterday].cursor is None


def test_a_link_that_is_no_longer_good_makes_the_read_start_over(world):
    _, yesterday, _ = days(world)
    token = {"continuationToken": rf"^'{yesterday}\|2\|"}
    world.fake.fail("GET", r"activityevents", 500, query=token, times=1)
    world.run("activity", days=3)
    world.fake.clear_faults()
    world.fake.fail(
        "GET", r"activityevents", 400, query=token, times=1
    )  # the link has expired
    world.fake.reset_calls()

    report = world.run("activity", days=3)

    assert report.status == COMPLETED
    assert sorted(ids(world, yesterday)) == sorted(set(ids(world, yesterday)))
    assert len(ids(world, yesterday)) == 5
    requests = world.fake.calls_to(PATH)
    assert "continuationToken" in requests[0].query  # tried to resume first
    assert "startDateTime" in requests[1].query  # then started over


def test_an_error_while_starting_a_day_is_a_failure_of_that_day_only(world):
    _, yesterday, before = days(world)
    world.fake.fail(
        "GET", r"activityevents", 500, query={"startDateTime": f"^'{yesterday}"}
    )

    report = world.run("activity", days=3)

    assert report.status == COMPLETED_WITH_FAILURES
    assert [key for key, _ in report.failures] == [f"{EVENTS}@{yesterday}"]
    assert report.counts[DONE] == 2 and report.counts[FAILED] == 1


def test_a_first_page_without_events_does_not_end_the_read(world):
    _, yesterday, _ = days(world)
    world.fake.empty_first_events_page = True

    world.run("activity", days=3)

    assert len(ids(world, yesterday)) == 5


def test_a_day_without_events_is_kept_as_an_empty_log(tmp_path):
    world = World(tmp_path, event_days=0)

    report = world.run("activity", days=2)

    assert report.status == COMPLETED
    assert [log.rows for log in logs(world).values()] == [0, 0]


def test_the_log_remembers_what_was_asked_but_not_the_token(world):
    _, yesterday, _ = days(world)
    world.run("activity", days=3)

    manifest = logs(world)[yesterday].manifest

    assert manifest["request"]["path"] == "/admin/activityevents"
    assert (
        manifest["request"]["params"]["startDateTime"] == f"'{yesterday}T00:00:00.000Z'"
    )
    assert "authorization" not in str(manifest).lower()


def test_the_last_page_ends_the_read_even_if_it_still_carries_a_link(world):
    _, yesterday, _ = days(world)
    world.fake.link_after_last_events_page = True

    world.run("activity", days=3)

    assert len(ids(world, yesterday)) == 5
    assert logs(world)[yesterday].cursor is None  # nothing left to resume
    assert world.fake.count(PATH) == 8  # 3 + 3 + 2 pages: no request beyond the last


# -- how far a read covers, and sealing --------------------------------------------------


def interrupt_today(world):
    """A read of today that stops after its first page: the link to go on keeps the window
    that ends at 12:00."""
    today = world.clock.now().date()
    world.fake.fail(
        "GET",
        r"activityevents",
        500,
        query={"continuationToken": rf"^'{today}\|2\|"},
        times=1,
    )
    world.run("activity", days=1)
    return today


def test_a_read_says_how_far_it_covers_the_day(world):
    today, yesterday, _ = days(world)

    world.run("activity", days=2)

    found = logs(world)
    assert (
        found[yesterday].covered_until.isoformat()
        == f"{yesterday}T23:59:59.999000+00:00"
    )
    assert found[today].covered_until.isoformat() == f"{today}T12:00:00+00:00"


def test_a_continued_read_that_covered_part_of_the_day_does_not_seal_it_short(world):
    today = interrupt_today(world)
    assert logs(world)[today].rows == 2 and logs(world)[today].cursor
    assert logs(world)[today].covered_until is None
    world.clock.advance(hours=40)  # the day is over, and so is the time to wait
    world.fake.reset_calls()

    report = world.run("activity", days=3)

    assert report.status == COMPLETED
    found = logs(world)[today]
    # the continued read ended at 12:00; the day was then read whole before it was sealed
    assert found.sealed and found.rows == 5
    assert ids(world, today) == [f"ev-{today:%Y%m%d}-00{k}" for k in range(5)]
    windows = [
        c.query["endDateTime"]
        for c in world.fake.calls_to(PATH)
        if c.query.get("startDateTime", "").startswith(f"'{today}")
    ]
    assert windows == [f"'{today}T23:59:59.999Z'"]


def test_a_continued_read_of_a_day_still_open_is_not_read_again(world):
    today = interrupt_today(world)
    world.clock.advance(hours=2)  # 14:00: today is not over
    world.fake.reset_calls()

    report = world.run("activity", days=1)

    assert report.status == COMPLETED
    assert not any("startDateTime" in c.query for c in world.fake.calls_to(PATH))
    found = logs(world)[today]
    assert not found.sealed
    assert found.covered_until.isoformat().startswith(f"{today}T12:00")


def test_a_day_stored_by_an_older_version_is_read_whole_before_it_is_sealed(world):
    today = interrupt_today(world)
    stored = logs(world)[today]
    manifest = dict(stored.manifest)
    del manifest["window_end"]  # written before reads said how far they cover
    (stored.directory / "manifest.json").write_text(json.dumps(manifest))
    world.clock.advance(hours=40)

    world.run("activity", days=3)

    found = logs(world)[today]
    assert found.sealed and found.rows == 5


def test_the_stored_events_are_read_once_per_day_not_once_per_page(world, monkeypatch):
    calls = []
    real = world.store.read_events

    def counting(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(world.store, "read_events", counting)

    world.run("activity", days=3)

    assert len(calls) == 3  # one per day, though the days take 3, 3 and 2 pages
