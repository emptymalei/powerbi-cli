"""The Details tab, and ``f`` that fetches what an item lacks, for that item and no other."""

import pytest
from sync_helpers import World
from textual.widgets import TabbedContent
from tui_helpers import backend_of, plain, run_ui

from pbi_cli.core.store import LakeStore
from pbi_cli.tui.commands import commands_for
from pbi_cli.tui.modals import ConfirmModal


@pytest.fixture
def world(tmp_path):
    world = World(tmp_path)
    world.run()
    return world


@pytest.fixture
def user_world(world):
    world.only_user()
    world.run()  # what a user can see: the lists of the workspaces
    return world


def user_backend(world, **replace):
    return backend_of(world, client_for=world.engine._client_for, **replace)


async def pick(ui, kind, item_id, tab=None):
    await ui.select("workspace", "ws-0001")
    await ui.pick_row(f"{kind}:{item_id}")
    if tab:
        await ui.press(tab)


def details(ui):
    """The rows and the line above them, of the Details tab."""
    return ui.rows("#details"), ui.static("#details-note")


def asked(ui):
    """The title and the plan of the dialog that asks whether to fetch."""
    screen = ui.app.screen
    assert isinstance(screen, ConfirmModal)
    return plain(screen.query_one("#dialog-body Static").content)


def calls(world, pattern):
    return [c.path for c in world.fake.calls_to(pattern)]


# ---------------------------------------------------------------------------
# the tab
# ---------------------------------------------------------------------------


def test_the_details_tab_lists_what_an_item_can_have_and_what_the_lake_lacks(world):
    async def scenario(ui):
        await pick(ui, "dataset", "ds-0001", "6")
        return details(ui)

    rows, note = run_ui(backend_of(world), scenario)

    assert [r[:2] for r in rows] == [
        ["Users", "missing"],
        ["Data sources", "missing"],
        ["Refreshes", "missing"],
        ["Parameters", "missing"],
    ]
    assert rows[0][3] == "press f: 1 request, an administrator account"
    assert rows[3][3] == "press f: 1 request, a user account"  # only a user reads those
    assert "The lake holds 0 of 4 details of this." in note
    assert "Press f to fetch the users, data sources, refreshes and parameters" in note


def test_the_details_tab_is_one_of_the_tabs_and_has_a_key(world):
    async def scenario(ui):
        await pick(ui, "dataset", "ds-0001", "6")
        return ui.explorer.query_one("#detail", TabbedContent).active

    assert run_ui(backend_of(world), scenario) == "tab-details"


def test_a_workspace_has_the_users_detail(world):
    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        await ui.press("6")
        return details(ui)

    rows, note = run_ui(backend_of(world), scenario)

    assert [r[:2] for r in rows] == [["Users", "missing"]]
    assert rows[0][3] == "press f: 1 request, an administrator account"


@pytest.mark.parametrize(
    "kind, item, names",
    [
        ("report", "rep-0001", ["Users", "Pages"]),
        ("dashboard", "dash-0001", ["Users", "Tiles"]),
        ("dataflow", "flow-0001", ["Users", "Data sources"]),
    ],
)
def test_every_kind_of_item_has_its_own_details(world, kind, item, names):
    async def scenario(ui):
        await pick(ui, kind, item, "6")
        return [r[0] for r in ui.rows("#details")]

    assert run_ui(backend_of(world), scenario) == names


def test_what_has_no_details_says_so(world):
    async def scenario(ui):
        await pick(ui, "app", "app-0001", "6")
        return ui.static("#details-note")

    assert "nothing more to fetch for this app" in run_ui(backend_of(world), scenario)


def test_what_is_not_a_workspace_or_an_item_says_so(world):
    async def scenario(ui):
        await ui.select("capacities")
        await ui.press("6")
        return ui.static("#details-note")

    assert "one at a time" in run_ui(backend_of(world), scenario)


# ---------------------------------------------------------------------------
# f
# ---------------------------------------------------------------------------


def test_f_fetches_what_an_item_lacks_for_that_item_and_no_other(world):
    world.fake.reset_calls()

    async def scenario(ui):
        await pick(ui, "dataset", "ds-0001", "6")
        await ui.press("f")
        text = asked(ui)
        await ui.press("enter")
        await ui.finish_sync()
        await ui.settle()
        return text, details(ui)

    text, (rows, note) = run_ui(backend_of(world), scenario)

    assert (
        "Fetch the users, data sources, refreshes and parameters of Dataset 1" in text
    )
    assert [(r[0], r[1], r[3]) for r in rows] == [
        ("Users", "2 rows", "admin.datasets.users"),
        ("Data sources", "1 row", "admin.datasets.datasources"),
        ("Refreshes", "1 row", "admin.refreshables"),
        ("Parameters", "1 row", "user.dataset_parameters"),
    ]
    assert "The lake holds 4 of 4 details of this." in note
    assert calls(world, r"^/admin/datasets/[^/]+/users$") == [
        "/admin/datasets/ds-0001/users"
    ]
    assert calls(world, r"^/admin/datasets/[^/]+/datasources$") == [
        "/admin/datasets/ds-0001/datasources"
    ]
    assert calls(world, r"^/groups/[^/]+/datasets/[^/]+/parameters$") == [
        "/groups/ws-0001/datasets/ds-0001/parameters"
    ]
    assert calls(world, r"^/admin/capacities/refreshables$") == [
        "/admin/capacities/refreshables"
    ]


