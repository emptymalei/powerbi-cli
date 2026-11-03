"""Backing up the lake, downloading a report, and finding what a person can open: from the
command palette of the terminal UI."""

import csv
import io
from types import SimpleNamespace

import pytest
from sync_helpers import World
from textual.widgets import Input
from tui_helpers import backend_of, run_ui

from pbi_cli.core.backup import BACKUP_FILE
from pbi_cli.core.catalog import Item
from pbi_cli.core.reportexport import ExportedReport, ExportError
from pbi_cli.core.store import LakeStore
from pbi_cli.errors import TokenExpiredError
from pbi_cli.tui.commands import commands_for
from pbi_cli.tui.modals import AskModal, ExportModal, SignInModal


@pytest.fixture
def world(tmp_path):
    world = World(tmp_path)
    world.run()
    world.run("report-users", "app-users")
    return world


@pytest.fixture
def folder(tmp_path, monkeypatch):
    place = tmp_path / "work"
    place.mkdir()
    monkeypatch.chdir(place)
    return place


@pytest.fixture
def downloads(monkeypatch):
    """Stand in for Power BI: record the exports that are asked for, answer with a PDF."""
    asked = []
    answer = {"error": None}

    def fake_export(client, group_id, report_id, fmt, **options):
        asked.append((group_id, report_id, fmt))
        if answer["error"] is not None:
            raise answer["error"]
        return ExportedReport(b"%PDF-data", f".{fmt}", "Sales", fmt)

    monkeypatch.setattr("pbi_cli.tui.app.export_report", fake_export)
    asked_with = SimpleNamespace(asked=asked, fail=lambda e: answer.update(error=e))
    return asked_with


def titles(ui):
    return [c.title for c in commands_for(ui.app, ui.app.screen)]


def command(ui, title):
    return next(c for c in commands_for(ui.app, ui.app.screen) if c.title == title)


def notes(ui):
    return [n.message for n in ui.app._notifications]


def view_only(world):
    root = world.store.root
    return backend_of(
        world,
        store=LakeStore(root, readonly=True, reason="The lake only reads."),
        work_lake=str(root),
    )


# ---------------------------------------------------------------------------
# back up the lake
# ---------------------------------------------------------------------------


def test_the_lake_can_be_backed_up_from_the_palette(world):
    async def scenario(ui):
        command(ui, "Back up the lake").run()
        await ui.settle()
        return notes(ui)

    said = run_ui(backend_of(world), scenario)

    [copy] = sorted(world.store.root.parent.glob(f"{world.store.root.name}-backup-*"))
    assert (copy / BACKUP_FILE).exists()
    assert said[0] == "Backing up the lake …"
    assert said[1].startswith("Backed up ") and str(copy) in said[1]
    assert f"pbi lake restore {copy}" in said[1]


def test_a_lake_that_is_only_looked_at_is_not_backed_up(world):
    async def scenario(ui):
        assert "Back up the lake" not in titles(ui)
        ui.app.backup_now()  # whatever calls it: it refuses
        await ui.settle()
        return notes(ui)

    said = run_ui(view_only(world), scenario)

    assert said == ["The lake only reads."]
    assert not list(world.store.root.parent.glob("*-backup-*"))


def test_a_running_sync_is_not_copied_in_the_middle(world):
    async def scenario(ui):
        ui.app.run_state = SimpleNamespace(running=True, stopping=False)
        command(ui, "Back up the lake").run()
        await ui.settle()
        ui.app.run_state = None
        return notes(ui)

    said = run_ui(backend_of(world), scenario)

    assert said == ["A sync is running: stop it, or wait for it, then back up."]
    assert not list(world.store.root.parent.glob("*-backup-*"))


# ---------------------------------------------------------------------------
# download a report
# ---------------------------------------------------------------------------


def test_a_report_can_be_downloaded_in_several_formats(world, downloads):
    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        await ui.pick_row("report:rep-0001")
        found = [t for t in titles(ui) if t.startswith("Download")]
        await ui.pick_row("dataset:ds-0001")
        return found, [t for t in titles(ui) if t.startswith("Download")]

    on_report, on_dataset = run_ui(backend_of(world), scenario)

    assert on_report == [
        "Download this report as a .pbix file",
        "Download this report as a PDF",
        "Download this report as a PowerPoint presentation",
        "Download this report as images (a .zip)",
    ]
    assert on_dataset == []  # only a report can be downloaded


def test_a_download_is_saved_in_the_folder_and_not_over_an_earlier_one(
    world, downloads, folder
):
    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        await ui.pick_row("report:rep-0001")
        for _ in range(2):
            command(ui, "Download this report as a PDF").run()
            await ui.settle()
        return notes(ui), ui.app.export_folder

    said, remembered = run_ui(backend_of(world), scenario)

    assert downloads.asked == [("ws-0001", "rep-0001", "pdf")] * 2
    assert (folder / "Report 1.pdf").read_bytes() == b"%PDF-data"
    assert (folder / "Report 1 (2).pdf").read_bytes() == b"%PDF-data"
    assert f"Saved {folder / 'Report 1.pdf'}" in said
    assert "Asking Power BI for Report 1 as pdf … this can take a while." in said
    assert remembered == str(folder)


