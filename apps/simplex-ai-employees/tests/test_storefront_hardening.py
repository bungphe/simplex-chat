"""The web shop against visitors who try what customers don't."""

from __future__ import annotations

import re

from test_channels import settle
from test_storefront import last_code, post, site  # noqa: F401  (the fixture)


def login_cookie(client) -> str:
    return next((c.value for c in client.session.cookie_jar if c.key == "sf_login"), "")


async def test_a_guest_cannot_take_over_someone_elses_orders(site):  # noqa: F811
    shop, office, mails, _p, _ids = site
    # the attacker orders first with the victim's phone number and their own email
    await shop.get("/")
    await post(shop, "/cart/add", item="SOFA-01", qty="1")
    await post(
        shop,
        "/checkout",
        status=200,
        name="Kẻ gian",
        phone="0977 111 222",
        email="att@evil.test",
        address="A",
    )
    # the victim orders later as a guest, with their own phone number
    shop.session.cookie_jar.clear()
    await shop.get("/")
    await post(shop, "/cart/add", item="SOFA-01", qty="1")
    await post(shop, "/checkout", status=200, name="Nạn nhân", phone="0977111222", address="B")
    # the attacker cannot log in with the email they typed
    shop.session.cookie_jar.clear()
    await shop.get("/login")
    await post(shop, "/login", who="att@evil.test")
    await settle(office.hub)
    assert not any(m[1]["Subject"] and "mã đăng nhập" in m[1]["Subject"] for m in mails)


async def test_the_login_cookie_says_nothing_about_who_is_a_customer(site):  # noqa: F811
    shop, office, _m, _p, _ids = site
    office.hub.crm.create_contact("Hoa", "0901 234 567", "hoa@example.com")
    await shop.get("/login")
    await post(shop, "/login", who="hoa@example.com")
    known = login_cookie(shop)
    await post(shop, "/login", who="nobody@example.com")
    unknown = login_cookie(shop)
    shape = re.compile(r"[\w-]{24}\.[0-9a-f]{32}")
    assert shape.fullmatch(known) and shape.fullmatch(unknown) and known != unknown


async def test_codes_and_links_only_work_where_they_belong(site):  # noqa: F811
    shop, office, mails, _p, _ids = site
    crm = office.hub.crm
    hoa = crm.create_contact("Hoa", "0901 234 567", "hoa@example.com")
    sf = office.storefront
    # a login link's token is not a code, and a code is not a login link
    link = sf.magic_link(int(hoa["id"]))
    handle, token = link.split("/")[-2:]
    assert sf.verify_code(handle, token) is None
    await shop.get("/login")
    await post(shop, "/login", who="hoa@example.com")
    await settle(office.hub)
    code = last_code(mails)
    code_handle = login_cookie(shop).rpartition(".")[0]
    await post(shop, "/l", status=400, id=code_handle, token=code)
    r, _ = await post(shop, "/verify", code=code)  # still good: nothing used it up
    assert r.headers["Location"] == "/account"
    assert sf.verify_code(handle, token, link=True) == hoa["id"]


async def test_bad_order_codes_are_not_found(site):  # noqa: F811
    shop, _office, _m, _p, _ids = site
    for code in ("DH99999999999999999999999", "DH²", "x"):
        assert (await shop.get(f"/order/{code}")).status == 404
        assert (await shop.get(f"/account/invoice/{code}")).status == 404
    await shop.get("/")
    await post(shop, "/l", status=400, id="²", token="x")


async def test_redirects_get_the_security_headers_and_the_language(site):  # noqa: F811
    shop, _office, _m, _p, _ids = site
    r = await shop.get("/account?lang=en", allow_redirects=False)
    assert r.status == 303
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.cookies["sf_lang"].value == "en"


async def test_the_cart_orders_what_it_shows(site):  # noqa: F811
    shop, office, _m, _p, _ids = site
    await shop.get("/")
    await post(shop, "/cart/add", item="SOFA-01", qty="1")
    await post(shop, "/cart/add", item="GARBAGE", qty="2")
    cart = await (await shop.get("/cart")).text()
    assert "Giỏ hàng (1)" in cart  # the unknown line is gone for good
    _r, body = await post(
        shop, "/checkout", status=200, name="Hoa", phone="0901 234 567", address="12 Lê Lợi"
    )
    assert "Cảm ơn quý khách" in body
    order = office.inventory.order(office.inventory.orders()[0]["id"])
    assert [i["sku"] for i in order["items"]] == ["SOFA-01"]


