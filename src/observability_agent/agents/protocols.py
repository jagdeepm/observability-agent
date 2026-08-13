"""Agent protocols — typed function signatures for each specialized agent.

Each agent is a callable that takes a query + context and returns Evidence.
The Coordinator calls these; MCP is an implementation detail hidden behind the interface.
"""

from __future__ import annotations

from typing import Any, Protocol

from observability_agent.domain.models import Evidence


class MetricsAgent(Protocol):
    """Query Prometheus metrics. NL → PromQL → MCP → Evidence."""

    async def __call__(
        self,
        natural_language: str,
        hypothesis_id: str | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> Evidence: ...


class LogsAgent(Protocol):
    """Query Loki logs. NL → LogQL → MCP → Evidence."""

    async def __call__(
        self,
        natural_language: str,
        hypothesis_id: str | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> Evidence: ...


class TraceAgent(Protocol):
    """Query Tempo traces. NL → TraceQL → MCP → Evidence."""

    async def __call__(
        self,
        natural_language: str,
        hypothesis_id: str | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> Evidence: ...


class KubernetesAgent(Protocol):
    """Query Kubernetes state. NL → kubectl → MCP → Evidence."""

    async def __call__(
        self,
        natural_language: str,
        hypothesis_id: str | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> Evidence: ...


class DeploymentAgent(Protocol):
    """Query deployment history. NL → GitHub/ArgoCD → MCP → Evidence."""

    async def __call__(
        self,
        natural_language: str,
        hypothesis_id: str | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> Evidence: ...


class KnowledgeAgent(Protocol):
    """Query documentation/runbooks. NL → RAG search → Evidence."""

    async def __call__(
        self,
        natural_language: str,
        hypothesis_id: str | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> Evidence: ...


# ---------------------------------------------------------------------------
# Mock agent factory for tests
# ---------------------------------------------------------------------------


class MockAgent:
    """Returns pre-built Evidence objects. Used in Coordinator tests.

    Usage:
        agent = MockAgent([evidence1, evidence2])
        result = await agent("some query")
        # Returns evidence1, then evidence2, then raises if exhausted
    """

    def __init__(self, responses: list[Evidence] | None = None) -> None:
        self._responses = list(responses or [])
        self._index = 0
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        natural_language: str,
        hypothesis_id: str | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> Evidence:
        self.calls.append({
            "natural_language": natural_language,
            "hypothesis_id": hypothesis_id,
            "extra_context": extra_context,
        })
        if self._index >= len(self._responses):
            raise IndexError(
                f"MockAgent exhausted after {self._index} calls. "
                f"Register more responses or reduce call count."
            )
        result = self._responses[self._index]
        self._index += 1
        # Apply the hypothesis_id from the call if provided (overrides pre-built)
        if hypothesis_id is not None:
            result.hypothesis_id = hypothesis_id
        return result

    def register(self, *evidence: Evidence) -> None:
        self._responses.extend(evidence)