"""The web shop with customer accounts, invoices by email, customers near a showroom,
and the shop's commands in the SimpleX apps."""

from __future__ import annotations

import re

import httpx2
import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_channels import PASSWORD, WEBHOOK, H, Platforms, settle

from ai_employees.inventory import InventoryError
from ai_employees.loyalty import vip_card
from ai_employees.mailer import Mailer
from ai_employees.storefront import CODE_ATTEMPTS, create_shop_app
from ai_employees.web import create_app

from fakes import ScriptedLLM, make_office

SITE = "http://shop.test"


class Maps(Platforms):
    """The chat bridge plus Google's geocoder."""

    def __init__(self) -> None:
        super().__init__()
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.route))

    def route(self, r: httpx2.Request) -> httpx2.Response:
        if r.url.host == "maps.googleapis.com":
            if "Thủ Đức" in r.url.params["address"]:
                return httpx2.Response(
                    200,
                    json={
                        "status": "OK",
                        "results": [{"geometry": {"location": {"lat": 10.85, "lng": 106.77}}}],
                    },
                )
            return httpx2.Response(200, json={"status": "ZERO_RESULTS", "results": []})
        return self.handle(r)


@pytest.fixture
async def site(tmp_path, monkeypatch):
    monkeypatch.setenv("T_HOOK_SECRET", "hook-secret-1")
    monkeypatch.setenv("SHOP_SMTP_PASSWORD", "smtp-pass")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "maps-key")
    mails: list = []
    monkeypatch.setattr(Mailer, "_send_now", lambda self, smtp, msg: mails.append((smtp, msg)))
    platforms = Maps()
    office = make_office(
        tmp_path, ScriptedLLM(), http=platforms.client, channels=[WEBHOOK], storefront={"public_url": SITE}
    )
    office.mailer.save(
        {
            "smtp_host": "smtp.test",
            "smtp_port": 587,
            "smtp_user": "shop@test.vn",
            "password_env": "SHOP_SMTP_PASSWORD",
            "sender": "Nội thất ABC <shop@test.vn>",
        }
    )
    inv = office.inventory
    inv.save_settings({"shop_name": "Nội thất ABC"})
    kho = inv.save_warehouse(None, {"code": "Q1", "name": "Showroom Quận 1"})["id"]
    ncc = inv.save_supplier(None, {"name": "Foshan", "lead_time_days": 45})["id"]
    sofa = inv.save_product(
        None,
        {
            "sku": "SOFA-01",
            "name": "Sofa da 3 chỗ",
            "category": "Phòng khách",
            "cbm": "1",
            "vip_price": 9_000_000,
            "description": "Da bò thật",
            "image_url": "https://img.test/sofa.jpg",
        },
    )["id"]
    hidden = inv.save_product(None, {"sku": "NOI-BO", "name": "Hàng trưng bày nội bộ", "on_web": False})["id"]
    with pytest.raises(InventoryError, match="https"):
        inv.save_product(sofa, {"image_url": "http://img.test/sofa.jpg"})
    inv.add_opening_stock(sofa, kho, 2, 6_000_000, margin_pct=40)
    inv.add_opening_stock(hidden, kho, 1, 100_000, margin_pct=40)
    po = inv.save_po(
        None,
        {
            "supplier_id": ncc,
            "warehouse_id": kho,
            "exchange_rate": "25000",
            "eta": "2026-11-20",
            "items": [{"product_id": sofa, "qty": 5, "unit_cost_foreign": "240", "margin_pct": 40}],
        },
        actor="t",
    )
    inv.set_po_status(po["id"], "ordered")
    shop = TestClient(TestServer(create_shop_app(office)))
    await shop.start_server()
    yield shop, office, mails, platforms, {"kho": kho, "sofa": sofa}
    await shop.close()


def csrf(client: TestClient) -> str:
    return next(c.value for c in client.session.cookie_jar if c.key == "sf_csrf")


async def post(client: TestClient, path: str, status: int = 303, **data: str):
    r = await client.post(path, data={"csrf": csrf(client), **data}, allow_redirects=False)
    body = await r.text()
    assert r.status == status, body
    return r, body


def last_code(mails: list) -> str:
    return re.search(r"\b(\d{6})\b", mails[-1][1].get_content()).group(1)


