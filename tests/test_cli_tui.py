"""``pbi tui``, and what a bare ``pbi`` does: the command line side of the TUI."""

from datetime import timedelta

import pytest
from core_helpers import NOW, make_token
from loguru import logger
from typer.testing import CliRunner

from pbi_cli import cli_tui
from pbi_cli.cli import app, load_auth
from pbi_cli.config import PBIConfig
from pbi_cli.core.registry import Scope
from pbi_cli.core.store import LakeStore
from pbi_cli.errors import PBIError


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
    assert "--lake" in result.output
    assert opened == {}


def test_it_opens_the_tui_on_the_lake(opened, cache_folder):
    result = invoke("tui")

    assert result.exit_code == 0, result.output
    backend = opened["backend"]
    assert backend.store.root == cache_folder / "lake"
    assert opened["tenant"] is None


def a_lake(folder):
    store = LakeStore(folder)
    store.write_snapshot("tenant-1", "admin.groups", {}, {"value": [{"id": "w"}]})
    return store.root


def test_it_opens_another_lake_read_only_with_no_cache_folder(opened, tmp_path):
    root = a_lake(tmp_path / "shared")

    result = invoke("tui", "--lake", str(root))

    assert result.exit_code == 0, result.output
    backend = opened["backend"]
    assert backend.store.root == root
    assert "only reads" in backend.readonly
    assert backend.work_lake is None


def test_it_remembers_the_lakes_that_were_opened(opened, cache_folder, tmp_path):
    first, second = a_lake(tmp_path / "a"), a_lake(tmp_path / "b")

    invoke("tui", "--lake", str(first))
    invoke("tui", "--lake", str(second))
    invoke("tui")  # the work lake is not a recent lake

    assert PBIConfig().recent_lakes == [str(second), str(first)]


def test_the_work_lake_is_still_written_when_a_lake_is_named_that_is_the_same(
    opened, cache_folder
):
    root = a_lake(cache_folder / "lake")

    invoke("tui", "--lake", str(root))

    backend = opened["backend"]
    assert backend.readonly == "" and backend.work_lake == str(root)
    assert PBIConfig().recent_lakes == []


# ---------------------------------------------------------------------------
# pbi tui --config
# ---------------------------------------------------------------------------


def write_plan(folder, text):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "pbi-plan.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_plan_file_goes_to_the_backend(opened, cache_folder, tmp_path):
    path = write_plan(tmp_path, "version: 1\nsession: {lazy: auto, open: Fin}\n")

    result = invoke("tui", "--config", str(path))

    assert result.exit_code == 0, result.output
    backend = opened["backend"]
    assert backend.plan.session.lazy == "auto" and backend.plan.path == path.resolve()
    assert backend.store.root == cache_folder / "lake"  # the plan names no lake


def test_without_a_plan_file_the_backend_has_none(opened, cache_folder):
    invoke("tui")

    assert opened["backend"].plan is None and opened["backend"].reload_plan is None


def test_the_lake_of_the_plan_file_is_opened_read_only(opened, cache_folder, tmp_path):
    shared = a_lake(tmp_path / "shared")
    path = write_plan(
        tmp_path / "plans", f"version: 1\nsession: {{lake: '{shared}'}}\n"
    )

    result = invoke("tui", "--config", str(path))

    assert result.exit_code == 0, result.output
    backend = opened["backend"]
    assert backend.store.root == shared
    assert "opened with the plan file" in backend.readonly


def test_a_lake_of_the_plan_file_is_relative_to_the_file(opened, tmp_path):
    a_lake(tmp_path / "plans" / "shared")
    path = write_plan(tmp_path / "plans", "version: 1\nsession: {lake: shared}\n")

    result = invoke("tui", "--config", str(path))

    assert result.exit_code == 0, result.output
    assert opened["backend"].store.root == tmp_path / "plans" / "shared"


def test_the_option_and_the_environment_beat_the_lake_of_the_plan_file(
    opened, tmp_path, monkeypatch
):
    planned = a_lake(tmp_path / "planned")
    typed = a_lake(tmp_path / "typed")
    from_environment = a_lake(tmp_path / "environment")
    path = write_plan(tmp_path / "p", f"version: 1\nsession: {{lake: '{planned}'}}\n")

    invoke("tui", "--config", str(path), "--lake", str(typed))
    option = opened["backend"].store.root
    monkeypatch.setenv(cli_tui.LAKE_ENV, str(from_environment))
    invoke("tui", "--config", str(path))
    environment = opened["backend"].store.root

    assert option == typed and environment == from_environment


