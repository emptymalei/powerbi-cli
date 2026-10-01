"""What the TUI needs from the world around it.

The TUI does not look for the lake, the token or the settings itself: the command line hands
them in as a `Backend`, so that tests can hand in a fake service and an empty lake instead.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from pbi_cli.core.client import PowerBIClient
from pbi_cli.core.registry import Scope
from pbi_cli.core.store import LakeStore
from pbi_cli.core.sync.engine import SyncEngine


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Identity:
    """Who the TUI is signed in as.

    :param tenant: the key of the tenant in the lake (from the token)
    :param profile: the name of the profile of the token
    :param group: ``admin`` or ``user``: the kind of token
    :param expires_at: when the token expires, when it says
    :param problem: why there is no token, when there is none
    """

    tenant: Optional[str] = None
    profile: Optional[str] = None
    group: Optional[str] = None
    expires_at: Optional[datetime] = None
    problem: str = ""

    @property
    def signed_in(self) -> bool:
        return self.tenant is not None


@dataclass
class Backend:
    """The world around the TUI.

    :param store: the data lake
    :param client_for: the API client that signs in for a kind of token (made on first use;
        it may raise `~pbi_cli.errors.PBIError` when there is no token)
    :param sign_in: stores a bearer token for a profile of a group (``token, profile,
        group``) and makes `client_for` use it from then on
    :param active_profile: the name of the active profile of a group, if there is one
    :param make_engine: builds the sync engine (default: one that uses `client_for`)
    :param clock: the current time (aware, UTC)
    """

    store: LakeStore
    client_for: Callable[[Scope], PowerBIClient]
    sign_in: Callable[[str, str, str], None]
    active_profile: Callable[[str], Optional[str]] = lambda group: None
    make_engine: Optional[Callable[[], SyncEngine]] = None
    clock: Callable[[], datetime] = _utcnow

    def engine(self) -> SyncEngine:
        """A sync engine for the lake."""
        if self.make_engine is not None:
            return self.make_engine()
        return SyncEngine(self.client_for, self.store, clock=self.clock)

    def identity(self) -> Identity:
        """Who the stored tokens are: the administrator's, else the user's.

        Never raises: when there is no usable token the answer says why.
        """
        problems = []
        for scope in (Scope.ADMIN, Scope.USER):
            try:
                client = self.client_for(scope)
                return Identity(
                    tenant=client.tenant_key(),
                    profile=client.profile_name(),
                    group=scope.value,
                    expires_at=client.token_info().expires_at,
                )
            except (
                Exception
            ) as error:  # a keyring or settings problem must not end the UI
                problems.append(str(error))
        return Identity(problem=problems[0] if problems else "")
