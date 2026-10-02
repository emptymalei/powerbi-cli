"""Credentials for the Power BI API and the expiry check.

The client asks a *provider* for the credentials before each request, so a token that is
replaced in the meantime (for example after signing in again) is picked up without
rebuilding anything. The command line builds the provider from the profiles stored with
``pbi auth``.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Mapping, Optional

from pbi_cli.core.jwt import TokenInfo, token_info
from pbi_cli.errors import AuthError, TokenExpiredError

#: A request is not started when the token expires within this time.
EXPIRY_LEEWAY = timedelta(seconds=30)


@dataclass(frozen=True)
class Credentials:
    """A bearer token and where it came from.

    The token never appears in ``repr`` (and so not in logs or tracebacks).

    :param token: the bearer token, without the ``Bearer`` prefix
    :param profile: name of the stored profile the token belongs to
    :param group: ``user`` or ``admin``: the group of that profile
    """

    token: str = field(repr=False)
    profile: Optional[str] = None
    group: Optional[str] = None

    @property
    def headers(self) -> Dict[str, str]:
        """The ``Authorization`` header for a request."""
        return {"Authorization": f"Bearer {self.token}"}

    @property
    def info(self) -> TokenInfo:
        """The tenant and expiry read from the token."""
        return token_info(self.token)

    @property
    def tenant_id(self) -> Optional[str]:
        """The tenant the token was issued by, when the token says so."""
        return self.info.tenant_id

    @property
    def identity(self) -> str:
        """Who the token is for, as a key: the object id the token states, else the name of
        the profile. The answer of an operation that depends on who asks is kept per
        identity in the lake."""
        subject = self.info.subject
        if subject:
            return subject
        return f"profile:{self.profile}" if self.profile else "unknown"

    def sign_in_hint(self) -> str:
        """The command that stores a fresh token for this profile."""
        command = "pbi auth -t <token>"
        if self.profile:
            command += f" -p {self.profile}"
        if self.group:
            command += f" -g {self.group}"
        return command


#: Returns the current credentials; called before every request.
CredentialsProvider = Callable[[], Credentials]


def credentials_from_headers(
    headers: Mapping[str, str],
    profile: Optional[str] = None,
    group: Optional[str] = None,
) -> Credentials:
    """Build credentials from an ``{"Authorization": "Bearer ..."}`` mapping.

    :param headers: what ``load_auth`` returns
    :param profile: name of the profile the headers were loaded for
    :param group: group of that profile
    :raises AuthError: if there is no bearer token in the headers
    """
    value = headers.get("Authorization", "")
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError("No bearer token found in the credentials.")
    return Credentials(token=token.strip(), profile=profile, group=group)


def ensure_not_expired(
    credentials: Credentials,
    now: Optional[datetime] = None,
    leeway: timedelta = EXPIRY_LEEWAY,
) -> None:
    """Raise `TokenExpiredError` if the token expired or is about to.

    Tokens whose expiry cannot be read are accepted: the API decides (``401``).

    :param credentials: the credentials to check
    :param now: the current time (UTC), for tests
    :param leeway: refuse tokens that expire within this time
    """
    now = now or datetime.now(timezone.utc)
    info = credentials.info
    if not info.is_expired(now=now, leeway=leeway):
        return
    assert info.expires_at is not None
    who = f" for profile '{credentials.profile}'" if credentials.profile else ""
    raise TokenExpiredError(
        f"The token{who} expired at {info.expires_at:%Y-%m-%d %H:%M} UTC. "
        f"Sign in again and store a fresh token with `{credentials.sign_in_hint()}`.",
        group=credentials.group,
        profile=credentials.profile,
    )
