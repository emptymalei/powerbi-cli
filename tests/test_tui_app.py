"""The app: who is signed in, the dialogs, the tenant, and the command palette."""

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from core_helpers import make_client, make_token
from sync_helpers import TENANT, World
from textual.screen import ModalScreen
from textual.widgets import Input, RadioSet
from tui_helpers import backend_of, plain, run_ui

from pbi_cli.errors import AuthError
from pbi_cli.tui import modals
from pbi_cli.tui.app import lake_label
from pbi_cli.tui.commands import commands_for
from pbi_cli.tui.palette import GotoProvider


@pytest.fixture
def world(tmp_path):
    world = World(tmp_path)
    world.run()
    return world


def no_token(scope):
    raise AuthError("No active profile set for group 'admin'.")


# ---------------------------------------------------------------------------
# the header
# ---------------------------------------------------------------------------


def test_the_header_counts_down_the_token(tmp_path):
    world = World(tmp_path)
    world.run("groups")
    world.admin = make_client(
        world.fake,
        clock=world.clock,
        store=world.store,
        token=make_token(tenant=TENANT, expires_in=timedelta(minutes=42)),
    )[0]

    async def scenario(ui):
        return ui.static("#who")

    assert "token 42 min" in run_ui(backend_of(world), scenario)


def test_the_header_says_when_the_token_has_expired(tmp_path):
    world = World(tmp_path)
    world.run("groups")
    world.admin = make_client(
        world.fake,
        clock=world.clock,
        store=world.store,
        token=make_token(tenant=TENANT, expires_in=timedelta(minutes=-5)),
    )[0]

    async def scenario(ui):
        return ui.static("#who")

    assert "token expired 5 min ago" in run_ui(backend_of(world), scenario)


def test_without_a_token_the_lake_can_still_be_browsed(world):
    async def scenario(ui):
        return ui.static("#who"), ui.tree()[0], ui.explorer.pbi.tenant

    who, first, tenant = run_ui(backend_of(world, client_for=no_token), scenario)

    assert "not signed in" in who and "no profile" in who
    assert first == "● Workspaces  12"
    assert tenant == TENANT  # the only tenant in the lake


def test_an_unreadable_token_store_does_not_end_the_app(world):
    def broken(scope):
        raise OSError("the keyring is locked")

    async def scenario(ui):
        return ui.app.identity.problem, ui.tree()[0]

    problem, first = run_ui(backend_of(world, client_for=broken), scenario)

    assert "keyring is locked" in problem and first == "● Workspaces  12"


def test_the_header_follows_a_new_token(world):
    async def scenario(ui):
        before = ui.static("#who")
        world.admin = make_client(
            world.fake,
            clock=world.clock,
            store=world.store,
            token=make_token(tenant=TENANT, expires_in=timedelta(minutes=7)),
        )[0]
        ui.app.refresh_identity()
        await ui.until(lambda: "token 7 min" in ui.static("#who"))
        return before, ui.static("#who")

    before, after = run_ui(backend_of(world), scenario)

    assert "token 3650 d" in before and "token 7 min" in after


# ---------------------------------------------------------------------------
# signing in
# ---------------------------------------------------------------------------


def test_signing_in_stores_the_token_for_the_profile_of_the_group(world):
    signed = []

    async def scenario(ui):
        await ui.press("a")
        modal = type(ui.app.screen).__name__
        profile = ui.app.screen.query_one("#profile", Input).value
        await ui.type("abc.def.ghi")
        await ui.press("enter")
        return (
            modal,
            profile,
            type(ui.app.screen).__name__,
            [n.message for n in ui.app._notifications],
        )

    modal, profile, after, notes = run_ui(
        backend_of(world, sign_in=lambda *args: signed.append(args)), scenario
    )

    assert (modal, profile, after) == ("SignInModal", "admin-nlm", "ExplorerScreen")
    assert signed == [("abc.def.ghi", "admin-nlm", "admin")]
    assert "Signed in (admin)." in notes


def test_a_pasted_token_is_cleaned_up(world):
    signed = []

    async def scenario(ui):
        await ui.press("a")
        ui.app.screen.query_one("#token", Input).value = "Bearer  abc.def\n .ghi \n"
        await ui.press("enter")

    run_ui(backend_of(world, sign_in=lambda *args: signed.append(args)), scenario)

    assert signed == [("abc.def.ghi", "admin-nlm", "admin")]


