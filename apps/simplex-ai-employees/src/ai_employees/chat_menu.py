"""The shop in the SimpleX apps: the command menu and self-service commands.

The official SimpleX apps (Android, iOS, desktop; v6.4.3 or later) show a bot's commands
as a menu (type "/" or tap the "//" button next to the message field) and make
"/command" text in the bot's messages tappable. Nothing has to change in the apps: the
AI employee's profile declares the menu, and each admin gets a longer menu with the
shop's management commands (a per-contact preference, which only that contact sees).

Customers: products and prices (VIP prices for VIP customers), combos, their orders,
invoices, points and VIP card, a one-tap login link to the web shop, and asking for a
person. Admins: today's sales, open orders, stock, low stock, approvals and the AI
employee's own settings.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .inventory import InventoryError, order_code
from .loyalty import vip_card

if TYPE_CHECKING:
    from .employee import Employee

log = logging.getLogger(__name__)

# keyword -> the menu entry; `params` puts "/keyword <params>" in the message field to fill in
CUSTOMER_MENU: list[dict[str, Any]] = [
    {"type": "command", "keyword": "products", "label": "🛋 Tìm sản phẩm & giá", "params": "<tên hoặc mã>"},
    {"type": "command", "keyword": "combos", "label": "🎁 Combo tiết kiệm"},
    {"type": "command", "keyword": "orders", "label": "📦 Đơn hàng của tôi"},
    {"type": "command", "keyword": "invoice", "label": "🧾 Nhận hoá đơn", "params": "<mã đơn>"},
    {"type": "command", "keyword": "points", "label": "⭐ Điểm tích luỹ & thẻ VIP"},
    {"type": "command", "keyword": "shop", "label": "🌐 Đăng nhập website"},
    {"type": "command", "keyword": "staff", "label": "🙋 Gặp nhân viên"},
    {"type": "command", "keyword": "forget", "label": "Xoá lịch sử trò chuyện / Forget me"},
]
ADMIN_MENU: dict[str, Any] = {
    "type": "menu",
    "label": "🔧 Quản lý cửa hàng",
    "commands": [
        {"type": "command", "keyword": "ai report", "label": "📊 Doanh thu hôm nay"},
        {"type": "command", "keyword": "ai orders", "label": "📦 Đơn đang mở"},
        {"type": "command", "keyword": "ai stock", "label": "🏬 Tồn kho", "params": "<mã hoặc tên>"},
        {"type": "command", "keyword": "ai lowstock", "label": "⚠️ Hàng sắp hết"},
        {"type": "command", "keyword": "ai pending", "label": "✅ Yêu cầu chờ duyệt"},
        {"type": "command", "keyword": "ai show", "label": "🤖 Cấu hình nhân viên AI"},
        {"type": "command", "keyword": "ai help", "label": "❓ Tất cả lệnh quản trị"},
    ],
}
CUSTOMER_WORDS = {"products", "combos", "orders", "invoice", "points", "shop", "staff", "help", "start"}
STATUS = {
    "confirmed": "đã xác nhận",
    "completed": "hoàn tất",
    "cancelled": "đã huỷ",
    "returned": "đã trả hàng",
}


def admin_menu() -> list[dict[str, Any]]:
    return [*CUSTOMER_MENU, ADMIN_MENU]


def tap(command: str) -> str:
    """A command the customer can tap in the SimpleX apps (with its parameters)."""
    return f"/'{command}'" if " " in command else f"/{command}"


class ChatMenu:
    def __init__(self, employee: Employee):
        self.employee = employee
        self.office = employee.office

    def _money(self, v: Any) -> str:
        cur = self.office.inventory.settings()["currency"]
        return (
            (f"{v:,}".replace(",", ".") + (" đ" if cur == "VND" else f" {cur}"))
            if v is not None
            else "liên hệ"
        )

    def _contact(self, cid: int, name: str) -> tuple[Any, dict[str, Any]]:
        hub = self.office.hub
        conv = hub.inbox.upsert(f"simplex:{self.employee.id}", str(cid), name, self.employee.id)
        return conv, hub.crm.observe(conv, "", "simplex")

    async def handle(self, cid: int, word: str, args: str, name: str) -> str:
        conv, contact = self._contact(cid, name)
        try:
            return await getattr(self, f"cmd_{word}")(conv, contact, args.strip())
        except InventoryError as e:
            return str(e)

    async def cmd_help(self, conv: Any, contact: dict[str, Any], args: str) -> str:
        return (
            "Quý khách có thể nhắn tự nhiên, hoặc chạm vào lệnh:\n"
            f"{tap('products')} – tìm sản phẩm, ví dụ {tap('products sofa')}\n"
            f"{tap('combos')} – combo tiết kiệm\n"
            f"{tap('orders')} – đơn hàng của tôi\n"
            f"{tap('points')} – điểm tích luỹ và thẻ VIP\n"
            f"{tap('shop')} – đăng nhập website\n"
            f"{tap('staff')} – gặp nhân viên"
        )

    cmd_start = cmd_help

    async def cmd_products(self, conv: Any, contact: dict[str, Any], args: str) -> str:
        inv = self.office.inventory
        if not args:
            return f"Gõ tên hoặc mã sản phẩm, ví dụ: {tap('products sofa')}"
        found = inv.lookup(args, bool(contact["vip"]), limit=6)
        if not found:
            return f"Chưa tìm thấy sản phẩm “{args}”. Quý khách mô tả thêm để nhân viên tư vấn nhé."
        shop = self.office.storefront
        lines = []
        for p in found:
            if p["available"]:
                stock = f"còn {p['available']}"
            elif p["incoming"]:
                stock = "đặt trước" + (f", về khoảng {p['next_eta']}" if p["next_eta"] else "")
            else:
                stock = "tạm hết"
            line = f"*{p['name']}* ({p['sku']}) – {self._money(p['price'])}{' (giá VIP)' if p['vip_price'] else ''} · {stock}"
            if shop and shop.public_url:
                line += f"\n{shop.public_url}/p/{p['sku']}"
            lines.append(line)
        return "\n".join(lines)

    async def cmd_combos(self, conv: Any, contact: dict[str, Any], args: str) -> str:
        combos = [c for c in self.office.inventory.combos(bool(contact["vip"])) if c["live"]]
        if not combos:
            return "Hiện chưa có combo nào."
        return "\n\n".join(
            f"🎁 *{c['name']}* – {self._money(c['price'])} (tiết kiệm {self._money(c['saving'])})\n"
            + "\n".join(f"  • {i['name']} × {i['qty']}" for i in c["items"])
            + ("" if c["available"] else "\n  (tạm hết hàng)")
            for c in combos[:6]
        )

    async def cmd_orders(self, conv: Any, contact: dict[str, Any], args: str) -> str:
        orders = self.office.inventory.orders(contact_id=int(contact["id"]), limit=5)
        if not orders:
            return "Quý khách chưa có đơn hàng nào."
        lines = []
        for o in orders:
            due = self.office.inventory.major(
                max(0, self.office.inventory.minor(o["total"]) - self.office.inventory.minor(o["paid"]))
            )
            lines.append(
                f"*{o['code']}* · {o['created'][:10]} · {STATUS.get(o['status'], o['status'])}"
                f"{' · đặt trước' if o['kind'] == 'preorder' else ''} · {self._money(o['total'])}"
                + (f" · còn {self._money(due)}" if due and o["status"] == "confirmed" else "")
                + f"\n  hoá đơn: {tap('invoice ' + o['code'])}"
            )
        return "\n".join(lines)

    def _own_order(self, contact: dict[str, Any], code: str) -> dict[str, Any]:
        try:
            oid = int(code.strip().upper().removeprefix("DH"))
            order = self.office.inventory.order(oid)
        except (ValueError, InventoryError):
            order = None
        if order is None or order["contact_id"] != contact["id"]:
            raise InventoryError(f"Không tìm thấy đơn {code} của quý khách. Xem các đơn: {tap('orders')}")
        return order

    async def cmd_invoice(self, conv: Any, contact: dict[str, Any], args: str) -> str:
        if not args:
            return f"Gõ mã đơn, ví dụ {tap('invoice DH00012')}, hoặc xem các đơn: {tap('orders')}"
        order = self._own_order(contact, args)
        text = self.office.inventory.receipt_text(int(order["id"]))
        address = order.get("email") or contact.get("email")
        if address and self.office.mailer.ready:
            from .invoices import email_invoice

            try:
                await email_invoice(self.office, int(order["id"]))
                text += f"\n\n📧 Hoá đơn cũng đã được gửi tới {_mask(address)}."
            except InventoryError as e:
                log.info("chat: invoice %s not emailed: %s", order["code"], e)
        return text

    async def cmd_points(self, conv: Any, contact: dict[str, Any], args: str) -> str:
        s = self.office.inventory.settings()
        cid = int(contact["id"])
        text = f"⭐ Điểm tích luỹ: *{contact['points']}* · Đã mua: {self._money(self.office.inventory.major(contact['total_spent']))}"
        if contact["vip"]:
            text += f"\nQuý khách là khách VIP · thẻ số *{vip_card(cid)}*: giá VIP áp dụng cho mọi đơn."
        elif int(s["vip_points"]):
            need = max(0, int(s["vip_points"]) - int(contact["points"]))
            text += f"\nCòn {need} điểm nữa để lên VIP."
        return text

    async def cmd_shop(self, conv: Any, contact: dict[str, Any], args: str) -> str:
        shop = self.office.storefront
        if shop is None or not shop.public_url:
            return "Cửa hàng chưa mở website. Quý khách cứ nhắn tại đây để đặt hàng nhé."
        link = shop.magic_link(int(contact["id"]))
        return (
            f"🌐 Đăng nhập website {shop.public_url} bằng đường dẫn riêng này (dùng 1 lần, trong 10 phút):\n{link}\n"
            "Đừng chuyển đường dẫn này cho người khác."
        )

    async def cmd_staff(self, conv: Any, contact: dict[str, Any], args: str) -> str:
        hub = self.office.hub
        hub.inbox.set_mode(conv.id, "human")
        hub.inbox.set_status(conv.id, "open")
        hub.inbox.add_labels(conv.id, ["cần nhân viên"])
        hub.add_note(
            conv.id,
            "Khách bấm “Gặp nhân viên” trong ứng dụng SimpleX" + (f": {args}" if args else ""),
            "system",
        )
        await self.employee.notify_admins(
            f"🙋 {contact['name'] or conv.customer_name or 'Khách'} ({contact['phone'] or 'SimpleX'}) muốn gặp nhân viên"
            f"{': ' + args if args else ''}. Trả lời trong Hộp thư của trang quản trị."
        )
        await self.office.staff_links.notify(
            "inbox",
            f"🙋 #{conv.id} {contact['name'] or conv.customer_name or 'Khách'} muốn gặp nhân viên"
            f"{': ' + args if args else ''}. Xem: /'open {conv.id}'",
        )
        return "Đã báo nhân viên, quý khách vui lòng chờ trong giây lát. 🙏"

    # ------------------------------------------------------------------ #
    # admins

    def report(self) -> str:
        today = datetime.now().astimezone().date().isoformat()
        inv = self.office.inventory
        p = self.office.sales.pnl(today, today)
        new = inv.orders(since=today, limit=1000)
        web = [o for o in new if o.get("channel") == "web"]
        return (
            f"*Hôm nay {today}*\n"
            f"Đơn mới: {len(new)} (website {len(web)}) · giá trị {self._money(sum(o['total'] for o in new if o['status'] != 'cancelled'))}\n"
            f"Đã giao: {p['orders']} đơn · doanh thu {self._money(p['revenue'])} · lãi gộp {self._money(p['gross_profit'])}"
            f" ({p['gross_margin_pct']}%)"
        )

    def open_orders(self) -> str:
        orders = self.office.inventory.orders(status="confirmed", limit=15)
        if not orders:
            return "Không có đơn đang mở."
        return "*Đơn đang mở*\n" + "\n".join(
            f"{o['code']} · {o['customer_name'] or '-'} · {o['channel'] or o['source'] or '-'}"
            f"{' · đặt trước' if o['kind'] == 'preorder' else ''} · {self._money(o['total'])}"
            f"{' · giao ' + o['delivery_date'] if o['delivery_date'] else ''}"
            for o in orders
        )

    def stock(self, query: str) -> str:
        if not query:
            return f"Cú pháp: {tap('ai stock <mã hoặc tên>')}"
        rows = self.office.inventory.products(query, limit=8)
        if not rows:
            return f"Không có sản phẩm “{query}”."
        return "\n".join(
            f"*{p['sku']}* {p['name']}: có thể bán {p['available']} (tồn {p['on_hand']}, giữ {p['reserved']})"
            + (
                f" · sắp về {p['incoming']}" + (f" ({p['next_eta']})" if p["next_eta"] else "")
                if p["incoming"]
                else ""
            )
            + (
                " · " + ", ".join(f"{w['code']}: {w['on_hand']}" for w in p["by_warehouse"])
                if p["by_warehouse"]
                else ""
            )
            for p in rows
        )

    def low_stock(self) -> str:
        rows = [p for p in self.office.inventory.products(limit=500) if p["level"] in ("empty", "reorder")]
        if not rows:
            return "Không có hàng nào sắp hết. 👍"
        return "*Cần nhập thêm*\n" + "\n".join(
            f"{p['sku']} {p['name']}: còn {p['available']}"
            + (f", nên đặt {p['suggest_order']}" if p["suggest_order"] else "")
            for p in rows[:20]
        )

    async def sync_admin_menu(self, contact_id: int) -> None:
        """Give an admin the longer menu (only this contact sees it)."""
        await self.employee.staff.sync_menu(contact_id)


def _mask(email: str) -> str:
    name, _, domain = email.partition("@")
    return f"{name[:2]}***@{domain}"


def order_ref(oid: int) -> str:
    return tap(f"invoice {order_code(oid)}")
