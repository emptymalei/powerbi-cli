"""The Sync screen: the plan, running a sync, stopping it, and what goes wrong."""

import threading

import pytest
from sync_helpers import World
from textual.widgets import Button, Input, Label, SelectionList
from tui_helpers import backend_of, plain, run_ui

from pbi_cli.core.sync import runners
from pbi_cli.core.sync.plan import SNAPSHOT
from pbi_cli.core.sync.targets import TARGETS
from pbi_cli.errors import PBIError


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


async def open_sync(ui):
    """Open the screen and wait for its first plan."""
    await ui.press("s")
    await ui.until(lambda: ui.sync.plans > 0)
    await ui.settle()


async def replan(ui, change=None):
    """Make a change, and wait for the plan that follows from it."""
    shown = ui.sync.plans
    if change is not None:
        change()
    await ui.until(lambda: ui.sync.plans > shown)
    await ui.settle()


def targets(ui):
    return ui.sync.query_one("#targets", SelectionList)


def set_value(ui, selector, value):
    def change():
        ui.sync.query_one(selector).value = value

    return change


def holding_up(monkeypatch):
    """Make every unit of a sync wait for the test: returns (entered, release)."""
    entered, release = threading.Event(), threading.Event()
    real = runners.RUNNERS[SNAPSHOT]

    def slow(ctx, unit):
        entered.set()
        release.wait(10)
        return real(ctx, unit)

    monkeypatch.setitem(runners.RUNNERS, SNAPSHOT, slow)
    return entered, release


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------


def test_the_screen_opens_with_the_plan_of_a_plain_sync(world):
    async def scenario(ui):
        await open_sync(ui)
        return (
            type(ui.app.screen).__name__,
            ui.rows("#plan"),
            ui.rows("#quota"),
            ui.static("#plan-notes"),
            ui.static("#plan-head"),
            list(targets(ui).selected),
        )

    screen, plan, quota, notes, head, selected = run_ui(backend_of(world), scenario)

    assert screen == "SyncScreen"
    assert selected == [
        "groups",
        "apps",
        "capacities",
        "reports",
        "datasets",
        "dashboards",
        "dataflows",
    ]
    assert plan[:2] == [
        ["groups", "admin.groups", "1", "0", "1", "1"],
        ["apps", "admin.apps", "1", "0", "1", "1"],
    ]
    assert len(plan) == 7
    assert ["admin.groups", "1", "50/h, 15/min", "15"] in quota
    assert "Everything fits the quota now" in notes
    assert "tenant-1" in head


def test_the_plan_follows_the_targets_and_the_options(world):
    world.run()

    async def scenario(ui):
        await open_sync(ui)
        before = ui.static("#plan-notes")
        await replan(ui, lambda: targets(ui).select("activity"))
        with_events = [r[0] for r in ui.rows("#plan")]
        days = ui.rows("#plan")[-1]
        await replan(ui, set_value(ui, "#days", "3"))
        three_days = ui.rows("#plan")[-1]
        await replan(ui, set_value(ui, "#force", True))
        forced = ui.rows("#plan")[0]
        return before, with_events, days, three_days, forced

    before, with_events, days, three_days, forced = run_ui(backend_of(world), scenario)

    assert "Nothing to fetch" in before  # the lake holds everything fresh
    assert with_events[-1] == "activity"
    assert days[2] == "28" and three_days[2] == "3"
    assert forced[3] == "0" and forced[4] == "1"  # forced: nothing counts as fresh


def test_the_heading_says_what_the_warning_sign_means(world):
    async def scenario(ui):
        await open_sync(ui)
        return plain(ui.sync.query_one(".heading", Label).content), ui.static(
            "#targets-note"
        )

    heading, note = run_ui(backend_of(world), scenario)

    assert "⚠ copies personal data or queries" in heading
    assert (
        note == "groups: Workspaces"
    )  # the first target is the one that is highlighted


def test_a_note_says_what_a_sensitive_target_copies(world):
    async def scenario(ui):
        await open_sync(ui)
        targets(ui).highlighted = [t.name for t in TARGETS].index("scan")
        await ui.settle()
        return ui.static("#targets-note")

    note = run_ui(backend_of(world), scenario)

    assert "scan" in note and "Copies" in note and "contents of every workspace" in note


