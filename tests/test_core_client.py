"""Tests for the Power BI client, against a scripted fake API (no network)."""

import json
import re
from datetime import datetime, timedelta

import pytest
import requests
from core_helpers import (
    BASE,
    NOW,
    UTC,
    FakeAdapter,
    Time,
    make_client,
    make_response,
    make_token,
)
from loguru import logger

from pbi_cli.core import client as client_module
from pbi_cli.core.auth import Credentials
from pbi_cli.core.client import FOREVER, PowerBIClient, Throttled, body_hash, rows_of
from pbi_cli.core.ratelimit import Limiter, QuotaTracker
from pbi_cli.core.registry import get_endpoint
from pbi_cli.core.store import LakeStore
from pbi_cli.errors import (
    ApiError,
    OfflineCacheMiss,
    PBIError,
    RateLimitError,
    TokenExpiredError,
)

T0 = NOW


def ok(body, **kw):
    return make_response(200, body, **kw)


# ---------------------------------------------------------------------------
# one request
# ---------------------------------------------------------------------------


def test_request_sends_the_token_and_returns_the_json():
    adapter = FakeAdapter().add("GET", "/admin/apps", ok({"value": [{"id": "a1"}]}))
    client, _ = make_client(adapter, token=make_token())
    response = client.request("admin.apps", {"$top": 5})

    assert response.status == 200
    assert response.data == {"value": [{"id": "a1"}]}
    sent = adapter.requests[0]
    assert sent.url == f"{BASE}/admin/apps?%24top=5"
    assert sent.headers["Authorization"].startswith("Bearer ")
    assert sent.headers["Accept"] == "application/json"
    assert sent.headers["User-Agent"].startswith("pbi-cli")


def test_query_values_and_path_values_are_encoded():
    adapter = FakeAdapter().add(
        "GET", "artifactAccess", ok({"artifactAccessEntities": []})
    )
    client, _ = make_client(adapter)
    client.request(
        "admin.users.artifact_access",
        {"userId": "guest#EXT#@x.com", "artifactTypes": ["Report", "Dashboard"]},
    )
    assert adapter.requests[0].url == (
        f"{BASE}/admin/users/guest%23EXT%23%40x.com/artifactAccess?artifactTypes=Dashboard%2CReport"
    )

    adapter = FakeAdapter().add("GET", "/admin/groups", ok({"value": []}))
    client, _ = make_client(adapter)
    client.request("admin.groups", {"$filter": "state eq 'Deleted'", "$top": 1})
    assert adapter.requests[0].url.endswith(
        "?%24filter=state%20eq%20%27Deleted%27&%24top=1"
    )


def test_post_sends_a_json_body():
    adapter = FakeAdapter().add(
        "POST", "getInfo", make_response(202, {"id": "scan-1", "status": "NotStarted"})
    )
    client, _ = make_client(adapter)
    response = client.request(
        "admin.scan.start",
        {"lineage": True, "datasetSchema": False},
        body={"workspaces": ["w1", "w2"]},
    )
    assert response.status == 202
    assert response.data["id"] == "scan-1"
    sent = adapter.requests[0]
    assert sent.method == "POST"
    assert sent.url.endswith(
        "/admin/workspaces/getInfo?datasetSchema=false&lineage=true"
    )
    assert json.loads(sent.body) == {"workspaces": ["w1", "w2"]}
    assert sent.headers["Content-Type"] == "application/json"


def test_an_empty_body_is_none():
    adapter = FakeAdapter().add("GET", "scanResult", make_response(202))
    client, _ = make_client(adapter)
    response = client.request("admin.scan.result", {"scanId": "s1"})
    assert (response.status, response.data) == (202, None)


def test_unknown_parameters_fail_before_anything_is_sent():
    adapter = FakeAdapter()
    client, _ = make_client(adapter)
    with pytest.raises(ValueError, match="unknown parameter"):
        client.request("admin.groups", {"nope": 1})
    with pytest.raises(ValueError, match="missing path parameter"):
        client.request("admin.reports.users", {})
    assert adapter.requests == []


