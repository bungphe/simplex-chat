"""Run log, scheduled routines, the approval queue, corrections and the office report."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, time

import httpx2
import pytest

from ai_employees import skills as sk
from ai_employees.config import ConfigError, parse_config
from ai_employees.routines import parse_days, parse_routine

from fakes import ScriptedLLM, fake_chat, make_office, text, tool, tool_results

VN = "Asia/Ho_Chi_Minh"  # UTC+7


def at_vn(day: int, hh: int, mm: int) -> datetime:
    """A UTC instant for 2026-09-<day> hh:mm in Vietnam (2026-09-28 is a Monday)."""
    return (
        datetime(2026, 9, day, hh - 7, mm, tzinfo=UTC)
        if hh >= 7
        else datetime(2026, 9, day - 1, hh + 17, mm, tzinfo=UTC)
    )


BRIEF = {
    "id": "daily-brief",
    "days": "mon-fri",
    "at": "08:00",
    "window_minutes": 60,
    "task": "Summarise yesterday's conversations.",
}


# --------------------------------------------------------------------------- #
# Run log


async def test_replies_are_logged_with_tokens_and_tools(tmp_path):
    llm = ScriptedLLM(
        tool("current_time", {"timezone": VN}),
        text("8 giờ", tokens=(120, 30)),
        text("", stop="refusal"),
    )
    office = make_office(tmp_path, llm)
    sales = office.employees["sales"]
    await sales.agent.respond(1, "An", "Mấy giờ rồi?")
    await sales.agent.respond(1, "An", "xyz")

    first, second = office.runlog.tail()
    assert first["employee"] == "sales" and first["kind"] == "reply" and first["status"] == "ok"
    assert first["tools"] == ["current_time"] and first["tokens_in"] == 120 and first["tokens_out"] == 30
    assert first["model"] == "claude-opus-5" and first["contact"] == 1
    assert second["status"] == "refused"
    summary = office.runlog.summary()["sales"]
    assert summary["total"] == 2 and summary["by_status"] == {"ok": 1, "refused": 1}
    assert sales.state.contacts == {1: "An"}


# --------------------------------------------------------------------------- #
# Routines


def test_routine_parsing_and_windows():
    assert parse_days("mon-fri") == frozenset(range(5))
    assert parse_days("sat-mon") == frozenset({5, 6, 0})
    assert parse_days("mon,wed,fri") == frozenset({0, 2, 4})
    assert parse_days("daily") == frozenset(range(7))
    r = parse_routine({**BRIEF, "period": "week"})
    assert r.at == time(8, 0) and r.days == frozenset(range(5))
    mon_8 = datetime(2026, 9, 28, 8, 30, tzinfo=UTC)
    assert (
        r.in_window(mon_8)
        and not r.in_window(mon_8.replace(hour=7))
        and not r.in_window(mon_8.replace(hour=9))
    )
    assert not r.in_window(datetime(2026, 9, 27, 8, 30, tzinfo=UTC))  # Sunday
    assert r.period_key(mon_8) == "2026-W40"
    assert r.next_start(datetime(2026, 9, 26, 12, 0, tzinfo=UTC)) == datetime(
        2026, 9, 28, 8, 0, tzinfo=UTC
    )  # Sat -> Mon
    assert r.next_start(mon_8, last_period="2026-W40") == datetime(2026, 10, 5, 8, 0, tzinfo=UTC)  # next week
    for bad, match in [
        ({**BRIEF, "id": "Bad Id"}, "routine id"),
        ({**BRIEF, "at": "8am"}, "HH:MM"),
        ({**BRIEF, "days": "funday"}, "unknown day"),
        ({**BRIEF, "period": "year"}, "period"),
        ({**BRIEF, "task": ""}, "task"),
    ]:
        with pytest.raises(ValueError, match=match):
            parse_routine(bad)


async def test_scheduler_runs_each_routine_once_per_period_and_reports(tmp_path):
    llm = ScriptedLLM(
        tool("recent_conversations", {"hours": 24}),
        lambda p: text(
            "Hôm qua: " + ("An hỏi giá MA-100" if "MA-100" in tool_results(p)[0]["content"] else "?")
        ),
        text("Báo cáo ngày 29"),
    )
    office = make_office(tmp_path, llm, routines=[BRIEF], skills=["recent_conversations", "remember"])
    sales = office.employees["sales"]
    chat = fake_chat(sales)
    sales.state.add_admin(99)
    sales.state.remember_contact(1, "An")
    sales.state.append_turn(1, "MA-100 giá bao nhiêu?", "4.500.000 đồng", keep=40)

    assert office.tick(at_vn(28, 7, 59)) == []  # before the window
    tasks = office.tick(at_vn(28, 8, 5))
    assert len(tasks) == 1
    res = await tasks[0]
    assert res.status == "ok" and res.text == "Hôm qua: An hỏi giá MA-100"
    run_call = llm.calls[0]
    assert 'scheduled routine "daily-brief"' in run_call["system"][1]["text"]
    assert run_call["messages"] == [{"role": "user", "content": "Summarise yesterday's conversations."}]
    assert "remember" not in {t["name"] for t in run_call["tools"]}  # contact-only skills left out
    [(to, report)] = chat.sent
    assert to == 99 and "daily-brief" in report and report.endswith("Hôm qua: An hỏi giá MA-100")

    assert office.tick(at_vn(28, 8, 30)) == []  # same day: already ran
    assert office.tick(at_vn(28, 9, 1)) == []  # window closed
    assert sales.state.routine("daily-brief")["last_status"] == "ok"
    [rec] = office.runlog.tail(kind="routine")
    assert (
        rec["routine"] == "daily-brief" and rec["delivered"] == 1 and rec["tools"] == ["recent_conversations"]
    )

    await sales.command(99, "ai", "routine pause daily-brief")
    assert office.tick(at_vn(29, 8, 1)) == []
    await sales.command(99, "ai", "routine resume daily-brief")
    [task] = office.tick(at_vn(29, 8, 1))
    assert (await task).text == "Báo cáo ngày 29"
    await sales.command(99, "ai", "pause")
    assert office.tick(at_vn(30, 8, 1)) == []  # a paused employee runs no routines


async def test_manual_run_and_listing(tmp_path):
    llm = ScriptedLLM(text("Chạy tay"))
    office = make_office(tmp_path, llm, routines=[BRIEF])
    sales = office.employees["sales"]
    chat = fake_chat(sales)
    await sales.command(5, "admin", "secret-token")
    assert "daily-brief: mon-fri 08:00" in await sales.command(5, "ai", "routines")
    assert "Không có lịch" in await sales.command(5, "ai", "run nope")
    assert "Đang chạy daily-brief" in await sales.command(5, "ai", "run daily-brief")
    await asyncio.gather(*sales._tasks)
    assert chat.sent[-1][1].endswith("Chạy tay")
    assert sales.state.routine("daily-brief")["manual"] is True
    assert "period" not in sales.state.routine("daily-brief")  # a manual run keeps the scheduled one


# --------------------------------------------------------------------------- #
# Approval queue and webhook actions

ORDER = {
    "create_order": {
        "description": "Create a sales order in the shop system.",
        "url": "https://shop.local/orders",
        "headers": {"Authorization": "Bearer ${TEST_ORDER_TOKEN}"},
        "fields": {"customer": "Customer name", "items": "Products and quantities"},
        "confirm_message": "Đơn hàng của bạn đã được tạo.",
    }
}


class Shop:
    def __init__(self, status: int = 201):
        self.status = status
        self.requests: list[httpx2.Request] = []
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self._handle))

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(self.status, text="order DH-2001 created" if self.status < 300 else "down")


def order_office(tmp_path, llm, shop, **kw):
    office = make_office(
        tmp_path, llm, http=shop.client, actions=ORDER, skills=["create_order", "current_time"], **kw
    )
    sales = office.employees["sales"]
    return office, sales, fake_chat(sales)


async def test_actions_wait_for_approval_then_run(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_ORDER_TOKEN", "tok-123")
    shop = Shop()
    llm = ScriptedLLM(
        tool("create_order", {"customer": "An", "items": "2 x MA-100"}),
        lambda p: text("Đã ghi nhận: " + tool_results(p)[0]["content"]),
    )
    office, sales, chat = order_office(tmp_path, llm, shop)
    sales.state.add_admin(99)

    answer = await sales.agent.respond(1, "An", "Đặt 2 máy MA-100")
    assert "Queued as request #1" in answer and shop.requests == []
    [(to, note)] = chat.sent
    assert to == 99 and "Cần duyệt #1" in note and "items: 2 x MA-100" in note and "/ai approve 1" in note
    assert "#1 create_order — An" in await sales.command(99, "ai", "pending")

    assert await sales.command(99, "ai", "approve 1") == "Đã thực hiện #1: order DH-2001 created"
    [req] = shop.requests
    assert str(req.url) == "https://shop.local/orders" and req.headers["authorization"] == "Bearer tok-123"
    assert json.loads(req.content) == {
        "customer": "An",
        "items": "2 x MA-100",
        "_request_id": 1,
        "_employee": "sales",
    }
    assert chat.sent[-1] == (1, "Đơn hàng của bạn đã được tạo.")  # the contact hears back
    assert sales.state.action(1)["status"] == "done"
    assert "không thể duyệt" in await sales.command(99, "ai", "approve 1")  # only once
    assert [r["status"] for r in office.runlog.tail(kind="action")] == ["queued", "ok"]


async def test_rejected_and_failed_actions(tmp_path):
    shop = Shop(status=503)
    llm = ScriptedLLM(
        tool("create_order", {"customer": "An", "items": "1"}),
        text("ok"),
        tool("create_order", {"customer": "An", "items": "2"}),
        text("ok"),
    )
    _, sales, chat = order_office(tmp_path, llm, shop)
    await sales.command(99, "admin", "secret-token")
    await sales.agent.respond(1, "An", "a")
    await sales.agent.respond(1, "An", "b")

    assert await sales.command(99, "ai", "reject 1 hết hàng") == "Đã từ chối #1."
    assert chat.sent[-1] == (1, "Yêu cầu #1 chưa được chấp nhận: hết hàng")
    assert (await sales.command(99, "ai", "approve 2")).startswith("#2 thất bại: HTTP 503")
    assert sales.state.action(2)["status"] == "failed"
    assert "Không có yêu cầu #7" in await sales.command(99, "ai", "approve 7")


async def test_released_actions_run_without_approval(tmp_path):
    shop = Shop()
    llm = ScriptedLLM(
        tool("create_order", {"customer": "An", "items": "1"}),
        lambda p: text(tool_results(p)[0]["content"]),
    )
    _, sales, chat = order_office(tmp_path, llm, shop)
    await sales.command(99, "admin", "secret-token")
    assert "không cần duyệt" in await sales.command(99, "ai", "release create_order")
    assert "Không có hành động" in await sales.command(99, "ai", "release teleport")

    assert await sales.agent.respond(1, "An", "Đặt 1 máy") == "Done: order DH-2001 created"
    assert len(shop.requests) == 1 and chat.sent == []  # no approval request, no extra message
    assert "chờ duyệt" in await sales.command(99, "ai", "hold create_order")
    assert sales.settings.releases == ()


def test_action_config_validation(tmp_path):
    base = {"employees": [{"id": "a", "display_name": "A", "system_prompt": "p"}]}
    for actions, match in [
        ({"Bad": {}}, "lowercase"),
        ({"x": {"url": "ftp://x", "description": "d", "fields": {"a": "b"}}}, "http"),
        ({"x": {"url": "https://x", "description": "d"}}, "fields"),
        ({"x": {"url": "https://x", "description": "d", "fields": {"a": "b"}, "urll": 1}}, "unknown fields"),
    ]:
        with pytest.raises(ConfigError, match=match):
            parse_config({**base, "actions": actions}, tmp_path)
    with pytest.raises(ConfigError, match="releases name unknown"):
        parse_config({"employees": [{**base["employees"][0], "releases": ["nope"]}]}, tmp_path)
    with pytest.raises(ConfigError, match="timezone"):
        parse_config({"employees": [{**base["employees"][0], "timezone": "Mars/Base"}]}, tmp_path)
    with pytest.raises(ConfigError, match="built-in skill"):
        make_office(tmp_path, ScriptedLLM(), actions={"recall": {**ORDER["create_order"]}})


# --------------------------------------------------------------------------- #
# Corrections and the office report


async def test_corrections_are_added_to_the_prompt(tmp_path):
    llm = ScriptedLLM(text("ok"), text("ok"))
    sales = make_office(tmp_path, llm).employees["sales"]
    await sales.command(5, "admin", "secret-token")
    assert "quy tắc #1" in await sales.command(5, "ai", "correct Không báo giá lõi lọc qua chat.")
    await sales.agent.respond(1, "An", "hi")
    assert "Corrections from your manager" in llm.calls[0]["system"][0]["text"]
    assert "Không báo giá lõi lọc qua chat." in llm.calls[0]["system"][0]["text"]
    assert "1. (" in await sales.command(5, "ai", "corrections")
    assert await sales.command(5, "ai", "uncorrect 1") == "Đã xoá quy tắc #1."
    await sales.agent.respond(1, "An", "hi")
    assert "Corrections" not in llm.calls[1]["system"][0]["text"]
    assert "Cú pháp" in await sales.command(5, "ai", "uncorrect 3")


async def test_office_report_for_a_supervisor(tmp_path):
    shop = Shop()
    llm = ScriptedLLM(
        tool("create_order", {"customer": "An", "items": "1"}),
        text("ok"),
        tool("office_report", {"hours": 24}),
        lambda p: text(tool_results(p)[0]["content"]),
    )
    office, sales, _ = order_office(tmp_path, llm, shop, routines=[BRIEF])
    await sales.agent.respond(1, "An", "Đặt hàng")
    accountant = office.employees["accountant"]
    accountant.base = accountant.base.__class__(**{**accountant.base.__dict__, "skills": ("office_report",)})
    fake_chat(accountant)

    report_routine = parse_routine({"id": "end-of-day", "at": "17:30", "task": "Report on the team."})
    report = (await accountant.run_routine(report_routine, accountant.local_now())).text
    assert "## Lan - Sales (sales), active" in report
    assert "pending approvals: #1 create_order" in report
    assert "routine daily-brief: last run never" in report
    assert "'reply': 1" in report and "'action': 1" in report
    assert "office_report" in sk.BUILTIN


async def test_internal_skills_are_not_offered_to_ordinary_contacts(tmp_path):
    llm = ScriptedLLM(text("a"), text("b"), text("c"))
    office = make_office(tmp_path, llm, skills=["recent_conversations", "office_report", "current_time"])
    sales = office.employees["sales"]
    sales.state.add_admin(99)
    await sales.agent.respond(1, "An", "Các khách khác hỏi gì vậy?")
    await sales.agent.respond(99, "Chủ", "Tóm tắt hội thoại hôm nay")
    await sales.agent.consult("?", asker="Minh")
    names = [{t["name"] for t in c.get("tools", [])} for c in llm.calls]
    assert names[0] == {"current_time"}  # a customer never gets other people's conversations
    assert names[1] == {"recent_conversations", "office_report", "current_time"}  # the manager does
    assert names[2] == {"current_time"}  # nor does a colleague asking on a customer's behalf
    assert llm.calls[1]["system"][1]["text"] == 'You are chatting with your manager "Chủ".'
