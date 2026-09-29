"""Privacy: the AI disclosure, the policy page, export and erasure of a customer's data,
message retention, and the admin-only API."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_channels import PASSWORD, WEBHOOK, H, Platforms, settle

from ai_employees import privacy
from ai_employees.storefront import create_shop_app
from ai_employees.web import create_app

from fakes import ScriptedLLM, make_office, text

SITE = "http://shop.test"
NOTICE = (
    "Bạn đang trò chuyện với trợ lý AI của Nội thất ABC. Nhắn 'nhân viên' hoặc /staff để gặp người thật. "
    f"Chính sách bảo mật: {SITE}/privacy"
)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("T_HOOK_SECRET", "hook-secret-1")
    llm, platforms = ScriptedLLM(), Platforms()
    # privacy={}: the real defaults (the shared fixture turns the disclosure off for other tests)
    office = make_office(
        tmp_path, llm, http=platforms.client, channels=[WEBHOOK], storefront={"public_url": SITE}, privacy={}
    )
    office.inventory.save_settings({"shop_name": "Nội thất ABC", "shop_address": "12 Lê Lợi, Q1"})
    return office, llm, platforms


@pytest.fixture
async def ui(setup):
    office, llm, platforms = setup
    app = create_app(office, PASSWORD)
    privacy.add_routes(app.router)
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client, office, llm, platforms
    await client.close()


def visitor(hub, conv: str, text_: str, mid: str, name: str = "Linh"):
    return hub.push_inbound(
        "website", {"conversation_id": conv, "customer_name": name, "text": text_, "message_id": mid}
    )


async def test_ai_disclosure_is_sent_once_per_conversation(setup):
    office, llm, platforms = setup
    hub = office.hub
    assert privacy.settings(office)["ai_disclosure"] is True
    llm.responses += [text("Dạ có ạ."), text("Giá 5 triệu ạ.")]
    visitor(hub, "v1", "Shop có sofa không?", "m1")
    await settle(hub)
    visitor(hub, "v1", "Giá bao nhiêu vậy?", "m2")
    await settle(hub)
    assert [m["text"] for _, m in platforms.sent] == [f"{NOTICE}\n\nDạ có ạ.", "Giá 5 triệu ạ."]
    conv = hub.inbox.find("website", "v1")
    # staff see the notice in the inbox; the AI's own memory only holds its answer
    assert hub.inbox.messages(conv.id)[1]["text"].startswith(NOTICE)
    history = [t["content"] for t in office.employees["sales"].state.history(conv.contact_id)]
    assert history[:2] == ["Shop có sofa không?", "Dạ có ạ."] and not any(NOTICE in t for t in history)

    # every conversation gets its own notice, once
    llm.responses.append(text("Chào chị."))
    visitor(hub, "v2", "Chào shop", "m3", name="Hoa")
    await settle(hub)
    assert platforms.sent[-1][1]["text"] == f"{NOTICE}\n\nChào chị."

    # turned off: nothing is added
    privacy.save_settings(office, {"ai_disclosure": False})
    llm.responses.append(text("Dạ."))
    visitor(hub, "v3", "Còn hàng không?", "m4", name="Mai")
    await settle(hub)
    assert platforms.sent[-1][1]["text"] == "Dạ."
    assert hub.inbox.messages(hub.inbox.find("website", "v3").id)[-1]["text"] == "Dạ."

    # replies sent outside the hub (the SimpleX bots) prefix the answer the same way
    privacy.save_settings(office, {"ai_disclosure": True, "disclosure_text": "AI của {shop} đây. {url}"})
    sx = hub.inbox.upsert("simplex:sales", "77", "Tuấn", "sales")
    assert (
        await hub.with_disclosure(sx.id, "Xin chào") == f"AI của Nội thất ABC đây. {SITE}/privacy\n\nXin chào"
    )
    assert await hub.with_disclosure(sx.id, "Lại nữa") == "Lại nữa"
    assert await hub.with_disclosure(999_999, "Không có hội thoại") == "Không có hội thoại"


def test_placeholders_and_missing_details():
    values = {"shop": "ABC", "address": "", "phone": "", "email": "", "url": "", "retention": "90 ngày"}
    assert privacy.fill(privacy.DEFAULT_DISCLOSURE, values) == (
        "Bạn đang trò chuyện với trợ lý AI của ABC. Nhắn 'nhân viên' hoặc /staff để gặp người thật."
    )
    assert (
        privacy.fill("Điện thoại: {phone} · Email: {email}", {**values, "email": "a@b.vn"}) == "Email: a@b.vn"
    )
    assert privacy.fill("{shop} – {address}\nLưu {retention}.", values) == "ABC\nLưu 90 ngày."
    assert "{" not in privacy.fill(privacy.DEFAULT_POLICY, values)


async def test_policy_page_renders_the_text_with_the_shop_details(setup):
    office, *_ = setup
    office.inventory.save_settings({"shop_phone": "0901 234 567"})
    privacy.save_settings(office, {"contact_email": "privacy@abc.vn", "retention_days": 90})
    app = create_shop_app(office)
    privacy.shop_routes(app.router)
    shop = TestClient(TestServer(app))
    await shop.start_server()
    try:
        r = await shop.get("/privacy")
        page = await r.text()
        assert r.status == 200 and "<title>Chính sách bảo mật – Nội thất ABC</title>" in page
        assert "<h1>Chính sách bảo mật dữ liệu cá nhân của Nội thất ABC</h1>" in page
        assert "<li>Họ tên, số điện thoại, email, địa chỉ giao hàng.</li>" in page
        assert "Nội thất ABC – 12 Lê Lợi, Q1<br>Điện thoại: 0901 234 567 · Email: privacy@abc.vn" in page
        assert "Nội dung chat được lưu 90 ngày." in page and f"{SITE}/privacy." in page

        privacy.save_settings(
            office, {"policy_text": "Xin chào {shop} <b>\n\n- Một <script>alert(1)</script>\n- Hai"}
        )
        page = await (await shop.get("/privacy")).text()
        assert (
            "<p>Xin chào Nội thất ABC &lt;b&gt;</p><ul><li>Một &lt;script&gt;" in page
            and "<script>alert" not in page
        )
    finally:
        await shop.close()


async def test_export_then_erase_a_customer(ui):
    client, office, llm, platforms = ui
    hub, inv = office.hub, office.inventory
    llm.responses.append(text("Dạ em gửi báo giá ạ."))
    conv = visitor(hub, "v9", "Tôi là Linh, sđt 0901234567, muốn mua sofa", "m9")
    await settle(hub)
    contact = hub.crm.contact_of(conv.id)
    cid = int(contact["id"])
    assert contact["phone"] == "0901234567" and hub.crm.search("0901234567")
    kho = inv.save_warehouse(None, {"code": "KHO", "name": "Kho tổng"})["id"]
    sofa = inv.save_product(
        None, {"sku": "SOFA-01", "name": "Sofa da", "category": "Phòng khách", "cbm": "1"}
    )["id"]
    inv.add_opening_stock(sofa, kho, 5, 6_000_000, margin_pct=40)
    order = inv.create_order(
        [{"product_id": sofa, "qty": 1}],
        warehouse_id=kho,
        contact_id=cid,
        conversation_id=conv.id,
        customer_name="Linh",
        phone="0901234567",
        address="12 Lê Lợi",
        email="linh@example.com",
    )
    office.loyalty.adjust(cid, 10, "quà tặng")
    hub.inbox.db.execute(
        "INSERT INTO sf_sessions (token_hash, contact_id, created, expires) VALUES ('h1', ?, '2026-01-01', '2999-01-01')",
        (cid,),
    )
    state = office.employees["sales"].state
    state.set_note(conv.contact_id, "sở thích", "sofa da màu nâu")
    hub.inbox.add(conv.id, "note", "Khách khó tính", "Thu")

    await client.post("/api/login", json={"password": PASSWORD}, headers=H)
    assert (await client.get("/api/privacy/contacts/424242/export")).status == 404
    r = await client.get(f"/api/privacy/contacts/{cid}/export")
    assert (
        r.status == 200
        and r.headers["Content-Disposition"] == f'attachment; filename="khach-hang-{cid}.json"'
    )
    data = await r.json()
    assert data["contact"]["phone"] == "0901234567" and data["shop"] == "Nội thất ABC"
    [c] = data["conversations"]
    assert [m["sender"] for m in c["messages"]] == ["customer", "ai"]
    assert c["messages"][0]["text"] == "Tôi là Linh, sđt 0901234567, muốn mua sofa"
    assert [n["text"] for n in c["internal_notes"]] == ["Khách khó tính"]
    [o] = data["orders"]
    assert (
        o["code"] == order["code"]
        and o["items"][0]["sku"] == "SOFA-01"
        and "profit" not in o
        and "cost" not in o
    )
    assert [p["delta"] for p in data["loyalty_points"]] == [10] and data["storefront"] == {
        "sessions": 1,
        "login_codes": 0,
    }
    [mem] = data["ai_memory"]
    assert (
        mem["employee"] == "sales"
        and mem["notes"] == {"sở thích": "sofa da màu nâu"}
        and len(mem["turns"]) == 2
    )

    # erasure must be confirmed with the customer's name or phone number
    erase = f"/api/privacy/contacts/{cid}/erase"
    assert (await client.post(erase, json={"confirm": "Lan"}, headers=H)).status == 400
    assert (await client.post(erase, json={}, headers=H)).status == 400
    r = await client.post(erase, json={"confirm": "090 123 4567"}, headers=H)
    assert r.status == 200
    summary = (await r.json())["erased"]
    assert summary | {"log_id": 0} == {
        "contact_id": cid,
        "subject": "L… …567",
        "conversations": 1,
        "messages": 3,
        "orders": 1,
        "bookings": 0,
        "points_rows": 1,
        "points": 10,
        "sessions": 1,
        "login_codes": 0,
        "log_id": 0,
        "memory": {"sales": 2},
    }
    assert (
        hub.crm.contact(cid) is None and hub.crm.search("0901234567") == [] and hub.crm.search("Linh") == []
    )
    assert hub.inbox.conversation(conv.id) is None
    assert (
        hub.inbox.db.row("SELECT COUNT(*) AS n FROM messages WHERE conversation_id=?", (conv.id,))["n"] == 0
    )
    kept = inv.order(order["id"])
    assert kept["total"] == order["total"] and kept["items"][0]["sku"] == "SOFA-01"
    assert (kept["customer_name"], kept["phone"], kept["address"], kept["email"]) == (privacy.ERASED,) * 4
    assert kept["contact_id"] is None and kept["conversation_id"] is None
    assert hub.inbox.db.row("SELECT COUNT(*) AS n FROM sf_sessions WHERE contact_id=?", (cid,))["n"] == 0
    assert hub.inbox.db.row("SELECT COUNT(*) AS n FROM crm_points WHERE contact_id=?", (cid,))["n"] == 0
    assert state.history(conv.contact_id) == [] and state.notes(conv.contact_id) == {}
    assert state.contact_name(conv.contact_id) == f"#{conv.contact_id}"
    assert (await client.post(erase, json={"confirm": "Linh"}, headers=H)).status == 404

    log = (await (await client.get("/api/privacy/log")).json())["log"]
    assert [(e["action"], e["actor"], e["contact_id"]) for e in log] == [
        ("erase", "admin", cid),
        ("export", "admin", cid),
    ]
    assert log[0]["counts"]["messages"] == 3 and log[1]["counts"] == {
        "conversations": 1,
        "messages": 2,
        "orders": 1,
    }
    assert "Linh" not in str(log) and "0901234567" not in str(log)  # the audit trail keeps no details

    # the customer writing again starts from a clean slate
    llm.responses.append(text("Dạ chào anh."))
    again = visitor(hub, "v9", "Xin chào lại", "m10")
    await settle(hub)
    assert hub.crm.contact_of(again.id)["phone"] == "" and len(hub.inbox.messages(again.id)) == 2
    assert platforms.sent[-1][1]["text"].startswith(NOTICE)  # a new conversation: told again


def test_retention_deletes_only_old_messages_of_closed_conversations(setup):
    office, *_ = setup
    inbox = office.hub.inbox
    old = (datetime.now().astimezone() - timedelta(days=100)).isoformat(timespec="seconds")
    closed = inbox.upsert("website", "c1", "A", "sales")
    inbox.add(
        closed.id, "customer", "hỏi cũ", "A", "x1", old, [{"kind": "image", "url": "https://cdn/x.jpg"}]
    )
    inbox.add(closed.id, "ai", "trả lời cũ", "Lan", None, old)
    inbox.add(closed.id, "customer", "hỏi mới", "A", "x2")
    inbox.set_status(closed.id, "closed")
    gone = inbox.upsert("website", "c3", "C", "sales")
    inbox.add(gone.id, "customer", "chỉ có tin cũ", "C", "z1", old)
    inbox.set_status(gone.id, "closed")
    opened = inbox.upsert("website", "c2", "B", "sales")
    inbox.add(opened.id, "customer", "cũ nhưng đang mở", "B", "y1", old)

    assert privacy.apply_retention(office) == {"retention_days": 0, "messages": 0}
    privacy.save_settings(office, {"retention_days": 30})
    with pytest.raises(ValueError):
        privacy.save_settings(office, {"retention_days": "nhiều"})
    r = privacy.apply_retention(office)
    assert r["retention_days"] == 30 and r["messages"] == 3
    # from the scheduler (every tick) it runs at most every few hours; tests force it
    assert privacy.apply_retention(office) == {"retention_days": 30, "messages": 0, "skipped": True}
    inbox.add(gone.id, "customer", "cũ nữa", "C", "z2", old)
    inbox.set_status(gone.id, "closed")
    assert privacy.apply_retention(office, force=True)["messages"] == 1
    assert [m["text"] for m in inbox.messages(closed.id)] == ["hỏi mới"]
    assert inbox.messages(gone.id) == [] and inbox.conversation(gone.id).last_preview == ""
    assert [m["text"] for m in inbox.messages(opened.id)] == ["cũ nhưng đang mở"]
    assert privacy.apply_retention(office, force=True)["messages"] == 0
    assert [e["counts"]["messages"] for e in privacy.log_entries(office) if e["action"] == "retention"] == [
        1,
        3,
    ]


async def test_settings_api_and_admin_only_access(ui):
    client, *_ = ui
    assert (await client.get("/api/privacy/settings")).status == 401
    await client.post("/api/login", json={"password": PASSWORD}, headers=H)
    d = await (await client.get("/api/privacy/settings")).json()
    assert d["settings"]["ai_disclosure"] is True and d["settings"]["retention_days"] == 0
    assert d["policy_url"] == f"{SITE}/privacy" and d["preview"] == NOTICE
    assert d["defaults"]["policy_text"] == privacy.DEFAULT_POLICY
    assert (
        await client.put("/api/privacy/settings", json={"retention_days": "abc"}, headers=H)
    ).status == 400
    assert (await client.put("/api/privacy/settings", json={"retention_days": 9999}, headers=H)).status == 400
    r = await client.put(
        "/api/privacy/settings",
        json={
            "retention_days": "45",
            "contact_email": " dpo@abc.vn ",
            "disclosure_text": "Xin chào từ {shop}",
        },
        headers=H,
    )
    d = await r.json()
    assert (
        r.status == 200
        and d["settings"]["retention_days"] == 45
        and d["settings"]["contact_email"] == "dpo@abc.vn"
    )
    assert d["preview"] == "Xin chào từ Nội thất ABC"

    # a store manager may not read or change any of it
    r = await client.post(
        "/api/users",
        json={"username": "quan", "name": "Quân", "role": "manager", "password": "0123456789"},
        headers=H,
    )
    assert r.status == 200
    await client.post("/api/logout", headers=H)
    assert (
        await client.post("/api/login", json={"username": "quan", "password": "0123456789"}, headers=H)
    ).status == 200
    for method, path in (
        ("GET", "/api/privacy/settings"),
        ("PUT", "/api/privacy/settings"),
        ("GET", "/api/privacy/contacts/1/export"),
        ("POST", "/api/privacy/contacts/1/erase"),
        ("GET", "/api/privacy/log"),
    ):
        r = await client.request(method, path, json={} if method != "GET" else None, headers=H)
        assert r.status == 403, (method, path)
