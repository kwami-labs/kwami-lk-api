"""A PostgREST-shaped client over a raw Postgres connection.

`tests/fakes/supabase.py` claims to behave like PostgREST. Nothing checked that
claim, so this is the other half of the comparison: the same builder surface,
backed by real SQL, so a scenario can be run against both and the results
compared.

It implements the subset of the builder the fake implements -- no more, because
an operation neither side supports is not a divergence anyone can hit -- and it
translates each call the way PostgREST does:

* `.range(a, b)` is inclusive at both ends, so it is `LIMIT b - a + 1 OFFSET a`.
* `.single()` demands exactly one row and raises PGRST116 otherwise;
  `.maybe_single()` allows zero (returning ``None``) but not many.
* `col->>key` reads a JSON member as text.
* `like`/`ilike` patterns are passed through to SQL, where `%` is the wildcard.
* A unique violation surfaces as an `APIError` carrying SQLSTATE 23505.
"""

from __future__ import annotations

from typing import Any

from postgrest.exceptions import APIError

_OPERATORS = {
    "eq": "=",
    "neq": "<>",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "like": "LIKE",
    "ilike": "ILIKE",
}


def _column_sql(column: str) -> str:
    """Render a column reference, honouring PostgREST's `col->>key` accessor."""
    if "->>" in column:
        base, _, key = column.partition("->>")
        return f'"{base.strip()}" ->> %(jsonkey_{key.strip()})s'
    return f'"{column}"'


class PgResult:
    def __init__(self, data: Any, count: int | None = None) -> None:
        self.data = data
        self.count = count