def test_the_lake_of_the_plan_file_is_remembered_like_any_other(
    opened, cache_folder, tmp_path
):
    shared = a_lake(tmp_path / "shared")
    path = write_plan(tmp_path / "p", f"version: 1\nsession: {{lake: '{shared}'}}\n")

    invoke("tui", "--config", str(path))

    assert PBIConfig().recent_lakes == [str(shared)]


def test_the_plan_file_can_be_read_again(opened, cache_folder, tmp_path):
    path = write_plan(tmp_path, "version: 1\nsession: {lazy: ask}\n")
    invoke("tui", "--config", str(path))
    backend = opened["backend"]

    path.write_text("version: 1\nsession: {lazy: auto}\n", encoding="utf-8")
    again = backend.read_plan_again()

    assert again.session.lazy == "auto" and backend.plan is again


def test_a_plan_file_that_is_wrong_when_it_is_read_again_leaves_the_old_one(
    opened, cache_folder, tmp_path
):
    from pbi_cli.core.planfile import PlanFileError

    path = write_plan(tmp_path, "version: 1\nsession: {lazy: ask}\n")
    invoke("tui", "--config", str(path))
    backend = opened["backend"]
    old = backend.plan

    path.write_text("version: 2\n", encoding="utf-8")
    with pytest.raises(PlanFileError, match="version: 2 is not a version"):
        backend.read_plan_again()

    assert backend.plan is old


def test_a_session_without_a_plan_file_has_nothing_to_read_again(opened, cache_folder):
    invoke("tui")

    with pytest.raises(PBIError, match="no plan file"):
        opened["backend"].read_plan_again()
    with pytest.raises(PBIError, match="no plan file"):
        opened["backend"].plan_run()


def test_a_lake_of_the_plan_file_that_is_not_there_says_to_leave_it_out(
    opened, cache_folder, tmp_path
):
    path = write_plan(
        tmp_path / "p", f"version: 1\nsession: {{lake: '{tmp_path / 'nope'}'}}\n"
    )

    result = invoke("tui", "--config", str(path))

    assert result.exit_code == 1
    assert "There is no data lake at" in result.output and "nope" in result.output
    assert "leave that out to open your work lake" in result.output
    assert opened == {}


def test_the_cache_folder_as_the_lake_of_the_plan_file_is_the_work_lake_before_a_sync(
    opened, cache_folder, tmp_path
):
    path = write_plan(
        tmp_path / "p", f"version: 1\nsession: {{lake: '{cache_folder}'}}\n"
    )

    result = invoke("tui", "--config", str(path))

    assert result.exit_code == 0, result.output
    backend = opened["backend"]
    assert backend.store.root == cache_folder / "lake" and not backend.readonly


def test_a_way_to_read_a_plan_again_is_no_plan(opened, cache_folder):
    invoke("tui")
    backend = opened["backend"]
    backend.reload_plan = lambda: pytest.fail("there is no plan to read again")

    with pytest.raises(PBIError, match="no plan file to read again"):
        backend.read_plan_again()


def test_a_plan_file_that_is_wrong_is_said_before_the_tui_opens(
    opened, cache_folder, tmp_path
):
    path = write_plan(tmp_path, "version: 1\nsession: {lazy: sometimes}\n")

    result = invoke("tui", "--config", str(path))

    assert result.exit_code == 1
    assert "pbi-plan.yaml:2: session.lazy: 'sometimes' is not a mode" in result.output
    assert opened == {}


def test_a_file_that_does_not_exist_is_a_usage_error(opened, cache_folder, tmp_path):
    result = invoke("tui", "--config", str(tmp_path / "nope.yaml"))

    assert result.exit_code == 2 and "does not exist" in result.output
    assert opened == {}


def test_the_help_tells_about_the_plan_file():
    result = invoke("tui", "--help")

    assert "--config" in result.output and "plan file" in result.output.lower()
    assert "session" in result.output


def test_the_environment_names_the_lake_too(opened, tmp_path, monkeypatch):
    monkeypatch.setenv("PBI_LAKE", str(a_lake(tmp_path / "shared")))

    result = invoke("tui")

    assert result.exit_code == 0 and opened["backend"].store.root == tmp_path / "shared"


