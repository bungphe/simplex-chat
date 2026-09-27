"""The shop's own website: catalog, cart, checkout and order tracking, with customer
accounts.

It is a separate listener from the admin UI (config `storefront:`), meant for the
Internet behind an HTTPS reverse proxy. Pages are rendered on the server, with no
script at all. Prices, stock and incoming goods come from the inventory, so the website
always shows what the counter and the AI employees sell.

Customers do not have passwords: a customer of the shop (a CRM contact) types their
email address or phone number and gets a one-time code, by email (the shop's mailer)
or on the chat channel they already use (SimpleX, Zalo, Telegram, ...). Logged in, a
VIP customer sees and pays the VIP prices, and every customer sees their points, VIP
card, orders and invoices. Guests can buy too: their order joins the contact with the
same phone number (or a new one) and can be followed with a private link.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

from aiohttp import web

from .crm import phone_key
from .inventory import InventoryError, order_code
from .invoices import RECEIPT_CSP, receipt_html
from .loyalty import vip_card
from .mailer import valid_email

if TYPE_CHECKING:
    from .config import StorefrontConfig
    from .employee import Office

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sf_sessions (
  token_hash TEXT PRIMARY KEY, contact_id {int} NOT NULL, created TEXT NOT NULL, expires TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sf_codes (
  id {id}, contact_id {int} NOT NULL, code_hash TEXT NOT NULL, via TEXT NOT NULL,
  attempts {int} NOT NULL DEFAULT 0, used {int} NOT NULL DEFAULT 0, created TEXT NOT NULL, expires TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sf_codes_contact ON sf_codes (contact_id, created)
"""
KEY = "storefront"
SESSION_DAYS = 30
CODE_MINUTES = 10
CODE_ATTEMPTS = 5
CODES_PER_CONTACT = 3  # per 15 minutes
REQUESTS_PER_IP = 20  # login requests per 15 minutes
CART_LINES = 30
SESSION, CSRF, CART, LOGIN = "sf_session", "sf_csrf", "sf_cart", "sf_login"
CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self' https:; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'"
)
HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}
STATUS = {
    "confirmed": "Đã xác nhận",
    "completed": "Hoàn tất",
    "cancelled": "Đã huỷ",
    "returned": "Đã trả hàng",
}
SENT = (
    "Nếu thông tin khớp với một khách hàng của cửa hàng, mã đăng nhập đã được gửi qua email "
    "hoặc qua kênh chat quý khách thường dùng (SimpleX, Zalo, Telegram…). Mã có hiệu lực {m} phút."
)

OFFICE: web.AppKey[Office] = web.AppKey("office")
SHOP: web.AppKey[Storefront] = web.AppKey("shop")
NEW_CSRF: web.RequestKey[str] = web.RequestKey("new_csrf")
CUSTOMER: web.RequestKey[dict[str, Any] | None] = web.RequestKey("customer")


