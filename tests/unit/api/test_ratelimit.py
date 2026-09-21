"""`src.api.ratelimit` — who a request is billed to, and what a 429 looks like.

The application had no rate limiting at all, while `src/core/errors.py` had
mapped 429 to `rate_limited` since it was written. These cover the two things
that decide whether a limiter helps or hurts: the identity it keys on, and
whether a throttled caller gets a usable answer.

Limits are disabled for the rest of the suite (tests/.env.test) because many
tests drive one endpoint dozens of times; the ones here turn the limiter on.
"""

from __future__ import annotations

import pytest
from slowapi.errors import RateLimitExceeded
from starlette.requests import Request

from src.api.ratelimit import (
    PROVISIONING_LIMIT,
    TOKEN_LIMIT,
    limiter,
    rate_limit_exceeded_handler,
    rate_limit_key,
)
from src.core.security import AuthUser

pytestmark = pytest.mark.anyio


def _request(headers: dict[str, str] | None = None, *, user=None, client=("10.0.0.1", 1234)):
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/token",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": client,
    }
    request = Request(scope)
    if user is not None:
        request.state.user = user
    return request


class TestRateLimitKey:
    def test_an_authenticated_caller_is_billed_by_user(self):
        """Not by IP: one office behind a NAT must not share a single budget."""
        user = AuthUser({"sub": "user-1", "email": "a@b.c"})
        assert rate_limit_key(_request(user=user)) == "user:user-1"

    def test_the_same_user_from_two_addresses_shares_one_budget(self):
        """The other half: changing address must not buy more headroom."""
        user = AuthUser({"sub": "user-1"})
        first = rate_limit_key(_request(user=user, client=("10.0.0.1", 1)))
        second = rate_limit_key(_request(user=user, client=("203.0.113.9", 1)))
        assert first == second

    def test_the_agent_key_is_an_identity(self):
        key = rate_limit_key(_request({"X-Kwami-API-Key": "agent-secret"}))
        assert key.startswith("key:")

    def test_the_admin_key_is_an_identity(self):
        assert rate_limit_key(_request({"X-Admin-API-Key": "admin-secret"})).startswith("key:")

    def test_the_secret_itself_never_becomes_the_key(self):
        """The key reaches the limiter's store and its log lines."""
        assert "agent-secret" not in rate_limit_key(_request({"X-Kwami-API-Key": "agent-secret"}))

    def test_two_different_keys_are_two_identities(self):
        a = rate_limit_key(_request({"X-Kwami-API-Key": "one"}))
        b = rate_limit_key(_request({"X-Kwami-API-Key": "two"}))
        assert a != b

    def test_an_anonymous_caller_falls_back_to_the_address(self):
        assert rate_limit_key(_request()) == "ip:10.0.0.1"

    def test_a_user_outranks_a_key(self):
        user = AuthUser({"sub": "user-1"})
        key = rate_limit_key(_request({"X-Kwami-API-Key": "agent"}, user=user))
        assert key == "user:user-1"


class TestRateLimitResponse:
    async def test_it_uses_the_shared_error_envelope(self):
        exc = RateLimitExceeded(type("L", (), {"error_message": None, "limit": TOKEN_LIMIT})())
        response = await rate_limit_exceeded_handler(_request(), exc)
        assert response.status_code == 429
        body = response.body.decode()
        assert '"code":"rate_limited"' in body
        assert "detail" in body, "the app reads `detail` on every error response"

    async def test_it_tells_the_caller_when_to_retry(self):
        exc = RateLimitExceeded(type("L", (), {"error_message": None, "limit": TOKEN_LIMIT})())
        response = await rate_limit_exceeded_handler(_request(), exc)
        assert int(response.headers["Retry-After"]) > 0

    async def test_a_non_slowapi_exception_still_answers_429(self):
        response = await rate_limit_exceeded_handler(_request(), RuntimeError("boom"))
        assert response.status_code == 429


class TestLimitsAreEnforced:
    """End to end, with the limiter switched on for the duration."""

    @pytest.fixture
    def limits_on(self, monkeypatch):
        monkeypatch.setattr(limiter, "enabled", True)
        limiter.reset()
        yield
        limiter.reset()

    async def test_a_burst_of_token_requests_is_throttled(
        self, limits_on, auth_client, fake_supabase, tenant
    ):
        allowed = int(TOKEN_LIMIT.split("/")[0])
        statuses = [
            (await auth_client.post("/token", json={})).status_code for _ in range(allowed + 5)
        ]
        assert 429 in statuses, "the limit has to actually bite"
        assert statuses.index(429) >= allowed, "and not before the budget is spent"

    async def test_the_throttled_response_carries_the_request_id(self, limits_on, auth_client):
        allowed = int(TOKEN_LIMIT.split("/")[0])
        response = None
        for _ in range(allowed + 5):
            response = await auth_client.post("/token", json={})
            if response.status_code == 429:
                break
        assert response is not None and response.status_code == 429
        assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]

    async def test_provisioning_is_limited_harder_than_tokens(self):
        """Buying a phone number spends real money at Twilio."""
        assert int(PROVISIONING_LIMIT.split("/")[0]) < int(TOKEN_LIMIT.split("/")[0])
