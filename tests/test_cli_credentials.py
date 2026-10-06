"""Where a token is stored: the keyring, or the credentials file when the keyring will not hold it.

The Windows Credential Manager takes at most about 1,200 characters, and a Power BI token is
longer, so on Windows the file is the usual place: what must hold is that the file is then read
(an older, shorter token that did fit in the keyring must not hide it), and that what is said
about it is true.
"""

import json

import keyring
from conftest import MemoryKeyring
from keyring.errors import PasswordSetError
from loguru import logger
from typer.testing import CliRunner

from pbi_cli import cli
from pbi_cli.cli import (
    IN_KEYRING,
    KEYRING_SERVICE,
    _get_credential,
    _get_credentials_file,
    app,
    store_token,
)

LONG = "x" * 100


class LimitedKeyring(MemoryKeyring):
    """A keyring like the Credential Manager: it refuses a token that is too long."""

    LIMIT = 40

    def set_password(self, service, username, password):
        if len(password) > self.LIMIT:
            raise PasswordSetError("The stub received bad data.")
        super().set_password(service, username, password)


def warnings_of(action):
    """The warnings that the logger got while ``action`` ran."""
    seen = []
    handler = logger.add(
        lambda m: seen.append(str(m)), level="WARNING", format="{message}"
    )
    try:
        action()
    finally:
        logger.remove(handler)
    return seen


def test_a_token_that_the_keyring_holds_is_stored_there(memory_keyring):
    stored = store_token("abc", "adm", "admin")

    assert stored.where == IN_KEYRING == "keyring"
    assert memory_keyring.get_password(KEYRING_SERVICE, "adm") == "abc"
    assert not _get_credentials_file().exists()


def test_a_token_that_the_keyring_refuses_goes_to_the_credentials_file():
    keyring.set_keyring(LimitedKeyring())

    stored = store_token(LONG, "adm", "admin")

    assert stored.where == str(_get_credentials_file())
    assert json.loads(_get_credentials_file().read_text())["adm"] == LONG
    assert _get_credential("adm") == LONG


def test_an_older_token_in_the_keyring_does_not_hide_a_newer_one_in_the_file():
    keyring.set_keyring(LimitedKeyring())
    store_token("short", "adm", "admin")  # it fits
    assert keyring.get_password(KEYRING_SERVICE, "adm") == "short"

    store_token(LONG, "adm", "admin")  # this one does not

    assert _get_credential("adm") == LONG
    assert keyring.get_password(KEYRING_SERVICE, "adm") is None


def test_the_token_of_another_profile_stays_in_the_keyring():
    keyring.set_keyring(LimitedKeyring())
    store_token("short", "svc", "user")

    store_token(LONG, "adm", "admin")

    assert keyring.get_password(KEYRING_SERVICE, "svc") == "short"
    assert _get_credential("svc") == "short" and _get_credential("adm") == LONG


def test_a_token_that_fits_again_is_read_from_the_keyring():
    keyring.set_keyring(LimitedKeyring())
    store_token(LONG, "adm", "admin")  # in the file

    store_token("short", "adm", "admin")  # in the keyring, which is read first

    assert _get_credential("adm") == "short"


def test_what_is_said_of_a_token_the_keyring_refused_is_true():
    keyring.set_keyring(LimitedKeyring())

    said = warnings_of(lambda: store_token(LONG, "adm", "admin"))

    [message] = [m for m in said if "keyring" in m]
    assert "did not take the token" in message
    assert str(_get_credentials_file()) in message
    assert "install a keyring backend" not in message  # that would not help


def test_what_is_said_when_there_is_no_keyring_gives_the_advice(monkeypatch):
    monkeypatch.setattr(cli, "_check_keyring_availability", lambda: False)

    said = warnings_of(lambda: store_token("abc", "adm", "admin"))

    [message] = [m for m in said if "eyring" in m]
    assert "Keyring not available" in message and "install a keyring backend" in message
    assert _get_credential("adm") == "abc"


def test_pbi_auth_says_securely_only_when_the_keyring_holds_the_token():
    result = CliRunner().invoke(app, ["auth", "-t", "abc", "-p", "adm", "-g", "admin"])

    assert result.exit_code == 0
    assert (
        "✓ Credentials saved securely for profile 'adm' in group 'admin'"
        in result.output
    )


def test_pbi_auth_says_where_the_token_went_when_the_keyring_did_not_hold_it():
    keyring.set_keyring(LimitedKeyring())

    result = CliRunner().invoke(app, ["auth", "-t", LONG, "-p", "adm", "-g", "admin"])

    assert result.exit_code == 0
    where = str(_get_credentials_file())
    assert (
        f"✓ Credentials saved in {where} (the system keyring did not hold the token) "
        "for profile 'adm' in group 'admin'"
    ) in result.output
    assert "securely" not in result.output


def test_pbi_auth_without_a_group_says_it_too():
    keyring.set_keyring(LimitedKeyring())

    result = CliRunner().invoke(app, ["auth", "-t", LONG, "-p", "plain"])

    assert "✓ Credentials saved in " in result.output
    assert "for profile 'plain'" in result.output
    assert "securely" not in result.output