async def test_guest_buys_now_and_preorders_the_rest(site):
    shop, office, mails, _p, _ids = site
    r = await shop.get("/")
    page = await r.text()
    assert r.status == 200 and "Sofa da 3 chỗ" in page and "10.000.000 đ" in page and "Còn hàng" in page
    assert "Hàng trưng bày nội bộ" not in page  # not on the website
    assert 'src="https://img.test/sofa.jpg"' in page
    assert (
        "script-src" not in r.headers["Content-Security-Policy"]
        and "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    )
    assert (await shop.get("/p/NOI-BO")).status == 404
    found = await (await shop.get("/", params={"c": "Phòng khách", "q": "sofa"})).text()
    assert "Sofa da 3 chỗ" in found and 'name="c" value="Phòng khách"' in found
    assert "Sofa da 3 chỗ" not in await (await shop.get("/", params={"c": "Phòng ngủ"})).text()
    assert "Da bò thật" in await (await shop.get("/p/SOFA-01")).text()

    # a form without the page's token is refused
    r = await shop.post("/cart/add", data={"item": "SOFA-01", "qty": "1"}, allow_redirects=False)
    assert r.status == 403
    await post(shop, "/cart/add", item="SOFA-01", qty="3")
    cart = await (await shop.get("/cart")).text()
    assert "Có sẵn 2, phần còn lại đặt trước" in cart and "30.000.000 đ" in cart

    # checkout needs the customer's details
    _r, body = await post(shop, "/checkout", status=400, name="Hoa", phone="", address="x")
    assert "số điện thoại" in body
    _r, body = await post(
        shop,
        "/checkout",
        status=200,
        name="Hoa Nguyễn",
        phone="0901 234 567",
        email="hoa@example.com",
        address="12 Lê Lợi, Quận 1",
        note="Giao buổi chiều",
    )
    assert "Cảm ơn quý khách" in body
    now, pre = sorted(office.inventory.orders(), key=lambda o: o["kind"] != "now")
    assert (now["kind"], pre["kind"]) == ("now", "preorder")
    assert now["channel"] == "web" and now["source"] == "storefront" and now["email"] == "hoa@example.com"
    assert now["total"] == 20_000_000 and pre["total"] == 10_000_000
    contact = office.hub.crm.contact(now["contact_id"])
    assert contact["phone"] == "0901 234 567" and contact["address"] == "12 Lê Lợi, Quận 1"
    assert pre["contact_id"] == contact["id"]
    assert "Giỏ hàng trống" in await (await shop.get("/cart")).text()

    # the confirmation email, with private tracking links
    await settle(office.hub)
    confirm = mails[-1][1]
    assert confirm["To"] == "hoa@example.com" and now["code"] in confirm["Subject"]
    link = re.search(rf"{SITE}(/order/{now['code']}\?t=\w+)", confirm.get_content()).group(1)
    assert link in body  # also on the thank-you page
    tracking = await (await shop.get(link)).text()
    assert now["code"] in tracking and "Đã xác nhận" in tracking
    assert (await shop.get(f"/order/{now['code']}")).status == 404
    assert (await shop.get(f"/order/{now['code']}?t=0000")).status == 404
    assert (await shop.get(link.replace(now["code"], pre["code"]))).status == 404

    # the guest's email stays on the order: nobody logs in with an email a guest typed
    assert contact["email"] == ""

    # a guest with a VIP customer's phone number still sees the normal prices, never
    # changes that customer's details, and their order never joins that customer's
    # account (they could read its orders and invoices, or have their own shown there)
    office.hub.crm.update(contact["id"], vip=True, email="hoa@example.com")
    assert "9.000.000" not in await (await shop.get("/")).text()
    await post(shop, "/cart/add", item="SOFA-01", qty="1")
    await post(
        shop,
        "/checkout",
        status=200,
        name="Kẻ lạ",
        phone="0901234567",
        email="la@evil.test",
        address="Chỗ khác",
    )
    latest = office.inventory.orders()[0]
    assert latest["total"] == 10_000_000 and latest["contact_id"] != contact["id"]
    assert latest["email"] == "la@evil.test"
    assert office.hub.crm.contact(latest["contact_id"])["email"] == ""
    assert office.hub.crm.contact(contact["id"])["email"] == "hoa@example.com"
    assert office.hub.crm.contact(contact["id"])["address"] == "12 Lê Lợi, Quận 1"


async def test_customer_login_with_a_code_vip_prices_and_invoices(site):
    shop, office, mails, _p, _ids = site
    crm = office.hub.crm
    hoa = crm.create_contact("Hoa Nguyễn", "0901 234 567", "hoa@example.com")
    crm.update(hoa["id"], vip=True)
    other = crm.create_contact("Tuấn", "0912 000 111")
    theirs = office.inventory.create_order([{"sku": "SOFA-01", "qty": 1}], contact_id=other["id"])

    await shop.get("/login")
    # somebody unknown: the same answer, and no code goes anywhere
    r, _ = await post(shop, "/login", who="ai.do@nowhere.test")
    await settle(office.hub)
    assert r.headers["Location"] == "/verify" and mails == []
    _r, body = await post(shop, "/verify", status=400, code="123456")
    assert "Mã không đúng" in body

    await post(shop, "/login", who="HOA@example.com")
    await settle(office.hub)
    assert mails[-1][1]["To"] == "hoa@example.com" and "mã đăng nhập" in mails[-1][1]["Subject"]
    code = last_code(mails)
    await post(shop, "/verify", status=400, code="000000" if code != "000000" else "111111")
    r, _ = await post(shop, "/verify", code=code)
    assert r.headers["Location"] == "/account"
    await post(shop, "/verify", status=400, code=code)  # used once

    account = await (await shop.get("/account")).text()
    assert vip_card(hoa["id"]) in account and "Khách hàng VIP" in account
    catalog = await (await shop.get("/")).text()
    assert "9.000.000 đ" in catalog and "Giá VIP" in catalog and "⭐VIP" in catalog

    # a logged-in VIP pays the VIP price; the form is filled from the account
    await post(shop, "/cart/add", item="SOFA-01", qty="1")
    cart = await (await shop.get("/cart")).text()
    assert 'value="0901 234 567"' in cart and "9.000.000 đ" in cart
    await post(
        shop, "/checkout", status=200, name="Hoa Nguyễn", phone="0901 234 567", address="5 Hai Bà Trưng"
    )
    mine = office.inventory.orders(contact_id=hoa["id"])[0]
    assert mine["total"] == 9_000_000 and mine["email"] == "hoa@example.com"
    assert crm.contact(hoa["id"])["address"] == "5 Hai Bà Trưng"  # was empty: remembered

    account = await (await shop.get("/account")).text()
    assert mine["code"] in account
    invoice = await shop.get(f"/account/invoice/{mine['code']}")
    assert invoice.status == 200 and "Nội thất ABC" in await invoice.text()
    assert "script-src 'unsafe-inline'" in invoice.headers["Content-Security-Policy"]
    assert (await shop.get(f"/account/invoice/{theirs['code']}")).status == 404
    assert (await shop.get(f"/order/{mine['code']}")).status == 200  # their own order, no link needed
    assert (await shop.get(f"/order/{theirs['code']}")).status == 404

    await post(shop, "/account", name="Hoa N.", address="7 Pasteur")
    assert crm.contact(hoa["id"])["name"] == "Hoa N." and crm.contact(hoa["id"])["address"] == "7 Pasteur"
    await post(shop, "/logout")
    r = await shop.get("/account", allow_redirects=False)
    assert r.status == 303 and r.headers["Location"] == "/login"

    # a code tried too often stops working, even when it is finally right
    await post(shop, "/login", who="0901234567")
    await settle(office.hub)
    code = last_code(mails)
    for _ in range(CODE_ATTEMPTS):
        await post(shop, "/verify", status=400, code="999999" if code != "999999" else "888888")
    await post(shop, "/verify", status=400, code=code)
    # and at most three codes per 15 minutes
    await post(shop, "/login", who="0901234567")
    await settle(office.hub)
    sent = len(mails)
    await post(shop, "/login", who="0901234567")
    await settle(office.hub)
    assert len(mails) == sent


async def test_login_code_on_the_chat_channel_is_hidden_from_staff(site):
    shop, office, mails, platforms, _ids = site
    hub = office.hub
    conv = hub.inbox.upsert("website", "v7", "Minh", "sales")
    contact = hub.crm.observe(conv, "số của em 0912 345 678", "webhook")
    assert contact["phone"]
    await shop.get("/login")
    await post(shop, "/login", who="0912.345.678")
    await settle(office.hub)
    assert mails == []  # no email: the code went to the chat
    _kind, body = platforms.sent[-1]
    code = re.search(r"\b(\d{6})\b", str(body)).group(1)
    texts = [m["text"] for m in hub.inbox.messages(conv.id, limit=10)]
    assert not any(code in t for t in texts) and any("đã gửi mã đăng nhập" in t for t in texts)
    r, _ = await post(shop, "/verify", code=code)
    assert r.headers["Location"] == "/account"


async def test_invoices_by_email_from_the_counter_and_automatically(site):
    _shop, office, mails, _p, _ids = site
    admin = TestClient(TestServer(create_app(office, PASSWORD)))
    await admin.start_server()
    try:

        async def call(method, path, body=None, status=200):
            r = await admin.request(method, path, json=body, headers=H)
            data = await r.json() if r.content_type == "application/json" else await r.text()
            assert r.status == status, data
            return data

        await call("POST", "/api/login", {"password": PASSWORD})
        settings = await call("GET", "/api/inventory/mail")
        assert settings["ready"] and settings["password_set"] and "smtp-pass" not in str(settings)
        await call("PUT", "/api/inventory/mail", {"smtp_tls": "tls1.3"}, status=400)
        await call("POST", "/api/inventory/mail/test", {"to": "boss@test.vn"})
        smtp, msg = mails[-1]
        assert smtp["password"] == "smtp-pass" and smtp["host"] == "smtp.test" and msg["To"] == "boss@test.vn"
        assert msg["Auto-Submitted"] == "auto-generated"

        order = await call(
            "POST", "/api/pos/orders", {"items": [{"sku": "SOFA-01", "qty": 1}], "customer_name": "Lan"}
        )
        await call("POST", f"/api/pos/orders/{order['id']}/email-invoice", {}, status=400)  # no address yet
        r = await call("POST", f"/api/pos/orders/{order['id']}/email-invoice", {"to": "lan@example.com"})
        assert r["sent_to"] == "lan@example.com"
        msg = mails[-1][1]
        assert order["code"] in msg["Subject"] and msg.get_body(("html",)) is not None
        assert "Nội thất ABC" in msg.get_body(("html",)).get_content()
        assert office.inventory.order(order["id"])["email"] == "lan@example.com"
        page = await call("GET", f"/api/pos/orders/{order['id']}/receipt")
        assert order["code"] in page and "print()" in page

        # automatic: completed sales go out by email
        await call("PUT", "/api/inventory/mail", {"auto_invoice": True})
        sent = len(mails)
        await call("POST", f"/api/pos/orders/{order['id']}/payments", {"method": "cash"})
        await call("POST", f"/api/pos/orders/{order['id']}/complete")
        await settle(office.hub)
        assert len(mails) == sent + 1 and mails[-1][1]["To"] == "lan@example.com"

        # mail settings are for admins; a manager may look
        await call(
            "POST",
            "/api/users",
            {"username": "quan", "name": "Quân", "role": "manager", "password": "0123456789"},
        )
        await call("POST", "/api/logout", {})
        await call("POST", "/api/login", {"username": "quan", "password": "0123456789"})
        assert (await call("GET", "/api/inventory/mail"))["smtp_host"] == "smtp.test"
        await call("PUT", "/api/inventory/mail", {"smtp_host": "evil.test"}, status=403)
    finally:
        await admin.close()


async def test_customers_within_30_km_of_the_showroom(site):
    _shop, office, _mails, _p, ids = site
    crm, sales = office.hub.crm, office.sales
    with pytest.raises(InventoryError, match="chưa có toạ độ"):
        sales.segment(near_wh=ids["kho"], radius_km=30)
    office.delivery.set_warehouse_coords(ids["kho"], "10.7769", "106.7009")  # Quận 1
    near = crm.create_contact("Gần", "0900000001")
    crm.update(near["id"], lat=10.80, lng=106.66)  # Bình Thạnh, ~5 km
    far = crm.create_contact("Xa", "0900000002")
    crm.update(far["id"], lat=21.03, lng=105.85)  # Hà Nội
    edge = crm.create_contact("Biên Hoà", "0900000003")
    crm.update(edge["id"], lat=10.95, lng=106.82)  # ~23 km
    crm.create_contact("Chưa rõ", "0900000004")
    with pytest.raises(ValueError):
        crm.update(far["id"], lat=123)

    rows = sales.segment(min_orders=0, near_wh=ids["kho"], radius_km=30)
    assert [r["name"] for r in rows] == ["Gần", "Biên Hoà"]
    assert rows[0]["distance_km"] < 6 < rows[1]["distance_km"] < 30
    assert len(sales.segment(min_orders=0, near_wh=ids["kho"], radius_km=10)) == 1
    csv = sales.segment_csv(rows)
    assert "distance_km" in csv.splitlines()[0] and "Biên Hoà" in csv
    assert sales.located()["located"] == 3

    # a delivery remembers where the customer lives; the geocoder fills the rest
    who = crm.create_contact("Tâm", "0900000005")
    order = office.inventory.create_order([{"sku": "SOFA-01", "qty": 1}], contact_id=who["id"])
    office.delivery.book(
        {"order_id": order["id"], "delivery_date": "2026-10-01", "address": "3 Võ Văn Ngân, Thủ Đức"}, "t"
    )
    assert crm.contact(who["id"])["address"] == "3 Võ Văn Ngân, Thủ Đức"
    lost = crm.create_contact("Lạc", "0900000006")
    crm.update(lost["id"], address="không rõ ở đâu")
    assert sales.located()["to_geocode"] == 2
    assert await office.delivery.geocode_customers() == {"located": 1, "failed": 1}
    assert crm.contact(who["id"])["lat"] == 10.85
    assert "Tâm" in [r["name"] for r in sales.segment(min_orders=0, near_wh=ids["kho"], radius_km=30)]


async def test_the_shop_in_the_simplex_apps(site):
    shop, office, _mails, _p, _ids = site
    sales = office.employees["sales"]
    menu = sales.menu

    # the profile declares the menu the SimpleX apps show, with parameters to fill in
    profile = sales.bot._profile_to_wire()
    assert profile["peerType"] == "bot"
    commands = {c["keyword"]: c for c in profile["preferences"]["commands"]}
    assert commands["products"]["params"] and "params" not in commands["orders"] and "forget" in commands

    assert "Sofa da 3 chỗ" in await menu.handle(11, "products", "sofa", "Hoa")
    assert "10.000.000 đ" in await menu.handle(11, "products", "sofa", "Hoa")
    assert f"{SITE}/p/SOFA-01" in await menu.handle(11, "products", "sofa", "Hoa")
    conv = office.hub.inbox.find("simplex:sales", "11")
    contact = office.hub.crm.contact_of(conv.id)
    office.hub.crm.update(contact["id"], vip=True)
    assert "9.000.000 đ (giá VIP)" in await menu.handle(11, "products", "sofa", "Hoa")
    assert "chưa có đơn" in await menu.handle(11, "orders", "", "Hoa")

    order = office.inventory.create_order([{"sku": "SOFA-01", "qty": 1}], contact_id=contact["id"], vip=True)
    other = office.inventory.create_order([{"sku": "SOFA-01", "qty": 1}])
    listing = await menu.handle(11, "orders", "", "Hoa")
    assert order["code"] in listing and f"/'invoice {order['code']}'" in listing  # tappable in the app
    invoice = await menu.handle(11, "invoice", order["code"].lower(), "Hoa")
    assert order["code"] in invoice
    assert "Không tìm thấy" in await menu.handle(11, "invoice", other["code"], "Hoa")
    points = await menu.handle(11, "points", "", "Hoa")
    assert vip_card(contact["id"]) in points

    # a one-tap login to the website, confirmed with a button there
    reply = await menu.handle(11, "shop", "", "Hoa")
    link = re.search(rf"{SITE}(/l/[\w-]+/[\w-]+)", reply).group(1)
    await shop.get(link)
    oid, token = link.split("/")[2:]
    r, _ = await post(shop, "/l", id=oid, token=token)
    assert r.headers["Location"] == "/account"
    assert vip_card(contact["id"]) in await (await shop.get("/account")).text()
    await post(shop, "/l", status=400, id=oid, token=token)  # once

    assert "Đã báo nhân viên" in await menu.handle(11, "staff", "muốn đổi màu", "Hoa")
    conv = office.hub.inbox.conversation(conv.id)
    assert conv.mode == "human" and "cần nhân viên" in office.hub.inbox.labels(conv.id)

    # admins: the management commands, also in their own menu
    assert "chỉ dành cho quản trị viên" in await sales.command(5, "ai", "report")
    await sales.command(5, "admin", "secret-token")
    assert "Đơn mới: 2" in await sales.command(5, "ai", "report")
    assert order["code"] in await sales.command(5, "ai", "orders")
    assert "SOFA-01" in await sales.command(5, "ai", "stock sofa")
    assert "sắp về 5" in await sales.command(5, "ai", "stock sofa")
    assert "Cần nhập thêm" in await sales.command(5, "ai", "lowstock") or "sắp hết" in await sales.command(
        5, "ai", "lowstock"
    )
    from ai_employees.chat_menu import admin_menu

    assert admin_menu()[-1]["type"] == "menu" and any(
        c["keyword"] == "ai report" for c in admin_menu()[-1]["commands"]
    )
