"""Domain errors and the single place they become HTTP responses.

Why this exists
---------------
Services signalled failure with bare ``ValueError("Kwami not found")``. Routes
then each decided what that meant: some caught it and returned 404, most did not
catch it at all and returned 500 for what is an ordinary not-found. Separately,
~52 handlers ended in ``raise HTTPException(500, detail=str(e))``, which forwards
upstream provider text (and whatever it contains) straight to the client.

Services raise a typed error; the handlers registered by ``install_error_handlers``
map it to a status code and a stable envelope. Routes stop catching anything they
do not genuinely handle.

Envelope::

    {"detail": "Kwami not found",
     "error": {"code": "kwami_not_found", "message": "Kwami not found", "details": null}}

``detail`` is retained alongside the structured ``error`` object deliberately.
The Kwami app already reads ``detail`` from every error response, so emitting only
the new shape would break error handling in a separate deployable. New clients
read ``error.code``, which is stable in a way that a human-readable string is not;
``detail`` can be dropped once the app no longer reads it.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("kwami-api.errors")


class DomainError(Exception):
    """Base class for expected, client-facing failures."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "bad_request"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: Any = None,
    ) -> None:
        self.message = message or self.__class__.__doc__ or "Request failed"
        self.details = details
        if code:
            self.code = code
        super().__init__(self.message)


class NotFoundError(DomainError):
    """The requested resource does not exist."""

    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ForbiddenError(DomainError):
    """You do not have access to this resource."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class ConflictError(DomainError):
    """The request conflicts with the current state."""

    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class ValidationFailedError(DomainError):
    """The request was not valid."""

    status_code = 422  # starlette renamed its constant; the literal is version-safe
    code = "validation_failed"


class InsufficientCreditsError(DomainError):
    """Insufficient credits. Please purchase credits to continue."""

    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "insufficient_credits"


class ExternalServiceError(DomainError):
    """An upstream service failed."""

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "upstream_error"


class ServiceUnavailableError(DomainError):
    """This feature is not configured."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"


# -- specific errors, so call sites read as intent ---------------------------


class KwamiNotFoundError(NotFoundError):
    """Kwami not found."""

    code = "kwami_not_found"


class ChannelNotFoundError(NotFoundError):
    """Channel not found."""

    code = "channel_not_found"


class ContactNotFoundError(NotFoundError):
    """Contact not found."""

    code = "contact_not_found"


class WalletNotFoundError(NotFoundError):
    """Wallet not found."""

    code = "wallet_not_found"


class InvalidPhoneNumberError(ValidationFailedError):
    """The phone number is not valid."""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "invalid_phone_number"


class InvalidEmailAddressError(ValidationFailedError):
    """The email address is not valid."""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "invalid_email_address"


def error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {
        # Kept for the existing app, which reads `detail` on every error.
        "detail": message,
        "error": {"code": code, "message": message, "details": details},
    }


def install_error_handlers(app: FastAPI) -> None:
    """Register the handlers that turn exceptions into the envelope."""

    @app.exception_handler(DomainError)
    async def _domain_error(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        message = detail if isinstance(detail, str) else "Request failed"
        details = None if isinstance(detail, str) else detail
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(_code_for_status(exc.status_code), message, details),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic's raw error list echoes the offending `input` value back to the
        # caller. For /token and /email/send that is user content, so only the
        # location and the reason are returned.
        details = [
            {"loc": list(err.get("loc", ())), "msg": err.get("msg"), "type": err.get("type")}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=error_body("validation_failed", "Request validation failed", details),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Full traceback server-side, nothing upstream-specific to the client.
        logger.exception("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_body("internal_error", "An unexpected error occurred"),
        )


_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    402: "insufficient_credits",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "validation_failed",
    429: "rate_limited",
    500: "internal_error",
    502: "upstream_error",
    503: "service_unavailable",
}


def _code_for_status(status_code: int) -> str:
    return _STATUS_CODES.get(status_code, "error")
