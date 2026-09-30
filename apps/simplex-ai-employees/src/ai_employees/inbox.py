"""The unified inbox: every conversation from every channel in one store.

Channels are SimpleX (one per employee account, "simplex:<employee>"), Zalo OA,
Facebook Messenger, Telegram, WhatsApp, email and generic webhook channels. Each
conversation has a mode:

- "ai":    the assigned AI employee answers new customer messages;
- "human": a person has taken over; the AI stays silent until switched back.

and, for the team working it, a status (open / closed; a new customer message
reopens it), labels, a person and a team it is assigned to, and internal notes
(messages with sender "note": staff only, never sent, never shown to the AI).
Every answer to a waiting customer is timed, for the SLA report.

Employees keep their own per-contact memory keyed by an integer contact id.
SimpleX contacts use their SimpleX contact id; conversations from other channels
use EXTERNAL_BASE + the inbox conversation id, which never collides with them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from .db import Database, IntegrityError
from .i18n import tr
from .state import now_iso

EXTERNAL_BASE = 1_000_000_000
ATTACHMENT_KINDS = {
    "image": "ảnh",
    "video": "video",
    "audio": "ghi âm",
    "file": "tệp",
    "sticker": "sticker",
    "link": "link",
}


def describe(text: str, attachments: list[dict[str, Any]] | None) -> str:
    """Text plus a short label per attachment ("[ảnh]", "[tệp: báo giá.pdf]")."""
    labels = []
    for a in attachments or []:
        label = tr(ATTACHMENT_KINDS.get(a.get("kind", ""), "tệp"))
        labels.append(f"[{label}: {a['name']}]" if a.get("name") else f"[{label}]")
    return " ".join(x for x in [text.strip(), *labels] if x)


SENDERS = ("customer", "ai", "human", "system", "note")
MODES = ("ai", "human")
STATUSES = ("open", "closed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id {id},
  channel TEXT NOT NULL,
  external_id TEXT NOT NULL,
  customer_name TEXT NOT NULL DEFAULT '',
  employee TEXT NOT NULL DEFAULT '',
  mode TEXT NOT NULL DEFAULT 'ai',
  unread {int} NOT NULL DEFAULT 0,
  last_ts TEXT,
  last_preview TEXT NOT NULL DEFAULT '',
  last_sender TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  assignee TEXT NOT NULL DEFAULT '',
  team TEXT NOT NULL DEFAULT '',
  waiting_since TEXT,
  UNIQUE (channel, external_id)
);
CREATE INDEX IF NOT EXISTS conversations_recent ON conversations (last_ts);
CREATE TABLE IF NOT EXISTS messages (
  id {id},
  conversation_id {int} NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  external_id TEXT,
  sender TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL,
  ts TEXT NOT NULL,
  attachments TEXT NOT NULL DEFAULT '',
  translation TEXT NOT NULL DEFAULT '',
  UNIQUE (conversation_id, external_id)
);
CREATE INDEX IF NOT EXISTS messages_conv ON messages (conversation_id, id);
CREATE TABLE IF NOT EXISTS channel_state (
  channel TEXT PRIMARY KEY,
  data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_labels (
  conversation_id {int} NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  label TEXT NOT NULL,
  PRIMARY KEY (conversation_id, label)
);
CREATE INDEX IF NOT EXISTS conversation_labels_by_label ON conversation_labels (label);
CREATE TABLE IF NOT EXISTS response_times (
  id {id},
  conversation_id {int} NOT NULL,
  channel TEXT NOT NULL,
  responder TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT '',
  waited_from TEXT NOT NULL,
  answered_at TEXT NOT NULL,
  seconds {int} NOT NULL
);
CREATE INDEX IF NOT EXISTS response_times_by_time ON response_times (answered_at)
"""
# columns added after the first release, for databases created before them
LATER_COLUMNS = {
    "messages": {"attachments": "TEXT NOT NULL DEFAULT ''", "translation": "TEXT NOT NULL DEFAULT ''"},
    "conversations": {
        "status": "TEXT NOT NULL DEFAULT 'open'",
        "assignee": "TEXT NOT NULL DEFAULT ''",
        "team": "TEXT NOT NULL DEFAULT ''",
        "waiting_since": "TEXT",
    },
}
INDEXES = """
CREATE INDEX IF NOT EXISTS conversations_status ON conversations (status, waiting_since);
CREATE INDEX IF NOT EXISTS conversations_assignee ON conversations (assignee)
"""


