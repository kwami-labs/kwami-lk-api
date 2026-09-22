"""Shared doubles for the `/memory` route tests.

`memory.py` is a thin layer over the Zep v3 SDK: almost every route is
`verify_user_access` -> one or two `client.graph.*` calls -> a reshaped dict, with
a broad `except` that turns an SDK failure into a 500 (or, for a few, an empty
result when the message contains "404").

`FakeZep` models that surface rather than the SDK's transport. It is deliberately
attribute-shaped, not dict-shaped, because the routes reach for attributes with
`getattr(..., default)` and a dict would silently satisfy none of them.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.api.routes.memory import get_zep_client
from src.core.security import AuthUser

USER_ID = "test-user-id"


def node(
    uuid: str = "n-1",
    name: str = "Node",
    summary: str | None = "A node",
    labels: list[str] | None = None,
    **extra: Any,
) -> SimpleNamespace:
    return SimpleNamespace(
        uuid_=uuid,
        uuid=uuid,
        name=name,
        summary=summary,
        labels=labels if labels is not None else ["Entity"],
        attributes=extra.pop("attributes", {}),
        created_at=extra.pop("created_at", "2026-03-01T00:00:00Z"),
        **extra,
    )


def edge(
    uuid: str = "e-1",
    fact: str | None = "A fact",
    name: str | None = "RELATES_TO",
    source: str = "n-1",
    target: str = "n-2",
    **extra: Any,
) -> SimpleNamespace:
    return SimpleNamespace(
        uuid_=uuid,
        uuid=uuid,
        fact=fact,
        name=name,
        source_node_uuid=source,
        target_node_uuid=target,
        created_at=extra.pop("created_at", "2026-03-01T00:00:00Z"),
        valid_at=extra.pop("valid_at", None),
        invalid_at=extra.pop("invalid_at", None),
        expired_at=extra.pop("expired_at", None),
        attributes=extra.pop("attributes", {}),
        episodes=extra.pop("episodes", []),
        **extra,
    )


def thread(thread_id: str = "t-1", user_id: str | None = USER_ID, **extra: Any):
    return SimpleNamespace(
        thread_id=thread_id,
        uuid_=thread_id,
        user_id=user_id,
        created_at=extra.pop("created_at", None),
        **extra,
    )


class Recorder:
    """An awaitable stand-in that records its calls and replays a scripted result."""

    def __init__(self, result: Any = None):
        self.result = result
        self.calls: list[dict] = []
        self.error: Exception | None = None

    async def __call__(self, *args, **kwargs):
        self.calls.append(kwargs or {"_args": args})
        if self.error is not None:
            raise self.error
        if callable(self.result):
            return self.result(**kwargs)
        return self.result

    @property
    def last(self) -> dict:
        return self.calls[-1]

    @property
    def called(self) -> bool:
        return bool(self.calls)


class FakeZep:
    """Only the surface `memory.py` actually touches."""

    def __init__(self):
        self.graph = SimpleNamespace(
            search=Recorder(SimpleNamespace(edges=[], nodes=[], episodes=[])),
            add=Recorder(SimpleNamespace(uuid_="ep-1")),
            node=SimpleNamespace(
                get_by_user_id=Recorder([]),
                get_edges=Recorder([]),
                get=Recorder(node()),
                delete=Recorder(None),
                update=Recorder(None),
            ),
            edge=SimpleNamespace(
                get_by_user_id=Recorder([]),
                get=Recorder(edge()),
                delete=Recorder(None),
                update=Recorder(None),
            ),
            episode=SimpleNamespace(get_by_user_id=Recorder(SimpleNamespace(episodes=[]))),
            add_fact_triple=Recorder(
                SimpleNamespace(
                    edge_uuid="e-new", source_node_uuid="n-new", target_node_uuid="n-tgt"
                )
            ),
            get_ontology=Recorder(None),
            list_all=Recorder(SimpleNamespace(graphs=[])),
            update=Recorder(None),
            list_custom_instructions=Recorder(SimpleNamespace(instructions=[])),
            add_custom_instructions=Recorder(None),
            delete_custom_instructions=Recorder(None),
            set_ontology=Recorder(None),
            list_entity_types=Recorder(SimpleNamespace(entity_types=[], edge_types=[])),
        )
        self.thread = SimpleNamespace(
            list_all=Recorder(SimpleNamespace(threads=[], total_count=0)),
            get_context=Recorder(SimpleNamespace(context="ctx")),
            delete=Recorder(None),
            get=Recorder(SimpleNamespace(messages=[])),
        )
        self.user = SimpleNamespace(
            delete=Recorder(None),
            get=Recorder(SimpleNamespace(first_name=None, last_name=None, email=None, metadata={})),
            update=Recorder(None),
            add=Recorder(None),
        )


@pytest.fixture
def zep():
    return FakeZep()


@pytest.fixture
def memory_client(app_instance, auth_registry, zep):
    """An authenticated client whose Zep dependency is the fake."""
    from httpx import ASGITransport, AsyncClient

    from tests.conftest import TEST_USER_HEADER

    user = AuthUser(
        {
            "sub": USER_ID,
            "email": "test@example.com",
            "role": "authenticated",
            "aud": "authenticated",
        }
    )
    auth_registry[USER_ID] = user
    app_instance.dependency_overrides[get_zep_client] = lambda: zep

    async def _factory():
        return AsyncClient(
            transport=ASGITransport(app=app_instance),
            base_url="http://test",
            headers={TEST_USER_HEADER: USER_ID},
        )

    return _factory


@pytest.fixture
async def mem(memory_client):
    client = await memory_client()
    async with client as c:
        yield c
