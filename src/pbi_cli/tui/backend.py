"""What the TUI needs from the world around it.

The TUI does not look for the lake, the token or the settings itself: the command line hands
them in as a `Backend`, so that tests can hand in a fake service and an empty lake instead.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional, Set

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
    :param name: who the token is for (the user name, or the application), when it says
    """

    tenant: Optional[str] = None
    profile: Optional[str] = None
    group: Optional[str] = None
    expires_at: Optional[datetime] = None
    problem: str = ""
    name: Optional[str] = None

    @property
    def signed_in(self) -> bool:
        return self.tenant is not None


@dataclass(frozen=True)
class AccountInfo:
    """A stored profile, as the Accounts dialog lists it.

    :param group: ``admin`` or ``user``
    :param profile: its name
    :param active: whether it is the active profile of its group
    :param name: who its token is for, when the token says
    :param tenant: the tenant its token was issued by, when the token says
    :param expires_at: when its token expires, when it says
    :param has_token: whether a token is stored under the profile at all
    """

    group: str
    profile: str
    active: bool = False
    name: Optional[str] = None
    tenant: Optional[str] = None
    expires_at: Optional[datetime] = None
    has_token: bool = True


@dataclass
class Backend:
    """The world around the TUI.

    :param store: the data lake
    :param client_for: the API client that signs in for a kind of token, as a profile when
        it is given one (made on first use; it may raise `~pbi_cli.errors.PBIError` when
        there is no token)
    :param sign_in: stores a bearer token for a profile of a group (``token, profile,
        group``) and makes `client_for` use it from then on
    :param active_profile: the name of the active profile of a group, if there is one
    :param make_engine: builds the sync engine (default: one that uses `client_for`)
    :param clock: the current time (aware, UTC)
    :param open_lake: opens another lake to look at, from a folder or URL; it raises
        `~pbi_cli.errors.PBIError` when that is not possible (default: no other lake)
    :param recent_lakes: the lakes opened lately, newest first
    :param work_lake: where the work lake is (the lake of the cache folder), if there is one
    :param accounts: the stored profiles of both groups, with who they are and when their
        tokens expire (default: none)
    :param activate: makes a profile the active one of its group, as ``pbi profile switch``
        does, and makes `client_for` use it from then on (default: not possible)
    """

    store: LakeStore
    client_for: Callable[..., PowerBIClient]
    sign_in: Callable[[str, str, str], None]
    active_profile: Callable[[str], Optional[str]] = lambda group: None
    make_engine: Optional[Callable[[], SyncEngine]] = None
    clock: Callable[[], datetime] = _utcnow
    open_lake: Optional[Callable[[str], LakeStore]] = None
    recent_lakes: Callable[[], List[str]] = lambda: []
    work_lake: Optional[str] = None
    accounts: Callable[[], List[AccountInfo]] = lambda: []
    activate: Optional[Callable[[str, str], None]] = None

    @property
    def readonly(self) -> str:
        """Why nothing can be fetched into this lake (an empty text when something can).

        A lake that is only looked at needs no account: nothing asks about one.
        """
        return self.store.why_read_only()

    def engine(self) -> SyncEngine:
        """A sync engine for the lake."""
        if self.make_engine is not None:
            return self.make_engine()
        return SyncEngine(self.client_for, self.store, clock=self.clock)

    def identities(self) -> List[Identity]:
        """Who the stored tokens are: the administrator's and the user's, those there are.

        Never raises. A lake that is only looked at has none: no account is needed, so none
        is asked about.
        """
        found: List[Identity] = []
        if self.readonly:
            return found
        for scope in (Scope.ADMIN, Scope.USER):
            try:
                client = self.client_for(scope)
                info = client.token_info()
                found.append(
                    Identity(
                        tenant=client.tenant_key(),
                        profile=client.profile_name(),
                        group=scope.value,
                        expires_at=info.expires_at,
                        name=info.name,
                    )
                )
            except Exception:  # a keyring or settings problem must not end the UI
                continue
        return found

    def available_scopes(self) -> Set[Scope]:
        """The kinds of account that are stored (none for a lake that is only looked at)."""
        return {Scope(i.group) for i in self.identities() if i.group}

    def identity(self) -> Identity:
        """Who the stored tokens are, for the tenant: the administrator's, else the user's.

        Never raises: when there is no usable token the answer says why.
        """
        if self.readonly:
            return Identity()
        found = self.identities()
        if found:
            return found[0]
        problems = []
        for scope in (Scope.ADMIN, Scope.USER):
            try:
                self.client_for(scope).tenant_key()
            except Exception as error:
                problems.append(str(error))
        return Identity(problem=problems[0] if problems else "")
