"""The Scan tab in the Explorer, and the tabs that combine the workspaces of the list."""

import pytest
from sync_helpers import World
from textual.widgets import Input, TabbedContent, Tree
from tui_helpers import backend_of, plain, run_ui

from pbi_cli.core.scan import ScanFlags
from pbi_cli.tui import render
from pbi_cli.tui.commands import commands_for

EVERYTHING = ScanFlags(
    lineage=True,
    datasource_details=True,
    dataset_schema=True,
    dataset_expressions=True,
    get_artifact_users=True,
)


@pytest.fixture
def world(tmp_path):
    world = World(tmp_path)
    world.run()
    world.run("scan", scan_flags=EVERYTHING, workspace_ids=("ws-0001", "ws-0002"))
    world.run("group-users", force=True)
    return world


def scan_text(ui):
    return plain(ui.explorer.query_one("#scan").content)


def active_tab(ui):
    return ui.explorer.query_one("#detail", TabbedContent).active


async def focus_the_tree(ui):
    ui.explorer.query_one("#tree", Tree).focus()
    await ui.settle()


# ---------------------------------------------------------------------------
# the tab
# ---------------------------------------------------------------------------


def test_the_scan_tab_is_the_last_tab_and_has_a_key(world):
    async def scenario(ui):
        tabs = [
            pane.id
            for pane in ui.explorer.query_one("#detail", TabbedContent).query("TabPane")
        ]
        await ui.select("workspace", "ws-0001")
        await ui.press("7")
        return tabs, active_tab(ui)

    tabs, active = run_ui(backend_of(world), scenario)

    assert tabs[-1] == "tab-scan" and active == "tab-scan"


def test_the_scan_of_a_workspace_is_shown_in_its_tab(world):
    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        await ui.press("7")
        return scan_text(ui)

    text = run_ui(backend_of(world), scenario)

    assert text.startswith("Workspace 1   scan as of")
    assert "Where the data comes from (read from the queries)" in text


def test_the_scan_of_an_item_follows_the_row(world):
    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        await ui.press("7")
        await ui.pick_row("dataset:ds-0001")
        dataset = scan_text(ui)
        await ui.pick_row("report:rep-0001")
        report = scan_text(ui)
        return dataset, report

    dataset, report = run_ui(backend_of(world), scenario)

    assert dataset.startswith("Dataset 1   dataset · in Workspace 1")
    assert "SQL Server  sql.example / ds-0001  dbo.Sales" in dataset
    assert report.startswith("Report 1   report · in Workspace 1")
    assert "Built on the dataset Dataset 1:" in report


def test_a_workspace_that_was_not_scanned_says_how_to_get_a_scan(world):
    async def scenario(ui):
        await ui.select("workspace", "ws-0005")
        await ui.press("7")
        return scan_text(ui)

    text = run_ui(backend_of(world), scenario)

    assert text.startswith("No scan of Workspace 5 is in the lake. Press r to scan it")


def test_the_palette_offers_the_scan_tab(world):
    async def scenario(ui):
        return {c.title: c.help for c in commands_for(ui.app, ui.app.screen)}

    found = run_ui(backend_of(world), scenario)

    assert found["Show the Scan tab"] == (
        "What a scan says: tables, measures, and where each table gets its data  ·  key 7"
    )


# ---------------------------------------------------------------------------
# the workspace list: its tabs cover the workspaces the table lists
# ---------------------------------------------------------------------------


def test_the_title_of_the_list_says_what_the_tabs_cover(world):
    async def scenario(ui):
        await ui.select("workspaces")
        return plain(ui.explorer.query_one("#table-title").content)

    title = run_ui(backend_of(world), scenario)

    assert title == (
        "Workspaces: 12  ·  the tabs below cover all of them until you pick a row"
    )


def test_the_users_and_scan_tabs_of_the_list_combine_its_workspaces(world):
    async def scenario(ui):
        await ui.select("workspaces")
        await focus_the_tree(ui)
        await ui.press("2")
        users = plain(ui.explorer.query_one("#users-note").content), ui.columns(
            "#users"
        )
        await ui.press("7")
        return users, scan_text(ui)

    (note, columns), scan = run_ui(backend_of(world), scenario)

    assert note.startswith("24 entries: 24 people with access to 12 of 12 workspaces.")
    assert columns == ["Name", "E-mail or id", "Access", "Type", "Workspace"]
    assert scan.startswith("12 workspaces   2 scanned, 10 not")


def test_the_info_of_the_list_counts_the_workspaces(world):
    async def scenario(ui):
        await ui.select("workspaces")
        await focus_the_tree(ui)
        return ui.static("#info")

    info = run_ui(backend_of(world), scenario)

    assert (
        "Workspaces                     12" in info
        and "Scanned                        2 of 12" in info
    )


def test_the_filter_of_the_table_narrows_what_the_tabs_cover(world):
    async def scenario(ui):
        await ui.select("workspaces")
        await focus_the_tree(ui)
        ui.explorer.query_one("#table-filter", Input).value = "workspace 12"
        await ui.settle()
        await ui.press("2")
        return (
            plain(ui.explorer.query_one("#users-note").content),
            plain(ui.explorer.query_one("#table-title").content),
        )

    note, title = run_ui(backend_of(world), scenario)

    assert note.startswith("2 entries: 2 people with access to 1 of 1 workspace.")
    assert "1 match(es) for 'workspace 12'" in title


def test_the_tabs_cover_every_workspace_of_the_list_even_when_the_table_shows_fewer(
    world, monkeypatch
):
    monkeypatch.setattr(render, "MAX_ROWS", 5)

    async def scenario(ui):
        await ui.select("workspaces")
        await focus_the_tree(ui)
        return (
            len(ui.rows()),
            plain(ui.explorer.query_one("#table-title").content),
            ui.static("#info"),
        )

    shown, title, info = run_ui(backend_of(world), scenario)

    assert shown == 5
    assert "the first 5 are shown: narrow them with /" in title
    assert info.startswith("Workspaces   12 workspaces")  # not the 5 of the table


def test_a_picked_row_is_one_workspace_again(world):
    async def scenario(ui):
        await ui.select("workspaces")
        await ui.pick_row("ws-0002")
        await ui.press("2")
        return plain(ui.explorer.query_one("#users-note").content), ui.columns("#users")

    note, columns = run_ui(backend_of(world), scenario)

    assert note.startswith("2 with access")
    assert columns == ["Name", "E-mail or id", "Access", "Type"]


def test_the_personal_workspaces_have_their_own_set(world):
    world.fake.workspaces[11]["type"] = "PersonalGroup"
    world.run("groups", force=True)

    async def scenario(ui):
        await ui.select("personal")
        await focus_the_tree(ui)
        await ui.press("1")
        return ui.static("#info")

    info = run_ui(backend_of(world), scenario)

    assert info.startswith("Personal workspaces   1 workspaces")
