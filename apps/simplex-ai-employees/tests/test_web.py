"""Admin web UI: authentication, CSRF protection and every API the page uses."""

from __future__ import annotations

import json
import os
import re
import stat

import httpx2
import pytest
from aiohttp.test_utils import TestClient, TestServer

from ai_employees.web import CSRF_HEADER, CSRF_VALUE, create_app

from fakes import ScriptedLLM, fake_chat, make_office, text, tool

PASSWORD = "correct horse battery staple"
H = {CSRF_HEADER: CSRF_VALUE}
BRIEF = {"id": "daily-brief", "days": "daily", "at": "08:00", "task": "Tóm tắt."}
ORDER = {
    "create_order": {
        "description": "Create an order.",
        "url": "https://shop.local/orders",
        "fields": {"items": "Items"},
    }
}


@pytest.fixture
async def ui(tmp_path):
    shop_requests: list[httpx2.Request] = []

    def shop(request: httpx2.Request) -> httpx2.Response:
        shop_requests.append(request)
        return httpx2.Response(201, text="created")

    llm = ScriptedLLM()
    office = make_office(
        tmp_path,
        llm,
        http=httpx2.AsyncClient(transport=httpx2.MockTransport(shop)),
        actions=ORDER,
        routines=[BRIEF],
        skills=["create_order", "current_time"],
    )
    chat = fake_chat(office.employees["sales"])
    client = TestClient(TestServer(create_app(office, PASSWORD)))
    await client.start_server()
    yield client, office, llm, chat, shop_requests
    await client.close()


async def login(client: TestClient) -> None:
    r = await client.post("/api/login", json={"password": PASSWORD}, headers=H)
    assert r.status == 200


async def test_page_and_security_headers(ui):
    client, *_ = ui
    r = await client.get("/")
    assert r.status == 200 and "Quản trị nhân viên AI" in await r.text()
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"
    assert (await client.get("/static/admin.js")).headers["Content-Type"].startswith("application/javascript")
    assert (await client.get("/static/secret.py")).status == 404
    # every script the page loads is served (the language first: the others use tr())
    for src in re.findall(r'<script src="([^"]+)"', await r.text()):
        assert (await client.get(src)).status == 200, src
    # an installable app (PWA): manifest, service worker at the root, icons, all public
    import json

    manifest = json.loads(await (await client.get("/manifest.webmanifest")).text())
    assert manifest["display"] == "standalone" and manifest["start_url"] == "/"
    assert {i["sizes"] for i in manifest["icons"]} >= {"192x192", "512x512"}
    sw = await client.get("/sw.js")
    assert sw.status == 200 and "/api" in await sw.text()
    icon = await client.get("/static/icon-512.png")
    assert icon.headers["Content-Type"] == "image/png" and (await icon.read())[:4] == b"\x89PNG"
    assert '<link rel="manifest"' in await r.text()


async def test_login_session_and_csrf(ui):
    client, *_ = ui
    assert (await client.get("/api/overview")).status == 401
    assert (await client.post("/api/login", json={"password": PASSWORD})).status == 403  # no CSRF header
    assert (await client.post("/api/login", json={"password": "nope"}, headers=H)).status == 401
    r = await client.post("/api/login", json={"password": PASSWORD}, headers=H)
    set_cookie = r.headers["Set-Cookie"]
    assert "aie_session=" in set_cookie and "HttpOnly" in set_cookie and "SameSite=Strict" in set_cookie
    assert (await client.get("/api/overview")).status == 200
    # logged in, but a state change without the header (a cross-site form) is refused
    assert (await client.patch("/api/employees/sales", json={"paused": True})).status == 403
    await client.post("/api/logout", json={}, headers=H)
    assert (await client.get("/api/overview")).status == 401


