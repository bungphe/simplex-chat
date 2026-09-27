"""Point of sale, promotions/vouchers/combos, room sets, loyalty and automatic VIP,
commissions and P&L, delivery, marketplace sync, roles and start-of-day notices."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta

import httpx2
import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_channels import PASSWORD, WEBHOOK, H, Platforms, settle

from ai_employees.delivery import best_order, haversine_km
from ai_employees.inventory import InventoryError
from ai_employees.web import create_app

from fakes import ScriptedLLM, make_office, text

HOOK = {"X-Hook-Secret": "hook-secret-1"}


class Shop(Platforms):
    """The chat bridge plus a website webhook and Amazon's APIs."""

    def __init__(self) -> None:
        super().__init__()
        self.site: list[tuple[dict, str]] = []
        self.amazon: list[tuple[str, str, dict]] = []
        self.amazon_orders: list[dict] = []
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.route))

    def route(self, r: httpx2.Request) -> httpx2.Response:
        if r.url.host == "site.local":
            self.site.append((json.loads(r.content), r.headers.get("X-Signature", "")))
            return httpx2.Response(200, json={"ok": True})
        if r.url.host == "lwa.local":
            form = dict(x.split("=", 1) for x in r.content.decode().split("&"))
            assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "Atzr-refresh"
            return httpx2.Response(200, json={"access_token": "Atza-token", "expires_in": 3600})
        if r.url.host == "sp.local":
            assert r.headers["x-amz-access-token"] == "Atza-token"
            body = json.loads(r.content) if r.content else {}
            self.amazon.append((r.method, r.url.path, body))
            if r.url.path.startswith("/listings/"):
                return httpx2.Response(
                    200, json={"sku": r.url.path.rsplit("/", 1)[1], "status": "ACCEPTED", "issues": []}
                )
            if r.url.path == "/orders/v0/orders":
                return httpx2.Response(200, json={"payload": {"Orders": self.amazon_orders}})
            if r.url.path.endswith("/orderItems"):
                return httpx2.Response(
                    200,
                    json={
                        "payload": {
                            "OrderItems": [
                                {
                                    "SellerSKU": "AMZ-SOFA",
                                    "QuantityOrdered": 1,
                                    "ItemPrice": {"Amount": "400.00"},
                                }
                            ]
                        }
                    },
                )
        return self.handle(r)


@pytest.fixture
async def shop(tmp_path, monkeypatch):
    for k, v in {
        "T_HOOK_SECRET": "hook-secret-1",
        "SITE_SECRET": "site-secret",
        "AMZ_ID": "amzn1.app",
        "AMZ_SECRET": "s3cr3t",
        "AMZ_REFRESH": "Atzr-refresh",
        "CATALOG_KEY": "catalog-key-123",
    }.items():
        monkeypatch.setenv(k, v)
    llm, platforms = ScriptedLLM(), Shop()
    office = make_office(tmp_path, llm, http=platforms.client, channels=[WEBHOOK])
    client = TestClient(TestServer(create_app(office, PASSWORD)))
    await client.start_server()
    await call(client, "POST", "/api/login", {"password": PASSWORD})
    inv = office.inventory
    inv.kho = inv.save_warehouse(None, {"code": "KHO", "name": "Kho tổng"})["id"]
    ncc = inv.save_supplier(None, {"name": "Foshan", "lead_time_days": 45})["id"]
    ids = {}
    for sku, name, cat in [
        ("SOFA-01", "Sofa da 3 chỗ", "Phòng khách"),
        ("BAN-TRA", "Bàn trà gỗ", "Phòng khách"),
        ("KE-TV", "Kệ TV gỗ", "Phòng khách"),
        ("SOFA-02", "Sofa vải", "Phòng khách"),
    ]:
        ids[sku] = inv.save_product(None, {"sku": sku, "name": name, "category": cat, "cbm": "1"})["id"]
    inv.save_product(ids["SOFA-01"], {"box_count": 1})
    for sku, cost, qty in [
        ("SOFA-01", 6_000_000, 10),
        ("BAN-TRA", 1_200_000, 5),
        ("KE-TV", 1_800_000, 4),
        ("SOFA-02", 3_000_000, 3),
    ]:
        inv.add_opening_stock(ids[sku], inv.kho, qty, cost, margin_pct=40)
    inv.ids, inv.ncc = ids, ncc
    yield client, office, llm, platforms
    await client.close()


