"""`/email` and `src.services.email_service` — the Smart Hub.

Two things carry the weight: `validate_username`, because the username becomes a
permanent public address, and `process_inbound_email`, which is reached from an
unauthenticated SendGrid webhook and must never raise on a message addressed to
nobody.

Usernames return i18n error KEYS, not sentences, so the app renders them; the
tests assert on the keys for that reason.
"""

from __future__ import annotations

import pytest

from src.api.routes import email as email_route
from src.services import email_service
from src.services.email_service import (
    USERNAME_RE,
    _single,
    activate_account,
    check_username_available,
    deactivate_account,
    fetch_inbox,
    find_account_by_address,
    get_account,
    get_message,
    get_unread_counts,
    process_inbound_email,
    store_outbound_email,
    update_message,
    validate_username,
)
from tests.helpers import async_return

pytestmark = pytest.mark.anyio


@pytest.fixture
async def account(fake_supabase, tenant):
    return await activate_account(user_id=tenant.user_id, kwami_id=tenant.kwami_id, username="ada")


# -- helpers -----------------------------------------------------------------


class TestSingle:
    def test_a_list_yields_its_first_row(self):
        assert _single(type("R", (), {"data": [{"id": 1}]})()) == {"id": 1}

    def test_an_empty_list_is_none(self):
        assert _single(type("R", (), {"data": []})()) is None

    def test_a_bare_dict_passes_through(self):
        assert _single(type("R", (), {"data": {"id": 1}})()) == {"id": 1}

    def test_no_data_attribute_is_none(self):
        assert _single(object()) is None


class TestValidateUsername:
    @pytest.mark.parametrize("name", ["ada", "ada.lovelace", "a-b_c", "a1b", "x" * 30])
    def test_valid_names(self, name):
        assert validate_username(name) is None

    def test_an_empty_name(self):
        assert validate_username("") == "email.errors.usernameRequired"

    @pytest.mark.parametrize("name", ["a", "ab"])
    def test_too_short(self, name):
        assert validate_username(name) == "email.errors.usernameTooShort"

    def test_too_long(self):
        assert validate_username("x" * 31) == "email.errors.usernameTooLong"

    @pytest.mark.parametrize(
        "name",
        [".ada", "ada.", "-ada", "ada-", "_ada", "ada_", "ad a", "ada!", "ada@x", "ádá"],
        ids=[
            "leading dot",
            "trailing dot",
            "leading dash",
            "trailing dash",
            "leading underscore",
            "trailing underscore",
            "space",
            "punctuation",
            "at sign",
            "non-ascii",
        ],
    )
    def test_invalid_shapes(self, name):
        assert validate_username(name) == "email.errors.usernameInvalid"

    def test_it_is_case_insensitive(self):
        assert validate_username("ADA") is None

    def test_the_regex_requires_alphanumeric_ends(self):
        assert USERNAME_RE.match("a.b") is not None
        assert USERNAME_RE.match(".ab") is None


class TestCheckUsernameAvailable:
    async def test_an_unused_name_is_available(self, fake_supabase):
        assert await check_username_available("nobody") is True

    async def test_a_taken_name_is_not(self, fake_supabase, account):
        assert await check_username_available("ada") is False

    async def test_the_check_is_case_insensitive(self, fake_supabase, account):
        assert await check_username_available("ADA") is False


