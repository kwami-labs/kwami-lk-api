"""Where a browser goes after consenting, to hand the result to the signed-in app.

A consent link can be sent to someone else, so its callback must not connect anything:
whoever opens the link would end up giving their account to whoever started it. The
callback only passes the code on to the app in that same browser, or on that same
computer, and the app finishes while signed in. The API compares that user with the one
who started it, so a stranger's code reaches their own app and is refused there.
"""

from __future__ import annotations

import httpx

from src.core.config import settings

#: Tauri registers this scheme. The webview's own origin is not reachable from a browser.
DESKTOP_TARGET = "kwami://connect/callback"


class UnknownReturnError(ValueError):
    """The request came from somewhere the result must not be sent to."""


def return_target(origin: str | None, *, desktop: bool) -> str:
    """Where this consent comes back to. It decides who could ever see the code.

    Exact origins only: with CORS at "*", any caller could name their own site, and
    collect the code of whoever opens the link.
    """
    if desktop:
        return DESKTOP_TARGET
    if not origin or origin == "*" or origin not in settings.cors_origins:
        raise UnknownReturnError("This origin cannot receive a connection")
    return f"{origin.rstrip('/')}/"


def return_url(target: str, provider: str, state: str, code: str) -> str:
    """In the fragment: it stays out of server logs and Referer headers.

    `grant`, not `code`: a `code` in the address is what Supabase takes for its own sign-in.
    """
    fragment = httpx.QueryParams({"connect": provider, "state": state, "grant": code})
    return f"{target}#{fragment}"
