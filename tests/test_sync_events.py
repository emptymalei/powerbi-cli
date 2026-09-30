"""The audit event logs: one per UTC day, read page by page, sealed when complete."""

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
