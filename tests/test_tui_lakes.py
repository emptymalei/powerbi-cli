"""The TUI on a lake that is only looked at, and the dialog that opens another lake."""

from datetime import datetime, timezone

import pytest
from sync_helpers import World
from textual.widgets import Button, OptionList
from tui_helpers import backend_of, run_ui

from pbi_cli.core.scan import ScanFlags
from pbi_cli.core.store import LakeStore, PublishInfo
from pbi_cli.core.sync.plan import SyncOptions
from pbi_cli.errors import PBIError
from pbi_cli.tui.commands import commands_for
from pbi_cli.tui.modals import OpenLakeModal, SignInModal
from pbi_cli.tui.run import RunState

REASON = "The lake was opened with --lake, which only reads."


def read_only(root, reason=REASON) -> LakeStore:
    return LakeStore(root, readonly=True, reason=reason)


def messages(ui):
    return [note.message for note in ui.app._notifications]


class Asked:
    """A `client_for` that remembers it was called, and refuses."""

    def __init__(self):
        self.scopes = []

    def __call__(self, scope):
        self.scopes.append(scope)
        raise AssertionError("an account was asked about")

    def engine(self):
        self.scopes.append("engine")
        raise AssertionError("an engine was asked for")


@pytest.fixture
def viewed(tmp_path):
    """A lake that is only looked at, and the account questions that must not be asked."""
    world = World(tmp_path)
    world.run()
    world.run("scan", scan_flags=ScanFlags(lineage=True))
    asked = Asked()
    backend = backend_of(
        world,
        store=read_only(world.store.root),
        client_for=asked,
        make_engine=asked.engine,
    )
    return world, backend, asked


# ---------------------------------------------------------------------------
# view only
# ---------------------------------------------------------------------------


def test_a_lake_that_is_only_looked_at_is_browsed_and_no_account_is_asked_about(viewed):
    _, backend, asked = viewed

    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        return ui.static("#who"), ui.tree(), ui.rows()

    who, tree, rows = run_ui(backend, scenario)

    assert "view only" in who and "no profile" not in who and "token" not in who
    assert tree[0].startswith("● Workspaces") and len(rows) == 5
    assert asked.scopes == []  # not for the header, not for anything


def test_signing_in_is_refused_with_the_reason(viewed):
    _, backend, _ = viewed

    async def scenario(ui):
        await ui.press("a")
        return type(ui.app.screen).__name__, messages(ui)

    screen, notes = run_ui(backend, scenario)

    assert screen == "ExplorerScreen"
    assert any(REASON in note for note in notes)


def test_fetching_again_is_refused_with_the_reason(viewed):
    _, backend, asked = viewed

    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        await ui.press("r")
        return type(ui.app.screen).__name__, messages(ui)

    screen, notes = run_ui(backend, scenario)

    assert screen == "ExplorerScreen"  # no dialog asking to fetch
    assert any(REASON in note for note in notes)
    assert asked.scopes == []


def test_a_sync_cannot_be_started_on_a_lake_that_is_only_looked_at(viewed):
    _, backend, asked = viewed

    async def scenario(ui):
        started = ui.app.start_sync(SyncOptions(targets=("groups",)), "Sync")
        await ui.settle()  # a notification is a message: it arrives a moment later
        return started, ui.app.run_state, messages(ui)

    started, state, notes = run_ui(backend, scenario)

    assert started is False and state is None
    assert any(REASON in note for note in notes)
    assert asked.scopes == []


def test_the_sync_screen_shows_what_the_lake_holds_but_plans_and_runs_nothing(viewed):
    _, backend, asked = viewed

    async def scenario(ui):
        await ui.press("s")
        await ui.until(lambda: ui.sync.plans >= 1)
        run_button = ui.sync.query_one("#run", Button)
        await ui.press("r")
        return (
            ui.static("#plan-head", ui.sync),
            run_button.disabled,
            ui.app.run_state,
            [row[0] for row in ui.rows("#holdings", ui.sync)],
            ui.rows("#used", ui.sync),
        )

    head, disabled, state, holdings, used = run_ui(backend, scenario)

    assert "View only" in head and REASON in head
    assert disabled is True and state is None
    assert "groups" in holdings and "reports" in holdings
    assert used == []  # the quota is the account's: none is asked about
    assert asked.scopes == []


def test_a_published_lake_says_who_published_it_and_when(tmp_path):
    world = World(tmp_path)
    world.run("groups")
    LakeStore(world.store.root, publishing=True).write_marker(
        PublishInfo(
            published_at=datetime(2026, 10, 2, 7, 20, tzinfo=timezone.utc),
            published_by="ana@laptop",
            tenants=["tenant-1"],
        )
    )
    asked = Asked()
    backend = backend_of(
        world,
        store=LakeStore(
            world.store.root
        ),  # not opened read-only: the marker protects it
        client_for=asked,
        make_engine=asked.engine,
    )

    async def scenario(ui):
        return ui.static("#who"), messages(ui)

    who, _ = run_ui(backend, scenario)

    assert "view only" in who
    assert "published 2026-10-02 07:20 by ana@laptop" in who
    assert (
        backend.readonly.startswith("The lake ") and "published by" in backend.readonly
    )
    assert asked.scopes == []


def test_the_work_lake_is_not_view_only(tmp_path):
    world = World(tmp_path)
    world.run("groups")

    async def scenario(ui):
        return ui.static("#who")

    who = run_ui(backend_of(world), scenario)

    assert "view only" not in who and "admin-nlm (admin)" in who and "token" in who


# ---------------------------------------------------------------------------
# open a lake
# ---------------------------------------------------------------------------


