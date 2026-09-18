"""`src.core.security` and `src.api.deps` — the auth boundary.

Everything here is a gate: JWKS construction, JWT verification, the three
memory-namespace rules, the two admin paths, and the internal shared secret.
The rejecting arms are the point of the module, so they are what these cover.
"""

from __future__ import annotations

import hmac
from typing import Any

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from src.api import deps
from src.core import security
from src.core.security import (
    AdminPrincipal,
    AuthUser,
    check_user_access,
    get_jwks_client,
    is_admin_user,
    is_valid_admin_api_key,
    verify_token,
)


@pytest.fixture(autouse=True)
def _reset_jwks_singleton(monkeypatch):
    """`_jwks_client` is a module global; leaking it between tests hides bugs."""
    monkeypatch.setattr(security, "_jwks_client", None, raising=False)


def _creds(token: str = "t") -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


class TestJwksClient:
    def test_no_client_without_a_configured_url(self, monkeypatch):
        monkeypatch.setattr(security.settings, "supabase_url", None, raising=False)
        assert get_jwks_client() is None

    def test_a_client_is_built_and_then_cached(self, monkeypatch):
        built: list[str] = []

        class FakePyJWKClient:
            def __init__(self, url: str, cache_keys: bool = False):
                built.append(url)

        monkeypatch.setattr(security, "PyJWKClient", FakePyJWKClient)
        monkeypatch.setattr(
            security.settings, "supabase_url", "https://proj.supabase.co", raising=False
        )

        first = get_jwks_client()
        second = get_jwks_client()

        assert first is not None
        assert first is second, "the client is a cached singleton"
        assert built == ["https://proj.supabase.co/auth/v1/.well-known/jwks.json"]


class TestVerifyToken:
    @pytest.mark.anyio
    async def test_it_refuses_when_jwks_is_not_configured(self, monkeypatch):
        monkeypatch.setattr(security, "get_jwks_client", lambda: None)
        with pytest.raises(jwt.InvalidTokenError, match="JWKS not configured"):
            await verify_token("anything")

    @pytest.mark.anyio
    async def test_it_decodes_with_the_algorithm_from_the_token_header(self, monkeypatch):
        seen: dict[str, Any] = {}

        class FakeKey:
            key = "the-key"

        class FakeClient:
            def get_signing_key_from_jwt(self, token):
                seen["token"] = token
                return FakeKey()

        monkeypatch.setattr(security, "get_jwks_client", lambda: FakeClient())
        monkeypatch.setattr(security.jwt, "get_unverified_header", lambda t: {"alg": "ES256"})
        monkeypatch.setattr(
            security.jwt,
            "decode",
            lambda token, key, algorithms, audience: (
                seen.update(algorithms=algorithms, audience=audience, key=key) or {"sub": "u1"}
            ),
        )

        assert await verify_token("tok") == {"sub": "u1"}
        assert seen["algorithms"] == ["ES256"]
        assert seen["audience"] == "authenticated"
        assert seen["token"] == "tok"

    @pytest.mark.anyio
    async def test_an_unreadable_header_falls_back_to_rs256(self, monkeypatch):
        """A malformed header must not crash before the signature is even checked."""
        seen: dict[str, Any] = {}

        class FakeKey:
            key = "k"

        class FakeClient:
            def get_signing_key_from_jwt(self, token):
                return FakeKey()

        def boom(_):
            raise ValueError("not a jwt")

        monkeypatch.setattr(security, "get_jwks_client", lambda: FakeClient())
        monkeypatch.setattr(security.jwt, "get_unverified_header", boom)
        monkeypatch.setattr(
            security.jwt,
            "decode",
            lambda token, key, algorithms, audience: (
                seen.update(algorithms=algorithms) or {"sub": "u"}
            ),
        )

        await verify_token("garbage")
        assert seen["algorithms"] == ["RS256"]

    @pytest.mark.anyio
    async def test_a_header_without_an_alg_defaults_to_rs256(self, monkeypatch):
        seen: dict[str, Any] = {}

        class FakeKey:
            key = "k"

        class FakeClient:
            def get_signing_key_from_jwt(self, token):
                return FakeKey()

        monkeypatch.setattr(security, "get_jwks_client", lambda: FakeClient())
        monkeypatch.setattr(security.jwt, "get_unverified_header", lambda t: {})
        monkeypatch.setattr(
            security.jwt,
            "decode",
            lambda token, key, algorithms, audience: (
                seen.update(algorithms=algorithms) or {"sub": "u"}
            ),
        )

        await verify_token("tok")
        assert seen["algorithms"] == ["RS256"]


