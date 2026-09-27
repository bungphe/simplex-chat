"""Web API of the shop's business pages: point of sale, marketing, reports, delivery,
marketplaces and start-of-day notices (see pos, sales.py, delivery.py, marketplace.py).
Registered by web.create_app; access per role is decided by web._may."""

from __future__ import annotations

import hmac
import os
from datetime import datetime, timedelta
from typing import Any

from aiohttp import web

from .inventory import InventoryError


def _w():
    from . import web as w

    return w


def _office(request: web.Request) -> Any:
    return request.app[_w().OFFICE]


def _user(request: web.Request) -> Any:
    return _w()._user(request)


def _manager(request: web.Request) -> bool:
    return _user(request).role in ("admin", "manager")


def _id(request: web.Request, key: str = "id") -> int:
    try:
        return int(request.match_info[key])
    except ValueError:
        raise _w().ApiError(400, f"{key} phải là số") from None


def _period(request: web.Request, days: int = 30) -> tuple[str, str]:
    today = datetime.now().astimezone().date()
    start = request.query.get("start") or (today - timedelta(days=days - 1)).isoformat()
    end = request.query.get("end") or today.isoformat()
    return start[:10], end[:10]


def handler(fn: Any) -> Any:
    """fn(request, data) -> result; JSON out, InventoryError/ValueError -> 400."""

    async def run(request: web.Request) -> web.StreamResponse:
        w = _w()
        data = (
            await w._body(request)
            if request.method in ("POST", "PUT", "PATCH") and request.can_read_body
            else {}
        )
        try:
            result = fn(request, data)
            if hasattr(result, "__await__"):
                result = await result
        except (InventoryError, ValueError) as e:
            raise w.ApiError(400, str(e)) from None
        except KeyError as e:
            raise w.ApiError(404, f"không tìm thấy {e}") from None
        return result if isinstance(result, web.StreamResponse) else w._json(result)

    return run


