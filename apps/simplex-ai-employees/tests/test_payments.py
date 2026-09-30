"""Online payments on the web shop: VNPay, MoMo and ZaloPay, their notifications, the pay
pages and the staff's pay links."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from urllib.parse import parse_qs, parse_qsl, quote_plus, urlsplit

import httpx2
import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_channels import PASSWORD, WEBHOOK, H, Platforms, settle
from test_storefront import post

from ai_employees.mailer import Mailer
from ai_employees.payments import payments_of
from ai_employees.storefront import create_shop_app
from ai_employees.web import create_app

from fakes import ScriptedLLM, make_office

SITE = "http://shop.test"
VNPAY_SECRET, MOMO_SECRET, KEY1, KEY2 = "vnpay-hash-secret", "momo-secret-key", "zalo-key-1", "zalo-key-2"
SETTINGS = {
    "sandbox": True,
    "vnpay": {"enabled": True, "tmn_code": "TMN01", "secret_env": "T_VNPAY_SECRET"},
    "momo": {
        "enabled": True,
        "partner_code": "MOMOTEST",
        "access_key": "akey",
        "secret_env": "T_MOMO_SECRET",
    },
    "zalopay": {
        "enabled": True,
        "app_id": "2553",
        "key1_env": "T_ZALOPAY_KEY1",
        "key2_env": "T_ZALOPAY_KEY2",
    },
    "bank": {"enabled": True, "info": "Vietcombank 0011 000 123 456 - Nguyen Van A"},
}


def sha256(key: str, data: str) -> str:
    return hmac.new(key.encode(), data.encode(), hashlib.sha256).hexdigest()


def vnpay_sign(params: dict) -> str:
    # as VNPay hashes: every vnp_ parameter but the hash and its type, sorted, quote_plus
    skip = ("vnp_SecureHash", "vnp_SecureHashType")
    query = "&".join(f"{k}={quote_plus(str(params[k]))}" for k in sorted(params) if k not in skip)
    return hmac.new(VNPAY_SECRET.encode(), query.encode(), hashlib.sha512).hexdigest()


def momo_sign(p: dict) -> str:
    keys = (
        "amount",
        "extraData",
        "message",
        "orderId",
        "orderInfo",
        "orderType",
        "partnerCode",
        "payType",
        "requestId",
        "responseTime",
        "resultCode",
        "transId",
    )
    return sha256(MOMO_SECRET, "accessKey=akey&" + "&".join(f"{k}={p[k]}" for k in keys))


class Gateways(Platforms):
    """The chat bridge plus the MoMo and ZaloPay create-order APIs."""

    def __init__(self) -> None:
        super().__init__()
        self.created: list[dict] = []
        self.refuse = False
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.route))

    def route(self, r: httpx2.Request) -> httpx2.Response:
        if r.url.host == "test-payment.momo.vn":
            body = json.loads(r.content)
            raw = "&".join(
                f"{k}={body[j]}"
                for k, j in (
                    ("accessKey", "accessKey"),
                    ("amount", "amount"),
                    ("extraData", "extraData"),
                    ("ipnUrl", "ipnUrl"),
                    ("orderId", "orderId"),
                    ("orderInfo", "orderInfo"),
                    ("partnerCode", "partnerCode"),
                    ("redirectUrl", "redirectUrl"),
                    ("requestId", "requestId"),
                    ("requestType", "requestType"),
                )
                if j != "accessKey"
            )
            raw = "accessKey=akey&" + raw
            assert body["signature"] == sha256(MOMO_SECRET, raw), "MoMo signature"
            self.created.append(("momo", body))
            if self.refuse:
                return httpx2.Response(200, json={"resultCode": 41, "message": "Duplicate orderId"})
            return httpx2.Response(
                200, json={"resultCode": 0, "payUrl": f"https://test-payment.momo.vn/pay/{body['orderId']}"}
            )
        if r.url.host == "sb-openapi.zalopay.vn":
            form = {k: v[0] for k, v in parse_qs(r.content.decode(), keep_blank_values=True).items()}
            data = "|".join(
                form[k]
                for k in ("app_id", "app_trans_id", "app_user", "amount", "app_time", "embed_data", "item")
            )
            assert form["mac"] == sha256(KEY1, data), "ZaloPay mac"
            self.created.append(("zalopay", form))
            return httpx2.Response(
                200,
                json={
                    "return_code": 1,
                    "order_url": f"https://sbgateway.zalopay.vn/openinapp?order={form['app_trans_id']}",
                },
            )
        return self.handle(r)


@pytest.fixture
async def site(tmp_path, monkeypatch):
    monkeypatch.setenv("T_HOOK_SECRET", "hook-secret-1")
    monkeypatch.setenv("SHOP_SMTP_PASSWORD", "smtp-pass")
    monkeypatch.setenv("T_VNPAY_SECRET", VNPAY_SECRET)
    monkeypatch.setenv("T_MOMO_SECRET", MOMO_SECRET)
    monkeypatch.setenv("T_ZALOPAY_KEY1", KEY1)
    monkeypatch.setenv("T_ZALOPAY_KEY2", KEY2)
    mails: list = []
    monkeypatch.setattr(Mailer, "_send_now", lambda self, smtp, msg: mails.append((smtp, msg)))
    gateways = Gateways()
    office = make_office(
        tmp_path, ScriptedLLM(), http=gateways.client, channels=[WEBHOOK], storefront={"public_url": SITE}
    )
    office.mailer.save(
        {
            "smtp_host": "smtp.test",
            "smtp_user": "shop@test.vn",
            "password_env": "SHOP_SMTP_PASSWORD",
            "sender": "shop@test.vn",
        }
    )
    inv = office.inventory
    inv.save_settings({"shop_name": "Nội thất ABC"})
    kho = inv.save_warehouse(None, {"code": "Q1", "name": "Showroom Quận 1"})["id"]
    sofa = inv.save_product(None, {"sku": "SOFA-01", "name": "Sofa da 3 chỗ", "category": "Phòng khách"})[
        "id"
    ]
    inv.add_opening_stock(sofa, kho, 10, 6_000_000, margin_pct=40)  # sells at 10.000.000
    payments_of(office).save(SETTINGS)
    shop = TestClient(TestServer(create_shop_app(office)))
    await shop.start_server()
    yield shop, office, mails, gateways
    await shop.close()


async def buy(shop, office, mails, email="hoa@example.com"):
    """A guest's web order of one sofa: (order, its private token, the thank-you page)."""
    await shop.get("/")
    await post(shop, "/cart/add", item="SOFA-01", qty="1")
    _r, page = await post(
        shop, "/checkout", status=200, name="Hoa Nguyễn", phone="0901234567", email=email, address="12 Lê Lợi"
    )
    order = office.inventory.orders()[0]
    await settle(office.hub)
    token = re.search(rf"/order/{order['code']}\?t=(\w+)", page).group(1)
    return order, token, page


