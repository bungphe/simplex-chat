"""Staff at work in the SimpleX apps: the inbox, the counter, the warehouse and deliveries
as chat commands, with a menu for each role.

A staff member links their SimpleX chat with the AI employee to their staff account:
in the admin web UI (Tài khoản → Liên kết SimpleX) they get a one-time code and send
`/link <code>` to the AI employee. From then on that chat has their role: the menu the
SimpleX apps show them (a per-contact preference) lists what their role may do, and
every command is checked against the role, as in the web UI. They are also told about
their work there: customers asking for a person, new web orders.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .inventory import PAYMENT_METHODS, InventoryError
from .users import ROLE_AREAS, User, Users

if TYPE_CHECKING:
    from .employee import Employee, Office

log = logging.getLogger(__name__)

KEY = "simplex_staff"
CODE_MINUTES = 15

# command -> (area it belongs to, menu entry); area "*" : any linked staff member,
# "approve": admins and managers
COMMANDS: dict[str, tuple[str, dict[str, Any]]] = {
    "inbox": ("inbox", {"label": "💬 Khách đang chờ"}),
    "open": ("inbox", {"label": "📖 Xem hội thoại", "params": "<số hội thoại>"}),
    "reply": ("inbox", {"label": "✍️ Trả lời khách", "params": "<số hội thoại> <nội dung>"}),
    "aion": ("inbox", {"label": "🤖 Trả lại cho AI", "params": "<số hội thoại>"}),
    "close": ("inbox", {"label": "✔️ Đóng hội thoại", "params": "<số hội thoại>"}),
    "sell": ("pos", {"label": "🧾 Tạo đơn", "params": "<mã SP> <SL>, <mã SP> <SL>; <SĐT> <tên khách>"}),
    "order": ("pos", {"label": "🔎 Xem đơn", "params": "<mã đơn>"}),
    "pay": ("pos", {"label": "💵 Thu tiền", "params": "<mã đơn> <cash|card|transfer|wallet|cod> [số tiền]"}),
    "done": ("pos", {"label": "📦 Đã giao tại quầy", "params": "<mã đơn>"}),
    "sales": ("pos", {"label": "📋 Đơn của tôi hôm nay"}),
    "stock": ("inventory-read", {"label": "🏬 Tồn kho", "params": "<mã hoặc tên>"}),
    "lowstock": ("inventory-read", {"label": "⚠️ Hàng sắp hết"}),
    "incoming": ("inventory", {"label": "🚢 Hàng đang về"}),
    "receive": ("inventory", {"label": "📥 Nhận đủ đơn nhập", "params": "<số đơn nhập>"}),
    "trips": ("delivery", {"label": "🚚 Chuyến giao hôm nay"}),
    "go": ("delivery", {"label": "▶️ Bắt đầu chuyến", "params": "<mã chuyến>"}),
    "delivered": ("delivery", {"label": "✅ Đã giao", "params": "<số lịch giao>"}),
    "failed": ("delivery", {"label": "↩️ Không giao được", "params": "<số lịch giao> <lý do>"}),
    "report": ("reports", {"label": "📊 Doanh thu hôm nay"}),
    "openorders": ("reports", {"label": "📦 Đơn đang mở"}),
    "approvals": ("approve", {"label": "✅ Yêu cầu chờ duyệt"}),
    "approve": ("approve", {"label": "👍 Duyệt", "params": "<số>"}),
    "reject": ("approve", {"label": "👎 Từ chối", "params": "<số> [lý do]"}),
    "me": ("*", {"label": "👤 Tài khoản của tôi"}),
    "unlink": ("*", {"label": "🔌 Huỷ liên kết"}),
}
GROUPS = [
    ("💬 Hộp thư", ("inbox", "open", "reply", "aion", "close")),
    ("🧾 Bán hàng", ("sell", "order", "pay", "done", "sales")),
    ("🏬 Kho", ("stock", "lowstock", "incoming", "receive")),
    ("🚚 Giao hàng", ("trips", "go", "delivered", "failed")),
    ("📊 Quản lý", ("report", "openorders", "approvals", "approve", "reject")),
]
STAFF_WORDS = {*COMMANDS, "link"}


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def allowed(user: User, command: str) -> bool:
    area = COMMANDS[command][0]
    if user.is_admin or area == "*":
        return True
    if area == "approve":
        return user.role == "manager"
    areas = ROLE_AREAS.get(user.role, ())
    if area.endswith("-read"):
        return area in areas or area[:-5] in areas
    return area in areas


def staff_menu(user: User) -> list[dict[str, Any]]:
    """The menu the SimpleX apps show this staff member: their role's commands."""
    menu: list[dict[str, Any]] = []
    for label, words in GROUPS:
        items = [{"type": "command", "keyword": w, **COMMANDS[w][1]} for w in words if allowed(user, w)]
        if items:
            menu.append({"type": "menu", "label": label, "commands": items})
    menu += [{"type": "command", "keyword": w, **COMMANDS[w][1]} for w in ("me", "unlink")]
    return menu


