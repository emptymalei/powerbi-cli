"""The Explorer: the tree, the table, the detail pane, filtering and refreshing."""

import pytest
from sync_helpers import World
from textual.widgets import Input, Tree
from tui_helpers import backend_of, plain, run_ui

from pbi_cli.core.catalog import Match
from pbi_cli.core.scan import ScanFlags
from pbi_cli.errors import AuthError
from pbi_cli.tui.explorer import NodeRef

FULL = ScanFlags(lineage=True, datasource_details=True, get_artifact_users=True)


@pytest.fixture
def synced(tmp_path):
    """A lake with the plain targets, a complete scan with every option, and two days of events."""
    world = World(tmp_path)
    world.run()
    world.run("scan", scan_flags=FULL)
    world.run("activity", days=2)
    return world


def names(rows):
    return [row[0] for row in rows]


# ---------------------------------------------------------------------------
# the tree
# ---------------------------------------------------------------------------


def test_the_tree_shows_what_the_lake_holds(synced):
    async def scenario(ui):
        return ui.tree()

    tree = run_ui(backend_of(synced), scenario)

    assert tree[0] == "● Workspaces  12"
    assert tree[1] == "  ● Workspace 1"
    assert "  ● Workspace 9" in tree
    assert tree[-4:] == [
        "● Apps  3",
        "● Capacities  1",
        "Activity  2",
        "  2026-09-29  5",
    ][:0] or (
        "● Apps  3" in tree and "● Capacities  1" in tree and "Activity  2" in tree
    )
    assert any(line.startswith("  2026-09-30") for line in tree)


def test_the_header_says_who_and_where(synced):
    async def scenario(ui):
        return ui.static("#who")

    who = run_ui(backend_of(synced), scenario)

    assert "tenant-1" in who and "admin-nlm (admin)" in who and "token" in who


def test_personal_workspaces_have_their_own_branch(tmp_path):
    world = World(tmp_path)
    world.fake.workspaces[2]["type"] = "PersonalGroup"
    world.run("groups")

    async def scenario(ui):
        return ui.tree()

    tree = run_ui(backend_of(world), scenario)

    assert tree[0] == "● Workspaces  11"
    assert any(line == "● Personal workspaces  1" for line in tree)


def test_an_empty_lake_says_what_to_do(tmp_path):
    world = World(tmp_path)

    async def scenario(ui):
        return ui.static("#info"), ui.tree(), ui.static("#who")

    info, tree, who = run_ui(backend_of(world), scenario)

    assert "The lake is empty" in info and "press s" in info
    assert tree == []
    assert "tenant-1" in who


# ---------------------------------------------------------------------------
# the table and the detail pane
# ---------------------------------------------------------------------------


def test_the_root_lists_what_the_lake_holds_and_sums_it_up(synced):
    async def scenario(ui):
        return ui.rows(), ui.static("#info")

    rows, info = run_ui(backend_of(synced), scenario)

    assert [(r[1], r[2]) for r in rows] == [
        ("Workspaces", "12"),
        ("Reports", "6"),
        ("Datasets", "4"),
        ("Dashboards", "1"),
        ("Dataflows", "1"),
        ("Apps", "3"),
        ("Capacities", "1"),
        ("Audit events", "2 day(s)"),
    ]
    assert "Tenant" in info and "fresh" in info  # the legend of the dots


def test_the_workspaces_node_lists_the_workspaces(synced):
    async def scenario(ui):
        await ui.select("workspaces")
        return ui.columns(), ui.rows(), ui.static("#table-title")

    columns, rows, title = run_ui(backend_of(synced), scenario)

    assert columns == ["", "Name", "Type", "State", "Items", "Scanned"]
    assert len(rows) == 12
    first = next(row for row in rows if row[1] == "Workspace 1")
    assert first[2:5] == ["Workspace", "Active", "5"] and first[5] == "0 s ago"
    assert "12" in title


def test_a_workspace_shows_its_items_and_its_own_details(synced):
    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        return ui.columns(), ui.rows(), ui.static("#info")

    columns, rows, info = run_ui(backend_of(synced), scenario)

    assert columns == ["Name", "Type", "Owner", "Updated", "From"]
    assert [(r[0], r[1], r[4]) for r in rows] == [
        ("Report 1", "Report", "list + scan"),
        ("Dataset 1", "Dataset", "list + scan"),
        ("Dashboard 1", "Dashboard", "list + scan"),
        ("Dataflow 1", "Dataflow", "list + scan"),
        ("App 1", "App", "list"),
    ]
    assert "Workspace 1" in info and "ws-0001" in info
    assert "Reports" in info and "Scan" in info and "lineage" in info