def test_the_kind_of_token_and_the_profile_can_be_chosen(world):
    signed = []

    async def scenario(ui):
        await ui.press("a")
        radios = ui.app.screen.query_one("#group", RadioSet)
        radios.query("RadioButton").last().value = True
        await ui.settle()
        profile = ui.app.screen.query_one("#profile", Input).value
        ui.app.screen.query_one("#profile", Input).value = "mine"
        ui.app.screen.query_one("#token", Input).value = "t.o.k"
        await ui.press("enter")
        return profile

    profile = run_ui(
        backend_of(world, sign_in=lambda *args: signed.append(args)), scenario
    )

    assert profile == "user-nlm"
    assert signed == [("t.o.k", "mine", "user")]


def test_an_empty_token_or_profile_is_not_stored(world):
    signed = []

    async def scenario(ui):
        await ui.press("a")
        await ui.press("enter")
        first = ui.app.screen.query_one("#error").content
        ui.app.screen.query_one("#token", Input).value = "t.o.k"
        ui.app.screen.query_one("#profile", Input).value = " "
        await ui.press("enter")
        return (
            first,
            ui.app.screen.query_one("#error").content,
            type(ui.app.screen).__name__,
        )

    first, second, screen = run_ui(
        backend_of(world, sign_in=lambda *args: signed.append(args)), scenario
    )

    assert first == "Paste the token first." and second == "Give the profile a name."
    assert screen == "SignInModal" and signed == []


def test_a_token_that_cannot_be_stored_is_reported_without_the_token(world):
    def refuse(token, profile, group):
        raise OSError("keyring locked")

    async def scenario(ui):
        await ui.press("a")
        ui.app.screen.query_one("#token", Input).value = "secret.token.value"
        await ui.press("enter")
        return (
            str(ui.app.screen.query_one("#error").content),
            type(ui.app.screen).__name__,
        )

    error, screen = run_ui(backend_of(world, sign_in=refuse), scenario)

    assert "Could not store the token: keyring locked" in error
    assert "secret.token.value" not in error and screen == "SignInModal"


def test_signing_in_can_be_cancelled(world):
    signed = []

    async def scenario(ui):
        await ui.press("a")
        await ui.click("#cancel")
        return type(ui.app.screen).__name__

    screen = run_ui(
        backend_of(world, sign_in=lambda *args: signed.append(args)), scenario
    )

    assert screen == "ExplorerScreen" and signed == []


def test_every_dialog_is_made_from_the_dialog_class():
    """The styles of the dialogs belong to ``Dialog``: one that is made another way comes up
    in a corner of the screen, which no test of what a dialog does would notice."""
    found = [
        item
        for item in vars(modals).values()
        if isinstance(item, type)
        and issubclass(item, ModalScreen)
        and item.__module__ == modals.__name__
        and item is not modals.Dialog
    ]

    assert len(found) >= 4
    assert [
        item.__name__ for item in found if not issubclass(item, modals.Dialog)
    ] == []


@pytest.mark.parametrize("key", ["a", "o"])
def test_a_dialog_comes_up_in_the_middle_of_the_screen(world, key):
    async def scenario(ui):
        await ui.press(key)
        screen = ui.app.screen
        return (
            type(screen).__name__,
            screen.styles.align_horizontal,
            screen.styles.align_vertical,
        )

    backend = backend_of(world, open_lake=lambda location: world.store)
    name, horizontal, vertical = run_ui(backend, scenario)

    assert name != "ExplorerScreen"
    assert (horizontal, vertical) == ("center", "middle")


def test_the_token_is_typed_masked(world):
    async def scenario(ui):
        await ui.press("a")
        return ui.app.screen.query_one("#token", Input).password

    assert run_ui(backend_of(world), scenario) is True


def test_the_sync_screen_does_not_open_over_a_dialog(world):
    async def scenario(ui):
        await ui.press("a")
        ui.app.action_open_sync()
        await ui.settle()
        return type(ui.app.screen).__name__, len(ui.app.screen_stack)

    assert run_ui(backend_of(world), scenario) == ("SignInModal", 3)


def test_only_one_sign_in_dialog_opens(world):
    async def scenario(ui):
        await ui.press("a")
        ui.app.action_sign_in()
        await ui.settle()
        return len(ui.app.screen_stack)

    assert run_ui(backend_of(world), scenario) == 3  # default screen, explorer, dialog


