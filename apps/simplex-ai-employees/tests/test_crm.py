"""Customers across channels: contacts, details found in messages, duplicates, merging,
companies, and the AI remembering a merged customer's other channels."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from test_channels import PASSWORD, H, env, ms, settle, setup, ui  # noqa: F401 - fixtures

from ai_employees.crm import CRM, find_email, find_phone, phone_key
from ai_employees.inbox import Inbox

from fakes import text


def test_phone_and_email_detection():
    assert find_phone("sđt em 0901 234 567 nhé") == "0901 234 567"
    assert find_phone("gọi +84 901.234.567 giúp") == "+84 901.234.567"
    assert find_phone("mã đơn DH-2024123456") == ""
    assert find_phone("giá 4500000 đồng") == ""
    assert phone_key("0901 234 567") == phone_key("+84 901-234-567") == "84901234567"
    assert find_email("mail: Mai.Nguyen@ABC.com.vn ạ") == "mai.nguyen@abc.com.vn"
    assert find_email("không có") == ""


def test_contacts_merge_split_and_companies(tmp_path):
    inbox = Inbox(tmp_path / "inbox.db")
    crm = CRM(inbox.db)
    web = inbox.upsert("website", "v1", "Mai", "sales")
    wa = inbox.upsert("whatsapp", "84901234567@c.us", "Mai N.", "sales")
    mail = inbox.upsert("email", "mai@abc.com.vn", "Nguyễn Mai", "sales")

    a = crm.observe(web, "Em hỏi máy lọc, sđt 0901 234 567")
    assert (a["name"], a["phone"]) == ("Mai", "0901 234 567")
    b = crm.observe(wa, "Hello", "whatsapp")
    assert b["phone"] == "+84901234567" and b["id"] != a["id"]
    assert crm.observe(web, "số mới 0988 888 888")["phone"] == "0901 234 567"  # never overwritten
    [group] = crm.duplicates()
    assert group["reason"] == "phone" and {c["id"] for c in group["contacts"]} == {a["id"], b["id"]}
    assert crm.duplicates(a["id"]) == [group]

    company = crm.save_company(None, name="Công ty ABC", domain="@ABC.com.vn")
    assert company["domain"] == "abc.com.vn"
    c = crm.observe(mail, "Báo giá giúp em", "email")
    assert c["email"] == "mai@abc.com.vn" and c["company_id"] == company["id"]  # linked by domain

    merged = crm.merge(a["id"], b["id"])
    assert crm.contact(b["id"]) is None and merged["phone"] == "0901 234 567"
    assert crm.conversations(a["id"]) == [web.id, wa.id]
    merged = crm.merge(a["id"], c["id"])
    assert merged["email"] == "mai@abc.com.vn" and merged["company_id"] == company["id"]
    assert crm.duplicates() == []
    [row] = crm.search("0901234567")
    assert row["conversation_count"] == 3 and row["id"] == a["id"]
    assert [r["id"] for r in crm.search(company_id=company["id"])] == [a["id"]]

    alone = crm.split(wa)
    assert alone["id"] != a["id"] and crm.conversations(a["id"]) == [web.id, mail.id]
    crm.delete_company(company["id"])
    assert crm.contact(a["id"])["company_id"] is None and crm.companies() == []


async def test_crm_in_the_inbox_and_the_ai_remembers_other_channels(ui):  # noqa: F811
    client, office, llm, _platforms = ui
    hub = office.hub
    hook = {"X-Hook-Secret": "hook-secret-1"}
    llm.responses.append(text("Dạ em ghi nhận số của chị ạ."))
    await client.post(
        "/hooks/website",
        json={
            "conversation_id": "v1",
            "customer_name": "Chị Mai",
            "text": "Em muốn đặt MA-100, sđt 0901 234 567",
        },
        headers=hook,
    )
    llm.responses.append(text("Dạ chào chị."))
    zalo = hub.push_inbound(
        "zalo-canhan",
        {
            "event": "message",
            "data": {
                "id": "z1",
                "type": "user",
                "threadId": "t1",
                "senderName": "Mai",
                "content": "Chào shop, số chị 0901234567",
                "timestamp": ms(datetime.now(UTC)),
            },
        },
    )
    await settle(hub)
    web_conv = hub.inbox.find("website", "v1")

    # staff account limited to the website channel
    await client.post("/api/login", json={"password": PASSWORD}, headers=H)
    await client.post(
        "/api/users",
        json={
            "username": "thu",
            "name": "Thu",
            "role": "agent",
            "password": "0123456789",
            "channels": ["website"],
        },
        headers=H,
    )
    r = await (await client.get(f"/api/inbox/{zalo.id}/contact")).json()
    zalo_contact = r["contact"]
    assert zalo_contact["phone"] == "0901234567" and len(zalo_contact["duplicates"]) == 1
    r = await (await client.get(f"/api/inbox/{web_conv.id}/contact")).json()
    web_contact = r["contact"]
    assert [c["id"] for c in web_contact["conversations"]] == [web_conv.id]

    # the admin merges the two from the inbox
    r = await client.post(
        f"/api/inbox/{web_conv.id}/contact/merge", json={"other": zalo_contact["id"]}, headers=H
    )
    merged = (await r.json())["contact"]
    assert {c["id"] for c in merged["conversations"]} == {web_conv.id, zalo.id} and merged["duplicates"] == []

    # the AI answering on Zalo now knows what was said on the website
    llm.responses.append(text("Dạ đơn MA-100 của chị em đang xử lý ạ."))
    hub.push_inbound(
        "zalo-canhan",
        {
            "event": "message",
            "data": {
                "id": "z2",
                "type": "user",
                "threadId": "t1",
                "content": "Đơn của chị sao rồi?",
                "timestamp": ms(datetime.now(UTC)),
            },
        },
    )
    await settle(hub)
    system = json.dumps(llm.calls[-1].get("system"), ensure_ascii=False)
    assert "The same customer on website" in system and "Em muốn đặt MA-100" in system
    assert "Customer profile: name: Chị Mai; phone: 0901 234 567" in system

    # the customers page is for admins; companies and editing
    r = await client.post("/api/crm/companies", json={"name": "ABC", "domain": "abc.vn"}, headers=H)
    company = (await r.json())["companies"][0]
    r = await client.patch(
        f"/api/crm/contacts/{merged['id']}", json={"email": "mai@abc.vn", "notes": "Khách sỉ"}, headers=H
    )
    detail = (await r.json())["contact"]
    assert detail["company"] == "ABC" and detail["notes"] == "Khách sỉ"
    listed = await (await client.get("/api/crm/contacts?q=abc.vn")).json()
    assert [c["id"] for c in listed["contacts"]] == [merged["id"]]
    assert (
        await client.patch(f"/api/crm/contacts/{merged['id']}", json={"company_id": 999}, headers=H)
    ).status == 400
    assert (await (await client.get("/api/crm/duplicates")).json())["groups"] == []

    # an agent sees the customer through their own channel only, and cannot merge
    await client.post("/api/logout", json={}, headers=H)
    await client.post("/api/login", json={"username": "thu", "password": "0123456789"}, headers=H)
    assert (await client.get("/api/crm/contacts")).status == 403
    r = await (await client.get(f"/api/inbox/{web_conv.id}/contact")).json()
    assert [c["id"] for c in r["contact"]["conversations"]] == [web_conv.id]
    assert r["contact"]["duplicates"] == [] and r["companies"] == [{"id": company["id"], "name": "ABC"}]
    r = await client.post(f"/api/inbox/{web_conv.id}/contact", json={"name": "Nguyễn Thị Mai"}, headers=H)
    assert (await r.json())["contact"]["name"] == "Nguyễn Thị Mai"
    assert (
        await client.post(f"/api/inbox/{web_conv.id}/contact/merge", json={"split": True}, headers=H)
    ).status == 403
    assert (await client.get(f"/api/inbox/{zalo.id}/contact")).status == 404
