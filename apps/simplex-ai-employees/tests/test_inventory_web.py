"""The inventory in the admin UI and for the AI employee: prices and stock in answers,
orders placed by the AI (after approval) reserving real stock, VIP prices."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_channels import PASSWORD, WEBHOOK, H, Platforms, settle

from ai_employees.web import create_app

from fakes import ScriptedLLM, make_office, text, tool, tool_results

HOOK = {"X-Hook-Secret": "hook-secret-1"}


@pytest.fixture
async def shop(tmp_path, monkeypatch):
    monkeypatch.setenv("T_HOOK_SECRET", "hook-secret-1")
    llm, platforms = ScriptedLLM(), Platforms()
    office = make_office(
        tmp_path,
        llm,
        http=platforms.client,
        channels=[WEBHOOK],
        actions={"create_order": {"kind": "stock_order", "description": "Chốt đơn và giữ hàng"}},
        skills=["products", "create_order", "current_time"],
    )
    client = TestClient(TestServer(create_app(office, PASSWORD)))
    await client.start_server()
    yield client, office, llm, platforms
    await client.close()


async def call(client, method, path, body=None, status=200):
    r = await client.request(method, path, json=body, headers=H)
    data = await r.json()
    assert r.status == status, data
    return data


async def test_inventory_api_and_the_ai_selling_from_stock(shop):
    client, office, llm, _platforms = shop
    hub = office.hub
    await call(client, "POST", "/api/login", {"password": PASSWORD})

    kho = (await call(client, "POST", "/api/inventory/warehouses", {"code": "KHO", "name": "Kho tổng"}))["id"]
    await call(client, "POST", "/api/inventory/warehouses", {"code": "KHO", "name": "Trùng"}, status=400)
    ncc = (await call(client, "POST", "/api/inventory/suppliers", {"name": "Foshan", "lead_time_days": 45}))[
        "id"
    ]
    sofa = await call(
        client, "POST", "/api/inventory/products", {"sku": "sofa-01", "name": "Sofa da 3 chỗ", "cbm": "1.2"}
    )
    assert sofa["sku"] == "SOFA-01" and sofa["level"] == "empty" and sofa["price"] is None

    # preview, then save a container
    preview = await call(
        client,
        "POST",
        "/api/inventory/calc",
        {
            "exchange_rate": "25000",
            "freight": 12_000_000,
            "customs": 2_400_000,
            "items": [{"qty": 10, "unit_cost_foreign": "100", "unit_cbm": "1.2", "margin_pct": 40}],
        },
    )
    [item] = preview["items"]
    assert item["landed_cost"] == 2_500_000 + 1_200_000 + 240_000 and item["prices"][0] == 6_567_000
    po = await call(
        client,
        "POST",
        "/api/inventory/purchase-orders",
        {
            "supplier_id": ncc,
            "warehouse_id": kho,
            "exchange_rate": "25000",
            "freight": 12_000_000,
            "customs": 2_400_000,
            "eta": "2026-11-01",
            "items": [{"sku": "SOFA-01", "qty": 10, "unit_cost_foreign": "100", "margin_pct": 40}],
        },
    )
    assert po["po_number"] == "PN00001" and po["items"][0]["prices"] == item["prices"]
    await call(
        client,
        "POST",
        f"/api/inventory/purchase-orders/{po['id']}/status",
        {"status": "received"},
        status=400,
    )
    await call(client, "POST", f"/api/inventory/purchase-orders/{po['id']}/status", {"status": "ordered"})
    got = await call(
        client,
        "POST",
        f"/api/inventory/purchase-orders/{po['id']}/receive",
        {"items": [{"item_id": po["items"][0]["id"], "qty": 10}]},
    )
    assert got["status"] == "received"

    # the customer asks; the AI looks the product up (price and stock, never the cost)
    llm.responses += [
        tool("products", {"query": "sofa"}),
        text("Dạ sofa da 3 chỗ giá 6.567.000đ, còn 10 cái ạ."),
    ]
    r = await client.post(
        "/hooks/website",
        json={
            "conversation_id": "v1",
            "customer_name": "Chị Mai",
            "text": "Sofa da giá bao nhiêu?",
            "message_id": "m1",
        },
        headers=HOOK,
    )
    cid = (await r.json())["conversation"]
    await settle(hub)
    [result] = tool_results(llm.calls[1])
    assert (
        "SOFA-01" in result["content"]
        and "6,567,000 VND" in result["content"]
        and "10 units in stock" in result["content"]
    )
    assert "3,940,000" not in result["content"]  # the landed cost stays inside

    # the AI places the order: held for approval, then the stock is reserved
    llm.responses += [
        tool(
            "create_order",
            {
                "items": "SOFA-01 x 2",
                "customer_name": "Nguyễn Thị Mai",
                "phone": "0901234567",
                "address": "12 Lê Lợi, Q1",
                "note": "-",
            },
        ),
        text("Dạ em đã ghi nhận đơn, quản lý xác nhận ngay ạ."),
    ]
    await client.post(
        "/hooks/website",
        json={"conversation_id": "v1", "text": "Chị lấy 2 cái, sđt 0901234567", "message_id": "m2"},
        headers=HOOK,
    )
    await settle(hub)
    pending = (await call(client, "GET", "/api/approvals"))["pending"]
    assert [p["action"] for p in pending] == ["create_order"]
    assert (await call(client, "GET", "/api/inventory/orders"))["orders"] == []
    await call(client, "POST", f"/api/approvals/sales/{pending[0]['id']}/approve")
    [order] = (await call(client, "GET", "/api/inventory/orders"))["orders"]
    assert (order["status"], order["total"], order["conversation_id"]) == ("confirmed", 2 * 6_567_000, cid)
    assert order["source"].startswith("ai:sales") and order["customer_name"] == "Nguyễn Thị Mai"
    assert (await call(client, "GET", f"/api/inventory/products/{sofa['id']}"))["available"] == 8
    detail = await call(client, "POST", f"/api/inventory/orders/{order['id']}/complete")
    assert detail["status"] == "completed" and detail["profit"] == 2 * (6_567_000 - 3_940_000)

    # a VIP customer: the next stage's price, for the AI and for staff in the inbox
    await call(client, "POST", f"/api/inbox/{cid}/contact", {"vip": True})
    llm.responses += [tool("products", {"query": "SOFA-01"}), text("Dạ giá VIP ạ.")]
    await client.post(
        "/hooks/website",
        json={"conversation_id": "v1", "text": "Còn giá nào tốt hơn không?", "message_id": "m3"},
        headers=HOOK,
    )
    await settle(hub)
    vip_price = item["prices"][1]
    assert f"{vip_price:,} VND (VIP price" in tool_results(llm.calls[-1])[0]["content"]
    picked = await call(client, "GET", f"/api/inbox/{cid}/products?q=sofa")
    assert picked["vip"] is True and picked["products"][0]["price"] == vip_price
    assert "landed_cost" not in picked["products"][0]

    # the price run from the UI, settings, the reorder report, CSV
    run = await call(client, "POST", "/api/inventory/pricing/run", {})
    assert run["changes"] == []  # not due yet
    await call(
        client, "POST", f"/api/inventory/products/{sofa['id']}/stage", {"stage": 3, "reason": "Xả hàng"}
    )
    log = (await call(client, "GET", "/api/inventory/pricing/log"))["log"]
    assert log[0]["trigger"] == "manual" and log[0]["actor"] == "Chủ"
    await call(client, "PUT", "/api/inventory/settings", {"stage_discounts": [0, 50, 10, 20, 30]}, status=400)
    assert (await call(client, "GET", "/api/inventory/reorder"))["products"] == []
    r = await client.get("/api/inventory/products.csv")
    assert r.status == 200 and "SOFA-01" in await r.text()
    r = await client.post(
        "/api/inventory/products/import",
        data="sku,name\nghe-02,Ghế ăn\n".encode(),
        headers={**H, "Content-Type": "text/csv"},
    )
    assert (await r.json())["created"] == 1

    # sales staff: only the inbox lookup, never the inventory pages
    await call(
        client,
        "POST",
        "/api/users",
        {"username": "thu", "name": "Thu", "role": "agent", "password": "0123456789"},
    )
    await call(client, "POST", "/api/logout", {})
    await call(client, "POST", "/api/login", {"username": "thu", "password": "0123456789"})
    assert (await client.get("/api/inventory")).status == 403
    assert (await client.post("/api/inventory/orders", json={}, headers=H)).status == 403
    assert (await call(client, "GET", f"/api/inbox/{cid}/products?q=ghe"))["products"][0]["sku"] == "GHE-02"
    await call(client, "POST", f"/api/inbox/{cid}/contact", {"vip": False})  # ignored: not an admin
    assert (await call(client, "GET", f"/api/inbox/{cid}/contact"))["contact"]["vip"] is True


async def test_a_stock_order_the_shop_cannot_fill_fails_cleanly(shop):
    client, office, llm, _platforms = shop
    await call(client, "POST", "/api/login", {"password": PASSWORD})
    await call(client, "POST", "/api/inventory/warehouses", {"code": "KHO", "name": "Kho tổng"})
    llm.responses += [
        tool(
            "create_order",
            {"items": "KHONG-CO x 1", "customer_name": "A", "phone": "1", "address": "-", "note": "-"},
        ),
        text("Dạ em ghi nhận."),
    ]
    office.hub.push_inbound("website", {"conversation_id": "v2", "text": "Đặt hàng", "message_id": "x"})
    await settle(office.hub)
    [pending] = (await call(client, "GET", "/api/approvals"))["pending"]
    r = await call(client, "POST", f"/api/approvals/sales/{pending['id']}/approve")
    assert "Không có SKU KHONG-CO" in json_dump(r)
    assert (await call(client, "GET", "/api/inventory/orders"))["orders"] == []


def json_dump(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)