def _utc(delta: timedelta = timedelta()) -> str:
    return (datetime.now(UTC) + delta).isoformat(timespec="seconds")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Storefront:
    """What the website does, apart from HTTP (tested directly)."""

    def __init__(self, office: Office, public_url: str = ""):
        self.office = office
        self.inv = office.inventory
        self.crm = office.hub.crm
        self.db = self.crm.db
        self.db.script(SCHEMA)
        self.public_url = public_url
        self._ip_hits: dict[str, list[float]] = {}

    @property
    def secret(self) -> bytes:
        doc = self.office.docs.get(KEY) or {}
        if not doc.get("secret"):
            self.office.docs.update(KEY, lambda d: d.setdefault("secret", secrets.token_hex(32)), {})
            doc = self.office.docs.get(KEY) or {}
        return bytes.fromhex(doc["secret"])

    def sign(self, purpose: str, value: str) -> str:
        return hmac.new(self.secret, f"{purpose}:{value}".encode(), hashlib.sha256).hexdigest()[:32]

    def check(self, purpose: str, value: str, signature: str) -> bool:
        return hmac.compare_digest(self.sign(purpose, value), signature or "")

    # ------------------------------------------------------------------ #
    # accounts: one-time codes and sessions

    def find_contact(self, who: str) -> dict[str, Any] | None:
        who = who.strip()
        if "@" in who:
            row = self.db.row("SELECT id FROM crm_contacts WHERE email=? ORDER BY id LIMIT 1", (who.lower(),))
        else:
            key = phone_key(who)
            row = (
                self.db.row("SELECT id FROM crm_contacts WHERE phone_key=? ORDER BY id LIMIT 1", (key,))
                if key
                else None
            )
        return self.crm.contact(int(row["id"])) if row else None

    def allow_ip(self, ip: str) -> bool:
        now = time.monotonic()
        hits = [t for t in self._ip_hits.get(ip, []) if now - t < 900]
        if len(self._ip_hits) > 10000:  # a flood of addresses: forget the oldest
            self._ip_hits.clear()
        if len(hits) >= REQUESTS_PER_IP:
            self._ip_hits[ip] = hits
            return False
        self._ip_hits[ip] = [*hits, now]
        return True

    async def request_code(self, who: str) -> int:
        """Send a login code to a known customer; returns the code's id (0 when nobody was
        found or nothing could be sent: the page says the same either way)."""
        contact = self.find_contact(who)
        if contact is None:
            return 0
        cid = int(contact["id"])
        recent = self.db.row(
            "SELECT COUNT(*) AS n FROM sf_codes WHERE contact_id=? AND created>=?",
            (cid, _utc(timedelta(minutes=-15))),
        )
        if recent and int(recent["n"]) >= CODES_PER_CONTACT:
            log.info("storefront: too many login codes for contact %s", cid)
            return 0
        code = f"{secrets.randbelow(1_000_000):06d}"
        shop = self.inv.settings()["shop_name"] or "Cửa hàng"
        text = f"Mã đăng nhập website {shop}: {code} (hiệu lực {CODE_MINUTES} phút). Đừng đưa mã này cho ai."
        via = ""
        by_email = "@" in who or not self.crm.conversations(cid)
        if contact["email"] and self.office.mailer.ready and by_email:
            try:
                await self.office.mailer.send(contact["email"], f"{shop} – mã đăng nhập", text)
                via = "email"
            except InventoryError as e:
                log.info("storefront: login code email failed: %s", e)
        if not via:
            for conv_id in reversed(self.crm.conversations(cid)):
                if await self.office.hub.send_private(conv_id, text, "🔐 [đã gửi mã đăng nhập website]"):
                    via = "chat"
                    break
        if not via and contact["email"] and self.office.mailer.ready:
            try:
                await self.office.mailer.send(contact["email"], f"{shop} – mã đăng nhập", text)
                via = "email"
            except InventoryError:
                pass
        if not via:
            log.info("storefront: no way to send a login code to contact %s", cid)
            return 0
        oid = self.db.execute(
            "INSERT INTO sf_codes (contact_id, code_hash, via, created, expires) VALUES (?, ?, ?, ?, ?) RETURNING id",
            (cid, "", via, _utc(), _utc(timedelta(minutes=CODE_MINUTES))),
        )
        assert oid is not None
        self.db.execute(
            "UPDATE sf_codes SET code_hash=? WHERE id=?", (self.sign("code", f"{oid}:{code}"), oid)
        )
        return int(oid)

    def magic_link(self, contact_id: int) -> str:
        """A one-time login link for a customer who asked for it in the chat (they are
        already known there): valid for CODE_MINUTES, once."""
        token = secrets.token_urlsafe(24)
        oid = self.db.execute(
            "INSERT INTO sf_codes (contact_id, code_hash, via, created, expires) VALUES (?, ?, ?, ?, ?) RETURNING id",
            (contact_id, "", "link", _utc(), _utc(timedelta(minutes=CODE_MINUTES))),
        )
        assert oid is not None
        self.db.execute(
            "UPDATE sf_codes SET code_hash=? WHERE id=?", (self.sign("code", f"{oid}:{token}"), oid)
        )
        return f"{self.public_url}/l/{oid}/{token}"

    def verify_code(self, code_id: int, code: str) -> int | None:
        """The contact id when the code is right (once), else None."""
        row = self.db.row("SELECT * FROM sf_codes WHERE id=?", (code_id,))
        if row is None or row["used"] or int(row["attempts"]) >= CODE_ATTEMPTS or row["expires"] < _utc():
            return None
        if not hmac.compare_digest(self.sign("code", f"{code_id}:{code.strip()}"), row["code_hash"]):
            self.db.execute("UPDATE sf_codes SET attempts=attempts+1 WHERE id=?", (code_id,))
            return None
        if (
            self.db.execute("UPDATE sf_codes SET used=1 WHERE id=? AND used=0 RETURNING id", (code_id,))
            is None
        ):
            return None
        return int(row["contact_id"])

    def start_session(self, contact_id: int) -> str:
        token = secrets.token_urlsafe(32)
        self.db.execute("DELETE FROM sf_sessions WHERE expires<?", (_utc(),))
        self.db.execute(
            "INSERT INTO sf_sessions (token_hash, contact_id, created, expires) VALUES (?, ?, ?, ?)",
            (_sha(token), contact_id, _utc(), _utc(timedelta(days=SESSION_DAYS))),
        )
        return token

    def session_contact(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        row = self.db.row(
            "SELECT contact_id FROM sf_sessions WHERE token_hash=? AND expires>=?", (_sha(token), _utc())
        )
        return self.crm.contact(int(row["contact_id"])) if row else None

    def end_session(self, token: str) -> None:
        self.db.execute("DELETE FROM sf_sessions WHERE token_hash=?", (_sha(token),))

    # ------------------------------------------------------------------ #
    # catalog and cart

    def catalog(self, query: str = "", category: str = "", vip: bool = False) -> list[dict[str, Any]]:
        out = []
        for p in self.inv.products(query, limit=300):
            if not p["on_web"] or (category and p["category"] != category):
                continue
            out.append(self.offer(p, vip))
        return out

    def offer(self, p: dict[str, Any], vip: bool) -> dict[str, Any]:
        price = self.inv.current_price(int(p["id"]), vip)
        incoming = max(0, int(p["incoming"]) - int(p["preordered"]))
        if price is None and incoming:  # not in stock yet: the price of the goods on their way
            slot = next(
                (x for x in self.inv._incoming(int(p["id"])) if int(x["qty"]) > int(x["qty_preordered"])),
                None,
            )
            if slot and int(slot["price1"]):
                price = {"price": int(slot["price1"]), "list_price": int(slot["price1"]), "vip": False}
        return {
            **p,
            "price": self.inv.major(price["price"]) if price else None,
            "list_price": self.inv.major(price["list_price"]) if price else None,
            "promo": (price or {}).get("promo", ""),
            "vip_price": bool(price and price["vip"]),
            "available": max(0, int(p["available"])),
            "can_preorder": incoming,
        }

    def product(self, sku: str, vip: bool = False) -> dict[str, Any] | None:
        row = self.inv.by_sku(sku)
        if row is None or not row["active"] or not row["on_web"]:
            return None
        return self.offer(self.inv.product(int(row["id"])), vip)

    def categories(self) -> list[str]:
        return [
            r["category"]
            for r in self.db.rows(
                "SELECT DISTINCT category FROM inv_products WHERE active=1 AND on_web=1 AND category<>'' ORDER BY category"
            )
        ]

    def cart_lines(self, cart: list[dict[str, Any]], vip: bool) -> tuple[list[dict[str, Any]], Any]:
        lines, total = [], 0
        for it in cart:
            try:
                if it.get("combo"):
                    c = self.inv.combo(str(it["combo"]), vip)
                    if not c["live"]:
                        continue
                    line = {
                        "key": f"combo:{c['code']}",
                        "name": f"Combo {c['name']}",
                        "sku": c["code"],
                        "price": c["price"],
                        "qty": int(it["qty"]),
                        "list_price": c["separate_price"],
                    }
                else:
                    p = self.product(str(it["sku"]), vip)
                    if p is None or p["price"] is None:
                        continue
                    line = {
                        "key": p["sku"],
                        "name": p["name"],
                        "sku": p["sku"],
                        "price": p["price"],
                        "qty": int(it["qty"]),
                        "list_price": p["list_price"],
                        "available": p["available"],
                        "can_preorder": p["can_preorder"],
                        "next_eta": p["next_eta"],
                    }
            except InventoryError:
                continue
            line["total"] = line["price"] * line["qty"]
            total += line["total"]
            lines.append(line)
        return lines, total

    # ------------------------------------------------------------------ #
    # checkout

    def _customer(self, logged_in: dict[str, Any] | None, form: dict[str, str]) -> int:
        if logged_in is not None:
            cid = int(logged_in["id"])
            self.crm.locate(cid, form.get("address", ""))
            if form.get("email") and not logged_in["email"] and valid_email(form["email"]):
                self.crm.update(cid, email=form["email"])
            return cid
        # A guest: the contact with this phone number, else a new one. A guest never
        # changes an existing customer's details (they are not logged in as them).
        key = phone_key(form.get("phone", ""))
        row = (
            self.db.row("SELECT id FROM crm_contacts WHERE phone_key=? ORDER BY id LIMIT 1", (key,))
            if key
            else None
        )
        if row:
            return int(row["id"])
        contact = self.crm.create_contact(form.get("name", ""), form.get("phone", ""), form.get("email", ""))
        self.crm.locate(int(contact["id"]), form.get("address", ""))
        return int(contact["id"])

    def checkout(
        self, cart: list[dict[str, Any]], form: dict[str, str], logged_in: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """Place the web order: what is in stock now, and a preorder for the rest (on goods
        already on their way). VIP prices only for a logged-in VIP customer."""
        name, phone = form.get("name", "").strip(), form.get("phone", "").strip()
        email = form.get("email", "").strip().lower()
        if not cart:
            raise InventoryError("Giỏ hàng trống")
        if not name or not phone_key(phone):
            raise InventoryError("Vui lòng nhập họ tên và số điện thoại")
        if email and not valid_email(email):
            raise InventoryError("Email không hợp lệ")
        if not form.get("address", "").strip():
            raise InventoryError("Vui lòng nhập địa chỉ giao hàng")
        vip = bool(logged_in and logged_in["vip"])
        wh = int(self.inv.default_warehouse()["id"])
        now_items: list[dict[str, Any]] = []
        later: list[dict[str, Any]] = []
        for it in cart:
            if it.get("combo"):
                now_items.append({"combo": it["combo"], "qty": it["qty"]})
                continue
            row = self.inv.by_sku(str(it["sku"]))
            if row is None or not row["active"] or not row["on_web"]:
                raise InventoryError(f"Sản phẩm {it['sku']} không còn bán")
            free = max(0, self.inv._available(wh, int(row["id"])))
            take = min(int(it["qty"]), free)
            if take:
                now_items.append({"sku": row["sku"], "qty": take})
            if int(it["qty"]) > take:
                later.append({"sku": row["sku"], "qty": int(it["qty"]) - take})
        contact_id = self._customer(logged_in, form)
        common = {
            "warehouse_id": wh,
            "contact_id": contact_id,
            "customer_name": name,
            "phone": phone,
            "address": form.get("address", "").strip(),
            "note": form.get("note", "").strip(),
            "vip": vip,
            "source": "storefront",
            "actor": "web",
            "channel": "web",
            "email": email or (logged_in or {}).get("email", ""),
        }
        orders = []
        try:
            if now_items:
                orders.append(self.inv.create_order(now_items, kind="now", **common))
            if later:
                orders.append(self.inv.create_order(later, kind="preorder", **common))
        except InventoryError:
            for o in orders:  # all or nothing
                self.inv.cancel_order(int(o["id"]), actor="web")
            raise
        return orders

    def order_link(self, order: dict[str, Any]) -> str:
        code = order_code(int(order["id"]))
        return f"{self.public_url}/order/{code}?t={self.sign('order', code)}"

    async def after_checkout(self, orders: list[dict[str, Any]]) -> None:
        """Confirm to the customer by email, and tell the shop."""
        shop = self.inv.settings()["shop_name"] or "Cửa hàng"
        first = orders[0]
        lines = []
        for o in orders:
            kind = "Đặt trước (giao khi hàng về)" if o["kind"] == "preorder" else "Có sẵn"
            lines.append(f"{o['code']} – {kind}: {o['total']:,}")
            lines += [f"  • {i['name']} × {i['qty']}" for i in o["items"]]
        summary = "\n".join(lines)
        if first.get("email") and self.office.mailer.ready:
            try:
                await self.office.mailer.send(
                    first["email"],
                    f"{shop} – đã nhận đơn {', '.join(o['code'] for o in orders)}",
                    f"Cảm ơn {first['customer_name']}! {shop} đã nhận đơn hàng của quý khách:\n\n{summary}\n\n"
                    + "".join(f"Theo dõi {o['code']}: {self.order_link(o)}\n" for o in orders)
                    + "\nNhân viên sẽ liên hệ để hẹn giao hàng.",
                )
            except InventoryError as e:
                log.info("storefront: confirmation email not sent: %s", e)
        employee = next(iter(self.office.employees.values()), None)
        if employee is not None:
            await employee.notify_admins(
                f"🛒 Đơn web mới từ {first['customer_name']} ({first['phone']}):\n{summary}\nĐịa chỉ: {first['address']}"
            )


# ---------------------------------------------------------------------- #
# HTTP


def _money(office: Office) -> Any:
    cur = office.inventory.settings()["currency"]
    unit = "đ" if cur == "VND" else cur

    def fmt(v: Any) -> str:
        if v is None:
            return "Liên hệ"
        return f"{v:,}".replace(",", ".") + f" {unit}"

    return fmt


def _cart(request: web.Request) -> list[dict[str, Any]]:
    shop = request.app[SHOP]
    raw = request.cookies.get(CART, "")
    data, _, sig = raw.rpartition(".")
    if not data or not shop.check("cart", data, sig):
        return []
    try:
        cart = json.loads(base64.urlsafe_b64decode(data.encode()))
    except ValueError:
        return []
    return [x for x in cart if isinstance(x, dict) and int(x.get("qty") or 0) > 0][:CART_LINES]


def _set_cart(request: web.Request, resp: web.StreamResponse, cart: list[dict[str, Any]]) -> None:
    data = base64.urlsafe_b64encode(json.dumps(cart[:CART_LINES], separators=(",", ":")).encode()).decode()
    _cookie(request, resp, CART, f"{data}.{request.app[SHOP].sign('cart', data)}", days=14)


def _secure(request: web.Request) -> bool:
    return request.secure or request.app[SHOP].public_url.startswith("https://")


def _cookie(request: web.Request, resp: web.StreamResponse, name: str, value: str, days: float) -> None:
    resp.set_cookie(
        name,
        value,
        max_age=int(days * 86400),
        httponly=True,
        samesite="Lax",
        secure=_secure(request),
        path="/",
    )


def _customer(request: web.Request) -> dict[str, Any] | None:
    return request.app[SHOP].session_contact(request.cookies.get(SESSION, ""))


def _csrf(request: web.Request) -> str:
    return request.cookies.get(CSRF) or request.get(NEW_CSRF) or ""


def _client_ip(request: web.Request) -> str:
    """The visitor's address; behind our own reverse proxy (a loopback or private
    address, e.g. Docker's), the last X-Forwarded-For entry, the one the proxy added."""
    ip = request.remote or ""
    forwarded = request.headers.get("X-Forwarded-For", "")
    try:
        behind_proxy = ipaddress.ip_address(ip).is_private
    except ValueError:
        behind_proxy = False
    if forwarded and behind_proxy:
        ip = forwarded.split(",")[-1].strip()
    return ip


async def _form(request: web.Request) -> dict[str, str]:
    data = await request.post()
    token = request.cookies.get(CSRF, "")
    if not token or not hmac.compare_digest(token, str(data.get("csrf", ""))):
        raise web.HTTPForbidden(text="Phiên làm việc đã hết hạn, vui lòng tải lại trang.")
    return {k: str(v)[:500] for k, v in data.items()}


@web.middleware
async def _headers(request: web.Request, handler: Any) -> web.StreamResponse:
    if not request.cookies.get(CSRF):
        request[NEW_CSRF] = secrets.token_urlsafe(24)
    resp = await handler(request)
    for k, v in HEADERS.items():
        resp.headers.setdefault(k, v)
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("Cache-Control", "no-store")
    if request.get(NEW_CSRF):
        _cookie(request, resp, CSRF, request[NEW_CSRF], days=1)
    return resp


def _page(request: web.Request, title: str, body: str, status: int = 200) -> web.Response:
    office = request.app[OFFICE]
    s = office.inventory.settings()
    e = html.escape
    who = request.get(CUSTOMER)
    count = sum(int(x["qty"]) for x in _cart(request))
    account = (
        f'<a href="/account">{e(who["name"] or "Tài khoản")}{" ⭐VIP" if who["vip"] else ""}</a>'
        if who
        else '<a href="/login">Đăng nhập</a>'
    )
    doc = f"""<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(title)} – {e(s["shop_name"] or "Cửa hàng")}</title>
<link rel="stylesheet" href="/static/shop.css"></head><body>
<header><a class="brand" href="/">{e(s["shop_name"] or "Cửa hàng")}</a>
<nav><a href="/">Sản phẩm</a><a href="/combos">Combo</a><a href="/cart">Giỏ hàng ({count})</a>{account}</nav></header>
<main>{body}</main>
<footer>{e(s["shop_address"])}{" · " + e(s["shop_phone"]) if s["shop_phone"] else ""}</footer></body></html>"""
    return web.Response(text=doc, content_type="text/html", status=status)


def _hidden(request: web.Request) -> str:
    return f'<input type="hidden" name="csrf" value="{html.escape(_csrf(request))}">'


def _price_html(request: web.Request, p: dict[str, Any]) -> str:
    m = _money(request.app[OFFICE])
    e = html.escape
    out = f'<span class="price">{e(m(p["price"]))}</span>'
    if p.get("list_price") and p["price"] is not None and p["list_price"] > p["price"]:
        out += f" <s>{e(m(p['list_price']))}</s>"
    if p.get("promo"):
        out += f' <span class="badge">{e(p["promo"])}</span>'
    if p.get("vip_price"):
        out += ' <span class="badge vip">Giá VIP</span>'
    return out


def _stock_html(p: dict[str, Any]) -> str:
    if p["available"] > 0:
        return '<span class="ok">Còn hàng</span>'
    if p["can_preorder"] > 0:
        eta = f" – về khoảng {html.escape(p['next_eta'])}" if p.get("next_eta") else ""
        return f'<span class="warn">Đặt trước{eta}</span>'
    return '<span class="bad">Tạm hết hàng</span>'


def _buy_form(request: web.Request, key: str, can_buy: bool) -> str:
    if not can_buy:
        return ""
    return (
        f'<form method="post" action="/cart/add" class="buy">{_hidden(request)}'
        f'<input type="hidden" name="item" value="{html.escape(key)}">'
        '<input type="number" name="qty" value="1" min="1" max="99" aria-label="Số lượng">'
        "<button>Thêm vào giỏ</button></form>"
    )


async def catalog(request: web.Request) -> web.Response:
    shop, e = request.app[SHOP], html.escape
    who = request[CUSTOMER]
    q, cat = request.query.get("q", "")[:80], request.query.get("c", "")[:80]
    items = shop.catalog(q, cat, bool(who and who["vip"]))
    cats = "".join(
        f'<a class="chip{" on" if c == cat else ""}" href="/?{urlencode({"c": c})}">{e(c)}</a>'
        for c in shop.categories()
    )
    cards = "".join(
        '<article class="card">'
        + (f'<img src="{e(p["image_url"])}" alt="" loading="lazy">' if p["image_url"] else "")
        + f'<h3><a href="/p/{quote(p["sku"])}">{e(p["name"])}</a></h3><div class="muted">{e(p["sku"])}</div>'
        f"<div>{_price_html(request, p)}</div><div>{_stock_html(p)}</div>"
        f"{_buy_form(request, p['sku'], p['price'] is not None and (p['available'] or p['can_preorder']))}</article>"
        for p in items
    )
    body = (
        f'<form class="search" method="get" action="/"><input name="q" value="{e(q)}" placeholder="Tìm sản phẩm…">'
        + (f'<input type="hidden" name="c" value="{e(cat)}">' if cat else "")
        + "<button>Tìm</button></form>"
        f'<div class="chips"><a class="chip{"" if cat else " on"}" href="/">Tất cả</a>{cats}</div>'
        + (f'<div class="grid">{cards}</div>' if cards else "<p>Không tìm thấy sản phẩm.</p>")
    )
    if who and who["vip"]:
        body = '<p class="note">⭐ Quý khách đang xem giá VIP.</p>' + body
    elif not who:
        body = '<p class="note">Khách VIP: <a href="/login">đăng nhập</a> để xem giá ưu đãi riêng.</p>' + body
    return _page(request, "Sản phẩm", body)


async def product_page(request: web.Request) -> web.Response:
    shop, e = request.app[SHOP], html.escape
    who = request[CUSTOMER]
    p = shop.product(request.match_info["sku"], bool(who and who["vip"]))
    if p is None:
        return _page(request, "Không tìm thấy", "<p>Sản phẩm không tồn tại hoặc đã ngừng bán.</p>", 404)
    attrs = "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in p["attributes"].items())
    body = (
        '<article class="detail">'
        + (f'<img src="{e(p["image_url"])}" alt="">' if p["image_url"] else "")
        + f'<div><h1>{e(p["name"])}</h1><div class="muted">{e(p["sku"])} · {e(p["unit"])}</div>'
        f"<p>{_price_html(request, p)}</p><p>{_stock_html(p)}</p>"
        f"{_buy_form(request, p['sku'], p['price'] is not None and (p['available'] or p['can_preorder']))}"
        f'<p class="desc">{e(p["description"])}</p>'
        + (f"<table>{attrs}</table>" if attrs else "")
        + "</div></article>"
    )
    return _page(request, p["name"], body)


async def combos_page(request: web.Request) -> web.Response:
    office, e = request.app[OFFICE], html.escape
    who = request[CUSTOMER]
    m = _money(office)
    cards = ""
    for c in office.inventory.combos(bool(who and who["vip"])):
        if not c["live"]:
            continue
        parts = "".join(f"<li>{e(i['name'])} × {i['qty']}</li>" for i in c["items"])
        cards += (
            f'<article class="card"><h3>{e(c["name"])}</h3><ul>{parts}</ul>'
            f'<div><span class="price">{e(m(c["price"]))}</span> <s>{e(m(c["separate_price"]))}</s> '
            f'<span class="badge">Tiết kiệm {e(m(c["saving"]))}</span></div>'
            f"{_buy_form(request, 'combo:' + c['code'], c['available'] > 0)}"
            + ("" if c["available"] > 0 else '<span class="bad">Tạm hết hàng</span>')
            + "</article>"
        )
    return _page(
        request,
        "Combo",
        f'<h1>Combo tiết kiệm</h1><div class="grid">{cards or "<p>Chưa có combo.</p>"}</div>',
    )


def _item(key: str, qty: int) -> dict[str, Any]:
    if key.startswith("combo:"):
        return {"combo": key[6:].upper()[:40], "qty": qty}
    return {"sku": key.upper()[:60], "qty": qty}


def _key(it: dict[str, Any]) -> str:
    return f"combo:{it['combo']}" if it.get("combo") else str(it["sku"])


async def cart_add(request: web.Request) -> web.Response:
    form = await _form(request)
    try:
        qty = max(1, min(99, int(form.get("qty") or 1)))
    except ValueError:
        qty = 1
    key = form.get("item", "").strip()
    cart = _cart(request)
    for it in cart:
        if _key(it) == _key(_item(key, 0)):
            it["qty"] = min(99, int(it["qty"]) + qty)
            break
    else:
        cart.append(_item(key, qty))
    resp = web.HTTPSeeOther("/cart")
    _set_cart(request, resp, cart)
    raise resp


async def cart_update(request: web.Request) -> web.Response:
    form = await _form(request)
    cart = []
    for it in _cart(request):
        try:
            qty = int(form.get(f"qty:{_key(it)}", it["qty"]))
        except ValueError:
            qty = int(it["qty"])
        if qty > 0 and form.get("remove") != _key(it):
            cart.append({**it, "qty": min(99, qty)})
    resp = web.HTTPSeeOther("/cart")
    _set_cart(request, resp, cart)
    raise resp


def _cart_body(request: web.Request, error: str = "", values: dict[str, str] | None = None) -> str:
    shop, e = request.app[SHOP], html.escape
    m = _money(request.app[OFFICE])
    who = request[CUSTOMER]
    lines, total = shop.cart_lines(_cart(request), bool(who and who["vip"]))
    if not lines:
        return '<h1>Giỏ hàng</h1><p>Giỏ hàng trống. <a href="/">Xem sản phẩm</a></p>'
    rows = ""
    for ln in lines:
        note = ""
        if "available" in ln and ln["qty"] > ln["available"]:
            eta = f" (về khoảng {e(ln['next_eta'])})" if ln.get("next_eta") else ""
            note = (
                f'<div class="warn">Có sẵn {ln["available"]}, phần còn lại đặt trước{eta}</div>'
                if ln["qty"] - ln["available"] <= ln["can_preorder"]
                else f'<div class="bad">Chỉ còn {ln["available"] + ln["can_preorder"]}</div>'
            )
        rows += (
            f"<tr><td>{e(ln['name'])}<div class='muted'>{e(ln['sku'])}</div>{note}</td>"
            f"<td>{e(m(ln['price']))}</td>"
            f'<td><input type="number" name="qty:{e(ln["key"])}" value="{ln["qty"]}" min="0" max="99" aria-label="Số lượng"></td>'
            f"<td>{e(m(ln['total']))}</td>"
            f'<td><button name="remove" value="{e(ln["key"])}" class="link">Xoá</button></td></tr>'
        )
    v = values or {}
    if who and not values:
        v = {"name": who["name"], "phone": who["phone"], "email": who["email"], "address": who["address"]}
    field = lambda n, label, t="text", req=True: (
        f'<label>{label}<input type="{t}" name="{n}" value="{e(v.get(n, ""))}"{" required" if req else ""}></label>'
    )
    return (
        f'<h1>Giỏ hàng</h1><form method="post" action="/cart/update">{_hidden(request)}'
        f"<table class='cart'><tr><th>Sản phẩm</th><th>Đơn giá</th><th>SL</th><th>Thành tiền</th><th></th></tr>{rows}</table>"
        f'<p class="total">Tạm tính: {e(m(total))} <button>Cập nhật</button></p></form>'
        + (f'<p class="error">{e(error)}</p>' if error else "")
        + f'<h2>Đặt hàng</h2><form method="post" action="/checkout" class="checkout">{_hidden(request)}'
        + field("name", "Họ tên")
        + field("phone", "Số điện thoại", "tel")
        + field("email", "Email (nhận xác nhận và hoá đơn)", "email", False)
        + field("address", "Địa chỉ giao hàng")
        + f'<label>Ghi chú<textarea name="note">{e(v.get("note", ""))}</textarea></label>'
        + "<button>Đặt hàng</button></form>"
        + (
            ""
            if who
            else '<p class="muted">Đã là khách của cửa hàng? <a href="/login">Đăng nhập</a> để tích điểm và xem giá VIP.</p>'
        )
    )


async def cart_page(request: web.Request) -> web.Response:
    return _page(request, "Giỏ hàng", _cart_body(request))


async def checkout(request: web.Request) -> web.Response:
    form = await _form(request)
    shop = request.app[SHOP]
    try:
        orders = shop.checkout(_cart(request), form, request[CUSTOMER])
    except (InventoryError, ValueError) as e:
        return _page(request, "Giỏ hàng", _cart_body(request, str(e), form), 400)
    request.app[OFFICE].hub.spawn(shop.after_checkout(orders))
    links = "".join(
        f'<li><a href="{html.escape(shop.order_link(o).removeprefix(shop.public_url))}">{o["code"]}</a> – '
        f"{'đặt trước, giao khi hàng về' if o['kind'] == 'preorder' else 'hàng có sẵn'}</li>"
        for o in orders
    )
    resp = _page(
        request,
        "Đã đặt hàng",
        f"<h1>Cảm ơn quý khách!</h1><p>Cửa hàng đã nhận đơn và sẽ liên hệ để hẹn giao hàng.</p><ul>{links}</ul>"
        "<p class='muted'>Lưu lại đường dẫn trên để theo dõi đơn hàng.</p>",
    )
    _set_cart(request, resp, [])
    return resp


def _order_body(request: web.Request, oid: int) -> str:
    office, e = request.app[OFFICE], html.escape
    m = _money(office)
    o = office.inventory.order(oid)
    items = "".join(
        f"<tr><td>{e(i['name'])}</td><td>{i['qty']}</td><td>{e(m(i['line_total']))}</td>"
        f"<td>{'chờ hàng về' + (' (' + e(i['eta']) + ')' if i.get('eta') else '') if i['status'] == 'awaiting' else ''}</td></tr>"
        for i in o["items"]
    )
    booking = office.hub.crm.db.row(
        "SELECT delivery_date, time_window, status FROM dl_bookings WHERE order_id=? AND status<>'cancelled' ORDER BY id DESC LIMIT 1",
        (oid,),
    )
    delivery = (
        f"<p>Giao hàng: {e(booking['delivery_date'])} {e(booking['time_window'])} ({e(booking['status'])})</p>"
        if booking
        else ""
    )
    return (
        f"<h1>Đơn {e(o['code'])}</h1><p>{e(STATUS.get(o['status'], o['status']))} · {e(o['created'][:10])}"
        f"{' · đặt trước' if o['kind'] == 'preorder' else ''}</p>"
        f"<table class='cart'>{items}</table><p class='total'>Tổng: {e(m(o['total']))} · Đã trả: {e(m(o['paid']))}</p>"
        f"{delivery}"
    )


async def order_page(request: web.Request) -> web.Response:
    shop = request.app[SHOP]
    code = request.match_info["code"].upper()
    who = request[CUSTOMER]
    try:
        oid = int(code.removeprefix("DH"))
        order = request.app[OFFICE].inventory.order(oid)
    except (ValueError, InventoryError):
        return _page(request, "Không tìm thấy", "<p>Không tìm thấy đơn hàng.</p>", 404)
    mine = who is not None and order["contact_id"] == who["id"]
    if not (mine or shop.check("order", order["code"], request.query.get("t", ""))):
        return _page(request, "Không tìm thấy", "<p>Không tìm thấy đơn hàng.</p>", 404)
    extra = f'<p><a href="/account/invoice/{order["code"]}">Xem hoá đơn</a></p>' if mine else ""
    return _page(request, order["code"], _order_body(request, oid) + extra)


async def login_page(request: web.Request) -> web.Response:
    body = (
        "<h1>Đăng nhập</h1><p>Nhập email hoặc số điện thoại quý khách đã dùng với cửa hàng. "
        "Cửa hàng gửi mã đăng nhập qua email hoặc qua kênh chat quý khách thường dùng.</p>"
        f'<form method="post" action="/login" class="checkout">{_hidden(request)}'
        '<label>Email hoặc số điện thoại<input name="who" required autocomplete="username"></label>'
        "<button>Gửi mã</button></form>"
    )
    return _page(request, "Đăng nhập", body)


async def login(request: web.Request) -> web.Response:
    form = await _form(request)
    shop = request.app[SHOP]
    if not shop.allow_ip(_client_ip(request)):
        return _page(request, "Đăng nhập", "<p>Quá nhiều yêu cầu, vui lòng thử lại sau ít phút.</p>", 429)
    code_id = await shop.request_code(form.get("who", "")[:200])
    resp = web.HTTPSeeOther("/verify")
    ref = str(code_id or secrets.randbelow(10**9) + 10**9)  # the same page either way
    _cookie(request, resp, LOGIN, f"{ref}.{shop.sign('login', ref)}", days=CODE_MINUTES / 1440)
    raise resp


def _verify_body(request: web.Request, error: str = "") -> str:
    return (
        f"<h1>Nhập mã</h1><p>{html.escape(SENT.format(m=CODE_MINUTES))}</p>"
        + (f'<p class="error">{html.escape(error)}</p>' if error else "")
        + f'<form method="post" action="/verify" class="checkout">{_hidden(request)}'
        '<label>Mã 6 số<input name="code" inputmode="numeric" pattern="[0-9]{6}" required autocomplete="one-time-code"></label>'
        '<button>Đăng nhập</button></form><p><a href="/login">Gửi lại mã</a></p>'
    )


async def verify_page(request: web.Request) -> web.Response:
    return _page(request, "Nhập mã", _verify_body(request))


async def verify(request: web.Request) -> web.Response:
    form = await _form(request)
    shop = request.app[SHOP]
    ref, _, sig = request.cookies.get(LOGIN, "").partition(".")
    contact_id = (
        shop.verify_code(int(ref), form.get("code", ""))
        if ref.isdigit() and shop.check("login", ref, sig)
        else None
    )
    if contact_id is None:
        return _page(request, "Nhập mã", _verify_body(request, "Mã không đúng hoặc đã hết hạn."), 400)
    resp = web.HTTPSeeOther("/account")
    _cookie(request, resp, SESSION, shop.start_session(contact_id), days=SESSION_DAYS)
    resp.del_cookie(LOGIN, path="/")
    raise resp


async def link_page(request: web.Request) -> web.Response:
    """A login link from the chat: confirmed with a button, so that link previews and
    prefetching never use it up."""
    e = html.escape
    body = (
        "<h1>Đăng nhập</h1><p>Bấm nút dưới đây để đăng nhập vào tài khoản của quý khách.</p>"
        f'<form method="post" action="/l" class="checkout">{_hidden(request)}'
        f'<input type="hidden" name="id" value="{e(request.match_info["id"])}">'
        f'<input type="hidden" name="token" value="{e(request.match_info["token"])}">'
        "<button>Đăng nhập</button></form>"
    )
    return _page(request, "Đăng nhập", body)


async def link_login(request: web.Request) -> web.Response:
    form = await _form(request)
    shop = request.app[SHOP]
    ref = form.get("id", "")
    contact_id = shop.verify_code(int(ref), form.get("token", "")) if ref.isdigit() else None
    if contact_id is None:
        return _page(
            request,
            "Đăng nhập",
            '<p class="error">Đường dẫn đã hết hạn hoặc đã được dùng.</p><p><a href="/login">Đăng nhập bằng mã</a></p>',
            400,
        )
    resp = web.HTTPSeeOther("/account")
    _cookie(request, resp, SESSION, shop.start_session(contact_id), days=SESSION_DAYS)
    raise resp


async def logout(request: web.Request) -> web.Response:
    await _form(request)
    request.app[SHOP].end_session(request.cookies.get(SESSION, ""))
    resp = web.HTTPSeeOther("/")
    resp.del_cookie(SESSION, path="/")
    raise resp


async def account(request: web.Request) -> web.Response:
    office, e = request.app[OFFICE], html.escape
    who = request[CUSTOMER]
    if who is None:
        raise web.HTTPSeeOther("/login")
    m = _money(office)
    s = office.inventory.settings()
    cid = int(who["id"])
    if who["vip"]:
        status = f'<p class="vipcard">⭐ Khách hàng VIP · Thẻ số <b>{vip_card(cid)}</b></p>'
    elif int(s["vip_points"]):
        status = f"<p>Còn {max(0, int(s['vip_points']) - int(who['points']))} điểm nữa để lên VIP.</p>"
    else:
        status = ""
    orders = "".join(
        f'<tr><td><a href="/order/{o["code"]}">{o["code"]}</a></td><td>{e(o["created"][:10])}</td>'
        f"<td>{e(STATUS.get(o['status'], o['status']))}</td><td>{e(m(o['total']))}</td>"
        f'<td><a href="/account/invoice/{o["code"]}">Hoá đơn</a></td></tr>'
        for o in office.inventory.orders(contact_id=cid, limit=50)
    )
    points = "".join(
        f"<tr><td>{e(p['ts'][:10])}</td><td>{e(p['reason'])}</td><td>{p['delta']:+}</td></tr>"
        for p in office.loyalty.history(cid)[:20]
    )
    body = (
        f"<h1>Xin chào {e(who['name'] or 'quý khách')}</h1>{status}"
        f"<p>Điểm tích luỹ: <b>{who['points']}</b> · Đã mua: {e(m(office.inventory.major(who['total_spent'])))}</p>"
        f"<h2>Đơn hàng</h2>"
        + (f"<table class='cart'>{orders}</table>" if orders else "<p>Chưa có đơn hàng.</p>")
        + (f"<h2>Lịch sử điểm</h2><table class='cart'>{points}</table>" if points else "")
        + f'<h2>Thông tin</h2><form method="post" action="/account" class="checkout">{_hidden(request)}'
        f'<label>Họ tên<input name="name" value="{e(who["name"])}"></label>'
        f'<label>Địa chỉ giao hàng<input name="address" value="{e(who["address"])}"></label>'
        f"<p class='muted'>Điện thoại: {e(who['phone'] or '-')} · Email: {e(who['email'] or '-')}</p>"
        "<button>Lưu</button></form>"
        f'<form method="post" action="/logout">{_hidden(request)}<button class="link">Đăng xuất</button></form>'
    )
    return _page(request, "Tài khoản", body)


async def account_save(request: web.Request) -> web.Response:
    form = await _form(request)
    who = request[CUSTOMER]
    if who is None:
        raise web.HTTPSeeOther("/login")
    crm = request.app[OFFICE].hub.crm
    fields: dict[str, Any] = {}
    if form.get("name", "").strip():
        fields["name"] = form["name"]
    if form.get("address", "").strip() != who["address"]:
        fields.update(address=form.get("address", ""), lat=None, lng=None)  # to be located again
    if fields:
        crm.update(int(who["id"]), **fields)
    raise web.HTTPSeeOther("/account")


async def invoice(request: web.Request) -> web.Response:
    who = request[CUSTOMER]
    office = request.app[OFFICE]
    try:
        oid = int(request.match_info["code"].upper().removeprefix("DH"))
        order = office.inventory.order(oid)
    except (ValueError, InventoryError):
        order = None
    if who is None or order is None or order["contact_id"] != who["id"]:
        return _page(request, "Không tìm thấy", "<p>Không tìm thấy hoá đơn.</p>", 404)
    resp = web.Response(text=receipt_html(office, oid), content_type="text/html")
    resp.headers["Content-Security-Policy"] = RECEIPT_CSP
    return resp


async def stylesheet(request: web.Request) -> web.Response:
    from pathlib import Path

    css = (Path(__file__).parent / "static" / "shop.css").read_text()
    resp = web.Response(text=css, content_type="text/css")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


async def no_icon(_request: web.Request) -> web.Response:
    return web.Response(status=204)


def _with_customer(fn: Any) -> Any:
    async def handler(request: web.Request) -> web.StreamResponse:
        request[CUSTOMER] = _customer(request)
        return await fn(request)

    return handler


def create_shop_app(office: Office, public_url: str = "") -> web.Application:
    app = web.Application(middlewares=[_headers], client_max_size=64 * 1024)
    app[OFFICE] = office
    app[SHOP] = getattr(office, "storefront", None) or Storefront(office, public_url)
    r = app.router
    for method, path, fn in (
        ("GET", "/", catalog),
        ("GET", "/p/{sku}", product_page),
        ("GET", "/combos", combos_page),
        ("GET", "/cart", cart_page),
        ("POST", "/cart/add", cart_add),
        ("POST", "/cart/update", cart_update),
        ("POST", "/checkout", checkout),
        ("GET", "/order/{code}", order_page),
        ("GET", "/login", login_page),
        ("POST", "/login", login),
        ("GET", "/verify", verify_page),
        ("POST", "/verify", verify),
        ("GET", "/l/{id}/{token}", link_page),
        ("POST", "/l", link_login),
        ("POST", "/logout", logout),
        ("GET", "/account", account),
        ("POST", "/account", account_save),
        ("GET", "/account/invoice/{code}", invoice),
    ):
        r.add_route(method, path, _with_customer(fn))
    r.add_get("/static/shop.css", stylesheet)
    r.add_get("/favicon.ico", no_icon)
    return app


async def start_storefront(office: Office, cfg: StorefrontConfig) -> web.AppRunner:
    runner = web.AppRunner(create_shop_app(office, cfg.public_url), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, cfg.host, cfg.port).start()
    log.info("storefront: http://%s:%s (public: %s)", cfg.host, cfg.port, cfg.public_url or "-")
    return runner
