"""Delivery to customers' homes (sale-management module 3).

- **Bookings** on a delivery calendar: a sales order, a day and a time slot (or a fixed
  window, "15-16h"), the address and the special requests that cost extra: assembly and
  carrying up stairs (per floor). The surcharge is added to the order. The calendar shows,
  per day, the bookings, the boxes to load and the special requests.
- **Carriers** (own trucks or partners) with their rates, and their **drivers**.
- **Trips**: a truck's bookings of a day, in the shortest order. With a Google Maps key
  (GOOGLE_MAPS_API_KEY) addresses are geocoded and driving times come from the Distance
  Matrix API; otherwise straight-line distances between the coordinates staff entered.
  Nearest-neighbour then 2-opt; staff can still reorder by hand.
- **Lifecycle**: booked -> assigned (on a trip) -> shipping (trip started: the customer is
  told on their chat channel) -> done (the goods leave stock: the sale is completed and
  its FIFO profit realised) or comeback (not delivered: the goods stay reserved; book again).
- **Daily delivery list** for the driver (CSV: stop, customer, items and boxes per warehouse,
  signature, notes) and each carrier's trips, stops and cost for a period.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import os
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .i18n import tr
from .inventory import InventoryError, _int, order_code
from .state import now_iso

if TYPE_CHECKING:
    from .employee import Office

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS dl_carriers (
  id {id}, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
  internal {int} NOT NULL DEFAULT 0, rate_per_trip {int} NOT NULL DEFAULT 0, rate_per_stop {int} NOT NULL DEFAULT 0,
  active {int} NOT NULL DEFAULT 1, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dl_drivers (
  id {id}, carrier_id {int} NOT NULL, name TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '', vehicle TEXT NOT NULL DEFAULT '',
  license TEXT NOT NULL DEFAULT '', username TEXT NOT NULL DEFAULT '', active {int} NOT NULL DEFAULT 1, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dl_bookings (
  id {id}, order_id {int} NOT NULL, delivery_date TEXT NOT NULL, slot TEXT NOT NULL DEFAULT 'flexible',
  time_window TEXT NOT NULL DEFAULT '', customer_name TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL, lat TEXT NOT NULL DEFAULT '', lng TEXT NOT NULL DEFAULT '', assembling {int} NOT NULL DEFAULT 0,
  floors {int} NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '', surcharge {int} NOT NULL DEFAULT 0,
  boxes {int} NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'booked', result_note TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS dl_bookings_date ON dl_bookings (delivery_date, status);
CREATE INDEX IF NOT EXISTS dl_bookings_order ON dl_bookings (order_id);
CREATE TABLE IF NOT EXISTS dl_routes (
  id {id}, delivery_date TEXT NOT NULL, carrier_id {int} NOT NULL, driver_id {int}, origin_wh {int} NOT NULL,
  status TEXT NOT NULL DEFAULT 'planned', start_time TEXT NOT NULL DEFAULT '08:00', distance_km TEXT NOT NULL DEFAULT '0',
  duration_min {int} NOT NULL DEFAULT 0, cost {int} NOT NULL DEFAULT 0, optimized_by TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, started TEXT, finished TEXT);
CREATE INDEX IF NOT EXISTS dl_routes_date ON dl_routes (delivery_date);
CREATE TABLE IF NOT EXISTS dl_stops (
  id {id}, route_id {int} NOT NULL, booking_id {int} NOT NULL, seq {int} NOT NULL, eta TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending', arrived TEXT);
CREATE INDEX IF NOT EXISTS dl_stops_route ON dl_stops (route_id, seq)
"""
SETTINGS_KEY = "delivery_settings"
DEFAULTS = {
    "floor_fee": 50_000,
    "assembly_fee": 200_000,
    "stop_minutes": 20,
    "assembly_minutes": 40,
    "speed_kmh": 25,
}
SLOTS = {
    "morning": "Sáng (7-12h)",
    "afternoon": "Chiều (12-17h)",
    "evening": "Tối (17-20h)",
    "flexible": "Cả ngày",
}
MAPS = "https://maps.googleapis.com/maps/api"


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def best_order(matrix: list[list[float]]) -> list[int]:
    """Visiting order of points 1..n starting from point 0 and coming back to it, with the
    smallest total: nearest neighbour, improved by 2-opt."""
    n = len(matrix) - 1
    if n <= 1:
        return list(range(1, n + 1))
    left, tour, at = set(range(1, n + 1)), [], 0
    while left:
        at = min(left, key=lambda j: matrix[at][j])
        tour.append(at)
        left.remove(at)

    def length(t: list[int]) -> float:
        path = [0, *t, 0]
        return sum(matrix[path[k]][path[k + 1]] for k in range(len(path) - 1))

    improved = True
    while improved:
        improved = False
        for i in range(len(tour) - 1):
            for j in range(i + 1, len(tour)):
                candidate = tour[:i] + tour[i : j + 1][::-1] + tour[j + 1 :]
                if length(candidate) + 1e-9 < length(tour):
                    tour, improved = candidate, True
    return tour