async def test_edit_employee(ui):
    client, office, *_ = ui
    await login(client)
    over = await (await client.get("/api/overview")).json()
    assert [e["id"] for e in over["employees"]] == ["sales", "accountant"]
    assert over["employees"][0]["routines"][0]["id"] == "daily-brief"

    r = await client.patch(
        "/api/employees/sales",
        json={
            "system_prompt": "Bạn là Lan.",
            "model": "claude-sonnet-5",
            "effort": "high",
            "skills": ["current_time"],
            "releases": ["create_order"],
            "paused": True,
        },
        headers=H,
    )
    d = await r.json()
    assert r.status == 200 and d["system_prompt"] == "Bạn là Lan." and d["model"] == "claude-sonnet-5"
    assert d["skills"] == ["current_time"] and d["releases"] == ["create_order"] and d["paused"]
    sales = office.employees["sales"]
    assert sales.settings.effort == "high" and sales.settings.paused

    for body, msg in [
        ({"model": "gpt-9"}, "chưa được khai báo"),
        ({"skills": ["teleport"]}, "không tồn tại"),
        ({"releases": ["nope"]}, "không tồn tại"),
        ({"effort": "huge"}, "effort"),
        ({"system_prompt": " "}, "trống"),
    ]:
        r = await client.patch("/api/employees/sales", json=body, headers=H)
        assert r.status == 400 and msg in (await r.json())["error"]
    assert (await client.get("/api/employees/nobody")).status == 404

    await client.post("/api/employees/sales/corrections", json={"text": "Luôn xưng em."}, headers=H)
    d = await (await client.get("/api/employees/sales")).json()
    assert d["corrections"][0]["text"] == "Luôn xưng em."
    d = await (await client.delete("/api/employees/sales/corrections/1", headers=H)).json()
    assert d["corrections"] == []
    d = await (await client.post("/api/employees/sales/reset", json={}, headers=H)).json()
    assert d["overrides"] == [] and d["model"] == "claude-opus-5"


async def test_models_keys_are_write_only(ui, tmp_path):
    client, office, llm, *_ = ui
    await login(client)
    r = await client.post(
        "/api/models",
        json={
            "name": "deepseek",
            "provider": "openai",
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-very-secret",
            "extra_body": '{"temperature": 0.2}',
        },
        headers=H,
    )
    body = await r.text()
    assert r.status == 200 and "sk-very-secret" not in body
    row = next(m for m in json.loads(body)["models"] if m["name"] == "deepseek")
    assert (
        row["has_key"]
        and row["source"] == "ui"
        and row["describe"] == "openai: deepseek-chat @ api.deepseek.com"
    )
    if office.db is None:  # SQLite: keys added from the UI are in an owner-only file
        assert stat.S_IMODE(os.stat(tmp_path / "state" / "office.sqlite").st_mode) == 0o600
    assert office.model_profile("deepseek").extra_body == {"temperature": 0.2}

    # assign it, then test a connection on the Claude model through the fake
    r = await client.patch("/api/employees/sales", json={"model": "deepseek"}, headers=H)
    assert (await r.json())["model_desc"] == "openai: deepseek-chat @ api.deepseek.com"
    llm.responses.append(text("OK"))
    t = await (await client.post("/api/models/claude-opus-5/test", json={}, headers=H)).json()
    assert t["ok"] and t["text"] == "OK"

    bad = await client.post("/api/models", json={"name": "x", "provider": "azure", "model": "m"}, headers=H)
    assert bad.status == 400
    assert (await client.delete("/api/models/claude-opus-5", headers=H)).status == 400
    r = await client.delete("/api/models/deepseek", headers=H)
    assert all(m["name"] != "deepseek" for m in (await r.json())["models"])


async def test_approvals_routines_conversations_and_runlog(ui):
    client, office, llm, _chat, shop_requests = ui
    await login(client)
    sales = office.employees["sales"]
    llm.responses += [tool("create_order", {"items": "2 x MA-100"}), text("Đã ghi nhận")]
    await sales.agent.respond(7, "An", "Đặt 2 máy")

    ap = await (await client.get("/api/approvals")).json()
    [p] = ap["pending"]
    assert p["action"] == "create_order" and p["employee"] == "sales" and p["contact_name"] == "An"
    r = await (await client.post(f"/api/approvals/sales/{p['id']}/approve", json={}, headers=H)).json()
    assert r["action"]["status"] == "done" and len(shop_requests) == 1
    assert (await (await client.get("/api/approvals")).json())["recent"][0]["decided_by"] == "web admin"

    llm.responses.append(text("Báo cáo sáng"))
    r = await (await client.post("/api/employees/sales/routines/daily-brief/run", json={}, headers=H)).json()
    assert r == {"status": "ok", "text": "Báo cáo sáng"}
    d = await (
        await client.post("/api/employees/sales/routines/daily-brief/pause", json={"paused": True}, headers=H)
    ).json()
    assert d["routines"][0]["paused"] and d["routines"][0]["last_output"] == "Báo cáo sáng"

    convs = await (await client.get("/api/employees/sales/conversations")).json()
    assert convs["contacts"][0]["name"] == "An" and convs["contacts"][0]["turns"] == 1
    conv = await (await client.get("/api/employees/sales/conversations/7")).json()
    assert [t["role"] for t in conv["turns"]] == ["user", "assistant"] and "ts" in conv["turns"][0]
    await client.delete("/api/employees/sales/conversations/7", headers=H)
    assert sales.state.history(7) == []

    log = await (await client.get("/api/runlog?employee=sales&kind=action")).json()
    assert [r["status"] for r in log["records"]] == ["ok", "queued"]  # newest first
