"""Admin web UI hardening: role limits, customer data for channel-limited staff, discounts
at the counter, bad input answered with 400 (not 500), sessions and login throttling."""

from __future__ import annotations

from test_business import as_user, call, shop  # noqa: F401 - fixtures
from test_channels import PASSWORD, H

from ai_employees import web
from ai_employees.db import Database


async def test_marketing_cannot_read_marketplace_or_mail_settings(shop):  # noqa: F811
    client, *_ = shop
    await as_user(client, "mkt", "marketing")
    assert (await client.get("/api/inventory/products")).status == 200  # inventory-read
    for path in ("/api/inventory/marketplaces", "/api/inventory/mail", "/api/inventory/settings"):
        assert (await client.get(path)).status == 403, path
    await as_user(client, "hoa", "manager")
    assert (await client.get("/api/inventory/mail")).status == 200  # managers may look
    assert (await client.put("/api/inventory/mail", json={}, headers=H)).status == 403


async def test_marketing_reads_reports_but_does_not_change_them(shop):  # noqa: F811
    client, *_ = shop
    await as_user(client, "mkt", "marketing")
    for path in ("/api/reports/pnl", "/api/reports/settings", "/api/reports/commissions"):
        assert (await client.get(path)).status == 200, path
    assert (await client.put("/api/reports/settings", json={"rate_pct": 50}, headers=H)).status == 403
    expense = {"name": "Điện", "category": "utilities", "amount": 1, "date": "2026-09-01"}
    assert (await client.post("/api/reports/expenses", json=expense, headers=H)).status == 403


async def test_only_admins_set_vip_or_merge_customers_in_the_crm(shop):  # noqa: F811
    client, office, *_ = shop
    crm = office.hub.crm
    a = crm.create_contact("Chị Mai", "0901234567")
    b = crm.create_contact("Mai", "0901 234 567")
    await as_user(client, "hoa", "manager")
    await call(client, "PATCH", f"/api/crm/contacts/{a['id']}", {"vip": True, "notes": "Khách quen"})
    assert (crm.contact(a["id"])["vip"], crm.contact(a["id"])["notes"]) == (0, "Khách quen")
    await call(client, "POST", f"/api/crm/contacts/{a['id']}/merge", {"other": b["id"]}, status=403)
    assert crm.contact(b["id"]) is not None
    await call(client, "PATCH", f"/api/crm/contacts/{a['id']}", {"lat": [1]}, status=400)


async def test_channel_limited_staff_find_customers_only_by_exact_phone_or_email(shop):  # noqa: F811
    client, office, *_ = shop
    crm = office.hub.crm
    nam = crm.create_contact("Anh Nam", "0901 111 222", "nam@example.com")
    crm.create_contact("Anh Nam Sơn", "0902 333 444")
    await as_user(client, "hoa", "manager")
    assert len((await call(client, "GET", "/api/pos/customers?q=Nam"))["customers"]) == 2
    await as_user(client, "linh", "agent", channels=["website"])
    for q in ("", "Nam", "0901", "example.com"):
        assert (await call(client, "GET", f"/api/pos/customers?q={q}"))["customers"] == [], q
    for q in ("0901111222", "+84 901 111 222", "NAM@example.com"):
        found = (await call(client, "GET", f"/api/pos/customers?q={q}"))["customers"]
        assert [c["id"] for c in found] == [nam["id"]], q


async def test_staff_discounts_are_capped_managers_are_not(shop):  # noqa: F811
    client, office, *_ = shop
    inv = office.inventory
    sofa = inv.ids["SOFA-01"]
    assert inv.current_price(sofa)["price"] == 10_000_000

    def sale(**line):
        return {"items": [{"product_id": sofa, "qty": 1, **line}]}

    await as_user(client, "linh", "cashier")
    ok = await call(client, "POST", "/api/pos/orders", sale(discount_pct=10))
    assert ok["items"][0]["unit_price"] == 9_000_000
    await call(client, "POST", "/api/pos/orders", sale(unit_price=9_500_000))
    await call(client, "POST", "/api/pos/orders", {**sale(), "discount": 1_000_000})
    for body in (
        sale(discount_pct=20),
        sale(unit_price=5_000_000),
        sale(unit_price=9_500_000, discount_pct=10),  # 14.5% together
        {**sale(discount_pct=5), "discount": 600_000},
    ):
        await call(client, "POST", "/api/pos/orders", body, status=403)
    await as_user(client, "kho1", "warehouse")  # orders from the inventory page, too
    await call(client, "POST", "/api/inventory/orders", sale(unit_price=1_000_000), status=403)
    await as_user(client, "hoa", "manager")
    order = await call(client, "POST", "/api/pos/orders", sale(discount_pct=30))
    assert order["items"][0]["unit_price"] == 7_000_000


