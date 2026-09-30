"""The commands that read the API go through the client and keep the answers in the lake.

``workspaces list``, ``users user-access`` and ``apps list`` have ``--use-cache`` and
``--cache-only``; ``reports list`` and ``reports pages`` always ask the API (see
``test_cli_reports.py``). The API is scripted with the ``fake_api`` fixture.
"""

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

import pytest
from core_helpers import FakeAdapter, make_response, make_token
from typer.testing import CliRunner

from pbi_cli.cli import _set_credential, app
from pbi_cli.config import PBIConfig
from pbi_cli.core.store import LakeStore

GROUPS = re.compile(r"/admin/groups\?")
ALL_EXPAND = "dashboards,dataflows,datasets,reports,users,workbooks"
FAR_FUTURE = timedelta(days=36500)


def groups_body(*names: str) -> Dict[str, Any]:
    return {
        "@odata.context": "https://api.powerbi.com/v1.0/myorg/$metadata#groups",
        "value": [
            {
                "id": f"id-{name}",
                "name": name,
                "type": "Workspace",
                "state": "Active",
                "isReadOnly": False,
                "isOnDedicatedCapacity": False,
            }
            for name in names
        ],
    }


def query_of(url: str) -> Dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def lake(cache_folder: Path) -> LakeStore:
    return LakeStore(cache_folder / "lake")


def use_token(monkeypatch, tenant: Optional[str] = "tenant-1", **kwargs: Any) -> str:
    """Sign in (for ``load_auth``) with a synthetic token of this tenant."""
    kwargs.setdefault("expires_in", FAR_FUTURE)
    token = make_token(tenant=tenant, **kwargs)
    monkeypatch.setattr(
        "pbi_cli.cli.load_auth",
        lambda profile=None, group="user": {"Authorization": f"Bearer {token}"},
    )
    return token


def run(*args: str, input: Optional[str] = None):
    return CliRunner().invoke(app, list(args), input=input)


# ---------------------------------------------------------------------------
# workspaces list
# ---------------------------------------------------------------------------


def test_workspaces_list_asks_the_api_and_prints_a_table(fake_api, signed_in):
    fake_api.add("GET", GROUPS, make_response(200, groups_body("Sales", "HR")))

    result = run("workspaces", "list")

    assert result.exit_code == 0
    assert "Workspaces: 2 record(s)" in result.stdout
    assert "Sales" in result.stdout and "HR" in result.stdout
    assert fake_api.calls()[0].headers["Authorization"] == f"Bearer {signed_in}"


def test_workspaces_list_sends_top_expand_and_filter(fake_api, signed_in):
    fake_api.add("GET", GROUPS, make_response(200, groups_body("A")))

    run(
        "workspaces",
        "list",
        "--top",
        "50",
        "-e",
        "users",
        "-e",
        "reports",
        "-f",
        "x eq 1",
    )

    assert query_of(fake_api.urls()[0]) == {
        "$top": "50",
        "$expand": "reports,users",
        "$filter": "x eq 1",
    }


def test_workspaces_list_expands_everything_by_default(fake_api, signed_in):
    fake_api.add("GET", GROUPS, make_response(200, groups_body("A")))

    run("workspaces", "list")

    assert query_of(fake_api.urls()[0]) == {"$top": "1000", "$expand": ALL_EXPAND}
    assert fake_api.urls()[0].startswith(
        "https://api.powerbi.com/v1.0/myorg/admin/groups?"
    )


def test_workspaces_list_uses_the_admin_token(fake_api, monkeypatch):
    asked: List[str] = []
    token = make_token(expires_in=FAR_FUTURE)

    def load_auth(profile=None, group="user"):
        asked.append(group)
        return {"Authorization": f"Bearer {token}"}

    monkeypatch.setattr("pbi_cli.cli.load_auth", load_auth)
    fake_api.add("GET", GROUPS, make_response(200, groups_body("A")))

    run("workspaces", "list")

    assert set(asked) == {"admin"}


