"""``--with-work-lake``: a shared lake with the work lake on top, from the command line, a plan
file and the terminal UI."""

import pytest
from sync_helpers import TENANT, World
from tui_helpers import backend_of, run_ui
from typer.testing import CliRunner

from pbi_cli import cli_tui
from pbi_cli.cli import app
from pbi_cli.config import PBIConfig
from pbi_cli.core.catalog import Catalog
from pbi_cli.core.layers import LayeredStore
from pbi_cli.core.planfile import PlanFile, PlanFileError
from pbi_cli.core.publish import plan_publish, publish
from pbi_cli.core.store import LakeStore
from pbi_cli.errors import PBIError
from pbi_cli.session import resolve_lake
from pbi_cli.tui import render
from pbi_cli.tui.app import ON_TOP
from pbi_cli.tui.commands import commands_for


@pytest.fixture
def shared(tmp_path):
    """A lake that a colleague synced and published: workspaces, reports and their users."""
    colleague = World(tmp_path / "colleague")
    colleague.run("groups", "reports", "report-users")
    plan = plan_publish(
        colleague.store, LakeStore(tmp_path / "shared"), publisher="ana@laptop"
    )
    publish(plan, now=colleague.clock.now())
    return tmp_path / "shared"


@pytest.fixture
def mine(isolated_home):
    """The work lake (the cache folder's), with what the person's own account fetched."""
    PBIConfig().cache_folder = str(isolated_home / "cache")
    world = World(isolated_home / "cache")  # its lake is cache/lake
    world.clock.advance(days=1)
    world.run("apps", "app-users")
    return world


def lake(*args):
    return CliRunner().invoke(app, ["lake", *args])


# ---------------------------------------------------------------------------
# choosing the lake
# ---------------------------------------------------------------------------


def test_the_lake_asked_for_is_opened_with_the_work_lake_on_top(shared, mine):
    opened = resolve_lake(str(shared), environ={}, with_work_lake=True)

    assert opened.layered and not opened.work
    assert isinstance(opened.store, LayeredStore)
    assert opened.store.root == shared
    assert opened.store.work.root == mine.store.root
    assert opened.store.writable  # what is fetched goes to the work lake


def test_without_a_work_lake_it_says_how_to_get_one(shared, isolated_home):
    with pytest.raises(PBIError) as refused:
        resolve_lake(str(shared), environ={}, with_work_lake=True)

    assert "set-cache-folder" in str(refused.value)


def test_the_work_lake_on_top_of_itself_is_the_work_lake(mine):
    opened = resolve_lake(str(mine.store.root), environ={}, with_work_lake=True)

    assert opened.work and not opened.layered


def test_without_with_work_lake_the_shared_lake_is_only_read(shared, mine):
    opened = resolve_lake(str(shared), environ={})

    assert not opened.layered and opened.readonly


# ---------------------------------------------------------------------------
# pbi lake
# ---------------------------------------------------------------------------


def test_lake_ls_says_which_lake_each_answer_is_in(shared, mine):
    result = lake("ls", "--lake", str(shared), "--with-work-lake")

    assert result.exit_code == 0, result.output
    assert "with your work lake on top" in result.output
    assert "LAKE" in result.output
    lines = result.output.splitlines()
    assert any("admin.reports" in line and "shared" in line for line in lines)
    assert any("admin.apps" in line and "work" in line for line in lines)


def test_lake_users_answers_from_both_lakes(shared, mine):
    apps = lake("users", "app", "--lake", str(shared), "--with-work-lake")
    reports = lake("users", "report", "--lake", str(shared), "--with-work-lake")
    shared_alone = lake("users", "app", "--lake", str(shared))

    assert apps.exit_code == 0, apps.output
    assert reports.exit_code == 0, reports.output
    assert shared_alone.exit_code == 1  # the shared lake holds no app users


# ---------------------------------------------------------------------------
# a plan file
# ---------------------------------------------------------------------------