class TestActivateAccount:
    async def test_it_provisions_an_account(self, fake_supabase, tenant):
        acct = await activate_account(
            user_id=tenant.user_id, kwami_id=tenant.kwami_id, username="Ada"
        )
        assert acct["username"] == "ada", "usernames are lower-cased"
        assert acct["is_active"] is True

    async def test_it_is_idempotent_for_the_same_kwami(self, fake_supabase, tenant, account):
        again = await activate_account(
            user_id=tenant.user_id, kwami_id=tenant.kwami_id, username="different"
        )
        assert again["id"] == account["id"], "the existing account wins"

    async def test_an_invalid_username_is_refused(self, fake_supabase, tenant):
        with pytest.raises(ValueError, match="usernameTooShort"):
            await activate_account(user_id=tenant.user_id, kwami_id=tenant.kwami_id, username="ab")

    async def test_a_taken_username_is_refused(self, fake_supabase, tenant, other_tenant, account):
        with pytest.raises(ValueError, match="usernameTaken"):
            await activate_account(
                user_id=other_tenant.user_id, kwami_id=other_tenant.kwami_id, username="ada"
            )

    async def test_an_insert_returning_nothing_is_a_runtime_error(
        self, monkeypatch, fake_supabase, tenant
    ):
        monkeypatch.setattr(email_service, "_single", lambda r: None)
        monkeypatch.setattr(email_service, "get_account", async_return(None))
        monkeypatch.setattr(email_service, "check_username_available", async_return(True))
        with pytest.raises(RuntimeError, match="Failed to create email account"):
            await activate_account(user_id=tenant.user_id, kwami_id=tenant.kwami_id, username="ada")


class TestGetAndDeactivateAccount:
    async def test_no_account_is_none(self, fake_supabase, tenant):
        assert await get_account(tenant.user_id, tenant.kwami_id) is None

    async def test_another_tenant_cannot_see_it(self, fake_supabase, tenant, other_tenant, account):
        assert await get_account(other_tenant.user_id, tenant.kwami_id) is None

    async def test_deactivating_removes_the_account_and_its_messages(
        self, fake_supabase, tenant, account
    ):
        await store_outbound_email(
            account=account, to_addresses=["x@y.z"], subject="s", body_text="b"
        )
        assert await deactivate_account(tenant.user_id, tenant.kwami_id) is True
        assert fake_supabase.db.rows("kwami_email_accounts") == []
        assert fake_supabase.db.rows("kwami_email_messages") == []

    async def test_deactivating_nothing_is_false(self, fake_supabase, tenant):
        assert await deactivate_account(tenant.user_id, tenant.kwami_id) is False

    async def test_another_tenant_cannot_deactivate_it(
        self, fake_supabase, tenant, other_tenant, account
    ):
        assert await deactivate_account(other_tenant.user_id, tenant.kwami_id) is False
        assert len(fake_supabase.db.rows("kwami_email_accounts")) == 1


class TestFindAccountByAddress:
    async def test_it_finds_by_full_address(self, fake_supabase, account):
        assert (await find_account_by_address("ada@kwami.io"))["id"] == account["id"]

    async def test_it_finds_by_bare_username(self, fake_supabase, account):
        assert (await find_account_by_address("ada"))["id"] == account["id"]

    async def test_it_is_case_insensitive(self, fake_supabase, account):
        assert (await find_account_by_address("ADA@KWAMI.IO"))["id"] == account["id"]

    async def test_an_unknown_address_is_none(self, fake_supabase):
        assert await find_account_by_address("nobody@kwami.io") is None

    async def test_an_inactive_account_is_not_found(self, fake_supabase, account):
        for row in fake_supabase.db.rows("kwami_email_accounts"):
            row["is_active"] = False
        assert await find_account_by_address("ada@kwami.io") is None


