"""``session.lazy`` of a plan file: what the Explorer does about a detail the lake lacks."""

import threading
from dataclasses import replace
from pathlib import Path

import pytest
from sync_helpers import World
from tui_helpers import backend_of, run_ui

from pbi_cli.core.details import AUTO_KEEPS, Detail, Provider, has_margin, providers_of
from pbi_cli.core.registry import Scope, get_endpoint
from pbi_cli.core.store import LakeStore
from pbi_cli.core.sync import runners
from pbi_cli.core.sync.plan import SNAPSHOT
from pbi_cli.core.sync.targets import get_target
from pbi_cli.tui import explorer
from pbi_cli.tui.backend import Backend
from pbi_cli.tui.fetching import Fetching

from pbi_cli.core.planfile import PlanFile  # isort: skip


REAL_AUTO_AFTER = explorer.AUTO_AFTER  # before the tests make it shorter
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def world(tmp_path):
    world = World(tmp_path)
    world.run("groups", "reports", "datasets", "dashboards", "dataflows")
    return world


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    """Do not wait a second and a half to see whether somebody stays."""
    monkeypatch.setattr(explorer, "AUTO_AFTER", 0.05)


def lazy(world, tmp_path, mode, **replace_with) -> Backend:
    path = tmp_path / "pbi-plan.yaml"
    path.write_text(f"version: 1\nsession: {{lazy: {mode}}}\n", encoding="utf-8")
    return backend_of(world, plan=PlanFile.load(path), **replace_with)


async def look_at(ui, workspace, key):
    """Select an item of a workspace, as somebody who stays on it."""
    await ui.select("workspace", workspace)
    await ui.pick_row(key)


def toasts(ui):
    return [n.message for n in ui.app._notifications]


def held(world, endpoint, placeholder):
    return sorted(
        found.params[placeholder]
        for found in world.store.parameter_sets("tenant-1", endpoint)
    )


# ---------------------------------------------------------------------------
# lazy: auto
# ---------------------------------------------------------------------------


def test_auto_fetches_the_harmless_details_of_the_item_you_stay_on(world, tmp_path):
    async def scenario(ui):
        await look_at(ui, "ws-0002", "report:rep-0002")
        await ui.until(lambda: ui.app.run_state is not None)
        await ui.finish_sync()
        return ui.app.run_state.label, toasts(ui)

    label, messages = run_ui(lazy(world, tmp_path, "auto"), scenario)

    assert label == "Fetch the pages of Report 2"
    assert "Fetching the pages of Report 2 ..." in messages
    assert held(world, "user.report_pages", "reportId") == ["rep-0002"]
    assert held(world, "user.report_pages", "groupId") == ["ws-0002"]


def test_the_sensitive_details_are_never_fetched_by_themselves(world, tmp_path):
    async def scenario(ui):
        await look_at(ui, "ws-0002", "dataset:ds-0002")
        await ui.until(lambda: ui.app.run_state is not None)
        await ui.finish_sync()
        return ui.app.run_state.label

    label = run_ui(lazy(world, tmp_path, "auto"), scenario)

    assert label == "Fetch the refreshes of Dataset 2"
    # the way of a user is harmless; the administrator's summary copies who owns it
    assert held(world, "user.dataset_refreshes", "datasetId") == ["ds-0002"]
    assert not world.store.parameter_sets("tenant-1", "admin.refreshables")
    for endpoint in (
        "user.dataset_users",
        "user.dataset_datasources",
        "user.dataset_parameters",
        "admin.datasets.users",
        "admin.datasets.datasources",
    ):
        assert not world.store.parameter_sets("tenant-1", endpoint), endpoint


def test_a_report_gets_its_pages_but_not_its_users(world, tmp_path):
    async def scenario(ui):
        await look_at(ui, "ws-0001", "report:rep-0001")
        await ui.until(lambda: ui.app.run_state is not None)
        await ui.finish_sync()

    run_ui(lazy(world, tmp_path, "auto"), scenario)

    assert not world.store.parameter_sets("tenant-1", "admin.reports.users")


def test_a_workspace_has_nothing_harmless_to_fetch(world, tmp_path):
    async def scenario(ui):
        await ui.select("workspace", "ws-0002")
        await ui.pilot.pause(0.3)
        await ui.settle()
        return ui.app.run_state

    assert run_ui(lazy(world, tmp_path, "auto"), scenario) is None


def test_what_was_fetched_is_shown_for_the_same_row(world, tmp_path):
    async def scenario(ui):
        await look_at(ui, "ws-0002", "report:rep-0002")
        await ui.until(lambda: ui.app.run_state is not None)
        await ui.finish_sync()
        await ui.settle()
        return ui.explorer._subject.name, ui.explorer._ref

    name, ref = run_ui(lazy(world, tmp_path, "auto"), scenario)

    assert name == "Report 2" and ref.id == "ws-0002"


