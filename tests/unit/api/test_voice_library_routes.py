"""`/voices/library` — searching ElevenLabs' library and saving a voice from it.

The account is the whole deployment's, so what matters is who gets in, what reaches
ElevenLabs' path, and that each refusal arrives with a code the app can explain.
"""

from __future__ import annotations

import pytest

from src.core import security
from src.services import elevenlabs_voices as el
from src.services.elevenlabs_voices import LibraryCapacity, LibraryPage, LibraryVoice

pytestmark = pytest.mark.anyio

ADD = "/voices/library/owner1/voice1"


@pytest.fixture
def admin(monkeypatch, tenant):
    """Make the seeded tenant an administrator."""
    emails = [tenant.auth_user.email]
    monkeypatch.setattr(type(security.settings), "admin_emails", property(lambda self: emails))


class TestAccess:
    async def test_needs_a_login(self, client):
        assert (await client.get("/voices/library/access")).status_code == 401

    async def test_a_plain_no_for_everybody_else_without_asking_elevenlabs(
        self, tenant_client, monkeypatch
    ):
        async def never():
            raise AssertionError("no call to ElevenLabs for a non-administrator")

        monkeypatch.setattr(el, "library_capacity", never)
        response = await tenant_client.get("/voices/library/access")
        assert response.status_code == 200
        assert response.json() == {"allowed": False, "capacity": None}

    async def test_a_plain_no_without_a_key(self, tenant_client, admin, monkeypatch):
        monkeypatch.setattr(el, "has_key", lambda: False)
        assert (await tenant_client.get("/voices/library/access")).json()["allowed"] is False

    async def test_an_administrator_also_gets_the_room_left(
        self, tenant_client, admin, monkeypatch
    ):
        async def capacity():
            return LibraryCapacity(tier="creator", voice_limit=30, adds_used=12, adds_limit=95)

        monkeypatch.setattr(el, "has_key", lambda: True)
        monkeypatch.setattr(el, "library_capacity", capacity)
        body = (await tenant_client.get("/voices/library/access")).json()
        assert body["allowed"] is True
        assert body["capacity"]["adds_limit"] == 95 and body["capacity"]["voice_limit"] == 30


class TestSearch:
    async def test_needs_a_login(self, client):
        assert (await client.get("/voices/library")).status_code == 401

    async def test_is_only_for_administrators(self, tenant_client):
        assert (await tenant_client.get("/voices/library")).status_code == 403

    async def test_passes_the_filters_on(self, tenant_client, admin, monkeypatch):
        seen: dict = {}

        async def search(**kwargs):
            seen.update(kwargs)
            return LibraryPage(
                voices=[LibraryVoice(id="v", name="V", owner_id="o", full_name="V - x")],
                has_more=True,
                total=1,
            )

        monkeypatch.setattr(el, "search_library", search)
        response = await tenant_client.get(
            "/voices/library",
            params={"search": "madrid", "language": "es", "gender": "female", "page": 2},
        )
        assert response.status_code == 200
        assert seen == {"search": "madrid", "language": "es", "gender": "female", "page": 2}
        body = response.json()
        assert body["has_more"] is True
        assert body["voices"][0]["owner_id"] == "o" and body["voices"][0]["in_account"] is False

    @pytest.mark.parametrize(
        "params", [{"language": "ES!"}, {"gender": "other"}, {"page": -1}, {"search": "x" * 121}]
    )
    async def test_a_bad_filter_never_leaves(self, tenant_client, admin, params):
        assert (await tenant_client.get("/voices/library", params=params)).status_code == 422

    async def test_without_a_key_it_says_so(self, tenant_client, admin, monkeypatch):
        async def search(**_):
            raise el.VoiceLibraryNoKeyError

        monkeypatch.setattr(el, "search_library", search)
        response = await tenant_client.get("/voices/library")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "voice_library_no_key"


class TestAdd:
    async def test_needs_a_login(self, client):
        assert (await client.post(ADD, json={"name": "n"})).status_code == 401

    async def test_is_only_for_administrators(self, tenant_client, monkeypatch):
        async def never(*_):
            raise AssertionError("nothing is added for a non-administrator")

        monkeypatch.setattr(el, "add_library_voice", never)
        assert (await tenant_client.post(ADD, json={"name": "n"})).status_code == 403

    async def test_saves_it_under_the_trimmed_name(self, tenant_client, admin, monkeypatch):
        seen: list = []

        async def add(*args):
            seen.append(args)
            return "saved"

        monkeypatch.setattr(el, "add_library_voice", add)
        response = await tenant_client.post(ADD, json={"name": "  Lalo - Spanish "})
        assert response.status_code == 201 and response.json() == {"id": "saved"}
        assert seen == [("owner1", "voice1", "Lalo - Spanish")]

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            (ADD, {"name": "   "}),
            (ADD, {"name": "x" * 121}),
            (ADD, {}),
            ("/voices/library/own%2E%2Eer/voice1", {"name": "n"}),
            ("/voices/library/owner1/vo%3Fice", {"name": "n"}),
            ("/voices/library/" + "o" * 129 + "/voice1", {"name": "n"}),
        ],
    )
    async def test_nothing_odd_reaches_elevenlabs(
        self, tenant_client, admin, monkeypatch, path, body
    ):
        async def never(*_):
            raise AssertionError("this must not reach ElevenLabs")

        monkeypatch.setattr(el, "add_library_voice", never)
        assert (await tenant_client.post(path, json=body)).status_code == 422

    @pytest.mark.parametrize(
        ("error", "status", "code"),
        [
            (el.VoiceNoRoomError, 409, "voice_no_room"),
            (el.VoiceFreePlanError, 409, "voice_free_plan"),
            (el.VoiceWithdrawnError, 404, "voice_withdrawn"),
            (el.VoiceKeyRefusedError, 502, "voice_key_refused"),
            (el.VoiceLibraryFailedError, 502, "voice_library_failed"),
            (el.VoiceLibraryNoKeyError, 503, "voice_library_no_key"),
        ],
    )
    async def test_each_refusal_arrives_with_its_code(
        self, tenant_client, admin, monkeypatch, error, status, code
    ):
        async def add(*_):
            raise error

        monkeypatch.setattr(el, "add_library_voice", add)
        response = await tenant_client.post(ADD, json={"name": "n"})
        assert response.status_code == status
        assert response.json()["error"]["code"] == code