def test_credentials_are_asked_for_on_every_request():
    adapter = FakeAdapter().add("GET", "/admin/apps", ok({"value": []}))
    tokens = [make_token(tenant="t1"), make_token(tenant="t1")]
    seen = []

    def provider():
        creds = Credentials(tokens[len(seen) % 2] + "", profile="p")
        seen.append(creds.token)
        return creds

    clock = Time()
    client = PowerBIClient(
        provider,
        session=adapter.session(),
        base_url=BASE,
        clock=clock.now,
        sleep=clock.sleep,
        limiter=Limiter(QuotaTracker(clock=clock.time), sleep=clock.sleep),
    )
    client.request("admin.apps")
    client.request("admin.apps")
    assert len(seen) == 2  # a token replaced after signing in again is picked up


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


def test_an_expired_token_is_refused_before_any_request():
    adapter = FakeAdapter()
    client, _ = make_client(adapter, token=make_token(expires_in=timedelta(minutes=-1)))
    with pytest.raises(TokenExpiredError) as excinfo:
        client.request("admin.groups", {"$top": 1})
    assert "pbi auth -t <token> -p admin-nlm -g admin" in str(excinfo.value)
    assert adapter.requests == []


def test_401_means_the_token_was_rejected():
    body = {"error": {"code": "TokenExpired", "message": "Access token has expired"}}
    adapter = FakeAdapter().add("GET", "/admin/apps", make_response(401, body))
    token = make_token()
    client, _ = make_client(adapter, token=token)
    with pytest.raises(TokenExpiredError) as excinfo:
        client.request("admin.apps")
    assert "401" in str(excinfo.value)
    assert "pbi auth -t <token> -p admin-nlm -g admin" in str(excinfo.value)
    assert token not in str(excinfo.value)


def test_403_on_an_admin_endpoint_points_at_the_admin_profile():
    adapter = FakeAdapter().add(
        "GET",
        "/admin/apps",
        make_response(403, {"error": {"code": "PowerBINotAuthorizedException"}}),
    )
    client, _ = make_client(adapter)
    with pytest.raises(ApiError) as excinfo:
        client.request("admin.apps")
    assert excinfo.value.status == 403
    assert excinfo.value.code == "PowerBINotAuthorizedException"
    assert "Fabric administrator" in str(excinfo.value)
    assert "-g admin" in str(excinfo.value)


def test_403_on_a_user_endpoint_does_not_mention_admin():
    adapter = FakeAdapter().add(
        "GET", "/apps", make_response(403, {"error": {"message": "nope"}})
    )
    client, _ = make_client(adapter, group="user")
    with pytest.raises(ApiError) as excinfo:
        client.request("user.apps")
    assert "administrator" not in str(excinfo.value)
    assert "nope" in str(excinfo.value)


@pytest.mark.parametrize(
    "status,needle",
    [
        (400, "invalid request (400)"),
        (404, "not found (404)"),
        (500, "HTTP 500"),
        (503, "HTTP 503"),
    ],
)
def test_other_error_statuses(status, needle):
    body = {"error": {"code": "SomeCode", "message": "Something specific"}}
    adapter = FakeAdapter().add("GET", "/admin/apps", make_response(status, body))
    client, _ = make_client(adapter)
    with pytest.raises(ApiError) as excinfo:
        client.request("admin.apps")
    message = str(excinfo.value)
    assert needle in message
    assert "Something specific" in message and "SomeCode" in message
    assert excinfo.value.status == status
    assert excinfo.value.code == "SomeCode"


def test_error_details_come_from_whatever_the_body_offers():
    def error_for(response):
        adapter = FakeAdapter().add("GET", "/admin/apps", response)
        client, _ = make_client(adapter)
        with pytest.raises(ApiError) as excinfo:
            client.request("admin.apps")
        return str(excinfo.value)

    assert "bare string" in error_for(make_response(400, {"error": "bare string"}))
    assert "top level message" in error_for(
        make_response(400, {"message": "top level message"})
    )
    assert "plain text body" in error_for(make_response(502, text="plain text body"))
    pbi_style = {"error": {"pbi.error": {"code": "PowerBIEntityNotFound"}}}
    assert "PowerBIEntityNotFound" in error_for(make_response(404, pbi_style))


