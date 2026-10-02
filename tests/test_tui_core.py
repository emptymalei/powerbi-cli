"""What the TUI is made of that needs no terminal: rendering, the run state, the summary."""

import io
from datetime import datetime, timedelta, timezone

import pytest
from core_helpers import make_client, make_token
from rich.console import Console
from sync_helpers import TENANT, World

from pbi_cli.core.catalog import Catalog, Freshness
from pbi_cli.core.registry import Scope
from pbi_cli.core.scan import ScanFlags
from pbi_cli.core.sync.engine import Event
from pbi_cli.core.sync.plan import SyncOptions, Unit
from pbi_cli.core.sync.runners import DEFERRED, DONE, FAILED, SKIPPED, Outcome
from pbi_cli.errors import AuthError
from pbi_cli.tui import Backend, render, textual_available
from pbi_cli.tui.render import Plain
from pbi_cli.tui.run import MAX_LINES, RunState, describe, report_lines
from pbi_cli.tui.summary import summarize

FULL = ScanFlags(lineage=True, datasource_details=True, get_artifact_users=True)
UTC = timezone.utc


def text(renderable) -> str:
    console = Console(
        file=io.StringIO(),
        width=160,
        force_terminal=False,
        color_system=None,
        record=True,
    )
    console.print(renderable)
    return console.export_text()


@pytest.fixture
def world(tmp_path):
    world = World(tmp_path)
    world.run()
    world.run("scan", scan_flags=FULL)
    world.run("activity", days=2)
    return world


@pytest.fixture
def catalog(world):
    return Catalog(world.store, TENANT, clock=world.clock.now)


# ---------------------------------------------------------------------------
# small formatting helpers
# ---------------------------------------------------------------------------


def test_the_dots_say_how_fresh_something_is():
    assert (
        str(render.dot(Freshness.FRESH)) == "●"
        and render.dot(Freshness.FRESH).style == "green"
    )
    assert render.dot(Freshness.AGING).style == "yellow"
    assert render.dot(Freshness.OLD).style == "red"
    assert str(render.dot(Freshness.NONE)) == "○"
    assert render.meaning(Freshness.OLD) == "a week old or more"
    assert "not in the lake" in text(render.legend())


def test_times_and_ids_are_shortened_for_tables():
    assert render.short_time("2026-09-30T12:34:56.789Z") == "2026-09-30 12:34"
    assert render.short_time("") == ""
    assert render.short_id("72f988bf-86f1-41af") == "72f988bf-86f…"
    assert render.short_id("short") == "short"


def test_ago_says_never_for_nothing(catalog):
    assert render.ago(catalog, None) == "never"
    assert render.ago(catalog, catalog.now() - timedelta(hours=3)) == "3 h ago"


@pytest.mark.parametrize(
    "expires_in, signed_in, expected, style",
    [
        (timedelta(minutes=42), True, "token 42 min", "green"),
        (timedelta(minutes=5), True, "token 5 min", "bold yellow"),
        (timedelta(minutes=-3), True, "token expired 3 min ago", "bold red"),
        (None, True, "token without expiry", "grey62"),
        (None, False, "not signed in", "bold red"),
    ],
)
def test_the_token_indicator(expires_in, signed_in, expected, style):
    now = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    expires = now + expires_in if expires_in is not None else None

    shown = render.token_text(expires, now, signed_in)

    assert str(shown) == expected and shown.style == style


def test_sizes_are_in_the_largest_unit_that_keeps_them_short():
    assert render._size(120) == "120 B"
    assert render._size(2048) == "2.0 KB"
    assert render._size(5 * 1024 * 1024) == "5.0 MB"
    assert render._size(3 * 1024**3) == "3.0 GB"
    assert render._size(5 * 1024**4) == "5120.0 GB"


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------