async def call(client, method, path, body=None, status=200):
    r = await client.request(method, path, json=body, headers=H)
    data = await r.json() if r.content_type == "application/json" else await r.text()
    assert r.status == status, data
    return data


async def as_user(client, username, role, **extra):
    await call(client, "POST", "/api/logout", {})
    await call(client, "POST", "/api/login", {"password": PASSWORD})
    await client.post(
        "/api/users",
        json={
            "username": username,
            "name": username.title(),
            "role": role,
            "password": "0123456789",
            **extra,
        },
        headers=H,
    )
    await call(client, "POST", "/api/logout", {})
    await call(client, "POST", "/api/login", {"username": username, "password": "0123456789"})


async def test_point_of_sale_loyalty_and_vip(shop):
    client, office, llm, platforms = shop
    inv = office.inventory
    sofa = inv.ids["SOFA-01"]
    assert inv.current_price(sofa)["price"] == 10_000_000
    inv.save_settings(
        {"shop_name": "Nội thất Minh An", "vat_pct": 8, "points_per": 1_000_000, "vip_points": 14}
    )
    await call(
        client,
        "POST",
        "/api/marketing/vouchers",
        {"code": "chao10", "discount_type": "pct", "value": 10, "max_discount": 500_000, "max_uses": 1},
    )

    # a customer who chatted with us before comes to the shop
    llm.responses.append(text("Dạ em chào anh."))
    r = await client.post(
        "/hooks/website",
        json={
            "conversation_id": "v1",
            "customer_name": "Anh Nam",
            "text": "Tôi ghé cửa hàng chiều nay, sđt 0901 111 222",
            "message_id": "m1",
        },
        headers=HOOK,
    )
    conv_id = (await r.json())["conversation"]
    await settle(office.hub)
    contact = office.hub.crm.contact_of(conv_id)

    await as_user(client, "linh", "cashier")
    found = await call(client, "GET", "/api/pos/customers?q=0901111222")
    assert [c["id"] for c in found["customers"]] == [contact["id"]]
    products = await call(client, "GET", "/api/pos/products?q=sofa")
    assert {p["sku"] for p in products["products"]} == {
        "SOFA-01",
        "SOFA-02",
    } and "landed_cost" not in products["products"][0]
    order = await call(
        client,
        "POST",
        "/api/pos/orders",
        {
            "contact_id": contact["id"],
            "voucher": "CHAO10",
            "items": [
                {"product_id": sofa, "qty": 1, "discount_pct": 5},
                {"product_id": inv.ids["BAN-TRA"], "qty": 1},
            ],
        },
    )
    assert order["items"][0]["unit_price"] == 9_500_000 and order["items"][0]["promo"] == "-5%"
    assert order["voucher_discount"] == 500_000 and order["total"] == 9_500_000 + 2_000_000 - 500_000
    assert order["salesperson"] == "linh" and order["payment_status"] == "unpaid"
    await call(
        client,
        "POST",
        "/api/pos/orders",
        {"voucher": "CHAO10", "items": [{"product_id": sofa, "qty": 1}]},
        status=400,
    )

    # a deposit by transfer, the rest in cash with change
    oid = order["id"]
    await call(
        client,
        "POST",
        f"/api/pos/orders/{oid}/payments",
        {"method": "transfer", "amount": 5_000_000, "idempotency_key": "k1"},
    )
    again = await call(
        client,
        "POST",
        f"/api/pos/orders/{oid}/payments",
        {"method": "transfer", "amount": 5_000_000, "idempotency_key": "k1"},
    )
    assert again["paid"] == 5_000_000 and again["payment_status"] == "partial"
    detail = await call(client, "GET", f"/api/pos/orders/{oid}")
    assert detail["due"] == 6_000_000 and detail["change_hints"][0] == 6_000_000
    paid = await call(
        client, "POST", f"/api/pos/orders/{oid}/payments", {"method": "cash", "tendered": 6_500_000}
    )
    assert paid["payment_status"] == "paid" and paid["payments"][-1]["change"] == 500_000
    await call(client, "POST", f"/api/pos/orders/{oid}/payments", {"method": "cash", "amount": 1}, status=400)
    receipt = await client.get(f"/api/pos/orders/{oid}/receipt")
    page = await receipt.text()
    assert (
        "Nội thất Minh An" in page
        and "Đã gồm VAT 8%" in page
        and "default-src 'none'" in receipt.headers["Content-Security-Policy"]
    )

    # handed over: stock out, points; the cashier cannot cancel or take it back
    done = await call(client, "POST", f"/api/pos/orders/{oid}/complete")
    assert done["status"] == "completed"
    await call(client, "POST", f"/api/pos/orders/{oid}/return", {}, status=403)
    c = office.hub.crm.contact(contact["id"])
    assert (c["points"], c["orders_count"], c["vip"]) == (11, 1, 0)
    assert len((await call(client, "GET", "/api/pos/orders"))["orders"]) == 1

    # the second purchase reaches 14 points: VIP, congratulated on their chat channel
    sent_before = len(platforms.sent)
    second = await call(
        client,
        "POST",
        "/api/pos/orders",
        {"contact_id": contact["id"], "items": [{"product_id": inv.ids["KE-TV"], "qty": 1}]},
    )
    await call(
        client,
        "POST",
        f"/api/pos/orders/{second['id']}/payments",
        {"method": "card", "amount": second["total"]},
    )
    await call(client, "POST", f"/api/pos/orders/{second['id']}/complete")
    await settle(office.hub)
    c = office.hub.crm.contact(contact["id"])
    assert c["vip"] == 1 and c["points"] == 11 + 3
    congrats = [s for s in platforms.sent[sent_before:] if "VIP" in s[1]["text"]]
    assert congrats and "VIP" + f"{contact['id']:06d}" in congrats[0][1]["text"]
    # VIP prices from now on: the next stage's price
    vip_view = await call(client, "GET", f"/api/pos/products?q=sofa-01&contact={contact['id']}")
    assert vip_view["vip"] and vip_view["products"][0]["price"] == 9_000_000

    # a manager takes a sale back within the undo window: stock, money and points return
    await as_user(client, "hoa", "manager")
    back = await call(client, "POST", f"/api/pos/orders/{second['id']}/return", {"reason": "Khách đổi ý"})
    assert back["status"] == "returned" and back["payment_status"] == "refunded" and back["paid"] == 0
    assert office.hub.crm.contact(contact["id"])["points"] == 11
    assert inv.product(inv.ids["KE-TV"])["available"] == 4
    points = await call(client, "GET", f"/api/crm/contacts/{contact['id']}/points")
    assert [x["delta"] for x in points["history"]][:2] == [-3, 3]


