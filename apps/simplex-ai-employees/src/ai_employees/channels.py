"""Chat channels outside SimpleX: Zalo OA, Facebook Messenger and generic webhooks
(Telegram, WhatsApp and email are in channels_extra.py).

Each channel polls its platform for new messages (no public URL needed) and can
send a text reply. The Zalo and Facebook calls follow the same endpoints as the
Chat Quality Agent sync adapters (listrecentchat / conversation, Graph API
conversations / messages), plus the platforms' send-message endpoints.

    channels:
      - id: zalo-shop
        type: zalo_oa
        employee: sales
        app_id: "123"
        app_secret_env: ZALO_APP_SECRET
        access_token_env: ZALO_ACCESS_TOKEN
        refresh_token_env: ZALO_REFRESH_TOKEN
      - id: fanpage
        type: facebook
        employee: sales
        page_id: "1000"
        access_token_env: FB_PAGE_TOKEN
      - id: zalo-canhan                          # = the gateway's account id
        type: zalo_personal                      # a personal Zalo account via zalo-gateway/
        employee: sales
        gateway_url: http://zalo-gateway:3000
        api_key_env: ZALO_GATEWAY_KEY            # the gateway's API_SECRET
        secret_env: ZALO_GATEWAY_HOOK_SECRET     # the gateway's WEBHOOK_SECRET
      - id: website
        type: webhook
        employee: sales
        secret_env: WEBCHAT_SECRET              # inbound: POST /hooks/website
        reply_url: https://example.com/reply   # outbound: POST {conversation_id, text}
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

import httpx2

from .actions import expand_env

if TYPE_CHECKING:
    from .hub import ChannelHub

log = logging.getLogger(__name__)

TYPES = ("zalo_oa", "zalo_personal", "facebook", "webhook", "telegram", "whatsapp", "email")
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


class ChannelError(Exception):
    pass


@dataclass(frozen=True)
class ChannelConfig:
    id: str
    type: str
    employee: str
    auto_reply: bool = True
    poll_seconds: float = 30.0
    debounce_seconds: float = 5.0
    options: dict[str, Any] = field(default_factory=dict)

    def opt(self, name: str, default: Any = None) -> Any:
        """An option, or the environment variable named by `<name>_env`."""
        if env := self.options.get(f"{name}_env"):
            return os.environ.get(env) or self.options.get(name, default)
        return self.options.get(name, default)


def parse_channel(raw: dict[str, Any], employees: set[str]) -> ChannelConfig:
    cid = str(raw.get("id") or "")
    if not _ID.match(cid) or cid.startswith("simplex"):
        raise ValueError(f"channel id '{cid}' must be lowercase letters, digits, '-' or '_' (not 'simplex…')")
    ctype = raw.get("type")
    if ctype not in TYPES:
        raise ValueError(f"channel {cid}: type must be one of {TYPES}")
    employee = raw.get("employee")
    if employee not in employees:
        raise ValueError(f"channel {cid}: employee '{employee}' is not declared")
    common = {"id", "type", "employee", "auto_reply", "poll_seconds", "debounce_seconds"}
    options = {k: v for k, v in raw.items() if k not in common}
    required = {
        "zalo_oa": ("app_id",),
        "zalo_personal": ("gateway_url",),
        "facebook": ("page_id",),
        "webhook": (),
        "telegram": ("bot_token|bot_token_env",),
        "whatsapp": ("waha_url",),
        "email": ("smtp_host",),
    }[ctype]
    for r in required:
        if not any(options.get(alt) for alt in r.split("|")):
            raise ValueError(f"channel {cid}: '{r.split('|')[0]}' is required for {ctype}")
    return ChannelConfig(
        id=cid,
        type=ctype,
        employee=employee,
        auto_reply=bool(raw.get("auto_reply", True)),
        poll_seconds=float(raw.get("poll_seconds", 30)),
        debounce_seconds=float(raw.get("debounce_seconds", 5)),
        options=options,
    )


@dataclass
class InboundMessage:
    conversation: str  # the platform's id for this customer / thread
    customer_name: str
    text: str
    sender: str  # "customer" or "agent" (someone answering on the platform itself)
    external_id: str
    ts: datetime
    attachments: list[dict[str, Any]] = field(default_factory=list)


def attachment(kind: str, url: Any = None, thumb: Any = None, name: Any = None) -> dict[str, Any]:
    """One attachment in the inbox's shape; only http(s) URLs are kept."""
    a: dict[str, Any] = {"kind": kind}
    for key, value in (("url", url), ("thumb", thumb)):
        if isinstance(value, str) and value.startswith(("https://", "http://")):
            a[key] = value[:2000]
    if isinstance(name, str) and name.strip():
        a["name"] = name.strip()[:200]
    return a


