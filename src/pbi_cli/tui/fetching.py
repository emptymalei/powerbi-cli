"""What the session can do about a detail that the lake does not hold.

The Details tab says it, ``f`` does it, and the command palette offers it: all three ask the
same question (which account fetches it, and what does that cost), so it is answered once.
"""

from dataclasses import dataclass, replace
from typing import AbstractSet, Callable, List, Optional, Tuple

from pbi_cli.core.details import Detail, Provider, choose, fetch_options, has_margin
from pbi_cli.core.registry import Endpoint, Scope
from pbi_cli.core.sync.plan import SyncOptions

#: What a session does about a detail that the lake does not hold (``session.lazy`` of a plan
#: file): ask for it (``f``), fetch the harmless ones by itself, or do nothing.
ASK = "ask"
AUTO = "auto"
OFF = "off"


@dataclass(frozen=True)
class Fetching:
    """The accounts that are stored, and whether the lake can be written.

    :param available: the kinds of account that are stored (``None``: any is assumed)
    :param view_only: why nothing can be fetched into the lake (``""``: it can)
    :param lazy: what the session does about a detail the lake lacks: ``ask``, ``auto`` or
        ``off``
    :param admin_profile: the administrator profile that a fetch is made through (default: the
        active one); a session with a plan file uses the accounts of the file
    :param user_profile: the same for the user account
    """

    available: Optional[AbstractSet[Scope]] = None
    view_only: str = ""
    lazy: str = ASK
    admin_profile: Optional[str] = None
    user_profile: Optional[str] = None

    def provider(self, detail: Detail) -> Optional[Provider]:
        """The provider that would fetch a detail now, if a stored account can."""
        return choose(detail, self.available)

    def how(self, detail: Detail) -> str:
        """What to do to get a detail that the lake does not hold, in words."""
        if detail.held is not None:
            return ""
        if self.view_only:
            return "view only: nothing is fetched into this lake"
        if self.lazy == OFF:
            return "not in the lake (fetching on demand is off: session.lazy)"
        provider = self.provider(detail)
        if provider is not None:
            return f"press f: 1 request, {provider.needs}"
        if detail.providers:
            return f"needs {detail.providers[0].needs}, and none is stored"
        return "it is not fetched for one item"

    def wanted(self, details: List[Detail]) -> List[Tuple[Detail, Provider]]:
        """The details that are missing and that a stored account can fetch, each with the
        provider that would fetch it. A session that fetches nothing on demand wants none.
        """
        found: List[Tuple[Detail, Provider]] = []
        if self.lazy == OFF:
            return found
        for detail in details:
            if detail.held is not None:
                continue
            provider = self.provider(detail)
            if provider is not None:
                found.append((detail, provider))
        return found

    def auto(
        self,
        details: List[Detail],
        quota_left: Callable[[Endpoint], Optional[int]],
    ) -> List[Tuple[Detail, Provider]]:
        """The details that a session may fetch **without asking**: it is set to (``lazy:
        auto``), the lake can be written, and the detail is missing; and the way to fetch it is
        a stored account's, harmless (it copies no personal data, queries or connection
        details), not refused the last time, and leaves quota for what somebody asks for.

        :param details: the details of one item
        :param quota_left: the requests that fit now for an operation (``None``: not known)
        """
        found: List[Tuple[Detail, Provider]] = []
        if self.lazy != AUTO or self.view_only:
            return found
        for detail in details:
            if detail.held is not None:
                continue
            refused = set(detail.refused)
            for provider in detail.providers:
                if (
                    (self.available is None or provider.scope in self.available)
                    and not provider.target.sensitive
                    and provider.key not in refused
                    and has_margin(provider.endpoint, quota_left(provider.endpoint))
                ):
                    found.append((detail, provider))
                    break
        return found

    def options(self, wanted: List[Tuple[Detail, Provider]]) -> SyncOptions:
        """The sync that fetches them, for this item and no other, through the accounts of
        the session."""
        return replace(
            fetch_options([provider for _, provider in wanted]),
            admin_profile=self.admin_profile,
            user_profile=self.user_profile,
        )