class TestProcessInboundEmail:
    async def test_it_classifies_and_stores(self, fake_supabase, account):
        row = await process_inbound_email(
            from_address="billing@unknown.test",
            to_addresses=["ada@kwami.io"],
            subject="Your invoice is ready",
            body_text="Amount due $10.00",
            body_html="",
        )
        assert row is not None
        assert row["direction"] == "inbound"
        assert row["category"] == "bills"
        assert row["action_card_data"]["amount"] == "10.00"

    async def test_it_tries_every_recipient(self, fake_supabase, account):
        row = await process_inbound_email(
            from_address="a@b.c",
            to_addresses=["nobody@kwami.io", "ada@kwami.io"],
            subject="Hi",
            body_text="",
            body_html="",
        )
        assert row is not None

    async def test_mail_for_nobody_is_dropped_not_raised(self, fake_supabase, caplog):
        """Reached from an unauthenticated webhook -- a 500 here means retries."""
        with caplog.at_level("WARNING", logger="kwami-api.email"):
            assert (
                await process_inbound_email(
                    from_address="a@b.c",
                    to_addresses=["nobody@kwami.io"],
                    subject="Hi",
                    body_text="",
                    body_html="",
                )
                is None
            )
        assert "unknown address" in caplog.text

    async def test_no_recipients_at_all_is_dropped(self, fake_supabase):
        assert (
            await process_inbound_email(
                from_address="a@b.c",
                to_addresses=[],
                subject="s",
                body_text="",
                body_html="",
            )
            is None
        )

    async def test_headers_and_cc_default_to_empty(self, fake_supabase, account):
        row = await process_inbound_email(
            from_address="a@b.c",
            to_addresses=["ada@kwami.io"],
            subject="s",
            body_text="",
            body_html="",
        )
        assert row["headers"] == {}
        assert row["cc_addresses"] == []

    async def test_an_insert_that_returns_nothing_is_none(
        self, monkeypatch, fake_supabase, account
    ):
        monkeypatch.setattr(email_service, "insert_or_existing", async_return(None))
        assert (
            await process_inbound_email(
                from_address="a@b.c",
                to_addresses=["ada@kwami.io"],
                subject="s",
                body_text="",
                body_html="",
            )
            is None
        )


class TestInboxQueries:
    def _msg(self, fake_supabase, account, **overrides):
        return fake_supabase.db.seed(
            "kwami_email_messages",
            {
                "account_id": account["id"],
                "user_id": account["user_id"],
                "kwami_id": account["kwami_id"],
                "direction": "inbound",
                "from_address": "a@b.c",
                "to_addresses": ["ada@kwami.io"],
                "subject": "s",
                "body_text": "b",
                "category": "personal",
                "is_read": False,
                "is_archived": False,
                "is_starred": False,
                "received_at": "2026-03-01T00:00:00+00:00",
                **overrides,
            },
        )[0]

    async def test_it_lists_messages(self, fake_supabase, account):
        self._msg(fake_supabase, account)
        assert len(await fetch_inbox(account["user_id"], account["kwami_id"])) == 1

    async def test_archived_messages_are_hidden_by_default(self, fake_supabase, account):
        self._msg(fake_supabase, account, is_archived=True)
        assert await fetch_inbox(account["user_id"], account["kwami_id"]) == []

    async def test_archived_messages_can_be_included(self, fake_supabase, account):
        self._msg(fake_supabase, account, is_archived=True)
        assert (
            len(await fetch_inbox(account["user_id"], account["kwami_id"], include_archived=True))
            == 1
        )

    async def test_it_filters_by_category(self, fake_supabase, account):
        self._msg(fake_supabase, account, category="bills")
        self._msg(fake_supabase, account, category="travel")
        assert (
            len(await fetch_inbox(account["user_id"], account["kwami_id"], category="bills")) == 1
        )

    async def test_the_all_category_is_not_a_filter(self, fake_supabase, account):
        self._msg(fake_supabase, account, category="bills")
        self._msg(fake_supabase, account, category="travel")
        assert len(await fetch_inbox(account["user_id"], account["kwami_id"], category="all")) == 2

    async def test_it_paginates(self, fake_supabase, account):
        for _ in range(5):
            self._msg(fake_supabase, account)
        assert len(await fetch_inbox(account["user_id"], account["kwami_id"], page_size=2)) == 2
        assert (
            len(await fetch_inbox(account["user_id"], account["kwami_id"], page=3, page_size=2))
            == 1
        )

    async def test_it_is_scoped_to_the_user(self, fake_supabase, account, other_tenant):
        self._msg(fake_supabase, account)
        assert await fetch_inbox(other_tenant.user_id, account["kwami_id"]) == []

    async def test_get_message(self, fake_supabase, account):
        msg = self._msg(fake_supabase, account)
        assert (await get_message(account["user_id"], str(msg["id"])))["id"] == msg["id"]

    async def test_get_message_is_scoped_to_the_user(self, fake_supabase, account, other_tenant):
        msg = self._msg(fake_supabase, account)
        assert await get_message(other_tenant.user_id, str(msg["id"])) is None

    async def test_get_an_unknown_message(self, fake_supabase, account):
        assert await get_message(account["user_id"], "00000000-0000-0000-0000-000000000000") is None

    async def test_unread_counts_group_by_category(self, fake_supabase, account):
        self._msg(fake_supabase, account, category="bills")
        self._msg(fake_supabase, account, category="bills")
        self._msg(fake_supabase, account, category="travel")
        assert await get_unread_counts(account["user_id"], account["kwami_id"]) == {
            "bills": 2,
            "travel": 1,
        }

    async def test_read_and_archived_messages_are_not_counted(self, fake_supabase, account):
        self._msg(fake_supabase, account, category="bills", is_read=True)
        self._msg(fake_supabase, account, category="bills", is_archived=True)
        assert await get_unread_counts(account["user_id"], account["kwami_id"]) == {}

    async def test_a_message_without_a_category_counts_as_uncategorized(
        self, monkeypatch, fake_supabase, account
    ):
        monkeypatch.setattr(
            email_service,
            "get_supabase_admin",
            lambda: _rows_client([{}]),
        )
        assert await get_unread_counts("u", "k") == {"uncategorized": 1}


