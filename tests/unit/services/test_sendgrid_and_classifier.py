"""`src.services.sendgrid_service` and `src.services.email_classifier`.

The SendGrid client is the one place outbound mail leaves the service, so its
payload shape and its failure mode (return None, never raise) are the contract.
The classifier is pure rules, applied in a fixed order — domain, then subject,
then body, then a personal-looking fallback — and the order is what decides a
message that matches two rules.
"""

from __future__ import annotations

import hashlib
import hmac

import httpx
import pytest
import respx

from src.services import sendgrid_service
from src.services.email_classifier import (
    BILLS,
    EVENTS,
    NEWSLETTERS,
    NOTIFICATIONS,
    PERSONAL,
    SHOPPING,
    TRAVEL,
    UNCATEGORIZED,
    WORK,
    ClassificationResult,
    _extract_card_data,
    _extract_domain,
    classify,
)
from src.services.sendgrid_service import (
    SENDGRID_SEND_URL,
    send_email,
    verify_inbound_webhook,
)


@pytest.fixture
def sendgrid_key(monkeypatch):
    monkeypatch.setattr(sendgrid_service.settings, "sendgrid_api_key", "SG.test", raising=False)


def _sent_payload(route) -> dict:
    import json

    return json.loads(route.calls.last.request.content)


