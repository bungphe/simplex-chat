"""Email to tenants: verification codes, provisioning, invoices, reminders, suspension.

Texts are Vietnamese in the code and go through tr(); each email is rendered in the
tenant's language. `Mailer.send` is the one place mail leaves; tests replace it.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any

from ..i18n import number, tr, use_language
from .config import SaasConfig, SmtpConfig

log = logging.getLogger(__name__)


class Mailer:
    def __init__(self, smtp: SmtpConfig | None):
        self.smtp = smtp
        self.sent: list[dict[str, str]] = []  # kept only without SMTP (dev and tests)

    def _send_now(self, msg: EmailMessage) -> None:
        assert self.smtp
        s = self.smtp
        context = ssl.create_default_context()
        cls = smtplib.SMTP_SSL if s.tls == "ssl" else smtplib.SMTP
        kwargs: dict[str, Any] = {"timeout": 30, **({"context": context} if s.tls == "ssl" else {})}
        with cls(s.host, s.port, **kwargs) as server:
            if s.tls == "starttls":
                server.starttls(context=context)
            if s.user:
                server.login(s.user, s.password)
            server.send_message(msg)

    async def send(self, to: str, subject: str, text: str) -> bool:
        if self.smtp is None:
            self.sent.append({"to": to, "subject": subject, "text": text})
            log.info("mail (no SMTP configured) to %s: %s", to, subject)
            return True
        msg = EmailMessage()
        msg["From"] = self.smtp.sender
        msg["To"] = to
        msg["Subject"] = subject.replace("\n", " ")[:200]
        msg["Message-ID"] = make_msgid(domain=self.smtp.sender.partition("@")[2] or None)
        msg["Auto-Submitted"] = "auto-generated"
        msg.set_content(text)
        try:
            await asyncio.to_thread(self._send_now, msg)
        except (OSError, smtplib.SMTPException) as e:
            log.warning("mail to %s failed: %s", to, type(e).__name__)  # no addresses' contents in logs
            return False
        return True


class Notifier:
    def __init__(self, cfg: SaasConfig, mailer: Mailer):
        self.cfg, self.mailer = cfg, mailer

    async def _to(self, tenant: dict[str, Any], subject: str, body: str) -> None:
        footer = tr("\n\n— {0}\nQuản lý dịch vụ: {1}/portal", self.cfg.base_domain, self.cfg.public_url)
        await self.mailer.send(tenant["email"], subject, body + footer)

    async def code(self, email: str, lang: str, code: str, minutes: int) -> None:
        with use_language(lang):
            await self.mailer.send(
                email,
                tr("Mã xác nhận đăng ký: {0}", code),
                tr(
                    "Mã xác nhận email của bạn là {0}. Mã có hiệu lực {1} phút.\n"
                    "Nếu bạn không đăng ký dùng thử, hãy bỏ qua email này.",
                    code,
                    minutes,
                ),
            )

    async def ready(self, tenant: dict[str, Any]) -> None:
        with use_language(tenant["lang"]):
            await self._to(
                tenant,
                tr("Cửa hàng {0} đã sẵn sàng", tenant["shop_name"]),
                tr(
                    "Chào {0},\n\nHệ thống nhân viên AI của {1} đã được khởi tạo.\n"
                    "Trang quản trị: {2}\nWebsite bán hàng: {3}\n"
                    "Đăng nhập trang quản trị với tài khoản admin và mật khẩu đã hiển thị khi đăng ký "
                    "(có thể cấp lại trong cổng quản lý dịch vụ).\nDùng thử miễn phí đến {4}.",
                    tenant["owner_name"],
                    tenant["shop_name"],
                    self.cfg.admin_url(tenant["slug"]),
                    self.cfg.shop_url(tenant["slug"]),
                    tenant["trial_ends"] or "",
                ),
            )

    async def trial_reminder(self, tenant: dict[str, Any], days_left: int) -> None:
        with use_language(tenant["lang"]):
            await self._to(
                tenant,
                tr("Còn {0} ngày dùng thử", days_left),
                tr(
                    "Chào {0},\n\nThời gian dùng thử của {1} kết thúc ngày {2}. Để tiếp tục sử dụng, "
                    "hãy thanh toán hoá đơn đầu tiên trong cổng quản lý dịch vụ. Sau ngày này hệ thống "
                    "sẽ tạm dừng cho đến khi nhận được thanh toán.",
                    tenant["owner_name"],
                    tenant["shop_name"],
                    tenant["trial_ends"] or "",
                ),
            )

    async def invoice(self, tenant: dict[str, Any], invoice: dict[str, Any]) -> None:
        with use_language(tenant["lang"]):
            await self._to(
                tenant,
                tr("Hoá đơn #{0}: {1} {2}", invoice["id"], number(invoice["amount"]), invoice["currency"]),
                tr(
                    "Chào {0},\n\nHoá đơn dịch vụ kỳ {1} → {2}: {3} {4}, hạn thanh toán {5}.\n"
                    "Thanh toán bằng chuyển khoản với nội dung SAAS-{6} (hướng dẫn trong cổng quản lý dịch vụ).",
                    tenant["owner_name"],
                    invoice["period_start"],
                    invoice["period_end"],
                    number(invoice["amount"]),
                    invoice["currency"],
                    invoice["due"],
                    invoice["id"],
                ),
            )

    async def paid(self, tenant: dict[str, Any], invoice: dict[str, Any]) -> None:
        with use_language(tenant["lang"]):
            await self._to(
                tenant,
                tr("Đã nhận thanh toán hoá đơn #{0}", invoice["id"]),
                tr(
                    "Cảm ơn bạn. Hoá đơn #{0} ({1} {2}) đã được ghi nhận; dịch vụ của {3} được gia hạn đến {4}.",
                    invoice["id"],
                    number(invoice["amount"]),
                    invoice["currency"],
                    tenant["shop_name"],
                    invoice["period_end"],
                ),
            )

    async def status(self, tenant: dict[str, Any], status: str) -> None:
        with use_language(tenant["lang"]):
            subject, body = self._status_text(tenant, status)
            await self._to(tenant, subject, body)

    def _status_text(self, tenant: dict[str, Any], status: str) -> tuple[str, str]:
        texts = {
            "past_due": (
                tr("Hoá đơn quá hạn"),
                tr(
                    "Hoá đơn dịch vụ của {0} đã quá hạn. Vui lòng thanh toán trong {1} ngày, nếu không hệ thống sẽ tạm dừng.",
                    tenant["shop_name"],
                    self.cfg.grace_days,
                ),
            ),
            "suspended": (
                tr("Dịch vụ đã tạm dừng"),
                tr(
                    "Hệ thống nhân viên AI của {0} đã tạm dừng vì chưa nhận được thanh toán. Dữ liệu được giữ {1} ngày; "
                    "thanh toán hoá đơn để mở lại ngay.",
                    tenant["shop_name"],
                    self.cfg.delete_after_days,
                ),
            ),
            "active": (
                tr("Dịch vụ đã hoạt động trở lại"),
                tr("Hệ thống nhân viên AI của {0} đã được mở lại. Cảm ơn bạn!", tenant["shop_name"]),
            ),
            "deleted": (
                tr("Đã xoá dịch vụ"),
                tr(
                    "Hệ thống của {0} đã bị xoá sau {1} ngày tạm dừng. Một bản sao lưu dữ liệu được lưu giữ; "
                    "liên hệ với chúng tôi nếu bạn cần nhận lại.",
                    tenant["shop_name"],
                    self.cfg.delete_after_days,
                ),
            ),
        }
        return texts[status]