class TestUpdateMessage:
    def _msg(self, fake_supabase, account):
        return fake_supabase.db.seed(
            "kwami_email_messages",
            {
                "account_id": account["id"],
                "user_id": account["user_id"],
                "kwami_id": account["kwami_id"],
                "direction": "inbound",
                "from_address": "a@b.c",
                "subject": "s",
                "body_text": "b",
                "category": "personal",
                "is_read": False,
                "is_starred": False,
                "is_archived": False,
            },
        )[0]

    async def test_it_updates_the_allowed_flags(self, fake_supabase, account):
        msg = self._msg(fake_supabase, account)
        updated = await update_message(
            account["user_id"], str(msg["id"]), is_read=True, is_starred=True, is_archived=True
        )
        assert updated["is_read"] is True
        assert updated["is_starred"] is True
        assert updated["is_archived"] is True

    async def test_unknown_fields_are_ignored(self, fake_supabase, account):
        msg = self._msg(fake_supabase, account)
        updated = await update_message(account["user_id"], str(msg["id"]), category="bills")
        assert updated["category"] == "personal", "only the three flags are writable"

    async def test_no_writable_fields_is_a_plain_read(self, fake_supabase, account):
        msg = self._msg(fake_supabase, account)
        assert (await update_message(account["user_id"], str(msg["id"])))["id"] == msg["id"]

    async def test_another_tenant_cannot_update_it(self, fake_supabase, account, other_tenant):
        msg = self._msg(fake_supabase, account)
        assert await update_message(other_tenant.user_id, str(msg["id"]), is_read=True) is None


class TestStoreOutboundEmail:
    async def test_it_stores_a_sent_message(self, fake_supabase, account):
        row = await store_outbound_email(
            account=account,
            to_addresses=["x@y.z"],
            cc_addresses=["cc@y.z"],
            subject="Hi",
            body_text="text",
            body_html="<p>x</p>",
            sendgrid_message_id="msg-1",
        )
        assert row["direction"] == "outbound"
        assert row["from_address"] == account["email_address"]
        assert row["is_read"] is True, "your own sent mail is not unread"
        assert row["category"] == "personal"
        assert row["sendgrid_message_id"] == "msg-1"

    async def test_absent_cc_becomes_an_empty_list(self, fake_supabase, account):
        row = await store_outbound_email(
            account=account, to_addresses=["x@y.z"], subject="s", body_text="b"
        )
        assert row["cc_addresses"] == []


