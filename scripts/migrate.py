#!/usr/bin/env python
"""Apply pending SQL migrations and record them in ``schema_migrations``.

Usage
-----
    uv run python scripts/migrate.py                 # apply everything pending
    uv run python scripts/migrate.py --dry-run       # list what would run
    uv run python scripts/migrate.py --baseline 010  # mark 000-010 applied, run nothing
    uv run python scripts/migrate.py --verify        # checksum applied files

Connection comes from ``DATABASE_URL``.

Files run in numeric-prefix order inside a transaction, so a failure rolls back
rather than leaving the schema half-migrated. A file whose first line is
``-- +no-transaction`` runs with autocommit instead: ``ALTER TYPE ... ADD VALUE``
cannot execute inside a transaction block, which the Supabase SQL editor hides
because it autocommits each statement.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
VERSION_RE = re.compile(r"^(\d+)_")
NO_TRANSACTION = "-- +no-transaction"


class MigrationError(RuntimeError):
    pass


def discover() -> list[tuple[str, Path]]:
    """Return (version, path) sorted numerically, rejecting duplicate prefixes."""
    found: dict[str, Path] = {}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = VERSION_RE.match(path.name)
        if not match:
            raise MigrationError(f"{path.name} has no numeric prefix")
        version = match.group(1)
        if version in found:
            raise MigrationError(
                f"Duplicate migration version {version}: {found[version].name} and {path.name}. "
                "Apply order would be ambiguous; renumber one of them."
            )
        found[version] = path
    return sorted(found.items(), key=lambda kv: int(kv[0]))


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def connect(dsn: str):
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - depends on the integration extra
        raise MigrationError(
            "psycopg is required. Install it with: uv sync --extra integration"
        ) from exc
    return psycopg.connect(dsn, autocommit=True)


def applied_versions(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.schema_migrations')")
        if cur.fetchone()[0] is None:
            return {}
        cur.execute("SELECT version, checksum FROM schema_migrations")
        return {row[0]: row[1] for row in cur.fetchall()}


def record(conn, version: str, path: Path) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s) "
            "ON CONFLICT (version) DO NOTHING",
            (version, path.name, checksum(path)),
        )


def apply_one(conn, version: str, path: Path) -> None:
    sql = path.read_text()
    transactional = not sql.lstrip().startswith(NO_TRANSACTION)
    if transactional:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(sql)
            record(conn, version, path)
    else:
        # Autocommit: the statement itself cannot be wrapped. If it fails partway
        # the registry row is not written, so a rerun retries it -- which is why
        # these files must use IF NOT EXISTS.
        with conn.cursor() as cur:
            cur.execute(sql)
        record(conn, version, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--baseline",
        metavar="VERSION",
        help="Mark every migration up to VERSION as applied without running it. "
        "Use once on a database that was migrated by hand.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Fail if an already-applied migration file has been edited since.",
    )
    args = parser.parse_args()

    try:
        migrations = discover()
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run and not args.dsn:
        for version, path in migrations:
            print(f"  {version}  {path.name}")
        return 0

    if not args.dsn:
        print("error: set DATABASE_URL or pass --dsn", file=sys.stderr)
        return 2

    with connect(args.dsn) as conn:
        # The registry has to exist before anything can be recorded.
        registry = next((p for v, p in migrations if v == "000"), None)
        if registry is not None:
            with conn.cursor() as cur:
                cur.execute(registry.read_text())

        done = applied_versions(conn)

        if args.verify:
            drift = [
                path.name
                for version, path in migrations
                if version in done and done[version] != checksum(path)
            ]
            if drift:
                print(
                    "error: applied migrations have been edited: " + ", ".join(drift),
                    file=sys.stderr,
                )
                return 1
            print(f"ok: {len(done)} applied migrations match their recorded checksums")
            return 0

        if args.baseline:
            cutoff = int(args.baseline)
            stamped = 0
            for version, path in migrations:
                if int(version) <= cutoff and version not in done:
                    record(conn, version, path)
                    stamped += 1
                    print(f"stamped  {version}  {path.name}")
            print(f"baseline complete: {stamped} migration(s) marked applied, none run")
            return 0

        pending = [(v, p) for v, p in migrations if v not in done]
        if not pending:
            print("nothing to do: schema is up to date")
            return 0

        for version, path in pending:
            if args.dry_run:
                print(f"would apply  {version}  {path.name}")
                continue
            print(f"applying  {version}  {path.name}", flush=True)
            apply_one(conn, version, path)
        print(f"{'would apply' if args.dry_run else 'applied'} {len(pending)} migration(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
