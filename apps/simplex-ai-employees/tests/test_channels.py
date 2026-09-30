"""Omnichannel inbox: Zalo OA / Facebook / webhook channels on mock platform APIs,
AI auto-replies, human takeover, and the inbox and hook endpoints of the web UI."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
from typing import Any

import httpx2
import pytest
from aiohttp.test_utils import TestClient, TestServer

from ai_employees.config import ConfigError, parse_config
from ai_employees.hub import iso
from ai_employees.inbox import EXTERNAL_BASE
from ai_employees.web import CSRF_HEADER, CSRF_VALUE, create_app

from fakes import ScriptedLLM, fake_chat, make_office, text

PASSWORD = "correct horse battery staple"
H = {CSRF_HEADER: CSRF_VALUE}

ZALO = {
    "id": "zalo-shop",
    "type": "zalo_oa",
    "employee": "sales",
    "app_id": "42",
    "app_secret_env": "T_ZALO_SECRET",
    "access_token_env": "T_ZALO_ACCESS",
    "refresh_token_env": "T_ZALO_REFRESH",
    "webhook_secret_env": "T_ZALO_OA_SECRET",
    "debounce_seconds": 0,
}
FACEBOOK = {
    "id": "fanpage",
    "type": "facebook",
    "employee": "sales",
    "page_id": "1000",
    "access_token_env": "T_FB_TOKEN",
    "app_secret_env": "T_FB_APP_SECRET",
    "verify_token": "verify-me-123",
    "debounce_seconds": 0,
}
ZALO_PERSONAL = {
    "id": "zalo-canhan",
    "type": "zalo_personal",
    "employee": "sales",
    "gateway_url": "http://gateway.local:3000",
    "api_key_env": "T_GW_KEY",
    "secret_env": "T_GW_HOOK",
    "debounce_seconds": 0,
}
WEBHOOK = {
    "id": "website",
    "type": "webhook",
    "employee": "sales",
    "secret_env": "T_HOOK_SECRET",
    "reply_url": "https://bridge.local/reply",
    "debounce_seconds": 0,
}


def ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


def fb_time(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%S+0000")


class Platforms:
    """Zalo OA, Facebook Graph and a webhook bridge, all on one mock transport."""

    def __init__(self) -> None:
        self.zalo_token = "fresh"  # the token the Zalo API currently accepts
        self.zalo_msgs: list[dict[str, Any]] = []  # newest first, as Zalo returns them
        self.fb_msgs: list[dict[str, Any]] = []
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.refreshes = 0
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle))

    def handle(self, r: httpx2.Request) -> httpx2.Response:
        url = r.url
        if url.host == "oauth.zaloapp.com":
            assert r.headers["secret_key"] == "zsecret"
            if url.params["refresh_token"] != "r1":
                return httpx2.Response(200, json={"error": -14014, "message": "Invalid refresh token"})
            self.refreshes += 1
            self.zalo_token = "fresh"
            return httpx2.Response(200, json={"access_token": "fresh", "refresh_token": "r2"})
        if url.host == "openapi.zalo.me":
            if r.headers.get("access_token") != self.zalo_token:
                return httpx2.Response(200, json={"error": -216, "message": "Access token is invalid"})
            if url.path.endswith("/listrecentchat"):
                last = self.zalo_msgs[:1]
                return httpx2.Response(200, json={"error": 0, "data": last})
            if url.path.endswith("/conversation"):
                assert json.loads(url.params["data"])["user_id"] == "u-1"
                return httpx2.Response(200, json={"error": 0, "data": self.zalo_msgs})
            if url.path.endswith("/user/detail"):
                return httpx2.Response(200, json={"error": 0, "data": {"display_name": "Hoa Nguyễn"}})
            if url.path.endswith("/message/cs"):
                body = json.loads(r.content)
                self.sent.append(("zalo", body))
                mid = f"z-out-{len(self.sent)}"
                now = ms(datetime.now(UTC))
                self.zalo_msgs.insert(
                    0,
                    {
                        "src": 0,
                        "message_id": mid,
                        "message": body["message"]["text"],
                        "time": now,
                        "to_id": "u-1",
                        "to_display_name": "Hoa",
                    },
                )
                return httpx2.Response(200, json={"error": 0, "data": {"message_id": mid}})
        if url.host == "graph.facebook.com":
            assert url.params["access_token"] == "fbtoken"
            if url.path.endswith("/1000/conversations"):
                updated = self.fb_msgs[0]["created_time"] if self.fb_msgs else fb_time(datetime.now(UTC))
                conv = {
                    "id": "t_1",
                    "updated_time": updated,
                    "participants": {
                        "data": [{"id": "1000", "name": "Shop"}, {"id": "psid-9", "name": "Tuấn"}]
                    },
                }
                return httpx2.Response(200, json={"data": [conv]})
            if url.path.endswith("/t_1/messages"):
                return httpx2.Response(200, json={"data": self.fb_msgs})
            if url.path.endswith("/psid-77"):
                return httpx2.Response(200, json={"name": "Tuấn Trần", "id": "psid-77"})
            if url.path.endswith("/me/messages"):
                body = json.loads(r.content)
                self.sent.append(("facebook", body))
                return httpx2.Response(200, json={"message_id": f"m_{len(self.sent)}"})
        if url.host == "gateway.local":
            if r.headers.get("x-api-key") != "gw-key-0123456789":
                return httpx2.Response(401, json={"error": "unauthorized"})
            if url.path == "/zalo-canhan/api/send-message":
                body = json.loads(r.content)
                self.sent.append(("zalo_personal", body))
                return httpx2.Response(200, json={"ok": True, "message_id": "7001"})
            if url.path == "/zalo-canhan/api/init":
                return httpx2.Response(200, json={"state": "qr_pending"})
            if url.path == "/zalo-canhan/api/qr":
                return httpx2.Response(200, json={"state": "qr_pending", "qr": "data:image/png;base64,AAAA"})
        if url.host == "cdn.example" or r.headers.get("host") == "cdn.example":  # pinned to its address
            if url.path == "/p.jpg":
                return httpx2.Response(200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"})
            if url.path == "/page.html":
                return httpx2.Response(
                    200, content=b"<script>alert(1)</script>", headers={"content-type": "text/html"}
                )
            if url.path == "/hop":
                return httpx2.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
        if url.host == "bridge.local":
            self.sent.append(("webhook", json.loads(r.content)))
            return httpx2.Response(200, json={"message_id": f"w-{len(self.sent)}"})
        return httpx2.Response(404, json={"error": "unexpected " + str(url)})

    def zalo_customer(self, mid: str, message: str, ts: datetime) -> None:
        self.zalo_msgs.insert(
            0,
            {
                "src": 1,
                "message_id": mid,
                "message": message,
                "time": ms(ts),
                "from_id": "u-1",
                "from_display_name": "Hoa",
            },
        )

    def fb(self, mid: str, message: str, ts: datetime, from_page: bool = False) -> None:
        sender = {"id": "1000", "name": "Shop"} if from_page else {"id": "psid-9", "name": "Tuấn"}
        self.fb_msgs.insert(0, {"id": mid, "message": message, "from": sender, "created_time": fb_time(ts)})


@pytest.fixture
def env(monkeypatch):
    for k, v in {
        "T_ZALO_SECRET": "zsecret",
        "T_ZALO_ACCESS": "stale",
        "T_ZALO_REFRESH": "r1",
        "T_FB_TOKEN": "fbtoken",
        "T_HOOK_SECRET": "hook-secret-1",
        "T_ZALO_OA_SECRET": "zalo-oa-secret-key",
        "T_FB_APP_SECRET": "fb-app-secret",
        "T_GW_KEY": "gw-key-0123456789",
        "T_GW_HOOK": "gw-hook-0123456789",
    }.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def setup(tmp_path, env):
    llm = ScriptedLLM()
    platforms = Platforms()
    office = make_office(
        tmp_path, llm, http=platforms.client, channels=[ZALO, ZALO_PERSONAL, FACEBOOK, WEBHOOK]
    )
    chat = fake_chat(office.employees["sales"])
    return office, llm, platforms, chat


async def settle(hub: Any) -> None:
    """Wait for debounced replies (and anything they start) to finish."""
    for _ in range(20):
        await asyncio.sleep(0)
        if not hub._tasks:
            return
        await asyncio.gather(*list(hub._tasks), return_exceptions=True)


def test_channel_config_is_validated(tmp_path):
    base = {
        "state_dir": str(tmp_path),
        "employees": [
            {"id": "sales", "display_name": "Lan", "system_prompt": "x", "db": str(tmp_path / "db")}
        ],
    }
    bad = [
        ({"id": "Zalo Shop", "type": "zalo_oa", "employee": "sales", "app_id": "1"}, "lowercase"),
        ({"id": "simplex-x", "type": "webhook", "employee": "sales"}, "lowercase"),
        ({"id": "z", "type": "viber", "employee": "sales"}, "type must be"),
        ({"id": "z", "type": "zalo_oa", "employee": "nobody", "app_id": "1"}, "not declared"),
        ({"id": "z", "type": "zalo_oa", "employee": "sales"}, "'app_id' is required"),
        ({"id": "f", "type": "facebook", "employee": "sales"}, "'page_id' is required"),
    ]
    for ch, msg in bad:
        with pytest.raises(ConfigError, match=msg):
            parse_config({**base, "channels": [ch]}, base_dir=tmp_path)
    dup = {"id": "w", "type": "webhook", "employee": "sales"}
    with pytest.raises(ConfigError, match="duplicate"):
        parse_config({**base, "channels": [dup, dup]}, base_dir=tmp_path)


async def test_zalo_poll_refreshes_token_and_ai_answers_only_new_messages(setup, tmp_path):
    office, llm, platforms, _ = setup
    hub = office.hub
    platforms.zalo_token = "fresh"  # the configured "stale" token is refused with -216
    platforms.zalo_customer("z1", "Hôm qua mình hỏi giá", hub.started - timedelta(hours=3))
    platforms.zalo_customer("z2", "Máy lọc MA-100 giá bao nhiêu?", datetime.now(UTC) + timedelta(seconds=1))
    llm.responses.append(text("Dạ MA-100 giá 4.990.000đ ạ."))

    assert await hub.poll_once("zalo-shop") == 2
    await settle(hub)

    # the rotated token pair is kept (owner-only) so it survives restarts
    assert platforms.refreshes == 1
    assert hub.secrets["zalo-shop"] == {"access_token": "fresh", "refresh_token": "r2"}
    if office.db is None:  # SQLite: the file holding tokens is owner-only
        assert stat.S_IMODE(os.stat(tmp_path / "state" / "office.sqlite").st_mode) == 0o600

    # only the message that arrived after start-up was answered; the backlog is context
    assert platforms.sent == [
        ("zalo", {"recipient": {"user_id": "u-1"}, "message": {"text": "Dạ MA-100 giá 4.990.000đ ạ."}})
    ]
    assert len(llm.calls) == 1 and "MA-100 giá bao nhiêu" in json.dumps(
        llm.calls[0]["messages"], ensure_ascii=False
    )
    [conv] = hub.inbox.list()
    assert (conv.channel, conv.customer_name, conv.mode) == ("zalo-shop", "Hoa", "ai")
    assert [m["sender"] for m in hub.inbox.messages(conv.id)] == ["customer", "customer", "ai"]

    # the next poll sees our own reply echoed back: no duplicate, no takeover, no new answer
    assert await hub.poll_once("zalo-shop") == 0
    await settle(hub)
    assert hub.inbox.conversation(conv.id).mode == "ai" and len(hub.inbox.messages(conv.id)) == 3

    # a staff member answers inside the Zalo OA manager: recorded, and the AI steps back
    platforms.zalo_msgs.insert(
        0,
        {
            "src": 0,
            "message_id": "z-staff",
            "message": "Chị để em gọi lại ạ",
            "time": ms(datetime.now(UTC)),
            "to_id": "u-1",
            "to_display_name": "Hoa",
        },
    )
    platforms.zalo_customer("z3", "Ok em", datetime.now(UTC) + timedelta(seconds=2))
    assert await hub.poll_once("zalo-shop") == 2
    await settle(hub)
    conv = hub.inbox.conversation(conv.id)
    assert conv.mode == "human" and len(platforms.sent) == 1 and len(llm.calls) == 1
    assert hub.inbox.messages(conv.id)[-2]["author"] == "trên zalo_oa"


async def test_poll_errors_are_recorded_not_raised(setup, monkeypatch):
    office, _, _, _ = setup
    monkeypatch.setenv("T_ZALO_REFRESH", "revoked")
    assert await office.hub.poll_once("zalo-shop") == 0
    assert "-14014" in office.hub.inbox.channel_state("zalo-shop")["last_error"]


async def test_facebook_human_takeover_and_handback(setup):
    office, llm, platforms, _ = setup
    hub, sales = office.hub, office.employees["sales"]
    now = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
    platforms.fb("m1", "Còn hàng không shop?", now)
    platforms.fb("m2", "Mình ở Đà Nẵng", now + timedelta(seconds=1))
    llm.responses.append(text("Dạ còn hàng, giao Đà Nẵng 2 ngày ạ."))
    await hub.poll_once("fanpage")
    await settle(hub)
    # two quick messages, one answer that saw both
    assert len(llm.calls) == 1 and "Còn hàng không shop?\\nMình ở Đà Nẵng" in json.dumps(
        llm.calls[0]["messages"], ensure_ascii=False
    )
    assert platforms.sent[-1] == (
        "facebook",
        {
            "recipient": {"id": "psid-9"},
            "messaging_type": "RESPONSE",
            "message": {"text": "Dạ còn hàng, giao Đà Nẵng 2 ngày ạ."},
        },
    )
    [conv] = hub.inbox.list(channel="fanpage")
    assert conv.customer_name == "Tuấn" and conv.contact_id == EXTERNAL_BASE + conv.id

    # a person answers from the inbox and takes over
    platforms.fb("m3", "Cho mình xin giảm giá", now + timedelta(seconds=5))
    await hub.poll_once("fanpage")
    await hub.human_reply(conv.id, "Anh để em hỏi quản lý nhé", "Thu")
    await settle(hub)
    assert platforms.sent[-1][1]["message"]["text"] == "Anh để em hỏi quản lý nhé"
    assert hub.inbox.conversation(conv.id).mode == "human" and len(llm.calls) == 1
    # the AI's memory has the staff turn, so it can continue the conversation later
    assert sales.state.history(conv.contact_id)[-1]["content"] == "[nhân viên Thu] Anh để em hỏi quản lý nhé"

    platforms.fb("m4", "Vậy chốt nhé", now + timedelta(seconds=9))
    await hub.poll_once("fanpage")
    await settle(hub)
    assert len(llm.calls) == 1  # silent while a human handles it

    llm.responses.append(text("Dạ em chốt đơn cho anh ạ."))
    hub.inbox.set_mode(conv.id, "ai")
    assert await hub.reply_ai(conv.id) == "Dạ em chốt đơn cho anh ạ."
    assert await hub.reply_ai(conv.id) is None  # nothing left waiting


async def test_ai_reply_is_dropped_when_taken_over_mid_answer(setup):
    office, llm, platforms, _ = setup
    hub = office.hub

    def slow(params):
        hub.inbox.set_mode(conv.id, "human")  # someone clicked "take over" while the model ran
        return text("late answer")

    llm.responses.append(slow)
    conv = hub.webhook_inbound("website", {"conversation_id": "v1", "text": "hello"})
    await settle(hub)
    assert platforms.sent == [] and [m["sender"] for m in hub.inbox.messages(conv.id)] == ["customer"]


async def test_simplex_chats_are_mirrored_and_respect_takeover(setup):
    office, llm, _, chat = setup
    sales, hub = office.employees["sales"], office.hub
    replies: list[str] = []

    async def reply(t: str) -> None:
        replies.append(t)

    def msg(t: str) -> NS:
        contact = {"contactId": 5, "profile": {"displayName": "Bảo"}, "localDisplayName": "bao"}
        return NS(chat_info={"contact": contact}, text=t, reply=reply)

    llm.responses.append(text("Chào anh Bảo!"))
    await sales._on_text(msg("Chào shop"))
    await asyncio.gather(*list(sales._tasks))
    conv = hub.inbox.find("simplex:sales", "5")
    assert replies == ["Chào anh Bảo!"] and conv.contact_id == 5
    assert [(m["sender"], m["text"]) for m in hub.inbox.messages(conv.id)] == [
        ("customer", "Chào shop"),
        ("ai", "Chào anh Bảo!"),
    ]
    # admin commands (which carry tokens) never reach the inbox
    await sales._on_text(msg("/admin wrong-token"))
    assert len(hub.inbox.messages(conv.id)) == 2

    await hub.human_reply(conv.id, "Em là Thu, em hỗ trợ anh nhé", "Thu")
    assert chat.sent[-1] == (5, "Em là Thu, em hỗ trợ anh nhé")
    await sales._on_text(msg("Ok"))
    assert not sales._tasks and len(llm.calls) == 1  # human mode: the bot stays quiet


@pytest.fixture
async def ui(setup):
    office, llm, platforms, _chat = setup
    client = TestClient(TestServer(create_app(office, PASSWORD)))
    await client.start_server()
    yield client, office, llm, platforms
    await client.close()


async def test_webhook_channel_and_inbox_api(ui):
    client, office, llm, platforms = ui
    hub = office.hub
    hook = {"X-Hook-Secret": "hook-secret-1"}
    body = {
        "conversation_id": "visitor-7",
        "customer_name": "Linh",
        "text": "Shop có ship COD không?",
        "message_id": "w1",
    }

    assert (await client.post("/hooks/website", json=body)).status == 401
    assert (await client.post("/hooks/website", json=body, headers={"X-Hook-Secret": "nope"})).status == 401
    # Messenger checks its own signature, not the bridge secret
    assert (await client.post("/hooks/fanpage", json=body, headers=hook)).status == 401
    assert (await client.post("/hooks/website", json={"conversation_id": "x"}, headers=hook)).status == 400
    llm.responses.append(text("Dạ có COD toàn quốc ạ."))
    r = await client.post("/hooks/website", json=body, headers=hook)
    assert r.status == 200
    cid = (await r.json())["conversation"]
    assert (await client.post("/hooks/website", json=body, headers=hook)).status == 200  # retried delivery
    await settle(hub)
    assert platforms.sent == [("webhook", {"conversation_id": "visitor-7", "text": "Dạ có COD toàn quốc ạ."})]
    polled = await (await client.get("/hooks/website/visitor-7?after=0", headers=hook)).json()
    assert [m["sender"] for m in polled["messages"]] == ["customer", "ai"]  # the retry was deduplicated
    assert (await client.get("/hooks/website/visitor-7?after=x", headers=hook)).status == 400

    # the inbox API needs a login
    assert (await client.get("/api/inbox")).status == 401
    assert (await client.post("/api/login", json={"password": PASSWORD}, headers=H)).status == 200
    data = await (await client.get("/api/inbox")).json()
    [c] = data["conversations"]
    assert c["channel_info"] == {"type": "webhook", "name": "Webhook · website"} and c["unread"] == 1
    assert {ch["id"] for ch in data["channels"]} == {
        "simplex:sales",
        "simplex:accountant",
        "zalo-shop",
        "zalo-canhan",
        "fanpage",
        "website",
    }
    assert (await (await client.get("/api/inbox?mode=human")).json())["conversations"] == []
    assert len((await (await client.get("/api/inbox?q=COD")).json())["conversations"]) == 1

    # a draft from the AI: not sent, not remembered
    llm.responses.append(text("Dạ phí ship 30k ạ."))
    history = office.employees["sales"].state.history(EXTERNAL_BASE + cid)
    r = await (await client.post(f"/api/inbox/{cid}/suggest", json={}, headers=H)).json()
    assert r == {"text": "Dạ phí ship 30k ạ."} and len(platforms.sent) == 1
    assert office.employees["sales"].state.history(EXTERNAL_BASE + cid) == history
    assert "handoff_to_human" not in {t["name"] for t in llm.calls[-1].get("tools", [])}

    d = await (
        await client.post(
            f"/api/inbox/{cid}/reply", json={"text": "Phí ship 30k chị nhé", "author": "Thu"}, headers=H
        )
    ).json()
    assert d["conversation"]["mode"] == "human" and d["conversation"]["unread"] == 0
    assert d["messages"][-1] | {"id": 0, "ts": ""} == {
        "id": 0,
        "ts": "",
        "external_id": "w-2",
        "sender": "human",
        "author": "Chủ",  # the logged-in account (the owner), not the typed name
        "text": "Phí ship 30k chị nhé",
        "attachments": [],
        "translation": "",
    }
    assert (await client.post(f"/api/inbox/{cid}/reply", json={"text": " "}, headers=H)).status == 400
    assert (await client.post(f"/api/inbox/{cid}/reply", json={"text": "x"})).status == 403  # CSRF header

    d = await (
        await client.post(f"/api/inbox/{cid}/assign", json={"employee": "accountant"}, headers=H)
    ).json()
    assert d["conversation"]["employee"] == "accountant"
    assert (
        await client.post(f"/api/inbox/{cid}/assign", json={"employee": "ghost"}, headers=H)
    ).status == 400
    assert (await client.post(f"/api/inbox/{cid}/mode", json={"mode": "robot"}, headers=H)).status == 400
    d = await (await client.post(f"/api/inbox/{cid}/mode", json={"mode": "ai"}, headers=H)).json()
    assert d["conversation"]["mode"] == "ai"
    assert (await client.get("/api/inbox/999")).status == 404

    chans = {c["id"]: c for c in (await (await client.get("/api/channels")).json())["channels"]}
    assert chans["website"]["stats"] == {"conversations": 1, "unread": 0, "human": 0}
    assert chans["zalo-shop"]["auto_reply"] and chans["simplex:sales"]["type"] == "simplex"
    r = await (await client.post("/api/channels/fanpage/poll", json={}, headers=H)).json()
    assert r["added"] == 0 and r["state"]["last_error"] is None
    assert (await client.post("/api/channels/nope/poll", json={}, headers=H)).status == 404


async def test_simplex_page_api(ui):
    client, office, *_ = ui
    sales = office.employees["sales"]
    calls: list[Any] = []

    async def get_user() -> dict[str, Any]:
        return {"userId": 1}

    async def create_link(user_id: int) -> str:
        calls.append(("create", user_id))
        return "https://simplex.chat/invitation#/?v=2&smp=abc"

    async def connect(link: str) -> str:
        calls.append(("connect", link))
        if "bad" in link:
            raise RuntimeError("invalid link")
        return "invitation"

    sales.bot.address = "https://simplex.chat/contact#/?v=2&smp=xyz"
    sales.bot.api.api_get_active_user = get_user
    sales.bot.api.api_create_link = create_link
    sales.bot.api.api_connect_active_user = connect
    office.employees["accountant"].bot = NS(address=None)

    await client.post("/api/login", json={"password": PASSWORD}, headers=H)
    accounts = {a["id"]: a for a in (await (await client.get("/api/simplex")).json())["accounts"]}
    assert accounts["sales"]["qr"].startswith("data:image/svg+xml") and accounts["accountant"]["qr"] is None
    r = await (await client.post("/api/simplex/sales/invite", json={}, headers=H)).json()
    assert r["link"].startswith("https://simplex.chat/invitation") and r["qr"].startswith(
        "data:image/svg+xml"
    )
    assert (
        await client.post("/api/simplex/sales/connect", json={"link": "simplex:/bad"}, headers=H)
    ).status == 400
    r = await client.post(
        "/api/simplex/sales/connect", json={"link": "https://simplex.chat/contact#x"}, headers=H
    )
    assert (await r.json())["kind"] == "invitation"
    assert calls == [
        ("create", 1),
        ("connect", "simplex:/bad"),
        ("connect", "https://simplex.chat/contact#x"),
    ]


async def test_no_apology_is_sent_when_no_model_is_available(setup):
    office, llm, platforms, _ = setup
    hub = office.hub

    def down(params):
        raise ConnectionError("model API unreachable")

    llm.responses.append(down)
    conv = hub.webhook_inbound("website", {"conversation_id": "v2", "text": "Còn hàng không?"})
    await settle(hub)
    assert platforms.sent == []  # nothing sent to the customer
    conv = hub.inbox.conversation(conv.id)
    assert conv.unread == 1 and hub.inbox.pending_customer_text(conv.id)  # waiting for staff


async def test_zalo_personal_gateway(ui):
    client, office, llm, platforms = ui
    hub = office.hub
    hook = {"X-Hook-Secret": "gw-hook-0123456789"}
    now = ms(datetime.now(UTC))

    def event(mid: str, content: Any, is_self: bool = False, type_: str = "user") -> dict[str, Any]:
        data = {
            "id": mid,
            "type": type_,
            "threadId": "5550001",
            "senderId": "5550001",
            "senderName": "Mai Anh",
            "content": content,
            "timestamp": now,
            "isSelf": is_self,
        }
        return {"event": "message", "account": "zalo-canhan", "data": data}

    # the gateway posts to {WEBHOOK_URL}/{account}; the account must match the channel
    assert (await client.post("/hooks/zalo-canhan/zalo-canhan", json=event("1", "hi"))).status == 401
    assert (await client.post("/hooks/zalo-canhan/other", json=event("1", "hi"), headers=hook)).status == 404
    r = await client.post(
        "/hooks/zalo-canhan/zalo-canhan", json=event("1", "hi", type_="group"), headers=hook
    )
    assert (await r.json())["conversation"] is None  # group chats are ignored

    llm.responses.append(text("Dạ em chào chị Mai Anh ạ."))
    r = await client.post("/hooks/zalo-canhan/zalo-canhan", json=event("100", "Shop ơi"), headers=hook)
    cid = (await r.json())["conversation"]
    await settle(hub)
    assert platforms.sent == [
        ("zalo_personal", {"threadId": "5550001", "message": "Dạ em chào chị Mai Anh ạ."})
    ]
    conv = hub.inbox.conversation(cid)
    assert conv.customer_name == "Mai Anh" and conv.channel == "zalo-canhan"

    # our own reply comes back from the gateway (selfListen) with the id it returned: ignored
    await client.post(
        "/hooks/zalo-canhan", json=event("7001", "Dạ em chào chị Mai Anh ạ.", is_self=True), headers=hook
    )
    assert hub.inbox.conversation(cid).mode == "ai" and len(hub.inbox.messages(cid)) == 2
    # a photo arrives as an attachment; the owner typing on the phone takes the chat over
    llm.responses.append(text("Dạ?"))
    photo = event("101", None)
    photo["data"]["attachment"] = {
        "type": "chat.photo",
        "url": "https://f1.zdn.vn/a.jpg",
        "thumb": "https://f1.zdn.vn/t.jpg",
    }
    await client.post("/hooks/zalo-canhan", json=photo, headers=hook)
    await client.post("/hooks/zalo-canhan", json=event("102", "Chị chờ em chút", is_self=True), headers=hook)
    await settle(hub)
    msgs = hub.inbox.messages(cid)
    assert msgs[2]["attachments"] == [
        {"kind": "image", "url": "https://f1.zdn.vn/a.jpg", "thumb": "https://f1.zdn.vn/t.jpg"}
    ]
    assert msgs[-1]["sender"] == "human"
    assert hub.inbox.conversation(cid).mode == "human"

    await client.post("/api/login", json={"password": PASSWORD}, headers=H)
    r = await (await client.post("/api/channels/zalo-canhan/login", json={}, headers=H)).json()
    assert r == {"state": "qr_pending", "qr": "data:image/png;base64,AAAA"}
    assert (await client.post("/api/channels/website/login", json={}, headers=H)).status == 404
    assert (await client.get("/hooks/zalo-canhan/5550001", headers=hook)).status == 404  # webhook-only


def restart(office: Any, tmp_path: Any, llm: Any, platforms: Platforms) -> Any:
    """A new office process on the same state directory, as after a restart."""
    office.hub.inbox.db.close()
    again = make_office(
        tmp_path, llm, http=platforms.client, channels=[ZALO, ZALO_PERSONAL, FACEBOOK, WEBHOOK]
    )
    fake_chat(again.employees["sales"])
    again.hub.resume_delay = 0
    return again


async def test_messages_that_arrive_while_down_are_answered_after_restart(setup, tmp_path):
    office, llm, platforms, _ = setup
    platforms.fb("m1", "Chào shop", datetime.now(UTC) - timedelta(minutes=30))
    await office.hub.poll_once("fanpage")  # first run: history is only imported
    assert llm.calls == []

    # the office is down; the customer writes; the office comes back
    platforms.fb(
        "m2", "Shop ơi còn hàng không?", datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=5)
    )
    office2 = restart(office, tmp_path, llm, platforms)
    llm.responses.append(text("Dạ còn hàng ạ."))
    await office2.hub.poll_once("fanpage")
    await settle(office2.hub)
    assert platforms.sent[-1][1]["message"]["text"] == "Dạ còn hàng ạ."
    assert "Chào shop\\nShop ơi còn hàng không?" in json.dumps(llm.calls[0]["messages"], ensure_ascii=False)


async def test_waiting_customers_are_answered_at_start_up(setup, tmp_path):
    office, llm, platforms, _ = setup
    hub = office.hub
    # a reply timer that never fired (the process stopped), and an old unanswered message
    hub.schedule_reply = lambda *a, **k: None
    waiting = hub.push_inbound("website", {"conversation_id": "v-new", "text": "Còn hàng không?"})
    old = hub.push_inbound("website", {"conversation_id": "v-old", "text": "Hỏi từ hôm trước"})
    hub.inbox.db.execute(
        "UPDATE conversations SET last_ts=? WHERE id=?", (iso(datetime.now(UTC) - timedelta(days=2)), old.id)
    )
    taken = hub.push_inbound("website", {"conversation_id": "v-human", "text": "Cho gặp người"})
    hub.inbox.set_mode(taken.id, "human")

    office2 = restart(office, tmp_path, llm, platforms)
    llm.responses.append(text("Dạ còn ạ."))
    assert office2.hub.resume_pending() == [waiting.id]  # not the 2-day-old one, not the human one
    await settle(office2.hub)
    assert platforms.sent == [("webhook", {"conversation_id": "v-new", "text": "Dạ còn ạ."})]
    assert office2.hub.resume_pending() == []  # answered: nothing waits any more


async def test_simplex_message_answered_by_catch_up_is_not_answered_twice(setup):
    office, llm, _, chat = setup
    sales, hub = office.employees["sales"], office.hub
    replies: list[str] = []

    async def reply(t: str) -> None:
        replies.append(t)

    contact = {"contactId": 9, "profile": {"displayName": "Dũng"}, "localDisplayName": "dung"}
    conv, mid = hub.simplex_inbound(sales, 9, "Dũng", "Có ai không?")
    llm.responses.append(text("Dạ em đây ạ."))
    assert await hub.reply_ai(conv.id) == "Dạ em đây ạ."  # the start-up catch-up answered it
    assert chat.sent == [(9, "Dạ em đây ạ.")]
    # the bot's own handler for that same message then finds it answered
    await sales._answer(
        NS(chat_info={"contact": contact}, reply=reply), 9, "Dũng", "Có ai không?", conv.id, mid
    )
    assert replies == [] and len(llm.calls) == 1


async def test_attachments_from_every_channel(setup):
    office, llm, platforms, _ = setup
    hub = office.hub
    now = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
    # Zalo OA: a photo with no caption
    platforms.zalo_msgs.insert(
        0,
        {
            "src": 1,
            "message_id": "zp",
            "type": "photo",
            "url": "https://photo.zdn.vn/full.jpg",
            "thumb": "https://photo.zdn.vn/thumb.jpg",
            "time": ms(now),
            "from_id": "u-1",
            "from_display_name": "Hoa",
        },
    )
    # Facebook: a caption with a file
    platforms.fb("mf", "Báo giá giúp em", now)
    platforms.fb_msgs[0]["attachments"] = {
        "data": [
            {"mime_type": "application/pdf", "name": "yeu-cau.pdf", "file_url": "https://cdn.fbsbx.com/y.pdf"}
        ]
    }
    llm.responses += [text("Dạ ảnh gì ạ?"), text("Dạ em xem ạ.")]
    await hub.poll_once("zalo-shop")
    await hub.poll_once("fanpage")
    await settle(hub)

    zalo = hub.inbox.find("zalo-shop", "u-1")
    [m] = [m for m in hub.inbox.messages(zalo.id) if m["sender"] == "customer"]
    assert m["attachments"] == [
        {"kind": "image", "url": "https://photo.zdn.vn/full.jpg", "thumb": "https://photo.zdn.vn/thumb.jpg"}
    ]
    assert zalo.last_preview == "[ảnh]" or hub.inbox.conversation(zalo.id).last_sender == "ai"
    fb = hub.inbox.find("fanpage", "psid-9")
    [m] = [m for m in hub.inbox.messages(fb.id) if m["sender"] == "customer"]
    assert m["text"] == "Báo giá giúp em" and m["attachments"][0]["name"] == "yeu-cau.pdf"
    # the model is told what was sent, and that it cannot see it
    prompts = [json.dumps(c["messages"], ensure_ascii=False) for c in llm.calls]
    assert any("[ảnh]" in p and "không xem được" in p for p in prompts)
    assert any("Báo giá giúp em [tệp: yeu-cau.pdf]" in p for p in prompts)

    # webhook: attachments only, and a non-http URL is dropped
    llm.responses.append(text("ok"))
    conv = hub.push_inbound(
        "website",
        {
            "conversation_id": "a1",
            "attachments": [{"kind": "image", "url": "file:///etc/passwd", "name": "x.png"}],
        },
    )
    assert hub.inbox.messages(conv.id)[0]["attachments"] == [{"kind": "image", "name": "x.png"}]
    with pytest.raises(ValueError):
        hub.push_inbound("website", {"conversation_id": "a2"})
    await settle(hub)


async def test_simplex_images_and_links_reach_the_inbox(setup):
    office, llm, _, _ = setup
    sales, hub = office.employees["sales"], office.hub
    replies: list[str] = []

    async def reply(t: str) -> None:
        replies.append(t)

    contact = {"contactId": 5, "profile": {"displayName": "Bảo"}, "localDisplayName": "bao"}
    image = NS(
        chat_info={"contact": contact},
        chat_item={"chatItem": {"file": {"fileName": "may-loc.jpg", "fileSize": 120000}}},
        content={"type": "image", "text": "Máy này còn không?", "image": "data:image/jpg;base64,/9j/4AAQ"},
        reply=reply,
    )
    llm.responses.append(text("Dạ mẫu này còn ạ."))
    await sales._on_other(image)
    await asyncio.gather(*list(sales._tasks))
    conv = hub.inbox.find("simplex:sales", "5")
    [m, answer] = hub.inbox.messages(conv.id)
    assert m["text"] == "Máy này còn không?"
    assert m["attachments"] == [
        {"kind": "image", "thumb": "data:image/jpg;base64,/9j/4AAQ", "name": "may-loc.jpg"}
    ]
    assert replies == ["Dạ mẫu này còn ạ."] and answer["sender"] == "ai"

    link = NS(
        chat_info={"contact": contact},
        chat_item={"chatItem": {}},
        content={
            "type": "link",
            "text": "Xem giúp https://shop.vn/ma-100",
            "preview": {
                "uri": "https://shop.vn/ma-100",
                "title": "MA-100",
                "image": "data:image/png;base64,iVBO",
            },
        },
        reply=reply,
    )
    llm.responses.append(text("Dạ đây là MA-100 ạ."))
    await sales._on_other(link)
    await asyncio.gather(*list(sales._tasks))
    assert hub.inbox.messages(conv.id)[2]["attachments"][0] == {
        "kind": "link",
        "thumb": "data:image/png;base64,iVBO",
        "url": "https://shop.vn/ma-100",
        "name": "MA-100",
    }
    assert replies[-1] == "Dạ đây là MA-100 ạ."  # link messages are answered, not refused


async def test_attachment_media_is_proxied_safely(ui, monkeypatch):
    client, office, llm, _ = ui
    from ai_employees import media

    async def fake_resolve(host: str) -> list[str]:
        return {"cdn.example": ["93.184.216.34"], "intranet.example": ["10.0.0.5"]}.get(host, [host])

    monkeypatch.setattr(media, "resolve", fake_resolve)
    llm.responses.append(text("ok"))
    files = [
        {"kind": "image", "url": "https://cdn.example/p.jpg"},
        {"kind": "file", "url": "https://cdn.example/page.html", "name": "báo giá.html"},
        {"kind": "image", "url": "https://intranet.example/x.jpg"},
        {"kind": "image", "url": "https://cdn.example/hop"},
    ]
    conv = office.hub.push_inbound("website", {"conversation_id": "m1", "attachments": files})
    await settle(office.hub)
    mid = office.hub.inbox.messages(conv.id)[0]["id"]
    base = f"/api/inbox/{conv.id}/media/{mid}"
    assert (await client.get(f"{base}/0")).status == 401  # needs a login
    await client.post("/api/login", json={"password": PASSWORD}, headers=H)

    r = await client.get(f"{base}/0")
    assert r.status == 200 and r.content_type == "image/jpeg" and await r.read() == b"\xff\xd8jpeg"
    assert r.headers["Cache-Control"] == "private, max-age=3600"
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    # anything that is not a plain image is a download, never rendered in the admin origin
    r = await client.get(f"{base}/1")
    assert r.content_type == "application/octet-stream"
    assert r.headers["Content-Disposition"] == "attachment; filename*=UTF-8''b%C3%A1o%20gi%C3%A1.html"
    # private addresses are refused, also when reached through a redirect
    assert (await client.get(f"{base}/2")).status == 502
    assert (await client.get(f"{base}/3")).status == 502
    assert (await client.get(f"{base}/9")).status == 404
    assert (await client.get(f"/api/inbox/{conv.id}/media/999/0")).status == 404


async def test_staff_accounts_roles_and_channel_scope(ui, tmp_path):
    client, office, llm, _platforms = ui
    hub = office.hub
    llm.responses += [text("a"), text("b")]
    web_conv = hub.push_inbound("website", {"conversation_id": "w1", "customer_name": "Linh", "text": "Hi"})
    zalo_conv = hub.push_inbound(
        "zalo-canhan",
        {
            "event": "message",
            "data": {
                "id": "z1",
                "type": "user",
                "threadId": "t1",
                "content": "Chào",
                "timestamp": ms(datetime.now(UTC)),
            },
        },
    )
    await settle(hub)

    # the owner logs in as before (no username = "admin")
    assert (await client.post("/api/login", json={"password": PASSWORD}, headers=H)).status == 200
    assert (await (await client.get("/api/me")).json())["user"]["role"] == "admin"
    bad = [
        {"username": "admin", "name": "x", "role": "agent", "password": "0123456789"},
        {"username": "Thu Tran", "name": "x", "role": "agent", "password": "0123456789"},
        {"username": "thu", "name": "Thu", "role": "agent", "password": "short"},
        {"username": "thu", "name": "Thu", "role": "boss", "password": "0123456789"},
        {"username": "thu", "name": "Thu", "role": "agent", "password": "0123456789", "channels": ["nope"]},
    ]
    for body in bad:
        assert (await client.post("/api/users", json=body, headers=H)).status == 400, body
    r = await client.post(
        "/api/users",
        json={
            "username": "thu",
            "name": "Thu Trần",
            "role": "agent",
            "password": "thu-pass-2026",
            "channels": ["website"],
        },
        headers=H,
    )
    users = {u["username"]: u for u in (await r.json())["users"]}
    assert users["thu"] == {
        "username": "thu",
        "name": "Thu Trần",
        "role": "agent",
        "channels": ["website"],
        "lang": "",
        "disabled": False,
        "created": users["thu"]["created"],
    }
    stored = json.dumps(office.docs.get("users"))
    assert "thu-pass-2026" not in stored and '"thu"' in stored  # the account is there, hashed
    if office.db is None:
        assert oct(os.stat(tmp_path / "state" / "office.sqlite").st_mode & 0o777) == "0o600"
    await client.post("/api/logout", json={}, headers=H)

    # the sales agent: inbox of their channel only
    assert (
        await client.post("/api/login", json={"username": "thu", "password": "nope-nope-1"}, headers=H)
    ).status == 401
    r = await client.post("/api/login", json={"username": "Thu", "password": "thu-pass-2026"}, headers=H)
    assert (await r.json())["user"]["name"] == "Thu Trần"
    inbox = await (await client.get("/api/inbox")).json()
    assert [c["id"] for c in inbox["conversations"]] == [web_conv.id]
    assert [c["id"] for c in inbox["channels"]] == ["website"]
    assert [c["id"] for c in (await (await client.get("/api/channels")).json())["channels"]] == ["website"]
    assert (await client.get(f"/api/inbox/{zalo_conv.id}")).status == 404
    assert (
        await client.post(f"/api/inbox/{zalo_conv.id}/reply", json={"text": "x"}, headers=H)
    ).status == 404
    for method, path in [
        ("GET", "/api/overview"),
        ("GET", "/api/models"),
        ("GET", "/api/users"),
        ("GET", "/api/approvals"),
        ("GET", "/api/runlog"),
        ("POST", "/api/channels/fanpage/poll"),
        ("PATCH", "/api/employees/sales"),
        ("GET", "/api/simplex"),
    ]:
        r = await client.request(method, path, json={}, headers=H)
        assert r.status == 403, path
    d = await (
        await client.post(
            f"/api/inbox/{web_conv.id}/reply", json={"text": "Dạ em Thu đây", "author": "Giám đốc"}, headers=H
        )
    ).json()
    assert d["messages"][-1]["author"] == "Thu Trần"  # the account, not a typed name

    # own password: needs the current one
    assert (
        await client.post("/api/me/password", json={"old": "wrong", "new": "new-pass-2026"}, headers=H)
    ).status == 400
    assert (
        await client.post(
            "/api/me/password", json={"old": "thu-pass-2026", "new": "new-pass-2026"}, headers=H
        )
    ).status == 200

    # the owner disables the account: its session ends at once
    owner = TestClient(client.server)
    await owner.start_server()
    await owner.post("/api/login", json={"username": "admin", "password": PASSWORD}, headers=H)
    assert (await owner.patch("/api/users/thu", json={"disabled": True}, headers=H)).status == 200
    assert (await client.get("/api/inbox")).status == 401
    assert (
        await client.post("/api/login", json={"username": "thu", "password": "new-pass-2026"}, headers=H)
    ).status == 401
    assert (await owner.patch("/api/users/thu", json={"disabled": False}, headers=H)).status == 200
    assert (
        await client.post("/api/login", json={"username": "thu", "password": "new-pass-2026"}, headers=H)
    ).status == 200
    assert (await owner.delete("/api/users/thu", headers=H)).status == 200
    assert (await client.get("/api/inbox")).status == 401
    assert (await owner.delete("/api/users/thu", headers=H)).status == 404
    await owner.close()


async def test_a_sent_reply_is_kept_even_if_the_platform_reuses_its_id(setup, monkeypatch):
    office, llm, _, _ = setup
    hub = office.hub

    async def send(conversation: str, text: str) -> str:
        return "same-id"

    monkeypatch.setattr(hub.channels["website"], "send", send)
    llm.responses += [text("một"), text("hai")]
    conv = hub.push_inbound("website", {"conversation_id": "r1", "text": "a"})
    await settle(hub)
    hub.push_inbound("website", {"conversation_id": "r1", "text": "b"})
    await settle(hub)
    assert [m["text"] for m in hub.inbox.messages(conv.id)] == ["a", "một", "b", "hai"]


async def test_messenger_official_webhook(ui):
    import hashlib
    import hmac as hm

    client, office, llm, platforms = ui
    hub = office.hub

    def signed(payload: dict) -> tuple[bytes, dict]:
        body = json.dumps(payload).encode()
        sig = "sha256=" + hm.new(b"fb-app-secret", body, hashlib.sha256).hexdigest()
        return body, {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}

    q = {"hub.mode": "subscribe", "hub.verify_token": "verify-me-123", "hub.challenge": "c-42"}
    r = await client.get("/hooks/fanpage", params=q)
    assert r.status == 200 and await r.text() == "c-42"
    assert (await client.get("/hooks/fanpage", params={**q, "hub.verify_token": "nope"})).status == 403

    def event(mid: str, text: str, echo: bool = False) -> dict:
        user, page = {"id": "psid-77"}, {"id": "1000"}
        msg = {"mid": mid, "text": text, **({"is_echo": True} if echo else {})}
        ts = ms(datetime.now(UTC))
        return {
            "object": "page",
            "entry": [
                {
                    "id": "1000",
                    "time": ts,
                    "messaging": [
                        {
                            "sender": page if echo else user,
                            "recipient": user if echo else page,
                            "timestamp": ts,
                            "message": msg,
                        }
                    ],
                }
            ],
        }

    body, headers = signed(event("m_1", "Shop còn hàng MA-100 không?"))
    assert (
        await client.post("/hooks/fanpage", data=body, headers={"Content-Type": "application/json"})
    ).status == 401
    forged = {**headers, "X-Hub-Signature-256": "sha256=" + "0" * 64}
    assert (await client.post("/hooks/fanpage", data=body, headers=forged)).status == 401

    llm.responses.append(text("Dạ còn hàng ạ."))
    r = await client.post("/hooks/fanpage", data=body, headers=headers)
    cid = (await r.json())["conversation"]
    await settle(hub)
    assert platforms.sent[-1] == (
        "facebook",
        {"recipient": {"id": "psid-77"}, "messaging_type": "RESPONSE", "message": {"text": "Dạ còn hàng ạ."}},
    )
    assert hub.inbox.conversation(cid).customer_name == "Tuấn Trần"  # looked up from the Graph API
    # our reply echoed back by Meta: ignored; a page admin typing in Meta Business Suite: takes over
    body, headers = signed(event("m_2", "Dạ còn hàng ạ.", echo=True))
    await client.post("/hooks/fanpage", data=body, headers=headers)
    assert hub.inbox.conversation(cid).mode == "ai" and len(hub.inbox.messages(cid)) == 2
    body, headers = signed(event("m_3", "Em gọi lại cho anh nhé", echo=True))
    await client.post("/hooks/fanpage", data=body, headers=headers)
    assert hub.inbox.conversation(cid).mode == "human"
    # a redelivered event is not answered twice
    body, headers = signed(event("m_1", "Shop còn hàng MA-100 không?"))
    await client.post("/hooks/fanpage", data=body, headers=headers)
    assert len(hub.inbox.messages(cid)) == 3


async def test_zalo_oa_official_webhook(ui):
    import hashlib

    client, office, llm, platforms = ui
    hub = office.hub

    def signed(payload: dict, secret: str = "zalo-oa-secret-key") -> tuple[bytes, dict]:
        body = json.dumps(payload).encode()
        mac = hashlib.sha256(("42" + body.decode() + str(payload["timestamp"]) + secret).encode()).hexdigest()
        return body, {"X-ZEvent-Signature": f"mac={mac}", "Content-Type": "application/json"}

    now = str(ms(datetime.now(UTC)))
    image = {
        "app_id": "42",
        "event_name": "user_send_image",
        "timestamp": now,
        "sender": {"id": "u-555"},
        "recipient": {"id": "oa-1"},
        "message": {
            "msg_id": "zm-1",
            "text": "Máy nhà mình đây",
            "attachments": [
                {
                    "type": "image",
                    "payload": {"url": "https://zdn.vn/a.jpg", "thumbnail": "https://zdn.vn/t.jpg"},
                }
            ],
        },
    }
    body, headers = signed(image, secret="wrong")
    assert (await client.post("/hooks/zalo-shop", data=body, headers=headers)).status == 401
    llm.responses.append(text("Dạ em xem ảnh rồi ạ."))
    body, headers = signed(image)
    r = await client.post("/hooks/zalo-shop", data=body, headers=headers)
    cid = (await r.json())["conversation"]
    await settle(hub)
    [m, _reply] = hub.inbox.messages(cid)
    assert m["attachments"] == [
        {"kind": "image", "url": "https://zdn.vn/a.jpg", "thumb": "https://zdn.vn/t.jpg"}
    ]
    assert platforms.sent[-1] == (
        "zalo",
        {"recipient": {"user_id": "u-555"}, "message": {"text": "Dạ em xem ảnh rồi ạ."}},
    )
    assert hub.inbox.conversation(cid).customer_name == "Hoa Nguyễn"
    follow = {"app_id": "42", "event_name": "follow", "timestamp": now, "follower": {"id": "u-555"}}
    body, headers = signed(follow)
    assert (await (await client.post("/hooks/zalo-shop", data=body, headers=headers)).json())[
        "conversation"
    ] is None