def test_a_row_of_the_table_is_described_in_the_detail_pane(synced):
    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        await ui.pick_row("dataset:ds-0001")
        return ui.static("#info")

    info = run_ui(backend_of(synced), scenario)

    assert "Dataset 1" in info and "ds-0001" in info and "owner@example.com" in info


def test_the_json_tab_shows_the_stored_answer(synced):
    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        await ui.pick_row("report:rep-0001")
        await ui.press("4")
        return ui.static("#json")

    shown = run_ui(backend_of(synced), scenario)

    assert '"datasetId": "ds-0001"' in shown and '"name": "Report 1"' in shown
    assert "stored in" in shown


def test_the_users_tab_lists_who_has_access(synced):
    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        await ui.press("2")
        workspace = ui.rows("#users"), ui.static("#users-note")
        await ui.pick_row("report:rep-0001")
        report = ui.rows("#users"), ui.static("#users-note")
        return workspace, report, ui.columns("#users")

    workspace, report, columns = run_ui(backend_of(synced), scenario)

    assert columns == ["Name", "E-mail or id", "Access", "Type"]
    assert workspace[0] == [["Owner", "owner@example.com", "Admin", "User"]]
    assert "from scan" in workspace[1]
    assert report[0] == [["Ann", "ann@example.com", "Owner", "User"]]


def test_the_lineage_tab_shows_what_it_is_built_from_and_what_is_built_on_it(synced):
    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        await ui.pick_row("dataset:ds-0001")
        await ui.press("3")
        return ui.static("#lineage")

    shown = run_ui(backend_of(synced), scenario)

    assert "Built from" in shown and "dataflow Dataflow 1" in shown
    assert "data source Sql: sql.example, ds-0001" in shown
    assert "Built on it" in shown and "report Report 1" in shown
    assert "dashboard Dashboard 1" in shown
    assert "report Report 5" in shown and "in Workspace 5" in shown


def test_the_lineage_tab_of_a_workspace_asks_for_an_item(synced):
    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        await ui.press("3")
        return ui.static("#lineage")

    assert "Pick a report" in run_ui(backend_of(synced), scenario)


def test_the_versions_tab_lists_the_stored_answers(synced):
    synced.clock.advance(hours=3)
    synced.run("groups", "scan", force=True, scan_flags=FULL)

    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        await ui.press("5")
        return (
            ui.rows("#versions"),
            ui.columns("#versions"),
            ui.static("#versions-note"),
        )

    rows, columns, note = run_ui(backend_of(synced), scenario)

    assert columns == ["Fetched", "", "Operation", "Rows", "Size", "By profile"]
    assert sorted(r[2] for r in rows) == [
        "admin.groups",
        "admin.groups",
        "admin.scan.result",
        "admin.scan.result",
    ]
    assert rows[0][1] == "0 s ago" and rows[-1][1] == "3 h ago"
    assert "4 stored answer(s)" in note


def test_apps_and_capacities_have_their_own_nodes(synced):
    async def scenario(ui):
        await ui.select("apps")
        apps = ui.columns(), ui.rows()
        await ui.select("capacities")
        return apps, (ui.columns(), ui.rows(), ui.static("#info"))

    apps, capacities = run_ui(backend_of(synced), scenario)

    assert apps[0] == ["Name", "Workspace", "Published by", "Updated"]
    assert [(r[0], r[1]) for r in apps[1]] == [
        ("App 1", "Workspace 1"),
        ("App 2", "Workspace 2"),
        ("App 3", "Workspace 3"),
    ]
    assert capacities[0] == ["Name", "SKU", "Region", "State"]
    assert capacities[1] == [["Capacity 1", "P1", "-", "-"]]
    assert "Capacity 1" in capacities[2]


def test_a_day_of_audit_events_is_listed_newest_first(synced):
    async def scenario(ui):
        await ui.select("activity")
        days = ui.columns(), ui.rows()
        await ui.select("activity", "2026-09-29")
        return days, ui.columns(), ui.rows(), ui.static("#info")

    days, columns, events, info = run_ui(backend_of(synced), scenario)

    assert days[0] == ["Day", "Events", "State", "Updated"]
    assert [d[0] for d in days[1]] == ["2026-09-30", "2026-09-29"]
    assert columns == ["Time", "Activity", "User", "Item", "Workspace"]
    assert len(events) == 5
    assert [e[0] for e in events] == sorted((e[0] for e in events), reverse=True)
    assert "2026-09-29" in info and "audit events" in info


# ---------------------------------------------------------------------------
# filtering and going somewhere
# ---------------------------------------------------------------------------


