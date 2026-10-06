"""The terminal UI with a plan file: its Sync screen, a run of its steps, and its session."""

import pytest
from sync_helpers import World
from textual.widgets import Button
from tui_helpers import backend_of, run_ui

from pbi_cli.core.planfile import PlanFile
from pbi_cli.core.store import LakeStore
from pbi_cli.tui.commands import commands_for
from pbi_cli.tui.explorer import NodeRef
from pbi_cli.tui.modals import SignInModal
from pbi_cli.tui.planscreen import PlanSyncScreen
from pbi_cli.tui.syncscreen import SyncScreen

PLAN = """\
version: 1
accounts: {admin: admin-nlm}
tenant: {targets: [groups, reports, datasets, dashboards, dataflows]}
workspaces:
  - {name: "Workspace 2", details: [users], via: admin}
session: {open: "Workspace 3", lazy: ask}
"""


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def planned(world, tmp_path, text=PLAN, **replace):
    """A backend with a plan file that can be read again."""
    path = tmp_path / "pbi-plan.yaml"
    path.write_text(text, encoding="utf-8")
    return backend_of(
        world,
        plan=PlanFile.load(path),
        reload_plan=lambda: PlanFile.load(path),
        **replace,
    )


def write(tmp_path, text):
    (tmp_path / "pbi-plan.yaml").write_text(text, encoding="utf-8")


async def open_sync(ui):
    await ui.press("s")
    await ui.until(lambda: ui.sync.plans > 0)
    await ui.settle()


def notes(ui):
    return [n.message for n in ui.app._notifications]


# ---------------------------------------------------------------------------
# the screen
# ---------------------------------------------------------------------------


def test_the_sync_screen_shows_the_file_and_the_plan_of_its_steps(world, tmp_path):
    world.run("groups")

    async def scenario(ui):
        await open_sync(ui)
        screen = ui.app.screen
        return (
            type(screen).__name__,
            ui.static("#plan-file", screen),
            ui.columns("#plan", screen),
            ui.rows("#plan", screen),
            ui.rows("#quota", screen),
            ui.static("#plan-head", screen),
            ui.static("#plan-notes", screen),
            len(screen.query("#targets")),
            str(screen.query_one("#run", Button).label),
        )

    kind, summary, columns, rows, quota, head, notes_text, lists, button = run_ui(
        planned(world, tmp_path), scenario
    )

    assert kind == "PlanSyncScreen"
    assert "pbi-plan.yaml" in summary and "administrator: admin-nlm" in summary
    assert "Workspace 2" in summary and "users; via admin" in summary
    assert "lazy: ask" in summary and "Press l to read the file again." in summary
    assert columns == [
        "Step",
        "Target",
        "Operation",
        "Units",
        "Fresh",
        "To do",
        "Requests",
    ]
    assert rows[0][:3] == ["1", "groups", "admin.groups"]
    assert [r[0] for r in rows if r[0]] == ["1", "2"]  # two steps, each numbered once
    assert any(r[1] == "report-users" for r in rows)
    assert quota and quota[0][0].startswith("admin.")
    assert "Plan file: pbi-plan.yaml" in head and "Steps: 2" in head
    assert "Accounts: admin-nlm (admin)" in head
    assert button == "Run plan"
    assert "step 1: the tenant (admin-nlm)" in notes_text
    assert lists == 0  # the targets and the options are the file's


def test_without_a_plan_file_the_sync_screen_is_the_usual_one(world):
    async def scenario(ui):
        await open_sync(ui)
        screen = ui.app.screen
        return (
            type(screen).__name__,
            len(screen.query("#targets")),
            len(screen.query("#plan-file")),
        )

    kind, targets, summary = run_ui(backend_of(world), scenario)

    assert (kind, targets, summary) == ("SyncScreen", 1, 0)
    assert issubclass(PlanSyncScreen, SyncScreen) and PlanSyncScreen.plan_mode
    assert not SyncScreen.plan_mode


def test_the_plan_before_the_first_sync_says_which_names_cannot_be_looked_up(
    world, tmp_path
):
    async def scenario(ui):
        await open_sync(ui)
        return ui.static("#plan-notes"), ui.rows("#plan")

    text, rows = run_ui(planned(world, tmp_path), scenario)

    assert "no workspace is called 'Workspace 2'" in text
    assert "the lake holds no list of workspaces yet" in text
    assert [r[0] for r in rows if r[0]] == ["1"]  # the workspaces wait for the list