def test_a_connection_problem_becomes_an_api_error():
    adapter = FakeAdapter().add(
        "GET", "/admin/apps", requests.ConnectionError("connection reset")
    )
    client, _ = make_client(adapter)
    with pytest.raises(ApiError) as excinfo:
        client.request("admin.apps")
    assert "admin.apps" in str(excinfo.value) and "connection reset" in str(
        excinfo.value
    )


def test_a_success_answer_that_is_not_json_is_an_error():
    adapter = FakeAdapter().add(
        "GET", "/admin/apps", make_response(200, text="<html>proxy login</html>")
    )
    client, _ = make_client(adapter)
    with pytest.raises(ApiError, match="not JSON"):
        client.request("admin.apps")


# ---------------------------------------------------------------------------
# throttling
# ---------------------------------------------------------------------------


def test_429_is_retried_after_the_time_the_api_asks_for():
    adapter = FakeAdapter().add(
        "GET",
        "/admin/apps",
        make_response(429, headers={"Retry-After": "7"}),
        ok({"value": [{"id": "a"}]}),
    )
    events = []
    client, clock = make_client(adapter, on_throttle=events.append)
    response = client.request("admin.apps")
    assert response.data == {"value": [{"id": "a"}]}
    assert clock.slept == [7.0]
    assert events == [Throttled("admin.apps", 7.0, 1)]
    assert len(adapter.requests) == 2