async def test_vnpay_redirect_ipn_and_return(site):
    shop, office, mails, _g = site
    inv = office.inventory
    pay = payments_of(office)
    assert pay.enabled_gateways() == ["vnpay", "momo", "zalopay"]
    order, token, page = await buy(shop, office, mails)
    code = order["code"]
    assert f"/pay/{code}?t={token}" in page and "Thanh toán online" in page  # thank-you page
    assert f"{SITE}/pay/{code}?t={token}" in mails[-1][1].get_content()  # confirmation email
    tracking = await (await shop.get(f"/order/{code}?t={token}")).text()
    assert "Thanh toán online" in tracking

    # the pay page: the customer's own order only (or with its private token)
    assert (await shop.get(f"/pay/{code}")).status == 404
    assert (await shop.get(f"/pay/{code}?t=deadbeef")).status == 404
    r = await shop.get(f"/pay/{code}?t={token}")
    page = await r.text()
    assert r.status == 200 and "Còn phải trả: <b>10.000.000 đ" not in page and "10.000.000 đ" in page
    assert all(f'value="{g}"' in page for g in ("vnpay", "momo", "zalopay"))
    assert "Vietcombank 0011" in page and code in page
    assert "script-src" not in r.headers["Content-Security-Policy"]

    # to VNPay: a signed redirect built on the server
    r, _ = await post(shop, f"/pay/{code}?t={token}", gateway="vnpay")
    url = r.headers["Location"]
    assert url.startswith("https://sandbox.vnpayment.vn/paymentv2/vpcpay.html?")
    q = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    assert q["vnp_Amount"] == "1000000000" and q["vnp_TmnCode"] == "TMN01" and q["vnp_CurrCode"] == "VND"
    assert q["vnp_ReturnUrl"] == f"{SITE}/pay/return/vnpay" and q["vnp_TxnRef"].startswith(code)
    assert re.fullmatch(r"\d{14}", q["vnp_CreateDate"]) and q["vnp_ExpireDate"] > q["vnp_CreateDate"]
    assert q["vnp_OrderInfo"].isascii() and len(q["vnp_TxnRef"]) <= 100
    assert q["vnp_SecureHash"] == vnpay_sign(q)  # recomputed here, independently
    ref = q["vnp_TxnRef"]
    assert pay.intent(ref)["status"] == "pending" and pay.intent(ref)["amount"] == 10_000_000

    async def ipn(**params):
        r = await shop.get("/pay/ipn/vnpay", params=params)
        assert r.status == 200
        return (await r.json())["RspCode"]

    ok = {
        "vnp_Amount": "1000000000",
        "vnp_BankCode": "NCB",
        "vnp_OrderInfo": q["vnp_OrderInfo"],
        "vnp_PayDate": "20260929101500",
        "vnp_ResponseCode": "00",
        "vnp_TmnCode": "TMN01",
        "vnp_TransactionNo": "14512345",
        "vnp_TransactionStatus": "00",
        "vnp_TxnRef": ref,
        "vnp_SecureHashType": "HmacSHA512",
    }
    assert await ipn(**{**ok, "vnp_SecureHash": "00" * 64}) == "97"
    tampered = {**ok, "vnp_TransactionNo": "1"}
    assert await ipn(**tampered, vnp_SecureHash=vnpay_sign(ok)) == "97"
    bad_amount = {**ok, "vnp_Amount": "500000000"}
    assert await ipn(**bad_amount, vnp_SecureHash=vnpay_sign(bad_amount)) == "04"
    unknown = {**ok, "vnp_TxnRef": "DH99999AAAA"}
    assert await ipn(**unknown, vnp_SecureHash=vnpay_sign(unknown)) == "01"
    assert inv.order(order["id"])["paid"] == 0
    assert await ipn(**ok, vnp_SecureHash=vnpay_sign(ok)) == "00"
    paid = inv.order(order["id"])
    assert paid["paid"] == 10_000_000 and paid["payment_status"] == "paid"
    assert paid["payments"][-1]["method"] == "transfer" and "14512345" in paid["payments"][-1]["ref"]
    assert paid["payments"][-1]["idempotency_key"] == f"vnpay:{ref}"
    assert await ipn(**ok, vnp_SecureHash=vnpay_sign(ok)) == "02"  # retried: nothing recorded twice
    assert inv.order(order["id"])["paid"] == 10_000_000 and len(inv.order(order["id"])["payments"]) == 1
    intent = pay.intent(ref)
    assert intent["status"] == "paid" and intent["txn"] == "14512345" and intent["paid_at"]

    # the customer's return page (signed by VNPay too)
    r = await shop.get("/pay/return/vnpay", params={**ok, "vnp_SecureHash": vnpay_sign(ok)})
    page = await r.text()
    assert r.status == 200 and "Đã thanh toán" in page and code in page and f"/order/{code}?t=" in page
    assert (await shop.get("/pay/return/vnpay", params={**ok, "vnp_SecureHash": "ab"})).status == 404
    # paid: no more online payment, and the page says so
    _r, page = await post(shop, f"/pay/{code}?t={token}", status=400, gateway="vnpay")
    assert "đã thanh toán đủ" in page
    assert "Thanh toán online" not in await (await shop.get(f"/order/{code}?t={token}")).text()
    await settle(office.hub)


