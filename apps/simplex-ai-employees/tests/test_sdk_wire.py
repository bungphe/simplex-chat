"""The real Anthropic SDK against a mock HTTP transport: checks what goes over the wire."""

from __future__ import annotations

import json

import anthropic
import httpx2

from ai_employees.llm import AnthropicLLM

from fakes import make_office


def message(content: list[dict], stop_reason: str) -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


async def test_requests_are_valid_for_the_sdk(tmp_path):
    requests: list[httpx2.Request] = []
    replies = [
        message(
            [{"type": "tool_use", "id": "toolu_9", "name": "knowledge_search", "input": {"query": "MA-100"}}],
            "tool_use",
        ),
        message([{"type": "text", "text": "MA-100 giá 4.500.000 đồng."}], "end_turn"),
    ]

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json=replies.pop(0))

    client = anthropic.AsyncAnthropic(
        api_key="test-key",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    sales = make_office(tmp_path, AnthropicLLM(client)).employees["sales"]

    assert await sales.agent.respond(1, "An", "MA-100 giá bao nhiêu?") == "MA-100 giá 4.500.000 đồng."

    first, second = (json.loads(r.content) for r in requests)
    assert requests[0].url.path == "/v1/messages"
    assert "server-side-fallback-2026-07-01" in requests[0].headers["anthropic-beta"]
    assert first["fallbacks"] == "default"
    assert first["cache_control"] == {"type": "ephemeral"}
    assert first["output_config"] == {"effort": "medium"}
    assert {t["name"] for t in first["tools"]} >= {"knowledge_search", "ask_colleague"}
    # the SDK's own response blocks round-trip into the next request
    assert second["messages"][1] == {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "toolu_9", "name": "knowledge_search", "input": {"query": "MA-100"}}
        ],
    }
    result = second["messages"][2]["content"][0]
    assert result["tool_use_id"] == "toolu_9" and "4.500.000" in result["content"]
