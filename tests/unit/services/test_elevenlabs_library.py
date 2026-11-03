"""`src.services.elevenlabs_voices` — searching the public library and saving from it.

What matters: a voice with its own rate is never offered, a free plan never saves a
voice the agent could not speak with, the catalogue is forgotten only after a real
add, and each refusal says what happened instead of a bare failure.
"""

from __future__ import annotations

from types import SimpleNamespace

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from src.services import elevenlabs_voices as el

pytestmark = pytest.mark.anyio

ENTRY = {
    "public_owner_id": "owner1",
    "voice_id": "lalo",
    "name": "Lalo - Friendly Mexican Spanish",
    "category": "professional",
    "gender": "male",
    "accent": "mexican",
    "language": "es",
    "descriptive": "chill",
    "preview_url": "https://cdn.test/lalo.mp3",
    "is_added_by_user": True,
}
PLAN = {
    "tier": "creator",
    "voice_slots_used": 12,
    "voice_limit": 30,
    "voice_add_edit_counter": 12,
    "max_voice_add_edits": 95,
}


def http_error(status: int) -> aiohttp.ClientResponseError:
    url = URL("https://api.elevenlabs.io/v1/shared-voices")
    info = aiohttp.RequestInfo(url, "GET", CIMultiDictProxy(CIMultiDict()), url)
    return aiohttp.ClientResponseError(info, (), status=status)


def answer_get(monkeypatch, answer) -> list[str]:
    """Answer `_get_json` with `answer` (a payload or an exception); log the paths."""
    paths: list[str] = []

    async def fake(_session, path):
        paths.append(path)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(el, "_get_json", fake)
    return paths


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    el.clear_cache()
    monkeypatch.setattr(el, "get_settings", lambda: SimpleNamespace(elevenlabs_api_key="k"))
    yield
    el.clear_cache()


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.setattr(el, "get_settings", lambda: SimpleNamespace(elevenlabs_api_key=None))


class TestToLibraryVoice:
    def test_the_flat_fields_read_like_an_account_voice(self):
        v = el.to_library_voice(ENTRY)
        assert v is not None
        assert (v.id, v.name, v.gender, v.language) == ("lalo", "Lalo", "male", "es")
        assert v.description == "Friendly Mexican Spanish (mexican)"
        assert v.preview_url == "https://cdn.test/lalo.mp3"
        assert (v.owner_id, v.full_name, v.in_account) == (
            "owner1",
            "Lalo - Friendly Mexican Spanish",
            True,
        )

    def test_without_its_owner_it_cannot_be_saved(self):
        assert el.to_library_voice({"voice_id": "x", "name": "X"}) is None

    def test_without_an_id_it_is_skipped(self):
        assert el.to_library_voice({"public_owner_id": "o"}) is None

    def test_a_missing_flag_means_not_in_the_account(self):
        v = el.to_library_voice({"public_owner_id": "o", "voice_id": "x"})
        assert v is not None and v.in_account is False and v.full_name == "x"


class TestSearchLibrary:
    async def test_asks_for_the_most_used_voices_at_the_plans_own_rate(self, monkeypatch):
        paths = answer_get(
            monkeypatch,
            {"voices": [ENTRY, {"name": "no id"}], "has_more": True, "total_count": 9},
        )
        page = await el.search_library(search="peninsular voz", language="es", page=2)
        assert [v.id for v in page.voices] == ["lalo"]
        assert page.has_more is True and page.total == 9
        assert paths == [
            "/shared-voices?page_size=30&page=2&sort=cloned_by_count"
            "&include_custom_rates=false&search=peninsular+voz&language=es"
        ]

    async def test_an_empty_answer_is_an_empty_page(self, monkeypatch):
        answer_get(monkeypatch, {})
        page = await el.search_library()
        assert page.voices == [] and page.has_more is False and page.total == 0

    @pytest.mark.usefixtures("no_key")
    async def test_without_a_key_it_says_so(self):
        with pytest.raises(el.VoiceLibraryNoKeyError):
            await el.search_library()

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (http_error(401), el.VoiceKeyRefusedError),
            (http_error(403), el.VoiceKeyRefusedError),
            (http_error(500), el.VoiceLibraryFailedError),
            (aiohttp.ClientConnectionError("down"), el.VoiceLibraryFailedError),
            (TimeoutError(), el.VoiceLibraryFailedError),
            (ValueError("not json"), el.VoiceLibraryFailedError),
        ],
    )
    async def test_each_failure_has_its_own_error(self, monkeypatch, error, expected):
        answer_get(monkeypatch, error)
        with pytest.raises(expected):
            await el.search_library()


class TestLibraryCapacity:
    async def test_reads_the_plans_figures(self, monkeypatch):
        answer_get(monkeypatch, PLAN)
        assert await el.library_capacity() == el.LibraryCapacity(
            tier="creator", voice_slots_used=12, voice_limit=30, adds_used=12, adds_limit=95
        )

    async def test_a_plan_without_a_limit_on_additions_has_none(self, monkeypatch):
        answer_get(monkeypatch, {"tier": "scale"})
        capacity = await el.library_capacity()
        assert capacity is not None and capacity.adds_limit is None and capacity.voice_limit == 0

    async def test_a_key_that_cannot_read_its_plan_gives_nothing(self, monkeypatch):
        answer_get(monkeypatch, http_error(401))
        assert await el.library_capacity() is None

    @pytest.mark.usefixtures("no_key")
    async def test_without_a_key_there_is_nothing_to_read(self):
        assert await el.library_capacity() is None
        assert el.has_key() is False


