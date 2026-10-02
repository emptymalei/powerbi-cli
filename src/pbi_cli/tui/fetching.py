"""What the session can do about a detail that the lake does not hold.

The Details tab says it, ``f`` does it, and the command palette offers it: all three ask the
same question (which account fetches it, and what does that cost), so it is answered once.
"""

from dataclasses import dataclass
from typing import AbstractSet, List, Optional, Tuple

from pbi_cli.core.details import Detail, Provider, choose, fetch_options
from pbi_cli.core.registry import Scope
from pbi_cli.core.sync.plan import SyncOptions


@dataclass(frozen=True)
class Fetching:
    """The accounts that are stored, and whether the lake can be written.

    :param available: the kinds of account that are stored (``None``: any is assumed)
    :param view_only: why nothing can be fetched into the lake (``""``: it can)
    """

    available: Optional[AbstractSet[Scope]] = None
    view_only: str = ""

    def provider(self, detail: Detail) -> Optional[Provider]:
        """The provider that would fetch a detail now, if a stored account can."""
        return choose(detail, self.available)

    def how(self, detail: Detail) -> str:
        """What to do to get a detail that the lake does not hold, in words."""
        if detail.held is not None:
            return ""
        if self.view_only:
            return "view only: nothing is fetched into this lake"
        provider = self.provider(detail)
        if provider is not None:
            return f"press f: 1 request, {provider.needs}"
        if detail.providers:
            return f"needs {detail.providers[0].needs}, and none is stored"
        return "it is not fetched for one item"

    def wanted(self, details: List[Detail]) -> List[Tuple[Detail, Provider]]:
        """The details that are missing and that a stored account can fetch, each with the
        provider that would fetch it."""
        found = []
        for detail in details:
            if detail.held is not None:
                continue
            provider = self.provider(detail)
            if provider is not None:
                found.append((detail, provider))
        return found

    def options(self, wanted: List[Tuple[Detail, Provider]]) -> SyncOptions:
        """The sync that fetches them, for this item and no other."""
        return fetch_options([provider for _, provider in wanted])