# -- routes ------------------------------------------------------------------


class TestCheckUsernameRoute:
    async def test_an_available_name(self, tenant_client):
        r = await tenant_client.post("/email/check-username", json={"username": "brandnew"})
        assert r.status_code == 200
        assert r.json() == {"available": True, "error": None}

    async def test_an_invalid_name_returns_its_error_key(self, tenant_client):
        r = await tenant_client.post("/email/check-username", json={"username": "ab"})
        assert r.json() == {"available": False, "error": "email.errors.usernameTooShort"}

    async def test_a_taken_name(self, tenant_client, account):
        r = await tenant_client.post("/email/check-username", json={"username": "ada"})
        assert r.json()["available"] is False

    async def test_an_empty_name_is_a_422(self, tenant_client):
        assert (
            await tenant_client.post("/email/check-username", json={"username": ""})
        ).status_code == 422

    async def test_it_requires_auth(self, client):
        assert (
            await client.post("/email/check-username", json={"username": "x"})
        ).status_code == 401


class TestActivateRoute:
    async def test_it_activates(self, tenant_client, tenant):
        r = await tenant_client.post(
            "/email/activate", json={"kwami_id": tenant.kwami_id, "username": "ada"}
        )
        assert r.status_code == 200
        assert r.json()["username"] == "ada"
        assert r.json()["is_active"] is True

    async def test_a_taken_username_is_a_400(
        self, tenant_client, other_tenant_client, tenant, other_tenant, account
    ):
        r = await other_tenant_client.post(
            "/email/activate",
            json={"kwami_id": other_tenant.kwami_id, "username": "ada"},
        )
        assert r.status_code == 400
        assert r.json()["detail"] == "email.errors.usernameTaken"

    async def test_a_short_username_is_a_422(self, tenant_client, tenant):
        r = await tenant_client.post(
            "/email/activate", json={"kwami_id": tenant.kwami_id, "username": "ab"}
        )
        assert r.status_code == 422

    async def test_it_requires_auth(self, client, tenant):
        r = await client.post(
            "/email/activate", json={"kwami_id": tenant.kwami_id, "username": "ada"}
        )
        assert r.status_code == 401


class TestAccountRoutes:
    async def test_no_account_yields_null(self, tenant_client, tenant):
        r = await tenant_client.get("/email/account", params={"kwami_id": tenant.kwami_id})
        assert r.status_code == 200
        assert r.json() == {"account": None}

    async def test_an_activated_account_is_returned(self, tenant_client, tenant, account):
        r = await tenant_client.get("/email/account", params={"kwami_id": tenant.kwami_id})
        assert r.json()["account"]["username"] == "ada"

    async def test_deactivating(self, tenant_client, tenant, account):
        r = await tenant_client.delete("/email/account", params={"kwami_id": tenant.kwami_id})
        assert r.status_code == 200
        assert r.json() == {"ok": True}

    async def test_deactivating_nothing_is_a_404(self, tenant_client, tenant):
        r = await tenant_client.delete("/email/account", params={"kwami_id": tenant.kwami_id})
        assert r.status_code == 404

    async def test_the_kwami_id_is_required(self, tenant_client):
        assert (await tenant_client.get("/email/account")).status_code == 422

    async def test_they_require_auth(self, client, tenant):
        assert (
            await client.get("/email/account", params={"kwami_id": tenant.kwami_id})
        ).status_code == 401
        assert (
            await client.delete("/email/account", params={"kwami_id": tenant.kwami_id})
        ).status_code == 401