def test_rows_can_be_filtered_by_all_their_words(catalog):
    entries = render.item_entries(catalog.items("ws-0001"))

    assert [e.cells[0] for e in render.filter_entries(entries, "")] == [
        e.cells[0] for e in entries
    ]
    assert [e.cells[0] for e in render.filter_entries(entries, "FLOW")] == [
        "Dataflow 1"
    ]
    assert [e.cells[0] for e in render.filter_entries(entries, "list scan")] == [
        "Report 1",
        "Dataset 1",
        "Dashboard 1",
        "Dataflow 1",
    ]
    assert render.filter_entries(entries, "nothing like this") == []


def test_workspace_rows_carry_the_freshness_and_the_item_counts(catalog):
    (row,) = render.workspace_entries(catalog, [catalog.workspace("ws-0001")])

    assert row.key == "ws-0001" and row.kind == "workspace"
    assert str(row.cells[0]) == "●" and row.cells[1] == "Workspace 1"
    assert row.cells[4] == "5" and row.cells[5] == "0 s ago"
    (empty,) = render.workspace_entries(catalog, [catalog.workspace("ws-0012")])
    assert empty.cells[4] == "-"


def test_event_rows_name_the_item_and_the_workspace(catalog):
    rows = render.event_entries(
        [
            {
                "Id": "e1",
                "CreationTime": "2026-09-30T10:00:00",
                "Activity": "ViewReport",
                "UserId": "u@x",
                "ReportName": "Sales",
                "WorkSpaceName": "Finance",
            },
            {"CreationTime": "2026-09-30T11:00:00", "Activity": "Export"},
        ]
    )

    assert rows[0].cells == (
        "2026-09-30 10:00:00",
        "ViewReport",
        "u@x",
        "Sales",
        "Finance",
    )
    assert rows[1].cells == ("2026-09-30 11:00:00", "Export", "-", "-", "-")
    assert (
        rows[0].key == "event:e1" and rows[1].key == "event:1"
    )  # no id: the row number


def test_the_overview_lists_every_list_and_the_events(catalog):
    rows = render.overview_entries(catalog)

    assert [r.cells[1] for r in rows] == [
        "Workspaces",
        "Reports",
        "Datasets",
        "Dashboards",
        "Dataflows",
        "Apps",
        "Capacities",
        "Audit events",
    ]
    workspaces = rows[0].subject
    assert (
        workspaces.data["Operation"] == "admin.groups" and workspaces.data["Rows"] == 12
    )
    assert workspaces.data["Partial"] == "no"


def test_the_overview_of_an_empty_lake(tmp_path):
    from pbi_cli.core.store import LakeStore

    empty = Catalog(LakeStore(tmp_path / "empty"), "t")

    rows = render.overview_entries(empty)

    assert str(rows[0].cells[0]) == "○" and rows[0].cells[3] == "not in the lake"
    assert rows[0].subject.data["Operation"] == "-"
    assert rows[-1].cells[2] == "-"
    assert render.lake_subject(empty).title == "The lake is empty"


def test_a_list_that_is_only_partial_says_so_in_the_overview(tmp_path):
    world = World(tmp_path)
    world.store.write_snapshot(
        TENANT, "admin.groups", {"$top": "2"}, {"value": [{"id": "a", "name": "A"}]}
    )
    catalog = Catalog(world.store, TENANT, clock=world.clock.now)

    workspaces = render.overview_entries(catalog)[0]

    assert workspaces.cells[2] == "1"
    assert workspaces.subject.data["Partial"] == "yes: fetched with a filter or a limit"


# ---------------------------------------------------------------------------
# the detail pane
# ---------------------------------------------------------------------------


def test_the_info_of_a_workspace_lists_its_fields_contents_and_sources(catalog):
    shown = text(render.info(catalog, catalog.workspace("ws-0001")))

    assert "Workspace 1" in shown and "workspace · ws-0001" in shown
    assert "isOnDedicatedCapacity" in shown
    assert "Reports" in shown and "Dataflows" in shown
    assert "admin.groups, fetched 0 s ago" in shown
    assert "options: lineage, datasourceDetails, getArtifactUsers" in shown


def test_the_info_of_a_workspace_without_a_scan_says_so(world):
    world = World(world.store.root.parent / "other")
    world.run("groups")
    catalog = Catalog(world.store, TENANT, clock=world.clock.now)

    shown = text(render.info(catalog, catalog.workspace("ws-0001")))

    assert "none in the lake" in shown


