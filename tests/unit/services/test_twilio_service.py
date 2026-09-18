"""`src.services.twilio_service` — provisioning, signing, and messaging.

The Twilio REST client is replaced wholesale: these tests are about the shapes
this module builds and the errors it forgives, not about Twilio's HTTP layer.
Two things carry real weight — `signed_url_candidates`, because a wrong URL
means signature validation can never pass behind a TLS terminator, and the
404-forgiving deletes, because a retry must not fail on an already-gone
resource.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qsl, urlparse

import pytest
from fastapi import HTTPException
from twilio.base.exceptions import TwilioException, TwilioRestException

from src.services import twilio_service
from src.services.twilio_service import (
    _append_sip_headers,
    _capabilities_dict,
    _number_search_kw,
    _twilio_page_is_404,
    attach_phone_number_to_sip_trunk,
    build_message_ack_response,
    build_voice_bridge_response,
    detach_phone_number_from_sip_trunk,
    ensure_twilio_enabled,
    extract_twilio_error,
    get_twilio_client,
    place_direct_pstn_test_call,
    purchase_phone_number,
    release_incoming_phone_number,
    search_available_numbers,
    send_sms_message,
    send_whatsapp_message,
    signed_url_candidates,
    webhook_url,
)


@pytest.fixture(autouse=True)
def _reset_client(monkeypatch):
    monkeypatch.setattr(twilio_service, "_twilio_client", None, raising=False)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(
        type(twilio_service.settings), "twilio_enabled", property(lambda self: True)
    )


@pytest.fixture
def fake_client(monkeypatch, enabled):
    """A stand-in Twilio client, installed as the module singleton."""
    client = _FakeClient()
    monkeypatch.setattr(twilio_service, "_twilio_client", client, raising=False)
    return client


class TestGetTwilioClient:
    def test_it_refuses_when_twilio_is_not_configured(self, monkeypatch):
        monkeypatch.setattr(
            type(twilio_service.settings), "twilio_enabled", property(lambda self: False)
        )
        with pytest.raises(RuntimeError, match="not configured"):
            get_twilio_client()

    def test_it_builds_and_caches_a_client(self, monkeypatch, enabled):
        built: list[tuple] = []
        monkeypatch.setattr(
            twilio_service, "Client", lambda sid, token: built.append((sid, token)) or "CLIENT"
        )
        monkeypatch.setattr(twilio_service.settings, "twilio_account_sid", "AC1", raising=False)
        monkeypatch.setattr(twilio_service.settings, "twilio_auth_token", "tok", raising=False)
        assert get_twilio_client() == "CLIENT"
        assert get_twilio_client() == "CLIENT"
        assert built == [("AC1", "tok")]


class TestEnsureTwilioEnabled:
    def test_it_passes_when_enabled(self, enabled):
        assert ensure_twilio_enabled() is None

    def test_it_503s_when_disabled(self, monkeypatch):
        monkeypatch.setattr(
            type(twilio_service.settings), "twilio_enabled", property(lambda self: False)
        )
        with pytest.raises(HTTPException) as exc:
            ensure_twilio_enabled()
        assert exc.value.status_code == 503


class TestWebhookUrl:
    def test_an_explicit_url_wins(self, monkeypatch):
        monkeypatch.setattr(
            twilio_service.settings, "app_public_url", "https://api.kwami.io", raising=False
        )
        assert webhook_url("/x", explicit="https://override.test/y") == "https://override.test/y"

    def test_it_is_built_from_the_public_url(self, monkeypatch):
        monkeypatch.setattr(
            twilio_service.settings, "app_public_url", "https://api.kwami.io/", raising=False
        )
        assert webhook_url("/webhooks/twilio/voice") == (
            "https://api.kwami.io/webhooks/twilio/voice"
        )

    def test_no_public_url_means_no_webhook_url(self, monkeypatch):
        monkeypatch.setattr(twilio_service.settings, "app_public_url", None, raising=False)
        assert webhook_url("/x") is None


class TestSignedUrlCandidates:
    def _request(self, url: str, headers: dict[str, str] | None = None):
        from starlette.datastructures import Headers
        from starlette.requests import Request

        parsed = urlparse(url)
        raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        scope = {
            "type": "http",
            "method": "POST",
            "scheme": parsed.scheme,
            "server": (parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)),
            "path": parsed.path,
            "query_string": parsed.query.encode(),
            "headers": raw_headers,
            "root_path": "",
        }
        request = Request(scope)
        assert isinstance(request.headers, Headers)
        return request

    def test_the_configured_public_url_is_tried_first(self, monkeypatch):
        """Configuration, not request data -- a spoofed Host cannot influence it."""
        monkeypatch.setattr(
            twilio_service.settings, "app_public_url", "https://api.kwami.io", raising=False
        )
        candidates = signed_url_candidates(self._request("http://internal/webhooks/twilio/voice"))
        assert candidates[0] == "https://api.kwami.io/webhooks/twilio/voice"

    def test_the_query_string_is_preserved(self, monkeypatch):
        monkeypatch.setattr(
            twilio_service.settings, "app_public_url", "https://api.kwami.io", raising=False
        )
        candidates = signed_url_candidates(self._request("http://internal/x?a=1&b=2"))
        assert candidates[0] == "https://api.kwami.io/x?a=1&b=2"

    def test_the_forwarded_proto_is_offered(self, monkeypatch):
        monkeypatch.setattr(twilio_service.settings, "app_public_url", None, raising=False)
        candidates = signed_url_candidates(
            self._request("http://internal/x", {"x-forwarded-proto": "https"})
        )
        assert any(c.startswith("https://") for c in candidates)

    def test_only_the_first_forwarded_proto_is_used(self, monkeypatch):
        """A proxy chain sends `https,http`; the client-facing one is first."""
        monkeypatch.setattr(twilio_service.settings, "app_public_url", None, raising=False)
        candidates = signed_url_candidates(
            self._request("http://internal/x", {"x-forwarded-proto": " https , http "})
        )
        assert any(c.startswith("https://") for c in candidates)

    def test_the_raw_request_url_is_always_last(self, monkeypatch):
        monkeypatch.setattr(twilio_service.settings, "app_public_url", None, raising=False)
        candidates = signed_url_candidates(self._request("http://internal/x"))
        assert candidates[-1].startswith("http://internal/x")

    def test_without_any_configuration_there_is_exactly_one_candidate(self, monkeypatch):
        monkeypatch.setattr(twilio_service.settings, "app_public_url", None, raising=False)
        assert len(signed_url_candidates(self._request("http://internal/x"))) == 1


class TestCapabilitiesDict:
    def test_a_dict_of_capabilities(self):
        item = type("I", (), {"capabilities": {"voice": True, "SMS": True, "MMS": False}})()
        assert _capabilities_dict(item) == {"voice": True, "sms": True, "mms": False}

    def test_a_json_string_of_capabilities(self):
        item = type("I", (), {"capabilities": json.dumps({"voice": True, "sms": True})})()
        assert _capabilities_dict(item)["sms"] is True

    def test_both_key_spellings_are_accepted(self):
        lower = type("I", (), {"capabilities": {"sms": True, "mms": True}})()
        upper = type("I", (), {"capabilities": {"SMS": True, "MMS": True}})()
        assert _capabilities_dict(lower) == _capabilities_dict(upper)

    def test_malformed_json_yields_all_false(self):
        item = type("I", (), {"capabilities": "{not json"})()
        assert _capabilities_dict(item) == {"voice": False, "sms": False, "mms": False}

    def test_a_json_scalar_yields_all_false(self):
        item = type("I", (), {"capabilities": "42"})()
        assert _capabilities_dict(item) == {"voice": False, "sms": False, "mms": False}

    def test_a_missing_attribute_yields_all_false(self):
        assert _capabilities_dict(object()) == {"voice": False, "sms": False, "mms": False}


class TestTwilioPageIs404:
    def test_a_response_object_carrying_404(self):
        resp = type("R", (), {"status_code": 404})()
        assert _twilio_page_is_404(TwilioException("boom", resp)) is True

    def test_a_response_object_carrying_another_status(self):
        resp = type("R", (), {"status_code": 500})()
        assert _twilio_page_is_404(TwilioException("boom", resp)) is False

    def test_the_message_text_fallback(self):
        assert _twilio_page_is_404(TwilioException("HTTP 404 Not Found")) is True

    def test_the_json_body_fallback(self):
        assert _twilio_page_is_404(TwilioException('{"status":404}')) is True

    def test_an_unrelated_error(self):
        assert _twilio_page_is_404(TwilioException("HTTP 500")) is False


class TestNumberSearchKw:
    def test_the_limit_is_always_present(self):
        assert _number_search_kw(
            country_code="US", kind="local", area_code=None, contains=None, limit=5
        ) == {"limit": 5}

    def test_contains_is_passed_through(self):
        kw = _number_search_kw(
            country_code="US", kind="local", area_code=None, contains="555", limit=5
        )
        assert kw["contains"] == "555"

    @pytest.mark.parametrize("cc", ["US", "us", "CA"])
    def test_an_area_code_applies_in_north_america(self, cc):
        kw = _number_search_kw(
            country_code=cc, kind="local", area_code="415", contains=None, limit=5
        )
        assert kw["area_code"] == "415"

    def test_an_area_code_is_dropped_elsewhere(self):
        """Twilio rejects `area_code` outside US/CA."""
        kw = _number_search_kw(
            country_code="ES", kind="local", area_code="415", contains=None, limit=5
        )
        assert "area_code" not in kw

    def test_an_area_code_is_dropped_for_toll_free(self):
        kw = _number_search_kw(
            country_code="US", kind="toll_free", area_code="415", contains=None, limit=5
        )
        assert "area_code" not in kw

    def test_mobile_accepts_an_area_code_in_north_america(self):
        kw = _number_search_kw(
            country_code="US", kind="mobile", area_code="415", contains=None, limit=5
        )
        assert kw["area_code"] == "415"


class TestSearchAvailableNumbers:
    def test_the_first_responding_subresource_wins(self, fake_client):
        fake_client.available["US"].local.items = [_number("+14155552671")]
        results = search_available_numbers(
            country_code="us", area_code=None, contains=None, limit=5
        )
        assert [r["phoneNumber"] for r in results] == ["+14155552671"]
        assert results[0]["capabilities"] == {"voice": True, "sms": True, "mms": False}

    def test_a_404_falls_through_to_the_next_kind(self, fake_client, caplog):
        """Many countries have no Local subresource."""
        fake_client.available["ES"].local.error = TwilioException("HTTP 404 Not Found")
        fake_client.available["ES"].mobile.items = [_number("+34600000000")]
        results = search_available_numbers(
            country_code="ES", area_code=None, contains=None, limit=5
        )
        assert [r["phoneNumber"] for r in results] == ["+34600000000"]

    def test_every_kind_404ing_yields_an_empty_list(self, fake_client, caplog):
        for kind in ("local", "mobile", "toll_free"):
            getattr(fake_client.available["ZZ"], kind).error = TwilioException("HTTP 404")
        with caplog.at_level("WARNING", logger="kwami-api.twilio"):
            assert (
                search_available_numbers(country_code="ZZ", area_code=None, contains=None, limit=5)
                == []
            )
        assert "No Twilio phone number subresources responded" in caplog.text

    def test_an_absent_subresource_is_skipped(self, fake_client):
        fake_client.available["ZZ"].local = None
        fake_client.available["ZZ"].mobile.items = [_number("+1")]
        assert (
            len(search_available_numbers(country_code="ZZ", area_code=None, contains=None, limit=5))
            == 1
        )

    def test_a_country_with_no_subresources_at_all_is_empty_and_silent(self, fake_client, caplog):
        """No 404 was seen, so there is nothing to warn about -- just no inventory."""
        country = fake_client.available["ZZ"]
        country.local = country.mobile = country.toll_free = None
        with caplog.at_level("WARNING", logger="kwami-api.twilio"):
            assert (
                search_available_numbers(country_code="ZZ", area_code=None, contains=None, limit=5)
                == []
            )
        assert "No Twilio phone number subresources responded" not in caplog.text

    def test_a_non_404_error_propagates(self, fake_client):
        """A credentials or rate-limit failure must not read as "no inventory"."""
        fake_client.available["US"].local.error = TwilioException("HTTP 401 Unauthorized")
        with pytest.raises(TwilioException):
            search_available_numbers(country_code="US", area_code=None, contains=None, limit=5)

    def test_an_empty_inventory_is_not_a_fallthrough(self, fake_client):
        """An empty list is an answer; it must not try the next kind."""
        fake_client.available["US"].local.items = []
        fake_client.available["US"].mobile.items = [_number("+1")]
        assert (
            search_available_numbers(country_code="US", area_code=None, contains=None, limit=5)
            == []
        )


class TestPurchaseAndRelease:
    def test_purchasing_wires_up_every_webhook(self, monkeypatch, fake_client):
        monkeypatch.setattr(
            twilio_service.settings, "app_public_url", "https://api.kwami.io", raising=False
        )
        monkeypatch.setattr(
            twilio_service.settings, "twilio_voice_status_callback_url", None, raising=False
        )
        result = purchase_phone_number(phone_number="+14155552671", friendly_name="Kwami")
        assert result["sid"] == "PN1"
        kwargs = fake_client.incoming_phone_numbers.created
        assert kwargs["voice_url"] == "https://api.kwami.io/webhooks/twilio/voice"
        assert kwargs["status_callback"] == "https://api.kwami.io/webhooks/twilio/voice/status"
        assert kwargs["sms_url"] == "https://api.kwami.io/webhooks/twilio/whatsapp"

    def test_an_explicit_status_callback_overrides(self, monkeypatch, fake_client):
        monkeypatch.setattr(
            twilio_service.settings, "app_public_url", "https://api.kwami.io", raising=False
        )
        monkeypatch.setattr(
            twilio_service.settings,
            "twilio_voice_status_callback_url",
            "https://override.test/s",
            raising=False,
        )
        purchase_phone_number(phone_number="+1", friendly_name="K")
        assert fake_client.incoming_phone_numbers.created["status_callback"] == (
            "https://override.test/s"
        )

    def test_releasing_a_number(self, fake_client):
        release_incoming_phone_number("PN1")
        assert fake_client.incoming_phone_numbers.deleted == ["PN1"]

    def test_releasing_an_already_gone_number_is_forgiven(self, fake_client, caplog):
        """A retry must not fail on a number that is already released."""
        fake_client.incoming_phone_numbers.delete_error = _rest_exception(404)
        with caplog.at_level("INFO", logger="kwami-api.twilio"):
            release_incoming_phone_number("PN1")
        assert "already released" in caplog.text

    def test_another_release_failure_propagates(self, fake_client):
        fake_client.incoming_phone_numbers.delete_error = _rest_exception(500)
        with pytest.raises(TwilioRestException):
            release_incoming_phone_number("PN1")

    def test_releasing_without_a_sid_is_refused(self):
        with pytest.raises(ValueError, match="Missing Twilio incoming phone SID"):
            release_incoming_phone_number("")


class TestSipTrunk:
    def test_attaching_returns_the_resource_sid(self, monkeypatch, fake_client):
        monkeypatch.setattr(twilio_service.settings, "twilio_sip_trunk_sid", "TK1", raising=False)
        assert attach_phone_number_to_sip_trunk("PN1") == "TP1"

    def test_attaching_is_a_no_op_without_a_trunk(self, monkeypatch):
        monkeypatch.setattr(twilio_service.settings, "twilio_sip_trunk_sid", None, raising=False)
        assert attach_phone_number_to_sip_trunk("PN1") is None

    def test_detaching(self, monkeypatch, fake_client):
        monkeypatch.setattr(twilio_service.settings, "twilio_sip_trunk_sid", "TK1", raising=False)
        detach_phone_number_from_sip_trunk("TP1")
        assert fake_client.trunking.deleted == ["TP1"]

    @pytest.mark.parametrize(
        ("trunk_sid", "resource_sid"), [(None, "TP1"), ("TK1", ""), (None, "")]
    )
    def test_detaching_is_a_no_op_when_either_id_is_missing(
        self, monkeypatch, trunk_sid, resource_sid
    ):
        monkeypatch.setattr(
            twilio_service.settings, "twilio_sip_trunk_sid", trunk_sid, raising=False
        )
        assert detach_phone_number_from_sip_trunk(resource_sid) is None

    def test_detaching_an_already_gone_association_is_forgiven(
        self, monkeypatch, fake_client, caplog
    ):
        monkeypatch.setattr(twilio_service.settings, "twilio_sip_trunk_sid", "TK1", raising=False)
        fake_client.trunking.delete_error = _rest_exception(404)
        with caplog.at_level("DEBUG", logger="kwami-api.twilio"):
            detach_phone_number_from_sip_trunk("TP1")
        assert "already gone" in caplog.text

    def test_another_detach_failure_propagates(self, monkeypatch, fake_client):
        monkeypatch.setattr(twilio_service.settings, "twilio_sip_trunk_sid", "TK1", raising=False)
        fake_client.trunking.delete_error = _rest_exception(500)
        with pytest.raises(TwilioRestException):
            detach_phone_number_from_sip_trunk("TP1")


class TestMessagingAndCalls:
    def test_a_direct_pstn_test_call(self, fake_client):
        result = place_direct_pstn_test_call(to_e164="+14155552671", from_e164="+14155552672")
        assert result == {"sid": "CA1", "status": "queued"}
        twiml = fake_client.calls.created["twiml"]
        assert "<Say" in twiml and "<Hangup" in twiml

    def test_sending_whatsapp(self, monkeypatch, fake_client):
        monkeypatch.setattr(
            twilio_service.settings, "app_public_url", "https://api.kwami.io", raising=False
        )
        monkeypatch.setattr(
            twilio_service.settings, "twilio_messaging_status_callback_url", None, raising=False
        )
        result = send_whatsapp_message(
            from_address="whatsapp:+1", to_address="whatsapp:+2", body="hi"
        )
        assert result["sid"] == "SM1"
        assert fake_client.messages.created["status_callback"] == (
            "https://api.kwami.io/webhooks/twilio/whatsapp/status"
        )

    def test_sending_sms(self, monkeypatch, fake_client):
        monkeypatch.setattr(
            twilio_service.settings, "app_public_url", "https://api.kwami.io", raising=False
        )
        monkeypatch.setattr(
            twilio_service.settings, "twilio_messaging_status_callback_url", None, raising=False
        )
        result = send_sms_message(from_number="+1", to_number="+2", body="hi")
        assert result == {"sid": "SM1", "status": "queued", "from": "+1", "to": "+2"}

    def test_an_explicit_messaging_callback_overrides(self, monkeypatch, fake_client):
        monkeypatch.setattr(
            twilio_service.settings,
            "twilio_messaging_status_callback_url",
            "https://override.test/m",
            raising=False,
        )
        send_sms_message(from_number="+1", to_number="+2", body="hi")
        assert fake_client.messages.created["status_callback"] == "https://override.test/m"


class TestTwiMLBuilders:
    def test_the_voice_bridge_dials_sip(self):
        xml = build_voice_bridge_response(
            livekit_uri="sip:kwami@sip.livekit.cloud", headers={"X-Kwami-Id": "k1"}
        )
        assert "<Dial" in xml
        assert "answerOnBridge" in xml
        assert "sip:kwami@sip.livekit.cloud" in xml

    def test_the_message_ack(self):
        xml = build_message_ack_response("Got it")
        assert "<Message>Got it</Message>" in xml

    def test_headers_become_uri_query_parameters(self):
        uri = _append_sip_headers("sip:kwami@host", {"X-Kwami-Id": "k1", "X-User": "u1"})
        query = dict(parse_qsl(urlparse(uri).query))
        assert query == {"X-Kwami-Id": "k1", "X-User": "u1"}

    def test_existing_query_parameters_are_preserved(self):
        uri = _append_sip_headers("sip:kwami@host?transport=tcp", {"X-Kwami-Id": "k1"})
        query = dict(parse_qsl(urlparse(uri).query))
        assert query == {"transport": "tcp", "X-Kwami-Id": "k1"}

    def test_a_header_overrides_a_matching_existing_parameter(self):
        uri = _append_sip_headers("sip:kwami@host?X-Kwami-Id=old", {"X-Kwami-Id": "new"})
        assert dict(parse_qsl(urlparse(uri).query)) == {"X-Kwami-Id": "new"}

    def test_no_headers_leaves_the_uri_alone(self):
        assert _append_sip_headers("sip:kwami@host", {}) == "sip:kwami@host"


class TestExtractTwilioError:
    def test_a_twilio_rest_exception_yields_its_code_and_message(self):
        exc = _rest_exception(400, code=21211, msg="Invalid 'To' number")
        assert extract_twilio_error(exc) == ("21211", "Invalid 'To' number")

    def test_a_rest_exception_without_a_code(self):
        exc = _rest_exception(400, code=None, msg="something")
        assert extract_twilio_error(exc) == (None, "something")

    def test_any_other_exception_stringifies(self):
        assert extract_twilio_error(ValueError("boom")) == (None, "boom")


# -- doubles -----------------------------------------------------------------


def _rest_exception(status: int, code: int | None = None, msg: str = "boom"):
    exc = TwilioRestException(status=status, uri="/x", msg=msg, code=code)
    return exc


def _number(phone: str):
    return type(
        "N",
        (),
        {
            "phone_number": phone,
            "friendly_name": phone,
            "region": "CA",
            "locality": "San Francisco",
            "postal_code": "94103",
            "capabilities": {"voice": True, "SMS": True, "MMS": False},
        },
    )()


class _Sub:
    def __init__(self):
        self.items: list = []
        self.error: Exception | None = None

    def list(self, **kwargs):
        if self.error:
            raise self.error
        return self.items


class _Country:
    def __init__(self):
        self.local = _Sub()
        self.mobile = _Sub()
        self.toll_free = _Sub()


class _IncomingPhoneNumbers:
    def __init__(self):
        self.created: dict = {}
        self.deleted: list[str] = []
        self.delete_error: Exception | None = None

    def create(self, **kwargs):
        self.created = kwargs
        return type(
            "P",
            (),
            {
                "sid": "PN1",
                "phone_number": kwargs.get("phone_number"),
                "friendly_name": kwargs.get("friendly_name"),
                "status": "in-use",
            },
        )()

    def __call__(self, sid):
        outer = self

        class _Handle:
            def delete(self):
                if outer.delete_error:
                    raise outer.delete_error
                outer.deleted.append(sid)

        return _Handle()


class _Trunking:
    def __init__(self):
        self.deleted: list[str] = []
        self.delete_error: Exception | None = None
        self.v1 = self

    def trunks(self, trunk_sid):
        outer = self

        class _Trunk:
            @property
            def phone_numbers(self):
                class _PhoneNumbers:
                    def create(self, phone_number_sid):
                        return type("R", (), {"sid": "TP1"})()

                    def __call__(self, resource_sid):
                        class _Handle:
                            def delete(self):
                                if outer.delete_error:
                                    raise outer.delete_error
                                outer.deleted.append(resource_sid)

                        return _Handle()

                return _PhoneNumbers()

        return _Trunk()


class _Messages:
    def __init__(self):
        self.created: dict = {}

    def create(self, **kwargs):
        self.created = kwargs
        return type(
            "M",
            (),
            {
                "sid": "SM1",
                "status": "queued",
                "from_": kwargs.get("from_"),
                "to": kwargs.get("to"),
            },
        )()


class _Calls:
    def __init__(self):
        self.created: dict = {}

    def create(self, **kwargs):
        self.created = kwargs
        return type("C", (), {"sid": "CA1", "status": "queued"})()


class _CountryMap(dict):
    def __missing__(self, cc):
        country = _Country()
        self[cc] = country
        return country


class _FakeClient:
    def __init__(self):
        # defaultdict so a test can reach for `available["ES"].local` before the
        # code under test has asked for that country.
        self.available: dict[str, _Country] = _CountryMap()
        self.incoming_phone_numbers = _IncomingPhoneNumbers()
        self.trunking = _Trunking()
        self.messages = _Messages()
        self.calls = _Calls()

    def available_phone_numbers(self, cc):
        return self.available.setdefault(cc, _Country())