class TestInboxRoutes:
    async def test_an_empty_inbox(self, tenant_client, tenant, account):
        r = await tenant_client.get("/email/inbox", params={"kwami_id": tenant.kwami_id})
        assert r.status_code == 200
        assert r.json() == {"messages": []}

    async def test_it_accepts_a_category_and_pagination(self, tenant_client, tenant, account):
        r = await tenant_client.get(
            "/email/inbox",
            params={"kwami_id": tenant.kwami_id, "category": "bills", "page": 2, "page_size": 10},
        )
        assert r.status_code == 200

    @pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 0}, {"page_size": 101}])
    async def test_out_of_range_pagination_is_a_422(self, tenant_client, tenant, params):
        r = await tenant_client.get("/email/inbox", params={"kwami_id": tenant.kwami_id, **params})
        assert r.status_code == 422

    async def test_unread_counts(self, tenant_client, tenant, account, fake_supabase):
        fake_supabase.db.seed(
            "kwami_email_messages",
            {
                "account_id": account["id"],
                "user_id": tenant.user_id,
                "kwami_id": tenant.kwami_id,
                "direction": "inbound",
                "from_address": "a@b.c",
                "subject": "s",
                "body_text": "b",
                "category": "bills",
                "is_read": False,
                "is_archived": False,
            },
        )
        r = await tenant_client.get("/email/unread-counts", params={"kwami_id": tenant.kwami_id})
        assert r.json() == {"counts": {"bills": 1}}

    async def test_they_require_auth(self, client, tenant):
        assert (
            await client.get("/email/inbox", params={"kwami_id": tenant.kwami_id})
        ).status_code == 401
        assert (
            await client.get("/email/unread-counts", params={"kwami_id": tenant.kwami_id})
        ).status_code == 401


class TestMessageRoutes:
    def _msg(self, fake_supabase, account):
        return fake_supabase.db.seed(
            "kwami_email_messages",
            {
                "account_id": account["id"],
                "user_id": account["user_id"],
                "kwami_id": account["kwami_id"],
                "direction": "inbound",
                "from_address": "a@b.c",
                "subject": "s",
                "body_text": "b",
                "category": "personal",
                "is_read": False,
                "is_starred": False,
                "is_archived": False,
            },
        )[0]

    async def test_getting_a_message(self, tenant_client, account, fake_supabase):
        msg = self._msg(fake_supabase, account)
        r = await tenant_client.get(f"/email/messages/{msg['id']}")
        assert r.status_code == 200
        assert r.json()["message"]["id"] == str(msg["id"])

    async def test_an_unknown_message_is_a_404(self, tenant_client):
        r = await tenant_client.get("/email/messages/00000000-0000-0000-0000-000000000000")
        assert r.status_code == 404

    async def test_another_tenants_message_is_a_404(
        self, other_tenant_client, account, fake_supabase
    ):
        msg = self._msg(fake_supabase, account)
        assert (await other_tenant_client.get(f"/email/messages/{msg['id']}")).status_code == 404

    async def test_patching_the_flags(self, tenant_client, account, fake_supabase):
        msg = self._msg(fake_supabase, account)
        r = await tenant_client.patch(
            f"/email/messages/{msg['id']}",
            json={"is_read": True, "is_starred": True, "is_archived": True},
        )
        assert r.status_code == 200
        assert r.json()["message"]["is_read"] is True

    async def test_patching_one_flag_leaves_the_others(self, tenant_client, account, fake_supabase):
        msg = self._msg(fake_supabase, account)
        r = await tenant_client.patch(f"/email/messages/{msg['id']}", json={"is_starred": True})
        assert r.json()["message"]["is_read"] is False

    async def test_an_empty_patch_is_a_read(self, tenant_client, account, fake_supabase):
        msg = self._msg(fake_supabase, account)
        r = await tenant_client.patch(f"/email/messages/{msg['id']}", json={})
        assert r.status_code == 200

    async def test_patching_an_unknown_message_is_a_404(self, tenant_client):
        r = await tenant_client.patch(
            "/email/messages/00000000-0000-0000-0000-000000000000", json={"is_read": True}
        )
        assert r.status_code == 404

    async def test_they_require_auth(self, client):
        assert (await client.get("/email/messages/x")).status_code == 401
        assert (await client.patch("/email/messages/x", json={})).status_code == 401