def test_on_the_users_tab_f_fetches_only_the_users(world):
    world.fake.reset_calls()

    async def scenario(ui):
        await pick(ui, "dataset", "ds-0001", "2")
        await ui.press("f")
        text = asked(ui)
        await ui.press("enter")
        await ui.finish_sync()
        await ui.settle()
        return text, ui.rows("#users"), ui.static("#users-note")

    text, rows, note = run_ui(backend_of(world), scenario)

    assert "Fetch the users of Dataset 1" in text and "data sources" not in text
    assert [(r[0], r[2]) for r in rows] == [("Ann", "Owner"), ("Bob", "Read")]
    assert "from admin.datasets.users" in note
    assert not world.fake.calls_to(r"/datasources$")


def test_the_users_tab_says_how_to_get_the_users_when_they_are_missing(world):
    async def scenario(ui):
        await pick(ui, "dataset", "ds-0001", "2")
        return ui.static("#users-note")

    note = run_ui(backend_of(world), scenario)

    assert "The lake does not hold the users of this item." in note
    assert "Press f: 1 request, an administrator account." in note


def test_an_item_whose_details_are_all_held_has_nothing_to_fetch(world):
    world.run("datasets", "dataset-users", "datasources", "refreshables")
    world.only_user()
    world.run("user-dataset-parameters")

    async def scenario(ui):
        await pick(ui, "dataset", "ds-0001", "6")
        before = details(ui)
        await ui.press("f")
        return (
            before,
            [n.message for n in ui.app._notifications],
            type(ui.app.screen).__name__,
        )

    (rows, note), notes, screen = run_ui(
        backend_of(world, client_for=world.engine._client_for), scenario
    )

    assert all(r[1].endswith("row") or r[1].endswith("rows") for r in rows)
    assert "Press f" not in note
    assert any("The lake holds every detail of this already." in n for n in notes)
    assert screen == "ExplorerScreen"


# ---------------------------------------------------------------------------
# the accounts
# ---------------------------------------------------------------------------


def test_someone_with_only_a_user_account_fetches_what_a_user_can(user_world):
    user_world.fake.reset_calls()

    async def scenario(ui):
        await pick(ui, "dataset", "ds-0001", "6")
        before = details(ui)[0]
        await ui.press("f")
        text = asked(ui)
        await ui.press("enter")
        await ui.finish_sync()
        await ui.settle()
        return before, text, details(ui)[0]

    before, text, after = run_ui(user_backend(user_world), scenario)

    assert [r[3] for r in before] == [
        "press f: 1 request, a user account with Reshare permission on the dataset (ReadWriteReshare)",
        "press f: 1 request, a user account with Write permission on the dataset",
        "press f: 1 request, a user account with Write permission on the dataset",
        "press f: 1 request, a user account",
    ]
    assert (
        "Fetch the users, data sources, refreshes and parameters of Dataset 1" in text
    )
    assert [r[3] for r in after] == [
        "user.dataset_users",
        "user.dataset_datasources",
        "user.dataset_refreshes",
        "user.dataset_parameters",
    ]
    assert not user_world.fake.calls_to(r"^/admin")


def test_a_refusal_is_shown_with_the_permission_that_is_missing(user_world):
    user_world.fake.restricted_datasets.add("ds-0001")

    async def scenario(ui):
        await pick(ui, "dataset", "ds-0001", "6")
        await ui.press("f")
        await ui.press("enter")
        await ui.finish_sync()
        await ui.settle()
        return details(ui)

    rows, note = run_ui(user_backend(user_world), scenario)

    states = {r[0]: r[1] for r in rows}
    assert states["Users"] == "refused" and states["Data sources"] == "refused"
    assert states["Parameters"] == "1 row"  # that one needs no permission
    said = {line.split(":")[0]: line for line in note.splitlines()}
    write = "The account needs Write permission on the dataset."
    assert write in said["Data sources"] and write in said["Refreshes"]
    assert "The account needs Reshare permission on the dataset" in said["Users"]


def test_a_detail_only_an_administrator_can_fetch_says_which_account_is_missing(
    user_world,
):
    async def scenario(ui):
        await pick(ui, "report", "rep-0001", "6")
        rows, note = details(ui)
        await ui.press("f")
        text = asked(ui)
        await ui.press("escape")
        return rows, text

    rows, text = run_ui(user_backend(user_world), scenario)

    assert rows[0][:2] == ["Users", "missing"]
    assert rows[0][3] == "needs an administrator account, and none is stored"
    assert rows[1][3] == "press f: 1 request, a user account"  # the pages
    assert "Fetch the pages of Report 1" in text and "users" not in text