# ---------------------------------------------------------------------------
# tenants
# ---------------------------------------------------------------------------


def two_tenants(world):
    """Put a second tenant in the lake."""
    world.store.write_snapshot(
        "tenant-2", "admin.groups", {}, {"value": [{"id": "x1", "name": "Other"}]}
    )


def test_a_lake_with_two_tenants_asks_which_one_when_nobody_is_signed_in(world):
    two_tenants(world)

    async def scenario(ui):
        asked = type(ui.app.screen).__name__
        await ui.press("down")
        await ui.press("enter")
        return asked, ui.app.tenant, ui.tree(), ui.static("#who")

    asked, tenant, tree, who = run_ui(backend_of(world, client_for=no_token), scenario)

    assert asked == "ChoiceModal"
    assert tenant == "tenant-2" and tree == ["● Workspaces  1", "  ● Other"]
    assert "tenant-2" in who


def test_the_tenant_of_the_token_wins_and_t_switches(world):
    two_tenants(world)

    async def scenario(ui):
        first = ui.app.tenant
        await ui.press("t")
        modal = type(ui.app.screen).__name__
        await ui.press("down")
        await ui.press("enter")
        return first, modal, ui.app.tenant, ui.tree()

    first, modal, tenant, tree = run_ui(backend_of(world), scenario)

    assert first == TENANT and modal == "ChoiceModal"
    assert tenant == "tenant-2" and tree == ["● Workspaces  1", "  ● Other"]


def test_a_tenant_can_be_given(world):
    two_tenants(world)

    async def scenario(ui):
        return ui.app.tenant, ui.tree()

    tenant, tree = run_ui(backend_of(world), scenario, tenant="tenant-2")

    assert tenant == "tenant-2" and tree == ["● Workspaces  1", "  ● Other"]


def test_one_tenant_is_not_asked_about(world):
    async def scenario(ui):
        await ui.press("t")
        return type(ui.app.screen).__name__, [n.message for n in ui.app._notifications]

    screen, notes = run_ui(backend_of(world), scenario)

    assert screen == "ExplorerScreen" and "The lake holds only one tenant." in notes


def test_the_choice_of_a_tenant_can_be_cancelled(world):
    two_tenants(world)

    async def scenario(ui):
        await ui.press("t")
        await ui.press("escape")
        return type(ui.app.screen).__name__, ui.app.tenant

    assert run_ui(backend_of(world), scenario) == ("ExplorerScreen", TENANT)


def test_signing_in_later_finds_the_tenant(tmp_path):
    world = World(tmp_path)
    world.run("groups")
    two_tenants(world)
    state = {"signed": False}

    def client_for(scope):
        if not state["signed"]:
            raise AuthError("No active profile set for group 'admin'.")
        return world.admin

    def sign_in(token, profile, group):
        state["signed"] = True

    async def scenario(ui):
        await ui.press("escape")  # the question about the tenant
        before = ui.app.tenant
        await ui.press("a")
        await ui.type("t.o.k")
        await ui.press("enter")
        await ui.settle()
        return before, ui.app.tenant, ui.tree()[0]

    before, tenant, first = run_ui(
        backend_of(world, client_for=client_for, sign_in=sign_in), scenario
    )

    assert before is None and tenant == TENANT and first == "● Workspaces  12"


# ---------------------------------------------------------------------------
# the command palette
# ---------------------------------------------------------------------------


def test_the_palette_finds_workspaces_and_items_by_name(world):
    async def scenario(ui):
        provider = GotoProvider(ui.app.screen)
        found = [hit async for hit in provider.search("report 3")]
        await found[0].command() if False else None
        found[0].command()
        await ui.settle()
        return [(plain(h.text), h.help) for h in found], ui.static("#info")

    hits, info = run_ui(backend_of(world), scenario)

    assert hits == [("Report: Report 3  in Workspace 3", "rep-0003")]
    assert "Report 3" in info and "rep-0003" in info


def test_the_palette_has_nothing_without_a_lake(tmp_path):
    world = World(tmp_path)

    async def scenario(ui):
        provider = GotoProvider(ui.app.screen)
        return [hit async for hit in provider.search("report")]

    assert run_ui(backend_of(world), scenario) == []


