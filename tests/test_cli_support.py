"""The client pool, and storing a token the way ``pbi auth`` does."""

import threading
from contextlib import contextmanager
from datetime import timedelta

import pytest
from core_helpers import make_token

from pbi_cli.cli import StoredToken, load_auth, store_token
from pbi_cli.cli_support import ClientPool
from pbi_cli.config import PBIConfig
from pbi_cli.core.auth import Credentials
from pbi_cli.core.client import PowerBIClient
from pbi_cli.core.registry import Scope
from pbi_cli.errors import AuthError

# ---------------------------------------------------------------------------
# the client pool
# ---------------------------------------------------------------------------


class Made:
    """Records the clients a pool asked for, and when they were closed."""

    def __init__(self):
        self.groups = []
        self.closed = []

    @contextmanager
    def __call__(self, group):
        client = object()
        self.groups.append(group)
        try:
            yield client
        finally:
            self.closed.append(client)


def test_a_client_is_made_when_a_scope_is_first_needed_and_then_reused():
    made = Made()
    pool = ClientPool(made)

    admin = pool(Scope.ADMIN)

    assert pool(Scope.ADMIN) is admin
    assert made.groups == ["admin"]
    user = pool(Scope.USER)
    assert user is not admin and made.groups == ["admin", "user"]


def test_reset_closes_the_clients_and_the_next_call_makes_new_ones():
    made = Made()
    pool = ClientPool(made)
    first = pool(Scope.ADMIN)

    pool.reset()

    assert made.closed == [first]
    second = pool(Scope.ADMIN)
    assert second is not first and made.groups == ["admin", "admin"]


def test_leaving_the_pool_closes_every_client():
    made = Made()
    with ClientPool(made) as pool:
        clients = [pool(Scope.ADMIN), pool(Scope.USER)]
        assert made.closed == []

    assert made.closed == clients[::-1]  # the last one opened is the first one closed


def test_a_pool_can_be_asked_from_threads():
    made = Made()
    pool = ClientPool(made)
    found = []

    def ask():
        found.append(pool(Scope.ADMIN))

    threads = [threading.Thread(target=ask) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert made.groups == ["admin"] and len({id(c) for c in found}) == 1


def test_the_default_pool_builds_the_clients_of_the_command_line(signed_in):
    with ClientPool() as pool:
        client = pool(Scope.ADMIN)

        assert isinstance(client, PowerBIClient)
        assert client.tenant_key() == "tenant-1"


# ---------------------------------------------------------------------------
# what the client says about the token
# ---------------------------------------------------------------------------


def test_the_client_reports_what_its_token_states():
    token = make_token(tenant="tenant-9", expires_in=timedelta(hours=2))
    client = PowerBIClient(lambda: Credentials(token, profile="p", group="admin"))

    info = client.token_info()

    assert info.tenant_id == "tenant-9"
    assert info.expires_at is not None


# ---------------------------------------------------------------------------
# storing a token
# ---------------------------------------------------------------------------


def test_a_token_is_stored_for_a_profile_of_a_group(memory_keyring):
    stored = store_token("abc", "admin-nlm", "admin")

    assert stored == StoredToken("admin-nlm", "admin", True)
    assert memory_keyring.get_password("pbi-cli", "admin-nlm") == "abc"
    assert PBIConfig().get_group_active_profile("admin") == "admin-nlm"
    assert load_auth(group="admin") == {"Authorization": "Bearer abc"}


def test_a_second_profile_does_not_take_over_the_group():
    store_token("abc", "first", "admin")

    again = store_token("def", "second", "admin")

    assert again.active is False
    assert PBIConfig().get_group_active_profile("admin") == "first"
    assert PBIConfig().has_profile_in_group("admin", "second")


def test_a_fresh_token_replaces_the_old_one_of_the_profile(memory_keyring):
    store_token("old", "admin-nlm", "admin")

    stored = store_token("new", "admin-nlm", "admin")

    assert stored.active is True
    assert memory_keyring.get_password("pbi-cli", "admin-nlm") == "new"


def test_the_bearer_prefix_is_removed(memory_keyring):
    store_token("Bearer abc", "p", "user")

    assert memory_keyring.get_password("pbi-cli", "p") == "abc"


def test_a_token_without_a_group_goes_to_the_flat_profiles(memory_keyring):
    stored = store_token("abc", "default")

    assert stored == StoredToken("default", None, True)
    assert load_auth(profile="default") == {"Authorization": "Bearer abc"}
    other = store_token("def", "other")
    assert other.active is False


def test_loading_a_token_that_was_never_stored_is_an_auth_error():
    with pytest.raises(AuthError):
        load_auth(group="admin")
