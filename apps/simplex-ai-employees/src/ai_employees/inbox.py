"""The unified inbox: every conversation from every channel in one store.

Channels are SimpleX (one per employee account, "simplex:<employee>"), Zalo OA,
Facebook Messenger and generic webhook channels. Each conversation has a mode:

- "ai":    the assigned AI employee answers new customer messages;
- "human": a person has taken over; the AI stays silent until switched back.

Employees keep their own per-contact memory keyed by an integer contact id.
SimpleX contacts use their SimpleX contact id; conversations from other channels
use EXTERNAL_BASE + the inbox conversation id, which never collides with them.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
        label = ATTACHMENT_KINDS.get(a.get("kind", ""), "tệp")
        labels.append(f"[{label}: {a['name']}]" if a.get("name") else f"[{label}]")
    return " ".join(x for x in [text.strip(), *labels] if x)


SENDERS = ("customer", "ai", "human", "system")
MODES = ("ai", "human")

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id INTEGER PRIMARY KEY,
  channel TEXT NOT NULL,
  external_id TEXT NOT NULL,
  customer_name TEXT NOT NULL DEFAULT '',
  employee TEXT NOT NULL DEFAULT '',
  mode TEXT NOT NULL DEFAULT 'ai',
  unread INTEGER NOT NULL DEFAULT 0,
  last_ts TEXT,
  last_preview TEXT NOT NULL DEFAULT '',
  last_sender TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL,
  UNIQUE (channel, external_id)
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  external_id TEXT,
  sender TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL,
  ts TEXT NOT NULL,
  UNIQUE (conversation_id, external_id)
);
CREATE INDEX IF NOT EXISTS messages_conv ON messages (conversation_id, id);
CREATE TABLE IF NOT EXISTS channel_state (
  channel TEXT PRIMARY KEY,
  data TEXT NOT NULL
);
"""


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
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(messages)")}
        if "attachments" not in columns:  # inbox.db from before attachments were kept
            self.db.execute("ALTER TABLE messages ADD COLUMN attachments TEXT NOT NULL DEFAULT ''")
        self._lock = threading.Lock()

    def _conv(self, row: sqlite3.Row | None) -> Conversation | None:
        return Conversation(**dict(row)) if row else None

    # conversations

    def conversation(self, conv_id: int) -> Conversation | None:
        return self._conv(self.db.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone())

    def find(self, channel: str, external_id: str) -> Conversation | None:
        row = self.db.execute(
            "SELECT * FROM conversations WHERE channel=? AND external_id=?", (channel, external_id)
        ).fetchone()
        return self._conv(row)

    def by_contact(self, employee: str, contact_id: int) -> Conversation | None:
        if contact_id >= EXTERNAL_BASE:
            return self.conversation(contact_id - EXTERNAL_BASE)
        return self.find(f"simplex:{employee}", str(contact_id))

    def upsert(self, channel: str, external_id: str, customer_name: str, employee: str) -> Conversation:
        with self._lock:
            conv = self.find(channel, external_id)
            if conv is None:
                self.db.execute(
                    "INSERT INTO conversations (channel, external_id, customer_name, employee, created) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (channel, external_id, customer_name, employee, now_iso()),
                )
            elif customer_name and customer_name != conv.customer_name:
                self.db.execute(
                    "UPDATE conversations SET customer_name=? WHERE id=?", (customer_name, conv.id)
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
    ) -> list[Conversation]:
        sql, args = "SELECT * FROM conversations WHERE 1=1", []
        if channel:
            sql += " AND channel=?"
            args.append(channel)
        if mode:
            sql += " AND mode=?"
            args.append(mode)
        if query:
            sql += " AND (customer_name LIKE ? OR last_preview LIKE ?)"
            args += [f"%{query}%", f"%{query}%"]
        sql += " ORDER BY last_ts DESC NULLS LAST, id DESC LIMIT ?"
        args.append(limit)
        return [c for c in (self._conv(r) for r in self.db.execute(sql, args)) if c]

    def set_mode(self, conv_id: int, mode: str) -> None:
        assert mode in MODES
        self.db.execute("UPDATE conversations SET mode=? WHERE id=?", (mode, conv_id))

    def set_employee(self, conv_id: int, employee: str) -> None:
        self.db.execute("UPDATE conversations SET employee=? WHERE id=?", (employee, conv_id))

    def mark_read(self, conv_id: int) -> None:
        self.db.execute("UPDATE conversations SET unread=0 WHERE id=?", (conv_id,))

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
    ) -> int | None:
        """Store a message; returns None when this external message was already stored.

        Attachments are {"kind": image|video|audio|file|sticker|link, "url"?, "thumb"?, "name"?}."""
        assert sender in SENDERS
        ts = ts or now_iso()
        stored = json.dumps(attachments, ensure_ascii=False) if attachments else ""
        preview = describe(text, attachments)
        with self._lock:
            try:
                cur = self.db.execute(
                    "INSERT INTO messages (conversation_id, external_id, sender, author, text, ts, attachments) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (conv_id, external_id, sender, author, text, ts, stored),
                )
            except sqlite3.IntegrityError:
                return None
            # Arrival order, not platform timestamps, decides what is "last": platform
            # clocks can be ahead of ours, and a reply must never look older than its question.
            self.db.execute(
                "UPDATE conversations SET last_ts=MAX(COALESCE(last_ts, ''), ?), last_preview=?, "
                "last_sender=?, unread = unread + ? WHERE id=?",
                (ts, preview[:160], sender, 1 if sender == "customer" else 0, conv_id),
            )
            return cur.lastrowid

    def has_external(self, conv_id: int, external_id: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM messages WHERE conversation_id=? AND external_id=?", (conv_id, external_id)
        ).fetchone()
        return row is not None

    def messages(self, conv_id: int, limit: int = 300) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT id, external_id, sender, author, text, ts, attachments FROM messages "
            "WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
            (conv_id, limit),
        ).fetchall()
        return [self._message(r) for r in reversed(rows)]

    @staticmethod
    def _message(row: sqlite3.Row) -> dict[str, Any]:
        m = dict(row)
        m["attachments"] = json.loads(m["attachments"]) if m.get("attachments") else []
        return m

    def message(self, conv_id: int, message_id: int) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT id, external_id, sender, author, text, ts, attachments FROM messages "
            "WHERE conversation_id=? AND id=?",
            (conv_id, message_id),
        ).fetchone()
        return self._message(row) if row else None

    def pending_customer_text(self, conv_id: int) -> list[dict[str, Any]]:
        """Customer messages after the last reply of any kind: what still needs an answer."""
        out: list[dict[str, Any]] = []
        for m in reversed(self.messages(conv_id, limit=50)):
            if m["sender"] != "customer":
                break
            out.append(m)
        return list(reversed(out))

    def is_pending(self, conv_id: int, message_id: int) -> bool:
        """True while nothing has been sent in this conversation after that message."""
        row = self.db.execute(
            "SELECT 1 FROM messages WHERE conversation_id=? AND id>? AND sender!='customer' LIMIT 1",
            (conv_id, message_id),
        ).fetchone()
        return row is None

    def awaiting_answer(self, since_ts: str) -> list[Conversation]:
        """Conversations in AI mode whose last message is a customer's, newer than since_ts."""
        rows = self.db.execute(
            "SELECT * FROM conversations WHERE mode='ai' AND last_sender='customer' AND last_ts>=? "
            "ORDER BY last_ts",
            (since_ts,),
        ).fetchall()
        return [c for c in (self._conv(r) for r in rows) if c]

    def recent_outbound(self, conv_id: int, since_ts: str) -> list[str]:
        rows = self.db.execute(
            "SELECT text FROM messages WHERE conversation_id=? AND sender IN ('ai','human','system') AND ts>=?",
            (conv_id, since_ts),
        ).fetchall()
        return [r["text"] for r in rows]

    # per-channel cursors and settings

    def channel_state(self, channel: str) -> dict[str, Any]:
        row = self.db.execute("SELECT data FROM channel_state WHERE channel=?", (channel,)).fetchone()
        return json.loads(row["data"]) if row else {}

    def set_channel_state(self, channel: str, **fields: Any) -> None:
        with self._lock:
            data = {**self.channel_state(channel), **fields}
            self.db.execute(
                "INSERT INTO channel_state (channel, data) VALUES (?, ?) "
                "ON CONFLICT(channel) DO UPDATE SET data=excluded.data",
                (channel, json.dumps(data, ensure_ascii=False)),
            )

    def stats(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for r in self.db.execute(
            "SELECT channel, COUNT(*) n, SUM(unread) unread, SUM(mode='human') human FROM conversations GROUP BY channel"
        ):
            out[r["channel"]] = {
                "conversations": r["n"],
                "unread": r["unread"] or 0,
                "human": r["human"] or 0,
            }
        return out
