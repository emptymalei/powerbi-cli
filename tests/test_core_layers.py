"""A shared lake with the work lake on top: both read as one, only the work lake written."""

import json

import pytest
from sync_helpers import TENANT, World

from pbi_cli.core import accessreport
from pbi_cli.core.accessreport import access_report
from pbi_cli.core.catalog import Catalog
from pbi_cli.core.layers import LAYERS, SHARED, WORK, LayeredStore, work_store
from pbi_cli.core.publish import plan_publish, publish
from pbi_cli.core.store import LakeStore
from pbi_cli.errors import ReadOnlyLake


def files_of(root):
    return sorted(
        (str(p.relative_to(root)), p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    )


def publish_to(world, destination, publisher="ana@laptop"):
    plan = plan_publish(
        world.store, LakeStore(destination), publisher=publisher, force=True
    )
    publish(plan, now=world.clock.now())


@pytest.fixture
def shared(tmp_path):
    """A lake that a colleague synced and published: workspaces, reports and their users."""
    colleague = World(tmp_path / "colleague")
    colleague.run("groups", "reports", "report-users", "activity", days=2)
    publish_to(colleague, tmp_path / "shared")
    colleague.destination = tmp_path / "shared"
    return colleague


@pytest.fixture
def mine(tmp_path):
    """The work lake of the person who looks: their own account, a day later."""
    world = World(tmp_path / "mine")
    world.clock.advance(days=1)
    return world


def layered(shared, mine):
    return LayeredStore(
        mine.store, LakeStore(shared.destination, readonly=True, reason="shared")
    )


def catalog(store, world):
    return Catalog(store, TENANT, clock=world.clock.now)


# ---------------------------------------------------------------------------
# reading both
# ---------------------------------------------------------------------------


def test_what_either_lake_holds_is_read_as_one(shared, mine):
    mine.run("apps", "app-users")
    lake = layered(shared, mine)

    found = catalog(lake, mine)

    assert found.listing("report") is not None  # the shared lake's
    assert found.listing("app") is not None  # the work lake's
    assert lake.tenants() == [TENANT]
    assert {"admin.reports", "admin.apps"} <= set(lake.endpoints(TENANT))


def test_the_answer_fetched_last_is_taken_and_its_lake_is_known(shared, mine):
    lake = layered(shared, mine)
    params = {"$expand": "users"}
    older = (
        lake.latest(TENANT, "admin.groups", params)
        or lake.parameter_sets(TENANT, "admin.groups")[0].latest
    )
    assert lake.layer_of(older) == SHARED

    mine.run("groups")

    newest = lake.parameter_sets(TENANT, "admin.groups")[0].latest
    assert lake.layer_of(newest) == WORK
    assert newest.fetched_at > older.fetched_at
    versions = lake.versions(
        TENANT, "admin.groups", lake.parameter_sets(TENANT, "admin.groups")[0].params
    )
    assert [lake.layer_of(v) for v in versions] == [WORK, SHARED]


def test_an_older_answer_in_the_work_lake_does_not_hide_a_newer_shared_one(
    tmp_path, mine
):
    colleague = World(tmp_path / "colleague")
    colleague.clock.advance(days=3)  # synced after the work lake was
    colleague.run("groups")
    publish_to(colleague, tmp_path / "shared")
    mine.run("groups")
    lake = LayeredStore(mine.store, LakeStore(tmp_path / "shared", readonly=True))

    newest = lake.parameter_sets(TENANT, "admin.groups")[0].latest

    assert lake.layer_of(newest) == SHARED


def test_answers_that_depend_on_who_asked_are_kept_beside_each_other(shared, mine):
    shared_user = World(shared.destination.parent / "colleague-user")
    shared_user.run("user-groups")
    publish_to(shared_user, shared.destination.parent / "shared-user")
    mine.accounts(svc="oid-service-account")
    mine.run("user-groups")
    lake = LayeredStore(
        mine.store, LakeStore(shared.destination.parent / "shared-user", readonly=True)
    )

    sets = lake.parameter_sets(TENANT, "user.groups")

    assert len(sets) == 2
    assert sorted(lake.layer_of(s.latest) for s in sets) == [SHARED, WORK]


def test_a_day_of_events_is_taken_whole_from_the_lake_that_holds_more(shared, mine):
    lake = layered(shared, mine)
    days = lake.event_days(TENANT, "admin.activityevents")
    assert days and all(lake.layer_of(d) == SHARED for d in days)
    newest = days[0]

    events = list(lake.read_events(TENANT, "admin.activityevents", newest.day))

    assert len(events) == newest.rows
    assert lake.event_ids(TENANT, "admin.activityevents", newest.day) == {
        e["Id"] for e in events if "Id" in e
    }


def test_the_work_lakes_state_comes_first(shared, mine):
    lake = layered(shared, mine)
    assert lake.read_state(TENANT, "sync") == shared.state()

    mine.run("apps")

    assert lake.read_state(TENANT, "sync") == mine.state()


# ---------------------------------------------------------------------------
# writing: the work lake only
# ---------------------------------------------------------------------------


def test_everything_is_written_to_the_work_lake_and_the_shared_lake_is_untouched(
    shared, mine
):
    before = files_of(shared.destination)
    lake = layered(shared, mine)

    lake.write_state(TENANT, "notes", {"a": 1})
    access_report(catalog(lake, mine), "report")

    assert files_of(shared.destination) == before
    assert mine.store.read_state(TENANT, "notes") == {"a": 1}
    assert lake.writable and work_store(lake) is mine.store


def test_a_work_lake_that_cannot_be_written_makes_the_view_read_only(shared, tmp_path):
    lake = LayeredStore(
        LakeStore(tmp_path / "mine", readonly=True, reason="no"),
        LakeStore(shared.destination, readonly=True),
    )

    assert not lake.writable and lake.why_read_only() == "no"
    with pytest.raises(ReadOnlyLake):
        lake.write_state(TENANT, "notes", {})


def test_who_published_it_is_the_shared_lakes(shared, mine):
    assert layered(shared, mine).published().published_by == "ana@laptop"
    assert layered(shared, mine).root == shared.destination


# ---------------------------------------------------------------------------
# who has access to what, from both
# ---------------------------------------------------------------------------


def never_built(monkeypatch):
    def fail(catalog):
        raise AssertionError("the table was worked out again")

    monkeypatch.setattr(accessreport, "build_access_table", fail)


def test_the_access_table_takes_users_from_both_and_is_kept_in_the_work_lake(
    shared, mine, monkeypatch
):
    mine.run("apps", "app-users")
    lake = layered(shared, mine)

    reports = access_report(catalog(lake, mine), "report")
    apps = access_report(catalog(lake, mine), "app")

    assert reports.rows and apps.rows
    kept = list((mine.store.root / LAYERS).rglob("access.csv"))
    assert len(kept) == 1
    # the work lake's own table is its own: no report users in it
    own = access_report(catalog(mine.store, mine), "report")
    assert not own.rows

    never_built(monkeypatch)
    again = access_report(catalog(layered(shared, mine), mine), "report")
    assert again.rows == reports.rows


def test_a_fetch_into_the_work_lake_makes_the_table_of_both_stale(shared, mine):
    lake = layered(shared, mine)
    assert not access_report(catalog(lake, mine), "app").rows

    mine.run("apps", "app-users")

    assert lake.tables_stale(TENANT)
    assert access_report(catalog(lake, mine), "app").rows


def test_a_shared_lake_published_again_makes_the_table_of_both_stale(shared, mine):
    lake = layered(shared, mine)
    access_report(catalog(lake, mine), "report")
    assert not lake.tables_stale(TENANT)

    shared.clock.advance(hours=1)
    publish_to(shared, shared.destination, publisher="bob@desk")

    assert layered(shared, mine).tables_stale(TENANT)


def test_a_stamp_that_does_not_match_is_never_taken(shared, mine):
    lake = layered(shared, mine)
    access_report(catalog(lake, mine), "report")
    stamp = next((mine.store.root / LAYERS).rglob("layers.json"))
    kept = json.loads(stamp.read_text("utf-8"))
    stamp.write_text(json.dumps({**kept, "shared": "something else"}), "utf-8")

    assert lake.tables_stale(TENANT)


def test_publishing_the_work_lake_leaves_the_shared_lakes_data_out(
    shared, mine, tmp_path
):
    mine.run("apps")
    access_report(catalog(layered(shared, mine), mine), "report")

    plan = plan_publish(mine.store, LakeStore(tmp_path / "out"), publisher="me")
    publish(plan, now=mine.clock.now())

    out = LakeStore(tmp_path / "out")
    assert "admin.reports" not in out.endpoints(TENANT)
    assert not (tmp_path / "out" / LAYERS).exists()