def test_workspaces_list_stores_what_it_fetched(cache_folder, fake_api, signed_in):
    body = groups_body("Sales")
    fake_api.add("GET", GROUPS, make_response(200, body))

    result = run("workspaces", "list", "--top", "50")

    assert "Saved to the data lake (version: " in result.stdout
    store = lake(cache_folder)
    assert store.tenants() == ["tenant-1"]
    (stored,) = store.parameter_sets("tenant-1", "admin.groups")
    assert stored.params == {"$expand": ALL_EXPAND, "$top": "50"}
    assert stored.latest.load() == body
    assert stored.latest.manifest["rows"] == 1


def test_workspaces_list_always_asks_the_api_by_default(
    cache_folder, fake_api, signed_in
):
    fake_api.add(
        "GET",
        GROUPS,
        make_response(200, groups_body("Old")),
        make_response(200, groups_body("New")),
    )

    run("workspaces", "list")
    second = run("workspaces", "list")

    assert len(fake_api.calls()) == 2
    assert "New" in second.stdout
    store = lake(cache_folder)
    (stored,) = store.parameter_sets("tenant-1", "admin.groups")
    assert len(store.versions("tenant-1", "admin.groups", stored.params)) == 2


def test_workspaces_list_use_cache_does_not_call_the_api(
    cache_folder, fake_api, signed_in
):
    fake_api.add("GET", GROUPS, make_response(200, groups_body("Sales")))
    run("workspaces", "list")

    result = run("workspaces", "list", "--use-cache")

    assert result.exit_code == 0
    assert len(fake_api.calls()) == 1
    assert "Using cached data from " in result.stdout
    assert "Sales" in result.stdout


def test_workspaces_list_use_cache_accepts_any_age(cache_folder, fake_api, signed_in):
    """``--use-cache`` is "whatever is stored", as it always was."""
    store = lake(cache_folder)
    from datetime import datetime, timezone

    store.write_snapshot(
        "tenant-1",
        "admin.groups",
        {"$expand": ALL_EXPAND, "$top": "1000"},
        groups_body("Ancient"),
        fetched_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )

    result = run("workspaces", "list", "--use-cache")

    assert "Ancient" in result.stdout
    assert "Using cached data from 2020-01-01 00:00 UTC" in result.stdout
    assert fake_api.calls() == []


def test_workspaces_list_use_cache_fetches_when_nothing_is_stored(
    cache_folder, fake_api, signed_in
):
    fake_api.add("GET", GROUPS, make_response(200, groups_body("Fresh")))

    result = run("workspaces", "list", "--use-cache")

    assert result.exit_code == 0
    assert "Nothing stored for this request yet" in result.stdout
    assert "Fresh" in result.stdout
    assert len(fake_api.calls()) == 1
    assert lake(cache_folder).parameter_sets("tenant-1", "admin.groups")


def test_workspaces_list_cache_only_answers_from_the_lake(
    cache_folder, fake_api, signed_in
):
    fake_api.add("GET", GROUPS, make_response(200, groups_body("Sales")))
    run("workspaces", "list")

    result = run("workspaces", "list", "--cache-only")

    assert result.exit_code == 0
    assert "Sales" in result.stdout
    assert len(fake_api.calls()) == 1


def test_workspaces_list_cache_only_fails_when_nothing_is_stored(
    cache_folder, fake_api, signed_in
):
    result = run("workspaces", "list", "--cache-only")

    assert result.exit_code == 1
    assert (
        "Error: Nothing is stored for admin.groups with these parameters"
        in result.output
    )
    assert fake_api.calls() == []


def test_workspaces_list_cache_only_needs_a_cache_folder(fake_api, signed_in):
    result = run("workspaces", "list", "--cache-only")

    assert result.exit_code == 1
    assert "--cache-only needs the data lake" in result.output
    assert "pbi config set-cache-folder" in result.output
    assert fake_api.calls() == []


def test_workspaces_list_cache_only_explains_that_caching_is_disabled(
    cache_folder, fake_api, signed_in
):
    PBIConfig().cache_enabled = False

    result = run("workspaces", "list", "--cache-only")

    assert result.exit_code == 1
    assert "pbi config enable-cache" in result.output


def test_cache_only_wins_over_use_cache(cache_folder, fake_api, signed_in):
    result = run("workspaces", "list", "--use-cache", "--cache-only")

    assert result.exit_code == 1
    assert "Nothing is stored" in result.output
    assert fake_api.calls() == []


