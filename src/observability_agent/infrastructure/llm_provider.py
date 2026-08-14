"""LLM provider abstraction — single-method interface for structured generation."""

from __future__ import annotations

import os
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel


@runtime_checkable
class LLMProvider(Protocol):
    """Protocol for LLM providers. One method: generate structured output."""

    async def generate_structured(
        self,
        prompt: str,
        output_schema: type[BaseModel],
        system_prompt: str = "",
        max_retries: int = 2,
    ) -> BaseModel:
        """Generate a validated Pydantic model from the LLM."""
        ...


class PydanticAIProvider:
    """LLM provider backed by PydanticAI. Supports any OpenAI-compatible API.

    Configuration via env vars:
        LLM_MODEL: model identifier (default: "openai:gpt-4o")
        LLM_API_KEY: API key for the provider
        LLM_BASE_URL: base URL for OpenAI-compatible endpoints
    """

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        model = model or os.getenv("LLM_MODEL", "openai:gpt-4o")
        api_key = api_key or os.getenv("LLM_API_KEY", "")
        base_url = base_url or os.getenv("LLM_BASE_URL", "")

        # Parse model name:
        # - "openai:gpt-4o" -> "gpt-4o" (standard format)
        # - "nvidia/nemotron-3-ultra-550b-a55b:free" -> keep as-is (OpenRouter format)
        if ":" in model and "/" not in model:
            model_name = model.split(":", 1)[-1]
        else:
            model_name = model

        # Create provider with API key and base URL for OpenAI-compatible endpoints
        # Add OpenRouter required headers
        import httpx
        http_client = httpx.AsyncClient(
            headers={
                "HTTP-Referer": "https://github.com/observability-agent",
                "X-Title": "observability-agent",
            },
            # Every other client in this codebase (Prometheus, Loki, Tempo, K8s,
            # GitHub) sets an explicit timeout; this was the one that didn't.
            # Without it, a stalled proxy or an oversized batch-evaluate prompt
            # hangs the coroutine forever — observed live: an investigation stuck
            # for 40+ minutes on one call, no error, no log line, nothing for the
            # scheduler to retry. The Coordinator's LLM calls have no timeout or
            # try/except of their own (unlike the agents' evidence gathering), so
            # this is the only backstop. 120s is generous for one completion —
            # long enough that a legitimately large hypothesis batch still
            # finishes — but bounded, so a hang becomes a caught exception that
            # the scheduler's existing retry-and-flag-stale path can handle.
            timeout=httpx.Timeout(120.0, connect=10.0),
        )
        provider = OpenAIProvider(
            api_key=api_key or None,
            base_url=base_url or None,
            http_client=http_client,
        )
        self._model = OpenAIChatModel(model_name=model_name, provider=provider)
        # ponytail: lazy-init the agent per call since output_type varies
        self._agent: Any = None

    async def generate_structured(
        self,
        prompt: str,
        output_schema: type[BaseModel],
        system_prompt: str = "",
        max_retries: int = 2,
    ) -> BaseModel:
        from pydantic_ai import Agent

        agent = Agent(
            self._model,
            system_prompt=system_prompt,
        )
        result = await agent.run(
            prompt,
            output_type=output_schema,
            retries=max_retries,
        )
        return result.output


def create_llm_provider(
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> LLMProvider:
    """Factory: create the right LLM provider based on configuration.

    Set LLM_MOCK=true to use the mock provider (for tests and offline dev).
    Otherwise creates a PydanticAIProvider with env-var configuration.
    """
    if os.getenv("LLM_MOCK", "").lower() == "true":
        return MockLLMProvider()
    return PydanticAIProvider(model=model, api_key=api_key, base_url=base_url)


class MockLLMProvider:
    """Mock LLM provider that returns pre-registered Pydantic objects by schema type.

    Registers a queue of responses per schema type. Each call consumes one response.
    Used in tests — register responses in the order they'll be called.
    """

    def __init__(self) -> None:
        self._responses: dict[type[BaseModel], list[BaseModel]] = {}
        self.calls: list[dict[str, Any]] = []

    def register(self, output_schema: type[BaseModel], response: BaseModel) -> None:
        """Register a canned response. Multiple registrations for the same schema
        are queued and consumed in FIFO order."""
        self._responses.setdefault(output_schema, []).append(response)

    async def generate_structured(
        self,
        prompt: str,
        output_schema: type[BaseModel],
        system_prompt: str = "",
        max_retries: int = 2,
    ) -> BaseModel:
        self.calls.append({
            "prompt": prompt,
            "output_schema": output_schema.__name__,
            "system_prompt": system_prompt,
        })
        queue = self._responses.get(output_schema, [])
        if queue:
            return queue.pop(0)
        raise KeyError(
            f"No mock response registered for {output_schema.__name__}. "
            f"Call mock_llm.register({output_schema.__name__}, ...) in your test."
        )