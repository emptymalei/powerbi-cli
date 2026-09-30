"""Scans in a sync: batches of 100, incremental from the last complete scan, resumable."""

import time
from datetime import timedelta

import pytest
from sync_helpers import TENANT, World

from pbi_cli.core.scan import ScanFlags
from pbi_cli.core.sync.engine import COMPLETED, COMPLETED_WITH_FAILURES, TOKEN_EXPIRED
from pbi_cli.core.sync.runners import DEFERRED, DONE, FAILED, SKIPPED

RESULT = "admin.scan.result"
POST = (r"getInfo", "POST")


@pytest.fixture
def world(tmp_path):
    return World(tmp_path, workspaces=250, reports=6, datasets=4)


def scanned(world):
    """The workspace ids of every stored scan."""
    found = set()
    for stored in world.store.parameter_sets(TENANT, RESULT):
        found.update(stored.latest.manifest["workspace_ids"])
    return found


def baseline(world):
    return world.engine._session(world.options("scan")).state.scan.get(
        "last_success_at"
    )


def last_modified_call(world):
    return world.fake.calls_to(r"/modified$")[-1]


# ---------------------------------------------------------------------------
# a full scan
# ---------------------------------------------------------------------------


def test_a_full_scan_covers_every_workspace_in_batches_of_100(world):
    report = world.run("scan")

    assert report.status == COMPLETED
    assert (
        report.by_target["scan"][DONE] == 1 + 3
    )  # the list of workspaces, three scans
    calls = world.fake.calls_to(*POST)
    assert sorted(len(c.body["workspaces"]) for c in calls) == [50, 100, 100]
    assert len(world.store.parameter_sets(TENANT, RESULT)) == 3
    assert scanned(world) == {w["id"] for w in world.fake.workspaces}


def test_a_scan_is_stored_as_a_job_with_what_was_asked(world):
    world.run("scan")

    stored = world.store.parameter_sets(TENANT, RESULT)[0].latest
    manifest = stored.manifest
    assert manifest["kind"] == "job" and manifest["endpoint"] == RESULT
    assert (
        manifest["scan_id"].startswith("scan-") and manifest["profile"] == "admin-nlm"
    )
    assert len(manifest["workspace_ids"]) in (50, 100)
    assert manifest["flags"]["lineage"] == "false"
    assert manifest["rows"] == len(manifest["workspace_ids"])
    assert [w["id"] for w in stored.load()["workspaces"]] == manifest["workspace_ids"]


def test_the_scan_flags_are_sent_and_recorded(world):
    flags = ScanFlags(lineage=True, datasource_details=True)

    world.run("scan", scan_flags=flags)

    query = world.fake.calls_to(*POST)[0].query
    assert query["lineage"] == "true" and query["datasourceDetails"] == "true"
    assert query["datasetSchema"] == "false"
    sets = world.store.parameter_sets(TENANT, RESULT)
    assert all(s.params["lineage"] == "true" for s in sets)
    assert all(s.params["getArtifactUsers"] == "false" for s in sets)
    first = next(s for s in sets if "ws-0001" in s.latest.manifest["workspace_ids"])
    assert first.latest.load()["datasourceInstances"]  # ws-0001 has a dataset


def test_the_list_of_workspaces_is_asked_every_time_and_not_stored(world):
    world.run("scan")

    assert world.stored("admin.workspaces.modified") is None
    world.fake.reset_calls()
    world.run("scan")
    assert world.fake.count(r"/modified$") == 1


def test_personal_and_inactive_workspaces_can_be_left_out(world):
    world.run("scan", exclude_personal=True, exclude_inactive=True)

    query = last_modified_call(world).query
    assert query["excludePersonalWorkspaces"] == "true"
    assert query["excludeInActiveWorkspaces"] == "true"


def test_a_scan_that_takes_a_few_checks_is_polled(tmp_path):
    world = World(tmp_path, workspaces=40, scan_polls=2)

    report = world.run("scan", scan_interval=5.0)

    assert report.status == COMPLETED
    assert world.fake.count(r"scanStatus") == 3
    assert world.clock.slept == [5.0, 5.0]


# ---------------------------------------------------------------------------
# continuing from the last scan
# ---------------------------------------------------------------------------