async def test_vnpay_signed_return_records_a_failed_or_successful_payment(site):
    shop, office, mails, _g = site
    order, token, _page = await buy(shop, office, mails)
    r, _ = await post(shop, f"/pay/{order['code']}?t={token}", gateway="vnpay")
    ref = dict(parse_qsl(urlsplit(r.headers["Location"]).query))["vnp_TxnRef"]
    failed = {
        "vnp_Amount": "1000000000",
        "vnp_ResponseCode": "24",
        "vnp_TransactionStatus": "02",
        "vnp_TxnRef": ref,
        "vnp_TransactionNo": "0",
    }
    page = await (
        await shop.get("/pay/return/vnpay", params={**failed, "vnp_SecureHash": vnpay_sign(failed)})
    ).text()
    assert "không thành công" in page and f"/pay/{order['code']}?t=" in page  # try again
    assert office.inventory.order(order["id"])["paid"] == 0
    assert payments_of(office).intent(ref)["status"] == "failed"
    # a new attempt, successful: the signed return records it before the IPN arrives
    r, _ = await post(shop, f"/pay/{order['code']}?t={token}", gateway="vnpay")
    ref2 = dict(parse_qsl(urlsplit(r.headers["Location"]).query))["vnp_TxnRef"]
    assert ref2 != ref
    ok = {
        "vnp_Amount": "1000000000",
        "vnp_ResponseCode": "00",
        "vnp_TransactionStatus": "00",
        "vnp_TxnRef": ref2,
        "vnp_TransactionNo": "77",
    }
    page = await (await shop.get("/pay/return/vnpay", params={**ok, "vnp_SecureHash": vnpay_sign(ok)})).text()
    assert "Đã thanh toán" in page
    assert office.inventory.order(order["id"])["paid"] == 10_000_000
    r = await shop.get("/pay/ipn/vnpay", params={**ok, "vnp_SecureHash": vnpay_sign(ok)})
    assert (await r.json())["RspCode"] == "02" and len(office.inventory.order(order["id"])["payments"]) == 1
    await settle(office.hub)


