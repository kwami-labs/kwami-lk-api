"""`/webhooks/livekit` — room lifecycle from LiveKit Cloud, and `record_room_event`.

`livekit_sessions` always had `active`, `ended`, `last_seen_at` and `ended_at`, and
nothing wrote them. LiveKit announces each of those moments in a webhook signed with
the project's API key and secret over the SHA-256 of the body; these tests sign
deliveries the same way, so a change in what LiveKit sends or how it signs fails here.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime

import pytest
from livekit.api import AccessToken

from src.core.config import settings
from src.services.sessions import record_room_event

pytestmark = pytest.mark.anyio

URL = "/webhooks/livekit"
STARTED_AT = 1_762_430_000  # 2025-11-06T11:53:20Z


def _signed(body: str, *, secret: str | None = None) -> dict[str, str]:
    digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
    token = (
        AccessToken(settings.livekit_api_key, secret or settings.livekit_api_secret)
        .with_sha256(digest)
        .to_jwt()
    )
    return {"Authorization": token, "Content-Type": "application/webhook+json"}


def _event(name: str, room: str = "kwami-web-r1", created_at: int = STARTED_AT) -> str:
    return json.dumps(
        {
            "event": name,
            "room": {"name": room, "sid": "RM_x"},
            "id": "EV_1",
            "createdAt": created_at,
        }
    )


async def deliver(client, body: str, headers: dict[str, str] | None = None):
    return await client.post(URL, content=body, headers=headers or _signed(body))


@pytest.fixture
def issued_room(fake_supabase):
    fake_supabase.db.seed(
        "livekit_sessions",
        {
            "room_name": "kwami-web-r1",
            "user_id": "u1",
            "kwami_id": "k1",
            "source": "web",
            "status": "issued",
            "last_seen_at": None,
            "ended_at": None,
        },
    )
    return fake_supabase


def _row(fake_supabase) -> dict:
    [row] = fake_supabase.db.rows("livekit_sessions")
    return row


class TestTheRoute:
    async def test_a_started_room_becomes_active(self, client, issued_room):
        r = await deliver(client, _event("room_started"))

        assert r.status_code == 200
        assert r.json() == {"ok": True, "updated": True}
        row = _row(issued_room)
        assert row["status"] == "active"
        assert row["last_seen_at"] == datetime.fromtimestamp(STARTED_AT, UTC).isoformat()

    async def test_a_finished_room_is_ended_with_its_end_time(self, client, issued_room):
        await deliver(client, _event("room_started"))
        r = await deliver(client, _event("room_finished", created_at=STARTED_AT + 300))

        assert r.status_code == 200
        row = _row(issued_room)
        assert row["status"] == "ended"
        assert row["ended_at"] == datetime.fromtimestamp(STARTED_AT + 300, UTC).isoformat()

    async def test_a_bearer_prefix_is_accepted(self, client, issued_room):
        body = _event("participant_joined")
        headers = _signed(body)
        headers["Authorization"] = f"Bearer {headers['Authorization']}"

        r = await deliver(client, body, headers)

        assert r.status_code == 200
        assert _row(issued_room)["status"] == "active"

    async def test_an_event_without_a_time_is_stamped_now(self, client, issued_room):
        before = datetime.now(UTC)
        await deliver(client, _event("participant_joined", created_at=0))

        seen = datetime.fromisoformat(_row(issued_room)["last_seen_at"])
        assert seen >= before.replace(microsecond=0)

    async def test_a_room_this_api_never_issued_is_acknowledged_and_ignored(
        self, client, issued_room
    ):
        r = await deliver(client, _event("room_started", room="someone-elses-room"))

        assert r.status_code == 200
        assert r.json() == {"ok": True, "updated": False}
        assert _row(issued_room)["status"] == "issued"


class TestSignatures:
    async def test_an_unsigned_delivery_is_refused(self, client, issued_room, caplog):
        with caplog.at_level("WARNING", logger="kwami-api.webhooks"):
            r = await client.post(URL, content=_event("room_finished"))

        assert r.status_code == 401
        assert _row(issued_room)["status"] == "issued"
        assert "did not verify" in caplog.text

    async def test_a_body_changed_after_signing_is_refused(self, client, issued_room):
        signed_for = _event("room_started")
        r = await deliver(client, _event("room_finished"), _signed(signed_for))

        assert r.status_code == 401
        assert _row(issued_room)["status"] == "issued"

    async def test_a_signature_from_another_secret_is_refused(self, client, issued_room):
        body = _event("room_finished")
        r = await deliver(
            client, body, _signed(body, secret="another-secret-long-enough-for-hs256")
        )

        assert r.status_code == 401


class TestWhenTheDatabaseFails:
    async def test_the_delivery_is_left_for_livekit_to_retry(
        self, client, issued_room, monkeypatch, caplog
    ):
        async def boom(*args, **kwargs):
            raise RuntimeError("connection reset")

        monkeypatch.setattr("src.api.routes.webhooks.record_room_event", boom)
        with caplog.at_level("ERROR", logger="kwami-api.webhooks"):
            r = await deliver(client, _event("room_finished"))

        assert r.status_code == 503
        assert "Could not record LiveKit event room_finished" in caplog.text


class TestRecordRoomEvent:
    AT = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)

    async def test_leaving_only_moves_the_last_seen_time(self, issued_room):
        assert await record_room_event("participant_left", "kwami-web-r1", at=self.AT) is True

        row = _row(issued_room)
        assert row["status"] == "issued"
        assert row["last_seen_at"] == self.AT.isoformat()

    async def test_an_ended_room_is_never_reopened(self, issued_room):
        await record_room_event("room_finished", "kwami-web-r1", at=self.AT)

        # A late or redelivered join must not resurrect it.
        assert await record_room_event("participant_joined", "kwami-web-r1", at=self.AT) is False
        assert _row(issued_room)["status"] == "ended"

    async def test_an_abandoned_room_stays_abandoned(self, fake_supabase):
        fake_supabase.db.seed(
            "livekit_sessions", {"room_name": "r2", "user_id": "u1", "status": "abandoned"}
        )

        assert await record_room_event("room_started", "r2", at=self.AT) is False

    @pytest.mark.parametrize("event", ["egress_started", "ingress_ended", "", "room_metadata"])
    async def test_events_that_say_nothing_about_the_room_change_nothing(self, issued_room, event):
        assert await record_room_event(event, "kwami-web-r1", at=self.AT) is False
        assert _row(issued_room)["status"] == "issued"

    async def test_an_event_without_a_room_changes_nothing(self, issued_room):
        assert await record_room_event("room_started", "", at=self.AT) is False