def test_a_workspace_says_what_is_missing_when_the_lists_are_not_in_the_lake(tmp_path):
    world = World(tmp_path)
    world.run("groups")  # only the list of workspaces
    catalog = Catalog(world.store, TENANT, clock=world.clock.now)

    rows = render.contents_rows(catalog, "ws-0001")
    hint = render.empty_hint(catalog, "ws-0001")
    shown = text(render.info(catalog, catalog.workspace("ws-0001")))

    assert rows == [
        (name, "the list is not in the lake")
        for name in ("Reports", "Datasets", "Dashboards", "Dataflows", "Apps")
    ]
    assert (
        "lists of reports, datasets, dashboards, dataflows and apps are not in the lake"
        in hint.long
    )
    assert "Press s and Run" in hint.long and "r to scan" in hint.long
    assert "not in the lake" in hint.short and "press s" in hint.short
    assert "the list is not in the lake" in shown and "Press s and Run" in shown


def test_a_workspace_with_the_lists_but_no_item_and_no_scan_says_to_scan_it(tmp_path):
    world = World(tmp_path)
    world.run()  # the lists: no item of the last workspace, which was never scanned
    catalog = Catalog(world.store, TENANT, clock=world.clock.now)

    hint = render.empty_hint(catalog, "ws-0012")
    shown = text(render.info(catalog, catalog.workspace("ws-0012")))

    assert render.contents_rows(catalog, "ws-0012") == []
    assert hint.short == "never scanned: press r to scan it"
    assert "never scanned" in shown and "Press r" in shown


def test_a_scanned_workspace_without_items_is_just_empty(catalog):
    hint = render.empty_hint(catalog, "ws-0012")
    shown = text(render.info(catalog, catalog.workspace("ws-0012")))

    assert render.contents_rows(catalog, "ws-0012") == []
    assert hint.short == "it holds nothing"
    assert "holds no report, dataset, dashboard, dataflow or app" in shown
    assert "Press" not in shown


def test_a_workspace_with_items_shows_no_hint_and_only_counts(catalog):
    shown = text(render.info(catalog, catalog.workspace("ws-0001")))

    assert "not in the lake" not in shown and "Press" not in shown
    assert render.contents_rows(catalog, "ws-0001")[0] == ("Reports", "1")


def test_the_info_of_an_item_merges_the_list_and_the_scan(catalog):
    item = catalog.item("report", "rep-0001")

    shown = text(render.info(catalog, item))

    assert "Report 1" in shown and "report · rep-0001" in shown
    assert "webUrl" in shown and "datasetId" in shown
    assert "admin.reports, fetched 0 s ago" in shown


def test_a_list_fetched_with_a_limit_is_marked_in_the_info(tmp_path):
    world = World(tmp_path)
    world.store.write_snapshot(
        TENANT,
        "admin.reports",
        {"$top": "1"},
        {"value": [{"id": "rep-0001", "name": "Report 1", "workspaceId": "ws-0001"}]},
    )
    catalog = Catalog(world.store, TENANT, clock=world.clock.now)

    shown = text(render.info(catalog, catalog.item("report", "rep-0001")))

    assert "fetched with a filter or a limit: it may be incomplete" in shown


def test_the_info_of_a_day_capacity_and_event(catalog):
    day = catalog.event_days()[1]
    assert "audit events" in text(render.info(catalog, day))
    assert "complete" in text(render.info(catalog, day)) or "open" in text(
        render.info(catalog, day)
    )
    capacity = render.capacity_entries(catalog)[0].subject
    assert "Capacity 1" in text(render.info(catalog, capacity))
    event = render.event_entries(catalog.events(day.day))[0].subject
    assert event.kind == "event" and "ViewReport" in text(render.info(catalog, event))
    assert render.subject_data(day) == day.manifest
    assert render.subject_data(capacity)["id"] == "cap-0001"