def test_a_plan_that_cannot_be_made_is_shown_as_a_problem(world, tmp_path):
    def nobody(scope, profile=None):
        from pbi_cli.errors import PBIError

        raise PBIError("No profile set")

    world.engine._client_for = nobody

    async def scenario(ui):
        await open_sync(ui)
        head, rows, can_run = ui.static("#plan-head"), ui.rows("#plan"), ui.sync.can_run
        await ui.press("r")  # there is nothing to run: it says why
        return head, rows, can_run, ui.app.run_state, notes(ui)

    head, rows, can_run, state, toasts = run_ui(
        planned(world, tmp_path, "version: 1\ntenant: {}\n"), scenario
    )

    assert "No account is stored" in head
    assert rows == [] and not can_run
    assert state is None
    assert any("No account is stored" in message for message in toasts)


def test_a_lake_that_is_only_looked_at_has_no_plan_to_run(world, tmp_path):
    world.run("groups")
    backend = planned(
        world,
        tmp_path,
        store=LakeStore(world.store.root, readonly=True, reason="The lake only reads."),
    )

    async def scenario(ui):
        await open_sync(ui)
        return ui.static("#plan-head"), ui.sync.can_run, ui.rows("#plan")

    head, can_run, rows = run_ui(backend, scenario)

    assert head.startswith("View only. The lake only reads.")
    assert not can_run and rows == []


# ---------------------------------------------------------------------------
# running it
# ---------------------------------------------------------------------------


def test_run_goes_through_the_steps_and_the_screen_shows_each(world, tmp_path):
    world.run("groups")

    async def scenario(ui):
        await open_sync(ui)
        await ui.press("r")
        tab = ui.sync.query_one("#sync-tabs").active
        await ui.finish_sync()
        return (
            ui.log(),
            ui.app.run_state.label,
            ui.static("#run-line"),
            ui.static("#plan-notes"),
            notes(ui),
            tab,
        )

    log, label, line, plan_notes, toasts, tab = run_ui(
        planned(world, tmp_path), scenario
    )

    assert (
        label == "Plan pbi-plan.yaml" and tab == "tab-run"
    )  # the log is shown at once
    assert "Step 1: the tenant (admin-nlm)" in log
    assert (
        "Step 2: dashboard-users, dataflow-users, dataset-users, group-users, "
        "report-users for 1 workspace (admin-nlm)" in log
    )
    assert log.index("Step 1:") < log.index("Step 2:")
    assert "Plan pbi-plan.yaml: finished" in line
    assert [
        s.params["groupId"]
        for s in world.store.parameter_sets("tenant-1", "admin.groups.users")
    ] == ["ws-0002"]
    assert "Nothing to fetch" in plan_notes  # planned again after the run
    assert any("Plan pbi-plan.yaml: done." in message for message in toasts)


def test_the_run_button_runs_the_plan_too(world, tmp_path):
    world.run("groups")

    async def scenario(ui):
        await open_sync(ui)
        await ui.click("#run")
        await ui.finish_sync()
        return ui.log()

    assert "Step 2:" in run_ui(planned(world, tmp_path), scenario)


def test_the_name_of_the_step_is_in_the_line_of_a_run_that_goes_on(
    world, tmp_path, monkeypatch
):
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
        await open_sync(ui)
        await ui.press("r")
        await ui.until(entered.is_set)
        await ui.until(lambda: "step 1" in ui.static("#run-line"))
        line = ui.static("#run-line")
        stop_enabled = not ui.sync.query_one("#stop", Button).disabled
        release.set()
        await ui.finish_sync()
        return line, stop_enabled

    line, stop_enabled = run_ui(planned(world, tmp_path), scenario)

    assert "Plan pbi-plan.yaml: step 1 (the tenant (admin-nlm)), stage 1" in line
    assert stop_enabled


def test_a_plan_cannot_be_run_twice_at_once(world, tmp_path, monkeypatch):
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
        await open_sync(ui)
        await ui.press("r")
        await ui.until(entered.is_set)
        await ui.press("r")
        refused = "A sync is already running." in notes(ui)
        release.set()
        await ui.finish_sync()
        return refused

    assert run_ui(planned(world, tmp_path), scenario) is True


