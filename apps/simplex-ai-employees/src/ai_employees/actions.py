"""Outbound actions and the approval queue.

An action is something that leaves the office and is hard to take back: creating
an order in another system, booking an appointment, notifying a supplier. Actions
are declared in the config as webhooks, so any system with an HTTP endpoint
(your own API, n8n, Zapier, Make, a CRM) can be connected without code:

    actions:
      create_order:
        description: Create a sales order in the shop system
        url: https://n8n.example.com/webhook/order
        headers: {Authorization: "Bearer ${ORDER_TOKEN}"}
        fields:
          customer_name: Customer's full name
          phone: Phone number
          items: Products and quantities

An action with `kind: stock_order` needs no URL: it confirms a sales order in the
office's own inventory (see inventory.py), reserving the goods at current prices:

    actions:
      create_order:
        kind: stock_order
        description: Confirm an order and reserve the goods

Each action becomes a skill of the same name. When an employee uses it, the
request is **held for a manager's approval** unless that employee's managers have
released the action (`releases:` in the config, or `/ai release <action>` in chat).
Only managers release; nothing an employee or a contact writes can.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx2

from . import lang
from . import skills as sk
from .state import now_iso

if TYPE_CHECKING:
    from .employee import Employee

log = logging.getLogger(__name__)

STATUSES = ("pending", "executing", "done", "failed", "rejected")
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}$")


@dataclass(frozen=True)
class ActionDef:
    name: str
    description: str
    url: str
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)
    confirm_message: str | dict[str, str] | None = None  # text, or {language code: text}
    timeout: float = 30.0
    kind: str = "webhook"  # or "stock_order": an order in the office's own inventory

    def skill(self) -> sk.Skill:
        props = {k: {"type": "string", "description": v} for k, v in self.fields.items()}
        schema = {
            "type": "object",
            "properties": props,
            "required": list(props),
            "additionalProperties": False,
        }
        desc = f"{self.description} This action may need a manager's approval before it happens."

        async def handler(ctx: sk.SkillContext, **args: str) -> str:
            return await ctx.employee.actions.request(self, args, ctx)

        return sk.Skill(name=self.name, description=desc, input_schema=schema, handler=handler)


STOCK_ORDER_FIELDS = {
    "items": "Products and quantities as 'SKU x quantity', separated by ';' (e.g. 'MA-100 x 2; LOC-01 x 1'). "
    "Use the SKUs from the products skill.",
    "customer_name": "Customer's full name",
    "phone": "Phone number",
    "address": "Delivery address, or 'nhận tại cửa hàng' for pickup",
    "note": "Anything else the shop must know (delivery time...), or '-'",
}


def parse_action(name: str, raw: dict[str, Any]) -> ActionDef:
    if not _NAME.match(name):
        raise ValueError(f"action name '{name}' must be lowercase letters, digits or '_'")
    if raw.get("kind") == "stock_order":
        unknown = set(raw) - {"kind", "description", "confirm_message"}
        if unknown:
            raise ValueError(f"action {name}: unknown fields {', '.join(sorted(unknown))}")
        return ActionDef(
            name=name,
            description=str(
                raw.get("description") or "Confirm a sales order and reserve the goods in stock."
            ),
            url="",
            fields=dict(STOCK_ORDER_FIELDS),
            confirm_message=raw.get("confirm_message"),
            kind="stock_order",
        )
    if raw.get("kind") not in (None, "webhook"):
        raise ValueError(f"action {name}: kind must be webhook or stock_order")
    url = str(raw.get("url") or "")
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"action {name}: 'url' must be an http(s) URL")
    if not raw.get("description"):
        raise ValueError(f"action {name}: 'description' is required")
    method = str(raw.get("method", "POST")).upper()
    if method not in ("POST", "PUT", "PATCH"):
        raise ValueError(f"action {name}: method must be POST, PUT or PATCH")
    fields = {str(k): str(v) for k, v in (raw.get("fields") or {}).items()}
    if not fields:
        raise ValueError(f"action {name}: 'fields' must list at least one field")
    unknown = set(raw) - {
        "kind",
        "description",
        "url",
        "method",
        "headers",
        "fields",
        "confirm_message",
        "timeout",
    }
    if unknown:
        raise ValueError(f"action {name}: unknown fields {', '.join(sorted(unknown))}")
    return ActionDef(
        name=name,
        description=str(raw["description"]),
        url=url,
        method=method,
        headers={str(k): str(v) for k, v in (raw.get("headers") or {}).items()},
        fields=fields,
        confirm_message=raw.get("confirm_message"),
        timeout=float(raw.get("timeout", 30.0)),
    )


def expand_env(value: str) -> str:
    """`${NAME}` placeholders come from the environment at send time, never from the config file."""
    return _ENV.sub(lambda m: os.environ.get(m.group(1), ""), value)


class ActionDesk:
    """One employee's approval queue."""

    def __init__(self, employee: Employee):
        self.employee = employee

    @property
    def state(self):
        return self.employee.state

    def released(self, name: str) -> bool:
        return name in self.employee.settings.releases

    def pending(self) -> list[dict[str, Any]]:
        return self.state.pending_actions()

    async def request(self, action: ActionDef, args: dict[str, str], ctx: sk.SkillContext) -> str:
        rec = self.state.add_action(
            action=action.name,
            args=args,
            contact=ctx.contact_id,
            contact_name=ctx.contact_name,
            status="pending",
        )
        if self.released(action.name):
            ok, detail = await self._execute(rec["id"], decided_by="release")
            return f"Done: {detail}" if ok else f"Error: the action failed: {detail}"
        self.employee.log("action", "queued", action=action.name, request=rec["id"], contact=ctx.contact_id)
        await self.employee.notify_admins(
            f"🔔 *Cần duyệt #{rec['id']}* — {action.name} (từ {ctx.contact_name})\n"
            + "\n".join(f"• {k}: {v}" for k, v in args.items())
            + f"\n\n/ai approve {rec['id']}  ·  /ai reject {rec['id']} <lý do>"
        )
        return (
            f"Queued as request #{rec['id']}: a manager must approve it before it happens. "
            "Tell the contact their request was received and will be confirmed shortly."
        )

    async def approve(self, action_id: int, by: str) -> str:
        rec = self.state.action(action_id)
        if rec is None:
            return f"Không có yêu cầu #{action_id}."
        if rec["status"] != "pending":
            return f"Yêu cầu #{action_id} đang ở trạng thái '{rec['status']}', không thể duyệt."
        ok, detail = await self._execute(action_id, decided_by=by)
        return f"Đã thực hiện #{action_id}: {detail}" if ok else f"#{action_id} thất bại: {detail}"

    async def reject(self, action_id: int, by: str, reason: str) -> str:
        rec = self.state.action(action_id)
        if rec is None:
            return f"Không có yêu cầu #{action_id}."
        if rec["status"] != "pending":
            return f"Yêu cầu #{action_id} đang ở trạng thái '{rec['status']}', không thể từ chối."
        self.state.update_action(
            action_id, status="rejected", decided_by=by, decided=now_iso(), reason=reason
        )
        self.employee.log("action", "rejected", action=rec["action"], request=action_id)
        await self._tell_contact(rec, "request_rejected", reason=reason)
        return f"Đã từ chối #{action_id}."

    async def _execute(self, action_id: int, decided_by: str) -> tuple[bool, str]:
        rec = self.state.update_action(
            action_id, status="executing", decided_by=decided_by, decided=now_iso()
        )
        action = self.employee.office.config.actions.get(rec["action"])
        if action is None:
            detail = f"action {rec['action']} is no longer configured"
            self.state.update_action(action_id, status="failed", result=detail)
            return False, detail
        if action.kind == "stock_order":
            ok, detail = self._stock_order(rec)
            self.state.update_action(
                action_id, status="done" if ok else "failed", result=detail, finished=now_iso()
            )
            self.employee.log(
                "action", "ok" if ok else "error", action=action.name, request=action_id, by=decided_by
            )
            if ok and decided_by != "release":
                await self._tell_contact(rec, "request_confirmed", message=action.confirm_message)
            return ok, detail
        headers = {k: expand_env(v) for k, v in action.headers.items()}
        payload = {**rec["args"], "_request_id": action_id, "_employee": self.employee.id}
        try:
            r = await self.employee.office.http_client.request(
                action.method, expand_env(action.url), json=payload, headers=headers, timeout=action.timeout
            )
            ok = 200 <= r.status_code < 300
            detail = (
                (r.text or f"HTTP {r.status_code}")[:500] if ok else f"HTTP {r.status_code}: {r.text[:300]}"
            )
        except httpx2.HTTPError as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        self.state.update_action(
            action_id, status="done" if ok else "failed", result=detail, finished=now_iso()
        )
        self.employee.log(
            "action", "ok" if ok else "error", action=action.name, request=action_id, by=decided_by
        )
        if ok and decided_by != "release":
            await self._tell_contact(rec, "request_confirmed", message=action.confirm_message)
        if not ok:
            log.warning("%s: action #%s %s failed: %s", self.employee.id, action_id, action.name, detail)
        return ok, detail

    def _stock_order(self, rec: dict[str, Any]) -> tuple[bool, str]:
        """An order in the office's own inventory: goods reserved at the customer's price."""
        from .inventory import InventoryError

        office = self.employee.office
        args = rec["args"]
        items = [
            {"sku": sku, "qty": int(qty)}
            for sku, qty in re.findall(
                r"([A-Za-z0-9][A-Za-z0-9._/-]*)\s*[x×*]\s*(\d+)", str(args.get("items", ""))
            )
        ]
        if not items:
            return False, "no 'SKU x quantity' items in the request"
        contact = rec.get("contact")
        conv = office.hub.inbox.by_contact(self.employee.id, int(contact)) if contact is not None else None
        customer = office.hub.crm.contact_of(conv.id) if conv else None
        note = str(args.get("note", "")).strip()
        try:
            order = office.inventory.create_order(
                items,
                contact_id=int(customer["id"]) if customer else None,
                conversation_id=conv.id if conv else None,
                customer_name=str(args.get("customer_name", ""))[:120],
                phone=str(args.get("phone", ""))[:40],
                address=str(args.get("address", ""))[:300],
                vip=bool(customer and customer.get("vip")),
                note="" if note == "-" else note,
                source=f"ai:{self.employee.id}#{rec['id']}",
                actor=self.employee.settings.display_name,
                # the staff member looking after the conversation earns the sale; else the AI
                salesperson=(conv.assignee if conv and conv.assignee else f"ai:{self.employee.id}"),
                channel="chat",
            )
        except InventoryError as e:
            return False, str(e)
        lines = ", ".join(f"{i['sku']} x{i['qty']} ({i['unit_price']:,})" for i in order["items"])
        return True, f"order {order['code']}: {lines}; total {order['total']:,}"

    async def _tell_contact(
        self, rec: dict[str, Any], key: str, message: str | dict[str, str] | None = None, reason: str = ""
    ) -> None:
        """Tell the customer a decision, in their language: the built-in text, or the action's
        confirm_message (per language, or translated from the staff language), plus any reason."""
        cid = rec.get("contact")
        if cid is None:
            return
        agent = self.employee.agent
        code = agent.contact_language(cid)
        if isinstance(message, dict) and message:
            text = (
                message.get(code or "") or message.get(agent.staff_language) or next(iter(message.values()))
            )
            if code and code not in message:
                text = await agent.for_contact(cid, text)
        elif isinstance(message, str) and message:
            text = await agent.for_contact(cid, message)
        else:
            text = lang.text(key, code, id=rec["id"])
        if reason:
            text = text.rstrip(".。") + ": " + await agent.for_contact(cid, reason)
        try:
            await self.employee.office.hub.send_to_contact(self.employee, cid, text)
        except Exception:
            log.exception("%s: cannot notify contact %s", self.employee.id, rec.get("contact"))