def test_the_next_scan_continues_from_the_start_of_the_last_one(world):
    first_start = world.clock.now()
    world.run("scan")
    assert "modifiedSince" not in last_modified_call(world).query  # the first is full
    world.clock.advance(hours=2)
    changed = [world.fake.workspaces[3]["id"], world.fake.workspaces[200]["id"]]
    for workspace_id in changed:
        world.fake.modify_workspace(
            workspace_id, world.clock.now() - timedelta(minutes=30)
        )
    world.fake.reset_calls()

    report = world.run("scan")

    assert report.status == COMPLETED
    since = f"{first_start - timedelta(hours=1):%Y-%m-%dT%H:%M:%S}.000Z"  # an hour of overlap
    assert last_modified_call(world).query["modifiedSince"] == since
    (scan,) = world.fake.calls_to(*POST)
    assert sorted(scan.body["workspaces"]) == sorted(changed)
    assert baseline(world) == world.clock.now().isoformat()


def test_nothing_is_scanned_when_nothing_changed(world):
    world.run("scan")
    world.clock.advance(hours=2)
    world.fake.reset_calls()

    report = world.run("scan")

    assert report.status == COMPLETED and report.counts[DONE] == 1
    assert world.fake.count(*POST) == 0
    assert (
        baseline(world) == world.clock.now().isoformat()
    )  # and the next one continues here


def test_a_scan_right_after_another_stays_within_what_the_api_accepts(world):
    world.run("scan")
    world.fake.reset_calls()

    report = world.run("scan")  # no time has passed at all

    assert report.status == COMPLETED and not report.failures
    since = last_modified_call(world).query["modifiedSince"]
    assert since.endswith(
        "T11:00:00.000Z"
    )  # an hour back, older than the 30 minutes needed


def test_a_baseline_too_old_for_the_api_means_a_full_scan(world):
    world.run("scan")
    world.clock.advance(days=30)
    world.fake.reset_calls()

    report = world.run("scan")

    assert report.status == COMPLETED
    assert "modifiedSince" not in last_modified_call(world).query
    assert world.fake.count(*POST) == 3


def test_other_flags_mean_a_full_scan_again(world):
    world.run("scan")
    world.clock.advance(hours=1)

    world.run("scan", scan_flags=ScanFlags(lineage=True))

    assert "modifiedSince" not in last_modified_call(world).query
    assert (
        len(world.store.parameter_sets(TENANT, RESULT)) == 6
    )  # three per set of flags


def test_other_coverage_means_a_full_scan_again(world):
    world.run("scan", exclude_personal=True)
    world.clock.advance(hours=1)

    world.run("scan", exclude_personal=False)

    assert "modifiedSince" not in last_modified_call(world).query
    world.clock.advance(hours=1)
    world.run("scan", exclude_personal=False)
    assert "modifiedSince" in last_modified_call(world).query


def test_a_full_scan_on_request_does_not_repeat_what_is_fresh(world):
    world.run("scan")
    world.fake.reset_calls()

    report = world.run("scan", full_scan=True)

    assert "modifiedSince" not in last_modified_call(world).query
    assert report.by_target["scan"][SKIPPED] == 3 and world.fake.count(*POST) == 0


def test_force_scans_everything_again_not_only_what_changed(world):
    world.run("scan")
    world.clock.advance(hours=2)  # nothing has changed since
    world.fake.reset_calls()

    world.run("scan", force=True)

    assert "modifiedSince" not in last_modified_call(world).query
    assert world.fake.count(*POST) == 3


# ---------------------------------------------------------------------------
# what blocks the baseline, and what is resumed
# ---------------------------------------------------------------------------


def test_a_failed_scan_leaves_the_others_and_blocks_the_baseline(world):
    world.fake.failing_scan_workspaces = {"ws-0150"}  # in the second batch

    report = world.run("scan")

    assert report.status == COMPLETED_WITH_FAILURES
    assert report.counts[FAILED] == 1 and report.counts[DONE] == 1 + 2
    assert baseline(world) is None
    assert len(world.store.parameter_sets(TENANT, RESULT)) == 2
    assert world.state()["scan"].get("jobs", {}) == {}  # a failed scan is not resumed

    world.fake.failing_scan_workspaces = set()
    world.fake.reset_calls()
    again = world.run("scan")

    assert again.status == COMPLETED
    assert world.fake.count(*POST) == 1  # only the batch that failed
    assert again.by_target["scan"][SKIPPED] == 2
    assert baseline(world) is not None


def test_a_failing_list_of_workspaces_is_a_failure_and_no_baseline(world):
    world.fake.fail("GET", r"/modified$", 500)

    report = world.run("scan")

    assert report.status == COMPLETED_WITH_FAILURES and baseline(world) is None
    assert world.fake.count(*POST) == 0


