"""Model access. `LLM` is the seam tests replace with a scripted fake."""

from __future__ import annotations

from typing import Any, Protocol

import anthropic


class MissingCredentialsError(Exception):
    """No Claude API credentials could be resolved from the environment."""


class LLM(Protocol):
    async def create(self, **params: Any) -> Any: ...


class AnthropicLLM:
    """Claude via the official SDK. Credentials resolve from the environment
    (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an `ant auth login` profile)."""

    def __init__(self, client: anthropic.AsyncAnthropic | None = None):
        # Chat replies are short; fail faster than the SDK's 10-minute default.
        self.client = client or anthropic.AsyncAnthropic(timeout=120.0, max_retries=2)

    async def create(self, **params: Any) -> Any:
        # The beta surface carries the server-side refusal fallback parameters.
        try:
            return await self.client.beta.messages.create(**params)
        except TypeError as e:
            # The SDK reports unresolvable credentials as a TypeError at request time.
            if "authentication method" in str(e):
                raise MissingCredentialsError(str(e)) from e
            raise
