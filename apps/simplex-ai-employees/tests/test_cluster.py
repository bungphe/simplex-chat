"""Several office processes on one PostgreSQL (run with AIE_TEST_DATABASE_URL)."""

from __future__ import annotations

import asyncio
import os

import pytest
from test_channels import WEBHOOK, Platforms

from ai_employees.config import ConfigError

from fakes import ScriptedLLM, fake_chat, make_office, text

pytestmark = pytest.mark.skipif(not os.environ.get("AIE_TEST_DATABASE_URL"), reason="needs PostgreSQL")


def shard(tmp_path, monkeypatch, index, llm, platforms):
    monkeypatch.setenv("AIE_SHARD", str(index))
    monkeypatch.setenv("T_HOOK_SECRET", "hook-secret-1")
    office = make_office(tmp_path, llm, http=platforms.client, channels=[WEBHOOK], cluster={"shards": 2})
    return office, fake_chat(office.employees["sales"])


async def test_each_conversation_is_answered_once_by_its_shard(tmp_path, monkeypatch):
    platforms = Platforms()
    llm0, llm1 = ScriptedLLM(), ScriptedLLM()
    office0, chat0 = shard(tmp_path, monkeypatch, 0, llm0, platforms)
    office1, _ = shard(tmp_path, monkeypatch, 1, llm1, platforms)
    stopping = asyncio.Event()
    listeners = [asyncio.create_task(o.cluster.run(stopping)) for o in (office0, office1)]
    await asyncio.sleep(0.5)  # LISTEN is active
    try:
        # two customers, both arriving at shard 1 (a load balancer may send anything anywhere)
        convs = [
            office1.hub.push_inbound("website", {"conversation_id": f"c{i}", "text": "Chào shop"})
            for i in (1, 2)
        ]
        mine = {
            office0: [c for c in convs if office0.cluster.owns(c)],
            office1: [c for c in convs if office1.cluster.owns(c)],
        }
        assert len(mine[office0]) == 1 and len(mine[office1]) == 1
        llm0.responses.append(text("Dạ shop 0 đây"))
        llm1.responses.append(text("Dạ shop 1 đây"))
        for _ in range(50):
            await asyncio.sleep(0.1)
            if len(platforms.sent) == 2:
                break
        await asyncio.sleep(0.3)
        assert sorted(b["text"] for _, b in platforms.sent) == ["Dạ shop 0 đây", "Dạ shop 1 đây"]  # once each
        by_conv = {b["conversation_id"]: b["text"] for _, b in platforms.sent}
        assert by_conv[mine[office0][0].external_id] == "Dạ shop 0 đây"  # answered by its owner

        # SimpleX only runs on shard 0: shard 1's messages go through the outbox
        await office1.cluster.simplex_send(office1.employees["sales"], 42, "Đơn #7 đã xác nhận")
        for _ in range(30):
            await asyncio.sleep(0.1)
            if chat0.sent:
                break
        assert chat0.sent == [(42, "Đơn #7 đã xác nhận")]
        await office0.cluster._drain_outbox()  # never sent twice
        assert len(chat0.sent) == 1
    finally:
        stopping.set()
        await asyncio.wait_for(asyncio.gather(*listeners), timeout=5)


def test_shards_need_postgres(tmp_path, monkeypatch):
    monkeypatch.delenv("AIE_TEST_DATABASE_URL")
    with pytest.raises(ConfigError, match="PostgreSQL"):
        make_office(tmp_path, ScriptedLLM(), cluster={"shards": 2})
    monkeypatch.setenv("AIE_SHARD", "3")
    with pytest.raises(ConfigError, match="AIE_SHARD=3"):
        make_office(tmp_path / "b", ScriptedLLM())
