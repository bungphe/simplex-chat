"""Per-employee persistent state, in database tables: SQLite next to where the employee's
JSON file used to be, or the office's PostgreSQL (`database_url`) shared by all processes.

- Per customer (names, recent turns, summaries, notes, languages): rows, so each message
  writes a few rows instead of rewriting everything.
- The approval queue: rows, numbered per employee (#1, #2...).
- Small settings (admin overrides, admins, shared memory, routine runs): one JSON document
  per employee, updated with optimistic locking so several processes never lose a change.

An older <employee>.json is imported once and renamed to .json.imported."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .db import Database

MAX_UNSUMMARIZED = 200  # trimmed turns kept while a summary cannot be made (model down)
MAX_SUMMARY = 2000
MAX_NOTES = 40

SCHEMA = """
CREATE TABLE IF NOT EXISTS mem_contacts (
  employee TEXT NOT NULL, contact {int} NOT NULL, name TEXT NOT NULL,
  PRIMARY KEY (employee, contact));
CREATE TABLE IF NOT EXISTS mem_turns (
  id {id}, employee TEXT NOT NULL, contact {int} NOT NULL, role TEXT NOT NULL,
  content TEXT NOT NULL, ts TEXT NOT NULL, waiting {int} NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS mem_turns_by_contact ON mem_turns (employee, contact, waiting, id);
CREATE INDEX IF NOT EXISTS mem_turns_by_time ON mem_turns (employee, ts);
CREATE TABLE IF NOT EXISTS mem_notes (
  employee TEXT NOT NULL, contact {int} NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
  PRIMARY KEY (employee, contact, key));
CREATE TABLE IF NOT EXISTS mem_summaries (
  employee TEXT NOT NULL, contact {int} NOT NULL, text TEXT NOT NULL, updated TEXT NOT NULL,
  PRIMARY KEY (employee, contact));
CREATE TABLE IF NOT EXISTS state_docs (
  key TEXT PRIMARY KEY, data TEXT NOT NULL, version {int} NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS actions (
  employee TEXT NOT NULL, number {int} NOT NULL, created TEXT NOT NULL, status TEXT NOT NULL,
  data TEXT NOT NULL, PRIMARY KEY (employee, number));
CREATE INDEX IF NOT EXISTS actions_by_status ON actions (employee, status);
CREATE TABLE IF NOT EXISTS mem_languages (
  employee TEXT NOT NULL, contact {int} NOT NULL, lang TEXT NOT NULL, source TEXT NOT NULL,
  country TEXT, PRIMARY KEY (employee, contact))
"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


DOC_DEFAULTS: dict[str, Any] = {
    "overrides": {},
    "admins": [],
    "shared_memory": [],  # lessons for every conversation: {"id", "text", "status", ...}
    "next_memory_id": 1,
    "routines": {},
}


class ConflictError(Exception):
    pass


class EmployeeState:
    def __init__(self, path: str | os.PathLike[str], db: Database | None = None, employee: str | None = None):
        self.path = Path(path)
        self.employee = employee or self.path.stem
        self.db = db or Database(str(self.path.with_suffix(".sqlite")))
        self.db.script(SCHEMA)
        self._key = f"employee:{self.employee}"
        # another process may change the document: re-read it at most this often
        self.cache_seconds = 1.0 if self.db.postgres else float("inf")
        self._doc: dict[str, Any] = {}
        self._version = -1
        self._loaded = 0.0
        self.db.execute(
            "INSERT INTO state_docs (key, data, version) VALUES (?, ?, 0) ON CONFLICT DO NOTHING",
            (self._key, json.dumps(DOC_DEFAULTS)),
        )
        if self.path.exists():
            self._import_json()

    # the small settings document

    @property
    def data(self) -> dict[str, Any]:
        """The settings document (read-only view; change it through the methods below)."""
        if time.monotonic() - self._loaded > self.cache_seconds or self._version < 0:
            self._reload()
        return self._doc

    def _reload(self) -> None:
        row = self.db.row("SELECT data, version FROM state_docs WHERE key=?", (self._key,))
        assert row is not None
        self._doc, self._version = {**DOC_DEFAULTS, **json.loads(row["data"])}, int(row["version"])
        self._loaded = time.monotonic()

    def _update(self, change: Callable[[dict[str, Any]], Any]) -> Any:
        """Apply a change to the freshest document; retried if another process wrote first."""
        for _ in range(20):
            self._reload()
            doc = json.loads(json.dumps(self._doc))  # a private copy to change
            result = change(doc)
            ok = self.db.execute(
                "UPDATE state_docs SET data=?, version=version+1 WHERE key=? AND version=? RETURNING version",
                (json.dumps(doc, ensure_ascii=False), self._key, self._version),
            )
            if ok is not None:
                self._doc, self._version, self._loaded = doc, ok, time.monotonic()
                return result
        raise ConflictError(f"state of {self.employee} keeps changing; try again")

    def _import_json(self) -> None:
        """Data from an older <employee>.json moves into the tables, once."""
        old = json.loads(self.path.read_text(encoding="utf-8"))
        e, db = self.employee, self.db
        with db.transaction():
            db.many(
                "INSERT INTO mem_contacts (employee, contact, name) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                [(e, int(c), n) for c, n in old.get("contacts", {}).items()],
            )
            for part, waiting in (("unsummarized", 1), ("history", 0)):
                db.many(
                    "INSERT INTO mem_turns (employee, contact, role, content, ts, waiting) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (e, int(c), t["role"], t["content"], t.get("ts", ""), waiting)
                        for c, turns in old.get(part, {}).items()
                        for t in turns
                    ],
                )
            db.many(
                "INSERT INTO mem_notes (employee, contact, key, value) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                [(e, int(c), k, v) for c, notes in old.get("notes", {}).items() for k, v in notes.items()],
            )
            db.many(
                "INSERT INTO mem_summaries (employee, contact, text, updated) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                [(e, int(c), x["text"], x.get("updated", "")) for c, x in old.get("summaries", {}).items()],
            )
            db.many(
                "INSERT INTO mem_languages (employee, contact, lang, source, country) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT DO NOTHING",
                [
                    (e, int(c), x["lang"], x["source"], x.get("country"))
                    for c, x in old.get("languages", {}).items()
                ],
            )
            db.many(
                "INSERT INTO actions (employee, number, created, status, data) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT DO NOTHING",
                [
                    (e, a["id"], a["created"], a["status"], json.dumps(a, ensure_ascii=False))
                    for a in old.get("actions", [])
                ],
            )
        doc = {k: old[k] for k in DOC_DEFAULTS if k in old}
        self._update(lambda d: d.update(doc))
        self.path.rename(self.path.with_suffix(".json.imported"))

    # overrides (runtime config set by admins)

    @property
    def overrides(self) -> dict[str, Any]:
        return self.data["overrides"]

    def set_override(self, key: str, value: Any) -> None:
        self._update(lambda d: d["overrides"].__setitem__(key, value))

    def clear_overrides(self) -> None:
        self._update(lambda d: d.__setitem__("overrides", {}))

    # admins (contact IDs in this employee's SimpleX account)

    def is_admin(self, contact_id: int) -> bool:
        return contact_id in self.data["admins"]

    def add_admin(self, contact_id: int) -> None:
        if contact_id not in self.data["admins"]:
            self._update(lambda d: contact_id in d["admins"] or d["admins"].append(contact_id))

    def remove_admin(self, contact_id: int) -> None:
        if contact_id in self.data["admins"]:
            self._update(lambda d: contact_id in d["admins"] and d["admins"].remove(contact_id))

    @property
    def admins(self) -> list[int]:
        return list(self.data["admins"])

    # contacts seen, for names in reports and the admin UI

    def remember_contact(self, contact_id: int, name: str) -> None:
        self.db.execute(
            "INSERT INTO mem_contacts (employee, contact, name) VALUES (?, ?, ?) "
            "ON CONFLICT (employee, contact) DO UPDATE SET name=excluded.name WHERE mem_contacts.name <> excluded.name",
            (self.employee, contact_id, name),
        )

    def contact_name(self, contact_id: int) -> str:
        row = self.db.row(
            "SELECT name FROM mem_contacts WHERE employee=? AND contact=?", (self.employee, contact_id)
        )
        return row["name"] if row else f"#{contact_id}"

    @property
    def contacts(self) -> dict[int, str]:
        rows = self.db.rows("SELECT contact, name FROM mem_contacts WHERE employee=?", (self.employee,))
        return {int(r["contact"]): r["name"] for r in rows}

    def contact_overview(self, limit: int = 300) -> list[dict[str, Any]]:
        """Contacts with their number of remembered exchanges and last activity, newest first."""
        rows = self.db.rows(
            "SELECT c.contact, c.name, COUNT(t.id) AS turns, MAX(t.ts) AS last FROM mem_contacts c "
            "LEFT JOIN mem_turns t ON t.employee=c.employee AND t.contact=c.contact AND t.waiting=0 "
            "WHERE c.employee=? GROUP BY c.contact, c.name ORDER BY last DESC LIMIT ?",
            (self.employee, limit),
        )
        return [
            {"id": int(r["contact"]), "name": r["name"], "turns": int(r["turns"]) // 2, "last": r["last"]}
            for r in rows
        ]

    # conversation memory: plain-text user/assistant turns per contact

    def history(self, contact_id: int) -> list[dict[str, str]]:
        """Turns as model messages (role and content only)."""
        return [{"role": t["role"], "content": t["content"]} for t in self.timed_history(contact_id)]

    def timed_history(self, contact_id: int) -> list[dict[str, str]]:
        """Turns with their timestamps (`ts`, when recorded)."""
        return self.db.rows(
            "SELECT role, content, ts FROM mem_turns WHERE employee=? AND contact=? AND waiting=0 ORDER BY id",
            (self.employee, contact_id),
        )

    def turns_since(self, since: str) -> dict[int, list[dict[str, str]]]:
        """Recent turns of every contact (for reports), grouped by contact."""
        out: dict[int, list[dict[str, str]]] = {}
        for r in self.db.rows(
            "SELECT contact, role, content, ts FROM mem_turns WHERE employee=? AND ts>=? ORDER BY id",
            (self.employee, since),
        ):
            out.setdefault(int(r["contact"]), []).append(
                {"role": r["role"], "content": r["content"], "ts": r["ts"]}
            )
        return out

    def append_turn(self, contact_id: int, user: str, assistant: str, keep: int) -> None:
        ts = now_iso()
        e, db = self.employee, self.db
        with db.transaction():
            db.many(
                "INSERT INTO mem_turns (employee, contact, role, content, ts) VALUES (?, ?, ?, ?, ?)",
                [(e, contact_id, "user", user, ts), (e, contact_id, "assistant", assistant, ts)],
            )
            if keep <= 0:
                return
            n = db.row(
                "SELECT COUNT(*) AS n FROM mem_turns WHERE employee=? AND contact=? AND waiting=0",
                (e, contact_id),
            )["n"]
            if n > keep:
                # keep an even number so history always starts with a user turn; what falls out
                # of the window waits to be folded into the contact's long-term summary
                cut = n - (keep - keep % 2)
                db.execute(
                    "UPDATE mem_turns SET waiting=1 WHERE id IN (SELECT id FROM mem_turns "
                    "WHERE employee=? AND contact=? AND waiting=0 ORDER BY id LIMIT ?)",
                    (e, contact_id, cut),
                )
                waiting = db.row(
                    "SELECT COUNT(*) AS n FROM mem_turns WHERE employee=? AND contact=? AND waiting=1",
                    (e, contact_id),
                )["n"]
                if waiting > MAX_UNSUMMARIZED:
                    self._drop_waiting(contact_id, waiting - MAX_UNSUMMARIZED)

    def _drop_waiting(self, contact_id: int, count: int) -> None:
        self.db.execute(
            "DELETE FROM mem_turns WHERE id IN (SELECT id FROM mem_turns "
            "WHERE employee=? AND contact=? AND waiting=1 ORDER BY id LIMIT ?)",
            (self.employee, contact_id, count),
        )

    def archive_history(self, contact_id: int) -> None:
        """Move all recent turns to the summary queue (e.g. to summarise a conversation now)."""
        self.db.execute(
            "UPDATE mem_turns SET waiting=1 WHERE employee=? AND contact=? AND waiting=0",
            (self.employee, contact_id),
        )

    # long-term memory of a contact: a summary of everything older than the history window

    def summary(self, contact_id: int) -> str:
        row = self.db.row(
            "SELECT text FROM mem_summaries WHERE employee=? AND contact=?", (self.employee, contact_id)
        )
        return row["text"] if row else ""

    def unsummarized(self, contact_id: int) -> list[dict[str, str]]:
        return self.db.rows(
            "SELECT role, content, ts FROM mem_turns WHERE employee=? AND contact=? AND waiting=1 ORDER BY id",
            (self.employee, contact_id),
        )

    def set_summary(self, contact_id: int, text: str, consumed: int = 0) -> None:
        """Store a new summary; `consumed` trimmed turns are now part of it."""
        with self.db.transaction():
            if text.strip():
                self.db.execute(
                    "INSERT INTO mem_summaries (employee, contact, text, updated) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (employee, contact) DO UPDATE SET text=excluded.text, updated=excluded.updated",
                    (self.employee, contact_id, text.strip()[:MAX_SUMMARY], now_iso()),
                )
            else:
                self.db.execute(
                    "DELETE FROM mem_summaries WHERE employee=? AND contact=?", (self.employee, contact_id)
                )
            if consumed:
                self._drop_waiting(contact_id, consumed)

    # the customer's language: detected from their messages, or set by staff (then kept)

    def language(self, contact_id: int) -> dict[str, str]:
        row = self.db.row(
            "SELECT lang, source, country FROM mem_languages WHERE employee=? AND contact=?",
            (self.employee, contact_id),
        )
        if not row:
            return {}
        return {k: v for k, v in row.items() if v}

    def observe_language(self, contact_id: int, lang: str | None) -> None:
        """A detected language; ignored when staff chose one for this customer."""
        if not lang:
            return
        self.db.execute(
            "INSERT INTO mem_languages (employee, contact, lang, source) VALUES (?, ?, ?, 'auto') "
            "ON CONFLICT (employee, contact) DO UPDATE SET lang=excluded.lang "
            "WHERE mem_languages.source <> 'staff' AND mem_languages.lang <> excluded.lang",
            (self.employee, contact_id, lang),
        )

    def set_language(self, contact_id: int, lang: str | None, country: str | None = None) -> None:
        """Staff choice (kept until cleared); None goes back to detection."""
        if lang:
            self.db.execute(
                "INSERT INTO mem_languages (employee, contact, lang, source, country) VALUES (?, ?, ?, 'staff', ?) "
                "ON CONFLICT (employee, contact) DO UPDATE SET lang=excluded.lang, source='staff', "
                "country=excluded.country",
                (self.employee, contact_id, lang, country),
            )
        else:
            self.db.execute(
                "DELETE FROM mem_languages WHERE employee=? AND contact=?", (self.employee, contact_id)
            )

    # shared memory: lessons for every conversation. Written by a manager, or proposed by
    # the AI and kept "pending" until a manager approves (so no contact can plant them).

    @property
    def shared_memory(self) -> list[dict[str, Any]]:
        return list(self.data["shared_memory"])

    def add_memory(self, text: str, status: str, source: str) -> dict[str, Any]:
        def change(d: dict[str, Any]) -> dict[str, Any]:
            entry = {
                "id": d["next_memory_id"],
                "text": " ".join(text.split())[:500],
                "status": status,  # "active" | "pending"
                "source": source,
                "created": now_iso(),
            }
            d["next_memory_id"] += 1
            d["shared_memory"].append(entry)
            return entry

        return self._update(change)

    def update_memory(self, memory_id: int, **fields: Any) -> dict[str, Any]:
        def change(d: dict[str, Any]) -> dict[str, Any]:
            entry = next((m for m in d["shared_memory"] if m["id"] == memory_id), None)
            if entry is None:
                raise KeyError(memory_id)
            entry.update(fields)
            return entry

        return self._update(change)

    def remove_memory(self, memory_id: int) -> None:
        def change(d: dict[str, Any]) -> None:
            kept = [m for m in d["shared_memory"] if m["id"] != memory_id]
            if len(kept) == len(d["shared_memory"]):
                raise KeyError(memory_id)
            d["shared_memory"] = kept

        self._update(change)

    def forget(self, contact_id: int | None = None) -> None:
        tables = ("mem_turns", "mem_notes", "mem_summaries", "mem_languages")
        with self.db.transaction():
            for table in tables:
                if contact_id is None:
                    self.db.execute(f"DELETE FROM {table} WHERE employee=?", (self.employee,))
                else:
                    self.db.execute(
                        f"DELETE FROM {table} WHERE employee=? AND contact=?", (self.employee, contact_id)
                    )

    # notes the agent keeps about a contact (remember / recall skills)

    def notes(self, contact_id: int) -> dict[str, str]:
        rows = self.db.rows(
            "SELECT key, value FROM mem_notes WHERE employee=? AND contact=? ORDER BY key",
            (self.employee, contact_id),
        )
        return {r["key"]: r["value"] for r in rows}

    def set_note(self, contact_id: int, key: str, value: str) -> None:
        key = " ".join(key.split())[:60]
        notes = self.notes(contact_id)
        if key not in notes and len(notes) >= MAX_NOTES:
            raise ValueError(f"at most {MAX_NOTES} notes per contact; update or delete one")
        self.db.execute(
            "INSERT INTO mem_notes (employee, contact, key, value) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (employee, contact, key) DO UPDATE SET value=excluded.value",
            (self.employee, contact_id, key, value[:500]),
        )

    def delete_note(self, contact_id: int, key: str) -> None:
        self.db.execute(
            "DELETE FROM mem_notes WHERE employee=? AND contact=? AND key=?", (self.employee, contact_id, key)
        )

    # routines: the last period each one ran for, and its last result

    def routine(self, routine_id: str) -> dict[str, Any]:
        return dict(self.data["routines"].get(routine_id, {}))

    def update_routine(self, routine_id: str, **fields: Any) -> None:
        self._update(lambda d: d["routines"].setdefault(routine_id, {}).update(fields))

    # approval queue for outbound actions: rows numbered per employee

    @property
    def actions(self) -> list[dict[str, Any]]:
        rows = self.db.rows("SELECT data FROM actions WHERE employee=? ORDER BY number", (self.employee,))
        return [json.loads(r["data"]) for r in rows]

    def pending_actions(self) -> list[dict[str, Any]]:
        rows = self.db.rows(
            "SELECT data FROM actions WHERE employee=? AND status='pending' ORDER BY number", (self.employee,)
        )
        return [json.loads(r["data"]) for r in rows]

    def recent_actions(self, limit: int = 100, since: str | None = None) -> list[dict[str, Any]]:
        """Newest first; decided or not."""
        sql, args = "SELECT data FROM actions WHERE employee=?", [self.employee]
        if since:
            sql += " AND created>=?"
            args.append(since)
        rows = self.db.rows(sql + " ORDER BY number DESC LIMIT ?", [*args, limit])
        return [json.loads(r["data"]) for r in rows]

    def add_action(self, **fields: Any) -> dict[str, Any]:
        from .db import IntegrityError

        for _ in range(20):  # two processes may pick the same number: the loser takes the next
            row = self.db.row("SELECT MAX(number) AS n FROM actions WHERE employee=?", (self.employee,))
            action = {"id": int(row["n"] or 0) + 1 if row else 1, "created": now_iso(), **fields}
            try:
                self.db.execute(
                    "INSERT INTO actions (employee, number, created, status, data) VALUES (?, ?, ?, ?, ?)",
                    (
                        self.employee,
                        action["id"],
                        action["created"],
                        action.get("status", ""),
                        json.dumps(action, ensure_ascii=False),
                    ),
                )
                return action
            except IntegrityError:
                continue
        raise ConflictError("could not number the request; try again")

    def action(self, action_id: int) -> dict[str, Any] | None:
        row = self.db.row(
            "SELECT data FROM actions WHERE employee=? AND number=?", (self.employee, action_id)
        )
        return json.loads(row["data"]) if row else None

    def claim_action(self, action_id: int, expect: str, **fields: Any) -> dict[str, Any] | None:
        """Change a request only if it is still in status `expect` (e.g. pending -> executing),
        atomically: of two processes approving (or approving and rejecting) at once, one wins.
        Returns the updated request, or None when it was not in that status (or is gone)."""
        with self.db.transaction():
            action = self.action(action_id)
            if action is None or action.get("status") != expect:
                return None
            action.update(fields)
            won = self.db.execute(
                "UPDATE actions SET status=?, data=? WHERE employee=? AND number=? AND status=? RETURNING number",
                (
                    action.get("status", ""),
                    json.dumps(action, ensure_ascii=False),
                    self.employee,
                    action_id,
                    expect,
                ),
            )
        return action if won is not None else None

    def update_action(self, action_id: int, **fields: Any) -> dict[str, Any]:
        with self.db.transaction():
            action = self.action(action_id)
            if action is None:
                raise KeyError(action_id)
            action.update(fields)
            self.db.execute(
                "UPDATE actions SET status=?, data=? WHERE employee=? AND number=?",
                (action.get("status", ""), json.dumps(action, ensure_ascii=False), self.employee, action_id),
            )
        return action