def test_a_place_without_a_lake_is_an_error(opened, tmp_path):
    result = invoke("tui", "--lake", str(tmp_path / "nothing"))

    assert result.exit_code == 1 and "no data lake at" in result.output
    assert opened == {}


def test_the_backend_can_open_other_lakes_for_the_dialog(
    opened, cache_folder, tmp_path
):
    invoke("tui")
    backend = opened["backend"]
    other = a_lake(tmp_path / "other")

    store = backend.open_lake(str(other))

    assert store.root == other and not store.writable
    assert backend.recent_lakes() == [str(other)]
    assert backend.open_lake(str(cache_folder / "lake")).writable  # the work lake again
    assert backend.recent_lakes() == [str(other)]


def test_the_backend_says_when_a_lake_cannot_be_opened(opened, cache_folder, tmp_path):
    invoke("tui")

    with pytest.raises(PBIError, match="no data lake at"):
        opened["backend"].open_lake(str(tmp_path / "nothing"))


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


def test_the_environment_lake_lets_a_bare_pbi_open_the_tui(monkeypatch, tmp_path):
    class Terminal:
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", Terminal())
    monkeypatch.setattr("sys.stdout", Terminal())
    monkeypatch.setattr(cli_tui, "textual_available", lambda: True)
    assert cli_tui.should_launch() is False  # no cache folder, no PBI_LAKE

    monkeypatch.setenv("PBI_LAKE", str(tmp_path))

    assert cli_tui.should_launch() is True


def test_the_backend_lists_the_stored_profiles_with_who_they_are(opened, cache_folder):
    invoke("tui")
    backend = opened["backend"]
    expires = timedelta(hours=1)
    backend.sign_in(
        make_token(tenant="tenant-1", expires_in=expires, upn="adm@x.com"),
        "adm",
        "admin",
    )
    backend.sign_in(
        make_token(tenant="tenant-1", expires_in=expires, upn="svc@x.com"),
        "svc",
        "user",
    )
    backend.sign_in(
        make_token(tenant="tenant-1", expires_in=expires, upn="ana@x.com"),
        "ana",
        "user",
    )

    found = {(a.group, a.profile): a for a in backend.accounts()}

    assert set(found) == {("admin", "adm"), ("user", "svc"), ("user", "ana")}
    assert found[("admin", "adm")].active and found[("user", "svc")].active
    assert not found[("user", "ana")].active  # the first of a group is the active one
    assert found[("user", "svc")].name == "svc@x.com"
    assert found[("user", "svc")].tenant == "tenant-1"
    assert found[("user", "svc")].expires_at == NOW + expires
    assert all(a.has_token for a in found.values())


def test_a_profile_whose_token_is_gone_is_listed_without_one(
    opened, cache_folder, memory_keyring
):
    invoke("tui")
    backend = opened["backend"]
    backend.sign_in(make_token(tenant="tenant-1"), "svc", "user")
    memory_keyring.delete_password("pbi-cli", "svc")

    (account,) = backend.accounts()

    assert account.profile == "svc" and account.has_token is False
    assert account.name is None and account.expires_at is None


def test_making_a_profile_active_is_what_pbi_profile_switch_does(opened, cache_folder):
    invoke("tui")
    backend = opened["backend"]
    backend.sign_in(make_token(tenant="tenant-1", oid="o-svc"), "svc", "user")
    backend.sign_in(make_token(tenant="tenant-1", oid="o-ana"), "ana", "user")
    assert backend.client_for(Scope.USER).profile_name() == "svc"  # the first is active

    backend.activate("user", "ana")

    assert PBIConfig().get_group_active_profile("user") == "ana"
    assert backend.active_profile("user") == "ana"
    assert backend.client_for(Scope.USER).profile_name() == "ana"  # the pool was reset
    assert backend.client_for(Scope.USER).identity_key() == "o-ana"


def test_a_client_can_be_asked_for_by_profile(opened, cache_folder):
    invoke("tui")
    backend = opened["backend"]
    backend.sign_in(make_token(tenant="tenant-1", oid="o-svc"), "svc", "user")
    backend.sign_in(make_token(tenant="tenant-1", oid="o-ana"), "ana", "user")

    by_name = backend.client_for(Scope.USER, "ana")

    assert by_name.profile_name() == "ana" and by_name.identity_key() == "o-ana"
    assert backend.client_for(Scope.USER, "ana") is by_name  # made once
    assert backend.client_for(Scope.USER).profile_name() == "svc"  # the active one
