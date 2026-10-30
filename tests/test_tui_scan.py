"""The Scan tab in the Explorer, and the tabs that combine the workspaces of the list."""

import pytest
from sync_helpers import World
from textual.widgets import Input, TabbedContent, Tree
from tui_helpers import backend_of, plain, run_ui

from pbi_cli.core.planfile import PlanFile
from pbi_cli.core.scan import ScanFlags
from pbi_cli.tui import render
from pbi_cli.tui.commands import commands_for
from pbi_cli.tui.modals import ConfirmModal

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


def planned(world, tmp_path, text):
    """A backend with a plan file (and a lake whose newest scan of Workspace 1 is poor)."""
    path = tmp_path / "pbi-plan.yaml"
    path.write_text(text, encoding="utf-8")
    return backend_of(
        world, plan=PlanFile.load(path), reload_plan=lambda: PlanFile.load(path)
    )


def make_the_scan_poor(world, flags=ScanFlags(lineage=True)):
    world.clock.advance(minutes=5)
    world.run("scan", scan_flags=flags, workspace_ids=("ws-0001",), force=True)


def question(ui):
    """What the dialog that asks whether to scan says."""
    screen = ui.app.screen
    assert isinstance(screen, ConfirmModal)
    return plain(screen.query_one("#dialog-body Static").content)


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


def test_the_scan_tab_says_what_the_plan_asks_for_and_the_scan_lacks(world, tmp_path):
    make_the_scan_poor(world)

    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        await ui.press("7")
        return scan_text(ui)

    text = run_ui(
        planned(
            world, tmp_path, "version: 1\nworkspaces:\n  - {id: ws-0001, scan: all}\n"
        ),
        scenario,
    )

    assert (
        "The plan file asks for datasource details, dataset schema, dataset expressions "
        "and get artifact users for this workspace, but its newest scan was made "
        "without them"
    ) in " ".join(
        text.split()
    )  # the sentence wraps


def test_a_scan_that_has_what_the_plan_asks_for_is_not_out_of_date_in_the_tab(
    world, tmp_path
):
    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        await ui.press("7")
        return scan_text(ui)

    text = run_ui(
        planned(
            world, tmp_path, "version: 1\nworkspaces:\n  - {id: ws-0001, scan: all}\n"
        ),
        scenario,
    )

    assert "The plan file asks for" not in text and "without" not in text


def test_r_scans_a_workspace_with_what_the_plan_asks_for_and_its_last_scan_had(
    world, tmp_path
):
    make_the_scan_poor(world, ScanFlags(lineage=True, get_artifact_users=True))
    world.clock.advance(hours=5)
    world.fake.reset_calls()

    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        await ui.press("r")
        text = question(ui)
        await ui.press("enter")
        await ui.finish_sync()
        return text

    text = run_ui(
        planned(
            world,
            tmp_path,
            "version: 1\nworkspaces:\n  - {id: ws-0001, scan: [dataset_schema]}\n",
        ),
        scenario,
    )

    assert "with lineage, dataset schema, get artifact users" in text
    (call,) = world.fake.calls_to("getInfo", "POST")
    assert call.query == {
        "lineage": "true",
        "datasourceDetails": "false",
        "datasetSchema": "true",
        "datasetExpressions": "false",
        "getArtifactUsers": "true",
    }


def test_r_scans_a_workspace_the_plan_does_not_name_with_every_option(world, tmp_path):
    make_the_scan_poor(world)
    world.clock.advance(hours=5)
    world.fake.reset_calls()

    async def scenario(ui):
        await ui.press("w")  # every workspace, not only the one that the plan names
        await ui.select("workspace", "ws-0001")
        await ui.press("r")
        text = question(ui)
        await ui.press("enter")
        await ui.finish_sync()
        return text

    text = run_ui(
        planned(
            world,
            tmp_path,
            "version: 1\nworkspaces:\n  - {id: ws-0002, scan: [lineage]}\n",
        ),
        scenario,
    )

    assert "with lineage, datasource details, dataset schema" in text
    (call,) = world.fake.calls_to("getInfo", "POST")
    assert set(call.query.values()) == {"true"}


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