def test_nothing_selected_and_bad_numbers_are_said_not_planned(world):
    async def scenario(ui):
        await open_sync(ui)
        await replan(ui, lambda: targets(ui).deselect_all())
        nothing = ui.static("#plan-head"), ui.sync.query_one("#run", Button).disabled

        def zero():
            targets(ui).select_all()
            ui.sync.query_one("#days", Input).value = "0"

        await replan(ui, zero)
        days = ui.static("#plan-head"), ui.sync.query_one("#run", Button).disabled

        def many():
            ui.sync.query_one("#days", Input).value = "28"
            ui.sync.query_one("#workers", Input).value = "99"

        await replan(ui, many)
        return nothing, days, ui.static("#plan-head")

    nothing, days, workers = run_ui(backend_of(world), scenario)

    assert nothing == ("Select at least one target.", True)
    assert days[0] == "Days of audit events must be a number from 1 to 28" and days[1]
    assert "Requests at once must be a number from 1 to 16" in workers


def test_a_plan_that_cannot_be_made_says_why(world):
    def client_for(scope):
        raise PBIError("No active profile set for group 'user'.")

    async def scenario(ui):
        await open_sync(ui)
        await replan(ui, lambda: targets(ui).select("user-groups"))
        return ui.static("#plan-head"), ui.sync.query_one("#run", Button).disabled

    text, disabled = run_ui(backend_of(world, client_for=client_for), scenario)

    assert "No account is stored" in text and disabled


def test_what_does_not_fit_the_quota_is_marked_and_explained(world):
    for n in range(15):  # the per-minute quota of the workspace list is used up
        world.admin.request("admin.groups", {"$top": n + 1})

    async def scenario(ui):
        await open_sync(ui)
        return ui.rows("#quota"), ui.static("#plan-notes")

    quota, notes = run_ui(backend_of(world), scenario)

    assert ["admin.groups", "1", "50/h, 15/min", "0"] in quota
    assert "admin.groups needs 1 requests and 0 fit now" in notes


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------


def test_a_sync_runs_and_is_reported(world):
    async def scenario(ui):
        await open_sync(ui)
        await ui.click("#run")
        tab = ui.sync.query_one("#sync-tabs").active
        await ui.finish_sync()
        await ui.until(lambda: "Nothing to fetch" in ui.static("#plan-notes"))
        return (
            tab,
            ui.log(),
            ui.static("#run-line"),
            ui.app.run_state.report,
            ui.rows("#holdings"),
            ui.static("#lake-lines"),
            ui.sync.query_one("#run", Button).disabled,
            ui.tree(),
        )

    tab, log, summary, report, holdings, lines, disabled, tree = run_ui(
        backend_of(world), scenario
    )

    assert tab == "tab-run"
    assert "Stage 1: 7 unit(s) of groups, apps, capacities" in log
    assert "✓ admin.groups  12 rows" in log
    assert "Finished in" in log and "7 fetched" in log
    assert report.status == "completed" and report.counts["done"] == 7
    assert "finished in" in summary and "7 fetched" in summary
    assert [r[0] for r in holdings][:2] == ["groups", "apps"]
    assert "Last sync" in lines and "completed" in lines
    assert disabled is False
    assert tree[0] == "● Workspaces  12"  # and the Explorer has read the lake again


def test_the_screen_and_the_header_do_not_fail_when_they_are_taken_apart(world):
    """The app closes with timers still running: one that fires after the widgets it draws
    on are gone must not end in an error (it did, now and then, when the app was quit).
    """

    async def scenario(ui):
        await open_sync(ui)
        await ui.click("#run")
        await ui.finish_sync()
        await ui.sync.query_one("#progress").remove()
        await ui.app.screen.query_one("#who").remove()
        ui.sync._tick()
        ui.app.screen.query_one("StatusBar").refresh_status()
        return "no error"

    assert run_ui(backend_of(world), scenario) == "no error"


def test_a_failed_unit_is_reported_and_listed(world):
    world.fake.fail("GET", r"^/admin/apps$", 500)

    async def scenario(ui):
        await open_sync(ui)
        await ui.click("#run")
        await ui.finish_sync()
        return (
            ui.log(),
            ui.static("#lake-lines"),
            [n.message for n in ui.app._notifications],
        )

    log, lines, notes = run_ui(backend_of(world), scenario)

    assert "✗ admin.apps:" in log
    assert "failed unit(s)" in lines and "admin.apps" in lines
    assert any("failure" in n for n in notes)


