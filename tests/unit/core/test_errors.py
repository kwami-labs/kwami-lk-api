"""`src.core.errors` — the one place an exception becomes an HTTP response.

Two properties matter and are easy to lose:

1. Every error leaves through the same envelope, `detail` *and* `error.code`.
   The Kwami app reads `detail`; new clients read `error.code`.
2. Nothing upstream-specific reaches the client. The unhandled handler logs the
   traceback and returns a fixed string; the validation handler drops Pydantic's
   `input` field, which for /token and /email/send is user content.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from src.core.errors import (
    ChannelNotFoundError,
    ConflictError,
    ContactNotFoundError,
    DomainError,
    ExternalServiceError,
    ForbiddenError,
    InsufficientCreditsError,
    InvalidEmailAddressError,
    InvalidPhoneNumberError,
    KwamiNotFoundError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationFailedError,
    WalletNotFoundError,
    _code_for_status,
    error_body,
    install_error_handlers,
)


class TestDomainErrorConstruction:
    def test_the_docstring_is_the_default_message(self):
        assert KwamiNotFoundError().message == "Kwami not found."

    def test_an_explicit_message_wins(self):
        assert KwamiNotFoundError("gone").message == "gone"

    def test_the_class_code_is_the_default(self):
        assert KwamiNotFoundError().code == "kwami_not_found"

    def test_an_explicit_code_overrides_it_on_the_instance_only(self):
        err = KwamiNotFoundError(code="custom")
        assert err.code == "custom"
        assert KwamiNotFoundError().code == "kwami_not_found", "the class must not be mutated"

    def test_details_ride_along(self):
        assert KwamiNotFoundError(details={"id": "k1"}).details == {"id": "k1"}
        assert KwamiNotFoundError().details is None

    def test_it_is_a_normal_exception_carrying_its_message(self):
        with pytest.raises(DomainError, match="boom"):
            raise DomainError("boom")
        assert str(KwamiNotFoundError("gone")) == "gone"

    def test_a_subclass_without_a_docstring_falls_back_to_a_generic_message(self):
        class BareError(DomainError):
            __doc__ = None

        assert BareError().message == "Request failed"


@pytest.mark.parametrize(
    ("cls", "status_code", "code"),
    [
        (DomainError, 400, "bad_request"),
        (NotFoundError, 404, "not_found"),
        (ForbiddenError, 403, "forbidden"),
        (ConflictError, 409, "conflict"),
        (ValidationFailedError, 422, "validation_failed"),
        (InsufficientCreditsError, 402, "insufficient_credits"),
        (ExternalServiceError, 502, "upstream_error"),
        (ServiceUnavailableError, 503, "service_unavailable"),
        (KwamiNotFoundError, 404, "kwami_not_found"),
        (ChannelNotFoundError, 404, "channel_not_found"),
        (ContactNotFoundError, 404, "contact_not_found"),
        (WalletNotFoundError, 404, "wallet_not_found"),
        (InvalidPhoneNumberError, 400, "invalid_phone_number"),
        (InvalidEmailAddressError, 400, "invalid_email_address"),
    ],
)
def test_every_error_keeps_its_status_and_code(cls, status_code, code):
    """A silent remap here turns a 402 into a 500 for every caller."""
    assert cls.status_code == status_code
    assert cls.code == code


def test_error_body_carries_both_shapes():
    assert error_body("kwami_not_found", "Kwami not found", {"id": "k1"}) == {
        "detail": "Kwami not found",
        "error": {
            "code": "kwami_not_found",
            "message": "Kwami not found",
            "details": {"id": "k1"},
        },
    }


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (400, "bad_request"),
        (401, "unauthorized"),
        (402, "insufficient_credits"),
        (403, "forbidden"),
        (404, "not_found"),
        (409, "conflict"),
        (422, "validation_failed"),
        (429, "rate_limited"),
        (500, "internal_error"),
        (502, "upstream_error"),
        (503, "service_unavailable"),
        (418, "error"),
        (599, "error"),
    ],
)
def test_status_codes_map_to_stable_slugs(status_code, expected):
    assert _code_for_status(status_code) == expected


class Body(BaseModel):
    name: str
    secret: str


@pytest.fixture
def handled_app() -> FastAPI:
    """A minimal app with the real handlers and one route per failure mode."""
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/domain")
    async def _domain():
        raise KwamiNotFoundError(details={"kwami_id": "k1"})

    @app.get("/domain-bare")
    async def _domain_bare():
        raise InsufficientCreditsError()

    @app.get("/http-str")
    async def _http_str():
        raise HTTPException(status_code=403, detail="Admin access required")

    @app.get("/http-dict")
    async def _http_dict():
        raise HTTPException(status_code=409, detail={"reason": "already claimed"})

    @app.get("/http-headers")
    async def _http_headers():
        raise HTTPException(status_code=401, detail="nope", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/http-teapot")
    async def _http_teapot():
        raise HTTPException(status_code=418, detail="short and stout")

    @app.post("/validate")
    async def _validate(body: Body):
        return {"ok": body.name}

    @app.get("/boom")
    async def _boom():
        raise RuntimeError("upstream said: sk_live_deadbeef leaked")

    return app


@pytest.fixture
async def error_client(handled_app: FastAPI):
    transport = ASGITransport(app=handled_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_a_domain_error_becomes_its_own_status_and_envelope(error_client):
    r = await error_client.get("/domain")
    assert r.status_code == 404
    assert r.json() == {
        "detail": "Kwami not found.",
        "error": {
            "code": "kwami_not_found",
            "message": "Kwami not found.",
            "details": {"kwami_id": "k1"},
        },
    }


@pytest.mark.anyio
async def test_a_domain_error_without_details_still_has_the_key(error_client):
    r = await error_client.get("/domain-bare")
    assert r.status_code == 402
    assert r.json()["error"]["code"] == "insufficient_credits"
    assert r.json()["error"]["details"] is None


@pytest.mark.anyio
async def test_an_http_exception_with_a_string_detail(error_client):
    r = await error_client.get("/http-str")
    assert r.status_code == 403
    assert r.json() == {
        "detail": "Admin access required",
        "error": {
            "code": "forbidden",
            "message": "Admin access required",
            "details": None,
        },
    }


@pytest.mark.anyio
async def test_a_non_string_detail_moves_into_details(error_client):
    r = await error_client.get("/http-dict")
    assert r.status_code == 409
    assert r.json() == {
        "detail": "Request failed",
        "error": {
            "code": "conflict",
            "message": "Request failed",
            "details": {"reason": "already claimed"},
        },
    }


@pytest.mark.anyio
async def test_headers_survive_the_rewrite(error_client):
    """Dropping WWW-Authenticate would break every 401 client."""
    r = await error_client.get("/http-headers")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.anyio
async def test_an_unmapped_status_gets_the_generic_slug(error_client):
    r = await error_client.get("/http-teapot")
    assert r.status_code == 418
    assert r.json()["error"]["code"] == "error"


@pytest.mark.anyio
async def test_a_404_from_no_matching_route_uses_the_envelope_too(error_client):
    r = await error_client.get("/nothing-here")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


@pytest.mark.anyio
async def test_validation_errors_report_location_and_reason_only(error_client):
    r = await error_client.post("/validate", json={"name": 1234})
    assert r.status_code == 422
    body = r.json()
    assert body["detail"] == "Request validation failed"
    assert body["error"]["code"] == "validation_failed"
    for item in body["error"]["details"]:
        assert set(item) == {"loc", "msg", "type"}


@pytest.mark.anyio
async def test_validation_errors_do_not_echo_the_offending_value(error_client):
    """Pydantic's raw list carries `input`; for /email/send that is user content."""
    r = await error_client.post("/validate", json={"name": "ok", "secret": {"leak": "me"}})
    assert r.status_code == 422
    assert "leak" not in r.text


@pytest.mark.anyio
async def test_an_unhandled_exception_is_a_flat_500(error_client):
    r = await error_client.get("/boom")
    assert r.status_code == 500
    assert r.json() == {
        "detail": "An unexpected error occurred",
        "error": {
            "code": "internal_error",
            "message": "An unexpected error occurred",
            "details": None,
        },
    }


@pytest.mark.anyio
async def test_an_unhandled_exception_never_forwards_upstream_text(error_client):
    r = await error_client.get("/boom")
    assert "sk_live_deadbeef" not in r.text
    assert "RuntimeError" not in r.text


@pytest.mark.anyio
async def test_the_traceback_is_logged_server_side(error_client, caplog):
    with caplog.at_level("ERROR", logger="kwami-api.errors"):
        await error_client.get("/boom")
    assert "Unhandled error on GET /boom" in caplog.text
    assert "sk_live_deadbeef" in caplog.text, "the detail belongs in the log, not the response"
