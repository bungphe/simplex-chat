"""Selling on marketplaces and your own website (sale-management module 4, section 2.2).

When a product's price changes (the nightly stage run, a promotion, a manager) or its
stock changes (a sale, a receipt, a transfer), it is queued and pushed to every
connected marketplace within a minute:

- **Amazon** through the official Selling Partner API: the Listings Items API sets the
  offer price (converted to the marketplace currency) and the quantity available. New
  Amazon orders are pulled every few minutes into the office's own orders (goods
  reserved), completed when Amazon shows them shipped, cancelled when cancelled.
  Credentials come only from environment variables (a Login with Amazon client id and
  secret and the seller's refresh token), named in the settings.
- **Webhook**: your own website (or n8n...) receives each product's price and stock,
  signed with HMAC-SHA256 (header X-Signature: sha256=<hex>).

Each product can use another SKU on a marketplace, or "-" to keep it off it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

import httpx2

from .inventory import InventoryError
from .state import now_iso

if TYPE_CHECKING:
    from .employee import Office

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS mk_sync_queue (
  marketplace TEXT NOT NULL, product_id {int} NOT NULL, reason TEXT NOT NULL, queued TEXT NOT NULL,
  PRIMARY KEY (marketplace, product_id));
CREATE TABLE IF NOT EXISTS mk_sync_log (
  id {id}, ts TEXT NOT NULL, marketplace TEXT NOT NULL, product_id {int}, sku TEXT NOT NULL DEFAULT '',
  ok {int} NOT NULL, detail TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS mk_sync_log_ts ON mk_sync_log (id)
"""
KEY = "marketplaces"
TYPES = ("amazon", "webhook")
AMAZON_HOSTS = {
    "na": "https://sellingpartnerapi-na.amazon.com",
    "eu": "https://sellingpartnerapi-eu.amazon.com",
    "fe": "https://sellingpartnerapi-fe.amazon.com",
}
LWA_TOKEN = "https://api.amazon.com/auth/o2/token"


class MarketplaceError(Exception):
    pass


