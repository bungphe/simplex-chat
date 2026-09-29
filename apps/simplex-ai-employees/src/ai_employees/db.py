"""A small database layer: SQLite by default, PostgreSQL for several office processes.

    database_url: postgresql://user:pass@db:5432/aie     # in employees.yaml (or DATABASE_URL)

SQL is written once with `?` placeholders and portable syntax (ON CONFLICT upserts);
this module adapts it. Calls are synchronous and short (indexed single-row reads and
writes); PostgreSQL connections are opened lazily, one per process.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")


class IntegrityError(Exception):
    """A UNIQUE constraint was violated (a duplicate row)."""


class Database:
    def __init__(self, url: str):
        self.url = url
        self.postgres = url.startswith(("postgres://", "postgresql://"))
        self._lock = threading.RLock()
        self.closed = False
        self._depth = 0  # open transaction() blocks
        if self.postgres:
            import psycopg  # optional dependency: pip install "psycopg[binary]"

            self._pg = psycopg
            self._conn = psycopg.connect(url, autocommit=True)
        else:
            path = url.removeprefix("sqlite:///") if url.startswith("sqlite:") else url
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            # customer data, API keys, tokens and password hashes: owner-only (WAL files follow)
            os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
            os.chmod(path, 0o600)
            self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")

    # PostgreSQL spells some things differently; schemas use these tokens.
    def ddl(self, sql: str) -> str:
        if self.postgres:
            sql = sql.replace("{id}", "BIGSERIAL PRIMARY KEY").replace("{int}", "BIGINT")
            sql = sql.replace("{real}", "DOUBLE PRECISION")  # PostgreSQL's REAL is only 4 bytes
            sql = sql.replace("{blob}", "BYTEA")
        else:
            sql = sql.replace("{id}", "INTEGER PRIMARY KEY").replace("{int}", "INTEGER")
            sql = sql.replace("{real}", "REAL").replace("{blob}", "BLOB")
        return sql

    def _call(self, run: Callable[[], T]) -> T:
        """Run against the connection. PostgreSQL may restart (or drop an idle connection):
        outside a transaction a lost connection is reopened and the statement run once more;
        inside one the error is raised (the transaction is gone) and the next statement reconnects."""
        with self._lock:
            if not self.postgres:
                return run()
            if self._depth == 0 and self._conn.closed and not self.closed:
                self._reconnect()
            try:
                return run()
            except self._pg.OperationalError:
                if self._depth or self.closed or not self._conn.closed:
                    raise  # a transaction was lost, or the server refused the statement itself
                log.warning("database: connection lost; reconnecting")
                self._reconnect()
                return run()

    def _reconnect(self) -> None:
        with suppress(self._pg.Error, OSError):
            self._conn.close()
        self._conn = self._pg.connect(self.url, autocommit=True)

    def script(self, sql: str) -> None:
        def run() -> None:
            for statement in (s.strip() for s in self.ddl(sql).split(";")):
                if statement:
                    self._conn.execute(statement)

        self._call(run)

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.postgres else sql

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int | None:
        """Run a statement; returns the new row id for `INSERT ... RETURNING id`, if any."""

        def run() -> int | None:
            cur = self._conn.execute(self._sql(sql), tuple(params))
            if "RETURNING" in sql.upper():
                row = cur.fetchone()
                return int(row[0]) if row else None
            return None

        try:
            return self._call(run)
        except sqlite3.IntegrityError as e:
            raise IntegrityError(str(e)) from e
        except Exception as e:
            if self.postgres and isinstance(e, self._pg.errors.UniqueViolation):
                raise IntegrityError(str(e)) from e
            raise

    def rows(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        def run() -> list[dict[str, Any]]:
            cur = self._conn.execute(self._sql(sql), tuple(params))
            if self.postgres:
                names = [d.name for d in cur.description or []]
                return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]
            return [dict(r) for r in cur.fetchall()]

        return self._call(run)

    def row(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        found = self.rows(sql, params)
        return found[0] if found else None

    def many(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        params = [tuple(p) for p in seq]

        def run() -> None:
            if self.postgres:
                with self._conn.cursor() as cur:
                    cur.executemany(self._sql(sql), params)
            else:
                self._conn.executemany(sql, params)

        self._call(run)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Statements inside run all-or-nothing (and no other thread interleaves)."""
        with self._lock:
            if self.postgres:
                if self._depth == 0 and self._conn.closed and not self.closed:
                    self._reconnect()
                self._depth += 1
                try:
                    with self._conn.transaction():
                        yield
                finally:
                    self._depth -= 1
            else:
                self._conn.execute("BEGIN")
                try:
                    yield
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
                self._conn.execute("COMMIT")

    def add_columns(self, table: str, columns: dict[str, str]) -> None:
        """Columns added after a table was first created ({int} in the DDL works here too)."""
        if self.postgres:
            for name, ddl in columns.items():
                self.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {self.ddl(ddl)}")
            return
        have = {r["name"] for r in self.rows(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in have:
                self.execute(f"ALTER TABLE {table} ADD COLUMN {name} {self.ddl(ddl)}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
            self.closed = True


_shared: dict[str, Database] = {}


def connect(url: str) -> Database:
    """PostgreSQL: one shared connection per process. SQLite: a connection per caller."""
    if not url.startswith(("postgres://", "postgresql://")):
        return Database(url)
    if url not in _shared or _shared[url].closed:
        _shared[url] = Database(url)
    return _shared[url]


class DocStore:
    """Small JSON documents (office settings, rotating tokens), safe with several processes:
    every change re-reads the latest version and is retried if another process wrote first."""

    def __init__(self, db: Database):
        import json
        import time

        self.db, self._json, self._time = db, json, time
        db.script(
            "CREATE TABLE IF NOT EXISTS state_docs (key TEXT PRIMARY KEY, data TEXT NOT NULL, version {int} NOT NULL DEFAULT 0)"
        )
        self._cache: dict[str, tuple[float, int, Any]] = {}
        self.cache_seconds = 1.0 if db.postgres else float("inf")

    def get(self, key: str, default: Any = None) -> Any:
        cached = self._cache.get(key)
        if cached and self._time.monotonic() - cached[0] <= self.cache_seconds:
            return cached[2]
        row = self.db.row("SELECT data, version FROM state_docs WHERE key=?", (key,))
        value = self._json.loads(row["data"]) if row else default
        self._cache[key] = (self._time.monotonic(), int(row["version"]) if row else -1, value)
        return value

    def update(self, key: str, change: Any, default: Any) -> Any:
        """`change(doc)` edits a fresh copy in place and may return a result."""
        for _ in range(20):
            self._cache.pop(key, None)
            current = self.get(key, None)
            version = self._cache[key][1]
            doc = self._json.loads(self._json.dumps(current if current is not None else default))
            result = change(doc)
            data = self._json.dumps(doc, ensure_ascii=False)
            if version < 0:
                try:
                    self.db.execute(
                        "INSERT INTO state_docs (key, data, version) VALUES (?, ?, 1)", (key, data)
                    )
                    ok: int | None = 1
                except IntegrityError:
                    ok = None
            else:
                ok = self.db.execute(
                    "UPDATE state_docs SET data=?, version=version+1 WHERE key=? AND version=? RETURNING version",
                    (data, key, version),
                )
            if ok is not None:
                self._cache[key] = (self._time.monotonic(), ok, doc)
                return result
        raise RuntimeError(f"{key} keeps changing; try again")
