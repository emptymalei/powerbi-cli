"""``pbi tui``, and what a bare ``pbi`` does: the command line side of the TUI."""

import pytest
from loguru import logger
from typer.testing import CliRunner

from pbi_cli import cli_tui
from pbi_cli.cli import app, load_auth
from pbi_cli.config import PBIConfig
from pbi_cli.core.registry import Scope


@pytest.fixture
def opened(monkeypatch):
    """Replace the TUI by a recorder of how it was opened."""
    seen = {}

    def fake_run(backend, tenant=None):
        seen["backend"] = backend
        seen["tenant"] = tenant
        seen["handlers"] = len(logger._core.handlers)

    monkeypatch.setattr(cli_tui, "run", fake_run)
    monkeypatch.setattr(cli_tui, "textual_available", lambda: True)
    return seen


def invoke(*args):
    return CliRunner().invoke(app, list(args))


# ---------------------------------------------------------------------------
# pbi tui
# ---------------------------------------------------------------------------


def test_it_says_what_to_install_without_textual(monkeypatch, cache_folder):
    monkeypatch.setattr(cli_tui, "textual_available", lambda: False)

    result = invoke("tui")

    assert result.exit_code == 1
    assert "The TUI needs Textual" in result.output and "pbi-cli[tui]" in result.output


def test_it_says_what_to_do_without_a_lake(opened):
    result = invoke("tui")

    assert result.exit_code == 1
    assert "browses the data lake" in result.output
    assert "pbi config set-cache-folder" in result.output
    assert opened == {}


def test_it_opens_the_tui_on_the_lake(opened, cache_folder):
    result = invoke("tui")

    assert result.exit_code == 0, result.output
    backend = opened["backend"]
    assert backend.store.root == cache_folder / "lake"
    assert opened["tenant"] is None


def test_a_tenant_can_be_given(opened, cache_folder):
    invoke("tui", "-t", "72f988bf")

    assert opened["tenant"] == "72f988bf"


def test_it_browses_the_lake_even_when_caching_is_switched_off(opened, cache_folder):
    PBIConfig().cache_enabled = False

    result = invoke("tui")

    assert (
        result.exit_code == 0 and opened["backend"].store.root == cache_folder / "lake"
    )


def test_a_token_that_is_typed_in_the_tui_is_stored_like_pbi_auth(
    opened, cache_folder, memory_keyring
):
    invoke("tui")
    backend = opened["backend"]

    backend.sign_in("abc.def.ghi", "admin-nlm", "admin")

    assert memory_keyring.get_password("pbi-cli", "admin-nlm") == "abc.def.ghi"
    assert backend.active_profile("admin") == "admin-nlm"
    assert backend.active_profile("user") is None
    assert load_auth(group="admin") == {"Authorization": "Bearer abc.def.ghi"}


def test_the_clients_look_the_token_up_again_after_signing_in(opened, cache_folder):
    invoke("tui")
    backend = opened["backend"]
    backend.sign_in("first.token.x", "p", "admin")
    first = backend.client_for(Scope.ADMIN)
    assert first.profile_name() == "p"

    backend.sign_in("second.token.x", "p", "admin")
    second = backend.client_for(Scope.ADMIN)

    assert second is not first  # the pool was reset, so the new token is read
    assert second._credentials().token == "second.token.x"


def test_the_logger_does_not_write_over_the_screen(opened, cache_folder, tmp_path):
    before = len(logger._core.handlers)

    invoke("tui")

    assert opened["handlers"] == 1  # only the file of the TUI
    assert len(logger._core.handlers) == before  # and the usual one is back
    assert cli_tui.log_file().parent.exists()


def test_a_log_file_that_cannot_be_made_does_not_stop_the_tui(
    opened, cache_folder, monkeypatch, tmp_path
):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a folder should be")
    monkeypatch.setattr(cli_tui, "log_file", lambda: blocker / "tui.log")

    result = invoke("tui")

    assert result.exit_code == 0 and opened["handlers"] == 0


def test_the_logger_is_put_back_when_the_tui_fails(cache_folder, monkeypatch):
    monkeypatch.setattr(cli_tui, "textual_available", lambda: True)
    before = len(logger._core.handlers)

    def broken(backend, tenant=None):
        raise RuntimeError("the screen broke")

    monkeypatch.setattr(cli_tui, "run", broken)

    result = invoke("tui")

    assert result.exit_code != 0 and isinstance(result.exception, RuntimeError)
    assert len(logger._core.handlers) == before


# ---------------------------------------------------------------------------
# a bare pbi
# ---------------------------------------------------------------------------


def test_a_bare_pbi_greets_when_it_is_not_in_a_terminal(opened, cache_folder):
    result = invoke()

    assert result.exit_code == 0 and "Welcome to pbi cli" in result.output
    assert opened == {}


def test_a_bare_pbi_opens_the_tui_in_a_terminal_with_a_lake(
    opened, cache_folder, monkeypatch
):
    monkeypatch.setattr(cli_tui, "should_launch", lambda: True)
    monkeypatch.setattr("pbi_cli.cli.should_launch", lambda: True)

    result = invoke()

    assert result.exit_code == 0 and "Welcome" not in result.output
    assert opened["backend"].store.root == cache_folder / "lake"


def test_a_bare_pbi_reports_a_problem_in_a_line(monkeypatch, cache_folder):
    monkeypatch.setattr("pbi_cli.cli.should_launch", lambda: True)
    monkeypatch.setattr(cli_tui, "textual_available", lambda: False)

    result = invoke()

    assert result.exit_code == 1 and "Error: The TUI needs Textual" in result.output


@pytest.mark.parametrize(
    "stdin, stdout, textual, lake, expected",
    [
        (True, True, True, True, True),
        (False, True, True, True, False),
        (True, False, True, True, False),
        (True, True, False, True, False),
        (True, True, True, False, False),
    ],
)
def test_the_tui_opens_on_a_bare_pbi_only_when_it_can(
    monkeypatch, cache_folder, stdin, stdout, textual, lake, expected
):
    class Stream:
        def __init__(self, tty):
            self._tty = tty

        def isatty(self):
            return self._tty

    monkeypatch.setattr("sys.stdin", Stream(stdin))
    monkeypatch.setattr("sys.stdout", Stream(stdout))
    monkeypatch.setattr(cli_tui, "textual_available", lambda: textual)
    if not lake:
        PBIConfig().cache_folder = None

    assert cli_tui.should_launch() is expected