def test_a_scan_that_takes_too_long_is_continued_by_the_next_run(tmp_path):
    world = World(tmp_path, workspaces=50, scan_polls=10**6)

    report = world.run("scan", scan_timeout=12.0, scan_interval=5.0)

    assert report.counts[FAILED] == 1
    assert "did not complete within 12.0s" in report.failures[0][1]
    assert len(world.state()["scan"]["jobs"]) == 1  # the scan may still succeed
    world.fake.scan_polls = 0  # it does
    world.fake.reset_calls()
    events = []

    again = world.run("scan", on_event=events.append)

    assert again.status == COMPLETED
    assert world.fake.count(*POST) == 0  # continued, not started again
    assert world.state()["scan"]["jobs"] == {}
    assert len(world.store.parameter_sets(TENANT, RESULT)) == 1
    assert any(
        "continued an earlier scan" in e.outcome.message
        for e in events
        if e.kind == "unit"
    )


def test_an_expired_token_leaves_a_started_scan_to_be_continued(tmp_path):
    world = World(tmp_path, workspaces=250, scan_polls=3)
    world.fake.expire_token_after(3)  # the list, the start of batch 1, one check of it

    report = world.run("scan")

    assert report.status == TOKEN_EXPIRED and "pbi auth" in report.message
    jobs = world.state()["scan"]["jobs"]
    assert len(jobs) == 1  # the started scan is remembered; the others never started
    assert next(iter(jobs.values()))["scan_id"] == "scan-0001"
    assert baseline(world) is None

    world.fake.expire_token_after(None)
    world.fake.scan_polls = 0
    world.fake.reset_calls()
    again = world.run("scan")

    assert again.status == COMPLETED
    assert world.fake.count(*POST) == 2  # batches 2 and 3: batch 1 was continued
    assert world.state()["scan"]["jobs"] == {}
    assert len(world.store.parameter_sets(TENANT, RESULT)) == 3


def test_a_scan_the_api_forgot_is_started_again(tmp_path):
    world = World(tmp_path, workspaces=50, scan_polls=10**6)
    world.run("scan", scan_timeout=6.0, scan_interval=5.0)
    world.fake.scan_polls = 0
    world.fake.expire_scans()  # after 24 hours the API no longer has it
    world.fake.reset_calls()

    report = world.run("scan")

    assert report.status == COMPLETED
    assert world.fake.count(*POST) == 1 and world.fake.count(r"scanStatus") == 2
    assert world.state()["scan"]["jobs"] == {}


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------


def test_scans_run_side_by_side_but_never_more_than_the_workers(tmp_path):
    world = World(
        tmp_path,
        workspaces=800,
        scan_polls=3,
        sleep=lambda seconds: time.sleep(0.005),
    )

    report = world.run("scan", workers=4, scan_interval=1.0)

    assert report.status == COMPLETED and report.by_target["scan"][DONE] == 1 + 8
    assert 2 <= world.fake.max_in_flight_scans <= 4
    assert len(world.store.parameter_sets(TENANT, RESULT)) == 8
    assert scanned(world) == {w["id"] for w in world.fake.workspaces}


def test_a_failure_of_another_target_does_not_hold_back_the_scan_baseline(world):
    world.fake.fail("GET", r"^/admin/groups$", 500)

    report = world.run("groups", "scan")

    assert report.status == COMPLETED_WITH_FAILURES and report.failures
    assert baseline(world) is not None  # every workspace was scanned


def test_a_scan_that_was_cut_short_sets_no_baseline(world):
    world.fake.expire_token_after(2)  # the list of workspaces and one scan

    report = world.run("scan")

    assert report.status == TOKEN_EXPIRED and baseline(world) is None


def test_scans_held_back_by_a_quota_set_no_baseline_and_are_done_later(world):
    # Power BI asks to wait more than an hour before the next scan is started
    world.fake.fail("POST", r"getInfo", 429, times=1, headers={"Retry-After": "4000"})

    report = world.run("scan")

    assert report.status == COMPLETED
    assert report.counts[DEFERRED] == 3 and report.counts[DONE] == 1
    assert baseline(world) is None
    assert 3900 < report.retry_after <= 4000
    assert world.fake.count(*POST) == 1  # the others were not even tried

    world.clock.advance(seconds=4001)
    again = world.run("scan")

    assert again.status == COMPLETED and again.counts[DONE] == 1 + 3
    assert baseline(world) is not None
