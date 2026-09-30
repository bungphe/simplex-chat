"""Sales staff, marketing and money (sale-management modules 4-5):

- **Shifts and commissions.** Hours worked per shift; commission for a period =
  (completed sales - hours x the sales target per hour) x the commission rate. Only
  orders *delivered* in the period count (a cancelled or returned order never pays a
  commission). An optional employer contribution (insurance, superannuation) is added to
  the shop's cost; it is never taken off what the salesperson earns.
- **Advertising spend** per campaign and platform, and **expenses** (rent, utilities,
  payroll...), both flowing into the **profit and loss** report with revenue, the FIFO
  cost of goods sold, delivery costs and commissions.
- **Weekly price report**: every product whose price changed this week, with what it sold,
  its profit and the stock left, to judge the price and promotion changes.
- **Customer segments** for remarketing (top buyers, VIP, by channel), as CSV.
- **Start-of-day notices**: an important message every staff member sees in a popup after
  logging in, until they confirm it or skip it.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from .delivery import haversine_km
from .i18n import tr
from .inventory import InventoryError, _dec
from .state import now_iso

if TYPE_CHECKING:
    from .employee import Office

SCHEMA = """
CREATE TABLE IF NOT EXISTS sales_shifts (
  id {id}, username TEXT NOT NULL, warehouse_id {int}, work_date TEXT NOT NULL, hours TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL DEFAULT '', created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sales_shifts_user ON sales_shifts (username, work_date);
CREATE TABLE IF NOT EXISTS sales_commissions (
  id {id}, username TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL, sales {int} NOT NULL,
  orders {int} NOT NULL, hours TEXT NOT NULL, target_per_hour {int} NOT NULL, target {int} NOT NULL,
  excess {int} NOT NULL, rate_pct TEXT NOT NULL, commission {int} NOT NULL, contribution_pct TEXT NOT NULL,
  contribution {int} NOT NULL, status TEXT NOT NULL DEFAULT 'draft', created_by TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL, decided TEXT);
CREATE TABLE IF NOT EXISTS mk_ad_spend (
  id {id}, campaign TEXT NOT NULL, platform TEXT NOT NULL, amount {int} NOT NULL, start_date TEXT NOT NULL,
  end_date TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fin_expenses (
  id {id}, name TEXT NOT NULL, category TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'fixed', amount {int} NOT NULL,
  expense_date TEXT NOT NULL, taxable {int} NOT NULL DEFAULT 1, note TEXT NOT NULL DEFAULT '', created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS staff_notices (
  id {id}, title TEXT NOT NULL, body TEXT NOT NULL, active {int} NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL DEFAULT '', created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS staff_notice_acks (
  notice_id {int} NOT NULL, username TEXT NOT NULL, action TEXT NOT NULL, ts TEXT NOT NULL,
  PRIMARY KEY (notice_id, username))
"""
SETTINGS_KEY = "sales_settings"
DEFAULTS = {
    "target_per_hour": 1_000_000,
    "rate_pct": 1.0,
    "contribution_pct": 0.0,
    "payroll_contribution_pct": 0.0,
}
PLATFORMS = ("facebook", "google", "tiktok", "zalo", "shopee", "other")
EXPENSE_CATEGORIES = ("rent", "utilities", "insurance", "payroll", "delivery", "marketing", "other")


def _date(value: Any, what: str) -> str:
    try:
        return date.fromisoformat(str(value or "")[:10]).isoformat()
    except ValueError:
        raise InventoryError(tr("{0}: dạng YYYY-MM-DD", what)) from None


def _pct(value: Any, what: str) -> Decimal:
    d = _dec(value, what)
    if not 0 <= d <= 100:
        raise InventoryError(tr("{0}: từ 0 đến 100", what))
    return d


def _day_bounds(start: str, end: str) -> tuple[str, str]:
    """Timestamps covering whole days, in the office's local time (as orders are stamped)."""
    lo = datetime.fromisoformat(start).astimezone().isoformat(timespec="seconds")
    hi = (datetime.fromisoformat(end) + timedelta(days=1)).astimezone().isoformat(timespec="seconds")
    return lo, hi


class Sales:
    def __init__(self, office: Office):
        self.office = office
        self.inv = office.inventory
        self.db = self.inv.db
        self.db.script(SCHEMA)

    # settings

    def settings(self) -> dict[str, Any]:
        s = {**DEFAULTS, **(self.office.docs.get(SETTINGS_KEY) or {})}
        return {**s, "target_per_hour": self.inv.major(int(s["target_per_hour"]))}

    def save_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        if "target_per_hour" in data:
            clean["target_per_hour"] = self.inv.minor(data["target_per_hour"], tr("Định mức doanh số / giờ"))
        for key, what in (
            ("rate_pct", tr("% hoa hồng")),
            ("contribution_pct", tr("% đóng góp của chủ")),
            ("payroll_contribution_pct", tr("% bảo hiểm trên lương")),
        ):
            if key in data:
                clean[key] = float(_pct(data[key], what))
        self.office.docs.update(SETTINGS_KEY, lambda d: d.update(clean), {})
        return self.settings()

    # ------------------------------------------------------------------ #
    # shifts and commissions

    def add_shift(
        self,
        username: str,
        work_date: Any,
        hours: Any,
        warehouse_id: Any = None,
        note: str = "",
        actor: str = "",
    ) -> dict[str, Any]:
        h = _dec(hours, tr("Số giờ"))
        if not 0 < h <= 24:
            raise InventoryError(tr("Số giờ trong một ca: từ 0 đến 24"))
        if not username:
            raise InventoryError(tr("Chọn nhân viên"))
        sid = self.db.execute(
            "INSERT INTO sales_shifts (username, warehouse_id, work_date, hours, note, created_by, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (
                username,
                int(warehouse_id) if warehouse_id else None,
                _date(work_date, tr("Ngày làm")),
                str(h),
                note[:300],
                actor,
                now_iso(),
            ),
        )
        return self.db.row("SELECT * FROM sales_shifts WHERE id=?", (sid,)) or {}

    def delete_shift(self, sid: int) -> None:
        self.db.execute("DELETE FROM sales_shifts WHERE id=?", (sid,))

    def shifts(self, start: str, end: str, username: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM sales_shifts WHERE work_date>=? AND work_date<=?", [start, end]
        if username:
            sql += " AND username=?"
            args.append(username)
        return self.db.rows(sql + " ORDER BY work_date DESC, id DESC", args)

    def sales_by_person(self, start: str, end: str) -> dict[str, dict[str, int]]:
        lo, hi = _day_bounds(start, end)
        out: dict[str, dict[str, int]] = {}
        for r in self.db.rows(
            "SELECT salesperson, COUNT(*) AS n, SUM(total-shipping_fee) AS sales, SUM(profit) AS profit FROM inv_orders "
            "WHERE status='completed' AND completed>=? AND completed<? AND salesperson<>'' GROUP BY salesperson",
            (lo, hi),
        ):
            out[r["salesperson"]] = {
                "orders": int(r["n"]),
                "sales": int(r["sales"] or 0),
                "profit": int(r["profit"] or 0),
            }
        return out

    def preview_commissions(
        self,
        start: str,
        end: str,
        target_per_hour: Any = None,
        rate_pct: Any = None,
        contribution_pct: Any = None,
    ) -> list[dict[str, Any]]:
        """Commission for everyone who sold or worked in the period (nothing is saved)."""
        start, end = _date(start, tr("Từ ngày")), _date(end, tr("Đến ngày"))
        if end < start:
            raise InventoryError(tr("Đến ngày phải sau từ ngày"))
        s = {**DEFAULTS, **(self.office.docs.get(SETTINGS_KEY) or {})}
        per_hour = (
            self.inv.minor(target_per_hour, tr("Định mức"))
            if target_per_hour not in (None, "")
            else int(s["target_per_hour"])
        )
        rate = _pct(rate_pct if rate_pct not in (None, "") else s["rate_pct"], tr("% hoa hồng"))
        contrib = _pct(
            contribution_pct if contribution_pct not in (None, "") else s["contribution_pct"],
            tr("% đóng góp"),
        )
        sold = self.sales_by_person(start, end)
        hours: dict[str, Decimal] = {}
        for r in self.shifts(start, end):
            hours[r["username"]] = hours.get(r["username"], Decimal(0)) + Decimal(r["hours"])
        rows = []
        for user in sorted(set(sold) | set(hours)):
            if user.startswith("ai:"):
                continue  # AI employees' sales are reported, not paid
            sales = sold.get(user, {}).get("sales", 0)
            h = hours.get(user, Decimal(0))
            target = int((Decimal(per_hour) * h).to_integral_value())
            excess = max(0, sales - target)
            commission = int((Decimal(excess) * rate / 100).to_integral_value())
            contribution = int((Decimal(commission) * contrib / 100).to_integral_value())
            rows.append(
                {
                    "username": user,
                    "orders": sold.get(user, {}).get("orders", 0),
                    "sales": sales,
                    "hours": str(h),
                    "sales_per_hour": int(sales / h) if h else None,
                    "target_per_hour": per_hour,
                    "target": target,
                    "excess": excess,
                    "rate_pct": str(rate),
                    "commission": commission,
                    "contribution_pct": str(contrib),
                    "contribution": contribution,
                    "period_start": start,
                    "period_end": end,
                }
            )
        return [self._money(r) for r in rows]

    def _money(self, r: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "sales",
            "target_per_hour",
            "target",
            "excess",
            "commission",
            "contribution",
            "sales_per_hour",
        )
        return {**r, **{k: self.inv.major(r[k]) for k in keys if r.get(k) is not None}}

    def save_commissions(self, start: str, end: str, actor: str, **params: Any) -> list[dict[str, Any]]:
        """Save the period's commissions as drafts; saving the same period again replaces its
        drafts. A commission already finalized or paid must be put back to draft first."""
        rows = self.preview_commissions(start, end, **params)
        start, end = _date(start, tr("Từ ngày")), _date(end, tr("Đến ngày"))
        with self.db.transaction():
            done = self.db.row(
                "SELECT username FROM sales_commissions WHERE period_start=? AND period_end=? AND status<>'draft' "
                "ORDER BY id LIMIT 1",
                (start, end),
            )
            if done:
                raise InventoryError(
                    tr(
                        "Hoa hồng kỳ {0} – {1} của {2} đã chốt: chuyển về nháp trước khi tính lại",
                        start,
                        end,
                        done["username"],
                    )
                )
            self.db.execute(
                "DELETE FROM sales_commissions WHERE period_start=? AND period_end=? AND status='draft'",
                (start, end),
            )
            for r in rows:
                self.db.execute(
                    "INSERT INTO sales_commissions (username, period_start, period_end, sales, orders, hours, target_per_hour, "
                    "target, excess, rate_pct, commission, contribution_pct, contribution, created_by, created) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        r["username"],
                        r["period_start"],
                        r["period_end"],
                        self.inv.minor(r["sales"]),
                        r["orders"],
                        r["hours"],
                        self.inv.minor(r["target_per_hour"]),
                        self.inv.minor(r["target"]),
                        self.inv.minor(r["excess"]),
                        r["rate_pct"],
                        self.inv.minor(r["commission"]),
                        r["contribution_pct"],
                        self.inv.minor(r["contribution"]),
                        actor,
                        now_iso(),
                    ),
                )
        return self.commissions()

    def commissions(self, username: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM sales_commissions", []
        if username:
            sql += " WHERE username=?"
            args.append(username)
        return [self._money(r) for r in self.db.rows(sql + " ORDER BY id DESC LIMIT 500", args)]

    def set_commission_status(self, cid: int, status: str) -> dict[str, Any]:
        flow = {"draft": ("finalized",), "finalized": ("paid", "draft")}
        row = self.db.row("SELECT * FROM sales_commissions WHERE id=?", (cid,))
        if row is None:
            raise InventoryError(tr("Không có bảng hoa hồng này"))
        if status not in flow.get(row["status"], ()):
            raise InventoryError(tr("Không chuyển được từ '{0}' sang '{1}'", row["status"], status))
        self.db.execute(
            "UPDATE sales_commissions SET status=?, decided=? WHERE id=?", (status, now_iso(), cid)
        )
        return self._money(self.db.row("SELECT * FROM sales_commissions WHERE id=?", (cid,)) or {})

    # ------------------------------------------------------------------ #
    # advertising and expenses

    def add_ad_spend(self, data: dict[str, Any]) -> dict[str, Any]:
        platform = str(data.get("platform") or "").lower()
        if platform not in PLATFORMS:
            raise InventoryError(tr("Nền tảng: {0}", ", ".join(PLATFORMS)))
        start, end = _date(data.get("start_date"), tr("Từ ngày")), _date(data.get("end_date"), tr("Đến ngày"))
        if end < start:
            raise InventoryError(tr("Đến ngày phải sau từ ngày"))
        campaign = str(data.get("campaign") or "").strip()[:200]
        if not campaign:
            raise InventoryError(tr("Tên chiến dịch là bắt buộc"))
        aid = self.db.execute(
            "INSERT INTO mk_ad_spend (campaign, platform, amount, start_date, end_date, note, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (
                campaign,
                platform,
                self.inv.minor(data.get("amount"), tr("Chi phí")),
                start,
                end,
                str(data.get("note") or "")[:300],
                now_iso(),
            ),
        )
        return self._ad(self.db.row("SELECT * FROM mk_ad_spend WHERE id=?", (aid,)) or {})

    def _ad(self, r: dict[str, Any]) -> dict[str, Any]:
        return {**r, "amount": self.inv.major(r["amount"])}

    def ad_spend(self) -> list[dict[str, Any]]:
        return [
            self._ad(r)
            for r in self.db.rows("SELECT * FROM mk_ad_spend ORDER BY start_date DESC, id DESC LIMIT 500")
        ]

    def delete(self, table: str, rid: int) -> None:
        assert table in ("mk_ad_spend", "fin_expenses", "sales_shifts")
        self.db.execute(f"DELETE FROM {table} WHERE id=?", (rid,))

    def add_expense(self, data: dict[str, Any]) -> dict[str, Any]:
        category = str(data.get("category") or "other")
        if category not in EXPENSE_CATEGORIES:
            raise InventoryError(tr("Loại chi phí: {0}", ", ".join(EXPENSE_CATEGORIES)))
        kind = str(data.get("kind") or "fixed")
        if kind not in ("fixed", "variable"):
            raise InventoryError(tr("Chi phí cố định (fixed) hoặc lưu động (variable)"))
        name = str(data.get("name") or "").strip()[:200]
        if not name:
            raise InventoryError(tr("Tên khoản chi là bắt buộc"))
        eid = self.db.execute(
            "INSERT INTO fin_expenses (name, category, kind, amount, expense_date, taxable, note, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (
                name,
                category,
                kind,
                self.inv.minor(data.get("amount"), tr("Số tiền")),
                _date(data.get("date"), tr("Ngày")),
                1 if data.get("taxable", True) else 0,
                str(data.get("note") or "")[:300],
                now_iso(),
            ),
        )
        row = self.db.row("SELECT * FROM fin_expenses WHERE id=?", (eid,)) or {}
        return {**row, "amount": self.inv.major(row["amount"])}

    def expenses(self, start: str, end: str) -> list[dict[str, Any]]:
        return [
            {**r, "amount": self.inv.major(r["amount"])}
            for r in self.db.rows(
                "SELECT * FROM fin_expenses WHERE expense_date>=? AND expense_date<=? ORDER BY expense_date DESC, id DESC",
                (start, end),
            )
        ]

    # ------------------------------------------------------------------ #
    # reports

    def pnl(self, start: str, end: str) -> dict[str, Any]:
        """Profit and loss for whole days: revenue and FIFO cost of the goods delivered in the
        period, minus advertising (shared by days of each campaign in the period), expenses,
        commissions of the period and delivery costs."""
        start, end = _date(start, tr("Từ ngày")), _date(end, tr("Đến ngày"))
        lo, hi = _day_bounds(start, end)
        sales = (
            self.db.row(
                "SELECT COUNT(*) AS n, SUM(total) AS total, SUM(shipping_fee) AS ship, SUM(cost) AS cost, SUM(profit) AS profit, "
                "SUM(discount+voucher_discount) AS disc FROM inv_orders WHERE status='completed' AND completed>=? AND completed<?",
                (lo, hi),
            )
            or {}
        )
        days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
        ads = 0
        by_platform: dict[str, int] = {}
        for a in self.db.rows("SELECT * FROM mk_ad_spend WHERE start_date<=? AND end_date>=?", (end, start)):
            a_start, a_end = max(a["start_date"], start), min(a["end_date"], end)
            span = (date.fromisoformat(a["end_date"]) - date.fromisoformat(a["start_date"])).days + 1
            inside = (date.fromisoformat(a_end) - date.fromisoformat(a_start)).days + 1
            part = int(a["amount"]) * inside // span
            ads += part
            by_platform[a["platform"]] = by_platform.get(a["platform"], 0) + part
        s = {**DEFAULTS, **(self.office.docs.get(SETTINGS_KEY) or {})}
        expenses: dict[str, int] = {}
        payroll_taxable = 0
        for e in self.db.rows(
            "SELECT * FROM fin_expenses WHERE expense_date>=? AND expense_date<=?", (start, end)
        ):
            expenses[e["category"]] = expenses.get(e["category"], 0) + int(e["amount"])
            if e["category"] == "payroll" and e["taxable"]:
                payroll_taxable += int(e["amount"])
        payroll_contrib = int(Decimal(payroll_taxable) * Decimal(str(s["payroll_contribution_pct"])) / 100)
        comm = (
            self.db.row(
                "SELECT SUM(commission+contribution) AS c FROM sales_commissions WHERE status IN ('finalized','paid') "
                "AND period_end>=? AND period_end<=?",
                (start, end),
            )
            or {}
        )
        delivery = getattr(self.office, "delivery", None)
        delivery_cost = delivery.cost_between(start, end) if delivery else 0
        revenue = int(sales.get("total") or 0) - int(sales.get("ship") or 0)
        gross = revenue - int(sales.get("cost") or 0)
        costs = ads + sum(expenses.values()) + payroll_contrib + int(comm.get("c") or 0) + delivery_cost
        m = self.inv.major
        return {
            "start": start,
            "end": end,
            "days": days,
            "orders": int(sales.get("n") or 0),
            "revenue": m(revenue),
            "shipping_income": m(int(sales.get("ship") or 0)),
            "discounts": m(int(sales.get("disc") or 0)),
            "cogs": m(int(sales.get("cost") or 0)),
            "gross_profit": m(gross),
            "gross_margin_pct": round(100 * gross / revenue, 1) if revenue else 0,
            "ads": m(ads),
            "ads_by_platform": {k: m(v) for k, v in by_platform.items()},
            "expenses": {k: m(v) for k, v in expenses.items()},
            "payroll_contribution": m(payroll_contrib),
            "commissions": m(int(comm.get("c") or 0)),
            "delivery_costs": m(delivery_cost),
            "net_profit": m(gross + int(sales.get("ship") or 0) - costs),
        }

    def weekly_prices(self, today: date | None = None) -> list[dict[str, Any]]:
        """Products whose price changed since Monday: the price then and now, sales, profit
        and stock this week."""
        today = today or datetime.now().astimezone().date()
        monday = today - timedelta(days=today.weekday())
        lo, hi = _day_bounds(monday.isoformat(), today.isoformat())
        changed = {
            int(r["product_id"])
            for r in self.db.rows(
                "SELECT DISTINCT product_id FROM inv_price_log WHERE ts>=? AND ts<?", (lo, hi)
            )
        }
        out = []
        for pid in sorted(changed):
            first = self.db.row(
                "SELECT * FROM inv_price_log WHERE product_id=? AND ts>=? ORDER BY id LIMIT 1", (pid, lo)
            )
            sold = (
                self.db.row(
                    "SELECT SUM(i.qty) AS q, SUM(i.line_total) AS rev FROM inv_order_items i JOIN inv_orders o ON o.id=i.order_id "
                    "WHERE i.product_id=? AND o.status='completed' AND o.completed>=? AND o.completed<?",
                    (pid, lo, hi),
                )
                or {}
            )
            cost = (
                self.db.row(
                    "SELECT SUM(a.qty*a.unit_cost) AS c FROM inv_order_allocs a JOIN inv_order_items i ON i.id=a.order_item_id "
                    "JOIN inv_orders o ON o.id=i.order_id WHERE i.product_id=? AND o.status='completed' AND o.completed>=? AND o.completed<?",
                    (pid, lo, hi),
                )
                or {}
            )
            p = self.inv.product(pid)
            lot = next((x for x in p["lots"] if x["status"] == "active"), None)
            rev = int(sold.get("rev") or 0)
            out.append(
                {
                    "product_id": pid,
                    "sku": p["sku"],
                    "name": p["name"],
                    "price_before": self.inv.major(first["old_price"])
                    if first and first["old_price"] is not None
                    else None,
                    "price_now": p["price"],
                    "stage": p["stage"],
                    "sold": int(sold.get("q") or 0),
                    "revenue": self.inv.major(rev),
                    "profit": self.inv.major(rev - int(cost.get("c") or 0)),
                    "available": p["available"],
                    "remaining_pct": lot["remaining_pct"] if lot else None,
                }
            )
        return out

    def segment(
        self,
        kind: str = "top",
        limit: int = 500,
        channel_type: str = "",
        min_orders: int = 0,
        near_wh: int | None = None,
        radius_km: float | None = None,
    ) -> list[dict[str, Any]]:
        """Customers for a remarketing campaign: top buyers, VIPs, or those who came from a
        channel type (facebook, zalo_oa, telegram...); with `near_wh` and `radius_km`, only
        those living within that distance of the showroom (nearest first)."""
        sql = (
            "SELECT c.id, c.name, c.phone, c.email, c.vip, c.points, c.total_spent, c.orders_count, "
            "c.address, c.lat, c.lng FROM crm_contacts c WHERE c.orders_count>=?"
        )
        args: list[Any] = [min_orders]
        if kind == "vip":
            sql += " AND c.vip=1"
        if channel_type:
            ids = [c for c, ch in self.office.hub.channels.items() if ch.type == channel_type]
            if channel_type == "simplex":
                sql += " AND EXISTS (SELECT 1 FROM crm_links l JOIN conversations v ON v.id=l.conversation_id WHERE l.contact_id=c.id AND v.channel LIKE ?)"
                args.append("simplex:%")  # a literal % breaks PostgreSQL's %s placeholders
            elif ids:
                sql += (
                    " AND EXISTS (SELECT 1 FROM crm_links l JOIN conversations v ON v.id=l.conversation_id "
                    f"WHERE l.contact_id=c.id AND v.channel IN ({','.join('?' * len(ids))}))"
                )
                args += ids
            else:
                return []
        origin = None
        if near_wh:
            wh = self.inv.warehouse(int(near_wh))
            if not wh.get("lat") or not wh.get("lng"):
                raise InventoryError(tr("Kho {0} chưa có toạ độ (Giao hàng → Kho: đặt toạ độ)", wh["code"]))
            origin = (float(wh["lat"]), float(wh["lng"]))
            sql += " AND c.lat IS NOT NULL AND c.lng IS NOT NULL"
        sql += " ORDER BY c.total_spent DESC, c.id"
        if origin is None:
            sql += " LIMIT ?"
            args.append(limit)
        rows = self.db.rows(sql, args)
        out = []
        for r in rows:
            row = {**r, "total_spent": self.inv.major(r["total_spent"])}
            if origin is not None:
                km = haversine_km(origin, (float(r["lat"]), float(r["lng"])))
                if radius_km is not None and km > float(radius_km):
                    continue
                row["distance_km"] = round(km, 1)
            out.append(row)
        if origin is not None:
            out.sort(key=lambda x: x["distance_km"])
        return out[:limit]

    def located(self) -> dict[str, int]:
        """How many customers have a known location (the rest cannot be found by distance)."""
        row = (
            self.db.row(
                "SELECT COUNT(*) AS n, SUM(CASE WHEN lat IS NOT NULL THEN 1 ELSE 0 END) AS located, "
                "SUM(CASE WHEN lat IS NULL AND address<>'' THEN 1 ELSE 0 END) AS to_geocode FROM crm_contacts"
            )
            or {}
        )
        return {k: int(row.get(k) or 0) for k in ("n", "located", "to_geocode")}

    def segment_csv(self, rows: list[dict[str, Any]]) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        near = any("distance_km" in r for r in rows)
        w.writerow(
            ["name", "phone", "email", "vip", "points", "total_spent", "orders", "address"]
            + (["distance_km"] if near else [])
        )
        for r in rows:
            w.writerow(
                [
                    r["name"],
                    r["phone"],
                    r["email"],
                    "1" if r["vip"] else "",
                    r["points"],
                    r["total_spent"],
                    r["orders_count"],
                    r.get("address", ""),
                ]
                + ([r.get("distance_km", "")] if near else [])
            )
        return "\ufeff" + buf.getvalue()

    # ------------------------------------------------------------------ #
    # start-of-day notices

    def add_notice(self, title: str, body: str, actor: str) -> dict[str, Any]:
        if not title.strip() or not body.strip():
            raise InventoryError(tr("Cần tiêu đề và nội dung"))
        nid = self.db.execute(
            "INSERT INTO staff_notices (title, body, created_by, created) VALUES (?, ?, ?, ?) RETURNING id",
            (title.strip()[:150], body.strip()[:4000], actor, now_iso()),
        )
        return self.db.row("SELECT * FROM staff_notices WHERE id=?", (nid,)) or {}

    def notices(self) -> list[dict[str, Any]]:
        rows = self.db.rows("SELECT * FROM staff_notices ORDER BY id DESC LIMIT 100")
        acks: dict[int, list[dict[str, Any]]] = {}
        for a in self.db.rows("SELECT * FROM staff_notice_acks"):
            acks.setdefault(int(a["notice_id"]), []).append(a)
        return [{**r, "acks": acks.get(int(r["id"]), [])} for r in rows]

    def pending_notices(self, username: str) -> list[dict[str, Any]]:
        """Active notices this person has neither confirmed nor skipped today."""
        today = datetime.now().astimezone().date().isoformat()
        return self.db.rows(
            "SELECT n.* FROM staff_notices n WHERE n.active=1 AND NOT EXISTS (SELECT 1 FROM staff_notice_acks a "
            "WHERE a.notice_id=n.id AND a.username=? AND (a.action='done' OR a.ts>=?)) ORDER BY n.id",
            (username, today),
        )

    def ack_notice(self, nid: int, username: str, action: str) -> None:
        """'done' (handled: never shown again) or 'skip' (shown again tomorrow)."""
        if action not in ("done", "skip"):
            raise InventoryError(tr("action: done hoặc skip"))
        self.db.execute(
            "INSERT INTO staff_notice_acks (notice_id, username, action, ts) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (notice_id, username) DO UPDATE SET action=excluded.action, ts=excluded.ts",
            (nid, username, action, now_iso()),
        )

    def close_notice(self, nid: int) -> None:
        self.db.execute("UPDATE staff_notices SET active=0 WHERE id=?", (nid,))
