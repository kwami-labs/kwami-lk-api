"""Reading a whole user graph, one Zep page at a time.

Zep answers a node or edge listing with at most 50 records, whatever `limit`
asks for (measured on 2026-11-06: `limit=100` and `limit=500` both returned 50
from a graph holding 84 facts). Every reader in this package used to make a
single call with `limit=1000`, so a memory past 50 facts or 50 entities was
silently cut short: in the panel counts, in the graph view, and in the analysis
passes -- where "has no edge among the first 50" was read as "orphan" and the
node was deleted.

The listing paginates with `uuid_cursor`, the uuid of the last record of the
previous page. A walk stops on an empty page rather than on a short one, so a
future change to Zep's page size cannot truncate a graph again; that costs one
extra call at the end of each walk.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

#: What Zep returns per call at most. Asking for more is harmless and ignored.
ZEP_PAGE_SIZE = 50

#: Ceiling on one walk, so a runaway graph cannot keep a request open forever.
MAX_GRAPH_ITEMS = 10_000

Fetch = Callable[..., Awaitable[Any]]


def uuid_of(record: Any) -> str | None:
    """The uuid of a Zep record, which the SDK exposes as `uuid_`."""
    return getattr(record, "uuid_", None) or getattr(record, "uuid", None)


async def walk(
    fetch: Fetch,
    user_id: str,
    *,
    cursor: str | None = None,
    max_items: int = MAX_GRAPH_ITEMS,
    **options: Any,
) -> AsyncIterator[Any]:
    """Yield every record `fetch` lists for `user_id`, following the cursor.

    `options` go to `fetch` unchanged (`filters`, `order_by`, `direction`).
    """
    yielded: set[str] = set()
    count = 0
    while True:
        kwargs: dict[str, Any] = {"user_id": user_id, "limit": ZEP_PAGE_SIZE, **options}
        if cursor:
            kwargs["uuid_cursor"] = cursor
        batch = list(await fetch(**kwargs) or [])
        fresh = False
        for record in batch:
            uuid = uuid_of(record)
            # A listing that ignored the cursor would hand back the same page
            # forever; a record already yielded is how that shows.
            if uuid is not None and uuid in yielded:
                continue
            if uuid is not None:
                yielded.add(uuid)
            fresh = True
            yield record
            count += 1
            if count >= max_items:
                return
        next_cursor = uuid_of(batch[-1]) if batch else None
        if not fresh or not next_cursor:
            return
        cursor = next_cursor


async def collect(
    fetch: Fetch,
    user_id: str,
    *,
    max_items: int = MAX_GRAPH_ITEMS,
    **options: Any,
) -> list[Any]:
    """Every record of a walk, as a list."""
    return [record async for record in walk(fetch, user_id, max_items=max_items, **options)]


async def page(
    fetch: Fetch,
    user_id: str,
    *,
    limit: int,
    cursor: str | None = None,
    offset: int = 0,
    **options: Any,
) -> tuple[list[Any], str | None]:
    """One page of `limit` records, and the cursor that continues after it.

    `cursor` continues a previous page. `offset` is for callers that still count
    from the start: the records before it are walked and dropped, which is what
    an offset costs against a cursor-only listing.

    The returned cursor is None when the walk ended inside this page.
    """
    wanted = offset + limit + 1
    records = [
        record async for record in walk(fetch, user_id, cursor=cursor, max_items=wanted, **options)
    ]
    window = records[offset : offset + limit]
    has_more = len(records) > offset + limit
    next_cursor = uuid_of(window[-1]) if has_more and window else None
    return window, next_cursor