class PgQuery:
    """Mirrors `tests.fakes.supabase._Query`, executing SQL instead."""

    def __init__(self, conn, table: str) -> None:
        self._conn = conn
        self._table = table
        self._columns = "*"
        self._filters: list[tuple[str, str, Any]] = []
        self._or_filters: list[list[tuple[str, str, Any]]] = []
        self._order: tuple[str, bool] | None = None
        self._range: tuple[int, int] | None = None
        self._limit: int | None = None
        self._insert: Any = None
        self._upsert: Any = None
        self._on_conflict: str | None = None
        self._update: dict[str, Any] | None = None
        self._delete = False
        self._single = False
        self._maybe_single = False
        self._wants_count = False

    # -- builder ------------------------------------------------------------

    def select(self, *args: Any, **kwargs: Any) -> PgQuery:
        if args and args[0] and args[0] != "*":
            self._columns = ", ".join(f'"{c.strip()}"' for c in str(args[0]).split(","))
        self._wants_count = kwargs.get("count") is not None
        return self

    def _filter(self, op: str, column: str, value: Any) -> PgQuery:
        self._filters.append((op, column, value))
        return self

    def eq(self, column: str, value: Any) -> PgQuery:
        return self._filter("eq", column, value)

    def neq(self, column: str, value: Any) -> PgQuery:
        return self._filter("neq", column, value)

    def gt(self, column: str, value: Any) -> PgQuery:
        return self._filter("gt", column, value)

    def gte(self, column: str, value: Any) -> PgQuery:
        return self._filter("gte", column, value)

    def lt(self, column: str, value: Any) -> PgQuery:
        return self._filter("lt", column, value)

    def lte(self, column: str, value: Any) -> PgQuery:
        return self._filter("lte", column, value)

    def is_(self, column: str, value: Any) -> PgQuery:
        return self._filter("is", column, value)

    def in_(self, column: str, values: Any) -> PgQuery:
        return self._filter("in", column, list(values))

    def like(self, column: str, pattern: str) -> PgQuery:
        return self._filter("like", column, pattern)

    def ilike(self, column: str, pattern: str) -> PgQuery:
        return self._filter("ilike", column, pattern)

    def or_(self, expression: str) -> PgQuery:
        clauses = []
        for clause in expression.split(","):
            column, _, rest = clause.partition(".")
            op, _, value = rest.partition(".")
            clauses.append((op, column, value))
        self._or_filters.append(clauses)
        return self

    def order(self, column: str | None = None, *, desc: bool = False, **_kwargs: Any) -> PgQuery:
        if column:
            self._order = (column, desc)
        return self

    def range(self, start: int, end: int) -> PgQuery:
        self._range = (start, end)
        return self

    def limit(self, count: int) -> PgQuery:
        self._limit = count
        return self

    def insert(self, payload: Any) -> PgQuery:
        self._insert = payload
        return self

    def upsert(self, payload: Any, *, on_conflict: str | None = None, **_kw: Any) -> PgQuery:
        self._upsert = payload
        self._on_conflict = on_conflict
        return self

    def update(self, payload: dict[str, Any]) -> PgQuery:
        self._update = payload
        return self

    def delete(self) -> PgQuery:
        self._delete = True
        return self

    def single(self) -> PgQuery:
        self._single = True
        return self

    def maybe_single(self) -> PgQuery:
        self._maybe_single = True
        return self

    # -- execution ----------------------------------------------------------

    def _where(self) -> tuple[str, dict[str, Any]]:
        params: dict[str, Any] = {}
        parts: list[str] = []
        for i, (op, column, value) in enumerate(self._filters):
            parts.append(self._predicate(op, column, value, f"f{i}", params))
        for j, clauses in enumerate(self._or_filters):
            ors = [
                self._predicate(op, column, value, f"o{j}_{k}", params)
                for k, (op, column, value) in enumerate(clauses)
            ]
            parts.append("(" + " OR ".join(ors) + ")")
        return (" WHERE " + " AND ".join(parts)) if parts else "", params

    def _predicate(self, op: str, column: str, value: Any, name: str, params: dict) -> str:
        col = _column_sql(column)
        if "->>" in column:
            params[f"jsonkey_{column.partition('->>')[2].strip()}"] = column.partition(">>")[2]
        if op == "is":
            if value is None:
                return f"{col} IS NULL"
            return f"{col} IS {'TRUE' if value else 'FALSE'}"
        if op == "in":
            # `= ANY(array)` rather than `IN (...)`: psycopg3 binds a list as a
            # single array parameter and does not expand a tuple into a value list,
            # so `IN %(x)s` is a syntax error. ANY also handles the empty list.
            params[name] = list(value)
            return f"{col} = ANY(%({name})s)"
        params[name] = value
        # `->>` yields text, so compare as text on both sides.
        cast = "::text" if "->>" in column else ""
        return f"{col} {_OPERATORS[op]} %({name})s{cast}"

    def execute(self) -> PgResult:
        if self._insert is not None:
            return self._run_write("insert")
        if self._upsert is not None:
            return self._run_write("upsert")
        if self._update is not None:
            return self._run_update()
        if self._delete:
            return self._run_delete()
        return self._run_select()

    def _rows(self, cur) -> list[dict[str, Any]]:
        names = [d.name for d in cur.description] if cur.description else []
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]

    def _shape(self, rows: list[dict[str, Any]], count: int | None = None) -> PgResult:
        if self._single or self._maybe_single:
            if len(rows) > 1 or (not rows and self._single):
                raise APIError(
                    {
                        "message": "JSON object requested, multiple (or no) rows returned",
                        "code": "PGRST116",
                        "hint": None,
                        "details": f"Results contain {len(rows)} rows",
                    }
                )
            return PgResult(rows[0] if rows else None, count)
        return PgResult(rows, count)

    def _run_select(self) -> PgResult:
        where, params = self._where()
        total = None
        if self._wants_count:
            with self._conn.cursor() as cur:
                cur.execute(f'SELECT count(*) FROM "{self._table}"{where}', params)
                total = cur.fetchone()[0]
        sql = f'SELECT {self._columns} FROM "{self._table}"{where}'
        if self._order:
            column, desc = self._order
            sql += f" ORDER BY {_column_sql(column)} {'DESC' if desc else 'ASC'}"
        if self._range:
            start, end = self._range
            sql += f" LIMIT {end - start + 1} OFFSET {start}"
        elif self._limit is not None:
            sql += f" LIMIT {self._limit}"
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return self._shape(self._rows(cur), total)

    def _run_write(self, mode: str) -> PgResult:
        payloads = self._insert if mode == "insert" else self._upsert
        payloads = payloads if isinstance(payloads, list) else [payloads]
        out: list[dict[str, Any]] = []
        for payload in payloads:
            columns = list(payload)
            placeholders = ", ".join(f"%({c})s" for c in columns)
            collist = ", ".join(f'"{c}"' for c in columns)
            sql = f'INSERT INTO "{self._table}" ({collist}) VALUES ({placeholders})'
            if mode == "upsert":
                conflict = self._on_conflict or ""
                targets = ", ".join(f'"{c.strip()}"' for c in conflict.split(",") if c.strip())
                if targets:
                    assignments = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in columns)
                    sql += f" ON CONFLICT ({targets}) DO UPDATE SET {assignments}"
            sql += " RETURNING *"
            try:
                with self._conn.cursor() as cur:
                    cur.execute(sql, payload)
                    out.extend(self._rows(cur))
            except Exception as exc:  # psycopg raises its own class per SQLSTATE
                raise _as_api_error(exc) from exc
        return self._shape(out)

    def _run_update(self) -> PgResult:
        where, params = self._where()
        assignments = ", ".join(f'"{c}" = %(set_{c})s' for c in self._update)
        params.update({f"set_{c}": v for c, v in self._update.items()})
        sql = f'UPDATE "{self._table}" SET {assignments}{where} RETURNING *'
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                return self._shape(self._rows(cur))
        except Exception as exc:
            raise _as_api_error(exc) from exc

    def _run_delete(self) -> PgResult:
        where, params = self._where()
        with self._conn.cursor() as cur:
            cur.execute(f'DELETE FROM "{self._table}"{where} RETURNING *', params)
            return self._shape(self._rows(cur))


def _as_api_error(exc: Exception) -> Exception:
    """Translate a psycopg error into the APIError shape PostgREST returns."""
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate is None:
        return exc
    return APIError({"message": str(exc), "code": sqlstate, "hint": None, "details": None})


class PgClient:
    """Mirrors `tests.fakes.supabase.FakeSupabase`, minus the RPC dispatch."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def table(self, name: str) -> PgQuery:
        return PgQuery(self._conn, name)

    def from_(self, name: str) -> PgQuery:
        return PgQuery(self._conn, name)