@pytest.fixture
def two_lakes(tmp_path):
    """The work lake (12 workspaces), and a lake somebody shared (3)."""
    mine = World(tmp_path / "mine")
    mine.run("groups")
    theirs = World(tmp_path / "theirs", workspaces=3)
    theirs.run("groups")
    shared = theirs.store.root
    opened = []

    def open_lake(location):
        opened.append(location)
        if location == str(shared):
            return read_only(shared)
        if location == str(mine.store.root):
            return LakeStore(mine.store.root)
        raise PBIError(f"There is no data lake at {location}.")

    backend = backend_of(
        mine,
        open_lake=open_lake,
        recent_lakes=lambda: [str(shared)],
        work_lake=str(mine.store.root),
    )
    return backend, shared, opened


def test_the_dialog_offers_the_work_lake_and_the_recent_ones(two_lakes):
    backend, shared, _ = two_lakes

    async def scenario(ui):
        await ui.press("o")
        screen = ui.app.screen
        choices = screen.query_one("#choices", OptionList)
        return type(screen).__name__, [
            str(choices.get_option_at_index(i).prompt)
            for i in range(choices.option_count)
        ]

    name, options = run_ui(backend, scenario)

    assert name == "OpenLakeModal"
    assert options[0].startswith("Work lake") and "mine" in options[0]
    assert options[1].startswith("Recent") and str(shared) in options[1]


def test_a_typed_location_opens_that_lake_read_only_and_the_tree_follows(two_lakes):
    backend, shared, opened = two_lakes

    async def scenario(ui):
        before = ui.tree()[0]
        await ui.press("o")
        await ui.type(str(shared))
        await ui.press("enter")
        await ui.until(lambda: ui.tree() and ui.tree()[0] != before)
        await ui.settle()
        return before, ui.tree()[0], ui.static("#who"), messages(ui)

    before, after, who, notes = run_ui(backend, scenario)

    assert before == "● Workspaces  12" and after == "● Workspaces  3"
    assert opened == [str(shared)]
    assert "view only" in who and "theirs" in who
    assert any("Opened" in note and "view only" in note for note in notes)
    assert backend.store.root == shared


def test_a_recent_lake_can_be_chosen_and_the_work_lake_chosen_again(two_lakes):
    backend, shared, opened = two_lakes

    async def choose(ui, index):
        await ui.press("o")
        choices = ui.app.screen.query_one("#choices", OptionList)
        choices.focus()
        await ui.pilot.pause()
        choices.highlighted = index
        choices.action_select()
        await ui.settle()

    async def scenario(ui):
        await choose(ui, 1)  # the recent one
        await ui.until(lambda: ui.tree() and ui.tree()[0] == "● Workspaces  3")
        view_only = "view only" in ui.static("#who")
        await choose(ui, 0)  # the work lake
        await ui.until(lambda: ui.tree() and ui.tree()[0] == "● Workspaces  12")
        return view_only, ui.static("#who")

    view_only, who = run_ui(backend, scenario)

    assert view_only is True
    assert "view only" not in who and "admin-nlm (admin)" in who
    assert opened == [str(shared), str(backend.work_lake)]
    assert backend.readonly == ""


def test_a_lake_that_cannot_be_opened_is_said_and_the_open_one_stays(two_lakes):
    backend, _, _ = two_lakes
    mine = backend.store

    async def scenario(ui):
        await ui.press("o")
        await ui.type("/nowhere")
        await ui.press("enter")
        await ui.until(lambda: any("Cannot open" in m for m in messages(ui)))
        return ui.tree()[0], messages(ui)

    first, notes = run_ui(backend, scenario)

    assert first == "● Workspaces  12"
    assert any(
        "Cannot open /nowhere: There is no data lake at /nowhere." in n for n in notes
    )
    assert backend.store is mine


def test_another_lake_is_not_opened_while_a_sync_runs(two_lakes):
    backend, _, opened = two_lakes

    async def scenario(ui):
        ui.app.run_state = RunState("Sync", SyncOptions(), backend.clock())
        await ui.press("o")
        return type(ui.app.screen).__name__, messages(ui)

    screen, notes = run_ui(backend, scenario)

    assert screen == "ExplorerScreen" and opened == []
    assert any("A sync is running" in note for note in notes)


def test_a_session_that_cannot_open_lakes_says_so(tmp_path):
    world = World(tmp_path)
    world.run("groups")

    async def scenario(ui):
        await ui.press("o")
        return type(ui.app.screen).__name__, messages(ui)

    screen, notes = run_ui(backend_of(world), scenario)

    assert screen == "ExplorerScreen"
    assert any("cannot open another lake" in note for note in notes)


def test_the_sign_in_dialog_still_opens_on_a_lake_that_is_written(tmp_path):
    world = World(tmp_path)
    world.run("groups")

    async def scenario(ui):
        await ui.press("a")
        return isinstance(ui.app.screen, SignInModal)

    assert run_ui(backend_of(world), scenario) is True


def test_the_dialog_is_reachable_from_the_palette(two_lakes):
    backend, _, _ = two_lakes

    async def scenario(ui):
        commands = {c.title for c in commands_for(ui.app, ui.app.screen)}
        return "Open a lake…" in commands

    assert run_ui(backend, scenario) is True


def test_the_dialog_gives_up_on_escape(two_lakes):
    backend, _, opened = two_lakes

    async def scenario(ui):
        await ui.press("o")
        assert isinstance(ui.app.screen, OpenLakeModal)
        await ui.press("escape")
        return type(ui.app.screen).__name__

    assert run_ui(backend, scenario) == "ExplorerScreen" and opened == []
