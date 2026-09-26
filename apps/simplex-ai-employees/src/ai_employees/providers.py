"""AI model providers behind a common interface.

The agent loop speaks in `Turn`s, `ToolCall`s and `ToolResult`s; each provider
keeps the conversation in its own wire format and translates:

- `anthropic`: Claude through the official Anthropic SDK.
- `openai`: any OpenAI-compatible Chat Completions API (OpenAI, Gemini's
  OpenAI endpoint, DeepSeek, Groq, OpenRouter, Mistral, Ollama, vLLM, LM Studio…).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import urlparse

import anthropic
import httpx2

from .llm import LLM, AnthropicLLM, MissingCredentialsError

if TYPE_CHECKING:
    from .config import EmployeeConfig
    from .skills import Skill

log = logging.getLogger(__name__)

PROVIDERS = ("anthropic", "openai")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models on which server-side refusal fallback is turned on by default.
FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5")


def fallback_default(provider: str, model: str) -> bool:
    return provider == "anthropic" and model.startswith(FALLBACK_MODELS)


Stop = Literal["end", "tool_use", "max_tokens", "refusal", "pause"]


class ModelError(Exception):
    """The model could not produce a turn (network, rate limit, server error…)."""


class ModelAuthError(ModelError):
    """Credentials are missing or rejected."""


@dataclass(frozen=True)
class ModelProfile:
    """A declared AI model: which API, where, with which key and model name."""

    name: str
    provider: str
    model: str
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)
    refusal_fallback: bool = True  # anthropic only
    timeout: float = 120.0

    def key(self) -> str | None:
        if self.api_key_env:
            return os.environ.get(self.api_key_env) or self.api_key
        return self.api_key

    def describe(self) -> str:
        where = f" @ {urlparse(self.base_url).netloc or self.base_url}" if self.base_url else ""
        return f"{self.provider}: {self.model}{where}"


@dataclass
class ToolCall:
    id: str
    name: str
    input: Any


@dataclass
class ToolResult:
    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass
class Turn:
    stop: Stop
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    message: Any = None  # the assistant message in the provider's format, to append back
    tokens_in: int = 0
    tokens_out: int = 0


class ChatModel(Protocol):
    profile: ModelProfile

    def supports(self, skill: Skill) -> bool: ...

    def messages(self, history: list[dict[str, str]], user_text: str) -> list[Any]: ...

    async def step(
        self,
        *,
        system: tuple[str, str],
        messages: list[Any],
        tools: list[Skill],
        settings: EmployeeConfig,
    ) -> Turn: ...

    def tool_result_messages(self, results: list[ToolResult]) -> list[Any]: ...


def plain_messages(history: list[dict[str, str]], user_text: str) -> list[dict[str, Any]]:
    return [*history, {"role": "user", "content": user_text}]


class AnthropicChatModel:
    def __init__(self, profile: ModelProfile, llm: LLM | None = None):
        self.profile = profile
        if llm is None:
            kw: dict[str, Any] = {"timeout": profile.timeout, "max_retries": 2}
            if key := profile.key():
                kw["api_key"] = key
            if profile.base_url:
                kw["base_url"] = profile.base_url
            if profile.headers:
                kw["default_headers"] = profile.headers
            llm = AnthropicLLM(anthropic.AsyncAnthropic(**kw))
        self.llm = llm

    def supports(self, skill: Skill) -> bool:
        return True

    def messages(self, history: list[dict[str, str]], user_text: str) -> list[Any]:
        return plain_messages(history, user_text)

    async def step(
        self, *, system: tuple[str, str], messages: list[Any], tools: list[Skill], settings: EmployeeConfig
    ) -> Turn:
        params: dict[str, Any] = {
            "model": self.profile.model,
            "max_tokens": settings.max_tokens,
            # Stable prompt first, per-conversation line last, so the prefix caches.
            "system": [{"type": "text", "text": system[0]}, {"type": "text", "text": system[1]}],
            "messages": messages,
            "cache_control": {"type": "ephemeral"},
            **self.profile.extra_body,
        }
        if tools:
            params["tools"] = [t.tool_param() for t in tools]
        if settings.effort:
            params["output_config"] = {"effort": settings.effort}
        if self.profile.refusal_fallback:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        try:
            resp = await self.llm.create(**params)
        except (anthropic.AuthenticationError, MissingCredentialsError) as e:
            raise ModelAuthError(str(e)) from e
        except anthropic.APIStatusError as e:
            raise ModelError(f"HTTP {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise ModelError(str(e)) from e

        stop: Stop = {
            "tool_use": "tool_use",
            "max_tokens": "max_tokens",
            "refusal": "refusal",
            "pause_turn": "pause",
        }.get(resp.stop_reason, "end")
        usage = getattr(resp, "usage", None)
        tokens_in = sum(
            getattr(usage, f, 0) or 0
            for f in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
        return Turn(
            stop=stop,
            text="\n\n".join(b.text for b in resp.content if b.type == "text").strip(),
            tool_calls=[ToolCall(b.id, b.name, b.input) for b in resp.content if b.type == "tool_use"],
            message={"role": "assistant", "content": resp.content},
            tokens_in=tokens_in,
            tokens_out=getattr(usage, "output_tokens", 0) or 0,
        )

    def tool_result_messages(self, results: list[ToolResult]) -> list[Any]:
        blocks = []
        for r in results:
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": r.tool_call_id,
                "content": r.content,
            }
            if r.is_error:
                block["is_error"] = True
            blocks.append(block)
        return [{"role": "user", "content": blocks}]


class OpenAICompatibleChatModel:
    """Chat Completions over HTTP: POST {base_url}/chat/completions."""

    def __init__(self, profile: ModelProfile, http: httpx2.AsyncClient | None = None):
        self.profile = profile
        self.http = http or httpx2.AsyncClient(timeout=profile.timeout)
        self.url = (profile.base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"

    def supports(self, skill: Skill) -> bool:
        return skill.server_tool is None  # server tools (web_search) run only on Anthropic's API

    def messages(self, history: list[dict[str, str]], user_text: str) -> list[Any]:
        return plain_messages(history, user_text)

    async def step(
        self, *, system: tuple[str, str], messages: list[Any], tools: list[Skill], settings: EmployeeConfig
    ) -> Turn:
        body: dict[str, Any] = {
            "model": self.profile.model,
            "messages": [{"role": "system", "content": f"{system[0]}\n\n{system[1]}"}, *messages],
            **self.profile.extra_body,
        }
        if tools:
            body["tools"] = [t.openai_tool() for t in tools]
        headers = {"Content-Type": "application/json", **self.profile.headers}
        if key := self.profile.key():
            headers.setdefault("Authorization", f"Bearer {key}")
        try:
            r = await self.http.post(self.url, json=body, headers=headers)
        except httpx2.HTTPError as e:
            raise ModelError(f"{self.url}: {e}") from e
        if r.status_code in (401, 403):
            raise ModelAuthError(f"HTTP {r.status_code}: {r.text[:300]}")
        if r.status_code >= 400:
            raise ModelError(f"HTTP {r.status_code}: {r.text[:300]}")
        try:
            data = r.json()
            choice = data["choices"][0]
            msg = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise ModelError(f"unexpected response: {r.text[:300]}") from e

        calls = []
        for c in msg.get("tool_calls") or []:
            fn = c.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = None  # reported back to the model as a tool error
            calls.append(ToolCall(c.get("id", ""), fn.get("name", ""), args))
        finish = choice.get("finish_reason")
        stop: Stop = (
            "tool_use"
            if calls
            else {"length": "max_tokens", "content_filter": "refusal"}.get(finish or "", "end")
        )
        # Echo back only the fields the API accepts in an assistant message.
        message = {"role": "assistant", "content": msg.get("content")}
        if msg.get("tool_calls"):
            message["tool_calls"] = msg["tool_calls"]
        usage = data.get("usage") or {}
        return Turn(
            stop=stop,
            text=(msg.get("content") or "").strip(),
            tool_calls=calls,
            message=message,
            tokens_in=usage.get("prompt_tokens") or 0,
            tokens_out=usage.get("completion_tokens") or 0,
        )

    def tool_result_messages(self, results: list[ToolResult]) -> list[Any]:
        return [{"role": "tool", "tool_call_id": r.tool_call_id, "content": r.content} for r in results]


def make_model(
    profile: ModelProfile, anthropic_llm: LLM | None = None, http: httpx2.AsyncClient | None = None
) -> ChatModel:
    if profile.provider == "anthropic":
        return AnthropicChatModel(profile, anthropic_llm)
    if profile.provider == "openai":
        if not profile.key() and profile.api_key_env:
            log.warning(
                "model %s: %s is not set; sending requests without a key", profile.name, profile.api_key_env
            )
        return OpenAICompatibleChatModel(profile, http)
    raise ValueError(f"unknown provider {profile.provider}")