async def test_momo_create_and_ipn(site):
    shop, office, mails, gateways = site
    inv = office.inventory
    order, token, _page = await buy(shop, office, mails)
    r, _ = await post(shop, f"/pay/{order['code']}?t={token}", gateway="momo")
    assert r.headers["Location"].startswith("https://test-payment.momo.vn/pay/")
    kind, body = gateways.created[-1]
    assert kind == "momo" and body["amount"] == 10_000_000 and body["partnerCode"] == "MOMOTEST"
    assert body["requestType"] == "payWithMethod" and body["autoCapture"] is True and body["lang"] == "vi"
    assert body["ipnUrl"] == f"{SITE}/pay/ipn/momo" and body["redirectUrl"] == f"{SITE}/pay/return/momo"
    ref = body["orderId"]
    ipn = {
        "partnerCode": "MOMOTEST",
        "orderId": ref,
        "requestId": ref,
        "amount": 10_000_000,
        "orderInfo": body["orderInfo"],
        "orderType": "momo_wallet",
        "transId": 2_147_483_648,
        "resultCode": 0,
        "message": "Thành công.",
        "payType": "qr",
        "responseTime": 1_759_100_000_000,
        "extraData": "",
    }
    bad = await shop.post("/pay/ipn/momo", json={**ipn, "signature": "ff" * 32})
    assert bad.status == 400 and inv.order(order["id"])["paid"] == 0
    wrong = {**ipn, "amount": 1_000}
    assert (await shop.post("/pay/ipn/momo", json={**wrong, "signature": momo_sign(wrong)})).status == 400
    r = await shop.post("/pay/ipn/momo", json={**ipn, "signature": momo_sign(ipn)})
    assert r.status == 204 and await r.read() == b""
    paid = inv.order(order["id"])
    assert paid["paid"] == 10_000_000 and paid["payments"][-1]["method"] == "wallet"
    assert "MoMo 2147483648" in paid["payments"][-1]["ref"]
    r = await shop.post("/pay/ipn/momo", json={**ipn, "signature": momo_sign(ipn)})
    assert r.status == 204 and len(inv.order(order["id"])["payments"]) == 1
    # the customer comes back: the same fields as query parameters, signed
    q = {k: str(v) for k, v in ipn.items()}
    page = await (await shop.get("/pay/return/momo", params={**q, "signature": momo_sign(ipn)})).text()
    assert "Đã thanh toán" in page and "MoMo" in page
    assert (await shop.get("/pay/return/momo", params={**q, "signature": "00"})).status == 404

    # MoMo refusing to create the order: the customer is told, no intent stays pending
    other, token2, _ = await buy(shop, office, mails, email="b@example.com")
    gateways.refuse = True
    _r, page = await post(shop, f"/pay/{other['code']}?t={token2}", status=400, gateway="momo")
    assert "MoMo từ chối" in page and "Duplicate orderId" in page
    assert payments_of(office).intents(other["id"])[0]["status"] == "failed"
    await settle(office.hub)