class TestAuthUser:
    def test_it_reads_the_standard_claims(self):
        u = AuthUser(
            {"sub": "u1", "email": "a@b.c", "role": "service_role", "aud": "authenticated"}
        )
        assert (u.id, u.email, u.role, u.aud) == ("u1", "a@b.c", "service_role", "authenticated")

    def test_missing_claims_get_safe_defaults(self):
        u = AuthUser({})
        assert u.id == ""
        assert u.email is None
        assert u.role == "authenticated"
        assert u.aud == ""

    def test_the_raw_payload_is_kept(self):
        payload = {"sub": "u1", "custom": 1}
        assert AuthUser(payload).raw_payload == payload

    def test_repr_names_the_user_without_dumping_the_token(self):
        r = repr(AuthUser({"sub": "u1", "email": "a@b.c", "secret_claim": "nope"}))
        assert r == "AuthUser(id=u1, email=a@b.c)"
        assert "secret_claim" not in r


class TestAdminPrincipal:
    def test_it_defaults_to_an_identity_with_no_user(self):
        p = AdminPrincipal(auth_method="api_key")
        assert (p.auth_method, p.user_id, p.email) == ("api_key", None, None)


class TestCheckUserAccess:
    @pytest.mark.parametrize(
        "namespace",
        ["u1", "kwami_u1", "kwami_u1_abc", "kwami_u1_"],
        ids=["own id", "legacy namespace", "per-kwami namespace", "bare per-kwami prefix"],
    )
    def test_the_three_anchored_rules_allow(self, namespace):
        assert check_user_access(AuthUser({"sub": "u1"}), namespace) is True

    @pytest.mark.parametrize(
        "namespace",
        ["u2", "kwami_u2", "u1kwami_", "kwami_u12", "kwami_u12_x", "xkwami_u1", ""],
        ids=[
            "another user",
            "another user's namespace",
            "the old unanchored replace() hole",
            "an id that merely starts with ours",
            "same, with a suffix",
            "a prefixed namespace",
            "empty",
        ],
    )
    def test_everything_else_is_denied(self, namespace):
        assert check_user_access(AuthUser({"sub": "u1"}), namespace) is False

    def test_a_user_with_no_id_is_denied(self):
        assert check_user_access(AuthUser({}), "anything") is False


class TestIsAdminUser:
    def test_no_user_is_not_an_admin(self):
        assert is_admin_user(None) is False

    def test_the_service_role_is_an_admin(self):
        assert is_admin_user(AuthUser({"sub": "u", "role": "service_role"})) is True

    def test_an_allowlisted_email_is_an_admin(self, monkeypatch):
        monkeypatch.setattr(
            type(security.settings), "admin_emails", property(lambda self: ["owner@example.com"])
        )
        assert is_admin_user(AuthUser({"sub": "u", "email": "Owner@Example.com"})) is True

    def test_an_email_off_the_list_is_not(self, monkeypatch):
        monkeypatch.setattr(
            type(security.settings), "admin_emails", property(lambda self: ["owner@example.com"])
        )
        assert is_admin_user(AuthUser({"sub": "u", "email": "someone@example.com"})) is False

    def test_a_user_without_an_email_is_not(self, monkeypatch):
        monkeypatch.setattr(
            type(security.settings), "admin_emails", property(lambda self: ["owner@example.com"])
        )
        assert is_admin_user(AuthUser({"sub": "u"})) is False