class StaffLinks:
    """Which SimpleX chats belong to which staff accounts (shared by every process)."""

    def __init__(self, office: Office):
        self.office = office
        self.users = Users(office.docs, "")

    def _doc(self) -> dict[str, Any]:
        return self.office.docs.get(KEY) or {}

    def new_code(self, username: str) -> str:
        code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
        expires = time.time() + CODE_MINUTES * 60

        def change(d: dict[str, Any]) -> None:
            codes = {k: v for k, v in d.setdefault("codes", {}).items() if v["expires"] > time.time()}
            codes[_sha(code)] = {"username": username, "expires": expires}
            d["codes"] = codes

        self.office.docs.update(KEY, change, {})
        return code

    def redeem(self, code: str, employee: str, contact_id: int) -> User | None:
        key = _sha(code.strip().upper())

        def change(d: dict[str, Any]) -> str | None:
            found = d.setdefault("codes", {}).pop(key, None)
            if not found or found["expires"] < time.time():
                return None
            d.setdefault("contacts", {})[f"{employee}:{contact_id}"] = {
                "username": found["username"],
                "since": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            return str(found["username"])

        username = self.office.docs.update(KEY, change, {})
        return self.users.get(username) if username else None

    def user(self, employee: str, contact_id: int) -> User | None:
        link = self._doc().get("contacts", {}).get(f"{employee}:{contact_id}")
        return self.users.get(link["username"]) if link else None  # a disabled account loses it

    def unlink(self, employee: str, contact_id: int) -> None:
        self.office.docs.update(
            KEY, lambda d: d.setdefault("contacts", {}).pop(f"{employee}:{contact_id}", None), {}
        )

    def of_user(self, username: str) -> list[dict[str, Any]]:
        out = []
        for key, link in self._doc().get("contacts", {}).items():
            if link["username"] == username:
                employee, _, cid = key.partition(":")
                e = self.office.employees.get(employee)
                out.append(
                    {
                        "key": key,
                        "employee": employee,
                        "employee_name": e.base.display_name if e else employee,
                        "contact_id": int(cid),
                        "since": link["since"],
                    }
                )
        return out

    def remove(self, username: str, key: str) -> bool:
        def change(d: dict[str, Any]) -> bool:
            link = d.setdefault("contacts", {}).get(key)
            if link and link["username"] == username:
                del d["contacts"][key]
                return True
            return False

        return bool(self.office.docs.update(KEY, change, {}))

    def linked(self) -> list[tuple[str, int, User]]:
        out = []
        for key, link in self._doc().get("contacts", {}).items():
            user = self.users.get(link["username"])
            if user is not None:
                employee, _, cid = key.partition(":")
                out.append((employee, int(cid), user))
        return out

    async def notify(self, area: str, text: str) -> int:
        """Tell the linked staff whose role covers `area` (inbox, pos, ...)."""
        sent = 0
        for employee_id, cid, user in self.linked():
            employee = self.office.employees.get(employee_id)
            if employee is None or not (user.is_admin or area in ROLE_AREAS.get(user.role, ())):
                continue
            try:
                await self.office.cluster.simplex_send(employee, cid, text)
                sent += 1
            except Exception:  # noqa: BLE001 - one unreachable phone must not stop the others
                log.warning("staff: could not notify %s (%s:%s)", user.username, employee_id, cid)
        return sent


class StaffChat:
    """The commands of one AI employee's SimpleX account, for linked staff."""

    def __init__(self, employee: Employee):
        self.employee = employee
        self.office = employee.office

    @property
    def links(self) -> StaffLinks:
        return self.office.staff_links

    def _money(self, v: Any) -> str:
        return self.employee.menu._money(v)

    async def handle(self, cid: int, word: str, args: str) -> str:
        if word == "link":
            user = self.links.redeem(args, self.employee.id, cid) if args.strip() else None
            if user is None:
                return "Mã liên kết không đúng hoặc đã hết hạn. Lấy mã mới trong trang quản trị: Tài khoản → Liên kết SimpleX."
            await self.sync_menu(cid)
            return f"Đã liên kết với tài khoản {user.name} ({user.role}). Gõ / hoặc bấm // để xem các lệnh của bạn."
        user = self.links.user(self.employee.id, cid)
        if user is None:
            return "Lệnh dành cho nhân viên. Liên kết trước: trang quản trị → Tài khoản → Liên kết SimpleX, rồi gửi /link <mã>."
        if not allowed(user, word):
            return "Vai trò của bạn không dùng được lệnh này."
        try:
            result = getattr(self, f"cmd_{word}")(user, args.strip(), cid)
            return await result if hasattr(result, "__await__") else result
        except (InventoryError, ValueError) as e:
            return str(e)

    async def sync_menu(self, cid: int) -> None:
        """This contact's menu: their role's commands (plus the admin menu for AI admins)."""
        from .chat_menu import ADMIN_MENU, CUSTOMER_MENU

        user = self.links.user(self.employee.id, cid)
        commands = staff_menu(user) if user else list(CUSTOMER_MENU)
        if self.employee.state.is_admin(cid):
            commands.append(ADMIN_MENU)
        try:
            await self.employee.bot.api.api_set_contact_prefs(cid, {"commands": commands})
        except Exception:  # noqa: BLE001 - typed commands work without the menu
            log.warning("%s: could not set the menu of contact %s", self.employee.id, cid)

    # ------------------------------------------------------------------ #

    def cmd_me(self, user: User, args: str, cid: int) -> str:
        areas = "tất cả" if user.is_admin else ", ".join(ROLE_AREAS.get(user.role, ())) or "-"
        return f"👤 {user.name} ({user.username}) · vai trò {user.role} · phần việc: {areas}"

    async def cmd_unlink(self, user: User, args: str, cid: int) -> str:
        self.links.unlink(self.employee.id, cid)
        await self.sync_menu(cid)
        return "Đã huỷ liên kết chat này với tài khoản nhân viên."

    # inbox

    def _conv(self, user: User, ref: str) -> Any:
        num = ref.strip().lstrip("#")
        conv = self.office.hub.inbox.conversation(int(num)) if num.isdigit() else None
        if conv is None or not user.sees(conv.channel):
            raise InventoryError(f"Không có hội thoại #{num}. Xem danh sách: /inbox")
        return conv

    def cmd_inbox(self, user: User, args: str, cid: int) -> str:
        inbox = self.office.hub.inbox
        convs = [c for c in inbox.list(status="open", limit=100) if user.sees(c.channel)]
        waiting = [c for c in convs if c.waiting_since or c.unread or c.mode == "human"][:12]
        if not waiting:
            return "Không có khách nào đang chờ. 👍"
        lines = []
        for c in waiting:
            who = "🙋 người" if c.mode == "human" else "🤖 AI"
            lines.append(
                f"#{c.id} *{c.customer_name or c.external_id}* · {c.channel.split(':')[0]} · {who}"
                f"{' · ' + str(c.unread) + ' tin mới' if c.unread else ''}\n  {c.last_preview[:80]}\n  /'open {c.id}'"
            )
        return "*Khách đang chờ*\n" + "\n".join(lines)

    def cmd_open(self, user: User, args: str, cid: int) -> str:
        conv = self._conv(user, args)
        msgs = self.office.hub.inbox.messages(conv.id, limit=10)
        names = {
            "customer": conv.customer_name or "Khách",
            "ai": "AI",
            "human": "NV",
            "system": "hệ thống",
            "note": "ghi chú",
        }
        body = "\n".join(f"[{names.get(m['sender'], m['sender'])}] {m['text'][:300]}" for m in msgs)
        return (
            f"*#{conv.id} {conv.customer_name or conv.external_id}* ({conv.channel.split(':')[0]}, "
            f"{'người trả lời' if conv.mode == 'human' else 'AI trả lời'})\n{body}\n\n"
            f"Trả lời: /reply {conv.id} <nội dung> · trả lại AI: /'aion {conv.id}' · đóng: /'close {conv.id}'"
        )

    async def cmd_reply(self, user: User, args: str, cid: int) -> str:
        ref, _, text = args.partition(" ")
        conv = self._conv(user, ref)
        if not text.strip():
            return f"Cú pháp: /reply {conv.id} <nội dung>"
        await self.office.hub.human_reply(conv.id, text.strip(), user.name, take_over=True, translate=True)
        return f"Đã gửi cho {conv.customer_name or 'khách'} (#{conv.id}). AI tạm dừng ở hội thoại này: /'aion {conv.id}' để trả lại."

    def cmd_aion(self, user: User, args: str, cid: int) -> str:
        conv = self._conv(user, args)
        self.office.hub.inbox.set_mode(conv.id, "ai")
        return f"#{conv.id}: AI trả lời tiếp."

    def cmd_close(self, user: User, args: str, cid: int) -> str:
        conv = self._conv(user, args)
        self.office.hub.inbox.set_status(conv.id, "closed")
        return f"Đã đóng hội thoại #{conv.id}."

    # the counter

    def _order(self, user: User, code: str) -> dict[str, Any]:
        try:
            order = self.office.inventory.order(int(code.strip().upper().removeprefix("DH")))
        except (ValueError, InventoryError):
            raise InventoryError(f"Không có đơn {code}") from None
        if not (user.is_admin or user.role == "manager"):
            today = datetime.now().astimezone().date().isoformat()
            if order["salesperson"] != user.username or not order["created"].startswith(today):
                raise InventoryError(f"Không có đơn {code} của bạn hôm nay")
        return order

    def _describe(self, o: dict[str, Any]) -> str:
        items = "\n".join(
            f"  • {i['name']} × {i['qty']} = {self._money(i['line_total'])}" for i in o["items"]
        )
        return (
            f"*{o['code']}* · {o['customer_name'] or 'Khách lẻ'} {o['phone']} · {o['status']}"
            f"{' · đặt trước' if o['kind'] == 'preorder' else ''}\n{items}\n"
            f"Tổng {self._money(o['total'])} · đã trả {self._money(o['paid'])} · còn {self._money(o['due'])}"
        )

    def cmd_sell(self, user: User, args: str, cid: int) -> str:
        lines, _, who = args.partition(";")
        items = []
        for part in lines.split(","):
            words = part.split()
            if not words:
                continue
            qty = int(words[-1]) if len(words) > 1 and words[-1].isdigit() else 1
            sku = words[0]
            items.append({"sku": sku, "qty": qty})
        if not items:
            return "Cú pháp: /sell SOFA-01 1, GHE-02 4; 0901234567 Chị Lan"
        phone, _, name = who.strip().partition(" ")
        crm = self.office.hub.crm
        contact = None
        if phone:
            from .crm import phone_key

            row = crm.db.row(
                "SELECT id FROM crm_contacts WHERE phone_key=? ORDER BY id LIMIT 1", (phone_key(phone),)
            )
            contact = crm.contact(int(row["id"])) if row else crm.create_contact(name.strip(), phone)
        order = self.office.inventory.create_order(
            items,
            contact_id=int(contact["id"]) if contact else None,
            customer_name=name.strip() or (contact or {}).get("name", ""),
            phone=phone,
            vip=bool(contact and contact["vip"]),
            source="pos",
            channel="pos",
            salesperson=user.username,
            actor=user.name,
        )
        return (
            self._describe(order) + f"\nThu tiền: /pay {order['code']} cash · giao: /'done {order['code']}'"
        )

    def cmd_order(self, user: User, args: str, cid: int) -> str:
        return self._describe(self._order(user, args))

    def cmd_pay(self, user: User, args: str, cid: int) -> str:
        words = args.split()
        if len(words) < 2 or words[1] not in PAYMENT_METHODS:
            return (
                f"Cú pháp: /pay <mã đơn> <{'|'.join(m for m in PAYMENT_METHODS if m != 'refund')}> [số tiền]"
            )
        order = self._order(user, words[0])
        o = self.office.inventory.add_payment(
            int(order["id"]),
            words[1],
            amount=words[2].replace(".", "") if len(words) > 2 else None,
            cashier=user.username,
            # the same command twice within 30 s (a double tap) records one payment
            idempotency_key=f"chat-{cid}-{_sha(args)[:16]}-{int(time.time() // 30)}",
        )
        return self._describe(o)

    def cmd_done(self, user: User, args: str, cid: int) -> str:
        order = self._order(user, args)
        return (
            self._describe(self.office.inventory.complete_order(int(order["id"]), user.name))
            + "\n✅ Đã xuất kho."
        )

    def cmd_sales(self, user: User, args: str, cid: int) -> str:
        today = (
            datetime.now()
            .astimezone()
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .isoformat(timespec="seconds")
        )
        orders = self.office.inventory.orders(salesperson=user.username, since=today)
        if not orders:
            return "Hôm nay bạn chưa có đơn."
        total = sum(o["total"] for o in orders if o["status"] != "cancelled")
        return f"*Đơn của bạn hôm nay* ({len(orders)} đơn, {self._money(total)})\n" + "\n".join(
            f"{o['code']} · {o['customer_name'] or 'Khách lẻ'} · {o['status']} · {self._money(o['total'])}"
            for o in orders[:20]
        )

    # the warehouse

    def cmd_stock(self, user: User, args: str, cid: int) -> str:
        return self.employee.menu.stock(args)

    def cmd_lowstock(self, user: User, args: str, cid: int) -> str:
        return self.employee.menu.low_stock()

    def cmd_incoming(self, user: User, args: str, cid: int) -> str:
        inv = self.office.inventory
        rows = [p for s in ("ordered", "shipping", "arrived", "partial") for p in inv.pos(s)]
        if not rows:
            return "Không có đơn nhập nào đang về."
        return "*Hàng đang về*\n" + "\n".join(
            f"{p['po_number']} · {p['supplier_name']} → {p['warehouse_name']} · {p['status']}"
            f"{' · ETA ' + p['eta'] if p['eta'] else ''} · {p['qty']} sp\n  nhận đủ: /'receive {p['po_number']}'"
            for p in rows[:15]
        )

    def cmd_receive(self, user: User, args: str, cid: int) -> str:
        inv = self.office.inventory
        row = inv.db.row("SELECT id FROM inv_purchase_orders WHERE po_number=?", (args.strip().upper(),))
        if row is None:
            return f"Không có đơn nhập {args}. Xem: /incoming"
        po = inv.po(int(row["id"]))
        items = [
            {
                "item_id": i["id"],
                "qty": int(i["qty_ordered"]) - int(i["qty_received"]) - int(i["qty_damaged"]),
            }
            for i in po["items"]
        ]
        items = [i for i in items if i["qty"] > 0]
        if not items:
            return f"{po['po_number']} đã nhận đủ."
        result = inv.receive_po(int(row["id"]), items, actor=user.name)
        served = result.get("served_preorders") or []
        return (
            f"📥 Đã nhận {sum(i['qty'] for i in items)} sản phẩm của {po['po_number']} vào kho."
            + (f" Đã giữ hàng cho {len(served)} đơn đặt trước." if served else "")
            + " Hàng hỏng hay thiếu: sửa trong trang Kho hàng."
        )

    # deliveries

    def cmd_trips(self, user: User, args: str, cid: int) -> str:
        today = datetime.now().astimezone().date().isoformat()
        routes = [
            r for r in self.office.delivery.routes(today, today) if r["status"] in ("planned", "in_progress")
        ]
        if not routes:
            return "Hôm nay không có chuyến giao nào."
        out = []
        for r in routes:
            head = f"*{r['code']}* · {r['status']} · {r['carrier_name']}" + (
                f" · {r['driver']['name']}" if r["driver"] else ""
            )
            if r["status"] == "planned":
                head += f"\n  bắt đầu: /'go {r['code']}'"
            stops = []
            for n, s in enumerate(r["stops"], 1):
                b = s["booking"]
                line = f"  {n}. #{b['id']} {b['customer_name']} {b['phone']} · {b['address']}"
                if s["status"] == "pending" and r["status"] == "in_progress":
                    line += f"\n     /'delivered {b['id']}' · /failed {b['id']} <lý do>"
                elif s["status"] != "pending":
                    line += f" · {'✅' if s['status'] == 'done' else '↩️'}"
                stops.append(line)
            out.append(head + "\n" + "\n".join(stops))
        return "\n\n".join(out)

    def _route_id(self, ref: str) -> int:
        num = ref.strip().upper().removeprefix("CX")
        if not num.isdigit():
            raise InventoryError("Cú pháp: /go CX00001")
        return int(num)

    async def cmd_go(self, user: User, args: str, cid: int) -> str:
        r = await self.office.delivery.start_route(self._route_id(args), user.name)
        return f"🚚 {r['code']} đã chạy; khách được báo. Xem điểm giao: /trips"

    def _stop(self, ref: str) -> tuple[int, int]:
        num = ref.strip().lstrip("#")
        row = self.office.delivery.db.row(
            "SELECT s.route_id FROM dl_stops s JOIN dl_routes r ON r.id=s.route_id "
            "WHERE s.booking_id=? AND r.status='in_progress' ORDER BY s.id DESC LIMIT 1",
            (int(num) if num.isdigit() else -1,),
        )
        if row is None:
            raise InventoryError(f"Lịch giao #{num} không thuộc chuyến nào đang chạy. Xem: /trips")
        return int(row["route_id"]), int(num)

    def cmd_delivered(self, user: User, args: str, cid: int) -> str:
        rid, bid = self._stop(args)
        r = self.office.delivery.stop_result(rid, bid, "done", actor=user.name)
        left = sum(1 for s in r["stops"] if s["status"] == "pending")
        return f"✅ Đã giao #{bid}." + (f" Còn {left} điểm." if left else " Xong chuyến! 🎉")

    def cmd_failed(self, user: User, args: str, cid: int) -> str:
        ref, _, note = args.partition(" ")
        if not note.strip():
            return f"Cú pháp: /failed {ref or '<số>'} <lý do>"
        rid, bid = self._stop(ref)
        self.office.delivery.stop_result(rid, bid, "comeback", note.strip(), actor=user.name)
        return f"↩️ #{bid}: mang hàng về ({note.strip()}). Quản lý sẽ hẹn lại."

    # managers

    def cmd_report(self, user: User, args: str, cid: int) -> str:
        return self.employee.menu.report()

    def cmd_openorders(self, user: User, args: str, cid: int) -> str:
        return self.employee.menu.open_orders()

    async def cmd_approvals(self, user: User, args: str, cid: int) -> str:
        return await self.employee._admin("pending", by=user.name)

    async def cmd_approve(self, user: User, args: str, cid: int) -> str:
        return await self.employee._admin(f"approve {args}", by=user.name)

    async def cmd_reject(self, user: User, args: str, cid: int) -> str:
        return await self.employee._admin(f"reject {args}", by=user.name)
