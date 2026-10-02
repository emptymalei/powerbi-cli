"""Tests for the endpoint registry."""

import pytest

from pbi_cli.core.registry import (
    BASE_URL,
    ENDPOINTS,
    HOUR,
    MINUTE,
    Kind,
    Paging,
    RateLimit,
    Scope,
    get_endpoint,
)
from pbi_cli.errors import PBIError

# The quotas as stated on the documentation page of each operation, verified on
# 2026-09-30. ``None``: the page states no limit. Change a value here only together with
# the registry and with evidence from the documentation.
DOCUMENTED_LIMITS = {
    "admin.groups": "50/h, 15/min",
    "admin.apps": "200/h",
    "admin.reports": "50/h, 5/min",
    "admin.datasets": "50/h, 5/min",
    "admin.dashboards": "50/h, 5/min",
    "admin.dataflows": "200/h",
    "admin.capacities": "200/h",
    "admin.users.artifact_access": "200/h",
    "admin.reports.users": "200/h",
    "admin.datasets.datasources": "300/h",
    "admin.workspaces.modified": "30/h",
    "admin.activityevents": "200/h",
    "admin.scan.start": "500/h, 16 concurrent",
    "admin.scan.status": "10000/h",
    "admin.scan.result": "500/h",
    "user.groups": None,
    "user.apps": None,
    "user.group_reports": None,
    "user.group_datasets": None,
    "user.group_dashboards": None,
    "user.group_dataflows": None,
    "user.group_users": None,
    "user.report_pages": None,
}


def describe(endpoint_id):
    limit = get_endpoint(endpoint_id).limit
    return limit.describe() if limit else None


def test_documented_limits_are_what_the_registry_says():
    assert {e.id: describe(e.id) for e in ENDPOINTS} == DOCUMENTED_LIMITS


def test_ids_are_unique_and_lookup_works():
    ids = [e.id for e in ENDPOINTS]
    assert len(ids) == len(set(ids))
    for endpoint in ENDPOINTS:
        assert get_endpoint(endpoint.id) is endpoint


def test_unknown_endpoint_lists_the_known_ones():
    with pytest.raises(PBIError) as excinfo:
        get_endpoint("admin.nope")
    assert "admin.nope" in str(excinfo.value)
    assert "admin.groups" in str(excinfo.value)


def test_only_reads_and_the_scan_start_are_listed():
    posts = [e.id for e in ENDPOINTS if e.method == "POST"]
    assert posts == ["admin.scan.start"]
    assert {e.method for e in ENDPOINTS} == {"GET", "POST"}


def test_scope_matches_the_path():
    for endpoint in ENDPOINTS:
        is_admin_path = endpoint.path.startswith("/admin/")
        assert is_admin_path == (endpoint.scope is Scope.ADMIN), endpoint.id


def test_kinds():
    assert get_endpoint("admin.activityevents").kind is Kind.EVENTS
    jobs = {e.id for e in ENDPOINTS if e.kind is Kind.JOB}
    assert jobs == {"admin.scan.start", "admin.scan.status", "admin.scan.result"}
    assert all(
        e.kind is Kind.SNAPSHOT
        for e in ENDPOINTS
        if e.id not in jobs | {"admin.activityevents"}
    )


def test_documentation_links_point_at_the_docs():
    for endpoint in ENDPOINTS:
        assert endpoint.doc_url.startswith(
            "https://learn.microsoft.com/en-us/rest/api/power-bi/"
        ), endpoint.id


def test_fan_out_parents_exist_and_fill_one_placeholder():
    ids = {e.id for e in ENDPOINTS}
    for endpoint in ENDPOINTS:
        if endpoint.parent is None:
            continue
        assert endpoint.parent in ids
        assert len(endpoint.path_params) == 1, endpoint.id


def test_paging_declarations_are_complete():
    for endpoint in ENDPOINTS:
        if endpoint.paging is Paging.SKIP:
            assert endpoint.page_size and endpoint.page_size > 0
            assert {"$top", "$skip"} <= set(endpoint.query)
        if endpoint.paging is Paging.CONTINUATION:
            assert endpoint.items_keys


def test_limits_are_well_formed():
    for endpoint in ENDPOINTS:
        if endpoint.limit is None:
            continue
        assert endpoint.limit.windows
        for count, seconds in endpoint.limit.windows:
            assert count > 0 and seconds > 0


def test_rate_limit_description():
    assert RateLimit(windows=((15, MINUTE), (50, HOUR))).describe() == "50/h, 15/min"
    assert RateLimit(windows=((5, 30),)).describe() == "5/30s"
    assert (
        RateLimit(windows=((500, HOUR),), max_concurrent=16).describe()
        == "500/h, 16 concurrent"
    )


