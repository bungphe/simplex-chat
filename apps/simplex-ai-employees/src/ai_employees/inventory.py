"""Products, warehouses, purchasing and automatic pricing.

Rebuilt from the sale-management project (modules 1 and 2: products and the 5-stage
pricing engine; warehouses, supplier stock orders, landed cost, FIFO lots, reorder
point), in this office's database and API:

- **Products** (one row per SKU, with an optional group for variants), stock per
  warehouse (on hand, reserved, available), a ledger of every stock movement.
- **Purchase orders** (a container or a shipment from a supplier) with the landed cost
  of every item: purchase price in the supplier's currency x exchange rate, plus the
  container's freight and customs shared by volume (CBM), or by value when no volumes
  are given. The stage-1 price comes from a target margin (or the margin from a price),
  and stages 2-5 from the stage discounts; staff can edit every price before receiving.
- **FIFO lots**: each receipt is a lot with its own cost and five stage prices. A
  product sells at the price of its ACTIVE lot; newer lots wait (QUEUED) until the old
  one is sold out, then the next lot becomes active at stage 1 (new arrival price).
- **Automatic pricing**, once a day (and on demand): a lot moves to the next stage when
  its sellable stock has fallen to the target percentage and it has held its price for
  the minimum number of days, or when it has held its price for the maximum number of
  days (slow sellers are marked down). Every change is logged with its reason. VIP
  customers get the lot's VIP price, or else the next stage's price.
- **Sales orders** reserve stock when confirmed (FIFO lots, for cost and profit), and
  take it out when completed; cancelling releases it. **Pre-orders** reserve goods on an
  incoming purchase order and are allocated automatically when it is received.
- **Transfers** between warehouses and shops, **stock counts** and **reorder
  suggestions**: daily sales velocity x the supplier's lead time + safety stock.

Money is kept in whole minor units of the office currency (VND: dong, no decimals).
"""

from __future__ import annotations

import csv
import io
import json
import math
import uuid
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .db import Database, DocStore, IntegrityError
from .state import now_iso

SETTINGS_KEY = "inventory_settings"
DEFAULT_RULES = {  # from stage -> the defaults of the sale-management pricing engine
    "1": {"target_pct": 80, "min_days": 7, "max_days": 14},
    "2": {"target_pct": 60, "min_days": 7, "max_days": 30},
    "3": {"target_pct": 40, "min_days": 7, "max_days": 45},
    "4": {"target_pct": 20, "min_days": 7, "max_days": 60},
}
DEFAULT_SETTINGS: dict[str, Any] = {
    "currency": "VND",
    "decimals": 0,
    "round_to": 1000,  # prices rounded to the nearest 1.000 đ
    "default_margin_pct": 40,
    "stage_discounts": [0, 10, 25, 35, 50],  # % off the stage-1 price
    "rules": DEFAULT_RULES,
    "price_hour": 0,  # the daily price run, local time (not during opening hours)
    "velocity_days": 30,
    "safety_days": 7,
    "cover_days": 30,  # a reorder covers the lead time plus this many days of sales
}
PO_STATUSES = ("draft", "ordered", "shipping", "arrived", "partial", "received", "cancelled")
LEVELS = ("empty", "reorder", "low", "medium", "good")

SCHEMA = """
CREATE TABLE IF NOT EXISTS inv_warehouses (
  id {id}, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'warehouse',
  address TEXT NOT NULL DEFAULT '', active {int} NOT NULL DEFAULT 1, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inv_suppliers (
  id {id}, name TEXT NOT NULL, country TEXT NOT NULL DEFAULT '', contact_name TEXT NOT NULL DEFAULT '',
  phone TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', lead_time_days {int} NOT NULL DEFAULT 30,
  payment_terms TEXT NOT NULL DEFAULT '', active {int} NOT NULL DEFAULT 1, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inv_products (
  id {id}, sku TEXT NOT NULL UNIQUE, name TEXT NOT NULL, category TEXT NOT NULL DEFAULT '',
  group_name TEXT NOT NULL DEFAULT '', unit TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
  attributes TEXT NOT NULL DEFAULT '{}', cbm TEXT NOT NULL DEFAULT '0', weight_kg TEXT NOT NULL DEFAULT '0',
  supplier_id {int}, safety_stock {int} NOT NULL DEFAULT -1, vip_price {int}, auto_pricing {int} NOT NULL DEFAULT 1,
  rules TEXT NOT NULL DEFAULT '', active {int} NOT NULL DEFAULT 1, created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inv_stock (
  warehouse_id {int} NOT NULL, product_id {int} NOT NULL, on_hand {int} NOT NULL DEFAULT 0,
  reserved {int} NOT NULL DEFAULT 0, PRIMARY KEY (warehouse_id, product_id));
CREATE TABLE IF NOT EXISTS inv_purchase_orders (
  id {id}, po_number TEXT NOT NULL UNIQUE, supplier_id {int} NOT NULL, warehouse_id {int} NOT NULL,
  container_code TEXT NOT NULL DEFAULT '', eta TEXT NOT NULL DEFAULT '', arrived TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft', currency TEXT NOT NULL DEFAULT 'USD', exchange_rate TEXT NOT NULL DEFAULT '1',
  freight {int} NOT NULL DEFAULT 0, customs {int} NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inv_po_items (
  id {id}, po_id {int} NOT NULL, product_id {int} NOT NULL, qty_ordered {int} NOT NULL,
  qty_received {int} NOT NULL DEFAULT 0, qty_damaged {int} NOT NULL DEFAULT 0, qty_preordered {int} NOT NULL DEFAULT 0,
  unit_cost_foreign TEXT NOT NULL DEFAULT '0', unit_cbm TEXT NOT NULL DEFAULT '0', unit_cost {int} NOT NULL DEFAULT 0,
  unit_freight {int} NOT NULL DEFAULT 0, unit_tax {int} NOT NULL DEFAULT 0, landed_cost {int} NOT NULL DEFAULT 0,
  margin_pct TEXT NOT NULL DEFAULT '', price1 {int} NOT NULL DEFAULT 0, price2 {int} NOT NULL DEFAULT 0,
  price3 {int} NOT NULL DEFAULT 0, price4 {int} NOT NULL DEFAULT 0, price5 {int} NOT NULL DEFAULT 0, vip_price {int});
CREATE INDEX IF NOT EXISTS inv_po_items_po ON inv_po_items (po_id);
CREATE INDEX IF NOT EXISTS inv_po_items_product ON inv_po_items (product_id);
CREATE TABLE IF NOT EXISTS inv_lots (
  id {id}, product_id {int} NOT NULL, warehouse_id {int} NOT NULL, po_item_id {int}, source TEXT NOT NULL DEFAULT '',
  initial_qty {int} NOT NULL, remaining_qty {int} NOT NULL, reserved_qty {int} NOT NULL DEFAULT 0,
  preordered_qty {int} NOT NULL DEFAULT 0, landed_cost {int} NOT NULL, stage {int} NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'queued', received_date TEXT NOT NULL, activated_at TEXT, stage_since TEXT,
  exhausted_at TEXT, price1 {int} NOT NULL, price2 {int} NOT NULL, price3 {int} NOT NULL, price4 {int} NOT NULL,
  price5 {int} NOT NULL, vip_price {int}, created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS inv_lots_product ON inv_lots (product_id, status, received_date);
CREATE TABLE IF NOT EXISTS inv_moves (
  id {id}, ts TEXT NOT NULL, warehouse_id {int} NOT NULL, product_id {int} NOT NULL, lot_id {int},
  kind TEXT NOT NULL, qty {int} NOT NULL, ref_type TEXT NOT NULL DEFAULT '', ref_id {int},
  actor TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS inv_moves_product ON inv_moves (product_id, ts);
CREATE INDEX IF NOT EXISTS inv_moves_kind ON inv_moves (kind, ts);
CREATE TABLE IF NOT EXISTS inv_price_log (
  id {id}, ts TEXT NOT NULL, product_id {int} NOT NULL, lot_id {int}, old_stage {int}, new_stage {int},
  old_price {int}, new_price {int}, trigger TEXT NOT NULL, actor TEXT NOT NULL DEFAULT '',
  remaining_pct TEXT NOT NULL DEFAULT '', days {int}, reason TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS inv_price_log_product ON inv_price_log (product_id, id);
CREATE TABLE IF NOT EXISTS inv_transfers (
  id {id}, from_wh {int} NOT NULL, to_wh {int} NOT NULL, status TEXT NOT NULL DEFAULT 'requested',
  note TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL DEFAULT '', created TEXT NOT NULL,
  shipped TEXT, received TEXT);
CREATE TABLE IF NOT EXISTS inv_transfer_items (
  id {id}, transfer_id {int} NOT NULL, product_id {int} NOT NULL, qty {int} NOT NULL,
  qty_received {int} NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS inv_orders (
  id {id}, kind TEXT NOT NULL DEFAULT 'now', warehouse_id {int} NOT NULL, contact_id {int}, conversation_id {int},
  customer_name TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'confirmed', subtotal {int} NOT NULL DEFAULT 0, discount {int} NOT NULL DEFAULT 0,
  total {int} NOT NULL DEFAULT 0, cost {int} NOT NULL DEFAULT 0, profit {int} NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL, completed TEXT, cancelled TEXT);
CREATE TABLE IF NOT EXISTS inv_order_items (
  id {id}, order_id {int} NOT NULL, product_id {int} NOT NULL, qty {int} NOT NULL, unit_price {int} NOT NULL,
  stage {int}, vip {int} NOT NULL DEFAULT 0, po_item_id {int}, status TEXT NOT NULL DEFAULT 'reserved',
  line_total {int} NOT NULL);
CREATE INDEX IF NOT EXISTS inv_order_items_order ON inv_order_items (order_id);
CREATE INDEX IF NOT EXISTS inv_order_items_po ON inv_order_items (po_item_id, status);
CREATE TABLE IF NOT EXISTS inv_order_allocs (
  id {id}, order_item_id {int} NOT NULL, lot_id {int}, qty {int} NOT NULL, unit_cost {int} NOT NULL);
CREATE INDEX IF NOT EXISTS inv_order_allocs_item ON inv_order_allocs (order_item_id)
"""


class InventoryError(ValueError):
    """A request that cannot be done (not enough stock, a wrong status...); the message is for staff."""


def _dec(value: Any, what: str) -> Decimal:
    try:
        d = Decimal(str(value).strip().replace(",", "")) if value not in (None, "") else Decimal(0)
    except InvalidOperation:
        raise InventoryError(f"{what}: không phải là số") from None
    if not d.is_finite():
        raise InventoryError(f"{what}: không phải là số")
    return d


def _int(value: Any, what: str, minimum: int | None = 0) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        raise InventoryError(f"{what}: phải là số nguyên") from None
    if minimum is not None and n < minimum:
        raise InventoryError(f"{what}: phải từ {minimum} trở lên")
    return n


def _today() -> str:
    return datetime.now().astimezone().date().isoformat()


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.astimezone()


def order_code(order_id: int) -> str:
    return f"DH{order_id:05d}"


def transfer_code(transfer_id: int) -> str:
    return f"CK{transfer_id:05d}"


