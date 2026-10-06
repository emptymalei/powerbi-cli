"""``pbi_cli.session`` builds the lake and the client from the user's settings."""

import os
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from core_helpers import make_response, make_token
from urllib3.exceptions import InsecureRequestWarning

from pbi_cli.config import PBIConfig
from pbi_cli.core.auth import Credentials
from pbi_cli.core.registry import get_endpoint
from pbi_cli.core.store import LakeStore, PublishInfo
from pbi_cli.errors import PBIError, ReadOnlyLake
from pbi_cli.session import (
    LAKE_ENV,
    as_path,
    is_work_lake,
    lake_hint,
    lake_path,
    lake_root,
    open_client,
    open_lake,
    quota_file,
    resolve_lake,
)

TOKEN = make_token(tenant="tenant-1", expires_in=timedelta(days=36500))
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def credentials() -> Credentials:
    return Credentials(token=TOKEN, profile="p", group="admin")


# -- where the lake is ----------------------------------------------------------------


def test_there_is_no_lake_without_a_cache_folder():
    assert lake_path() is None
    assert open_lake() is None


def test_the_lake_is_the_lake_folder_of_the_cache_folder(cache_folder):
    assert lake_path() == cache_folder / "lake"
    store = open_lake()
    assert isinstance(store, LakeStore)
    assert store.root == cache_folder / "lake"


def test_a_cache_folder_in_s3_holds_the_lake_in_s3():
    PBIConfig().cache_folder = "s3://bucket/powerbi"

    assert str(lake_path()) == "s3://bucket/powerbi/lake"
    assert str(open_lake().root) == "s3://bucket/powerbi/lake"


def test_there_is_no_lake_to_use_while_caching_is_disabled(cache_folder):
    PBIConfig().cache_enabled = False

    assert open_lake() is None
    # ... but it is still there to browse
    assert lake_path() == cache_folder / "lake"


def test_hints_say_what_to_do(cache_folder):
    assert "available" in lake_hint()
    PBIConfig().cache_enabled = False
    assert "pbi config enable-cache" in lake_hint()
    PBIConfig().cache_folder = None
    assert "pbi config set-cache-folder" in lake_hint()


# -- the client -------------------------------------------------------------------------


def test_the_client_writes_to_the_lake_of_the_settings(cache_folder, fake_api):
    fake_api.add("GET", "/admin/groups", make_response(200, {"value": [{"id": "1"}]}))

    with open_client(credentials) as client:
        client.fetch("admin.groups", {"$top": 1})

    assert LakeStore(cache_folder / "lake").tenants() == ["tenant-1"]


def test_the_client_has_no_lake_when_there_is_no_cache_folder(fake_api):
    fake_api.add("GET", "/admin/groups", make_response(200, {"value": []}))

    with open_client(credentials) as client:
        assert client.store is None
        assert client.fetch("admin.groups", {"$top": 1}).snapshot is None


def test_the_quota_is_counted_across_runs(tmp_path, fake_api):
    """A second run of a command knows what the first one used."""
    fake_api.add("GET", "/admin/groups", make_response(200, {"value": []}))
    endpoint = get_endpoint("admin.groups")

    with open_client(credentials) as first:
        first.fetch("admin.groups", {"$top": 1}, refresh=True)
        first.fetch("admin.groups", {"$top": 2}, refresh=True)
    assert quota_file().exists()

    with open_client(credentials) as second:
        usage = second.limiter.usage(endpoint, "tenant-1")
    assert [(u.allowed, u.seconds, u.used) for u in usage] == [
        (50, 3600, 2),
        (15, 60, 2),
    ]


def test_the_quota_file_is_in_the_config_folder(isolated_home):
    assert quota_file() == Path(isolated_home) / ".pbi_cli" / "quota.json"


