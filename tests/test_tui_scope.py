"""The Explorer of a session with a plan file: the workspaces the file names, and a key for all."""

import pytest
from sync_helpers import World
from tui_helpers import backend_of, plain, run_ui

from pbi_cli.core.catalog import Match
from pbi_cli.core.planfile import PlanFile
from pbi_cli.tui.commands import commands_for
from pbi_cli.tui.palette import GotoProvider

PLAN = """\
version: 1
accounts: {admin: admin-nlm}
workspaces:
  - {name: "Workspace 2", details: [users], via: admin}
"""

SHOWN = (
    "Showing the 1 workspace(s) of the plan file, of 12. Press w for every workspace."
)
EVERYONE = "Showing every workspace (12). Press w for those of the plan file."
NO_PLAN = "There is no plan file that names workspaces: every workspace is shown."


@pytest.fixture
def world(tmp_path):
    world = World(tmp_path)
    world.fake.workspaces[11]["type"] = "PersonalGroup"  # Workspace 12 is somebody's
    world.run("groups")
    return world


def planned(world, tmp_path, text=PLAN):
    path = tmp_path / "pbi-plan.yaml"
    path.write_text(text, encoding="utf-8")
    return backend_of(
        world, plan=PlanFile.load(path), reload_plan=lambda: PlanFile.load(path)
    )


def notes(ui):
    return [n.message for n in ui.app._notifications]


def workspaces_in_the_tree(ui):
    """The names under the Workspaces node of the tree (not the personal ones)."""
    found, under = [], False
    for line in ui.tree():
        if not line.startswith(" "):
            under = line.startswith("● Workspaces")
        elif under:
            found.append(line.strip(" ●"))
    return found


def title(ui):
    return plain(ui.explorer.query_one("#table-title").content)


# ---------------------------------------------------------------------------
# what is shown
# ---------------------------------------------------------------------------


def test_the_workspaces_of_the_plan_are_all_that_is_shown_at_first(world, tmp_path):
    async def scenario(ui):
        await ui.select("workspaces")
        return ui.tree(), ui.rows(), title(ui)

    tree, rows, heading = run_ui(planned(world, tmp_path), scenario)

    assert tree[:2] == ["● Workspaces  1 of 11", "  ● Workspace 2"]
    assert [row[1] for row in rows] == ["Workspace 2"]
    assert (
        heading
        == "Workspaces: 1 of 11  ·  only those of the plan file: press w for every workspace"
    )


def test_w_shows_every_workspace_and_then_the_plans_again(world, tmp_path):
    async def scenario(ui):
        await ui.press("w")
        every = (ui.tree()[0], len(workspaces_in_the_tree(ui)))
        await ui.press("w")
        again = (ui.tree()[0], len(workspaces_in_the_tree(ui)))
        return every, again, notes(ui)

    every, again, said = run_ui(planned(world, tmp_path), scenario)

    assert every == ("● Workspaces  11", 11)
    assert again == ("● Workspaces  1 of 11", 1)
    assert said[-2:] == [EVERYONE, SHOWN]


def test_the_session_can_start_with_every_workspace(world, tmp_path):
    text = PLAN + "session: {workspaces: all}\n"

    async def scenario(ui):
        return ui.tree()[0], ui.app.show_all_workspaces

    assert run_ui(planned(world, tmp_path, text), scenario) == (
        "● Workspaces  11",
        True,
    )


def test_a_plan_that_names_no_workspace_shows_every_one_and_w_says_why(world, tmp_path):
    text = "version: 1\ntenant: {targets: [groups]}\n"

    async def scenario(ui):
        await ui.press("w")
        return ui.tree()[0], notes(ui)

    first, said = run_ui(planned(world, tmp_path, text), scenario)

    assert first == "● Workspaces  11" and said[-1] == NO_PLAN


