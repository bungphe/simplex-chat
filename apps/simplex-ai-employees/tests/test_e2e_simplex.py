"""End-to-end over a real SimpleX network: a customer chats with AI employees.

Needs libsimplex and a reachable SMP server; enable with
    SIMPLEX_TEST_SMP=smp://<fingerprint>@127.0.0.1 pytest tests/test_e2e_simplex.py
Sales runs on the Anthropic provider and the accountant on an OpenAI-compatible one;
both models are rule-based fakes, so no API key is needed. Everything else is real.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import AsyncExitStack
from typing import Any

import pytest
from simplex_chat import Client, Message, Profile, SqliteDb

from ai_employees.employee import EmployeeBot

from fakes import OpenAIServer, make_office, oa_text, oa_tool, text, tool, tool_names

SMP = os.environ.get("SIMPLEX_TEST_SMP")
pytestmark = pytest.mark.skipif(not SMP, reason="set SIMPLEX_TEST_SMP to run")


class RuleLLM:
    """Picks a tool from keywords, then answers with the tool results."""

    async def create(self, **p: Any) -> Any:
        last = p["messages"][-1]
        if isinstance(last["content"], list):  # tool results came back
            return text("Kết quả: " + " | ".join(r["content"] for r in last["content"])[:600])
        q, names = last["content"], tool_names(p)
        if "quản lý" in q and "handoff_to_human" in names:
            return tool("handoff_to_human", {"summary": q})
        if "hoá đơn" in q and "ask_colleague" in names:
            return tool("ask_colleague", {"colleague": "accountant", "question": q})
        if "knowledge_search" in names:
            return tool("knowledge_search", {"query": q})
        return text("Không rõ")


def openai_rules(body: dict[str, Any]) -> dict[str, Any]:
    """The same keyword rules, answered in OpenAI Chat Completions format."""
    last = body["messages"][-1]
    if last["role"] == "tool":
        return oa_text("Kết quả: " + last["content"][:600])
    names = {t["function"]["name"] for t in body.get("tools", [])}
    if "knowledge_search" in names:
        return oa_tool("knowledge_search", {"query": last["content"]})
    return oa_text("Không rõ")


class Customer(Client):
    def __init__(self, **kw: Any):
        super().__init__(**kw)
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.on_message(chat_type="direct")(self._collect)

    async def _collect(self, msg: Message[Any]) -> None:
        await self.inbox.put(msg.text or "")

    async def _post_start(self, user: Any) -> None:
        await self.api.send_chat_cmd(f"/smp {SMP}")
        await super()._post_start(user)

    async def ask(self, contact_id: int, text: str, replies: int = 1) -> list[str]:
        await self.api.api_send_text_message(["direct", contact_id], text)
        return [await asyncio.wait_for(self.inbox.get(), 30) for _ in range(replies)]


async def test_customer_chats_with_ai_employees(tmp_path):
    server = OpenAIServer(*([openai_rules] * 10))
    office = make_office(
        tmp_path,
        RuleLLM(),
        smp=(SMP,),
        models={"local": {"provider": "openai", "base_url": "http://llm.local/v1", "model": "qwen-test"}},
        http=server.client,
        accountant_model="local",
        welcome="Xin chào, mình là Lan!",
    )
    sales = office.employees["sales"]
    customer = Customer(
        profile=Profile(display_name="Khách An"), db=SqliteDb(file_prefix=str(tmp_path / "customer"))
    )
    assert isinstance(sales.bot, EmployeeBot)

    async with AsyncExitStack() as stack:
        for e in office.employees.values():
            await stack.enter_async_context(e.bot)
        await stack.enter_async_context(customer)
        tasks = [
            asyncio.create_task(c.serve_forever())
            for c in [*(e.bot for e in office.employees.values()), customer]
        ]
        await asyncio.sleep(0.5)
        try:
            assert sales.bot.address and "127.0.0.1" in sales.bot.address
            contact = await customer.connect_to(sales.bot.address, timeout=60)
            cid = contact["contactId"]
            assert contact["profile"]["displayName"] == "Lan - Sales"
            assert await asyncio.wait_for(customer.inbox.get(), 30) == "Xin chào, mình là Lan!"

            # knowledge_search over the sales documents
            [r] = await customer.ask(cid, "Máy lọc nước MA-100 giá bao nhiêu?")
            assert "4.500.000" in r
            # delegation: sales asks the accountant, who searches accounting documents
            [r] = await customer.ask(cid, "Mình cần xuất hoá đơn VAT thì cần gì?")
            assert "mã số thuế" in r
            assert server.bodies and server.bodies[0]["model"] == "qwen-test"  # accountant used its own model
            # admin commands in chat
            [r] = await customer.ask(cid, "/ai show")
            assert "chỉ dành cho quản trị viên" in r
            [r] = await customer.ask(cid, "/admin secret-token")
            assert "Bạn đã là quản trị viên" in r
            [r] = await customer.ask(cid, "/ai prompt Bạn là Lan.\nLuôn xưng em.")
            assert r == "Đã cập nhật vai trò (system prompt)."
            assert sales.settings.system_prompt == "Bạn là Lan.\nLuôn xưng em."
            # handoff: the admin (this contact) receives the escalation, then the reply
            got = await customer.ask(cid, "Cho mình gặp quản lý", replies=2)
            assert any(m.startswith("[Chuyển tiếp từ Khách An]") for m in got)
            assert any("Forwarded to 1 supervisor" in m for m in got)
            # memory of this conversation is kept per contact
            assert len(sales.state.history(cid)) == 6
        finally:
            office.stop()
            customer.stop()
            await asyncio.gather(*tasks)