def test_the_palette_offers_the_screens_and_the_sign_in(world):
    async def scenario(ui):
        return [c.title for c in commands_for(ui.app, ui.app.screen)]

    titles = run_ui(backend_of(world), scenario)

    assert {
        "Open the Sync screen",
        "Sign in…",
        "Reload the lake",
        "Choose the tenant…",
    } <= set(titles)


# ---------------------------------------------------------------------------
# a worker that hands something over as the app closes
# ---------------------------------------------------------------------------


def test_what_a_worker_hands_over_is_used_while_the_app_runs(world):
    async def scenario(ui):
        calls = []
        await asyncio.to_thread(ui.app.call_from_thread, lambda: calls.append("ran"))
        return calls

    assert run_ui(backend_of(world), scenario) == ["ran"]


def test_what_a_worker_hands_over_is_dropped_once_the_app_has_been_asked_to_exit(world):
    async def scenario(ui):
        calls = []
        ui.app._exit = True
        try:
            await asyncio.to_thread(
                ui.app.call_from_thread, lambda: calls.append("ran")
            )
        finally:
            ui.app._exit = False
        return calls

    assert run_ui(backend_of(world), scenario) == []


def test_what_a_worker_hands_over_is_dropped_while_the_app_is_shutting_down(world):
    async def scenario(ui):
        calls = []
        ui.app._running = False
        try:
            await asyncio.to_thread(
                ui.app.call_from_thread, lambda: calls.append("ran")
            )
        finally:
            ui.app._running = True
        return calls

    assert run_ui(backend_of(world), scenario) == []


# ---------------------------------------------------------------------------
# the rest
# ---------------------------------------------------------------------------


def test_the_lake_in_the_home_folder_is_shortened_to_a_tilde():
    assert lake_label(Path.home() / "pbi" / "lake") == "~/pbi/lake"
    assert lake_label("/data/lake") == "/data/lake"
    assert lake_label("/a/" + "very-long-" * 10 + "lake", 20).startswith("…")
    assert len(lake_label("/a/" + "very-long-" * 10 + "lake", 20)) == 20


def test_an_unreadable_lake_is_reported_and_the_app_goes_on(world, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr("pbi_cli.tui.app.Catalog", broken)

    async def scenario(ui):
        return [n.message for n in ui.app._notifications], ui.tree()

    notes, tree = run_ui(backend_of(world), scenario)

    assert any("Cannot read the lake: disk gone" in n for n in notes)
    assert tree == ["reading the lake ..."]


def test_the_app_cleans_up_its_log_sink(world):
    from loguru import logger

    before = len(logger._core.handlers)

    async def scenario(ui):
        return len(logger._core.handlers)

    during = run_ui(backend_of(world), scenario)

    assert during == before + 1 and len(logger._core.handlers) == before


# ---------------------------------------------------------------------------
# quitting
# ---------------------------------------------------------------------------


def test_q_quits_at_once_when_nothing_runs(world):
    async def scenario(ui):
        await ui.press("q")
        return ui.app._exit

    assert run_ui(backend_of(world), scenario) is True


def test_quitting_while_a_sync_runs_asks_and_stops_it(world, monkeypatch):
    import threading

    from pbi_cli.core.sync import runners
    from pbi_cli.core.sync.plan import SNAPSHOT

    entered, release = threading.Event(), threading.Event()
    real = runners.RUNNERS[SNAPSHOT]

    def slow(ctx, unit):
        entered.set()
        release.wait(10)
        return real(ctx, unit)

    monkeypatch.setitem(runners.RUNNERS, SNAPSHOT, slow)

    async def scenario(ui):
        from pbi_cli.core.sync.plan import SyncOptions

        ui.app.start_sync(SyncOptions(targets=("groups",), workers=1), "Sync")
        await ui.until(entered.is_set)
        await ui.press("q")
        asked = type(ui.app.screen).__name__, ui.app._exit
        await ui.press("escape")
        stayed = (
            type(ui.app.screen).__name__,
            ui.app._exit,
            ui.app.run_state.stop.is_set(),
        )
        await ui.press("q")
        await ui.press("enter")
        release.set()
        return asked, stayed, ui.app.run_state.stop.is_set(), ui.app._exit

    asked, stayed, stopped, exited = run_ui(backend_of(world), scenario)

    assert asked == ("ConfirmModal", False)
    assert stayed == ("ExplorerScreen", False, False)
    assert stopped is True and exited is True