class TestSendEmail:
    @pytest.mark.anyio
    async def test_an_unconfigured_key_is_a_runtime_error(self, monkeypatch):
        monkeypatch.setattr(sendgrid_service.settings, "sendgrid_api_key", None, raising=False)
        with pytest.raises(RuntimeError, match="SENDGRID_API_KEY is not configured"):
            await send_email(from_address="a@b.c", to_addresses=["d@e.f"], subject="s")

    @pytest.mark.anyio
    @respx.mock
    async def test_a_plain_text_email(self, sendgrid_key):
        route = respx.post(SENDGRID_SEND_URL).mock(
            return_value=httpx.Response(202, headers={"X-Message-Id": "msg-1"})
        )
        message_id = await send_email(
            from_address="kwami@kwami.io",
            to_addresses=["a@example.com"],
            subject="Hello",
            body_text="Body",
        )
        assert message_id == "msg-1"
        payload = _sent_payload(route)
        assert payload["from"] == {"email": "kwami@kwami.io"}
        assert payload["subject"] == "Hello"
        assert payload["personalizations"][0]["to"] == [{"email": "a@example.com"}]
        assert payload["content"] == [{"type": "text/plain", "value": "Body"}]

    @pytest.mark.anyio
    @respx.mock
    async def test_the_api_key_is_sent_as_a_bearer_token(self, sendgrid_key):
        route = respx.post(SENDGRID_SEND_URL).mock(return_value=httpx.Response(202))
        await send_email(from_address="a@b.c", to_addresses=["d@e.f"], subject="s")
        assert route.calls.last.request.headers["authorization"] == "Bearer SG.test"

    @pytest.mark.anyio
    @respx.mock
    async def test_multiple_recipients_and_cc(self, sendgrid_key):
        route = respx.post(SENDGRID_SEND_URL).mock(return_value=httpx.Response(202))
        await send_email(
            from_address="a@b.c",
            to_addresses=["one@x.com", "two@x.com"],
            subject="s",
            cc_addresses=["cc@x.com"],
        )
        p = _sent_payload(route)["personalizations"][0]
        assert p["to"] == [{"email": "one@x.com"}, {"email": "two@x.com"}]
        assert p["cc"] == [{"email": "cc@x.com"}]

    @pytest.mark.anyio
    @respx.mock
    async def test_no_cc_key_when_there_is_no_cc(self, sendgrid_key):
        route = respx.post(SENDGRID_SEND_URL).mock(return_value=httpx.Response(202))
        await send_email(from_address="a@b.c", to_addresses=["d@e.f"], subject="s")
        assert "cc" not in _sent_payload(route)["personalizations"][0]

    @pytest.mark.anyio
    @respx.mock
    async def test_both_bodies_are_sent_in_order(self, sendgrid_key):
        route = respx.post(SENDGRID_SEND_URL).mock(return_value=httpx.Response(202))
        await send_email(
            from_address="a@b.c",
            to_addresses=["d@e.f"],
            subject="s",
            body_text="text",
            body_html="<p>html</p>",
        )
        assert _sent_payload(route)["content"] == [
            {"type": "text/plain", "value": "text"},
            {"type": "text/html", "value": "<p>html</p>"},
        ]

    @pytest.mark.anyio
    @respx.mock
    async def test_html_only(self, sendgrid_key):
        route = respx.post(SENDGRID_SEND_URL).mock(return_value=httpx.Response(202))
        await send_email(
            from_address="a@b.c", to_addresses=["d@e.f"], subject="s", body_html="<p>x</p>"
        )
        assert _sent_payload(route)["content"] == [{"type": "text/html", "value": "<p>x</p>"}]

    @pytest.mark.anyio
    @respx.mock
    async def test_an_empty_body_still_sends_one_content_part(self, sendgrid_key):
        """SendGrid rejects a mail with no content at all."""
        route = respx.post(SENDGRID_SEND_URL).mock(return_value=httpx.Response(202))
        await send_email(from_address="a@b.c", to_addresses=["d@e.f"], subject="s")
        assert _sent_payload(route)["content"] == [{"type": "text/plain", "value": ""}]

    @pytest.mark.anyio
    @respx.mock
    async def test_a_reply_to_is_carried(self, sendgrid_key):
        route = respx.post(SENDGRID_SEND_URL).mock(return_value=httpx.Response(202))
        await send_email(
            from_address="a@b.c", to_addresses=["d@e.f"], subject="s", reply_to="r@x.com"
        )
        assert _sent_payload(route)["reply_to"] == {"email": "r@x.com"}

    @pytest.mark.anyio
    @respx.mock
    async def test_no_reply_to_key_when_absent(self, sendgrid_key):
        route = respx.post(SENDGRID_SEND_URL).mock(return_value=httpx.Response(202))
        await send_email(from_address="a@b.c", to_addresses=["d@e.f"], subject="s")
        assert "reply_to" not in _sent_payload(route)

    @pytest.mark.anyio
    @respx.mock
    @pytest.mark.parametrize("status", [200, 201, 202])
    async def test_every_success_status_is_accepted(self, sendgrid_key, status):
        respx.post(SENDGRID_SEND_URL).mock(
            return_value=httpx.Response(status, headers={"X-Message-Id": "m"})
        )
        assert await send_email(from_address="a@b.c", to_addresses=["d@e.f"], subject="s") == "m"

    @pytest.mark.anyio
    @respx.mock
    @pytest.mark.parametrize("status", [400, 401, 429, 500])
    async def test_a_failure_returns_none_and_logs(self, sendgrid_key, status, caplog):
        """Returning None rather than raising -- the caller records a send failure."""
        respx.post(SENDGRID_SEND_URL).mock(
            return_value=httpx.Response(status, text="upstream detail")
        )
        with caplog.at_level("ERROR", logger="kwami-api.sendgrid"):
            assert (
                await send_email(from_address="a@b.c", to_addresses=["d@e.f"], subject="s") is None
            )
        assert "SendGrid send failed" in caplog.text

    @pytest.mark.anyio
    @respx.mock
    async def test_a_success_without_a_message_id_returns_none(self, sendgrid_key):
        respx.post(SENDGRID_SEND_URL).mock(return_value=httpx.Response(202))
        assert await send_email(from_address="a@b.c", to_addresses=["d@e.f"], subject="s") is None


