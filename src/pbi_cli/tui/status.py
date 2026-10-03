"""The line at the top of every screen: who is signed in, where the lake is, what is running."""

from typing import Any

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.widgets import Static

from pbi_cli.tui import render

#: The frames of the little spinner that shows a sync is running.
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

#: Seconds between two redraws of the line.
REFRESH_EVERY = 0.5

#: What the right end of the line says about the command palette (it can be clicked).
HINT = ": commands"


class StatusBar(Horizontal):
    """Tenant, profile, lake and token on the left; the running sync on the right."""

    def compose(self) -> ComposeResult:
        yield Static("", id="who")
        yield Static("", id="busy")
        yield Static(Text(HINT, style="grey62"), id="hint")

    @on(events.Click, "#hint")
    def _open_the_palette(self) -> None:
        self.app.action_command_palette()

    def on_mount(self) -> None:
        self._frame = 0
        self.set_interval(REFRESH_EVERY, self.refresh_status)
        self.refresh_status()

    def refresh_status(self) -> None:
        """Draw the line again (called twice a second, and when something changes)."""
        try:
            self._draw()
        except NoMatches:
            return  # the app is closing and the line is taken apart: nothing to draw

    def _draw(self) -> None:
        app: Any = self.app
        now = app.backend.clock()
        who = Text()
        who.append(" pbi ", style="bold black on #F2C811")
        who.append(" ")
        tenant = app.tenant
        who.append(f"tenant {render.short_id(tenant, 14)}" if tenant else "no tenant")
        who.append(" │ ")
        view_only = bool(app.backend.readonly)
        if view_only:
            who.append("view only", style="bold yellow")
        elif app.identities:
            for number, found in enumerate(app.identities):
                if number:
                    who.append(" · ")
                who.append(f"{found.profile or 'profile'} ({found.group}) ")
                who.append_text(render.token_text(found.expires_at, now, True))
        else:
            who.append("no profile  ", style="grey62")
            who.append_text(render.token_text(None, now, False))
        who.append(" │ ")
        who.append(app.lake_label, style="grey62")
        published = app.backend.store.published()
        if published is not None:
            who.append(" │ ")
            if published.complete:
                who.append(
                    f"published {published.published_at:%Y-%m-%d %H:%M} by "
                    f"{published.published_by}",
                    style="grey62",
                )
            else:
                who.append(
                    f"publish by {published.published_by} not finished", style="yellow"
                )
        self.query_one("#who", Static).update(who)

        busy = Text()
        state = app.run_state
        if state is not None and state.running:
            self._frame = (self._frame + 1) % len(SPINNER)
            busy.append(f"{SPINNER[self._frame]} ", style="#F2C811")
            busy.append(state.summary(app.backend.clock) + " ")
        self.query_one("#busy", Static).update(busy)
