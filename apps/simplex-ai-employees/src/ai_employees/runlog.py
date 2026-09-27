"""The run log: one JSON line per unit of work, append only.

Every reply, colleague consultation, routine run and outbound action is recorded,
so the admin UI, the office report and a supervisor employee read one source of
truth instead of each keeping their own counts.
"""

from __future__ import annotations

import json
import os
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .state import now_iso

# Closed vocabularies, so counts and filters never meet a surprise value.
KINDS = ("reply", "consult", "routine", "action", "suggest", "memory")
STATUSES = ("ok", "busy", "refused", "step_limit", "error", "queued", "rejected", "skipped")


class RunLog:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    def append(self, *, employee: str, kind: str, status: str, **fields: Any) -> dict[str, Any]:
        assert kind in KINDS and status in STATUSES, (kind, status)
        record = {"ts": now_iso(), "employee": employee, "kind": kind, "status": status, **fields}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def tail(
        self,
        limit: int = 200,
        employee: str | None = None,
        kind: str | None = None,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """The most recent matching records, oldest first. Malformed lines are skipped."""
        if not self.path.exists():
            return []
        out: deque[dict[str, Any]] = deque(maxlen=limit)
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if employee and r.get("employee") != employee:
                    continue
                if kind and r.get("kind") != kind:
                    continue
                if since and datetime.fromisoformat(r["ts"]) < since:
                    continue
                out.append(r)
        return list(out)

    def summary(self, hours: float = 24) -> dict[str, dict[str, Any]]:
        """Per employee: counts by kind and status, tokens, and the last activity."""
        since = datetime.now().astimezone() - timedelta(hours=hours)
        stats: dict[str, dict[str, Any]] = {}
        for r in self.tail(limit=100_000, since=since):
            s = stats.setdefault(
                r["employee"],
                {"total": 0, "by_kind": {}, "by_status": {}, "tokens_in": 0, "tokens_out": 0, "last": None},
            )
            s["total"] += 1
            s["by_kind"][r["kind"]] = s["by_kind"].get(r["kind"], 0) + 1
            s["by_status"][r["status"]] = s["by_status"].get(r["status"], 0) + 1
            s["tokens_in"] += r.get("tokens_in", 0)
            s["tokens_out"] += r.get("tokens_out", 0)
            s["last"] = r["ts"]
        return stats
