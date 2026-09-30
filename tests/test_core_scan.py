"""Tests for metadata scans: the job flow, resuming, and cutting results per workspace."""

from datetime import timedelta

import pytest
from core_helpers import NOW, Time, make_client
from fake_powerbi import FakePowerBI

from pbi_cli.core.scan import (
    MAX_WORKSPACES,
    RESULT_KEPT,
    ScanFlags,
    ScanJob,
    batch_key,
    chunked,
    latest_scan,
    run_scan,
    scan_params,
    split_scan_result,
    store_scan,
)
from pbi_cli.core.store import LakeStore
from pbi_cli.errors import ScanError, ScanTimeout, TokenExpiredError


@pytest.fixture
def clock():
    return Time()


@pytest.fixture
def fake(clock):
    return FakePowerBI(clock=clock.now, workspaces=8, reports=6, datasets=4)


@pytest.fixture
def client(fake, clock):
    return make_client(fake, clock=clock)[0]


def scan(client, clock, ids, flags=ScanFlags(), **kwargs):
    return run_scan(
        client,
        ids,
        flags,
        sleep=clock.sleep,
        monotonic=clock.time,
        clock=clock.now,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# chunking and flags
# ---------------------------------------------------------------------------


def test_chunked_keeps_the_order_and_fills_the_chunks():
    ids = [f"w{i}" for i in range(7)]

    assert chunked(ids, 3) == [["w0", "w1", "w2"], ["w3", "w4", "w5"], ["w6"]]
    assert chunked([], 3) == []
    assert [len(c) for c in chunked(list(range(250)))] == [100, 100, 50]
    with pytest.raises(ValueError):
        chunked(ids, 0)


def test_flags_are_named_as_the_api_names_them():
    flags = ScanFlags(lineage=True, dataset_schema=True)

    assert flags.query() == {
        "lineage": True,
        "datasourceDetails": False,
        "datasetSchema": True,
        "datasetExpressions": False,
        "getArtifactUsers": False,
    }
    assert flags.canonical()["lineage"] == "true"
    assert flags.canonical()["datasourceDetails"] == "false"
    assert flags.describe() == "lineage, datasetSchema"
    assert ScanFlags().describe() == "none"


def test_flags_survive_a_round_trip():
    flags = ScanFlags(datasource_details=True, get_artifact_users=True)

    assert ScanFlags.from_query(flags.canonical()) == flags
    assert ScanFlags.from_query(flags.query()) == flags
    assert ScanFlags.from_query({}) == ScanFlags()


# ---------------------------------------------------------------------------
# running a scan
# ---------------------------------------------------------------------------


def test_a_scan_is_started_polled_and_fetched(client, clock, fake):
    started = []

    run = scan(
        client,
        clock,
        ["ws-0001", "ws-0002"],
        ScanFlags(lineage=True),
        on_started=started.append,
    )

    (start,) = fake.calls_to(r"getInfo", "POST")
    assert start.body == {"workspaces": ["ws-0001", "ws-0002"]}
    assert start.query["lineage"] == "true" and start.query["datasetSchema"] == "false"
    assert fake.count(r"scanStatus") == 1 and fake.count(r"scanResult") == 1
    assert [w["id"] for w in run.result["workspaces"]] == ["ws-0001", "ws-0002"]
    assert run.scan_id == "scan-0001" and run.polls == 1 and run.resumed is False
    assert run.started_at == NOW and run.finished_at == NOW
    assert started == [ScanJob("scan-0001", NOW)]
    assert clock.slept == []


def test_the_scan_id_is_reported_before_the_first_status_check(client, clock, fake):
    order = []
    fake_calls = fake.calls

    def on_started(job):
        order.append(("started", len(fake_calls)))

    scan(client, clock, ["ws-0001"], on_started=on_started)

    assert order == [("started", 1)]  # only the getInfo call had been made


def test_a_running_scan_is_polled_until_it_succeeds(client, clock, fake):
    fake.scan_polls = 2
    polls = []

    run = scan(
        client,
        clock,
        ["ws-0001"],
        interval=5.0,
        on_poll=lambda attempt, status, wait: polls.append((attempt, status, wait)),
    )

    assert run.polls == 3
    assert clock.slept == [5.0, 5.0]
    assert polls == [(1, "Running", 5.0), (2, "Running", 5.0)]
    assert fake.count(r"scanStatus") == 3 and fake.count(r"scanResult") == 1


def test_a_result_that_is_not_ready_yet_is_asked_for_again(client, clock, fake):
    fake.result_not_ready_times = 2

    run = scan(client, clock, ["ws-0001"], interval=3.0)

    assert run.result["workspaces"]
    assert fake.count(r"scanResult") == 3
    assert clock.slept == [3.0, 3.0]


def test_a_scan_that_does_not_finish_times_out(client, clock, fake):
    fake.scan_polls = 10**6

    with pytest.raises(ScanTimeout, match="did not complete within 12.0s") as info:
        scan(client, clock, ["ws-0001"], interval=5.0, timeout=12.0)

    assert isinstance(info.value, ScanError)  # callers may treat both alike
    assert info.value.scan_id == "scan-0001"
    assert clock.slept == [5.0, 5.0, 2.0]  # the last wait is cut to the deadline


def test_a_failed_scan_is_an_error_with_the_reason(client, clock, fake):
    fake.failing_scan_workspaces = {"ws-0002"}

    with pytest.raises(ScanError, match="failed") as info:
        scan(client, clock, ["ws-0001", "ws-0002"])

    assert "ScanFailed" in str(info.value) and info.value.scan_id == "scan-0001"
    assert not isinstance(info.value, ScanTimeout)  # its id is of no use any more
    assert fake.count(r"scanResult") == 0


def test_an_error_in_the_body_of_the_answer_is_an_error(client, clock, fake):
    fake.fail(
        "POST",
        r"getInfo",
        202,
        body={"error": {"code": "CapacityNotActive", "message": "nope"}},
    )

    with pytest.raises(ScanError, match="CapacityNotActive"):
        scan(client, clock, ["ws-0001"])


def test_an_answer_without_a_scan_id_is_an_error(client, clock, fake):
    fake.fail("POST", r"getInfo", 202, body={"status": "NotStarted"})

    with pytest.raises(ScanError, match="Unexpected answer"):
        scan(client, clock, ["ws-0001"])


@pytest.mark.parametrize("count", [0, MAX_WORKSPACES + 1])
def test_a_scan_takes_one_to_a_hundred_workspaces(client, clock, fake, count):
    with pytest.raises(ValueError, match="1 to 100"):
        scan(client, clock, [f"ws-{i}" for i in range(count)])

    assert fake.calls == []


def test_a_hundred_workspaces_are_one_scan(clock):
    fake = FakePowerBI(clock=clock.now, workspaces=100)
    client, _ = make_client(fake, clock=clock)

    run = scan(client, clock, [w["id"] for w in fake.workspaces])

    assert len(run.result["workspaces"]) == 100
    assert fake.count(r"getInfo", "POST") == 1


def test_the_api_rejecting_the_workspaces_is_reported(client, clock, fake):
    fake.fail(
        "POST",
        r"getInfo",
        400,
        body={"error": {"code": "InvalidRequest", "message": "bad"}},
    )

    with pytest.raises(Exception, match="invalid request"):
        scan(client, clock, ["ws-0001"])


# ---------------------------------------------------------------------------
# resuming
# ---------------------------------------------------------------------------


def test_a_scan_that_was_started_earlier_is_not_started_again(client, clock, fake):
    first = scan(client, clock, ["ws-0001"], on_started=lambda job: None)
    fake.reset_calls()
    job = ScanJob(first.scan_id, NOW)
    started = []

    # the result was fetched already, but the API still knows the scan
    run = scan(client, clock, ["ws-0001"], resume=job, on_started=started.append)

    assert fake.count(r"getInfo") == 0 and started == []
    assert run.resumed is True and run.scan_id == first.scan_id
    assert run.result["workspaces"][0]["id"] == "ws-0001"


def test_a_scan_the_api_forgot_is_started_anew(client, clock, fake):
    started = []

    run = scan(
        client,
        clock,
        ["ws-0001"],
        resume=ScanJob("scan-long-gone", NOW - timedelta(hours=30)),
        on_started=started.append,
    )

    assert run.resumed is False and run.scan_id == "scan-0001"
    assert started == [ScanJob("scan-0001", NOW)]
    assert fake.count(r"scanStatus/scan-long-gone") == 1
    assert fake.count(r"getInfo", "POST") == 1


def test_a_job_knows_when_the_result_is_gone():
    job = ScanJob("s", NOW)

    assert not job.expired(NOW + RESULT_KEPT - timedelta(seconds=1))
    assert job.expired(NOW + RESULT_KEPT)


def test_an_expired_token_leaves_the_scan_to_be_resumed(client, clock, fake):
    fake.scan_polls = 5
    fake.expire_token_after(2)  # getInfo and one status check are answered
    jobs = []

    with pytest.raises(TokenExpiredError):
        scan(client, clock, ["ws-0001"], on_started=jobs.append)

    assert jobs == [ScanJob("scan-0001", NOW)]
    fake.expire_token_after(None)
    fake.reset_calls()
    resumed = scan(client, clock, ["ws-0001"], resume=jobs[0])
    assert resumed.resumed is True and fake.count(r"getInfo") == 0


# ---------------------------------------------------------------------------
# cutting a result per workspace
# ---------------------------------------------------------------------------


def instance(ident, **extra):
    return {"datasourceType": "Sql", "datasourceId": ident, **extra}


RESULT = {
    "workspaces": [
        {
            "id": "a",
            "datasets": [{"datasourceUsages": [{"datasourceInstanceId": "i1"}]}],
            "dataflows": [
                {
                    "datasourceUsages": [{"datasourceInstanceId": "i2"}],
                    "misconfiguredDatasourceUsages": [{"datasourceInstanceId": "m1"}],
                }
            ],
        },
        {
            "id": "b",
            "datasets": [{"datasourceUsages": [{"datasourceInstanceId": "i2"}]}],
        },
        {"id": "c", "reports": [{"id": "r"}]},
    ],
    "datasourceInstances": [instance("i1"), instance("i2"), instance("i3")],
    "misconfiguredDatasourceInstances": [instance("m1"), instance("m2")],
}


def test_each_workspace_gets_its_own_result():
    pieces = split_scan_result(RESULT)

    assert list(pieces) == ["a", "b", "c"]
    assert [w["id"] for w in pieces["a"]["workspaces"]] == ["a"]
    assert pieces["a"]["workspaces"][0] is RESULT["workspaces"][0]


def test_each_workspace_gets_the_data_sources_it_uses():
    pieces = split_scan_result(RESULT)
    ids = lambda piece, key: [i["datasourceId"] for i in piece[key]]  # noqa: E731

    assert ids(pieces["a"], "datasourceInstances") == ["i1", "i2"]
    assert ids(pieces["b"], "datasourceInstances") == ["i2"]
    assert ids(pieces["c"], "datasourceInstances") == []
    assert ids(pieces["a"], "misconfiguredDatasourceInstances") == ["m1"]
    assert ids(pieces["b"], "misconfiguredDatasourceInstances") == []


def test_a_data_source_without_an_id_cannot_be_attributed_and_is_not_lost():
    result = {
        "workspaces": [{"id": "a"}, {"id": "b"}],
        "datasourceInstances": [instance(""), {"datasourceType": "Web"}],
        "misconfiguredDatasourceInstances": [],
    }

    pieces = split_scan_result(result)

    assert len(pieces["a"]["datasourceInstances"]) == 2
    assert len(pieces["b"]["datasourceInstances"]) == 2


def test_other_fields_and_missing_lists_are_handled():
    pieces = split_scan_result({"workspaces": [{"id": "a"}], "futureField": {"x": 1}})

    assert pieces["a"] == {
        "futureField": {"x": 1},
        "workspaces": [{"id": "a"}],
        "datasourceInstances": [],
        "misconfiguredDatasourceInstances": [],
    }
    assert split_scan_result({}) == {}


def test_a_real_scan_result_is_cut_per_workspace(client, clock, fake):
    run = scan(
        client,
        clock,
        ["ws-0001", "ws-0002", "ws-0003"],
        ScanFlags(datasource_details=True),
    )

    pieces = split_scan_result(run.result)

    assert list(pieces) == ["ws-0001", "ws-0002", "ws-0003"]
    for workspace_id, piece in pieces.items():
        workspace = piece["workspaces"][0]
        used = {
            usage["datasourceInstanceId"]
            for dataset in workspace["datasets"]
            for usage in dataset["datasourceUsages"]
        }
        assert {i["datasourceId"] for i in piece["datasourceInstances"]} == used


# ---------------------------------------------------------------------------
# the lake
# ---------------------------------------------------------------------------


def test_a_batch_is_the_same_whatever_the_order():
    assert batch_key(["b", "a", "c"]) == batch_key(["c", "a", "b"])
    assert batch_key(["a", "b"]) != batch_key(["a", "c"])
    assert len(batch_key(["a"])) == 12


def test_scans_are_identified_by_workspaces_and_flags():
    plain = scan_params(["a", "b"], ScanFlags())
    lineage = scan_params(["b", "a"], ScanFlags(lineage=True))

    assert plain["batch"] == lineage["batch"]
    assert plain != lineage
    assert plain["lineage"] == "false" and lineage["lineage"] == "true"


def test_a_scan_is_stored_as_a_job_and_found_again(client, clock, fake, tmp_path):
    store = LakeStore(tmp_path / "lake")
    ids = ["ws-0002", "ws-0001"]
    flags = ScanFlags(lineage=True)
    run = scan(client, clock, ids, flags)

    snapshot = store_scan(store, "t1", run, ids, flags, profile="admin-nlm")

    manifest = snapshot.manifest
    assert manifest["kind"] == "job" and manifest["endpoint"] == "admin.scan.result"
    assert manifest["scan_id"] == run.scan_id
    assert manifest["workspace_ids"] == ["ws-0001", "ws-0002"]
    assert manifest["flags"]["lineage"] == "true" and manifest["rows"] == 2
    assert manifest["profile"] == "admin-nlm"
    assert manifest["request"]["method"] == "POST"
    assert manifest["request"]["body_sha256"]
    assert snapshot.load() == run.result
    found = latest_scan(store, "t1", ["ws-0001", "ws-0002"], flags)
    assert found is not None and found.version == snapshot.version
    assert latest_scan(store, "t1", ids, ScanFlags()) is None
    assert latest_scan(store, "t2", ids, flags) is None


def test_the_lake_browser_lists_stored_scans(client, clock, fake, tmp_path):
    store = LakeStore(tmp_path / "lake")
    run = scan(client, clock, ["ws-0001"])
    store_scan(store, "t1", run, ["ws-0001"], ScanFlags())

    assert store.endpoints("t1") == ["admin.scan.result"]
    (pset,) = store.parameter_sets("t1", "admin.scan.result")
    assert pset.params["batch"] == batch_key(["ws-0001"])
