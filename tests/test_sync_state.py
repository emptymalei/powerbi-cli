"""What a sync remembers between runs."""

import threading
from datetime import datetime, timedelta, timezone

import pytest

from pbi_cli.core.scan import RESULT_KEPT, ScanFlags, ScanJob
from pbi_cli.core.store import LakeStore
from pbi_cli.core.sync.state import (
    DEFERRED,
    FAILED,
    MAX_MARKED,
    MAX_RUNS,
    STATE_NAME,
    SyncState,
)

UTC = timezone.utc
T0 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.now = T0
        self.mono = 0.0

    def __call__(self):
        return self.now

    def monotonic(self):
        return self.mono


@pytest.fixture
def store(tmp_path):
    return LakeStore(tmp_path / "lake")


@pytest.fixture
def clock():
    return Clock()


def make(store, clock, tenant="t1"):
    return SyncState(store, tenant, clock=clock, monotonic=clock.monotonic)


class CountingStore(LakeStore):
    def __init__(self, root):
        super().__init__(root)
        self.writes = 0

    def write_state(self, tenant, name, data):
        self.writes += 1
        super().write_state(tenant, name, data)


def read(store, tenant="t1"):
    return store.read_state(tenant, STATE_NAME)


# -- runs ---------------------------------------------------------------------------------


def test_a_new_state_is_empty(store, clock):
    state = make(store, clock)

    assert state.runs == [] and state.units == {} and state.last_run is None
    assert read(store) is None  # nothing is written until something happens


def test_a_run_is_recorded_when_it_begins_and_when_it_ends(store, clock):
    state = make(store, clock)

    run_id = state.begin_run(["groups", "apps"], {"force": False})
    assert run_id == "20261001T080000Z"
    assert read(store)["runs"][-1]["status"] == "running"

    clock.now += timedelta(minutes=2)
    state.end_run("completed", {"done": 2, "failed": 0}, "all good")

    run = read(store)["runs"][-1]
    assert run["status"] == "completed" and run["message"] == "all good"
    assert run["targets"] == ["groups", "apps"] and run["options"] == {"force": False}
    assert run["counts"] == {"done": 2, "failed": 0}
    assert run["started_at"] == T0.isoformat()
    assert run["finished_at"] == (T0 + timedelta(minutes=2)).isoformat()
    assert state.last_run["status"] == "completed"


def test_the_state_survives_a_new_process(store, clock):
    first = make(store, clock)
    first.begin_run(["groups"], {})
    first.mark(
        "admin.groups", FAILED, target="groups", endpoint="admin.groups", error="boom"
    )
    first.end_run("completed_with_failures", {"failed": 1})

    second = make(store, clock)

    assert second.last_run["status"] == "completed_with_failures"
    assert second.units["admin.groups"]["error"] == "boom"


def test_a_run_that_was_never_ended_is_interrupted(store, clock):
    first = make(store, clock)
    first.begin_run(["groups"], {})  # the process dies here

    assert make(store, clock).last_run["status"] == "interrupted"
    second = make(store, clock)
    second.begin_run(["apps"], {})
    assert [r["status"] for r in second.runs] == ["interrupted", "running"]


def test_only_the_latest_runs_are_kept(store, clock):
    state = make(store, clock)
    for n in range(MAX_RUNS + 5):
        clock.now += timedelta(seconds=1)
        state.begin_run([f"t{n}"], {})
        state.end_run("completed", {})

    assert len(state.runs) == MAX_RUNS
    assert state.runs[-1]["targets"] == [f"t{MAX_RUNS + 4}"]
    assert state.runs[0]["targets"] == ["t5"]


def test_ending_without_a_run_does_nothing(store, clock):
    make(store, clock).end_run("completed", {})

    assert read(store) is None


def test_a_state_of_another_layout_is_ignored(store, clock):
    store.write_state("t1", STATE_NAME, {"schema": 99, "runs": [{"id": "x"}]})

    assert make(store, clock).runs == []


