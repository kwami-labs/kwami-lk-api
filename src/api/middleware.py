"""The middlewares every request passes through.

Before this the application had exactly one -- CORS. That left three gaps that
matter once the service is carrying real traffic:

* no correlation id, so a reported 500 could not be found in the logs;
* no security headers;
* no cap on request bodies, and `/webhooks/email/inbound` reads a multipart
  form with attachments straight into memory.
"""

from __future__ import annotations

import logging
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.core.errors import error_body
from src.core.logging import request_id_var

logger = logging.getLogger("kwami-api.access")

REQUEST_ID_HEADER = "X-Request-ID"

# 10 MiB. SendGrid Inbound Parse forwards attachments, and `await request.form()`
# buffers the whole body; without a cap one message decides the worker's memory.
DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024

SECURITY_HEADERS = {
    # The API serves JSON and TwiML, never documents a browser should sniff.
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # Nothing here is embedded anywhere; the API is not a page.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}

# Two years, the minimum for preload lists. Only sent over TLS: on a plain-HTTP
# request the header is meaningless and browsers ignore it anyway.
HSTS_HEADER = "max-age=63072000; includeSubDomains"


class RequestContextMiddleware:
    """Assign a request id, log the access line, and return the id to the caller.

    An inbound ``X-Request-ID`` is honoured so a trace started at the edge -- the
    Cloudflare Worker, or the app -- keeps one id end to end. It is length-capped
    and stripped of anything unprintable, because it reaches the log.

    Raw ASGI rather than ``BaseHTTPMiddleware``. That base class runs the rest of
    the application in a separate anyio task with a pair of memory streams
    between them, which costs a task and two queue hops on every request and --
    less obviously -- hides the downstream code from coverage's tracer, so whole
    route modules measured 26% while their tests passed.
    """

    MAX_ID_LENGTH = 200

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = self._incoming_id(scope) or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500

        async def send_with_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "request",
                extra={
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "status": status_code,
                    "duration_ms": round(duration_ms, 2),
                    "request_id": request_id,
                },
            )
            request_id_var.reset(token)

    @classmethod
    def _incoming_id(cls, scope: Scope) -> str | None:
        for name, value in scope.get("headers") or []:
            if name == REQUEST_ID_HEADER.lower().encode():
                raw = value.decode("latin-1")
                cleaned = "".join(c for c in raw if c.isprintable())[: cls.MAX_ID_LENGTH].strip()
                return cleaned or None
        return None


class SecurityHeadersMiddleware:
    """Add the response headers that cost nothing and close whole bug classes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        is_tls = scope.get("scheme") == "https"

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
                if is_tls:
                    headers.setdefault("Strict-Transport-Security", HSTS_HEADER)
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodySizeLimitMiddleware:
    """Refuse a body larger than ``max_bytes`` with 413.

    Raw ASGI rather than ``BaseHTTPMiddleware``: that base class builds a *new*
    ``Request`` downstream from the scope and receive channel, so wrapping the
    stream on the request object it hands to ``dispatch`` has no effect on what
    the handler actually reads. Wrapping ``receive`` is the only place the byte
    count can be enforced, and it avoids the extra task and queue the base class
    adds to every request.

    ``Content-Length`` is checked where the client declares one, and bytes are
    counted as they arrive otherwise, so a chunked upload cannot get around the
    cap by omitting the header.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int = DEFAULT_MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > self.max_bytes
            except ValueError:
                too_big = True
            if too_big:
                await self._reject(scope, send)
                return

        seen = 0
        response_started = False

        async def counted_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    # Raised into the handler that is awaiting the body. It
                    # unwinds through the app and is caught below, where a 413
                    # can still be sent because nothing has been written yet.
                    raise _BodyTooLargeError
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, guarded_send)
        except _BodyTooLargeError:
            if response_started:
                # The handler answered before reading the oversized body; there
                # is nothing left to say on this connection.
                raise
            await self._reject(scope, send)

    async def _reject(self, scope: Scope, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content=error_body("payload_too_large", "Request body is too large"),
        )
        await response(scope, receive_nothing, send)


async def receive_nothing() -> Message:
    """A receive channel for a response sent without reading the request."""
    return {"type": "http.request", "body": b"", "more_body": False}


class _BodyTooLargeError(Exception):
    """Raised from the counted receive channel; converted to a 413 above."""