def test_the_table_can_be_filtered_and_the_filter_cleared(synced):
    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        table = ui.explorer.query_one("#table")
        table.focus()
        await ui.press("slash")
        shown = ui.explorer.query_one("#table-filter", Input).display
        await ui.type("data")
        narrowed = names(ui.rows()), ui.static("#table-title")
        await ui.press("escape")
        cleared = (
            names(ui.rows()),
            ui.explorer.query_one("#table-filter", Input).display,
        )
        return shown, narrowed, cleared

    shown, narrowed, cleared = run_ui(backend_of(synced), scenario)

    assert shown is True
    assert narrowed[0] == ["Dataset 1", "Dataflow 1"]
    assert "2 match(es)" in narrowed[1]
    assert len(cleared[0]) == 5 and cleared[1] is False


def test_the_tree_can_be_filtered(synced):
    async def scenario(ui):
        ui.explorer.query_one("#tree", Tree).focus()
        await ui.press("slash")
        await ui.type("space 1")
        narrowed = ui.tree()
        await ui.press("escape")
        return narrowed, ui.tree()

    narrowed, cleared = run_ui(backend_of(synced), scenario)

    workspaces = [line for line in narrowed if line.startswith("  ●")]
    assert sorted(workspaces) == [
        "  ● Workspace 1",
        "  ● Workspace 10",
        "  ● Workspace 11",
        "  ● Workspace 12",
    ]
    assert len([line for line in cleared if line.startswith("  ●")]) == 12


def test_a_row_can_be_filtered_by_its_type(synced):
    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        ui.explorer.query_one("#table").focus()
        await ui.press("slash")
        await ui.type("report")
        return names(ui.rows())

    assert run_ui(backend_of(synced), scenario) == ["Report 1"]


def test_goto_selects_a_workspace_and_an_item_in_it(synced):
    async def scenario(ui):
        ui.app.goto(Match("report", "rep-0003", "Report 3", "ws-0003", "Workspace 3"))
        await ui.settle()
        return ui.static("#table-title"), ui.static("#info"), ui.explorer._ref

    title, info, ref = run_ui(backend_of(synced), scenario)

    assert ref.kind == "workspace" and ref.id == "ws-0003"
    assert title.startswith("Workspace 3")
    assert "Report 3" in info and "rep-0003" in info


def test_goto_a_workspace(synced):
    async def scenario(ui):
        ui.app.goto(Match("workspace", "ws-0007", "Workspace 7"))
        await ui.settle()
        return ui.static("#info")

    assert "Workspace 7" in run_ui(backend_of(synced), scenario)


def test_goto_something_without_a_workspace_says_so(synced):
    async def scenario(ui):
        ui.app.goto(Match("report", "rep-0001", "Report 1"))
        await ui.settle()
        return [n.message for n in ui.app._notifications]

    notes = run_ui(backend_of(synced), scenario)

    assert any("does not say which workspace" in n for n in notes)


# ---------------------------------------------------------------------------
# reading the lake again, and fetching again
# ---------------------------------------------------------------------------


def test_reload_reads_what_another_process_synced(tmp_path):
    world = World(tmp_path)
    world.run("groups")

    async def scenario(ui):
        before = ui.tree()
        world.run("reports", "apps")  # as a nightly `pbi sync run` would
        await ui.press("l")
        return before, ui.tree()

    before, after = run_ui(backend_of(world), scenario)

    assert not any("Apps" in line for line in before)
    assert "● Apps  3" in after


def test_refresh_of_a_workspace_scans_it_after_asking(synced):
    synced.clock.advance(hours=5)
    synced.fake.reset_calls()

    async def scenario(ui):
        await ui.select("workspace", "ws-0002")
        await ui.press("r")
        asked = type(ui.app.screen).__name__
        text = plain(ui.app.screen.query_one("#dialog-body Static").content)
        await ui.press("enter")
        await ui.finish_sync()
        return asked, text, ui.app.run_state

    asked, text, state = run_ui(backend_of(synced), scenario)

    assert asked == "ConfirmModal"
    assert "Scan Workspace 2" in text and "scan: 1 to do, 5 request(s)" in text
    assert "admin.scan.start: 1 request(s) of the quota (500/h, 16 concurrent)" in text
    assert "Everything fits the quota now." in text
    assert state is not None and state.label == "Scan Workspace 2"
    assert state.report.counts["done"] == 1
    (call,) = synced.fake.calls_to("getInfo", "POST")
    assert call.body["workspaces"] == ["ws-0002"]
    assert call.query["lineage"] == "true" and call.query["getArtifactUsers"] == "true"


def test_a_refresh_inside_a_workspace_scans_it_whatever_row_is_selected(synced):
    synced.clock.advance(hours=5)
    synced.fake.reset_calls()

    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        await ui.pick_row("app:app-0001")  # apps are not in scans: it is the workspace
        await ui.press("r")
        await ui.press("enter")
        await ui.finish_sync()
        return ui.app.run_state.label

    assert run_ui(backend_of(synced), scenario) == "Scan Workspace 1"
    (call,) = synced.fake.calls_to("getInfo", "POST")
    assert call.body["workspaces"] == ["ws-0001"]


