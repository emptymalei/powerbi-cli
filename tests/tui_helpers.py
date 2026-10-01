"""Run the TUI against a fake Power BI service and read what is on the screen."""

import asyncio
import io
from typing import Any, Awaitable, Callable, List, Optional

import pytest
from rich.console import Console
from textual.widgets import DataTable, Static, Tree
from textual.worker import WorkerCancelled

from pbi_cli.core.sync.engine import SyncEngine
from pbi_cli.tui.app import PBIApp
from pbi_cli.tui.backend import Backend
from pbi_cli.tui.explorer import ExplorerScreen, NodeRef
from pbi_cli.tui.syncscreen import SyncScreen


def plain(renderable: Any) -> str:
    """The text of anything a widget can show."""
    if isinstance(renderable, str):
        return renderable
    console = Console(
        file=io.StringIO(),
        width=200,
        force_terminal=False,
        color_system=None,
        record=True,
    )
    console.print(renderable)
    return console.export_text().rstrip("\n")


def backend_of(world, **replace: Any) -> Backend:
    """A backend that works on a `World`: its lake, its clients, its engine and its clock."""
    if "client_for" in replace and "make_engine" not in replace:
        replace["make_engine"] = lambda: SyncEngine(
            replace["client_for"],
            world.store,
            clock=world.clock.now,
            sleep=world.clock.sleep,
            monotonic=world.clock.time,
        )
    settings: dict = dict(
        store=world.store,
        client_for=world.client_for,
        sign_in=lambda token, profile, group: None,
        active_profile=lambda group: "admin-nlm" if group == "admin" else "user-nlm",
        make_engine=lambda: world.engine,
        clock=world.clock.now,
    )
    settings.update(replace)
    return Backend(**settings)


class Ui:
    """What a test does with the running app."""

    def __init__(self, app: PBIApp, pilot: Any):
        self.app = app
        self.pilot = pilot

    # -- driving ----------------------------------------------------------------------------

    async def settle(self, sync: bool = False) -> None:
        """Wait until the workers are done and what they ordered has been drawn.

        A sync that is running is not waited for (a test may be holding it up) unless
        ``sync`` is true.
        """
        for _ in range(8):
            running = [
                w
                for w in self.app.workers
                if not w.is_finished and (sync or w.group != "sync")
            ]
            for worker in running:
                try:
                    await worker.wait()
                except (
                    WorkerCancelled
                ):  # a newer selection replaced it: that is the point
                    pass
            await self.pilot.pause()
            if not running:
                return

    async def until(self, condition: Callable[[], Any], seconds: float = 10.0) -> None:
        """Wait until ``condition()`` is true; fail the test if it is not in time."""
        for _ in range(int(seconds / 0.01)):
            if condition():
                return
            await asyncio.sleep(0.01)  # not pilot.pause: the app is never idle for long
        raise AssertionError(f"gave up waiting for {condition}")

    async def finish_sync(self) -> None:
        """Wait for the sync that is running to end, and for the screen to show it."""
        await self.settle(sync=True)
        state = self.app.run_state
        if state is not None and "sync" in self.app._installed_screens:
            screen = self.sync
            if screen.is_mounted:
                await self.until(lambda: screen._finished_state is state)
                await self.settle(sync=True)

    async def press(self, *keys: str) -> None:
        await self.pilot.press(*keys)
        await self.settle()

    async def type(self, text: str) -> None:
        await self.pilot.press(*text)
        await self.settle()

    async def click(self, selector: str) -> None:
        await self.pilot.click(selector)
        await self.settle()

    # -- the screens ------------------------------------------------------------------------

    @property
    def explorer(self) -> ExplorerScreen:
        screen = self.app.get_screen("explorer")
        assert isinstance(screen, ExplorerScreen)
        return screen

    @property
    def sync(self) -> SyncScreen:
        screen = self.app.get_screen("sync")
        assert isinstance(screen, SyncScreen)
        return screen

    async def select(self, kind: str, node_id: str = "") -> None:
        """Move the cursor of the tree to a node."""
        self.explorer._select(self.explorer._tree_nodes[NodeRef(kind, node_id)])
        await self.settle()

    async def pick_row(self, key: str) -> None:
        """Move the cursor of the table to a row, and focus the table."""
        table = self.explorer.query_one("#table", DataTable)
        table.focus()
        table.move_cursor(row=table.get_row_index(key))
        await self.settle()

    # -- reading ----------------------------------------------------------------------------

    def static(self, selector: str, screen: Optional[Any] = None) -> str:
        widget = (screen or self.app.screen).query_one(selector, Static)
        return plain(widget.content)

    def rows(
        self, selector: str = "#table", screen: Optional[Any] = None
    ) -> List[List[str]]:
        table = (screen or self.app.screen).query_one(selector, DataTable)
        return [
            [str(cell) for cell in table.get_row_at(i)] for i in range(table.row_count)
        ]

    def columns(
        self, selector: str = "#table", screen: Optional[Any] = None
    ) -> List[str]:
        table = (screen or self.app.screen).query_one(selector, DataTable)
        return [str(column.label) for column in table.columns.values()]

    def tree(self) -> List[str]:
        """The labels of the tree, depth first, indented by depth."""
        found: List[str] = []

        def walk(node: Any, depth: int) -> None:
            for child in node.children:
                found.append("  " * depth + str(child.label))
                walk(child, depth + 1)

        walk(self.explorer.query_one("#tree", Tree).root, 0)
        return found

    def log(self) -> str:
        """The text of the log of the Sync screen."""
        return plain(self.sync.query_one("#log", Static).content)


def run_ui(
    backend: Backend,
    scenario: Callable[[Ui], Awaitable[Any]],
    *,
    size: tuple = (140, 42),
    tenant: Optional[str] = None,
) -> Any:
    """Start the app, wait for it to read the lake, run ``scenario`` and return its result."""

    async def main() -> Any:
        app = PBIApp(backend, tenant=tenant)
        async with app.run_test(size=size) as pilot:
            ui = Ui(app, pilot)
            await ui.settle()
            return await scenario(ui)

    return asyncio.run(main())
