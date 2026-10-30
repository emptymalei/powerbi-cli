"""Tokens are kept per group and profile: ``admin:default`` and ``user:default`` are two.

Tokens used to be kept under the bare profile name, so the ``default`` profile of the user
group and the one of the admin group shared one: admin commands then sent the user token,
and deleting the user profile deleted the admin token. Tokens stored that way are still
read, and moved to the name of the group and profile.
"""

import json
import stat

import pytest
from typer.testing import CliRunner

from pbi_cli import cli
from pbi_cli.cli import (
    KEYRING_SERVICE,
    _delete_credential,
    _get_credential,
    _get_credentials_file,
    _set_credential,
    app,
    load_auth,
    store_token,
)
from pbi_cli.config import PBIConfig
from pbi_cli.errors import AuthError


def run(*args: str):
    return CliRunner().invoke(app, list(args))


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def add_profile(group: str, profile: str = "default") -> None:
    config = PBIConfig()
    config.add_profile_to_group(group, profile)
    config.set_group_active_profile(group, profile)


@pytest.fixture
def no_keyring(monkeypatch):
    """Tokens go to the credentials file."""
    monkeypatch.setattr(cli, "_check_keyring_availability", lambda: False)


def test_the_default_profiles_of_the_two_groups_have_their_own_tokens(memory_keyring):
    store_token("user-token", "default", "user")
    store_token("admin-token", "default", "admin")

    assert load_auth(group="user") == bearer("user-token")
    assert load_auth(group="admin") == bearer("admin-token")
    assert memory_keyring.get_password(KEYRING_SERVICE, "user:default") == "user-token"
    assert (
        memory_keyring.get_password(KEYRING_SERVICE, "admin:default") == "admin-token"
    )


def test_pbi_auth_stores_the_token_of_each_group_apart():
    assert run("auth", "-t", "user-token", "-g", "user").exit_code == 0
    assert run("auth", "-t", "admin-token", "-g", "admin").exit_code == 0

    assert load_auth(group="user") == bearer("user-token")
    assert load_auth(group="admin") == bearer("admin-token")


def test_deleting_the_user_profile_keeps_the_admin_token():
    store_token("user-token", "default", "user")
    store_token("admin-token", "default", "admin")

    result = run("profile", "delete", "default", "-g", "user", "--yes")

    assert result.exit_code == 0
    assert load_auth(group="admin") == bearer("admin-token")
    assert _get_credential("default", "user") is None


def test_a_token_kept_under_the_bare_name_is_still_read_and_moved(memory_keyring):
    add_profile("admin")
    memory_keyring.set_password(KEYRING_SERVICE, "default", "old-token")

    assert load_auth(group="admin") == bearer("old-token")

    assert memory_keyring.get_password(KEYRING_SERVICE, "admin:default") == "old-token"
    assert memory_keyring.get_password(KEYRING_SERVICE, "default") is None


def test_a_bare_name_token_stays_while_another_group_still_uses_it(memory_keyring):
    add_profile("admin")
    add_profile("user")
    memory_keyring.set_password(KEYRING_SERVICE, "default", "old-token")

    assert load_auth(group="admin") == bearer("old-token")
    assert memory_keyring.get_password(KEYRING_SERVICE, "default") == "old-token"

    assert load_auth(group="user") == bearer("old-token")
    assert memory_keyring.get_password(KEYRING_SERVICE, "default") is None


def test_a_new_token_of_one_group_leaves_the_bare_name_token_to_the_other(
    memory_keyring,
):
    add_profile("user")
    memory_keyring.set_password(KEYRING_SERVICE, "default", "old-token")

    store_token("admin-token", "default", "admin")

    assert load_auth(group="admin") == bearer("admin-token")
    assert load_auth(group="user") == bearer("old-token")


def test_deleting_a_profile_keeps_the_bare_name_token_another_group_uses(
    memory_keyring,
):
    add_profile("admin")
    add_profile("user")
    memory_keyring.set_password(KEYRING_SERVICE, "default", "old-token")

    result = run("profile", "delete", "default", "-g", "user", "--yes")

    assert result.exit_code == 0
    assert load_auth(group="admin") == bearer("old-token")


def test_a_bare_name_token_in_the_credentials_file_is_read_too(no_keyring):
    add_profile("admin")
    _get_credentials_file().parent.mkdir(parents=True, exist_ok=True)
    _get_credentials_file().write_text(json.dumps({"default": "old-token"}))

    assert load_auth(group="admin") == bearer("old-token")
    assert json.loads(_get_credentials_file().read_text()) == {
        "admin:default": "old-token"
    }


def test_a_token_is_deleted_from_the_keyring_and_from_the_file(memory_keyring):
    memory_keyring.set_password(KEYRING_SERVICE, "admin:adm", "in-keyring")
    _get_credentials_file().parent.mkdir(parents=True, exist_ok=True)
    _get_credentials_file().write_text(
        json.dumps({"admin:adm": "in-file", "user:svc": "other"})
    )

    _delete_credential("adm", "admin")

    assert memory_keyring.get_password(KEYRING_SERVICE, "admin:adm") is None
    assert json.loads(_get_credentials_file().read_text()) == {"user:svc": "other"}
    assert _get_credential("adm", "admin") is None


def test_the_credentials_file_is_only_ever_readable_by_its_owner(
    no_keyring, monkeypatch
):
    modes = []
    real_open = cli.os.open

    def watching_open(path, flags, mode=0o777, *args, **kwargs):
        modes.append(mode)
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(cli.os, "open", watching_open)

    _set_credential("adm", "abc", "admin")

    assert modes == [0o600]
    assert stat.S_IMODE(_get_credentials_file().stat().st_mode) == 0o600
    assert [p.name for p in _get_credentials_file().parent.iterdir()] == [
        "credentials.json"
    ]


def test_a_failed_write_leaves_the_old_credentials_file(no_keyring, monkeypatch):
    _set_credential("adm", "abc", "admin")

    def broken_dump(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(cli.json, "dump", broken_dump)
    with pytest.raises(OSError):
        _set_credential("svc", "def", "user")

    assert json.loads(_get_credentials_file().read_text()) == {"admin:adm": "abc"}
    assert [p.name for p in _get_credentials_file().parent.iterdir()] == [
        "credentials.json"
    ]


def test_a_broken_credentials_file_is_an_auth_error(no_keyring):
    add_profile("admin")
    _get_credentials_file().parent.mkdir(parents=True, exist_ok=True)
    _get_credentials_file().write_text("{not json")

    with pytest.raises(AuthError) as error:
        load_auth(group="admin")

    assert str(_get_credentials_file()) in str(error.value)


def test_a_broken_credentials_file_is_reported_without_a_traceback(no_keyring):
    add_profile("admin")
    _get_credentials_file().parent.mkdir(parents=True, exist_ok=True)
    _get_credentials_file().write_text("[1, 2")

    result = run("profile", "list")

    assert result.exit_code == 1
    assert "Error: Could not read the stored tokens in" in result.output
    assert "Traceback" not in result.output
