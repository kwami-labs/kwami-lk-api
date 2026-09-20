"""In-memory stand-in for the Supabase client used by the fast test layers.

Scope and contract
------------------
This fake exists so route- and service-level tests run in-process at unit speed.
It is *not* a substitute for the database: the atomicity guarantees, RLS policies
and constraints live in ``migrations/*.sql`` and are exercised by the integration
suite against a real Postgres.

Two rules keep it honest:

1. Every builder method implemented here is covered by
   ``tests/integration/contracts/test_fake_supabase_conformance.py``, which runs the
   same query against this fake and against a real Postgres reached through
   ``PgClient``, an adapter that issues the SQL PostgREST would. Behaviour the fake
   claims but the database does not is a CI failure. (That file is newer than this
   docstring, which asserted the guarantee for a long time before anything provided
   it -- and the first run found four divergences.)
2. It models what the database actually enforces -- server-generated ids and
   timestamps, unique indexes, GENERATED columns, and the ``credit_transaction_type``
   enum domain -- because those are exactly the things the old hand-rolled fake
   silently ignored.

One documented gap: ordinary column ``DEFAULT`` clauses are **not** modelled. A test
that inserts a partial row and reads a defaulted column back gets ``None`` here and a
value from Postgres; it belongs in the integration lane. The gap is pinned by a
strict xfail in the conformance file.

The query builder core is shared; only ``execute()`` differs between the sync and
async variants, and the conformance file runs every scenario through both.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from postgrest.exceptions import APIError

# Mirrors migrations/001_credits_system.sql:10. Deliberately not extended:
# passing a value outside this set must fail here exactly as it does in Postgres.
CREDIT_TRANSACTION_TYPES = frozenset({"purchase", "usage", "bonus", "refund"})

# Tables whose primary key is `id uuid DEFAULT gen_random_uuid()`.
_TABLES_WITH_UUID_PK = frozenset(
    {
        "credit_transactions",
        "credit_usage_logs",
        "user_kwamis",
        "kwami_channels",
        "kwami_contacts",
        "kwami_conversations",
        "kwami_call_events",
        "kwami_message_events",
        "kwami_email_accounts",
        "kwami_email_messages",
        "kwami_calendar_events",
        "kwami_wallets",
        "wallet_funding_intents",
        "wallet_funding_events",
        "wallet_transactions",
        "wallet_token_allowlist",
        "wallet_balances_cache",
        "provider_usage_imports",
        "provider_usage_lines",
        "provider_reconciliation_runs",
        "provider_reconciliation_findings",
        "user_app_settings",
        "payment_events",
        "usage_reports",
        "livekit_sessions",
        "browser_contexts",
    }
)

_TABLES_WITH_CREATED_AT = _TABLES_WITH_UUID_PK | {"user_credits"}
_TABLES_WITH_UPDATED_AT = frozenset(
    {
        "user_credits",
        "user_kwamis",
        "kwami_channels",
        "kwami_contacts",
        "kwami_conversations",
        "kwami_email_accounts",
        "kwami_calendar_events",
        "kwami_wallets",
        "wallet_funding_intents",
        "wallet_balances_cache",
        "browser_contexts",
    }
)

# Columns Postgres computes on write, as `GENERATED ALWAYS AS (...) STORED`.
# Keyed by table, then by column, to a callable over the row being written.
#
# Without these the fake hands back a row missing the column entirely, and a
# route that reads it raises KeyError -- a 500 in a test for behaviour that works
# against a real database. `email_address` is the only one today
# (migrations/007_kwami_email.sql:25).
GENERATED_COLUMNS: dict[str, dict[str, Any]] = {
    "kwami_email_accounts": {
        "email_address": lambda row: f"{row.get('username')}@kwami.io",
    },
}


# (table, columns) pairs that carry a UNIQUE index in the migrations.
#
# These are COLUMN names, not index names. Two entries here read `provider_sid`
# for a long time -- the tail of the *index* names `idx_kwami_call_events_provider_sid`
# and `idx_kwami_message_events_provider_sid` -- while the columns are actually
# `provider_call_sid` and `provider_message_sid`. A constraint on a column that does
# not exist never fires, so every duplicate-webhook test passed against a fake that
# accepted the row and a database that would have refused it.
# `test_declared_unique_indexes_exist_in_the_database` now cross-checks this tuple
# against pg_indexes so the two cannot drift again.
DEFAULT_UNIQUE_INDEXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("user_credits", ("user_id",)),
    ("kwami_channels", ("provider", "kind", "phone_number")),
    ("kwami_call_events", ("provider_call_sid",)),
    ("kwami_message_events", ("provider_message_sid",)),
    ("kwami_email_accounts", ("user_id", "kwami_id")),
    ("kwami_email_accounts", ("username",)),
    ("kwami_email_messages", ("sendgrid_message_id",)),
    ("kwami_wallets", ("kwami_id",)),
    ("kwami_wallets", ("public_key",)),
    ("wallet_funding_intents", ("idempotency_key",)),
    ("wallet_funding_events", ("provider", "provider_event_id")),
    ("wallet_token_allowlist", ("chain", "mint_address")),
    ("payment_events", ("provider", "event_id")),
    ("usage_reports", ("report_key",)),
    ("credit_transactions", ("idempotency_key",)),
    ("livekit_sessions", ("room_name",)),
    ("browser_contexts", ("owner_key", "vendor")),
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def unique_violation(table: str, columns: tuple[str, ...]) -> APIError:
    """The error PostgREST returns for a 23505 unique violation."""
    return APIError(
        {
            "message": f'duplicate key value violates unique constraint "{table}_{"_".join(columns)}_key"',
            "code": "23505",
            "hint": None,
            "details": f"Key ({', '.join(columns)}) already exists.",
        }
    )


def raise_error(message: str, code: str = "P0001") -> None:
    raise APIError({"message": message, "code": code, "hint": None, "details": None})


@dataclass
class FakeResult:
    data: Any
    count: int | None = None


@dataclass
class _Query:
    """Accumulated query state. Shared by the sync and async table wrappers."""

    db: FakeDatabase
    table_name: str
    filters: list[tuple[str, str, Any]] = field(default_factory=list)
    or_filters: list[list[tuple[str, str, Any]]] = field(default_factory=list)
    order_by: tuple[str, bool] | None = None
    range_start: int | None = None
    range_end: int | None = None
    pending_insert: Any = None
    pending_update: dict[str, Any] | None = None
    pending_upsert: Any = None
    upsert_on_conflict: tuple[str, ...] = ()
    delete_mode: bool = False
    wants_count: bool = False
    # Named with a leading underscore: `single` / `maybe_single` are builder
    # methods, and a dataclass field of the same name would take the function
    # object as its default.
    _single: bool = False
    _maybe_single: bool = False
    projection: tuple[str, ...] = ()

    # -- builder surface -------------------------------------------------
    def select(self, *args: Any, **kwargs: Any) -> _Query:
        if kwargs.get("count"):
            self.wants_count = True
        # The column list used to be ignored, so `select("id")` returned whole
        # rows and a test could assert on a column the query never asked for --
        # then pass against a database that does not return it. Caught by
        # tests/integration/contracts/test_fake_supabase_conformance.py.
        if args and args[0] and str(args[0]).strip() != "*":
            self.projection = tuple(c.strip() for c in str(args[0]).split(",") if c.strip())
        return self

    def eq(self, column: str, value: Any) -> _Query:
        self.filters.append(("eq", column, value))
        return self

    def neq(self, column: str, value: Any) -> _Query:
        self.filters.append(("neq", column, value))
        return self

    def gt(self, column: str, value: Any) -> _Query:
        self.filters.append(("gt", column, value))
        return self

    def gte(self, column: str, value: Any) -> _Query:
        self.filters.append(("gte", column, value))
        return self

    def lt(self, column: str, value: Any) -> _Query:
        self.filters.append(("lt", column, value))
        return self

    def lte(self, column: str, value: Any) -> _Query:
        self.filters.append(("lte", column, value))
        return self

    def is_(self, column: str, value: Any) -> _Query:
        self.filters.append(("is", column, value))
        return self

    def in_(self, column: str, values: Any) -> _Query:
        self.filters.append(("in", column, list(values)))
        return self

    def like(self, column: str, pattern: str) -> _Query:
        self.filters.append(("like", column, pattern))
        return self

    def ilike(self, column: str, pattern: str) -> _Query:
        self.filters.append(("ilike", column, pattern))
        return self

    def not_(self, *_args: Any, **_kwargs: Any) -> _Query:  # pragma: no cover
        raise NotImplementedError(
            "FakeSupabase.not_ is unimplemented. Add it here and to the conformance "
            "suite before using it in src/."
        )

    def or_(self, expression: str) -> _Query:
        """PostgREST `or=(a.ilike.x,b.ilike.y)` -- parsed into alternatives."""
        clauses: list[tuple[str, str, Any]] = []
        for part in expression.split(","):
            part = part.strip()
            if not part:
                continue
            column, _, rest = part.partition(".")
            op, _, value = rest.partition(".")
            clauses.append((op, column, value))
        self.or_filters.append(clauses)
        return self

    def order(self, column: str | None = None, *, desc: bool = False, **_kwargs: Any) -> _Query:
        if column:
            self.order_by = (column, desc)
        return self

    def range(self, start: int, end: int) -> _Query:
        self.range_start = start
        self.range_end = end
        return self

    def limit(self, count: int) -> _Query:
        if self.range_start is None:
            self.range_start = 0
            self.range_end = count - 1
        else:
            self.range_end = self.range_start + count - 1
        return self

    def insert(self, payload: Any) -> _Query:
        self.pending_insert = payload
        return self

    def upsert(self, payload: Any, *, on_conflict: str | None = None, **_kwargs: Any) -> _Query:
        self.pending_upsert = payload
        if on_conflict:
            self.upsert_on_conflict = tuple(c.strip() for c in on_conflict.split(","))
        return self

    def update(self, payload: dict[str, Any]) -> _Query:
        self.pending_update = dict(payload)
        return self

    def delete(self) -> _Query:
        self.delete_mode = True
        return self

    def single(self) -> _Query:
        self._single = True
        return self

    def maybe_single(self) -> _Query:
        self._maybe_single = True
        return self

    # -- evaluation ------------------------------------------------------
    @staticmethod
    def _read_column(row: dict[str, Any], column: str) -> Any:
        """Read a column, following PostgREST's `col->>key` json accessor."""
        if "->>" in column:
            base, _, key = column.partition("->>")
            container = row.get(base.strip())
            if not isinstance(container, dict):
                return None
            found = container.get(key.strip())
            # `->>` yields text in Postgres, so compare as text.
            return None if found is None else str(found)
        return row.get(column)

    @classmethod
    def _matches(cls, row: dict[str, Any], op: str, column: str, value: Any) -> bool:
        current = cls._read_column(row, column)
        if op == "eq":
            return str(current) == str(value) if isinstance(value, str) else current == value
        if op == "neq":
            return current != value
        if op == "is":
            return current is value or current == value
        if op == "in":
            return current in value
        if current is None:
            return False
        if op == "gt":
            return current > value
        if op == "gte":
            return current >= value
        if op == "lt":
            return current < value
        if op == "lte":
            return current <= value
        if op in ("like", "ilike"):
            needle = str(value).replace("%", "")
            haystack = str(current)
            if op == "ilike":
                needle, haystack = needle.lower(), haystack.lower()
            return needle in haystack
        raise NotImplementedError(f"FakeSupabase filter {op!r} is not implemented")

    def _apply_filters(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = list(rows)
        for op, column, value in self.filters:
            out = [row for row in out if self._matches(row, op, column, value)]
        for clauses in self.or_filters:
            out = [
                row
                for row in out
                if any(self._matches(row, op, column, value) for op, column, value in clauses)
            ]
        return out

    def _with_defaults(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply the column defaults Postgres would apply on INSERT."""
        row = dict(payload)
        if self.table_name in _TABLES_WITH_UUID_PK:
            row.setdefault("id", str(self.db.next_uuid()))
        if self.table_name in _TABLES_WITH_CREATED_AT:
            row.setdefault("created_at", _now_iso())
        if self.table_name in _TABLES_WITH_UPDATED_AT:
            row.setdefault("updated_at", _now_iso())
        # GENERATED ALWAYS is not a default: Postgres recomputes it on every
        # write and ignores whatever the client sent, so this overwrites rather
        # than setdefault.
        for column, compute in GENERATED_COLUMNS.get(self.table_name, {}).items():
            row[column] = compute(row)
        return row

    def _check_unique(self, row: dict[str, Any], existing: list[dict[str, Any]]) -> None:
        for columns in self.db.unique_indexes_for(self.table_name):
            if not all(row.get(c) is not None for c in columns):
                continue
            key = tuple(row.get(c) for c in columns)
            for other in existing:
                if other is row:
                    continue
                if tuple(other.get(c) for c in columns) == key:
                    raise unique_violation(self.table_name, columns)

    def run(self) -> FakeResult:
        table_rows = self.db.rows(self.table_name)

        if self.pending_insert is not None:
            payloads = (
                self.pending_insert
                if isinstance(self.pending_insert, list)
                else [self.pending_insert]
            )
            inserted = []
            for payload in payloads:
                row = self._with_defaults(payload)
                self._check_unique(row, table_rows)
                table_rows.append(row)
                inserted.append(dict(row))
            return self._shape(inserted)

        if self.pending_upsert is not None:
            payloads = (
                self.pending_upsert
                if isinstance(self.pending_upsert, list)
                else [self.pending_upsert]
            )
            conflict = self.upsert_on_conflict or self._default_conflict()
            result = []
            for payload in payloads:
                match = None
                if conflict:
                    key = tuple(payload.get(c) for c in conflict)
                    match = next(
                        (r for r in table_rows if tuple(r.get(c) for c in conflict) == key),
                        None,
                    )
                if match is not None:
                    match.update(payload)
                    if self.table_name in _TABLES_WITH_UPDATED_AT:
                        match["updated_at"] = _now_iso()
                    result.append(dict(match))
                else:
                    row = self._with_defaults(payload)
                    table_rows.append(row)
                    result.append(dict(row))
            return self._shape(result)

        filtered = self._apply_filters(table_rows)

        if self.pending_update is not None:
            updated = []
            for row in filtered:
                row.update(self.pending_update)
                if self.table_name in _TABLES_WITH_UPDATED_AT:
                    row.setdefault("updated_at", _now_iso())
                self._check_unique(row, table_rows)
                updated.append(dict(row))
            return self._shape(updated)

        if self.delete_mode:
            for row in filtered:
                table_rows.remove(row)
            return self._shape([dict(r) for r in filtered])

        if self.order_by:
            column, desc = self.order_by
            filtered = sorted(
                filtered,
                key=lambda r: (r.get(column) is None, r.get(column)),
                reverse=desc,
            )

        total = len(filtered)
        if self.range_start is not None and self.range_end is not None:
            filtered = filtered[self.range_start : self.range_end + 1]

        return self._shape([dict(r) for r in filtered], count=total if self.wants_count else None)

    def _default_conflict(self) -> tuple[str, ...]:
        for table, columns in DEFAULT_UNIQUE_INDEXES:
            if table == self.table_name:
                return columns
        return ()

    def _project(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return only the selected columns, as PostgREST does."""
        if not self.projection:
            return rows
        # A `col->>key` accessor or an embedded resource is not a plain column;
        # leaving such a row whole is closer than dropping everything.
        if any("->" in c or "(" in c for c in self.projection):
            return rows
        return [{c: row[c] for c in self.projection if c in row} for row in rows]

    def _shape(self, rows: list[dict[str, Any]], count: int | None = None) -> FakeResult:
        rows = self._project(rows)
        if self._single or self._maybe_single:
            # PostgREST refuses to collapse many rows into one object: both
            # `.single()` and `.maybe_single()` answer PGRST116. The fake used to
            # return the first row instead, which made a query that is an error
            # against the database look like a successful read of an arbitrary
            # row -- the worst possible direction for this to be wrong in.
            if len(rows) > 1:
                raise_error("JSON object requested, multiple (or no) rows returned", "PGRST116")
            if not rows:
                if self._single:
                    raise_error("JSON object requested, multiple (or no) rows returned", "PGRST116")
                return FakeResult(None, count)
            return FakeResult(rows[0], count)
        return FakeResult(rows, count)


class _SyncQuery(_Query):
    def execute(self) -> FakeResult:
        return self.run()


class _AsyncQuery(_Query):
    async def execute(self) -> FakeResult:
        return self.run()


class _RpcQuery:
    def __init__(self, result: Any) -> None:
        self._result = result

    def execute(self) -> FakeResult:
        return FakeResult(self._result)


class _AsyncRpcQuery(_RpcQuery):
    async def execute(self) -> FakeResult:  # type: ignore[override]
        return FakeResult(self._result)


class FakeDatabase:
    """The rows. Separated from the client so sync and async clients can share one."""

    def __init__(self, seed: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {}
        self._unique: dict[str, list[tuple[str, ...]]] = {}
        for table, columns in DEFAULT_UNIQUE_INDEXES:
            self._unique.setdefault(table, []).append(columns)
        self._uuid_counter = 0
        for table, rows in (seed or {}).items():
            self._tables[table] = [dict(r) for r in rows]

    def rows(self, table: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(table, [])

    def seed(self, table: str, *rows: dict[str, Any]) -> list[dict[str, Any]]:
        """Insert rows directly, applying the same column defaults as an INSERT.

        Seeded rows must be indistinguishable from rows the app wrote, or tests
        pass against fixtures that could not exist in Postgres (a `user_credits`
        row without `updated_at`, say, which `credits.get_balance` indexes
        unconditionally).
        """
        target = self.rows(table)
        added = []
        for row in rows:
            complete = dict(row)
            if table in _TABLES_WITH_UUID_PK:
                complete.setdefault("id", str(self.next_uuid()))
            if table in _TABLES_WITH_CREATED_AT:
                complete.setdefault("created_at", _now_iso())
            if table in _TABLES_WITH_UPDATED_AT:
                complete.setdefault("updated_at", _now_iso())
            target.append(complete)
            added.append(complete)
        return added

    def unique_indexes_for(self, table: str) -> list[tuple[str, ...]]:
        return self._unique.get(table, [])

    def add_unique_index(self, table: str, *columns: str) -> None:
        self._unique.setdefault(table, []).append(tuple(columns))

    def next_uuid(self) -> uuid.UUID:
        """Deterministic ids so assertions and snapshots are stable."""
        self._uuid_counter += 1
        return uuid.uuid5(uuid.NAMESPACE_URL, f"kwami-test-row-{self._uuid_counter}")


class FakeSupabase:
    """Mimics ``supabase.Client`` (and ``AsyncClient`` when ``async_mode=True``)."""

    def __init__(
        self,
        db: FakeDatabase | None = None,
        *,
        async_mode: bool = False,
    ) -> None:
        self.db = db if db is not None else FakeDatabase()
        self.async_mode = async_mode
        self.rpc_calls: list[tuple[str, dict[str, Any]]] = []

    # -- surface used by src/ -------------------------------------------
    def table(self, table_name: str) -> _Query:
        cls = _AsyncQuery if self.async_mode else _SyncQuery
        return cls(db=self.db, table_name=table_name)

    def from_(self, table_name: str) -> _Query:
        return self.table(table_name)

    def rpc(self, name: str, params: dict[str, Any]) -> _RpcQuery:
        self.rpc_calls.append((name, dict(params)))
        result = self._dispatch_rpc(name, params)
        cls = _AsyncRpcQuery if self.async_mode else _RpcQuery
        return cls(result)

    # -- stored procedures ----------------------------------------------
    def _credits_row(self, user_id: str) -> dict[str, Any]:
        rows = self.db.rows("user_credits")
        row = next((r for r in rows if r["user_id"] == user_id), None)
        if row is None:
            row = {
                "user_id": user_id,
                "balance": 0,
                "lifetime_purchased": 0,
                "lifetime_used": 0,
                "updated_at": _now_iso(),
            }
            rows.append(row)
        return row

    def _dispatch_rpc(self, name: str, params: dict[str, Any]) -> Any:
        if name == "add_credits":
            return self._add_credits(params)
        if name == "deduct_credits":
            return self._deduct_credits(params)
        raise NotImplementedError(
            f"FakeSupabase has no implementation for rpc({name!r}). Either add one "
            "here and to the conformance suite, or write the test at the integration layer."
        )

    def _add_credits(self, params: dict[str, Any]) -> int:
        amount = params["p_amount"]
        tx_type = params.get("p_type", "purchase")
        key = params.get("p_idempotency_key")
        # Postgres rejects a value outside the credit_transaction_type enum with 22P02.
        # The previous fake ignored p_type entirely, which is why the wallet-funding
        # enum bug stayed invisible.
        if tx_type not in CREDIT_TRANSACTION_TYPES:
            raise APIError(
                {
                    "message": (
                        f'invalid input value for enum credit_transaction_type: "{tx_type}"'
                    ),
                    "code": "22P02",
                    "hint": None,
                    "details": None,
                }
            )
        if amount is None or amount <= 0:
            raise_error("amount must be positive", "22023")

        # Mirrors the ON CONFLICT claim inside add_credits: repeating a key that
        # is already in the ledger is a no-op returning the current balance.
        if key and any(
            row.get("idempotency_key") == key for row in self.db.rows("credit_transactions")
        ):
            return self._credits_row(params["p_user_id"])["balance"]

        row = self._credits_row(params["p_user_id"])
        row["balance"] += amount
        row["lifetime_purchased"] += amount
        row["updated_at"] = _now_iso()
        self.db.rows("credit_transactions").append(
            {
                "id": str(self.db.next_uuid()),
                "user_id": params["p_user_id"],
                "type": tx_type,
                "amount": amount,
                "balance_after": row["balance"],
                "description": params.get("p_description", ""),
                "metadata": params.get("p_metadata") or {},
                "idempotency_key": key,
                "created_at": _now_iso(),
            }
        )
        return row["balance"]

    def _deduct_credits(self, params: dict[str, Any]) -> int:
        amount = params["p_amount"]
        row = self._credits_row(params["p_user_id"])
        # Mirrors `UPDATE ... WHERE balance >= p_amount` + RAISE in
        # migrations/001_credits_system.sql:150-166: on failure nothing is written.
        if row["balance"] < amount:
            raise_error("Insufficient credits")
        row["balance"] -= amount
        row["lifetime_used"] += amount
        row["updated_at"] = _now_iso()
        self.db.rows("credit_transactions").append(
            {
                "id": str(self.db.next_uuid()),
                "user_id": params["p_user_id"],
                "type": "usage",
                "amount": -amount,
                "balance_after": row["balance"],
                "description": params.get("p_description", ""),
                "metadata": params.get("p_metadata") or {},
                "created_at": _now_iso(),
            }
        )
        return row["balance"]
