"""Interactive API documentation: Scalar at `/docs`, Swagger UI at `/swagger`, ReDoc at `/redoc`.

FastAPI's built-in `/docs` renders Swagger UI from jsDelivr, and every response here carries
`Content-Security-Policy: default-src 'none'` (see `SECURITY_HEADERS`). The browser therefore refused
the Swagger scripts and stylesheet and the page was blank, even though the route answered 200.

So the three pages are served from here instead, each with its own narrow policy: scripts only from
jsDelivr and only with a per-request nonce, nothing framed, and no network access beyond this
origin. The API-wide policy is untouched; `SecurityHeadersMiddleware` uses `setdefault`, so a header
a route sets itself is the one that is sent, and only these three routes do.

The bundles are pinned to exact versions. A floating `@latest` on a page that holds a bearer token in
its "Authorize" box is a supply-chain hole.
"""

from __future__ import annotations

import json
import secrets
from html import escape

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse

SCALAR_VERSION = "1.76.4"
SWAGGER_UI_VERSION = "5.34.1"
REDOC_VERSION = "2.5.4"

_CDN = "https://cdn.jsdelivr.net"

SCALAR_JS = f"{_CDN}/npm/@scalar/api-reference@{SCALAR_VERSION}/dist/browser/standalone.min.js"
SWAGGER_JS = f"{_CDN}/npm/swagger-ui-dist@{SWAGGER_UI_VERSION}/swagger-ui-bundle.js"
SWAGGER_CSS = f"{_CDN}/npm/swagger-ui-dist@{SWAGGER_UI_VERSION}/swagger-ui.css"
REDOC_JS = f"{_CDN}/npm/redoc@{REDOC_VERSION}/bundles/redoc.standalone.js"

# The three UIs inject their own <style> elements at runtime, which a nonce cannot cover, so
# `style-src` has to allow inline styles. Script execution is what the nonce locks down.
_FAVICON = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E"


def docs_csp(nonce: str) -> str:
    return "; ".join(
        (
            "default-src 'none'",
            f"script-src 'nonce-{nonce}' {_CDN}",
            f"style-src 'unsafe-inline' {_CDN}",
            f"font-src data: {_CDN} https://fonts.scalar.com",
            "img-src 'self' data: https:",
            # "Try it out" calls the API itself; nothing else may be reached from the page.
            "connect-src 'self'",
            "worker-src blob:",
            "frame-ancestors 'none'",
            "base-uri 'none'",
            "form-action 'none'",
        )
    )


def _page(html: str, nonce: str) -> HTMLResponse:
    return HTMLResponse(html, headers={"Content-Security-Policy": docs_csp(nonce)})


def _scalar_page(title: str, openapi_url: str, nonce: str) -> str:
    config = json.dumps(
        {
            "theme": "purple",
            "layout": "modern",
            "defaultOpenAllTags": False,
            "hideClientButton": False,
            "persistAuth": False,
            "metaData": {"title": title},
        }
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<link rel="icon" href="{_FAVICON}">
</head>
<body>
<script id="api-reference" nonce="{nonce}" data-url="{escape(openapi_url)}" data-configuration='{escape(config, quote=True)}'></script>
<script nonce="{nonce}" src="{SCALAR_JS}"></script>
</body>
</html>"""


def _swagger_page(title: str, openapi_url: str, nonce: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<link rel="icon" href="{_FAVICON}">
<link rel="stylesheet" href="{SWAGGER_CSS}">
</head>
<body>
<div id="swagger-ui"></div>
<script nonce="{nonce}" src="{SWAGGER_JS}"></script>
<script nonce="{nonce}">
  window.ui = SwaggerUIBundle({{
    url: {json.dumps(openapi_url)},
    dom_id: "#swagger-ui",
    deepLinking: true,
    displayRequestDuration: true,
    filter: true,
    persistAuthorization: false,
    tryItOutEnabled: true,
    presets: [SwaggerUIBundle.presets.apis],
    layout: "BaseLayout",
  }});
</script>
</body>
</html>"""


def _redoc_page(title: str, openapi_url: str, nonce: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<link rel="icon" href="{_FAVICON}">
</head>
<body>
<redoc spec-url="{escape(openapi_url)}"></redoc>
<script nonce="{nonce}" src="{REDOC_JS}"></script>
</body>
</html>"""


def install_docs(app: FastAPI) -> None:
    """Mount `/docs` (Scalar), `/swagger` and `/redoc` on `app`.

    Called only when docs are enabled. The routes stay out of the schema they render.
    """
    router = APIRouter(include_in_schema=False)
    openapi_url = app.openapi_url or "/openapi.json"

    @router.get("/docs")
    async def scalar(request: Request) -> HTMLResponse:
        nonce = secrets.token_urlsafe(16)
        return _page(_scalar_page(f"{app.title} - API reference", openapi_url, nonce), nonce)

    @router.get("/swagger")
    async def swagger(request: Request) -> HTMLResponse:
        nonce = secrets.token_urlsafe(16)
        return _page(_swagger_page(f"{app.title} - Swagger UI", openapi_url, nonce), nonce)

    @router.get("/redoc")
    async def redoc(request: Request) -> HTMLResponse:
        nonce = secrets.token_urlsafe(16)
        return _page(_redoc_page(f"{app.title} - ReDoc", openapi_url, nonce), nonce)

    app.include_router(router)
