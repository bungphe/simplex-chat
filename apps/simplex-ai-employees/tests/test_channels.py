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
    "debounce_seconds": 0,
}
FACEBOOK = {
    "id": "fanpage",
    "type": "facebook",
    "employee": "sales",
    "page_id": "1000",
    "access_token_env": "T_FB_TOKEN",
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
        ({"id": "z", "type": "telegram", "employee": "sales"}, "type must be"),
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
    secrets_file = tmp_path / "state" / "channel_secrets.json"
    assert json.loads(secrets_file.read_text())["zalo-shop"] == {
        "access_token": "fresh",
        "refresh_token": "r2",
    }
    assert stat.S_IMODE(os.stat(secrets_file).st_mode) == 0o600

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
    assert (
        await client.post("/hooks/fanpage", json=body, headers=hook)
    ).status == 404  # not a webhook channel
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
        "author": "Thu",
        "text": "Phí ship 30k chị nhé",
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
    # a sticker gets a placeholder; the owner typing on the phone takes the chat over
    llm.responses.append(text("Dạ?"))
    await client.post("/hooks/zalo-canhan", json=event("101", {"type": "sticker"}), headers=hook)
    await client.post("/hooks/zalo-canhan", json=event("102", "Chị chờ em chút", is_self=True), headers=hook)
    await settle(hub)
    msgs = hub.inbox.messages(cid)
    assert msgs[2]["text"].startswith("[khách gửi") and msgs[-1]["sender"] == "human"
    assert hub.inbox.conversation(cid).mode == "human"

    await client.post("/api/login", json={"password": PASSWORD}, headers=H)
    r = await (await client.post("/api/channels/zalo-canhan/login", json={}, headers=H)).json()
    assert r == {"state": "qr_pending", "qr": "data:image/png;base64,AAAA"}
    assert (await client.post("/api/channels/website/login", json={}, headers=H)).status == 404
    assert (await client.get("/hooks/zalo-canhan/5550001", headers=hook)).status == 404  # webhook-only
