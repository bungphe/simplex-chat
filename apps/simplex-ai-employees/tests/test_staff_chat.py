"""Staff at work in the SimpleX apps: linking a chat to a staff account, the menu of each
role, and the inbox, counter, warehouse and delivery commands."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_channels import PASSWORD, WEBHOOK, H, Platforms, settle

from ai_employees.staff_chat import staff_menu
from ai_employees.users import Users
from ai_employees.web import create_app

from fakes import ScriptedLLM, fake_chat, make_office, text


@pytest.fixture
async def office(tmp_path, monkeypatch):
    monkeypatch.setenv("T_HOOK_SECRET", "hook-secret-1")
    llm, platforms = ScriptedLLM(), Platforms()
    office = make_office(tmp_path, llm, http=platforms.client, channels=[WEBHOOK])
    chat = fake_chat(office.employees["sales"])
    users = Users(office.docs, "")
    for username, role in [
        ("lan", "agent"),
        ("thu", "cashier"),
        ("kho", "warehouse"),
        ("hung", "delivery"),
        ("quan", "manager"),
    ]:
        users.add(username, username.title(), role, "0123456789")
    inv = office.inventory
    kho = inv.save_warehouse(None, {"code": "KHO", "name": "Kho tổng"})["id"]
    ncc = inv.save_supplier(None, {"name": "Foshan", "lead_time_days": 45})["id"]
    sofa = inv.save_product(None, {"sku": "SOFA-01", "name": "Sofa da", "cbm": "1"})["id"]
    inv.add_opening_stock(sofa, kho, 5, 6_000_000, margin_pct=40)
    po = inv.save_po(
        None,
        {
            "supplier_id": ncc,
            "warehouse_id": kho,
            "exchange_rate": "25000",
            "eta": "2026-11-20",
            "items": [{"product_id": sofa, "qty": 3, "unit_cost_foreign": "240", "margin_pct": 40}],
        },
        actor="t",
    )
    inv.set_po_status(po["id"], "ordered")
    office.po_number = po["po_number"]
    yield office, llm, platforms, chat, users


async def link(office, cid: int, username: str) -> str:
    code = office.staff_links.new_code(username)
    return await office.employees["sales"].staff.handle(cid, "link", code.lower())


async def test_linking_a_chat_gives_the_role_its_menu(office):
    office, _llm, _p, chat, users = office
    staff = office.employees["sales"].staff
    assert "Lệnh dành cho nhân viên" in await staff.handle(30, "inbox", "")
    assert "không đúng" in await staff.handle(30, "link", "WRONG123")
    code = office.staff_links.new_code("lan")
    assert "Lan (agent)" in await staff.handle(30, "link", code)
    assert "không đúng" in await staff.handle(31, "link", code)  # once
    menu = chat.prefs[30]["commands"]
    labels = [m["label"] for m in menu]
    assert "💬 Hộp thư" in labels and "🧾 Bán hàng" in labels and "🏬 Kho" not in labels
    reply = next(m for m in menu if m["label"] == "💬 Hộp thư")["commands"][2]
    assert reply["keyword"] == "reply" and reply["params"]
    assert "Vai trò của bạn không dùng được" in await staff.handle(30, "receive", office.po_number)

    # menus by role
    roles = {
        u: [m["label"] for m in staff_menu(users.get(u)) if m["type"] == "menu"]
        for u in ("thu", "kho", "hung", "quan")
    }
    tasks = "📋 Công việc"  # everybody's
    assert (
        roles["thu"] == ["🧾 Bán hàng", tasks]
        and roles["kho"] == ["🏬 Kho", tasks]
        and roles["hung"] == ["🚚 Giao hàng", tasks]
    )
    assert roles["quan"] == ["💬 Hộp thư", "🧾 Bán hàng", "🏬 Kho", "🚚 Giao hàng", "📊 Quản lý", tasks]

    # a disabled account loses its chats; unlinking restores the customer menu
    users.update("lan", disabled=True)
    assert "Lệnh dành cho nhân viên" in await staff.handle(30, "me", "")
    users.update("lan", disabled=False)
    assert "Lan" in await staff.handle(30, "me", "")
    await staff.handle(30, "unlink", "")
    assert chat.prefs[30]["commands"][0]["keyword"] == "products"
    assert "Lệnh dành cho nhân viên" in await staff.handle(30, "me", "")


async def test_the_inbox_from_the_phone(office):
    office, llm, platforms, chat, _users = office
    hub, staff = office.hub, office.employees["sales"].staff
    await link(office, 30, "lan")
    llm.responses += [text("Dạ em chào chị.")]
    hub.push_inbound(
        "website",
        {"conversation_id": "v1", "customer_name": "Chị Mai", "text": "Cho chị hỏi sofa", "message_id": "1"},
    )
    await settle(hub)
    conv = hub.inbox.find("website", "v1")
    listing = await staff.handle(30, "inbox", "")
    assert f"#{conv.id}" in listing and "Chị Mai" in listing and f"/'open {conv.id}'" in listing
    thread = await staff.handle(30, "open", str(conv.id))
    assert "Cho chị hỏi sofa" in thread and "Dạ em chào chị." in thread
    assert "Không có hội thoại" in await staff.handle(30, "open", "999")
    assert "Đã gửi cho Chị Mai" in await staff.handle(30, "reply", f"{conv.id} Dạ sofa còn hàng ạ")
    assert platforms.sent[-1][1]["text"] == "Dạ sofa còn hàng ạ"
    conv = hub.inbox.conversation(conv.id)
    assert conv.mode == "human"
    assert hub.inbox.messages(conv.id)[-1]["author"] == "Lan"
    await staff.handle(30, "aion", str(conv.id))
    assert hub.inbox.conversation(conv.id).mode == "ai"
    await staff.handle(30, "close", str(conv.id))
    assert hub.inbox.conversation(conv.id).status == "closed"

    # customers asking for a person reach the linked staff with the inbox in their role
    await link(office, 40, "kho")
    await office.employees["sales"].menu.handle(77, "staff", "muốn gặp người", "Hoa")
    told = [cid for cid, t in chat.sent if "muốn gặp nhân viên" in t]
    assert 30 in told and 40 not in told


async def test_counter_warehouse_and_deliveries_from_the_phone(office):
    office, _llm, _p, _chat, _users = office
    staff = office.employees["sales"].staff
    await link(office, 31, "thu")
    sold = await staff.handle(31, "sell", "sofa-01 2; 0901234567 Chị Lan")
    code = sold.split("*")[1]
    assert "Sofa da × 2" in sold and "20.000.000 đ" in sold
    order = office.inventory.order(int(code[2:]))
    assert order["salesperson"] == "thu" and order["contact_id"] and order["phone"] == "0901234567"
    assert "Cú pháp" in await staff.handle(31, "pay", f"{code} bitcoin")
    paid = await staff.handle(31, "pay", f"{code} cash 5.000.000")
    assert "đã trả 5.000.000 đ" in paid
    assert "còn 0 đ" in await staff.handle(31, "pay", f"{code} transfer")
    assert "Đã xuất kho" in await staff.handle(31, "done", code)
    assert code in await staff.handle(31, "sales", "")
    other = office.inventory.create_order([{"sku": "SOFA-01", "qty": 1}], salesperson="lan")
    assert "Không có đơn" in await staff.handle(31, "order", other["code"])  # someone else's sale

    await link(office, 32, "kho")
    assert office.po_number in await staff.handle(32, "incoming", "")
    assert "sắp về 3" in await staff.handle(32, "stock", "sofa")
    assert "Đã nhận 3 sản phẩm" in await staff.handle(32, "receive", office.po_number.lower())
    assert "đã nhận đủ" in await staff.handle(
        32, "receive", office.po_number
    ) or "chưa đặt" in await staff.handle(32, "receive", office.po_number)

    # a driver's day
    dl, inv = office.delivery, office.inventory
    await link(office, 33, "hung")
    car = dl.save_carrier(None, {"code": "NB", "name": "Xe nhà", "internal": True})
    from datetime import datetime

    today = datetime.now().astimezone().date().isoformat()
    bids = []
    for name in ("Chị Mai", "Anh Tuấn"):
        o = inv.create_order([{"sku": "SOFA-01", "qty": 1}], customer_name=name, phone="0901", address="Q1")
        bids.append(dl.book({"order_id": o["id"], "delivery_date": today, "address": "12 Lê Lợi"})["id"])
    kho = inv.default_warehouse()["id"]
    route = dl.create_route(
        {"delivery_date": today, "carrier_id": car["id"], "origin_wh": kho, "booking_ids": bids}
    )
    trips = await staff.handle(33, "trips", "")
    assert route["code"] in trips and f"/'go {route['code']}'" in trips
    assert "đã chạy" in await staff.handle(33, "go", route["code"].lower())
    assert f"/'delivered {bids[0]}'" in await staff.handle(33, "trips", "")
    assert "Còn 1 điểm" in await staff.handle(33, "delivered", str(bids[0]))
    assert "Cú pháp" in await staff.handle(33, "failed", str(bids[1]))
    assert "mang hàng về" in await staff.handle(33, "failed", f"{bids[1]} khách vắng nhà")
    assert dl.booking(bids[1])["status"] == "comeback"
    assert "Vai trò" in await staff.handle(33, "sell", "SOFA-01 1")

    await link(office, 34, "quan")
    assert "Hôm nay" in await staff.handle(34, "report", "")
    assert "chờ duyệt" in (await staff.handle(34, "approvals", "")).lower()


async def test_linking_from_the_admin_web_ui(office):
    office, _llm, _p, _chat, _users = office
    client = TestClient(TestServer(create_app(office, PASSWORD)))
    await client.start_server()
    try:
        await client.post("/api/login", json={"username": "lan", "password": "0123456789"}, headers=H)
        r = await client.post("/api/me/simplex", json={}, headers=H)
        code = (await r.json())["code"]
        assert len(code) == 8
        await office.employees["sales"].staff.handle(30, "link", code)
        data = await (await client.get("/api/me/simplex", headers=H)).json()
        [linked] = data["links"]
        assert linked["employee"] == "sales" and linked["contact_id"] == 30
        assert {e["id"] for e in data["employees"]} == {"sales", "accountant"}
        r = await client.delete(f"/api/me/simplex/{linked['key']}", headers=H)
        assert r.status == 200
        assert (await (await client.get("/api/me/simplex", headers=H)).json())["links"] == []
        assert (await client.delete(f"/api/me/simplex/{linked['key']}", headers=H)).status == 404
    finally:
        await client.close()


async def test_the_customer_menu_and_replies_in_the_customer_language(office, monkeypatch):
    import re

    from ai_employees import i18n

    # a language without its own catalog: the replies are translated by the model
    monkeypatch.setattr(i18n, "catalog", lambda code: {})
    office, llm, _p, chat, _users = office
    sales = office.employees["sales"]
    sales.state.set_language(50, "en")

    def translate(params):
        content = params["messages"][-1]["content"]
        content = content if isinstance(content, str) else " ".join(c.get("text", "") for c in content)
        return text("Your orders: " + " ".join(re.findall(r"⟦P\d+⟧", content)))

    _conv, contact = sales.menu._contact(50, "John")
    order = office.inventory.create_order([{"sku": "SOFA-01", "qty": 1}], contact_id=contact["id"])
    llm.responses.append(translate)
    reply = await sales.menu.handle(50, "orders", "", "John")
    # prices, order numbers and the tappable command survive the translation unchanged
    assert (
        reply.startswith("Your orders:") and order["code"] in reply and f"/'invoice {order['code']}'" in reply
    )
    assert "10,000,000 VND" in reply

    # the menu follows the customer's language, set once per language
    await sales.staff.localize_menu(50)
    labels = [c["label"] for c in chat.prefs[50]["commands"]]
    assert labels[0] == "🛋 Products & prices" and labels[-1] == "Clear the assistant's memory"
    chat.prefs.clear()
    await sales.staff.localize_menu(50)
    assert 50 not in chat.prefs
    await sales.staff.localize_menu(51)  # Vietnamese (or unknown): the profile's own menu
    assert 51 not in chat.prefs
    sales.state.set_language(50, "ja")
    await sales.staff.localize_menu(50)
    assert chat.prefs[50]["commands"][1]["label"] == "🎁 お得なセット"


async def test_removed_or_reset_accounts_lose_their_chats(office):
    office, _llm, _p, _chat, _users = office
    links = office.staff_links
    await link(office, 40, "lan")
    await link(office, 41, "quan")
    assert links.user("sales", 40).username == "lan"
    client = TestClient(TestServer(create_app(office, PASSWORD)))
    await client.start_server()
    try:
        await client.post("/api/login", json={"password": PASSWORD}, headers=H)
        # an admin resets a password (a lost phone?): the chat is unlinked
        r = await client.patch("/api/users/quan", json={"password": "9876543210"}, headers=H)
        assert r.status == 200 and links.user("sales", 41) is None
        # a removed account's chat does not come back with a new account of that name
        assert (await client.delete("/api/users/lan", headers=H)).status == 200
        r = await client.post(
            "/api/users",
            json={"username": "lan", "name": "Lan 2", "role": "manager", "password": "0123456789"},
            headers=H,
        )
        assert r.status == 200 and links.user("sales", 40) is None
    finally:
        await client.close()
