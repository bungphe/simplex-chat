"""Invoices and the daily lifecycle job.

- Trial: reminders 4 and 1 days before the end (day 10 and 13 of 14); the first invoice
  is issued with the first reminder so the tenant can pay before the trial ends. At the end
  the tenant is active if that invoice was paid, else suspended (its deployment stopped).
- Active: an invoice for the next month is issued `grace_days` before the paid period
  ends (idempotent: one invoice per period start). Past the period end unpaid: past_due;
  past the grace: suspended. Suspended for `delete_after_days`: archived and deleted.
- `Billing.mark_paid(invoice, gateway, ref)` is the entry point for operators today and
  a payment gateway's notification later; it extends the paid period and resumes a
  suspended tenant.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date, timedelta
from typing import Any

from ..i18n import tr
from .service import SaasError, Service

log = logging.getLogger(__name__)
TRIAL_REMINDERS = (4, 1)  # days before the trial ends
PAYABLE = ("due", "awaiting_confirmation")


def add_month(d: date) -> date:
    year, month = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return d.replace(year=year, month=month, day=min(d.day, calendar.monthrange(year, month)[1]))


def _d(text: str | None) -> date | None:
    return date.fromisoformat(text) if text else None


class Billing:
    def __init__(self, service: Service):
        self.s = service
        self.store, self.cfg = service.store, service.cfg

    # --- invoices ------------------------------------------------------------------
    async def issue(self, tenant: dict[str, Any], period_start: date, due: date) -> dict[str, Any] | None:
        """One invoice per period: a new one is emailed and returned; None when one exists
        already (or the plan is free)."""
        if self.store.invoice_for_period(tenant["id"], period_start.isoformat()):
            return None
        plan = self.cfg.plan(tenant["plan"])
        if plan.price_month <= 0:
            return None
        invoice = self.store.add_invoice(
            tenant["id"],
            period_start.isoformat(),
            add_month(period_start).isoformat(),
            plan.price_month,
            self.cfg.currency,
            due.isoformat(),
        )
        self.store.log(
            tenant["id"], "system", "invoice_issued", {"invoice": invoice["id"], "amount": invoice["amount"]}
        )
        await self.s.notifier.invoice(tenant, invoice)
        return invoice

    def claim_transferred(self, invoice: dict[str, Any], actor: str) -> None:
        """The tenant says the bank transfer was made; an operator confirms it."""
        if invoice["status"] != "due":
            raise SaasError(tr("Hoá đơn này không ở trạng thái chờ thanh toán"), 409)
        self.store.update_invoice(invoice["id"], status="awaiting_confirmation", gateway="bank_transfer")
        self.store.log(invoice["tenant_id"], actor, "transfer_claimed", {"invoice": invoice["id"]})

    async def mark_paid(
        self, invoice: dict[str, Any], gateway: str, ref: str, actor: str = "gateway"
    ) -> None:
        if invoice["status"] == "paid":
            return
        if invoice["status"] == "void":
            raise SaasError(tr("Hoá đơn đã huỷ"), 409)
        tenant = self.store.tenant(int(invoice["tenant_id"]))
        if tenant is None:
            raise SaasError(tr("Không tìm thấy khách hàng"), 404)
        self.store.update_invoice(
            invoice["id"],
            status="paid",
            gateway=gateway[:40],
            ref=ref[:120],
            paid_at=self.s.today().isoformat(),
        )
        paid_until = max(_d(tenant["paid_until"]) or date.min, _d(invoice["period_end"]) or date.min)
        self.store.update_tenant(tenant["id"], paid_until=paid_until.isoformat())
        self.store.log(
            tenant["id"],
            actor,
            "invoice_paid",
            {"invoice": invoice["id"], "gateway": gateway, "ref": ref[:120]},
        )
        fresh = self.store.tenant(tenant["id"]) or tenant
        await self.s.notifier.paid(fresh, {**invoice, "status": "paid"})
        if fresh["status"] in ("past_due", "suspended"):
            await self.s.resume(fresh, actor)

    def void(self, invoice: dict[str, Any], actor: str) -> None:
        if invoice["status"] == "paid":
            raise SaasError(tr("Không huỷ được hoá đơn đã thanh toán"), 409)
        self.store.update_invoice(invoice["id"], status="void")
        self.store.log(invoice["tenant_id"], actor, "invoice_void", {"invoice": invoice["id"]})

    # --- the daily job -------------------------------------------------------------
    async def daily(self, today: date | None = None) -> dict[str, int]:
        today = today or self.s.today()
        counts = {"reminders": 0, "invoices": 0, "past_due": 0, "suspended": 0, "deleted": 0, "activated": 0}
        # unverified sign-ups older than a day free their slug and email
        self.store.db.execute(
            "DELETE FROM tenants WHERE status='pending_email' AND created<?",
            ((today - timedelta(days=1)).isoformat(),),
        )
        for tenant in self.store.tenants(("trial", "active", "past_due", "suspended")):
            try:
                await self._step(tenant, today, counts)
            except Exception:  # one tenant's trouble must not stop the others
                log.exception("daily job: tenant %s", tenant["slug"])
        return counts

    async def _step(self, t: dict[str, Any], today: date, counts: dict[str, int]) -> None:
        tid = t["id"]
        paid_until = _d(t["paid_until"])
        if t["status"] == "trial":
            ends = _d(t["trial_ends"]) or today
            for days in TRIAL_REMINDERS:
                if today >= ends - timedelta(days=days) and not self.store.has_event(
                    tid, f"trial_reminder_{days}"
                ):
                    if not t["cancel_requested"]:
                        await self.issue(t, ends, ends)
                    await self.s.notifier.trial_reminder(t, max((ends - today).days, 0))
                    self.store.log(tid, "system", f"trial_reminder_{days}")
                    counts["reminders"] += 1
            if today >= ends:
                if paid_until and paid_until > today:
                    self.store.update_tenant(tid, status="active")
                    self.store.log(tid, "system", "activated")
                    counts["activated"] += 1
                else:
                    await self.s.suspend(t, "system", "trial ended without payment")
                    counts["suspended"] += 1
            return
        if t["status"] in ("active", "past_due"):
            end = paid_until or today
            issue = not t["cancel_requested"] and today >= end - timedelta(days=self.cfg.grace_days)
            if issue and await self.issue(t, end, end):
                counts["invoices"] += 1
            if today > end and t["status"] == "active":
                self.store.update_tenant(tid, status="past_due")
                self.store.log(tid, "system", "past_due")
                await self.s.notifier.status(t, "past_due")
                counts["past_due"] += 1
            if today > end + timedelta(days=self.cfg.grace_days):
                await self.s.suspend(t, "system", "unpaid after grace")
                counts["suspended"] += 1
            return
        if t["status"] == "suspended":
            since = _d(t["suspended_at"]) or today
            if today >= since + timedelta(days=self.cfg.delete_after_days):
                await self.s.delete(t, "system", keep_backup=True)
                counts["deleted"] += 1
