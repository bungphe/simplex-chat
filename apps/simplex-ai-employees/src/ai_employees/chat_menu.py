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

from .i18n import has_catalog, number, tr, use_language
from .inventory import InventoryError, order_code
from .loyalty import vip_card
from .menu_i18n import MENU_TEXT

if TYPE_CHECKING:
    from .employee import Employee

log = logging.getLogger(__name__)

# keyword -> the menu entry; `params` puts "/keyword <params>" in the message field to fill in
ICONS = {
    "products": "🛋",
    "combos": "🎁",
    "orders": "📦",
    "invoice": "🧾",
    "points": "⭐",
    "shop": "🌐",
    "staff": "🙋",
}


def customer_menu(code: str | None = "vi") -> list[dict[str, Any]]:
    """The customers' menu in their language (English for languages without a translation)."""
    text = MENU_TEXT.get(code or "vi") or MENU_TEXT["en"]
    menu: list[dict[str, Any]] = []
    for keyword in ("products", "combos", "orders", "invoice", "points", "shop", "staff"):
        entry: dict[str, Any] = {
            "type": "command",
            "keyword": keyword,
            "label": f"{ICONS[keyword]} {text[keyword]}",
        }
        if f"{keyword}@" in text:
            entry["params"] = text[f"{keyword}@"]
        menu.append(entry)
    menu.append(
        {
            "type": "command",
            "keyword": "forget",
            "label": f"{text['forget']} / Forget me" if text is not MENU_TEXT["en"] else text["forget"],
        }
    )
    return menu


CUSTOMER_MENU: list[dict[str, Any]] = customer_menu("vi")
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


def _localized(entry: dict[str, Any]) -> dict[str, Any]:
    """A menu entry with its label and parameter hint in the current language."""
    out = {**entry, "label": tr(entry["label"])}
    if "params" in entry:
        out["params"] = tr(entry["params"])
    if "commands" in entry:
        out["commands"] = [_localized(c) for c in entry["commands"]]
    return out


def admin_menu_entry() -> dict[str, Any]:
    """The admins' management menu, in the current language."""
    return _localized(ADMIN_MENU)


def admin_menu() -> list[dict[str, Any]]:
    return [*customer_menu(), admin_menu_entry()]


def tap(command: str) -> str:
    """A command the customer can tap in the SimpleX apps (with its parameters)."""
    return f"/'{command}'" if " " in command else f"/{command}"


