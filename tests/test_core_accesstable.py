"""The access table: who has access to what, worked out from the lake once and kept in it,
so that a question reads one file instead of the users of every item."""

import pytest
from sync_helpers import TENANT, World
from typer.testing import CliRunner

from pbi_cli import cli_sync
from pbi_cli.cli import app
from pbi_cli.config import PBIConfig
from pbi_cli.core import accessreport
from pbi_cli.core import catalog as accessreport_catalog
from pbi_cli.core.accessreport import (
    TABLE_VERSION,
    access_of,
    access_report,
    access_table,
    build_access_table,
    load_access_table,
)
from pbi_cli.core.catalog import Catalog
from pbi_cli.core.scan import ScanFlags
from pbi_cli.core.store import STALE, TABLES, LakeStore

USERS = ScanFlags(get_artifact_users=True)


@pytest.fixture
def world(tmp_path):
    world = World(tmp_path)
    world.run("groups", "reports", "report-users", "apps", "app-users")
    return world


def catalog_of(world, store=None):
    return Catalog(store or world.store, TENANT, clock=world.clock.now)


def tables(world):
    return world.store.root / f"tenant={TENANT}" / TABLES


def never_built(monkeypatch):
    def fail(catalog):
        raise AssertionError("the table was worked out again")

    monkeypatch.setattr(accessreport, "build_access_table", fail)


# ---------------------------------------------------------------------------
# kept in the lake
# ---------------------------------------------------------------------------


def test_the_first_question_works_it_out_and_keeps_it_in_the_lake(world):
    said = []

    found = access_report(catalog_of(world), "report", on_build=lambda: said.append(1))

    assert said == [1]
    assert found.rows and found.missing == []
    assert {p.name for p in tables(world).iterdir()} == {
        "access.csv",
        "access-gaps.csv",
        "access.json",
    }
    assert not world.store.tables_stale(TENANT)


def test_the_next_question_reads_the_kept_table(world, monkeypatch):
    first = access_report(catalog_of(world), "report")
    never_built(monkeypatch)
    said = []

    again = access_report(catalog_of(world), "report", on_build=lambda: said.append(1))

    assert said == []
    assert again.rows == first.rows
    assert again.items == first.items and again.missing == first.missing


def test_the_kept_table_gives_what_working_it_out_gives(world):
    world.run("scan", scan_flags=USERS)  # some users only from the scan
    catalog = catalog_of(world)
    made = build_access_table(catalog)
    access_table(catalog)  # kept

    kept = load_access_table(catalog_of(world))

    assert kept is not None
    assert kept.rows == made.rows and kept.gaps == made.gaps


def test_one_catalog_reads_the_table_once(world, monkeypatch):
    catalog = catalog_of(world)
    access_report(catalog, "report")
    reads = []
    monkeypatch.setattr(
        accessreport, "load_access_table", lambda c: reads.append(1) or None
    )

    access_report(catalog, "app")
    access_of(catalog, "ann@example.com")

    assert reads == []


def test_a_catalog_that_reads_the_lake_again_reads_the_table_again(world):
    catalog = catalog_of(world)
    before = access_report(catalog, "report")
    world.run("dataset-users", force=True)

    catalog.reload()

    assert access_report(catalog, "dataset").rows
    assert access_report(catalog, "report").rows == before.rows


# ---------------------------------------------------------------------------
# stale after a snapshot
# ---------------------------------------------------------------------------


def test_a_new_snapshot_makes_the_table_stale_and_the_next_question_redoes_it(world):
    access_report(catalog_of(world), "report")
    assert not world.store.tables_stale(TENANT)

    world.run("dataset-users")

    assert world.store.tables_stale(TENANT)
    said = []
    found = access_report(catalog_of(world), "dataset", on_build=lambda: said.append(1))
    assert said == [1] and found.rows
    assert not world.store.tables_stale(TENANT)


def test_a_snapshot_written_while_the_table_is_worked_out_leaves_it_stale(
    world, monkeypatch
):
    real = accessreport.build_access_table

    def build_while_a_sync_writes(catalog):
        table = real(catalog)
        world.run("dataset-users")  # lands after the users were read
        return table

    monkeypatch.setattr(accessreport, "build_access_table", build_while_a_sync_writes)

    access_report(catalog_of(world), "report")

    assert world.store.tables_stale(TENANT)