class TestSendRoute:
    def _body(self, kwami_id, **overrides):
        return {
            "kwami_id": kwami_id,
            "to_addresses": ["someone@example.com"],
            "subject": "Hi",
            "body_text": "Hello",
            **overrides,
        }

    async def test_unconfigured_email_is_a_503(self, monkeypatch, tenant_client, tenant):
        monkeypatch.setattr(
            type(email_route.settings), "email_enabled", property(lambda self: False)
        )
        r = await tenant_client.post("/email/send", json=self._body(tenant.kwami_id))
        assert r.status_code == 503
        assert r.json()["detail"] == "Email sending is not configured"

    async def test_sending_without_an_account_is_a_400(self, monkeypatch, tenant_client, tenant):
        monkeypatch.setattr(
            type(email_route.settings), "email_enabled", property(lambda self: True)
        )
        r = await tenant_client.post("/email/send", json=self._body(tenant.kwami_id))
        assert r.status_code == 400
        assert r.json()["detail"] == "Email account not activated"

    async def test_it_sends_and_stores(self, monkeypatch, tenant_client, tenant, account):
        monkeypatch.setattr(
            type(email_route.settings), "email_enabled", property(lambda self: True)
        )
        seen = {}

        async def fake_send(**kwargs):
            seen.update(kwargs)
            return "msg-1"

        monkeypatch.setattr(email_route, "send_email", fake_send)
        r = await tenant_client.post(
            "/email/send",
            json=self._body(tenant.kwami_id, cc_addresses=["cc@example.com"], body_html="<p>x</p>"),
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["message"]["direction"] == "outbound"
        assert seen["from_address"] == account["email_address"]
        assert seen["cc_addresses"] == ["cc@example.com"]

    async def test_an_empty_cc_list_is_sent_as_none(
        self, monkeypatch, tenant_client, tenant, account
    ):
        """SendGrid rejects an empty `cc` array."""
        monkeypatch.setattr(
            type(email_route.settings), "email_enabled", property(lambda self: True)
        )
        seen = {}

        async def fake_send(**kwargs):
            seen.update(kwargs)
            return None

        monkeypatch.setattr(email_route, "send_email", fake_send)
        await tenant_client.post("/email/send", json=self._body(tenant.kwami_id))
        assert seen["cc_addresses"] is None

    async def test_a_send_failure_still_records_the_attempt(
        self, monkeypatch, tenant_client, tenant, account
    ):
        """`send_email` returns None on failure; the row is stored without an id."""
        monkeypatch.setattr(
            type(email_route.settings), "email_enabled", property(lambda self: True)
        )

        async def fake_send(**kwargs):
            return None

        monkeypatch.setattr(email_route, "send_email", fake_send)
        r = await tenant_client.post("/email/send", json=self._body(tenant.kwami_id))
        assert r.status_code == 200
        assert r.json()["message"]["sendgrid_message_id"] is None

    async def test_it_requires_auth(self, client, tenant):
        assert (
            await client.post("/email/send", json=self._body(tenant.kwami_id))
        ).status_code == 401


# -- doubles -----------------------------------------------------------------


def _first_then(*values):
    calls = {"n": 0}

    def _call(result):
        index = min(calls["n"], len(values) - 1)
        calls["n"] += 1
        return values[index]

    return _call


def _rows_client(rows):
    class _T:
        def select(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

        async def execute(self):
            return type("R", (), {"data": rows})()

    return type("C", (), {"table": staticmethod(lambda n: _T())})()