def test_it_does_not_try_a_detail_twice(world, tmp_path):
    world.fake.restricted_datasets.add("ds-0002")  # the user may not read its history

    async def scenario(ui):
        await look_at(ui, "ws-0002", "dataset:ds-0002")
        await ui.until(lambda: ui.app.run_state is not None)
        await ui.finish_sync()
        first = ui.app.run_state
        explorer_screen = ui.explorer
        explorer_screen._auto_look(explorer_screen._serial)
        await ui.settle()
        return first, ui.app.run_state

    first, second = run_ui(lazy(world, tmp_path, "auto"), scenario)

    assert second is first  # no second run


def test_a_look_that_is_out_of_date_does_not_even_work_out_what_to_fetch(
    world, tmp_path, monkeypatch
):
    monkeypatch.setattr(explorer, "AUTO_AFTER", 60)
    chosen = []
    monkeypatch.setattr(
        explorer.ExplorerScreen,
        "_auto_choose",
        lambda self, serial, subject: chosen.append(serial),
    )

    async def scenario(ui):
        await look_at(ui, "ws-0002", "report:rep-0002")
        screen = ui.explorer
        old = screen._serial
        await ui.select("workspace", "ws-0001")
        screen._auto_look(old)
        stale = list(chosen)
        screen._auto_look(screen._serial)
        return stale, list(chosen), screen._serial

    stale, current, serial = run_ui(lazy(world, tmp_path, "auto"), scenario)

    assert stale == [] and current == [serial]


def _wanted(ui):
    """What may be fetched for the item that is shown, as the explorer works it out."""
    subject = ui.explorer._subject
    fetching = ui.app.fetching()
    details = ui.app.catalog.details(subject)
    return subject, fetching.auto(details, ui.app.backend.quota_left), fetching


def test_a_fetch_that_was_chosen_for_an_item_you_left_is_not_started(
    world, tmp_path, monkeypatch
):
    monkeypatch.setattr(explorer, "AUTO_AFTER", 60)

    async def scenario(ui):
        await look_at(ui, "ws-0002", "report:rep-0002")
        subject, wanted, fetching = _wanted(ui)
        stale = ui.explorer._serial - 1
        ui.explorer._auto_start(stale, subject, wanted, fetching)
        await ui.settle()
        after_stale = ui.app.run_state
        ui.explorer._auto_start(ui.explorer._serial, subject, wanted, fetching)
        await ui.until(lambda: ui.app.run_state is not None)
        await ui.finish_sync()
        return after_stale, ui.app.run_state

    after_stale, started = run_ui(lazy(world, tmp_path, "auto"), scenario)

    assert after_stale is None and started is not None


def test_a_detail_that_was_tried_is_not_started_again(world, tmp_path, monkeypatch):
    monkeypatch.setattr(explorer, "AUTO_AFTER", 60)

    async def scenario(ui):
        await look_at(ui, "ws-0002", "report:rep-0002")
        subject, wanted, fetching = _wanted(ui)
        ui.explorer._auto_start(ui.explorer._serial, subject, wanted, fetching)
        await ui.finish_sync()
        await ui.settle()  # the lake was read again: the selection is a new one
        first = ui.app.run_state
        again = ui.explorer._subject
        ui.explorer._auto_start(ui.explorer._serial, again, wanted, fetching)
        await ui.settle()
        return first, ui.app.run_state, ui.explorer._auto_tried

    first, second, tried = run_ui(lazy(world, tmp_path, "auto"), scenario)

    assert second is first  # no second run
    assert tried == {("report", "rep-0002", "pages")}


@pytest.mark.parametrize("mode", ["ask", "off"])
def test_no_timer_is_set_when_nothing_is_fetched_by_itself(
    world, tmp_path, monkeypatch, mode
):
    monkeypatch.setattr(
        explorer, "AUTO_AFTER", 60
    )  # a timer that was set would be there

    async def scenario(ui):
        await look_at(ui, "ws-0002", "report:rep-0002")
        return ui.explorer._auto_timer

    assert run_ui(lazy(world, tmp_path, mode), scenario) is None


def test_a_look_that_is_out_of_date_does_nothing_and_the_current_one_does(
    world, tmp_path, monkeypatch
):
    monkeypatch.setattr(explorer, "AUTO_AFTER", 60)  # the timer never fires here

    async def scenario(ui):
        await look_at(ui, "ws-0002", "report:rep-0002")
        screen = ui.explorer
        old = screen._serial
        await ui.select("workspace", "ws-0001")  # somewhere else: the serial moved on
        screen._auto_look(old)
        await ui.settle()
        stale = ui.app.run_state
        screen._auto_look(screen._serial)  # the first item of that workspace
        await ui.settle()
        return stale, ui.app.run_state

    stale, current = run_ui(lazy(world, tmp_path, "auto"), scenario)

    assert stale is None
    assert current is not None and current.label.startswith("Fetch the ")


