"""The fake service must enforce the documented rules, or the tests built on it prove little."""

import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fake_powerbi import PREFIX, FakePowerBI

UTC = timezone.utc
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
BASE = f"https://api.test{PREFIX}"


@pytest.fixture
def fake():
    return FakePowerBI(clock=lambda: NOW, workspaces=7, reports=5, datasets=3)


def get(fake, path, **params):
    return fake.session().get(f"{BASE}{path}", params=params)


def post(fake, path, body, **params):
    return fake.session().post(f"{BASE}{path}", json=body, params=params)


# -- lists ---------------------------------------------------------------------------


def test_groups_need_top_and_page_with_skip(fake):
    assert get(fake, "/admin/groups").status_code == 400
    first = get(fake, "/admin/groups", **{"$top": 5}).json()
    rest = get(fake, "/admin/groups", **{"$top": 5, "$skip": 5}).json()

    assert [w["id"] for w in first["value"]] == [f"ws-{i:04d}" for i in range(1, 6)]
    assert [w["id"] for w in rest["value"]] == ["ws-0006", "ws-0007"]
    assert first["@odata.count"] == 7
    assert "modified_at" not in first["value"][0]


def test_groups_top_has_a_maximum(fake):
    assert get(fake, "/admin/groups", **{"$top": 5001}).status_code == 400


def test_other_lists_and_children(fake):
    assert len(get(fake, "/admin/reports").json()["value"]) == 5
    assert len(get(fake, "/admin/datasets").json()["value"]) == 3
    assert get(fake, "/admin/reports/rep-0002/users").json()["value"][0]["emailAddress"]
    assert get(fake, "/admin/reports/nope/users").status_code == 404
    source = get(fake, "/admin/datasets/ds-0001/datasources").json()["value"][0]
    assert source["connectionDetails"]["database"] == "ds-0001"


def test_user_scope(fake):
    groups = get(fake, "/groups").json()["value"]
    assert [g["id"] for g in groups] == ["ws-0001", "ws-0002", "ws-0003"]
    assert get(fake, "/groups/ws-0001/reports").status_code == 200
    assert get(fake, "/groups/ws-0005/reports").status_code == 403
    assert get(fake, "/groups/ws-0001/reports/rep-0001/pages").json()["value"]


def test_unknown_paths_are_404(fake):
    assert get(fake, "/admin/nothing").status_code == 404


# -- scans ---------------------------------------------------------------------------


def test_get_info_takes_one_to_a_hundred_workspaces(fake):
    assert (
        post(fake, "/admin/workspaces/getInfo", {"workspaces": []}).status_code == 400
    )
    too_many = {"workspaces": [f"ws-{i}" for i in range(101)]}
    assert post(fake, "/admin/workspaces/getInfo", too_many).status_code == 400
    ok = post(fake, "/admin/workspaces/getInfo", {"workspaces": ["ws-0001"]})
    assert ok.status_code == 202 and ok.json()["status"] == "NotStarted"


def test_a_scan_runs_then_succeeds_and_the_result_follows_the_flags(fake):
    fake.scan_polls = 2
    scan_id = post(
        fake,
        "/admin/workspaces/getInfo",
        {"workspaces": ["ws-0001", "ws-0002"]},
        datasourceDetails="true",
        getArtifactUsers="true",
    ).json()["id"]

    statuses = [
        get(fake, f"/admin/workspaces/scanStatus/{scan_id}").json()["status"]
        for _ in range(3)
    ]
    assert statuses == ["Running", "Running", "Succeeded"]
    result = get(fake, f"/admin/workspaces/scanResult/{scan_id}")

    assert result.status_code == 200
    body = result.json()
    assert [w["id"] for w in body["workspaces"]] == ["ws-0001", "ws-0002"]
    assert body["datasourceInstances"]
    users = [r for w in body["workspaces"] for r in w["reports"] if "users" in r]
    assert users


def test_a_scan_result_is_not_ready_before_the_scan_has_succeeded(fake):
    fake.scan_polls = 1
    scan_id = post(
        fake, "/admin/workspaces/getInfo", {"workspaces": ["ws-0001"]}
    ).json()["id"]

    assert get(fake, f"/admin/workspaces/scanResult/{scan_id}").status_code == 202
    get(fake, f"/admin/workspaces/scanStatus/{scan_id}")
    get(fake, f"/admin/workspaces/scanStatus/{scan_id}")
    assert get(fake, f"/admin/workspaces/scanResult/{scan_id}").status_code == 200


