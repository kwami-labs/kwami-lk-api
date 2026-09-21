"""Structured logging and the request-correlation id.

Two problems this solves.

A user reporting a 500 could previously quote only
``{"detail": "An unexpected error occurred"}`` -- deliberately opaque, because
the alternative leaks upstream text -- and there was nothing in it to find the
matching server log line with. Every log record now carries a ``request_id``,
the same one returned in the body and the ``X-Request-ID`` header.

And the records were plain text built with f-strings, which a log aggregator
cannot group, filter or alert on. They are JSON now, so ``request_id``,
``path`` and ``status`` are fields rather than substrings.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from typing import Any

# Set by RequestContextMiddleware for the lifetime of one request. A ContextVar
# rather than a thread local: the service is async, so many requests share a
# thread and only the context is per-task.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Attributes LogRecord always carries; anything else was passed as `extra` and
# belongs in the payload.
_STANDARD_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "asctime",
    "message",
    "taskName",
}


def get_request_id() -> str | None:
    """The current request's id, or None outside a request."""
    return request_id_var.get()


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # Whatever the call site passed as `extra=`.
        for key, value in record.__dict__.items():
            if key not in _STANDARD_FIELDS and not key.startswith("_"):
                payload[key] = value

        # `default=str` so a stray UUID or datetime cannot make logging raise.
        return json.dumps(payload, default=str)


def configure_logging(*, debug: bool = False, json_output: bool = True) -> None:
    """Install the formatter on the root handler.

    Plain text stays available for local development, where a human is reading
    the terminal rather than an aggregator.
    """
    handler = logging.StreamHandler()
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
        )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if debug else logging.INFO)
