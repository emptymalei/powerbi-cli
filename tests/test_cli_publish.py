"""``pbi lake publish``: a complete copy of the lake that others can open."""

import pytest
from sync_helpers import TENANT, World
from typer.testing import CliRunner

from pbi_cli.cli import app
from pbi_cli.config import PBIConfig
from pbi_cli.core.scan import ScanFlags
from pbi_cli.core.store import PUBLISH_FILE, LakeStore

FULL = ScanFlags(lineage=True, datasource_details=True, get_artifact_users=True)


def lake(*args, input=None):
    return CliRunner().invoke(app, ["lake", *args], input=input)


@pytest.fixture
def work(isolated_home):
    """The work lake: the cache folder is set, and its lake holds every kind of data."""
    PBIConfig().cache_folder = str(isolated_home / "cache")
    world = World(isolated_home / "cache")  # its lake is cache/lake
    world.run()
    world.run("scan", scan_flags=FULL)
    world.run("activity", days=2)
    world.run("report-users")
    return world


@pytest.fixture
def destination(tmp_path):
    return tmp_path / "shared"


def test_a_dry_run_shows_what_would_be_published_and_writes_nothing(work, destination):
    result = lake("publish", str(destination), "--dry-run")

    assert result.exit_code == 0, result.output
    assert f"From: {work.store.root}" in result.output
    assert f"To:   {destination} (empty)" in result.output
    assert "Tenants: tenant-1" in result.output
    for category in ("lists", "scans", "users", "activity"):
        assert category in result.output
    assert "⚠ the people who can open each report" in result.output
    assert "IP addresses" in result.output
    assert "Dry run: nothing was written." in result.output
    assert not destination.exists()


def test_it_asks_before_it_writes_and_no_means_no(work, destination):
    result = lake("publish", str(destination), input="n\n")

    assert result.exit_code == 1 and "Publish this to" in result.output
    assert not destination.exists()


def test_answering_yes_publishes(work, destination):
    result = lake("publish", str(destination), input="y\n")

    assert result.exit_code == 0, result.output
    assert LakeStore(destination).published() is not None


def test_yes_publishes_without_asking_and_says_how_to_open_it(work, destination):
    result = lake("publish", str(destination), "--yes")

    assert result.exit_code == 0, result.output
    assert "Publish this to" not in result.output
    assert f"✓ Published to {destination}:" in result.output
    assert "0 already there" in result.output
    assert f"Others open it with: pbi tui --lake {destination}" in result.output
    assert (destination / PUBLISH_FILE).is_file()


def test_the_published_lake_opens_with_lake_and_needs_no_cache_folder(
    work, destination
):
    lake("publish", str(destination), "--yes")
    PBIConfig().set("cache_folder", None)

    result = lake("ls", "--lake", str(destination))

    assert result.exit_code == 0, result.output
    assert f"Data lake: {destination}" in result.output and TENANT in result.output


def test_publishing_again_says_what_was_there_already(work, destination):
    lake("publish", str(destination), "--yes")

    result = lake("publish", str(destination), "--yes")

    assert result.exit_code == 0, result.output
    assert "already there" in result.output and "0 already there" not in result.output
    assert "(published by" in lake("publish", str(destination), "--dry-run").output


def test_categories_can_be_left_out(work, destination):
    result = lake(
        "publish", str(destination), "-x", "activity", "--exclude", "users", "--yes"
    )

    assert result.exit_code == 0, result.output
    tenant_dir = destination / f"tenant={TENANT}"
    assert not (tenant_dir / "endpoint=admin_activityevents").exists()
    assert not (tenant_dir / "endpoint=admin_reports_users").exists()
    assert (tenant_dir / "endpoint=admin_scan_result").is_dir()
    assert (
        "activity (left out)"
        in lake("publish", str(destination), "-x", "activity", "--dry-run").output
    )


def test_a_category_that_does_not_exist_is_an_error(work, destination):
    result = lake("publish", str(destination), "-x", "everything", "--yes")

    assert result.exit_code == 1 and "Cannot leave out 'everything'" in result.output
    assert "scans, users, datasources, activity" in result.output
    assert not destination.exists()


def test_the_lake_cannot_be_published_onto_itself(work):
    result = lake("publish", str(work.store.root), "--yes")

    assert result.exit_code == 1 and "is the lake that is published" in result.output


def test_a_folder_with_other_things_is_not_published_into(work, destination):
    destination.mkdir()
    (destination / "notes.txt").write_text("mine", encoding="utf-8")

    result = lake("publish", str(destination), "--yes")

    assert result.exit_code == 1 and "not a published lake" in result.output
    assert sorted(p.name for p in destination.iterdir()) == ["notes.txt"]


def test_a_lake_that_someone_else_published_needs_force(work, destination, monkeypatch):
    monkeypatch.setattr("pbi_cli.core.publish.publisher_name", lambda: "ana@laptop")
    lake("publish", str(destination), "--yes")
    monkeypatch.setattr("pbi_cli.core.publish.publisher_name", lambda: "bob@desk")

    refused = lake("publish", str(destination), "--yes")
    forced = lake("publish", str(destination), "--yes", "--force")

    assert refused.exit_code == 1 and "published by ana@laptop" in refused.output
    assert "--force" in refused.output
    assert forced.exit_code == 0, forced.output
    assert LakeStore(destination).published().published_by == "bob@desk"


def test_prune_and_history_are_not_given_together(work, destination):
    result = lake("publish", str(destination), "--prune", "--history", "--yes")

    assert result.exit_code == 1 and "cannot go with --history" in result.output


def test_another_lake_can_be_the_one_that_is_published(
    isolated_home, tmp_path, destination
):
    other = World(tmp_path / "other")
    other.run("groups")

    result = lake("publish", str(destination), "--yes", "--lake", str(other.store.root))

    assert result.exit_code == 0, result.output
    assert (
        f"From: {other.store.root}"
        in lake(
            "publish", str(destination), "--dry-run", "--lake", str(other.store.root)
        ).output
    )
    assert LakeStore(destination).tenants() == [TENANT]


def test_without_a_lake_there_is_nothing_to_publish(isolated_home, destination):
    result = lake("publish", str(destination), "--yes")

    assert result.exit_code == 1 and "There is no data lake yet" in result.output


def test_a_tenant_the_lake_does_not_hold_is_an_error(work, destination):
    result = lake("publish", str(destination), "-t", "other", "--yes")

    assert result.exit_code == 1 and "no data of tenant 'other'" in result.output


def test_a_published_lake_cannot_be_pruned_or_written_to(work, destination):
    lake("publish", str(destination), "--yes")

    result = lake("prune", "--yes", "--lake", str(destination))

    assert result.exit_code == 1 and "only reads" in result.output


def test_the_destination_can_be_in_s3(work, local_s3):
    result = lake("publish", "s3://bucket/shared", "--yes")

    assert result.exit_code == 0, result.output
    assert "Published to s3://bucket/shared:" in result.output
    listed = lake("ls", "--lake", "s3://bucket/shared")
    assert listed.exit_code == 0 and TENANT in listed.output
