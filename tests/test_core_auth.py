"""Tests for reading tokens locally and for the credentials helpers."""

from datetime import timedelta

import pytest
from core_helpers import NOW, UTC, make_token

from pbi_cli.core.auth import (
    EXPIRY_LEEWAY,
    Credentials,
    credentials_from_headers,
    ensure_not_expired,
)
from pbi_cli.core.jwt import decode_claims, token_info
from pbi_cli.errors import AuthError, TokenExpiredError

# ---------------------------------------------------------------------------
# jwt
# ---------------------------------------------------------------------------


def test_token_info_reads_tenant_and_expiry():
    token = make_token(tenant="11111111-2222-3333-4444-555555555555")
    info = token_info(token)
    assert info.tenant_id == "11111111-2222-3333-4444-555555555555"
    assert info.expires_at == (NOW + timedelta(hours=1)).replace(tzinfo=UTC)


def test_token_info_seconds_left_and_expired():
    info = token_info(make_token(expires_in=timedelta(minutes=10)))
    assert info.seconds_left(NOW) == 600
    assert not info.is_expired(NOW)
    assert info.is_expired(NOW + timedelta(minutes=10))
    assert info.is_expired(NOW + timedelta(minutes=11))
    # leeway: "expires within 30 seconds" counts as expired
    assert info.is_expired(NOW + timedelta(minutes=9, seconds=40), leeway=EXPIRY_LEEWAY)


def test_token_without_expiry_is_never_expired():
    info = token_info(make_token(expires_in=None))
    assert info.expires_at is None
    assert info.seconds_left(NOW) is None
    assert not info.is_expired(NOW + timedelta(days=3650))


@pytest.mark.parametrize(
    "token",
    ["", "not-a-jwt", "a.b", "a.b.c.d", "a.!!!.c", "a.e30.c.d"],
)
def test_unreadable_tokens_give_empty_claims(token):
    assert decode_claims(token) == {}
    info = token_info(token)
    assert info.tenant_id is None and info.expires_at is None


def test_claims_that_are_not_an_object_are_ignored():
    import base64

    payload = base64.urlsafe_b64encode(b'["tid", "exp"]').decode().rstrip("=")
    assert decode_claims(f"h.{payload}.s") == {}


def test_odd_claim_values_are_ignored():
    info = token_info(make_token(tenant=None, expires_in=None, exp="soon", tid=123))
    assert info.tenant_id is None
    assert info.expires_at is None
    bool_exp = make_token(tenant=None, expires_in=None, exp=True)
    assert token_info(bool_exp).expires_at is None
    huge_exp = make_token(tenant=None, expires_in=None, exp=10**30)
    assert token_info(huge_exp).expires_at is None


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------


def test_credentials_headers_and_tenant():
    creds = Credentials(token=make_token(tenant="t-9"), profile="p", group="admin")
    assert creds.headers == {"Authorization": f"Bearer {creds.token}"}
    assert creds.tenant_id == "t-9"


def test_credentials_repr_does_not_leak_the_token():
    creds = Credentials(token="super-secret-token", profile="p")
    assert "super-secret-token" not in repr(creds)
    assert "super-secret-token" not in str(creds)
    assert "profile='p'" in repr(creds)


def test_sign_in_hint_names_profile_and_group():
    assert Credentials("t").sign_in_hint() == "pbi auth -t <token>"
    assert (
        Credentials("t", profile="admin-nlm", group="admin").sign_in_hint()
        == "pbi auth -t <token> -p admin-nlm -g admin"
    )


def test_credentials_from_headers():
    creds = credentials_from_headers({"Authorization": "Bearer abc"}, "p", "user")
    assert (creds.token, creds.profile, creds.group) == ("abc", "p", "user")


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Basic x"},
    ],
)
def test_credentials_from_headers_rejects_other_schemes(headers):
    with pytest.raises(AuthError):
        credentials_from_headers(headers)


# ---------------------------------------------------------------------------
# expiry check
# ---------------------------------------------------------------------------


def test_fresh_token_passes():
    creds = Credentials(make_token(expires_in=timedelta(hours=1)))
    ensure_not_expired(creds, now=NOW)


def test_token_without_expiry_passes():
    ensure_not_expired(Credentials(make_token(expires_in=None)), now=NOW)
    ensure_not_expired(Credentials("opaque-token"), now=NOW)


def test_expired_token_raises_with_a_sign_in_hint():
    creds = Credentials(
        make_token(expires_in=timedelta(minutes=-5)), profile="admin-nlm", group="admin"
    )
    with pytest.raises(TokenExpiredError) as excinfo:
        ensure_not_expired(creds, now=NOW)
    message = str(excinfo.value)
    assert "profile 'admin-nlm'" in message
    assert "expired at 2026-09-30 11:55 UTC" in message
    assert "pbi auth -t <token> -p admin-nlm -g admin" in message
    assert creds.token not in message


def test_token_about_to_expire_is_refused():
    creds = Credentials(make_token(expires_in=timedelta(seconds=10)))
    with pytest.raises(TokenExpiredError):
        ensure_not_expired(creds, now=NOW)
    ensure_not_expired(creds, now=NOW, leeway=timedelta(0))
