"""A backup of a lake: everything is copied, the copy reads like the lake, and nothing that
is there already is overwritten."""

import json
from datetime import datetime, timezone

import pytest
from cloudpathlib import CloudPath
from sync_helpers import TENANT, World

from pbi_cli.core.backup import BACKUP_FILE, backup_lake, default_place
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