class TestVerifyInboundWebhook:
    def test_no_configured_secret_skips_the_check(self, monkeypatch):
        """Development mode: the check is skipped, not failed."""
        monkeypatch.setattr(
            sendgrid_service.settings, "sendgrid_inbound_webhook_secret", None, raising=False
        )
        assert verify_inbound_webhook("t", "123", "anything") is True

    def test_a_correct_signature_is_accepted(self, monkeypatch):
        monkeypatch.setattr(
            sendgrid_service.settings, "sendgrid_inbound_webhook_secret", "shh", raising=False
        )
        expected = hmac.new(b"shh", b"123token", hashlib.sha256).hexdigest()
        assert verify_inbound_webhook("token", "123", expected) is True

    def test_a_wrong_signature_is_refused(self, monkeypatch):
        monkeypatch.setattr(
            sendgrid_service.settings, "sendgrid_inbound_webhook_secret", "shh", raising=False
        )
        assert verify_inbound_webhook("token", "123", "deadbeef") is False

    def test_the_timestamp_is_part_of_the_signed_payload(self, monkeypatch):
        """Otherwise a captured signature replays against any timestamp."""
        monkeypatch.setattr(
            sendgrid_service.settings, "sendgrid_inbound_webhook_secret", "shh", raising=False
        )
        sig = hmac.new(b"shh", b"123token", hashlib.sha256).hexdigest()
        assert verify_inbound_webhook("token", "999", sig) is False


class TestExtractDomain:
    def test_a_plain_address(self):
        assert _extract_domain("a@Example.COM") == "example.com"

    def test_a_display_name_form(self):
        assert _extract_domain("Ada <ada@example.com>") == "example.com"

    def test_an_address_with_no_at_sign(self):
        assert _extract_domain("not-an-address") == ""

    def test_the_last_at_sign_wins(self):
        assert _extract_domain("weird@name@example.com") == "example.com"


class TestClassify:
    def test_a_known_travel_domain(self):
        r = classify(from_address="no-reply@booking.com", subject="Trip", body_text="")
        assert r.category == TRAVEL

    def test_a_subdomain_of_a_known_domain(self):
        r = classify(from_address="x@mail.booking.com", subject="Trip", body_text="")
        assert r.category == TRAVEL

    def test_the_domain_rule_beats_the_subject_rule(self):
        """An airline sending an invoice is still travel."""
        r = classify(from_address="billing@united.com", subject="Your invoice", body_text="")
        assert r.category == TRAVEL

    @pytest.mark.parametrize(
        ("subject", "expected"),
        [
            ("Your invoice is ready", BILLS),
            ("Payment due soon", BILLS),
            ("You're invited to our webinar", EVENTS),
            ("RSVP now", EVENTS),
            ("Your order has shipped", SHOPPING),
            ("Out for delivery", SHOPPING),
            ("Weekly newsletter", NEWSLETTERS),
            ("Sprint planning", WORK),
            ("Pull request opened", WORK),
        ],
    )
    def test_subject_patterns(self, subject, expected):
        assert (
            classify(from_address="x@unknown-domain.test", subject=subject, body_text="").category
            == expected
        )

    def test_the_subject_match_is_case_insensitive(self):
        assert (
            classify(from_address="x@unknown.test", subject="YOUR INVOICE", body_text="").category
            == BILLS
        )

    def test_the_body_is_the_third_fallback(self):
        r = classify(
            from_address="x@unknown.test",
            subject="Hi",
            body_text="Please see the attached invoice for payment due.",
        )
        assert r.category == BILLS

    def test_only_the_first_2000_body_characters_are_scanned(self):
        body = ("x" * 2100) + "invoice"
        r = classify(from_address="x@unknown.test", subject="Hi", body_text=body)
        assert r.category != BILLS

    def test_a_long_human_looking_email_is_personal(self):
        r = classify(
            from_address="friend@gmail.com",
            subject="Lunch?",
            body_text="Hey, are you free for lunch on Thursday? Let me know what works.",
        )
        assert r.category == PERSONAL
        assert r.action_card_data["summary"] == "Lunch?"

    def test_a_noreply_sender_is_never_personal(self):
        r = classify(
            from_address="noreply@unknown.test",
            subject="Hi",
            body_text="x" * 100,
        )
        assert r.category == UNCATEGORIZED

    @pytest.mark.parametrize("sender", ["no-reply@x.test", "NoReply@x.test", "unsubscribe@x.test"])
    def test_every_machine_sender_marker_blocks_personal(self, sender):
        assert (
            classify(from_address=sender, subject="Hi", body_text="x" * 100).category
            == UNCATEGORIZED
        )

    def test_a_short_body_is_not_personal(self):
        assert (
            classify(from_address="friend@gmail.com", subject="Hi", body_text="short").category
            == UNCATEGORIZED
        )

    def test_the_length_boundary_is_exclusive(self):
        assert (
            classify(from_address="f@gmail.com", subject="Hi", body_text="x" * 40).category
            == UNCATEGORIZED
        )
        assert (
            classify(from_address="f@gmail.com", subject="Hi", body_text="x" * 41).category
            == PERSONAL
        )

    def test_nothing_matches_at_all(self):
        assert classify(from_address="", subject="", body_text="").category == UNCATEGORIZED

    def test_a_known_notifications_domain(self):
        """Whichever domain the table assigns to notifications must land there."""
        from src.services.email_classifier import _DOMAIN_CATEGORY

        domains = next(d for d, cat in _DOMAIN_CATEGORY if cat == NOTIFICATIONS)
        assert (
            classify(from_address=f"x@{domains[0]}", subject="Hi", body_text="").category
            == NOTIFICATIONS
        )

    def test_the_default_result_is_uncategorized_with_no_card(self):
        assert ClassificationResult() == ClassificationResult(UNCATEGORIZED, {})