class TestIsValidAdminApiKey:
    def test_no_key_supplied_is_false(self, monkeypatch):
        monkeypatch.setattr(security.settings, "admin_api_key", "secret", raising=False)
        assert is_valid_admin_api_key(None) is False
        assert is_valid_admin_api_key("") is False

    def test_no_key_configured_is_false(self, monkeypatch):
        monkeypatch.setattr(security.settings, "admin_api_key", None, raising=False)
        assert is_valid_admin_api_key("anything") is False

    def test_the_right_key_is_true(self, monkeypatch):
        monkeypatch.setattr(security.settings, "admin_api_key", "secret", raising=False)
        assert is_valid_admin_api_key("secret") is True

    @pytest.mark.parametrize("wrong", ["sec", "secrets", "Secret", "x"])
    def test_a_wrong_key_is_false_including_prefixes(self, monkeypatch, wrong):
        monkeypatch.setattr(security.settings, "admin_api_key", "secret", raising=False)
        assert is_valid_admin_api_key(wrong) is False

    def test_it_uses_a_constant_time_comparison(self, monkeypatch):
        """A `==` here leaks the secret's length and prefix through timing."""
        calls: list[tuple[str, str]] = []
        real = hmac.compare_digest  # bound before patching, or the spy recurses

        def spy(a, b):
            calls.append((a, b))
            return real(a, b)

        monkeypatch.setattr(security.settings, "admin_api_key", "secret", raising=False)
        monkeypatch.setattr(security.hmac, "compare_digest", spy)
        assert is_valid_admin_api_key("secret") is True
        assert calls == [("secret", "secret")]


class TestGetCurrentUser:
    @pytest.mark.anyio
    async def test_no_credentials_is_anonymous(self):
        assert await deps.get_current_user(None) is None

    @pytest.mark.anyio
    async def test_auth_not_configured_is_anonymous(self, monkeypatch):
        monkeypatch.setattr(type(deps.settings), "auth_enabled", property(lambda self: False))
        assert await deps.get_current_user(_creds()) is None

    @pytest.mark.anyio
    async def test_a_valid_token_becomes_an_auth_user(self, monkeypatch):
        monkeypatch.setattr(type(deps.settings), "auth_enabled", property(lambda self: True))

        async def ok(token):
            assert token == "the-token"
            return {"sub": "u1", "email": "a@b.c"}

        monkeypatch.setattr(deps, "verify_token", ok)
        user = await deps.get_current_user(_creds("the-token"))
        assert user is not None and user.id == "u1"

    @pytest.mark.parametrize(
        ("raised", "detail"),
        [
            (jwt.ExpiredSignatureError("expired"), "Token has expired"),
            (jwt.InvalidAudienceError("aud"), "Invalid token audience"),
            (jwt.InvalidTokenError("bad"), "Invalid authentication token"),
        ],
        ids=["expired", "wrong audience", "invalid"],
    )
    @pytest.mark.anyio
    async def test_each_jwt_failure_is_a_401_with_its_own_message(
        self, monkeypatch, raised, detail
    ):
        monkeypatch.setattr(type(deps.settings), "auth_enabled", property(lambda self: True))

        async def boom(token):
            raise raised

        monkeypatch.setattr(deps, "verify_token", boom)
        with pytest.raises(HTTPException) as exc:
            await deps.get_current_user(_creds())
        assert exc.value.status_code == 401
        assert exc.value.detail == detail
        assert exc.value.headers == {"WWW-Authenticate": "Bearer"}


class TestRequireAuth:
    @pytest.mark.anyio
    async def test_it_passes_an_authenticated_user_through(self):
        user = AuthUser({"sub": "u1"})
        assert await deps.require_auth(user) is user

    @pytest.mark.anyio
    async def test_it_401s_an_anonymous_caller(self):
        with pytest.raises(HTTPException) as exc:
            await deps.require_auth(None)
        assert exc.value.status_code == 401
        assert exc.value.detail == "Authentication required"