def test_workspaces_list_without_a_cache_folder_stores_nothing(
    tmp_path, fake_api, signed_in
):
    fake_api.add("GET", GROUPS, make_response(200, groups_body("Sales")))

    result = run("workspaces", "list")

    assert result.exit_code == 0
    assert "Saved to the data lake" not in result.stdout
    assert not list(tmp_path.rglob("tenant=*"))


def test_workspaces_list_does_not_store_when_caching_is_disabled(
    cache_folder, fake_api, signed_in
):
    PBIConfig().cache_enabled = False
    fake_api.add("GET", GROUPS, make_response(200, groups_body("Sales")))

    result = run("workspaces", "list")

    assert result.exit_code == 0
    assert not cache_folder.exists() or not list(cache_folder.rglob("tenant=*"))


def damage_what_is_stored(cache_folder: Path) -> None:
    """Truncate every stored response, as a crash or an editor might."""
    files = list((cache_folder / "lake").rglob("data.json"))
    assert files
    for file in files:
        file.write_text("{not json", encoding="utf-8")


def test_workspaces_list_use_cache_asks_the_api_when_the_lake_cannot_be_read(
    cache_folder, fake_api, signed_in
):
    fake_api.add(
        "GET",
        GROUPS,
        make_response(200, groups_body("Damaged")),
        make_response(200, groups_body("Fresh")),
    )
    run("workspaces", "list")
    damage_what_is_stored(cache_folder)

    result = run("workspaces", "list", "--use-cache")

    assert result.exit_code == 0
    assert "Fresh" in result.stdout
    assert len(fake_api.calls()) == 2
    assert "Saved to the data lake" in result.stdout


def test_workspaces_list_cache_only_says_why_the_lake_cannot_be_read(
    cache_folder, fake_api, signed_in
):
    fake_api.add("GET", GROUPS, make_response(200, groups_body("Damaged")))
    run("workspaces", "list")
    damage_what_is_stored(cache_folder)

    result = run("workspaces", "list", "--cache-only")

    assert result.exit_code == 1
    assert "Error: The data lake could not be read" in result.output
    assert "Traceback" not in result.output
    assert len(fake_api.calls()) == 1


# -- the stored answer is the answer to *this* request -----------------------------


def test_a_different_request_does_not_get_a_stored_answer(
    cache_folder, fake_api, signed_in
):
    """The old cache had one key for every ``workspaces list``, whatever the options."""
    fake_api.add(
        "GET",
        GROUPS,
        make_response(200, groups_body("Top1000")),
        make_response(200, groups_body("Top5")),
    )
    run("workspaces", "list")

    result = run("workspaces", "list", "--top", "5", "--use-cache")

    assert "Top5" in result.stdout and "Top1000" not in result.stdout
    assert len(fake_api.calls()) == 2


@pytest.mark.parametrize(
    "other",
    [["-e", "users"], ["-f", "state eq 'Active'"]],
    ids=["another $expand", "another $filter"],
)
def test_other_options_are_other_requests(cache_folder, fake_api, signed_in, other):
    fake_api.add("GET", GROUPS, make_response(200, groups_body("A")))
    run("workspaces", "list")

    run("workspaces", "list", "--use-cache", *other)

    assert len(fake_api.calls()) == 2
    assert len(lake(cache_folder).parameter_sets("tenant-1", "admin.groups")) == 2


def test_the_order_of_expand_does_not_matter(cache_folder, fake_api, signed_in):
    fake_api.add("GET", GROUPS, make_response(200, groups_body("A")))
    run("workspaces", "list", "-e", "users", "-e", "reports")

    run("workspaces", "list", "--use-cache", "-e", "reports", "-e", "users")

    assert len(fake_api.calls()) == 1


def test_another_tenant_does_not_get_a_stored_answer(
    cache_folder, fake_api, monkeypatch
):
    fake_api.add(
        "GET",
        GROUPS,
        make_response(200, groups_body("Contoso")),
        make_response(200, groups_body("Fabrikam")),
    )
    use_token(monkeypatch, tenant="contoso")
    run("workspaces", "list")

    use_token(monkeypatch, tenant="fabrikam")
    result = run("workspaces", "list", "--use-cache")

    assert "Fabrikam" in result.stdout and "Contoso" not in result.stdout
    assert lake(cache_folder).tenants() == ["contoso", "fabrikam"]