class _Post:
    """Answers the add with `answer`; logs what was posted."""

    def __init__(self):
        self.answer = (200, {"voice_id": "saved"})
        self.calls = []

    async def __call__(self, _session, path, payload):
        self.calls.append((path, payload))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class TestAddLibraryVoice:
    @pytest.fixture
    def post(self, monkeypatch):
        fake = _Post()
        monkeypatch.setattr(el, "_post_json", fake)
        answer_get(monkeypatch, PLAN)
        return fake

    async def test_posts_the_voice_and_forgets_the_catalogue(self, post):
        el._cache = (float("inf"), [])
        assert await el.add_library_voice("o", "v", "Lalo - Spanish") == "saved"
        assert post.calls == [("/voices/add/o/v", {"new_name": "Lalo - Spanish"})]
        assert el._cache is None

    @pytest.mark.parametrize(("owner", "voice"), [("../user", "v"), ("o", "a/b"), ("o", "")])
    async def test_an_id_that_could_change_the_route_never_goes_out(self, post, owner, voice):
        with pytest.raises(el.VoiceWithdrawnError):
            await el.add_library_voice(owner, voice, "n")
        assert post.calls == []

    async def test_an_answer_without_an_id_keeps_the_librarys(self, post):
        post.answer = (200, None)
        assert await el.add_library_voice("o", "v", "n") == "v"

    async def test_a_refused_add_keeps_the_catalogue(self, post):
        cached = (float("inf"), [])
        el._cache = cached
        post.answer = (500, None)
        with pytest.raises(el.VoiceLibraryFailedError):
            await el.add_library_voice("o", "v", "n")
        assert el._cache is cached

    async def test_a_free_plan_is_refused_before_asking(self, monkeypatch, post):
        answer_get(monkeypatch, {"tier": "free"})
        with pytest.raises(el.VoiceFreePlanError):
            await el.add_library_voice("o", "v", "n")
        assert post.calls == []

    async def test_an_unknown_plan_lets_elevenlabs_decide(self, monkeypatch, post):
        answer_get(monkeypatch, http_error(401))
        assert await el.add_library_voice("o", "v", "n") == "saved"

    @pytest.mark.usefixtures("no_key")
    async def test_without_a_key_it_says_so(self):
        with pytest.raises(el.VoiceLibraryNoKeyError):
            await el.add_library_voice("o", "v", "n")

    @pytest.mark.parametrize("error", [aiohttp.ClientConnectionError("down"), TimeoutError()])
    async def test_no_answer_is_a_failure(self, post, error):
        post.answer = error
        with pytest.raises(el.VoiceLibraryFailedError):
            await el.add_library_voice("o", "v", "n")

    @pytest.mark.parametrize(
        ("status", "body", "expected"),
        [
            (
                400,
                {"detail": {"status": "voice_limit_reached", "message": "x"}},
                el.VoiceNoRoomError,
            ),
            (
                403,
                {"detail": {"status": "x", "message": "Your maximum amount of custom voices"}},
                el.VoiceNoRoomError,
            ),
            (402, {"detail": "voice add limit"}, el.VoiceNoRoomError),
            (404, {"detail": "voice not found"}, el.VoiceWithdrawnError),
            (401, {"detail": {"status": "missing_permissions"}}, el.VoiceKeyRefusedError),
            (403, None, el.VoiceKeyRefusedError),
            # A rate limit or a bad name is not the account being full.
            (429, {"detail": "voice limit"}, el.VoiceLibraryFailedError),
            (422, {"detail": [{"msg": "voice name maximum"}]}, el.VoiceLibraryFailedError),
            (400, "plain", el.VoiceLibraryFailedError),
        ],
    )
    async def test_a_refusal_is_read(self, post, status, body, expected):
        post.answer = (status, body)
        with pytest.raises(expected):
            await el.add_library_voice("o", "v", "n")


class _PostResponse:
    status = 200

    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type="application/json"):
        assert content_type is None
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _PostSession:
    def __init__(self, response):
        self.response, self.posted = response, []

    def post(self, url, json):
        self.posted.append((url, json))
        return self.response


class TestPostJson:
    async def test_gives_the_status_and_the_body(self):
        session = _PostSession(_PostResponse({"voice_id": "v"}))
        answer = await el._post_json(session, "/voices/add/o/v", {"new_name": "n"})  # type: ignore[arg-type]
        assert answer == (200, {"voice_id": "v"})
        assert session.posted == [
            ("https://api.elevenlabs.io/v1/voices/add/o/v", {"new_name": "n"})
        ]

    async def test_a_body_that_is_not_json_is_none(self):
        session = _PostSession(_PostResponse(ValueError("html")))
        assert await el._post_json(session, "/x", {}) == (200, None)  # type: ignore[arg-type]