def test_a_second_sync_is_not_started_while_one_runs(world, monkeypatch):
    entered, release = holding_up(monkeypatch)

    async def scenario(ui):
        await open_sync(ui)
        await ui.click("#run")
        await ui.until(entered.is_set)
        await ui.until(lambda: ui.sync.query_one("#run", Button).disabled)
        first = ui.app.run_state
        second = ui.app.start_sync(first.options, "again")
        release.set()
        await ui.finish_sync()
        return (
            second,
            ui.app.run_state is first,
            [n.message for n in ui.app._notifications],
        )

    second, same, notes = run_ui(backend_of(world), scenario)

    assert second is False and same
    assert "A sync is already running." in notes


def test_stop_ends_the_run_and_what_is_done_is_kept(world, monkeypatch):
    entered, release = holding_up(monkeypatch)

    async def scenario(ui):
        await open_sync(ui)
        await replan(ui, set_value(ui, "#workers", "1"))
        await ui.click("#run")
        await ui.until(entered.is_set)
        await ui.until(lambda: not ui.sync.query_one("#stop", Button).disabled)
        await ui.click("#stop")
        stopping = ui.app.run_state.stopping
        stop_disabled_while_stopping = ui.sync.query_one("#stop", Button).disabled
        release.set()
        await ui.finish_sync()
        return (
            stopping,
            stop_disabled_while_stopping,
            ui.app.run_state.report,
            ui.log(),
            ui.sync.query_one("#stop", Button).disabled,
            ui.sync.query_one("#run", Button).disabled,
        )

    stopping, stop_disabled, report, log, stop_off, run_disabled = run_ui(
        backend_of(world), scenario
    )

    assert stopping and stop_disabled
    assert report.status == "interrupted" and report.cancelled >= 1
    flat = " ".join(log.split())  # the log wraps long lines
    assert "Stopping: the requests in flight finish first" in flat
    assert "Stopped:" in flat and "run the sync again to continue" in flat
    assert stop_off and not run_disabled
    assert world.stored("admin.groups") is not None  # what finished is in the lake


def test_a_token_that_expires_asks_to_sign_in_and_the_sync_goes_on(world):
    world.fake.expire_token_in(2)
    signed = []

    def sign_in(token, profile, group):
        signed.append((token, profile, group))
        world.fake.expire_token_after(None)

    async def scenario(ui):
        await open_sync(ui)
        await ui.click("#run")
        await ui.finish_sync()
        modal = type(ui.app.screen).__name__
        reason = str(ui.app.screen.query_one("#dialog-reason").content)
        first = ui.app.run_state
        await ui.type("tok.en.value")
        await ui.press("enter")
        await ui.until(lambda: ui.app.run_state is not first)
        await ui.finish_sync()
        return modal, reason, first.report.status, ui.app.run_state.report.status

    modal, reason, first, second = run_ui(backend_of(world, sign_in=sign_in), scenario)

    assert modal == "SignInModal" and "token" in reason.lower()
    assert signed == [("tok.en.value", "admin-nlm", "admin")]
    assert first == "token_expired"
    assert second == "completed"  # signing in started the sync again, and it went on


def test_escape_goes_back_to_the_explorer_and_the_run_goes_on(world, monkeypatch):
    entered, release = holding_up(monkeypatch)

    async def scenario(ui):
        await open_sync(ui)
        await ui.click("#run")
        await ui.until(entered.is_set)
        await ui.press("escape")
        back = type(ui.app.screen).__name__
        await ui.until(lambda: "Sync: stage 1" in ui.static("#busy"))
        busy = ui.static("#busy")
        release.set()
        await ui.finish_sync()
        await ui.press("s")
        await ui.until(lambda: "Finished in" in ui.log())
        return back, busy, ui.log(), ui.app.run_state.running

    back, busy, log, running = run_ui(backend_of(world), scenario)

    assert back == "ExplorerScreen"
    assert "Sync: stage 1" in busy  # the header says that something is running
    assert (
        "Finished in" in log and not running
    )  # and the screen shows it again when opened