class Channel:
    type = ""
    push_format = "json"  # or "form" (multipart / urlencoded posts, e.g. inbound email)

    def __init__(self, cfg: ChannelConfig, hub: ChannelHub):
        self.cfg = cfg
        self.hub = hub
        self.id = cfg.id

    @property
    def http(self) -> httpx2.AsyncClient:
        return self.hub.office.http_client

    async def poll(self, since: datetime) -> list[InboundMessage]:
        return []

    async def send(self, conversation: str, text: str) -> str | None:
        """Send text; returns the platform's message id when it gives one."""
        raise NotImplementedError

    # Channels that push messages to POST /hooks/<channel id>

    def secret(self) -> str:
        return ""

    def accepts_push(self) -> bool:
        return bool(self.secret())

    def verify_push(
        self, headers: Mapping[str, str], body: bytes, query: Mapping[str, str] | None = None
    ) -> bool:
        """Is this request really from the platform? Default: a shared X-Hook-Secret."""
        given = headers.get("X-Hook-Secret", "").encode()
        return bool(self.secret()) and hmac.compare_digest(given, self.secret().encode())

    def parse_push(self, payload: dict[str, Any]) -> list[InboundMessage]:
        raise NotImplementedError

    async def lookup_name(self, conversation: str) -> str:
        """The customer's display name, when the platform's events do not carry it."""
        return ""

    async def fetch_media(self, url: str) -> tuple[str, bytes] | None:
        """(content type, bytes) for an attachment only this channel can download
        (with its credentials); None for ordinary public URLs."""
        return None


def _ms(ts: Any) -> datetime:
    return datetime.fromtimestamp(float(ts) / 1000, tz=UTC)


