"""``pbi lake backup``, and ``--backup`` on the commands that write into a lake."""

from datetime import datetime, timedelta, timezone

import pytest
from cloudpathlib import CloudPath
from core_helpers import make_token
from fake_powerbi import FakePowerBI
from sync_helpers import World
from typer.testing import CliRunner

from pbi_cli.cli import app, store_token
from pbi_cli.config import PBIConfig
from pbi_cli.core.backup import BACKUP_FILE
from pbi_cli.core.store import PUBLISH_FILE, LakeStore, PublishInfo


def pbi(*args, input=None):
    return CliRunner().invoke(app, list(args), input=input)


def backups_next_to(lake_root):
    return sorted(lake_root.parent.glob(f"{lake_root.name}-backup-*"))


def endpoints(root):
    return {p.name for p in root.glob("tenant=*/endpoint=*")}


@pytest.fixture
def fake(monkeypatch):
    service = FakePowerBI(workspaces=20, reports=3, event_days=2, events_per_day=3)
    monkeypatch.setattr("pbi_cli.core.client.make_session", service.session)
    return service


@pytest.fixture
def ready(fake, cache_folder, signed_in):
    return cache_folder / "lake"


@pytest.fixture
def work(isolated_home):
    """A work lake that holds a few versions of the workspace list."""
    PBIConfig().cache_folder = str(isolated_home / "cache")
    world = World(isolated_home / "cache")
    for _ in range(3):
        world.run("groups", force=True)
        world.clock.advance(hours=1)
    return world


# ---------------------------------------------------------------------------
# pbi sync run --backup
# ---------------------------------------------------------------------------


def test_a_sync_run_copies_the_lake_before_it_writes(ready):
    assert pbi("sync", "run", "groups").exit_code == 0
    before = endpoints(ready)

    result = pbi("sync", "run", "activity", "--days", "2", "--backup")

    assert result.exit_code == 0, result.output
    [copy] = backups_next_to(ready)
    assert f"Backed up" in result.output and str(copy) in result.output
    assert endpoints(copy) == before  # what the lake held before the run
    assert "endpoint=admin_activityevents" in endpoints(ready)
    assert (copy / BACKUP_FILE).exists()


def test_a_backup_can_go_to_a_place_of_choice(ready, tmp_path):
    pbi("sync", "run", "groups")

    result = pbi("sync", "run", "groups", "--backup-to", str(tmp_path / "kept"))

    assert result.exit_code == 0, result.output
    assert (tmp_path / "kept" / BACKUP_FILE).exists()
    assert backups_next_to(ready) == []


def test_the_command_that_continues_a_run_does_not_copy_the_lake_again(fake, ready):
    pbi("sync", "run", "groups")
    fake.expire_token_after(1)

    result = pbi("sync", "run", "apps", "reports", "--force", "--backup")

    assert result.exit_code == 1
    assert "run `pbi sync run apps reports --force` again" in result.output


def test_a_plan_file_run_copies_the_lake_before_its_first_step(ready, tmp_path):
    token = make_token(tenant="tenant-1", expires_in=timedelta(days=36500), oid="o")
    store_token(token, "adm", "admin")
    pbi("sync", "run", "groups")
    plan = tmp_path / "pbi-plan.yaml"
    plan.write_text("version: 1\ntenant: {targets: [groups, apps]}\n", encoding="utf-8")

    result = pbi("sync", "run", "--config", str(plan), "--backup")

    assert result.exit_code == 0, result.output
    [copy] = backups_next_to(ready)
    assert result.output.index("Backed up") < result.output.index("Step 1")
    assert "endpoint=admin_apps" not in endpoints(copy)
    assert "endpoint=admin_apps" in endpoints(ready)


def test_the_first_run_has_nothing_to_back_up(ready):
    result = pbi("sync", "run", "groups", "--backup")

    assert result.exit_code == 0, result.output
    assert "nothing to back up" in result.output


def test_a_backup_that_cannot_be_made_stops_the_run_before_it_writes(ready, tmp_path):
    pbi("sync", "run", "groups")
    taken = tmp_path / "taken"
    taken.mkdir()
    (taken / "mine.txt").write_text("x", encoding="utf-8")
    before = {p for p in ready.rglob("*")}

    result = pbi("sync", "run", "activity", "--backup-to", str(taken))

    assert result.exit_code == 1
    assert "holds something already" in result.output
    assert {p for p in ready.rglob("*")} == before  # nothing was written


# ---------------------------------------------------------------------------
# pbi lake prune --backup
# ---------------------------------------------------------------------------


def test_prune_copies_every_version_before_it_deletes(work):
    root = work.store.root
    params = work.store.parameter_sets("tenant-1", "admin.groups")[0].params

    result = pbi("lake", "prune", "--yes", "--backup")

    assert result.exit_code == 0, result.output
    [copy] = backups_next_to(root)
    assert len(LakeStore(copy).versions("tenant-1", "admin.groups", params)) == 3
    assert len(work.store.versions("tenant-1", "admin.groups", params)) == 1


# ---------------------------------------------------------------------------
# pbi lake publish --backup
# ---------------------------------------------------------------------------


def test_publishing_again_can_copy_the_earlier_publish_first(work, tmp_path):
    shared = tmp_path / "shared"
    assert pbi("lake", "publish", str(shared), "--yes").exit_code == 0

    result = pbi("lake", "publish", str(shared), "--yes", "--backup")

    assert result.exit_code == 0, result.output
    [copy] = backups_next_to(shared)
    assert (copy / PUBLISH_FILE).exists() and (copy / BACKUP_FILE).exists()


