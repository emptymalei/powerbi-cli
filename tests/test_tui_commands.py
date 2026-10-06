"""The command palette: ``:`` or ``Ctrl+P`` search every action of the UI and run one."""

import threading
from datetime import timedelta

import pytest
from sync_helpers import World
from textual.command import CommandInput, CommandList, CommandPalette
from textual.widgets import Input, SelectionList, TabbedContent
from tui_helpers import backend_of, run_ui

from pbi_cli.core.registry import Scope
from pbi_cli.core.store import LakeStore
from pbi_cli.core.sync import runners
from pbi_cli.core.sync.plan import SNAPSHOT
from pbi_cli.errors import AuthError
from pbi_cli.tui.backend import AccountInfo
from pbi_cli.tui.commands import commands_for
from pbi_cli.tui.modals import WORK_LAKE, AccountsModal
from pbi_cli.tui.palette import CommandsProvider, looks_like_a_location


@pytest.fixture
def world(tmp_path):
    world = World(tmp_path)
    world.run()
    return world


def titles(ui):
    """What the palette would list over the screen that is shown."""
    return [c.title for c in commands_for(ui.app, ui.app.screen)]


def command(ui, title):
    return next(c for c in commands_for(ui.app, ui.app.screen) if c.title == title)


async def search(ui, query):
    provider = CommandsProvider(ui.app.screen)
    return [hit async for hit in provider.search(query)]


async def discover(ui):
    provider = CommandsProvider(ui.app.screen)
    return [hit async for hit in provider.discover()]


def view_only(world, **replace):
    """A backend on a lake that is only looked at."""
    root = world.store.root
    settings = dict(
        store=LakeStore(root, readonly=True, reason="The lake only reads."),
        work_lake=str(root),
    )
    settings.update(replace)
    return backend_of(world, **settings)


def accounts_of(world):
    soon = world.clock.now() + timedelta(minutes=40)
    return [
        AccountInfo("admin", "adm", True, "adm@x.com", "tenant-1", soon),
        AccountInfo("user", "svc", True, "svc@x.com", "tenant-1", soon),
        AccountInfo("user", "ana", False, "ana@x.com", "tenant-1", soon),
    ]


async def open_palette(ui, key=":"):
    await ui.press(key)
    palette = ui.app.screen
    assert isinstance(palette, CommandPalette)
    return palette


def listed(palette):
    """The titles that the palette shows now."""
    options = palette.query_one(CommandList)
    return [
        options.get_option_at_index(n).hit.text for n in range(options.option_count)
    ]


# ---------------------------------------------------------------------------
# what is offered, and when
# ---------------------------------------------------------------------------


def test_the_explorer_offers_what_can_be_done_with_the_lake(world):
    async def scenario(ui):
        return titles(ui)

    found = run_ui(backend_of(world), scenario)

    assert {
        "Fetch the lists of the tenant",
        "Filter the tree or the table",
        "Show the Info tab",
        "Show the Users tab",
        "Show the Lineage tab",
        "Show the JSON tab",
        "Show the Versions tab",
        "Open the Sync screen",
        "Reload the lake",
        "Accounts…",
        "Sign in…",
        "Choose the tenant…",
    } <= set(found)
    assert not {
        "Open the Explorer",
        "Run the sync",
        "Stop the sync",
        "Clear the filter",
    } & set(found)


def test_a_filter_can_be_cleared_from_the_palette_once_there_is_one(world):
    async def scenario(ui):
        before = titles(ui)
        await ui.press("/")
        await ui.type("work")
        during = titles(ui)
        command(ui, "Clear the filter").run()
        await ui.settle()
        return before, during, titles(ui)

    before, during, after = run_ui(backend_of(world), scenario)

    assert "Clear the filter" not in before
    assert "Clear the filter" in during
    assert "Clear the filter" not in after


def test_the_tabs_of_the_explorer_are_shown_from_the_palette(world):
    async def scenario(ui):
        command(ui, "Show the Users tab").run()
        await ui.settle()
        return ui.explorer.query_one("#detail", TabbedContent).active

    assert run_ui(backend_of(world), scenario) == "tab-users"