def _seconds(start: str, end: str) -> int:
    try:
        return max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()))
    except ValueError:
        return 0


MESSAGE_COLUMNS = "id, external_id, sender, author, text, ts, attachments, translation"


@dataclass
class Conversation:
    id: int
    channel: str
    external_id: str
    customer_name: str
    employee: str
    mode: str
    unread: int
    last_ts: str | None
    last_preview: str
    last_sender: str
    created: str
    status: str = "open"
    assignee: str = ""
    team: str = ""
    waiting_since: str | None = None

    @property
    def is_simplex(self) -> bool:
        return self.channel.startswith("simplex:")

    @property
    def contact_id(self) -> int:
        """The id the employee uses for this customer's memory and notes."""
        return int(self.external_id) if self.is_simplex else EXTERNAL_BASE + self.id

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "contact_id": self.contact_id}


class Inbox:
    """The inbox tables, in SQLite (a file path) or the office's PostgreSQL (a Database)."""

    def __init__(self, path: str | Path | Database):
        self.db = path if isinstance(path, Database) else Database(str(path))
        self.db.script(SCHEMA)
        for table, later in LATER_COLUMNS.items():
            if self.db.postgres:
                for column, ddl in later.items():
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}")
                continue
            columns = {r["name"] for r in self.db.rows(f"PRAGMA table_info({table})")}
            for column, ddl in later.items():
                if column not in columns:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        self.db.script(INDEXES)

    _FIELDS = tuple(f.name for f in fields(Conversation))

    def _conv(self, row: dict[str, Any] | None) -> Conversation | None:
        return Conversation(**{k: row[k] for k in self._FIELDS}) if row else None

    # conversations

    def conversation(self, conv_id: int) -> Conversation | None:
        return self._conv(self.db.row("SELECT * FROM conversations WHERE id=?", (conv_id,)))

    def find(self, channel: str, external_id: str) -> Conversation | None:
        return self._conv(
            self.db.row(
                "SELECT * FROM conversations WHERE channel=? AND external_id=?", (channel, external_id)
            )
        )

    def by_contact(self, employee: str, contact_id: int) -> Conversation | None:
        if contact_id >= EXTERNAL_BASE:
            return self.conversation(contact_id - EXTERNAL_BASE)
        return self.find(f"simplex:{employee}", str(contact_id))

    def upsert(self, channel: str, external_id: str, customer_name: str, employee: str) -> Conversation:
        # one statement, safe when several processes see the same new customer at once
        self.db.execute(
            "INSERT INTO conversations (channel, external_id, customer_name, employee, created) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (channel, external_id) DO UPDATE SET "
            "customer_name = CASE WHEN excluded.customer_name <> '' THEN excluded.customer_name "
            "ELSE conversations.customer_name END",
            (channel, external_id, customer_name, employee, now_iso()),
        )
        conv = self.find(channel, external_id)
        assert conv is not None
        return conv

    def list(
        self,
        channel: str | None = None,
        mode: str | None = None,
        query: str | None = None,
        limit: int = 200,
        status: str | None = None,
        label: str | None = None,
        assignee: str | None = None,
        teams: list[str] | None = None,
        team: str | None = None,
        waiting: bool = False,
    ) -> list[Conversation]:
        """`assignee` "-" means unassigned; `teams` with `assignee` means "mine": assigned
        to that person, or to one of their teams and nobody in particular."""
        sql, args = "SELECT * FROM conversations WHERE 1=1", []
        if channel:
            sql += " AND channel=?"
            args.append(channel)
        if mode:
            sql += " AND mode=?"
            args.append(mode)
        if status:
            sql += " AND status=?"
            args.append(status)
        if waiting:
            sql += " AND waiting_since IS NOT NULL"
        if team:
            sql += " AND team=?"
            args.append(team)
        if label:
            sql += " AND id IN (SELECT conversation_id FROM conversation_labels WHERE label=?)"
            args.append(label)
        if assignee == "-":
            sql += " AND assignee='' AND team=''"
        elif assignee and teams:
            sql += f" AND (assignee=? OR (assignee='' AND team IN ({','.join('?' * len(teams))})))"
            args += [assignee, *teams]
        elif assignee:
            sql += " AND assignee=?"
            args.append(assignee)
        if query:
            sql += " AND (customer_name LIKE ? OR last_preview LIKE ?)"
            args += [f"%{query}%", f"%{query}%"]
        sql += " ORDER BY last_ts DESC NULLS LAST, id DESC LIMIT ?"
        args.append(limit)
        return [c for c in (self._conv(r) for r in self.db.rows(sql, args)) if c]

    def set_mode(self, conv_id: int, mode: str) -> None:
        assert mode in MODES
        self.db.execute("UPDATE conversations SET mode=? WHERE id=?", (mode, conv_id))

    def set_employee(self, conv_id: int, employee: str) -> None:
        self.db.execute("UPDATE conversations SET employee=? WHERE id=?", (employee, conv_id))

    def mark_read(self, conv_id: int) -> None:
        self.db.execute("UPDATE conversations SET unread=0 WHERE id=?", (conv_id,))

    def set_status(self, conv_id: int, status: str) -> None:
        """Closing also stops the SLA clock: nobody owes this customer an answer."""
        assert status in STATUSES
        if status == "closed":
            self.db.execute(
                "UPDATE conversations SET status='closed', waiting_since=NULL, unread=0 WHERE id=?",
                (conv_id,),
            )
        else:
            self.db.execute("UPDATE conversations SET status='open' WHERE id=?", (conv_id,))

    def set_assignee(self, conv_id: int, assignee: str, team: str) -> None:
        self.db.execute("UPDATE conversations SET assignee=?, team=? WHERE id=?", (assignee, team, conv_id))

    # labels

    def labels(self, conv_id: int) -> list[str]:
        return self.labels_for([conv_id]).get(conv_id, [])

    def labels_for(self, conv_ids: list[int]) -> dict[int, list[str]]:
        if not conv_ids:
            return {}
        out: dict[int, list[str]] = {}
        rows = self.db.rows(
            f"SELECT conversation_id, label FROM conversation_labels WHERE conversation_id IN "
            f"({','.join('?' * len(conv_ids))}) ORDER BY label",
            conv_ids,
        )
        for r in rows:
            out.setdefault(int(r["conversation_id"]), []).append(r["label"])
        return out

    def set_labels(self, conv_id: int, labels: list[str]) -> None:
        with self.db.transaction():
            self.db.execute("DELETE FROM conversation_labels WHERE conversation_id=?", (conv_id,))
            for label in dict.fromkeys(labels):
                self.db.execute(
                    "INSERT INTO conversation_labels (conversation_id, label) VALUES (?, ?)", (conv_id, label)
                )

    def add_labels(self, conv_id: int, labels: list[str]) -> None:
        for label in labels:
            self.db.execute(
                "INSERT INTO conversation_labels (conversation_id, label) VALUES (?, ?) ON CONFLICT DO NOTHING",
                (conv_id, label),
            )

    def rename_label(self, old: str, new: str | None) -> None:
        """A label renamed (or deleted, with None) in the settings."""
        if new is None:
            self.db.execute("DELETE FROM conversation_labels WHERE label=?", (old,))
            return
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO conversation_labels (conversation_id, label) "
                "SELECT conversation_id, ? FROM conversation_labels WHERE label=? ON CONFLICT DO NOTHING",
                (new, old),
            )
            self.db.execute("DELETE FROM conversation_labels WHERE label=?", (old,))

    # messages

    def add(
        self,
        conv_id: int,
        sender: str,
        text: str,
        author: str = "",
        external_id: str | None = None,
        ts: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        translation: str = "",
    ) -> int | None:
        """Store a message; returns None when this external message was already stored.

        Attachments are {"kind": image|video|audio|file|sticker|link, "url"?, "thumb"?, "name"?}."""
        assert sender in SENDERS
        ts = ts or now_iso()
        stored = json.dumps(attachments, ensure_ascii=False) if attachments else ""
        preview = describe(text, attachments)
        try:
            with self.db.transaction():
                mid = self.db.execute(
                    "INSERT INTO messages "
                    "(conversation_id, external_id, sender, author, text, ts, attachments, translation) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                    (conv_id, external_id, sender, author, text, ts, stored, translation),
                )
                if sender == "note":
                    return mid  # internal: the conversation's state and preview do not change
                if sender == "customer":
                    # a customer writing again reopens the conversation and starts the SLA clock
                    extra, extra_args = ", status='open', waiting_since=COALESCE(waiting_since, ?)", [ts]
                elif sender in ("ai", "human"):
                    self._timed_answer(conv_id, sender, author)
                    extra, extra_args = ", waiting_since=NULL", []
                else:
                    extra, extra_args = "", []
                # Arrival order, not platform timestamps, decides what is "last": platform
                # clocks can be ahead of ours, and a reply must never look older than its question.
                self.db.execute(
                    "UPDATE conversations SET "
                    "last_ts = CASE WHEN last_ts IS NULL OR last_ts < ? THEN ? ELSE last_ts END, "
                    f"last_preview=?, last_sender=?, unread = unread + ?{extra} WHERE id=?",
                    (ts, ts, preview[:160], sender, 1 if sender == "customer" else 0, *extra_args, conv_id),
                )
        except IntegrityError:
            return None
        return mid

    def _timed_answer(self, conv_id: int, responder: str, author: str) -> None:
        row = self.db.row("SELECT channel, waiting_since FROM conversations WHERE id=?", (conv_id,))
        if not row or not row["waiting_since"]:
            return
        now = now_iso()
        self.db.execute(
            "INSERT INTO response_times "
            "(conversation_id, channel, responder, author, waited_from, answered_at, seconds) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                conv_id,
                row["channel"],
                responder,
                author,
                row["waiting_since"],
                now,
                _seconds(row["waiting_since"], now),
            ),
        )

    def has_external(self, conv_id: int, external_id: str) -> bool:
        return (
            self.db.row(
                "SELECT 1 AS x FROM messages WHERE conversation_id=? AND external_id=?",
                (conv_id, external_id),
            )
            is not None
        )

    def messages(self, conv_id: int, limit: int = 300, notes: bool = True) -> list[dict[str, Any]]:
        """`notes=False` for anything outside the staff inbox (the AI, bridges)."""
        where = "" if notes else " AND sender<>'note'"
        rows = self.db.rows(
            f"SELECT {MESSAGE_COLUMNS} FROM messages WHERE conversation_id=?{where} ORDER BY id DESC LIMIT ?",
            (conv_id, limit),
        )
        return [self._message(r) for r in reversed(rows)]

    @staticmethod
    def _message(row: dict[str, Any]) -> dict[str, Any]:
        m = dict(row)
        m["attachments"] = json.loads(m["attachments"]) if m.get("attachments") else []
        return m

    def set_translation(self, conv_id: int, message_id: int, translation: str) -> None:
        self.db.execute(
            "UPDATE messages SET translation=? WHERE conversation_id=? AND id=?",
            (translation, conv_id, message_id),
        )

    def message(self, conv_id: int, message_id: int) -> dict[str, Any] | None:
        row = self.db.row(
            f"SELECT {MESSAGE_COLUMNS} FROM messages WHERE conversation_id=? AND id=?", (conv_id, message_id)
        )
        return self._message(row) if row else None

    def pending_customer_text(self, conv_id: int) -> list[dict[str, Any]]:
        """Customer messages after the last reply of any kind: what still needs an answer."""
        out: list[dict[str, Any]] = []
        for m in reversed(self.messages(conv_id, limit=50, notes=False)):
            if m["sender"] != "customer":
                break
            out.append(m)
        return list(reversed(out))

    def is_pending(self, conv_id: int, message_id: int) -> bool:
        """True while nothing has been sent in this conversation after that message."""
        row = self.db.row(
            "SELECT 1 AS x FROM messages WHERE conversation_id=? AND id>? AND sender NOT IN ('customer','note') LIMIT 1",
            (conv_id, message_id),
        )
        return row is None

    def awaiting_answer(self, since_ts: str) -> list[Conversation]:
        """Conversations in AI mode whose last message is a customer's, newer than since_ts."""
        rows = self.db.rows(
            "SELECT * FROM conversations WHERE mode='ai' AND last_sender='customer' AND last_ts>=? ORDER BY last_ts",
            (since_ts,),
        )
        return [c for c in (self._conv(r) for r in rows) if c]

    def recent_outbound(self, conv_id: int, since_ts: str) -> list[str]:
        rows = self.db.rows(
            "SELECT text FROM messages WHERE conversation_id=? AND sender IN ('ai','human','system') AND ts>=?",
            (conv_id, since_ts),
        )
        return [r["text"] for r in rows]

    # per-channel cursors and settings

    def channel_state(self, channel: str) -> dict[str, Any]:
        row = self.db.row("SELECT data FROM channel_state WHERE channel=?", (channel,))
        return json.loads(row["data"]) if row else {}

    def set_channel_state(self, channel: str, **fields: Any) -> None:
        with self.db.transaction():
            data = {**self.channel_state(channel), **fields}
            self.db.execute(
                "INSERT INTO channel_state (channel, data) VALUES (?, ?) "
                "ON CONFLICT(channel) DO UPDATE SET data=excluded.data",
                (channel, json.dumps(data, ensure_ascii=False)),
            )

    def sla(self, since_ts: str, now_ts: str, target_seconds: int) -> dict[str, Any]:
        """Answer times since `since_ts`, and who is waiting now (for the SLA report)."""
        late_ts = datetime.fromtimestamp(
            datetime.fromisoformat(now_ts).timestamp() - target_seconds,
            tz=datetime.fromisoformat(now_ts).tzinfo,
        ).isoformat(timespec="seconds")
        responders = [
            {
                "responder": r["responder"],
                "author": r["author"],
                "answers": int(r["n"]),
                "avg_seconds": round(float(r["avg"] or 0)),
                "max_seconds": int(r["mx"] or 0),
                "within_target": int(r["ok"] or 0),
            }
            for r in self.db.rows(
                "SELECT responder, CASE WHEN responder='ai' THEN '' ELSE author END AS author, COUNT(*) AS n, "
                "AVG(seconds) AS avg, MAX(seconds) AS mx, SUM(CASE WHEN seconds<=? THEN 1 ELSE 0 END) AS ok "
                "FROM response_times WHERE answered_at>=? GROUP BY 1, 2 ORDER BY 3 DESC",
                (target_seconds, since_ts),
            )
        ]
        by_channel = {
            r["channel"]: {"answers": int(r["n"]), "avg_seconds": round(float(r["avg"] or 0))}
            for r in self.db.rows(
                "SELECT channel, COUNT(*) AS n, AVG(seconds) AS avg FROM response_times "
                "WHERE answered_at>=? GROUP BY channel",
                (since_ts,),
            )
        }
        counts = (
            self.db.row(
                "SELECT SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open, "
                "SUM(CASE WHEN status='open' AND waiting_since IS NOT NULL THEN 1 ELSE 0 END) AS waiting, "
                "SUM(CASE WHEN status='open' AND waiting_since IS NOT NULL AND waiting_since<? THEN 1 ELSE 0 END) AS late, "
                "SUM(CASE WHEN status='open' AND assignee='' AND team='' THEN 1 ELSE 0 END) AS unassigned "
                "FROM conversations",
                (late_ts,),
            )
            or {}
        )
        workload = [
            {"assignee": r["assignee"], "team": r["team"], "open": int(r["n"]), "waiting": int(r["w"] or 0)}
            for r in self.db.rows(
                "SELECT assignee, team, COUNT(*) AS n, "
                "SUM(CASE WHEN waiting_since IS NOT NULL THEN 1 ELSE 0 END) AS w FROM conversations "
                "WHERE status='open' AND (assignee<>'' OR team<>'') GROUP BY assignee, team ORDER BY 3 DESC"
            )
        ]
        oldest = [
            c
            for c in (
                self._conv(r)
                for r in self.db.rows(
                    "SELECT * FROM conversations WHERE status='open' AND waiting_since IS NOT NULL "
                    "ORDER BY waiting_since LIMIT 20"
                )
            )
            if c
        ]
        return {
            "counts": {k: int(counts.get(k) or 0) for k in ("open", "waiting", "late", "unassigned")},
            "responders": responders,
            "by_channel": by_channel,
            "workload": workload,
            "oldest_waiting": oldest,
        }

    def stats(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for r in self.db.rows(
            "SELECT channel, COUNT(*) AS n, SUM(unread) AS unread, "
            "SUM(CASE WHEN mode='human' THEN 1 ELSE 0 END) AS human FROM conversations GROUP BY channel"
        ):
            out[r["channel"]] = {
                "conversations": int(r["n"]),
                "unread": int(r["unread"] or 0),
                "human": int(r["human"] or 0),
            }
        return out
