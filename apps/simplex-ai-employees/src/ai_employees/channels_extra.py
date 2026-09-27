"""Telegram, WhatsApp (through WAHA) and email channels.

Rebuilt from m.agent's adapters (telegram_adapter, whatsapp_adapter, email_adapter),
with every inbound request verified and no credentials in URLs the staff can see:

    channels:
      - id: telegram
        type: telegram
        employee: sales
        bot_token_env: TELEGRAM_BOT_TOKEN      # from @BotFather
        secret_env: TELEGRAM_WEBHOOK_SECRET    # Telegram sends it back on every update
        public_url: https://shop.example.com   # for "register webhook" in the admin UI
      - id: whatsapp
        type: whatsapp                         # a WAHA server (github.com/devlikeapro/waha)
        employee: sales
        waha_url: http://waha:3000
        session: default
        api_key_env: WAHA_API_KEY
        hmac_key_env: WAHA_WEBHOOK_HMAC        # WAHA webhook "hmac.key" (or secret_env + a custom header)
      - id: email
        type: email
        employee: sales
        secret_env: EMAIL_INBOUND_KEY          # SendGrid Inbound Parse URL: /hooks/email?key=...
        smtp_host: smtp.example.com
        smtp_user: support@example.com
        smtp_password_env: SMTP_PASSWORD
        smtp_from: "Shop Minh An <support@example.com>"
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import smtplib
import ssl
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from email.message import EmailMessage
from email.parser import HeaderParser
from email.utils import make_msgid, parseaddr
from typing import Any

import httpx2

from .channels import Channel, ChannelError, InboundMessage


class _HideBotToken(logging.Filter):
    """HTTP client request logs print full URLs, and Telegram's hold the bot token."""

    TOKEN = re.compile(r"/bot\d+:[\w-]+")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "/bot" in message:
            record.msg, record.args = self.TOKEN.sub("/bot<token>", message), None
        return True


for _name in ("httpx", "httpx2"):
    logging.getLogger(_name).addFilter(_HideBotToken())


def _secs(ts: Any) -> datetime:
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Telegram