def test_an_expired_token_of_a_user_asks_for_that_users_token(world, tmp_path):
    world.accounts(ana="oid-ana", bob="oid-bob")
    backend = planned(
        world,
        tmp_path,
        "version: 1\naccounts: {user: [ana, bob]}\ntenant: {targets: [user-groups]}\n",
    )

    async def scenario(ui):
        await open_sync(ui)
        world.fake.expire_token_after(0)
        await ui.press("r")
        await ui.finish_sync()
        await ui.until(lambda: isinstance(ui.app.screen, SignInModal))
        modal = ui.app.screen
        return modal._group, modal._profile, modal.query_one("#profile").value

    group, profile, shown = run_ui(backend, scenario)

    assert (group, profile, shown) == ("user", "ana", "ana")


def test_after_signing_in_the_plan_goes_on(world, tmp_path):
    world.accounts(ana="oid-ana", bob="oid-bob")
    stored = []
    backend = planned(
        world,
        tmp_path,
        "version: 1\naccounts: {user: [ana, bob]}\ntenant: {targets: [user-groups]}\n",
        sign_in=lambda token, profile, group: stored.append((profile, group)),
    )

    async def scenario(ui):
        await open_sync(ui)
        world.fake.expire_token_after(0)
        await ui.press("r")
        await ui.finish_sync()
        await ui.until(lambda: isinstance(ui.app.screen, SignInModal))
        world.fake.expire_token_after(None)
        modal = ui.app.screen
        modal.query_one("#token").value = "fresh-token"
        await ui.press("enter")
        await ui.until(lambda: not isinstance(ui.app.screen, SignInModal))
        await ui.finish_sync()
        return ui.app.run_state.report.status, ui.app.run_state.label

    status, label = run_ui(backend, scenario)

    assert stored == [("ana", "user")]
    assert status == "completed" and label == "Plan pbi-plan.yaml"
    assert len(world.store.parameter_sets("tenant-1", "user.groups")) == 2


# ---------------------------------------------------------------------------
# reading the file again
# ---------------------------------------------------------------------------


def test_l_reads_the_file_again_and_plans_it_again(world, tmp_path):
    world.run("groups")

    async def scenario(ui):
        await open_sync(ui)
        before = ui.static("#plan-file"), ui.rows("#plan")
        write(
            tmp_path,
            PLAN.replace(
                "session:",
                "  - {name: 'Workspace 3', details: [datasources], via: admin}\nsession:",
            ),
        )
        shown = ui.sync.plans
        await ui.press("l")
        await ui.until(lambda: ui.sync.plans > shown)
        await ui.settle()
        after = ui.static("#plan-file"), ui.rows("#plan")
        return before, after, notes(ui)

    before, after, toasts = run_ui(planned(world, tmp_path), scenario)

    assert "Workspace 3" not in before[0].split("Session")[0]
    assert "Workspace 3" in after[0].split("Session")[0]
    assert len(after[1]) > len(before[1])
    assert "Read the plan file again." in toasts


def test_a_file_that_is_wrong_is_said_and_the_one_in_use_stays(world, tmp_path):
    world.run("groups")

    async def scenario(ui):
        await open_sync(ui)
        before = ui.static("#plan-file"), ui.rows("#plan")
        write(tmp_path, "version: 1\nworkspace: []\n")
        await ui.press("l")
        await ui.settle()
        return before, (ui.static("#plan-file"), ui.rows("#plan")), notes(ui)

    before, after, toasts = run_ui(planned(world, tmp_path), scenario)

    assert before == after
    assert any(
        "pbi-plan.yaml:2: workspace: unknown key" in message for message in toasts
    )


def test_a_session_without_a_plan_file_cannot_read_one_again(world):
    async def scenario(ui):
        await open_sync(ui)
        await ui.press("l")
        return notes(ui)

    assert "Read the plan file again." not in run_ui(backend_of(world), scenario)


# ---------------------------------------------------------------------------
# the palette
# ---------------------------------------------------------------------------