def test_every_tab_has_something_for_every_subject(catalog):
    workspace = catalog.workspace("ws-0001")
    item = catalog.item("dataset", "ds-0001")
    capacity = render.capacity_entries(catalog)[0].subject
    day = catalog.event_days()[0]

    for subject in (workspace, item, capacity, day):
        for tab in render.TABS:
            detail = render.detail(catalog, tab, subject)
            assert detail.body is not None or detail.rows is not None
    assert render.detail(catalog, "info", None).body is not None
    with pytest.raises(ValueError):
        render.detail(catalog, "nonsense", item)


def test_the_users_tab_for_what_has_no_users(catalog):
    detail = render.detail(
        catalog, "users", render.capacity_entries(catalog)[0].subject
    )

    assert detail.rows is None and "workspaces, reports" in text(detail.body)


def test_the_users_note_says_where_they_come_from_or_what_to_do(catalog, tmp_path):
    ws = render.detail(catalog, "users", catalog.workspace("ws-0001"))

    assert ws.rows == [("Owner", "owner@example.com", "Admin", "User")]
    assert ws.note == "1 with access, from scan, fetched 0 s ago."

    world = World(tmp_path / "bare")
    world.run("groups", "reports")
    bare = Catalog(world.store, TENANT, clock=world.clock.now)
    missing = render.detail(bare, "users", bare.item("report", "rep-0001"))
    assert missing.rows == [] and "report-users" in missing.note


def test_the_lineage_tab_shows_the_notes_and_the_empty_sides(tmp_path):
    world = World(tmp_path)
    world.run("groups", "reports", "datasets")
    bare = Catalog(world.store, TENANT, clock=world.clock.now)

    shown = text(render.detail(bare, "lineage", bare.item("report", "rep-0001")).body)

    assert "Built from" in shown and "dataset Dataset 1" in shown
    assert "Built on it" in shown and "nothing the lake knows of" in shown
    assert "No scan of this workspace is in the lake" in shown


def test_the_versions_tab_counts_the_answers(catalog):
    detail = render.detail(catalog, "versions", catalog.workspace("ws-0001"))

    assert detail.note == "2 stored answer(s) hold this, newest first."
    assert len(detail.rows) == 2 and detail.rows[0][2] in (
        "admin.groups",
        "admin.scan.result",
    )


def test_json_is_cut_when_it_is_very_long():
    data = {"rows": list(range(render.MAX_JSON_LINES + 50))}

    shown = text(render.json_renderable(data, "/lake/x"))

    assert "more lines" in shown and "stored in /lake/x" in shown
    short = text(render.json_renderable({"a": 1}))
    assert "more lines" not in short and "stored in" not in short


def test_where_a_subject_is_stored(catalog, world):
    workspace = catalog.workspace("ws-0001")
    assert "admin_scan_result" in render.where_stored(workspace, catalog)
    listed = catalog.item("app", "app-0001")  # apps are not in scans
    assert "admin_apps" in render.where_stored(listed, catalog)
    day = catalog.event_days()[0]
    assert render.where_stored(day, catalog) == str(day.directory)
    assert render.where_stored(Plain("capacity", "c", {}), catalog) is None


# ---------------------------------------------------------------------------
# plans
# ---------------------------------------------------------------------------


def test_a_plan_is_put_in_rows_and_notes(world):
    world.clock.advance(hours=30)
    plan = world.plan("default", "report-users")

    rows = render.plan_rows(plan)
    quota = render.quota_rows(plan)
    notes = render.plan_notes(plan)

    assert rows[0] == ("groups", "admin.groups", "1", "0", "1", "1")
    assert any(r[0] == "report-users" for r in rows)
    assert ("admin.groups", "1", "50/h, 15/min", "15", True) in quota
    assert "Everything fits the quota now." in notes


def test_a_plan_that_depends_on_an_unfetched_list(tmp_path):
    plan = World(tmp_path).plan("report-users")

    assert render.plan_rows(plan) == [
        ("reports *", "admin.reports", "1", "0", "1", "1"),
        ("report-users", "admin.reports.users", "?", "?", "?", "?"),
    ]
    assert render.plan_notes(plan) == [
        "* only here because another target needs its rows",
        "report-users: depends on reports, which has nothing in the lake yet",
        "Everything fits the quota now.",
    ]


