"""Small adapters shared by the unit tests.

The service layer moved to the async Supabase client, so a stub standing in for
a service function has to be awaitable. A plain `lambda: None` raises
``TypeError: object NoneType can't be used in 'await' expression`` at the call
site, which says nothing useful about the test.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def async_return(value: Any) -> Callable[..., Any]:
    """A coroutine function that ignores its arguments and returns ``value``."""

    async def _stub(*_args: Any, **_kwargs: Any) -> Any:
        return value

    return _stub


def async_sequence(*values: Any) -> Callable[..., Any]:
    """A coroutine function returning each value in turn, then repeating the last.

    For the read-then-reread paths: a claim that loses a race reads ``None``,
    then re-reads and finds the winner's row.
    """
    remaining = list(values)

    async def _stub(*_args: Any, **_kwargs: Any) -> Any:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return _stub


def async_raise(exc: BaseException) -> Callable[..., Any]:
    """A coroutine function that raises ``exc``."""

    async def _stub(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    return _stub
