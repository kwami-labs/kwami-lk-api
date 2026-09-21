"""The docs flag has to close the schema, not just the two UIs.

`ENABLE_DOCS=false` in production was only removing `docs_url` and `redoc_url`.
`/openapi.json` stayed served: the same route map, parameters and models the UIs
render, minus the HTML.

These assert on `src.main.docs_urls` -- the real production decision -- rather
than on a locally built app, so the "off" case is exercised whatever
`tests/.env.test` sets `APP_ENV` to. Serving is checked too, to pin that a
`None` really does mean 404 rather than a FastAPI default.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.main import docs_urls

DOC_URLS = ("/openapi.json", "/docs", "/redoc")


async def _status(app: FastAPI, url: str) -> int:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return (await client.get(url)).status_code


def test_the_flag_being_off_closes_all_three():
    assert docs_urls(False) == {"docs_url": None, "redoc_url": None, "openapi_url": None}


def test_the_flag_being_on_opens_all_three():
    assert docs_urls(True) == {
        "docs_url": "/docs",
        "redoc_url": "/redoc",
        "openapi_url": "/openapi.json",
    }


@pytest.mark.anyio
@pytest.mark.parametrize("url", DOC_URLS)
async def test_a_closed_app_serves_none_of_them(url: str):
    assert await _status(FastAPI(**docs_urls(False)), url) == 404


@pytest.mark.anyio
@pytest.mark.parametrize("url", DOC_URLS)
async def test_an_open_app_serves_all_of_them(url: str):
    assert await _status(FastAPI(**docs_urls(True)), url) == 200


def test_the_running_app_was_built_from_the_flag():
    """The wiring in src.main, not just the helper."""
    from src.core.config import settings
    from src.main import app

    expected = docs_urls(settings.show_docs)
    assert app.docs_url == expected["docs_url"]
    assert app.redoc_url == expected["redoc_url"]
    assert app.openapi_url == expected["openapi_url"]
