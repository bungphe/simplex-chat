"""The channel hub: moves messages between chat platforms, the inbox and the AI employees.

- Polls every channel for new messages and stores them in the unified inbox.
- When a customer writes in a conversation in "ai" mode, waits a few seconds for
  follow-up messages, then has the assigned employee answer them all at once.
- Anyone replying from the inbox, or directly on the platform, takes the
  conversation over ("human" mode): the AI stays silent until switched back.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .channels import Channel, ChannelError, InboundMessage, make_channel
from .desk import Desk
from .inbox import EXTERNAL_BASE, Conversation, Inbox, describe
from .lang import detect
from .state import now_iso

if TYPE_CHECKING:
    from .employee import Employee, Office

log = logging.getLogger(__name__)

ECHO_WINDOW = timedelta(minutes=10)


def customer_text(messages: list[dict[str, Any]]) -> str:
    """What the AI reads for the customer's waiting messages. The model does not see
    attachments, only that they were sent, so it can ask or hand over instead of guessing."""
    lines = []
    for m in messages:
        line = describe(m["text"], m.get("attachments"))
        if m.get("attachments"):
            line += " (bạn không xem được tệp đính kèm; nếu cần, hỏi khách mô tả hoặc chuyển cho người thật)"
        lines.append(line)
    return "\n".join(lines)


def iso(ts: datetime) -> str:
    return ts.astimezone().isoformat(timespec="seconds")


class ChannelHub:
    def __init__(self, office: Office):
        self.office = office
        state_dir = Path(office.config.state_dir)
        self.inbox = Inbox(office.db or state_dir / "inbox.db")
        self.desk = Desk(office.docs)
        old = state_dir / "channel_secrets.json"
        if old.exists():  # tokens kept before they moved into the database
            saved = json.loads(old.read_text(encoding="utf-8"))
            office.docs.update("channel_secrets", lambda d: d.update(saved), {})
            old.rename(old.with_suffix(".json.imported"))
        self.channels: dict[str, Channel] = {c.id: make_channel(c, self) for c in office.config.channels}
        self.started = datetime.now(UTC)
        self.catch_up = timedelta(hours=office.config.catch_up_hours)
        # Customer messages newer than this get an AI answer. A polled channel that ran
        # before answers what arrived while we were down (since its last cursor); on its
        # very first run it only imports the recent history, as context.
        self.answer_from: dict[str, datetime] = {}
        for ch in self.channels.values():
            cursor = self.inbox.channel_state(ch.id).get("cursor")
            polled = type(ch).poll is not Channel.poll
            if polled and not cursor:
                self.answer_from[ch.id] = self.started
            else:
                self.answer_from[ch.id] = self.started - self.catch_up
        self.resume_delay = 2.0  # seconds before the first start-up catch-up answer
        self._timers: dict[int, asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ #
    # secrets that change at runtime (rotating Zalo tokens), owner-only file

    @property
    def secrets(self) -> dict[str, dict[str, str]]:
        """Tokens that change at runtime (rotating Zalo tokens), shared by all processes."""
        return self.office.docs.get("channel_secrets", {})

    def save_secret(self, channel_id: str, **values: str) -> None:
        self.office.docs.update("channel_secrets", lambda d: d.setdefault(channel_id, {}).update(values), {})

    # ------------------------------------------------------------------ #
    # polling

    async def run(self, stopping: asyncio.Event) -> None:
        self.resume_pending()
        await asyncio.gather(*(self._poll_loop(ch, stopping) for ch in self.channels.values()))

    def resume_pending(self) -> list[int]:
        """At start-up: answer customers left waiting by a restart or an unreachable model
        (the reply timer does not survive a restart). Older messages stay for staff."""
        since = iso(datetime.now(UTC) - self.catch_up)
        resumed = []
        for i, conv in enumerate(self.inbox.awaiting_answer(since)):
            ch = self.channels.get(conv.channel)
            if ch is not None and not ch.cfg.auto_reply:
                continue
            if ch is None and not (
                conv.is_simplex and conv.channel.split(":", 1)[1] in self.office.employees
            ):
                continue  # a channel no longer configured
            if not self.office.cluster.owns(conv):
                continue  # its own shard answers it
            self.schedule_reply(conv.id, self.resume_delay + i)  # staggered, not all at once
            resumed.append(conv.id)
        if resumed:
            log.info("inbox: answering %d conversation(s) left waiting before start-up", len(resumed))
        return resumed

    async def _poll_loop(self, ch: Channel, stopping: asyncio.Event) -> None:
        if ch.cfg.poll_seconds <= 0 or type(ch).poll is Channel.poll:
            return  # push-only channel (webhook)
        if not self.office.cluster.is_primary:
            return  # one shard polls; the owners answer
        while not stopping.is_set():
            await self.poll_once(ch.id)
            try:
                await asyncio.wait_for(stopping.wait(), timeout=ch.cfg.poll_seconds)
            except TimeoutError:
                pass

    async def poll_once(self, channel_id: str) -> int:
        ch = self.channels[channel_id]
        st = self.inbox.channel_state(ch.id)
        # First run: import the last day for context, but only answer what arrives from now on.
        since = datetime.fromisoformat(st["cursor"]) if st.get("cursor") else self.started - timedelta(days=1)
        try:
            msgs = await ch.poll(since - timedelta(minutes=2))  # overlap; duplicates are dropped
        except (ChannelError, Exception) as e:  # noqa: BLE001 - a channel must never stop the loop
            log.warning("%s: poll failed: %s", ch.id, e)
            self.inbox.set_channel_state(ch.id, last_poll=now_iso(), last_error=str(e)[:300])
            return 0
        added = self.ingest(ch, msgs)
        cursor = max([m.ts for m in msgs], default=since)
        self.inbox.set_channel_state(ch.id, cursor=iso(cursor), last_poll=now_iso(), last_error=None)
        return added

    def ingest(self, ch: Channel, msgs: list[InboundMessage]) -> int:
        added = 0
        to_answer: set[int] = set()
        for m in sorted(msgs, key=lambda m: m.ts):
            conv = self.inbox.upsert(ch.id, m.conversation, m.customer_name, ch.cfg.employee)
            if m.sender == "customer" and (employee := self.employee_for(conv)) is not None:
                employee.state.observe_language(conv.contact_id, detect(m.text))
            if m.sender == "customer":
                if self.inbox.add(
                    conv.id, "customer", m.text, m.customer_name, m.external_id, iso(m.ts), m.attachments
                ):
                    added += 1
                    if self.triage(conv, m.text):
                        to_answer.discard(conv.id)
                    elif m.ts >= self.answer_from.get(ch.id, self.started):
                        to_answer.add(conv.id)
                continue
            # A message from the business side that we did not send: someone answered on
            # the platform itself (or it is the echo of our own message).
            if self.inbox.has_external(conv.id, m.external_id):
                continue
            if m.text in self.inbox.recent_outbound(conv.id, iso(m.ts - ECHO_WINDOW)):
                continue
            if self.inbox.add(
                conv.id, "human", m.text, f"trên {ch.type}", m.external_id, iso(m.ts), m.attachments
            ):
                added += 1
                if m.ts >= self.answer_from.get(ch.id, self.started):
                    self.inbox.set_mode(conv.id, "human")
                    to_answer.discard(conv.id)
        if ch.cfg.auto_reply:
            for conv_id in to_answer:
                conv = self.inbox.conversation(conv_id)
                if conv is not None:
                    self.office.cluster.request_reply(conv, ch.cfg.debounce_seconds)
        return added

    def push_inbound(self, channel_id: str, payload: dict[str, Any]) -> Conversation | None:
        """A message pushed to /hooks/<channel id> (webhook bridges, the Zalo gateway)."""
        ch = self.channels.get(channel_id)
        if ch is None or not ch.accepts_push():
            raise KeyError(channel_id)
        msgs = [m for m in ch.parse_push(payload) if m.conversation and m.external_id]
        if not msgs:
            return None
        self.ingest(ch, msgs)
        conv = self.inbox.find(ch.id, msgs[-1].conversation)
        if conv is not None and not conv.customer_name:
            self._spawn(self._fill_name(ch, conv))
        return conv

    async def _fill_name(self, ch: Channel, conv: Conversation) -> None:
        """Platform events carry only an id: ask the platform for the customer's name."""
        try:
            name = await ch.lookup_name(conv.external_id)
        except Exception as e:  # noqa: BLE001 - a missing name is cosmetic
            log.info("%s: no name for %s: %s", ch.id, conv.external_id, e)
            return
        if name:
            self.inbox.upsert(ch.id, conv.external_id, name, conv.employee)

    def _spawn(self, coro: Any) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:  # no event loop (a synchronous caller): skip the extra
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    webhook_inbound = push_inbound

    def triage(self, conv: Conversation, text: str) -> bool:
        """Apply the inbox rules to a new customer message: labels, a team or person (only
        if nobody has the conversation yet). True when a rule handed it to a person."""
        rules = self.desk.matching_rules(conv.channel, text)
        if not rules:
            return False
        if labels := [label for r in rules for label in r["labels"]]:
            self.inbox.add_labels(conv.id, labels)
        now = self.inbox.conversation(conv.id) or conv
        team = now.team or next((r["team"] for r in rules if r["team"]), "")
        assignee = now.assignee or next((r["assignee"] for r in rules if r["assignee"]), "")
        if (team, assignee) != (now.team, now.assignee):
            self.inbox.set_assignee(conv.id, assignee, team)
        if any(r["handoff"] for r in rules):
            if now.mode != "human":
                self.inbox.set_mode(conv.id, "human")
                log.info("inbox: conversation %s handed to staff by a triage rule", conv.id)
            return True
        return False

    def add_note(self, conv_id: int, text: str, author: str) -> int | None:
        """An internal note: staff only, never sent to the customer or shown to the AI."""
        return self.inbox.add(conv_id, "note", text, author)

    async def summarize_thread(self, conv_id: int) -> str:
        """A short briefing for staff picking up the conversation (nothing is stored)."""
        conv = self.inbox.conversation(conv_id)
        if conv is None:
            raise KeyError(conv_id)
        employee = self.employee_for(conv)
        if employee is None:
            raise KeyError(conv.employee)
        who = {
            "customer": "Khách",
            "ai": "AI",
            "human": "Nhân viên",
            "system": "Hệ thống",
            "note": "Ghi chú nội bộ",
        }
        lines = [
            f"{who[m['sender']]}{' ' + m['author'] if m['author'] and m['sender'] != 'customer' else ''} "
            f"({m['ts'][:16]}): {describe(m['text'], m['attachments'])[:1500]}"
            for m in self.inbox.messages(conv_id, limit=120)
        ]
        return await employee.agent.summarize_thread(
            conv.contact_id, conv.customer_name, "\n".join(lines), self.office.config.staff_language
        )

    # ------------------------------------------------------------------ #
    # answering

    def schedule_reply(self, conv_id: int, delay: float) -> None:
        """Answer after `delay` seconds without new messages (customers often send several)."""
        if (old := self._timers.get(conv_id)) and not old.done():
            old.cancel()

        async def later() -> None:
            await asyncio.sleep(delay)
            self._timers.pop(conv_id, None)
            try:
                await self.reply_ai(conv_id)
            except ChannelError as e:
                log.warning("inbox: AI reply to conversation %s not delivered: %s", conv_id, e)
            except Exception:
                log.exception("inbox: AI reply to conversation %s failed", conv_id)

        task = asyncio.create_task(later())
        self._timers[conv_id] = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def employee_for(self, conv: Conversation) -> Employee | None:
        return self.office.employees.get(conv.employee)

    async def reply_ai(self, conv_id: int) -> str | None:
        conv = self.inbox.conversation(conv_id)
        if conv is None or conv.mode != "ai":
            return None
        employee = self.employee_for(conv)
        pending = self.inbox.pending_customer_text(conv_id)
        if employee is None or employee.settings.paused or not pending:
            return None
        async with employee._locks[conv.contact_id]:
            pending = self.inbox.pending_customer_text(conv_id)  # may have been answered meanwhile
            if not pending:
                return None
            text = customer_text(pending)
            r = await employee.agent.respond_run(conv.contact_id, conv.customer_name or "khách", text)
            if r.status == "busy":
                # No model reachable: don't send an apology on a business channel. The
                # message stays unread and waiting, for staff or the customer's next message.
                log.warning("%s: no model available; conversation %s left for staff", conv.channel, conv_id)
                return None
            answer = r.text
            if (now := self.inbox.conversation(conv_id)) is None or now.mode != "ai":
                log.info(
                    "%s: conversation %s taken over while the AI was answering; not sent",
                    conv.channel,
                    conv_id,
                )
                return None
            await self.deliver(now, answer, "ai", employee.settings.display_name)
            return answer

    async def deliver(
        self, conv: Conversation, text: str, sender: str, author: str, original: str = ""
    ) -> None:
        """Send on the conversation's own channel and record it in the inbox."""
        external_id = None
        if conv.is_simplex:
            employee = self.office.employees[conv.channel.split(":", 1)[1]]
            await self.office.cluster.simplex_send(employee, int(conv.external_id), text)
        else:
            ch = self.channels.get(conv.channel)
            if ch is None:
                raise ChannelError(f"channel {conv.channel} is no longer configured")
            try:
                external_id = await ch.send(conv.external_id, text)
            except ChannelError as e:
                self.inbox.set_channel_state(ch.id, last_error=f"gửi tin: {e}"[:300])
                raise
        if self.inbox.add(conv.id, sender, text, author, external_id, translation=original) is None:
            # The platform reused a message id: never lose the record of what was sent.
            log.warning(
                "%s: message id %s already stored; keeping the reply without it", conv.channel, external_id
            )
            self.inbox.add(conv.id, sender, text, author, translation=original)

    async def human_reply(
        self, conv_id: int, text: str, author: str, take_over: bool = True, translate: bool = False
    ) -> None:
        """Staff reply. With `translate`, staff write in their own language and the customer
        gets it in theirs; the inbox keeps both."""
        conv = self.inbox.conversation(conv_id)
        if conv is None:
            raise KeyError(conv_id)
        employee = self.employee_for(conv)
        original = ""
        if translate and employee is not None:
            target = employee.agent.contact_language(conv.contact_id)
            if target and target != self.office.config.staff_language:
                original = text
                text = await employee.agent.translate(text, target, "A reply from shop staff to a customer.")
        if (timer := self._timers.pop(conv_id, None)) is not None:
            timer.cancel()
        if take_over:
            self.inbox.set_mode(conv_id, "human")
        pending = customer_text(self.inbox.pending_customer_text(conv_id)) or "(…)"
        await self.deliver(conv, text, "human", author, original)
        self.inbox.mark_read(conv_id)
        # Keep the AI's memory complete, so it knows what staff said if it takes over again.
        if employee is not None:
            employee.state.append_turn(
                conv.contact_id,
                pending,
                f"[nhân viên {author}] {text}",
                keep=employee.settings.history_messages,
            )

    async def translate_message(self, conv_id: int, message_id: int) -> str:
        """A customer message in the staff language (kept, so it is translated once)."""
        conv = self.inbox.conversation(conv_id)
        m = self.inbox.message(conv_id, message_id) if conv else None
        if conv is None or m is None:
            raise KeyError(message_id)
        if m["translation"]:
            return m["translation"]
        employee = self.employee_for(conv)
        if employee is None:
            raise KeyError(conv.employee)
        out = await employee.agent.translate(
            m["text"], self.office.config.staff_language, "A customer's message, for shop staff to read."
        )
        self.inbox.set_translation(conv_id, message_id, out)
        return out

    async def suggest(self, conv_id: int, in_staff_language: bool = False) -> str:
        conv = self.inbox.conversation(conv_id)
        if conv is None:
            raise KeyError(conv_id)
        employee = self.employee_for(conv)
        if employee is None:
            raise KeyError(conv.employee)
        pending = customer_text(self.inbox.pending_customer_text(conv_id))
        return await employee.agent.suggest(
            conv.contact_id,
            conv.customer_name or "khách",
            pending,
            write_in=self.office.config.staff_language if in_staff_language else None,
        )

    # ------------------------------------------------------------------ #
    # SimpleX conversations are answered by the employee's own bot; mirror them here

    def simplex_inbound(
        self,
        employee: Employee,
        contact_id: int,
        name: str,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> tuple[Conversation, int | None]:
        """Mirror a SimpleX message; returns the conversation and the stored message id."""
        conv = self.inbox.upsert(f"simplex:{employee.id}", str(contact_id), name, employee.id)
        employee.state.observe_language(contact_id, detect(text))
        mid = self.inbox.add(conv.id, "customer", text, name, attachments=attachments)
        if mid is not None and self.triage(conv, text):
            conv = self.inbox.conversation(conv.id) or conv
        return conv, mid

    def simplex_outbound(self, employee: Employee, contact_id: int, text: str, sender: str) -> None:
        conv = self.inbox.upsert(f"simplex:{employee.id}", str(contact_id), "", employee.id)
        self.inbox.add(conv.id, sender, text, employee.settings.display_name if sender == "ai" else "")

    async def send_to_contact(
        self, employee: Employee, contact_id: int, text: str, sender: str = "system"
    ) -> None:
        """Message a customer of this employee on whichever channel they use."""
        if contact_id >= EXTERNAL_BASE:
            conv = self.inbox.conversation(contact_id - EXTERNAL_BASE)
            if conv is None:
                raise KeyError(contact_id)
            await self.deliver(conv, text, sender, employee.settings.display_name)
            return
        await self.office.cluster.simplex_send(employee, contact_id, text)
        self.simplex_outbound(employee, contact_id, text, sender)