def test_nothing_is_waited_for_where_there_is_nothing_to_fetch(
    world, tmp_path, monkeypatch
):
    monkeypatch.setattr(explorer, "AUTO_AFTER", 60)

    async def scenario(ui):
        at_the_root = ui.explorer._auto_timer  # the overview of the lake: not an item
        await look_at(ui, "ws-0002", "report:rep-0002")
        on_an_item = ui.explorer._auto_timer
        return at_the_root, on_an_item

    at_the_root, on_an_item = run_ui(lazy(world, tmp_path, "auto"), scenario)

    assert at_the_root is None and on_an_item is not None


def test_a_new_selection_replaces_the_timer_of_the_old_one(
    world, tmp_path, monkeypatch
):
    monkeypatch.setattr(explorer, "AUTO_AFTER", 60)

    async def scenario(ui):
        await look_at(ui, "ws-0002", "report:rep-0002")
        first = ui.explorer._auto_timer
        stopped = []
        real_stop = first.stop
        first.stop = lambda: (stopped.append(True), real_stop())[1]
        await ui.pick_row("report:rep-0002")
        await ui.select("workspace", "ws-0003")
        return first, ui.explorer._auto_timer, stopped

    first, second, stopped = run_ui(lazy(world, tmp_path, "auto"), scenario)

    assert first is not None and second is not first
    assert stopped == [True]  # the one that was waiting was stopped, not left to fire