# -- units ----------------------------------------------------------------------------------


def test_a_failed_unit_is_remembered_with_its_attempts(store, clock):
    state = make(store, clock)
    state.mark(
        "admin.reports.users?reportId=r1",
        FAILED,
        target="report-users",
        endpoint="admin.reports.users",
        error="forbidden (403)",
    )
    clock.now += timedelta(hours=1)
    state.mark(
        "admin.reports.users?reportId=r1",
        FAILED,
        target="report-users",
        endpoint="admin.reports.users",
        error="forbidden (403)",
    )

    unit = state.units_with(FAILED)["admin.reports.users?reportId=r1"]
    assert unit["attempts"] == 2 and unit["error"] == "forbidden (403)"
    assert unit["target"] == "report-users" and unit["status"] == FAILED
    assert unit["updated_at"] == (T0 + timedelta(hours=1)).isoformat()
    assert state.units_with(DEFERRED) == {}


def test_a_deferred_unit_remembers_when_to_come_back(store, clock):
    state = make(store, clock)
    state.mark(
        "k", DEFERRED, target="t", endpoint="e", error="quota", retry_after=900.0
    )

    assert state.units_with(DEFERRED)["k"]["retry_after"] == 900.0


def test_a_unit_that_is_done_since_is_forgotten(store, clock):
    state = make(store, clock)
    state.mark("k", FAILED, target="t", endpoint="e", error="x")
    state.clear("k")
    state.clear("never-marked")
    state.flush(force=True)

    assert state.units == {} and read(store)["units"] == {}


def test_only_failed_or_deferred_can_be_marked(store, clock):
    with pytest.raises(ValueError, match="failed or deferred"):
        make(store, clock).mark("k", "done", target="t", endpoint="e")


# -- writing --------------------------------------------------------------------------------


def test_writes_are_spaced_out_while_units_finish(tmp_path, clock):
    store = CountingStore(tmp_path / "lake")
    state = make(store, clock)

    for n in range(50):
        state.mark(f"k{n}", FAILED, target="t", endpoint="e")
    assert store.writes == 1  # the first mark wrote, the others waited

    clock.mono += 1.0
    state.mark("k-later", FAILED, target="t", endpoint="e")
    assert store.writes == 2
    state.flush()  # nothing changed since
    assert store.writes == 2
    assert len(read(store)["units"]) == 51  # the 50 and the later one


def test_a_forced_flush_writes_what_was_waiting(tmp_path, clock):
    store = CountingStore(tmp_path / "lake")
    state = make(store, clock)
    state.mark("a", FAILED, target="t", endpoint="e")
    state.mark("b", FAILED, target="t", endpoint="e")
    assert set(read(store)["units"]) == {"a"}

    state.flush(force=True)

    assert set(read(store)["units"]) == {"a", "b"}


def test_the_state_can_be_shared_by_threads(store, clock):
    state = make(store, clock)
    errors = []

    def work(n):
        try:
            for k in range(100):
                state.mark(f"u{n}-{k}", FAILED, target="t", endpoint="e")
                state.units_with(FAILED)
        except Exception as error:  # pragma: no cover - only on failure
            errors.append(error)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    state.flush(force=True)

    assert errors == [] and len(state.units) == 800
    assert len(read(store)["units"]) == 800


def test_the_state_is_kept_per_tenant(store, clock):
    a, b = make(store, clock, "t1"), make(store, clock, "t2")
    a.mark("k", FAILED, target="t", endpoint="e")
    a.flush(force=True)

    assert read(store, "t1")["units"] and read(store, "t2") is None
    assert b.units == {}


# -- scans ---------------------------------------------------------------------------------


