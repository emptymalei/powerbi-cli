"""The people of every item of a kind, read from the lake."""

import pytest
from sync_helpers import TENANT, World

from pbi_cli.core.accessreport import (
    ACCESS_KINDS,
    COLUMNS,
    AccessReport,
    AccessRow,
    access_report,
)
from pbi_cli.core.catalog import Access, Catalog, UsersView
from pbi_cli.core.scan import ScanFlags

FULL = ScanFlags(lineage=True, datasource_details=True, get_artifact_users=True)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def catalog_of(world):
    return Catalog(world.store, TENANT, clock=world.clock.now)


def test_every_person_of_every_report_is_a_row(world):
    world.run("groups", "report-users")
    catalog = catalog_of(world)

    found = access_report(catalog, "report")

    reports = catalog.all_items("report")
    assert found.items == len(reports) > 1
    assert found.missing == []
    assert {row.item_id for row in found.rows} == {r.id for r in reports}
    one = next(row for row in found.rows if row.item_id == "rep-0001")
    assert one.kind == "report" and one.source == "admin.reports.users"
    assert one.person.email and one.workspace_id.startswith("ws-")
    assert one.workspace  # the name of the workspace, not only its id
    assert len(one.cells()) == len(COLUMNS)
    assert found.table[0] == found.rows[0].cells()


def test_the_rows_come_by_item_and_then_by_person(world):
    world.run("report-users")
    found = access_report(catalog_of(world), "report")

    keys = [(r.item.casefold(), r.item_id) for r in found.rows]
    assert keys == sorted(keys)


def test_the_people_of_an_item_come_in_name_order(world, monkeypatch):
    world.run("groups", "reports")
    catalog = catalog_of(world)
    unsorted = UsersView(
        [
            Access("Zed", "z@x.com", "Member", "User"),
            Access("amy", "a@x.com", "Admin", "User"),
        ],
        "scan",
    )
    monkeypatch.setattr(Catalog, "users", lambda self, subject, state=None: unsorted)

    found = access_report(catalog, "report", ["ws-0001"])

    first = [r for r in found.rows if r.item_id == found.rows[0].item_id]
    assert [r.person.name for r in first] == ["amy", "Zed"]


def test_items_whose_users_the_lake_does_not_hold_are_named_not_hidden(world):
    world.run("groups", "reports")  # the reports, but nobody who can open them
    catalog = catalog_of(world)

    found = access_report(catalog, "report")

    assert found.rows == []
    assert found.items == len(found.missing) == len(catalog.all_items("report"))
    assert "(" in found.missing[0]  # name (workspace)


def test_a_workspace_can_be_chosen_by_id_or_by_name(world):
    world.run("groups", "report-users")
    catalog = catalog_of(world)
    workspace = catalog.workspaces()[0]
    in_it = {
        i.id for i in catalog.all_items("report") if i.workspace_id == workspace.id
    }

    by_id = access_report(catalog, "report", [workspace.id])
    by_name = access_report(catalog, "report", [workspace.name.upper()])

    assert {r.item_id for r in by_id.rows} == in_it
    assert by_id.rows == by_name.rows


def test_a_workspace_that_is_not_there_is_an_error(world):
    world.run("report-users")

    with pytest.raises(ValueError, match="no workspace 'nope'"):
        access_report(catalog_of(world), "report", ["nope"])


def test_the_people_of_workspaces(world):
    world.run("groups")
    world.run("scan", scan_flags=FULL)  # a scan lists the users of each workspace
    catalog = catalog_of(world)

    found = access_report(catalog, "workspace")

    assert found.items == len(catalog.workspaces())
    assert found.rows and all(r.kind == "workspace" for r in found.rows)
    assert all(r.item_id == r.workspace_id for r in found.rows)


def test_the_people_of_apps(world):
    world.run("apps", "app-users")

    found = access_report(catalog_of(world), "app")

    assert found.rows and all(r.workspace == "" for r in found.rows)
    assert {r.source for r in found.rows} == {"admin.apps.users"}


def test_people_counts_each_person_once():
    ann = Access("Ann", "Ann@Example.com", "Owner", "User")
    again = Access("Ann", "ann@example.COM", "Member", "User")  # the case may differ
    rows = [
        AccessRow("report", "R1", "r1", "", "", ann, "scan"),
        AccessRow("report", "R2", "r2", "", "", again, "scan"),
    ]
    nameless = Access("Group", "", "Member", "Group")
    rows.append(AccessRow("report", "R3", "r3", "", "", nameless, "scan"))

    assert AccessReport(rows=rows).people == 1  # the same person on two reports


def test_an_unknown_kind_is_an_error(world):
    world.run("report-users")

    with pytest.raises(ValueError, match="Unknown kind 'table'"):
        access_report(catalog_of(world), "table")
    assert "workspace" in ACCESS_KINDS and "report" in ACCESS_KINDS
