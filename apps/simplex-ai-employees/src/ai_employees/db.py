"""A small database layer: SQLite by default, PostgreSQL for several office processes.

    database_url: postgresql://user:pass@db:5432/aie     # in employees.yaml (or DATABASE_URL)

SQL is written once with `?` placeholders and portable syntax (ON CONFLICT upserts);
this module adapts it. Calls are synchronous and short (indexed single-row reads and
writes); PostgreSQL connections are opened lazily, one per process.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class IntegrityError(Exception):
    """A UNIQUE constraint was violated (a duplicate row)."""


class Database:
    def __init__(self, url: str):
        self.url = url
        self.postgres = url.startswith(("postgres://", "postgresql://"))
        self._lock = threading.RLock()
        self.closed = False
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
        else:
            sql = sql.replace("{id}", "INTEGER PRIMARY KEY").replace("{int}", "INTEGER")
        return sql

    def script(self, sql: str) -> None:
        with self._lock:
            for statement in (s.strip() for s in self.ddl(sql).split(";")):
                if statement:
                    self._conn.execute(statement)

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.postgres else sql

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int | None:
        """Run a statement; returns the new row id for `INSERT ... RETURNING id`, if any."""
        with self._lock:
            try:
                cur = self._conn.execute(self._sql(sql), tuple(params))
            except sqlite3.IntegrityError as e:
                raise IntegrityError(str(e)) from e
            except Exception as e:
                if self.postgres and isinstance(e, self._pg.errors.UniqueViolation):
                    raise IntegrityError(str(e)) from e
                raise
            if "RETURNING" in sql.upper():
                row = cur.fetchone()
                return int(row[0]) if row else None
            return None

    def rows(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(self._sql(sql), tuple(params))
            if self.postgres:
                names = [d.name for d in cur.description or []]
                return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]
            return [dict(r) for r in cur.fetchall()]

    def row(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        found = self.rows(sql, params)
        return found[0] if found else None

    def many(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            if self.postgres:
                with self._conn.cursor() as cur:
                    cur.executemany(self._sql(sql), [tuple(p) for p in seq])
            else:
                self._conn.executemany(sql, [tuple(p) for p in seq])

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Statements inside run all-or-nothing (and no other thread interleaves)."""
        with self._lock:
            if self.postgres:
                with self._conn.transaction():
                    yield
            else:
                self._conn.execute("BEGIN")
                try:
                    yield
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
                self._conn.execute("COMMIT")

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
