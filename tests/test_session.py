"""``pbi_cli.session`` builds the lake and the client from the user's settings."""

import warnings
from datetime import timedelta
from pathlib import Path

from core_helpers import make_response, make_token
from urllib3.exceptions import InsecureRequestWarning

from pbi_cli.config import PBIConfig
from pbi_cli.core.auth import Credentials
from pbi_cli.core.registry import get_endpoint
from pbi_cli.core.store import LakeStore
from pbi_cli.session import lake_hint, lake_path, open_client, open_lake, quota_file

TOKEN = make_token(tenant="tenant-1", expires_in=timedelta(days=36500))


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
