"""Declared models, OpenAI-compatible providers, and mixing providers in one office."""

from __future__ import annotations

import pytest

from ai_employees.agent import BUSY_TEXT, REFUSAL_TEXT
from ai_employees.config import ConfigError, parse_config

from fakes import OpenAIServer, ScriptedLLM, make_office, oa_text, oa_tool, text, tool, tool_results

LOCAL = {
    "local": {
        "provider": "openai",
        "base_url": "http://llm.local/v1/",
        "model": "qwen-test",
        "api_key_env": "TEST_LOCAL_KEY",
        "headers": {"X-Team": "ops"},
        "extra_body": {"temperature": 0.2},
    }
}


async def test_openai_compatible_tool_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_LOCAL_KEY", "sk-local")
    server = OpenAIServer(
        oa_tool("knowledge_search", {"query": "hoá đơn VAT"}),
        lambda body: oa_text(
            "Cần " + ("mã số thuế" if "mã số thuế" in body["messages"][-1]["content"] else "?")
        ),
    )
    office = make_office(tmp_path, ScriptedLLM(), models=LOCAL, http=server.client, accountant_model="local")
    accountant = office.employees["accountant"]

    assert await accountant.agent.respond(9, "An", "Xuất hoá đơn VAT cần gì?") == "Cần mã số thuế"

    req, second = server.requests[0], server.bodies[1]
    first = server.bodies[0]
    assert str(req.url) == "http://llm.local/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-local" and req.headers["x-team"] == "ops"
    assert first["model"] == "qwen-test" and first["temperature"] == 0.2
    assert first["messages"][0]["role"] == "system"
    assert first["messages"][0]["content"].startswith("Bạn là Minh")
    assert first["messages"][1:] == [{"role": "user", "content": "Xuất hoá đơn VAT cần gì?"}]
    assert first["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "knowledge_search",
                "description": first["tools"][0]["function"]["description"],
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "Keywords to search for"}},
                    "required": ["query"],
                },
            },
        }
    ]
    assistant, result = second["messages"][2:]
    assert assistant["role"] == "assistant" and assistant["tool_calls"][0]["id"] == "call_1"
    assert result["role"] == "tool" and result["tool_call_id"] == "call_1"
    assert "mã số thuế" in result["content"]
    assert accountant.state.history(9)[-1] == {"role": "assistant", "content": "Cần mã số thuế"}


async def test_employees_on_different_providers_work_together(tmp_path):
    server = OpenAIServer(oa_text("Cần tên công ty, mã số thuế, email."))
    llm = ScriptedLLM(
        tool("ask_colleague", {"colleague": "accountant", "question": "Xuất hoá đơn VAT cần gì?"}),
        lambda p: text("Kế toán nói: " + tool_results(p)[0]["content"]),
    )
    office = make_office(tmp_path, llm, models=LOCAL, http=server.client, accountant_model="local")

    answer = await office.employees["sales"].agent.respond(1, "An", "Mình cần hoá đơn VAT")

    assert answer == "Kế toán nói: Cần tên công ty, mã số thuế, email."
    assert llm.calls[0]["model"] == "claude-opus-5"  # sales on Claude
    assert server.bodies[0]["model"] == "qwen-test"  # accountant on the local model
    assert "Lan - Sales is asking you" in server.bodies[0]["messages"][0]["content"]


async def test_openai_stop_reasons_and_errors(tmp_path):
    server = OpenAIServer(
        oa_text("dài quá", finish="length"),
        oa_text("", finish="content_filter"),
        {"status": 401, "text": "invalid api key"},
        {"status": 503},
        oa_tool("knowledge_search", "{not json"),
        oa_text("xin lỗi"),
    )
    office = make_office(tmp_path, ScriptedLLM(), models=LOCAL, http=server.client, accountant_model="local")
    agent = office.employees["accountant"].agent

    assert await agent.respond(1, "An", "a") == "dài quá …"
    assert await agent.respond(1, "An", "b") == REFUSAL_TEXT
    assert await agent.respond(1, "An", "c") == BUSY_TEXT
    assert await agent.respond(1, "An", "d") == BUSY_TEXT
    assert await agent.respond(1, "An", "e") == "xin lỗi"
    bad_args = server.bodies[-1]["messages"][-1]
    assert bad_args == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "Error: tool input must be a JSON object",
    }
    assert "Authorization" not in server.requests[0].headers  # TEST_LOCAL_KEY unset: no key sent


async def test_server_tools_are_not_offered_to_other_providers(tmp_path):
    server = OpenAIServer(oa_text("ok"))
    office = make_office(
        tmp_path,
        ScriptedLLM(),
        models=LOCAL,
        http=server.client,
        model="local",
        skills=["web_search", "current_time"],
    )
    await office.employees["sales"].agent.respond(1, "An", "hi")
    assert [t["function"]["name"] for t in server.bodies[0]["tools"]] == ["current_time"]


def test_model_declarations(tmp_path):
    cfg = parse_config(
        {
            "models": {
                "claude": {"provider": "anthropic", "model": "claude-opus-5", "api_key_env": "X"},
                "haiku": {"model": "claude-haiku-4-5"},
                **LOCAL,
            },
            "employees": [{"id": "a", "display_name": "A", "system_prompt": "p", "model": "local"}],
        },
        tmp_path,
    )
    assert cfg.models["claude"].refusal_fallback and not cfg.models["haiku"].refusal_fallback
    assert not cfg.models["local"].refusal_fallback
    assert cfg.models["local"].describe() == "openai: qwen-test @ llm.local"
    assert cfg.model_profile("claude-sonnet-5").provider == "anthropic"  # bare Claude ids still work
    assert cfg.model_profile("gpt-x") is None

    def bad(models, model="m"):
        emp = [{"id": "a", "display_name": "A", "system_prompt": "p", "model": model}]
        return lambda: parse_config({"models": models, "employees": emp}, tmp_path)

    for build, match in [
        (bad({"m": {"provider": "azure", "model": "x"}}), "provider must be one of"),
        (bad({"m": {"provider": "openai"}}), "'model'"),
        (bad({"m": {"model": "x", "apikey": "typo"}}), "unknown fields apikey"),
        (bad({}, model="gpt-4"), "not declared"),
    ]:
        with pytest.raises(ConfigError, match=match):
            build()


async def test_admin_assigns_declared_models(tmp_path):
    office = make_office(tmp_path, ScriptedLLM(), models=LOCAL)
    sales = office.employees["sales"]
    await sales.command(5, "admin", "secret-token")

    listing = await sales.command(5, "ai", "models")
    assert "- local: openai: qwen-test @ llm.local" in listing
    assert "- claude-opus-5: anthropic: claude-opus-5 — dùng bởi sales, accountant" in listing
    assert "Các model đã khai báo" in await sales.command(5, "ai", "model gpt-9")
    assert (
        await sales.command(5, "ai", "model local") == "Đã gán model local (openai: qwen-test @ llm.local)."
    )
    assert sales.chat_model().profile.name == "local"
    assert "Model: local (openai: qwen-test @ llm.local)" in await sales.command(5, "ai", "show")
    # an override naming a model removed from the config falls back to the configured one
    sales.state.set_override("model", "removed")
    assert sales.chat_model().profile.name == "claude-opus-5"
