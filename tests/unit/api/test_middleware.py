"""`src.api.middleware` — request correlation, security headers, body caps.

Before this the application had one middleware, CORS. A reported 500 could not
be traced to a log line, no security headers were sent, and
`/webhooks/email/inbound` read an unbounded multipart body into memory.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from src.api.middleware import (
    REQUEST_ID_HEADER,
    SECURITY_HEADERS,
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from src.core.logging import get_request_id

pytestmark = pytest.mark.anyio


def _app(*middleware, max_bytes: int = 1024) -> Starlette:
    async def echo(request):
        body = await request.body()
        return PlainTextResponse(f"{len(body)}:{get_request_id()}")

    app = Starlette(routes=[Route("/echo", echo, methods=["GET", "POST"])])
    for cls in middleware:
        if cls is BodySizeLimitMiddleware:
            app.add_middleware(cls, max_bytes=max_bytes)
        else:
            app.add_middleware(cls)
    return app


async def _call(app, method="GET", **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.request(method, "/echo", **kwargs)


class TestRequestContext:
    async def test_every_response_carries_a_request_id(self):
        response = await _call(_app(RequestContextMiddleware))
        assert response.headers[REQUEST_ID_HEADER]

    async def test_the_id_is_visible_to_the_handler(self):
        """It is what `error_body` puts in the envelope."""
        response = await _call(_app(RequestContextMiddleware))
        assert response.text.split(":")[1] == response.headers[REQUEST_ID_HEADER]

    async def test_two_requests_get_different_ids(self):
        app = _app(RequestContextMiddleware)
        first = await _call(app)
        second = await _call(app)
        assert first.headers[REQUEST_ID_HEADER] != second.headers[REQUEST_ID_HEADER]

    async def test_an_inbound_id_is_honoured(self):
        """A trace started at the edge keeps one id end to end."""
        response = await _call(
            _app(RequestContextMiddleware), headers={REQUEST_ID_HEADER: "edge-abc123"}
        )
        assert response.headers[REQUEST_ID_HEADER] == "edge-abc123"

    async def test_an_overlong_inbound_id_is_truncated(self):
        response = await _call(
            _app(RequestContextMiddleware), headers={REQUEST_ID_HEADER: "x" * 5000}
        )
        assert len(response.headers[REQUEST_ID_HEADER]) <= RequestContextMiddleware.MAX_ID_LENGTH

    async def test_unprintable_characters_are_stripped(self):
        """The id reaches the log; a control character there is a log-injection."""
        response = await _call(
            _app(RequestContextMiddleware), headers={REQUEST_ID_HEADER: "a\u0000b\u001bc"}
        )
        assert response.headers[REQUEST_ID_HEADER] == "abc"

    async def test_an_empty_inbound_id_is_replaced(self):
        response = await _call(_app(RequestContextMiddleware), headers={REQUEST_ID_HEADER: "   "})
        assert response.headers[REQUEST_ID_HEADER].strip()

    async def test_the_context_does_not_leak_between_requests(self):
        await _call(_app(RequestContextMiddleware))
        assert get_request_id() is None

    async def test_the_access_line_is_logged_with_its_fields(self, caplog):
        with caplog.at_level("INFO", logger="kwami-api.access"):
            await _call(_app(RequestContextMiddleware))
        record = next(r for r in caplog.records if r.name == "kwami-api.access")
        assert record.path == "/echo"
        assert record.status == 200
        assert record.duration_ms >= 0


class TestSecurityHeaders:
    @pytest.mark.parametrize("header", sorted(SECURITY_HEADERS))
    async def test_each_header_is_sent(self, header):
        response = await _call(_app(SecurityHeadersMiddleware))
        assert response.headers[header] == SECURITY_HEADERS[header]

    async def test_hsts_is_only_sent_over_tls(self):
        """Meaningless on plain HTTP, and browsers ignore it there anyway."""
        response = await _call(_app(SecurityHeadersMiddleware))
        assert "Strict-Transport-Security" not in response.headers

    async def test_hsts_is_sent_over_tls(self):
        app = _app(SecurityHeadersMiddleware)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://t") as client:
            response = await client.get("/echo")
        assert "max-age=" in response.headers["Strict-Transport-Security"]


class TestBodySizeLimit:
    async def test_a_small_body_passes(self):
        response = await _call(_app(BodySizeLimitMiddleware), "POST", content=b"x" * 100)
        assert response.status_code == 200

    async def test_an_oversized_declared_body_is_refused(self):
        response = await _call(_app(BodySizeLimitMiddleware), "POST", content=b"x" * 5000)
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"

    async def test_a_malformed_content_length_is_refused(self):
        response = await _call(
            _app(BodySizeLimitMiddleware),
            "POST",
            content=b"x" * 10,
            headers={"content-length": "not-a-number"},
        )
        assert response.status_code == 413

    async def test_a_chunked_body_is_counted_as_it_streams(self):
        """Omitting Content-Length must not get around the cap."""

        async def chunks():
            for _ in range(20):
                yield b"x" * 200

        response = await _call(_app(BodySizeLimitMiddleware), "POST", content=chunks())
        assert response.status_code == 413

    async def test_a_chunked_body_under_the_cap_passes(self):
        async def chunks():
            yield b"x" * 100

        response = await _call(_app(BodySizeLimitMiddleware), "POST", content=chunks())
        assert response.status_code == 200


class TestNonHttpScopes:
    """Lifespan and websocket scopes pass straight through.

    A middleware that assumes `scope["type"] == "http"` breaks application
    startup, because the lifespan event goes through the same stack.
    """

    @pytest.mark.parametrize(
        "middleware",
        [RequestContextMiddleware, SecurityHeadersMiddleware, BodySizeLimitMiddleware],
    )
    async def test_a_lifespan_scope_is_passed_through_untouched(self, middleware):
        seen: list[dict] = []

        async def app(scope, receive, send):
            seen.append(scope)

        await middleware(app)({"type": "lifespan"}, None, None)
        assert seen == [{"type": "lifespan"}]


class TestBodyLimitEdges:
    async def test_a_handler_that_answers_before_reading_is_left_alone(self):
        """If the response already started there is nothing left to say."""

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": [(b"x", b"y")]})
            await receive()  # only now discover the body is too large
            await send({"type": "http.response.body", "body": b""})

        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {"type": "http.request", "body": b"x" * 5000, "more_body": False}

        from src.api.middleware import _BodyTooLargeError

        middleware = BodySizeLimitMiddleware(app, max_bytes=10)
        with pytest.raises(_BodyTooLargeError):
            await middleware({"type": "http", "headers": []}, receive, send)
        assert sent[0]["status"] == 200, "the started response is not rewritten"

    async def test_the_standin_receive_channel_reports_an_empty_body(self):
        from src.api.middleware import receive_nothing

        assert await receive_nothing() == {
            "type": "http.request",
            "body": b"",
            "more_body": False,
        }

    async def test_a_disconnect_message_is_passed_through_uncounted(self):
        """Only `http.request` carries a body; anything else is forwarded as-is."""
        received: list[dict] = []

        async def app(scope, receive, send):
            received.append(await receive())
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            pass

        middleware = BodySizeLimitMiddleware(app, max_bytes=1)
        await middleware({"type": "http", "headers": []}, receive, send)
        assert received == [{"type": "http.disconnect"}]
