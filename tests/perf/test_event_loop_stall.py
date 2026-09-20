"""Does a slow database call block every other request?

Every third-party SDK this service uses is the synchronous one -- the Supabase
client from `create_client`, `stripe.*`, `twilio.rest.Client`, and
`urllib.request.urlopen` in the reconciliation service -- and nothing offloads
them. They are called straight from `async def` handlers, so each one parks the
whole event loop for the duration of a network round trip. A static walk of the
call graph puts 94 of 167 `async def` functions on a path that reaches one.

This measures the consequence instead of describing it: N requests that each wait
D seconds in the database layer. On a loop that is genuinely concurrent they
overlap and finish in about D. On a blocked loop they serialise and take N*D.

The assertion is written the way the service is *supposed* to behave, and marked
xfail because today it does not. `xfail_strict` is on, so the day the async SDK
migration lands this test turns red as an XPASS and someone has to delete the
marker -- which is the point. It is the acceptance criterion for that work, not
a description of the bug.
"""

from __future__ import annotations

import time

import anyio
import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = [pytest.mark.anyio, pytest.mark.slow]

# Long enough that serialisation is unmistakable against scheduling noise, short
# enough that the test costs well under a second either way.
DELAY_SECONDS = 0.05
CONCURRENCY = 10

# A serialised run takes CONCURRENCY * DELAY; a concurrent one takes about DELAY.
# The threshold sits between them with room for a slow CI runner on both sides.
CONCURRENT_BUDGET = DELAY_SECONDS * (CONCURRENCY / 2)


@pytest.fixture
def slow_database(fake_supabase, monkeypatch):
    """Make every database round trip cost DELAY_SECONDS of *blocking* wall clock.

    `time.sleep` rather than `anyio.sleep` on purpose: that is what a synchronous
    driver waiting on a socket does to the loop, and it is the thing being
    measured.
    """
    from tests.fakes import supabase as fake_module

    original = fake_module._SyncQuery.execute

    def slow_execute(self):
        time.sleep(DELAY_SECONDS)
        return original(self)

    monkeypatch.setattr(fake_module._SyncQuery, "execute", slow_execute)
    return fake_supabase


async def _time_concurrent_requests(app, path: str, headers: dict[str, str]) -> float:
    """Wall-clock seconds for CONCURRENCY simultaneous GETs."""
    statuses: list[int] = []

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=headers
    ) as client:

        async def one() -> None:
            statuses.append((await client.get(path)).status_code)

        started = time.perf_counter()
        async with anyio.create_task_group() as tasks:
            for _ in range(CONCURRENCY):
                tasks.start_soon(one)
        elapsed = time.perf_counter() - started

    assert statuses == [200] * CONCURRENCY, f"requests failed: {statuses}"
    return elapsed


@pytest.mark.xfail(
    strict=True,
    reason=(
        "The synchronous Supabase client is called from `async def` handlers with no "
        "offload, so concurrent requests serialise on the event loop. Delete this "
        "marker when the async SDK migration lands -- xfail_strict turns the XPASS "
        "into a failure so it cannot be forgotten."
    ),
)
async def test_concurrent_reads_do_not_serialise(
    app_instance, mock_auth_user, auth_registry, slow_database
):
    """Ten concurrent balance reads should cost one round trip, not ten."""
    slow_database.db.seed(
        "user_credits",
        {
            "user_id": mock_auth_user.id,
            "balance": 1000,
            "lifetime_purchased": 1000,
            "lifetime_used": 0,
        },
    )
    auth_registry[mock_auth_user.id] = mock_auth_user

    elapsed = await _time_concurrent_requests(
        app_instance, "/credits/balance", {"X-Test-User": mock_auth_user.id}
    )

    assert elapsed < CONCURRENT_BUDGET, (
        f"{CONCURRENCY} concurrent requests took {elapsed:.2f}s against a "
        f"{DELAY_SECONDS}s database call -- they ran one after another, so the "
        f"event loop was blocked for the whole of each one."
    )


async def test_the_loop_is_blocked_today(
    app_instance, mock_auth_user, auth_registry, slow_database
):
    """The same measurement, asserted the way the service behaves now.

    Kept alongside the xfail so the file states a fact rather than only an
    aspiration: if this one ever fails, concurrency changed and the test above is
    the one to read.
    """
    slow_database.db.seed(
        "user_credits",
        {
            "user_id": mock_auth_user.id,
            "balance": 1000,
            "lifetime_purchased": 1000,
            "lifetime_used": 0,
        },
    )
    auth_registry[mock_auth_user.id] = mock_auth_user

    elapsed = await _time_concurrent_requests(
        app_instance, "/credits/balance", {"X-Test-User": mock_auth_user.id}
    )

    assert elapsed >= DELAY_SECONDS * (CONCURRENCY - 1), (
        f"{CONCURRENCY} concurrent requests took only {elapsed:.2f}s -- they "
        f"overlapped, so the blocking call is no longer on the event loop and "
        f"`test_concurrent_reads_do_not_serialise` should lose its xfail marker."
    )