async def test_stock_shown_is_the_stock_the_website_sells_from(site):  # noqa: F811
    shop, office, _m, _p, _ids = site
    inv = office.inventory
    other = inv.save_warehouse(None, {"code": "Q7", "name": "Kho Quận 7"})["id"]
    lamp = inv.save_product(None, {"sku": "DEN-01", "name": "Đèn bàn"})["id"]
    inv.add_opening_stock(lamp, other, 4, 200_000, margin_pct=40)
    page = await (await shop.get("/p/DEN-01")).text()
    assert "Còn hàng" not in page


async def test_a_phone_number_cannot_pile_up_unpaid_web_orders(site):  # noqa: F811
    shop, _office, _m, _p, _ids = site
    await shop.get("/")
    for _ in range(3):
        await post(shop, "/cart/add", item="SOFA-01", qty="1")
        await post(shop, "/checkout", status=200, name="Hoa", phone="0901 555 666", address="x")
    await post(shop, "/cart/add", item="SOFA-01", qty="1")
    _r, body = await post(shop, "/checkout", status=400, name="Hoa", phone="0901555666", address="x")
    assert "đang chờ cửa hàng xác nhận" in body


def claim_url(mails) -> str:
    body = mails[-1][1].get_content()
    return re.search(r"http://shop\.test(/claim\?\S+)", body).group(1)


async def test_a_guest_makes_their_account_from_the_confirmation_email(site):  # noqa: F811
    shop, office, mails, _p, _ids = site
    await shop.get("/")
    await post(shop, "/cart/add", item="SOFA-01", qty="1")
    await post(
        shop, "/checkout", status=200, name="Mai", phone="0933 000 111", email="mai@example.com", address="x"
    )
    await settle(office.hub)
    link = claim_url(mails)
    order = office.inventory.orders()[0]
    # the page only shows a button (mail scanners opening the link use nothing up)
    page = await (await shop.get(link)).text()
    assert "mai@example.com" in page and office.hub.crm.contact(order["contact_id"])["email"] == ""
    query = dict(x.split("=", 1) for x in link.split("?", 1)[1].split("&"))
    from urllib.parse import unquote

    fields = {k: unquote(v) for k, v in query.items()}
    await post(
        shop, "/claim", status=400, **{**fields, "e": "att@evil.test"}
    )  # the signature binds the email
    r, _ = await post(shop, "/claim", **fields)
    assert r.headers["Location"] == "/account"
    assert order["code"] in await (await shop.get("/account")).text()
    assert office.hub.crm.contact(order["contact_id"])["email"] == "mai@example.com"


async def test_a_claim_link_never_opens_a_counter_customer(site):  # noqa: F811
    shop, office, mails, _p, _ids = site
    crm = office.hub.crm
    # a customer staff met at the counter (no email, no chat), with a purchase there
    lan = crm.create_contact("Chị Lan", "0944 222 333")
    office.inventory.create_order([{"sku": "SOFA-01", "qty": 1}], contact_id=lan["id"])
    # someone orders on the website with her number and their own email
    await shop.get("/")
    await post(shop, "/cart/add", item="SOFA-01", qty="1")
    await post(
        shop, "/checkout", status=200, name="Lạ", phone="0944222333", email="att@evil.test", address="x"
    )
    await settle(office.hub)
    assert office.inventory.orders()[0]["contact_id"] == lan["id"]
    from urllib.parse import parse_qsl

    fields = dict(parse_qsl(claim_url(mails).split("?", 1)[1]))
    await post(shop, "/claim", status=400, **fields)
    assert crm.contact(lan["id"])["email"] == ""


async def test_unpaid_web_orders_nobody_took_up_go_back_on_sale(site):  # noqa: F811
    shop, office, mails, _p, _ids = site
    inv, sf = office.inventory, office.storefront
    await shop.get("/")
    await post(shop, "/cart/add", item="SOFA-01", qty="2")
    await post(
        shop, "/checkout", status=200, name="Hoa", phone="0901 234 567", email="hoa@example.com", address="x"
    )
    await post(shop, "/cart/add", item="SOFA-01", qty="1")  # more than in stock: a preorder, paid below
    await post(shop, "/checkout", status=200, name="Tú", phone="0902 000 000", address="y")
    await settle(office.hub)
    first, paid = sorted(inv.orders(), key=lambda o: o["id"])
    inv.add_payment(paid["id"], "cash", cashier="t")
    assert await sf.release_stale() == []  # not old yet
    sf.hold_hours = 1
    office.hub.crm.db.execute("UPDATE inv_orders SET created='2020-01-01T00:00:00+00:00'")
    assert await sf.release_stale() == [first["code"]]
    assert inv.order(first["id"])["status"] == "cancelled"
    assert inv.order(paid["id"])["status"] == "confirmed"
    assert "đã huỷ" in mails[-1][1]["Subject"] and mails[-1][1]["To"] == "hoa@example.com"
    sf.hold_hours = 0
    assert await sf.release_stale() == []