def test_it_waits_while_a_sync_runs(world, tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    real = runners.RUNNERS[SNAPSHOT]

    def slow(ctx, unit):
        entered.set()
        release.wait(10)
        return real(ctx, unit)

    monkeypatch.setitem(runners.RUNNERS, SNAPSHOT, slow)

    async def scenario(ui):
        ui.app.start_sync(world.options("apps"), "Sync")
        await ui.until(entered.is_set)
        running = ui.app.run_state
        await look_at(ui, "ws-0002", "report:rep-0002")
        await ui.pilot.pause(0.3)
        await ui.settle()
        same = ui.app.run_state is running
        warned = "A sync is already running." in toasts(ui)
        release.set()
        await ui.finish_sync()
        return same, warned

    same, warned = run_ui(lazy(world, tmp_path, "auto"), scenario)

    assert same and not warned  # it did not even try while the sync ran


def test_a_lake_that_is_only_looked_at_is_never_written_by_itself(world, tmp_path):
    backend = lazy(
        world,
        tmp_path,
        "auto",
        store=LakeStore(world.store.root, readonly=True, reason="The lake only reads."),
    )

    async def scenario(ui):
        await look_at(ui, "ws-0002", "report:rep-0002")
        await ui.pilot.pause(0.3)
        await ui.settle()
        return ui.app.run_state

    assert run_ui(backend, scenario) is None


# ---------------------------------------------------------------------------
# lazy: ask and lazy: off
# ---------------------------------------------------------------------------


def test_ask_fetches_nothing_until_you_press_f(world, tmp_path):
    async def scenario(ui):
        await look_at(ui, "ws-0002", "report:rep-0002")
        await ui.pilot.pause(0.3)
        await ui.settle()
        before = ui.app.run_state
        choice = ui.explorer.fetch_choice()
        return before, choice, ui.explorer._auto_timer

    before, choice, timer = run_ui(lazy(world, tmp_path, "ask"), scenario)

    assert before is None and timer is None  # nothing is even waited for
    assert choice is not None and "pages" in choice[0]


def test_a_session_without_a_plan_file_asks(world):
    async def scenario(ui):
        await look_at(ui, "ws-0002", "report:rep-0002")
        await ui.pilot.pause(0.3)
        await ui.settle()
        return ui.app.run_state, ui.app.lazy

    state, mode = run_ui(backend_of(world), scenario)

    assert state is None and mode == "ask"


def test_off_offers_nothing_and_f_says_why(world, tmp_path):
    async def scenario(ui):
        await look_at(ui, "ws-0002", "report:rep-0002")
        await ui.press("6")
        await ui.settle()
        rows = ui.rows("#details")
        choice = ui.explorer.fetch_choice()
        await ui.press("f")
        return rows, choice, toasts(ui), ui.app.run_state

    rows, choice, messages, state = run_ui(lazy(world, tmp_path, "off"), scenario)

    assert choice is None and state is None
    assert any("fetching on demand is off" in " ".join(row) for row in rows)
    assert not any("press f" in " ".join(row) for row in rows)
    assert (
        "Fetching on demand is switched off by the plan file (session.lazy: off)."
        in messages
    )


def test_off_does_not_fetch_by_itself_either(world, tmp_path):
    async def scenario(ui):
        await look_at(ui, "ws-0002", "report:rep-0002")
        await ui.pilot.pause(0.3)
        await ui.settle()
        return ui.app.run_state, ui.explorer._auto_timer

    assert run_ui(lazy(world, tmp_path, "off"), scenario) == (None, None)


# ---------------------------------------------------------------------------
# what may be fetched without asking, and the quota it leaves
# ---------------------------------------------------------------------------


def a_detail(sensitive="", scope_user=False, refused=(), held_value=None):
    """A detail with one way to fetch it: the users of a report by an administrator (which
    has a quota of 200 an hour), made harmless or not."""
    target = get_target("user-pages" if scope_user else "report-users")
    provider = Provider(
        replace(target, sensitive=sensitive),
        get_endpoint(target.endpoint),
        {"reportId": "r"},
        {"reportId": ["r"]},
    )
    return (
        Detail("users", "Users", (provider,), held_value, (), tuple(refused)),
        provider,
    )


def test_only_what_is_harmless_may_be_fetched_by_itself():
    harmless, _ = a_detail()
    sensitive, _ = a_detail(sensitive="e-mail addresses")

    auto = Fetching(None, "", "auto")

    assert [d.name for d, _ in auto.auto([harmless], lambda e: None)] == ["users"]
    assert auto.auto([sensitive], lambda e: None) == []


def test_it_is_up_to_the_session_whether_anything_is_fetched_by_itself():
    harmless, _ = a_detail()

    for mode in ("ask", "off"):
        assert Fetching(None, "", mode).auto([harmless], lambda e: None) == []
    assert Fetching(None, "view only", "auto").auto([harmless], lambda e: None) == []


def test_a_detail_that_two_harmless_ways_can_fetch_is_fetched_by_one():
    _, admin = a_detail()
    _, user = a_detail(scope_user=True)
    both = Detail("users", "Users", (admin, user), None, (), ())

    found = Fetching(None, "", "auto").auto([both], lambda e: None)

    assert [(d.name, p.target.name) for d, p in found] == [("users", "report-users")]


def test_what_the_lake_holds_and_what_was_refused_is_left_alone():
    harmless, provider = a_detail()
    refused, _ = a_detail(refused=[provider.key])
    got, _ = a_detail(held_value=object())

    auto = Fetching(None, "", "auto")

    assert auto.auto([refused], lambda e: None) == []
    assert auto.auto([got], lambda e: None) == []


def test_only_an_account_that_is_stored_fetches_by_itself():
    harmless, _ = a_detail()

    assert Fetching({Scope.USER}, "", "auto").auto([harmless], lambda e: None) == []
    assert (
        len(Fetching({Scope.ADMIN}, "", "auto").auto([harmless], lambda e: None)) == 1
    )


def test_a_fetch_by_itself_leaves_half_of_the_quota_for_what_somebody_asks():
    harmless, _ = a_detail()  # 200 requests an hour
    auto = Fetching(None, "", "auto")

    assert auto.auto([harmless], lambda e: 101) != []
    assert auto.auto([harmless], lambda e: 100) == []  # half is left: stop here
    assert auto.auto([harmless], lambda e: 0) == []
    assert auto.auto([harmless], lambda e: None) != []  # nothing is known: go on


def test_the_backend_says_how_much_quota_is_left_for_an_operation(world):
    backend = backend_of(world)
    users = get_endpoint("admin.reports.users")
    world.fake.reset_calls()

    full = backend.quota_left(users)
    world.admin.request("admin.reports.users", {"reportId": "rep-0001"})

    assert full == 200 and backend.quota_left(users) == 199
    assert backend.quota_left(get_endpoint("user.report_pages")) is None


def test_the_backend_knows_nothing_without_an_account(world):
    from pbi_cli.errors import PBIError

    def nobody(scope, profile=None):
        raise PBIError("No profile set")

    backend = backend_of(world, client_for=nobody)

    assert backend.quota_left(get_endpoint("admin.reports.users")) is None


def test_the_providers_of_a_detail_carry_their_sensitivity():
    found = providers_of("report", "users", "rep-1", "ws-1")
    pages = providers_of("report", "pages", "rep-1", "ws-1")

    assert all(p.target.sensitive for p in found)
    assert [p.target.sensitive for p in pages] == [""]


def test_the_guide_says_how_long_it_waits_and_how_much_quota_it_leaves():
    guide = (ROOT / "docs" / "tui.md").read_text(encoding="utf-8")

    assert REAL_AUTO_AFTER == 1.5 and "one and a half seconds" in guide
    assert AUTO_KEEPS == 0.5 and "more than half of the smallest allowance" in guide