class TestExtractCardData:
    def test_bills_pull_an_amount_and_a_date(self):
        data = _extract_card_data("Invoice", "Amount due $1,234.56 by 03/15/2026", BILLS)
        assert data["amount"] == "1,234.56"
        assert data["due_date"] == "03/15/2026"

    def test_bills_with_neither(self):
        assert _extract_card_data("Invoice", "no numbers here", BILLS) == {}

    def test_an_amount_with_a_space_after_the_sign(self):
        assert _extract_card_data("Invoice", "$ 50", BILLS)["amount"] == "50"

    def test_travel_pulls_a_date_and_summarises(self):
        data = _extract_card_data("Flight to Lisbon", "Departing March 3, 2026", TRAVEL)
        assert data["travel_date"] == "March 3, 2026"
        assert data["summary"] == "Flight to Lisbon"

    def test_travel_always_has_a_summary(self):
        assert _extract_card_data("Trip", "no date", TRAVEL) == {"summary": "Trip"}

    def test_events_pull_a_date_and_a_name(self):
        data = _extract_card_data("Team offsite", "on 04/01/2026", EVENTS)
        assert data["event_date"] == "04/01/2026"
        assert data["event_name"] == "Team offsite"

    def test_shopping_pulls_a_tracking_url(self):
        data = _extract_card_data(
            "Shipped", "Follow it at https://carrier.test/track/abc123", SHOPPING
        )
        assert data["tracking_url"] == "https://carrier.test/track/abc123"

    def test_shopping_without_a_tracking_url(self):
        assert _extract_card_data("Shipped", "no link", SHOPPING) == {"summary": "Shipped"}

    def test_long_subjects_are_truncated_for_the_card(self):
        long_subject = "x" * 200
        assert len(_extract_card_data(long_subject, "", TRAVEL)["summary"]) == 120

    @pytest.mark.parametrize(
        "category", [NEWSLETTERS, WORK, PERSONAL, NOTIFICATIONS, UNCATEGORIZED]
    )
    def test_categories_with_no_card_extract_nothing(self, category):
        assert _extract_card_data("Subject", "Body $10 on 01/01/2026", category) == {}