def test_the_sync_screen_offers_to_run_and_the_tabs_but_not_what_it_is_already_showing(
    world,
):
    async def scenario(ui):
        await ui.press("s")
        await ui.until(lambda: ui.sync.plans > 0)
        return titles(ui)

    found = run_ui(backend_of(world), scenario)

    assert {
        "Run the sync",
        "Show the Plan tab",
        "Show the Run tab",
        "Show the Lake tab",
    } <= set(found)
    assert "Open the Explorer" in found and "Open the Sync screen" not in found
    assert not {"Stop the sync", "Filter the tree or the table"} & set(found)


def test_the_tabs_of_the_sync_screen_are_shown_from_the_palette(world):
    async def scenario(ui):
        await ui.press("s")
        await ui.until(lambda: ui.sync.plans > 0)
        shown = []
        for name in ("Lake", "Run", "Plan"):
            command(ui, f"Show the {name} tab").run()
            await ui.settle()
            shown.append(ui.sync.query_one("#sync-tabs", TabbedContent).active)
        return shown

    assert run_ui(backend_of(world), scenario) == ["tab-lake", "tab-run", "tab-plan"]


def test_a_sync_screen_over_a_lake_that_is_only_looked_at_offers_no_run(world):
    async def scenario(ui):
        await ui.press("s")
        await ui.settle()
        return titles(ui)

    found = run_ui(view_only(world), scenario)

    assert "Run the sync" not in found and "Show the Lake tab" in found


def test_the_run_is_not_offered_when_nothing_is_planned(world):
    async def scenario(ui):
        await ui.press("s")
        await ui.until(lambda: ui.sync.plans > 0)
        planned = "Run the sync" in titles(ui)
        shown = ui.sync.plans
        ui.sync.query_one("#targets", SelectionList).deselect_all()  # nothing to plan
        await ui.until(lambda: ui.sync.plans > shown)
        await ui.settle()
        return planned, "Run the sync" in titles(ui)

    assert run_ui(backend_of(world), scenario) == (True, False)


def test_the_run_is_not_offered_once_the_lake_has_become_one_that_is_only_looked_at(
    world,
):
    async def scenario(ui):
        await ui.press("s")
        await ui.until(lambda: ui.sync.plans > 0)
        before = "Run the sync" in titles(ui)
        # another lake is opened while the screen shows the plan of the work lake
        ui.app.backend.store = LakeStore(
            world.store.root, readonly=True, reason="The lake only reads."
        )
        return before, "Run the sync" in titles(ui)

    assert run_ui(backend_of(world), scenario) == (True, False)


def test_a_filter_of_the_table_can_be_cleared_from_the_palette_too(world):
    async def scenario(ui):
        ui.explorer.query_one("#table").focus()
        await ui.press("/")
        await ui.type("report")
        return "Clear the filter" in titles(ui)

    assert run_ui(backend_of(world), scenario) is True


