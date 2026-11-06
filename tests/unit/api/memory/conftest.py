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


#: What Zep returns per listing call at most, whatever `limit` asks for.
ZEP_PAGE = 50


def _uuid(record: Any) -> str | None:
    return getattr(record, "uuid_", None) or getattr(record, "uuid", None)


class Ownership:
    """Which uuids the test user's graph holds, for the routes' ownership probes.

    By default the user owns every uuid, so a test about something else is not
    tripped by the check. `foreign` uuids belong to another tenant; `edgeless`
    nodes are the user's but have no edges, so only the node listing finds them.
    """

    def __init__(self):
        self.foreign: set[str] = set()
        self.edgeless: set[str] = set()
        self.probes: list[dict] = []
        self.error: Exception | None = None

    def answer(self, filters: Any) -> list[Any]:
        if self.error is not None:
            raise self.error
        edge_uuids = getattr(filters, "edge_uuids", None) or []
        connected = getattr(filters, "connected_node_uuids", None) or []
        found = [edge(uuid=uuid) for uuid in edge_uuids if uuid not in self.foreign]
        found += [
            edge(uuid=f"e-{uuid}", source=uuid, target="n-user")
            for uuid in connected
            if uuid not in self.foreign and uuid not in self.edgeless
        ]
        return found


class Listing(Recorder):
    """`graph.{node,edge}.get_by_user_id`: Zep's paging over a scripted list.

    Like Zep, a call returns at most 50 records and continues after the record
    named by `uuid_cursor`. A call with `filters` is an ownership probe and is
    answered by `Ownership` instead, recorded apart so tests that count listing
    calls do not see it.
    """

    def __init__(self, ownership: Ownership):
        super().__init__([])
        self.ownership = ownership

    async def __call__(self, *args, **kwargs):
        if kwargs.get("filters") is not None:
            self.ownership.probes.append(kwargs)
            records = self.ownership.answer(kwargs["filters"])
        else:
            records = await super().__call__(*args, **kwargs)
        if not isinstance(records, list):
            return records
        cursor = kwargs.get("uuid_cursor")
        if cursor:
            after = next((i for i, r in enumerate(records) if _uuid(r) == cursor), None)
            records = [] if after is None else records[after + 1 :]
        return records[:ZEP_PAGE]


class FakeZep:
    """Only the surface `memory.py` actually touches."""

    def __init__(self):
        self.ownership = Ownership()
        self.graph = SimpleNamespace(
            search=Recorder(SimpleNamespace(edges=[], nodes=[], episodes=[])),
            add=Recorder(SimpleNamespace(uuid_="ep-1")),
            node=SimpleNamespace(
                get_by_user_id=Listing(self.ownership),
                get_edges=Recorder([]),
                get=Recorder(node()),
                delete=Recorder(None),
                update=Recorder(None),
            ),
            edge=SimpleNamespace(
                get_by_user_id=Listing(self.ownership),
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
