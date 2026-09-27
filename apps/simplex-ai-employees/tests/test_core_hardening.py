"""Hardening of the core: approvals decided once, colleague consultations without side
effects, prices and phone numbers in translation, PostgreSQL reconnects, the cluster
outbox and configuration mistakes."""

from __future__ import annotations

import os

import httpx2
import pytest
from test_operations import ORDER, Shop, order_office

from ai_employees import i18n, lang
from ai_employees.agent import TranslationError
from ai_employees.config import ConfigError, parse_config
from ai_employees.routines import parse_routine
from ai_employees.state import EmployeeState

from fakes import ScriptedLLM, fake_chat, make_office, text, tool, tool_names

PG = os.environ.get("AIE_TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(not PG, reason="needs PostgreSQL")


# --------------------------------------------------------------------------- #
# Approvals: a request is sent at most once, and approve/reject cannot both win


async def queued(tmp_path, shop):
    llm = ScriptedLLM(tool("create_order", {"customer": "An", "items": "1"}), text("ok"))
    office, sales, chat = order_office(tmp_path, llm, shop)
    sales.state.add_admin(99)
    await sales.agent.respond(1, "An", "Đặt 1 máy")
    assert sales.state.action(1)["status"] == "pending"
    return office, sales, chat


def stale_once(state, rec):
    """state.action returns `rec` (read before another process decided) the first time."""
    real, calls = state.action, []

    def action(n):
        calls.append(n)
        return dict(rec) if len(calls) == 1 else real(n)

    state.action = action


def test_only_one_claim_wins(tmp_path):
    a = EmployeeState(tmp_path / "e.json")
    b = EmployeeState(tmp_path / "e.json")  # another process on the same database
    a.add_action(action="x", args={}, status="pending")
    assert a.claim_action(1, "pending", status="executing", decided_by="An")["status"] == "executing"
    assert b.claim_action(1, "pending", status="rejected") is None
    assert b.action(1)["status"] == "executing" and b.action(1)["decided_by"] == "An"
    assert a.claim_action(7, "pending", status="executing") is None


async def test_an_approval_that_lost_the_race_sends_nothing(tmp_path):
    shop = Shop()
    _, sales, _ = await queued(tmp_path, shop)
    pending = sales.state.action(1)
    # another process approved it after this one read it as pending
    sales.state.claim_action(1, "pending", status="executing", decided_by="Bình")
    stale_once(sales.state, pending)
    assert "không thể duyệt" in await sales.actions.approve(1, by="An")
    assert shop.requests == []
    assert sales.state.action(1)["decided_by"] == "Bình"


async def test_a_reject_cannot_overwrite_an_approval(tmp_path):
    shop = Shop()
    _, sales, chat = await queued(tmp_path, shop)
    pending = sales.state.action(1)
    sales.state.claim_action(1, "pending", status="executing", decided_by="Bình")
    stale_once(sales.state, pending)
    before = len(chat.sent)
    assert "không thể từ chối" in await sales.actions.reject(1, by="An", reason="hết hàng")
    assert sales.state.action(1)["status"] == "executing"
    assert len(chat.sent) == before  # the customer is not told it was rejected


@pytest.mark.parametrize(
    "error", [httpx2.InvalidURL("Invalid URL 'x'"), RuntimeError("boom")], ids=["invalid-url", "crash"]
)
async def test_any_send_error_marks_the_request_failed(tmp_path, error):
    shop = Shop()
    _, sales, _ = await queued(tmp_path, shop)

    async def broken(*args, **kwargs):
        raise error

    sales.office.http_client.request = broken
    answer = await sales.actions.approve(1, by="An")
    assert answer.startswith(f"#1 thất bại: {type(error).__name__}")
    rec = sales.state.action(1)
    assert rec["status"] == "failed" and str(error) in rec["result"] and rec["finished"]


# --------------------------------------------------------------------------- #
# A colleague's question never places orders or hands the customer over


async def test_consultations_offer_no_actions_or_handoff(tmp_path):
    llm = ScriptedLLM(text("a"), text("b"))
    office = make_office(
        tmp_path,
        llm,
        http=Shop().client,
        actions=ORDER,
        skills=["create_order", "handoff_to_human", "current_time"],
    )
    sales = office.employees["sales"]
    await sales.agent.respond(1, "An", "Chào shop")
    await sales.agent.consult("Còn hàng không?", asker="Minh")
    assert {"create_order", "handoff_to_human"} <= tool_names(llm.calls[0])
    assert tool_names(llm.calls[1]) == {"current_time"}


# --------------------------------------------------------------------------- #
# Prices in translation


@pytest.mark.parametrize(
    "message",
    [
        "Gọi 090.123.4567 nhé",
        "Gọi 0901.234.567 nhé",
        "Giao ngày 12.05.2026",
        "Giao ngày 1.05.2026",
        "Mã 0901234567",
        "1,2 triệu",  # not a grouped number: left alone
        "1.234.567,89",
    ],
)
def test_phones_and_dates_are_not_prices(message):
    assert lang.protect(message, "en") == (message, [])


def test_prices_are_still_protected():
    assert lang.protect("Giá 4.500.000đ", "en") == ("Giá ⟦P0⟧", ["4,500,000 VND"])
    assert lang.protect("Giá 4.500.000 đ.", "en") == ("Giá ⟦P0⟧.", ["4,500,000 VND"])
    assert lang.protect("Ship 30.000, tổng 500000 vnd", "en") == (
        "Ship ⟦P0⟧, tổng ⟦P1⟧",
        ["30,000", "500,000 VND"],
    )
    assert lang.protect("Giá 4.500.000đ", "vi")[1] == ["4.500.000đ"]


async def test_a_retry_that_changes_a_price_is_refused(tmp_path):
    llm = ScriptedLLM(
        text("MA-100は円です"),  # the placeholder was dropped
        text("MA-100は450万ドンです"),  # the plain retry rewrote the price
    )
    sales = make_office(tmp_path, llm).employees["sales"]
    with pytest.raises(TranslationError, match="lost 4,500,000 VND"):
        await sales.agent.translate("MA-100 giá 4.500.000đ", "ja")
    # a notice for a customer then goes out untranslated rather than with a wrong price
    llm.responses[:] = [text("MA-100は円です"), text("MA-100は450万ドンです")]
    sales.state.set_language(1, "ja")
    assert await sales.agent.for_contact(1, "MA-100 giá 4.500.000đ") == "MA-100 giá 4.500.000đ"


async def test_a_retry_may_group_the_price_its_own_way(tmp_path):
    llm = ScriptedLLM(text("MA-100は円です"), text("MA-100は4.500.000ドンです"))
    sales = make_office(tmp_path, llm).employees["sales"]
    assert await sales.agent.translate("MA-100 giá 4.500.000đ", "ja") == "MA-100は4.500.000ドンです"


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("How much is the sofa 4.500.000đ please", "en"),
        ("How much is the sofa 500k please", "en"),
        ("Is 4.500.000 ₫ the price for this", "en"),
        ("Đà Nẵng có giao không", "vi"),
    ],
)
def test_prices_do_not_make_a_message_vietnamese(message, code):
    assert lang.detect(message) == code


