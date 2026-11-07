"""`src.core.logging` — JSON records carrying the request id.

Records used to be plain text built with f-strings, which an aggregator cannot
group or alert on, and carried nothing tying a line to the request that
produced it. A user reporting a 500 quoted an opaque message and there was no
way to find it.
"""

from __future__ import annotations

import json
import logging

import pytest

from src.core.logging import (
    JsonFormatter,
    configure_logging,
    get_request_id,
    request_id_var,
)


def _record(**kwargs) -> logging.LogRecord:
    defaults = {
        "name": "kwami-api.test",
        "level": logging.INFO,
        "pathname": __file__,
        "lineno": 1,
        "msg": "hello %s",
        "args": ("world",),
        "exc_info": None,
    }
    return logging.LogRecord(**{**defaults, **kwargs})


class TestJsonFormatter:
    def test_it_emits_one_json_object(self):
        payload = json.loads(JsonFormatter().format(_record()))
        assert payload["level"] == "INFO"
        assert payload["logger"] == "kwami-api.test"
        assert payload["message"] == "hello world", "%-style args are interpolated"
        assert payload["ts"]

    def test_the_request_id_is_attached_when_there_is_one(self):
        token = request_id_var.set("req-123")
        try:
            payload = json.loads(JsonFormatter().format(_record()))
        finally:
            request_id_var.reset(token)
        assert payload["request_id"] == "req-123"

    def test_there_is_no_request_id_outside_a_request(self):
        assert "request_id" not in json.loads(JsonFormatter().format(_record()))

    def test_extra_fields_become_top_level_keys(self):
        """`path` and `status` must be fields, not substrings of a message."""
        record = _record()
        record.path = "/token"
        record.status = 200
        payload = json.loads(JsonFormatter().format(record))
        assert payload["path"] == "/token"
        assert payload["status"] == 200

    def test_an_exception_is_included(self):
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = _record(exc_info=sys.exc_info())
        payload = json.loads(JsonFormatter().format(record))
        assert "ValueError: boom" in payload["exception"]

    def test_an_unserialisable_value_does_not_break_logging(self):
        """Logging must never be the thing that raises."""
        record = _record()
        record.thing = object()
        payload = json.loads(JsonFormatter().format(record))
        assert isinstance(payload["thing"], str)


class TestConfigureLogging:
    @pytest.fixture(autouse=True)
    def _restore(self):
        root = logging.getLogger()
        saved, level = list(root.handlers), root.level
        yield
        root.handlers = saved
        root.setLevel(level)

    def test_json_output_installs_the_json_formatter(self):
        configure_logging(json_output=True)
        assert isinstance(logging.getLogger().handlers[0].formatter, JsonFormatter)

    def test_text_output_is_available_for_local_development(self):
        configure_logging(json_output=False)
        assert not isinstance(logging.getLogger().handlers[0].formatter, JsonFormatter)

    def test_debug_lowers_the_level(self):
        configure_logging(debug=True)
        assert logging.getLogger().level == logging.DEBUG

    def test_it_replaces_rather_than_stacks_handlers(self):
        """Calling it twice must not double every line."""
        configure_logging()
        configure_logging()
        assert len(logging.getLogger().handlers) == 1


class TestGetRequestId:
    def test_it_is_none_outside_a_request(self):
        assert get_request_id() is None

    def test_it_reads_the_context(self):
        token = request_id_var.set("abc")
        try:
            assert get_request_id() == "abc"
        finally:
            request_id_var.reset(token)
