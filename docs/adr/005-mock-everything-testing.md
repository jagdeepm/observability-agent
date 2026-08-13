# ADR-005: Mock-Everything Testing Strategy

**Status:** Accepted
**Date:** 2026-07-21

## Context

The Coordinator orchestrates LLM calls and agent calls. Testing the Coordinator's mechanics — does it call the right agents in the right order, apply LLM outputs correctly, handle errors gracefully — should not require real LLM API calls or MCP servers. Those are slow, flaky, and expensive.

The project needs a testing strategy that:

- Runs in milliseconds, not seconds
- Passes deterministically (no LLM non-determinism)
- Costs nothing (no API calls)
- Tests the Coordinator's mechanics, not the LLM's judgment

## Decision

**Mock every external dependency.** The Coordinator's constructor takes all dependencies as injected interfaces. Tests replace every dependency with a mock:

| Dependency | Mock | What it returns |
|---|---|---|
| `LLMProvider` | `MockLLMProvider` | Queued Pydantic objects by schema type |
| `MetricsAgent` | `MockAgent` | Queued `Evidence` objects |
| `LogsAgent` | `MockAgent` | Queued `Evidence` objects |
| ... (all 6 agents) | `MockAgent` | Queued `Evidence` objects |
| `IncidentRepository` | `InMemoryIncidentRepository` | Dict-backed store with `save_count` |

### MockLLMProvider

Stores a queue of responses per output schema type. Each `generate_structured` call pops and returns the next response. Supports multiple calls to the same schema (e.g., two `_deepen` calls both needing `DeepenHypothesesOutput`).

```python
mock_llm.register(GenerateHypothesesOutput, GenerateHypothesesOutput(
    hypotheses=[HypothesisProposal(statement="DB connections exhausted")]
))
```

### MockAgent

Stores a queue of `Evidence` objects. Applies `hypothesis_id` from the call. Records all calls for assertion.

```python
agents["metrics"].register(Evidence(source=PROMETHEUS, query="q", summary="ok"))
# After investigation:
assert agents["metrics"].calls[0]["hypothesis_id"] is None  # exploratory
assert agents["metrics"].calls[1]["hypothesis_id"] == hyp.id  # targeted
```

### Position-based fallback

The LLM's `hypothesis_id` in evaluations is a position-based fallback. Mocks use `"will-be-set"` as a placeholder. The `_find_evaluation` method matches by ID first, then falls back to position. This avoids tests needing to know auto-generated UUIDs.

## Consequences

### Positive

- **Fast.** 57 tests run in 0.77s. No network calls, no LLM API latency.
- **Deterministic.** Same inputs → same outputs. No flaky tests from LLM non-determinism.
- **Free.** No API costs.
- **Tests mechanics, not judgment.** Tests verify the Coordinator's workflow logic — does it call the right agents, apply confirm/refute correctly, prune refuted branches, produce RCA. The LLM's judgment (is this hypothesis actually correct?) is tested separately through manual integration tests.
- **`demo()` pattern.** The `demo()` function in `test_coordinator.py` runs a full integration scenario with mocks. It traces the entire lifecycle and asserts the outcome. This serves as documentation and a smoke test.

### Negative

- **Doesn't test LLM quality.** Mock tests don't catch bad prompts, hallucinations, or poor RCA quality. These are tested through manual integration runs with `observability-agent investigate --mock-llm` (uses real LLM with mock agents) or full end-to-end runs.
- **Mock setup is verbose.** Each test registers 4-6 mock responses in sequence. The `register_exploratory` helper reduces boilerplate for the exploratory phase.

## Alternatives Not Chosen

- **VCR-style recording (replay recorded LLM responses).** Would capture real LLM behavior but introduce non-determinism (different runs produce different responses). Mock responses are hand-crafted to test specific scenarios.
- **Real LLM in tests.** Expensive, slow, flaky. Rejected for unit tests. The `demo()` function and `--mock-llm` CLI flag provide integration testing with the real LLM.
- **Fake LLM (rule-based).** A fake LLM that generates responses from templates would be more complex than the mock and harder to reason about. The mock's explicit queue is more transparent.