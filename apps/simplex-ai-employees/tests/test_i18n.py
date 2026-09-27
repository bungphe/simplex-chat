"""Interface languages: tr() and the current language, the catalogs, and the language of
the admin UI, staff chats, the web shop and receipts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_channels import PASSWORD, WEBHOOK, H, Platforms

from ai_employees import i18n
from ai_employees.i18n import tr, use_language
from ai_employees.storefront import create_shop_app
from ai_employees.users import Users
from ai_employees.web import create_app

from fakes import ScriptedLLM, fake_chat, make_office

LOCALES = Path(i18n.__file__).parent / "locales"
FAKE = {
    "en": {
        "Còn {0} sản phẩm": "{0} products left",
        "Mật khẩu hiện tại": "Current password",
        "Không có sản phẩm này": "No such product",
        "Vai trò của bạn không dùng được lệnh này.": "Your role cannot use this command.",
        "Sản phẩm": "Products",
        "Tổng": "Total",
    },
    "de": {"Còn {0} sản phẩm": "Noch {1} Produkte"},  # a broken translation
}


@pytest.fixture
def catalogs(monkeypatch):
    monkeypatch.setattr(i18n, "catalog", lambda code: FAKE.get(code, {}))


def test_tr_follows_the_current_language(catalogs):
    assert tr("Còn {0} sản phẩm", 3) == "Còn 3 sản phẩm"
    with use_language("en-US"):
        assert tr("Còn {0} sản phẩm", 3) == "3 products left"
        assert tr("Chưa dịch") == "Chưa dịch"  # not in the catalog: the source
        with use_language("de"):
            assert tr("Còn {0} sản phẩm", 3) == "Còn 3 sản phẩm"  # broken: the source
        assert i18n.current() == "en"
    with use_language("xx"):
        assert i18n.current() == i18n.default()
    assert i18n.best_match("fr-CH, en;q=0.9, *;q=0.5") == "fr"
    assert i18n.best_match("xx, ja;q=0.3, ko;q=0.8") == "ko"
    assert (
        i18n.best_match("") is None and i18n.normalize("zh-Hans") == "zh" and i18n.normalize("pt_BR") == "pt"
    )


def test_catalogs_match_the_code_and_keep_placeholders():
    sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
    import i18n_batch

    source = json.loads((LOCALES / "source.json").read_text(encoding="utf-8"))
    every = set(source["ui"]) | set(source["server"])
    assert len(every) > 1000
    for code in i18n.LANGUAGES:
        if code == i18n.SOURCE:
            continue
        data = json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8"))
        assert set(data) == every, f"{code}: run scripts/i18n_extract.py"
    assert i18n_batch.check([]) == 0


@pytest.fixture
async def office(tmp_path, monkeypatch, catalogs):
    monkeypatch.setenv("T_HOOK_SECRET", "hook-secret-1")
    office = make_office(
        tmp_path,
        ScriptedLLM(),
        http=Platforms().client,
        channels=[WEBHOOK],
        storefront={"public_url": "http://shop.test"},
    )
    fake_chat(office.employees["sales"])
    yield office


async def test_the_admin_ui_and_its_errors_in_the_staff_members_language(office):
    client = TestClient(TestServer(create_app(office, PASSWORD)))
    await client.start_server()
    try:
        Users(office.docs, "").add("anna", "Anna", "manager", "0123456789")
        await client.post("/api/login", json={"username": "anna", "password": "0123456789"}, headers=H)
        r = await client.get("/api/inventory/products/999", headers=H)
        assert (await r.json())["error"] == "Không có sản phẩm này"
        r = await client.put("/api/me/lang", json={"lang": "en"}, headers=H)
        assert r.status == 200 and r.cookies["ui_lang"].value == "en"
        assert (await client.put("/api/me/lang", json={"lang": "klingon"}, headers=H)).status == 400
        assert (await (await client.get("/api/me", headers=H)).json())["user"]["lang"] == "en"
        r = await client.get("/api/inventory/products/999", headers=H)
        assert (await r.json())["error"] == "No such product"
        # the catalog the page loads before its scripts: only the admin UI's texts
        script = await (await client.get("/i18n/catalog.js", headers=H)).text()
        data = json.loads(script.removeprefix("const I18N = ").rstrip().rstrip(";"))
        assert data["lang"] == "en" and data["msgs"]["Mật khẩu hiện tại"] == "Current password"
        assert "Không có sản phẩm này" not in data["msgs"]  # a server text
        assert data["languages"]["ar"] == "العربية"
        # a browser asking for Arabic before anyone logs in: right to left
        fresh = TestClient(TestServer(create_app(office, PASSWORD)))
        await fresh.start_server()
        try:
            script = await (
                await fresh.get("/i18n/catalog.js", headers={"Accept-Language": "ar-EG,ar;q=0.9"})
            ).text()
            assert '"lang": "ar"' in script and '"rtl": true' in script
        finally:
            await fresh.close()
    finally:
        await client.close()


async def test_staff_chat_and_the_web_shop_in_their_languages(office):
    users = Users(office.docs, "")
    users.add("ben", "Ben", "warehouse", "0123456789")
    users.set_language("ben", "en")
    staff = office.employees["sales"].staff
    await staff.handle(40, "link", office.staff_links.new_code("ben"))
    assert await staff.handle(40, "sell", "SOFA 1") == "Your role cannot use this command."
    users.set_language("ben", "")
    assert await staff.handle(40, "sell", "SOFA 1") == "Vai trò của bạn không dùng được lệnh này."

    shop = TestClient(TestServer(create_shop_app(office)))
    await shop.start_server()
    try:
        page = await (await shop.get("/", headers={"Accept-Language": "en-GB,en"})).text()
        assert '<html lang="en"' in page and ">Products<" in page
        r = await shop.get("/?lang=vi", headers={"Accept-Language": "en"})
        assert '<html lang="vi"' in await r.text() and r.cookies["sf_lang"].value == "vi"
        page = await (await shop.get("/", headers={"Accept-Language": "en"})).text()
        assert '<html lang="vi"' in page  # the visitor's choice wins over the browser's
        assert 'href="?lang=ar"' in page
    finally:
        await shop.close()


async def test_receipts_in_the_shops_language(office):
    from ai_employees.invoices import receipt_html

    inv = office.inventory
    kho = inv.save_warehouse(None, {"code": "KHO", "name": "Kho"})["id"]
    pid = inv.save_product(None, {"sku": "SOFA", "name": "Sofa"})["id"]
    inv.add_opening_stock(pid, kho, 2, 1_000_000, margin_pct=50)
    order = inv.create_order([{"sku": "SOFA", "qty": 1}])
    with use_language("en"):  # a cashier using the admin UI in English...
        page = receipt_html(office, order["id"])
    assert '<html lang="vi"' in page and "Tổng" in page  # ...prints the shop's receipt
    page = receipt_html(office, order["id"], lang="en")
    assert '<html lang="en"' in page and "Total" in page