def test_the_palette_offers_to_run_the_plan_and_to_read_the_file_again(world, tmp_path):
    world.run("groups")

    async def scenario(ui):
        await open_sync(ui)
        listed = {c.title: c for c in commands_for(ui.app, ui.app.screen)}
        await ui.pilot.pause()
        return list(listed), listed["Run the plan"].help, listed

    titles, help_text, listed = run_ui(planned(world, tmp_path), scenario)

    assert "Run the plan" in titles and "Run the sync" not in titles
    assert "Read the plan file again" in titles
    assert help_text.endswith("key r")
    assert listed["Read the plan file again"].help.endswith("key l")


def test_the_usual_palette_has_no_plan_commands(world):
    world.run("groups")

    async def scenario(ui):
        await open_sync(ui)
        return [c.title for c in commands_for(ui.app, ui.app.screen)]

    titles = run_ui(backend_of(world), scenario)

    assert "Run the sync" in titles
    assert "Run the plan" not in titles and "Read the plan file again" not in titles


def test_the_command_to_read_the_file_again_does(world, tmp_path):
    world.run("groups")

    async def scenario(ui):
        await open_sync(ui)
        command = next(
            c
            for c in commands_for(ui.app, ui.app.screen)
            if c.title == "Read the plan file again"
        )
        shown = ui.sync.plans
        command.run()
        await ui.until(lambda: ui.sync.plans > shown)
        return notes(ui)

    assert "Read the plan file again." in run_ui(planned(world, tmp_path), scenario)


# ---------------------------------------------------------------------------
# the session
# ---------------------------------------------------------------------------


def test_the_workspace_of_the_session_is_selected_at_the_start(world, tmp_path):
    world.run("groups")

    async def scenario(ui):
        return ui.explorer._ref, ui.explorer._subject

    ref, subject = run_ui(planned(world, tmp_path), scenario)

    assert ref == NodeRef("workspace", "ws-0003")
    assert subject.name == "Workspace 3"


def test_a_name_pattern_opens_the_first_match_and_an_id_that_one(world, tmp_path):
    world.run("groups")

    def opened(text):
        async def scenario(ui):
            return ui.explorer._ref

        return run_ui(planned(world, tmp_path, text), scenario)

    pattern = opened("version: 1\nsession: {open: 'Workspace 1?'}\n")
    by_id = opened("version: 1\nsession: {open: ws-0005}\n")

    assert pattern == NodeRef("workspace", "ws-0010")  # the first of 10, 11, 12 by name
    assert by_id == NodeRef("workspace", "ws-0005")


def test_a_workspace_that_is_not_there_is_said_and_nothing_is_selected(world, tmp_path):
    world.run("groups")

    async def scenario(ui):
        return ui.explorer._ref, notes(ui)

    ref, toasts = run_ui(
        planned(world, tmp_path, "version: 1\nsession: {open: Nothing*}\n"), scenario
    )

    assert ref == NodeRef("root")
    assert any("No workspace of the lake matches 'Nothing*'" in m for m in toasts)


def test_no_workspace_is_opened_before_the_lake_is_read(world, tmp_path):
    world.run("groups")

    async def scenario(ui):
        catalog = ui.app.catalog
        ui.app._opened = False  # as if nothing was opened yet
        ui.app.catalog = None  # and the lake were still being read
        early = ui.app.workspace_to_open()
        still_wanted = not ui.app._opened
        ui.app.catalog = catalog
        later = ui.app.workspace_to_open()  # the lake is read: now it is asked for
        return early, still_wanted, later, ui.app._opened

    early, still_wanted, later, opened = run_ui(
        planned(world, tmp_path, "version: 1\nsession: {open: ws-0005}\n"), scenario
    )

    assert early is None and still_wanted
    assert later == "ws-0005" and opened


def test_the_session_selects_the_workspace_once_only(world, tmp_path):
    world.run("groups")

    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        ui.app.reload_catalog()
        await ui.settle()
        await ui.settle()
        return ui.explorer._ref

    assert run_ui(planned(world, tmp_path), scenario) == NodeRef("workspace", "ws-0001")


def test_without_a_session_nothing_is_selected_at_the_start(world, tmp_path):
    world.run("groups")

    async def scenario(ui):
        return ui.explorer._ref

    assert run_ui(planned(world, tmp_path, "version: 1\n"), scenario) == NodeRef("root")
    assert run_ui(backend_of(world), scenario) == NodeRef("root")