def test_a_token_without_a_tenant_is_filed_under_its_profile(
    cache_folder, fake_api, monkeypatch
):
    config = PBIConfig()
    config.add_profile_to_group("admin", "admin-nlm")
    config.set_group_active_profile("admin", "admin-nlm")
    _set_credential("admin-nlm", make_token(tenant=None, expires_in=FAR_FUTURE))
    fake_api.add("GET", GROUPS, make_response(200, groups_body("A")))

    result = run("workspaces", "list")

    assert result.exit_code == 0
    assert lake(cache_folder).tenants() == ["profile-admin-nlm"]


# -- files --------------------------------------------------------------------------


def test_workspaces_list_writes_a_json_file(tmp_path, fake_api, signed_in):
    body = groups_body("Sales")
    fake_api.add("GET", GROUPS, make_response(200, body))
    target = tmp_path / "out"

    result = run("workspaces", "list", "-tf", str(target), "-n", "ws")

    assert result.exit_code == 0
    assert json.loads((target / "ws.json").read_text()) == body


def test_workspaces_list_writes_excel_also_from_the_lake(
    tmp_path, cache_folder, fake_api, signed_in
):
    """``--use-cache`` with an Excel file crashed: the code wanted the API object."""
    body = groups_body("Sales")
    body["value"][0]["users"] = [
        {"emailAddress": "a@example.com", "groupUserAccessRight": "Admin"}
    ]
    fake_api.add("GET", GROUPS, make_response(200, body))
    run("workspaces", "list")
    target = tmp_path / "out"

    result = run(
        "workspaces", "list", "--use-cache", "-ft", "excel", "-tf", str(target)
    )

    assert result.exit_code == 0
    assert (target / "workspaces.xlsx").exists()
    assert len(fake_api.calls()) == 1


@pytest.mark.parametrize("top", ["0", "-3"])
def test_workspaces_list_rejects_a_top_below_one(fake_api, signed_in, top):
    result = run("workspaces", "list", "--top", top)

    assert result.exit_code == 2
    assert "--top" in result.output and "at least 1" in result.output
    assert fake_api.calls() == []


def test_workspaces_list_reads_more_than_one_page_when_top_asks_for_it(
    tmp_path, fake_api, signed_in
):
    """``--top`` caps the total; the API returns at most 5000 workspaces per request."""
    first = groups_body(*[f"w{i}" for i in range(5000)])
    fake_api.add(
        "GET",
        GROUPS,
        make_response(200, first),
        make_response(200, groups_body("last")),
    )
    target = tmp_path / "out"

    result = run("workspaces", "list", "--top", "5010", "-tf", str(target))

    assert result.exit_code == 0
    assert [query_of(u).get("$skip") for u in fake_api.urls()] == [None, "5000"]
    assert [query_of(u)["$top"] for u in fake_api.urls()] == ["5000", "10"]
    assert len(json.loads((target / "workspaces.json").read_text())["value"]) == 5001


# -- tokens -------------------------------------------------------------------------


def sign_in_as_admin(token: str, profile: str = "admin-nlm") -> None:
    """Store a token the way ``pbi auth -g admin -p <profile>`` does."""
    config = PBIConfig()
    config.add_profile_to_group("admin", profile)
    config.set_group_active_profile("admin", profile)
    _set_credential(profile, token)


def test_an_expired_token_is_reported_with_the_command_to_fix_it(fake_api):
    sign_in_as_admin(make_token(expires_in=timedelta(days=-1)))

    result = run("workspaces", "list")

    assert result.exit_code == 1
    assert "Error: The token for profile 'admin-nlm' expired at " in result.output
    assert "pbi auth -t <token> -p admin-nlm -g admin" in result.output
    assert fake_api.calls() == []
    assert "Traceback" not in result.output


def test_a_rejected_token_is_reported_with_the_command_to_fix_it(fake_api):
    sign_in_as_admin(make_token(expires_in=FAR_FUTURE))
    fake_api.add("GET", GROUPS, make_response(401, {"error": {"code": "TokenExpired"}}))

    result = run("workspaces", "list")

    assert result.exit_code == 1
    assert "Power BI rejected the token (401 Unauthorized)" in result.output
    assert "pbi auth -t <token> -p admin-nlm -g admin" in result.output