async def test_zalopay_create_callback_and_return(site):
    shop, office, mails, gateways = site
    inv = office.inventory
    pay = payments_of(office)
    order, token, _page = await buy(shop, office, mails)
    r, _ = await post(shop, f"/pay/{order['code']}?t={token}", gateway="zalopay")
    assert r.headers["Location"].startswith("https://sbgateway.zalopay.vn/openinapp?order=")
    kind, form = gateways.created[-1]
    assert kind == "zalopay" and form["app_id"] == "2553" and form["amount"] == "10000000"
    assert re.fullmatch(r"\d{6}_" + order["code"] + r"[0-9A-F]{8}", form["app_trans_id"])
    assert json.loads(form["embed_data"]) == {"redirecturl": f"{SITE}/pay/return/zalopay"}
    assert (
        form["item"] == "[]" and form["callback_url"] == f"{SITE}/pay/ipn/zalopay" and form["bank_code"] == ""
    )
    ref = form["app_trans_id"]

    # the return page before the callback: informational only
    q = {
        "appid": "2553",
        "apptransid": ref,
        "pmcid": "38",
        "bankcode": "",
        "amount": "10000000",
        "discountamount": "0",
        "status": "1",
    }
    checksum = sha256(
        KEY2,
        "|".join(
            q[k] for k in ("appid", "apptransid", "pmcid", "bankcode", "amount", "discountamount", "status")
        ),
    )
    assert (await shop.get("/pay/return/zalopay", params=q)).status == 404  # no checksum
    page = await (await shop.get("/pay/return/zalopay", params={**q, "checksum": checksum})).text()
    assert "Đang xác nhận" in page and "tải lại" in page and inv.order(order["id"])["paid"] == 0

    data = json.dumps(
        {
            "app_id": 2553,
            "app_trans_id": ref,
            "app_time": 1_759_100_000_000,
            "app_user": "guest",
            "amount": 10_000_000,
            "embed_data": form["embed_data"],
            "item": "[]",
            "zp_trans_id": 250929000000123,
            "server_time": 1_759_100_005_000,
            "channel": 38,
            "merchant_user_id": "",
            "user_fee_amount": 0,
            "discount_amount": 0,
        }
    )

    async def callback(body):
        r = await shop.post("/pay/ipn/zalopay", json=body)
        assert r.status == 200
        return await r.json()

    assert (await callback({"data": data, "mac": "bad", "type": 1}))["return_code"] == -1
    assert (await callback({"data": data, "mac": sha256(KEY1, data), "type": 1}))[
        "return_code"
    ] == -1  # key2 signs
    assert inv.order(order["id"])["paid"] == 0
    wrong = data.replace("10000000", "10")
    assert (await callback({"data": wrong, "mac": sha256(KEY2, wrong), "type": 1}))["return_code"] == 2
    assert await callback({"data": data, "mac": sha256(KEY2, data), "type": 1}) == {
        "return_code": 1,
        "return_message": "success",
    }
    paid = inv.order(order["id"])
    assert paid["paid"] == 10_000_000 and "ZaloPay 250929000000123" in paid["payments"][-1]["ref"]
    assert (await callback({"data": data, "mac": sha256(KEY2, data), "type": 1}))["return_code"] == 1
    assert len(inv.order(order["id"])["payments"]) == 1
    assert pay.intent(ref)["status"] == "paid"
    page = await (await shop.get("/pay/return/zalopay", params={**q, "checksum": checksum})).text()
    assert "Đã thanh toán" in page
    await settle(office.hub)


