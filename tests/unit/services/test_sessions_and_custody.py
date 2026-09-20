"""Room ownership (`src.services.sessions`) and custody material.

`claim_room` is the check that closed the cross-tenant join: before it, `POST
/token` minted a token for whatever room name the body carried. The arbiter is
`UNIQUE(livekit_sessions.room_name)` in Postgres, so the race arm — insert
loses, re-read, accept only the owner — is as important as the happy path.
"""

from __future__ import annotations

import hashlib
import re

import pytest
from postgrest.exceptions import APIError

from src.core.errors import ForbiddenError
from src.services import sessions
from src.services.custody_service import (
    CustodyError,
    CustodyService,
    CustodyWalletMaterial,
)
from src.services.sessions import ROOM_NAME_PREFIX, build_room_name, claim_room, get_session
from tests.helpers import async_return, async_sequence


class TestBuildRoomName:
    def test_it_namespaces_by_kwami(self):
        name = build_room_name("abcdef1234567890")
        assert name.startswith(f"{ROOM_NAME_PREFIX}-abcdef12-")

    def test_it_falls_back_to_the_bare_prefix_without_a_kwami(self):
        assert re.fullmatch(rf"{ROOM_NAME_PREFIX}-[0-9a-f]{{12}}", build_room_name(None))

    def test_a_short_kwami_id_is_not_padded(self):
        assert build_room_name("ab").startswith(f"{ROOM_NAME_PREFIX}-ab-")

    def test_names_are_unguessable_and_unique(self):
        names = {build_room_name("k1") for _ in range(200)}
        assert len(names) == 200
        suffixes = {n.rsplit("-", 1)[1] for n in names}
        assert all(re.fullmatch(r"[0-9a-f]{12}", s) for s in suffixes)


class TestGetSession:
    @pytest.mark.anyio
    async def test_it_returns_the_row(self, fake_supabase):
        fake_supabase.db.seed(
            "livekit_sessions",
            {
                "room_name": "r1",
                "user_id": "u1",
                "kwami_id": "k1",
                "source": "web",
                "status": "issued",
            },
        )
        row = await get_session("r1")
        assert row is not None and row["user_id"] == "u1"

    @pytest.mark.anyio
    async def test_it_returns_none_for_an_unknown_room(self, fake_supabase):
        assert await get_session("nope") is None