def test_a_download_is_made_with_the_user_account(world, downloads, folder):
    scopes = []

    def client_for(scope, profile=None):
        scopes.append(scope)
        return world.client_for(scope, profile)

    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        await ui.pick_row("report:rep-0001")
        download = command(ui, "Download this report as a PDF")
        scopes.clear()  # listing the palette probes the accounts: that is not the question
        download.run()
        await ui.settle()

    run_ui(backend_of(world, client_for=client_for), scenario)

    assert scopes == ["user"]  # a report is downloaded as somebody who can open it


def test_a_report_whose_workspace_the_lake_does_not_know_is_not_asked_for(
    world, downloads, folder
):
    lost = Item(
        kind="report", id="rep-x", name="Lost", workspace_id=None, raw={}, scan=None
    )

    async def scenario(ui):
        ui.app.download_report(lost, "pdf")
        await ui.settle()
        return notes(ui)

    said = run_ui(backend_of(world), scenario)

    assert said == ["The lake does not say which workspace this report is in."]
    assert downloads.asked == []


def test_a_pbix_does_not_say_it_takes_a_while(world, downloads, folder):
    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        await ui.pick_row("report:rep-0001")
        command(ui, "Download this report as a .pbix file").run()
        await ui.settle()
        return notes(ui)

    said = run_ui(backend_of(world), scenario)

    assert said[0] == "Asking Power BI for Report 1 as pbix …"


def test_a_download_that_fails_says_why_and_saves_nothing(world, downloads, folder):
    downloads.fail(ExportError("Power BI could not export the report."))

    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        await ui.pick_row("report:rep-0001")
        command(ui, "Download this report as a PDF").run()
        await ui.settle()
        return notes(ui)

    said = run_ui(backend_of(world), scenario)

    assert "Power BI could not export the report." in said
    assert list(folder.iterdir()) == []


def test_an_expired_token_asks_for_a_fresh_one(world, downloads, folder):
    downloads.fail(TokenExpiredError("expired", group="user", profile="user-nlm"))

    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        await ui.pick_row("report:rep-0001")
        command(ui, "Download this report as a PDF").run()
        await ui.settle()
        return isinstance(ui.app.screen, SignInModal)

    assert run_ui(backend_of(world), scenario) is True


def test_downloads_are_not_offered_on_a_lake_that_is_only_looked_at(world, downloads):
    async def scenario(ui):
        await ui.select("workspace", "ws-0001")
        await ui.pick_row("report:rep-0001")
        return [t for t in titles(ui) if t.startswith("Download")]

    assert run_ui(view_only(world), scenario) == []


# ---------------------------------------------------------------------------
# who has access to ...
# ---------------------------------------------------------------------------


async def ask(ui, who):
    command(ui, "Who has access to…").run()
    await ui.settle()
    assert isinstance(ui.app.screen, AskModal)
    ui.app.screen.query_one("#answer", Input).value = who
    await ui.press("enter")


def offered(ui):
    """What the export dialog says it will save (one choice: a line of text)."""
    screen = ui.app.screen
    assert isinstance(screen, ExportModal)
    return ui.static("#what-one", screen)


def test_what_a_person_can_open_is_offered_as_a_file(world, folder):
    async def scenario(ui):
        await ask(ui, "ANN-REP-0001@example.com")
        what = offered(ui)
        await ui.click("#save")
        return what, notes(ui)

    what, said = run_ui(backend_of(world), scenario)

    assert what == (
        "What 'ANN-REP-0001@example.com' can open: "
        "Access of ANN-REP-0001@example.com (1 row)"
    )
    target = folder / "Access of ANN-REP-0001@example.com.csv"
    rows = list(csv.DictReader(io.StringIO(target.read_text(encoding="utf-8-sig"))))
    assert [(r["Kind"], r["Item"], r["E-mail or id"]) for r in rows] == [
        ("Report", "Report 1", "ann-rep-0001@example.com")
    ]
    assert any(m.startswith("Saved Access of ANN-REP-0001") for m in said)


def test_a_part_of_a_name_finds_people_too_and_says_so(world, folder):
    async def scenario(ui):
        await ask(ui, "rep-0002")
        return offered(ui)

    what = run_ui(backend_of(world), scenario)

    assert what.startswith("What 'rep-0002' can open (part of a name):")


def test_nobody_found_is_said_not_shown_as_an_empty_dialog(world, folder):
    async def scenario(ui):
        await ask(ui, "nobody@nowhere.example")
        return type(ui.app.screen).__name__, notes(ui)

    screen, said = run_ui(backend_of(world), scenario)

    assert screen == "ExplorerScreen"
    assert any(
        "Nothing in the lake gives 'nobody@nowhere.example' access" in m for m in said
    )


def test_the_gaps_in_the_lake_are_said_because_the_answer_may_be_incomplete(
    world, folder
):
    async def scenario(ui):
        await ask(ui, "ann-rep-0001@example.com")
        return notes(ui)

    said = run_ui(backend_of(world), scenario)

    assert any(
        "have no users in the lake, so the answer may be incomplete" in m for m in said
    )


def test_it_works_on_a_lake_that_is_only_looked_at(world, folder):
    async def scenario(ui):
        assert "Who has access to…" in titles(ui)
        await ask(ui, "ann-rep-0001@example.com")
        return isinstance(ui.app.screen, ExportModal)

    assert run_ui(view_only(world), scenario) is True


def test_giving_up_the_question_asks_nothing(world):
    async def scenario(ui):
        command(ui, "Who has access to…").run()
        await ui.settle()
        await ui.press("escape")
        return type(ui.app.screen).__name__, notes(ui)

    assert run_ui(backend_of(world), scenario) == ("ExplorerScreen", [])
