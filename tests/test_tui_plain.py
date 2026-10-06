"""Names that look like markup (``[Confidential]Sales``, ``Sales [/Q4]``) are shown as they are.

Textual reads a ``str`` it is given as markup: the first would lose its bracket and the second
would end the app. Power BI names are free text, and tags in brackets are common in them.
"""

import asyncio

import pytest
from rich.text import Text
from sync_helpers import World
from textual.app import App
from textual.widgets import DataTable, OptionList
from tui_helpers import backend_of, plain, run_ui

from pbi_cli.core.catalog import Catalog
from pbi_cli.tui import render
from pbi_cli.tui.modals import ChoiceModal, ConfirmModal, OpenLakeModal
from pbi_cli.tui.plain import PlainLabel, PlainStatic, PlainTable

TAGGED = "[Confidential]IT Management"
BRACKETED = "[Experiment] Test"
BROKEN = "Sales [/Q4]"
NAMES = {"ws-0001": TAGGED, "ws-0002": BRACKETED, "ws-0003": BROKEN}


@pytest.fixture
def world(tmp_path):
    world = World(tmp_path)
    for workspace in world.fake.workspaces:
        workspace["name"] = NAMES.get(workspace["id"], workspace["name"])
    for report in world.fake.reports:
        report["name"] = f"[DEV] {report['name']}"
    world.run("groups", "reports", "datasets", "dashboards", "dataflows")
    return world


def shown(ui, selector):
    """What a widget of the screen shows, after Textual has read its text."""
    return ui.app.screen.query_one(selector).visual.plain


def rendered_options(screen, count):
    """What the first options of the list of choices of a dialog show, once Textual has read
    their text."""
    choices = screen.query_one("#choices", OptionList)
    return [
        choices._get_visual(choices.get_option_at_index(i)).plain for i in range(count)
    ]


# ---------------------------------------------------------------------------
# the widgets
# ---------------------------------------------------------------------------


def test_a_plain_static_and_label_show_their_text_as_it_is():
    assert PlainStatic(BROKEN).visual.plain == BROKEN
    assert PlainStatic(TAGGED).visual.plain == TAGGED
    assert PlainLabel(BRACKETED).visual.plain == BRACKETED


def test_a_plain_static_shows_what_it_is_updated_with_as_it_is():
    static = PlainStatic()
    static.update(f"{BROKEN}: 2 item(s)")

    assert static.visual.plain == f"{BROKEN}: 2 item(s)"


def in_an_app(widget, check):
    """Mount a widget in an app and let ``check`` look at it (a table needs an app to add rows)."""

    class Host(App):
        def compose(self):
            yield widget

    async def main():
        async with Host().run_test():
            return check(widget)

    return asyncio.run(main())


def test_a_plain_table_keeps_the_text_of_a_cell_and_what_is_not_text():
    marked = Text("x", style="bold")

    def check(table):
        table.add_columns("Name", "Marked", "Count")
        table.add_row(TAGGED, marked, 3, key="row")
        return table.get_row("row")

    cells = in_an_app(PlainTable(), check)

    assert [type(cell) for cell in cells] == [Text, Text, int]
    assert cells[0].plain == TAGGED and cells[1] is marked and cells[2] == 3


def test_a_plain_table_still_takes_a_key_and_a_height():
    def check(table):
        table.add_column("Name")
        key = table.add_row(BROKEN, height=2, key="mine")
        return key.value, table.row_count, table.rows[key].height

    assert in_an_app(PlainTable(), check) == ("mine", 1, 2)


# ---------------------------------------------------------------------------
# the Explorer
# ---------------------------------------------------------------------------


def test_the_tree_and_the_title_of_the_table_have_the_whole_name(world):
    async def scenario(ui):
        titles = []
        for workspace in NAMES:
            await ui.select("workspace", workspace)
            titles.append(shown(ui, "#table-title"))
        return ui.tree(), titles

    tree, titles = run_ui(backend_of(world), scenario)

    for name in NAMES.values():
        assert f"  ● {name}" in tree
    assert [t.split(": ")[0] for t in titles] == list(NAMES.values())


