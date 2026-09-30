"""The shop's own email: invoices, web order confirmations, login codes for customers.

Settings live in the office database (Kho hàng -> Cửa hàng): SMTP host, port, TLS mode,
user, sender, and the *name* of the environment variable holding the password (never
the password itself). Without them, the SMTP settings of an `email` chat channel are
used, if there is one.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from typing import TYPE_CHECKING, Any

from .i18n import tr
from .inventory import InventoryError

if TYPE_CHECKING:
    from .employee import Office

log = logging.getLogger(__name__)

KEY = "mail_settings"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def valid_email(value: str) -> bool:
    return bool(_EMAIL.match(value.strip()))


class Mailer:
    def __init__(self, office: Office):
        self.office = office

    def settings(self) -> dict[str, Any]:
        return self.office.docs.get(KEY) or {}

    def public(self) -> dict[str, Any]:
        s = self.settings()
        return {
            **s,
            "password_set": bool(os.environ.get(s.get("password_env") or "")),
            "ready": self._smtp() is not None,
            "using_channel": not s.get("smtp_host") and self._smtp() is not None,
        }

    def save(self, data: dict[str, Any]) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        for key, limit in (("smtp_host", 200), ("smtp_user", 200), ("password_env", 80), ("sender", 200)):
            if key in data:
                clean[key] = str(data[key] or "").strip()[:limit]
        if "smtp_port" in data:
            clean["smtp_port"] = int(data["smtp_port"] or 587)
        if "smtp_tls" in data:
            if data["smtp_tls"] not in ("starttls", "ssl", "none"):
                raise InventoryError(tr("TLS: starttls, ssl hoặc none"))
            clean["smtp_tls"] = data["smtp_tls"]
        if "auto_invoice" in data:
            clean["auto_invoice"] = bool(data["auto_invoice"])
        if clean.get("sender") and not valid_email(parseaddr(clean["sender"])[1]):
            raise InventoryError(tr("Địa chỉ gửi không hợp lệ"))
        self.office.docs.update(KEY, lambda d: d.update(clean), {})
        return self.public()

    def _smtp(self) -> dict[str, Any] | None:
        s = self.settings()
        if s.get("smtp_host"):
            return {
                "host": s["smtp_host"],
                "port": int(s.get("smtp_port") or 587),
                "tls": s.get("smtp_tls") or "starttls",
                "user": s.get("smtp_user") or "",
                "password": os.environ.get(s.get("password_env") or "", ""),
                "sender": s.get("sender") or s.get("smtp_user") or "",
            }
        for ch in self.office.hub.channels.values():  # an email chat channel's SMTP
            if ch.type == "email":
                o = ch.cfg.opt
                return {
                    "host": o("smtp_host"),
                    "port": int(o("smtp_port", 587)),
                    "tls": o("smtp_tls", "starttls"),
                    "user": o("smtp_user", ""),
                    "password": o("smtp_password", ""),
                    "sender": o("smtp_from") or o("smtp_user", ""),
                }
        return None

    @property
    def ready(self) -> bool:
        return self._smtp() is not None

    def _send_now(self, smtp: dict[str, Any], msg: EmailMessage) -> None:
        context = ssl.create_default_context()
        cls = smtplib.SMTP_SSL if smtp["tls"] == "ssl" else smtplib.SMTP
        kwargs: dict[str, Any] = {"timeout": 30, **({"context": context} if smtp["tls"] == "ssl" else {})}
        with cls(smtp["host"], smtp["port"], **kwargs) as server:
            if smtp["tls"] == "starttls":
                server.starttls(context=context)
            if smtp["user"]:
                server.login(smtp["user"], smtp["password"])
            server.send_message(msg)

    async def send(self, to: str, subject: str, text: str, html: str | None = None) -> str:
        """Send one email; returns its Message-ID. Raises InventoryError when it cannot."""
        smtp = self._smtp()
        if smtp is None:
            raise InventoryError(tr("Chưa cài đặt email gửi đi (Kho hàng → Cửa hàng → Email)"))
        if not valid_email(to):
            raise InventoryError(tr("Địa chỉ email không hợp lệ: {0}", to))
        msg = EmailMessage()
        msg["From"] = smtp["sender"]
        msg["To"] = to.strip()
        msg["Subject"] = subject.replace("\n", " ")[:200]
        msg["Message-ID"] = make_msgid(domain=(parseaddr(smtp["sender"])[1].partition("@")[2] or None))
        msg["Auto-Submitted"] = "auto-generated"  # our email channel never answers these
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        try:
            await asyncio.to_thread(self._send_now, smtp, msg)
        except (OSError, smtplib.SMTPException) as e:
            log.warning("mail to %s failed: %s", to, e)
            raise InventoryError(tr("Không gửi được email: {0}", type(e).__name__)) from None
        return str(msg["Message-ID"])