def test_a_plan_that_does_not_fit_the_quota_says_what_is_held_back(tmp_path):
    world = World(tmp_path)
    for n in range(15):
        world.admin.request("admin.groups", {"$top": n + 1})

    plan = world.plan("groups")

    assert render.quota_rows(plan) == [
        ("admin.groups", "1", "50/h, 15/min", "0", False)
    ]
    assert render.plan_notes(plan) == [
        "admin.groups needs 1 requests and 0 fit now (50/h, 15/min): the rest is held "
        "back, about 1 more hour(s); run again to continue."
    ]


def test_nothing_to_fetch_is_said(world):
    assert "Nothing to fetch: the lake holds everything fresh." in render.plan_notes(
        world.plan("groups")
    )


# ---------------------------------------------------------------------------
# the run state
# ---------------------------------------------------------------------------


def unit(key="admin.groups", target="groups"):
    return Unit(
        key=key,
        target=target,
        kind="snapshot",
        endpoint="admin.groups",
        scope=Scope.ADMIN,
    )


def event(status, *, done=1, total=1, message="", key="admin.groups", target="groups"):
    return Event(
        "unit",
        unit=unit(key, target),
        outcome=Outcome(status, message),
        done=done,
        total=total,
    )


def test_a_small_stage_logs_every_unit_and_a_big_one_only_every_tenth():
    assert describe(
        Event("stage", stage=2, units=6, targets=("reports", "datasets"))
    ) == (
        "Stage 2: 6 unit(s) of reports, datasets",
        "bold",
    )
    assert describe(event(DONE, message="12 rows")) == (
        "✓ admin.groups  12 rows",
        "green",
    )
    assert describe(event(SKIPPED, message="fresh")) == (
        "· admin.groups  fresh",
        "grey62",
    )
    quiet = describe(event(DONE, done=3, total=100))
    milestone = describe(event(DONE, done=10, total=100))
    last = describe(event(DONE, done=100, total=100))
    assert quiet is None
    assert milestone == ("[10/100] groups ...", "grey62") and last is not None


def test_failures_and_holds_are_always_logged():
    failed = describe(event(FAILED, done=3, total=100, message="boom\nsecond line"))
    held = describe(event(DEFERRED, done=3, total=100, message="quota"))

    assert failed == ("✗ admin.groups: boom", "red")
    assert held == ("… admin.groups: quota", "yellow")


def test_a_run_state_follows_the_events_and_the_end():
    state = RunState("Sync", SyncOptions(), datetime(2026, 9, 30, 12, tzinfo=UTC))
    state.on_event(Event("stage", stage=1, units=2, targets=("groups",)))
    state.on_event(event(DONE, done=1, total=2, message="1 rows"))
    state.on_event(event(FAILED, done=2, total=2, message="no"))

    stage, done, units, targets, counts = state.progress()
    assert (stage, done, units, targets) == (1, 2, 2, ("groups",))
    assert counts[DONE] == 1 and counts[FAILED] == 1
    assert state.running and not state.stopping
    assert (
        state.summary(lambda: datetime(2026, 9, 30, 12, tzinfo=UTC))
        == "Sync: stage 1: groups 2/2"
    )
    lines, cursor = state.read(0)
    assert [t for t, _ in lines] == [
        "Stage 1: 2 unit(s) of groups",
        "✓ admin.groups  1 rows",
        "✗ admin.groups: no",
    ]
    assert state.read(cursor) == ([], cursor)  # only what is new

    state.request_stop()
    state.request_stop()  # said once
    assert state.stopping and state.stop.is_set()
    assert sum(1 for t, _ in state.read(0)[0] if t.startswith("Stopping")) == 1

    state.finish(datetime(2026, 9, 30, 12, 0, 7, tzinfo=UTC))
    assert not state.running and not state.stopping
    assert state.summary(lambda: datetime(2026, 9, 30, 13, tzinfo=UTC)).startswith(
        "Sync: finished in 7 s, 1 fetched"
    )
    state.request_stop()  # a finished run cannot be stopped
    assert not any(t.startswith("Stopping") for t, _ in state.read(cursor + 99)[0])