def test_without_a_plan_w_says_so_and_every_workspace_is_shown(world):
    async def scenario(ui):
        await ui.press("w")
        return ui.tree()[0], notes(ui)

    first, said = run_ui(backend_of(world), scenario)

    assert first == "● Workspaces  11" and said[-1] == NO_PLAN


def test_the_personal_workspaces_are_listed_only_when_the_plan_names_them(
    world, tmp_path
):
    named = PLAN + "  - {id: ws-0012, details: [users], via: admin}\n"

    async def scenario(ui):
        return ui.tree()

    without = run_ui(planned(world, tmp_path), scenario)
    with_it = run_ui(planned(world, tmp_path, named), scenario)

    assert [line for line in without if "Personal" in line] == []
    assert [line for line in with_it if "Personal" in line] == [
        "● Personal workspaces  1 of 1"
    ]


def test_a_workspace_the_plan_names_and_the_lake_lacks_leaves_an_empty_node(
    world, tmp_path
):
    text = "version: 1\nworkspaces:\n  - {id: elsewhere, details: [users]}\n"

    async def scenario(ui):
        await ui.select("workspaces")
        return ui.tree(), ui.rows(), title(ui)

    tree, rows, heading = run_ui(planned(world, tmp_path, text), scenario)

    assert tree == ["● Workspaces  0 of 11"] and rows == []
    assert heading.startswith("Workspaces: 0 of 11  ·  only those of the plan file")


def test_the_scope_follows_the_lake_when_it_is_read_again(world, tmp_path):
    text = 'version: 1\nworkspaces:\n  - {name: "Workspace 1?", details: [users]}\n'

    async def scenario(ui):
        before = ui.tree()[0]
        world.fake.workspaces.append(
            {**world.fake.workspaces[0], "id": "ws-0013", "name": "Workspace 13"}
        )
        world.run("groups", force=True)
        await ui.press("l")
        await ui.settle()
        return before, ui.tree()[0]

    before, after = run_ui(planned(world, tmp_path, text), scenario)

    assert before == "● Workspaces  2 of 11"  # 10 and 11: 12 is personal
    assert after == "● Workspaces  3 of 12"  # and the new 13


# ---------------------------------------------------------------------------
# the palette
# ---------------------------------------------------------------------------


def toggle_title(ui):
    found = [
        c for c in commands_for(ui.app, ui.app.screen) if "workspace" in c.title.lower()
    ]
    return [(c.title, c.help) for c in found if c.title.startswith("Show ")]


def test_the_palette_offers_the_other_way_to_see_the_workspaces(world, tmp_path):
    async def scenario(ui):
        scoped = toggle_title(ui)
        await ui.press("w")
        everyone = toggle_title(ui)
        return scoped, everyone

    scoped, everyone = run_ui(planned(world, tmp_path), scenario)

    assert scoped == [
        (
            "Show every workspace",
            "The tree and the table list all of the lake's  ·  key w",
        )
    ]
    assert everyone == [
        (
            "Show only the workspaces of the plan file",
            "The tree and the table list the ones the file names  ·  key w",
        )
    ]


def test_the_palette_offers_nothing_about_workspaces_without_a_plan_that_names_them(
    world, tmp_path
):
    async def scenario(ui):
        return toggle_title(ui)

    assert run_ui(backend_of(world), scenario) == []
    text = "version: 1\ntenant: {targets: [groups]}\n"
    assert run_ui(planned(world, tmp_path, text), scenario) == []


def test_the_command_of_the_palette_switches_the_workspaces(world, tmp_path):
    async def scenario(ui):
        command = next(
            c
            for c in commands_for(ui.app, ui.app.screen)
            if c.title == "Show every workspace"
        )
        command.run()
        await ui.settle()
        return ui.tree()[0]

    assert run_ui(planned(world, tmp_path), scenario) == "● Workspaces  11"