def test_a_stored_answer_is_served_with_an_expired_token(
    cache_folder, fake_api, monkeypatch
):
    use_token(monkeypatch)
    fake_api.add("GET", GROUPS, make_response(200, groups_body("Sales")))
    run("workspaces", "list")

    use_token(monkeypatch, expires_in=timedelta(days=-1))
    result = run("workspaces", "list", "--use-cache")

    assert result.exit_code == 0 and "Sales" in result.stdout
    assert len(fake_api.calls()) == 1


def test_no_profile_is_reported_as_an_error_not_a_traceback(fake_api):
    result = run("workspaces", "list")

    assert result.exit_code == 1
    assert "No active profile set for group 'admin'" in result.output
    assert "Traceback" not in result.output


def test_a_forbidden_answer_says_an_admin_is_needed(fake_api, signed_in):
    fake_api.add("GET", GROUPS, make_response(403, {"error": {"code": "Forbidden"}}))

    result = run("workspaces", "list")

    assert result.exit_code == 1
    assert "needs a Fabric administrator" in result.output


# ---------------------------------------------------------------------------
# apps list
# ---------------------------------------------------------------------------

ADMIN_APPS = re.compile(r"/admin/apps\?")
USER_APPS = re.compile(r"/myorg/apps$")


def apps_body(count: int, prefix: str = "app") -> Dict[str, Any]:
    return {
        "value": [
            {"id": f"{prefix}-{i}", "name": f"{prefix.title()} {i}"}
            for i in range(count)
        ]
    }


def test_apps_list_defaults_to_the_apps_of_the_user(fake_api, signed_in):
    fake_api.add("GET", USER_APPS, make_response(200, apps_body(2)))

    result = run("apps", "list")

    assert result.exit_code == 0
    assert "Apps (user): 2 record(s)" in result.stdout
    assert fake_api.urls() == ["https://api.powerbi.com/v1.0/myorg/apps"]


def test_apps_list_as_admin_uses_the_admin_operation_and_token(fake_api, monkeypatch):
    asked: List[str] = []
    token = make_token(expires_in=FAR_FUTURE)

    def load_auth(profile=None, group="user"):
        asked.append(group)
        return {"Authorization": f"Bearer {token}"}

    monkeypatch.setattr("pbi_cli.cli.load_auth", load_auth)
    fake_api.add("GET", ADMIN_APPS, make_response(200, apps_body(1)))

    result = run("apps", "list", "--role", "admin")

    assert result.exit_code == 0 and "Apps (admin)" in result.stdout
    assert query_of(fake_api.urls()[0]) == {"$top": "200"}
    assert set(asked) == {"admin"}


def test_apps_list_as_admin_reads_every_page(tmp_path, fake_api, signed_in):
    """The admin operation returned only the first 200 apps; now all of them."""
    fake_api.add(
        "GET",
        ADMIN_APPS,
        make_response(200, apps_body(200)),
        make_response(200, apps_body(3, "more")),
    )
    target = tmp_path / "out"

    result = run("apps", "list", "--role", "admin", "-tf", str(target), "-n", "all")

    assert result.exit_code == 0
    assert [query_of(u).get("$skip") for u in fake_api.urls()] == [None, "200"]
    assert len(json.loads((target / "all.json").read_text())["value"]) == 203


def test_apps_of_the_user_and_of_the_admin_are_stored_separately(
    cache_folder, fake_api, signed_in
):
    fake_api.add("GET", USER_APPS, make_response(200, apps_body(1, "mine")))
    fake_api.add("GET", ADMIN_APPS, make_response(200, apps_body(1, "theirs")))
    run("apps", "list")
    run("apps", "list", "--role", "admin")

    mine = run("apps", "list", "--use-cache")
    theirs = run("apps", "list", "--role", "admin", "--use-cache")

    assert "Mine 0" in mine.stdout and "Theirs" not in mine.stdout
    assert "Theirs 0" in theirs.stdout and "Mine" not in theirs.stdout
    assert len(fake_api.calls()) == 2
    assert lake(cache_folder).endpoints("tenant-1") == ["admin.apps", "user.apps"]


