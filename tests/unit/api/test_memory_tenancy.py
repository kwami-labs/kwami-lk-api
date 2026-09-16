"""Memory endpoints must not cross tenants, and must not stop at page one.

Three defects are pinned here:
  * `/memory/debug/{user_id}` returned `all_threads` -- every thread in the Zep
    project, with its owner's id -- to any authenticated caller.
  * Thread ownership was `user_id in str(thread_id)`, a substring test, so a
    caller could read and DELETE another tenant's threads.
  * Listing took a single `list_all` page, so "delete all my memory" silently
    left data behind once a project exceeded one page.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

from src.api.routes.memory import get_zep_client, iter_all_threads, thread_belongs_to


def thread(thread_id: str, user_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(thread_id=thread_id, uuid_=thread_id, user_id=user_id, created_at=None)


# --------------------------------------------------------------------------
# ownership
# --------------------------------------------------------------------------


def test_owner_recorded_on_the_thread_is_authoritative():
    assert thread_belongs_to("t-1", "alice", "alice") is True
    assert thread_belongs_to("alice-thread", "bob", "alice") is False


@pytest.mark.parametrize("thread_id", ["alice", "alice:1", "alice_kwami"])
def test_legacy_threads_match_on_an_anchored_id(thread_id: str):
    """Threads predating the user_id field fall back to a prefix match."""
    assert thread_belongs_to(thread_id, None, "alice") is True


@pytest.mark.parametrize(
    "thread_id",
    [
        "malice",  # contains "alice"
        "bob-alice-thread",  # contains "alice" in the middle
        "xalice",
        "aliceb",  # shares a prefix but is a different namespace
        "",
    ],
)
def test_substring_matches_are_rejected(thread_id: str):
    assert thread_belongs_to(thread_id, None, "alice") is False


# --------------------------------------------------------------------------
# pagination
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_iter_all_threads_follows_every_page():
    pages = {
        1: SimpleNamespace(threads=[thread(f"t{i}") for i in range(100)], total_count=250),
        2: SimpleNamespace(threads=[thread(f"t{i}") for i in range(100, 200)], total_count=250),
        3: SimpleNamespace(threads=[thread(f"t{i}") for i in range(200, 250)], total_count=250),
    }
    client = MagicMock()
    client.thread.list_all = AsyncMock(
        side_effect=lambda page_number, page_size: pages[page_number]
    )

    seen = [t.thread_id async for t in iter_all_threads(client)]

    assert len(seen) == 250, "a single page would have returned only the first 100"
    assert client.thread.list_all.await_count == 3


@pytest.mark.anyio
async def test_iter_all_threads_stops_on_an_empty_page():
    client = MagicMock()
    client.thread.list_all = AsyncMock(return_value=SimpleNamespace(threads=[], total_count=0))

    assert [t async for t in iter_all_threads(client)] == []
    assert client.thread.list_all.await_count == 1


# --------------------------------------------------------------------------
# the endpoint
# --------------------------------------------------------------------------


@pytest.fixture
def zep_with_two_tenants(app_instance):
    """A project holding threads for the caller and for someone else."""
    client = MagicMock()
    client.thread.list_all = AsyncMock(
        return_value=SimpleNamespace(
            threads=[
                thread("t-mine", "test-user-id"),
                thread("t-theirs", "some-other-tenant"),
                thread("t-lookalike", "test-user-id-2"),
            ],
            total_count=3,
        )
    )
    client.thread.get_context = AsyncMock(return_value=SimpleNamespace(context="ctx"))
    client.graph.search = AsyncMock(return_value=SimpleNamespace(edges=[]))
    client.graph.node.get_by_user_id = AsyncMock(return_value=[])
    client.user.get = AsyncMock(return_value=SimpleNamespace(user_id="test-user-id"))
    app_instance.dependency_overrides[get_zep_client] = lambda: client
    return client


@pytest.mark.anyio
async def test_debug_does_not_leak_other_tenants(auth_client: AsyncClient, zep_with_two_tenants):
    response = await auth_client.get("/memory/debug/test-user-id")
    assert response.status_code == 200
    body = response.json()

    # The whole-project dump is gone.
    assert "all_threads" not in body

    returned = {t["thread_id"] for t in body["threads"]}
    assert returned == {"t-mine"}
    assert "t-theirs" not in returned
    # "test-user-id-2" starts with the caller's id but is a different tenant.
    assert "t-lookalike" not in returned


@pytest.mark.anyio
async def test_delete_only_touches_own_threads(auth_client: AsyncClient, zep_with_two_tenants):
    zep_with_two_tenants.thread.delete = AsyncMock()
    zep_with_two_tenants.user.delete = AsyncMock()

    response = await auth_client.delete("/memory/test-user-id")
    assert response.status_code == 200

    deleted = {c.kwargs["thread_id"] for c in zep_with_two_tenants.thread.delete.await_args_list}
    assert deleted == {"t-mine"}, (
        "a substring match would also have deleted another tenant's thread"
    )
