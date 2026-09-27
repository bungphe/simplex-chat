"""The SimpleX chat commands under stress: bad input, secrets, slow models, repeats,
accounts that change, and the texts the translators get."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from ai_employees.employee import parse_command
from ai_employees.staff_chat import MENU_LANG_KEY, StaffLinks, sale_customer, small_int
from ai_employees.users import Users

from fakes import ScriptedLLM, fake_chat, make_office, text

HUGE = "9" * 30


@pytest.fixture
async def shop(tmp_path):
    llm = ScriptedLLM()
    office = make_office(tmp_path, llm)
    chat = fake_chat(office.employees["sales"])
    users = Users(office.docs, "")
    for username, role, channels in [
        ("lan", "agent", None),
        ("thu", "cashier", None),
        ("web", "agent", ["website"]),  # sees the website chats only
        ("quan", "manager", None),
    ]:
        users.add(username, username.title(), role, "0123456789", channels)
    inv = office.inventory
    kho = inv.save_warehouse(None, {"code": "KHO", "name": "Kho tổng"})["id"]
    sofa = inv.save_product(None, {"sku": "SOFA-01", "name": "Sofa da", "cbm": "1"})["id"]
    inv.add_opening_stock(sofa, kho, 50, 6_000_000, margin_pct=40)
    yield office, llm, chat, users


async def link(office, cid: int, username: str) -> str:
    code = office.staff_links.new_code(username)
    return await office.employees["sales"].staff.handle(cid, "link", code)


def message(cid: int, t: str, replies: list[tuple[int, str]]) -> NS:
    async def reply(answer: str) -> None:
        replies.append((cid, answer))

    contact = {"contactId": cid, "profile": {"displayName": f"K{cid}"}, "localDisplayName": f"k{cid}"}
    return NS(chat_info={"contact": contact}, text=t, reply=reply, content={"type": "link", "text": t})


async def drain(employee) -> None:
    while employee._tasks:
        await asyncio.gather(*list(employee._tasks))


# 1. /sell never attaches a sale to an unrelated contact


def test_sale_customer_reads_name_and_phone_in_either_order():
    assert sale_customer(" 0901234567 Chị Lan") == ("Chị Lan", "0901234567")
    assert sale_customer("Chị Lan 0901 234 567") == ("Chị Lan", "0901 234 567")
    assert sale_customer("Chị Lan; 0901.234.567") == ("Chị Lan", "0901.234.567")
    assert sale_customer("+1 202 555 0123; John") == ("John", "+1 202 555 0123")
    assert sale_customer("Chị Lan") == ("Chị Lan", "")
    assert sale_customer("12345 Anh Ba") == ("12345 Anh Ba", "")
    assert sale_customer("") == ("", "")


async def test_a_walk_in_sale_without_a_valid_phone_has_no_contact(shop):
    office, _llm, _chat, _users = shop
    staff, crm, inv = office.employees["sales"].staff, office.hub.crm, office.inventory
    phoneless = crm.create_contact("Khách cũ không có số")
    await link(office, 31, "thu")
    for who in ("Chị Lan", "12345 Anh Ba", "Chị; Hoa"):
        sold = await staff.handle(31, "sell", f"SOFA-01 1; {who}")
        order = inv.order(int(sold.split("*")[1][2:]))
        assert order["contact_id"] is None, who
    assert inv.orders(contact_id=int(phoneless["id"])) == []

    first = await staff.handle(31, "sell", "SOFA-01 1; Chị Mai; 0901 234 567")
    again = await staff.handle(31, "sell", "SOFA-01 1; Chị Mai 0901234567")
    a, b = (inv.order(int(r.split("*")[1][2:])) for r in (first, again))
    assert a["contact_id"] and a["contact_id"] == b["contact_id"] != phoneless["id"]
    assert crm.contact(a["contact_id"])["name"] == "Chị Mai" and a["customer_name"] == "Chị Mai"


# 2. and 8. /pay: amounts in the shop's currency, each payment recorded once


async def test_pay_amounts_with_decimals(tmp_path):
    office = make_office(tmp_path, ScriptedLLM())
    fake_chat(office.employees["sales"])
    inv = office.inventory
    inv.save_settings({"currency": "USD", "decimals": 2, "round_to": 0})
    kho = inv.save_warehouse(None, {"code": "KHO", "name": "Main"})["id"]
    chair = inv.save_product(None, {"sku": "CH-1", "name": "Chair"})["id"]
    inv.add_opening_stock(chair, kho, 5, 100, margin_pct=40)
    Users(office.docs, "").add("thu", "Thu", "cashier", "0123456789")
    await link(office, 31, "thu")
    staff = office.employees["sales"].staff
    code = (await staff.handle(31, "sell", "CH-1 1")).split("*")[1]
    await staff.handle(31, "pay", f"{code} cash 12.50")
    assert inv.order(int(code[2:]))["paid"] == 12.5


async def test_pay_is_recorded_once_per_order_state(shop):
    office, _llm, _chat, _users = shop
    staff, inv = office.employees["sales"].staff, office.inventory
    await link(office, 31, "thu")
    code = (await staff.handle(31, "sell", "SOFA-01 1")).split("*")[1]
    oid = int(code[2:])
    await staff.handle(31, "pay", f"{code} cash 1.000.000")
    # a second payment of the same amount, after the first: recorded
    await staff.handle(31, "pay", f"{code} cash 1.000.000")
    assert inv.order(oid)["paid"] == 2_000_000
    # the same command on the same order state (as a retry that did not see the first)
    inv.db.execute("UPDATE inv_orders SET paid=? WHERE id=?", (1_000_000, oid))
    reply = await staff.handle(31, "pay", f"{code} cash 1000000")
    assert "đã được ghi rồi" in reply
    assert len(inv.order(oid)["payments"]) == 2


# 3. links end with the account


async def test_links_are_dropped_with_the_account(shop):
    office, _llm, chat, users = shop
    links, staff = office.staff_links, office.employees["sales"].staff
    code = links.new_code("lan")
    users.update("lan", disabled=True)
    assert "không đúng" in await staff.handle(30, "link", code)
    users.update("lan", disabled=False)
    assert links.of_user("lan") == []  # nothing stored for a disabled account

    await link(office, 30, "lan")
    pending = links.new_code("lan")
    assert links.drop_user("lan") == [("sales", 30)]
    assert links.of_user("lan") == [] and links.user("sales", 30) is None
    assert "không đúng" in await staff.handle(31, "link", pending)  # its codes too
    assert links.drop_user("lan") == []

    # the menus: the former staff chat gets the customer menu back
    await link(office, 30, "lan")
    chats = links.drop_user("lan")
    assert await links.resync(chats=chats) == 1
    assert chat.prefs[30]["commands"][0]["keyword"] == "products"


# 4. and 9. commands beside the receive loop; secrets never reach the inbox or a model


async def test_a_slow_command_does_not_hold_up_other_chats(shop):
    office, llm, _chat, _users = shop
    sales = office.employees["sales"]
    release = asyncio.Event()
    handle = sales.menu.handle

    async def slow(cid, word, args, name):
        await release.wait()
        return await handle(cid, word, args, name)

    sales.menu.handle = slow
    replies: list[tuple[int, str]] = []
    llm.responses += [text("Chào chị!"), text("Dạ em đây.")]
    await sales._on_text(message(5, "/combos", replies))  # waits on the "model"
    await sales._on_text(message(5, "alo", replies))  # after it, in order
    await sales._on_text(message(6, "Chào shop", replies))  # another customer: not blocked
    for _ in range(50):
        await asyncio.sleep(0)
        if (6, "Chào chị!") in replies:
            break
    assert replies == [(6, "Chào chị!")]
    release.set()
    await drain(sales)
    assert [r for r in replies if r[0] == 5] == [(5, "Hiện chưa có combo nào."), (5, "Dạ em đây.")]


def test_parse_command():
    assert parse_command("/Products sofa") == ("products", "sofa")
    assert parse_command("/ADMIN secret") == ("admin", "secret")
    assert parse_command("/Admin:secret") == ("admin", ":secret")
    assert parse_command("/admin\nsecret") == ("admin", "secret")
    assert parse_command("/LINK abc") == ("link", "abc")
    assert parse_command("/ai\tshow") == ("ai", "show")
    assert parse_command("/") is None and parse_command("hello /admin") is None


async def test_commands_in_any_case_and_secrets_stay_out_of_the_inbox(shop):
    office, llm, _chat, _users = shop
    sales, hub = office.employees["sales"], office.hub
    replies: list[tuple[int, str]] = []
    await sales._on_text(message(5, "/Admin secret-token", replies))
    assert sales.state.is_admin(5)
    code = office.staff_links.new_code("lan")
    await sales._on_text(message(6, f"/LINK {code}", replies))
    await sales._on_text(message(7, "/ADMIN\nwrong", replies))
    await sales._on_other(message(8, "/admin https://example.com/x", replies))
    await drain(sales)
    assert office.staff_links.user("sales", 6).username == "lan"
    assert [c for c, r in replies if "Mã quản trị không đúng" in r] == [7, 8]
    for cid in (5, 6, 7, 8):
        conv = hub.inbox.find("simplex:sales", str(cid))
        assert conv is None or hub.inbox.messages(conv.id) == []
    assert llm.calls == []


# 5. /forget says what it does


async def test_forget_tells_the_truth(shop):
    office, _llm, _chat, _users = shop
    reply = await office.employees["sales"].command(5, "forget", "")
    assert "quên" in reply and "vẫn lưu" in reply and "memory" in reply
    assert "deleted" not in reply


# 6. numbers too long for the database, and Python's own error texts


async def test_huge_numbers_get_an_answer(shop, monkeypatch):
    office, _llm, _chat, _users = shop
    sales = office.employees["sales"]
    staff = sales.staff
    await link(office, 34, "quan")
    assert "Không có hội thoại" in await staff.handle(34, "open", HUGE)
    assert "Không có đơn" in await staff.handle(34, "order", "DH" + HUGE)
    assert "Không có đơn" in await staff.handle(34, "pay", f"DH{HUGE} cash")
    assert "Cú pháp" in await staff.handle(34, "sell", f"SOFA-01 {HUGE}")
    assert "Cú pháp" in await staff.handle(34, "go", "CX" + HUGE)
    assert "không thuộc chuyến" in await staff.handle(34, "delivered", HUGE)
    assert "Cú pháp: /approve <số>" in await staff.handle(34, "approve", HUGE)
    assert "Không tìm thấy đơn" in await sales.menu.handle(9, "invoice", "DH" + HUGE, "Hoa")
    await sales.command(5, "admin", "secret-token")
    assert "Cú pháp: /ai reject <số>" in await sales.command(5, "ai", f"reject {HUGE}")
    assert small_int("12") == 12 and small_int(HUGE) is None and small_int("١٢") is None

    def broken(*a, **k):
        raise ValueError("invalid literal for int() with base 10: 'x'")

    monkeypatch.setattr(office.inventory, "create_order", broken)
    reply = await staff.handle(34, "sell", "SOFA-01 1")
    assert "invalid literal" not in reply and reply.startswith("Cú pháp: /sell <mã SP>")


# 7. and 10. who is told, and how often


async def test_staff_are_told_about_their_channels_once(shop):
    office, _llm, chat, _users = shop
    sales = office.employees["sales"]
    await link(office, 30, "lan")
    await link(office, 40, "web")
    first = await sales.menu.handle(77, "staff", "", "Hoa")
    assert "Đã báo nhân viên" in first
    told = [cid for cid, t in chat.sent if "muốn gặp nhân viên" in t]
    assert told == [30]  # not the agent limited to the website chats
    again = await sales.menu.handle(77, "staff", "", "Hoa")
    assert "đã được báo rồi" in again
    assert [cid for cid, t in chat.sent if "muốn gặp nhân viên" in t] == [30]


async def test_admin_token_guessing_is_locked_out(shop):
    office, _llm, _chat, _users = shop
    sales = office.employees["sales"]
    for _ in range(5):
        assert await sales.command(5, "admin", "guess") == "Mã quản trị không đúng."
    assert "Thử lại sau 60 phút" in await sales.command(5, "admin", "secret-token")
    assert not sales.state.is_admin(5)
    assert "Bạn đã là quản trị viên" in await sales.command(6, "admin", "secret-token")  # others


async def test_invoice_email_at_most_once_per_order(shop, monkeypatch):
    office, _llm, _chat, _users = shop
    from ai_employees import invoices

    sent: list[int] = []

    async def email_invoice(office, oid):
        sent.append(oid)

    monkeypatch.setattr(invoices, "email_invoice", email_invoice)
    monkeypatch.setattr(office, "mailer", NS(ready=True))
    menu = office.employees["sales"].menu
    _conv, contact = menu._contact(50, "Hoa")
    office.hub.crm.update(int(contact["id"]), email="hoa@example.com")
    order = office.inventory.create_order([{"sku": "SOFA-01", "qty": 1}], contact_id=contact["id"])
    assert "đã được gửi tới ho***@example.com" in await menu.handle(50, "invoice", order["code"], "Hoa")
    assert "ít phút trước" in await menu.handle(50, "invoice", order["code"], "Hoa")
    assert sent == [order["id"]]


# 11. the customers' menu languages in a table


async def test_menu_languages_move_to_a_table(shop):
    office, _llm, chat, _users = shop
    office.docs.update(MENU_LANG_KEY, lambda d: d.update({"sales:50": "en", "bad": "fr"}), {})
    links = StaffLinks(office)  # as at the next start
    assert links.menu_language("sales", 50) == "en" and links.menu_language("sales", 51) == "vi"
    assert office.docs.get(MENU_LANG_KEY) == {}
    sales = office.employees["sales"]
    sales.state.set_language(50, "en")
    await sales.staff.localize_menu(50)
    assert 50 not in chat.prefs  # already given the English menu
    sales.state.set_language(50, "ja")
    await sales.staff.localize_menu(50)
    assert office.staff_links.menu_language("sales", 50) == "ja" and 50 in chat.prefs


# 12. menus and hints for the staff


async def test_staff_hints_and_menus_after_a_role_change(shop):
    office, _llm, chat, users = shop
    staff = office.employees["sales"].staff
    await link(office, 34, "quan")
    assert "/'stock <mã hoặc tên>'" in await staff.handle(34, "stock", "")
    assert "Cú pháp: /reject <số> [lý do]" in await staff.handle(34, "reject", "x")
    groups = [m["label"] for m in chat.prefs[34]["commands"] if m["type"] == "menu"]
    assert "🏬 Kho" in groups and "📊 Quản lý" in groups
    users.update("quan", role="cashier")
    assert await staff.resync("quan") == 1
    assert [m["label"] for m in chat.prefs[34]["commands"] if m["type"] == "menu"] == ["🧾 Bán hàng"]


def test_every_menu_label_is_extracted_for_translation():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("i18n_extract", root / "scripts" / "i18n_extract.py")
    extract = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(extract)
    texts = extract.server_texts()
    from ai_employees.staff_chat import COMMANDS, GROUPS

    labels = [label for label, _words in GROUPS]
    labels += [e[k] for _area, e in COMMANDS.values() for k in ("label", "params") if k in e]
    assert "🏬 Kho" in texts and [x for x in labels if x not in texts] == []
