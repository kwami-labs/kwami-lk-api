"""`/contacts` — per-kwami contacts CRUD.

Two rules run through every route: the kwami must belong to the caller, and the
contact must belong to the kwami named in the request. The second is easy to
lose because the contact lookup is already scoped by user, so a caller could
otherwise move another of their own kwamis' contacts by naming the wrong id.
Phone numbers are normalised to E.164 on the way in.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.anyio

# `src.services.channels.get_owned_kwami` raises a bare ValueError rather than a
# DomainError, so an ownership rejection reaches the catch-all handler and becomes
# a 500. The shared `client` fixtures re-raise app exceptions, which would surface
# that as an error rather than a response, so these tests use a transport that
# lets the handler produce its response -- the same one uvicorn produces.
LEGACY_OWNERSHIP_STATUS = 500


def _nonraising(app, user=None):
    from tests.conftest import TEST_USER_HEADER

    headers = {TEST_USER_HEADER: user.id} if user else {}
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers=headers,
    )


@pytest.fixture
async def tenant_client_soft(app_instance, tenant, auth_registry):
    """`tenant_client`, but 500s come back as responses instead of exceptions."""
    auth_registry[tenant.auth_user.id] = tenant.auth_user
    async with _nonraising(app_instance, tenant.auth_user) as c:
        yield c


@pytest.fixture
async def other_tenant_client_soft(app_instance, other_tenant, auth_registry):
    auth_registry[other_tenant.auth_user.id] = other_tenant.auth_user
    async with _nonraising(app_instance, other_tenant.auth_user) as c:
        yield c


def _body(kwami_id: str, **overrides) -> dict:
    return {
        "kwamiId": kwami_id,
        "displayName": "Ada Lovelace",
        "phoneNumber": "+14155552671",
        **overrides,
    }


async def _create(client, kwami_id, **overrides):
    r = await client.post("/contacts", json=_body(kwami_id, **overrides))
    assert r.status_code == 200, r.text
    return r.json()["contact"]


class TestList:
    async def test_an_empty_kwami_lists_nothing(self, tenant_client, tenant):
        r = await tenant_client.get("/contacts", params={"kwamiId": tenant.kwami_id})
        assert r.status_code == 200
        assert r.json() == {"contacts": []}

    async def test_it_lists_what_was_created(self, tenant_client, tenant):
        await _create(tenant_client, tenant.kwami_id)
        r = await tenant_client.get("/contacts", params={"kwamiId": tenant.kwami_id})
        assert [c["display_name"] for c in r.json()["contacts"]] == ["Ada Lovelace"]

    async def test_a_search_term_is_passed_through(self, tenant_client, tenant):
        await _create(tenant_client, tenant.kwami_id)
        r = await tenant_client.get("/contacts", params={"kwamiId": tenant.kwami_id, "q": "Ada"})
        assert r.status_code == 200

    async def test_a_blank_search_term_is_ignored(self, tenant_client, tenant):
        await _create(tenant_client, tenant.kwami_id)
        r = await tenant_client.get("/contacts", params={"kwamiId": tenant.kwami_id, "q": "   "})
        assert len(r.json()["contacts"]) == 1

    @pytest.mark.parametrize("limit", [0, 201, -1])
    async def test_an_out_of_range_limit_is_rejected(self, tenant_client, tenant, limit):
        r = await tenant_client.get(
            "/contacts", params={"kwamiId": tenant.kwami_id, "limit": limit}
        )
        assert r.status_code == 422

    async def test_the_limit_bounds_are_inclusive(self, tenant_client, tenant):
        for limit in (1, 200):
            r = await tenant_client.get(
                "/contacts", params={"kwamiId": tenant.kwami_id, "limit": limit}
            )
            assert r.status_code == 200

    async def test_another_tenants_kwami_is_refused(self, other_tenant_client_soft, tenant):
        """Access is denied -- but as a 500, which is a defect. See LEGACY_OWNERSHIP_STATUS."""
        r = await other_tenant_client_soft.get("/contacts", params={"kwamiId": tenant.kwami_id})
        assert r.status_code == LEGACY_OWNERSHIP_STATUS
        assert "contacts" not in r.text, "no data leaks, whatever the status"

    async def test_it_requires_auth(self, client, tenant):
        assert (
            await client.get("/contacts", params={"kwamiId": tenant.kwami_id})
        ).status_code == 401

    async def test_the_kwami_id_is_required(self, tenant_client):
        assert (await tenant_client.get("/contacts")).status_code == 422


class TestCreate:
    async def test_it_stores_a_normalised_number(self, tenant_client, tenant):
        contact = await _create(tenant_client, tenant.kwami_id, phoneNumber="(415) 555-2671")
        assert contact["phone_number"] == "+14155552671"

    async def test_the_display_name_is_stripped(self, tenant_client, tenant):
        contact = await _create(tenant_client, tenant.kwami_id, displayName="  Ada  ")
        assert contact["display_name"] == "Ada"

    async def test_an_unparseable_number_is_a_400(self, tenant_client, tenant):
        r = await tenant_client.post(
            "/contacts", json=_body(tenant.kwami_id, phoneNumber="not-a-number")
        )
        assert r.status_code == 400

    async def test_the_value_error_arm_is_unreachable_dead_code(
        self, tenant_client, tenant, monkeypatch
    ):
        """`except ValueError` around `normalize_phone_number` can never fire.

        `normalize_phone_number` raises `InvalidPhoneNumberError`, which is a
        `DomainError`, not a `ValueError` -- `channels.py` says so in its own
        docstring, because `phonenumbers.NumberParseException` is not a
        `ValueError` either and that was the original bug. The 400 a bad number
        produces comes from the domain-error handler, not from this arm. Forced
        here so the branch is exercised; the arm should be deleted.
        """
        from src.api.routes import contacts as contacts_route

        def raise_plain_value_error(value, region):
            raise ValueError("a plain ValueError, which production never raises")

        monkeypatch.setattr(contacts_route, "normalize_phone_number", raise_plain_value_error)
        r = await tenant_client.post("/contacts", json=_body(tenant.kwami_id))
        assert r.status_code == 400
        assert "a plain ValueError" in r.json()["detail"]

    async def test_a_parseable_but_invalid_number_is_a_400(self, tenant_client, tenant):
        r = await tenant_client.post("/contacts", json=_body(tenant.kwami_id, phoneNumber="+1555"))
        assert r.status_code == 400

    async def test_the_optional_handles_are_stored(self, tenant_client, tenant):
        contact = await _create(
            tenant_client,
            tenant.kwami_id,
            email="  ADA@Example.COM  ",
            instagram="  @ada  ",
            tiktok="  @ada_tt  ",
            whatsappAddress="+14155552671",
            metadata={"note": "met at a conference"},
        )
        assert contact["email"] == "ada@example.com", "emails are lowercased and stripped"
        assert contact["instagram"] == "@ada"
        assert contact["tiktok"] == "@ada_tt"
        assert contact["whatsapp_address"] == "+14155552671"
        assert contact["metadata"] == {"note": "met at a conference"}

    @pytest.mark.parametrize("blank", ["", "   "])
    async def test_blank_optional_handles_become_null(self, tenant_client, tenant, blank):
        """A whitespace-only handle is absence, not a value."""
        contact = await _create(
            tenant_client, tenant.kwami_id, email=blank, instagram=blank, tiktok=blank
        )
        assert contact["email"] is None
        assert contact["instagram"] is None
        assert contact["tiktok"] is None

    async def test_an_absent_whatsapp_address_is_null(self, tenant_client, tenant):
        assert (await _create(tenant_client, tenant.kwami_id))["whatsapp_address"] is None

    async def test_an_invalid_whatsapp_address_is_rejected(self, tenant_client, tenant):
        r = await tenant_client.post(
            "/contacts", json=_body(tenant.kwami_id, whatsappAddress="nonsense")
        )
        assert r.status_code == 400

    async def test_absent_metadata_defaults_to_empty(self, tenant_client, tenant):
        assert (await _create(tenant_client, tenant.kwami_id))["metadata"] == {}

    async def test_an_empty_display_name_is_rejected(self, tenant_client, tenant):
        r = await tenant_client.post("/contacts", json=_body(tenant.kwami_id, displayName=""))
        assert r.status_code == 422

    async def test_an_overlong_display_name_is_rejected(self, tenant_client, tenant):
        r = await tenant_client.post(
            "/contacts", json=_body(tenant.kwami_id, displayName="x" * 121)
        )
        assert r.status_code == 422

    async def test_another_tenants_kwami_is_refused(self, other_tenant_client_soft, tenant):
        r = await other_tenant_client_soft.post("/contacts", json=_body(tenant.kwami_id))
        assert r.status_code == LEGACY_OWNERSHIP_STATUS

    async def test_it_requires_auth(self, client, tenant):
        assert (await client.post("/contacts", json=_body(tenant.kwami_id))).status_code == 401


class TestUpdate:
    async def test_it_updates_every_field(self, tenant_client, tenant):
        contact = await _create(tenant_client, tenant.kwami_id)
        r = await tenant_client.patch(
            f"/contacts/{contact['id']}",
            json=_body(
                tenant.kwami_id,
                displayName="Ada L",
                phoneNumber="(415) 555-2672",
                email="NEW@Example.com",
                instagram="@new",
                tiktok="@new_tt",
                metadata={"k": "v"},
            ),
        )
        assert r.status_code == 200
        updated = r.json()["contact"]
        assert updated["display_name"] == "Ada L"
        assert updated["phone_number"] == "+14155552672"
        assert updated["email"] == "new@example.com"
        assert updated["metadata"] == {"k": "v"}

    async def test_naming_another_of_your_own_kwamis_is_a_400(
        self, tenant_client, tenant, fake_supabase
    ):
        """The contact is scoped by user, so this check is the only thing stopping
        a caller re-homing their own contact onto a different kwami of theirs."""
        contact = await _create(tenant_client, tenant.kwami_id)
        second = fake_supabase.db.seed(
            "user_kwamis", {"user_id": tenant.user_id, "name": "Second", "config": {}}
        )[0]
        r = await tenant_client.patch(f"/contacts/{contact['id']}", json=_body(str(second["id"])))
        assert r.status_code == 400
        assert "does not belong" in r.json()["detail"]

    async def test_blank_optionals_are_cleared(self, tenant_client, tenant):
        contact = await _create(tenant_client, tenant.kwami_id, email="a@b.c", instagram="@x")
        r = await tenant_client.patch(
            f"/contacts/{contact['id']}",
            json=_body(tenant.kwami_id, email="  ", instagram="", tiktok=""),
        )
        updated = r.json()["contact"]
        assert updated["email"] is None
        assert updated["instagram"] is None
        assert updated["tiktok"] is None

    async def test_an_absent_whatsapp_address_is_cleared(self, tenant_client, tenant):
        contact = await _create(tenant_client, tenant.kwami_id, whatsappAddress="+14155552671")
        r = await tenant_client.patch(f"/contacts/{contact['id']}", json=_body(tenant.kwami_id))
        assert r.json()["contact"]["whatsapp_address"] is None

    async def test_an_unknown_contact_is_refused(self, tenant_client_soft, tenant):
        r = await tenant_client_soft.patch(
            "/contacts/00000000-0000-0000-0000-000000000000", json=_body(tenant.kwami_id)
        )
        assert r.status_code == LEGACY_OWNERSHIP_STATUS

    async def test_another_tenants_contact_is_refused(
        self, tenant_client, other_tenant_client_soft, tenant, other_tenant
    ):
        contact = await _create(tenant_client, tenant.kwami_id)
        r = await other_tenant_client_soft.patch(
            f"/contacts/{contact['id']}", json=_body(other_tenant.kwami_id)
        )
        assert r.status_code == LEGACY_OWNERSHIP_STATUS
        assert "Ada" not in r.text

    async def test_it_requires_auth(self, client, tenant):
        r = await client.patch("/contacts/abc", json=_body(tenant.kwami_id))
        assert r.status_code == 401


class TestDelete:
    async def test_it_deletes(self, tenant_client, tenant):
        contact = await _create(tenant_client, tenant.kwami_id)
        r = await tenant_client.delete(
            f"/contacts/{contact['id']}", params={"kwamiId": tenant.kwami_id}
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        listed = await tenant_client.get("/contacts", params={"kwamiId": tenant.kwami_id})
        assert listed.json()["contacts"] == []

    async def test_naming_a_kwami_you_do_not_own_is_refused(
        self, tenant_client, tenant_client_soft, tenant, other_tenant
    ):
        contact = await _create(tenant_client, tenant.kwami_id)
        r = await tenant_client_soft.delete(
            f"/contacts/{contact['id']}", params={"kwamiId": other_tenant.kwami_id}
        )
        assert r.status_code == LEGACY_OWNERSHIP_STATUS

    async def test_naming_another_of_your_own_kwamis_is_a_400(
        self, tenant_client, tenant, tenant_factory, fake_supabase
    ):
        """The ownership check passes; the contact-belongs-to-kwami check is what rejects."""
        contact = await _create(tenant_client, tenant.kwami_id)
        second = fake_supabase.db.seed(
            "user_kwamis",
            {"user_id": tenant.user_id, "name": "Second", "config": {}},
        )[0]
        r = await tenant_client.delete(
            f"/contacts/{contact['id']}", params={"kwamiId": str(second["id"])}
        )
        assert r.status_code == 400
        assert "does not belong" in r.json()["detail"]

    async def test_an_unknown_contact_is_refused(self, tenant_client_soft, tenant):
        r = await tenant_client_soft.delete(
            "/contacts/00000000-0000-0000-0000-000000000000",
            params={"kwamiId": tenant.kwami_id},
        )
        assert r.status_code == LEGACY_OWNERSHIP_STATUS

    async def test_another_tenants_kwami_is_refused(self, other_tenant_client_soft, tenant):
        r = await other_tenant_client_soft.delete(
            "/contacts/abc", params={"kwamiId": tenant.kwami_id}
        )
        assert r.status_code == LEGACY_OWNERSHIP_STATUS

    async def test_the_kwami_id_is_required(self, tenant_client):
        assert (await tenant_client.delete("/contacts/abc")).status_code == 422

    async def test_it_requires_auth(self, client, tenant):
        r = await client.delete("/contacts/abc", params={"kwamiId": tenant.kwami_id})
        assert r.status_code == 401