def test_a_run_that_has_finished_cannot_be_stopped():
    state = RunState("Sync", SyncOptions(), datetime(2026, 9, 30, tzinfo=UTC))
    state.finish(datetime(2026, 9, 30, 0, 1, tzinfo=UTC))

    state.request_stop()

    assert not state.stop.is_set() and not state.stopping
    assert state.read(0) == ([], 0)


def test_the_end_of_a_run_puts_how_it_went_in_the_log(world):
    report = world.run("groups", force=True)
    state = RunState("Sync", SyncOptions(), world.clock.now())

    state.finish(world.clock.now(), report=report)

    lines = [line for line, _ in state.read(0)[0]]
    assert lines[0].startswith("Finished in") and "1 fetched" in lines[0]
    assert state.report is report and state.error == ""


def test_a_run_that_could_not_start_keeps_the_error():
    state = RunState("Sync", SyncOptions(), datetime(2026, 9, 30, tzinfo=UTC))

    state.finish(datetime(2026, 9, 30, tzinfo=UTC), error="no profile")

    assert state.error == "no profile" and state.report is None and not state.running


def test_a_run_that_has_not_reported_a_stage_is_starting():
    state = RunState("Scan X", SyncOptions(), datetime(2026, 9, 30, tzinfo=UTC))

    assert (
        state.summary(lambda: datetime(2026, 9, 30, tzinfo=UTC)) == "Scan X: starting"
    )


def test_the_log_of_a_run_keeps_the_newest_lines_and_numbers_them_on():
    state = RunState("Sync", SyncOptions(), datetime(2026, 9, 30, tzinfo=UTC))
    for n in range(MAX_LINES + 5):
        state.log(f"line {n}")

    lines, cursor = state.read(0)
    assert (
        len(lines) == MAX_LINES and lines[0][0] == "line 5" and cursor == MAX_LINES + 5
    )
    state.log("one more")
    assert state.read(cursor) == ([("one more", "")], cursor + 1)


def test_the_report_of_a_run_is_put_in_words(world):
    world.fake.fail("GET", r"^/admin/apps$", 500)
    world.fake.fail("GET", r"^/admin/groups$", 429, headers={"Retry-After": "4000"})
    world.clock.advance(hours=30)
    report = world.run()

    lines = [line for line, _ in report_lines(report)]

    assert lines[0].startswith("Finished in") and "failed" in lines[0]
    assert any(line.startswith("  failed: admin.apps") for line in lines)
    assert any("held back by a quota" in line for line in lines)


def test_a_long_list_of_failures_is_cut(world):
    from pbi_cli.core.sync.engine import RunReport

    report = RunReport(run_id="r", started_at=world.clock.now())
    report.failures = [(f"unit-{n}", "no") for n in range(14)]
    report.cancelled = 3
    report.notes.append("something to know")

    lines = [line for line, _ in report_lines(report)]

    assert sum(1 for line in lines if line.startswith("  failed:")) == 10
    assert any("and 4 more" in line for line in lines)
    assert any("3 unit(s) were not started" in line for line in lines)
    assert "  something to know" in lines


# ---------------------------------------------------------------------------
# the summary of the lake
# ---------------------------------------------------------------------------


def test_the_summary_says_what_the_lake_holds_and_how_the_last_run_went(world):
    world.fake.fail("GET", r"^/admin/apps$", 500)
    world.clock.advance(hours=30)
    world.run("groups", "apps")
    now = world.clock.now()

    summary = summarize(world.store, TENANT, now, world.admin.limiter)

    assert (
        "groups",
        "admin.groups",
        "1 request(s)",
        "0 s ago",
    ) in summary.holdings
    lines = [line for line, _ in summary.lines]
    assert lines[0].startswith("Last sync:") and "completed, with failures" in lines[0]
    assert "1 failed unit(s), tried again by the next sync:" in lines
    assert any("admin.apps" in line and "attempts: 1" in line for line in lines)
    assert any(line.startswith("Last complete scan began") for line in lines)
    assert any(op == "admin.groups" for op, _ in summary.quota)