async def test_promotions_combos_and_room_sets(shop):
    client, office, _llm, _platforms = shop
    inv = office.inventory
    sofa, ban, ke = inv.ids["SOFA-01"], inv.ids["BAN-TRA"], inv.ids["KE-TV"]
    now = datetime.now().astimezone()
    promo = await call(
        client,
        "POST",
        "/api/marketing/promotions",
        {
            "name": "Sale 9.9",
            "badge": "Hot Deal",
            "discount_type": "pct",
            "value": 20,
            "starts": (now - timedelta(days=1)).date().isoformat(),
            "ends": (now + timedelta(days=3)).date().isoformat(),
            "product_ids": [sofa],
        },
    )
    price = inv.current_price(sofa)
    assert (price["price"], price["promo"], price["list_price"]) == (8_000_000, "Hot Deal", 10_000_000)
    # the % follows the automatic stage price
    inv.set_stage(sofa, 2, "Chủ")
    assert inv.current_price(sofa)["price"] == 7_200_000
    await call(client, "DELETE", f"/api/marketing/promotions/{promo['id']}")
    assert inv.current_price(sofa)["price"] == 9_000_000
    await call(
        client,
        "POST",
        "/api/marketing/promotions",
        {
            "name": "Xả kho phòng khách",
            "discount_type": "amount",
            "value": 300_000,
            "starts": now.date().isoformat(),
            "ends": now.date().isoformat(),
            "category": "phòng khách",
        },
    )
    assert inv.current_price(ban)["price"] == 2_000_000 - 300_000
    with pytest.raises(InventoryError, match="kết thúc"):
        inv.save_promotion(
            None,
            {
                "name": "x",
                "discount_type": "pct",
                "value": 10,
                "starts": "2026-01-02",
                "ends": "2026-01-01",
                "all_products": True,
            },
        )

    combo = await call(
        client,
        "POST",
        "/api/marketing/combos",
        {
            "code": "pk-01",
            "name": "Bộ phòng khách",
            "price": 10_000_000,
            "items": [{"product_id": sofa, "qty": 1}, {"product_id": ban, "qty": 1}],
        },
    )
    assert combo["separate_price"] == 9_000_000 - 300_000 + 1_700_000 and combo["available"] == 5
    order = inv.create_order([{"combo": "PK-01", "qty": 1}])
    assert (
        order["total"] == 10_000_000
        and order["discount"] == 400_000
        and {i["combo"] for i in order["items"]} == {"PK-01"}
    )

    # a living-room set within 12 million: everything in stock
    sets = await call(client, "GET", "/api/pos/sets?template=phong-khach&budget=15000000")
    assert sets["template"] == "Phòng khách" and sets["sets"]
    best = sets["sets"][0]
    assert best["total"] <= 15_000_000 and {i["sku"] for i in best["items"]} >= {"BAN-TRA", "KE-TV"}
    too_small = inv.suggest_sets("phòng khách", 1_000_000)
    assert too_small["sets"] == [] and too_small["cheapest"] > 1_000_000
    assert inv.suggest_sets("phòng ngủ", 50_000_000)["missing"] == ["Giường", "Nệm", "Tủ đầu giường"]
    assert inv.product(ke)["available"] == 4