def test_a_refresh_from_the_list_of_workspaces_scans_the_one_that_is_picked(synced):
    synced.clock.advance(hours=5)
    synced.fake.reset_calls()

    async def scenario(ui):
        await ui.select("workspaces")
        await ui.pick_row("ws-0003")
        await ui.press("r")
        await ui.press("enter")
        await ui.finish_sync()
        return ui.app.run_state.label

    assert run_ui(backend_of(synced), scenario) == "Scan Workspace 3"
    (call,) = synced.fake.calls_to("getInfo", "POST")
    assert call.body["workspaces"] == ["ws-0003"]


@pytest.mark.parametrize(
    "node, label, path",
    [
        (("apps", ""), "Fetch the apps", r"^/admin/apps$"),
        (("capacities", ""), "Fetch the capacities", r"^/admin/capacities$"),
        (("activity", ""), "Fetch the audit events", r"^/admin/activityevents$"),
        (
            ("activity", "2026-09-29"),
            "Fetch the audit events",
            r"^/admin/activityevents$",
        ),
        (("root", ""), "Fetch the lists of the tenant", r"^/admin/reports$"),
    ],
)
def test_refresh_fetches_what_the_node_is_made_of(synced, node, label, path):
    synced.clock.advance(hours=2)
    synced.fake.reset_calls()

    async def scenario(ui):
        await ui.select(*node)
        await ui.press("r")
        await ui.press("enter")
        await ui.finish_sync()
        return ui.app.run_state.label

    assert run_ui(backend_of(synced), scenario) == label
    assert synced.fake.count(path) >= 1


def test_enter_opens_and_closes_a_branch_of_the_tree(synced):
    async def scenario(ui):
        tree = ui.explorer.query_one("#tree", Tree)
        node = ui.explorer._tree_nodes[NodeRef("activity")]
        tree.get_node_at_line(0)
        tree.move_cursor(node)
        await ui.settle()
        tree.focus()
        closed = node.is_expanded
        await ui.press("enter")
        opened = node.is_expanded
        await ui.press("enter")
        return closed, opened, node.is_expanded

    assert run_ui(backend_of(synced), scenario) == (False, True, False)


def test_refresh_can_be_cancelled(synced):
    synced.fake.reset_calls()

    async def scenario(ui):
        await ui.select("workspace", "ws-0002")
        await ui.press("r")
        await ui.press("escape")
        return ui.app.run_state

    assert run_ui(backend_of(synced), scenario) is None
    assert synced.fake.calls == []


def test_refresh_of_the_lists_fetches_them_again(synced):
    synced.clock.advance(hours=2)
    synced.fake.reset_calls()

    async def scenario(ui):
        await ui.select("workspaces")
        await ui.press("r")
        await ui.press("enter")
        return ui.app.run_state

    state = run_ui(backend_of(synced), scenario)

    assert state.label == "Fetch the lists of the tenant"
    assert (
        synced.fake.count(r"^/admin/groups$") == 1
        and synced.fake.count(r"^/admin/reports$") == 1
    )


def test_refresh_without_credentials_asks_to_sign_in(synced):
    def client_for(scope):
        raise AuthError("No active profile set for group 'admin'.")

    async def scenario(ui):
        await ui.select("apps")
        await ui.press("r")
        return (
            type(ui.app.screen).__name__,
            ui.app.screen.query_one("#dialog-reason").content,
        )

    screen, reason = run_ui(backend_of(synced, client_for=client_for), scenario)

    assert screen == "SignInModal" and "No active profile" in str(reason)


def test_a_token_that_expires_while_refreshing_asks_to_sign_in_and_goes_on(synced):
    synced.clock.advance(hours=5)
    synced.fake.expire_token_in(
        1
    )  # the list of modified workspaces goes through, then 401
    signed = []

    def sign_in(token, profile, group):
        signed.append((token, profile, group))
        synced.fake.expire_token_after(None)

    async def scenario(ui):
        await ui.select("workspace", "ws-0002")
        await ui.press("r")
        await ui.press("enter")
        modal = type(ui.app.screen).__name__
        await ui.type("abc.def.ghi")
        await ui.press("enter")
        return modal, ui.app.run_state

    modal, state = run_ui(backend_of(synced, sign_in=sign_in), scenario)

    assert modal == "SignInModal"
    assert signed == [("abc.def.ghi", "admin-nlm", "admin")]
    assert state.report.status == "completed"  # it went on after signing in