def test_only_the_quotas_that_were_used_are_shown(world):
    summary = summarize(world.store, TENANT, world.clock.now(), world.admin.limiter)

    shown = dict(summary.quota)

    assert "admin.groups" in shown and shown["admin.groups"].startswith("49/50 h")
    assert "admin.users.artifact_access" not in shown  # nothing asked for it


def test_scans_that_were_started_and_not_collected_are_listed(world):
    from pbi_cli.core.scan import ScanJob
    from pbi_cli.core.sync.state import SyncState

    state = SyncState(world.store, TENANT, clock=world.clock.now)
    state.set_job("abc", ScanJob("scan-1", world.clock.now()), FULL)

    lines = [
        line for line, _ in summarize(world.store, TENANT, world.clock.now()).lines
    ]

    assert (
        "1 scan(s) were started and not collected: a sync of the scan target continues them."
        in lines
    )


def test_the_summary_of_a_lake_nobody_synced(tmp_path):
    from pbi_cli.core.store import LakeStore

    summary = summarize(
        LakeStore(tmp_path / "x"), "t", datetime(2026, 9, 30, tzinfo=UTC)
    )

    assert summary.holdings == [] and summary.quota == []
    assert summary.lines == [("No sync is recorded for this tenant yet.", "grey62")]


def test_held_back_units_and_pending_scans_are_listed(world):
    world.fake.fail("POST", r"getInfo", 429, times=1, headers={"Retry-After": "4000"})
    world.clock.advance(days=2)
    world.run("scan", scan_flags=FULL, full_scan=True)

    summary = summarize(world.store, TENANT, world.clock.now())

    assert any(
        "held back by a quota, the first can be tried again in" in line
        for line, _ in summary.lines
    )


def test_many_failures_are_summed_up(world):
    from pbi_cli.core.sync.state import SyncState

    state = SyncState(world.store, TENANT)
    for n in range(12):
        state.mark(
            f"unit-{n}",
            "failed",
            target="reports",
            endpoint="admin.reports",
            error="no",
        )
    state.flush(force=True)

    lines = [
        line for line, _ in summarize(world.store, TENANT, world.clock.now()).lines
    ]

    assert "12 failed unit(s), tried again by the next sync:" in lines
    assert "  ... and 4 more" in lines


# ---------------------------------------------------------------------------
# the backend
# ---------------------------------------------------------------------------


def backend(world, **replace):
    settings = dict(
        store=world.store,
        client_for=world.client_for,
        sign_in=lambda *args: None,
        clock=world.clock.now,
    )
    settings.update(replace)
    return Backend(**settings)


def test_the_identity_is_the_administrators_token(world):
    identity = backend(world).identity()

    assert identity.signed_in and identity.tenant == TENANT
    assert (identity.profile, identity.group) == ("admin-nlm", "admin")
    assert identity.expires_at is not None and identity.problem == ""


def test_the_identity_falls_back_to_the_users_token(world):
    def only_user(scope):
        if scope is Scope.ADMIN:
            raise AuthError("No active profile set for group 'admin'.")
        return world.user

    identity = backend(world, client_for=only_user).identity()

    assert identity.group == "user" and identity.profile == "user-nlm"


def test_without_a_token_the_identity_says_why(world):
    def none(scope):
        raise AuthError(f"No active profile set for group '{scope.value}'.")

    identity = backend(world, client_for=none).identity()

    assert not identity.signed_in and identity.tenant is None
    assert identity.problem == "No active profile set for group 'admin'."


def test_a_token_without_a_tenant_claim_is_keyed_by_its_profile(world):
    client = make_client(world.fake, clock=world.clock, token=make_token(tenant=None))[
        0
    ]

    identity = backend(world, client_for=lambda scope: client).identity()

    assert identity.tenant == "profile-admin-nlm"


def test_the_default_engine_uses_the_clients_and_the_lake(world):
    engine = backend(world).engine()

    plan = engine.plan(SyncOptions(targets=("groups",), workers=1))

    assert plan.tenant == TENANT