def test_not_verifying_tls_does_not_print_a_warning():
    """The commands never verified TLS and never printed the warning about it."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        open_client(credentials, verify=False).close()
        warnings.warn("unverified request", InsecureRequestWarning)
    assert caught == []


def test_verifying_tls_keeps_the_warnings():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        open_client(credentials, verify=True).close()
        warnings.warn("unverified request", InsecureRequestWarning)
    assert len(caught) == 1


# -- choosing the lake to look at -------------------------------------------------------


def a_lake(folder) -> LakeStore:
    """A lake with one workspace list in it, made the normal way."""
    store = LakeStore(folder)
    store.write_snapshot("tenant-1", "admin.groups", {}, {"value": [{"id": "w"}]})
    return store


def test_nothing_is_open_without_a_choice_and_a_cache_folder():
    assert resolve_lake(environ={}) is None


def test_the_work_lake_is_what_is_open_by_default_and_it_is_written(cache_folder):
    opened = resolve_lake(environ={})

    assert opened.work and not opened.readonly
    assert opened.source == "the cache folder"
    assert opened.store.root == cache_folder / "lake"
    a_lake(opened.store.root)  # a write is accepted


def test_another_lake_is_opened_read_only_and_says_why(cache_folder, tmp_path):
    shared = a_lake(tmp_path / "shared").root

    opened = resolve_lake(str(shared), environ={})

    assert not opened.work and opened.readonly and opened.source == "--lake"
    assert opened.store.tenants() == ["tenant-1"]  # reading works
    with pytest.raises(ReadOnlyLake) as refused:
        opened.store.write_state("tenant-1", "x", {})
    assert "opened with --lake" in str(refused.value)
    assert "Only the work lake" in str(refused.value)
    assert not (shared / "tenant=tenant-1" / "_state").exists()


@pytest.mark.parametrize("as_cache_folder", [False, True])
def test_the_work_lake_asked_for_by_its_location_is_still_the_work_lake(
    cache_folder, as_cache_folder
):
    a_lake(cache_folder / "lake")
    asked = cache_folder if as_cache_folder else cache_folder / "lake"

    opened = resolve_lake(str(asked), environ={})

    assert opened.work and not opened.readonly
    assert opened.store.root == cache_folder / "lake"


def test_a_cache_folder_stands_for_the_lake_inside_it(tmp_path):
    a_lake(tmp_path / "theirs" / "lake")

    opened = resolve_lake(str(tmp_path / "theirs"), environ={})

    assert opened.store.root == tmp_path / "theirs" / "lake"
    assert opened.readonly


def test_the_environment_names_a_lake_and_the_option_wins(tmp_path):
    first = a_lake(tmp_path / "first").root
    second = a_lake(tmp_path / "second").root
    environment = {LAKE_ENV: str(first)}

    from_environment = resolve_lake(None, environ=environment)
    from_option = resolve_lake(str(second), environ=environment)

    assert from_environment.store.root == first and from_environment.source == LAKE_ENV
    assert "PBI_LAKE" in from_environment.store.why_read_only()
    assert from_option.store.root == second and from_option.source == "--lake"


def test_the_environment_beats_the_work_lake(cache_folder, tmp_path):
    a_lake(cache_folder / "lake")
    other = a_lake(tmp_path / "other").root

    opened = resolve_lake(None, environ={LAKE_ENV: str(other)})

    assert opened.store.root == other and opened.readonly


def test_a_plan_file_names_a_lake_that_the_option_and_the_environment_overrule(
    cache_folder, tmp_path
):
    a_lake(cache_folder / "lake")
    planned = a_lake(tmp_path / "planned").root
    other = a_lake(tmp_path / "other").root

    by_plan = resolve_lake(None, environ={}, plan_lake=str(planned))
    by_environment = resolve_lake(
        None, environ={LAKE_ENV: str(other)}, plan_lake=str(planned)
    )
    by_option = resolve_lake(str(other), environ={}, plan_lake=str(planned))

    assert by_plan.store.root == planned and by_plan.source == "the plan file"
    assert by_plan.readonly and not by_plan.work
    assert "opened with the plan file" in by_plan.store.why_read_only()
    assert by_environment.store.root == other and by_environment.source == LAKE_ENV
    assert by_option.store.root == other and by_option.source == "--lake"


@pytest.mark.parametrize("as_cache_folder", [True, False])
def test_the_work_lake_is_the_work_lake_before_the_first_sync_has_made_it(
    cache_folder, as_cache_folder
):
    assert not (cache_folder / "lake").exists()  # nothing was synced yet
    asked = cache_folder if as_cache_folder else cache_folder / "lake"

    by_option = resolve_lake(str(asked), environ={})
    by_plan = resolve_lake(None, environ={}, plan_lake=str(asked))

    for opened in (by_option, by_plan):
        assert opened.work and not opened.readonly
        assert opened.store.root == cache_folder / "lake"


def test_a_folder_that_is_not_the_cache_folder_is_not_the_work_lake_before_a_sync(
    cache_folder, tmp_path
):
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()

    with pytest.raises(PBIError, match="no data lake at"):
        resolve_lake(str(elsewhere), environ={})


def test_what_is_the_work_lake(cache_folder, tmp_path):
    assert is_work_lake(cache_folder)
    assert is_work_lake(cache_folder / "lake")
    assert is_work_lake(as_path(str(cache_folder) + os.sep))  # a trailing slash
    assert not is_work_lake(tmp_path / "other")
    assert not is_work_lake(cache_folder.parent)  # the folder around it


def test_there_is_no_work_lake_without_a_cache_folder(tmp_path):
    assert not is_work_lake(tmp_path)


def test_a_place_without_a_lake_says_where_the_location_came_from(
    cache_folder, tmp_path
):
    nothing = str(tmp_path / "nothing-here")

    from_option = _message(lambda: resolve_lake(nothing, environ={}))
    from_environment = _message(lambda: resolve_lake(None, environ={LAKE_ENV: nothing}))
    from_plan = _message(lambda: resolve_lake(None, environ={}, plan_lake=nothing))

    assert (
        f"There is no data lake at {nothing}: nothing there looks like one."
        in from_option
    )
    assert "session.lake" not in from_option and LAKE_ENV not in from_option
    assert (
        "environment variable PBI_LAKE: unset it to open your work lake"
        in from_environment
    )
    assert (
        "the `session.lake` of the plan file: leave that out to open your work lake "
        "(the cache folder), which a sync writes to"
    ) in from_plan


def _message(call) -> str:
    with pytest.raises(PBIError) as refused:
        call()
    return str(refused.value)


def test_a_plan_file_without_a_lake_leaves_the_work_lake(cache_folder):
    a_lake(cache_folder / "lake")

    opened = resolve_lake(None, environ={}, plan_lake=None)

    assert opened.work and opened.source == "the cache folder"
    assert resolve_lake(None, environ={}, plan_lake="").work  # nothing named


def test_a_plan_file_that_names_the_work_lake_leaves_it_writable(cache_folder):
    a_lake(cache_folder / "lake")

    opened = resolve_lake(None, environ={}, plan_lake=str(cache_folder))

    assert opened.work and not opened.readonly
    assert opened.source == "the plan file"


@pytest.mark.parametrize("make", ["missing", "empty"])
def test_a_place_without_a_lake_is_an_error(tmp_path, make):
    place = tmp_path / "nothing"
    if make == "empty":
        place.mkdir()

    with pytest.raises(PBIError, match="no data lake at"):
        resolve_lake(str(place), environ={})


def test_a_work_lake_that_is_not_there_yet_is_fine(cache_folder):
    opened = resolve_lake(str(cache_folder / "lake"), environ={})

    assert opened.work and not opened.readonly


def test_a_published_lake_is_protected_even_when_it_is_the_work_lake(cache_folder):
    LakeStore(a_lake(cache_folder / "lake").root, publishing=True).write_marker(
        PublishInfo(published_at=NOW, published_by="ana@laptop")
    )

    opened = resolve_lake(environ={})

    assert opened.work and opened.readonly
    assert "published by ana@laptop" in opened.store.why_read_only()


def test_a_lake_that_cannot_be_read_is_an_error_that_names_the_location(monkeypatch):
    def broken(location):
        raise RuntimeError("no credentials")

    monkeypatch.setattr("pbi_cli.session.as_path", broken)

    with pytest.raises(
        PBIError, match="Cannot read the lake at s3://bucket/x: no cred"
    ):
        resolve_lake("s3://bucket/x", environ={})


def test_a_remote_lake_is_opened_read_only(local_s3, cache_folder):
    a_lake("s3://bucket/shared")

    opened = resolve_lake("s3://bucket/shared", environ={})

    assert str(opened.store.root) == "s3://bucket/shared"
    assert opened.readonly and opened.store.tenants() == ["tenant-1"]
    with pytest.raises(ReadOnlyLake):
        opened.store.write_state("tenant-1", "x", {})


def test_a_remote_cache_folder_stands_for_the_lake_inside_it(local_s3, cache_folder):
    a_lake("s3://bucket/pbi/lake")

    opened = resolve_lake("s3://bucket/pbi", environ={})

    assert str(opened.store.root) == "s3://bucket/pbi/lake"


def test_a_folder_with_a_tenant_is_its_own_lake_root(tmp_path):
    root = a_lake(tmp_path / "lake").root

    assert lake_root(root) == root
