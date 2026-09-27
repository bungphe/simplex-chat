"""Products, purchasing with landed cost, FIFO lots, the 5-stage pricing engine, sales
orders and pre-orders, transfers, stock counts and reorder suggestions."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

from ai_employees.db import Database, DocStore
from ai_employees.inventory import Inventory, InventoryError

from fakes import postgres_schema


@pytest.fixture
def inv(tmp_path):
    if base := os.environ.get("AIE_TEST_DATABASE_URL"):
        db = Database(postgres_schema(base, tmp_path))
    else:
        db = Database(str(tmp_path / "inv.sqlite"))
    inv = Inventory(db, DocStore(db))
    inv.kho = inv.save_warehouse(None, {"code": "kho", "name": "Kho tổng Bình Dương"})["id"]
    inv.shop = inv.save_warehouse(None, {"code": "Q1", "name": "Cửa hàng Quận 1", "kind": "store"})["id"]
    inv.ncc = inv.save_supplier(None, {"name": "Foshan Furniture", "country": "CN", "lead_time_days": 45})[
        "id"
    ]
    inv.a = inv.save_product(
        None, {"sku": "sofa-01", "name": "Sofa da 3 chỗ", "cbm": "1.2", "category": "Phòng khách"}
    )["id"]
    inv.b = inv.save_product(None, {"sku": "ghe-02", "name": "Ghế ăn gỗ sồi", "cbm": "0.5"})["id"]
    return inv


def container(inv, qty_a=10, qty_b=20, **extra):
    """USD 100 and 40 a piece at 25.000 đ; freight 22 triệu and customs 4,4 triệu for 22 CBM."""
    po = inv.save_po(
        None,
        {
            "supplier_id": inv.ncc,
            "warehouse_id": inv.kho,
            "exchange_rate": "25000",
            "freight": 22_000_000,
            "customs": 4_400_000,
            "eta": "2026-10-15",
            "items": [
                {"product_id": inv.a, "qty": qty_a, "unit_cost_foreign": "100", "margin_pct": 40},
                {"product_id": inv.b, "qty": qty_b, "unit_cost_foreign": "40", "margin_pct": 40},
            ],
            **extra,
        },
        actor="Thu",
    )
    return inv.set_po_status(po["id"], "ordered")


def test_landed_cost_and_two_way_pricing(inv):
    po = container(inv)
    a, b = po["items"]
    assert po["total_cbm"] == "22.0"
    # freight 1 triệu / CBM, customs 200.000 / CBM
    assert (a["unit_cost"], a["unit_freight"], a["unit_tax"], a["landed_cost"]) == (
        2_500_000,
        1_200_000,
        240_000,
        3_940_000,
    )
    assert b["landed_cost"] == 1_000_000 + 500_000 + 100_000
    # stage 1 = cost / (1 - 40%), rounded to 1.000 đ; stages 2-5: -10%, -25%, -35%, -50%
    assert a["prices"] == [6_567_000, 5_910_000, 4_925_000, 4_269_000, 3_284_000]
    assert a["below_cost"] == [5]
    # the other way round: the margin of a chosen price
    plan = inv.price_plan(3_940_000, price1=7_880_000)
    assert plan["margin_pct"] == 50.0 and plan["profit"] == 3_940_000
    with pytest.raises(InventoryError):
        inv.price_plan(1000, margin_pct=100)
    # no volumes: freight shared by value
    costs = inv.landed_costs(
        [{"qty": 1, "unit_cost_foreign": "30"}, {"qty": 1, "unit_cost_foreign": "10"}], "1", 400, 0
    )
    assert [c["unit_freight"] for c in costs] == [300, 100]


def test_fifo_lots_and_the_pricing_engine(inv):
    po = container(inv)
    a_item = po["items"][0]
    inv.receive_po(po["id"], [{"item_id": a_item["id"], "qty": 10}], actor="Thu")
    p = inv.product(inv.a)
    assert (p["on_hand"], p["available"], p["price"], p["stage"]) == (10, 10, 6_567_000, 1)
    assert inv.po(po["id"])["status"] == "partial"

    # a second container arrives: its lot waits behind the first
    po2 = container(inv, qty_a=5, qty_b=1)
    inv.receive_po(po2["id"], [{"item_id": po2["items"][0]["id"], "qty": 5}])
    lots = inv.product(inv.a)["lots"]
    assert [x["status"] for x in lots] == ["active", "queued"]

    order = inv.create_order([{"sku": "SOFA-01", "qty": 3}], warehouse_id=inv.kho, customer_name="Chị Mai")
    assert order["total"] == 3 * 6_567_000 and order["cost"] == 3 * 3_940_000
    assert inv.product(inv.a)["available"] == 12
    done = inv.complete_order(order["id"])
    assert done["status"] == "completed" and done["profit"] == 3 * (6_567_000 - 3_940_000)
    p = inv.product(inv.a)
    assert (p["on_hand"], p["reserved"]) == (12, 0)
    assert p["lots"][0]["remaining_qty"] == 7 and p["lots"][0]["remaining_pct"] == 70.0

    now = datetime.now().astimezone()
    assert inv.run_pricing(now + timedelta(days=3)) == []  # 70% left but only 3 days at this price
    [change] = inv.run_pricing(now + timedelta(days=8))  # 70% <= 80% and 8 >= 7 days
    assert (change["old_stage"], change["new_stage"], change["new_price"]) == (1, 2, 5_910_000)
    assert "70%" in change["reason"]
    assert inv.run_pricing(now + timedelta(days=9)) == []  # 70% > 60%, 1 day at stage 2
    [change] = inv.run_pricing(now + timedelta(days=40))  # held 32 days >= 30: slow seller, marked down
    assert change["new_stage"] == 3 and "tối đa" in change["reason"]
    assert inv.current_price(inv.a)["price"] == 4_925_000
    # VIP: the next stage's price, or the lot's own VIP price
    assert inv.current_price(inv.a, vip=True)["price"] == 4_269_000
    inv.save_product(inv.a, {"vip_price": 4_000_000})
    assert inv.current_price(inv.a, vip=True)["price"] == 4_000_000

    # the old lot sells out: the next container's lot becomes active at stage 1
    inv.complete_order(inv.create_order([{"product_id": inv.a, "qty": 7}])["id"])
    assert inv.current_price(inv.a)["stage"] == 3  # until the night run: no price change mid-day
    [change] = inv.run_pricing(now + timedelta(days=41))
    # the smaller container carries the same freight over fewer CBM: a higher cost and price
    second_price = po2["items"][0]["prices"][0]
    assert change["event"] == "new_lot" and change["new_price"] == second_price > 6_567_000
    lots = inv.product(inv.a)["lots"]
    assert [x["status"] for x in lots] == ["active", "exhausted"]
    assert inv.current_price(inv.a) == {
        "price": second_price,
        "stage": 1,
        "lot_id": lots[0]["id"],
        "vip": False,
    }
    log = inv.price_log()
    assert [x["trigger"] for x in log][:3] == ["activate", "system", "system"]

    # a manager can set the stage by hand; automatic pricing can be switched off
    inv.set_stage(inv.a, 4, "Chủ", "Xả hàng cuối năm")
    assert inv.current_price(inv.a)["stage"] == 4 and inv.price_log()[0]["trigger"] == "manual"
    inv.save_product(inv.a, {"auto_pricing": False})
    assert inv.run_pricing(now + timedelta(days=400)) == []


def test_orders_preorders_and_cancelling(inv):
    po = container(inv, qty_a=4)
    inv.receive_po(po["id"], [{"item_id": po["items"][0]["id"], "qty": 4}])
    with pytest.raises(InventoryError, match="chỉ còn 4"):
        inv.create_order([{"product_id": inv.a, "qty": 5}])
    order = inv.create_order([{"product_id": inv.a, "qty": 4, "unit_price": 6_000_000}], discount=500_000)
    assert order["total"] == 4 * 6_000_000 - 500_000
    assert inv.product(inv.a)["available"] == 0
    inv.cancel_order(order["id"])
    assert inv.product(inv.a)["available"] == 4
    with pytest.raises(InventoryError):
        inv.complete_order(order["id"])

    # a pre-order on the container still at sea
    sea = container(inv, qty_a=6)
    inv.set_po_status(sea["id"], "shipping")
    pre = inv.create_order(
        [{"product_id": inv.a, "qty": 4, "po_id": sea["id"]}], kind="preorder", customer_name="Anh Tuấn"
    )
    assert pre["items"][0]["status"] == "awaiting" and pre["items"][0]["eta"] == "2026-10-15"
    p = inv.product(inv.a)
    assert (p["incoming"], p["preordered"]) == (6, 4)
    with pytest.raises(InventoryError, match="sắp về"):
        inv.create_order([{"product_id": inv.a, "qty": 3}], kind="preorder")
    with pytest.raises(InventoryError, match="chờ hàng"):
        inv.complete_order(pre["id"])
    with pytest.raises(InventoryError, match="đặt trước"):
        inv.set_po_status(sea["id"], "ordered") and inv.set_po_status(sea["id"], "cancelled")

    # the container arrives: the pre-order is served first
    received = inv.receive_po(sea["id"], [{"item_id": sea["items"][0]["id"], "qty": 5, "damaged": 1}])
    assert received["served_preorders"] == [pre["id"]] and received["status"] == "partial"
    assert inv.order(pre["id"])["items"][0]["status"] == "reserved"
    p = inv.product(inv.a)
    assert (p["on_hand"], p["reserved"], p["available"]) == (9, 4, 5)
    new_lot = next(x for x in p["lots"] if x["initial_qty"] == 5)
    assert new_lot["preordered_qty"] == 4 and new_lot["remaining_pct"] == 100.0  # 1 free of 1 to sell
    inv.complete_order(pre["id"])
    assert inv.product(inv.a)["on_hand"] == 5


def test_transfers_counts_reorder_and_csv(inv):
    po = container(inv, qty_a=10, qty_b=40)
    inv.receive_po(
        po["id"], [{"item_id": po["items"][0]["id"], "qty": 10}, {"item_id": po["items"][1]["id"], "qty": 40}]
    )
    assert inv.po(po["id"])["status"] == "received"

    t = inv.create_transfer(inv.kho, inv.shop, [{"sku": "SOFA-01", "qty": 3}], actor="Thu")
    with pytest.raises(InventoryError):
        inv.receive_transfer(t["id"])
    inv.ship_transfer(t["id"])
    assert inv.product(inv.a)["on_hand"] == 7  # on the truck
    got = inv.receive_transfer(t["id"], {t["items"][0]["id"]: 2})
    assert got["status"] == "received" and got["code"].startswith("CK")
    by_wh = {x["warehouse_id"]: x["on_hand"] for x in inv.product(inv.a)["by_warehouse"]}
    assert by_wh == {inv.kho: 7, inv.shop: 2}
    assert inv.product(inv.a)["lots"][0]["remaining_qty"] == 9  # the missing one left its lot too
    # a shop sells from its own shelf
    inv.complete_order(inv.create_order([{"product_id": inv.a, "qty": 2}], warehouse_id=inv.shop)["id"])
    with pytest.raises(InventoryError):
        inv.create_order([{"product_id": inv.a, "qty": 1}], warehouse_id=inv.shop)

    # stock count
    with pytest.raises(InventoryError, match="lý do"):
        inv.adjust(inv.b, inv.kho, 38, "")
    p = inv.adjust(inv.b, inv.kho, 38, "Kiểm kho: 2 hỏng", actor="Thu")
    assert p["on_hand"] == 38 and p["lots"][0]["remaining_qty"] == 38

    # 30 sold in 30 days = 1 a day; lead time 45 days; safety 7 days
    inv.complete_order(inv.create_order([{"product_id": inv.b, "qty": 30}])["id"])
    b = inv.product(inv.b)
    assert (b["available"], b["daily_sales"], b["reorder_point"], b["level"]) == (8, 1.0, 52, "reorder")
    assert b["suggest_order"] == 45 + 30 + 7 - 8
    assert [x["sku"] for x in inv.reorder()] == ["GHE-02"]
    summary = inv.summary()
    assert summary["sold"] == 32 and summary["orders"] == 2

    csv_text = inv.export_csv()
    assert csv_text.startswith("﻿sku,name") and "SOFA-01" in csv_text
    result = inv.import_csv(
        "sku,name,category\nsofa-01,Sofa da 3 chỗ (mới),Phòng khách\nban-03,Bàn trà,\n,thiếu mã,\n"
    )
    assert (result["created"], result["updated"], len(result["errors"])) == (1, 1, 1)
    assert inv.by_sku("BAN-03")["name"] == "Bàn trà"


def test_daily_run_once_a_day_and_settings(inv):
    now = datetime.now().astimezone().replace(hour=1)
    assert inv.maybe_run_daily(now) == []
    assert inv.maybe_run_daily(now + timedelta(minutes=5)) is None  # already ran today
    inv.save_settings({"price_hour": 3})
    assert inv.maybe_run_daily(now + timedelta(days=1)) is None  # before 3 o'clock
    with pytest.raises(InventoryError):
        inv.save_settings({"stage_discounts": [0, 20, 10, 30, 40]})
    s = inv.save_settings(
        {
            "stage_discounts": [0, 5, 15, 30, 45],
            "default_margin_pct": 35,
            "rules": {
                "1": {"target_pct": 90, "min_days": 3, "max_days": 10},
                "2": {"target_pct": 60, "min_days": 7, "max_days": 30},
                "3": {"target_pct": 40, "min_days": 7, "max_days": 45},
                "4": {"target_pct": 20, "min_days": 7, "max_days": 60},
            },
        }
    )
    assert s["stage_discounts"][1] == 5 and s["rules"]["1"]["max_days"] == 10
    assert inv.price_plan(650_000)["prices"][0] == 1_000_000  # 35% margin
    inv.add_opening_stock(inv.a, inv.kho, 5, 650_000)
    with pytest.raises(InventoryError, match="thập phân"):
        inv.save_settings({"decimals": 2})