class Inventory:
    def __init__(self, db: Database, docs: DocStore):
        self.db = db
        self.docs = docs
        db.script(SCHEMA)

    # ------------------------------------------------------------------ #
    # settings and money

    def settings(self) -> dict[str, Any]:
        saved = self.docs.get(SETTINGS_KEY) or {}
        out = {**DEFAULT_SETTINGS, **saved}
        out["rules"] = {**DEFAULT_RULES, **(saved.get("rules") or {})}
        return out

    def save_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        current = self.settings()
        clean: dict[str, Any] = {}
        if "currency" in changes:
            clean["currency"] = str(changes["currency"]).strip().upper()[:8] or "VND"
        if "decimals" in changes:
            decimals = _int(changes["decimals"], "Số chữ số thập phân")
            if decimals > 4:
                raise InventoryError("Số chữ số thập phân tối đa là 4")
            if decimals != current["decimals"] and self.db.row("SELECT 1 AS x FROM inv_lots LIMIT 1"):
                raise InventoryError("Không đổi được số chữ số thập phân khi đã có hàng nhập")
            clean["decimals"] = decimals
        for key, what, lo, hi in (
            ("round_to", "Làm tròn giá", 0, None),
            ("price_hour", "Giờ chạy định giá", 0, 23),
            ("velocity_days", "Số ngày tính tốc độ bán", 1, 365),
            ("safety_days", "Số ngày tồn an toàn", 0, 365),
            ("cover_days", "Số ngày hàng cho mỗi lần đặt", 0, 730),
        ):
            if key in changes:
                n = _int(changes[key], what, lo)
                if hi is not None and n > hi:
                    raise InventoryError(f"{what}: tối đa {hi}")
                clean[key] = n
        if "default_margin_pct" in changes:
            margin = float(_dec(changes["default_margin_pct"], "Lãi mục tiêu"))
            if not 0 <= margin < 100:
                raise InventoryError("Lãi mục tiêu phải từ 0 đến dưới 100%")
            clean["default_margin_pct"] = margin
        if "stage_discounts" in changes:
            clean["stage_discounts"] = self._discounts(changes["stage_discounts"])
        if "rules" in changes:
            clean["rules"] = self._rules(changes["rules"], partial=False)

        def change(doc: dict[str, Any]) -> None:
            doc.update(clean)

        self.docs.update(SETTINGS_KEY, change, {})
        return self.settings()

    @staticmethod
    def _discounts(value: Any) -> list[float]:
        if not isinstance(value, list) or len(value) != 5:
            raise InventoryError("Cần đúng 5 mức giảm giá (giai đoạn 1-5)")
        out = [float(_dec(v, "Mức giảm")) for v in value]
        if out[0] != 0 or any(not 0 <= v < 100 for v in out) or out != sorted(out):
            raise InventoryError("Mức giảm: giai đoạn 1 là 0%, các giai đoạn sau tăng dần, dưới 100%")
        return out

    @staticmethod
    def _rules(value: Any, partial: bool) -> dict[str, dict[str, float]]:
        if not isinstance(value, dict):
            raise InventoryError("Quy tắc chuyển giai đoạn không hợp lệ")
        out = {}
        for stage, rule in value.items():
            if str(stage) not in DEFAULT_RULES or not isinstance(rule, dict):
                raise InventoryError("Quy tắc chuyển giai đoạn: chỉ có giai đoạn 1 đến 4")
            target = float(_dec(rule.get("target_pct"), "Ngưỡng tồn"))
            lo = _int(rule.get("min_days"), "Số ngày tối thiểu")
            hi = _int(rule.get("max_days"), "Số ngày tối đa")
            if not 0 <= target <= 100 or hi < lo:
                raise InventoryError("Ngưỡng tồn 0-100%, số ngày tối đa không nhỏ hơn tối thiểu")
            out[str(stage)] = {"target_pct": target, "min_days": lo, "max_days": hi}
        if not partial and set(out) != set(DEFAULT_RULES):
            raise InventoryError("Cần quy tắc cho cả 4 lần chuyển giai đoạn")
        return out

    def minor(self, amount: Any, what: str = "Số tiền") -> int:
        """An amount typed by staff (major units, e.g. 4500000 or 12.5) to minor units."""
        d = _dec(amount, what)
        if d < 0:
            raise InventoryError(f"{what}: không được âm")
        return int((d * (10 ** self.settings()["decimals"])).quantize(Decimal(1), ROUND_HALF_UP))

    def major(self, minor: int | None) -> float | int | None:
        if minor is None:
            return None
        decimals = self.settings()["decimals"]
        return int(minor) if decimals == 0 else round(int(minor) / 10**decimals, decimals)

    def _round_price(self, value: Decimal) -> int:
        """Minor units, rounded to the configured step (1.000 đ by default)."""
        s = self.settings()
        step = max(1, int(s["round_to"]) * 10 ** int(s["decimals"]))  # round_to is in whole units; 0: none
        return int((value / step).quantize(Decimal(1), ROUND_HALF_UP) * step)

    # ------------------------------------------------------------------ #
    # pricing arithmetic (pure)

    def landed_costs(
        self, items: list[dict[str, Any]], exchange_rate: Any, freight: int, customs: int
    ) -> list[dict[str, int]]:
        """Per item: cost in the office currency, its share of freight and customs, landed cost.
        Freight and customs are shared by volume (CBM); by value when no volumes are known."""
        rate = _dec(exchange_rate, "Tỷ giá")
        if rate <= 0:
            raise InventoryError("Tỷ giá phải lớn hơn 0")
        scale = Decimal(10) ** self.settings()["decimals"]
        rows = []
        for it in items:
            qty = Decimal(_int(it["qty"], "Số lượng", 1))
            cost = (_dec(it.get("unit_cost_foreign"), "Giá mua") * rate * scale).quantize(
                Decimal(1), ROUND_HALF_UP
            )
            rows.append((qty, _dec(it.get("unit_cbm"), "Thể tích (CBM)"), cost))
        total_cbm = sum((q * c for q, c, _ in rows), Decimal(0))
        total_value = sum((q * v for q, _, v in rows), Decimal(0))
        out = []
        for qty, cbm, cost in rows:
            share = (
                cbm / total_cbm
                if total_cbm > 0
                else (cost / total_value if total_value > 0 else 1 / Decimal(len(rows)) / qty)
            )
            unit_freight = (Decimal(freight) * share).quantize(Decimal(1), ROUND_HALF_UP)
            unit_tax = (Decimal(customs) * share).quantize(Decimal(1), ROUND_HALF_UP)
            out.append(
                {
                    "unit_cost": int(cost),
                    "unit_freight": int(unit_freight),
                    "unit_tax": int(unit_tax),
                    "landed_cost": int(cost + unit_freight + unit_tax),
                }
            )
        return out

    def price_plan(self, landed_cost: int, margin_pct: Any = None, price1: Any = None) -> dict[str, Any]:
        """Stage-1 price from a target margin (price = cost / (1 - margin)), or the margin from
        a stage-1 price; stages 2-5 from the stage discounts."""
        s = self.settings()
        if price1 not in (None, ""):
            p1 = self.minor(price1, "Giá giai đoạn 1")
        else:
            margin = _dec(
                margin_pct if margin_pct not in (None, "") else s["default_margin_pct"], "Lãi mục tiêu"
            )
            if not 0 <= margin < 100:
                raise InventoryError("Lãi mục tiêu phải từ 0 đến dưới 100%")
            p1 = self._round_price(Decimal(landed_cost) / (1 - margin / 100))
        prices = [p1] + [
            self._round_price(Decimal(p1) * (1 - Decimal(str(d)) / 100)) for d in s["stage_discounts"][1:]
        ]
        margin_now = (Decimal(p1 - landed_cost) / p1 * 100) if p1 else Decimal(0)
        return {
            "landed_cost": landed_cost,
            "prices": prices,
            "profit": p1 - landed_cost,
            "margin_pct": float(margin_now.quantize(Decimal("0.01"), ROUND_HALF_UP)),
            "below_cost": [i + 1 for i, p in enumerate(prices) if p < landed_cost],
        }

    # ------------------------------------------------------------------ #
    # warehouses and suppliers

    def warehouses(self, active_only: bool = False) -> list[dict[str, Any]]:
        sql = (
            "SELECT * FROM inv_warehouses"
            + (" WHERE active=1" if active_only else "")
            + " ORDER BY kind DESC, name"
        )
        return self.db.rows(sql)

    def warehouse(self, wid: int) -> dict[str, Any]:
        row = self.db.row("SELECT * FROM inv_warehouses WHERE id=?", (wid,))
        if row is None:
            raise InventoryError("Không có kho này")
        return row

    def default_warehouse(self) -> dict[str, Any]:
        row = self.db.row("SELECT * FROM inv_warehouses WHERE active=1 ORDER BY kind='store', id LIMIT 1")
        if row is None:
            raise InventoryError("Chưa có kho nào: thêm kho trong trang Kho hàng")
        return row

    def save_warehouse(self, wid: int | None, data: dict[str, Any]) -> dict[str, Any]:
        fields = {}
        if "code" in data or wid is None:
            fields["code"] = str(data.get("code") or "").strip().upper()[:20]
            if not fields["code"]:
                raise InventoryError("Mã kho là bắt buộc")
        if "name" in data or wid is None:
            fields["name"] = str(data.get("name") or "").strip()[:120]
            if not fields["name"]:
                raise InventoryError("Tên kho là bắt buộc")
        if "kind" in data:
            if data["kind"] not in ("warehouse", "store"):
                raise InventoryError("Loại kho là warehouse (kho tổng) hoặc store (cửa hàng)")
            fields["kind"] = data["kind"]
        if "address" in data:
            fields["address"] = str(data["address"] or "").strip()[:300]
        if "active" in data:
            fields["active"] = 1 if data["active"] else 0
        return self._save("inv_warehouses", wid, fields, "kho")

    def suppliers(self) -> list[dict[str, Any]]:
        return self.db.rows("SELECT * FROM inv_suppliers ORDER BY active DESC, name")

    def save_supplier(self, sid: int | None, data: dict[str, Any]) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        for key, limit in (
            ("name", 200),
            ("country", 100),
            ("contact_name", 100),
            ("phone", 40),
            ("email", 200),
            ("payment_terms", 500),
        ):
            if key in data or (sid is None and key == "name"):
                fields[key] = str(data.get(key) or "").strip()[:limit]
        if "name" in fields and not fields["name"]:
            raise InventoryError("Tên nhà cung cấp là bắt buộc")
        if "lead_time_days" in data:
            fields["lead_time_days"] = _int(data["lead_time_days"], "Thời gian giao hàng (ngày)")
        if "active" in data:
            fields["active"] = 1 if data["active"] else 0
        return self._save("inv_suppliers", sid, fields, "nhà cung cấp")

    def _save(self, table: str, rid: int | None, fields: dict[str, Any], what: str) -> dict[str, Any]:
        try:
            if rid is None:
                fields["created"] = now_iso()
                cols = ", ".join(fields)
                rid = self.db.execute(
                    f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(fields))}) RETURNING id",
                    list(fields.values()),
                )
            elif fields:
                sets = ", ".join(f"{k}=?" for k in fields)
                self.db.execute(f"UPDATE {table} SET {sets} WHERE id=?", [*fields.values(), rid])
        except IntegrityError:
            raise InventoryError(f"Mã {what} đã tồn tại") from None
        row = self.db.row(f"SELECT * FROM {table} WHERE id=?", (rid,))
        if row is None:
            raise InventoryError(f"Không có {what} này")
        return row

    # ------------------------------------------------------------------ #
    # products

    PRODUCT_FIELDS = ("sku", "name", "category", "group_name", "unit", "description")

    def save_product(self, pid: int | None, data: dict[str, Any]) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        for key in self.PRODUCT_FIELDS:
            if key in data or (pid is None and key in ("sku", "name")):
                limit = 4000 if key == "description" else 60 if key == "sku" else 200
                fields[key] = str(data.get(key) or "").strip()[:limit]
        if "sku" in fields:
            fields["sku"] = fields["sku"].upper()
            if not fields["sku"]:
                raise InventoryError("Mã SKU là bắt buộc")
        if "name" in fields and not fields["name"]:
            raise InventoryError("Tên sản phẩm là bắt buộc")
        if "attributes" in data:
            attrs = data["attributes"] or {}
            if not isinstance(attrs, dict):
                raise InventoryError("Thuộc tính phải là danh sách tên: giá trị")
            fields["attributes"] = json.dumps(
                {str(k)[:40]: str(v)[:120] for k, v in attrs.items()}, ensure_ascii=False
            )
        for key, what in (("cbm", "Thể tích (CBM)"), ("weight_kg", "Cân nặng")):
            if key in data:
                d = _dec(data[key], what)
                if d < 0:
                    raise InventoryError(f"{what}: không được âm")
                fields[key] = str(d)
        if "supplier_id" in data:
            fields["supplier_id"] = int(data["supplier_id"]) if data["supplier_id"] else None
        if "safety_stock" in data:
            fields["safety_stock"] = (
                -1 if data["safety_stock"] in (None, "", -1) else _int(data["safety_stock"], "Tồn an toàn")
            )
        if "vip_price" in data:
            fields["vip_price"] = (
                self.minor(data["vip_price"], "Giá VIP") if data["vip_price"] not in (None, "") else None
            )
        if "auto_pricing" in data:
            fields["auto_pricing"] = 1 if data["auto_pricing"] else 0
        if "rules" in data:
            fields["rules"] = json.dumps(self._rules(data["rules"], partial=True)) if data["rules"] else ""
        if "active" in data:
            fields["active"] = 1 if data["active"] else 0
        fields["updated"] = now_iso()
        return self._save("inv_products", pid, fields, "SKU")

    def product_row(self, pid: int) -> dict[str, Any]:
        row = self.db.row("SELECT * FROM inv_products WHERE id=?", (pid,))
        if row is None:
            raise InventoryError("Không có sản phẩm này")
        return row

    def by_sku(self, sku: str) -> dict[str, Any] | None:
        return self.db.row("SELECT * FROM inv_products WHERE sku=?", (sku.strip().upper(),))

    def _lots(self, pid: int, open_only: bool = True) -> list[dict[str, Any]]:
        where = " AND status IN ('active','queued')" if open_only else ""
        return self.db.rows(
            f"SELECT * FROM inv_lots WHERE product_id=?{where} "
            "ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, received_date, id",
            (pid,),
        )

    def pricing_lot(self, pid: int) -> dict[str, Any] | None:
        """The lot whose price the product sells at: the active one, else the next in line."""
        lots = self._lots(pid)
        return lots[0] if lots else None

    def current_price(self, pid: int, vip: bool = False) -> dict[str, Any] | None:
        """{price, stage, lot_id, vip}: the active lot's stage price. VIP customers: the VIP
        price if one is set, else the next stage's price (never more than the normal price)."""
        lot = self.pricing_lot(pid)
        if lot is None:
            return None
        stage = int(lot["stage"])
        price = int(lot[f"price{stage}"])
        if vip:
            product = self.product_row(pid)
            special = lot["vip_price"] if lot["vip_price"] is not None else product["vip_price"]
            candidate = int(special) if special is not None else int(lot[f"price{min(stage + 1, 5)}"])
            if candidate < price:
                return {"price": candidate, "stage": stage, "lot_id": lot["id"], "vip": True}
        return {"price": price, "stage": stage, "lot_id": lot["id"], "vip": False}

    def _stock_rows(self, pid: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT s.*, w.code, w.name AS warehouse_name, w.kind FROM inv_stock s JOIN inv_warehouses w ON w.id=s.warehouse_id"
        return self.db.rows(sql + (" WHERE s.product_id=?" if pid else ""), (pid,) if pid else ())

    def _incoming(self, pid: int | None = None) -> list[dict[str, Any]]:
        """Goods still to arrive on open purchase orders (and how much is pre-sold)."""
        sql = (
            "SELECT i.id AS po_item_id, i.product_id, i.po_id, p.po_number, p.eta, p.status, p.warehouse_id, "
            "i.qty_ordered - i.qty_received - i.qty_damaged AS qty, i.qty_preordered, i.price1 "
            "FROM inv_po_items i JOIN inv_purchase_orders p ON p.id=i.po_id "
            "WHERE p.status IN ('ordered','shipping','arrived','partial') "
            "AND i.qty_ordered - i.qty_received - i.qty_damaged > 0"
        )
        return self.db.rows(
            sql + (" AND i.product_id=? ORDER BY p.eta, p.id" if pid else " ORDER BY p.eta, p.id"),
            (pid,) if pid else (),
        )

    def products(
        self, query: str = "", include_inactive: bool = False, limit: int = 500
    ) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM inv_products WHERE 1=1", []
        if not include_inactive:
            sql += " AND active=1"
        if query.strip():
            like = f"%{query.strip().lower()}%"
            sql += " AND (LOWER(sku) LIKE ? OR LOWER(name) LIKE ? OR LOWER(category) LIKE ? OR LOWER(group_name) LIKE ?)"
            args += [like] * 4
        rows = self.db.rows(sql + " ORDER BY group_name, sku LIMIT ?", [*args, limit])
        if not rows:
            return []
        stock: dict[int, list[dict[str, Any]]] = {}
        for s in self._stock_rows():
            stock.setdefault(int(s["product_id"]), []).append(s)
        incoming: dict[int, list[dict[str, Any]]] = {}
        for i in self._incoming():
            incoming.setdefault(int(i["product_id"]), []).append(i)
        velocity = self._velocity()
        leads = self._lead_times()
        return [
            self._summary(p, stock.get(p["id"], []), incoming.get(p["id"], []), velocity, leads) for p in rows
        ]

    def _summary(
        self,
        p: dict[str, Any],
        stock: list[dict[str, Any]],
        incoming: list[dict[str, Any]],
        velocity: dict[int, float],
        leads: dict[int, int],
    ) -> dict[str, Any]:
        s = self.settings()
        on_hand = sum(int(x["on_hand"]) for x in stock)
        reserved = sum(int(x["reserved"]) for x in stock)
        available = on_hand - reserved
        coming = sum(int(x["qty"]) for x in incoming)
        preordered = sum(int(x["qty_preordered"]) for x in incoming)
        daily = velocity.get(int(p["id"]), 0.0)
        lead = leads.get(int(p["id"]), 30)
        safety = (
            int(p["safety_stock"]) if int(p["safety_stock"]) >= 0 else math.ceil(daily * s["safety_days"])
        )
        rop = math.ceil(daily * lead + safety)
        position = available + coming - preordered
        if available <= 0:
            level = "empty"
        elif daily > 0 and position <= rop:
            level = "reorder"
        elif daily > 0 and available / daily < 30:
            level = "low"
        elif daily > 0 and available / daily <= 60:
            level = "medium"
        else:
            level = "good"
        suggest = (
            max(0, math.ceil(daily * (lead + s["cover_days"])) + safety - position)
            if daily > 0 and position <= rop
            else 0
        )
        price = self.current_price(int(p["id"]))
        lot = self.pricing_lot(int(p["id"]))
        return {
            **{
                k: p[k]
                for k in (
                    "id",
                    "sku",
                    "name",
                    "category",
                    "group_name",
                    "unit",
                    "active",
                    "auto_pricing",
                    "supplier_id",
                )
            },
            "attributes": json.loads(p["attributes"] or "{}"),
            "vip_price": self.major(p["vip_price"]),
            "on_hand": on_hand,
            "reserved": reserved,
            "available": available,
            "incoming": coming,
            "preordered": preordered,
            "next_eta": min((x["eta"] for x in incoming if x["eta"]), default=""),
            "price": self.major(price["price"]) if price else None,
            "stage": price["stage"] if price else None,
            "landed_cost": self.major(lot["landed_cost"]) if lot else None,
            "daily_sales": round(daily, 2),
            "lead_time_days": lead,
            "reorder_point": rop,
            "safety_stock": safety,
            "level": level,
            "suggest_order": suggest,
            "by_warehouse": [
                {
                    "warehouse_id": x["warehouse_id"],
                    "code": x["code"],
                    "on_hand": x["on_hand"],
                    "reserved": x["reserved"],
                }
                for x in stock
                if x["on_hand"] or x["reserved"]
            ],
        }

    def _velocity(self) -> dict[int, float]:
        days = self.settings()["velocity_days"]
        since = (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")
        return {
            int(r["product_id"]): -int(r["q"]) / days
            for r in self.db.rows(
                "SELECT product_id, SUM(qty) AS q FROM inv_moves WHERE kind='sale_out' AND ts>=? GROUP BY product_id",
                (since,),
            )
        }

    def _lead_times(self) -> dict[int, int]:
        """The product's own supplier, else the supplier of its latest purchase order."""
        out = {}
        for r in self.db.rows(
            "SELECT p.id, s.lead_time_days FROM inv_products p JOIN inv_suppliers s ON s.id=p.supplier_id"
        ):
            out[int(r["id"])] = int(r["lead_time_days"])
        for r in self.db.rows(
            "SELECT i.product_id, s.lead_time_days FROM inv_po_items i JOIN inv_purchase_orders o ON o.id=i.po_id "
            "JOIN inv_suppliers s ON s.id=o.supplier_id ORDER BY o.id"
        ):
            out.setdefault(int(r["product_id"]), int(r["lead_time_days"]))
        return out

    def product(self, pid: int) -> dict[str, Any]:
        p = self.product_row(pid)
        summary = self._summary(
            p, self._stock_rows(pid), self._incoming(pid), self._velocity(), self._lead_times()
        )
        lots = [self._lot_json(x) for x in self._lots(pid, open_only=False)[:50]]
        log = [
            self._log_json(x)
            for x in self.db.rows(
                "SELECT * FROM inv_price_log WHERE product_id=? ORDER BY id DESC LIMIT 50", (pid,)
            )
        ]
        moves = self.moves(product_id=pid, limit=50)
        incoming = [
            {
                "po_id": x["po_id"],
                "po_number": x["po_number"],
                "eta": x["eta"],
                "qty": x["qty"],
                "preordered": x["qty_preordered"],
                "po_item_id": x["po_item_id"],
            }
            for x in self._incoming(pid)
        ]
        rules = json.loads(p["rules"]) if p["rules"] else {}
        return {
            **summary,
            "description": p["description"],
            "cbm": p["cbm"],
            "weight_kg": p["weight_kg"],
            "safety_stock_setting": p["safety_stock"],
            "rules": rules,
            "lots": lots,
            "price_log": log,
            "moves": moves,
            "incoming_orders": incoming,
        }

    def _lot_json(self, lot: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": lot["id"],
            "status": lot["status"],
            "stage": lot["stage"],
            "source": lot["source"],
            "received_date": lot["received_date"],
            "initial_qty": lot["initial_qty"],
            "remaining_qty": lot["remaining_qty"],
            "reserved_qty": lot["reserved_qty"],
            "preordered_qty": lot["preordered_qty"],
            "landed_cost": self.major(lot["landed_cost"]),
            "activated_at": lot["activated_at"],
            "stage_since": lot["stage_since"],
            "prices": [self.major(lot[f"price{i}"]) for i in range(1, 6)],
            "vip_price": self.major(lot["vip_price"]),
            "remaining_pct": self._remaining_pct(lot),
        }

    def _log_json(self, r: dict[str, Any]) -> dict[str, Any]:
        return {**r, "old_price": self.major(r["old_price"]), "new_price": self.major(r["new_price"])}

    # CSV import / export (opens in Excel)

    CSV_COLUMNS = (
        "sku",
        "name",
        "category",
        "group_name",
        "unit",
        "cbm",
        "weight_kg",
        "vip_price",
        "safety_stock",
        "description",
    )

    def export_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([*self.CSV_COLUMNS, "on_hand", "available", "incoming", "price", "stage", "landed_cost"])
        for p in self.products(include_inactive=True, limit=100000):
            row = self.product_row(int(p["id"]))
            w.writerow(
                [
                    p["sku"],
                    p["name"],
                    p["category"],
                    p["group_name"],
                    p["unit"],
                    row["cbm"],
                    row["weight_kg"],
                    p["vip_price"] if p["vip_price"] is not None else "",
                    row["safety_stock"] if row["safety_stock"] >= 0 else "",
                    row["description"],
                    p["on_hand"],
                    p["available"],
                    p["incoming"],
                    p["price"] if p["price"] is not None else "",
                    p["stage"] or "",
                    p["landed_cost"] if p["landed_cost"] is not None else "",
                ]
            )
        return "﻿" + buf.getvalue()  # BOM: Excel reads it as UTF-8

    def import_csv(self, text: str) -> dict[str, Any]:
        reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
        if not reader.fieldnames or "sku" not in [f.strip().lower() for f in reader.fieldnames]:
            raise InventoryError("Tệp cần có cột sku (và name cho sản phẩm mới)")
        created = updated = 0
        errors = []
        for n, raw in enumerate(reader, start=2):
            row = {str(k).strip().lower(): (v or "").strip() for k, v in raw.items() if k}
            data = {k: row[k] for k in self.CSV_COLUMNS if k in row and row[k] != ""}
            try:
                existing = self.by_sku(row.get("sku", ""))
                if existing:
                    data.pop("sku", None)
                    self.save_product(int(existing["id"]), data)
                    updated += 1
                else:
                    self.save_product(None, data)
                    created += 1
            except InventoryError as e:
                errors.append(f"dòng {n}: {e}")
            if n > 20001:
                errors.append("tối đa 20.000 dòng mỗi lần")
                break
        return {"created": created, "updated": updated, "errors": errors[:50]}

    # ------------------------------------------------------------------ #
    # stock movements

    def _move(
        self,
        wh: int,
        pid: int,
        kind: str,
        qty: int,
        ref_type: str = "",
        ref_id: int | None = None,
        lot_id: int | None = None,
        actor: str = "",
        note: str = "",
    ) -> None:
        self.db.execute(
            "INSERT INTO inv_moves (ts, warehouse_id, product_id, lot_id, kind, qty, ref_type, ref_id, actor, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now_iso(), wh, pid, lot_id, kind, qty, ref_type, ref_id, actor, note[:300]),
        )

    def _add_on_hand(self, wh: int, pid: int, qty: int) -> None:
        self.db.execute(
            "INSERT INTO inv_stock (warehouse_id, product_id, on_hand, reserved) VALUES (?, ?, ?, 0) "
            "ON CONFLICT (warehouse_id, product_id) DO UPDATE SET on_hand = inv_stock.on_hand + excluded.on_hand",
            (wh, pid, qty),
        )

    def _take_on_hand(self, wh: int, pid: int, qty: int, from_reserved: bool) -> bool:
        """Remove stock; unreserved stock only unless it was reserved for this."""
        if from_reserved:
            return (
                self.db.execute(
                    "UPDATE inv_stock SET on_hand=on_hand-?, reserved=reserved-? "
                    "WHERE warehouse_id=? AND product_id=? AND reserved>=? AND on_hand>=? RETURNING warehouse_id",
                    (qty, qty, wh, pid, qty, qty),
                )
                is not None
            )
        return (
            self.db.execute(
                "UPDATE inv_stock SET on_hand=on_hand-? WHERE warehouse_id=? AND product_id=? AND on_hand-reserved>=? "
                "RETURNING warehouse_id",
                (qty, wh, pid, qty),
            )
            is not None
        )

    def _reserve(self, wh: int, pid: int, qty: int) -> bool:
        return (
            self.db.execute(
                "UPDATE inv_stock SET reserved=reserved+? WHERE warehouse_id=? AND product_id=? AND on_hand-reserved>=? "
                "RETURNING warehouse_id",
                (qty, wh, pid, qty),
            )
            is not None
        )

    def _available(self, wh: int, pid: int) -> int:
        row = self.db.row(
            "SELECT on_hand-reserved AS a FROM inv_stock WHERE warehouse_id=? AND product_id=?", (wh, pid)
        )
        return int(row["a"]) if row else 0

    def _new_lot(
        self,
        pid: int,
        wh: int,
        qty: int,
        landed: int,
        prices: list[int],
        vip: int | None,
        po_item_id: int | None,
        source: str,
        received: str,
    ) -> int:
        """A received lot: active at once if the product has nothing else to sell, else queued."""
        active = self.db.row(
            "SELECT id FROM inv_lots WHERE product_id=? AND status IN ('active','queued') LIMIT 1", (pid,)
        )
        now = now_iso()
        status = "queued" if active else "active"
        lot_id = self.db.execute(
            "INSERT INTO inv_lots (product_id, warehouse_id, po_item_id, source, initial_qty, remaining_qty, landed_cost, "
            "stage, status, received_date, activated_at, stage_since, price1, price2, price3, price4, price5, vip_price, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (
                pid,
                wh,
                po_item_id,
                source,
                qty,
                qty,
                landed,
                status,
                received,
                now if status == "active" else None,
                now if status == "active" else None,
                *prices,
                vip,
                now,
            ),
        )
        assert lot_id is not None
        if status == "active":
            self._log(
                pid,
                lot_id,
                None,
                1,
                None,
                prices[0],
                "activate",
                "",
                reason="Lô đầu tiên: bán giá giai đoạn 1",
            )
        return lot_id

    def add_opening_stock(
        self,
        pid: int,
        wh: int,
        qty: int,
        unit_cost: Any,
        margin_pct: Any = None,
        price1: Any = None,
        actor: str = "",
    ) -> dict[str, Any]:
        """Stock already on the shelves before purchasing was tracked here: a lot of its own."""
        self.product_row(pid)
        self.warehouse(wh)
        qty = _int(qty, "Số lượng", 1)
        landed = self.minor(unit_cost, "Giá vốn")
        plan = self.price_plan(landed, margin_pct, price1)
        with self.db.transaction():
            lot = self._new_lot(pid, wh, qty, landed, plan["prices"], None, None, "opening", _today())
            self._add_on_hand(wh, pid, qty)
            self._move(wh, pid, "opening", qty, "lot", lot, lot, actor, "Tồn đầu kỳ")
        return self.product(pid)

    def adjust(
        self, pid: int, wh: int, counted: int, reason: str, actor: str = "", unit_cost: Any = None
    ) -> dict[str, Any]:
        """A stock count: set the quantity on hand. Losses come out of the oldest free lots;
        extra goods become a lot at the given (or the latest) cost."""
        self.product_row(pid)
        self.warehouse(wh)
        counted = _int(counted, "Số lượng kiểm đếm")
        if not reason.strip():
            raise InventoryError("Cần ghi lý do điều chỉnh")
        with self.db.transaction():
            row = self.db.row(
                "SELECT on_hand, reserved FROM inv_stock WHERE warehouse_id=? AND product_id=?", (wh, pid)
            )
            on_hand, reserved = (int(row["on_hand"]), int(row["reserved"])) if row else (0, 0)
            delta = counted - on_hand
            if delta == 0:
                return self.product(pid)
            if counted < reserved:
                raise InventoryError(f"Đang giữ {reserved} cho đơn hàng: không đặt tồn thấp hơn số đó")
            if delta < 0:
                self._take_on_hand(wh, pid, -delta, from_reserved=False)
                left = -delta
                for lot in self._lots(pid):
                    free = int(lot["remaining_qty"]) - int(lot["reserved_qty"])
                    take = min(left, free)
                    if take > 0:
                        self.db.execute(
                            "UPDATE inv_lots SET remaining_qty=remaining_qty-? WHERE id=?", (take, lot["id"])
                        )
                        left -= take
                    if left == 0:
                        break
                self._move(wh, pid, "adjust", delta, actor=actor, note=reason)
            else:
                lot = self.pricing_lot(pid)
                landed = (
                    self.minor(unit_cost, "Giá vốn")
                    if unit_cost not in (None, "")
                    else int(lot["landed_cost"])
                    if lot
                    else 0
                )
                prices = (
                    [int(lot[f"price{i}"]) for i in range(1, 6)] if lot else self.price_plan(landed)["prices"]
                )
                lot_id = self._new_lot(
                    pid,
                    wh,
                    delta,
                    landed,
                    prices,
                    lot["vip_price"] if lot else None,
                    None,
                    "adjust",
                    _today(),
                )
                self._add_on_hand(wh, pid, delta)
                self._move(wh, pid, "adjust", delta, "lot", lot_id, lot_id, actor, reason)
        return self.product(pid)

    def moves(self, product_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql = (
            "SELECT m.*, w.code AS warehouse_code, p.sku FROM inv_moves m JOIN inv_warehouses w ON w.id=m.warehouse_id "
            "JOIN inv_products p ON p.id=m.product_id"
        )
        args: list[Any] = []
        if product_id:
            sql += " WHERE m.product_id=?"
            args.append(product_id)
        return self.db.rows(sql + " ORDER BY m.id DESC LIMIT ?", [*args, limit])

    # ------------------------------------------------------------------ #
    # purchase orders

    def save_po(self, po_id: int | None, data: dict[str, Any], actor: str = "") -> dict[str, Any]:
        """Create or edit a purchase order (while it is a draft or ordered): header and items.
        Landed costs are computed; stage prices come from each item's margin or stage-1 price,
        or are given explicitly (`prices`: 5 numbers)."""
        existing = self.db.row("SELECT * FROM inv_purchase_orders WHERE id=?", (po_id,)) if po_id else None
        if po_id and existing is None:
            raise InventoryError("Không có đơn nhập này")
        if existing and existing["status"] not in ("draft", "ordered"):
            raise InventoryError("Chỉ sửa được đơn nhập chưa lên đường")
        head = existing or {}
        supplier = _int(data.get("supplier_id", head.get("supplier_id")), "Nhà cung cấp", 1)
        if not self.db.row("SELECT 1 AS x FROM inv_suppliers WHERE id=?", (supplier,)):
            raise InventoryError("Không có nhà cung cấp này")
        wh = _int(data.get("warehouse_id", head.get("warehouse_id")), "Kho nhận", 1)
        self.warehouse(wh)
        rate = str(_dec(data.get("exchange_rate", head.get("exchange_rate", "1")), "Tỷ giá"))
        freight = (
            self.minor(data["freight"], "Cước container")
            if "freight" in data
            else int(head.get("freight", 0))
        )
        customs = (
            self.minor(data["customs"], "Thuế, phí hải quan")
            if "customs" in data
            else int(head.get("customs", 0))
        )
        eta = str(data.get("eta", head.get("eta", "")) or "")[:10]
        if eta:
            try:
                date.fromisoformat(eta)
            except ValueError:
                raise InventoryError("Ngày dự kiến về phải có dạng YYYY-MM-DD") from None
        header = {
            "po_number": str(data.get("po_number") or head.get("po_number") or "").strip()[:40],
            "supplier_id": supplier,
            "warehouse_id": wh,
            "container_code": str(data.get("container_code", head.get("container_code", "")) or "")[:40],
            "eta": eta,
            "currency": str(data.get("currency", head.get("currency", "USD")) or "USD").upper()[:8],
            "exchange_rate": rate,
            "freight": freight,
            "customs": customs,
            "notes": str(data.get("notes", head.get("notes", "")) or "")[:2000],
            "updated": now_iso(),
        }
        items = data.get("items")
        if items is None and existing:
            items = [
                {
                    "product_id": i["product_id"],
                    "qty": i["qty_ordered"],
                    "unit_cost_foreign": i["unit_cost_foreign"],
                    "unit_cbm": i["unit_cbm"],
                    "margin_pct": i["margin_pct"] or None,
                    "prices": [self.major(i[f"price{k}"]) for k in range(1, 6)],
                    "vip_price": self.major(i["vip_price"]),
                }
                for i in self._po_items(po_id)
            ]
        if not isinstance(items, list) or not items:
            raise InventoryError("Đơn nhập cần ít nhất một sản phẩm")
        clean = []
        for it in items:
            pid = _int(it.get("product_id"), "Sản phẩm", 1) if it.get("product_id") else None
            if pid is None and it.get("sku"):
                p = self.by_sku(str(it["sku"]))
                if p is None:
                    raise InventoryError(f"Không có SKU {it['sku']}")
                pid = int(p["id"])
            if pid is None:
                raise InventoryError("Mỗi dòng cần sản phẩm")
            product = self.product_row(pid)
            cbm = it.get("unit_cbm") if it.get("unit_cbm") not in (None, "") else product["cbm"]
            clean.append(
                {**it, "product_id": pid, "qty": _int(it.get("qty"), "Số lượng", 1), "unit_cbm": cbm}
            )
        costs = self.landed_costs(clean, rate, freight, customs)
        rows = []
        for it, c in zip(clean, costs, strict=True):
            if it.get("prices"):
                if len(it["prices"]) != 5:
                    raise InventoryError("Cần đủ 5 giá giai đoạn")
                prices = [self.minor(p, "Giá giai đoạn") for p in it["prices"]]
                margin = ""
            else:
                plan = self.price_plan(c["landed_cost"], it.get("margin_pct"), it.get("price1"))
                prices, margin = plan["prices"], str(plan["margin_pct"])
            vip = self.minor(it["vip_price"], "Giá VIP") if it.get("vip_price") not in (None, "") else None
            rows.append((it, c, prices, margin, vip))
        with self.db.transaction():
            if existing is None:
                header.update(status="draft", created=now_iso(), created_by=actor)
                numbered = not header["po_number"]
                if numbered:  # PN00001, PN00002... from the row id
                    header["po_number"] = f"tmp-{uuid.uuid4().hex}"
                cols = ", ".join(header)
                try:
                    po_id = self.db.execute(
                        f"INSERT INTO inv_purchase_orders ({cols}) VALUES ({', '.join('?' * len(header))}) RETURNING id",
                        list(header.values()),
                    )
                except IntegrityError:
                    raise InventoryError("Số đơn nhập đã tồn tại") from None
                if numbered:
                    self.db.execute(
                        "UPDATE inv_purchase_orders SET po_number=? WHERE id=?", (f"PN{po_id:05d}", po_id)
                    )
            else:
                sets = ", ".join(f"{k}=?" for k in header)
                self.db.execute(
                    f"UPDATE inv_purchase_orders SET {sets} WHERE id=?", [*header.values(), po_id]
                )
                if self.db.row(
                    "SELECT 1 AS x FROM inv_po_items WHERE po_id=? AND qty_preordered>0", (po_id,)
                ):
                    raise InventoryError("Đơn nhập đã có khách đặt trước: không sửa danh sách hàng được")
                self.db.execute("DELETE FROM inv_po_items WHERE po_id=?", (po_id,))
            for it, c, prices, margin, vip in rows:
                self.db.execute(
                    "INSERT INTO inv_po_items (po_id, product_id, qty_ordered, unit_cost_foreign, unit_cbm, unit_cost, "
                    "unit_freight, unit_tax, landed_cost, margin_pct, price1, price2, price3, price4, price5, vip_price) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        po_id,
                        it["product_id"],
                        it["qty"],
                        str(_dec(it.get("unit_cost_foreign"), "Giá mua")),
                        str(_dec(it["unit_cbm"], "CBM")),
                        c["unit_cost"],
                        c["unit_freight"],
                        c["unit_tax"],
                        c["landed_cost"],
                        margin,
                        *prices,
                        vip,
                    ),
                )
        assert po_id is not None
        return self.po(int(po_id))

    def _po_items(self, po_id: int) -> list[dict[str, Any]]:
        return self.db.rows(
            "SELECT i.*, p.sku, p.name FROM inv_po_items i JOIN inv_products p ON p.id=i.product_id WHERE i.po_id=? ORDER BY i.id",
            (po_id,),
        )

    def po(self, po_id: int) -> dict[str, Any]:
        head = self.db.row(
            "SELECT o.*, s.name AS supplier_name, s.lead_time_days, w.name AS warehouse_name FROM inv_purchase_orders o "
            "JOIN inv_suppliers s ON s.id=o.supplier_id JOIN inv_warehouses w ON w.id=o.warehouse_id WHERE o.id=?",
            (po_id,),
        )
        if head is None:
            raise InventoryError("Không có đơn nhập này")
        items = []
        total_cbm = Decimal(0)
        for i in self._po_items(po_id):
            total_cbm += Decimal(i["unit_cbm"]) * i["qty_ordered"]
            items.append(
                {
                    "id": i["id"],
                    "product_id": i["product_id"],
                    "sku": i["sku"],
                    "name": i["name"],
                    "qty_ordered": i["qty_ordered"],
                    "qty_received": i["qty_received"],
                    "qty_damaged": i["qty_damaged"],
                    "qty_preordered": i["qty_preordered"],
                    "unit_cost_foreign": i["unit_cost_foreign"],
                    "unit_cbm": i["unit_cbm"],
                    "unit_cost": self.major(i["unit_cost"]),
                    "unit_freight": self.major(i["unit_freight"]),
                    "unit_tax": self.major(i["unit_tax"]),
                    "landed_cost": self.major(i["landed_cost"]),
                    "margin_pct": i["margin_pct"],
                    "prices": [self.major(i[f"price{k}"]) for k in range(1, 6)],
                    "vip_price": self.major(i["vip_price"]),
                    "below_cost": [k for k in range(1, 6) if i[f"price{k}"] < i["landed_cost"]],
                }
            )
        return {
            **head,
            "freight": self.major(head["freight"]),
            "customs": self.major(head["customs"]),
            "total_cbm": str(total_cbm),
            "items": items,
        }

    def pos(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT o.id, o.po_number, o.status, o.eta, o.arrived, o.container_code, o.created, s.name AS supplier_name, "
            "w.name AS warehouse_name, (SELECT COUNT(*) FROM inv_po_items i WHERE i.po_id=o.id) AS item_count, "
            "(SELECT SUM(i.qty_ordered) FROM inv_po_items i WHERE i.po_id=o.id) AS qty, "
            "(SELECT SUM(i.qty_ordered*i.landed_cost) FROM inv_po_items i WHERE i.po_id=o.id) AS value "
            "FROM inv_purchase_orders o JOIN inv_suppliers s ON s.id=o.supplier_id JOIN inv_warehouses w ON w.id=o.warehouse_id"
        )
        rows = self.db.rows(
            sql + (" WHERE o.status=?" if status else "") + " ORDER BY o.id DESC LIMIT 300",
            (status,) if status else (),
        )
        return [{**r, "value": self.major(int(r["value"] or 0))} for r in rows]

    def set_po_status(self, po_id: int, status: str) -> dict[str, Any]:
        head = self.po(po_id)
        allowed = {
            "draft": ("ordered", "cancelled"),
            "ordered": ("shipping", "arrived", "cancelled", "draft"),
            "shipping": ("arrived", "ordered"),
            "arrived": ("shipping",),
        }
        if status not in allowed.get(head["status"], ()):
            raise InventoryError(f"Không chuyển được đơn nhập từ '{head['status']}' sang '{status}'")
        if status in ("cancelled", "draft") and any(i["qty_preordered"] for i in head["items"]):
            raise InventoryError("Đơn nhập đã có khách đặt trước: huỷ các đơn đặt trước trước")
        self.db.execute(
            "UPDATE inv_purchase_orders SET status=?, updated=? WHERE id=?", (status, now_iso(), po_id)
        )
        return self.po(po_id)

    def receive_po(
        self, po_id: int, items: list[dict[str, Any]], actor: str = "", received: str | None = None
    ) -> dict[str, Any]:
        """Goods counted in: each item's good quantity becomes a FIFO lot (with the item's
        cost and stage prices) and stock in the order's warehouse; damaged goods are recorded.
        Pre-orders waiting for these goods are then served, oldest first."""
        head = self.po(po_id)
        if head["status"] not in ("ordered", "shipping", "arrived", "partial"):
            raise InventoryError("Đơn nhập này chưa đặt hàng hoặc đã nhận đủ")
        received = received or _today()
        by_id = {int(i["id"]): i for i in self._po_items(po_id)}
        if not items:
            raise InventoryError("Nhập số lượng thực nhận")
        served: list[int] = []
        with self.db.transaction():
            for r in items:
                item = by_id.get(_int(r.get("item_id"), "Dòng hàng", 1))
                if item is None:
                    raise InventoryError("Dòng hàng không thuộc đơn nhập này")
                good = _int(r.get("qty", 0), "Số lượng nhận")
                damaged = _int(r.get("damaged", 0), "Số lượng hỏng")
                outstanding = item["qty_ordered"] - item["qty_received"] - item["qty_damaged"]
                if good + damaged > outstanding:
                    raise InventoryError(
                        f"{item['sku']}: nhận {good + damaged} nhưng chỉ còn {outstanding} chưa về"
                    )
                if good + damaged == 0:
                    continue
                self.db.execute(
                    "UPDATE inv_po_items SET qty_received=qty_received+?, qty_damaged=qty_damaged+? WHERE id=?",
                    (good, damaged, item["id"]),
                )
                pid, wh = int(item["product_id"]), int(head["warehouse_id"])
                if good:
                    prices = [int(item[f"price{k}"]) for k in range(1, 6)]
                    lot = self._new_lot(
                        pid,
                        wh,
                        good,
                        int(item["landed_cost"]),
                        prices,
                        item["vip_price"],
                        int(item["id"]),
                        "po",
                        received,
                    )
                    self._add_on_hand(wh, pid, good)
                    self._move(
                        wh, pid, "stock_in", good, "po", po_id, lot, actor, f"Nhập từ {head['po_number']}"
                    )
                    served += self._serve_preorders(int(item["id"]), lot, wh, pid, actor)
                if damaged:
                    self._move(
                        wh,
                        pid,
                        "damaged",
                        0,
                        "po",
                        po_id,
                        None,
                        actor,
                        f"{damaged} hỏng khi nhận từ {head['po_number']}",
                    )
            left = self.db.row(
                "SELECT SUM(qty_ordered-qty_received-qty_damaged) AS n FROM inv_po_items WHERE po_id=?",
                (po_id,),
            )
            status = "received" if not int(left["n"] or 0) else "partial"
            self.db.execute(
                "UPDATE inv_purchase_orders SET status=?, arrived=?, updated=? WHERE id=?",
                (status, received, now_iso(), po_id),
            )
        out = self.po(po_id)
        out["served_preorders"] = sorted(set(served))
        return out

    def _serve_preorders(self, po_item_id: int, lot_id: int, wh: int, pid: int, actor: str) -> list[int]:
        """Allocate goods just received to the pre-orders waiting for them (whole lines, oldest first)."""
        served = []
        for line in self.db.rows(
            "SELECT i.* FROM inv_order_items i JOIN inv_orders o ON o.id=i.order_id "
            "WHERE i.po_item_id=? AND i.status='awaiting' AND o.status='confirmed' ORDER BY o.id, i.id",
            (po_item_id,),
        ):
            qty = int(line["qty"])
            ok = self.db.execute(
                "UPDATE inv_lots SET reserved_qty=reserved_qty+?, preordered_qty=preordered_qty+? "
                "WHERE id=? AND remaining_qty-reserved_qty>=? RETURNING id",
                (qty, qty, lot_id, qty),
            )
            if ok is None or not self._reserve(wh, pid, qty):
                break
            cost = self.db.row("SELECT landed_cost FROM inv_lots WHERE id=?", (lot_id,))
            self.db.execute(
                "INSERT INTO inv_order_allocs (order_item_id, lot_id, qty, unit_cost) VALUES (?, ?, ?, ?)",
                (line["id"], lot_id, qty, cost["landed_cost"]),
            )
            self.db.execute("UPDATE inv_order_items SET status='reserved' WHERE id=?", (line["id"],))
            self.db.execute(
                "UPDATE inv_po_items SET qty_preordered=qty_preordered-? WHERE id=?", (qty, po_item_id)
            )
            self.db.execute("UPDATE inv_orders SET warehouse_id=? WHERE id=?", (wh, line["order_id"]))
            self._move(
                wh,
                pid,
                "reserve",
                0,
                "order",
                int(line["order_id"]),
                lot_id,
                actor,
                f"Giữ {qty} cho đơn đặt trước {order_code(int(line['order_id']))}",
            )
            served.append(int(line["order_id"]))
        return served

    # ------------------------------------------------------------------ #
    # automatic pricing

    def _rule(self, product: dict[str, Any], stage: int) -> dict[str, float]:
        own = json.loads(product["rules"]) if product["rules"] else {}
        return own.get(str(stage)) or self.settings()["rules"][str(stage)]

    @staticmethod
    def _remaining_pct(lot: dict[str, Any]) -> float:
        """Sellable stock of the lot, as a share of what it had to sell:
        (remaining - reserved) / (received - pre-sold before arrival)."""
        base = int(lot["initial_qty"]) - int(lot["preordered_qty"])
        free = max(0, int(lot["remaining_qty"]) - int(lot["reserved_qty"]))
        return round(100.0 * free / base, 1) if base > 0 else 0.0

    def _log(
        self,
        pid: int,
        lot_id: int | None,
        old_stage: int | None,
        new_stage: int | None,
        old_price: int | None,
        new_price: int | None,
        trigger: str,
        actor: str,
        pct: float | None = None,
        days: int | None = None,
        reason: str = "",
    ) -> None:
        self.db.execute(
            "INSERT INTO inv_price_log (ts, product_id, lot_id, old_stage, new_stage, old_price, new_price, trigger, actor, "
            "remaining_pct, days, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now_iso(),
                pid,
                lot_id,
                old_stage,
                new_stage,
                old_price,
                new_price,
                trigger,
                actor,
                "" if pct is None else str(pct),
                days,
                reason[:500],
            ),
        )

    def run_pricing(
        self, now: datetime | None = None, actor: str = "", product_id: int | None = None
    ) -> list[dict[str, Any]]:
        """The daily price run. For every product: a sold-out active lot is closed and the next
        lot becomes active at stage 1; otherwise, with automatic pricing on, the active lot
        moves one stage down when its rule is met. Returns the changes made."""
        now = now or datetime.now().astimezone()
        trigger = "admin" if actor else "system"
        changes = []
        sql = "SELECT * FROM inv_products WHERE active=1" + (" AND id=?" if product_id else "")
        for product in self.db.rows(sql, (product_id,) if product_id else ()):
            pid = int(product["id"])
            with self.db.transaction():
                lots = self._lots(pid)
                if not lots:
                    continue
                lot = lots[0]
                if lot["status"] == "active" and int(lot["remaining_qty"]) == 0:
                    self.db.execute(
                        "UPDATE inv_lots SET status='exhausted', exhausted_at=? WHERE id=?",
                        (now.isoformat(timespec="seconds"), lot["id"]),
                    )
                    old_price = int(lot[f"price{lot['stage']}"])
                    nxt = lots[1] if len(lots) > 1 else None
                    if nxt is None:
                        self._log(
                            pid,
                            lot["id"],
                            int(lot["stage"]),
                            None,
                            old_price,
                            None,
                            trigger,
                            actor,
                            reason="Lô đã bán hết, chưa có lô mới",
                        )
                        changes.append({"product_id": pid, "sku": product["sku"], "event": "sold_out"})
                        continue
                    lot = nxt
                    self._activate(lot, now)
                    self._log(
                        pid,
                        lot["id"],
                        None,
                        1,
                        old_price,
                        int(lot["price1"]),
                        "activate",
                        actor,
                        reason="Lô cũ đã bán hết: kích hoạt lô mới, về giá giai đoạn 1 (hàng mới về)",
                    )
                    changes.append(
                        {
                            "product_id": pid,
                            "sku": product["sku"],
                            "event": "new_lot",
                            "lot_id": lot["id"],
                            "new_stage": 1,
                            "new_price": self.major(int(lot["price1"])),
                        }
                    )
                    continue
                if lot["status"] == "queued":  # nothing active (e.g. after a cancelled lot): start the queue
                    self._activate(lot, now)
                    self._log(
                        pid,
                        lot["id"],
                        None,
                        1,
                        None,
                        int(lot["price1"]),
                        "activate",
                        actor,
                        reason="Kích hoạt lô",
                    )
                    continue
                stage = int(lot["stage"])
                if not product["auto_pricing"] or stage >= 5:
                    continue
                rule = self._rule(product, stage)
                pct = self._remaining_pct(lot)
                since = _parse_ts(lot["stage_since"]) or _parse_ts(lot["activated_at"]) or now
                days = (now - since).days
                stock_met = pct <= rule["target_pct"] and days >= rule["min_days"]
                time_met = days >= rule["max_days"]
                if not (stock_met or time_met):
                    continue
                if stock_met:
                    reason = (
                        f"Còn {pct:g}% hàng (≤ {rule['target_pct']:g}%) sau {days} ngày giữ giá "
                        f"(≥ {rule['min_days']} ngày tối thiểu)"
                    )
                else:
                    reason = (
                        f"Đã giữ giá {days} ngày (≥ {rule['max_days']} ngày tối đa), còn {pct:g}% hàng: "
                        "giảm giá để đẩy hàng"
                    )
                old_price, new_price = int(lot[f"price{stage}"]), int(lot[f"price{stage + 1}"])
                self.db.execute(
                    "UPDATE inv_lots SET stage=?, stage_since=? WHERE id=?",
                    (stage + 1, now.isoformat(timespec="seconds"), lot["id"]),
                )
                self._log(
                    pid, lot["id"], stage, stage + 1, old_price, new_price, trigger, actor, pct, days, reason
                )
                changes.append(
                    {
                        "product_id": pid,
                        "sku": product["sku"],
                        "event": "stage",
                        "lot_id": lot["id"],
                        "old_stage": stage,
                        "new_stage": stage + 1,
                        "old_price": self.major(old_price),
                        "new_price": self.major(new_price),
                        "reason": reason,
                    }
                )
        return changes

    def _activate(self, lot: dict[str, Any], now: datetime) -> None:
        ts = now.isoformat(timespec="seconds")
        self.db.execute(
            "UPDATE inv_lots SET status='active', stage=1, activated_at=?, stage_since=? WHERE id=?",
            (ts, ts, lot["id"]),
        )

    def set_stage(self, pid: int, stage: int, actor: str, reason: str = "") -> dict[str, Any]:
        """A manager sets the price stage of the product's current lot by hand."""
        if not 1 <= stage <= 5:
            raise InventoryError("Giai đoạn giá từ 1 đến 5")
        lot = self.pricing_lot(pid)
        if lot is None:
            raise InventoryError("Sản phẩm chưa có hàng nhập nên chưa có giá")
        with self.db.transaction():
            now = now_iso()
            if lot["status"] == "queued":
                self.db.execute(
                    "UPDATE inv_lots SET status='active', activated_at=? WHERE id=?", (now, lot["id"])
                )
            self.db.execute("UPDATE inv_lots SET stage=?, stage_since=? WHERE id=?", (stage, now, lot["id"]))
            self._log(
                pid,
                lot["id"],
                int(lot["stage"]),
                stage,
                int(lot[f"price{lot['stage']}"]),
                int(lot[f"price{stage}"]),
                "manual",
                actor,
                self._remaining_pct(lot),
                None,
                reason or "Quản lý đổi giá tay",
            )
        return self.product(pid)

    def set_lot_prices(self, lot_id: int, prices: list[Any], vip_price: Any, actor: str) -> dict[str, Any]:
        lot = self.db.row("SELECT * FROM inv_lots WHERE id=?", (lot_id,))
        if lot is None:
            raise InventoryError("Không có lô hàng này")
        if not isinstance(prices, list) or len(prices) != 5:
            raise InventoryError("Cần đủ 5 giá giai đoạn")
        values = [self.minor(p, "Giá giai đoạn") for p in prices]
        vip = self.minor(vip_price, "Giá VIP") if vip_price not in (None, "") else None
        with self.db.transaction():
            self.db.execute(
                "UPDATE inv_lots SET price1=?, price2=?, price3=?, price4=?, price5=?, vip_price=? WHERE id=?",
                (*values, vip, lot_id),
            )
            stage = int(lot["stage"])
            self._log(
                int(lot["product_id"]),
                lot_id,
                stage,
                stage,
                int(lot[f"price{stage}"]),
                values[stage - 1],
                "manual",
                actor,
                reason="Sửa bảng giá của lô",
            )
        return self.product(int(lot["product_id"]))

    def maybe_run_daily(self, now: datetime | None = None) -> list[dict[str, Any]] | None:
        """Called by the office scheduler: the price run once a day, at or after price_hour."""
        now = now or datetime.now().astimezone()
        if now.hour < int(self.settings()["price_hour"]):
            return None
        today = now.date().isoformat()
        claimed = []

        def claim(doc: dict[str, Any]) -> None:
            if doc.get("last_price_run") != today:
                doc["last_price_run"] = today
                claimed.append(True)

        self.docs.update("inventory_runs", claim, {})
        return self.run_pricing(now) if claimed else None

    def price_log(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.rows(
            "SELECT l.*, p.sku, p.name FROM inv_price_log l JOIN inv_products p ON p.id=l.product_id ORDER BY l.id DESC LIMIT ?",
            (limit,),
        )
        return [self._log_json(r) for r in rows]

    # ------------------------------------------------------------------ #
    # sales orders

    def create_order(
        self,
        items: list[dict[str, Any]],
        *,
        warehouse_id: int | None = None,
        kind: str = "now",
        contact_id: int | None = None,
        conversation_id: int | None = None,
        customer_name: str = "",
        phone: str = "",
        address: str = "",
        discount: Any = 0,
        vip: bool = False,
        note: str = "",
        source: str = "",
        actor: str = "",
    ) -> dict[str, Any]:
        """Confirm a sale and reserve its goods. `kind` "now": from stock in the warehouse;
        "preorder": on incoming purchase orders (`po_id` per item, or the earliest ETA),
        served automatically when the goods arrive. A line may carry its own `unit_price`
        (a once-off special price)."""
        if kind not in ("now", "preorder"):
            raise InventoryError("Loại đơn là now (có sẵn) hoặc preorder (đặt trước)")
        if not items:
            raise InventoryError("Đơn hàng cần ít nhất một sản phẩm")
        wh = int(warehouse_id) if warehouse_id else int(self.default_warehouse()["id"])
        self.warehouse(wh)
        lines = []
        for it in items:
            p = (
                self.product_row(_int(it["product_id"], "Sản phẩm", 1))
                if it.get("product_id")
                else self.by_sku(str(it.get("sku", "")))
            )
            if p is None:
                raise InventoryError(f"Không có SKU {it.get('sku')}")
            qty = _int(it.get("qty"), "Số lượng", 1)
            lines.append((p, qty, it))
        now = now_iso()
        with self.db.transaction():
            oid = self.db.execute(
                "INSERT INTO inv_orders (kind, warehouse_id, contact_id, conversation_id, customer_name, phone, address, note, "
                "source, created_by, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (
                    kind,
                    wh,
                    contact_id,
                    conversation_id,
                    customer_name[:120],
                    phone[:40],
                    address[:300],
                    note[:1000],
                    source[:40],
                    actor[:80],
                    now,
                ),
            )
            assert oid is not None
            subtotal = cost = 0
            for p, qty, it in lines:
                pid = int(p["id"])
                price = self.current_price(pid, vip)
                if it.get("unit_price") not in (None, ""):
                    unit = self.minor(it["unit_price"], "Giá bán")
                    stage, is_vip = (price or {}).get("stage"), False
                elif price is not None:
                    unit, stage, is_vip = price["price"], price["stage"], price["vip"]
                else:
                    unit, stage, is_vip = 0, None, False
                if kind == "now":
                    if not self._reserve(wh, pid, qty):
                        raise InventoryError(
                            f"{p['sku']}: kho chỉ còn {self._available(wh, pid)} có thể bán, cần {qty}"
                        )
                    line_id = self._order_line(oid, pid, qty, unit, stage, is_vip, None, "reserved")
                    cost += self._allocate(line_id, pid, qty)
                    self._move(
                        wh, pid, "reserve", 0, "order", oid, None, actor, f"Giữ {qty} cho {order_code(oid)}"
                    )
                else:
                    po_item = self._preorder_slot(pid, qty, it.get("po_id"))
                    if unit == 0:
                        unit = int(po_item["price1"])
                    self.db.execute(
                        "UPDATE inv_po_items SET qty_preordered=qty_preordered+? WHERE id=?",
                        (qty, po_item["id"]),
                    )
                    self._order_line(oid, pid, qty, unit, stage or 1, is_vip, int(po_item["id"]), "awaiting")
                subtotal += unit * qty
            disc = self.minor(discount, "Giảm giá") if discount not in (None, "", 0) else 0
            if disc > subtotal:
                raise InventoryError("Giảm giá lớn hơn tổng tiền")
            self.db.execute(
                "UPDATE inv_orders SET subtotal=?, discount=?, total=?, cost=? WHERE id=?",
                (subtotal, disc, subtotal - disc, cost, oid),
            )
        return self.order(int(oid))

    def _order_line(
        self,
        oid: int,
        pid: int,
        qty: int,
        unit: int,
        stage: int | None,
        vip: bool,
        po_item_id: int | None,
        status: str,
    ) -> int:
        line_id = self.db.execute(
            "INSERT INTO inv_order_items (order_id, product_id, qty, unit_price, stage, vip, po_item_id, status, line_total) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (oid, pid, qty, unit, stage, 1 if vip else 0, po_item_id, status, unit * qty),
        )
        assert line_id is not None
        return line_id

    def _allocate(self, line_id: int, pid: int, qty: int) -> int:
        """Reserve lot quantities for a sale, oldest lot first; returns the cost of the goods."""
        left, cost = qty, 0
        for lot in self._lots(pid):
            free = int(lot["remaining_qty"]) - int(lot["reserved_qty"])
            take = min(left, free)
            if take <= 0:
                continue
            if (
                self.db.execute(
                    "UPDATE inv_lots SET reserved_qty=reserved_qty+? WHERE id=? AND remaining_qty-reserved_qty>=? RETURNING id",
                    (take, lot["id"], take),
                )
                is None
            ):
                continue
            self.db.execute(
                "INSERT INTO inv_order_allocs (order_item_id, lot_id, qty, unit_cost) VALUES (?, ?, ?, ?)",
                (line_id, lot["id"], take, lot["landed_cost"]),
            )
            cost += take * int(lot["landed_cost"])
            left -= take
            if left == 0:
                break
        if left:  # stock without a lot (older data): costed at the latest known cost
            latest = self.db.row(
                "SELECT landed_cost FROM inv_lots WHERE product_id=? ORDER BY id DESC LIMIT 1", (pid,)
            )
            unit_cost = int(latest["landed_cost"]) if latest else 0
            self.db.execute(
                "INSERT INTO inv_order_allocs (order_item_id, lot_id, qty, unit_cost) VALUES (?, NULL, ?, ?)",
                (line_id, left, unit_cost),
            )
            cost += left * unit_cost
        return cost

    def _preorder_slot(self, pid: int, qty: int, po_id: Any) -> dict[str, Any]:
        for slot in self._incoming(pid):
            if po_id and int(slot["po_id"]) != int(po_id):
                continue
            if int(slot["qty"]) - int(slot["qty_preordered"]) >= qty:
                return {**slot, "id": slot["po_item_id"]}
        raise InventoryError("Không có lô hàng sắp về nào còn đủ số lượng để đặt trước")

    def complete_order(self, oid: int, actor: str = "") -> dict[str, Any]:
        """Goods handed over: they leave stock and their lots; the profit is recorded."""
        order = self._order_row(oid)
        if order["status"] != "confirmed":
            raise InventoryError(f"Đơn đang ở trạng thái '{order['status']}'")
        lines = self.db.rows("SELECT * FROM inv_order_items WHERE order_id=?", (oid,))
        if any(x["status"] == "awaiting" for x in lines):
            raise InventoryError("Đơn đặt trước còn chờ hàng về")
        wh = int(order["warehouse_id"])
        with self.db.transaction():
            cost = 0
            for line in lines:
                pid, qty = int(line["product_id"]), int(line["qty"])
                if not self._take_on_hand(wh, pid, qty, from_reserved=True):
                    raise InventoryError("Tồn kho không khớp với số đã giữ; kiểm kho rồi thử lại")
                for a in self.db.rows("SELECT * FROM inv_order_allocs WHERE order_item_id=?", (line["id"],)):
                    if a["lot_id"] is not None:
                        self.db.execute(
                            "UPDATE inv_lots SET remaining_qty=remaining_qty-?, reserved_qty=reserved_qty-? WHERE id=?",
                            (a["qty"], a["qty"], a["lot_id"]),
                        )
                    cost += int(a["qty"]) * int(a["unit_cost"])
                    self._move(
                        wh, pid, "sale_out", -int(a["qty"]), "order", oid, a["lot_id"], actor, order_code(oid)
                    )
                self.db.execute("UPDATE inv_order_items SET status='done' WHERE id=?", (line["id"],))
            self.db.execute(
                "UPDATE inv_orders SET status='completed', completed=?, cost=?, profit=total-? WHERE id=?",
                (now_iso(), cost, cost, oid),
            )
        return self.order(oid)

    def cancel_order(self, oid: int, actor: str = "") -> dict[str, Any]:
        order = self._order_row(oid)
        if order["status"] != "confirmed":
            raise InventoryError(f"Đơn đang ở trạng thái '{order['status']}'")
        wh = int(order["warehouse_id"])
        with self.db.transaction():
            for line in self.db.rows("SELECT * FROM inv_order_items WHERE order_id=?", (oid,)):
                pid, qty = int(line["product_id"]), int(line["qty"])
                if line["status"] == "awaiting":
                    self.db.execute(
                        "UPDATE inv_po_items SET qty_preordered=qty_preordered-? WHERE id=?",
                        (qty, line["po_item_id"]),
                    )
                elif line["status"] == "reserved":
                    self.db.execute(
                        "UPDATE inv_stock SET reserved=reserved-? WHERE warehouse_id=? AND product_id=?",
                        (qty, wh, pid),
                    )
                    for a in self.db.rows(
                        "SELECT * FROM inv_order_allocs WHERE order_item_id=? AND lot_id IS NOT NULL",
                        (line["id"],),
                    ):
                        pre = int(a["qty"]) if line["po_item_id"] else 0
                        self.db.execute(
                            "UPDATE inv_lots SET reserved_qty=reserved_qty-?, preordered_qty=preordered_qty-? WHERE id=?",
                            (a["qty"], pre, a["lot_id"]),
                        )
                    self._move(
                        wh,
                        pid,
                        "release",
                        0,
                        "order",
                        oid,
                        None,
                        actor,
                        f"Huỷ {order_code(oid)}: trả {qty} về kho",
                    )
                self.db.execute("UPDATE inv_order_items SET status='released' WHERE id=?", (line["id"],))
            self.db.execute(
                "UPDATE inv_orders SET status='cancelled', cancelled=? WHERE id=?", (now_iso(), oid)
            )
        return self.order(oid)

    def _order_row(self, oid: int) -> dict[str, Any]:
        row = self.db.row("SELECT * FROM inv_orders WHERE id=?", (oid,))
        if row is None:
            raise InventoryError("Không có đơn hàng này")
        return row

    def order(self, oid: int) -> dict[str, Any]:
        o = self._order_row(oid)
        items = self.db.rows(
            "SELECT i.*, p.sku, p.name, po.po_number, po.eta FROM inv_order_items i JOIN inv_products p ON p.id=i.product_id "
            "LEFT JOIN inv_po_items pi ON pi.id=i.po_item_id LEFT JOIN inv_purchase_orders po ON po.id=pi.po_id "
            "WHERE i.order_id=? ORDER BY i.id",
            (oid,),
        )
        return {
            **o,
            "code": order_code(oid),
            **{k: self.major(o[k]) for k in ("subtotal", "discount", "total", "cost", "profit")},
            "items": [
                {**i, "unit_price": self.major(i["unit_price"]), "line_total": self.major(i["line_total"])}
                for i in items
            ],
        }

    def orders(
        self, status: str | None = None, contact_id: int | None = None, limit: int = 300
    ) -> list[dict[str, Any]]:
        sql, args = (
            "SELECT o.*, w.code AS warehouse_code FROM inv_orders o JOIN inv_warehouses w ON w.id=o.warehouse_id WHERE 1=1",
            [],
        )
        if status:
            sql += " AND o.status=?"
            args.append(status)
        if contact_id:
            sql += " AND o.contact_id=?"
            args.append(contact_id)
        rows = self.db.rows(sql + " ORDER BY o.id DESC LIMIT ?", [*args, limit])
        return [
            {
                **r,
                "code": order_code(int(r["id"])),
                **{k: self.major(r[k]) for k in ("subtotal", "discount", "total", "cost", "profit")},
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ #
    # transfers between warehouses and shops

    def create_transfer(
        self, from_wh: int, to_wh: int, items: list[dict[str, Any]], note: str = "", actor: str = ""
    ) -> dict[str, Any]:
        if from_wh == to_wh:
            raise InventoryError("Kho đi và kho đến phải khác nhau")
        self.warehouse(from_wh)
        self.warehouse(to_wh)
        if not items:
            raise InventoryError("Phiếu chuyển cần ít nhất một sản phẩm")
        with self.db.transaction():
            tid = self.db.execute(
                "INSERT INTO inv_transfers (from_wh, to_wh, note, created_by, created) VALUES (?, ?, ?, ?, ?) RETURNING id",
                (from_wh, to_wh, note[:1000], actor, now_iso()),
            )
            for it in items:
                p = (
                    self.product_row(_int(it["product_id"], "Sản phẩm", 1))
                    if it.get("product_id")
                    else self.by_sku(str(it.get("sku", "")))
                )
                if p is None:
                    raise InventoryError(f"Không có SKU {it.get('sku')}")
                self.db.execute(
                    "INSERT INTO inv_transfer_items (transfer_id, product_id, qty) VALUES (?, ?, ?)",
                    (tid, p["id"], _int(it.get("qty"), "Số lượng", 1)),
                )
        return self.transfer(int(tid or 0))

    def ship_transfer(self, tid: int, actor: str = "") -> dict[str, Any]:
        t = self.transfer(tid)
        if t["status"] != "requested":
            raise InventoryError("Phiếu này đã xuất hoặc đã huỷ")
        with self.db.transaction():
            for it in t["items"]:
                if not self._take_on_hand(
                    int(t["from_wh"]), int(it["product_id"]), int(it["qty"]), from_reserved=False
                ):
                    raise InventoryError(
                        f"{it['sku']}: kho đi chỉ còn {self._available(int(t['from_wh']), int(it['product_id']))}"
                    )
                self._move(
                    int(t["from_wh"]),
                    int(it["product_id"]),
                    "transfer_out",
                    -int(it["qty"]),
                    "transfer",
                    tid,
                    actor=actor,
                    note=transfer_code(tid),
                )
            self.db.execute(
                "UPDATE inv_transfers SET status='in_transit', shipped=? WHERE id=?", (now_iso(), tid)
            )
        return self.transfer(tid)

    def receive_transfer(
        self, tid: int, received: dict[int, int] | None = None, actor: str = ""
    ) -> dict[str, Any]:
        """The shop counts what arrived and signs for it; shortfalls are noted in the ledger."""
        t = self.transfer(tid)
        if t["status"] != "in_transit":
            raise InventoryError("Phiếu này chưa xuất kho")
        with self.db.transaction():
            for it in t["items"]:
                qty = int(it["qty"])
                got = (
                    qty
                    if received is None or it["id"] not in received
                    else _int(received[it["id"]], "Số lượng nhận")
                )
                if got > qty:
                    raise InventoryError(f"{it['sku']}: nhận nhiều hơn số đã xuất")
                self.db.execute("UPDATE inv_transfer_items SET qty_received=? WHERE id=?", (got, it["id"]))
                if got:
                    self._add_on_hand(int(t["to_wh"]), int(it["product_id"]), got)
                    self._move(
                        int(t["to_wh"]),
                        int(it["product_id"]),
                        "transfer_in",
                        got,
                        "transfer",
                        tid,
                        actor=actor,
                        note=transfer_code(tid),
                    )
                if got < qty:
                    self._move(
                        int(t["to_wh"]),
                        int(it["product_id"]),
                        "transfer_loss",
                        0,
                        "transfer",
                        tid,
                        actor=actor,
                        note=f"{transfer_code(tid)}: thiếu {qty - got}",
                    )
                    lost = qty - got
                    for lot in self._lots(int(it["product_id"])):  # the missing goods leave their lots too
                        take = min(lost, int(lot["remaining_qty"]) - int(lot["reserved_qty"]))
                        if take > 0:
                            self.db.execute(
                                "UPDATE inv_lots SET remaining_qty=remaining_qty-? WHERE id=?",
                                (take, lot["id"]),
                            )
                            lost -= take
                        if not lost:
                            break
            self.db.execute(
                "UPDATE inv_transfers SET status='received', received=? WHERE id=?", (now_iso(), tid)
            )
        return self.transfer(tid)

    def cancel_transfer(self, tid: int, actor: str = "") -> dict[str, Any]:
        t = self.transfer(tid)
        if t["status"] not in ("requested", "in_transit"):
            raise InventoryError("Phiếu này đã hoàn tất hoặc đã huỷ")
        with self.db.transaction():
            if t["status"] == "in_transit":  # goods go back to where they came from
                for it in t["items"]:
                    self._add_on_hand(int(t["from_wh"]), int(it["product_id"]), int(it["qty"]))
                    self._move(
                        int(t["from_wh"]),
                        int(it["product_id"]),
                        "transfer_in",
                        int(it["qty"]),
                        "transfer",
                        tid,
                        actor=actor,
                        note=f"Huỷ {transfer_code(tid)}",
                    )
            self.db.execute("UPDATE inv_transfers SET status='cancelled' WHERE id=?", (tid,))
        return self.transfer(tid)

    def transfer(self, tid: int) -> dict[str, Any]:
        t = self.db.row(
            "SELECT t.*, a.code AS from_code, a.name AS from_name, b.code AS to_code, b.name AS to_name FROM inv_transfers t "
            "JOIN inv_warehouses a ON a.id=t.from_wh JOIN inv_warehouses b ON b.id=t.to_wh WHERE t.id=?",
            (tid,),
        )
        if t is None:
            raise InventoryError("Không có phiếu chuyển này")
        items = self.db.rows(
            "SELECT i.*, p.sku, p.name FROM inv_transfer_items i JOIN inv_products p ON p.id=i.product_id WHERE transfer_id=?",
            (tid,),
        )
        return {**t, "code": transfer_code(tid), "items": items}

    def transfers(self, limit: int = 200) -> list[dict[str, Any]]:
        return [
            self.transfer(int(r["id"]))
            for r in self.db.rows("SELECT id FROM inv_transfers ORDER BY id DESC LIMIT ?", (limit,))
        ]

    # ------------------------------------------------------------------ #
    # reports

    def reorder(self) -> list[dict[str, Any]]:
        """Products at or below their reorder point (or out of stock), most urgent first."""
        rows = [p for p in self.products(limit=100000) if p["level"] in ("empty", "reorder")]
        return sorted(rows, key=lambda p: (LEVELS.index(p["level"]), -p["daily_sales"]))

    def summary(self, days: int = 1) -> dict[str, Any]:
        since = (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")
        moves = {
            r["kind"]: int(r["q"] or 0)
            for r in self.db.rows(
                "SELECT kind, SUM(qty) AS q FROM inv_moves WHERE ts>=? GROUP BY kind", (since,)
            )
        }
        sales = (
            self.db.row(
                "SELECT COUNT(*) AS n, SUM(total) AS total, SUM(profit) AS profit FROM inv_orders WHERE status='completed' AND completed>=?",
                (since,),
            )
            or {}
        )
        value = (
            self.db.row(
                "SELECT SUM(remaining_qty*landed_cost) AS v FROM inv_lots WHERE status IN ('active','queued')"
            )
            or {}
        )
        return {
            "stock_in": moves.get("stock_in", 0),
            "sold": -moves.get("sale_out", 0),
            "transferred": -moves.get("transfer_out", 0),
            "orders": int(sales.get("n") or 0),
            "revenue": self.major(int(sales.get("total") or 0)),
            "profit": self.major(int(sales.get("profit") or 0)),
            "stock_value": self.major(int(value.get("v") or 0)),
        }

    # ------------------------------------------------------------------ #
    # for the AI employee: what a customer may be told (never costs or margins)

    def lookup(self, query: str, vip: bool = False, limit: int = 8) -> list[dict[str, Any]]:
        words = [w for w in query.lower().split() if w]
        rows = self.products(" ".join(words[:1]) if words else "", limit=200)
        if len(words) > 1:
            rows = [
                r
                for r in rows
                if all(
                    w in f"{r['sku']} {r['name']} {r['category']} {r['group_name']}".lower() for w in words
                )
            ]
        out = []
        for r in rows[:limit]:
            price = self.current_price(int(r["id"]), vip)
            out.append(
                {
                    "sku": r["sku"],
                    "name": r["name"],
                    "unit": r["unit"],
                    "attributes": r["attributes"],
                    "price": self.major(price["price"]) if price else None,
                    "vip_price": bool(price and price["vip"]),
                    "available": max(0, r["available"]),
                    "incoming": max(0, r["incoming"] - r["preordered"]),
                    "next_eta": r["next_eta"],
                }
            )
        return out
