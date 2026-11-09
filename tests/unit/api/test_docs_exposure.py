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

import re

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.docs import install_docs
from src.main import docs_urls

DOC_URLS = ("/openapi.json", "/docs", "/swagger", "/redoc")


def _app(show_docs: bool) -> FastAPI:
    app = FastAPI(**docs_urls(show_docs))
    if show_docs:
        install_docs(app)
    return app


async def _get(app: FastAPI, url: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(url)


def test_the_flag_being_off_closes_the_schema():
    assert docs_urls(False)["openapi_url"] is None


def test_the_flag_being_on_serves_the_schema():
    assert docs_urls(True)["openapi_url"] == "/openapi.json"


@pytest.mark.parametrize("show", [True, False])
def test_fastapis_own_pages_are_never_used(show: bool):
    """They are blocked by the API-wide CSP; `install_docs` serves working ones."""
    urls = docs_urls(show)
    assert urls["docs_url"] is None
    assert urls["redoc_url"] is None


@pytest.mark.anyio
@pytest.mark.parametrize("url", DOC_URLS)
async def test_a_closed_app_serves_none_of_them(url: str):
    assert (await _get(_app(False), url)).status_code == 404


@pytest.mark.anyio
@pytest.mark.parametrize("url", DOC_URLS)
async def test_an_open_app_serves_all_of_them(url: str):
    assert (await _get(_app(True), url)).status_code == 200


@pytest.mark.anyio
async def test_docs_is_scalar_and_swagger_is_swagger():
    docs = (await _get(_app(True), "/docs")).text
    swagger = (await _get(_app(True), "/swagger")).text
    assert "api-reference" in docs and "@scalar/api-reference@" in docs
    assert "SwaggerUIBundle" in swagger and "swagger-ui-dist@" in swagger


@pytest.mark.anyio
@pytest.mark.parametrize("url", ["/docs", "/swagger", "/redoc"])
async def test_each_page_carries_a_csp_that_lets_it_load(url: str):
    """The regression: `default-src 'none'` left every page blank in a browser."""
    response = await _get(_app(True), url)
    csp = response.headers["content-security-policy"]
    nonce = re.search(r"'nonce-([^']+)'", csp)
    assert nonce, csp
    assert "https://cdn.jsdelivr.net" in csp
    assert f'nonce="{nonce.group(1)}"' in response.text
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-eval" not in csp
    assert "connect-src 'self'" in csp


@pytest.mark.anyio
async def test_the_nonce_is_fresh_on_every_request():
    app = _app(True)
    first = (await _get(app, "/docs")).headers["content-security-policy"]
    second = (await _get(app, "/docs")).headers["content-security-policy"]
    assert first != second


@pytest.mark.anyio
async def test_the_pages_are_not_listed_in_the_schema():
    schema = (await _get(_app(True), "/openapi.json")).json()
    assert not {"/docs", "/swagger", "/redoc"} & set(schema["paths"])


@pytest.mark.anyio
async def test_the_rest_of_the_api_keeps_the_strict_csp():
    """Only the doc pages are relaxed: the middleware default stands everywhere else."""
    from src.main import app

    response = await _get(app, "/health")
    assert (
        response.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
    )


@pytest.mark.anyio
async def test_the_running_app_was_built_from_the_flag():
    """The wiring in src.main, not just the helper."""
    from src.core.config import settings
    from src.main import app

    expected = docs_urls(settings.show_docs)
    assert app.docs_url == expected["docs_url"]
    assert app.redoc_url == expected["redoc_url"]
    assert app.openapi_url == expected["openapi_url"]
    for url in DOC_URLS:
        served = (await _get(app, url)).status_code == 200
        assert served is settings.show_docs, url


@pytest.mark.anyio
async def test_the_schema_documents_the_api_as_a_whole():
    from src.main import app

    schema = app.openapi()
    assert "Authentication" in schema["info"]["description"]
    assert schema["info"]["contact"]["url"]
    tag_names = {t["name"] for t in schema["tags"]}
    used = {t for v in schema["paths"].values() for op in v.values() for t in op.get("tags", [])}
    assert used <= tag_names, f"tags with no description: {used - tag_names}"
    assert all(t["description"] for t in schema["tags"])
    assert {"UserJWT", "AdminApiKey", "AgentApiKey"} <= set(schema["components"]["securitySchemes"])


def test_every_operation_is_described():
    """`/docs` is the contract the app team reads; a route with no description is a hole in it."""
    from src.main import app

    undocumented = [
        f"{method.upper()} {path}"
        for path, operations in app.openapi()["paths"].items()
        for method, op in operations.items()
        if not (op.get("description") or "").strip()
    ]
    assert not undocumented, "routes with no docstring:\n  " + "\n  ".join(undocumented)