def test_a_first_publish_makes_no_backup_and_gives_no_tip(work, tmp_path):
    result = pbi("lake", "publish", str(tmp_path / "shared"), "--yes")

    assert result.exit_code == 0, result.output
    assert "Tip:" not in result.output and "Backing up" not in result.output


def test_publishing_again_to_s3_without_a_backup_gives_a_tip(work, local_s3):
    assert pbi("lake", "publish", "s3://bucket/pbi-lake", "--yes").exit_code == 0

    result = pbi("lake", "publish", "s3://bucket/pbi-lake", "--yes")

    assert result.exit_code == 0, result.output
    assert "Tip: --backup copies the lake" in result.output


# ---------------------------------------------------------------------------
# pbi lake backup
# ---------------------------------------------------------------------------


def test_the_work_lake_is_backed_up_next_to_it(work):
    result = pbi("lake", "backup")

    assert result.exit_code == 0, result.output
    [copy] = backups_next_to(work.store.root)
    assert f"pbi tui --lake {copy}" in result.output
    assert endpoints(copy) == endpoints(work.store.root)


def test_a_lake_in_s3_is_backed_up_where_asked(work, local_s3):
    pbi("lake", "publish", "s3://bucket/pbi-lake", "--yes")

    result = pbi(
        "lake", "backup", "--lake", "s3://bucket/pbi-lake", "--to", "s3://bucket/b1"
    )

    assert result.exit_code == 0, result.output
    assert "to s3://bucket/b1" in result.output
    assert LakeStore(CloudPath("s3://bucket/b1")).tenants() == ["tenant-1"]


def test_without_a_lake_there_is_nothing_to_back_up(isolated_home):
    result = pbi("lake", "backup")

    assert result.exit_code == 1 and "no data lake yet" in result.output


# ---------------------------------------------------------------------------
# pbi lake restore
# ---------------------------------------------------------------------------


def lake_files(root):
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.fixture
def damaged(work, tmp_path):
    """A work lake with a backup of how it was, and then a sync that went wrong."""
    root = work.store.root
    assert pbi("lake", "backup").exit_code == 0
    [saved] = backups_next_to(root)
    before = lake_files(root)
    gone = sorted(root.rglob("data.json"))[0]
    gone.unlink()
    (root / "stray.json").write_text("{}", encoding="utf-8")
    return root, saved, before


def test_a_restore_puts_the_lake_back_and_keeps_a_safety_copy(damaged):
    root, saved, before = damaged

    result = pbi("lake", "restore", str(saved), "--yes")

    assert result.exit_code == 0, result.output
    assert lake_files(root) == before
    assert "Restored:" in result.output and "1 deleted" in result.output
    [safety] = sorted(root.parent.glob(f"{root.name}-before-restore-*"))
    assert (safety / "stray.json").exists()  # the lake as it was before the restore
    assert str(safety) in result.output


def test_it_shows_what_it_would_do_and_asks_first(damaged):
    root, saved, before = damaged
    now = lake_files(root)

    result = pbi("lake", "restore", str(saved), input="n\n")

    assert result.exit_code == 1
    assert "copy" in result.output and "delete 1 file(s)" in result.output
    assert "Replace the lake" in result.output
    assert lake_files(root) == now  # a no is a no
    assert not list(root.parent.glob(f"{root.name}-before-restore-*"))


def test_the_safety_copy_can_be_left_out(damaged):
    root, saved, before = damaged

    result = pbi("lake", "restore", str(saved), "--yes", "--no-safety-copy")

    assert result.exit_code == 0, result.output
    assert lake_files(root) == before
    assert not list(root.parent.glob(f"{root.name}-before-restore-*"))


def test_files_can_be_kept_that_the_backup_does_not_have(damaged):
    root, saved, before = damaged

    result = pbi("lake", "restore", str(saved), "--yes", "--keep-extra")

    assert result.exit_code == 0, result.output
    assert (root / "stray.json").exists()


def test_an_incomplete_backup_is_refused_unless_asked(damaged):
    root, saved, before = damaged
    (saved / BACKUP_FILE).unlink()

    refused = pbi("lake", "restore", str(saved), "--yes")
    allowed = pbi("lake", "restore", str(saved), "--yes", "--allow-incomplete")

    assert refused.exit_code == 1 and "no backup.json" in refused.output
    assert allowed.exit_code == 0 and "may be incomplete" in allowed.output
    assert lake_files(root) == before


def test_a_published_lake_is_not_restored_over(work, tmp_path):
    root = work.store.root
    pbi("lake", "backup")
    [saved] = backups_next_to(root)
    LakeStore(root, publishing=True).write_marker(
        PublishInfo(
            published_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
            published_by="ana@laptop",
        )
    )

    result = pbi("lake", "restore", str(saved), "--yes")

    assert result.exit_code == 1 and "published by ana@laptop" in result.output


def test_without_a_cache_folder_there_is_no_lake_to_restore_into(
    isolated_home, tmp_path
):
    result = pbi("lake", "restore", str(tmp_path), "--yes")

    assert result.exit_code == 1 and "no data lake yet" in result.output


def test_a_backup_in_s3_is_restored_into_the_work_lake(work, local_s3):
    root = work.store.root
    before = lake_files(root)
    pbi("lake", "backup", "--to", "s3://bucket/saved")
    (root / "stray.json").write_text("{}", encoding="utf-8")

    result = pbi("lake", "restore", "s3://bucket/saved", "--yes", "--no-safety-copy")

    assert result.exit_code == 0, result.output
    assert lake_files(root) == before
