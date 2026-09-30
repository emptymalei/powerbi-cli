"""Fixtures shared by the tests.

Commands keep settings, tokens, quota counters and the data lake under the home folder and
tokens in the system keyring, so no test may run against the real ones: every test gets an
empty home and an in-memory keyring.
"""

from datetime import timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

import keyring
import pytest
from core_helpers import FakeAdapter, make_token
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from pbi_cli.config import PBIConfig


class MemoryKeyring(KeyringBackend):
    """A keyring that lives in a dict (the real one would be the user's keychain)."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self._items: Dict[Tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> Optional[str]:
        return self._items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self._items[(service, username)]
        except KeyError:
            raise PasswordDeleteError("Password not found") from None


@pytest.fixture(autouse=True)
def memory_keyring():
    """Keep tokens out of the real keyring (macOS Keychain, Credential Manager, ...)."""
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch) -> Path:
    """Point the home folder (and the config folders derived from it) at an empty folder."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home


@pytest.fixture
def cache_folder(isolated_home) -> Path:
    """A configured cache folder: the data lake of the commands lives in it."""
    folder = isolated_home / "cache"
    PBIConfig().cache_folder = str(folder)
    return folder


@pytest.fixture
def fake_api(monkeypatch) -> FakeAdapter:
    """Answer the API client's requests from a script instead of the network."""
    adapter = FakeAdapter()
    monkeypatch.setattr("pbi_cli.core.client.make_session", adapter.session)
    return adapter


@pytest.fixture
def signed_in(monkeypatch) -> str:
    """Make ``load_auth`` return a long-lived token of ``tenant-1``; returns the token."""
    token = make_token(tenant="tenant-1", expires_in=timedelta(days=36500))
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(
        "pbi_cli.cli.load_auth", lambda profile=None, group="user": dict(headers)
    )
    return token
