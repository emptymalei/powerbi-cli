"""Read a few claims from a bearer token, locally.

The token is only *read*: nothing is sent anywhere, the signature is not checked and
nothing is stored. The tenant id scopes the data lake by tenant, the expiry lets the client
(and the TUI) say when the token runs out, and the object id tells accounts apart.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class TokenInfo:
    """What pbi-cli reads from a token.

    :param tenant_id: the ``tid`` claim: the tenant (directory) the token was issued by
    :param expires_at: the ``exp`` claim as an aware UTC datetime
    :param subject: who the token is for: the object id of the account (``oid``), or of the
        application for a service principal (``appid``)
    :param name: how to call them: the user name (``upn``) or the name of the application
    """

    tenant_id: Optional[str] = None
    expires_at: Optional[datetime] = None
    subject: Optional[str] = None
    name: Optional[str] = None

    def seconds_left(self, now: Optional[datetime] = None) -> Optional[float]:
        """Seconds until the token expires, negative once it has; ``None`` if unknown."""
        if self.expires_at is None:
            return None
        now = now or datetime.now(timezone.utc)
        return (self.expires_at - now).total_seconds()

    def is_expired(
        self, now: Optional[datetime] = None, leeway: timedelta = timedelta(0)
    ) -> bool:
        """Whether the token has expired or expires within ``leeway``.

        A token without a readable expiry is never reported as expired.
        """
        left = self.seconds_left(now)
        return left is not None and left <= leeway.total_seconds()


def decode_claims(token: str) -> Dict[str, Any]:
    """Return the claims of a JWT, or ``{}`` when the token is not a readable JWT.

    :param token: the bearer token, without the ``Bearer`` prefix
    """
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (binascii.Error, ValueError, UnicodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _first_text(claims: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def token_info(token: str) -> TokenInfo:
    """Read the tenant, the expiry and who the token is for from a token.

    :param token: the bearer token, without the ``Bearer`` prefix
    """
    claims = decode_claims(token)

    tenant = claims.get("tid")
    tenant_id = tenant if isinstance(tenant, str) and tenant else None

    expires_at = None
    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and not isinstance(exp, bool):
        try:
            expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            expires_at = None

    return TokenInfo(
        tenant_id=tenant_id,
        expires_at=expires_at,
        subject=_first_text(claims, "oid", "appid", "azp"),
        name=_first_text(
            claims, "upn", "unique_name", "preferred_username", "name", "email", "appid"
        ),
    )