class ZaloOAChannel(Channel):
    """Zalo Official Account. Refresh tokens rotate on every use, so the newest pair
    is kept in the hub's secret store and survives restarts."""

    type = "zalo_oa"
    API_V2 = "https://openapi.zalo.me/v2.0/oa"
    API_V3 = "https://openapi.zalo.me/v3.0/oa"
    OAUTH = "https://oauth.zaloapp.com/v4/oa/access_token"

    def _tokens(self) -> tuple[str, str]:
        saved = self.hub.secrets.get(self.id, {})
        return (
            saved.get("access_token") or self.cfg.opt("access_token", ""),
            saved.get("refresh_token") or self.cfg.opt("refresh_token", ""),
        )

    async def _refresh(self) -> None:
        _, refresh = self._tokens()
        r = await self.http.post(
            self.cfg.opt("oauth_url", self.OAUTH),
            params={
                "refresh_token": refresh,
                "app_id": self.cfg.opt("app_id"),
                "grant_type": "refresh_token",
            },
            headers={
                "secret_key": self.cfg.opt("app_secret", ""),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        data = r.json()
        if not data.get("access_token"):
            raise ChannelError(f"zalo token refresh failed: {data.get('error')} {data.get('message', '')}")
        self.hub.save_secret(self.id, access_token=data["access_token"], refresh_token=data["refresh_token"])
        log.info("%s: zalo access token refreshed", self.id)

    async def _call(
        self, method: str, url: str, params: dict[str, Any] | None = None, body: Any = None
    ) -> Any:
        for attempt in range(2):
            access, _ = self._tokens()
            query = {"data": json.dumps(params)} if params is not None else None
            try:
                r = await self.http.request(
                    method, url, params=query, json=body, headers={"access_token": access}
                )
                data = r.json()
            except (httpx2.HTTPError, ValueError) as e:
                raise ChannelError(f"zalo {url}: {e}") from e
            err = data.get("error", 0)
            if err == -216 and attempt == 0:  # access token expired
                await self._refresh()
                continue
            if err:
                raise ChannelError(f"zalo error {err}: {data.get('message', '')}")
            return data.get("data")
        raise ChannelError("zalo: token refresh did not help")

    async def poll(self, since: datetime) -> list[InboundMessage]:
        v2 = self.cfg.opt("api_v2", self.API_V2)
        out: list[InboundMessage] = []
        for page in range(int(self.cfg.opt("max_pages", 5))):
            convs = await self._call("GET", f"{v2}/listrecentchat", {"offset": page * 10, "count": 10}) or []
            for conv in convs:
                if _ms(conv.get("time", 0)) < since:
                    return out
                oa_sent = conv.get("src") == 0
                user_id = str(conv.get("to_id" if oa_sent else "from_id") or "")
                name = conv.get("to_display_name" if oa_sent else "from_display_name") or ""
                msgs = (
                    await self._call(
                        "GET", f"{v2}/conversation", {"user_id": user_id, "offset": 0, "count": 10}
                    )
                    or []
                )
                for m in msgs:
                    ts = _ms(m.get("time", 0))
                    text = m.get("message") if isinstance(m.get("message"), str) else ""
                    files = self._attachments(m)
                    if ts < since or not (text.strip() or files):
                        continue
                    out.append(
                        InboundMessage(
                            conversation=user_id,
                            customer_name=name,
                            text=text,
                            sender="agent" if m.get("src") == 0 else "customer",
                            external_id=str(m.get("message_id")),
                            ts=ts,
                            attachments=files,
                        )
                    )
            if len(convs) < 10:
                break
        return out

    KINDS: ClassVar[dict[str, str]] = {
        "photo": "image",
        "gif": "image",
        "sticker": "sticker",
        "voice": "audio",
        "audio": "audio",
        "video": "video",
        "file": "file",
        "link": "link",
    }

    def _attachments(self, m: dict[str, Any]) -> list[dict[str, Any]]:
        kind = self.KINDS.get(str(m.get("type", "text")))
        if kind is None:
            return []
        links = m.get("links") if isinstance(m.get("links"), list) else []
        url = m.get("url") or (links[0].get("url") if links and isinstance(links[0], dict) else None)
        return [
            attachment(kind, url, m.get("thumb"), m.get("description") if kind in ("file", "link") else None)
        ]

    # Official webhook (Zalo OA dashboard: Webhook URL = https://<host>/hooks/<channel id>)

    def accepts_push(self) -> bool:
        return bool(self.cfg.opt("webhook_secret"))

    def verify_push(
        self, headers: Mapping[str, str], body: bytes, query: Mapping[str, str] | None = None
    ) -> bool:
        """X-ZEvent-Signature: mac=sha256(app_id + body + timestamp + OA secret key)."""
        secret = self.cfg.opt("webhook_secret", "")
        given = headers.get("X-ZEvent-Signature", "").removeprefix("mac=").strip()
        try:
            timestamp = str(json.loads(body).get("timestamp", ""))
        except (ValueError, AttributeError):
            return False
        data = str(self.cfg.opt("app_id")) + body.decode("utf-8", "replace") + timestamp + secret
        expected = hashlib.sha256(data.encode()).hexdigest()
        return bool(secret and given) and hmac.compare_digest(given.lower(), expected)

    PUSH_KINDS: ClassVar[dict[str, str]] = {
        "image": "image",
        "gif": "image",
        "sticker": "sticker",
        "audio": "audio",
        "voice": "audio",
        "video": "video",
        "file": "file",
        "link": "link",
    }

    def parse_push(self, payload: dict[str, Any]) -> list[InboundMessage]:
        event = str(payload.get("event_name", ""))
        if not event.startswith(("user_send_", "oa_send_")):
            return []  # follows, reactions, seen receipts...
        from_oa = event.startswith("oa_send_")
        user = (payload.get("recipient") if from_oa else payload.get("sender")) or {}
        message = payload.get("message") or {}
        files = []
        for a in message.get("attachments") or []:
            p = a.get("payload") or {}
            files.append(
                attachment(
                    self.PUSH_KINDS.get(str(a.get("type")), "file"),
                    p.get("url"),
                    p.get("thumbnail"),
                    p.get("name"),
                )
            )
        text = message.get("text") if isinstance(message.get("text"), str) else ""
        if not (text or files):
            return []
        return [
            InboundMessage(
                conversation=str(user.get("id") or ""),
                customer_name="",
                text=text[:4000],
                sender="agent" if from_oa else "customer",
                external_id=str(message.get("msg_id") or ""),
                ts=_ms(payload.get("timestamp") or datetime.now(UTC).timestamp() * 1000),
                attachments=files,
            )
        ]

    async def lookup_name(self, conversation: str) -> str:
        v3 = self.cfg.opt("api_v3", self.API_V3)
        data = await self._call("GET", f"{v3}/user/detail", {"user_id": conversation})
        return str((data or {}).get("display_name") or "")[:80]

    async def send(self, conversation: str, text: str) -> str | None:
        v3 = self.cfg.opt("api_v3", self.API_V3)
        data = await self._call(
            "POST",
            f"{v3}/message/cs",
            body={"recipient": {"user_id": conversation}, "message": {"text": text}},
        )
        return str((data or {}).get("message_id") or "") or None


class FacebookChannel(Channel):
    """Facebook Page (Messenger). Conversations are keyed by the customer's page-scoped id."""

    type = "facebook"
    GRAPH = "https://graph.facebook.com/v21.0"

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self.cfg.opt("access_token", "")
        try:
            r = await self.http.get(url, params={**(params or {}), "access_token": token})
            data = r.json()
        except (httpx2.HTTPError, ValueError) as e:
            raise ChannelError(f"facebook: {e}") from e
        if "error" in data:
            raise ChannelError(f"facebook error: {data['error'].get('message', data['error'])}")
        return data

    async def poll(self, since: datetime) -> list[InboundMessage]:
        graph = self.cfg.opt("graph_url", self.GRAPH)
        page_id = str(self.cfg.opt("page_id"))
        data = await self._get(
            f"{graph}/{page_id}/conversations",
            {"fields": "id,updated_time,participants", "platform": "messenger", "limit": 50},
        )
        out: list[InboundMessage] = []
        for conv in data.get("data", []):
            updated = datetime.strptime(conv["updated_time"], "%Y-%m-%dT%H:%M:%S%z")
            if updated < since:
                break  # sorted newest first
            customer = next(
                (p for p in conv.get("participants", {}).get("data", []) if str(p.get("id")) != page_id), {}
            )
            psid, name = str(customer.get("id", "")), customer.get("name", "")
            msgs = await self._get(
                f"{graph}/{conv['id']}/messages",
                {
                    "fields": "id,message,from,created_time,sticker,"
                    "attachments{mime_type,name,image_data,video_data,file_url}",
                    "limit": 25,
                },
            )
            for m in msgs.get("data", []):
                ts = datetime.strptime(m["created_time"], "%Y-%m-%dT%H:%M:%S%z")
                files = self._attachments(m)
                if ts < since or not (m.get("message") or files):
                    continue
                from_page = str(m.get("from", {}).get("id")) == page_id
                out.append(
                    InboundMessage(
                        psid,
                        name,
                        m.get("message") or "",
                        "agent" if from_page else "customer",
                        m["id"],
                        ts,
                        files,
                    )
                )
        return out

    @staticmethod
    def _attachments(m: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for a in (m.get("attachments") or {}).get("data", []):
            mime = str(a.get("mime_type") or "")
            if img := a.get("image_data"):
                out.append(attachment("image", img.get("url"), img.get("preview_url"), a.get("name")))
            elif vid := a.get("video_data"):
                out.append(attachment("video", vid.get("url"), vid.get("preview_url"), a.get("name")))
            else:
                kind = "audio" if mime.startswith("audio/") else "file"
                out.append(attachment(kind, a.get("file_url"), None, a.get("name")))
        if m.get("sticker"):
            out.append(attachment("sticker", m["sticker"], m["sticker"]))
        return out

    # Official webhook (Meta app dashboard: callback URL https://<host>/hooks/<channel id>,
    # verify token = verify_token; subscribe the page to "messages" and "message_echoes")

    def accepts_push(self) -> bool:
        return bool(self.cfg.opt("app_secret"))

    def verify_subscription(self, query: Mapping[str, str]) -> str | None:
        """The challenge to echo when Meta checks the callback URL, or None."""
        token = self.cfg.opt("verify_token", "")
        given = query.get("hub.verify_token", "")
        if (
            query.get("hub.mode") == "subscribe"
            and token
            and hmac.compare_digest(given.encode(), token.encode())
        ):
            return query.get("hub.challenge", "")
        return None

    def verify_push(
        self, headers: Mapping[str, str], body: bytes, query: Mapping[str, str] | None = None
    ) -> bool:
        """X-Hub-Signature-256: sha256=HMAC-SHA256(app secret, raw body)."""
        secret = self.cfg.opt("app_secret", "")
        given = headers.get("X-Hub-Signature-256", "").removeprefix("sha256=")
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return bool(secret and given) and hmac.compare_digest(given, expected)

    def parse_push(self, payload: dict[str, Any]) -> list[InboundMessage]:
        if payload.get("object") != "page":
            return []
        page_id = str(self.cfg.opt("page_id"))
        out = []
        for entry in payload.get("entry") or []:
            for ev in entry.get("messaging") or []:
                m = ev.get("message")
                if not m or m.get("is_deleted"):
                    continue  # deliveries, reads, postbacks...
                echo = bool(m.get("is_echo")) or str((ev.get("sender") or {}).get("id")) == page_id
                user = (ev.get("recipient") if echo else ev.get("sender")) or {}
                files = []
                for a in m.get("attachments") or []:
                    kind = {"image": "image", "video": "video", "audio": "audio", "file": "file"}.get(
                        a.get("type"), "link"
                    )
                    files.append(attachment(kind, (a.get("payload") or {}).get("url"), None, a.get("title")))
                if not (m.get("text") or files):
                    continue
                out.append(
                    InboundMessage(
                        conversation=str(user.get("id") or ""),
                        customer_name="",
                        text=str(m.get("text") or "")[:4000],
                        sender="agent" if echo else "customer",
                        external_id=str(m.get("mid") or ""),
                        ts=_ms(ev.get("timestamp") or datetime.now(UTC).timestamp() * 1000),
                        attachments=files,
                    )
                )
        return out

    async def lookup_name(self, conversation: str) -> str:
        graph = self.cfg.opt("graph_url", self.GRAPH)
        data = await self._get(f"{graph}/{conversation}", {"fields": "name"})
        return str(data.get("name") or "")[:80]

    async def send(self, conversation: str, text: str) -> str | None:
        graph = self.cfg.opt("graph_url", self.GRAPH)
        try:
            r = await self.http.post(
                f"{graph}/me/messages",
                params={"access_token": self.cfg.opt("access_token", "")},
                json={
                    "recipient": {"id": conversation},
                    "messaging_type": "RESPONSE",
                    "message": {"text": text},
                },
            )
            data = r.json()
        except (httpx2.HTTPError, ValueError) as e:
            raise ChannelError(f"facebook send: {e}") from e
        if "error" in data:
            raise ChannelError(f"facebook send error: {data['error'].get('message', data['error'])}")
        return data.get("message_id")


class ZaloPersonalChannel(Channel):
    """A personal Zalo account, through the gateway in zalo-gateway/ (zca-js, QR login;
    adapted from m.agent). The gateway pushes messages to /hooks/<channel id>; the channel
    id is the gateway's account id. zca-js is unofficial: prefer zalo_oa for sales."""

    type = "zalo_personal"

    @property
    def account(self) -> str:
        return str(self.cfg.opt("account") or self.id)

    def secret(self) -> str:
        return self.cfg.opt("secret", "") or ""

    async def _gateway(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        url = f"{str(self.cfg.opt('gateway_url')).rstrip('/')}/{self.account}/api/{path}"
        try:
            r = await self.http.request(
                method, url, json=body, headers={"x-api-key": self.cfg.opt("api_key", "")}
            )
            data = r.json()
        except (httpx2.HTTPError, ValueError) as e:
            raise ChannelError(f"zalo gateway: {e}") from e
        if r.status_code >= 300:
            raise ChannelError(f"zalo gateway: HTTP {r.status_code} {data.get('error', '')}")
        return data

    def parse_push(self, payload: dict[str, Any]) -> list[InboundMessage]:
        d = payload.get("data") or {}
        if payload.get("event") != "message" or d.get("type") != "user":
            return []  # group chats are not handled
        text = d.get("content") if isinstance(d.get("content"), str) else ""
        files = []
        if isinstance(a := d.get("attachment"), dict):
            files.append(
                attachment(
                    self.KINDS.get(str(a.get("type")), "file"), a.get("url"), a.get("thumb"), a.get("name")
                )
            )
        elif not text.strip():
            files.append(attachment("file"))  # an attachment the gateway could not describe
        return [
            InboundMessage(
                conversation=str(d.get("threadId") or ""),
                customer_name="" if d.get("isSelf") else str(d.get("senderName") or "")[:80],
                text=text[:4000],
                sender="agent" if d.get("isSelf") else "customer",
                external_id=str(d.get("id") or ""),
                ts=_ms(d.get("timestamp") or datetime.now(UTC).timestamp() * 1000),
                attachments=files,
            )
        ]

    # zca-js msgType values
    KINDS: ClassVar[dict[str, str]] = {
        "chat.photo": "image",
        "chat.gif": "image",
        "chat.sticker": "sticker",
        "chat.voice": "audio",
        "chat.video.msg": "video",
        "share.file": "file",
        "chat.recommended": "link",
        "chat.link": "link",
    }

    async def send(self, conversation: str, text: str) -> str | None:
        data = await self._gateway("POST", "send-message", {"threadId": conversation, "message": text})
        return str(data.get("message_id") or "") or None

    async def login(self) -> dict[str, Any]:
        """Start the account on the gateway; returns {state, qr} for the admin page."""
        await self._gateway("POST", "init", {})
        return await self._gateway("GET", "qr")


class WebhookChannel(Channel):
    """Any other platform, through your own bridge (n8n, a website chat widget, a bot):
    it POSTs customer messages to /hooks/<channel id>, and replies are POSTed to reply_url."""

    type = "webhook"

    def secret(self) -> str:
        return self.cfg.opt("secret", "") or ""

    def parse_push(self, payload: dict[str, Any]) -> list[InboundMessage]:
        conv_key, text = str(payload.get("conversation_id") or ""), str(payload.get("text") or "").strip()
        raw = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
        kinds = ("image", "video", "audio", "file", "sticker", "link")
        files = [
            attachment(
                a.get("kind") if a.get("kind") in kinds else "file",
                a.get("url"),
                a.get("thumb"),
                a.get("name"),
            )
            for a in raw[:10]
            if isinstance(a, dict)
        ]
        if not conv_key or not (text or files):
            raise ValueError("conversation_id and text (or attachments) are required")
        return [
            InboundMessage(
                conversation=conv_key,
                customer_name=str(payload.get("customer_name") or "")[:80],
                text=text[:4000],
                sender="customer",
                external_id=str(payload.get("message_id") or uuid.uuid4()),
                ts=datetime.now(UTC),
                attachments=files,
            )
        ]

    async def send(self, conversation: str, text: str) -> str | None:
        url = self.cfg.opt("reply_url")
        if not url:
            return None  # replies are only visible in the inbox (and to a bridge that polls it)
        headers = {k: expand_env(str(v)) for k, v in (self.cfg.options.get("reply_headers") or {}).items()}
        try:
            r = await self.http.post(
                expand_env(url), json={"conversation_id": conversation, "text": text}, headers=headers
            )
        except httpx2.HTTPError as e:
            raise ChannelError(f"webhook reply: {e}") from e
        if r.status_code >= 300:
            raise ChannelError(f"webhook reply: HTTP {r.status_code}")
        try:
            return str(r.json().get("message_id") or "") or None
        except ValueError:
            return None


def make_channel(cfg: ChannelConfig, hub: ChannelHub) -> Channel:
    from .channels_extra import EmailChannel, TelegramChannel, WhatsAppChannel

    classes: dict[str, type[Channel]] = {
        "telegram": TelegramChannel,
        "whatsapp": WhatsAppChannel,
        "email": EmailChannel,
        "zalo_oa": ZaloOAChannel,
        "zalo_personal": ZaloPersonalChannel,
        "facebook": FacebookChannel,
        "webhook": WebhookChannel,
    }
    return classes[cfg.type](cfg, hub)
