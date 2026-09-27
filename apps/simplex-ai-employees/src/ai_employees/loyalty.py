"""Loyalty points and automatic VIP (sale-management module 1, section 5).

Every completed sale linked to a customer earns points (1 point per `points_per` spent,
set on the inventory settings); at `vip_points` points the customer becomes VIP by
themselves: VIP prices apply from their next order, they get a congratulation message
with their VIP card number on the channel they use, and the managers are told. A
returned sale takes its points back (VIP status stays: staff can remove it).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .i18n import tr
from .state import now_iso

if TYPE_CHECKING:
    from .employee import Office

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS crm_points (
  id {id}, contact_id {int} NOT NULL, order_id {int}, delta {int} NOT NULL, reason TEXT NOT NULL, ts TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS crm_points_contact ON crm_points (contact_id, id)
"""
CONGRATS = (
    "🎉 Chúc mừng {name}! Quý khách đã trở thành khách hàng VIP của {shop} (thẻ VIP số {card}). "
    "Từ đơn tiếp theo, quý khách được giá ưu đãi dành riêng cho VIP. Cảm ơn quý khách đã tin tưởng!"
)


def vip_card(contact_id: int) -> str:
    return f"VIP{contact_id:06d}"


class Loyalty:
    def __init__(self, office: Office):
        self.office = office
        self.db = office.hub.crm.db
        self.db.script(SCHEMA)
        office.inventory.listeners.append(self.on_event)

    def on_event(self, event: str, data: dict[str, Any]) -> None:
        order = data.get("order") or {}
        if not order.get("contact_id"):
            return
        if event == "order_completed":
            self.award(order)
        elif event == "order_returned":
            self.take_back(order)

    def _minor(self, amount: Any) -> int:
        return self.office.inventory.minor(amount)

    def award(self, order: dict[str, Any]) -> dict[str, Any]:
        inv = self.office.inventory
        s = inv.settings()
        contact_id = int(order["contact_id"])
        spent = self._minor(order["total"]) - self._minor(order.get("shipping_fee") or 0)
        points = max(0, spent // max(1, self._minor(s["points_per"])))
        with self.db.transaction():
            self.db.execute(
                "UPDATE crm_contacts SET points=points+?, total_spent=total_spent+?, orders_count=orders_count+1 WHERE id=?",
                (points, spent, contact_id),
            )
            if points:
                self.db.execute(
                    "INSERT INTO crm_points (contact_id, order_id, delta, reason, ts) VALUES (?, ?, ?, ?, ?)",
                    (contact_id, order["id"], points, tr("Mua hàng {0}", order["code"]), now_iso()),
                )
            contact = self.db.row("SELECT * FROM crm_contacts WHERE id=?", (contact_id,))
            upgraded = bool(
                contact
                and not contact["vip"]
                and int(s["vip_points"])
                and int(contact["points"]) >= int(s["vip_points"])
            )
            if upgraded:
                self.db.execute(
                    "UPDATE crm_contacts SET vip=1, vip_since=? WHERE id=?", (now_iso(), contact_id)
                )
        if upgraded:
            log.info("loyalty: customer %s reached %s points and is now VIP", contact_id, contact["points"])
            self.office.hub.spawn(self._celebrate(contact_id, order))
        return {"points": points, "vip": upgraded}

    def take_back(self, order: dict[str, Any]) -> None:
        row = self.db.row(
            "SELECT SUM(delta) AS d FROM crm_points WHERE order_id=? AND contact_id=?",
            (order["id"], order["contact_id"]),
        )
        points = int(row["d"] or 0) if row else 0
        spent = self._minor(order["total"]) - self._minor(order.get("shipping_fee") or 0)
        with self.db.transaction():
            self.db.execute(
                "UPDATE crm_contacts SET points=MAX(points-?, 0), total_spent=total_spent-?, orders_count=orders_count-1 "
                "WHERE id=?".replace("MAX(", "GREATEST(" if self.db.postgres else "MAX("),
                (points, spent, order["contact_id"]),
            )
            if points:
                self.db.execute(
                    "INSERT INTO crm_points (contact_id, order_id, delta, reason, ts) VALUES (?, ?, ?, ?, ?)",
                    (order["contact_id"], order["id"], -points, tr("Trả hàng {0}", order["code"]), now_iso()),
                )

    def history(self, contact_id: int) -> list[dict[str, Any]]:
        return self.db.rows(
            "SELECT * FROM crm_points WHERE contact_id=? ORDER BY id DESC LIMIT 100", (contact_id,)
        )

    def adjust(self, contact_id: int, delta: int, reason: str) -> None:
        """Staff add or remove points by hand (a gift, a correction)."""
        with self.db.transaction():
            self.db.execute(
                "UPDATE crm_contacts SET points=MAX(points+?, 0) WHERE id=?".replace(
                    "MAX(", "GREATEST(" if self.db.postgres else "MAX("
                ),
                (delta, contact_id),
            )
            self.db.execute(
                "INSERT INTO crm_points (contact_id, delta, reason, ts) VALUES (?, ?, ?, ?)",
                (contact_id, delta, reason[:150] or tr("Điều chỉnh"), now_iso()),
            )

    async def _celebrate(self, contact_id: int, order: dict[str, Any]) -> None:
        crm, hub = self.office.hub.crm, self.office.hub
        contact = crm.contact(contact_id) or {}
        shop = self.office.inventory.settings()["shop_name"] or tr("cửa hàng")
        text = tr(CONGRATS).format(
            name=contact.get("name") or tr("quý khách"), shop=shop, card=vip_card(contact_id)
        )
        web = getattr(self.office, "storefront", None)
        if web is not None and web.public_url:
            text += tr(
                " Quý khách có thể đăng nhập {0}/login để mua với giá VIP và xem điểm, hoá đơn.",
                web.public_url,
            )
        convs = crm.conversations(contact_id)
        target = order.get("conversation_id") or (convs[-1] if convs else None)
        sent = await hub.notify_customer(int(target), text) if target else False
        conv = hub.inbox.conversation(int(target)) if target else None
        employee = hub.employee_for(conv) if conv else next(iter(self.office.employees.values()), None)
        if employee is not None:
            await employee.notify_admins(
                tr(
                    "⭐ Khách {0} ({1}) vừa lên VIP ({2} điểm, đã mua {3:,}). ",
                    contact.get("name") or contact_id,
                    contact.get("phone") or "-",
                    contact.get("points"),
                    self.office.inventory.major(contact.get("total_spent") or 0),
                )
                + (tr("Đã gửi lời chúc mừng.") if sent else tr("Chưa gửi được lời chúc mừng: liên hệ khách."))
            )