def test_a_table_of_another_version_is_worked_out_again(world):
    access_report(catalog_of(world), "report")
    manifest = tables(world) / "access.json"
    manifest.write_text(
        manifest.read_text().replace(
            f'"version": {TABLE_VERSION}', f'"version": {TABLE_VERSION + 1}'
        )
    )

    assert load_access_table(catalog_of(world)) is None


def test_a_broken_table_is_worked_out_again(world):
    access_report(catalog_of(world), "report")
    (tables(world) / "access.json").write_text("{not json")
    said = []

    found = access_report(catalog_of(world), "report", on_build=lambda: said.append(1))

    assert said == [1] and found.rows


def test_a_table_without_its_manifest_is_not_taken(world):
    access_report(catalog_of(world), "report")
    (tables(world) / "access.json").unlink()

    assert load_access_table(catalog_of(world)) is None


# ---------------------------------------------------------------------------
# a lake that is only read
# ---------------------------------------------------------------------------


def test_a_lake_that_is_only_read_works_it_out_in_memory(world):
    readonly = LakeStore(world.store.root, readonly=True, reason="only read")

    found = access_report(catalog_of(world, readonly), "report")

    assert found.rows
    assert not tables(world).joinpath("access.json").exists()


def test_a_lake_that_is_only_read_uses_a_table_that_is_there(world, monkeypatch):
    access_report(catalog_of(world), "report")
    never_built(monkeypatch)
    readonly = LakeStore(world.store.root, readonly=True, reason="only read")

    assert access_report(catalog_of(world, readonly), "report").rows


# ---------------------------------------------------------------------------
# the users that only a scan holds
# ---------------------------------------------------------------------------


def test_the_users_of_items_that_only_a_scan_holds_are_found(tmp_path):
    world = World(tmp_path)
    world.run("groups", "reports", "scan", scan_flags=USERS)

    found = access_of(catalog_of(world), "ann@example.com", kinds=["report"])

    assert found.rows and {r.source for r in found.rows} == {"scan"}
    assert {r.item_id for r in found.rows} == {r["id"] for r in world.fake.reports}
    assert found.missing == []


def test_the_walk_reads_each_stored_scan_once(tmp_path, monkeypatch):
    world = World(tmp_path, workspaces=250, reports=750)
    world.run("groups", "reports", "scan", scan_flags=USERS)
    catalog = catalog_of(world)
    reads = []
    real = accessreport_catalog.split_scan_result

    def counting(data):
        reads.append(1)
        return real(data)

    monkeypatch.setattr(accessreport_catalog, "split_scan_result", counting)

    build_access_table(catalog)

    # 250 workspaces are three stored scans; each kind that is in a scan walks them once
    assert len(reads) <= 3 * len(accessreport.ACCESS_KINDS)


# ---------------------------------------------------------------------------
# from the command line
# ---------------------------------------------------------------------------


@pytest.fixture
def work(isolated_home):
    PBIConfig().cache_folder = str(isolated_home / "cache")
    world = World(isolated_home / "cache")
    world.run("groups", "reports", "report-users")
    return world


def lake(*args):
    return CliRunner().invoke(app, ["lake", *args])


def test_pbi_lake_access_says_when_it_works_the_table_out(work):
    first = lake("access", "ann-rep-0001@example.com")
    again = lake("access", "ann-rep-0001@example.com")

    assert first.exit_code == 0 and again.exit_code == 0, first.output
    assert "Working out who has access" in first.output
    assert "Working out who has access" not in again.output
    assert "Report 1" in again.output


def test_a_sync_works_the_table_out_at_its_end(work, capsys):
    work.run("dataset-users")
    assert work.store.tables_stale(TENANT)

    cli_sync._update_tables(work.store)

    assert not work.store.tables_stale(TENANT)
    assert "Updated who has access to what in the lake" in capsys.readouterr().out
    assert "Working out" not in lake("access", "ann-rep-0001@example.com").output


def test_a_sync_whose_table_cannot_be_worked_out_still_ends_well(
    work, capsys, monkeypatch
):
    work.run("dataset-users")

    def broken(catalog):
        raise ValueError("a broken file")

    monkeypatch.setattr(cli_sync, "refresh_access_table", broken)

    cli_sync._update_tables(work.store)

    assert "Could not update who has access" in capsys.readouterr().out
    assert work.store.tables_stale(TENANT)


def test_the_stale_mark_is_a_file_of_the_tables_folder(work):
    assert (work.store.root / f"tenant={TENANT}" / TABLES / STALE).exists()