def _csv(text: str, name: str) -> web.Response:
    return web.Response(
        text=text,
        content_type="text/csv",
        charset="utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# --------------------------------------------------------------------------- #
# point of sale


def _own_order(request: web.Request, oid: int) -> dict[str, Any]:
    """Sales staff and cashiers see only the sales they made today; managers see all."""
    order = _office(request).inventory.order(oid)
    user = _user(request)
    if not _manager(request):
        today = datetime.now().astimezone().date().isoformat()
        if order["salesperson"] != user.username or not order["created"].startswith(today):
            raise _w().ApiError(404, "no such order")
    return order


def pos_products(request: web.Request, _d: dict[str, Any]) -> Any:
    """Search for the counter: today's price (for this customer), stock per warehouse,
    goods arriving (for pre-orders), combos. No costs."""
    office = _office(request)
    inv = office.inventory
    q = request.query.get("q", "")
    vip = False
    if cid := request.query.get("contact"):
        contact = office.hub.crm.contact(int(cid))
        vip = bool(contact and contact.get("vip"))
    rows = inv.products(q, limit=50)
    out = []
    for p in rows[:50]:
        price = inv.current_price(int(p["id"]), vip)
        out.append(
            {
                "id": p["id"],
                "sku": p["sku"],
                "name": p["name"],
                "unit": p["unit"],
                "attributes": p["attributes"],
                "price": inv.major(price["price"]) if price else None,
                "list_price": inv.major(price["list_price"]) if price else None,
                "promo": (price or {}).get("promo", ""),
                "vip": bool(price and price["vip"]),
                "stage": p["stage"],
                "available": p["available"],
                "by_warehouse": p["by_warehouse"],
                "incoming": [
                    x for x in inv.product(int(p["id"]))["incoming_orders"] if x["qty"] - x["preordered"] > 0
                ],
                "level": p["level"],
            }
        )
    return {
        "products": out,
        "combos": [c for c in inv.combos(vip) if c["live"]],
        "vip": vip,
        "currency": inv.settings()["currency"],
        "warehouses": inv.warehouses(active_only=True),
    }


def pos_order_create(request: web.Request, d: dict[str, Any]) -> Any:
    office = _office(request)
    user = _user(request)
    contact = office.hub.crm.contact(int(d["contact_id"])) if d.get("contact_id") else None
    return office.inventory.create_order(
        d.get("items") or [],
        warehouse_id=d.get("warehouse_id") or None,
        kind=str(d.get("kind", "now")),
        contact_id=int(contact["id"]) if contact else None,
        customer_name=str(d.get("customer_name") or (contact or {}).get("name") or ""),
        phone=str(d.get("phone") or (contact or {}).get("phone") or ""),
        email=str(d.get("email") or (contact or {}).get("email") or ""),
        address=str(d.get("address", "")),
        discount=d.get("discount", 0),
        vip=bool(contact and contact.get("vip")),
        voucher=str(d.get("voucher") or ""),
        shipping_fee=d.get("shipping_fee", 0),
        note=str(d.get("note", "")),
        source="pos",
        channel=str(d.get("channel") or "pos"),
        salesperson=user.username,
        actor=user.name,
    )


def pos_orders(request: web.Request, _d: dict[str, Any]) -> Any:
    inv = _office(request).inventory
    if _manager(request):
        return {"orders": inv.orders(request.query.get("status") or None)}
    today = (
        datetime.now()
        .astimezone()
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .isoformat(timespec="seconds")
    )
    return {"orders": inv.orders(salesperson=_user(request).username, since=today)}


def pos_order(request: web.Request, _d: dict[str, Any]) -> Any:
    order = _own_order(request, _id(request))
    order["change_hints"] = (
        [
            _office(request).inventory.major(x)
            for x in _office(request).inventory.change_hints(
                _office(request).inventory.minor(order["due"]),
                _office(request).inventory.settings()["decimals"],
            )
        ]
        if order["due"]
        else []
    )
    return order


def pos_pay(request: web.Request, d: dict[str, Any]) -> Any:
    oid = _id(request)
    _own_order(request, oid)
    user = _user(request)
    return _office(request).inventory.add_payment(
        oid,
        str(d.get("method", "")),
        d.get("amount"),
        d.get("tendered"),
        str(d.get("ref", "")),
        str(d.get("idempotency_key", "")),
        user.name,
    )


def pos_step(request: web.Request, d: dict[str, Any]) -> Any:
    oid, step = _id(request), request.match_info["step"]
    inv = _office(request).inventory
    user = _user(request)
    _own_order(request, oid)
    if step == "complete":  # handed over at the counter
        return inv.complete_order(oid, user.name)
    if not _manager(request):
        raise _w().ApiError(403, "Chỉ quản lý cửa hàng huỷ hoặc hoàn tác đơn")
    if step == "cancel":
        return inv.cancel_order(oid, user.name)
    if step == "return":
        return inv.return_order(oid, user.name, str(d.get("reason", "")), admin=user.is_admin)
    raise _w().ApiError(404, "unknown step")


async def pos_receipt(request: web.Request) -> web.Response:
    """A printable receipt (the browser's print dialog)."""
    from .invoices import RECEIPT_CSP, receipt_html

    oid = _id(request)
    _own_order(request, oid)
    return web.Response(
        text=receipt_html(_office(request), oid),
        content_type="text/html",
        charset="utf-8",
        headers={"Content-Security-Policy": RECEIPT_CSP},  # the page's own style and print button
    )


async def pos_email_invoice(request: web.Request, d: dict[str, Any]) -> Any:
    """The invoice by email: to the address given, the sale's, or the customer's."""
    from .invoices import email_invoice

    oid = _id(request)
    _own_order(request, oid)
    sent_to = await email_invoice(_office(request), oid, str(d.get("to") or "").strip() or None)
    return {"sent_to": sent_to}


def mail_settings(request: web.Request, d: dict[str, Any]) -> Any:
    mailer = _office(request).mailer
    if request.method == "PUT":
        return mailer.save(d)
    return mailer.public()


async def mail_test(request: web.Request, d: dict[str, Any]) -> Any:
    office = _office(request)
    to = str(d.get("to") or "").strip()
    shop = office.inventory.settings()["shop_name"] or "Cửa hàng"
    await office.mailer.send(to, f"{shop} – thử gửi email", "Email của cửa hàng đã hoạt động.")
    return {"sent_to": to}


async def crm_geocode(request: web.Request, _d: dict[str, Any]) -> Any:
    """Locate customers with an address but no coordinates (Google Geocoding)."""
    office = _office(request)
    result = await office.delivery.geocode_customers()
    return {**result, **office.sales.located()}


async def pos_send_receipt(request: web.Request, _d: dict[str, Any]) -> Any:
    """The receipt as a message on the customer's chat channel."""
    office = _office(request)
    oid = _id(request)
    order = _own_order(request, oid)
    conv = order["conversation_id"]
    if not conv and order["contact_id"]:
        convs = office.hub.crm.conversations(int(order["contact_id"]))
        conv = convs[-1] if convs else None
    if not conv:
        raise InventoryError("Khách chưa có kênh chat nào để gửi hoá đơn")
    ok = await office.hub.notify_customer(int(conv), office.inventory.receipt_text(oid))
    if not ok:
        raise InventoryError("Không gửi được hoá đơn")
    return {"ok": True}


def pos_customers(request: web.Request, _d: dict[str, Any]) -> Any:
    crm = _office(request).hub.crm
    rows = crm.search(request.query.get("q", ""), limit=20)
    return {
        "customers": [
            {k: r.get(k) for k in ("id", "name", "phone", "email", "vip")}
            | {"points": crm.contact(int(r["id"]))["points"]}
            for r in rows
        ]
    }


def pos_customer_create(request: web.Request, d: dict[str, Any]) -> Any:
    c = _office(request).hub.crm.create_contact(
        str(d.get("name") or ""), str(d.get("phone") or ""), str(d.get("email") or "")
    )
    return {k: c.get(k) for k in ("id", "name", "phone", "email", "vip", "points")}


def pos_voucher_check(request: web.Request, _d: dict[str, Any]) -> Any:
    inv = _office(request).inventory
    code = request.query.get("code", "").strip().upper()
    v = inv.db.row("SELECT * FROM inv_vouchers WHERE code=?", (code,))
    if v is None or not v["active"]:
        raise InventoryError(f"Voucher {code} không hợp lệ")
    return inv._voucher_json(v)


def pos_sets(request: web.Request, _d: dict[str, Any]) -> Any:
    office = _office(request)
    inv = office.inventory
    if not request.query.get("template"):
        return {"templates": inv.set_templates()}
    vip = False
    if cid := request.query.get("contact"):
        contact = office.hub.crm.contact(int(cid))
        vip = bool(contact and contact.get("vip"))
    return inv.suggest_sets(request.query["template"], request.query.get("budget") or 0, vip)


# --------------------------------------------------------------------------- #
# marketing


def mk_overview(request: web.Request, _d: dict[str, Any]) -> Any:
    office = _office(request)
    inv = office.inventory
    return {
        "promotions": inv.promotions(),
        "vouchers": inv.vouchers(),
        "combos": inv.combos(),
        "ads": office.sales.ad_spend(),
        "sets": inv.set_templates(),
        "platforms": list(_sales().PLATFORMS),
        "channel_types": sorted({ch.type for ch in office.hub.channels.values()} | {"simplex"}),
        # showrooms for "customers within X km"
        "showrooms": [
            {"id": w["id"], "name": w["name"], "located": bool(w.get("lat"))}
            for w in inv.warehouses(active_only=True)
        ],
        "located": office.sales.located(),
    }


def _sales() -> Any:
    from . import sales

    return sales


def mk_save(request: web.Request, d: dict[str, Any]) -> Any:
    inv = _office(request).inventory
    kind = request.match_info["kind"]
    rid = _id(request) if "id" in request.match_info else None
    if kind == "promotions":
        return inv.save_promotion(rid, d)
    if kind == "vouchers":
        return inv.save_voucher(rid, d)
    if kind == "combos":
        return inv.save_combo(rid, d)
    if kind == "sets":
        return inv.save_set_templates(d.get("templates"))
    if kind == "ads":
        return _office(request).sales.add_ad_spend(d)
    raise _w().ApiError(404, "unknown")


def mk_delete(request: web.Request, _d: dict[str, Any]) -> Any:
    office = _office(request)
    kind, rid = request.match_info["kind"], _id(request)
    table = {"promotions": "inv_promotions", "vouchers": "inv_vouchers", "combos": "inv_combos"}.get(kind)
    if kind == "ads":
        office.sales.delete("mk_ad_spend", rid)
    elif table:
        office.inventory.db.execute(f"UPDATE {table} SET active=0 WHERE id=?", (rid,))  # kept for past orders
        if kind == "promotions":
            office.inventory._emit(
                "prices",
                product_ids=[int(r["id"]) for r in office.inventory.db.rows("SELECT id FROM inv_products")],
            )
    else:
        raise _w().ApiError(404, "unknown")
    return {"ok": True}


def mk_segment(request: web.Request, _d: dict[str, Any]) -> Any:
    sales = _office(request).sales
    q = request.query
    rows = sales.segment(
        q.get("kind", "top"),
        int(q.get("limit") or 500),
        q.get("channel", ""),
        int(q.get("min_orders") or 0),
        near_wh=int(q["near_wh"]) if q.get("near_wh") else None,
        radius_km=float(q["radius_km"]) if q.get("radius_km") else None,
    )
    if q.get("format") == "csv":
        return _csv(sales.segment_csv(rows), "khach-hang.csv")
    return {"customers": rows, "located": sales.located()}


def mk_weekly(request: web.Request, _d: dict[str, Any]) -> Any:
    return {"products": _office(request).sales.weekly_prices()}


# --------------------------------------------------------------------------- #
# reports: P&L, commissions, shifts, expenses


def rp_pnl(request: web.Request, _d: dict[str, Any]) -> Any:
    start, end = _period(request)
    return _office(request).sales.pnl(start, end)


def rp_settings(request: web.Request, d: dict[str, Any]) -> Any:
    sales = _office(request).sales
    return sales.save_settings(d) if request.method == "PUT" else sales.settings()


def rp_commissions(request: web.Request, d: dict[str, Any]) -> Any:
    sales = _office(request).sales
    if request.method == "GET":
        start, end = _period(request, days=7)
        return {
            "preview": sales.preview_commissions(start, end),
            "saved": sales.commissions(),
            "start": start,
            "end": end,
        }
    params = {k: d.get(k) for k in ("target_per_hour", "rate_pct", "contribution_pct")}
    if d.get("save"):
        return {"saved": sales.save_commissions(d.get("start"), d.get("end"), _user(request).name, **params)}
    return {"preview": sales.preview_commissions(d.get("start"), d.get("end"), **params)}


def rp_commission_status(request: web.Request, d: dict[str, Any]) -> Any:
    if not _user(request).is_admin and _user(request).role != "manager":
        raise _w().ApiError(403, "Chỉ quản lý chốt hoa hồng")
    return _office(request).sales.set_commission_status(_id(request), str(d.get("status", "")))


def rp_shifts(request: web.Request, d: dict[str, Any]) -> Any:
    sales = _office(request).sales
    if request.method == "POST":
        return sales.add_shift(
            str(d.get("username", "")),
            d.get("work_date"),
            d.get("hours"),
            d.get("warehouse_id"),
            str(d.get("note", "")),
            _user(request).name,
        )
    start, end = _period(request, days=14)
    users = [
        {"username": u["username"], "name": u["name"], "role": u["role"]}
        for u in request.app[_w().USERS].list()
    ]
    return {"shifts": sales.shifts(start, end), "users": users, "start": start, "end": end}


def rp_expenses(request: web.Request, d: dict[str, Any]) -> Any:
    sales = _office(request).sales
    if request.method == "POST":
        return sales.add_expense(d)
    start, end = _period(request)
    return {"expenses": sales.expenses(start, end), "categories": list(_sales().EXPENSE_CATEGORIES)}


def rp_delete(request: web.Request, _d: dict[str, Any]) -> Any:
    table = {"shifts": "sales_shifts", "expenses": "fin_expenses"}[request.match_info["kind"]]
    _office(request).sales.delete(table, _id(request))
    return {"ok": True}


# --------------------------------------------------------------------------- #
# delivery


def dl_overview(request: web.Request, _d: dict[str, Any]) -> Any:
    office = _office(request)
    dl = office.delivery
    month = request.query.get("month") or datetime.now().astimezone().date().isoformat()[:7]
    day = request.query.get("date") or datetime.now().astimezone().date().isoformat()
    return {
        "settings": dl.settings(),
        "carriers": dl.carriers(),
        "drivers": dl.drivers(),
        "calendar": dl.calendar(month),
        "bookings": dl.bookings(day, day),
        "routes": dl.routes(day, day),
        "month": month,
        "date": day,
        "warehouses": office.inventory.warehouses(),
        "slots": _dl().SLOTS,
        "open_orders": [
            o
            for o in office.inventory.orders("confirmed")
            if not dl.db.row(
                "SELECT 1 AS x FROM dl_bookings WHERE order_id=? AND status IN ('booked','assigned','shipping')",
                (o["id"],),
            )
        ],
    }


def _dl() -> Any:
    from . import delivery

    return delivery


def dl_save(request: web.Request, d: dict[str, Any]) -> Any:
    dl = _office(request).delivery
    kind = request.match_info["kind"]
    rid = _id(request) if "id" in request.match_info else None
    if kind == "carriers":
        return dl.save_carrier(rid, d)
    if kind == "drivers":
        return dl.save_driver(rid, d)
    if kind == "bookings":
        return dl.update_booking(rid, d) if rid else dl.book(d, _user(request).name)
    if kind == "settings":
        return dl.save_settings(d)
    if kind == "warehouses" and rid:
        return dl.set_warehouse_coords(rid, d.get("lat"), d.get("lng"))
    raise _w().ApiError(404, "unknown")


async def dl_booking_step(request: web.Request, _d: dict[str, Any]) -> Any:
    dl, bid, step = _office(request).delivery, _id(request), request.match_info["step"]
    if step == "cancel":
        return dl.cancel_booking(bid)
    if step == "geocode":
        return await dl.geocode(bid)
    raise _w().ApiError(404, "unknown step")


def dl_route_create(request: web.Request, d: dict[str, Any]) -> Any:
    return _office(request).delivery.create_route(d, _user(request).name)


async def dl_route_step(request: web.Request, d: dict[str, Any]) -> Any:
    dl, rid, step = _office(request).delivery, _id(request), request.match_info["step"]
    name = _user(request).name
    if step == "optimize":
        return await dl.optimize(rid)
    if step == "reorder":
        return dl.reorder(rid, [int(x) for x in d.get("booking_ids") or []])
    if step == "remove":
        return dl.remove_stop(rid, int(d.get("booking_id") or 0))
    if step == "start":
        return await dl.start_route(rid, name)
    if step == "result":
        return dl.stop_result(
            rid, int(d.get("booking_id") or 0), str(d.get("result", "")), str(d.get("note", "")), name
        )
    if step == "cancel":
        return dl.cancel_route(rid)
    raise _w().ApiError(404, "unknown step")


def dl_route_csv(request: web.Request, _d: dict[str, Any]) -> Any:
    rid = _id(request)
    return _csv(_office(request).delivery.daily_list_csv(rid), f"giao-hang-CX{rid:05d}.csv")


def dl_statement(request: web.Request, _d: dict[str, Any]) -> Any:
    start, end = _period(request)
    return {"carriers": _office(request).delivery.carrier_statement(start, end), "start": start, "end": end}


def dl_bookings(request: web.Request, _d: dict[str, Any]) -> Any:
    start, end = _period(request, days=1)
    return {"bookings": _office(request).delivery.bookings(start, end, request.query.get("status") or None)}


# --------------------------------------------------------------------------- #
# marketplaces (admins; managers can read)


def mp_list(request: web.Request, _d: dict[str, Any]) -> Any:
    mp = _office(request).marketplaces
    return {"marketplaces": mp.public(), "log": mp.log(100)}


def mp_save(request: web.Request, d: dict[str, Any]) -> Any:
    return {"marketplaces": _office(request).marketplaces.save(d)}


def mp_remove(request: web.Request, _d: dict[str, Any]) -> Any:
    return {"marketplaces": _office(request).marketplaces.remove(request.match_info["mid"])}


async def mp_sync(request: web.Request, d: dict[str, Any]) -> Any:
    mp = _office(request).marketplaces
    mid = request.match_info["mid"]
    mp.config(mid)
    queued = mp.enqueue_all(mid)
    pushed = await mp.process()
    return {"queued": queued, "pushed": pushed, "log": mp.log(20)}


def mp_sku(request: web.Request, d: dict[str, Any]) -> Any:
    return _office(request).marketplaces.set_external_sku(
        _id(request), str(d.get("marketplace", "")), str(d.get("sku", ""))
    )


# --------------------------------------------------------------------------- #
# notices and loyalty


def notices(request: web.Request, d: dict[str, Any]) -> Any:
    sales = _office(request).sales
    user = _user(request)
    if request.method == "POST":
        if not user.is_admin and user.role != "manager":
            raise _w().ApiError(403, "Chỉ quản lý đăng thông báo")
        return sales.add_notice(str(d.get("title", "")), str(d.get("body", "")), user.name)
    return {
        "pending": sales.pending_notices(user.username),
        "all": sales.notices() if user.is_admin or user.role == "manager" else [],
    }


def notice_step(request: web.Request, d: dict[str, Any]) -> Any:
    sales, nid, step = _office(request).sales, _id(request), request.match_info["step"]
    user = _user(request)
    if step in ("done", "skip"):
        sales.ack_notice(nid, user.username, step)
    elif step == "close" and (user.is_admin or user.role == "manager"):
        sales.close_notice(nid)
    else:
        raise _w().ApiError(403, "not allowed")
    return {"ok": True}


def crm_points(request: web.Request, d: dict[str, Any]) -> Any:
    office = _office(request)
    cid = _id(request)
    if request.method == "POST":
        office.loyalty.adjust(cid, int(d.get("delta") or 0), str(d.get("reason", "")))
    contact = office.hub.crm.contact(cid) or {}
    return {
        "points": contact.get("points", 0),
        "vip": bool(contact.get("vip")),
        "vip_since": contact.get("vip_since"),
        "total_spent": office.inventory.major(contact.get("total_spent") or 0),
        "orders": contact.get("orders_count", 0),
        "card": _loyalty().vip_card(cid),
        "history": office.loyalty.history(cid),
        "orders_list": office.inventory.orders(contact_id=cid, limit=50),
    }


def _loyalty() -> Any:
    from . import loyalty

    return loyalty


# --------------------------------------------------------------------------- #
# public catalog for your own website (opt in: CATALOG_KEY), under /hooks/ like webhooks


async def catalog(request: web.Request) -> web.Response:
    key = os.environ.get("CATALOG_KEY", "")
    given = request.query.get("key", "") or request.headers.get("X-Catalog-Key", "")
    if not key or not hmac.compare_digest(given.encode(), key.encode()):
        raise web.HTTPNotFound()
    inv = _office(request).inventory
    rows = inv.lookup(request.query.get("q", ""), limit=min(int(request.query.get("limit") or 100), 500))
    return _w()._json({"products": rows, "currency": inv.settings()["currency"]})


def add_routes(r: web.UrlDispatcher) -> None:
    g, p = r.add_get, r.add_post
    g("/api/pos/products", handler(pos_products))
    g("/api/pos/orders", handler(pos_orders))
    p("/api/pos/orders", handler(pos_order_create))
    g(r"/api/pos/orders/{id:\d+}", handler(pos_order))
    g(r"/api/pos/orders/{id:\d+}/receipt", pos_receipt)
    p(r"/api/pos/orders/{id:\d+}/payments", handler(pos_pay))
    p(r"/api/pos/orders/{id:\d+}/send-receipt", handler(pos_send_receipt))
    p(r"/api/pos/orders/{id:\d+}/email-invoice", handler(pos_email_invoice))
    p(r"/api/pos/orders/{id:\d+}/{step}", handler(pos_step))
    g("/api/pos/customers", handler(pos_customers))
    p("/api/pos/customers", handler(pos_customer_create))
    g("/api/pos/voucher", handler(pos_voucher_check))
    g("/api/pos/sets", handler(pos_sets))

    g("/api/marketing", handler(mk_overview))
    g("/api/marketing/segment", handler(mk_segment))
    g("/api/marketing/weekly-prices", handler(mk_weekly))
    p("/api/marketing/{kind}", handler(mk_save))
    r.add_put(r"/api/marketing/{kind}/{id:\d+}", handler(mk_save))
    r.add_delete(r"/api/marketing/{kind}/{id:\d+}", handler(mk_delete))

    g("/api/reports/pnl", handler(rp_pnl))
    g("/api/reports/settings", handler(rp_settings))
    r.add_put("/api/reports/settings", handler(rp_settings))
    g("/api/reports/commissions", handler(rp_commissions))
    p("/api/reports/commissions", handler(rp_commissions))
    p(r"/api/reports/commissions/{id:\d+}/status", handler(rp_commission_status))
    g("/api/reports/shifts", handler(rp_shifts))
    p("/api/reports/shifts", handler(rp_shifts))
    g("/api/reports/expenses", handler(rp_expenses))
    p("/api/reports/expenses", handler(rp_expenses))
    r.add_delete(r"/api/reports/{kind}/{id:\d+}", handler(rp_delete))

    g("/api/delivery", handler(dl_overview))
    g("/api/delivery/bookings", handler(dl_bookings))
    g("/api/delivery/statement", handler(dl_statement))
    p("/api/delivery/routes", handler(dl_route_create))
    g(r"/api/delivery/routes/{id:\d+}/list.csv", handler(dl_route_csv))
    p(r"/api/delivery/routes/{id:\d+}/{step}", handler(dl_route_step))
    p(r"/api/delivery/bookings/{id:\d+}/{step}", handler(dl_booking_step))
    p("/api/delivery/{kind}", handler(dl_save))
    r.add_put(r"/api/delivery/{kind}/{id:\d+}", handler(dl_save))

    g("/api/inventory/marketplaces", handler(mp_list))
    p("/api/inventory/marketplaces", handler(mp_save))
    r.add_delete("/api/inventory/marketplaces/{mid}", handler(mp_remove))
    p("/api/inventory/marketplaces/{mid}/sync", handler(mp_sync))
    p(r"/api/inventory/products/{id:\d+}/external-sku", handler(mp_sku))

    g("/api/inventory/mail", handler(mail_settings))
    r.add_put("/api/inventory/mail", handler(mail_settings))
    p("/api/inventory/mail/test", handler(mail_test))
    p("/api/crm/geocode", handler(crm_geocode))

    g("/api/notices", handler(notices))
    p("/api/notices", handler(notices))
    p(r"/api/notices/{id:\d+}/{step}", handler(notice_step))
    g(r"/api/crm/contacts/{id:\d+}/points", handler(crm_points))
    p(r"/api/crm/contacts/{id:\d+}/points", handler(crm_points))
    g("/hooks/catalog", catalog)