class Delivery:
    def __init__(self, office: Office):
        self.office = office
        self.inv = office.inventory
        self.db = self.inv.db
        self.db.script(SCHEMA)
        self.db.add_columns(
            "inv_warehouses", {"lat": "TEXT NOT NULL DEFAULT ''", "lng": "TEXT NOT NULL DEFAULT ''"}
        )

    @property
    def maps_key(self) -> str:
        return os.environ.get("GOOGLE_MAPS_API_KEY", "")

    # settings

    def settings(self) -> dict[str, Any]:
        s = {**DEFAULTS, **(self.office.docs.get(SETTINGS_KEY) or {})}
        return {
            **s,
            "floor_fee": self.inv.major(int(s["floor_fee"])),
            "assembly_fee": self.inv.major(int(s["assembly_fee"])),
            "maps": bool(self.maps_key),
        }

    def save_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        for key, what in (("floor_fee", tr("Phí vác lầu / tầng")), ("assembly_fee", tr("Phí lắp ráp"))):
            if key in data:
                clean[key] = self.inv.minor(data[key], what)
        for key, what in (
            ("stop_minutes", tr("Phút mỗi điểm")),
            ("assembly_minutes", tr("Phút lắp ráp")),
            ("speed_kmh", tr("Tốc độ")),
        ):
            if key in data:
                clean[key] = _int(data[key], what, 1)
        self.office.docs.update(SETTINGS_KEY, lambda d: d.update(clean), {})
        return self.settings()

    def _raw(self) -> dict[str, Any]:
        return {**DEFAULTS, **(self.office.docs.get(SETTINGS_KEY) or {})}

    # carriers and drivers

    def save_carrier(self, cid: int | None, data: dict[str, Any]) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        for key, limit in (("code", 30), ("name", 150), ("phone", 40), ("email", 200)):
            if key in data or (cid is None and key in ("code", "name")):
                fields[key] = str(data.get(key) or "").strip()[:limit]
        if "code" in fields:
            fields["code"] = fields["code"].upper()
        if cid is None and not (fields.get("code") and fields.get("name")):
            raise InventoryError(tr("Mã và tên đơn vị vận chuyển là bắt buộc"))
        for key, what in (
            ("rate_per_trip", tr("Giá mỗi chuyến")),
            ("rate_per_stop", tr("Giá mỗi điểm giao")),
        ):
            if key in data:
                fields[key] = self.inv.minor(data[key] or 0, what)
        for key in ("internal", "active"):
            if key in data:
                fields[key] = 1 if data[key] else 0
        return self._carrier_json(self.inv._save("dl_carriers", cid, fields, tr("đơn vị vận chuyển")))

    def _carrier_json(self, c: dict[str, Any]) -> dict[str, Any]:
        return {
            **c,
            "rate_per_trip": self.inv.major(c["rate_per_trip"]),
            "rate_per_stop": self.inv.major(c["rate_per_stop"]),
        }

    def carriers(self) -> list[dict[str, Any]]:
        return [
            self._carrier_json(c)
            for c in self.db.rows("SELECT * FROM dl_carriers ORDER BY active DESC, name")
        ]

    def save_driver(self, did: int | None, data: dict[str, Any]) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if "carrier_id" in data or did is None:
            fields["carrier_id"] = _int(data.get("carrier_id"), tr("Đơn vị vận chuyển"), 1)
            if not self.db.row("SELECT 1 AS x FROM dl_carriers WHERE id=?", (fields["carrier_id"],)):
                raise InventoryError(tr("Không có đơn vị vận chuyển này"))
        for key, limit in (("name", 100), ("phone", 40), ("vehicle", 60), ("license", 40), ("username", 40)):
            if key in data or (did is None and key == "name"):
                fields[key] = str(data.get(key) or "").strip()[:limit]
        if "name" in fields and not fields["name"]:
            raise InventoryError(tr("Tên tài xế là bắt buộc"))
        if "active" in data:
            fields["active"] = 1 if data["active"] else 0
        return self.inv._save("dl_drivers", did, fields, tr("tài xế"))

    def drivers(self) -> list[dict[str, Any]]:
        return self.db.rows(
            "SELECT d.*, c.name AS carrier_name FROM dl_drivers d JOIN dl_carriers c ON c.id=d.carrier_id ORDER BY d.active DESC, d.name"
        )

    # bookings

    def _boxes(self, oid: int) -> int:
        row = self.db.row(
            "SELECT SUM(i.qty*p.box_count) AS b FROM inv_order_items i JOIN inv_products p ON p.id=i.product_id WHERE i.order_id=?",
            (oid,),
        )
        return int(row["b"] or 0) if row else 0

    def _set_fee(self, oid: int, delta: int) -> None:
        """Add (or remove) a delivery surcharge on the order's total."""
        if not delta:
            return
        self.db.execute(
            "UPDATE inv_orders SET shipping_fee=shipping_fee+?, total=total+?, payment_status=CASE WHEN paid>=total+? "
            "THEN 'paid' WHEN paid>0 THEN 'partial' ELSE 'unpaid' END WHERE id=?",
            (delta, delta, delta, oid),
        )

    def book(self, data: dict[str, Any], actor: str = "") -> dict[str, Any]:
        oid = _int(data.get("order_id"), tr("Đơn hàng"), 1)
        order = self.inv._order_row(oid)
        if order["status"] != "confirmed":
            raise InventoryError(tr("Chỉ đặt lịch giao cho đơn đã xác nhận, chưa giao"))
        if self.db.row(
            "SELECT 1 AS x FROM dl_bookings WHERE order_id=? AND status IN ('booked','assigned','shipping')",
            (oid,),
        ):
            raise InventoryError(tr("Đơn này đã có lịch giao"))
        day = str(data.get("delivery_date") or "")[:10]
        try:
            date.fromisoformat(day)
        except ValueError:
            raise InventoryError(tr("Ngày giao: dạng YYYY-MM-DD")) from None
        slot = str(data.get("slot") or "flexible")
        if slot not in SLOTS:
            raise InventoryError(tr("Khung giờ: {0}", ", ".join(SLOTS)))
        address = str(data.get("address") or order["address"] or "").strip()[:300]
        if not address:
            raise InventoryError(tr("Cần địa chỉ giao hàng"))
        s = self._raw()
        floors = _int(data.get("floors") or 0, tr("Số tầng"))
        assembling = bool(data.get("assembling"))
        surcharge = floors * int(s["floor_fee"]) + (int(s["assembly_fee"]) if assembling else 0)
        lat, lng = self._coords(data)
        now = now_iso()
        with self.db.transaction():
            bid = self.db.execute(
                "INSERT INTO dl_bookings (order_id, delivery_date, slot, time_window, customer_name, phone, address, lat, lng, "
                "assembling, floors, notes, surcharge, boxes, created_by, created, updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (
                    oid,
                    day,
                    slot,
                    str(data.get("time_window") or data.get("window") or "")[:40],
                    str(data.get("customer_name") or order["customer_name"])[:120],
                    str(data.get("phone") or order["phone"])[:40],
                    address,
                    lat,
                    lng,
                    1 if assembling else 0,
                    floors,
                    str(data.get("notes") or "")[:500],
                    surcharge,
                    self._boxes(oid),
                    actor,
                    now,
                    now,
                ),
            )
            self._set_fee(oid, surcharge)
            self.db.execute(
                "UPDATE inv_orders SET delivery_date=?, address=? WHERE id=?", (day, address, oid)
            )
        self._locate_customer(order, address, lat, lng)
        return self.booking(int(bid or 0))

    def _locate_customer(self, order: dict[str, Any], address: str, lat: Any, lng: Any) -> None:
        """The customer's address and location, for the next order and "customers near the showroom"."""
        if not order.get("contact_id"):
            return
        try:
            la, lo = (
                (float(lat), float(lng)) if lat not in (None, "") and lng not in (None, "") else (None, None)
            )
            self.office.hub.crm.locate(int(order["contact_id"]), address, la, lo)
        except (ValueError, KeyError):
            log.debug("delivery: could not store the location of contact %s", order.get("contact_id"))

    async def geocode_address(self, address: str) -> tuple[float, float]:
        """Coordinates of an address (Google Geocoding; needs GOOGLE_MAPS_API_KEY)."""
        if not self.maps_key:
            raise InventoryError(
                tr("Chưa có GOOGLE_MAPS_API_KEY: nhập toạ độ tay, hoặc để hệ thống giữ thứ tự bạn xếp")
            )
        r = await self.office.http_client.get(
            f"{MAPS}/geocode/json",
            params={"address": address, "region": "vn", "key": self.maps_key},
            timeout=15,
        )
        data = r.json()
        if data.get("status") != "OK":
            raise InventoryError(tr("Không tìm được địa chỉ: {0}", data.get("status")))
        loc = data["results"][0]["geometry"]["location"]
        return float(loc["lat"]), float(loc["lng"])

    async def geocode_customers(self, limit: int = 100) -> dict[str, int]:
        """Locate customers who have an address but no coordinates yet."""
        crm = self.office.hub.crm
        done = failed = 0
        for c in crm.db.rows(
            "SELECT id, address FROM crm_contacts WHERE lat IS NULL AND address<>'' ORDER BY id LIMIT ?",
            (limit,),
        ):
            try:
                lat, lng = await self.geocode_address(c["address"])
            except InventoryError as e:
                if "GOOGLE_MAPS_API_KEY" in str(e):
                    raise
                failed += 1
                continue
            crm.update(int(c["id"]), lat=lat, lng=lng)
            done += 1
        return {"located": done, "failed": failed}

    def _coords(self, data: dict[str, Any]) -> tuple[str, str]:
        lat, lng = str(data.get("lat") or "").strip(), str(data.get("lng") or "").strip()
        if lat or lng:
            try:
                la, lo = float(lat), float(lng)
            except ValueError:
                raise InventoryError(tr("Toạ độ không hợp lệ")) from None
            if not (-90 <= la <= 90 and -180 <= lo <= 180):
                raise InventoryError(tr("Toạ độ không hợp lệ"))
        return lat, lng

    async def geocode(self, bid: int) -> dict[str, Any]:
        """Coordinates for a booking's address (Google Geocoding; needs GOOGLE_MAPS_API_KEY)."""
        b = self.booking(bid)
        lat, lng = await self.geocode_address(b["address"])
        self.db.execute(
            "UPDATE dl_bookings SET lat=?, lng=?, updated=? WHERE id=?", (str(lat), str(lng), now_iso(), bid)
        )
        order = self.office.inventory.order(int(b["order_id"]))
        self._locate_customer(order, b["address"], lat, lng)
        return self.booking(bid)

    def update_booking(self, bid: int, data: dict[str, Any]) -> dict[str, Any]:
        b = self._row(bid)
        if b["status"] not in ("booked", "assigned"):
            raise InventoryError(tr("Chỉ sửa được lịch chưa giao"))
        fields: dict[str, Any] = {}
        if "delivery_date" in data:
            try:
                fields["delivery_date"] = date.fromisoformat(str(data["delivery_date"])[:10]).isoformat()
            except ValueError:
                raise InventoryError(tr("Ngày giao: dạng YYYY-MM-DD")) from None
            if fields["delivery_date"] != b["delivery_date"] and b["status"] == "assigned":
                raise InventoryError(tr("Lịch đã xếp chuyến: bỏ khỏi chuyến trước khi đổi ngày"))
        if "window" in data:
            data = {**data, "time_window": data["window"]}
        for key, limit in (
            ("time_window", 40),
            ("notes", 500),
            ("address", 300),
            ("phone", 40),
            ("customer_name", 120),
        ):
            if key in data:
                fields[key] = str(data[key] or "")[:limit]
        if "slot" in data:
            if data["slot"] not in SLOTS:
                raise InventoryError(tr("Khung giờ: {0}", ", ".join(SLOTS)))
            fields["slot"] = data["slot"]
        if "lat" in data or "lng" in data:
            fields["lat"], fields["lng"] = self._coords(data)
        if "floors" in data or "assembling" in data:
            s = self._raw()
            floors = _int(data.get("floors", b["floors"]) or 0, tr("Số tầng"))
            assembling = bool(data.get("assembling", b["assembling"]))
            surcharge = floors * int(s["floor_fee"]) + (int(s["assembly_fee"]) if assembling else 0)
            fields.update(floors=floors, assembling=1 if assembling else 0, surcharge=surcharge)
            self._set_fee(int(b["order_id"]), surcharge - int(b["surcharge"]))
        fields["updated"] = now_iso()
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE dl_bookings SET {sets} WHERE id=?", [*fields.values(), bid])
        if fields.get("lat"):
            new = self._row(bid)
            self._locate_customer(
                self.office.inventory.order(int(b["order_id"])), new["address"], new["lat"], new["lng"]
            )
        return self.booking(bid)

    def cancel_booking(self, bid: int) -> dict[str, Any]:
        b = self._row(bid)
        if b["status"] not in ("booked", "assigned"):
            raise InventoryError(tr("Chỉ huỷ được lịch chưa giao"))
        with self.db.transaction():
            self.db.execute("DELETE FROM dl_stops WHERE booking_id=? AND status='pending'", (bid,))
            self.db.execute(
                "UPDATE dl_bookings SET status='cancelled', updated=? WHERE id=?", (now_iso(), bid)
            )
            self._set_fee(int(b["order_id"]), -int(b["surcharge"]))
        return self.booking(bid)

    def _row(self, bid: int) -> dict[str, Any]:
        b = self.db.row("SELECT * FROM dl_bookings WHERE id=?", (bid,))
        if b is None:
            raise InventoryError(tr("Không có lịch giao này"))
        return b

    def booking(self, bid: int) -> dict[str, Any]:
        b = self._row(bid)
        order = self.inv.order(int(b["order_id"]))
        return {
            **b,
            "surcharge": self.inv.major(b["surcharge"]),
            "slot_name": tr(SLOTS.get(b["slot"], b["slot"])),
            "order_code": order_code(int(b["order_id"])),
            "order_total": order["total"],
            "due": order["due"],
            "warehouse_id": order["warehouse_id"],
            "items": [{"sku": i["sku"], "name": i["name"], "qty": i["qty"]} for i in order["items"]],
            "window": b["time_window"],
            "special": bool(b["assembling"] or b["floors"] or b["time_window"]),
        }

    def bookings(self, start: str, end: str, status: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT id FROM dl_bookings WHERE delivery_date>=? AND delivery_date<=?", [start, end]
        if status:
            sql += " AND status=?"
            args.append(status)
        return [
            self.booking(int(r["id"])) for r in self.db.rows(sql + " ORDER BY delivery_date, slot, id", args)
        ]

    def calendar(self, month: str) -> list[dict[str, Any]]:
        """Per day of the month: bookings, boxes, special requests, by status."""
        first = date.fromisoformat(f"{month[:7]}-01")
        nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        out: dict[str, dict[str, Any]] = {}
        for r in self.db.rows(
            "SELECT delivery_date AS d, status, COUNT(*) AS n, SUM(boxes) AS boxes, "
            "SUM(CASE WHEN assembling=1 OR floors>0 OR time_window<>'' THEN 1 ELSE 0 END) AS special "
            "FROM dl_bookings WHERE delivery_date>=? AND delivery_date<? AND status<>'cancelled' GROUP BY delivery_date, status",
            (first.isoformat(), nxt.isoformat()),
        ):
            day = out.setdefault(
                r["d"], {"date": r["d"], "bookings": 0, "boxes": 0, "special": 0, "by_status": {}}
            )
            day["bookings"] += int(r["n"])
            day["boxes"] += int(r["boxes"] or 0)
            day["special"] += int(r["special"] or 0)
            day["by_status"][r["status"]] = int(r["n"])
        return sorted(out.values(), key=lambda d: d["date"])

    # trips

    def create_route(self, data: dict[str, Any], actor: str = "") -> dict[str, Any]:
        day = str(data.get("delivery_date") or "")[:10]
        carrier = _int(data.get("carrier_id"), tr("Đơn vị vận chuyển"), 1)
        if not self.db.row("SELECT 1 AS x FROM dl_carriers WHERE id=? AND active=1", (carrier,)):
            raise InventoryError(tr("Không có đơn vị vận chuyển này"))
        driver = int(data["driver_id"]) if data.get("driver_id") else None
        origin = _int(data.get("origin_wh"), tr("Kho xuất phát"), 1)
        self.inv.warehouse(origin)
        ids = [int(x) for x in data.get("booking_ids") or []]
        if not ids:
            raise InventoryError(tr("Chọn các lịch giao cho chuyến"))
        with self.db.transaction():
            rid = self.db.execute(
                "INSERT INTO dl_routes (delivery_date, carrier_id, driver_id, origin_wh, start_time, created_by, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (day, carrier, driver, origin, str(data.get("start_time") or "08:00")[:5], actor, now_iso()),
            )
            for seq, bid in enumerate(ids, 1):
                b = self._row(bid)
                if b["status"] != "booked" or b["delivery_date"] != day:
                    raise InventoryError(
                        tr("Lịch #{0} không phải lịch chờ xếp chuyến của ngày {1}", bid, day)
                    )
                self.db.execute(
                    "INSERT INTO dl_stops (route_id, booking_id, seq) VALUES (?, ?, ?)", (rid, bid, seq)
                )
                self.db.execute(
                    "UPDATE dl_bookings SET status='assigned', updated=? WHERE id=?", (now_iso(), bid)
                )
        self._price(int(rid or 0))
        self._timetable(int(rid or 0), None)
        return self.route(int(rid or 0))

    def _stops(self, rid: int) -> list[dict[str, Any]]:
        return self.db.rows("SELECT * FROM dl_stops WHERE route_id=? ORDER BY seq", (rid,))

    def _price(self, rid: int) -> None:
        r = self.db.row("SELECT * FROM dl_routes WHERE id=?", (rid,))
        c = self.db.row("SELECT * FROM dl_carriers WHERE id=?", (r["carrier_id"],)) if r else None
        if r and c:
            n = len(self._stops(rid))
            self.db.execute(
                "UPDATE dl_routes SET cost=? WHERE id=?",
                (int(c["rate_per_trip"]) + int(c["rate_per_stop"]) * n, rid),
            )

    async def optimize(self, rid: int) -> dict[str, Any]:
        """Reorder the stops for the shortest trip that comes back to the warehouse."""
        route = self._route_row(rid)
        if route["status"] != "planned":
            raise InventoryError(tr("Chỉ sắp xếp được chuyến chưa chạy"))
        stops = self._stops(rid)
        wh = self.inv.warehouse(int(route["origin_wh"]))
        points = [(wh.get("lat"), wh.get("lng"))] + [
            (self._row(int(s["booking_id"]))["lat"], self._row(int(s["booking_id"]))["lng"]) for s in stops
        ]
        if not all(la and lo for la, lo in points):
            missing = tr("kho xuất phát") if not (points[0][0] and points[0][1]) else tr("một số địa chỉ")
            raise InventoryError(
                tr("Thiếu toạ độ của {0}: bấm Tìm toạ độ (Google Maps) hoặc nhập tay", missing)
            )
        coords = [(float(a), float(b)) for a, b in points]
        matrix, by = await self._matrix(coords)
        index = {int(st["id"]): k + 1 for k, st in enumerate(stops)}  # stop -> its point in the matrix
        order = best_order(matrix)
        with self.db.transaction():
            for new_seq, k in enumerate(order, 1):
                self.db.execute("UPDATE dl_stops SET seq=? WHERE id=?", (new_seq + 1000, stops[k - 1]["id"]))
            self.db.execute("UPDATE dl_stops SET seq=seq-1000 WHERE route_id=? AND seq>1000", (rid,))
            self.db.execute("UPDATE dl_routes SET optimized_by=? WHERE id=?", (by, rid))
        self._timetable(rid, (coords, matrix, index))
        return self.route(rid)

    async def _matrix(self, coords: list[tuple[float, float]]) -> tuple[list[list[float]], str]:
        """Minutes between every pair of points: Google driving times, else distance at the average speed."""
        if self.maps_key and len(coords) <= 10:
            joined = "|".join(f"{a},{b}" for a, b in coords)
            try:
                r = await self.office.http_client.get(
                    f"{MAPS}/distancematrix/json",
                    params={
                        "origins": joined,
                        "destinations": joined,
                        "mode": "driving",
                        "key": self.maps_key,
                    },
                    timeout=20,
                )
                data = r.json()
                if data.get("status") == "OK":
                    return [
                        [(e.get("duration") or {}).get("value", 1e7) / 60 for e in row["elements"]]
                        for row in data["rows"]
                    ], "google"
                log.warning("delivery: distance matrix: %s", data.get("status"))
            except Exception as e:  # noqa: BLE001 - fall back to straight lines
                log.warning("delivery: distance matrix failed: %s", e)
        speed = int(self._raw()["speed_kmh"])
        return [[haversine_km(a, b) * 1.3 / speed * 60 for b in coords] for a in coords], "distance"

    def _timetable(self, rid: int, geo: tuple[Any, Any, dict[int, int]] | None) -> None:
        """Arrival times from the start time: travel + time at each stop (longer with assembly)."""
        route = self._route_row(rid)
        s = self._raw()
        stops = self._stops(rid)
        t = datetime.fromisoformat(f"{route['delivery_date']}T{route['start_time']}")
        total_km, total_min = 0.0, 0
        prev = 0
        coords, matrix, index = geo if geo else (None, None, {})
        for st in stops:
            b = self._row(int(st["booking_id"]))
            if matrix is not None:
                k = index[int(st["id"])]
                travel = matrix[prev][k]
                total_km += haversine_km(coords[prev], coords[k]) * 1.3
                prev = k
            else:
                travel = 15
            t += timedelta(minutes=travel)
            self.db.execute("UPDATE dl_stops SET eta=? WHERE id=?", (t.strftime("%H:%M"), st["id"]))
            stay = (
                int(s["stop_minutes"])
                + (int(s["assembly_minutes"]) if b["assembling"] else 0)
                + 5 * int(b["floors"])
            )
            t += timedelta(minutes=stay)
            total_min += int(travel) + stay
        if matrix is not None and stops:
            total_min += int(matrix[prev][0])
            total_km += haversine_km(coords[prev], coords[0]) * 1.3
        self.db.execute(
            "UPDATE dl_routes SET distance_km=?, duration_min=? WHERE id=?",
            (f"{total_km:.1f}", total_min, rid),
        )

    def reorder(self, rid: int, booking_ids: list[int]) -> dict[str, Any]:
        """Staff drag stops into their own order (an urgent customer first)."""
        route = self._route_row(rid)
        if route["status"] != "planned":
            raise InventoryError(tr("Chỉ sắp xếp được chuyến chưa chạy"))
        stops = {int(s["booking_id"]): s for s in self._stops(rid)}
        if sorted(stops) != sorted(int(b) for b in booking_ids):
            raise InventoryError(tr("Danh sách điểm giao không khớp với chuyến"))
        with self.db.transaction():
            for seq, bid in enumerate(booking_ids, 1):
                self.db.execute("UPDATE dl_stops SET seq=? WHERE id=?", (seq + 1000, stops[int(bid)]["id"]))
            self.db.execute("UPDATE dl_stops SET seq=seq-1000 WHERE route_id=? AND seq>1000", (rid,))
            self.db.execute("UPDATE dl_routes SET optimized_by='manual' WHERE id=?", (rid,))
        self._timetable(rid, None)
        return self.route(rid)

    def remove_stop(self, rid: int, bid: int) -> dict[str, Any]:
        route = self._route_row(rid)
        if route["status"] != "planned":
            raise InventoryError(tr("Chuyến đã chạy"))
        with self.db.transaction():
            self.db.execute("DELETE FROM dl_stops WHERE route_id=? AND booking_id=?", (rid, bid))
            self.db.execute(
                "UPDATE dl_bookings SET status='booked', updated=? WHERE id=? AND status='assigned'",
                (now_iso(), bid),
            )
        self._price(rid)
        self._timetable(rid, None)
        return self.route(rid)

    async def start_route(self, rid: int, actor: str = "") -> dict[str, Any]:
        """The truck leaves: bookings go out for delivery and customers are told."""
        route = self._route_row(rid)
        if route["status"] != "planned":
            raise InventoryError(tr("Chuyến này đã chạy hoặc đã huỷ"))
        with self.db.transaction():
            self.db.execute(
                "UPDATE dl_routes SET status='in_progress', started=? WHERE id=?", (now_iso(), rid)
            )
            for st in self._stops(rid):
                self.db.execute(
                    "UPDATE dl_bookings SET status='shipping', updated=? WHERE id=?",
                    (now_iso(), st["booking_id"]),
                )
        driver = (
            self.db.row("SELECT * FROM dl_drivers WHERE id=?", (route["driver_id"],))
            if route["driver_id"]
            else None
        )
        for st in self._stops(rid):
            b = self._row(int(st["booking_id"]))
            order = self.inv._order_row(int(b["order_id"]))
            if order["conversation_id"]:
                text = (
                    tr(
                        "🚚 Đơn {0} đang được giao hôm nay, dự kiến khoảng {1}",
                        order_code(int(order["id"])),
                        st["eta"] or tr("trong ngày"),
                    )
                    + (tr(". Tài xế {0}, {1}", driver["name"], driver["phone"]) if driver else "")
                    + "."
                )
                self.office.hub.spawn(self.office.hub.notify_customer(int(order["conversation_id"]), text))
        return self.route(rid)

    def stop_result(self, rid: int, bid: int, result: str, note: str = "", actor: str = "") -> dict[str, Any]:
        """done: delivered (the sale is completed); comeback: brought back, goods stay reserved."""
        route = self._route_row(rid)
        if route["status"] != "in_progress":
            raise InventoryError(tr("Chuyến chưa chạy"))
        stop = self.db.row("SELECT * FROM dl_stops WHERE route_id=? AND booking_id=?", (rid, bid))
        if stop is None or stop["status"] != "pending":
            raise InventoryError(tr("Điểm giao này đã có kết quả"))
        if result not in ("done", "comeback"):
            raise InventoryError(tr("Kết quả: done (đã giao) hoặc comeback (quay về)"))
        b = self._row(bid)
        if result == "done":
            order = self.inv._order_row(int(b["order_id"]))
            if order["status"] == "confirmed":
                self.inv.complete_order(int(b["order_id"]), actor)  # goods leave stock: profit realised
        with self.db.transaction():
            self.db.execute(
                "UPDATE dl_stops SET status=?, arrived=? WHERE id=?", (result, now_iso(), stop["id"])
            )
            self.db.execute(
                "UPDATE dl_bookings SET status=?, result_note=?, updated=? WHERE id=?",
                (result, note[:300], now_iso(), bid),
            )
            if not self.db.row("SELECT 1 AS x FROM dl_stops WHERE route_id=? AND status='pending'", (rid,)):
                self.db.execute(
                    "UPDATE dl_routes SET status='completed', finished=? WHERE id=?", (now_iso(), rid)
                )
        return self.route(rid)

    def cancel_route(self, rid: int) -> dict[str, Any]:
        route = self._route_row(rid)
        if route["status"] != "planned":
            raise InventoryError(tr("Chỉ huỷ được chuyến chưa chạy"))
        with self.db.transaction():
            for st in self._stops(rid):
                self.db.execute(
                    "UPDATE dl_bookings SET status='booked', updated=? WHERE id=?",
                    (now_iso(), st["booking_id"]),
                )
            self.db.execute("DELETE FROM dl_stops WHERE route_id=?", (rid,))
            self.db.execute("UPDATE dl_routes SET status='cancelled', cost=0 WHERE id=?", (rid,))
        return self.route(rid)

    def _route_row(self, rid: int) -> dict[str, Any]:
        r = self.db.row("SELECT * FROM dl_routes WHERE id=?", (rid,))
        if r is None:
            raise InventoryError(tr("Không có chuyến này"))
        return r

    def route(self, rid: int) -> dict[str, Any]:
        r = self._route_row(rid)
        carrier = self.db.row("SELECT name FROM dl_carriers WHERE id=?", (r["carrier_id"],)) or {}
        driver = (
            self.db.row("SELECT name, phone, vehicle FROM dl_drivers WHERE id=?", (r["driver_id"],))
            if r["driver_id"]
            else None
        )
        stops = [{**st, "booking": self.booking(int(st["booking_id"]))} for st in self._stops(rid)]
        return {
            **r,
            "code": f"CX{rid:05d}",
            "cost": self.inv.major(r["cost"]),
            "carrier_name": carrier.get("name", ""),
            "driver": driver,
            "origin": self.inv.warehouse(int(r["origin_wh"]))["name"],
            "stops": stops,
            "boxes": sum(int(s["booking"]["boxes"]) for s in stops),
        }

    def routes(self, start: str, end: str) -> list[dict[str, Any]]:
        return [
            self.route(int(r["id"]))
            for r in self.db.rows(
                "SELECT id FROM dl_routes WHERE delivery_date>=? AND delivery_date<=? ORDER BY delivery_date, id",
                (start, end),
            )
        ]

    # reports

    def daily_list_csv(self, rid: int) -> str:
        """The driver's sheet: stop, customer, items and boxes from each warehouse, signature, notes."""
        route = self.route(rid)
        whs = {int(w["id"]): w["code"] for w in self.inv.warehouses()}
        used = sorted({int(s["booking"]["warehouse_id"]) for s in route["stops"]})
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(
            [
                f"{route['code']} · {route['delivery_date']} · {route['carrier_name']}"
                + (f" · {route['driver']['name']} {route['driver']['vehicle']}" if route["driver"] else "")
            ]
        )
        w.writerow(
            [
                "No",
                tr("Khách hàng"),
                *[tr("Từ kho {0}", whs.get(x, x)) for x in used],
                tr("Số kiện"),
                tr("Khách ký nhận"),
                tr("Ghi chú"),
            ]
        )
        for st in route["stops"]:
            b = st["booking"]
            items = "; ".join(f"{i['name']} x{i['qty']}" for i in b["items"])
            notes = ", ".join(
                x
                for x in (
                    b["window"] and tr("Giờ: {0}", b["window"]),
                    b["assembling"] and tr("Lắp ráp"),
                    b["floors"] and tr("Vác {0} tầng", b["floors"]),
                    b["notes"],
                    b["due"] and f"Thu {b['due']:,}",
                )
                if x
            )
            w.writerow(
                [
                    st["seq"],
                    f"{b['customer_name']} - {b['phone']} - {b['address']}",
                    *[items if int(b["warehouse_id"]) == x else "" for x in used],
                    b["boxes"],
                    "",
                    notes,
                ]
            )
        return "﻿" + buf.getvalue()

    def carrier_statement(self, start: str, end: str) -> list[dict[str, Any]]:
        rows = self.db.rows(
            "SELECT c.id, c.name, COUNT(DISTINCT r.id) AS trips, SUM(r.cost) AS cost, "
            "SUM(CASE WHEN s.status='done' THEN 1 ELSE 0 END) AS done, SUM(CASE WHEN s.status='comeback' THEN 1 ELSE 0 END) AS comeback "
            "FROM dl_routes r JOIN dl_carriers c ON c.id=r.carrier_id LEFT JOIN dl_stops s ON s.route_id=r.id "
            "WHERE r.delivery_date>=? AND r.delivery_date<=? AND r.status<>'cancelled' GROUP BY c.id, c.name",
            (start, end),
        )
        # cost counted once per trip (the join repeats it per stop)
        costs = {int(r["carrier_id"]): 0 for r in self.db.rows("SELECT carrier_id FROM dl_routes")}
        for r in self.db.rows(
            "SELECT carrier_id, SUM(cost) AS c FROM dl_routes WHERE delivery_date>=? AND delivery_date<=? "
            "AND status<>'cancelled' GROUP BY carrier_id",
            (start, end),
        ):
            costs[int(r["carrier_id"])] = int(r["c"] or 0)
        return [
            {
                "carrier_id": r["id"],
                "name": r["name"],
                "trips": int(r["trips"]),
                "done": int(r["done"] or 0),
                "comeback": int(r["comeback"] or 0),
                "cost": self.inv.major(costs.get(int(r["id"]), 0)),
            }
            for r in rows
        ]

    def cost_between(self, start: str, end: str) -> int:
        row = self.db.row(
            "SELECT SUM(cost) AS c FROM dl_routes WHERE delivery_date>=? AND delivery_date<=? AND status<>'cancelled'",
            (start, end),
        )
        return int(row["c"] or 0) if row else 0

    def set_warehouse_coords(self, wid: int, lat: Any, lng: Any) -> dict[str, Any]:
        la, lo = self._coords({"lat": lat, "lng": lng})
        self.db.execute("UPDATE inv_warehouses SET lat=?, lng=? WHERE id=?", (la, lo, wid))
        return self.inv.warehouse(wid)
