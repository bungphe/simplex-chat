"""Tenant operations: sign-up, verification, provisioning (background, with retries),
suspend/resume, plan changes, admin password resets, deletion with an archive."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import unicodedata
from collections.abc import Coroutine
from datetime import date, timedelta
from typing import Any

from ..i18n import LANGUAGES, normalize, tr
from .config import RESERVED_SLUGS, SLUG, SaasConfig
from .notify import Notifier
from .provisioner import Backend, ProvisionError
from .store import SaasStore, check_password, hash_password

log = logging.getLogger(__name__)

MIN_PASSWORD = 10
CODE_MINUTES = 15
CODE_ATTEMPTS = 5
PROVISION_ATTEMPTS = 3
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SaasError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def suggest_slug(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name.replace("đ", "d").replace("Đ", "D"))
    ascii_name = ascii_name.encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:31]
    return slug if len(slug) >= 3 else (slug + "-shop")[:31]


def valid_slug(slug: str) -> bool:
    return bool(SLUG.match(slug)) and slug not in RESERVED_SLUGS and "--" not in slug


class Service:
    def __init__(self, cfg: SaasConfig, store: SaasStore, backend: Backend, notifier: Notifier):
        self.cfg, self.store, self.backend, self.notifier = cfg, store, backend, notifier
        self.tasks: set[asyncio.Task[Any]] = set()
        self.retry_delay = 30.0
        self.reveals: dict[int, str] = {}  # tenant id -> admin password, shown once
        self.today = date.today

    # --- background work -----------------------------------------------------------
    def spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def drain(self) -> None:
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    async def _run(self, fn: Any, *args: Any) -> Any:
        return await asyncio.to_thread(fn, *args)

    # --- sign-up -------------------------------------------------------------------
    def _validate_signup(self, data: dict[str, Any]) -> dict[str, Any]:
        shop = str(data.get("shop_name", "")).strip()[:80]
        owner = str(data.get("owner_name", "")).strip()[:80]
        email = str(data.get("email", "")).strip().lower()[:200]
        phone = re.sub(r"[^0-9+ ]", "", str(data.get("phone", "")))[:20]
        password = str(data.get("password", ""))
        slug = str(data.get("slug", "")).strip().lower() or suggest_slug(shop)
        lang = normalize(str(data.get("lang") or "")) or "vi"
        plan = str(data.get("plan") or self.cfg.default_plan)
        if not shop or not owner:
            raise SaasError(tr("Vui lòng nhập tên cửa hàng và tên của bạn"))
        if not _EMAIL.match(email):
            raise SaasError(tr("Địa chỉ email không hợp lệ"))
        if len(password) < MIN_PASSWORD:
            raise SaasError(tr("mật khẩu cần ít nhất {0} ký tự", MIN_PASSWORD))
        if not valid_slug(slug):
            raise SaasError(
                tr("Tên miền con: 3-31 ký tự a-z, 0-9 và dấu gạch ngang, bắt đầu bằng chữ hoặc số")
            )
        if plan not in self.cfg.plans:
            raise SaasError(tr("Gói dịch vụ không tồn tại"))
        return {
            "shop_name": shop,
            "owner_name": owner,
            "email": email,
            "phone": phone,
            "password": password,
            "slug": slug,
            "lang": lang,
            "plan": plan,
            "region": str(data.get("region") or "VN")[:8].upper(),
        }

    def _free_pending(self, column: str, value: str) -> None:
        """An unverified sign-up holds a slug or email only until someone re-uses it."""
        row = self.store.tenant_by(column, value)
        if row and row["status"] == "pending_email":
            self.store.db.execute("DELETE FROM tenants WHERE id=?", (row["id"],))

    async def signup(self, data: dict[str, Any]) -> dict[str, Any]:
        clean = self._validate_signup(data)
        self._free_pending("email", clean["email"])
        self._free_pending("slug", clean["slug"])
        password = clean.pop("password")
        try:
            tenant = self.store.add_tenant(
                **clean, password_hash=hash_password(password), status="pending_email"
            )
        except ValueError:
            if self.store.tenant_by("email", clean["email"]):
                raise SaasError(tr("Email này đã đăng ký; hãy đăng nhập cổng quản lý dịch vụ")) from None
            raise SaasError(tr("Tên miền con này đã có người dùng, hãy chọn tên khác")) from None
        code = self.store.new_code(tenant["email"], "signup")
        self.store.log(tenant["id"], "system", "signup", {"slug": tenant["slug"], "plan": tenant["plan"]})
        await self.notifier.code(tenant["email"], tenant["lang"], code, CODE_MINUTES)
        return tenant

    async def resend_code(self, email: str) -> None:
        tenant = self.store.tenant_by("email", email.strip().lower())
        if tenant and tenant["status"] == "pending_email":
            code = self.store.new_code(tenant["email"], "signup")
            await self.notifier.code(tenant["email"], tenant["lang"], code, CODE_MINUTES)

    async def verify(self, email: str, code: str) -> dict[str, Any]:
        """The emailed code checks out: the tenant starts its trial and is provisioned."""
        email = email.strip().lower()
        tenant = self.store.tenant_by("email", email)
        if tenant is None or tenant["status"] != "pending_email":
            raise SaasError(tr("Không có đăng ký nào chờ xác nhận cho email này"), 404)
        if not self.store.check_code(email, "signup", code, CODE_MINUTES * 60, CODE_ATTEMPTS):
            raise SaasError(tr("Mã xác nhận sai hoặc đã hết hạn"), 403)
        return await self.activate_trial(tenant, actor="system")

    async def activate_trial(self, tenant: dict[str, Any], actor: str) -> dict[str, Any]:
        tid = tenant["id"]
        trial_ends = (self.today() + timedelta(days=self.cfg.trial_days)).isoformat()
        self.store.update_tenant(
            tid,
            status="trial",
            trial_ends=trial_ends,
            admin_port=self.cfg.port_base + 2 * tid,
            shop_port=self.cfg.port_base + 2 * tid + 1,
            provision_state="queued",
        )
        admin_password = secrets.token_urlsafe(12)
        self.reveals[tid] = admin_password
        self.store.log(tid, actor, "trial_started", {"trial_ends": trial_ends})
        secrets_env = {"AI_ADMIN_PASSWORD": admin_password, "AI_ADMIN_TOKEN": secrets.token_hex(16)}
        self.spawn(self._provision(tid, secrets_env))
        return self.store.tenant(tid) or tenant

    async def _provision(self, tid: int, secrets_env: dict[str, str]) -> None:
        for attempt in range(1, PROVISION_ATTEMPTS + 1):
            tenant = self.store.tenant(tid)
            if tenant is None or tenant["status"] in ("deleted", "pending_email"):
                return
            self.store.update_tenant(tid, provision_state="running")
            try:
                await self._run(self.backend.create, tenant, secrets_env)
            except (ProvisionError, OSError) as e:
                log.warning("provisioning %s failed (attempt %d): %s", tenant["slug"], attempt, e)
                self.store.update_tenant(tid, provision_state="failed", provision_error=str(e)[:300])
                self.store.log(tid, "system", "provision_failed", str(e)[:300])
                if attempt < PROVISION_ATTEMPTS:
                    await asyncio.sleep(self.retry_delay * attempt)
                continue
            self.store.update_tenant(tid, provision_state="ready", provision_error="")
            self.store.log(tid, "system", "provisioned")
            await self.notifier.ready(self.store.tenant(tid) or tenant)
            return

    def reveal(self, tid: int) -> str | None:
        return self.reveals.pop(tid, None)

    # --- accounts ------------------------------------------------------------------
    def authenticate(self, email: str, password: str) -> dict[str, Any] | None:
        tenant = self.store.tenant_by("email", email.strip().lower())
        if tenant is None or tenant["status"] in ("pending_email", "deleted"):
            check_password(password, "")  # same work either way
            return None
        return tenant if check_password(password, tenant["password_hash"]) else None

    def change_password(self, tenant: dict[str, Any], current: str, new: str) -> None:
        if not check_password(current, tenant["password_hash"]):
            raise SaasError(tr("Mật khẩu hiện tại không đúng"), 403)
        if len(new) < MIN_PASSWORD:
            raise SaasError(tr("mật khẩu cần ít nhất {0} ký tự", MIN_PASSWORD))
        self.store.update_tenant(tenant["id"], password_hash=hash_password(new))
        self.store.drop_sessions("tenant", str(tenant["id"]))
        self.store.log(tenant["id"], tenant["email"], "password_changed")

    async def reset_admin_password(self, tenant: dict[str, Any], actor: str) -> str:
        if tenant["status"] in ("deleted", "pending_email"):
            raise SaasError(tr("Dịch vụ không hoạt động"), 409)
        password = secrets.token_urlsafe(12)
        try:
            await self._run(self.backend.reset_admin_password, tenant, password)
        except (ProvisionError, OSError) as e:
            raise SaasError(tr("Không đặt lại được mật khẩu: {0}", type(e).__name__), 500) from None
        self.store.log(tenant["id"], actor, "admin_password_reset")
        return password

    # --- lifecycle -----------------------------------------------------------------
    async def suspend(self, tenant: dict[str, Any], actor: str, reason: str = "") -> None:
        if tenant["status"] in ("suspended", "deleted"):
            return
        await self._run(self.backend.stop, tenant)
        self.store.update_tenant(tenant["id"], status="suspended", suspended_at=self.today().isoformat())
        self.store.log(tenant["id"], actor, "suspended", reason)
        await self.notifier.status(tenant, "suspended")

    async def resume(self, tenant: dict[str, Any], actor: str) -> None:
        if tenant["status"] not in ("suspended", "past_due"):
            return
        await self._run(self.backend.start, tenant)
        still_trial = tenant["trial_ends"] and tenant["trial_ends"] > self.today().isoformat()
        self.store.update_tenant(tenant["id"], status="trial" if still_trial else "active", suspended_at=None)
        self.store.log(tenant["id"], actor, "resumed")
        await self.notifier.status(tenant, "active")

    async def change_plan(self, tenant: dict[str, Any], plan: str, actor: str) -> None:
        if plan not in self.cfg.plans:
            raise SaasError(tr("Gói dịch vụ không tồn tại"))
        self.store.update_tenant(tenant["id"], plan=plan)
        if tenant["status"] in ("trial", "active", "past_due"):
            await self._run(self.backend.start, {**tenant, "plan": plan})  # rewrites the limits
        self.store.log(tenant["id"], actor, "plan_changed", {"from": tenant["plan"], "to": plan})

    async def extend_trial(self, tenant: dict[str, Any], days: int, actor: str) -> None:
        if not 1 <= days <= 365:
            raise SaasError(tr("Số ngày gia hạn: 1-365"))
        base = max(date.fromisoformat(tenant["trial_ends"] or self.today().isoformat()), self.today())
        ends = (base + timedelta(days=days)).isoformat()
        self.store.update_tenant(tenant["id"], trial_ends=ends)
        self.store.log(tenant["id"], actor, "trial_extended", {"trial_ends": ends})
        fresh = self.store.tenant(tenant["id"]) or tenant
        if fresh["status"] == "suspended":
            await self.resume(fresh, actor)
        elif fresh["status"] == "active":
            self.store.update_tenant(tenant["id"], status="trial")

    def request_cancellation(self, tenant: dict[str, Any], actor: str, cancel: bool = True) -> None:
        self.store.update_tenant(tenant["id"], cancel_requested=1 if cancel else 0)
        self.store.log(tenant["id"], actor, "cancel_requested" if cancel else "cancel_withdrawn")

    async def delete(self, tenant: dict[str, Any], actor: str, keep_backup: bool = True) -> str | None:
        if tenant["status"] == "deleted":
            return None
        backup = await self._run(self.backend.destroy, tenant, keep_backup)
        self.store.update_tenant(tenant["id"], status="deleted", provision_state="destroyed")
        self.store.retire_identifiers(tenant)
        self.store.drop_sessions("tenant", str(tenant["id"]))
        self.store.log(tenant["id"], actor, "deleted", {"backup": backup, "slug": tenant["slug"]})
        await self.notifier.status(tenant, "deleted")
        return backup

    async def usage(self, tenant: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._run(self.backend.usage, tenant)
        except (ProvisionError, OSError):
            return {"disk_mb": None}

    def language_name(self, code: str) -> str:
        return LANGUAGES.get(code, code)