def test_path_params():
    assert get_endpoint("admin.groups").path_params == ()
    assert get_endpoint("user.report_pages").path_params == ("groupId", "reportId")


# ---------------------------------------------------------------------------
# parameters
# ---------------------------------------------------------------------------


def test_canonical_params_ignore_spelling_and_order():
    groups = get_endpoint("admin.groups")
    a = groups.canonical_params({"$expand": ["users", "reports"], "$top": 100})
    b = groups.canonical_params({"$top": "100", "$expand": "reports, users"})
    c = groups.canonical_params({"$expand": ["reports", "users", "users"], "$top": 100})
    assert a == b == c == {"$expand": "reports,users", "$top": "100"}


def test_canonical_params_differ_when_the_request_differs():
    groups = get_endpoint("admin.groups")
    assert groups.canonical_params({"$expand": "users"}) != groups.canonical_params(
        {"$expand": "reports"}
    )
    assert groups.canonical_params({}) != groups.canonical_params({"$top": 1})


def test_canonical_params_drop_none_and_lowercase_booleans():
    modified = get_endpoint("admin.workspaces.modified")
    params = modified.canonical_params(
        {"excludePersonalWorkspaces": True, "modifiedSince": None}
    )
    assert params == {"excludePersonalWorkspaces": "true"}


def test_canonical_params_include_path_parameters():
    users = get_endpoint("admin.reports.users")
    assert users.canonical_params({"reportId": "r-1"}) == {"reportId": "r-1"}
    assert users.canonical_params({"reportId": "r-1"}) != users.canonical_params(
        {"reportId": "r-2"}
    )


def test_unknown_parameter_is_rejected():
    with pytest.raises(ValueError, match="unknown parameter"):
        get_endpoint("admin.groups").split_params({"$nope": 1})


def test_missing_path_parameter_is_rejected():
    with pytest.raises(ValueError, match="missing path parameter"):
        get_endpoint("admin.reports.users").split_params({})


def test_list_for_a_single_value_parameter_is_rejected():
    with pytest.raises(ValueError, match="single value"):
        get_endpoint("admin.groups").canonical_params({"$filter": ["a", "b"]})


def test_split_params_separates_path_from_query():
    endpoint = get_endpoint("admin.users.artifact_access")
    path, query = endpoint.split_params(
        {"userId": "u@x.com", "artifactTypes": "Report"}
    )
    assert path == {"userId": "u@x.com"}
    assert query == {"artifactTypes": "Report"}


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


def test_build_url_without_parameters():
    assert get_endpoint("admin.apps").build_url({}, {}) == f"{BASE_URL}/admin/apps"


def test_build_url_encodes_path_values():
    endpoint = get_endpoint("admin.users.artifact_access")
    url = endpoint.build_url({"userId": "guest_x.com#EXT#@contoso.com"}, {})
    assert (
        url
        == f"{BASE_URL}/admin/users/guest_x.com%23EXT%23%40contoso.com/artifactAccess"
    )


def test_build_url_encodes_query_values_and_sorts_keys():
    endpoint = get_endpoint("admin.groups")
    url = endpoint.build_url({}, {"$top": "5", "$filter": "state eq 'Deleted'"})
    assert (
        url == f"{BASE_URL}/admin/groups?%24filter=state%20eq%20%27Deleted%27&%24top=5"
    )


def test_build_url_with_a_custom_base():
    url = get_endpoint("user.apps").build_url({}, {}, base_url="http://localhost:1")
    assert url == "http://localhost:1/apps"


# ---------------------------------------------------------------------------
# what depends on who asks
# ---------------------------------------------------------------------------


def test_only_what_depends_on_who_asks_is_per_identity():
    assert {e.id for e in ENDPOINTS if e.per_identity} == {"user.groups", "user.apps"}


def test_the_identity_is_a_part_of_the_key_of_the_request_but_not_of_the_url():
    endpoint = get_endpoint("user.groups")

    assert endpoint.canonical_params({"$top": 5, "_as": "oid-1"}) == {
        "$top": "5",
        "_as": "oid-1",
    }
    assert endpoint.canonical_params({"$top": 5}) == {"$top": "5"}
    path_params, query = endpoint.split_canonical({"$top": 5, "_as": "oid-1"})
    assert (path_params, query) == ({}, {"$top": "5"})
    assert "_as" not in endpoint.build_url(path_params, query)


def test_two_identities_are_two_requests():
    endpoint = get_endpoint("user.apps")

    assert endpoint.canonical_params({"_as": "a"}) != endpoint.canonical_params(
        {"_as": "b"}
    )


def test_an_operation_that_does_not_depend_on_who_asks_refuses_an_identity():
    with pytest.raises(ValueError, match="unknown parameter"):
        get_endpoint("admin.groups").canonical_params({"_as": "oid-1"})