class TelegramChannel(Channel):
    """A Telegram bot. Private chats only (the AI does not answer in groups)."""

    type = "telegram"
    API = "https://api.telegram.org"

    @property
    def _api(self) -> str:
        return str(self.cfg.opt("api_url", self.API)).rstrip("/")

    @property
    def _token(self) -> str:
        token = self.cfg.opt("bot_token", "")
        if not token:
            raise ChannelError("telegram: bot_token is not set")
        return str(token)

    def secret(self) -> str:
        return self.cfg.opt("secret", "") or ""

    def verify_push(
        self, headers: Mapping[str, str], body: bytes, query: Mapping[str, str] | None = None
    ) -> bool:
        given = headers.get("X-Telegram-Bot-Api-Secret-Token", "").encode()
        return bool(self.secret()) and hmac.compare_digest(given, self.secret().encode())

    async def _call(self, method: str, payload: dict[str, Any]) -> Any:
        try:
            r = await self.http.post(f"{self._api}/bot{self._token}/{method}", json=payload)
            data = r.json()
        except (httpx2.HTTPError, ValueError) as e:
            raise ChannelError(
                f"telegram {method}: {type(e).__name__}"
            ) from None  # no URL: it holds the token
        if not data.get("ok"):
            raise ChannelError(f"telegram {method}: {data.get('description', 'error')}")
        return data.get("result")

    def parse_push(self, payload: dict[str, Any]) -> list[InboundMessage]:
        m = payload.get("message")
        if not isinstance(m, dict) or (m.get("chat") or {}).get("type") != "private":
            return []  # edits, channel posts, groups, button presses...
        user = m.get("from") or {}
        name = (
            " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
            or user.get("username")
            or ""
        )
        files: list[dict[str, Any]] = []
        if photos := m.get("photo"):
            best = max(photos, key=lambda p: p.get("file_size", 0) or p.get("width", 0))
            files.append({"kind": "image", "url": f"telegram:{best.get('file_id')}"})
        for key, kind in (("document", "file"), ("video", "video"), ("voice", "audio"), ("audio", "audio")):
            if isinstance(f := m.get(key), dict):
                a = {"kind": kind, "url": f"telegram:{f.get('file_id')}"}
                if f.get("file_name"):
                    a["name"] = str(f["file_name"])[:200]
                files.append(a)
        if isinstance(s := m.get("sticker"), dict):
            files.append({"kind": "sticker", **({"name": s["emoji"]} if s.get("emoji") else {})})
        text = str(m.get("text") or m.get("caption") or "")
        if not (text or files):
            return []
        chat_id = str((m.get("chat") or {}).get("id"))
        return [
            InboundMessage(
                conversation=chat_id,
                customer_name=name[:80],
                text=text[:4000],
                sender="customer",
                external_id=f"{chat_id}:{m.get('message_id')}",
                ts=_secs(m.get("date")),
                attachments=files,
            )
        ]

    async def send(self, conversation: str, text: str) -> str | None:
        # plain text (no parse_mode): customer-visible text is never interpreted as markup
        result = await self._call("sendMessage", {"chat_id": conversation, "text": text})
        return f"{conversation}:{(result or {}).get('message_id')}"

    async def register_webhook(self) -> str:
        """Point the bot at /hooks/<channel id> (needs public_url, served over HTTPS)."""
        base = str(self.cfg.opt("public_url") or "").rstrip("/")
        if not base.startswith("https://"):
            raise ChannelError("telegram: set public_url to this server's public https:// address")
        url = f"{base}/hooks/{self.id}"
        await self._call(
            "setWebhook", {"url": url, "secret_token": self.secret(), "allowed_updates": ["message"]}
        )
        return url

    async def fetch_media(self, url: str) -> tuple[str, bytes] | None:
        if not url.startswith("telegram:"):
            return None
        info = await self._call("getFile", {"file_id": url.removeprefix("telegram:")})
        path = (info or {}).get("file_path")
        if not path:
            raise ChannelError("telegram: file not available")
        r = await self.http.get(f"{self._api}/file/bot{self._token}/{path}", timeout=30)
        if r.status_code != 200:
            raise ChannelError(f"telegram file: HTTP {r.status_code}")
        return r.headers.get("content-type", "").split(";")[0].strip().lower(), r.content[: 15 * 1024 * 1024]


# --------------------------------------------------------------------------- #
# WhatsApp through WAHA