def test_a_started_scan_is_written_at_once_and_found_again(tmp_path, clock):
    store = CountingStore(tmp_path / "lake")
    state = make(store, clock)
    flags = ScanFlags(lineage=True)

    state.set_job("batch1", ScanJob("scan-1", T0), flags)
    clock.mono += 0.0

    assert store.writes == 1 and state.pending_jobs == 1
    again = make(store, clock).job("batch1", flags)
    assert again == ScanJob("scan-1", T0)


def test_a_scan_with_other_flags_is_not_resumed(store, clock):
    state = make(store, clock)
    state.set_job("batch1", ScanJob("scan-1", T0), ScanFlags(lineage=True))

    assert state.job("batch1", ScanFlags()) is None
    assert state.job("other", ScanFlags(lineage=True)) is None


def test_a_scan_the_api_dropped_is_not_resumed(store, clock):
    state = make(store, clock)
    state.set_job("batch1", ScanJob("scan-1", T0), ScanFlags())
    clock.now = T0 + RESULT_KEPT

    assert state.job("batch1", ScanFlags()) is None


def test_a_collected_scan_is_forgotten(store, clock):
    state = make(store, clock)
    state.set_job("batch1", ScanJob("scan-1", T0), ScanFlags())

    state.clear_job("batch1")
    state.clear_job("batch1")
    state.flush(force=True)

    assert state.pending_jobs == 0 and read(store)["scan"]["jobs"] == {}


def test_old_scans_are_dropped_when_a_run_begins(store, clock):
    state = make(store, clock)
    state.set_job("old", ScanJob("scan-old", T0), ScanFlags())
    state.set_job("new", ScanJob("scan-new", T0 + timedelta(hours=20)), ScanFlags())
    clock.now = T0 + timedelta(hours=25)

    state.begin_run(["scan"], {})

    assert set(state.scan["jobs"]) == {"new"}


def test_the_last_complete_scan_is_the_baseline_for_the_same_flags(store, clock):
    state = make(store, clock)
    coverage = {"personal": True, "inactive": True}
    assert state.scan_baseline(ScanFlags(), coverage) is None

    state.set_scan_baseline(ScanFlags(lineage=True), coverage, T0)

    assert state.scan_baseline(ScanFlags(lineage=True), coverage) == T0
    # what it left out would be missing
    assert state.scan_baseline(ScanFlags(), coverage) is None
    assert make(store, clock).scan_baseline(ScanFlags(lineage=True), coverage) == T0


def test_a_scan_of_other_workspaces_is_no_baseline(store, clock):
    state = make(store, clock)
    state.set_scan_baseline(ScanFlags(), {"personal": False, "inactive": True}, T0)

    assert state.scan_baseline(ScanFlags(), {"personal": False, "inactive": True}) == T0
    assert (
        state.scan_baseline(ScanFlags(), {"personal": True, "inactive": True}) is None
    )


def test_damaged_scan_entries_are_dropped_not_fatal(store, clock):
    state = make(store, clock)
    state.scan["jobs"] = {
        "a": {"scan_id": "s"},
        "b": "junk",
        "c": {"scan_id": "s", "started_at": "no"},
    }

    assert state.job("a", ScanFlags()) is None
    assert state.job("b", ScanFlags()) is None
    assert state.job("c", ScanFlags()) is None
    state.begin_run(["scan"], {})
    assert state.scan["jobs"] == {}


def test_only_so_many_units_are_remembered_one_by_one(store, clock):
    state = make(store, clock)

    for n in range(MAX_MARKED + 7):
        state.mark(f"k{n}", DEFERRED, target="t", endpoint="e")
    state.flush(force=True)

    assert len(state.units) == MAX_MARKED and state.dropped == 7
    assert read(store)["units_dropped"] == 7
    state.mark(
        "k0", FAILED, target="t", endpoint="e"
    )  # one that is known is still updated
    assert state.units["k0"]["status"] == FAILED and state.dropped == 7
    state.begin_run(["x"], {})
    assert state.dropped == 0  # the next run judges the units afresh