def test_a_workspace_named_like_a_closing_tag_does_not_end_the_app(world):
    async def scenario(ui):
        await ui.select("workspace", "ws-0003")
        await ui.press("2")
        await ui.press("1")
        return shown(ui, "#table-title")

    assert run_ui(backend_of(world), scenario).startswith(BROKEN)


def test_the_cells_of_the_table_are_the_names_as_they_are(world):
    async def scenario(ui):
        await ui.select("workspaces")
        names = [row[1] for row in ui.rows()]
        await ui.select("workspace", "ws-0001")
        items = [row[0] for row in ui.rows()]
        table = ui.explorer.query_one("#table", DataTable)
        return names, items, [type(c) for c in table.get_row_at(0)]

    names, items, types = run_ui(backend_of(world), scenario)

    assert {TAGGED, BRACKETED, BROKEN} <= set(names)
    assert "[DEV] Report 1" in items
    assert Text in types and str not in types


def test_the_info_of_a_workspace_and_of_an_item_has_the_whole_name(world):
    catalog = Catalog(world.store, "tenant-1", clock=world.clock.now)

    for workspace_id, name in NAMES.items():
        info = plain(render.info(catalog, catalog.workspace(workspace_id)))
        assert name in info
    report = catalog.all_items("report")[0]
    assert "[DEV] Report 1" in plain(render.info(catalog, report))


def test_a_description_with_brackets_is_in_the_info_as_it_is(world):
    for workspace in world.fake.workspaces:
        workspace["description"] = "see [docs] and [/end]"
    world.run("groups", force=True)
    catalog = Catalog(world.store, "tenant-1", clock=world.clock.now)

    info = plain(render.info(catalog, catalog.workspace("ws-0001")))

    assert "see [docs] and [/end]" in info


# ---------------------------------------------------------------------------
# what the app says
# ---------------------------------------------------------------------------


def test_a_notification_is_never_read_as_markup(world):
    async def scenario(ui):
        ui.app.notify(f"Cannot fetch {BROKEN} [InvalidRequest]", markup=True)
        ui.explorer.notify(f"{TAGGED} is not there", title="Plan file")
        await ui.settle()
        await ui.pilot.pause(0.2)
        return [(n.message, n.markup) for n in ui.app._notifications]

    notes = run_ui(backend_of(world), scenario)

    assert notes == [
        (f"Cannot fetch {BROKEN} [InvalidRequest]", False),
        (f"{TAGGED} is not there", False),
    ]


def test_the_choices_of_a_dialog_are_shown_as_they_are(world):
    async def scenario(ui):
        ui.app.push_screen(
            ChoiceModal("Choose", [("a", f"{TAGGED} (tenant)"), ("b", BROKEN)])
        )
        await ui.settle()
        return rendered_options(ui.app.screen, 2)

    assert run_ui(backend_of(world), scenario) == [f"{TAGGED} (tenant)", BROKEN]


def test_the_lakes_of_the_open_dialog_are_shown_as_they_are(world):
    async def scenario(ui):
        ui.app.push_screen(
            OpenLakeModal(
                work="C:\\Users\\[me]\\lake",
                recent=["s3://bucket/[old]", "D:\\[/x]"],
                current="s3://bucket/[now]",
            )
        )
        await ui.settle()
        return rendered_options(ui.app.screen, 3)

    assert run_ui(backend_of(world), scenario) == [
        "Work lake   C:\\Users\\[me]\\lake",
        "Recent      s3://bucket/[old]",
        "Recent      D:\\[/x]",
    ]


def test_a_dialog_shows_the_names_it_is_asked_about_as_they_are(world):
    title = f"Fetch the users of {BROKEN}"

    async def scenario(ui):
        ui.app.push_screen(ConfirmModal(title, f"for {TAGGED}"))
        await ui.settle()
        return shown(ui, "#dialog-title"), shown(ui, "#dialog-body Static")

    assert run_ui(backend_of(world), scenario) == (title, f"for {TAGGED}")
