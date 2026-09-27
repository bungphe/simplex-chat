"""The run log: one row per unit of work, append only.

Every reply, colleague consultation, routine run, outbound action, memory summary and
translation is recorded, so the admin UI, the office report and a supervisor employee
read one source of truth instead of each keeping their own counts. Stored in a table
(SQLite by default, the office's PostgreSQL with `database_url`), so reading the last
day stays fast however long the office has been running. An older runlog.jsonl next to
it is imported once.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .db import Database
from .state import now_iso

# Closed vocabularies, so counts and filters never meet a surprise value.
KINDS = ("reply", "consult", "routine", "action", "suggest", "memory", "translate", "summary")
STATUSES = ("ok", "busy", "refused", "step_limit", "error", "queued", "rejected", "skipped")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runlog (
  id {id}, ts TEXT NOT NULL, employee TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
  tokens_in {int} NOT NULL DEFAULT 0, tokens_out {int} NOT NULL DEFAULT 0, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS runlog_by_time ON runlog (ts);
CREATE INDEX IF NOT EXISTS runlog_by_employee ON runlog (employee, kind, id)
"""


class RunLog:
    def __init__(self, path: str | os.PathLike[str], db: Database | None = None):
        self.path = Path(path)  # the legacy JSONL file
        self.db = db or Database(str(self.path.with_suffix(".sqlite")))
        self.db.script(SCHEMA)
        self._import_jsonl()

    def _import_jsonl(self) -> None:
        if not self.path.exists() or self.db.row("SELECT 1 AS x FROM runlog LIMIT 1"):
            return
        rows = []
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    rows.append(self._row(r))
                except (json.JSONDecodeError, KeyError):
                    continue
        self.db.many(
            "INSERT INTO runlog (ts, employee, kind, status, tokens_in, tokens_out, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.path.rename(self.path.with_suffix(".jsonl.imported"))

    @staticmethod
    def _row(r: dict[str, Any]) -> tuple[Any, ...]:
        return (
            r["ts"],
            r["employee"],
            r["kind"],
            r["status"],
            int(r.get("tokens_in", 0)),
            int(r.get("tokens_out", 0)),
            json.dumps(r, ensure_ascii=False),
        )

    def append(self, *, employee: str, kind: str, status: str, **fields: Any) -> dict[str, Any]:
        assert kind in KINDS and status in STATUSES, (kind, status)
        record = {"ts": now_iso(), "employee": employee, "kind": kind, "status": status, **fields}
        self.db.execute(
            "INSERT INTO runlog (ts, employee, kind, status, tokens_in, tokens_out, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
            self._row(record),
        )
        return record

    def tail(
        self,
        limit: int = 200,
        employee: str | None = None,
        kind: str | None = None,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """The most recent matching records, oldest first."""
        sql, args = "SELECT data, ts FROM runlog WHERE 1=1", []
        if employee:
            sql += " AND employee=?"
            args.append(employee)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if since is not None:
            sql += " AND ts>=?"
            args.append(since.astimezone().isoformat(timespec="seconds"))
        rows = self.db.rows(sql + " ORDER BY id DESC LIMIT ?", [*args, limit])
        return [json.loads(r["data"]) for r in reversed(rows)]

    def summary(self, hours: float = 24) -> dict[str, dict[str, Any]]:
        """Per employee: counts by kind and status, tokens, and the last activity."""
        since = (datetime.now().astimezone() - timedelta(hours=hours)).isoformat(timespec="seconds")
        stats: dict[str, dict[str, Any]] = {}
        for r in self.db.rows(
            "SELECT employee, kind, status, COUNT(*) AS n, SUM(tokens_in) AS tin, SUM(tokens_out) AS tout, "
            "MAX(ts) AS last FROM runlog WHERE ts>=? GROUP BY employee, kind, status",
            (since,),
        ):
            s = stats.setdefault(
                r["employee"],
                {"total": 0, "by_kind": {}, "by_status": {}, "tokens_in": 0, "tokens_out": 0, "last": None},
            )
            n = int(r["n"])
            s["total"] += n
            s["by_kind"][r["kind"]] = s["by_kind"].get(r["kind"], 0) + n
            s["by_status"][r["status"]] = s["by_status"].get(r["status"], 0) + n
            s["tokens_in"] += int(r["tin"] or 0)
            s["tokens_out"] += int(r["tout"] or 0)
            s["last"] = max(filter(None, [s["last"], r["last"]]), default=None)
        return stats