def test_a_failing_scan_reports_failed(fake):
    fake.failing_scan_workspaces = {"ws-0003"}
    scan_id = post(
        fake, "/admin/workspaces/getInfo", {"workspaces": ["ws-0001", "ws-0003"]}
    ).json()["id"]

    status = get(fake, f"/admin/workspaces/scanStatus/{scan_id}").json()
    assert status["status"] == "Failed" and status["error"]["code"] == "ScanFailed"


def test_scan_results_are_gone_after_a_day(fake):
    scan_id = post(
        fake, "/admin/workspaces/getInfo", {"workspaces": ["ws-0001"]}
    ).json()["id"]
    later = FakePowerBI(clock=lambda: NOW + timedelta(hours=25))
    later._scans = fake._scans

    assert get(later, f"/admin/workspaces/scanStatus/{scan_id}").status_code == 404


def test_in_flight_scans_are_counted(fake):
    ids = [
        post(fake, "/admin/workspaces/getInfo", {"workspaces": ["ws-0001"]}).json()[
            "id"
        ]
        for _ in range(3)
    ]
    assert fake.in_flight_scans == 3 and fake.max_in_flight_scans == 3
    for scan_id in ids:
        get(fake, f"/admin/workspaces/scanStatus/{scan_id}")
        get(fake, f"/admin/workspaces/scanResult/{scan_id}")
    assert fake.in_flight_scans == 0 and fake.max_in_flight_scans == 3


# -- modified workspaces -----------------------------------------------------------------


def test_modified_lists_everything_without_modified_since(fake):
    assert [w["id"] for w in get(fake, "/admin/workspaces/modified").json()] == [
        f"ws-{i:04d}" for i in range(1, 8)
    ]