class Marketplaces:
    def __init__(self, office: Office):
        self.office = office
        self.inv = office.inventory
        self.db = self.inv.db
        self.db.script(SCHEMA)
        self._tokens: dict[str, tuple[str, datetime]] = {}
        self.inv.listeners.append(self.on_event)
        self.pull_minutes = 5

    # settings: credentials are never stored, only the names of environment variables

    def configs(self) -> list[dict[str, Any]]:
        return self.office.docs.get(KEY) or []

    def config(self, mid: str) -> dict[str, Any]:
        for c in self.configs():
            if c["id"] == mid:
                return c
        raise InventoryError("Không có sàn này")

    def save(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        mid = str(data.get("id") or "").strip().lower()
        if not mid or not mid.replace("-", "").replace("_", "").isalnum() or len(mid) > 40:
            raise InventoryError("Mã sàn: chữ thường, số, '-'")
        kind = data.get("type")
        if kind not in TYPES:
            raise InventoryError(f"Loại sàn: {', '.join(TYPES)}")
        c: dict[str, Any] = {
            "id": mid,
            "type": kind,
            "name": str(data.get("name") or mid)[:80],
            "enabled": bool(data.get("enabled", True)),
        }
        env = lambda k: str(data.get(k) or "").strip()[:80]
        if kind == "amazon":
            for key in ("seller_id", "marketplace_id"):
                c[key] = str(data.get(key) or "").strip()[:40]
                if not c[key]:
                    raise InventoryError(f"{key} là bắt buộc")
            c["region"] = data.get("region") or "fe"
            if c["region"] not in AMAZON_HOSTS:
                raise InventoryError("region: na, eu hoặc fe")
            c["currency"] = str(data.get("currency") or "USD").upper()[:3]
            c["price_rate"] = str(Decimal(str(data.get("price_rate") or "1")))
            if Decimal(c["price_rate"]) <= 0:
                raise InventoryError("Tỷ giá quy đổi phải lớn hơn 0")
            c["product_type"] = str(data.get("product_type") or "PRODUCT")[:60]
            for key, default in (
                ("client_id_env", "AMAZON_LWA_CLIENT_ID"),
                ("client_secret_env", "AMAZON_LWA_CLIENT_SECRET"),
                ("refresh_token_env", "AMAZON_REFRESH_TOKEN"),
            ):
                c[key] = env(key) or default
            c["pull_orders"] = bool(data.get("pull_orders"))
            c["warehouse_id"] = int(data["warehouse_id"]) if data.get("warehouse_id") else None
            c["api_url"] = str(data.get("api_url") or "")  # tests / sandbox
            c["token_url"] = str(data.get("token_url") or "")
        else:
            url = str(data.get("url") or "")
            if not url.startswith(("https://", "http://")):
                raise InventoryError("URL webhook phải là http(s)")
            c["url"] = url[:500]
            c["secret_env"] = env("secret_env")
        others = [x for x in self.configs() if x["id"] != mid]
        self.office.docs.update(KEY, lambda d: (d.clear(), d.extend([*others, c])), [])
        return self.public()

    def remove(self, mid: str) -> list[dict[str, Any]]:
        others = [x for x in self.configs() if x["id"] != mid]
        self.office.docs.update(KEY, lambda d: (d.clear(), d.extend(others)), [])
        self.db.execute("DELETE FROM mk_sync_queue WHERE marketplace=?", (mid,))
        return self.public()

    def public(self) -> list[dict[str, Any]]:
        """Settings for the UI: which environment variables are set, never their values."""
        out = []
        for c in self.configs():
            envs = {k: bool(os.environ.get(v)) for k, v in c.items() if k.endswith("_env") and v}
            queued = self.db.row("SELECT COUNT(*) AS n FROM mk_sync_queue WHERE marketplace=?", (c["id"],))
            last = self.db.row(
                "SELECT * FROM mk_sync_log WHERE marketplace=? ORDER BY id DESC LIMIT 1", (c["id"],)
            )
            out.append({**c, "env_set": envs, "queued": int(queued["n"]) if queued else 0, "last": last})
        return out

    # queue

    def on_event(self, event: str, data: dict[str, Any]) -> None:
        if event in ("stock", "prices"):
            self.enqueue(data.get("product_ids") or [], event)

    def enqueue(self, product_ids: list[int], reason: str, marketplace: str | None = None) -> int:
        n = 0
        for c in self.configs():
            if not c.get("enabled") or (marketplace and c["id"] != marketplace):
                continue
            for pid in set(product_ids):
                self.db.execute(
                    "INSERT INTO mk_sync_queue (marketplace, product_id, reason, queued) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (marketplace, product_id) DO UPDATE SET reason=excluded.reason, queued=excluded.queued",
                    (c["id"], pid, reason, now_iso()),
                )
                n += 1
        return n

    def enqueue_all(self, marketplace: str | None = None) -> int:
        ids = [int(r["id"]) for r in self.db.rows("SELECT id FROM inv_products WHERE active=1")]
        return self.enqueue(ids, "full", marketplace)

    def log(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.db.rows("SELECT * FROM mk_sync_log ORDER BY id DESC LIMIT ?", (limit,))

    def _log(self, mid: str, pid: int | None, sku: str, ok: bool, detail: str) -> None:
        self.db.execute(
            "INSERT INTO mk_sync_log (ts, marketplace, product_id, sku, ok, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (now_iso(), mid, pid, sku, 1 if ok else 0, detail[:500]),
        )

    def external_sku(self, product: dict[str, Any], mid: str) -> str | None:
        own = json.loads(product.get("external_skus") or "{}").get(mid)
        if own == "-":
            return None
        return own or product["sku"]

    def offer(self, pid: int) -> dict[str, Any]:
        """What a marketplace is told: the price everyone pays today and the stock to sell."""
        p = self.inv.product_row(pid)
        price = self.inv.current_price(pid)
        summary = (
            next((x for x in self.inv.products(p["sku"], include_inactive=True) if int(x["id"]) == pid), None)
            or {}
        )
        return {
            "sku": p["sku"],
            "name": p["name"],
            "active": bool(p["active"]),
            "price": self.inv.major(price["price"]) if price else None,
            "promo": (price or {}).get("promo", ""),
            "currency": self.inv.settings()["currency"],
            "available": max(0, int(summary.get("available", 0))) if p["active"] else 0,
            "incoming": max(0, int(summary.get("incoming", 0)) - int(summary.get("preordered", 0))),
            "next_eta": summary.get("next_eta", ""),
        }

    async def process(self, limit: int = 100) -> int:
        """Push queued products (oldest first); failures stay queued for the next round."""
        done = 0
        for row in self.db.rows("SELECT * FROM mk_sync_queue ORDER BY queued LIMIT ?", (limit,)):
            mid, pid = row["marketplace"], int(row["product_id"])
            try:
                c = self.config(mid)
            except InventoryError:
                self.db.execute("DELETE FROM mk_sync_queue WHERE marketplace=?", (mid,))
                continue
            product = self.db.row("SELECT * FROM inv_products WHERE id=?", (pid,))
            sku = self.external_sku(product, mid) if product else None
            if product is None or sku is None:
                self.db.execute("DELETE FROM mk_sync_queue WHERE marketplace=? AND product_id=?", (mid, pid))
                continue
            try:
                detail = await (
                    self._push_amazon(c, pid, sku)
                    if c["type"] == "amazon"
                    else self._push_webhook(c, pid, sku)
                )
            except (MarketplaceError, httpx2.HTTPError, ValueError) as e:
                self._log(mid, pid, sku, False, str(e))
                log.warning("marketplace %s: %s not synced: %s", mid, sku, e)
                continue
            self.db.execute(
                "DELETE FROM mk_sync_queue WHERE marketplace=? AND product_id=? AND queued=?",
                (mid, pid, row["queued"]),
            )
            self._log(mid, pid, sku, True, detail)
            done += 1
            if c["type"] == "amazon":
                await asyncio.sleep(0.2)  # the Listings API allows 5 requests a second
        return done

    # webhook

    async def _push_webhook(self, c: dict[str, Any], pid: int, sku: str) -> str:
        body = json.dumps({"event": "product", **self.offer(pid), "sku": sku}, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json"}
        secret = os.environ.get(c.get("secret_env") or "", "")
        if secret:
            headers["X-Signature"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        r = await self.office.http_client.post(c["url"], content=body, headers=headers, timeout=20)
        if r.status_code >= 300:
            raise MarketplaceError(f"HTTP {r.status_code}")
        return f"HTTP {r.status_code}"

    # Amazon Selling Partner API

    async def _token(self, c: dict[str, Any]) -> str:
        cached = self._tokens.get(c["id"])
        if cached and cached[1] > datetime.now(UTC):
            return cached[0]
        values = {
            k: os.environ.get(c[f"{k}_env"], "") for k in ("client_id", "client_secret", "refresh_token")
        }
        if not all(values.values()):
            missing = [c[f"{k}_env"] for k, v in values.items() if not v]
            raise MarketplaceError(f"thiếu biến môi trường {', '.join(missing)}")
        r = await self.office.http_client.post(
            c.get("token_url") or LWA_TOKEN,
            data={"grant_type": "refresh_token", **values},
            timeout=20,
        )
        data = r.json()
        if r.status_code != 200 or "access_token" not in data:
            raise MarketplaceError(
                f"Login with Amazon: {data.get('error_description') or data.get('error') or r.status_code}"
            )
        self._tokens[c["id"]] = (
            data["access_token"],
            datetime.now(UTC) + timedelta(seconds=int(data.get("expires_in", 3600)) - 120),
        )
        return data["access_token"]

    def _host(self, c: dict[str, Any]) -> str:
        return (c.get("api_url") or AMAZON_HOSTS[c["region"]]).rstrip("/")

    async def _amazon(self, c: dict[str, Any], method: str, path: str, **kw: Any) -> httpx2.Response:
        token = await self._token(c)
        r = await self.office.http_client.request(
            method,
            f"{self._host(c)}{path}",
            headers={"x-amz-access-token": token, "accept": "application/json"},
            timeout=30,
            **kw,
        )
        if r.status_code == 429:
            raise MarketplaceError("Amazon: quá nhiều yêu cầu, thử lại sau")
        return r

    def _amazon_price(self, c: dict[str, Any], price: float) -> float:
        value = Decimal(str(price)) * Decimal(c["price_rate"])
        return float(value.quantize(Decimal("0.01"), ROUND_HALF_UP))

    async def _push_amazon(self, c: dict[str, Any], pid: int, sku: str) -> str:
        offer = self.offer(pid)
        patches: list[dict[str, Any]] = [
            {
                "op": "replace",
                "path": "/attributes/fulfillment_availability",
                "value": [{"fulfillment_channel_code": "DEFAULT", "quantity": offer["available"]}],
            }
        ]
        if offer["price"] is not None:
            patches.insert(
                0,
                {
                    "op": "replace",
                    "path": "/attributes/purchasable_offer",
                    "value": [
                        {
                            "marketplace_id": c["marketplace_id"],
                            "currency": c["currency"],
                            "our_price": [
                                {"schedule": [{"value_with_tax": self._amazon_price(c, offer["price"])}]}
                            ],
                        }
                    ],
                },
            )
        from urllib.parse import quote

        r = await self._amazon(
            c,
            "PATCH",
            f"/listings/2021-08-01/items/{quote(c['seller_id'])}/{quote(sku, safe='')}",
            params={"marketplaceIds": c["marketplace_id"]},
            json={"productType": c.get("product_type") or "PRODUCT", "patches": patches},
        )
        data = r.json() if r.content else {}
        issues = [i.get("message", "") for i in data.get("issues") or [] if i.get("severity") == "ERROR"]
        if r.status_code >= 300 or data.get("status") not in (None, "ACCEPTED") or issues:
            raise MarketplaceError(
                f"Amazon {r.status_code} {data.get('status', '')}: {'; '.join(issues)[:300]}".strip()
            )
        price = (
            f"{self._amazon_price(c, offer['price'])} {c['currency']}" if offer["price"] is not None else "-"
        )
        return f"giá {price}, còn {offer['available']}"

    async def pull_amazon_orders(self, c: dict[str, Any]) -> dict[str, int]:
        """New Amazon orders become orders here (goods reserved); shipped ones are completed,
        cancelled ones cancelled. Only SKUs and quantities are read (no customer data)."""
        state_key = f"marketplace_cursor:{c['id']}"
        cursor = (self.office.docs.get(state_key) or {}).get("after") or (
            datetime.now(UTC) - timedelta(days=2)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = await self._amazon(
            c,
            "GET",
            "/orders/v0/orders",
            params={"MarketplaceIds": c["marketplace_id"], "LastUpdatedAfter": cursor},
        )
        if r.status_code != 200:
            raise MarketplaceError(f"Amazon orders: HTTP {r.status_code}")
        payload = r.json().get("payload") or {}
        stats = {"created": 0, "completed": 0, "cancelled": 0}
        latest = cursor
        for o in payload.get("Orders") or []:
            amazon_id, status = o.get("AmazonOrderId"), o.get("OrderStatus")
            latest = max(latest, o.get("LastUpdateDate") or latest)
            ref = f"amazon:{amazon_id}"
            ours = self.db.row("SELECT id, status FROM inv_orders WHERE external_ref=?", (ref,))
            try:
                if ours is None and status in ("Unshipped", "PartiallyShipped", "Shipped"):
                    ours = await self._create_amazon_order(c, amazon_id, ref)
                    stats["created"] += 1 if ours else 0
                if ours and status == "Shipped" and ours["status"] == "confirmed":
                    self.inv.complete_order(int(ours["id"]), "Amazon")
                    stats["completed"] += 1
                elif ours and status == "Canceled" and ours["status"] == "confirmed":
                    self.inv.cancel_order(int(ours["id"]), "Amazon")
                    stats["cancelled"] += 1
            except InventoryError as e:
                self._log(c["id"], None, amazon_id or "", False, f"đơn Amazon {amazon_id}: {e}")
        self.office.docs.update(state_key, lambda d: d.update(after=latest), {})
        return stats

    async def _create_amazon_order(
        self, c: dict[str, Any], amazon_id: str, ref: str
    ) -> dict[str, Any] | None:
        r = await self._amazon(c, "GET", f"/orders/v0/orders/{amazon_id}/orderItems")
        items = []
        rate = Decimal(c["price_rate"])
        for it in (r.json().get("payload") or {}).get("OrderItems") or []:
            seller_sku, qty = str(it.get("SellerSKU") or ""), int(it.get("QuantityOrdered") or 0)
            if qty <= 0:
                continue
            product = self._by_external_sku(c["id"], seller_sku)
            if product is None:
                raise InventoryError(f"SKU Amazon {seller_sku} không khớp sản phẩm nào")
            line = {"product_id": product["id"], "qty": qty}
            amount = (it.get("ItemPrice") or {}).get("Amount")
            if amount:
                line["unit_price"] = str(
                    (Decimal(str(amount)) / qty / rate).quantize(
                        Decimal(1) if not self.inv.settings()["decimals"] else Decimal("0.01")
                    )
                )
            items.append(line)
        if not items:
            return None
        order = self.inv.create_order(
            items,
            warehouse_id=c.get("warehouse_id"),
            customer_name=f"Amazon {amazon_id}",
            channel="amazon",
            source=f"amazon:{c['id']}",
            external_ref=ref,
            actor="Amazon",
        )
        return {"id": order["id"], "status": order["status"]}

    def _by_external_sku(self, mid: str, sku: str) -> dict[str, Any] | None:
        for p in self.db.rows("SELECT * FROM inv_products WHERE external_skus<>'{}'"):
            if json.loads(p["external_skus"] or "{}").get(mid) == sku:
                return p
        return self.inv.by_sku(sku)

    def set_external_sku(self, pid: int, mid: str, sku: str) -> dict[str, Any]:
        self.config(mid)
        p = self.inv.product_row(pid)
        skus = json.loads(p["external_skus"] or "{}")
        if sku.strip():
            skus[mid] = sku.strip()[:60]
        else:
            skus.pop(mid, None)
        self.db.execute("UPDATE inv_products SET external_skus=? WHERE id=?", (json.dumps(skus), pid))
        self.enqueue([pid], "sku", mid)
        return {"product_id": pid, "external_skus": skus}

    # the background loop (primary shard)

    async def run(self, stopping: asyncio.Event) -> None:
        last_pull = datetime.min.replace(tzinfo=UTC)
        while not stopping.is_set():
            try:
                await self.process()
                if datetime.now(UTC) - last_pull >= timedelta(minutes=self.pull_minutes):
                    last_pull = datetime.now(UTC)
                    for c in self.configs():
                        if c.get("enabled") and c["type"] == "amazon" and c.get("pull_orders"):
                            try:
                                await self.pull_amazon_orders(c)
                            except (MarketplaceError, httpx2.HTTPError, ValueError) as e:
                                self._log(c["id"], None, "", False, f"lấy đơn: {e}")
            except Exception:
                log.exception("marketplace: sync round failed")
            try:
                await asyncio.wait_for(stopping.wait(), timeout=60)
            except TimeoutError:
                pass