def test_the_search_finds_only_the_workspaces_of_the_plan_while_only_those_are_shown(
    world, tmp_path
):
    async def hits(ui):
        provider = GotoProvider(ui.app.screen)
        return [plain(h.text) async for h in provider.search("workspace")]

    async def scenario(ui):
        scoped = await hits(ui)
        await ui.press("w")
        return scoped, await hits(ui)

    scoped, everyone = run_ui(planned(world, tmp_path), scenario)

    assert [h for h in scoped if h.startswith("Workspace:")] == [
        "Workspace: Workspace 2"
    ]
    assert len([h for h in everyone if h.startswith("Workspace:")]) == 12


def test_the_search_keeps_the_items_that_are_in_no_workspace(world, tmp_path):
    world.fake.apps[2].pop("workspaceId")  # App 3 belongs to no workspace
    world.run("apps")

    async def scenario(ui):
        provider = GotoProvider(ui.app.screen)
        return [plain(h.text) async for h in provider.search("app")]

    found = run_ui(planned(world, tmp_path), scenario)

    # App 1 is in a workspace the plan does not name, App 2 in the one it names
    assert [h for h in found if h.startswith("App:")] == [
        "App: App 2  in Workspace 2",
        "App: App 3",
    ]


BIG_PLAN = 'version: 1\nworkspaces:\n  - {name: "%s", details: [users]}\n'


def big_world(tmp_path):
    world = World(tmp_path, workspaces=45)
    world.run("groups")
    return world


async def workspace_hits(ui, query="workspace"):
    provider = GotoProvider(ui.app.screen)
    return [plain(h.text) async for h in provider.search(query)]


def test_the_search_under_a_scope_finds_a_workspace_that_the_plain_search_would_cut(
    tmp_path,
):
    world = big_world(tmp_path)

    async def scenario(ui):
        scoped = await workspace_hits(ui)
        await ui.press("w")
        return scoped, await workspace_hits(ui)

    # "Workspace 9" sorts last of the 45 names: 30 hits never reach it
    scoped, everyone = run_ui(
        planned(world, tmp_path, BIG_PLAN % "Workspace 9"), scenario
    )

    assert scoped == ["Workspace: Workspace 9"]
    assert "Workspace: Workspace 9" not in everyone and len(everyone) == 30


def test_the_search_under_a_scope_shows_no_more_hits_than_the_plain_search(tmp_path):
    world = big_world(tmp_path)

    async def scenario(ui):
        return await workspace_hits(ui)

    found = run_ui(planned(world, tmp_path, BIG_PLAN % "Workspace*"), scenario)

    assert len(found) == 30


# ---------------------------------------------------------------------------
# going to a workspace the plan does not name
# ---------------------------------------------------------------------------


def test_going_to_a_workspace_of_the_plan_keeps_the_scope(world, tmp_path):
    async def scenario(ui):
        ui.app.goto(Match("workspace", "ws-0002", "Workspace 2"))
        await ui.settle()
        return ui.explorer._ref.id, ui.app.show_all_workspaces, notes(ui)

    assert run_ui(planned(world, tmp_path), scenario) == ("ws-0002", False, [])


def test_going_to_another_workspace_shows_every_workspace_and_says_so(world, tmp_path):
    async def scenario(ui):
        ui.app.goto(Match("workspace", "ws-0007", "Workspace 7"))
        await ui.settle()
        return ui.explorer._ref.id, ui.app.show_all_workspaces, ui.tree()[0], notes(ui)

    ref, everyone, first, said = run_ui(planned(world, tmp_path), scenario)

    assert (ref, everyone, first) == ("ws-0007", True, "● Workspaces  11")
    assert said == [EVERYONE]


def test_the_summary_of_the_plan_says_which_workspaces_are_shown(world, tmp_path):
    from pbi_cli.tui import render

    plan = PlanFile.parse(PLAN + "session: {workspaces: all}\n", None)

    assert "  workspaces shown: all\n" in render.plan_file_summary(plan).plain
    assert (
        "workspaces shown"
        not in render.plan_file_summary(
            PlanFile.parse("version: 1\ntenant: {targets: [groups]}\n", None)
        ).plain
    )