async def test_settings_pay_links_and_other_customers(site):
    shop, office, _mails, _g = site
    inv, crm = office.inventory, office.hub.crm
    pay = payments_of(office)
    shopfront = office.storefront

    # a logged-in customer pays their own order, never somebody else's
    hoa = crm.create_contact("Hoa", "0901234567", "hoa@example.com")
    tuan = crm.create_contact("Tuấn", "0912000111")
    mine = inv.create_order([{"sku": "SOFA-01", "qty": 1}], contact_id=hoa["id"])
    theirs = inv.create_order([{"sku": "SOFA-01", "qty": 1}], contact_id=tuan["id"])
    shop.session.cookie_jar.update_cookies({"sf_session": shopfront.start_session(hoa["id"])})
    await shop.get("/")
    assert (await shop.get(f"/pay/{mine['code']}")).status == 200
    assert (await shop.get(f"/pay/{theirs['code']}")).status == 404
    await post(shop, f"/pay/{theirs['code']}", status=404, gateway="vnpay")
    r, _ = await post(shop, f"/pay/{mine['code']}", gateway="vnpay")
    assert "vnpayment.vn" in r.headers["Location"]
    account = await (await shop.get("/account")).text()
    assert f'href="/pay/{mine["code"]}"' in account

    # the chat: the customer's order list carries the pay link (a signed link, no secret)
    sales = office.employees["sales"]
    assert "chưa có đơn" in await sales.menu.handle(11, "orders", "", "Hoa")  # makes the contact
    conv = office.hub.inbox.find("simplex:sales", "11")
    contact = office.hub.crm.contact_of(conv.id)
    chat_order = inv.create_order([{"sku": "SOFA-01", "qty": 1}], contact_id=contact["id"])
    listing = await sales.menu.handle(11, "orders", "", "Hoa")
    assert pay.pay_link(chat_order) in listing and "thanh toán online" in listing
    for secret in (VNPAY_SECRET, MOMO_SECRET, KEY1, KEY2):
        assert secret not in listing
    inv.add_payment(chat_order["id"], "cash")
    assert "thanh toán online" not in await sales.menu.handle(11, "orders", "", "Hoa")

    # the admin API: settings for admins, pay links for the counter
    admin = TestClient(TestServer(create_app(office, PASSWORD)))
    await admin.start_server()
    try:

        async def call(method, path, body=None, status=200):
            r = await admin.request(method, path, json=body, headers=H)
            data = await r.json() if r.content_type == "application/json" else await r.text()
            assert r.status == status, data
            return data

        await call("POST", "/api/login", {"password": PASSWORD})
        s = await call("GET", "/api/pos/payments/settings")
        assert s["vnpay"]["configured"] and s["zalopay"]["env_set"] == {"key1_env": True, "key2_env": True}
        assert s["gateways"] == ["vnpay", "momo", "zalopay"] and s["public_url"] == SITE
        for secret in (VNPAY_SECRET, MOMO_SECRET, KEY1, KEY2):
            assert secret not in json.dumps(s)
        await call(
            "PUT", "/api/pos/payments/settings", {"vnpay": {"enabled": True, "tmn_code": ""}}, status=400
        )
        await call(
            "PUT",
            "/api/pos/payments/settings",
            {"momo": {"enabled": False, "secret_env": "bad name"}},
            status=400,
        )
        s = await call("PUT", "/api/pos/payments/settings", {"momo": {"enabled": False}, "sandbox": False})
        assert not s["momo"]["configured"] and s["gateways"] == ["vnpay", "zalopay"] and not s["sandbox"]
        assert pay.settings()["momo"]["secret_env"] == "MOMO_SECRET"  # back to the default name
        _r, page = await post(shop, f"/pay/{mine['code']}", status=400, gateway="momo")
        assert "MoMo chưa được bật" in page
        r, _ = await post(shop, f"/pay/{mine['code']}", gateway="vnpay")
        assert r.headers["Location"].startswith("https://pay.vnpay.vn/vpcpay.html?")  # production now
        await call("PUT", "/api/pos/payments/settings", {"sandbox": True})

        link = await call("POST", f"/api/pos/orders/{mine['id']}/paylink", {})
        assert link["url"] == pay.pay_link(mine) and link["payable"] and link["bank"]
        assert link["url"].startswith(f"{SITE}/pay/{mine['code']}?t=")
        # the link opens for anybody who has it (no login)
        guest = TestClient(TestServer(create_shop_app(office)))
        await guest.start_server()
        try:
            page = await (await guest.get(link["url"].removeprefix(SITE))).text()
            assert mine["code"] in page and 'value="vnpay"' in page
        finally:
            await guest.close()
        done = await call("POST", f"/api/pos/orders/{chat_order['id']}/paylink", {})
        assert not done["payable"]

        # cashiers get pay links for their own sales, and never the settings
        await call(
            "POST",
            "/api/users",
            {"username": "thu", "name": "Thu", "role": "cashier", "password": "0123456789"},
        )
        await call("POST", "/api/logout", {})
        await call("POST", "/api/login", {"username": "thu", "password": "0123456789"})
        await call("GET", "/api/pos/payments/settings", status=403)
        await call("PUT", "/api/pos/payments/settings", {"sandbox": False}, status=403)
        sale = await call(
            "POST", "/api/pos/orders", {"items": [{"sku": "SOFA-01", "qty": 1}], "customer_name": "Lan"}
        )
        assert (await call("POST", f"/api/pos/orders/{sale['id']}/paylink", {}))["payable"]
        await call("POST", f"/api/pos/orders/{mine['id']}/paylink", {}, status=404)  # not their sale
    finally:
        await admin.close()
    await settle(office.hub)


