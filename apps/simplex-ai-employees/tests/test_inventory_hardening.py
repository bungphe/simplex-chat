"""Regression tests: concurrent order changes and payments, costing extra stock, partial
receipts and split pre-orders, search in Vietnamese, commissions saved twice, Amazon
order paging, returns giving back vouchers, closing a part-received purchase order."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from ai_employees.db import Database, DocStore
from ai_employees.inventory import Inventory, InventoryError

from fakes import ScriptedLLM, make_office, postgres_schema


def _db(tmp_path) -> Database:
    if base := os.environ.get("AIE_TEST_DATABASE_URL"):
        return Database(postgres_schema(base, tmp_path))
    return Database(str(tmp_path / "inv.sqlite"))


@pytest.fixture
def inv(tmp_path):
    db = _db(tmp_path)
    inv = Inventory(db, DocStore(db))
    inv.kho = inv.save_warehouse(None, {"code": "kho", "name": "Kho tổng"})["id"]
    inv.shop = inv.save_warehouse(None, {"code": "Q1", "name": "Cửa hàng Quận 1", "kind": "store"})["id"]
    inv.ncc = inv.save_supplier(None, {"name": "Foshan"})["id"]
    inv.a = inv.save_product(None, {"sku": "den-1", "name": "Đèn bàn gỗ", "category": "Đèn"})["id"]
    inv.b = inv.save_product(None, {"sku": "ghe-2", "name": "Ghế ăn"})["id"]
    inv.tmp_path = tmp_path
    return inv


def other_process(inv, tmp_path):
    """A second office process on the same database."""
    db = _db(tmp_path)
    return Inventory(db, DocStore(db))


def po(inv, qty, wh=None, pid=None, cost="100000"):
    made = inv.save_po(
        None,
        {
            "supplier_id": inv.ncc,
            "warehouse_id": wh or inv.kho,
            "items": [{"product_id": pid or inv.a, "qty": qty, "unit_cost_foreign": cost}],
        },
    )
    return inv.set_po_status(made["id"], "ordered")


def stale(inv2, inv, oid):
    """Process 2 read the order before process 1 changed it."""
    row = inv._order_row(oid)
    inv2._order_row = lambda _oid: row


# 1. a status change or a payment is claimed once, whatever another process read before


def test_second_process_cannot_complete_cancel_or_return_twice(inv):
    inv2 = other_process(inv, inv.tmp_path)
    inv.add_opening_stock(inv.a, inv.kho, 10, 100000)
    o1 = inv.create_order([{"product_id": inv.a, "qty": 2}])
    stale(inv2, inv, o1["id"])
    inv.complete_order(o1["id"])
    with pytest.raises(InventoryError, match="completed"):
        inv2.complete_order(o1["id"])
    with pytest.raises(InventoryError, match="completed"):
        inv2.cancel_order(o1["id"])
    p = inv.product(inv.a)
    assert (p["on_hand"], p["reserved"]) == (8, 0)
    assert p["lots"][0]["remaining_qty"] == 8 and p["lots"][0]["reserved_qty"] == 0

    o2 = inv.create_order([{"product_id": inv.a, "qty": 3}])
    stale(inv2, inv, o2["id"])
    inv.cancel_order(o2["id"])
    with pytest.raises(InventoryError, match="cancelled"):
        inv2.cancel_order(o2["id"])
    assert inv.product(inv.a)["reserved"] == 0

    stale(inv2, inv, o1["id"])
    inv.return_order(o1["id"])
    with pytest.raises(InventoryError, match="đã giao"):
        inv2.return_order(o1["id"])
    p = inv.product(inv.a)
    assert (p["on_hand"], p["reserved"], p["lots"][0]["remaining_qty"]) == (10, 0, 10)


def test_payments_are_never_lost_or_over_the_total(inv):
    inv2 = other_process(inv, inv.tmp_path)
    inv.add_opening_stock(inv.a, inv.kho, 10, 100000, price1=300000)
    o = inv.create_order([{"product_id": inv.a, "qty": 1}])
    stale(inv2, inv, o["id"])
    inv.add_payment(o["id"], "cash", 100000)
    inv2.add_payment(o["id"], "card", 100000)
    got = inv.order(o["id"])
    assert (got["paid"], got["due"], got["payment_status"]) == (200000, 100000, "partial")
    stale(inv2, inv, o["id"])
    inv.add_payment(o["id"], "transfer", 50000)
    with pytest.raises(InventoryError, match="50,000"):
        inv2.add_payment(o["id"], "card", 100000)  # only 50.000 was still due
    # cash without an amount: the rest, with change from what was handed over
    inv2.add_payment(o["id"], "cash", tendered=100000)
    got = inv.order(o["id"])
    assert (got["paid"], got["payment_status"], got["payments"][-1]["change"]) == (300000, "paid", 50000)
    assert sum(p["amount"] for p in got["payments"]) == 300000


def test_same_payment_sent_twice_at_once_is_recorded_once(inv):
    inv2 = other_process(inv, inv.tmp_path)
    inv.add_opening_stock(inv.a, inv.kho, 10, 100000, price1=300000)
    o = inv.create_order([{"product_id": inv.a, "qty": 1}])
    inv.add_payment(o["id"], "transfer", 100000, idempotency_key="pay-1")
    real_row = inv2.db.row
    # process 2 checked the key just before process 1 recorded it
    inv2.db.row = lambda sql, params=(): None if "idempotency_key" in sql else real_row(sql, params)
    got = inv2.add_payment(o["id"], "transfer", 100000, idempotency_key="pay-1")
    assert got["paid"] == 100000 and len(got["payments"]) == 1


# 2. extra goods found in a count are costed; a line needs a price


def test_count_of_a_sold_out_product_keeps_its_cost_and_prices(inv):
    inv.add_opening_stock(inv.a, inv.kho, 2, 1_000_000)
    inv.complete_order(inv.create_order([{"product_id": inv.a, "qty": 2}])["id"])
    inv.run_pricing()
    assert inv.current_price(inv.a) is None  # sold out
    inv.adjust(inv.a, inv.kho, 3, "found in the back room")
    price = inv.current_price(inv.a)
    assert price["price"] == 1_667_000
    o = inv.create_order([{"product_id": inv.a, "qty": 1}])
    assert (o["total"], o["cost"]) == (1_667_000, 1_000_000)
    # never received: the count needs the cost
    with pytest.raises(InventoryError, match="giá vốn"):
        inv.adjust(inv.b, inv.kho, 3, "found")
    assert inv.adjust(inv.b, inv.kho, 3, "found", unit_cost=200000)["price"] == 333_000


def test_a_line_without_a_price_needs_one_set_by_staff(inv):
    inv.add_opening_stock(inv.a, inv.kho, 5, 100000)
    lot = inv.product(inv.a)["lots"][0]
    inv.set_lot_prices(lot["id"], [0, 0, 0, 0, 0], None, "Chủ")
    with pytest.raises(InventoryError, match="DEN-1: chưa có giá"):
        inv.create_order([{"product_id": inv.a, "qty": 1}])
    assert inv.product(inv.a)["reserved"] == 0
    gift = inv.create_order([{"product_id": inv.a, "qty": 1, "unit_price": 0}])  # a gift, on purpose
    assert gift["total"] == 0
    assert inv.create_order([{"product_id": inv.a, "qty": 1, "unit_price": 250000}])["total"] == 250000


# 3-5. pre-orders: partial receipts, one warehouse, split over purchase orders


def test_preorder_waits_then_takes_free_stock_of_its_warehouse(inv):
    order = po(inv, 10)
    item = order["items"][0]["id"]
    small = inv.create_order([{"product_id": inv.a, "qty": 2}], kind="preorder")
    big = inv.create_order([{"product_id": inv.a, "qty": 5}], kind="preorder")
    first = inv.receive_po(order["id"], [{"item_id": item, "qty": 3}])
    assert first["served_preorders"] == [small["id"]]  # the big one does not fit yet; not a stop
    assert inv.order(big["id"])["items"][0]["status"] == "awaiting"
    second = inv.receive_po(order["id"], [{"item_id": item, "qty": 4}])
    assert second["served_preorders"] == [big["id"]]  # 1 left from the first receipt + 4
    p = inv.product(inv.a)
    assert (p["on_hand"], p["reserved"], p["available"], p["preordered"]) == (7, 7, 0, 0)
    assert sum(x["preordered_qty"] for x in p["lots"]) == 7
    inv.cancel_order(big["id"])  # pre-sold marks go back with the goods
    p = inv.product(inv.a)
    assert (p["reserved"], sum(x["preordered_qty"] for x in p["lots"])) == (2, 2)
    inv.complete_order(small["id"])
    assert inv.product(inv.a)["on_hand"] == 5


def test_preorder_served_from_other_stock_keeps_lot_marks_right(inv):
    inv.add_opening_stock(inv.a, inv.kho, 4, 100000)
    held = inv.create_order([{"product_id": inv.a, "qty": 4}])
    order = po(inv, 2)
    with pytest.raises(InventoryError, match="sắp về"):
        inv.create_order([{"product_id": inv.a, "qty": 3}], kind="preorder")
    pre = inv.create_order([{"product_id": inv.a, "qty": 2}], kind="preorder")
    inv.cancel_order(held["id"])  # 4 free on the shelf now
    got = inv.receive_po(order["id"], [{"item_id": order["items"][0]["id"], "qty": 2}])
    assert got["served_preorders"] == [pre["id"]]
    lots = {x["source"]: x for x in inv.product(inv.a)["lots"]}
    assert (lots["po"]["reserved_qty"], lots["po"]["preordered_qty"]) == (2, 2)  # the goods it waited for
    assert (lots["opening"]["reserved_qty"], lots["opening"]["preordered_qty"]) == (0, 0)
    inv.cancel_order(pre["id"])
    assert all(x["preordered_qty"] == 0 and x["reserved_qty"] == 0 for x in inv.product(inv.a)["lots"])


def test_preorder_only_on_goods_coming_to_its_warehouse(inv):
    to_shop = po(inv, 5, wh=inv.shop)
    with pytest.raises(InventoryError, match="sắp về"):
        inv.create_order([{"product_id": inv.a, "qty": 2}], kind="preorder")  # the default: kho
    with pytest.raises(InventoryError, match="sắp về"):
        inv.create_order([{"product_id": inv.a, "qty": 2, "po_id": to_shop["id"]}], kind="preorder")
    pre = inv.create_order([{"product_id": inv.a, "qty": 2}], kind="preorder", warehouse_id=inv.shop)
    inv.receive_po(to_shop["id"], [{"item_id": to_shop["items"][0]["id"], "qty": 5}])
    assert inv.order(pre["id"])["warehouse_id"] == inv.shop
    inv.complete_order(pre["id"])
    by_wh = {x["warehouse_id"]: x["on_hand"] for x in inv.product(inv.a)["by_warehouse"]}
    assert by_wh == {inv.shop: 3}


def test_preorder_split_over_several_purchase_orders(inv):
    first, second = po(inv, 3, cost="100000"), po(inv, 4, cost="200000")
    po(inv, 9, wh=inv.shop)  # another warehouse: not counted for kho
    with pytest.raises(InventoryError, match="sắp về"):
        inv.create_order([{"product_id": inv.a, "qty": 8}], kind="preorder")
    pre = inv.create_order([{"product_id": inv.a, "qty": 7}], kind="preorder")
    lines = pre["items"]
    assert [(x["qty"], x["po_number"]) for x in lines] == [(3, first["po_number"]), (4, second["po_number"])]
    # not in stock yet: each part at its own purchase order's price
    assert [x["unit_price"] for x in lines] == [167000, 333000] and pre["total"] == 3 * 167000 + 4 * 333000
    assert inv.product(inv.a)["preordered"] == 7
    inv.receive_po(first["id"], [{"item_id": first["items"][0]["id"], "qty": 3}])
    with pytest.raises(InventoryError, match="chờ hàng"):
        inv.complete_order(pre["id"])
    inv.receive_po(second["id"], [{"item_id": second["items"][0]["id"], "qty": 4}])
    assert inv.complete_order(pre["id"])["cost"] == 3 * 100000 + 4 * 200000


# 6. search in any script


def test_search_is_case_insensitive_in_vietnamese(inv):
    inv.add_opening_stock(inv.a, inv.kho, 3, 100000)
    for q in ("đèn", "Đèn", "ĐÈN BÀN", "den-1", "DEN-1"):
        assert [p["sku"] for p in inv.products(q)] == ["DEN-1"], q
    assert [x["sku"] for x in inv.lookup("đèn gỗ")] == ["DEN-1"]
    assert [x["sku"] for x in inv.lookup("Đèn")] == ["DEN-1"]
    assert inv.lookup("đèn nhựa") == []
    inv.save_product(inv.a, {"name": "Tủ Áo"})
    assert [p["sku"] for p in inv.products("tủ áo")] == ["DEN-1"]
    # rows from before the search column: filled in at start
    inv.db.execute("UPDATE inv_products SET search_text=''")
    again = Inventory(inv.db, inv.docs)
    assert [p["sku"] for p in again.products("ghế")] == ["GHE-2"]


# 11-12. messages, returns and closing a purchase order


def test_duplicate_code_messages_and_voucher_back_on_return(inv):
    with pytest.raises(InventoryError, match="Mã kho đã tồn tại"):
        inv.save_warehouse(None, {"code": "KHO", "name": "x"})
    inv.save_voucher(None, {"code": "GIAM10", "discount_type": "pct", "value": 10, "max_uses": 1})
    inv.add_opening_stock(inv.a, inv.kho, 5, 100000)
    o = inv.create_order([{"product_id": inv.a, "qty": 1}], voucher="giam10")
    assert inv.vouchers()[0]["used"] == 1
    inv.complete_order(o["id"])
    inv.return_order(o["id"])
    assert inv.vouchers()[0]["used"] == 0
    inv.create_order([{"product_id": inv.a, "qty": 1}], voucher="GIAM10")  # can be used again


def test_part_received_purchase_order_can_be_closed(inv):
    order = po(inv, 10)
    item = order["items"][0]["id"]
    inv.receive_po(order["id"], [{"item_id": item, "qty": 4}])
    pre = inv.create_order([{"product_id": inv.a, "qty": 6}], kind="preorder")
    with pytest.raises(InventoryError, match="đặt trước"):
        inv.set_po_status(order["id"], "received")
    inv.cancel_order(pre["id"])
    assert inv.product(inv.a)["incoming"] == 6
    assert inv.set_po_status(order["id"], "received")["status"] == "received"
    assert inv.product(inv.a)["incoming"] == 0
    with pytest.raises(InventoryError):
        inv.receive_po(order["id"], [{"item_id": item, "qty": 1}])


# 7, 9, 10: sales and marketplaces


class Amazon:
    def __init__(self, pages: list[list[dict]]):
        self.pages = pages
        self.queries: list[dict] = []
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.route))

    def route(self, r: httpx2.Request) -> httpx2.Response:
        if r.url.host == "lwa.local":
            return httpx2.Response(200, json={"access_token": "t", "expires_in": 3600})
        if r.url.path == "/orders/v0/orders":
            q = dict(r.url.params)
            self.queries.append(q)
            n = int(q.get("NextToken", "0"))
            payload = {"Orders": self.pages[n]}
            if n + 1 < len(self.pages):
                payload["NextToken"] = str(n + 1)
            return httpx2.Response(200, json={"payload": payload})
        if r.url.path.endswith("/orderItems"):
            sku = "NOPE" if "bad" in r.url.path else "DEN-1"
            return httpx2.Response(
                200, json={"payload": {"OrderItems": [{"SellerSKU": sku, "QuantityOrdered": 1}]}}
            )
        return httpx2.Response(404)


def amazon_order(n, when, status="Unshipped"):
    return {"AmazonOrderId": n, "OrderStatus": status, "LastUpdateDate": when}


async def test_amazon_orders_follow_pages_and_retry_failed_ones(tmp_path, monkeypatch):
    for k in ("AMZ_ID", "AMZ_SECRET", "AMZ_REFRESH"):
        monkeypatch.setenv(k, "x")
    # yesterday: within the first window read (the last two days)
    day = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    amazon = Amazon(
        [
            [amazon_order("1", f"{day}T01:00:00Z"), amazon_order("bad", f"{day}T02:00:00Z")],
            [amazon_order("3", f"{day}T03:00:00Z")],
        ]
    )
    office = make_office(tmp_path, ScriptedLLM(), http=amazon.client)
    inv = office.inventory
    kho = inv.save_warehouse(None, {"code": "KHO", "name": "Kho"})["id"]
    pid = inv.save_product(None, {"sku": "DEN-1", "name": "Đèn"})["id"]
    inv.add_opening_stock(pid, kho, 5, 100000)
    mp = office.marketplaces
    mp.save(
        {
            "id": "amz",
            "type": "amazon",
            "seller_id": "S",
            "marketplace_id": "M",
            "client_id_env": "AMZ_ID",
            "client_secret_env": "AMZ_SECRET",
            "refresh_token_env": "AMZ_REFRESH",
            "api_url": "https://sp.local",
            "token_url": "https://lwa.local/token",
        }
    )
    stats = await mp.pull_amazon_orders(mp.config("amz"))
    assert stats["created"] == 2  # both pages; the order with an unknown SKU failed
    assert amazon.queries[1] == {"MarketplaceIds": "M", "NextToken": "1"}
    assert office.docs.get("marketplace_cursor:amz")["after"] == f"{day}T01:59:59Z"
    amazon.pages = [[amazon_order("bad", f"{day}T02:00:00Z"), amazon_order("3", f"{day}T03:00:00Z")]]
    assert (await mp.pull_amazon_orders(mp.config("amz")))["created"] == 0  # seen again, not twice
    assert amazon.queries[-1]["LastUpdatedAfter"] == f"{day}T01:59:59Z"
    assert len([o for o in inv.orders() if o["channel"] == "amazon"]) == 2
    # what marketplaces are told comes from the product itself, not a text search
    offer = mp.offer(pid)
    assert (offer["available"], offer["sku"]) == (3, "DEN-1")


def test_commissions_saved_twice_replace_the_drafts(tmp_path):
    office = make_office(tmp_path, ScriptedLLM())
    sales = office.sales
    day = "2026-09-01"
    sales.add_shift("lan", day, 8)
    sales.add_shift("minh", day, 4)
    sales.save_commissions(day, day, "Chủ")
    sales.save_commissions(day, day, "Chủ")
    rows = sales.commissions()
    assert sorted(r["username"] for r in rows) == ["lan", "minh"]
    sales.set_commission_status(rows[0]["id"], "finalized")
    with pytest.raises(InventoryError, match="đã chốt"):
        sales.save_commissions(day, day, "Chủ")
    assert len(sales.commissions()) == 2
    sales.set_commission_status(rows[0]["id"], "draft")
    sales.save_commissions(day, day, "Chủ")
    assert len(sales.commissions()) == 2


def test_segment_of_simplex_customers(tmp_path):
    office = make_office(tmp_path, ScriptedLLM())
    crm, db = office.hub.crm, office.inventory.db
    c = crm.create_contact("Lan", "0900000001")
    other = crm.create_contact("Minh", "0900000002")
    for cid, channel in ((c["id"], "simplex:bot"), (other["id"], "telegram:1")):
        conv = db.execute(
            "INSERT INTO conversations (channel, external_id, created) VALUES (?, ?, ?) RETURNING id",
            (channel, f"x{cid}", "2026-09-01T00:00:00+00:00"),
        )
        db.execute("INSERT INTO crm_links (conversation_id, contact_id) VALUES (?, ?)", (conv, cid))
    rows = office.sales.segment(channel_type="simplex", min_orders=0)
    assert [r["name"] for r in rows] == ["Lan"]