class WhatsAppChannel(Channel):
    """WhatsApp via a WAHA server. Groups and status updates are ignored."""

    type = "whatsapp"

    @property
    def _base(self) -> str:
        return str(self.cfg.opt("waha_url") or "").rstrip("/")

    def _headers(self) -> dict[str, str]:
        key = self.cfg.opt("api_key", "")
        return {"X-Api-Key": str(key)} if key else {}

    def secret(self) -> str:
        return self.cfg.opt("secret", "") or ""

    def accepts_push(self) -> bool:
        return bool(self.cfg.opt("hmac_key") or self.secret())

    def verify_push(
        self, headers: Mapping[str, str], body: bytes, query: Mapping[str, str] | None = None
    ) -> bool:
        if key := self.cfg.opt("hmac_key", ""):
            expected = hmac.new(str(key).encode(), body, hashlib.sha512).hexdigest()
            return hmac.compare_digest(headers.get("X-Webhook-Hmac", "").lower(), expected)
        return super().verify_push(headers, body)  # a custom X-Hook-Secret header

    def parse_push(self, payload: dict[str, Any]) -> list[InboundMessage]:
        if payload.get("event") not in ("message", "message.any"):
            return []
        m = payload.get("payload") or {}
        mine = bool(m.get("fromMe"))
        chat = str(m.get("to") if mine else m.get("from") or "")
        if not chat or chat.endswith(("@g.us", "@newsletter")) or chat == "status@broadcast":
            return []
        data = m.get("_data") or {}
        name = "" if mine else str(m.get("pushName") or data.get("notifyName") or data.get("pushName") or "")
        files = []
        if m.get("hasMedia"):
            media = m.get("media") or {}
            mime = str(media.get("mimetype") or "")
            kind = (
                "image"
                if mime.startswith("image/")
                else "video"
                if mime.startswith("video/")
                else ("audio" if mime.startswith("audio/") else "file")
            )
            a: dict[str, Any] = {"kind": kind}
            if isinstance(media.get("url"), str) and media["url"].startswith(("http://", "https://")):
                a["url"] = media["url"][:2000]
            if media.get("filename"):
                a["name"] = str(media["filename"])[:200]
            files.append(a)
        text = str(m.get("body") or "")
        if not (text or files):
            return []
        return [
            InboundMessage(
                conversation=chat,
                customer_name=name[:80],
                text=text[:4000],
                sender="agent" if mine else "customer",
                external_id=str(m.get("id") or uuid.uuid4()),
                ts=_secs(m.get("timestamp")),
                attachments=files,
            )
        ]

    async def send(self, conversation: str, text: str) -> str | None:
        body = {"session": self.cfg.opt("session", "default"), "chatId": conversation, "text": text}
        try:
            r = await self.http.post(f"{self._base}/api/sendText", json=body, headers=self._headers())
        except httpx2.HTTPError as e:
            raise ChannelError(f"whatsapp: {type(e).__name__} {e}") from None
        if r.status_code >= 300:
            raise ChannelError(f"whatsapp: HTTP {r.status_code} {r.text[:200]}")
        try:
            mid = r.json().get("id")
        except ValueError:
            return None
        if isinstance(mid, dict):
            mid = mid.get("_serialized") or mid.get("id")
        return str(mid) if mid else None

    async def fetch_media(self, url: str) -> tuple[str, bytes] | None:
        """WAHA serves media itself (often on the internal network), with its API key."""
        if not self._base or not url.startswith(self._base + "/"):
            return None
        r = await self.http.get(url, headers=self._headers(), timeout=30)
        if r.status_code != 200:
            raise ChannelError(f"whatsapp media: HTTP {r.status_code}")
        return r.headers.get("content-type", "").split(";")[0].strip().lower(), r.content[: 15 * 1024 * 1024]


# --------------------------------------------------------------------------- #
# Email: inbound through SendGrid Inbound Parse (multipart form), replies over SMTP


_QUOTE_START = re.compile(
    r"^(On .{5,200} wrote:|Vào .{5,200} đã viết:|-----Original Message-----|>+ ?)", re.MULTILINE
)
_NOREPLY = re.compile(r"^(no-?reply|mailer-daemon|postmaster|bounce)", re.IGNORECASE)


def _strip_quote(text: str) -> str:
    """The customer's new text, without the quoted earlier emails below it."""
    m = _QUOTE_START.search(text)
    return (text[: m.start()] if m and m.start() > 0 else text).strip()


