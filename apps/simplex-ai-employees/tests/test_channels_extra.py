"""Telegram, WhatsApp (WAHA) and email channels: verified webhooks in, AI replies out."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx2
import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from test_channels import settle

from ai_employees.config import ConfigError, parse_config
from ai_employees.web import CSRF_HEADER, CSRF_VALUE, create_app

from fakes import ScriptedLLM, make_office, text

PASSWORD = "correct horse battery staple"
H = {CSRF_HEADER: CSRF_VALUE}
TOKEN = "123456:TEST-bot-token"

TELEGRAM = {
    "id": "telegram",
    "type": "telegram",
    "employee": "sales",
    "bot_token_env": "T_TG_TOKEN",
    "secret_env": "T_TG_SECRET",
    "public_url": "https://shop.example",
    "debounce_seconds": 0,
}
WHATSAPP = {
    "id": "whatsapp",
    "type": "whatsapp",
    "employee": "sales",
    "waha_url": "http://waha.local:3000",
    "api_key_env": "T_WAHA_KEY",
    "hmac_key_env": "T_WAHA_HMAC",
    "debounce_seconds": 0,
}
EMAIL = {
    "id": "email",
    "type": "email",
    "employee": "sales",
    "secret_env": "T_MAIL_KEY",
    "smtp_host": "smtp.example",
    "smtp_user": "support@shop.example",
    "smtp_password_env": "T_SMTP_PASSWORD",
    "smtp_from": "Shop <support@shop.example>",
    "debounce_seconds": 0,
}


class Platforms:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle))

    def handle(self, r: httpx2.Request) -> httpx2.Response:
        url = r.url
        if url.host == "api.telegram.org":
            if url.path.startswith(f"/file/bot{TOKEN}/"):
                return httpx2.Response(200, content=b"\xff\xd8tg", headers={"content-type": "image/jpeg"})
            if not url.path.startswith(f"/bot{TOKEN}/"):
                return httpx2.Response(401, json={"ok": False, "description": "Unauthorized"})
            method, body = url.path.rsplit("/", 1)[1], json.loads(r.content)
            self.sent.append(("telegram:" + method, body))
            if method == "sendMessage":
                return httpx2.Response(200, json={"ok": True, "result": {"message_id": 900 + len(self.sent)}})
            if method == "getFile":
                return httpx2.Response(200, json={"ok": True, "result": {"file_path": "photos/f.jpg"}})
            if method == "setWebhook":
                return httpx2.Response(200, json={"ok": True, "result": True})
        if url.host == "waha.local":
            if r.headers.get("X-Api-Key") != "waha-key":
                return httpx2.Response(401, json={"error": "unauthorized"})
            if url.path == "/api/sendText":
                self.sent.append(("whatsapp", json.loads(r.content)))
                return httpx2.Response(201, json={"id": {"_serialized": "true_84901@c.us_OUT1"}})
            if url.path == "/api/files/default/m.jpg":
                return httpx2.Response(200, content=b"\xff\xd8wa", headers={"content-type": "image/jpeg"})
        return httpx2.Response(404, json={"error": "unexpected " + str(url)})


@pytest.fixture
async def ui(tmp_path, monkeypatch):
    for k, v in {
        "T_TG_TOKEN": TOKEN,
        "T_TG_SECRET": "tg-secret-0123456789",
        "T_WAHA_KEY": "waha-key",
        "T_WAHA_HMAC": "waha-hmac-0123456789",
        "T_MAIL_KEY": "mail-key-0123456789",
        "T_SMTP_PASSWORD": "smtp-pass",
    }.items():
        monkeypatch.setenv(k, v)
    llm, platforms = ScriptedLLM(), Platforms()
    office = make_office(tmp_path, llm, http=platforms.client, channels=[TELEGRAM, WHATSAPP, EMAIL])
    client = TestClient(TestServer(create_app(office, PASSWORD)))
    await client.start_server()
    yield client, office, llm, platforms
    await client.close()


def test_required_options(tmp_path):
    base = {
        "state_dir": str(tmp_path),
        "employees": [
            {"id": "sales", "display_name": "Lan", "system_prompt": "x", "db": str(tmp_path / "db")}
        ],
    }
    for ch, missing in (
        ({"id": "tg", "type": "telegram", "employee": "sales"}, "bot_token"),
        ({"id": "wa", "type": "whatsapp", "employee": "sales"}, "waha_url"),
        ({"id": "mail", "type": "email", "employee": "sales"}, "smtp_host"),
    ):
        with pytest.raises(ConfigError, match=missing):
            parse_config({**base, "channels": [ch]}, tmp_path)
    ok = {"id": "tg", "type": "telegram", "employee": "sales", "bot_token_env": "X"}
    assert parse_config({**base, "channels": [ok]}, tmp_path).channels[0].type == "telegram"


def tg_update(update_id: int, text: str | None = None, chat_type: str = "private", **extra: Any) -> dict:
    message = {
        "message_id": update_id,
        "date": int(datetime.now(UTC).timestamp()),
        "chat": {"id": 5551, "type": chat_type},
        "from": {"id": 5551, "first_name": "Anna", "last_name": "Müller"},
        **({"text": text} if text else {}),
        **extra,
    }
    return {"update_id": update_id, "message": message}


async def test_telegram(ui, caplog):
    client, office, llm, platforms = ui
    hub = office.hub
    hook = {"X-Telegram-Bot-Api-Secret-Token": "tg-secret-0123456789"}

    assert (await client.post("/hooks/telegram", json=tg_update(1, "hi"))).status == 401
    bad = {"X-Telegram-Bot-Api-Secret-Token": "nope"}
    assert (await client.post("/hooks/telegram", json=tg_update(1, "hi"), headers=bad)).status == 401
    # group chats are not answered
    r = await client.post("/hooks/telegram", json=tg_update(2, "hello all", chat_type="group"), headers=hook)
    assert (await r.json())["conversation"] is None

    llm.responses.append(text("Hello Anna! Yes, it is in stock."))
    r = await client.post("/hooks/telegram", json=tg_update(3, "Is MA-100 in stock?"), headers=hook)
    cid = (await r.json())["conversation"]
    await client.post("/hooks/telegram", json=tg_update(3, "Is MA-100 in stock?"), headers=hook)  # redelivery
    await settle(hub)
    assert platforms.sent == [
        ("telegram:sendMessage", {"chat_id": "5551", "text": "Hello Anna! Yes, it is in stock."})
    ]
    conv = hub.inbox.conversation(cid)
    assert conv.customer_name == "Anna Müller" and conv.external_id == "5551"
    assert [m["sender"] for m in hub.inbox.messages(cid)] == ["customer", "ai"]

    # a photo: stored as telegram:<file id>, downloaded with the bot token only by the server
    photo = [{"file_id": "small", "file_size": 10}, {"file_id": "big", "file_size": 999}]
    llm.responses.append(text("Nice!"))
    await client.post(
        "/hooks/telegram", json=tg_update(4, None, photo=photo, caption="this one"), headers=hook
    )
    await settle(hub)
    m = hub.inbox.messages(cid)[-2]
    assert m["text"] == "this one" and m["attachments"] == [{"kind": "image", "url": "telegram:big"}]
    await client.post("/api/login", json={"password": PASSWORD}, headers=H)
    caplog.set_level(logging.INFO)
    r = await client.get(f"/api/inbox/{cid}/media/{m['id']}/0")
    assert r.status == 200 and await r.read() == b"\xff\xd8tg"
    assert ("telegram:getFile", {"file_id": "big"}) in platforms.sent

    # registering the webhook from the admin UI
    r = await client.post("/api/channels/telegram/webhook", json={}, headers=H)
    assert (await r.json())["url"] == "https://shop.example/hooks/telegram"
    assert platforms.sent[-1] == (
        "telegram:setWebhook",
        {
            "url": "https://shop.example/hooks/telegram",
            "secret_token": "tg-secret-0123456789",
            "allowed_updates": ["message"],
        },
    )
    chs = {c["id"]: c for c in (await (await client.get("/api/channels")).json())["channels"]}
    assert chs["telegram"]["type"] == "telegram" and chs["telegram"]["state"]["webhook"].endswith(
        "/hooks/telegram"
    )
    assert (await client.post("/api/channels/whatsapp/webhook", json={}, headers=H)).status == 404
    # the bot token never reaches the logs or the UI
    assert TOKEN not in caplog.text and TOKEN not in json.dumps(chs)


async def test_telegram_errors_hide_the_token(ui):
    _client, office, _llm, platforms = ui
    from ai_employees.channels import ChannelError

    ch = office.hub.channels["telegram"]

    def boom(r: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused for " + str(r.url))

    platforms.client._transport = httpx2.MockTransport(boom)
    with pytest.raises(ChannelError) as e:
        await ch.send("5551", "hi")
    assert TOKEN not in str(e.value)


def waha(event: str, payload: dict, key: str = "waha-hmac-0123456789") -> tuple[bytes, dict]:
    body = json.dumps({"event": event, "session": "default", "payload": payload}).encode()
    mac = hmac.new(key.encode(), body, hashlib.sha512).hexdigest()
    return body, {
        "X-Webhook-Hmac": mac,
        "X-Webhook-Hmac-Algorithm": "sha512",
        "Content-Type": "application/json",
    }


async def test_whatsapp(ui):
    client, office, llm, platforms = ui
    hub = office.hub
    now = int(datetime.now(UTC).timestamp())
    msg = {
        "id": "false_84901@c.us_A1",
        "from": "84901@c.us",
        "fromMe": False,
        "body": "Còn hàng không shop?",
        "timestamp": now,
        "_data": {"notifyName": "Bình"},
    }

    body, headers = waha("message", msg, key="wrong")
    assert (await client.post("/hooks/whatsapp", data=body, headers=headers)).status == 401
    for chat in ("120363@g.us", "status@broadcast"):
        body, headers = waha("message", {**msg, "id": "x", "from": chat})
        assert (await (await client.post("/hooks/whatsapp", data=body, headers=headers)).json())[
            "conversation"
        ] is None

    llm.responses.append(text("Dạ còn hàng ạ."))
    body, headers = waha("message", msg)
    cid = (await (await client.post("/hooks/whatsapp", data=body, headers=headers)).json())["conversation"]
    await settle(hub)
    assert platforms.sent == [
        ("whatsapp", {"session": "default", "chatId": "84901@c.us", "text": "Dạ còn hàng ạ."})
    ]
    assert hub.inbox.conversation(cid).customer_name == "Bình"

    # the echo of our own reply is not a human takeover; a message typed on the phone is
    body, headers = waha(
        "message.any",
        {
            "id": "true_84901@c.us_OUT1",
            "from": "me",
            "to": "84901@c.us",
            "fromMe": True,
            "body": "Dạ còn hàng ạ.",
            "timestamp": now,
        },
    )
    await client.post("/hooks/whatsapp", data=body, headers=headers)
    assert hub.inbox.conversation(cid).mode == "ai"
    body, headers = waha(
        "message.any",
        {
            "id": "true_84901@c.us_P2",
            "to": "84901@c.us",
            "fromMe": True,
            "body": "Anh ơi em gọi lại nhé",
            "timestamp": now + 5,
        },
    )
    await client.post("/hooks/whatsapp", data=body, headers=headers)
    assert hub.inbox.conversation(cid).mode == "human"
    assert hub.inbox.messages(cid)[-1]["sender"] == "human"

    # media served by WAHA (internal address, API key) comes through the server
    media = {"url": "http://waha.local:3000/api/files/default/m.jpg", "mimetype": "image/jpeg"}
    body, headers = waha(
        "message",
        {
            **msg,
            "id": "false_84901@c.us_A3",
            "body": "",
            "hasMedia": True,
            "media": media,
            "timestamp": now + 9,
        },
    )
    await client.post("/hooks/whatsapp", data=body, headers=headers)
    m = hub.inbox.messages(cid)[-1]
    assert m["attachments"] == [{"kind": "image", "url": media["url"]}]
    await client.post("/api/login", json={"password": PASSWORD}, headers=H)
    r = await client.get(f"/api/inbox/{cid}/media/{m['id']}/0")
    assert r.status == 200 and await r.read() == b"\xff\xd8wa"


def inbound_mail(**fields: str) -> FormData:
    form = FormData()
    defaults = {
        "from": "Chị Mai <mai@example.vn>",
        "to": "support@shop.example",
        "subject": "Hỏi giá máy lọc nước",
        "text": "Chào shop, máy MA-100 giá bao nhiêu?\n\nOn Mon, 1 Jan 2026 Shop wrote:\n> old text",
        "headers": "Message-ID: <abc@example.vn>\nFrom: mai@example.vn\n",
        "SPF": "pass",  # SendGrid's checks of the sender
    }
    for k, v in {**defaults, **fields}.items():
        form.add_field(k, v)
    return form


async def test_email(ui, monkeypatch):
    client, office, llm, _platforms = ui
    hub = office.hub
    ch = hub.channels["email"]
    outbox: list[Any] = []
    monkeypatch.setattr(type(ch), "_smtp_send", lambda self, msg: outbox.append(msg))

    assert (await client.post("/hooks/email", data=inbound_mail())).status == 401
    assert (await client.post("/hooks/email?key=nope", data=inbound_mail())).status == 401

    llm.responses.append(text("Dạ máy MA-100 giá 4.500.000đ ạ."))
    r = await client.post("/hooks/email?key=mail-key-0123456789", data=inbound_mail())
    assert r.status == 200
    cid = (await r.json())["conversation"]
    await settle(hub)
    [m, _reply] = hub.inbox.messages(cid)
    assert m["text"] == "Hỏi giá máy lọc nước\n\nChào shop, máy MA-100 giá bao nhiêu?"  # quote stripped
    conv = hub.inbox.conversation(cid)
    assert conv.external_id == "mai@example.vn" and conv.customer_name == "Chị Mai"
    [sent] = outbox
    assert sent["To"] == "mai@example.vn" and sent["Subject"] == "Re: Hỏi giá máy lọc nước"
    assert sent["In-Reply-To"] == "<abc@example.vn>" and sent["References"] == "<abc@example.vn>"
    assert sent["Auto-Submitted"] == "auto-replied" and sent["From"] == "Shop <support@shop.example>"
    assert sent.get_content().strip() == "Dạ máy MA-100 giá 4.500.000đ ạ."

    # basic auth works too; attachments are read past (bigger than the app's JSON limit), names kept
    form = inbound_mail(
        subject="Re: Hỏi giá máy lọc nước",
        text="Đây là ảnh phòng nhà em",
        headers="Message-ID: <def@example.vn>\nReferences: <abc@example.vn>\n",
    )
    form.add_field(
        "attachment-info", json.dumps({"attachment1": {"filename": "phong.jpg", "type": "image/jpeg"}})
    )
    form.add_field("attachment1", b"\xff" * (600 * 1024), filename="phong.jpg", content_type="image/jpeg")
    auth = {"Authorization": "Basic " + base64.b64encode(b"inbound:mail-key-0123456789").decode()}
    llm.responses.append(text("Dạ em nhận được ảnh rồi ạ."))
    r = await client.post("/hooks/email", data=form, headers=auth)
    assert r.status == 200 and (await r.json())["conversation"] == cid
    await settle(hub)
    m = hub.inbox.messages(cid)[2]
    assert m["text"] == "Đây là ảnh phòng nhà em"  # a reply: no subject repeated
    assert m["attachments"] == [{"kind": "image", "name": "phong.jpg"}]
    assert outbox[-1]["References"] == "<abc@example.vn> <def@example.vn>"

    # auto-replies, bounces and mailing lists are never answered (no mail loops)
    for fields in (
        {"headers": "Auto-Submitted: auto-replied\nMessage-ID: <x1@e>\n"},
        {"headers": "Precedence: bulk\nMessage-ID: <x2@e>\n"},
        {"headers": "List-Id: <news.example>\nMessage-ID: <x3@e>\n"},
        {"from": "MAILER-DAEMON@example.vn", "headers": "Message-ID: <x4@e>\n"},
    ):
        r = await client.post("/hooks/email?key=mail-key-0123456789", data=inbound_mail(**fields))
        assert (await r.json())["conversation"] is None
    assert len(outbox) == 2