def test_a_plan_file_puts_the_work_lake_on_top(tmp_path):
    plan = PlanFile.parse(
        "version: 1\nsession: {lake: shared, with_work_lake: true}\n",
        tmp_path / "plan.yaml",
    )

    assert plan.session.with_work_lake and plan.lake.endswith("shared")


def test_a_plan_file_with_work_lake_needs_a_lake(tmp_path):
    with pytest.raises(PlanFileError) as refused:
        PlanFile.parse(
            "version: 1\nsession: {with_work_lake: true}\n", tmp_path / "plan.yaml"
        )

    assert "session.with_work_lake" in str(refused.value)
    assert "session.lake" in str(refused.value)


def test_pbi_tui_opens_the_lake_with_the_work_lake_on_top(shared, mine, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli_tui, "run", lambda backend, tenant=None: seen.update(b=backend)
    )
    monkeypatch.setattr(cli_tui, "textual_available", lambda: True)

    result = CliRunner().invoke(app, ["tui", "--lake", str(shared), "--with-work-lake"])

    assert result.exit_code == 0, result.output
    backend = seen["b"]
    assert backend.layered and not backend.readonly
    assert backend.written.root == mine.store.root
    layered = backend.open_lake(str(shared), with_work_lake=True)
    assert isinstance(layered, LayeredStore)
    assert not isinstance(backend.open_lake(str(shared)), LayeredStore)


# ---------------------------------------------------------------------------
# the terminal UI
# ---------------------------------------------------------------------------


def layered_backend(shared, mine, **replace):
    store = LayeredStore(mine.store, LakeStore(shared, readonly=True))
    return backend_of(
        mine,
        store=store,
        work_lake=str(mine.store.root),
        open_lake=lambda *a, **k: store,
        **replace,
    )


def test_a_sync_in_the_tui_reads_and_writes_the_work_lake_alone(shared, mine):
    backend = layered_backend(shared, mine, make_engine=None)

    assert backend.written is mine.store
    assert backend.engine()._store is mine.store
    assert backend.store.why_read_only() == ""


def test_the_header_and_the_palette_say_the_work_lake_is_on_top(shared, mine):
    async def scenario(ui):
        return ui.app.lake_label, [c.title for c in commands_for(ui.app, ui.app.screen)]

    label, titles = run_ui(layered_backend(shared, mine), scenario, tenant=TENANT)

    assert label.endswith(ON_TOP)
    assert (
        "Take the work lake off" in titles and "Put the work lake on top" not in titles
    )


def test_a_shared_lake_alone_offers_to_put_the_work_lake_on_top(shared, mine):
    opened = []
    alone = LakeStore(shared, readonly=True, reason="only reads")
    backend = backend_of(
        mine,
        store=alone,
        work_lake=str(mine.store.root),
        open_lake=lambda place, with_work_lake=False: opened.append(
            (place, with_work_lake)
        )
        or LayeredStore(mine.store, alone),
    )

    async def scenario(ui):
        titles = [c.title for c in commands_for(ui.app, ui.app.screen)]
        next(
            c
            for c in commands_for(ui.app, ui.app.screen)
            if c.title == "Put the work lake on top"
        ).run()
        await ui.settle()
        return titles, ui.app.backend.layered

    titles, layered = run_ui(backend, scenario, tenant=TENANT)

    assert "Put the work lake on top" in titles
    assert opened == [(str(shared), True)] and layered


def test_the_explorer_names_the_lake_of_each_list(shared, mine):
    store = LayeredStore(mine.store, LakeStore(shared, readonly=True))
    catalog = Catalog(store, TENANT, clock=mine.clock.now)

    reports = catalog.listing("report")
    apps = catalog.listing("app")

    assert render.from_lake(catalog, reports.snapshot) == " · shared lake"
    assert render.from_lake(catalog, apps.snapshot) == " · your work lake"
    assert render.from_lake(Catalog(mine.store, TENANT), apps.snapshot) == ""