# --------------------------------------------------------------------------- #
# PostgreSQL: a restarted server (or a dropped connection) is reconnected


def _kill(db):
    import psycopg

    pid = db.row("SELECT pg_backend_pid() AS pid")["pid"]
    with psycopg.connect(PG, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))


@needs_pg
def test_postgres_reconnects_outside_transactions(tmp_path):
    from ai_employees.db import Database

    from fakes import postgres_schema

    db = Database(postgres_schema(PG, tmp_path))
    db.script("CREATE TABLE IF NOT EXISTS t (id {id}, v TEXT)")
    db.execute("INSERT INTO t (v) VALUES (?)", ("a",))
    _kill(db)
    assert db.rows("SELECT v FROM t") == [{"v": "a"}]  # reconnected, same schema
    _kill(db)
    db.many("INSERT INTO t (v) VALUES (?)", [("b",), ("c",)])
    _kill(db)
    assert db.execute("INSERT INTO t (v) VALUES (?) RETURNING id", ("d",)) == 4
    db._conn.close()
    assert len(db.rows("SELECT v FROM t")) == 4

    # inside a transaction the error is raised (its work is gone), the next statement reconnects
    import psycopg

    with pytest.raises(psycopg.OperationalError), db.transaction():
        db.execute("INSERT INTO t (v) VALUES (?)", ("e",))
        _kill(db)
        db.execute("INSERT INTO t (v) VALUES (?)", ("f",))
    assert [r["v"] for r in db.rows("SELECT v FROM t ORDER BY id")] == ["a", "b", "c", "d"]
    with db.transaction():
        db.execute("INSERT INTO t (v) VALUES (?)", ("g",))
    assert db.row("SELECT COUNT(*) AS n FROM t")["n"] == 5
    db.close()


# --------------------------------------------------------------------------- #
# Cluster outbox: failed messages are retried a few times, not lost


