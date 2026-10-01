"""The command palette: jump to a workspace or an item by name."""

from functools import partial
from typing import Any

from rich.text import Text
from textual.command import Hit, Hits, Provider

from pbi_cli.core.catalog import label


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