class ChatMenu:
    def __init__(self, employee: Employee):
        self.employee = employee
        self.office = employee.office

    def _money(self, v: Any) -> str:
        cur = self.office.inventory.settings()["currency"]
        return (number(v) + (tr(" đ") if cur == "VND" else f" {cur}")) if v is not None else tr("liên hệ")

    def _contact(self, cid: int, name: str) -> tuple[Any, dict[str, Any]]:
        hub = self.office.hub
        conv = hub.inbox.upsert(f"simplex:{self.employee.id}", str(cid), name, self.employee.id)
        return conv, hub.crm.observe(conv, "", "simplex")

    async def handle(self, cid: int, word: str, args: str, name: str) -> str:
        conv, contact = self._contact(cid, name)
        native = self._native(cid)
        # in the customer's language: from the catalogs when there is one, else translated
        # (prices, codes, links and tappable commands kept as they are)
        with use_language(self.employee.agent.contact_language(cid) if native else None):
            try:
                reply = await getattr(self, f"cmd_{word}")(conv, contact, args.strip())
            except InventoryError as e:
                reply = str(e)
        if word == "shop" or native:
            return reply  # /shop: translated there, the login link never goes to a model
        return await self.employee.agent.for_contact(cid, reply)

    def _native(self, cid: int) -> bool:
        """Whether the customer's language has its own catalog (no model translation needed)."""
        code = self.employee.agent.contact_language(cid)
        return code is None or has_catalog(code)

    async def cmd_help(self, conv: Any, contact: dict[str, Any], args: str) -> str:
        return tr(
            "Quý khách có thể nhắn tự nhiên, hoặc chạm vào lệnh:\n{0} – tìm sản phẩm, ví dụ {1}\n{2} – combo tiết kiệm\n{3} – đơn hàng của tôi\n{4} – điểm tích luỹ và thẻ VIP\n{5} – đăng nhập website\n{6} – gặp nhân viên",
            tap("products"),
            tap("products sofa"),
            tap("combos"),
            tap("orders"),
            tap("points"),
            tap("shop"),
            tap("staff"),
        )

    cmd_start = cmd_help

    async def cmd_products(self, conv: Any, contact: dict[str, Any], args: str) -> str:
        inv = self.office.inventory
        if not args:
            return tr("Gõ tên hoặc mã sản phẩm, ví dụ: {0}", tap("products sofa"))
        found = inv.lookup(args, bool(contact["vip"]), limit=6)
        if not found:
            return tr("Chưa tìm thấy sản phẩm “{0}”. Quý khách mô tả thêm để nhân viên tư vấn nhé.", args)
        shop = self.office.storefront
        lines = []
        for p in found:
            if p["available"]:
                stock = tr("còn {0}", p["available"])
            elif p["incoming"]:
                stock = tr("đặt trước") + (tr(", về khoảng {0}", p["next_eta"]) if p["next_eta"] else "")
            else:
                stock = tr("tạm hết")
            line = f"*{p['name']}* ({p['sku']}) – {self._money(p['price'])}{tr(' (giá VIP)') if p['vip_price'] else ''} · {stock}"
            if shop and shop.public_url:
                line += f"\n{shop.public_url}/p/{p['sku']}"
            lines.append(line)
        return "\n".join(lines)

    async def cmd_combos(self, conv: Any, contact: dict[str, Any], args: str) -> str:
        combos = [c for c in self.office.inventory.combos(bool(contact["vip"])) if c["live"]]
        if not combos:
            return tr("Hiện chưa có combo nào.")
        return "\n\n".join(
            tr(
                "🎁 *{0}* – {1} (tiết kiệm {2})\n",
                c["name"],
                self._money(c["price"]),
                self._money(c["saving"]),
            )
            + "\n".join(f"  • {i['name']} × {i['qty']}" for i in c["items"])
            + ("" if c["available"] else tr("\n  (tạm hết hàng)"))
            for c in combos[:6]
        )

    async def cmd_orders(self, conv: Any, contact: dict[str, Any], args: str) -> str:
        orders = self.office.inventory.orders(contact_id=int(contact["id"]), limit=5)
        if not orders:
            return tr("Quý khách chưa có đơn hàng nào.")
        lines = []
        for o in orders:
            due = self.office.inventory.major(
                max(0, self.office.inventory.minor(o["total"]) - self.office.inventory.minor(o["paid"]))
            )
            lines.append(
                f"*{o['code']}* · {o['created'][:10]} · {tr(STATUS.get(o['status'], o['status']))}"
                f"{tr(' · đặt trước') if o['kind'] == 'preorder' else ''} · {self._money(o['total'])}"
                + (tr(" · còn {0}", self._money(due)) if due and o["status"] == "confirmed" else "")
                + tr("\n  hoá đơn: {0}", tap("invoice " + o["code"]))
            )
        return "\n".join(lines)

    def _own_order(self, contact: dict[str, Any], code: str) -> dict[str, Any]:
        try:
            oid = int(code.strip().upper().removeprefix("DH"))
            order = self.office.inventory.order(oid)
        except (ValueError, InventoryError):
            order = None
        if order is None or order["contact_id"] != contact["id"]:
            raise InventoryError(
                tr("Không tìm thấy đơn {0} của quý khách. Xem các đơn: {1}", code, tap("orders"))
            )
        return order

    async def cmd_invoice(self, conv: Any, contact: dict[str, Any], args: str) -> str:
        if not args:
            return tr("Gõ mã đơn, ví dụ {0}, hoặc xem các đơn: {1}", tap("invoice DH00012"), tap("orders"))
        order = self._own_order(contact, args)
        text = self.office.inventory.receipt_text(int(order["id"]))
        address = order.get("email") or contact.get("email")
        if address and self.office.mailer.ready:
            from .invoices import email_invoice

            try:
                await email_invoice(self.office, int(order["id"]))
                text += tr("\n\n📧 Hoá đơn cũng đã được gửi tới {0}.", _mask(address))
            except InventoryError as e:
                log.info("chat: invoice %s not emailed: %s", order["code"], e)
        return text

    async def cmd_points(self, conv: Any, contact: dict[str, Any], args: str) -> str:
        s = self.office.inventory.settings()
        cid = int(contact["id"])
        text = tr(
            "⭐ Điểm tích luỹ: *{0}* · Đã mua: {1}",
            contact["points"],
            self._money(self.office.inventory.major(contact["total_spent"])),
        )
        if contact["vip"]:
            text += tr("\nQuý khách là khách VIP · thẻ số *{0}*: giá VIP áp dụng cho mọi đơn.", vip_card(cid))
        elif int(s["vip_points"]):
            need = max(0, int(s["vip_points"]) - int(contact["points"]))
            text += tr("\nCòn {0} điểm nữa để lên VIP.", need)
        return text

    async def cmd_shop(self, conv: Any, contact: dict[str, Any], args: str) -> str:
        shop = self.office.storefront
        agent = self.employee.agent
        native = self._native(conv.contact_id)
        if shop is None or not shop.public_url:
            text = tr("Cửa hàng chưa mở website. Quý khách cứ nhắn tại đây để đặt hàng nhé.")
            return text if native else await agent.for_contact(conv.contact_id, text)
        link = shop.magic_link(int(contact["id"]))
        intro = tr(
            "🌐 Đăng nhập website {0} bằng đường dẫn riêng dưới đây (dùng 1 lần, trong 10 phút). Đừng chuyển đường dẫn này cho người khác.",
            shop.public_url,
        )
        if not native:
            intro = await agent.for_contact(conv.contact_id, intro)
        return f"{intro}\n{link}"

    async def cmd_staff(self, conv: Any, contact: dict[str, Any], args: str) -> str:
        hub = self.office.hub
        hub.inbox.set_mode(conv.id, "human")
        hub.inbox.set_status(conv.id, "open")
        with use_language(None):  # for the shop's staff: the office's language
            hub.inbox.add_labels(conv.id, [tr("cần nhân viên")])
            hub.add_note(
                conv.id,
                tr("Khách bấm “Gặp nhân viên” trong ứng dụng SimpleX") + (f": {args}" if args else ""),
                "system",
            )
            await self.employee.notify_admins(
                tr(
                    "🙋 {0} ({1}) muốn gặp nhân viên{2}. Trả lời trong Hộp thư của trang quản trị.",
                    contact["name"] or conv.customer_name or tr("Khách"),
                    contact["phone"] or "SimpleX",
                    ": " + args if args else "",
                )
            )
        await self.office.staff_links.notify(
            "inbox",
            lambda: tr(
                "🙋 #{0} {1} muốn gặp nhân viên{2}. Xem: /'open {3}'",
                conv.id,
                contact["name"] or conv.customer_name or tr("Khách"),
                ": " + args if args else "",
                conv.id,
            ),
        )
        return tr("Đã báo nhân viên, quý khách vui lòng chờ trong giây lát. 🙏")

    # ------------------------------------------------------------------ #
    # admins

    def report(self) -> str:
        today = datetime.now().astimezone().date().isoformat()
        inv = self.office.inventory
        p = self.office.sales.pnl(today, today)
        new = inv.orders(since=today, limit=1000)
        web = [o for o in new if o.get("channel") == "web"]
        return tr(
            "*Hôm nay {0}*\nĐơn mới: {1} (website {2}) · giá trị {3}\nĐã giao: {4} đơn · doanh thu {5} · lãi gộp {6} ({7}%)",
            today,
            len(new),
            len(web),
            self._money(sum(o["total"] for o in new if o["status"] != "cancelled")),
            p["orders"],
            self._money(p["revenue"]),
            self._money(p["gross_profit"]),
            p["gross_margin_pct"],
        )

    def open_orders(self) -> str:
        orders = self.office.inventory.orders(status="confirmed", limit=15)
        if not orders:
            return tr("Không có đơn đang mở.")
        return tr("*Đơn đang mở*\n") + "\n".join(
            f"{o['code']} · {o['customer_name'] or '-'} · {o['channel'] or o['source'] or '-'}"
            f"{tr(' · đặt trước') if o['kind'] == 'preorder' else ''} · {self._money(o['total'])}"
            f"{' · giao ' + o['delivery_date'] if o['delivery_date'] else ''}"
            for o in orders
        )

    def stock(self, query: str) -> str:
        if not query:
            return tr("Cú pháp: {0}", tap(tr("ai stock <mã hoặc tên>")))
        rows = self.office.inventory.products(query, limit=8)
        if not rows:
            return tr("Không có sản phẩm “{0}”.", query)
        return "\n".join(
            tr(
                "*{0}* {1}: có thể bán {2} (tồn {3}, giữ {4})",
                p["sku"],
                p["name"],
                p["available"],
                p["on_hand"],
                p["reserved"],
            )
            + (
                tr(" · sắp về {0}", p["incoming"]) + (f" ({p['next_eta']})" if p["next_eta"] else "")
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
            return tr("Không có hàng nào sắp hết. 👍")
        return tr("*Cần nhập thêm*\n") + "\n".join(
            tr("{0} {1}: còn {2}", p["sku"], p["name"], p["available"])
            + (tr(", nên đặt {0}", p["suggest_order"]) if p["suggest_order"] else "")
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