@needs_pg
async def test_outbox_retries_failed_messages(tmp_path, monkeypatch):
    from ai_employees.cluster import OUTBOX_TRIES

    monkeypatch.setenv("AIE_SHARD", "1")
    office1 = make_office(tmp_path, ScriptedLLM(), cluster={"shards": 2})
    monkeypatch.setenv("AIE_SHARD", "0")
    office0 = make_office(tmp_path, ScriptedLLM(), cluster={"shards": 2})
    chat = fake_chat(office0.employees["sales"])
    real_send, failures = chat.api_send_text_message, [1]

    async def flaky(chat_ref, text):
        if failures[0]:
            failures[0] -= 1
            raise ConnectionError("chat core restarting")
        return await real_send(chat_ref, text)

    chat.api_send_text_message = flaky
    db = office0.office_db
    await office1.cluster.simplex_send(office1.employees["sales"], 42, "Đơn #7 đã xác nhận")
    await office0.cluster._drain_outbox()
    assert chat.sent == []
    assert db.row("SELECT sent, attempts FROM simplex_outbox") == {"sent": 2, "attempts": 1}
    await office0.cluster._drain_outbox()
    assert chat.sent == [(42, "Đơn #7 đã xác nhận")]
    await office0.cluster._drain_outbox()
    assert len(chat.sent) == 1  # never twice
    assert db.row("SELECT sent, attempts FROM simplex_outbox") == {"sent": 1, "attempts": 2}

    failures[0] = 99  # always failing: given up after a few tries
    await office1.cluster.simplex_send(office1.employees["sales"], 43, "Hello")
    for _ in range(OUTBOX_TRIES + 2):
        await office0.cluster._drain_outbox()
    assert db.row("SELECT sent, attempts FROM simplex_outbox WHERE contact=43") == {
        "sent": 2,
        "attempts": OUTBOX_TRIES,
    }


# --------------------------------------------------------------------------- #
# Configuration mistakes are reported clearly

BASE = {"employees": [{"id": "a", "display_name": "A", "system_prompt": "p"}]}


def test_staff_language_is_normalized_and_checked(tmp_path):
    assert parse_config({**BASE, "staff_language": "en-US"}, tmp_path).staff_language == "en"
    assert parse_config(BASE, tmp_path).staff_language == "vi"
    with pytest.raises(ConfigError, match="staff_language 'klingon' is not supported"):
        parse_config({**BASE, "staff_language": "klingon"}, tmp_path)


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        (
            {"employees": [{**BASE["employees"][0], "max_steps": "eight"}]},
            "employee a: max_steps must be a number",
        ),
        ({"employees": [{**BASE["employees"][0], "max_tokens": None}]}, "max_tokens must be a number"),
        ({**BASE, "admin_ui": {"port": "http"}}, "admin_ui.port must be a number"),
        ({**BASE, "storefront": {"port": "x"}}, "storefront.port must be a number"),
        ({**BASE, "cluster": {"shards": "two"}}, "cluster.shards must be a number"),
        ({**BASE, "catch_up_hours": "a day"}, "catch_up_hours must be a number"),
        (
            {**BASE, "models": {"m": {"provider": "openai", "model": "x", "timeout": "slow"}}},
            "model m: timeout",
        ),
        (
            {
                **BASE,
                "actions": {
                    "x": {"url": "https://x", "description": "d", "fields": {"a": "b"}, "timeout": "1m"}
                },
            },
            "action x: timeout must be a number",
        ),
    ],
)
def test_non_numeric_settings_are_named(tmp_path, raw, match):
    with pytest.raises(ConfigError, match=match):
        parse_config(raw, tmp_path)


def test_routine_times_written_without_quotes(tmp_path):
    import yaml

    raw = yaml.safe_load("id: brief\ntask: t\nat: 10:30\n")
    assert raw["at"] == 630  # YAML 1.1 sexagesimal
    assert str(parse_routine(raw).at) == "10:30:00"
    assert str(parse_routine({**raw, "at": "08:00"}).at) == "08:00:00"
    with pytest.raises(ValueError, match="'at' must be HH:MM"):
        parse_routine({**raw, "at": 5000})
    with pytest.raises(ValueError, match="window_minutes must be a number"):
        parse_routine({**raw, "window_minutes": "an hour"})


def test_accept_language_with_spaces():
    assert i18n.best_match("en; q=0.5, fr;q=0.8") == "fr"
    assert i18n.best_match("fr ; q = 0, en;Q=0.3") == "en"
    assert i18n.best_match("de-DE,de;q=0.9") == "de"