def test_the_engine_that_was_handed_in_is_used(world):
    mine = object()

    assert backend(world, make_engine=lambda: mine).engine() is mine


def test_textual_is_looked_for_without_being_imported(monkeypatch):
    import importlib.util

    assert textual_available() == (importlib.util.find_spec("textual") is not None)

    monkeypatch.setattr("pbi_cli.tui.importlib.util.find_spec", lambda name: None)
    assert textual_available() is False


def test_a_workspace_of_a_user_lake_that_holds_nothing_is_not_told_to_scan(tmp_path):
    world = World(tmp_path)
    world.only_user()
    world.fake.workspaces.append(
        {
            "id": "ws-empty",
            "name": "Empty",
            "type": "Workspace",
            "state": "Active",
            "isReadOnly": False,
            "isOnDedicatedCapacity": False,
            "modified_at": world.clock.now(),
        }
    )
    world.fake.user_workspace_ids.append("ws-empty")
    world.run()
    catalog = Catalog(world.store, TENANT, clock=world.clock.now)

    hint = render.empty_hint(catalog, "ws-empty")
    shown = text(render.info(catalog, catalog.workspace("ws-empty")))

    assert hint.short == "it holds nothing"
    assert "Press" not in shown and "Visible to" in shown and "user-nlm" in shown


def test_an_item_that_only_a_users_list_has_says_which_list(tmp_path):
    world = World(tmp_path)
    world.only_user()
    world.run()
    catalog = Catalog(world.store, TENANT, clock=world.clock.now)

    shown = text(render.info(catalog, catalog.item("report", "rep-0001")))

    assert "user.group_reports of the workspace, fetched 0 s ago" in shown


# ---------------------------------------------------------------------------
# what the session can fetch of one item
# ---------------------------------------------------------------------------


def _detail(world, kind, item_id, workspace_id, name):
    from pbi_cli.core.details import collect
    from pbi_cli.core.sync.state import STATE_NAME

    state = world.store.read_state(TENANT, STATE_NAME) or {}
    found = collect(
        world.store, TENANT, kind, item_id, workspace_id, state.get("units") or {}
    )
    return next(d for d in found if d.name == name)


def test_a_detail_that_is_held_needs_nothing_to_be_said(world):
    from pbi_cli.tui.fetching import Fetching

    world.run("datasets", "dataset-users")
    users = _detail(world, "dataset", "ds-0001", "ws-0001", "users")

    assert Fetching().how(users) == ""
    assert Fetching(view_only="only reads").how(users) == ""
    assert Fetching().wanted([users]) == []  # nothing is fetched again


def test_a_missing_detail_says_how_to_get_it_by_the_accounts_that_are_stored(world):
    from pbi_cli.tui.fetching import Fetching

    users = _detail(world, "dataset", "ds-0001", "ws-0001", "users")
    report_users = _detail(world, "report", "rep-0001", "ws-0001", "users")

    assert Fetching().how(users) == "press f: 1 request, an administrator account"
    both = Fetching({Scope.ADMIN, Scope.USER})
    assert both.how(users) == "press f: 1 request, an administrator account"
    only_user = Fetching({Scope.USER})
    assert "a user account with Reshare permission" in only_user.how(users)
    assert only_user.how(report_users) == (
        "needs an administrator account, and none is stored"
    )
    assert Fetching(view_only="only reads").how(users).startswith("view only")


def test_what_is_wanted_is_what_is_missing_and_can_be_fetched(world):
    from pbi_cli.tui.fetching import Fetching

    world.run("datasets", "dataset-users")
    details = [
        _detail(world, "dataset", "ds-0001", "ws-0001", name)
        for name in ("users", "datasources", "parameters")
    ]

    wanted = Fetching({Scope.ADMIN}).wanted(details)

    assert [d.name for d, _ in wanted] == [
        "datasources"
    ]  # users are held, no user account
    options = Fetching({Scope.ADMIN}).options(wanted)
    assert options.targets == ("datasources",)
    assert options.only == {"datasetId": ("ds-0001",)}
