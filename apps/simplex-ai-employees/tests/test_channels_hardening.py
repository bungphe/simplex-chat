"""Regression tests: email threads and sender checks, CRM merges, a poll loop that
survives errors, triage keywords, media fetching pinned to the checked address,
capped platform downloads, delivery trips, geocoding batches and webhook retries."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timedelta
from typing import Any

import httpx2
import pytest
from test_channels import env, settle, setup  # noqa: F401 - fixtures
from test_channels_extra import inbound_mail, ui  # noqa: F401 - fixtures

from ai_employees import media
from ai_employees.channels import ChannelError
from ai_employees.crm import CRM
from ai_employees.db import Database, DocStore
from ai_employees.desk import Desk, fold
from ai_employees.inbox import Inbox
from ai_employees.inventory import InventoryError

from fakes import ScriptedLLM, make_office, text

KEY = "/hooks/email?key=mail-key-0123456789"


# --------------------------------------------------------------------------- #
# email


async def test_folded_email_headers_do_not_break_the_reply(ui, monkeypatch):  # noqa: F811
    client, office, llm, _ = ui
    hub = office.hub
    ch = hub.channels["email"]
    outbox: list[Any] = []
    monkeypatch.setattr(type(ch), "_smtp_send", lambda self, msg: outbox.append(msg))
    llm.responses.append(text("Dạ vâng ạ."))
    headers = "Message-ID:\n <new@example.vn>\nReferences: <r1@example.vn>\n\t<r2@example.vn>\nFrom: mai@example.vn\n"
    r = await client.post(KEY, data=inbound_mail(subject="Hỏi\n  giá", headers=headers))
    assert r.status == 200
    await settle(hub)
    [sent] = outbox
    assert sent["Subject"] == "Re: Hỏi giá"
    assert sent["In-Reply-To"] == "<new@example.vn>"
    assert sent["References"] == "<r1@example.vn> <r2@example.vn> <new@example.vn>"

    # a thread stored before headers were cleaned is sent on one line too
    hub.inbox.set_channel_state("email:mai@example.vn", subject="A\n b", references="<x@e>\r\n <y@e>")
    await ch.send("mai@example.vn", "Thêm")
    assert outbox[-1]["References"] == "<x@e> <y@e>" and outbox[-1]["Subject"] == "Re: A b"

    # anything else EmailMessage refuses is a channel error, not a crash
    monkeypatch.setattr(type(ch), "_message", lambda self, to, t: (_ for _ in ()).throw(ValueError("bad")))
    with pytest.raises(ChannelError):
        await ch.send("mai@example.vn", "x")


async def test_unauthenticated_email_is_kept_for_staff_not_answered(ui, monkeypatch):  # noqa: F811
    client, office, llm, _ = ui
    hub = office.hub
    ch = hub.channels["email"]
    outbox: list[Any] = []
    monkeypatch.setattr(type(ch), "_smtp_send", lambda self, msg: outbox.append(msg))

    forged = inbound_mail(SPF="fail", dkim="{@evil.example : pass}", subject="Đổi địa chỉ giao hàng")
    r = await client.post(KEY, data=forged)
    cid = (await r.json())["conversation"]
    await settle(hub)
    conv = hub.inbox.conversation(cid)
    assert conv.mode == "human" and outbox == [] and llm.calls == []
    notes = [m for m in hub.inbox.messages(cid) if m["sender"] == "note"]
    assert notes and "giả mạo" in notes[0]["text"]
    assert hub.inbox.channel_state("email:mai@example.vn").get("subject") is None  # thread not taken
    assert hub.crm.contact_of(cid) is None  # no details taken from it

    # SPF passing for another domain than the sender's is not enough either
    assert not ch._sender_verified(
        {"SPF": "pass", "envelope": '{"from": "x@evil.example"}'}, "mai@example.vn"
    )
    # DKIM passing for the sender's domain is
    assert ch._sender_verified({"SPF": "softfail", "dkim": "{@example.vn : pass}"}, "mai@example.vn")
    assert ch._sender_verified({"SPF": "pass", "envelope": '{"from": "b@mail.example.vn"}'}, "a@example.vn")


# --------------------------------------------------------------------------- #
# CRM


def test_merge_keeps_loyalty_and_moves_everything_owned(tmp_path):
    inbox = Inbox(tmp_path / "inbox.db")
    crm = CRM(inbox.db)
    db = inbox.db
    a = crm.create_contact("Mai", "0901234567")
    b = crm.create_contact("Mai N.", "0901234567")
    db.execute(
        "UPDATE crm_contacts SET points=?, total_spent=?, orders_count=?, vip=0 WHERE id=?",
        (10, 1000, 1, a["id"]),
    )
    db.execute(
        "UPDATE crm_contacts SET points=?, total_spent=?, orders_count=?, vip=1, vip_since=? WHERE id=?",
        (25, 5000, 3, "2025-01-01T00:00:00", b["id"]),
    )
    # the loyalty and web shop modules are in use here, the stock module is not (no inv_orders)
    db.script(
        "CREATE TABLE crm_points (id {id}, contact_id {int} NOT NULL, delta {int} NOT NULL);"
        "CREATE TABLE sf_sessions (token_hash TEXT PRIMARY KEY, contact_id {int} NOT NULL)"
    )
    db.execute("INSERT INTO crm_points (contact_id, delta) VALUES (?, 25)", (b["id"],))
    db.execute("INSERT INTO sf_sessions (token_hash, contact_id) VALUES ('t', ?)", (b["id"],))

    merged = crm.merge(a["id"], b["id"])
    assert (merged["points"], merged["total_spent"], merged["orders_count"]) == (35, 6000, 4)
    assert merged["vip"] == 1 and merged["vip_since"] == "2025-01-01T00:00:00"
    assert db.row("SELECT contact_id FROM crm_points")["contact_id"] == a["id"]
    assert db.row("SELECT contact_id FROM sf_sessions")["contact_id"] == a["id"]


# --------------------------------------------------------------------------- #
# the poll loop


async def test_poll_loop_survives_a_failing_ingest(setup, monkeypatch):  # noqa: F811
    office, _, _, _ = setup
    hub = office.hub
    ch = hub.channels["zalo-shop"]
    ch.cfg = dataclasses.replace(ch.cfg, poll_seconds=0.001)
    stopping = asyncio.Event()
    calls = []

    async def poll_once(channel_id: str) -> int:
        calls.append(channel_id)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        stopping.set()
        return 0

    monkeypatch.setattr(hub, "poll_once", poll_once)
    await asyncio.wait_for(hub._poll_loop(ch, stopping), timeout=5)
    assert len(calls) == 2


# --------------------------------------------------------------------------- #
# triage rules


def test_fold_and_long_keywords(tmp_path):
    assert fold("ĐƯỜNG Đi đổi") == "duong di doi"
    desk = Desk(DocStore(Database(str(tmp_path / "d.db"))))
    rule = {"name": "r", "keywords": ["x" * 81], "handoff": True}
    with pytest.raises(ValueError):
        desk.save("rules", [rule], set(), set())
    desk.save("rules", [{**rule, "keywords": ["khiếu nại"]}], set(), set())
    assert desk.matching_rules("web", "Em muốn KHIEU NAI") and not desk.matching_rules("web", "chào")


# --------------------------------------------------------------------------- #
# media


async def test_media_is_fetched_from_the_checked_address(monkeypatch):
    async def fake_resolve(host: str) -> list[str]:
        return {"cdn.example": ["93.184.216.34"], "v6.example": ["2606:2800:220:1::1"]}[host]

    monkeypatch.setattr(media, "resolve", fake_resolve)
    seen: list[httpx2.Request] = []

    def handle(r: httpx2.Request) -> httpx2.Response:
        seen.append(r)
        if r.url.path == "/hop":
            return httpx2.Response(302, headers={"location": "https://v6.example/p.png"})
        return httpx2.Response(200, content=b"png", headers={"content-type": "image/png"})

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
    assert await media.fetch(http, "https://cdn.example:8443/hop") == ("image/png", b"png")
    first, second = seen
    assert (first.url.host, first.url.port, first.headers["host"]) == (
        "93.184.216.34",
        8443,
        "cdn.example:8443",
    )
    assert first.extensions["sni_hostname"] == "cdn.example"
    assert (second.url.host, second.headers["host"]) == ("2606:2800:220:1::1", "v6.example")

    # through a proxy the proxy resolves the name (a bare address is often refused there)
    proxied = httpx2.AsyncClient(proxy="http://127.0.0.1:9")
    url, headers, ext = await media.pinned(proxied, "https://cdn.example/p.png")
    assert url.host == "cdn.example" and "sni_hostname" not in ext and "Host" not in headers


async def test_platform_downloads_are_capped_not_cut(ui, monkeypatch):  # noqa: F811
    _client, office, _, _ = ui
    hub = office.hub
    wa, tg = hub.channels["whatsapp"], hub.channels["telegram"]
    url = "http://waha.local:3000/api/files/default/m.jpg"
    assert await wa.fetch_media(url) == ("image/jpeg", b"\xff\xd8wa")
    monkeypatch.setattr(media, "MAX_BYTES", 3)
    with pytest.raises(ChannelError, match="too large"):
        await wa.fetch_media(url)
    with pytest.raises(ChannelError, match="too large"):
        await tg.fetch_media("telegram:abc")


# --------------------------------------------------------------------------- #
# delivery


class Maps:
    def __init__(self) -> None:
        self.asked: list[str] = []
        self.denied = False
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle))

    def handle(self, r: httpx2.Request) -> httpx2.Response:
        address = r.url.params["address"]
        self.asked.append(address)
        if self.denied:
            return httpx2.Response(200, json={"status": "REQUEST_DENIED"})
        if address == "broken":
            return httpx2.Response(502, content=b"<html>bad gateway</html>")
        if address == "timeout":
            raise httpx2.ConnectTimeout("slow")
        if address == "nowhere":
            return httpx2.Response(200, json={"status": "ZERO_RESULTS", "results": []})
        return httpx2.Response(
            200, json={"status": "OK", "results": [{"geometry": {"location": {"lat": 10.8, "lng": 106.7}}}]}
        )


@pytest.fixture
def shop(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "maps-key")
    maps = Maps()
    office = make_office(tmp_path, ScriptedLLM(), http=maps.client)
    inv = office.inventory
    wh = inv.save_warehouse(None, {"code": "KHO", "name": "Kho"})["id"]
    pid = inv.save_product(None, {"sku": "SOFA", "name": "Sofa", "cbm": "1"})["id"]
    inv.add_opening_stock(pid, wh, 10, 1_000_000, margin_pct=40)
    return office, maps, wh, pid


async def test_geocoding_batch_errors(shop):
    office, maps, _wh, _pid = shop
    crm = office.hub.crm
    ids = {}
    for name in ("broken", "timeout", "nowhere", "ok"):
        ids[name] = crm.create_contact(name, "")["id"]
        crm.update(ids[name], address=name)
    # one bad answer does not stop the others
    assert await office.delivery.geocode_customers() == {"located": 1, "failed": 3}
    assert crm.contact(ids["ok"])["lat"] == 10.8
    # an address Google does not know is not asked again, until it changes
    maps.asked.clear()
    assert await office.delivery.geocode_customers() == {"located": 0, "failed": 2}
    assert "nowhere" not in maps.asked
    crm.update(ids["nowhere"], address="ok")
    assert (await office.delivery.geocode_customers())["located"] == 1
    # a refused key stops the batch at the first address
    maps.asked.clear()
    maps.denied = True
    assert await office.delivery.geocode_customers() == {
        "located": 0,
        "failed": 0,
        "stopped": "REQUEST_DENIED",
    }
    assert len(maps.asked) == 1


async def test_trips_follow_their_bookings(shop):
    office, _maps, wh, pid = shop
    d, inv = office.delivery, office.inventory
    day = (datetime.now().astimezone() + timedelta(days=1)).date().isoformat()
    carrier = d.save_carrier(
        None, {"code": "X", "name": "Xe", "rate_per_trip": 300_000, "rate_per_stop": 50_000}
    )
    bookings = []
    for n in range(2):
        order = inv.create_order([{"product_id": pid, "qty": 1}], customer_name=f"K{n}", address=f"Số {n}")
        bookings.append(
            d.book({"order_id": order["id"], "delivery_date": day, "lat": "10.7", "lng": "106.6"})
        )
    trip = {"delivery_date": day, "carrier_id": carrier["id"], "origin_wh": wh}
    for bad in ("25:00", "8h", "noon"):
        with pytest.raises(InventoryError):
            d.create_route({**trip, "booking_ids": [bookings[0]["id"]], "start_time": bad})
    route = d.create_route({**trip, "booking_ids": [b["id"] for b in bookings], "start_time": "7:30"})
    assert route["start_time"] == "07:30" and route["cost"] == 300_000 + 2 * 50_000

    # a new address: the old coordinates are another place
    moved = d.update_booking(bookings[1]["id"], {"address": "Nhà mới"})
    assert (moved["lat"], moved["lng"]) == ("", "")
    kept = d.update_booking(bookings[0]["id"], {"address": "Số 0", "notes": "gọi trước"})
    assert kept["lat"] == "10.7"

    d.cancel_booking(bookings[0]["id"])
    route = d.route(route["id"])
    assert route["cost"] == 300_000 + 50_000 and len(route["stops"]) == 1
    d.cancel_booking(bookings[1]["id"])
    route = d.route(route["id"])
    assert route["status"] == "cancelled" and route["cost"] == 0 and route["stops"] == []


# --------------------------------------------------------------------------- #
# webhook bridges without message ids


async def test_webhook_retry_without_message_id_is_stored_once(setup):  # noqa: F811
    office, llm, _, _ = setup
    hub = office.hub
    llm.responses += [text("a"), text("b")]
    payload = {"conversation_id": "r1", "customer_name": "Linh", "text": "Còn hàng không?"}
    conv = hub.push_inbound("website", payload)
    hub.push_inbound("website", payload)  # the bridge retried
    assert [m["text"] for m in hub.inbox.messages(conv.id)] == ["Còn hàng không?"]
    hub.push_inbound("website", {**payload, "timestamp": 1})
    hub.push_inbound("website", {**payload, "timestamp": 2})  # the same words, sent again later
    assert len([m for m in hub.inbox.messages(conv.id) if m["sender"] == "customer"]) == 3
    await settle(hub)