def test_retry_after_may_be_a_date():
    when = (NOW + timedelta(seconds=42)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    adapter = FakeAdapter().add(
        "GET",
        "/admin/apps",
        make_response(429, headers={"Retry-After": when}),
        ok({"value": []}),
    )
    client, clock = make_client(adapter)
    client.request("admin.apps")
    assert clock.slept == [pytest.approx(42)]


def test_429_without_retry_after_backs_off():
    adapter = FakeAdapter().add(
        "GET",
        "/admin/apps",
        make_response(429),
        make_response(429),
        make_response(429),
        ok({"value": []}),
    )
    client, clock = make_client(adapter)
    client.request("admin.apps")
    assert clock.slept == [2.0, 4.0, 8.0]


def test_a_long_retry_after_is_not_waited_out_and_blocks_the_endpoint():
    adapter = FakeAdapter().add(
        "GET", "/admin/groups", make_response(429, headers={"Retry-After": "3000"})
    )
    client, clock = make_client(adapter)
    with pytest.raises(RateLimitError) as excinfo:
        client.request("admin.groups", {"$top": 1})
    assert excinfo.value.retry_after == 3000
    assert "50 minutes" in str(excinfo.value)
    assert clock.slept == []
    assert len(adapter.requests) == 1

    # the next attempt does not hit the same wall: it fails fast, locally
    with pytest.raises(RateLimitError):
        client.request("admin.groups", {"$top": 1})
    assert len(adapter.requests) == 1


def test_persistent_throttling_gives_up_after_the_retries():
    adapter = FakeAdapter().add(
        "GET", "/admin/apps", make_response(429, headers={"Retry-After": "1"})
    )
    client, clock = make_client(adapter, max_throttle_retries=2)
    with pytest.raises(RateLimitError):
        client.request("admin.apps")
    assert len(adapter.requests) == 3  # the first try and two retries
    assert clock.slept == [1.0, 1.0]


def test_requests_wait_for_the_documented_quota():
    adapter = FakeAdapter().add("GET", "/admin/groups", ok({"value": []}))
    client, clock = make_client(adapter)
    for _ in range(15):  # admin.groups allows 15 per minute
        client.request("admin.groups", {"$top": 1})
    assert clock.slept == []
    client.request("admin.groups", {"$top": 1})
    assert clock.slept == [pytest.approx(60)]


def test_a_request_fails_fast_when_the_quota_wait_is_too_long():
    adapter = FakeAdapter().add("GET", "/admin/groups", ok({"value": []}))
    client, clock = make_client(adapter, quota_wait=30)
    for _ in range(15):
        client.request("admin.groups", {"$top": 1})
    with pytest.raises(RateLimitError):
        client.request("admin.groups", {"$top": 1})
    assert len(adapter.requests) == 15


# ---------------------------------------------------------------------------
# fetch: paging
# ---------------------------------------------------------------------------


def rows(start, count):
    return [{"id": f"w{i}"} for i in range(start, start + count)]


def test_fetch_reads_all_pages_of_a_skip_list():
    adapter = FakeAdapter().add(
        "GET",
        "/admin/groups",
        ok({"@odata.context": "ctx", "value": rows(0, 5000)}),
        ok({"@odata.context": "ctx", "value": rows(5000, 5000)}),
        ok({"@odata.context": "ctx", "value": rows(10000, 120)}),
    )
    client, _ = make_client(adapter)
    result = client.fetch("admin.groups", {"$expand": ["users", "reports"]})

    assert [r["id"] for r in result.data["value"]][:2] == ["w0", "w1"]
    assert len(result.data["value"]) == 10120
    assert result.data["@odata.context"] == "ctx"
    assert result.manifest["pages"] == 3 and result.manifest["rows"] == 10120
    urls = adapter.urls()
    assert urls[0] == f"{BASE}/admin/groups?%24expand=reports%2Cusers&%24top=5000"
    assert (
        urls[1]
        == f"{BASE}/admin/groups?%24expand=reports%2Cusers&%24skip=5000&%24top=5000"
    )
    assert (
        urls[2]
        == f"{BASE}/admin/groups?%24expand=reports%2Cusers&%24skip=10000&%24top=5000"
    )


def test_top_caps_the_total_and_is_one_request_when_it_fits_a_page():
    adapter = FakeAdapter().add("GET", "/admin/groups", ok({"value": rows(0, 1000)}))
    client, _ = make_client(adapter)
    result = client.fetch("admin.groups", {"$top": 1000})
    assert len(result.data["value"]) == 1000
    assert adapter.urls() == [f"{BASE}/admin/groups?%24top=1000"]


def test_top_above_the_page_size_asks_for_the_rest_in_the_second_page():
    adapter = FakeAdapter().add(
        "GET",
        "/admin/groups",
        ok({"value": rows(0, 5000)}),
        ok({"value": rows(5000, 2000)}),
    )
    client, _ = make_client(adapter)
    result = client.fetch("admin.groups", {"$top": 7000})
    assert len(result.data["value"]) == 7000
    assert adapter.urls()[1].endswith("%24skip=5000&%24top=2000")


def test_skip_is_respected_as_the_start():
    adapter = FakeAdapter().add("GET", "/admin/groups", ok({"value": rows(0, 10)}))
    client, _ = make_client(adapter)
    client.fetch("admin.groups", {"$skip": 40, "$top": 10})
    assert adapter.urls() == [f"{BASE}/admin/groups?%24skip=40&%24top=10"]


def test_a_short_first_page_is_the_end():
    adapter = FakeAdapter().add("GET", "/admin/groups", ok({"value": rows(0, 3)}))
    client, _ = make_client(adapter)
    assert len(client.fetch("admin.groups").data["value"]) == 3
    assert len(adapter.requests) == 1


def test_top_must_be_positive():
    client, _ = make_client(FakeAdapter())
    with pytest.raises(ValueError, match="at least 1"):
        client.fetch("admin.groups", {"$top": 0})


def artifact_page(entities, following=None):
    body = {"artifactAccessEntities": entities}
    if following:
        body["continuationUri"] = (
            f"{BASE}/admin/users/u1/artifactAccess?continuationToken='{following}'"
        )
        body["continuationToken"] = following
    return ok(body)


def test_fetch_follows_continuation_links_and_merges_the_pages():
    adapter = (
        FakeAdapter()
        .add("GET", "continuationToken='t2'", artifact_page([{"artifactId": "c"}]))
        .add(
            "GET",
            "continuationToken='t1'",
            artifact_page([{"artifactId": "b"}], following="t2"),
        )
        .add(
            "GET",
            "artifactAccess",
            artifact_page([{"artifactId": "a"}], following="t1"),
        )
    )
    client, _ = make_client(adapter)
    result = client.fetch("admin.users.artifact_access", {"userId": "u1"})
    assert [e["artifactId"] for e in result.data["artifactAccessEntities"]] == [
        "a",
        "b",
        "c",
    ]
    assert (
        "continuationUri" not in result.data and "continuationToken" not in result.data
    )
    assert result.manifest["pages"] == 3 and result.manifest["rows"] == 3


def test_both_spellings_of_the_items_key_are_read():
    adapter = FakeAdapter().add(
        "GET", "artifactAccess", ok({"ArtifactAccessEntities": [{"ArtifactId": "a"}]})
    )
    client, _ = make_client(adapter)
    result = client.fetch("admin.users.artifact_access", {"userId": "u1"})
    assert result.data == {"ArtifactAccessEntities": [{"ArtifactId": "a"}]}
    assert result.manifest["rows"] == 1


def test_a_continuation_token_alone_is_enough():
    first = ok(
        {"artifactAccessEntities": [{"artifactId": "a"}], "continuationToken": "tok"}
    )
    adapter = (
        FakeAdapter()
        .add(
            "GET",
            "continuationToken=tok",
            ok({"artifactAccessEntities": [{"artifactId": "b"}]}),
        )
        .add("GET", "artifactAccess", first)
    )
    client, _ = make_client(adapter)
    result = client.fetch("admin.users.artifact_access", {"userId": "u1"})
    assert len(result.data["artifactAccessEntities"]) == 2


def test_a_continuation_link_to_another_host_is_never_followed():
    evil = ok(
        {
            "artifactAccessEntities": [],
            "continuationUri": "https://evil.example/steal?x=1",
        }
    )
    adapter = FakeAdapter().add("GET", "artifactAccess", evil)
    client, _ = make_client(adapter)
    with pytest.raises(ApiError, match="evil.example"):
        client.fetch("admin.users.artifact_access", {"userId": "u1"})
    assert len(adapter.requests) == 1


def test_a_repeated_continuation_link_is_an_error_not_an_endless_loop():
    same = f"{BASE}/admin/users/u1/artifactAccess?continuationToken='loop'"
    adapter = (
        FakeAdapter()
        .add(
            "GET",
            "continuationToken='loop'",
            ok({"artifactAccessEntities": [], "continuationUri": same}),
        )
        .add(
            "GET",
            "artifactAccess",
            ok({"artifactAccessEntities": [], "continuationUri": same}),
        )
    )
    client, _ = make_client(adapter)
    with pytest.raises(ApiError, match="repeated"):
        client.fetch("admin.users.artifact_access", {"userId": "u1"})


def test_too_many_pages_are_an_error(monkeypatch):
    monkeypatch.setattr(client_module, "MAX_PAGES", 3)
    pages = [
        ok(
            {
                "artifactAccessEntities": [],
                "continuationUri": f"{BASE}/admin/users/u1/artifactAccess?continuationToken='p{i}'",
            }
        )
        for i in range(10)
    ]
    adapter = FakeAdapter().add("GET", "artifactAccess", *pages)
    client, _ = make_client(adapter)
    with pytest.raises(ApiError, match="more than 3 pages"):
        client.fetch("admin.users.artifact_access", {"userId": "u1"})


def test_iter_pages_yields_each_page_and_can_resume_from_a_link():
    adapter = (
        FakeAdapter()
        .add("GET", "continuationToken='t1'", artifact_page([{"artifactId": "b"}]))
        .add(
            "GET",
            "artifactAccess",
            artifact_page([{"artifactId": "a"}], following="t1"),
        )
    )
    client, _ = make_client(adapter)
    pages = list(client.iter_pages("admin.users.artifact_access", {"userId": "u1"}))
    assert [len(p.data["artifactAccessEntities"]) for p in pages] == [1, 1]

    resumed = list(
        client.iter_pages(
            "admin.users.artifact_access",
            {"userId": "u1"},
            url=f"{BASE}/admin/users/u1/artifactAccess?continuationToken='t1'",
        )
    )
    assert len(resumed) == 1


def test_a_response_that_is_the_list_itself():
    adapter = FakeAdapter().add(
        "GET", "/admin/workspaces/modified", ok([{"id": "w1"}, {"id": "w2"}])
    )
    client, _ = make_client(adapter)
    result = client.fetch(
        "admin.workspaces.modified", {"modifiedSince": "2026-09-29T00:00:00Z"}
    )
    assert result.data == [{"id": "w1"}, {"id": "w2"}]
    assert result.manifest["rows"] == 2


# ---------------------------------------------------------------------------
# fetch: the lake
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return LakeStore(tmp_path / "lake")


def apps_adapter(*bodies):
    return FakeAdapter().add("GET", "/apps", *[ok(b) for b in bodies])


def test_a_fetched_answer_is_written_to_the_lake(store):
    adapter = apps_adapter({"value": [{"id": "a1"}]})
    client, _ = make_client(adapter, store=store, group="user")
    result = client.fetch("user.apps")

    assert result.from_cache is False
    assert result.snapshot is not None
    assert result.fetched_at == T0
    stored = store.latest("tenant-1", "user.apps", {})
    assert stored is not None and stored.load() == {"value": [{"id": "a1"}]}
    manifest = stored.manifest
    assert manifest["endpoint"] == "user.apps" and manifest["tenant"] == "tenant-1"
    assert manifest["profile"] == "admin-nlm"
    assert manifest["request"] == {
        "method": "GET",
        "path": "/apps",
        "params": {},
        "body_sha256": None,
    }
    assert manifest["rows"] == 1 and manifest["pages"] == 1


def test_a_fresh_snapshot_answers_without_calling_the_api(store):
    adapter = apps_adapter({"value": [{"id": "a1"}]}, {"value": [{"id": "a2"}]})
    client, clock = make_client(adapter, store=store, group="user")
    first = client.fetch("user.apps")
    clock.advance(minutes=30)  # user.apps stays fresh for an hour
    second = client.fetch("user.apps")

    assert len(adapter.requests) == 1
    assert second.from_cache is True
    assert second.data == first.data
    assert second.fetched_at == T0
    assert second.manifest["endpoint"] == "user.apps"


def test_a_stale_snapshot_is_fetched_again(store):
    adapter = apps_adapter({"value": [{"id": "a1"}]}, {"value": [{"id": "a2"}]})
    client, clock = make_client(adapter, store=store, group="user")
    client.fetch("user.apps")
    clock.advance(hours=2)
    second = client.fetch("user.apps")
    assert second.from_cache is False
    assert second.data == {"value": [{"id": "a2"}]}
    assert len(store.versions("tenant-1", "user.apps", {})) == 2


def test_max_age_overrides_the_endpoint_default(store):
    adapter = apps_adapter({"value": [1]}, {"value": [2]}, {"value": [3]})
    client, clock = make_client(adapter, store=store, group="user")
    client.fetch("user.apps")
    clock.advance(minutes=10)
    assert client.fetch("user.apps", max_age=timedelta(minutes=5)).from_cache is False
    assert client.fetch("user.apps", max_age=timedelta(minutes=5)).from_cache is True
    assert client.fetch("user.apps", max_age=timedelta(0)).from_cache is False
    clock.advance(days=400)
    assert client.fetch("user.apps", max_age=FOREVER).from_cache is True


def test_refresh_ignores_the_lake_but_still_stores(store):
    adapter = apps_adapter({"value": [1]}, {"value": [2]})
    client, _ = make_client(adapter, store=store, group="user")
    client.fetch("user.apps")
    result = client.fetch("user.apps", refresh=True)
    assert result.from_cache is False and result.data == {"value": [2]}
    assert store.latest("tenant-1", "user.apps", {}).load() == {"value": [2]}


def test_offline_answers_from_the_lake_whatever_its_age(store):
    adapter = apps_adapter({"value": [1]})
    client, clock = make_client(adapter, store=store, group="user")
    client.fetch("user.apps")
    clock.advance(days=90)
    result = client.fetch("user.apps", offline=True)
    assert result.from_cache is True and result.data == {"value": [1]}
    assert len(adapter.requests) == 1
    # offline wins over refresh
    assert client.fetch("user.apps", offline=True, refresh=True).from_cache is True


def test_offline_without_a_snapshot_says_so(store):
    adapter = FakeAdapter()
    client, _ = make_client(adapter, store=store, group="user")
    with pytest.raises(OfflineCacheMiss, match="user.apps"):
        client.fetch("user.apps", offline=True)
    assert adapter.requests == []


def test_offline_without_a_lake_says_so():
    client, _ = make_client(FakeAdapter(), store=None, group="user")
    with pytest.raises(OfflineCacheMiss, match="no lake is configured"):
        client.fetch("user.apps", offline=True)


def test_without_a_lake_every_fetch_calls_the_api():
    adapter = apps_adapter({"value": [1]}, {"value": [2]})
    client, _ = make_client(adapter, store=None, group="user")
    first = client.fetch("user.apps")
    second = client.fetch("user.apps")
    assert (first.data, second.data) == ({"value": [1]}, {"value": [2]})
    assert first.snapshot is None and first.manifest["rows"] == 1


def test_different_parameters_never_share_an_answer(store):
    groups = FakeAdapter().add(
        "GET",
        "/admin/groups",
        ok({"value": [{"id": "with-users"}]}),
        ok({"value": [{"id": "with-reports"}]}),
    )
    client, _ = make_client(groups, store=store)
    users = client.fetch("admin.groups", {"$expand": "users"})
    reports = client.fetch("admin.groups", {"$expand": "reports"})
    again = client.fetch("admin.groups", {"$expand": ["users"]})
    assert users.data != reports.data
    assert again.from_cache is True and again.data == users.data
    assert len(groups.requests) == 2


def test_parameter_spelling_does_not_matter(store):
    adapter = FakeAdapter().add("GET", "/admin/groups", ok({"value": []}))
    client, _ = make_client(adapter, store=store)
    client.fetch("admin.groups", {"$expand": "users, reports", "$top": 100})
    assert (
        client.fetch(
            "admin.groups", {"$top": "100", "$expand": ["reports", "users"]}
        ).from_cache
        is True
    )
    assert len(adapter.requests) == 1


def test_tenants_never_share_an_answer(store):
    a = FakeAdapter().add("GET", "/apps", ok({"value": [{"id": "from-a"}]}))
    b = FakeAdapter().add("GET", "/apps", ok({"value": [{"id": "from-b"}]}))
    client_a, _ = make_client(
        a, store=store, group="user", token=make_token(tenant="tenant-a")
    )
    client_b, _ = make_client(
        b, store=store, group="user", token=make_token(tenant="tenant-b")
    )
    assert client_a.fetch("user.apps").data == {"value": [{"id": "from-a"}]}
    assert client_b.fetch("user.apps").data == {"value": [{"id": "from-b"}]}
    assert sorted(store.tenants()) == ["tenant-a", "tenant-b"]


def test_a_token_without_a_tenant_is_keyed_by_the_profile(store):
    adapter = FakeAdapter().add("GET", "/apps", ok({"value": []}))
    client, _ = make_client(
        adapter, store=store, group="user", token="opaque-token", profile="my profile"
    )
    client.fetch("user.apps")
    assert store.tenants() == ["profile-my_profile"]


def test_a_tenant_can_be_fixed_and_then_no_credentials_are_needed_offline(store):
    adapter = apps_adapter({"value": [1]})
    client, _ = make_client(adapter, store=store, group="user", tenant="fixed")
    client.fetch("user.apps")
    assert store.tenants() == ["fixed"]

    def no_credentials():
        raise AssertionError("credentials must not be needed to read the lake")

    offline = PowerBIClient(
        no_credentials, store=store, tenant="fixed", session=FakeAdapter().session()
    )
    assert offline.fetch("user.apps", offline=True).data == {"value": [1]}


def test_an_expired_token_does_not_stop_a_fresh_cached_answer(store):
    adapter = apps_adapter({"value": [1]})
    client, clock = make_client(
        adapter,
        store=store,
        group="user",
        token=make_token(expires_in=timedelta(minutes=20)),
    )
    client.fetch("user.apps")
    clock.advance(minutes=30)  # the token has expired, the snapshot is still fresh
    assert client.fetch("user.apps").from_cache is True
    with pytest.raises(TokenExpiredError):
        client.fetch("user.apps", refresh=True)


class BrokenStore(LakeStore):
    def write_snapshot(self, *args, **kwargs):
        raise OSError("disk full")


def test_a_lake_that_cannot_be_written_does_not_lose_the_answer(tmp_path):
    adapter = apps_adapter({"value": [1]})
    client, _ = make_client(adapter, store=BrokenStore(tmp_path / "lake"), group="user")
    result = client.fetch("user.apps")
    assert result.data == {"value": [1]}
    assert result.snapshot is None and result.from_cache is False


class UnreadableStore(LakeStore):
    def latest(self, *args, **kwargs):
        raise OSError("S3 is not reachable")


def test_a_lake_that_cannot_be_read_does_not_stop_a_call_the_api_can_answer(tmp_path):
    adapter = apps_adapter({"value": [1]})
    client, _ = make_client(
        adapter, store=UnreadableStore(tmp_path / "lake"), group="user"
    )
    messages = []
    sink = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        result = client.fetch("user.apps", max_age=FOREVER)
    finally:
        logger.remove(sink)

    assert result.from_cache is False and result.data == {"value": [1]}
    assert len(adapter.requests) == 1
    assert any("Could not read user.apps from the lake" in m for m in messages)
    assert any("S3 is not reachable" in m for m in messages)


def test_a_damaged_stored_answer_is_fetched_again_and_replaced(store):
    adapter = apps_adapter({"value": [1]}, {"value": [2]})
    client, _ = make_client(adapter, store=store, group="user")
    first = client.fetch("user.apps")
    (first.snapshot.directory / "data.json").write_text("{not json", encoding="utf-8")

    second = client.fetch("user.apps")  # fresh by age, but it cannot be read

    assert second.from_cache is False and second.data == {"value": [2]}
    assert len(adapter.requests) == 2
    assert len(store.versions("tenant-1", "user.apps", {})) == 2
    assert client.fetch("user.apps").data == {"value": [2]}  # the new one is served


def test_offline_with_a_lake_that_cannot_be_read_says_why(tmp_path):
    adapter = FakeAdapter()
    client, _ = make_client(
        adapter, store=UnreadableStore(tmp_path / "lake"), group="user"
    )
    with pytest.raises(
        PBIError, match="could not be read: S3 is not reachable"
    ) as info:
        client.fetch("user.apps", offline=True)
    assert not isinstance(info.value, OfflineCacheMiss)
    assert adapter.requests == []


def test_offline_with_a_damaged_stored_answer_says_why(store):
    adapter = apps_adapter({"value": [1]})
    client, _ = make_client(adapter, store=store, group="user")
    first = client.fetch("user.apps")
    (first.snapshot.directory / "data.json").write_text("", encoding="utf-8")

    with pytest.raises(PBIError, match="The data lake could not be read"):
        client.fetch("user.apps", offline=True)
    assert len(adapter.requests) == 1


def test_the_token_never_reaches_the_lake(store, tmp_path):
    token = make_token(tenant="tenant-secret-check")
    adapter = FakeAdapter().add("GET", "/admin/groups", ok({"value": rows(0, 3)}))
    client, _ = make_client(adapter, store=store, token=token)
    client.fetch("admin.groups", {"$top": 10})
    contents = b"".join(
        p.read_bytes() for p in (tmp_path / "lake").rglob("*") if p.is_file()
    )
    assert token.encode() not in contents
    assert b"Authorization" not in contents and b"Bearer" not in contents


def test_only_read_only_snapshot_endpoints_can_be_fetched(store):
    client, _ = make_client(FakeAdapter(), store=store)
    for endpoint_id in (
        "admin.scan.start",
        "admin.scan.status",
        "admin.activityevents",
    ):
        with pytest.raises(ValueError, match="cannot be fetched"):
            client.fetch(
                endpoint_id, {"scanId": "s"} if "status" in endpoint_id else None
            )


def test_the_client_can_be_used_as_a_context_manager():
    with make_client(FakeAdapter())[0] as client:
        assert isinstance(client, PowerBIClient)


def test_tenant_key_reports_what_the_lake_uses():
    client, _ = make_client(FakeAdapter(), token=make_token(tenant="abc"))
    assert client.tenant_key() == "abc"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_rows_of():
    groups = get_endpoint("admin.groups")
    assert rows_of(groups, {"value": [1, 2]}) == [1, 2]
    assert rows_of(groups, {"other": 1}) == []
    assert rows_of(groups, None) == []
    modified = get_endpoint("admin.workspaces.modified")
    assert rows_of(modified, [{"id": 1}]) == [{"id": 1}]
    assert rows_of(modified, {"id": 1}) == []


def test_body_hash_is_stable():
    assert body_hash(None) is None
    assert body_hash({"a": 1, "b": 2}) == body_hash({"b": 2, "a": 1})
    assert body_hash({"a": 1}) != body_hash({"a": 2})
    assert re.fullmatch(r"[0-9a-f]{64}", body_hash({"workspaces": ["w1"]}))
