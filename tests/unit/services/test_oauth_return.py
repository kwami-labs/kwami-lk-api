"""Where a consent's result may go: only to the app, never to who asks for it."""

from __future__ import annotations

import httpx
import pytest

from src.services import oauth_return as o


class TestReturnTarget:
    def test_the_desktop_app_has_its_own_link(self):
        assert o.return_target(None, desktop=True) == o.DESKTOP_TARGET

    def test_a_listed_origin_is_accepted(self, monkeypatch):
        monkeypatch.setattr(o.settings, "cors_origins_str", "https://kwami.test")
        assert o.return_target("https://kwami.test", desktop=False) == "https://kwami.test/"

    @pytest.mark.parametrize("origin", ["https://evil.test", "", None])
    def test_anything_else_is_refused(self, monkeypatch, origin):
        monkeypatch.setattr(o.settings, "cors_origins_str", "https://kwami.test")
        with pytest.raises(o.UnknownReturnError):
            o.return_target(origin, desktop=False)

    def test_an_open_cors_list_accepts_nobody(self, monkeypatch):
        """With "*", a caller could name their own site and collect someone's code."""
        monkeypatch.setattr(o.settings, "cors_origins_str", "*")
        with pytest.raises(o.UnknownReturnError):
            o.return_target("https://evil.test", desktop=False)
        with pytest.raises(o.UnknownReturnError):
            o.return_target("*", desktop=False)


def test_the_result_travels_in_the_fragment():
    target, fragment = o.return_url("https://kwami.test/", "google", "s t", "c&d").split("#")
    assert target == "https://kwami.test/"
    assert dict(httpx.QueryParams(fragment)) == {
        "connect": "google",
        "state": "s t",
        "grant": "c&d",
    }