def test_apps_list_writes_a_json_file(tmp_path, fake_api, signed_in):
    body = apps_body(2)
    fake_api.add("GET", USER_APPS, make_response(200, body))
    target = tmp_path / "apps-out"

    result = run("apps", "list", "-tf", str(target))

    assert result.exit_code == 0
    assert json.loads((target / "apps.json").read_text()) == body


# ---------------------------------------------------------------------------
# users user-access
# ---------------------------------------------------------------------------

ACCESS_FIRST = re.compile(r"/admin/users/alice%40example\.com/artifactAccess$")
ACCESS_NEXT = re.compile(r"continuationToken=")
ACCESS_URL = (
    "https://api.powerbi.com/v1.0/myorg/admin/users/alice%40example.com/artifactAccess"
)


def script_user_access(fake_api: FakeAdapter) -> List[Dict[str, Any]]:
    first = {"ArtifactId": "a1", "DisplayName": "Sales", "ArtifactType": "Report"}
    second = {"ArtifactId": "a2", "DisplayName": "HR", "ArtifactType": "Dataset"}
    fake_api.add(
        "GET",
        ACCESS_FIRST,
        make_response(
            200,
            {
                "ArtifactAccessEntities": [first],
                "continuationUri": f"{ACCESS_URL}?continuationToken=t2",
                "continuationToken": "t2",
            },
        ),
    )
    fake_api.add(
        "GET", ACCESS_NEXT, make_response(200, {"ArtifactAccessEntities": [second]})
    )
    return [first, second]


def test_user_access_reads_every_page(fake_api, signed_in):
    """Only the first page of the artifact access was read before."""
    script_user_access(fake_api)

    result = run("users", "user-access", "-u", "alice@example.com")

    assert result.exit_code == 0
    assert "User Access Information for: alice@example.com" in result.stdout
    assert "Sales" in result.stdout and "HR" in result.stdout
    assert len(fake_api.calls()) == 2


def test_user_access_writes_the_artifacts_to_a_file(tmp_path, fake_api, signed_in):
    """``-tf`` was silently ignored: the code that writes the file could not run."""
    artifacts = script_user_access(fake_api)
    target = tmp_path / "out"

    result = run("users", "user-access", "-u", "alice@example.com", "-tf", str(target))

    assert result.exit_code == 0
    assert json.loads((target / "alice-example-com.json").read_text()) == {
        "artifacts": artifacts
    }


def test_user_access_file_name_can_be_chosen(tmp_path, fake_api, signed_in):
    script_user_access(fake_api)
    target = tmp_path / "out"

    run(
        "users",
        "user-access",
        "-u",
        "alice@example.com",
        "-tf",
        str(target),
        "-n",
        "alice",
    )

    assert (target / "alice.json").exists()


def test_user_access_is_stored_per_user(cache_folder, fake_api, signed_in):
    script_user_access(fake_api)
    run("users", "user-access", "-u", "alice@example.com")

    (stored,) = lake(cache_folder).parameter_sets(
        "tenant-1", "admin.users.artifact_access"
    )
    assert stored.params == {"userId": "alice@example.com"}
    assert stored.latest.manifest["pages"] == 2
    assert stored.latest.manifest["rows"] == 2


def test_user_access_use_cache_and_cache_only(cache_folder, fake_api, signed_in):
    artifacts = script_user_access(fake_api)
    run("users", "user-access", "-u", "alice@example.com")

    cached = run("users", "user-access", "-u", "alice@example.com", "--use-cache")
    only = run("users", "user-access", "-u", "alice@example.com", "--cache-only")
    other_user = run("users", "user-access", "-u", "bob@example.com", "--cache-only")

    assert "Using cached data from" in cached.stdout
    assert only.exit_code == 0
    assert other_user.exit_code == 1  # nothing is stored for bob
    assert len(fake_api.calls()) == 2
    assert artifacts[0]["DisplayName"] in cached.stdout


def test_user_access_needs_an_admin(fake_api, signed_in):
    fake_api.add(
        "GET", ACCESS_FIRST, make_response(403, {"error": {"code": "Forbidden"}})
    )

    result = run("users", "user-access", "-u", "alice@example.com")

    assert result.exit_code == 1
    assert "needs a Fabric administrator" in result.output