async def test_bad_input_is_a_400_not_a_500(shop, monkeypatch):  # noqa: F811
    client, office, *_ = shop
    inv = office.inventory
    shop2 = inv.save_warehouse(None, {"code": "CH1", "name": "Cửa hàng 1"})["id"]
    t = inv.create_transfer(inv.kho, shop2, [{"product_id": inv.ids["SOFA-01"], "qty": 1}])
    conv = office.hub.inbox.upsert("website", "v1", "Mai", "sales")
    monkeypatch.setenv("CATALOG_KEY", "catalog-key-123")
    for method, path, body in [
        ("GET", "/api/employees/sales/conversations/abc", None),
        ("DELETE", "/api/employees/sales/conversations/abc", None),
        ("DELETE", "/api/employees/sales/admins/abc", None),
        ("POST", "/api/approvals/sales/abc/approve", {}),
        ("GET", "/api/runlog?limit=x", None),
        ("POST", f"/api/inventory/transfers/{t['id']}/receive", {"received": {"x": 1}}),
        ("POST", f"/api/inventory/transfers/{t['id']}/receive", {"received": [1]}),
        ("POST", "/api/users", {"username": "an", "role": "agent", "password": "0123456789", "channels": 5}),
        (
            "POST",
            "/api/users",
            {"username": "an", "role": "agent", "password": "0123456789", "channels": [{}]},
        ),
        ("PUT", "/api/desk/labels", {"value": ["Gấp"]}),
        ("POST", f"/api/inbox/{conv.id}/labels", {"labels": [{}]}),
        ("POST", "/api/pos/orders", {"contact_id": [1], "items": []}),
        ("GET", "/hooks/catalog?key=catalog-key-123&limit=x", None),
    ]:
        r = await client.request(method, path, json=body, headers=H)
        assert r.status == 400, (method, path, r.status, await r.text())
    # a label rename with a malformed "was" is simply not a rename
    desk = await call(client, "PUT", "/api/desk/labels", {"value": [{"name": "Gấp", "was": ["x"]}]})
    assert [lb["name"] for lb in desk["labels"]] == ["Gấp"]


async def test_changing_ones_password_ends_the_other_sessions(shop):  # noqa: F811
    client, *_ = shop
    sessions = client.server.app[web.SESSIONS]
    await as_user(client, "linh", "cashier")
    elsewhere = client.session.cookie_jar.filter_cookies(client.make_url("/"))[web.COOKIE].value
    await call(client, "POST", "/api/login", {"username": "linh", "password": "0123456789"})
    here = client.session.cookie_jar.filter_cookies(client.make_url("/"))[web.COOKIE].value
    assert sessions.get(elsewhere) and sessions.get(here)
    await call(client, "POST", "/api/me/password", {"old": "0123456789", "new": "a new password 1"})
    fresh = client.session.cookie_jar.filter_cookies(client.make_url("/"))[web.COOKIE].value
    assert sessions.get(elsewhere) is None and sessions.get(here) is None
    assert fresh not in (elsewhere, here) and sessions.get(fresh)
    assert (await call(client, "GET", "/api/me"))["user"]["username"] == "linh"  # still logged in here


async def test_session_cookie_is_secure_behind_a_tls_proxy(shop):  # noqa: F811
    client, *_ = shop
    r = await client.post("/api/login", json={"password": PASSWORD}, headers=H)
    assert "Secure" not in r.headers["Set-Cookie"]  # plain http on localhost still works
    r = await client.post(
        "/api/login", json={"password": PASSWORD}, headers={**H, "X-Forwarded-Proto": "https"}
    )
    assert "Secure" in r.headers["Set-Cookie"]


async def test_login_failures_are_limited_per_address_and_per_account(shop, monkeypatch):  # noqa: F811
    client, *_ = shop
    monkeypatch.setattr(web, "LOGIN_DELAY", 0)
    await as_user(client, "linh", "cashier")

    async def attempt(username, password, ip):
        body = {"username": username, "password": password}
        return (await client.post("/api/login", json=body, headers={**H, "X-Forwarded-For": ip})).status

    # one account guessed from many addresses
    for i in range(web.LOGIN_MAX_FAILURES):
        assert await attempt("linh", "wrong", f"203.0.113.{i}") == 401
    assert await attempt("linh", "0123456789", "198.51.100.1") == 429
    assert await attempt("admin", PASSWORD, "198.51.100.1") == 200  # other accounts still work
    # many accounts guessed from one address
    for i in range(web.LOGIN_MAX_FAILURES):
        assert await attempt(f"user{i}", "wrong", "198.51.100.9") == 401
    assert await attempt("admin", PASSWORD, "198.51.100.9") == 429
    assert await attempt("admin", PASSWORD, "198.51.100.10") == 200


def test_real_columns_are_double_precision_in_postgresql(tmp_path):
    db = Database(str(tmp_path / "x.db"))
    assert db.ddl("expiry {real}") == "expiry REAL"
    db.postgres = True
    assert db.ddl("expiry {real}") == "expiry DOUBLE PRECISION"