def test_modified_since_filters_and_has_a_window(fake):
    fake.modify_workspace("ws-0004", NOW - timedelta(hours=1))
    since = (NOW - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    assert [
        w["id"]
        for w in get(fake, "/admin/workspaces/modified", modifiedSince=since).json()
    ] == ["ws-0004"]
    too_recent = (NOW - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    too_old = (NOW - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert (
        get(fake, "/admin/workspaces/modified", modifiedSince=too_recent).status_code
        == 400
    )
    assert (
        get(fake, "/admin/workspaces/modified", modifiedSince=too_old).status_code
        == 400
    )


# -- activity events -----------------------------------------------------------------------


def window(day, start="00:00:00.000", end="23:59:59.999"):
    return {
        "startDateTime": f"'{day}T{start}Z'",
        "endDateTime": f"'{day}T{end}Z'",
    }


def test_events_come_in_pages_chained_by_continuation_uri():
    fake = FakePowerBI(clock=lambda: NOW, events_per_day=5, events_page_size=2)
    day = NOW.date() - timedelta(days=1)
    url = f"{BASE}/admin/activityevents"
    page = fake.session().get(url, params=window(day)).json()
    seen = [e["Id"] for e in page["activityEventEntities"]]
    pages = 1
    while page["continuationUri"]:
        assert page["lastResultSet"] is False
        page = fake.session().get(page["continuationUri"]).json()
        seen += [e["Id"] for e in page["activityEventEntities"]]
        pages += 1

    assert page["lastResultSet"] is True and page["continuationUri"] is None
    assert pages == 3 and len(seen) == 5 == len(set(seen))


def test_events_rules_of_the_documentation(fake):
    day = NOW.date() - timedelta(days=1)
    url = f"{BASE}/admin/activityevents"
    s = fake.session()
    unquoted = {
        "startDateTime": f"{day}T00:00:00.000Z",
        "endDateTime": f"{day}T23:59:59.999Z",
    }
    assert s.get(url, params=unquoted).status_code == 400
    two_days = {
        "startDateTime": f"'{day}T00:00:00.000Z'",
        "endDateTime": f"'{NOW.date()}T00:00:00.000Z'",
    }
    assert s.get(url, params=two_days).status_code == 400
    old = NOW.date() - timedelta(days=30)
    assert s.get(url, params=window(old)).status_code == 400
    assert s.get(url, params=window(NOW.date() - timedelta(days=27))).status_code == 200


def test_events_respect_the_end_of_the_window():
    fake = FakePowerBI(clock=lambda: NOW, events_per_day=5, events_page_size=100)
    today = NOW.date()
    everything = (
        fake.session().get(f"{BASE}/admin/activityevents", params=window(today)).json()
    )
    until_noon = (
        fake.session()
        .get(f"{BASE}/admin/activityevents", params=window(today, end="12:00:00.000"))
        .json()
    )

    assert len(everything["activityEventEntities"]) == 5
    assert 0 < len(until_noon["activityEventEntities"]) < 5


def test_added_events_keep_counting_and_an_empty_first_page_can_be_simulated():
    fake = FakePowerBI(clock=lambda: NOW, events_per_day=2, events_page_size=10)
    day = NOW.date()
    fake.add_events(day, 2)
    fake.empty_first_events_page = True
    first = (
        fake.session().get(f"{BASE}/admin/activityevents", params=window(day)).json()
    )

    assert first["activityEventEntities"] == [] and first["lastResultSet"] is False
    second = fake.session().get(first["continuationUri"]).json()
    assert [e["Id"] for e in second["activityEventEntities"]] == [
        f"ev-{day:%Y%m%d}-00{k}" for k in range(4)
    ]


# -- faults, recording, threads ------------------------------------------------------------


def test_faults_apply_to_matching_requests_a_number_of_times(fake):
    fake.fail("GET", r"/admin/reports/rep-0003/users", 403, times=1)

    assert get(fake, "/admin/reports/rep-0003/users").status_code == 403
    assert get(fake, "/admin/reports/rep-0003/users").status_code == 200
    assert get(fake, "/admin/reports/rep-0001/users").status_code == 200


def test_throttling_sends_retry_after(fake):
    fake.throttle(r"/admin/apps", retry_after=7, times=1)

    response = get(fake, "/admin/apps", **{"$top": 10})
    assert response.status_code == 429 and response.headers["Retry-After"] == "7"
    assert get(fake, "/admin/apps", **{"$top": 10}).status_code == 200


def test_the_token_can_expire_after_some_requests(fake):
    fake.expire_token_after(2)

    assert [get(fake, "/admin/reports").status_code for _ in range(4)] == [
        200,
        200,
        401,
        401,
    ]
    fake.expire_token_after(None)
    assert get(fake, "/admin/reports").status_code == 200


def test_calls_are_recorded_and_counted(fake):
    get(fake, "/admin/groups", **{"$top": 2})
    post(fake, "/admin/workspaces/getInfo", {"workspaces": ["ws-0001"]}, lineage="true")

    assert fake.count(r"^/admin/groups$") == 1
    assert fake.count(r"getInfo", "POST") == 1
    call = fake.calls_to(r"getInfo")[0]
    assert call.query == {"lineage": "true"} and call.body == {
        "workspaces": ["ws-0001"]
    }
    fake.reset_calls()
    assert fake.calls == []


def test_it_can_be_shared_by_threads(fake):
    errors = []

    def work():
        try:
            for _ in range(25):
                assert get(fake, "/admin/reports").status_code == 200
        except Exception as error:  # pragma: no cover - only on failure
            errors.append(error)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == [] and len(fake.calls) == 200


def test_a_fault_can_be_limited_to_requests_with_certain_query_parameters():
    fake = FakePowerBI(clock=lambda: NOW, events_per_day=5, events_page_size=2)
    day = NOW.date() - timedelta(days=1)
    url = f"{BASE}/admin/activityevents"
    fake.fail(
        "GET", r"activityevents", 500, query={"continuationToken": r"\|2\|"}, times=1
    )

    first = fake.session().get(url, params=window(day))
    second = fake.session().get(first.json()["continuationUri"])
    retried = fake.session().get(first.json()["continuationUri"])

    assert first.status_code == 200  # no continuationToken: not matched
    assert second.status_code == 500 and retried.status_code == 200


def test_the_token_can_expire_a_number_of_requests_from_now(fake):
    get(fake, "/admin/reports")
    fake.expire_token_in(2)

    assert [get(fake, "/admin/reports").status_code for _ in range(4)] == [
        200,
        200,
        401,
        401,
    ]