async def test_no_gateway_no_button_and_no_website_no_link(tmp_path, monkeypatch):
    office = make_office(tmp_path, ScriptedLLM(), storefront={"public_url": SITE})
    inv = office.inventory
    kho = inv.save_warehouse(None, {"code": "Q1", "name": "Q1"})["id"]
    sofa = inv.save_product(None, {"sku": "SOFA-01", "name": "Sofa"})["id"]
    inv.add_opening_stock(sofa, kho, 2, 6_000_000, margin_pct=40)
    pay = payments_of(office)
    order = inv.create_order([{"sku": "SOFA-01", "qty": 1}])
    assert pay.public()["gateways"] == [] and not pay.available() and not pay.payable(inv.order(order["id"]))
    # enabled without its secret in the environment: not offered
    pay.save({"vnpay": {"enabled": True, "tmn_code": "T", "secret_env": "T_MISSING_SECRET"}})
    assert pay.public()["vnpay"]["enabled"] and not pay.public()["vnpay"]["configured"]
    assert pay.enabled_gateways() == []
    with pytest.raises(Exception, match="chưa được bật"):
        await pay.create_intent(order["id"], "vnpay")
    shop = TestClient(TestServer(create_shop_app(office)))
    await shop.start_server()
    try:
        token = office.storefront.sign("order", order["code"])
        page = await (await shop.get(f"/order/{order['code']}?t={token}")).text()
        assert "Thanh toán online" not in page
        page = await (await shop.get(f"/pay/{order['code']}?t={token}")).text()
        assert "chưa nhận thanh toán online" in page
        # bank transfer details alone make the page worth a button
        pay.save({"bank": {"enabled": True, "info": "ACB 123456"}})
        page = await (await shop.get(f"/order/{order['code']}?t={token}")).text()
        assert "Thanh toán online" in page
        page = await (await shop.get(f"/pay/{order['code']}?t={token}")).text()
        assert "ACB 123456" in page and 'name="gateway"' not in page  # no gateway buttons
        assert (await shop.get("/pay/ipn/paypal")).status == 404
        assert (await shop.get("/pay/return/paypal")).status == 404
    finally:
        await shop.close()
    # no website: no links, and the counter's pay link says why
    office.storefront = None
    with pytest.raises(Exception, match="storefront"):
        pay.pay_link(order)