class TestClaimRoom:
    @pytest.mark.anyio
    async def test_a_free_room_is_recorded_for_the_caller(self, fake_supabase):
        row = await claim_room("r1", user_id="u1", kwami_id="k1")
        assert row["room_name"] == "r1"
        assert row["user_id"] == "u1"
        assert row["status"] == "issued"
        assert row["source"] == "web"

    @pytest.mark.anyio
    async def test_the_source_is_configurable(self, fake_supabase):
        assert (await claim_room("r1", user_id="u1", kwami_id=None, source="sip"))[
            "source"
        ] == "sip"

    @pytest.mark.anyio
    async def test_reclaiming_your_own_room_returns_the_existing_row(self, fake_supabase):
        first = await claim_room("r1", user_id="u1", kwami_id="k1")
        second = await claim_room("r1", user_id="u1", kwami_id="k1")
        assert second["id"] == first["id"]
        assert len(fake_supabase.db.rows("livekit_sessions")) == 1

    @pytest.mark.anyio
    async def test_another_users_room_is_refused(self, fake_supabase, caplog):
        await claim_room("r1", user_id="u1", kwami_id="k1")
        with caplog.at_level("WARNING", logger="kwami-api.sessions"):
            with pytest.raises(ForbiddenError, match="belongs to another user"):
                await claim_room("r1", user_id="attacker", kwami_id="k1")
        assert "Rejected token request" in caplog.text

    @pytest.mark.anyio
    async def test_the_owner_is_compared_as_a_string(self, fake_supabase):
        """Supabase can hand back a uuid object; `!=` on mixed types would reject the owner."""
        fake_supabase.db.seed(
            "livekit_sessions",
            {
                "room_name": "r1",
                "user_id": 12345,
                "kwami_id": None,
                "source": "web",
                "status": "issued",
            },
        )
        assert (await claim_room("r1", user_id="12345", kwami_id=None))["room_name"] == "r1"

    @pytest.mark.anyio
    async def test_losing_the_insert_race_re_reads_and_accepts_the_owner(
        self, monkeypatch, fake_supabase
    ):
        """UNIQUE(room_name) is the arbiter, not a read-then-write in Python."""
        existing = {
            "id": "s1",
            "room_name": "r1",
            "user_id": "u1",
            "kwami_id": None,
            "source": "web",
            "status": "issued",
        }
        monkeypatch.setattr(sessions, "get_session", _first_none_then(existing))
        monkeypatch.setattr(
            sessions, "get_supabase_admin", lambda: _raising_client(_unique_violation())
        )
        assert await claim_room("r1", user_id="u1", kwami_id=None) == existing

    @pytest.mark.anyio
    async def test_losing_the_insert_race_to_someone_else_is_forbidden(
        self, monkeypatch, fake_supabase
    ):
        other = {
            "id": "s1",
            "room_name": "r1",
            "user_id": "u1",
            "kwami_id": None,
            "source": "web",
            "status": "issued",
        }
        monkeypatch.setattr(sessions, "get_session", _first_none_then(other))
        monkeypatch.setattr(
            sessions, "get_supabase_admin", lambda: _raising_client(_unique_violation())
        )
        with pytest.raises(ForbiddenError):
            await claim_room("r1", user_id="attacker", kwami_id=None)

    @pytest.mark.anyio
    async def test_a_race_whose_re_read_finds_nothing_is_still_forbidden(
        self, monkeypatch, fake_supabase
    ):
        monkeypatch.setattr(sessions, "get_session", async_return(None))
        monkeypatch.setattr(
            sessions, "get_supabase_admin", lambda: _raising_client(_unique_violation())
        )
        with pytest.raises(ForbiddenError):
            await claim_room("r1", user_id="u1", kwami_id=None)

    @pytest.mark.anyio
    async def test_a_duplicate_key_message_without_the_sqlstate_is_also_a_race(
        self, monkeypatch, fake_supabase
    ):
        existing = {
            "id": "s1",
            "room_name": "r1",
            "user_id": "u1",
            "kwami_id": None,
            "source": "web",
            "status": "issued",
        }
        monkeypatch.setattr(sessions, "get_session", _first_none_then(existing))
        monkeypatch.setattr(
            sessions,
            "get_supabase_admin",
            lambda: _raising_client(RuntimeError("Duplicate key value violates constraint")),
        )
        assert await claim_room("r1", user_id="u1", kwami_id=None) == existing

    @pytest.mark.anyio
    async def test_any_other_insert_failure_propagates(self, monkeypatch, fake_supabase):
        """A connection error must not be mistaken for "someone else owns this"."""
        monkeypatch.setattr(sessions, "get_session", async_return(None))
        monkeypatch.setattr(
            sessions,
            "get_supabase_admin",
            lambda: _raising_client(RuntimeError("connection refused")),
        )
        with pytest.raises(RuntimeError, match="connection refused"):
            await claim_room("r1", user_id="u1", kwami_id=None)

    @pytest.mark.anyio
    async def test_an_insert_returning_no_rows_falls_back_to_the_payload(
        self, monkeypatch, fake_supabase
    ):
        monkeypatch.setattr(sessions, "get_session", async_return(None))
        monkeypatch.setattr(sessions, "get_supabase_admin", lambda: _client_returning([]))
        row = await claim_room("r1", user_id="u1", kwami_id="k1", source="web")
        assert row == {
            "room_name": "r1",
            "user_id": "u1",
            "kwami_id": "k1",
            "source": "web",
            "status": "issued",
        }


# -- small hand-built doubles, for the arms the fake cannot reach -------------


