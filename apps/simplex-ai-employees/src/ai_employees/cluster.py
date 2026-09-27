"""Several office processes on one PostgreSQL: who answers what.

    cluster: {shards: 4}          # employees.yaml; each process started with AIE_SHARD=0..3

- Conversation c is owned by shard c % shards: only the owner schedules and sends AI
  replies for it (so a customer never gets two answers, and the per-customer lock and
  reply timer live in one place). Any process may receive a message (behind a load
  balancer); it stores it and tells the owner with PostgreSQL NOTIFY.
- Shard 0 runs what exists once: the SimpleX accounts, scheduled routines and channel
  polling. SimpleX messages sent from other shards (staff replies, order confirmations,
  alerts to managers) go through an outbox table that shard 0 delivers.

With one shard (the default, and always with SQLite) everything happens in-process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING, Any

from .config import ConfigError

if TYPE_CHECKING:
    from .employee import Employee, Office
    from .inbox import Conversation

log = logging.getLogger(__name__)

OUTBOX = """
CREATE TABLE IF NOT EXISTS simplex_outbox (
  id {id}, employee TEXT NOT NULL, contact {int} NOT NULL, text TEXT NOT NULL,
  created TEXT NOT NULL, sent {int} NOT NULL DEFAULT 0)
"""
# simplex_outbox.sent: 0 waiting, 1 sent (or being sent), 2 failed (retried up to OUTBOX_TRIES)
OUTBOX_TRIES = 3


class Cluster:
    def __init__(self, office: Office):
        self.office = office
        self.shards = int(office.config.shards)
        env = os.environ.get("AIE_SHARD", "0")
        self.shard = int(env)
        if not 0 <= self.shard < self.shards:
            raise ConfigError(f"AIE_SHARD={env} but cluster.shards is {self.shards}")
        if self.shards > 1 and office.db is None:
            raise ConfigError("cluster.shards > 1 needs a PostgreSQL database_url")
        if self.shards > 1:
            office.office_db.script(OUTBOX)
            office.office_db.add_columns("simplex_outbox", {"attempts": "{int} NOT NULL DEFAULT 0"})

    @property
    def active(self) -> bool:
        return self.shards > 1

    @property
    def is_primary(self) -> bool:
        """The shard that runs SimpleX accounts, routines and channel polling."""
        return self.shard == 0

    def owner(self, conv: Conversation) -> int:
        # SimpleX conversations stay with the shard running their account
        return 0 if conv.is_simplex else conv.id % self.shards

    def owns(self, conv: Conversation) -> bool:
        return self.owner(conv) == self.shard

    def _notify(self, shard: int, payload: dict[str, Any]) -> None:
        self.office.office_db.execute("SELECT pg_notify(?, ?)", (f"aie_shard_{shard}", json.dumps(payload)))

    # replies

    def request_reply(self, conv: Conversation, delay: float) -> None:
        """Have the owner of this conversation answer it after `delay` quiet seconds."""
        if self.owns(conv):
            self.office.hub.schedule_reply(conv.id, delay)
        else:
            self._notify(self.owner(conv), {"reply": conv.id, "delay": delay})

    # SimpleX messages, which only the primary shard can send

    async def simplex_send(self, employee: Employee, contact_id: int, text: str) -> None:
        from .employee import split_message

        if self.is_primary:
            for chunk in split_message(text):
                await employee.bot.api.api_send_text_message(["direct", contact_id], chunk)
            return
        from .state import now_iso

        oid = self.office.office_db.execute(
            "INSERT INTO simplex_outbox (employee, contact, text, created) VALUES (?, ?, ?, ?) RETURNING id",
            (employee.id, contact_id, text, now_iso()),
        )
        self._notify(0, {"outbox": oid})

    async def _drain_outbox(self) -> None:
        db = self.office.office_db
        pending = db.rows(
            "SELECT id, employee, contact, text FROM simplex_outbox "
            "WHERE sent=0 OR (sent=2 AND attempts<?) ORDER BY id",
            (OUTBOX_TRIES,),
        )
        for row in pending:
            # claim it first: never send twice, even if two notifications race
            claimed = db.execute(
                "UPDATE simplex_outbox SET sent=1, attempts=attempts+1 "
                "WHERE id=? AND (sent=0 OR sent=2) AND attempts<? RETURNING attempts",
                (row["id"], OUTBOX_TRIES),
            )
            if claimed is None:
                continue
            employee = self.office.employees.get(row["employee"])
            try:
                if employee is None:
                    raise LookupError(f"no employee {row['employee']}")
                await self.simplex_send(employee, int(row["contact"]), row["text"])
            except Exception:
                db.execute("UPDATE simplex_outbox SET sent=2 WHERE id=?", (row["id"],))
                log.exception(
                    "cluster: outbox message %s to %s failed (try %s of %s)",
                    row["id"],
                    row["contact"],
                    claimed,
                    OUTBOX_TRIES,
                )

    # listening

    async def run(self, stopping: asyncio.Event) -> None:
        if not self.active:
            return
        import psycopg

        assert self.office.config.database_url
        while not stopping.is_set():
            try:
                conn = await psycopg.AsyncConnection.connect(self.office.config.database_url, autocommit=True)
                async with conn:
                    await conn.execute(f"LISTEN aie_shard_{self.shard}")
                    log.info("cluster: shard %d/%d listening", self.shard, self.shards)
                    while not stopping.is_set():
                        if self.is_primary:
                            await self._drain_outbox()  # also catches a notification we missed
                        async for note in conn.notifies(timeout=1.0):
                            await self._handle(json.loads(note.payload))
            except (OSError, psycopg.Error) as e:
                log.warning("cluster: listener lost (%s); reconnecting", e)
                await asyncio.sleep(2)

    async def _handle(self, payload: dict[str, Any]) -> None:
        if "reply" in payload:
            self.office.hub.schedule_reply(int(payload["reply"]), float(payload.get("delay", 0)))
        elif "outbox" in payload and self.is_primary:
            await self._drain_outbox()