async def test_commissions_expenses_and_profit_and_loss(shop):
    client, office, _llm, _platforms = shop
    inv = office.inventory
    today = datetime.now().astimezone().date()
    await as_user(client, "linh", "agent")
    for _ in range(2):
        o = await call(
            client, "POST", "/api/pos/orders", {"items": [{"product_id": inv.ids["SOFA-01"], "qty": 1}]}
        )
        await call(client, "POST", f"/api/pos/orders/{o['id']}/complete")
    await as_user(client, "hoa", "manager")
    await call(
        client,
        "PUT",
        "/api/reports/settings",
        {"target_per_hour": 1_000_000, "rate_pct": 2, "contribution_pct": 10},
    )
    await call(
        client,
        "POST",
        "/api/reports/shifts",
        {"username": "linh", "work_date": today.isoformat(), "hours": 8},
    )
    await call(
        client,
        "POST",
        "/api/reports/shifts",
        {"username": "linh", "work_date": today.isoformat(), "hours": 25},
        status=400,
    )
    r = await call(client, "GET", f"/api/reports/commissions?start={today}&end={today}")
    [row] = r["preview"]
    # sales 20 triệu, target 8 h x 1 triệu: (20 - 8) x 2% = 240.000; employer adds 10% of it
    assert (row["sales"], row["target"], row["excess"], row["commission"], row["contribution"]) == (
        20_000_000,
        8_000_000,
        12_000_000,
        240_000,
        24_000,
    )
    saved = await call(
        client, "POST", "/api/reports/commissions", {"start": str(today), "end": str(today), "save": True}
    )
    cid = saved["saved"][0]["id"]
    await call(client, "POST", f"/api/reports/commissions/{cid}/status", {"status": "paid"}, status=400)
    await call(client, "POST", f"/api/reports/commissions/{cid}/status", {"status": "finalized"})

    await call(
        client,
        "POST",
        "/api/marketing/ads",
        {
            "campaign": "FB tháng 9",
            "platform": "facebook",
            "amount": 3_000_000,
            "start_date": str(today - timedelta(days=29)),
            "end_date": str(today),
        },
    )
    await call(
        client,
        "POST",
        "/api/reports/expenses",
        {"name": "Thuê mặt bằng", "category": "rent", "amount": 5_000_000, "date": str(today)},
    )
    pnl = await call(client, "GET", f"/api/reports/pnl?start={today}&end={today}")
    assert (pnl["revenue"], pnl["cogs"], pnl["gross_profit"]) == (20_000_000, 12_000_000, 8_000_000)
    assert pnl["ads"] == 100_000 and pnl["expenses"] == {"rent": 5_000_000} and pnl["commissions"] == 264_000
    assert pnl["net_profit"] == 8_000_000 - 100_000 - 5_000_000 - 264_000

    top = await call(client, "GET", "/api/marketing/segment?kind=top")
    assert top["customers"] == []
    weekly = await call(client, "GET", "/api/marketing/weekly-prices")
    assert {p["sku"] for p in weekly["products"]} >= {"SOFA-01"}


