# ADR-004: Agent Protocol Pattern for Observability Sources

**Status:** Accepted
**Date:** 2026-07-21

## Context

The Coordinator needs to gather evidence from six observability sources:

1. **Prometheus** — metrics (PromQL)
2. **Loki** — logs (LogQL)
3. **Tempo** — distributed traces (TraceQL)
4. **Kubernetes** — pod/event/resource state (kubectl)
5. **Deployment** — release history (GitHub/ArgoCD)
6. **Documentation** — runbooks and procedures (RAG)

Each source has a different query language, API, and MCP server. The Coordinator should not know these details. It should only know "I need evidence from this source" and receive a uniform `Evidence` object.

Additionally, each agent needs to be mockable for testing. The Coordinator tests must verify that the right agents are called in the right order without standing up real MCP servers.

## Decision

**Each observability source is a typed callable protocol.** The Coordinator depends on the protocol, not the implementation. MCP is hidden behind the agent interface.

### Protocol signature

```python
class MetricsAgent(Protocol):
    async def __call__(
        self,
        natural_language: str,
        hypothesis_id: str | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> Evidence: ...
```

Six protocols: `MetricsAgent`, `LogsAgent`, `TraceAgent`, `KubernetesAgent`, `DeploymentAgent`, `KnowledgeAgent`. Each is a 1:1 mapping to an observability source.

### Implementation pattern

Each real agent follows the same pipeline:

```
NL query → LLM translates to domain-specific query language
        → MCP client executes the query
        → LLM summarizes results
        → Evidence object returned
```

Example for `MetricsAgent`:
```
"checkout latency" → LLM → "rate(http_request_duration_seconds_sum[5m])" → Prometheus MCP → raw data → LLM → "P99 latency spiked to 1.2s" → Evidence
```

### Mock agent

`MockAgent` returns pre-built `Evidence` objects from a queue. It records all calls for assertion. This enables Coordinator tests without any MCP servers.

```python
agent = MockAgent([evidence1, evidence2])
result = await agent("some query", hypothesis_id="hyp-1")
# Returns evidence1, hyp_id set to "hyp-1"
```

## Consequences

### Positive

- **Testable.** The Coordinator tests inject `MockAgent` instances. No MCP servers needed. Tests verify agent calls and hypothesis_id routing.
- **Swappable.** Agent implementations can be swapped without changing the Coordinator. A future "TraceAgent via Elastic APM" can replace the Tempo implementation transparently.
- **Uniform error handling.** Each agent wraps its execution in try/except, returning error Evidence on failure. The Coordinator's `_safe_call` adds a second layer of protection.
- **Natural LLM integration.** Each agent calls the LLM twice (translate + summarize), using the same `LLMProvider` protocol. This is consistent and mockable.

### Negative

- **Six protocols.** Six nearly identical protocol definitions. A single generic `Agent[Source]` protocol could reduce duplication. Kept separate for now — Protocol classes are cheap and the explicit names (`MetricsAgent`, `LogsAgent`) make the Coordinator's constructor self-documenting.
- **Two LLM calls per agent.** Each evidence collection is 2 LLM round-trips (translate + summarize). For 6 exploratory agents + N targeted investigations, this adds up. The batch evaluation in the Coordinator partially offsets this by combining all hypothesis evaluations into one call.

## Alternatives Not Chosen

- **Single `Agent` protocol with a `source` parameter.** Less type-safe. The Coordinator's constructor would take a list of `Agent` instances and need to map by source. The explicit six-agent constructor is verbose but clear.
- **Agents as domain entities.** Would require an `Agent` entity, `AgentRun` entity, and persistence. Over-engineering — agents are stateless workers. Identity is recorded as metadata (`collected_by` field on Evidence), not as entities.
- **Direct MCP calls from the Coordinator.** Would couple the Coordinator to MCP. The agent protocol is the seam — it enables mock testing and future source swaps.