class TestRequireAdmin:
    @pytest.mark.anyio
    async def test_a_valid_api_key_is_enough(self, monkeypatch):
        monkeypatch.setattr(deps, "is_valid_admin_api_key", lambda k: True)
        principal = await deps.require_admin(None, "any-key")
        assert principal == AdminPrincipal(auth_method="api_key")

    @pytest.mark.anyio
    async def test_an_allowlisted_user_is_enough(self, monkeypatch):
        monkeypatch.setattr(deps, "is_valid_admin_api_key", lambda k: False)
        monkeypatch.setattr(deps, "is_admin_user", lambda u: True)
        user = AuthUser({"sub": "u1", "email": "owner@example.com"})
        principal = await deps.require_admin(user, None)
        assert principal == AdminPrincipal(
            auth_method="user", user_id="u1", email="owner@example.com"
        )

    @pytest.mark.anyio
    async def test_the_user_arm_tolerates_a_none_user(self, monkeypatch):
        """`is_admin_user` is the gate; the attribute reads must not assume a user."""
        monkeypatch.setattr(deps, "is_valid_admin_api_key", lambda k: False)
        monkeypatch.setattr(deps, "is_admin_user", lambda u: True)
        assert await deps.require_admin(None, None) == AdminPrincipal(
            auth_method="user", user_id=None, email=None
        )

    @pytest.mark.anyio
    async def test_it_403s_with_a_generic_message_when_admin_is_unconfigured(self, monkeypatch):
        monkeypatch.setattr(deps, "is_valid_admin_api_key", lambda k: False)
        monkeypatch.setattr(deps, "is_admin_user", lambda u: False)
        monkeypatch.setattr(deps.settings, "admin_api_key", None, raising=False)
        monkeypatch.setattr(type(deps.settings), "admin_emails", property(lambda self: []))
        with pytest.raises(HTTPException) as exc:
            await deps.require_admin(None, None)
        assert exc.value.status_code == 403
        assert exc.value.detail == "Admin access required"

    @pytest.mark.anyio
    @pytest.mark.parametrize("configured", ["key", "emails"])
    async def test_it_403s_with_a_specific_message_when_admin_is_configured(
        self, monkeypatch, configured
    ):
        monkeypatch.setattr(deps, "is_valid_admin_api_key", lambda k: False)
        monkeypatch.setattr(deps, "is_admin_user", lambda u: False)
        monkeypatch.setattr(
            deps.settings, "admin_api_key", "k" if configured == "key" else None, raising=False
        )
        monkeypatch.setattr(
            type(deps.settings),
            "admin_emails",
            property(lambda self: ["a@b.c"] if configured == "emails" else []),
        )
        with pytest.raises(HTTPException) as exc:
            await deps.require_admin(None, None)
        assert exc.value.detail == "Valid admin credentials required"


class TestRequireInternalApiKey:
    @pytest.mark.anyio
    async def test_it_401s_when_no_key_is_configured(self, monkeypatch):
        monkeypatch.setattr(deps.settings, "kwami_api_key", None, raising=False)
        with pytest.raises(HTTPException) as exc:
            await deps.require_internal_api_key("anything")
        assert exc.value.status_code == 401

    @pytest.mark.anyio
    async def test_it_401s_when_no_key_is_supplied(self, monkeypatch):
        monkeypatch.setattr(deps.settings, "kwami_api_key", "secret", raising=False)
        with pytest.raises(HTTPException) as exc:
            await deps.require_internal_api_key(None)
        assert exc.value.status_code == 401

    @pytest.mark.anyio
    async def test_the_right_key_passes(self, monkeypatch):
        monkeypatch.setattr(deps.settings, "kwami_api_key", "secret", raising=False)
        assert await deps.require_internal_api_key("secret") is None

    @pytest.mark.anyio
    @pytest.mark.parametrize("wrong", ["sec", "secrets", "Secret"])
    async def test_a_wrong_key_401s_including_a_prefix(self, monkeypatch, wrong):
        monkeypatch.setattr(deps.settings, "kwami_api_key", "secret", raising=False)
        with pytest.raises(HTTPException) as exc:
            await deps.require_internal_api_key(wrong)
        assert exc.value.status_code == 401