async def test_delivery_from_booking_to_doorstep(shop):
    client, office, llm, platforms = shop
    inv = office.inventory
    tomorrow = (datetime.now().astimezone() + timedelta(days=1)).date().isoformat()
    llm.responses.append(text("Dạ vâng ạ."))
    r = await client.post(
        "/hooks/website",
        json={
            "conversation_id": "v7",
            "customer_name": "Chị Lan",
            "text": "Giao giúp em nhé",
            "message_id": "q1",
        },
        headers=HOOK,
    )
    conv_id = (await r.json())["conversation"]
    await settle(office.hub)
    orders = []
    # on one road east of the warehouse: far, near, middle
    for n, (addr, lat, lng) in enumerate(
        [("Xa", "10.7500", "106.9500"), ("Gần", "10.7500", "106.7000"), ("Giữa", "10.7500", "106.8000")]
    ):
        o = inv.create_order(
            [{"product_id": inv.ids["SOFA-01"], "qty": 1}],
            customer_name=f"Khách {n}",
            address=addr,
            conversation_id=conv_id if n == 0 else None,
        )
        orders.append((o, lat, lng))
    await as_user(client, "tuan", "delivery")
    assert (await client.get("/api/pos/orders")).status == 403
    carrier = await call(
        client,
        "POST",
        "/api/delivery/carriers",
        {
            "code": "noibo",
            "name": "Xe nhà",
            "internal": True,
            "rate_per_trip": 300_000,
            "rate_per_stop": 50_000,
        },
    )
    driver = await call(
        client,
        "POST",
        "/api/delivery/drivers",
        {"carrier_id": carrier["id"], "name": "Anh Hùng", "phone": "0909", "vehicle": "Tải 1.5T"},
    )
    await call(client, "PUT", f"/api/delivery/warehouses/{inv.kho}", {"lat": "10.7500", "lng": "106.6500"})
    bookings = []
    for o, lat, lng in orders:
        b = await call(
            client,
            "POST",
            "/api/delivery/bookings",
            {
                "order_id": o["id"],
                "delivery_date": tomorrow,
                "slot": "morning",
                "lat": lat,
                "lng": lng,
                "floors": 2 if o is orders[0][0] else 0,
                "assembling": o is orders[0][0],
                "window": "9-10h" if o is orders[0][0] else "",
            },
        )
        bookings.append(b)
    assert bookings[0]["surcharge"] == 2 * 50_000 + 200_000 and bookings[0]["special"]
    assert inv.order(orders[0][0]["id"])["total"] == 10_000_000 + 300_000
    await call(
        client,
        "POST",
        "/api/delivery/bookings",
        {"order_id": orders[0][0]["id"], "delivery_date": tomorrow},
        status=400,
    )
    cal = (await call(client, "GET", f"/api/delivery?month={tomorrow[:7]}&date={tomorrow}"))["calendar"]
    day = next(d for d in cal if d["date"] == tomorrow)
    assert (day["bookings"], day["boxes"], day["special"]) == (3, 3, 1)

    route = await call(
        client,
        "POST",
        "/api/delivery/routes",
        {
            "delivery_date": tomorrow,
            "carrier_id": carrier["id"],
            "driver_id": driver["id"],
            "origin_wh": inv.kho,
            "booking_ids": [b["id"] for b in bookings],
        },
    )
    assert route["cost"] == 300_000 + 3 * 50_000
    optimized = await call(client, "POST", f"/api/delivery/routes/{route['id']}/optimize")
    order_of_stops = [s["booking"]["address"] for s in optimized["stops"]]
    assert order_of_stops in (["Gần", "Giữa", "Xa"], ["Xa", "Giữa", "Gần"])
    assert optimized["optimized_by"] == "distance" and float(optimized["distance_km"]) > 0
    csv_text = await call(client, "GET", f"/api/delivery/routes/{route['id']}/list.csv")
    assert "Từ kho KHO" in csv_text and "Lắp ráp" in csv_text and "Vác 2 tầng" in csv_text

    sent = len(platforms.sent)
    await call(client, "POST", f"/api/delivery/routes/{route['id']}/start")
    await settle(office.hub)
    assert any("đang được giao" in s[1]["text"] for s in platforms.sent[sent:])
    await call(
        client,
        "POST",
        f"/api/delivery/routes/{route['id']}/result",
        {"booking_id": bookings[0]["id"], "result": "done"},
    )
    assert inv.order(orders[0][0]["id"])["status"] == "completed"
    await call(
        client,
        "POST",
        f"/api/delivery/routes/{route['id']}/result",
        {"booking_id": bookings[1]["id"], "result": "comeback", "note": "Khách vắng nhà"},
    )
    assert inv.order(orders[1][0]["id"])["status"] == "confirmed"  # goods stay reserved; book again
    final = await call(
        client,
        "POST",
        f"/api/delivery/routes/{route['id']}/result",
        {"booking_id": bookings[2]["id"], "result": "done"},
    )
    assert final["status"] == "completed"
    statement = await call(client, "GET", f"/api/delivery/statement?start={tomorrow}&end={tomorrow}")
    assert statement["carriers"][0]["trips"] == 1 and statement["carriers"][0]["done"] == 2


