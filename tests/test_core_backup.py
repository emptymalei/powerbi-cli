"""A backup of a lake: everything is copied, the copy reads like the lake, and nothing that
is there already is overwritten."""

import json
from datetime import datetime, timezone

import pytest
from cloudpathlib import CloudPath
from sync_helpers import TENANT, World

from pbi_cli.core.backup import (  # noqa: E402
    BACKUP_FILE,
    backup_lake,
    default_place,
    plan_restore,
    restore_lake,
)
from pbi_cli.core.catalog import Catalog
from pbi_cli.core.store import LakeStore
from pbi_cli.errors import PBIError

NOW = datetime(2026, 10, 30, 22, 40, tzinfo=timezone.utc)


@pytest.fixture
def world(tmp_path):
    world = World(tmp_path / "src")
    world.run()
    world.run("activity", days=2)
    return world


def files_of(root):
    return {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and not p.name.endswith(".tmp")
    }


def test_a_backup_holds_every_file_and_reads_like_the_lake(world, tmp_path):
    target = tmp_path / "copy"
    done = backup_lake(world.store.root, target, now=NOW)

    assert files_of(target) == files_of(world.store.root) | {BACKUP_FILE}
    assert done.files == len(files_of(world.store.root)) and done.size > 0
    copy = Catalog(LakeStore(target), TENANT, clock=world.clock.now)
    mine = Catalog(world.store, TENANT, clock=world.clock.now)
    assert [w.id for w in copy.workspaces()] == [w.id for w in mine.workspaces()]
    assert [d.day for d in copy.event_days()] == [d.day for d in mine.event_days()]


def test_the_backup_file_says_what_was_copied_and_comes_last(world, tmp_path):
    order = []
    target = tmp_path / "copy"
    backup_lake(world.store.root, target, now=NOW, on_file=order.append)

    info = json.loads((target / BACKUP_FILE).read_text(encoding="utf-8"))
    assert info["source"] == str(world.store.root)
    assert info["made_at"].startswith("2026-10-30T22:40")
    assert info["files"] == len(order) == order[-1]


def test_by_default_the_backup_goes_next_to_the_lake(world):
    done = backup_lake(world.store.root, now=NOW)

    assert done.destination == world.store.root.parent / "lake-backup-20261030T224000Z"
    assert (done.destination / BACKUP_FILE).exists()


def test_an_unfinished_write_is_not_copied(world, tmp_path):
    (world.store.root / "half.json.tmp").write_text("{", encoding="utf-8")

    backup_lake(world.store.root, tmp_path / "copy", now=NOW)

    assert not (tmp_path / "copy" / "half.json.tmp").exists()


def test_an_empty_or_missing_lake_has_nothing_to_back_up(tmp_path):
    assert backup_lake(tmp_path / "nothing", tmp_path / "copy") is None
    (tmp_path / "empty").mkdir()
    assert backup_lake(tmp_path / "empty", tmp_path / "copy") is None
    assert not (tmp_path / "copy").exists()


def test_a_place_that_holds_something_is_refused(world, tmp_path):
    (tmp_path / "copy").mkdir()
    (tmp_path / "copy" / "mine.txt").write_text("keep me", encoding="utf-8")

    with pytest.raises(PBIError, match="holds something already"):
        backup_lake(world.store.root, tmp_path / "copy")
    assert (tmp_path / "copy" / "mine.txt").read_text(encoding="utf-8") == "keep me"


@pytest.mark.parametrize("where", ["itself", "inside", "around"])
def test_the_lake_itself_inside_it_or_around_it_is_refused(world, where):
    root = world.store.root
    target = {"itself": root, "inside": root / "copy", "around": root.parent}[where]

    with pytest.raises(PBIError, match="place of its own"):
        backup_lake(root, target)


def test_a_copy_that_fails_says_so_and_leaves_no_backup_file(
    world, tmp_path, monkeypatch
):
    import pbi_cli.core.backup as backup_module

    calls = []

    def failing_copy(source, target):
        calls.append(source)
        if len(calls) == 3:
            raise OSError("disk full")
        real_copy(source, target)

    real_copy = backup_module._copy
    monkeypatch.setattr(backup_module, "_copy", failing_copy)

    with pytest.raises(PBIError, match="stopped after 2 file"):
        backup_lake(world.store.root, tmp_path / "copy")
    assert not (tmp_path / "copy" / BACKUP_FILE).exists()


def test_a_lake_that_is_a_whole_bucket_needs_a_place(local_s3):
    with pytest.raises(PBIError, match="--backup-to"):
        default_place(CloudPath("s3://bucket/"), NOW)


def test_a_lake_in_s3_is_backed_up_in_s3(world, local_s3):
    remote = CloudPath("s3://bucket/pbi-lake")
    backup_lake(world.store.root, remote, now=NOW)  # a folder to S3

    done = backup_lake(remote, now=NOW)  # S3 to S3, next to it

    assert str(done.destination) == "s3://bucket/pbi-lake-backup-20261030T224000Z"
    assert files_of(done.destination) == files_of(remote)  # backup.json on both
    copy = Catalog(LakeStore(done.destination), TENANT, clock=world.clock.now)
    assert copy.workspaces()