class EmailChannel(Channel):
    type = "email"
    push_format = "form"

    def secret(self) -> str:
        return self.cfg.opt("secret", "") or ""

    def verify_push(
        self, headers: Mapping[str, str], body: bytes, query: Mapping[str, str] | None = None
    ) -> bool:
        """SendGrid Inbound Parse cannot sign: the key is in the URL (?key=) or basic auth."""
        secret = self.secret().encode()
        given = (query or {}).get("key", "")
        auth = headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                given = base64.b64decode(auth[6:]).decode().partition(":")[2] or given
            except (ValueError, UnicodeDecodeError):
                return False
        return bool(secret) and hmac.compare_digest(given.encode(), secret)

    def parse_push(self, payload: dict[str, Any]) -> list[InboundMessage]:
        raw_headers = HeaderParser().parsestr(str(payload.get("headers") or ""))
        auto = str(raw_headers.get("Auto-Submitted", "no")).lower()
        precedence = str(raw_headers.get("Precedence", "")).lower()
        name, address = parseaddr(str(payload.get("from") or ""))
        address = address.lower()
        if (
            not address
            or auto != "no"
            or precedence in ("bulk", "junk", "list", "auto_reply")
            or raw_headers.get("List-Id")
            or raw_headers.get("X-Autoreply")
            or raw_headers.get("X-Autorespond")
            or _NOREPLY.match(address)
        ):
            return []  # auto-replies, bounces and mailing lists: never answered (no mail loops)
        subject = str(payload.get("subject") or "").strip()
        body = _strip_quote(str(payload.get("text") or ""))
        if not body and payload.get("html"):
            body = _strip_quote(re.sub(r"<[^>]+>", " ", str(payload["html"])))
        files = []
        try:
            for info in (json.loads(str(payload.get("attachment-info") or "{}")) or {}).values():
                files.append(
                    {
                        "kind": "image" if str(info.get("type", "")).startswith("image/") else "file",
                        "name": str(info.get("filename") or info.get("name") or "")[:200],
                    }
                )
        except (ValueError, AttributeError):
            pass
        message_id = str(raw_headers.get("Message-ID") or "").strip() or f"<{uuid.uuid4()}@inbound>"
        # remember the thread, to answer in it
        refs = " ".join(x for x in (str(raw_headers.get("References") or "").strip(), message_id) if x)
        self.hub.inbox.set_channel_state(
            f"{self.id}:{address}", subject=subject, message_id=message_id, references=refs[-2000:]
        )
        text = (
            body
            if not subject or subject.lower().startswith(("re:", "aw:", "tr:"))
            else f"{subject}\n\n{body}"
        )
        if not text.strip() and not files:
            return []
        return [
            InboundMessage(
                conversation=address,
                customer_name=name[:80],
                text=text[:8000],
                sender="customer",
                external_id=message_id[:300],
                ts=datetime.now(UTC),
                attachments=files,
            )
        ]

    def _message(self, to: str, text: str) -> EmailMessage:
        thread = self.hub.inbox.channel_state(f"{self.id}:{to}")
        subject = thread.get("subject") or str(self.cfg.opt("default_subject", "Phản hồi từ cửa hàng"))
        sender = str(self.cfg.opt("smtp_from") or self.cfg.opt("smtp_user") or "")
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = to
        msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        msg["Message-ID"] = make_msgid(domain=(parseaddr(sender)[1].partition("@")[2] or None))
        if thread.get("message_id"):
            msg["In-Reply-To"] = thread["message_id"]
            msg["References"] = thread.get("references") or thread["message_id"]
        msg["Auto-Submitted"] = "auto-replied" if self.cfg.opt("mark_auto_replied", True) else "no"
        msg.set_content(text)
        return msg

    def _smtp_send(self, msg: EmailMessage) -> None:
        host = str(self.cfg.opt("smtp_host") or "")
        if not host:
            raise ChannelError("email: smtp_host is not set")
        port = int(self.cfg.opt("smtp_port", 587))
        mode = str(self.cfg.opt("smtp_tls", "starttls"))
        context = ssl.create_default_context()
        smtp_cls = smtplib.SMTP_SSL if mode == "ssl" else smtplib.SMTP
        kwargs: dict[str, Any] = {"timeout": 30, **({"context": context} if mode == "ssl" else {})}
        with smtp_cls(host, port, **kwargs) as server:
            if mode == "starttls":
                server.starttls(context=context)
            if user := self.cfg.opt("smtp_user"):
                server.login(str(user), str(self.cfg.opt("smtp_password", "")))
            server.send_message(msg)

    async def send(self, conversation: str, text: str) -> str | None:
        msg = self._message(conversation, text)
        try:
            await asyncio.to_thread(self._smtp_send, msg)  # smtplib blocks: keep the event loop free
        except (OSError, smtplib.SMTPException) as e:
            raise ChannelError(f"email: {type(e).__name__} {e}") from None
        return str(msg["Message-ID"])