def test_a_sync_that_runs_can_be_stopped_from_anywhere(world, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    real = runners.RUNNERS[SNAPSHOT]

    def slow(ctx, unit):
        entered.set()
        release.wait(10)
        return real(ctx, unit)

    monkeypatch.setitem(runners.RUNNERS, SNAPSHOT, slow)
    world.clock.advance(hours=30)  # the lists are old: there is something to fetch

    async def scenario(ui):
        await ui.press("s")
        await ui.until(lambda: ui.sync.plans > 0)
        before = titles(ui)
        await ui.click("#run")
        await ui.until(entered.is_set)
        while_shown = titles(ui)  # the Sync screen, with the sync running
        await ui.press("escape")  # back to the Explorer: the sync goes on
        running = titles(ui)
        command(ui, "Stop the sync").run()
        await ui.settle()
        stopping = ui.app.run_state.stopping
        asked_to_stop = titles(ui)
        release.set()
        await ui.finish_sync()
        return before, while_shown, running, stopping, asked_to_stop, titles(ui)

    before, while_shown, running, stopping, asked_to_stop, after = run_ui(
        backend_of(world), scenario
    )

    assert "Run the sync" in before and "Stop the sync" not in before
    assert "Run the sync" not in while_shown and "Stop the sync" in while_shown
    assert "Stop the sync" in running
    assert stopping is True and "Stop the sync" not in asked_to_stop
    assert "Stop the sync" not in after


def test_a_lake_that_is_only_looked_at_offers_nothing_that_needs_an_account(world):
    async def scenario(ui):
        return titles(ui)

    found = run_ui(view_only(world), scenario)

    assert not {"Accounts…", "Sign in…", "Fetch the lists of the tenant"} & set(found)
    assert not [t for t in found if t.startswith(("Make ", "Store a new token"))]
    assert "Filter the tree or the table" in found and "Reload the lake" in found


def test_the_work_lake_is_offered_while_another_lake_is_looked_at(world):
    opened = []

    async def scenario(ui):
        before = titles(ui)
        command(ui, "Open the work lake").run()
        await ui.settle()
        return before, opened

    before, opened = run_ui(
        view_only(world, open_lake=lambda place: opened.append(place) or world.store),
        scenario,
    )

    assert "Open the work lake" in before and opened == [str(world.store.root)]


def test_the_work_lake_is_not_offered_when_it_is_the_one_that_is_open(world):
    async def scenario(ui):
        return titles(ui)

    found = run_ui(
        backend_of(
            world, open_lake=lambda p: world.store, work_lake=str(world.store.root)
        ),
        scenario,
    )

    assert "Open a lake…" in found and "Open the work lake" not in found


def test_the_lakes_opened_lately_are_offered_except_the_ones_that_are_open(world):
    here = str(world.store.root)
    backend = backend_of(
        world,
        open_lake=lambda place: world.store,
        work_lake=here,
        recent_lakes=lambda: ["s3://bucket/lake", here, "/shared/lake"],
    )

    async def scenario(ui):
        return titles(ui)

    found = run_ui(backend, scenario)

    assert "Open s3://bucket/lake" in found and "Open /shared/lake" in found
    assert f"Open {here}" not in found


def test_only_the_latest_of_the_lakes_opened_lately_are_offered(world):
    places = [f"s3://bucket/lake-{n}" for n in range(9)]
    backend = backend_of(
        world,
        open_lake=lambda place: world.store,
        work_lake=str(world.store.root),
        recent_lakes=lambda: places,
    )

    async def scenario(ui):
        return [t for t in titles(ui) if t.startswith("Open s3://")]

    found = run_ui(backend, scenario)

    assert found == [f"Open {place}" for place in places[:5]]


def test_no_lake_is_offered_when_the_session_cannot_open_one(world):
    async def scenario(ui):
        return titles(ui)

    found = run_ui(backend_of(world), scenario)

    assert not [t for t in found if t.startswith("Open ") and "lake" in t.lower()]


def test_the_stored_profiles_are_offered_by_name(world):
    activated = []
    backend = backend_of(
        world,
        accounts=lambda: accounts_of(world),
        activate=lambda group, profile: activated.append((group, profile)),
    )

    async def scenario(ui):
        found = titles(ui)
        command(ui, "Make ana the active user account").run()
        await ui.settle()
        return found

    found = run_ui(backend, scenario)

    assert "Make ana the active user account" in found  # not active: can be made so
    assert "Make svc the active user account" not in found  # it is the active one
    assert "Make adm the active admin account" not in found
    assert {
        "Store a new token for adm",
        "Store a new token for svc",
        "Store a new token for ana",
    } <= set(found)
    assert activated == [("user", "ana")]


def test_a_profile_cannot_be_made_active_by_a_session_that_cannot_switch(world):
    backend = backend_of(world, accounts=lambda: accounts_of(world))  # no activate

    async def scenario(ui):
        return titles(ui)

    found = run_ui(backend, scenario)

    assert not [t for t in found if t.startswith("Make ")]
    assert "Store a new token for ana" in found


def test_a_new_token_for_a_profile_asks_for_that_profile(world):
    backend = backend_of(world, accounts=lambda: accounts_of(world))

    async def scenario(ui):
        command(ui, "Store a new token for ana").run()
        await ui.settle()
        modal = ui.app.screen
        return (
            type(modal).__name__,
            modal._selected_group(),
            modal.query_one("#profile", Input).value,
        )

    assert run_ui(backend, scenario) == ("SignInModal", "user", "ana")


def test_someone_with_only_a_user_account_is_offered_what_that_account_can_fetch(world):
    world.only_user()

    async def scenario(ui):
        return titles(ui)

    found = run_ui(backend_of(world, client_for=world.engine._client_for), scenario)

    assert "Fetch what you can see" in found
    assert "Fetch the lists of the tenant" not in found


# ---------------------------------------------------------------------------
# searching
# ---------------------------------------------------------------------------


def test_typing_finds_a_command_by_its_words_and_by_other_words_for_it(world):
    async def scenario(ui):
        by_title = [hit.text for hit in await search(ui, "acc")]
        by_keyword = [hit.text for hit in await search(ui, "login")]
        nothing = [hit.text for hit in await search(ui, "qzxqzx")]
        return by_title, by_keyword, nothing

    by_title, by_keyword, nothing = run_ui(backend_of(world), scenario)

    assert "Accounts…" in by_title
    assert "Sign in…" in by_keyword
    assert nothing == []


def test_the_best_match_comes_with_its_score(world):
    async def scenario(ui):
        hits = await search(ui, "reload")
        return sorted(hits, key=lambda h: h.score, reverse=True)[0]

    best = run_ui(backend_of(world), scenario)

    assert best.text == "Reload the lake" and best.score > 0
    assert "Read the lake again" in best.help and "key l" in best.help


def test_everything_is_listed_before_anything_is_typed_and_ours_comes_first(world):
    async def scenario(ui):
        return [hit.text for hit in await discover(ui)]

    found = run_ui(backend_of(world), scenario)

    assert found.index("Show the Info tab") < found.index("Quit")
    assert {"Reload the lake", "Quit", "Keys"} <= set(found)  # and Textual's own


def test_a_typed_place_can_be_opened(world):
    opened = []
    backend = backend_of(
        world, open_lake=lambda place: opened.append(place) or world.store
    )

    async def scenario(ui):
        hits = await search(ui, "  s3://contoso/lake ")  # pasted, with spaces around it
        place = next(h for h in hits if h.text.startswith("Open lake "))
        place.command()
        await ui.settle()
        return place.text

    text = run_ui(backend, scenario)

    assert text == "Open lake s3://contoso/lake" and opened == ["s3://contoso/lake"]


def test_a_place_is_not_offered_when_the_session_cannot_open_a_lake(world):
    async def scenario(ui):
        return [hit.text for hit in await search(ui, "s3://contoso/lake")]

    assert not [t for t in run_ui(backend_of(world), scenario) if "Open lake" in t]


@pytest.mark.parametrize(
    "text",
    [
        "s3://bucket/lake",
        "az://container/lake",
        "/data/lake",
        "~/PowerBI/cache",
        "./lake",
        "../lake",
        "C:\\Users\\me\\lake",
        "D:/lake",
    ],
)
def test_what_looks_like_a_place(text):
    assert looks_like_a_location(text) and looks_like_a_location(f"  {text}  ")


@pytest.mark.parametrize("text", ["accounts", "open lake", "run", "a:b", "1"])
def test_what_does_not_look_like_a_place(text):
    assert not looks_like_a_location(text)


def test_a_bracket_in_a_name_is_not_markup(world):
    backend = backend_of(
        world,
        accounts=lambda: [
            AccountInfo("user", "svc[1]", False, "a[b]@x.com", "t", None)
        ],
        activate=lambda group, profile: None,
    )

    async def scenario(ui):
        hits = await search(ui, "svc")
        return [(h.text, h.help) for h in hits if "svc[1]" in h.text]

    found = run_ui(backend, scenario)

    assert found and all("\\[" in help_text for _, help_text in found)


# ---------------------------------------------------------------------------
# the palette itself
# ---------------------------------------------------------------------------


def test_the_colon_opens_the_palette_and_a_command_that_is_chosen_runs(world):
    async def scenario(ui):
        palette = await open_palette(ui)
        await ui.until(lambda: "Accounts…" in listed(palette))
        await ui.type("accounts")
        await ui.until(lambda: listed(palette)[:1] == ["Accounts…"])
        await ui.press("enter")
        await ui.settle()
        return type(ui.app.screen).__name__

    assert run_ui(backend_of(world, accounts=lambda: []), scenario) == "AccountsModal"


def test_ctrl_p_opens_it_too(world):
    async def scenario(ui):
        palette = await open_palette(ui, "ctrl+p")
        await ui.until(lambda: listed(palette))
        return listed(palette)

    found = run_ui(backend_of(world), scenario)

    assert "Reload the lake" in found and "Open the Sync screen" in found


def test_a_place_typed_in_the_palette_is_offered_and_opened(world):
    opened = []
    backend = backend_of(
        world, open_lake=lambda place: opened.append(place) or world.store
    )

    async def scenario(ui):
        palette = await open_palette(ui)
        await ui.type("s3://x/y")  # the colons and slashes are text in the box
        await ui.until(lambda: "Open lake s3://x/y" in listed(palette))
        await ui.press("enter")
        await ui.settle()
        return type(ui.app.screen).__name__

    assert run_ui(backend, scenario) == "ExplorerScreen"
    assert opened == ["s3://x/y"]


def test_the_palette_lists_what_can_be_done_before_anything_is_typed(world):
    async def scenario(ui):
        palette = await open_palette(ui)
        await ui.until(lambda: len(listed(palette)) > 5)
        return listed(palette), palette.query_one(CommandInput).value

    found, typed = run_ui(backend_of(world), scenario)

    assert typed == "" and "Filter the tree or the table" in found
    assert "Quit" in found


def test_the_colon_is_text_in_a_box_that_is_being_typed_in(world):
    async def scenario(ui):
        await ui.press("/")  # the filter box has the focus
        await ui.press(":")
        return (
            type(ui.app.screen).__name__,
            ui.explorer.query_one("#tree-filter", Input).value,
        )

    assert run_ui(backend_of(world), scenario) == ("ExplorerScreen", ":")


def test_the_hint_in_the_header_opens_the_palette_when_it_is_clicked(world):
    async def scenario(ui):
        before = ui.static("#hint")
        await ui.click("#hint")
        return before, isinstance(ui.app.screen, CommandPalette)

    before, opened = run_ui(backend_of(world), scenario)

    assert ": commands" in before and opened is True


def test_the_colon_is_in_the_footer_as_commands(world):
    async def scenario(ui):
        bindings = ui.app.screen.active_bindings
        return bindings["colon"].binding.description, bindings["ctrl+p"].binding.action

    assert run_ui(backend_of(world), scenario) == ("Commands", "command_palette")


def test_a_command_of_the_palette_opens_the_sync_screen(world):
    async def scenario(ui):
        command(ui, "Open the Sync screen").run()
        await ui.settle()
        return type(ui.app.screen).__name__

    assert run_ui(backend_of(world), scenario) == "SyncScreen"


def test_a_command_of_the_palette_goes_back_to_the_explorer(world):
    async def scenario(ui):
        await ui.press("s")
        command(ui, "Open the Explorer").run()
        await ui.settle()
        return type(ui.app.screen).__name__

    assert run_ui(backend_of(world), scenario) == "ExplorerScreen"


def test_the_work_lake_command_goes_through_the_guards_of_the_dialog(world):
    """A sync that runs, or a dialog in the way, is not left for a lake to be opened."""
    opened = []
    backend = backend_of(
        world,
        open_lake=lambda place: opened.append(place) or world.store,
        work_lake=str(world.store.root),
    )

    async def scenario(ui):
        ui.app.push_screen(AccountsModal([], world.clock.now()))
        await ui.settle()
        ui.app.open_lake_at("s3://x/y")  # a dialog is open: not now
        await ui.settle()
        return list(opened)

    assert run_ui(backend, scenario) == []


def test_a_profile_that_cannot_be_made_active_says_so(world):
    def broken(group, profile):
        raise AuthError("the settings cannot be written")

    backend = backend_of(world, accounts=lambda: accounts_of(world), activate=broken)

    async def scenario(ui):
        command(ui, "Make ana the active user account").run()
        await ui.settle()
        return [n.message for n in ui.app._notifications]

    notes = run_ui(backend, scenario)

    assert any("Cannot switch: the settings cannot be written" in n for n in notes)


def test_the_scope_of_the_commands_is_what_the_accounts_say(world):
    """Not a claim about Power BI: what is offered follows `available_scopes`."""
    world.only_user()

    async def scenario(ui):
        return ui.app.backend.available_scopes()

    assert run_ui(backend_of(world, client_for=world.engine._client_for), scenario) == {
        Scope.USER
    }


def test_the_names_of_the_work_lake_constant_stands_for_the_work_lake(world):
    """The palette asks for the work lake with the same word as the dialog does."""
    opened = []
    backend = backend_of(
        world,
        open_lake=lambda place: opened.append(place) or world.store,
        work_lake="/work/lake",
    )

    async def scenario(ui):
        ui.app.open_lake_at(WORK_LAKE)
        await ui.settle()

    run_ui(backend, scenario)

    assert opened == ["/work/lake"]