def test_f_says_so_when_no_stored_account_can_fetch_what_is_missing(user_world):
    user_world.run("user-pages")  # the pages: what a user could fetch of a report

    async def scenario(ui):
        await pick(ui, "report", "rep-0001", "6")
        await ui.press("f")
        return [n.message for n in ui.app._notifications], type(ui.app.screen).__name__

    notes, screen = run_ui(user_backend(user_world), scenario)

    assert any("needs an administrator account" in n and "press a" in n for n in notes)
    assert screen == "ExplorerScreen"


def test_f_asks_for_nothing_when_nothing_is_selected_that_has_details(world):
    async def scenario(ui):
        await ui.select("capacities")
        await ui.press("f")
        return [n.message for n in ui.app._notifications]

    notes = run_ui(backend_of(world), scenario)

    assert any("Select a workspace or an item" in n for n in notes)


# ---------------------------------------------------------------------------
# a lake that is only looked at
# ---------------------------------------------------------------------------


def view_only(world):
    return backend_of(
        world,
        store=LakeStore(world.store.root, readonly=True, reason="The lake only reads."),
    )


def test_a_lake_that_is_only_looked_at_fetches_nothing_and_says_so(world):
    async def scenario(ui):
        await pick(ui, "dataset", "ds-0001", "6")
        rows, note = details(ui)
        await ui.press("f")
        return (
            rows,
            note,
            [n.message for n in ui.app._notifications],
            type(ui.app.screen).__name__,
        )

    rows, note, notes, screen = run_ui(view_only(world), scenario)

    assert rows[0][3] == "view only: nothing is fetched into this lake"
    assert "Press f" not in note
    assert (
        any("The lake only reads." in n for n in notes) and screen == "ExplorerScreen"
    )


# ---------------------------------------------------------------------------
# the command palette
# ---------------------------------------------------------------------------


def test_the_palette_offers_to_fetch_what_the_selected_item_lacks(world):
    async def scenario(ui):
        await pick(ui, "dataset", "ds-0001")
        found = [
            c
            for c in commands_for(ui.app, ui.app.screen)
            if c.title.startswith("Fetch the users")
        ]
        found[0].run()
        await ui.settle()
        return [c.title for c in found], type(ui.app.screen).__name__, asked(ui)

    titles, screen, text = run_ui(backend_of(world), scenario)

    assert titles == [
        "Fetch the users, data sources, refreshes and parameters of Dataset 1"
    ]
    assert screen == "ConfirmModal" and "Dataset 1" in text


def test_the_palette_offers_nothing_to_fetch_when_the_lake_holds_it_all(world):
    world.run("datasets", "dataset-users", "datasources", "refreshables")
    world.only_user()
    world.run("user-dataset-parameters")

    async def scenario(ui):
        await pick(ui, "dataset", "ds-0001")
        return [
            c.title
            for c in commands_for(ui.app, ui.app.screen)
            if " of Dataset 1" in c.title
        ]

    assert (
        run_ui(backend_of(world, client_for=world.engine._client_for), scenario) == []
    )


def test_the_palette_shows_the_details_tab(world):
    async def scenario(ui):
        titles = [c.title for c in commands_for(ui.app, ui.app.screen)]
        next(
            c
            for c in commands_for(ui.app, ui.app.screen)
            if c.title == "Show the Details tab"
        ).run()
        await ui.settle()
        return (
            "Show the Details tab" in titles,
            ui.explorer.query_one("#detail", TabbedContent).active,
        )

    assert run_ui(backend_of(world), scenario) == (True, "tab-details")


def test_a_refresh_selects_the_same_row_again_when_the_lake_has_been_read(world):
    world.clock.advance(hours=5)

    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        await ui.pick_row("dataset:ds-0001")
        before = ui.explorer._subject.name
        await ui.press("r")
        await ui.press("enter")
        await ui.finish_sync()
        await ui.settle()
        return before, ui.explorer._subject.name

    assert run_ui(backend_of(world), scenario) == ("Dataset 1", "Dataset 1")


def test_f_on_what_has_no_details_says_so(world):
    async def scenario(ui):
        await pick(ui, "app", "app-0001")
        await ui.press("f")
        return [n.message for n in ui.app._notifications]

    notes = run_ui(backend_of(world), scenario)

    assert any("nothing more to fetch for this app" in n for n in notes)


def test_the_palette_offers_no_fetch_on_a_lake_that_is_only_looked_at(world):
    async def scenario(ui):
        await pick(ui, "dataset", "ds-0001")
        return [
            c.title
            for c in commands_for(ui.app, ui.app.screen)
            if c.title.startswith("Fetch")
        ]

    assert run_ui(view_only(world), scenario) == []