def test_route_ordering():
    points = [(0.0, 0.0), (0.0, 3.0), (0.0, 1.0), (0.0, 2.0)]
    matrix = [[haversine_km(a, b) for b in points] for a in points]
    assert best_order(matrix) in ([2, 3, 1], [1, 3, 2])


async def test_marketplaces_roles_notices_and_catalog(shop):
    client, office, _llm, platforms = shop
    inv = office.inventory
    mp = office.marketplaces
    sofa = inv.ids["SOFA-01"]
    await call(
        client,
        "POST",
        "/api/inventory/marketplaces",
        {"id": "website", "type": "webhook", "url": "https://site.local/sync", "secret_env": "SITE_SECRET"},
    )
    await call(
        client,
        "POST",
        "/api/inventory/marketplaces",
        {
            "id": "amazon-au",
            "type": "amazon",
            "seller_id": "A1SELLER",
            "marketplace_id": "A39IBJ37TRP1C6",
            "region": "fe",
            "currency": "AUD",
            "price_rate": "0.00006",
            "client_id_env": "AMZ_ID",
            "client_secret_env": "AMZ_SECRET",
            "refresh_token_env": "AMZ_REFRESH",
            "pull_orders": True,
            "warehouse_id": inv.kho,
            "api_url": "https://sp.local",
            "token_url": "https://lwa.local/token",
        },
    )
    listed = await call(client, "GET", "/api/inventory/marketplaces")
    assert listed["marketplaces"][1]["env_set"] == {
        "client_id_env": True,
        "client_secret_env": True,
        "refresh_token_env": True,
    }
    assert "s3cr3t" not in json.dumps(listed)
    await call(
        client,
        "POST",
        f"/api/inventory/products/{sofa}/external-sku",
        {"marketplace": "amazon-au", "sku": "AMZ-SOFA"},
    )
    await call(
        client,
        "POST",
        f"/api/inventory/products/{inv.ids['SOFA-02']}/external-sku",
        {"marketplace": "amazon-au", "sku": "-"},
    )

    # a sale changes the stock: both are told within the next round
    inv.complete_order(inv.create_order([{"product_id": sofa, "qty": 2}])["id"])
    assert await mp.process() == 2  # the sofa, to the website and to Amazon (SOFA-02 is kept off Amazon)
    body, signature = next(x for x in platforms.site if x[0]["sku"] == "SOFA-01")
    raw = json.dumps(body, ensure_ascii=False).encode()
    assert signature == "sha256=" + hmac.new(b"site-secret", raw, hashlib.sha256).hexdigest()
    assert body["available"] == 8 and body["price"] == 10_000_000
    method, path, patch = next(x for x in platforms.amazon if x[1].startswith("/listings/"))
    assert (method, path) == ("PATCH", "/listings/2021-08-01/items/A1SELLER/AMZ-SOFA")
    offer = patch["patches"][0]["value"][0]
    assert offer["currency"] == "AUD" and offer["our_price"][0]["schedule"][0]["value_with_tax"] == 600.0
    assert patch["patches"][1]["value"][0]["quantity"] == 8
    assert not any("SOFA-02" in p for _m, p, _b in platforms.amazon)  # kept off Amazon

    # an Amazon order: reserved here; shipped there -> completed here
    platforms.amazon_orders = [
        {"AmazonOrderId": "249-1", "OrderStatus": "Unshipped", "LastUpdateDate": "2026-09-27T01:00:00Z"}
    ]
    stats = await mp.pull_amazon_orders(mp.config("amazon-au"))
    assert stats["created"] == 1
    [amz] = [o for o in inv.orders() if o["channel"] == "amazon"]
    assert amz["total"] == 6_666_667 and amz["external_ref"] == "amazon:249-1"  # AUD 400 / 0.00006
    platforms.amazon_orders[0]["OrderStatus"] = "Shipped"
    assert (await mp.pull_amazon_orders(mp.config("amazon-au")))["completed"] == 1
    assert inv.order(amz["id"])["status"] == "completed"

    # roles
    await as_user(client, "kho1", "warehouse")
    assert (await client.get("/api/inventory")).status == 200
    assert (await client.put("/api/inventory/settings", json={}, headers=H)).status == 403
    assert (await client.get("/api/inventory/marketplaces")).status == 403
    assert (await client.get("/api/delivery")).status == 403
    await as_user(client, "mkt", "marketing")
    assert (await client.get("/api/inventory/products")).status == 200
    assert (
        await client.post("/api/inventory/products", json={"sku": "X", "name": "x"}, headers=H)
    ).status == 403
    assert (await client.get("/api/marketing")).status == 200
    assert (await client.get("/api/reports/pnl")).status == 200

    # a start-of-day notice
    await as_user(client, "hoa", "manager")
    note = await call(
        client, "POST", "/api/notices", {"title": "Kiểm kê cuối tháng", "body": "Tối nay 20h kiểm kê kho."}
    )
    await as_user(client, "linh", "cashier")
    assert [n["id"] for n in (await call(client, "GET", "/api/notices"))["pending"]] == [note["id"]]
    await call(client, "POST", f"/api/notices/{note['id']}/skip")
    assert (await call(client, "GET", "/api/notices"))["pending"] == []  # until tomorrow
    await call(client, "POST", f"/api/notices/{note['id']}/close", status=403)

    # the public catalog for the website: only with its key, never costs
    assert (await client.get("/hooks/catalog")).status == 404
    r = await client.get("/hooks/catalog?key=catalog-key-123&q=sofa")
    data = await r.json()
    assert {p["sku"] for p in data["products"]} == {"SOFA-01", "SOFA-02"} and "landed_cost" not in data[
        "products"
    ][0]
