"""`src.services.livekit` — the one endpoint whose output is a credential.

The grants and the TTL are the security surface: a token that over-grants, or
one that outlives its session, is the failure that matters. Tokens are decoded
back with the signing secret rather than asserted as opaque strings.
"""

from __future__ import annotations

from datetime import timedelta

import jwt
import pytest

from src.core.config import settings
from src.services.livekit import _default_agent_name, create_token


def decode(token: str) -> dict:
    return jwt.decode(
        token,
        settings.livekit_api_secret,
        algorithms=["HS256"],
        options={"verify_aud": False},
    )


def test_the_agent_name_comes_from_settings(monkeypatch):
    """It was a module constant duplicating the setting, so web and SIP could differ."""
    monkeypatch.setattr(settings, "livekit_agent_name", "another-agent", raising=False)
    assert _default_agent_name() == "another-agent"


class TestIdentityAndName:
    def test_the_identity_defaults_to_the_participant_name(self):
        claims = decode(create_token("room", "alice"))
        assert claims["sub"] == "alice"
        assert claims["name"] == "alice"

    def test_an_explicit_identity_is_used(self):
        claims = decode(create_token("room", "Alice", participant_identity="user-1"))
        assert claims["sub"] == "user-1"
        assert claims["name"] == "Alice"


class TestGrants:
    def test_the_defaults_are_a_normal_publishing_participant(self):
        grants = decode(create_token("room-1", "alice"))["video"]
        assert grants["room"] == "room-1"
        assert grants["roomJoin"] is True
        assert grants.get("canPublish", True) is True
        assert grants.get("canSubscribe", True) is True
        assert grants.get("roomCreate", False) is False
        assert grants.get("roomAdmin", False) is False
        assert grants.get("agent", False) is False

    def test_every_permission_can_be_withdrawn(self):
        grants = decode(
            create_token(
                "room",
                "alice",
                can_publish=False,
                can_subscribe=False,
                can_publish_data=False,
                can_update_own_metadata=False,
                room_join=False,
            )
        )["video"]
        assert grants.get("canPublish", False) is False
        assert grants.get("canSubscribe", False) is False
        assert grants.get("canPublishData", False) is False
        assert grants.get("canUpdateOwnMetadata", False) is False
        assert grants.get("roomJoin", False) is False

    def test_elevated_permissions_are_opt_in(self):
        grants = decode(create_token("room", "svc", room_create=True, room_admin=True, agent=True))[
            "video"
        ]
        assert grants["roomCreate"] is True
        assert grants["roomAdmin"] is True
        assert grants["agent"] is True


class TestTtl:
    def test_it_defaults_to_the_configured_minutes(self, monkeypatch):
        monkeypatch.setattr(settings, "livekit_token_ttl_minutes", 15, raising=False)
        claims = decode(create_token("room", "alice"))
        assert claims["exp"] - claims["nbf"] == 15 * 60

    def test_the_setting_is_honoured_when_changed(self, monkeypatch):
        monkeypatch.setattr(settings, "livekit_token_ttl_minutes", 1, raising=False)
        claims = decode(create_token("room", "alice"))
        assert claims["exp"] - claims["nbf"] == 60

    def test_an_explicit_ttl_overrides_the_setting(self, monkeypatch):
        monkeypatch.setattr(settings, "livekit_token_ttl_minutes", 15, raising=False)
        claims = decode(create_token("room", "alice", ttl=timedelta(seconds=30)))
        assert claims["exp"] - claims["nbf"] == 30


class TestAgentDispatch:
    def test_an_agent_is_dispatched_by_default(self, monkeypatch):
        monkeypatch.setattr(settings, "livekit_agent_name", "kwami-agent", raising=False)
        cfg = decode(create_token("room", "alice"))["roomConfig"]
        assert cfg["agents"] == [{"agentName": "kwami-agent"}]

    def test_the_kwami_id_rides_along_as_metadata(self, monkeypatch):
        monkeypatch.setattr(settings, "livekit_agent_name", "kwami-agent", raising=False)
        cfg = decode(create_token("room", "alice", kwami_id="k-123"))["roomConfig"]
        assert cfg["agents"] == [{"agentName": "kwami-agent", "metadata": "k-123"}]

    def test_an_explicit_agent_name_wins(self, monkeypatch):
        monkeypatch.setattr(settings, "livekit_agent_name", "kwami-agent", raising=False)
        cfg = decode(create_token("room", "alice", agent_name="special"))["roomConfig"]
        assert cfg["agents"][0]["agentName"] == "special"

    def test_dispatch_can_be_turned_off(self):
        """The branch that was never covered: a token with no room config at all."""
        assert "roomConfig" not in decode(create_token("room", "alice", dispatch_agent=False))


def test_the_token_is_signed_with_the_configured_key():
    claims = decode(create_token("room", "alice"))
    assert claims["iss"] == settings.livekit_api_key

    # Padded to 32 bytes: a shorter key trips PyJWT's InsecureKeyLengthWarning,
    # which `filterwarnings = ["error", ...]` turns into a failure before the
    # signature is ever checked -- hiding what this test is actually asserting.
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(
            create_token("room", "alice"),
            "not-the-secret".ljust(32, "x"),
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
