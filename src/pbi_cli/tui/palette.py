"""The command palette: search the things you can do, and jump to a workspace or an item.

``:`` and ``Ctrl+P`` open it from anywhere. Typing narrows the list; ``Enter`` runs the
highlighted command. Two providers feed it: `CommandsProvider` (every action of the UI, by
name, see `pbi_cli.tui.commands`) and `GotoProvider` (workspaces and items, by name).
"""

import re
from functools import partial
from typing import Any, Iterable, List

from rich.text import Text
from textual.command import DiscoveryHit, Hit, Hits, Provider

from pbi_cli.core.catalog import label
from pbi_cli.tui.commands import Command, commands_for

#: What a typed location looks like: a URL, a path, or a Windows drive.
_LOCATION = re.compile(r"^(\w+://|[/~]|\.\.?[/\\]|[A-Za-z]:[/\\])")


def _markup_safe(text: str) -> str:
    """Text for the help line of a hit, which Textual reads as markup: a ``[`` is a letter."""
    return text.replace("[", "\\[")


def looks_like_a_location(text: str) -> bool:
    """Whether text typed in the palette is a place to open a lake from."""
    return bool(_LOCATION.match(text.strip()))


class CommandsProvider(Provider):
    """Every action of the UI, by name: what is offered is what can be done now."""

    def _commands(self) -> List[Command]:
        return commands_for(self.app, self.screen)

    def _system(self) -> Iterable[Any]:
        """The commands of Textual itself (theme, keys, screenshot, quit)."""
        return list(self.app.get_system_commands(self.screen))

    async def discover(self) -> Hits:
        for command in self._commands():
            yield DiscoveryHit(
                Text(command.title),
                command.run,
                text=command.title,
                help=_markup_safe(command.help),
            )
        for system in self._system():
            if system.discover:
                yield DiscoveryHit(
                    Text(system.title),
                    system.callback,
                    text=system.title,
                    help=_markup_safe(system.help),
                )

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        wanted = [
            (command.title, command.help, command.run, command.keywords)
            for command in self._commands()
        ] + [
            (system.title, system.help, system.callback, "")
            for system in self._system()
        ]
        for title, help_text, run, keywords in wanted:
            by_title = matcher.match(title)
            # other words that find it must be in it as typed, not scattered: a fuzzy match
            # of "sy" in "switch directory" would find the wrong thing
            by_keywords = 0.5 if query.strip().lower() in keywords else 0.0
            score = max(by_title, by_keywords)
            if score > 0:
                shown = matcher.highlight(title) if by_title else Text(title)
                yield Hit(score, shown, run, text=title, help=_markup_safe(help_text))
        app: Any = self.app
        if app.backend.open_lake is not None and looks_like_a_location(query):
            place = query.strip()
            yield Hit(
                1.0,
                Text(f"Open lake {place}"),
                partial(app.open_lake_at, place),
                text=f"Open lake {place}",
                help="Look at it, view only (no account is needed)",
            )


class GotoProvider(Provider):
    """Find workspaces, reports, datasets, dashboards and dataflows by name."""

    async def search(self, query: str) -> Hits:
        app: Any = self.app
        catalog = app.catalog
        if catalog is None:
            return
        matcher = self.matcher(query)
        for found in catalog.search(query, limit=30):
            where = f"  in {found.workspace}" if found.workspace else ""
            shown = f"{label(found.kind)}: {found.name}{where}"
            yield Hit(
                matcher.match(shown) or 0.3,
                matcher.highlight(shown) if matcher.match(shown) else Text(shown),
                partial(app.goto, found),
                text=shown,
                help=found.id,
            )