def test_a_lake_in_s3_can_be_backed_up_to_a_folder(world, local_s3, tmp_path):
    remote = CloudPath("s3://bucket/pbi-lake")
    backup_lake(world.store.root, remote, now=NOW)

    backup_lake(remote, tmp_path / "copy", now=NOW)

    assert files_of(tmp_path / "copy") == files_of(remote)


# ---------------------------------------------------------------------------
# restoring
# ---------------------------------------------------------------------------


@pytest.fixture
def saved(world, tmp_path):
    """The lake as it was, in a backup."""
    target = tmp_path / "backup"
    backup_lake(world.store.root, target, now=NOW)
    return target


def damage(world):
    """What a sync that stopped half way leaves: a file gone, a file changed, a new file."""
    lake = world.store.root
    some = sorted(p for p in lake.rglob("data.json"))
    gone, changed = some[0], some[1]
    original = {gone: gone.read_bytes(), changed: changed.read_bytes()}
    gone.unlink()
    changed.write_text('{"half": "written"', encoding="utf-8")
    extra = lake / "tenant=tenant-1" / "endpoint=admin_extra" / "stray.json"
    extra.parent.mkdir(parents=True)
    extra.write_text("{}", encoding="utf-8")
    return original, extra


def tree(root):
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_a_restore_makes_the_lake_what_the_backup_holds(world, saved):
    before = tree(world.store.root)
    original, extra = damage(world)

    plan = plan_restore(saved, world.store.root)
    done = restore_lake(plan)

    assert tree(world.store.root) == before
    assert done.copied == len(plan.copy) and done.deleted == 1
    assert not extra.exists() and not extra.parent.exists()  # and its folder is gone
    assert all(path.read_bytes() == data for path, data in original.items())


def test_the_plan_says_what_would_happen_and_writes_nothing(world, saved):
    original, extra = damage(world)
    damaged = tree(world.store.root)

    plan = plan_restore(saved, world.store.root)

    assert len(plan.copy) == len(files_of(saved)) - 1  # all but backup.json
    assert [parts[-1] for parts in plan.delete] == ["stray.json"]
    assert plan.size > 0 and plan.complete and plan.made_at.startswith("2026-10-30")
    assert plan.source == str(world.store.root)
    assert tree(world.store.root) == damaged


def test_files_that_the_backup_does_not_have_can_be_kept(world, saved):
    _, extra = damage(world)

    plan = plan_restore(saved, world.store.root, keep_extra=True)
    done = restore_lake(plan)

    assert plan.delete == () and done.deleted == 0
    assert extra.exists()


def test_a_backup_that_stopped_before_it_was_complete_is_refused(world, saved):
    (saved / BACKUP_FILE).unlink()

    with pytest.raises(PBIError, match="no backup.json"):
        plan_restore(saved, world.store.root)
    assert (
        plan_restore(saved, world.store.root, allow_incomplete=True).complete is False
    )


def test_nothing_to_restore_from_an_empty_place(world, tmp_path):
    with pytest.raises(PBIError, match="nothing to restore"):
        plan_restore(tmp_path / "missing", world.store.root)


@pytest.mark.parametrize("where", ["itself", "inside", "around"])
def test_a_backup_is_not_restored_over_itself_or_into_its_neighbours(
    world, saved, where
):
    lake = {"itself": saved, "inside": saved / "lake", "around": saved.parent}[where]

    with pytest.raises(PBIError, match="places of their own"):
        plan_restore(saved, lake)


def test_a_restore_that_cannot_copy_deletes_nothing(world, saved, monkeypatch):
    _, extra = damage(world)
    import pbi_cli.core.backup as backup_module

    calls = []

    def failing_copy(source, target):
        calls.append(source)
        if len(calls) == 3:
            raise OSError("disk full")
        real_copy(source, target)

    real_copy = backup_module._copy
    monkeypatch.setattr(backup_module, "_copy", failing_copy)
    plan = plan_restore(saved, world.store.root)

    with pytest.raises(PBIError, match="stopped after 2 of .* Nothing was deleted"):
        restore_lake(plan)
    assert extra.exists()

    monkeypatch.setattr(backup_module, "_copy", real_copy)
    restore_lake(plan)  # run again to finish
    assert not extra.exists()


def test_a_lake_in_s3_is_restored_from_a_backup_in_s3(world, local_s3):
    lake = CloudPath("s3://bucket/pbi-lake")
    backup_lake(world.store.root, lake, now=NOW)
    done = backup_lake(lake, CloudPath("s3://bucket/saved"), now=NOW)
    (lake / "stray.json").write_text("{}", encoding="utf-8")
    some = next(lake.rglob("data.json"))
    some.write_text("{}", encoding="utf-8")

    plan = plan_restore(CloudPath("s3://bucket/saved"), lake)
    restore_lake(plan)

    assert not (lake / "stray.json").exists()
    assert files_of(lake) - {BACKUP_FILE} == files_of(
        CloudPath("s3://bucket/saved")
    ) - {BACKUP_FILE}
    assert some.read_text(encoding="utf-8") != "{}"
    assert done.files > 0


def test_a_restore_into_a_lake_that_is_not_there_yet_makes_it(world, saved, tmp_path):
    target = tmp_path / "new" / "lake"

    done = restore_lake(plan_restore(saved, target))

    assert tree(target) == tree(world.store.root)
    assert done.deleted == 0
