"""`/internal/browser-contexts/*` — the memory behind "carry on where I left off".

The agent's navigation panel runs a cloud browser holding the user's real
cookies and logins. Browserbase addresses that persisted state by an opaque
Context id returned once at creation, with no lookup-by-name endpoint, so these
three routes are the only way back to it.

They are guarded by the same shared secret as every other `/internal/*` route,
and the auth assertions matter more here than the plumbing: the response body
is a handle to a browser profile that is already signed in to the user's email,
bank and everything else.
"""

import pytest
from httpx import AsyncClient

from src.core.config import settings

OWNER = "3f1a7b2c-9d4e-4f55-8a11-2b3c4d5e6f70"
TELEPHONY_OWNER = "sip_+14155552671"


def path(owner: str = OWNER) -> str:
    return f"/internal/browser-contexts/{owner}"


def key_header() -> dict[str, str]:
    return {"X-Kwami-API-Key": settings.kwami_api_key}


def body(context_id: str = "ctx-7", vendor: str = "browserbase") -> dict:
    return {"vendor": vendor, "context_id": context_id}


# -- Authentication ----------------------------------------------------------


@pytest.mark.anyio
async def test_reading_requires_the_agent_key(client: AsyncClient):
    response = await client.get(path(), params={"vendor": "browserbase"})
    assert response.status_code == 401


@pytest.mark.anyio
async def test_writing_requires_the_agent_key(client: AsyncClient):
    response = await client.post(path(), json=body())
    assert response.status_code == 401


@pytest.mark.anyio
async def test_deleting_requires_the_agent_key(client: AsyncClient):
    response = await client.delete(path())
    assert response.status_code == 401


@pytest.mark.anyio
async def test_a_wrong_key_is_rejected(client: AsyncClient):
    response = await client.get(
        path(),
        params={"vendor": "browserbase"},
        headers={"X-Kwami-API-Key": "not-the-key"},
    )
    assert response.status_code == 401


# -- Round trip --------------------------------------------------------------


@pytest.mark.anyio
async def test_a_saved_context_is_read_back(client: AsyncClient):
    saved = await client.post(path(), json=body(), headers=key_header())
    assert saved.status_code == 200

    response = await client.get(path(), params={"vendor": "browserbase"}, headers=key_header())

    assert response.status_code == 200
    assert response.json()["context_id"] == "ctx-7"


@pytest.mark.anyio
async def test_an_unknown_owner_is_a_404_not_an_empty_success(client: AsyncClient):
    """The agent branches on 404 to mean "create a fresh context"."""
    response = await client.get(
        path("nobody"), params={"vendor": "browserbase"}, headers=key_header()
    )
    assert response.status_code == 404


@pytest.mark.anyio
async def test_saving_twice_replaces_rather_than_duplicates(client: AsyncClient):
    """Two sessions starting at once must not leave two half-signed-in profiles."""
    await client.post(path(), json=body("ctx-old"), headers=key_header())
    await client.post(path(), json=body("ctx-new"), headers=key_header())

    response = await client.get(path(), params={"vendor": "browserbase"}, headers=key_header())

    assert response.json()["context_id"] == "ctx-new"


@pytest.mark.anyio
async def test_vendors_are_stored_separately(client: AsyncClient):
    """The two vendors do not share saved logins, so they cannot share a row."""
    await client.post(path(), json=body("ctx-bb", "browserbase"), headers=key_header())
    await client.post(path(), json=body("prof-bu", "browser_use"), headers=key_header())

    browserbase = await client.get(path(), params={"vendor": "browserbase"}, headers=key_header())
    browser_use = await client.get(path(), params={"vendor": "browser_use"}, headers=key_header())

    assert browserbase.json()["context_id"] == "ctx-bb"
    assert browser_use.json()["context_id"] == "prof-bu"


@pytest.mark.anyio
async def test_owners_never_see_each_others_profiles(client: AsyncClient):
    """A shared profile would hand one user's live logins to the next."""
    await client.post(path(), json=body("ctx-mine"), headers=key_header())

    response = await client.get(
        path("11111111-2222-3333-4444-555555555555"),
        params={"vendor": "browserbase"},
        headers=key_header(),
    )

    assert response.status_code == 404


@pytest.mark.anyio
async def test_a_telephony_identity_is_a_valid_owner(client: AsyncClient):
    """`kwami_id` falls back to the participant identity, which is not a uuid."""
    saved = await client.post(path(TELEPHONY_OWNER), json=body(), headers=key_header())
    assert saved.status_code == 200

    response = await client.get(
        path(TELEPHONY_OWNER), params={"vendor": "browserbase"}, headers=key_header()
    )
    assert response.json()["context_id"] == "ctx-7"


# -- Rejected input ----------------------------------------------------------


@pytest.mark.anyio
async def test_an_unknown_vendor_is_a_400_not_a_500(client: AsyncClient):
    response = await client.post(path(), json=body(vendor="playwright"), headers=key_header())
    assert response.status_code == 400


@pytest.mark.anyio
async def test_a_blank_context_id_is_refused(client: AsyncClient):
    response = await client.post(path(), json=body(context_id=""), headers=key_header())
    assert response.status_code == 422


@pytest.mark.anyio
async def test_the_vendor_is_required_when_reading(client: AsyncClient):
    response = await client.get(path(), headers=key_header())
    assert response.status_code == 422


# -- Clearing ----------------------------------------------------------------


@pytest.mark.anyio
async def test_deleting_forgets_every_vendor_for_that_owner(client: AsyncClient):
    await client.post(path(), json=body("ctx-bb", "browserbase"), headers=key_header())
    await client.post(path(), json=body("prof-bu", "browser_use"), headers=key_header())

    removed = await client.delete(path(), headers=key_header())
    assert removed.status_code == 200
    assert removed.json()["removed"] == 2

    for vendor in ("browserbase", "browser_use"):
        response = await client.get(path(), params={"vendor": vendor}, headers=key_header())
        assert response.status_code == 404


@pytest.mark.anyio
async def test_deleting_one_vendor_leaves_the_other(client: AsyncClient):
    await client.post(path(), json=body("ctx-bb", "browserbase"), headers=key_header())
    await client.post(path(), json=body("prof-bu", "browser_use"), headers=key_header())

    await client.delete(path(), params={"vendor": "browserbase"}, headers=key_header())

    gone = await client.get(path(), params={"vendor": "browserbase"}, headers=key_header())
    kept = await client.get(path(), params={"vendor": "browser_use"}, headers=key_header())
    assert gone.status_code == 404
    assert kept.status_code == 200
