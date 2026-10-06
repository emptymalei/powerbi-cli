"""Publishing a lake: what is copied, what is protected, and what is refused."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sync_helpers import TENANT, World

from pbi_cli.core.catalog import Catalog
from pbi_cli.core.publish import (
    CATEGORIES,
    EXCLUDABLE,
    category_of,
    plan_publish,
    publish,
)
from pbi_cli.core.scan import ScanFlags
from pbi_cli.core.store import PUBLISH_FILE, LakeStore
from pbi_cli.core.sync.state import STATE_NAME
from pbi_cli.errors import PBIError, ReadOnlyLake

FULL = ScanFlags(lineage=True, datasource_details=True, get_artifact_users=True)
ANA = "ana@laptop"
BOB = "bob@desk"
NOW = datetime(2026, 10, 2, 7, 20, tzinfo=timezone.utc)

FOLDERS = {
    "activity": "endpoint=admin_activityevents",
    "users": "endpoint=admin_reports_users",
    "datasources": "endpoint=admin_datasets_datasources",
    "scans": "endpoint=admin_scan_result",
}


@pytest.fixture
def world(tmp_path):
    """A lake with every kind of data in it, made by the real engine."""
    world = World(tmp_path / "src")
    world.run()
    world.run("scan", scan_flags=FULL)
    world.run("activity", days=2)
    world.run("report-users")
    world.run("datasources")
    return world


def files_of(root):
    """The files of a lake, as paths below its root."""
    return {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and not p.name.endswith(".tmp")
    }


def go(world, tmp_path, name="pub", **options):
    options.setdefault("publisher", ANA)
    destination = LakeStore(tmp_path / name)
    plan = plan_publish(world.store, destination, **options)
    return plan, publish(plan, now=NOW)


# ---------------------------------------------------------------------------
# what is copied
# ---------------------------------------------------------------------------


def test_a_published_lake_reads_like_the_lake_it_was_made_from(world, tmp_path):
    plan, result = go(world, tmp_path)

    mine = Catalog(world.store, TENANT, clock=world.clock.now)
    theirs = Catalog(LakeStore(tmp_path / "pub"), TENANT, clock=world.clock.now)
    assert [w.id for w in theirs.workspaces()] == [w.id for w in mine.workspaces()]
    assert [(i.kind, i.id) for i in theirs.items("ws-0001")] == [
        (i.kind, i.id) for i in mine.items("ws-0001")
    ]
    assert theirs.scan_of("ws-0001") is not None
    report = theirs.item("report", "rep-0001")
    assert [a.email for a in theirs.users(report).rows] == [
        a.email for a in mine.users(mine.item("report", "rep-0001")).rows
    ]
    assert [e.day for e in theirs.event_days()] == [e.day for e in mine.event_days()]
    assert result.info.published_by == ANA and result.info.tenants == [TENANT]


def test_the_published_lake_has_the_layout_of_the_lake_and_a_marker(world, tmp_path):
    plan, result = go(world, tmp_path)

    published = files_of(tmp_path / "pub")
    assert PUBLISH_FILE in published
    assert published - {PUBLISH_FILE} == {
        f"{'/'.join(leaf.parts)}/{name}"
        for leaf in plan.leaves
        for name, _ in leaf.files
    } | {f"tenant={TENANT}/_state/sync.json"}
    marker = json.loads((tmp_path / "pub" / PUBLISH_FILE).read_text(encoding="utf-8"))
    assert marker["protected"] is True and marker["complete"] is True
    assert marker["published_at"].startswith("2026-10-02T07:20")
    assert marker["files"] == plan.files and marker["bytes"] == plan.size
    assert marker["excluded"] == [] and marker["history"] is False


def test_only_the_newest_version_of_each_request_is_copied_unless_history_is_asked(
    world, tmp_path
):
    world.clock.advance(hours=30)
    world.run("groups")  # a second version of the list of workspaces
    params = world.store.parameter_sets(TENANT, "admin.groups")[0].params
    assert len(world.store.versions(TENANT, "admin.groups", params)) == 2

    go(world, tmp_path, "newest")
    go(world, tmp_path, "everything", history=True)

    newest = LakeStore(tmp_path / "newest").versions(TENANT, "admin.groups", params)
    every = LakeStore(tmp_path / "everything").versions(TENANT, "admin.groups", params)
    assert len(newest) == 1 and len(every) == 2
    assert (
        newest[0].version == world.store.latest(TENANT, "admin.groups", params).version
    )
    assert json.loads((tmp_path / "everything" / PUBLISH_FILE).read_text())["history"]


@pytest.mark.parametrize("category", EXCLUDABLE)
def test_a_category_can_be_left_out(world, tmp_path, category):
    plan, result = go(world, tmp_path, exclude=[category])

    tenant_dir = tmp_path / "pub" / f"tenant={TENANT}"
    assert not (tenant_dir / FOLDERS[category]).exists()
    others = [FOLDERS[c] for c in EXCLUDABLE if c != category]
    assert all((tenant_dir / folder).is_dir() for folder in others)
    assert result.info.excluded == [category]
    left = next(s for s in plan.summaries if s.category.name == category)
    assert left.excluded and left.files > 0


def test_leaving_out_the_scans_leaves_out_their_baseline_too(world, tmp_path):
    kept, _ = go(world, tmp_path, "kept")
    plan, _ = go(world, tmp_path, "left-out", exclude=["scans"])

    assert LakeStore(tmp_path / "kept").read_state(TENANT, STATE_NAME)["scan"]
    assert LakeStore(tmp_path / "left-out").read_state(TENANT, STATE_NAME)["scan"] == {}


def test_what_cannot_be_left_out_or_does_not_exist_is_an_error(world, tmp_path):
    for name in ("lists", "nothing"):
        with pytest.raises(PBIError, match=f"Cannot leave out '{name}'"):
            plan_publish(world.store, LakeStore(tmp_path / "pub"), exclude=[name])


def test_each_folder_is_copied_with_its_manifest_last(world, tmp_path):
    plan = plan_publish(world.store, LakeStore(tmp_path / "pub"), publisher=ANA)

    for leaf in plan.leaves:
        names = [name for name, _ in leaf.files]
        assert names[-1] == "manifest.json", leaf.parts
        assert names.count("manifest.json") == 1


def test_the_categories_are_told_apart_by_the_endpoint(world):
    assert category_of("admin_activityevents") == "activity"
    assert category_of("admin_reports_users") == "users"
    assert category_of("admin_users_artifact_access") == "users"
    assert category_of("admin_datasets_datasources") == "datasources"
    assert category_of("admin_scan_result") == "scans"
    assert category_of("admin_groups") == "lists"
    assert category_of("user_report_pages") == "lists"


def test_the_plan_says_what_each_category_holds_and_why_it_may_matter(world, tmp_path):
    plan = plan_publish(
        world.store, LakeStore(tmp_path / "pub"), exclude=["activity"], publisher=ANA
    )

    by_name = {s.category.name: s for s in plan.summaries}
    assert [s.category.name for s in plan.summaries] == [c.name for c in CATEGORIES]
    assert all(by_name[name].files > 0 for name in by_name)
    assert by_name["lists"].category.excludable is False
    assert "e-mail addresses" in by_name["users"].category.sensitive
    assert "IP addresses" in by_name["activity"].category.sensitive
    assert "queries" in by_name["scans"].category.sensitive
    assert by_name["activity"].excluded and not by_name["users"].excluded
    assert plan.files == sum(
        s.files for s in plan.summaries if not s.excluded
    ) and plan.size == sum(s.size for s in plan.summaries if not s.excluded)


def test_the_published_state_keeps_how_the_runs_went_but_not_what_is_pending(
    world, tmp_path
):
    state = world.store.read_state(TENANT, STATE_NAME)
    state["runs"] = [{"id": f"r{n}", "status": "completed"} for n in range(8)]
    state["units"] = {"admin.reports.users?reportId=rep-1": {"status": "failed"}}
    state["scan"]["jobs"] = {"batch": {"scan_id": "secret"}}
    world.store.write_state(TENANT, STATE_NAME, state)

    go(world, tmp_path)

    published = LakeStore(tmp_path / "pub").read_state(TENANT, STATE_NAME)
    assert [r["id"] for r in published["runs"]] == ["r3", "r4", "r5", "r6", "r7"]
    assert published["units"] == {}
    assert "jobs" not in published["scan"]
    assert published["scan"]["last_success_at"] == state["scan"]["last_success_at"]


# ---------------------------------------------------------------------------
# what is written, and what never is
# ---------------------------------------------------------------------------


def test_a_dry_run_writes_nothing(world, tmp_path):
    plan_publish(world.store, LakeStore(tmp_path / "pub"), publisher=ANA)

    assert not (tmp_path / "pub").exists()


def test_publishing_again_copies_only_what_can_change_and_never_overwrites(
    world, tmp_path
):
    first, _ = go(world, tmp_path)
    victim = next(
        leaf for leaf in first.leaves if not leaf.mutable and leaf.category == "lists"
    )
    path = tmp_path / "pub" / "/".join(victim.parts) / "data.json"
    path.write_text('{"tampered": true}', encoding="utf-8")

    plan, result = go(world, tmp_path)

    assert path.read_text(encoding="utf-8") == '{"tampered": true}'  # not overwritten
    changing = sum(len(leaf.files) for leaf in plan.leaves if leaf.mutable)
    assert result.copied == changing and changing > 0
    assert result.skipped == plan.files - changing


def test_a_day_of_events_that_is_still_open_is_copied_again(world, tmp_path):
    go(world, tmp_path)
    yesterday = world.clock.now().date() - timedelta(days=1)
    stored = world.store.event_day(TENANT, "admin.activityevents", yesterday)
    assert not stored.sealed
    world.fake.add_events(yesterday, 2)  # two events turn up late
    world.clock.advance(hours=2)
    world.run("activity", days=2)
    grown = world.store.event_day(TENANT, "admin.activityevents", yesterday)
    assert grown.rows == stored.rows + 2

    go(world, tmp_path)

    published = LakeStore(tmp_path / "pub").event_day(
        TENANT, "admin.activityevents", yesterday
    )
    assert published.rows == grown.rows


def test_a_publish_that_stops_halfway_is_finished_by_publishing_again(
    world, tmp_path, monkeypatch
):
    clean, _ = go(world, tmp_path, "clean")
    destination = LakeStore(tmp_path / "pub")
    plan = plan_publish(world.store, destination, publisher=ANA)
    real = LakeStore.write_file
    writes = []

    def failing(self, path, payload):
        writes.append(path)
        if len(writes) > 3:
            raise RuntimeError("the disk is full")
        real(self, path, payload)

    monkeypatch.setattr(LakeStore, "write_file", failing)
    with pytest.raises(RuntimeError, match="disk is full"):
        publish(plan, now=NOW)
    monkeypatch.setattr(LakeStore, "write_file", real)

    marker = LakeStore(tmp_path / "pub").published()
    assert marker is not None and marker.complete is False  # protected, and unfinished
    with pytest.raises(ReadOnlyLake):
        LakeStore(tmp_path / "pub").write_state(TENANT, "x", {})

    again = plan_publish(
        world.store, destination, publisher=ANA
    )  # allowed: same publisher
    publish(again, now=NOW)

    assert LakeStore(tmp_path / "pub").published().complete is True
    assert files_of(tmp_path / "pub") == files_of(tmp_path / "clean")


def test_the_marker_of_an_earlier_publish_stays_complete_while_a_later_one_runs(
    world, tmp_path, monkeypatch
):
    go(world, tmp_path)
    destination = LakeStore(tmp_path / "pub")
    seen = []
    real = LakeStore.write_file

    def watching(self, path, payload):
        seen.append(LakeStore(tmp_path / "pub").refresh_marker().complete)
        real(self, path, payload)

    monkeypatch.setattr(LakeStore, "write_file", watching)
    world.clock.advance(hours=30)
    world.run("groups")

    publish(plan_publish(world.store, destination, publisher=ANA), now=NOW)

    assert seen and all(seen)


def test_the_published_lake_is_protected_from_every_other_writer(world, tmp_path):
    go(world, tmp_path)

    with pytest.raises(ReadOnlyLake, match="published by ana@laptop"):
        LakeStore(tmp_path / "pub").write_state(TENANT, "x", {})


def test_older_versions_at_the_destination_can_be_deleted_afterwards(world, tmp_path):
    go(world, tmp_path)
    world.clock.advance(hours=30)
    world.run("groups")
    params = world.store.parameter_sets(TENANT, "admin.groups")[0].params
    published = LakeStore(tmp_path / "pub")
    publish(plan_publish(world.store, published, publisher=ANA), now=NOW)
    assert len(published.versions(TENANT, "admin.groups", params)) == 2

    plan, result = go(world, tmp_path, prune=True)

    assert result.pruned >= 1
    assert (
        len(LakeStore(tmp_path / "pub").versions(TENANT, "admin.groups", params)) == 1
    )


def test_a_second_publish_by_the_same_person_updates_the_marker(world, tmp_path):
    go(world, tmp_path)
    destination = LakeStore(tmp_path / "pub")
    later = datetime(2026, 10, 3, 7, 0, tzinfo=timezone.utc)

    publish(plan_publish(world.store, destination, publisher=ANA), now=later)

    assert LakeStore(tmp_path / "pub").published().published_at == later


# ---------------------------------------------------------------------------
# what is refused
# ---------------------------------------------------------------------------


def test_the_lake_cannot_be_published_onto_itself(world):
    with pytest.raises(PBIError, match="is the lake that is published"):
        plan_publish(world.store, LakeStore(world.store.root))


def test_a_destination_inside_the_lake_is_refused(world):
    with pytest.raises(PBIError, match="inside the lake that is published"):
        plan_publish(world.store, LakeStore(world.store.root / "copy"))


def test_a_lake_inside_the_destination_is_refused(world):
    with pytest.raises(
        PBIError, match="lake that is published is inside the destination"
    ):
        plan_publish(world.store, LakeStore(world.store.root.parent))


def test_a_folder_with_other_things_in_it_is_refused(world, tmp_path):
    (tmp_path / "pub").mkdir()
    (tmp_path / "pub" / "notes.txt").write_text("mine", encoding="utf-8")

    with pytest.raises(PBIError, match="has content and is not a published lake"):
        plan_publish(world.store, LakeStore(tmp_path / "pub"))

    assert (tmp_path / "pub" / "notes.txt").read_text(encoding="utf-8") == "mine"
    assert files_of(tmp_path / "pub") == {"notes.txt"}


def test_an_empty_folder_is_fine(world, tmp_path):
    (tmp_path / "pub").mkdir()

    go(world, tmp_path)

    assert PUBLISH_FILE in files_of(tmp_path / "pub")


def test_a_lake_that_someone_else_published_needs_force(world, tmp_path):
    go(world, tmp_path, publisher=ANA)

    with pytest.raises(PBIError, match="published by ana@laptop.*needs --force"):
        plan_publish(world.store, LakeStore(tmp_path / "pub"), publisher=BOB)

    plan, _ = go(world, tmp_path, publisher=BOB, force=True)
    assert LakeStore(tmp_path / "pub").published().published_by == BOB


def test_prune_and_history_do_not_go_together(world, tmp_path):
    with pytest.raises(PBIError, match="cannot go with --history"):
        plan_publish(world.store, LakeStore(tmp_path / "pub"), prune=True, history=True)


def test_a_tenant_the_lake_does_not_hold_is_an_error(world, tmp_path):
    with pytest.raises(PBIError, match="no data of tenant 'other'"):
        plan_publish(world.store, LakeStore(tmp_path / "pub"), tenants=["other"])


def test_a_lake_with_nothing_in_it_is_an_error(tmp_path):
    with pytest.raises(PBIError, match="nothing to publish"):
        plan_publish(LakeStore(tmp_path / "empty"), LakeStore(tmp_path / "pub"))


def test_one_tenant_can_be_chosen(world, tmp_path):
    other = World(tmp_path / "other")
    other.run("groups")
    for leaf_dir in (other.store.root / "tenant=tenant-1").iterdir():
        target = world.store.root / "tenant=tenant-2" / leaf_dir.name
        target.parent.mkdir(parents=True, exist_ok=True)
        import shutil

        shutil.copytree(leaf_dir, target)

    plan, result = go(world, tmp_path, tenants=["tenant-2"])

    assert plan.tenants == ["tenant-2"] and result.info.tenants == ["tenant-2"]
    assert not (tmp_path / "pub" / "tenant=tenant-1").exists()


# ---------------------------------------------------------------------------
# remote lakes
# ---------------------------------------------------------------------------


def test_a_lake_can_be_published_to_s3_and_read_from_there(world, local_s3):
    destination = LakeStore("s3://bucket/shared")
    plan = plan_publish(world.store, destination, publisher=ANA)

    result = publish(plan, now=NOW)

    remote = LakeStore("s3://bucket/shared")
    assert remote.tenants() == [TENANT] and remote.published() is not None
    assert remote.published().complete is True and result.copied == plan.files
    catalog = Catalog(remote, TENANT, clock=world.clock.now)
    assert len(catalog.workspaces()) == 12 and catalog.scan_of("ws-0001") is not None
    with pytest.raises(ReadOnlyLake):
        remote.write_state(TENANT, "x", {})


def test_a_lake_in_s3_can_be_published_to_a_folder(world, local_s3, tmp_path):
    source = LakeStore("s3://bucket/work")
    for path in world.store.root.rglob("*"):
        if path.is_file():
            relative = path.relative_to(world.store.root).as_posix()
            (source.root / relative).write_bytes(path.read_bytes())

    plan = plan_publish(source, LakeStore(tmp_path / "pub"), publisher=ANA)
    publish(plan, now=NOW)

    assert [
        w.id for w in Catalog(LakeStore(tmp_path / "pub"), TENANT).workspaces()
    ] == [w.id for w in Catalog(world.store, TENANT).workspaces()]
