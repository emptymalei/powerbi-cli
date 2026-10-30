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


def test_an_expired_token_says_whose_it_was():
    creds = Credentials(
        make_token(expires_in=timedelta(minutes=-5)), profile="svc", group="user"
    )

    with pytest.raises(TokenExpiredError) as excinfo:
        ensure_not_expired(creds, now=NOW)

    assert excinfo.value.group == "user" and excinfo.value.profile == "svc"


def test_a_token_that_has_no_profile_says_none():
    creds = Credentials(make_token(expires_in=timedelta(minutes=-5)), group="admin")

    with pytest.raises(TokenExpiredError) as excinfo:
        ensure_not_expired(creds, now=NOW)

    assert excinfo.value.profile is None and excinfo.value.group == "admin"


def test_an_error_about_an_account_may_name_the_profile_or_not():
    assert AuthError("x").profile is None and AuthError("x").group is None
    named = AuthError("x", group="user", profile="svc")
    assert (named.group, named.profile) == ("user", "svc")


def test_token_about_to_expire_is_refused():
    creds = Credentials(make_token(expires_in=timedelta(seconds=10)))
    with pytest.raises(TokenExpiredError):
        ensure_not_expired(creds, now=NOW)
    ensure_not_expired(creds, now=NOW, leeway=timedelta(0))


# ---------------------------------------------------------------------------
# who the token is for
# ---------------------------------------------------------------------------


def test_the_token_says_who_it_is_for():
    info = token_info(make_token(oid="oid-1", upn="ana@contoso.com"))

    assert info.subject == "oid-1" and info.name == "ana@contoso.com"


def test_an_application_token_is_for_the_application():
    info = token_info(make_token(appid="app-1"))

    assert info.subject == "app-1" and info.name == "app-1"


def test_the_object_id_comes_before_the_application_and_the_user_name_before_the_rest():
    info = token_info(
        make_token(oid="oid-1", appid="app-1", unique_name="u@x", name="Ana", upn="p@x")
    )

    assert info.subject == "oid-1" and info.name == "p@x"
    assert token_info(make_token(unique_name="u@x", name="Ana")).name == "u@x"
    assert token_info(make_token(name="Ana")).name == "Ana"


@pytest.mark.parametrize("claims", [{}, {"oid": 5}, {"oid": "  "}, {"upn": None}])
def test_a_token_that_does_not_say_has_no_subject(claims):
    info = token_info(make_token(**claims))

    assert info.subject is None
    assert info.name is None


def test_an_identity_is_the_object_id_else_the_profile_else_unknown():
    assert Credentials(make_token(oid="oid-1"), profile="p").identity == "oid-1"
    assert Credentials(make_token(), profile="p").identity == "profile:p"
    assert Credentials("opaque").identity == "unknown"


def test_an_expired_token_error_says_which_kind_of_account():
    credentials = Credentials(
        make_token(expires_in=timedelta(minutes=-5)), profile="svc", group="user"
    )

    with pytest.raises(TokenExpiredError) as error:
        ensure_not_expired(credentials)

    assert error.value.group == "user"