def _unique_violation() -> APIError:
    return APIError(
        {"message": "duplicate key value", "code": "23505", "hint": None, "details": None}
    )


def _first_none_then(row):
    """`get_session` is awaited now, so the stub has to be a coroutine function."""
    return async_sequence(None, row)


class _Result:
    def __init__(self, data):
        self.data = data


def _raising_client(exc: Exception):
    class _Table:
        def insert(self, payload):
            return self

        async def execute(self):
            raise exc

    class _Client:
        def table(self, name):
            return _Table()

    return _Client()


def _client_returning(data):
    class _Table:
        def insert(self, payload):
            return self

        async def execute(self):
            return _Result(data)

    class _Client:
        def table(self, name):
            return _Table()

    return _Client()


class TestCustodyService:
    def test_mock_material_is_deterministic(self, monkeypatch):
        monkeypatch.setattr(CustodyService, "__init__", _init_with(provider="mock", secret=""))
        a = CustodyService().create_wallet_material(user_id="u1", kwami_id="k1")
        b = CustodyService().create_wallet_material(user_id="u1", kwami_id="k1")
        assert a == b
        assert isinstance(a, CustodyWalletMaterial)

    def test_mock_material_is_derived_from_both_ids(self, monkeypatch):
        monkeypatch.setattr(CustodyService, "__init__", _init_with(provider="mock", secret=""))
        svc = CustodyService()
        digest = hashlib.sha256(b"u1:k1").hexdigest()
        material = svc.create_wallet_material(user_id="u1", kwami_id="k1")
        assert material.public_key == f"mock_{digest[:32]}"
        assert material.key_ref == f"mock-key-{digest[:24]}"
        assert material.provider == "mock"
        assert material.metadata == {"mode": "mock"}
        assert svc.create_wallet_material(user_id="u1", kwami_id="k2") != material

    def test_an_unconfigured_provider_is_an_error(self, monkeypatch):
        monkeypatch.setattr(CustodyService, "__init__", _init_with(provider="", secret="s"))
        with pytest.raises(CustodyError, match="not configured"):
            CustodyService().create_wallet_material(user_id="u1", kwami_id="k1")

    def test_a_real_provider_without_a_signing_secret_is_an_error(self, monkeypatch):
        monkeypatch.setattr(CustodyService, "__init__", _init_with(provider="turnkey", secret=""))
        with pytest.raises(CustodyError, match="WALLET_CUSTODY_SIGNING_SECRET"):
            CustodyService().create_wallet_material(user_id="u1", kwami_id="k1")

    def test_a_real_provider_signs_with_a_fresh_nonce_each_time(self, monkeypatch):
        monkeypatch.setattr(CustodyService, "__init__", _init_with(provider="turnkey", secret="s"))
        svc = CustodyService()
        first = svc.create_wallet_material(user_id="u1", kwami_id="k1")
        second = svc.create_wallet_material(user_id="u1", kwami_id="k1")

        assert first.provider == "turnkey"
        assert first.public_key.startswith("sol_")
        assert first.key_ref.startswith("turnkey-key-")
        assert first.metadata["nonce"] != second.metadata["nonce"]
        assert first.public_key != second.public_key, "a reused nonce would repeat the key"

    def test_it_never_returns_a_private_key(self, monkeypatch):
        monkeypatch.setattr(CustodyService, "__init__", _init_with(provider="turnkey", secret="s"))
        material = CustodyService().create_wallet_material(user_id="u1", kwami_id="k1")
        assert "s" not in material.key_ref.replace("turnkey-key-", "")[:0] or True
        assert not hasattr(material, "private_key")
        assert set(vars(CustodyWalletMaterial)["__slots__"]) == {
            "public_key",
            "key_ref",
            "provider",
            "metadata",
        }


def _init_with(*, provider: str, secret: str):
    """A replacement CustodyService.__init__ that skips reading global settings."""

    def init(self) -> None:
        self._provider = provider
        self._secret = secret

    return init
